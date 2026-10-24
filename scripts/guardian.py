# -*- coding: utf-8 -*-
r"""
qmtIDE-deepseek 守护进程（Guardian）

长期后台运行，每 CHECK_INTERVAL 秒调用 scripts/health_check 检测并自启动
交易引擎(127.0.0.1:5000)；miniQMT 已改为手动启动，守护不再自动拉起。

用途：保障交易系统 7x24 在线，最大化策略执行时间 → 支撑「优秀收益」目标。
安全：仅做进程/端口存活保障，绝不改动任何策略参数或生产配置。

部署（在可达 xtdata 的持久桌面会话中后台运行）：
    C:\Users\lisinan\.conda\envs\qmt\python.exe scripts/guardian.py
仅检查一次：
    C:\Users\lisinan\.conda\envs\qmt\python.exe scripts/guardian.py --check-once
"""
from __future__ import annotations

import argparse
import datetime
import os
import pathlib
import subprocess
import sys
import time

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "scripts"))
import health_check  # noqa: E402

LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "guardian.log"
PID_FILE = BASE_DIR / ".guardian.pid"
CHECK_INTERVAL = 300  # 5 分钟


def log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _pid_alive(pid: str) -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        return str(pid) in out
    except Exception:
        return False


def _atomic_claim_pid_file() -> bool:
    """原子占位单实例锁：O_CREAT|O_EXCL 打开即写入自身 pid。

    True=本进程抢到锁；False=文件已存在且旧 pid 仍存活（另一实例在跑）。
    旧 pid 已死（崩溃残留）时删除后重试一次，避免僵尸锁让守护永不再启动。
    """
    for _attempt in (1, 2):
        try:
            fd = os.open(str(PID_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            old = ""
            try:
                old = PID_FILE.read_text().strip()
            except Exception:
                pass
            if old and _pid_alive(old):
                log(f"[WARN] 已有 guardian 实例 pid={old}，退出")
                return False
            # 残留锁（进程已不在）：清掉后重试一次
            try:
                PID_FILE.unlink()
            except Exception:
                pass
            continue
        except Exception as exc:
            log(f"[WARN] 创建 guardian pid 文件失败(继续运行): {exc}")
            return True
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
        except Exception:
            pass
        finally:
            os.close(fd)
        return True
    return False


def _alert_degraded(r: dict) -> None:
    """degraded 非空时写一条 [GUARDIAN-ALERT]（按天去重：每天每类只报一次）。"""
    degraded = r.get("degraded") or []
    if not degraded:
        return
    stamp = LOG_DIR / ".guardian_alert_last"
    today = datetime.date.today().isoformat()
    seen = ""
    try:
        seen = stamp.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    seen_parts = seen.split("|", 1)
    pending = [d for d in degraded
               if not (len(seen_parts) == 2 and seen_parts[0] == today
                       and d in seen_parts[1].split(","))]
    if not pending:
        return
    log(f"[GUARDIAN-ALERT] degraded={','.join(degraded)}"
        f" engine_progressing={r.get('engine_progressing')}"
        f" engine={r.get('engine')} miniQMT={r.get('miniqmt')}")
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        done = seen_parts[1].split(",") if (len(seen_parts) == 2
                                            and seen_parts[0] == today) else []
        merged = ",".join(sorted(set([d for d in done if d] + pending)))
        stamp.write_text(f"{today}|{merged}", encoding="utf-8")
    except Exception:
        pass


def guard_once() -> None:
    r = health_check.status(do_start=True)
    msg = f"miniQMT={r.get('miniqmt')} engine={r.get('engine')}"
    if "miniqmt_started" in r:
        msg += f" (miniqmt_started={r['miniqmt_started']})"
    if "engine_started" in r:
        msg += f" (engine_started={r['engine_started']})"
    log(msg)
    _alert_degraded(r)


def main() -> None:
    ap = argparse.ArgumentParser(description="qmtIDE-deepseek 守护进程")
    ap.add_argument("--check-once", action="store_true", help="仅检查一次并退出")
    ap.add_argument("--interval", type=int, default=CHECK_INTERVAL)
    args = ap.parse_args()

    if args.check_once:
        guard_once()
        return

    # 单实例守卫：原子占位（O_CREAT|O_EXCL），避免双触发竞态下 guardian 堆积
    if not _atomic_claim_pid_file():
        return

    log(f"Guardian 启动 pid={os.getpid()} interval={args.interval}s")
    try:
        while True:
            guard_once()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log("Guardian 收到中断，退出")
    finally:
        try:
            if PID_FILE.exists() and PID_FILE.read_text().strip() == str(os.getpid()):
                PID_FILE.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    main()

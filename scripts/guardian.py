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


def guard_once() -> None:
    r = health_check.status(do_start=True)
    msg = f"miniQMT={r.get('miniqmt')} engine={r.get('engine')}"
    if "miniqmt_started" in r:
        msg += f" (miniqmt_started={r['miniqmt_started']})"
    if "engine_started" in r:
        msg += f" (engine_started={r['engine_started']})"
    log(msg)


def main() -> None:
    ap = argparse.ArgumentParser(description="qmtIDE-deepseek 守护进程")
    ap.add_argument("--check-once", action="store_true", help="仅检查一次并退出")
    ap.add_argument("--interval", type=int, default=CHECK_INTERVAL)
    args = ap.parse_args()

    if args.check_once:
        guard_once()
        return

    # 单实例守卫：避免重复 guardian 堆积
    try:
        if PID_FILE.exists():
            old = PID_FILE.read_text().strip()
            if old and _pid_alive(old):
                log(f"[WARN] 已有 guardian 实例 pid={old}，退出")
                return
    except Exception:
        pass
    try:
        PID_FILE.write_text(str(os.getpid()))
    except Exception:
        pass

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

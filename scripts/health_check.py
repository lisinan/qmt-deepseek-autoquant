# -*- coding: utf-8 -*-
"""
qmtIDE-deepseek 存活健康检查 + 自启动（只读/自愈两用）

- is_miniqmt_up() : tasklist 检测 XtMiniQmt.exe 是否运行（常驻引擎，非拉起器）
- is_engine_up()  : socket 探测 127.0.0.1:5000（引擎 Web 端口）
- engine_progressing(): 只读 equity_snapshots 最新 ts，判「引擎是否还在推进」
- ensure_miniqmt(): 受 MINIQMT_AUTOSTART 开关控制；当前=False，不自启（用户手动启动）
- ensure_engine() : 停止则自启动 python main.py --web（conda env qmt）

CLI:
  python scripts/health_check.py            # 只读检查并打印
  python scripts/health_check.py --json     # 输出 JSON 状态
  python scripts/health_check.py --start    # 停止则自启动并复核

安全：仅做进程/端口存活保障，绝不改动任何策略参数或生产配置。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import socket
import subprocess
import sys
import urllib.request


def _ensure_conda_dll_paths() -> None:
    """把 conda 环境的 DLL 目录加进本进程的搜索路径（必须在 import sqlite3 之前）。

    ★ 2026-10-24 事故：本文件为「引擎活性判定」新增了 ``import sqlite3``，
    而 guardian 由开机脚本以 **pythonw.exe** 拉起、**不带 conda 的 PATH**，
    于是 ``import _sqlite3`` 直接 ``DLL load failed`` ⇒ **守护进程静默崩溃**，
    重启后既没有 guardian 也没有引擎（当天是周六，未造成交易影响）。
    修法：按 sys.executable 推出环境根目录，把 Library\\bin / DLLs 等一并加入
    ``os.add_dll_directory`` 与 PATH，再 import sqlite3。
    """
    try:
        root = pathlib.Path(sys.executable).resolve().parent
        for sub in ("Library\\bin", "DLLs", "Library\\mingw-w64\\bin",
                    "Library\\usr\\bin", "Scripts"):
            p = root / sub
            if not p.is_dir():
                continue
            try:
                os.add_dll_directory(str(p))       # Python 3.8+
            except Exception:
                pass
            cur = os.environ.get("PATH", "")
            if str(p) not in cur:
                os.environ["PATH"] = str(p) + os.pathsep + cur
    except Exception:
        pass


_ensure_conda_dll_paths()
import sqlite3  # noqa: E402  （必须在 _ensure_conda_dll_paths() 之后）

# Windows 控制台 UTF-8（本文件会打印中文）。不加这段时，cp1252 控制台上 print
# 会抛 UnicodeEncodeError，导致 --start 在拉起引擎后打印「已尝试启动」时崩溃
# （引擎其实已成功拉起，但调用方看到的是 exit=1，容易误判为启动失败）。
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
MINIQMT_EXE = r"C:\pazq_qmt\bin.x64\XtItClient.exe"  # 拉起器：双击启动 miniQMT 的入口
# 真正常驻的引擎进程是 XtMiniQmt.exe；XtItClient.exe 仅作拉起器，启动后即退出，
# 不能用来判定"是否在运行"，否则会误报"miniQMT 退出"并反复自启动（2026-09-15 确诊）。
MINIQMT_ENGINE_PROC = "XtMiniQmt.exe"
MINIQMT_LAUNCHER_PROC = "XtItClient.exe"
# 是否允许守护/自启逻辑自动拉起 miniQMT。2026-09-15 起设为 False：
# 用户改为手动启动 miniQMT（避免自动拉起与券商断线重连的乒乓效应）。
# 置 True 可恢复自动拉起。
MINIQMT_AUTOSTART = False
CONDA_PY = r"C:\Users\lisinan\.conda\envs\qmt\python.exe"
WEB_HOST = "127.0.0.1"
WEB_PORT = 5000
DB_PATH = BASE_DIR / "storage" / "qmt.db"
# 交易时段内权益快照最多允许多久不更新。引擎在主循环里对 _persist_equity
# 做 ~60s 节流（REFRESH_INTERVAL=3s），正常情况下 1 分钟内必有一条新快照；
# 因此超过 15 分钟毫无新快照说明主循环本身卡死/空转（进程还在、端口还通，
# 但已经不再推进），这是 is_engine_up() 探测不到的那类故障。
SNAPSHOT_STALE_SEC = 15 * 60
# 进程存活但心跳接口读不到时（端口被别的进程占用）才回退用快照龄期判定。
SNAPSHOT_STALE_FALLBACK_SEC = 60 * 60


def _log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        log_dir = BASE_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "health_check.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def is_miniqmt_up() -> bool | None:
    """True=引擎在运行 / False=未运行 / None=检测失败(未知)。

    存活判定以常驻引擎进程 XtMiniQmt.exe 为准；XtItClient.exe 仅是拉起器，
    启动后即退出，不能用于判定存活，否则会误报"miniQMT 退出"并反复自启动。
    """
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {MINIQMT_ENGINE_PROC}"],
            capture_output=True, text=True, timeout=15,
        ).stdout
        if MINIQMT_ENGINE_PROC in out:
            return True
        # 兜底：极旧版本可能直接以拉起器进程常驻
        out2 = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {MINIQMT_LAUNCHER_PROC}"],
            capture_output=True, text=True, timeout=15,
        ).stdout
        return MINIQMT_LAUNCHER_PROC in out2
    except Exception as exc:  # 检测失败保守视为存活，避免误重启
        _log(f"[WARN] tasklist 检测 miniQMT 失败: {exc}")
        return None


def is_engine_up() -> bool:
    try:
        with socket.create_connection((WEB_HOST, WEB_PORT), timeout=3):
            return True
    except Exception:
        return False


def _in_trading_session(now: datetime.datetime) -> bool:
    """当前是否处于交易时段（复用 core.market_calendar，只读判定）。

    判定失败时保守返回 False：宁可漏报 stalled，也不要非交易时段误报。
    """
    try:
        if str(BASE_DIR) not in sys.path:
            sys.path.insert(0, str(BASE_DIR))
        from core.market_calendar import is_trading_time
        return bool(is_trading_time(now))
    except Exception as exc:
        _log(f"[WARN] 交易时段判定失败(按非交易时段处理): {exc}")
        return False


def _latest_snapshot_age_sec(now: datetime.datetime):
    """equity_snapshots 最新 ts 距 now 的秒数；读不到返回 None（=未知）。

    只读打开：``mode=ro`` 会以只读 URI 连接库文件，绝不写入/建表，
    因此守护与引擎并发访问是安全的（WAL 下读者不阻塞写者）。
    """
    if not DB_PATH.exists():
        return None
    try:
        uri = DB_PATH.resolve().as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            row = con.execute("SELECT MAX(ts) FROM equity_snapshots").fetchone()
        finally:
            con.close()
        raw = (row[0] if row else None) or ""
        raw = str(raw).strip()
        if not raw:
            return None
        ts = datetime.datetime.fromisoformat(raw[:26].replace("Z", ""))
        if ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
        return (now - ts).total_seconds()
    except Exception as exc:
        _log(f"[WARN] 读取权益快照时间失败(视为未知): {exc}")
        return None


def _engine_alive(engine_up: bool) -> bool:
    """引擎进程是否还活着：端口通道或 main.py 进程任一存在即为活着。

    为什么要额外看进程：主循环卡死时 Flask 线程未必响应，但进程还在。
    快照停更只有配上「进程还在」才是「在跑但不推进」的故障信号。
    """
    if engine_up:
        return True
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'", "get", "commandline"],
            capture_output=True, text=True, timeout=15,
        ).stdout or ""
        return "main.py" in out.lower()
    except Exception:
        return False


def _engine_heartbeat_ok() -> bool | None:
    """引擎自带心跳接口 /api/snapshot 是否可读（额外佐证主循环在推进）。

    True=可读 / False=进程在但读不到 / None=无法判定（连接失败等）。
    端口可能被别的进程占用，所以 True 不能直接当结论，只用于否决误报。
    """
    try:
        with urllib.request.urlopen(
                f"http://{WEB_HOST}:{WEB_PORT}/api/snapshot", timeout=4) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            return bool(json.loads(resp.read().decode("utf-8", "replace")))
    except Exception:
        return None


def engine_progressing(engine_up: bool | None = None,
                       now: datetime.datetime | None = None) -> bool | None:
    """引擎是否「在推进」：True=正常 / False=停更（stalled）/ None=无法判定。

    只在交易时段内下结论，非交易时段一律返回 None（引擎本就休眠，快照停更
    属正常）；DB 打不开同样返回 None —— 无法观测不等于故障。
    """
    now = now or datetime.datetime.now()
    if not _in_trading_session(now):
        return None
    age = _latest_snapshot_age_sec(now)
    if age is None:
        return None
    if engine_up is None:
        engine_up = is_engine_up()
    if not _engine_alive(engine_up):
        # 进程都没了：属 is_engine_up()/guardian 的拉起职责，这里不重复判定。
        return None
    if age <= SNAPSHOT_STALE_SEC:
        return True
    if not engine_up:
        # 端口未通：可能只是 HTTP 线程忙碌，用更宽的上限，且不下 stalled 结论。
        return True if age <= SNAPSHOT_STALE_FALLBACK_SEC else None
    if _engine_heartbeat_ok():
        # 端口通且心跳可读 ⇒ 主循环确实在推进，快照停更属节流/空闲，不判 stalled。
        return True
    _log(f"[WARN] 引擎权益快照已停更 {age / 60:.0f} 分钟（进程/端口在，主循环疑似卡死）")
    return False


def ensure_miniqmt(log=_log) -> bool:
    if not MINIQMT_AUTOSTART:
        log("[INFO] miniQMT 自动启动已禁用（按设置由用户手动启动），跳过拉起")
        return False
    if is_miniqmt_up():
        return True
    try:
        if not os.path.exists(MINIQMT_EXE):
            log(f"[ERR] miniQMT 不存在: {MINIQMT_EXE}")
            return False
        # DETACHED_PROCESS(0x8)：让 GUI 进程脱离守护父进程独立存活，
        # 避免父进程退出时连带终止。注意：进程能否真正常驻仍取决于能否在
        # 可交互会话内完成账户登录——守护只能拉起，不能替你输入密码。
        subprocess.Popen([MINIQMT_EXE], creationflags=0x00000008)
        log("[ACTION] 已尝试启动 miniQMT (DETACHED)")
        return True
    except Exception as exc:
        log(f"[ERR] 启动 miniQMT 失败: {exc}")
        return False


def ensure_engine(log=_log) -> bool:
    if is_engine_up():
        return True
    try:
        env = dict(os.environ)
        for p in [
            r"C:\Users\lisinan\.conda\envs\qmt\Library\bin",
            r"C:\Users\lisinan\.conda\envs\qmt\DLLs",
        ]:
            if p not in env.get("PATH", ""):
                env["PATH"] = p + os.pathsep + env.get("PATH", "")
        # DETACHED_PROCESS: 引擎独立于守护进程存活
        # 关键修复(R29 重启): 必须将 stdout/stderr 重定向到日志文件。否则父进程
        # (health_check/guardian) 退出后子进程写管道会触发 BrokenPipe 崩溃，
        # 导致引擎被拉起后秒退（崩溃循环，曾见 PID 26368/17292/6108 反复自退）。
        _log_dir = BASE_DIR / "logs"
        _log_dir.mkdir(parents=True, exist_ok=True)
        _eng_out = open(_log_dir / "engine_stdout.log", "a", buffering=1, encoding="utf-8")
        subprocess.Popen(
            [CONDA_PY, "main.py", "--web"],
            cwd=str(BASE_DIR), env=env, creationflags=0x00000008,
            stdout=_eng_out, stderr=subprocess.STDOUT,
        )
        log("[ACTION] 已尝试启动交易引擎 main.py --web")
        return True
    except Exception as exc:
        log(f"[ERR] 启动交易引擎失败: {exc}")
        return False


def engine_log_freshness(max_stale_sec: int = 1800):
    """引擎滚存日志是否还在增长（2026-10-22 事故新增）。

    事故：2026-10-21 22:11 启动的引擎进程**一直在跑**（notices.log 与 DB 每秒在写、
    端口 5000 可连、心跳正常），但 ``quant_system.log`` 自启动前就再没更新过，
    整整 12 小时没有任何策略/漏斗日志 —— 事后「今天为什么没买」只能靠临时脚本重算。
    这类故障 is_engine_up()（端口）与 engine_progressing()（权益快照）都发现不了。

    判据：``logs/quant_system*.log`` 与 ``logs/engine_stdout.log`` 的**最新 mtime**
    距今超过 ``max_stale_sec``（默认 30 分钟；引擎每 10 分钟必有心跳）即判 stale。
    返回 None=无法判定（日志目录不存在），True=在写，False=断供。
    """
    if not (BASE_DIR / "logs").exists():
        return None
    newest = 0.0
    for pat in ("quant_system.log", "quant_system-*.log", "engine_stdout.log"):
        for p in (BASE_DIR / "logs").glob(pat):
            try:
                newest = max(newest, p.stat().st_mtime)
            except Exception:
                continue
    if newest <= 0:
        return None
    import time as _t
    return (_t.time() - newest) <= max_stale_sec


def status(do_start: bool = False) -> dict:
    r: dict = {"miniqmt": is_miniqmt_up(), "engine": is_engine_up()}
    # 引擎是否仍在推进（契约 C12）。None=无法判定（非交易时段 / DB 打不开），
    # 不计入失败：告警只看 degraded 列表是否非空。
    _prog = engine_progressing(r["engine"])
    r["engine_progressing"] = _prog
    r["degraded"] = [] if _prog is not False else ["engine_stalled"]
    # 【2026-10-22】引擎活着但滚存日志断供（运维盲区，见 engine_log_freshness）：
    # 只有引擎在跑时才判定，否则离线状态本就无日志。
    _log_ok = engine_log_freshness() if r["engine"] else None
    r["engine_log_fresh"] = _log_ok
    if _log_ok is False:
        r["degraded"].append("engine_log_stale")
    if do_start:
        if not r["miniqmt"]:
            r["miniqmt_started"] = ensure_miniqmt()
        if not r["engine"]:
            r["engine_started"] = ensure_engine()
        import time
        time.sleep(2)
        after_mq = is_miniqmt_up()
        r["miniqmt"] = after_mq
        r["engine"] = is_engine_up()
        # 诊断：尝试自启动后进程仍不在（且非检测失败 None），说明进程拉起后
        # 自行退出——多为账户未登录/会话失效/账号未开通 miniQMT(极速交易)权限，
        # 需用户在桌面会话手动登录，守护无法替其完成登录。
        if r.get("miniqmt_started") and after_mq is False:
            r["miniqmt_exited_after_start"] = True
    return r


def main() -> int:
    ap = argparse.ArgumentParser(description="qmtIDE-deepseek 存活健康检查")
    ap.add_argument("--start", action="store_true", help="停止则自启动")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()
    r = status(do_start=args.start)
    if args.json:
        print(json.dumps(r, ensure_ascii=False))
    else:
        extra = ""
        if "miniqmt_started" in r:
            extra += f" | miniqmt_started={r['miniqmt_started']}"
        if "engine_started" in r:
            extra += f" | engine_started={r['engine_started']}"
        if r.get("miniqmt_exited_after_start"):
            extra += " | miniqmt_exited_after_start=True"
        if r.get("degraded"):
            extra += f" | degraded={','.join(r['degraded'])}"
        print(f"miniQMT={r.get('miniqmt')} | engine(5000)={r.get('engine')}"
              f" | engine_progressing={r.get('engine_progressing')}{extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

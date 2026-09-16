# -*- coding: utf-8 -*-
"""
qmtIDE-deepseek 存活健康检查 + 自启动（只读/自愈两用）

- is_miniqmt_up() : tasklist 检测 XtMiniQmt.exe 是否运行（常驻引擎，非拉起器）
- is_engine_up()  : socket 探测 127.0.0.1:5000（引擎 Web 端口）
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
        subprocess.Popen(
            [CONDA_PY, "main.py", "--web"],
            cwd=str(BASE_DIR), env=env, creationflags=0x00000008,
        )
        log("[ACTION] 已尝试启动交易引擎 main.py --web")
        return True
    except Exception as exc:
        log(f"[ERR] 启动交易引擎失败: {exc}")
        return False


def status(do_start: bool = False) -> dict:
    r: dict = {"miniqmt": is_miniqmt_up(), "engine": is_engine_up()}
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
        print(f"miniQMT={r.get('miniqmt')} | engine(5000)={r.get('engine')}{extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

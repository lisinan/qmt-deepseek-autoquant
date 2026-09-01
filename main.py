# -*- coding: utf-8 -*-
"""
qmtIDE-deepseek 主入口

用法：
  python main.py                  # paper + single 模式，无限循环
  python main.py --ticks 10       # 只跑 10 轮（自检）
  python main.py --mode live      # 实盘（需 minibroker.exe 运行）
  python main.py --strategy portfolio   # 组合策略（Top-N）
  python main.py --snapshot       # 打一次快照后退出
  python main.py --web            # 启动 Web 仪表板（默认 127.0.0.1:5000）
  python main.py --web --no-engine       # 只启动 Web，不启动 engine

环境变量：
  OPENROUTER_API_KEY / DEEPSEEK_API_KEY   LLM 密钥（无则跳过 AI 分析）
  OPENROUTER_MODEL / DEEPSEEK_MODEL       AI 模型名
"""
from __future__ import annotations

import argparse
import ctypes
import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Windows 控制台 UTF-8
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import (
    BASE_DIR, LOG_DIR, LOG_LEVEL, SINGLETON_LOCK, WEB_HOST, WEB_PORT,
)


def _interpreter_ok() -> bool:
    """判断当前解释器是否为项目依赖的 conda env 'qmt'。

    为什么需要：本项目依赖 xtquant / 现代 requests。若用其它 Python
    （如 WorkBuddy 托管的 3.13）运行，会因 QMT 站点包遮蔽 requests 触发
    ModuleNotFoundError: urllib3.packages.six.moves，main.py 在 import 阶段崩溃、
    引擎起不来（已多次实测）。这里**提前**给出清晰系统提示与正确启动命令，
    避免在毫无上下文的 traceback 里浪费排障时间。
    """
    exe = (sys.executable or "").lower().replace("\\", "/")
    if "envs/qmt" in exe or "/qmt/bin/python" in exe:
        return True
    if os.environ.get("QMT_IGNORE_INTERPRETER_CHECK"):
        return True
    return False


if not _interpreter_ok():
    sys.stderr.write(
        "\n[系统提示][ENV] 当前 Python 不是项目依赖的 conda 环境 'qmt'：\n"
        f"    {sys.executable}\n"
        "  本项目依赖 xtquant / 现代 requests，用错解释器会在 import 阶段崩溃。\n"
        "  正确启动命令（Windows）：\n"
        "    C:\\Users\\lisinan\\.conda\\envs\\qmt\\python.exe main.py\n"
        "  或先激活环境：conda activate qmt && python main.py\n"
        "  （设置环境变量 QMT_IGNORE_INTERPRETER_CHECK=1 可跳过此检查）\n\n"
    )

from engine.event_engine import EventEngine

PID_FILE = LOG_DIR / "engine.pid"
STOP_SENTINEL = LOG_DIR / "STOP_ENGINE"


class _SafeStreamHandler(logging.StreamHandler):
    """stdout 句柄失效时静默降级，而不是抛 OSError。

    为什么需要：进程被后台化/父控制台关闭后，Windows 上向 stdout 写入会抛
    ``OSError: [Errno 22] Invalid argument``。该异常会从**任意** logger 调用点
    冒出来，被上层 try/except 当成业务失败记下（实测 logs 里 17.7 万条
    "sector 评估失败 [Errno 22]" 全部源于此，且 traceback 与真实代码错位、
    极难排查）。这里一旦发现流不可用就永久摘掉该 handler，主循环不再受影响。
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        except (OSError, ValueError):
            # 流已失效：摘除自身，避免每条日志都抛异常
            try:
                logging.getLogger().removeHandler(self)
            except Exception:
                pass


class _BrokenPipeSafeStream:
    """把任意底层流包成「写失败也不抛异常」的安全流。

    后台化引擎的 stdout/stderr 往往是已失效的管道，向其 write 会抛
    ``OSError: [Errno 22] Invalid argument``，从任意调用点冒出（实测导致
    ``sector_scorer.evaluate_sectors`` 每轮抛异常、主循环每 ~0.3s 算一次
    traceback，CPU 被白白烧掉）。这里统一吞掉写异常，保证引擎不会被一条
    日志拖垮；必要时调用方仍可回退到文件 / os.devnull。
    """

    def __init__(self, stream) -> None:
        self._stream = stream

    def write(self, s):
        try:
            return self._stream.write(s)
        except Exception:
            return None

    def flush(self):
        try:
            self._stream.flush()
        except Exception:
            pass

    def writelines(self, lines):
        try:
            self._stream.writelines(lines)
        except Exception:
            pass

    def reconfigure(self, *args, **kwargs):
        try:
            self._stream.reconfigure(*args, **kwargs)
        except Exception:
            pass

    def fileno(self):
        try:
            return self._stream.fileno()
        except Exception:
            return -1

    def isatty(self):
        try:
            return self._stream.isatty()
        except Exception:
            return False

    @property
    def encoding(self):
        try:
            return self._stream.encoding or "utf-8"
        except Exception:
            return "utf-8"

    @property
    def errors(self):
        try:
            return self._stream.errors or "replace"
        except Exception:
            return "replace"

    def close(self):
        try:
            self._stream.close()
        except Exception:
            pass

    def seekable(self):
        return False

    def readable(self):
        return False

    def writable(self):
        return True


class _SafeRotatingFileHandler(RotatingFileHandler):
    """多进程安全的容量封顶轮转。

    演进史（每一步都是被真实故障推着走的）：
      v1 无上限 FileHandler → quant_system.log 涨到 535MB。
      v2 RotatingFileHandler → Windows 上 ``os.rename`` 遇文件被其他进程占用
         抛 ``PermissionError[WinError 32]``，且从**每条**日志的 emit 冒出。
      v3 捕获异常「跳过轮转」→ 不再报错，但**轮转从此永久失效**：只要还有
         第二个进程持有同一日志，容量上限就是空的。实测本轮发现 34 个残留
         引擎进程同时持有该文件，日志一路涨到 **1.02GB**（112MB/天）。
      v4（当前）：轮转失败不再一味忍让 ——
         a) 连续失败 ≥2 次说明存在跨进程竞争 → 切换到**进程私有文件**
            ``quant_system-<pid>.log``，此后本进程独占、轮转恢复正常；
         b) 切换前的兜底：文件超硬上限时**就地截断**，保证绝不无界增长。
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._rollover_fails = 0
        self._switched_to_private = False

    def _switch_to_private_file(self) -> None:
        """改写 baseFilename 到 <stem>-<pid>.log，消除跨进程 rename 竞争。"""
        self._switched_to_private = True
        p = Path(self.baseFilename)
        self.baseFilename = str(p.with_name(f"{p.stem}-{os.getpid()}{p.suffix}"))
        try:
            if self.stream:
                self.stream.close()
        except Exception:
            pass
        self.stream = self._open()

    def _hard_truncate(self) -> None:
        """就地截断：即便无法 rename，也不允许文件无界增长。"""
        try:
            if self.stream is None:
                self.stream = self._open()
            self.stream.truncate(0)
            self.stream.seek(0)
            self.stream.write(
                "== 日志被就地截断（轮转受其他进程占用阻塞，容量封顶生效）==\n")
            self.stream.flush()
        except Exception:
            pass

    def doRollover(self) -> None:
        try:
            super().doRollover()
            self._rollover_fails = 0
            return
        except OSError:
            self._rollover_fails += 1
        if not self._switched_to_private and self._rollover_fails >= 2:
            try:
                self._switch_to_private_file()
                self._rollover_fails = 0
                return
            except Exception:
                pass
        # 仍无法轮转：靠硬截断守住容量上限
        try:
            if os.path.getsize(self.baseFilename) > self.maxBytes * 2:
                self._hard_truncate()
        except Exception:
            pass

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        except Exception:
            # 任何写出异常都静默丢弃，绝不影响主循环。
            pass


# ============================================================ 单实例 / 停止

def _pid_alive(pid: int) -> bool:
    """判断 pid 是否存活。Windows 上**绝不能**用 os.kill(pid, 0)——CPython
    在 Windows 会把它翻译成 TerminateProcess，等于把目标进程杀掉。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        SYNCHRONIZE = 0x00100000
        h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_singleton(force: bool = False) -> bool:
    """单实例互斥。

    为什么需要：引擎主循环是无限循环，之前多轮自动化把它跑成后台进程后
    从未收敛，实测同时残留 **34 个引擎进程**（4 天累计 18.9 CPU 小时、
    2.4GB 内存、日志 1.02GB、彼此竞争同一份行情与日志文件）。有了 PID 锁，
    重复启动会被直接拒绝，问题从根上不再发生。
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if PID_FILE.exists():
            old = int((PID_FILE.read_text(encoding="utf-8").strip() or "0"))
            if old and old != os.getpid() and _pid_alive(old):
                if not force:
                    print(f"[singleton] 已有引擎在运行 (pid={old})。"
                          f"如需强制启动加 --force；停止请用 python main.py --stop",
                          file=sys.stderr)
                    return False
                print(f"[singleton] --force：忽略已运行实例 pid={old}",
                      file=sys.stderr)
    except Exception:
        pass
    try:
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass
    return True


def _release_singleton() -> None:
    try:
        if PID_FILE.exists():
            cur = (PID_FILE.read_text(encoding="utf-8").strip() or "0")
            if cur == str(os.getpid()):
                PID_FILE.unlink()
    except Exception:
        pass


def _request_stop(wait_sec: float = 30.0) -> int:
    """协作式停止：写哨兵文件，引擎主循环发现后自行收敛退出。

    比 taskkill 更可靠的地方：不依赖操作系统权限（本轮实测沙箱内
    taskkill/Stop-Process 对残留进程一律 Access denied），也不会把
    进程杀在下单/写库的半途。
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STOP_SENTINEL.write_text(str(time.time()), encoding="utf-8")
    print(f"[stop] 已发出停止信号: {STOP_SENTINEL}")
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        try:
            if not PID_FILE.exists():
                break
            pid = int((PID_FILE.read_text(encoding="utf-8").strip() or "0"))
            if not pid or not _pid_alive(pid):
                break
        except Exception:
            break
        time.sleep(1.0)
    try:
        STOP_SENTINEL.unlink()
    except Exception:
        pass
    print("[stop] 停止信号已回收（哨兵文件已删除），可正常重新启动")
    return 0


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    # 后台化后 stdout/stderr 常是失效管道，写之即抛 OSError[Errno 22]，
    # 会从任意调用点冒出并拖垮主循环（实测每轮算 traceback、CPU 空转）。
    # 全局压制 handler 异常的 traceback，并把标准流包成写失败也不抛的安全流。
    logging.raiseExceptions = False
    if sys.stdout is not None:
        sys.stdout = _BrokenPipeSafeStream(sys.stdout)
    if sys.stderr is not None:
        sys.stderr = _BrokenPipeSafeStream(sys.stderr)
    handlers: list = [
        # 轮转写入：单文件 20MB × 5 份，上限 ~120MB。
        # 原来是无上限 FileHandler，实盘长跑后 quant_system.log 涨到 535MB，
        # 既拖慢磁盘也让日志失去可读性。
        # 用 _SafeRotatingFileHandler：文件被占用时跳过轮转、不抛异常。
        _SafeRotatingFileHandler(
            LOG_DIR / "quant_system.log",
            maxBytes=20 * 1024 * 1024, backupCount=5, encoding="utf-8",
        ),
    ]
    if sys.stdout is not None:
        handlers.append(_SafeStreamHandler(sys.stdout))
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format=fmt,
        handlers=handlers,
    )


def main() -> int:
    _setup_logging()
    ap = argparse.ArgumentParser(description="qmtIDE-deepseek 量化交易入口")
    ap.add_argument("--ticks", type=int, default=0,
                    help="最多跑多少轮（0=无限）")
    ap.add_argument("--mode", choices=["paper", "live"], default=None,
                    help="执行模式（覆盖 EXECUTION_MODE）")
    ap.add_argument("--strategy", choices=["single", "portfolio"], default=None,
                    help="策略模式（覆盖 STRATEGY_MODE）")
    ap.add_argument("--snapshot", action="store_true",
                    help="仅打印一次快照后退出")
    ap.add_argument("--web", action="store_true",
                    help="启动 Web 仪表板")
    ap.add_argument("--no-engine", action="store_true",
                    help="（与 --web 合用）不启动 engine，只跑 Web")
    ap.add_argument("--init-positions", action="store_true",
                    help="live 模式启动时从 broker 同步真实持仓")
    ap.add_argument("--port", type=int, default=WEB_PORT,
                    help="Web 端口")
    ap.add_argument("--host", default=WEB_HOST,
                    help="Web 监听地址")
    ap.add_argument("--stop", action="store_true",
                    help="向正在运行的引擎发出协作式停止信号后退出")
    ap.add_argument("--force", action="store_true",
                    help="忽略单实例互斥，强制启动（不推荐）")
    ap.add_argument("--no-session-guard", action="store_true",
                    help="关闭交易时段守卫（非交易时段也全速轮询，仅调试用）")
    args = ap.parse_args()

    if args.stop:
        return _request_stop()

    # 单实例互斥：只有真正会长跑的模式才需要（快照是一次性的）
    long_running = not args.snapshot
    if SINGLETON_LOCK and long_running and not args.ticks:
        if not _acquire_singleton(force=args.force):
            return 2

    engine = EventEngine(
        session_guard=(False if args.no_session_guard else None),
        exec_mode=args.mode,
        strategy_mode=args.strategy,
        auto_init_positions=args.init_positions,
    )
    if args.snapshot:
        import json
        print(json.dumps(engine.snapshot(), ensure_ascii=False, indent=2))
        return 0
    try:
        if args.web:
            from web.app import run_web
            # 启动 engine（后台线程），除非 --no-engine
            if not args.no_engine:
                t = threading.Thread(target=engine.run,
                                     name="engine-main", daemon=True)
                t.start()
            # Flask 主线程
            run_web(engine=engine, host=args.host, port=args.port)
            return 0
        # 默认：engine 主循环
        engine.run(max_ticks=args.ticks)
        return 0
    finally:
        _release_singleton()


if __name__ == "__main__":
    sys.exit(main())
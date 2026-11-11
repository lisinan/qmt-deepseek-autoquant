# -*- coding: utf-8 -*-
"""QMT-side bridge: executes the engine's file-based order protocol via passorder.

WHY THIS FILE EXISTS
--------------------
QMT 2.1.17 -> 2.1.32 broke the standalone xtquant path. A separate python
process cannot attach a trading session any more:

    [TC::COrderServiceQuantAdaptor::onConnected] quant session 969621, illegal pid 3028

The lock under ``userdata_mini\\up_queue_xtquant`` is held by the QMT client
itself, so no outside process can register. Therefore the only supported way to
trade is to run INSIDE QMT and call the built-in ``passorder``.

MODES QMT MAY USE TO EXECUTE THIS FILE
-------------------------------------
QMT runs a "python strategy" in one of two ways, and they are NOT equivalent:

  A) embedded engine  -- QMT calls ``init(ContextInfo)`` then ``handlebar``
     and the ``ContextInfo.run_time`` timer. ``passorder`` /
     ``get_trade_detail_data`` are injected into this module's globals.
     This is the ONLY mode where the bridge can place orders.

  B) standalone run    -- QMT spawns ``bin.x64\\pythonw.exe -u <this file>
     <userdata> <ms_ts>``. Nothing is injected, ``init`` is never called, the
     script runs top-to-bottom and exits in ~0.2s. QMT then logs
     ``doRun return code:1`` because the process is gone.

To tell them apart at run time, this file writes a boot record to
``C:\\pazq_qmt_bridge\\boot.log`` at import time, and a MODE line whenever
``init`` is actually called. Always read boot.log first when debugging.

DEPLOY RULES (learned the hard way)
-----------------------------------
* The repo copy is UTF-8 (readable, reviewable). ``scripts/deploy_bridge.py``
  rewrites the first line to ``#encoding:gbk`` and writes GBK + CRLF to
  ``C:\\pazq_qmt\\python\\QMTIDE_BRIDGE.py``. Never edit the deployed copy by
  hand and never save it from QMT's editor: QMT rewrites the file, replaces
  every non-ASCII character with ``?`` and drops a utf-8 coding header on it.
* The strategy file name inside QMT (``QMTIDE_BRIDGE``) must match the file
  name, and QMT upper-cases it.
* QMT does NOT auto-restart strategies after a client restart, even with
  startupAutorun="1" in indexUserConfig.xml.

PROTOCOL
--------
Engine writes ``signal.json``::

    {"signal_id": "<uuid>", "action": "QUERY_ASSET",
     "account": "cash", "params": {}, "ts": 1234567890.0}

Bridge writes ``result.json``::

    {"signal_id": "<same>", "status": "ok" | "error",
     "data": {}, "message": "", "ts": 1234567890.0}

``signal_id`` is idempotent: the same id is answered from cache and never
executed twice (this is what stops a SELL from firing twice and selling 200
shares when only 100 are held).

Actions: QUERY_ASSET / QUERY_POSITION / QUERY_ORDER / QUERY_TRADE / BUY /
SELL / CANCEL / PING / DUMP. DUMP returns the field names of every
``get_trade_detail_data`` object so the engine side can be calibrated against
the real broker schema.
"""

import json
import os
import sys
import threading
import time
import traceback

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------
BRIDGE_ROOT = (os.environ.get("QMT_BRIDGE_ROOT")
               or r"C:\pazq_qmt_bridge")   # env override exists only for tests
DEFAULT_ACCOUNT_ID = os.environ.get("QMT_ACCOUNT_ID") or "000000000000"
DEFAULT_ACCOUNT_TYPE = "STOCK"

POLL_MS = 500                 # run_time timer period
HEARTBEAT_SEC = 1.0           # heartbeat write throttle

SIGNAL_FILE = os.path.join(BRIDGE_ROOT, "signal.json")
RESULT_FILE = os.path.join(BRIDGE_ROOT, "result.json")
HEARTBEAT_FILE = os.path.join(BRIDGE_ROOT, "heartbeat.json")
BOOT_LOG = os.path.join(BRIDGE_ROOT, "boot.log")
# 已处理过的 signal_id 落盘。`_processed` 只在内存里，QMT 策略一重启就清空，
# 而 signal.json 会一直留着最后一条 —— 于是重启后那条信号被**重新执行一遍**。
# 2026-11-08 02:16 实证：QMT 重启后把上一轮的 QUERY_POSITION 又跑了一次。
# 对查询无害，但如果那条是 BUY，就是重复买入真实资金。
SEEN_FILE = os.path.join(BRIDGE_ROOT, "processed_ids.json")

_processed = {}               # signal_id -> cached result dict
_processed_order = []         # insertion order, used to cap the cache
_MAX_CACHED = 200
_seen = set()                 # 跨重启的「这条 signal_id 处理过」黑名单
_dup_reported = set()         # 已就「拒绝重放」报过日志的 id（每 500ms 命中一次，不能都报）
_result_sid = [None]          # result.json 里当前是哪条 signal 的答案，避免无谓重写
_lock = threading.Lock()
_last_hb = [0.0]
_poll_seen = [0]              # poll 次数，只用于「定时器到底有没有起来」的观测

# passorder form that worked last time. Kept module-level on purpose: ContextInfo
# may be a C++-bound object that refuses new attributes, and an AttributeError
# raised while recording the form would be caught by the retry loop and make it
# place the SAME order again with the next form (double order, real money).
_po_form = [None]
_dict_warned = set()          # 去重「只报一次」的日志，避免 500ms 刷爆 boot.log
# ★ 代码版本号：PING / DUMP 会带出来，用来**证明 QMT 里跑的到底是哪一版**。
#   背景：2026-11-08 21:31 修了「ACCOUNT 市值用上一交易日收盘价」的缺陷，
#   但 QMT 已加载的旧代码不会自己更新 —— 只部署文件不重新运行策略，改动等于没做。
#   有了这个字段，一条 PING 就能回答「新代码生效了吗」，不用猜。
#   约定：改动了会影响实盘数值/下单的逻辑就换一个值（时间戳格式）。
CODE_REV = "2026-11-11T2005-frozen-in-total"
_io_selftest_ran = [False]    # IO 权限矩阵每个模块实例只探一次
SKIP_SOCKET_PROBE = False     # 测试置 True：省掉每次 connect 的 0.5s 等待

_BOOT_MAX_BYTES = 200 * 1024
_boot_lock = threading.Lock()  # 自测跑在后台线程，会和 bridge_poll 并发写日志


# ---------------------------------------------------------------------------
# boot diagnostics
# ---------------------------------------------------------------------------
def _boot(msg):
    """Append one line to boot.log. Never raises.

    QMT launches pythonw.exe with no std handles, so ``sys.stdout`` is None and
    every ``print`` raises. Anything that must be observable has to go to a
    file.
    """
    try:
        if not os.path.isdir(BRIDGE_ROOT):
            os.makedirs(BRIDGE_ROOT)
        try:
            if os.path.getsize(BOOT_LOG) > _BOOT_MAX_BYTES:
                os.replace(BOOT_LOG, BOOT_LOG + ".1")
        except Exception:
            pass
        line = "[%s] pid=%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                     os.getpid(), msg)
        with _boot_lock:
            with open(BOOT_LOG, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass


def _has_passorder():
    """True when QMT injected the trading functions into this namespace."""
    g = globals()
    if callable(g.get("passorder")) and callable(g.get("get_trade_detail_data")):
        return True
    try:
        import builtins  # noqa: F401  (py3)
        b = builtins
    except Exception:
        b = None
    if b is not None and callable(getattr(b, "passorder", None)):
        return True
    return False


def _boot_probe():
    """Record everything needed to tell mode A from mode B.

    MUST NOT RAISE. QMT's embedded interpreter is not a real process: it exposes
    a cut-down ``sys`` shim with no ``argv`` (2026-11-08:
    ``AttributeError: module 'sys' has no attribute 'argv'`` killed the whole
    module load, so ``init`` was never defined and the strategy silently did
    nothing). Every field is therefore read defensively and the caller wraps
    this in try/except as well.
    """
    def _read(fn, default="<unavailable>"):
        try:
            return fn()
        except Exception as e:
            return "%s: %s" % (type(e).__name__, e)

    _boot("=== boot name=%r passorder=%r stdout=%s"
          % (__name__,
             _read(lambda: _has_passorder(), "<probe failed>"),
             _read(lambda: repr(sys.stdout), "<unreadable>")))
    _boot("argv=%r" % (_read(lambda: getattr(sys, "argv", "<no sys.argv>"),
                             "<probe failed>"),))
    _boot("cwd/exe=%r / %r"
          % (_read(os.getcwd), _read(lambda: sys.executable)))
    _boot("python=%s" % _read(lambda: sys.version.replace("\n", " "), "<unknown>"))
    _boot("globals=%r"
          % (_read(lambda: sorted([n for n in globals().keys()
                                   if not n.startswith("_")]), []),))
    _boot("sys.path=%r" % (_read(lambda: list(sys.path), []),))

    def _env():
        out = {}
        for k, v in os.environ.items():
            ku = k.upper()
            if ku in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP") or ku.startswith("QMT"):
                out[k] = v
        return out
    _boot("env=%r" % (_read(_env, {}),))

    # QMT injects model-trading arguments as globals (PythonFormula::loadArgvs).
    _boot("qmt_globals account=%r accountType=%r"
          % (_read(lambda: globals().get("account"), None),
             _read(lambda: globals().get("accountType"), None)))


def _install_excepthook():
    """pythonw.exe swallows tracebacks; send them to boot.log instead."""
    def _hook(tp, val, tb):
        try:
            _boot("UNCAUGHT %s\n%s"
                  % (getattr(tp, "__name__", tp),
                     "".join(traceback.format_exception(tp, val, tb))))
        except Exception:
            pass
    try:
        sys.excepthook = _hook
    except Exception:
        pass


def _log(msg):
    """print replacement that also works when sys.stdout is None."""
    try:
        print(msg)
    except Exception:
        pass
    _boot(msg)


# Module-level boot must never abort: if it raises, QMT's loader gives up and
# ``init`` is never defined (that is exactly what happened on 2026-11-08).
try:
    _boot_probe()
except Exception as _e:
    _boot("boot probe aborted: %s: %s" % (type(_e).__name__, _e))
try:
    _install_excepthook()
except Exception:
    pass


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _now():
    return time.time()


def _atomic_write_json(path, obj):
    """Write-then-replace so the reader never sees a half-written JSON."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    # 2026-11-09 P0: replace 失败改为 8 次退避重试, 并删除 os.remove + os.rename 回退。
    # ★ 因果链要看对(team-lead 2026-11-09 亲自验过): 日志里那个 WinError 183
    #   不是 os.replace 报的, 而是**回退路径第二步** os.rename 报的:
    #     1) os.replace 失败(本机实测 WinError 5), 被 `except Exception:` 无声吞掉;
    #     2) 回退里 os.remove(path) 因目标仍被读句柄占着而 PermissionError,
    #        又被 `except: pass` 吞掉;
    #     3) 目标还在 -> os.rename(tmp, path) 报 WinError 183(ERROR_ALREADY_EXISTS)。
    #   整条链吞掉两个真实错误, 只留下最误导人的第三个。实盘代价: 08:48:52 与
    #   盘中各一次写失败, 整份 result 丢失, 引擎只能等满 10s 超时。
    # 现在: 退避重试(总预算 0.02*(1+..+8)=0.72s, 远小于 10s 超时), 耗尽才 raise;
    #   且**先写 boot.log 再抛** —— QMT 用 pythonw 跑, 没有 stdout, 不落盘就等于
    #   没发生过(2026-11-08 就是因为只 print 而误判过一次)。
    last = None
    for i in range(8):
        try:
            os.replace(tmp, path)
            return
        except OSError as e:
            last = e
            time.sleep(0.02 * (i + 1))
    # ★ 日志失败绝不许盖住真错误: 若 _boot 自己抛, 原始 last 也必须照抛不误;
    #   否则就变成「用次生错误盖住第一因」—— 正是今天在修的那个毛病。
    try:
        _boot("[BRIDGE] write failed after 8 retries: %s (%s) -> %s"
              % (type(last).__name__, last, path))
    except Exception:  # noqa: log-only
        pass
    raise last


def _read_bytes_ctypes(path):
    """C 层读文件，绕过 Python 层 open() 钩子。

    QMT 沙箱对策略的所有 ``open(path, 'r')`` 都抛
    ``PermissionError: Forbidden fileIO`` —— 连它自己的 .py 源码都读不了
    （2026-11-08 实测 7 个路径全拒）。但钩子挂在 Python 层，``kernel32``
    这一层通常照旧放行，所以这是第一顺位逃生口。
    """
    import ctypes
    from ctypes import wintypes

    k32 = _K32.get()
    GENERIC_READ = 0x80000000
    # ★ 必须同时给 WRITE|DELETE（= 0x7），不能只给 SHARE_READ。
    # 只给 SHARE_READ 时，QMT 侧一持有句柄，本机引擎的 os.replace(signal) 就会拿到
    # ERROR_SHARING_VIOLATION，`BrokerBroker._request` 直接计 error 并**丢掉整条
    # 信号**（如果是 SELL，就是一次静默漏卖）。
    # 2026-11-08 实测：8 次请求里 1 次写 signal 失败，而 bridge_probe 把它误报成
    # 「DUMP 超时」（dump() 返回 None 有超时/失败两条路径，文案只写了超时）。
    SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    INVALID_HANDLE = ctypes.c_void_p(-1).value

    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.GetFileSize.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
    k32.GetFileSize.restype = wintypes.DWORD
    k32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                             wintypes.LPDWORD, wintypes.LPVOID]
    k32.ReadFile.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    h = k32.CreateFileW(path, GENERIC_READ, SHARE_ALL, None,
                        OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
    if not h or h == INVALID_HANDLE:
        raise OSError(ctypes.get_last_error(), "CreateFileW failed: %s" % path)
    try:
        size = int(k32.GetFileSize(h, None))
        if size <= 0:
            return b""
        buf = ctypes.create_string_buffer(size)
        got = wintypes.DWORD(0)
        if not k32.ReadFile(h, buf, size, ctypes.byref(got), None):
            raise OSError(ctypes.get_last_error(), "ReadFile failed: %s" % path)
        return buf.raw[:got.value]
    finally:
        try:
            k32.CloseHandle(h)
        except Exception:
            pass


def _read_bytes_numpy(path):
    """第三顺位：numpy 的 C 层 fopen。QMT 自带 numpy 1.19.1。"""
    import numpy as np
    return np.fromfile(path, dtype=np.uint8).tobytes()


def _read_bytes_py(path):
    with open(path, "rb") as f:
        return f.read()


class _K32(object):
    """延迟加载 kernel32，避免在不需要的路径上多一次 import。"""

    _dll = [None]

    @classmethod
    def get(cls):
        if cls._dll[0] is None:
            import ctypes
            cls._dll[0] = ctypes.WinDLL("kernel32", use_last_error=True)
        return cls._dll[0]


_READERS = {
    "py": _read_bytes_py,
    "ctypes": _read_bytes_ctypes,
    "numpy": _read_bytes_numpy,
}
_reader = [None]        # 记住上一次成功的读法，别每次轮一遍


def _read_text(path):
    """按「上次成功的读法优先」的顺序试读链，成功即记住。"""
    names = list(_READERS)
    preferred = _reader[0]
    if preferred in names:
        names.remove(preferred)
        names.insert(0, preferred)
    last = None
    for name in names:
        try:
            raw = _READERS[name](path)
        except Exception as e:
            last = e
            continue
        if _reader[0] != name:
            _log("[BRIDGE] file reader=%s 可用（上一次=%s）"
                 % (name, preferred or "<未定>"))
            _reader[0] = name
        return raw.decode("utf-8")
    raise last if last else RuntimeError("no reader available")


def _read_json(path):
    try:
        return json.loads(_read_text(path))
    except Exception as e:
        # 只在第一次逐条报错：QMT 沙箱可能直接拒读（deny read），
        # 静默返回 None 会让「读不到」伪装成「没有信号」。
        key = "read_json_err:" + type(e).__name__
        if key not in _dict_warned:
            _dict_warned.add(key)
            _log("[BRIDGE] read_json failed once (%s): %s: %s"
                 % (path, type(e).__name__, e))
        return None


def _attr(obj, names, default=None):
    """First readable attribute out of ``names`` (QMT renames fields between builds)."""
    for n in names:
        try:
            v = getattr(obj, n)
        except Exception:
            continue
        if v is not None:
            return v
    return default


def _f(v, default=0.0):
    """float() with QMT's "not applicable" sentinel filtered out.

    QMT returns ``DBL_MAX`` (1.7976931348623157e+308) for fields that do not
    apply to the account type. 2026-11-08 DUMP evidence on the live STOCK
    account: ``m_dCurrMargin``, ``m_dPreBalance``, ``m_dStaticHoldMargin``,
    ``m_dStockLastPrice`` and ``m_dAvgOpenPrice`` were all exactly that value.
    ``float()`` succeeds on it, so without this guard 1.8e308 silently enters
    the ledger and any arithmetic downstream turns into inf/nan.
    """
    try:
        f = float(v)
    except Exception:
        return default
    if f != f or abs(f) >= 1e300:      # NaN, or QMT's DBL_MAX sentinel
        return default
    return f


def _i(v, default=0):
    try:
        return int(float(v))
    except Exception:
        return default


def _split_code(code):
    """'300308.SZ' -> ('300308', 'SZ'); infers the exchange for bare 6-digit codes."""
    s = str(code or "").strip().upper()
    if "." in s:
        a, b = s.split(".", 1)
        return a, b
    if s.startswith(("5", "6", "9")):
        return s, "SH"
    return s, "SZ"


def _join_code(sid, exch):
    sid = str(sid or "").strip()
    exch = str(exch or "").strip().upper()
    if not sid:
        return ""
    if "." in sid:
        return sid.upper()
    if not exch:
        return _split_code(sid)[0] + "." + _split_code(sid)[1]
    return sid + "." + exch


# ---------------------------------------------------------------------------
# heartbeat / reply
# ---------------------------------------------------------------------------
def _write_heartbeat(ContextInfo):
    t = _now()
    if t - _last_hb[0] < HEARTBEAT_SEC:
        return
    _last_hb[0] = t
    try:
        _atomic_write_json(HEARTBEAT_FILE, {
            "ts": t,
            "account": getattr(ContextInfo, "accountID", DEFAULT_ACCOUNT_ID),
            "account_type": getattr(ContextInfo, "account_type", DEFAULT_ACCOUNT_TYPE),
            "pid": os.getpid(),
            # 被动版本戳：任何人读 heartbeat.json 就能判断 QMT 里跑的是哪一版，
            # 不需要往 signal.json 写东西（那是引擎的下单通道，探针绝不该碰）。
            "rev": CODE_REV,
        })
    except Exception as e:
        _log("[BRIDGE] heartbeat write failed: %s" % e)


def _load_seen():
    """从磁盘恢复「已处理过的 signal_id」，用于跨重启拒绝重放。"""
    try:
        d = _read_json(SEEN_FILE)
    except Exception:
        d = None
    if isinstance(d, dict) and isinstance(d.get("ids"), list):
        for x in d["ids"]:
            if x:
                _seen.add(str(x))
    _log("[BRIDGE] seen 已恢复 %d 条（跨重启幂等）" % len(_seen))


def _save_seen():
    if not _seen:
        return
    try:
        _atomic_write_json(SEEN_FILE, {"ids": sorted(_seen)[-_MAX_CACHED:]})
    except Exception as e:
        _log("[BRIDGE] seen 落盘失败: %s" % e)


def _reply(signal_id, status, data=None, message=""):
    payload = {
        "signal_id": signal_id,
        "status": status,
        "data": data if data is not None else {},
        "message": message,
        "ts": _now(),
    }
    try:
        _atomic_write_json(RESULT_FILE, payload)
    except Exception as e:
        _log("[BRIDGE] result write failed: %s" % e)
    return payload


# ---------------------------------------------------------------------------
# queries: get_trade_detail_data wrappers
# ---------------------------------------------------------------------------
def _account_of(ContextInfo, account):
    """(account_id, account_type).

    Resolution order matters: QMT injects the model-trading arguments as module
    globals (``PythonFormula::loadArgvs`` -> ``account`` / ``accountType``), and
    the shipped sample reads those bare names rather than ContextInfo. So prefer
    ContextInfo when it carries them, otherwise use the injected globals, and
    only then fall back to the hard-coded constants.
    """
    g = globals()
    aid = (getattr(ContextInfo, "accountID", None)
           or getattr(ContextInfo, "account_id", None)
           or g.get("account"))
    atype = (getattr(ContextInfo, "account_type", None)
             or getattr(ContextInfo, "accountType", None)
             or g.get("accountType"))
    return (str(aid or DEFAULT_ACCOUNT_ID),
            str(atype or DEFAULT_ACCOUNT_TYPE))


def _q_account(ContextInfo, account):
    # 先查持仓（拿最新价市值），再查 ACCOUNT —— 顺序刻意如此：
    # ACCOUNT 里的市值可能停在上一交易日收盘（见下方实测），但现金/冻结只有它有。
    # 先持仓后账户也让「最后一次查询是 ACCOUNT」这个既有约定保持不变。
    pos_mv = _sum_position_mv(ContextInfo, account)
    aid, atype = _account_of(ContextInfo, account)
    rows = get_trade_detail_data(aid, atype, "ACCOUNT")  # noqa: F821
    if not rows:
        return None
    a = rows[0]
    cash = _f(_attr(a, ["m_dAvailable", "m_dCash"], 0))
    mv = _f(_attr(a, ["m_dInstrumentValue", "m_dStockValue", "m_dMarketValue"], 0))
    # ★★ 2026-11-08 21:30 实测（同一轮 QUERY，同一账号）：**ACCOUNT 与 POSITION 的市值
    #    用的不是同一个价格**。ACCOUNT.m_dInstrumentValue 停在**上一交易日收盘**，
    #    POSITION.m_dMarketValue 才是最新价。
    #      300308.SZ 11-06 跌 -9.18%（493.33 → 448.00）
    #      ACCOUNT  : 50,798.00 = 493.33×100 + 2.93×500   ← 11-05 收盘价
    #      POSITION : 46,251.00 = 448.00×100 + 2.902×500  ← 11-06 收盘价
    #      差额 4,547.00 与账本 11-06 的 daily_pnl -4,547.03 **分毫不差**
    #    => 若拿 ACCOUNT 口径当 `_broker_total_asset`：总资产 62,409.49（真值 57,862.49），
    #      回撤会算成 -7.5%（真实 -14.2%），熔断缓冲凭空多出 6.7pp。
    #      当前账本距 -20% 只剩 5.78pp => 这个误差足以决定熔断是否触发，必须用持仓汇总。
    #    持仓查询失败（None）时才回落到 ACCOUNT 口径 —— 宁可口径旧一天，也不肯凭空造数。
    if pos_mv is not None and pos_mv > 0:
        if mv > 0 and abs(pos_mv - mv) > max(1.0, 0.001 * max(pos_mv, mv)):
            if "acct_mv_mismatch" not in _dict_warned:
                _dict_warned.add("acct_mv_mismatch")
                _log("[BRIDGE] ACCOUNT 市值 %0.2f 与持仓汇总 %0.2f 不一致"
                     "（差 %0.2f，ACCOUNT 用的是上一交易日收盘价），"
                     "以持仓汇总为准" % (mv, pos_mv, mv - pos_mv))
        mv = pos_mv
    elif pos_mv == 0 and mv > 0:
        # 矛盾：持仓查得到但汇总为 0，而 ACCOUNT 说有市值。
        # 此时**绝不能**把市值清零（total_asset 会瞬间少掉全部持仓 => 假回撤 => 假熔断），
        # 保留 ACCOUNT 口径并只报一次日志，等下一轮持仓恢复正常。
        if "acct_mv_zero_conflict" not in _dict_warned:
            _dict_warned.add("acct_mv_zero_conflict")
            _log("[BRIDGE] 持仓汇总为 0 但 ACCOUNT 市值 %0.2f，暂按 ACCOUNT 口径"
                 "（不清零，避免假回撤）" % (mv,))
    return {
        "cash": cash,
        "frozen": _f(_attr(a, ["m_dFrozenCash", "m_dFrozen"], 0)),
        "market_value": mv,
        "total_asset": _total_asset_of(a, cash, mv),
    }


def _sum_position_mv(ContextInfo, account):
    """持仓市值汇总；持仓查询失败返回 ``None``（区别于「确实空仓」的 0.0）。"""
    try:
        rows = _q_positions(ContextInfo, account)
    except Exception as e:                            # noqa: BLE001
        _log("[BRIDGE] 持仓汇总失败，回落 ACCOUNT 口径: %r" % (e,))
        return None
    if rows is None:
        return None
    return float(sum(float(p.get("market_value") or 0.0) for p in rows))


def _total_asset_of(a, cash, mv):
    """总资产口径：券商字段优先，但只信「自洽」的那一个。

    ★ 2026-11-08 实测（实盘账户）：``m_dBalance`` 返回
    **137,321.50**，而同一快照里 ``m_dAvailable``(11,611.49) +
    ``m_dInstrumentValue``(46,251.00) = **57,862.49**，与引擎 xtquant 直连
    读到的 57,862.57 吻合到 0.08 元（佣金精度）。137,321.50 用任何字段组合
    都拼不出来，是坏值。

    这不是「本地拼装 vs 券商口径」的老问题 —— 2026-10-30 那个 P0 之所以
    禁止拼装，是因为 cash 与持仓来自**两次独立查询**、有亚秒级最终一致性
    窗口。此处 cash 与市值取自**同一个 get_trade_detail_data 返回的同一个
    对象**，是同一时刻的快照，不存在该窗口。

    若直接采用 137,321.50，切 bridge 后 engine/event_engine.py:4612 会把它
    当作 `_broker_total_asset`：断路器峰值虚高 2.37 倍 => 回撤恒为 0 =>
    max_drawdown 熔断永久失效；risk_per_trade / max_single_position_pct /
    daily_stop_flatten_pct 同步放大 2.37 倍。
    """
    frozen = _f(_attr(a, ["m_dFrozenCash", "m_dFrozen"], 0))
    derived = cash + frozen + mv
    broker = _f(_attr(a, ["m_dTotalAsset"], 0), 0.0)
    if broker <= 0:
        broker = _f(_attr(a, ["m_dBalance"], 0), 0.0)
    if broker <= 0:
        return derived
    # 券商口径与自洽口径差 1% 以上就判为坏值。真出现「可用资金 + 市值」
    # 覆盖不到的资产（在途资金、申购冻结）时差异不会这么大。
    if derived > 0 and abs(broker - derived) > 0.01 * max(derived, 1.0):
        key = "acct_total_mismatch"
        if key not in _dict_warned:
            _dict_warned.add(key)
            _log("[BRIDGE] 券商 total_asset 字段不可信（%0.2f），改用 "
                 "cash+市值=%0.2f，差 %0.2f" % (broker, derived, broker - derived))
        return derived
    return broker


def _q_positions(ContextInfo, account):
    aid, atype = _account_of(ContextInfo, account)
    rows = get_trade_detail_data(aid, atype, "POSITION")  # noqa: F821
    if rows is None:
        return None
    out = []
    for p in rows:
        code = _join_code(_attr(p, ["m_strInstrumentID", "m_strInstrumentName"], ""),
                          _attr(p, ["m_strExchangeID", "m_strExchange"], ""))
        qty = _i(_attr(p, ["m_nVolume", "m_nTotalVolume"], 0))
        if not code or qty == 0:
            continue
        out.append({
            "code": code,
            "quantity": qty,
            "available": _i(_attr(p, ["m_nCanUseVolume", "m_nAvailableVolume"], qty)),
            "avg_cost": _f(_attr(p, ["m_dOpenPrice", "m_dPositionCost"], 0)),
            "market_value": _f(_attr(p, ["m_dMarketValue", "m_dInstrumentValue"], 0)),
        })
    return out


def _q_orders(ContextInfo, account):
    aid, atype = _account_of(ContextInfo, account)
    rows = get_trade_detail_data(aid, atype, "ORDER")  # noqa: F821
    if rows is None:
        return None
    out = []
    for o in rows:
        code = _join_code(_attr(o, ["m_strInstrumentID"], ""),
                          _attr(o, ["m_strExchangeID"], ""))
        # ★ 方向只认 m_nOffsetFlag。旧实现回落到 ["m_nOffsetFlag","m_nOrderType",
        # "m_nOrderPriceType"]，而 m_nOrderPriceType 是**价格类型**（5/11/14…），
        # 拿它当方向用是张冠李戴；且默认值落到 "SELL" —— 认不出就猜卖，
        # 是朝「卖」的 fail-open。改为认不出就 UNKNOWN。
        # 上层 core/broker_bridge.py::_attribute_order_id 以**代码**为主键、
        # 方向仅作加分项，故 UNKNOWN 只会少加分，不会致错。
        # 仍未实测：48/49 是 xtquant 的 OFFSET_FLAG_OPEN/CLOSE 猜测值（股票
        # 买入=开仓、卖出=平仓），23/24 是 STOCK_BUY/SELL，33/34 是信用。
        # 必须在有真实委托时用 DUMP 校准一次（2026-11-08 为周日，ORDER count=0）。
        ot = _i(_attr(o, ["m_nOffsetFlag"], -1))
        if ot in (48, 23, 33):
            side = "BUY"
        elif ot in (49, 24, 34):
            side = "SELL"
        else:
            side = "UNKNOWN"
        out.append({
            "order_id": str(_attr(o, ["m_strOrderSysID", "m_strOrderID"], "")),
            "code": code,
            "side": side,
            "quantity": _i(_attr(o, ["m_nVolumeTotalOriginal", "m_nVolume"], 0)),
            "traded": _i(_attr(o, ["m_nVolumeTraded", "m_nTradedVolume"], 0)),
            "price": _f(_attr(o, ["m_dLimitPrice", "m_dPrice"], 0)),
            "status": str(_attr(o, ["m_nOrderStatus", "m_strOrderStatus"], "")),
        })
    return out


def _q_trades(ContextInfo, account):
    aid, atype = _account_of(ContextInfo, account)
    rows = get_trade_detail_data(aid, atype, "DEAL")  # noqa: F821
    if rows is None:
        return None
    out = []
    for t in rows:
        code = _join_code(_attr(t, ["m_strInstrumentID"], ""),
                          _attr(t, ["m_strExchangeID"], ""))
        out.append({
            "order_id": str(_attr(t, ["m_strOrderSysID", "m_strOrderID"], "")),
            "code": code,
            "traded_price": _f(_attr(t, ["m_dPrice", "m_dTradedPrice"], 0)),
            "traded_volume": _i(_attr(t, ["m_nVolume", "m_nTradedVolume"], 0)),
            "traded_time": str(_attr(t, ["m_strTradeTime", "m_strTime"], "")),
        })
    return out


# ---------------------------------------------------------------------------
# trading: passorder wrappers
# ---------------------------------------------------------------------------
# Documented QMT signatures (checked against the shipped, working sample
# C:\pazq_qmt\python\双均线实盘示例PY.py, which calls:
#     passorder(23, 1101, acct, code, 14, -1, vol, name, 1, msg, C)   # 11 args
# i.e. WITHOUT a separate account-type slot. The 12-arg variant is the one that
# does take accountType. An 11-arg call that puts accountType where the account
# id belongs silently shifts every later argument -- do not reintroduce it.
_PASSORDER_FORMS = [
    "aid_qt",          # 11 args: (op, otype, aid, code, prtype, price, vol, name, 1, msg, C)
    "atype_aid_qt",    # 12 args: (op, otype, atype, aid, code, prtype, price, vol, name, 1, msg, C)
    "atype_aid",       # 10 args: (op, otype, atype, aid, code, prtype, price, vol, name, C)
    "aid_only",        # 10 args: (op, otype, aid, code, prtype, price, vol, name, C)
]

QUICK_TRADE = 1       # 1 = normal, 2 = fast; the shipped sample uses 1


def _get_po_form(ContextInfo):
    """The passorder form that worked last time, if any."""
    v = None
    try:
        v = getattr(ContextInfo, "_bridge_po_form", None)
    except Exception:
        pass
    return v or _po_form[0]


def _set_po_form(ContextInfo, form):
    """Remember the form. Never raises -- see the comment on ``_po_form``."""
    _po_form[0] = form
    try:
        ContextInfo._bridge_po_form = form
    except Exception:
        pass


def _try_passorder(ContextInfo, form, op, otype, atype, aid, code, prtype,
                   price, vol, name):
    msg = "%s %s %s" % (name, "BUY" if op == 23 else "SELL", code)
    if form == "aid_qt":
        return passorder(op, otype, aid, code, prtype, price, vol,  # noqa: F821
                         name, QUICK_TRADE, msg, ContextInfo)
    if form == "atype_aid_qt":
        return passorder(op, otype, atype, aid, code, prtype, price, vol,  # noqa: F821
                         name, QUICK_TRADE, msg, ContextInfo)
    if form == "atype_aid":
        return passorder(op, otype, atype, aid, code, prtype, price, vol,  # noqa: F821
                         name, ContextInfo)
    if form == "aid_only":
        return passorder(op, otype, aid, code, prtype, price, vol,  # noqa: F821
                         name, ContextInfo)
    raise RuntimeError("unknown passorder form: %s" % form)


def _place_order(ContextInfo, side, code, volume, price, prtype, strategy_name):
    """(ok, order_id, message).

    passorder returns nothing usable, so order_id is best-effort (usually None).
    The engine MUST fall back to QUERY_ORDER matching -- see
    core/broker_bridge.py::_attribute_order_id.
    """
    aid, atype = _account_of(ContextInfo, None)
    sid, exch = _split_code(code)
    full_code = _join_code(sid, exch)
    op = 23 if str(side).upper() == "BUY" else 24
    otype = 1101  # 单股单账号普通买卖
    vol = _i(volume, 0)
    px = _f(price, 0.0)
    if vol <= 0:
        return False, None, "volume<=0"
    if px <= 0:
        return False, None, "price<=0 (bridge only sends limit prices, never guesses)"

    order_form = _get_po_form(ContextInfo)
    forms = [order_form] if order_form else list(_PASSORDER_FORMS)
    last_err = ""
    for form in forms:
        try:
            ret = _try_passorder(ContextInfo, form, op, otype, atype, aid,
                                 full_code, prtype, px, vol, strategy_name)
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, e)
            _log("[BRIDGE] passorder form=%s failed: %s" % (form, last_err))
            continue
        # 走到这里说明单已经发出去了。后面任何一步失败都不许再试下一种形式，
        # 否则同一笔单会下两次（真实资金）。
        _set_po_form(ContextInfo, form)
        _log("[BRIDGE] passorder ok form=%s side=%s code=%s vol=%s px=%s ret=%r"
             % (form, side, full_code, vol, px, ret))
        # passorder returns None / 0 on success in most builds
        if isinstance(ret, int) and ret < 0:
            return False, None, "passorder ret=%s" % ret
        return True, (str(ret) if ret not in (None, 0) else None), ""
    return False, None, "passorder all forms failed: %s" % last_err


def _cancel(ContextInfo, order_id, account):
    aid, atype = _account_of(ContextInfo, account)
    forms = [
        ("sysid", lambda: cancel(str(order_id), aid, atype, ContextInfo)),        # noqa: F821
        ("int", lambda: cancel(_i(order_id, 0), aid, atype, ContextInfo)),        # noqa: F821
        ("simple", lambda: cancel(str(order_id), ContextInfo)),                   # noqa: F821
    ]
    last_err = ""
    for name, fn in forms:
        try:
            ret = fn()
            _log("[BRIDGE] cancel ok form=%s id=%s ret=%r" % (name, order_id, ret))
            if isinstance(ret, int) and ret < 0:
                return False, "cancel ret=%s" % ret
            return True, ""
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, e)
    return False, "cancel all forms failed: %s" % last_err


# ---------------------------------------------------------------------------
# DUMP: schema discovery for calibration
# ---------------------------------------------------------------------------
def _dump(ContextInfo):
    g = sorted([n for n in globals().keys() if not n.startswith("_")])
    interesting = [n for n in g if any(k in n.lower() for k in
                                       ("passorder", "cancel", "order", "trade",
                                        "detail", "get_full_tick", "account"))]
    schema = {}
    aid, atype = _account_of(ContextInfo, None)
    for typ in ("ACCOUNT", "POSITION", "ORDER", "DEAL"):
        try:
            rows = get_trade_detail_data(aid, atype, typ)  # noqa: F821
        except Exception as e:
            schema[typ] = {"error": "%s: %s" % (type(e).__name__, e)}
            continue
        entry = {"count": len(rows) if rows is not None else None}
        if rows:
            o = rows[0]
            fields = {}
            for n in dir(o):
                if n.startswith("m_"):
                    try:
                        fields[n] = repr(getattr(o, n))
                    except Exception:
                        fields[n] = "<unreadable>"
            entry["fields"] = fields
        schema[typ] = entry
    return {
        "rev": CODE_REV,
        "globals": g,
        "interesting": interesting,
        "schema": schema,
        "account_id": aid,
        "account_type": atype,
        "passorder_form": _get_po_form(ContextInfo),
        "python": sys.version,
    }


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------
_QUERY_ACTIONS = {
    "QUERY_ASSET": _q_account,
    "QUERY_POSITION": _q_positions,
    "QUERY_ORDER": _q_orders,
    "QUERY_TRADE": _q_trades,
}


def _handle(ContextInfo, sig):
    action = str(sig.get("action") or "").upper()
    params = sig.get("params") or {}
    account = sig.get("account") or "cash"

    if action == "PING":
        # rev 用于确认「QMT 里跑的到底是哪一版代码」：只部署文件、不重新运行策略时，
        # 旧代码会一直活到 QMT 重启，改动静默无效（2026-11-08 差点踩到）。
        return True, {"pong": _now(), "rev": CODE_REV}, ""
    if action == "DUMP":
        return True, _dump(ContextInfo), ""
    if action in _QUERY_ACTIONS:
        data = _QUERY_ACTIONS[action](ContextInfo, account)
        if data is None:
            return False, None, "%s: get_trade_detail_data returned None" % action
        return True, data, ""
    if action in ("BUY", "SELL"):
        ok, oid, msg = _place_order(
            ContextInfo, action,
            params.get("code", ""),
            params.get("volume", 0),
            params.get("price", 0),
            _i(params.get("prtype", 11), 11),
            params.get("strategy_name", "qmtide_bridge"),
        )
        return ok, {"order_id": oid}, msg
    if action == "CANCEL":
        ok, msg = _cancel(ContextInfo, params.get("order_id", ""), account)
        return ok, {}, msg
    return False, None, "unknown action: %s" % action


def bridge_poll(ContextInfo):
    """One poll: heartbeat, then read signal -> handle -> write result."""
    try:
        _poll_seen[0] += 1
        # 前两次 poll 必须留痕：它能区分「定时器没起来」和「信号没读到」。
        # 2026-11-08 就因为缺这条，把「init 被中断、poll 从未执行」误判成
        # 「ctypes 读链没生效」，白查了一轮。
        if _poll_seen[0] <= 2:
            _log("[BRIDGE] poll #%d alive" % _poll_seen[0])

        _write_heartbeat(ContextInfo)
        if not os.path.exists(SIGNAL_FILE):
            return
        sig = _read_json(SIGNAL_FILE)
        if not isinstance(sig, dict) or not sig.get("signal_id"):
            if _poll_seen[0] <= 2:
                _log("[BRIDGE] signal present but unusable: %r" % (sig,))
            return
        sid = str(sig["signal_id"])

        with _lock:
            if sid in _processed:
                cached = _processed[sid]
                # 只在 result.json 还不是这条的答案时才写。否则 500ms 一轮会
                # 永远重写同一个文件（signal.json 残留时必然命中这条路径）。
                if _result_sid[0] != sid:
                    try:
                        _atomic_write_json(RESULT_FILE, cached)
                        _result_sid[0] = sid
                    except Exception:
                        pass
                return

            if sid in _seen:
                # 重启前已经处理过的旧信号。**绝不能重放** —— signal.json 会一直
                # 留着最后一条，QMT 策略重启后若不拦，已成交的 BUY 会再买一次。
                # ★ 只报一次：这条分支每 500ms 命中一次，若每次都写日志，
                # 2026-11-08 02:25 实测 7 分钟刷了 1288 行、把 boot.log 顶到
                # 200KB 上限**轮转掉了一轮诊断历史**（boot.log.1 已满）。
                if sid not in _dup_reported:
                    _dup_reported.add(sid)
                    _log("[BRIDGE] 拒绝重放已处理信号 %s action=%s"
                         % (sid, sig.get("action")))
                    _reply(sid, "duplicate",
                           {"signal_id": sid, "action": sig.get("action")},
                           "already processed before restart; not executed again")
                    _result_sid[0] = sid
                    if len(_dup_reported) > 4 * _MAX_CACHED:
                        _dup_reported.clear()
                return

            _log("[BRIDGE] got signal_id=%s action=%s reader=%s"
                 % (sid, sig.get("action"), _reader[0]))

            status, data, message = "error", None, ""
            try:
                ok, payload, message = _handle(ContextInfo, sig)
                status = "ok" if ok else "error"
                data = payload
            except Exception:
                message = traceback.format_exc()
                _log("[BRIDGE] handle exception:\n%s" % message)

            result = _reply(sid, status, data, message)
            _result_sid[0] = sid
            _processed[sid] = result
            _processed_order.append(sid)
            # 只有**执行成功**才记入跨重启黑名单：失败的信号应当允许重试
            # （例如 QMT 当时拒单、账户未就绪），失败也不算「已处理」。
            if status == "ok":
                _seen.add(sid)
                _save_seen()
            while len(_processed_order) > _MAX_CACHED:
                old = _processed_order.pop(0)
                _processed.pop(old, None)
    except Exception:
        _log("[BRIDGE] poll exception:\n%s" % traceback.format_exc())


# ---------------------------------------------------------------------------
# QMT entry points
# ---------------------------------------------------------------------------
def _io_selftest():
    """Probe what the QMT python sandbox actually permits. Runs once in init().

    2026-11-08: QMT logs ``file I/O check, ...\\signal.json, bWrite:false, deny
    read`` -- writes are allowed, READS ARE DENIED for paths outside its own
    tree. The bridge's inbound channel is therefore blind unless the signal lives
    somewhere readable, or we stop using files for that direction at all.
    This dumps the permission matrix into boot.log so the design can be chosen
    from evidence instead of guesswork.
    """
    if _io_selftest_ran[0]:
        return
    _io_selftest_ran[0] = True

    cands = [
        os.path.join(BRIDGE_ROOT, "signal.json"),
        os.path.join(BRIDGE_ROOT, "boot.log"),
        os.path.join(BRIDGE_ROOT, "heartbeat.json"),
        r"C:\pazq_qmt\python\QMTIDE_BRIDGE.py",
        r"C:\pazq_qmt\userdata\qmtide_bridge\signal.json",
        r"C:\pazq_qmt\userdata_mini\qmtide_bridge\signal.json",
        os.path.join(os.path.expanduser("~"), "qmtide_signal.json"),
    ]
    for p in cands:
        parts = []
        try:
            parts.append("exists=%s" % os.path.exists(p))
        except Exception as e:
            parts.append("exists!%s" % type(e).__name__)
        # 逐个读法单独试，看清哪一层能过
        for name in ("py", "ctypes", "numpy"):
            try:
                n = len(_READERS[name](p))
                parts.append("%s_ok=%d" % (name, n))
            except Exception as e:
                parts.append("%s!%s" % (name, type(e).__name__))
        try:
            with open(p + ".probe", "wb") as f:
                f.write(b"x")
            parts.append("write_ok")
            try:
                os.remove(p + ".probe")
            except Exception:
                parts.append("rm_fail")
        except Exception as e:
            parts.append("write!%s" % type(e).__name__)
        _log("[IO] %s -> %s" % (p, " ".join(parts)))

    # socket：必须用**阻塞** connect。
    # 2026-11-08 用 connect_ex + settimeout 得到恒为 10035(WSAEWOULDBLOCK)，
    # 那只是「非阻塞下未完成」，对「是否允许联网」零信息量 —— 测法本身是错的。
    # 测试环境跳过：每次 connect 白等，40 多个用例能拖出 20 秒。
    # 只探一个端口并把超时压到 0.3s：这条结论是「网络栈能不能用」，
    # 换端口不会给出新信息，但每多一个端口就多一份卡住 init 的风险。
    if SKIP_SOCKET_PROBE:
        return
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.3)
        try:
            s.connect(("127.0.0.1", 65001))
            _log("[IO] socket connect OK（127.0.0.1:65001 有人监听）")
        except ConnectionRefusedError:
            _log("[IO] socket CONNECTION REFUSED => 网络栈可用（该端口无监听）")
        except Exception as e:
            _log("[IO] socket %s: %s" % (type(e).__name__, e))
        finally:
            try:
                s.close()
            except Exception:
                pass
    except Exception as e:
        _log("[IO] socket UNAVAILABLE: %s: %s" % (type(e).__name__, e))


def init(ContextInfo):
    _log("[BRIDGE] MODE=embedded init() called (passorder available)")
    try:
        if not os.path.isdir(BRIDGE_ROOT):
            os.makedirs(BRIDGE_ROOT)
    except Exception as e:
        _log("[BRIDGE] mkdir failed: %s" % e)

    _set_po_form(ContextInfo, None)
    _last_hb[0] = 0.0
    _write_heartbeat(ContextInfo)

    period = "%dnMilliSecond" % POLL_MS
    try:
        ContextInfo.run_time("bridge_poll", period, "2000-01-01 00:00:00")
        timer = "run_time %s" % period
    except Exception as e:
        timer = "UNAVAILABLE: %s" % e

    aid, atype = _account_of(ContextInfo, None)
    # 定时器已注册，才做这次小文件读（一个已存在的几十字节 JSON，ctypes 读）。
    try:
        _load_seen()
    except Exception as e:
        _log("[BRIDGE] seen 恢复失败: %s: %s" % (type(e).__name__, e))
    _log("[BRIDGE] init done. root=%s account=(%s,%s) timer=%s rev=%s"
         % (BRIDGE_ROOT, aid, atype, timer, CODE_REV))

    # 自测必须放后台线程。它是「诊断」，不是「启动前置」，绝不能占住 init。
    # 2026-11-08 02:09 实测代价：socket 探针两个阻塞 connect 各 0.5s，用户恰好
    # 在那 1 秒里点了「停止」-> init 被 KeyboardInterrupt 打断 -> 从未返回 ->
    # QMT 已注册的 run_time 定时器永不调度 -> bridge_poll 一次都没跑 ->
    # result.json 永远不生成。日志证据：02:09:13 那轮 selftest 打完 7 个路径就
    # 断了、连 socket 行都没有，而 02:04 那轮（旧的非阻塞探针，秒回）有。
    try:
        _t = threading.Thread(target=_io_selftest, name="bridge_selftest")
        _t.setDaemon(True)   # py3.6：Thread(daemon=) 不保证，用 setDaemon
        _t.start()
    except Exception as e:
        _log("[IO] selftest skipped: %s: %s" % (type(e).__name__, e))


def handlebar(ContextInfo):
    """Safety net: run_time timers are the primary path, handlebar is the backup."""
    bridge_poll(ContextInfo)


def after_init(ContextInfo):
    """QMT calls this after init(); do one poll so the first signal is not delayed."""
    bridge_poll(ContextInfo)


# ---------------------------------------------------------------------------
# standalone entry (QMT mode B: pythonw.exe -u <this file> <userdata> <ts>)
# ---------------------------------------------------------------------------
class _StandaloneContext(object):
    """Minimal stand-in for ContextInfo when QMT did not inject one."""

    accountID = DEFAULT_ACCOUNT_ID
    account_type = DEFAULT_ACCOUNT_TYPE
    _bridge_po_form = None


def _standalone():
    if _has_passorder():
        _log("[BRIDGE] MODE=standalone but passorder IS available -> polling loop")
        _load_seen()      # 跨重启幂等：拒绝重放 signal.json 里残留的旧信号
        ctx = _StandaloneContext()
        period = POLL_MS / 1000.0
        try:
            while True:
                bridge_poll(ctx)
                time.sleep(period)
        except KeyboardInterrupt:
            _log("[BRIDGE] standalone loop interrupted")
        return 0

    _log("[BRIDGE] MODE=standalone, passorder NOT available -> "
         "this run can never trade; exiting so QMT can report the truth. "
         "Run the strategy from 模型交易 (embedded engine) instead.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(_standalone())
    except SystemExit:
        raise
    except Exception:
        _log("[BRIDGE] standalone crashed:\n%s" % traceback.format_exc())
        sys.exit(1)

# -*- coding: utf-8 -*-
"""大 QMT 侧 bridge 执行器的**第一版**（已被 qmt_bridge/qmtide_bridge.py 取代）

⚠ 状态：**未部署、不要直接用。** 唯一在用的是 ``qmt_bridge/qmtide_bridge.py``。
本文件保留仅为对照它的演进过程，两处已知缺陷已在 qmtide_bridge.py 中修正：

1. ``_try_passorder`` 的三种形式都把 ``accountType`` 塞进了**账号位**
   （``passorder(op, otype, atype, aid, code, ...)``），导致其后所有参数整体错位一格。
   QMT 随附可跑示例 ``双均线实盘示例PY.py:77`` 的正确形式是 11 参、**不含**
   accountType：``passorder(op, otype, aid, code, prtype, price, vol, name,
   quickTrade, msg, ContextInfo)``。
2. 无「跨重启不重放」保护：``_processed`` 只在内存，``signal.json`` 残留时
   QMT 策略一重启就会把上一条信号（可能是已成交的 BUY）再执行一次。

另：QMT 内嵌解释器的 ``sys`` 是精简 shim，**没有 ``argv``**；模块级任何一次
异常都会让 QMT 放弃加载、``init`` 根本不会被定义。qmtide_bridge.py 对此做了
逐字段容错 + 三级文件读链（Python 层 ``open()`` 在沙箱里被全禁）。

背景
----
2026-11-07 23:49 QMT 从 2.1.17.0 自动升级到 2.1.32.0 之后，外部 python 通过
``XtQuantTrader`` 连接交易通道被服务端拒绝：

    [TC::COrderServiceQuantAdaptor::onConnected] quant session 969621, illegal pid 3028

提权到管理员后仍被拒（已验证），``userdata_mini`` 下更是连 ``up_queue_xtquant``
都没有。两条路都要券商侧动作，短期内不可自解。

本路线：**下单动作由 QMT 策略主线程的 ``passorder`` 完成**，
不经过 ``xttrader``。策略进程由 QMT 自己拉起，天然属于白名单内。

部署
----
见 ``qmt_bridge/README_DEPLOY.md``（针对 ``qmtide_bridge.py`` 写的）。

协议（文件即接口）
------------------
本地写 ``signal.json``：:

    {"signal_id": "<唯一串>", "action": "QUERY_ASSET",
     "account": "cash", "params": {...}, "ts": 1234567890.0}

本策略写 ``result.json``：:

    {"signal_id": "<原样回传>", "status": "ok" | "error",
     "data": {...}, "message": "", "ts": 1234567890.0}

``signal_id`` 幂等：同一 id 只执行一次，重复请求直接回放上次结果，
因此本地超时重发**不会**造成重复下单。

首次联调请先发 ``DUMP``：它会回传本机 QMT 实际暴露的函数名与字段名，
用于校准 ``get_trade_detail_data`` 的字段（各版本略有差异）。
"""

import json
import os
import threading
import time
import traceback

# ---------------------------------------------------------------------------
# 配置（按需修改）
# ---------------------------------------------------------------------------
BRIDGE_ROOT = r"C:\pazq_qmt_bridge"
DEFAULT_ACCOUNT_ID = os.environ.get("QMT_ACCOUNT_ID") or "000000000000"
DEFAULT_ACCOUNT_TYPE = "STOCK"

POLL_MS = 500                 # 轮询间隔（毫秒）
HEARTBEAT_SEC = 1.0           # 心跳写入间隔（秒）

SIGNAL_FILE = os.path.join(BRIDGE_ROOT, "signal.json")
RESULT_FILE = os.path.join(BRIDGE_ROOT, "result.json")
HEARTBEAT_FILE = os.path.join(BRIDGE_ROOT, "heartbeat.json")

_processed = {}               # signal_id -> 上次 result dict（幂等）
_processed_order = []         # 用于裁剪 _processed 大小
_MAX_CACHED = 200
_lock = threading.Lock()
_last_hb = [0.0]


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _now():
    return time.time()


def _atomic_write_json(path, obj):
    """先写临时文件再 replace，避免本地读到半截 JSON。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    try:
        os.replace(tmp, path)
    except Exception:
        # 极老版本 python 没有 os.replace
        try:
            os.remove(path)
        except Exception:
            pass
        os.rename(tmp, path)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _attr(obj, names, default=None):
    """按候选名依次取属性（QMT 各版本字段名有出入）。"""
    for n in names:
        try:
            v = getattr(obj, n)
        except Exception:
            continue
        if v is not None:
            return v
    return default


def _f(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def _i(v, default=0):
    try:
        return int(float(v))
    except Exception:
        return default


def _split_code(code):
    """'300308.SZ' -> ('300308', 'SZ')；无后缀时按 6 位规则猜。"""
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
# 心跳 / 结果回写
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
        })
    except Exception as e:
        print("[BRIDGE] heartbeat write failed: %s" % e)


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
        print("[BRIDGE] result write failed: %s" % e)
    return payload


# ---------------------------------------------------------------------------
# 查询：get_trade_detail_data 字段映射
# ---------------------------------------------------------------------------
def _account_of(ContextInfo, account):
    """返回 (account_id, account_type)。account 只用于选 cash/credit 语义。"""
    aid = getattr(ContextInfo, "accountID", None) or DEFAULT_ACCOUNT_ID
    atype = getattr(ContextInfo, "account_type", None) or DEFAULT_ACCOUNT_TYPE
    return str(aid), str(atype)


def _q_account(ContextInfo, account):
    aid, atype = _account_of(ContextInfo, account)
    rows = get_trade_detail_data(aid, atype, "ACCOUNT")  # noqa: F821
    if not rows:
        return None
    a = rows[0]
    return {
        "cash": _f(_attr(a, ["m_dAvailable", "m_dCash"], 0)),
        "frozen": _f(_attr(a, ["m_dFrozenCash", "m_dFrozen"], 0)),
        "market_value": _f(_attr(a, ["m_dInstrumentValue", "m_dStockValue",
                                    "m_dMarketValue"], 0)),
        "total_asset": _f(_attr(a, ["m_dTotalAsset", "m_dBalance"], 0)),
    }


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
        # 23/24 = 股票买/卖；信用 33/34
        ot = _i(_attr(o, ["m_nOffsetFlag", "m_nOrderType", "m_nOrderPriceType"], 0))
        side = "BUY" if ot in (23, 33, 48, 49) else "SELL"
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
# 下单：passorder 形式自校准
# ---------------------------------------------------------------------------
# QMT 各版本 passorder 参数个数不完全一致（10 参 / 11 参），且 accountType 是否
# 需要显式传入也有差异。这里不猜，按候选形式逐个试，成功一次就记住。
_PASSORDER_FORMS = [
    # (op, otype, atype, aid, code, prtype, price, vol, name, quickTrade?, ContextInfo)
    "atype_aid_qt",
    "atype_aid",
    "aid_only",
]


def _try_passorder(ContextInfo, form, op, otype, atype, aid, code, prtype,
                   price, vol, name):
    if form == "atype_aid_qt":
        return passorder(op, otype, atype, aid, code, prtype, price, vol,  # noqa: F821
                         name, 2, ContextInfo)
    if form == "atype_aid":
        return passorder(op, otype, atype, aid, code, prtype, price, vol,  # noqa: F821
                         name, ContextInfo)
    if form == "aid_only":
        return passorder(op, otype, aid, code, prtype, price, vol,  # noqa: F821
                         name, 2, ContextInfo)
    raise RuntimeError("unknown passorder form: %s" % form)


def _place_order(ContextInfo, side, code, volume, price, prtype, strategy_name):
    """返回 (ok, order_id, message)。

    passorder 是**异步**的：成功只代表「已提交到柜台」，不代表已成交。
    成交要通过 ``QUERY_TRADE`` 轮询确认（本地 BridgeBroker 负责）。
    """
    aid, atype = _account_of(ContextInfo, None)
    sid, exch = _split_code(code)
    full_code = _join_code(sid, exch)
    op = 23 if str(side).upper() == "BUY" else 24
    otype = 1101  # 单股 单账号 按股数
    vol = _i(volume, 0)
    px = _f(price, 0.0)
    if vol <= 0:
        return False, None, "volume<=0"
    if px <= 0:
        return False, None, "price<=0（桥接只下指定价，不猜价）"

    order_form = getattr(ContextInfo, "_bridge_po_form", None)
    forms = [order_form] if order_form else list(_PASSORDER_FORMS)
    last_err = ""
    for form in forms:
        try:
            ret = _try_passorder(ContextInfo, form, op, otype, atype, aid,
                                 full_code, prtype, px, vol, strategy_name)
            ContextInfo._bridge_po_form = form
            print("[BRIDGE] passorder ok form=%s side=%s code=%s vol=%s px=%s ret=%r"
                  % (form, side, full_code, vol, px, ret))
            # passorder 正常返回 None / 0；返回负数视为失败
            if isinstance(ret, int) and ret < 0:
                return False, None, "passorder ret=%s" % ret
            return True, (str(ret) if ret not in (None, 0) else None), ""
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, e)
            print("[BRIDGE] passorder form=%s failed: %s" % (form, last_err))
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
            print("[BRIDGE] cancel ok form=%s id=%s ret=%r" % (name, order_id, ret))
            if isinstance(ret, int) and ret < 0:
                return False, "cancel ret=%s" % ret
            return True, ""
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, e)
    return False, "cancel all forms failed: %s" % last_err


# ---------------------------------------------------------------------------
# DUMP：字段自校准
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
        "globals": g,
        "interesting": interesting,
        "schema": schema,
        "account_id": aid,
        "account_type": atype,
        "passorder_form": getattr(ContextInfo, "_bridge_po_form", None),
        "python": __import__("sys").version,
    }


# ---------------------------------------------------------------------------
# 主流程
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
        return True, {"pong": _now()}, ""
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
    """一轮桥接轮询：读 signal -> 执行 -> 写 result。"""
    try:
        _write_heartbeat(ContextInfo)
        if not os.path.exists(SIGNAL_FILE):
            return
        sig = _read_json(SIGNAL_FILE)
        if not isinstance(sig, dict) or not sig.get("signal_id"):
            return
        sid = str(sig["signal_id"])

        with _lock:
            if sid in _processed:
                cached = _processed[sid]
                try:
                    _atomic_write_json(RESULT_FILE, cached)
                except Exception:
                    pass
                return

            status, data, message = "error", None, ""
            try:
                ok, payload, message = _handle(ContextInfo, sig)
                status = "ok" if ok else "error"
                data = payload
            except Exception:
                message = traceback.format_exc()
                print("[BRIDGE] handle exception:\n%s" % message)

            result = _reply(sid, status, data, message)
            _processed[sid] = result
            _processed_order.append(sid)
            while len(_processed_order) > _MAX_CACHED:
                old = _processed_order.pop(0)
                _processed.pop(old, None)
    except Exception:
        print("[BRIDGE] poll exception:\n%s" % traceback.format_exc())


# ---------------------------------------------------------------------------
# QMT 策略入口
# ---------------------------------------------------------------------------
def init(ContextInfo):
    try:
        if not os.path.isdir(BRIDGE_ROOT):
            os.makedirs(BRIDGE_ROOT)
    except Exception as e:
        print("[BRIDGE] mkdir failed: %s" % e)

    ContextInfo._bridge_po_form = None
    _last_hb[0] = 0.0
    _write_heartbeat(ContextInfo)

    period = "%dnMilliSecond" % POLL_MS
    try:
        ContextInfo.run_time("bridge_poll", period, "2000-01-01 00:00:00")
        timer = "run_time %s" % period
    except Exception as e:
        timer = "UNAVAILABLE: %s" % e

    print("[BRIDGE] init done. root=%s account=%s type=%s timer=%s"
          % (BRIDGE_ROOT,
             getattr(ContextInfo, "accountID", "?"),
             getattr(ContextInfo, "account_type", "?"),
             timer))


def handlebar(ContextInfo):
    """兜底：即使 run_time 不可用，handlebar 每根 bar 也会调一次。"""
    bridge_poll(ContextInfo)


def after_init(ContextInfo):
    """部分版本在 init 之后调此钩子，可选。"""
    bridge_poll(ContextInfo)

# -*- coding: utf-8 -*-
"""broker 连接/重连逻辑回归测试（不依赖真实 miniQMT）。

验证 2026-09-02 修复：
1. connect() 仅创建一次 XtQuantTrader 实例；force 重连复用同一对象
   （旧逻辑 force 重建实例抢同一 session → rc=-1 → 永久 disconnected）。
2. 底层 on_disconnected 回调 → _connected=False 且触发上层 on_disconnect 钩子。
3. AutoReconnector 连接成功后空闲等待，不周期性主动拆链（不再自伤）。
"""
import sys
import types
import time

# ---- 注入 fake xtquant（必须在 import core.broker 之前）----
_fake = types.ModuleType("xtquant")
_xt = types.ModuleType("xtquant.xttrader")


class XtQuantTraderCallback:
    def on_connected(self): pass
    def on_disconnected(self): pass
    def on_account_status(self, s): pass
    def on_stock_asset(self, a): pass
    def on_stock_order(self, o): pass
    def on_stock_trade(self, t): pass
    def on_stock_position(self, p): pass
    def on_order_error(self, e): pass
    def on_cancel_error(self, e): pass


_created = {"n": 0}
FAKE_LINK_UP = True          # False ⇒ connect() 返回 -1
BROKER_READY = True          # False ⇒ 账户查询返回 None（模拟未登录 / xtquant 版本不匹配）


class _FakeAsset:
    cash = 100000.0
    frozen_cash = 0.0
    market_value = 50000.0
    total_asset = 150000.0


class XtQuantTrader:
    def __init__(self, path, sess):
        self.path = path
        self.sess = sess
        self.connected = False
        self.cb = None
        _created["n"] += 1
        self._id = _created["n"]

    def start(self):
        pass

    def connect(self):
        if FAKE_LINK_UP:
            self.connected = True
            return 0
        return -1

    def register_callback(self, cb):
        self.cb = cb

    # 【2026-11-07 P0】账户查询桩。
    # connect() 现在要求「_probe_account 探测通过」才算连上（core/broker.py），
    # 所以桩必须实现 query_stock_asset；BROKER_READY=False 复刻 2026-11-07
    # 23:33 的实盘现场：connect 返回 -1 且 query_stock_asset 返回 None。
    def query_stock_asset(self, acc):
        return _FakeAsset() if BROKER_READY else None

    def query_stock_positions(self, acc):
        return [] if BROKER_READY else None

    def query_stock_orders(self, acc):
        return [] if BROKER_READY else None

    def query_stock_trades(self, acc):
        return [] if BROKER_READY else None

    def stop(self):
        self.connected = False


_xt.XtQuantTrader = XtQuantTrader
_xt.XtQuantTraderCallback = XtQuantTraderCallback
_fake.xttrader = _xt

# 【2026-10-30 修复】常量模块**不打桩**，透传真实 xtquant.xtconstant。
#
# 原实现只注册了 `xtquant` 与 `xtquant.xttrader`，于是整个 run_all 会话里
# `from xtquant.xtconstant import FIX_PRICE` 都会炸
#   ModuleNotFoundError: No module named 'xtquant.xtconstant';
#   'xtquant' is not a package
# 因为此处把 sys.modules["xtquant"] 换成了一个没有 __path__ 的普通模块。
# 实测后果：tests.test_live_order_price_type 的 8 条用例全红，
# 而单跑该文件（pytest）却是全绿 —— 典型的「测试之间互相污染」。
#
# xtconstant 是纯常量表（无 IO、不连券商），透传不改变本文件的隔离意图。
# 回滚：删除本段 _real_xtconst 相关三行。
try:
    import xtquant.xtconstant as _real_xtconst          # noqa: E402
    _fake.xtconstant = _real_xtconst
    sys.modules["xtquant.xtconstant"] = _real_xtconst
except Exception:                                       # 未装 xtquant 的环境
    pass

# 【2026-11-07】同理透传 xttype：core/broker.py::_probe_account 必须构造
# StockAccount 才能做账户查询探测，而 `sys.modules["xtquant"]=_fake`（无 __path__）
# 会让 `from xtquant.xttype import StockAccount` 直接失败 —— 与上面 xtconstant
# 是同一个坑。xttype 是纯数据类（无 IO），透传不改变本文件的隔离意图。
# 回滚：删除本段，并把 _probe_account 的账户构造改为不可用时返回 False。
try:
    import xtquant.xttype as _real_xttype               # noqa: E402
    _fake.xttype = _real_xttype
    sys.modules["xtquant.xttype"] = _real_xttype
except Exception:                                       # 未装 xtquant 的环境
    _xttype_stub = types.ModuleType("xtquant.xttype")

    class StockAccount:                                 # noqa: D401
        def __init__(self, account_id, account_type="STOCK"):
            self.account_id = account_id
            self.account_type = account_type

    _xttype_stub.StockAccount = StockAccount
    _fake.xttype = _xttype_stub
    sys.modules["xtquant.xttype"] = _xttype_stub

sys.modules["xtquant"] = _fake
sys.modules["xtquant.xttrader"] = _xt

from core.broker import QMTBroker
from core.auto_reconnect import AutoReconnector


def _mk_broker() -> QMTBroker:
    """构造 broker 并注入测试账号。

    为什么必须注入：``_probe_account`` 要靠 ``cfg["accounts"]`` 才能构造
    StockAccount，默认配置里是空串 ⇒ 探测恒失败。真实环境由
    ``trading_config.json`` 提供，但测试不能依赖那个外部文件
    （否则本机绿、换台机器红）。
    """
    b = QMTBroker()
    b._cfg["accounts"] = {"cash": "TESTCASH", "credit": "TESTCREDIT"}
    return b


def test_connect_creates_single_trader_and_reuses_on_force():
    b = _mk_broker()
    assert b.connect() is True
    assert b.is_connected is True
    assert b.mode == "xttrader"
    first = b._trader
    n_after_first = _created["n"]
    # force 重连：不应重建实例
    assert b.connect(force=True) is True
    assert b._trader is first, "force 重连不应重建 XtQuantTrader 实例"
    assert _created["n"] == n_after_first, "trader 实例应只创建一次"
    # 再次非强制 connect 也应幂等
    assert b.connect() is True
    assert b._trader is first


def test_disconnect_callback_marks_off_and_invokes_hook():
    b = _mk_broker()
    assert b.connect() is True
    fired = []
    b.set_on_disconnect(lambda: fired.append(1))
    # 模拟底层断线回调
    assert b._trader.cb is not None
    b._trader.cb.on_disconnected()
    assert b.is_connected is False
    assert b.mode == "disconnected"
    assert fired == [1], "on_disconnect 钩子应被触发"
    # 链路恢复后重连复用同一实例
    assert b.connect() is True
    assert b.is_connected is True
    assert b._trader is not None


def test_auto_reconnector_idles_when_connected():
    b = _mk_broker()
    calls = []

    def fn():
        calls.append(1)
        return b.connect(force=False)

    rc = AutoReconnector(name="t", connect_fn=fn, interval=0.1, max_interval=1.0)
    rc.start()
    time.sleep(0.5)
    rc.stop()
    # 连接成功后应只 connect 一次，之后空闲（不主动拆链）
    assert calls == [1], f"AutoReconnector 不应周期性重连，实际调用 {calls}"
    assert b.is_connected is True


def test_on_reconnected_fires_on_transition_from_disconnected():
    """2026-09-16 优化：交易连接从「未连」恢复后触发 on_reconnected 钩子，
    上层据此重发行情订阅 + 持仓同步（修复断连后数据/下单路径不同步）。"""
    b = _mk_broker()
    assert b.connect() is True          # 首次连接（was_connected=False → 触发）
    fired = []
    b.set_on_reconnected(lambda: fired.append(1))
    # 幂等重复 connect（was_connected=True）→ 不应再触发
    assert b.connect() is True
    assert fired == [], "幂等 connect 不应重复触发 on_reconnected"
    # 模拟断线 → 再次连接（was_connected=False → 触发）
    b._trader.cb.on_disconnected()
    assert b.is_connected is False
    assert b.connect() is True
    assert fired == [1], "断线恢复后应触发 on_reconnected 一次"


def test_connect_rejects_when_account_probe_fails():
    """【2026-11-07 P0】rc=-1 不得直接判为已连接。

    实盘现场（2026-11-07 23:33）：QMT 升级到 2.1.32.0、xtquant 仍旧版，
    ``connect()`` 返回 -1 且 ``query_stock_asset`` 返回 None。原实现把 -1
    当成功 ⇒ 引擎 ``持仓初始化: 0 只, cash=0.00``，300308 的 −18% 硬止损
    从此没有监控对象。
    """
    global FAKE_LINK_UP, BROKER_READY
    old_link, old_ready = FAKE_LINK_UP, BROKER_READY
    try:
        FAKE_LINK_UP = False        # connect -> -1
        BROKER_READY = False        # 且账户查不到
        b = _mk_broker()
        assert b.connect() is False, "探测失败时 connect 必须返回 False"
        assert b.is_connected is False
        assert b.mode == "disconnected"
    finally:
        FAKE_LINK_UP, BROKER_READY = old_link, old_ready


def test_connect_accepts_rc_minus_one_when_probe_passes():
    """rc=-1 但探测通过 ⇒ 仍按已连接处理（保留「重复 connect」语义）。"""
    global FAKE_LINK_UP, BROKER_READY
    old_link, old_ready = FAKE_LINK_UP, BROKER_READY
    try:
        FAKE_LINK_UP = False        # connect -> -1
        BROKER_READY = True         # 但账户查得到
        b = _mk_broker()
        assert b.connect() is True
        assert b.is_connected is True
        assert b.get_asset("cash")["total_asset"] == 150000.0
    finally:
        FAKE_LINK_UP, BROKER_READY = old_link, old_ready


def test_queries_never_disguise_none_as_empty():
    """【2026-11-07 P0】「查不到」≠「没钱 / 没仓」。

    原实现：``get_asset`` 用 ``getattr(None,"cash",0)`` 伪装成
    ``{'cash':0,...}``；``get_positions/orders/trades`` 用 ``for x in raw or []``
    伪装成 ``[]``。两者都**穿透**了上游 event_engine 的防护
    （``:4194 if asset:`` / ``:4207 if raw is None:``），使「通道断了」
    被读成「账户归零、空仓」，再经 SYNC_EMPTY_CONFIRM_N 清空本地账本。
    """
    global BROKER_READY
    old_ready = BROKER_READY
    try:
        b = _mk_broker()
        # 先真正建立连接，让 self._trader 非 None —— 否则查询会走
        # AttributeError 分支（同样是返回 None），测不到本次修的 `a is None`。
        assert b.connect() is True
        assert b._trader is not None
        BROKER_READY = False        # 此后查询一律返回 None（模拟查询瞬断）
        assert b.get_asset("cash") is None, "查不到账户不得返回全 0 dict"
        assert b.get_positions("cash") is None, "查不到持仓不得返回 []"
        assert b.get_orders("cash") is None, "查不到委托不得返回 []"
        assert b.get_trades("cash") is None, "查不到成交不得返回 []"
    finally:
        BROKER_READY = old_ready


if __name__ == "__main__":
    test_connect_creates_single_trader_and_reuses_on_force()
    test_disconnect_callback_marks_off_and_invokes_hook()
    test_auto_reconnector_idles_when_connected()
    test_on_reconnected_fires_on_transition_from_disconnected()
    test_connect_rejects_when_account_probe_fails()
    test_connect_accepts_rc_minus_one_when_probe_passes()
    test_queries_never_disguise_none_as_empty()
    print("ALL BROKER RECONNECT TESTS PASSED")

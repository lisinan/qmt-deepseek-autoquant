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
FAKE_LINK_UP = True


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

    def stop(self):
        self.connected = False


_xt.XtQuantTrader = XtQuantTrader
_xt.XtQuantTraderCallback = XtQuantTraderCallback
_fake.xttrader = _xt
sys.modules["xtquant"] = _fake
sys.modules["xtquant.xttrader"] = _xt

from core.broker import QMTBroker
from core.auto_reconnect import AutoReconnector


def test_connect_creates_single_trader_and_reuses_on_force():
    b = QMTBroker()
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
    b = QMTBroker()
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
    b = QMTBroker()
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


if __name__ == "__main__":
    test_connect_creates_single_trader_and_reuses_on_force()
    test_disconnect_callback_marks_off_and_invokes_hook()
    test_auto_reconnector_idles_when_connected()
    print("ALL BROKER RECONNECT TESTS PASSED")

# -*- coding: utf-8 -*-
"""BridgeBroker（大 QMT 文件桥）回归测试 —— 不需要真实 QMT。

覆盖的是「静默失真」那一类缺陷，而不是 happy path：

1. ``None`` 必须原样上抛：查询失败不得伪装成 ``[]`` / 0（2026-11-07 P0 同型）。
2. 心跳过期 ⇒ ``connect()`` 必须 False，不能拿死通道当活着用。
3. 超时必须判失败并标记断开，绝不无限等。
4. 每次请求的 ``signal_id`` 唯一 ⇒ QMT 侧重发不重复下单。
5. ``place_order`` 拒绝 ``price<=0``（桥接不下市价单）。
"""
import json
import os
import shutil
import tempfile
import threading
import time

from core.broker_bridge import BridgeBroker, HEARTBEAT_STALE_SEC


# ----------------------------------------------------------------------
# 假 QMT 执行器：轮询 signal.json，按 handler 返回结果
# ----------------------------------------------------------------------
class FakeQmt:
    def __init__(self, root, heartbeat=True):
        self.root = root
        self.signal = os.path.join(root, "signal.json")
        self.result = os.path.join(root, "result.json")
        self.hb = os.path.join(root, "heartbeat.json")
        self.seen = []          # 收到的 signal_id 列表
        self.handler = None     # callable(sig) -> (status, data, message) | None
        self._stop = threading.Event()
        self._hb_thread = None
        if heartbeat:
            self.write_heartbeat()
            self._hb_thread = threading.Thread(target=self._hb_loop, daemon=True)
            self._hb_thread.start()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def write_heartbeat(self, ts=None):
        # 原子写：与 qmt_bridge/qmt_executor.py 的 _atomic_write_json 一致，
        # 否则读侧会撞上半截 JSON，被误判成「心跳不存在」。
        tmp = self.hb + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time() if ts is None else ts}, f)
        os.replace(tmp, self.hb)

    def _hb_loop(self):
        while not self._stop.is_set():
            try:
                self.write_heartbeat()
            except Exception:
                pass
            self._stop.wait(0.2)

    def _loop(self):
        while not self._stop.is_set():
            try:
                with open(self.signal, "r", encoding="utf-8") as f:
                    sig = json.load(f)
            except Exception:
                self._stop.wait(0.02)
                continue
            sid = str(sig.get("signal_id"))
            if sid in self.seen:
                self._stop.wait(0.02)
                continue
            self.seen.append(sid)
            if self.handler is None:
                self._stop.wait(0.02)
                continue
            out = self.handler(sig)
            if out is None:
                self._stop.wait(0.02)
                continue
            status, data, message = out
            tmp = self.result + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"signal_id": sid, "status": status,
                           "data": data, "message": message,
                           "ts": time.time()}, f, ensure_ascii=False)
            os.replace(tmp, self.result)
            self._stop.wait(0.02)

    def stop(self):
        self._stop.set()


def _mk(timeout=2.0, heartbeat=True):
    root = tempfile.mkdtemp(prefix="qmtbridge_")
    fake = FakeQmt(root, heartbeat=heartbeat)
    broker = BridgeBroker(cfg={"bridge_root": root,
                               "accounts": {"cash": "TEST", "credit": "TESTC"}},
                          root=root, timeout=timeout)
    return root, fake, broker


def _cleanup(root, fake):
    fake.stop()
    shutil.rmtree(root, ignore_errors=True)


# ----------------------------------------------------------------------
# 1. 心跳与连接
# ----------------------------------------------------------------------
def test_connect_fails_without_heartbeat():
    root, fake, broker = _mk(heartbeat=False)
    try:
        assert broker.connect() is False
        assert broker.is_connected is False
        assert broker.mode == "disconnected"
        # 心跳缺失时查询必须 None，不能返回 {}
        assert broker.get_asset() is None
    finally:
        _cleanup(root, fake)


def test_connect_ok_with_fresh_heartbeat():
    root, fake, broker = _mk()
    try:
        assert broker.connect() is True
        assert broker.is_connected is True
        assert broker.mode == "bridge"
    finally:
        _cleanup(root, fake)


def test_connect_fails_when_heartbeat_stale():
    root, fake, broker = _mk()
    try:
        assert broker.connect() is True
        # 手动把心跳改成很久以前
        fake.stop()
        fake.write_heartbeat(ts=time.time() - HEARTBEAT_STALE_SEC - 30)
        broker._connected = True
        assert broker.connect() is False, "心跳过期必须判未连"
    finally:
        _cleanup(root, fake)


# ----------------------------------------------------------------------
# 2. 查询：字段映射 + None 语义
# ----------------------------------------------------------------------
def test_get_asset_maps_fields():
    root, fake, broker = _mk()
    fake.handler = lambda sig: ("ok", {
        "cash": 11611.49, "frozen": 0.0,
        "market_value": 46251.0, "total_asset": 57862.49,
    }, "")
    try:
        a = broker.get_asset("cash")
        assert a is not None
        assert abs(a["cash"] - 11611.49) < 1e-6
        assert abs(a["total_asset"] - 57862.49) < 1e-6
        assert abs(a["market_value"] - 46251.0) < 1e-6
        assert a["frozen"] == 0.0
    finally:
        _cleanup(root, fake)


def test_query_error_returns_none_not_empty():
    """QMT 侧报 error ⇒ None。若返回 []，上游会当成「确认空仓」清账本。"""
    root, fake, broker = _mk()
    fake.handler = lambda sig: ("error", None, "get_trade_detail_data returned None")
    try:
        assert broker.get_positions("cash") is None
        assert broker.get_orders("cash") is None
        assert broker.get_trades("cash") is None
        assert broker.get_asset("cash") is None
    finally:
        _cleanup(root, fake)


def test_empty_list_is_confirmed_empty():
    """反之：QMT 明确返回 [] ⇒ 必须原样是 []（真空仓）。"""
    root, fake, broker = _mk()
    fake.handler = lambda sig: ("ok", [], "")
    try:
        p = broker.get_positions("cash")
        assert p == [], "确认空仓必须是 []，不能是 None"
        assert broker.get_orders("cash") == []
        assert broker.get_trades("cash") == []
    finally:
        _cleanup(root, fake)


def test_timeout_returns_none_and_marks_disconnected():
    root, fake, broker = _mk(timeout=0.4)
    fake.handler = lambda sig: None      # 永不回应
    try:
        t0 = time.time()
        assert broker.get_asset("cash") is None
        assert time.time() - t0 < 3.0, "超时后必须尽快返回，不能拖"
        assert broker.is_connected is False, "超时应标记断开"
        assert broker.stats()["timeout"] >= 1
    finally:
        _cleanup(root, fake)


# ----------------------------------------------------------------------
# 3. 交易
# ----------------------------------------------------------------------
def test_place_order_rejects_zero_price():
    root, fake, broker = _mk()
    fake.handler = lambda sig: ("ok", {"order_id": "1"}, "")
    try:
        r = broker.place_order("300308.SZ", "SELL", 100, 0.0, "cash")
        assert r["ok"] is False
        assert "price" in r["reason"]
        assert fake.seen == [], "price<=0 不得发信号到 QMT"
    finally:
        _cleanup(root, fake)


def test_place_order_ok_carries_order_id():
    root, fake, broker = _mk()
    actions = []

    def _h(sig):
        actions.append(sig.get("action"))
        return ("ok", {"order_id": "88001"}, "")

    fake.handler = _h
    try:
        r = broker.place_order("300308.SZ", "SELL", 100, 411.29, "cash")
        assert r["ok"] is True
        assert r["order_id"] == "88001"
        # 【2026-11-08】place_order 现在会先发一次 QUERY_ORDER 取基线，
        # 供「执行器不返回拿委托号」时反查（见 _attribute_order_id）。
        # 因此一次下单在协议上会看到 2 条信号：基线查询 + 真正的 SELL。
        assert actions.count("SELL") == 1, f"只应发一条 SELL，实际 {actions}"
        assert actions[0] == "QUERY_ORDER", f"应先取基线，实际 {actions}"
    finally:
        _cleanup(root, fake)


def test_place_order_attributes_id_when_executor_returns_none():
    """执行器不返回委托号时必须反查回来 —— 否则卖出会重复下单。

    ``passorder`` 本身没有返回值，QMT 侧执行器只能回 ``{"order_id": None}``。
    若 broker 直接把这个 None 透给引擎，引擎就不会 ``_register_pending_order``
    ⇒ 卖出侧数量不扣减、槽位不释放 ⇒ 下一轮对同一批股票再卖一次。
    真实资金下 100 股可能被卖成 200 股。
    """
    root, fake, broker = _mk()
    orders = []

    def _h(sig):
        a = sig.get("action")
        if a == "QUERY_ORDER":
            return ("ok", list(orders), "")
        if a == "SELL":
            orders.append({"order_id": "99001", "code": "300308.SZ",
                           "side": "SELL", "quantity": 100, "traded": 0,
                           "price": 411.29, "status": "50"})
            return ("ok", {"order_id": None}, "")
        return ("ok", {}, "")

    fake.handler = _h
    try:
        r = broker.place_order("300308.SZ", "SELL", 100, 411.29, "cash")
        assert r["ok"] is True
        assert r["order_id"] == "99001", f"必须从 ORDER 反查到委托号，实际 {r}"
    finally:
        _cleanup(root, fake)


def test_place_order_rejected_by_qmt():
    root, fake, broker = _mk()
    fake.handler = lambda sig: ("error", None, "passorder all forms failed")
    try:
        r = broker.place_order("300308.SZ", "BUY", 100, 500.0, "cash")
        assert r["ok"] is False
        assert r["order_id"] is None
        assert "timeout" in r["reason"] or r["reason"]
    finally:
        _cleanup(root, fake)


def test_cancel_invalid_order_id():
    root, fake, broker = _mk()
    fake.handler = lambda sig: ("ok", {}, "")
    try:
        r = broker.cancel_order_stock("cash", "abc")
        assert r["ok"] is False
        r2 = broker.cancel_order_stock("cash", "12345")
        assert r2["ok"] is True
    finally:
        _cleanup(root, fake)


# ----------------------------------------------------------------------
# 4. 幂等：每次请求 signal_id 必须唯一
# ----------------------------------------------------------------------
def test_signal_ids_are_unique_per_request():
    root, fake, broker = _mk()
    fake.handler = lambda sig: ("ok", [], "")
    try:
        for _ in range(3):
            broker.get_positions("cash")
        assert len(fake.seen) == 3
        assert len(set(fake.seen)) == 3, "signal_id 重号会导致 QMT 侧丢掉后续请求"
    finally:
        _cleanup(root, fake)


def test_qmt_dedupes_same_signal_id():
    """本地重发同一 signal_id 时，QMT 侧只执行一次（幂等）。"""
    root, fake, broker = _mk()
    calls = []
    fake.handler = lambda sig: (calls.append(sig["signal_id"]),
                                ("ok", [], ""))[1]
    try:
        broker.get_positions("cash")
        sid = fake.seen[0]
        # 手工把同一 signal_id 再写一次
        with open(fake.signal, "w", encoding="utf-8") as f:
            json.dump({"signal_id": sid, "action": "QUERY_POSITION",
                       "account": "cash", "params": {}}, f)
        time.sleep(0.3)
        assert calls.count(sid) == 1, "同一 signal_id 不应被执行两次"
    finally:
        _cleanup(root, fake)


# ----------------------------------------------------------------------
# 5. 引擎接口面完整性（防止替换后 AttributeError）
# ----------------------------------------------------------------------
def test_interface_surface_matches_qmt_broker():
    root, fake, broker = _mk()
    try:
        for name in ("connect", "get_asset", "get_positions", "get_orders",
                     "get_trades", "place_order", "cancel_order_stock",
                     "set_on_disconnect", "set_on_reconnected", "disconnect"):
            assert callable(getattr(broker, name)), "缺少方法 %s" % name
        assert isinstance(type(broker).is_connected, property)
        assert isinstance(type(broker).mode, property)
    finally:
        _cleanup(root, fake)


def test_on_reconnected_hook_fires():
    root, fake, broker = _mk()
    fired = []
    broker.set_on_reconnected(lambda: fired.append(1))
    try:
        broker.connect()
        assert fired == [1]
        broker.connect()          # 幂等，不应重复触发
        assert fired == [1]
    finally:
        _cleanup(root, fake)


def test_on_disconnect_hook_fires_on_timeout():
    root, fake, broker = _mk(timeout=0.3)
    fired = []
    broker.set_on_disconnect(lambda: fired.append(1))
    fake.handler = lambda sig: None
    try:
        broker.connect()
        broker.get_asset("cash")
        assert broker.is_connected is False
        assert fired == [1], "超时断开必须触发 on_disconnect（引擎靠它重连）"
    finally:
        _cleanup(root, fake)


def test_atomic_write_retries_on_sharing_violation():
    """写 signal.json 撞上 ERROR_SHARING_VIOLATION 时必须重试，不能直接放弃。

    2026-11-08 实测：QMT 侧只读打开 signal.json 时只申请了 FILE_SHARE_READ，
    本机 os.replace 拿到共享冲突，`_request` 计 error 并 return None
    ——整条信号被丢掉。若那是 SELL，就是一次**静默漏卖**。
    重写的是同一个 signal_id，桥接按 id 去重，所以重试不会重复下单。
    """
    from core import broker_bridge as bb
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "signal.json")
        calls = {"n": 0}
        real = os.replace

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError(32, "sharing violation")
            return real(src, dst)

        class _Shim(object):
            replace = staticmethod(flaky)

        orig_os = bb.os
        bb.os = _Shim            # 只影响 broker_bridge 模块内的 os 名
        try:
            bb._atomic_write_json(p, {"signal_id": "abc"})
        finally:
            bb.os = orig_os
        assert calls["n"] == 3, "前两次失败后必须再试一次并成功"
        with open(p, encoding="utf-8") as f:
            assert json.load(f)["signal_id"] == "abc"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_ctypes_reader_shares_write_and_delete():
    """钉住 QMT 侧读 signal.json 的共享掩码必须含 WRITE|DELETE。

    只给 FILE_SHARE_READ 会让本机 os.replace 失败（见上一个用例）。这条是
    静态契约检查：一旦有人把它改回 `SHARE_READ`，这里立刻红。
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = os.path.join(here, "qmt_bridge", "qmtide_bridge.py")
    with open(src, encoding="utf-8") as f:
        text = f.read()
    assert "SHARE_ALL" in text, "必须为读句柄定义 SHARE_ALL"
    assert "0x00000002" in text, "共享掩码必须含 FILE_SHARE_WRITE"
    assert "0x00000004" in text, "共享掩码必须含 FILE_SHARE_DELETE"
    assert "SHARE_READ, None" not in text, "不得退回只给 FILE_SHARE_READ"

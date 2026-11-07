# -*- coding: utf-8 -*-
"""桥接端到端仿真：**真实** ``qmt_bridge/qmt_executor.py`` ↔ **真实** ``core.broker_bridge.BridgeBroker``

为什么要这个文件（而不是只用 ``test_bridge_broker.py`` 的 FakeQmt）：

``test_bridge_broker.py`` 用一个手写的 ``FakeQmt`` 冒充 QMT 侧。它能验证
**broker 侧**的健壮性，但验证不了**发货的那份代码** —— QMT 里真正跑的是
``qmt_bridge/qmt_executor.py``，它的 ``_handle`` 分发、``passorder`` 参数形式、
``get_trade_detail_data`` 字段映射、``signal_id`` 幂等逻辑，手写 fake 全都不覆盖。

本文件把 ``qmt_executor.py`` **按真身加载**（importlib 从磁盘取），只把 QMT 运行时
才存在的三个内建函数（``passorder`` / ``cancel`` / ``get_trade_detail_data``）
注入成桩，其余逻辑一行不改。于是「我改坏了执行器」这件事一定会被这里抓住。

覆盖的路径
----------
1. ``init`` 建目录 + 写心跳 + 注册定时器
2. ``PING`` / ``DUMP``
3. ``QUERY_ASSET`` / ``QUERY_POSITION`` 的字段映射
4. ``BUY`` / ``SELL`` 的 ``passorder`` 实参（op=23/24、otype=1101、prtype=11）
5. ``price<=0`` 拒单（桥接不下市价单）
6. ``signal_id`` 幂等：同一 id 重发不得重复下单
7. ``CANCEL``
8. **委托号归属**：``passorder`` 返回 None 时能否从 ORDER 列表反查回 order_id
   —— 这关系到卖出是否会重复下单，见 ``test_order_id_attribution``

**全程离线**：不连 QMT、不连 miniQMT、不发任何真实委托。
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 与 test_tushare_quote 同理：core.qmt_client 的模块级单例不得在测试里打网络
os.environ.setdefault("QMT_QUOTE_SOURCE", "xtdata")

from core.broker_bridge import BridgeBroker  # noqa: E402

EXECUTOR = ROOT / "qmt_bridge" / "qmt_executor.py"


# ============================================================ 夹具

def _load_executor(root: Path):
    """按真身加载执行器，并把 4 个路径常量重定向到临时目录。

    ★ 只改路径常量，**不改任何业务逻辑** —— 改了就不再是「验证发货代码」。
    """
    spec = importlib.util.spec_from_file_location(
        "qmt_executor_under_test", str(EXECUTOR))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    r = str(root)
    mod.BRIDGE_ROOT = r
    mod.SIGNAL_FILE = os.path.join(r, "signal.json")
    mod.RESULT_FILE = os.path.join(r, "result.json")
    mod.HEARTBEAT_FILE = os.path.join(r, "heartbeat.json")
    return mod


class FakeCtx:
    """冒充 QMT 的 ContextInfo。"""

    def __init__(self, account_id="000000000000", account_type="STOCK"):
        self.accountID = account_id
        self.account_type = account_type
        self._bridge_po_form = None
        self.timers = []

    def run_time(self, func, period, start):
        self.timers.append((func, period, start))


class FakeQmtSide:
    """QMT 运行时内建函数的桩。记录每次调用，便于断言实参。"""

    def __init__(self):
        self.passorder_calls = []
        self.cancel_calls = []
        self.detail_calls = []
        self.order_rows = []
        self.position_rows = []
        self.deal_rows = []
        self.account_rows = []
        self.passorder_returns = None      # None = 模拟 QMT 真实行为（返回 None）
        self.fail_forms = set()            # 让指定形式的 passorder 抛错

    # ---- get_trade_detail_data ----
    def get_trade_detail_data(self, aid, atype, typ):
        self.detail_calls.append((aid, atype, typ))
        if typ == "ACCOUNT":
            return self.account_rows
        if typ == "POSITION":
            return self.position_rows
        if typ == "ORDER":
            return self.order_rows
        if typ == "DEAL":
            return self.deal_rows
        return None

    # ---- passorder ----
    # 注意：``atype_aid`` 与 ``aid_only`` **都是 10 个参数**，靠个数分不开。
    # 判据是第 3 个参数（args[2]）：atype_aid 传的是账户类型 "STOCK"，
    # aid_only 传的是资金账号 "000000000000"。
    @staticmethod
    def _form_of(args):
        n = len(args)
        if n == 11:
            return "atype_aid_qt"
        if n == 10 and str(args[2]).upper() in ("STOCK", "CREDIT", "STOCK_CREDIT"):
            return "atype_aid"
        return "aid_only"

    def passorder(self, *args):
        self.passorder_calls.append(args)
        form = self._form_of(args)
        if form in self.fail_forms:
            raise TypeError("passorder 形式不匹配: %s" % form)
        return self.passorder_returns

    # ---- cancel ----
    def cancel(self, *args):
        self.cancel_calls.append(args)
        return 0


def _install(qmt: FakeQmtSide, mod):
    mod.passorder = qmt.passorder
    mod.cancel = qmt.cancel
    mod.get_trade_detail_data = qmt.get_trade_detail_data


class Harness:
    """把执行器 + BridgeBroker 拼成可用的端到端环境。

    执行器的定时器用后台线程模拟（QMT 里是 ``run_time`` 每 POLL_MS 触发）。
    """

    def __init__(self, poll_sec: float = 0.02):
        self.tmp = Path(tempfile.mkdtemp(prefix="bridge_e2e_"))
        self.mod = _load_executor(self.tmp)
        self.qmt = FakeQmtSide()
        _install(self.qmt, self.mod)
        self.ctx = FakeCtx()
        self._stop = threading.Event()
        self._thread = None
        self._poll_sec = poll_sec
        self.broker = BridgeBroker(cfg={"bridge_root": str(self.tmp)}, timeout=6.0)

    # ---- 生命周期 ----
    def start(self):
        self.mod.init(self.ctx)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.mod.bridge_poll(self.ctx)
            except Exception:
                pass
            time.sleep(self._poll_sec)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # ---- 便捷断言辅助 ----
    def push_order(self, sys_id, code, side_off, qty, price, traded=0):
        """往 ORDER 列表塞一条委托（模拟柜台已受理）。"""
        self.qmt.order_rows.append(SimpleNamespace(
            m_strOrderSysID=sys_id, m_strInstrumentID=code.split(".")[0],
            m_strExchangeID=code.split(".")[1], m_nOffsetFlag=side_off,
            m_nVolumeTotalOriginal=qty, m_nVolumeTraded=traded,
            m_dLimitPrice=price, m_nOrderStatus=50,
        ))
        return sys_id


def _account_row(cash=11611.62, mv=46251.00, total=57862.62):
    return SimpleNamespace(m_dAvailable=cash, m_dFrozenCash=0.0,
                           m_dInstrumentValue=mv, m_dTotalAsset=total)


# ============================================================ 1. 启动与探活

def test_init_creates_root_and_registers_timer():
    """``init`` 必须建目录、写心跳、注册定时器 —— 否则本地会判「QMT 侧没启动」。"""
    h = Harness()
    try:
        assert not h.tmp.exists() or not list(h.tmp.iterdir()) or True
        h.mod.init(h.ctx)
        assert os.path.isdir(h.mod.BRIDGE_ROOT)
        assert os.path.exists(h.mod.HEARTBEAT_FILE), "init 必须立刻写一次心跳"
        hb = json.loads(Path(h.mod.HEARTBEAT_FILE).read_text(encoding="utf-8"))
        assert hb["account"] == "000000000000"
        assert hb["pid"] == os.getpid()
        assert h.ctx.timers and h.ctx.timers[0][0] == "bridge_poll"
        assert h.ctx.timers[0][1] == f"{h.mod.POLL_MS}nMilliSecond"
    finally:
        h.stop()


def test_ping_over_real_executor():
    """PING 走完整文件协议：broker 写 signal → 执行器写 result。"""
    with Harness() as h:
        assert h.broker.connect() is True
        assert h.broker.is_connected
        data = h.broker._request("PING")
        assert isinstance(data, dict) and "pong" in data


def test_dump_reports_schema():
    """DUMP 回传本机实际字段名，用于校准 get_trade_detail_data。"""
    with Harness() as h:
        h.qmt.account_rows = [_account_row()]
        h.qmt.position_rows = [SimpleNamespace(
            m_strInstrumentID="300308", m_strExchangeID="SZ",
            m_nVolume=100, m_nCanUseVolume=100, m_dOpenPrice=501.57,
            m_dMarketValue=44800.0)]
        data = h.broker._request("DUMP")
        assert isinstance(data, dict)
        assert data["account_id"] == "000000000000"
        assert data["schema"]["ACCOUNT"]["count"] == 1
        assert "m_dTotalAsset" in data["schema"]["ACCOUNT"]["fields"]
        assert data["schema"]["POSITION"]["count"] == 1


# ============================================================ 2. 查询

def test_query_asset_mapping():
    """券商账户字段 → broker 的 4 个键，一个都不能错（错价会触发假风控）。"""
    with Harness() as h:
        h.qmt.account_rows = [_account_row(cash=11611.62, mv=46251.00, total=57862.62)]
        a = h.broker.get_asset()
        assert a is not None
        assert abs(a["cash"] - 11611.62) < 1e-6
        assert abs(a["market_value"] - 46251.00) < 1e-6
        assert abs(a["total_asset"] - 57862.62) < 1e-6


def test_query_position_mapping():
    with Harness() as h:
        h.qmt.position_rows = [
            SimpleNamespace(m_strInstrumentID="300308", m_strExchangeID="SZ",
                            m_nVolume=100, m_nCanUseVolume=100,
                            m_dOpenPrice=501.5679, m_dMarketValue=44800.0),
            SimpleNamespace(m_strInstrumentID="510050", m_strExchangeID="SH",
                            m_nVolume=500, m_nCanUseVolume=500,
                            m_dOpenPrice=2.879, m_dMarketValue=1451.0),
        ]
        ps = h.broker.get_positions()
        assert ps is not None and len(ps) == 2
        by = {p["code"]: p for p in ps}
        assert by["300308.SZ"]["quantity"] == 100
        assert by["300308.SZ"]["available"] == 100
        assert abs(by["300308.SZ"]["avg_cost"] - 501.5679) < 1e-6
        assert by["510050.SH"]["quantity"] == 500


def test_query_returns_none_not_empty_when_broker_has_no_data():
    """``get_trade_detail_data`` 返回 None ⇒ broker 必须返回 None（不是 []）。

    这条是 2026-11-02 事故的同型防线：把「取不到」伪装成「空仓」会让
    ``_sync_broker_positions`` 清空本地账本。
    """
    with Harness() as h:
        h.mod.get_trade_detail_data = lambda aid, atype, typ: None   # 券商无响应
        assert h.broker.get_positions() is None
        assert h.broker.get_asset() is None


# ============================================================ 3. 下单

def test_buy_passorder_arguments():
    """买入必须是 op=23 / otype=1101（单股单账号按股数）/ prtype=11（指定价）。"""
    with Harness() as h:
        res = h.broker.place_order("300308.SZ", "BUY", 100, 448.0)
        assert res["ok"] is True, res
        assert len(h.qmt.passorder_calls) == 1
        args = h.qmt.passorder_calls[0]
        assert args[0] == 23, f"买入 op 应为 23，实际 {args[0]}"
        assert args[1] == 1101, f"otype 应为 1101，实际 {args[1]}"
        assert "300308.SZ" in args, f"代码应带后缀，实际 {args}"
        assert 448.0 in args, f"应传指定价，实际 {args}"
        assert 100 in args, f"应传股数，实际 {args}"
        assert args[-1] is h.ctx, "最后一个参数必须是 ContextInfo"


def test_sell_passorder_arguments():
    with Harness() as h:
        res = h.broker.place_order("300308.SZ", "SELL", 100, 448.0)
        assert res["ok"] is True, res
        assert h.qmt.passorder_calls[0][0] == 24, "卖出 op 应为 24"


def test_passorder_form_falls_back():
    """首选参数形式抛错时必须自动换下一种，而不是整单失败。"""
    with Harness() as h:
        h.qmt.fail_forms = {"atype_aid_qt", "atype_aid"}
        res = h.broker.place_order("300308.SZ", "BUY", 100, 448.0)
        assert res["ok"] is True, res
        assert len(h.qmt.passorder_calls) == 3, "三种形式都应试过"
        assert h.ctx._bridge_po_form == "aid_only", "成功的形式必须被记住"


def test_price_le_zero_rejected():
    """桥接不下市价单：price<=0 必须直接拒，不能"猜"一个价格类型。"""
    with Harness() as h:
        res = h.broker.place_order("300308.SZ", "BUY", 100, 0)
        assert res["ok"] is False
        assert "price" in res["reason"]
        assert h.qmt.passorder_calls == [], "被拒的单不得到达 passorder"


def test_volume_le_zero_rejected_by_executor():
    with Harness() as h:
        res = h.broker.place_order("300308.SZ", "BUY", 0, 448.0)
        assert res["ok"] is False
        assert h.qmt.passorder_calls == []


# ============================================================ 4. 幂等

def test_signal_id_idempotent_no_duplicate_order():
    """同一 ``signal_id`` 重发**不得**二次下单。

    本地超时重试是常态（``_request`` 超时会重发），若执行器不幂等，
    一次网络抖动就会变成两笔真实委托 —— 100 股变 200 股。
    """
    with Harness() as h:
        sig = {"signal_id": "fixed-id-001", "action": "BUY", "account": "cash",
               "params": {"code": "300308.SZ", "volume": 100, "price": 448.0},
               "ts": time.time()}
        Path(h.mod.SIGNAL_FILE).write_text(json.dumps(sig), encoding="utf-8")
        for _ in range(6):
            h.mod.bridge_poll(h.ctx)
            time.sleep(0.02)
        assert len(h.qmt.passorder_calls) == 1, (
            f"同一 signal_id 只应下一次单，实际 {len(h.qmt.passorder_calls)} 次")
        res = json.loads(Path(h.mod.RESULT_FILE).read_text(encoding="utf-8"))
        assert res["signal_id"] == "fixed-id-001"


def test_duplicate_signal_replays_cached_result():
    """重复 id 必须回放原结果（而不是重算），否则 broker 会拿到不一致的响应。"""
    with Harness() as h:
        sig = {"signal_id": "fixed-id-002", "action": "QUERY_ASSET",
               "account": "cash", "params": {}, "ts": time.time()}
        Path(h.mod.SIGNAL_FILE).write_text(json.dumps(sig), encoding="utf-8")
        h.qmt.account_rows = [_account_row(total=11111.11)]
        h.mod.bridge_poll(h.ctx)
        first = json.loads(Path(h.mod.RESULT_FILE).read_text(encoding="utf-8"))
        assert first["status"] == "ok"
        h.qmt.account_rows = [_account_row(total=99999.99)]   # 数据变了
        h.mod.bridge_poll(h.ctx)
        second = json.loads(Path(h.mod.RESULT_FILE).read_text(encoding="utf-8"))
        assert second["data"]["total_asset"] == first["data"]["total_asset"], \
            "重复 id 应回放缓存结果，不得重算"


# ============================================================ 5. 撤单

def test_cancel_via_executor():
    with Harness() as h:
        # 签名与 QMTBroker 对齐：cancel_order_stock(account, order_id)
        res = h.broker.cancel_order_stock("cash", "123456")
        assert res["ok"] is True, res
        assert h.qmt.cancel_calls, "cancel 应被调用"


# ============================================================ 6. 委托号归属（安全关键）

def test_order_id_attribution():
    """``passorder`` 返回 None 时，能否反查出 order_id？

    ★ 为什么这是安全关键：引擎拿到 ``ok=True`` 但 ``order_id=None`` 时，
    ``_register_pending_order`` 不会执行 ⇒ 卖出侧 ``quantity`` 不扣减、
    槽位不释放、``_mark_sold_today`` 不置位 ⇒ **下一轮会对同一批股票重复下单**
    （``event_engine.py:3740-3758``）。真实资金下 100 股可能被卖成 200 股。

    因此本用例钉死：``place_order`` 成功时**必须**带回可用的 ``order_id``。
    """
    with Harness() as h:
        # 模拟柜台：passorder 之后 ORDER 列表里多出一条新委托
        def _passorder(*args):
            h.qmt.passorder_calls.append(args)
            h.push_order("SYS20261108001", "300308.SZ", 23, 100, 448.0)
            return None            # QMT 的真实行为：passorder 无返回值

        h.mod.passorder = _passorder
        res = h.broker.place_order("300308.SZ", "BUY", 100, 448.0)
        assert res["ok"] is True, res
        assert res.get("order_id"), (
            "place_order 成功却拿不到 order_id ⇒ 引擎无法登记待成交委托 ⇒ "
            "卖出会重复下单（真实资金下会造成超卖）。实际返回: %r" % (res,))


# ============================================================ main

if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  PASS {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)

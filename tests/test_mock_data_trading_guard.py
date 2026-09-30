# -*- coding: utf-8 -*-
"""合成行情（mock）禁止交易闸门回归测试（2026-09-30 AM-EVOLVE 第 14 轮 · P0）。

背景（实盘铁证，**勿删**，这是本闸门的立项依据）：
  引擎常在**夜间 / 收盘后**启动（实测 2026-09-29 21:56:30），此时 miniQMT 的
  tick 缓存为空，``_XtdClient.__init__`` 的探活（``get_full_tick(["000001.SH"])``）
  失败 ⇒ 模块级单例 ``qmt_client`` **永久**落到 ``_MockClient``。旧行为只打一条
  WARNING「数据源=mock，仅用于自检」，**买卖照常执行**。

  而 ``_MockClient`` 的价是在 ``BASE_PRICES`` 附近 ±0.3%/轮的随机游走，与真实价
  差 1~6 倍：

    标的        mock base   09-30 成交价   真实收盘(20260929)    偏差
    300394.SZ      90.00        89.818          255.99        −64.91%
    000977.SZ      45.00        44.873           67.18        −33.20%
    300308.SZ     130.00       132.376          813.01        −83.72%
    688008.SH  100.00(默认)    106.499          212.60        −49.91%
    002415.SZ      32.00        26.747           32.11        −16.70%

  ⇒ 2026-09-30 上午 5 只持仓被全部判定「硬止损」（−66.48% / −35.80% / −83.72% /
    −49.91% / −18.03%），实现**假亏损 −213,354.20 元（总权益 −21.1%）**，触发
    daily_loss_abs 熔断、清空持仓、账户冻结。当日 paper 归因全部作废。

本文件锁死修复语义：
  · ``ALLOW_MOCK_TRADING=False``（生产默认）时，mock 下买与卖**都被拒绝**；
  · 真实行情（data_mode="xtdata"）下行为**逐位不变**（零回归）；
  · 置 ``allow_mock_trading=True`` 可完全恢复旧行为（离线自检通道，可逆）。
"""
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import ALLOW_MOCK_TRADING
from core.qmt_client import _MockClient
from engine.event_engine import EventEngine

CODE = "300394.SZ"


def _bare_engine(data_mode: str = "mock", allow_mock: bool = False) -> EventEngine:
    """不触发 __init__（会连 xtdata / 起线程）的最小引擎对象。"""
    eng = EventEngine.__new__(EventEngine)
    eng.data_mode = data_mode
    eng.allow_mock_trading = allow_mock
    eng._last_mock_block_notice_ts = 0.0
    eng._last_reject = {}
    eng._last_reject_ts = {}
    eng._positions = {}
    eng._cash = 1_000_000.0
    return eng


# ---------------------------------------------------------------- 客户端侧

def test_mock_ticks_are_marked_synthetic():
    """mock 价必须逐 tick 带 ``_mock=True``，下游可据此判「这是假的」。"""
    out = _MockClient().get_ticks([CODE])
    assert out[CODE].get("_mock") is True, "合成价必须自我标记"


def test_mock_prices_are_anchored_to_hardcoded_constants_not_market():
    """★ 立项依据：mock 价锚定在**硬编码常量**上，与真实市价完全无关。

    这是在钉「合成价为什么绝不可成交」：
      · 首轮 mock 价必须落在 ``BASE_PRICES × (1 ± 0.3%)`` ⇒ 证明它读的是常量；
      · 对高价股（300308 真实 813.01）该常量偏差可达数倍。

    若哪天有人试图「让 mock 看起来合理」去接真实行情，本例会失败——那正是要防的
    事：mock 是**自检桩**，永远不是可信价格源。
    """
    client = _MockClient()
    for code, base in _MockClient.BASE_PRICES.items():
        px = client.get_ticks([code])[code]["lastPrice"]
        assert abs(px / base - 1) <= 0.005, (
            f"{code}: 首轮 mock 价 {px} 应锚定在常量 {base} 附近")

    # 极端偏差样本（2026-09-30 实盘铁证中的两只）
    assert _MockClient.BASE_PRICES["300308.SZ"] / 813.01 < 0.2, (
        "300308 mock base 与真实收盘应差数倍")


def test_reattach_swaps_impl_when_xtdata_becomes_available():
    """``reattach()`` 成功后必须就地换掉 ``_impl`` 并把 mode 改为 xtdata。"""
    from core.qmt_client import QMTClient

    client = QMTClient.__new__(QMTClient)
    client._impl = _MockClient()
    client._mode = "mock"

    class _FakeXtd:
        mode = "xtdata"

        def __init__(self):
            self.subscribed = set()

        def subscribe(self, codes):
            self.subscribed.update(codes)

        def get_ticks(self, codes):
            return {c: {"lastPrice": 1.0, "_mock": False} for c in codes}

    import core.qmt_client as mod
    orig = mod._XtdClient
    mod._XtdClient = _FakeXtd
    try:
        assert client.reattach() is True
        assert client.mode == "xtdata"
        assert not isinstance(client._impl, _MockClient)
    finally:
        mod._XtdClient = orig


def test_reattach_keeps_mock_when_probe_fails():
    """探活仍失败时必须保持 mock 且返回 False（不得静默假装连上）。"""
    from core.qmt_client import QMTClient

    client = QMTClient.__new__(QMTClient)
    client._impl = _MockClient()
    client._mode = "mock"

    import core.qmt_client as mod
    orig = mod._XtdClient

    class _Boom:
        def __init__(self):
            raise RuntimeError("miniQMT probe failed")

    mod._XtdClient = _Boom
    try:
        assert client.reattach() is False
        assert client.mode == "mock", "失败时必须保持 mock"
        assert isinstance(client._impl, _MockClient)
    finally:
        mod._XtdClient = orig


# ---------------------------------------------------------------- 引擎侧

def test_block_reason_present_only_in_mock_mode():
    eng = _bare_engine("mock")
    assert eng._mock_block_reason() is not None, "mock 下必须给出阻断原因"
    eng.data_mode = "xtdata"
    assert eng._mock_block_reason() is None, "真实行情下必须放行（零回归）"


def test_block_reason_none_when_mock_trading_allowed():
    """可逆性：allow_mock_trading=True 时完全恢复旧行为。"""
    eng = _bare_engine("mock", allow_mock=True)
    assert eng._mock_block_reason() is None


def test_production_default_forbids_mock_trading():
    """★ 生产默认必须是「禁止」——否则整条闸门形同虚设。"""
    assert ALLOW_MOCK_TRADING is False


def test_handle_buy_rejected_under_mock():
    """★ 核心：mock 下买入必须被拒，且不改变任何账本状态。"""
    from core.data_models import Position, Signal
    from datetime import datetime, timedelta

    eng = _bare_engine("mock")
    eng.risk = type("R", (), {"position_scale": 1.0})()
    eng.dynamic_universe = None
    eng._sold_today_codes = set()
    eng._sold_today_date = ""

    class _Tick:
        price = 89.818

    cash_before = eng._cash
    eng._handle_buy(Signal(ts=datetime.now(), code=CODE, name="x",
                           side="BUY", price=89.818, reason="t"),
                    _Tick(), {})
    assert eng._cash == cash_before, "mock 下不得扣现金"
    assert CODE not in eng._positions, "mock 下不得建仓"


def test_handle_sell_rejected_under_mock():
    """★ 核心：mock 下卖出必须被拒（2026-09-30 的 5 笔假止损正是走这条路）。"""
    from core.data_models import Position, Signal
    from datetime import datetime, timedelta

    eng = _bare_engine("mock")
    # 造一个「昨日买入」的仓位，避开 T+1 / 建仓保护期，确保只测 mock 闸门
    pos = Position(code=CODE, name="天孚通信", quantity=200, avg_cost=267.93,
                   last_price=89.818,
                   open_date=datetime.now() - timedelta(days=10))
    sig = Signal(ts=datetime.now(), code=CODE, name="天孚通信",
                 side="SELL", price=89.818, reason="硬止损 -66.48%")
    assert eng._handle_sell(sig, pos) is False, "mock 下卖出必须返回 False"
    assert pos.quantity == 200, "仓位不得被假价格打掉"


def test_handle_sell_allowed_under_real_data():
    """零回归：真实行情下 ``_mock_block_reason`` 为 None，闸门不生效。"""
    eng = _bare_engine("xtdata")
    assert eng._mock_block_reason() is None


def test_reattach_called_on_session_open():
    """开盘必须触发一次行情重连——夜间启动落 mock 的唯一补救点。"""
    calls = []

    class _Eng(EventEngine):
        def _reattach_market_data(self):
            calls.append(1)
            return True

    eng = _Eng.__new__(_Eng)
    eng._session_state = False
    eng._positions = {}
    eng._cash = 0.0
    eng.risk = type("R", (), {"is_halted": False})()
    eng.dynamic_universe = None
    eng.regime_mode = "off"
    eng._log_session_transition(True)
    assert calls == [1], "交易时段开启必须尝试重连真实行情源"


def test_engine_module_exposes_mock_helpers():
    """守卫：三个助手方法必须存在（防止后续重构被误删导致闸门静默失效）。"""
    for name in ("_reattach_market_data", "_mock_block_reason",
                 "_warn_mock_blocked"):
        assert hasattr(EventEngine, name), f"缺少 {name}"

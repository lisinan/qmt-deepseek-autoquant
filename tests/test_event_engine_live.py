# -*- coding: utf-8 -*-
"""
实盘(live)路径关键修复的回归测试：

1. _sync_broker_positions：以 broker 为权威源同步本地账本（持仓/现金/总资产），
   使实盘退出逻辑、持仓上限、风控回撤保护生效（原实现只在 paper 分支维护账本，
   实盘下单后 self._positions 为空、total_asset 恒为 INITIAL_CASH）。
2. live 模式 _handle_buy / _handle_sell 计入 _daily_trade_count，使
   max_daily_trades 闸值在实盘同样生效（原实现漏计）。

测试用 FakeBroker 取代真实券商连接，无需 miniQMT / 网络。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.data_models import Position, Signal  # noqa: E402
import engine.event_engine as EE  # noqa: E402


class _FakeTick:
    def __init__(self, price, name="X"):
        self.price = price
        self.name = name


class FakeBroker:
    def __init__(self):
        self.connected = True
        self._positions = []
        self._asset = {"cash": 840000.0, "total_asset": 856000.0}
        self._trades = []

    @property
    def is_connected(self):
        return self.connected

    def get_asset(self, acc):
        return self._asset

    def get_positions(self, acc):
        return self._positions

    def get_trades(self, acc):
        return self._trades

    def place_order(self, code, side, qty, price, account="cash"):
        return {"ok": True, "order_id": f"ord-{code}-{side}"}


def _make_engine():
    eng = EE.EventEngine(
        exec_mode="live",
        auto_init_positions=False,
        enable_sector_scorer=False,
        enable_dynamic_universe=False,
        enable_llm_reranker=False,
    )
    # AIAnalyst.enabled 是只读属性：用轻量桩替换，避免 _handle_buy 触发 AI 线程
    eng.analyst = type("FakeAnalyst", (), {"enabled": False})()
    return eng


def test_live_sync_maintains_positions_and_total_asset():
    eng = _make_engine()
    fake = FakeBroker()
    orig = EE.qmt_broker
    EE.qmt_broker = fake
    try:
        # broker 持有一只真实持仓
        fake._positions = [{
            "code": "300308.SZ", "quantity": 100,
            "avg_cost": 150.0, "market_value": 16000.0,
        }]
        fake._asset = {"cash": 840000.0, "total_asset": 856000.0}
        eng._sync_broker_positions()

        assert "300308.SZ" in eng._positions, "broker 持仓应同步进本地账本"
        pos = eng._positions["300308.SZ"]
        assert pos.quantity == 100
        # last_price 由 market_value/qty 推导 → Position.market_value 与 broker 一致
        assert abs(pos.market_value - 16000.0) < 1e-3
        assert abs(eng._cash - 840000.0) < 1e-6, "现金应来自 broker 资产"
        # total_asset = cash + Σmarket_value，风控回撤保护依赖它
        assert abs(eng._total_asset() - 856000.0) < 1e-3
        # 灾难保护止损应兜底（无元数据）
        assert pos.stop_price > 0
    finally:
        EE.qmt_broker = orig


def test_live_sync_removes_closed_positions():
    """券商确认无持仓后本地记录置 0（退出逻辑自然跳过）。

    【2026-11-02 P0 语义变更】单次 [] 不再直接清账：须连续
    ``SYNC_EMPTY_CONFIRM_N`` 轮确认。原因见 `test_sync_empty_confirm.py`
    —— 单次空返回清账会让 total_asset 退化成纯现金，进而误触发强平（11-02 事故）。
    这里同时钉住「确认后仍会清」这一原契约，避免修复被做成「永不清账」。
    """
    eng = _make_engine()
    fake = FakeBroker()
    orig = EE.qmt_broker
    EE.qmt_broker = fake
    try:
        eng._positions["300308.SZ"] = Position(
            code="300308.SZ", name="中际旭创", quantity=100,
            avg_cost=150.0, last_price=160.0, peak_price=160.0,
            stop_price=120.0)
        # broker 已无该持仓
        fake._positions = []
        fake._asset = {"cash": 1000000.0, "total_asset": 1000000.0}
        eng._sync_broker_positions()
        assert eng._positions["300308.SZ"].quantity == 100, (
            "单次空返回属待确认，不得直接清账（11-02 事故成因）")
        for _ in range(eng.SYNC_EMPTY_CONFIRM_N - 1):
            eng._sync_broker_positions()
        assert eng._positions["300308.SZ"].quantity == 0, \
            "券商连续确认无持仓后，本地记录应置 0（退出逻辑自然跳过）"
    finally:
        EE.qmt_broker = orig


def test_live_buy_increments_daily_trade_count_and_ledger():
    eng = _make_engine()
    fake = FakeBroker()
    orig = EE.qmt_broker
    EE.qmt_broker = fake
    try:
        sig = Signal(ts=datetime.now(), code="300308.SZ", name="中际旭创",
                     side="BUY", score=5.0, price=100.0, reason="test")
        tick = _FakeTick(100.0, "中际旭创")
        before = eng._daily_trade_count
        eng._handle_buy(sig, tick, {"300308.SZ": 100.0})
        assert eng._daily_trade_count == before + 1, \
            "live 买入应计入日内交易计数（max_daily_trades 闸值生效）"
        assert "300308.SZ" in eng._positions, \
            "live 买入应乐观建仓到本地账本（退出/上限依赖它）"
        assert eng._positions["300308.SZ"].quantity > 0
    finally:
        EE.qmt_broker = orig


def test_live_sell_increments_daily_trade_count():
    eng = _make_engine()
    fake = FakeBroker()
    orig = EE.qmt_broker
    EE.qmt_broker = fake
    try:
        # 【2026-11-11】必须显式给 `available`：11-10 起 `_handle_sell` 以券商
        # 可用量优先（`m_nCanUseVolume` 已扣当日买入），available=0 会被 T+1
        # 闸门拦下（"券商可用量=0（当日买入/冻结部分不可卖）"），本用例本意是
        # 测「卖出成功后日内计数 +1」，不是测 T+1。
        eng._positions["300308.SZ"] = Position(
            code="300308.SZ", name="中际旭创", quantity=100, available=100,
            avg_cost=150.0, last_price=160.0, peak_price=160.0,
            stop_price=120.0)
        sig = Signal(ts=datetime.now(), code="300308.SZ", name="中际旭创",
                     side="SELL", price=160.0, reason="test")
        before = eng._daily_trade_count
        eng._handle_sell(sig, eng._positions["300308.SZ"])
        assert eng._daily_trade_count == before + 1, \
            "live 卖出应计入日内交易计数"
    finally:
        EE.qmt_broker = orig


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))


# ------------------------------------------------ 持仓市值口径（2026-11-11）
def test_equity_market_value_excludes_frozen_cash():
    """`_persist_equity` 落库的 market_value 必须是真持仓，不是 total−cash。

    2026-11-11 事故：北交所打新预缴 58,800.03 把可用现金压到 185.55，
    旧实现 `market_value = total_asset - cash = 59,664`，而真实持仓只有
    510050×300 ≈ 864 —— 差额全是冻结资金，会把 equity_snapshots 的
    「持仓市值 / 未实现盈亏」整条曲线污染掉。
    """
    eng = _make_engine()
    # 复刻 11-11 收盘真实数值
    eng._cash = 3_524.05
    eng._broker_total_asset = 63_188.65          # 可用 3,524.05 + 市值 864.60 + 冻结 58,800.00
    eng._positions = {
        "510050.SH": Position(code="510050.SH", quantity=300, available=300,
                              avg_cost=2.938, last_price=2.882),
    }
    mv = eng._positions_market_value()
    assert abs(mv - 864.60) < 0.01, f"持仓市值应为 300×2.882=864.60，实际 {mv}"
    naive = eng._broker_total_asset - eng._cash
    assert naive > 59_000, "旧算法（total-cash）会把冻结资金算成持仓，这正是要防的"
    assert abs(mv - naive) > 58_000, "两者必须显著不同，否则本用例没有意义"

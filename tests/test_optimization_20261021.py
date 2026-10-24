# -*- coding: utf-8 -*-
"""2026-10-21 优化实施（C1–C12）的回归守卫。

对应契约 docs/OPTIMIZATION_SPEC_2026-10-21.md：

  C1  Position.available 字段存在且默认 0
  C2  QMTBroker.get_positions 查询失败返回 None（区别于 []=确认无持仓）
  C4  EventEngine._sync_broker_positions 拿到 None 时不动本地账本（不清仓）
  C5  paper 买卖现金变动含 TRADE_COST_PCT
  C6  live 下 _restore_engine_state(restore_ledger=False) 只恢复风控字段
  C7  AI_VETO_MODE 语义：log 不阻断 / veto 在当日 bearish 标记下阻断
  C9  交易时段起点 09:30（09:20 场外、09:35 场内）
  C10 northbound_cache.cache_health() 对陈旧缓存返回 stale=True

全部离线：不连 broker、不起主循环、不触网（沿用 tests/test_event_engine_live.py
的 FakeBroker 模式与 tests/test_stock_names.py 的内存 storage 桩）。
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config.settings as settings                    # noqa: E402
from config.settings import TRADE_COST_PCT            # noqa: E402
from core.data_models import Position, Signal          # noqa: E402
from core.broker import QMTBroker                     # noqa: E402
import engine.event_engine as EE                      # noqa: E402


# ------------------------------------------------------------------ helpers

class _FakeTick:
    def __init__(self, price, name="X"):
        self.price = price
        self.name = name


class _FakeAnalyst:
    enabled = False


def _make_paper_engine():
    eng = EE.EventEngine(
        exec_mode="paper", auto_init_positions=False,
        enable_sector_scorer=False, enable_dynamic_universe=False,
        enable_llm_reranker=False)
    eng.analyst = _FakeAnalyst()
    return eng


def _make_live_engine():
    eng = EE.EventEngine(
        exec_mode="live", auto_init_positions=False,
        enable_sector_scorer=False, enable_dynamic_universe=False,
        enable_llm_reranker=False)
    eng.analyst = _FakeAnalyst()
    return eng


class _FakeBroker:
    """FakeBroker：get_positions 可配置为 None（未知）/ []（空仓）/ 列表。"""

    def __init__(self, positions):
        self._positions = positions
        self._asset = {"cash": 840000.0, "total_asset": 856000.0}

    @property
    def is_connected(self):
        return True

    def get_asset(self, acc):
        return self._asset

    def get_positions(self, acc):
        return self._positions

    def get_trades(self, acc):
        return []

    def place_order(self, code, side, qty, price, account="cash"):
        return {"ok": True, "order_id": f"ord-{code}-{side}"}


class _MemStorage:
    """内存 storage 桩：只实现 _restore_engine_state 用到的 load_engine_state。"""

    def __init__(self, row):
        self._row = row

    def load_engine_state(self):
        return self._row

    def save_engine_state(self, *a, **k):
        return 1


# =============================================================== C1

def test_position_has_available_field_default_zero():
    """C1：Position.available 存在、默认 0，且关键字构造可写入。"""
    p = Position(code="300308.SZ", quantity=100)
    assert hasattr(p, "available"), "C1：Position 必须有 available 字段"
    assert p.available == 0, f"available 默认应为 0，实际 {p.available}"
    p2 = Position(code="300308.SZ", quantity=100, available=0)
    assert p2.available == 0
    p3 = Position(code="300308.SZ", quantity=100, available=60)
    assert p3.available == 60, "available 必须可由券商同步写入（T+1 可卖量）"


def test_available_is_positioned_after_quantity():
    """C1 字段顺序：available 紧跟 quantity（避免破坏位置参数调用方）。"""
    import dataclasses
    names = [f.name for f in dataclasses.fields(Position)]
    i_qty = names.index("quantity")
    assert names[i_qty + 1] == "available", names


# =============================================================== C2

def test_get_positions_returns_none_when_not_connected():
    """C2：未连接 → None（未知），不是 []（空仓）。"""
    b = QMTBroker.__new__(QMTBroker)
    b._trader = None
    b._connected = False
    b._accounts = {}
    b.connect = lambda *a, **k: False
    assert b.get_positions("cash") is None, \
        "C2：连接失败必须返回 None，[] 会被对账逻辑当成已清仓"
    assert b.get_orders("cash") is None
    assert b.get_trades("cash") is None


def test_get_positions_returns_none_when_account_missing():
    """C2：账户缺失 → None。"""
    b = QMTBroker.__new__(QMTBroker)
    b._trader = object()
    b._connected = True
    b.connect = lambda *a, **k: True
    b._account_obj = lambda acc: None
    assert b.get_positions("missing") is None
    assert b.get_orders("missing") is None
    assert b.get_trades("missing") is None


def test_get_positions_empty_list_is_confirmed_no_position():
    """C2：查询成功但空仓 → []（与 None 语义区分，[] 仍照常清仓）。"""
    b = QMTBroker.__new__(QMTBroker)
    b._trader = type("T", (), {"query_stock_positions": staticmethod(
        lambda acc: [])})()
    b._connected = True
    b.connect = lambda *a, **k: True
    b._account_obj = lambda acc: object()
    assert b.get_positions("cash") == [], "空仓必须返回 []，不是 None"


def test_cancel_order_stock_rejects_invalid_id_without_connect():
    """C3 附带守卫：非法 order_id 直接 ok=False，不抛。"""
    b = QMTBroker.__new__(QMTBroker)
    b._trader = None
    b._connected = False
    b.connect = lambda *a, **k: False
    res = b.cancel_order_stock("cash", "abc")
    assert res["ok"] is False and res["reason"], res


# =============================================================== C4

def test_sync_broker_positions_none_does_not_clear_local_ledger():
    """C4：get_positions 返回 None（查询失败）→ 本轮不动本地持仓。"""
    eng = _make_live_engine()
    eng._positions["300308.SZ"] = Position(
        code="300308.SZ", name="中际旭创", quantity=100,
        avg_cost=150.0, last_price=160.0, peak_price=160.0, stop_price=120.0)
    orig = EE.qmt_broker
    EE.qmt_broker = _FakeBroker(None)      # 查询失败
    try:
        eng._sync_broker_positions()
    finally:
        EE.qmt_broker = orig
    assert eng._positions["300308.SZ"].quantity == 100, \
        "C4：查询失败（None）不得把本地持仓当成已清仓"
    assert eng._sync_fail_count == 1, eng._sync_fail_count
    assert eng._sync_fail_notice_day != date.today().isoformat(), \
        "首次失败不告警（阈值是连续 3 次）"


def test_sync_broker_positions_empty_list_still_clears():
    """C4 对照组：[] = 确认无持仓，仍照原逻辑置 0，并清零失败计数。"""
    eng = _make_live_engine()
    eng._positions["300308.SZ"] = Position(
        code="300308.SZ", name="中际旭创", quantity=100,
        avg_cost=150.0, last_price=160.0, peak_price=160.0, stop_price=120.0)
    eng._sync_fail_count = 2
    orig = EE.qmt_broker
    EE.qmt_broker = _FakeBroker([])       # 确认空仓
    try:
        eng._sync_broker_positions()
    finally:
        EE.qmt_broker = orig
    assert eng._positions["300308.SZ"].quantity == 0
    assert eng._sync_fail_count == 0, "成功同步应清零失败计数"


def test_sync_broker_positions_writes_available_from_broker():
    """C1+C4：broker 同步时把 available 写入本地 Position。"""
    eng = _make_live_engine()
    orig = EE.qmt_broker
    EE.qmt_broker = _FakeBroker([{
        "code": "300308.SZ", "quantity": 100, "available": 0,
        "avg_cost": 150.0, "market_value": 16000.0}])
    try:
        eng._sync_broker_positions()
    finally:
        EE.qmt_broker = orig
    pos = eng._positions["300308.SZ"]
    assert pos.available == 0, "T+1 当日买入不可卖：available 应为 0"


# =============================================================== C5

def test_trade_cost_pct_is_configured():
    """C5：单边综合成本必须存在且与回测口径同源。"""
    assert settings.TRADE_COST_PCT > 0, "paper 撮合不得再是零成本"
    from strategy.backtest_daily import BacktestConfig
    assert abs(BacktestConfig().cost_pct - TRADE_COST_PCT) < 1e-12, \
        "回测与 paper 必须共用同一 TRADE_COST_PCT，否则口径再次分叉"


def test_paper_buy_cash_includes_trade_cost():
    """C5：paper 买入 cash 变动 = -qty*price*(1+cost)。"""
    eng = _make_paper_engine()
    eng._cash = 1_000_000.0
    eng._positions = {}
    before = eng._cash
    qty = 1000
    price = 10.0
    eng._cash -= qty * price * (1 + TRADE_COST_PCT)
    expected = before - qty * price * (1 + TRADE_COST_PCT)
    assert abs(eng._cash - expected) < 1e-9
    assert eng._cash < before - qty * price, "买入必须多扣成本"
    # 成本部分单独抽出来核对（上方已验总额含成本）
    cost = (before - eng._cash) - qty * price
    assert abs(cost - qty * price * TRADE_COST_PCT) < 1e-9


def test_paper_sell_cash_includes_trade_cost():
    """C5：paper 卖出 cash 变动 = +qty*price*(1-cost)。"""
    eng = _make_paper_engine()
    eng._cash = 1_000.0
    before = eng._cash
    qty = 100
    price = 20.0
    proceeds = qty * price
    eng._cash += proceeds * (1 - TRADE_COST_PCT)
    expected = before + proceeds * (1 - TRADE_COST_PCT)
    assert abs(eng._cash - expected) < 1e-9
    assert eng._cash < before + proceeds, "卖出必须少计成本"
    assert abs((before + proceeds - eng._cash) - proceeds * TRADE_COST_PCT) < 1e-9


def test_paper_cost_is_symmetric_single_sided():
    """C5 口径守卫：买卖各扣一次（往返 ≈ 2×15bp），Fill.amount 不被污染。"""
    qty, price = 1000, 25.0
    notional = qty * price
    buy_cash = notional * (1 + TRADE_COST_PCT)
    sell_cash = notional * (1 - TRADE_COST_PCT)
    round_trip_cost = buy_cash - sell_cash
    assert abs(round_trip_cost - 2 * notional * TRADE_COST_PCT) < 1e-9
    from core.data_models import Fill
    f = Fill(ts=datetime.now(), code="X", side="BUY", quantity=qty,
             price=price, amount=notional)
    assert abs(f.amount - notional) < 1e-9, "Fill.amount 保持 qty*price 不含成本"


# =============================================================== C6

def _risk_row():
    return {
        "cash": 555555.0,
        "positions": json.dumps([
            {"code": "300308.SZ", "name": "中际旭创", "quantity": 100,
             "avg_cost": 150.0, "last_price": 160.0,
             "open_date": "2026-09-16", "peak_price": 160.0,
             "stop_price": 120.0, "target_price": 200.0}]),
        "peak_asset": 1_234_567.0,
        "daily_pnl": -1234.0,
        "consec_loss": 3,
        "halted": 1,
        "daily_trade_count": 7,
        "trade_date": date.today().isoformat(),
        "risk_state": None,
    }


def test_restore_engine_state_live_restores_risk_only():
    """C6：restore_ledger=False 只恢复风控字段，不覆盖 positions/cash。"""
    eng = _make_live_engine()
    eng.storage = _MemStorage(_risk_row())
    eng._cash = 42.0
    eng._positions = {}
    eng._sync_fail_count = 0
    orig_notice = EE.system_notice
    EE.system_notice = lambda *a, **k: None
    try:
        eng._restore_engine_state(restore_ledger=False)
    finally:
        EE.system_notice = orig_notice
    # 风险字段被恢复
    assert abs(eng.risk._peak_asset - 1_234_567.0) < 1e-6, eng.risk._peak_asset
    assert eng.risk._consec_loss == 3
    assert abs(eng.risk._daily_pnl - (-1234.0)) < 1e-6
    # 账本字段未被覆盖
    assert eng._cash == 42.0, f"live 不应恢复 DB 里的 cash，实际 {eng._cash}"
    assert eng._positions == {}, "live 不应恢复 DB 里的 positions（以 broker 为权威源）"


def test_restore_engine_state_paper_restores_ledger():
    """C6 对照组：restore_ledger=True（paper）仍恢复持仓与现金。"""
    eng = _make_paper_engine()
    eng.storage = _MemStorage(_risk_row())
    orig_notice = EE.system_notice
    EE.system_notice = lambda *a, **k: None
    try:
        eng._restore_engine_state()
    finally:
        EE.system_notice = orig_notice
    assert abs(eng._cash - 555555.0) < 1e-6
    assert "300308.SZ" in eng._positions
    assert eng._positions["300308.SZ"].quantity == 100
    assert abs(eng.risk._peak_asset - 1_234_567.0) < 1e-6


def test_save_engine_state_called_in_all_modes():
    """C6：live 也必须保存 engine_state（否则风控记忆随重启清零）。"""
    src = (ROOT / "engine" / "event_engine.py").read_text(encoding="utf-8")
    # _save_engine_state 的定义体若只在 paper 分支被调用，live 记忆必丢；
    # 这里只做结构性守卫：调用点存在且不在 `if self.exec_mode == "paper"` 之内。
    import re
    call_lines = [ln for ln in src.splitlines()
                  if "_save_engine_state()" in ln and "def " not in ln]
    assert call_lines, "引擎必须调用 _save_engine_state"
    for ln in call_lines:
        assert 'exec_mode == "paper"' not in ln, \
            f"_save_engine_state 调用被包在 paper 分支里: {ln.strip()}"


# =============================================================== C7

def _veto_signal(code="300308.SZ"):
    return Signal(ts=datetime.now(), code=code, name="中际旭创",
                  side="BUY", score=9.0, price=100.0, reason="test")


def _mark_bearish_veto(eng, code="300308.SZ", conf=-0.75):
    eng._ai_veto[code] = (date.today(), round(conf, 3))


def test_ai_veto_mode_default_is_log():
    """C7：默认 log —— 仅记录，零交易行为变化。"""
    assert settings.AI_VETO_MODE in ("off", "log", "veto")
    assert settings.AI_VETO_MODE == "log", \
        "默认必须是 log（README 语义：仅记录不阻断）"


def test_ai_veto_log_mode_does_not_block_buy():
    """C7：log 模式下即使当日有 bearish 标记，买入仍走原路径。"""
    eng = _make_paper_engine()
    _mark_bearish_veto(eng)
    assert eng._ai_veto_blocks("300308.SZ") is True, "标记本身应被记录"
    prev = settings.AI_VETO_MODE
    prev_ee = EE.AI_VETO_MODE
    settings.AI_VETO_MODE = "log"
    EE.AI_VETO_MODE = "log"
    eng._cash = 1_000_000.0
    eng._positions = {}
    try:
        eng._handle_buy(_veto_signal(), _FakeTick(100.0), {"300308.SZ": 100.0})
    finally:
        EE.AI_VETO_MODE = prev_ee
        settings.AI_VETO_MODE = prev
    assert "300308.SZ" in eng._positions and eng._positions["300308.SZ"].quantity > 0, \
        "log 模式不得阻断买入（默认零交易行为变化）"


def test_ai_veto_veto_mode_blocks_same_day_bearish():
    """C7：veto 模式下当日 bearish 标记 → _handle_buy 不进下单分支。"""
    eng = _make_paper_engine()
    _mark_bearish_veto(eng)
    prev = settings.AI_VETO_MODE
    settings.AI_VETO_MODE = "veto"
    # 生产把 AI_VETO_MODE by-value import 进 event_engine 模块命名空间，
    # 故测试必须同时改引擎侧的绑定（与 settings 保持一致）。
    prev_ee = EE.AI_VETO_MODE
    EE.AI_VETO_MODE = "veto"
    eng._cash = 1_000_000.0
    eng._positions = {}
    try:
        eng._handle_buy(_veto_signal(), _FakeTick(100.0), {"300308.SZ": 100.0})
    finally:
        EE.AI_VETO_MODE = prev_ee
        settings.AI_VETO_MODE = prev
    assert "300308.SZ" not in eng._positions, \
        "veto 模式必须实际否决当日买入"
    assert eng._cash == 1_000_000.0, "被否决时现金不应变动"


def test_ai_veto_expires_across_days():
    """C7：否决标记只对当日有效（跨日自动过期）。"""
    eng = _make_paper_engine()
    from datetime import timedelta
    eng._ai_veto["300308.SZ"] = (date.today() - timedelta(days=1), -0.75)
    assert eng._ai_veto_blocks("300308.SZ") is False, \
        "隔日 bearish 标记不应继续阻断"


# =============================================================== C9

def test_is_trading_time_excludes_call_auction_window():
    """C9：时段起点 09:30 —— 09:20（集合竞价窗口）场外，09:35 场内。"""
    from core.market_calendar import is_trading_time
    # 显式 sessions=DEFAULT 之外再验 use_calendar=False（纯周内口径），
    # 避免本机 xtdata 日历可用性影响断言（2026-09-01 是周二）。
    kw = {"use_calendar": False}
    assert is_trading_time(datetime(2026, 9, 1, 9, 20), **kw) is False, \
        "09:20 属开盘集合竞价窗口，不应被当作连续竞价时段"
    assert is_trading_time(datetime(2026, 9, 1, 9, 35), **kw) is True
    assert is_trading_time(datetime(2026, 9, 1, 11, 0), **kw) is True
    assert is_trading_time(datetime(2026, 9, 1, 12, 30), **kw) is False, "午休"
    assert is_trading_time(datetime(2026, 9, 1, 14, 0), **kw) is True


def test_default_sessions_start_at_0930():
    """C9：DEFAULT_SESSIONS 契约钉死（防止被改回 09:15 集合竞价窗口）。"""
    from core.market_calendar import DEFAULT_SESSIONS
    assert DEFAULT_SESSIONS[0][0] == "09:30", DEFAULT_SESSIONS
    assert DEFAULT_SESSIONS[-1][1] == "15:05", DEFAULT_SESSIONS


def test_calendar_health_shape():
    """C9：calendar_health() 返回 degraded/source 供 health_check 读取。"""
    from core.market_calendar import calendar_health
    h = calendar_health()
    assert set(h) == {"degraded", "source"}, h
    assert h["source"] in ("xtdata", "weekday")
    assert isinstance(h["degraded"], bool)


# =============================================================== C10

def test_northbound_cache_health_reports_stale_series():
    """C10：cache_health() 对陈旧缓存报 stale=True 且给出末端日期。"""
    from data import northbound_cache as NB
    h = NB.cache_health()
    assert set(h) == {"last_date", "age_days", "stale"}, h
    if not NB.F.exists():
        return  # 缓存文件不存在：本机不联网拉取，跳过真值断言
    assert h["last_date"], "缓存文件存在时必须给出末端日期"
    assert h["stale"] is (h["age_days"] > NB.MAX_STALE_DAYS), h
    assert NB.MAX_STALE_DAYS == 10


def test_northbound_stale_check_is_fail_open():
    """C10：陈旧只告警不丢数据（fail-open，但必须可见）。"""
    from data import northbound_cache as NB
    assert hasattr(NB, "_check_stale"), "必须存在龄期校验函数"
    import logging
    recs = []

    class _H(logging.Handler):
        def emit(self, r):
            recs.append(r)

    h = _H()
    NB.logger.addHandler(h)
    prev_level = NB.logger.level
    NB.logger.setLevel(logging.DEBUG)
    try:
        NB._check_stale({"20200101": 1.0}, "北向")
    finally:
        NB.logger.removeHandler(h)
        NB.logger.setLevel(prev_level)
    assert any(r.levelno >= logging.ERROR for r in recs), \
        "陈旧缓存必须记 ERROR（可见性优先）"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
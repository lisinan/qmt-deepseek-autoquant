# -*- coding: utf-8 -*-
"""risk/manager.py 单元测试。"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.data_models import Fill, Order, Position
from risk.manager import RiskManager


def _order(qty=100, price=10.0):
    return Order(ts=datetime.now(), code="x", side="BUY",
                 quantity=qty, price=price, order_type="limit", account="cash")


def test_can_open_basic():
    r = RiskManager()
    ok, reason = r.can_open(_order(100, 10.0), {}, total_asset=100000, daily_trade_count=0)
    assert ok, reason


def test_can_open_blocked_by_amount():
    r = RiskManager({"max_order_amount": 5000})
    ok, reason = r.can_open(_order(1000, 10.0), {}, total_asset=100000, daily_trade_count=0)
    assert not ok
    assert "amount" in reason


def test_can_open_blocked_by_position_pct():
    r = RiskManager({"max_single_position_pct": 0.1})
    # amount=10000 / total=100000 = 10%，刚刚好；10001 超
    ok, _ = r.can_open(_order(1001, 10.0), {}, total_asset=100000, daily_trade_count=0)
    assert not ok


def test_can_open_blocked_by_daily_trades():
    r = RiskManager({"max_daily_trades": 3})
    ok, reason = r.can_open(_order(100, 10.0), {}, total_asset=100000, daily_trade_count=3)
    assert not ok
    assert "daily" in reason


def test_consecutive_loss_scale():
    r = RiskManager({"max_consecutive_losses": 3})
    assert r.position_scale == 1.0
    # 连亏 1 次后还没到 threshold
    r.on_fill(Fill(ts=datetime.now(), code="x", side="SELL", quantity=1,
                   price=9.0, amount=9.0, account="cash"), avg_cost=10.0)
    assert r.position_scale == 1.0
    # 连亏 3 次 → 应该降仓
    for _ in range(2):
        r.on_fill(Fill(ts=datetime.now(), code="x", side="SELL", quantity=1,
                       price=9.0, amount=9.0, account="cash"), avg_cost=10.0)
    assert r.position_scale < 1.0


def test_consecutive_loss_resets_on_win():
    r = RiskManager({"max_consecutive_losses": 3})
    for _ in range(2):
        r.on_fill(Fill(ts=datetime.now(), code="x", side="SELL", quantity=1,
                       price=9.0, amount=9.0, account="cash"), avg_cost=10.0)
    assert r.consecutive_losses == 2
    # 一笔盈利
    r.on_fill(Fill(ts=datetime.now(), code="x", side="SELL", quantity=1,
                   price=11.0, amount=11.0, account="cash"), avg_cost=10.0)
    assert r.consecutive_losses == 0


def test_halt_on_daily_loss_pct():
    r = RiskManager({"daily_loss_limit_pct": -0.02})
    # total_asset=10000，-2% = -200。每笔亏 1 元 (cost 10 - price 9) × 1 股 = 1 元
    # 30 笔亏 30 元 = 0.3%，不到 2%；改用 500 笔亏 500 元 = 5% > 2% → halt
    for _ in range(500):
        r.on_fill(Fill(ts=datetime.now(), code="x", side="SELL", quantity=1,
                       price=9.0, amount=9.0, account="cash"),
                  avg_cost=10.0, total_asset=10000.0)
    assert r.is_halted


def test_halt_on_consecutive_losses():
    r = RiskManager({"max_consecutive_losses_halt": 5})
    for _ in range(5):
        r.on_fill(Fill(ts=datetime.now(), code="x", side="SELL", quantity=1,
                       price=9.0, amount=9.0, account="cash"), avg_cost=10.0)
    assert r.is_halted


def test_halt_on_max_drawdown():
    r = RiskManager({"max_drawdown_pct": -0.10})
    r.on_asset_update(100000)   # peak
    r.on_asset_update(89000)    # -11%
    assert r.is_halted


def test_resume():
    # 显式设定阈值（默认已放宽到 -0.25，避免与"自然回撤 ~-20%"误触发混淆）
    r = RiskManager({"max_drawdown_pct": -0.10})
    r.on_asset_update(100000)
    r.on_asset_update(80000)   # -20% → halt
    assert r.is_halted
    r.resume("manual")
    assert not r.is_halted


def test_snapshot():
    r = RiskManager()
    snap = r.snapshot()
    assert "halted" in snap
    assert "position_scale" in snap
    assert snap["position_scale"] == 1.0


def _sell_loss(r, n=1):
    for _ in range(n):
        r.on_fill(Fill(ts=datetime.now(), code="x", side="SELL", quantity=1,
                       price=9.0, amount=9.0, account="cash"), avg_cost=10.0)


def test_consec_loss_halt_auto_recovers():
    """2026-08-30 补丁：consec_loss 熔断不再是永久杀死开关，冷却后自动恢复。"""
    r = RiskManager({"max_consecutive_losses_halt": 5})
    _sell_loss(r, 5)
    assert r.is_halted, "连亏 5 次应熔断"
    assert r._halt_reason.startswith("consec_loss")
    # 同日不应恢复
    r._maybe_recover(date.today())
    assert r.is_halted, "冷却期未满不应恢复"
    # 冷却 1 日后自动恢复（通过每轮 can_open 也生效）
    r._halt_day = date.today() - timedelta(days=2)
    ok, _ = r.can_open(_order(100, 10.0), {}, total_asset=100000, daily_trade_count=0)
    assert ok, "冷却后空仓停牌引擎应能恢复开仓"
    assert not r.is_halted
    assert r.consecutive_losses == 0


def test_daily_loss_abs_halt_auto_recovers():
    """daily_loss_abs 熔断同样可冷却恢复，而非永久停牌。"""
    r = RiskManager({"daily_loss_limit_abs": 3})
    for _ in range(4):  # 4 笔各亏 1 元 → 累计 -4 > 3
        r.on_fill(Fill(ts=datetime.now(), code="x", side="SELL", quantity=1,
                       price=9.0, amount=9.0, account="cash"), avg_cost=10.0)
    assert r.is_halted
    assert r._halt_reason.startswith("daily_loss_abs")
    r._halt_day = date.today() - timedelta(days=2)
    r._maybe_recover(date.today())
    assert not r.is_halted


def test_drawdown_halt_recovers_after_dd_recover_days():
    """回撤熔断仍按 dd_recover_days 冷却恢复（既有行为不被破坏）。"""
    r = RiskManager({"max_drawdown_pct": -0.10, "dd_recover_days": 5})
    r.on_asset_update(100000)
    r.on_asset_update(89000)
    assert r.is_halted
    # 未到冷却期
    r._halt_day = date.today() - timedelta(days=4)
    r._maybe_recover(date.today(), total_asset=89000)
    assert r.is_halted
    # 满 5 日恢复
    r._halt_day = date.today() - timedelta(days=6)
    r._maybe_recover(date.today(), total_asset=90000)
    assert not r.is_halted
    assert r.consecutive_losses == 0
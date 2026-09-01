# -*- coding: utf-8 -*-
"""strategy/portfolio_strategy.py 单元测试。"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.data_models import Bar, Position, Signal
from strategy.portfolio_strategy import PortfolioStrategy
from strategy.trend_strategy import TrendStrategy


def _mk_sig(code: str, score: float = 5.0, price: float = 10.0) -> Signal:
    return Signal(ts=datetime.now(), code=code, name=code,
                  side="BUY", score=score, price=price, reason="test")


def _gen_bars(n: int, base: float = 10.0, slope: float = 0.05,
              noise: float = 0.015, seed: int = 42) -> list:
    import random
    rng = random.Random(seed)
    bars = []
    price = base
    start = datetime.now() - timedelta(days=n)
    for i in range(n):
        o = price
        c = o + slope + rng.gauss(0, noise)
        h = max(o, c) + abs(rng.gauss(0, noise * 0.5))
        l = min(o, c) - abs(rng.gauss(0, noise * 0.5))
        v = rng.randint(100000, 500000)
        bars.append(Bar(ts=start + timedelta(days=i),
                        open=o, high=h, low=l, close=c, volume=v))
        price = c
    return bars


def test_select_picks_top_n():
    p = PortfolioStrategy(max_positions=3)
    codes_bars = {}
    for i, slope in enumerate([0.10, 0.08, 0.12, -0.05, 0.02]):
        codes_bars[f"00000{i}.SZ"] = (f"stock{i}",
                                       _gen_bars(120, slope=slope))
    targets = p.select(codes_bars)
    assert len(targets) <= 3
    # Top-N 应该是上行 + 评分高的
    codes = [s.code for s in targets]
    assert "000002.SZ" in codes   # slope=0.12 最高


def test_select_excludes_warmup():
    p = PortfolioStrategy(max_positions=5)
    codes_bars = {
        "x.SZ": ("x", _gen_bars(120, slope=0.1)),  # 正常
        "y.SZ": ("y", _gen_bars(30, slope=0.1)),   # warmup
    }
    targets = p.select(codes_bars)
    codes = [s.code for s in targets]
    assert "x.SZ" in codes
    assert "y.SZ" not in codes


def test_select_excludes_index():
    p = PortfolioStrategy()
    codes_bars = {
        "000001.SH": ("上证", _gen_bars(120, slope=0.1)),
        "300308.SZ": ("中际", _gen_bars(120, slope=0.1)),
    }
    targets = p.select(codes_bars)
    codes = [s.code for s in targets]
    assert "000001.SH" not in codes


def test_plan_rebalance_sells_non_target():
    p = PortfolioStrategy(max_positions=5, max_single_pct=0.3,
                          cash_buffer_pct=0.0)
    targets = [_mk_sig("x.SZ", score=5.0, price=10.0)]
    positions = {
        "y.SZ": Position(code="y.SZ", name="y", quantity=100,
                         avg_cost=10.0, last_price=11.0),
    }
    orders = p.plan_rebalance(targets, positions,
                              current_prices={"x.SZ": 10.0, "y.SZ": 11.0},
                              cash=0, total_asset=100000)
    # 应该有 1 个 SELL（y）
    sides = [o.side for o in orders]
    assert "SELL" in sides


def test_plan_rebalance_buys_new_target():
    p = PortfolioStrategy(max_positions=5, max_single_pct=0.3,
                          cash_buffer_pct=0.0)
    targets = [_mk_sig("x.SZ", score=5.0, price=100.0)]
    orders = p.plan_rebalance(targets, {},
                              current_prices={"x.SZ": 100.0},
                              cash=50000, total_asset=100000)
    # 应该有 1 个 BUY
    buys = [o for o in orders if o.side == "BUY"]
    assert len(buys) == 1
    assert buys[0].code == "x.SZ"
    assert buys[0].quantity > 0
    # 整百股
    assert buys[0].quantity % 100 == 0


def test_plan_rebalance_respects_single_position_cap():
    p = PortfolioStrategy(max_positions=5, max_single_pct=0.20,
                          cash_buffer_pct=0.0)
    targets = [_mk_sig("x.SZ", score=5.0, price=100.0)]
    # cash=100000, total_asset=100000, cap=20% → max spend 20000
    # price=100 → qty=200 股
    orders = p.plan_rebalance(targets, {},
                              current_prices={"x.SZ": 100.0},
                              cash=100000, total_asset=100000)
    buys = [o for o in orders if o.side == "BUY"]
    assert len(buys) == 1
    assert buys[0].quantity * 100.0 <= 100000 * 0.20 + 100   # 单 cap


def test_plan_rebalance_skips_held_targets():
    """已在持仓的目标不动（不出 BUY 也不出 SELL）。"""
    p = PortfolioStrategy(max_positions=5, max_single_pct=0.3,
                          cash_buffer_pct=0.0)
    target = _mk_sig("x.SZ", score=5.0, price=10.0)
    positions = {
        "x.SZ": Position(code="x.SZ", name="x", quantity=100,
                         avg_cost=10.0, last_price=10.0),
    }
    orders = p.plan_rebalance([target], positions,
                              current_prices={"x.SZ": 10.0},
                              cash=0, total_asset=1000)
    assert orders == []   # 无操作


def test_evaluate_exit_delegates_to_trend():
    p = PortfolioStrategy(trend=TrendStrategy(params={"exit_mode": "scalp"}))
    pos = Position(code="300308.SZ", name="中际",
                   quantity=100, avg_cost=10.0, last_price=10.0,
                   open_date=datetime.now())
    bars = _gen_bars(80, slope=0.05)
    sig = p.evaluate_exit("300308.SZ", "中际", pos, 9.4, bars)
    assert sig is not None
    assert sig.side == "SELL"
    assert "止损" in sig.reason
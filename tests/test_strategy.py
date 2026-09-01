# -*- coding: utf-8 -*-
"""strategy/trend_strategy.py 单元测试（合成 K 线，无真实行情）。"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.data_models import Bar, Position
from strategy.trend_strategy import TrendStrategy


def _gen_bars(n: int, base: float = 10.0, slope: float = 0.05,
              noise: float = 0.02, seed: int = 42) -> list:
    """生成合成 K 线：线性趋势 + 高斯噪声。"""
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


def test_index_returns_hold():
    s = TrendStrategy()
    sig = s.on_bars("000001.SH", "上证指数", _gen_bars(120))
    assert sig.side == "HOLD"
    assert "index" in sig.reason


def test_warmup_returns_hold():
    s = TrendStrategy()
    sig = s.on_bars("300308.SZ", "中际旭创", _gen_bars(30))
    assert sig.side == "HOLD"
    assert "warmup" in sig.reason


def test_uptrend_buy_signal():
    s = TrendStrategy()
    sig = s.on_bars("300308.SZ", "中际旭创", _gen_bars(120, slope=0.1, noise=0.01))
    # 强上行趋势应该触发 BUY（至少评分 >= threshold）
    assert sig.side == "BUY", f"expected BUY, got {sig.side} (score={sig.score}, reason={sig.reason})"
    assert sig.score >= s.p["buy_score_threshold"]


def test_downtrend_hold_signal():
    s = TrendStrategy()
    sig = s.on_bars("300308.SZ", "中际旭创", _gen_bars(120, slope=-0.1, noise=0.01))
    assert sig.side in ("BUY", "HOLD")
    # 下行趋势通常不买入（评分应较低）
    assert sig.score < s.p["buy_score_threshold"] + 2


def test_exit_on_stop_loss():
    s = TrendStrategy(params={"exit_mode": "scalp"})  # 验证剥头皮退出语义
    pos = Position(code="300308.SZ", name="中际旭创",
                   quantity=100, avg_cost=10.0, last_price=10.0,
                   open_date=datetime.now())
    # 现价 -5% > -3% stop_loss → 止损
    bars = _gen_bars(80, slope=0.05)
    sig = s.on_exit("300308.SZ", pos, 9.4, bars)
    assert sig is not None
    assert sig.side == "SELL"
    assert "止损" in sig.reason


def test_exit_on_take_profit():
    s = TrendStrategy(params={"exit_mode": "scalp"})  # 验证剥头皮退出语义
    pos = Position(code="300308.SZ", name="中际旭创",
                   quantity=100, avg_cost=10.0, last_price=10.0,
                   open_date=datetime.now())
    bars = _gen_bars(80, slope=0.05)
    sig = s.on_exit("300308.SZ", pos, 11.5, bars)
    assert sig is not None
    assert sig.side == "SELL"
    assert "止盈" in sig.reason


def test_exit_on_hold_timeout():
    s = TrendStrategy(params={"exit_mode": "scalp"})  # 验证剥头皮退出语义
    pos = Position(code="300308.SZ", name="中际旭创",
                   quantity=100, avg_cost=10.0, last_price=10.05,
                   open_date=datetime.now() - timedelta(days=30))
    bars = _gen_bars(80, slope=0.05)
    sig = s.on_exit("300308.SZ", pos, 10.05, bars)
    assert sig is not None
    assert sig.side == "SELL"
    assert "超时" in sig.reason


def test_zero_position_no_exit():
    s = TrendStrategy()
    pos = Position(code="300308.SZ", name="x", quantity=0, avg_cost=10.0)
    bars = _gen_bars(80)
    sig = s.on_exit("300308.SZ", pos, 9.0, bars)
    assert sig is None
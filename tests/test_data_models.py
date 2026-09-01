# -*- coding: utf-8 -*-
"""core/data_models.py 单元测试。"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.data_models import Bar, Fill, Order, Position, Signal, Tick, AIAnalysis


def test_tick_validity():
    t = Tick(ts=datetime.now(), code="x", price=10.0)
    assert t.is_valid
    t.price = 0
    assert not t.is_valid


def test_position_pnl():
    p = Position(code="x", quantity=100, avg_cost=10.0, last_price=11.0)
    assert p.market_value == 1100.0
    assert p.cost_value == 1000.0
    assert p.pnl == 100.0
    assert abs(p.pnl_pct - 0.1) < 1e-6


def test_position_zero_cost():
    p = Position(code="x", quantity=100, avg_cost=0)
    assert p.pnl_pct == 0.0


def test_signal_factors_default():
    s = Signal(ts=datetime.now(), code="x", side="BUY", score=5.0, price=10.0)
    assert s.factors == {}
    assert s.ai_comment == ""


def test_bar_construction():
    p = Bar(ts=datetime.now(), open=1, high=2, low=0.5, close=1.5, volume=100)
    assert p.close == 1.5


def test_fill_construction():
    p = Fill(ts=datetime.now(), code="x", side="BUY",
             quantity=100, price=10.0, amount=1000.0)
    assert p.amount == 1000.0


def test_order_construction():
    p = Order(ts=datetime.now(), code="x", side="BUY", quantity=100, price=10.0)
    assert p.account == "cash"  # 默认


def test_ai_analysis_defaults():
    a = AIAnalysis(ts=datetime.now(), code="x", model="m")
    assert a.stance == "neutral"
    assert a.confidence == 0.0
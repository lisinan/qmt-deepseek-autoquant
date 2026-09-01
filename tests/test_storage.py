# -*- coding: utf-8 -*-
"""storage/db.py 单元测试。"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.data_models import AIAnalysis, Fill, Order, Signal
from storage.db import Storage


def _tmp_db():
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Path(p)


def test_init_creates_tables():
    db = _tmp_db()
    try:
        s = Storage(db)
        s.save_signal(Signal(ts=datetime.now(), code="x", side="BUY",
                              score=5.0, price=10.0, reason="t"))
        rows = s.get_signals()
        assert len(rows) >= 1
        assert rows[0]["code"] == "x"
    finally:
        s.close()
        db.unlink(missing_ok=True)


def test_save_and_query_signals():
    db = _tmp_db()
    try:
        s = Storage(db)
        for i in range(5):
            s.save_signal(Signal(ts=datetime.now(), code=f"x{i % 2}",
                                  side="BUY", score=float(i), price=10.0 + i,
                                  reason=f"r{i}", factors={"k": float(i)}))
        rows = s.get_signals(limit=10)
        assert len(rows) == 5
        # code 过滤
        rows2 = s.get_signals(limit=10, code="x0")
        assert len(rows2) == 3
    finally:
        s.close()
        db.unlink(missing_ok=True)


def test_save_and_query_fills():
    db = _tmp_db()
    try:
        s = Storage(db)
        s.save_fill(Fill(ts=datetime.now(), code="x", side="BUY",
                         quantity=100, price=10.0, amount=1000.0),
                    order_id="o1")
        rows = s.get_fills()
        assert len(rows) == 1
        assert rows[0]["order_id"] == "o1"
    finally:
        s.close()
        db.unlink(missing_ok=True)


def test_save_ai():
    db = _tmp_db()
    try:
        s = Storage(db)
        s.save_ai(AIAnalysis(ts=datetime.now(), code="x", model="m",
                              summary="test", stance="bullish",
                              confidence=0.5, risks="r"))
        rows = s.get_ai_analyses()
        assert len(rows) == 1
        assert rows[0]["stance"] == "bullish"
    finally:
        s.close()
        db.unlink(missing_ok=True)


def test_save_risk_snapshot():
    db = _tmp_db()
    s = Storage(db)
    sid = s.save_risk_snapshot({"halted": False, "daily_pnl": 100.0})
    assert sid >= 0
    s.close()


def test_first_equity_today():
    db = _tmp_db()
    s = Storage(db)
    # 空库返回 None
    assert s.first_equity_today() is None
    # 写入今日权益快照
    sid = s.save_equity_snapshot(1000000.0, 500000.0, 500000.0, 3, -0.5)
    assert sid >= 0
    base = s.first_equity_today()
    assert base is not None and abs(base - 1000000.0) < 1e-6
    s.close()
    db.unlink(missing_ok=True)


def test_save_and_load_engine_state():
    db = _tmp_db()
    s = Storage(db)
    assert s.load_engine_state() is None
    s.save_engine_state({
        "cash": 1000.0, "positions": "[{\"code\":\"x\"}]",
        "daily_trade_count": 2, "daily_pnl": 50.0, "consec_loss": 1,
        "peak_asset": 2000.0, "day_open_asset": 900.0, "tick_count": 10,
        "peak_equity": 2000.0, "trade_date": "2026-09-01",
    })
    row = s.load_engine_state()
    assert row is not None
    assert abs(row["cash"] - 1000.0) < 1e-6
    assert row["daily_trade_count"] == 2
    assert abs(row["day_open_asset"] - 900.0) < 1e-6
    s.close()
    db.unlink(missing_ok=True)


def test_save_order():
    db = _tmp_db()
    try:
        s = Storage(db)
        s.save_order(Order(ts=datetime.now(), code="x", side="BUY",
                            quantity=100, price=10.0), order_id="o1")
        # 没有专门的 orders get，跳过 query
    finally:
        s.close()
        db.unlink(missing_ok=True)


def test_save_order_fill_equity_with_mode():
    """paper→live 切换核实：订单/成交/权益快照均带 mode 标记，默认 paper。"""
    db = _tmp_db()
    try:
        s = Storage(db)
        s.save_order(Order(ts=datetime.now(), code="x", side="BUY",
                           quantity=100, price=10.0), order_id="o1", mode="live")
        s.save_fill(Fill(ts=datetime.now(), code="x", side="BUY",
                         quantity=100, price=10.0, amount=1000.0),
                    order_id="o1", mode="live")
        s.save_equity_snapshot(1000000.0, 500000.0, 500000.0, 3, -0.5,
                               mode="live")
        o = s.get_orders()[0]
        f = s.get_fills()[0]
        eq = s.get_equity_snapshots()[0]
        assert o["mode"] == "live", "订单应标记 mode=live"
        assert f["mode"] == "live", "成交应标记 mode=live"
        assert eq["mode"] == "live", "权益快照应标记 mode=live"

        # 默认（不传 mode）应为 paper，保证既有调用方向后兼容
        s.save_fill(Fill(ts=datetime.now(), code="y", side="SELL",
                         quantity=10, price=9.0, amount=90.0))
        assert s.get_fills(code="y")[0]["mode"] == "paper"

        # 大小写归一（LIVE → live）
        s.save_order(Order(ts=datetime.now(), code="z", side="BUY",
                           quantity=1, price=1.0), mode="LIVE")
        assert s.get_orders(code="z")[0]["mode"] == "live"
    finally:
        s.close()
        db.unlink(missing_ok=True)
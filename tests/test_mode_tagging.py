# -*- coding: utf-8 -*-
"""
回归测试：paper→live 切换后，订单/成交/权益快照均带执行模式标记(mode=paper/live)，
且引擎每次启动以系统提示固化当前模式，使盘后复盘(review_daily)能干净区分两段收益、
方便对账与核实。

用 FakeBroker 取代真实券商，无需 miniQMT / 网络。
"""
from __future__ import annotations

import sys
from collections import defaultdict
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


def _spy(eng):
    """用间谍替换 storage 写入，捕获每条记录的 mode。"""
    cap = defaultdict(list)

    def so(o, order_id=None, mode="paper"):
        cap["orders"].append(mode)

    def sf(f, order_id=None, mode="paper"):
        cap["fills"].append(mode)

    eng.storage.save_order = so
    eng.storage.save_fill = sf
    return cap


def _make_engine(exec_mode):
    eng = EE.EventEngine(
        exec_mode=exec_mode,
        auto_init_positions=False,
        enable_sector_scorer=False,
        enable_dynamic_universe=False,
        enable_llm_reranker=False,
    )
    # AIAnalyst.enabled 只读：用轻量桩替换，避免触发 AI 线程
    eng.analyst = type("FakeAnalyst", (), {"enabled": False})()
    return eng


def test_live_buy_and_sell_tagged_live():
    eng = _make_engine("live")
    fake = FakeBroker()
    orig = EE.qmt_broker
    EE.qmt_broker = fake
    try:
        cap = _spy(eng)
        sig = Signal(ts=datetime.now(), code="300308.SZ", name="中际旭创",
                     side="BUY", score=5.0, price=100.0, reason="test")
        eng._handle_buy(sig, _FakeTick(100.0, "中际旭创"), {"300308.SZ": 100.0})
        assert cap["orders"] == ["live"], "live 买入下单应标记 mode=live"
        pos = eng._positions["300308.SZ"]
        sig2 = Signal(ts=datetime.now(), code="300308.SZ", name="中际旭创",
                      side="SELL", price=110.0, reason="test")
        eng._handle_sell(sig2, pos)
        assert cap["orders"].count("live") == 2, "live 卖出下单应标记 mode=live"
    finally:
        EE.qmt_broker = orig


def test_paper_buy_and_sell_tagged_paper():
    eng = _make_engine("paper")
    try:
        cap = _spy(eng)
        sig = Signal(ts=datetime.now(), code="300308.SZ", name="中际旭创",
                     side="BUY", score=5.0, price=100.0, reason="test")
        eng._handle_buy(sig, _FakeTick(100.0, "中际旭创"), {"300308.SZ": 100.0})
        assert cap["orders"] == ["paper"], "paper 买入下单应标记 mode=paper"
        assert cap["fills"] == ["paper"], "paper 买入成交应标记 mode=paper"
        pos = eng._positions["300308.SZ"]
        sig2 = Signal(ts=datetime.now(), code="300308.SZ", name="中际旭创",
                      side="SELL", price=110.0, reason="test")
        eng._handle_sell(sig2, pos)
        assert cap["orders"].count("paper") == 2
        assert cap["fills"].count("paper") == 2
    finally:
        pass


def test_poll_records_fill_in_live_mode():
    eng = _make_engine("live")
    fake = FakeBroker()
    # 构造一笔已成交 trade，使 _poll_and_record_fill 命中并落库
    fake._trades = [{"order_id": "ord-1", "traded_price": 105.0,
                     "traded_volume": 50}]
    orig = EE.qmt_broker
    EE.qmt_broker = fake
    try:
        cap = _spy(eng)
        eng._poll_and_record_fill("ord-1", "300308.SZ", "BUY", 50, 105.0, "cash")
        assert cap["fills"] == ["live"], "实盘成交回报应标记 mode=live"
    finally:
        EE.qmt_broker = orig


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

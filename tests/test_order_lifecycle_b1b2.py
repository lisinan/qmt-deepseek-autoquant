# -*- coding: utf-8 -*-
"""B1（live 委托生命周期）+ B2（卖出侧保护）验收守卫。

对应契约 docs/OPTIMIZATION_SPEC_B1B2_2026-10-21.md §3 的 8 条：

  1. _market_stale=True 时 _handle_sell 返回 False 且不调用 place_order；
  2. live 卖单提交后本地 quantity 不归零，同 code 再次卖出被拒（pending_sell）；
  3. available=300 / quantity=1000 → 报单量 300；available=0 且非 T+1 → 整仓并 WARN；
  4. 同一 oid 分两笔成交（400+600）→ filled=1000、save_fill×2、risk.on_fill×2；
  5. 超时未成交 → cancel_order_stock 一次；BUY 回滚未成交数量，SELL 数量不变；
  6. 强平同一天内对同一 code 最多重试 3 次（_flatten_attempts）；
  7. paper 跨日后 pos.available == pos.quantity；
  8. 既有相关测试保持通过（由 tests/run_all.py 的 mods 清单统一执行）。

全部离线：FakeBroker 取代真实券商（不连 miniQMT / 不触网），内存 storage 桩
记录 save_fill / save_order 调用，risk.on_fill 用记录器包裹。
"""
from __future__ import annotations

import contextlib
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 【2026-10-22 事故修复】本文件此前只在 run_all.py 下跑（那里已设 QMT_DB），
# 一旦被**单独 import**（调试时常这么干）就会用真实 Storage 写生产库 ——
# 实测 2026-10-21 22:29 的 600519.SH / order_id="o1" / price=100.0 三笔伪造成交
# 即由此产生（已由 scripts/repair_test_pollution.py 清除）。
# 因此每个会构造真实 Storage 的测试模块都必须在 import 项目模块**之前**自设隔离。
_TMP = Path(tempfile.mkdtemp(prefix="qmt_b1b2_"))
os.environ.setdefault("QMT_DB", str(_TMP / "b1b2_qmt.db"))
os.environ.setdefault("QMT_NOTICES_LOG", str(_TMP / "b1b2_notices.log"))

from core.data_models import Position, Signal          # noqa: E402
import engine.event_engine as EE                        # noqa: E402


# --------------------------------------------------------------- 桩

class _FakeTick:
    def __init__(self, price, name="X"):
        self.price = price
        self.name = name


class _FakeAnalyst:
    enabled = False


class FakeBroker:
    """记录型券商桩：place_order / cancel_order_stock 全部记账。"""

    def __init__(self):
        self.connected = True
        self._positions = []
        self._asset = {"cash": 840000.0, "total_asset": 856000.0}
        self._trades = []
        self.place_calls = []          # [(code, side, qty, price, account)]
        self.cancel_calls = []         # [(account, oid)]
        self._seq = 0

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
        self._seq += 1
        self.place_calls.append((code, side, int(qty), price, account))
        return {"ok": True, "order_id": f"ord-{self._seq}"}

    def cancel_order_stock(self, account, order_id):
        self.cancel_calls.append((account, order_id))
        return {"ok": True, "reason": "已撤单"}


class MemStorage:
    """内存 storage 桩：只记录 save_order / save_fill 调用。"""

    def __init__(self):
        self.orders = []
        self.fills = []

    def save_order(self, order, order_id=None, mode="paper"):
        self.orders.append((order, order_id, mode))

    def save_fill(self, fill, order_id=None, mode="paper"):
        self.fills.append((fill, order_id, mode))

    def save_signal(self, *a, **k):
        return 1

    def save_equity_snapshot(self, *a, **k):
        return 1


class _LogCatcher(logging.Handler):
    """收集指定 logger 的记录（用于断言 WARN 告警）。"""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@contextlib.contextmanager
def _quiet_notices():
    """屏蔽 system_notice（不往 notices.log 写测试噪音）。"""
    with patch.object(EE, "system_notice", lambda *a, **k: None):
        yield


def _make_engine(mode="live"):
    eng = EE.EventEngine(
        exec_mode=mode,
        auto_init_positions=False,
        enable_sector_scorer=False,
        enable_dynamic_universe=False,
        enable_llm_reranker=False,
    )
    eng.analyst = _FakeAnalyst()
    # 合成行情闸门：data_mode 必须是 xtdata，否则 mock 闸门会拦掉所有成交，
    # 使本文件无法验证 B1/B2 的 live 语义。
    eng.data_mode = "xtdata"
    eng.allow_mock_trading = False
    eng.storage = MemStorage()
    eng._cash = 840000.0
    eng._positions = {}
    eng._pos_meta = {}
    return eng


def _old_position(code="300308.SZ", qty=1000, avail=0, avg=150.0, last=160.0):
    """老仓（7 天前建仓）⇒ 非 T+1、也不在建仓保护窗内。"""
    return Position(
        code=code, name="测试股", quantity=qty, available=avail,
        avg_cost=avg, last_price=last,
        open_date=datetime.now() - timedelta(days=7),
        peak_price=last, stop_price=avg * 0.8, target_price=avg * 1.5,
    )


def _sell_sig(code="300308.SZ", price=160.0):
    return Signal(ts=datetime.now(), code=code, name="测试股",
                  side="SELL", score=9.0, price=price, reason="test_sell")


def _record_on_fill(eng):
    """把 risk.on_fill 换成记录器（返回 (fill, avg_cost, total_asset) 列表）。"""
    calls = []
    eng.risk.on_fill = lambda fill, avg_cost=0.0, total_asset=0.0: \
        calls.append((fill, avg_cost, total_asset))
    return calls


# ============================================================== 1. 行情陈旧守卫

def test_handle_sell_returns_false_and_no_order_when_market_stale():
    """§3-1：_market_stale=True ⇒ _handle_sell 返回 False 且不调 place_order。"""
    eng = _make_engine("live")
    broker = FakeBroker()
    pos = _old_position()
    eng._positions[pos.code] = pos
    eng._market_stale = True
    with _quiet_notices(), patch.object(EE, "qmt_broker", broker):
        ok = eng._handle_sell(_sell_sig(), pos)
    assert ok is False, "行情陈旧时卖出必须被拦截"
    assert broker.place_calls == [], \
        f"行情陈旧时不得下单，实际 {broker.place_calls}"
    assert pos.quantity == 1000, "被拦截的卖出不得改变持仓数量"
    assert eng._last_reject.get(pos.code) == "stale", eng._last_reject
    assert pos.code not in eng._pending_sells, \
        "被拦截的卖出不得登记待成交卖单"


# ============================================================== 2. 卖单不归零 + 去重

def test_live_sell_keeps_quantity_and_blocks_duplicate_sell():
    """§3-2：live 卖单提交后本地 quantity 不归零；同 code 再次卖出被拒。"""
    eng = _make_engine("live")
    broker = FakeBroker()
    pos = _old_position(qty=1000, avail=1000)
    eng._positions[pos.code] = pos
    with _quiet_notices(), patch.object(EE, "qmt_broker", broker):
        first = eng._handle_sell(_sell_sig(), pos)
        assert first is False, \
            "委托已提交但未成交 ⇒ 槽位未释放 ⇒ 必须返回 False"
        assert len(broker.place_calls) == 1, broker.place_calls
        assert pos.quantity == 1000, \
            f"live 卖单提交后本地数量不得归零，实际 {pos.quantity}"
        assert eng._pending_sells.get(pos.code), \
            "已提交的卖单必须登记在 _pending_sells（供去重）"
        oid = eng._pending_sells[pos.code]
        assert oid in eng._pending_orders, eng._pending_orders

        # 同一 code 再次卖出 → 被 pending_sell 拒绝
        second = eng._handle_sell(_sell_sig(), pos)
        assert second is False, "重复卖出必须被拒"
        assert len(broker.place_calls) == 1, \
            f"重复卖出不得再次下单，实际 {broker.place_calls}"
        assert eng._last_reject.get(pos.code) == "pending_sell", eng._last_reject


# ============================================================== 3. 卖出量夹紧

def test_live_sell_quantity_clamped_to_available():
    """§3-3a：available=300 / quantity=1000 ⇒ 报单量 300。"""
    eng = _make_engine("live")
    broker = FakeBroker()
    pos = _old_position(qty=1000, avail=300)
    eng._positions[pos.code] = pos
    with _quiet_notices(), patch.object(EE, "qmt_broker", broker):
        eng._handle_sell(_sell_sig(), pos)
    assert len(broker.place_calls) == 1, broker.place_calls
    _code, side, qty, price, _account = broker.place_calls[0]
    assert side == "SELL"
    assert qty == 300, f"报单量应夹紧到 available=300，实际 {qty}"
    assert price == 160.0, price


def test_live_sell_available_zero_submits_full_position_with_warning():
    """§3-3b：available=0 且非 T+1 ⇒ 仍按整仓提交，并发一次 WARN。"""
    eng = _make_engine("live")
    broker = FakeBroker()
    pos = _old_position(qty=1000, avail=0)
    eng._positions[pos.code] = pos
    catcher = _LogCatcher()
    prev_level = EE.logger.level
    EE.logger.setLevel(logging.WARNING)
    EE.logger.addHandler(catcher)
    try:
        with _quiet_notices(), patch.object(EE, "qmt_broker", broker):
            eng._handle_sell(_sell_sig(), pos)
    finally:
        EE.logger.removeHandler(catcher)
        EE.logger.setLevel(prev_level)
    assert len(broker.place_calls) == 1, broker.place_calls
    _c, _s, qty, _p, _a = broker.place_calls[0]
    assert qty == 1000, f"available=0 时应按整仓 1000 提交，实际 {qty}"
    msgs = [r.getMessage() for r in catcher.records]
    assert any("可用量字段为 0" in m for m in msgs), \
        f"available=0 必须告警，实际日志 {msgs}"


def test_live_sell_available_zero_warns_only_once_per_code_per_day():
    """§3-3b 附加：available=0 的 WARN 每个 code 每日只发一次。"""
    eng = _make_engine("live")
    broker = FakeBroker()
    pos = _old_position(qty=1000, avail=0)
    eng._positions[pos.code] = pos
    catcher = _LogCatcher()
    prev_level = EE.logger.level
    EE.logger.setLevel(logging.WARNING)
    EE.logger.addHandler(catcher)
    try:
        with _quiet_notices(), patch.object(EE, "qmt_broker", broker):
            eng._handle_sell(_sell_sig(), pos)
            # 撤掉待成交去重标记，模拟下一轮再次尝试卖出同一只
            eng._pending_sells.pop(pos.code, None)
            eng._handle_sell(_sell_sig(), pos)
    finally:
        EE.logger.removeHandler(catcher)
        EE.logger.setLevel(prev_level)
    warns = [r.getMessage() for r in catcher.records
             if "可用量字段为 0" in r.getMessage()]
    assert len(warns) == 1, \
        f"同一 code 当日只应告警一次，实际 {len(warns)} 次"


# ============================================================== 4. 分笔成交

def test_reconcile_applies_two_partial_fills_for_same_oid():
    """§3-4：同一 oid 两笔成交（400+600）⇒ filled=1000 / save_fill×2 / on_fill×2。"""
    eng = _make_engine("live")
    broker = FakeBroker()
    pos = _old_position(qty=1000, avail=1000)
    eng._positions[pos.code] = pos
    fills = _record_on_fill(eng)
    eng._register_pending_order("oid-1", pos.code, "SELL", 1000, 160.0,
                                pos.avg_cost, "cash")
    broker._trades = [
        {"order_id": "oid-1", "traded_time": "2026-10-21 10:00:00",
         "traded_volume": 400, "traded_price": 160.0},
        {"order_id": "oid-1", "traded_time": "2026-10-21 10:00:05",
         "traded_volume": 600, "traded_price": 160.5},
    ]
    with _quiet_notices(), patch.object(EE, "qmt_broker", broker):
        eng._reconcile_pending_orders()
    po = eng._pending_orders.get("oid-1")
    assert po is None, f"成交满额后应从待成交表移除，实际 {po}"
    assert len(eng.storage.fills) == 2, \
        f"两笔成交应落库两次，实际 {len(eng.storage.fills)}"
    assert len(fills) == 2, f"risk.on_fill 应被调两次，实际 {len(fills)}"
    for f, _avg, _ta in fills:
        assert f.side == "SELL", f.side
    assert abs(sum(f.quantity for f, _a, _t in fills) - 1000) < 1e-9
    # 满额成交 → 本地数量扣减 + 释放待成交槽位
    assert pos.quantity == 0, f"满额成交后应清零持仓，实际 {pos.quantity}"
    assert pos.code not in eng._pending_sells


def test_reconcile_ignores_duplicate_fill_report():
    """§3-4 附加：同一 (oid, traded_time, traded_volume) 回报只落实一次。"""
    eng = _make_engine("live")
    broker = FakeBroker()
    pos = _old_position(qty=1000, avail=1000)
    eng._positions[pos.code] = pos
    fills = _record_on_fill(eng)
    eng._register_pending_order("oid-2", pos.code, "SELL", 1000, 160.0,
                                pos.avg_cost, "cash")
    trade = {"order_id": "oid-2", "traded_time": "2026-10-21 10:00:00",
             "traded_volume": 400, "traded_price": 160.0}
    broker._trades = [trade, dict(trade)]
    with _quiet_notices(), patch.object(EE, "qmt_broker", broker):
        eng._reconcile_pending_orders()
    assert len(eng.storage.fills) == 1, \
        f"重复回报只应落库一次，实际 {len(eng.storage.fills)}"
    assert len(fills) == 1, f"重复回报只应计盈亏一次，实际 {len(fills)}"


# ============================================================== 5. 超时撤单

def test_reconcile_timeout_cancels_and_rolls_back_buy():
    """§3-5a：BUY 超时未成交 ⇒ cancel_order_stock 一次 + 回滚未成交数量。"""
    eng = _make_engine("live")
    broker = FakeBroker()
    code = "600519.SH"
    pos = _old_position(code=code, qty=1000, avail=1000, avg=100.0, last=100.0)
    eng._positions[code] = pos
    eng._pos_meta[code] = {"stop_price": 80.0}
    eng._register_pending_order("oid-buy", code, "BUY", 1000, 100.0,
                                100.0, "cash")
    po = eng._pending_orders["oid-buy"]
    po["filled"] = 200            # 部分成交
    po["ts"] = time.time() - 200  # 远超 BUY 超时阈值(90s)
    broker._trades = []
    with _quiet_notices(), patch.object(EE, "qmt_broker", broker):
        eng._reconcile_pending_orders()
    assert len(broker.cancel_calls) == 1, \
        f"超时必须撤单一次，实际 {broker.cancel_calls}"
    assert broker.cancel_calls[0] == ("cash", "oid-buy"), broker.cancel_calls
    assert "oid-buy" not in eng._pending_orders
    # 未成交 800 股回滚：1000 - 800 = 200
    assert pos.quantity == 200, \
        f"BUY 未成交部分应回滚，实际 {pos.quantity}"


def test_reconcile_buy_timeout_after_sync_keeps_filled_quantity():
    """回归（WP-H 发现的 P0）：上一轮 broker 同步已把 quantity 校正为「已成交量」，
    超时回滚**不得**再减「未成交量」（200 − 800 = −600 ⇒ 持仓被清空、_pos_meta 里的
    open_date/止损价丢失 ⇒ T+1 与止损同时失效）。"""
    eng = _make_engine("live")
    broker = FakeBroker()
    code = "600519.SH"
    pos = _old_position(code=code, qty=200, avail=200, avg=100.0, last=100.0)
    eng._positions[code] = pos
    eng._pos_meta[code] = {"stop_price": 80.0, "open_date": datetime.now()}
    eng._register_pending_order("oid-buy2", code, "BUY", 1000, 100.0,
                                100.0, "cash")
    po = eng._pending_orders["oid-buy2"]
    po["filled"] = 200
    po["ts"] = time.time() - 200
    broker._trades = []
    with _quiet_notices(), patch.object(EE, "qmt_broker", broker):
        eng._reconcile_pending_orders()
    assert len(broker.cancel_calls) == 1, broker.cancel_calls
    assert code in eng._positions, "已成交的 200 股不得被清仓"
    assert eng._positions[code].quantity == 200, eng._positions[code].quantity
    assert code in eng._pos_meta, "_pos_meta 必须保留（open_date / 止损价）"


def test_reconcile_timeout_sell_keeps_quantity():
    """§3-5b：SELL 超时未成交 ⇒ 撤单一次，持仓数量不变，仅释放去重槽位。"""
    eng = _make_engine("live")
    broker = FakeBroker()
    pos = _old_position(qty=1000, avail=1000)
    eng._positions[pos.code] = pos
    eng._register_pending_order("oid-sell", pos.code, "SELL", 1000, 160.0,
                                pos.avg_cost, "cash")
    eng._pending_orders["oid-sell"]["ts"] = time.time() - 200  # 超 SELL 阈值 30s
    broker._trades = []
    with _quiet_notices(), patch.object(EE, "qmt_broker", broker):
        eng._reconcile_pending_orders()
    assert len(broker.cancel_calls) == 1, broker.cancel_calls
    assert pos.quantity == 1000, \
        f"SELL 未成交即未卖出，数量应不变，实际 {pos.quantity}"
    assert pos.code not in eng._pending_sells, \
        "撤单后必须释放待成交去重槽位"
    assert "oid-sell" not in eng._pending_orders


# ============================================================== 6. 强平重试上限

class _StubClient:
    """qmt_client 桩：get_ticks 返回固定价，mode=xtdata。"""
    mode = "xtdata"

    def __init__(self, raw):
        self._raw = raw

    def get_ticks(self, codes):
        return self._raw


def _prepare_flatten_engine(eng, code, price=10.0):
    """把 _run_once 的重活替换成无操作桩，只保留强平块的行为。"""
    eng._market_stale = False
    eng._update_market_staleness = lambda raw: None
    eng._aggregate_bar = lambda *a, **k: None
    eng._reconcile_pending_orders = lambda: None
    eng._sync_broker_positions = lambda: None
    eng._refresh_daily_async = lambda *a, **k: None
    eng._update_position_marks = lambda ticks: None
    eng._persist_equity = lambda *a, **k: None
    eng._maybe_save_risk_snapshot = lambda: None
    eng._run_single_step = lambda ticks: None
    eng._daily_fallback_exit = lambda *a, **k: None
    eng.risk.on_asset_update = lambda *a, **k: None
    eng.risk._flatten_requested = True
    eng._last_daily_date = datetime.now().date()
    raw = {code: {"lastPrice": price, "lastClose": price, "open": price,
                  "high": price, "low": price, "volume": 1000,
                  "amount": price * 1000}}
    return _StubClient(raw)


def test_flatten_retries_same_code_at_most_three_times_per_day():
    """§3-6：强平同一天内对同一 code 最多重试 3 次（_flatten_attempts）。"""
    eng = _make_engine("live")
    code = "300308.SZ"
    pos = _old_position(code=code, qty=1000, avail=1000, avg=10.0, last=10.0)
    eng._positions[code] = pos
    calls = []
    eng._handle_sell = lambda sig, p, now=None, force=False: \
        calls.append((sig, p, force))
    stub = _prepare_flatten_engine(eng, code)
    assert eng.FLATTEN_MAX_ATTEMPTS == 3, eng.FLATTEN_MAX_ATTEMPTS
    with _quiet_notices(), patch.object(EE, "qmt_client", stub):
        for _ in range(5):
            eng._run_once([code])
    assert len(calls) == 3, \
        f"同一 code 当日强平最多 3 次，实际 {len(calls)} 次"
    assert eng._flatten_attempts.get(code) == 3, eng._flatten_attempts
    assert all(c[2] is True for c in calls), \
        "强平必须以 force=True 提交（绕过建仓保护期）"
    assert all(c[0].reason == "daily_hard_stop_flatten" for c in calls), \
        [c[0].reason for c in calls]


def test_flatten_attempts_reset_on_new_day():
    """§3-6 附加：跨日重置强平尝试计数（_flatten_day / _flatten_attempts）。"""
    eng = _make_engine("live")
    code = "300308.SZ"
    eng._positions[code] = _old_position(code=code, qty=1000, avail=1000,
                                         avg=10.0, last=10.0)
    eng._flatten_attempts = {code: 3}
    eng._flatten_day = "2000-01-01"
    eng._flatten_notified = True
    eng._trade_date = datetime.now().date() - timedelta(days=1)
    eng._reset_daily_if_needed()
    assert eng._flatten_attempts == {}, eng._flatten_attempts
    assert eng._flatten_day == "", eng._flatten_day
    assert eng._flatten_notified is False


# ============================================================== 7. paper 跨日 available

def test_paper_cross_day_resets_available_to_quantity():
    """§3-7：paper 跨日后 pos.available == pos.quantity。"""
    eng = _make_engine("paper")
    code = "300308.SZ"
    pos = _old_position(code=code, qty=1000, avail=0)
    eng._positions[code] = pos
    eng._trade_date = datetime.now().date() - timedelta(days=1)
    eng._reset_daily_if_needed()
    assert pos.available == pos.quantity == 1000, \
        f"paper 跨日应恢复可卖量，实际 available={pos.available}"


# ============================================================== 8. 既有测试

def test_legacy_lifecycle_modules_importable():
    """§3-8：既有相关测试模块必须仍可导入（不因本次改动而消失）。"""
    import importlib
    for m in ("tests.test_event_engine_live", "tests.test_t1_sell_guard",
              "tests.test_daily_fallback_exit"):
        importlib.import_module(m)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

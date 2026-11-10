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


def _loss_fill(qty=1, price=9.0):
    return Fill(ts=datetime.now(), code="x", side="SELL", quantity=qty,
                price=price, amount=price * qty, account="cash")


def _feed_losses_on_distinct_days(r, n, qty=1, price=9.0):
    """喂 n 笔亏损卖出，每笔落在**不同交易日**。

    【2026-09-24 测试与生产解耦】生产改为「同批退出合并计数」后，同一交易日的
    多笔亏损 SELL 只计 1 次连亏（见 tests/test_risk_consec_batch.py）。本文件
    的原意是验证**阶梯降仓 / halt 机制本身**，与计数粒度无关，故显式把每笔
    亏损放到不同交易日，保住原有断言不放宽。
    """
    for k in range(n):
        r._last_loss_day = date.today() - timedelta(days=(n - k))
        r.on_fill(_loss_fill(qty, price), avg_cost=10.0)
    return r


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
    # 连亏 1 次后还没到 threshold（不同交易日，见 _feed_losses_on_distinct_days）
    _feed_losses_on_distinct_days(r, 1)
    assert r.position_scale == 1.0
    # 连亏 3 次 → 应该降仓
    _feed_losses_on_distinct_days(r, 2)
    assert r.position_scale < 1.0


def test_consecutive_loss_resets_on_win():
    r = RiskManager({"max_consecutive_losses": 3})
    _feed_losses_on_distinct_days(r, 2)
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
    _feed_losses_on_distinct_days(r, 5)
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
    """喂 n 笔亏损卖出。**每笔落在不同交易日**（同日合并计数的解耦，见文件头）。"""
    _feed_losses_on_distinct_days(r, n)


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


def test_daily_hard_stop_flatten_triggers():
    """2026-09-16 优化：当日账户亏损（相对开盘）达 daily_stop_flatten_pct
    即置 flatten_requested 并停牌，引擎据此强平全部可卖持仓。

    【2026-11-10 P0】改为**连续 N 轮确认**（`DAILY_STOP_CONFIRM_ROUNDS`）：
    单轮瞬态读数会因「买入在途、资金已扣持仓未刷新」而误判（实盘 14:17 事故：
    读出 −7.04% 而真实 −1.7%，直接置 flatten 并空转强平到收盘）。
    本用例仍断言「**持续** −7% 必须强平」——保护没有放松，只是要求越线不是瞬态。
    """
    r = RiskManager({"daily_stop_flatten_pct": -0.06})
    # 开盘 100000，跌到 93000（-7% <= -6%）→ 连续 N 轮后应触发强平+停牌
    for _ in range(r.DAILY_STOP_CONFIRM_ROUNDS):
        r.on_asset_update(93000, day_open_asset=100000)
    assert r.flatten_requested, "持续 −7% 应触发日内硬止损强平"
    assert r.is_halted, "日内硬止损应同时暂停新开仓"


def test_daily_hard_stop_single_transient_reading_does_not_flatten():
    """★ 反向断言（2026-11-10 实盘事故）：单轮瞬态越线**不得**强平。

    14:17:21 买 510500 500 股（3,347 元）→ 14:17:36 资产更新少算一个持仓
    → 读出 daily_stop −7.04%（真实 −1.7%）→ 假熔断并空转强平。
    """
    r = RiskManager({"daily_stop_flatten_pct": -0.06})
    r.on_asset_update(93000, day_open_asset=100000)      # 单轮瞬态
    assert not r.flatten_requested, "单轮瞬态读数就置了强平标志 ⇒ 假强平复发"
    r.on_asset_update(99500, day_open_asset=100000)      # 恢复 → 计数清零
    r.on_asset_update(93000, day_open_asset=100000)      # 再来一次瞬态
    assert not r.flatten_requested, "计数未随恢复清零 ⇒ 零散瞬态会被累加成真熔断"



def test_daily_hard_stop_no_trigger_in_normal_drawdown():
    """正常波动（-3%）不应触发强平，避免被趋势股日内噪音误伤。"""
    r = RiskManager({"daily_stop_flatten_pct": -0.06})
    r.on_asset_update(97000, day_open_asset=100000)   # -3%
    assert not r.flatten_requested
    assert not r.is_halted


def test_daily_hard_stop_flatten_reset_on_resume():
    """手动 resume 应同时清除 flatten_requested（恢复后不再强平）。"""
    r = RiskManager({"daily_stop_flatten_pct": -0.06})
    for _ in range(r.DAILY_STOP_CONFIRM_ROUNDS):      # 连续确认后才触发
        r.on_asset_update(93000, day_open_asset=100000)
    assert r.flatten_requested
    r.resume("manual")
    assert not r.flatten_requested
    assert not r.is_halted


def test_daily_hard_stop_threshold_default():
    """默认阈值存在且为 -0.06（与 settings.RISK_PARAMS 对齐）。"""
    r = RiskManager()
    assert r.p.get("daily_stop_flatten_pct") == -0.06
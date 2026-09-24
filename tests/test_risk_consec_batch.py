# -*- coding: utf-8 -*-
"""「同批退出合并计数」回归测试（2026-09-24 AM-EVOLVE 第 6 轮 P0 缺陷修复）。

缺陷本体
--------
RiskManager.on_fill 对**每笔**亏损 SELL 独立执行 `_consec_loss += 1`。
但「连续亏损」的语义是「连续多次**独立**判断失误」，而本组合 5 只标的同属
AI 产业链、高度同向 —— 板块回调时全部仓位在同一时刻一起破位离场，on_fill
在几毫秒内被连续调用 N 次 ⇒ **1 次板块级事件被计成 N 次连亏**。

实盘铁证（2026-09-24，storage/qmt.db.fills）
-------------------------------------------
    350  2026-09-24T10:00:00.662591  300502.SZ  SELL  80   449.22
    351  2026-09-24T10:00:00.664592  603986.SH  SELL  80   388.66
    352  2026-09-24T10:00:00.664592  688008.SH  SELL  160  218.24
    353  2026-09-24T10:00:00.665591  688012.SH  SELL  80   344.00
4 笔相隔 **3 毫秒** ⇒ _consec_loss 由 3 直接跳到 7，瞬间越过 halt 阈值 5
⇒ halted=True、position_scale=0.0，账户自 10:00 起整个下午盘（含观察篮）
完全丧失开仓能力（risk_snapshots 11:25 仍为 scale=0.0 / halted=True）。

本文件锁定修复语义：
  1. 同一交易日内多笔亏损 SELL **只计 1 次**连亏（防「1 次板块回调 = N 次失误」）；
  2. 盈利卖出照旧**立即**归零连亏（保护语义未被削弱）；
  3. 跨交易日后可再次计数（连亏保护链路完整保留）；
  4. 连续多个交易日亏损仍能推进到 halt（不是把保护关掉）；
  5. `last_loss_day` 随 engine_state 持久化（重启后当日不得再计一次）。
"""
from __future__ import annotations

import sys
from datetime import date, timedelta

sys.path.insert(0, ".")

from core.data_models import Fill  # noqa: E402
from risk.manager import RiskManager  # noqa: E402


def _loss(code: str = "300502.SZ") -> Fill:
    """构造一笔亏损卖出：成本 100，卖出价 90（每 100 股亏 1000 元）。"""
    return Fill(ts="2026-09-24T10:00:00", code=code, side="SELL",
                quantity=100, price=90.0, amount=9000.0)


def _win(code: str = "300502.SZ") -> Fill:
    """构造一笔盈利卖出：成本 100，卖出价 110。"""
    return Fill(ts="2026-09-24T10:00:00", code=code, side="SELL",
                quantity=100, price=110.0, amount=11000.0)


def _mk() -> RiskManager:
    return RiskManager()


def test_same_day_multiple_loss_sells_count_once():
    """★ 核心回归：同日 4 笔亏损 SELL（相差 3 毫秒）只应计 1 次连亏。

    修复前：consec 0 → 4（且实盘是由 3 跳到 7，直接越过 halt=5 全冻结）。
    """
    r = _mk()
    for code in ("300502.SZ", "603986.SH", "688008.SH", "688012.SH"):
        r.on_fill(_loss(code), avg_cost=100.0, total_asset=1_000_000.0)
    assert r.consecutive_losses == 1, (
        f"同批退出必须合并为 1 次连亏，实际 {r.consecutive_losses}")
    assert not r.export_state()["halted"], "单次板块回调不得触发熔断"
    assert r.position_scale == 1.0, "单次板块回调不得降仓"


def test_winning_sell_resets_streak_immediately():
    """盈利卖出照旧立即归零连亏，并清掉当日计数标记。"""
    r = _mk()
    r.on_fill(_loss(), avg_cost=100.0, total_asset=1_000_000.0)
    r.on_fill(_loss("603986.SH"), avg_cost=100.0, total_asset=1_000_000.0)
    assert r.consecutive_losses == 1
    r.on_fill(_win("688008.SH"), avg_cost=100.0, total_asset=1_000_000.0)
    assert r.consecutive_losses == 0, "盈利卖出必须立即归零连亏"
    # 归零后当日再次亏损应重新计为第 1 次（不是被 last_loss_day 吞掉）
    r.on_fill(_loss("688012.SH"), avg_cost=100.0, total_asset=1_000_000.0)
    assert r.consecutive_losses == 1


def test_next_trading_day_can_count_again():
    """跨交易日：连亏保护链路完整保留，每天至多 +1。"""
    r = _mk()
    today = date.today()
    r._last_loss_day = today - timedelta(days=1)
    r._consec_loss = 1
    r.on_fill(_loss(), avg_cost=100.0, total_asset=1_000_000.0)
    assert r.consecutive_losses == 2, "新交易日应能再计 1 次连亏"


def test_streak_across_days_still_reaches_halt():
    """连续 5 个亏损交易日仍能推进到 halt（本修复不是把保护关掉）。"""
    r = _mk()
    for d in range(5):
        r._last_loss_day = date.today() - timedelta(days=5 - d)
        r.on_fill(_loss(f"60{d:04d}.SH"), avg_cost=100.0,
                  total_asset=1_000_000.0)
    assert r.consecutive_losses == 5, (
        f"连续 5 个亏损交易日应计满 5 次，实际 {r.consecutive_losses}")
    assert r.position_scale == 0.0, "达 halt 阈值必须完全停手"
    assert r.export_state()["halted"], "达 halt 阈值必须熔断"


def test_last_loss_day_persisted_and_restored():
    """`last_loss_day` 必须随 engine_state 持久化，否则重启后当日可再计一次。"""
    r = _mk()
    r.on_fill(_loss(), avg_cost=100.0, total_asset=1_000_000.0)
    state = r.export_state()
    assert state.get("last_loss_day") == date.today().isoformat()

    # 真实重启路径：_consec_loss 由 engine_state 的独立列恢复（非 export_state），
    # 而 last_loss_day 走 export_state/load_state。两者都要还原才等价于重启。
    r2 = _mk()
    r2.load_state(state)
    r2._consec_loss = 1
    assert r2._last_loss_day == date.today(), "last_loss_day 必须随 engine_state 还原"
    # 重启后当日再亏一笔，不得再 +1
    r2.on_fill(_loss("603986.SH"), avg_cost=100.0, total_asset=1_000_000.0)
    assert r2.consecutive_losses == 1, "重启后同日不得重复计数"


def test_daily_pnl_still_accumulates_all_fills():
    """合并计数只改「连亏次数」口径，日内盈亏仍按全部成交累计（风控口径不变）。"""
    r = _mk()
    for code in ("300502.SZ", "603986.SH", "688008.SH", "688012.SH"):
        r.on_fill(_loss(code), avg_cost=100.0, total_asset=1_000_000.0)
    # 每笔亏 (90-100)*100 = -1000
    assert abs(r.daily_pnl - (-4000.0)) < 1e-6, (
        f"日内盈亏应累计全部 4 笔 = -4000，实际 {r.daily_pnl}")

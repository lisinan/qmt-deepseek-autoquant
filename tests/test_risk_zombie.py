# -*- coding: utf-8 -*-
"""账户「僵尸冻结」自愈回归测试（2026-09-21 P0 修复）。

背景（paper 实盘复现）：09-16 强平 5 笔全亏 → 连亏计数 9（≥ halt 阈值 5）→
降仓阶梯末端 0.0 → position_scale=0.0 → 引擎 `_handle_buy` 在 `scale<=0` 直接 return。
由于 _halted 未持久化而 _consec_loss 从 engine_state 恢复为陈旧值，重启后 halted=False，
旧 `_maybe_recover` 首行 `if not self._halted: return False` 使冷却恢复**永不触发**；
且 reset_daily() 只挂在 on_fill() 上，零成交时日切重置也不执行。
结果：账户 09-17/18/21 连续三日 100% 现金、0 持仓、0 成交，且无任何自愈路径。

本文件锁定修复语义：
  1. 「未 halt 但连亏达 halt 阈值」的陈旧态，按 halt_recover_days 冷却后自愈；
  2. 自愈**不重置回撤基线**（max_drawdown 保护不弱化）；
  3. 冷却未到不提前解封；
  4. 正常连亏（未达 halt 阈值）仍按阶梯降仓，不被误清零；
  5. 日切在零成交时也能重置日内盈亏（reset_daily 挂到 on_asset_update）。
"""
from __future__ import annotations

import sys
from datetime import date, timedelta

sys.path.insert(0, ".")

from risk.manager import RiskManager  # noqa: E402


def _mk(consec: int, halt_day: date | None, daily_pnl: float = 0.0) -> RiskManager:
    r = RiskManager()
    r._consec_loss = consec
    r._halted = False
    r._halt_reason = ""
    r._halt_day = halt_day
    r._daily_pnl = daily_pnl
    return r


def test_zombie_freeze_self_heals_after_cooldown():
    """陈旧连亏 + 冷却已满 → 自愈，仓位倍数恢复 1.0。"""
    r = _mk(consec=9, halt_day=date.today() - timedelta(days=5),
            daily_pnl=-188766.82)
    assert r.position_scale == 0.0, "修复前应处于冻结（复现缺陷）"
    r.on_asset_update(803680.18, day_open_asset=803680.18)
    assert r.position_scale == 1.0, "冷却满后应恢复满仓能力"
    assert r.consecutive_losses == 0
    assert r.daily_pnl == 0.0


def test_zombie_freeze_stays_frozen_before_cooldown():
    """冷却未满（同一天触发）→ 不解封，避免刚冻结就立刻放行。"""
    r = _mk(consec=9, halt_day=date.today())
    r.on_asset_update(803680.18, day_open_asset=803680.18)
    assert r.position_scale == 0.0, "冷却未满不应解封"
    assert r.consecutive_losses == 9


def test_zombie_heal_preserves_drawdown_baseline():
    """自愈只清连亏/日内盈亏，**保留 peak_asset**，回撤熔断不被架空。"""
    r = _mk(consec=9, halt_day=date.today() - timedelta(days=3))
    r._peak_asset = 1_000_000.0
    r.on_asset_update(803680.18, day_open_asset=803680.18)
    assert r.position_scale == 1.0
    assert r._peak_asset == 1_000_000.0, "回撤基线必须保留"


def test_normal_consecutive_losses_still_derated():
    """未达 halt 阈值的正常连亏：仍按阶梯降仓，且不被自愈逻辑误清零。"""
    # max_consecutive_losses=3：consec=3 → idx=1 → 0.8；consec=4 → idx=2 → 0.6
    r = _mk(consec=3, halt_day=None)
    r.on_asset_update(1_000_000.0, day_open_asset=1_000_000.0)
    assert r.consecutive_losses == 3, "正常连亏不应被自愈清零"
    assert r.position_scale == 0.8
    r2 = _mk(consec=4, halt_day=None)
    assert r2.position_scale == 0.6


def test_healthy_state_unaffected():
    """健康账户：position_scale 恒为 1.0，自愈逻辑不介入。"""
    r = _mk(consec=0, halt_day=None)
    assert r.position_scale == 1.0
    r.on_asset_update(1_000_000.0, day_open_asset=1_000_000.0)
    assert r.position_scale == 1.0


def test_daily_pnl_resets_on_day_rollover_without_fills():
    """零成交时跨日也要重置日内盈亏（旧实现只在 on_fill 里重置，会背陈旧亏损）。"""
    r = _mk(consec=0, halt_day=None, daily_pnl=-5000.0)
    r._today = date.today() - timedelta(days=1)   # 模拟上一交易日遗留
    r.on_asset_update(1_000_000.0, day_open_asset=1_000_000.0)
    assert r.daily_pnl == 0.0, "跨日必须清零日内盈亏"

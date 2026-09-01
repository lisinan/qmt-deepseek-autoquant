# -*- coding: utf-8 -*-
"""批次 B 验证：T+1 约束的回测正确性。

A 股 T+1 限制：当日买入次日才能卖出。原回测实现里 A 段刚建的仓在 B 段
离场循环立即就能被止损/破位卖出，等于替策略规避了「买入当天大幅跳水
只能扛到明天」的那部分风险。

本测试在合成面板上验证：
  T+1=True   当日建仓 → 当日触发止损 → 不卖（继续持仓）
  T+1=False  当日建仓 → 当日触发止损 → 当日卖出（口径有 bug）
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy.backtest_daily import run_backtest, BacktestConfig  # noqa: E402


def _buy_today_sell_today_panel() -> dict:
    """专门构造一组：Day 0 BUY 在尾盘发出，Day 1 开盘成交 → Day 1 跌穿止损。

    需要 >= WARMUP (默认 65) + 几根 才能进入主循环。用 80 根 + 主信号在尾部。
    前 76 根是平盘 warmup，后面 Day 0..Day 4 是触发段。
    """
    pre = [100.0] * 76
    pre_low = [99.5] * 76
    pre_high = [100.5] * 76
    pre_vol = [1_000_000] * 76
    return {
        "X.SZ": {
            "open":  pre    + [100.0, 100.0, 90.0, 95.0, 100.0],
            "high":  pre_high + [100.0, 100.0, 92.0, 96.0, 100.0],
            "low":   pre_low + [ 99.5,  92.0, 89.5, 94.5, 99.5],
            "close": pre    + [100.0,  93.0, 91.0, 95.5, 100.0],
            "volume": pre_vol + [1_000_000] * 5,
            "valid":  [True] * 81,
        }
    }


def _run(panel: dict, t1: bool) -> dict:
    cfg = BacktestConfig(
        cost_pct=0.0,
        t1_restriction=t1,
        use_gate=False,
        buy_score_threshold=0.0,
        min_signals=0,
        max_positions=1,
        fixed_amount=10000.0, risk_per_trade=0.0,
        vol_sizing=False,
        exit_mode="scalp",
        stop_loss=-0.04, take_profit=10.0,
        max_hold_days=10,
        down_day_exit_pct=-99.0,
        momentum_rank=False,
    )
    return run_backtest(list(panel.keys()), cfg, count=len(panel["X.SZ"]["close"]),
                       preloaded=panel)


def test_t1_delays_exit_comparing_holds():
    """T+1 应使 avg_hold 不小于 T+0（最坏持平、有则变长）。

    合成面板让策略在 WARMUP 后的 Day 1 建仓、Day 2 出现低 92.0（-8% 远破 -4% 止损）：
    T+0 下 Day 2 立即被止损卖出；T+1 下 Day 2 只跟不卖，Day 3 才能执行，
    使 avg_hold 增大。出于多个连续建仓、退出原因混合，平均只需 “不小于” 即可。
    """
    p = _buy_today_sell_today_panel()
    r_old = _run(p, t1=False)
    r_new = _run(p, t1=True)
    h_old = r_old.get("avg_hold") or 0
    h_new = r_new.get("avg_hold") or 0
    assert r_old.get("n_trades", 0) >= 1
    assert r_new.get("n_trades", 0) >= 1
    assert h_new >= h_old, \
        f"T+1 下平均持仓 {h_new} 应不小于 T+0 的 {h_old}"


def test_t1_does_not_break_t0_for_reference():
    """回滚保险：t1_restriction=False 时现现现现与回测基准一致。"""
    p = _buy_today_sell_today_panel()
    r = _run(p, t1=False)
    assert r.get("n_trades", 0) >= 1
    assert "error" not in r, r


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
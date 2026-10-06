# -*- coding: utf-8 -*-
"""【2026-10-06 AM-EVOLVE 第 17 轮】板块轮动（rotation）回测代理守卫。

背景：轮动是本项目**最后一条**「实盘有、回测无」的建仓分支。实盘
`enable_rotation=True` + `rotation_fill_empty_slot=True` + `rotation_hot_top_n=15`
一直在跑，而 `strategy/backtest_daily.py` 在 2026-10-06 之前对 rotation**零建模**
（G/H 系列只建模了它的两个副作用：绕日线闸门、超仓）。

★ 为什么这条分支值得怀疑（先量后改，非先猜）：
   热板块候选（LLM rerank top ∪ 板块推荐池 top15）**显式绕过动量闸门**
   （event_engine.py:774-777）⇒ 实盘真正持有的可能是动量第 4~18 名，
   而「放宽动量池」的 Sharpe 代价实测 60×9：top_n 4 −0.398 / 6 −0.494 /
   15 −0.641 ⇒ 与观察篮（Sharpe −0.69）同型的结构性拖累。

本文件锁死 4 条语义（防止代理退化成「看起来建模了、其实没生效」的死代码）：
  1. 默认关闭 ⇒ **零行为变化**（P0 基线必须仍是已验证数值）
  2. 打开后**必须真的改变结果**，且方向为「换手上升、Sharpe 下降」
  3. 代理默认值必须与**生产 STRATEGY_PARAMS 逐项一致**（防第 9 次口径漂移）
  4. `rotation_max_swaps_per_eval=0` 必须等效关闭「弱换强」（保留补空槽）
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import strategy._evolve_wf as W  # noqa: E402
from strategy.backtest_daily import BacktestConfig, run_backtest  # noqa: E402

# 【门锁 1】默认口径下 P0 基线的已验证数值（23 只 AI 宇宙 × 900 根，单边 0.15%）。
# 与 tests/test_manual_entry_proxy.py 同源（down_day_exit_pct=-3.25 口径）。
BASE_IS_RET = 0.9068        # +90.68%
BASE_IS_SHARPE = 1.7925     # +1.79
BASE_IS_MDD = -0.0542       # -5.42%
BASE_TRADES = 176           # 已验证成交笔数
TOL_RET = 0.02
TOL_SH = 0.05
TOL_MDD = 0.01


def _is_result(cfg):
    """全样本 IS 回测（复用验证器的预加载与对齐逻辑）。"""
    codes = W.wide_universe()
    data = W.preload(codes + [W.MARKET_INDEX_CODE, "000300.SH"], 900)
    _d0, panel = W.aligned_universe(data, codes)
    ks = [k for k in panel.keys() if k not in W.INDEX_CODES]
    return run_backtest(ks, cfg, count=900, preloaded=panel)


def test_default_rotation_off_keeps_validated_baseline():
    """门锁 1：默认关闭 ⇒ 零行为变化（不破坏任何历史结论与已验证基线）。"""
    b = W.base_cfg()
    assert b.rotation_enabled is False, (
        "P0 基线的 rotation_enabled 必须为 False（它代表「已验证的纯动量路径」）；"
        "轮动本身尚未被回测验证过，默认不得静默生效")
    r = _is_result(b)
    assert abs(r["total_return"] - BASE_IS_RET) < TOL_RET, (
        f"P0 收益漂移：期望 {BASE_IS_RET:.4f}，实际 {r['total_return']:.4f}")
    assert abs(r["sharpe"] - BASE_IS_SHARPE) < TOL_SH, (
        f"P0 Sharpe 漂移：期望 {BASE_IS_SHARPE:.4f}，实际 {r['sharpe']:.4f}")
    assert abs(r["max_drawdown"] - BASE_IS_MDD) < TOL_MDD, (
        f"P0 MDD 漂移：期望 {BASE_IS_MDD:.4f}，实际 {r['max_drawdown']:.4f}")


def test_rotation_proxy_actually_changes_behavior():
    """门锁 2：打开后必须真的改变结果，且方向为「换手↑、Sharpe↓」。

    这条用例同时是本轮结论的**可复现锚点**：若日后有人修改轮动/动量逻辑
    使得「轮动不再拖累」，此处会立即变红，强制重新量一遍代价。
    """
    base_r = _is_result(W.base_cfg())
    rot_r = _is_result(replace(W.base_cfg(), rotation_enabled=True))
    assert rot_r["n_trades"] > base_r["n_trades"] * 1.15, (
        f"开启轮动后换手应显著上升（实测 {base_r['n_trades']} → "
        f"{rot_r['n_trades']} 笔）；若几乎没变 ⇒ 代理已被断开（死代码）")
    assert rot_r["sharpe"] < base_r["sharpe"] - 0.10, (
        f"轮动应显著拖累 Sharpe（实测 {base_r['sharpe']:.2f} → "
        f"{rot_r['sharpe']:.2f}）；若不再拖累，需重跑复核本轮结论")


def test_rotation_proxy_defaults_match_production():
    """门锁 3：代理默认值必须与生产 STRATEGY_PARAMS 逐项一致（防第 9 次漂移）。

    本项目已踩 8 次「回测器默认值 ≠ 生产值」的同型坑（top_n / rpt /
    northbound_mode / down_day / atr / max_single_position_pct /
    daily_stop_flatten_pct / manual_entry_*）。轮动全套旋钮必须一次对齐。
    """
    import config.settings as S  # noqa: E402
    p = S.STRATEGY_PARAMS
    c = BacktestConfig()
    pairs = [
        ("rotation_hot_top_n", c.rotation_hot_top_n, p.get("rotation_hot_top_n")),
        ("rotation_fill_empty_slot", c.rotation_fill_empty_slot,
         p.get("rotation_fill_empty_slot")),
        ("rotation_intraday_breakout_pct", c.rotation_intraday_breakout_pct,
         p.get("rotation_intraday_breakout_pct")),
        ("rotation_min_score_gap", c.rotation_min_score_gap,
         p.get("rotation_min_score_gap")),
        ("rotation_min_score_gap_hot", c.rotation_min_score_gap_hot,
         p.get("rotation_min_score_gap_hot")),
        ("rotation_max_swaps_per_eval", c.rotation_max_swaps_per_eval,
         p.get("rotation_max_swaps_per_eval")),
        ("rotation_require_daily_gate", c.rotation_require_daily_gate,
         p.get("rotation_require_daily_gate")),
    ]
    for name, got, want in pairs:
        assert got == want, (
            f"{name} 回测默认({got}) ≠ 生产({want}) ⇒ 又一处口径漂移，"
            f"必须显式对齐（本项目已踩 8 次同型坑）")


def test_zero_swaps_disables_weak_to_strong_only():
    """门锁 4：rotation_max_swaps_per_eval=0 应等效「只留补空槽、关闭弱换强」。

    这是本轮备选落盘动作的**最小可逆开关**：实盘同名参数存在且有读取点
    （event_engine.py:846 `if swaps >= self.rotation_max_swaps_per_eval: break`），
    置 0 即只保留补空槽分支，无需改代码。
    """
    base_r = _is_result(W.base_cfg())
    full_r = _is_result(replace(W.base_cfg(), rotation_enabled=True))
    no_w2s = _is_result(replace(W.base_cfg(), rotation_enabled=True,
                                rotation_max_swaps_per_eval=0))
    assert base_r["n_trades"] <= no_w2s["n_trades"] < full_r["n_trades"], (
        f"关闭弱换强后成交数应落在「无轮动」与「全轮动」之间："
        f"实测 {base_r['n_trades']} / {no_w2s['n_trades']} / {full_r['n_trades']}")

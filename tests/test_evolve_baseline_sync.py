# -*- coding: utf-8 -*-
"""验证器基线 vs 生产配置「口径漂移」守卫（2026-09-28 第 10 轮 AM-EVOLVE 新增）。

背景（本项目已踩两次同一个坑）：
  2026-09-18（第 2 轮）：base_cfg 停留在 top_n=6 / risk_per_trade=0.02，而生产
    已是 3 / 0.012 ⇒ 所有"增量 dSh"相对错误基线计算，上一轮已落盘参数的收益
    被重复计入增量。
  2026-09-28（第 10 轮）：base_cfg 未设置 northbound_mode（默认 "off"），而生产
    自 09-19 起就是 "gate" ⇒ P0 基线不含北向闸门，OOS 均值 Sharpe 系统性少算
    **+0.207**（1.266 → 1.473），KPI 与全部候选增量同时失真。

两次都是「生产改了/本就如此，验证器没跟上」。本守卫把这件事变成**自动失败**：
  · MUST_MATCH 字段：验证器必须与生产**逐字段相等**，否则直接红。
  · KNOWN_DIVERGENCE 字段：已知且尚未裁决的历史差异（回测侧刻意保留的旧值）。
    这里不要求相等，但**钉住具体数值**——任一侧再变动都会红，防止差异无声扩大。

新增参数时：若它同时存在于 STRATEGY_PARAMS 与 BacktestConfig，必须归入二者之一，
不能"默默不提"。
"""
from __future__ import annotations
import sys
sys.path.insert(0, ".")

import config.settings as S
from strategy._evolve_wf import base_cfg

# 验证器必须与生产逐字段一致（这些都是有实盘读取点、且回测/实盘同语义的参数）
MUST_MATCH = {
    "exit_mode": None,            # "trend"
    "trend_exit_ma": None,        # 60
    "hard_stop_pct": None,        # -0.18
    "trend_max_hold_days": None,  # 120
    "momentum_rank": None,        # True
    "momentum_top_n": None,       # 3
    "momentum_lookback": None,    # 60
    "risk_per_trade": None,       # 0.012
    "max_positions": None,        # 5
    "buy_score_threshold": None,  # 4.0
    "min_signals": None,          # 3
    "northbound_mode": None,      # "gate"（2026-09-28 同步）
    "nb_lookback": None,          # 20
    # 2026-09-28 第 11 轮：北向闸门可信度守卫（两侧同语义、必须同步，否则
    # 回测会用「恒正成交额序列」白拿 2023-2024 的拦截收益而实盘拿不到）
    "nb_credibility_guard": None,
    "nb_stale_days": None,
    "nb_degenerate_days": None,
    "min_daily_bias": None,       # 2.0（bias 通道关闭，2026-09-22 落盘）
    "regime_mode": None,          # "off"
    "regime_index": None,
    "regime_ma": None,
    "regime_breadth_thresh": None,
    "regime_force_exit": None,
    "stop_loss": None,            # -0.04（trend 模式下仅作 ATR 止损下限兜底）
    "take_profit": None,          # 0.12
    "tp_atr_mult": None,          # 4.0
    "trailing_stop": None,        # -0.03（scalp 模式参数，trend 下不生效）
    "trailing_activation": None,
    "trailing_floor": None,
    "trend_vol_sizing": None,     # False（2026-09-21 已证伪：IS 收益腰斩）
    # 2026-09-29 第 12 轮：由 KNOWN_DIVERGENCE 移入（原 回测-9.0 / 生产-99.0）。
    # 本轮裁决为「启用单日暴跌退出」，两侧同为 -5.0。★ 注意：该参数生效还依赖
    # 实盘侧的日线口径实现（trend_strategy._day_change_pct），仅同步数值不够。
    "down_day_exit_pct": None,    # -5.0
}

# 已知、尚未裁决的历史差异：(回测侧值, 生产侧值)。钉住数值 ⇒ 任一侧变动即红。
KNOWN_DIVERGENCE = {
    # 生产 2.5 / 验证器 2.0。2026-09-21 已判定 atr_stop_mult 属"杠杆旋钮"
    # （Sharpe 几乎不变、收益与 MDD 同步放大），故未强求同步；但差异须可见。
    "atr_stop_mult": (2.0, 2.5),
    # （2026-09-29 第 12 轮）down_day_exit_pct 已裁决并移入 MUST_MATCH：
    # 两侧同为 -5.0。此前登记的 (-9.0, -99.0) 差异已消除。
}

# 两侧同名但**不参与本守卫**：回测侧的研究旋钮 / 已废弃字段 / 语义不同的同名项。
# 登记在这里是为了让"漏登记"检查保持封闭（任何新增同名项都必须显式归类）。
IGNORED = {
    "max_hold_days": "scalp 模式专用；生产 exit_mode='trend'，该字段不参与任何决策",
    "ma_short": "回测未建模（BacktestConfig 无同名语义字段）",
}


def _prod(key):
    return S.STRATEGY_PARAMS[key]


def test_validator_baseline_matches_production():
    b = base_cfg()
    bad = []
    for k in MUST_MATCH:
        assert k in S.STRATEGY_PARAMS, f"生产 STRATEGY_PARAMS 缺失 {k}"
        assert hasattr(b, k), f"验证器 base_cfg 缺失字段 {k}"
        pv, bv = _prod(k), getattr(b, k)
        # bool/int/float/str 混用时的稳健比较
        if isinstance(pv, bool) or isinstance(bv, bool):
            ok = bool(pv) == bool(bv)
        elif isinstance(pv, (int, float)) and isinstance(bv, (int, float)):
            ok = abs(float(pv) - float(bv)) < 1e-9
        else:
            ok = str(pv) == str(bv)
        if not ok:
            bad.append(f"{k}: 生产={pv!r} 验证器={bv!r}")
    assert not bad, (
        "验证器 base_cfg 与生产配置漂移（P0 基线失真会让所有 dSh 增量与 KPI 失真，"
        "2026-09-18 / 2026-09-28 各踩一次）：" + "; ".join(bad))


def test_known_divergences_are_pinned():
    """已知差异不得无声变化——任一侧改值都必须显式更新本表并说明原因。"""
    b = base_cfg()
    for k, (back_v, prod_v) in KNOWN_DIVERGENCE.items():
        assert k in S.STRATEGY_PARAMS, f"生产 STRATEGY_PARAMS 缺失 {k}"
        assert abs(float(getattr(b, k)) - float(back_v)) < 1e-9, (
            f"{k}: 验证器侧已从 {back_v} 变为 {getattr(b, k)!r}，"
            f"须显式更新 KNOWN_DIVERGENCE 并说明原因")
        assert abs(float(_prod(k)) - float(prod_v)) < 1e-9, (
            f"{k}: 生产侧已从 {prod_v} 变为 {_prod(k)!r}，"
            f"须显式更新 KNOWN_DIVERGENCE 并说明原因")


def test_no_undocumented_shared_params():
    """同时存在于两侧、却既不在 MUST_MATCH 也不在 KNOWN_DIVERGENCE 的字段 = 漏登记。"""
    from dataclasses import fields as dc_fields
    from strategy.backtest_daily import BacktestConfig

    bt_fields = {f.name for f in dc_fields(BacktestConfig)}
    shared = {k for k in S.STRATEGY_PARAMS if k in bt_fields}
    covered = set(MUST_MATCH) | set(KNOWN_DIVERGENCE) | set(IGNORED)
    missing = sorted(shared - covered)
    assert not missing, (
        "以下字段同时存在于 STRATEGY_PARAMS 与 BacktestConfig，但未登记到本守卫"
        "（必须归入 MUST_MATCH 或 KNOWN_DIVERGENCE，避免口径无声漂移）："
        + ", ".join(missing))

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
    # 2026-09-29 第 13 轮：由 KNOWN_DIVERGENCE 移入（原 回测 2.0 / 生产 2.5）。
    # 本轮裁决为「放宽 ATR 止损倍数」，两侧同为 2.9。**这是该漂移第 5 次同型复现**
    # （09-18 top_n/rpt、09-28 northbound_mode、09-29AM down_day、09-29PM 本项）。
    # 注意：变更生产值前必须用「相对生产现状」的 dSh 决策，而不是相对 base_cfg，
    # 否则 KNOWN_DIVERGENCE 会让增量算错（本轮 2.5 vs 2.0 差 0.06 个 Sharpe）。
    "atr_stop_mult": None,        # 2.9
}

# 已知、尚未裁决的历史差异：(回测侧值, 生产侧值)。钉住数值 ⇒ 任一侧变动即红。
KNOWN_DIVERGENCE = {
    # （空）2026-09-29 第 12 轮：down_day_exit_pct 裁决为 -5.0，移入 MUST_MATCH。
    # （空）2026-09-29 第 13 轮：atr_stop_mult 裁决为 2.9，移入 MUST_MATCH。
    # 【2026-09-30 第 15 轮】观察篮首次建模，两个字段**刻意保留不同**：
    #   manual_entry_codes      生产 = 5 只观察篮 / 回测基线 = () 空元组
    #   manual_entry_exit_exempt 生产 = True                  / 回测基线 = True
    # 为什么不同：**P0 基线的定义就是「已验证的纯动量路径」**，
    #   它代表「观察篮存在之前」的那条被走完 15 轮验证的策略；
    #   观察篮是**叠加在其上的一层实盘行为**，必须作为**显式候选**（N1/N2/N3/N4）
    #   与基线对比，而不是悄悄并进 P0——否则所有历史 dSh 都会因为基线里多塞了
    #   一块从未验证过的仓位来源而整体失真。
    # ⇒ 代价：这两个字段现在是「回测无、实盘有」的第 8 次登记差异，
    #   演进时会用 `--track defect --baseline N1_观察篮实盘现状_豁免` 反向对照，
    #   使 dSh 直接读出「关闭观察篮/豁免」的修复收益。
    "manual_entry_codes": ((), ("300502.SZ", "300308.SZ", "002415.SZ",
                                "000977.SZ", "603986.SH")),
    "manual_entry_exit_exempt": (True, True),
}

# 两侧同名但**不参与本守卫**：回测侧的研究旋钮 / 已废弃字段 / 语义不同的同名项。
# 登记在这里是为了让"漏登记"检查保持封闭（任何新增同名项都必须显式归类）。
IGNORED = {
    "max_hold_days": "scalp 模式专用；生产 exit_mode='trend'，该字段不参与任何决策",
    "ma_short": "回测未建模（BacktestConfig 无同名语义字段）",
}


# 【2026-09-30 第 14 轮】RISK_PARAMS 侧的同名同步字段。
#   ★ 旧守卫**只比对 STRATEGY_PARAMS**，于是 ``max_single_position_pct``
#   （生产 0.19 / 回测 0.30）默默漂移了很久无人察觉——这是第 6 次同型漂移，
#   也是第一次「漂移发生在守卫视野之外」。故把 RISK_PARAMS 也纳入比对。
RISK_MUST_MATCH = {
    "max_single_position_pct": None,   # 0.19
    "daily_stop_flatten_pct": None,    # -0.06（同批第 7 次漂移，剂量扫描零差异）
}


def _prod(key):
    return S.STRATEGY_PARAMS[key]


def _prod_risk(key):
    return S.RISK_PARAMS[key]


def _compare(b, key, pv):
    """返回 None 表示一致，否则返回描述串。"""
    assert hasattr(b, key), f"验证器 base_cfg 缺失字段 {key}"
    bv = getattr(b, key)
    if isinstance(pv, bool) or isinstance(bv, bool):
        ok = bool(pv) == bool(bv)
    elif isinstance(pv, (int, float)) and isinstance(bv, (int, float)):
        ok = abs(float(pv) - float(bv)) < 1e-9
    else:
        ok = str(pv) == str(bv)
    return None if ok else f"{key}: 生产={pv!r} 验证器={bv!r}"


def test_validator_baseline_matches_production():
    b = base_cfg()
    bad = []
    for k in MUST_MATCH:
        assert k in S.STRATEGY_PARAMS, f"生产 STRATEGY_PARAMS 缺失 {k}"
        d = _compare(b, k, _prod(k))
        if d:
            bad.append(d)
    for k in RISK_MUST_MATCH:
        assert k in S.RISK_PARAMS, f"生产 RISK_PARAMS 缺失 {k}"
        d = _compare(b, k, _prod_risk(k))
        if d:
            bad.append(d)
    assert not bad, (
        "验证器 base_cfg 与生产配置漂移（P0 基线失真会让所有 dSh 增量与 KPI 失真，"
        "2026-09-18 / 2026-09-28 各踩一次）：" + "; ".join(bad))


def test_known_divergences_are_pinned():
    """已知差异不得无声变化——任一侧改值都必须显式更新本表并说明原因。"""
    b = base_cfg()
    for k, (back_v, prod_v) in KNOWN_DIVERGENCE.items():
        assert k in S.STRATEGY_PARAMS, f"生产 STRATEGY_PARAMS 缺失 {k}"
        # 【2026-09-30 第 15 轮】原实现只认数值（float 强转），加入观察篮这类
        # **非数值**字段后会直接 TypeError ⇒ 比较改为类型无关。
        # 语义不变：任一侧改值都必须显式更新本表并说明原因。
        got_b, got_p = getattr(b, k), _prod(k)
        if isinstance(back_v, (tuple, list)):
            ok_b = tuple(got_b) == tuple(back_v)
            ok_p = tuple(got_p or ()) == tuple(prod_v)
        elif isinstance(back_v, bool) or isinstance(got_b, bool):
            ok_b, ok_p = bool(got_b) == bool(back_v), bool(got_p) == bool(prod_v)
        else:
            ok_b = abs(float(got_b) - float(back_v)) < 1e-9
            ok_p = abs(float(got_p) - float(prod_v)) < 1e-9
        assert ok_b, (
            f"{k}: 验证器侧已从 {back_v} 变为 {got_b!r}，"
            f"须显式更新 KNOWN_DIVERGENCE 并说明原因")
        assert ok_p, (
            f"{k}: 生产侧已从 {prod_v} 变为 {got_p!r}，"
            f"须显式更新 KNOWN_DIVERGENCE 并说明原因")


def test_no_undocumented_shared_params():
    """两侧同名却未登记的字段 = 漏登记（STRATEGY_PARAMS **和** RISK_PARAMS 都要查）。

    【2026-09-30 第 14 轮扩展】原实现只扫 ``STRATEGY_PARAMS``，于是
    ``max_single_position_pct``（RISK_PARAMS 0.19 / BacktestConfig 0.30）在守卫
    视野之外默默漂移——第 6 次同型漂移第一次发生在 RISK 侧。这里一并覆盖。
    """
    from dataclasses import fields as dc_fields
    from strategy.backtest_daily import BacktestConfig

    bt_fields = {f.name for f in dc_fields(BacktestConfig)}
    covered = (set(MUST_MATCH) | set(KNOWN_DIVERGENCE) | set(IGNORED)
               | set(RISK_MUST_MATCH))
    missing = sorted(
        ({k for k in S.STRATEGY_PARAMS if k in bt_fields}
         | {k for k in S.RISK_PARAMS if k in bt_fields}) - covered)
    assert not missing, (
        "以下字段同时存在于生产配置与 BacktestConfig，但未登记到本守卫"
        "（必须归入 MUST_MATCH / RISK_MUST_MATCH / KNOWN_DIVERGENCE，"
        "避免口径无声漂移）：" + ", ".join(missing))

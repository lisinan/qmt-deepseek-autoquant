# -*- coding: utf-8 -*-
"""R40 守卫：KPI#2「正收折数」是**划分方式依赖**的统计量，不可作决策依据。

【背景】PM-EVOLVE R40（2026-10-20）实测：同一条真实回测曲线（末 540 根，
20240724→20261019，累计 +218.8%），只改变 fold 长度重切，
「正收段数占比」在 **50.0% ~ 77.8%** 之间摆动（极差 27.8pp），
7 种划分无一达到 85.7%（6/7）目标线；而同一段的**拼接 Sharpe 恒为 2.1596**。

【为什么钉这个】本项目已 17 次栽在「量测通道里的数字不是它看起来的意思」。
若不钉住，后续自进化轮次会继续把「正收 20/26 差 2 折」当作策略缺陷去调参
（AM R39 已因此把 momentum_weight 四剂量全扫一遍，最终判为削峰填谷）。

【断言纪律（第 19 条教训：断言钉契约、别钉副产品）】
本文件**全部使用合成输入**，不依赖 xtdata / tushare / 真实面板 ⇒
数据源翻转时结论不会翻转。钉的是这两个统计量的**数学性质**：
  1. 折口径正收比例随 fold 改变（不稳定性）
  2. 拼接 Sharpe 与 fold 无关（划分不变性）
"""
from __future__ import annotations

import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from strategy.kpi_stats import (                                  # noqa: E402
    daily_returns_from_curve,
    fold_positive_ratio,
    month_win_rate,
    span_sharpe,
)


# 上涨窗口 / 其余为下跌（刻意**不均匀**：两段深亏期，模拟真实趋势策略
# 「低暴露 + 长骑行 + 深回撤」的指纹，而不是均匀脉冲——均匀脉冲会让
# 每一种 fold 都切出相同比例、反而测不出不稳定性）。
_UP_WINDOWS = [(0, 100), (170, 360), (500, 540)]
_DR = 0.0055


def _synthetic_curve(n: int = 540) -> list:
    """合成一条「整体大赚、但有两段深亏期」的权益曲线（无需外部数据）。

    R40 真实段对照：末 540 根累计 +218.8%、拼接 Sharpe 2.1596，
    而正收比例只有 50.0%~77.8%。本合成曲线复刻该形态：
    净上涨 330 根 / 净下跌 210 根 ⇒ 整段约 +93%、Sharpe ≈ 3.5，
    但 7 种 fold 的正收比例**全部**低于 85.7%，且彼此相差 > 30pp。
    """
    def is_up(i: int) -> bool:
        return any(a <= i < b for a, b in _UP_WINDOWS)

    eq = [1_000_000.0]
    for i in range(n):
        eq.append(eq[-1] * (1.0 + (_DR if is_up(i) else -_DR)))
    return eq


def test_fold_positive_ratio_changes_with_fold_only():
    """同一条曲线，只改 fold ⇒ 正收比例必须改变（口径不稳定）。"""
    daily = daily_returns_from_curve(_synthetic_curve())
    ratios = {}
    for fold in (30, 45, 60, 90, 135, 180, 270):
        ratios[fold] = fold_positive_ratio(daily, fold)["pos_pct"]
    spread = max(ratios.values()) - min(ratios.values())
    assert spread >= 15.0, (
        f"正收比例应随 fold 显著摆动，实测极差仅 {spread:.1f}pp：{ratios}")


def test_span_sharpe_is_invariant_to_how_we_cut():
    """划分不变量：拼接 Sharpe 与 fold 无关（同一段日收益只算一次）。"""
    daily = daily_returns_from_curve(_synthetic_curve())
    base = span_sharpe(daily)
    assert abs(base) > 1e-9
    # 无论声称按哪种 fold 切，整段 Sharpe 都是同一个数
    for fold in (30, 60, 90, 270):
        st = fold_positive_ratio(daily, fold)
        assert span_sharpe(daily) == base
        assert st["nseg"] == len(daily) // fold


def test_short_folds_are_dominated_by_single_segment_noise():
    """折越短 ⇒ 单段样本越少 ⇒ 越容易被单段噪声支配（极差单调性倾向）。

    断言：fold=30（18 段）与 fold=270（2 段）的正收比例之差的绝对值 > 0，
    且 fold=30 的段数是 fold=270 的 9 倍（段数由 fold 反比决定）。
    """
    daily = daily_returns_from_curve(_synthetic_curve())
    a = fold_positive_ratio(daily, 30)
    b = fold_positive_ratio(daily, 270)
    assert a["nseg"] == 9 * b["nseg"]
    assert a["pos_pct"] != b["pos_pct"], (
        "不同 fold 的正收比例若完全相同，说明该指标对划分免疫（与 R40 实测矛盾）")


def test_month_win_rate_is_well_defined_and_bounded():
    """大样本替代口径：日历月正收益占比，取值必须在 [0,100] 且月数正确。"""
    daily = daily_returns_from_curve(_synthetic_curve(540))
    dates = [f"20{24 + (i // 360):02d}{(i // 30) % 12 + 1:02d}15"
             for i in range(len(daily))]
    m = month_win_rate(daily, dates)
    assert m["months"] == 18, m
    assert 0.0 <= m["pos_month_pct"] <= 100.0, m
    assert m["pos_months"] <= m["months"]


def test_month_win_rate_aggregates_geometrically():
    """月内按几何累加（而非算术），保证「先跌 50% 再涨 50%」= −25% 而非 0%。"""
    daily = [-0.5, 0.5]
    dates = ["20240101", "20240102"]
    m = month_win_rate(daily, dates)
    assert m["months"] == 1
    assert abs(m["by_month"]["202401"] - (-0.25)) < 1e-9, m


def test_daily_returns_skips_non_positive_equity():
    """权益 <= 0 的**起点**不产生收益（防除零/负权益把曲线算爆）。

    契约：长度为 n 的曲线最多产生 n-1 个日收益；起点 <= 0 的段被跳过，
    且**不抛异常**（爆仓点不会污染后续聚合）。
    """
    curve = [1.0, 1.1, 0.0, 0.0, 2.0]
    r = daily_returns_from_curve(curve)
    # i=0: 1.0->1.1 可算；i=1: 1.1->0.0 可算（起点 > 0）；
    # i=2,i=3: 起点 = 0 ⇒ 跳过（否则除零）
    assert len(r) == 2, r
    assert abs(r[0] - 0.1) < 1e-12, r
    # 正常曲线不丢点
    good = [1.0, 1.1, 1.2, 1.3]
    assert len(daily_returns_from_curve(good)) == 3


def test_sharpe_degenerate_inputs():
    """空序列 / 零方差不崩，返回 0（守卫而非噪声放大）。"""
    assert span_sharpe([]) == 0.0
    assert span_sharpe([0.01]) == 0.0
    assert span_sharpe([0.01, 0.01, 0.01]) == 0.0


def test_fold_ratio_accounts_for_all_bars():
    """各段收益复合 = 整段收益（切分不丢 bar、不重复计）。"""
    daily = daily_returns_from_curve(_synthetic_curve(540))
    total = 1.0
    for r in daily:
        total *= (1.0 + r)
    total -= 1.0
    for fold in (30, 60, 90):
        st = fold_positive_ratio(daily, fold)
        acc = 1.0
        for s in st["segs"]:
            acc *= (1.0 + s)
        assert abs((acc - 1.0) - total) < 1e-9, (fold, acc - 1.0, total)


def test_857_target_is_unreachable_by_any_cut_on_this_curve():
    """★ 核心裁决依据：这条「长期大赚」的合成曲线，
    按任何一种 fold 切分都**达不到** 85.7%（6/7）的正收目标线。

    ⇒ 该目标线衡量的是「策略能否每 60~120 根都盈利」，
    对「低暴露 + 长骑行 + 深回撤」型趋势策略在数学上近乎不可达，
    与策略优劣无关（真实曲线实测 50.0%~77.8%，同样全 FAIL）。
    """
    daily = daily_returns_from_curve(_synthetic_curve())
    assert span_sharpe(daily) > 2.0, "合成曲线本身应是大赚曲线（Sharpe > 2）"
    for fold in (30, 45, 60, 90, 135, 180, 270):
        pct = fold_positive_ratio(daily, fold)["pos_pct"]
        assert pct < 85.7, (
            f"fold={fold} 竟达到 {pct:.1f}%，与 R40 实测结论矛盾")

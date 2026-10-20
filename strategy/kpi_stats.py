# -*- coding: utf-8 -*-
"""KPI 口径工具：把「折/分段口径」与「大样本口径」显式分开。

【为什么需要这个模块】（PM-EVOLVE R40，2026-10-20）

本项目两个核心 KPI 都被证明是**划分方式依赖的小样本统计量**：

- KPI#1「walk-forward 折均值 Sharpe」：R18 已证同一段 540 根，60×9 报 1.409
  而 90×6 报 1.660（差 0.251），但**拼接 Sharpe 逐位相同**。
- KPI#2「walk-forward 正收益折数」：R40 已证同一条真实曲线（末 540 根，
  20240724→20261019，累计 +218.8%），只改 fold 长度重切，正收比例在
  **50.0% ~ 77.8%** 之间摆动（极差 27.8pp），7 种划分**无一**达到 85.7% 目标线；
  而同一段的拼接 Sharpe 恒为 2.1596。

两者的共同结构：**把一段连续历史切成 k 段，每段各算一个统计量
（Sharpe 是比率、正收是符号），再对这 k 个量取算术统计**。
折越短，每段样本越少，统计量越被单段噪声支配 ⇒ 读数主要反映「怎么切」，
而不是「策略好不好」。

因此本模块提供**与划分方式无关**的大样本口径，供 KPI 裁决时对照：
  * ``span_sharpe``         —— 整段日收益序列的年化 Sharpe（划分不变量）
  * ``month_win_rate``      —— 日历月正收益占比（大样本、有经济含义）
  * ``fold_positive_ratio`` —— 折口径正收比例（**仅用于展示其不稳定性**）

【使用纪律】
  * 用 ``span_sharpe`` / ``month_win_rate`` 做**决策**；
  * 用 ``fold_positive_ratio`` 只能做**对照**，且必须同时报告多个 fold，
    否则就是在拿划分方式当结论。
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Dict, List, Sequence

BARS_PER_YEAR = 245


def daily_returns_from_curve(curve: Sequence[float]) -> List[float]:
    """权益曲线 → 日收益序列（长度 = len(curve) - 1）。"""
    out = []
    for i in range(len(curve) - 1):
        p0 = curve[i]
        if p0 > 0:
            out.append(curve[i + 1] / p0 - 1.0)
    return out


def span_sharpe(daily: Sequence[float], bars_per_year: int = BARS_PER_YEAR) -> float:
    """整段日收益序列的年化 Sharpe。

    **划分不变量**：同一段日收益，无论按 60/90/270 根切成几段，本值恒定。
    """
    if len(daily) < 2:
        return 0.0
    m = sum(daily) / len(daily)
    var = sum((r - m) ** 2 for r in daily) / (len(daily) - 1)
    sd = math.sqrt(var)
    if sd <= 1e-12:
        return 0.0
    return m / sd * math.sqrt(bars_per_year)


def fold_positive_ratio(daily: Sequence[float], fold: int) -> Dict[str, float]:
    """把日收益序列按 ``fold`` 根切成段，返回各段收益与正收比例。

    ⚠ 本函数返回的 ``pos_pct`` **依赖 fold**，不是策略属性。
    仅用于与其它 fold 对照，证明该口径的不稳定性。
    """
    n = len(daily)
    k = n // fold
    segs = []
    for j in range(k):
        acc = 1.0
        for r in daily[j * fold:(j + 1) * fold]:
            acc *= (1.0 + r)
        segs.append(acc - 1.0)
    pos = sum(1 for x in segs if x > 0)
    return {
        "nseg": k,
        "pos": pos,
        "pos_pct": 100.0 * pos / k if k else 0.0,
        "worst": min(segs) if segs else 0.0,
        "segs": segs,
    }


def month_win_rate(daily: Sequence[float], dates: Sequence) -> Dict[str, float]:
    """日历月正收益占比（大样本口径）。

    ``dates`` 与 ``daily`` 等长，元素只要 ``str(x)[:6]`` 能得到 ``YYYYMM`` 即可。
    月内收益按日收益**几何**累加（更准确）。
    """
    pnl: Dict = defaultdict(float)
    for r, d in zip(daily, dates):
        pnl[str(d)[:6]] += math.log1p(r) if r > -1.0 else -float("inf")
    months = {k: math.expm1(v) for k, v in pnl.items()}
    pos = sum(1 for v in months.values() if v > 0)
    return {
        "months": len(months),
        "pos_months": pos,
        "pos_month_pct": 100.0 * pos / len(months) if months else 0.0,
        "by_month": months,
    }

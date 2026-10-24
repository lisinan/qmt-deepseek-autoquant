# -*- coding: utf-8 -*-
"""显著性护栏：置换检验 + Deflated Sharpe（评审报告 §3.1 P0）。

背景：参数网格（40+ 轮）× 多套折划分 = 多重检验，但晋升闸门只做
「最差窗 dSh ≥ +0.10」阈值比较，没有任何多重检验校正，噪声高原会被
误判为参数高原。本模块提供三个纯函数护栏：

1. ``permutation_pvalue``        —— 经验单尾 p 值（含 +1 连续性修正）；
2. ``permutation_test_returns``  —— 对日收益做**符号翻转**（sign flip）
   生成零分布（零假设：收益对称、无正期望），看观测 Sharpe 是否显著；
3. ``deflated_sharpe``           —— Bailey & López de Prado (2014) 的 DSR：
   把「试了 n_trials 次后的最大 Sharpe」对期望极大值做收缩；
4. ``trial_adjusted_threshold``  —— 给定试验次数的显著 Sharpe 门槛，
   供闸门②使用（不改默认闸门裁决）。

约束：纯函数、零第三方依赖（不用 numpy/scipy）、无 IO、可复现
（random.Random(seed)）。
"""
from __future__ import annotations

import math
import random
from typing import Callable, Dict, List, Sequence

from strategy.kpi_stats import span_sharpe


# ---------------------------------------------------------------- 置换检验

def permutation_pvalue(observed: float, null_samples: Sequence[float]) -> float:
    """经验单尾 p 值：p = (1 + #{null >= observed}) / (1 + n)。

    +1 修正（Phipson & Smyth 2010）：避免 p=0，且对有限置换是合法 p 值。
    方向为右尾（"越大越显著"），对应「观测 Sharpe 高于随机符号的水平」。
    """
    n = len(null_samples)
    if n == 0:
        return 1.0
    ge = sum(1 for x in null_samples if x >= observed)
    return (1.0 + ge) / (1.0 + n)


def permutation_test_returns(
    daily: Sequence[float],
    n_perm: int = 500,
    seed: int = 0,
    stat: Callable = span_sharpe,
) -> Dict[str, float]:
    """对日收益做符号翻转置换检验。

    零假设 H0：日收益关于 0 对称（无正期望、无择时能力）。
    每次置换独立地对每个 r_i 以 1/2 概率翻号，重算统计量 stat
    （默认整段年化 Sharpe，``kpi_stats.span_sharpe``）。

    返回 ``{observed, p_value, null_mean, null_p95, n_perm}``：
      * observed   —— 原始序列的统计量；
      * p_value    —— ``permutation_pvalue(observed, null)``；
      * null_mean  —— 零分布均值（应≈0，是自检信号）；
      * null_p95   —— 零分布 95 分位（排序插值）；
      * n_perm     —— 实际置换次数。
    """
    rng = random.Random(seed)
    daily = list(daily)
    observed = stat(daily) if daily else 0.0
    nulls: List[float] = []
    for _ in range(max(0, n_perm)):
        perm = [r if rng.random() < 0.5 else -r for r in daily]
        nulls.append(stat(perm))
    nulls.sort()
    if nulls:
        mean = sum(nulls) / len(nulls)
        # 线性插值分位
        k = 0.95 * (len(nulls) - 1)
        lo = int(math.floor(k))
        hi = min(lo + 1, len(nulls) - 1)
        p95 = nulls[lo] + (nulls[hi] - nulls[lo]) * (k - lo)
    else:
        mean = p95 = 0.0
    return {
        "observed": observed,
        "p_value": permutation_pvalue(observed, nulls),
        "null_mean": mean,
        "null_p95": p95,
        "n_perm": len(nulls),
    }


# --------------------------------------------------- Deflated Sharpe Ratio

def _norm_cdf(x: float) -> float:
    """标准正态 CDF，Φ(x) = (1 + erf(x/√2)) / 2。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def deflated_sharpe(
    sr: float,
    n_trials: int,
    n_obs: int,
    skew: float = 0.0,
    kurt: float = 3.0,
) -> Dict[str, float]:
    """Deflated Sharpe Ratio（Bailey & López de Prado 2014）。

    问题：试了 ``n_trials`` 个参数组合后取最大 Sharpe ``sr``，其期望在
    H0（真实 Sharpe=0）下已因多重检验而膨胀。DSR 回答：观测到的 sr
    相对「n_trials 次独立试验下最大 Sharpe 的期望」是否显著为正。

    公式（夏普均用**年化**口径；n_obs 为日收益样本数，年化因子 A=√252）：

      Var[ŜR̂] ≈ (1 - skew·ŜR + (kurt-1)/4 · ŜR²) / (n_obs - 1)   （Lo 2002）
      E[max SR₀]（n 次 N(0, Var) 取最大的近似期望）
        = √Var · [ (1-γ) · Φ⁻¹(1 - 1/n) + γ · Φ⁻¹(1 - 1/(n·e)) ]
      DSR = Φ( (ŜR - E[max SR₀]) / √Var )      ∈ (0,1)，>0.95 视为显著

    其中 γ ≈ 0.5772 为 Euler–Mascheroni 常数；Φ⁻¹ 用 Acklam 有理逼近。
    对日频 SR 先换算：ŜR_daily = sr/√252，算出 DSR 后结论与频率无关。

    返回 ``{sr0, dsr, n_trials, n_obs}``：
      * sr0 —— 期望最大 Sharpe（年化口径，即门槛）；
      * dsr —— 概率型显著度（不是 p 值；1-DSR 才是「H0 下看到这么大」的概率）。
    """
    n_trials = max(1, int(n_trials))
    n_obs = max(2, int(n_obs))
    ann = math.sqrt(252.0)
    sr_d = sr / ann  # 日频 Sharpe
    var = (1.0 - skew * sr_d + (kurt - 1.0) / 4.0 * sr_d * sr_d) / (n_obs - 1)
    var = max(var, 1e-12)
    sd = math.sqrt(var)
    gamma = 0.5772156649015329
    if n_trials <= 1:
        e_max_d = 0.0  # 单次试验：E[max] = 0（无多重检验膨胀）
    else:
        q1 = _norm_ppf(1.0 - 1.0 / n_trials)
        q2 = _norm_ppf(1.0 - 1.0 / (n_trials * math.e))
        e_max_d = sd * ((1.0 - gamma) * q1 + gamma * q2)   # 日频期望极大值
    dsr = _norm_cdf((sr_d - e_max_d) / sd)
    return {
        "sr0": e_max_d * ann,
        "dsr": dsr,
        "n_trials": n_trials,
        "n_obs": n_obs,
    }


def trial_adjusted_threshold(n_trials: int, n_obs: int, z: float = 1.645) -> float:
    """给定试验次数下的「显著 Sharpe 门槛」（年化），供闸门②使用。

    在 DSR 框架下：门槛 = sr0 + z·sd(SR̂)，其中
      * sr0 = E[max SR₀ | n_trials] —— H0 下试 n 次后最大 Sharpe 的期望；
      * sd(SR̂) = 1/√(n_obs-1) —— 标准假设（sr=0, skew=0, kurt=3）下的
        Sharpe 估计标准误；
      * z = 显著性分位（默认 1.645 ≈ 单尾 5%；2.33 ≈ 单尾 1%）。

    量级自检：n_trials=1、n_obs=250 时，sr0=0，门槛 ≈ 1.645/√249 ≈ 0.104
    （日频）→ 年化 ≈ 1.645·√(252/249) ≈ 1.65；z=2.33 时 ≈ 2.34，
    与教科书「Sharpe 显著需 ≈ t 值 × √(1/n_years)」量级相符。
    """
    d = deflated_sharpe(0.0, n_trials, n_obs)
    sd_ann = math.sqrt(252.0 / max(1, n_obs - 1))  # 年化标准误
    return d["sr0"] + z * sd_ann


def _norm_ppf(p: float) -> float:
    """标准正态分位数 Φ⁻¹(p)，Acklam (2010) 有理逼近，精度 ~1e-9。"""
    if not 0.0 < p < 1.0:
        raise ValueError(f"p 必须在 (0,1)，得到 {p}")
    a = [-3.969683028665376e+01, 2.209460984245205e+02,
         -2.759285104469687e+02, 1.383577518672690e+02,
         -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02,
         -1.556989798598866e+02, 6.680131188771972e+01,
         -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01,
         -2.400758277161838e+00, -2.549732539343734e+00,
         4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01,
         2.445134137142996e+00, 3.754408661907416e+00]
    plow, phigh = 0.02425, 1.0 - 0.02425
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    if p > phigh:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
        (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)

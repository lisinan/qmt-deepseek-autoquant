# -*- coding: utf-8 -*-
"""
技术指标库（纯 Python 实现，无第三方依赖，便于离线测试）。

约定：
- 输入是价格序列 list[float]（按时间升序）。
- 输出是等长 list[Optional[float]]，前段不足窗口处填 None。
- 永不抛异常；空输入返回空列表。
"""
from __future__ import annotations

import math
from typing import List, Optional

F = Optional[float]


def _none_list(n: int) -> List[F]:
    return [None] * n


# ---------------------------------------------------------------- 均线

def sma(values: List[float], period: int) -> List[F]:
    """简单移动平均。"""
    n = len(values)
    out: List[F] = _none_list(n)
    if n == 0 or period <= 0:
        return out
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def ema(values: List[float], period: int) -> List[F]:
    """指数移动平均。"""
    n = len(values)
    out: List[F] = _none_list(n)
    if n == 0 or period <= 0:
        return out
    k = 2.0 / (period + 1)
    prev: Optional[float] = None
    for i, v in enumerate(values):
        prev = v if prev is None else v * k + prev * (1 - k)
        out[i] = prev
    return out


# ---------------------------------------------------------------- RSI

def rsi(values: List[float], period: int = 14) -> List[F]:
    """Wilder RSI。返回等长序列，前 period 个为 None。"""
    n = len(values)
    out: List[F] = _none_list(n)
    if n <= period:
        return out
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, n):
        chg = values[i] - values[i - 1]
        gains.append(max(chg, 0.0))
        losses.append(max(-chg, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def _rsi() -> float:
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - 100.0 / (1.0 + rs)

    out[period] = _rsi()
    for i in range(period, n - 1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = _rsi()
    return out


# ---------------------------------------------------------------- MACD

def macd(values: List[float], fast: int = 12, slow: int = 26,
         signal: int = 9):
    """MACD。返回 (dif, dea, hist) 三个等长序列。"""
    n = len(values)
    dif = _none_list(n)
    dea = _none_list(n)
    hist = _none_list(n)
    if n == 0:
        return dif, dea, hist
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    dif_raw: List[F] = _none_list(n)
    for i in range(n):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            dif_raw[i] = ema_fast[i] - ema_slow[i]
    # DEA = EMA(dif, signal)，跳过 None 头
    valid_dif = [d for d in dif_raw if d is not None]
    if valid_dif:
        dea_valid = ema(valid_dif, signal)
        start = n - len(valid_dif)
        for j, d in enumerate(dea_valid):
            idx = start + j
            dea[idx] = d
            if d is not None and dif_raw[idx] is not None:
                hist[idx] = dif_raw[idx] - d
    return dif_raw, dea, hist


# ---------------------------------------------------------------- KDJ

def kdj(highs: List[float], lows: List[float], closes: List[float],
        period: int = 9):
    """KDJ。返回 (k, d, j)。使用国内常用 SMA 平滑。"""
    n = len(closes)
    k: List[F] = _none_list(n)
    d: List[F] = _none_list(n)
    j: List[F] = _none_list(n)
    if n < period:
        return k, d, j
    rsv: List[float] = []
    for i in range(n):
        lo = min(lows[max(0, i - period + 1): i + 1])
        hi = max(highs[max(0, i - period + 1): i + 1])
        rsv.append(50.0 if hi == lo else (closes[i] - lo) / (hi - lo) * 100.0)
    prev_k = 50.0
    prev_d = 50.0
    for i in range(n):
        prev_k = 2 / 3 * prev_k + 1 / 3 * rsv[i]
        prev_d = 2 / 3 * prev_d + 1 / 3 * prev_k
        k[i] = prev_k
        d[i] = prev_d
        j[i] = 3 * prev_k - 2 * prev_d
    return k, d, j


# ---------------------------------------------------------------- BOLL

def boll(values: List[float], period: int = 20, std_mult: float = 2.0):
    """布林带。返回 (mid, upper, lower)。"""
    n = len(values)
    mid = sma(values, period)
    upper: List[F] = _none_list(n)
    lower: List[F] = _none_list(n)
    for i in range(period - 1, n):
        window = values[i - period + 1: i + 1]
        mean = sum(window) / period
        var = sum((x - mean) ** 2 for x in window) / period
        sd = math.sqrt(var)
        upper[i] = mean + std_mult * sd
        lower[i] = mean - std_mult * sd
    return mid, upper, lower


# ---------------------------------------------------------------- ATR

def atr(highs: List[float], lows: List[float], closes: List[float],
        period: int = 14) -> List[F]:
    """平均真实波幅 (Wilder)。"""
    n = len(closes)
    out: List[F] = _none_list(n)
    if n == 0:
        return out
    trs: List[float] = []
    for i in range(n):
        if i == 0:
            trs.append(highs[i] - lows[i])
        else:
            trs.append(max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            ))
    if len(trs) < period:
        return out
    prev = sum(trs[:period]) / period
    out[period - 1] = prev
    for i in range(period, n):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


# ---------------------------------------------------------------- VWAP

def vwap(typicals: List[float], volumes: List[int]) -> List[F]:
    """累计 VWAP（从序列起点累计到当前）。"""
    n = len(typicals)
    out: List[F] = _none_list(n)
    cum_pv = 0.0
    cum_v = 0
    for i in range(n):
        v = volumes[i] if i < len(volumes) else 0
        cum_pv += typicals[i] * v
        cum_v += v
        out[i] = cum_pv / cum_v if cum_v > 0 else None
    return out


def vwap_roll(typicals: List[float], volumes: List[int],
              window: int = 0) -> List[F]:
    """滚动 N 日 VWAP（**平移不变**）。window <= 0 ⇒ 退化为累计 VWAP。

    【2026-10-12 PM-EVOLVE 第 27 轮 · 第 13 次同型口径缺陷修复】

    缺陷：``vwap`` 是「**从序列起点**累计」的均价 —— 它的数值**取决于你把序列
    从哪一天开始算**。而实盘 `DailyContext` 只拉 `DAILY_COUNT=120` 根日线，
    回测用全样本 901 根 ⇒ 同一只股票、同一天，两侧 VWAP 值系统性不同
    ⇒ vwap 因子（0/1/2，6 因子里权重最大的一项）不同 ⇒ score 不同。

    实测铁证（scripts/_pm_score_align_20261012.py，23 只 × 近 250 根）：
      · 35.1%（2017/5750）的 (标的, 交易日) 上 live 与回测 score **不同**；
      · 差异 **100%** 来自 vwap 因子的 0/1/2 迁移（0→2 有 941 次、2→0 有 836 次）；
      · 回测判定买入（score ≥ 4.0）的 2684 个 (标的, 交易日) 里，
        **482 个（18.0%）在实盘口径下 < 4.0 ⇒ 实盘会漏买**。

    修复：改滚动窗口 VWAP —— 只要序列尾部对齐，无论从哪天开始算都
    **逐位相同**（满足 R26 立下的「不变性检验」判据）。预热区（i < window-1）
    回退到累计值，保证 `window<=0` 时与 ``vwap`` **逐位相同**（零行为变化）。
    """
    n = len(typicals)
    # 前缀和：O(n) 而非 O(n*window)，避免拖慢 walk-forward 网格
    cpv = [0.0] * (n + 1)
    cvv = [0.0] * (n + 1)
    for i in range(n):
        vv = volumes[i] if i < len(volumes) else 0
        cpv[i + 1] = cpv[i] + typicals[i] * vv
        cvv[i + 1] = cvv[i] + vv

    # window < 0：**模拟实盘**——序列只有最近 W 根（DAILY_COUNT=120），
    # 每个 bar i 的 VWAP 从 max(0, i-W+1) 累计到 i。用于量测
    # 「实盘口径 vs 回测口径」的真实差距（回测有全样本，实盘只有 120 根）。
    if window < 0:
        w = -window
        out: List[F] = _none_list(n)
        for i in range(n):
            lo = max(0, i - w + 1)
            pv = cpv[i + 1] - cpv[lo]
            vv2 = cvv[i + 1] - cvv[lo]
            out[i] = pv / vv2 if vv2 > 0 else None
        return out

    cum = vwap(typicals, volumes)
    if window == 0 or window >= n:
        return cum
    out = list(cum)
    for i in range(window - 1, n):
        lo = i - window + 1
        pv = cpv[i + 1] - cpv[lo]
        vv = cvv[i + 1] - cvv[lo]
        out[i] = pv / vv if vv > 0 else None
    return out


# ---------------------------------------------------------------- 工具

def slope(values: List[F], lookback: int = 5) -> Optional[float]:
    """最近一段有效值的线性斜率（每根 bar 变化量，近似）。"""
    valid = [v for v in values if v is not None]
    if len(valid) < 2:
        return None
    tail = valid[-lookback:]
    if len(tail) < 2:
        tail = valid[-2:]
    first, last = tail[0], tail[-1]
    return (last - first) / (len(tail) - 1) if first else None


def pct_change(values: List[F], lookback: int = 1) -> Optional[float]:
    """最近有效值相对 lookback 根前的涨跌幅。"""
    valid = [v for v in values if v is not None]
    if len(valid) < lookback + 1:
        return None
    prev, last = valid[-lookback - 1], valid[-1]
    if not prev:
        return None
    return (last - prev) / prev


def last(values: List[F]) -> Optional[float]:
    """最后一个非 None 值。"""
    for v in reversed(values):
        if v is not None:
            return v
    return None


def cross_above(a: List[F], b: List[F]) -> bool:
    """a 是否刚从下方上穿 b（看最后两个有效交叉点）。"""
    diffs = []
    for i in range(len(a)):
        if a[i] is not None and b[i] is not None:
            diffs.append(a[i] - b[i])
    if len(diffs) < 2:
        return False
    return diffs[-2] <= 0 < diffs[-1]


def cross_below(a: List[F], b: List[F]) -> bool:
    """a 是否刚从上方下穿 b。"""
    diffs = []
    for i in range(len(a)):
        if a[i] is not None and b[i] is not None:
            diffs.append(a[i] - b[i])
    if len(diffs) < 2:
        return False
    return diffs[-2] >= 0 > diffs[-1]

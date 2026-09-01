# -*- coding: utf-8 -*-
"""core/indicators.py 单元测试（纯 Python 断言，无 pytest 依赖）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import indicators as I


def _approx(a, b, tol=1e-6):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) < tol


# ============================================================ SMA

def test_sma_basic():
    s = I.sma([1, 2, 3, 4, 5], 3)
    assert s[0] is None and s[1] is None
    assert _approx(s[2], 2.0)
    assert _approx(s[3], 3.0)
    assert _approx(s[4], 4.0)


def test_sma_short_input():
    s = I.sma([1, 2], 5)
    assert all(v is None for v in s)


def test_sma_period_one():
    s = I.sma([1, 2, 3], 1)
    assert s == [1.0, 2.0, 3.0]


# ============================================================ EMA

def test_ema_converges():
    s = I.sma([10] * 20, 5)
    e = I.ema([10] * 20, 5)
    assert _approx(s[-1], 10.0)
    assert _approx(e[-1], 10.0)


# ============================================================ RSI

def test_rsi_all_up():
    r = I.rsi([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16], 14)
    # 全部上涨 → RSI = 100
    assert _approx(r[-1], 100.0)


def test_rsi_all_down():
    r = I.rsi([16, 15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1], 14)
    assert _approx(r[-1], 0.0)


def test_rsi_short_input():
    r = I.rsi([1, 2, 3], 14)
    assert all(v is None for v in r)


# ============================================================ MACD

def test_macd_shapes():
    closes = [float(i) for i in range(60)]
    dif, dea, hist = I.macd(closes, 12, 26, 9)
    assert len(dif) == len(dea) == len(hist) == 60
    # 线性上升时 hist 后期为正
    assert dif[-1] > 0
    assert dea[-1] is not None


# ============================================================ KDJ

def test_kdj_shapes_and_bounds():
    closes = [10 + i * 0.1 for i in range(40)]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    k, d, j = I.kdj(highs, lows, closes, 9)
    assert len(k) == len(d) == len(j) == 40
    # 末段 K/D/J 在合理范围
    for v in (k[-1], d[-1], j[-1]):
        assert v is not None
        assert -20 < v < 150


# ============================================================ BOLL

def test_boll():
    closes = [10 + (i % 5) for i in range(40)]
    mid, up, lo = I.boll(closes, 20, 2.0)
    assert mid[-1] is not None
    assert up[-1] > mid[-1] > lo[-1]


# ============================================================ ATR

def test_atr_positive():
    closes = [10 + i * 0.1 for i in range(30)]
    highs = [c + 0.2 for c in closes]
    lows = [c - 0.2 for c in closes]
    a = I.atr(highs, lows, closes, 14)
    assert a[-1] is not None and a[-1] > 0


# ============================================================ VWAP

def test_vwap():
    tps = [10.0, 11.0, 12.0]
    vols = [100, 200, 300]
    v = I.vwap(tps, vols)
    expected = [(10 * 100) / 100, (10 * 100 + 11 * 200) / 300,
                (10 * 100 + 11 * 200 + 12 * 300) / 600]
    for i, e in enumerate(expected):
        assert _approx(v[i], e)


# ============================================================ 工具

def test_slope_up():
    v = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert I.slope(v) > 0


def test_pct_change():
    v = [1.0, 1.1, 1.21]
    assert _approx(I.pct_change(v, 1), 0.1, 1e-3)


def test_last_none():
    assert I.last([None, None, None]) is None
    assert I.last([None, 1.0, 2.0]) == 2.0


def test_cross():
    a = [1.0, 2.0, 3.0, 4.0]
    b = [3.0, 3.0, 3.0, 3.0]
    # a 从下方上穿 b：a[2]=3.0 不严格 >，a[3]=4.0 > 3.0
    # 但需要看前一个 ≤ 0 的 diff
    # a[2]-b[2]=0 (≤0), a[3]-b[3]=1 (>0) → cross_above = True
    assert I.cross_above(a, b)
    assert not I.cross_below(a, b)
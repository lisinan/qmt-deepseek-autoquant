# -*- coding: utf-8 -*-
"""【2026-10-12 第 27 轮 PM-EVOLVE】VWAP 因子「累计 vs 滚动」口径对齐守卫。

## 本轮发现的缺陷（本项目第 13 次同型「口径/量测」类）

6 因子评分里的 **vwap 因子（0/1/2，6 因子中权重最大）** 用 ``I.vwap``，而
``I.vwap`` 是「**从序列起点累计**」的均价 —— 它的数值**取决于序列从哪一天
开始算**。而实盘 ``DailyContext`` 只拉 ``DAILY_COUNT=120`` 根日线，回测用全样本
（901/911 根）⇒ 同一标的、同一天，两侧 VWAP 值系统性不同 ⇒ vwap 因子不同
⇒ score 不同。

实测（23 只 × 近 250 根，2026-10-12）：
  · 35.1%（2017/5750）的 (标的,交易日) 两侧 score **不同**，差异 **100%** 来自
    vwap 因子的 0/1/2 迁移（0→2 有 941 次、2→0 有 836 次）；
  · 回测判定买入（score≥4.0）的 2684 个信号里 **482 个（18.0%）在实盘口径下
    <4.0 ⇒ 实盘漏买**。

## 但量化后的结论是「真实而无关紧要」（R23 第 12 条教训的直接应用）

用 tushare 真实面板（911 根）在自研验证器上实测：
  · 滚动修复档 vwap_window = 20/40/60/90/120 ⇒ dSh −0.020/−0.033/−0.020/−0.033/−0.008
  · **模拟实盘档** vwap_window = −30/−60/−120 ⇒ dSh −0.020/−0.020/−0.008
  ⇒ **全部未过 +0.10 闸门，且现状（0 = 累计）就是 argmax** ⇒ REJECTED，轴闭合。

## 本文件锁死的语义（R17 守则：默认中性 + 开启后真变 + 不变性）
  1. ``vwap_roll(w=0)`` 必须与旧 ``I.vwap`` **逐位相同**（默认零行为变化）
  2. ``vwap_roll(w<0)`` 必须等于「只取最近 W 根从头累计」（模拟实盘口径）
  3. ★ **平移不变性**：滚动档下，同一序列的「全量」与「尾部切片」末值必须
     逐位相同；累计档下则**允许**不同（这正是缺陷本身，用反例钉住）
  4. 回测 ``score_daily_series`` 默认（vwap_window=0 / vwap_factor_fixed=−1）
     必须与历史实现逐位相同
  5. ``vwap_factor_fixed>=0`` 开启后必须真变（防死代码）
  6. 实盘侧 ``DailyContext._compute`` 在默认参数下与历史 ``I.vwap`` 同值
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import indicators as I  # noqa: E402
from strategy.backtest_daily import (  # noqa: E402
    BacktestConfig, score_daily, score_daily_series)
from strategy.daily_context import DailyContext  # noqa: E402


def _seq(n: int = 300, seed: int = 7):
    """确定性合成 OHLCV（不依赖网络/真实行情，CI 可复现）。"""
    import random
    rnd = random.Random(seed)
    c = [100.0]
    for _ in range(n - 1):
        c.append(max(5.0, c[-1] * (1 + rnd.uniform(-0.03, 0.031))))
    h = [x * 1.012 for x in c]
    l = [x * 0.988 for x in c]
    o = [x * (1 + (rnd.uniform(-0.004, 0.004))) for x in c]
    v = [int(1e6 * (1 + rnd.uniform(-0.35, 0.45))) for _ in c]
    return o, h, l, c, v


def test_vwap_roll_zero_is_bitwise_identical_to_cumulative():
    """① window=0 必须与旧 I.vwap 逐位相同（默认零行为变化）。"""
    o, h, l, c, v = _seq()
    typ = [(a + b + d) / 3 for a, b, d in zip(h, l, c)]
    old = I.vwap(typ, v)
    new = I.vwap_roll(typ, v, 0)
    assert len(old) == len(new)
    for i, (a, b) in enumerate(zip(old, new)):
        assert a == b, f"index {i}: {a} != {b}"


def test_vwap_roll_negative_window_simulates_live_short_history():
    """② window<0 必须等于「只取最近 W 根、从头累计」（模拟实盘 120 根）。"""
    o, h, l, c, v = _seq(n=200)
    typ = [(a + b + d) / 3 for a, b, d in zip(h, l, c)]
    W = 120
    got = I.vwap_roll(typ, v, -W)
    for i in range(len(c)):
        lo = max(0, i - W + 1)
        pv = sum(typ[j] * v[j] for j in range(lo, i + 1))
        vv = sum(v[lo:i + 1])
        exp = pv / vv if vv > 0 else None
        assert got[i] == exp or (
            got[i] is not None and exp is not None
            and abs(got[i] - exp) < 1e-9), f"i={i}: {got[i]} != {exp}"


def test_rolling_vwap_is_translation_invariant_cumulative_is_not():
    """③★ 核心：滚动档平移不变；累计档（现状）平移可变 —— 用反例钉住缺陷。"""
    o, h, l, c, v = _seq(n=400, seed=11)
    typ = [(a + b + d) / 3 for a, b, d in zip(h, l, c)]
    tail = 200
    typ_t = typ[-tail:]
    v_t = v[-tail:]

    # 滚动档：全量末值 == 尾部切片末值（逐位相同）
    full = I.vwap_roll(typ, v, 60)[-1]
    cut = I.vwap_roll(typ_t, v_t, 60)[-1]
    assert full is not None and cut is not None
    assert abs(full - cut) < 1e-12, f"滚动档不满足平移不变: {full} != {cut}"

    # 累计档：两者**应当**不同（这正是缺陷；若相同说明测试样本退化了）
    c_full = I.vwap(typ, v)[-1]
    c_cut = I.vwap(typ_t, v_t)[-1]
    assert abs(c_full - c_cut) > 1e-9, (
        "累计 VWAP 在本样本上恰好相同，反例退化，无法钉住缺陷 —— 请换 seed")


def test_score_daily_series_default_is_bitwise_unchanged():
    """④ 默认参数下 score_daily_series 必须与历史实现逐位相同。"""
    o, h, l, c, v = _seq(n=260)
    a = score_daily_series(o, h, l, c, v)
    b = score_daily_series(o, h, l, c, v, 0, -1.0)
    assert len(a) == len(b)
    for i, (x, y) in enumerate(zip(a, b)):
        assert x[0] == y[0], f"i={i}: score {x[0]} != {y[0]}"
        assert x[1] == y[1], f"i={i}: factors {x[1]} != {y[1]}"


def test_score_daily_default_matches_series_last():
    """④b score_daily 默认与 score_daily_series 末根一致（两侧同口径）。"""
    o, h, l, c, v = _seq(n=200)
    s, f = score_daily(o, h, l, c, v)
    arr = score_daily_series(o, h, l, c, v)
    assert s == arr[-1][0]
    assert f == arr[-1][1]


def test_vwap_rolling_window_really_changes_score():
    """⑤ 滚动窗口开启后必须真变（防代理退化成死代码）。"""
    o, h, l, c, v = _seq(n=400, seed=23)
    base = score_daily_series(o, h, l, c, v, 0)
    roll = score_daily_series(o, h, l, c, v, 60)
    diff = sum(1 for x, y in zip(base, roll) if x[0] != y[0])
    assert diff > 0, "vwap_window=60 与现状逐位相同 ⇒ 代理是死代码"


def test_vwap_factor_fixed_off_by_default_and_really_works():
    """⑥ vwap_factor_fixed 默认 −1（现状）；>=0 时该因子恒取该值。"""
    o, h, l, c, v = _seq(n=200)
    base = score_daily_series(o, h, l, c, v)
    same = score_daily_series(o, h, l, c, v, 0, -1.0)
    assert [x[0] for x in base] == [x[0] for x in same], "默认档不是零行为变化"

    fixed0 = score_daily_series(o, h, l, c, v, 0, 0.0)
    assert all(x[1]["vwap"] == 0.0 for x in fixed0), "fixed=0 未把 vwap 因子钉为 0"
    assert [x[0] for x in fixed0] != [x[0] for x in base], "fixed=0 未真变"


def test_daily_context_default_matches_historical_cumulative_vwap():
    """⑦ 实盘侧读取点：默认（vwap_window 未配置）必须与旧 I.vwap 同值。"""
    o, h, l, c, v = _seq(n=300)
    d = {"open": o, "high": h, "low": l, "close": c, "volume": v}
    feat = DailyContext._compute_for_test("TEST.SZ", d)
    typ = [(a + b + x) / 3 for a, b, x in zip(h, l, c)]
    expected = I.last(I.vwap(typ, v)) or 0.0
    assert abs(feat.vwap - expected) < 1e-9, (
        f"实盘侧默认口径漂移: {feat.vwap} != {expected}")


def test_backtest_config_defaults_are_neutral():
    """⑧ BacktestConfig 两个研究开关默认必须是中性值（改了会静默改基线）。"""
    cfg = BacktestConfig()
    assert cfg.vwap_window == 0, "vwap_window 默认必须为 0（累计 VWAP = 现状）"
    assert cfg.vwap_factor_fixed == -1.0, (
        "vwap_factor_fixed 默认必须为 -1（关闭 = 现状）")

# -*- coding: utf-8 -*-
"""【2026-10-06 PM-EVOLVE 第 18 轮】拼接口径（span）量测守卫 + 「不追高」代理守卫。

★ 本轮最重要的发现（量测口径，非 alpha）
------------------------------------------------------------------
KPI #1「wf 样本外均值 Sharpe」用的是**折均值口径**：每个折单独算 Sharpe 再取
算术平均。但折只有 60~120 根日线、**3~36 笔成交**，而 Sharpe 是「均值/标准差」
型比率统计量 ⇒ 小样本下估计噪声极大，口径本身系统性偏离策略真实表现。

铁证：60×9 与 90×6 覆盖的是**完全相同**的 540 根（20240712~20260930）——
同一段行情、同一套参数、同样 146 笔成交。然而：
    折均值口径：60×9 = 1.409 / 90×6 = 1.660   ← 相差 0.251
    拼接口径  ：两者**完全相同**  = 1.998
⇒ 0.251 的差异 **100% 来自划分方式**，是统计假象而非策略差异。
极端个例：60×9 F9 仅 **3 笔成交** 却报出 Sharpe −4.11，单折把 9 折均值
拉低约 0.46，直接决定 KPI #1 是否达标。

后果：KPI #1 缺口（折均值 1.672 vs 目标 2.0 = −0.328）在拼接口径下只剩
**1.980 vs 2.0 = −0.020**。⇒ 继续用单参数网格去「修复」这个缺口，
本质上是在拟合小样本噪声——这正是 18 轮单参数网格无果的真正原因。

本文件锁死 4 条语义：
  1. 拼接口径只取决于「数据段」，不取决于折划分（60×9 与 90×6 必须逐位相同）
  2. 同一段数据下，**折均值可以不同**（这正是要证明的假象）
  3. `entry_max_day_gain_pct` 默认 1.0 ⇒ **零行为变化**
  4. `entry_max_day_gain_pct` 收紧后**必须真的改变结果**（非死代码）
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

_PANEL = None


def _panel():
    """惰性加载一次对齐面板（含数据预取），供多个用例复用。"""
    global _PANEL
    if _PANEL is None:
        codes = W.wide_universe()
        data = W.preload(codes + [W.MARKET_INDEX_CODE, "000300.SH"], 900)
        d0, panel = W.aligned_universe(data, codes)
        _PANEL = (codes, panel, len(d0))
    return _PANEL


def _bt(cfg, dset):
    ks = [k for k in dset.keys() if k not in W.INDEX_CODES]
    return run_backtest(ks, cfg, count=900, preloaded=dset)


def _span(fold, nf, cfg=None):
    """拼接口径：把该窗口覆盖的连续区间当一条曲线跑一次回测。"""
    codes, panel, n = _panel()
    subs = W.make_folds(panel, codes, n, fold, nf)
    lo = min(s for s, _e, _d in subs) - W.FIXED_WARMUP
    hi = max(e for _s, e, _d in subs)
    return _bt(cfg or W.base_cfg(), W.slice_by_index(panel, lo, hi))


# --------------------------------------------------------------- 门锁 1/2

def test_span_identical_for_same_data_segment():
    """60×9 与 90×6 覆盖同一段 540 根 ⇒ 拼接口径必须逐位相同。"""
    s60, s90 = _span(60, 9), _span(90, 6)
    assert s60["sharpe"] == s90["sharpe"], (
        f"同段 540 根拼接 Sharpe 应相同：60x9={s60['sharpe']} vs 90x6={s90['sharpe']}")
    assert s60["total_return"] == s90["total_return"]
    assert s60["max_drawdown"] == s90["max_drawdown"]
    assert s60["n_trades"] == s90["n_trades"]


def test_fold_mean_differs_while_span_does_not():
    """折均值可以不同（划分假象），拼接必须相同（唯一真值）。"""
    codes, panel, n = _panel()
    base = W.base_cfg()
    fm60 = W.oos_stats(base, W.make_folds(panel, codes, n, 60, 9), _bt)[0]
    fm90 = W.oos_stats(base, W.make_folds(panel, codes, n, 90, 6), _bt)[0]
    assert round(fm60["mean_sharpe"], 6) != round(fm90["mean_sharpe"], 6), (
        "本用例的前提是两套划分的折均值确实不同；若相同则该轴失去判别力")
    assert _span(60, 9)["sharpe"] == _span(90, 6)["sharpe"]


# --------------------------------------------------------------- 门锁 3/4

def test_entry_max_day_gain_default_off_keeps_baseline():
    """新增过滤字段默认 1.0（=+100%，恒不成立）⇒ 零行为变化。"""
    assert BacktestConfig.entry_max_day_gain_pct == 1.0
    assert W.base_cfg().entry_max_day_gain_pct == 1.0
    codes, panel, n = _panel()
    base = W.base_cfg()
    explicit = replace(base, entry_max_day_gain_pct=1.0)
    a = W.oos_stats(base, W.make_folds(panel, codes, n, 90, 6), _bt)[0]
    b = W.oos_stats(explicit, W.make_folds(panel, codes, n, 90, 6), _bt)[0]
    assert a["mean_sharpe"] == b["mean_sharpe"]
    assert a["pos"] == b["pos"]
    assert a["worst"] == b["worst"]


def test_entry_max_day_gain_actually_changes_behavior():
    """收紧后必须真的改变结果，否则与 tp_atr_mult 同型（死代码）。"""
    codes, panel, n = _panel()
    base = W.base_cfg()
    tight = replace(base, entry_max_day_gain_pct=0.03)
    a = W.oos_stats(base, W.make_folds(panel, codes, n, 90, 6), _bt)[0]
    b = W.oos_stats(tight, W.make_folds(panel, codes, n, 90, 6), _bt)[0]
    assert a["mean_sharpe"] != b["mean_sharpe"], (
        "entry_max_day_gain_pct=0.03 未改变结果 ⇒ 该字段是死代码")
    # 方向：本轮实测为**负**（追高本身是 alpha 来源），锁死方向防日后误启用
    assert b["mean_sharpe"] < a["mean_sharpe"], (
        "不追高过滤应使 OOS 变差（实测 −0.340）；若变好说明基线或实现已变，需重裁")

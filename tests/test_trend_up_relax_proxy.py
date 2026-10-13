# -*- coding: utf-8 -*-
"""【2026-10-09 AM-EVOLVE 第 24 轮】入场日线闸门 trend_up 放宽/收紧档守卫。

## 为什么有这条轴

半日度量的实盘铁证：连续两个交易日（10-08 / 10-09）**开盘卖光后全天零 BUY、
100% 现金**。闸门通过率诊断（scripts/_am_gate_diag.py，23 只 × 900 根）：

    近 90 个交易日（20260527→20261008）
      至少 1 只通过 trend_up        ：69/90 = **76.7%**
      至少 2 只通过（= 生产宽度闸门 2）：49/90 = **54.4%**
    末 30 个交易日：pass=0 的天数占 **17/30 = 57%**
    trend_up 四分量单独通过率：above_ma20 37.3% / above_ma60 40.8% /
                              ma20_slope 39.1% / macd 45.2% ⇒ 联合仅 **10.97%**

⇒ 「低暴露 / KPI#4 不达标」的机制根因不是亏钱，而是**约一半交易日根本不参与**。

## 本轮实测结论（重要负面证据，该轴整体闭合）

90×6 单窗 dSh：
    放宽：no_macd −0.283 / no_slope −0.104 / no_ma60 −0.173 / any3 −0.428
    收紧：ma60s（额外要求 MA60 5 日斜率>0）**+0.050**（IS +164.5%→+174.6%）

四窗口共识（60x9 / 75x7 / 90x6 / 120x4）：
    ma60s  −0.084 / +0.031 / +0.050 / −0.002  均值 −0.001  最差 **−0.084**
    no_macd −0.486 / −0.276 / −0.283 / −0.482  最差 −0.486
    any3    −0.481 / −0.501 / −0.428 / −0.662  最差 −0.662

⇒ **放宽是灾难（−0.38~−0.52），收紧是窗口噪声（−0.084~+0.050）**。
  trend_up 的严格性本身就是 alpha 源，其「严格度」恰在最优，**任何方向都不要动**。
  本轮据此 REJECTED（最差窗 −0.084 未达 +0.10），开关保持默认 "none" 不动基线。

## 本文件锁死的 4 条语义（R17 定的「新增回测代理」守则）
  1. 默认值必须是 "none"（任何人误改都会静默改写全部基线）
  2. `"none"` ⇒ 结果**逐位等同**不传该字段（零行为变化）
  3. 各放宽档 ⇒ 结果**真的变了**（防代理退化成死代码）
  4. 各**放宽**档必须是**负效应**（dSh < 0）—— 把本轮结论钉成回归断言：
     若哪天放宽档变成正效应，说明市场体制或策略已改变，**必须重新审视**，
     而不是无脑接受。
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import strategy._evolve_wf as W  # noqa: E402
from strategy.backtest_daily import (  # noqa: E402
    BacktestConfig, run_backtest, trend_up_relaxed,
)


def _is_result(cfg):
    codes = W.wide_universe()
    data = W.preload(codes + [W.MARKET_INDEX_CODE, "000300.SH"], 900)
    _d0, panel = W.aligned_universe(data, codes)
    ks = [k for k in panel.keys() if k not in W.INDEX_CODES]
    return run_backtest(ks, cfg, count=900, preloaded=panel)


def test_default_is_none():
    """门锁 1：研究开关默认关闭，防止静默改写全部已钉基线。"""
    assert BacktestConfig().trend_up_relax == "none"
    assert W.base_cfg().trend_up_relax == "none"


def test_none_keeps_legacy_behaviour():
    """门锁 2：显式 "none" 必须与「不传该字段」逐位相同（零行为变化）。"""
    base = W.base_cfg()
    off = replace(base, trend_up_relax="none")
    r_base, r_off = _is_result(base), _is_result(off)
    for k in ("total_return", "sharpe", "max_drawdown", "n_trades", "win_rate"):
        assert r_base[k] == r_off[k], f"{k} 发生变化：{r_base[k]} vs {r_off[k]}"


def test_relax_really_changes_result():
    """门锁 3：各档必须真的改变结果（防代理退化成死代码）。"""
    base = W.base_cfg()
    for relax in ("no_macd", "no_slope", "no_ma60", "any3", "ma60s"):
        on = replace(base, trend_up_relax=relax)
        r_base, r_on = _is_result(base), _is_result(on)
        changed = (r_base["total_return"] != r_on["total_return"]
                   or r_base["n_trades"] != r_on["n_trades"])
        assert changed, f"trend_up_relax={relax} 未产生任何变化 ⇒ 代理已断开"


def test_loosening_is_harmful():
    """门锁 4：任何**放宽**档都必须是负效应（本轮实测 −0.104 ~ −0.428）。

    这是本轮最重要的负面证据：trend_up 的严格性 = alpha 源。
    若此断言失败 ⇒ 闸门严格度已不再是 alpha 源，必须重新做入场口径审计。
    """
    base = W.base_cfg()
    r_base = _is_result(base)
    for relax in ("no_macd", "no_slope", "no_ma60", "any3"):
        r_on = _is_result(replace(base, trend_up_relax=relax))
        assert r_on["sharpe"] < r_base["sharpe"], (
            f"放宽档 {relax} 的 IS Sharpe 未下降（{r_on['sharpe']:.3f} vs "
            f"{r_base['sharpe']:.3f}）⇒ 需重审入场闸门")
        # 【2026-10-13 R28】MDD 断言由「必须变深」改为「不得显著改善（≤+0.5pp）」。
        #   原因（实测，非放宽结论）：基线收紧到 momentum_min_mom=0.15 后，
        #   `no_slope` 档的 MDD 是 −7.00% vs 基线 −7.04%（**浅 0.04pp**，噪声级），
        #   而其 Sharpe 仍显著下降 1.8896→1.8417（−0.048）。
        #   即「放宽 trend_up = 损 alpha」的结论**依然成立**，只是 no_slope 这一
        #   个分量不再同时恶化回撤（入池收紧后斜率条件已冗余）。
        assert r_on["max_drawdown"] <= r_base["max_drawdown"] + 0.005, (
            f"放宽档 {relax} 的 IS MDD 反而显著改善（{r_on['max_drawdown']*100:.2f}%"
            f" vs {r_base['max_drawdown']*100:.2f}%）⇒ 需重审入场闸门")


def test_relaxed_truth_table_is_total():
    """真值表纯函数：档位组合必须与注释一致（回测/实盘共用同一张表）。"""
    assert trend_up_relaxed(True, True, True, True, "none") is True
    assert trend_up_relaxed(True, True, True, False, "none") is False
    # no_macd：d 可假
    assert trend_up_relaxed(True, True, True, False, "no_macd") is True
    assert trend_up_relaxed(False, True, True, True, "no_macd") is False
    # no_slope：c 可假
    assert trend_up_relaxed(True, True, False, True, "no_slope") is True
    assert trend_up_relaxed(True, False, True, True, "no_slope") is False
    # no_ma60：b 可假
    assert trend_up_relaxed(True, False, True, True, "no_ma60") is True
    assert trend_up_relaxed(False, True, True, True, "no_ma60") is False
    # any3：恰好 3 个成立即通过
    assert trend_up_relaxed(True, True, True, False, "any3") is True
    assert trend_up_relaxed(True, True, False, False, "any3") is False
    # 未知档位一律退化为严格 4-AND（保守，防拼写错误静默放宽闸门）
    assert trend_up_relaxed(True, True, True, False, "typo_xxx") is False

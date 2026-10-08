# -*- coding: utf-8 -*-
"""【2026-10-08 PM-EVOLVE 第 23 轮】离场执行时点口径（exit_next_open）守卫。

## 为什么有这条轴

文件头声明的回测语义是「信号在 day i 收盘产生，day i+1 开盘成交（避免未来函数）」，
**入场侧确实如此**（C 段收盘出信号 → A 段次日开盘成交）。但**离场侧** B 段是
「day i 的 high/low/close 判定 + day i 成交」——其中 ``trend_break`` 用**当日收盘价
cl** 判定 MA20 下穿 / 收盘跌破 exit_ma，却仍以**同一个 cl** 成交 ⇒ 少了一根 bar 的
执行延迟，是 exit 侧的前视偏差。

实盘铁证（2026-10-08，国庆后复市首日）：4 笔「趋势破位离场」全部落在
09:19:02~09:19:32 的 30 秒内，用的是**上一根已收盘日线**的判定、在**次日开盘后**
以盘中价成交。实盘不可能在当日收盘那一刻用当日收盘价卖出。

## 本轮实测结论（重要负面证据，勿重复投入）

把 trend_break 顺延到次日开盘成交后（23 只 × 900/1000 根对齐日线）：

    900 根：IS ret +164.49% → +164.70%  Sh 1.864 → 1.865  MDD -7.60% → -7.60%
    1000 根：IS ret +210.71% → +211.40%  Sh 1.976 → 1.979  MDD -7.11% → -7.11%
    OOS 90x6 均值 Sh：1.824 → 1.830（dSh **+0.006**）

⇒ exit 侧的一根 bar 前视偏差**真实存在但影响 ≈ 0**，不是「回测漂亮、实盘亏钱」的
收益杀手。**本轮据此 REJECTED（未达 +0.10 闸门），开关保持默认 False 不动基线。**

## 本文件锁死的 4 条语义（R17 定的「新增回测代理」守则：默认 off 零行为变化 +
开启后结果真变，两者都要钉死）
  1. 默认值必须是 False（任何人误改成 True 都会静默改写全部基线）
  2. ``exit_next_open=False`` ⇒ 结果**逐位等同**不传该字段（零行为变化）
  3. ``=True`` ⇒ 结果**真的变了**（防代理被无声断开、退化成死代码）
  4. 变化幅度必须是「小」的（< 10%），把本轮量测结论钉成回归断言：
     若哪天这条断言失败，说明 exit 时点突然变成大影响，**必须重新审视整个口径**
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


def _is_result(cfg):
    codes = W.wide_universe()
    data = W.preload(codes + [W.MARKET_INDEX_CODE, "000300.SH"], 900)
    _d0, panel = W.aligned_universe(data, codes)
    ks = [k for k in panel.keys() if k not in W.INDEX_CODES]
    return run_backtest(ks, cfg, count=900, preloaded=panel)


def test_default_is_false():
    """门锁 1：研究开关默认关闭，防止静默改写全部已钉基线。"""
    assert BacktestConfig().exit_next_open is False
    assert W.base_cfg().exit_next_open is False


def test_off_keeps_legacy_behaviour():
    """门锁 2：显式 False 必须与「不传该字段」逐位相同（零行为变化）。"""
    base = W.base_cfg()
    off = replace(base, exit_next_open=False)
    r_base, r_off = _is_result(base), _is_result(off)
    for k in ("total_return", "sharpe", "max_drawdown", "n_trades", "win_rate"):
        assert r_base[k] == r_off[k], f"{k} 发生变化：{r_base[k]} vs {r_off[k]}"


def test_on_really_changes_result():
    """门锁 3：True 必须真的改变结果（防代理退化成死代码）。"""
    base = W.base_cfg()
    on = replace(base, exit_next_open=True)
    r_base, r_on = _is_result(base), _is_result(on)
    changed = (r_base["total_return"] != r_on["total_return"]
               or r_base["n_trades"] != r_on["n_trades"])
    assert changed, "exit_next_open=True 未产生任何变化 ⇒ 代理已断开"


def test_impact_is_negligible():
    """门锁 4：影响必须是「小」的（本轮实测 dSh +0.006、IS 收益 +0.2pt）。

    若此断言失败 ⇒ exit 执行时点突然成为大影响项，必须重新做口径审计，
    而不是直接接受新基线。
    """
    base = W.base_cfg()
    on = replace(base, exit_next_open=True)
    r_base, r_on = _is_result(base), _is_result(on)
    d = abs(r_on["total_return"] - r_base["total_return"])
    assert d < 0.10, f"exit 时点影响过大（Δret={d*100:.2f}pt），须重审口径"
    ds = abs(r_on["sharpe"] - r_base["sharpe"])
    assert ds < 0.10, f"exit 时点影响过大（ΔSharpe={ds:.3f}），须重审口径"

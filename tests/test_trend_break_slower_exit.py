# -*- coding: utf-8 -*-
"""【2026-10-12 第 26 轮 AM-EVOLVE】趋势破位「更慢离场」双开关守卫。

## 为什么有这条轴

第 25 轮 PM 把退出侧「**更快**离场」整类闭合：静态 ATR 止损 k=1.5 为 −0.089、
k≥2.5 惰性；吊灯止损严格单调负（4.0 −0.019 → 1.0 −1.551）；hard_stop −0.12 惰性。
判词是「趋势策略的钱是骑出来的，提前下车即减 alpha；左尾已被 trend_break 截断完
（IS MDD 仅 −6.94%，预算 −20% 只用 35%）」。

本项目 26 轮的所有退出侧扫描**只朝「更紧」一个方向**走过，其反面从未被测过。
本轴即同一枚硬币的反面：**更慢离场**。两个正交剂量旋钮：

  * ``trend_break_confirm_days`` N>1：破位条件需**连续 N 个交易日**成立才离场
    （对冲震荡市「单日插针破 MA60 → 次日反弹」的 whipsaw）。
  * ``trend_break_buffer_pct``  b>0：判定线由 ``cl < MA60`` 放宽为
    ``cl < MA60×(1−b)``（与 ``trend_exit_ma`` 的均线**周期**轴正交）。

## 本文件锁死的语义（R17 守则：默认 off 零行为变化 + 开启后真变）
  1. 默认必须是 1 / 0.0（误改会静默改写全部已钉基线）
  2. 默认值 ⇒ 结果**逐位等同** ``base_cfg()``
  3. 开启 ⇒ 结果**真的变了**（防代理退化成死代码）
  4. 结构性方向：放宽离场 ⇒ 成交笔数必**不增**、平均持仓天数必**不减**
  5. 影响受控：不得把 IS 回撤推过生产预算 −20%（放宽离场天然抬高 MDD，
     但若直接击穿预算说明开关被误接成「关闭退出」级别的巨变）
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


def test_defaults_are_neutral():
    """门锁 1：两个开关的默认值必须等于「现状」（1 / 0.0）。"""
    c = BacktestConfig()
    assert c.trend_break_confirm_days == 1
    assert c.trend_break_buffer_pct == 0.0
    b = W.base_cfg()
    assert b.trend_break_confirm_days == 1
    assert b.trend_break_buffer_pct == 0.0


def test_off_keeps_legacy_behaviour():
    """门锁 2：显式默认值必须与 base_cfg 逐位完全相同（零行为变化）。"""
    base = W.base_cfg()
    off = replace(base, trend_break_confirm_days=1, trend_break_buffer_pct=0.0)
    r_base, r_off = _is_result(base), _is_result(off)
    for k in ("total_return", "sharpe", "max_drawdown", "n_trades", "win_rate"):
        assert r_base[k] == r_off[k], f"{k} 发生变化：{r_base[k]} vs {r_off[k]}"


def test_confirm_days_really_changes_and_slows_exit():
    """门锁 3+4：confirm_days=3 必须真变，且方向为「更慢离场」。

    更慢离场的两个可观测指纹：成交笔数**不增**（出场次数减少）、
    平均持仓天数**不减**。
    """
    # 【2026-10-13 R28】锚定 momentum_min_mom=0.0：本代理的「可观测性」是在
    #   0.0 基线下标定的。R28 把入池下限收紧到 0.15 后，confirm_days/buffer
    #   在**新基线下变为逐位惰性**（见 test_*_inert_under_tightened_pool），
    #   这是与 R26「trend_break 是持续状态、无 whipsaw 可回收」同向的新证据，
    #   而不是代理断开。故此处显式锚定原验证条件。
    base = replace(W.base_cfg(), momentum_min_mom=0.0)
    on = replace(base, trend_break_confirm_days=3)
    r_base, r_on = _is_result(base), _is_result(on)
    changed = (r_base["total_return"] != r_on["total_return"]
               or r_base["n_trades"] != r_on["n_trades"])
    assert changed, "trend_break_confirm_days=3 未产生任何变化 ⇒ 代理已断开"
    assert r_on["n_trades"] <= r_base["n_trades"], "放宽离场不应增加成交笔数"
    assert r_on.get("avg_hold", 0) >= r_base.get("avg_hold", 0), (
        "放宽离场不应缩短平均持仓天数")


def test_buffer_really_changes_and_slows_exit():
    """门锁 3+4：buffer=0.02 必须真变，且方向同为「更慢离场」。"""
    base = replace(W.base_cfg(), momentum_min_mom=0.0)   # 锚定理由同上（R28）
    on = replace(base, trend_break_buffer_pct=0.02)
    r_base, r_on = _is_result(base), _is_result(on)
    changed = (r_base["total_return"] != r_on["total_return"]
               or r_base["n_trades"] != r_on["n_trades"])
    assert changed, "trend_break_buffer_pct=0.02 未产生任何变化 ⇒ 代理已断开"
    assert r_on["n_trades"] <= r_base["n_trades"], "放宽离场不应增加成交笔数"


def test_impact_stays_within_drawdown_budget():
    """门锁 5：放宽离场会抬高 MDD，但不得击穿生产回撤预算 −20%。

    若击穿 ⇒ 开关被误接成「关闭退出」级别的巨变，须重做口径审计。
    """
    base = W.base_cfg()
    for cfg in (replace(base, trend_break_confirm_days=4),
                replace(base, trend_break_buffer_pct=0.03)):
        r = _is_result(cfg)
        assert r["max_drawdown"] > -0.20, (
            f"放宽离场击穿回撤预算：IS MDD={r['max_drawdown']*100:.2f}%")


def test_slower_exit_is_inert_under_tightened_momentum_pool():
    """【2026-10-13 R28 新事实】入池下限收紧到 0.15 后，「更慢离场」**完全惰性**。

    实测（910 根，P0 = momentum_min_mom 0.15）：
        confirm_days=1（现状） ret +131.93% Sh 1.8896 n=163
        confirm_days=3         ret +131.93% Sh 1.8896 n=163   ← 逐位相同
        buffer_pct=0.02        ret +131.93% Sh 1.8896 n=163   ← 逐位相同
    而在旧基线（0.0）下两者都会变（ret 128.38%→129.43%、n 170→169）。

    ⇒ 机理：收紧入池后**被剔除的正是那些「破位后立刻反弹」的摇摆仓**，
      剩下的仓位破位即持续破位 ⇒ 没有任何 whipsaw 可回收。
    这与 R26「trend_break 一旦成立必连续 ≥2 日成立、几乎全由 MA20<MA60 触发」
    是同一事实的**第二条独立证据**，并进一步说明：
      **退出侧在收紧入池后已无任何可调空间（快慢两向皆惰性）。**
    """
    base = W.base_cfg()
    assert base.momentum_min_mom == 0.15, "本用例的前提是 R28 已落盘的 0.15"
    for cfg in (replace(base, trend_break_confirm_days=3),
                replace(base, trend_break_confirm_days=5),
                replace(base, trend_break_buffer_pct=0.02),
                replace(base, trend_break_buffer_pct=0.05)):
        r = _is_result(cfg)
        assert r["total_return"] == _is_result(base)["total_return"], (
            "收紧入池后更慢离场应完全惰性；若此处变红 ⇒ 退出侧重新出现 whipsaw，"
            "须重做退出轴审计")
        assert r["n_trades"] == _is_result(base)["n_trades"]

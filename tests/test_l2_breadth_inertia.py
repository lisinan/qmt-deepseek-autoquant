# -*- coding: utf-8 -*-
"""【AM-EVOLVE R32 2026-10-15 午休】L2 杠杆基线下的「宽度 / 左尾」惰性图谱守卫。

★ 本轮两个候选都以 **REJECTED** 收场，但产出的是一张**惰性图谱**——它把两条
  长期争议轴在 L2 新尺度上的分辨率一次性钉死，避免日后重复投入：

  1. `max_positions`：**只有 2 和 3 有分辨率；≥4 近乎惰性，6≡8 逐位相同（饱和）**。
     机理：`momentum_top_n=3` 先把候选池截断到 3 只 ⇒ 并发上限几乎不可能绑定
     （残留的 0.25pt 差异来自「已持仓但已跌出 top_n」的第 4 只）。
     ⇒ **宽度真实旋钮是 `momentum_top_n`，不是 `max_positions`**。
  2. `max_positions=2`：四窗 dSh **+0.195/+0.052/+0.144/+0.028**（全正、均值 +0.105），
     IS MDD −13.05% → **−9.81%**、成交 164 → 118 笔；但 **IS Calmar 22.66 → 22.56
     基本不变** ⇒ 形态是**纯缩杠杆**（Sharpe 与 Calmar 同时不变、收益与回撤同步降），
     按 §六「尺度型改动不可套 +0.10 最差窗」仍不足以判为 alpha。且
     IS 收益 +295.7%→+221.4%、IS Sh 1.866→1.853（闸门① 双降 fail）、
     最差窗 +0.028 < +0.10（闸门② fail）⇒ **本轮不落盘**。
  3. `hard_stop_pct`：**在 L2 下 −0.10 … 关闭 全程逐位惰性**，仅 −0.08 有反应且
     四窗全负（均值 −0.226、最差 −0.279）。
     ⇒ **证伪了本轮开工假设**「L2 把 MDD 放大到 −13% 后，-18% 硬止损首次进入
     可达区间」：L2 的更大回撤**不是**由个股硬止损事件造成的，左尾仍被
     trend_break 完全截断（与 R25 在 L1 上的结论同型，L2 未改变它）。

本文件锁死 4 条语义（防代理/配置日后被无声改动而无人察觉）。
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import strategy._evolve_wf as W  # noqa: E402


def _is_result(cfg):
    """与其余基线守卫同源的确定性 tushare 面板（不依赖 xtdata 可达性）。"""
    from tests._baseline_tushare_panel import baseline_is_result
    return baseline_is_result(cfg)


def test_max_positions_ge4_is_saturated_under_topn3():
    """门锁 1：`momentum_top_n=3` 先把池截断到 3 ⇒ max_positions≥4 近乎无分辨率。

    实测（确定性 tushare 面板，L2 基线）：
        mp=4  ret 2.954982（−0.25pt）  Sh 1.875429
        mp=5  ret 2.957419（生产值）   Sh 1.866456
        mp=6  ret 2.957946             Sh 1.867065
        mp=8  ret 2.957946（与 6 逐位相同，已饱和）
    ⇒ 放宽并发上限到 4/6/8 对结果无实质影响，「宽度」不由 max_positions 决定。
    """
    r5 = _is_result(W.base_cfg())
    for v in (4, 6, 8):
        rv = _is_result(replace(W.base_cfg(), max_positions=v))
        assert abs(rv["total_return"] - r5["total_return"]) < 0.01, \
            (f"max_positions={v} 应因 top_n=3 夹紧而近乎等同 5，"
             f"实际 Δret={rv['total_return'] - r5['total_return']:+.6f}")
        assert abs(rv["sharpe"] - r5["sharpe"]) < 0.02, \
            f"max_positions={v} 的 Sharpe 应近乎等同，实际 {rv['sharpe']:.6f}"
    # 6 与 8 **逐位相同**（并发上限已饱和，再多也不会更宽）
    r6 = _is_result(replace(W.base_cfg(), max_positions=6))
    r8 = _is_result(replace(W.base_cfg(), max_positions=8))
    assert r6["total_return"] == r8["total_return"], "max_positions 6 与 8 应逐位相同（饱和）"
    assert r6["sharpe"] == r8["sharpe"], "max_positions 6 与 8 的 Sharpe 应逐位相同（饱和）"


def test_max_positions_2_has_real_resolution_and_is_pure_deleveraging():
    """门锁 2：max_positions=2 真的改变结果，且形态是**纯缩杠杆**（Calmar 不变）。"""
    base = _is_result(W.base_cfg())
    r2 = _is_result(replace(W.base_cfg(), max_positions=2))
    assert r2["total_return"] != base["total_return"], \
        "max_positions=2 应真的改变结果（防该轴整体退化成死代码）"
    # 回撤显著改善：−13.05% → −9.81%
    assert r2["max_drawdown"] > base["max_drawdown"] + 0.02, \
        (f"max_positions=2 应把 MDD 从 {base['max_drawdown']:.4f} 显著改善，"
         f"实际 {r2['max_drawdown']:.4f}")
    # 负面证据固化：IS 收益与 IS Sharpe 同时下降 ⇒ 闸门① fail ⇒ 本轮不落盘
    assert r2["total_return"] < base["total_return"], \
        "固化：max_positions=2 的 IS 收益是下降的（+295.7%→+221.4%）"
    assert r2["sharpe"] < base["sharpe"], \
        "固化：max_positions=2 的 IS Sharpe 是下降的（1.866→1.853）"
    # 形态判据：Calmar（ret/|MDD|）基本不变 ⇒ 纯缩放，不是 alpha
    c_base = base["total_return"] / abs(base["max_drawdown"])
    c_2 = r2["total_return"] / abs(r2["max_drawdown"])
    assert abs(c_2 - c_base) / c_base < 0.10, \
        f"max_positions=2 应为纯缩杠杆（Calmar 不变），实际 {c_base:.2f} → {c_2:.2f}"


def test_hard_stop_is_bitwise_inert_from_10pct_to_off_under_l2():
    """门锁 3：L2 下 hard_stop_pct 在 −0.10 … 关闭 全程**逐位**惰性（左尾已被 trend_break 截断）。"""
    r18 = _is_result(W.base_cfg())
    for v in (-0.10, -0.12, -0.15, -0.25, -0.99):
        rv = _is_result(replace(W.base_cfg(), hard_stop_pct=v))
        assert rv["total_return"] == r18["total_return"], \
            f"hard_stop_pct={v} 在 L2 下应逐位等同于 −0.18，实际 {rv['total_return']:.6f}"
        assert rv["sharpe"] == r18["sharpe"], \
            f"hard_stop_pct={v} 的 Sharpe 应逐位等同，实际 {rv['sharpe']:.6f}"


def test_hard_stop_08_is_the_only_active_and_is_worse():
    """门锁 4：−0.08 是唯一有反应的方向，且更差 ⇒ 收紧硬止损整条轴封死。"""
    base = _is_result(W.base_cfg())
    r08 = _is_result(replace(W.base_cfg(), hard_stop_pct=-0.08))
    assert r08["total_return"] != base["total_return"], \
        "hard_stop_pct=-0.08 应真的改变结果（防该轴连判别力都没有）"
    assert r08["sharpe"] < base["sharpe"], \
        (f"固化负面证据：−0.08 的 IS Sharpe 应低于 −0.18"
         f"（{r08['sharpe']:.4f} vs {base['sharpe']:.4f}）")

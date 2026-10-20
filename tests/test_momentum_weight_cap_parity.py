# -*- coding: utf-8 -*-
"""守卫：动量排名加权（momentum_weight）的第 18 例「回测有、实盘无」口径背离修复 + R39 裁决指纹。

★ 背景（2026-10-20 AM-EVOLVE 第 39 轮）
  回测器 ``backtest_daily.run_backtest`` 里，动量排名加权的乘子 ``wf`` 原本乘在
  **最终目标金额之后**（``tgt = min(budget/stop_dist, cap, fixed) * wf``），
  而实盘 ``risk/position_sizer.py::PositionSizer.size`` 的结构是
  ``min(risk_budget/stop_pct, equity*max_single_position_pct, max_order_amount)``
  —— **单仓上限一定会夹紧**。

  ⇒ wf=1.5 在回测里把单仓推到 ``0.40 × 1.5 = 0.60``，实盘却一定被夹回 0.40。
  回测度量的是一个「不存在单仓上限的世界」，属本项目第 18 例
  「回测有、实盘无」口径背离（同型：R14 合成行情 / R22 peak 脏值 / R25 固定金额 /
  R37 两条降级通道 / R38 回撤断路器）。

  修复：抽出纯函数 ``size_target_with_wf``，把 wf 作用在**风险预算**上并置于
  ``min(...)`` 之内 ⇒ 返回值恒 <= cap，与实盘同口径。

★ R39 裁决（本候选**未落盘**）：即便修正口径后，四窗共识最差窗 dSh 仍只有
  +0.003（pow1.0）/−0.049/−0.092/−0.107，且 IS 收益与 Calmar 随剂量单调下降。
  逐折指纹表明它「改善弱折、削弱强折」= **波动平滑型**改动而非 alpha。
  生产 ``STRATEGY_PARAMS`` **不得**出现 momentum_weight（实盘亦无读取点）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy._evolve_wf import base_cfg, replace  # noqa: E402
from strategy.backtest_daily import (  # noqa: E402
    momentum_rank_weights, size_target_with_wf)

CAP = 400_000.0          # equity 1,000,000 × max_single_position_pct 0.40
FIXED = 400_000.0


# ============================================ 一、纯函数：权重语义（快、确定性）

def test_rank_weights_mean_is_one_and_linear_at_pow1():
    """pow=1.0 必须与历史线性公式逐位相同（1.5 / 1.0 / 0.5），且均值恒为 1。"""
    mv = {"A": 0.50, "B": 0.30, "C": 0.10}
    w = momentum_rank_weights(["A", "B", "C"], mv, pow_=1.0)
    assert w == {"A": 1.5, "B": 1.0, "C": 0.5}, f"线性档应逐位兼容历史语义，实际 {w}"
    assert abs(sum(w.values()) / len(w) - 1.0) < 1e-12, "rank-weight 均值必须为 1.0"


def test_rank_weights_are_monotone_and_pow_widens_spread():
    """剂量语义：pow 越大头部越重、尾部越轻，且始终按动量降序。"""
    mv = {"A": 0.50, "B": 0.30, "C": 0.10}
    w1 = momentum_rank_weights(["A", "B", "C"], mv, pow_=1.0)
    w3 = momentum_rank_weights(["A", "B", "C"], mv, pow_=3.0)
    assert w3["A"] > w1["A"] and w3["C"] < w1["C"], (
        f"pow 增大应加大离散度，实际 {w1} -> {w3}")
    assert w1["A"] > w1["B"] > w1["C"], "权重必须按动量降序"
    for pw in (1.0, 1.5, 2.0, 3.0):
        w = momentum_rank_weights(["A", "B", "C"], mv, pow_=pw)
        assert abs(sum(w.values()) / len(w) - 1.0) < 1e-9, f"pow={pw} 均值必须为 1.0"


# ============================ 二、★ 核心不变式：单仓上限必须仍然夹紧

def test_size_target_with_wf_never_breaks_single_position_cap():
    """不变式：无论 wf 多大、预算多足，目标仓位恒 <= cap（与实盘 PositionSizer 同口径）。"""
    for wf in (0.2, 0.5, 1.0, 1.5, 2.0, 2.25, 3.0):
        for budget in (10_000.0, 40_000.0, 400_000.0, 4_000_000.0):
            for stop in (0.01, 0.05, 0.18, 0.5):
                got = size_target_with_wf(budget, stop, wf, CAP, FIXED)
                assert got <= CAP + 1e-9, (
                    f"单仓上限被突破：wf={wf} budget={budget} stop={stop} -> {got} > {CAP}")
                got_nv = size_target_with_wf(budget, stop, wf, CAP, FIXED,
                                             vol_sizing=False)
                assert got_nv <= CAP + 1e-9, "非 vol_sizing 分支同样不得突破上限"


def test_naive_post_cap_formula_would_break_cap_reverse_control():
    """★ 反向对照：证明上一条断言有判别力——旧的「后置乘法」确实会突破上限。

    若这条断言某天变成绿色（旧公式也不再超限），说明上面那条不变式测试已退化为
    恒真断言，必须重写（第 14 次同型「代理退化假阴性」的防护）。
    """
    budget, stop = 4_000_000.0, 0.05     # 预算极足 ⇒ 必然被 cap 夹紧
    naive = min(budget / stop, CAP, FIXED) * 1.5      # 旧实现
    assert naive > CAP * 1.4, (
        f"反向对照失效：旧公式应显著突破上限，实际 {naive} vs cap {CAP}")
    assert size_target_with_wf(budget, stop, 1.5, CAP, FIXED) <= CAP + 1e-9, (
        "修正后同一输入必须被夹紧")


# ==================================== 三、口径对齐：生产不得出现未落盘旋钮

def test_production_params_must_not_contain_momentum_weight():
    """momentum_weight 未过闸门 ⇒ 生产不得出现（实盘亦无读取点，落盘即死代码）。"""
    from config.settings import STRATEGY_PARAMS
    assert "momentum_weight" not in STRATEGY_PARAMS, (
        "momentum_weight 未通过晋升闸门（最差窗 dSh +0.003 < +0.10 且 IS 收益下降），"
        "且实盘 PositionSizer 无读取点 ⇒ 不得写入生产 STRATEGY_PARAMS")
    assert base_cfg().momentum_weight is False, "P0 基线必须保持 momentum_weight 关闭"


# ============================================ 四、R39 裁决指纹（确定性面板）

def _is_result(cfg):
    from tests._baseline_tushare_panel import baseline_is_result
    return baseline_is_result(cfg)


def test_momentum_weight_lowers_is_return_and_is_monotone_in_dose():
    """R39 指纹①：IS 收益下降，且随剂量单调劣化（拒绝「Sharpe 升即好」）。"""
    p0 = _is_result(base_cfg())
    r1 = _is_result(replace(base_cfg(), momentum_weight=True,
                            momentum_weight_pow=1.0))
    r3 = _is_result(replace(base_cfg(), momentum_weight=True,
                            momentum_weight_pow=3.0))
    assert r1["total_return"] < p0["total_return"], (
        f"R39 指纹：线性档 IS 收益应低于 P0，实际 {r1['total_return']} vs "
        f"{p0['total_return']}")
    assert r3["total_return"] < r1["total_return"], (
        f"剂量应单调劣化，实际 pow1.0={r1['total_return']} pow3.0={r3['total_return']}")
    # ★ 正向对照：候选必须真的产生了行为差异，否则代理是死的
    assert abs(r1["total_return"] - p0["total_return"]) > 1e-6, (
        "正向对照失败：momentum_weight=True 应与 P0 有可观测差异（代理可能没接上）")

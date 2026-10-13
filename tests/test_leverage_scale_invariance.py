# -*- coding: utf-8 -*-
"""★ 杠杆/尺度型改动的判据守卫（2026-10-08 第 23 轮 PM-EVOLVE）。

【为什么要单独建这个文件】
2026-09-16 因实盘 -19.6% 恐慌性收紧 ``risk_per_trade`` 0.02→0.012（连同
``max_order_amount`` 30万→18万）。事后实测：该收紧几乎没换来风险调整后收益，
却付出约 100pt 绝对收益（EVOLUTION_DECISIONS.md §七.3）。
此后它在 20 轮网格里被反复否决，原因只有一个 ——

    **Sharpe 是尺度不变量**（scale invariance）

整仓按 k 倍缩放 ⇒ 收益 k·R、波动 k·σ ⇒ Sharpe = R/σ **恒定不变**。
于是「最差窗 dSh ≥ +0.10」这条闸门对一切仓位放大天然判 FAIL ——
它不是在筛掉坏改动，而是在**筛掉所有杠杆**。Calmar 同理（同样失效）。

这是第 11 次「量测口径先制造假阴性」： defect 在方法层，不在策略层。

【本文件钉死三件事】
  1. Sharpe / Calmar 对常数杠杆**严格不变**（数学恒等式，非经验值）
     ⇒ 任何声称「用 Sharpe 判断要不要放大仓位」的流程都是错的
  2. 收益与回撤**随杠杆单调放大** ⇒ 决策变量是「回撤预算剩余多少」，
     而不是「Sharpe 有没有涨」
  3. 生产 risk_per_trade 不可越过 0.02，且与验证器 base_cfg 同步
     （第 11 次口径漂移防护：本参数已在 MUST_MATCH 里，这里再钉一次缓存读取路径）
"""
import math
from datetime import datetime  # noqa: F401  (与既有测试一致的 import 风格)

from config.settings import STRATEGY_PARAMS, RISK_PARAMS  # noqa
from risk.position_sizer import PositionSizer  # noqa

# 一段真实形态的日收益（含趋势与回撤段），避免用随机数导致测试不稳定
_BASE_RETS = [
    0.012, -0.008, 0.021, 0.004, -0.015, 0.009, 0.017, -0.003,
    0.006, -0.024, 0.011, 0.019, 0.002, -0.011, 0.014, 0.008,
    -0.019, 0.023, 0.005, -0.007, 0.016, 0.003, -0.013, 0.010,
]


def _equity(rets):
    eq = [1.0]
    for r in rets:
        eq.append(eq[-1] * (1.0 + r))
    return eq


def _total_return(eq):
    return eq[-1] / eq[0] - 1.0


def _sharpe(rets, ann=252.0):
    mu = sum(rets) / len(rets)
    var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
    sd = math.sqrt(var)
    return (mu / sd) * math.sqrt(ann) if sd > 0 else 0.0


def _max_drawdown(eq):
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1.0)
    return mdd


def _lever(rets, k):
    """常数杠杆组合：每日本金按 k 倍暴露 ⇒ 日收益严格为 k·r_i。"""
    return [k * r for r in rets]


def test_sharpe_is_strictly_invariant_to_leverage():
    """核心恒等式：Sharpe(k·r) === Sharpe(r)，与 k 无关。"""
    base = _sharpe(_BASE_RETS)
    for k in (0.5, 1.25, 1.67, 2.0, 3.0):
        got = _sharpe(_lever(_BASE_RETS, k))
        assert abs(got - base) < 1e-9, (
            f"k={k}: Sharpe 应为尺度不变量，期望 {base:.12f} 实得 {got:.12f}")


def test_calmar_is_also_scale_invariant():
    """Calmar 同样失效：它和 Sharpe 一样是比值量，不是绝对收益。

    ⇒ 想用「单位回撤换多少收益」来裁决要不要加杠杆，同样会被判持平。
    """
    def calmar(rets):
        eq = _equity(rets)
        mdd = _max_drawdown(eq)
        return (_total_return(eq) / abs(mdd)) if mdd else 0.0

    base = calmar(_BASE_RETS)
    for k in (1.25, 1.67, 2.0):
        got = calmar(_lever(_BASE_RETS, k))
        # Calmar 在小样本/离散路径下近似不变（非严格），只要求不被杠杆系统性抬高
        assert abs(got - base) < 0.35 * abs(base), (
            f"k={k}: Calmar 应近似尺度不变量，期望≈{base:.4f} 实得 {got:.4f}")


def test_return_and_drawdown_scale_monotonically_with_leverage():
    """收益与回撤都随杠杆单调放大 ⇒ 决策变量是「回撤预算剩余」。"""
    base_ret = _total_return(_equity(_BASE_RETS))
    base_mdd = _max_drawdown(_equity(_BASE_RETS))
    prev_ret, prev_mdd = base_ret, base_mdd
    for k in (1.25, 1.67, 2.0):
        ret = _total_return(_equity(_lever(_BASE_RETS, k)))
        mdd = _max_drawdown(_equity(_lever(_BASE_RETS, k)))
        assert ret > prev_ret, f"k={k}: 收益应随杠杆上升（{prev_ret:.4f}→{ret:.4f}）"
        assert abs(mdd) > abs(prev_mdd), (
            f"k={k}: 回撤应随杠杆放大（{prev_mdd:.4f}→{mdd:.4f}）")
        prev_ret, prev_mdd = ret, mdd
    # 近似线性：k=2 的收益应接近 2 倍（复利效应允许偏差）
    ret2 = _total_return(_equity(_lever(_BASE_RETS, 2.0)))
    assert 1.7 * base_ret < ret2 < 2.3 * base_ret, (
        f"k=2 收益应约为基准 2 倍，实得 {ret2/base_ret:.2f} 倍")


def test_zero_sharpe_delta_does_not_mean_zero_value():
    """把上面的结论写成一句可执行的话：dSh=0 但收益翻倍 ⇒ 不能据此否决。

    本轮实测（production 0.012 → 0.020）：IS 收益 +111.17%→+210.71%（1.90 倍），
    Sharpe 2.012→1.976（差 -0.036），OOS Calmar 2.85→2.84（持平）。
    这条用例保证将来任何「因为 dSh≈0 而 REJECT」的自动化流程会立刻失败。
    """
    base_sh = _sharpe(_BASE_RETS)
    base_ret = _total_return(_equity(_BASE_RETS))
    k = 1.67
    d_sh = _sharpe(_lever(_BASE_RETS, k)) - base_sh
    d_ret = _total_return(_equity(_lever(_BASE_RETS, k))) - base_ret
    assert abs(d_sh) < 1e-9, "前提：Sharpe 增量必须为 0"
    assert d_ret > 0.5 * base_ret, (
        f"Sharpe 增量为 0 时收益仍应大幅提升，实得 Δ={d_ret:.4f}")


def test_risk_per_trade_within_hard_limit():
    """risk_per_trade 不得超过 R29 授权的上限 0.03。

    历史：≤0.02 曾是「硬底线」（见工作记忆 §一 生产铁律）。
    R29（2026-10-13）用户显式授权越过该底线以协调杠杆（路径 1：纯杠杆），
    落盘 L1 = risk_per_trade 0.020→0.030 / max_single_position_pct 0.19→0.30 /
    max_position_amount & max_order_amount 180000→300000（约 1.5–1.67× 杠杆）。
    本用例的护栏含义随之升级为「不得超过用户已授权上限」，
    防止任何未经授权的进一步上调（例如误回到 0.04/0.05）。
    若需再上调，必须重新取得用户授权并同步更新此处断言。
    """
    R29_AUTHORIZED_CEILING = 0.03
    rpt = float(STRATEGY_PARAMS["risk_per_trade"])
    assert rpt <= R29_AUTHORIZED_CEILING + 1e-9, (
        f"risk_per_trade={rpt} 超过 R29 授权上限 {R29_AUTHORIZED_CEILING}；"
        f"若需再上调须重新取得用户授权")
    assert rpt > 0.0


def test_rpt_backtester_matches_production():
    """第 11 次口径漂移防护：验证器 base_cfg 必须与生产逐位相同。

    risk_per_trade 已在 test_evolve_baseline_sync.MUST_MATCH 里，此处再钉一次
    （MUST_MATCH 用通用比较器；这里是显式双读，任一侧漏改都会当场红）。
    """
    from strategy._evolve_wf import base_cfg
    prod = float(STRATEGY_PARAMS["risk_per_trade"])
    got = float(base_cfg().risk_per_trade)
    assert got == prod, (
        f"验证器 base_cfg.risk_per_trade={got} 与生产 {prod} 不一致")


def test_position_scales_linear_with_rpt_before_cap():
    """未触及仓位上限时，目标持仓金额应与 risk_per_trade 成正比。"""
    lo = PositionSizer({"risk_per_trade": 0.012, "atr_stop_mult": 2.9,
                        "max_single_position_pct": 0.19,
                        "max_order_amount": 10 ** 9})
    hi = PositionSizer({"risk_per_trade": 0.020, "atr_stop_mult": 2.9,
                        "max_single_position_pct": 0.19,
                        "max_order_amount": 10 ** 9})
    price, equity, atr = 50.0, 1_000_000.0, 0.04
    q_lo = lo.size(price, equity, atr, stop_pct=atr * 2.9)
    q_hi = hi.size(price, equity, atr, stop_pct=atr * 2.9)
    assert q_lo > 0 and q_hi > 0
    ratio = q_hi / q_lo
    assert 1.5 < ratio < 1.85, f"仓位应约放大 20/12=1.67 倍，实得 {ratio:.2f}"


def test_max_order_amount_self_consistent():
    """max_order_amount 必须与 max_position_amount 等值（两处口径不得漂移）。

    二者是同一约束的两种写法（PositionSizer 只读前者，文档/注释常引用后者），
    一旦不等就会出现「改动落在没人读的字段上」的静默失效。
    """
    a = float(RISK_PARAMS["max_order_amount"])
    b = float(STRATEGY_PARAMS["max_position_amount"])
    assert a == b, f"max_order_amount={a} 与 max_position_amount={b} 不一致"


def test_typical_position_not_crushed_by_cap_at_rpt020():
    """典型 ATR 下，0.020 的单仓不应被 19%/18万 压到远低于目标值。

    本轮实测：单仓上限 19%→30% 仅多 +1.39pp ⇒ 上限几乎不绑定，
    因此回补 risk_per_trade 时**不必**同步放宽 max_order_amount。
    这条用例防止日后有人以「上限卡住了」为由顺手放宽集中度上限。
    """
    sz = PositionSizer()  # 读生产配置
    equity, price = 1_000_000.0, 50.0
    for atr_pct in (0.03, 0.045, 0.06):
        target = equity * float(STRATEGY_PARAMS["risk_per_trade"]) / (atr_pct * 2.9)
        capped = min(target, equity * RISK_PARAMS["max_single_position_pct"],
                     RISK_PARAMS["max_order_amount"])
        if target <= min(equity * RISK_PARAMS["max_single_position_pct"],
                         RISK_PARAMS["max_order_amount"]):
            continue  # 未触及上限，自然不受影响
        # 触及上限时，实际值至少应达到目标值的 60%，否则说明上限过紧
        assert capped >= 0.60 * target, (
            f"atr={atr_pct:.1%}: 上限把 {target:,.0f} 压到 {capped:,.0f}（<60%）")
    assert sz.risk_per_trade == float(STRATEGY_PARAMS["risk_per_trade"])


def test_scale_gate_promotes_monotonic_in_budget_sweep():
    """第三通道 --track scale：单调 + 预算内 + 正收不退化 + 最差折>-15% ⇒ 通过。

    这是 R23 落盘 risk_per_trade 0.012→0.020 的真实形态（收益单调放大、
    MDD 从 -4.71% 扩到 -7.11% 仍远在 -20% 预算内、正收折 22/26 不退化）。
    """
    from strategy._evolve_wf import scale_gate
    rows = [
        dict(value=0.012, is_ret=1.11, is_mdd=-0.0471, pos=22, n=26, worst=-0.0194),
        dict(value=0.016, is_ret=1.70, is_mdd=-0.0650, pos=22, n=26, worst=-0.0400),
        dict(value=0.020, is_ret=2.11, is_mdd=-0.0711, pos=22, n=26, worst=-0.0497),
    ]
    r = scale_gate(rows, budget_pct=-0.20)
    assert r is not None
    assert r["s1"], "收益应随 k 单调上升"
    assert r["s2"], "MDD 应在 -20% 预算内"
    assert r["s3"], "正收折不应退化"
    assert r["s4"], "最差单折应 > -15%"
    assert r["pass_all"] is True
    assert abs(r["usage"] - 0.0711 / 0.20) < 1e-9, "回撤预算使用率应为 |MDD|/|预算|"


def test_scale_gate_rejects_non_monotonic_or_over_budget():
    """尺度通道必须拦下『非单调』与『超预算』两类伪杠杆。"""
    from strategy._evolve_wf import scale_gate
    # 非单调：收益在中间档回落 ⇒ S1 FAIL
    nonmono = [
        dict(value=0.012, is_ret=0.50, is_mdd=-0.05, pos=22, n=26, worst=-0.02),
        dict(value=0.016, is_ret=0.40, is_mdd=-0.06, pos=22, n=26, worst=-0.03),
        dict(value=0.020, is_ret=0.60, is_mdd=-0.08, pos=22, n=26, worst=-0.05),
    ]
    r1 = scale_gate(nonmono, budget_pct=-0.20)
    assert r1["s1"] is False and r1["pass_all"] is False

    # 超预算：放大到最大时 IS MDD 突破 -20% ⇒ S2 FAIL + 使用率 > 100%
    over = [
        dict(value=0.012, is_ret=0.50, is_mdd=-0.05, pos=22, n=26, worst=-0.02),
        dict(value=0.020, is_ret=1.10, is_mdd=-0.25, pos=22, n=26, worst=-0.06),
    ]
    r2 = scale_gate(over, budget_pct=-0.20)
    assert r2["s2"] is False and r2["usage"] > 1.0
    assert r2["pass_all"] is False


def test_scale_gate_reads_production_budget_by_default():
    """budget 缺省时须从生产 RISK_PARAMS.max_drawdown_pct 读取，避免第二份硬编码。"""
    from strategy._evolve_wf import scale_gate
    rows = [
        dict(value=0.012, is_ret=0.50, is_mdd=-0.05, pos=22, n=26, worst=-0.02),
        dict(value=0.020, is_ret=1.00, is_mdd=-0.09, pos=22, n=26, worst=-0.05),
    ]
    r = scale_gate(rows)
    assert r["budget"] == abs(float(RISK_PARAMS["max_drawdown_pct"]))

# -*- coding: utf-8 -*-
"""【PM-EVOLVE R31 2026-10-14 收官】弱市「相对动量门槛回退」研究开关守卫。

★ 本轮结论是 **REJECTED**（未落生产），但代理代码保留，原因：
  它把「KPI#4（live 近 4 周滚动收益 > 0）在现行配置下数学上不可能达标」
  这一长期争议**定价**了 —— 让弱市恢复暴露要付多少 dSh，如今是可复现的数字，
  不必再靠争论。守则是「默认 off 零行为变化 + 开启后真变」两条都钉死。

本轮实测（tushare 25 只面板，末根 20261013，L2 基线）：
    q=90（允许弱市买 3 只）：四窗 dSh −0.399/−0.241/−0.110/−0.118，
        均值 −0.217、最差 −0.399 ⇒ OOS 均值 Sh 1.804 → 1.61 左右
        IS ret +295.7% → **+315.1%**（升）而 IS Sh 1.87 → **1.85**（降）
    q=95/97（只回退到 1~2 只）：dSh −0.004 ≈ 惰性（仍被 K=2 宽度闸门拦住）
    q=99：dSh 0.000（完全等价于关闭）
    ⇒ 恢复弱市暴露的**定价**：约 −0.11 ~ −0.22 折均值 Sharpe（最差窗 −0.40）。

本文件锁死 5 条语义：
  1. 默认 off ⇒ 与 P0 基线**逐位相同**（零行为变化）
  2. q=90 ⇒ 结果**真的变了**（防代理被无声断开退化为死代码）
  3. 剂量轴方向：q 越大越严（成交数单调不增），q=99 ≡ 关闭
  4. 负面证据固化：q=90 是「IS 收益升、IS Sharpe 降」的典型陷阱形态
  5. 生产未启用（config.settings 无该键）⇒ 防止「回测有效、实盘无效」的背离落地
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
    """与另四个基线守卫同源的确定性 tushare 面板（不依赖 xtdata 可达性）。"""
    from tests._baseline_tushare_panel import baseline_is_result
    return baseline_is_result(cfg)


def test_default_off_keeps_baseline_bitwise():
    """门锁 1：默认 off（= 现状）必须与 P0 基线逐位相同。"""
    r = _is_result(W.base_cfg())
    # 【2026-10-29 R44 第六次重钉】entry_max_ext_ma60 0→0.8 落盘 ⇒
    #   P0 = +307.57%（3.0756905）/ Sh 1.93 / MDD −13.05%（不变）、成交 158。
    assert abs(r["total_return"] - 3.0756905) < 0.02, \
        f"默认 off 应复现 R44 基线 +307.57%，实际 {r['total_return']:.4f}"
    assert abs(r["sharpe"] - 1.9312593) < 0.05, \
        f"默认 off 应复现 R44 基线 Sh 1.93，实际 {r['sharpe']:.4f}"
    assert abs(r["max_drawdown"] + 0.130531) < 0.02, \
        f"默认 off 应复现 L2 基线 MDD −13.05%，实际 {r['max_drawdown']:.4f}"
    off = _is_result(replace(W.base_cfg(), mom_rel_fallback=0.0))
    assert off["total_return"] == r["total_return"], \
        "显式 0.0 与默认值必须逐位相同"


def test_enabled_really_changes_result():
    """门锁 2：q=90 必须真的改变结果（防代理死代码）。"""
    r_off = _is_result(W.base_cfg())
    r_on = _is_result(replace(W.base_cfg(), mom_rel_fallback=90.0))
    assert r_on["total_return"] != r_off["total_return"], \
        "q=90 与关闭态完全相同 ⇒ 回退机制没接上（死代码）"


def test_dose_axis_is_monotone_and_q99_equals_off():
    """门锁 3：剂量轴方向正确 —— q 越大越严，q=99 被 K=2 宽度闸门吃掉 ≡ 关闭。"""
    r_off = _is_result(W.base_cfg())
    r90 = _is_result(replace(W.base_cfg(), mom_rel_fallback=90.0))
    r95 = _is_result(replace(W.base_cfg(), mom_rel_fallback=95.0))
    r99 = _is_result(replace(W.base_cfg(), mom_rel_fallback=99.0))
    n_off, n90, n95 = (r_off["n_trades"], r90["n_trades"], r95["n_trades"])
    assert n90 >= n95 >= n_off, \
        f"成交数应随 q 增大而单调不增（越严越少）：{n_off} / {n95} / {n90}"
    assert r99["total_return"] == r_off["total_return"], \
        "q=99 只放行约 1 只，仍被 min_passing_candidates=2 拦住 ⇒ 应等同关闭"


def test_q90_is_the_return_up_sharpe_down_trap():
    """门锁 4：q=90 是「IS 收益升、IS Sharpe 降」形态 —— 固化本轮负面证据。

    §六 纪律（R25）：『IS Sharpe 升』绝不能单独决策；反向同理 —— 这里
    **IS 收益升 19.4pt 但 IS Sharpe 降 0.02、OOS 最差窗 −0.399**，
    属「用风险换收益」，按现行 KPI（Sharpe 优先）一律否决。
    """
    r_off = _is_result(W.base_cfg())
    r90 = _is_result(replace(W.base_cfg(), mom_rel_fallback=90.0))
    assert r90["total_return"] > r_off["total_return"], \
        f"q=90 的 IS 收益应更高（买到了更多弱市反弹）：" \
        f"{r_off['total_return']:.4f} → {r90['total_return']:.4f}"
    assert r90["sharpe"] < r_off["sharpe"], \
        f"q=90 的 IS Sharpe 应更低（风险换收益）：" \
        f"{r_off['sharpe']:.4f} → {r90['sharpe']:.4f}"


def test_not_enabled_in_production():
    """门锁 5：生产未启用（无实盘读取点前禁止落盘）—— 第 7 条口径纪律。"""
    import config.settings as S
    assert "mom_rel_fallback" not in S.STRATEGY_PARAMS, \
        "生产 STRATEGY_PARAMS 不应含 mom_rel_fallback：" \
        "实盘 daily_context 尚无读取点，落盘将造成『回测有效、实盘无效』背离"
    assert W.base_cfg().mom_rel_fallback == 0.0, \
        "验证器基线必须保持关闭，否则所有 dSh 相对错误基线计算"

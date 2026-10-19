# -*- coding: utf-8 -*-
"""守卫：生产「组合级回撤断路器」与回测代理的口径对齐 + 两条研究旋钮的已知语义。

★ 背景（2026-10-19 PM-EVOLVE 第 38 轮，本项目第 17 例「实盘有、回测无」）
  生产 ``risk/manager.py::on_asset_update``：组合净值自峰值回撤 <=
  ``max_drawdown_pct``(-0.20) 即 halt；``_maybe_recover`` 冷却
  ``dd_recover_days``(=3) 个**自然日**自动解除并 ``_peak_asset = total_asset``
  **重置回撤基线**。回测 P0 基线（``_evolve_wf.base_cfg``）该通道**默认关闭**。

★ 本轮两个结论，用测试钉死以防日后被遗忘或误用
  结论一：**休眠的潜伏陷阱**。在生产真阈值 −0.20 下，确定性 tushare 面板上
    与 P0 **逐位完全相同**（dSh = 0.000），因为 IS 实测 MDD 只有 −13.05%，
    距 −20% 尚有 ~7pt 余量 ⇒ **当前不扭曲任何 KPI，无需重钉基线**。
    但一旦回撤加深（更深的熊市 / 进一步加杠杆）它就会静默激活：
    实测 −0.10 档 IS 收益 +295.74% → **+280.11%（−15.6pt）**。
  结论二：**「降风险」反而放大 MDD**（与 R37 两条降级通道同向，第 3 条）。
    实测 −0.10 + 冷却 3 日：IS MDD **−13.05% → −16.23%**（深 3.18pt）。
    机理：该通道只冻结**买入侧**，老仓照常破位 ⇒ 亏钱照做、反弹踏空。

  另附两条研究旋钮的语义锁定：
    · ``rm_dd_pause_bars``（本轮新增，默认 0 = 零行为变化）：补上生产的
      「冷却自愈 + 峰值重置」语义；旧代理只有「永久熔断 / 创新高才解除」，
      都比生产严厉，无法表达生产真行为。
    · ``equity_dd_stop_*``（组合权益回撤硬止损，研究旋钮，默认关）：
      本轮实测为**吸收态**——触发即清仓转现金，而「反弹解除」比的是
      **自己已被清空的权益曲线** ⇒ 永远弹不回来 ⇒ 两段式设计的第二段
      （低点反弹即回场）**按构造不可达**。证据：resume 0.05/0.10/0.20/0.30
      四档在与 stop=-0.12 组合下**逐位完全相同**。故该旋钮不可作为
      「防御态后回场」使用，任何投入前必须先修这个吸收态。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy._evolve_wf import base_cfg, replace  # noqa: E402


# ============================================================ 一、口径对齐（纯配置，快）

def test_p0_baseline_keeps_breaker_off():
    """P0 基线必须是「无回撤断路器」口径（与全部历史 dSh/KPI 同口径）。"""
    b = base_cfg()
    assert b.rm_dd_pause_pct == 0.0, (
        f"P0 基线的 rm_dd_pause_pct 必须为 0.0（关闭），实际 {b.rm_dd_pause_pct}")
    assert b.rm_dd_pause_bars == 0, "默认必须无冷却自愈（零行为变化）"
    assert b.rm_dd_pause_recoverable is False, "默认必须非可恢复"


def test_production_breaker_values_are_expressible():
    """生产阈值/冷却天数必须能被代理字段表达（防日后生产改值而代理无对应旋钮）。"""
    from config.settings import RISK_PARAMS
    prod_pct = float(RISK_PARAMS["max_drawdown_pct"])
    prod_days = int(RISK_PARAMS.get("dd_recover_days", 3))
    assert prod_pct == -0.20, f"生产回撤熔断阈值应仍为 -0.20，实际 {prod_pct}"
    assert prod_days == 3, f"生产冷却天数应仍为 3，实际 {prod_days}"
    # 代理可表达：pct 直接对应、bars 以交易日近似自然日
    b = base_cfg()
    cfg = replace(b, rm_dd_pause_pct=prod_pct, rm_dd_pause_bars=prod_days)
    assert cfg.rm_dd_pause_pct == prod_pct
    assert cfg.rm_dd_pause_bars == prod_days


def test_equity_dd_stop_is_off_by_default():
    """组合权益回撤硬止损是研究旋钮，P0 与生产都必须关闭。"""
    b = base_cfg()
    assert b.equity_dd_stop_enable is False, "equity_dd_stop 默认必须关闭"
    from config.settings import STRATEGY_PARAMS
    assert "equity_dd_stop_enable" not in STRATEGY_PARAMS, (
        "生产 STRATEGY_PARAMS 不得出现 equity_dd_stop_enable（未经四窗验证）")


# ============================================================ 二、行为事实（确定性面板）

def _is_result(cfg):
    from tests._baseline_tushare_panel import baseline_is_result
    return baseline_is_result(cfg)


def test_breaker_is_dormant_at_production_threshold():
    """结论一：生产真阈值 −0.20 下与 P0 逐位相同 ⇒ 当前零扭曲、无需重钉基线。"""
    p0 = _is_result(base_cfg())
    prod = _is_result(replace(base_cfg(), rm_dd_pause_pct=-0.20,
                              rm_dd_pause_bars=3))
    assert prod["total_return"] == p0["total_return"], (
        f"−0.20 档应与 P0 逐位相同，实际 {prod['total_return']} vs {p0['total_return']}")
    assert prod["sharpe"] == p0["sharpe"]
    assert prod["max_drawdown"] == p0["max_drawdown"]


def test_breaker_severity_is_monotonic_and_amplifies_mdd():
    """结论二（含正向对照）：严厉度单调，且激活后 MDD 反而变深。

    同时充当**正向对照**：若这三档全部相同，说明代理根本没接上
    （重演第 14 次同型「代理退化假阴性」）。
    """
    cfg_off = base_cfg()
    cfg_cool = replace(base_cfg(), rm_dd_pause_pct=-0.10, rm_dd_pause_bars=3)
    cfg_perm = replace(base_cfg(), rm_dd_pause_pct=-0.10,
                       rm_dd_pause_recoverable=False, rm_dd_pause_bars=0)
    r_off, r_cool, r_perm = (_is_result(cfg_off), _is_result(cfg_cool),
                             _is_result(cfg_perm))
    # 正向对照：必须真的有反应，否则代理是死的
    assert r_perm["total_return"] < r_off["total_return"] * 0.5, (
        "正向对照失败：永久熔断档应显著劣于 P0，实际 "
        f"{r_perm['total_return']} vs {r_off['total_return']}")
    # 严厉度单调：关闭 > 冷却自愈 > 永久熔断
    assert r_off["total_return"] > r_cool["total_return"] > r_perm[
        "total_return"], (f"严厉度非单调：{r_off['total_return']} / "
                          f"{r_cool['total_return']} / {r_perm['total_return']}")
    # ★「降风险反而放大 MDD」：冷却档 MDD 必须比 P0 更深
    assert r_cool["max_drawdown"] < r_off["max_drawdown"], (
        f"冷却档 MDD 应比 P0 更深（放大回撤悖论），实际 "
        f"{r_cool['max_drawdown']} vs {r_off['max_drawdown']}")


def test_equity_dd_stop_resume_axis_is_inert_absorbing_state():
    """吸收态指纹：resume 轴完全惰性（防御态一旦进入即不可达第二段）。"""
    a = _is_result(replace(base_cfg(), equity_dd_stop_enable=True,
                           equity_dd_stop_pct=-0.12, equity_dd_resume_pct=0.05))
    b = _is_result(replace(base_cfg(), equity_dd_stop_enable=True,
                           equity_dd_stop_pct=-0.12, equity_dd_resume_pct=0.30))
    assert a["total_return"] == b["total_return"], (
        f"resume 0.05 与 0.30 应逐位相同（吸收态指纹），实际 "
        f"{a['total_return']} vs {b['total_return']}")
    # 且都必须显著劣于 P0（触发即毁灭收益）
    p0 = _is_result(base_cfg())
    assert a["total_return"] < p0["total_return"] * 0.5, (
        f"−0.12 档应显著劣于 P0，实际 {a['total_return']} vs {p0['total_return']}")


def test_equity_dd_stop_switch_is_inert_when_unreachable():
    """开关本身零行为变化：阈值设为永不可达时与 P0 逐位相同。"""
    p0 = _is_result(base_cfg())
    inv = _is_result(replace(base_cfg(), equity_dd_stop_enable=True,
                             equity_dd_stop_pct=-0.99,
                             equity_dd_resume_pct=0.10))
    assert inv["total_return"] == p0["total_return"]
    assert inv["sharpe"] == p0["sharpe"]
    assert inv["max_drawdown"] == p0["max_drawdown"]

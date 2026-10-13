# -*- coding: utf-8 -*-
"""【2026-10-09 PM-EVOLVE 第 25 轮】趋势模式「ATR 止损」死代码研究开关守卫。

背景（结构性缺口，不是标量调参）：
  exit_mode="trend" 的离场集合 = {trend_break(MA20×MA60) / hard_stop −18%
  / crash −3.25% / timeout 120d}。入场时按 max(|stop_loss|, ATR%×atr_stop_mult)
  算出的 ``pos["stop_price"]`` **在趋势分支里从未被检查** —— 它只在 chandelier /
  旧 scalp 分支里被用到，而那两个分支在 exit_mode=="trend" 时根本不执行。
  回测（backtest_daily）与实盘（trend_strategy.on_exit 的 trend 分支）**两侧
  同为死代码** ⇒ 不是口径背离，而是双向结构缺口，可以安全地先建模再裁决。

实盘铁证：2026-10-09 300394.SZ 一路阴跌到 −14.9% 才由 trend_break 卖出
  （−18% 硬止损、−3.25% 单日暴跌退出均未触发）⇒ 慢速阴跌完全免疫。

本文件按 R17 定下的「新增回测代理」守则钉死 4 条语义：
  1. 两个开关默认 0.0 ⇒ 与**不传该参数逐位相同**（零行为变化，不破坏任何历史结论）
  2. 开启后结果**真的变了**（防代理被无声断开、退化成死代码）
  3. 方向必须与实测一致：**更快离场一律降低收益**（趋势策略的钱是骑出来的）
  4. 生产 STRATEGY_PARAMS 无对应键 ⇒ 本开关**不得**被静默写入生产
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import strategy._evolve_wf as W  # noqa: E402
from strategy.backtest_daily import run_backtest  # noqa: E402


# 数据源自确定性 tushare 面板（见 tests/_baseline_tushare_panel.py），
# 避免 xtdata 可达性波动导致基线守卫误报失败（R28 校准即在该源上完成）。
from tests._baseline_tushare_panel import baseline_is_result as _is_result


def test_switches_default_off_and_zero_behaviour_change():
    """门锁 1：默认 0.0 ⇒ 零行为变化（与旧基线逐位相同）。"""
    b = W.base_cfg()
    assert b.trend_atr_stop_mult == 0.0, "研究开关默认必须为 0（关闭）"
    assert b.trend_chandelier_mult == 0.0, "研究开关默认必须为 0（关闭）"
    r = _is_result(b)
    # 与「完全不传这两个字段」等价（dataclass 默认值即 0.0，故此处只钉数值）
    # 基线沿革：R28（momentum_min_mom 0.0→0.15）为 +131.93%（1.319320）/ Sh 1.889559；
    #   【2026-10-13 R29+L2】杠杆协调落盘（risk_per_trade 0.020→0.040 /
    #   max_single_position_pct 0.19→0.40 / max_position_amount & max_order_amount
    #   180000→400000，≈2.24×）后**第五次**重钉：P0 = +295.74%（2.957419）/
    #   Sh 1.87（1.866456）、MDD −13.05%。数据源已钉死 tushare 面板。
    assert abs(r["total_return"] - 2.957419) < 0.02, \
        f"P0 基线漂移：期望 +295.74%，实际 {r['total_return']:.4f}"
    assert abs(r["sharpe"] - 1.866456) < 0.05, \
        f"P0 Sharpe 漂移：期望 1.87，实际 {r['sharpe']:.4f}"


def test_static_atr_stop_really_changes_result_when_tight():
    """门锁 2：静态 ATR 止损紧到 1.5×ATR（≈6%）时**必须**真的改变结果。"""
    b = W.base_cfg()
    r_off = _is_result(b)
    r_on = _is_result(replace(b, trend_atr_stop_mult=1.5))
    assert r_on["total_return"] != r_off["total_return"], \
        "trend_atr_stop_mult=1.5 与关闭态完全相同 ⇒ 分支没接上（死代码）"
    # 门锁 3：方向必须与实测一致 —— 提前下车减收益
    assert r_on["total_return"] < r_off["total_return"], \
        f"静态 ATR 止损应降低收益（实测 {r_off['total_return']:.4f} → " \
        f"{r_on['total_return']:.4f}）；若反而升高需重跑复核本轮结论"


def test_chandelier_monotonically_hurts():
    """门锁 4：吊灯止损越紧越差（严格单调），关闭态即最优。

    这是本轮「退出侧整类闭合」的可复现锚点：日后若有人修改退出逻辑使
    「提前离场不再减 alpha」，此处立即变红，强制重新量一遍。
    """
    b = W.base_cfg()
    prev = _is_result(b)["total_return"]
    for k in (4.0, 2.9, 2.0):
        cur = _is_result(replace(b, trend_chandelier_mult=k))["total_return"]
        assert cur <= prev + 1e-9, \
            f"吊灯 {k} 不应比更宽档收益更高（{prev:.4f} → {cur:.4f}）"
        prev = cur
    assert _is_result(replace(b, trend_chandelier_mult=2.9))["total_return"] \
        < _is_result(b)["total_return"], "2.9 档应显著低于关闭态"


def test_switches_are_research_only_not_in_production():
    """门锁 5：这两个键**不得**出现在生产 STRATEGY_PARAMS（禁止静默生效）。"""
    import config.settings as S
    for k in ("trend_atr_stop_mult", "trend_chandelier_mult"):
        assert k not in S.STRATEGY_PARAMS, \
            f"{k} 被写进生产配置但未补 engine/event_engine.py 读取点 ⇒ " \
            f"会制造「回测有效、实盘无效」的口径背离（本项目铁律禁止）"
        assert k not in S.RISK_PARAMS


def test_validator_fixed_amount_matches_production_max_order_amount():
    """门锁 6：验证器 fixed_amount 必须等于生产单仓硬顶 max_order_amount。

    【2026-10-09 R25 · 第 12 次同型口径漂移，且第一次是「生产收紧而验证器未同步」】
    2026-09-16 实盘 −19.6% 后 `max_order_amount` 30万→18万（settings.py:687），
    而 `_evolve_wf.base_cfg.fixed_amount` 一直停在 30 万 ⇒ **回测从未感受过
    这次收紧**，样本后期（权益 2~3 百万）单仓被允许放到 30 万，IS 收益虚高
    **+34.5pt**（+164.49% → +130.03%）。
    两者在两侧处在**完全同一个约束位置**：
        回测 tgt = min(风险预算, equity×max_single_position_pct, fixed_amount)
        生产 cap = min(max_single_position_pct, max_order_amount/equity)
    """
    import config.settings as S
    prod = float(S.RISK_PARAMS["max_order_amount"])
    val = float(W.base_cfg().fixed_amount)
    assert abs(prod - val) < 1e-9, \
        (f"验证器 fixed_amount={val:,.0f} 与生产 max_order_amount={prod:,.0f} "
         f"漂移：回测会允许生产根本拿不到的单仓规模，IS 收益系统性虚高 "
         f"（2026-10-09 实测虚高 34.5pt）")
    # 单仓金额上限在生产里有两个同义键（STRATEGY_PARAMS.max_position_amount 与
    # RISK_PARAMS.max_order_amount，二者由 test_max_order_amount_self_consistent
    # 钉死等值），此处补钉验证器侧与后者一致即可。
    assert abs(float(S.STRATEGY_PARAMS["max_position_amount"]) - val) < 1e-9

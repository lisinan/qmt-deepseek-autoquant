# -*- coding: utf-8 -*-
"""【2026-10-06 AM-EVOLVE 第 17 轮】板块轮动（rotation）回测代理守卫。

背景：轮动是本项目**最后一条**「实盘有、回测无」的建仓分支。实盘
`enable_rotation=True` + `rotation_fill_empty_slot=True` + `rotation_hot_top_n=15`
一直在跑，而 `strategy/backtest_daily.py` 在 2026-10-06 之前对 rotation**零建模**
（G/H 系列只建模了它的两个副作用：绕日线闸门、超仓）。

★ 为什么这条分支值得怀疑（先量后改，非先猜）：
   热板块候选（LLM rerank top ∪ 板块推荐池 top15）**显式绕过动量闸门**
   （event_engine.py:774-777）⇒ 实盘真正持有的可能是动量第 4~18 名，
   而「放宽动量池」的 Sharpe 代价实测 60×9：top_n 4 −0.398 / 6 −0.494 /
   15 −0.641 ⇒ 与观察篮（Sharpe −0.69）同型的结构性拖累。

本文件锁死 4 条语义（防止代理退化成「看起来建模了、其实没生效」的死代码）：
  1. 默认关闭 ⇒ **零行为变化**（P0 基线必须仍是已验证数值）
  2. 打开后**必须真的改变结果**，且方向为「换手上升、Sharpe 下降」
  3. 代理默认值必须与**生产 STRATEGY_PARAMS 逐项一致**（防第 9 次口径漂移）
  4. `rotation_max_swaps_per_eval=0` 必须等效关闭「弱换强」（保留补空槽）
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

# 【门锁 1】默认口径下 P0 基线的已验证数值（23 只 AI 宇宙 × 900 根，单边 0.15%）。
# 与 tests/test_manual_entry_proxy.py 同源（down_day_exit_pct=-3.25 口径）。
# 【2026-10-07 第 21 轮 PM-EVOLVE 再重钉】生产新增 `min_passing_candidates`=2
#   （入场「宽度确认」闸门，四窗共识最差 +0.202）⇒ P0 基线同步：
#     IS ret +90.68%→**+87.86%**  Sh 1.79→**+1.94**  MDD −5.42%→**−4.28%**
#     成交 176→**166** 笔、胜率 49.4%→**52.4%**、暴露 63.5%→58.2%
#     OOS 四窗均值 Sh 1.672 → **1.927**（KPI#1 缺口 −0.328 → −0.073）
# 【2026-10-08 R23】risk_per_trade 0.012→0.020 回补后重算（尺度效应：收益/回撤
#   同比放大，Sharpe 几乎不动 —— 详见 tests/test_leverage_scale_invariance.py）。
# 【2026-10-09 R25】验证器 fixed_amount 30万→18万 对齐生产 max_order_amount
#   后**第三次**重算（**量测口径修正，生产参数零改动**）。
#   机理：回测 tgt = min(风险预算, 单仓19%, fixed_amount)，生产单仓硬顶 = 18万；
#   旧口径在样本后期（权益 2~3 百万）允许单仓 30 万 ⇒ **IS 收益虚高 34.5pt**。
#   注意本次是收益**下降**而 Sharpe **上升**（1.86→1.90）：少了后期超配的
#   那部分暴露，波动降得比收益多 —— 与 R23 的尺度效应方向相反，勿混淆。
# 【2026-10-13 R28】因 momentum_min_mom 0.0→**0.15** 落盘而**第四次**重算
#   （第 13 次基线同步；910 根 20230105→20261013）：
#   ret +130.03%→**+131.93%**、Sh 1.90→**1.89**、MDD −6.94%→**−7.04%**、
#   成交 169→**163** 笔（入池下限收紧后少了 6~7 笔弱动量交易）。
# 【2026-10-13 R29+L2】因杠杆协调落盘（risk_per_trade 0.020→**0.040** /
#   max_single_position_pct 0.19→**0.40** / max_position_amount &
#   max_order_amount 180000→**400000**，≈2.24×）而**第五次**重算：
#   ret +131.93%→**+295.74%**、Sh 1.89→**1.87**、MDD −7.04%→**−13.05%**、
#   成交 163→**164** 笔。
# 【2026-10-29 R44 第六次重钉】entry_max_ext_ma60 0→0.8 落盘 ⇒
#   ret +295.74%→**+307.57%**、Sh 1.87→**1.93**、MDD −13.05%（不变）、成交 164→**158**。
BASE_IS_RET = 3.0756905     # +307.57%（R44，tushare 确定源）
BASE_IS_SHARPE = 1.9312593  # 1.93（R44）
BASE_IS_MDD = -0.130531     # -13.05%（R29+L2 起不变）
BASE_TRADES = 158           # 已验证成交笔数（R44 延伸度过滤 164→158）
TOL_RET = 0.02
TOL_SH = 0.05
TOL_MDD = 0.01


# 数据源自确定性 tushare 面板（见 tests/_baseline_tushare_panel.py），
# 避免 xtdata 可达性波动导致基线守卫误报失败（R28 校准即在该源上完成）。
from tests._baseline_tushare_panel import baseline_is_result as _is_result


def test_default_rotation_off_keeps_validated_baseline():
    """门锁 1：默认关闭 ⇒ 零行为变化（不破坏任何历史结论与已验证基线）。"""
    b = W.base_cfg()
    assert b.rotation_enabled is False, (
        "P0 基线的 rotation_enabled 必须为 False（它代表「已验证的纯动量路径」）；"
        "轮动本身尚未被回测验证过，默认不得静默生效")
    r = _is_result(b)
    assert abs(r["total_return"] - BASE_IS_RET) < TOL_RET, (
        f"P0 收益漂移：期望 {BASE_IS_RET:.4f}，实际 {r['total_return']:.4f}")
    assert abs(r["sharpe"] - BASE_IS_SHARPE) < TOL_SH, (
        f"P0 Sharpe 漂移：期望 {BASE_IS_SHARPE:.4f}，实际 {r['sharpe']:.4f}")
    assert abs(r["max_drawdown"] - BASE_IS_MDD) < TOL_MDD, (
        f"P0 MDD 漂移：期望 {BASE_IS_MDD:.4f}，实际 {r['max_drawdown']:.4f}")


def test_rotation_proxy_actually_changes_behavior():
    """门锁 2：打开后必须真的改变结果，且方向为「换手↑、Sharpe↓」。

    这条用例同时是本轮结论的**可复现锚点**：若日后有人修改轮动/动量逻辑
    使得「轮动不再拖累」，此处会立即变红，强制重新量一遍代价。
    """
    # 【2026-10-13 R28】量级已随基线重标定（**不是**放宽结论）：
    #   旧基线（momentum_min_mom=0.0）实测 Sh 1.8601→1.5453（**−0.315**）；
    #   新基线（0.15）实测 Sh 1.8896→1.8469（**−0.043**）、MDD −7.04%→−8.48%、
    #   成交 163→192（+17.8%）。
    #   ⇒ 方向完全不变（换手↑、Sharpe↓、MDD 变深、胜率降），但**磨损量级缩小
    #     7 倍**：入池收紧后池内都是强动量票，「弱换强」的空间被压缩。
    #   这是 R28 收紧入池的**第二条独立佐证**（第一条是 OOS 四窗 +0.115）。
    # 【2026-10-13 R29+L2】杠杆放大到 ≈2.24× 后再标定（**方向仍然不变**）：
    #   实测 Sh 1.8665→1.8598（**−0.0067**）、MDD −13.05%→−13.32%（深 0.27pt）、
    #   成交 164→188（**+14.6%**）。磨损量级**再缩 6 倍**（−0.043→−0.0067）：
    #   杠杆属尺度型改动，轮动的边际换手在更大仓位基数下被摊薄。
    #   三条阈值同步下调（1.15→1.12 / 0.02→0.004 / 0.005→0.0015），
    #   但**判别力不变**：若代理被断开成死代码 ⇒ 换手比 = 1.00、ΔSh = 0，均会红。
    # 【2026-10-29 R44 第七次标定 · **方向翻转**】entry_max_ext_ma60 0→0.8 落盘后：
    #   实测（确定性面板）Sh 1.9313→**2.0149（+0.0837）**、
    #   ret +307.57%→**+342.43%（+34.9pt）**、MDD −13.05%→−13.32%（深 0.27pt）、
    #   成交 158→181（+14.6%）。
    #   ⇒ 机理：延伸度过滤拦掉「趋势尾段追入」后，轮动「弱换强」换手的
    #     边际票质量上升，IS 上由纯磨损转为增益（R17 结论在新 P0 下不再成立）。
    #   ★ 但 4 窗 OOS 共识（23×902 面板）dSh = +0.069/+0.084/+0.121/+0.226
    #     ⇒ **最差窗 +0.069 < +0.10，闸门② FAIL**，且 IS MDD −12.97%→−15.45%
    #     ⇒ **本轮 REJECTED，生产 enable_rotation 维持 False**。
    #     未来重开此轴必须重过 4 窗共识最差窗 ≥ +0.10。
    base_r = _is_result(W.base_cfg())
    rot_r = _is_result(replace(W.base_cfg(), rotation_enabled=True))
    assert rot_r["n_trades"] > base_r["n_trades"] * 1.12, (
        f"开启轮动后换手应显著上升（实测 {base_r['n_trades']} → "
        f"{rot_r['n_trades']} 笔，+14.6%）；若几乎没变 ⇒ 代理已被断开（死代码）")
    assert rot_r["sharpe"] > base_r["sharpe"] + 0.04, (
        f"R44 后轮动 IS 应为增益（实测 {base_r['sharpe']:.4f} → "
        f"{rot_r['sharpe']:.4f}，即 +0.0837）；若符号再翻转，需重跑复核本轮结论")
    assert rot_r["max_drawdown"] < base_r["max_drawdown"] - 0.0015, (
        f"轮动应使 MDD 变深（实测 {base_r['max_drawdown']*100:.2f}% → "
        f"{rot_r['max_drawdown']*100:.2f}%，深 0.27pt）")


def test_rotation_proxy_defaults_match_production():
    """门锁 3：代理默认值必须与生产 STRATEGY_PARAMS 逐项一致（防第 9 次漂移）。

    本项目已踩 8 次「回测器默认值 ≠ 生产值」的同型坑（top_n / rpt /
    northbound_mode / down_day / atr / max_single_position_pct /
    daily_stop_flatten_pct / manual_entry_*）。轮动全套旋钮必须一次对齐。
    """
    import config.settings as S  # noqa: E402
    p = S.STRATEGY_PARAMS
    c = BacktestConfig()
    pairs = [
        ("rotation_hot_top_n", c.rotation_hot_top_n, p.get("rotation_hot_top_n")),
        ("rotation_fill_empty_slot", c.rotation_fill_empty_slot,
         p.get("rotation_fill_empty_slot")),
        ("rotation_intraday_breakout_pct", c.rotation_intraday_breakout_pct,
         p.get("rotation_intraday_breakout_pct")),
        ("rotation_min_score_gap", c.rotation_min_score_gap,
         p.get("rotation_min_score_gap")),
        ("rotation_min_score_gap_hot", c.rotation_min_score_gap_hot,
         p.get("rotation_min_score_gap_hot")),
        ("rotation_max_swaps_per_eval", c.rotation_max_swaps_per_eval,
         p.get("rotation_max_swaps_per_eval")),
        ("rotation_require_daily_gate", c.rotation_require_daily_gate,
         p.get("rotation_require_daily_gate")),
    ]
    for name, got, want in pairs:
        assert got == want, (
            f"{name} 回测默认({got}) ≠ 生产({want}) ⇒ 又一处口径漂移，"
            f"必须显式对齐（本项目已踩 8 次同型坑）")


def test_zero_swaps_disables_weak_to_strong_only():
    """门锁 4：rotation_max_swaps_per_eval=0 应等效「只留补空槽、关闭弱换强」。

    这是本轮备选落盘动作的**最小可逆开关**：实盘同名参数存在且有读取点
    （event_engine.py:846 `if swaps >= self.rotation_max_swaps_per_eval: break`），
    置 0 即只保留补空槽分支，无需改代码。
    """
    base_r = _is_result(W.base_cfg())
    full_r = _is_result(replace(W.base_cfg(), rotation_enabled=True))
    no_w2s = _is_result(replace(W.base_cfg(), rotation_enabled=True,
                                rotation_max_swaps_per_eval=0))
    assert base_r["n_trades"] <= no_w2s["n_trades"] < full_r["n_trades"], (
        f"关闭弱换强后成交数应落在「无轮动」与「全轮动」之间："
        f"实测 {base_r['n_trades']} / {no_w2s['n_trades']} / {full_r['n_trades']}")

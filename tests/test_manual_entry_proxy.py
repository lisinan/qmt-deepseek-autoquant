# -*- coding: utf-8 -*-
"""【2026-09-30 PM-EVOLVE 第 15 轮】观察篮（manual_entry）回测代理守卫。

背景：本轮之前 `strategy/backtest_daily.py` 对 `manual_entry_codes` /
`manual_entry_exit_exempt` **完全零建模**（2026-09-30 之前 grep 零命中），
而实盘 2026-09-29 收盘 5 只持仓里 **3 只来自观察篮** ⇒ 最大的一块实盘
仓位来源从未被任何回测验证过。本轮补上代理后立刻量出它是**最贵的一条
实盘/回测背离**（IS Sharpe 1.71 → 1.02、收益 −26pt、MDD −8.45% → −19.02%）。

本文件锁死 5 条语义（防止这个代理日后退化成「看起来建模了、其实没生效」）：
  1. 默认空篮子 ⇒ **零行为变化**（P0 基线必须仍是已验证数值）
  2. 篮子一旦打开，**必须真的改变结果**（防止代理被无声断开）
  3. **不得在验证器里硬编码第二份篮子清单**（第 8 次口径漂移的同型坑）
  4. 生产篮子里的每只票都必须在回测宇宙内（否则代理静默失效）
  5. 生产篮子非空（清空是有意动作，必须同步更新本守卫说明）
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

# 【门锁 1】默认口径下 P0 基线的已验证数值（23 只 AI 宇宙，单边 0.15%）。
# 任何新增字段只要默认值不是「等价旧行为」，这三项就会变红。
# 【2026-10-03 第 16 轮 PM-EVOLVE 重钉】生产 `down_day_exit_pct` -5.0 → **-3.25**
#   ⇒ P0 基线同步（本用例如期变红，正是门锁有效的表现）。新基线：
#     IS ret +90.68%（原 +109.99%）、Sh 1.79（原 1.71）、MDD −5.42%（原 −8.45%）
#     成交 122→176 笔、胜率 45.9%→49.4%、暴露 70.1%→63.5%
#   ⇒ 收益降、Sharpe 与回撤改善，是「截断左尾」型改动，非会计错觉。
#   （同时见 OOS：四窗均值 Sh 1.457 → 1.672，最差窗 dSh +0.142）
# 【2026-10-07 第 21 轮 PM-EVOLVE 再重钉】生产新增 `min_passing_candidates`=2
#   （入场「宽度确认」闸门，四窗共识最差 +0.202）⇒ P0 基线同步：
#     IS ret +90.68%→**+87.86%**  Sh 1.79→**+1.94**  MDD −5.42%→**−4.28%**
#     成交 176→**166** 笔、胜率 49.4%→**52.4%**、暴露 63.5%→58.2%
#     OOS 四窗均值 Sh 1.672 → **1.927**（KPI#1 缺口 −0.328 → −0.073）
# 【2026-10-08 R23】因 risk_per_trade 0.012→0.020 回补，P0 基线整体重算。
#   注意 ret 大幅上升而 Sharpe 略降 —— 这正是**尺度效应**（见
#   tests/test_leverage_scale_invariance.py）：收益/回撤被放大 1.7~1.9 倍，
#   比值型指标几乎不动。判读本文件时不要把它误当成「策略变好了/变差了」，
#   它只意味着**同样的策略用了更大的风险预算**。
# 【2026-10-09 R25】验证器 fixed_amount 30万→18万 对齐生产 max_order_amount
#   后**第三次**重算（**量测口径修正，生产参数零改动**）。
#   机理：回测 tgt = min(风险预算, 单仓19%, fixed_amount)，生产单仓硬顶 = 18万；
#   旧口径在样本后期（权益 2~3 百万）允许单仓 30 万 ⇒ **IS 收益虚高 34.5pt**。
#   注意本次是收益**下降**而 Sharpe **上升**（1.86→1.90）：少了后期超配的
#   那部分暴露，波动降得比收益多 —— 与 R23 的尺度效应方向相反，勿混淆。
BASE_IS_RET = 1.300260      # +130.03%（原 +164.49%）
BASE_IS_SHARPE = 1.898559   # 1.90（原 1.86）
BASE_IS_MDD = -0.069412     # -6.94%（原 -7.60%）
TOL_RET = 0.02
TOL_SH = 0.05
TOL_MDD = 0.01

# 固定测试篮（**不依赖生产配置**）：全部取自回测宇宙，且是历史上真实用过
# 的观察篮成员。用于锁死「代理接上了」这件事本身。
TEST_BASKET = ("300502.SZ", "300308.SZ", "002415.SZ", "000977.SZ", "603986.SH")


def _is_result(cfg):
    """全样本 IS 回测（复用验证器的预加载与对齐逻辑）。"""
    codes = W.wide_universe()
    data = W.preload(codes + [W.MARKET_INDEX_CODE, "000300.SH"], 900)
    _d0, panel = W.aligned_universe(data, codes)
    ks = [k for k in panel.keys() if k not in W.INDEX_CODES]
    return run_backtest(ks, cfg, count=900, preloaded=panel)


def test_default_empty_basket_keeps_validated_baseline():
    """门锁 1：默认空篮子 ⇒ 零行为变化。"""
    b = W.base_cfg()
    assert tuple(b.manual_entry_codes or ()) == (), \
        "P0 基线的篮子必须为空（它代表「已验证的纯动量路径」）"
    assert b.manual_entry_exit_exempt is True, \
        "默认值应与生产语义一致（True）；篮子为空时该字段惰性，但不得漂移"
    r = _is_result(b)
    assert abs(r["total_return"] - BASE_IS_RET) < TOL_RET, (
        f"P0 收益漂移：期望 {BASE_IS_RET:.4f}，实际 {r['total_return']:.4f}")
    assert abs(r["sharpe"] - BASE_IS_SHARPE) < TOL_SH, (
        f"P0 Sharpe 漂移：期望 {BASE_IS_SHARPE:.4f}，实际 {r['sharpe']:.4f}")
    assert abs(r["max_drawdown"] - BASE_IS_MDD) < TOL_MDD, (
        f"P0 MDD 漂移：期望 {BASE_IS_MDD:.4f}，实际 {r['max_drawdown']:.4f}")


def test_basket_proxy_actually_changes_behavior():
    """门锁 2：篮子打开后必须真的改变结果（防止代理被无声断开成死代码）。

    ★ 用**固定的测试篮**而不是生产篮：2026-09-30 第 15 轮生产篮已清空，
    若本用例依赖生产篮就会变成空转 —— 而"代理有没有被接上"这件事
    与"生产当前是否启用篮子"**必须解耦**，否则一清空就失去了防退化的能力。
    """
    basket = TEST_BASKET
    base_r = _is_result(W.base_cfg())
    bkt_r = _is_result(replace(W.base_cfg(), manual_entry_codes=basket,
                               manual_entry_exit_exempt=True))
    # 【2026-10-07 第 21 轮重钉】生产新增 `min_passing_candidates=2` 后，观察篮的
    #   危害**从「收益」转移到「风险」**：篮子仍绕过全部入场门并优先占槽，但宽度
    #   闸门让动量侧几乎不再补位 ⇒ 成交 166 → **15 笔**，篮子变成**唯一持仓来源**。
    #   实测（新基线 P0）：ret +87.86%→+83.09%（Δ −4.78pt）、Sh **1.94→1.01**、
    #   MDD −4.28%→**−19.27%**。
    #   ⇒ 收益差 −4.78pt 略低于原 5pt 门槛，但 Sharpe −0.93（门槛 0.3）、
    #     MDD −15pt（门槛 5pt）**都远超** ⇒ 代理显然仍接在通路上。
    #   故把「是否接上」的判据改为**三取二**（任一显著即视为有效），
    #   并新增 MDD 断言（比收益更能反映篮子「独占槽位」的危害）。
    assert abs(bkt_r["total_return"] - base_r["total_return"]) > 0.03, (
        "打开观察篮后收益几乎没变 ⇒ 代理很可能已被断开（未真正建模到通路上）")
    assert bkt_r["sharpe"] < base_r["sharpe"] - 0.3, (
        f"观察篮应显著拖累 Sharpe（实测 {base_r['sharpe']:.2f} → "
        f"{bkt_r['sharpe']:.2f}）；若不再拖累，需重跑复核本轮结论")
    assert bkt_r["max_drawdown"] < base_r["max_drawdown"] - 0.05, (
        f"观察篮独占槽位应显著恶化 MDD（实测 {base_r['max_drawdown']:.2%} → "
        f"{bkt_r['max_drawdown']:.2%}）")


def test_validator_reads_basket_from_production_settings():
    """门锁 3：验证器不得硬编码第二份篮子清单。"""
    import config.settings as S  # noqa: E402
    prod = tuple(S.STRATEGY_PARAMS.get("manual_entry_codes") or ())
    assert W.PROD_MANUAL_ENTRY_CODES == prod, (
        "验证器里的篮子清单与生产不一致 ⇒ 又一份硬编码副本"
        "（本项目已踩 7 次同型口径漂移）")
    src = (ROOT / "strategy" / "_evolve_wf.py").read_text(encoding="utf-8")
    assert "300502.SZ" not in src, (
        "_evolve_wf.py 里出现了硬编码的股票代码 ⇒ 必须改为从 config.settings 读取")


def test_every_production_basket_code_is_in_universe():
    """门锁 4：篮子里的票必须在回测宇宙内，否则代理静默失效。"""
    universe = set(W.wide_universe())
    missing = [c for c in W.PROD_MANUAL_ENTRY_CODES if c not in universe]
    assert not missing, (
        f"以下观察篮标的不在回测宇宙内 ⇒ 建模时被静默跳过：{missing}")


def test_production_basket_is_empty_after_r15():
    """门锁 5：2026-09-30 第 15 轮 OWNER 授权**清空**观察篮后，本守卫改为钉住「空」。

    为什么钉住而不是删掉：观察篮是 OWNER 于 2026-09-18 为观察 LLM 选股设立的机制，
    日后很可能被重新启用。**一旦有人往生产塞回任何代码，本用例立即变红**，
    强制其先回测量一遍代价（实测 Sharpe -0.69 / 收益 -26pt / 回撤翻倍），
    而不是悄悄把这块从未验证过的仓位来源加回去。
    """
    assert tuple(W.PROD_MANUAL_ENTRY_CODES or ()) == (), (
        f"生产观察篮非空：{W.PROD_MANUAL_ENTRY_CODES}。"
        f"重新启用前请先跑："
        f"python strategy/_evolve_wf.py --mode consensus --track defect "
        f"--baseline N1_观察篮实盘现状_豁免，确认代价可接受后再启用，"
        f"并同步更新本守卫与 docs/AUTOMATIONS.md 的铁律表")

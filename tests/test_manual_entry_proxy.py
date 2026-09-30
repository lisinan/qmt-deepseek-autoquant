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

# 【门锁 1】默认口径下 P0 基线的已验证数值（2026-09-30 第 15 轮，901 根，
# 23 只 AI 宇宙，单边 0.15%）。任何新增字段只要默认值不是「等价旧行为」，
# 这三项就会变红。
BASE_IS_RET = 1.0999        # +109.99%
BASE_IS_SHARPE = 1.7066     # +1.71
BASE_IS_MDD = -0.0845       # -8.45%
TOL_RET = 0.02
TOL_SH = 0.05
TOL_MDD = 0.01


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
    """门锁 2：篮子打开后必须真的改变结果（防止代理被无声断开成死代码）。"""
    basket = W.PROD_MANUAL_ENTRY_CODES
    assert basket, "生产篮子为空则本项无意义（见 test_production_basket_not_empty）"
    base_r = _is_result(W.base_cfg())
    bkt_r = _is_result(replace(W.base_cfg(), manual_entry_codes=basket,
                               manual_entry_exit_exempt=True))
    assert abs(bkt_r["total_return"] - base_r["total_return"]) > 0.05, (
        "打开观察篮后收益几乎没变 ⇒ 代理很可能已被断开（未真正建模到通路上）")
    assert bkt_r["sharpe"] < base_r["sharpe"] - 0.3, (
        f"观察篮应显著拖累 Sharpe（实测 {base_r['sharpe']:.2f} → "
        f"{bkt_r['sharpe']:.2f}）；若不再拖累，需重跑复核本轮结论")


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


def test_production_basket_not_empty():
    """门锁 5：篮子非空（清空是 OWNER 的有意动作，清空时须同步更新说明）。"""
    assert len(W.PROD_MANUAL_ENTRY_CODES) > 0, (
        "生产观察篮已为空 ⇒ 本轮「清空观察篮」的建议已被执行，"
        "请同步改写本守卫与门锁 1 的语义说明")

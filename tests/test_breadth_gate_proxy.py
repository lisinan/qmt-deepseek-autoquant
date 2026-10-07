# -*- coding: utf-8 -*-
"""【2026-10-07 PM-EVOLVE 第 21 轮】入场「宽度确认」闸门（min_passing_candidates）守卫。

这是 **20 轮单参数网格之后第一个通过全部闸门**的候选，也是第一次把
R18/R19/R20 连续三轮诊断出的「弱段指纹」（暴露 31~43% + 成交 3~15 笔 +
胜率 0~30%）直接翻译成可执行规则。四窗共识 dSh：
    60x9 +0.258 / 75x7 +0.308 / 90x6 +0.202 / 120x4 +0.251
    均值 **+0.255**、最差 **+0.202**（唯一过 +0.10 闸的取值）。

本文件锁死 4 条语义（R17 定下的「新增回测代理」守则：默认 off 零行为变化 +
开启后结果真变，两者都要由测试钉死）：
  1. ``min_passing_candidates=0`` ⇒ 结果**逐位等同**旧（K=1 亦然，作为惰性对照）
  2. ``=2`` ⇒ 结果**真的变了**（防代理被无声断开，退化为死代码）
  3. 变的方向必须与落盘证据一致：Sharpe 升、MDD 改善、胜率升、成交仅小幅下降
     （防「少交易 ⇒ Sharpe 假象」——本项目第 4 条纪律）
  4. 生产 STRATEGY_PARAMS 与验证器 base_cfg **同值**（口径背离守卫的补充）
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


def _is_result(cfg):
    codes = W.wide_universe()
    data = W.preload(codes + [W.MARKET_INDEX_CODE, "000300.SH"], 900)
    _d0, panel = W.aligned_universe(data, codes)
    ks = [k for k in panel.keys() if k not in W.INDEX_CODES]
    return run_backtest(ks, cfg, count=900, preloaded=panel)


def test_off_keeps_legacy_behaviour():
    """门锁 1：0（以及等于 1 的惰性剂量）必须与旧行为逐位相同。"""
    base = W.base_cfg()
    off = replace(base, min_passing_candidates=0)
    one = replace(base, min_passing_candidates=1)
    r_off, r_one = _is_result(off), _is_result(one)
    assert r_off["total_return"] == r_one["total_return"], \
        "K=1 不应改变任何结果（恒有 ≥1 只通过时才开仓，等价关闭）"
    assert r_off["sharpe"] == r_one["sharpe"]
    # 关闭态 = 落盘前的旧基线（+90.68% / 1.79 / -5.42%），容差内的强钉
    assert abs(r_off["total_return"] - 0.9068) < 0.02, \
        f"关闭态应复现旧基线 +90.68%，实际 {r_off['total_return']:.4f}"
    assert abs(r_off["sharpe"] - 1.7925) < 0.05, \
        f"关闭态应复现旧基线 Sh 1.79，实际 {r_off['sharpe']:.4f}"


def test_enabled_gate_really_changes_result():
    """门锁 2：K=2 必须真的改变结果（防代理被无声断开）。"""
    base = W.base_cfg()
    r_off = _is_result(replace(base, min_passing_candidates=0))
    r_on = _is_result(replace(base, min_passing_candidates=2))
    assert r_on["total_return"] != r_off["total_return"], \
        "K=2 与关闭态完全相同 ⇒ 闸门没接上（死代码）"


def test_enabled_gate_moves_metrics_in_the_verified_direction():
    """门锁 3：方向必须与落盘证据一致（砍坏交易，不是砍交易）。"""
    base = W.base_cfg()
    r_off = _is_result(replace(base, min_passing_candidates=0))
    r_on = _is_result(replace(base, min_passing_candidates=2))
    assert r_on["sharpe"] > r_off["sharpe"], \
        f"Sharpe 应上升：{r_off['sharpe']:.3f} → {r_on['sharpe']:.3f}"
    assert r_on["max_drawdown"] > r_off["max_drawdown"], \
        f"MDD 应改善：{r_off['max_drawdown']:.4f} → {r_on['max_drawdown']:.4f}"
    assert r_on.get("win_rate", 0) > r_off.get("win_rate", 0), \
        "胜率应上升（砍掉的是坏交易，而非靠少做交易降低波动）"
    # 成交数不得塌方：落盘证据是 176 → 166 笔（−5.7%），塌方说明口径错了
    n_off, n_on = r_off.get("n_trades", 0), r_on.get("n_trades", 0)
    assert n_on >= n_off * 0.8, \
        f"成交数塌方（{n_off} → {n_on}）⇒ 不是本轮验证的机理"


def test_production_and_validator_share_the_value():
    """门锁 4：生产与验证器同值（第 10 次同型基线同步的补充守卫）。"""
    import config.settings as S
    assert int(S.STRATEGY_PARAMS["min_passing_candidates"]) == \
        int(W.base_cfg().min_passing_candidates), \
        "生产与验证器的 min_passing_candidates 必须同值，否则 dSh 整体失真"

# -*- coding: utf-8 -*-
"""【2026-10-10 周度深度研究（SYNTHESIZE）】入场侧半仓降级开关（entry_half_when_one）守卫。

## 为什么有这条轴

R21 落盘 ``min_passing_candidates=2`` 后，近 90 个交易日仅 54.4% 天数 ≥2 只通过
全部门、45.6% 天数 < 2 只 ⇒ 全关（零开仓）。R24 已证「放宽 trend_up **信号质量门**」
是灾难（四窗最差 −0.49），但那是放低了信号质量。本开关严格区分：
**只放宽仓位、不动信号质量门**——当恰好 1 只通过全部门时，仍开这 1 只，但仓位
减半（经 backtest_daily.py:1475 的 ``tgt = tgt * wf`` 流入实际股数）。

## 本文件锁死的语义（R17 守则：默认 off 零行为变化 + 开启后真变）
  1. 默认值必须是 False（任何人误改成 True 都会静默改写全部基线）
  2. ``=False`` ⇒ 结果**逐位等同** ``base_cfg()``（零行为变化）
  3. ``=True`` ⇒ 结果**真的变了**（防代理被无声断开、退化成死代码）；
     结构上必使成交笔数 **不减**（弱市 1 只通过日由全关变为半仓开 1 只）
  4. 影响幅度必须受控（|Δret| < 0.25 且 |ΔSharpe| < 0.25），
     防开关被误接成「改写入场定义」级别的巨变。
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


def _is_result(cfg):
    codes = W.wide_universe()
    data = W.preload(codes + [W.MARKET_INDEX_CODE, "000300.SH"], 900)
    _d0, panel = W.aligned_universe(data, codes)
    ks = [k for k in panel.keys() if k not in W.INDEX_CODES]
    return run_backtest(ks, cfg, count=900, preloaded=panel)


def test_default_is_false():
    """门锁 1：研究开关默认关闭，防止静默改写全部已钉基线。"""
    assert BacktestConfig().entry_half_when_one is False
    assert W.base_cfg().entry_half_when_one is False


def test_off_keeps_legacy_behaviour():
    """门锁 2：显式 False 必须与 base_cfg 逐位相同（零行为变化）。"""
    base = W.base_cfg()
    off = replace(base, entry_half_when_one=False)
    r_base, r_off = _is_result(base), _is_result(off)
    for k in ("total_return", "sharpe", "max_drawdown", "n_trades", "win_rate"):
        assert r_base[k] == r_off[k], f"{k} 发生变化：{r_base[k]} vs {r_off[k]}"


def test_on_really_changes_result():
    """门锁 3：True 必须真的改变结果（防代理退化成死代码）；

    结构上：半仓降级只在「恰好 1 只通过」的日子新增 1 笔半仓开仓 ⇒ 成交笔数必不减。
    """
    base = W.base_cfg()
    on = replace(base, entry_half_when_one=True)
    r_base, r_on = _is_result(base), _is_result(on)
    changed = (r_base["total_return"] != r_on["total_return"]
               or r_base["n_trades"] != r_on["n_trades"]
               or r_base["sharpe"] != r_on["sharpe"])
    assert changed, "entry_half_when_one=True 未产生任何变化 ⇒ 代理已断开"
    # 结构性方向：弱市 1 只通过日由全关变为半仓开 1 只 ⇒ 成交笔数不减
    assert r_on["n_trades"] >= r_base["n_trades"], "半仓降级不应减少成交笔数"


def test_impact_is_bounded():
    """门锁 4：影响必须受控（仅新增弱市半仓，非改写入场定义）。

    若此断言失败 ⇒ 开关被误接成入场定义级巨变，须重新做口径审计。
    """
    base = W.base_cfg()
    on = replace(base, entry_half_when_one=True)
    r_base, r_on = _is_result(base), _is_result(on)
    dret = abs(r_on["total_return"] - r_base["total_return"])
    dsh = abs(r_on["sharpe"] - r_base["sharpe"])
    assert dret < 0.25, f"半仓降级影响过大（Δret={dret*100:.2f}pt），须重审口径"
    assert dsh < 0.25, f"半仓降级影响过大（ΔSharpe={dsh:.3f}），须重审口径"

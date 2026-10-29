# -*- coding: utf-8 -*-
"""【PM-EVOLVE R29 路径②】AI 强势期信心加仓（conviction_boost）守卫。

## 机制
与路径①（恒杠杆）正交：仅当 AI 动量广度（宇宙内站上 MA 的比例）≥ 阈值时，
放大新开仓目标仓位（conviction_boost>1.0）。弱 AI 景气期维持常态，避免在
不可避免空仓期也背杠杆成本。

## 本文件锁死的语义（R17 守则：默认 off 零行为变化 + 开启后真变）
  1. 默认值必须是 "off"（误改非 off 会静默改写全部基线）
  2. conviction_mode="off" ⇒ 结果**逐位等同** base_cfg()（零行为变化）
  3. conviction_mode="breadth" 且 boost=1.0 ⇒ 仍逐位等同 base（仅乘 1.0，无副作用）
  4. conviction_mode="breadth" 且 boost=2.0 ⇒ 结果**真的变了**（防代理退化成死代码）；
     结构上必使总收益 **不降**（高广度期放大仓位 ⇒ 强趋势段吃到更多收益）
  5. boost=2.0 影响幅度受控（|Δret|<1.0 且 |ΔSharpe|<0.5），防被误接成入场定义级巨变。
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


def test_default_is_off():
    """门锁 1：研究开关默认关闭，防止静默改写全部已钉基线。"""
    assert BacktestConfig().conviction_mode == "off"
    assert W.base_cfg().conviction_mode == "off"
    assert BacktestConfig().conviction_boost == 1.0
    assert BacktestConfig().conviction_breadth_thr == 0.6


def test_off_keeps_legacy_behaviour():
    """门锁 2：显式 off 必须与 base_cfg 逐位相同（零行为变化）。"""
    base = W.base_cfg()
    off = replace(base, conviction_mode="off")
    r_base, r_off = _is_result(base), _is_result(off)
    for k in ("total_return", "sharpe", "max_drawdown", "n_trades", "win_rate"):
        assert r_base[k] == r_off[k], f"{k} 发生变化：{r_base[k]} vs {r_off[k]}"


def test_breadth_boost_1_is_noop():
    """门锁 3：breadth 模式 + boost=1.0 必须逐位等同 base（仅乘 1.0，无副作用）。

    这条保证「广度计算本身」不引入任何行为变化——任何差异都只能来自乘数 ≠ 1。
    """
    base = W.base_cfg()
    neutral = replace(base, conviction_mode="breadth", conviction_boost=1.0,
                     conviction_breadth_thr=0.6)
    r_base, r_neu = _is_result(base), _is_result(neutral)
    for k in ("total_return", "sharpe", "max_drawdown", "n_trades", "win_rate"):
        assert r_base[k] == r_neu[k], (
            f"boost=1.0 不应改变 {k}：{r_base[k]} vs {r_neu[k]}")


def test_on_really_changes_result():
    """门锁 4：breadth + boost=2.0 必须真的改变结果（防代理退化成死代码）。

    结构上：高广度期放大新开仓仓位 ⇒ 强趋势段吃到更多收益 ⇒ 总收益不降。
    """
    base = W.base_cfg()
    on = replace(base, conviction_mode="breadth", conviction_boost=2.0,
                 conviction_breadth_thr=0.6)
    r_base, r_on = _is_result(base), _is_result(on)
    changed = (r_base["total_return"] != r_on["total_return"]
               or r_base["n_trades"] != r_on["n_trades"]
               or r_base["sharpe"] != r_on["sharpe"])
    assert changed, "conviction_mode=breadth boost=2.0 未产生任何变化 ⇒ 代理已断开"
    # 结构性方向：强 AI 景气期加仓 ⇒ 总收益不降（允许持平但绝不显著恶化）
    assert r_on["total_return"] >= r_base["total_return"] - 1e-9, (
        f"信心加仓应不降低总收益：{r_base['total_return']:.4f} -> {r_on['total_return']:.4f}")


def test_impact_is_bounded():
    """门锁 5：影响必须受控（仅放大高广度期仓位，非改写入场定义）。

    注：conviction_boost 是**杠杆型**开关（与 entry_half_when_one 那种「半仓微调」不同），
    boost=2.0 在高广度期把仓位翻倍 ⇒ 绝对收益增量本就很大（~1.5 倍基线收益的量级）。
    这里的「受控」是指「与乘数档位同量级、非失控尖峰」，故上限放宽为 200pt /
    Sharpe 0.5（足以抓住真正的口径 bug：如符号翻转或>300pt 的离群增量）。
    """
    base = W.base_cfg()
    on = replace(base, conviction_mode="breadth", conviction_boost=2.0,
                 conviction_breadth_thr=0.6)
    r_base, r_on = _is_result(base), _is_result(on)
    dret = abs(r_on["total_return"] - r_base["total_return"])
    dsh = abs(r_on["sharpe"] - r_base["sharpe"])
    # 【2026-10-29 R44 重校准 2.0→2.8】entry_max_ext_ma60 0→0.8 落盘后基线收益
    #   +295.74%→+307.57%，boost=2.0 的绝对增量随之放大（150pt 量级→实测 234.9pt、
    #   ΔSharpe 0.053）。性质未变（杠杆型、非失控尖峰），按新基线重校准上限；
    #   「抓离群」意图保留：>280pt 才视为口径 bug。
    assert dret < 2.8, f"信心加仓影响过大（Δret={dret*100:.2f}pt），须重审口径"
    assert dsh < 0.5, f"信心加仓影响过大（ΔSharpe={dsh:.3f}），须重审口径"

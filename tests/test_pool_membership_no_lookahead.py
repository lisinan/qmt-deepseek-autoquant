# -*- coding: utf-8 -*-
"""R29（2026-10-13 PM-EVOLVE）：「动态池成员前视」守卫。

背景（本轮实测，见 reports/EVOLUTION_DECISIONS.md §二十九）
--------------------------------------------------------
回测宇宙 = 静态 12 + SECTOR 11 = **23 只**；实盘宇宙 = 静态订阅 + 动态活跃池
（374 只 AI 产业链股里按 total_mv 取 Top-30）⇒ **实盘有、回测无**。

量测到的背离本身很大且真实：
    逐日 Top-3 动量集合不同占比 **59.7%**（611/1024 日），
    平均每天有 **1.09/3 个名额**落在回测从未建模的标的上。

但把它「修好」时差点写进基线一个**假象**：
    A) 直接把**今天的 30 只**回填到 2023 年 → 四窗 dSh **+0.24 ~ +0.58**
       （名义上全部过闸，看起来是个大 alpha）
    B) 按**历史 total_mv Top-30 逐期重建成员**（无前视）→ 四窗最差窗
       **−0.078**，不过闸
    C) 再补上 **62 只「历史进过池」**的全部标的（含后来缩水的，各自按
       入池日切片）→ 四窗折均值 **−0.034**、拼接口径 **+0.015**、IS Sharpe
       **−0.05**、MDD 恶化 1.65pt ⇒ **中性偏负**

⇒ A 与 C 之间 **0.5 的 dSh 全部是「今日赢家回填」的幸存者偏差**，
   不是 alpha。宇宙口径不一致**真实但无关紧要**（第 13 次同型口径缺陷
   的第二次现形：R27 的 vwap 因子 35% 评分不同 / dSh 仅 −0.008~−0.033）。

本守卫把这条**方法论**钉死：任何「把成员集合按**今天**的快照回填到历史」
的量测都必然高估；成员必须按**入池日切片**（tenure），且**必须**包含
后来掉出池子的标的，否则就是挑赢家。

用合成面板实现（不依赖网络 / 不依赖真实面板 ⇒ 确定性）。
"""
from __future__ import annotations

import math

from strategy.backtest_daily import BacktestConfig, align_panel, run_backtest

N = 160
ENTER = 80          # 合成「入池日」索引


def _synth(code: str, n: int, start_price: float, gain: float,
           phase: str = "early", seed: int = 7) -> dict:
    """确定性合成日线（无随机）。

    phase="early"：涨幅全部发生在**入池日之前**（模拟「今天的赢家」——
                   它的历史涨幅正是它今天能进池的原因）；
    phase="late" ：涨幅全部发生在入池日之后。
    """
    close = []
    for i in range(n):
        if phase == "early":
            f = min(i, ENTER) / ENTER
        else:
            f = max(0, i - ENTER) / (n - ENTER)
        c = start_price * (1.0 + gain * f) * (
            1.0 + 0.02 * math.sin((i + seed) / 5.0))
        close.append(round(c, 4))
    high = [c * 1.01 for c in close]
    low = [c * 0.99 for c in close]
    open_ = [c * 0.999 for c in close]
    return {
        "date": [f"D{i:04d}" for i in range(n)],
        "open": open_, "high": high, "low": low,
        "close": close, "volume": [1e6] * n,
    }


def _cfg() -> BacktestConfig:
    cfg = BacktestConfig()
    # 关掉日线趋势闸门等需要长预热/外部数据的通道，保证确定性
    cfg.use_gate = False
    cfg.regime_mode = "off"
    cfg.exit_mode = "trend"
    cfg.momentum_rank = True
    cfg.momentum_top_n = 5
    cfg.momentum_lookback = 10
    cfg.momentum_min_mom = 0.0
    cfg.max_positions = 5
    cfg.vol_sizing = False          # 固定金额 ⇒ 确定性、便于断言
    cfg.fixed_amount = 100000.0
    cfg.risk_per_trade = 0.02
    cfg.cost_pct = 0.0015
    cfg.manual_entry_codes = ()
    cfg.min_passing_candidates = 0
    cfg.entry_mode = "factor"
    return cfg


def test_entry_sliced_series_is_invalid_before_entry():
    """按入池日切片后，入池前的 bar 必须 `valid=False`（不可成交）。"""
    late = _synth("BBB", N, 10.0, 1.2)
    sliced = {k: list(v[ENTER:]) for k, v in late.items()}
    dates, panel = align_panel({"AAA": _synth("AAA", N, 20.0, 0.2),
                                "BBB": sliced})
    assert len(dates) == N          # 日期轴取并集、不截断
    v = panel["BBB"]["valid"]
    assert not any(v[:ENTER]), "入池前的 bar 必须不可成交"
    assert all(v[ENTER:]), "入池后应恢复可成交"


def _slice(d: dict) -> dict:
    """按「入池日」切片：只保留入池日及之后的历史。"""
    return {k: list(v[ENTER:]) for k, v in d.items()}


def test_backfill_full_history_inflates_return():
    """★ 核心：把「今天的赢家」的历史回填 ⇒ 收益被系统性高估。

    合成口径：标的涨幅**全部**发生在入池日之前（这正是它今天能进池的原因）。
    - 回填完整历史（A 口径）：回测在入池日之前就建仓，吃到全部涨幅；
    - 按入池日切片（C 口径）：入池后价格已走平 ⇒ 吃不到。

    实测（R29）：真实面板上 A 与 C 之间相差 **0.5 个 dSh**，全部来自这里。
    """
    cfg = _cfg()
    late = _synth("BBB", N, 10.0, 2.0, phase="early")
    r_full = run_backtest(["BBB"], cfg, preloaded={"BBB": late})
    r_sliced = run_backtest(["BBB"], cfg, preloaded={"BBB": _slice(late)})
    assert "error" not in r_full and "error" not in r_sliced
    assert r_full["total_return"] > r_sliced["total_return"] + 0.005, (
        f"回填历史必须显著高估收益（前视），实测 full="
        f"{r_full['total_return']:.4f} vs sliced={r_sliced['total_return']:.4f}")


def test_membership_lookahead_changes_result_so_it_must_be_controlled():
    """前视与否会改变结论 ⇒ 成员口径必须显式处理，不能默认回填。

    （这是本轮的核心教训：A 口径 +0.24~+0.58 dSh 与 C 口径 −0.034 之间的
    差额全部来自这一条，而不是来自策略本身。）
    """
    cfg = _cfg()
    base = _synth("BBB", N, 10.0, 2.0, phase="early")
    full = {"BBB": base}
    sliced = {"BBB": _slice(base)}
    r_full = run_backtest(["BBB"], cfg, preloaded=full)
    r_sliced = run_backtest(["BBB"], cfg, preloaded=sliced)
    assert abs(r_full["total_return"] - r_sliced["total_return"]) > 1e-6, (
        "成员口径（回填 vs 按入池日切片）必须产生不同结果；"
        "若相同则说明该轴是惰性的，本轮结论需要重新审视")

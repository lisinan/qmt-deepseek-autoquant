# -*- coding: utf-8 -*-
"""【2026-10-13 第 28 轮 AM-EVOLVE】动量闸门「正动量硬过滤」阈值守卫。

## 起因：**今日实盘铁证**（不是凭空假设）

2026-10-13 上午 09:15~10:15 连续 7 条（数据源=xtdata，**真实**非 mock）：

    无入场信号：动量闸门后候选池为空（当前宇宙无 60 日正动量标的）

⇒ 绑定约束已由前 5 个交易日的「评分 4.0 阈值」**换成动量闸门的 m>0 硬过滤**，
  是连续 6 个交易日零 BUY 的直接原因之一。
tushare 独立复核（截至 2026-10-13 收盘，静态 12 只）：60 日动量 > 0 的**仅 2 只**
（688111.SH +2.60% / 002415.SZ +1.30%），其余 −4.9% ~ −35.2% ⇒ 过滤确实在生效。

## 两侧现状同源（改之前必须知道）

  回测 ``backtest_daily.py``            ``if raw <= 0: continue``
  实盘 ``daily_context.py::top_momentum`` ``if m > 0:``
⇒ 提升为可配置 ``momentum_min_mom``，**默认 0.0 与两侧现状逐位相同**。

## 本轮裁决：落盘 0.15（四窗共识最差 dSh **+0.115** ≥ +0.10）

911 根 tushare 真实面板（20230104→20261013）、4 套互不重叠折划分：

  阈值    60x9     75x7     90x6    120x4    最差    均值    IS收益   IS_Sh
  0.00   +0.000   +0.000   +0.000  +0.000  +0.000  +0.000  +128.4%  1.86 ← 旧现状
  0.05   +0.028   +0.055   +0.060  +0.009  +0.009  +0.038  +135.4%  1.90
  0.10   -0.002   +0.103   +0.112  +0.054  -0.002  +0.067  +136.5%  1.91
  **0.14 +0.424   +0.255   +0.183  +0.115  +0.115  +0.244  +134.8%  1.90**
  **0.15 +0.408   +0.239   +0.167  +0.115  +0.115  +0.232  +131.9%  1.89 ← 落盘**
  **0.16 +0.404   +0.235   +0.160  +0.101  +0.101  +0.225  +131.4%  1.88**
  0.20   +0.262   +0.109   +0.044  -0.016  -0.016  +0.100  +120.4%  1.82
  -0.10  +0.000   +0.000   +0.000  +0.000  +0.000  +0.000  +128.4%  1.86 ← **完全惰性**
  -0.35  +0.000   +0.000   +0.000  +0.000  +0.000  +0.000  +128.4%  1.86 ← **完全惰性**

★ 两个方向的结论都重要：
  · **放宽（负阈值）完全惰性 dSh=0.000** —— 因为 top_n=3 取的是动量最高者，
    放宽进池的弱/负动量标的根本排不进前 3，且 trend_up + 评分 4.0 会把它们挡掉
    ⇒ **动量闸门不是低暴露的真凶，别再往「放宽」方向投**。
  · **收紧（正阈值）是真高原**：0.14/0.15/0.16 三档**连续**过闸门（非尖峰），
    逐折复核 26 折中 **11 折改善、0 折恶化**，且改善集中在**负 Sharpe 的弱折**
    （60x9 F9 −3.433→0.000、75x7 F7 −3.500→−2.025、90x6 F6 −1.209→−0.359）
    ⇒ 机理与 R24「trend_up 严格度本身就是 alpha 源」一致：
      弱市里「矮子里拔将军」买最不弱的票 = 亏损来源。
"""
from __future__ import annotations

import math
import random
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import STRATEGY_PARAMS  # noqa: E402
from strategy.backtest_daily import BacktestConfig, run_backtest  # noqa: E402
from strategy.daily_context import DailyContext  # noqa: E402


# ------------------------------------------------------------------ 合成面板

def _mk(n: int = 300, seed: int = 1, drift: float = 0.003):
    """确定性合成日线（不依赖网络/真实行情，CI 可复现）。"""
    rnd = random.Random(seed)
    c = [100.0]
    for i in range(n - 1):
        c.append(max(5.0, c[-1] * (1 + drift + 0.012 * math.sin(i / 9.0)
                                   + rnd.uniform(-0.004, 0.004))))
    o = [x * (1 + rnd.uniform(-0.003, 0.003)) for x in c]
    h = [x * 1.010 for x in c]
    l = [x * 0.990 for x in c]
    v = [float(int(1e6 * (1 + rnd.uniform(-0.2, 0.2)))) for _ in c]
    return dict(open=o, high=h, low=l, close=c, volume=v)


def _panel():
    """2 只强动量(>15%) + 3 只弱正动量(0<m<15%) + 1 只负动量。

    设计目的：top_n=3 时，阈值 0.0 会把「2 强 + 1 弱」放进池，
    阈值 0.15 只剩「2 强」⇒ 两侧**必须**产生不同成交。
    """
    return {
        "A1": _mk(drift=0.0045, seed=1),
        "A2": _mk(drift=0.0040, seed=2),
        "B1": _mk(drift=0.0008, seed=3),
        "B2": _mk(drift=0.0007, seed=4),
        "B3": _mk(drift=0.0006, seed=5),
        "C1": _mk(drift=-0.002, seed=6),
    }


def _cfg() -> BacktestConfig:
    # use_gate=False / 评分门槛归零：把「入池」隔离成唯一变量，
    # 否则趋势闸门与评分会一起变，测不出动量阈值本身的作用。
    return BacktestConfig(
        momentum_rank=True, momentum_top_n=3, momentum_lookback=60,
        min_warmup=70, cost_pct=0.0015, use_gate=False,
        buy_score_threshold=0.0, min_signals=0, min_passing_candidates=0,
        exit_mode="trend")


def _run(mm: float, panel=None):
    panel = panel or _panel()
    return run_backtest(list(panel), replace(_cfg(), momentum_min_mom=mm),
                        count=300, preloaded=panel)


# ------------------------------------------------------------------ 实盘侧桩

class _StubDaily:
    """只提供 top_momentum 需要的 momentum_60d（不触网、不读行情）。"""

    def __init__(self, moms: dict):
        self._m = moms

    def momentum_60d(self, code: str, lookback: int = 60) -> float:
        return float(self._m.get(code, 0.0))


def _live(codes, moms, top_n=5, lookback=60):
    return DailyContext.top_momentum(_StubDaily(moms), codes, top_n, lookback)


# ------------------------------------------------------------------ 用例

def test_backtest_default_is_zero():
    """① 回测默认 0.0 = 旧「必须为正」语义（零行为变化）。"""
    assert BacktestConfig().momentum_min_mom == 0.0


def test_live_default_matches_legacy_positive_only():
    """② 实盘未配置（默认 0.0）时，只返回**正动量**标的 —— 与旧硬编码 `m > 0` 同义。"""
    codes = ["P1", "P2", "Z0", "N1"]
    moms = {"P1": 0.20, "P2": 0.01, "Z0": 0.0, "N1": -0.05}
    old = STRATEGY_PARAMS.pop("momentum_min_mom", None)
    try:
        got = _live(codes, moms)
    finally:
        if old is not None:
            STRATEGY_PARAMS["momentum_min_mom"] = old
    assert got == ["P1", "P2"], got          # Z0(=0) 与 N1(<0) 都被挡掉


def test_live_threshold_honors_param_and_is_monotone():
    """③ 实盘读取点真实生效 + 阈值越高池越小（子集单调）。"""
    codes = ["P1", "P2", "Z0", "N1", "N2"]
    moms = {"P1": 0.20, "P2": 0.08, "Z0": 0.0, "N1": -0.05, "N2": -0.30}
    old = STRATEGY_PARAMS.get("momentum_min_mom")
    try:
        STRATEGY_PARAMS["momentum_min_mom"] = 0.15
        strict = _live(codes, moms)
        STRATEGY_PARAMS["momentum_min_mom"] = 0.0
        base = _live(codes, moms)
        STRATEGY_PARAMS["momentum_min_mom"] = -0.10
        loose = _live(codes, moms)
    finally:
        if old is None:
            STRATEGY_PARAMS.pop("momentum_min_mom", None)
        else:
            STRATEGY_PARAMS["momentum_min_mom"] = old
    assert strict == ["P1"], strict
    assert base == ["P1", "P2"], base
    # 阈值 -0.10 下 Z0(=0.0) 也进池（0.0 > -0.10），N2(-0.30) 仍被挡
    assert loose == ["P1", "P2", "Z0", "N1"], loose
    assert set(strict) <= set(base) <= set(loose)  # 子集单调


def test_backtest_threshold_really_changes_behavior():
    """④ 回测侧「开启后真变」：0.15 的成交数必须**显著少于** 0.0（防死代码）。"""
    r0 = _run(0.0)
    r15 = _run(0.15)
    assert r0.get("n_trades", 0) > 0, "合成面板在阈值 0 下必须有成交"
    assert r15.get("n_trades", 0) > 0, "阈值 0.15 仍应有强动量标的成交"
    assert r15["n_trades"] < r0["n_trades"], (
        f"momentum_min_mom 未生效（死代码）：0.0={r0['n_trades']} 笔 vs "
        f"0.15={r15['n_trades']} 笔")


def test_backtest_threshold_monotone_subset():
    """⑤ 阈值越高 ⇒ 入池集合单调收缩 ⇒ 成交数单调不增（数学性质，非拟合）。"""
    ns = [_run(v)["n_trades"] for v in (-0.35, 0.0, 0.15, 0.30)]
    assert ns[0] >= ns[1] >= ns[2] >= ns[3], ns


def test_validator_and_production_in_sync():
    """⑥ 验证器 P0 与生产必须同值（防本项目第 12 次同型「基线漂移」）。"""
    from strategy._evolve_wf import base_cfg
    prod = float(STRATEGY_PARAMS.get("momentum_min_mom", 0.0))
    assert float(base_cfg().momentum_min_mom) == prod, (
        f"P0 基线漂移：验证器={base_cfg().momentum_min_mom} 生产={prod}")

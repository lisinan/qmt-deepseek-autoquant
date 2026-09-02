# -*- coding: utf-8 -*-
"""
回归测试：实盘日线评分器 (DailyContext._compute) 必须与已验证回测评分器
(backtest_daily.score_daily / score_daily_series) 严格一致。

为什么需要：
  别人重构 live 路径时曾把 _compute 的 oversold 因子里的 KDJ 整段删掉，
  并误把「RSI 无数据」当成 rsi=0=超卖，导致 live 与回测在约 4.4% 的临界
  买入信号上「两张皮」。本项目收益 100% 来自「价格动量+趋势骑行+集中最强
  5 只+波动率目标」这一已验证口径，任何 live/回测偏离都直接侵蚀实盘收益。

本测试用确定性合成面板（seeded）逐 bar 比对（含预热区 i<14 的 RSI 无数据
分支），任一因子或总分非 0 偏差即失败。真实日线比对见
strategy/_verify_daily_context_consistency.py（手动深跑，需 xtdata 可达）。

运行（conda env qmt）：python tests/run_all.py
"""
from __future__ import annotations

import random

from config.settings import STOCK_CODES  # noqa: F401  (确保项目可 import)
from strategy.daily_context import DailyContext
from strategy import backtest_daily as B

_FACTORS = ("trend", "momentum", "oversold", "volume", "position", "vwap")
_BUY_THRESHOLD = 4.0


def _make_synth_panel(n_stocks: int = 23, n_bars: int = 120,
                      seed: int = 20260902):
    rnd = random.Random(seed)
    panel = {}
    for s in range(n_stocks):
        close = [100.0]
        for _ in range(n_bars - 1):
            close.append(max(1.0, close[-1] * (1 + rnd.uniform(-0.05, 0.05))))
        o, h, l, v = [], [], [], []
        for i in range(n_bars):
            prev = close[i - 1] if i > 0 else close[i] * 0.99
            op = prev * (1 + rnd.uniform(-0.01, 0.01))
            cl = close[i]
            hi = max(op, cl) * (1 + rnd.uniform(0.0, 0.03))
            lo = min(op, cl) * (1 - rnd.uniform(0.0, 0.03))
            vol = rnd.uniform(0.5, 3.0) * 1_000_000.0
            o.append(op); h.append(hi); l.append(lo); v.append(vol)
        panel[f"SYN{s:02d}"] = {"open": o, "high": h, "low": l,
                                "close": close, "volume": v}
    # 手工边界序列：强制命中 RSI 低/高、KDJ<20/>100、量能比、BOLL、VWAP 各分支
    c_a = [max(5.0, 100.0 - i) for i in range(n_bars)]
    c_b = [100.0 + i for i in range(n_bars)]
    c_c = [100.0 + 0.2 * (i % 2) for i in range(n_bars)]
    c_d = [100.0 * (1 + 0.01 * i) for i in range(n_bars)]
    for name, cl in (("EDGE_A", c_a), ("EDGE_B", c_b),
                     ("EDGE_C", c_c), ("EDGE_D", c_d)):
        o, h, l, v = [], [], [], []
        for i in range(n_bars):
            prev = cl[i - 1] if i > 0 else cl[i] * 0.99
            op = prev * (1 + rnd.uniform(-0.01, 0.01))
            hi = max(op, cl[i]) * (1 + 0.02)
            lo = min(op, cl[i]) * (1 - 0.02)
            mul = 3.0 if (name == "EDGE_D" and i > n_bars * 0.7) else 1.0
            vol = rnd.uniform(0.8, 1.2) * 1_000_000.0 * mul
            o.append(op); h.append(hi); l.append(lo); v.append(vol)
        panel[name] = {"open": o, "high": h, "low": l,
                       "close": cl, "volume": v}
    return panel


def test_daily_context_matches_backtest_scorer():
    """逐 bar（含预热区）比对 _compute 与 score_daily 的 6 因子 + 总分。"""
    panel = _make_synth_panel()
    diff_points = 0
    straddle = 0
    worst = 0.0
    for code, d in panel.items():
        n = len(d["close"])
        for i in range(n):  # 含 i<14（RSI 无数据分支）
            o = d["open"][:i + 1]; h = d["high"][:i + 1]
            l = d["low"][:i + 1]; c = d["close"][:i + 1]
            v = d["volume"][:i + 1]
            dd = {"open": o, "high": h, "low": l, "close": c, "volume": v}
            live = DailyContext._compute_for_test(code, dd)
            bt_score, bt_factors = B.score_daily(o, h, l, c, v)
            for k in _FACTORS:
                if abs((live.factors.get(k, 0.0) or 0.0)
                       - (bt_factors.get(k, 0.0) or 0.0)) > 1e-9:
                    diff_points += 1
            sd = abs(live.score - bt_score)
            worst = max(worst, sd)
            if sd > 1e-9:
                diff_points += 1
            if (live.score >= _BUY_THRESHOLD) != (bt_score >= _BUY_THRESHOLD):
                straddle += 1
    assert diff_points == 0, (
        f"_compute 与 score_daily 存在 {diff_points} 个差异点 "
        f"(最大总分偏差 {worst:.4f}，决策翻转 {straddle})")
    assert straddle == 0, f"存在 {straddle} 个临界买入决策翻转"


def test_backtest_score_daily_matches_score_daily_series():
    """回测内部两版评分器末根须一致（防止回归破坏回测自身）。"""
    panel = _make_synth_panel(n_stocks=8, n_bars=120, seed=7)
    mism = 0
    for d in panel.values():
        o, h, l, c, v = (d["open"], d["high"], d["low"],
                         d["close"], d["volume"])
        s_last, f_last = B.score_daily(o, h, l, c, v)
        s_s, f_s = B.score_daily_series(o, h, l, c, v)[-1]
        if abs(s_last - s_s) > 1e-9:
            mism += 1
        for k in _FACTORS:
            if abs((f_last.get(k, 0) or 0) - (f_s.get(k, 0) or 0)) > 1e-9:
                mism += 1
    assert mism == 0, f"score_daily vs score_daily_series 末根不一致: {mism}"

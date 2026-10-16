# -*- coding: utf-8 -*-
"""【PM-EVOLVE R35】信念仓位（conviction sizing）**实盘读取点**守卫。

背景（为什么要这个测试）：R29（2026-10-13）已在回测里建好
``BacktestConfig.conviction_mode / conviction_boost / conviction_breadth_thr``，
但 live 引擎没有任何读取点 ⇒ 若直接改 ``config/settings.py`` 就会制造
「回测有效、实盘死代码」的第 N 次口径背离。R35 补上实盘链路：

    config.settings.STRATEGY_PARAMS["conviction_*"]
        -> strategy.daily_context.DailyContext.conviction_multiplier()
        -> engine.event_engine 新开仓 qty 乘子（在现金夹紧之前）

本守卫钉死五件事：
  1. 生产默认 **未启用**（mode="off"）⇒ 任何宽度下乘子恒 1.0，零行为变化；
  2. 启用后 **真的会变**（宽度达标 → 乘子 == boost），防代理退化成死代码；
  3. 宽度不达标 / 宽度不可得 ⇒ fail-safe 回落到 1.0（绝不反向缩小、绝不报错）；
  4. 「只改 mode 不改 boost」的**静默空操作陷阱**被显式拒绝；
  5. 引擎源码里**确实存在**读取点（防日后重构把调用删掉而测试仍绿）。
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

import config.settings as S
from strategy.daily_context import DailyContext


def _ctx_with_closes(closes: dict) -> DailyContext:
    """构造一个只装载了 _closes 的 DailyContext（不触发任何网络/行情）。"""
    ctx = DailyContext.__new__(DailyContext)          # 跳过 __init__（会建锁/线程）
    import threading
    ctx._lock = threading.RLock()
    ctx._closes = dict(closes)
    ctx._feats = {}
    return ctx


def _flat(n: int, price: float) -> list:
    return [float(price)] * n


def _rising(n: int) -> list:
    return [100.0 + i for i in range(n)]


def _set_params(mode, boost, thr):
    S.STRATEGY_PARAMS["conviction_mode"] = mode
    S.STRATEGY_PARAMS["conviction_boost"] = boost
    S.STRATEGY_PARAMS["conviction_breadth_thr"] = thr


def _restore():
    _set_params("off", 1.0, 0.6)


def test_production_default_is_disabled():
    """锁：生产当前**未启用**信念加仓（R35 只接线不启用，待 OWNER 授权）。"""
    assert S.STRATEGY_PARAMS["conviction_mode"] == "off"
    assert float(S.STRATEGY_PARAMS["conviction_boost"]) == 1.0
    assert float(S.STRATEGY_PARAMS["conviction_breadth_thr"]) == 0.6


def test_disabled_returns_one_for_any_breadth():
    """mode="off" ⇒ 无论宽度多高，乘子恒 1.0（零行为变化）。"""
    ctx = _ctx_with_closes({"300308.SZ": _rising(120), "300502.SZ": _rising(120)})
    _set_params("off", 2.0, 0.6)
    try:
        assert ctx.conviction_multiplier() == 1.0
    finally:
        _restore()


def test_enabled_actually_changes_when_breadth_high():
    """广度达标 ⇒ 乘子 == boost（防「接线了但恒等」的死代码）。"""
    ctx = _ctx_with_closes({"300308.SZ": _rising(120), "300502.SZ": _rising(120)})
    _set_params("breadth", 1.15, 0.6)
    try:
        m = ctx.conviction_multiplier()
        assert abs(m - 1.15) < 1e-9, f"宽度达标却未放大: {m}"
    finally:
        _restore()


def test_enabled_returns_one_when_breadth_low():
    """广度不达标 ⇒ 乘子 1.0（不在弱市背杠杆）。"""
    # 两只都在 MA60 下方：前 119 根高位、末根暴跌 ⇒ 收盘 < MA60
    falling = [1000.0] * 119 + [1.0]
    ctx = _ctx_with_closes({"300308.SZ": falling, "300502.SZ": falling})
    _set_params("breadth", 1.15, 0.6)
    try:
        assert ctx.conviction_multiplier() == 1.0
    finally:
        _restore()


def test_no_data_is_fail_safe():
    """宽度不可得（无标的 / 数据不足）⇒ 1.0，不放大也不抛异常。"""
    _set_params("breadth", 1.15, 0.6)
    try:
        assert _ctx_with_closes({}).conviction_multiplier() == 1.0
        assert _ctx_with_closes({"300308.SZ": [1.0]}).conviction_multiplier() == 1.0
    finally:
        _restore()


def test_silent_noop_trap_is_rejected():
    """★ 陷阱守卫：若有人启用 mode="breadth" 却把 boost 留在 <=1.0，
    那是**静默空操作**（乘子恒 1，看起来生效实际没变）。启用必须带 boost>1。"""
    mode = S.STRATEGY_PARAMS["conviction_mode"]
    boost = float(S.STRATEGY_PARAMS["conviction_boost"])
    if mode == "breadth":
        assert boost > 1.0, (
            "conviction_mode 已启用但 boost<=1.0 ⇒ 静默空操作。"
            "启用时应同时把 boost 设为实测最优 1.15（见 settings 内 R35 证据）。"
        )


def test_engine_has_read_point():
    """引擎源码必须真的调用 conviction_multiplier（防重构删掉调用而测试仍绿）。"""
    from pathlib import Path
    src = Path("engine/event_engine.py").read_text(encoding="utf-8")
    assert "conviction_multiplier" in src, "engine/event_engine.py 信念仓位读取点被移除"
    assert "_clamp_to_buying_power" in src  # 放大后仍须经现金夹紧
    # 乘子必须作用在 qty 上（而不是只算不用）
    assert "raw_qty * scale * conv_mult" in src


def test_breadth_ratio_matches_backtest_definition():
    """宽度定义必须与回测同源：close > MA(ma) 的宇宙占比（排除指数、数据不足者）。"""
    # 一只持续上涨（收盘 >> MA60），一只长期横盘后末根小跌（收盘 < MA60）
    ctx = _ctx_with_closes({"300308.SZ": _rising(120),
                            "300502.SZ": _flat(119, 100.0) + [99.0]})
    r = ctx.breadth_ratio(ma=60)
    assert r is not None
    assert abs(r - 0.5) < 1e-9, f"宽度应为 1/2，实际 {r}"
    # 与既有布尔门语义一致
    assert ctx.breadth_above_ma(ma=60, thresh=0.5) is True
    assert ctx.breadth_above_ma(ma=60, thresh=0.9) is False

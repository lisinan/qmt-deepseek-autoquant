"""加固 A 回归测试：日线兜底强平闭合「无 tick 持仓」的分钟止损盲区。

背景：原 _run_once 退出循环在 ``code not in ticks`` 时直接 ``continue``，导致当轮
没有 tick 的持仓永远不进入 TrendStrategy.on_exit——开盘跳空 / 一字跌停 / 长停复盘 /
稀疏 tick 场景下，硬止损 -18% / 趋势破位 / 持仓超时等退出条件会滞后触发。

加固：新增 EventEngine._daily_fallback_exit，用 DailyContext 最新日线 close 作价格
代理重算 on_exit，使无 tick 持仓也周期性得到退出再评估。本文件锁定这一行为。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import STRATEGY_PARAMS                  # noqa: E402
from strategy.daily_context import DailyFeatures             # noqa: E402
from strategy.trend_strategy import TrendStrategy            # noqa: E402
import engine.event_engine as EE                              # noqa: E402


def _engine():
    eng = EE.EventEngine(exec_mode="paper", auto_init_positions=False,
                         enable_sector_scorer=False,
                         enable_dynamic_universe=False,
                         enable_llm_reranker=False)
    eng.analyst = type("FakeAnalyst", (), {"enabled": False})()
    return eng


class FakeDaily:
    """最小 DailyContext 桩：features + trend_broken + atr_pct + is_ready。"""

    def __init__(self, feats: dict, broken: bool = False):
        self._feats = feats
        self._broken = broken

    def features(self, code):
        return self._feats.get(code)

    def trend_broken(self, code, ma=60):
        return self._broken

    def atr_pct(self, code):
        return 0.0

    def is_ready(self):
        return True


class FakePos:
    """最小持仓桩：on_exit 仅读 quantity/avg_cost/open_date/name。"""

    def __init__(self, code, name, qty, avg_cost, open_date=None):
        self.code = code
        self.name = name
        self.quantity = qty
        self.avg_cost = avg_cost
        self.open_date = open_date
        self.last_price = avg_cost
        self.peak_price = avg_cost
        self.stop_price = 0.0


def _wire(eng, feats, broken=False):
    """装好 daily / strategy / positions 并接管 _handle_sell 记录调用。"""
    daily = FakeDaily(feats, broken=broken)
    eng.daily = daily
    strat = TrendStrategy()
    strat.p = dict(STRATEGY_PARAMS)
    strat.p["exit_mode"] = "trend"
    strat.p["hard_stop_pct"] = -0.18
    strat.daily = daily
    eng._trend = strat
    eng.strategy = strat
    eng._positions = {}
    calls = []

    def _rec(sig, pos):
        calls.append(sig)

    eng._handle_sell = _rec
    return eng, calls


def test_daily_fallback_triggers_hard_stop_on_gap_down():
    """无 tick 持仓，日线 close 较成本 -20%（≤ -18%）→ 触发硬止损卖出。"""
    eng = _engine()
    feats = {"A": DailyFeatures(code="A", close=80.0, score=0.0,
                                factors={}, trend_up=False, bias=-1.0)}
    eng, calls = _wire(eng, feats)
    pos = FakePos("A", "测试A", 100, 100.0)
    eng._positions["A"] = pos

    eng._daily_fallback_exit("A", pos)

    assert len(calls) == 1, f"跳空 -20% 应触发强平，实际 calls={len(calls)}"
    assert calls[0].side == "SELL"
    assert "硬止损" in calls[0].reason, f"原因应含『硬止损』，实际 {calls[0].reason}"


def test_daily_fallback_uses_daily_close_as_price():
    """兜底卖出价应取日线 close（价格代理），而非任意默认值。"""
    eng = _engine()
    feats = {"A": DailyFeatures(code="A", close=80.0, score=0.0,
                                factors={}, trend_up=False, bias=-1.0)}
    eng, calls = _wire(eng, feats)
    pos = FakePos("A", "测试A", 100, 100.0)
    eng._positions["A"] = pos

    eng._daily_fallback_exit("A", pos)

    assert len(calls) == 1
    assert calls[0].price == 80.0, f"卖出价应=日线close 80.0，实际 {calls[0].price}"


def test_daily_fallback_no_exit_when_healthy():
    """无 tick 持仓，日线 close 仅 -5%（> -18%）且无趋势破位 → 不卖出。"""
    eng = _engine()
    feats = {"A": DailyFeatures(code="A", close=95.0, score=0.0,
                                factors={}, trend_up=True, bias=0.3)}
    eng, calls = _wire(eng, feats)
    pos = FakePos("A", "测试A", 100, 100.0)
    eng._positions["A"] = pos

    eng._daily_fallback_exit("A", pos)

    assert len(calls) == 0, f"健康持仓不应卖出，实际 calls={len(calls)}"


def test_daily_fallback_triggers_on_trend_broken():
    """无 tick 持仓但日线趋势破位（trend_broken=True）→ 触发趋势破位离场。"""
    eng = _engine()
    feats = {"A": DailyFeatures(code="A", close=110.0, score=0.0,
                                factors={}, trend_up=True, bias=0.5)}
    eng, calls = _wire(eng, feats, broken=True)
    pos = FakePos("A", "测试A", 100, 100.0)
    eng._positions["A"] = pos

    eng._daily_fallback_exit("A", pos)

    assert len(calls) == 1, f"趋势破位应触发离场，实际 calls={len(calls)}"
    assert "趋势破位" in calls[0].reason, \
        f"原因应含『趋势破位』，实际 {calls[0].reason}"


def test_daily_fallback_skips_when_no_daily_features():
    """DailyContext.features(code) 返回 None（缺日线数据）→ 安全跳过，不误杀。"""
    eng = _engine()
    feats = {}  # A 无任何日线特征
    eng, calls = _wire(eng, feats)
    pos = FakePos("A", "测试A", 100, 100.0)
    eng._positions["A"] = pos

    eng._daily_fallback_exit("A", pos)

    assert len(calls) == 0, "缺日线数据不应卖出（避免误杀/卡死）"


def test_daily_fallback_skips_when_daily_none():
    """eng.daily 未就绪（None）→ 安全跳过。"""
    eng = _engine()
    eng.daily = None
    strat = TrendStrategy()
    strat.p = dict(STRATEGY_PARAMS)
    strat.p["exit_mode"] = "trend"
    eng._trend = strat
    eng.strategy = strat
    calls = []
    eng._handle_sell = lambda sig, pos: calls.append(sig)
    pos = FakePos("A", "测试A", 100, 100.0)

    eng._daily_fallback_exit("A", pos)

    assert len(calls) == 0, "daily 未就绪不应卖出"


def test_run_once_no_tick_branch_routes_to_daily_fallback_exit():
    """架构回归（加固 A）：_run_once 中无 tick 持仓必须路由到 _daily_fallback_exit，
    不能像旧实现那样 ``code not in ticks: continue`` 跳过 on_exit（分钟止损盲区）。
    """
    src = (ROOT / "engine" / "event_engine.py").read_text(encoding="utf-8")
    m = re.search(r"def _run_once\(self.*?(?=\n    def |\nclass )",
                  src, re.DOTALL)
    assert m, "_run_once 未找到"
    body = m.group(0)
    assert "self._daily_fallback_exit(code, pos)" in body, (
        "_run_once 的无 tick 分支必须调 self._daily_fallback_exit(code, pos)，"
        "否则回归到『无 tick 持仓跳过 on_exit』的分钟止损盲区。")

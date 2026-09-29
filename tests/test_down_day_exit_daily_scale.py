# -*- coding: utf-8 -*-
"""「单日暴跌退出」必须是**日线**涨跌幅口径（与回测同尺度）。

背景（2026-09-29 AM-EVOLVE 第 12 轮）：
  回测 ``backtest_daily.py`` 的 crash 判定用的是日线口径::

      (close[i] / close[i - 1] - 1) * 100 <= cfg.down_day_exit_pct

  而实盘 ``TrendStrategy.on_exit`` 原来取 ``bars`` —— 那是引擎的 **1 分钟 bar**
  缓冲（``event_engine.py`` 里 ``bars = list(self._bars.get(code, []))``）⇒
  量到的是「相邻两分钟」的涨跌幅，与日线差约两个数量级。
  后果：单分钟跌 5% 极罕见 ⇒ 无论阈值设成多少，实盘该分支都近乎永不触发
  （等价于生产当时 -99.0 的"关闭"状态）。若只改阈值而不补实现，就会制造
  「回测有效、实盘无效」的口径背离 —— 项目铁律明令禁止。

本文件把「日线尺度」钉死：
  1. DailyContext 必须产出 day_change_pct（相邻两根**日线**收盘价之比）；
  2. 日跌 -6% 但分钟线走平时 ⇒ **必须**触发暴跌退出；
  3. 分钟跌 -6% 但日线只跌 -0.5% 时 ⇒ **不得**触发（证明不是分钟尺度）；
  4. 取不到日线时退出必须静默放弃，绝不退化到分钟 bar（避免语义静默漂移）；
  5. 生产与验证器阈值同步为 -5.0。
"""

from datetime import datetime

from core.data_models import Position
from config.settings import STRATEGY_PARAMS
from strategy.daily_context import DailyContext, DailyFeatures
from strategy.trend_strategy import TrendStrategy


CODE = "300308.SZ"


class _FakeDaily:
    """最小 DailyContext 桩：只暴露 day_change_pct / trend_broken / features。"""

    def __init__(self, change=None, broken=False, has_feature=True):
        self._change = change
        self._broken = broken
        self._has_feature = has_feature

    def is_ready(self):
        return True

    def day_change_pct(self, code):
        return self._change

    def trend_broken(self, code, exit_ma=60):
        return self._broken

    def features(self, code):
        if not self._has_feature:
            return None
        return DailyFeatures(code=code, close=100.0,
                             day_change_pct=self._change or 0.0)


def _pos():
    return Position(code=CODE, name="中际旭创", quantity=100,
                    avg_cost=100.0, last_price=100.0,
                    open_date=datetime.now(), peak_price=110.0)


def _minute_bars(prev_close: float, last_close: float, n: int = 40):
    """构造分钟 bar：前 n-1 根恒为 prev_close，最后一根跳到 last_close。"""
    bars = []
    for i in range(n):
        c = prev_close if i < n - 1 else last_close

        class _B:
            close = c
            high = c
            low = c
            open = c
            volume = 1000

        bars.append(_B())
    return bars


def _strategy(daily):
    s = TrendStrategy(params={"exit_mode": "trend",
                              "down_day_exit_pct": -5.0,
                              "hard_stop_pct": -0.18,
                              "trend_max_hold_days": 120,
                              "take_profit": 0.12})
    s.daily = daily
    return s


# ------------------------------------------------ 1) DailyContext 日线口径

def test_daily_context_computes_day_change_from_daily_closes():
    """day_change_pct 必须来自相邻两根**日线**收盘价。"""
    vals = DailyContext._compute_for_test(
        CODE, {"open": [10.0] * 50, "high": [10.0] * 50,
               "low": [10.0] * 50, "close": [10.0] * 49 + [9.4],
               "volume": [1000.0] * 50})
    assert abs(vals.day_change_pct - (-0.06)) < 1e-9, \
        f"日跌 6% 应得 -0.06，实际 {vals.day_change_pct}"


def test_daily_context_day_change_accessor():
    """day_change_pct(code) 有值时透传；查无此标的时返回 None（而非 0.0）。"""
    dc = DailyContext(codes=[CODE])
    dc._feats[CODE] = DailyFeatures(code=CODE, close=94.0, day_change_pct=-0.06)
    assert abs(dc.day_change_pct(CODE) - (-0.06)) < 1e-9
    assert dc.day_change_pct("000000.SZ") is None, \
        "查无此标的必须返回 None，让调用方放弃暴跌退出"


# ---------------------------------- 2) 日线暴跌必须触发（哪怕分钟线走平）

def test_crash_exit_fires_on_daily_drop_even_when_minutes_flat():
    """日跌 -6%、分钟线完全走平 ⇒ 仍必须触发（旧实现会漏掉）。"""
    daily = _FakeDaily(change=-0.06)
    s = _strategy(daily)
    bars = _minute_bars(94.0, 94.0)          # 分钟级零波动
    sig = s.on_exit(CODE, _pos(), 94.0, bars)
    assert sig is not None and sig.side == "SELL", \
        "日跌 -6% 应触发单日暴跌退出（旧分钟尺度实现会漏）"
    assert "暴跌" in sig.reason, sig.reason


# ------------------------------ 3) 分钟暴跌、日线平稳 ⇒ 不得触发（关键回归）

def test_crash_exit_ignores_minute_scale_drop():
    """分钟跌 -6% 而日线只跌 -0.5% ⇒ 不得触发（证明已不是分钟尺度）。"""
    daily = _FakeDaily(change=-0.005)
    s = _strategy(daily)
    bars = _minute_bars(100.0, 94.0)         # 最后一根分钟 bar 跌 6%
    sig = s.on_exit(CODE, _pos(), 94.0, bars)
    assert sig is None or sig.side != "SELL", \
        f"日线仅跌 0.5% 不应触发，实际 {sig.reason if sig else None}"


# ---------------------------------- 4) 无日线 ⇒ 静默放弃，不退化到分钟 bar

def test_crash_exit_silent_when_no_daily_context():
    """取不到日线时必须放弃该退出，绝不用分钟 bar 顶替（语义静默漂移）。"""
    s = _strategy(None)
    bars = _minute_bars(100.0, 80.0)         # 分钟暴跌 -20%
    sig = s.on_exit(CODE, _pos(), 80.0, bars)
    assert sig is None or "暴跌" not in (sig.reason or ""), \
        "无日线上下文时不得按分钟涨跌幅判暴跌"

    s2 = _strategy(_FakeDaily(change=None, has_feature=False))
    sig2 = s2.on_exit(CODE, _pos(), 80.0, bars)
    assert sig2 is None or "暴跌" not in (sig2.reason or ""), \
        "日线查无此标的时不得按分钟涨跌幅判暴跌"


def test_day_change_pct_helper_returns_none_without_daily():
    s = TrendStrategy()
    s.daily = None
    assert s._day_change_pct(CODE) is None


# ------------------------------------------------ 5) 阈值两侧同步

def test_production_threshold_is_enabled():
    """生产必须已启用（-5.0），且验证器基线同值（防再次无声漂移）。"""
    assert STRATEGY_PARAMS["down_day_exit_pct"] == -5.0, \
        f"生产阈值应为 -5.0，实际 {STRATEGY_PARAMS['down_day_exit_pct']}"
    from strategy._evolve_wf import base_cfg
    assert base_cfg().down_day_exit_pct == -5.0, \
        "验证器基线必须与生产同值（见 test_evolve_baseline_sync）"

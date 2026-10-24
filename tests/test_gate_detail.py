# -*- coding: utf-8 -*-
"""入场闸门逐只明细（Observability.gate_detail）单元测试。

为什么单独测：这块是「学量化/排障」的主要观测面，它的语义是
「每只票差在哪一条」——错了会把人教错。用假 owner + 假 DailyContext 纯逻辑验证，
不连 miniQMT、不读生产库。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.observability import Observability  # noqa: E402


class _FakeDaily:
    """最小 DailyContext 桩：只需 covered_codes / features / momentum_60d / breadth_ratio。"""

    def __init__(self, feats, moms):
        self._feats = feats
        self._moms = moms

    def covered_codes(self):
        return list(self._feats.keys())

    def features(self, code):
        return self._feats.get(code)

    def momentum_60d(self, code, lookback=60):
        return self._moms.get(code)

    def breadth_ratio(self, ma=60):
        return 0.25


def _feat(code, **kw):
    base = dict(code=code, name=code, close=10.0, ma20=9.0, ma60=8.0,
                ma20_slope=0.05, macd_hist=0.1, above_ma20=True,
                above_ma60=True, bias=0.3, trend_up=True, score=5.0,
                factors={"a": 1.0, "b": 1.0, "c": 1.0}, day_change_pct=0.01)
    base.update(kw)
    return SimpleNamespace(**base)


def _engine_with(daily):
    return SimpleNamespace(daily=daily, data_mode="xtdata",
                           risk=SimpleNamespace(is_halted=False),
                           _market_stale=False)


def test_gate_detail_all_pass_and_blocked_reason():
    feats = {
        "AAA.SH": _feat("AAA.SH"),                       # 全通过
        "BBB.SH": _feat("BBB.SH", above_ma60=False,      # 卡在 ②
                        trend_up=False),
        "CCC.SH": _feat("CCC.SH"),                       # 动量不足 → 卡在 ⑥
    }
    moms = {"AAA.SH": 0.30, "BBB.SH": 0.30, "CCC.SH": 0.01}
    obs = Observability(_engine_with(_FakeDaily(feats, moms)))
    out = obs.gate_detail()
    rows = {r["code"]: r for r in out["rows"]}

    assert out["summary"]["ready"] is True
    assert out["summary"]["universe_n"] == 3
    assert out["summary"]["trend_up_n"] == 2      # AAA + BBB(标志位写的是 True→见下)
    # BBB 被显式标 trend_up=False
    assert rows["BBB.SH"]["trend_up"] is False
    # 第一处卡点按「动量 → MA60 → 均线排列 → 斜率 → MACD → 评分」顺序给出
    assert "MA60" in rows["BBB.SH"]["blocked_by"], rows["BBB.SH"]["blocked_by"]
    assert "动量" in rows["CCC.SH"]["blocked_by"], rows["CCC.SH"]["blocked_by"]
    assert rows["AAA.SH"]["blocked_by"] == "", rows["AAA.SH"]["blocked_by"]
    # AAA 七项全过
    assert rows["AAA.SH"]["passed_n"] == 7


def test_gate_detail_sorted_by_passed_then_momentum():
    feats = {
        "LOW.SH": _feat("LOW.SH", above_ma60=False, ma20_slope=-0.01,
                        macd_hist=-0.2, trend_up=False),
        "HIGH.SH": _feat("HIGH.SH"),
        "MID.SH": _feat("MID.SH", ma20_slope=-0.01, trend_up=False),
    }
    moms = {"LOW.SH": 0.30, "HIGH.SH": 0.20, "MID.SH": 0.25}
    obs = Observability(_engine_with(_FakeDaily(feats, moms)))
    codes = [r["code"] for r in obs.gate_detail()["rows"]]
    assert codes[0] == "HIGH.SH", codes          # 通过条件最多
    assert codes[-1] == "LOW.SH", codes          # 通过条件最少


def test_gate_detail_missing_daily_is_safe():
    obs = Observability(SimpleNamespace(daily=None, data_mode="xtdata"))
    out = obs.gate_detail()
    assert out["summary"]["ready"] is False
    assert out["rows"] == []


def test_gate_detail_respects_strategy_params(monkeypatch=None):
    """参数经 engine 命名空间动态读取：改 STRATEGY_PARAMS 会改变动量门槛判定。"""
    import engine.event_engine as EE

    feats = {"X.SH": _feat("X.SH")}
    moms = {"X.SH": 0.10}
    obs = Observability(_engine_with(_FakeDaily(feats, moms)))

    old = EE.STRATEGY_PARAMS.get("momentum_min_mom")
    try:
        EE.STRATEGY_PARAMS["momentum_min_mom"] = 0.05     # 0.10 > 0.05 ⇒ 通过
        assert obs.gate_detail()["rows"][0]["mom_ok"] is True
        EE.STRATEGY_PARAMS["momentum_min_mom"] = 0.15     # 0.10 < 0.15 ⇒ 不通过
        assert obs.gate_detail()["rows"][0]["mom_ok"] is False
    finally:
        if old is None:
            EE.STRATEGY_PARAMS.pop("momentum_min_mom", None)
        else:
            EE.STRATEGY_PARAMS["momentum_min_mom"] = old

# -*- coding: utf-8 -*-
"""板块轮动 / 弱换强（enable_rotation）决策逻辑的单元测试。

直接复用生产方法 ``EventEngine._maybe_rotate`` / ``_hot_codes_set``，用最小桩
隔离决策路径（不启动真实引擎 / 不连 xtdata），覆盖：
  - 满仓 + 热板块候选评分显著高于最弱持仓 → 轮换（卖最弱、买最强）
  - 评分差不足 → 不轮换
  - 候选不在热板块 → 不轮换
  - 组合未满 → 不轮换
  - enable_rotation=False（默认）→ 不轮换
  - _hot_codes_set 取 LLM top ∪ 板块推荐池 top
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from engine.event_engine import EventEngine
from core.data_models import Signal, Position


class _Feat:
    def __init__(self, score=0.0):
        self.score = score
        self.trend_up = False
        self.bias = 0.0
        self.close = 10.0
        self.factors = {}


class _Daily:
    def __init__(self, scores):
        self._scores = scores

    def features(self, code):
        return _Feat(score=self._scores.get(code, 0.0))


class _Strategy:
    def __init__(self, scores):
        self._scores = scores

    def on_daily_features(self, code, name, features):
        sc = self._scores.get(code, 0.0)
        side = "BUY" if sc >= 1.0 else "HOLD"
        return Signal(ts=datetime.now(), code=code, name=name,
                      side=side, score=sc, price=10.0)


class _Rec:
    def __init__(self, code):
        self.code = code


class _Scorer:
    def __init__(self, codes):
        self.recommendations = [_Rec(c) for c in codes]


class _LLM:
    def __init__(self, codes):
        self.ranked_codes = list(codes)


def _make_engine(scores, held, hot_codes, enable=True, cooldown=0.0):
    eng = SimpleNamespace()
    eng.enable_rotation = enable
    eng.rotation_min_score_gap = 2.0
    eng.rotation_cooldown_sec = cooldown
    eng.rotation_hot_top_n = 15
    eng.max_positions = 5
    eng.dynamic_universe = None
    eng._last_rotate_ts = 0.0
    eng._last_rotate_eval_ts = 0.0
    eng.daily = _Daily(scores)
    eng.strategy = _Strategy(scores)
    eng.sector_scorer = _Scorer(hot_codes)
    eng._llm_last_result = _LLM(hot_codes)
    eng._positions = {}
    for c in held:
        eng._positions[c] = Position(code=c, name=c, quantity=100,
                                     avg_cost=10.0, last_price=10.0)
    eng._sells = []
    eng._buys = []
    # 用受控候选宇宙替代真实 STOCK_CODES/INDEX_CODES 全局，隔离决策路径
    eng._apply_momentum_gate = lambda s: set(hot_codes)
    # 把真实方法绑定到桩对象（_maybe_rotate 内部会 self._hot_codes_set()）
    eng._hot_codes_set = lambda: EventEngine._hot_codes_set(eng)
    eng._handle_sell = lambda sig, pos: eng._sells.append(sig.code)
    eng._handle_buy = lambda sig, tick, cp: eng._buys.append((sig.code, sig.score))
    return eng


def _ticks(codes):
    return {c: SimpleNamespace(price=10.0) for c in codes}


def test_rotation_swaps_weakest_for_hot_candidate():
    # 实际 5 仓（09-07 场景）+ 热板块候选 300308.SZ 高分
    held = ["688072.SH", "688120.SH", "002415.SZ", "688082.SH", "301165.SZ"]
    scores = {"688072.SH": 3.0, "688120.SH": 2.0, "002415.SZ": 4.0,
              "688082.SH": 1.0, "301165.SZ": 2.0, "300308.SZ": 9.0}
    eng = _make_engine(scores, held, hot_codes=["300308.SZ"])
    EventEngine._maybe_rotate(eng, _ticks(["300308.SZ"]))
    # 最弱 = 688082.SH(1.0)；候选 300308.SZ(9.0)，差 8.0 ≥ 2.0 → 轮换
    assert eng._sells == ["688082.SH"], eng._sells
    assert eng._buys == [("300308.SZ", 9.0)], eng._buys


def test_rotation_skips_when_gap_too_small():
    held = ["688072.SH", "688120.SH", "002415.SZ", "688082.SH", "301165.SZ"]
    scores = {"688072.SH": 3.0, "688120.SH": 2.0, "002415.SZ": 4.0,
              "688082.SH": 1.0, "301165.SZ": 2.0, "300308.SZ": 2.5}
    eng = _make_engine(scores, held, hot_codes=["300308.SZ"])
    EventEngine._maybe_rotate(eng, _ticks(["300308.SZ"]))
    # 差 2.5-1.0=1.5 < 2.0 → 不轮换
    assert eng._sells == []
    assert eng._buys == []


def test_rotation_skips_when_candidate_not_hot():
    held = ["688072.SH", "688120.SH", "002415.SZ", "688082.SH", "301165.SZ"]
    scores = {"688072.SH": 3.0, "688120.SH": 2.0, "002415.SZ": 4.0,
              "688082.SH": 1.0, "301165.SZ": 2.0, "300308.SZ": 9.0}
    # 候选虽高分，但不在热板块名单 → 不轮换
    eng = _make_engine(scores, held, hot_codes=[])
    EventEngine._maybe_rotate(eng, _ticks(["300308.SZ"]))
    assert eng._sells == []
    assert eng._buys == []


def test_rotation_skips_when_not_full():
    held = ["688072.SH", "688120.SH"]  # 仅 2 仓
    scores = {"688072.SH": 3.0, "688120.SH": 2.0, "300308.SZ": 9.0}
    eng = _make_engine(scores, held, hot_codes=["300308.SZ"])
    EventEngine._maybe_rotate(eng, _ticks(["300308.SZ"]))
    assert eng._sells == []
    assert eng._buys == []


def test_rotation_disabled_by_default():
    held = ["688072.SH", "688120.SH", "002415.SZ", "688082.SH", "301165.SZ"]
    scores = {"688072.SH": 3.0, "688120.SH": 2.0, "002415.SZ": 4.0,
              "688082.SH": 1.0, "301165.SZ": 2.0, "300308.SZ": 9.0}
    eng = _make_engine(scores, held, hot_codes=["300308.SZ"], enable=False)
    EventEngine._maybe_rotate(eng, _ticks(["300308.SZ"]))
    assert eng._sells == []
    assert eng._buys == []


def test_hot_codes_set_union_of_llm_and_sector():
    eng = _make_engine({}, [], hot_codes=["300308.SZ", "300502.SZ"])
    eng._llm_last_result = _LLM(["300394.SZ"])
    eng.sector_scorer = _Scorer(["300308.SZ", "300502.SZ"])
    hot = EventEngine._hot_codes_set(eng)
    assert hot == {"300394.SZ", "300308.SZ", "300502.SZ"}, hot


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

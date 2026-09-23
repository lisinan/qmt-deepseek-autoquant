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
from config.settings import STRATEGY_PARAMS
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


def _make_engine(scores, held, hot_codes, enable=True, cooldown=0.0,
                 sell_ok=True):
    eng = SimpleNamespace()
    eng.enable_rotation = enable
    eng.rotation_min_score_gap = 2.0
    eng.rotation_cooldown_sec = cooldown
    eng.rotation_hot_top_n = 15
    eng.rotation_intraday_breakout_pct = 1.5
    eng.rotation_min_score_gap_hot = 0.0
    eng.rotation_max_swaps_per_eval = 3
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
    # 【2026-09-23 AM-EVOLVE】同样绑定日线闸门判定（_maybe_rotate 内部会调用）。
    # 生产当前 rotation_require_daily_gate=False ⇒ 恒返回 True、保持既有行为，
    # 故下列既有用例的语义不受影响（保原意，与生产默认值解耦）。
    eng._rotation_daily_gate_ok = (
        lambda feat: EventEngine._rotation_daily_gate_ok(eng, feat))
    # 【2026-09-23 PM-EVOLVE】_handle_sell 现在返回 bool（True=仓位真的清掉）。
    # 桩须跟上新契约：sell_ok=True 模拟正常卖出（清仓并释放槽位）；
    # sell_ok=False 模拟 T+1 / 建仓保护期拦截（槽位未释放）。
    def _fake_sell(sig, pos):
        if not sell_ok:
            return False
        eng._sells.append(sig.code)
        if sig.code in eng._positions:
            eng._positions[sig.code].quantity = 0
        return True
    eng._handle_sell = _fake_sell
    eng._handle_buy = lambda sig, tick, cp: eng._buys.append((sig.code, sig.score))
    # 【2026-09-22】轮动换入/换出也要落库（此前不落库 → 引擎有成交而 signals
    # 表 0 行，页面「实时信号」只剩几天前的旧信号）。桩需跟上新契约。
    eng._saved_signals = []
    eng._save_signal = lambda sig: eng._saved_signals.append(sig)
    return eng


def _ticks(codes, chg=None):
    chg = chg or {}
    return {c: SimpleNamespace(price=10.0, change_pct=chg.get(c, 0.0))
            for c in codes}


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
    # 2026-09-22：换出与换入都要写进信号表
    sides = sorted(s.side for s in eng._saved_signals)
    assert sides == ["BUY", "SELL"], sides


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


def test_rotation_hot_bypasses_momentum_gate():
    # 动量闸门把候选池过滤成空集（模拟 60 日动量前 N 不含光模块），
    # 但候选仍在 AI 热板块名单 → 应绕过动量闸门、仍发生轮换。
    held = ["688072.SH", "688120.SH", "002415.SZ", "688082.SH", "301165.SZ"]
    scores = {"688072.SH": 3.0, "688120.SH": 2.0, "002415.SZ": 4.0,
              "688082.SH": 1.0, "301165.SZ": 2.0, "300308.SZ": 9.0}
    eng = _make_engine(scores, held, hot_codes=["300308.SZ"])
    eng._apply_momentum_gate = lambda s: set()  # 闸门排除一切候选
    EventEngine._maybe_rotate(eng, _ticks(["300308.SZ"]))
    assert eng._sells == ["688082.SH"], eng._sells
    assert eng._buys == [("300308.SZ", 9.0)], eng._buys


def test_rotation_breakout_relaxes_gap():
    # 候选在热板块 + 日内突破(change_pct≥阈值)，但日线评分差<rotation_min_score_gap；
    # 突破应放宽门槛（rotation_min_score_gap_hot=0）仍触发轮换。
    held = ["688072.SH", "688120.SH", "002415.SZ", "688082.SH", "301165.SZ"]
    scores = {"688072.SH": 3.0, "688120.SH": 2.0, "002415.SZ": 4.0,
              "688082.SH": 1.0, "301165.SZ": 2.0, "300308.SZ": 2.5}
    eng = _make_engine(scores, held, hot_codes=["300308.SZ"])
    # 若无突破：2.5-1.0=1.5 < 2.0 → 不换；有突破(3%)→换
    EventEngine._maybe_rotate(
        eng, _ticks(["300308.SZ"], {"300308.SZ": 3.0}))
    assert eng._sells == ["688082.SH"], eng._sells
    assert eng._buys == [("300308.SZ", 2.5)], eng._buys


def test_rotation_batch_multiple_swaps():
    # 多只热板块候选日内突破 → 单次评估批量换出多只最弱老仓（最多 max_swaps）。
    held = ["688072.SH", "688120.SH", "002415.SZ", "688082.SH", "301165.SZ"]
    scores = {"688072.SH": 3.0, "688120.SH": 2.0, "002415.SZ": 4.0,
              "688082.SH": 1.0, "301165.SZ": 2.0,
              "300308.SZ": 9.0, "300502.SZ": 8.0, "300394.SZ": 7.0}
    eng = _make_engine(
        scores, held,
        hot_codes=["300308.SZ", "300502.SZ", "300394.SZ"])
    EventEngine._maybe_rotate(eng, _ticks(
        ["300308.SZ", "300502.SZ", "300394.SZ"],
        {"300308.SZ": 3.0, "300502.SZ": 3.0, "300394.SZ": 3.0}))
    bought = {c for c, _ in eng._buys}
    assert len(eng._buys) == 3, eng._buys
    assert bought == {"300308.SZ", "300502.SZ", "300394.SZ"}, bought
    assert len(eng._sells) == 3, eng._sells


def test_rotation_4of5_fills_empty_slot():
    # 4/5（差一仓）+ 热板块候选 300308.SZ 日内突破 → 补强空槽
    # （纯买入、不卖 existing；组合回到 5/5 且第 5 仓是确认热度标的）
    held = ["688072.SH", "688120.SH", "002415.SZ", "688082.SH"]  # 4 仓
    scores = {"688072.SH": 3.0, "688120.SH": 2.0, "002415.SZ": 4.0,
              "688082.SH": 1.0, "300308.SZ": 9.0}
    eng = _make_engine(scores, held, hot_codes=["300308.SZ"])
    EventEngine._maybe_rotate(
        eng, _ticks(["300308.SZ"], {"300308.SZ": 3.0}))
    # 不卖现有持仓；买入 300308.SZ 补空槽
    assert eng._sells == [], eng._sells
    assert eng._buys == [("300308.SZ", 9.0)], eng._buys


def test_rotation_4of5_no_candidate_skips():
    # 4/5 但无任何候选 tick（行情缺失）→ 不补、不卖
    held = ["688072.SH", "688120.SH", "002415.SZ", "688082.SH"]
    scores = {"688072.SH": 3.0, "688120.SH": 2.0, "002415.SZ": 4.0,
              "688082.SH": 1.0, "300308.SZ": 9.0}
    eng = _make_engine(scores, held, hot_codes=["300308.SZ"])
    EventEngine._maybe_rotate(eng, _ticks([]))
    assert eng._sells == []
    assert eng._buys == []


def test_rotation_4of5_knob_off_skips():
    # rotation_fill_empty_slot=False → 4/5 不补强，回滚到仅满仓才轮换的旧行为
    held = ["688072.SH", "688120.SH", "002415.SZ", "688082.SH"]
    scores = {"688072.SH": 3.0, "688120.SH": 2.0, "002415.SZ": 4.0,
              "688082.SH": 1.0, "300308.SZ": 9.0}
    eng = _make_engine(scores, held, hot_codes=["300308.SZ"])
    eng.rotation_fill_empty_slot = False
    EventEngine._maybe_rotate(
        eng, _ticks(["300308.SZ"], {"300308.SZ": 3.0}))
    assert eng._sells == []
    assert eng._buys == []


def test_rotation_4of5_weak_hot_no_breakout_skips():
    # 候选在热板块但日线 HOLD 且非日内突破 → 不补（避免换入弱热名）
    held = ["688072.SH", "688120.SH", "002415.SZ", "688082.SH"]
    scores = {"688072.SH": 3.0, "688120.SH": 2.0, "002415.SZ": 4.0,
              "688082.SH": 1.0, "300308.SZ": 0.5}  # 候选热但日线 HOLD
    eng = _make_engine(scores, held, hot_codes=["300308.SZ"])
    EventEngine._maybe_rotate(eng, _ticks(["300308.SZ"]))  # change_pct 默认 0
    assert eng._sells == []
    assert eng._buys == []


def test_rotation_daily_gate_default_off_preserves_breakout_bypass():
    """守卫：生产默认 rotation_require_daily_gate=False ⇒ 日内突破仍可绕过日线
    闸门（既有行为不变）。

    背景：2026-09-23 AM-EVOLVE 定位到轮动绕过 min_daily_bias 闸门的口径背离并
    实现了修复，但**回测未达晋升闸门 ②**（4 窗口均值 dSh +0.087 < +0.10）⇒
    按纪律不启用。本测试锁死「默认不启用」这一决定；一旦有人把默认值改成
    True，或把 False 语义改坏，这里立即失败。
    """
    held = ["688072.SH", "688120.SH", "002415.SZ", "688082.SH"]
    scores = {"688072.SH": 3.0, "688120.SH": 2.0, "002415.SZ": 4.0,
              "688082.SH": 1.0, "300308.SZ": 9.0}
    eng = _make_engine(scores, held, hot_codes=["300308.SZ"])
    # 桩特征：trend_up=False、bias=0.0 < min_daily_bias(2.0) ⇒ 闸门本应拒绝
    assert EventEngine._rotation_daily_gate_ok(eng, _Feat(score=9.0)) is True
    EventEngine._maybe_rotate(
        eng, _ticks(["300308.SZ"], {"300308.SZ": 3.0}))
    assert eng._buys == [("300308.SZ", 9.0)], eng._buys


def test_rotation_daily_gate_enabled_blocks_weak_candidate():
    """守卫：置 rotation_require_daily_gate=True ⇒ 轮动必须与主信号路径同闸门，
    trend_up=False 且 bias < min_daily_bias 的候选被拒绝；trend_up=True 仍放行。
    """
    held = ["688072.SH", "688120.SH", "002415.SZ", "688082.SH"]
    scores = {"688072.SH": 3.0, "688120.SH": 2.0, "002415.SZ": 4.0,
              "688082.SH": 1.0, "300308.SZ": 9.0}
    old = STRATEGY_PARAMS.get("rotation_require_daily_gate")
    try:
        STRATEGY_PARAMS["rotation_require_daily_gate"] = True
        eng = _make_engine(scores, held, hot_codes=["300308.SZ"])
        weak = _Feat(score=9.0)          # trend_up=False, bias=0.0
        strong = _Feat(score=9.0)
        strong.trend_up = True
        assert EventEngine._rotation_daily_gate_ok(eng, weak) is False
        assert EventEngine._rotation_daily_gate_ok(eng, strong) is True
        # 闸门生效 → 弱势候选即使日内突破 3% 也不补空槽
        EventEngine._maybe_rotate(
            eng, _ticks(["300308.SZ"], {"300308.SZ": 3.0}))
        assert eng._buys == [], eng._buys
    finally:
        if old is None:
            STRATEGY_PARAMS.pop("rotation_require_daily_gate", None)
        else:
            STRATEGY_PARAMS["rotation_require_daily_gate"] = old


def test_rotation_no_overshoot_when_sell_blocked_by_t1():
    """守卫：轮动换出被 T+1 拦截（_handle_sell 返回 False）时**不得**换入。

    背景：2026-09-23 PM-EVOLVE 定位的 P0 缺陷——原实现无条件「先卖最弱 → 再买
    候选」，而 T+1 会锁死当日建仓的卖出，于是净持仓 +1、突破 max_positions。
    实盘铁证：10:31:06 先打印「[T+1 拦截] 603986 跳过」，同一时刻仍 BUY 688012。
    回测器 max_positions 严格夹紧 ⇒ 超仓是「从未被回测验证的变体」
    （OOS 网格 6 −0.087 / 7 −0.097 / 8 −0.086）。
    """
    held = ["688072.SH", "688120.SH", "002415.SZ", "688082.SH", "300308.SZ"]
    scores = {"688072.SH": 3.0, "688120.SH": 2.0, "002415.SZ": 4.0,
              "688082.SH": 1.0, "300308.SZ": 0.5, "688111.SH": 9.0}
    eng = _make_engine(scores, held, hot_codes=["688111.SH"], sell_ok=False)
    EventEngine._maybe_rotate(eng, _ticks(["688111.SH"], {"688111.SH": 3.0}))
    # 卖出被拦截 ⇒ 放弃换入，持仓不增加（也不会突破 max_positions=5）
    assert eng._buys == [], eng._buys
    assert len([p for p in eng._positions.values() if p.quantity > 0]) == 5


def test_rotation_converges_when_already_over_max_positions():
    """守卫：持仓已超 max_positions 时，弱换强只卖不买，使持仓收敛回上限。

    针对历史遗留超仓（equity_snapshots 09-22 EOD 8 仓 / 09-23 EOD 6 仓，
    均 > max_positions=5）的收敛路径。
    """
    held = ["688072.SH", "688120.SH", "002415.SZ", "688082.SH",
            "300308.SZ", "688111.SH"]        # 6 仓，已超上限 5
    scores = {"688072.SH": 3.0, "688120.SH": 2.0, "002415.SZ": 4.0,
              "688082.SH": 1.0, "300308.SZ": 1.5, "688111.SH": 1.2,
              "688981.SH": 9.0}
    eng = _make_engine(scores, held, hot_codes=["688981.SH"])
    EventEngine._maybe_rotate(eng, _ticks(["688981.SH"], {"688981.SH": 3.0}))
    # 只卖不买 ⇒ 持仓由 6 收敛到 5，且不产生新买入
    assert eng._buys == [], eng._buys
    assert len([p for p in eng._positions.values() if p.quantity > 0]) == 5
    assert len(eng._sells) == 1, eng._sells


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

# -*- coding: utf-8 -*-
"""观察篮（manual entry）机制单元测试【2026-09-18】。

背景：全宇宙 trend_up=False 时日线闸门整体关闭，paper 账户 100% 现金、零成交，
自进化闭环失去度量对象。观察篮用于把「LLM 排序前五」这类尚未纳入策略的候选
真正建仓观察（paper），但**不绕过**风控与现金夹紧。

直接复用生产方法 ``EventEngine._manual_entry_step`` / ``_is_manual_exempt``，
用最小桩隔离（不启动真实引擎 / 不连 xtdata），覆盖：
  - 清单内未持仓代码 → 建仓（绕过动量/日线闸门与评分阈值）
  - 已持仓代码 → 不重复买入
  - 当日已卖出的观察篮代码 → 当日不再自动补回（防买入/平仓空转）
  - 清单为空 → 完全不介入
  - 豁免判定：清单内 + 开关开 → True；开关关 / 非观察篮 → False
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

import config.settings as settings
from engine.event_engine import EventEngine
from core.data_models import Position, Signal

CODES = ["300502.SZ", "300308.SZ", "002415.SZ"]


def _make_engine(held, bought=None):
    bought = bought if bought is not None else []
    eng = SimpleNamespace()
    eng._positions = {}
    for c in held:
        eng._positions[c] = Position(code=c, name=c, quantity=100,
                                     avg_cost=10.0, last_price=10.0)
    eng._manual_positions = set(held)
    eng._manual_sold_today = set()
    eng._manual_sold_date = datetime.now().strftime("%Y-%m-%d")
    eng._buys = bought
    # 记录买入，并把代码放进持仓（模拟 _handle_buy 成功建仓）
    def _buy(sig, tick, cp):
        eng._buys.append(sig.code)
        eng._positions[sig.code] = Position(
            code=sig.code, name=sig.name, quantity=100,
            avg_cost=sig.price, last_price=sig.price)
    eng._handle_buy = _buy
    # 【2026-09-22】观察篮买入也要落库（此前只有策略路径落库，导致引擎有成交
    # 而 signals 表连日 0 行，页面「实时信号」只剩几天前的旧信号）。
    eng._saved_signals = []

    def _save_signal(sig):
        eng._saved_signals.append(sig)
    eng._save_signal = _save_signal
    return eng


def _ticks(codes):
    return {c: SimpleNamespace(price=10.0) for c in codes}


@contextmanager
def _with_codes(codes, exempt=True):
    """临时改写观察篮配置，用完恢复，避免污染其它用例。"""
    old_codes = settings.STRATEGY_PARAMS.get("manual_entry_codes")
    old_ex = settings.STRATEGY_PARAMS.get("manual_entry_exit_exempt")
    settings.STRATEGY_PARAMS["manual_entry_codes"] = list(codes)
    settings.STRATEGY_PARAMS["manual_entry_exit_exempt"] = exempt
    try:
        yield
    finally:
        settings.STRATEGY_PARAMS["manual_entry_codes"] = old_codes
        settings.STRATEGY_PARAMS["manual_entry_exit_exempt"] = old_ex


def test_manual_entry_buys_unheld_codes():
    eng = _make_engine(held=[])
    with _with_codes(CODES):
        EventEngine._manual_entry_step(eng, _ticks(CODES), {})
    assert sorted(eng._buys) == sorted(CODES), eng._buys
    assert eng._manual_positions == set(CODES)
    # 2026-09-22：买入决策必须写入信号表，否则页面「实时信号」看不到真实成交
    assert sorted(s.code for s in eng._saved_signals) == sorted(CODES)
    assert all(s.side == "BUY" for s in eng._saved_signals)


def test_manual_entry_skips_already_held():
    eng = _make_engine(held=["300502.SZ"])
    with _with_codes(CODES):
        EventEngine._manual_entry_step(eng, _ticks(CODES), {})
    # 已持有的不重复买，只补其余两只
    assert sorted(eng._buys) == ["002415.SZ", "300308.SZ"], eng._buys


def test_manual_entry_does_not_rebuy_sold_today():
    """当日被卖出的观察篮标的不自动补回 —— 否则「日内强平 → 立即补仓」会空转。"""
    eng = _make_engine(held=[])
    # 上一轮观察篮持有 300502.SZ，本轮已不在持仓 → 视为当日已卖
    eng._manual_positions = {"300502.SZ"}
    with _with_codes(CODES):
        EventEngine._manual_entry_step(eng, _ticks(CODES), {})
    assert "300502.SZ" not in eng._buys, eng._buys
    assert eng._manual_sold_today == {"300502.SZ"}
    assert sorted(eng._buys) == ["002415.SZ", "300308.SZ"], eng._buys


def test_manual_entry_noop_when_list_empty():
    eng = _make_engine(held=[])
    with _with_codes([]):
        EventEngine._manual_entry_step(eng, _ticks(CODES), {})
    assert eng._buys == []


def test_manual_entry_skips_code_without_tick():
    eng = _make_engine(held=[])
    with _with_codes(CODES):
        # 只给其中一只行情，其余无 tick 应跳过（不让 _handle_buy 拿到 0 价）
        EventEngine._manual_entry_step(eng, _ticks(["300502.SZ"]), {})
    assert eng._buys == ["300502.SZ"], eng._buys


def test_is_manual_exempt_matrix():
    eng = _make_engine(held=["300502.SZ"])
    with _with_codes(CODES, exempt=True):
        assert EventEngine._is_manual_exempt(eng, "300502.SZ") is True
        assert EventEngine._is_manual_exempt(eng, "600000.SH") is False
    with _with_codes(CODES, exempt=False):
        # 关掉豁免开关 → 退出逻辑回到已验证的原行为
        assert EventEngine._is_manual_exempt(eng, "300502.SZ") is False

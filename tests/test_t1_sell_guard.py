# -*- coding: utf-8 -*-
"""A 股 T+1 卖出闸门单元测试（2026-09-14 修复回归）。

背景：原 live/paper 引擎允许「当日买入、当日卖出」，会生成实盘根本无法成交的
同日 round-trip。真实案例：2026-09-08 300394.SZ 于 10:21:14 买入、10:21:17 即
被「趋势破位」卖出（-0.04%）——A 股 T+1 下当日买入当日不可卖，该笔不可执行。

本测试覆盖：
  - ``is_t1_locked`` 纯函数（None / 当日 / 上一交易日 三态）；
  - ``_handle_sell`` 对当日买入的仓位单点拦截（quantity 不被清零）；
  - 上一交易日买入的老仓仍可被正常卖出（不误杀）；
  - 复现 300394 同日 round-trip 场景：买入即被破位退出 → 现被 T+1 拦截。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, time as dtime
from types import SimpleNamespace

from engine.event_engine import EventEngine, is_t1_locked, is_in_entry_protection
from core.market_calendar import is_trading_day
from core.data_models import Position, Signal


def _first_sellable(open_date: datetime) -> date:
    """复刻 ``is_in_entry_protection`` 的首个可卖交易日推导（use_calendar=False
    以走周末启发式，保证测试与无 xtdata 沙箱行为一致、确定性）。"""
    for off in range(1, 9):
        d = open_date.date() + timedelta(days=off)
        if is_trading_day(d, use_calendar=False):
            return d
    raise AssertionError("测试日期推导失败：8 日内无非周末日")


def _make_protect_engine(protect_minutes: int = 30) -> SimpleNamespace:
    """最小引擎桩：开启保护期，保留真实 ``_handle_sell``。"""
    eng = _make_sell_engine(t1=True)
    eng.entry_protect_minutes = protect_minutes
    return eng


def _make_sell_engine(t1: bool = True) -> SimpleNamespace:
    """最小引擎桩：保留真实 ``_handle_sell``，mock 其副作用依赖。"""
    eng = SimpleNamespace()
    eng.t1_restriction = t1
    eng.entry_protect_minutes = 0    # 默认关保护期，T+1 单测聚焦 T+1 本身
    eng.exec_mode = "paper"          # 走 paper 卖出分支
    eng._cash = 1_000_000.0
    eng._daily_trade_count = 0
    eng._total_asset = lambda: 1_000_000.0
    # paper 分支副作用：on_fill / save_order / save_fill 全部置为无操作
    eng.risk = SimpleNamespace(on_fill=lambda *a, **k: None)
    eng.storage = SimpleNamespace(
        save_order=lambda *a, **k: None,
        save_fill=lambda *a, **k: None,
    )
    return eng


def test_is_t1_locked_none_is_sellable():
    # 历史账本缺字段 / 测试桩 → 不锁定（保守，不误杀）
    assert is_t1_locked(None, date.today()) is False


def test_is_t1_locked_today_is_locked():
    assert is_t1_locked(datetime.now(), date.today()) is True


def test_is_t1_locked_previous_day_is_sellable():
    assert is_t1_locked(datetime.now() - timedelta(days=1), date.today()) is False


def test_handle_sell_blocks_today_bought_position():
    # 当日买入的仓位：破位退出尝试应被 T+1 拦截，quantity 保持不变。
    eng = _make_sell_engine(t1=True)
    pos = Position(code="300394.SZ", name="天孚通信", quantity=300,
                   avg_cost=271.12, last_price=271.0,
                   open_date=datetime.now())  # 买入于今日
    sig = Signal(ts=datetime.now(), code="300394.SZ", side="SELL",
                 price=271.0, reason="趋势破位离场")
    EventEngine._handle_sell(eng, sig, pos)
    assert pos.quantity == 300, "T+1 下当日买入不应被卖出（quantity 应维持 300）"


def test_handle_sell_allows_previous_day_position():
    # 上一交易日买入的老仓：仍可正常卖出（不误杀）。
    eng = _make_sell_engine(t1=True)
    pos = Position(code="688120.SH", name="杭可科技", quantity=200,
                   avg_cost=256.68, last_price=245.09,
                   open_date=datetime.now() - timedelta(days=7))  # 09-01 建仓
    sig = Signal(ts=datetime.now(), code="688120.SH", side="SELL",
                 price=245.09, reason="板块轮动换出")
    EventEngine._handle_sell(eng, sig, pos)
    assert pos.quantity == 0, "上一交易日买入的老仓应可正常卖出（quantity 清零）"


def test_handle_sell_t1_off_allows_same_day():
    # 非 A 股（T+1 关闭）时，当日买入也可卖——验证开关可控、不硬锁死。
    eng = _make_sell_engine(t1=False)
    pos = Position(code="300394.SZ", quantity=300, avg_cost=271.12,
                   last_price=271.0, open_date=datetime.now())
    sig = Signal(ts=datetime.now(), code="300394.SZ", side="SELL",
                 price=271.0, reason="趋势破位离场")
    EventEngine._handle_sell(eng, sig, pos)
    assert pos.quantity == 0, "T1_RESTRICTION=False 时当日买入应可卖出"


def test_300394_same_day_roundtrip_blocked():
    """复现并锁定 2026-09-08 300394 同日 round-trip 缺陷的修复结果。

    轮动于 10:21:14 买入 300394（open_date=今日），3 秒后日内破位退出试图卖出——
    修复前会生成不可成交的同日卖单；修复后 T+1 闸门拦截，仓位保留到次日。
    """
    eng = _make_sell_engine(t1=True)
    # 轮动买入的候选（当日建仓、T+1 锁定）
    new_pos = Position(code="300394.SZ", name="天孚通信", quantity=300,
                       avg_cost=271.12, last_price=271.0,
                       open_date=datetime.now())
    break_sig = Signal(ts=datetime.now(), code="300394.SZ", side="SELL",
                       price=271.0, reason="趋势破位离场 -0.04%")
    # 这就是原 bug 的卖出入口（日内破位退出 → _handle_sell）
    EventEngine._handle_sell(eng, break_sig, new_pos)
    # 断言：未被卖出（锁定至下一交易日），与实盘可执行性一致
    assert new_pos.quantity == 300
    assert is_t1_locked(new_pos.open_date, date.today()) is True


# ============================================================
# 建仓保护期（叠加于 T+1 之上，2026-09-14 新增回归）
# ============================================================
# 防护对象：轮动换入候选在「首个可卖交易日」开盘即面临分钟级破位/止损退出，
# 早盘正常波动可能把它「换入即误伤」扫出。保护期把不可卖窗口延伸到首个可卖
# 交易日 09:30 起 ENTRY_PROTECT_MINUTES 分钟，期间任何经 _handle_sell 的退出
# 均被单点拦截。
def test_is_in_entry_protection_guards():
    # None 持仓日 / 关闭（N<=0）→ 不锁定，保守不误杀
    assert is_in_entry_protection(None, 30) is False
    assert is_in_entry_protection(datetime(2026, 9, 7, 10, 0), 0) is False


def test_is_in_entry_protection_window_boundaries():
    # open_date=周一 2026-09-07 10:00 → 首个可卖日=周二 2026-09-08
    open_date = datetime(2026, 9, 7, 10, 0)
    fs = _first_sellable(open_date)
    assert fs == date(2026, 9, 8)
    session_open = datetime.combine(fs, dtime(9, 30))
    protect_until = session_open + timedelta(minutes=30)  # 10:00
    # 窗口左闭右开 [09:30, 10:00)
    assert is_in_entry_protection(open_date, 30, session_open) is True
    assert is_in_entry_protection(open_date, 30,
                                  protect_until - timedelta(seconds=1)) is True
    # 严格小于：恰好等于 protect_until 已不在保护期
    assert is_in_entry_protection(open_date, 30, protect_until) is False
    # 窗口外（10:00 之后）放行
    assert is_in_entry_protection(open_date, 30,
                                  protect_until + timedelta(minutes=5)) is False


def test_handle_sell_blocks_during_entry_protection():
    # 保护窗内（首个可卖日 09:45）：破位退出尝试应被拦截，quantity 不变。
    eng = _make_protect_engine(protect_minutes=30)
    open_date = datetime(2026, 9, 7, 10, 0)          # 首个可卖日 2026-09-08
    fs = _first_sellable(open_date)
    now_in_window = datetime.combine(fs, dtime(9, 45))  # 09:45 < 10:00
    pos = Position(code="300502.SZ", name="新易盛", quantity=200,
                   avg_cost=280.0, last_price=279.5,
                   open_date=open_date)
    sig = Signal(ts=now_in_window, code="300502.SZ", side="SELL",
                 price=279.5, reason="趋势破位离场 -0.18%")
    EventEngine._handle_sell(eng, sig, pos, now=now_in_window)
    assert pos.quantity == 200, "保护窗内不应退出（quantity 应维持 200）"


def test_handle_sell_allows_after_entry_protection():
    # 保护窗外（首个可卖日 10:30）：正常退出放行，quantity 清零。
    # 同时验证 T+1 不误杀：open_date=09-07 已非今日，T+1 放行。
    eng = _make_protect_engine(protect_minutes=30)
    open_date = datetime(2026, 9, 7, 10, 0)
    fs = _first_sellable(open_date)
    now_after = datetime.combine(fs, dtime(10, 30))  # 10:30 > 10:00
    pos = Position(code="300502.SZ", name="新易盛", quantity=200,
                   avg_cost=280.0, last_price=275.0,
                   open_date=open_date)
    sig = Signal(ts=now_after, code="300502.SZ", side="SELL",
                 price=275.0, reason="板块轮动换出")
    EventEngine._handle_sell(eng, sig, pos, now=now_after)
    assert pos.quantity == 0, "保护窗外应可正常退出（quantity 清零）"


def test_handle_sell_protection_disabled_when_zero():
    # entry_protect_minutes<=0 时，即便在早盘窗口内也放行（开关可控）。
    eng = _make_protect_engine(protect_minutes=0)
    open_date = datetime(2026, 9, 7, 10, 0)
    fs = _first_sellable(open_date)
    now_in_window = datetime.combine(fs, dtime(9, 45))
    pos = Position(code="300502.SZ", quantity=200, avg_cost=280.0,
                   last_price=279.5, open_date=open_date)
    sig = Signal(ts=now_in_window, code="300502.SZ", side="SELL",
                 price=279.5, reason="趋势破位离场")
    EventEngine._handle_sell(eng, sig, pos, now=now_in_window)
    assert pos.quantity == 0, "保护期关闭时应可卖出"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

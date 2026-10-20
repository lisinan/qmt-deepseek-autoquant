# -*- coding: utf-8 -*-
"""【2026-10-14 AM-EVOLVE 第 30 轮】日内已实现盈亏「跨日复位」守卫（参数零改动）。

★ 缺陷（今日实盘铁证）
    ``storage/qmt.db`` 的 ``engine_state`` 实测（2026-10-14 11:29:42）：

        trade_date = 2026-10-14    daily_pnl = -7964.0    当日 fills = 0 笔

    即：今日**一笔都没有成交**，日内已实现盈亏却是 **-7,964 元**——
    来源是 2026-10-09 唯一一笔 SELL 300394.SZ（-7,964）。

★ 根因链（三环）
    1. ``RiskManager.reset_daily()`` 的清零条件是 ``today != self._today``，而
       ``self._today`` 在 ``__init__`` 里就等于今天 ⇒ **只有进程内跨日**才清零；
    2. 引擎**每天早晨重启**（今日日志 08:05 / 08:09 / 08:13 / 08:14 共四次）
       ⇒ 实际走的是 ``_restore_engine_state()`` 恢复路径，该方法
       ``self.risk._daily_pnl = float(row.get("daily_pnl") or 0.0)`` 原样读回旧值；
    3. ``_reset_daily_if_needed()`` 复位了 ``_daily_trade_count`` /
       ``_day_open_asset`` / ``_daily_flatten_done``，**唯独漏了
       ``risk._daily_pnl``** ⇒ 该计数器永不复位。

★ 影响（量化）
    生效阈值 ``_daily_loss_limit()`` = max(5000, 3% × 972,533.6) = **29,176** 元
    ⇒ -7,964 已占 **27.3%**。更严重的是：引擎每日重启 ⇒ 进程内跨日清零分支
    永不执行 ⇒ 每个亏损日的已实现亏损都**累加**进这个基线，日内亏损预算
    **单调缩水**，最终在无任何当日亏损的情况下触发 ``daily_loss`` 熔断。

★ 口径背离（本项目第 14 例同型）
    回测 ``strategy/backtest_daily.py`` 每根 bar 结束执行
    ``_dl_day_pnl = 0.0   # 日切归零（实盘 reset_daily）``
    ⇒ 回测恒从 0 起算、实盘从历史残留值起算 ⇒ 实盘比回测更容易触发日内熔断
    （与 R14 合成行情 / R20 陈旧报价 / R22 peak_equity 脏值同型：
      「实盘更严、回测更松」的隐性背离）。

★ 修复
    ``engine/event_engine.py::_reset_daily_if_needed`` 的跨日分支新增
    ``self.risk._daily_pnl = 0.0``。**参数零改动**（config/settings.py diff 空）。
"""
from __future__ import annotations

import inspect
import re
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.event_engine import EventEngine  # noqa: E402
from risk.manager import RiskManager  # noqa: E402
import strategy.backtest_daily as BD  # noqa: E402


def _engine(trade_date: date, daily_pnl: float) -> EventEngine:
    """构造一个**只带本用例所需字段**的引擎实例（绕开 __init__ 的重型装配）。"""
    eng = object.__new__(EventEngine)
    eng._trade_date = trade_date
    eng._daily_trade_count = 7
    eng._day_open_asset = 123456.0
    eng._daily_flatten_done = True
    eng.risk = RiskManager()
    eng.risk._daily_pnl = daily_pnl
    return eng


# ---------------------------------------------------------------- 1) 跨日清零

def test_cross_day_reset_clears_daily_pnl():
    """跨交易日（恢复路径）必须把继承来的日内已实现盈亏清零。

    这是缺陷本体：10-09 的 -7,964 被 10-14 的引擎原样继承，而当日零成交。
    """
    eng = _engine(trade_date=date.today() - timedelta(days=1),
                  daily_pnl=-7964.0)
    eng._reset_daily_if_needed()
    assert eng.risk._daily_pnl == 0.0, (
        f"跨日未清零日内已实现盈亏：{eng.risk._daily_pnl}（应为 0.0）。"
        "这正是 2026-10-14 实盘 engine_state daily_pnl=-7964 而当日零成交的根因")
    assert eng._daily_trade_count == 0
    assert eng._trade_date == date.today()


def test_cross_day_reset_zeroes_stale_budget_consumption():
    """清零后，日内亏损预算必须恢复到完整的 max(abs, pct×权益)。

    未修复时 -7,964 占用 29,176 预算的 27.3%，当日可用额度被凭空扣掉。
    """
    eng = _engine(trade_date=date.today() - timedelta(days=1),
                  daily_pnl=-7964.0)
    total_asset = 972533.6
    limit_before = eng.risk._daily_loss_limit(total_asset)
    eng._reset_daily_if_needed()
    available = limit_before + eng.risk._daily_pnl  # _daily_pnl 已为 0
    assert available == limit_before, (
        f"修复后当日可用亏损预算应等于完整阈值 {limit_before}，实得 {available}")


# ------------------------------------------------- 2) 同日重启**不得**清零

def test_same_day_restart_preserves_daily_pnl():
    """同一交易日内重启（盘中重启）**不得**抹掉当日已实现盈亏。

    防止「过度修复」：清零只允许发生在真正的日切上，否则盘中重启会反复
    重置熔断累计器，使日内亏损熔断彻底失效（等价于把保护关掉）。
    """
    eng = _engine(trade_date=date.today(), daily_pnl=-7964.0)
    eng._reset_daily_if_needed()
    assert eng.risk._daily_pnl == -7964.0, (
        f"同日重启不得清零日内已实现盈亏，实得 {eng.risk._daily_pnl}")


# ------------------------------------------------- 3) 语义与 RiskManager 对齐

def test_risk_manager_reset_daily_is_the_reference_semantics():
    """``RiskManager.reset_daily`` 是「日内」语义的参照实现：跨日必清零。

    本用例把参照语义钉死，防止日后有人改动 reset_daily 而让两条路径再次分叉。
    """
    rm = RiskManager()
    rm._today = date.today() - timedelta(days=1)
    rm._daily_pnl = -7964.0
    rm.reset_daily()
    assert rm._daily_pnl == 0.0, "RiskManager.reset_daily 跨日必须清零 _daily_pnl"


# ------------------------------------------------- 4) 回测侧同口径（第 14 例）

def test_backtest_side_resets_daily_pnl_each_bar():
    """回测侧必须按日切归零，否则又是一次「回测更松、实盘更严」的背离。

    ★【2026-10-19 加固】改用磁盘直读（``tests.source_guard.module_source``）而非
    ``inspect.getsource`` —— 后者依赖 linecache/`.pyc`，`.pyc` 陈旧时会返回错位
    内容，造成幽灵失败（实盘铁证见 tests/source_guard.py 顶部说明）。
    """
    from tests.source_guard import module_source
    src = module_source("strategy/backtest_daily.py")
    assert src is not None, "backtest_daily.py 未找到"
    assert "_dl_day_pnl = 0.0" in src, (
        "回测侧缺少日切归零赋值 `_dl_day_pnl = 0.0`；"
        "实盘若不同步清零，日内亏损熔断在两侧口径就不一致")
    assert "日切归零" in src, "回测侧日切归零处缺少口径注释，两侧对齐关系不可追溯"


# ------------------------------------------------- 5) 恢复路径必须调用复位

def test_restore_path_invokes_cross_day_reset():
    """``_restore_engine_state`` 必须仍在调用 ``_reset_daily_if_needed``。

    结构性守卫：修复依赖「恢复路径会调用复位」这一前提。若日后重构把该调用
    摘掉，则 ``risk._daily_pnl = row["daily_pnl"]`` 会再次直接生效，
    缺陷静默回归（R22 peak_equity 同型教训）。

    ★【2026-10-19 R37 加固】``inspect.getsource`` 依赖 ``linecache``，而
    linecache 的首个条目来自 **`.pyc` 里记录的源文件大小/mtime**。当
    ``__pycache__`` 里留着旧版 `.pyc`（本项目实测发生过：engine/event_engine.py
    在 10-19 被两个会话先后改动，`.pyc` 滞后）时，``co_firstlineno`` 指向旧文件
    的行号，`getsource` 会在**新文件**上按旧行号切块 ⇒ 返回另一函数的源码，
    本用例报「恢复路径不再调用 _reset_daily_if_needed」的**假失败**
    （实盘铁证：2026-10-19 run_all 基线 498/501，清掉 __pycache__ 后 499/501）。

    改为**直接读磁盘源码**（共享 helper ``tests.source_guard.method_source``）
    ⇒ 与 `.pyc`/行号彻底解耦，只认「当前文件内容」。这也让守卫对「源码被改但
    pyc 未更新」同样有效。
    """
    from tests.source_guard import method_source
    body = method_source("engine/event_engine.py", "_restore_engine_state")
    assert body is not None, "_restore_engine_state 未找到"
    assert "_reset_daily_if_needed()" in body, (
        "恢复路径不再调用 _reset_daily_if_needed，跨日复位将失效")
    assert "_daily_pnl" in body, "恢复路径不再读写 daily_pnl，本守卫前提已变，须重审"

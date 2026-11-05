# -*- coding: utf-8 -*-
"""跨日真实收益（``day_chain``）守卫：报告不得只留日内口径。

背景（2026-11-05 DAILY-REVIEW G 块，当天就发生的口径分叉）
------------------------------------------------------------
2026-11-05 是**隔夜跳空日**：前收 66,411.12 → 开盘首笔估值 61,870.10（跳空 −6.84%），
收盘 62,409.60。于是同一个交易日在系统里有两个「当日收益」：

  - 引擎 ``risk.daily_pnl``（分母 ``day_open_asset``）= **+539.50 / +0.87%**；
  - 账本 ``daily_ret_pct``（前收 → 今收）           = **−4,001.52 / −6.03%**。

``review_daily.py::_account_daily`` 硬编码 ``mode='paper'``，live 下恒返回 ``{}``
（其 docstring 已写明修它属设计决策），于是报告头版只剩日内口径 —— **把
「−6.03% 的一天」显示成「+0.87%」**，差 6.90pp，正好等于隔夜跳空。

本文件钉住 ``_day_chain`` 五条：
  1. 与账本同口径：前快照日末 → 目标日末，含隔夜跳空；
  2. 只取目标日**当天**的快照做末端（不得被次日快照污染）；
  3. 前值不在收盘附近（<14:30）必须标 ``prev_is_close=False``，不静默当收盘用
     （防 2026-10-30 「11:29 盘中值当收盘」型假亏损复发）；
  4. 无数据返回 ``{}``（报告显示「—」），**绝不猜**；
  5. 纯新增字段：不触碰 ``equity.daily_return_pct`` 语义。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config.settings  # noqa: E402,F401  （须先于 sqlite3：DLL 路径）
from strategy import review_daily as R  # noqa: E402

# 2026-11-05 真实数字（读自 storage/qmt.db equity_snapshots，非编造）
D1104_EOD_TS = "2026-11-04T15:04:44.778680"
D1104_EOD_TA = 66411.12
D1105_EOD_TS = "2026-11-05T15:04:56.727199"
D1105_EOD_TA = 62409.60


def _db(rows):
    """rows: [(ts, total_asset, mode), ...] -> 内存库"""
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE equity_snapshots (ts TEXT, total_asset REAL, mode TEXT)")
    c.executemany("INSERT INTO equity_snapshots VALUES (?,?,?)", rows)
    c.commit()
    return c


def test_cross_day_includes_overnight_gap():
    c = _db([(D1104_EOD_TS, D1104_EOD_TA, "live"),
             ("2026-11-05T09:30:46.751839", 61870.10, "live"),
             (D1105_EOD_TS, D1105_EOD_TA, "live")])
    d = R._day_chain(c, "2026-11-05")
    assert d["prev_asset"] == D1104_EOD_TA
    assert d["eod_asset"] == D1105_EOD_TA
    assert d["daily_pnl"] == -4001.52
    assert d["daily_ret_pct"] == -6.03


def test_intraday_caliber_would_hide_the_gap():
    """反向对照：日内口径（首快照→末快照）必须是 +0.87%，证明两条口径确实不同。

    若哪天有人把 ``_day_chain`` 改成「取当天首条快照」，本断言会红 ——
    那正是把 bug 改回来的方向。
    """
    c = _db([(D1104_EOD_TS, D1104_EOD_TA, "live"),
             ("2026-11-05T09:30:46.751839", 61870.10, "live"),
             (D1105_EOD_TS, D1105_EOD_TA, "live")])
    d = R._day_chain(c, "2026-11-05")
    intraday = round((D1105_EOD_TA / 61870.10 - 1) * 100, 2)
    assert intraday == 0.87
    assert d["daily_ret_pct"] != intraday
    assert abs(d["daily_ret_pct"] - intraday) > 6.0     # 差 6.90pp


def test_end_is_target_day_only():
    """次日快照不得被当成目标日末值。"""
    c = _db([(D1104_EOD_TS, D1104_EOD_TA, "live"),
             (D1105_EOD_TS, D1105_EOD_TA, "live"),
             ("2026-11-06T15:04:00", 70000.0, "live")])
    d = R._day_chain(c, "2026-11-05")
    assert d["eod_asset"] == D1105_EOD_TA
    assert d["eod_ts"] == D1105_EOD_TS


def test_prev_midday_is_flagged_not_silent():
    """前值是 11:29 盘中快照 ⇒ 必须打标记（2026-10-30 型假亏损的防线）。"""
    c = _db([("2026-10-30T11:29:43.000000", 65748.42, "live"),
             ("2026-11-02T15:04:48.745261", 63207.98, "live")])
    d = R._day_chain(c, "2026-11-02")
    assert d["prev_is_close"] is False
    # 数字照算，但标记必须为 False ⇒ 报告加星号，不静默比较
    assert d["daily_ret_pct"] == round((63207.98 / 65748.42 - 1) * 100, 2)


def test_prev_close_is_flagged_true():
    c = _db([(D1104_EOD_TS, D1104_EOD_TA, "live"),
             (D1105_EOD_TS, D1105_EOD_TA, "live")])
    assert R._day_chain(c, "2026-11-05")["prev_is_close"] is True


def test_no_data_returns_empty_and_never_guesses():
    c = _db([])
    assert R._day_chain(c, "2026-11-05") == {}
    # 只有当日、没有前一日 ⇒ 同样返回 {}（不能拿当日首条冒充前一日）
    c2 = _db([(D1105_EOD_TS, D1105_EOD_TA, "live")])
    assert R._day_chain(c2, "2026-11-05") == {}


def test_mode_filter_is_explicit():
    """默认只认 live —— paper 时代的 972,533.60 不得混进 live 跨日链。"""
    c = _db([("2026-10-28T15:00:00", 972533.60, "paper"),
             (D1104_EOD_TS, D1104_EOD_TA, "live"),
             (D1105_EOD_TS, D1105_EOD_TA, "live")])
    d = R._day_chain(c, "2026-11-05")
    assert d["prev_asset"] == D1104_EOD_TA          # 不是 972,533.60
    assert d["mode"] == "live"

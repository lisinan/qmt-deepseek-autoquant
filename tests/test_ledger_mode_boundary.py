# -*- coding: utf-8 -*-
"""账本模式断点守卫：paper → live 切换日不得跨模式链接 prev_asset。

背景（2026-11-01 评估发现，P0 量测层）
------------------------------------
- paper 时期 EOD：**972,533.60**（初始 1,000,000）
- 2026-10-29 15:05 切 live：券商总资产 **66,542.31**
- 10-30 EOD：**63,684.34**

账本 `strategy/record_ledger.py` 原先只按「最近一个有快照的交易日」取
`prev_asset`，不认识模式切换 ⇒ live 首日会被记成

    63,684 / 972,533.60 − 1 = **−93.5%**

而 EVOLVE 全部「当前 vs 目标差距」都以账本为唯一事实来源 ⇒ 会得出
「账户崩了、要降杠杆」的结论，与真实（当日 +1,273 元）完全相反。
这是本项目第 19 类缺陷「口径背离」的量测版本，且**没有任何自动化会去怀疑账本本身**。

本文件钉住五条：
  1. 断点日 prev_asset 必须取断点基线（66,542.31），不是 paper 的 972,533.60；
  2. 断点之后在 live **内部**仍逐日链接（不能每天回基线，否则日收益全成 0）；
  3. range_pct 以断点基线为分母（不再用 paper 的 1,000,000）；
  4. ★ 正向对照：断点**之前**的 paper 行逐位不变（历史不改写）；
  5. 行内必须带 mode 字段，供 KPI#4「live 日度账本」过滤。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config.settings  # noqa: E402,F401  （须先于 sqlite3：DLL 路径）
import strategy.record_ledger as RL   # noqa: E402

PAPER_EOD = 972533.60
LIVE_BASE = 66542.31
LIVE_1029 = 65748.42
LIVE_1030 = 63684.34


def _mkdb() -> Path:
    """造一个最小 equity_snapshots 库（字段与线上一致）。"""
    tmp = Path(tempfile.mkdtemp(prefix="qmt_ledger_")) / "ledger.db"
    con = sqlite3.connect(tmp)
    con.execute(
        "CREATE TABLE equity_snapshots ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, total_asset REAL, "
        "cash REAL, positions_count INTEGER)")
    # build_row_from_db 会顺带统计当日成交笔数/持仓数，缺表会抛 OperationalError
    con.execute("CREATE TABLE fills (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT)")
    rows = [
        ("2026-10-27T15:00:00", PAPER_EOD, 0.0, 0),
        ("2026-10-28T15:00:00", PAPER_EOD, 0.0, 0),
        # ---- 模式断点：10-29 起为 live 真实资金 ----
        ("2026-10-29T15:00:00", LIVE_1029, 8337.31, 1),
        ("2026-10-30T15:00:00", LIVE_1030, 6953.42, 2),
    ]
    con.executemany(
        "INSERT INTO equity_snapshots (ts,total_asset,cash,positions_count) "
        "VALUES (?,?,?,?)", rows)
    con.commit()
    con.close()
    return tmp


def _row(day: str, db: Path) -> dict:
    # ledger_rows=[] 强制走 DB 兜底，保持本文件既有断言（DB 派生 prev）的隔离性
    r = RL.build_row_from_db(day, db, ledger_rows=[])
    assert r is not None, f"{day} 未构造出账本行"
    return r


# ---------------------------------------------------------------- ① 断点日

def test_break_day_prev_is_live_baseline_not_paper():
    """断点日 prev 必须取 live 基线，绝不能链到 paper 的 972,533.60。"""
    db = _mkdb()
    r = _row("2026-10-29", db)
    assert r["prev_asset"] == LIVE_BASE, (
        f"断点日 prev_asset={r['prev_asset']}（应为 live 基线 {LIVE_BASE}）")
    assert r["daily_ret_pct"] > -5.0, (
        f"live 首日被记成 {r['daily_ret_pct']}% 的假崩盘 —— 跨模式链接未断开")


# ---------------------------------------------------------------- ② 内部链接

def test_day_after_break_chains_within_live():
    """断点之后仍逐日链接（10-30 的 prev 应为 10-29 的 EOD）。"""
    db = _mkdb()
    r = _row("2026-10-30", db)
    assert r["prev_asset"] == LIVE_1029, (
        f"10-30 prev={r['prev_asset']}（应为 10-29 EOD {LIVE_1029}）")
    assert abs(r["daily_pnl"] - (LIVE_1030 - LIVE_1029)) < 0.02, r["daily_pnl"]


# ---------------------------------------------------------------- ③ 区间基准

def test_range_pct_uses_live_baseline():
    """live 行区间收益以 live 基线为分母。"""
    db = _mkdb()
    r = _row("2026-10-30", db)
    expect = (LIVE_1030 / LIVE_BASE - 1) * 100.0
    assert abs(r["range_pct"] - round(expect, 2)) < 0.02, (
        f"range_pct={r['range_pct']}（应约 {expect:.2f}%，不能用 paper 的 1,000,000）")


# ---------------------------------------------------------------- ④ 历史不改写

def test_paper_rows_before_break_unchanged():
    """★ 正向对照：断点之前的 paper 行语义逐位不变（历史不重算）。"""
    db = _mkdb()
    r = _row("2026-10-28", db)
    assert r["prev_asset"] == PAPER_EOD, r["prev_asset"]
    assert abs(r["daily_pnl"]) < 0.02, (
        f"paper 无波动日应记 0 收益，实际 {r['daily_pnl']}")
    assert r["mode"] == "paper", r["mode"]


# ---------------------------------------------------------------- ⑤ 可过滤

def test_rows_carry_mode_for_kpi4():
    """行内必须带 mode / baseline，KPI#4 才能只统计 live 日。"""
    db = _mkdb()
    live = _row("2026-10-30", db)
    paper = _row("2026-10-28", db)
    assert live["mode"] == "live" and paper["mode"] == "paper"
    assert live.get("baseline") == LIVE_BASE


# ---------------------------------------------------------------- ⑥ 穿透缺陷

def test_prev_prefers_corrected_ledger_row_over_db_raw():
    """【2026-11-02 穿透缺陷】prev_asset 必须继承上一行账本的修正值，
    而不是回 DB 的原始盘中快照（否则 11-02 会取到 10-30 未修正的 65,748.42）。"""
    tmp = Path(tempfile.mkdtemp(prefix="qmt_ledger_pen_")) / "ledger.db"
    con = sqlite3.connect(tmp)
    con.execute(
        "CREATE TABLE equity_snapshots ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, total_asset REAL, "
        "cash REAL, positions_count INTEGER)")
    con.execute("CREATE TABLE fills (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT)")
    con.executemany(
        "INSERT INTO equity_snapshots (ts,total_asset,cash,positions_count) "
        "VALUES (?,?,?,?)", [
            ("2026-10-30T15:00:00", 65748.42, 6953.42, 2),   # DB 原始（未修正）
            ("2026-11-02T15:00:00", 63207.98, 63207.98, 0),  # 今日 EOD
        ])
    con.commit()
    con.close()
    # 账本里 10-30 已修正为 63,684.42（与线上 fix=ledger_eod_1030_close_recalc 同）
    ledger = [{"date": "2026-10-30", "eod_asset": 63684.42}]
    r = RL.build_row_from_db("2026-11-02", tmp, ledger_rows=ledger)
    assert r is not None
    assert r["prev_asset"] == 63684.42, r["prev_asset"]   # 继承账本修正值
    assert abs(r["daily_ret_pct"] - -0.75) < 0.02, r["daily_ret_pct"]
    # 反向对照：ledger_rows=[] 时必须回退到 DB 原始 65,748.42（旧行为不变）
    r2 = RL.build_row_from_db("2026-11-02", tmp, ledger_rows=[])
    assert r2["prev_asset"] == 65748.42, r2["prev_asset"]

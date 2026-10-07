# -*- coding: utf-8 -*-
"""【2026-10-07 PM-EVOLVE 第 21 轮】账本「陈旧盯市污染」守卫测试。

守的缺陷（P0，量测层）：``equity_snapshots`` 的 EOD 权益是账本（KPI #4）的
唯一锚，但它来自引擎盯市；盯市价一旦不是真实成交价，**账本就会把脏价格
当成账户盈亏记账**。

实盘铁证（R20 AM 发现、本轮接力清理）：2026-10-07 是国庆休市日，引擎
``get_full_tick`` 读到 timetag=2026-10-06 11:30 的**伪快照**，
5 只持仓零成交而市值由 491,347.0 抬到 536,520.0 ⇒ 总资产 975,273.6 →
**1,020,446.6（虚增 +45,173 / +4.63%）**。AM 已修「不再产生新污染」
（``MARK_STALE_FREEZE_VALUATION``），但**已被污染的历史快照仍在库里**：
若 10-08 的 ``prev_asset`` 取到 1,020,446.6，账本会记一笔 −4.4% 的
**假亏损**（与 R14 合成行情假成交同型），KPI #4 再次失真。

判据（全部取自 DB 自身，不依赖任何外部行情源）：
  ① 当日零成交（组合没动） ② 当日权益快照零波动（盯市价根本没在动）
  ③ 却相对前一可信日跳变 > 0.5% ⇒ 判定为陈旧盯市污染，EOD 回溯到最近
     一个可信交易日（语义：没成交也没真实价格变动的日子不应产生盈亏）。

本文件锁死 9 条语义。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import strategy.record_ledger as RL  # noqa: E402


def _make_db(path: Path, days: dict, fills: dict | None = None) -> Path:
    """days: {'2026-10-07': [(ts, total_asset, positions_count), ...]}"""
    import sqlite3
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE equity_snapshots ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, "
                "total_asset REAL, cash REAL, market_value REAL, "
                "positions_count INTEGER, drawdown_pct REAL, mode TEXT)")
    con.execute("CREATE TABLE fills (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "ts TEXT, code TEXT, side TEXT, price REAL, qty INTEGER, "
                "amount REAL, reason TEXT, mode TEXT)")
    for day, snaps in sorted(days.items()):
        for ts, asset, pos in snaps:
            con.execute("INSERT INTO equity_snapshots "
                        "(ts,total_asset,cash,market_value,positions_count,"
                        "drawdown_pct,mode) VALUES (?,?,?,?,?,?,?)",
                        (ts, asset, asset, 0.0, pos, 0.0, "paper"))
    for day, n in (fills or {}).items():
        for i in range(n):
            con.execute("INSERT INTO fills (ts,code,side,price,qty,amount,"
                        "reason,mode) VALUES (?,?,?,?,?,?,?,?)",
                        (f"{day}T10:0{i}:00", "000001.SZ", "BUY",
                         10.0, 100, 1000.0, "test", "paper"))
    con.commit()
    con.close()
    return path


def _patch(tmp: Path, db: Path, ledger_rows=None):
    import json
    RL.DB_PATH = db
    RL.LOG_DIR = tmp
    RL.REPORT_DIR = tmp
    RL.LEDGER_MD = tmp / "EVOLUTION_LEDGER.md"
    RL.LEDGER_JSONL = tmp / "evolution_ledger.jsonl"
    if ledger_rows is not None:
        RL.LEDGER_JSONL.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in ledger_rows),
            encoding="utf-8")


def _reset():
    RL.DB_PATH = ROOT / "storage" / "qmt.db"
    RL.LOG_DIR = ROOT / "logs"
    RL.REPORT_DIR = ROOT / "reports"
    RL.LEDGER_MD = RL.REPORT_DIR / "EVOLUTION_LEDGER.md"
    RL.LEDGER_JSONL = RL.LOG_DIR / "evolution_ledger.jsonl"


# 复刻真实事故：09-30 真实 EOD 975,273.6；10-07（休市）快照恒为 1,020,446.6
_TRUTH = {
    "2026-09-30": [("2026-09-30T15:00:00", 975_273.6, 5)],
    "2026-10-07": [("2026-10-07T09:20:00", 1_020_446.6, 5),
                   ("2026-10-07T12:00:00", 1_020_446.6, 5),
                   ("2026-10-07T15:04:00", 1_020_446.6, 5)],
}


def test_stale_inflated_day_is_flagged():
    """零成交 + 权益全天零波动 + 相对前日跳变 4.63% ⇒ 判为污染。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = _make_db(tmp / "t.db", _TRUTH)
        try:
            _patch(tmp, db)
            assert RL.stale_mark_suspect_day("2026-10-07") is True, \
                "休市日零成交零波动却虚增 4.63%，必须判为陈旧盯市污染"
        finally:
            _reset()


def test_eod_falls_back_to_last_credible_day():
    """污染日的 EOD 必须回溯到最近可信交易日（=真实 09-30 的 975,273.6）。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = _make_db(tmp / "t.db", _TRUTH)
        try:
            _patch(tmp, db)
            assert RL.eod_asset("2026-10-07") == 975_273.6, \
                "污染日不得把虚增的 1,020,446.6 当作 EOD"
            assert RL.eod_asset_raw("2026-10-07") == 1_020_446.6, \
                "原始值仍须可查（留痕与审计需要）"
        finally:
            _reset()


def test_consecutive_stale_days_walk_back_further():
    """连续两个污染日要一路回溯到更早的可信日，而不是停在第一个污染日。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = _make_db(tmp / "t.db", {
            "2026-09-30": [("2026-09-30T15:00:00", 975_273.6, 5)],
            "2026-10-06": [("2026-10-06T10:00:00", 1_020_000.0, 5),
                           ("2026-10-06T15:00:00", 1_020_000.0, 5)],
            "2026-10-07": [("2026-10-07T10:00:00", 1_020_000.0, 5),
                           ("2026-10-07T15:00:00", 1_020_000.0, 5)],
        })
        try:
            _patch(tmp, db)
            assert RL.stale_mark_suspect_day("2026-10-06") is True
            assert RL.eod_asset("2026-10-07") == 975_273.6
        finally:
            _reset()


def test_real_trading_day_with_fills_never_flagged():
    """有成交的真实交易日：即使权益恰好零波动也绝不判污染（防误伤）。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = _make_db(tmp / "t.db", {
            "2026-09-29": [("2026-09-29T15:00:00", 974_013.6, 3)],
            "2026-09-30": [("2026-09-30T10:00:00", 980_000.0, 5),
                           ("2026-09-30T15:00:00", 980_000.0, 5)],
        }, fills={"2026-09-30": 2})
        try:
            _patch(tmp, db)
            assert RL.stale_mark_suspect_day("2026-09-30") is False
            assert RL.eod_asset("2026-09-30") == 980_000.0, \
                "有成交的日子必须原样采用快照值（零回归）"
        finally:
            _reset()


def test_intraday_variation_proves_fresh_marks():
    """零成交但权益在动（真实价格变动）⇒ 盯市是新鲜的，不判污染。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = _make_db(tmp / "t.db", {
            "2026-09-29": [("2026-09-29T15:00:00", 974_013.6, 5)],
            "2026-09-30": [("2026-09-30T10:00:00", 970_000.0, 5),
                           ("2026-09-30T15:00:00", 982_000.0, 5)],
        })
        try:
            _patch(tmp, db)
            assert RL.stale_mark_suspect_day("2026-09-30") is False
            assert RL.eod_asset("2026-09-30") == 982_000.0
        finally:
            _reset()


def test_flat_but_small_move_is_not_flagged():
    """零成交零波动但偏离 < 0.5%（正常静止日）⇒ 不判污染，避免无意义回溯。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = _make_db(tmp / "t.db", {
            "2026-09-29": [("2026-09-29T15:00:00", 974_013.6, 5)],
            "2026-09-30": [("2026-09-30T10:00:00", 976_000.0, 5),
                           ("2026-09-30T15:00:00", 976_000.0, 5)],
        })
        try:
            _patch(tmp, db)
            assert RL.stale_mark_suspect_day("2026-09-30") is False
            assert RL.eod_asset("2026-09-30") == 976_000.0
        finally:
            _reset()


def test_build_row_records_zero_and_leaves_audit_trail():
    """污染日记账：收益 0%，且 note 明写原始脏值被否决（可审计）。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = _make_db(tmp / "t.db", _TRUTH)
        try:
            _patch(tmp, db)
            row = RL.build_row_from_db("2026-10-07")
            assert row is not None
            assert row["eod_asset"] == 975_273.6
            assert abs(row["daily_ret_pct"]) < 1e-9, \
                "休市/污染日不应产生任何盈亏"
            assert "1,020,446.60" in (row.get("note") or ""), \
                "必须留下「原始脏值被中和」的审计痕迹"
        finally:
            _reset()


def test_missing_days_excludes_stale_days():
    """污染日不进「断供日」清单 ⇒ 不会被 --backfill 补记成假行。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = _make_db(tmp / "t.db", _TRUTH)
        try:
            _patch(tmp, db)
            missing = RL.missing_days(path=db, ledger_rows=[])
            assert "2026-10-07" not in missing, \
                "陈旧盯市污染日不得作为断供日被补记"
            assert "2026-09-30" in missing
        finally:
            _reset()


def test_no_regression_on_clean_history():
    """零回归：没有任何污染日时，eod_asset == eod_asset_raw（逐位相同）。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = _make_db(tmp / "t.db", {
            "2026-09-28": [("2026-09-28T15:00:00", 974_013.6, 3)],
            "2026-09-29": [("2026-09-29T15:00:00", 978_840.6, 5)],
        }, fills={"2026-09-29": 2})
        try:
            _patch(tmp, db)
            for d in ("2026-09-28", "2026-09-29"):
                assert RL.eod_asset(d) == RL.eod_asset_raw(d), \
                    f"{d} 未被判污染时取值必须与修复前完全一致"
        finally:
            _reset()

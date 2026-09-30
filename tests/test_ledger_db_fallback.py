# -*- coding: utf-8 -*-
"""【2026-09-30 PM-EVOLVE 第 15 轮】账本断供守卫测试。

守的缺陷（P0，量测层）：``strategy/record_ledger.py`` 的唯一输入是
``logs/review_<date>.json``，而它由 15:35 OBSERVE 自动化产出 ⇒ 三级单点故障
（OBSERVE 未跑 / 中途异常 / 目标日取错）会让账本**静默**断供、无任何告警。

实盘铁证：``logs/evolution_ledger.jsonl`` 只有 2026-09-16 / 2026-09-25 两行，
而 ``equity_snapshots`` 明明有 09-28 / 09-29 / 09-30 三日完整快照 ⇒
KPI #4「paper 近 4 周滚动收益」连续 4 个交易日无法计算，却没人收到报错。

修复原则：``equity_snapshots`` 才是账户真相的**原始出处**，review JSON 只是
它的加工品；加工链断掉时退回原始出处，而不是放弃度量。

本文件锁死 5 条语义：
  1. EOD 权益 = 当日**最后一条**快照（不是最大/最小值）
  2. 跨日收益 = (当日 EOD / 前一有快照交易日 EOD - 1)
  3. 回退行必须带 source="db_fallback"，且复盘专属字段降级 None（绝不猜测）
  4. 有 review JSON 时行为与修复前**逐位相同**（source="review"）
  5. 补记必须能跳过「账户复位」边界之前的日期（否则算出的是复位金额差）
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import strategy.record_ledger as RL  # noqa: E402


# ---------------------------------------------------------------- helpers
def _make_db(path: Path, days: dict, fills: dict | None = None) -> Path:
    """days: {'2026-09-28': [(ts, total_asset, positions_count), ...]}"""
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
    """把 record_ledger 的所有落盘路径重定向到临时目录。"""
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


# ---------------------------------------------------------------- tests
def test_eod_asset_is_last_snapshot_of_day():
    """EOD = 当日**最后一条**快照。反例：取 min/max 会被日内波动污染。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = _make_db(tmp / "t.db", {
            "2026-09-28": [("2026-09-28T09:35:00", 991_309.6, 3),
                           ("2026-09-28T15:00:00", 974_013.6, 3),
                           ("2026-09-28T11:00:00", 981_000.0, 3)],
        })
        try:
            _patch(tmp, db)
            assert RL.eod_asset("2026-09-28") == 974_013.6, \
                "EOD 必须是按 id 排序的最后一条（15:00），不是最大或最小"
            assert RL.eod_asset("2026-09-27") is None
        finally:
            _reset()


def test_prev_snapshot_day_skips_holidays():
    """前一交易日 = 快照中最近的更早日期（自动跳过周末/停市）。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = _make_db(tmp / "t.db", {
            "2026-09-25": [("2026-09-25T15:00:00", 991_309.6, 3)],
            "2026-09-28": [("2026-09-28T15:00:00", 974_013.6, 3)],
        })
        try:
            _patch(tmp, db)
            assert RL.prev_snapshot_day("2026-09-28") == "2026-09-25"
            assert RL.prev_snapshot_day("2026-09-25") is None
        finally:
            _reset()


def test_build_row_from_db_cross_day_return():
    """跨日真实收益口径（含隔夜重估）。用已知真值钉死：
    09-25 EOD 991,309.6 → 09-28 EOD 974,013.6 = -1.745%。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = _make_db(tmp / "t.db", {
            "2026-09-25": [("2026-09-25T15:00:00", 991_309.6, 3)],
            "2026-09-28": [("2026-09-28T15:00:00", 974_013.6, 3)],
        }, fills={"2026-09-28": 4})
        try:
            _patch(tmp, db)
            row = RL.build_row_from_db("2026-09-28")
            assert row is not None
            assert row["prev_asset"] == 991_309.6
            assert row["eod_asset"] == 974_013.6
            assert abs(row["daily_ret_pct"] - (-1.745)) < 0.01, \
                f"跨日收益应为 -1.745%，实际 {row['daily_ret_pct']}"
            assert abs(row["daily_pnl"] - (-17_296.0)) < 1.0
            assert row["fills_n"] == 4
            assert row["eod_positions_n"] == 3
        finally:
            _reset()


def test_db_row_marks_source_and_degrades_unknown_fields():
    """回退行必须自证来源，且复盘流水线专属字段降级 None（绝不猜测）。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = _make_db(tmp / "t.db", {
            "2026-09-28": [("2026-09-28T15:00:00", 974_013.6, 3)],
        })
        try:
            _patch(tmp, db)
            row = RL.build_row_from_db("2026-09-28")
            assert row["source"] == "db_fallback", "回退行必须自证来源"
            for k in ("stable", "safety", "accuracy", "efficiency",
                      "overall", "halt_count", "max_consec_loss"):
                assert row[k] is None, f"{k} 在 DB 回退下无法确证，应为 None"
            # 无前一日 ⇒ 收益降级 None，不得编造 0
            assert row["daily_ret_pct"] is None
        finally:
            _reset()


def test_build_row_from_db_returns_none_without_snapshot():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = _make_db(tmp / "t.db", {
            "2026-09-25": [("2026-09-25T15:00:00", 991_309.6, 3)],
        })
        try:
            _patch(tmp, db)
            assert RL.build_row_from_db("2026-09-28") is None
        finally:
            _reset()


def test_missing_days_detects_gap_and_since_guard():
    """断供检测 + 补记必须能跳过账户复位边界之前的日期。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = _make_db(tmp / "t.db", {
            "2026-09-22": [("2026-09-22T15:00:00", 997_827.0, 8)],
            "2026-09-25": [("2026-09-25T15:00:00", 991_309.6, 3)],
            "2026-09-28": [("2026-09-28T15:00:00", 974_013.6, 3)],
            "2026-09-29": [("2026-09-29T15:00:00", 978_840.6, 5)],
        })
        ledger = [{"date": "2026-09-25"}]
        try:
            _patch(tmp, db, ledger_rows=ledger)
            gap = RL.missing_days(ledger_rows=ledger)
            assert gap == ["2026-09-22", "2026-09-28", "2026-09-29"], gap
            # 09-22 在账本首行之前，且跨越 2026-09-21 账户复位 ⇒ 必须排除
            assert RL.last_ledger_date() == "2026-09-25"
            safe = RL.missing_days(ledger_rows=ledger, since="2026-09-25")
            assert safe == ["2026-09-28", "2026-09-29"], safe
        finally:
            _reset()


def test_review_path_unchanged_when_json_exists():
    """有 review JSON 时行为与修复前逐位相同：source=review，字段取自复盘。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = _make_db(tmp / "t.db", {
            "2026-09-28": [("2026-09-28T15:00:00", 974_013.6, 3)],
        })
        rep = {
            "date": "2026-09-28",
            "account_daily": {"prev_asset": 991309.6, "eod_asset": 974013.6,
                              "daily_ret_pct": -1.745, "daily_pnl": -17296.0},
            "account_range": {"range_pct": -2.599},
            "optimization": {"scores": {"稳定": 75, "安全": 100,
                                        "准确": 100, "高效": 100},
                             "overall": 94},
            "risk": {"halt_count": 0, "max_consecutive_losses": 1},
            "pnl": {"fills_n": 4, "eod_positions": {"a": 1, "b": 2, "c": 3}},
        }
        (tmp / "review_2026-09-28.json").write_text(
            json.dumps(rep, ensure_ascii=False), encoding="utf-8")
        try:
            _patch(tmp, db)
            row = RL.build_row(RL.load_review("2026-09-28"))
            row.setdefault("source", "review")
            assert row["source"] == "review"
            assert row["overall"] == 94          # 复盘字段照旧，不被 DB 覆盖
            assert row["eod_positions_n"] == 3
            assert "note" not in row
        finally:
            _reset()


def test_backfill_writes_rows_and_check_gap_exit_code():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = _make_db(tmp / "t.db", {
            "2026-09-25": [("2026-09-25T15:00:00", 991_309.6, 3)],
            "2026-09-28": [("2026-09-28T15:00:00", 974_013.6, 3)],
            "2026-09-29": [("2026-09-29T15:00:00", 978_840.6, 5)],
        })
        ledger = [{"date": "2026-09-25", "eod_asset": 991309.6,
                   "daily_ret_pct": 0.0, "range_pct": -0.87}]
        try:
            _patch(tmp, db, ledger_rows=ledger)
            # 断供存在 ⇒ --check-gap 返回 2（可被自动化当作告警信号）
            argv = sys.argv
            try:
                sys.argv = ["record_ledger", "--check-gap"]
                assert RL.main() == 2
            finally:
                sys.argv = argv
            # 补记后无断供
            for d in ("2026-09-28", "2026-09-29"):
                row = RL.build_row_from_db(d)
                RL.append_markdown(row)
                RL.append_jsonl(row)
            argv = sys.argv
            try:
                sys.argv = ["record_ledger", "--check-gap"]
                assert RL.main() == 0
            finally:
                sys.argv = argv
            rows = RL.read_ledger()
            assert [r["date"] for r in rows] == ["2026-09-25", "2026-09-28",
                                                 "2026-09-29"]
            assert all(r.get("source") == "db_fallback"
                       for r in rows if r["date"] >= "2026-09-28")
            # md 幂等：同日不重复追加
            RL.append_markdown(RL.build_row_from_db("2026-09-28"))
            md = RL.LEDGER_MD.read_text(encoding="utf-8")
            assert md.count("| 2026-09-28 ") == 1, "同日不得重复写入"
        finally:
            _reset()

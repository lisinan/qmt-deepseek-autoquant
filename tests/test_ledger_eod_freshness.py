# -*- coding: utf-8 -*-
"""账本 EOD 新鲜度守卫：盘中快照不得静默冒充收盘价。

背景（2026-11-01 复核发现，P0 量测层）
------------------------------------
``record_ledger.eod_asset_raw`` 取「当日最后一条权益快照」当 EOD。引擎若在
收盘前退出/休眠，这个值就是盘中价，**但没有任何字段能看出差别**：

    2026-10-30 最后快照 11:29:43 → total_asset 65,748.42（300308 按 573.64 盯市）
    10-30 收盘真实总资产        → 63,684.42（300308 收盘 553.00 ⇒ 差 2,064.00）
    ⇒ 账本记 −1.19%，真实 −4.29%（方向对、幅度低估 3.1pt，故无任何环节报警）

这是本项目「静默口径污染」的第 20 例，且比明显错误更危险：KPI#4 直接建在它上面。

本文件钉住五条：
  1. 收盘后快照（≥15:00）→ ``eod_is_close=True`` 且不追加 note；
  2. 盘中快照（11:29）→ ``eod_is_close=False`` 且 note 含 ``[EOD 非收盘]``；
  3. 无快照 → ``eod_is_close=None``（不猜，遵循「缺字段降级」）；
  4. 两条构造路径（review JSON / DB 直读）都必须带该字段（跨路径一致性）；
  5. ★ 正向对照：新字段不改变任何既有数值语义（eod_asset / prev_asset 逐位不变），
     且收盘快照日**不得**被打上「非收盘」标记（防误杀）。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config.settings  # noqa: E402,F401  （须先于 sqlite3：DLL 路径）
import strategy.record_ledger as RL   # noqa: E402


def _mkdb(rows) -> Path:
    """造最小库：equity_snapshots + fills（build_row_from_db 会统计成交笔数）。"""
    tmp = Path(tempfile.mkdtemp(prefix="qmt_eod_")) / "eod.db"
    con = sqlite3.connect(tmp)
    con.execute(
        "CREATE TABLE equity_snapshots ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, total_asset REAL, "
        "cash REAL, positions_count INTEGER)")
    con.execute("CREATE TABLE fills (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT)")
    con.executemany(
        "INSERT INTO equity_snapshots (ts,total_asset,cash,positions_count) "
        "VALUES (?,?,?,?)", rows)
    con.commit()
    con.close()
    return tmp


# ---------------------------------------------------------------- ① 收盘快照

def test_close_snapshot_is_marked_close():
    """15:00 之后的快照就是收盘价，必须判为可信，且不得被加「非收盘」标记。"""
    db = _mkdb([("2026-10-28T15:04:45", 972533.60, 0.0, 0)])
    r = RL.build_row_from_db("2026-10-28", db)
    assert r is not None
    assert r["eod_is_close"] is True, r["eod_is_close"]
    assert r["eod_ts"].startswith("2026-10-28T15:04"), r["eod_ts"]
    assert "[EOD 非收盘]" not in (r.get("note") or ""), r["note"]


def test_exact_1500_counts_as_close():
    """边界：恰好 15:00:00 必须算收盘（>= 而非 >，避免边界日被误判）。"""
    db = _mkdb([("2026-10-29T15:00:00", 65748.42, 8337.31, 1)])
    r = RL.build_row_from_db("2026-10-29", db)
    assert r["eod_is_close"] is True, r["eod_is_close"]


# ---------------------------------------------------------------- ② 盘中快照

def test_intraday_snapshot_is_flagged_and_noted():
    """11:29 的盘中快照必须被标成不可比，并在 note 里写明原因（可追溯）。"""
    db = _mkdb([
        ("2026-10-30T09:30:53", 62411.28, 8337.28, 1),
        ("2026-10-30T11:29:43", 65748.42, 6953.42, 2),   # ← 引擎 11:29 后休眠
    ])
    r = RL.build_row_from_db("2026-10-30", db)
    assert r is not None
    assert r["eod_is_close"] is False, (
        f"11:29 的快照被当成收盘价（eod_is_close={r['eod_is_close']}）")
    assert "[EOD 非收盘]" in (r.get("note") or ""), r.get("note")
    assert "11:29" in r["note"], r["note"]


# ---------------------------------------------------------------- ③ 无快照

def test_no_snapshot_degrades_to_none():
    """没有当日快照时不猜（build_row_from_db 直接返回 None，annotate 也不伪造）。"""
    db = _mkdb([("2026-10-28T15:04:45", 972533.60, 0.0, 0)])
    assert RL.build_row_from_db("2026-10-29", db) is None
    row = RL.annotate_eod_freshness({"date": "2026-10-29", "eod_asset": 1.0}, db)
    assert row["eod_is_close"] is None, row["eod_is_close"]
    assert row["eod_ts"] is None, row["eod_ts"]


# ---------------------------------------------------------------- ④ 跨路径

def test_review_path_also_carries_freshness():
    """review JSON 路径也必须带 eod_is_close —— 两条路径口径分叉过（R43 第 24 条）。"""
    import inspect
    src = inspect.getsource(RL.build_row)
    assert "annotate_eod_freshness" in src, (
        "build_row 未接 annotate_eod_freshness ⇒ review 路径又会静默丢失收盘判定")
    src2 = inspect.getsource(RL.build_row_from_db)
    assert "annotate_eod_freshness" in src2, (
        "build_row_from_db 未接 annotate_eod_freshness ⇒ DB 路径丢失收盘判定")


# ---------------------------------------------------------------- ⑤ 正向对照

def test_new_fields_do_not_change_existing_numbers():
    """★ 对照组：加字段不得改动任何既有数值（否则就是借清理之名改历史）。"""
    rows = [
        ("2026-10-27T15:00:00", 972533.60, 0.0, 0),
        ("2026-10-28T15:04:45", 972533.60, 0.0, 0),
        ("2026-10-29T15:00:00", 65748.42, 8337.31, 1),
        ("2026-10-30T11:29:43", 63684.42, 6953.42, 2),
    ]
    db = _mkdb(rows)
    r27 = RL.build_row_from_db("2026-10-27", db)
    assert r27["eod_asset"] == 972533.60, r27["eod_asset"]
    assert r27["mode"] == "paper", r27["mode"]
    r30 = RL.build_row_from_db("2026-10-30", db)
    # 数值语义由 MODE_BREAKS 决定，与本函数的字段追加无关
    assert r30["eod_asset"] == 63684.42, r30["eod_asset"]
    assert r30["prev_asset"] == 65748.42, r30["prev_asset"]
    assert r30["mode"] == "live", r30["mode"]
    assert r30["eod_is_close"] is False, "盘中价必须被标出，否则清理失效"


# ---------------------------------------------------------------- ⑥ 修补路径

def test_repair_path_carries_freshness_marker():
    """★ 修补路径必须带新鲜度标记（2026-11-03 审计发现的第 6 条）。

    全账本唯一真出过「盘中值当收盘」事故的就是 2026-10-30 这一行，而它恰恰是
    唯一**连键都没有**的行 —— 因为 ``scripts/_fix_ledger_1030_eod_20261101.py``
    手工改 EOD 时绕过了 ``annotate_eod_freshness``。消费端
    ``row.get("eod_is_close", True)`` 会取默认 True，把重算值当成可信收盘值。

    前 5 条用例只测生成端（build_row*），不测修补端 —— 这就是它们全绿却漏掉
    这个缺陷的原因（「测了 A 路径，就以为 B 路径也被测了」）。
    """
    import importlib.util
    p = ROOT / "scripts" / "_fix_ledger_1030_eod_20261101.py"
    assert p.exists(), f"修补脚本不存在：{p}"
    spec = importlib.util.spec_from_file_location("_fix1030", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    out = mod._rebuild({"date": "2026-10-30", "eod_asset": 65748.42, "note": ""})
    assert "eod_ts" in out, "修补行缺 eod_ts ⇒ 消费端无法判来源"
    assert "eod_is_close" in out, (
        "修补行缺 eod_is_close 键 ⇒ row.get(..., True) 默认按「可信收盘」处理")
    assert out["eod_is_close"] is False, \
        "该日无 15:00 后快照 ⇒ 不得声称为可信收盘口径"
    assert out["eod_ts"] is None, "当日无收盘快照 ⇒ 不得把 11:29 盘中快照伪造成 eod_ts"
    assert "EOD 新鲜度" in out["note"], f"note 未说明来源：{out['note']}"
    # 正向对照：标注字段不得改写任何既有数值
    assert out["eod_asset"] == 63684.42, out["eod_asset"]
    assert out["prev_asset"] == 66542.31, out["prev_asset"]
    assert out["fix"] == "ledger_eod_1030_close_recalc", out["fix"]

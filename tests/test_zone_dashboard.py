# -*- coding: utf-8 -*-
"""双功能区统一看板（`scripts/zone_dashboard.py`）守卫。

## 这份测试在防什么

看板是**独立进程 + 纯只读**的形态，它不进引擎、不碰 `config.settings`，
所以它出错的唯一方式就是「悄悄偏离事实」：

1. **端口 / 库路径两处实现漂移**（纪律 #14「同一事实两份实现 = 必然静默漂移」）。
   `ZONES` 里硬编码了 5000 / 5001 与两个 DB 路径，而它们是 `core/zone.py::zone_config()`
   的派生值。哪天有人只改了一边，看板就会**安静地展示另一个区的数据** ——
   比报错危险得多（看起来一切正常，实际在看错误的东西）。
2. **只读被破坏**。看板对 live 库必须零写入：它跑在 live 引擎旁边，任何时候
   对 `storage/qmt.db` 的写入都可能与引擎事务打架。所以打开方式必须是
   `mode=ro`，且源码里不得出现 INSERT/UPDATE/DELETE。
3. **归一化口径**。paper 起始 100 万、live 真实 5.8 万，差约 17 倍；不归一而同图，
   live 会被压成贴地线。这里钉死「窗口首点 = 100」，且首点非正时**不出点**
   （避免除零或符号翻转）。
4. **单区故障不得拖垮整页**。任一区离线时 `build_payload()` 必须返回
   `online=False` + 错误串，绝不抛异常（抛了就是白屏，双区一起看不见）。

纪律：本模块所有用例都只碰临时目录里的 sqlite，绝不打开生产库；HTTP 用例
只起临时端口，不访问 5000 / 5001 上的真实引擎（`build_payload` 被替换成桩）。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import scripts.zone_dashboard as zd
from core.zone import VALID_ZONES, zone_config


# ---------------------------------------------------------------- 工具
@contextmanager
def _tmpdir():
    """临时目录：用 ignore_errors=True 清理。

    不用 `tempfile.TemporaryDirectory`：Windows 上它清 sqlite 文件时偶发
    `NotADirectoryError`（把 `q.db` 当目录 scandir），会让**用例本身**假失败。
    """
    d = tempfile.mkdtemp(prefix="zdash_")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _make_db(path: Path, rows) -> Path:
    """建一个最小账本：equity_snapshots / orders / fills。rows = [(ts, total_asset)]。"""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE equity_snapshots (ts TEXT, total_asset REAL)")
        conn.execute("CREATE TABLE orders (ts TEXT)")
        conn.execute("CREATE TABLE fills (ts TEXT)")
        conn.executemany("INSERT INTO equity_snapshots VALUES (?,?)", rows)
        conn.commit()
    finally:
        conn.close()
    return path


def _ts(hhmm: str) -> str:
    """今天 + 指定时分，返回 ISO 字符串（与引擎写入格式同形）。"""
    d = datetime.now().date().isoformat()
    return f"{d}T{hhmm}:00"


# ---------------------------------------------------------------- 1. 两处实现一致性
def test_zone_ports_match_core_zone():
    """ZONES 的端口必须等于 zone_config() 的 web_port —— 防看板看错区。"""
    by_key = {z["key"]: z for z in zd.ZONES}
    assert set(by_key) == set(VALID_ZONES), \
        f"ZONES 的 key 必须恰好是 {VALID_ZONES}，实际 {sorted(by_key)}"
    for key in VALID_ZONES:
        want = zone_config(key).web_port
        got = by_key[key]["port"]
        assert got == want, (
            f"{key} 端口漂移：看板 {got} != zone_config {want}。"
            f"只改一边的后果是看板安静地展示另一个区的数据。"
        )


def test_zone_db_paths_match_core_zone():
    """ZONES 的库路径必须等于 zone_config() 的 db_path（逐项、且互不相交）。"""
    by_key = {z["key"]: z for z in zd.ZONES}
    seen = {}
    for key in VALID_ZONES:
        want = Path(zone_config(key).db_path).resolve()
        got = Path(by_key[key]["db"]).resolve()
        assert got == want, f"{key} 库路径漂移：看板 {got} != zone_config {want}"
        assert got.exists() or True          # 库可以尚未创建，路径正确即可
        assert got not in seen, \
            f"{key} 与 {seen[got]} 指向同一个库 {got} —— 两区共用账本会串数据"
        seen[got] = key
    # 端口也必须互不相同（否则一个区永远连到另一个区）
    ports = [by_key[k]["port"] for k in VALID_ZONES]
    assert len(set(ports)) == len(ports), f"两区端口重复：{ports}"


def test_zone_keys_have_display_fields():
    """看板要展示的字段不得为空 —— 缺了页面会显示成空白卡片。"""
    for z in zd.ZONES:
        for f in ("key", "name", "sub", "port", "db", "color"):
            assert z.get(f) not in (None, ""), f"{z.get('key')} 缺少展示字段 {f}"


# ---------------------------------------------------------------- 2. 只读
def test_read_series_is_read_only():
    """源码守卫：只读打开 + 无任何写语句。

    看板跑在 live 引擎旁边，对 `storage/qmt.db` 的任何写入都可能与引擎事务打架。
    """
    src = Path(zd.__file__).read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "mode=ro" in code, "sqlite 必须用 mode=ro 只读打开"
    for kw in ("INSERT ", "UPDATE ", "DELETE ", "CREATE TABLE", "DROP TABLE"):
        assert kw not in code, f"看板源码里出现写语句 {kw!r} —— 看板必须是纯只读"


def test_read_series_does_not_modify_db():
    """行为守卫：读完之后行数与 mtime 都不变。"""
    with _tmpdir() as td:
        db = _make_db(Path(td) / "q.db",
                      [(_ts("09:30"), 1_000_000.0), (_ts("10:30"), 1_010_000.0)])
        before_rows = sqlite3.connect(str(db)).execute(
            "SELECT COUNT(*) FROM equity_snapshots").fetchone()[0]
        before_mtime = db.stat().st_mtime_ns
        out = zd._read_series(db)
        assert out["error"] is None, out["error"]
        after_rows = sqlite3.connect(str(db)).execute(
            "SELECT COUNT(*) FROM equity_snapshots").fetchone()[0]
        assert after_rows == before_rows
        assert db.stat().st_mtime_ns == before_mtime, "只读读取不得改动库文件"


# ---------------------------------------------------------------- 3. 归一化口径
def test_read_series_normalizes_to_100():
    """窗口首点必须 = 100，后续点按首点缩放；amount 保留原值供 tooltip。"""
    with _tmpdir() as td:
        db = _make_db(Path(td) / "q.db", [
            (_ts("09:30"), 1_000_000.0),
            (_ts("10:30"), 1_010_000.0),
            (_ts("11:30"), 995_000.0),
        ])
        out = zd._read_series(db)
        assert out["error"] is None, out["error"]
        pts = out["points"]
        assert len(pts) == 3, pts
        assert pts[0][1] == 100.0, f"首点必须归一成 100，实际 {pts[0][1]}"
        assert abs(pts[1][1] - 101.0) < 1e-6, pts[1]
        assert abs(pts[2][1] - 99.5) < 1e-6, pts[2]
        # 原始金额必须留着（页面 tooltip 要显示真实资产）
        assert abs(pts[1][2] - 1_010_000.0) < 1e-6, pts[1]
        # 时间升序
        assert [p[0] for p in pts] == sorted(p[0] for p in pts)


def test_read_series_first_point_nonpositive_yields_no_points():
    """首点 <= 0 时不出点（避免除零 / 符号翻转成反向曲线）。"""
    with _tmpdir() as td:
        db = _make_db(Path(td) / "q.db", [(_ts("09:30"), 0.0), (_ts("10:30"), 100.0)])
        out = zd._read_series(db)
        assert out["error"] is None
        assert out["points"] == [], f"首点为 0 时不该出点，实际 {out['points']}"


def test_read_series_caps_point_count():
    """点数必须被抽样压到 SERIES_LIMIT 以内（否则 6000+ 点会压住浏览器）。"""
    with _tmpdir() as td:
        rows = [(_ts("09:30"), 1_000_000.0 + i) for i in range(zd.SERIES_LIMIT * 4)]
        db = _make_db(Path(td) / "q.db", rows)
        out = zd._read_series(db)
        assert out["error"] is None
        assert len(out["points"]) <= zd.SERIES_LIMIT + 1, \
            f"抽样后仍有 {len(out['points'])} 点（上限 {zd.SERIES_LIMIT}）"
        assert out["rows"] > 0


def test_read_series_missing_db_returns_error_not_raise():
    """库不存在时返回错误串，绝不抛 —— 新 paper 区首日就是这个状态。"""
    out = zd._read_series(Path(tempfile.gettempdir()) / "__no_such_zone__.db")
    assert out["points"] == []
    assert out["error"], "库不存在时必须给出可读的错误串"


# ---------------------------------------------------------------- 4. 单区故障不拖垮整页
def test_build_payload_offline_zones_do_not_raise(monkeypatch=None):
    """两个区都离线时：online=False + error 非空，整体不抛。"""
    with _tmpdir() as td:
        dead = [dict(z) for z in zd.ZONES]
        for d in dead:
            d["port"] = 1                  # 未监听端口，连不上
            d["db"] = Path(td) / f"{d['key']}.db"   # 库也不存在
        orig = zd.ZONES
        zd.ZONES = tuple(dead)
        try:
            payload = zd.build_payload()
        finally:
            zd.ZONES = orig
        assert set(payload["zones"]) == set(VALID_ZONES)
        for key in VALID_ZONES:
            z = payload["zones"][key]
            assert z["online"] is False, f"{key} 应判为离线"
            assert z["error"], f"{key} 离线时必须给出错误串"
            assert z["total_asset"] is None, "离线区不得编造资金数据"
            assert z["series"]["points"] == []


def test_build_payload_online_shape():
    """在线区字段齐全（用桩快照验证取数路径，不碰真实引擎）。"""
    fake_snap = {
        "exec_mode": "paper", "strategy_mode": "momentum",
        "data_mode": "tushare", "broker_mode": "paper_no_trade",
        "broker_connected": False,
        "total_asset": 1_000_000.0, "cash": 1_000_000.0,
        "positions": {"600000.SH": {"volume": 100}},
        "risk": {"halted": False, "halt_reason": None,
                 "peak_asset": 1_000_000.0, "asset_trusted": True},
        "session": {"state": "closed", "trading_now": False},
        "gate_funnel": {"candidates": 0}, "no_entry_reason": "非交易时段",
        "tick": 12, "market_data_ts": _ts("15:00"), "degraded": [],
    }
    orig_fetch, orig_zones = zd._fetch_snapshot, zd.ZONES
    zd._fetch_snapshot = lambda port: dict(fake_snap)       # noqa: ARG005
    with _tmpdir() as td:
        zd.ZONES = tuple(dict(z, db=Path(td) / f"{z['key']}.db") for z in orig_zones)
        try:
            payload = zd.build_payload()
        finally:
            zd._fetch_snapshot, zd.ZONES = orig_fetch, orig_zones
    z = payload["zones"]["paper"]
    assert z["online"] is True
    assert z["exec_mode"] == "paper"
    assert z["positions_n"] == 1, "positions_n 必须是持仓个数"
    assert z["halted"] is False
    assert z["trading_now"] is False
    assert z["gate_funnel"] == {"candidates": 0}


def test_pick_handles_nested_and_missing():
    """点路径取数：嵌套命中 / 中间缺失 / 叶子为 None 都回落 default。"""
    snap = {"risk": {"halted": True, "peak_asset": None}, "exec_mode": "live"}
    assert zd._pick(snap, "risk.halted") is True
    assert zd._pick(snap, "risk.peak_asset", "x") == "x", "None 叶子必须回落 default"
    assert zd._pick(snap, "risk.nope", "y") == "y"
    assert zd._pick(snap, "a.b.c", "z") == "z"
    assert zd._pick(snap, "exec_mode") == "live"


def test_db_stats_today_counts():
    """今日委托 / 成交计数 + 最后成交时间（回答「这个区今天动没动手」）。"""
    today = datetime.now().date().isoformat()
    with _tmpdir() as td:
        db = _make_db(Path(td) / "q.db", [(_ts("09:30"), 1_000_000.0)])
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO orders VALUES (?)", (f"{today}T09:31:00",))
        conn.execute("INSERT INTO orders VALUES (?)", (f"{today}T09:32:00",))
        conn.execute("INSERT INTO orders VALUES ('2026-01-05T09:31:00')")
        conn.execute("INSERT INTO fills VALUES (?)", (f"{today}T09:33:00",))
        conn.commit()
        conn.close()
        out = zd._db_stats(db)
    assert out["orders_today"] == 2, out
    assert out["fills_today"] == 1, out
    assert out["last_fill_ts"].startswith(today), out


def test_db_stats_missing_db_is_none_not_zero():
    """库不存在时只能是 None（未知），不能报 0（会被读成「今天没交易」）。"""
    out = zd._db_stats(Path(tempfile.gettempdir()) / "__no_such_zone__.db")
    assert out["orders_today"] is None, out
    assert out["fills_today"] is None, out


# ---------------------------------------------------------------- 5. HTTP 层
def test_http_routes_serve_page_json_and_healthz():
    """起临时端口，验 / 、/api/zones、/healthz、未知路径。"""
    stub = {"generated_at": "x", "series_days": zd.SERIES_DAYS,
            "zones": {k: {"key": k, "online": False} for k in VALID_ZONES}}
    orig = zd.build_payload
    zd.build_payload = lambda: stub
    srv = zd.ThreadingHTTPServer(("127.0.0.1", 0), zd.Handler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        base = f"http://127.0.0.1:{port}"
        with urllib.request.urlopen(base + "/healthz", timeout=5) as r:
            assert r.status == 200 and r.read() == b"ok"
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            html = r.read().decode("utf-8")
            assert "双功能区" in html or "统一看板" in html, "首页必须是看板页面"
            assert "canvas" in html.lower(), "页面必须含权益曲线画布"
        with urllib.request.urlopen(base + "/api/zones", timeout=5) as r:
            got = json.loads(r.read().decode("utf-8"))
            assert set(got["zones"]) == set(VALID_ZONES)
        try:
            urllib.request.urlopen(base + "/nope", timeout=5)
            raise AssertionError("未知路径应返回 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        srv.shutdown()
        srv.server_close()
        zd.build_payload = orig


def test_dashboard_imports_no_project_modules():
    """源码守卫：看板不得 import 项目模块（否则改引擎代码就要重启看板）。

    唯一例外是标准库与 sqlite/urllib。它必须能在引擎挂掉时也正常启动。
    """
    src = Path(zd.__file__).read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    forbidden = ("from config", "import config", "from core", "import core",
                 "from engine", "import engine", "from storage", "import storage",
                 "from web", "import web", "from strategy", "import strategy")
    for kw in forbidden:
        assert kw not in code, (
            f"看板出现 {kw!r} —— 它必须是独立进程，import 项目模块会把"
            f"「改引擎代码就得重启看板」这个耦合带回来"
        )

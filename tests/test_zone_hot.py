# -*- coding: utf-8 -*-
"""热点面板（`scripts/zone_dashboard.py` 的 `/api/hot`）守卫。

## 这份测试在防什么

热点面板要回答「AI 产业链哪个环节在升温、哪些票是热点」。它和权益曲线一样是
**独立进程 + 纯只读**形态，因此出错方式同样是「悄悄偏离事实」而不是报错：

1. **聚合口径被改坏**。引擎盘中每 15~30 秒重扫一遍，同一只票一天能落几百行
   （2026-11-06：新易盛 804 行、中际旭创 597 行）。若哪天有人把「每票取当日
   最后一条」改成「对全表求均值」，热度就变成按**扫描频率**加权 —— 榜还是那张榜，
   数字却成了采样伪影。这里用人为构造的小库把口径钉死：同票多行只认最后一条。
2. **环比张冠李戴**。`heat_delta` 必须比「同名板块」的上一交易日，且上一交易日
   没有该板块时必须是 `None` 而不是 `0`（`0` = 热度没变，`None` = 今天才进榜，
   两种含义完全不同）。
3. **空库炸整页**。paper 区明天才有数据，今天 `sector_recommendations` 是空表。
   空库 / 缺表 / 库不存在都必须退化成「空数组 + 可读 error」，绝不抛。
4. **缓存失效时静默打到生产库**。页面每 5 秒轮询 `/api/zones`；热点若不缓存，
   就会反复全表聚合 live 库（10 万行），与正在写库的 live 引擎抢锁 —— 这是整个
   看板唯一可能**影响到生产**的路径。用计数器桩钉住「TTL 内第二次不重新查库」。
5. **只读纪律**。热点查询同样必须只有 `SELECT`，且仍以 `mode=ro` 打开。

纪律：本模块所有用例只碰临时目录里的 sqlite；跑 HTTP 的用例只绑 127.0.0.1:0
（系统临时端口），并且把 `ZONES` 临时指向临时库。**绝不打开生产库、绝不访问
5000 / 5001 上正在跑的真实引擎。**
"""
from __future__ import annotations

import inspect
import json
import re
import shutil
import sqlite3
import tempfile
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import scripts.zone_dashboard as zd

# 固定日期（不用「今天」）：热点面板是历史取数，用例不该随运行日漂移。
_D2, _D1 = "2026-11-06", "2026-11-05"


# ---------------------------------------------------------------- 工具
@contextmanager
def _tmpdir():
    """临时目录：用 ignore_errors=True 清理。

    不用 `tempfile.TemporaryDirectory`：Windows 上它清 sqlite 文件时偶发
    `NotADirectoryError`（把 q.db 当目录 scandir），会让**用例本身**假失败。
    """
    d = tempfile.mkdtemp(prefix="zhot_")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@contextmanager
def _zones_on(td: Path):
    """把看板两个区临时指向 td 里的临时库，进出都清热点缓存。

    这样即使被测代码绕过桩真的去开库，也只碰临时目录。
    """
    orig = zd.ZONES
    zd.ZONES = tuple(dict(z, db=Path(td) / f"{z['key']}.db") for z in orig)
    zd._hot_cache_clear()
    try:
        yield {z["key"]: Path(z["db"]) for z in zd.ZONES}
    finally:
        zd.ZONES = orig
        zd._hot_cache_clear()


_SECTOR_DDL = (
    "CREATE TABLE sector_recommendations ("
    "id INTEGER PRIMARY KEY, ts TEXT, code TEXT, name TEXT, sector TEXT, "
    "sector_label TEXT, composite REAL, heat_contribution REAL, tech_score REAL, "
    "fundamental_score REAL, pe REAL, roe REAL, change_pct REAL, reason TEXT)"
)
_SIGNAL_DDL = (
    "CREATE TABLE signals ("
    "id INTEGER PRIMARY KEY, ts TEXT, code TEXT, name TEXT, side TEXT, score REAL, "
    "price REAL, reason TEXT, factors_json TEXT, ai_comment TEXT, "
    "ai_confidence REAL, blocked_by TEXT)"
)


def _make_db(path: Path, sectors=(), signals=(),
             with_sectors: bool = True, with_signals: bool = True) -> Path:
    """建一个最小热点库。行格式见 `_srow()` / `_sig()`。"""
    conn = sqlite3.connect(str(path))
    try:
        if with_sectors:
            conn.execute(_SECTOR_DDL)
            conn.executemany(
                "INSERT INTO sector_recommendations "
                "(ts, code, name, sector, sector_label, composite, "
                "heat_contribution, tech_score, fundamental_score, pe, roe, "
                "change_pct, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [tuple(r) for r in sectors],
            )
        if with_signals:
            conn.execute(_SIGNAL_DDL)
            conn.executemany(
                "INSERT INTO signals (ts, code, name, side, score, price, "
                "reason, blocked_by) VALUES (?,?,?,?,?,?,?,?)",
                [tuple(r) for r in signals],
            )
        conn.commit()
    finally:
        conn.close()
    return path


def _srow(day, hhmm, code, name, label, composite, heat,
          tech=5.0, fund=5.0, pe=30.0, roe=10.0, chg=0.0):
    """一条板块推荐原始行（列顺序与引擎写入一致）。"""
    return (f"{day}T{hhmm}:00", code, name, "s", label, composite, heat,
            tech, fund, pe, roe, chg, "r")


def _sig(ts, code, name, side="BUY", score=0.0, blocked_by=""):
    return (ts, code, name, side, score, 10.0, "why", blocked_by)


def _two_day_rows():
    """两日样本：晶圆代工/设备（两日都有）、AI 应用/软件（只在 _D2）。

    数值刻意选成「除得尽」：晶圆代工 _D2 均热 = (2.83+3.17)/2 = 3.0，
    _D1 均热 = (2.0+1.0)/2 = 1.5 ⇒ 环比应为 +1.5。
    """
    return [
        _srow(_D2, "09:35", "688981.SH", "中芯国际", "晶圆代工/设备", 4.57, 2.83,
              tech=7.96, fund=3.5, pe=188.02, roe=2.77, chg=0.19),
        _srow(_D2, "09:40", "688012.SH", "中微公司", "晶圆代工/设备", 6.0, 3.17,
              chg=0.19),
        _srow(_D2, "09:35", "002415.SZ", "海康威视", "AI 应用/软件", 2.0, 1.0),
        _srow(_D1, "09:35", "688981.SH", "中芯国际", "晶圆代工/设备", 4.0, 2.0),
        _srow(_D1, "09:35", "688012.SH", "中微公司", "晶圆代工/设备", 5.0, 1.0),
    ]


# ---------------------------------------------------------------- 1. 板块聚合
def test_sectors_aggregate_sort_and_top():
    """板块聚合：n / avg_heat / 排序 / 龙头股 全部要对得上手算结果。"""
    with _tmpdir() as td, _zones_on(td) as dbs:
        _make_db(dbs["live"], sectors=_two_day_rows())
        out = zd.build_hot("live")
    assert out["error"] is None, out["error"]
    assert out["day"] == _D2
    assert out["prev_day"] == _D1
    sec = out["sectors"]
    assert len(sec) == 2, sec

    # 降序：晶圆代工 3.0 > AI 应用 1.0
    assert sec[0]["label"] == "晶圆代工/设备", sec
    assert sec[1]["label"] == "AI 应用/软件", sec

    top = sec[0]
    assert top["n"] == 2, f"n 必须是当日在册票数，实际 {top['n']}"
    assert top["avg_heat"] == 3.0, f"均热应为 (2.83+3.17)/2 = 3.0，实际 {top['avg_heat']}"
    # 龙头 = 板块内综合分最高的那只（中微公司 6.0 > 中芯国际 4.57）
    assert top["top"] == "688012.SH 中微公司", top["top"]
    assert abs(top["avg_composite"] - 5.285) <= 0.01, top["avg_composite"]

    assert sec[1]["n"] == 1
    assert sec[1]["avg_heat"] == 1.0


def test_stocks_sorted_by_composite_and_capped_at_15():
    """个股榜：按 composite 降序、最多 15 只，且字段是库里那一行的原值。"""
    rows = [_srow(_D2, "09:35", f"{i:06d}.SZ", f"票{i}", "同一环节",
                  composite=float(i), heat=1.0, pe=10.0 + i, roe=1.0 + i,
                  chg=0.5)
            for i in range(1, 19)]                       # 18 只，故意超出 15
    with _tmpdir() as td, _zones_on(td) as dbs:
        _make_db(dbs["live"], sectors=rows)
        out = zd.build_hot("live")
    stk = out["stocks"]
    assert len(stk) == zd.HOT_STOCKS == 15, f"应截到 15 只，实际 {len(stk)}"
    comps = [s["composite"] for s in stk]
    assert comps == sorted(comps, reverse=True), f"必须按 composite 降序：{comps}"
    assert comps[0] == 18.0, f"榜首应是综合分最高的，实际 {comps[0]}"
    # 原值可核查：pe / roe / change_pct 就是该行写进去的值
    first = stk[0]
    assert first["pe"] == 28.0 and first["roe"] == 19.0, first
    for f in ("code", "name", "sector_label", "composite", "heat",
              "tech", "fund", "pe", "roe", "change_pct"):
        assert f in first, f"个股条目缺字段 {f}"


def test_same_code_multiple_scans_uses_last_row():
    """同票当日多行 ⇒ 只认最后一条（否则热度变成按扫描频率加权）。"""
    rows = [
        _srow(_D2, "09:35", "300502.SZ", "新易盛", "光模块/光器件", 1.0, 9.9),
        _srow(_D2, "11:00", "300502.SZ", "新易盛", "光模块/光器件", 3.0, 5.0),
        _srow(_D2, "15:04", "300502.SZ", "新易盛", "光模块/光器件", 5.31, 1.5),
    ]
    with _tmpdir() as td, _zones_on(td) as dbs:
        _make_db(dbs["live"], sectors=rows)
        out = zd.build_hot("live")
    assert len(out["stocks"]) == 1, f"同一只票只能出一条，实际 {len(out['stocks'])}"
    s = out["stocks"][0]
    assert s["composite"] == 5.31 and s["heat"] == 1.5, s
    # 板块均值也必须用最后一条，不能被 9.9 那条早期高点拉高
    assert out["sectors"][0]["avg_heat"] == 1.5, out["sectors"][0]
    assert out["sectors"][0]["n"] == 1, "n 是票数，不是行数"


def test_signals_latest_20_descending():
    """信号流水：最近 20 条、按 ts 降序，blocked_by 归一成空串。"""
    sigs = [_sig(f"{_D2}T{9 + i // 60:02d}:{i % 60:02d}:00", f"{i:06d}.SZ",
                 f"票{i}", blocked_by=("风控拦截" if i % 5 == 0 else None))
            for i in range(25)]
    with _tmpdir() as td, _zones_on(td) as dbs:
        _make_db(dbs["live"], sectors=_two_day_rows(), signals=sigs)
        out = zd.build_hot("live")
    got = out["signals"]
    assert len(got) == zd.HOT_SIGNALS == 20, f"应取 20 条，实际 {len(got)}"
    ts = [s["ts"] for s in got]
    assert ts == sorted(ts, reverse=True), "信号必须按 ts 降序"
    assert ts[0] == f"{_D2}T09:24:00", f"首条应是最新的，实际 {ts[0]}"
    assert all(s["blocked_by"] is not None for s in got), "blocked_by 不得是 None"
    assert any(s["blocked_by"] for s in got), "被拦下的信号必须保留拦截原因"


def test_days_capped_at_5_and_day_param_moves_prev_day():
    """days 只回最近 5 个有数据日；指定 day 时 prev_day 要跟着往前走。"""
    days = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06",
            "2026-11-09"]
    rows = [_srow(d, "09:35", "688981.SH", "中芯国际", "晶圆代工/设备", 1.0, 1.0)
            for d in days]
    with _tmpdir() as td, _zones_on(td) as dbs:
        _make_db(dbs["live"], sectors=rows)
        out = zd.build_hot("live")
        back = zd.build_hot("live", "2026-11-05")
    assert len(out["days"]) == zd.HOT_DAYS == 5, out["days"]
    assert out["days"] == list(reversed(days))[:5], out["days"]
    assert out["day"] == "2026-11-09" and out["prev_day"] == "2026-11-06", out
    assert back["day"] == "2026-11-05", back["day"]
    assert back["prev_day"] == "2026-11-04", (
        f"指定 day 后 prev_day 必须是该日的上一交易日，实际 {back['prev_day']}")


# ---------------------------------------------------------------- 2. 环比
def test_heat_delta_compares_same_sector_with_prev_day():
    """heat_delta = 今日该板块 avg_heat − 上一交易日同名板块 avg_heat。"""
    with _tmpdir() as td, _zones_on(td) as dbs:
        _make_db(dbs["live"], sectors=_two_day_rows())
        out = zd.build_hot("live")
    by = {s["label"]: s for s in out["sectors"]}
    assert by["晶圆代工/设备"]["heat_delta"] == 1.5, (
        f"3.0 − 1.5 应为 +1.5，实际 {by['晶圆代工/设备']['heat_delta']}")


def test_heat_delta_is_none_when_prev_day_lacks_sector():
    """上一交易日没有该板块 ⇒ heat_delta 必须是 None，不能是 0。

    0 会被读成「热度没变」，而真实含义是「这板块今天才进榜」。
    """
    with _tmpdir() as td, _zones_on(td) as dbs:
        _make_db(dbs["live"], sectors=_two_day_rows())
        out = zd.build_hot("live")
    by = {s["label"]: s for s in out["sectors"]}
    assert by["AI 应用/软件"]["heat_delta"] is None, (
        f"上一日无该板块时应为 None，实际 {by['AI 应用/软件']['heat_delta']}")
    assert by["AI 应用/软件"]["avg_heat"] == 1.0, "没有环比也要照常出均热"


def test_heat_delta_none_when_only_one_day_of_data():
    """只有一天数据时所有 heat_delta 都是 None，页面显示「—」而不是假 0。"""
    with _tmpdir() as td, _zones_on(td) as dbs:
        _make_db(dbs["live"], sectors=[
            _srow(_D2, "09:35", "688981.SH", "中芯国际", "晶圆代工/设备", 4.57, 2.83)])
        out = zd.build_hot("live")
    assert out["prev_day"] is None, out["prev_day"]
    assert out["sectors"][0]["heat_delta"] is None, out["sectors"][0]


# ---------------------------------------------------------------- 3. 降级不炸
def test_empty_table_degrades_with_error():
    """空表（paper 区现状）⇒ 空数组 + 可读 error，绝不抛。"""
    with _tmpdir() as td, _zones_on(td) as dbs:
        _make_db(dbs["paper"])                      # 建表但不插数据
        out = zd.build_hot("paper")
    assert out["sectors"] == [] and out["stocks"] == [] and out["signals"] == []
    assert out["days"] == [] and out["day"] is None
    assert out["error"], "空表必须给出可读 error（页面据此显示空态）"
    assert "尚无" in out["error"], f"error 应说明原因，实际 {out['error']!r}"


def test_missing_table_degrades_with_error():
    """缺表（旧库 / 迁移未跑）⇒ 同样只降级，不抛。"""
    with _tmpdir() as td, _zones_on(td) as dbs:
        _make_db(dbs["live"], with_sectors=False, with_signals=False)
        out = zd.build_hot("live")
    assert out["sectors"] == [] and out["stocks"] == []
    assert out["error"], "缺表必须给出可读 error"


def test_missing_db_degrades_with_error():
    """库不存在（新区首次启动前）⇒ 降级，不抛。"""
    with _tmpdir() as td, _zones_on(td):
        out = zd.build_hot("paper")                 # 库文件根本没建
    assert out["sectors"] == [] and out["signals"] == []
    assert out["error"], "库不存在时必须给出可读 error"


def test_missing_signals_table_does_not_kill_sectors():
    """信号表缺失只影响流水，不能把已经算好的板块一起扔掉（分层降级）。"""
    with _tmpdir() as td, _zones_on(td) as dbs:
        _make_db(dbs["live"], sectors=_two_day_rows(), with_signals=False)
        out = zd.build_hot("live")
    assert out["signals"] == [], "缺信号表时流水为空"
    assert out["sectors"], "板块数据不能被信号表的故障连带清空"
    assert out["stocks"], "个股数据同上"


# ---------------------------------------------------------------- 4. 非法 zone
def test_invalid_zone_raises_value_error():
    """非法 zone ⇒ ValueError（端点据此回 400，而不是 500）。"""
    for bad in ("bogus", "", "LIVE"):
        try:
            zd.build_hot(bad)
            raise AssertionError(f"非法 zone {bad!r} 应抛 ValueError")
        except ValueError as e:
            assert "未知区" in str(e), e


# ---------------------------------------------------------------- 5. 缓存
def test_hot_cache_ttl_is_60_seconds():
    """TTL 常量钉死 60 秒（spec 约束：5 秒页面轮询绝不能直连 10 万行全表聚合）。"""
    assert zd._HOT_CACHE_TTL == 60, zd._HOT_CACHE_TTL


def test_hot_cache_hit_within_ttl_does_not_requery():
    """TTL 内同一 (zone, day) 第二次调用不得再查库（用计数器桩验证）。"""
    calls = {"n": 0}
    orig = zd._query_hot

    def stub(db_path, day=None):                    # noqa: ARG001
        calls["n"] += 1
        return {"day": _D2, "days": [_D2, _D1], "prev_day": _D1,
                "sectors": [], "stocks": [], "signals": [], "error": None}

    with _tmpdir() as td, _zones_on(td):
        zd._query_hot = stub
        try:
            a = zd.build_hot("live")
            b = zd.build_hot("live")                # 命中缓存
            c = zd.build_hot("live", _D1)           # 不同 day ⇒ 新 key
        finally:
            zd._query_hot = orig
    assert calls["n"] == 2, f"同 (zone,day) 应只查库一次，实际 {calls['n']} 次"
    assert a["day"] == b["day"] == _D2
    assert c["zone"] == "live"


def test_hot_cache_expires_after_ttl():
    """超过 TTL 必须重新查库（否则数据永远停在第一次，成了假实时）。"""
    calls = {"n": 0}
    orig = zd._query_hot

    def stub(db_path, day=None):                    # noqa: ARG001
        calls["n"] += 1
        return {"day": _D2, "days": [_D2], "prev_day": None,
                "sectors": [], "stocks": [], "signals": [], "error": None}

    with _tmpdir() as td, _zones_on(td):
        zd._query_hot = stub
        try:
            zd.build_hot("live")
            assert calls["n"] == 1
            key = ("live", None)
            assert key in zd._HOT_CACHE, "第一次调用必须留下缓存条目"
            # 把条目写入时刻人工往前拨，等价于「已过 TTL」（不 patch 全局 time）
            stamp, payload = zd._HOT_CACHE[key]
            zd._HOT_CACHE[key] = (stamp - zd._HOT_CACHE_TTL - 1, payload)
            zd.build_hot("live")
        finally:
            zd._query_hot = orig
    assert calls["n"] == 2, f"TTL 过后必须重新查库，实际 {calls['n']} 次"


def test_hot_cache_does_not_grow_with_bogus_day():
    """非法 day 不进缓存 —— 否则 (zone, 任意字符串) 会把缓存撑成内存泄漏。"""
    calls = {"n": 0}
    orig = zd._query_hot

    def stub(db_path, day=None):                    # noqa: ARG001
        calls["n"] += 1
        return {"day": _D2, "days": [_D2], "prev_day": None,
                "sectors": [], "stocks": [], "signals": [], "error": None}

    with _tmpdir() as td, _zones_on(td):
        zd._query_hot = stub
        try:
            for i in range(5):
                zd.build_hot("live", f"1999-01-0{i + 1}")
            n_cached = len(zd._HOT_CACHE)
            assert calls["n"] == 5, calls["n"]
        finally:
            zd._query_hot = orig
    assert n_cached == 0, f"非法 day 不得写入缓存，实际缓存了 {n_cached} 条"


# ---------------------------------------------------------------- 6. HTTP 层
def test_http_hot_returns_200_with_contract_shape():
    """`/api/hot` 200 + 契约字段齐全（这是页面渲染的唯一数据源）。"""
    with _tmpdir() as td, _zones_on(td) as dbs:
        _make_db(dbs["live"], sectors=_two_day_rows())
        srv = zd.ThreadingHTTPServer(("127.0.0.1", 0), zd.Handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{port}/api/hot?zone=live"
            with urllib.request.urlopen(url, timeout=10) as r:
                assert r.status == 200
                got = json.loads(r.read().decode("utf-8"))
        finally:
            srv.shutdown()
            srv.server_close()

    for k in ("zone", "day", "days", "prev_day", "sectors", "stocks",
              "signals", "error"):
        assert k in got, f"响应缺少契约字段 {k}"
    assert got["zone"] == "live" and got["day"] == _D2
    assert got["sectors"] and got["stocks"], "有数据时必须返回板块与个股"


def test_http_hot_invalid_zone_returns_400_not_500():
    """非法 zone ⇒ 400 + 说明；绝不能是 500（500 会被读成「看板坏了」）。"""
    with _tmpdir() as td, _zones_on(td):
        srv = zd.ThreadingHTTPServer(("127.0.0.1", 0), zd.Handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{port}/api/hot?zone=bogus"
            try:
                urllib.request.urlopen(url, timeout=10)
                raise AssertionError("非法 zone 应返回 400")
            except urllib.error.HTTPError as e:
                assert e.code == 400, f"非法 zone 应回 400，实际 {e.code}"
                body = json.loads(e.read().decode("utf-8"))
                assert body.get("error"), "400 也要带可读说明"
        finally:
            srv.shutdown()
            srv.server_close()


# ---------------------------------------------------------------- 7. 只读守卫
def test_hot_queries_are_select_only():
    """源码守卫：热点三处取数只有 SELECT，且仍以 mode=ro 打开。

    看板跑在 live 引擎旁边，对 `storage/qmt.db` 的任何写入都可能与引擎事务打架。
    """
    src = Path(zd.__file__).read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "mode=ro" in code, "sqlite 必须用 mode=ro 只读打开"

    for fn in (zd._day_rows, zd._recent_signals, zd._query_hot):
        fsrc = inspect.getsource(fn)
        assert "SELECT" in fsrc.upper(), f"{fn.__name__} 里找不到 SELECT"
        for kw in ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"):
            assert not re.search(rf"\b{kw}\b", fsrc), (
                f"{fn.__name__} 里出现写语句 {kw} —— 热点面板必须纯只读")

    assert "sector_recommendations" in inspect.getsource(zd._day_rows)
    assert "FROM signals" in inspect.getsource(zd._recent_signals)
    # 整份源码也不得出现写语句（与 test_zone_dashboard 的只读守卫同口径）
    for kw in ("INSERT ", "UPDATE ", "DELETE ", "CREATE TABLE", "DROP TABLE"):
        assert kw not in code, f"看板源码里出现写语句 {kw!r}"


def test_hot_read_does_not_modify_db():
    """行为守卫：跑完整套聚合后，库文件行数与 mtime 都不变。"""
    with _tmpdir() as td, _zones_on(td) as dbs:
        db = _make_db(dbs["live"], sectors=_two_day_rows(),
                      signals=[_sig(f"{_D2}T09:35:00", "688981.SH", "中芯国际")])
        before_rows = sqlite3.connect(str(db)).execute(
            "SELECT COUNT(*) FROM sector_recommendations").fetchone()[0]
        before_mtime = db.stat().st_mtime_ns
        out = zd.build_hot("live")
        assert out["sectors"], "前提：必须真的读到了数据"
        after_rows = sqlite3.connect(str(db)).execute(
            "SELECT COUNT(*) FROM sector_recommendations").fetchone()[0]
        assert db.stat().st_mtime_ns == before_mtime, "只读读取不得改动库文件"
    assert after_rows == before_rows


def test_hot_panel_imports_no_project_modules():
    """源码守卫：热点面板同样不得 import 项目模块（独立进程是它的存在理由）。"""
    src = Path(zd.__file__).read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    for kw in ("from config", "import config", "from core", "import core",
               "from engine", "import engine", "from storage", "import storage",
               "from web", "import web", "from strategy", "import strategy"):
        assert kw not in code, f"看板出现 {kw!r} —— 它必须是独立进程"

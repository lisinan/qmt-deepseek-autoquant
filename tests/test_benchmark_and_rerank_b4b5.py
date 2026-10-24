# -*- coding: utf-8 -*-
"""B4/B5 验收守卫（对应 docs/OPTIMIZATION_SPEC_B4B5_2026-10-21.md §3）。

覆盖 6 组契约：

  1) strategy.benchmark.compute：等权 BH 数值正确 / 数据不足跳过且 n_codes 正确 /
     load_daily 抛异常时返回 None 字段且不抛异常（合成日线，monkeypatch load_daily，
     **绝不联网**）。
  2) EventEngine.snapshot()["benchmark"] 含契约全部键；1800 秒内重复调用只算一次
     （计数器断言 _compute_benchmark 只被调一次）。
  3) rerank 缓存：同一 codes/heat、market_ctx 变化两次调用 → 底层 client 只调一次、
     cached_hits==1、第二次 result.cached 为 True（C-B5-1）。
  4) rerank 失败：client.chat_json 返回 None / 抛异常 → 返回 None、last_error 含
     模型名、calls 递增、产生 WARNING 日志（C-B5-2）。
  5) storage.save_llm_rerank 落库可查（临时 QMT_DB / 临时 sqlite 隔离）。
  6) 触发条件：非交易时段不调用 _fire_llm_rerank；距上次不足
     LLM_RERANK_INTERVAL_SEC 不调用（C-B5-4）。

全部离线：不连 broker、不起主循环、不触网。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 独立运行本文件时也要隔离生产库 / 生产 notices 日志（run_all.py 已设则不动）。
_TMP = Path(tempfile.mkdtemp(prefix="qmt_b4b5_"))
os.environ.setdefault("QMT_DB", str(_TMP / "b4b5_qmt.db"))
os.environ.setdefault("QMT_NOTICES_LOG", str(_TMP / "b4b5_notices.log"))

import engine.event_engine as EE                # noqa: E402
import strategy.benchmark as bench_mod          # noqa: E402
from ai import llm_reranker as rerank_mod       # noqa: E402
from ai.llm_reranker import LLMReranker         # noqa: E402
from config.settings import LLM_RERANK_INTERVAL_SEC  # noqa: E402
from core.data_models import Tick               # noqa: E402
from storage.db import Storage                 # noqa: E402
from strategy.sector_scorer import (           # noqa: E402
    SectorScore, StockRecommendation)


# ============================================================ 通用 helper

def _mk_sector(name="光模块", label="光模块/光器件", heat=5.0):
    return SectorScore(
        sector=name, label=label, heat_score=heat,
        avg_change_pct=1.0, avg_volume_ratio=1.2, strength=0.75,
        n_stocks=4, n_up=3, best_code="300308.SZ", best_name="中际旭创",
        best_change_pct=2.0,
    )


def _mk_rec(code, name="x", composite=5.0, sector="光模块"):
    return StockRecommendation(
        ts=datetime.now(), code=code, name=name,
        sector=sector, sector_label="光模块/光器件",
        composite=composite, heat_contribution=5.0, tech_score=5.0,
        fundamental_score=5.0, pe=50.0, roe=15.0, change_pct=1.0,
        reason="t",
    )


def _panel(base, last, dates=("20260101", "20260102", "20260105")):
    """合成日线面板（load_daily 的返回结构：date/close 平行数组）。"""
    closes = [base, (base + last) / 2.0, last]
    return {"date": list(dates), "close": closes}


class _LogCapture:
    """捕获指定 logger 的记录（用于断言 WARNING）。"""

    def __init__(self, logger_name):
        self.logger = logging.getLogger(logger_name)
        self.records = []
        self._handler = None

    def __enter__(self):
        cap = self

        class _H(logging.Handler):
            def emit(self, record):
                cap.records.append(record)

        self._handler = _H(level=logging.WARNING)
        self.logger.addHandler(self._handler)
        prev = self.logger.propagate
        self.logger.propagate = False
        self._prev_propagate = prev
        return self.records

    def __exit__(self, *exc):
        if self._handler is not None:
            self.logger.removeHandler(self._handler)
        self.logger.propagate = self._prev_propagate
        return False


def _tmp_db():
    fd, p = tempfile.mkstemp(suffix=".db", dir=str(_TMP))
    os.close(fd)
    return Path(p)


def _make_engine(tmp_log_dir: Path, storage: Storage = None):
    """构造一个离线 paper 引擎（不连 broker、不起主循环）。"""
    eng = EE.EventEngine(
        exec_mode="paper", auto_init_positions=False,
        enable_sector_scorer=False, enable_dynamic_universe=False,
        enable_llm_reranker=False,
        storage=storage or Storage(_tmp_db()),
    )
    eng.analyst = type("FakeAnalyst", (), {"enabled": False, "client": None})()
    return eng


class _TempLogDir:
    """把基准日度落盘目录临时指向临时目录（绝不写生产 logs/），退出时还原。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self._orig = None

    def __enter__(self):
        self._orig = EE.LOG_DIR
        EE.LOG_DIR = str(self.path)
        return self.path

    def __exit__(self, *exc):
        EE.LOG_DIR = self._orig
        return False


# ============================================================ 1) benchmark.compute

def test_benchmark_compute_equal_weight_and_index():
    """等权 BH 数值正确（各标的「末值/基点−1」的算术平均），宽基同法。"""
    panels = {
        # A: +20%；B: +10%；指数: +5%
        "A.SZ": _panel(100.0, 120.0),
        "B.SZ": _panel(100.0, 110.0),
        "IDX": _panel(1000.0, 1050.0),
    }
    orig = bench_mod.load_daily
    bench_mod.load_daily = lambda code, count=260: panels.get(code)
    try:
        out = bench_mod.compute(since="2026-01-01", codes=["A.SZ", "B.SZ"],
                                 index_code="IDX")
    finally:
        bench_mod.load_daily = orig

    assert out["universe_bh_pct"] is not None
    expected = (0.20 + 0.10) / 2.0
    assert abs(out["universe_bh_pct"] - expected) < 1e-9, out
    assert out["index_pct"] is not None
    assert abs(out["index_pct"] - 0.05) < 1e-9, out
    assert out["n_codes"] == 2, out
    assert out["since"] == "2026-01-01"
    assert out["as_of"] == "2026-01-05", out
    assert out["note"] == "ok", out


def test_benchmark_compute_skips_insufficient_code():
    """某 code 数据不足（since 后无有效收盘 / 直接 None）时被跳过，n_codes 正确。"""
    panels = {
        "A.SZ": _panel(100.0, 120.0),
        "SHORT.SZ": _panel(100.0, 110.0, dates=("20251201", "20251202")),  # 全在 since 前
        "NONE.SZ": None,                                                   # 直接无数据
        "IDX": _panel(1000.0, 1050.0),
    }
    orig = bench_mod.load_daily
    bench_mod.load_daily = lambda code, count=260: panels.get(code)
    try:
        out = bench_mod.compute(since="2026-01-01",
                                 codes=["A.SZ", "SHORT.SZ", "NONE.SZ"],
                                 index_code="IDX")
    finally:
        bench_mod.load_daily = orig

    assert out["n_codes"] == 1, out          # 只有 A.SZ 有效
    assert abs(out["universe_bh_pct"] - 0.20) < 1e-9, out
    assert "SHORT.SZ" in out["note"], out
    assert "NONE.SZ" in out["note"], out


def test_benchmark_compute_never_raises_on_load_error():
    """load_daily 抛异常时返回 None 字段且不抛异常（铁律：任何异常都不得逃逸）。"""
    def boom(code, count=260):
        raise RuntimeError("load failed: " + str(code))

    orig = bench_mod.load_daily
    bench_mod.load_daily = boom
    try:
        out = bench_mod.compute(since="2026-01-01", codes=["A.SZ", "B.SZ"],
                                 index_code="IDX")
    finally:
        bench_mod.load_daily = orig

    assert out["universe_bh_pct"] is None, out
    assert out["index_pct"] is None, out
    assert out["n_codes"] == 0, out
    assert out["note"], out


# ============================================================ 2) snapshot 基准

_BENCH_KEYS = ("ts", "since", "as_of", "account_pct", "universe_bh_pct",
               "index_pct", "cash_pct", "excess_pct", "basis", "n_codes")


def test_snapshot_benchmark_contains_contract_keys():
    """snapshot()["benchmark"] 含契约全部键（正常算成态）。"""
    log_dir = _TMP / "logs_ok"
    eng = _make_engine(log_dir)
    eng._benchmark_since = lambda: "2026-01-01"

    panels = {"IDX": _panel(1000.0, 1050.0)}
    orig = bench_mod.load_daily
    bench_mod.load_daily = lambda code, count=260: panels.get(
        code, _panel(100.0, 110.0))
    try:
        with _TempLogDir(log_dir):
            eng._compute_benchmark()
            snap = eng.snapshot()
    finally:
        bench_mod.load_daily = orig

    bench = snap["benchmark"]
    for k in _BENCH_KEYS:
        assert k in bench, f"benchmark 缺少契约键 {k}: {bench}"
    assert bench["basis"] == "initial_cash", bench
    assert bench["n_codes"] > 0, bench
    # account_pct = 总资产/INITIAL_CASH − 1（新引擎无持仓 ⇒ 0）
    assert abs(bench["account_pct"]) < 1e-9, bench
    # excess_pct = account_pct − universe_bh_pct
    assert bench["excess_pct"] is not None, bench
    assert abs(bench["excess_pct"] -
               (bench["account_pct"] - bench["universe_bh_pct"])) < 1e-9, bench
    assert bench["cash_pct"] == 0.0, bench

    # 每日首次算成落盘 logs/benchmark_daily.jsonl（临时目录）
    f = Path(log_dir) / "benchmark_daily.jsonl"
    assert f.exists(), f
    row = json.loads(f.read_text(encoding="utf-8").strip().splitlines()[-1])
    for k in ("date", "account_pct", "universe_bh_pct", "index_pct",
              "excess_pct", "n_codes"):
        assert k in row, row


def test_benchmark_snapshot_cached_within_1800s():
    """1800 秒内重复调用只算一次（_compute_benchmark 计数器 == 1）。"""
    log_dir = _TMP / "logs_cache"
    eng = _make_engine(log_dir)
    eng._benchmark_since = lambda: "2026-01-01"

    panels = {"IDX": _panel(1000.0, 1050.0)}
    orig_load = bench_mod.load_daily
    bench_mod.load_daily = lambda code, count=260: panels.get(
        code, _panel(100.0, 110.0))
    try:
        calls = {"n": 0}
        real = eng._compute_benchmark

        def counting():
            calls["n"] += 1
            return real()

        eng._compute_benchmark = counting
        eng._benchmark_ts = 0.0            # 强制首次刷新
        with _TempLogDir(log_dir):
            first = eng._benchmark_snapshot()
            thr = eng._benchmark_thread
            assert thr is not None
            thr.join(timeout=10)
            assert not thr.is_alive()
            assert calls["n"] == 1, calls

            # 1800 秒内再调两次：不得重复计算
            second = eng._benchmark_snapshot()
            third = eng._benchmark_snapshot()
        assert calls["n"] == 1, f"基准被重复计算 {calls['n']} 次"
        # 缓存命中：返回的是已算成的快照
        assert second.get("basis") == "initial_cash", second
        assert third.get("basis") == "initial_cash", third
        assert second.get("ts") == third.get("ts"), (second, third)
        assert second.get("n_codes", 0) > 0, second
    finally:
        bench_mod.load_daily = orig_load


# ============================================================ 3) rerank 缓存

class _StubClient:
    """可计数的 LLM 客户端桩。"""

    _DEFAULT = {
        "macro_view": "bullish",
        "analysis": "板块强势",
        "ranked_codes": ["300308.SZ", "688256.SH"],
        "adjustments": {"300308.SZ": "+0.5 ROE 强支撑"},
    }

    def __init__(self, model="stub-model", payload="__default__", raises=None,
                 last_error=None):
        self.enabled = True
        self.model = model
        self.calls = 0
        self.payload = (self._DEFAULT if payload == "__default__" else payload)
        self.raises = raises
        self.last_error = last_error

    def chat_json(self, messages, **kw):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.payload

    def health(self):
        return {"enabled": True, "model": self.model,
                "last_error": self.last_error}


def test_rerank_cache_ignores_market_ctx():
    """同一 codes/heat、market_ctx 变化 → 底层 client 只调一次，cached_hits==1。"""
    stub = _StubClient()
    orig_ds, orig_or = rerank_mod.deepseek, rerank_mod.openrouter
    rerank_mod.deepseek = stub
    rerank_mod.openrouter = stub
    try:
        r = LLMReranker(cache_ttl_sec=300)
        recs = [_mk_rec("300308.SZ"), _mk_rec("688256.SH")]
        heat = {"光模块": _mk_sector()}
        r1 = r.rerank(recs, heat, {"大盘": "+0.10%"})
        r2 = r.rerank(recs, heat, {"大盘": "-0.85%"})   # market_ctx 变了
        assert r1 is not None and r2 is not None
        assert stub.calls == 1, f"底层 client 被调 {stub.calls} 次"
        assert r.snapshot()["cached_hits"] == 1, r.snapshot()
        assert r2.cached is True
        assert r.snapshot()["calls"] == 1, r.snapshot()
        assert r.snapshot()["cache_size"] == 1, r.snapshot()
    finally:
        rerank_mod.deepseek, rerank_mod.openrouter = orig_ds, orig_or


def test_rerank_cache_key_excludes_market_ctx():
    """契约级断言：缓存键只含 codes + heat（不含 market_ctx）。"""
    import inspect
    src = inspect.getsource(LLMReranker.rerank)
    assert '"codes": codes' in src
    assert '"heat": heat_sig' in src
    # market_ctx 仍进 prompt（只影响展示/推理）
    assert "market_ctx" in src


# ============================================================ 4) rerank 失败

def test_rerank_failure_on_none_result_is_diagnosable():
    """chat_json 返回 None → 返回 None、last_error 含模型名、calls 递增、有 WARN。"""
    stub = _StubClient(payload=None, last_error="http://x HTTP 503")
    orig_ds, orig_or = rerank_mod.deepseek, rerank_mod.openrouter
    rerank_mod.deepseek = stub
    rerank_mod.openrouter = stub
    try:
        r = LLMReranker()
        recs = [_mk_rec("300308.SZ")]
        heat = {"光模块": _mk_sector()}
        with _LogCapture("ai.llm_reranker") as recs_log:
            out = r.rerank(recs, heat, {})
        assert out is None
        assert r.snapshot()["calls"] == 1, r.snapshot()
        assert "stub-model" in r.last_error, r.last_error
        assert r.snapshot()["last_error"] == r.last_error
        assert r.snapshot()["last_error_ts"] > 0
        warns = [x for x in recs_log if x.levelno >= logging.WARNING]
        assert warns, "失败分支必须打 WARNING，不能静默"
        assert any("LLM rerank 失败" in x.getMessage() for x in warns), \
            [x.getMessage() for x in warns]
    finally:
        rerank_mod.deepseek, rerank_mod.openrouter = orig_ds, orig_or


def test_rerank_failure_on_client_exception_is_diagnosable():
    """chat_json 抛异常 → 返回 None、last_error 含模型名、有 WARN。"""
    stub = _StubClient(raises=RuntimeError("boom"))
    orig_ds, orig_or = rerank_mod.deepseek, rerank_mod.openrouter
    rerank_mod.deepseek = stub
    rerank_mod.openrouter = stub
    try:
        r = LLMReranker()
        recs = [_mk_rec("300308.SZ")]
        heat = {"光模块": _mk_sector()}
        with _LogCapture("ai.llm_reranker") as recs_log:
            out = r.rerank(recs, heat, {})
        assert out is None
        assert r.snapshot()["calls"] == 1, r.snapshot()
        assert "stub-model" in r.last_error, r.last_error
        assert "RuntimeError" in r.last_error, r.last_error
        warns = [x for x in recs_log if x.levelno >= logging.WARNING]
        assert warns, "客户端异常分支必须打 WARNING"
    finally:
        rerank_mod.deepseek, rerank_mod.openrouter = orig_ds, orig_or


# ============================================================ 5) save_llm_rerank 落库

def test_save_llm_rerank_persists_and_queryable():
    """save_llm_rerank 落库可查（临时 sqlite 隔离，不碰生产库）。"""
    db = _tmp_db()
    st = Storage(db)
    try:
        rid = st.save_llm_rerank(
            ts="2026-10-21T10:00:00", model="deepseek-chat",
            macro_view="bullish",
            ranked=["300308.SZ", "688256.SH"],
            adjustments={"300308.SZ": "+0.5 ROE 强支撑"},
            raw='{"macro_view":"bullish"}', cached=False)
        assert rid > 0, rid
        conn = st._conn_get()
        rows = conn.execute(
            "SELECT ts, model, macro_view, ranked_json, adjustments_json, "
            "raw, cached FROM llm_rerank WHERE id=?", (rid,)).fetchall()
        assert len(rows) == 1, rows
        r = rows[0]
        assert r["ts"] == "2026-10-21T10:00:00"
        assert r["model"] == "deepseek-chat"
        assert r["macro_view"] == "bullish"
        assert json.loads(r["ranked_json"]) == ["300308.SZ", "688256.SH"]
        assert "300308.SZ" in json.loads(r["adjustments_json"])
        assert r["cached"] == 0

        # cached=True 也可落库
        rid2 = st.save_llm_rerank(ts="2026-10-21T10:10:00", model="m",
                                  macro_view="neutral", ranked=[],
                                  adjustments={}, raw="", cached=True)
        assert rid2 > 0
        v = st._conn_get().execute(
            "SELECT cached FROM llm_rerank WHERE id=?", (rid2,)).fetchone()[0]
        assert v == 1
        n = st._conn_get().execute(
            "SELECT COUNT(*) FROM llm_rerank").fetchone()[0]
        assert n == 2, n
    finally:
        try:
            st.close()
        except Exception:
            pass
        db.unlink(missing_ok=True)


# ============================================================ 6) 触发条件

class _FakeSectorScorer:
    """最小 sector_scorer 桩：只提供 _evaluate_sectors 用到的接口。"""

    def __init__(self, recs):
        self.recommendations = list(recs)
        self.sector_scores = {"光模块": _mk_sector()}

    def evaluate_sectors(self, market_data):
        return None

    def build_recommendations(self, market_data, tech_scores):
        return None

    def best_target(self):
        return self.recommendations[0] if self.recommendations else None


class _StubReranker:
    enabled = True

    def __init__(self):
        self.calls = 0

    def rerank(self, recs, sector_scores, market_ctx):
        self.calls += 1
        return None

    def snapshot(self):
        return {"enabled": True, "model": "stub", "calls": self.calls,
                "cached_hits": 0, "last_call_ts": 0.0, "last_ok_ts": 0.0,
                "last_error": "", "last_error_ts": 0.0, "cache_size": 0}


def _trigger_engine(log_dir):
    eng = _make_engine(log_dir)
    eng.sector_scorer = _FakeSectorScorer([_mk_rec("300308.SZ")])
    eng.llm_reranker = _StubReranker()
    eng._tick_count = 1        # 避开「每 5 tick 落库推荐池」分支
    fired = []
    eng._fire_llm_rerank = lambda md: fired.append(md)
    return eng, fired


def _ticks():
    return {"300308.SZ": Tick(ts=datetime.now(), code="300308.SZ",
                              name="中际旭创", price=100.0, change_pct=1.0)}


def test_rerank_not_triggered_outside_trading_time():
    """非交易时段（is_trading_time=False）不触发 LLM 重排。"""
    eng, fired = _trigger_engine(_TMP)
    orig = EE.is_trading_time
    EE.is_trading_time = lambda dt=None: False
    try:
        eng._last_llm_rerank_ts = 0.0    # 间隔已满足
        eng._evaluate_sectors(_ticks())
    finally:
        EE.is_trading_time = orig
    assert fired == [], "非交易时段不应触发 _fire_llm_rerank"


def test_rerank_not_triggered_before_interval_elapsed():
    """距上次触发不足 LLM_RERANK_INTERVAL_SEC 时不触发。"""
    eng, fired = _trigger_engine(_TMP)
    orig = EE.is_trading_time
    EE.is_trading_time = lambda dt=None: True
    try:
        eng._last_llm_rerank_ts = time.time() - (LLM_RERANK_INTERVAL_SEC - 1)
        eng._evaluate_sectors(_ticks())
        assert fired == [], "间隔未到不应触发 _fire_llm_rerank"
        # 间隔已到 + 交易时段 ⇒ 应触发
        eng._last_llm_rerank_ts = time.time() - (LLM_RERANK_INTERVAL_SEC + 1)
        eng._evaluate_sectors(_ticks())
        assert len(fired) == 1, fired
        assert eng._last_llm_rerank_ts > 0
    finally:
        EE.is_trading_time = orig


# ============================================================ 7) 实施期修正回归

def test_rerank_cache_hit_returns_copy_not_alias():
    """回归（WP-L 发现）：缓存命中不得改写第一次调用者手里的对象。

    原实现命中时原地 ``rr.cached = True``，导致 r1 与 r2 是同一实例、
    r1.cached 被悄悄改成 True，调用方无法区分「本次是否命中」。
    """
    stub = _StubClient()
    orig_ds, orig_or = rerank_mod.deepseek, rerank_mod.openrouter
    rerank_mod.deepseek = stub
    rerank_mod.openrouter = stub
    try:
        r = LLMReranker(cache_ttl_sec=300)
        recs = [_mk_rec("300308.SZ"), _mk_rec("688256.SH")]
        heat = {"光模块": _mk_sector()}
        r1 = r.rerank(recs, heat, {"大盘": "+0.10%"})
        r2 = r.rerank(recs, heat, {"大盘": "+0.10%"})
        assert r1 is not None and r2 is not None
        assert r1 is not r2, "缓存命中必须返回副本，不能返回同一对象"
        assert r1.cached is False, f"第一次调用者的 cached 被改写: {r1.cached}"
        assert r2.cached is True, r2.cached
        assert stub.calls == 1, stub.calls
    finally:
        rerank_mod.deepseek, rerank_mod.openrouter = orig_ds, orig_or


def test_benchmark_daily_upsert_keeps_one_row_per_day():
    """回归（WP-L 发现）：同一交易日多次刷新必须只留一行，且以最后一次为准。

    原实现用进程内标记「首次算成才 append」⇒ 引擎重启会重复写行，且早盘
    首次的资产口径会把当日行永久钉住。
    """
    log_dir = _TMP / "logs_upsert"
    eng = _make_engine(log_dir)
    eng._benchmark_since = lambda: "2026-01-01"

    panels = {"IDX": _panel(1000.0, 1050.0)}
    orig = bench_mod.load_daily
    bench_mod.load_daily = lambda code, count=260: panels.get(
        code, _panel(100.0, 110.0))
    try:
        asset = {"v": 1_000_000.0}
        eng._total_asset = lambda: asset["v"]
        with _TempLogDir(log_dir):
            eng._compute_benchmark()
            asset["v"] = 1_100_000.0        # 盘中资产上涨
            eng._compute_benchmark()        # 再次刷新（应覆盖当日行）
    finally:
        bench_mod.load_daily = orig

    f = Path(log_dir) / "benchmark_daily.jsonl"
    rows = [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()
            if x.strip()]
    today = datetime.now().date().isoformat()
    same_day = [r for r in rows if r.get("date") == today]
    assert len(same_day) == 1, f"同一交易日应只有一行，实际 {rows}"
    assert abs(same_day[0]["account_pct"] - 0.10) < 1e-6, same_day[0]

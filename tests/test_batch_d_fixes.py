# -*- coding: utf-8 -*-
"""批次 D 修复的回归测试（工程增强，零策略语义变更）。

覆盖：
  #10 分钟线预热：引擎启动即从 get_history('1m') 灌满 _bars，
      消除「每次重启 / 每天开盘先瞎 60 分钟」（占交易时长 25%，且早盘正是
      趋势股最活跃、突破最多发的时段）
  #11 push 缓存新鲜度：原实现写了 _pushed_at 却从不读取，某只股停止推送
      （停牌/掉线）时会无限期复用陈旧价格，而止损/峰值/总资产全依赖它
  #9  Web 层 Storage 单例：原每请求新建且从不 close（建表迁移重跑 + 连接泄漏）
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import (          # noqa: E402
    BAR_WARMUP_MAX_STALE_DAYS as STALE_LIMIT, STRATEGY_PARAMS,
)
import core.qmt_client as QC                      # noqa: E402
import engine.event_engine as EE                  # noqa: E402


def _engine():
    eng = EE.EventEngine(exec_mode="paper", auto_init_positions=False,
                         enable_sector_scorer=False,
                         enable_dynamic_universe=False,
                         enable_llm_reranker=False)
    eng.analyst = type("FakeAnalyst", (), {"enabled": False})()
    return eng


# ===================================================== #10 分钟线预热

def _fake_1m(n: int, base: float = 100.0):
    t0 = datetime.now().replace(second=0, microsecond=0) - timedelta(minutes=n)
    out = []
    for i in range(n):
        p = base + i * 0.01
        out.append({"ts": t0 + timedelta(minutes=i), "open": p, "high": p + 0.02,
                    "low": p - 0.02, "close": p + 0.01,
                    "volume": 1000 + i, "amount": (1000 + i) * p})
    return out


def test_warmup_fills_bars_from_history():
    eng = _engine()
    calls = []

    def fake_hist(code, period="1d", count=100):
        calls.append((code, period, count))
        return _fake_1m(200)

    orig = EE.qmt_client.get_history
    EE.qmt_client.get_history = fake_hist
    try:
        eng._warmup_bars(["300308.SZ", "300502.SZ"], download=False)
    finally:
        EE.qmt_client.get_history = orig

    assert [c[1] for c in calls] == ["1m", "1m"], f"应拉 1 分钟线，实际 {calls}"
    maxlen = STRATEGY_PARAMS["ma_long"] * 6
    for code in ("300308.SZ", "300502.SZ"):
        bars = eng._bars[code]
        assert len(bars) == maxlen, \
            f"{code} 应灌满缓冲区 {maxlen} 根，实际 {len(bars)}"
        assert len(bars) >= 60, "必须 ≥60 根，否则 on_bars 仍返回 warmup"
        assert bars[-1].ts > bars[0].ts, "时间顺序应递增"
        assert all(b.ts.second == 0 and b.ts.microsecond == 0 for b in bars), \
            "bar 时间戳须对齐到分钟（与 _aggregate_bar 的分桶口径一致）"


def test_warmup_rejects_stale_history():
    """实测踩到的坑：本地 1m 缓存可能是 6 周前的数据。

    2026-09-02 直读本地：300308.SZ 拿到 07-22 的 bar，收盘价 1060.8 vs 真实
    859.3（差 19%）。把这种数据灌进 MA/ATR/VWAP 比不预热更危险，必须拒用。
    """
    eng = _engine()
    stale_days = STALE_LIMIT + 30

    def stale_hist(code, period="1d", count=100):
        t0 = (datetime.now() - timedelta(days=stale_days)).replace(
            second=0, microsecond=0)
        return [{"ts": t0 + timedelta(minutes=i), "open": 100.0, "high": 100.1,
                 "low": 99.9, "close": 100.0, "volume": 10, "amount": 1000.0}
                for i in range(200)]

    orig_h, orig_d = EE.qmt_client.get_history, EE.qmt_client.download_history
    EE.qmt_client.get_history = stale_hist
    EE.qmt_client.download_history = lambda c, p="1m": False
    try:
        stat = eng._warmup_bars(["300308.SZ"], download=True)
    finally:
        EE.qmt_client.get_history = orig_h
        EE.qmt_client.download_history = orig_d

    assert len(eng._bars["300308.SZ"]) == 0, \
        f"陈旧数据必须被拒用，实际灌入 {len(eng._bars['300308.SZ'])} 根"
    assert stat["ready"] == 0 and stat["empty"] == 1, stat


def test_warmup_downloads_then_succeeds_when_local_missing():
    """本地无数据 → 先 download_history_data 补拉再重读（实测必须这么做）。"""
    eng = _engine()
    state = {"downloaded": False}

    def hist(code, period="1d", count=100):
        return _fake_1m(120) if state["downloaded"] else None

    def dl(code, period="1m"):
        state["downloaded"] = True
        return True

    orig_h, orig_d = EE.qmt_client.get_history, EE.qmt_client.download_history
    EE.qmt_client.get_history = hist
    EE.qmt_client.download_history = dl
    try:
        stat = eng._warmup_bars(["300308.SZ"], download=True)
    finally:
        EE.qmt_client.get_history = orig_h
        EE.qmt_client.download_history = orig_d

    assert state["downloaded"], "本地无数据时应尝试补拉"
    assert stat["downloaded"] == 1 and stat["ready"] == 1, stat
    assert len(eng._bars["300308.SZ"]) >= 60


def test_warmup_respects_time_budget():
    """补拉 ~9.75s/只，必须可被时间预算卡住，不能无限拖下去。"""
    eng = _engine()
    dl_calls = []
    orig_h, orig_d = EE.qmt_client.get_history, EE.qmt_client.download_history
    EE.qmt_client.get_history = lambda c, period="1d", count=100: None
    EE.qmt_client.download_history = lambda c, p="1m": dl_calls.append(c) or True
    try:
        stat = eng._warmup_bars(["A.SZ", "B.SZ", "C.SZ"], download=True,
                                budget_sec=0.0)
    finally:
        EE.qmt_client.get_history = orig_h
        EE.qmt_client.download_history = orig_d
    assert dl_calls == [], "预算耗尽时不应再发起补拉"
    assert stat["budget_skipped"] == 3, stat


def test_warmup_preserves_newer_live_bars():
    """预热跑在后台线程，期间主循环可能已聚合出更新的 bar，不能丢。"""
    eng = _engine()
    from core.data_models import Bar
    future = datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=5)
    eng._bars["300308.SZ"].append(Bar(ts=future, open=9, high=9, low=9, close=9))
    orig = EE.qmt_client.get_history
    EE.qmt_client.get_history = lambda c, period="1d", count=100: _fake_1m(120)
    try:
        eng._warmup_bars(["300308.SZ"], download=False)
    finally:
        EE.qmt_client.get_history = orig
    dq = eng._bars["300308.SZ"]
    assert dq[-1].ts == future and abs(dq[-1].close - 9) < 1e-9, \
        "比历史更新的实时 bar 应保留在末尾"


def test_warmup_skips_codes_that_already_have_bars():
    """已有实时聚合结果的标的不应被历史数据覆盖。"""
    eng = _engine()
    from core.data_models import Bar
    dq = eng._bars["300308.SZ"]
    for i in range(60):
        dq.append(Bar(ts=datetime.now().replace(second=0, microsecond=0),
                      open=1, high=1, low=1, close=1))
    called = []
    orig = EE.qmt_client.get_history
    EE.qmt_client.get_history = lambda c, period="1d", count=100: called.append(c)
    try:
        eng._warmup_bars(["300308.SZ"], download=False)
    finally:
        EE.qmt_client.get_history = orig
    assert called == [], "已有 ≥60 根 bar 的标的应跳过，不覆盖实时聚合结果"


def test_warmup_degrades_silently_on_failure():
    eng = _engine()
    orig = EE.qmt_client.get_history

    def boom(code, period="1d", count=100):
        raise RuntimeError("xtdata down")

    EE.qmt_client.get_history = boom
    try:
        eng._warmup_bars(["300308.SZ"], download=False)       # 不应抛出
    finally:
        EE.qmt_client.get_history = orig
    assert len(eng._bars["300308.SZ"]) == 0, "失败时退回原「现场攒 bar」行为"


def test_warmup_first_tick_does_not_inject_cumulative_volume():
    """预热后首个实时 tick 仍必须记 0 增量，避免把当日累计量灌进 bar。"""
    eng = _engine()
    orig = EE.qmt_client.get_history
    EE.qmt_client.get_history = lambda c, period="1d", count=100: _fake_1m(120)
    try:
        eng._warmup_bars(["300308.SZ"], download=False)
    finally:
        EE.qmt_client.get_history = orig
    assert "300308.SZ" not in eng._last_cum_vol, \
        "预热不应预置累计量锚点"
    d_vol, d_amt = eng._volume_delta(
        "300308.SZ", type("T", (), {"volume": 512_000, "amount": 9.9e8})())
    assert (d_vol, d_amt) == (0, 0.0), \
        f"首个 tick 必须记 0 增量（否则 volume_surge 因子失真），实际 {(d_vol, d_amt)}"


# ===================================================== #11 tick 新鲜度

class _FakeXtdata:
    def __init__(self):
        self.full_tick_calls = []

    def get_full_tick(self, codes):
        self.full_tick_calls.append(list(codes))
        return {c: {"lastPrice": 55.0, "lastClose": 54.0, "open": 54.5,
                    "high": 55.5, "low": 54.0, "volume": 10, "amount": 550.0}
                for c in codes}


def _xtd_client_with_cache(pushed_at_offset: float):
    """构造一个绕过 __init__ 探活的 _XtdClient，注入指定新鲜度的 push 缓存。"""
    cl = QC._XtdClient.__new__(QC._XtdClient)
    import threading
    cl.xtdata = _FakeXtdata()
    cl.subscribed = set()
    cl._lock = threading.RLock()
    cl._push_cache = {"300308.SZ": {
        "lastPrice": 130.0, "lastClose": 129.0, "open": 129.5, "high": 131.0,
        "low": 129.0, "volume": 100, "amount": 13000.0,
        "_pushed_at": time.time() - pushed_at_offset,
    }}
    return cl


def test_fresh_push_cache_is_used_without_snapshot_call():
    cl = _xtd_client_with_cache(pushed_at_offset=1.0)
    out = cl.get_ticks(["300308.SZ"])
    assert abs(out["300308.SZ"]["lastPrice"] - 130.0) < 1e-9, \
        "新鲜的 push 缓存应直接使用"
    assert cl.xtdata.full_tick_calls == [], "新鲜时不应触发快照兜底"


def test_stale_push_cache_falls_back_to_snapshot():
    stale = QC._XtdClient.PUSH_STALE_SEC + 10
    cl = _xtd_client_with_cache(pushed_at_offset=stale)
    out = cl.get_ticks(["300308.SZ"])
    assert cl.xtdata.full_tick_calls == [["300308.SZ"]], \
        "陈旧缓存必须降级走 get_full_tick（原实现无限期复用旧价，" \
        "而止损/峰值/总资产全依赖该价格）"
    assert abs(out["300308.SZ"]["lastPrice"] - 55.0) < 1e-9, \
        "应采用快照返回的新价，而不是缓存里的 130.0"


def test_stale_threshold_is_generous_enough_for_normal_loop():
    """阈值须远大于主循环间隔，避免正常运行时反复退化为快照拉取。"""
    from config.settings import REFRESH_INTERVAL
    assert QC._XtdClient.PUSH_STALE_SEC >= REFRESH_INTERVAL * 10, \
        "新鲜度阈值过紧会让每轮都退化成 get_full_tick"


# ===================================================== #9 Web Storage 单例

def test_web_storage_is_singleton():
    import web.routes as WR
    WR._STORAGE = None
    try:
        a = WR._storage()
        b = WR._storage()
        assert a is b, "Web 层应共享同一个 Storage（原每请求新建且从不 close）"
    finally:
        if WR._STORAGE is not None:
            WR._STORAGE.close()
        WR._STORAGE = None


def test_sse_payload_built_once():
    """回归防护：SSE 里曾把 payload 字典构建两遍（第一个立刻被覆盖）。"""
    src = (ROOT / "web" / "routes.py").read_text(encoding="utf-8")
    body = src[src.index("def api_stream"):]
    assert body.count("payload = {") == 1, \
        f"api_stream 中 payload 应只构建一次，实际 {body.count('payload = {')} 次"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

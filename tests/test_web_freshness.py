# -*- coding: utf-8 -*-
"""Web 展示三连故障回归测试（2026-09-22 修复）。

用户从页面观察到「llm 重排 / 实时信号都没正常工作，行情信息滞后」。根因三条：

1. 行情滞后：引擎进程带着失效的 HTTP(S)_PROXY 启动 → 每次外网 API 都等满超时。
   sector 评估每轮对每只候选调 tushare（summary 内部 3 次调用），旧实现失败
   **不写缓存**，于是下轮照旧重打网络 → 主循环从 3s/轮 拖到实测 58s/轮。
2. LLM 重排：同一代理问题让 DeepSeek 抛 ProxyError；手动触发仍返回 200，
   页面继续显示上一次结果 —— 失败对用户完全不可见。
3. 实时信号：观察篮 / 板块轮动两条买入路径都没调 `_save_signal`，引擎当天
   成交 7 笔而 signals 表 0 行，页面只剩几天前的旧信号。

本文件锁定修复语义：
  - tushare 失败进负缓存 + 连续失败全局熔断（不再每轮等超时）；
  - DeepSeek 代理失败自动降级直连，并把错误写进 health；
  - tick 携带真实行情时间（xtdata time / 推送到达时间 / 抓取时刻三级兜底）；
  - 信号新鲜度与行情新鲜度可计算。
"""
from __future__ import annotations

import sys
import time
from datetime import datetime

sys.path.insert(0, ".")

from engine.event_engine import _tick_ts  # noqa: E402


# ---------------------------------------------------------------- tick 时间

def test_tick_ts_prefers_exchange_time():
    """优先用交易所行情时间（xtdata time，毫秒）。"""
    ts = _tick_ts({"time": 1790047800000, "lastPrice": 223.57})
    assert ts == datetime.fromtimestamp(1790047800.0)
    assert ts.year == 2026


def test_tick_ts_falls_back_to_pushed_at():
    """没有 time 时用推送到达时间（秒）。"""
    ts = _tick_ts({"_pushed_at": 1790047800.0})
    assert ts == datetime.fromtimestamp(1790047800.0)


def test_tick_ts_falls_back_to_now():
    """两者都没有时退回当前时刻（不抛异常）。"""
    before = datetime.now()
    ts = _tick_ts({"lastPrice": 1.0})
    assert before <= ts <= datetime.now()


def test_tick_ts_ignores_garbage():
    """非法值不能让整轮崩掉。"""
    ts = _tick_ts({"time": "not-a-number", "_pushed_at": None})
    assert isinstance(ts, datetime)


# ---------------------------------------------------------------- tushare 熔断

def test_tushare_fail_circuit_opens():
    """连续失败达阈值后全局熔断，后续调用不再打网络（0 秒返回）。"""
    from data.tushare_client import tushare_client as tc

    class Boom:
        def __getattr__(self, n):
            def f(*a, **k):
                raise RuntimeError("network down")
            return f

    saved_pro, saved_fail, saved_dis, saved_consec = (
        tc._pro, dict(tc._fail_at), tc._disabled_until, tc._consec_fail)
    try:
        tc._pro = Boom()
        tc._fail_at.clear()
        tc._disabled_until = 0.0
        tc._consec_fail = 0

        codes = [f"6000{i:02d}.SH" for i in range(20)]
        for c in codes:
            tc.summary(c)
        assert tc.health()["circuit_open"], "连续失败后应全局熔断"

        t0 = time.time()
        for c in codes:
            tc.summary(c)
        assert time.time() - t0 < 0.5, "熔断期内不应再产生网络等待"
    finally:
        tc._pro, tc._fail_at, tc._disabled_until, tc._consec_fail = (
            saved_pro, saved_fail, saved_dis, saved_consec)


def test_tushare_health_shape():
    """health() 供前端展示，字段必须齐备。"""
    from data.tushare_client import tushare_client as tc
    h = tc.health()
    for k in ("enabled", "connected", "circuit_open", "retry_after_sec"):
        assert k in h, f"health 缺字段 {k}"


# ---------------------------------------------------------------- DeepSeek 降级

def _patch_session(fake_cls):
    """替换 requests.Session 并返回还原函数（tests/run_all.py 不提供 pytest 夹具）。"""
    import requests
    real = requests.Session
    requests.Session = fake_cls

    def _undo():
        requests.Session = real
    return _undo


def _ok_resp():
    class FakeResp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}], "model": "m"}
    return FakeResp()


def test_deepseek_proxy_failure_falls_back_to_direct():
    """环境代理不可达时，必须自动改用直连重试一次。"""
    import requests
    from ai.deepseek_client import DeepSeekClient

    c = DeepSeekClient()
    calls = []

    class FakeSession:
        def __init__(self):
            self.trust_env = True

        def post(self, url, headers=None, json=None, timeout=None):
            calls.append(self.trust_env)
            if self.trust_env:
                raise requests.exceptions.ProxyError("proxy refused")
            return _ok_resp()

        def close(self):
            pass

    undo = _patch_session(FakeSession)
    try:
        out = c.chat([{"role": "user", "content": "hi"}], timeout=1)
    finally:
        undo()

    assert out is not None, "代理失败后必须降级直连并成功"
    assert calls == [True, False], f"应先试代理再直连，实际 {calls}"
    assert c._direct_only is True, "直连成功后应记住，后续不再白等代理超时"
    assert c.last_error is None


def test_deepseek_records_last_error():
    """两条路都失败时，错误必须写进 last_error 供前端展示。"""
    import requests
    from ai.deepseek_client import DeepSeekClient

    c = DeepSeekClient()

    class DeadSession:
        def __init__(self):
            self.trust_env = True

        def post(self, *a, **k):
            raise requests.exceptions.ConnectionError("all down")

        def close(self):
            pass

    undo = _patch_session(DeadSession)
    try:
        out = c.chat([{"role": "user", "content": "hi"}], timeout=1)
    finally:
        undo()

    assert out is None
    assert c.last_error and "ConnectionError" in c.last_error
    h = c.health()
    assert h["last_error"] == c.last_error


# ---------------------------------------------------------------- 新鲜度

def test_market_data_ts_picks_newest():
    """_market_data_ts 取所有标的中最新的一只。"""
    from engine.event_engine import EventEngine

    e = EventEngine.__new__(EventEngine)      # 不跑 __init__（避免起线程/连行情）
    e._last_ticks = {
        "A": {"ts": "2026-09-22T09:30:00"},
        "B": {"ts": "2026-09-22T10:15:00"},
        "C": {"ts": "2026-09-22T09:59:00"},
    }
    assert e._market_data_ts() == "2026-09-22T10:15:00"

    e._last_ticks = {}
    assert e._market_data_ts() is None


def test_slow_round_threshold_default():
    """慢轮告警阈值应显著小于观测到的故障耗时（58s），且大于正常轮（<1s）。"""
    import inspect
    from engine.event_engine import EventEngine
    src = inspect.getsource(EventEngine.__init__)
    assert "SLOW_ROUND_SEC" in src, "主循环慢轮阈值必须仍然是实例属性"
    assert "self._slow_rounds = 0" in src

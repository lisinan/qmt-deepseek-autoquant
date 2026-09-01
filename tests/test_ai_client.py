# -*- coding: utf-8 -*-
"""ai/openrouter_client.py 与 ai/analyst.py 单元测试。

无 key / 无网络时全部跳过，不报错。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai.analyst import AIAnalyst, _build_user_prompt, _indicators_hash, \
    _parse_confidence, _parse_stance
from ai.openrouter_client import OpenRouterClient


def test_disabled_without_key():
    c = OpenRouterClient(api_key="")
    assert not c.enabled
    assert c.chat([{"role": "user", "content": "hi"}]) is None


def test_parse_stance_variants():
    assert _parse_stance("bullish") == "bullish"
    assert _parse_stance("看多") == "bullish"
    assert _parse_stance("buy") == "bullish"
    assert _parse_stance("bearish") == "bearish"
    assert _parse_stance("看空") == "bearish"
    assert _parse_stance("sell") == "bearish"
    assert _parse_stance("中性") == "neutral"
    assert _parse_stance("") == "neutral"
    assert _parse_stance(None) == "neutral"


def test_parse_confidence_clamp():
    assert _parse_confidence(0.5) == 0.5
    assert _parse_confidence(2.0) == 1.0
    assert _parse_confidence(-2.0) == -1.0
    assert _parse_confidence("abc") == 0.0


def test_indicators_hash_deterministic():
    a = _indicators_hash({"ma5": 1.0, "rsi": 50.0})
    b = _indicators_hash({"rsi": 50.0, "ma5": 1.0})
    assert a == b


def test_user_prompt_includes_indicators():
    ind = {"ma5": 12.5, "ma10": 12.0, "ma20": 11.5, "rsi": 55.0,
           "macd_hist": 0.1, "kdj_j": 60.0, "vwap": 12.3,
           "close": 12.8, "change_pct": 1.5, "score": 5.5}
    prompt = _build_user_prompt("300308.SZ", "中际旭创", ind,
                                {"index_name": "创业板指", "index_change_pct": 0.5})
    assert "中际旭创" in prompt
    assert "MA5" in prompt
    assert "RSI14" in prompt
    assert "创业板指" in prompt


def test_analyst_disabled_without_key():
    a = AIAnalyst(client=OpenRouterClient(api_key=""))
    assert not a.enabled
    assert a.analyze("x", "x", {"ma5": 1.0}) is None


def test_analyst_cache_hit():
    """模拟两次相同输入，第二次应命中缓存。"""
    class _StubClient:
        enabled = True
        model = "stub"
        calls = 0
        def chat_json(self, messages, **kw):
            self.calls += 1
            return {"stance": "bullish", "confidence": 0.3,
                    "summary": "ok", "risks": "x"}
    stub = _StubClient()
    a = AIAnalyst(client=stub, cache_ttl_sec=60)
    ind = {"ma5": 1.0, "ma10": 2.0, "ma20": 3.0, "rsi": 50.0,
           "macd_hist": 0.1, "kdj_j": 60.0, "vwap": 1.5,
           "close": 2.0, "change_pct": 0.5, "score": 5.0}
    r1 = a.analyze("x", "x", ind)
    assert r1 is not None
    assert not r1.cached
    r2 = a.analyze("x", "x", ind)
    assert r2 is not None
    assert r2.cached
    assert stub.calls == 1   # 第二次走缓存
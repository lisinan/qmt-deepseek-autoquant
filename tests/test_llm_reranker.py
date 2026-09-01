# -*- coding: utf-8 -*-
"""ai/llm_reranker.py 单元测试。"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai.llm_reranker import LLMReranker, _build_user_prompt, RerankResult
from strategy.sector_scorer import SectorScore, StockRecommendation


def _mk_sector(name, label, heat=5.0, avg_chg=1.0, n_up=3, n=4,
               best_code="x.SZ", best_name="x", best_chg=2.0):
    return SectorScore(
        sector=name, label=label, heat_score=heat,
        avg_change_pct=avg_chg, avg_volume_ratio=1.2,
        strength=n_up / max(1, n), n_stocks=n, n_up=n_up,
        best_code=best_code, best_name=best_name,
        best_change_pct=best_chg,
    )


def _mk_rec(code, name="x", composite=5.0, sector="光模块",
            sector_label="光模块/光器件", heat=5.0, tech=5.0,
            fund=5.0, pe=50.0, roe=15.0, chg=1.0):
    return StockRecommendation(
        ts=datetime.now(), code=code, name=name,
        sector=sector, sector_label=sector_label,
        composite=composite, heat_contribution=heat,
        tech_score=tech, fundamental_score=fund,
        pe=pe, roe=roe, change_pct=chg,
    )


def test_disabled_without_key():
    from ai import llm_reranker as mod
    # property 不能 setattr；改用替换 module 级 client 对象
    class FakeDisabled:
        enabled = False
        model = "fake"
        def chat_json(self, *a, **kw): return None
    orig_ds = mod.deepseek
    orig_or = mod.openrouter
    mod.deepseek = FakeDisabled()
    mod.openrouter = FakeDisabled()
    try:
        r = LLMReranker()
        assert not r.enabled
        recs = [_mk_rec("x.SZ"), _mk_rec("y.SZ")]
        heat = {"光模块": _mk_sector("光模块", "光模块/光器件")}
        result = r.rerank(recs, heat, {})
        assert result is None
    finally:
        mod.deepseek = orig_ds
        mod.openrouter = orig_or


def test_build_prompt_includes_sector_and_recs():
    recs = [_mk_rec("300308.SZ", "中际旭创", pe=94, roe=39)]
    heat = {"光模块": _mk_sector("光模块", "光模块/光器件", heat=7.5)}
    prompt = _build_user_prompt(recs, heat, {"大盘": "创业板 -3.2%"})
    assert "300308.SZ" in prompt
    assert "光模块" in prompt
    assert "heat=7.5" in prompt
    assert "PE=94" in prompt
    assert "ROE=39" in prompt
    assert "大盘" in prompt


def test_rerank_returns_none_on_empty():
    from ai import llm_reranker as mod
    r = LLMReranker()
    # 即使 enabled，没有 recs 也返回 None
    if r.enabled:
        # mock 不让 chat_json 被调用
        assert r.rerank([], {}, {}) is None


def test_snapshot():
    r = LLMReranker()
    snap = r.snapshot()
    assert "enabled" in snap
    assert "last_call_ts" in snap
    assert "cache_size" in snap


def test_rerank_with_stub_client():
    """用 stub client 模拟 LLM 返回，验证解析逻辑。"""
    class StubClient:
        enabled = True
        model = "stub"
        def chat_json(self, messages, **kw):
            return {
                "macro_view": "bullish",
                "analysis": "板块强势，建议关注光模块",
                "ranked_codes": ["300308.SZ", "688256.SH"],   # 故意倒序
                "adjustments": {
                    "300308.SZ": "+0.5 ROE 39% 强支撑",
                    "688256.SH": "-0.5 PE 过高",
                },
            }
    # 替换模块里的 deepseek / openrouter 为 stub
    from ai import llm_reranker as mod
    orig_ds = mod.deepseek
    orig_or = mod.openrouter
    mod.deepseek = StubClient()
    mod.openrouter = StubClient()
    try:
        r = LLMReranker()
        recs = [_mk_rec("300308.SZ"), _mk_rec("688256.SH"), _mk_rec("002230.SZ")]
        heat = {"光模块": _mk_sector("光模块", "光模块/光器件")}
        result = r.rerank(recs, heat, {})
        assert result is not None
        assert isinstance(result, RerankResult)
        assert result.macro_view == "bullish"
        # 验证 ranked_codes 顺序：LLM 输出在前，剩余补齐在后
        assert result.ranked_codes[:2] == ["300308.SZ", "688256.SH"]
        assert "002230.SZ" in result.ranked_codes
        assert "300308.SZ" in result.adjustments
    finally:
        mod.deepseek = orig_ds
        mod.openrouter = orig_or


def test_rerank_filters_invalid_codes():
    """LLM 可能在 ranked_codes 里塞入不存在的代码，应被剔除。"""
    class StubClient:
        enabled = True
        model = "stub"
        def chat_json(self, messages, **kw):
            return {
                "macro_view": "neutral",
                "analysis": "",
                "ranked_codes": ["XXXXXX.SZ", "300308.SZ"],   # XXXXXX 不存在
                "adjustments": {},
            }
    from ai import llm_reranker as mod
    orig_ds = mod.deepseek
    orig_or = mod.openrouter
    mod.deepseek = StubClient()
    mod.openrouter = StubClient()
    try:
        r = LLMReranker()
        recs = [_mk_rec("300308.SZ"), _mk_rec("688256.SH")]
        heat = {"光模块": _mk_sector("光模块", "光模块/光器件")}
        result = r.rerank(recs, heat, {})
        assert result is not None
        assert "XXXXXX.SZ" not in result.ranked_codes
        assert "300308.SZ" in result.ranked_codes
        # 688256.SH 被补齐
        assert "688256.SH" in result.ranked_codes
    finally:
        mod.deepseek = orig_ds
        mod.openrouter = orig_or


def test_cache_hit():
    """相同输入二次调用应命中缓存。"""
    class StubClient:
        enabled = True
        model = "stub"
        calls = 0
        def chat_json(self, messages, **kw):
            self.calls += 1
            return {
                "macro_view": "bullish",
                "analysis": "x",
                "ranked_codes": ["300308.SZ"],
                "adjustments": {},
            }
    stub = StubClient()
    from ai import llm_reranker as mod
    orig_ds = mod.deepseek
    mod.deepseek = stub
    try:
        r = LLMReranker(cache_ttl_sec=60)
        recs = [_mk_rec("300308.SZ"), _mk_rec("688256.SH")]
        heat = {"光模块": _mk_sector("光模块", "光模块/光器件")}
        r1 = r.rerank(recs, heat, {})
        r2 = r.rerank(recs, heat, {})
        assert stub.calls == 1
        assert r2.cached is True
    finally:
        mod.deepseek = orig_ds
# -*- coding: utf-8 -*-
"""
AI 分析师

把策略产出的指标 + 行情上下文打包成紧凑 prompt，让 OpenRouter 免费模型
输出结构化 JSON（stance / confidence / summary / risks），用于：
- 在 Signal 上附加 ai_comment（人类可读）
- ai_confidence 作为额外过滤（强 bearish + 高 confidence → 抑制买入）

无 API key / 网络异常时所有方法返回 None，主策略不阻塞。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime
from typing import List, Optional

from config.settings import AI_CACHE_TTL_SEC, AI_ENABLED
from core.data_models import AIAnalysis
from ai.deepseek_client import DeepSeekClient, deepseek
from ai.openrouter_client import OpenRouterClient, openrouter

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """你是 A 股量化交易助手，严格只输出 JSON，不要任何解释。
JSON 字段：
- stance: "bullish" / "bearish" / "neutral"
- confidence: -1.0 到 1.0 的浮点数（看多正，看空负）
- summary: 中文一句话结论（<= 50 字）
- risks: 中文一句话风险提示（<= 50 字）

规则：
1. 多指标冲突时给 neutral 而非二选一
2. confidence 的绝对值不要轻易超过 0.7
3. 没有明确信号就给 neutral + confidence 接近 0
"""


def _build_user_prompt(code: str, name: str, ind: dict,
                       market_ctx: dict) -> str:
    parts = [f"标的：{name}({code})"]
    if market_ctx:
        parts.append(f"大盘：{market_ctx.get('index_name', '')} "
                     f"涨跌幅={market_ctx.get('index_change_pct', 0):.2f}%, "
                     f"趋势={market_ctx.get('index_trend', '未知')}")
    keys = [
        ("ma5", "MA5"), ("ma10", "MA10"), ("ma20", "MA20"),
        ("rsi", "RSI14"),
        ("macd_dif", "MACD-DIF"), ("macd_dea", "MACD-DEA"), ("macd_hist", "MACD-HIST"),
        ("kdj_j", "KDJ-J"),
        ("boll_upper", "BOLL上轨"), ("boll_lower", "BOLL下轨"),
        ("atr", "ATR14"), ("vwap", "VWAP"),
        ("change_pct", "当日涨跌%"), ("score", "策略评分"),
    ]
    for k, label in keys:
        v = ind.get(k)
        if v is None:
            continue
        parts.append(f"{label}={v:.3f}" if isinstance(v, float) else f"{label}={v}")
    parts.append("请输出 JSON。")
    return "\n".join(parts)


def _indicators_hash(ind: dict) -> str:
    blob = json.dumps(ind, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.md5(blob.encode("utf-8")).hexdigest()


def _parse_stance(s: str) -> str:
    s = (s or "").strip().lower()
    if s in ("bullish", "bearish", "neutral"):
        return s
    # 兼容中文/其他写法
    if "多" in s or "涨" in s or "buy" in s:
        return "bullish"
    if "空" in s or "跌" in s or "sell" in s:
        return "bearish"
    return "neutral"


def _parse_confidence(v) -> float:
    try:
        c = float(v)
        return max(-1.0, min(1.0, c))
    except Exception:
        return 0.0


class AIAnalyst:
    def __init__(self, client=None,
                 cache_ttl_sec: int = None):
        # 优先级：DeepSeek（直连）> OpenRouter > 注入的 client
        if client is not None:
            self._client = client
        elif deepseek.enabled:
            self._client = deepseek
        else:
            self._client = openrouter
        self._ttl = cache_ttl_sec if cache_ttl_sec is not None else AI_CACHE_TTL_SEC
        self._cache: dict = {}     # key=(code, hash) -> (AIAnalysis, ts)

    @property
    def enabled(self) -> bool:
        return AI_ENABLED and self._client.enabled

    @property
    def client(self):
        return self._client

    def analyze(self, code: str, name: str, indicators: dict,
                market_ctx: dict = None,
                recent_bars: List[dict] = None) -> Optional[AIAnalysis]:
        """
        indicators: 必填，至少含 ma5/ma10/ma20/rsi/macd_hist/vwap/score/change_pct
        market_ctx: 可选
        recent_bars: 当前未使用（保留字段）

        成功返回 AIAnalysis；失败/无 key 返回 None。
        """
        if not self.enabled:
            return None
        if not indicators:
            return None
        h = _indicators_hash(indicators)
        key = (code, h)
        cached = self._cache.get(key)
        now = time.time()
        if cached:
            ai, ts = cached
            if now - ts < self._ttl:
                ai.cached = True
                return ai

        prompt_user = _build_user_prompt(code, name, indicators, market_ctx or {})
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt_user},
        ]
        parsed = self._client.chat_json(messages, max_tokens=400, temperature=0.2)
        if not parsed or not isinstance(parsed, dict):
            return None

        ai = AIAnalysis(
            ts=datetime.now(),
            code=code,
            model=self._client.model,
            summary=str(parsed.get("summary", ""))[:200],
            stance=_parse_stance(parsed.get("stance", "")),
            confidence=_parse_confidence(parsed.get("confidence", 0)),
            risks=str(parsed.get("risks", ""))[:200],
            raw=json.dumps(parsed, ensure_ascii=False),
            cached=False,
        )
        self._cache[key] = (ai, now)
        return ai

    def clear_cache(self):
        self._cache.clear()


# 模块级单例
ai_analyst = AIAnalyst()
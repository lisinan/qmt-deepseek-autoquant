# -*- coding: utf-8 -*-
"""
LLM 选股重排序器

目标：
  - 把当前产业链推荐池（量化打分结果）送进 LLM
  - LLM 综合考虑宏观风险 + 板块轮动 + 基本面矛盾点
  - 输出：解释 + 重新排序 + 评分调整理由

输入：
  - recommendations: List[StockRecommendation]
  - sector_heat: Dict[sector, SectorScore]
  - market_ctx: dict (大盘涨跌幅等)

输出：
  - RerankResult dataclass，含：
    - macro_view: bullish/bearish/neutral
    - analysis: 50~100 字总结
    - ranked_codes: 重排序后的代码列表
    - adjustments: {code: reason}
    - model: 使用的模型名
    - ts: 时间戳

设计原则：
  - 无 LLM key 时返回 None，主策略不受影响
  - 主推荐池 + LLM 重排序结果并存，用户可在 UI 切换
  - LLM 调用走 AIAnalyst 同款（优先 DeepSeek，回退 OpenRouter）
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Dict, Optional

from ai.analyst import ai_analyst
from ai.deepseek_client import deepseek
from ai.openrouter_client import openrouter

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """你是 A 股 AI 板块量化分析师。综合量化打分与宏观风险，给出有逻辑的选股建议。

【输出要求】严格 JSON，不要任何解释：
{
  "macro_view": "bullish|bearish|neutral",
  "analysis": "<= 80 字中文总结",
  "ranked_codes": ["code1", "code2", "code3", ...],   // 重排序后的代码（不超过原数组长度）
  "adjustments": {
    "code1": "+0.5 因为光模块板块 +5.1% 强势；PE 偏高但 ROE 39% 支撑",
    "code2": "-0.3 因为 PE > 200 估值过热；建议减仓"
  }
}

【约束】
- ranked_codes 必须从输入 codes 选，不能新增
- 调整幅度建议在 [-1.0, +1.0]
- 不要凭空给出输入里没有的代码
- 给出具体板块名/PE/ROE 等数据支撑，不要空话
"""


def _build_user_prompt(recommendations, sector_heat, market_ctx) -> str:
    parts = ["【市场环境】"]
    if market_ctx:
        for k, v in market_ctx.items():
            parts.append(f"- {k}: {v}")
    else:
        parts.append("- (无大盘上下文)")

    parts.append("\n【产业链热度】")
    for sec_name, sc in sector_heat.items():
        parts.append(
            f"- {sc.label} ({sec_name}): heat={sc.heat_score:.1f}/10 "
            f"平均涨幅={sc.avg_change_pct:+.2f}% 上涨家数={sc.n_up}/{sc.n_stocks} "
            f"最强: {sc.best_code} {sc.best_name} ({sc.best_change_pct:+.2f}%)"
        )

    parts.append("\n【当前推荐池（按综合分降序）】")
    for i, r in enumerate(recommendations, 1):
        parts.append(
            f"{i}. {r.code} {r.name} 行业={r.sector_label} "
            f"综合={r.composite:.2f} (热={r.heat_contribution:.1f}+技={r.tech_score:.1f}+基={r.fundamental_score:.1f}) "
            f"PE={r.pe} ROE={r.roe} 当日={r.change_pct:+.2f}%"
        )

    parts.append("\n请基于上述量化结果 + 板块联动 + 估值合理性，给出 LLM 重排序。")
    return "\n".join(parts)


@dataclass
class RerankResult:
    ts: datetime
    model: str = ""
    macro_view: str = "neutral"
    analysis: str = ""
    ranked_codes: list = field(default_factory=list)
    adjustments: dict = field(default_factory=dict)
    raw: str = ""
    cached: bool = False


class LLMReranker:
    def __init__(self, cache_ttl_sec: int = 300):
        self._ttl = cache_ttl_sec
        self._cache: Dict[str, tuple] = {}   # (key) -> (RerankResult, ts)
        self._lock = threading.RLock()
        self._last_call_ts: float = 0
        self._last_error: str = ""

    @property
    def enabled(self) -> bool:
        return (deepseek.enabled or openrouter.enabled)

    def rerank(self,
               recommendations,
               sector_heat: Dict,
               market_ctx: Optional[dict] = None) -> Optional[RerankResult]:
        """调用 LLM 重排序。无 key / 失败时返回 None。"""
        if not self.enabled:
            return None
        if not recommendations:
            return None

        market_ctx = market_ctx or {}
        codes = [r.code for r in recommendations]
        # 缓存键：基于 codes + sector_heat 的关键数字
        heat_sig = {k: round(v.heat_score, 1) for k, v in sector_heat.items()}
        cache_key = json.dumps(
            {"codes": codes, "heat": heat_sig, "ctx": market_ctx},
            sort_keys=True, ensure_ascii=False,
        )
        with self._lock:
            cached = self._cache.get(cache_key)
            now = time.time()
            if cached and now - cached[1] < self._ttl:
                rr = cached[0]
                rr.cached = True
                return rr

        prompt = _build_user_prompt(recommendations, sector_heat, market_ctx)
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        client = deepseek if deepseek.enabled else openrouter
        parsed = client.chat_json(messages, max_tokens=600, temperature=0.2)
        self._last_call_ts = time.time()
        if not parsed or not isinstance(parsed, dict):
            self._last_error = "chat_json 返回 None / 非 dict"
            return None

        # 校验 ranked_codes 必须在 codes 里
        ranked = parsed.get("ranked_codes") or []
        valid_codes = [c for c in ranked if c in codes]
        # LLM 可能漏掉代码，补齐未排序的
        for c in codes:
            if c not in valid_codes:
                valid_codes.append(c)
        if not valid_codes:
            self._last_error = "ranked_codes 为空"
            return None

        result = RerankResult(
            ts=datetime.now(),
            model=client.model,
            macro_view=str(parsed.get("macro_view") or "neutral"),
            analysis=str(parsed.get("analysis") or "")[:200],
            ranked_codes=valid_codes,
            adjustments=parsed.get("adjustments") or {},
            raw=json.dumps(parsed, ensure_ascii=False),
            cached=False,
        )
        with self._lock:
            self._cache[cache_key] = (result, now)
        logger.info("LLM rerank 完成: %s → %s (adj=%d)",
                    codes, valid_codes, len(result.adjustments))
        return result

    @property
    def last_call_ts(self) -> float:
        return self._last_call_ts

    @property
    def last_error(self) -> str:
        return self._last_error

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "enabled": self.enabled,
                "last_call_ts": self.last_call_ts,
                "last_error": self._last_error,
                "cache_size": len(self._cache),
            }


# 模块级单例
llm_reranker = LLMReranker()
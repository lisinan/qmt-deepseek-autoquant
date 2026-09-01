# -*- coding: utf-8 -*-
"""
OpenRouter HTTP 客户端（基于 requests，不依赖 openai 包）

特点：
- 主模型 + 免费 fallback 链（OPENROUTER_FREE_FALLBACKS）
- 失败优雅返回 None，绝不抛异常
- 支持 JSON mode（response_format={"type":"json_object"}）
- 内置 list_free_models() 用于诊断
"""
from __future__ import annotations

import json
import logging
import time
from typing import List, Optional

import requests

from config.settings import (
    OPENROUTER_API_KEY, OPENROUTER_BASE_URL, OPENROUTER_MODEL,
    OPENROUTER_FREE_FALLBACKS, OPENROUTER_TIMEOUT,
)

logger = logging.getLogger(__name__)


class OpenRouterClient:
    def __init__(self, api_key: str = None, base_url: str = None,
                 model: str = None):
        self.api_key = (api_key if api_key is not None else OPENROUTER_API_KEY).strip()
        self.base_url = (base_url or OPENROUTER_BASE_URL).rstrip("/")
        self.model = model or OPENROUTER_MODEL
        self._models_cache: List[str] = []
        self._models_cached_at: float = 0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    # ---------- 公共方法 ----------

    def chat(self, messages: List[dict], *, model: str = None,
             max_tokens: int = None, temperature: float = 0.3,
             timeout: float = None,
             response_format_json: bool = False) -> Optional[dict]:
        """
        返回 {"content": str, "model": str, "usage": dict} 或 None（失败）。
        主模型失败时按 OPENROUTER_FREE_FALLBACKS 自动尝试。
        """
        if not self.enabled:
            logger.debug("OpenRouter 未配置 API key，跳过调用")
            return None
        candidates = [model or self.model] + [
            m for m in OPENROUTER_FREE_FALLBACKS if m != (model or self.model)
        ]
        body0 = {
            "max_tokens": max_tokens or 600,
            "temperature": temperature,
        }
        if response_format_json:
            body0["response_format"] = {"type": "json_object"}

        last_err = ""
        for m in candidates:
            body = dict(body0)
            body["model"] = m
            body["messages"] = messages
            try:
                r = self._post(body, timeout or OPENROUTER_TIMEOUT)
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                logger.debug("OpenRouter %s 网络异常: %s", m, last_err)
                continue
            if r is None:
                last_err = "http error"
                continue
            try:
                data = r.json()
                content = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})
                return {"content": content, "model": m, "usage": usage,
                        "raw": data}
            except Exception as e:
                last_err = f"parse: {e}"
                logger.debug("OpenRouter %s 响应解析失败: %s", m, e)
                continue
        logger.info("OpenRouter 全部候选失败：%s", last_err)
        return None

    def chat_json(self, messages: List[dict], **kw) -> Optional[dict]:
        """chat + response_format_json + 解析 JSON 字符串。"""
        kw["response_format_json"] = True
        resp = self.chat(messages, **kw)
        if not resp:
            return None
        content = (resp.get("content") or "").strip()
        # 兼容 LLM 把 JSON 包在 ```json ... ``` 里
        if content.startswith("```"):
            content = content.strip("`")
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()
        try:
            return json.loads(content)
        except Exception as e:
            logger.debug("chat_json 解析失败: %s | content=%s", e, content[:200])
            return None

    def list_free_models(self, force: bool = False) -> List[str]:
        """GET /models，过滤 ":free"，缓存 5 分钟。"""
        if not self.enabled:
            return []
        if not force and self._models_cache and time.time() - self._models_cached_at < 300:
            return list(self._models_cache)
        try:
            r = requests.get(
                f"{self.base_url}/models",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=15,
            )
            if r.status_code != 200:
                logger.warning("list_models HTTP %s", r.status_code)
                return []
            data = r.json()
            ids = []
            for m in (data.get("data") or []):
                mid = m.get("id") or ""
                if ":free" in mid:
                    ids.append(mid)
            self._models_cache = sorted(set(ids))
            self._models_cached_at = time.time()
            return self._models_cache
        except Exception as e:
            logger.warning("list_models 失败: %s", e)
            return []

    # ---------- 内部 ----------

    def _post(self, body: dict, timeout: float):
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        for attempt in range(3):
            try:
                r = requests.post(url, headers=headers, json=body, timeout=timeout)
                if r.status_code == 200:
                    return r
                # 429/5xx 重试
                if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                logger.debug("OpenRouter HTTP %s: %s", r.status_code, r.text[:200])
                return None
            except (requests.Timeout, requests.ConnectionError) as e:
                if attempt < 2:
                    time.sleep(0.3 * (attempt + 1))
                    continue
                raise


# 模块级单例
openrouter = OpenRouterClient()
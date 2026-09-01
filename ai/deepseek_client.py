# -*- coding: utf-8 -*-
"""
DeepSeek 直连 API 客户端（OpenAI 兼容协议）

与 OpenRouterClient 接口一致，可在 AIAnalyst 中替换使用。
- 端点：https://api.deepseek.com（v1/chat/completions）
- 模型：deepseek-chat (V3) / deepseek-reasoner (R1)
- 无 free tier，但 token 价格低；用户已提供 key
"""
from __future__ import annotations

import json
import logging
import time
from typing import List, Optional

import requests

from config.settings import (
    DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL,
)

logger = logging.getLogger(__name__)


class DeepSeekClient:
    def __init__(self, api_key: str = None, base_url: str = None,
                 model: str = None, timeout: float = 60.0):
        self.api_key = (api_key if api_key is not None else DEEPSEEK_API_KEY).strip()
        self.base_url = (base_url or DEEPSEEK_BASE_URL).rstrip("/")
        self.model = model or DEEPSEEK_MODEL
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def chat(self, messages: List[dict], *, model: str = None,
             max_tokens: int = None, temperature: float = 0.3,
             timeout: float = None,
             response_format_json: bool = False) -> Optional[dict]:
        if not self.enabled:
            return None
        body = {
            "model": model or self.model,
            "messages": messages,
            "max_tokens": max_tokens or 600,
            "temperature": temperature,
            "stream": False,
        }
        if response_format_json:
            body["response_format"] = {"type": "json_object"}

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            r = requests.post(url, headers=headers, json=body,
                              timeout=timeout or self.timeout)
            if r.status_code != 200:
                logger.warning("DeepSeek HTTP %s: %s",
                               r.status_code, r.text[:200])
                return None
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            return {"content": content, "model": data.get("model", self.model),
                    "usage": data.get("usage", {}), "raw": data}
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.warning("DeepSeek 网络异常: %s", e)
            return None
        except Exception as e:
            logger.warning("DeepSeek 调用失败: %s: %s", type(e).__name__, e)
            return None

    def chat_json(self, messages: List[dict], **kw) -> Optional[dict]:
        kw["response_format_json"] = True
        resp = self.chat(messages, **kw)
        if not resp:
            return None
        content = (resp.get("content") or "").strip()
        if content.startswith("```"):
            content = content.strip("`")
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()
        try:
            return json.loads(content)
        except Exception as e:
            logger.debug("DeepSeek chat_json 解析失败: %s | content=%s",
                         e, content[:200])
            return None


# 模块级单例
deepseek = DeepSeekClient()
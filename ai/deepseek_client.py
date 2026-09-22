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
    # 【2026-09-22】代理失效自动降级
    # 现象：引擎进程常在环境变量里带着 HTTP(S)_PROXY=127.0.0.1:7897（Clash），
    # 代理软件没开时 requests 会抛 ProxyError(WinError 10061) —— 于是 LLM 重排
    # **静默失败**（手动点按钮照样返回 200，页面却永远显示上一次的旧结果）。
    # 现在：命中代理错误时用 trust_env=False 的 Session 直连重试一次；
    # 仍失败则把错误写进 self.last_error，供 /api/llm/rerank/latest 回传前端。
    PROXY_RETRY_ONCE = True

    def __init__(self, api_key: str = None, base_url: str = None,
                 model: str = None, timeout: float = 60.0):
        self.api_key = (api_key if api_key is not None else DEEPSEEK_API_KEY).strip()
        self.base_url = (base_url or DEEPSEEK_BASE_URL).rstrip("/")
        self.model = model or DEEPSEEK_MODEL
        self.timeout = timeout
        self.last_error: Optional[str] = None
        self.last_error_ts: Optional[float] = None
        self.last_ok_ts: Optional[float] = None
        self._direct_only = False   # 上次靠直连成功 → 本次直接走直连，少等一次超时

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def health(self) -> dict:
        """供前端/诊断展示的健康状态。"""
        return {
            "enabled": self.enabled,
            "model": self.model,
            "mode": "direct" if self._direct_only else "env-proxy",
            "last_error": self.last_error,
            "last_error_ts": self.last_error_ts,
            "last_ok_ts": self.last_ok_ts,
        }

    def _post(self, url: str, headers: dict, body: dict,
              timeout: float, trust_env: bool) -> "requests.Response":
        s = requests.Session()
        s.trust_env = trust_env      # False = 忽略 HTTP(S)_PROXY 环境变量
        try:
            return s.post(url, headers=headers, json=body, timeout=timeout)
        finally:
            s.close()

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
        def _fail(msg: str) -> None:
            self.last_error = msg
            self.last_error_ts = time.time()

        # 依次尝试：直连（若上回直连成功过）→ 环境代理 → 直连兜底
        attempts = [False, True] if self._direct_only else [True]
        if not self._direct_only and self.PROXY_RETRY_ONCE:
            attempts = [True, False]
        last_exc: Optional[Exception] = None
        for trust_env in attempts:
            try:
                r = self._post(url, headers, body, timeout or self.timeout, trust_env)
                if r.status_code != 200:
                    logger.warning("DeepSeek HTTP %s: %s",
                                   r.status_code, r.text[:200])
                    _fail(f"HTTP {r.status_code}: {r.text[:200]}")
                    return None
                data = r.json()
                content = data["choices"][0]["message"]["content"]
                self._direct_only = (trust_env is False)
                self.last_error = None
                self.last_ok_ts = time.time()
                return {"content": content, "model": data.get("model", self.model),
                        "usage": data.get("usage", {}), "raw": data}
            except (requests.Timeout, requests.ConnectionError) as e:
                last_exc = e
                logger.warning("DeepSeek 网络异常(%s): %s",
                               "env-proxy" if trust_env else "direct", e)
            except Exception as e:
                last_exc = e
                logger.warning("DeepSeek 调用失败: %s: %s", type(e).__name__, e)
                break
        _fail(f"{type(last_exc).__name__}: {last_exc}" if last_exc else "unknown")
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
# -*- coding: utf-8 -*-
"""qmt_client 行情新鲜度阈值回归测试（2026-09-16 优化）。

验证 PUSH_STALE_SEC 由 60s 收紧到 30s：超过该陈旧度的 push 缓存会被丢弃、
降级走 get_full_tick 快照，避免在过期报价上做止损/开仓决策。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import core.qmt_client as qmt_client


def test_push_stale_sec_tightened_to_30():
    """xtdata 实时客户端 push 缓存陈旧阈值应为 30s（不为旧的 60s）。"""
    impl = qmt_client.qmt_client._impl
    # 仅当运行在真实 xtdata 模式（_XtdClient）时校验常量；mock 模式跳过。
    if qmt_client.qmt_client.mode == "xtdata" and hasattr(impl, "PUSH_STALE_SEC"):
        assert impl.PUSH_STALE_SEC == 30.0, (
            f"PUSH_STALE_SEC 应为 30.0，实际 {impl.PUSH_STALE_SEC}")
    else:
        # mock 模式：直接校验模块级常量定义（从源码语义），确保不为 60
        from core.qmt_client import _XtdClient
        assert _XtdClient.PUSH_STALE_SEC == 30.0


if __name__ == "__main__":
    test_push_stale_sec_tightened_to_30()
    print("QMT CLIENT STALE TEST PASSED")

# -*- coding: utf-8 -*-
"""tushare 域名必须绕开代理（2026-11-08 事故守卫）。

事故链（真实发生，非假设）：
  1. guardian 用无参 ``python main.py --web`` 拉起引擎，继承了环境里的
     ``HTTP_PROXY=127.0.0.1:7897``（VPN）；
  2. 那一刻 VPN 没开；
  3. ``data.tushare_client`` 的每次调用都
     ``ProxyError: [WinError 10061] 目标计算机积极拒绝``；
  4. 行情源按 ``xtdata → tushare → mock`` 优先级降到 **mock**；
  5. ``mock_trade_block_reason`` 首行 ``data_mode=="mock"`` ⇒ **当日所有买卖被禁**。

一个与行情毫无关系的环境变量，让一个交易日零成交。修法刻意只加 ``NO_PROXY``，
不改全局代理（GitHub / 包管理仍可走代理）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.tushare_quote import (                        # noqa: E402
    TUSHARE_NO_PROXY_HOSTS, bypass_proxy_for_tushare,
)

WADITU_URL = "http://api.waditu.com/dataapi/stock_basic"
OTHER_URL = "https://github.com/"


def _save_env():
    keys = ("HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy")
    return {k: os.environ.get(k) for k in keys}


def _restore_env(snap):
    for k, v in snap.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_waditu_bypasses_proxy_others_do_not():
    """核心断言：置了坏代理后，tushare 域名不算「要走代理」，其它域名照旧走。"""
    from requests.utils import should_bypass_proxies

    snap = _save_env()
    try:
        os.environ["HTTP_PROXY"] = os.environ["http_proxy"] = "http://127.0.0.1:7897"
        for k in ("NO_PROXY", "no_proxy"):
            os.environ.pop(k, None)
        # 前置条件：没有 NO_PROXY 时，坏代理会被用在 tushare 上（复刻事故）
        assert should_bypass_proxies(WADITU_URL, None) is False, \
            "前置条件不成立：无 NO_PROXY 时本应走代理"
        bypass_proxy_for_tushare()
        assert should_bypass_proxies(WADITU_URL, None) is True, \
            "tushare 域名必须绕开代理，否则 VPN 一关行情就掉 mock"
        assert should_bypass_proxies(OTHER_URL, None) is False, \
            "不得误伤：其它域名仍然要走代理"
    finally:
        _restore_env(snap)


def test_merges_with_existing_no_proxy():
    """已有 NO_PROXY 条目不得被覆盖（可能有别的人写过 localhost 等）。"""
    snap = _save_env()
    try:
        os.environ["NO_PROXY"] = "localhost,127.0.0.1"
        merged = bypass_proxy_for_tushare()
        assert "localhost" in merged and "127.0.0.1" in merged, \
            f"既有条目被吞掉: {merged}"
        for h in TUSHARE_NO_PROXY_HOSTS:
            assert h in merged
    finally:
        _restore_env(snap)


def test_idempotent_no_duplicates():
    """重复调用不得让 NO_PROXY 无限变长（引擎会多次 _connect）。"""
    snap = _save_env()
    try:
        os.environ.pop("NO_PROXY", None)
        os.environ.pop("no_proxy", None)
        first = bypass_proxy_for_tushare()
        for _ in range(5):
            again = bypass_proxy_for_tushare()
        assert again == first, f"重复调用产生了漂移: {first} -> {again}"
        assert again.count("api.waditu.com") == 1
    finally:
        _restore_env(snap)


def test_tushare_client_calls_the_bypass():
    """`data/tushare_client.py` 必须调用同一份实现，不得各自造一个域名清单。"""
    src = (ROOT / "data" / "tushare_client.py").read_text(encoding="utf-8")
    assert "bypass_proxy_for_tushare" in src, \
        "tushare_client 连接前必须绕开代理（它才是掉 mock 的那条链）"

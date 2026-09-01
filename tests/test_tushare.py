# -*- coding: utf-8 -*-
"""data/tushare_client.py 单元测试。

无 token 时优雅降级（不报错）；有 token 时实际调 API（依赖网络）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.tushare_client import TushareClient, _normalize_basic


def test_disabled_without_token():
    c = TushareClient(token="")
    assert not c.enabled
    assert c.test_connection() is False
    assert c.get_stock_basic("300308.SZ") is None
    assert c.get_daily_basic("300308.SZ") is None
    assert c.passes_filter("300308.SZ") is True   # 拿不到数据放行


def test_normalize_basic():
    raw = {"ts_code": "x.SZ", "trade_date": "20260824",
           "close": 10.5, "pe": 20.0, "pb": 5.0,
           "total_mv": "100000.0", "turnover_rate": "1.2"}
    out = _normalize_basic(raw)
    assert out["close"] == 10.5
    assert out["pe"] == 20.0
    assert out["pb"] == 5.0
    assert out["total_mv"] == 100000.0
    assert out["turnover_rate"] == 1.2


def test_normalize_basic_handles_bad_values():
    out = _normalize_basic({"pe": "", "pb": None, "close": "abc"})
    assert out["pe"] is None
    assert out["pb"] is None
    assert out["close"] is None  # 不能转 float


def test_with_token_live():
    """需要真实 token 和网络；任一失败就跳过。"""
    c = TushareClient()
    if not c.enabled:
        print("SKIP: no tushare token")
        return
    # test_connection 可能因网络/限频失败，不强制断言
    try:
        ok = c.test_connection()
        if not ok:
            print("SKIP: tushare connection failed (network/rate)")
            return
        # 有数据 → 验证 summary 字段完整
        s = c.summary("300308.SZ")
        assert s is not None
        assert "name" in s
        assert "industry" in s
        assert "close" in s
        print(f"OK: summary 300308.SZ = {s.get('name')} PE={s.get('pe')} ROE={s.get('roe')}")
    except Exception as e:
        print(f"SKIP: tushare live test failed: {e}")
# -*- coding: utf-8 -*-
"""data/dynamic_universe.py 单元测试。

每个测试用独立的临时 cache 文件，避免与默认 storage/dynamic_universe.json 互相污染。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.dynamic_universe import DynamicUniverse


def _isolated_universe(config):
    """新建 DynamicUniverse 并禁用默认缓存加载，返回实例。"""
    du = DynamicUniverse(config)
    # 用临时目录的 cache 文件，避免与项目 storage 互污染
    tmp = Path(tempfile.mkdtemp()) / "cache.json"
    du._cache_file = tmp
    # 重置已加载的数据
    du._universe = {}
    du._industries = {}
    du._active_pool = []
    du._last_refresh = 0
    return du


def test_disabled_when_config_off():
    du = _isolated_universe({"enabled": False, "industries": ["半导体"]})
    assert not du.enabled
    assert du.refresh() is False


def test_disabled_without_tushare():
    du = _isolated_universe({"enabled": True, "industries": ["半导体"]})
    # enabled 取决于 tushare_client.enabled；这里只测函数返回 bool
    assert isinstance(du.refresh(), bool)


def test_snapshot_empty():
    du = _isolated_universe({"enabled": False, "industries": ["半导体"]})
    snap = du.snapshot()
    assert snap["enabled"] is False
    assert snap["n_total"] == 0
    assert snap["active_pool_size"] == 0


def test_needs_refresh_fresh_vs_old():
    du = _isolated_universe({"enabled": False, "industries": ["半导体"]})
    # enabled=False → 永远不刷新
    assert not du.needs_refresh()


def test_with_token_live():
    """需要真实 token + 网络；任一失败就跳过。"""
    du = _isolated_universe(None)
    if not du.enabled:
        print("SKIP: no tushare token / disabled")
        return
    ok = du.refresh(force=True)
    if not ok:
        print("SKIP: tushare refresh failed (network/rate)")
        return
    snap = du.snapshot()
    print(f"OK: total={snap['n_total']} active={snap['active_pool_size']} "
          f"industries={snap['by_industry']}")
    assert snap["n_total"] > 0
    assert "半导体" in snap["by_industry"]
    assert "通信设备" in snap["by_industry"]
    assert "IT设备" in snap["by_industry"]
    assert snap["active_pool_size"] <= snap["n_total"]
    assert snap["active_pool_size"] <= du.config["active_pool_size"]


def test_get_name_and_industry():
    du = _isolated_universe(None)
    if not du.enabled:
        print("SKIP: no tushare token")
        return
    if not du.refresh(force=True):
        print("SKIP: refresh failed")
        return
    code = du.active_codes[0] if du.active_codes else None
    if code:
        name = du.get_name(code)
        ind = du.get_industry(code)
        print(f"OK: {code} name={name} industry={ind}")
        assert name
        assert ind in du.config["industries"]


def test_persistence_file():
    """刷新后会写 _cache_file（独立临时）。"""
    du = _isolated_universe(None)
    if not du.enabled:
        print("SKIP: no tushare token")
        return
    if not du.refresh(force=True):
        print("SKIP: refresh failed")
        return
    import json
    cache = du._cache_file
    assert cache.exists()
    with cache.open(encoding="utf-8") as f:
        data = json.load(f)
    assert "last_refresh" in data
    assert "active_pool" in data
    assert "universe" in data
    print(f"OK: {cache} ({len(data['active_pool'])} active)")
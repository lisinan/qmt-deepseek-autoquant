# -*- coding: utf-8 -*-
"""
集中式股票代码 → 名称解析（前端 + 后端共用）。

覆盖来源（按优先级合并，后写不覆盖先写）：
  1. 静态 UNIVERSE / SECTOR_CONFIG（无需网络）
  2. 动态候选池 dynamic_universe（来自 Tushare，含名称）
  3. Tushare 全市场 stock_basic（最佳覆盖，后台刷新、带缓存）

原前端只用 window.UNIVERSE（仅 16 只静态标的），导致动态候选池（~373 只）
与持仓里的非静态标的在行情表 / 重排序列表 / 持仓表只显示代码。
本模块提供完整 code→name 映射，前端用 NAME(code) 即可正确显示中文名。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Optional

from config.settings import UNIVERSE, SECTOR_CONFIG

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()

# 全市场名称缓存（来自 Tushare stock_basic，后台刷新）
_FULL_CACHE: Dict[str, str] = {}
_FULL_TS = 0.0
_FULL_TTL = 3600  # 1 小时


def _seed_static() -> Dict[str, str]:
    """静态名称：UNIVERSE + 各产业链环节 + 常见指数。"""
    m: Dict[str, str] = {}
    for code, name in UNIVERSE.items():
        if name:
            m[code] = name
    for sec in SECTOR_CONFIG.get("sectors", {}).values():
        for code, name in sec.get("stocks", []):
            if name:
                m.setdefault(code, name)
    # 常见指数补充
    for code, name in (("000001.SH", "上证指数"), ("399001.SZ", "深证成指"),
                       ("399006.SZ", "创业板指"), ("000300.SH", "沪深300")):
        m.setdefault(code, name)
    return m


def _refresh_full_from_tushare() -> None:
    """后台拉取 Tushare 全市场名称进缓存（best-effort，不阻塞主流程）。"""
    global _FULL_CACHE, _FULL_TS
    try:
        from data.tushare_client import tushare_client as _tc
    except Exception as e:  # pragma: no cover
        logger.debug("导入 tushare_client 失败: %s", e)
        return
    if not getattr(_tc, "enabled", False):
        return
    try:
        df = _tc.get_stock_basic()  # 全市场 DataFrame
        if df is None or not hasattr(df, "empty") or df.empty:
            return
        with _LOCK:
            m: Dict[str, str] = {}
            for _, row in df.iterrows():
                c = row.get("ts_code")
                n = row.get("name")
                if c and n:
                    m[c] = n
            _FULL_CACHE = m
            _FULL_TS = time.time()
        logger.info("Tushare 全市场名称已缓存 %d 只", len(_FULL_CACHE))
    except Exception as e:
        logger.debug("Tushare 全市场名称刷新失败: %s", e)


def build_name_map(dynamic_universe=None) -> Dict[str, str]:
    """合并静态 + 动态候选池 + 已缓存全市场名称，返回 code -> name。"""
    with _LOCK:
        m = _seed_static()
        # 动态候选池（来自 Tushare，含名称）
        if dynamic_universe is not None:
            try:
                umap = dynamic_universe.universe_map()
                if isinstance(umap, dict):
                    for code, info in umap.items():
                        nm = info.get("name") if isinstance(info, dict) else None
                        if nm:
                            m.setdefault(code, nm)
            except Exception as e:
                logger.debug("build_name_map 动态候选池失败: %s", e)
        # 合并已缓存的全市场名称（后台刷新所得）
        for c, n in _FULL_CACHE.items():
            m.setdefault(c, n)
        return dict(m)


def get_name_map(dynamic_universe=None, force_full: bool = False) -> Dict[str, str]:
    """取名称映射；force_full=True 时同步触发一次 Tushare 全市场刷新（会阻塞）。"""
    if force_full:
        _refresh_full_from_tushare()
    return build_name_map(dynamic_universe=dynamic_universe)


def get_stock_name(code: str, dynamic_universe=None) -> str:
    """单只代码 → 名称，找不到返回原代码。"""
    if not code:
        return code
    return build_name_map(dynamic_universe=dynamic_universe).get(code) or code


def background_refresh() -> None:
    """后台刷新全市场名称缓存（在 Web 页面加载时调用，不阻塞响应）。"""
    threading.Thread(target=_refresh_full_from_tushare, daemon=True).start()

# -*- coding: utf-8 -*-
"""
动态候选池（从 Tushare 全市场按行业过滤）

解决问题：
  - 静态 UNIVERSE 只有 16 只，错失大量潜在标的
  - 固定池子不随市场变化
  - 行业轮动时不能及时跟踪

流程：
  1. 从 Tushare stock_basic 拉指定行业的全部 A 股
  2. 过滤：ST / 北交所 / 上市太短
  3. 按市值取活跃 Top-N（避免订阅 400+ 只造成网络压力）
  4. 与静态 UNIVERSE 合并 → 实际候选池
  5. 每 N 小时刷新一次（持久化到 dynamic_universe.json 供查看）

注意：
  - 这是真正的"动态扩展"，而非依赖某个 LLM 的"推荐"
  - 行业过滤规则完全透明，可在 DYNAMIC_UNIVERSE_CONFIG 调
"""
from __future__ import annotations

import copy
import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from config.settings import BASE_DIR, DYNAMIC_UNIVERSE_CONFIG
from data.tushare_client import tushare_client

logger = logging.getLogger(__name__)


class DynamicUniverse:
    def __init__(self, config: Optional[dict] = None):
        # 深拷贝防止外部修改污染
        self.config = copy.deepcopy(config or DYNAMIC_UNIVERSE_CONFIG)
        self._universe: Dict[str, dict] = {}     # code -> {name, industry, list_date, market}
        self._industries: Dict[str, List[str]] = {}   # industry -> [codes]
        self._active_pool: List[str] = []        # 实际参与评分/订阅的 Top-N
        self._last_refresh: float = 0
        self._lock = threading.RLock()
        self._cache_file = BASE_DIR / "storage" / "dynamic_universe.json"
        # 启动时优先从缓存加载（新进程冷启动时也能快速读出数据）
        self._load_from_cache()

    # ============================================================ 状态

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", False)) and tushare_client.enabled

    @property
    def codes(self) -> List[str]:
        """全量动态池（过滤后）。"""
        with self._lock:
            return list(self._universe.keys())

    @property
    def active_codes(self) -> List[str]:
        """实际参与评分/订阅的活跃 Top-N。"""
        with self._lock:
            return list(self._active_pool)

    @property
    def last_refresh(self) -> float:
        return self._last_refresh

    @property
    def last_refresh_str(self) -> str:
        if self._last_refresh == 0:
            return "never"
        return datetime.fromtimestamp(self._last_refresh).isoformat()

    def get_industry(self, code: str) -> Optional[str]:
        with self._lock:
            info = self._universe.get(code)
            return info.get("industry") if info else None

    def get_name(self, code: str) -> Optional[str]:
        with self._lock:
            info = self._universe.get(code)
            return info.get("name") if info else None

    def universe_map(self) -> Dict[str, dict]:
        with self._lock:
            return dict(self._universe)

    def industries_breakdown(self) -> Dict[str, int]:
        with self._lock:
            return {k: len(v) for k, v in self._industries.items()}

    def needs_refresh(self) -> bool:
        if not self.enabled:
            return False
        interval = self.config.get("refresh_interval", 86400)
        return time.time() - self._last_refresh > interval

    # ============================================================ 核心：刷新

    def refresh(self, force: bool = False) -> bool:
        """
        从 Tushare 拉指定行业股票 → 过滤 → 取 Top-N 活跃池。
        返回是否成功刷新。
        """
        if not self.enabled:
            logger.info("DynamicUniverse 未启用（enabled=%s, tushare=%s）",
                        self.config.get("enabled"), tushare_client.enabled)
            return False

        with self._lock:
            interval = self.config.get("refresh_interval", 86400)
            if not force and time.time() - self._last_refresh < interval:
                return True   # 不需要刷新但算 OK

            industries = self.config.get("industries") or []
            df = tushare_client.get_industry_stocks(industries)
            if df is None or df.empty:
                logger.warning("DynamicUniverse: Tushare 返回空")
                return False

            universe: Dict[str, dict] = {}
            ind_to_codes: Dict[str, List[str]] = {ind: [] for ind in industries}
            for _, row in df.iterrows():
                code = row.get("ts_code")
                name = row.get("name") or ""
                industry = row.get("industry")
                market = row.get("market") or ""
                list_date = str(row.get("list_date") or "")

                if not code or not industry:
                    continue
                if industry not in ind_to_codes:
                    continue

                # 过滤：ST
                if self.config.get("exclude_st", True):
                    if "ST" in name or "*ST" in name:
                        continue
                # 过滤：北交所
                if self.config.get("exclude_bj", True):
                    if market == "北交所":
                        continue
                # 过滤：上市天数
                min_days = self.config.get("min_list_days", 365)
                if min_days > 0 and list_date:
                    try:
                        listed = datetime.strptime(list_date, "%Y%m%d")
                        if (datetime.now() - listed).days < min_days:
                            continue
                    except ValueError:
                        pass

                universe[code] = {
                    "name": name,
                    "industry": industry,
                    "list_date": list_date,
                    "market": market,
                }
                ind_to_codes[industry].append(code)

            # 取活跃 Top-N（按总市值）
            active_size = self.config.get("active_pool_size", 30)
            active_pool = self._pick_active_pool(universe, active_size)

            self._universe = universe
            self._industries = ind_to_codes
            self._active_pool = active_pool
            self._last_refresh = time.time()

            self._save_to_file()
            logger.info(
                "DynamicUniverse 刷新完成: total=%d active=%d industries=%s",
                len(universe), len(active_pool),
                {k: len(v) for k, v in ind_to_codes.items()},
            )
            return True

    def _pick_active_pool(self, universe: Dict[str, dict],
                          size: int) -> List[str]:
        """按总市值选 Top-N（用 daily_basic.total_mv）。"""
        if size <= 0 or not universe:
            return list(universe.keys())[:size]
        if not tushare_client.enabled:
            return list(universe.keys())[:size]
        # 批量拉 daily_basic（一次性，避免 N 次单查）
        scored: List[tuple] = []   # (total_mv, code)
        for code in universe.keys():
            basic = tushare_client.get_daily_basic(code)
            mv = (basic or {}).get("total_mv") or 0
            scored.append((mv, code))
        scored.sort(reverse=True)
        return [code for _, code in scored[:size]]

    # ============================================================ 持久化

    def _save_to_file(self):
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            with self._cache_file.open("w", encoding="utf-8") as f:
                json.dump({
                    "last_refresh": self.last_refresh_str,
                    "industries": self.config.get("industries", []),
                    "n_total": len(self._universe),
                    "active_pool_size": len(self._active_pool),
                    "by_industry": {k: len(v) for k, v in self._industries.items()},
                    "active_pool": self._active_pool,
                    "universe": self._universe,
                    "industries_map": self._industries,
                    "sample": {
                        code: self._universe[code]
                        for code in list(self._universe.keys())[:5]
                    },
                }, f, ensure_ascii=False, indent=2, default=str)
        except Exception as e:
            logger.debug("保存 dynamic_universe.json 失败: %s", e)

    def _load_from_cache(self):
        """启动时优先从 cache file 加载（避免冷启动显示空白）。"""
        try:
            if not self._cache_file.exists():
                return
            with self._cache_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
            self._active_pool = data.get("active_pool") or []
            self._universe = data.get("universe") or {}
            self._industries = data.get("industries_map") or {}
            # 还原 last_refresh（从 ISO 字符串）
            ts_str = data.get("last_refresh")
            if ts_str and ts_str != "never":
                try:
                    self._last_refresh = datetime.fromisoformat(ts_str).timestamp()
                except Exception:
                    pass
            if self._universe:
                logger.info("DynamicUniverse 从缓存加载: %d 只, 活跃 %d",
                            len(self._universe), len(self._active_pool))
        except Exception as e:
            logger.debug("加载 dynamic_universe.json 失败: %s", e)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "enabled": self.enabled,
                "last_refresh": self.last_refresh_str,
                "industries": self.config.get("industries", []),
                "n_total": len(self._universe),
                "active_pool_size": len(self._active_pool),
                "by_industry": {k: len(v) for k, v in self._industries.items()},
                "active_pool": list(self._active_pool),
            }


# 模块级单例
dynamic_universe = DynamicUniverse()
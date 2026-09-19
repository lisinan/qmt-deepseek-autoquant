# -*- coding: utf-8 -*-
"""北向资金(沪深港通净买入)市场级缓存 —— 全新正交数据轴研发。

与 data/moneyflow_cache.py（个股级主力净流）不同：北向是**市场级**单一序列
（沪股通+深股通每日净买入额 north_money），非个股级。它捕捉「外资系统性
资金面」方向，与价格动量正交，本宇宙尚未使用。适合做组合层择时/风险预算
调制（对冲隔夜跳空、外资系统性撤离），而非选股排名。

数据：Tushare `moneyflow_hsgt` 接口，字段 `north_money`（单位万元，净买入为正）。
对齐：trade_date 与 xtdata 日线交易日一致（同为 A 股交易日），无未来函数
（收盘后发布的北向净流用于当日收盘信号，次日开盘执行）。

缓存：单文件 data/cache/northbound_hsgt.json（全历史 {trade_date: north_money}），
重复回测零网络消耗。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Dict, Optional

from config.settings import BASE_DIR
from data.tushare_client import tushare_client

logger = logging.getLogger(__name__)

CACHE_DIR = BASE_DIR / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
F = CACHE_DIR / "northbound_hsgt.json"


def _seg_load(start: str, end: str) -> Dict[str, float]:
    """用 Tushare moneyflow_hsgt 拉 [start,end]；失败回退逐月。"""
    out: Dict[str, float] = {}
    if not tushare_client._connect():
        return out
    pro = tushare_client._pro
    # 主路径：按月分段（接口对长区间易限流/截断）
    y, m = int(start[:4]), int(start[4:6])
    cur = f"{y:04d}{m:02d}01"
    while cur <= end:
        ny, nm = int(cur[:4]), int(cur[4:6]) + 12
        ny += nm // 12
        nm = nm % 12 or 12
        nxt = f"{ny:04d}{nm:02d}01"
        seg_end = str(min(int(nxt) - 1, int(end)))
        try:
            df = pro.moneyflow_hsgt(start_date=cur, end_date=seg_end)
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    d = str(row.get("trade_date"))
                    v = row.get("north_money")
                    out[d] = float(v) if v is not None else 0.0
        except Exception as e:
            logger.warning("northbound %s~%s 失败: %s", cur, seg_end, e)
        cur = nxt
    return out


def get_northbound(start: str = "20221201",
                   end: Optional[str] = None,
                   force: bool = False) -> Dict[str, float]:
    """返回 {trade_date(YYYYMMDD): north_money(float, 万元)}。

    - 优先命中本地磁盘缓存（零网络）。
    - 拿不到数据时返回 {}（调用方应降级为「不设防」，不阻断交易）。
    """
    end = end or time.strftime("%Y%m%d")
    if not force and F.exists():
        try:
            full = {d: float(v)
                    for d, v in json.loads(F.read_text(encoding="utf-8")).items()}
            return {d: v for d, v in full.items() if start <= d <= end}
        except Exception:
            pass
    data = _seg_load(start, end)
    if data:
        merged: Dict[str, float] = {}
        if F.exists():
            try:
                merged = {d: float(v)
                          for d, v in json.loads(F.read_text(encoding="utf-8")).items()}
            except Exception:
                pass
        merged.update(data)
        try:
            F.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.debug("northbound 缓存写失败: %s", e)
    return {d: v for d, v in data.items() if start <= d <= end}


def preload_northbound(start: str = "20221201",
                       end: Optional[str] = None) -> Dict[str, float]:
    """批量预加载（与 get_northbound 同义，供回测一次性复用）。"""
    return get_northbound(start, end)

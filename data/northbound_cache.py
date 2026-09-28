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


# ---------------------------------------------------------------------------
# 可信度守卫（2026-09-28 PM-EVOLVE 第 11 轮落盘）
#
# 背景（实盘铁证）：本序列取自 tushare `moneyflow_hsgt.north_money`
# (= hgt + sgt)。**自 2024-08-19 起沪深港通已停止披露每日净买入额**，实测：
#   ┌──────────────────────┬────┬──────┬──────────┬──────────┐
#   │ 区间                 │ n  │ 正占比│ 均值      │ 标准差    │
#   ├──────────────────────┼────┼──────┼──────────┼──────────┤
#   │ 2023-01~2024-08-16   │235 │ 0.44 │     -53   │    5,987  │  ← 真净买入
#   │ 2024-08-19~2026-08-25│365 │ 1.00 │  272,396  │   92,996  │  ← 非净买入
#   └──────────────────────┴────┴──────┴──────────┴──────────┘
#   断点前最长连续正值仅 **8 天**（235 天样本）；断点后 **365/365 天全正**。
#   ⇒ 断点后是「恒正、窄带」序列（成交额口径），**不是净买入**。
#   ⇒ 滚动 20 日累计 < 0 在 2025 年 0/128 天、2026 年 0/132 天成立
#     ⇒ **实盘 nb gate 是「死闸门」，自 2025 年起从未真正拦截过任何建仓**。
#
# 危害：① 用「恒正序列」冒充净买入，闸门形同虚设却让人误以为有保护；
#      ② 本地缓存一旦写入**永不再刷新**（get_northbound 命中即 return），
#         实测末端停在 2026-08-25（陈旧 34 天），一旦符号恢复就会用
#         **5 周前的旧数据**拦截当日建仓。
#
# 守卫：任一为真即判定**不可信 → fail-open（不拦截）**并告警：
#   (a) 陈旧：序列末端交易日距评估日 > nb_stale_days 个自然日；
#   (b) 退化：截至评估日的 trailing nb_degenerate_days 个交易日**全为正**
#             （真实净买入序列不可能连涨 60 日：断点前实测最长仅 8 天、
#              60 日滚动全正窗口数 0/175 ⇒ 误判率为 0）。
# ---------------------------------------------------------------------------
NB_STALE_DAYS = 10
NB_DEGENERATE_DAYS = 60


def _days_between(a: str, b: str) -> Optional[int]:
    """两个 YYYYMMDD 之间相差的自然日数（b - a）；解析失败返回 None。"""
    try:
        from datetime import date
        da = date(int(a[:4]), int(a[4:6]), int(a[6:8]))
        db = date(int(b[:4]), int(b[4:6]), int(b[6:8]))
        return (db - da).days
    except Exception:
        return None


def nb_gate_credible(series: Dict[str, float],
                     dates_sorted: Optional[List[str]],
                     eval_day: str,
                     stale_days: int = NB_STALE_DAYS,
                     degenerate_days: int = NB_DEGENERATE_DAYS) -> tuple:
    """判定「截至 eval_day，该序列能否当作真实净买入来驱动闸门」。

    返回 ``(credible: bool, reason: str)``。``reason`` ∈
    ``ok`` / ``empty`` / ``stale`` / ``degenerate``。
    调用方在 ``credible=False`` 时必须 **fail-open**（不拦截建仓）。
    """
    if not series:
        return False, "empty"
    keys = dates_sorted if dates_sorted is not None else sorted(series.keys())
    past = [d for d in keys if d <= eval_day]
    if not past:
        return False, "empty"

    if stale_days > 0:
        gap = _days_between(past[-1], eval_day)
        if gap is not None and gap > stale_days:
            return False, "stale"

    if degenerate_days > 0 and len(past) >= degenerate_days:
        win = past[-degenerate_days:]
        if all(series.get(d, 0.0) > 0 for d in win):
            return False, "degenerate"
    return True, "ok"

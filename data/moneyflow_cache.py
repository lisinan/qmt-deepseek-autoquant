# -*- coding: utf-8 -*-
"""
主力资金流缓存（Tushare moneyflow 端点）

目的：把「主力资金净流入」这一**全新 alpha 数据**（当前策略完全未用）
缓存到本地磁盘，供回测 / live 引擎按交易日对齐调用。

为什么值得做（与历史被拒项区别）：
  - 此前所有被拒改进都是「参数级微调」或「市场择时叠加层」（regime /
    tailhedge / 移动止损 / sizing）。它们本质都是同一信号的重新排列，
    在强趋势 AI 宇宙里反复被样本外证伪。
  - 主力资金流是**新数据轴**：机构/大户逐日净买卖方向，与价格动量正交，
    在 A 股有独立预测力（经典「聪明钱」效应）。把它作为「动量质量确认」
    或「横截面动量增强」，是记忆里点名的下一个突破点（新数据 alpha）。

对齐约定：moneyflow 的 trade_date 与 xtdata 日线交易日一致（同为 A 股
交易日），故可直接按日期字符串映射，无未来函数（收盘后发布的资金流
用于当日收盘信号生成，次日开盘成交）。

缓存：每只股票一个 JSON 文件（data/cache/moneyflow_<ts_code>.json），
覆盖 [start, end] 全历史。重复回测零网络消耗。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

from config.settings import BASE_DIR
from data.tushare_client import tushare_client

logger = logging.getLogger(__name__)

CACHE_DIR = BASE_DIR / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 资金流字段 → 语义
_FIELDS = (
    "buy_sm_vol", "buy_md_vol", "buy_lg_vol", "buy_elg_vol",
    "sell_sm_vol", "sell_md_vol", "sell_lg_vol", "sell_elg_vol",
    "net_mf_vol", "net_mf_amount",
)


def _cache_file(ts_code: str) -> Path:
    return CACHE_DIR / f"moneyflow_{ts_code}.json"


# 缓存龄期上限（自然日）。序列末端日期距今天超过该值即视为陈旧：
# 记 error 日志但**仍照常返回数据**（fail-open，数据可见性优先）。
MAX_STALE_DAYS = 10


def _days_between(a: str, b: str) -> Optional[int]:
    """两个 YYYYMMDD 之间相差的自然日数（b - a）；解析失败返回 None。"""
    try:
        from datetime import date
        da = date(int(a[:4]), int(a[4:6]), int(a[6:8]))
        db = date(int(b[:4]), int(b[4:6]), int(b[6:8]))
        return (db - da).days
    except Exception:
        return None


def _check_stale(series: Dict[str, dict], label: str) -> None:
    """校验序列末端日期龄期；超限记 error 但不丢数据（fail-open）。"""
    if not series:
        return
    try:
        last = max(series.keys())
    except Exception:
        return
    gap = _days_between(last, time.strftime("%Y%m%d"))
    if gap is not None and gap > MAX_STALE_DAYS:
        logger.error("%s缓存已陈旧 %d 天（末端 %s），数据可能失效", label, gap, last)


def cache_health(ts_code: Optional[str] = None) -> Dict[str, object]:
    """缓存健康度（只读本地缓存文件，不联网）。

    ``ts_code=None`` 时扫描 ``data/cache/moneyflow_*.json`` 全部文件，
    取末端日期最新的一份作为整体健康度。
    返回 ``{"last_date": "YYYYMMDD"|"", "age_days": int, "stale": bool}``。
    """
    empty = {"last_date": "", "age_days": -1, "stale": False}
    if ts_code:
        files = [_cache_file(ts_code)]
    else:
        try:
            files = sorted(CACHE_DIR.glob("moneyflow_*.json"))
        except Exception:
            return empty
    last = ""
    try:
        for f in files:
            if not f.exists():
                continue
            series = json.loads(f.read_text(encoding="utf-8"))
            if series:
                d = max(series.keys())
                if d > last:
                    last = d
    except Exception:
        return empty
    if not last:
        return empty
    gap = _days_between(last, time.strftime("%Y%m%d"))
    age = gap if gap is not None else -1
    return {"last_date": last, "age_days": age,
            "stale": bool(age > MAX_STALE_DAYS)}


def get_moneyflow(ts_code: str, start: str = "20230101",
                  end: Optional[str] = None, force: bool = False) -> Dict[str, dict]:
    """返回 {trade_date(str YYYYMMDD): {net_mf_amount, net_mf_vol, ...}}。

    - 优先命中本地磁盘缓存（零网络）。
    - 拿不到数据时返回 {}（调用方应降级为「不设防」，不阻断交易）。
    """
    end = end or time.strftime("%Y%m%d")
    f = _cache_file(ts_code)
    if not force and f.exists():
        try:
            full = json.loads(f.read_text(encoding="utf-8"))
            _check_stale(full, f"主力资金流 {ts_code}")
            return {d: v for d, v in full.items()}
        except Exception:
            pass
    if not tushare_client.enabled:
        return {}
    df = tushare_client.moneyflow_df(ts_code, start, end)
    if df is None:
        return {}
    out: Dict[str, dict] = {}
    for _, row in df.iterrows():
        d = str(row.get("trade_date"))
        out[d] = {k: (float(row.get(k) or 0.0) if k in _FIELDS else row.get(k))
                  for k in _FIELDS}
    try:
        f.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.debug("写资金流缓存失败 %s: %s", ts_code, e)
    return out


def preload_moneyflow(codes: List[str], start: str = "20230101",
                      end: Optional[str] = None) -> Dict[str, Dict[str, dict]]:
    """批量预加载多只股票资金流（供回测一次性复用）。"""
    out: Dict[str, Dict[str, dict]] = {}
    for c in codes:
        m = get_moneyflow(c, start, end)
        if m:
            out[c] = m
    return out

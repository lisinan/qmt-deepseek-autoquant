# -*- coding: utf-8 -*-
"""
业绩预告上修（earnings-guidance upward revision）信号缓存

目的：把"公司自身披露的盈利指引上修"这一**全新基本面数据轴**缓存到本地，
供回测 / live 引擎按交易日对齐调用。这是本宇宙当前唯一尚未测试的、与价格
动量**正交**的另类 alpha 来源（记忆点名的下一个突破点：盈利修正 / 评级）。

为什么是"上修"而不是 PE/PB 过滤：
  - 此前项目只用 daily_basic 的 PE/PB/MV 做"静态质量过滤"，那是横截面 level，
    与价格高度共线（强趋势股 PE 也高）。
  - 业绩预告上修是**公司层面的前瞻修正事件**（同一报告期，后一次指引高于前
    一次），属"盈利动量 / 预告漂移(PEAD)"经典因子，捕捉的是"预期在变好"的
    边际信息，与 20 日价格动量正交。

对齐约定：ann_date（披露日）收盘后信号可得，次日开盘成交，无未来函数。
缓存：每只股票一个 JSON（data/cache/earnrev_<ts_code>.json），全历史零重复网络。
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


def _cache_file(ts_code: str) -> Path:
    return CACHE_DIR / f"earnrev_{ts_code}.json"


def _rev_from_forecast(df) -> Dict[str, float]:
    """输入 forecast DataFrame，输出 {ann_date(str YYYYMMDD): signed_rev_pct}。

    signed_rev_pct = 本次指引「净利润同比变动中点」 - 同一报告期上次指引中点。
      · 仅当同一 end_date（报告期）存在≥2 次指引时才定义修正（首次指引无基线，跳过）。
      · rev > 0 = 上修（基本面利好）；rev < 0 = 下修；rev = 0 = 持平。
    信号语义：gate 模式只看 rev>0（上修）；rank 模式用 signed_rev 做横截面倾斜。
    """
    rows = []
    for _, r in df.iterrows():
        try:
            ann = str(r.get("ann_date"))
            end = str(r.get("end_date"))
            pmin = float(r.get("p_change_min") or 0.0)
            pmax = float(r.get("p_change_max") or 0.0)
        except Exception:
            continue
        if not ann or not end:
            continue
        rows.append((ann, end, (pmin + pmax) / 2.0))
    rows.sort(key=lambda x: x[0])          # 按披露日升序
    last_mid: Dict[str, float] = {}
    out: Dict[str, float] = {}
    for ann, end, mid in rows:
        prev = last_mid.get(end)
        if prev is not None:
            rev = mid - prev
            # 记录所有有基线的修正（含下修），符号区分方向
            out[ann] = round(rev, 4)
        last_mid[end] = mid
    return out


def get_earnrev(ts_code: str, start: str = "20200101",
                end: Optional[str] = None, force: bool = False) -> Dict[str, float]:
    """返回 {ann_date(str YYYYMMDD): signed_rev_pct}。

    - 优先命中本地磁盘缓存（零网络）。
    - 拿不到数据时返回 {}（调用方降级为「不设防」，不阻断交易）。
    """
    end = end or time.strftime("%Y%m%d")
    f = _cache_file(ts_code)
    if not force and f.exists():
        try:
            return {d: float(v) for d, v in
                    json.loads(f.read_text(encoding="utf-8")).items()}
        except Exception:
            pass
    if not tushare_client.enabled:
        return {}
    df = tushare_client.forecast_df(ts_code, start, end)
    if df is None:
        return {}
    if df is None or df.empty:
        return {}
    rev = _rev_from_forecast(df)
    try:
        f.write_text(json.dumps(rev, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.debug("写 earnrev 缓存失败 %s: %s", ts_code, e)
    return rev


def preload_earnrev(codes: List[str], start: str = "20200101",
                    end: Optional[str] = None) -> Dict[str, Dict[str, float]]:
    """批量预加载多只股票的上修信号（供回测一次性复用）。"""
    out: Dict[str, Dict[str, float]] = {}
    for c in codes:
        m = get_earnrev(c, start, end)
        if m:
            out[c] = m
    return out

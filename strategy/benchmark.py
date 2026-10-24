# -*- coding: utf-8 -*-
"""
等权买入持有基准（B4 基准管道，WP-I）

与实盘账户口径对齐的「无脑持有」参照系：
  - universe_bh_pct：股票池内各标的自 ``since`` 当日收盘起、持有到最新的
    **等权** 收益（各标的「末根 close / 基点 close − 1」的算术平均）。
  - index_pct：宽基指数（如 MARKET_INDEX_CODE）同口径的区间涨幅。

数据源仅用 ``strategy.backtest_daily.load_daily`` 的本地缓存（它内部可能
触发 ``download_history``，约 0.1s/只），因此**本函数必须只在后台线程调用**，
绝不允许出现在 ``snapshot()`` 等同步高频路径上。

铁律：任何异常都不得抛出——数据不足 / 加载失败均把对应字段置 None 并写 note。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from strategy.backtest_daily import load_daily


def _bh_pct(daily: Optional[Dict[str, List[float]]], since: str) -> Optional[float]:
    """单标的「末根 close / since 当日 close − 1」。

    在 date 序列里找第一个 >= since 的下标 i；找不到或 i >= len-1（无后续
    收盘可作末值）返回 None。
    """
    if not daily:
        return None
    dates = daily.get("date") or []
    closes = daily.get("close") or []
    n = len(closes)
    if n == 0 or len(dates) != n:
        return None
    i = -1
    for k, d in enumerate(dates):
        if d >= since:
            i = k
            break
    if i < 0 or i >= n - 1:
        return None
    base = closes[i]
    last = closes[-1]
    if not base:
        return None
    return float(last / base - 1.0)


def compute(since: str, codes: list, index_code: str,
            count: int = 160) -> dict:
    """等权买入持有基准。

    since: "YYYY-MM-DD" 或 "YYYYMMDD"（load_daily 的 date 序列为 %Y%m%d）；
    codes: 股票池；index_code: 宽基。
    返回 {"universe_bh_pct": float|None, "index_pct": float|None,
          "n_codes": int, "since": str, "as_of": "YYYY-MM-DD", "note": str}
    """
    # since 统一成 "YYYYMMDD" 以对齐 load_daily 的 date 序列格式。
    since_key = since.replace("-", "") if since else ""
    out = {
        "universe_bh_pct": None,
        "index_pct": None,
        "n_codes": 0,
        "since": since,
        "as_of": None,
        "note": "",
    }
    notes: List[str] = []

    try:
        # 股票池等权收益
        pcts: List[float] = []
        for code in (codes or []):
            try:
                daily = load_daily(code, count=count)
            except Exception:
                daily = None
            if daily is None:
                notes.append(f"{code}:数据不足")
                continue
            try:
                p = _bh_pct(daily, since_key)
            except Exception:
                p = None
            if p is None:
                notes.append(f"{code}:since 后无有效收盘")
                continue
            pcts.append(p)
        if pcts:
            out["universe_bh_pct"] = float(sum(pcts) / len(pcts))
            out["n_codes"] = len(pcts)
        else:
            out["universe_bh_pct"] = None

        # 宽基指数
        try:
            idx = load_daily(index_code, count=count)
        except Exception:
            idx = None
        if idx is None:
            out["index_pct"] = None
            notes.append(f"{index_code}:数据不足")
        else:
            try:
                out["index_pct"] = _bh_pct(idx, since_key)
            except Exception:
                out["index_pct"] = None
            if out["index_pct"] is None:
                notes.append(f"{index_code}:since 后无有效收盘")

        # as_of：优先用指数序列末根日期（load_daily 的 date 为 %Y%m%d，转成 YYYY-MM-DD）。
        out["as_of"] = None
        if idx and idx.get("date"):
            try:
                raw_last = idx["date"][-1]
                if len(raw_last) == 8:
                    out["as_of"] = f"{raw_last[:4]}-{raw_last[4:6]}-{raw_last[6:8]}"
                else:
                    out["as_of"] = raw_last
            except Exception:
                out["as_of"] = None
    except Exception as ex:  # 兜底：绝不让异常逃逸
        out["universe_bh_pct"] = None
        out["index_pct"] = None
        out["n_codes"] = 0
        notes.append(f"compute 异常: {ex}")

    # 去重并整理 note（保留顺序）。
    seen = set()
    dedup: List[str] = []
    for n in notes:
        if n and n not in seen:
            seen.add(n)
            dedup.append(n)
    out["note"] = "; ".join(dedup) if dedup else "ok"
    return out

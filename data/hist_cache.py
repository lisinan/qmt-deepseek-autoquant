# -*- coding: utf-8 -*-
"""
历史日线数据层（下载 → 按交易日对齐 → 本地缓存）

存在的意义
----------
旧回测器 `strategy/backtest_daily.py` 用 **数组位置 i** 跨标的取数，而
xtdata 本地各标的的历史起点差异极大（实测 2022-05-09 ~ 2023-12-28，
跨度 19 个月）。于是 i=100 对 A 股是 2022-10，对 B 股却是 2024-05 ——
横截面动量排名、组合权益曲线全部错位，结论不可用。

本模块提供唯一正确的做法：
  1. 用 xtdata.download_history_data 把本地缺失的历史补到指定起点；
  2. 读取后按 **交易日字符串 (YYYYMMDD)** 建立索引；
  3. 输出 `AlignedPanel`：共享同一条日期轴，缺失日用 None 标记，
     回测器据此判断"该标的当日是否可交易"。

用法
----
    from data.hist_cache import load_panel
    panel = load_panel(codes, start="20190101")
    panel.dates            # List[str] 升序交易日
    panel.close["300308.SZ"][k]   # 可能为 None（当日无数据/未上市）
"""
from __future__ import annotations

import json
import pickle
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CACHE_DIR = ROOT / "storage" / "hist_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

FIELDS = ("open", "high", "low", "close", "volume", "amount")

# 复权方式：回测必须用前复权，否则送转/分红当日会出现"假暴跌"，
# 被趋势破位/硬止损误判成真实行情。
DIVIDEND_TYPE = "front_ratio"


def _bad(x) -> bool:
    """None / NaN / <=0 都视为无效价格。

    注意：xtdata 对上市前的交易日返回 float('nan')，而 Python 里
    `not float('nan')` 为 False —— 不显式判 NaN 会把上市前的空洞
    当成有效数据，直接污染回测。
    """
    if x is None:
        return True
    try:
        xf = float(x)
    except Exception:
        return True
    return xf != xf or xf <= 0        # xf != xf 即 NaN


# ============================================================ 面板结构

@dataclass
class AlignedPanel:
    """按交易日对齐的多标的行情面板。

    所有 List 长度 == len(dates)。值为 None 表示该标的当日无数据。
    """
    dates: List[str]                              # YYYYMMDD 升序
    open: Dict[str, List[Optional[float]]] = field(default_factory=dict)
    high: Dict[str, List[Optional[float]]] = field(default_factory=dict)
    low: Dict[str, List[Optional[float]]] = field(default_factory=dict)
    close: Dict[str, List[Optional[float]]] = field(default_factory=dict)
    volume: Dict[str, List[Optional[float]]] = field(default_factory=dict)
    amount: Dict[str, List[Optional[float]]] = field(default_factory=dict)

    @property
    def codes(self) -> List[str]:
        return list(self.close.keys())

    def __len__(self) -> int:
        return len(self.dates)

    def first_valid_index(self, code: str) -> int:
        """该标的首个有效数据的日期下标（无数据返回 len）。"""
        seq = self.close.get(code) or []
        for k, v in enumerate(seq):
            if v is not None:
                return k
        return len(self.dates)

    def valid_count(self, code: str) -> int:
        return sum(1 for v in (self.close.get(code) or []) if v is not None)

    def slice_dates(self, start: str = None, end: str = None) -> "AlignedPanel":
        lo = 0
        hi = len(self.dates)
        if start:
            while lo < hi and self.dates[lo] < start:
                lo += 1
        if end:
            while hi > lo and self.dates[hi - 1] > end:
                hi -= 1
        sub = AlignedPanel(dates=self.dates[lo:hi])
        for f in FIELDS:
            src = getattr(self, f)
            dst = getattr(sub, f)
            for code, seq in src.items():
                dst[code] = seq[lo:hi]
        return sub

    def stats(self) -> str:
        lines = [f"panel: {len(self.dates)} 交易日  "
                 f"{self.dates[0] if self.dates else '-'} ~ "
                 f"{self.dates[-1] if self.dates else '-'}  "
                 f"{len(self.codes)} 标的"]
        for code in sorted(self.codes):
            seq = self.close[code]
            ok = sum(1 for v in seq if v is not None)
            fi = self.first_valid_index(code)
            lines.append(f"  {code}  有效 {ok}/{len(seq)}  起始 "
                         f"{self.dates[fi] if fi < len(self.dates) else '-'}")
        return "\n".join(lines)


# ============================================================ 下载 / 读取

def _xtdata():
    from xtquant import xtdata
    return xtdata


def download(codes: Sequence[str], start: str = "20190101",
             end: str = "", period: str = "1d", verbose: bool = True) -> None:
    """把本地缺失的历史补齐（xtdata 落盘到 userdata_mini/datadir）。"""
    xtdata = _xtdata()
    total = len(codes)
    for idx, code in enumerate(codes, 1):
        try:
            xtdata.download_history_data(code, period, start, end)
            if verbose:
                print(f"  [{idx}/{total}] {code} 下载完成", flush=True)
        except Exception as e:
            if verbose:
                print(f"  [{idx}/{total}] {code} 下载失败: "
                      f"{type(e).__name__}: {e}", flush=True)


def _read_raw(codes: Sequence[str], start: str, end: str,
              period: str = "1d") -> Dict[str, Dict[str, list]]:
    """一次性读取多标的原始数据，返回 {code: {field: list, 'time': list}}。"""
    xtdata = _xtdata()
    out: Dict[str, Dict[str, list]] = {}
    try:
        raw = xtdata.get_market_data_ex(
            field_list=["time", *FIELDS],
            stock_list=list(codes), period=period,
            start_time=start, end_time=end or "",
            dividend_type=DIVIDEND_TYPE,
        )
    except TypeError:
        # 老版本 xtdata 不支持 dividend_type 关键字
        raw = xtdata.get_market_data_ex(
            field_list=["time", *FIELDS],
            stock_list=list(codes), period=period,
            start_time=start, end_time=end or "",
        )
    if not raw:
        return out
    for code in codes:
        d = raw.get(code)
        if d is None:
            continue
        if hasattr(d, "columns"):        # DataFrame
            cols = {c: list(d[c]) for c in d.columns}
            # DataFrame 的 index 通常就是日期字符串
            try:
                idx = [str(x) for x in d.index]
            except Exception:
                idx = []
        else:
            cols = dict(d)
            idx = []
        # 解析日期
        tlist = cols.get("time") or []
        dates: List[str] = []
        for k in range(len(tlist)):
            t = tlist[k]
            s = _to_date_str(t)
            if s is None and k < len(idx):
                s = _to_date_str(idx[k])
            dates.append(s or "")
        if not any(dates) and idx:
            dates = [(_to_date_str(x) or "") for x in idx]
        if not any(dates):
            continue
        rec = {"date": dates}
        n = len(dates)
        for f in FIELDS:
            src = cols.get(f) or []
            vals: List[Optional[float]] = []
            for k in range(n):
                x = src[k] if k < len(src) else None
                if f in ("volume", "amount"):
                    # 成交量允许为 0（停牌），但 NaN 归一成 0
                    try:
                        xf = float(x) if x is not None else 0.0
                    except Exception:
                        xf = 0.0
                    vals.append(0.0 if xf != xf else xf)
                else:
                    vals.append(None if _bad(x) else float(x))
            rec[f] = vals
        out[code] = rec
    return out


def _to_date_str(t) -> Optional[str]:
    """把 xtdata 的 time 字段统一成 YYYYMMDD。"""
    if t is None:
        return None
    if isinstance(t, str):
        s = t.strip()
        if len(s) >= 8 and s[:8].isdigit():
            return s[:8]
        return None
    try:
        v = int(t)
    except Exception:
        return None
    if v <= 0:
        return None
    # 8 位当日期用（20240101），13 位毫秒时间戳，10 位秒
    if 19000101 <= v <= 99991231:
        return str(v)
    try:
        ts = datetime.fromtimestamp(v / 1000 if v > 1e12 else v)
        return ts.strftime("%Y%m%d")
    except Exception:
        return None


# ============================================================ 对齐

def align(raw: Dict[str, Dict[str, list]],
          calendar_from: Sequence[str] = None) -> AlignedPanel:
    """把 {code: {date, o,h,l,c,v,a}} 对齐到统一日期轴。

    calendar_from: 指定基准日期轴（通常用指数）；None 时取所有标的日期并集。
    """
    if calendar_from:
        dates = sorted(set(calendar_from))
    else:
        s = set()
        for rec in raw.values():
            s.update(d for d in rec["date"] if d)
        dates = sorted(s)
    pos = {d: k for k, d in enumerate(dates)}
    panel = AlignedPanel(dates=dates)
    n = len(dates)
    for code, rec in raw.items():
        for f in FIELDS:
            getattr(panel, f)[code] = [None] * n
        for k, d in enumerate(rec["date"]):
            j = pos.get(d)
            if j is None:
                continue
            # 四价齐全才算有效交易日（上市前 NaN、停牌都会被挡掉）
            if any(_bad(rec[f][k]) for f in ("open", "high", "low", "close")):
                continue
            for f in FIELDS:
                getattr(panel, f)[code][j] = rec[f][k]
    return panel


# ============================================================ 对外入口

def load_panel(codes: Sequence[str], start: str = "20190101", end: str = "",
               calendar_code: str = None, do_download: bool = True,
               cache: bool = True, verbose: bool = True) -> AlignedPanel:
    """下载(可选) → 读取 → 对齐 → 缓存。"""
    codes = list(dict.fromkeys(codes))
    key_src = f"{'|'.join(sorted(codes))}|{start}|{end}|{calendar_code}"
    import hashlib
    key = hashlib.md5(key_src.encode()).hexdigest()[:16]
    cache_file = CACHE_DIR / f"panel_{key}.pkl"

    if cache and cache_file.exists():
        try:
            with cache_file.open("rb") as f:
                panel: AlignedPanel = pickle.load(f)
            if verbose:
                print(f"[hist_cache] 命中缓存 {cache_file.name}  "
                      f"{len(panel.dates)} 日 × {len(panel.codes)} 标的")
            return panel
        except Exception:
            pass

    if do_download:
        if verbose:
            print(f"[hist_cache] 下载 {len(codes)} 个标的 {start}~{end or 'now'} ...")
        download(codes, start, end, verbose=verbose)

    raw = _read_raw(codes, start, end)
    if verbose:
        print(f"[hist_cache] 读到 {len(raw)} 个标的原始序列")
    cal = None
    if calendar_code and calendar_code in raw:
        cal = [d for d in raw[calendar_code]["date"] if d]
    panel = align(raw, cal)

    if cache:
        try:
            with cache_file.open("wb") as f:
                pickle.dump(panel, f)
        except Exception:
            pass
    return panel


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20190101")
    ap.add_argument("--end", default="")
    ap.add_argument("--no-download", action="store_true")
    args = ap.parse_args()

    from config.settings import STOCK_CODES, INDEX_CODES
    codes = sorted(STOCK_CODES) + sorted(INDEX_CODES)
    panel = load_panel(codes, args.start, args.end,
                       do_download=not args.no_download, cache=False)
    print(panel.stats())


if __name__ == "__main__":
    main()

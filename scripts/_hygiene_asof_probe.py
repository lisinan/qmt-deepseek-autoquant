# -*- coding: utf-8 -*-
"""量测卫生巡检 #3b：快照 as-of 日期判定。

对同一批标的，分别用「截至 20261029」与「截至 20261030」的收盘序列复算
trend_up，看引擎日志中的 trend_up=2 对应哪一天 ⇒ 判断快照数据日期是否可考。
只读、不写生产文件。
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import indicators as I  # noqa: E402
from config.settings import INDEX_CODES, STOCK_CODES  # noqa: E402


def trend_up_of(closes, highs, lows) -> bool:
    ma20 = I.last(I.sma(closes, 20)) or 0.0
    ma60 = I.last(I.sma(closes, 60)) or 0.0
    slope = I.slope(I.sma(closes, 20), lookback=5) or 0.0
    _, _, hist = I.macd(closes, 12, 26, 9)
    macd_hist = I.last(hist) or 0.0
    close = closes[-1]
    return bool(ma20 and ma60 and close > ma20 and close > ma60
                and slope > 0 and macd_hist >= 0)


def main() -> int:
    from xtquant import xtdata

    dyn = ROOT / "storage" / "dynamic_universe.json"
    active = []
    if dyn.exists():
        active = list(json.loads(dyn.read_text(encoding="utf-8")).get("active_pool") or [])
    codes = list(dict.fromkeys(list(sorted(STOCK_CODES)) + active + list(sorted(INDEX_CODES))))
    # 只对个股（不含指数）统计 trend_up —— 与引擎漏斗口径一致
    stock_like = [c for c in codes if c not in INDEX_CODES]
    print(f"[宇宙] 候选个股 {len(stock_like)} 只（动态 {len(active)} 已并入）")

    data = xtdata.get_market_data_ex(
        field_list=["time", "open", "high", "low", "close", "volume"],
        stock_list=stock_like, period="1d", count=200,
        dividend_type="none", fill_data=False,
    )

    for cutoff in ("20261029", "20261030"):
        hits = []
        for code in stock_like:
            df = data.get(code)
            if df is None or len(df) < 80:
                continue
            ts = df["time"].astype("float64").values
            dates = [datetime.datetime.fromtimestamp(t / 1000).strftime("%Y%m%d") for t in ts]
            keep = [i for i, d in enumerate(dates) if d <= cutoff]
            if len(keep) < 80:
                continue
            c = [float(df["close"].values[i]) for i in keep]
            h = [float(df["high"].values[i]) for i in keep]
            lo = [float(df["low"].values[i]) for i in keep]
            if trend_up_of(c, h, lo):
                hits.append(f"{code}({round(c[-1], 2)})")
        print(f"[as-of {cutoff}] trend_up = {len(hits)} 只: {', '.join(hits)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

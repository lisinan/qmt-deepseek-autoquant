# -*- coding: utf-8 -*-
"""定位引擎「漏斗分层」快照的数据日期。

背景：2026-10-28 全天 25 条「漏斗分层」逐字节相同（trend_up 10 / score 16 /
最高分 603986.SH），而用同一批 38 只标的复算 10-27（trend_up 1 / score 2）与
10-28（2 / 3）都对不上 ⇒ 引擎内存里的日线特征很可能是更早某天的快照。

本脚本把最近 N 个交易日全部复算一遍，找出 trend_up==10 且 score_ok==16 的日期。

只读、零副作用。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import STRATEGY_PARAMS  # noqa: E402
from core import indicators as I  # noqa: E402
from strategy.backtest_daily import score_daily_series  # noqa: E402

LIVE_JSON = ROOT / "reports" / "why_no_entry" / "WHY_NO_ENTRY_LIVE.json"
SCORE_TH = float(STRATEGY_PARAMS.get("buy_score_threshold", 4.0))
MIN_SIG = int(STRATEGY_PARAMS.get("min_signals", 3))
TAIL = 120
WIN = 34                                  # 复算最近 N 个交易日


def fetch(codes: list) -> dict:
    import tushare as ts
    from data.tushare_client import tushare_client as T
    ts.set_token(T.token)
    pro = ts.pro_api(timeout=30)
    out = {}
    for c in codes:
        try:
            df = pro.daily(ts_code=c, start_date="20250401", end_date="20261028")
            if df is None or not len(df):
                continue
            df = df.sort_values("trade_date")
            out[c] = {
                "date": [str(x) for x in df["trade_date"]],
                "open": [float(x) for x in df["open"]],
                "high": [float(x) for x in df["high"]],
                "low": [float(x) for x in df["low"]],
                "close": [float(x) for x in df["close"]],
                "volume": [float(x) * 100.0 for x in df["vol"]],
            }
        except Exception as e:  # noqa: BLE001
            print(f"  [跳过] {c}: {e}")
    return out


def main() -> None:
    codes = [r["code"] for r in json.loads(
        LIVE_JSON.read_text(encoding="utf-8"))["rows"]]
    data = fetch(codes)
    axis = max((p["date"] for p in data.values()), key=len)[-WIN:]
    print(f"标的 {len(data)} 只，交易日 {len(axis)} 根："
          f"{axis[0]} → {axis[-1]}\n")
    print(f"{'日期':<10}{'N':>4}{'trend_up':>10}{'score_ok':>10}  最高分")
    hits = []
    for d in axis:
        rows = []
        for c, p in data.items():
            if d not in p["date"]:
                continue
            i = p["date"].index(d)
            if i < 70:
                continue
            lo = max(0, i + 1 - TAIL)
            cl = p["close"][lo:i + 1]
            o, h, l, v = (p["open"][lo:i + 1], p["high"][lo:i + 1],
                          p["low"][lo:i + 1], p["volume"][lo:i + 1])
            ma20 = I.last(I.sma(cl, 20)) or 0.0
            ma60 = I.last(I.sma(cl, 60)) or 0.0
            _, _, hist = I.macd(cl, 12, 26, 9)
            hv = I.last(hist)
            slope = I.slope(I.sma(cl, 20), 5) or 0.0
            close = cl[-1]
            tu = bool(ma20 and ma60 and close > ma20 and close > ma60
                      and slope > 0 and hv is not None and hv >= 0)
            sc, fac = score_daily_series(o, h, l, cl, v)[-1]
            pos = sum(1 for x in fac.values() if x and x > 0)
            rows.append((c, tu, sc, pos, sc >= SCORE_TH and pos >= MIN_SIG))
        tu_n = sum(1 for r in rows if r[1])
        sc_n = sum(1 for r in rows if r[4])
        best = max(rows, key=lambda r: r[2]) if rows else None
        mark = ""
        if tu_n == 10 and sc_n == 16:
            mark = "   <<< 命中引擎日志(10/16)"
            hits.append(d)
        print(f"{d:<10}{len(rows):>4}{tu_n:>10}{sc_n:>10}  "
              f"{best[0] if best else '-'} {best[2] if best else 0:.2f}{mark}")
    print(f"\n命中日期: {hits or '无'}")


if __name__ == "__main__":
    main()

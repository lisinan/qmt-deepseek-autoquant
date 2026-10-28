# -*- coding: utf-8 -*-
"""数据日期对齐检验：引擎「漏斗分层」日志到底基于哪一天的日线？

已验证事实（2026-10-28）：
  - quant_system.log 全天 25 条「漏斗分层」逐字节相同（09:30:34 → 15:00:44）
  - daily_context.FEATURE_TTL_SEC = 6*3600 ⇒ 09:30 建立的特征快照整天不刷新
  - _expected_last_trade_date: 盘中（<15:05）= 上一交易日
  ⇒ 假设：日志中的 trend_up=10 / score=16 是 **10-27 收盘**口径，而非 10-28。

本脚本用同一批 38 只标的、同一公式（daily_context 口径，取最近 120 根），
逐日复算 trend_up / score 通过数，检验假设。

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
TAIL = 120                      # = daily_context.DAILY_COUNT
DATES = ("20261022", "20261023", "20261024", "20261027", "20261028")


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
    print(f"标的数 {len(codes)}（与实盘 38 只同集合）")
    data = fetch(codes)
    print(f"取到 {len(data)} 只\n")

    for d in DATES:
        rows = []
        for c, p in data.items():
            if d not in p["date"]:
                continue
            i = p["date"].index(d)
            if i < 70:
                continue
            lo = max(0, i + 1 - TAIL)                 # 实盘只看最近 120 根
            cl = p["close"][lo:i + 1]
            o = p["open"][lo:i + 1]
            h = p["high"][lo:i + 1]
            l = p["low"][lo:i + 1]
            v = p["volume"][lo:i + 1]
            ma20 = I.last(I.sma(cl, 20)) or 0.0
            ma60 = I.last(I.sma(cl, 60)) or 0.0
            _, _, hist = I.macd(cl, 12, 26, 9)
            hv = I.last(hist)
            slope = I.slope(I.sma(cl, 20), 5) or 0.0
            close = cl[-1]
            tu = bool(ma20 and ma60 and close > ma20 and close > ma60
                      and slope > 0 and hv is not None and hv >= 0)
            sc, fac = score_daily_series(o, h, l, c_ := cl, v)[-1]
            pos = sum(1 for x in fac.values() if x and x > 0)
            rows.append({"code": c, "trend_up": tu, "score": sc,
                         "pos": pos,
                         "score_ok": sc >= SCORE_TH and pos >= MIN_SIG})
        tu_n = sum(1 for r in rows if r["trend_up"])
        sc_n = sum(1 for r in rows if r["score_ok"])
        best = max(rows, key=lambda r: r["score"]) if rows else None
        print(f"{d}  N={len(rows):>2}  trend_up={tu_n:>2}  "
              f"score_ok={sc_n:>2}  最高分={best['code']} {best['score']:.2f}"
              if best else f"{d} 无数据")


if __name__ == "__main__":
    main()

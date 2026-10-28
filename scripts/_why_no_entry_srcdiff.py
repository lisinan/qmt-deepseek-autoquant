# -*- coding: utf-8 -*-
"""数据源差异定位：引擎用 xtdata 本地日线，研究侧用 tushare 面板。

前两步已确认：
  - 引擎漏斗日志每日固定一个快照（09:15~09:30 建立），日内 25 条完全相同
  - 该快照按日变化：10-19~10-23 = 0, 10-26 = 2, 10-27 = 4, 10-28 = 10
  - tushare 复算：10-27 = 1, 10-28 = 2 —— 系统性偏低

本脚本用 xtdata（qmt_client，与实盘同源）逐只取日线，打印：
  - 每只标的的**最后一根 bar 日期**（判定本地数据新鲜度）
  - 逐日 trend_up / score_ok 复算，与引擎日志逐日对照
  - 同一只标的 xtdata vs tushare 的收盘价差异（判定复权口径）

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
LOG = {"20261023": 0, "20261026": 2, "20261027": 4, "20261028": 10}


def main() -> None:
    codes = [r["code"] for r in json.loads(
        LIVE_JSON.read_text(encoding="utf-8"))["rows"]]
    from core.qmt_client import qmt_client  # noqa: PLC0415
    if hasattr(qmt_client, "connect"):
        qmt_client.connect()

    series: dict = {}
    lastday: dict = {}
    for c in codes:
        raw = qmt_client.get_history(c, period="1d", count=200) or []
        if len(raw) < 70:
            print(f"  [数据不足] {c} {len(raw)} 根")
            continue
        series[c] = {
            "date": [b["ts"].strftime("%Y%m%d") for b in raw],
            "open": [b["open"] for b in raw],
            "high": [b["high"] for b in raw],
            "low": [b["low"] for b in raw],
            "close": [b["close"] for b in raw],
            "volume": [b["volume"] for b in raw],
        }
        lastday[c] = series[c]["date"][-1]

    import collections
    print("本地日线末根日期分布:", dict(collections.Counter(lastday.values())))
    print(f"可用 {len(series)} 只\n")

    axis = sorted({d for p in series.values() for d in p["date"][-8:]})
    print(f"{'日期':<10}{'N':>4}{'trend_up':>10}{'score_ok':>10}  最高分"
          f"   引擎日志")
    for d in axis:
        rows = []
        for c, p in series.items():
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
        flag = LOG.get(d)
        tag = f"  引擎={flag}" if flag is not None else ""
        ok = " ✓MATCH" if flag == tu_n else ""
        print(f"{d:<10}{len(rows):>4}{tu_n:>10}{sc_n:>10}  "
              f"{best[0] if best else '-'} {best[2] if best else 0:.2f}"
              f"{tag}{ok}")

    # 复权口径：xtdata vs tushare 末日收盘
    print("\n--- xtdata vs tushare 末日收盘（前 8 只）---")
    import tushare as ts
    from data.tushare_client import tushare_client as T
    ts.set_token(T.token)
    pro = ts.pro_api(timeout=30)
    for c in list(series)[:8]:
        try:
            df = pro.daily(ts_code=c, start_date="20261020", end_date="20261028")
            df = df.sort_values("trade_date")
            t_row = df.iloc[-1]
            print(f"  {c:<11} xtdata {series[c]['close'][-1]:>9.2f} "
                  f"({lastday[c]})  tushare {t_row['close']:>9.2f} "
                  f"({t_row['trade_date']})")
        except Exception as e:  # noqa: BLE001
            print(f"  {c}: {e}")


if __name__ == "__main__":
    main()

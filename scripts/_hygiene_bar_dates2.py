# -*- coding: utf-8 -*-
"""量测卫生巡检 #1b：有效候选集（静态 ∪ 动态池）日线末根日期（只读）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import INDEX_CODES, STOCK_CODES  # noqa: E402


def main() -> int:
    from xtquant import xtdata

    dyn_path = ROOT / "storage" / "dynamic_universe.json"
    active = []
    if dyn_path.exists():
        d = json.loads(dyn_path.read_text(encoding="utf-8"))
        active = list(d.get("active_pool") or [])

    codes = list(dict.fromkeys(list(sorted(STOCK_CODES)) + active + list(sorted(INDEX_CODES))))
    print(f"[有效集] 静态 {len(STOCK_CODES)} + 动态 {len(active)} + 指数 {len(INDEX_CODES)} "
          f"→ 去重 {len(codes)}")

    # 最近交易日（xtdata 交易日历）
    try:
        cal = xtdata.get_trading_dates("SH", start_time="20260901", end_time="20261231")
        last_td = max(cal) if cal else None
        import datetime
        print("[交易日历] 最近交易日 =",
              datetime.datetime.fromtimestamp(last_td / 1000).strftime("%Y%m%d")
              if last_td else "未获取")
    except Exception as e:  # noqa: BLE001
        print(f"[交易日历] 未获取: {e}")

    data = xtdata.get_market_data_ex(
        field_list=["time", "close"], stock_list=codes, period="1d",
        count=5, dividend_type="none", fill_data=False,
    )
    import datetime
    from collections import defaultdict
    buckets: dict[str, list[str]] = defaultdict(list)
    for code in codes:
        df = data.get(code)
        if df is None or len(df) == 0:
            buckets["NO_DATA"].append(code)
            continue
        ts = float(df.iloc[-1]["time"])
        d = datetime.datetime.fromtimestamp(ts / 1000).strftime("%Y%m%d")
        buckets[d].append(code)

    for d in sorted(buckets):
        names = buckets[d]
        print(f"[{d}] {len(names)} 只: {', '.join(names) if len(names) <= 40 else str(len(names))+'只'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

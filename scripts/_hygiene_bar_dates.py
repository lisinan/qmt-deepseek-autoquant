# -*- coding: utf-8 -*-
"""量测卫生巡检 #1：读静态宇宙 + 指数的日线末根日期（只读）。

不刷新、不落盘生产文件；仅打印各标的最后一根日线 bar 日期与收盘。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import INDEX_CODES, STOCK_CODES  # noqa: E402


def main() -> int:
    from xtquant import xtdata

    stock_codes = sorted(STOCK_CODES)
    index_codes = sorted(INDEX_CODES)
    print(f"[宇宙] 个股 {len(stock_codes)} / 指数 {len(index_codes)} "
          f"/ 合计 {len(stock_codes) + len(index_codes)}")

    rows = []
    for code in stock_codes + index_codes:
        try:
            data = xtdata.get_market_data_ex(
                field_list=["time", "close"],
                stock_list=[code],
                period="1d",
                count=5,
                dividend_type="none",
                fill_data=False,
            )
            df = data.get(code)
            if df is None or len(df) == 0:
                rows.append((code, "NO_DATA", None, None, 0))
                continue
            last = df.iloc[-1]
            rows.append((code, str(last.get("time")), float(last.get("close")),
                          int(df.index[-1]) if not isinstance(df.index[-1], str) else df.index[-1],
                          len(df)))
        except Exception as e:  # noqa: BLE001
            rows.append((code, f"ERR:{type(e).__name__}:{e}", None, None, 0))

    for code, t, close, _, n in rows:
        print(f"  {code:12s} last_time={t} close={close} bars={n}")

    dates = [r[1][:8] for r in rows if r[1] and r[1][:1].isdigit()]
    uniq = sorted(set(dates))
    print(f"[末根日期分布] 唯一取值 {len(uniq)} 种: {uniq}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

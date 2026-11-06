# -*- coding: utf-8 -*-
"""量测卫生巡检 #1b：有效候选集（静态 ∪ 动态池）日线末根日期（只读）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import INDEX_CODES, STOCK_CODES, VALIDATION_UNIVERSE  # noqa: E402


def main() -> int:
    from xtquant import xtdata

    dyn_path = ROOT / "storage" / "dynamic_universe.json"
    active = []
    if dyn_path.exists():
        d = json.loads(dyn_path.read_text(encoding="utf-8"))
        active = list(d.get("active_pool") or [])

    # 【2026-11-06 DAILY-REVIEW SAFE 修复】原检查池漏掉 VALIDATION_UNIVERSE。
    # 后果：510050.SH 是唯一"只靠 tick、不在 _daily_codes() 里"的标的，其日线
    # 自 2026-11-03 起结构性断更（今日实测真实末根 20261103，落后 3 个交易日），
    # 而本脚本只查 37 只 ⇒ 断更对巡检**完全不可见**，连续 4 天报"全部最新"。
    # 回滚：删掉下面 VALIDATION_UNIVERSE 一项即可（纯观测脚本，不受控于引擎）。
    # 注意：fill_data=False 必须保留 —— 默认 True 会给缺失标的**前值填充假 bar**
    # （实测批量取数时 510050 被补出 11-04/05/06 三根 2.884 的假收盘，dropna 也拦不住）。
    codes = list(dict.fromkeys(list(sorted(STOCK_CODES)) + active
                               + list(sorted(INDEX_CODES))
                               + list(VALIDATION_UNIVERSE or ())))
    print(f"[有效集] 静态 {len(STOCK_CODES)} + 动态 {len(active)} + 指数 {len(INDEX_CODES)} "
          f"+ 验证 {len(VALIDATION_UNIVERSE or ())} → 去重 {len(codes)}")

    # 最近交易日（xtdata 交易日历）
    last_td = None
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

    # 【2026-11-06 DAILY-REVIEW SAFE】把「落后几个交易日」显式算出来 —— 原来只分组打印，
    # 落后标的混在另一个日期桶里，人眼扫不出来，正是 510050 断更 4 天没人发现的原因。
    if last_td:
        import datetime
        want = datetime.datetime.fromtimestamp(last_td / 1000).strftime("%Y%m%d")
        lag = {d: v for d, v in buckets.items() if d != want}
        if lag:
            print(f"[落后] 相对最近交易日 {want}："
                  + "; ".join(f"{d} → {len(v)} 只（{', '.join(v)}）" for d, v in sorted(lag.items())))
        else:
            print(f"[落后] 无，全部 {len(codes)} 只末根 = {want}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

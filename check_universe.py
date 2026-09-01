# -*- coding: utf-8 -*-
"""检查扩展宇宙（STOCK_CODES + SECTOR_CONFIG）的日线数据可用性。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config.settings import STOCK_CODES, SECTOR_CONFIG  # noqa: E402
from strategy.backtest_daily import load_daily  # noqa: E402

sector_codes = set()
for _k, v in SECTOR_CONFIG["sectors"].items():
    for code, _name in v["stocks"]:
        sector_codes.add(code)

wide = sorted(STOCK_CODES | sector_codes)
print(f"STOCK_CODES={len(STOCK_CODES)}  SECTOR={len(sector_codes)}  "
      f"合并去重={len(wide)}")
print("-" * 70)

ok, bad = [], []
lens = {}
for code in wide:
    d = load_daily(code, 500)
    if d and len(d["close"]) >= 300:
        ok.append(code)
        lens[code] = len(d["close"])
    else:
        bad.append((code, len(d["close"]) if d else 0))

print(f"可用(>=300根): {len(ok)}")
for c in ok:
    print(f"  {c}  bars={lens[c]}")
if bad:
    print(f"\n不可用/过短: {len(bad)}")
    for c, L in bad:
        print(f"  {c}  bars={L}")

print("\n长度分布:", sorted(set(lens.values())))
# 日期轴一致性
if ok:
    d0 = load_daily(ok[0], 500)
    print(f"样本日期范围: {d0['date'][0]} -> {d0['date'][-1]}")
print("\nWIDE_OK = " + repr(ok))

# -*- coding: utf-8 -*-
"""北向资金(沪深港通净买入)数据可达性探测 —— 决定是否启动 northbound_cache 研发。

市场级信号(沪股通+深股通每日净买入)，非个股级；适合组合层择时/风险预算调制，
而非选股排名(已证伪噪声大的路子)。
"""
from __future__ import annotations
import sys
sys.path.insert(0, ".")
from data.tushare_client import tushare_client

print("enabled:", tushare_client.enabled)
if not tushare_client._connect():
    print("PRO_CONNECT_FAIL"); sys.exit()
pro = tushare_client._pro

# tushare 市场级北向接口候选
trials = [
    ("moneyflow_hsgt(trade_date)", dict(trade_date="20240902")),
    ("moneyflow_hsgt(range)", dict(start_date="20240901", end_date="20240912")),
    ("hsgt_fund_flow(trade_date)", dict(trade_date="20240902")),
]
found = None
for label, kw in trials:
    try:
        df = pro.moneyflow_hsgt(**kw) if "moneyflow_hsgt" in label else pro.hsgt_fund_flow(**kw)
        if df is not None and not df.empty:
            print(f"[OK] {label} -> shape={df.shape}")
            print("cols:", list(df.columns))
            print(df.head(3).to_string())
            found = label
            break
        else:
            print(f"[EMPTY] {label}")
    except Exception as e:
        print(f"[ERR] {label}: {repr(e)[:200]}")

if found is None:
    print("NORTHBOUND_UNREACHABLE")
else:
    print("NORTHBOUND_OK:", found)

# -*- coding: utf-8 -*-
"""PM-EVOLVE R31 (2026-10-14) 全日真实度量：读 storage/qmt.db"""
import sqlite3, json, os
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "storage", "qmt.db")
con = sqlite3.connect(DB)
cur = con.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tabs = [r[0] for r in cur.fetchall()]
print("TABLES:", tabs)
def q(sql):
    try:
        cur.execute(sql); return cur.fetchall()
    except Exception as e:
        return [("ERR", str(e))]
print("\n--- engine_state ---")
for r in q("SELECT * FROM engine_state"):
    print(r)
print("\n--- equity_snapshots 2026-10-14 首/末 ---")
for r in q("SELECT ts,total_asset,cash,market_value,positions_n FROM equity_snapshots WHERE ts LIKE '2026-10-14%' ORDER BY ts LIMIT 3"):
    print("FIRST", r)
for r in q("SELECT ts,total_asset,cash,market_value,positions_n FROM equity_snapshots WHERE ts LIKE '2026-10-14%' ORDER BY ts DESC LIMIT 3"):
    print("LAST", r)
print("count:", q("SELECT COUNT(*) FROM equity_snapshots WHERE ts LIKE '2026-10-14%'")[0])
print("\n--- fills 2026-10-14 ---")
for r in q("SELECT * FROM fills WHERE ts LIKE '2026-10-14%' "):
    print(r)
print("\n--- 最近 8 日 EOD ---")
for r in q("""SELECT substr(ts,1,10) d, COUNT(*), MIN(total_asset), MAX(total_asset)
              FROM equity_snapshots GROUP BY d ORDER BY d DESC LIMIT 8"""):
    print(r)

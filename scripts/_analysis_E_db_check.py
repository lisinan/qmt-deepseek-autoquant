# -*- coding: utf-8 -*-
"""E_data_governance 核查脚本 Part4: qmt.db 时效核查(只读)"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "storage" / "qmt.db"

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
cur = con.cursor()
tables = [r[0] for r in cur.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
print("表清单:", tables)

for t in ["equity_snapshots", "signals", "orders", "fills", "engine_state"]:
    if t not in tables:
        print(f"\n[{t}] 不存在")
        continue
    cols = [r[1] for r in cur.execute(f"PRAGMA table_info({t})")]
    n = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f"\n[{t}] 行数={n} 列={cols}")
    # 找时间列
    tcol = None
    for cand in ("ts", "timestamp", "trade_date", "date", "created_at", "time"):
        if cand in cols:
            tcol = cand
            break
    if tcol:
        mn, mx = cur.execute(f"SELECT MIN({tcol}), MAX({tcol}) FROM {t}").fetchone()
        print(f"  时间列 {tcol}: min={mn} max={mx}")
    # 打印最后3行(截断宽列)
    rows = cur.execute(f"SELECT * FROM {t} ORDER BY rowid DESC LIMIT 3").fetchall()
    for r in rows:
        s = str(r)
        print("  末行:", s[:220])

con.close()
print("\nDB 文件 mtime:", DB.stat().st_mtime)
import datetime
print("DB 文件修改时间:", datetime.datetime.fromtimestamp(DB.stat().st_mtime))

# -*- coding: utf-8 -*-
"""OWNER 指令单通道的只读体检（2026-11-10）。

下午测试下单时，每发一条指令就跑一次这个，一次看全四样：
  指令队列（还没被处理的）/ 幂等账本（每条指令的终态）/
  今日 orders 与 fills（引擎到底有没有真的下单、成交）/
  当前持仓与现金（账面是否自洽）。

只读：SQLite 用 mode=ro，指令文件只读不改。不 import 项目模块。
用法：
    python scripts/owner_order_check.py
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORAGE = ROOT / "storage"
DB = STORAGE / "qmt.db"
QUEUE = STORAGE / "owner_orders.jsonl"
DONE = STORAGE / "owner_orders_done.json"


def _hr(t):
    print("\n" + "=" * 62)
    print(t)
    print("=" * 62)


def main() -> int:
    _hr("1) 指令队列  storage/owner_orders.jsonl")
    if not QUEUE.exists():
        print("（文件不存在 = 队列为空，安全）")
    else:
        lines = [ln for ln in io.open(QUEUE, encoding="utf-8").read().splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        print("行数:", len(lines))
        for ln in lines:
            print("  ", ln[:120])

    _hr("2) 幂等账本  storage/owner_orders_done.json")
    if not DONE.exists():
        print("（尚无任何指令被执行过）")
    else:
        try:
            done = json.loads(io.open(DONE, encoding="utf-8").read() or "{}")
        except Exception as e:                                   # noqa: BLE001
            print("解析失败:", e)
            done = {}
        if not done:
            print("（空）")
        for k in sorted(done):
            v = done[k]
            print("  %-22s %-8s %s  %s" % (k, v.get("status"),
                                          v.get("ts", ""), (v.get("reason") or "")[:60]))

    if not DB.exists():
        print("\n[ERR] 找不到库:", DB)
        return 2
    con = sqlite3.connect("file:%s?mode=ro" % DB.as_posix(), uri=True)

    _hr("3) 今日 orders / fills")
    oc = [c[1] for c in con.execute("PRAGMA table_info(orders)")]
    rows = list(con.execute(
        "select * from orders where ts like '2026-11-10%' order by id desc limit 8"))
    print("orders:", len(rows), "行")
    for r in rows:
        d = dict(zip(oc, r))
        print("  %s %-6s %-10s qty=%-5s px=%-9s order_id=%s" % (
            d.get("ts", "")[:19], d.get("side"), d.get("code"),
            d.get("quantity"), d.get("price"), d.get("order_id")))
    fc = [c[1] for c in con.execute("PRAGMA table_info(fills)")]
    rows = list(con.execute(
        "select * from fills where ts like '2026-11-10%' order by id desc limit 8"))
    print("fills :", len(rows), "行")
    for r in rows:
        d = dict(zip(fc, r))
        print("  %s %-6s %-10s qty=%-5s px=%s" % (
            d.get("ts", "")[:19], d.get("side"), d.get("code"),
            d.get("quantity"), d.get("price")))

    _hr("4) 当前持仓 / 现金（engine_state）")
    row = con.execute("select ts,cash,day_open_asset,peak_asset,positions "
                      "from engine_state where id=1").fetchone()
    if row:
        print("ts=%s  cash=%s  day_open=%s  peak=%s" % (row[0], row[1], row[2], row[3]))
        try:
            for p in json.loads(row[4] or "[]"):
                print("  %-10s qty=%-6s avg=%-10s last=%-10s open_date=%s" % (
                    p.get("code"), p.get("quantity"),
                    round(float(p.get("avg_cost") or 0), 4),
                    p.get("last_price"), p.get("open_date")))
        except Exception as e:                                   # noqa: BLE001
            print("  持仓解析失败:", e)
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""核实 2026-10-08 复市首日：成交、熔断、持仓、信号"""
import sqlite3, json

c = sqlite3.connect('storage/qmt.db')
c.row_factory = sqlite3.Row
cur = c.cursor()

print('=' * 88)
print('1) 今日 fills')
cur.execute("select * from fills where ts like '2026-10-08%' order by ts")
for r in cur.fetchall():
    d = dict(r)
    print('  %s %s %s x%-6s @%-9s amount=%-11s reason=%s'
          % (d['ts'][11:19], d['code'], d['side'], d['quantity'],
             d['price'], round(d['amount'], 2), str(d.get('reason'))[:60]))

print()
print('=' * 88)
print('2) engine_state')
cur.execute("select * from engine_state limit 1")
r = cur.fetchone()
if r:
    d = dict(r)
    for k, v in d.items():
        print('  %-20s %s' % (k, str(v)[:150]))

print()
print('=' * 88)
print('3) 近 6 日 EOD 权益')
cur.execute("""
select substr(ts,1,10) d,
 (select total_asset from equity_snapshots e2 where substr(e2.ts,1,10)=substr(e1.ts,1,10)
  order by e2.ts desc limit 1) eod, count(*) n
from equity_snapshots e1 group by d order by d desc limit 8
""")
for r in cur.fetchall():
    print('  %s  EOD=%-14s n=%s' % (r['d'], r['eod'], r['n']))

print()
print('=' * 88)
print('4) 今日退出理由（notices / 系统提示交易）')
try:
    cur.execute("select * from signals where ts like '2026-10-08%' and side='SELL' order by ts limit 10")
    rows = cur.fetchall()
    print('  SELL signals:', len(rows))
    for r in rows:
        print('   ', r['ts'][11:19], r['code'], r['score'], str(r['reason'])[:90])
except Exception as e:
    print('  err', e)

c.close()

# -*- coding: utf-8 -*-
"""PM-EVOLVE 2026-10-09 全日度量：成交 / 权益 / 风控状态 / 信号闸门"""
import sqlite3, json

c = sqlite3.connect('storage/qmt.db')
c.row_factory = sqlite3.Row
cur = c.cursor()

DAY = '2026-10-09'

print('=' * 90)
print('1) 当日 fills (%s)' % DAY)
cur.execute("select * from fills where ts like ? order by ts", (DAY + '%',))
rows = cur.fetchall()
tot = 0.0
for r in rows:
    d = dict(r)
    print('  %s %s %s x%-6s @%-9s amount=%-11s reason=%s'
          % (d['ts'][11:19], d['code'], d['side'], d['quantity'],
             d['price'], round(d['amount'], 2), str(d.get('reason'))[:60]))
print('  合计 %d 笔' % len(rows))

print()
print('=' * 90)
print('2) 当日 equity_snapshots (首尾 + 笔数)')
cur.execute("select ts, total_asset, cash, market_value, positions_count from equity_snapshots where ts like ? order by ts", (DAY + '%',))
snaps = cur.fetchall()
if snaps:
    print('  首 %s  tot=%s cash=%s mv=%s pos=%s' % (snaps[0]['ts'][11:19], snaps[0]['total_asset'], snaps[0]['cash'], snaps[0]['market_value'], snaps[0]['positions_count']))
    print('  末 %s  tot=%s cash=%s mv=%s pos=%s' % (snaps[-1]['ts'][11:19], snaps[-1]['total_asset'], snaps[-1]['cash'], snaps[-1]['market_value'], snaps[-1]['positions_count']))
    print('  n=%d' % len(snaps))
    vals = set(round(float(s['total_asset'] or 0), 2) for s in snaps)
    print('  唯一权益值个数=%d  %s' % (len(vals), sorted(vals)[:6]))
else:
    print('  无快照')

print()
print('=' * 90)
print('3) 近 8 日 EOD 权益')
cur.execute("""
select substr(ts,1,10) d,
 (select total_asset from equity_snapshots e2 where substr(e2.ts,1,10)=substr(e1.ts,1,10)
  order by e2.ts desc limit 1) eod, count(*) n,
 (select count(*) from fills f where substr(f.ts,1,10)=substr(e1.ts,1,10)) fills
from equity_snapshots e1 group by d order by d desc limit 8
""")
for r in cur.fetchall():
    print('  %s  EOD=%-14s n=%-5s fills=%s' % (r['d'], r['eod'], r['n'], r['fills']))

print()
print('=' * 90)
print('4) engine_state / risk_state')
cur.execute("select * from engine_state limit 1")
r = cur.fetchone()
if r:
    for k, v in dict(r).items():
        print('  %-22s %s' % (k, str(v)[:220]))

print()
print('=' * 90)
print('5) 当日 positions / 持仓')
for t in ('positions', 'paper_positions'):
    try:
        cur.execute("select * from %s" % t)
        rr = cur.fetchall()
        print('  [%s] n=%d' % (t, len(rr)))
        for x in rr[:8]:
            print('   ', {k: str(v)[:40] for k, v in dict(x).items()})
    except Exception as e:
        print('  [%s] %s' % (t, e))

print()
print('=' * 90)
print('6) 当日 signal / decision 日志表探测')
cur.execute("select name from sqlite_master where type='table' order by name")
print('  tables:', [x[0] for x in cur.fetchall()])

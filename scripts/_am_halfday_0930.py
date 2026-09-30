# -*- coding: utf-8 -*-
"""AM-EVOLVE 半日度量脚本（2026-09-30 午休窗口）"""
import sqlite3, json, datetime

db = 'storage/qmt.db'
c = sqlite3.connect(db)
c.row_factory = sqlite3.Row
cur = c.cursor()

print('=' * 70)
print('1) equity_snapshots 今日（2026-09-30）')
cur.execute("select * from equity_snapshots where ts like '2026-09-30%' order by ts")
rows = cur.fetchall()
print('今日快照数:', len(rows))
if rows:
    print('列:', [k for k in rows[0].keys()])
    first, last = rows[0], rows[-1]
    tot = [r['total_asset'] for r in rows]
    print('首:', first['ts'], {k: first[k] for k in first.keys() if k != 'ts'})
    print('末:', last['ts'], {k: last[k] for k in last.keys() if k != 'ts'})
    print('min/max total:', min(tot), max(tot))

print()
print('=' * 70)
print('2) 最近 10 个交易日的 EOD（每日最后一条）')
cur.execute("""
select substr(ts,1,10) d, count(*) n, min(total_asset), max(total_asset),
       (select total_asset from equity_snapshots e2 where substr(e2.ts,1,10)=substr(e1.ts,1,10) order by e2.ts desc limit 1) eod
from equity_snapshots e1 group by d order by d desc limit 12
""")
for r in cur.fetchall():
    print(' ', r['d'], 'n=%-4d' % r['n'], 'min=%.2f max=%.2f EOD=%.2f' % (r[2], r[3], r['eod']))

print()
print('=' * 70)
print('3) engine_state')
cur.execute("select * from engine_state limit 3")
for r in cur.fetchall():
    print(dict(r))

print()
print('=' * 70)
print('4) risk_snapshots 今日最后几条')
cur.execute("select * from risk_snapshots where ts like '2026-09-30%' order by ts desc limit 3")
rr = cur.fetchall()
for r in rr:
    print(dict(r))
if not rr:
    cur.execute("select * from risk_snapshots order by ts desc limit 3")
    for r in cur.fetchall():
        print(dict(r))

print()
print('=' * 70)
print('5) 今日 fills')
cur.execute("select * from fills where ts like '2026-09-30%' order by ts")
fl = cur.fetchall()
print('今日成交数:', len(fl))
for r in fl:
    d = dict(r)
    print(' ', d.get('ts'), d.get('code'), d.get('side'), d.get('price'), d.get('qty'), d.get('amount'),
          str(d.get('reason'))[:110])

print()
print('=' * 70)
print('6) 今日 signals 统计')
cur.execute("select count(*) from signals where ts like '2026-09-30%'")
print('signals 行数:', cur.fetchone()[0])
try:
    cur.execute("select * from signals where ts like '2026-09-30%' order by ts desc limit 5")
    for r in cur.fetchall():
        d = dict(r)
        print('  ', {k: (str(v)[:80]) for k, v in d.items()})
except Exception as e:
    print('err', e)

print()
print('=' * 70)
print('7) 引擎是否重启过（fills 连续性 / 最早今日记录）')
cur.execute("select min(ts), max(ts), count(*) from fills")
print('fills 全表:', cur.fetchone()[:])
c.close()

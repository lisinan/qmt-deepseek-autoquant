# -*- coding: utf-8 -*-
"""直接取 get_full_tick，看现在返回什么价"""
import sys, time
sys.path.insert(0, '.')
from xtquant import xtdata

codes = ['300394.SZ', '000977.SZ', '300308.SZ', '688008.SH', '002415.SZ',
         '300502.SZ', '603986.SH', '688012.SH', '688498.SH']

for code in codes:
    try:
        t = xtdata.get_full_tick([code])
        v = (t or {}).get(code)
        if not v:
            print('%-11s EMPTY' % code); continue
        print('%-11s price=%-10s lastClose=%-10s open=%-10s high=%-10s low=%-10s time=%s  vol=%s'
              % (code, v.get('lastPrice'), v.get('lastClose'), v.get('open'),
                 v.get('high'), v.get('low'), v.get('time'), v.get('volume')))
    except Exception as e:
        print(code, 'ERR', e)

print()
print('=== get_market_data_ex 1m 最近 3 根 ===')
for code in codes[:5]:
    try:
        d = xtdata.get_market_data_ex(['close'], [code], period='1m', count=3)
        df = d.get(code)
        if df is None:
            print(code, 'NO 1m'); continue
        print(code, [(str(i)[:16], round(float(x), 3)) for i, x in zip(df.index, df['close'])])
    except Exception as e:
        print(code, 'ERR', e)

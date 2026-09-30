# -*- coding: utf-8 -*-
"""核对 09-30 真实行情 vs 成交价"""
import sys, os
sys.path.insert(0, '.')
try:
    from xtquant import xtdata
except Exception as e:
    print('xtdata import fail:', e)
    sys.exit(1)

codes = ['300394.SZ', '000977.SZ', '300308.SZ', '688008.SH', '002415.SZ',
         '300502.SZ', '603986.SH', '688012.SH', '688498.SH']

print('=' * 90)
print('标的  末根日期  最近 5 根 close')
for code in codes:
    try:
        d = xtdata.get_market_data_ex(['close', 'open', 'high', 'low', 'volume'], [code], period='1d', count=6)
        df = d.get(code)
        if df is None or len(df) == 0:
            print(code, 'NO DATA')
            continue
        df = df.dropna(how='all')
        idx = [str(i)[:10] for i in df.index]
        cl = [round(float(x), 3) for x in df['close'].tolist()]
        print('%-11s %s  %s' % (code, idx[-1], list(zip(idx, cl))))
    except Exception as e:
        print(code, 'ERR', e)

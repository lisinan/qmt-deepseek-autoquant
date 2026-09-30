# -*- coding: utf-8 -*-
"""验证 09-30 的成交价 = _MockClient 的随机游走价（铁证）"""
import sys, random
sys.path.insert(0, '.')
from core.qmt_client import _MockClient

fills = [
    ('300394.SZ', 200, 89.818, 267.93),
    ('000977.SZ', 1500, 44.873, 69.90),
    ('300308.SZ', 100, 132.376, 800.00),
    ('688008.SH', 400, 106.499, 209.18),
    ('002415.SZ', 5500, 26.747, 32.63),
]
print('=' * 84)
print('%-11s %10s %10s %10s %10s %8s' % ('code', 'mock_base', '成交价', '真实收盘', '成本', 'mock偏离'))
real = {'300394.SZ': 255.99, '000977.SZ': 67.18, '300308.SZ': 813.01,
        '688008.SH': 212.60, '002415.SZ': 32.11}
for code, qty, px, cost in fills:
    base = _MockClient.BASE_PRICES.get(code, 100.0)
    print('%-11s %10.2f %10.3f %10.2f %10.2f %+7.2f%%  | 真实价偏离 %+.2f%%'
          % (code, base, px, real[code], cost, (px / base - 1) * 100, (px / real[code] - 1) * 100))

print()
print('=' * 84)
print('模拟 mock 随机游走：从 BASE_PRICES 起步，每轮 ±0.3%，看能否到达成交价')
for code, qty, px, cost in fills:
    base = _MockClient.BASE_PRICES.get(code, 100.0)
    random.seed(20260930)
    prev = base
    hit_round = None
    lo = hi = base
    for r in range(1, 60001):
        chg = random.uniform(-0.3, 0.3)
        prev = round(prev * (1 + chg / 100), 3)
        lo, hi = min(lo, prev), max(hi, prev)
        if hit_round is None and abs(prev - px) / px < 0.005:
            hit_round = r
    print('%-11s base=%-8.2f 目标=%-9.3f 6万轮内区间=[%.2f, %.2f] 命中轮次=%s'
          % (code, base, px, lo, hi, hit_round))

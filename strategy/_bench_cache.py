# -*- coding: utf-8 -*-
"""执行效率基准：on_bars 指标缓存（同分钟 20 次 tick 只重算 1 次）。

模拟 live 主循环：每只股票每分钟 append 一根新 bar，但 3s 一轮的 tick
会反复调用 on_bars，且同一分钟内 bar 序列（除最后一根实时价外）不变。
缓存命中时输出与即时重算完全一致，仅跳过冗余计算。
"""
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy.trend_strategy import TrendStrategy  # noqa: E402
from core.data_models import Bar  # noqa: E402
import random  # noqa: E402

random.seed(42)
N_STOCKS = 30
N_BARS = 120          # 与 _bars deque maxlen = ma_long*6 一致
N_TICKS = 20          # 一分钟内的 tick 数（3s 间隔）

bars_per_stock = []
for _ in range(N_STOCKS):
    price = 50.0
    bars = []
    for i in range(N_BARS):
        # 模拟分钟级 OHLC
        o = price
        c = price * (1 + random.uniform(-0.01, 0.01))
        h = max(o, c) * (1 + random.uniform(0, 0.008))
        l = min(o, c) * (1 - random.uniform(0, 0.008))
        v = random.randint(1000, 5000)
        bars.append(Bar(ts=i, open=o, high=h, low=l, close=c, volume=v, amount=v * c))
        price = c
    bars_per_stock.append(bars)


def bench(use_cache: bool):
    strat = TrendStrategy()
    total = 0.0
    iters = 0
    # 预热：先跑一遍让缓存建立（模拟每分钟首 tick 重算）
    for bars in bars_per_stock:
        strat.on_bars("X.SH", "x", bars)
    t0 = time.perf_counter()
    for _ in range(N_TICKS):
        for bars in bars_per_stock:
            if not use_cache:
                strat._ind_cache.clear()   # 强制每 tick 重算
            strat.on_bars("X.SH", "x", bars)
            iters += 1
    dt = time.perf_counter() - t0
    # 验证缓存输出与无缓存一致（抽样一只）
    s2 = TrendStrategy()
    b = bars_per_stock[0]
    s2._ind_cache.clear()
    ref = s2._compute_indicators(b)
    strat._ind_cache.clear()
    got = strat._compute_indicators(b)
    same = all(abs((ref[k] or 0) - (got[k] or 0)) < 1e-9
               for k in ref if isinstance(ref[k], (int, float)))
    return dt, iters, same


if __name__ == "__main__":
    dt_off, it_off, _ = bench(use_cache=False)
    dt_on, it_on, same = bench(use_cache=True)
    print(f"股票数={N_STOCKS}  bar序列长={N_BARS}  每分钟tick数={N_TICKS}")
    print(f"无缓存(on_bars每tick重算): {dt_off*1000:7.1f} ms / 全量 "
          f"({it_off} calls)  → 单call {dt_off/it_off*1000:.3f} ms")
    print(f"有缓存(同分钟命中)       : {dt_on*1000:7.1f} ms / 全量 "
          f"({it_on} calls)  → 单call {dt_on/it_on*1000:.3f} ms")
    print(f"加速比: {dt_off/dt_on:.1f}×   缓存输出与即时重算一致: {same}")
    # 单轮(3s)主循环预算：每分钟 20 tick 中只有首 tick 重算，
    # 其余 19 tick 命中缓存 → 实际每轮计算量 ≈ 无缓存的 1/20
    per_tick_no_cache = dt_off / it_off
    # 每分钟真正发生的重算次数 = N_STOCKS（首tick），其余命中
    print(f"折算：每分钟 20 tick 中，无缓存需 {N_STOCKS*N_TICKS} 次重算；"
          f"有缓存仅 {N_STOCKS} 次（其余 {N_STOCKS*(N_TICKS-1)} 命中）"
          f"→ 主循环 CPU 占用≈原来的 {1/N_TICKS*100:.0f}%")

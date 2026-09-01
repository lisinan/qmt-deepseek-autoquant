# -*- coding: utf-8 -*-
"""参数扫描：真实日线数据上搜索最优退出/仓位参数（数据只拉一次）。"""
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from strategy.backtest_daily import (run_backtest, BacktestConfig,  # noqa
                                      load_daily, STOCK_CODES)  # noqa

codes = list(STOCK_CODES)
# 数据只拉一次
print("加载日线数据...", flush=True)
data = {}
for c in codes:
    d = load_daily(c, 260)
    if d:
        data[c] = d
print(f"已加载 {len(data)} 只", flush=True)

combos = []
for atr_stop in [1.5, 2.0, 2.5]:
    for tp in [3.0, 4.0, 5.0]:
        for act in [0.05, 0.08, 0.12]:
            for tr in [-0.04, -0.06, -0.08]:
                for mh in [20, 30]:
                    combos.append((atr_stop, tp, act, tr, mh))

results = []
for i, (atr_stop, tp, act, tr, mh) in enumerate(combos):
    cfg = BacktestConfig(
        use_gate=True, stop_loss=-0.04, take_profit=0.12,
        atr_stop_mult=atr_stop, tp_atr_mult=tp,
        trailing=True, trailing_activation=act,
        trailing_stop=tr, trailing_floor=-0.005,
        vol_sizing=True, max_hold_days=mh,
        cost_pct=0.0015, chandelier=False,
    )
    r = run_backtest(codes, cfg, preloaded=data)
    if "total_return" not in r:
        continue
    results.append((r["total_return"], r["sharpe"], r["win_rate"],
                    r["max_drawdown"], atr_stop, tp, act, tr, mh,
                    r["n_trades"], r["avg_win"], r["avg_loss"]))
    print(f"[{i+1}/{len(combos)}] ret={r['total_return']*100:6.1f}% "
          f"sh={r['sharpe']:5.2f} win={r['win_rate']*100:4.1f}% "
          f"mdd={r['max_drawdown']*100:5.1f}% stop={atr_stop} tp={tp} "
          f"act={act} tr={tr} mh={mh}", flush=True)

results.sort(reverse=True)
print("\n===== 按累计收益降序 前 12 =====")
print(f"{'ret%':>7} {'sh':>5} {'win%':>6} {'mdd%':>7} {'stop':>5} {'tp':>4} {'act':>5} {'tr':>5} {'mh':>3} {'#tr':>4} {'w%':>6} {'l%':>6}")
for row in results[:12]:
    ret, sh, wr, mdd, ast, tp, act, tr, mh, ntr, aw, al = row
    print(f"{ret*100:6.1f} {sh:5.2f} {wr*100:6.1f} {mdd*100:7.1f} "
          f"{ast:5.1f} {tp:4.1f} {act:5.2f} {tr:5.2f} {mh:3d} {ntr:4d} "
          f"{aw*100:6.1f} {al*100:6.1f}")

print("\n===== 按 Sharpe 降序 前 8 =====")
for row in sorted(results, key=lambda x: x[1], reverse=True)[:8]:
    ret, sh, wr, mdd, ast, tp, act, tr, mh, ntr, aw, al = row
    print(f"sh={sh:5.2f} ret={ret*100:6.1f}% win={wr*100:5.1f}% "
          f"mdd={mdd*100:6.1f}% stop={ast} tp={tp} act={act} tr={tr} mh={mh}")

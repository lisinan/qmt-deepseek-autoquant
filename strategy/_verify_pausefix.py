# -*- coding: utf-8 -*-
"""验证 RiskManager −10%→−0.25 修复：实盘建模(LIVE) 不再永久停牌，收益≈回测理想。"""
from __future__ import annotations
import sys
from pathlib import Path
from dataclasses import replace
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from strategy.opt_harness import wide_universe, preload, slice_by_index, base_cfg, FIXED_WARMUP  # noqa
from strategy.backtest_daily import run_backtest  # noqa
from config.settings import INDEX_CODES  # noqa

COUNT, IS_END, FOLD = 750, 350, 75
codes = wide_universe()
data = preload(codes + ["399006.SZ", "000300.SH"], COUNT)
n = min(len(d["close"]) for d in data.values())
is_end = min(IS_END, n - 120)
oos_lo = max(0, is_end - FIXED_WARMUP)
is_data = slice_by_index(data, 0, is_end)
oos_data = slice_by_index(data, oos_lo, n)
folds = [(s, slice_by_index(data, s - FIXED_WARMUP, min(s + FOLD, n)))
         for s in range(FIXED_WARMUP, n - FOLD + 1, FOLD)]
base = base_cfg()

def bt(cfg, dset):
    ks = [k for k in dset.keys() if k not in INDEX_CODES]
    return run_backtest(ks, cfg, count=COUNT, preloaded=dset)

print(f"验证：rm_dd_pause_pct=-0.25 + recoverable（修复后实盘建模）\n")
for rpt in (0.02, 0.03, 0.04):
    cfg_off = replace(base, risk_per_trade=rpt, rm_dd_pause_pct=0.0)
    cfg_fix = replace(base, risk_per_trade=rpt, rm_dd_pause_pct=-0.25,
                      rm_dd_pause_recoverable=True)
    ro_off = bt(cfg_off, oos_data)
    ro_fix = bt(cfg_fix, oos_data)
    # folds
    sh_off = []; sh_fix = []; halt_fix = 0
    for s, dsub in folds:
        r = bt(cfg_off, dsub); sh_off.append(r["sharpe"])
        r2 = bt(cfg_fix, dsub); sh_fix.append(r2["sharpe"])
        if r2.get("rm_halted_ever"): halt_fix += 1
    moff = sum(sh_off)/len(sh_off); mfix = sum(sh_fix)/len(sh_fix)
    print(f"rpt={rpt}:")
    print(f"  OFF     OOS ret={ro_off['total_return']*100:+7.2f}%  Sh={ro_off['sharpe']:+5.2f}  MDD={ro_off['max_drawdown']*100:6.2f}%")
    print(f"  FIX     OOS ret={ro_fix['total_return']*100:+7.2f}%  Sh={ro_fix['sharpe']:+5.2f}  MDD={ro_fix['max_drawdown']*100:6.2f}%  halt={ro_fix['rm_halted_days']}d  fired={ro_fix['rm_halted_ever']}")
    print(f"  多折均值Sh: OFF={moff:+.2f}  FIX={mfix:+.2f}  FIX触发折={halt_fix}/{len(folds)}")
    match = "✓ 修复后实盘≈回测" if (not ro_fix['rm_halted_ever'] and abs(ro_fix['total_return']-ro_off['total_return'])<0.05) else "✗ 仍有偏差"
    print(f"  -> {match}")

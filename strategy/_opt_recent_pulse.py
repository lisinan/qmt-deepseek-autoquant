# -*- coding: utf-8 -*-
"""最新行情脉冲验证：当前生产配置(regime off + -0.25 冷却断路器)在最新鲜数据上的表现。
重点隔离「最近窗口(覆盖 2026-04→08 最新行情)」与全 walk-forward 折序列，确认策略仍盈利、
且实盘断路器不会误触发（否则收益无法兑现）。
"""
from __future__ import annotations
import sys, time, json
from pathlib import Path
from dataclasses import replace
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from strategy.opt_harness import wide_universe, preload, slice_by_index, base_cfg, FIXED_WARMUP  # noqa
from strategy.backtest_daily import run_backtest  # noqa
from config.settings import INDEX_CODES  # noqa

COUNT, FOLD = 750, 75
codes = wide_universe()
t0 = time.time()
data = preload(codes + ["399006.SZ", "000300.SH"], COUNT)
n = min(len(d["close"]) for d in data.values())
dates = data[codes[0]]["date"]
print(f"[数据] {len(data)} 只 × {n} 根日线 (末日 {dates[-1]})  载入 {time.time()-t0:.1f}s")

def bt(cfg, dset):
    ks = [k for k in dset.keys() if k not in INDEX_CODES]
    return run_backtest(ks, cfg, count=COUNT, preloaded=dset)

prod = replace(base_cfg(), rm_dd_pause_pct=-0.25, rm_dd_pause_recoverable=True)   # 实盘断路器建模
raw  = replace(base_cfg(), rm_dd_pause_pct=0.0)                                    # 无暂停基线

KEYS = ("total_return", "sharpe", "max_drawdown", "alpha", "exposure",
        "rm_halted_days", "rm_halted_ever")

def report(name, dsub):
    r = bt(prod, dsub)
    rb = bt(raw, dsub)
    df, dt = dsub[codes[0]]["date"][0], dsub[codes[0]]["date"][-1]
    ln = len(dsub[codes[0]]["date"])
    print(f"\n=== {name}  日期 {df} → {dt} ({ln} 根) ===")
    print(f"  生产(断路器-0.25): ret={r['total_return']*100:+7.2f}%  Sh={r['sharpe']:+.2f}  "
          f"MDD={r['max_drawdown']*100:6.2f}%  α={r['alpha']*100:+6.1f}pt  exp={r['exposure']*100:.0f}%  "
          f"halt={r.get('rm_halted_days',0)}d fired={r.get('rm_halted_ever',False)}")
    print(f"  基线(无暂停)     : ret={rb['total_return']*100:+7.2f}%  Sh={rb['sharpe']:+.2f}  "
          f"MDD={rb['max_drawdown']*100:6.2f}%  α={rb['alpha']*100:+6.1f}pt")
    delta = r["total_return"] - rb["total_return"]
    flag = "✓ 休眠(收益≈基线)" if (abs(delta) < 0.02 and not r.get("rm_halted_ever")) else "✗ 触发"
    print(f"  断路器影响(ret差): {delta*100:+.2f}pt  -> {flag}")
    prod_d = {k: r.get(k) for k in KEYS}
    raw_d = {k: rb.get(k) for k in ("total_return", "sharpe", "max_drawdown", "alpha", "exposure")}
    return {"name": name, "date_from": df, "date_to": dt, "prod": prod_d, "raw": raw_d}

out = {"generated": time.strftime("%Y-%m-%d %H:%M"), "n_bars": n, "last_date": dates[-1]}
out["full_oos"] = report("全样本 (含最新行情)", slice_by_index(data, 0, n))
lo3 = n - 250
out["recent_window"] = report(f"最近 ~250 根 (剔除预热后约120根可交易, 末日 {dates[-1]})",
                               slice_by_index(data, lo3, n))

starts = list(range(FIXED_WARMUP, n - FOLD + 1, FOLD))
out["folds"] = []
for i, s in enumerate(starts):
    dsub = slice_by_index(data, s - FIXED_WARMUP, min(s + FOLD, n))
    r = bt(prod, dsub)
    df, dt = dsub[codes[0]]["date"][0], dsub[codes[0]]["date"][-1]
    print(f"\n=== Fold{i+1}/{len(starts)}  日期 {df} → {dt} ===")
    print(f"  ret={r['total_return']*100:+7.2f}%  Sh={r['sharpe']:+.2f}  MDD={r['max_drawdown']*100:6.2f}%  "
          f"α={r['alpha']*100:+6.1f}pt  exp={r['exposure']*100:.0f}%  halt={r.get('rm_halted_days',0)}d")
    fold_d = {"fold": i + 1, "date_from": df, "date_to": dt}
    for k in KEYS:
        fold_d[k] = r.get(k)
    out["folds"].append(fold_d)

(ROOT / "logs").mkdir(exist_ok=True)
(ROOT / "logs" / "opt_recent_pulse.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print("\n[已保存] logs/opt_recent_pulse.json")

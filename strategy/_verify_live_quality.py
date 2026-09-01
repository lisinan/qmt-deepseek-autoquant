# -*- coding: utf-8 -*-
"""模拟盘前的生产质量复验：用最新实时数据（xtdata）重跑当前生产配置，
确认「科学分析→优秀收益」在当前代码上仍可复现（全样本 + 7 折 walk-forward）。
仅验证 P0（现状无 regime）+ 实盘断路器建模，不参与任何参数改动。
"""
from __future__ import annotations
import sys, time, json
from pathlib import Path
from dataclasses import replace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from strategy.opt_harness import (wide_universe, preload, slice_by_index,
                                  base_cfg, FIXED_WARMUP)
from strategy.backtest_daily import run_backtest
from config.settings import INDEX_CODES

COUNT = 750
codes = wide_universe()
t0 = time.time()
data = preload(codes + ["399006.SZ", "000300.SH"], COUNT)
n = min(len(d["close"]) for d in data.values())
dates = data[codes[0]]["date"]
print(f"[数据] {len(data)} 只 × {n} 根 (末日 {dates[-1]})  载入 {time.time()-t0:.1f}s")

# 生产配置（与 settings.STRATEGY_PARAMS 对应）
cfg = base_cfg()
# 实盘断路器建模：收益兑现能力的最终判据
prod = replace(cfg, rm_dd_pause_pct=-0.25, rm_dd_pause_recoverable=True)
raw = replace(cfg, rm_dd_pause_pct=0.0)
ks = [k for k in data if k not in INDEX_CODES]

# ---------------- 全样本 ----------------
r = run_backtest(ks, prod, count=COUNT, preloaded=data)
print(f"\n[全样本 生产配置(断路器-0.25)]")
print(f"  收益={r['total_return']*100:+.2f}%  Sharpe={r['sharpe']:+.2f}  "
      f"Sortino={r['sortino']:+.2f}  MDD={r['max_drawdown']*100:.2f}%  "
      f"Calmar={r['calmar']:.2f}  PF={r['profit_factor']:.2f}  "
      f"n={r['n_trades']}  win={r['win_rate']*100:.1f}%  "
      f"exposure={r['exposure']*100:.0f}%  alpha={r['alpha']*100:+.2f}pt  "
      f"halt={r.get('rm_halted_days',0)}d fired={r.get('rm_halted_ever',False)}")
rb = run_backtest(ks, raw, count=COUNT, preloaded=data)
delta = r["total_return"] - rb["total_return"]
flag = "✓ 断路器休眠(收益≈基线,可兑现)" if (abs(delta) < 0.02 and not r.get("rm_halted_ever")) else "✗ 触发"
print(f"  基线(无暂停) ret={rb['total_return']*100:+.2f}%  "
      f"断路器影响(ret差)={delta*100:+.2f}pt -> {flag}")

# ---------------- 7 折 walk-forward P0 ----------------
FOLD = 75
starts = list(range(FIXED_WARMUP, n - FOLD + 1, FOLD))
shs, pos_alpha, worst = [], 0, 999.0
print(f"\n[7折 walk-forward 生产配置] {len(starts)} 窗")
for k, s in enumerate(starts):
    lo, hi = s - FIXED_WARMUP, min(s + FOLD, n)
    dsub = slice_by_index(data, lo, hi)
    rr = run_backtest(ks, prod, count=COUNT, preloaded=dsub)
    shs.append(rr["sharpe"])
    if rr["alpha"] > 0:
        pos_alpha += 1
    worst = min(worst, rr["total_return"])
    print(f"  Fold{k+1}: {data[codes[0]]['date'][s]}→{data[codes[0]]['date'][min(s+FOLD,n)-1]} "
          f"ret={rr['total_return']*100:+.2f}% Sh={rr['sharpe']:+.2f} "
          f"a={rr['alpha']*100:+.1f}pt halt={rr.get('rm_halted_days',0)}d")
msh = sum(shs) / len(shs)
print(f"\n  均值Sharpe={msh:+.2f}  正alpha={pos_alpha}/{len(starts)}  最差折={worst*100:+.2f}%")

out = {
    "date_to": dates[-1], "n_stocks": len(ks), "n_bars": n,
    "full": {k: r.get(k) for k in ("total_return", "sharpe", "sortino",
            "max_drawdown", "calmar", "profit_factor", "n_trades",
            "win_rate", "alpha", "exposure", "rm_halted_days", "rm_halted_ever")},
    "raw_full_return": rb["total_return"],
    "folds_mean_sharpe": msh, "folds_pos_alpha": pos_alpha,
    "folds": len(starts), "worst_fold": worst,
}
Path("logs/verify_live_quality.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
print("\n[保存] logs/verify_live_quality.json")

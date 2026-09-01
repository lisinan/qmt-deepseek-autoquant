# -*- coding: utf-8 -*-
"""
全新特征验证：量能突破确认（vol_confirm）

纪律（与 opt_harness 一致）：
  - IS/OOS 分离 + 多折 walk-forward 双验证
  - 对比等权买入持有基准，alpha 为正且多折稳健才考虑并入
  - 一切含真实交易成本（0.15% 单边）

用法：
  python strategy/_opt_volconfirm.py
"""
from __future__ import annotations
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import MARKET_INDEX_CODE, INDEX_CODES  # noqa: E402
from strategy.opt_harness import (  # noqa: E402
    wide_universe, preload, slice_by_index, base_cfg, FIXED_WARMUP,
)
from strategy.backtest_daily import run_backtest  # noqa: E402

COUNT = 500
IS_END = 350
REG = dict(regime_mode="index", regime_index=MARKET_INDEX_CODE,
           regime_ma=60, regime_force_exit=True)
FOLD = 75


def fmt(tag, r):
    if not r or "error" in r:
        return f"{tag:<22} ERROR {r.get('error','') if r else ''}"
    return (f"{tag:<22} ret={r['total_return']*100:+7.2f}% "
            f"Sh={r['sharpe']:+5.2f} So={r['sortino']:+5.2f} "
            f"MDD={r['max_drawdown']*100:7.2f}% Cal={r['calmar']:5.2f} "
            f"PF={r['profit_factor']:4.2f} n={r['n_trades']:>3} "
            f"win={r['win_rate']*100:4.1f}% exp={r['exposure']*100:4.1f}% "
            f"a={r['alpha']*100:+6.2f}pt")


def main():
    codes = wide_universe()
    t0 = time.time()
    data = preload(codes + [MARKET_INDEX_CODE, "000300.SH"], COUNT)
    print(f"[数据] {len(data)} 只 × {COUNT} 根  载入 {time.time()-t0:.1f}s")
    if not data:
        return

    n = min(len(d["close"]) for d in data.values())
    is_end = min(IS_END, n - 120)
    oos_lo = max(0, is_end - FIXED_WARMUP)
    is_data = slice_by_index(data, 0, is_end)
    oos_data = slice_by_index(data, oos_lo, n)
    print(f"[窗口] 总 {n} | IS=[{FIXED_WARMUP},{is_end}) "
          f"OOS=[{is_end},{n}) | IS日 {is_data[codes[0]]['date'][0]} "
          f"-> {is_data[codes[0]]['date'][-1]} | OOS {oos_data[codes[0]]['date'][0]} "
          f"-> {oos_data[codes[0]]['date'][-1]}")

    def bt(cfg, dset):
        ks = [k for k in dset.keys() if k not in INDEX_CODES]
        return run_backtest(ks, cfg, count=COUNT, preloaded=dset)

    base = base_cfg()
    vc = base_cfg()
    vc.vol_confirm = True
    vc.vol_confirm_mult = 1.5
    vc.vol_confirm_ma = 20
    vc.vol_confirm_tol = 0.03

    print("\n" + "=" * 120)
    print("IS / OOS 双段验证：当前生产配置 vs 量能突破确认(vol_confirm)")
    print("=" * 120)
    ri_b, ro_b = bt(base, is_data), bt(base, oos_data)
    ri_v, ro_v = bt(vc, is_data), bt(vc, oos_data)
    print(f"\n{fmt('BASE(IS)', ri_b)}")
    print(f"{fmt('BASE(OOS)', ro_b)}")
    print(f"{fmt('VOLC(IS)', ri_v)}")
    print(f"{fmt('VOLC(OOS)', ro_v)}")
    print(f"\n{'等权买入持有':<22} IS={ri_b['bench_return']*100:+.2f}% "
          f"OOS={ro_b['bench_return']*100:+.2f}%")
    print(f"\n--- 增量（VOLC - BASE）---")
    print(f"  IS  : ret {(ri_v['total_return']-ri_b['total_return'])*100:+.2f}pt "
          f"Sh {ri_v['sharpe']-ri_b['sharpe']:+.2f} "
          f"alpha {(ri_v['alpha']-ri_b['alpha'])*100:+.2f}pt "
          f"win {(ri_v['win_rate']-ri_b['win_rate'])*100:+.1f}pt")
    print(f"  OOS : ret {(ro_v['total_return']-ro_b['total_return'])*100:+.2f}pt "
          f"Sh {ro_v['sharpe']-ro_b['sharpe']:+.2f} "
          f"alpha {(ro_v['alpha']-ro_b['alpha'])*100:+.2f}pt "
          f"win {(ro_v['win_rate']-ro_b['win_rate'])*100:+.1f}pt "
          f"MDD {(ro_v['max_drawdown']-ro_b['max_drawdown'])*100:+.2f}pt")

    # ---- 多折 walk-forward ----
    print("\n" + "=" * 120)
    print("多折 walk-forward（稳健性）：BASE vs VOLC，逐折收益差 (VOLC-BASE)")
    print("=" * 120)
    starts = list(range(FIXED_WARMUP, n - FOLD + 1, FOLD))
    d0 = data[codes[0]]["date"]
    base_rs, vc_rs = [], []
    for k, s in enumerate(starts):
        lo, hi = s - FIXED_WARMUP, min(s + FOLD, n)
        dsub = slice_by_index(data, lo, hi)
        rb = bt(base, dsub)
        rv = bt(vc, dsub)
        base_rs.append(rb)
        vc_rs.append(rv)
        if "error" not in rb and "error" not in rv:
            d = (rv["total_return"] - rb["total_return"]) * 100
            print(f"  Fold{k+1} {d0[s]}->{d0[min(s+FOLD,n)-1]}: "
                  f"BASE={rb['total_return']*100:+.1f}% "
                  f"VOLC={rv['total_return']*100:+.1f}%  Δ={d:+.1f}pt "
                  f"(Sh_B={rb['sharpe']:+.2f} Sh_V={rv['sharpe']:+.2f})")
    sh_b = sum(r["sharpe"] for r in base_rs if "error" not in r) / len(base_rs)
    sh_v = sum(r["sharpe"] for r in vc_rs if "error" not in r) / len(vc_rs)
    pa_b = sum(1 for r in base_rs if "error" not in r and r["alpha"] > 0)
    pa_v = sum(1 for r in vc_rs if "error" not in r and r["alpha"] > 0)
    print(f"\n  均值Sharpe: BASE={sh_b:+.2f}  VOLC={sh_v:+.2f}  "
          f"(Δ={sh_v-sh_b:+.2f})")
    print(f"  正alpha折数: BASE={pa_b}/{len(base_rs)}  VOLC={pa_v}/{len(vc_rs)}")

    # ---- 裁决 ----
    oos_alpha_v = ro_v["alpha"]
    verdict = ("并入" if (oos_alpha_v > 0 and sh_v >= sh_b
                          and pa_v >= pa_b) else "拒绝")
    print(f"\n=== 裁决：VOLC OOS alpha={oos_alpha_v*100:+.2f}pt "
          f"多折均值Sh Δ={sh_v-sh_b:+.2f} → 【{verdict}】 ===")

    out = ROOT / "logs" / "opt_volconfirm.json"
    out.parent.mkdir(exist_ok=True)
    payload = {
        "verdict": verdict,
        "base": {"IS": _kv(ri_b), "OOS": _kv(ro_b)},
        "vol_confirm": {"IS": _kv(ri_v), "OOS": _kv(ro_v)},
        "folds_base": [_kv(r) for r in base_rs],
        "folds_vc": [_kv(r) for r in vc_rs],
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"[保存] {out}")


def _kv(r):
    if not r or "error" in r:
        return {"error": r.get("error", "unknown") if r else "none"}
    return {k: r.get(k) for k in
            ("total_return", "sharpe", "sortino", "max_drawdown", "calmar",
             "profit_factor", "n_trades", "win_rate", "alpha", "exposure")}


if __name__ == "__main__":
    main()

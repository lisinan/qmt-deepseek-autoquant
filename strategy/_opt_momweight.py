# -*- coding: utf-8 -*-
"""
动量排名加权仓位（momentum_weight）严格验证
—— 建立在「当前已验证生产配置」之上：
   regime(创业板指 MA60)+force_exit + trend 骑行 + 波动率目标 + max_positions=5

纪律：只有 OOS alpha 为正「且」多折 walk-forward 稳健，才建议并入生产。
判定：与基准(同配置无加权)逐折对比，看累计差与正alpha折数。
"""
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import MARKET_INDEX_CODE, INDEX_CODES  # noqa: E402
from strategy.opt_harness import (  # noqa: E402
    base_cfg, wide_universe, preload, slice_by_index,
)
from strategy.backtest_daily import run_backtest  # noqa: E402

FIXED_WARMUP = 130


def bt(cfg, dset):
    ks = [k for k in dset.keys() if k not in INDEX_CODES]
    return run_backtest(ks, cfg, count=500, preloaded=dset)


def main():
    codes = wide_universe()
    t0 = time.time()
    data = preload(codes + [MARKET_INDEX_CODE], 500)
    print(f"[数据] {len(data)} 只 × 500 日线  载入 {time.time()-t0:.1f}s")
    if not data:
        print("无数据")
        return

    n = min(len(d["close"]) for d in data.values())
    IS_END = min(350, n - 120)
    OOS_LO = max(0, IS_END - FIXED_WARMUP)
    is_data = slice_by_index(data, 0, IS_END)
    oos_data = slice_by_index(data, OOS_LO, n)
    print(f"[窗口] 总 {n} | IS=[{FIXED_WARMUP},{IS_END}) "
          f"OOS=[{IS_END},{n}) 无重叠")

    REG = dict(regime_mode="index", regime_index=MARKET_INDEX_CODE,
               regime_ma=60, regime_force_exit=True)
    base = replace(base_cfg(), **REG)          # 当前生产（无加权）
    cand = replace(base, momentum_weight=True)  # 候选（加权）

    def fmt(tag, r):
        if "error" in r:
            return f"{tag} ERROR {r['error']}"
        return (f"{tag} ret={r['total_return']*100:+7.2f}% Sh={r['sharpe']:+5.2f} "
                f"MDD={r['max_drawdown']*100:7.2f}% Cal={r['calmar']:5.2f} "
                f"n={r['n_trades']:>3} win={r['win_rate']*100:4.1f}% "
                f"exp={r['exposure']*100:4.1f}% a={r['alpha']*100:+6.2f}pt")

    # ---------------- IS / OOS ----------------
    ri_b, ro_b = bt(base, is_data), bt(base, oos_data)
    ri_c, ro_c = bt(cand, is_data), bt(cand, oos_data)
    print("\n=== 样本内 / 样本外 ===")
    print(fmt("BASE(IS) ", ri_b))
    print(fmt("CAND(IS) ", ri_c))
    print(fmt("BASE(OOS)", ro_b))
    print(fmt("CAND(OOS)", ro_c))
    print(f"\nOOS 对比: ret { (ro_c['total_return']-ro_b['total_return'])*100:+.2f}pt"
          f"  Sharpe {ro_c['sharpe']-ro_b['sharpe']:+.2f}"
          f"  alpha { (ro_c['alpha']-ro_b['alpha'])*100:+.2f}pt")

    # ---------------- 多折 walk-forward ----------------
    FOLD = 75
    starts = list(range(FIXED_WARMUP, n - FOLD + 1, FOLD))
    print(f"\n=== 多折 walk-forward（{len(starts)} 窗 × {FOLD} 日）===")
    base_folds, cand_folds = [], []
    for k, s in enumerate(starts):
        lo, hi = s - FIXED_WARMUP, min(s + FOLD, n)
        dsub = slice_by_index(data, lo, hi)
        base_folds.append(bt(base, dsub))
        cand_folds.append(bt(cand, dsub))
        rb, rc = base_folds[-1], cand_folds[-1]
        d = (rc["total_return"] - rb["total_return"]) * 100
        print(f"  F{k+1} base={rb['total_return']*100:+6.1f}% "
              f"cand={rc['total_return']*100:+6.1f}%  Δ={d:+5.1f}pt"
              f"  Sh {rc['sharpe']:+5.2f} vs {rb['sharpe']:+5.2f}")

    wins = sum(1 for rb, rc in zip(base_folds, cand_folds)
               if rc["total_return"] > rb["total_return"])
    tot = sum((rc["total_return"] - rb["total_return"]) * 100
              for rb, rc in zip(base_folds, cand_folds))
    palpha_c = sum(1 for r in cand_folds if r["alpha"] > 0)
    msh_b = sum(r["sharpe"] for r in base_folds) / len(base_folds)
    msh_c = sum(r["sharpe"] for r in cand_folds) / len(cand_folds)
    print(f"\n逐折胜: {wins}/{len(starts)}  累计差: {tot:+.1f}pt"
          f"  候选正alpha折: {palpha_c}/{len(starts)}"
          f"  均值Sh cand={msh_c:+.2f} vs base={msh_b:+.2f}")

    # ---------------- 裁决 ----------------
    oos_alpha_ok = (ro_c["alpha"] - ro_b["alpha"]) > 0
    robust = (msh_c >= msh_b - 0.05) and (tot > -5.0)
    verdict = "并入(通过)" if (oos_alpha_ok and robust) else "拒绝(证据不足)"
    print(f"\n裁决: OOS alpha 改善={oos_alpha_ok}  多折稳健={robust}  -> {verdict}")

    out = ROOT / "logs" / "opt_momweight.json"
    payload = {
        "verdict": verdict,
        "oos": {"base": {k: ro_b[k] for k in ("total_return", "sharpe",
                 "max_drawdown", "alpha", "exposure")},
                "cand": {k: ro_c[k] for k in ("total_return", "sharpe",
                 "max_drawdown", "alpha", "exposure")}},
        "is": {"base": {k: ri_b[k] for k in ("total_return", "sharpe", "alpha")},
               "cand": {k: ri_c[k] for k in ("total_return", "sharpe", "alpha")}},
        "folds": {"wins": wins, "total": len(starts), "cum_diff_pt": tot,
                  "cand_pos_alpha": palpha_c, "mean_sh_base": msh_b,
                  "mean_sh_cand": msh_c},
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"[保存] {out}")


if __name__ == "__main__":
    main()

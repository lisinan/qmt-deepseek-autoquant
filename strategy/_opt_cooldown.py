# -*- coding: utf-8 -*-
"""
再入场冷静期验证：降低动量重排名带来的无谓换手、削减成本侵蚀。

纪律：建立在已验证生产配置之上；IS/OOS + 多折 walk-forward 双验证，
只有两者都改善才并入生产。绝不重复已被否决的方向。
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import MARKET_INDEX_CODE
from strategy.opt_harness import wide_universe, preload, slice_by_index, base_cfg
from strategy.backtest_daily import run_backtest

FIXED_WARMUP = 130
REG = dict(regime_mode="index", regime_index=MARKET_INDEX_CODE,
           regime_ma=60, regime_force_exit=True)
INDEX_CODES = {"000001.SH", "399001.SZ", "399006.SZ", "000300.SH"}


def bt(cfg, dset):
    ks = [k for k in dset.keys() if k not in INDEX_CODES]
    return run_backtest(ks, cfg, count=500, preloaded=dset)


def main():
    codes = wide_universe()
    t0 = time.time()
    data = preload(codes + [MARKET_INDEX_CODE, "000300.SH"], 500)
    print(f"[数据] {len(data)} 只 × 500 根  载入 {time.time()-t0:.1f}s")
    if not data:
        print("无数据"); return
    n = min(len(d["close"]) for d in data.values())
    IS_END = min(350, n - 120)
    OOS_LO = max(0, IS_END - FIXED_WARMUP)
    is_data = slice_by_index(data, 0, IS_END)
    oos_data = slice_by_index(data, OOS_LO, n)

    b = base_cfg()
    E0 = replace(b, **REG)
    cands = {
        "E0_base_生产": E0,
        "C1_cool5": replace(E0, reentry_cooldown=5),
        "C2_cool10": replace(E0, reentry_cooldown=10),
        "C3_cool20": replace(E0, reentry_cooldown=20),
    }

    print("\n" + "=" * 138)
    print("IS / OOS 双段验证（再入场冷静期）")
    print("=" * 138)
    print(f"{'配置':<18}{'段':<5}{'收益':>10}{'Sharpe':>9}{'Sortino':>8}"
          f"{'MDD':>9}{'Calmar':>8}{'PF':>6}{'笔数':>5}{'胜率':>7}{'持仓':>7}{'alpha':>9}")
    print("-" * 138)
    rows = []
    for name, cfg in cands.items():
        ri = bt(cfg, is_data); ro = bt(cfg, oos_data)
        rows.append((name, cfg, ri, ro))
        for tag, r in (("IS", ri), ("OOS", ro)):
            if "error" in r:
                print(f"{name:<18}{tag:<5} ERROR {r['error']}"); continue
            print(f"{name if tag=='IS' else '':<18}{tag:<5}"
                  f"{r['total_return']*100:>+9.2f}%{r['sharpe']:>+8.2f}"
                  f"{r['sortino']:>+8.2f}{r['max_drawdown']*100:>+8.2f}%"
                  f"{r['calmar']:>8.2f}{r['profit_factor']:>6.2f}"
                  f"{r['n_trades']:>5}{r['win_rate']*100:>6.1f}%"
                  f"{r['avg_hold']:>6.1f}d{r['alpha']*100:>+8.2f}pt")
        print("-" * 138)

    print("\nIS → OOS 衰减 & 裁决（OOS alpha>0 且 ΔSh>-0.8 = 稳健）")
    print(f"{'配置':<18}{'OOS_ret':>10}{'OOS_Sh':>9}{'OOS_MDD':>10}{'OOS_a':>10}{'判定':>10}")
    for name, cfg, ri, ro in rows:
        if "error" in ri or "error" in ro:
            continue
        v = ("稳健" if (ro["alpha"] > 0 and (ro["sharpe"]-ri["sharpe"]) > -0.8) else
             "衰减" if ro["alpha"] > 0 else "失效")
        print(f"{name:<18}{ro['total_return']*100:>+9.2f}%{ro['sharpe']:>+8.2f}"
              f"{ro['max_drawdown']*100:>+9.2f}%{ro['alpha']*100:>+9.2f}pt{v:>10}")

    # folds
    print("\n" + "=" * 150)
    FOLD = 75
    starts = list(range(FIXED_WARMUP, n - FOLD + 1, FOLD))
    d0 = data[codes[0]]["date"]
    fold_res = {}
    for name, cfg in cands.items():
        fold_res[name] = []
        for s in starts:
            lo, hi = s - FIXED_WARMUP, min(s + FOLD, n)
            fold_res[name].append(bt(cfg, slice_by_index(data, lo, hi)))

    names = list(cands.keys())
    print(f"{'配置':<18}" + "".join(f"{'F'+str(k+1):>13}" for k in range(len(starts)))
          + f"{'均Sh':>8}{'正a':>6}{'最差':>9}{'均MDD':>8}")
    print("-" * 150)
    summary = {}
    for name in names:
        rs = fold_res[name]
        line = f"{name:<18}"; shs, pa, worst, mdds = [], 0, 999.0, []
        for r in rs:
            if "error" in r:
                line += f"{'ERR':>13}"; continue
            line += f"{r['total_return']*100:>+12.1f}%"
            shs.append(r["sharpe"]); mdds.append(r["max_drawdown"])
            if r["alpha"] > 0: pa += 1
            worst = min(worst, r["total_return"])
        msh = sum(shs)/len(shs) if shs else 0
        line += f"{msh:>+8.2f}{pa:>5}/{len(rs)}{worst*100:>+8.1f}%{sum(mdds)/len(mdds)*100:>+7.1f}%"
        print(line)
        summary[name] = (msh, pa, worst, sum(mdds)/len(mdds))

    print("\n逐折对比基线 E0（累计差单位 pt）")
    print(f"{'配置':<18}" + "".join(f"{'F'+str(k+1):>9}" for k in range(len(starts)))
          + f"{'胜':>5}{'累计差':>9}")
    print("-" * 150)
    for name in names:
        if name == "E0_base_生产":
            continue
        rs = fold_res[name]; base_rs = fold_res["E0_base_生产"]
        line = f"{name:<18}"; wins, tot = 0, 0.0
        for k, r in enumerate(rs):
            if "error" in r or "error" in base_rs[k]:
                line += f"{'-':>9}"; continue
            d = (r["total_return"] - base_rs[k]["total_return"]) * 100
            tot += d
            if d > 0: wins += 1
            line += f"{d:>+8.1f}"
        line += f"{wins:>4}/{len(rs)}{tot:>+9.1f}pt"
        print(line)

    out = ROOT / "logs" / "opt_cooldown.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({
        "summary": {nm: {"mean_sharpe": s[0], "pos_alpha": s[1],
                        "worst": s[2], "mean_mdd": s[3]} for nm, s in summary.items()},
        "is_oos": {nm: {"IS": {k: rows[i][2][k] for k in ("total_return","sharpe","max_drawdown","alpha","n_trades")},
                        "OOS": {k: rows[i][3][k] for k in ("total_return","sharpe","max_drawdown","alpha","n_trades")}}
                   for i, nm in enumerate(names)},
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[保存] {out}")


if __name__ == "__main__":
    main()

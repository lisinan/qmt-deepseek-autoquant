# -*- coding: utf-8 -*-
"""
全新结构性候选验证：分行业分散持仓（max_per_sector）

与历史被拒项（参数级微调 / 信号质量层特征）本质不同，这是一个
**组合层风险约束**：在已验证的 max_positions 总上限之内，进一步限制
同一 AI 产业链环节最多持几只，强制组合跨环节铺开，降低环节内隐性
集中风险（光模块三杰齐涨齐跌 → 动量排名前 N 常挤在同一环节 → 伪分散）。

验证纪律（与历史一致）：
  - IS/OOS 切分：OOS 只看一次结论
  - 多折 walk-forward：判据不是"平均多好"，而是"在多少个窗口稳定优于对照"
  - 并入门槛：OOS alpha >= 0 且 多折均值 Sharpe 不低于基线 且 正 alpha 折占多数

用法：
  python strategy/_opt_sector.py
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

from strategy.opt_harness import (  # noqa: E402
    wide_universe, preload, slice_by_index, base_cfg,
    FIXED_WARMUP, INDEX_CODES,
)
from strategy.backtest_daily import run_backtest  # noqa: E402


def fmt(r: dict) -> str:
    if not r or "error" in r:
        return "ERROR " + (r.get("error", "") if r else "")
    return (f"ret={r['total_return']*100:+7.2f}% Sh={r['sharpe']:+5.2f} "
            f"So={r['sortino']:+5.2f} MDD={r['max_drawdown']*100:7.2f}% "
            f"Cal={r['calmar']:5.2f} PF={r['profit_factor']:4.2f} "
            f"n={r['n_trades']:>3} win={r['win_rate']*100:4.1f}% "
            f"hold={r['avg_hold']:4.1f}d exp={r['exposure']*100:4.1f}% "
            f"a={r['alpha']*100:+7.2f}pt")


def main():
    codes = wide_universe()
    count = 500
    t0 = time.time()
    data = preload(codes + ["399006.SZ", "000300.SH"], count)
    print(f"[数据] {len(data)} 只 ×{count} 载入 {time.time()-t0:.1f}s")
    n = min(len(d["close"]) for d in data.values())
    IS_END = min(350, n - 120)
    OOS_LO = max(0, IS_END - FIXED_WARMUP)
    is_data = slice_by_index(data, 0, IS_END)
    oos_data = slice_by_index(data, OOS_LO, n)
    print(f"[窗口] 总 {n} | IS=[{FIXED_WARMUP},{IS_END}) "
          f"| OOS=[{IS_END},{n}) 无重叠")

    def bt(cfg, dset):
        ks = [k for k in dset.keys() if k not in INDEX_CODES]
        return run_backtest(ks, cfg, count=count, preloaded=dset)

    # ---- 候选：在已验证生产配置上叠加分行业上限 ----
    b = base_cfg()
    tests = {
        "S0_基线(max_per_sector=0)": replace(b, max_per_sector=0),
        "S1_max_per_sector=2": replace(b, max_per_sector=2),
        "S2_max_per_sector=3": replace(b, max_per_sector=3),
    }

    # ---------------- IS / OOS 双段 ----------------
    print("\n" + "=" * 132)
    print("分行业分散持仓：IS / OOS 双段验证（基线 = 当前生产配置）")
    print("=" * 132)
    rows = []
    for name, cfg in tests.items():
        ri = bt(cfg, is_data)
        ro = bt(cfg, oos_data)
        rows.append((name, cfg, ri, ro))
        for tag, r in (("IS", ri), ("OOS", ro)):
            print(f"{name if tag=='IS' else '':<26}{tag:<4}{fmt(r)}")

    print("\n" + "-" * 132)
    print("IS → OOS 衰减（过拟合检测）")
    print(f"{'配置':<26}{'IS_Sh':>8}{'OOS_Sh':>8}{'ΔSh':>8}"
          f"{'IS_a':>10}{'OOS_a':>11}{'Δa':>10}{'判定':>12}")
    for name, cfg, ri, ro in rows:
        dsh = ro["sharpe"] - ri["sharpe"]
        da = (ro["alpha"] - ri["alpha"]) * 100
        verdict = ("稳健" if (ro["alpha"] > 0 and dsh > -0.8) else
                   "衰减" if ro["alpha"] > 0 else "失效")
        print(f"{name:<26}{ri['sharpe']:>+8.2f}{ro['sharpe']:>+8.2f}{dsh:>+8.2f}"
              f"{ri['alpha']*100:>+9.2f}pt{ro['alpha']*100:>+10.2f}pt"
              f"{da:>+9.2f}pt{verdict:>12}")

    # ---------------- 多折 walk-forward ----------------
    FOLD = 75
    starts = list(range(FIXED_WARMUP, n - FOLD + 1, FOLD))
    d0 = data[codes[0]]["date"]
    print("\n" + "=" * 140)
    print(f"多折 walk-forward：{len(starts)} 个连续窗口（每窗 {FOLD} 根≈3.6月）")
    print("=" * 140)
    for k, s in enumerate(starts):
        print(f"  Fold{k+1}: {d0[s]} -> {d0[min(s+FOLD, n)-1]}")

    fold_res = {}
    bench_by_fold = []
    for k, s in enumerate(starts):
        lo, hi = s - FIXED_WARMUP, min(s + FOLD, n)
        dsub = slice_by_index(data, lo, hi)
        for name, cfg in tests.items():
            fold_res.setdefault(name, []).append(bt(cfg, dsub))
        bench_by_fold.append(fold_res["S0_基线(max_per_sector=0)"][k]["bench_return"])

    print(f"\n{'配置':<26}" + "".join(f"{'F'+str(k+1):>13}"
                                      for k in range(len(starts)))
          + f"{'均值Sh':>9}{'正a':>7}{'最差':>10}")
    print("-" * 140)
    print(f"{'[基准]等权买入持有':<26}"
          + "".join(f"{bench_by_fold[k]*100:>+12.1f}%" for k in range(len(starts))))
    print("-" * 140)
    summary = {}
    for name in tests:
        rs = fold_res[name]
        line = f"{name:<26}"
        shs, pa, worst = [], 0, 999.0
        for r in rs:
            line += f"{r['total_return']*100:>+12.1f}%"
            shs.append(r["sharpe"])
            if r["alpha"] > 0:
                pa += 1
            worst = min(worst, r["total_return"])
        msh = sum(shs)/len(shs)
        line += f"{msh:>+9.2f}{pa:>5}/{len(rs)}{worst*100:>+9.1f}%"
        print(line)
        summary[name] = msh

    # 逐折对比基线 S0
    print("\n" + "=" * 140)
    print("逐折对比「基线 S0」：+ = 该折收益更高")
    print("=" * 140)
    base_rs = fold_res["S0_基线(max_per_sector=0)"]
    print(f"{'配置':<26}" + "".join(f"{'F'+str(k+1):>10}"
                                    for k in range(len(starts)))
          + f"{'胜':>6}{'累计差':>10}")
    for name in tests:
        if name == "S0_基线(max_per_sector=0)":
            continue
        rs = fold_res[name]
        line = f"{name:<26}"
        wins, tot = 0, 0.0
        for k, r in enumerate(rs):
            d = (r["total_return"] - base_rs[k]["total_return"]) * 100
            tot += d
            if d > 0:
                wins += 1
            line += f"{d:>+9.1f}"
        line += f"{wins:>4}/{len(rs)}{tot:>+9.1f}pt"
        print(line)

    # ---------------- 裁决 ----------------
    print("\n" + "=" * 132)
    print("裁决（并入门槛：OOS alpha>=0 且 多折均值Sh>=基线 且 正alpha折占多数）")
    print("=" * 132)
    base_oos = fold_res["S0_基线(max_per_sector=0)"][0]  # placeholder
    base_ri, base_ro = rows[0][2], rows[0][3]
    base_folds = fold_res["S0_基线(max_per_sector=0)"]
    base_msh = sum(r["sharpe"] for r in base_folds)/len(base_folds)
    base_pa = sum(1 for r in base_folds if r["alpha"] > 0)
    best = None
    for name, cfg, ri, ro in rows:
        if name == "S0_基线(max_per_sector=0)":
            continue
        fr = fold_res[name]
        fmsh = sum(r["sharpe"] for r in fr)/len(fr)
        fpa = sum(1 for r in fr if r["alpha"] > 0)
        ok_oos = ro["alpha"] > 0
        ok_fold_sh = fmsh >= base_msh
        ok_fold_pa = fpa >= base_pa
        verdict = "并入" if (ok_oos and ok_fold_sh and ok_fold_pa) else "拒绝"
        print(f"  {name:<26} OOSα={ro['alpha']*100:>+7.2f}pt "
              f"多折Sh={fmsh:+.2f}(基线{base_msh:+.2f}) "
              f"正α折={fpa}/{len(fr)}(基线{base_pa}) → {verdict}")
        if verdict == "并入" and best is None:
            best = (name, cfg)
    if best:
        print(f"\n>>> 通过验证：{best[0]} 可并入生产配置。")
    else:
        print("\n>>> 无候选通过验证：维持当前生产配置（纪律性拒绝过拟合）。")

    out = ROOT / "logs" / "opt_sector.json"
    out.parent.mkdir(exist_ok=True)
    payload = {
        "base_oos": {k: base_ro[k] for k in
                     ("total_return", "sharpe", "max_drawdown", "calmar",
                      "alpha", "n_trades", "win_rate", "exposure")},
        "base_fold_msh": round(base_msh, 4),
        "base_fold_pos_alpha": base_pa,
        "results": {
            name: {
                "IS": {k: ri[k] for k in
                       ("total_return", "sharpe", "max_drawdown", "calmar",
                        "alpha", "n_trades", "win_rate", "exposure")},
                "OOS": {k: ro[k] for k in
                        ("total_return", "sharpe", "max_drawdown", "calmar",
                         "alpha", "n_trades", "win_rate", "exposure")},
                "folds": [{k: r.get(k) for k in
                           ("total_return", "sharpe", "max_drawdown",
                            "calmar", "alpha", "exposure")}
                          for r in fold_res[name]],
            } for name, cfg, ri, ro in rows
        },
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"\n[保存] {out}")


if __name__ == "__main__":
    main()

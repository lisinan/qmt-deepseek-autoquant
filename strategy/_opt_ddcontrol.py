# -*- coding: utf-8 -*-
"""
全新杠杆验证：组合级动态暴露(DD control) + 选股排序口径(mom_metric)

纪律（与 opt_harness 一致）：
  - 所有候选建立在"已验证生产配置"之上：
      regime(创业板指 MA60)+force_exit + trend 骑行 + 波动率目标仓位 + max_positions=5
  - 用 IS/OOS 切分 + 多折 walk-forward 双验证；只有**两者都改善**才并入生产。
  - 不重复已被否决的方向（紧移动止损、真实宽止损 sizing、宽指数闸门、
    抗抖动 regime、双过滤、参数级微调）。

新维度：
  - mom_metric="sharpe"  : 风险调整动量（区间日收益 均值/标准差）→ 选"走得稳"的趋势
  - mom_metric="residual": 残差动量（相对 regime 指数的超额涨幅）→ 剔除市场 beta
  - dd_ctrl=True         : 组合级回撤控制，净值自峰值回撤超阈值时缩减新开仓暴露
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
from strategy.opt_harness import (
    wide_universe, preload, slice_by_index, base_cfg, fmt_row, score_config,
)
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
    print(f"[数据] {len(data)} 只 × 500 根日线  载入 {time.time()-t0:.1f}s")
    if not data:
        print("无数据，退出"); return

    n = min(len(d["close"]) for d in data.values())
    IS_END = min(350, n - 120)
    OOS_LO = max(0, IS_END - FIXED_WARMUP)
    is_data = slice_by_index(data, 0, IS_END)
    oos_data = slice_by_index(data, OOS_LO, n)
    print(f"[窗口] 总 {n} 根 | IS 交易=[{FIXED_WARMUP},{IS_END}) "
          f"| OOS 交易=[{IS_END},{n}) 无重叠")
    print(f"       IS {is_data[codes[0]]['date'][0]} -> {is_data[codes[0]]['date'][-1]}")
    print(f"       OOS {oos_data[codes[0]]['date'][0]} -> {oos_data[codes[0]]['date'][-1]}")

    b = base_cfg()
    E0 = replace(b, **REG)   # 已验证生产配置（基线）
    cands = {
        "E0_base_生产": E0,
        "E1_momSharpe": replace(E0, mom_metric="sharpe"),
        "E2_momResidual": replace(E0, mom_metric="residual"),
        "E3_ddCtrl": replace(E0, dd_ctrl=True),
        "E4_ddCtrl+momSharpe": replace(E0, dd_ctrl=True, mom_metric="sharpe"),
    }

    # ---------------- IS / OOS ----------------
    print("\n" + "=" * 138)
    print("IS / OOS 双段验证（候选均建立在已验证生产配置之上）")
    print("=" * 138)
    print(f"\n{'配置':<22}{'段':<5}{'收益':>10}{'Sharpe':>9}{'Sortino':>8}"
          f"{'MDD':>9}{'Calmar':>8}{'PF':>6}{'笔数':>5}{'胜率':>7}"
          f"{'持仓':>7}{'alpha':>9}")
    print("-" * 138)
    rows = []
    for name, cfg in cands.items():
        ri = bt(cfg, is_data)
        ro = bt(cfg, oos_data)
        rows.append((name, cfg, ri, ro))
        for tag, r in (("IS", ri), ("OOS", ro)):
            if "error" in r:
                print(f"{name:<22}{tag:<5} ERROR {r['error']}"); continue
            print(f"{name if tag=='IS' else '':<22}{tag:<5}"
                  f"{r['total_return']*100:>+9.2f}%{r['sharpe']:>+8.2f}"
                  f"{r['sortino']:>+8.2f}{r['max_drawdown']*100:>+8.2f}%"
                  f"{r['calmar']:>8.2f}{r['profit_factor']:>6.2f}"
                  f"{r['n_trades']:>5}{r['win_rate']*100:>6.1f}%"
                  f"{r['avg_hold']:>6.1f}d{r['alpha']*100:>+8.2f}pt")
        print("-" * 138)

    r0 = rows[0][2]; r0o = rows[0][3]
    print(f"\n{'等权买入持有':<22}{'IS':<5}{r0['bench_return']*100:>+9.2f}%"
          f"{r0['bench_sharpe']:>+8.2f}{'':>8}{r0['bench_mdd']*100:>+8.2f}%")
    print(f"{'':<22}{'OOS':<5}{r0o['bench_return']*100:>+9.2f}%"
          f"{r0o['bench_sharpe']:>+8.2f}{'':>8}{r0o['bench_mdd']*100:>+8.2f}%")

    print("\nIS → OOS 衰减 & 裁决（OOS alpha>0 且 ΔSh>-0.8 = 稳健）")
    print(f"{'配置':<22}{'IS_Sh':>8}{'OOS_Sh':>8}{'ΔSh':>8}"
          f"{'OOS_ret':>10}{'OOS_MDD':>10}{'OOS_a':>10}{'判定':>10}")
    verdicts = {}
    for name, cfg, ri, ro in rows:
        if "error" in ri or "error" in ro:
            continue
        dsh = ro["sharpe"] - ri["sharpe"]
        v = ("稳健" if (ro["alpha"] > 0 and dsh > -0.8) else
             "衰减" if ro["alpha"] > 0 else "失效")
        verdicts[name] = v
        print(f"{name:<22}{ri['sharpe']:>+8.2f}{ro['sharpe']:>+8.2f}"
              f"{dsh:>+8.2f}{ro['total_return']*100:>+9.2f}%"
              f"{ro['max_drawdown']*100:>+9.2f}%{ro['alpha']*100:>+9.2f}pt{v:>10}")

    # ---------------- folds（多折 walk-forward）----------------
    print("\n" + "=" * 150)
    print("多折 walk-forward：连续、互不重叠检验窗口（每窗 75 根 ≈ 3.5 个月）")
    print("判据：均值 Sharpe 与逐折 vs 基线的胜率/累计差")
    print("=" * 150)
    FOLD = 75
    starts = list(range(FIXED_WARMUP, n - FOLD + 1, FOLD))
    d0 = data[codes[0]]["date"]
    for k, s in enumerate(starts):
        print(f"  Fold{k+1}: 索引[{s},{min(s+FOLD, n)}) "
              f"日期 {d0[s]} -> {d0[min(s+FOLD, n)-1]}")
    fold_res = {}
    for name, cfg in cands.items():
        fold_res[name] = []
        for k, s in enumerate(starts):
            lo, hi = s - FIXED_WARMUP, min(s + FOLD, n)
            dsub = slice_by_index(data, lo, hi)
            fold_res[name].append(bt(cfg, dsub))

    names = list(cands.keys())
    print(f"\n{'配置':<22}" + "".join(f"{'F'+str(k+1):>13}" for k in range(len(starts)))
          + f"{'均Sh':>8}{'正a':>6}{'最差':>9}{'均MDD':>8}")
    print("-" * 150)
    summary = {}
    for name in names:
        rs = fold_res[name]
        line = f"{name:<22}"
        shs, pa, worst, mdds = [], 0, 999.0, []
        for r in rs:
            if "error" in r:
                line += f"{'ERR':>13}"; continue
            line += f"{r['total_return']*100:>+12.1f}%"
            shs.append(r["sharpe"]); mdds.append(r["max_drawdown"])
            if r["alpha"] > 0: pa += 1
            worst = min(worst, r["total_return"])
        msh = sum(shs)/len(shs) if shs else 0
        line += f"{msh:>+8.2f}{pa:>5}/{len(rs)}{worst*100:>+8.1f}%"
        line += f"{sum(mdds)/len(mdds)*100:>+7.1f}%"
        print(line)
        summary[name] = (msh, pa, worst, sum(mdds)/len(mdds))

    # 逐折 vs 基线 E0
    print("\n逐折对比基线 E0（正=该折收益更高；累计差单位 pt）")
    print(f"{'配置':<22}" + "".join(f"{'F'+str(k+1):>9}" for k in range(len(starts)))
          + f"{'胜':>5}{'累计差':>9}")
    print("-" * 150)
    for name in names:
        if name == "E0_base_生产":
            continue
        rs = fold_res[name]; base_rs = fold_res["E0_base_生产"]
        line = f"{name:<22}"; wins, tot = 0, 0.0
        for k, r in enumerate(rs):
            if "error" in r or "error" in base_rs[k]:
                line += f"{'-':>9}"; continue
            d = (r["total_return"] - base_rs[k]["total_return"]) * 100
            tot += d
            if d > 0: wins += 1
            line += f"{d:>+8.1f}"
        line += f"{wins:>4}/{len(rs)}{tot:>+9.1f}pt"
        print(line)
        summary[name] = summary[name] + (wins, tot)

    # 保存
    out = ROOT / "logs" / "opt_ddcontrol.json"
    out.parent.mkdir(exist_ok=True)
    payload = {
        "verdicts_is_oos": verdicts,
        "folds": [{"start": s, "date_from": d0[s],
                   "date_to": d0[min(s+FOLD, n)-1]} for s in starts],
        "summary": {nm: {"mean_sharpe": s_[0], "pos_alpha": s_[1],
                         "worst": s_[2], "mean_mdd": s_[3],
                         **({"wins": s_[4], "cum_diff": s_[5]} if len(s_) > 4 else {})}
                    for nm, s_ in summary.items()},
        "is_oos": {
            nm: {"IS": {k: rows[i][2][k] for k in
                        ("total_return","sharpe","sortino","max_drawdown","calmar","profit_factor","n_trades","win_rate","alpha","exposure")},
                 "OOS": {k: rows[i][3][k] for k in
                         ("total_return","sharpe","sortino","max_drawdown","calmar","profit_factor","n_trades","win_rate","alpha","exposure")}}
            for i, nm in enumerate(names)
        },
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[保存] {out}")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""
第三轮优化验证（本轮新增三个「从未被检验过」的结构性维度）

维度 1｜真实分散度（fixed_amount）
    生产配置 fixed_amount=300000、单标的上限 equity*0.30、初始资金 100 万。
    → 每仓恒为 30 万 = 30%，现金只够开 ~3 仓，max_positions=8 形同虚设。
    此前对 max_positions 的扫描全部被这个现金约束吃掉（3 以上无差别），
    所以「分散度」这一结构维度实际上从未被检验。

维度 2｜regime 抗抖动
    闸门是二值 close>MA60，在均线附近反复翻转 → 全清仓再重建，
    双向各付 0.15% 成本并错杀刚起步的趋势。测 N 日确认 / 缓冲带 / MA 斜率。

维度 3｜横截面动量口径（mom_metric）
    原口径是区间涨幅，天然偏好高波动股。测风险调整动量与相对指数残差动量。

判据（沿用前两轮纪律）：IS 只用于观察，OOS 为准，且必须通过多折
walk-forward 一致性检验才允许并入生产。OOS 失效即拒绝，不做二次调参。

用法：
    python strategy/_optround3.py --stage all
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import INDEX_CODES, MARKET_INDEX_CODE  # noqa: E402
from strategy.backtest_daily import BacktestConfig, run_backtest  # noqa: E402
from strategy.opt_harness import (  # noqa: E402
    FIXED_WARMUP, base_cfg, preload, slice_by_index, wide_universe,
)

REG = dict(regime_mode="index", regime_index=MARKET_INDEX_CODE,
           regime_ma=60, regime_force_exit=True)


def prod_cfg() -> BacktestConfig:
    """当前**生产**配置：opt_harness.base_cfg() + 已落地的 regime 闸门。"""
    return replace(base_cfg(), **REG)


def fmt(tag: str, r: dict) -> str:
    if not r or "error" in r:
        return f"{tag:<30} ERROR {r.get('error','') if r else ''}"
    return (f"{tag:<30}{r['total_return']*100:>+9.2f}%{r['sharpe']:>+8.2f}"
            f"{r['max_drawdown']*100:>+9.2f}%{r['calmar']:>7.2f}"
            f"{r['n_trades']:>5}{r['win_rate']*100:>6.1f}%"
            f"{r['exposure']*100:>8.1f}%{r['alpha']*100:>+9.2f}pt")


HDR = (f"{'配置':<30}{'收益':>10}{'Sharpe':>8}{'MDD':>10}{'Calmar':>7}"
       f"{'笔数':>5}{'胜率':>6}{'暴露':>8}{'alpha':>11}")


def variants() -> Dict[str, BacktestConfig]:
    p = prod_cfg()
    v: Dict[str, BacktestConfig] = {"A0_生产基准": p}

    # ---- 维度 1：真实分散度 ----
    # 100 万资金下 fixed_amount 决定实际可开仓位数：300k→3, 250k→4,
    # 200k→5, 150k→6, 125k→8。max_positions 已是 8，不再是约束。
    for amt, slots in ((250_000, 4), (200_000, 5), (150_000, 6),
                       (125_000, 8), (100_000, 10)):
        v[f"B_分散{slots}仓(amt{amt//1000}k)"] = replace(p, fixed_amount=amt)

    # ---- 维度 2：regime 抗抖动 ----
    for d in (2, 3, 5):
        v[f"C_确认{d}日"] = replace(p, regime_confirm_days=d)
    for b in (0.01, 0.02, 0.03):
        v[f"D_缓冲带{b:.0%}"] = replace(p, regime_buffer_pct=b)
    v["E_MA斜率为正"] = replace(p, regime_slope_days=5)
    v["F_确认3日+缓冲2%"] = replace(p, regime_confirm_days=3,
                                    regime_buffer_pct=0.02)

    # ---- 维度 3：动量口径 ----
    v["G_风险调整动量"] = replace(p, mom_metric="sharpe")
    v["H_残差动量(剥beta)"] = replace(p, mom_metric="residual")
    return v


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all",
                    choices=["regress", "isoos", "folds", "all"])
    ap.add_argument("--count", type=int, default=500)
    ap.add_argument("--is-end", type=int, default=350)
    args = ap.parse_args()

    codes = wide_universe()
    t0 = time.time()
    data = preload(codes + [MARKET_INDEX_CODE, "000300.SH"], args.count)
    print(f"[数据] {len(data)} 只 × {args.count} 根  载入 {time.time()-t0:.1f}s")
    if not data:
        print("无数据，退出")
        return

    n = min(len(d["close"]) for d in data.values())
    IS_END = min(args.is_end, n - 120)
    OOS_LO = max(0, IS_END - FIXED_WARMUP)
    is_data = slice_by_index(data, 0, IS_END)
    oos_data = slice_by_index(data, OOS_LO, n)
    d0 = data[codes[0]]["date"]
    print(f"[窗口] 总 {n} 根 | IS 交易=[{FIXED_WARMUP},{IS_END}) "
          f"{d0[FIXED_WARMUP]}->{d0[IS_END-1]}")
    print(f"       OOS 交易=[{IS_END},{n}) {d0[IS_END]}->{d0[n-1]}  无重叠")

    def bt(cfg, dset):
        ks = [k for k in dset.keys() if k not in INDEX_CODES]
        return run_backtest(ks, cfg, count=args.count, preloaded=dset)

    out_payload: dict = {}

    # ---------------- 0) 回归校验 ----------------
    # 新增的 regime 状态机 / 动量口径在默认参数下必须与改造前完全一致。
    # 用上一轮已记录的 OOS 数值做锚点：ret=+1.26% Sharpe=+0.24。
    if args.stage in ("regress", "all"):
        print("\n" + "=" * 108)
        print("0) 回归校验：默认参数下新代码必须复现上一轮基准（锚点 OOS +1.26% / Sh +0.24）")
        print("=" * 108)
        print(HDR)
        r_is = bt(prod_cfg(), is_data)
        r_oos = bt(prod_cfg(), oos_data)
        print(fmt("生产基准 IS", r_is))
        print(fmt("生产基准 OOS", r_oos))
        ok = (abs(r_oos["total_return"] - 0.0126) < 0.004
              and abs(r_oos["sharpe"] - 0.24) < 0.06)
        print(f"\n  → 回归判定: {'通过（与上一轮一致，改造无副作用）' if ok else '★不一致，需排查★'}")
        out_payload["regress"] = {
            "IS": {k: r_is.get(k) for k in
                   ("total_return", "sharpe", "max_drawdown", "n_trades")},
            "OOS": {k: r_oos.get(k) for k in
                    ("total_return", "sharpe", "max_drawdown", "n_trades")},
            "passed": bool(ok),
        }
        if args.stage == "regress":
            return

    # ---------------- 1) IS / OOS 双段 ----------------
    vs = variants()
    if args.stage in ("isoos", "all"):
        print("\n" + "=" * 108)
        print("1) 样本内(IS) / 样本外(OOS) 双段验证 —— OOS 才是裁决依据")
        print("=" * 108)
        rows = []
        for name, cfg in vs.items():
            ri, ro = bt(cfg, is_data), bt(cfg, oos_data)
            rows.append((name, ri, ro))
        for seg, k in (("样本内 IS", 1), ("样本外 OOS", 2)):
            print(f"\n--- {seg} ---")
            print(HDR)
            for name, ri, ro in rows:
                print(fmt(name, (ri, ro)[k - 1]))
        b = rows[0]
        print(f"\n{'[基准]等权买入持有 IS':<30}"
              f"{b[1]['bench_return']*100:>+9.2f}%{b[1]['bench_sharpe']:>+8.2f}"
              f"{b[1]['bench_mdd']*100:>+9.2f}%")
        print(f"{'[基准]等权买入持有 OOS':<30}"
              f"{b[2]['bench_return']*100:>+9.2f}%{b[2]['bench_sharpe']:>+8.2f}"
              f"{b[2]['bench_mdd']*100:>+9.2f}%")

        print("\n" + "=" * 108)
        print("按 OOS Sharpe 排序（对照 = A0_生产基准）")
        print("=" * 108)
        base_o = rows[0][2]
        rank = sorted([r for r in rows if "error" not in r[2]],
                      key=lambda x: -x[2]["sharpe"])
        print(f"{'配置':<30}{'OOS_ret':>10}{'OOS_Sh':>9}{'ΔSh':>8}"
              f"{'Δret':>10}{'OOS_MDD':>10}{'IS_ret':>10}{'判定':>10}")
        for name, ri, ro in rank:
            dsh = ro["sharpe"] - base_o["sharpe"]
            dret = (ro["total_return"] - base_o["total_return"]) * 100
            verdict = ("优于基准" if (dsh > 0.05 and dret > 0.5) else
                       "持平" if abs(dsh) <= 0.05 else "劣于基准")
            print(f"{name:<30}{ro['total_return']*100:>+9.2f}%"
                  f"{ro['sharpe']:>+9.2f}{dsh:>+8.2f}{dret:>+9.2f}pt"
                  f"{ro['max_drawdown']*100:>+9.2f}%"
                  f"{ri['total_return']*100:>+9.2f}%{verdict:>10}")

        out_payload["isoos"] = {
            name: {
                "IS": {k: ri.get(k) for k in
                       ("total_return", "sharpe", "max_drawdown", "calmar",
                        "n_trades", "win_rate", "alpha", "exposure")},
                "OOS": {k: ro.get(k) for k in
                        ("total_return", "sharpe", "max_drawdown", "calmar",
                         "n_trades", "win_rate", "alpha", "exposure")},
                "OOS_curve": ro.get("equity_curve"),
                "OOS_bench_curve": ro.get("bench_curve"),
                "OOS_exit_reasons": ro.get("exit_reasons"),
            }
            for name, ri, ro in rows if "error" not in ri and "error" not in ro
        }

    # ---------------- 2) 多折 walk-forward ----------------
    if args.stage in ("folds", "all"):
        FOLD = 75
        starts = list(range(FIXED_WARMUP, n - FOLD + 1, FOLD))
        print("\n" + "=" * 132)
        print(f"2) 多折 walk-forward：{len(starts)} 个互不重叠窗口"
              f"（每窗 {FOLD} 根 ≈ {FOLD/21:.1f} 个月）")
        print("判据不是均值多好，而是「在多少个窗口里稳定不输于基准」。")
        print("=" * 132)
        for k, s in enumerate(starts):
            print(f"  Fold{k+1}: {d0[s]} -> {d0[min(s+FOLD, n)-1]}")

        fold_res: Dict[str, list] = {}
        for s in starts:
            dsub = slice_by_index(data, s - FIXED_WARMUP, min(s + FOLD, n))
            for name, cfg in vs.items():
                fold_res.setdefault(name, []).append(bt(cfg, dsub))

        hdr = (f"\n{'配置':<30}"
               + "".join(f"{'F'+str(k+1):>11}" for k in range(len(starts)))
               + f"{'均值Sh':>9}{'正alpha':>9}{'最差':>10}")
        print(hdr)
        print("-" * 132)
        base_rs = fold_res["A0_生产基准"]
        summary = []
        for name in vs:
            rs = fold_res[name]
            line = f"{name:<30}"
            shs, pos_a, worst = [], 0, 9.99
            for r in rs:
                if "error" in r:
                    line += f"{'ERR':>11}"
                    continue
                line += f"{r['total_return']*100:>+10.1f}%"
                shs.append(r["sharpe"])
                if r["alpha"] > 0:
                    pos_a += 1
                worst = min(worst, r["total_return"])
            msh = sum(shs) / len(shs) if shs else 0.0
            line += f"{msh:>+9.2f}{pos_a:>6}/{len(rs)}{worst*100:>+9.1f}%"
            print(line)
            summary.append((name, msh, pos_a, worst))

        print("\n" + "=" * 132)
        print("逐折对比基准 A0（+ = 该折更优）；「胜」需 >= 3/5 才算稳健")
        print("=" * 132)
        print(f"{'配置':<30}"
              + "".join(f"{'F'+str(k+1):>10}" for k in range(len(starts)))
              + f"{'胜':>7}{'累计差':>11}")
        wins_map = {}
        for name in vs:
            if name == "A0_生产基准":
                continue
            rs = fold_res[name]
            line, wins, tot = f"{name:<30}", 0, 0.0
            for k, r in enumerate(rs):
                if "error" in r or "error" in base_rs[k]:
                    line += f"{'-':>10}"
                    continue
                dd = (r["total_return"] - base_rs[k]["total_return"]) * 100
                tot += dd
                if dd > 0:
                    wins += 1
                line += f"{dd:>+9.1f}"
            line += f"{wins:>5}/{len(rs)}{tot:>+10.1f}pt"
            wins_map[name] = (wins, len(rs), tot)
            print(line)

        print("\n" + "=" * 132)
        print("排序（均值 Sharpe）")
        print("=" * 132)
        for name, msh, pa, worst in sorted(summary, key=lambda x: -x[1]):
            w = wins_map.get(name)
            wtxt = f"  逐折胜={w[0]}/{w[1]} 累计{w[2]:+.1f}pt" if w else "  (基准)"
            print(f"  {name:<30} 均值Sh={msh:+.2f}  正alpha={pa}/5  "
                  f"最差折={worst*100:+.1f}%{wtxt}")

        out_payload["folds"] = {
            "windows": [{"from": d0[s], "to": d0[min(s+FOLD, n)-1]}
                        for s in starts],
            "results": {
                name: [{k: r.get(k) for k in
                        ("total_return", "sharpe", "max_drawdown", "calmar",
                         "n_trades", "win_rate", "alpha", "exposure")}
                       for r in fold_res[name]]
                for name in vs
            },
        }

    out = ROOT / "logs" / "opt_round3.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(out_payload, ensure_ascii=False),
                   encoding="utf-8")
    print(f"\n[保存] {out}")


if __name__ == "__main__":
    main()

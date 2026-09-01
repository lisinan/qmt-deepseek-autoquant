# -*- coding: utf-8 -*-
"""
regime 闸门「三态裁决」实验（2026-08-29 D）

背景：上一轮滚动 walk-forward（25 窗）+ 7 折交叉验证得出反向结论——
生产用的 **硬闸门**（创业板指 MA60 + 强制清仓）在 2024–2026 全样本上是
风险调整后收益的净拖累（无闸门均值 Sharpe +1.69 vs 硬闸门 +0.85，7/7 折跑输）。

但那一轮把闸门当成一个「开/关」的整体，忽略了它其实有两条**互相独立**的作用路径：
  路径 A（拦新开仓）  backtest_daily.py:790   `len(positions) < max_positions and regime_ok[i]`
  路径 B（强制清仓）  backtest_daily.py:768   `regime_force_exit and not regime_ok[i]`

假设：拖累主要来自路径 B。强制清仓把趋势骑行中的赢家在指数刚破 MA60 时砍掉，
之后指数回抽 MA60 之上才允许重新开仓 → 典型「出得来、回不去」，错过反弹主升段。
而路径 A（下行市不追新仓）本身是廉价的、几乎不损失 alpha 的防御。

因此本实验拆成三态，在同一数据、同一预热、同一成本下裁决：
  G0  闸门全关          regime_mode="off"                               （永远满仓对照）
  G1  硬闸门（现生产）  index MA60, force_exit=True                     （拦新仓 + 强制清仓）
  G2  软闸门（新候选）  index MA60, force_exit=False                    （只拦新仓，持仓交给 trend 退出）

验证方法（三重，任一不过则不并入）：
  1. 25 个连续滚动窗（每窗 63d，步长 21d，预热 130d）——时间稳健性
  2. 7 折互不重叠 walk-forward（每折 75d）——最严格的分段一致性
  3. 配对统计检验（逐窗差值的 t 统计量 + 符号检验）——排除点估计噪声

输出：logs/opt_gatemode.json
"""
from __future__ import annotations

import json
import math
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import MARKET_INDEX_CODE, INDEX_CODES  # noqa: E402
from strategy.backtest_daily import run_backtest            # noqa: E402
from strategy.opt_harness import (                          # noqa: E402
    preload, wide_universe, slice_by_index, base_cfg, FIXED_WARMUP,
)

WINDOW = 63      # 滚动检验窗 ≈ 3 个月
STEP = 21        # 步长 ≈ 1 个月
FOLD = 75        # 多折窗长（与历史 folds 实验一致，便于交叉比对）
COUNT = 750      # 历史长度 ≈ 3 年


# ------------------------------------------------------------ 统计工具

def mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def stdev(xs: List[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def paired_test(diffs: List[float]) -> dict:
    """配对差值的 t 统计量 + 符号检验（不依赖 scipy）。

    t = mean / (sd/sqrt(n))；|t| >= 2 视为在 ~95% 水平上显著。
    符号检验用正态近似：z = (2*wins - n) / sqrt(n)。
    """
    n = len(diffs)
    if n < 2:
        return {"n": n, "mean": mean(diffs), "sd": 0.0, "t": 0.0,
                "wins": 0, "winrate": 0.0, "sign_z": 0.0, "significant": False}
    m, sd = mean(diffs), stdev(diffs)
    t = m / (sd / math.sqrt(n)) if sd > 0 else 0.0
    wins = sum(1 for d in diffs if d > 0)
    sign_z = (2 * wins - n) / math.sqrt(n)
    return {"n": n, "mean": m, "sd": sd, "t": t, "wins": wins,
            "winrate": wins / n, "sign_z": sign_z,
            "significant": abs(t) >= 2.0}


# ------------------------------------------------------------ 主流程

def main() -> None:
    codes = wide_universe()
    t0 = time.time()
    data = preload(codes + [MARKET_INDEX_CODE], COUNT)
    print(f"[数据] {len(data)} 只 × {COUNT} 根日线  载入 {time.time()-t0:.1f}s")
    if not data or MARKET_INDEX_CODE not in data:
        print("无数据 / 无指数，退出")
        return

    n = min(len(d["close"]) for d in data.values())
    dates = data[codes[0]]["date"]
    idx_close = data[MARKET_INDEX_CODE]["close"]
    print(f"[总] {n} 根日线  {dates[0]} → {dates[-1]}")

    b = base_cfg()
    GATE = dict(regime_mode="index", regime_index=MARKET_INDEX_CODE, regime_ma=60)
    arms: Dict[str, object] = {
        "G0_闸门全关":   replace(b, regime_mode="off"),
        "G1_硬闸门(生产)": replace(b, **GATE, regime_force_exit=True),
        "G2_软闸门(候选)": replace(b, **GATE, regime_force_exit=False),
    }

    def bt(cfg, dsub):
        ks = [k for k in dsub.keys() if k not in INDEX_CODES]
        return run_backtest(ks, cfg, count=COUNT, preloaded=dsub)

    def idx_above_ma60(s: int) -> bool:
        lo = max(0, s - 60)
        return idx_close[s] > (sum(idx_close[lo:s]) / max(1, s - lo))

    # ========================================================= 1. 滚动窗
    starts = list(range(FIXED_WARMUP, n - WINDOW + 1, STEP))
    print("\n" + "=" * 132)
    print(f"【方法 1】滚动 walk-forward：{len(starts)} 个窗（每窗 {WINDOW}d，"
          f"步长 {STEP}d，预热 {FIXED_WARMUP}d）")
    print("=" * 132)
    print(f"{'窗起始':<12}{'市况':>6}"
          + "".join(f"{name.split('_')[0]+'ret':>12}" for name in arms)
          + f"{'G1-G0α':>10}{'G2-G0α':>10}{'G2-G1α':>10}"
          + f"{'exp G1':>8}{'exp G2':>8}")
    print("-" * 132)

    roll: Dict[str, list] = {k: [] for k in arms}
    roll_meta = []
    for s in starts:
        lo, hi = s - FIXED_WARMUP, s + WINDOW
        dsub = slice_by_index(data, lo, hi)
        res = {}
        bad = False
        for name, cfg in arms.items():
            r = bt(cfg, dsub)
            if "error" in r:
                bad = True
                break
            res[name] = r
        if bad:
            print(f"  窗 {dates[s]}: ERROR skip")
            continue
        for name in arms:
            roll[name].append(res[name])
        above = idx_above_ma60(s)
        roll_meta.append({"date_from": dates[s],
                          "date_to": dates[min(s + WINDOW, n) - 1],
                          "idx_above_start": above})
        g0, g1, g2 = res["G0_闸门全关"], res["G1_硬闸门(生产)"], res["G2_软闸门(候选)"]
        print(f"{dates[s]:<12}{('UP' if above else 'DOWN'):>6}"
              f"{g0['total_return']*100:>+11.1f}%"
              f"{g1['total_return']*100:>+11.1f}%"
              f"{g2['total_return']*100:>+11.1f}%"
              f"{(g1['alpha']-g0['alpha'])*100:>+9.1f}"
              f"{(g2['alpha']-g0['alpha'])*100:>+9.1f}"
              f"{(g2['alpha']-g1['alpha'])*100:>+9.1f}"
              f"{g1['exposure']*100:>7.0f}%{g2['exposure']*100:>7.0f}%")

    k = len(roll_meta)
    if k == 0:
        print("无有效窗，退出")
        return

    def agg(name: str) -> dict:
        rs = roll[name]
        return {
            "mean_ret": mean([r["total_return"] for r in rs]),
            "mean_sh": mean([r["sharpe"] for r in rs]),
            "mean_alpha": mean([r["alpha"] for r in rs]),
            "mean_mdd": mean([r["max_drawdown"] for r in rs]),
            "mean_exp": mean([r["exposure"] for r in rs]),
            "worst_ret": min(r["total_return"] for r in rs),
            "worst_mdd": min(r["max_drawdown"] for r in rs),
            "pos_alpha": sum(1 for r in rs if r["alpha"] > 0),
            "mean_n": mean([r["n_trades"] for r in rs]),
        }

    roll_agg = {name: agg(name) for name in arms}
    print("\n" + "=" * 132)
    print("滚动窗聚合")
    print("=" * 132)
    print(f"{'配置':<18}{'均ret':>9}{'均Sh':>8}{'均α':>10}{'均MDD':>9}"
          f"{'最差MDD':>10}{'最差ret':>10}{'均exp':>8}{'正α':>8}{'均交易':>8}")
    for name in arms:
        a = roll_agg[name]
        print(f"{name:<18}{a['mean_ret']*100:>+8.2f}%{a['mean_sh']:>+8.2f}"
              f"{a['mean_alpha']*100:>+9.2f}pt{a['mean_mdd']*100:>8.2f}%"
              f"{a['worst_mdd']*100:>9.2f}%{a['worst_ret']*100:>+9.2f}%"
              f"{a['mean_exp']*100:>7.0f}%{a['pos_alpha']:>5}/{k}"
              f"{a['mean_n']:>8.1f}")

    # 配对检验（滚动窗，用 alpha 与 sharpe 两个口径）
    pairs = [("G1_硬闸门(生产)", "G0_闸门全关"),
             ("G2_软闸门(候选)", "G0_闸门全关"),
             ("G2_软闸门(候选)", "G1_硬闸门(生产)")]
    roll_tests = {}
    print("\n" + "=" * 132)
    print("配对统计检验（滚动窗逐窗差值；|t|>=2 视为显著）")
    print("=" * 132)
    for a_, b_ in pairs:
        d_alpha = [roll[a_][i]["alpha"] - roll[b_][i]["alpha"] for i in range(k)]
        d_sh = [roll[a_][i]["sharpe"] - roll[b_][i]["sharpe"] for i in range(k)]
        ta, ts = paired_test(d_alpha), paired_test(d_sh)
        roll_tests[f"{a_} vs {b_}"] = {"alpha": ta, "sharpe": ts}
        print(f"  {a_} − {b_}")
        print(f"      α : 均值{ta['mean']*100:+6.2f}pt  t={ta['t']:+5.2f}  "
              f"胜率 {ta['wins']}/{ta['n']}  符号z={ta['sign_z']:+.2f}  "
              f"{'显著' if ta['significant'] else '不显著'}")
        print(f"      Sh: 均值{ts['mean']:+6.2f}    t={ts['t']:+5.2f}  "
              f"胜率 {ts['wins']}/{ts['n']}  符号z={ts['sign_z']:+.2f}  "
              f"{'显著' if ts['significant'] else '不显著'}")

    # 按市况分组
    grp = {}
    for label, want in (("down_start", False), ("up_start", True)):
        ids = [i for i in range(k) if roll_meta[i]["idx_above_start"] is want]
        g = {"n": len(ids)}
        for name in arms:
            g[name] = {
                "mean_alpha": mean([roll[name][i]["alpha"] for i in ids]),
                "mean_ret": mean([roll[name][i]["total_return"] for i in ids]),
                "mean_sh": mean([roll[name][i]["sharpe"] for i in ids]),
                "mean_mdd": mean([roll[name][i]["max_drawdown"] for i in ids]),
                "worst_ret": (min(roll[name][i]["total_return"] for i in ids)
                              if ids else 0.0),
            }
        grp[label] = g
    print("\n" + "=" * 132)
    print("按窗起始市况分组（DOWN=指数在 MA60 之下，闸门「应该」发挥作用的窗）")
    print("=" * 132)
    for label in ("down_start", "up_start"):
        g = grp[label]
        print(f"  {label} (n={g['n']})")
        for name in arms:
            v = g[name]
            print(f"      {name:<18} α={v['mean_alpha']*100:+7.2f}pt  "
                  f"ret={v['mean_ret']*100:+7.2f}%  Sh={v['mean_sh']:+5.2f}  "
                  f"MDD={v['mean_mdd']*100:7.2f}%  最差={v['worst_ret']*100:+7.2f}%")

    # ========================================================= 2. 多折
    fstarts = list(range(FIXED_WARMUP, n - FOLD + 1, FOLD))
    print("\n" + "=" * 132)
    print(f"【方法 2】多折 walk-forward：{len(fstarts)} 折互不重叠（每折 {FOLD}d）")
    print("=" * 132)
    fold_res: Dict[str, list] = {kk: [] for kk in arms}
    bench = []
    fold_meta = []
    for s in fstarts:
        lo, hi = s - FIXED_WARMUP, min(s + FOLD, n)
        dsub = slice_by_index(data, lo, hi)
        ok = True
        tmp = {}
        for name, cfg in arms.items():
            r = bt(cfg, dsub)
            if "error" in r:
                ok = False
                break
            tmp[name] = r
        if not ok:
            continue
        for name in arms:
            fold_res[name].append(tmp[name])
        bench.append(tmp["G0_闸门全关"]["bench_return"])
        fold_meta.append({"date_from": dates[s], "date_to": dates[hi - 1]})

    nf = len(fold_meta)
    print(f"{'配置':<18}" + "".join(f"{'F'+str(i+1):>12}" for i in range(nf))
          + f"{'均Sh':>8}{'正α':>8}{'最差':>10}{'均MDD':>9}{'均exp':>8}")
    print("-" * 132)
    print(f"{'[基准]等权持有':<18}"
          + "".join(f"{bench[i]*100:>+11.1f}%" for i in range(nf)))
    print("-" * 132)
    fold_agg = {}
    for name in arms:
        rs = fold_res[name]
        shs = [r["sharpe"] for r in rs]
        line = f"{name:<18}" + "".join(f"{r['total_return']*100:>+11.1f}%" for r in rs)
        pa = sum(1 for r in rs if r["alpha"] > 0)
        worst = min(r["total_return"] for r in rs)
        mmdd = mean([r["max_drawdown"] for r in rs])
        mexp = mean([r["exposure"] for r in rs])
        print(line + f"{mean(shs):>+8.2f}{pa:>5}/{nf}{worst*100:>+9.1f}%"
                     f"{mmdd*100:>8.1f}%{mexp*100:>7.0f}%")
        fold_agg[name] = {"mean_sh": mean(shs), "pos_alpha": pa,
                          "worst_ret": worst, "mean_mdd": mmdd,
                          "mean_exp": mexp,
                          "mean_ret": mean([r["total_return"] for r in rs]),
                          "mean_alpha": mean([r["alpha"] for r in rs])}

    fold_tests = {}
    print("\n配对检验（多折逐折差值）")
    for a_, b_ in pairs:
        d_alpha = [fold_res[a_][i]["alpha"] - fold_res[b_][i]["alpha"]
                   for i in range(nf)]
        d_ret = [fold_res[a_][i]["total_return"] - fold_res[b_][i]["total_return"]
                 for i in range(nf)]
        ta, tr = paired_test(d_alpha), paired_test(d_ret)
        fold_tests[f"{a_} vs {b_}"] = {"alpha": ta, "ret": tr}
        print(f"  {a_} − {b_}: α均值{ta['mean']*100:+6.2f}pt t={ta['t']:+5.2f} "
              f"胜 {ta['wins']}/{nf} | ret累计{sum(d_ret)*100:+6.1f}pt "
              f"胜 {tr['wins']}/{nf}")

    # ========================================================= 3. 裁决
    g0r, g1r, g2r = (roll_agg["G0_闸门全关"], roll_agg["G1_硬闸门(生产)"],
                     roll_agg["G2_软闸门(候选)"])
    g0f, g1f, g2f = (fold_agg["G0_闸门全关"], fold_agg["G1_硬闸门(生产)"],
                     fold_agg["G2_软闸门(候选)"])
    print("\n" + "=" * 132)
    print("【裁决】")
    print("=" * 132)
    print(f"  滚动窗均值 Sharpe : G0={g0r['mean_sh']:+.2f}  "
          f"G1={g1r['mean_sh']:+.2f}  G2={g2r['mean_sh']:+.2f}")
    print(f"  多折均值 Sharpe   : G0={g0f['mean_sh']:+.2f}  "
          f"G1={g1f['mean_sh']:+.2f}  G2={g2f['mean_sh']:+.2f}")
    print(f"  滚动窗均 MDD      : G0={g0r['mean_mdd']*100:.2f}%  "
          f"G1={g1r['mean_mdd']*100:.2f}%  G2={g2r['mean_mdd']*100:.2f}%")
    print(f"  滚动窗最差 MDD    : G0={g0r['worst_mdd']*100:.2f}%  "
          f"G1={g1r['worst_mdd']*100:.2f}%  G2={g2r['worst_mdd']*100:.2f}%")
    print(f"  滚动窗均暴露      : G0={g0r['mean_exp']*100:.0f}%  "
          f"G1={g1r['mean_exp']*100:.0f}%  G2={g2r['mean_exp']*100:.0f}%")

    payload = {
        "design": {
            "window": WINDOW, "step": STEP, "fold": FOLD, "count": COUNT,
            "n_bars": n, "warmup": FIXED_WARMUP,
            "date_from": dates[0], "date_to": dates[-1],
            "n_roll_windows": k, "n_folds": nf,
            "regime": f"{MARKET_INDEX_CODE} MA60",
            "arms": {
                "G0_闸门全关": "regime_mode=off（永远满仓对照）",
                "G1_硬闸门(生产)": "index MA60 + force_exit=True（拦新仓+强制清仓）",
                "G2_软闸门(候选)": "index MA60 + force_exit=False（只拦新仓）",
            },
        },
        "rolling": {"meta": roll_meta, "agg": roll_agg, "tests": roll_tests,
                    "by_index_state": grp,
                    "rows": {name: [{kk: r.get(kk) for kk in
                                     ("total_return", "sharpe", "max_drawdown",
                                      "calmar", "alpha", "exposure", "n_trades",
                                      "win_rate")}
                                    for r in roll[name]] for name in arms}},
        "folds": {"meta": fold_meta, "bench": bench, "agg": fold_agg,
                  "tests": fold_tests,
                  "rows": {name: [{kk: r.get(kk) for kk in
                                   ("total_return", "sharpe", "max_drawdown",
                                    "calmar", "alpha", "exposure", "n_trades",
                                    "win_rate")}
                                  for r in fold_res[name]] for name in arms}},
    }
    out = ROOT / "logs" / "opt_gatemode.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"\n[保存] {out}")


if __name__ == "__main__":
    main()

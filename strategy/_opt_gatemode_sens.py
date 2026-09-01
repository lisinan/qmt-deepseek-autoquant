# -*- coding: utf-8 -*-
"""
regime 闸门三态裁决的「窗长敏感性」稳健性检验。

_opt_gatemode.py 用固定窗长（滚动 63d / 多折 75d）得出「软闸门 G2 优于硬闸门 G1、
且与无闸门 G0 风险调整后持平但尾部更优」的结论。本脚本检验该结论是否只是特定
窗长的巧合——若结论稳健，则在各种窗长下排序应保持一致。

扫描：
  滚动窗长 WINDOW ∈ {42, 63, 84, 126}（≈2/3/4/6 个月），步长固定 21d
  多折窗长 FOLD   ∈ {50, 75, 100, 125}

判据：G2 的均值 Sharpe 在所有窗长下都应
  (a) 显著高于 G1，且
  (b) 与 G0 接近（差距远小于 G1-G0 的差距）。

输出：logs/opt_gatemode_sens.json
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

COUNT = 750
STEP = 21


def mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def stdev(xs: List[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def tstat(d: List[float]) -> float:
    if len(d) < 2:
        return 0.0
    sd = stdev(d)
    return (mean(d) / (sd / math.sqrt(len(d)))) if sd > 0 else 0.0


def main() -> None:
    codes = wide_universe()
    data = preload(codes + [MARKET_INDEX_CODE], COUNT)
    if not data or MARKET_INDEX_CODE not in data:
        print("无数据，退出")
        return
    n = min(len(d["close"]) for d in data.values())
    print(f"[数据] {len(data)} 只 × {n} 根日线")

    b = base_cfg()
    GATE = dict(regime_mode="index", regime_index=MARKET_INDEX_CODE, regime_ma=60)
    arms = {
        "G0": replace(b, regime_mode="off"),
        "G1": replace(b, **GATE, regime_force_exit=True),
        "G2": replace(b, **GATE, regime_force_exit=False),
    }

    def bt(cfg, dsub):
        ks = [k for k in dsub.keys() if k not in INDEX_CODES]
        return run_backtest(ks, cfg, count=COUNT, preloaded=dsub)

    def run_windows(starts: List[int], width: int) -> Dict[str, list]:
        out: Dict[str, list] = {k: [] for k in arms}
        for s in starts:
            dsub = slice_by_index(data, s - FIXED_WARMUP, min(s + width, n))
            tmp, ok = {}, True
            for name, cfg in arms.items():
                r = bt(cfg, dsub)
                if "error" in r:
                    ok = False
                    break
                tmp[name] = r
            if ok:
                for name in arms:
                    out[name].append(tmp[name])
        return out

    results = []
    t0 = time.time()

    print("\n" + "=" * 118)
    print("滚动窗长敏感性（步长固定 21d）")
    print("=" * 118)
    print(f"{'窗长':>6}{'窗数':>6}"
          f"{'G0 Sh':>9}{'G1 Sh':>9}{'G2 Sh':>9}"
          f"{'G2-G1 t':>10}{'G2-G0 t':>10}"
          f"{'G0 wMDD':>10}{'G1 wMDD':>10}{'G2 wMDD':>10}{'排序':>14}")
    print("-" * 118)
    for W in (42, 63, 84, 126):
        starts = list(range(FIXED_WARMUP, n - W + 1, STEP))
        res = run_windows(starts, W)
        k = len(res["G0"])
        if k < 3:
            continue
        sh = {a: mean([r["sharpe"] for r in res[a]]) for a in arms}
        wmdd = {a: min(r["max_drawdown"] for r in res[a]) for a in arms}
        d21 = [res["G2"][i]["sharpe"] - res["G1"][i]["sharpe"] for i in range(k)]
        d20 = [res["G2"][i]["sharpe"] - res["G0"][i]["sharpe"] for i in range(k)]
        order = " > ".join(sorted(arms, key=lambda a: -sh[a]))
        row = {"kind": "rolling", "width": W, "n": k,
               "sharpe": sh, "worst_mdd": wmdd,
               "t_G2_G1": tstat(d21), "t_G2_G0": tstat(d20),
               "mean_alpha": {a: mean([r["alpha"] for r in res[a]]) for a in arms},
               "mean_exp": {a: mean([r["exposure"] for r in res[a]]) for a in arms},
               "order": order}
        results.append(row)
        print(f"{W:>6}{k:>6}{sh['G0']:>+9.2f}{sh['G1']:>+9.2f}{sh['G2']:>+9.2f}"
              f"{row['t_G2_G1']:>+10.2f}{row['t_G2_G0']:>+10.2f}"
              f"{wmdd['G0']*100:>9.1f}%{wmdd['G1']*100:>9.1f}%"
              f"{wmdd['G2']*100:>9.1f}%{order:>14}")

    print("\n" + "=" * 118)
    print("多折窗长敏感性（互不重叠）")
    print("=" * 118)
    print(f"{'折长':>6}{'折数':>6}"
          f"{'G0 Sh':>9}{'G1 Sh':>9}{'G2 Sh':>9}"
          f"{'G2>G1折':>9}{'G2>G0折':>9}"
          f"{'G0最差':>10}{'G1最差':>10}{'G2最差':>10}{'排序':>14}")
    print("-" * 118)
    for F in (50, 75, 100, 125):
        starts = list(range(FIXED_WARMUP, n - F + 1, F))
        res = run_windows(starts, F)
        k = len(res["G0"])
        if k < 3:
            continue
        sh = {a: mean([r["sharpe"] for r in res[a]]) for a in arms}
        worst = {a: min(r["total_return"] for r in res[a]) for a in arms}
        w21 = sum(1 for i in range(k)
                  if res["G2"][i]["total_return"] > res["G1"][i]["total_return"])
        w20 = sum(1 for i in range(k)
                  if res["G2"][i]["total_return"] > res["G0"][i]["total_return"])
        order = " > ".join(sorted(arms, key=lambda a: -sh[a]))
        row = {"kind": "folds", "width": F, "n": k, "sharpe": sh,
               "worst_ret": worst, "wins_G2_over_G1": w21,
               "wins_G2_over_G0": w20,
               "mean_mdd": {a: mean([r["max_drawdown"] for r in res[a]]) for a in arms},
               "mean_alpha": {a: mean([r["alpha"] for r in res[a]]) for a in arms},
               "order": order}
        results.append(row)
        print(f"{F:>6}{k:>6}{sh['G0']:>+9.2f}{sh['G1']:>+9.2f}{sh['G2']:>+9.2f}"
              f"{w21:>6}/{k}{w20:>6}/{k}"
              f"{worst['G0']*100:>+9.1f}%{worst['G1']*100:>+9.1f}%"
              f"{worst['G2']*100:>+9.1f}%{order:>14}")

    # ---------------- 一致性裁决 ----------------
    roll_rows = [r for r in results if r["kind"] == "rolling"]
    fold_rows = [r for r in results if r["kind"] == "folds"]
    g2_beats_g1 = all(r["sharpe"]["G2"] > r["sharpe"]["G1"] for r in results)
    g2_near_g0 = all(
        abs(r["sharpe"]["G2"] - r["sharpe"]["G0"]) <
        abs(r["sharpe"]["G1"] - r["sharpe"]["G0"]) for r in results)
    g2_tail_ok = all(r["worst_mdd"]["G2"] >= r["worst_mdd"]["G0"] for r in roll_rows)
    verdict = {
        "G2_beats_G1_all_widths": g2_beats_g1,
        "G2_closer_to_G0_than_G1_all_widths": g2_near_g0,
        "G2_worst_mdd_no_worse_than_G0_all_rolling": g2_tail_ok,
        "n_configs_tested": len(results),
        "robust": bool(g2_beats_g1 and g2_near_g0),
    }
    print("\n" + "=" * 118)
    print("【一致性裁决】")
    print("=" * 118)
    print(f"  所有窗长下 G2 均值Sharpe > G1      : {g2_beats_g1}")
    print(f"  所有窗长下 G2 比 G1 更靠近 G0      : {g2_near_g0}")
    print(f"  所有滚动窗长下 G2 最差MDD 不劣于 G0: {g2_tail_ok}")
    print(f"  → 结论稳健（不依赖窗长）           : {verdict['robust']}")
    print(f"  共测试 {len(results)} 组窗长配置，耗时 {time.time()-t0:.0f}s")

    out = ROOT / "logs" / "opt_gatemode_sens.json"
    out.write_text(json.dumps({"design": {"count": COUNT, "step": STEP,
                                          "n_bars": n, "warmup": FIXED_WARMUP},
                               "rows": results, "verdict": verdict},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[保存] {out}")


if __name__ == "__main__":
    main()

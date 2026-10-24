# -*- coding: utf-8 -*-
"""C5 一次性研究脚本：用显著性检验重审历史已落盘参数。

对 4 个已落盘参数（down_day_exit_pct=-3.25 / atr_stop_mult=2.9 /
momentum_min_mom=0.15 / min_passing_candidates=2）各做：
  1) base_cfg()（落地态）与「回退到落地前值」两态各跑一次回测
     （确定性 tushare 面板，tests/_baseline_tushare_panel.py 同源口径）；
  2) 每态算 span_sharpe，并跑 permutation_test_returns（n_perm=500, seed=20261022）
     得单尾 p；
  3) 对候选态算 deflated_sharpe（n_trials=10 乐观 / 100 保守）；
  4) 两态 dSR 的 95% CI 用 block bootstrap（block=5, 1000 次, seed=20261023）；
  5) 汇总表 + 判定（dSR>0 且 CI 下界>0 且 DSR@100>0.95 才算「显著」）。

产出：reports/C5_SIGNIFICANCE_AUDIT_2026-10-22.md（含 JSON 明细）。
只读生产数据，不写 storage/qmt.db，不触碰任何进程。

数据口径：tushare 面板（storage/hist_cache/ts_panel_*.pkl 最新可用份），
wide_universe 全宇宙，末 900 根，aligned_universe 对齐，base_cfg 成本 0.0015。
"""
from __future__ import annotations

import json
import math
import random
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tests._baseline_tushare_panel as TP  # noqa: E402
import strategy._evolve_wf as W  # noqa: E402
from strategy.kpi_stats import span_sharpe, daily_returns_from_curve  # noqa: E402
from strategy.significance import permutation_test_returns, deflated_sharpe  # noqa: E402

SEED_PERM = 20261022
SEED_BOOT = 20261023
N_PERM = 500
N_BOOT = 1000
BLOCK = 5

# 参数 / 落地值 / 回退值（落地前值，取自 _evolve_wf.py 注释链）
PARAMS = [
    ("down_day_exit_pct", -3.25, -5.0),
    ("atr_stop_mult", 2.9, 2.5),
    ("momentum_min_mom", 0.15, 0.0),
    ("min_passing_candidates", 2, 0),
]


def run_one(cfg, panel, codes):
    from strategy.backtest_daily import run_backtest
    ks = [k for k in panel.keys() if k not in W.INDEX_CODES]
    res = run_backtest(ks, cfg, count=TP._COUNT, preloaded=panel)
    if "error" in res:
        raise RuntimeError(f"回测失败: {res}")
    curve = res["equity_curve"]
    return daily_returns_from_curve(curve), res


def block_bootstrap_dsr(a, b, block=BLOCK, n_boot=N_BOOT, seed=SEED_BOOT):
    """两态日收益（等长面板同日轴）的 span_sharpe 之差的 block bootstrap 95% CI。"""
    assert len(a) == len(b)
    n = len(a)
    rng = random.Random(seed)
    diffs = []
    for _ in range(n_boot):
        idx = []
        while len(idx) < n:
            s = rng.randrange(0, n - block + 1)
            idx.extend(range(s, s + block))
        idx = idx[:n]
        sa = span_sharpe([a[i] for i in idx])
        sb = span_sharpe([b[i] for i in idx])
        diffs.append(sa - sb)
    diffs.sort()
    lo = diffs[int(0.025 * (n_boot - 1))]
    hi = diffs[int(0.975 * (n_boot - 1))]
    return lo, hi, sum(diffs) / n_boot


def moments(x):
    n = len(x)
    m = sum(x) / n
    v = sum((r - m) ** 2 for r in x) / (n - 1)
    sd = math.sqrt(v) if v > 0 else 1e-12
    skew = sum((r - m) ** 3 for r in x) / n / sd ** 3
    kurt = sum((r - m) ** 4 for r in x) / n / sd ** 4
    return skew, kurt


def main():
    import time
    t0 = time.time()
    panel = TP._pick_panel()
    TP._panel_cache = panel
    codes = W.wide_universe()
    data = {}
    for c in codes:
        d = panel[c]
        k = min(len(d["close"]), TP._COUNT)
        data[c] = {kk: list(vv[-k:]) for kk, vv in d.items()}
    dates, panel_al = W.aligned_universe(data, codes)
    print(f"[panel] {len(codes)} codes, {len(dates)} aligned days, "
          f"{dates[0]}→{dates[-1]}")

    base = W.base_cfg()
    rows = []
    detail = {}
    for name, deployed, revert in PARAMS:
        cfg_dep = base if getattr(base, name) == deployed else replace(base, **{name: deployed})
        cfg_rev = replace(base, **{name: revert})
        assert getattr(cfg_dep, name) == deployed

        da, res_a = run_one(cfg_dep, panel_al, codes)
        db, res_b = run_one(cfg_rev, panel_al, codes)
        sr_a, sr_b = span_sharpe(da), span_sharpe(db)
        dsr = sr_a - sr_b
        perm = permutation_test_returns(da, n_perm=N_PERM, seed=SEED_PERM)
        sk, ku = moments(da)
        dsr10 = deflated_sharpe(sr_a, 10, len(da), skew=sk, kurt=ku)["dsr"]
        dsr100 = deflated_sharpe(sr_a, 100, len(da), skew=sk, kurt=ku)["dsr"]
        ci_lo, ci_hi, ci_mean = block_bootstrap_dsr(da, db)
        sig = (dsr > 0) and (ci_lo > 0) and (dsr100 > 0.95)
        verdict = "显著" if sig else "不显著/不可区分"
        rows.append({
            "param": name, "deployed": deployed, "revert": revert,
            "sr_deployed": round(sr_a, 4), "sr_revert": round(sr_b, 4),
            "dSR": round(dsr, 4),
            "ci95": [round(ci_lo, 4), round(ci_hi, 4)],
            "p_perm": round(perm["p_value"], 4),
            "DSR@10": round(dsr10, 4), "DSR@100": round(dsr100, 4),
            "verdict": verdict,
        })
        detail[name] = {
            "n_daily": len(da), "skew": round(sk, 3), "kurt": round(ku, 3),
            "perm": perm, "ci_boot_mean": round(ci_mean, 4),
            "ret_deployed": res_a.get("total_return"),
            "ret_revert": res_b.get("total_return"),
            "mdd_deployed": res_a.get("max_drawdown"),
            "mdd_revert": res_b.get("max_drawdown"),
            "trades_deployed": res_a.get("n_trades"),
            "trades_revert": res_b.get("n_trades"),
        }
        print(f"[done] {name}: dSR={dsr:+.3f} CI[{ci_lo:+.3f},{ci_hi:+.3f}] "
              f"p={perm['p_value']:.3f} DSR100={dsr100:.3f} → {verdict} "
              f"({time.time()-t0:.0f}s)")

    elapsed = time.time() - t0
    out = {
        "data": {
            "source": "tushare panel (storage/hist_cache/ts_panel_*.pkl)",
            "codes": len(codes), "days": len(dates),
            "range": [str(dates[0]), str(dates[-1])],
        },
        "settings": {"n_perm": N_PERM, "seed_perm": SEED_PERM,
                     "n_boot": N_BOOT, "block": BLOCK, "seed_boot": SEED_BOOT},
        "table": rows, "detail": detail, "elapsed_sec": round(elapsed, 1),
    }
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports" / "C5_SIGNIFICANCE_AUDIT_2026-10-22.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = []
    lines.append("# C5 —— 已落盘参数显著性重审（置换检验 + DSR + block bootstrap）\n")
    lines.append(f"数据：tushare 确定性面板，{len(codes)} 只 × {len(dates)} 交易日 "
                 f"（{dates[0]}→{dates[-1]}），base_cfg 口径（成本单边 0.15%）。\n")
    lines.append("判定标准：dSR>0 且 block-bootstrap 95%CI 下界>0 且 DSR@100>0.95 → 「显著」，"
                 "否则「不显著/不可区分」。\n")
    lines.append("| 参数 | 落地值 | 回退值 | SR(落地) | SR(回退) | dSR | dSR 95%CI | p(置换) | DSR@10 | DSR@100 | 判定 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        lines.append(
            f"| {r['param']} | {r['deployed']} | {r['revert']} | {r['sr_deployed']:.3f} | "
            f"{r['sr_revert']:.3f} | {r['dSR']:+.3f} | [{r['ci95'][0]:+.3f}, {r['ci95'][1]:+.3f}] | "
            f"{r['p_perm']:.3f} | {r['DSR@10']:.3f} | {r['DSR@100']:.3f} | {r['verdict']} |")
    (ROOT / "reports" / "_c5_table.md").write_text(
        "\n".join(lines), encoding="utf-8")
    print(f"[elapsed] {elapsed:.0f}s")
    return out


if __name__ == "__main__":
    main()

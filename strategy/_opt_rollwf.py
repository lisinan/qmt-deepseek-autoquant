# -*- coding: utf-8 -*-
"""
滚动 walk-forward 稳健性研究：量化 regime 闸门（创业板指 MA60 + 强制清仓）
在连续时间窗上的贡献，定位其「稳健区间」与「失效区间」。

方法（防过拟合）：
  - 固定生产配置（不重新调参），纯粹检验「同一套已验证策略」在连续时间上的
    样本外稳健性。
  - 每个检验窗 = WINDOW 根交易日（≈3 个月），步长 STEP（≈1 个月）滚动；
    每窗用前 FIXED_WARMUP 根做指标预热，窗内交易严格无重叠。
  - 对照：BASE（无 regime，永远满仓） vs PROD（regime 指数闸门 + 强制清仓
    + trend + 波动率目标 + max_positions=5）。
  - 计算每个窗 REGIME 闸门的增量贡献（ret / Sharpe / alpha），并按「窗起始时
    创业板指是否站上 MA60」分组，检验闸门是否在它「应该」生效的下行市稳定贡献。

输出：logs/opt_rollwf.json + reports/optimization_report_2026-08-29c.html
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

from config.settings import MARKET_INDEX_CODE, INDEX_CODES  # noqa: E402
from strategy.backtest_daily import run_backtest            # noqa: E402
from strategy.opt_harness import (                          # noqa: E402
    preload, wide_universe, slice_by_index, base_cfg, FIXED_WARMUP,
)

WINDOW = 63      # 每检验窗 ≈ 3 个月（交易日）
STEP = 21        # 滚动步长 ≈ 1 个月
COUNT = 750      # 历史长度 ≈ 3 年


def main() -> None:
    codes = wide_universe()
    t0 = time.time()
    data = preload(codes + [MARKET_INDEX_CODE], COUNT)
    print(f"[数据] {len(data)} 只 × {COUNT} 根日线  载入 {time.time()-t0:.1f}s")
    if not data or MARKET_INDEX_CODE not in data:
        print("无数据 / 无指数，退出")
        return

    n = min(len(d["close"]) for d in data.values())
    print(f"[总] {n} 根日线")

    base = base_cfg()
    REG = dict(regime_mode="index", regime_index=MARKET_INDEX_CODE,
               regime_ma=60, regime_force_exit=True)
    cfg_base = base                         # 无 regime（永远满仓对照）
    cfg_prod = replace(base, **REG)         # 生产配置（regime 闸门）

    dates_full = data[codes[0]]["date"]
    idx_full = data[MARKET_INDEX_CODE]["close"]

    def bt(cfg, dsub):
        ks = [k for k in dsub.keys() if k not in INDEX_CODES]
        return run_backtest(ks, cfg, count=COUNT, preloaded=dsub)

    def idx_state_at(s):
        """窗起始 s 处：创业板指 站上 MA60? 窗内站上比例? 窗收益?"""
        lo = max(0, s - 60)
        ma60 = sum(idx_full[lo:s]) / (s - lo)
        px = idx_full[s]
        above = px > ma60
        # 窗内逐日站上 MA60 比例
        cnt = 0
        tot = 0
        for i in range(s, min(s + WINDOW, n)):
            if i - 60 >= 0:
                m = sum(idx_full[i - 60:i]) / 60
                tot += 1
                if idx_full[i] > m:
                    cnt += 1
        frac = cnt / tot if tot else 0.0
        e = min(s + WINDOW, n) - 1
        idx_ret = idx_full[e] / idx_full[s] - 1.0 if idx_full[s] else 0.0
        return above, frac, idx_ret

    starts = list(range(FIXED_WARMUP, n - WINDOW + 1, STEP))
    print(f"[窗口] {len(starts)} 个滚动窗（每窗 {WINDOW}d，步长 {STEP}d）")

    rows = []
    for s in starts:
        lo, hi = s - FIXED_WARMUP, s + WINDOW
        dsub = slice_by_index(data, lo, hi)
        rb = bt(cfg_base, dsub)
        rp = bt(cfg_prod, dsub)
        if "error" in rb or "error" in rp:
            print(f"  窗 {dates_full[s]}: ERROR skip")
            continue
        above, frac, idx_ret = idx_state_at(s)
        row = {
            "date_from": dates_full[s],
            "date_to": dates_full[min(s + WINDOW, n) - 1],
            "base_ret": rb["total_return"], "base_sh": rb["sharpe"],
            "base_mdd": rb["max_drawdown"], "base_alpha": rb["alpha"],
            "base_exp": rb["exposure"], "base_n": rb["n_trades"],
            "prod_ret": rp["total_return"], "prod_sh": rp["sharpe"],
            "prod_mdd": rp["max_drawdown"], "prod_alpha": rp["alpha"],
            "prod_exp": rp["exposure"], "prod_n": rp["n_trades"],
            "contrib_ret": rp["total_return"] - rb["total_return"],
            "contrib_sh": rp["sharpe"] - rb["sharpe"],
            "contrib_alpha": rp["alpha"] - rb["alpha"],
            "contrib_mdd": rp["max_drawdown"] - rb["max_drawdown"],
            "gate_engaged": rp["exposure"] < rb["exposure"] - 0.01,
            "idx_above_start": above, "idx_frac_above": frac,
            "idx_ret": idx_ret,
        }
        rows.append(row)
        tag = "DOWN" if not above else " UP "
        print(f"  {row['date_from']}→{row['date_to']} [{tag}] "
              f"contrib α={row['contrib_alpha']*100:+6.2f}pt "
              f"Sh={row['contrib_sh']:+.2f} "
              f"ret={row['contrib_ret']*100:+6.2f}% "
              f"exp_base={row['base_exp']*100:.0f}%→prod={row['prod_exp']*100:.0f}%")

    # ---------------- 聚合 ----------------
    k = len(rows)
    win_sh = sum(1 for r in rows if r["contrib_sh"] > 0)
    win_alpha = sum(1 for r in rows if r["contrib_alpha"] > 0)
    mean_cr = sum(r["contrib_ret"] for r in rows) / k
    mean_cs = sum(r["contrib_sh"] for r in rows) / k
    mean_ca = sum(r["contrib_alpha"] for r in rows) / k
    mean_cmd = sum(r["contrib_mdd"] for r in rows) / k

    down = [r for r in rows if not r["idx_above_start"]]
    up = [r for r in rows if r["idx_above_start"]]
    def avg(lst, key):
        return sum(r[key] for r in lst) / len(lst) if lst else 0.0
    grp = {
        "down_start": {
            "n": len(down),
            "mean_contrib_alpha": avg(down, "contrib_alpha"),
            "mean_contrib_sh": avg(down, "contrib_sh"),
            "mean_contrib_ret": avg(down, "contrib_ret"),
            "win_alpha": sum(1 for r in down if r["contrib_alpha"] > 0),
            "idx_ret": avg(down, "idx_ret"),
        },
        "up_start": {
            "n": len(up),
            "mean_contrib_alpha": avg(up, "contrib_alpha"),
            "mean_contrib_sh": avg(up, "contrib_sh"),
            "mean_contrib_ret": avg(up, "contrib_ret"),
            "win_alpha": sum(1 for r in up if r["contrib_alpha"] > 0),
            "idx_ret": avg(up, "idx_ret"),
        },
    }

    # 按年聚合
    by_year = {}
    for r in rows:
        yr = r["date_from"][:4]
        by_year.setdefault(yr, []).append(r)
    year_agg = []
    for yr in sorted(by_year):
        lst = by_year[yr]
        year_agg.append({
            "year": yr, "n": len(lst),
            "mean_contrib_alpha": avg(lst, "contrib_alpha"),
            "mean_contrib_sh": avg(lst, "contrib_sh"),
            "mean_contrib_ret": avg(lst, "contrib_ret"),
            "win_alpha": sum(1 for r in lst if r["contrib_alpha"] > 0),
        })

    # 最弱 / 最强 5 窗
    worst = sorted(rows, key=lambda r: r["contrib_alpha"])[:5]
    best = sorted(rows, key=lambda r: r["contrib_alpha"], reverse=True)[:5]

    summary = {
        "design": {"window": WINDOW, "step": STEP, "count": COUNT,
                   "n_bars": n, "n_windows": k,
                   "date_from": rows[0]["date_from"], "date_to": rows[-1]["date_to"],
                   "regime": "创业板指(399006.SZ) MA60 + 强制清仓"},
        "headline": {
            "gate_winrate_sharpe": win_sh / k,
            "gate_winrate_alpha": win_alpha / k,
            "mean_contrib_ret": mean_cr, "mean_contrib_sh": mean_cs,
            "mean_contrib_alpha": mean_ca, "mean_contrib_mdd": mean_cmd,
        },
        "by_index_state": grp,
        "by_year": year_agg,
        "worst5": worst, "best5": best,
        "rows": rows,
    }

    out = ROOT / "logs" / "opt_rollwf.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"\n[保存] {out}")
    print(f"\n=== 摘要 ===")
    print(f"滚动窗数={k}  区间 {rows[0]['date_from']}→{rows[-1]['date_to']}")
    print(f"闸门 Sharpe 胜率={win_sh/k*100:.0f}%  alpha 胜率={win_alpha/k*100:.0f}%")
    print(f"平均贡献 ret={mean_cr*100:+.2f}%  Sh={mean_cs:+.2f}  "
          f"α={mean_ca*100:+.2f}pt  MDDΔ={mean_cmd*100:+.2f}%")
    print(f"下行市起始窗(n={len(down)}): α贡献 {grp['down_start']['mean_contrib_alpha']*100:+.2f}pt, "
          f"胜率 {grp['down_start']['win_alpha']}/{len(down)}")
    print(f"上行市起始窗(n={len(up)}): α贡献 {grp['up_start']['mean_contrib_alpha']*100:+.2f}pt, "
          f"胜率 {grp['up_start']['win_alpha']}/{len(up)}")
    print(f"最弱5窗 α贡献: " + ", ".join(f"{r['date_from']}:{r['contrib_alpha']*100:+.1f}" for r in worst))


if __name__ == "__main__":
    main()

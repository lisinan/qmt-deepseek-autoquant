# -*- coding: utf-8 -*-
"""R44（2026-10-29 AM-EVOLVE 第 44 轮）：entry_max_ext_ma60 多窗口共识 + 逐折指纹。

复用 strategy/_evolve_wf.py 的窗口/统计函数，对「趋势延伸度过滤」做与
--mode consensus 同语义的 4 窗裁决（最差窗 dSh 为落盘唯一决策依据）。
纯研究脚本，不进生产链路、不注册 run_all。
"""
import sys
import time
from dataclasses import replace

sys.path.insert(0, ".")

from config.settings import INDEX_CODES, MARKET_INDEX_CODE  # noqa: E402
from strategy._evolve_wf import (aligned_universe, base_cfg, make_folds,  # noqa: E402
                                 oos_stats, preload, wide_universe)
from strategy.backtest_daily import run_backtest  # noqa: E402

COUNT = 900
DOSES = [0.0, 0.7, 0.8, 0.9]
WINDOWS = [(60, 9), (75, 7), (90, 6), (120, 4)]


def main():
    codes = wide_universe()
    t0 = time.time()
    data = preload(codes + [MARKET_INDEX_CODE, "000300.SH"], COUNT)
    d0, panel = aligned_universe(data, codes)
    data = panel
    n = len(d0)
    print(f"[数据] {len(data)} 只 × {n} 根  {d0[0]} -> {d0[-1]}  "
          f"载入 {time.time()-t0:.1f}s")

    def bt(cfg, dset):
        ks = [k for k in dset.keys() if k not in INDEX_CODES]
        return run_backtest(ks, cfg, count=COUNT, preloaded=dset)

    base = base_cfg()
    base_is = bt(base, data)
    print(f"基线(off) IS: ret={base_is['total_return']*100:+.2f}% "
          f"Sh={base_is['sharpe']:+.2f} MDD={base_is['max_drawdown']*100:.2f}% "
          f"trades={base_is['n_trades']}")

    # 各剂量 IS（含绑定检查：成交数变化 = 过滤器真的拦了入场）
    is_res = {}
    for dose in DOSES:
        cfg = replace(base, entry_max_ext_ma60=dose)
        r = bt(cfg, data)
        is_res[dose] = r
        print(f"IS ext={dose}: ret={r['total_return']*100:+.2f}% "
              f"Sh={r['sharpe']:+.2f} MDD={r['max_drawdown']*100:.2f}% "
              f"trades={r['n_trades']} win={r['win_rate']*100:.1f}%")

    # 4 窗共识
    print("\n" + "=" * 100)
    print(f"{'dose':<8}" + "".join(f"{f}x{n_:<2}".rjust(10) for f, n_ in WINDOWS)
          + f"{'最差dSh':>10}{'均值dSh':>10}{'IS_Sh':>8}  闸门②(最差≥+0.10)")
    print("=" * 100)
    summary = {}
    for dose in DOSES:
        cfg = replace(base, entry_max_ext_ma60=dose)
        per, folds_detail = [], {}
        for fold, nf in WINDOWS:
            subs_w = make_folds(data, codes, n, fold, nf)
            st_b, rs_b = oos_stats(base, subs_w, bt)
            st_c, rs_c = oos_stats(cfg, subs_w, bt)
            per.append(st_c["mean_sharpe"] - st_b["mean_sharpe"])
            folds_detail[f"{fold}x{nf}"] = [
                dict(i=k,
                     base_ret=rs_b[k]["total_return"] * 100,
                     cand_ret=rs_c[k]["total_return"] * 100)
                for k in range(len(rs_c))
                if "error" not in rs_c[k] and "error" not in rs_b[k]]
        mn = min(per)
        avg = sum(per) / len(per)
        ish = is_res[dose]["sharpe"]
        print(f"{dose:<8}" + "".join(f"{d:>+10.3f}" for d in per)
              + f"{mn:>+10.3f}{avg:>+10.3f}{ish:>+8.2f}  "
              f"{'PASS' if mn >= 0.10 else 'FAIL'}")
        summary[dose] = dict(per_window=per, worst=mn, mean=avg, is_sharpe=ish,
                             folds=folds_detail)

    # 逐折指纹（剂量 0.9）：正向应集中在「基线亏钱折」而非「基线大赚折」，
    # 否则为削峰填谷（波动平滑）而非 alpha（R39/R41 判据）。
    for dose in (0.8, 0.9):
        print(f"\n---- 逐折指纹 ext={dose}（cand−base 收益差 pt）----")
        for win, rows in summary[dose]["folds"].items():
            parts = []
            for r in rows:
                d = r["cand_ret"] - r["base_ret"]
                if abs(d) >= 0.5:
                    tag = "▲基线赚" if r["base_ret"] > 5 else (
                        "▼基线亏" if r["base_ret"] < 0 else "·")
                    parts.append(f"F{r['i']+1}{tag}{d:+.1f}")
            print(f"  {win}: {' '.join(parts) if parts else '(无显著差异折)'}")


if __name__ == "__main__":
    main()

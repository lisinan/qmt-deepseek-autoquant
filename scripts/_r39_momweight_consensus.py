# -*- coding: utf-8 -*-
"""R39（2026-10-20 AM-EVOLVE 第 39 轮）聚焦式四窗口共识。

为什么不用 `_evolve_wf.py --mode consensus`：它会把 candidates_final() 里
**全部 60+ 个历史候选**都重跑一遍（4 窗口 × 60 候选），本轮只需要少数几个配置。

候选：momentum_weight=True（动量排名加权：在前 N 名新入选里按动量降序赋
rank-weight，最强者更大、最弱者更小，均值=1 ⇒ 总资本不变、向最强者集中）。
★ 本轮同时修正了 wf 的作用点（旧实现乘在 max_single_position_pct 夹紧**之后**，
  会把单仓推到 60% ⇒ 第 18 例「回测有、实盘无」背离）。修正后 wf 作用在风险
  预算上并在 min(...) 之内，单仓上限仍然夹紧，与实盘 PositionSizer 同口径。
"""
import sys
import os
import json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from strategy import _evolve_wf as W  # noqa: E402
from dataclasses import replace  # noqa: E402

WINDOWS = [(60, 9), (75, 7), (90, 6), (120, 4)]
COUNT = 900


def main():
    codes = W.wide_universe()
    data = W.preload(codes + [W.MARKET_INDEX_CODE, "000300.SH"], COUNT)
    d0, panel = W.aligned_universe(data, codes)
    data = panel
    n = len(d0)
    print(f"[数据] {len(data)} 只 × {n} 根  {d0[0]} -> {d0[-1]}")

    def bt(cfg, dset):
        ks = [k for k in dset.keys() if k not in W.INDEX_CODES]
        return W.run_backtest(ks, cfg, count=COUNT, preloaded=dset)

    base = W.base_cfg()
    tests = {"P0_当前生产基线": base}
    for pw in (1.0, 1.5, 2.0, 3.0):
        tests[f"R39_动量加权_pow{pw}"] = replace(
            base, momentum_weight=True, momentum_weight_pow=pw)

    is_res = {name: bt(cfg, data) for name, cfg in tests.items()}
    print()
    for name in tests:
        r = is_res[name]
        print(f"IS {name:<24} ret={r['total_return']*100:+9.3f}% "
              f"Sh={r['sharpe']:+.4f} MDD={r['max_drawdown']*100:+.3f}% "
              f"trades={r.get('trades', r.get('n_trades', '?'))} "
              f"calmar={r['total_return']/abs(r['max_drawdown']):.2f}")

    print()
    hdr = f"{'配置':<24}" + "".join(f"{w[0]}x{w[1]:<2}".rjust(12) for w in WINDOWS)
    print(hdr + f"{'最差dSh':>10}{'均值dSh':>10}{'最差MDD':>10}{'最差折':>10}"
                f"{'OOS均值':>10}")
    print("-" * 116)
    out = {}
    for name, cfg in tests.items():
        row = []
        tot_pos = tot_n = 0
        worst_mdd = 0.0
        worst_fold = 0.0
        oos_shs = []
        for fold, nf in WINDOWS:
            subs = W.make_folds(data, codes, n, fold, nf)
            st, rs = W.oos_stats(cfg, subs, bt)
            b_st, b_rs = W.oos_stats(base, subs, bt)
            row.append(st["mean_sharpe"] - b_st["mean_sharpe"])
            oos_shs.append(st["mean_sharpe"])
            tot_pos += st["pos"]
            tot_n += st["n"]
            worst_mdd = min(worst_mdd, st["mean_mdd"])
            worst_fold = min(worst_fold, st["worst"])
        mean_oos = sum(oos_shs) / len(oos_shs)
        out[name] = dict(d_by_win=row, worst=min(row), mean=sum(row) / len(row),
                         pos=tot_pos, n=tot_n, mdd=worst_mdd, fold=worst_fold,
                         oos_mean=mean_oos)
        print(f"{name:<24}" + "".join(f"{v:+12.3f}" for v in row)
              + f"{min(row):+10.3f}{sum(row)/len(row):+10.3f}"
              + f"{worst_mdd*100:+10.2f}{worst_fold*100:+10.2f}"
              + f"{mean_oos:+10.3f}")

    print()
    for name, a in out.items():
        r = is_res[name]
        gap = abs(a["oos_mean"] - r['sharpe']) / abs(r['sharpe']) * 100
        print(f"{name}: 全窗正收={a['pos']}/{a['n']} ({a['pos']/a['n']*100:.1f}%)  "
              f"OOS均值={a['oos_mean']:.4f} IS_Sh={r['sharpe']:.4f} "
              f"③差距={gap:.1f}%  IS_MDD={r['max_drawdown']*100:.2f}%")

    with open("reports/_r39_momweight_consensus.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())

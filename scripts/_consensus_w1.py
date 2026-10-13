# -*- coding: utf-8 -*-
"""聚焦共识：仅对 P0 与 W1_entry_half_when_one 跑 4 窗口 walk-forward 共识。

复用 _evolve_wf 的数据装载与折划分管线，避免重跑 candidates_final() 中 ~70 个
历史候选。落盘裁决依据 = 多窗口**最差窗** dSh（防窗口运气）。
"""
import sys
import time
import json
from pathlib import Path
from dataclasses import replace

ROOT = Path("C:/Users/lisinan/Desktop/qmtIDE-deepseek")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy._evolve_wf import (  # noqa: E402
    base_cfg, oos_stats, make_folds, aligned_universe, wide_universe,
    preload, MARKET_INDEX_CODE, INDEX_CODES, run_backtest,
)

WINDOWS = [(60, 9), (75, 7), (90, 6), (120, 4)]


def main():
    count = 900
    codes = wide_universe()
    t0 = time.time()
    data = preload(codes + [MARKET_INDEX_CODE, "000300.SH"], count)
    d0, panel = aligned_universe(data, codes)
    data = panel
    n = len(d0)
    print(f"[数据] {len(data)} 只 × {n} 根（已按日期对齐） {d0[0]} -> {d0[-1]} "
          f"载入 {time.time()-t0:.1f}s")

    def bt(cfg, dset):
        ks = [k for k in dset.keys() if k not in INDEX_CODES]
        return run_backtest(ks, cfg, count=count, preloaded=dset)

    b = base_cfg()
    tests = {
        "P0_当前生产基线": b,
        "W1_entry_half_when_one": replace(b, entry_half_when_one=True),
    }
    is_res = {name: bt(cfg, data) for name, cfg in tests.items()}
    bi = is_res["P0_当前生产基线"]
    print(f"\n基线 P0 IS: ret={bi['total_return']*100:+.2f}% Sh={bi['sharpe']:+.2f} "
          f"MDD={bi['max_drawdown']*100:.2f}%")

    res = {name: [] for name in tests}
    for fold, nf in WINDOWS:
        subs_w = make_folds(data, codes, n, fold, nf)
        b_st, b_rs = oos_stats(b, subs_w, bt)
        for name, cfg in tests.items():
            st, rs = oos_stats(cfg, subs_w, bt)
            tot = sum((rs[k]["total_return"] - b_rs[k]["total_return"]) * 100
                      for k in range(len(rs))
                      if "error" not in rs[k] and "error" not in b_rs[k])
            res[name].append(dict(win=f"{fold}x{nf}",
                                  d_sh=st["mean_sharpe"] - b_st["mean_sharpe"],
                                  oos_sh=st["mean_sharpe"], mdd=st["mean_mdd"],
                                  worst=st["worst"], pos=st["pos"], n=st["n"],
                                  cum=tot))

    print(f"\n{'='*100}")
    print("多窗口共识：W1_entry_half_when_one（闸门② 默认 alpha：最差窗 dSh ≥ +0.10）")
    print("=" * 100)
    final = {}
    for name in tests:
        if name == "P0_当前生产基线":
            continue
        rs_ = res[name]
        print(f"\n=== {name} ===")
        for r in rs_:
            print(f"  {r['win']:>7} dSh={r['d_sh']:>+.3f} OOS_Sh={r['oos_sh']:+.3f} "
                  f"MDD={r['mdd']*100:+.2f}% worst={r['worst']*100:+.1f}% "
                  f"pos={r['pos']}/{r['n']} cum={r['cum']:+.1f}pt")
        mn = min(r["d_sh"] for r in rs_)
        avg = sum(r["d_sh"] for r in rs_) / len(rs_)
        mean_oos = sum(r["oos_sh"] for r in rs_) / len(rs_)
        wmdd = max(r["mdd"] for r in rs_)
        wf = min(r["worst"] for r in rs_)
        isr = is_res[name]
        gap = (abs(mean_oos - isr["sharpe"]) / abs(isr["sharpe"])
               if isr["sharpe"] else 9.9)
        g1 = (isr["total_return"] > bi["total_return"]) or (isr["sharpe"] > bi["sharpe"])
        g2 = mn >= 0.10
        g3 = gap <= 0.25
        g4 = wmdd >= -0.22
        g5 = wf > -0.15
        ok = g1 and g2 and g3 and g4 and g5
        print(f"  ① IS↑={'PASS' if g1 else 'fail'} "
              f"②最差dSh≥+0.10={'PASS' if g2 else 'fail'}(min={mn:+.3f}) "
              f"③OOS/IS gap={gap*100:.1f}%={'PASS' if g3 else 'FAIL'} "
              f"④MDD≥-22%={'PASS' if g4 else 'FAIL'} "
              f"⑤最差折>-15%={'PASS' if g5 else 'FAIL'}")
        print(f"  >>> {'通过（可落盘）' if ok else '否决（不落盘）'}")
        final[name] = dict(per_window=rs_, min_d_sh=mn, mean_d_sh=avg,
                           mean_oos_sharpe=mean_oos, worst_mdd=wmdd,
                           worst_fold=wf, is_ret=isr["total_return"],
                           is_sharpe=isr["sharpe"], is_mdd=isr["max_drawdown"],
                           gap=gap, pass_gate=ok)

    out = dict(window=f"{d0[0]}-{d0[-1]}", n=n,
               base=dict(is_ret=bi["total_return"], is_sharpe=bi["sharpe"],
                         is_mdd=bi["max_drawdown"]),
               w1=final.get("W1_entry_half_when_one"))
    Path(ROOT / "reports" / "_consensus_w1_20261010.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n[保存] reports/_consensus_w1_20261010.json")


if __name__ == "__main__":
    main()

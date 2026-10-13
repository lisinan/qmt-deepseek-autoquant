# -*- coding: utf-8 -*-
"""AM-EVOLVE R28（2026-10-13）聚焦共识：只跑 P0 与 X 系列（momentum_min_mom）。

与 scripts/_consensus_w1.py 同构（10-10 建立的先例）：复用 _evolve_wf 的数据装载、
折划分与 oos_stats 管线，避免重跑 candidates_final() 里 90+ 个历史候选。
裁决依据 = 4 套互不重叠折划分下的**最差窗** dSh（防窗口运气）。

数据源：本 shell 内 xtdata 不可达 ⇒ 注入 tushare 真实日线面板（末根 20261013）。
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

from config.settings import (  # noqa: E402
    STOCK_CODES, SECTOR_CONFIG, MARKET_INDEX_CODE, INDEX_CODES)
from scripts._pm_ts_panel_20261012 import load_ts_panel  # noqa: E402
from strategy._evolve_wf import (  # noqa: E402
    base_cfg, oos_stats, make_folds, aligned_universe, wide_universe,
    preload, run_backtest,
)

END = "20261013"
START = "20211201"
WINDOWS = [(60, 9), (75, 7), (90, 6), (120, 4)]

s = set(STOCK_CODES)
for _k, v in SECTOR_CONFIG["sectors"].items():
    for c, _n in v["stocks"]:
        s.add(c)
s.add(MARKET_INDEX_CODE)
s.add("000300.SH")
CODES = sorted(s)
PANEL = load_ts_panel(CODES, count=901, end=END, start=START)


def _fake_load_daily(code: str, count: int = 260):
    d = PANEL.get(code)
    if not d:
        return None
    n = len(d["close"])
    k = min(n, max(count, 120))
    return {kk: list(vv[-k:]) for kk, vv in d.items()}


def main() -> int:
    import strategy._evolve_wf as W
    W.load_daily = _fake_load_daily
    W._CACHE.clear()

    count = 901
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
        "X1_动量阈值+5%": replace(b, momentum_min_mom=0.05),
        "X2_动量阈值+10%": replace(b, momentum_min_mom=0.10),
        "X2b_动量阈值+14%": replace(b, momentum_min_mom=0.14),
        "X3_动量阈值+15%": replace(b, momentum_min_mom=0.15),
        "X3b_动量阈值+16%": replace(b, momentum_min_mom=0.16),
        "X4_动量阈值+20%": replace(b, momentum_min_mom=0.20),
        "X5_动量阈值-10%": replace(b, momentum_min_mom=-0.10),
        "X6_动量阈值-35%": replace(b, momentum_min_mom=-0.35),
    }
    is_res = {name: bt(cfg, data) for name, cfg in tests.items()}
    bi = is_res["P0_当前生产基线"]
    print(f"\n基线 P0 IS: ret={bi['total_return']*100:+.2f}% "
          f"Sh={bi['sharpe']:+.2f} MDD={bi['max_drawdown']*100:.2f}% "
          f"成交={bi.get('trades')} 暴露={bi.get('exposure', 0)*100:.1f}%")

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
                                  cum=tot, base_sh=b_st["mean_sharpe"]))

    print("\n" + "=" * 112)
    print("多窗口共识（闸门② alpha：最差窗 dSh ≥ +0.10）")
    print("=" * 112)
    final = {}
    for name in tests:
        if name == "P0_当前生产基线":
            continue
        rs_ = res[name]
        line = f"{name:<18}"
        for r in rs_:
            line += f"{r['d_sh']:>+10.3f} "
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
        line += (f"{mn:>+10.3f}{avg:>+10.3f}{wmdd*100:>9.2f}%{wf*100:>+9.1f}%"
                 f"{isr['total_return']*100:>+9.1f}%{isr['sharpe']:>+8.2f}"
                 f"{isr['max_drawdown']*100:>8.2f}%"
                 f"{isr.get('trades', 0):>7}"
                 f"  {'>>> 通过' if ok else '否决'}")
        print(line)
        print(f"{'':<18} 逐窗 dSh: " + "  ".join(
            f"{r['win']}={r['d_sh']:+.3f}(OOS {r['oos_sh']:.3f}/base "
            f"{r['base_sh']:.3f}, 正收 {r['pos']}/{r['n']})" for r in rs_))
        print(f"{'':<18} ①IS↑={'PASS' if g1 else 'fail'} "
              f"②min_dSh={mn:+.3f}{'PASS' if g2 else 'fail'} "
              f"③gap={gap*100:.1f}%{'PASS' if g3 else 'FAIL'} "
              f"④MDD={wmdd*100:.2f}%{'PASS' if g4 else 'FAIL'} "
              f"⑤worst={wf*100:+.1f}%{'PASS' if g5 else 'FAIL'}")
        final[name] = dict(per_window=rs_, min_d_sh=mn, mean_d_sh=avg,
                           mean_oos_sharpe=mean_oos, worst_mdd=wmdd,
                           worst_fold=wf, is_ret=isr["total_return"],
                           is_sharpe=isr["sharpe"], is_mdd=isr["max_drawdown"],
                           is_trades=isr.get("trades"),
                           is_exposure=isr.get("exposure"),
                           gap=gap, pass_gate=ok)

    out = dict(window=f"{d0[0]}-{d0[-1]}", n=n,
               base=dict(is_ret=bi["total_return"], is_sharpe=bi["sharpe"],
                         is_mdd=bi["max_drawdown"], is_trades=bi.get("trades"),
                         is_exposure=bi.get("exposure")),
               cands=final)
    p = ROOT / "reports" / "_am_consensus_mom_20261013.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[保存] {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

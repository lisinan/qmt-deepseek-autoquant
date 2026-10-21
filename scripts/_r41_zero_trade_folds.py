# -*- coding: utf-8 -*-
"""【R41 AM-EVOLVE 2026-10-21】★「零成交折」量化 —— KPI#1 / KPI#2 缺口的真实来源

背景（本轮发现）：P0 基线全样本跑下来，**权益自 2026-07-15 起逐日零变动直至
2026-10-21**（68 个交易日）。退出原因分布 {'crash':158,'hard_stop':2,
'trend_break':2,'daily_stop_flatten':1} ⇒ 末段根本没有任何建仓。

推论：walk-forward 的**每一个窗口的末折都整折落在零成交区** ⇒ 收益恒 0。
而 KPI 判定用的是 ``total_return > 0`` ⇒ 零成交折一律记为**非正收**；
同时它的 Sharpe 也被算进「折均值 Sharpe」⇒ 系统性拉低 KPI#1。

本脚本把这件事量化到「多少个折是零成交、剔除后 KPI 变成多少」，
产出供 OWNER 裁决 D4/D9（口径）与 D1（KPI#4）用的**决策就绪数字**。

只读、零副作用、不写任何配置。
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy._evolve_wf import (  # noqa: E402
    wide_universe, preload, aligned_universe, base_cfg, make_folds,
    MARKET_INDEX_CODE, INDEX_CODES,
)
from strategy.backtest_daily import run_backtest  # noqa: E402

COUNT = 900
WINDOWS = [(60, 9), (75, 7), (90, 6), (120, 4)]


def main():
    codes = wide_universe()
    data = preload(codes + [MARKET_INDEX_CODE, "000300.SH"], COUNT)
    if not data:
        print("无数据，退出")
        return 1
    d0, panel = aligned_universe(data, codes)
    if not d0:
        print("无数据（对齐失败），退出")
        return 1
    data = panel
    n = len(d0)
    print(f"[数据] {len(data)} 只 × {n} 根  {d0[0]} -> {d0[-1]}")

    def bt(cfg, dset):
        ks = [k for k in dset.keys() if k not in INDEX_CODES]
        return run_backtest(ks, cfg, count=COUNT, preloaded=dset)

    base = base_cfg()
    all_rows = []
    for fold, nf in WINDOWS:
        subs = make_folds(data, codes, n, fold, nf)
        print(f"\n--- 窗口 {fold}x{nf} ---")
        print(f"{'折':<5}{'区间':<26}{'收益%':>9}{'Sharpe':>9}{'MDD%':>8}{'成交':>6}  零成交?")
        for i, (s, e, d) in enumerate(subs):
            r = bt(base, d)
            if not r or "error" in r:
                print(f"F{i+1:<4} ERROR")
                continue
            nt = r.get("n_trades", 0)
            zero = (nt == 0)
            row = dict(window=f"{fold}x{nf}", fold=f"F{i+1}",
                       start=d0[s], end=d0[min(e, n) - 1],
                       ret=r["total_return"], sharpe=r["sharpe"],
                       mdd=r["max_drawdown"], n_trades=nt, zero_trade=zero)
            all_rows.append(row)
            print(f"F{i+1:<4}{d0[s]}~{d0[min(e, n)-1]:<15}"
                  f"{r['total_return']*100:>+8.2f}%{r['sharpe']:>+9.3f}"
                  f"{r['max_drawdown']*100:>8.2f}%{nt:>6}  "
                  f"{'★零成交' if zero else ''}")

    tot = len(all_rows)
    zero_rows = [r for r in all_rows if r["zero_trade"]]
    live = [r for r in all_rows if not r["zero_trade"]]
    pos_all = sum(1 for r in all_rows if r["ret"] > 0)
    pos_live = sum(1 for r in live if r["ret"] > 0)
    sh_all = sum(r["sharpe"] for r in all_rows) / tot
    sh_live = sum(r["sharpe"] for r in live) / len(live) if live else 0.0
    worst_all = min(r["ret"] for r in all_rows)
    worst_live = min(r["ret"] for r in live) if live else 0.0
    neg_live = [r for r in live if r["ret"] <= 0]

    print("\n" + "=" * 88)
    print("★ 零成交折量化（4 窗口共 26 折，末端锚定）")
    print("=" * 88)
    print(f"  总折数                : {tot}")
    print(f"  ★零成交折（成交=0）   : {len(zero_rows)}  "
          f"→ {', '.join(r['window']+r['fold'] for r in zero_rows)}")
    print(f"  零成交折日期区间      : "
          f"{min(r['start'] for r in zero_rows)} ~ {max(r['end'] for r in zero_rows)}"
          if zero_rows else "  零成交折: 无")
    print("-" * 88)
    print(f"  KPI#2 正收（现状口径，ret>0）      : {pos_all}/{tot} = "
          f"{pos_all/tot*100:.1f}%   目标 85.7%  ⇒ "
          f"{'PASS' if pos_all/tot >= 6/7 else 'FAIL'}")
    print(f"  KPI#2 正收（剔除零成交折）         : {pos_live}/{len(live)} = "
          f"{pos_live/len(live)*100:.1f}%   ⇒ "
          f"{'PASS' if pos_live/len(live) >= 6/7 else 'FAIL'}")
    print(f"  KPI#1 折均值 Sharpe（现状口径）    : {sh_all:+.3f}   目标 2.0  ⇒ "
          f"{'PASS' if sh_all >= 2.0 else 'FAIL'}")
    print(f"  KPI#1 折均值 Sharpe（剔除零成交）  : {sh_live:+.3f}   ⇒ "
          f"{'PASS' if sh_live >= 2.0 else 'FAIL'}")
    print(f"  最差折（现状 / 剔除零成交）        : {worst_all*100:+.2f}% / "
          f"{worst_live*100:+.2f}%   底线 -15%")
    print("-" * 88)
    print(f"  剔除后仍为负的真·亏损折 : {len(neg_live)} → "
          + ", ".join(f"{r['window']}{r['fold']}({r['ret']*100:+.2f}%)"
                      for r in neg_live))

    out = dict(n_bars=n, window=f"{d0[0]}~{d0[-1]}",
               folds=all_rows,
               kpi=dict(total=tot, zero=len(zero_rows),
                        pos_all=pos_all, pos_live=pos_live, n_live=len(live),
                        sharpe_all=sh_all, sharpe_live=sh_live,
                        worst_all=worst_all, worst_live=worst_live))
    with open("reports/_r41_zero_trade_folds.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print("\n[已写] reports/_r41_zero_trade_folds.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())

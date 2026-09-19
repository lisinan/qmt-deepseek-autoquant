# -*- coding: utf-8 -*-
"""北向资金协同闸门(NB-A)共识验证：IS + 4 窗口 OOS。

候选：northbound_mode="gate"，扫描 nb_lookback ∈ {10,20,40}。
基线：生产配置(northbound_mode=off)。
正交性：北向净买入 vs 创业板指(399006)日收益同期相关 + 增量窗口占比。
判定：晋升闸门② OOS 均值 Sharpe 提升 ≥ +0.10 才落盘，否则 REJECTED（严谨）。
"""
from __future__ import annotations
import sys, time, json, math
sys.path.insert(0, ".")
import strategy.backtest_daily as B
from strategy.backtest_daily import run_backtest, align_panel
from strategy._evolve_wf import make_folds, oos_stats, base_cfg
from strategy.opt_harness import wide_universe, preload
from data.northbound_cache import get_northbound

MARKET = "399006.SZ"
WINDOWS = [(90, 6), (60, 9), (120, 5), (180, 4)]
COUNT = 750


def main():
    codes = wide_universe()
    t0 = time.time()
    data = preload(codes + [MARKET, "000300.SH"], COUNT)
    nb = get_northbound("20221201", "20260825")          # 全局市场级序列
    ks = [c for c in codes if c in data]
    base = base_cfg()
    cands = {}
    for k in (10, 20, 40):
        c = base_cfg()
        c.northbound_mode = "gate"
        c.nb_lookback = k
        cands[f"NB_gate_lb{k}"] = c

    def bt(cfg, d):
        return run_backtest(ks, cfg, count=COUNT, preloaded=d, nb_data=nb)

    # ---- 正交性（北向 vs 指数价格）----
    dates_all, panel_all = align_panel(data)
    ic = panel_all[MARKET]["close"]
    pairs = []
    for i in range(1, len(dates_all)):
        v = nb.get(dates_all[i])
        if v is not None and ic[i - 1] > 0 and ic[i] > 0:
            pairs.append((ic[i] / ic[i - 1] - 1, v))
    if pairs:
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        mx = sum(xs) / len(xs)
        my = sum(ys) / len(ys)
        cov = sum((x - mx) * (y - my) for x, y in pairs) / len(pairs)
        sx = math.sqrt(sum((x - mx) ** 2 for x in xs) / len(xs))
        sy = math.sqrt(sum((y - my) ** 2 for y in ys) / len(ys))
        corr = cov / (sx * sy) if sx * sy > 0 else 0.0
        inc = sum(1 for x, y in pairs if y < 0 and x > -0.01)
        neg = sum(1 for _, y in pairs if y < 0) or 1
        print(f"[正交性] 北向净买入 vs 创业板指日收益 同期相关 r={corr:.3f}")
        print(f"  北向净流出日中指数未大跌(>-1%)占比={100.0*inc/neg:.1f}% "
              f"(潜在增量窗口：外资撤但价格未崩)")

    # ---- IS 全样本 ----
    print("\n=== IS (全样本) ===")
    is_r = {}
    for name, cfg in [("P0", base)] + list(cands.items()):
        is_r[name] = bt(cfg, data)
    for name, r in is_r.items():
        print(f"  {name}: ret={r['total_return']*100:+.1f}% sh={r['sharpe']:.2f} "
              f"mdd={r['max_drawdown']*100:.1f}% alpha={r['alpha']*100:+.1f}pt "
              f"exp={r['exposure']:.2f} trades={r['n_trades']}")

    # ---- OOS 共识 ----
    res = {name: [] for name in ["P0"] + list(cands)}
    for fold, nf in WINDOWS:
        subs_w = make_folds(data, ks, len(dates_all), fold, nf)
        b_st, b_rs = oos_stats(base, subs_w, bt)
        for name, cfg in [("P0", base)] + list(cands.items()):
            st, rs = oos_stats(cfg, subs_w, bt)
            dsh = st["mean_sharpe"] - b_st["mean_sharpe"]
            tot = sum((rs[i]["total_return"] - b_rs[i]["total_return"]) * 100
                      for i in range(len(rs))
                      if "error" not in rs[i] and "error" not in b_rs[i])
            res[name].append(dict(win=f"{fold}x{nf}", d_sh=dsh,
                                  oos_sh=st["mean_sharpe"], mdd=st["mean_mdd"],
                                  worst=st["worst"], pos=st["pos"], n=st["n"],
                                  cum=tot, base_sh=b_st["mean_sharpe"]))

    print("\n=== OOS 共识 (相对 P0 增量) ===")
    for name in ["P0"] + list(cands):
        rows = res[name]
        mean_dsh = sum(r["d_sh"] for r in rows) / len(rows)
        print(f"\n[{name}] 均值dSh={mean_dsh:+.3f}")
        for r in rows:
            print(f"  {r['win']}: dSh={r['d_sh']:+.3f} oosSh={r['oos_sh']:.2f} "
                  f"mdd={r['mdd']*100:.1f}% worst={r['worst']*100:+.1f}% "
                  f"pos={r['pos']}/{r['n']} cum={r['cum']:+.1f}pt")

    print("\n=== 晋升闸门② (OOS 均值 Sharpe +0.10) ===")
    verdicts = {}
    for name in cands:
        mean_dsh = sum(r["d_sh"] for r in res[name]) / len(res[name])
        verdict = "PASS" if mean_dsh >= 0.10 else "REJECTED"
        verdicts[name] = verdict
        print(f"  {name}: 均值dSh={mean_dsh:+.3f} -> {verdict}")

    out = {
        "is": {n: {k: r.get(k) for k in ("total_return", "sharpe",
                                         "max_drawdown", "alpha", "exposure",
                                         "n_trades")} for n, r in is_r.items()},
        "oos": {n: res[n] for n in res},
        "windows": [f"{f}x{n}" for f, n in WINDOWS],
        "verdicts": verdicts,
        "ortho_corr": corr if pairs else None,
    }
    with open("logs/research_northbound_wf.json", "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print(f"\nDONE {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()

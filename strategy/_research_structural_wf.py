# -*- coding: utf-8 -*-
"""周度深度研究 · 结构性候选共识验证（SYNTHESIZE 层）

复用 _evolve_wf 的 make_folds / oos_stats 与晋升闸门逻辑，对**结构性**（非参数微调）
候选跑 IS + 4 窗口 OOS 共识，作为落盘决策依据。

候选聚焦本轮真实瓶颈（全样本 alpha = -111pt，策略被等权买入持有反超）：
  S1 mom_metric="sharpe"   —— 风险调整动量排序（选更稳的趋势，改善 Sharpe）
  S2 mom_metric="residual" —— 剥离指数 beta 的残差动量（选真正跑赢市场的名字）
  S3 moneyflow_mode="rank" —— 主力资金流双因子倾斜（与价格动量正交的新 alpha 源）

用法：python strategy/_research_structural_wf.py
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

from strategy.opt_harness import (wide_universe, preload, slice_by_index,
                                   base_cfg, FIXED_WARMUP)
from strategy._evolve_wf import make_folds, oos_stats
from strategy.backtest_daily import run_backtest, BacktestConfig
from config.settings import INDEX_CODES, MARKET_INDEX_CODE

WINDOWS = [(60, 9), (75, 7), (90, 6), (120, 4)]


def main():
    codes = wide_universe()
    t0 = time.time()
    data = preload(codes + [MARKET_INDEX_CODE, "000300.SH"], 750)
    if not data:
        print("无数据，退出")
        return
    n = min(len(d["close"]) for d in data.values())
    d0 = data[codes[0]]["date"]
    print(f"[数据] {len(data)} 只 × {n} 根  {d0[0]} -> {d0[-1]}  载入 {time.time()-t0:.1f}s")

    def bt(cfg, dset, mf_data=None):
        ks = [k for k in dset.keys() if k not in INDEX_CODES]
        return run_backtest(ks, cfg, count=750, preloaded=dset, mf_data=mf_data)

    base = base_cfg()
    # ---- 候选（结构性）----
    cands = {
        "S1_mom_sharpe": replace(base, mom_metric="sharpe"),
        "S2_mom_residual": replace(base, mom_metric="residual"),
        "S3_moneyflow_rank": replace(base, moneyflow_mode="rank",
                                     moneyflow_weight=0.30),
    }

    # ---- 预取新数据轴（一次性，避免每折重复网络拉取）----
    mf_data = None
    if any(cfg.moneyflow_mode in ("gate", "rank") for cfg in cands.values()):
        try:
            from data.moneyflow_cache import preload_moneyflow
            mf_data = preload_moneyflow(list({c for c in data if c not in INDEX_CODES}),
                                        start=d0[0], end=d0[-1])
            print(f"[资金流] 预取 {len(mf_data)} 只")
        except Exception as e:
            print(f"[资金流] 预取失败: {e!r} -> S3 降级为无数据(rev=0 等效)")

    # ---- IS ----
    is_res = {"P0_当前生产基线": bt(base, data)}
    for name, cfg in cands.items():
        is_res[name] = bt(cfg, data)
    bi = is_res["P0_当前生产基线"]
    print(f"\n基线 P0 IS: ret={bi['total_return']*100:+.2f}% Sh={bi['sharpe']:+.2f} "
          f"MDD={bi['max_drawdown']*100:.2f}% alpha={bi['alpha']*100:+.1f}pt")

    # ---- OOS 共识 ----
    all_names = ["P0_当前生产基线"] + list(cands.keys())
    res = {name: [] for name in all_names}
    for fold, nf in WINDOWS:
        subs_w = make_folds(data, codes, n, fold, nf)
        b_st, b_rs = oos_stats(base, subs_w, bt)
        for name, cfg in [("P0_当前生产基线", base)] + list(cands.items()):
            # S3 资金流候选：注入预取 mf_data，避免每折重复网络拉取
            if name == "S3_moneyflow_rank" and mf_data is not None:
                rs = [bt(cfg, d, mf_data=mf_data) for _s, _e, d in subs_w]
                st = oos_stats_from(cfg, rs)[0]
            else:
                st, rs = oos_stats(cfg, subs_w, bt)
            tot = sum((rs[k]["total_return"] - b_rs[k]["total_return"]) * 100
                      for k in range(len(rs))
                      if "error" not in rs[k] and "error" not in b_rs[k])
            res[name].append(dict(win=f"{fold}x{nf}",
                                  d_sh=st["mean_sharpe"] - b_st["mean_sharpe"],
                                  oos_sh=st["mean_sharpe"], mdd=st["mean_mdd"],
                                  worst=st["worst"], pos=st["pos"], n=st["n"],
                                  cum=tot, base_sh=b_st["mean_sharpe"]))

    # ---- 晋升闸门 ----
    print(f"\n{'配置':<20}" + "".join(f"{w[0]}x{w[1]:<2}".rjust(11) for w in WINDOWS)
          + f"{'最差dSh':>10}{'均值dSh':>10}{'最差MDD':>10}{'最差折':>10}"
          f"{'IS_ret':>10}{'IS_Sh':>8}  结论")
    final = []
    for name in cands:
        rs_ = res[name]
        line = f"{name:<20}"
        for r in rs_:
            line += f"{r['d_sh']:>+10.3f} "
        mn = min(r["d_sh"] for r in rs_)
        avg = sum(r["d_sh"] for r in rs_) / len(rs_)
        mean_oos_sh = sum(r["oos_sh"] for r in rs_) / len(rs_)
        wmdd = max(r["mdd"] for r in rs_)
        wf = min(r["worst"] for r in rs_)
        isr = is_res[name]
        g1 = (isr["total_return"] > bi["total_return"]) or (isr["sharpe"] > bi["sharpe"])
        g2 = mn >= 0.10
        g4 = wmdd >= -0.22
        g5 = wf > -0.15
        gap = (abs(mean_oos_sh - isr["sharpe"]) / abs(isr["sharpe"])
               if isr["sharpe"] else 9.99)
        g3 = gap <= 0.25
        ok = g1 and g2 and g3 and g4 and g5
        line += (f"{mn:>+10.3f}{avg:>+10.3f}{wmdd*100:>9.2f}%{wf*100:>+9.1f}%"
                 f"{isr['total_return']*100:>+9.1f}%{isr['sharpe']:>+8.2f}"
                 f"  {'>>> 通过' if ok else '否决'}")
        print(line)
        print(f"{'':<20} ③OOS/IS: 均值OOS_Sh={mean_oos_sh:+.3f} vs IS_Sh={isr['sharpe']:+.2f}"
              f" → 差 {gap*100:.1f}% {'PASS' if g3 else 'FAIL'}"
              f" | ①IS↑={'PASS' if g1 else 'fail'} ②最差dSh≥+.10={'PASS' if g2 else 'fail'}"
              f" ④最差MDD≥-22%={'PASS' if g4 else 'FAIL'} ⑤最差折>-15%={'PASS' if g5 else 'FAIL'}")
        final.append(dict(name=name, per_window=rs_, min_d_sh=mn, mean_d_sh=avg,
                          mean_oos_sharpe=mean_oos_sh, worst_mdd=wmdd, worst_fold=wf,
                          is_ret=isr["total_return"], is_sharpe=isr["sharpe"],
                          is_mdd=isr["max_drawdown"], is_alpha=isr["alpha"],
                          g1=g1, g2=g2, g3=g3, g4=g4, g5=g5, gap=gap, pass_gate=ok))

    # IS 明细表
    print(f"\n{'配置':<20}{'IS_ret':>10}{'IS_Sh':>8}{'IS_MDD':>9}{'IS_alpha':>10}")
    for name in all_names:
        r = is_res[name]
        print(f"{name:<20}{r['total_return']*100:>+9.2f}%{r['sharpe']:>+8.2f}"
              f"{r['max_drawdown']*100:>8.2f}%{r['alpha']*100:>+9.1f}pt")

    out = ROOT / "logs" / "research_structural_wf.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(dict(
        window=f"{d0[0]}-{d0[-1]}", n_bars=n,
        base=dict(is_ret=bi["total_return"], is_sharpe=bi["sharpe"],
                  is_mdd=bi["max_drawdown"], is_alpha=bi["alpha"]),
        p0_oos_windows=res["P0_当前生产基线"],
        final=final,
    ), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[保存] {out}")


def oos_stats_from(cfg, rs):
    """从已算好的 backtest 结果列表构造 oos_stats 风格的聚合。"""
    ok = [r for r in rs if r and "error" not in r]
    if not ok:
        return None, rs
    return dict(
        mean_sharpe=sum(r["sharpe"] for r in ok) / len(ok),
        mean_ret=sum(r["total_return"] for r in ok) / len(ok),
        mean_mdd=sum(r["max_drawdown"] for r in ok) / len(ok),
        pos=sum(1 for r in ok if r["total_return"] > 0),
        n=len(ok),
        worst=min(r["total_return"] for r in ok),
        rets=[r["total_return"] for r in ok],
    ), rs


if __name__ == "__main__":
    main()

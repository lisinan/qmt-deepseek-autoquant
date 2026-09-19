# -*- coding: utf-8 -*-
"""新数据轴（主力资金流）专项诊断 —— 2026-09-19 OWNER 批准研发

目的：确诊前轮 S3（moneyflow_mode="rank"，dSh=+0.000）究竟是「无 alpha」还是
「harness/数据假阴性」。前轮预取仅覆盖 3 只探针的 2024-2026 缓存且未 force 全宇宙，
引擎拿到大量缺失的资金流 → 排名退化为动量排名 → 误报 0。

本脚本：
  A) 强制预热全宇宙(25 只)全周期(2022-2026)资金流缓存，报告覆盖率
  B) 用引擎原生 moneyflow 模式跑 IS + 4 窗口 OOS：
       P0   moneyflow_mode="off"                （生产基线）
       RANK_03  mode="rank"  weight=0.30  win=5
       RANK_05  mode="rank"  weight=0.50  win=10
       GATE     mode="gate"  win=5  min_amount=1.0（近窗主力净流入>0）
  C) 直接计算「动量前N」与「资金流前N」选股集合重叠度(Jaccard) —— 正交性硬指标
  D) 给出结论：资金流是否真有独立 alpha / 是否已可接回引擎闸门

用法：python strategy/_research_moneyflow_diag.py
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

from strategy.opt_harness import (wide_universe, preload, base_cfg, FIXED_WARMUP)
from strategy._evolve_wf import make_folds, oos_stats
from strategy.backtest_daily import run_backtest, BacktestConfig, align_panel
from config.settings import INDEX_CODES, MARKET_INDEX_CODE
from data.moneyflow_cache import get_moneyflow

WINDOWS = [(60, 9), (75, 7), (90, 6), (120, 4)]
MF_START, MF_END = "20220101", "20260825"


def _coverage(codes):
    """强制预热并报告资金流覆盖率。"""
    print(f"[A] 强制预热资金流缓存 {len(codes)} 只 x {MF_START}->{MF_END} ...")
    t0 = time.time()
    cov = {}
    for c in codes:
        d = get_moneyflow(c, start=MF_START, end=MF_END, force=True)
        if d:
            nn = sum(1 for v in d.values() if v.get("net_mf_amount"))
            cov[c] = (nn, len(d))
    tot_days = max((v[1] for v in cov.values()), default=0)
    frac = sum(v[0] / v[1] for v in cov.values() if v[1]) / len(cov) if cov else 0
    print(f"    命中 {len(cov)}/{len(codes)} 只；平均非空覆盖率 {frac*100:.1f}%；"
          f"单只最多 {tot_days} 交易日；耗时 {time.time()-t0:.1f}s")
    poor = [c for c, v in cov.items() if v[1] and v[0] / v[1] < 0.5] if cov else list(codes)
    if poor:
        print(f"    ! 低覆盖 {len(poor)} 只: {poor[:8]}")
    return cov, frac


def _overlap(dates, panel, mf_net, lb=60, win=5, topn=3):
    """逐月重排：动量前N 与 资金流前N 的 Jaccard 重叠度（正交性硬指标）。"""
    n = len(dates)
    jac, mom_top, mf_top = [], [], []
    for i in range(lb, n, 21):  # 约月度
        moms, flows = [], []
        for code, d in panel.items():
            cc = d["close"]
            if i < lb or cc[i] <= 0:
                continue
            base = cc[i - lb]
            if base <= 0:
                continue
            raw = cc[i] / base - 1
            if raw <= 0:
                continue
            moms.append((raw, code))
            arr = mf_net.get(code)
            fv = sum(arr[i - win:i]) if arr else 0.0
            flows.append((fv, code))
        if not moms:
            continue
        moms.sort(reverse=True)
        flows.sort(reverse=True)
        a = set(c for _, c in moms[:topn])
        # 资金流前N（仅取近窗有正净流者，模拟 gate/min_amount>0）
        bpos = set(c for fv, c in flows[:topn] if fv > 0)
        mom_top.append(a)
        mf_top.append(bpos)
        if a and bpos:
            inter = len(a & bpos)
            uni = len(a | bpos)
            jac.append(inter / uni if uni else 0.0)
    avg = sum(jac) / len(jac) if jac else 0.0
    return avg, len(mom_top)


def main():
    codes = wide_universe()
    t0 = time.time()
    data = preload(codes + [MARKET_INDEX_CODE, "000300.SH"], 750)
    if not data:
        print("无数据，退出")
        return
    n = min(len(d["close"]) for d in data.values())
    d0 = data[codes[0]]["date"]
    print(f"[数据] {len(data)} 只 x {n} 根  {d0[0]} -> {d0[-1]}  载入 {time.time()-t0:.1f}s")

    # ---- A) 覆盖率 ----
    cov, frac = _coverage([c for c in data if c not in INDEX_CODES])

    # ---- 对齐资金流到交易日轴（供重叠度计算）----
    dates_p, panel = align_panel({c: data[c] for c in data if c not in INDEX_CODES})
    mf_net = {}
    for code in panel:
        raw = get_moneyflow(code, start=dates_p[0], end=dates_p[-1])  # 命中已预热缓存
        mf_net[code] = [float(raw.get(dt, {}).get("net_mf_amount") or 0.0)
                        for dt in dates_p] if raw else [0.0] * len(dates_p)

    # ---- C) 正交性（选股集合重叠）----
    jac03, cnt = _overlap(dates_p, panel, mf_net, lb=60, win=5, topn=3)
    jac05, _ = _overlap(dates_p, panel, mf_net, lb=60, win=10, topn=5)
    print(f"[C] 动量前3 交 资金流前3 Jaccard={jac03:.3f}（{cnt}次重排）"
          f" | 前5 Jaccard(win10)={jac05:.3f}")
    if jac03 > 0.6:
        label = "高度重叠(资金流=动量重排,无新信息)"
    elif jac03 > 0.3:
        label = "部分正交"
    else:
        label = "显著正交(潜在新alpha)"
    print(f"    -> {label}")

    def bt(cfg, dset):
        ks = [k for k in dset.keys() if k not in INDEX_CODES]
        return run_backtest(ks, cfg, count=750, preloaded=dset)

    base = base_cfg()
    modes = {
        "P0_off": replace(base, moneyflow_mode="off"),
        "RANK_03": replace(base, moneyflow_mode="rank",
                          moneyflow_weight=0.30, moneyflow_window=5),
        "RANK_05": replace(base, moneyflow_mode="rank",
                          moneyflow_weight=0.50, moneyflow_window=10),
        "GATE": replace(base, moneyflow_mode="gate",
                       moneyflow_window=5, moneyflow_min_amount=1.0),
    }

    # ---- IS ----
    is_res = {name: bt(cfg, data) for name, cfg in modes.items()}
    bi = is_res["P0_off"]
    print(f"\n基线 P0 IS: ret={bi['total_return']*100:+.2f}% Sh={bi['sharpe']:+.2f} "
          f"MDD={bi['max_drawdown']*100:.2f}% alpha={bi['alpha']*100:+.1f}pt")

    # ---- OOS 共识 ----
    res = {name: [] for name in modes}
    for fold, nf in WINDOWS:
        subs_w = make_folds(data, codes, n, fold, nf)
        b_st, b_rs = oos_stats(base, subs_w, bt)
        for name, cfg in modes.items():
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
    print(f"\n{'模式':<12}" + "".join(f"{w[0]}x{w[1]}".rjust(11) for w in WINDOWS)
          + f"{'最差dSh':>10}{'均值dSh':>10}{'最差MDD':>10}{'最差折':>10}"
          f"{'IS_ret':>10}{'IS_Sh':>8}  结论")
    final = []
    for name in [m for m in modes if m != "P0_off"]:
        rs_ = res[name]
        line = f"{name:<12}"
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
        final.append(dict(name=name, min_d_sh=mn, mean_d_sh=avg,
                          mean_oos_sharpe=mean_oos_sh, worst_mdd=wmdd,
                          worst_fold=wf, is_ret=isr["total_return"],
                          is_sharpe=isr["sharpe"], is_mdd=isr["max_drawdown"],
                          is_alpha=isr["alpha"], g1=g1, g2=g2, g3=g3, g4=g4,
                          g5=g5, gap=gap, pass_gate=ok))

    print(f"\n{'模式':<12}{'IS_ret':>10}{'IS_Sh':>8}{'IS_MDD':>9}{'IS_alpha':>10}")
    for name in modes:
        r = is_res[name]
        print(f"{name:<12}{r['total_return']*100:>+9.2f}%{r['sharpe']:>+8.2f}"
              f"{r['max_drawdown']*100:>8.2f}%{r['alpha']*100:>+9.1f}pt")

    out = ROOT / "logs" / "research_moneyflow_diag.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(dict(
        window=f"{d0[0]}-{d0[-1]}", n_bars=n,
        coverage=dict(n_codes=len(cov), frac=frac,
                      poor=[c for c, v in cov.items()
                            if v[1] and v[0] / v[1] < 0.5]),
        jaccard_top3=jac03, jaccard_top5=jac05,
        base=dict(is_ret=bi["total_return"], is_sharpe=bi["sharpe"],
                  is_mdd=bi["max_drawdown"], is_alpha=bi["alpha"]),
        p0_oos_windows=res["P0_off"],
        final=final,
    ), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[保存] {out}")


if __name__ == "__main__":
    main()

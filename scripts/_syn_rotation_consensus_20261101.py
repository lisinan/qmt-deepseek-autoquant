# -*- coding: utf-8 -*-
"""SYNTHESIZE 2026-11-01 聚焦共识：政策 A（弱市期诊断）结构性候选。
复用 _evolve_wf 的内部工具，对「轮动弱换强」做 4 窗口共识复验。
R43 已撤回 enable_rotation 的「已证伪」结论（无回测支撑）；R44(10-29) 测过
enable_rotation=True 四窗共识最差 +0.069 < +0.10 ⇒ REJECTED。本轮复验确认现状。
不改动任何生产配置；仅读、仅报告。
"""
from __future__ import annotations
import sys, time
from pathlib import Path
from dataclasses import replace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from strategy.opt_harness import (wide_universe, preload, slice_by_index,
                                  base_cfg, FIXED_WARMUP)
from strategy.backtest_daily import run_backtest
from config.settings import INDEX_CODES
import strategy._evolve_wf as EW

COUNT = 900
codes = wide_universe()
t0 = time.time()
data = preload(codes + ["399006.SZ", "000300.SH"], COUNT)
n = min(len(d["close"]) for d in data.values())
dates = data[codes[0]]["date"]
print(f"[数据] {len(data)} 只 × {n} 根 (末日 {dates[-1]}) 载入 {time.time()-t0:.1f}s")
ks = [k for k in data if k not in INDEX_CODES]

b = base_cfg()
# 结构性候选（组合层弱换强），全部来自 candidates_final 的 R 系列
tests = {
    "P0_当前生产基线": b,
    "R1_轮动开启": replace(b, rotation_enabled=True),
    "R2_轮动_仅补空槽": replace(b, rotation_enabled=True, rotation_weak_to_strong=False),
    "R3_轮动_hot5": replace(b, rotation_enabled=True, rotation_hot_top_n=5),
    "R4_轮动_绕日线闸门": replace(b, rotation_enabled=True, rotation_require_daily_gate=False),
}

def bt(cfg, dset):
    return run_backtest([k for k in dset if k not in INDEX_CODES], cfg, count=900, preloaded=dset)

WINDOWS = [(60, 9), (75, 7), (90, 6), (120, 4)]
bi = {name: bt(cfg, data) for name, cfg in tests.items()}   # IS 结果
configs = dict(tests)                                       # 配置（用于 OOS）
base = configs["P0_当前生产基线"]
print(f"\n基线 P0 IS: ret={bi['P0_当前生产基线']['total_return']*100:+.2f}% Sh={bi['P0_当前生产基线']['sharpe']:+.2f} "
      f"MDD={bi['P0_当前生产基线']['max_drawdown']*100:.2f}%")

res = {name: [] for name in tests}
for fold, nf in WINDOWS:
    subs_w = EW.make_folds(data, codes, n, fold, nf)
    b_st, b_rs = EW.oos_stats(base, subs_w, bt)
    for name, cfg in tests.items():
        st, rs = EW.oos_stats(cfg, subs_w, bt)
        tot = sum((rs[k]["total_return"] - b_rs[k]["total_return"]) * 100
                  for k in range(len(rs))
                  if "error" not in rs[k] and "error" not in b_rs[k])
        res[name].append(dict(win=f"{fold}x{nf}",
                              d_sh=st["mean_sharpe"] - b_st["mean_sharpe"],
                              oos_sh=st["mean_sharpe"], mdd=st["mean_mdd"],
                              worst=st["worst"]))

print(f"\n{'配置':<22}" + "".join(f"{w[0]}x{w[1]:<2}".rjust(11) for w in WINDOWS)
      + f"{'最差dSh':>10}{'均值dSh':>10}{'最差MDD':>10}{'最差折':>10}{'IS_ret':>10}{'IS_Sh':>8}  结论")
final = []
for name in tests:
    if name == "P0_当前生产基线":
        continue
    rs_ = res[name]
    line = f"{name:<22}"
    for r in rs_:
        line += f"{r['d_sh']:>+10.3f} "
    mn = min(r["d_sh"] for r in rs_)
    avg = sum(r["d_sh"] for r in rs_) / len(rs_)
    mean_oos_sh = sum(r["oos_sh"] for r in rs_) / len(rs_)
    wmdd = max(r["mdd"] for r in rs_)
    wf = min(r["worst"] for r in rs_)
    isr = bi[name]
    g1 = (isr["total_return"] > bi["P0_当前生产基线"]["total_return"]) or (isr["sharpe"] > bi["P0_当前生产基线"]["sharpe"])
    g2 = mn >= 0.10  # TRACK=alpha 默认闸门
    g4 = wmdd >= -0.22
    g5 = wf > -0.15
    gap = (abs(mean_oos_sh - isr["sharpe"]) / abs(isr["sharpe"]) if isr["sharpe"] else 9.99)
    g3 = gap <= 0.25
    ok = g1 and g2 and g3 and g4 and g5
    line += (f"{mn:>+10.3f}{avg:>+10.3f}{wmdd*100:>9.2f}%{wf*100:>+9.1f}%"
             f"{isr['total_return']*100:>+9.1f}%{isr['sharpe']:>+8.2f}"
             f"  {'>>> 通过' if ok else '否决'}")
    print(line)
    print(f"{'':<22} ③OOS/IS: 均值OOS_Sh={mean_oos_sh:+.3f} vs IS_Sh={isr['sharpe']:+.2f} "
          f"→ 差 {gap*100:.1f}% {'PASS' if g3 else 'FAIL'}"
          f" | ①IS↑={'PASS' if g1 else 'fail'} ②最差≥+.10={'PASS' if g2 else 'fail'}"
          f" ④最差MDD≥-22%={'PASS' if g4 else 'FAIL'} ⑤最差折>-15%={'PASS' if g5 else 'FAIL'}")
    final.append(dict(name=name, min_d_sh=mn, mean_d_sh=avg, worst_mdd=wmdd,
                      worst_fold=wf, is_ret=isr["total_return"], is_sharpe=isr["sharpe"],
                      pass_gate=ok))
print("\n[结论] 政策 A 结构性候选「轮动弱换强」四窗共识复验完成，无通过项 ⇒ 维持关闭。")

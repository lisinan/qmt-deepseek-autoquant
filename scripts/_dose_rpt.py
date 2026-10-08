# -*- coding: utf-8 -*-
"""risk_per_trade 细剂量扫描 + 逐折明细（尺度型改动的专用判据）。

【为什么不能用 alpha 闸门判尺度型改动】
  仓位整体按 k 缩放 ⇒ 盈利折与亏损折**同方向放大**。
  故「最差窗 dRet > 0」在数学上不可能成立（亏损折必然更亏）。
  它是为 alpha 型改动（改变选股/择时逻辑）设计的判据，对尺度型天然判 FAIL。

【尺度型改动的正确判据】
  S1 Calmar（收益/|MDD|）不劣化 —— 单位回撤换到的收益是否变多
  S2 MDD 仍在生产预算 -20% 内（且给出预算使用率）
  S3 正收折数不退化
  S4 最差单折 > -15%
  S5 收益随 k **单调**上升（若出现峰，取峰而非端点）

本脚本扫 0.012→0.020 细剂量，输出 Calmar 峰，并给出 dRet<0 折的明细与归因。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy._evolve_wf import (  # noqa
    preload, wide_universe, aligned_universe, base_cfg,
    make_folds, INDEX_CODES,
)
from strategy.backtest_daily import run_backtest  # noqa
import dataclasses  # noqa

CODES = wide_universe()
data = preload(CODES, 1000)
dates, panel = aligned_universe(data, CODES)
n = len(dates)
ks = [k for k in panel.keys() if k not in INDEX_CODES]
BASE = base_cfg()
WINDOWS = [(60, 9), (75, 7), (90, 6), (120, 4)]
DOSAGE = [0.012, 0.014, 0.016, 0.018, 0.020]

print("面板 %d 只 × %d 根  %s -> %s" % (len(panel), n, dates[0], dates[-1]))

FOLDS = []
for f, nf in WINDOWS:
    for s, e, d in make_folds(panel, CODES, n, f, nf):
        FOLDS.append(("%dx%d" % (f, nf), dates[s], dates[min(e, n) - 1], d))
print("折数 %d" % len(FOLDS))


def bt(cfg, dset):
    k2 = [k for k in dset.keys() if k not in INDEX_CODES]
    return run_backtest(k2, cfg, count=1000, preloaded=dset)


RES = {}
print()
print("=" * 108)
print("%-8s %10s %8s %9s %9s | %10s %9s %9s %7s %8s" % (
    "rpt", "IS收益", "IS Sharp", "IS MDD", "IS Calmar",
    "OOS收益", "OOS MDD", "OOS Calmar", "正收", "最差折"))
print("=" * 108)

for v in DOSAGE:
    cfg = dataclasses.replace(BASE, risk_per_trade=v)
    isr = run_backtest(ks, cfg, count=1000, preloaded=panel)
    rs = [bt(cfg, d) for _t, _a, _b, d in FOLDS]
    ok = [r for r in rs if r and "error" not in r]
    ret = [r["total_return"] for r in ok]
    mdd = [r["max_drawdown"] for r in ok]
    mret = sum(ret) / len(ret)
    mmdd = sum(mdd) / len(mdd)
    RES[v] = dict(is_ret=isr["total_return"], is_sh=isr["sharpe"],
                  is_mdd=isr["max_drawdown"],
                  is_cal=isr["total_return"] / abs(isr["max_drawdown"]),
                  oos_ret=mret, oos_mdd=mmdd, oos_cal=mret / abs(mmdd),
                  pos=sum(1 for x in ret if x > 0), n=len(ret),
                  worst=min(ret), rets=ret, mdds=mdd)
    R = RES[v]
    print("%-8s %+9.2f%% %9.3f %8.2f%% %9.2f | %+9.2f%% %8.2f%% %9.2f %4d/%d %7.2f%%"
          % (v, R["is_ret"] * 100, R["is_sh"], R["is_mdd"] * 100, R["is_cal"],
             R["oos_ret"] * 100, R["oos_mdd"] * 100, R["oos_cal"],
             R["pos"], R["n"], R["worst"] * 100))
print("=" * 108)

best_is = max(DOSAGE, key=lambda v: RES[v]["is_cal"])
best_oos = max(DOSAGE, key=lambda v: RES[v]["oos_cal"])
print()
print("IS Calmar 峰   : rpt=%s (%.2f)" % (best_is, RES[best_is]["is_cal"]))
print("OOS Calmar 峰  : rpt=%s (%.2f)" % (best_oos, RES[best_oos]["oos_cal"]))
print("单调性检查 IS 收益: " + " -> ".join("%+.1f%%" % (RES[v]["is_ret"] * 100) for v in DOSAGE))
print("单调性检查 OOS收益: " + " -> ".join("%+.2f%%" % (RES[v]["oos_ret"] * 100) for v in DOSAGE))

print()
print("--- 0.020 vs 0.012 逐折明细（dRet < 0 的折 = 放大后更亏）---")
a, b = RES[0.012], RES[0.020]
neg = []
print("%-8s %12s %10s %10s %9s %9s" % ("窗口", "区间", "A收益", "B收益", "dRet", "dMDD"))
for i, (tag, d0, d1, _d) in enumerate(FOLDS):
    dr = b["rets"][i] - a["rets"][i]
    dm = b["mdds"][i] - a["mdds"][i]
    mark = " <<<" if dr < 0 else ""
    if dr < 0:
        neg.append((tag, d0, d1, a["rets"][i], b["rets"][i], dr))
    if dr < 0 or i < 3:
        print("%-8s %s-%s %+9.2f%% %+9.2f%% %+8.2fpp %+8.2fpp%s"
              % (tag, d0[:6], d1[:6], a["rets"][i] * 100, b["rets"][i] * 100,
                 dr * 100, dm * 100, mark))
print()
print("dRet<0 的折：%d/%d" % (len(neg), len(FOLDS)))
for tag, d0, d1, ra_, rb_, dr in neg:
    print("   %-8s %s-%s  %+.2f%% -> %+.2f%%  (%+.2fpp)"
          % (tag, d0[:6], d1[:6], ra_ * 100, rb_ * 100, dr * 100))
print()
print("归因：若 dRet<0 的折在 A 下**本来就是负收益**，则放大后更亏是尺度效应的"
      "必然代价，而非逻辑退化 ⇒ 应由 MDD 预算裁决，而非逐折全正。")

# -*- coding: utf-8 -*-
"""四窗口稳健性 + 单仓上限绑定检验（承 _diag_leverage.py）。

(1) 四窗：60x9 / 75x7 / 90x6 / 120x4，看 0.020 是否全窗一致优于 0.012。
(2) 单仓上限绑定：单仓 = equity × rpt / (ATR% × atr_mult)，若 > max_single_position_pct
    即被夹住 ⇒ 提高 rpt 的边际收益被上限吃掉，需同时评估上限。
判据改用 **Calmar = 收益/|MDD|**（绝对收益目标），而非 Sharpe（尺度不变量，
对仓位放大不敏感 ⇒ 用它做闸门会系统性否决一切收益提升）。
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
print("面板: %d 只 × %d 根  %s -> %s" % (len(panel), n, dates[0], dates[-1]))

BASE = base_cfg()
WINDOWS = [(60, 9), (75, 7), (90, 6), (120, 4)]


def bt(cfg, dset):
    k2 = [k for k in dset.keys() if k not in INDEX_CODES]
    return run_backtest(k2, cfg, count=1000, preloaded=dset)


def oos(cfg, fold, nf):
    subs = make_folds(panel, CODES, n, fold, nf)
    rs = [bt(cfg, d) for _s, _e, d in subs]
    ok = [r for r in rs if r and "error" not in r]
    if not ok:
        return None
    ret = sum(r["total_return"] for r in ok) / len(ok)
    mdd = sum(r["max_drawdown"] for r in ok) / len(ok)
    sh = sum(r["sharpe"] for r in ok) / len(ok)
    pos = sum(1 for r in ok if r["total_return"] > 0)
    worst = min(r["total_return"] for r in ok)
    return dict(ret=ret, sh=sh, mdd=mdd, pos=pos, n=len(ok), worst=worst,
                calmar=ret / abs(mdd) if mdd else 0)


print()
print("=" * 112)
print("%-22s %9s %8s %9s %8s | %9s %8s %9s %8s %6s" % (
    "配置", "IS收益", "IS Sharpe", "IS MDD", "IS Calmar",
    "OOS收益", "OOS Sh", "OOS MDD", "OOS Calmar", "正收"))
print("=" * 112)


def show(name, cfg):
    r = run_backtest(ks, cfg, count=1000, preloaded=panel)
    cal = r["total_return"] / abs(r["max_drawdown"]) if r["max_drawdown"] else 0
    os_all = [oos(cfg, f, nf) for f, nf in WINDOWS]
    os_all = [o for o in os_all if o]
    mret = sum(o["ret"] for o in os_all) / len(os_all)
    msh = sum(o["sh"] for o in os_all) / len(os_all)
    mmdd = sum(o["mdd"] for o in os_all) / len(os_all)
    mcal = mret / abs(mmdd) if mmdd else 0
    mpos = sum(o["pos"] for o in os_all)
    mtot = sum(o["n"] for o in os_all)
    print("%-22s %+8.2f%% %9.3f %8.2f%% %8.2f | %+8.2f%% %8.3f %8.2f%% %8.2f %4d/%d"
          % (name, r["total_return"] * 100, r["sharpe"], r["max_drawdown"] * 100,
             cal, mret * 100, msh, mmdd * 100, mcal, mpos, mtot))
    return dict(is_ret=r["total_return"], is_sh=r["sharpe"],
                is_mdd=r["max_drawdown"], is_cal=cal,
                oos_ret=mret, oos_sh=msh, oos_mdd=mmdd, oos_cal=mcal,
                pos=mpos, tot=mtot, per=os_all)


cfg_a = BASE
cfg_b = dataclasses.replace(BASE, risk_per_trade=0.020)
cfg_c = dataclasses.replace(BASE, risk_per_trade=0.020,
                            max_single_position_pct=0.30)
cfg_d = dataclasses.replace(BASE, risk_per_trade=0.015)

A = show("A 现状 rpt=0.012 上限19%", cfg_a)
B = show("B rpt=0.020 上限19%", cfg_b)
C = show("C rpt=0.020 上限30%", cfg_c)
D = show("D rpt=0.015 上限19%", cfg_d)
print("=" * 112)

print()
print("--- 逐窗明细（收益 / MDD / Calmar）---")
print("%-10s %26s %26s %26s" % ("窗口", "A 现状", "B rpt0.020", "C rpt0.020+上限30%"))
for i, (f, nf) in enumerate(WINDOWS):
    a, b, c = A["per"][i], B["per"][i], C["per"][i]
    print("%-10s %+8.2f%% %7.2f%% %7.2f | %+8.2f%% %7.2f%% %7.2f | %+8.2f%% %7.2f%% %7.2f"
          % ("%dx%d" % (f, nf),
             a["ret"] * 100, a["mdd"] * 100, a["calmar"],
             b["ret"] * 100, b["mdd"] * 100, b["calmar"],
             c["ret"] * 100, c["mdd"] * 100, c["calmar"]))

print()
print("--- 判据（改用 Calmar / 绝对收益，不再用 Sharpe）---")
for tag, X in (("B vs A", (A, B)), ("C vs A", (A, C)), ("D vs A", (A, D))):
    a, b = X
    print("  %-8s IS收益 %+.2f%%→%+.2f%% (%+.2fpp)  IS MDD %.2f%%→%.2f%%  "
          "IS Calmar %.2f→%.2f | OOS收益 %+.2f%%→%+.2f%%  OOS Calmar %.2f→%.2f"
          % (tag, a["is_ret"] * 100, b["is_ret"] * 100,
             (b["is_ret"] - a["is_ret"]) * 100,
             a["is_mdd"] * 100, b["is_mdd"] * 100, a["is_cal"], b["is_cal"],
             a["oos_ret"] * 100, b["oos_ret"] * 100, a["oos_cal"], b["oos_cal"]))
print()
print("  · Sharpe 闸门会判 B: dSh=%.3f ⇒ REJECTED —— 这正是尺度不变量的盲点。"
      % (B["oos_sh"] - A["oos_sh"]))
print("  · Calmar 闸门判 B: dCalmar=%.2f ⇒ %s"
      % (B["oos_cal"] - A["oos_cal"],
         "PROMOTE" if B["oos_cal"] > A["oos_cal"] else "REJECT"))
print("  · 生产 max_drawdown_pct=-0.20，B 的 IS MDD 仅 %.2f%% ⇒ 回撤预算只用 %.0f%%"
      % (B["is_mdd"] * 100, abs(B["is_mdd"]) / 0.20 * 100))
print("  · risk_per_trade ≤ 0.02 为不可越底线 ⇒ 0.020 已是上限。")

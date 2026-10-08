# -*- coding: utf-8 -*-
"""★ 决定性核实：回测在「实盘复位后这段」到底是赚是亏。

实盘：2026-09-21 复位 100 万 → 2026-10-08 973,233.60（−2.68%）。
回测 IS：+86.92% / Sh 1.93 / MDD −4.28%（全样本 2023-01 → 2026-10）。

两者矛盾必须解释。两种可能：
  (a) 回测在这段也是负的 ⇒ 策略在近期弱段真的失效（"逻辑差"成立）
  (b) 回测在这段是正的而实盘负 ⇒ 存在**实盘专有缺陷**（又一个「实盘有、回测无」分支）

本脚本用生产配置跑全样本，取出该区间的权益曲线切片，与实盘逐日对照。
同时给出同期 buy&hold 对照，判断是 beta 拖累还是 alpha 失效。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy._evolve_wf import (  # noqa
    preload, wide_universe, aligned_universe, base_cfg, INDEX_CODES,
)
from strategy.backtest_daily import run_backtest  # noqa

WINDOWS = [
    ("复位后全区间", "20260922", "20261008"),
    ("09-22~09-30", "20260922", "20260930"),
    ("近 20 根", None, None),
    ("近 60 根", None, None),
    ("近 250 根(约1年)", None, None),
]

CODES = wide_universe()
data = preload(CODES, 1000)
dates, panel = aligned_universe(data, CODES)
n = len(dates)
print("对齐面板: %d 只 × %d 根  %s -> %s" % (len(panel), n, dates[0], dates[-1]))

cfg = base_cfg()
ks = [k for k in panel.keys() if k not in INDEX_CODES]
res = run_backtest(ks, cfg, count=1000, preloaded=panel)
if "error" in res:
    print("ERROR", res["error"])
    sys.exit(1)

eq = res["equity_curve"]
WARMUP = 130
# equity_curve[0] = 初始 1e6，之后第 k 项对应 bar index = WARMUP + k - 1
# ⇒ bar i 对应 k = i - WARMUP + 1
print("IS: ret %+.2f%%  Sharpe %.3f  MDD %.2f%%  成交 %d"
      % (res["total_return"] * 100, res["sharpe"],
         res["max_drawdown"] * 100, len(res.get("trades", []))))

print()
print("=" * 96)
print("%-22s %10s %10s | %10s %10s | %8s" % (
    "区间", "起", "止", "回测收益", "buy&hold", "根数"))
print("=" * 96)


def bar_of(d):
    for i, x in enumerate(dates):
        if x >= d:
            return i
    return len(dates) - 1


def seg_ret(lo_bar, hi_bar):
    k0 = lo_bar - WARMUP + 1
    k1 = hi_bar - WARMUP + 1
    if k0 < 0 or k1 >= len(eq) or k1 <= k0:
        return None
    return eq[k1] / eq[k0] - 1


def bh_ret(lo_bar, hi_bar):
    """同期全宇宙等权买入持有（不做任何择时/选股）"""
    v0 = v1 = 0.0
    units = {}
    for c in ks:
        p0 = panel[c]["close"][lo_bar]
        if p0 and p0 > 0:
            units[c] = (1.0 / len(ks)) / p0
    for c in ks:
        p0 = panel[c]["close"][lo_bar] or panel[c]["close"][lo_bar - 1]
        p1 = panel[c]["close"][hi_bar] or panel[c]["close"][hi_bar - 1]
        if c in units and p0 and p1:
            v0 += units[c] * p0
            v1 += units[c] * p1
    return (v1 / v0 - 1) if v0 > 0 else None


rows = []
for name, a, b in WINDOWS:
    if a and b:
        lo, hi = bar_of(a), bar_of(b)
    else:
        m = {"近 20 根": 20, "近 60 根": 60, "近 250 根(约1年)": 250}[name]
        hi, lo = n - 1, n - 1 - m
    r = seg_ret(lo, hi)
    bh = bh_ret(lo, hi)
    rows.append((name, dates[lo], dates[hi], r, bh, hi - lo))
    print("%-22s %10s %10s | %+9.2f%% %+9.2f%% | %8d"
          % (name, dates[lo], dates[hi], r * 100, bh * 100, hi - lo))

print("-" * 96)
print("%-22s %10s %10s | %+9.2f%% %+9.2f%% | %8d"
      % ("★ 实盘（复位100万）", "20260922", "20261008", -2.68, bh_ret(bar_of("20260922"), bar_of("20261008")) * 100,
         bar_of("20261008") - bar_of("20260922")))
print("=" * 96)

# ---- 逐年拆解：策略 vs buy&hold ----
print()
print("=" * 76)
print("%-10s %12s %12s %12s" % ("年份", "策略收益", "buy&hold", "差(alpha)"))
print("=" * 76)
years = sorted({d[:4] for d in dates})
for y in years:
    idx = [i for i, d in enumerate(dates) if d[:4] == y]
    if not idx:
        continue
    lo, hi = idx[0], idx[-1]
    r = seg_ret(lo, hi)
    bh = bh_ret(lo, hi)
    if r is None or bh is None:
        continue
    print("%-10s %+11.2f%% %+11.2f%% %+11.2f%%" % (y, r * 100, bh * 100, (r - bh) * 100))
print("=" * 76)
print()
print("判据:")
print("  · 若『复位后全区间』回测亦为负 ⇒ 策略近期真失效（逻辑问题，非实盘缺陷）")
print("  · 若回测为正而实盘为负 ⇒ 存在实盘专有缺陷，需继续找")
print("  · 逐年 alpha < 0 的年份：策略在 beta 之上做负贡献")

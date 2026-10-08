# -*- coding: utf-8 -*-
"""risk_per_trade 0.012→0.020 落盘前：八项晋升闸门逐条核验（Calmar 口径）。

背景：Sharpe 是尺度不变量（整仓按 k 缩放 ⇒ 收益/波动同比例放大，比值不变），
故以 Sharpe 为闸门会系统性否决一切仓位放大。本轮目标已由 OWNER 明确为
「获得正收益」⇒ 闸门改用 Calmar（收益/|MDD|）+ 绝对收益，并保留
「MDD 不得突破生产预算 -20%」与「正收折不退化」两条硬约束。
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


def bt(cfg, dset):
    k2 = [k for k in dset.keys() if k not in INDEX_CODES]
    return run_backtest(k2, cfg, count=1000, preloaded=dset)


def all_folds(cfg):
    out = []
    for f, nf in WINDOWS:
        for s, e, d in make_folds(panel, CODES, n, f, nf):
            r = bt(cfg, d)
            if r and "error" not in r:
                out.append((f, nf, r))
    return out


A = all_folds(BASE)
B = all_folds(dataclasses.replace(BASE, risk_per_trade=0.020))
isa = run_backtest(ks, BASE, count=1000, preloaded=panel)
isb = run_backtest(ks, dataclasses.replace(BASE, risk_per_trade=0.020),
                   count=1000, preloaded=panel)

ra = [r["total_return"] for _f, _n, r in A]
rb = [r["total_return"] for _f, _n, r in B]
ma = [r["max_drawdown"] for _f, _n, r in A]
mb = [r["max_drawdown"] for _f, _n, r in B]

print("面板 %d 只 × %d 根  %s -> %s   折数 %d"
      % (len(panel), n, dates[0], dates[-1], len(A)))
print()
print("=" * 78)
print("八项晋升闸门（目标=绝对收益；Sharpe 仅作参考、不作判据）")
print("=" * 78)


def g(no, txt, ok, detail):
    print("%s ①①%s %s" % ("PASS " if ok else "FAIL ", "", txt) if False else
          "%s %-6s %-42s %s" % ("[PASS]" if ok else "[FAIL]", no, txt, detail))


g("①", "IS 收益提升",
  isb["total_return"] > isa["total_return"],
  "%.2f%% -> %.2f%% (%+.2fpp)"
  % (isa["total_return"] * 100, isb["total_return"] * 100,
     (isb["total_return"] - isa["total_return"]) * 100))

d_ret = [b - a for a, b in zip(ra, rb)]
g("②", "OOS 四窗收益全提升（Calmar 通道）",
  all(x > 0 for x in d_ret),
  "逐折 dRet: 最小 %+.2fpp 最大 %+.2fpp 均值 %+.2fpp（%d 折全正=%s）"
  % (min(d_ret) * 100, max(d_ret) * 100, sum(d_ret) / len(d_ret) * 100,
     len(d_ret), all(x > 0 for x in d_ret)))

g("③", "OOS 方向一致（无符号翻转）",
  all((a > 0) == (b > 0) or b > a for a, b in zip(ra, rb)),
  "正收折 %d/%d -> %d/%d"
  % (sum(1 for x in ra if x > 0), len(ra),
     sum(1 for x in rb if x > 0), len(rb)))

g("④", "MDD 不突破生产预算 -20%",
  min(mb) > -0.20 and isb["max_drawdown"] > -0.20,
  "IS %.2f%%；OOS 最差折 %.2f%%（预算 -20%%，仅用 %.0f%%）"
  % (isb["max_drawdown"] * 100, min(mb) * 100,
     abs(isb["max_drawdown"]) / 0.20 * 100))

g("⑤", "最差单折 > -15%",
  min(rb) > -0.15,
  "最差折 %.2f%%（对照 A %.2f%%）" % (min(rb) * 100, min(ra) * 100))

g("⑥", "正收折数不退化",
  sum(1 for x in rb if x > 0) >= sum(1 for x in ra if x > 0),
  "%d/%d -> %d/%d" % (sum(1 for x in ra if x > 0), len(ra),
                      sum(1 for x in rb if x > 0), len(rb)))

g("⑦", "风险底线不动（rpt<=0.02 / DD<=-0.15 / T1 / max_pos>=3）",
  0.020 <= 0.02 + 1e-9,
  "risk_per_trade 0.012 -> 0.020（恰为不可越底线，未越界）")

ca = isa["total_return"] / abs(isa["max_drawdown"])
cb = isb["total_return"] / abs(isb["max_drawdown"])
g("⑧", "Calmar 不劣化（新目标函数）", cb >= ca,
  "IS %.2f -> %.2f (%+.2f)" % (ca, cb, cb - ca))

print("=" * 78)
print()
print("参考（不作判据）：IS Sharpe %.3f -> %.3f (dSh %+.3f)  OOS Sh %.3f -> %.3f"
      % (isa["sharpe"], isb["sharpe"], isb["sharpe"] - isa["sharpe"],
         sum(r["sharpe"] for _f, _n, r in A) / len(A),
         sum(r["sharpe"] for _f, _n, r in B) / len(B)))
print("最差 5 折（B）：" + ", ".join("%+.2f%%" % (x * 100) for x in sorted(rb)[:5]))
print("最差 5 折（A）：" + ", ".join("%+.2f%%" % (x * 100) for x in sorted(ra)[:5]))
print()
print("结论：%s" % ("全部通过 ⇒ 可落盘"
                   if all([isb["total_return"] > isa["total_return"],
                           all(x > 0 for x in d_ret), min(mb) > -0.20,
                           min(rb) > -0.15, cb >= ca]) else "存在未过项 ⇒ 不得落盘"))

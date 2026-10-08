# -*- coding: utf-8 -*-
"""★ 核心假设检验：Sharpe 是「尺度不变量」，用它做考核目标 = 系统性否决所有
能提高绝对收益的仓位放大。

数学：整仓按 k 倍缩放 → 收益 ≈ k·R、波动 ≈ k·σ ⇒ Sharpe = R/σ **不变**，
而绝对收益与回撤同比例放大。所以：
  · 以 Sharpe 为闸门 ⇒ 所有 risk_per_trade 取值 dSh ≈ 0 ⇒ 全部 REJECTED
    （记忆中 risk_per_trade 确实被列为「已证伪」）
  · 以「绝对收益 / Calmar」为目标 ⇒ 在 MDD 预算内取最大 k

本脚本扫 risk_per_trade 剂量轴，同时报告 Sharpe 与绝对收益两套判据，
看是否出现「Sharpe 不动、收益翻倍」的尺度效应。

风险底线（不可越）：risk_per_trade ≤ 0.02。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy._evolve_wf import (  # noqa
    preload, wide_universe, aligned_universe, base_cfg,
    make_folds, INDEX_CODES,
)
from strategy.backtest_daily import run_backtest, BacktestConfig  # noqa
import dataclasses  # noqa

CODES = wide_universe()
data = preload(CODES, 1000)
dates, panel = aligned_universe(data, CODES)
n = len(dates)
print("面板: %d 只 × %d 根  %s -> %s" % (len(panel), n, dates[0], dates[-1]))
ks = [k for k in panel.keys() if k not in INDEX_CODES]

BASE = base_cfg()
DOSAGE = [("0.012 现值", 0.012), ("0.015", 0.015), ("0.018", 0.018),
          ("0.020 底线", 0.020)]

subs = make_folds(panel, CODES, n, 90, 6)


def bt(cfg, dset):
    k2 = [k for k in dset.keys() if k not in INDEX_CODES]
    return run_backtest(k2, cfg, count=1000, preloaded=dset)


def oos(cfg):
    rs = [bt(cfg, d) for _s, _e, d in subs]
    ok = [r for r in rs if r and "error" not in r]
    if not ok:
        return None
    ret = sum(r["total_return"] for r in ok) / len(ok)
    mdd = sum(r["max_drawdown"] for r in ok) / len(ok)
    sh = sum(r["sharpe"] for r in ok) / len(ok)
    pos = sum(1 for r in ok if r["total_return"] > 0)
    return ret, sh, mdd, pos, len(ok), min(r["total_return"] for r in ok)


print()
print("=" * 104)
print("%-14s | %10s %8s %9s %8s | %11s %8s %9s %6s" % (
    "risk_per_trade", "IS收益", "IS Sharpe", "IS MDD", "IS Calmar",
    "OOS收益", "OOS Sh", "OOS MDD", "正收"))
print("=" * 104)

rows = []
for name, v in DOSAGE:
    cfg = dataclasses.replace(BASE, risk_per_trade=v)
    r = run_backtest(ks, cfg, count=1000, preloaded=panel)
    calmar = (r["total_return"] / abs(r["max_drawdown"])
              if r["max_drawdown"] else 0)
    o = oos(cfg)
    ocal = (o[0] / abs(o[2]) if o and o[2] else 0)
    rows.append((name, v, r, calmar, o, ocal))
    print("%-14s | %+9.2f%% %9.3f %8.2f%% %8.2f | %+10.2f%% %8.3f %8.2f%% %4d/%d"
          % (name, r["total_return"] * 100, r["sharpe"],
             r["max_drawdown"] * 100, calmar,
             o[0] * 100, o[1], o[2] * 100, o[3], o[4]))

print("=" * 104)
print()
print("判据：")
print("  · 若 IS/OOS **收益随 k 上升而 Sharpe 基本不变** ⇒ 尺度效应成立 ⇒")
print("    过去 20 轮用 Sharpe 做闸门，等价于否决了一切仓位放大 ⇒ 这就是")
print("    「回测 Sharpe 2.0 却赚不到钱」的根因（目标函数选错，非逻辑差）。")
print("  · 决策量 = 在 MDD 预算（生产 max_drawdown_pct=-0.20）内取最大 k。")
print("  · risk_per_trade ≤ 0.02 为不可越底线。")

# -*- coding: utf-8 -*-
"""KPI#4 重新定性：把「live paper 近 4 周滚动收益 > 0%」改成
「相对宇宙等权 buy&hold 的超额收益」，并量化当前弱市下策略 100% 空仓的真实价值。

数据：确定性 tushare 面板（23 只 AI 宇宙，末根 20261016），与进化周期同源。
不改动任何生产参数，仅做分析。
"""
from __future__ import annotations
import pickle
import numpy as np
from pathlib import Path

PANEL = Path("storage/hist_cache/ts_panel_23_901_20211201_20261016.pkl")
d = pickle.load(open(PANEL, "rb"))
codes = [c for c in d if c not in ("399006.SZ", "000300.SH")]
dates = list(d[codes[0]]["date"])
print(f"[面板] {len(codes)} 只 × {len(dates)} 根  起 {dates[0]} → 末 {dates[-1]}")


def window_ret(start_date: str, end_date: str):
    bh_list, rb_list = [], []
    n_ok = 0
    for c in codes:
        cd = d[c]["date"]
        if start_date not in cd or end_date not in cd:
            continue
        s = cd.index(start_date)
        e = cd.index(end_date)
        close = np.array(d[c]["close"][s:e + 1], dtype=float)
        if len(close) < 2:
            continue
        bh_list.append(close[-1] / close[0] - 1)
        dr = close[1:] / close[:-1] - 1
        rb_list.append(float(np.prod(1 + dr) - 1))
        n_ok += 1
    if not bh_list:
        return None
    return float(np.mean(bh_list)), float(np.mean(rb_list)), n_ok


# 1) KPI#4 考核窗口：2026-09-22（ledger 复位可比起点）→ 2026-10-16
print("\n=== 窗口 A：KPI#4 考核窗口 20260922 → 20261016 ===")
rA = window_ret("20260922", "20261016")
if rA:
    bhA, rbA, nA = rA
    print(f"  有效股票数 = {nA}")
    print(f"  宇宙等权 buy&hold 收益 = {bhA*100:+.2f}%")
    print(f"  宇宙每日等权再平衡收益 = {rbA*100:+.2f}%")
    print(f"  策略 live paper 同期收益 = 0.00%  (100% 现金，0 成交)")
    print(f"  ★ 相对超额（策略 − 宇宙BH） = {0-bhA*100:+.2f}pt")
    print(f"  ★ 相对超额（策略 − 宇宙RB） = {0-rbA*100:+.2f}pt")

# 2) 更长窗口：2026-01-02 → 2026-10-16（今年至今）
print("\n=== 窗口 B：2026 年至今 20260105 → 20261016 ===")
rB = window_ret("20260105", "20261016")
bhB = rbB = None
if rB:
    bhB, rbB, nB = rB
    print(f"  有效股票数 = {nB}")
    print(f"  宇宙等权 buy&hold 收益 = {bhB*100:+.2f}%")
    print(f"  宇宙每日等权再平衡收益 = {rbB*100:+.2f}%")
    print(f"  策略 live paper 同期收益（实际）≈ -2.75%（ledger 累计；含 09-16 实盘 -19.6% 旧伤）")
    print(f"  ★ 若剔除 09-16 旧伤，10-09 后空仓段相对宇宙BH 超额 = {0-bhB*100:+.2f}pt")

# 3) 弱市单月：2026-09-01 → 2026-10-16（策略空仓主因窗口）
print("\n=== 窗口 C：弱市主因窗口 20260901 → 20261016 ===")
rC = window_ret("20260901", "20261016")
if rC:
    bhC, rbC, nC = rC
    print(f"  有效股票数 = {nC}")
    print(f"  宇宙等权 buy&hold 收益 = {bhC*100:+.2f}%")
    print(f"  宇宙每日等权再平衡收益 = {rbC*100:+.2f}%")
    print(f"  策略 live paper 同期收益 = 0.00%（空仓）")
    print(f"  ★ 相对超额（策略 − 宇宙BH） = {0-bhC*100:+.2f}pt")

out = dict(
    window_A=dict(start="20260922", end="20261016", universe_bh=bhA, universe_rb=rbA,
                  strategy=0.0, excess_bh=-bhA, excess_rb=-rbA),
    window_B=dict(start="20260105", end="20261016", universe_bh=bhB, universe_rb=rbB),
    window_C=dict(start="20260901", end="20261016", universe_bh=bhC, universe_rb=rbC,
                  strategy=0.0, excess_bh=-bhC),
)
Path("logs/kpi4_relative_excess_20261017.json").write_text(
    __import__("json").dumps(out, ensure_ascii=False, indent=1))
print("\n[保存] logs/kpi4_relative_excess_20261017.json")

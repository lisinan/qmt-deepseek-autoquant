"""PM-EVOLVE R42（2026-10-21 18:00）—— 替代 KPI 口径实算：逐折「相对宇宙等权买入持有」超额。

【为什么】
R40 §11 教训第 21 条：**提替代 KPI 前必须先把替代口径算出来**，否则只是把不可达
从一个口径搬到另一个口径。R40 当时建议「把 KPI#2 改为『相对宇宙 buy&hold 超额 > 0』」，
但**从未实算**。本轮把它算出来。

【动机来自本轮 D10 诊断】
2026-04-23~2026-10-21 区间宇宙个股中位收益 **−21.98%**、23 只里仅 3 只上涨
⇒ 这是一个**宇宙级 beta 崩塌期**。若策略在同一段只亏 4~10%，则「负折」不是缺陷，
而是**相对宇宙大幅领先**。本脚本逐折量化该超额。

【口径（划分不变量）】
同一日历区间无论怎么切折，**策略收益与基准收益都在同一段日期上计算**，
超额 = 策略 − 基准 ⇒ 不像「折均值 Sharpe / 正收折数」那样随划分方式翻转。

用法：
  python scripts/_r42_fold_excess.py
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy._evolve_wf import (  # noqa: E402
    aligned_universe, base_cfg, make_folds, preload, slice_by_index,
    wide_universe, INDEX_CODES, MARKET_INDEX_CODE,
)
from strategy.backtest_daily import run_backtest  # noqa: E402

SCHEMES = [(60, 9), (75, 7), (90, 6), (120, 4)]
REPORT = os.path.join("reports", "_r42_fold_excess.json")


def bh_return(dset, lo_idx_in_fold):
    """折内宇宙等权买入持有收益（用折[-1]收益，避免除零）。

    dset: 已对齐面板（含 stride/warmup 前缀）。lo_idx_in_fold 是折在本面板中的起点索引。
    """
    rets = []
    for code, d in dset.items():
        if code in INDEX_CODES:
            continue
        c = d["close"]
        valid = d["valid"]
        i0 = next((i for i in range(lo_idx_in_fold, len(c)) if valid[i]), None)
        if i0 is None:
            continue
        i1 = len(c) - 1
        while i1 > i0 and not valid[i1]:
            i1 -= 1
        if c[i0] <= 0:
            continue
        rets.append(c[i1] / c[i0] - 1)
    if not rets:
        return 0.0
    return sum(rets) / len(rets)


def main():
    codes = wide_universe()
    t0 = time.time()
    data = preload(codes + [MARKET_INDEX_CODE, "000300.SH"], 900)
    if not data:
        print("无数据，退出")
        return
    dates, panel = aligned_universe(data, codes)
    ks = [k for k in panel.keys() if k not in INDEX_CODES]
    panel = {k: panel[k] for k in ks}
    n = len(dates)
    print(f"[数据] {len(panel)} 只 × {n} 根  {dates[0]} -> {dates[-1]}  "
          f"载入 {time.time()-t0:.1f}s")

    cfg = base_cfg()

    def bt(dset):
        return run_backtest(list(dset.keys()), cfg, count=900, preloaded=dset)

    out = {}
    all_excess = []
    print("\n" + "=" * 104)
    print("【逐折】策略收益 vs 宇宙等权买入持有（同一日历区间）")
    print("=" * 104)
    for fold, nfolds in SCHEMES:
        starts = [n - fold * k for k in range(nfolds, 0, -1)]
        starts = [s for s in starts if s - 130 >= 0]
        rows = []
        for s in starts:
            lo = s - 130
            hi = min(s + fold, n)
            sub = slice_by_index(panel, lo, hi)
            r = bt(sub)
            if not r or "error" in r:
                continue
            # 折在本 sub 面板内的起点索引 = 130（stride/off）
            bh = bh_return(sub, 130)
            ret = r["total_return"]
            rows.append(dict(date=f"{dates[s]}~{dates[hi-1]}", ret=ret, bh=bh,
                             excess=ret - bh, sharpe=r["sharpe"],
                             trades=r.get("trades", 0)))
        key = f"{fold}x{nfolds}"
        out[key] = rows
        print(f"\n--- 划分 {key} ---")
        print("{:<26}{:>10}{:>12}{:>12}{:>9}{:>7}".format(
            "折区间", "策略%", "宇宙B&H%", "超额pt", "Sharpe", "成交"))
        for r in rows:
            print("{:<26}{:>10.2f}{:>12.2f}{:>12.2f}{:>9.2f}{:>7}".format(
                r["date"], r["ret"] * 100, r["bh"] * 100, r["excess"] * 100,
                r["sharpe"], r["trades"]))
        pos_abs = sum(1 for r in rows if r["ret"] > 0)
        pos_exc = sum(1 for r in rows if r["excess"] > 0)
        exs = [r["excess"] * 100 for r in rows]
        all_excess += exs
        print(f"  汇总：折数 {len(rows)}｜绝对正收 {pos_abs}/{len(rows)} "
              f"({100.0*pos_abs/len(rows):.1f}%)｜**超额正收 {pos_exc}/{len(rows)} "
              f"({100.0*pos_exc/len(rows):.1f}%)**｜超额均值 "
              f"{sum(exs)/len(exs):+.2f}pt｜最差超额 {min(exs):+.2f}pt")

    print("\n" + "=" * 104)
    print("【四套划分合并】26 折的超额分布")
    print("=" * 104)
    all_excess.sort()
    print(f"折数 {len(all_excess)}｜超额正收 {sum(1 for x in all_excess if x > 0)}"
          f"/{len(all_excess)} ({100.0*sum(1 for x in all_excess if x>0)/len(all_excess):.1f}%)"
          f"｜均值 {sum(all_excess)/len(all_excess):+.2f}pt"
          f"｜中位 {all_excess[len(all_excess)//2]:+.2f}pt"
          f"｜最差 {all_excess[0]:+.2f}pt｜最好 {all_excess[-1]:+.2f}pt")

    # ---- 负折专项：所有「绝对收益<0」的折，其超额到底是多少 ----
    print("\n" + "=" * 104)
    print("【负折专项】绝对收益为负的折 —— 相对宇宙是领先还是落后？")
    print("=" * 104)
    print("{:<10}{:<26}{:>10}{:>12}{:>12}".format(
        "划分", "折区间", "策略%", "宇宙B&H%", "超额pt"))
    for key, rows in out.items():
        for r in rows:
            if r["ret"] < 0:
                print("{:<10}{:<26}{:>10.2f}{:>12.2f}{:>12.2f}".format(
                    key, r["date"], r["ret"] * 100, r["bh"] * 100, r["excess"] * 100))

    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n[产出] {REPORT}")


if __name__ == "__main__":
    main()

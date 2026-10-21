"""PM-EVOLVE R42（2026-10-21 18:00）—— D10 弱市期独立诊断。

【为什么做这个】
R41 已证：26 折里 4 个负折**全部**是各窗口的末折，区间清一色 2026-04-23 ~ 2026-10-21，
而此前 22 折全部为正 ⇒ KPI#1/#2 的缺口是**周期依赖**，不是参数缺陷。
本轮不再扫参数，改为对这段日历区间做**结构性诊断**（纯量测，零落盘）。

【诊断什么】
1. 逐日漏斗分解：trend_up 四子条件 a/b/c/d 各自的通过率，随时间怎么退化。
2. 动量闸门（60 日收益 > 0.15）通过率，及其与 trend_up 的**交集**（真正的绑定约束）。
3. 分段对照：period A = 面板起点 ~ 2026-04-22（前 22 折所在区） vs
              period B = 2026-04-23 ~ 2026-10-21（4 个负折所在区）。
4. 个股层面：period B 里宇宙个股的收益/回撤/曾否通过 trend_up。
5. 「次约束」归属：把 a&b&c&d 拆掉时，宇宙能恢复多少通过票（只做诊断，不落盘）。

用法：
  export PATH=/c/Users/lisinan/.conda/envs/qmt/Library/bin:/c/Users/lisinan/.conda/envs/qmt/DLLs:$PATH
  export PYTHONPATH=.
  python scripts/_r42_weak_market_diag.py
"""
from __future__ import annotations

import json
import math
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy._evolve_wf import (  # noqa: E402
    aligned_universe, preload, wide_universe, INDEX_CODES, MARKET_INDEX_CODE,
)
from core import indicators as I  # noqa: E402

WEAK_START = "20260423"          # 4 个负折的共同起点（120x4 末折）
REPORT = os.path.join("reports", "_r42_weak_market_diag.json")


def _sma_seq(arr, n, i):
    """arr[:i+1] 的 n 日均值（末尾值），不足返回 None。"""
    if i + 1 < n:
        return None
    return sum(arr[i + 1 - n:i + 1]) / n


def precompute(dates, panel):
    """逐标的预算 MA20/MA60 序列、斜率、MACD 柱，避免 O(n^2)。"""
    out = {}
    for code, d in panel.items():
        c = d["close"]
        n = len(c)
        ma20 = [None] * n
        ma60 = [None] * n
        slope20 = [None] * n
        hist = [None] * n
        for i in range(n):
            if i >= 19:
                ma20[i] = sum(c[i - 19:i + 1]) / 20
            if i >= 59:
                ma60[i] = sum(c[i - 59:i + 1]) / 60
            if i >= 24 and ma20[i] is not None and ma20[i - 5] is not None:
                slope20[i] = ma20[i] - ma20[i - 5]
            if i >= 34:
                _, _, h = I.macd(c[:i + 1], 12, 26, 9)
                hist[i] = (I.last(h) if h else None)
        out[code] = dict(ma20=ma20, ma60=ma60, slope20=slope20, hist=hist)
    return out


def daily_funnel(dates, panel, prec, warmup=130):
    """逐日漏斗。返回 rows[dict]。"""
    rows = []
    n = len(dates)
    for i in range(warmup, n):
        cnt = dict(valid=0, a=0, b=0, c=0, d=0, tu=0, mom_ok=0, both=0,
                   mom_pass=0)
        top_codes = []
        for code, d in panel.items():
            if not d["valid"][i]:
                continue
            cnt["valid"] += 1
            c = d["close"]
            p = prec[code]
            ma20, ma60 = p["ma20"][i], p["ma60"][i]
            if ma20 is None or ma60 is None:
                continue
            a = c[i] > ma20
            b = ma20 > ma60
            cc = (p["slope20"][i] or 0) > 0
            dd = (p["hist"][i] or 0) >= 0
            cnt["a"] += 1 if a else 0
            cnt["b"] += 1 if b else 0
            cnt["c"] += 1 if cc else 0
            cnt["d"] += 1 if dd else 0
            tu = a and b and cc and dd
            cnt["tu"] += 1 if tu else 0
            # 动量闸门（lookback=60, min_mom=0.15）
            if i >= 60 and c[i - 60] > 0:
                raw = c[i] / c[i - 60] - 1
                cnt["mom_pass"] += 1 if raw > 0.15 else 0
                if raw > 0.15:
                    top_codes.append((raw, code, tu))
                if tu and raw > 0.15:
                    cnt["both"] += 1
                    cnt["mom_ok"] += 1
        top_codes.sort(reverse=True)
        top3 = [t[1] for t in top_codes[:3]]
        rows.append(dict(
            date=dates[i], valid=cnt["valid"],
            a=cnt["a"], b=cnt["b"], c=cnt["c"], d=cnt["d"],
            tu=cnt["tu"], mom_pass=cnt["mom_pass"], both=cnt["both"],
            top3_tu=sum(1 for t in top_codes[:3] if t[2]),
            top3=top3,
        ))
    return rows


def agg(rows, lo=None, hi=None):
    seg = [r for r in rows
           if (lo is None or r["date"] >= lo) and (hi is None or r["date"] <= hi)]
    if not seg:
        return {}
    nv = max(1, sum(r["valid"] for r in seg))
    n = len(seg)
    return dict(
        days=n,
        avg_valid=round(sum(r["valid"] for r in seg) / n, 2),
        a=round(sum(r["a"] for r in seg) / n, 2),
        b=round(sum(r["b"] for r in seg) / n, 2),
        c=round(sum(r["c"] for r in seg) / n, 2),
        d=round(sum(r["d"] for r in seg) / n, 2),
        tu=round(sum(r["tu"] for r in seg) / n, 2),
        mom_pass=round(sum(r["mom_pass"] for r in seg) / n, 2),
        both=round(sum(r["both"] for r in seg) / n, 2),
        top3_tu=round(sum(r["top3_tu"] for r in seg) / n, 2),
        # 「有票可买」天数占比（>=2 只同时过 trend_up 且过动量门槛）
        days_both_ge2=round(100.0 * sum(1 for r in seg if r["both"] >= 2) / n, 1),
        days_both_ge1=round(100.0 * sum(1 for r in seg if r["both"] >= 1) / n, 1),
        days_top3_tu_ge1=round(100.0 * sum(1 for r in seg if r["top3_tu"] >= 1) / n, 1),
        # 相对宇宙宽度的通过率
        tu_rate=round(100.0 * sum(r["tu"] for r in seg) / nv, 2),
        both_rate=round(100.0 * sum(r["both"] for r in seg) / nv, 2),
        d_rate=round(100.0 * sum(r["d"] for r in seg) / nv, 2),
        b_rate=round(100.0 * sum(r["b"] for r in seg) / nv, 2),
    )


def monthly(rows):
    buckets = defaultdict(list)
    for r in rows:
        buckets[r["date"][:6]].append(r)
    return {k: agg(v) for k, v in sorted(buckets.items())}


def per_stock(dates, panel, lo, hi):
    """period B 的个股表现 + 是否曾通过 trend_up。"""
    idx = [i for i, dt in enumerate(dates) if lo <= dt <= hi]
    if not idx:
        return []
    i0, i1 = idx[0], idx[-1]
    out = []
    for code, d in panel.items():
        c = d["close"]
        valid = [d["valid"][i] for i in range(i0, i1 + 1)]
        if not any(valid):
            continue
        r0, r1 = c[i0], c[i1]
        if r0 <= 0:
            continue
        ret = r1 / r0 - 1
        peak = -1e9
        mdd = 0.0
        for k in range(i0, i1 + 1):
            if not d["valid"][k]:
                continue
            peak = max(peak, c[k])
            if peak > 0:
                mdd = min(mdd, c[k] / peak - 1)
        out.append(dict(code=code, ret=round(ret * 100, 2),
                        mdd=round(mdd * 100, 2)))
    out.sort(key=lambda x: -x["ret"])
    return out


def main():
    codes = wide_universe()
    data = preload(codes + [MARKET_INDEX_CODE, "000300.SH"], 900)
    if not data:
        print("无数据，退出")
        return
    dates, panel = aligned_universe(data, codes)
    ks = [k for k in panel.keys() if k not in INDEX_CODES]
    panel = {k: panel[k] for k in ks}
    print(f"[数据] {len(panel)} 只 × {len(dates)} 根  {dates[0]} -> {dates[-1]}")

    print("[计算] 预计算 MA20/MA60/斜率/MACD …")
    prec = precompute(dates, panel)
    rows = daily_funnel(dates, panel, prec)
    print(f"[漏斗] {len(rows)} 个交易日（warmup 130 之后）")

    A = agg(rows, hi="20260422")
    B = agg(rows, lo=WEAK_START)
    # 更细的两段：把 4 个负折的整体 vs 最末 6 个月再切一刀
    B1 = agg(rows, lo="20260423", hi="20260715")     # 弱市前半（弱 часто signals）
    B2 = agg(rows, lo="20260716")                    # 回测零暴露段（2026-07-15 起）

    print("\n" + "=" * 96)
    print("【分段对照】逐日漏斗均值（单位：只数，宇宙约 23 只）")
    print("=" * 96)
    hdr = ["段", "区间", "天数", "a收>MA20", "b MA20>MA60", "c 斜率>0",
           "d MACD>=0", "trend_up", "动量>15%", "两者交集", "top3中过tu"]
    print("{:<6}{:<22}{:>5}{:>10}{:>13}{:>10}{:>11}{:>10}{:>10}{:>10}{:>12}".format(*hdr))
    for name, seg, lohi in [("A", A, f"{rows[0]['date']}~20260422"),
                            ("B", B, "20260423~20261021"),
                            ("B1", B1, "20260423~20260715"),
                            ("B2", B2, "20260716~20261021")]:
        if not seg:
            continue
        print("{:<6}{:<22}{:>5}{:>10}{:>13}{:>10}{:>11}{:>10}{:>10}{:>10}{:>12}".format(
            name, lohi, seg["days"], seg["a"], seg["b"], seg["c"], seg["d"],
            seg["tu"], seg["mom_pass"], seg["both"], seg["top3_tu"]))

    print("\n" + "=" * 96)
    print("【比率口径】相对宇宙宽度的通过率（%）+「有票可买」天数占比")
    print("=" * 96)
    print("{:<6}{:>10}{:>12}{:>12}{:>10}{:>16}{:>16}{:>18}".format(
        "段", "tu率%", "交集率%", "MACD率%", "MA60率%", "交集>=2只天%", "交集>=1只天%", "top3有过tu天%"))
    for name, seg in [("A", A), ("B", B), ("B1", B1), ("B2", B2)]:
        if not seg:
            continue
        print("{:<6}{:>10}{:>12}{:>12}{:>10}{:>16}{:>16}{:>18}".format(
            name, seg["tu_rate"], seg["both_rate"], seg["d_rate"], seg["b_rate"],
            seg["days_both_ge2"], seg["days_both_ge1"], seg["days_top3_tu_ge1"]))

    print("\n" + "=" * 96)
    print("【月度】逐月漏斗（a/b/c/d/tu/交集/top3过tu，单位只）")
    print("=" * 96)
    print("{:<8}{:>4}{:>6}{:>6}{:>6}{:>6}{:>6}{:>7}{:>7}{:>8}{:>12}".format(
        "月", "天", "valid", "a", "b", "c", "d", "tu", "交集", "top3tu", "交集>=2天%"))
    for k, v in monthly(rows).items():
        print("{:<8}{:>4}{:>6}{:>6}{:>6}{:>6}{:>6}{:>6}{:>7}{:>8}{:>12}".format(
            k, v["days"], v["avg_valid"], v["a"], v["b"], v["c"], v["d"],
            v["tu"], v["both"], v["top3_tu"], v["days_both_ge2"]))

    stk = per_stock(dates, panel, WEAK_START, dates[-1])
    print("\n" + "=" * 96)
    print(f"【个股】period B（{WEAK_START}~{dates[-1]}）宇宙个股表现，按收益降序")
    print("=" * 96)
    for i in range(0, len(stk), 4):
        print("  ".join(f"{s['code']}:{s['ret']:+7.2f}%(mdd{s['mdd']:+.1f})"
                        for s in stk[i:i + 4]))
    med = sorted(s["ret"] for s in stk)
    med = med[len(med) // 2] if med else 0.0
    up = sum(1 for s in stk if s["ret"] > 0)
    print(f"\n个股：{up}/{len(stk)} 只上涨，中位 {med:+.2f}%，"
          f"均值 {sum(s['ret'] for s in stk)/max(1,len(stk)):+.2f}%")

    payload = dict(
        panel=f"{len(panel)}只×{len(dates)}根 {dates[0]}~{dates[-1]}",
        rows=rows, seg=dict(A=A, B=B, B1=B1, B2=B2),
        monthly=monthly(rows), per_stock=stk,
    )
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"\n[产出] {REPORT}")


if __name__ == "__main__":
    main()

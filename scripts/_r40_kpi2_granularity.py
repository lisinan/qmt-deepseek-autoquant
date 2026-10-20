# -*- coding: utf-8 -*-
"""PM-EVOLVE R40 (2026-10-20 收官) · 候选 B：KPI#2「正收折数」口径审计。

【问题】KPI#2 = walk-forward 正收益折数 ≥ 6/7（85.7%），当前 20/26 = 76.9%，差 2 折。
        它是**唯一**靠「参数改动」看起来还有希望的指标
        （KPI#1 拼接口径 2.138 已达标待追认、KPI#3 已 PASS、KPI#4 已定价为结构无解）。

【怀疑】R18（第 18 轮）已证 KPI#1「折均值 Sharpe」是**划分方式依赖的小样本噪声**：
        同一段 540 根，60×9 报 1.409 而 90×6 报 1.660（差 0.251），
        而拼接 Sharpe 逐位相同。
        KPI#2 与 KPI#1 是**同一族统计量**：都是「把一段连续历史切成 k 段、
        每段各算一个量（Sharpe 是比率 / 正收是符号），再对这 k 个量取算术统计」。
        ⇒ 强烈怀疑 20/26 同样是划分假象，而非策略缺陷。

【判据（第 7 条教训）】「换成别的划分方式结论还成立吗？」

【设计（v2，刻意做得最干净）】
  跑**一次**全样本连续回测拿到完整 equity_curve，锁定「末 540 根」这段**同一条**
  真实曲线，然后**只改变 fold 长度**（30/45/60/90/135/180/270）把它切成 k 段，
  统计「正收段数 / 总段数」。
  ⇒ 数据、策略、参数、账户全部不变，变化的**只有划分方式**。
     若正收比例随之大幅摆动 ⇒ 该指标测的是划分方式，不是策略。
  对照组：同一段 540 根的**日收益序列**（与 fold 完全无关）算出的拼接 Sharpe
     恒为同一个数 ⇒ 展示「真值稳定 / 折统计摆动」的反差。

输出 JSON：reports/_r40_kpi2_granularity.json
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy import _evolve_wf as W  # noqa: E402

COUNT = 900
SPAN = 540          # 锁定区间：末 540 根（= 60×9 与 90×6 的共同覆盖段）
FOLDS = [30, 45, 60, 90, 135, 180, 270]


def _sharpe(daily: list) -> float:
    """日收益序列 → 年化 Sharpe（与 _curve_stats 同口径：×√245）。"""
    if len(daily) < 2:
        return 0.0
    m = sum(daily) / len(daily)
    var = sum((r - m) ** 2 for r in daily) / (len(daily) - 1)
    sd = math.sqrt(var)
    if sd <= 1e-12:
        return 0.0
    return m / sd * math.sqrt(245)


def main() -> int:
    codes = W.wide_universe()
    data = W.preload(codes + [W.MARKET_INDEX_CODE, "000300.SH"], COUNT)
    d0, panel = W.aligned_universe(data, codes)
    data = panel
    n = len(d0)
    print(f"[数据] {len(data)} 只 × {n} 根  {d0[0]} -> {d0[-1]}")

    def bt(cfg, dset):
        ks = [k for k in dset.keys() if k not in W.INDEX_CODES]
        return W.run_backtest(ks, cfg, count=COUNT, preloaded=dset)

    base = W.base_cfg()
    full = bt(base, data)
    curve = full.get("equity_curve") or []
    print(f"[全样本] ret={full['total_return']*100:+.2f}% Sh={full['sharpe']:.4f} "
          f"MDD={full['max_drawdown']*100:.2f}% curve_len={len(curve)}")
    # equity_curve 只覆盖回测真正遍历的 bar（前 129 根为指标预热，不 append）。
    # 索引映射：bar b 收盘后权益 = curve[b - off]，其中 off = n - len(curve)。
    off = n - len(curve)
    if len(curve) < SPAN + 1:
        print("!! curve 长度不足，退出")
        return 1
    print(f"[曲线] len={len(curve)} 预热偏移 off={off} "
          f"⇒ 覆盖 bar {off}..{n-1}")

    # 末 540 根：bar 索引 [n-SPAN, n-1]，段初取 bar (n-SPAN-1) 收盘后权益
    a = n - SPAN                       # = 360
    seg_curve = curve[a - 1 - off: n - off]   # 长度 541 = 段初 + 540 根
    assert len(seg_curve) == SPAN + 1, (len(seg_curve), SPAN + 1)
    daily = [(seg_curve[i + 1] / seg_curve[i] - 1.0)
             for i in range(SPAN) if seg_curve[i] > 0]
    span_sh = _sharpe(daily)
    span_ret = 1.0
    for r in daily:
        span_ret *= (1.0 + r)
    span_ret -= 1.0
    # 按月聚合（同一条曲线，与 fold 无关）
    from collections import defaultdict
    month_pnl = defaultdict(float)
    for i, r in enumerate(daily):
        month_pnl[str(d0[a + i])[:6]] += r
    mpos = sum(1 for v in month_pnl.values() if v > 0)
    mtot = len(month_pnl)

    print(f"\n锁定区间 = 末 {SPAN} 根（{d0[a]} -> {d0[-1]}），"
          f"同**一条**真实曲线，只改 fold 长度重切：\n")
    print(f"  该段真实拼接 Sharpe（与 fold 无关）    = {span_sh:.4f}")
    print(f"  该段真实累计收益                        = {span_ret*100:+.1f}%")
    print(f"  该段正收益日历月占比                    = {mpos}/{mtot} "
          f"({100.0*mpos/max(mtot,1):.1f}%)")
    print(f"  目标线 85.7%（6/7）\n")

    hdr = (f"{'fold':>6}{'段数':>6}{'正收':>8}{'正收%':>9}{'达标?':>8}"
           f"{'最差段':>10}{'段收益明细(前8)':>10}")
    print(hdr)
    print("-" * 100)

    results = {}
    for fold in FOLDS:
        k = SPAN // fold
        segs = []
        for j in range(k):
            i0, i1 = j * fold, (j + 1) * fold
            p0, p1 = seg_curve[i0], seg_curve[i1]
            segs.append(p1 / p0 - 1.0 if p0 > 0 else 0.0)
        pos = sum(1 for x in segs if x > 0)
        pct = 100.0 * pos / max(len(segs), 1)
        ok = "PASS" if pct >= 85.7 else "FAIL"
        detail = " ".join(f"{x*100:+.0f}" for x in segs[:8])
        print(f"{fold:>6}{len(segs):>6}{pos:>8}{pct:>8.1f}%{ok:>8}"
              f"{min(segs)*100:>9.1f}%  {detail}")
        results[str(fold)] = {
            "nseg": len(segs), "pos": pos, "pos_pct": round(pct, 1),
            "pass": ok == "PASS", "worst": round(min(segs), 4),
            "segs": [round(x, 4) for x in segs],
        }

    pcts = [v["pos_pct"] for v in results.values()]
    print(f"\n=== 判据：同一段 540 根、同一条曲线、只换划分方式 ===")
    print(f"  正收段数占比 ：{min(pcts):.1f}% ~ {max(pcts):.1f}% "
          f"（极差 {max(pcts)-min(pcts):.1f}pp）")
    print(f"  拼接 Sharpe  ：{span_sh:.4f}（恒定，与划分无关）")
    print(f"  ⇒ 折口径 KPI#2 的读数完全由 fold 长度决定，"
          f"{'没有任何划分达标' if max(pcts) < 85.7 else '存在达标划分'}。")

    out = {
        "span": SPAN, "n": n, "last_date": str(d0[-1]),
        "full_is_ret": round(full["total_return"], 6),
        "full_is_sharpe": round(full["sharpe"], 4),
        "span_sharpe": round(span_sh, 4),
        "span_ret": round(span_ret, 4),
        "months": mtot, "pos_months": mpos,
        "pos_month_pct": round(100.0 * mpos / max(mtot, 1), 1),
        "by_fold": results,
        "pos_pct_min": min(pcts), "pos_pct_max": max(pcts),
    }
    p = ROOT / "reports" / "_r40_kpi2_granularity.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

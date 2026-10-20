# -*- coding: utf-8 -*-
"""PM-EVOLVE R40 (2026-10-20 收官) · 承接 AM R39 §7.4「全因子分布对齐」。

背景（AM R39 §5 新疑点）：
  - `buy_score_threshold` 在**回测中恒不绑定**（3.0/3.5 与 4.0 逐位相同），
    `min_signals` 1/2 与 3 逐位相同 ⇒ 回测里凡通过前序闸门（trend_up + 动量 top3）
    的候选，score 一律 ≥ 4.0。
  - 但实盘 R26 实测「最高分 3.58 < 4.0」时评分门槛是**硬约束**。
  ⇒ 疑点：回测评分分布与实盘评分分布**不是同一分布**，回测可能高估开仓频率。

R27 已做过一次对齐，但只归因到 vwap 因子，且只测了末根 + 近 250 根。
本脚本做**全因子逐因子归因**，回答三个问题：
  Q1. 除 vwap 外，还有没有第二个「起点依赖型」因子造成 full/live 分布差异？
      （RSI/MACD/ATR/BOLL/KDJ 都是递归或滚动指标，理论上差异应衰减；实测证伪/证实）
  Q2. 回测口径判定可用的候选（score≥4.0），在 live 口径下有多少 <4.0（实盘会漏买）？
  Q3. score 差异是否 100% 由 vwap 因子解释（即「除 vwap 外零差异」）？

输出 JSON：reports/_r40_factor_align.json
"""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy import _evolve_wf as W                       # noqa: E402
from strategy.backtest_daily import score_daily_series     # noqa: E402
from strategy.daily_context import DailyContext, DAILY_COUNT  # noqa: E402

COUNT = 900
TAIL = DAILY_COUNT          # 120，与 live 一致
LOOKBACK = 400              # 逐日对照的根数
FACTOR_KEYS = ["trend", "momentum", "oversold", "volume", "position", "vwap"]


def main() -> int:
    codes = W.wide_universe()
    data = W.preload(codes, COUNT)
    dates0, panel = W.aligned_universe(data, codes)
    data = panel
    n = len(dates0)
    print(f"[数据] {len(data)} 只 × {n} 根  {dates0[0]} -> {dates0[-1]}")

    per_factor_mismatch = collections.Counter()
    score_mismatch = 0
    total = 0
    # Q3：score 不同但 vwap 相同的样本（= 非 vwap 差异源）
    non_vwap_driven = []
    # Q2：回测 score>=4.0 而 live <4.0
    buy_full = 0
    missed_by_live = 0
    extra_by_live = 0
    # 因子差异幅度分布
    delta_by_factor = collections.defaultdict(list)
    live_score_all = []
    full_score_all = []

    lo = max(TAIL, n - LOOKBACK)
    for code, d in data.items():
        o, h, l, cl, v = d["open"], d["high"], d["low"], d["close"], d["volume"]
        full_series = score_daily_series(o, h, l, cl, v)
        for i in range(lo, n):
            tail = {k: list(val[i - TAIL + 1:i + 1]) for k, val in d.items()}
            lf = DailyContext._compute_for_test(code, tail)
            s_full, f_full = full_series[i]
            f_live = lf.factors
            total += 1
            full_score_all.append(s_full)
            live_score_all.append(lf.score)

            diffs = []
            for k in FACTOR_KEYS:
                a = f_full.get(k)
                b = f_live.get(k)
                if abs((a or 0.0) - (b or 0.0)) > 1e-9:
                    per_factor_mismatch[k] += 1
                    delta_by_factor[k].append(round((a or 0.0) - (b or 0.0), 3))
                    diffs.append(k)

            if abs(s_full - lf.score) > 1e-9:
                score_mismatch += 1
                if "vwap" not in diffs:
                    # score 变了但 vwap 没变 ⇒ 存在 vwap 之外的差异源
                    non_vwap_driven.append({
                        "code": code, "i": i, "date": str(dates0[i]),
                        "s_full": round(s_full, 2), "s_live": round(lf.score, 2),
                        "diff_factors": diffs,
                        "f_full": {k: f_full.get(k) for k in FACTOR_KEYS},
                        "f_live": {k: f_live.get(k) for k in FACTOR_KEYS},
                    })

            if s_full >= 4.0:
                buy_full += 1
                if lf.score < 4.0:
                    missed_by_live += 1
            if lf.score >= 4.0 and s_full < 4.0:
                extra_by_live += 1

    print(f"\n=== 样本：{total} 个 (标的, 日)，覆盖 {n-lo} 根 ===")
    print(f"score 不一致：{score_mismatch} ({100.0*score_mismatch/max(total,1):.2f}%)")
    print("\n【Q1 逐因子不一致率】")
    for k in FACTOR_KEYS:
        c = per_factor_mismatch[k]
        print(f"  {k:<10} {c:>6} / {total}  ({100.0*c/max(total,1):5.2f}%)")

    print(f"\n【Q3 非 vwap 驱动的 score 差异】{len(non_vwap_driven)} 例 "
          f"({100.0*len(non_vwap_driven)/max(score_mismatch,1):.2f}% of 不一致)")
    for r in non_vwap_driven[:10]:
        print(f"  {r['code']} {r['date']}: full={r['s_full']} live={r['s_live']} "
              f"diff_factors={r['diff_factors']}")

    print(f"\n【Q2 门槛漏买】回测 score>=4.0 = {buy_full}；"
          f"live 口径 <4.0（实盘漏买）= {missed_by_live} "
          f"({100.0*missed_by_live/max(buy_full,1):.2f}%)")
    print(f"  反向（live 会买、回测不买）= {extra_by_live}")

    def _q(vals, p):
        if not vals:
            return None
        s = sorted(vals)
        return round(s[int(p * (len(s) - 1))], 2)

    print(f"\n【分布对照】full score: mean={sum(full_score_all)/len(full_score_all):.2f} "
          f"p05={_q(full_score_all,0.05)} p50={_q(full_score_all,0.5)} "
          f"p95={_q(full_score_all,0.95)} max={max(full_score_all):.2f}")
    print(f"             live score: mean={sum(live_score_all)/len(live_score_all):.2f} "
          f"p05={_q(live_score_all,0.05)} p50={_q(live_score_all,0.5)} "
          f"p95={_q(live_score_all,0.95)} max={max(live_score_all):.2f}")

    # 末根（今日）全宇宙对照：实盘「最高分」是什么口径
    print("\n【末根对照 top8（full 口径排序）】")
    last_rows = []
    for code, d in data.items():
        o, h, l, cl, v = d["open"], d["high"], d["low"], d["close"], d["volume"]
        s_full, _ = score_daily_series(o, h, l, cl, v)[-1]
        tail = {k: list(val[-TAIL:]) for k, val in d.items()}
        lf = DailyContext._compute_for_test(code, tail)
        last_rows.append((code, s_full, lf.score, lf.trend_up))
    last_rows.sort(key=lambda x: -x[1])
    for c, sf, sl, tu in last_rows[:8]:
        print(f"  {c}: full={sf:.2f} live={sl:.2f} trend_up={tu}")
    allmax_full = max(r[1] for r in last_rows)
    allmax_live = max(r[2] for r in last_rows)
    print(f"  全宇宙最高分：full={allmax_full:.2f} live={allmax_live:.2f} "
          f"（门槛 4.0 ⇒ full 通过 {sum(1 for r in last_rows if r[1]>=4.0)} 只 / "
          f"live 通过 {sum(1 for r in last_rows if r[2]>=4.0)} 只）")
    trend_up_n = sum(1 for r in last_rows if r[3])
    print(f"  末根 trend_up 通过 = {trend_up_n} 只")

    out = {
        "sample_n": total,
        "score_mismatch_n": score_mismatch,
        "per_factor_mismatch": dict(per_factor_mismatch),
        "non_vwap_driven_n": len(non_vwap_driven),
        "non_vwap_examples": non_vwap_driven[:20],
        "buy_full_n": buy_full,
        "missed_by_live_n": missed_by_live,
        "extra_by_live_n": extra_by_live,
        "last_full_max": allmax_full,
        "last_live_max": allmax_live,
        "last_trend_up_n": trend_up_n,
        "last_date": str(dates0[-1]),
    }
    p = ROOT / "reports" / "_r40_factor_align.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

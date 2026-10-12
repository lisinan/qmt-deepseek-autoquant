# -*- coding: utf-8 -*-
"""PM-EVOLVE R27 (2026-10-12 收官) 第 13 次同型口径核查：
「live DailyContext.score」 vs 「backtest score_daily」是否逐位相同。

假设（来自 AM R26 §9-3 交接 + 今日实盘铁证）：
  live 的 DailyContext 只拉 DAILY_COUNT=120 根日线，而 I.vwap 是
  **从序列起点累计**的（core/indicators.py:191-202，"累计 VWAP（从序列起点累计到当前）"）。
  回测用全样本（901 根，起点 2023-01）⇒ 两侧 VWAP 值系统性不同
  ⇒ vwap 因子（0/1/2，权重最大 2.0）不同 ⇒ score 不同
  ⇒ 实盘的 buy_score_threshold=4.0 实际等价于回测的另一个阈值。

  方向预判：强势股（追高，R18/R20 已证为 alpha 源）在「全样本累计 VWAP」下
  close >> vwap ⇒ diff 大正 ⇒ vwap=2.0；在「120 根累计 VWAP」下 close≈近 120 日均价
  ⇒ diff 小 ⇒ vwap=1.0/0.0。**若成立，则实盘系统性拿不到这个 alpha。**

输出 JSON：reports/_score_align_20261012.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import STOCK_CODES, SECTOR_CONFIG          # noqa: E402
from strategy.backtest_daily import load_daily, score_daily_series  # noqa: E402
from strategy.daily_context import DailyContext, DAILY_COUNT    # noqa: E402

N = 901
TAIL = 120          # 与 live DAILY_COUNT 对齐


def universe() -> list:
    s = set(STOCK_CODES)
    for _k, v in SECTOR_CONFIG["sectors"].items():
        for code, _n in v["stocks"]:
            s.add(code)
    return sorted(s)


def main() -> int:
    codes = universe()
    data = {}
    for c in codes:
        d = load_daily(c, N)
        if d and len(d["close"]) >= 200:
            data[c] = d
    print(f"universe loaded: {len(data)}/{len(codes)}")

    # ---- 1) 末根对照（全样本口径 vs live 120 根口径）----
    last_diff = {}
    vwap_gap = {}
    for c, d in data.items():
        o, h, l, cl, v = d["open"], d["high"], d["low"], d["close"], d["volume"]
        s_full, f_full = score_daily_series(o, h, l, cl, v)[-1]
        tail = {k: list(val[-TAIL:]) for k, val in d.items()}
        lf = DailyContext._compute_for_test(c, tail)
        last_diff[c] = round(s_full - lf.score, 3)
        vwap_gap[c] = {
            "score_full": s_full,
            "score_live": lf.score,
            "vwap_full_f": f_full.get("vwap"),
            "vwap_live_f": lf.factors.get("vwap"),
            "factors_full": f_full,
            "factors_live": lf.factors,
        }

    nz = {c: v for c, v in last_diff.items() if abs(v) > 1e-9}
    print(f"\n=== 末根 score 差异：{len(nz)}/{len(data)} 只不一致 ===")
    for c, v in sorted(nz.items(), key=lambda x: -abs(x[1]))[:15]:
        g = vwap_gap[c]
        print(f"  {c}: full={g['score_full']:.2f} live={g['score_live']:.2f} "
              f"d={v:+.2f} | vwap {g['vwap_full_f']}->{g['vwap_live_f']}")

    # ---- 2) 近 250 根逐日对照（滚动 120 根窗口）----
    day_rows = []
    for c, d in data.items():
        o, h, l, cl, v = d["open"], d["high"], d["low"], d["close"], d["volume"]
        full_series = score_daily_series(o, h, l, cl, v)
        n = len(cl)
        for i in range(n - 250, n):
            if i < TAIL:
                continue
            tail = {k: list(val[i - TAIL + 1:i + 1]) for k, val in d.items()}
            lf = DailyContext._compute_for_test(c, tail)
            s_full = full_series[i][0]
            day_rows.append({
                "code": c, "i": i, "date": str(d.get("date", [None] * n)[i]),
                "s_full": s_full, "s_live": lf.score,
                "d": round(s_full - lf.score, 3),
                "f_full": full_series[i][1], "f_live": lf.factors,
            })
    tot = len(day_rows)
    nz_day = [r for r in day_rows if abs(r["d"]) > 1e-9]
    print(f"\n=== 近 250 根逐日：{len(nz_day)}/{tot} "
          f"({100.0*len(nz_day)/max(tot,1):.1f}%) 不一致 ===")

    # vwap 因子差异分布
    import collections
    cnt = collections.Counter()
    for r in nz_day:
        cnt[(r["f_full"].get("vwap"), r["f_live"].get("vwap"))] += 1
    print("  vwap 因子 (full -> live) 迁移计数：")
    for k, v in sorted(cnt.items(), key=lambda x: -x[1])[:10]:
        print(f"    {k[0]} -> {k[1]} : {v}")

    # ---- 3) 关键：回测判定 BUY(score>=4.0) 的日子，live 口径给多少分 ----
    buy_full = [r for r in day_rows if r["s_full"] >= 4.0]
    missed = [r for r in buy_full if r["s_live"] < 4.0]
    print(f"\n=== 回测口径 score>=4.0 的 (code,day) = {len(buy_full)}；"
          f"其中 live 口径 <4.0（实盘会漏买）= {len(missed)} "
          f"({100.0*len(missed)/max(len(buy_full),1):.1f}%) ===")
    for r in missed[:12]:
        print(f"  {r['code']} {r['date']}: full={r['s_full']:.2f} "
              f"live={r['s_live']:.2f} vwap {r['f_full'].get('vwap')}"
              f"->{r['f_live'].get('vwap')}")

    # 反向：live>=4.0 而 full<4.0
    extra = [r for r in day_rows if r["s_live"] >= 4.0 and r["s_full"] < 4.0]
    print(f"  反向（live 会买、回测不买）= {len(extra)}")

    # ---- 4) 今日 (末根) 对照：实盘日志「候选 1 只、最高分 688111.SH 3.58」----
    print("\n=== 末根（2026-10-09 收盘）全宇宙排序（full 口径 top10）===")
    last = {c: (vwap_gap[c]["score_full"], vwap_gap[c]["score_live"])
            for c in data}
    for c, (sf, sl) in sorted(last.items(), key=lambda x: -x[1][0])[:10]:
        print(f"  {c}: full={sf:.2f} live={sl:.2f}")

    out = {
        "n_codes": len(data),
        "last_diff": last_diff,
        "last_detail": vwap_gap,
        "day_rows_n": tot,
        "day_mismatch_n": len(nz_day),
        "vwap_migration": {f"{k[0]}->{k[1]}": v for k, v in cnt.items()},
        "buy_full_n": len(buy_full),
        "missed_by_live_n": len(missed),
        "extra_by_live_n": len(extra),
        "D_TAIL": TAIL,
    }
    p = ROOT / "reports" / "_score_align_20261012.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""【2026-10-21 用户提问】「今天为何没有开仓」—— 逐层量化诊断

与实盘引擎**同源同口径**复算：
  - 宇宙 = `list(STOCK_CODES) + list(INDEX_CODES)`（event_engine.py:290 构造
    DailyContext 的入参，即日志里「宇宙 38 只」的同一个集合）
  - 动量 = `DailyContext.momentum_60d`（实盘动量闸门的唯一读数函数）
  - 闸门 = `top_momentum(..., min_mom=0.15, lookback=60, top_n=3)`

只读、零副作用、不写任何配置。
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import STOCK_CODES, INDEX_CODES, STRATEGY_PARAMS  # noqa: E402
from strategy.daily_context import DailyContext  # noqa: E402

LOOKBACK = int(STRATEGY_PARAMS.get("momentum_lookback", 60) or 60)
MIN_MOM = float(STRATEGY_PARAMS.get("momentum_min_mom", 0.0) or 0.0)
TOP_N = int(STRATEGY_PARAMS.get("momentum_top_n", 3) or 3)
SCORE_TH = float(STRATEGY_PARAMS.get("buy_score_threshold", 4.0))
MIN_SIG = int(STRATEGY_PARAMS.get("min_signals", 3) or 3)
MIN_BIAS = float(STRATEGY_PARAMS.get("min_daily_bias", 2.0) or 0.0)


def main():
    from data.dynamic_universe import DynamicUniverse
    dyn = DynamicUniverse()
    try:
        dyn.refresh(force=True)
        active = list(dyn.active_codes)
    except Exception as e:
        print(f"[动态池] 刷新失败（忽略）: {e}")
        active = []
    codes = list(dict.fromkeys(
        list(STOCK_CODES) + list(INDEX_CODES) + active))
    print(f"[宇宙] {len(codes)} 只（STOCK_CODES {len(STOCK_CODES)} + "
          f"INDEX_CODES {len(INDEX_CODES)} + 动态活跃池 {len(active)}）")
    print(f"[闸门] momentum_min_mom={MIN_MOM}  lookback={LOOKBACK}  "
          f"top_n={TOP_N}  buy_score_threshold={SCORE_TH}  "
          f"min_signals={MIN_SIG}  min_daily_bias={MIN_BIAS}")

    ctx = DailyContext(codes=codes)
    n = ctx.refresh(codes=codes, force=True)
    print(f"[刷新] {n} 只")

    covered = sorted(ctx.covered_codes())
    print(f"[有日线数据] {len(covered)} 只")

    # ---- 逐只动量（实盘同源函数） ----
    rows = []
    for c in covered:
        m = ctx.momentum_60d(c, LOOKBACK)
        f = ctx.features(c)
        rows.append(dict(
            code=c,
            mom=m,
            trend_up=bool(getattr(f, "trend_up", False)) if f else None,
            bias=float(getattr(f, "bias", 0.0) or 0.0) if f else None,
            score=float(getattr(f, "score", 0.0) or 0.0) if f else None,
            last=ctx.last_close(c) if hasattr(ctx, "last_close") else None,
        ))
    rows.sort(key=lambda r: r["mom"], reverse=True)

    print(f"\n[60 日动量分布]（共 {len(rows)} 只，降序）")
    print(f"{'代码':<12}{'60日动量':>10}  {'过15%闸门':<8}{'trend_up':<9}"
          f"{'bias':>7}{'score':>7}")
    n_pass = 0
    for r in rows:
        ok = r["mom"] > MIN_MOM
        if ok:
            n_pass += 1
        mark = " ✓" if ok else ""
        tu = "True" if r["trend_up"] else "False"
        print(f"{r['code']:<12}{r['mom']*100:>+9.2f}%  {str(ok):<8}{tu:<9}"
              f"{(r['bias'] if r['bias'] is not None else float('nan')):>+7.2f}"
              f"{(r['score'] if r['score'] is not None else float('nan')):>7.2f}{mark}")

    # ---- 闸门模拟 ----
    gated = ctx.top_momentum(covered, top_n=TOP_N, lookback=LOOKBACK)
    print(f"\n[动量闸门输出] top_momentum(top_n={TOP_N}) -> {gated}")

    trend_up_n = sum(1 for r in rows if r["trend_up"])
    bias_ok_n = sum(1 for r in rows
                    if r["bias"] is not None and r["bias"] >= MIN_BIAS)
    score_ok_n = 0
    for r in rows:
        f = ctx.features(r["code"])
        fac = (getattr(f, "factors", None) or {}) if f else {}
        pos = sum(1 for v in fac.values() if v and v > 0)
        if r["score"] is not None and r["score"] >= SCORE_TH and pos >= MIN_SIG:
            score_ok_n += 1
    best = max(rows, key=lambda r: r["score"] or 0.0) if rows else None

    print("\n" + "=" * 78)
    print("漏斗分层（与 logs/quant_system.log 的『漏斗分层』字段同口径）")
    print("=" * 78)
    print(f"  宇宙            : {len(covered)} 只")
    print(f"  trend_up        : {trend_up_n} 只")
    print(f"  bias>={MIN_BIAS}    : {bias_ok_n} 只")
    print(f"  score>={SCORE_TH} 且正因子>={MIN_SIG}: {score_ok_n} 只"
          f"（最高分 {best['code'] if best else '-'} "
          f"{best['score'] if best else float('nan')}）")
    print(f"  60日动量 > {MIN_MOM*100:.0f}%  : **{n_pass} 只**")
    print(f"  ⇒ 动量闸门后候选池: {'空（今日无开仓）' if not gated else gated}")

    moms = [r["mom"] for r in rows]
    if moms:
        top = max(moms)
        med = sorted(moms)[len(moms) // 2]
        print(f"\n  ★ 60 日动量最高仅 {top*100:+.2f}%，中位数 {med*100:+.2f}%，"
              f"> {MIN_MOM*100:.0f}% 的有 {n_pass}/{len(moms)} 只")
        # 若把闸门降到「只有正动量」会怎样
        n_pos = sum(1 for m in moms if m > 0)
        print(f"  ★ 若闸门降到 min_mom=0（只要求正动量）: {n_pos} 只入池"
              f" → 可选出 {min(TOP_N, n_pos)} 只")

    out = dict(window="2026-10-21", n_universe=len(covered),
               min_mom=MIN_MOM, lookback=LOOKBACK, top_n=TOP_N,
               n_pass_mom=n_pass, n_trend_up=trend_up_n, n_bias_ok=bias_ok_n,
               n_score_ok=score_ok_n, gated=gated,
               rows=[{k: v for k, v in r.items()} for r in rows])
    with open("reports/_r41_why_no_entry_today.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print("\n[已写] reports/_r41_why_no_entry_today.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())

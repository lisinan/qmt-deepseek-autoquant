# -*- coding: utf-8 -*-
"""「为何没有开仓」—— 实盘同源复算（DailyContext 真实路径）。

目的：与 logs/quant_system.log 的「漏斗分层」逐数对齐。
    event_engine.py:293 构造 DailyContext(codes=STOCK_CODES+INDEX_CODES)
    refresh 时并入动态池；_entry_funnel_stats 用 self.daily.features(c)

本脚本**直接调用实盘类**，不重写任何公式，输出：
    - 每只标的的最后一根日线日期 + 收盘（判定数据源/日期是否与面板一致）
    - trend_up / bias / score / factors / momentum_60d
    - 漏斗分层合计（应与引擎日志同数）

只读、零副作用、不写任何生产文件。
"""
from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import (  # noqa: E402
    INDEX_CODES, STOCK_CODES, STRATEGY_PARAMS,
)
from strategy.daily_context import DailyContext  # noqa: E402

OUT = ROOT / "reports" / "why_no_entry"
OUT.mkdir(parents=True, exist_ok=True)
PANEL = ROOT / "storage" / "hist_cache" / "ts_panel_25_901_20211201_20261028.pkl"

SCORE_TH = float(STRATEGY_PARAMS.get("buy_score_threshold", 4.0))
MIN_SIG = int(STRATEGY_PARAMS.get("min_signals", 3))
MIN_BIAS = float(STRATEGY_PARAMS.get("min_daily_bias", 2.0))
MIN_MOM = float(STRATEGY_PARAMS.get("momentum_min_mom", 0.0))
LOOKBACK = int(STRATEGY_PARAMS.get("momentum_lookback", 60) or 60)
TOP_N = int(STRATEGY_PARAMS.get("momentum_top_n", 3) or 3)


def main() -> None:
    from data.dynamic_universe import DynamicUniverse
    dyn = DynamicUniverse()
    try:
        dyn.refresh(force=True)
        active = list(dyn.active_codes)
    except Exception as e:  # noqa: BLE001
        print(f"[动态池] 刷新失败（忽略）: {e}")
        active = []
    codes = list(dict.fromkeys(list(STOCK_CODES) + list(INDEX_CODES) + active))
    print(f"[宇宙] 传入 {len(codes)} 只（STOCK {len(STOCK_CODES)} + "
          f"INDEX {len(INDEX_CODES)} + 动态 {len(active)}）")

    ctx = DailyContext(codes=codes)
    n = ctx.refresh(codes=codes, force=True)
    covered = sorted(ctx.covered_codes())
    print(f"[刷新] {n} 只；[有日线] {len(covered)} 只")

    panel = pickle.loads(PANEL.read_bytes()) if PANEL.exists() else {}

    rows = []
    for c in covered:
        f = ctx.features(c)
        if f is None:
            continue
        with ctx._lock:                                  # noqa: SLF001
            closes = list(ctx._closes.get(c) or [])
        mom = ctx.momentum_60d(c, LOOKBACK)
        fac = (f.factors or {})
        pos = sum(1 for v in fac.values() if v and v > 0)
        pc = panel.get(c, {})
        pclose = pc.get("close", [None])[-1] if pc else None
        pdate = pc.get("date", [None])[-1] if pc else None
        rows.append({
            "code": c, "live_close": round(f.close, 3),
            "panel_close": pclose, "panel_date": pdate,
            "close_diff_pct": (round((f.close / pclose - 1) * 100, 3)
                               if pclose else None),
            "ma20": round(f.ma20, 3), "ma60": round(f.ma60, 3),
            "slope": round(f.ma20_slope, 4), "hist": round(f.macd_hist, 4),
            "rsi": (round(f.rsi, 2) if f.rsi is not None else None),
            "above_ma20": f.above_ma20, "above_ma60": f.above_ma60,
            "bias": round(f.bias, 3), "trend_up": bool(f.trend_up),
            "score": round(f.score, 2), "pos_factors": pos,
            "factors": {k: round(v, 2) for k, v in fac.items()},
            "mom60": (round(mom, 4) if mom is not None else None),
            "daily_ok": bool(f.trend_up or f.bias >= MIN_BIAS),
            "score_ok": bool(f.score >= SCORE_TH and pos >= MIN_SIG),
        })

    st = {
        "universe_n": len(rows),
        "trend_up_n": sum(1 for r in rows if r["trend_up"]),
        "bias_ok_n": sum(1 for r in rows if r["bias"] >= MIN_BIAS),
        "score_ok_n": sum(1 for r in rows if r["score_ok"]),
        "daily_ok_n": sum(1 for r in rows if r["daily_ok"]),
        "signal_n": sum(1 for r in rows if r["daily_ok"] and r["score_ok"]),
        "mom_pass_n": sum(1 for r in rows if r["daily_ok"] and r["score_ok"]
                          and (r["mom60"] or -9) >= MIN_MOM),
    }
    st["mom_gt_mom_pct_over_all"] = sum(
        1 for r in rows if (r["mom60"] or -9) >= MIN_MOM)

    print("\n" + "=" * 76)
    print("漏斗分层（实盘同源，应与 logs/quant_system.log 对齐）")
    print("=" * 76)
    print(f"  宇宙        : {st['universe_n']} 只")
    print(f"  trend_up    : {st['trend_up_n']} 只")
    print(f"  bias>={MIN_BIAS}  : {st['bias_ok_n']} 只")
    print(f"  score>={SCORE_TH}且正因子>={MIN_SIG}: {st['score_ok_n']} 只")
    print(f"  日线闸门通过: {st['daily_ok_n']} 只（trend_up 或 bias）")
    print(f"  信号候选    : {st['signal_n']} 只")
    print(f"  动量>{MIN_MOM:.2f} : {st['mom_pass_n']} 只")

    print("\n--- 收盘价比对（live vs tushare 面板末日）---")
    diff = [r for r in rows if r["close_diff_pct"] not in (None, 0.0)]
    print(f"  可比值 {len([r for r in rows if r['close_diff_pct'] is not None])} 只，"
          f"不一致 {len(diff)} 只")
    for r in diff[:12]:
        print(f"  {r['code']:<11} live {r['live_close']:>9.3f}  "
              f"panel {r['panel_close']:>9.3f}  差 {r['close_diff_pct']:+.3f}%")

    print("\n--- trend_up=True 明细 ---")
    for r in [x for x in rows if x["trend_up"]]:
        print(f"  {r['code']:<11} close {r['live_close']:>9.2f} "
              f"MA20 {r['ma20']:>9.2f} MA60 {r['ma60']:>9.2f} "
              f"slope {r['slope']:+.3f} hist {r['hist']:+.4f} "
              f"bias {r['bias']:+.2f} score {r['score']:.2f} "
              f"mom {(r['mom60'] or 0)*100:+.1f}%")

    best = max(rows, key=lambda r: r["score"]) if rows else None
    gated = ctx.top_momentum(covered, top_n=TOP_N, lookback=LOOKBACK)
    print(f"\n[动量闸门] top_momentum(top_n={TOP_N}) -> {gated}")
    if best:
        print(f"[最高分] {best['code']} {best['score']:.2f} "
              f"factor={best['factors']}")

    payload = {"generated_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
               "params": {"score_th": SCORE_TH, "min_sig": MIN_SIG,
                          "min_bias": MIN_BIAS, "min_mom": MIN_MOM,
                          "lookback": LOOKBACK, "top_n": TOP_N},
               "funnel_live": st, "gated": gated, "rows": rows}
    (OUT / "WHY_NO_ENTRY_LIVE.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[已写] {OUT / 'WHY_NO_ENTRY_LIVE.json'}")


if __name__ == "__main__":
    main()

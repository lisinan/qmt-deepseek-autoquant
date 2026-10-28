# -*- coding: utf-8 -*-
"""「为何没有开仓」核心量化：动量闸门缺口。

已确认（前三步）：
  - 日线闸门：bias>=2.0 通道按设计关闭（bias∈[-1,1]，2.0 是哨兵值，
    backtest_daily.py:496-502 明确注释「2.0 = 关闭」），实质只剩 trend_up
  - trend_up 通过数按日上行：10-19~10-23 = 0 → 10-26 = 2 → 10-27 = 4 → 10-28 = 10
    （引擎日志），即日线闸门正在打开，不是硬阻断
  - 评分闸门：每交易日 1~16 只通过（视数据日期），同样不是唯一卡点
  - **动量闸门：每交易日均为 0 只**（引擎日志「候选池为空」+ top_momentum() 返回 []）

本脚本量化这个唯一硬卡点：60 日动量分布 + 缺口 + 反事实阶梯。

只读、零副作用。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import STRATEGY_PARAMS  # noqa: E402
from core import indicators as I  # noqa: E402
from strategy.backtest_daily import score_daily_series  # noqa: E402

LIVE_JSON = ROOT / "reports" / "why_no_entry" / "WHY_NO_ENTRY_LIVE.json"
OUT = ROOT / "reports" / "why_no_entry"
SCORE_TH = float(STRATEGY_PARAMS.get("buy_score_threshold", 4.0))
MIN_SIG = int(STRATEGY_PARAMS.get("min_signals", 3))
MIN_MOM = float(STRATEGY_PARAMS.get("momentum_min_mom", 0.15))
MOM_N = int(STRATEGY_PARAMS.get("momentum_lookback", 60) or 60)
TOP_N = int(STRATEGY_PARAMS.get("momentum_top_n", 3) or 3)
TAIL = 120


def main() -> None:
    codes = [r["code"] for r in json.loads(
        LIVE_JSON.read_text(encoding="utf-8"))["rows"]]
    from core.qmt_client import qmt_client  # noqa: PLC0415
    if hasattr(qmt_client, "connect"):
        qmt_client.connect()

    payload = {"params": {"score_th": SCORE_TH, "min_sig": MIN_SIG,
                          "min_mom": MIN_MOM, "mom_n": MOM_N,
                          "top_n": TOP_N}, "dates": {}}
    for asof, drop in (("20261027", 1), ("20261028", 0)):
        rows = []
        for c in codes:
            raw = qmt_client.get_history(c, period="1d", count=200) or []
            if len(raw) < MOM_N + TAIL // 2:
                continue
            if drop:
                raw = raw[:-drop]
            cl = [b["close"] for b in raw]
            o = [b["open"] for b in raw]
            h = [b["high"] for b in raw]
            l = [b["low"] for b in raw]
            v = [b["volume"] for b in raw]
            lo = max(0, len(cl) - TAIL)
            cw = cl[lo:]
            ma20 = I.last(I.sma(cw, 20)) or 0.0
            ma60 = I.last(I.sma(cw, 60)) or 0.0
            _, _, hist = I.macd(cw, 12, 26, 9)
            hv = I.last(hist)
            sl = I.slope(I.sma(cw, 20), 5) or 0.0
            rsi = I.last(I.rsi(cw, 14))
            close = cl[-1]
            trend_up = bool(ma20 and ma60 and close > ma20 and close > ma60
                            and sl > 0 and hv is not None and hv >= 0)
            sc, fac = score_daily_series(o[lo:], h[lo:], l[lo:], cw, v[lo:])[-1]
            pos = sum(1 for x in fac.values() if x and x > 0)
            mom = (close / cl[-1 - MOM_N] - 1.0) if len(cl) > MOM_N else None
            rows.append({
                "code": c, "close": round(close, 2),
                "ma20": round(ma20, 2), "ma60": round(ma60, 2),
                "vs_ma20_pct": round((close / ma20 - 1) * 100, 2) if ma20 else None,
                "vs_ma60_pct": round((close / ma60 - 1) * 100, 2) if ma60 else None,
                "slope": round(sl, 4), "macd_hist": round(hv, 4) if hv else None,
                "rsi": round(rsi, 1) if rsi is not None else None,
                "trend_up": trend_up, "score": round(sc, 2),
                "pos_factors": pos, "factors": fac,
                "score_ok": bool(sc >= SCORE_TH and pos >= MIN_SIG),
                "mom60": round(mom, 4) if mom is not None else None,
                "need_pct_to_thr": (round((cl[-1 - MOM_N] * (1 + MIN_MOM)
                                           / close - 1) * 100, 2)
                                    if len(cl) > MOM_N else None),
            })
        rows.sort(key=lambda r: (r["mom60"] if r["mom60"] is not None
                                 else -9), reverse=True)
        mo = [r["mom60"] for r in rows if r["mom60"] is not None]
        mo_sorted = sorted(mo)
        n = len(mo_sorted)
        d = {
            "n": len(rows),
            "trend_up": sum(1 for r in rows if r["trend_up"]),
            "score_ok": sum(1 for r in rows if r["score_ok"]),
            "daily_ok": sum(1 for r in rows if r["trend_up"]),
            "mom_max": round(max(mo_sorted), 4),
            "mom_max_code": rows[0]["code"] if rows else None,
            "mom_median": round(mo_sorted[n // 2], 4) if n else None,
            "mom_mean": round(sum(mo_sorted) / n, 4) if n else None,
            "mom_pos_n": sum(1 for m in mo_sorted if m > 0),
            "mom_pass_n": sum(1 for m in mo_sorted if m >= MIN_MOM),
            "mom_top3": [(r["code"], r["mom60"]) for r in rows[:3]],
            "counterfactual": [
                {"thr": t, "pass": sum(1 for m in mo_sorted if m >= t)}
                for t in (0.15, 0.12, 0.10, 0.08, 0.05, 0.03, 0.0)],
            "candidates_if_thr_0": [r["code"] for r in rows
                                    if r["trend_up"] or r["score_ok"]][:8],
            "rows": rows,
        }
        payload["dates"][asof] = d
        print(f"\n===== as_of {asof} =====")
        print(f"  N={d['n']}  trend_up={d['trend_up']}  score_ok={d['score_ok']}"
              f"  动量通过={d['mom_pass_n']}")
        print(f"  60日动量: 最大 {d['mom_max']*100:+.2f}% ({d['mom_max_code']})"
              f"  中位 {d['mom_median']*100:+.2f}%  均值 {d['mom_mean']*100:+.2f}%"
              f"  为正 {d['mom_pos_n']}/{d['n']}")
        print("  top3: " + ", ".join(f"{c} {m*100:+.1f}%" for c, m in d["mom_top3"]))
        print("  反事实: " + "  ".join(
            f"{c['thr']:.2f}→{c['pass']}只" for c in d["counterfactual"]))
        top = rows[0]
        print(f"  距 15% 闸门: 最强 {top['code']} 还差 "
              f"{top['need_pct_to_thr']:+.1f}%" if top["need_pct_to_thr"] is not None
              else "")

    (OUT / "WHY_NO_ENTRY_MOMENTUM.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[已写] {OUT / 'WHY_NO_ENTRY_MOMENTUM.json'}")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""「为何没有开仓」逐闸门归因（2026-10-28）。

复算引擎真实门链，与 quant_system.log 的「漏斗分层」逐数对齐：
    宇宙 N 只 → trend_up T → bias>=2.0 B → score>=4.0 且正因子>=3 S
    → (daily_ok = trend_up or bias>=2.0) 与 S 取交集 → 动量>=0.15 → 最终候选

输出:
    reports/why_no_entry/WHY_NO_ENTRY_2026-10-28.json   机器可读
    reports/why_no_entry/WHY_NO_ENTRY_2026-10-28.md     人读表

纪律：只读面板/只读 settings，不写任何生产文件。
"""
from __future__ import annotations

import json
import pickle
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import (  # noqa: E402
    SECTOR_CONFIG, STOCK_CODES, STRATEGY_PARAMS,
)
from strategy.backtest_daily import (  # noqa: E402
    daily_trend_up, score_daily_series,
)
from core import indicators as I  # noqa: E402

PANEL = ROOT / "storage" / "hist_cache" / "ts_panel_25_901_20211201_20261028.pkl"
OUT = ROOT / "reports" / "why_no_entry"
OUT.mkdir(parents=True, exist_ok=True)

AS_OF = "20261028"
LOOKBACK_DAYS = 22          # 漏斗时间序列回看交易日数
MOM_N = 60
MIN_MOM = float(STRATEGY_PARAMS.get("momentum_min_mom", 0.15))
SCORE_TH = float(STRATEGY_PARAMS.get("buy_score_threshold", 4.0))
MIN_SIG = int(STRATEGY_PARAMS.get("min_signals", 3))
MIN_BIAS = float(STRATEGY_PARAMS.get("min_daily_bias", 2.0))


def load_panel() -> dict:
    return pickle.loads(PANEL.read_bytes())


def fetch_dynamic(panel: dict) -> tuple:
    """补齐动态候选池日线（tushare）。返回 (panel, 新增代码数, 失败列表)。"""
    du_path = ROOT / "storage" / "dynamic_universe.json"
    dyn = []
    if du_path.exists():
        dyn = json.loads(du_path.read_text(encoding="utf-8")).get("active_pool", [])
    missing = [c for c in dyn if c not in panel]
    if not missing:
        return panel, 0, []
    import tushare as ts
    from data.tushare_client import tushare_client as T
    ts.set_token(T.token)
    pro = ts.pro_api(timeout=30)
    added, failed = 0, []
    for c in missing:
        try:
            df = pro.daily(ts_code=c, start_date="20211201", end_date=AS_OF)
            if df is None or not len(df):
                failed.append(c)
                continue
            df = df.sort_values("trade_date")
            panel[c] = {
                "date": [str(x) for x in df["trade_date"]],
                "open": [float(x) for x in df["open"]],
                "high": [float(x) for x in df["high"]],
                "low": [float(x) for x in df["low"]],
                "close": [float(x) for x in df["close"]],
                "volume": [float(x) * 100.0 for x in df["vol"]],
            }
            added += 1
        except Exception as e:  # noqa: BLE001
            failed.append(f"{c}:{e}")
        time.sleep(0.15)
    return panel, added, failed


def universe() -> list:
    s = set(STOCK_CODES)
    for _k, v in SECTOR_CONFIG["sectors"].items():
        for c, _n in v["stocks"]:
            s.add(c)
    du_path = ROOT / "storage" / "dynamic_universe.json"
    if du_path.exists():
        s |= set(json.loads(du_path.read_text(encoding="utf-8")).get("active_pool", []))
    return sorted(c for c in s if c.endswith((".SH", ".SZ")))


def bias_of(closes: list) -> float:
    ma20 = I.last(I.sma(closes, 20)) or 0.0
    ma60 = I.last(I.sma(closes, 60)) or 0.0
    close = closes[-1]
    b = 0.0
    if ma20:
        b += 0.35 * (1 if close > ma20 else -1)
    if ma60:
        b += 0.35 * (1 if close > ma60 else -1)
    if ma20 and ma60:
        b += 0.30 * (1 if ma20 > ma60 else -1)
    return max(-1.0, min(1.0, b))


def main() -> None:
    t0 = time.time()
    panel = load_panel()
    panel, added, failed = fetch_dynamic(panel)
    codes = [c for c in universe() if c in panel]

    # ---------- 逐 code 预算序列 ----------
    per: dict = {}
    for c in codes:
        d = panel[c]
        o, h, l, cl, v = d["open"], d["high"], d["low"], d["close"], d["volume"]
        n = len(cl)
        if n < 80:
            continue
        scores = score_daily_series(o, h, l, cl, v)
        ma20s, ma60s = I.sma(cl, 20), I.sma(cl, 60)
        _, _, hist = I.macd(cl, 12, 26, 9)
        rsi14 = I.rsi(cl, 14)
        per[c] = {
            "date": d["date"], "close": cl, "ma20": ma20s, "ma60": ma60s,
            "hist": hist, "rsi": rsi14, "scores": scores,
            "trend": [daily_trend_up(cl, h, l, i) for i in range(n)],
        }
    # ---------- 交易日轴（取面板最长序列）----------
    axis = max((per[c]["date"] for c in per), key=len)

    def idx_of(c: str, day: str):
        try:
            return per[c]["date"].index(day)
        except ValueError:
            return None

    def day_metrics(c: str, day: str) -> dict | None:
        p = per[c]
        i = idx_of(c, day)
        if i is None or i < MOM_N + 1:
            return None
        cl = p["close"]
        score, factors = p["scores"][i]
        pos = sum(1 for x in factors.values() if x and x > 0)
        mom = cl[i] / cl[i - MOM_N] - 1.0 if cl[i - MOM_N] else None
        ma20, ma60 = p["ma20"][i], p["ma60"][i]
        slope_ok = False
        if ma20 and i >= 5 and p["ma20"][i - 5]:
            slope_ok = ma20 > p["ma20"][i - 5]
        return {
            "code": c, "close": cl[i], "ma20": ma20, "ma60": ma60,
            "above_ma20": bool(ma20 and cl[i] > ma20),
            "above_ma60": bool(ma60 and cl[i] > ma60),
            "slope_ok": slope_ok,
            "hist_ok": bool(p["hist"][i] is not None and p["hist"][i] >= 0),
            "hist": p["hist"][i], "rsi": p["rsi"][i],
            "bias": bias_of(cl[: i + 1]),
            "trend_up": bool(p["trend"][i]),
            # ★ 实盘口径（daily_context.py:333）= close>MA20 & close>MA60 & slope>0 & hist>=0
            # ★ 回测口径（backtest_daily.daily_trend_up）= close>MA20 & MA20>MA60 & slope>0 & hist>=0
            "trend_live": bool(
                ma20 and ma60 and cl[i] > ma20 and cl[i] > ma60
                and slope_ok and (p["hist"][i] is not None and p["hist"][i] >= 0)),
            "trend_bt_ma": bool(
                ma20 and ma60 and cl[i] > ma20 and ma20 > ma60
                and slope_ok and (p["hist"][i] is not None and p["hist"][i] >= 0)),
            "score": score, "pos_factors": pos,
            "mom60": mom,
        }

    # ---------- 漏斗时间序列 ----------
    recent = [d for d in axis[-LOOKBACK_DAYS:]]
    funnel = []
    for day in recent:
        rows = [m for c in per if (m := day_metrics(c, day))]
        if not rows:
            continue
        t = sum(1 for r in rows if r["trend_up"])
        t_live = sum(1 for r in rows if r["trend_live"])
        b = sum(1 for r in rows if r["bias"] >= MIN_BIAS)
        s = sum(1 for r in rows if r["score"] >= SCORE_TH
                and r["pos_factors"] >= MIN_SIG)
        daily_ok = [r for r in rows if r["trend_live"] or r["bias"] >= MIN_BIAS]
        sig = [r for r in daily_ok
               if r["score"] >= SCORE_TH and r["pos_factors"] >= MIN_SIG]
        mom_pass = [r for r in sig if r["mom60"] is not None
                    and r["mom60"] >= MIN_MOM]
        funnel.append({
            "date": day, "universe": len(rows), "trend_up": t,
            "trend_up_live": t_live, "bias": b,
            "score_pass": s, "daily_ok": len(daily_ok), "signal": len(sig),
            "mom_pass": len(mom_pass),
            "top_score": max((r["score"] for r in rows), default=0.0),
            "max_mom_all": max((r["mom60"] for r in rows
                                if r["mom60"] is not None), default=None),
            "sig_max_mom": max((r["mom60"] for r in sig
                                if r["mom60"] is not None), default=None),
        })

    # ---------- 今日逐股归因 ----------
    today = funnel[-1]["date"] if funnel else AS_OF
    trows = [m for c in per if (m := day_metrics(c, today))]
    for r in trows:
        fails = []
        if not r["above_ma20"]:
            fails.append("close<=MA20")
        if not r["above_ma60"]:
            fails.append("close<=MA60")
        if not r["slope_ok"]:
            fails.append("MA20斜率<=0")
        if not r["hist_ok"]:
            fails.append("MACD柱<0")
        r["trend_fails"] = fails
        if not r["trend_live"] and r["bias"] < MIN_BIAS:
            if fails:
                r["block"] = "日线闸门(" + "/".join(fails) + ")"
            else:
                r["block"] = "日线闸门"
        elif r["score"] < SCORE_TH or r["pos_factors"] < MIN_SIG:
            r["block"] = (f"评分闸门(score={r['score']:.2f}<{SCORE_TH}"
                          f" 或 正因子={r['pos_factors']}<{MIN_SIG})")
        elif r["mom60"] is None or r["mom60"] < MIN_MOM:
            r["block"] = f"动量闸门(mom={100*(r['mom60'] or 0):.1f}%<{100*MIN_MOM:.0f}%)"
        else:
            r["block"] = "全部通过"
        # 达到双闸门所需涨幅（近似）
        need = 0.0
        if r["ma20"] and r["close"] < r["ma20"]:
            need = max(need, r["ma20"] / r["close"] - 1)
        if r["ma60"] and r["close"] < r["ma60"]:
            need = max(need, r["ma60"] / r["close"] - 1)
        i = idx_of(r["code"], today)
        cl = per[r["code"]]["close"]
        if i is not None and i >= MOM_N and cl[i - MOM_N]:
            target = cl[i - MOM_N] * (1 + MIN_MOM)
            need = max(need, target / cl[i] - 1)
        r["need_pct"] = need * 100
        # 只差动量（已过日线+评分）
        r["only_mom_left"] = (r["trend_live"] or r["bias"] >= MIN_BIAS) \
            and r["score"] >= SCORE_TH and r["pos_factors"] >= MIN_SIG
    trows.sort(key=lambda r: r["block"])

    # ---------- trend_up 四分量失败计数 ----------
    comp = {
        "close<=MA20": sum(1 for r in trows if not r["above_ma20"]),
        "close<=MA60": sum(1 for r in trows if not r["above_ma60"]),
        "MA20<=MA60": sum(1 for r in trows
                          if not (r["ma20"] and r["ma60"] and r["ma20"] > r["ma60"])),
        "MA20斜率<=0": sum(1 for r in trows if not r["slope_ok"]),
        "MACD柱<0": sum(1 for r in trows if not r["hist_ok"]),
        "实盘trend_up通过(close>MA60口径)": sum(1 for r in trows if r["trend_live"]),
        "回测trend_up通过(MA20>MA60口径)": sum(1 for r in trows if r["trend_bt_ma"]),
    }

    # ---------- 反事实阶梯（研究口径，不改生产）----------
    counter = []
    for thr in (0.15, 0.10, 0.05, 0.0):
        n = sum(1 for r in trows if r["only_mom_left"]
                and (r["mom60"] or -9) >= thr)
        counter.append({"mom_threshold": thr, "eligible": n})
    counter.append({
        "note": "若只保留实盘 trend_up 而要求动量>0（放宽到 0）",
        "eligible": sum(1 for r in trows if r["trend_live"]
                        and (r["mom60"] or -9) >= 0),
    })

    payload = {
        "as_of": today,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "params": {"score_threshold": SCORE_TH, "min_signals": MIN_SIG,
                   "min_daily_bias": MIN_BIAS, "min_mom": MIN_MOM,
                   "mom_lookback": MOM_N},
        "universe": {"n": len(trows), "dynamic_added": added,
                     "fetch_failed": failed},
        "funnel": funnel,
        "today_rows": trows,
        "trend_components": comp,
        "counterfactual": counter,
        "elapsed_sec": round(time.time() - t0, 2),
    }
    (OUT / "WHY_NO_ENTRY_2026-10-28.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---------- markdown ----------
    L = []
    L.append(f"# 为何没有开仓 · 逐闸门归因（as_of {today}）\n")
    L.append(f"- 宇宙: {len(trows)} 只（静态 23 + 动态候选池补 {added} 只）"
             f"｜参数: score>={SCORE_TH}, 正因子>={MIN_SIG}, "
             f"min_mom={MIN_MOM}, lookback={MOM_N}\n")
    L.append("\n## 一、漏斗时间序列（近 %d 个交易日）\n\n" % len(funnel))
    L.append("> 实盘口径 trend_up = close>MA20 且 close>MA60 且 MA20 斜率>0 且 MACD 柱>=0"
             "（daily_context.py:333）；回测口径 = close>MA20 且 MA20>MA60 且 斜率>0 且 柱>=0"
             "（backtest_daily.daily_trend_up）——两者并非同义，见第四节。\n\n")
    L.append("| 日期 | 宇宙 | trend_up(实盘) | trend_up(回测) | bias≥2 | 评分通过 | 日线通过 | 信号候选 | 动量通过 | 最高分 | 全池最大动量 | 信号池最大动量 |\n")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n")
    for f in funnel:
        mm = f["max_mom_all"]
        sm = f["sig_max_mom"]
        L.append(f"| {f['date']} | {f['universe']} | {f['trend_up_live']} | "
                 f"{f['trend_up']} | {f['bias']} | {f['score_pass']} | "
                 f"{f['daily_ok']} | {f['signal']} | **{f['mom_pass']}** | "
                 f"{f['top_score']:.2f} | "
                 f"{'—' if mm is None else f'{100*mm:.1f}%'} | "
                 f"{'—' if sm is None else f'{100*sm:.1f}%'} |\n")
    L.append("\n## 二、今日 trend_up 四分量失败计数（N=%d）\n\n" % len(trows))
    for k, v in comp.items():
        L.append(f"- {k}: **{v}**\n")
    L.append("\n## 三、反事实阶梯（研究口径，不改生产）\n\n")
    for c in counter:
        if "note" in c:
            L.append(f"- {c['note']}: {c['eligible']} 只\n")
        else:
            L.append(f"- 动量阈值 {c['mom_threshold']:.2f}: "
                     f"{c['eligible']} 只可入场\n")
    L.append("\n## 四、今日逐股归因（按卡点分组）\n\n")
    L.append("| 代码 | 收盘 | vsMA20 | vsMA60 | 斜率 | MACD柱 | RSI | bias | score | 正因子 | 60日动量 | 卡在 | 需涨 |\n")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|\n")
    for r in trows:
        vs20 = (r["close"] / r["ma20"] - 1) * 100 if r["ma20"] else 0
        vs60 = (r["close"] / r["ma60"] - 1) * 100 if r["ma60"] else 0
        hist_s = "—" if r["hist"] is None else format(r["hist"], ".3f")
        rsi_s = "—" if r["rsi"] is None else format(r["rsi"], ".1f")
        mom_s = "—" if r["mom60"] is None else format(100 * r["mom60"], "+.1f") + "%"
        L.append(
            f"| {r['code']} | {r['close']:.2f} | {vs20:+.1f}% | {vs60:+.1f}% | "
            f"{'✓' if r['slope_ok'] else '✗'} | {hist_s} | {rsi_s} | "
            f"{r['bias']:+.2f} | {r['score']:.2f} | {r['pos_factors']} | "
            f"{mom_s} | {r['block']} | {r['need_pct']:+.1f}% |\n")
    (OUT / "WHY_NO_ENTRY_2026-10-28.md").write_text("".join(L), encoding="utf-8")

    print("=== 漏斗时间序列（末日）===")
    for f in funnel[-6:]:
        print(f"{f['date']} 宇宙{f['universe']} trend_up实盘{f['trend_up_live']}"
              f"/回测{f['trend_up']} bias{f['bias']} score{f['score_pass']} "
              f"daily_ok{f['daily_ok']} sig{f['signal']} mom{f['mom_pass']}")
    print("\n=== trend_up 分量失败 ===")
    print(comp)
    print("\n=== 反事实 ===")
    for c in counter:
        print(c)
    print(f"\n=== 今日 N={len(trows)}；只差动量的: "
          f"{sum(1 for r in trows if r['only_mom_left'])}")
    print(f"=== 耗时 {payload['elapsed_sec']}s；输出 {OUT}")


if __name__ == "__main__":
    main()

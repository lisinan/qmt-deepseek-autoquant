# -*- coding: utf-8 -*-
"""R43（2026-10-22）—— 弱市→反弹「捕获率」诊断：深跌后策略会错过第一波吗？

【问题】
    2026-10-22 实测：宇宙 38 只 60 日动量中位 −27.25%、trend_up 通过 0 只、
    账户 100% 现金已 13 个交易日。入场闸门 = close>MA20>MA60 且 MA20 斜率>0
    且 MACD 柱>=0（trend_up）且 60 日动量>15%。
    ⇒ 一旦 V 型反弹，这套闸门会滞后多久？错过的幅度有多大？

【方法（全部确定性，无联网补拉）】
    数据源：storage/hist_cache/ts_panel_pool62_20211201_20261013.pkl（tushare 面板）
            + strategy/_evolve_wf.py 的 aligned_universe 对齐逻辑（按日期对齐，
            停牌前向填充）。**不调用 load_daily** ⇒ 不会触发 xtdata 联网补拉。
    宇宙：config/settings.py 的 wide_universe()（23 只生产宇宙，面板全覆盖）。
    深跌段识别：等权宇宙指数（逐日等权再平衡收益累乘）自局部高点的回撤 ≤ −15%
            的全部片段；同一段只取最深的一天为 trough（回填至 −15% 阈值以下、
            回到前高之前为一段）。
    观察窗：trough 后 20 / 40 个交易日。
      · 宇宙收益 = 观察窗内等权买入持有（trough 收盘等权买入 → 窗口末收盘卖出）。
      · 策略收益 = run_backtest(base_cfg()) 在「trough 前 130 根预热 + 观察窗」
        的切片上跑一次，取其 equity_curve 推导（等价 total_return；20/10 日窗口
        从同一条曲线截取，**每段只跑 1 次回测**）。
      · 首次建仓日 = equity_curve 首次不等于初始权益 1,000,000 的那一根
        （方法与局限见报告 §方法）。
      · 捕获率 = 策略收益 / 宇宙收益（宇宙收益 ≤ +2% 或为负 ⇒ 跳过并说明）。
    随机性：本脚本无随机采样；仍固定 random.seed(0) 以便将来扩展复现。

【产出】
    reports/REBOUND_CAPTURE_2026-10-22.md
    reports/_res_rebound_capture.json

用法：
    export PATH=/c/Users/lisinan/.conda/envs/qmt/Library/bin:/c/Users/lisinan/.conda/envs/qmt/DLLs:$PATH
    export PYTHONPATH=.
    python scripts/_res_rebound_capture.py
"""
from __future__ import annotations

import json
import os
import pickle
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

random.seed(0)

from core import indicators as I  # noqa: E402
from strategy._evolve_wf import (  # noqa: E402
    aligned_universe, base_cfg, wide_universe,
)
from strategy.backtest_daily import run_backtest  # noqa: E402

PANEL_FILE = ROOT / "storage" / "hist_cache" / "ts_panel_pool62_20211201_20261013.pkl"
REPORT_MD = ROOT / "reports" / "REBOUND_CAPTURE_2026-10-22.md"
REPORT_JSON = ROOT / "reports" / "_res_rebound_capture.json"

WARMUP = 130            # base_cfg().min_warmup
DD_THRESHOLD = -0.15    # 深跌段阈值：自局部高点回撤 ≥ 15%
WINDOWS = (20, 40)      # 观察窗（交易日）
MAX_SEGMENTS = 8        # 预算：最多回测 8 段
MIN_UNIV_RET = 0.02     # 宇宙收益 ≤ 2% ⇒ 捕获率无意义，跳过
EDGE_ONLY = True        # True = 闸门只放宽 MA60 条件（a & c & d）


# ---------------------------------------------------------------- 数据装载
def load_panel(codes: list) -> tuple:
    """从 tushare 面板缓存装载并对齐（不触网）。"""
    raw = pickle.loads(PANEL_FILE.read_bytes())
    data = {}
    for c in codes:
        d = raw.get(c)
        if not d:
            continue
        n = len(d["close"])
        data[c] = {k: list(v[-n:]) for k, v in d.items()}
    dates, panel = aligned_universe(data, list(data.keys()))
    for code in panel:
        panel[code]["date"] = list(dates)
    return dates, panel


# ---------------------------------------------------------------- 指标
def uw_index(dates, panel) -> list:
    """等权宇宙指数：逐日「各票当日收益等权平均」累乘（每日再平衡等权）。"""
    n = len(dates)
    idx = [1.0] * n
    for i in range(1, n):
        rs = []
        for d in panel.values():
            c0, c1 = d["close"][i - 1], d["close"][i]
            if c0 and c1 and c0 > 0:
                rs.append(c1 / c0 - 1.0)
        if rs:
            idx[i] = idx[i - 1] * (1.0 + sum(rs) / len(rs))
        else:
            idx[i] = idx[i - 1]
    return idx


def trend_up_series(dates, panel) -> tuple:
    """逐票 trend_up 布尔序列 + 逐日 breadth（通过票数 / 有效票数）。"""
    per_code = {}
    for code, d in panel.items():
        cl = d["close"]
        s20 = I.sma(cl, 20)
        s60 = I.sma(cl, 60)
        sl20 = [None] * len(cl)
        for i in range(len(cl)):
            if i >= 24 and s20[i] is not None and s20[i - 5] is not None:
                sl20[i] = s20[i] - s20[i - 5]
        _, _, hist = I.macd(cl, 12, 26, 9)
        tu = [False] * len(cl)
        for i in range(len(cl)):
            if i < 60:
                continue
            a = bool(s20[i] and cl[i] > s20[i])
            b = bool(s20[i] and s60[i] and s20[i] > s60[i])
            c = bool(sl20[i] is not None and sl20[i] > 0)
            dd = bool((hist[i] or 0) >= 0)
            tu[i] = a and b and c and dd
        per_code[code] = tu
    n = len(dates)
    breadth = [0.0] * n
    breadth_cnt = [0] * n
    valid_cnt = [0] * n
    for i in range(n):
        c = v = 0
        for code, d in panel.items():
            if not d["valid"][i]:
                continue
            v += 1
            if per_code[code][i]:
                c += 1
        valid_cnt[i] = v
        breadth_cnt[i] = c
        breadth[i] = (c / v) if v else 0.0
    return per_code, breadth, breadth_cnt, valid_cnt


def find_drawdown_segments(idx: list, threshold: float = DD_THRESHOLD) -> list:
    """深跌段：自局部高点回撤 ≤ threshold 的连续片段（同一段只取最深的一天）。"""
    segs = []
    n = len(idx)
    peak_i, peak_v = 0, idx[0]
    trough_i, trough_v = None, None
    in_ep = False
    for i in range(1, n):
        if idx[i] > peak_v:
            if in_ep and trough_i is not None:
                segs.append({"peak_i": peak_i, "trough_i": trough_i,
                             "trough_val": trough_v, "peak_val": peak_v,
                             "recover_i": i})
            in_ep = False
            trough_i, trough_v = None, None
            peak_i, peak_v = i, idx[i]
            continue
        dd = idx[i] / peak_v - 1.0
        if dd <= threshold:
            in_ep = True
            if trough_v is None or idx[i] < trough_v:
                trough_v, trough_i = idx[i], i
    if in_ep and trough_i is not None:
        segs.append({"peak_i": peak_i, "trough_i": trough_i,
                     "trough_val": trough_v, "peak_val": peak_v,
                     "recover_i": None})
    return segs


# ---------------------------------------------------------------- 收益计算
def universe_bh(panel, i0: int, i1: int) -> tuple:
    """等权买入持有：i0 收盘等权买入 → i1 收盘卖出（等权算术平均）。"""
    rets = []
    for d in panel.values():
        if not (d["valid"][i0] and d["valid"][i1]):
            continue
        c0, c1 = d["close"][i0], d["close"][i1]
        if c0 and c1 and c0 > 0:
            rets.append(c1 / c0 - 1.0)
    if not rets:
        return 0.0, 0
    return sum(rets) / len(rets), len(rets)


def slice_panel(panel, lo: int, hi: int) -> dict:
    out = {}
    for code, d in panel.items():
        out[code] = {
            "date": list(d["date"][lo:hi + 1]),
            "open": list(d["open"][lo:hi + 1]),
            "high": list(d["high"][lo:hi + 1]),
            "low": list(d["low"][lo:hi + 1]),
            "close": list(d["close"][lo:hi + 1]),
            "volume": list(d["volume"][lo:hi + 1]),
            "valid": list(d["valid"][lo:hi + 1]),
        }
    return out


def run_segment(codes, panel, trough_i: int, win: int, cfg) -> dict:
    """在「trough 前 130 根预热 + 观察窗」切片上跑一次回测。"""
    lo, hi = trough_i - WARMUP, trough_i + win
    if lo < 0 or hi >= len(panel[codes[0]]["close"]):
        return {"error": "slice out of range"}
    sub = slice_panel(panel, lo, hi)
    res = run_backtest(codes, cfg, count=hi - lo + 1, preloaded=sub)
    if "error" in res:
        return {"error": res["error"]}
    eq = res["equity_curve"]
    # equity_curve: index0 = 循环前初始权益(1e6)；index k>=1 对应切片 bar 129+k。
    # trough 位于切片 bar 130 ⇒ k=1。窗口 w 日末 ⇒ k = 259+w。
    def ret_w(w: int) -> float:
        k = 259 + w
        if k >= len(eq) or eq[1] <= 0:
            return 0.0
        return eq[k] / eq[1] - 1.0
    first_k = None
    for k in range(1, len(eq)):
        if abs(eq[k] - eq[0]) > 1e-6:
            first_k = k
            break
    lag = None if first_k is None else (first_k - 1)
    return {
        "total_return": res["total_return"],
        "ret10": ret_w(10),
        "ret20": ret_w(20),
        "ret40": ret_w(40),
        "n_trades": res["n_trades"],
        "exposure": res["exposure"],
        "sharpe": res["sharpe"],
        "max_drawdown": res["max_drawdown"],
        "first_entry_lag": lag,
        "first_entry_k": first_k,
        "n_bars": res["n_bars"],
        "start_date": res["start_date"],
        "end_date": res["end_date"],
    }


# ---------------------------------------------------------------- 主流程
def main():
    codes = wide_universe()
    dates, panel = load_panel(codes)
    n = len(dates)
    print(f"[数据] {len(panel)} 只 × {n} 根  {dates[0]} → {dates[-1]}（tushare 面板，无联网）")

    idx = uw_index(dates, panel)
    print("[计算] trend_up / breadth …")
    _, breadth, breadth_cnt, valid_cnt = trend_up_series(dates, panel)

    segs = find_drawdown_segments(idx)
    print(f"[深跌段] 回撤 ≥15% 的片段：{len(segs)} 段")

    # 合格段：trough 前后都要有足够数据（预热 130 + 观察窗 40）
    eligible = [s for s in segs
                if s["trough_i"] >= WARMUP + 5
                and s["trough_i"] + 40 <= n - 1]
    print(f"[合格段] {len(eligible)} 段（预热/前视充足）")
    # 取最近 MAX_SEGMENTS 段（与当前市场最相关）
    eligible.sort(key=lambda s: s["trough_i"])
    chosen = eligible[-MAX_SEGMENTS:]
    if len(chosen) < MAX_SEGMENTS:
        extra = [s for s in segs if s not in eligible]
        chosen = chosen + extra[:MAX_SEGMENTS - len(chosen)]

    cfg = base_cfg()
    results = []
    for s in chosen:
        ti = s["trough_i"]
        row = {
            "peak_date": dates[s["peak_i"]],
            "trough_date": dates[ti],
            "trough_date_fmt": f"{dates[ti][:4]}-{dates[ti][4:6]}-{dates[ti][6:8]}",
            "peak_to_trough_dd_pct": round((s["trough_val"] / s["peak_val"] - 1) * 100, 2),
            "breadth_at_trough_pct": round(breadth[ti] * 100, 1),
            "breadth_cnt_at_trough": breadth_cnt[ti],
            "valid_cnt": valid_cnt[ti],
            "uw_ret_10d_pct": None, "uw_ret_20d_pct": None, "uw_ret_40d_pct": None,
            "universe_n": 0,
            "strat_ret_10d_pct": None, "strat_ret_20d_pct": None, "strat_ret_40d_pct": None,
            "capture_20d": None, "capture_40d": None,
            "first_entry_lag": None, "n_trades": None, "exposure": None,
            "total_return_40": None,
        }
        for w in WINDOWS:
            r, k = universe_bh(panel, ti, ti + w)
            row[f"uw_ret_{w}d_pct"] = round(r * 100, 2)
            if w == 20:
                row["universe_n"] = k
        r10, _ = universe_bh(panel, ti, ti + 10)
        row["uw_ret_10d_pct"] = round(r10 * 100, 2)

        bt = run_segment(codes, panel, ti, 40, cfg)
        if "error" in bt:
            row["error"] = bt["error"]
            results.append(row)
            continue
        row["strat_ret_10d_pct"] = round(bt["ret10"] * 100, 2)
        row["strat_ret_20d_pct"] = round(bt["ret20"] * 100, 2)
        row["strat_ret_40d_pct"] = round(bt["ret40"] * 100, 2)
        row["total_return_40"] = round(bt["total_return"] * 100, 2)
        row["n_trades"] = bt["n_trades"]
        row["exposure"] = round(bt["exposure"] * 100, 1)
        row["first_entry_lag"] = bt["first_entry_lag"]
        row["_bt"] = bt
        for w in (20, 40):
            u = row[f"uw_ret_{w}d_pct"]
            s_ = row[f"strat_ret_{w}d_pct"]
            if u is not None and u > MIN_UNIV_RET * 100:
                row[f"capture_{w}d"] = round(s_ / u, 3)
        # breadth 恢复情况（快通道判据的观测量）
        rec = None
        for k in range(ti + 1, min(ti + 11, n)):
            if breadth[k] > 0.25:
                rec = k - ti
                break
        row["breadth_gt25_after_trough_days"] = rec
        if rec is not None:
            rb, _ = universe_bh(panel, ti + rec, min(ti + rec + 10, n - 1))
            row["uw_ret_10d_from_breadth25_pct"] = round(rb * 100, 2)
        results.append(row)
        print(f"  {row['trough_date_fmt']} dd={row['peak_to_trough_dd_pct']:>6}%  "
              f"uw20={row['uw_ret_20d_pct']:>7}% str20={row['strat_ret_20d_pct']:>7}%  "
              f"uw40={row['uw_ret_40d_pct']:>7}% str40={row['strat_ret_40d_pct']:>7}%  "
              f"lag={row['first_entry_lag']}  trades={row['n_trades']}")

    # ---- 汇总 ----
    def med(vals):
        v = sorted(x for x in vals if x is not None)
        if not v:
            return None
        m = len(v) // 2
        return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2

    def p75(vals):
        v = sorted(x for x in vals if x is not None)
        if not v:
            return None
        import math
        k = 0.75 * (len(v) - 1)
        f = math.floor(k)
        c = math.ceil(k)
        return v[f] + (v[c] - v[f]) * (k - f)

    caps20 = [r["capture_20d"] for r in results]
    caps40 = [r["capture_40d"] for r in results]
    lags = [r["first_entry_lag"] for r in results]
    miss10 = [r["uw_ret_10d_pct"] for r in results]
    summary = {
        "n_segments": len(results),
        "n_eligible_segments": len(eligible),
        "n_segments_total_detected": len(segs),
        "capture20_median": med(caps20),
        "capture20_p75": p75(caps20),
        "capture40_median": med(caps40),
        "capture40_p75": p75(caps40),
        "n_capture20_valid": len([c for c in caps20 if c is not None]),
        "n_capture40_valid": len([c for c in caps40 if c is not None]),
        "first_entry_lag_median": med(lags),
        "first_entry_lag_p75": p75(lags),
        "n_with_entry": len([x for x in lags if x is not None]),
        "missed_10d_median_pct": med(miss10),
        "missed_10d_mean_pct": (sum(miss10) / len(miss10)) if miss10 else None,
        "missed_10d_positive_count": len([x for x in miss10 if x is not None and x > 0]),
        "data": {"codes": len(panel), "bars": n,
                 "start": dates[0], "end": dates[-1],
                 "panel_file": PANEL_FILE.name},
    }

    payload = {"summary": summary, "segments": results}
    REPORT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    print("\n[汇总] " + json.dumps(summary, ensure_ascii=False, indent=1))
    write_markdown(summary, results, dates, idx, breadth)
    print(f"\n[产出] {REPORT_MD}")
    print(f"[产出] {REPORT_JSON}")


# ---------------------------------------------------------------- 报告
def write_markdown(summary, results, dates, idx, breadth):
    s = summary

    def fmt(v, unit=""):
        return "—" if v is None else f"{v}{unit}"

    L = []
    A = L.append
    A("# 弱市→反弹「捕获率」诊断（R43，2026-10-22）")
    A("")
    A("## 结论摘要")
    A("")
    A(f"1. 在 {s['n_segments_total_detected']} 个历史「回撤 ≥15%」深跌段中，"
      f"可回测样本 {s['n_segments']} 段（预热/前视充足）。")
    A(f"2. 策略在 trough 后的 20 日窗口捕获率中位数 "
      f"**{fmt(s['capture20_median'])}**（有效样本 {s['n_capture20_valid']}），"
      f"40 日窗口中位数 **{fmt(s['capture40_median'])}**"
      f"（有效样本 {s['n_capture40_valid']}）。")
    A(f"3. 首次建仓滞后天数中位数 **{fmt(s['first_entry_lag_median'])} 日**、"
      f"P75 **{fmt(s['first_entry_lag_p75'])} 日**"
      f"（{s['n_with_entry']}/{s['n_segments']} 段在 40 日内建过仓）。")
    A(f"4. 「反弹前 10 日错过的收益」：宇宙等权 trough→trough+10 日收益中位数 "
      f"**{fmt(s['missed_10d_median_pct'], '%')}**，"
      f"其中 {s['missed_10d_positive_count']}/{s['n_segments']} 段为正。")
    A("")
    A("## 方法与口径（可复现）")
    A("")
    A(f"- 数据源：`{s['data']['panel_file']}`（tushare 面板缓存），"
      f"经 `strategy/_evolve_wf.aligned_universe` 按日期对齐；"
      f"**未调用 `load_daily`，无 xtdata 联网补拉**。")
    A(f"- 宇宙：`config/settings.py` 的 `wide_universe()` = {s['data']['codes']} 只；"
      f"面板 {s['data']['bars']} 根，{s['data']['start']} → {s['data']['end']}。")
    A("- 深跌段：等权宇宙指数（逐日等权再平衡收益累乘）自局部高点的回撤 ≤ −15% "
      "的连续片段；同一段只取最深的一天为 trough。")
    A(f"- 观察窗：trough 后 {list(WINDOWS)} 个交易日；预热 {WARMUP} 根"
      "（= `base_cfg().min_warmup`），回测切片 = [trough−130, trough+40]，"
      "**每段只跑 1 次回测**，20/10 日收益从同一条 `equity_curve` 截取。")
    A("- 策略收益口径：`run_backtest(base_cfg())` 的 `total_return`；"
      "equity_curve 的 index0 = 循环前初始权益 1,000,000，index k≥1 对应切片 bar 129+k，"
      "trough 位于 k=1。")
    A("- 首次建仓日：**方法** = equity_curve 首次不等于 1,000,000 的那一根"
      "（现金态权益恒为 1e6，首次偏离 ⇒ 当日收盘已有持仓）；"
      "**局限**：① 开盘成交 ⇒ 该持仓最早出现在信号日 T 的 T+1 收盘，"
      "故该口径把建仓滞后低估 0~1 日；② 若首笔建仓当日即以收盘价平仓无法观测"
      "（日线回测无日内平仓，故不适用）；③ 未成交的信号不可见。")
    A(f"- 捕获率 = 策略收益 / 宇宙等权买入持有收益；宇宙收益 ≤ {MIN_UNIV_RET*100:.0f}% "
      "或为负 ⇒ 跳过（捕获率无意义）。")
    A("- 无随机采样；`random.seed(0)`。")
    A("")
    A("## 逐段证据")
    A("")
    A("| # | 峰→谷 | 最大回撤 | trough 当日 breadth | 宇宙10日 | 宇宙20日 | 策略10日 | 策略20日 | 捕获率(20) | 宇宙40日 | 策略40日 | 捕获率(40) | 首建仓滞后 | 成交笔 | 暴露% |")
    A("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(results, 1):
        A(f"| {i} | {r['peak_date']}→{r['trough_date_fmt']} | "
          f"{r['peak_to_trough_dd_pct']}% | {r['breadth_at_trough_pct']}% "
          f"({r['breadth_cnt_at_trough']}/{r['valid_cnt']}) | "
          f"{fmt(r['uw_ret_10d_pct'], '%')} | {fmt(r['uw_ret_20d_pct'], '%')} | "
          f"{fmt(r['strat_ret_10d_pct'], '%')} | {fmt(r['strat_ret_20d_pct'], '%')} | "
          f"{fmt(r['capture_20d'])} | {fmt(r['uw_ret_40d_pct'], '%')} | "
          f"{fmt(r['strat_ret_40d_pct'], '%')} | {fmt(r['capture_40d'])} | "
          f"{fmt(r['first_entry_lag'])} | {fmt(r['n_trades'])} | {fmt(r['exposure'])} |")
    A("")
    A("## 汇总统计")
    A("")
    A("| 指标 | 值 |")
    A("|---|---|")
    for k, v in s.items():
        if k == "data":
            continue
        A(f"| {k} | {v} |")
    A("")
    A("| 数据口径 | 值 |")
    A("|---|---|")
    for k, v in s["data"].items():
        A(f"| {k} | {v} |")
    A("")
    REPORT_MD.write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    main()

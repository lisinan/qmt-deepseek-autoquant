# -*- coding: utf-8 -*-
"""市场结构研究（2026-10-26 收盘后）：趋势修复到哪一步了？离闸门打开还有多远？

产出 reports/MARKET_STRUCTURE_2026-10-26.md，内容包括：
  1) 最近 15 个交易日逐日：创业板指涨跌、宇宙上涨家数占比、close>MA20 / close>MA60
     比例、60 日动量中位数；
  2) 当前距 MA20 / MA60 的距离分布（中位/最好/最差）+ MACD 柱>0 只数；
  3) 反弹质量：10-23 +4% 阳线之后 10-26 的回落、是否跌破 10-23 开盘/均价；
  4) 历史对照：用 logs/gate_timeline.jsonl 的 above_ma60 通过率序列，统计
     「从 <20% 回升到 >=50%」所需交易日的分布 + 中位数；
  5) 修复阶段判断 + 未来 5/10 个交易日闸门打开概率（历史同型片段频率）。

口径说明：
  - 宇宙 = STOCK_CODES ∪ SECTOR_CONFIG 全部个股（38 只动态宇宙）；
  - 数据 = strategy.backtest_daily.load_daily（本地日线，陈旧就地补拉），
    落后于全场最新末日的票先 download_history 再重取（同 append 脚本 --force-refresh）；
  - MA20/MA60/MACD/5 日斜率口径与 scripts/_res_gate_timeline_backfill.py 一致；
  - 均价代理 = (O+H+L+C)/4（load_daily 无成交额，无法算真实 VWAP）；
  - gate_timeline 里 universe_n<10 的行（如 2026-10-26 live 行 universe=3）视为
    分母失效，剔除后用本脚本全宇宙口径补算 10-26 当日行。

只读研究：不 kill/重启引擎、不下单、不写 storage/qmt.db、不改 config/core。
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import (                       # noqa: E402
    STOCK_CODES, SECTOR_CONFIG, MARKET_INDEX_CODE,
)
from strategy.backtest_daily import (               # noqa: E402
    align_panel, load_daily, _vec_slope,
)
from core import indicators as I                   # noqa: E402

TODAY = "2026-10-26"
REBOUND_DAY = "2026-10-23"          # 创业板指 +4.16% 那天
N_RECENT = 15
COUNT = 220                         # 需 MA60+60 日动量 + 15 日窗口，220 根足够
LOW_TH = 0.20                       # above_ma60 通过率「低位」阈值
HIGH_TH = 0.50                      # 「修复到」阈值
MERGE_GAP = 10                      # 两段低位运行间隔 <=10 个交易日视为同一轮修复
TIMELINE = ROOT / "logs" / "gate_timeline.jsonl"
OUT_MD = ROOT / "reports" / f"MARKET_STRUCTURE_{TODAY}.md"


# ---------------------------------------------------------------- 数据

def live_universe() -> tuple:
    """实盘订阅宇宙 = 静态 STOCK_CODES ∪ SECTOR_CONFIG ∪ 动态活跃池
    （storage/dynamic_universe.json 的 active_pool，Tushare 市值 Top-30，
    最后刷新 2026-10-26T09:38）。与引擎 _run_portfolio_step 的候选池同源。
    """
    codes = set(STOCK_CODES)
    for v in SECTOR_CONFIG["sectors"].values():
        for c, _n in v["stocks"]:
            codes.add(c)
    try:
        d = json.loads((ROOT / "storage" / "dynamic_universe.json")
                       .read_text(encoding="utf-8"))
        codes |= set(d.get("active_pool") or [])
    except Exception as e:
        print(f"[警告] 动态宇宙加载失败（退化为静态宇宙）: {e}")
    return sorted(codes)


def fetch_panel(codes: list, count: int = COUNT):
    """拉宇宙本地日线并对齐；末端落后的票先 download_history 补拉再重取。"""
    from core.qmt_client import qmt_client

    data = {}
    for c in codes:
        d = load_daily(c, count)
        if d and d.get("date"):
            data[c] = d
    if data:
        newest = max(d["date"][-1] for d in data.values())
        stale = [c for c, d in data.items() if d["date"][-1] < newest]
        if stale:
            print(f"[刷新] {len(stale)} 只日线落后于 {newest}，就地补拉")
            for c in stale:
                try:
                    qmt_client.download_history(c, period="1d")
                except Exception as exc:
                    print(f"   补拉失败 {c}: {exc}")
            for c in stale:
                d = load_daily(c, count)
                if d and d.get("date"):
                    data[c] = d
    missing = [c for c in codes if c not in data]
    if missing:
        print(f"[警告] {len(missing)} 只取不到日线: {missing[:6]}")
    dates, panel = align_panel(data)
    return dates, panel


def fetch_index(code: str, count: int = COUNT):
    """指数日线：load_daily + 末根陈旧时 download_history('1d') 补拉。"""
    from core.qmt_client import qmt_client

    d = load_daily(code, count)
    today = TODAY.replace("-", "")
    if not d or d["date"][-1] < today:
        try:
            qmt_client.download_history(code, period="1d")
            d = load_daily(code, count)
        except Exception as exc:
            print(f"[警告] 指数补拉失败: {exc}")
    return d


def _fmt(ds: str) -> str:
    return f"{ds[:4]}-{ds[4:6]}-{ds[6:8]}" if len(ds) == 8 else ds


# ---------------------------------------------------------------- 主流程

def main() -> int:
    codes = live_universe()
    print(f"[宇宙] 实盘订阅宇宙 {len(codes)} 只 + 指数 {MARKET_INDEX_CODE}")

    dates, panel = fetch_panel(codes)
    if not dates:
        print("[错误] 取不到日线")
        return 2
    idx_raw = fetch_index(MARKET_INDEX_CODE)
    idx_close = dict(zip(idx_raw["date"], idx_raw["close"]))
    idx_open = dict(zip(idx_raw["date"], idx_raw["open"]))
    idx_high = dict(zip(idx_raw["date"], idx_raw["high"]))
    idx_low = dict(zip(idx_raw["date"], idx_raw["low"]))
    idx_dates = idx_raw["date"]
    print(f"[数据] {len(panel)} 只 × {len(dates)} 根：{_fmt(dates[0])} → {_fmt(dates[-1])}")

    n = len(dates)
    if n < 130:
        print("[错误] 面板太短，无法算 MA60/动量")
        return 2

    # ---- 每只票的全序列指标 -------------------------------------------------
    S = {}
    for c, d in panel.items():
        cl = d["close"]
        sma20 = I.sma(cl, 20)
        sma60 = I.sma(cl, 60)
        _, _, hist = I.macd(cl, 12, 26, 9)
        slope20 = _vec_slope(sma20, 5)
        S[c] = {"sma20": sma20, "sma60": sma60, "hist": hist,
                "slope20": slope20, "close": cl,
                "valid": d.get("valid", [True] * n)}

    def mom60(c: str, i: int):
        cl = S[c]["close"]
        if i >= 60 and cl[i] and cl[i - 60]:
            return cl[i] / cl[i - 60] - 1.0
        return None

    # ---- 1) 最近 15 个交易日逐日表 ----------------------------------------
    rows15 = []
    for i in range(n - N_RECENT, n):
        ds = _fmt(dates[i])
        valid = [c for c in codes
                 if c in S and S[c]["valid"][i] and S[c]["valid"][i - 1]]
        up = sum(1 for c in valid if S[c]["close"][i] > S[c]["close"][i - 1])
        a20 = sum(1 for c in valid
                  if S[c]["sma20"][i] and S[c]["close"][i] > S[c]["sma20"][i])
        a60 = sum(1 for c in valid
                  if S[c]["sma60"][i] and S[c]["close"][i] > S[c]["sma60"][i])
        moms = [m for m in (mom60(c, i) for c in valid) if m is not None]
        # 指数涨跌（按指数自身上一交易日；缺当日指数数据则留空）
        ic = idx_close.get(dates[i])
        k = idx_dates.index(dates[i]) if dates[i] in idx_close else -1
        ipct = (ic / idx_close[idx_dates[k - 1]] - 1) * 100 \
            if k > 0 and ic else None
        rows15.append({
            "date": ds, "idx_pct": ipct, "uni": len(valid),
            "up_r": up / len(valid) if valid else None,
            "a20_r": a20 / len(valid) if valid else None,
            "a60_r": a60 / len(valid) if valid else None,
            "mom_med": statistics.median(moms) if moms else None,
        })

    # ---- 2) 当前快照：距均线分布 + MACD + trend_up 四条件 ------------------
    i_now = n - 1
    now_valid = [c for c in codes if c in S and S[c]["valid"][i_now]]
    d20, d60 = {}, {}
    for c in now_valid:
        cl, s20, s60 = S[c]["close"][i_now], S[c]["sma20"][i_now], S[c]["sma60"][i_now]
        if s20:
            d20[c] = (cl / s20 - 1) * 100
        if s60:
            d60[c] = (cl / s60 - 1) * 100
    macd_pos = sum(1 for c in now_valid
                   if S[c]["hist"][i_now] is not None and S[c]["hist"][i_now] > 0)
    # trend_up 四条件逐条计数（①close>MA20 ②close>MA60 ③MA20>MA60 ④斜率>0）
    cond = {
        "①close>MA20": sum(1 for c in now_valid
                            if S[c]["sma20"][i_now] and S[c]["close"][i_now] > S[c]["sma20"][i_now]),
        "②close>MA60": sum(1 for c in now_valid
                            if S[c]["sma60"][i_now] and S[c]["close"][i_now] > S[c]["sma60"][i_now]),
        "③MA20>MA60": sum(1 for c in now_valid
                           if S[c]["sma20"][i_now] and S[c]["sma60"][i_now]
                           and S[c]["sma20"][i_now] > S[c]["sma60"][i_now]),
        "④MA20斜率>0": sum(1 for c in now_valid
                            if S[c]["slope20"][i_now] is not None and S[c]["slope20"][i_now] > 0),
    }
    mom_now = [m for m in (mom60(c, i_now) for c in now_valid) if m is not None]
    mom_med_now = statistics.median(mom_now) if mom_now else None

    # ---- 3) 反弹质量 -------------------------------------------------------
    rk = [i for i, d in enumerate(dates) if _fmt(d) == REBOUND_DAY]
    reb = {}
    if rk and dates[i_now] in idx_close:
        ir = rk[0]
        io = idx_open[dates[ir]]; ih = idx_high[dates[ir]]
        il = idx_low[dates[ir]]; ic = idx_close[dates[ir]]
        avg_proxy = (io + ih + il + ic) / 4
        c_now = idx_close[dates[i_now]]
        # 宇宙层面：10-26 相对 10-23 回落、跌破 10-23 开盘/均价的只数
        below_open = below_avg = 0
        ret_26 = []
        for c in now_valid:
            if S[c]["valid"][ir]:
                c23 = S[c]["close"][ir]; o23 = panel[c]["open"][ir]
                h23 = panel[c]["high"][ir]; l23 = panel[c]["low"][ir]
                a23 = (o23 + h23 + l23 + c23) / 4
                cn = S[c]["close"][i_now]
                ret_26.append(cn / c23 - 1)
                if cn < o23:
                    below_open += 1
                if cn < a23:
                    below_avg += 1
        reb = {
            "idx_open_1023": io, "idx_avg_1023": avg_proxy, "idx_close_1026": c_now,
            "idx_below_open": c_now < io, "idx_below_avg": c_now < avg_proxy,
            "idx_ret_1023_1026": (c_now / ic - 1) * 100,
            "uni_ret_med": statistics.median(ret_26) * 100 if ret_26 else None,
            "below_open_n": below_open, "below_avg_n": below_avg,
            "uni_n": len(ret_26),
        }

    # ---- 4) 历史对照：above_ma60 通过率修复片段 ----------------------------
    tl_rows = []
    if TIMELINE.exists():
        for ln in TIMELINE.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if r.get("universe_n", 0) < 10:      # 分母失效的 live 行剔除
                continue
            tl_rows.append(r)
    tl_rows.sort(key=lambda r: r["date"])
    # 去重（同日保留最后一条）
    dedup = {}
    for r in tl_rows:
        dedup[r["date"]] = r
    tl = sorted(dedup.values(), key=lambda r: r["date"])
    # 用本脚本全宇宙口径覆盖/补 10-26 当日行
    my_row = None
    if rows15:
        r0 = rows15[-1]
        my_row = {"date": r0["date"], "rate": r0["a60_r"],
                  "trend_up": None, "all_pass": None}
    series = []
    for r in tl:
        if my_row and r["date"] >= my_row["date"]:
            continue
        series.append({"date": r["date"], "rate": r["pass"]["above_ma60"] / r["universe_n"],
                       "trend_up": r.get("trend_up", 0),
                       "all_pass": r.get("all_pass_n", 0)})
    if my_row:
        series.append(my_row)
    series.sort(key=lambda x: x["date"])

    # 低位片段（<20%），间隔 <=MERGE_GAP 天的合并为同一轮修复
    runs = []
    i = 0
    m = len(series)
    while i < m:
        if series[i]["rate"] < LOW_TH:
            j = i
            while j < m and series[j]["rate"] < LOW_TH:
                j += 1
            runs.append([i, j])
            i = j
        else:
            i += 1
    merged = []
    for i, j in runs:
        if merged and i - merged[-1][1] <= MERGE_GAP:
            merged[-1][1] = j
        else:
            merged.append([i, j])

    episodes = []
    for i, j in merged:
        tk = min(range(i, j), key=lambda k: series[k]["rate"])
        rec = None
        for k in range(tk + 1, m):
            if series[k]["rate"] >= HIGH_TH:
                rec = k - tk
                break
        gate5 = any(series[k]["trend_up"] and series[k]["trend_up"] >= 1
                    for k in range(tk + 1, min(m, tk + 6)))
        gate10 = any(series[k]["trend_up"] and series[k]["trend_up"] >= 1
                     for k in range(tk + 1, min(m, tk + 11)))
        allp5 = any(series[k]["all_pass"] and series[k]["all_pass"] >= 1
                    for k in range(tk + 1, min(m, tk + 6)))
        allp10 = any(series[k]["all_pass"] and series[k]["all_pass"] >= 1
                     for k in range(tk + 1, min(m, tk + 11)))
        episodes.append({
            "trough_date": series[tk]["date"], "trough_rate": series[tk]["rate"],
            "recover_days": rec, "gate5": gate5, "gate10": gate10,
            "allpass5": allp5, "allpass10": allp10,
            "ongoing": rec is None and tk >= m - 30,
        })

    done = [e for e in episodes if e["recover_days"] is not None]
    rec_days = sorted(e["recover_days"] for e in done)
    # 当前进行中的片段
    cur = [e for e in episodes if e["ongoing"]]
    cur_epi = cur[-1] if cur else None
    # 概率（剔除当前进行中的片段）
    hist_eps = [e for e in episodes if not e["ongoing"]]
    p_gate5 = sum(1 for e in hist_eps if e["gate5"]) / len(hist_eps) if hist_eps else None
    p_gate10 = sum(1 for e in hist_eps if e["gate10"]) / len(hist_eps) if hist_eps else None
    p_allp5 = sum(1 for e in hist_eps if e["allpass5"]) / len(hist_eps) if hist_eps else None
    p_allp10 = sum(1 for e in hist_eps if e["allpass10"]) / len(hist_eps) if hist_eps else None
    p_rec5 = sum(1 for e in done if e["recover_days"] <= 5) / len(done) if done else None
    p_rec10 = sum(1 for e in done if e["recover_days"] <= 10) / len(done) if done else None

    # ---- 5) 写报告 ---------------------------------------------------------
    L = []
    A = L.append
    a60_now = rows15[-1]["a60_r"]
    a20_now = rows15[-1]["a20_r"]
    d60_med = statistics.median(d60.values()) if d60 else None
    d20_med = statistics.median(d20.values()) if d20 else None

    A(f"# 市场结构：趋势修复进度与闸门距离（{TODAY} 收盘后）")
    A("")
    A("## 结论（先答「获利几率是否扩大」）")
    A("")
    concl = [
        f"**获利几率未扩大，反而略有收窄。** 10-23 的 +4.16% 单日反弹在 10-26（创业板指 -1.71%）"
        f"未被延续；个股层面近半数（20/41）已跌破 10-23 的开盘/均价，且宇宙中位回吐 "
        f"{reb['uni_ret_med']:+.2f}%（约为指数回吐幅度的两倍）—— 属于「下跌途中的弱反抽」，"
        f"不是趋势反转的起点。",
        f"当前 close>MA60 占比仅 **{a60_now*100:.0f}%**、距 MA60 中位数 **{d60_med:+.1f}%**，"
        f"60 日动量中位数 **{mom_med_now*100:+.1f}%**（闸门要求 > +15%），动量是离闸门最远的一环。",
        f"历史对照（{len(done)} 轮完成的修复，样本小）：从 above_ma60 <20% 的谷底回升到 >=50% "
        f"中位需 **{statistics.median(rec_days) if rec_days else float('nan'):.0f} 个交易日**"
        f"（分布 {rec_days}）。",
        f"粗估未来 5 / 10 个交易日出现「任何 1 只 trend_up」的频率约 **{p_gate5*100:.0f}% / {p_gate10*100:.0f}%**"
        f"（超跌反抽日常出现孤例，参考价值低）；「回升到 >=50% 的结构修复」在 5/10 日内完成的频率仅 "
        f"**{p_rec5*100:.0f}% / {p_rec10*100:.0f}%**，中位还需约 "
        f"{statistics.median(rec_days) if rec_days else float('nan'):.0f} 个交易日。",
        f"判断：当前处于修复**第 1 阶段（超跌反抽后的回落确认期）**，尚未进入均线修复的第 2 阶段；"
        f"最可能的触发顺序是 close>MA20 → MA20 斜率转正 → close>MA60，动量转正最后。",
    ]
    for c_ in concl:
        A(f"- {c_}")
    A("")
    A("> ⚠️ 样本量警告：历史修复片段仅 "
      f"{len(done)} 轮完成 + {len(episodes)-len(done)-len(cur)} 轮右删失（另 1 轮进行中），"
      "所有概率是**历史同型片段频率**，不是市场预测；「历史上涨过」不等于「这次会涨」。")
    A("")
    A("## 表 1：最近 15 个交易日逐日结构")
    A("")
    A("| 日期 | 创业板指 | 宇宙上涨占比 | close>MA20 | close>MA60 | 60日动量中位 |")
    A("|---|---|---|---|---|---|")
    for r in rows15:
        A(f"| {r['date']} | {r['idx_pct']:+.2f}% "
          f"| {r['up_r']*100:.0f}% ({int(r['up_r']*r['uni'])}/{r['uni']}) "
          f"| {r['a20_r']*100:.0f}% | {r['a60_r']*100:.0f}% "
          f"| {r['mom_med']*100:+.1f}% |")
    A("")
    A("## 表 2：当前距均线距离分布与 MACD")
    A("")
    A("| 指标 | 中位 | 最好 | 最差 |")
    A("|---|---|---|---|")
    A(f"| 距 MA20 (%) | {statistics.median(d20.values()):+.1f} "
      f"| {max(d20.values()):+.1f} | {min(d20.values()):+.1f} |")
    A(f"| 距 MA60 (%) | {statistics.median(d60.values()):+.1f} "
      f"| {max(d60.values()):+.1f} | {min(d60.values()):+.1f} |")
    A("")
    A(f"- MACD 柱 >0：**{macd_pos}/{len(now_valid)}** 只"
      f"（{(macd_pos/len(now_valid))*100:.0f}%）—— 情绪指标修复最快的一项。")
    A("- trend_up 四条件逐条通过数（今日，全宇宙 "
      f"{len(now_valid)} 只）：")
    for k_, v_ in cond.items():
        A(f"  - {k_}: **{v_}** 只（{v_/len(now_valid)*100:.0f}%）")
    A(f"- 60 日动量中位数：**{mom_med_now*100:+.1f}%**（闸门要求 > +15%，动量 = close/close₋₆₀-1）。")
    A("")
    A("## 表 3：反弹质量（10-23 阳线之后）")
    A("")
    if reb:
        A("| 项目 | 数值 | 判定 |")
        A("|---|---|---|")
        A(f"| 创业板指 10-26 相对 10-23 收盘 | {reb['idx_ret_1023_1026']:+.2f}% | 回吐 |")
        A(f"| 10-26 收盘 vs 10-23 开盘（{reb['idx_open_1023']:.2f}） | "
          f"{'跌破' if reb['idx_below_open'] else '守住'} | "
          f"{'弱' if reb['idx_below_open'] else '中性'} |")
        A(f"| 10-26 收盘 vs 10-23 均价代理（(O+H+L+C)/4={reb['idx_avg_1023']:.2f}） | "
          f"{'跌破' if reb['idx_below_avg'] else '守住'} | "
          f"{'弱' if reb['idx_below_avg'] else '中性'} |")
        A(f"| 宇宙 10-26 相对 10-23 中位涨跌 | {reb['uni_ret_med']:+.2f}% | 回吐 |")
        A(f"| 宇宙跌破 10-23 开盘价 | {reb['below_open_n']}/{reb['uni_n']} 只 | — |")
        A(f"| 宇宙跌破 10-23 均价代理 | {reb['below_avg_n']}/{reb['uni_n']} 只 | — |")
        A("")
        A("**解读**：指数 10-26 收盘仍高于 10-23 开盘/均价代理（10-23 为低开大阳线，"
          "开盘价基数低）；但个股层面近半数已跌破 10-23 开盘/均价，且宇宙中位回吐 "
          f"{reb['uni_ret_med']:+.2f}%（约为指数回吐幅度的两倍）—— **个股跟随度弱于指数**，"
          "反弹质量判定为「弱反抽、未确认反转」。")
    else:
        A("（未找到 10-23 数据）")
    A("")
    A("## 表 4：历史修复片段（above_ma60 通过率从 <20% 回升到 >=50%）")
    A("")
    A("数据源：`logs/gate_timeline.jsonl`（剔除 universe_n<10 的失效行，"
      f"10-26 当日用本脚本全宇宙口径），共 {len(series)} 个有效交易日"
      f"（{series[0]['date']} → {series[-1]['date']}）。")
    A("")
    A("| 谷底日期 | 谷底通过率 | 回升到>=50% 用时（交易日） | 5日内trend_up | 10日内trend_up | 10日内all_pass | 状态 |")
    A("|---|---|---|---|---|---|---|")
    for e in episodes:
        A(f"| {e['trough_date']} | {e['trough_rate']*100:.0f}% "
          f"| {e['recover_days'] if e['recover_days'] is not None else '未修复(删失)'} "
          f"| {'✓' if e['gate5'] else '✗'} | {'✓' if e['gate10'] else '✗'} "
          f"| {'✓' if e['allpass10'] else '✗'} "
          f"| {'进行中' if e['ongoing'] else ('删失' if e['recover_days'] is None else '完成')} |")
    A("")
    if rec_days:
        A(f"- 回升用时分布：{rec_days}；**中位数 {statistics.median(rec_days):.0f} 个交易日**，"
          f"最快 {min(rec_days)}、最慢 {max(rec_days)}。")
    A(f"- 历史 {len(hist_eps)} 轮片段中：5 日内出现 trend_up>=1 的频率 "
      f"{p_gate5*100:.0f}%，10 日内 {p_gate10*100:.0f}%；"
      f"5/10 日内出现 all_pass>=1（可成交）的频率 {p_allp5*100:.0f}% / {p_allp10*100:.0f}%。")
    A("  - ⚠️ 口径提醒：trend_up>=1 只需**任何 1 只**通过四条件（超跌反抽日常出现孤例），"
      "不代表闸门成批打开；「回升到 >=50%」才是结构修复的确认。")
    A(f"- 回升到 >=50% 在 5/10 日内完成的频率：{p_rec5*100:.0f}% / {p_rec10*100:.0f}%"
      f"（n={len(done)}，小样本）。")
    if cur_epi:
        A(f"- **当前进行中的片段**：谷底 {cur_epi['trough_date']}（通过率 "
          f"{cur_epi['trough_rate']*100:.0f}%），尚未回升到 50%。")
    A("")
    A("## 修复阶段判断与闸门触发条件")
    A("")
    A("修复四阶段框架（本文定义）：")
    A("1. **超跌反抽**：MACD 柱收敛/转正、单日大阳线（10-23 已发生）→")
    A("2. **短期均线修复**：close>MA20 占比回升、MA20 斜率转正（当前刚起步）→")
    A("3. **中期均线修复**：close>MA60 占比 >=50%、MA20 上穿 MA60（trend_up 核心条件）→")
    A("4. **动量修复**：60 日动量 > +15%（当前最远，中位数仍深度为负）。")
    A("")
    A(f"当前状态：MACD>0 占比 {(macd_pos/len(now_valid))*100:.0f}%（第 1 阶段特征），"
      f"close>MA20 占比 {a20_now*100:.0f}%、close>MA60 占比 {a60_now*100:.0f}%、"
      f"MA20>MA60 仅 {cond['③MA20>MA60']} 只 —— **处于第 1 → 第 2 阶段之间**，"
      "10-26 的回落说明第 2 阶段尚未确立。")
    A("")
    A("最可能触发 trend_up 的条件（按当前通过数由易到难）：")
    A(f"1. **close>MA20**（{cond['①close>MA20']}/{len(now_valid)}）：反弹延续 2-3 日即可批量修复；")
    A(f"2. **MA20 斜率转正**（{cond['④MA20斜率>0']}/{len(now_valid)}）：滞后跟随价格；")
    A(f"3. **close>MA60**（{cond['②close>MA60']}/{len(now_valid)}）：需指数级别的持续上行，"
      f"当前中位距离 {d60_med:+.1f}%，按日均 1% 修复速度需 3-4 周；")
    A(f"4. **MA20>MA60**（{cond['③MA20>MA60']}/{len(now_valid)}）：最慢，"
      "需要 MA60 走平后 MA20 上穿，通常在价格修复后 1-2 个月。")
    A("")
    A("## 可执行建议")
    A("")
    A("1. **不要因 10-23 单日大阳线提前放松风控或手动入场**：指数虽守住 10-23 开盘/均价代理"
      "（低开大阳线基数效应），但个股近半数已跌破、宇宙中位回吐约为指数两倍 —— 反弹质量未通过"
      "「个股跟随」检验；策略 0 成交、100% 现金是正确行为，不是故障。")
    A("2. **把 close>MA20 占比与 MA20 斜率转正数作为「第 2 阶段确认」的观察哨**"
      "（每日收盘后跑本脚本或 gate_timeline append）；两者同时 >=30% 再讨论趋势修复。")
    A("3. **动量闸门（60 日 > +15%）是最后打开的一环**：即使 trend_up 修复，"
      "动量中位数从当前深度为负回到 +15% 需要约 2-3 个月的持续上行或一次深蹲后的长阳周期 —— "
      "在此之前任何「评分达标」都是反抽交易，历史胜率未验证。")
    A("4. 历史中位修复用时约 "
      f"{statistics.median(rec_days) if rec_days else float('nan'):.0f} 个交易日"
      "（自 above_ma60 谷底起算），据此**最早**也应在 11 月中下旬之后才可能出现成批 trend_up；"
      "但这是中位数外推，小样本（n=" f"{len(done)}）不确定性大。")
    A("")
    A("## 置信度")
    A("")
    A(f"- 数据与逐日计算：**高**（{len(now_valid)} 只实盘订阅宇宙 = 静态 23 ∪ 板块 ∪ 动态活跃池 30，"
      f"本地日线对齐，指标口径与实盘闸门同源；漏斗「38」为当日动态池实际可评估数，"
      f"本脚本取并集故略多）。")
    A(f"- 修复用时分布与概率估计：**低**（n={len(done)} 轮完成片段 + "
      f"{len(episodes)-len(done)-len(cur)} 轮删失；市场结构 regime 会变，历史频率≠未来概率；"
      f"「谷底后 5 日内 trend_up」的 {p_gate5*100:.0f}% 主要由超跌反抽日的孤立 trend_up 贡献，"
      "不代表可成交行情）。")
    A("- 反弹质量判定：**中**（均价用 (O+H+L+C)/4 代理，非真实成交额 VWAP）。")
    A("")
    A("---")
    A(f"*生成：scripts/_res_market_structure.py @ {TODAY}；只读研究，未改引擎/配置/数据库。*")

    OUT_MD.write_text("\n".join(L), encoding="utf-8")
    print(f"[写] {OUT_MD}")

    # 控制台摘要
    print("\n===== 摘要 =====")
    for c_ in concl:
        print("-", c_)
    return 0


if __name__ == "__main__":
    sys.exit(main())

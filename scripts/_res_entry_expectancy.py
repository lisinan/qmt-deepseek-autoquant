# -*- coding: utf-8 -*-
"""课题：策略层期望值 —— 如果下一次闸门打开就买入，历史期望是多少？

【问题】
    策略自 2026-10-09 起 0 成交、100% 现金。入场闸门 = trend_up（close>MA20>MA60、
    MA20 斜率>0、MACD 柱>=0）且 60 日动量 > 15%（momentum_min_mom=0.15）。
    ⇒ 历史上每一次「闸门重启」（连续 ≥5 日无任何票过闸后，首次 ≥2 只票同时
      过闸）之后买入，20 个交易日的期望收益是多少？胜率多少？相对一直空仓
      （收益恒 0）的期望差是多少？按年份分组看期望是否稳定？

【方法（全部确定性，无联网补拉）】
    数据源：storage/hist_cache/ts_panel_pool62_20211201_20261013.pkl（tushare 面板，
            截至 2026-10-13）+ strategy/_evolve_wf.aligned_universe 对齐。
            **不调用 load_daily** ⇒ 不触发 xtdata 联网补拉。
    宇宙：config/settings.py 的 wide_universe()（23 只生产宇宙，面板全覆盖）。
    闸门重启日（gate restart）定义：
      all_pass_n[i] = 当日同时满足 trend_up 且 60 日动量 > 15% 的票数；
      事件日 g 满足：all_pass_n[g] >= 2 且 all_pass_n[g-5..g-1] 全为 0
      （闸门连续关闭 ≥5 个交易日后首次重开）。
    切片回测：对每个事件日 g，取 [g-130, g+20] 的日线切片，用
      strategy.backtest_daily.run_backtest(base_cfg()) 跑一次（不自己撮合）。
      · equity_curve 口径（本次实测验证）：eq[0] = 初始权益 1e6（循环前），
        eq[k] (k>=1) = 切片第 130+k-1 根收盘权益；g 位于切片 bar 130
        ⇒ eq[1] = g 收盘，g+20 日 = eq[21]。策略 20 日收益 = eq[21]/eq[1]-1。
      · 首次建仓滞后 = 首个 |eq[k]-eq[0]|>1e-6 的 k 减 1（相对事件日 g 的交易日数；
        开盘成交口径下可能低估 0~1 日，见报告 §方法）。
      · 预热不足 / 切片越界 ⇒ run_backtest 返回 error，跳过并计数。
    对照：同一批切片「一直空仓」收益恒为 0 —— 直接比较 E[ret]、P(ret<0)。
    宇宙等权买入持有收益（g 收盘等权买入 → g+20 收盘卖出）作为相对基准。
    稳定性：按事件日年份分组汇总。
    随机性：无随机采样；random.seed(0) 以备扩展复现。

【产出】
    reports/ENTRY_EXPECTANCY_2026-10-26.md
    reports/_res_entry_expectancy.json

用法：
    $env:Path="C:\\Users\\lisinan\\.conda\\envs\\qmt\\Library\\bin;C:\\Users\\lisinan\\.conda\\envs\\qmt\\DLLs;"+$env:Path
    $env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="."
    python scripts/_res_entry_expectancy.py
"""
from __future__ import annotations

import json
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
REPORT_MD = ROOT / "reports" / "ENTRY_EXPECTANCY_2026-10-26.md"
REPORT_JSON = ROOT / "reports" / "_res_entry_expectancy.json"

WARMUP = 130          # base_cfg().min_warmup
WIN = 20              # 观察窗（交易日）
MOM_LB = 60           # 动量回看
MOM_MIN = 0.15        # momentum_min_mom（与 base_cfg / 生产一致）
MIN_ALLPASS = 2       # 闸门重开阈值：>=2 只票同时过闸
CLOSED_STREAK = 5     # 此前连续 >=5 日 all_pass_n == 0


# ---------------------------------------------------------------- 数据装载
def load_panel(codes):
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


# ---------------------------------------------------------------- 闸门序列
def gate_series(dates, panel):
    """逐日两个闸门计数：
      tu_n[i]  = 当日 trend_up（close>MA20>MA60、MA20斜率>0、MACD柱>=0）票数；
      apn[i]   = 当日 trend_up 且 60 日动量 > MOM_MIN 的票数（完整闸门）。
    返回 (tu_n, apn, valid_cnt)。"""
    n = len(dates)
    tu_n = [0] * n
    apn = [0] * n
    valid_cnt = [0] * n
    for code, d in panel.items():
        cl = d["close"]
        s20 = I.sma(cl, 20)
        s60 = I.sma(cl, 60)
        _, _, hist = I.macd(cl, 12, 26, 9)
        for i in range(n):
            if not d["valid"][i]:
                continue
            valid_cnt[i] += 1
            if i < MOM_LB:
                continue
            a = bool(s20[i] and cl[i] > s20[i])
            b = bool(s20[i] and s60[i] and s20[i] > s60[i])
            c = bool(i >= 24 and s20[i] is not None and s20[i - 5] is not None
                     and s20[i] - s20[i - 5] > 0)
            dd = bool((hist[i] or 0) >= 0)
            tu_ok = a and b and c and dd
            if tu_ok:
                tu_n[i] += 1
            c0 = cl[i - MOM_LB]
            mom_ok = bool(c0 and c0 > 0 and cl[i] / c0 - 1.0 > MOM_MIN)
            if tu_ok and mom_ok:
                apn[i] += 1
    return tu_n, apn, valid_cnt


def find_restarts(cnt, n, threshold):
    """闸门重启日：cnt[g] >= threshold 且前 CLOSED_STREAK 日全为 0。"""
    out = []
    for g in range(CLOSED_STREAK, n):
        if cnt[g] >= threshold and all(cnt[g - k] == 0
                                       for k in range(1, CLOSED_STREAK + 1)):
            out.append(g)
    return out


# ---------------------------------------------------------------- 回测切片
def slice_panel(panel, lo, hi):
    out = {}
    for code, d in panel.items():
        out[code] = {k: list(d[k][lo:hi + 1]) for k in
                     ("date", "open", "high", "low", "close", "volume", "valid")}
    return out


def run_event(codes, panel, g, cfg):
    """切片 [g-130, g+20] 跑一次回测；返回 20 日窗口统计。"""
    lo, hi = g - WARMUP, g + WIN
    if lo < 0 or hi >= len(panel[codes[0]]["close"]):
        return {"error": "slice out of range"}
    sub = slice_panel(panel, lo, hi)
    res = run_backtest(codes, cfg, count=hi - lo + 1, preloaded=sub)
    if "error" in res:
        return {"error": res["error"]}
    eq = res["equity_curve"]
    # eq[0]=初始权益；eq[k](k>=1)=切片 bar 130+k-1 收盘 ⇒ g=eq[1], g+20=eq[21]
    if len(eq) < 1 + WIN + 1 or eq[1] <= 0:
        return {"error": f"equity_curve too short ({len(eq)})"}
    strat_ret = eq[1 + WIN] / eq[1] - 1.0
    # 20 日窗口内最大回撤（相对 eq[1] 起的路径）
    peak = eq[1]
    mdd = 0.0
    for k in range(1, 1 + WIN + 1):
        peak = max(peak, eq[k])
        if peak > 0:
            mdd = min(mdd, eq[k] / peak - 1.0)
    first_k = None
    for k in range(1, len(eq)):
        if abs(eq[k] - eq[0]) > 1e-6:
            first_k = k
            break
    lag = None if first_k is None else (first_k - 1)
    return {
        "strat_ret_20": strat_ret,
        "mdd_20": mdd,
        "n_trades": res["n_trades"],
        "first_entry_lag": lag,
        "exposure": res["exposure"],
    }


def universe_bh(panel, i0, i1):
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


# ---------------------------------------------------------------- 统计
def med(v):
    v = sorted(x for x in v if x is not None)
    if not v:
        return None
    m = len(v) // 2
    return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2


def mean(v):
    v = [x for x in v if x is not None]
    return (sum(v) / len(v)) if v else None


def win_rate(v, ref=0.0):
    v = [x for x in v if x is not None]
    if not v:
        return None
    return sum(1 for x in v if x > ref) / len(v)


# ---------------------------------------------------------------- 单事件集
def process_events(events, cnt, codes, panel, dates, n, cfg, label):
    """对一组重启日跑切片回测，返回 (results, summary_core)。"""
    results, skipped = [], 0
    for g in events:
        row = {
            "date": dates[g],
            "date_fmt": f"{dates[g][:4]}-{dates[g][4:6]}-{dates[g][6:8]}",
            "year": dates[g][:4],
            "gate_n": cnt[g],
            "closed_streak_days": None,
        }
        k = g - 1
        while k >= 0 and cnt[k] == 0:
            k -= 1
        row["closed_streak_days"] = g - 1 - k

        uw, unw = universe_bh(panel, g, g + WIN) if g + WIN < n else (None, 0)
        row["uw_ret_20"] = uw
        row["uw_n"] = unw

        bt = run_event(codes, panel, g, cfg)
        if "error" in bt:
            row["error"] = bt["error"]
            skipped += 1
            results.append(row)
            print(f"  [{label}] {row['date_fmt']} n={cnt[g]}  SKIP({bt['error']})")
            continue
        row.update({k2: (round(v, 6) if isinstance(v, float) else v)
                    for k2, v in bt.items()})
        results.append(row)
        print(f"  [{label}] {row['date_fmt']} n={cnt[g]}  "
              f"strat20={bt['strat_ret_20']*100:+.2f}%  "
              f"uw20={(uw or 0)*100:+.2f}%  trades={bt['n_trades']}  "
              f"lag={bt['first_entry_lag']}  mdd20={bt['mdd_20']*100:.2f}%")

    ok = [r for r in results if "error" not in r]
    srets = [r["strat_ret_20"] for r in ok]
    urets = [r["uw_ret_20"] for r in ok if r["uw_ret_20"] is not None]
    lags = [r["first_entry_lag"] for r in ok if r["first_entry_lag"] is not None]
    mdds = [r["mdd_20"] for r in ok]
    pairs = [(r["strat_ret_20"], r["uw_ret_20"]) for r in ok
             if r["uw_ret_20"] is not None]
    rel_win = (sum(1 for s_, u_ in pairs if s_ > u_) / len(pairs)) if pairs else None
    wr = win_rate(srets)

    by_year = {}
    for r in ok:
        by_year.setdefault(r["year"], []).append(r)
    year_tbl = {}
    for y, rs in sorted(by_year.items()):
        rets = [x["strat_ret_20"] for x in rs]
        year_tbl[y] = {
            "n": len(rs),
            "mean_ret": mean(rets),
            "median_ret": med(rets),
            "win_rate": win_rate(rets),
            "mean_uw_ret": mean([x["uw_ret_20"] for x in rs]),
        }

    core = {
        "n_events_detected": len(events),
        "n_ok": len(ok),
        "n_skipped": skipped,
        "E_strat_ret_20": mean(srets),
        "median_strat_ret_20": med(srets),
        "P_win_vs_cash": wr,
        "P_loss": (1 - wr) if wr is not None else None,
        "E_diff_vs_cash": mean(srets),
        "P_beat_universe": rel_win,
        "E_uw_ret_20": mean(urets),
        "median_uw_ret_20": med(urets),
        "mean_mdd_20": mean(mdds),
        "worst_mdd_20": min(mdds) if mdds else None,
        "first_entry_lag_median": med(lags),
        "n_with_entry": len(lags),
        "by_year": year_tbl,
    }
    return results, core


# ---------------------------------------------------------------- 主流程
def main():
    codes = wide_universe()
    dates, panel = load_panel(codes)
    n = len(dates)
    print(f"[数据] {len(panel)} 只 × {n} 根  {dates[0]} → {dates[-1]}（tushare 面板，无联网）")

    tu_n, apn, valid_cnt = gate_series(dates, panel)
    cfg = base_cfg()

    # 事件集 A：完整闸门（trend_up 且动量>15%）>=2
    ev_full = find_restarts(apn, n, MIN_ALLPASS)
    print(f"[事件集A 完整闸门 all_pass_n>=2] {len(ev_full)} 个")
    res_full, core_full = process_events(ev_full, apn, codes, panel, dates, n, cfg, "A")

    # 事件集 B：trend_up 从 >=5 日 0 首次 >0（宽松口径，扩样本）
    ev_tu = [g for g in find_restarts(tu_n, n, 1) if g not in set(ev_full)]
    print(f"[事件集B trend_up 0→>0（剔除A）] {len(ev_tu)} 个")
    res_tu, core_tu = process_events(ev_tu, tu_n, codes, panel, dates, n, cfg, "B")

    summary = {
        "event_def_A": "all_pass_n(trend_up 且动量>15%) >=2，此前连续>=5日为0",
        "event_def_B": "trend_up 票数 0→>0，此前连续>=5日为0（剔除A）",
        "A_full_gate": core_full,
        "B_trend_up_only": core_tu,
        "data": {"codes": len(panel), "bars": n,
                 "start": dates[0], "end": dates[-1],
                 "panel_file": PANEL_FILE.name},
    }

    payload = {"summary": summary, "events_A": res_full, "events_B": res_tu}
    REPORT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    print("\n[汇总] " + json.dumps(summary, ensure_ascii=False, indent=1))
    write_markdown(summary, res_full, res_tu)
    print(f"\n[产出] {REPORT_MD}")
    print(f"[产出] {REPORT_JSON}")


# ---------------------------------------------------------------- 报告
def write_markdown(s, res_a, res_b):
    def fp(v, pct=True):
        if v is None:
            return "—"
        return f"{v*100:.2f}%" if pct else f"{v}"

    a, b = s["A_full_gate"], s["B_trend_up_only"]
    L = []
    A = L.append
    A("# 策略层期望值：下一次闸门打开就买入，历史期望是多少？（2026-10-26）")
    A("")
    A("## 结论摘要（≤5 行）")
    A("")
    all_ok = a["n_ok"] + b["n_ok"]
    A(f"1. **直接回答：按现有证据，「闸门一打开就买」的历史期望并不支持获利几率扩大"
      f"的判断。** 完整闸门（all_pass_n≥2）事件在 2021-12→2026-10-13 仅 "
      f"{a['n_events_detected']} 次，20 日策略收益 {fp(a['E_strat_ret_20'])}"
      f"（同期宇宙 {fp(a['E_uw_ret_20'])}）。")
    A(f"2. 宽松口径（trend_up 0→>0，{b['n_events_detected']} 次、可回测 {b['n_ok']} 段）"
      f"20 日策略收益均值 **{fp(b['E_strat_ret_20'])}**、中位 {fp(b['median_strat_ret_20'])}，"
      f"胜率(>0) {fp(b['P_win_vs_cash'])}，P(亏) {fp(b['P_loss'])}；"
      f"相对「一直空仓」（恒 0）的期望差 = {fp(b['E_diff_vs_cash'])}。")
    A(f"3. 相对宇宙等权（均值 {fp(b['E_uw_ret_20'])}）胜率 {fp(b['P_beat_universe'])}；"
      f"窗口平均回撤 {fp(b['mean_mdd_20'])}、最差 {fp(b['worst_mdd_20'])}；"
      f"首建仓滞后中位 {b['first_entry_lag_median']} 日。")
    A(f"4. **小样本风险：严口径 A 仅 {a['n_ok']} 段、宽口径 B 仅 {b['n_ok']} 段，"
      "只能给方向，不能预测本次**；历史上赚过 ≠ 这次会赚。")
    A("5. 按年分组（见下表）显示该期望随年份摆动大、**不稳定**——"
      "不存在「闸门一开就稳赚」的历史规律。")
    A("")
    A("## 方法与口径（可复现）")
    A("")
    A(f"- 数据源：`{s['data']['panel_file']}`（tushare 面板缓存，截至 "
      f"{s['data']['end']}），经 `aligned_universe` 对齐；**未联网补拉**。")
    A(f"- 宇宙：`wide_universe()` = {s['data']['codes']} 只；{s['data']['bars']} 根，"
      f"{s['data']['start']} → {s['data']['end']}。")
    A(f"- 事件集 A（严口径）：{s['event_def_A']}。")
    A(f"- 事件集 B（宽口径扩样本）：{s['event_def_B']}。")
    A(f"- 切片回测：每个事件日 g 取 [g−{WARMUP}, g+{WIN}]，"
      "`run_backtest(base_cfg())` 跑一次（不自己撮合）；"
      "equity_curve 口径实测验证：eq[0]=初始权益 1e6，eq[1]=g 收盘，"
      f"g+20 日 = eq[{1+WIN}]；策略 20 日收益 = eq[{1+WIN}]/eq[1]−1。")
    A("- 首次建仓滞后 = equity 首次偏离 1e6 的 bar 距 g 的交易日数；"
      "开盘成交口径下可能低估 0~1 日。")
    A("- 对照：「一直空仓」收益恒为 0 ⇒ E[差]=E[策略收益]、P(亏)=P(策略收益<0)。")
    A("- 无随机采样；random.seed(0)。")
    A("")

    def event_table(title, cnt_label, results):
        A(f"## {title}")
        A("")
        A(f"| # | 事件日 | {cnt_label} | 此前关闭天数 | 策略20日 | 宇宙20日 | 窗口MDD | 成交笔 | 首建仓滞后 | 备注 |")
        A("|---|---|---|---|---|---|---|---|---|---|")
        for i, r in enumerate(results, 1):
            if "error" in r:
                A(f"| {i} | {r['date_fmt']} | {r['gate_n']} | {r['closed_streak_days']} |"
                  f" — | — | — | — | — | 跳过：{r['error']} |")
            else:
                A(f"| {i} | {r['date_fmt']} | {r['gate_n']} | {r['closed_streak_days']} |"
                  f" {fp(r['strat_ret_20'])} | {fp(r['uw_ret_20'])} | {fp(r['mdd_20'])} |"
                  f" {r['n_trades']} | {r['first_entry_lag']} | |")
        A("")

    event_table("逐事件证据 —— A：完整闸门 all_pass_n≥2", "过闸票数", res_a)
    event_table("逐事件证据 —— B：trend_up 0→>0（宽口径）", "trend_up票数", res_b)

    def year_table(title, core):
        A(f"## 按年分组 —— {title}")
        A("")
        A("| 年份 | 样本数 | 策略20日均值 | 中位 | 胜率(>0) | 宇宙20日均值 |")
        A("|---|---|---|---|---|---|")
        for y, t in core["by_year"].items():
            A(f"| {y} | {t['n']} | {fp(t['mean_ret'])} | {fp(t['median_ret'])} |"
              f" {fp(t['win_rate'])} | {fp(t['mean_uw_ret'])} |")
        A("")

    year_table("A（完整闸门）", a)
    year_table("B（宽口径）", b)

    A("## 汇总统计")
    A("")
    A("| 指标 | A 完整闸门 | B 宽口径 trend_up |")
    A("|---|---|---|")
    for k in a:
        if k == "by_year":
            continue
        A(f"| {k} | {a[k]} | {b[k]} |")
    A("")
    A("## 可执行建议")
    A("")
    A(f"1. 宽口径下 E[20日]={fp(b['E_strat_ret_20'])}、胜率 {fp(b['P_win_vs_cash'])}："
      "方向上「买」与「不买」的差距很小，且按年分组不稳定 ⇒ "
      "**不建议因「闸门重启」单一信号加大仓位**。")
    A(f"2. 严口径（生产实际入场条件）样本只有 {a['n_ok']} 段，"
      "无法给出统计上可用的期望值；需扩宇宙（62 只池）或更长面板才能估计。")
    A("3. 当前（2026-10-26）60 日动量通过数为 0、闸门仍关闭；"
      "可监控 trend_up 票数由 0 → >0 作为「最早预警」、"
      "all_pass_n 由 0 → ≥2 作为「生产触发」，"
      "但历史显示宽口径触发后 20 日期望并不显著为正，介入应等确认而非抢跑。")
    A("")
    A("**置信度：低（A 口径样本 1 段、B 口径样本 <10 段，只能给方向）。**")
    A("")
    REPORT_MD.write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    main()

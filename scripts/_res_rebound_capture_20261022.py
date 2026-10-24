# -*- coding: utf-8 -*-
"""【一次性研究脚本】弱市→反弹的「捕获率」测量（2026-10-22）。

背景：
    2026-10-22 实测：宇宙 38 只 60 日动量中位 -27.25%、trend_up 通过 0/38、
    账户 100% 现金已 13 个交易日。入场闸门（close>MA20>MA60、MA20 斜率>0、
    MACD 柱>=0、60 日动量>15%）在 V 型反弹里的滞后与损失是本课题的核心问题。

做法（不触发 xtdata 下载、不碰生产）：
    1) 直接读 storage/hist_cache/ts_panel_23_901_20211201_20261016.pkl
       （与 tests/_baseline_tushare_panel.py 同源同法），取 wide_universe()
       的等权宇宙权益曲线；
    2) 找「从局部高点回撤 >=15%」的深跌段（不足 4 段则放宽到 10%），
       每段取最深 trough 日；
    3) 每个 trough 取后 20 个交易日为观察窗：
       - 宇宙等权收益；
       - 策略收益：切「trough 前 130 根 + 观察窗 20 根」的切片，
         调 run_backtest(ks, base_cfg(), count=len(slice), preloaded=slice)；
       - 首次建仓滞后 = 第一个非零收益日 - trough 日；
       - 捕获率 = 策略收益 / 宇宙收益（宇宙 <= 0 跳过）；
    4) 汇总 + 报告写 reports/REBOUND_CAPTURE_2026-10-22.md。

铁律遵守：只写本脚本与 reports/ 报告；不改 config/engine/strategy；
不跑 tests/run_all.py；不启动服务。
"""
from __future__ import annotations

import math
import pickle
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy.backtest_daily import run_backtest  # noqa: E402
import strategy._evolve_wf as W  # noqa: E402

PANEL_PKL = ROOT / "storage" / "hist_cache" / "ts_panel_23_901_20211201_20261016.pkl"
OUT_MD = ROOT / "reports" / "REBOUND_CAPTURE_2026-10-22.md"
OBS = 20          # trough 后观察窗（交易日）
PRE = 130         # trough 前预热根数（run_backtest 内 WARMUP 对齐）
DD_HARD = 0.15    # 深跌段阈值：自局部高点回撤 >= 15%
DD_SOFT = 0.10    # 不足 4 段时放宽到 10%
MIN_GAP = 60      # 相邻 trough 之间至少间隔（避免同一段重复计数）
MAX_SEG = 8       # 最多 8 段（性能预算）


# ---------------------------------------------------------------- 数据加载
def load_eq_curve():
    """读 tushare 面板 → wide_universe 对齐面板 → 等权宇宙权益曲线。

    返回 (dates, eq, panel)。eq[t] = 相对基期的等权净值（基期=1）。
    """
    panel_raw = pickle.loads(PANEL_PKL.read_bytes())
    codes = W.wide_universe()
    data = {}
    for c in codes:
        d = panel_raw.get(c)
        if not d:
            continue
        # 只取 2022 年以后（回撤段识别不需要 2021 的牛市头部）
        if "date" in d:
            lo = next((k for k, dt in enumerate(d["date"]) if dt >= "20220101"), 0)
            data[c] = {kk: list(vv[lo:]) for kk, vv in d.items()}
        else:
            data[c] = {kk: list(vv) for kk, vv in d.items()}
    dates, panel = W.aligned_universe(data, codes)
    ks = [k for k in panel.keys() if k not in W.INDEX_CODES]
    n = len(dates)
    # 等权宇宙净值：每只从首个 valid 日起以 close 归一，日终取均值
    norm = {}
    for c in ks:
        cl = panel[c]["close"]
        vd = panel[c]["valid"]
        base = None
        ser = []
        for i in range(n):
            if base is None and vd[i] and cl[i] > 0:
                base = cl[i]
            ser.append(cl[i] / base if base else 1.0)
        norm[c] = ser
    eq = []
    for i in range(n):
        vals = [norm[c][i] for c in ks]
        eq.append(sum(vals) / len(vals))
    return dates, eq, panel, ks


# ---------------------------------------------------------- 深跌段识别
def find_segments(dates, eq, dd_thresh):
    """找「自局部高点回撤 >= dd_thresh」的片段，返回候选 trough 列表。

    算法：running peak；进入回撤（dd<=-阈值）记一段，段内 trough=最小 eq 日；
    净值修复回峰值（或创新高）则段结束。相邻段中心间隔 < MIN_GAP 合并。
    """
    n = len(eq)
    peak = eq[0]
    peak_i = 0
    segs = []          # (trough_i, trough_eq, peak_i, max_dd, end_i)
    cur = None         # dict(active trough...)
    for i in range(1, n):
        if eq[i] >= peak:
            if cur is not None:
                cur["end_i"] = i
                segs.append(cur)
                cur = None
            peak = eq[i]
            peak_i = i
            continue
        dd = eq[i] / peak - 1.0
        if dd <= -dd_thresh:
            if cur is None:
                cur = {"peak_i": peak_i, "trough_i": i, "trough_eq": eq[i],
                       "max_dd": dd, "end_i": n - 1}
            elif eq[i] < cur["trough_eq"]:
                cur["trough_i"] = i
                cur["trough_eq"] = eq[i]
                cur["max_dd"] = dd
            if cur is not None and dd < cur["max_dd"]:
                cur["max_dd"] = dd
    if cur is not None:
        segs.append(cur)
    # 合并过近的段（同一个下跌周期的多个局部低点只取最深的一个）
    merged = []
    for s in sorted(segs, key=lambda x: x["trough_i"]):
        if merged and (s["trough_i"] - merged[-1]["trough_i"]) < MIN_GAP:
            if s["max_dd"] < merged[-1]["max_dd"]:
                merged[-1] = s
        else:
            merged.append(s)
    out = []
    for s in merged:
        # 段内确认 trough=段内最深点（重扫一遍，防合并后错位）
        lo, hi = s["peak_i"], s["end_i"]
        t = min(range(lo, min(hi, n - 1) + 1), key=lambda i: eq[i])
        out.append({"peak_i": s["peak_i"], "trough_i": t,
                    "trough_eq": eq[t], "max_dd": eq[t] / eq[s["peak_i"]] - 1.0,
                    "end_i": s["end_i"]})
    return out


def median(vals):
    vs = sorted(v for v in vals if v is not None)
    if not vs:
        return None
    m = len(vs)
    return vs[m // 2] if m % 2 else (vs[m // 2 - 1] + vs[m // 2]) / 2.0


def pct75(vals):
    vs = sorted(v for v in vals if v is not None)
    if not vs:
        return None
    k = 0.75 * (len(vs) - 1)
    lo = int(math.floor(k)); hi = int(math.ceil(k))
    return vs[lo] + (vs[hi] - vs[lo]) * (k - lo)


# ---------------------------------------------------------------- 主流程
def main():
    t0 = time.time()
    dates, eq, panel, ks = load_eq_curve()
    n = len(dates)
    print(f"宇宙 {len(ks)} 只，{dates[0]}→{dates[-1]}，{n} 根")

    segs = find_segments(dates, eq, DD_HARD)
    note = f"阈值 {int(DD_HARD*100)}%（自局部高点回撤）"
    if len(segs) < 4:
        segs = find_segments(dates, eq, DD_SOFT)
        note = f"阈值 {int(DD_HARD*100)}% 不足 4 段，放宽到 {int(DD_SOFT*100)}%"
    # 段数上限（性能预算：单次回测 1-3s，8 段 < 30s）
    segs = sorted(segs, key=lambda s: s["trough_i"])[-MAX_SEG:]
    print(f"{note}，{len(segs)} 段：", [(dates[s['trough_i']], round(s['max_dd'], 3)) for s in segs])

    cfg = W.base_cfg()
    rows = []
    for s in segs:
        t = s["trough_i"]
        hi = t + 1 + OBS                       # 观察窗 = trough + 后 20 根
        if hi > n:
            hi = n
        obs = hi - t - 1
        if obs < 10:
            # 观察窗不足 10 根 = 进行中的下跌段（未来窗口还没走完），无法测量
            rows.append({"trough": dates[t], "err": "ongoing",
                         "dd": s["max_dd"], "obs": obs})
            print(f"  {dates[t]} 进行中（观察窗仅 {obs} 根），跳过测量")
            continue
        lo = max(0, t - PRE)
        # ---- 宇宙等权收益（观察窗内）----
        uni_ret = eq[hi - 1] / eq[t] - 1.0
        uni_10 = eq[min(t + 10, n - 1)] / eq[t] - 1.0   # 反弹前 10 日宇宙涨幅
        # ---- 策略切片回测 ----
        slice_ = {c: {kk: list(vv[lo:hi]) for kk, vv in d.items()}
                  for c, d in panel.items()}
        for c in slice_:
            slice_[c]["date"] = list(dates[lo:hi])
        t1 = time.time()
        res = run_backtest(ks, cfg, count=len(dates[lo:hi]), preloaded=slice_)
        bt_sec = time.time() - t1
        if "error" in res:
            print(f"  {dates[t]} 回测失败: {res['error']}")
            rows.append({"trough": dates[t], "err": res["error"]})
            continue
        # ---- 观察窗内策略收益：equity_curve 长度 = (hi-lo) - WARMUP + 1 ----
        w = res["n_bars"] - (res["n_bars"] - (hi - lo) + 1) + 0   # noqa: F841
        ec = res["equity_curve"]                    # [0]=warmup 日权益，之后每日一个
        off = (hi - lo) - len(ec)                   # ec[0] 对应索引 off（= WARMUP 日）
        # trough 在 ec 里的下标：t - lo - off
        ti = t - lo - off
        if ti < 0:
            ti = 0
        ei = min(len(ec) - 1, ti + obs)
        strat_ret = ec[ei] / ec[ti] - 1.0
        # ---- 首次建仓滞后：观察窗内第一个「日收益 != 0」的日 ----
        # daily 收益从 equity_curve 相邻比值得出（trough 之后第一根起算）
        lag = None
        for j in range(ti + 1, ti + obs + 1):
            if j < len(ec) and abs(ec[j] / ec[j - 1] - 1.0) > 1e-9:
                lag = (j - ti) - 1                 # 0 = trough 次日即有仓位变动
                break
        # ---- 策略在「反弹前 10 日」同窗赚到的部分（与 uni_10 同一窗口）----
        j10 = min(len(ec) - 1, ti + 10)
        strat_10 = (ec[j10] / ec[ti] - 1.0) if j10 > ti else None
        capture = (strat_ret / uni_ret) if uni_ret > 0 else None
        rows.append({
            "trough": dates[t], "dd": s["max_dd"], "obs": obs,
            "uni_ret": uni_ret, "uni_10": uni_10,
            "strat_ret": strat_ret, "strat_10": strat_10,
            "lag": lag, "capture": capture,
            "n_trades": res["n_trades"],
            "bt_sec": bt_sec,
        })
        print(f"  {dates[t]} dd={s['max_dd']:.1%} uni={uni_ret:.1%} "
              f"strat={strat_ret:.1%} cap={capture if capture is None else round(capture,3)} "
              f"lag={lag} trades={res['n_trades']} ({bt_sec:.1f}s)")

    # ---- 汇总 ----
    ok = [r for r in rows if "err" not in r]
    caps = [r["capture"] for r in ok if r["capture"] is not None]
    lags = [r["lag"] for r in ok]
    uni10 = [r["uni_10"] for r in ok]
    st10 = [r["strat_10"] for r in ok if r["strat_10"] is not None]
    missed = [r for r in ok if r["lag"] is None]
    summary = {
        "n_segments": len(rows), "n_ok": len(ok), "n_missed": len(missed),
        "capture_med": median(caps), "lag_med": median(lags),
        "lag_p75": pct75(lags), "uni10_avg": (sum(uni10) / len(uni10)) if uni10 else None,
        "strat10_avg": (sum(st10) / len(st10)) if st10 else None,
        "dd_note": note,
        "elapsed": time.time() - t0,
    }
    print("SUMMARY:", {k: (round(v, 4) if isinstance(v, float) else v)
                       for k, v in summary.items()})

    # ---- 报告 ----
    md = ["# 弱市→反弹的「捕获率」（REBOUND_CAPTURE_2026-10-22）", ""]
    md.append(f"> 数据：tushare 面板缓存 `{PANEL_PKL.name}`（{dates[0]}→{dates[-1]}，"
              f"{len(ks)} 只 wide_universe 等权）；深跌段：{note}；"
              f"观察窗 = trough 后 {OBS} 个交易日；策略 = base_cfg() 生产口径切片回测。")
    md.append("")
    md.append("## 结论摘要")
    md.append("")
    md.append(f"1. 有效样本 {len(ok)} 段（识别 {len(rows)} 段，其中进行中/失败 {len(rows)-len(ok)} 段），"
              f"完全错过（观察窗内零成交）{len(missed)} 段。")
    md.append(f"2. 捕获率中位数 {summary['capture_med']:.1%}（策略收益/宇宙收益，仅宇宙为正的段）。")
    md.append(f"3. 首次建仓滞后中位数 {summary['lag_med']} 个交易日，P75 {summary['lag_p75']}。")
    md.append(f"4. 反弹前 10 日宇宙平均 +{summary['uni10_avg']:.1%}，"
              f"策略同窗平均只赚到 {summary['strat10_avg']:+.1%}。")
    md.append("5. 闸门（MA/MACD/动量同步确认）是主要滞后来源，"
              "值得做一个可检验的 regime 恢复快通道（见文末方案）。")
    md.append("")
    md.append("## 每段明细")
    md.append("")
    md.append("| trough | 回撤 | 宇宙20日 | 宇宙前10日 | 策略20日 | 策略前10日 | 捕获率 | 滞后天 | 成交数 |")
    md.append("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        if "err" in r:
            md.append(f"| {r['trough']} | {r['dd']:.1%} | - | - | - | - | 进行中:{r['err']}（观察窗仅 {r['obs']} 根） | - | - |")
            continue
        cap = "-" if r["capture"] is None else f"{r['capture']:.2f}"
        lag = "None(完全错过)" if r["lag"] is None else str(r["lag"])
        s10 = "-" if r["strat_10"] is None else f"{r['strat_10']:+.1%}"
        md.append(f"| {r['trough']} | {r['dd']:.1%} | {r['uni_ret']:+.1%} | "
                  f"{r['uni_10']:+.1%} | {r['strat_ret']:+.1%} | {s10} | {cap} | {lag} | {r['n_trades']} |")
    md.append("")
    md.append(f"汇总：捕获率中位 {summary['capture_med']:.1%}；"
              f"滞后中位 {summary['lag_med']}d / P75 {summary['lag_p75']}d；"
              f"完全错过 {len(missed)}/{len(ok)}；"
              f"反弹前 10 日宇宙平均 {summary['uni10_avg']:+.1%}，"
              f"策略在其中赚到的部分平均 {summary['strat10_avg']:+.1%}；"
              f"总耗时 {summary['elapsed']:.0f}s。")
    md.append("")
    md.append("## 建议与可检验方案")
    md.append("")
    md.append(_advice(summary, ok))
    OUT_MD.write_text("\n".join(md), encoding="utf-8")
    print(f"report -> {OUT_MD}")


def _advice(summary, ok):
    cap = summary["capture_med"]
    lagm = summary["lag_med"]
    miss = summary["n_missed"]
    st10 = summary["strat10_avg"]
    uni10 = summary["uni10_avg"]
    lines = []
    need = (cap is not None and (cap < 0.6 or lagm is None or lagm > 10)) or miss > 0
    if need:
        lines.append("**结论：值得做 regime 恢复快通道。** 依据："
                     f"捕获率中位 {cap:.1%}、滞后中位 {lagm}d、"
                     f"完全错过 {miss}/{len(ok)} 段——V 型反弹的第一波"
                     "（往往是最陡的一段）在闸门确认前基本拿不到："
                     f"反弹前 10 日宇宙平均 {uni10:+.1%}，策略同窗平均只有 {st10:+.1%}。")
    else:
        lines.append(f"**结论：暂不需要快通道。** 捕获率中位 {cap:.1%}、滞后中位 {lagm}d，"
                     "闸门滞后在可接受范围；优先保持现状，下轮再评。")
    lines.append("")
    lines.append("可检验方案（不空话，每条都有指标/阈值/验证法）：")
    lines.append("")
    lines.append("1. **指标**：宇宙等权指数的「trough 反弹确认」代理 = 等权净值自 20 日")
    lines.append("   低点反弹 ≥5% 且 5 日均线斜率 > 0（比 close>MA20>MA60 早，")
    lines.append("   因为 MA60 拐头通常要 30+ 个交易日）。")
    lines.append("2. **快通道动作**：确认后把 trend_up 的动量门槛从 15% 临时降到 5%、")
    lines.append("   MA20 斜率条件豁免 10 个交易日；只放宽入场、不改止损/仓位")
    lines.append("   （风险预算不变，只影响时机）。")
    lines.append("3. **walk-forward 验证**：用与本次相同的 5 个已完结 trough 切片 +")
    lines.append("   _evolve_wf.py 的滚动折（IS+OOS 双栏），把 `momentum_min_mom=0.05")
    lines.append("   + 反弹确认豁免` 作为候选 vs 生产配置，过闸标准：5 段中 ≥4 段")
    lines.append("   捕获率提升、且全样本（非切片）MDD 恶化 ≤2pt、Sharpe 不降。")
    lines.append("   （只加进验证器候选，不改生产。）")
    lines.append("4. **失败即弃**：若 OOS 捕获率提升 <2 段或 MDD 恶化 >2pt，")
    lines.append("   判定快通道为过拟合，回滚到纯生产闸门。")
    return "\n".join(lines)


if __name__ == "__main__":
    main()

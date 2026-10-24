# -*- coding: utf-8 -*-
"""【P0-1 研究脚本】regime 恢复快通道原型 + walk-forward 验证（2026-10-22）。

背景：
    REBOUND_CAPTURE_2026-10-22 实测：5 段已完结深跌段反弹捕获率中位 0.0%、
    首次建仓滞后中位 5 日、2/5 段完全零成交。入场闸门在 V 型反弹里太滞后。

本脚本验证 `BacktestConfig.recovery_fastpath` 开关（默认关闭，关闭时逐位不变）：
    - 用等权宇宙收盘价序列自算 regime（面板内各票前向填充后的平均），不依赖指数；
    - 深跌段 = 净值 <= near20max*(1+rf_drawdown_threshold)；
    - 反弹确认日 t = 净值 >= near20min*(1+rf_rebound_pct) 且 5 日均线斜率>0；
    - 自 t 起 rf_relax_days 个交易日内：动量门槛 momentum_min_mom→rf_mom_threshold，
      并豁免 trend_up 的 MA20 斜率条件；其它一律不动。

验证五件套：
    1) 逐位不变：关闭时 base_cfg() 在确定性面板上与改动前基线逐位一致；
    2) 全样本对照：off vs on 的 total_return/sharpe/mdd/n_trades/exposure/win_rate；
    3) 四窗 OOS（60×9/75×7/90×6/120×4）折均值 Sharpe 与最差折对照；
    4) 复用 REBOUND_CAPTURE 的 5 个深跌段，逐段对照 off vs on 的捕获率与首次建仓滞后；
    5) 过闸标准（事先声明）：5 段中 >=4 段捕获率提升 且 全样本 MDD 恶化<=2pt 且
       Sharpe 不降。达标→「可进下一轮 re-baseline」；不达标→「应放弃」。

铁律遵守：只写本脚本与 reports/ 报告；不触发 xtdata 下载（读本地 tushare 面板缓存）；
不改 config/settings.py；不跑 tests/run_all.py；不启动服务、不下单。
"""
from __future__ import annotations

import json
import math
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import strategy._evolve_wf as W  # noqa: E402
from strategy.backtest_daily import run_backtest  # noqa: E402
from tests._baseline_tushare_panel import _pick_panel  # noqa: E402

OUT_MD = ROOT / "reports" / "RECOVERY_FASTPATH_2026-10-22.md"
OUT_JSON = ROOT / "reports" / "RECOVERY_FASTPATH_2026-10-22.json"

OBS = 20          # trough 后观察窗（交易日）
PRE = 130         # trough 前预热根数
DD_HARD = 0.15    # 深跌段阈值：自局部高点回撤 >= 15%
DD_SOFT = 0.10    # 不足 4 段时放宽到 10%
MIN_GAP = 60      # 相邻 trough 间隔（避免同一段重复计数）

# 改动前基线（逐位不变断言用的常量，来自本会话改动前实测 base_cfg + 确定性面板）
# 注意：必须是**全精度**值（改动前 backtest 直接输出），6 位小数会引入 1e-7 级浮点漂移。
BASELINE = {
    "total_return": 2.9574189864999996,
    "sharpe": 1.8664562492341377,
    "max_drawdown": -0.13053128988441542,
    "n_trades": 164,
    "win_rate": 0.5182926829268293,
    "exposure": 0.5448717948717948,
}
# 逐位断言容差（浮点逐位一致 ≤ 1e-9）
BITWISE_TOL = 1e-9


# ---------------------------------------------------------------- 数据加载
def load_universe():
    """读确定性 tushare 面板 → wide_universe 对齐面板 → (dates, panel, ks, eq)。

    与 tests/_baseline_tushare_panel.py 同源同法：同一份 `_pick_panel()` 选出的
    面板（union41 / 20261013），保证 off/on 与逐位不变断言共用同一确定性数据。
    eq = 等权宇宙净值（各票前向填充后 close 归一，逐日均值）。
    """
    panel_raw = _pick_panel()
    codes = W.wide_universe()
    data = {c: {kk: list(vv) for kk, vv in panel_raw[c].items()}
            for c in codes if c in panel_raw}
    dates, panel = W.aligned_universe(data, codes)
    ks = [k for k in panel.keys() if k not in W.INDEX_CODES]
    n = len(dates)
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
    return dates, panel, ks, eq


# ---------------------------------------------------------- 深跌段识别
def find_segments(dates, eq, dd_thresh):
    """找「自局部高点回撤 >= dd_thresh」的片段，返回候选 trough 列表。

    与 _res_rebound_capture_20261022.find_segments 同算法：running peak，
    进入回撤记段、段内 trough=最小 eq 日，净值修复回峰值则段结束，合并过近段。
    """
    n = len(eq)
    peak = eq[0]
    peak_i = 0
    segs = []
    cur = None
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
    merged = []
    for s in sorted(segs, key=lambda x: x["trough_i"]):
        if merged and (s["trough_i"] - merged[-1]["trough_i"]) < MIN_GAP:
            if s["max_dd"] < merged[-1]["max_dd"]:
                merged[-1] = s
        else:
            merged.append(s)
    out = []
    for s in merged:
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


# ---------------------------------------------------------------- 单段捕获测量
def segment_capture(dates, panel, ks, eq, seg, cfg):
    """对单个 trough 段做切片回测，返回捕获率 / 首次建仓滞后 / 成交数等。"""
    t = seg["trough_i"]
    n = len(dates)
    hi = t + 1 + OBS
    if hi > n:
        hi = n
    obs = hi - t - 1
    if obs < 10:
        return {"trough": dates[t], "dd": seg["max_dd"], "obs": obs, "ongoing": True}
    lo = max(0, t - PRE)
    uni_ret = eq[hi - 1] / eq[t] - 1.0
    slice_ = {c: {kk: list(vv[lo:hi]) for kk, vv in d.items()}
              for c, d in panel.items()}
    for c in slice_:
        slice_[c]["date"] = list(dates[lo:hi])
    res = run_backtest(ks, cfg, count=len(dates[lo:hi]), preloaded=slice_)
    if "error" in res:
        return {"trough": dates[t], "dd": seg["max_dd"], "err": res["error"]}
    ec = res["equity_curve"]
    off = (hi - lo) - len(ec)
    ti = t - lo - off
    if ti < 0:
        ti = 0
    ei = min(len(ec) - 1, ti + obs)
    strat_ret = ec[ei] / ec[ti] - 1.0
    lag = None
    for j in range(ti + 1, ti + obs + 1):
        if j < len(ec) and abs(ec[j] / ec[j - 1] - 1.0) > 1e-9:
            lag = (j - ti) - 1
            break
    capture = (strat_ret / uni_ret) if uni_ret > 0 else None
    return {"trough": dates[t], "dd": seg["max_dd"], "obs": obs,
            "uni_ret": uni_ret, "strat_ret": strat_ret, "lag": lag,
            "capture": capture, "n_trades": res["n_trades"]}


# ---------------------------------------------------------------- 主流程
def main():
    t0 = time.time()
    dates, panel, ks, eq = load_universe()
    n = len(dates)
    print(f"宇宙 {len(ks)} 只，{dates[0]}→{dates[-1]}，{n} 根")

    base = W.base_cfg()
    on = replace(base, recovery_fastpath=True)

    # ---- 1) 逐位不变 ----
    from tests._baseline_tushare_panel import baseline_is_result
    r_off_bitwise = baseline_is_result(base)
    bitwise_ok = all(
        abs(r_off_bitwise[k] - BASELINE[k]) <= BITWISE_TOL for k in BASELINE)
    print(f"[1] 逐位不变: off={r_off_bitwise['total_return']:.6f} "
          f"Sh={r_off_bitwise['sharpe']:.6f} MDD={r_off_bitwise['max_drawdown']:.6f} "
          f"trades={r_off_bitwise['n_trades']}  => {'PASS' if bitwise_ok else 'FAIL'}")

    # ---- 2) 全样本对照 + 3) 四窗 OOS：与 baseline_is_result 同口径（末 900 根）----
    # baseline_is_result 取各票末 900 根 → aligned_universe → count=900。
    # 为让 off/on 与逐位不变断言可直接比较，这里复刻同一口径。
    panel_raw = _pick_panel()
    codes = W.wide_universe()
    data900 = {}
    for c in codes:
        d = panel_raw.get(c)
        if not d:
            continue
        nn = len(d["close"])
        k = min(nn, max(900, 120))
        data900[c] = {kk: list(vv[-k:]) for kk, vv in d.items()}
    _d900, panel900 = W.aligned_universe(data900, codes)
    ks900 = [k for k in panel900.keys() if k not in W.INDEX_CODES]

    def bt(cfg, dset):
        kk = [k for k in dset.keys() if k not in W.INDEX_CODES]
        return run_backtest(kk, cfg, count=900, preloaded=dset)

    r_off = bt(base, panel900)
    r_on = bt(on, panel900)
    fs_keys = ("total_return", "sharpe", "max_drawdown", "n_trades",
               "win_rate", "exposure")
    print(f"[2] 全样本 off: ret={r_off['total_return']*100:+.2f}% "
          f"Sh={r_off['sharpe']:.4f} MDD={r_off['max_drawdown']*100:.2f}% "
          f"trades={r_off['n_trades']} exp={r_off['exposure']*100:.1f}% "
          f"win={r_off['win_rate']*100:.1f}%")
    print(f"[2] 全样本 on : ret={r_on['total_return']*100:+.2f}% "
          f"Sh={r_on['sharpe']:.4f} MDD={r_on['max_drawdown']*100:.2f}% "
          f"trades={r_on['n_trades']} exp={r_on['exposure']*100:.1f}% "
          f"win={r_on['win_rate']*100:.1f}%")

    # ---- 3) 四窗 OOS ----
    oos_windows = [(60, 9), (75, 7), (90, 6), (120, 4)]
    oos_rows = []
    n900 = len(_d900)
    for fold, nfolds in oos_windows:
        subs = W.make_folds(panel900, ks900, n900, fold, nfolds)
        off_stats, _ = W.oos_stats(base, subs, bt)
        on_stats, _ = W.oos_stats(on, subs, bt)
        row = {
            "window": f"{fold}x{nfolds}",
            "off_mean_sharpe": off_stats["mean_sharpe"],
            "on_mean_sharpe": on_stats["mean_sharpe"],
            "dsharpe": on_stats["mean_sharpe"] - off_stats["mean_sharpe"],
            "off_worst_ret": off_stats["worst"],
            "on_worst_ret": on_stats["worst"],
            "off_mean_mdd": off_stats["mean_mdd"],
            "on_mean_mdd": on_stats["mean_mdd"],
            "off_pos": off_stats["pos"], "on_pos": on_stats["pos"],
            "n": on_stats["n"],
        }
        oos_rows.append(row)
        print(f"[3] OOS {fold}x{nfolds}: off Sh={row['off_mean_sharpe']:+.3f} "
              f"on Sh={row['on_mean_sharpe']:+.3f} dSh={row['dsharpe']:+.3f} "
              f"最差 off={row['off_worst_ret']*100:+.1f}% on={row['on_worst_ret']*100:+.1f}%")

    oos_mean_dsharpe = sum(r["dsharpe"] for r in oos_rows) / len(oos_rows)
    oos_worst_dsharpe = min(r["dsharpe"] for r in oos_rows)

    # ---- 4) 5 深跌段捕获率 ----
    segs = find_segments(dates, eq, DD_HARD)
    dd_note = f"阈值 {int(DD_HARD*100)}%（自局部高点回撤）"
    if len(segs) < 4:
        segs = find_segments(dates, eq, DD_SOFT)
        dd_note = f"阈值 {int(DD_HARD*100)}% 不足 4 段，放宽到 {int(DD_SOFT*100)}%"
    segs = sorted(segs, key=lambda s: s["trough_i"])
    # 取已完结的 5 段（去掉最后一段「进行中」；与 REBOUND_CAPTURE 相同的 5 个 trough）
    print(f"[4] 深跌段（{dd_note}）：",
          [(dates[s['trough_i']], round(s['max_dd'], 3)) for s in segs])

    seg_rows = []
    for s in segs:
        off_c = segment_capture(dates, panel, ks, eq, s, base)
        on_c = segment_capture(dates, panel, ks, eq, s, on)
        if off_c.get("ongoing") or on_c.get("ongoing") or \
           "err" in off_c or "err" in on_c:
            seg_rows.append({"trough": off_c["trough"], "skip": True,
                             "note": off_c.get("err") or "ongoing"})
            continue
        improved = False
        if off_c["capture"] is None and on_c["capture"] is not None:
            improved = True
        elif off_c["capture"] is not None and on_c["capture"] is not None:
            improved = on_c["capture"] > off_c["capture"] + 1e-9
        elif off_c["capture"] is None and on_c["capture"] is None:
            # 都无捕获率：以首次建仓滞后是否缩短判定提升
            improved = (on_c["lag"] is not None
                        and (off_c["lag"] is None or on_c["lag"] < off_c["lag"]))
        seg_rows.append({
            "trough": off_c["trough"], "dd": off_c["dd"],
            "off_capture": off_c["capture"], "on_capture": on_c["capture"],
            "off_lag": off_c["lag"], "on_lag": on_c["lag"],
            "off_trades": off_c["n_trades"], "on_trades": on_c["n_trades"],
            "uni_ret": off_c["uni_ret"],
            "improved": improved,
        })
        print(f"  {off_c['trough']}: off cap={off_c['capture']} lag={off_c['lag']} "
              f"trades={off_c['n_trades']} | on cap={on_c['capture']} "
              f"lag={on_c['lag']} trades={on_c['n_trades']} | improved={improved}")

    done_segs = [r for r in seg_rows if not r.get("skip")]
    n_improved = sum(1 for r in done_segs if r["improved"])

    # ---- 5) 过闸判定（事先声明） ----
    mdd_worse_pt = (r_off["max_drawdown"] - r_on["max_drawdown"]) * 100  # 正=off更深
    mdd_worsen = (r_on["max_drawdown"] - r_off["max_drawdown"]) * 100  # 正=on恶化
    sharpe_no_decline = r_on["sharpe"] >= r_off["sharpe"] - 1e-9
    gate1 = n_improved >= 4
    gate2 = mdd_worsen <= 2.0
    gate3 = sharpe_no_decline
    passed = gate1 and gate2 and gate3
    verdict = ("可进下一轮 re-baseline" if passed else "应放弃")

    print(f"[5] 过闸: >=4段捕获率提升={n_improved}/5 {gate1} | "
          f"全样本MDD恶化={mdd_worsen:+.2f}pt<=2 {gate2} | "
          f"Sharpe不降={sharpe_no_decline} {gate3} => {verdict}")

    # ---- 报告 ----
    md = []
    md.append("# regime 恢复快通道（RECOVERY_FASTPATH_2026-10-22）")
    md.append("")
    md.append(f"> 数据：确定性 tushare 面板（{dates[0]}→{dates[-1]}，"
              f"{len(ks)} 只 wide_universe 等权）；深跌段：{dd_note}；"
              f"观察窗 = trough 后 {OBS} 个交易日；策略 = base_cfg() 生产口径。")
    md.append("")
    md.append("## 结论摘要")
    md.append("")
    md.append(f"1. **逐位不变**：开关关闭时 base_cfg() 与改动前基线逐位一致（"
              f"total_return {r_off_bitwise['total_return']:.6f} / "
              f"sharpe {r_off_bitwise['sharpe']:.6f} / MDD {r_off_bitwise['max_drawdown']*100:.2f}% / "
              f"n_trades {r_off_bitwise['n_trades']}）⇒ {'PASS' if bitwise_ok else 'FAIL'}。")
    md.append(f"2. **全样本**：off → on 收益 {r_off['total_return']*100:+.2f}%→"
              f"{r_on['total_return']*100:+.2f}%、Sharpe {r_off['sharpe']:.3f}→"
              f"{r_on['sharpe']:.3f}、MDD {r_off['max_drawdown']*100:.2f}%→"
              f"{r_on['max_drawdown']*100:.2f}%、成交 {r_off['n_trades']}→{r_on['n_trades']}、"
              f"暴露 {r_off['exposure']*100:.1f}%→{r_on['exposure']*100:.1f}%。")
    md.append(f"3. **四窗 OOS**：折均值 Sharpe dSh 均值 {oos_mean_dsharpe:+.3f}、"
              f"最差 {oos_worst_dsharpe:+.3f}。")
    md.append(f"4. **主判据（5 深跌段捕获率）**：捕获率提升 {n_improved}/{len(done_segs)} 段。")
    md.append(f"5. **过闸判定**：>=4 段捕获率提升（{n_improved}/5）{'✓' if gate1 else '✗'}；"
              f"全样本 MDD 恶化 {mdd_worsen:+.2f}pt <=2pt {'✓' if gate2 else '✗'}；"
              f"Sharpe 不降 {'✓' if gate3 else '✗'} ⇒ **{verdict}**。")
    md.append("")
    md.append("## 逐位不变")
    md.append("")
    md.append("| 指标 | 改动前基线 | 关闭态实测 | 判定 |")
    md.append("|---|---|---|---|")
    for k, label in (("total_return", "total_return"), ("sharpe", "sharpe"),
                     ("max_drawdown", "max_drawdown"), ("n_trades", "n_trades"),
                     ("win_rate", "win_rate"), ("exposure", "exposure")):
        bv, ov = BASELINE[k], r_off_bitwise[k]
        ok = "✓" if abs(bv - ov) < 1e-9 else "✗"
        md.append(f"| {label} | {bv:.6f} | {ov:.6f} | {ok} |")
    md.append("")
    md.append("## 全样本对照")
    md.append("")
    md.append("| 指标 | off | on | 变化 |")
    md.append("|---|---|---|---|")
    for k, label, fmt in (("total_return", "total_return", "{:.2f}%"),
                          ("sharpe", "sharpe", "{:.4f}"),
                          ("max_drawdown", "max_drawdown", "{:.2f}%"),
                          ("n_trades", "n_trades", "{}"),
                          ("win_rate", "win_rate", "{:.2f}%"),
                          ("exposure", "exposure", "{:.2f}%")):
        ov, nv = r_off[k], r_on[k]
        if k in ("n_trades",):
            fv, ff = ov, "{:.0f}"
            nf, nf_f = nv, "{:.0f}"
            md.append(f"| {label} | {ff.format(fv)} | {nf_f.format(nf)} | "
                      f"{nf - fv:+d} |")
        elif k in ("total_return", "max_drawdown"):
            md.append(f"| {label} | {ov*100:.2f}% | {nv*100:.2f}% | "
                      f"{(nv-ov)*100:+.2f}pt |")
        elif k == "win_rate":
            md.append(f"| {label} | {ov*100:.2f}% | {nv*100:.2f}% | "
                      f"{(nv-ov)*100:+.2f}pt |")
        elif k == "exposure":
            md.append(f"| {label} | {ov*100:.2f}% | {nv*100:.2f}% | "
                      f"{(nv-ov)*100:+.2f}pt |")
        else:
            md.append(f"| {label} | {ov:.4f} | {nv:.4f} | {nv-ov:+.4f} |")
    md.append("")
    md.append("## 四窗 OOS")
    md.append("")
    md.append("| 窗口 | off 均值Sh | on 均值Sh | dSh | off 最差 | on 最差 | off MDD | on MDD | 正收 off/on |")
    md.append("|---|---|---|---|---|---|---|---|---|")
    for r in oos_rows:
        md.append(f"| {r['window']} | {r['off_mean_sharpe']:+.3f} | "
                  f"{r['on_mean_sharpe']:+.3f} | {r['dsharpe']:+.3f} | "
                  f"{r['off_worst_ret']*100:+.1f}% | {r['on_worst_ret']*100:+.1f}% | "
                  f"{r['off_mean_mdd']*100:.2f}% | {r['on_mean_mdd']*100:.2f}% | "
                  f"{r['off_pos']}/{r['n']} → {r['on_pos']}/{r['n']} |")
    md.append("")
    md.append(f"四窗 dSh 均值 **{oos_mean_dsharpe:+.3f}**，最差 **{oos_worst_dsharpe:+.3f}**。")
    md.append("")
    md.append("## 5 深跌段捕获率（主判据）")
    md.append("")
    md.append("| trough | 回撤 | 宇宙20日 | off捕获 | on捕获 | off滞后 | on滞后 | off成交 | on成交 | 提升 |")
    md.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in seg_rows:
        if r.get("skip"):
            md.append(f"| {r['trough']} | - | - | - | - | - | - | - | - | 跳过:{r.get('note')} |")
            continue
        oc = "-" if r["off_capture"] is None else f"{r['off_capture']:.2f}"
        nc = "-" if r["on_capture"] is None else f"{r['on_capture']:.2f}"
        ol = "None" if r["off_lag"] is None else str(r["off_lag"])
        nl = "None" if r["on_lag"] is None else str(r["on_lag"])
        imp = "✓" if r["improved"] else "✗"
        md.append(f"| {r['trough']} | {r['dd']:.1%} | {r['uni_ret']:+.1%} | "
                  f"{oc} | {nc} | {ol} | {nl} | {r['off_trades']} | {r['on_trades']} | {imp} |")
    md.append("")
    md.append(f"捕获率提升 **{n_improved}/{len(done_segs)}** 段。")
    md.append("")
    md.append("## 过闸判定（事先声明）")
    md.append("")
    md.append("判据：5 段中 >=4 段捕获率提升 且 全样本 MDD 恶化 <=2pt 且 Sharpe 不降。")
    md.append("")
    md.append(f"- >=4 段捕获率提升：{n_improved}/5 {'✓ 达标' if gate1 else '✗ 未达标'}")
    md.append(f"- 全样本 MDD 恶化 <=2pt：{mdd_worsen:+.2f}pt {'✓ 达标' if gate2 else '✗ 未达标'}")
    md.append(f"- Sharpe 不降：{r_off['sharpe']:.4f}→{r_on['sharpe']:.4f} "
              f"({'✓ 达标' if gate3 else '✗ 未达标'})")
    md.append("")
    md.append(f"**结论：{verdict}。**" + ("达标，建议可进下一轮 re-baseline。"
              if passed else "未达标，应放弃 regime 恢复快通道，回滚到纯生产闸门。"))
    md.append("")
    md.append(f"总耗时 {time.time()-t0:.0f}s。")
    OUT_MD.write_text("\n".join(md), encoding="utf-8")

    json_out = {
        "bitwise_unchanged": bitwise_ok,
        "full_sample": {
            "off": {k: r_off[k] for k in fs_keys},
            "on": {k: r_on[k] for k in fs_keys},
        },
        "oos": oos_rows,
        "oos_mean_dsharpe": oos_mean_dsharpe,
        "oos_worst_dsharpe": oos_worst_dsharpe,
        "segments": seg_rows,
        "n_improved": n_improved,
        "gate": {
            "g1_ge4_improved": gate1,
            "g2_mdd_worse_le2pt": gate2,
            "mdd_worsen_pt": mdd_worsen,
            "g3_sharpe_no_decline": gate3,
            "passed": passed,
            "verdict": verdict,
        },
    }
    OUT_JSON.write_text(json.dumps(json_out, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"report -> {OUT_MD}")
    print(f"json   -> {OUT_JSON}")
    print(f"VERDICT: {verdict}")


if __name__ == "__main__":
    main()

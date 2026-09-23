# -*- coding: utf-8 -*-
"""自进化执行器专用 walk-forward 验证器（EVOLVE 环节）。

用途：把「当前生产配置」与候选改动放在**同一批互不重叠的滚动折**上对比，
输出 IS（全样本）+ OOS（多折）双栏证据，供晋升闸门裁决。

两种模式：
  --mode compare  对比候选配置组（candidates()）
  --mode grid     对单个参数做 OOS 网格扫描（找参数高原，拒绝尖峰）

用法：
    python strategy/_evolve_wf.py --mode grid --param reentry_cooldown --values 3,5,8,10,15,20
    python strategy/_evolve_wf.py --mode compare

【2026-09-18 易踩坑】``--values`` 传**负值**列表时必须用等号连写，不能用空格：
    python strategy/_evolve_wf.py --mode grid --param hard_stop_pct --values=-0.12,-0.15,-0.18
  否则 argparse 会把 "-0.12,-0.15,..." 当成另一个选项名，报
  "argument --values: expected one argument" 而让人误以为回测数据出错。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import STOCK_CODES, SECTOR_CONFIG, INDEX_CODES, MARKET_INDEX_CODE  # noqa: E402
from strategy.backtest_daily import BacktestConfig, run_backtest, load_daily  # noqa: E402

FIXED_WARMUP = 130


def wide_universe() -> list:
    s = set(STOCK_CODES)
    for _k, v in SECTOR_CONFIG["sectors"].items():
        for code, _name in v["stocks"]:
            s.add(code)
    return sorted(s)


_CACHE: dict = {}


def preload(codes: list, count: int) -> dict:
    out = {}
    for code in codes:
        key = f"{code}:{count}"
        if key not in _CACHE:
            d = load_daily(code, count)
            if d:
                _CACHE[key] = d
        if key in _CACHE:
            out[code] = _CACHE[key]
    return out


def slice_by_index(data: dict, lo: int, hi: int) -> dict:
    return {c: {k: list(v[lo:hi]) for k, v in d.items()} for c, d in data.items()}


def base_cfg() -> BacktestConfig:
    """当前生产配置（与已验证基线同口径）。

    【2026-09-18 口径修正】原 base_cfg 停留在 momentum_top_n=6 / risk_per_trade=0.02，
    与 config/settings.py 的当前生产值（top_n=3 / risk_per_trade=0.012）不一致，
    会导致「增量 dSh」相对错误基线计算——上一轮 C1_topn3 已落盘，若不修基线，
    本轮 top_n=3 的收益会被重复计入增量。
    现按 P0 = 真实生产配置对齐，后续所有增量一律相对此基线。
    """
    return BacktestConfig(
        use_gate=True, cost_pct=0.0015, vol_sizing=True,
        exit_mode="trend", trend_exit_ma=60, hard_stop_pct=-0.18,
        trend_max_hold_days=120,
        momentum_rank=True, momentum_top_n=3, momentum_lookback=60,
        risk_per_trade=0.012, fixed_amount=300000.0,
        down_day_exit_pct=-9.0, max_positions=5,
        buy_score_threshold=4.0, min_signals=3,
        atr_stop_mult=2.0, tp_atr_mult=4.0,
        min_warmup=FIXED_WARMUP,
    )


def candidates() -> dict:
    b = base_cfg()
    out = {"P0_当前生产基线": b}
    # ---- 第 2 轮：网格扫描筛出的正向维度 ----
    out["C1_topn3"] = replace(b, momentum_top_n=3)
    out["C2_topn2"] = replace(b, momentum_top_n=2)
    out["C3_topn4"] = replace(b, momentum_top_n=4)
    out["C4_topn3_maxpos3"] = replace(b, momentum_top_n=3, max_positions=3)
    out["C5_topn3_cd3"] = replace(b, momentum_top_n=3, reentry_cooldown=3)
    out["C6_topn3_cd5"] = replace(b, momentum_top_n=3, reentry_cooldown=5)
    out["C7_maxpos3"] = replace(b, max_positions=3)
    out["C8_topn3_hs12"] = replace(b, momentum_top_n=3, hard_stop_pct=-0.12)
    return out


def candidates_final() -> dict:
    """第 3 轮：在已确认的 momentum_top_n=3 主效应上做最小增量。"""
    b = base_cfg()
    out = {"P0_当前生产基线": b}
    out["D1_topn3"] = replace(b, momentum_top_n=3)
    out["D2_topn3_cd5"] = replace(b, momentum_top_n=3, reentry_cooldown=5)
    out["D3_topn3_cd5_hs12"] = replace(
        b, momentum_top_n=3, reentry_cooldown=5, hard_stop_pct=-0.12)
    out["D4_topn3_hs12"] = replace(b, momentum_top_n=3, hard_stop_pct=-0.12)
    out["D5_topn3_cd8"] = replace(b, momentum_top_n=3, reentry_cooldown=8)
    out["D6_topn3_cd5_hs15"] = replace(
        b, momentum_top_n=3, reentry_cooldown=5, hard_stop_pct=-0.15)
    # ---- 2026-09-21 AM：单日暴跌清仓阈值（生产当前 -99.0=关闭，基线 -9.0）----
    # 90x6 网格里 -5.0 达 +0.121 门槛，但邻居 -5.5/+0.005、-4.5/+0.063 均未达标
    # → 疑似尖峰。此处纳入 4 窗口共识做终局裁决（防窗口运气）。
    out["E1_dd5"] = replace(b, down_day_exit_pct=-5.0)
    out["E2_dd6"] = replace(b, down_day_exit_pct=-6.0)
    # ---- 2026-09-22 PM-EVOLVE：日线偏置闸门 min_daily_bias ----
    # 实盘入场闸门是 ``trend_up or bias >= min_daily_bias``（trend_strategy.py:151，
    # 生产 0.2），而回测历史只有 trend_up 一路（等价于 2.0=关闭）。
    # F1 = 回测基线口径（关闭 bias 通道）；F2 = **当前生产口径**（0.2，放行 bias>=0.3）。
    # 若 F2 在四窗口一致为负，则生产应改为 2.0 与已验证口径对齐。
    out["F1_bias关闭_2.0"] = replace(b, min_daily_bias=2.0)
    out["F2_生产口径_0.2"] = replace(b, min_daily_bias=0.2)
    out["F3_bias_-0.3"] = replace(b, min_daily_bias=-0.3)
    # ---- 2026-09-23 AM-EVOLVE：轮动「日内突破绕过日线闸门」代理 ----
    # 实盘 _maybe_rotate 在 is_breakout=True 时无视 daily-gate 的 HOLD 直接买入
    # （event_engine.py:674-675 / 706-707），回测器无 rotation 逻辑 ⇒ 口径背离。
    # G1 = 只豁免日线闸门（保留评分门槛）；G2 = 连同评分门槛一起豁免
    # （完整复现实盘轮动语义）；G3 = 更严的突破阈值做邻居对照。
    # 若 G* 在四窗口一致为负，则「关闭轮动的闸门旁路」即为正向改动。
    out["G1_轮动绕闸门_1.5"] = replace(b, breakout_bypass_gate=1.5)
    out["G2_轮动绕闸门绕评分_1.5"] = replace(
        b, breakout_bypass_gate=1.5, breakout_bypass_score=True)
    out["G3_轮动绕闸门_3.0"] = replace(b, breakout_bypass_gate=3.0)
    return out


# ============================================================ 通用评估

def make_folds(data, codes, n, fold, nfolds):
    starts = list(range(FIXED_WARMUP, n - fold + 1, fold))[-nfolds:]
    subs = []
    for s in starts:
        lo, hi = s - FIXED_WARMUP, min(s + fold, n)
        subs.append((s, min(s + fold, n), slice_by_index(data, lo, hi)))
    return subs


def oos_stats(cfg, subs, bt):
    rs = [bt(cfg, d) for _s, _e, d in subs]
    ok = [r for r in rs if r and "error" not in r]
    if not ok:
        return None, rs
    shs = [r["sharpe"] for r in ok]
    return dict(
        mean_sharpe=sum(shs) / len(shs),
        mean_ret=sum(r["total_return"] for r in ok) / len(ok),
        mean_mdd=sum(r["max_drawdown"] for r in ok) / len(ok),
        pos=sum(1 for r in ok if r["total_return"] > 0),
        n=len(ok),
        worst=min(r["total_return"] for r in ok),
        rets=[r["total_return"] for r in ok],
    ), rs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=900)
    ap.add_argument("--fold", type=int, default=90)
    ap.add_argument("--folds", type=int, default=6)
    ap.add_argument("--mode", default="grid", choices=["grid", "compare", "consensus"])
    ap.add_argument("--param", default="reentry_cooldown")
    ap.add_argument("--values", default="3,5,8,10,15,20")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    codes = wide_universe()
    t0 = time.time()
    data = preload(codes + [MARKET_INDEX_CODE, "000300.SH"], args.count)
    if not data:
        print("无数据，退出")
        return
    n = min(len(d["close"]) for d in data.values())
    d0 = data[codes[0]]["date"]
    print(f"[数据] {len(data)} 只 × {n} 根  {d0[0]} -> {d0[-1]}  载入 {time.time()-t0:.1f}s")

    def bt(cfg, dset):
        ks = [k for k in dset.keys() if k not in INDEX_CODES]
        return run_backtest(ks, cfg, count=args.count, preloaded=dset)

    subs = make_folds(data, codes, n, args.fold, args.folds)
    print(f"[折] {len(subs)} 折 × {args.fold} 根："
          + " ".join(f"F{i+1}:{d0[s][:6]}-{d0[e-1][:6]}" for i, (s, e, _d) in enumerate(subs)))

    base = base_cfg()
    base_is = bt(base, data)
    b_stats, base_rs = oos_stats(base, subs, bt)
    print(f"\n基线 P0: IS ret={base_is['total_return']*100:+.2f}% Sh={base_is['sharpe']:+.2f} "
          f"MDD={base_is['max_drawdown']*100:.2f}% | OOS 均值Sh={b_stats['mean_sharpe']:+.3f} "
          f"正收={b_stats['pos']}/{b_stats['n']} 均值MDD={b_stats['mean_mdd']*100:.2f}% "
          f"最差={b_stats['worst']*100:+.1f}%")

    # ---------------- grid ----------------
    if args.mode == "grid":
        raw = args.values.split(",")
        values = []
        for v in raw:
            v = v.strip()
            try:
                values.append(int(v) if v.lstrip("-").isdigit() else float(v))
            except ValueError:
                values.append(v)
        cur = getattr(base, args.param, "?")
        print(f"\n{'=' * 108}")
        print(f"OOS 网格扫描：{args.param}（当前={cur}）— 找参数高原，拒绝尖峰")
        print("=" * 108)
        print(f"{'值':<10}{'OOS均值Sh':>10}{'dSh':>8}{'均值ret':>10}{'均值MDD':>9}"
              f"{'正收':>7}{'最差折':>9}{'累计差pt':>10}  IS_ret    IS_Sh   IS_MDD")
        rows = []
        for v in values:
            cfg = replace(base, **{args.param: v})
            st, rs = oos_stats(cfg, subs, bt)
            if not st:
                print(f"{str(v):<10} ERROR")
                continue
            isr = bt(cfg, data)
            dsh = st["mean_sharpe"] - b_stats["mean_sharpe"]
            tot = sum((rs[k]["total_return"] - base_rs[k]["total_return"]) * 100
                      for k in range(len(rs))
                      if "error" not in rs[k] and "error" not in base_rs[k])
            mk = "  <=当前" if v == cur else ""
            print(f"{str(v):<10}{st['mean_sharpe']:>+10.3f}{dsh:>+8.3f}"
                  f"{st['mean_ret']*100:>+9.1f}%{st['mean_mdd']*100:>8.2f}%"
                  f"{st['pos']:>4}/{st['n']}{st['worst']*100:>+8.1f}%{tot:>+9.1f}  "
                  f"{isr['total_return']*100:>+7.1f}%{isr['sharpe']:>+8.2f}"
                  f"{isr['max_drawdown']*100:>8.2f}%{mk}")
            rows.append(dict(value=v, oos_sharpe=st["mean_sharpe"], d_sharpe=dsh,
                             oos_ret=st["mean_ret"], oos_mdd=st["mean_mdd"],
                             pos=st["pos"], n=st["n"], worst=st["worst"],
                             cum_diff_pt=tot, is_ret=isr["total_return"],
                             is_sharpe=isr["sharpe"], is_mdd=isr["max_drawdown"]))
        best = max(rows, key=lambda r: r["oos_sharpe"]) if rows else None
        if best:
            print(f"\n  -> OOS 最优值 = {best['value']}  dSharpe={best['d_sharpe']:+.3f} "
                  f"累计差={best['cum_diff_pt']:+.1f}pt  "
                  f"{'【达到 +0.10 闸门】' if best['d_sharpe'] >= 0.10 else '【未达 +0.10 闸门】'}")
        if args.json:
            Path(args.json).write_text(json.dumps(
                dict(param=args.param, base=dict(
                    oos_sharpe=b_stats["mean_sharpe"], oos_ret=b_stats["mean_ret"],
                    oos_mdd=b_stats["mean_mdd"], pos=b_stats["pos"], n=b_stats["n"],
                    worst=b_stats["worst"], is_ret=base_is["total_return"],
                    is_sharpe=base_is["sharpe"], is_mdd=base_is["max_drawdown"]),
                    rows=rows), ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[保存] {args.json}")
        return

    # ---------------- consensus（多窗口共识，防窗口运气）----------------
    if args.mode == "consensus":
        WINDOWS = [(60, 9), (75, 7), (90, 6), (120, 4)]
        tests = candidates_final()
        print(f"\n{'=' * 116}")
        print("多窗口共识：同一候选在 4 套互不重叠的折划分下重跑，"
              "看最差窗口而非平均（防窗口运气）")
        print("=" * 116)
        is_res = {name: bt(cfg, data) for name, cfg in tests.items()}
        bi = is_res["P0_当前生产基线"]
        print(f"基线 P0 IS: ret={bi['total_return']*100:+.2f}% Sh={bi['sharpe']:+.2f} "
              f"MDD={bi['max_drawdown']*100:.2f}%")
        res = {name: [] for name in tests}
        for fold, nf in WINDOWS:
            subs_w = make_folds(data, codes, n, fold, nf)
            b_st, b_rs = oos_stats(base_cfg(), subs_w, bt)
            for name, cfg in tests.items():
                st, rs = oos_stats(cfg, subs_w, bt)
                tot = sum((rs[k]["total_return"] - b_rs[k]["total_return"]) * 100
                          for k in range(len(rs))
                          if "error" not in rs[k] and "error" not in b_rs[k])
                res[name].append(dict(win=f"{fold}x{nf}",
                                      d_sh=st["mean_sharpe"] - b_st["mean_sharpe"],
                                      oos_sh=st["mean_sharpe"],
                                      mdd=st["mean_mdd"], worst=st["worst"],
                                      pos=st["pos"], n=st["n"],
                                      cum=tot, base_sh=b_st["mean_sharpe"]))
        print(f"\n{'配置':<22}" + "".join(f"{w[0]}x{w[1]:<2}".rjust(11) for w in WINDOWS)
              + f"{'最差dSh':>10}{'均值dSh':>10}{'最差MDD':>10}{'最差折':>10}{'IS_ret':>10}{'IS_Sh':>8}  结论")
        final = []
        for name in tests:
            if name == "P0_当前生产基线":
                continue
            rs_ = res[name]
            line = f"{name:<22}"
            for r in rs_:
                line += f"{r['d_sh']:>+10.3f} "
            mn = min(r["d_sh"] for r in rs_)
            avg = sum(r["d_sh"] for r in rs_) / len(rs_)
            # ③ OOS 与 IS 的 Sharpe 差距：必须用**绝对值**比较，
            #    不是拿「相对基线的增量 dSh」去比 IS Sharpe。
            mean_oos_sh = sum(r["oos_sh"] for r in rs_) / len(rs_)
            wmdd = max(r["mdd"] for r in rs_)          # 负得最多
            wf = min(r["worst"] for r in rs_)
            isr = is_res[name]
            g1 = (isr["total_return"] > bi["total_return"]) or (isr["sharpe"] > bi["sharpe"])
            g2 = mn >= 0.10
            g4 = wmdd >= -0.22
            g5 = wf > -0.15
            gap = (abs(mean_oos_sh - isr["sharpe"]) / abs(isr["sharpe"])
                   if isr["sharpe"] else 9.99)
            g3 = gap <= 0.25
            ok = g1 and g2 and g3 and g4 and g5
            line += (f"{mn:>+10.3f}{avg:>+10.3f}{wmdd*100:>9.2f}%{wf*100:>+9.1f}%"
                     f"{isr['total_return']*100:>+9.1f}%{isr['sharpe']:>+8.2f}"
                     f"  {'>>> 通过' if ok else '否决'}")
            print(line)
            print(f"{'':<22} ③OOS/IS: 均值OOS_Sh={mean_oos_sh:+.3f} vs IS_Sh={isr['sharpe']:+.2f} "
                  f"→ 差 {gap*100:.1f}% {'PASS' if g3 else 'FAIL'}"
                  f" | ①IS↑={'PASS' if g1 else 'fail'} ②最差dSh≥+.10={'PASS' if g2 else 'fail'}"
                  f" ④最差MDD≥-22%={'PASS' if g4 else 'FAIL'} ⑤最差折>-15%={'PASS' if g5 else 'FAIL'}")
            final.append(dict(name=name, per_window=rs_, min_d_sh=mn, mean_d_sh=avg,
                              mean_oos_sharpe=mean_oos_sh,
                              worst_mdd=wmdd, worst_fold=wf, is_ret=isr["total_return"],
                              is_sharpe=isr["sharpe"], is_mdd=isr["max_drawdown"],
                              g1=g1, g2=g2, g3=g3, g4=g4, g5=g5, gap=gap, pass_gate=ok))
        if args.json:
            Path(args.json).write_text(json.dumps(
                dict(window=f"{d0[0]}-{d0[-1]}", n_bars=n,
                     base=dict(is_ret=bi["total_return"], is_sharpe=bi["sharpe"],
                               is_mdd=bi["max_drawdown"]), final=final),
                ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n[保存] {args.json}")
        return

    # ---------------- compare ----------------
    tests = candidates()
    print(f"\n{'=' * 124}\n【样本内 IS】全样本（含成本）\n{'=' * 124}")
    print(f"{'配置':<24}{'IS收益%':>10}{'IS_Sh':>8}{'IS_MDD%':>9}{'成交':>7}"
          f"{'胜率%':>8}{'持仓d':>7}{'暴露%':>8}{'alpha_pt':>10}")
    is_res = {}
    for name, cfg in tests.items():
        r = bt(cfg, data)
        is_res[name] = r
        if "error" in r:
            print(f"{name:<24} ERROR {r['error']}")
            continue
        print(f"{name:<24}{r['total_return']*100:>+9.2f}%{r['sharpe']:>+8.2f}"
              f"{r['max_drawdown']*100:>8.2f}%{r['n_trades']:>6}"
              f"{r['win_rate']*100:>7.1f} {r['avg_hold']:>6.1f}"
              f"{r['exposure']*100:>7.1f} {r['alpha']*100:>+9.2f}")

    print(f"\n{'=' * 124}\n【样本外 OOS】{len(subs)} 折逐折明细\n{'=' * 124}")
    print(f"{'配置':<24}" + "".join(f"{'F'+str(i+1):>10}" for i in range(len(subs)))
          + f"{'均值Sh':>9}{'正收':>7}{'均值MDD':>9}{'最差折':>9}{'均值ret':>9}")
    print("-" * 124)
    fold_res = {name: [bt(cfg, d) for _s, _e, d in subs] for name, cfg in tests.items()}
    base_rs = fold_res["P0_当前生产基线"]
    print(f"{'[基准]等权买入持有':<24}"
          + "".join(f"{r['bench_return']*100:>+9.1f}%" for r in base_rs))
    summary = []
    for name in tests:
        rs = fold_res[name]
        line = f"{name:<24}"
        shs, mdds, rets, pos, worst = [], [], [], 0, 999.0
        for r in rs:
            if "error" in r:
                line += f"{'ERR':>10}"
                continue
            line += f"{r['total_return']*100:>+9.1f}%"
            shs.append(r["sharpe"]); mdds.append(r["max_drawdown"])
            rets.append(r["total_return"])
            if r["total_return"] > 0:
                pos += 1
            worst = min(worst, r["total_return"])
        msh = sum(shs)/len(shs) if shs else 0.0
        mdd = sum(mdds)/len(mdds) if mdds else 0.0
        mret = sum(rets)/len(rets) if rets else 0.0
        line += f"{msh:>+9.3f}{pos:>4}/{len(rs)}{mdd*100:>8.2f}%{worst*100:>+8.1f}%{mret*100:>+8.1f}%"
        print(line)
        summary.append(dict(name=name, mean_sharpe=msh, pos=pos, n=len(rs),
                            mean_mdd=mdd, worst=worst, mean_ret=mret))

    print(f"\n{'=' * 124}\n逐折 vs 基线 P0（单位 pt）\n{'=' * 124}")
    print(f"{'配置':<24}" + "".join(f"{'F'+str(i+1):>10}" for i in range(len(subs)))
          + f"{'胜':>7}{'累计差':>10}")
    for s in summary:
        name = s["name"]
        if name == "P0_当前生产基线":
            continue
        rs = fold_res[name]
        line = f"{name:<24}"
        wins, tot = 0, 0.0
        for k, r in enumerate(rs):
            if "error" in r or "error" in base_rs[k]:
                line += f"{'-':>10}"
                continue
            d = (r["total_return"] - base_rs[k]["total_return"]) * 100
            tot += d
            if d > 0:
                wins += 1
            line += f"{d:>+9.1f}"
        line += f"{wins:>4}/{len(rs)}{tot:>+9.1f}pt"
        print(line)
        s["wins"] = wins
        s["cum_diff_pt"] = tot

    print(f"\n{'=' * 124}\n晋升闸门裁决\n{'=' * 124}")
    bs = next(s for s in summary if s["name"] == "P0_当前生产基线")
    bi = is_res["P0_当前生产基线"]
    print(f"基线 P0: IS ret={bi['total_return']*100:+.2f}% Sh={bi['sharpe']:+.2f} "
          f"MDD={bi['max_drawdown']*100:.2f}% | OOS 均值Sh={bs['mean_sharpe']:+.3f} "
          f"正收={bs['pos']}/{bs['n']} 均值MDD={bs['mean_mdd']*100:.2f}% "
          f"最差={bs['worst']*100:+.1f}%")
    print(f"{'配置':<24}{'dOOS_Sh':>9}{'②≥+.10':>9}{'③IS/OOS':>9}"
          f"{'④MDD≥-22':>10}{'⑤最差>-15':>10}{'①IS↑':>7}  结论")
    for s in summary:
        if s["name"] == "P0_当前生产基线":
            continue
        isr = is_res[s["name"]]
        d_sh = s["mean_sharpe"] - bs["mean_sharpe"]
        g2 = d_sh >= 0.10
        g4 = s["mean_mdd"] >= -0.22
        g5 = s["worst"] > -0.15
        g1 = (isr["total_return"] > bi["total_return"]) or (isr["sharpe"] > bi["sharpe"])
        is_sh, oos_sh = isr["sharpe"], s["mean_sharpe"]
        gap = abs(oos_sh - is_sh) / abs(is_sh) if is_sh else 9.99
        g3 = gap <= 0.25
        ok = g1 and g2 and g3 and g4 and g5
        print(f"{s['name']:<24}{d_sh:>+9.3f}{'PASS' if g2 else 'fail':>9}"
              f"{gap*100:>8.1f}%{'PASS' if g4 else 'FAIL':>10}"
              f"{'PASS' if g5 else 'FAIL':>10}{'PASS' if g1 else 'fail':>7}"
              f"  {'>>> 通过' if ok else '否决'}")
        s.update(dict(d_oos_sharpe=d_sh, g1=g1, g2=g2, g3=g3, g4=g4, g5=g5,
                      is_sharpe=is_sh, is_ret=isr["total_return"],
                      is_mdd=isr["max_drawdown"], gap=gap, pass_gate=ok))

    if args.json:
        Path(args.json).write_text(json.dumps(
            dict(window=f"{d0[0]}-{d0[-1]}", n_bars=n, fold=args.fold,
                 folds=len(subs), base=dict(is_ret=bi["total_return"],
                                            is_sharpe=bi["sharpe"],
                                            is_mdd=bi["max_drawdown"],
                                            oos_sharpe=bs["mean_sharpe"],
                                            oos_mdd=bs["mean_mdd"],
                                            oos_worst=bs["worst"],
                                            oos_pos=bs["pos"], oos_n=bs["n"]),
                 summary=summary), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[保存] {args.json}")


if __name__ == "__main__":
    main()

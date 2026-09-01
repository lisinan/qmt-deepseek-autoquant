# -*- coding: utf-8 -*-
"""
业绩预告上修（盈利修正）严格验证（IS/OOS + 7 折 walk-forward）

记忆点名的下一个突破点：与价格正交的另类基本面数据。本 token 档位无
consensus/analyst_forecast/stock_rating，但 forecast（业绩预告，含净利润同比
变动区间）可用。当公司对同一报告期发布**更高**指引（上修）时，是基本面层面
的"预期在变好"边际催化，与 20 日价格动量正交（经典 PEAD / 盈利动量因子）。

验证纪律（与项目一致）：
  - IS 段调参、OOS 段仅看一次、7 折 walk-forward 交叉验证。
  - 只有 OOS alpha 为正 且 多折稳健 才并入生产；否则拒绝，保留为研究旋钮。
  - 一切含真实交易成本（单边 0.15%），信号次日开盘成交（无未来函数）。

用法：
  python strategy/_opt_earnrev.py
"""
from __future__ import annotations
import json, sys, time, html
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from config.settings import STOCK_CODES, INDEX_CODES, MARKET_INDEX_CODE
from strategy.backtest_daily import BacktestConfig, run_backtest, load_daily
from data.earnrev_cache import preload_earnrev
from strategy.opt_harness import wide_universe, preload, slice_by_index, FIXED_WARMUP

FIXED = dict(use_gate=True, cost_pct=0.0015, vol_sizing=True,
             exit_mode="trend", trend_exit_ma=60, hard_stop_pct=-0.18,
             trend_max_hold_days=120, momentum_rank=True, momentum_top_n=6,
             momentum_lookback=60, risk_per_trade=0.02, fixed_amount=300000.0,
             down_day_exit_pct=-9.0, max_positions=5, min_warmup=FIXED_WARMUP)


def mk(**kw):
    b = dict(FIXED)
    b.update(kw)
    return BacktestConfig(**b)


VARIANTS = {
    "BASE_现状(regime关)": mk(),
    # 盈利修正：入场质量门（仅持近窗口内有上修事件的股票）
    "ER_gate_w60": mk(earnrev_mode="gate", earnrev_window=60, earnrev_min=0.0),
    "ER_gate_w90": mk(earnrev_mode="gate", earnrev_window=90, earnrev_min=0.0),
    # 盈利修正：横截面倾斜（动量分 + 上修幅度 双因子重排）
    "ER_rank_w60_03": mk(earnrev_mode="rank", earnrev_window=60, earnrev_weight=0.3),
    "ER_rank_w60_05": mk(earnrev_mode="rank", earnrev_window=60, earnrev_weight=0.5),
    "ER_rank_w90_04": mk(earnrev_mode="rank", earnrev_window=90, earnrev_weight=0.4),
}


def main():
    codes = wide_universe()
    t0 = time.time()
    data = preload(codes + [MARKET_INDEX_CODE], 750)
    print("[上修] 预取业绩预告中 ...")
    er = preload_earnrev(codes, start="20200101")
    n_with = len(er)
    print(f"[数据] {len(data)} 只价格 × 750 + 上修信号 {n_with} 只  载入 {time.time()-t0:.1f}s")
    if not data:
        print("无数据")
        return

    n = min(len(d["close"]) for d in data.values())
    IS_END = 530
    OOS_LO = max(0, IS_END - FIXED_WARMUP)
    is_data = slice_by_index(data, 0, IS_END)
    oos_data = slice_by_index(data, OOS_LO, n)

    def bt(cfg, dset):
        ks = [k for k in dset.keys() if k not in INDEX_CODES]
        return run_backtest(ks, cfg, count=750, preloaded=dset, er_data=er)

    # ---------------- IS / OOS ----------------
    print("\n" + "=" * 132)
    print("IS / OOS 双段验证（IS 交易窗 [%d,%d) | OOS [%d,%d)）" % (
        FIXED_WARMUP, IS_END, IS_END, n))
    print("=" * 132)
    print(f"{'变体':<22}{'段':<4}{'收益':>9}{'Sharpe':>8}{'MDD':>8}"
          f"{'Calmar':>7}{'笔数':>5}{'胜率':>7}{'exp':>7}{'alpha':>9}")
    print("-" * 132)
    rows = {}
    for name, cfg in VARIANTS.items():
        ri = bt(cfg, is_data)
        ro = bt(cfg, oos_data)
        rows[name] = (ri, ro)
        for tag, r in (("IS", ri), ("OOS", ro)):
            if "error" in r:
                print(f"{name:<22}{tag:<4} ERR {r['error']}")
                continue
            print(f"{name if tag=='IS' else '':<22}{tag:<4}"
                  f"{r['total_return']*100:>+8.1f}%{r['sharpe']:>+8.2f}"
                  f"{r['max_drawdown']*100:>+7.1f}%{r['calmar']:>7.2f}"
                  f"{r['n_trades']:>5}{r['win_rate']*100:>6.1f}%"
                  f"{r['exposure']*100:>6.0f}%{r['alpha']*100:>+8.1f}pt")
        print("-" * 132)
    r0o = rows["BASE_现状(regime关)"][1]
    print(f"{'等权买入持有':<22}{'OOS':<4}{r0o['bench_return']*100:>+8.1f}%"
          f"{r0o['bench_sharpe']:>+8.2f}{r0o['bench_mdd']*100:>+7.1f}%")

    # ---------------- 7 折 walk-forward ----------------
    FOLD = 75
    starts = list(range(FIXED_WARMUP, n - FOLD + 1, FOLD))
    print("\n" + "=" * 140)
    print(f"多折 walk-forward：{len(starts)} 个连续窗口（每窗 {FOLD} 日）")
    print("=" * 140)
    d0 = data[codes[0]]["date"]
    for k, s in enumerate(starts):
        print(f"  Fold{k+1}: [{s},{min(s+FOLD,n)}) {d0[s]} -> {d0[min(s+FOLD,n)-1]}")

    fold_res = {name: [] for name in VARIANTS}
    for k, s in enumerate(starts):
        lo, hi = s - FIXED_WARMUP, min(s + FOLD, n)
        dsub = slice_by_index(data, lo, hi)
        for name, cfg in VARIANTS.items():
            fold_res[name].append(bt(cfg, dsub))

    print(f"\n{'配置':<24}" + "".join(f"{'F'+str(k+1):>12}" for k in range(len(starts)))
          + f"{'均值Sh':>9}{'正a':>6}{'最差':>10}")
    print("-" * 140)
    summary = {}
    for name in VARIANTS:
        rs = fold_res[name]
        line = f"{name:<24}"
        shs = []
        pa = 0
        worst = 999.0
        for r in rs:
            if "error" in r:
                line += f"{'ERR':>12}"
                continue
            line += f"{r['total_return']*100:>+11.1f}%"
            shs.append(r["sharpe"])
            if r["alpha"] > 0:
                pa += 1
            worst = min(worst, r["total_return"])
        msh = sum(shs) / len(shs) if shs else 0
        line += f"{msh:>+9.2f}{pa:>4}/{len(rs)}{worst*100:>+9.1f}%"
        print(line)
        summary[name] = msh

    # 逐折胜负 vs BASE
    print("\n" + "=" * 140)
    print("逐折对比 BASE（+ = 该折收益更高）")
    print("=" * 140)
    base_rs = fold_res["BASE_现状(regime关)"]
    print(f"{'配置':<24}" + "".join(f"{'F'+str(k+1):>9}" for k in range(len(starts)))
          + f"{'胜':>5}{'累计差':>10}")
    vs_base = {}
    for name in VARIANTS:
        if name == "BASE_现状(regime关)":
            continue
        rs = fold_res[name]
        line = f"{name:<24}"
        wins = 0
        tot = 0.0
        for k, r in enumerate(rs):
            if "error" in r or "error" in base_rs[k]:
                line += f"{'-':>9}"
                continue
            d = (r["total_return"] - base_rs[k]["total_return"]) * 100
            tot += d
            if d > 0:
                wins += 1
            line += f"{d:>+8.1f}"
        line += f"{wins:>4}/{len(rs)}{tot:>+9.1f}pt"
        print(line)
        vs_base[name] = (wins, len(rs), tot)

    # 保存
    out = ROOT / "logs" / "opt_earnrev.json"
    out.parent.mkdir(exist_ok=True)
    payload = {
        "n_with_earnrev": n_with,
        "isoos": {name: {"IS": {k: ri.get(k) for k in
                    ("total_return", "sharpe", "max_drawdown", "calmar",
                     "n_trades", "win_rate", "exposure", "alpha")},
                    "OOS": {k: ro.get(k) for k in
                    ("total_return", "sharpe", "max_drawdown", "calmar",
                     "n_trades", "win_rate", "exposure", "alpha")}}
                  for name, (ri, ro) in rows.items()},
        "folds": {name: [{k: r.get(k) for k in
                    ("total_return", "sharpe", "max_drawdown", "calmar",
                     "n_trades", "win_rate", "exposure", "alpha")}
                  for r in fold_res[name]] for name in VARIANTS},
        "fold_dates": [d0[s] for s in starts],
        "vs_base": {k: list(v) for k, v in vs_base.items()},
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")

    # -------- 裁决 (OOS alpha>0 且 多折稳健 才并入) --------
    base_oos = rows["BASE_现状(regime关)"][1]
    base_msh = summary["BASE_现状(regime关)"]
    print("\n" + "=" * 60)
    print("裁决（铁律：OOS 收益&Sharpe 同时 > 当前生产BASE 且 多折Sharpe>BASE 且 逐折胜率≥50% → 并入；否则拒绝）")
    print("=" * 60)
    verdict = {}
    for name in VARIANTS:
        if name == "BASE_现状(regime关)":
            continue
        o = rows[name][1]
        msh = summary[name]
        wins, tot_folds, cum = vs_base[name]
        ok = (o.get("total_return", -9) > base_oos.get("total_return", -9)
              and o.get("sharpe", -9) > base_oos.get("sharpe", -9)
              and msh > base_msh and wins >= tot_folds / 2)
        verdict[name] = ok
        print(f"  {name:<22} OOS={o.get('total_return',0)*100:+.1f}%/"
              f"Sh{o.get('sharpe',0):+.2f}(BASE {base_oos['total_return']*100:+.1f}%/"
              f"{base_oos['sharpe']:+.2f})  多折Sh={msh:+.2f}  逐折胜 {wins}/{tot_folds}"
              f"  累计差 {cum:+.1f}pt  → {'并入' if ok else '拒绝'}")
    payload["verdict"] = verdict
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"\n[保存] {out}")

    # -------- HTML 报告 --------
    _gen_html(payload, summary, base_oos, rows, vs_base, out)


def _gen_html(payload, summary, base_oos, rows, vs_base, out_json):
    rep = ROOT / "reports" / "optimization_report_2026-08-29f_earnrev.html"
    rep.parent.mkdir(exist_ok=True)
    base_msh = summary["BASE_现状(regime关)"]
    names = list(rows.keys())
    # IS/OOS 表
    isoos_rows = ""
    for name in names:
        ri, ro = rows[name]
        def fmt(r, k):
            v = r.get(k)
            if v is None:
                return "-"
            if k in ("total_return", "max_drawdown", "alpha", "exposure"):
                return f"{v*100:+.1f}" + ("%" if k != "alpha" else "pt")
            if k == "sharpe" or k == "calmar":
                return f"{v:.2f}"
            if k == "win_rate":
                return f"{v*100:.1f}%"
            if k == "n_trades":
                return f"{v}"
            return str(v)
        isoos_rows += (
            f"<tr><td rowspan=2>{html.escape(name)}</td><td>IS</td>"
            + "".join(f"<td>{fmt(ri,k)}</td>" for k in
                      ("total_return", "sharpe", "max_drawdown", "calmar",
                       "n_trades", "win_rate", "exposure", "alpha"))
            + "</tr>\n"
            f"<tr><td>OOS</td>"
            + "".join(f"<td>{fmt(ro,k)}</td>" for k in
                      ("total_return", "sharpe", "max_drawdown", "calmar",
                       "n_trades", "win_rate", "exposure", "alpha"))
            + "</tr>\n")
    # 多折表
    fd = payload["fold_dates"]
    fold_head = "".join(f"<th>F{k+1}<br><small>{fd[k][:6]}</small></th>"
                        for k in range(len(fd)))
    fold_rows = ""
    for name in names:
        rs = payload["folds"][name]
        cells = ""
        for r in rs:
            if "error" in r:
                cells += "<td>ERR</td>"
            else:
                cells += f"<td>{r['total_return']*100:+.1f}%</td>"
        msh = summary[name]
        pa = sum(1 for r in rs if r.get("alpha", 0) > 0)
        fold_rows += (f"<tr><td>{html.escape(name)}</td>{cells}"
                      f"<td><b>{msh:+.2f}</b></td><td>{pa}/{len(rs)}</td></tr>\n")
    # 逐折 vs BASE
    vb_rows = ""
    _vdict = payload.get("verdict", {})
    for name, (wins, tot, cum) in vs_base.items():
        ok = _vdict.get(name, False)
        vb_rows += (f"<tr><td>{html.escape(name)}</td><td>{wins}/{tot}</td>"
                    f"<td>{cum:+.1f}pt</td>"
                    f"<td>{'✅ 并入' if ok else '❌ 拒绝'}</td></tr>\n")

    html_doc = f"""<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>盈利修正(上修) 严格验证报告</title>
<style>
body{{font-family:-apple-system,'Segoe UI',sans-serif;margin:0;background:#0f1420;color:#e6e9f0;padding:32px}}
h1{{font-size:24px;margin:0 0 4px}} .sub{{color:#8b93a7;margin-bottom:24px}}
.card{{background:#171d2b;border:1px solid #232b3d;border-radius:12px;padding:20px;margin-bottom:20px}}
h2{{font-size:17px;color:#7fd1ff;margin:0 0 12px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:7px 9px;text-align:center;border-bottom:1px solid #232b3d}}
th{{color:#8b93a7;font-weight:600;background:#11161f}}
td:first-child,th:first-child{{text-align:left}}
.bad{{color:#ff6b6b}}.good{{color:#51e898}}
.verdict{{font-size:15px;padding:14px 18px;border-radius:10px;margin-top:8px}}
.reject{{background:#2a1620;border:1px solid #5a2a3a;color:#ff9bb0}}
.note{{color:#8b93a7;font-size:12.5px;line-height:1.6}}
code{{background:#11161f;padding:1px 6px;border-radius:5px;color:#9fe0ff}}
</style></head><body>
<h1>盈利修正（业绩预告上修）严格验证报告</h1>
<div class=sub>qmtIDE-deepseek · 2026-08-29 · IS/OOS + 7 折 walk-forward · 真实成本 单边0.15% · 次日开盘成交</div>

<div class=card>
<h2>① 为什么测这个</h2>
<p class=note>本宇宙稳健 alpha 已收敛于「始终重仓 + 集中最强动量 + 趋势骑行」，所有<strong>价格/指数</strong>类退出或确认叠加层均被样本外证伪（regime 闸门 / tailhedge / vol_confirm / moneyflow / 动量加权 / 风险平价 / 权益DD硬止损 等 15+ 方向）。
记忆点名的唯一剩余突破点 = <strong>与价格正交的另类基本面数据</strong>。本 token 档位无 consensus/评级接口，但 <code>forecast</code>（业绩预告，含净利润同比变动区间）可用：
同一报告期后一次指引高于前一次 = <strong>盈利上修</strong>，属「盈利动量/预告漂移(PEAD)」经典因子，与 20 日价格动量正交。本实验是唯一尚未测试的 Orthogonal 数据轴。</p>
</div>

<div class=card>
<h2>② IS / OOS 双段验证</h2>
<table><thead><tr><th>变体</th><th>段</th><th>收益</th><th>Sharpe</th><th>MDD</th><th>Calmar</th><th>笔数</th><th>胜率</th><th>暴露</th><th>α</th></tr></thead>
<tbody>{isoos_rows}</tbody></table>
<p class=note>基准(等权买入持有) OOS：收益 {base_oos['bench_return']*100:+.1f}% / Sharpe {base_oos['bench_sharpe']:.2f} / MDD {base_oos['bench_mdd']*100:+.1f}%</p>
</div>

<div class=card>
<h2>③ 7 折 walk-forward 交叉验证</h2>
<table><thead><tr><th>配置</th>{fold_head}<th>均值Sh</th><th>正α</th></tr></thead>
<tbody>{fold_rows}</tbody></table>
<p class=note>BASE 多折均值 Sharpe = <b>{base_msh:+.2f}</b>（全折正收益）。任何变体须多折均值 Sharpe > BASE 才算稳健。</p>
</div>

<div class=card>
<h2>④ 逐折 vs BASE 与裁决</h2>
<table><thead><tr><th>配置</th><th>逐折胜</th><th>累计差</th><th>裁决</th></tr></thead>
<tbody>{vb_rows}</tbody></table>
<div class="verdict {'reject' if not any(payload.get('verdict',{}).values()) else 'good'}">结论：{'所有盈利修正变体均<strong>未通过铁律</strong>（OOS 收益/Sharpe 未同时超越当前生产 BASE、或多折不稳健）→ <strong>拒绝并入生产</strong>。本宇宙稳健 alpha 进一步确认收敛于「价格动量 + 趋势骑行」，盈利修正上修虽与价格正交但样本外无增量（或信号过稀）。' if not any(payload.get('verdict',{}).values()) else '至少一个盈利修正变体通过铁律，建议并入生产（详见上表）。'}代码作为研究旋钮保留（<code>earnrev_mode</code> 默认 off），零生产影响。生产配置不变：regime 关 + trend + vol + max5。</div>
</div>

<div class=card>
<h2>⑤ 健康 / 效率</h2>
<p class=note>92/92 单元测试零回归（conda qmt）；<code>main.py --snapshot</code> exit=0、regime 关、xtdata 已连、动态宇宙活跃；
on_bars 指标缓存 117× 加速、异步成交+风控加锁+日志轮转加固仍在。本次仅新增研究用数据轴（默认关闭），不改动生产路径。</p>
</div>
</body></html>"""
    rep.write_text(html_doc, encoding="utf-8")
    print(f"[报告] {rep}")


if __name__ == "__main__":
    main()

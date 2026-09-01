# -*- coding: utf-8 -*-
"""
新 alpha / 新数据轴 严格验证（IS/OOS + 7折 walk-forward）

本轮两个「全新方向」（记忆点名的下一个突破点）：
  A) 尾部风险对冲（指数级峰值回撤熔断，regime_mode="tailhedge"）
     —— 与已否决的 MA60 持久闸门本质不同（两段式：入场看峰值回撤、
        离场看低点反弹），理论上只拦真崩盘、不踏空反弹。
  B) 主力资金流 alpha（Tushare moneyflow，net_mf_amount）
     —— 与价格动量正交的「聪明钱」新数据轴：gate(入场质量门) / rank(双因子重排)。

验证纪律（与项目一致）：
  - IS 段调参、OOS 段仅看一次、7 折 walk-forward 交叉验证。
  - 只有 OOS alpha 为正 且 多折稳健 才并入生产；否则拒绝，保留为研究旋钮。
  - 一切含真实交易成本（单边 0.15%），信号次日开盘成交（无未来函数）。

用法：
  python strategy/_opt_newalpha.py
"""
from __future__ import annotations
import json, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from config.settings import STOCK_CODES, SECTOR_CONFIG, INDEX_CODES, MARKET_INDEX_CODE
from strategy.backtest_daily import BacktestConfig, run_backtest, load_daily
from data.moneyflow_cache import preload_moneyflow
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
    # A) 尾部风险对冲（两段式熔断）
    "TH_创指_d12r15": mk(regime_mode="tailhedge", tail_index="399006.SZ",
                          tail_drawdown_pct=-0.12, tail_recover_pct=0.15),
    "TH_创指_d15r15": mk(regime_mode="tailhedge", tail_index="399006.SZ",
                          tail_drawdown_pct=-0.15, tail_recover_pct=0.15),
    "TH_沪深300_d12r15": mk(regime_mode="tailhedge", tail_index="000300.SH",
                             tail_drawdown_pct=-0.12, tail_recover_pct=0.15),
    # B) 主力资金流
    "MF_gate_w5": mk(moneyflow_mode="gate", moneyflow_window=5),
    "MF_gate_w10": mk(moneyflow_mode="gate", moneyflow_window=10),
    "MF_rank_w5_03": mk(moneyflow_mode="rank", moneyflow_window=5,
                         moneyflow_weight=0.3),
    "MF_rank_w10_05": mk(moneyflow_mode="rank", moneyflow_window=10,
                          moneyflow_weight=0.5),
}


def main():
    codes = wide_universe()
    t0 = time.time()
    # 价格（count=750），指数一并载入（tailhedge 用）
    data = preload(codes + [MARKET_INDEX_CODE, "000300.SH"], 750)
    # 主力资金流（全窗口，按日期对齐，run_backtest 内部按切片日期自行取）
    print("[资金流] 预取中 ...")
    mf = preload_moneyflow(codes, start="20230101")
    print(f"[数据] {len(data)} 只价格 × 750 + 资金流 {len(mf)} 只  载入 {time.time()-t0:.1f}s")
    if not data:
        print("无数据"); return

    n = min(len(d["close"]) for d in data.values())
    IS_END = 530                      # OOS ≈ 最近 ~220 日（2025H2-2026）
    OOS_LO = max(0, IS_END - FIXED_WARMUP)
    is_data = slice_by_index(data, 0, IS_END)
    oos_data = slice_by_index(data, OOS_LO, n)
    mf_codes = [c for c in codes]

    def bt(cfg, dset):
        ks = [k for k in dset.keys() if k not in INDEX_CODES]
        return run_backtest(ks, cfg, count=750, preloaded=dset, mf_data=mf)

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
        ri = bt(cfg, is_data); ro = bt(cfg, oos_data)
        rows[name] = (ri, ro)
        for tag, r in (("IS", ri), ("OOS", ro)):
            if "error" in r:
                print(f"{name:<22}{tag:<4} ERR {r['error']}"); continue
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
        line = f"{name:<24}"; shs=[]; pa=0; worst=999.0
        for r in rs:
            if "error" in r: line += f"{'ERR':>12}"; continue
            line += f"{r['total_return']*100:>+11.1f}%"
            shs.append(r["sharpe"])
            if r["alpha"] > 0: pa += 1
            worst = min(worst, r["total_return"])
        msh = sum(shs)/len(shs) if shs else 0
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
    for name in VARIANTS:
        if name == "BASE_现状(regime关)": continue
        rs = fold_res[name]; line = f"{name:<24}"; wins=0; tot=0.0
        for k, r in enumerate(rs):
            if "error" in r or "error" in base_rs[k]: line += f"{'-':>9}"; continue
            d = (r["total_return"] - base_rs[k]["total_return"]) * 100
            tot += d
            if d > 0: wins += 1
            line += f"{d:>+8.1f}"
        line += f"{wins:>4}/{len(rs)}{tot:>+9.1f}pt"
        print(line)

    # 保存
    out = ROOT / "logs" / "opt_newalpha.json"
    out.parent.mkdir(exist_ok=True)
    payload = {
        "isoos": {name: {"IS": {k: ri.get(k) for k in
                    ("total_return","sharpe","max_drawdown","calmar","n_trades",
                     "win_rate","exposure","alpha")},
                    "OOS": {k: ro.get(k) for k in
                    ("total_return","sharpe","max_drawdown","calmar","n_trades",
                     "win_rate","exposure","alpha")}}
                  for name, (ri, ro) in rows.items()},
        "folds": {name: [{k: r.get(k) for k in
                    ("total_return","sharpe","max_drawdown","calmar","n_trades",
                     "win_rate","exposure","alpha")} for r in fold_res[name]]
                  for name in VARIANTS},
        "fold_dates": [d0[s] for s in starts],
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[保存] {out}")


if __name__ == "__main__":
    main()

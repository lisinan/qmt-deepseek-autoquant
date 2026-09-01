# -*- coding: utf-8 -*-
"""
科学参数优化框架（walk-forward 样本内/外验证）

设计原则（防过拟合）：
  1. **样本内外分离**：IS 段调参，OOS 段验证。OOS 只看一次结论，不回头改参。
  2. **参数高原优于参数尖峰**：报告邻域稳定性，孤立最优点视为噪声。
  3. **扩展标的池**：23 只 AI 产业链股（STOCK_CODES ∪ SECTOR_CONFIG），
     降低单只股票主导结果的风险。
  4. **一切含真实交易成本**（默认单边 0.15%）。
  5. **对比等权买入持有基准**，只有 alpha 为正才算优化成功。

用法：
    python strategy/opt_harness.py --stage sweep
    python strategy/opt_harness.py --stage confirm
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import (  # noqa: E402
    STOCK_CODES, SECTOR_CONFIG, INDEX_CODES, MARKET_INDEX_CODE,
)
from strategy.backtest_daily import (  # noqa: E402
    BacktestConfig, run_backtest, load_daily,
)


# ============================================================ 宇宙 & 数据

def wide_universe() -> List[str]:
    """STOCK_CODES ∪ SECTOR_CONFIG 全部个股（去重排序）。"""
    s = set(STOCK_CODES)
    for _k, v in SECTOR_CONFIG["sectors"].items():
        for code, _name in v["stocks"]:
            s.add(code)
    return sorted(s)


_CACHE: Dict[str, dict] = {}


def preload(codes: List[str], count: int = 500) -> Dict[str, dict]:
    """一次性加载日线，供所有回测复用（避免重复拉数据）。"""
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


def slice_by_index(data: Dict[str, dict], lo: int, hi: int) -> Dict[str, dict]:
    """按索引切片（数据已按日期对齐、等长时安全）。"""
    out = {}
    for code, d in data.items():
        out[code] = {k: list(v[lo:hi]) for k, v in d.items()}
    return out


# ============================================================ 评分

def score_config(r: dict) -> float:
    """综合适应度：兼顾收益、风险调整后收益与回撤。

    不单看 total_return（会选出「满仓单押」的脆弱配置），
    也不单看 Sharpe（会选出「几乎不交易」的空仓配置）。
    """
    if not r or "error" in r:
        return -999.0
    if r.get("n_trades", 0) < 10:      # 样本太少不可信
        return -999.0
    sharpe = r["sharpe"]
    calmar = r["calmar"]
    alpha = r["alpha"]
    # 主项 Sharpe，辅以 Calmar 与超额收益
    return sharpe * 1.0 + min(calmar, 3.0) * 0.30 + alpha * 0.50


def fmt_row(tag: str, r: dict) -> str:
    if not r or "error" in r:
        return f"{tag:<34} ERROR {r.get('error','') if r else ''}"
    return (f"{tag:<34} ret={r['total_return']*100:+7.2f}% "
            f"Sh={r['sharpe']:+5.2f} So={r['sortino']:+5.2f} "
            f"MDD={r['max_drawdown']*100:7.2f}% Cal={r['calmar']:5.2f} "
            f"PF={r['profit_factor']:4.2f} n={r['n_trades']:>3} "
            f"win={r['win_rate']*100:4.1f}% hold={r['avg_hold']:4.1f}d "
            f"exp={r['exposure']*100:4.1f}% a={r['alpha']*100:+7.2f}pt")


# ============================================================ 基础配置

FIXED_WARMUP = 130   # 所有配置统一预热，保证 IS/OOS 交易窗口可比


def base_cfg() -> BacktestConfig:
    """当前生产配置（settings.STRATEGY_PARAMS 对应）。"""
    return BacktestConfig(
        use_gate=True, cost_pct=0.0015, vol_sizing=True,
        exit_mode="trend", trend_exit_ma=60, hard_stop_pct=-0.18,
        trend_max_hold_days=120,
        momentum_rank=True, momentum_top_n=6, momentum_lookback=60,
        risk_per_trade=0.02, fixed_amount=300000.0,
        down_day_exit_pct=-9.0, max_positions=5,  # 2026-08-27 并入：IS/OOS+多折双验证优于8
        buy_score_threshold=4.0, min_signals=3,
        atr_stop_mult=2.0, tp_atr_mult=4.0,
        min_warmup=FIXED_WARMUP,
    )


# 参数网格（逐维扫描 + 关键交叉，避免组合爆炸）
GRID_1D = {
    "trend_exit_ma": [20, 30, 40, 50, 60, 80],
    "momentum_top_n": [3, 4, 5, 6, 8, 10],
    "momentum_lookback": [20, 40, 60, 90, 120],
    "hard_stop_pct": [-0.10, -0.14, -0.18, -0.25],
    "risk_per_trade": [0.01, 0.015, 0.02, 0.03, 0.04],
    "max_positions": [3, 4, 5, 6, 8, 10],
    "buy_score_threshold": [3.0, 3.5, 4.0, 4.5, 5.0],
    "down_day_exit_pct": [-6.0, -9.0, -99.0],
    "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
}


# ============================================================ 候选配置
# 结构性参数才是优化对象。杠杆（risk_per_trade / atr_stop_mult）统一固定，
# 因为在 trend 模式下二者只是同一个「仓位规模」旋钮的正反面：
#     实际仓位 ∝ risk_per_trade / atr_stop_mult
# 把杠杆当成优化目标会让适应度奖励 beta 而非 alpha。

def candidates() -> Dict[str, BacktestConfig]:
    b = base_cfg()
    out: Dict[str, BacktestConfig] = {}

    # 参照组
    out["A_base_当前生产"] = b
    out["Z_scalp_旧范式"] = replace(
        b, exit_mode="scalp", trailing=True, chandelier=True,
        max_hold_days=20, momentum_rank=False, risk_per_trade=0.01,
        fixed_amount=50000.0, hard_stop_pct=-0.99,
    )

    # 单维改动（隔离验证每个 IS 发现）
    out["B_exitma20"] = replace(b, trend_exit_ma=20)
    out["C_maxpos4"] = replace(b, max_positions=4)
    out["D_lookback90"] = replace(b, momentum_lookback=90)
    out["E_hardstop10"] = replace(b, hard_stop_pct=-0.10)
    out["F_topn3"] = replace(b, momentum_top_n=3)
    out["G_nocrashexit"] = replace(b, down_day_exit_pct=-99.0)
    # 全新结构性候选：动量排名加权仓位（强者更大、弱者更小，均值=1）
    out["K_动量加权"] = replace(b, momentum_weight=True)
    out["L_regime+动量加权"] = replace(
        b, regime_mode="index", regime_index=MARKET_INDEX_CODE,
        regime_ma=60, regime_force_exit=True, momentum_weight=True)
    # 全新结构性候选：组合权益回撤硬止损（用真实组合回撤而非指数代理，
    # 两段式：峰值回撤清仓、低点反弹重开；与指数闸门/tailhedge 本质不同）
    out["M_权益DD硬止损-15"] = replace(
        b, equity_dd_stop_enable=True,
        equity_dd_stop_pct=-0.15, equity_dd_resume_pct=0.10)
    out["N_权益DD硬止损-20"] = replace(
        b, equity_dd_stop_enable=True,
        equity_dd_stop_pct=-0.20, equity_dd_resume_pct=0.12)

    # 结构性组合（只用有「参数高原」支撑的维度，不含 exit_ma=20 尖峰）
    out["H_结构组合"] = replace(
        b, max_positions=4, momentum_lookback=90, hard_stop_pct=-0.10,
    )
    # 结构组合 + exit_ma=20（IS 尖峰，需 OOS 裁决）
    out["I_结构+exitma20"] = replace(
        b, max_positions=4, momentum_lookback=90, hard_stop_pct=-0.10,
        trend_exit_ma=20,
    )
    # 贪心全最优（典型过拟合对照组，预期 OOS 衰减）
    out["J_贪心全最优"] = replace(
        b, trend_exit_ma=20, momentum_top_n=3, momentum_lookback=90,
        hard_stop_pct=-0.10, risk_per_trade=0.04, max_positions=4,
        buy_score_threshold=4.5, down_day_exit_pct=-99.0, atr_stop_mult=3.0,
    )
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="sweep",
                    choices=["timing", "sweep", "confirm", "stability",
                             "regime", "folds"])
    ap.add_argument("--count", type=int, default=500)
    ap.add_argument("--is-end", type=int, default=350,
                    help="样本内结束索引（含 warmup）")
    args = ap.parse_args()

    codes = wide_universe()
    t0 = time.time()
    # 指数一并载入（供 regime 过滤用，不参与交易）
    data = preload(codes + [MARKET_INDEX_CODE, "000300.SH"], args.count)
    print(f"[数据] {len(data)} 只 × {args.count} 根日线  载入 {time.time()-t0:.1f}s")
    if not data:
        print("无数据，退出")
        return

    n = min(len(d["close"]) for d in data.values())
    IS_END = min(args.is_end, n - 120)
    # OOS 回退恰好 FIXED_WARMUP 根做指标预热 → 交易严格从 IS_END 开始，
    # 与 IS 段无重叠，且所有参数组合的窗口完全一致。
    OOS_LO = max(0, IS_END - FIXED_WARMUP)
    is_data = slice_by_index(data, 0, IS_END)
    oos_data = slice_by_index(data, OOS_LO, n)
    print(f"[窗口] 总 {n} 根 | IS 交易=[{FIXED_WARMUP},{IS_END}) "
          f"| OOS 交易=[{IS_END},{n}) 无重叠")
    print(f"       IS 日期 {is_data[codes[0]]['date'][0]} -> "
          f"{is_data[codes[0]]['date'][-1]}")
    print(f"       OOS 日期 {oos_data[codes[0]]['date'][0]} -> "
          f"{oos_data[codes[0]]['date'][-1]}")

    def bt(cfg, dset):
        # 指数只做 regime 判定，绝不进入交易池 / 基准
        ks = [k for k in dset.keys() if k not in INDEX_CODES]
        return run_backtest(ks, cfg, count=args.count, preloaded=dset)

    # ---------------- timing ----------------
    if args.stage == "timing":
        t = time.time()
        r = bt(base_cfg(), is_data)
        dt = time.time() - t
        print(f"\n单次回测 {dt:.2f}s → 60 次约 {dt*60/60:.1f} 分钟")
        print(fmt_row("base(IS)", r))
        return

    # ---------------- sweep（样本内逐维扫描）----------------
    if args.stage == "sweep":
        base = base_cfg()
        r_base_is = bt(base, is_data)
        print("\n" + "=" * 130)
        print("样本内基准（当前生产配置）")
        print("=" * 130)
        print(fmt_row("BASE(IS)", r_base_is))
        print(f"{'':<34} 基准买入持有: ret={r_base_is['bench_return']*100:+.2f}% "
              f"Sharpe={r_base_is['bench_sharpe']:+.2f} "
              f"MDD={r_base_is['bench_mdd']*100:.2f}%")

        best_per_dim: Dict[str, list] = {}
        all_rows = []
        for pname, values in GRID_1D.items():
            print("\n" + "-" * 130)
            print(f"[维度] {pname}")
            print("-" * 130)
            rows = []
            for v in values:
                cfg = replace(base, **{pname: v})
                r = bt(cfg, is_data)
                fit = score_config(r)
                rows.append((fit, v, r))
                all_rows.append((pname, v, fit, r))
                mark = "  <= 当前" if getattr(base, pname) == v else ""
                print(f"  {pname}={str(v):<8} fit={fit:+6.3f}  "
                      f"{fmt_row('', r).strip()}{mark}")
            rows.sort(key=lambda x: x[0], reverse=True)
            best_per_dim[pname] = [(f, v) for f, v, _ in rows]

        print("\n" + "=" * 130)
        print("各维度最优（样本内 fit 排序）")
        print("=" * 130)
        for pname, lst in best_per_dim.items():
            cur = getattr(base, pname)
            top = lst[0]
            print(f"  {pname:<22} 最优={str(top[1]):<8}(fit={top[0]:+.3f})  "
                  f"当前={str(cur):<8}  "
                  f"全序={[ (str(v), round(f,2)) for f, v in lst ]}")

        # 保存
        out = ROOT / "logs" / "opt_sweep_is.json"
        out.parent.mkdir(exist_ok=True)
        payload = []
        for pname, v, fit, r in all_rows:
            payload.append({
                "param": pname, "value": v, "fit": fit,
                "total_return": r.get("total_return"),
                "sharpe": r.get("sharpe"), "sortino": r.get("sortino"),
                "mdd": r.get("max_drawdown"), "calmar": r.get("calmar"),
                "n_trades": r.get("n_trades"), "win_rate": r.get("win_rate"),
                "alpha": r.get("alpha"), "exposure": r.get("exposure"),
            })
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        print(f"\n[保存] {out}")
        return

    # ---------------- stability（同一网格在 OOS 重跑，看形状是否稳定）----------------
    if args.stage == "stability":
        base = base_cfg()
        print("\n" + "=" * 130)
        print("参数形状稳定性：同一网格分别在 IS / OOS 上重跑")
        print("看的不是「哪个值最好」，而是「IS 的形状在 OOS 是否重现」。")
        print("形状不重现 = 该维度的 IS 最优点是噪声，不可采用。")
        print("=" * 130)
        stab = {}
        for pname, values in GRID_1D.items():
            print(f"\n[{pname}]")
            print(f"  {'值':<10}{'IS_fit':>9}{'IS_ret':>10}{'IS_Sh':>8}"
                  f"{'OOS_fit':>9}{'OOS_ret':>10}{'OOS_Sh':>8}{'OOS_MDD':>9}")
            pairs = []
            for v in values:
                cfg = replace(base, **{pname: v})
                ri = bt(cfg, is_data)
                ro = bt(cfg, oos_data)
                fi, fo = score_config(ri), score_config(ro)
                pairs.append((v, fi, fo))
                mk = " <=当前" if getattr(base, pname) == v else ""
                print(f"  {str(v):<10}{fi:>+9.3f}"
                      f"{ri['total_return']*100:>+9.1f}%{ri['sharpe']:>+8.2f}"
                      f"{fo:>+9.3f}"
                      f"{ro['total_return']*100:>+9.1f}%{ro['sharpe']:>+8.2f}"
                      f"{ro['max_drawdown']*100:>+8.1f}%{mk}")
            # Spearman 秩相关（IS 排名 vs OOS 排名）
            n_ = len(pairs)
            ris = sorted(range(n_), key=lambda k: pairs[k][1], reverse=True)
            ros = sorted(range(n_), key=lambda k: pairs[k][2], reverse=True)
            rank_i = {k: r for r, k in enumerate(ris)}
            rank_o = {k: r for r, k in enumerate(ros)}
            dsum = sum((rank_i[k] - rank_o[k]) ** 2 for k in range(n_))
            rho = 1 - 6 * dsum / (n_ * (n_ ** 2 - 1)) if n_ > 1 else 0.0
            is_best = max(pairs, key=lambda p: p[1])[0]
            oos_best = max(pairs, key=lambda p: p[2])[0]
            verdict = ("稳定" if rho >= 0.5 else
                       "弱" if rho >= 0.0 else "反转(噪声)")
            stab[pname] = rho
            print(f"  -> Spearman(IS,OOS)={rho:+.2f} [{verdict}]  "
                  f"IS最优={is_best}  OOS最优={oos_best}"
                  f"{'  一致' if is_best == oos_best else '  不一致'}")

        print("\n" + "=" * 130)
        print("稳定性汇总（rho 越高越可信）")
        print("=" * 130)
        for p, rho in sorted(stab.items(), key=lambda kv: kv[1], reverse=True):
            bar = "#" * int(max(0, rho) * 20)
            print(f"  {p:<22}{rho:+.2f}  {bar}")
        return

    # ---------------- folds（多折 walk-forward：最严格的稳健性检验）----------------
    if args.stage == "folds":
        b = base_cfg()
        REG = dict(regime_mode="index", regime_index=MARKET_INDEX_CODE,
                   regime_ma=60, regime_force_exit=True)
        REG_DUAL = dict(regime_mode="dual", regime_index=MARKET_INDEX_CODE,
                        regime_ma=60, regime_breadth_thresh=0.5,
                        regime_force_exit=True)
        tests: Dict[str, BacktestConfig] = {
            "P0_现状(无regime)": b,
            "P1_regime指数MA60+清仓": replace(b, **REG),
            "P2_P1+lookback90": replace(b, **REG, momentum_lookback=90),
            "P3_P1+maxpos5": replace(b, **REG, max_positions=5),
            "P4_P1+hardstop10": replace(b, **REG, hard_stop_pct=-0.10),
            "P5_P1+无暴跌退出": replace(b, **REG, down_day_exit_pct=-99.0),
            "P6_P1+lookback90+maxpos5": replace(
                b, **REG, momentum_lookback=90, max_positions=5),
            # 抗抖动（anti-whipsaw）：针对创业板指高β易 whipaw
            "P7_P1+确认2日": replace(b, **REG, regime_confirm_days=2),
            "P8_P1+缓冲2pct": replace(b, **REG, regime_buffer_pct=0.02),
            # 双过滤：指数MA60 且 宽度>50%
            "P9_P1+双过滤": replace(b, **REG_DUAL),
            # 全新：动量排名加权仓位（在已验证 regime 生产配置上叠加 alpha 杠杆）
            "P10_P1+动量加权": replace(b, **REG, momentum_weight=True),
            # 全新：组合权益回撤硬止损（用真实组合回撤，两段式）
            "P11_P0+权益DD-15": replace(
                b, equity_dd_stop_enable=True,
                equity_dd_stop_pct=-0.15, equity_dd_resume_pct=0.10),
            "P12_P0+权益DD-20": replace(
                b, equity_dd_stop_enable=True,
                equity_dd_stop_pct=-0.20, equity_dd_resume_pct=0.12),
        }

        FOLD = 75
        starts = list(range(FIXED_WARMUP, n - FOLD + 1, FOLD))
        print("\n" + "=" * 140)
        print(f"多折 walk-forward：{len(starts)} 个连续、互不重叠的检验窗口"
              f"（每窗 {FOLD} 根 ≈ {FOLD/21:.1f} 个月）")
        print("判据不是「平均多好」，而是「在多少个窗口里稳定优于对照」。")
        print("=" * 140)
        d0 = data[codes[0]]["date"]
        for k, s in enumerate(starts):
            print(f"  Fold{k+1}: 索引[{s},{min(s+FOLD, n)}) "
                  f"日期 {d0[s]} -> {d0[min(s+FOLD, n)-1]}")

        fold_res: Dict[str, list] = {}
        bench_by_fold = []
        for k, s in enumerate(starts):
            lo, hi = s - FIXED_WARMUP, min(s + FOLD, n)
            dsub = slice_by_index(data, lo, hi)
            for name, cfg in tests.items():
                r = bt(cfg, dsub)
                fold_res.setdefault(name, []).append(r)
            bench_by_fold.append(fold_res["P0_现状(无regime)"][k]["bench_return"])

        # 明细
        print(f"\n{'配置':<26}" + "".join(f"{'F'+str(k+1):>13}"
                                          for k in range(len(starts)))
              + f"{'均值Sh':>9}{'正alpha':>9}{'最差':>10}")
        print("-" * 140)
        print(f"{'[基准]等权买入持有':<26}"
              + "".join(f"{bench_by_fold[k]*100:>+12.1f}%"
                        for k in range(len(starts))))
        print("-" * 140)
        summary = []
        for name in tests:
            rs = fold_res[name]
            line = f"{name:<26}"
            shs, pos_alpha, worst = [], 0, 999.0
            for r in rs:
                if "error" in r:
                    line += f"{'ERR':>13}"
                    continue
                line += f"{r['total_return']*100:>+12.1f}%"
                shs.append(r["sharpe"])
                if r["alpha"] > 0:
                    pos_alpha += 1
                worst = min(worst, r["total_return"])
            msh = sum(shs) / len(shs) if shs else 0
            line += f"{msh:>+9.2f}{pos_alpha:>6}/{len(rs)}{worst*100:>+9.1f}%"
            print(line)
            summary.append((name, msh, pos_alpha, worst, rs))

        # 逐折胜负（vs 现状）
        print("\n" + "=" * 140)
        print("逐折对比「现状 P0」：+ = 该折收益更高")
        print("=" * 140)
        base_rs = fold_res["P0_现状(无regime)"]
        print(f"{'配置':<26}" + "".join(f"{'F'+str(k+1):>10}"
                                        for k in range(len(starts)))
              + f"{'胜':>6}{'累计差':>10}")
        for name in tests:
            if name == "P0_现状(无regime)":
                continue
            rs = fold_res[name]
            line = f"{name:<26}"
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

        print("\n" + "=" * 140)
        print("排序（按 均值Sharpe）")
        print("=" * 140)
        for name, msh, pa, worst, rs in sorted(summary, key=lambda x: -x[1]):
            mdds = [r["max_drawdown"] for r in rs if "error" not in r]
            exps = [r["exposure"] for r in rs if "error" not in r]
            print(f"  {name:<26} 均值Sh={msh:+.2f}  正alpha={pa}/{len(rs)}  "
                  f"最差折={worst*100:+.1f}%  "
                  f"平均MDD={sum(mdds)/len(mdds)*100:.1f}%  "
                  f"平均exp={sum(exps)/len(exps)*100:.0f}%")

        out = ROOT / "logs" / "opt_folds.json"
        out.parent.mkdir(exist_ok=True)
        payload = {
            "folds": [{"start": s, "date_from": d0[s],
                       "date_to": d0[min(s+FOLD, n)-1]} for s in starts],
            "bench": bench_by_fold,
            "results": {
                name: [{k: r.get(k) for k in
                        ("total_return", "sharpe", "max_drawdown", "calmar",
                         "n_trades", "win_rate", "alpha", "exposure")}
                       for r in fold_res[name]]
                for name in tests
            },
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        print(f"\n[保存] {out}")
        return

    # ---------------- regime（市场状态过滤：核心结构性改进）----------------
    if args.stage == "regime":
        b = base_cfg()
        variants: Dict[str, BacktestConfig] = {"R0_无过滤(现状)": b}
        # 指数闸门（创业板指），不同 MA 长度
        for ma in (20, 40, 60, 100):
            variants[f"R1_指数MA{ma}"] = replace(
                b, regime_mode="index", regime_index=MARKET_INDEX_CODE,
                regime_ma=ma)
        # 指数闸门 + 状态转差强制清仓
        for ma in (20, 40, 60):
            variants[f"R2_指数MA{ma}+清仓"] = replace(
                b, regime_mode="index", regime_index=MARKET_INDEX_CODE,
                regime_ma=ma, regime_force_exit=True)
        # 宽度闸门（宇宙内站上 MA 的比例）
        for ma, th in ((60, 0.4), (60, 0.5), (60, 0.6), (40, 0.5), (20, 0.5)):
            variants[f"R3_宽度MA{ma}>{th:.0%}"] = replace(
                b, regime_mode="breadth", regime_ma=ma,
                regime_breadth_thresh=th)
        for ma, th in ((60, 0.5), (40, 0.5)):
            variants[f"R4_宽度MA{ma}>{th:.0%}+清仓"] = replace(
                b, regime_mode="breadth", regime_ma=ma,
                regime_breadth_thresh=th, regime_force_exit=True)
        # 抗抖动（anti-whipsaw）：针对创业板指高β易 whipaw 的已知弱点
        variants[f"R5_指数MA60+清仓+确认2日"] = replace(
            b, regime_mode="index", regime_index=MARKET_INDEX_CODE,
            regime_ma=60, regime_force_exit=True, regime_confirm_days=2)
        variants[f"R6_指数MA60+清仓+缓冲2%"] = replace(
            b, regime_mode="index", regime_index=MARKET_INDEX_CODE,
            regime_ma=60, regime_force_exit=True, regime_buffer_pct=0.02)
        variants[f"R7_指数MA60+清仓+确认2日+缓冲2%"] = replace(
            b, regime_mode="index", regime_index=MARKET_INDEX_CODE,
            regime_ma=60, regime_force_exit=True,
            regime_confirm_days=2, regime_buffer_pct=0.02)
        # 双过滤：指数站上 MA60「且」宽度>50% 才放行（抗指数/个股失真）
        variants[f"R8_指数+宽度>50%+清仓(双过滤)"] = replace(
            b, regime_mode="dual", regime_index=MARKET_INDEX_CODE,
            regime_ma=60, regime_breadth_thresh=0.5, regime_force_exit=True)

        print("\n" + "=" * 138)
        print("市场状态过滤（regime filter）—— 解决「永远满仓」结构缺陷")
        print("现状 exposure≈97%：熊市/震荡市也满仓骑跌。目标是 OOS 由负转正。")
        print("=" * 138)
        print(f"\n{'变体':<24}{'段':<5}{'收益':>10}{'Sharpe':>8}{'MDD':>9}"
              f"{'Calmar':>8}{'笔数':>5}{'胜率':>7}{'exposure':>10}"
              f"{'alpha':>10}")
        print("-" * 138)
        rows = []
        for name, cfg in variants.items():
            ri, ro = bt(cfg, is_data), bt(cfg, oos_data)
            rows.append((name, cfg, ri, ro))
            for tag, r in (("IS", ri), ("OOS", ro)):
                if "error" in r:
                    print(f"{name:<24}{tag:<5} ERROR {r['error']}")
                    continue
                print(f"{name if tag=='IS' else '':<24}{tag:<5}"
                      f"{r['total_return']*100:>+9.2f}%{r['sharpe']:>+8.2f}"
                      f"{r['max_drawdown']*100:>+8.2f}%{r['calmar']:>8.2f}"
                      f"{r['n_trades']:>5}{r['win_rate']*100:>6.1f}%"
                      f"{r['exposure']*100:>9.1f}%{r['alpha']*100:>+9.2f}pt")
            print("-" * 138)

        r0, r0o = rows[0][2], rows[0][3]
        print(f"\n{'等权买入持有':<24}{'IS':<5}{r0['bench_return']*100:>+9.2f}%"
              f"{r0['bench_sharpe']:>+8.2f}{r0['bench_mdd']*100:>+8.2f}%")
        print(f"{'':<24}{'OOS':<5}{r0o['bench_return']*100:>+9.2f}%"
              f"{r0o['bench_sharpe']:>+8.2f}{r0o['bench_mdd']*100:>+8.2f}%")

        print("\n" + "=" * 138)
        print("按 OOS 表现排序（这是唯一有意义的排序）")
        print("=" * 138)
        ranked = sorted([r for r in rows if "error" not in r[3]],
                        key=lambda x: x[3]["sharpe"], reverse=True)
        print(f"{'变体':<24}{'OOS_ret':>10}{'OOS_Sh':>9}{'OOS_MDD':>10}"
              f"{'OOS_alpha':>11}{'IS_ret':>10}{'IS_Sh':>8}{'exp':>7}")
        for name, cfg, ri, ro in ranked:
            print(f"{name:<24}{ro['total_return']*100:>+9.2f}%"
                  f"{ro['sharpe']:>+9.2f}{ro['max_drawdown']*100:>+9.2f}%"
                  f"{ro['alpha']*100:>+10.2f}pt{ri['total_return']*100:>+9.2f}%"
                  f"{ri['sharpe']:>+8.2f}{ro['exposure']*100:>6.1f}%")

        out = ROOT / "logs" / "opt_regime.json"
        out.parent.mkdir(exist_ok=True)
        payload = {}
        for name, cfg, ri, ro in rows:
            if "error" in ri or "error" in ro:
                continue
            payload[name] = {
                "IS": {k: ri[k] for k in
                       ("total_return", "sharpe", "max_drawdown", "calmar",
                        "n_trades", "win_rate", "alpha", "exposure")},
                "OOS": {k: ro[k] for k in
                        ("total_return", "sharpe", "max_drawdown", "calmar",
                         "n_trades", "win_rate", "alpha", "exposure")},
                "OOS_curve": ro["equity_curve"],
                "OOS_bench_curve": ro["bench_curve"],
                "IS_curve": ri["equity_curve"],
                "IS_bench_curve": ri["bench_curve"],
                "exit_reasons_oos": ro["exit_reasons"],
            }
        out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        print(f"\n[保存] {out}")
        return

    # ---------------- confirm（候选配置 IS/OOS 双段验证）----------------
    if args.stage == "confirm":
        cands = candidates()
        print("\n" + "=" * 132)
        print("候选配置：样本内(IS) / 样本外(OOS) 双段验证")
        print("=" * 132)
        rows = []
        for name, cfg in cands.items():
            ri = bt(cfg, is_data)
            ro = bt(cfg, oos_data)
            rows.append((name, cfg, ri, ro))

        print(f"\n{'配置':<22}{'段':<5}{'收益':>10}{'Sharpe':>8}{'Sortino':>8}"
              f"{'MDD':>9}{'Calmar':>8}{'PF':>6}{'笔数':>5}{'胜率':>7}"
              f"{'持仓':>7}{'alpha':>9}")
        print("-" * 132)
        for name, cfg, ri, ro in rows:
            for tag, r in (("IS", ri), ("OOS", ro)):
                if "error" in r:
                    print(f"{name:<22}{tag:<5} ERROR {r['error']}")
                    continue
                print(f"{name if tag=='IS' else '':<22}{tag:<5}"
                      f"{r['total_return']*100:>+9.2f}%{r['sharpe']:>+8.2f}"
                      f"{r['sortino']:>+8.2f}{r['max_drawdown']*100:>+8.2f}%"
                      f"{r['calmar']:>8.2f}{r['profit_factor']:>6.2f}"
                      f"{r['n_trades']:>5}{r['win_rate']*100:>6.1f}%"
                      f"{r['avg_hold']:>6.1f}d{r['alpha']*100:>+8.2f}pt")
            print("-" * 132)

        # 基准
        r0 = rows[0][2]
        r0o = rows[0][3]
        print(f"\n{'等权买入持有':<22}{'IS':<5}{r0['bench_return']*100:>+9.2f}%"
              f"{r0['bench_sharpe']:>+8.2f}{'':>8}{r0['bench_mdd']*100:>+8.2f}%")
        print(f"{'':<22}{'OOS':<5}{r0o['bench_return']*100:>+9.2f}%"
              f"{r0o['bench_sharpe']:>+8.2f}{'':>8}{r0o['bench_mdd']*100:>+8.2f}%")

        # IS→OOS 衰减分析
        print("\n" + "=" * 132)
        print("IS → OOS 衰减（过拟合检测：衰减越小越稳健）")
        print("=" * 132)
        print(f"{'配置':<22}{'IS_Sh':>8}{'OOS_Sh':>8}{'ΔSh':>8}"
              f"{'IS_alpha':>10}{'OOS_alpha':>11}{'Δalpha':>10}{'判定':>12}")
        for name, cfg, ri, ro in rows:
            if "error" in ri or "error" in ro:
                continue
            dsh = ro["sharpe"] - ri["sharpe"]
            da = (ro["alpha"] - ri["alpha"]) * 100
            verdict = ("稳健" if (ro["alpha"] > 0 and dsh > -0.8) else
                       "衰减" if ro["alpha"] > 0 else "失效")
            print(f"{name:<22}{ri['sharpe']:>+8.2f}{ro['sharpe']:>+8.2f}"
                  f"{dsh:>+8.2f}{ri['alpha']*100:>+9.2f}pt"
                  f"{ro['alpha']*100:>+10.2f}pt{da:>+9.2f}pt{verdict:>12}")

        # 保存
        out = ROOT / "logs" / "opt_confirm.json"
        out.parent.mkdir(exist_ok=True)
        payload = {}
        for name, cfg, ri, ro in rows:
            if "error" in ri or "error" in ro:
                continue
            payload[name] = {
                "config": {k: v for k, v in asdict(cfg).items()},
                "IS": {k: ri[k] for k in
                       ("total_return", "sharpe", "sortino", "max_drawdown",
                        "calmar", "profit_factor", "n_trades", "win_rate",
                        "avg_hold", "alpha", "exposure", "bench_return")},
                "OOS": {k: ro[k] for k in
                        ("total_return", "sharpe", "sortino", "max_drawdown",
                         "calmar", "profit_factor", "n_trades", "win_rate",
                         "avg_hold", "alpha", "exposure", "bench_return")},
                "IS_curve": ri["equity_curve"],
                "OOS_curve": ro["equity_curve"],
                "IS_bench_curve": ri["bench_curve"],
                "OOS_bench_curve": ro["bench_curve"],
            }
        out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        print(f"\n[保存] {out}")
        return


if __name__ == "__main__":
    main()

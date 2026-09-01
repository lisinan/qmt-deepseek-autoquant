# -*- coding: utf-8 -*-
"""
实验台：在日期对齐的真实历史上做 A/B、消融、参数敏感性、walk-forward。

原则
----
1. **公平比较**：所有策略用同一份数据、同一套成本（佣金+滑点）、同一时间窗。
2. **诚实基准**：等权买入持有（后上市标的上市后才买，不塞未来函数）
   + 创业板指本身。跑不赢基准就要明说。
3. **反过拟合**：任何"优选"配置必须在 walk-forward 的多个不重叠子区间
   都不崩，才算可用；只在单区间最优的一律标记为可疑。

用法
----
    python strategy/experiments.py --suite core
    python strategy/experiments.py --suite ablation
    python strategy/experiments.py --suite sweep
    python strategy/experiments.py --suite walkforward
    python strategy/experiments.py --suite all
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import STOCK_CODES, INDEX_CODES        # noqa: E402
from data.hist_cache import load_panel                      # noqa: E402
from strategy.research_lab import (                          # noqa: E402
    LabConfig, run_lab, buy_hold_benchmark, index_benchmark,
    print_table, fmt_row, HEADER,
)

START = "20190101"
INDEX = "399006.SZ"          # 创业板指（AI 产业链最贴近的宽基）


# ============================================================ 预设

def preset_original() -> LabConfig:
    """原策略语义（6 因子 + 闸门 + ATR 止损止盈 + 紧移动止损 + 5% 仓位）。

    与旧代码的唯一区别：计入真实成本、日期正确对齐。
    """
    return LabConfig(
        name="A_原策略(scalp)",
        entry_mode="factor", use_gate=True,
        exit_mode="scalp", stop_loss=-0.04, take_profit=0.12,
        atr_stop_mult=2.5, tp_atr_mult=4.0,
        trailing=True, trailing_activation=0.06, trailing_stop=-0.03,
        max_hold_days=20,
        sizing="risk", risk_per_trade=0.01, max_pos_pct=0.05,
        max_positions=8, momentum_rank=False, regime_filter=False,
    )


def preset_prev_optimized() -> LabConfig:
    """上一轮"优化版"（趋势骑行 + 动量前6 + 信念仓位 30%）。"""
    return LabConfig(
        name="B_上轮优化(trend)",
        entry_mode="factor", use_gate=True,
        exit_mode="trend", trend_exit_ma=60, hard_stop_pct=-0.18,
        max_hold_days=120, down_day_exit_pct=-9.0,
        sizing="risk", risk_per_trade=0.02, max_pos_pct=0.30,
        max_positions=8,
        momentum_rank=True, momentum_top_n=6, momentum_lookback=60,
        regime_filter=False,
    )


def preset_v3() -> LabConfig:
    """本轮候选：趋势骑行 + 指数 regime 过滤 + 动量轮动 + 吊灯止损。"""
    return LabConfig(
        name="C_本轮候选(regime)",
        entry_mode="factor", use_gate=True,
        exit_mode="chandelier", chandelier_mult=3.0, hard_stop_pct=-0.20,
        max_hold_days=250,
        sizing="risk", risk_per_trade=0.02, max_pos_pct=0.30,
        max_positions=5,
        momentum_rank=True, momentum_top_n=5, momentum_lookback=60,
        regime_filter=True, regime_ma=60, regime_exit=False,
        rotate=True, rotate_edge=0.20,
    )


# ============================================================ suites

def load_all():
    codes = sorted(STOCK_CODES) + sorted(INDEX_CODES)
    panel = load_panel(codes, START, do_download=False, verbose=False)
    return panel, sorted(STOCK_CODES)


def suite_core(panel, stocks) -> List[dict]:
    rows = []
    bh = buy_hold_benchmark(panel, stocks)
    bh["name"] = "基准_等权买入持有"
    rows.append(bh)
    ix = index_benchmark(panel, INDEX)
    if "error" not in ix:
        ix["name"] = "基准_创业板指"
        rows.append(ix)
    for cfg in (preset_original(), preset_prev_optimized(), preset_v3()):
        rows.append(run_lab(panel, stocks, cfg, INDEX))
    return rows


def suite_ablation(panel, stocks) -> List[dict]:
    """从上轮优化版出发，逐项加/减模块，量化每个模块的边际贡献。"""
    base = replace(preset_prev_optimized(), name="base")
    variants = [
        ("base", base),
        ("+regime(MA60)", replace(base, regime_filter=True)),
        ("+regime+退市清仓", replace(base, regime_filter=True,
                                     regime_exit=True)),
        ("+吊灯止损", replace(base, exit_mode="chandelier",
                              chandelier_mult=3.0, max_hold_days=250)),
        ("+吊灯+regime", replace(base, exit_mode="chandelier",
                                 chandelier_mult=3.0, max_hold_days=250,
                                 regime_filter=True)),
        ("+轮动", replace(base, rotate=True)),
        ("+等权仓位", replace(base, sizing="equal", max_positions=5)),
        ("+目标波动仓位", replace(base, sizing="voltarget",
                                  max_positions=5)),
        ("+突破入场", replace(base, entry_mode="breakout")),
        ("+滚动VWAP", replace(base, vwap_mode="rolling")),
        ("+时间止损20d", replace(base, time_stop_days=20)),
        ("-动量排名", replace(base, momentum_rank=False)),
    ]
    rows = []
    for nm, cfg in variants:
        rows.append(run_lab(panel, stocks, replace(cfg, name=nm), INDEX))
    return rows


def suite_sweep(panel, stocks) -> Dict[str, List[dict]]:
    base = replace(preset_prev_optimized(), name="base",
                   regime_filter=True, exit_mode="chandelier",
                   chandelier_mult=3.0, max_hold_days=250)
    out: Dict[str, List[dict]] = {}
    out["regime_ma"] = [
        run_lab(panel, stocks, replace(base, name=f"regime_ma={x}",
                                       regime_ma=x), INDEX)
        for x in (20, 40, 60, 90, 120, 200)
    ]
    out["chandelier_mult"] = [
        run_lab(panel, stocks, replace(base, name=f"chand={x}",
                                       chandelier_mult=x), INDEX)
        for x in (2.0, 2.5, 3.0, 3.5, 4.0, 5.0)
    ]
    out["momentum_top_n"] = [
        run_lab(panel, stocks, replace(base, name=f"top_n={x}",
                                       momentum_top_n=x,
                                       max_positions=max(3, x)), INDEX)
        for x in (2, 3, 4, 5, 6, 8)
    ]
    out["momentum_lookback"] = [
        run_lab(panel, stocks, replace(base, name=f"mom_lb={x}",
                                       momentum_lookback=x), INDEX)
        for x in (20, 40, 60, 90, 120)
    ]
    out["max_pos_pct"] = [
        run_lab(panel, stocks, replace(base, name=f"max_pos={x}",
                                       max_pos_pct=x), INDEX)
        for x in (0.15, 0.20, 0.25, 0.30, 0.40)
    ]
    out["risk_per_trade"] = [
        run_lab(panel, stocks, replace(base, name=f"risk={x}",
                                       risk_per_trade=x), INDEX)
        for x in (0.01, 0.015, 0.02, 0.03, 0.04)
    ]
    return out


WF_WINDOWS = [
    ("2019-2020 牛+疫情", "20190101", "20201231"),
    ("2021 结构牛",       "20210101", "20211231"),
    ("2022 熊市",         "20220101", "20221231"),
    ("2023 震荡",         "20230101", "20231231"),
    ("2024 急跌+反弹",     "20240101", "20241231"),
    ("2025-2026 AI牛",    "20250101", "20261231"),
]


def suite_walkforward(panel, stocks, cfgs=None) -> Dict[str, List[dict]]:
    cfgs = cfgs or [preset_original(), preset_prev_optimized(), preset_v3()]
    out: Dict[str, List[dict]] = {}
    for label, s, e in WF_WINDOWS:
        sub = panel.slice_dates(s, e)
        if len(sub.dates) < 160:
            continue
        rows = []
        bh = buy_hold_benchmark(sub, stocks, warmup=0)
        bh["name"] = "基准_买入持有"
        rows.append(bh)
        ixr = index_benchmark(sub, INDEX)
        if "error" not in ixr:
            ixr["name"] = "基准_创业板指"
            rows.append(ixr)
        for cfg in cfgs:
            # 子区间内预热靠 warmup 天，窗口需要足够长
            rows.append(run_lab(sub, stocks, replace(cfg, warmup=130), INDEX))
        out[label] = rows
    return out


# ============================================================ 入口

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", default="core",
                    choices=["core", "ablation", "sweep", "walkforward", "all"])
    ap.add_argument("--json", default="", help="把结果摘要写到 JSON")
    args = ap.parse_args()

    panel, stocks = load_all()
    print(f"数据: {len(panel.dates)} 交易日  {panel.dates[0]} ~ "
          f"{panel.dates[-1]}  |  股票 {len(stocks)} 只  |  指数 {INDEX}")

    dump: Dict[str, object] = {}

    if args.suite in ("core", "all"):
        rows = suite_core(panel, stocks)
        print_table(rows, "【核心对比】全样本 2019-2026（含佣金+滑点）")
        dump["core"] = [_slim(r) for r in rows]

    if args.suite in ("ablation", "all"):
        rows = suite_ablation(panel, stocks)
        print_table(rows, "【模块消融】从上轮优化版出发，逐项加减")
        dump["ablation"] = [_slim(r) for r in rows]

    if args.suite in ("sweep", "all"):
        sw = suite_sweep(panel, stocks)
        for k, rows in sw.items():
            print_table(rows, f"【参数敏感性】{k}")
        dump["sweep"] = {k: [_slim(r) for r in v] for k, v in sw.items()}

    if args.suite in ("walkforward", "all"):
        wf = suite_walkforward(panel, stocks)
        for label, rows in wf.items():
            print_table(rows, f"【Walk-Forward】{label}")
        dump["walkforward"] = {k: [_slim(r) for r in v] for k, v in wf.items()}

    if args.json:
        p = Path(args.json)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as f:
            json.dump(dump, f, ensure_ascii=False, indent=1)
        print(f"\n[已写出] {p}")


def _slim(r: dict) -> dict:
    if "error" in r:
        return {"name": r.get("name", "?"), "error": r["error"]}
    keep = ("name", "total_return", "cagr", "max_drawdown", "sharpe",
            "sortino", "calmar", "n_trades", "win_rate", "profit_factor",
            "avg_hold", "exposure", "start", "end", "exit_reasons",
            "avg_win", "avg_loss")
    d = {k: r[k] for k in keep if k in r}
    d["curve"] = r.get("curve", [])
    d["curve_dates"] = r.get("dates", [])
    return d


if __name__ == "__main__":
    main()

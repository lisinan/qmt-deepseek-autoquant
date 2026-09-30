# -*- coding: utf-8 -*-
"""诊断：观察篮为什么贵——拆开「槽位占用」与「退出豁免」两个效应。"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import strategy._evolve_wf as W  # noqa: E402
from strategy.backtest_daily import run_backtest  # noqa: E402

MARKET = W.MARKET_INDEX_CODE
INDEX_CODES = W.INDEX_CODES


def main() -> None:
    codes = W.wide_universe()
    data = W.preload(codes + [MARKET, "000300.SH"], 900)
    d0, panel = W.aligned_universe(data, codes)
    data = panel
    ks = [k for k in data.keys() if k not in INDEX_CODES]

    b = W.base_cfg()
    basket = W.PROD_MANUAL_ENTRY_CODES
    print(f"生产观察篮：{basket}\n")

    cases = {
        "P0 无篮子（已验证口径）": b,
        "N1 实盘现状 5槽+豁免": replace(b, manual_entry_codes=basket,
                                    manual_entry_exit_exempt=True),
        "N2 5槽+关豁免": replace(b, manual_entry_codes=basket,
                             manual_entry_exit_exempt=False),
        "N3 2槽+豁免": replace(b, manual_entry_codes=basket[:2],
                           manual_entry_exit_exempt=True),
    }

    print(f"{'配置':<26}{'收益':>10}{'Sharpe':>9}{'MDD':>9}{'成交':>7}"
          f"{'篮子成交':>9}{'篮子盈利占比':>12}")
    print("-" * 82)
    for name, cfg in cases.items():
        r = run_backtest(ks, cfg, count=900, preloaded=data)
        trades = r.get("trades") or []
        bset = set(cfg.manual_entry_codes or ())
        btr = [t for t in trades if t["code"] in bset]
        bwin = sum(1 for t in btr if t["pnl_pct"] > 0)
        rate = (bwin / len(btr) * 100) if btr else float("nan")
        print(f"{name:<26}{r['total_return']*100:>+9.2f}%{r['sharpe']:>+9.2f}"
              f"{r['max_drawdown']*100:>8.2f}%{len(trades):>7}"
              f"{len(btr):>9}{rate:>11.1f}%")


if __name__ == "__main__":
    main()

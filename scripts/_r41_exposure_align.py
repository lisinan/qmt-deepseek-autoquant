# -*- coding: utf-8 -*-
"""【R41 AM-EVOLVE 2026-10-21】回测/实盘「近端暴露」对齐量测（第 20 例口径背离审计）

动机：实盘自 2026-10-05 起**连续 12 个交易日零 BUY**（100% 现金、0 成交）。
     R31/R38 已把它定价为「低暴露 = alpha 形态」，但从未核对过：
     **回测在同样这段最近行情里是不是也空仓？**

若回测在末 13 根同样空仓  ⇒ 两侧一致，D1（KPI#4 无解）第 15 条独立证据。
若回测在末 13 根有持仓/成交 ⇒ **第 20 例「回测有、实盘无」**，是真正的缺陷。

只读、零副作用、不写任何配置。
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy._evolve_wf import (  # noqa: E402
    wide_universe, preload, aligned_universe, base_cfg, MARKET_INDEX_CODE, INDEX_CODES,
)
from strategy.backtest_daily import run_backtest  # noqa: E402

COUNT = 900
TAIL = 20


def main():
    codes = wide_universe()
    data = preload(codes + [MARKET_INDEX_CODE, "000300.SH"], COUNT)
    if not data:
        print("无数据，退出")
        return 1
    d0, panel = aligned_universe(data, codes)
    if not d0:
        print("无数据（对齐失败），退出")
        return 1
    data = panel
    n = len(d0)
    print(f"[数据] {len(data)} 只 × {n} 根  {d0[0]} -> {d0[-1]}")

    ks = [k for k in data.keys() if k not in INDEX_CODES]
    cfg = base_cfg()
    r = run_backtest(ks, cfg, count=COUNT, preloaded=data)
    print(f"[P0 IS] ret={r['total_return']*100:+.2f}% Sh={r['sharpe']:.4f} "
          f"MDD={r['max_drawdown']*100:.2f}% 成交={r['n_trades']} "
          f"Exposure={r['exposure']*100:.1f}%")

    # ---- 末端成交时点分布 ----
    trades = r.get("trades") or []
    print(f"[成交] 共 {len(trades)} 笔")
    if trades:
        def tdate(t):
            return str(t.get("date") or t.get("exit_date") or t.get("entry_date") or "")
        # 只统计 BUY（建仓）
        buys = []
        for t in trades:
            d = None
            for key in ("entry_date", "date", "buy_date", "open_date"):
                if isinstance(t, dict) and t.get(key):
                    d = str(t[key])
                    break
            buys.append(d)
        buys = [b for b in buys if b]
        print(f"[末 10 笔建仓日] {buys[-10:]}")
        last_buy = buys[-1] if buys else None
        print(f"[最后建仓日] {last_buy}  (实盘最后建仓 ≈ 2026-10-05 之前)")

    # ---- 末端权益曲线 / 暴露代理 ----
    eq = r.get("equity_curve") or []
    print(f"[权益曲线] 长度 {len(eq)}  末值 {eq[-1]:,.2f}" if eq else "[权益曲线] 空")
    if eq:
        m = min(len(eq), TAIL)
        print(f"[末 {m} 根权益]")
        for i in range(len(eq) - m, len(eq)):
            j = i if len(eq) == n else i
            date = d0[j] if j < len(d0) and j >= 0 else "?"
            prev = eq[i - 1] if i > 0 else eq[i]
            chg = (eq[i] / prev - 1) * 100 if prev else 0.0
            print(f"   {date}  {eq[i]:>14,.2f}  {chg:+.3f}%")

    out = {
        "n_bars": n,
        "start": d0[0],
        "end": d0[-1],
        "is_ret": r["total_return"],
        "is_sharpe": r["sharpe"],
        "is_mdd": r["max_drawdown"],
        "n_trades": r["n_trades"],
        "exposure": r["exposure"],
        "tail_dates": d0[-TAIL:],
        "tail_equity": eq[-TAIL:] if eq else [],
    }
    with open("reports/_r41_exposure_align.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print("[已写] reports/_r41_exposure_align.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())

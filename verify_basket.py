# -*- coding: utf-8 -*-
"""
全宇宙回测对比（STOCK_CODES，500 日线，约 2 年）：
  baseline          : 原逻辑（无闸门/无成本/5万固定/紧止损/5天）
  optimized_scalp   : 优化v1（闸门+吊灯+波动率仓位+成本）
  optimized_trend   : 本次优化（趋势骑行退出 + 动量排名 + 信念仓位 + 成本）
并给出等权买入持有基准。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config.settings import STOCK_CODES  # noqa: E402
from strategy.backtest_daily import run_backtest, BacktestConfig, load_daily  # noqa: E402


def buyhold_basket(count=500):
    rets = []
    for code in STOCK_CODES:
        d = load_daily(code, count)
        if not d or len(d["close"]) < 120:
            continue
        c = d["close"]
        rets.append(c[-1] / c[65] - 1)
    n = len(rets)
    if not n:
        return 0.0, 0
    # 等权：每支投入 1/n，组合收益 = mean(个券收益)
    return sum(rets) / n, n


def preset(name):
    if name == "baseline":
        return BacktestConfig(
            use_gate=False, stop_loss=-0.03, take_profit=0.10,
            atr_stop_mult=0.0, tp_atr_mult=0.0,
            trailing=False, vol_sizing=False, max_hold_days=5,
            cost_pct=0.0, fixed_amount=50000.0,
        )
    if name == "optimized_scalp":
        return BacktestConfig(
            use_gate=True, stop_loss=-0.04, take_profit=0.12,
            atr_stop_mult=2.0, tp_atr_mult=4.0,
            trailing=True, trailing_activation=0.06, trailing_stop=-0.03,
            trailing_floor=-0.005, vol_sizing=True, max_hold_days=20,
            cost_pct=0.0015, chandelier=True, fixed_amount=50000.0,
        )
    if name == "baseline_cost":
        # 公平基线：原逻辑但计入真实交易成本（919 笔 × 双边 ≈ -28% 拖累）
        return BacktestConfig(
            use_gate=False, stop_loss=-0.03, take_profit=0.10,
            atr_stop_mult=0.0, tp_atr_mult=0.0,
            trailing=False, vol_sizing=False, max_hold_days=5,
            cost_pct=0.0015, fixed_amount=50000.0,
        )
    if name == "optimized_trend":
        return BacktestConfig(
            use_gate=True, cost_pct=0.0015, vol_sizing=True,
            exit_mode="trend", trend_exit_ma=60, hard_stop_pct=-0.18,
            trend_max_hold_days=120,
            momentum_rank=True, momentum_top_n=6, momentum_lookback=60,
            risk_per_trade=0.02, fixed_amount=300000.0,
            down_day_exit_pct=-9.0,
        )
    raise ValueError(name)


def fmt(r):
    if "error" in r:
        return f"ERR({r['error']})"
    return (f"ret={r['total_return']*100:+7.2f}%  Sharpe={r['sharpe']:+.2f}  "
            f"MDD={r['max_drawdown']*100:7.2f}%  win={r['win_rate']*100:4.1f}%  "
            f"n={r['n_trades']}  avgHold={r['avg_hold']:5.1f}d")


if __name__ == "__main__":
    bh, n = buyhold_basket(500)
    print("=" * 96)
    print(f"全宇宙回测  |  STOCK_CODES={len(STOCK_CODES)} 实际载入={n}  |  500 日线")
    print(f"等权买入持有基准: {bh*100:+.2f}%")
    print("=" * 96)
    results = {}
    for nm in ["baseline", "baseline_cost", "optimized_scalp", "optimized_trend"]:
        r = run_backtest(list(STOCK_CODES), preset(nm), count=500)
        results[nm] = r
        print(f"\n### {nm.upper()}")
        print(f"  {fmt(r)}")
    print("\n" + "-" * 96)
    print("对比（新优化 - 原优化scalp / baseline）:")
    ot = results["optimized_trend"]; os_ = results["optimized_scalp"]; bl = results["baseline"]
    bc = results.get("baseline_cost", bl)
    print(f"  累计收益: vs fair-base(含成本) { (ot['total_return']-bc['total_return'])*100:+.2f}pt"
          f"   vs old-opt { (ot['total_return']-os_['total_return'])*100:+.2f}pt")
    print(f"  Sharpe : vs fair-base { ot['sharpe']-bc['sharpe']:+.2f}"
          f"   vs old-opt { ot['sharpe']-os_['sharpe']:+.2f}")
    print(f"  MDD    : vs fair-base { (ot['max_drawdown']-bc['max_drawdown'])*100:+.2f}pt"
          f"   vs old-opt { (ot['max_drawdown']-os_['max_drawdown'])*100:+.2f}pt（负=更优）")

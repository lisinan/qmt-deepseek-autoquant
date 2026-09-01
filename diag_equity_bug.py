# -*- coding: utf-8 -*-
"""
诊断脚本：验证回测引擎的权益记账 bug

假设：run_backtest 里 daily_rets 的计算把「次日开盘买入的持仓」
      用「当日收盘价」标记，导致注入虚假波动，Sharpe 失真。

验证方法：
  1) 确认 xtdata 数据可用（真实日线）
  2) 跑 optimized_trend，打印 total_return 与 sharpe
  3) 用 equity_curve 独立重算 daily returns 与 Sharpe，
     与引擎内部返回的 sharpe 对比。
     若两者差异巨大 → 证明 daily_rets 采样口径错误。
"""
import sys
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config.settings import STOCK_CODES  # noqa: E402
from core.qmt_client import qmt_client  # noqa: E402
from strategy.backtest_daily import run_backtest, BacktestConfig, load_daily  # noqa: E402


def sharpe_from_curve(curve):
    """从权益曲线独立重算年化 Sharpe（正确口径：相邻两日末权益）。"""
    rets = []
    for i in range(1, len(curve)):
        if curve[i - 1] > 0:
            rets.append(curve[i] / curve[i - 1] - 1)
    if len(rets) < 2:
        return 0.0, 0.0, 0.0
    mean_r = sum(rets) / len(rets)
    var_r = sum((x - mean_r) ** 2 for x in rets) / len(rets)
    std_r = math.sqrt(var_r)
    sh = (mean_r / std_r * math.sqrt(245)) if std_r > 0 else 0.0
    return sh, mean_r, std_r


print("=" * 80)
print(f"[1] 数据源模式: {qmt_client.mode}")
print("=" * 80)

# 确认真实数据
d = load_daily("300308.SZ", 500)
if d:
    print(f"  300308.SZ 载入 {len(d['close'])} 根日线, "
          f"首收={d['close'][0]:.2f} 末收={d['close'][-1]:.2f}")
else:
    print("  !! 无法载入日线数据")
    sys.exit(1)

cfg = BacktestConfig(
    use_gate=True, cost_pct=0.0015, vol_sizing=True,
    exit_mode="trend", trend_exit_ma=60, hard_stop_pct=-0.18,
    trend_max_hold_days=120,
    momentum_rank=True, momentum_top_n=6, momentum_lookback=60,
    risk_per_trade=0.02, fixed_amount=300000.0,
    down_day_exit_pct=-9.0,
)

print()
print("=" * 80)
print("[2] 运行 optimized_trend 回测")
print("=" * 80)
r = run_backtest(list(STOCK_CODES), cfg, count=500)
if "error" in r:
    print("ERROR:", r["error"])
    sys.exit(1)

print(f"  引擎返回 total_return = {r['total_return']*100:+.2f}%")
print(f"  引擎返回 sharpe       = {r['sharpe']:+.3f}   <-- 可疑")
print(f"  引擎返回 MDD          = {r['max_drawdown']*100:.2f}%")
print(f"  n_trades={r['n_trades']}  avg_hold={r['avg_hold']:.1f}d")

curve = r["equity_curve"]
sh2, mean_r, std_r = sharpe_from_curve(curve)
print()
print("=" * 80)
print("[3] 用 equity_curve 独立重算（正确口径）")
print("=" * 80)
print(f"  曲线长度        = {len(curve)}")
print(f"  曲线首/末       = {curve[0]:,.0f} -> {curve[-1]:,.0f}")
print(f"  重算 total_ret  = {(curve[-1]/curve[0]-1)*100:+.2f}%")
print(f"  重算 sharpe     = {sh2:+.3f}   <-- 正确口径")
print(f"  日均收益 mean_r = {mean_r*100:+.4f}%")
print(f"  日波动   std_r  = {std_r*100:.4f}%")

print()
print("=" * 80)
print("[4] 结论")
print("=" * 80)
diff = abs(sh2 - r["sharpe"])
print(f"  Sharpe 差异 = {diff:.3f}")
if diff > 0.5:
    print("  >>> 确认 BUG：引擎内部 daily_rets 口径错误。")
    print("  >>> 原因：prev_equity 与 equity 都用 close[i] 标记，")
    print("      但买入价是 open[i+1]，持仓在建仓当日就被错误重估，")
    print("      把「隔夜跳空」当成当日盈亏注入 daily_rets。")
else:
    print("  >>> 两者一致，daily_rets 口径正确。")

# 额外：检查数据长度是否对齐（跨股票日期对齐 bug）
print()
print("=" * 80)
print("[5] 检查跨标的数据长度对齐")
print("=" * 80)
lens = {}
for code in STOCK_CODES:
    dd = load_daily(code, 500)
    if dd:
        lens[code] = len(dd["close"])
if lens:
    uniq = sorted(set(lens.values()))
    print(f"  各标的日线长度分布: {uniq}")
    if len(uniq) > 1:
        print("  >>> 存在长度不一致！索引 i 在不同标的指向不同日期。")
        for code, L in sorted(lens.items(), key=lambda kv: kv[1]):
            print(f"      {code}: {L}")
    else:
        print("  >>> 所有标的长度一致（本样本内暂无错位风险）")

# -*- coding: utf-8 -*-
"""★ 决定性验证：策略是否真的跑赢「什么都不做」。

回测 IS 报 +86.92% / Sh 1.93 看着很好，但实盘自 09-22 复位以来是亏的
（100 万 → 973,233.60，−2.68%）。这个矛盾必须解释。

最可能的解释：这段样本（2023-01~2026-09）AI 板块暴涨
（300308 130→813、688498 →1638），「+87%」只是**买入并持有的 beta**，
策略在 beta 之上做的是**负贡献**（摩擦 + 日线闸门压低暴露 + 频繁换手）。

本脚本用**同一批对齐面板**算基准：
  A) 全宇宙等权 buy & hold（不做任何择时）
  B) 动量 top3 等权 buy & hold（只选股、不择时）
  C) 只按「日线闸门 trend_up」择时持有全宇宙（只择时、不选股）
再与策略 IS 对照，看 alpha 到底是正是负。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy._evolve_wf import preload, wide_universe, aligned_universe  # noqa
from strategy.backtest_daily import load_daily  # noqa

CODES = wide_universe()
print('宇宙:', len(CODES), '只')

data = preload(CODES, 1000)
dates, panel = aligned_universe(data, CODES)
n = len(dates)
print('对齐面板: %d 根  %s -> %s' % (n, dates[0], dates[-1]))

WARM = 130


def equity_buyhold(panel, dates, codes, lo=WARM):
    """等权买入持有：lo 日起全仓等权，之后不再调整（忽略成本）。"""
    px = {c: panel[c]['close'] for c in codes}
    valid = {c: (panel[c].get('valid') or [1] * len(dates)) for c in codes}
    m = len(dates) - lo
    eq = [1.0]
    # 等权持有固定份额：每只买 1/price[lo] 份（归一化）
    units = {}
    for c in codes:
        p0 = px[c][lo]
        units[c] = (1.0 / len(codes)) / p0 if p0 and p0 > 0 else 0.0
    for i in range(lo + 1, len(dates)):
        v = 0.0
        for c in codes:
            p = px[c][i]
            # 停牌/缺失沿用上一根价
            if not p or p <= 0 or (i < len(valid[c]) and not valid[c][i]):
                p = px[c][i - 1]
            v += units[c] * (p or 0)
        eq.append(v)
    return eq


def stats(eq):
    rets = []
    for i in range(1, len(eq)):
        if eq[i - 1] > 0:
            rets.append(eq[i] / eq[i - 1] - 1)
    if not rets:
        return 0, 0, 0
    import statistics as st
    mu = st.mean(rets)
    sd = st.pstdev(rets) or 1e-9
    total = eq[-1] / eq[0] - 1
    sharpe = (mu / sd) * (252 ** 0.5)
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return total, sharpe, mdd


print()
print('=' * 92)
print('%-42s %12s %10s %10s' % ('方案', '总收益', 'Sharpe', '最大回撤'))
print('=' * 92)

res = {}
eq = equity_buyhold(panel, dates, CODES)
t, s, m = stats(eq)
res['A_全宇宙等权买入持有'] = (t, s, m)
print('%-42s %+11.2f%% %10.2f %9.2f%%' % ('A 全宇宙等权 buy&hold（不择时）', t * 100, s, m * 100))

# B) 动量 top3 等权 buy&hold：用 60 日动量每 20 日再平衡选 3 只、始终满仓
def equity_momentum_topn(panel, dates, codes, topn=3, lookback=60, rebal=20, lo=WARM):
    px = {c: panel[c]['close'] for c in codes}
    eq = [1.0]
    held = None
    units = {}
    for i in range(lo + 1, len(dates)):
        if held is None or (i - lo) % rebal == 0:
            mom = []
            for c in codes:
                if i - lookback < 0:
                    continue
                p0, p1 = px[c][i - lookback], px[c][i]
                if p0 and p0 > 0 and p1:
                    mom.append((p1 / p0 - 1, c))
            mom.sort(reverse=True)
            held = [c for _, c in mom[:topn]] or codes[:topn]
            units = {c: (1.0 / len(held)) / (px[c][i] or 1) for c in held}
        v = sum(units.get(c, 0) * (px[c][i] or px[c][i - 1]) for c in held)
        eq.append(v)
    return eq


eq = equity_momentum_topn(panel, dates, CODES)
t, s, m = stats(eq)
res['B_动量top3轮动'] = (t, s, m)
print('%-42s %+11.2f%% %10.2f %9.2f%%' % ('B 动量 top3 每20日轮动（满仓）', t * 100, s, m * 100))

# C) 趋势择时：MA60 之上满仓持有全宇宙，之下空仓
def equity_trend_timing(panel, dates, codes, ma=60, lo=WARM):
    px = {c: panel[c]['close'] for c in codes}
    eq = [1.0]
    units = {}
    invested = False
    for i in range(lo + 1, len(dates)):
        # 用全宇宙等权指数的 MA 判趋势
        idx = []
        for j in range(max(0, i - ma), i + 1):
            vals = [px[c][j] for c in codes if px[c][j] and px[c][j] > 0]
            idx.append(sum(vals) / len(vals) if vals else 0)
        ma_v = sum(idx) / len(idx) if idx else 0
        cur = idx[-1] if idx else 0
        on = cur > ma_v
        if on and not invested:
            units = {c: (1.0 / len(codes)) / (px[c][i] or 1) for c in codes}
            invested = True
        elif not on and invested:
            units = {}
            invested = False
        v = sum(units.get(c, 0) * (px[c][i] or px[c][i - 1]) for c in codes) if invested else eq[-1]
        eq.append(v)
    return eq


eq = equity_trend_timing(panel, dates, CODES)
t, s, m = stats(eq)
res['C_MA60择时'] = (t, s, m)
print('%-42s %+11.2f%% %10.2f %9.2f%%' % ('C 全宇宙 + MA60 择时（只择时）', t * 100, s, m * 100))

print('-' * 92)
print('%-42s %+11.2f%% %10.2f %9.2f%%' % ('★ 现策略 IS（生产配置，含 0.15% 成本）', 86.92, 1.93, -4.28))
print('=' * 92)
print()
print('结论判据：若 A/B/C 任一显著优于策略 IS，说明策略在基准之上做的是**负贡献**。')

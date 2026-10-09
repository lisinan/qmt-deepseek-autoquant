# -*- coding: utf-8 -*-
"""PM-EVOLVE 第 25 轮：atr_stop_mult × max_single_position_pct 交互实验。

目的（AM 交接 #1）：判定 rpt=0.020 之后 max_single_position_pct=0.19 是否已成为
绑定约束。判据：若放宽上限后 atr 轴的收益梯度显著变陡 / 暴露与胜率不再恒常，
则证明已夹紧。
"""
import sys, time
from dataclasses import replace

sys.path.insert(0, '.')
from strategy._evolve_wf import (wide_universe, preload, aligned_universe,
                                 make_folds, oos_stats, base_cfg,
                                 MARKET_INDEX_CODE, INDEX_CODES, FIXED_WARMUP)
from strategy.backtest_daily import run_backtest

COUNT = 900
FOLD, NFOLDS = 90, 6

codes = wide_universe()
t0 = time.time()
data = preload(codes + [MARKET_INDEX_CODE, "000300.SH"], COUNT)
d0, panel = aligned_universe(data, codes)
data = panel
n = len(d0)
print('[数据] %d 只 x %d 根  %s -> %s  (%.1fs)' % (len(data), n, d0[0], d0[-1], time.time() - t0))


def bt(cfg, dset):
    ks = [k for k in dset.keys() if k not in INDEX_CODES]
    return run_backtest(ks, cfg, count=COUNT, preloaded=dset)


subs = make_folds(data, codes, n, FOLD, NFOLDS)
base = base_cfg()
base_is = bt(base, data)
bs, base_rs = oos_stats(base, subs, bt)
print('\n基线: IS ret=%+.2f%% Sh=%+.2f MDD=%.2f%% | OOS 均值Sh=%+.3f 正收%d/%d '
      '均值MDD=%.2f%% 最差=%.1f%%'
      % (base_is['total_return'] * 100, base_is['sharpe'],
         base_is['max_drawdown'] * 100, bs['mean_sharpe'], bs['pos'], bs['n'],
         bs['mean_mdd'] * 100, bs['worst'] * 100))

ATRS = [2.3, 2.5, 2.7, 2.9, 3.1, 3.3]
CAPS = [0.19, 0.25, 0.35, 0.50, 1.00]

print('\n' + '=' * 132)
print('交互矩阵 (90x6 OOS 均值)')
print('=' * 132)
print('%-8s' % 'atr\\cap' + ''.join('%22s' % ('cap=%.2f' % c) for c in CAPS))
print('%-8s' % '' + ''.join('%22s' % 'Sh/ret/MDD/exp/win' for c in CAPS))
print('-' * 132)

res = {}
for a in ATRS:
    line = '%-8s' % ('%.1f' % a)
    for c in CAPS:
        cfg = replace(base, atr_stop_mult=a, max_single_position_pct=c)
        st, rs = oos_stats(cfg, subs, bt)
        isr = bt(cfg, data)
        res[(a, c)] = dict(sh=st['mean_sharpe'], ret=st['mean_ret'],
                           mdd=st['mean_mdd'], worst=st['worst'],
                           pos=st['pos'], n=st['n'],
                           is_ret=isr['total_return'], is_sh=isr['sharpe'],
                           is_mdd=isr['max_drawdown'],
                           ntr=isr.get('n_trades', 0),
                           exp=isr.get('exposure', 0),
                           win=isr.get('win_rate', 0))
        line += '%22s' % ('%+.3f/%+.1f%%/%.2f%%' % (
            st['mean_sharpe'], st['mean_ret'] * 100, st['mean_mdd'] * 100))
    print(line)

print('\n' + '=' * 132)
print('IS 明细（暴露 / 胜率 / 成交 / 收益）—— 检测夹紧的关键列')
print('=' * 132)
print('%-6s %-8s %10s %9s %9s %8s %8s %9s %9s %9s'
      % ('atr', 'cap', 'IS_ret%', 'IS_Sh', 'IS_MDD%', '成交', '胜率%', '暴露%', 'OOS_Sh', '最差折%'))
print('-' * 132)
for a in ATRS:
    for c in CAPS:
        r = res[(a, c)]
        print('%-6.1f %-8.2f %+9.2f%% %+9.2f %+8.2f%% %8d %8.1f %8.1f %+9.3f %+8.1f%%'
              % (a, c, r['is_ret'] * 100, r['is_sh'], r['is_mdd'] * 100,
                 r['ntr'], r['win'] * 100, r['exp'] * 100, r['sh'], r['worst'] * 100))

print('\n' + '=' * 132)
print('夹紧判据：同一 atr 下 cap 从 0.19 -> 1.00 的 IS 收益增幅')
print('=' * 132)
print('%-6s %14s %14s %14s %14s' % ('atr', 'IS_ret(0.19)', 'IS_ret(1.00)', 'Δ(pt)', '暴露Δ'))
for a in ATRS:
    lo = res[(a, 0.19)]
    hi = res[(a, 1.00)]
    print('%-6.1f %+13.2f%% %+13.2f%% %+13.2f %+13.1f'
          % (a, lo['is_ret'] * 100, hi['is_ret'] * 100,
             (hi['is_ret'] - lo['is_ret']) * 100,
             (hi['exp'] - lo['exp']) * 100))

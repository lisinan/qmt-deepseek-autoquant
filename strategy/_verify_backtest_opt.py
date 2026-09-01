# -*- coding: utf-8 -*-
"""验证 run_backtest 的 O(n^2)->O(n) 预计算优化数值完全等价。

逐索引对比：
  - trend_up_arr[i]   vs daily_trend_up(cl, hi, lo, i)
  - atr_pct_arr[i]    vs atr_pct_at(hi, lo, cl, i)
  - score_arr[i]      vs score_daily(cl[:i+1], hi[:i+1], lo[:i+1], cl[:i+1], vo[:i+1])
若全部一致，则优化后 run_backtest 结果与优化前逐位相同（仅更快）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core.indicators as I  # noqa: E402
from strategy.backtest_daily import (  # noqa: E402
    load_daily, score_daily_series, score_daily,
    daily_trend_up, atr_pct_at, _vec_slope,
)

CODES = ["300308.SZ", "688256.SH", "002415.SZ", "300502.SZ"]


def build_arrays(d):
    o = d["open"]; cl = d["close"]
    hi = d["high"]; lo = d["low"]; vo = d["volume"]
    n_c = len(cl)
    s20 = I.sma(cl, 20); s60 = I.sma(cl, 60); s20s = _vec_slope(s20, 5)
    _, _, mh = I.macd(cl, 12, 26, 9)
    tu = [False] * n_c
    for i in range(n_c):
        if i < 60:
            continue
        m20 = s20[i]; m60 = s60[i]; m20s = s20s[i]
        if (m20 and m60 and cl[i] > m20 > m60 and m20s is not None
                and m20s > 0 and (mh[i] or 0) >= 0):
            tu[i] = True
    atr = I.atr(hi, lo, cl, 14)
    ap = [0.0] * n_c
    for i in range(n_c):
        a = atr[i]
        if a and cl[i]:
            ap[i] = a / cl[i]
    sc = score_daily_series(o, hi, lo, cl, vo)
    return tu, ap, sc


def main():
    max_diff_trend = max_diff_atr = max_diff_score = 0
    bad = 0
    for code in CODES:
        d = load_daily(code, 500)
        if not d or len(d["close"]) < 120:
            print(f"skip {code}: no data")
            continue
        o = d["open"]; cl = d["close"]
        hi = d["high"]; lo = d["low"]; vo = d["volume"]
        tu, ap, sc = build_arrays(d)
        n = len(cl)
        for i in range(n):
            # trend_up
            ref = daily_trend_up(cl, hi, lo, i)
            if ref != tu[i]:
                bad += 1
                print(f"[trend] {code} i={i} arr={tu[i]} fn={ref}")
            # atr
            ref_a = atr_pct_at(hi[:i + 1], lo[:i + 1], cl[:i + 1], i)
            if abs(ref_a - ap[i]) > 1e-12:
                bad += 1
                max_diff_atr = max(max_diff_atr, abs(ref_a - ap[i]))
                if bad < 10:
                    print(f"[atr] {code} i={i} arr={ap[i]:.6f} fn={ref_a:.6f}")
            # score
            ref_s, ref_f = score_daily(cl[:i + 1], hi[:i + 1], lo[:i + 1],
                                      cl[:i + 1], vo[:i + 1])
            if abs(ref_s - sc[i][0]) > 1e-9:
                bad += 1
                max_diff_score = max(max_diff_score, abs(ref_s - sc[i][0]))
                if bad < 10:
                    print(f"[score] {code} i={i} arr={sc[i][0]:.4f} fn={ref_s:.4f}")
            # factor positive count
            ref_pos = sum(1 for x in ref_f.values() if x > 0)
            arr_pos = sum(1 for x in sc[i][1].values() if x > 0)
            if ref_pos != arr_pos:
                bad += 1
                print(f"[pos] {code} i={i} arr={arr_pos} fn={ref_pos}")
    print(f"\n不一致项总数 = {bad}")
    print(f"最大 atr 差 = {max_diff_atr:.2e}  最大 score 差 = {max_diff_score:.2e}")
    if bad == 0:
        print("✅ 预计算数组与逐 bar 函数数值完全一致（run_backtest 结果不变）")
    else:
        print("❌ 存在不一致，需回滚优化")
    return bad


if __name__ == "__main__":
    sys.exit(1 if main() else 0)

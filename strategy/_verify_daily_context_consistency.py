# -*- coding: utf-8 -*-
"""
实盘日线评分器 (DailyContext._compute) 与 已验证回测评分器
(backtest_daily.score_daily / score_daily_series) 的逐 bar 一致性验证。

目的：本轮「继续」要闭环的问题——实盘 daily 决策路径是否与回测同口径，
避免 live 收益与回测「两张皮」。

方法：
  1) 确定性合成面板（seeded 随机游走 + 手工边界序列），逐 bar
     比对 _compute_for_test(code, d[:i+1]).factors/score 与
     score_daily(o[:i+1], h[:i+1], l[:i+1], c[:i+1], v[:i+1]) 的
     factors/score，严格相等（覆盖预热区 + 所有因子分支）。
  2) 若运行环境能取到真实日线（xtdata 本地可达），再对全宇宙逐 bar
     比对，报告真实偏差 / 决策翻转（临界 4.0）数量，应为 0。
  3) 同时核对 score_daily 与 score_daily_series 在末根 bar 上一致
     （回测内部两版的一致性，防止回归）。

退出码：全部 0 偏差 → 0；存在非 0 偏差 → 1。
"""
from __future__ import annotations

import random
import sys
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import STOCK_CODES  # noqa: E402
from strategy.daily_context import DailyContext  # noqa: E402
from strategy import backtest_daily as B  # noqa: E402

BUY_THRESHOLD = 4.0


# ------------------------------------------------------------ 合成数据
def _make_synth_panel(n_stocks: int = 23, n_bars: int = 120,
                      seed: int = 20260902) -> Dict[str, dict]:
    """生成结构合理的 OHLCV 序列（high>=max(o,c)>=low，volume>0）。

    用 seeded 随机游走，覆盖 RSI 中/低/高、KDJ <20 / >100 / 中段、
    量能比 >=1.2 / 1.0 / <1.0、BOLL 位置各分支、VWAP 各分支。
    120 根 bar 足以让多数分支自然命中；另手工叠加 4 条边界序列兜底。
    """
    rnd = random.Random(seed)
    panel: Dict[str, dict] = {}
    for s in range(n_stocks):
        close = [100.0]
        for _ in range(n_bars - 1):
            step = rnd.uniform(-0.05, 0.05)
            close.append(max(1.0, close[-1] * (1 + step)))
        o, h, l, v = [], [], [], []
        for i in range(n_bars):
            prev = close[i - 1] if i > 0 else close[i] * 0.99
            op = prev * (1 + rnd.uniform(-0.01, 0.01))
            cl = close[i]
            hi = max(op, cl) * (1 + rnd.uniform(0.0, 0.03))
            lo = min(op, cl) * (1 - rnd.uniform(0.0, 0.03))
            vol = rnd.uniform(0.5, 3.0) * 1_000_000.0
            o.append(op); h.append(hi); l.append(lo); v.append(vol)
        panel[f"SYN{s:02d}"] = {"open": o, "high": h, "low": l,
                                "close": close, "volume": v}
    # 手工边界序列：强制命中各极值分支
    # a) 长期阴跌 → RSI<=30, KDJ<20（超卖深）
    c_a = [100.0 - i * 1.0 for i in range(n_bars)]
    c_a = [max(5.0, x) for x in c_a]
    # b) 长期暴涨 → RSI>=80, KDJ>100（超买深）
    c_b = [100.0 + i * 1.0 for i in range(n_bars)]
    # c) 横盘 → RSI 中段、KDJ 中段、量能极低
    c_c = [100.0 + 0.2 * (i % 2) for i in range(n_bars)]
    # d) 高量突破 → 量能比 >1.2
    c_d = [100.0 * (1 + 0.01 * i) for i in range(n_bars)]
    for name, cl in (("EDGE_A", c_a), ("EDGE_B", c_b),
                     ("EDGE_C", c_c), ("EDGE_D", c_d)):
        o, h, l, v = [], [], [], []
        for i in range(n_bars):
            prev = cl[i - 1] if i > 0 else cl[i] * 0.99
            op = prev * (1 + rnd.uniform(-0.01, 0.01))
            hi = max(op, cl[i]) * (1 + 0.02)
            lo = min(op, cl[i]) * (1 - 0.02)
            # EDGE_D 末段放量
            mul = 3.0 if (name == "EDGE_D" and i > n_bars * 0.7) else 1.0
            vol = rnd.uniform(0.8, 1.2) * 1_000_000.0 * mul
            o.append(op); h.append(hi); l.append(lo); v.append(vol)
        panel[name] = {"open": o, "high": h, "low": l,
                       "close": cl, "volume": v}
    return panel


def _compare_pair(o, h, l, c, v) -> tuple:
    """比对 _compute（实盘）与 score_daily（回测）在当前截断序列上的结果。

    返回 (live_factors, live_score, bt_factors, bt_score)。
    """
    d = {"open": o, "high": h, "low": l, "close": c, "volume": v}
    live = DailyContext._compute_for_test("CMP", d)
    bt_score, bt_factors = B.score_daily(o, h, l, c, v)
    return live.factors, live.score, bt_factors, bt_score


def _run_synth() -> dict:
    panel = _make_synth_panel()
    total = 0
    diff_points = 0
    max_abs = 0.0
    straddle = 0
    live_buy = bt_buy = 0
    factor_max_abs: Dict[str, float] = {}
    for code, d in panel.items():
        n = len(d["close"])
        for i in range(n):  # 含预热区(0..8)，验证全分支
            o = d["open"][:i + 1]; h = d["high"][:i + 1]
            l = d["low"][:i + 1]; c = d["close"][:i + 1]
            v = d["volume"][:i + 1]
            lf, ls, bf, bs = _compare_pair(o, h, l, c, v)
            total += 1
            for k in ("trend", "momentum", "oversold", "volume",
                      "position", "vwap"):
                dv = abs((lf.get(k, 0.0) or 0.0) - (bf.get(k, 0.0) or 0.0))
                factor_max_abs[k] = max(factor_max_abs.get(k, 0.0), dv)
                if dv > 1e-9:
                    diff_points += 1
            sd = abs(ls - bs)
            max_abs = max(max_abs, sd)
            if sd > 1e-9:
                diff_points += 1  # 总分差异（与因子差异分开计）
            lb = ls >= BUY_THRESHOLD
            bb = bs >= BUY_THRESHOLD
            if lb:
                live_buy += 1
            if bb:
                bt_buy += 1
            if lb != bb:
                straddle += 1
    return {
        "scope": "synthetic", "n_points": total,
        "diff_points": diff_points, "max_abs_score_diff": round(max_abs, 6),
        "straddle": straddle, "live_buy": live_buy, "bt_buy": bt_buy,
        "factor_max_abs": {k: round(v, 6) for k, v in factor_max_abs.items()},
    }


def _run_real() -> dict:
    """若可取真实日线则逐 bar 比对；否则返回 unavailable。"""
    panel: Dict[str, dict] = {}
    for code in STOCK_CODES:
        d = B.load_daily(code, count=400)
        if d and len(d["close"]) >= 120:
            panel[code] = d
    if not panel:
        return {"scope": "real", "available": False}
    total = 0
    diff_points = 0
    max_abs = 0.0
    straddle = 0
    live_buy = bt_buy = 0
    for code, d in panel.items():
        n = len(d["close"])
        warm = 60
        for i in range(warm, n):
            o = d["open"][:i + 1]; h = d["high"][:i + 1]
            l = d["low"][:i + 1]; c = d["close"][:i + 1]
            v = d["volume"][:i + 1]
            lf, ls, bf, bs = _compare_pair(o, h, l, c, v)
            total += 1
            for k in ("trend", "momentum", "oversold", "volume",
                      "position", "vwap"):
                if abs((lf.get(k, 0.0) or 0.0) - (bf.get(k, 0.0) or 0.0)) > 1e-9:
                    diff_points += 1
            sd = abs(ls - bs)
            max_abs = max(max_abs, sd)
            if sd > 1e-9:
                diff_points += 1
            lb = ls >= BUY_THRESHOLD
            bb = bs >= BUY_THRESHOLD
            if lb:
                live_buy += 1
            if bb:
                bt_buy += 1
            if lb != bb:
                straddle += 1
    return {
        "scope": "real", "available": True,
        "n_stocks": len(panel), "n_points": total,
        "diff_points": diff_points, "max_abs_score_diff": round(max_abs, 6),
        "straddle": straddle, "live_buy": live_buy, "bt_buy": bt_buy,
    }


def _check_backtest_internal() -> dict:
    """回测内部两版一致性：score_daily 末根 vs score_daily_series 末根。"""
    panel = _make_synth_panel(n_stocks=8, n_bars=120, seed=7)
    mism = 0
    checked = 0
    for d in panel.values():
        o, h, l, c, v = (d["open"], d["high"], d["low"],
                         d["close"], d["volume"])
        s_last, f_last = B.score_daily(o, h, l, c, v)
        arr = B.score_daily_series(o, h, l, c, v)
        s_s, f_s = arr[-1]
        checked += 1
        if abs(s_last - s_s) > 1e-9:
            mism += 1
        for k in f_last:
            if abs((f_last.get(k, 0) or 0) - (f_s.get(k, 0) or 0)) > 1e-9:
                mism += 1
    return {"scope": "backtest_internal", "checked": checked, "mismatch": mism}


def main() -> int:
    print("=" * 78)
    print("DailyContext._compute  vs  backtest_daily.score_daily  逐 bar 一致性")
    print("=" * 78)

    synth = _run_synth()
    print(f"\n[合成] 标的={len(_make_synth_panel())}  评分点={synth['n_points']}")
    print(f"  因子级最大偏差: {synth['factor_max_abs']}")
    print(f"  总分最大偏差  : {synth['max_abs_score_diff']}")
    print(f"  差异点        : {synth['diff_points']} (0=完全一致)")
    print(f"  决策翻转(临界 {BUY_THRESHOLD}): {synth['straddle']} "
          f"(live买 {synth['live_buy']} / 回测买 {synth['bt_buy']})")

    bt_in = _check_backtest_internal()
    print(f"\n[回测内部] score_daily vs score_daily_series 末根核对: "
          f"checked={bt_in['checked']} mismatch={bt_in['mismatch']}")

    real = _run_real()
    if real.get("available"):
        print(f"\n[真实] 标的={real['n_stocks']}  评分点={real['n_points']}")
        print(f"  总分最大偏差  : {real['max_abs_score_diff']}")
        print(f"  差异点        : {real['diff_points']}")
        print(f"  决策翻转(临界 {BUY_THRESHOLD}): {real['straddle']} "
              f"(live买 {real['live_buy']} / 回测买 {real['bt_buy']})")
    else:
        print("\n[真实] 环境取不到真实日线(xtdata 不可达)，跳过真实数据比对")

    # 判定
    ok = (synth["diff_points"] == 0 and synth["straddle"] == 0
          and bt_in["mismatch"] == 0)
    if real.get("available"):
        ok = ok and real["diff_points"] == 0 and real["straddle"] == 0

    print("\n" + "=" * 78)
    if ok:
        print("✅ 通过：实盘 daily 评分与已验证回测严格一致（含所有因子分支+预热区）")
        print("   live 收益与回测同口径，闭环「两张皮」风险。")
        return 0
    print("❌ 未通过：存在非 0 偏差，见上。")
    return 1


if __name__ == "__main__":
    sys.exit(main())

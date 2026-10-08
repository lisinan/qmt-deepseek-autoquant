# -*- coding: utf-8 -*-
"""
方向 A 验证（Step 2 修正版）：全市场横截面动量 vs AI 宇宙动量

修正了初版的三个缺陷：
  1) 存活者偏差：宇宙按**时点市值**（20230703，回测起点）选 top-N，不用当前市值（未来函数）。
  2) n=156 截断：预加载后过滤掉 <400 根日线的股票（近期上市/数据缺失），避免整窗被截短。
  3) 折对齐：用 align_panel 对齐到统一交易日轴后再切片。

对比口径：只有宇宙不同，策略 = base_cfg()（生产配置）。
"""
from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, ".")
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from config.settings import STOCK_CODES, INDEX_CODES, MARKET_INDEX_CODE
from core.qmt_client import qmt_client
from strategy.backtest_daily import BacktestConfig, run_backtest, align_panel
from strategy.opt_harness import base_cfg, FIXED_WARMUP, wide_universe
from data.tushare_client import tushare_client

COUNT = 750
FOLD = 75
MIN_BARS = 400          # 过滤短历史股票（近期上市）
PT_DATE = "20230703"    # 时点市值选取日（回测起点，无前视）


def full_market_codes(top_n: int):
    """按**时点**总市值（PT_DATE）取 top-N，排除北交所/上市过晚。"""
    if not tushare_client._connect():
        return []
    pro = tushare_client._pro
    df = pro.daily_basic(trade_date=PT_DATE, fields="ts_code,total_mv")
    if df is None or df.empty:
        return []
    df = df[~df["ts_code"].str.endswith(".BJ")]
    df = df.dropna(subset=["total_mv"]).sort_values("total_mv", ascending=False)
    return df.head(top_n)["ts_code"].tolist()


def preload_xt(codes, count=COUNT):
    out = {}
    t0 = time.time()
    for i, code in enumerate(codes):
        raw = qmt_client.get_history(code, period="1d", count=count)
        if not raw or len(raw) < 120:
            qmt_client.download_history(code, period="1d")
            raw = qmt_client.get_history(code, period="1d", count=count)
        if raw and len(raw) >= 120:
            out[code] = {
                "date": [b["ts"].strftime("%Y%m%d") for b in raw],
                "open": [b["open"] for b in raw],
                "high": [b["high"] for b in raw],
                "low": [b["low"] for b in raw],
                "close": [b["close"] for b in raw],
                "volume": [b["volume"] for b in raw],
            }
        if (i + 1) % 100 == 0:
            print(f"  预加载 {i+1}/{len(codes)}  ({time.time()-t0:.0f}s)", flush=True)
    return out


def prepare_universe(codes):
    """预加载 + 过滤短历史 + 对齐到统一交易日轴。返回 (codes, panel, n, dates)。"""
    data = preload_xt(codes, COUNT)
    data = {c: d for c, d in data.items() if len(d["close"]) >= MIN_BARS}
    ks = [c for c in data if c not in INDEX_CODES]
    if not ks:
        return [], {}, 0, []
    dates, panel = align_panel({k: data[k] for k in ks})
    for code in panel:
        panel[code]["date"] = list(dates)
    return ks, panel, len(dates), dates


def bt(codes, cfg, data):
    return run_backtest(codes, cfg, count=COUNT, preloaded=data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=300)
    args = ap.parse_args()

    ai_codes = wide_universe()   # STOCK_CODES ∪ SECTOR_CONFIG（生产基线宇宙）
    fm_codes = full_market_codes(args.top)
    print(f"[宇宙] AI(wide_universe)={len(ai_codes)} 只；全市场时点top{args.top}={len(fm_codes)} 只")

    cfg = base_cfg()
    results = {}
    panels = {}
    for tag, codes in (("A_AI宇宙", ai_codes), ("B_全市场", fm_codes)):
        t0 = time.time()
        ks, panel, n, dates = prepare_universe(codes)
        print(f"[{tag}] 对齐后 {len(ks)} 只 × {n} 根 ({dates[0]}~{dates[-1]})  "
              f"({time.time()-t0:.0f}s)", flush=True)
        if not ks or n < FIXED_WARMUP + FOLD:
            print(f"  !! {tag} 数据不足，跳过")
            continue
        panels[tag] = panel
        results[tag] = dict(codes=ks, panel=panel, n=n, dates=dates)

    if len(results) < 2:
        print("宇宙数据不足，终止")
        return

    # ---- IS 全样本 ----
    print("\n" + "=" * 96)
    print("IS 全样本（同策略，只有宇宙不同）")
    print("=" * 96)
    for tag in ("A_AI宇宙", "B_全市场"):
        r = results[tag]
        is_r = bt(r["codes"], cfg, r["panel"])
        r["is"] = is_r
        if "error" in is_r:
            print(f"{tag:<14} ERROR {is_r['error']}")
        else:
            print(f"{tag:<14} ret={is_r['total_return']*100:+7.2f}% Sh={is_r['sharpe']:+5.2f} "
                  f"MDD={is_r['max_drawdown']*100:6.2f}% Cal={is_r['calmar']:5.2f} "
                  f"n={is_r['n_trades']:>3} win={is_r['win_rate']*100:4.1f}% "
                  f"exp={is_r['exposure']*100:4.0f}% alpha={is_r['alpha']*100:+6.1f}pt")

    # ---- 7 折 walk-forward ----
    print("\n" + "=" * 96)
    print(f"7 折 walk-forward（fold={FOLD}, warmup={FIXED_WARMUP}）")
    print("=" * 96)
    n_common = min(r["n"] for r in results.values())
    starts = list(range(FIXED_WARMUP, n_common - FOLD + 1, FOLD))
    print(f"窗数={len(starts)}（共同窗口 n={n_common}）")

    def slice_panel(panel, lo, hi):
        return {c: {k: list(v[lo:hi]) for k, v in d.items()} for c, d in panel.items()}

    for tag in results:
        results[tag]["folds"] = []
    for s in starts:
        lo, hi = s - FIXED_WARMUP, min(s + FOLD, n_common)
        for tag in ("A_AI宇宙", "B_全市场"):
            r = results[tag]
            dsub = slice_panel(r["panel"], lo, hi)
            results[tag]["folds"].append(bt(r["codes"], cfg, dsub))

    print(f"\n{'宇宙':<14}" + "".join(f"{'F'+str(k+1):>9}" for k in range(len(starts)))
          + f"{'均值Sh':>8}{'正收':>6}{'最差':>8}{'均值MDD':>9}")
    print("-" * 96)
    summary = {}
    for tag in ("A_AI宇宙", "B_全市场"):
        rs = results[tag]["folds"]
        line = f"{tag:<14}"
        shs, mdd, pos, worst = [], [], 0, 999.0
        for r in rs:
            if "error" in r:
                line += f"{'ERR':>9}"
                continue
            line += f"{r['total_return']*100:>+8.1f}%"
            shs.append(r["sharpe"]); mdd.append(r["max_drawdown"])
            if r["total_return"] > 0:
                pos += 1
            worst = min(worst, r["total_return"])
        msh = sum(shs)/len(shs) if shs else 0.0
        mmdd = sum(mdd)/len(mdd) if mdd else 0.0
        line += f"{msh:>+8.2f}{pos:>4}/{len(rs)}{worst*100:>+7.1f}%{mmdd*100:>8.2f}%"
        print(line)
        summary[tag] = dict(mean_sh=msh, pos=pos, n=len(rs), worst=worst, mean_mdd=mmdd)

    a, b = summary["A_AI宇宙"], summary["B_全市场"]
    dsh = b["mean_sh"] - a["mean_sh"]
    print("\n" + "=" * 96)
    print(f"结论：全市场时点top{args.top} vs AI 宇宙  OOS 均值 Sharpe 差 = {dsh:+.3f}")
    print(f"  A_AI宇宙: 均值Sh={a['mean_sh']:+.2f} 正收{a['pos']}/{a['n']} "
          f"最差{a['worst']*100:+.1f}% 均值MDD={a['mean_mdd']*100:.2f}%")
    print(f"  B_全市场: 均值Sh={b['mean_sh']:+.2f} 正收{b['pos']}/{b['n']} "
          f"最差{b['worst']*100:+.1f}% 均值MDD={b['mean_mdd']*100:.2f}%")
    print(f"  判定: {'>>> 全市场更优（继续四窗共识 + top500/800）' if dsh > 0 else '全市场不优于 AI 宇宙（方向 A 初步证伪）'}")


if __name__ == "__main__":
    main()

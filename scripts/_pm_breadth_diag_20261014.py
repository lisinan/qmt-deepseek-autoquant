# -*- coding: utf-8 -*-
"""PM-EVOLVE R31（2026-10-14 收官）：弱市回退机制的**触发频率与强度**诊断。

回答三个问题（决定候选 A 是否真的能救 KPI#4）：
  Q1 过去 N 个交易日里，「绝对门槛下通过数 < 2」的天数占比（= 回退触发率）？
  Q2 触发日里，改用横截面 q 分位门槛后能有几只通过（= 回退后宽度）？
  Q3 最新一根 bar（即今日实盘面对的宇宙）的动量分布与回退后宽度？
另外给出 P0 vs 回退档的 IS 成交/暴露/胜率对照。
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE_CACHE = ROOT / "storage" / "hist_cache" / "ts_panel_25_901_20211201_20261013.pkl"
LB = 60
THR = 0.15
NEED = 2           # min_passing_candidates


def pct(vals, q):
    vs = sorted(vals)
    i = int(round(q / 100.0 * (len(vs) - 1)))
    return vs[i]


def main() -> int:
    panel = pickle.loads(BASE_CACHE.read_bytes())
    sys.path.insert(0, str(ROOT))
    import strategy._evolve_wf as _W
    codes = [c for c in _W.wide_universe()
             if c in panel and c not in _W.INDEX_CODES]
    print(f"[diag] 宇宙 {len(codes)} 只（wide_universe ∩ 面板 − 指数）")

    n = min(len(panel[c]["close"]) for c in codes)
    # 全宇宙按日期对齐：以最短者末段为准（面板已同源，长度不同只差在上市日）
    closes = {c: panel[c]["close"][-n:] for c in codes}
    dates = panel[codes[0]]["date"][-n:]

    stat = {}
    last = None
    for i in range(LB, n):
        raws = []
        for c in codes:
            base = closes[c][i - LB]
            if base <= 0:
                continue
            raws.append(closes[c][i] / base - 1)
        if len(raws) < 5:
            continue
        n_abs = sum(1 for v in raws if v > THR)
        row = {"n_abs": n_abs}
        for q in (90.0, 95.0, 97.0, 99.0):
            p = pct(raws, q)
            t = max(min(THR, p), -9.9)
            row[q] = sum(1 for v in raws if v > t)
        stat[dates[i]] = row
        last = (dates[i], sorted(raws, reverse=True)[:5], row)

    tot = len(stat)
    trig = [d for d, r in stat.items() if r["n_abs"] < NEED]
    print(f"\n样本 {tot} 个交易日（{dates[LB]} ~ {dates[-1]}）")
    print(f"Q1 触发率（通过数 < {NEED}）：{len(trig)}/{tot} = "
          f"{len(trig)/tot*100:.1f}%")
    for q in (90.0, 95.0, 97.0, 99.0):
        ok = sum(1 for d in trig if stat[d][q] >= NEED)
        print(f"Q2 q={q:.0f}：触发日中回退后宽度 ≥{NEED} 的天数 = {ok}/{len(trig)}"
              f" = {ok/max(1,len(trig))*100:.1f}%")

    print("\nQ3 最新 bar：", last[0])
    print("    60日动量 Top5：" + "  ".join(f"{v*100:+.2f}%" for v in last[1]))
    print("    通过数：绝对门槛(>15%)={}  ".format(last[2]["n_abs"])
          + "  ".join("q{}={}".format(int(q), last[2][q])
                      for q in (90.0, 95.0, 97.0, 99.0)))

    # ---- IS 成交/暴露对照 ----
    import strategy._evolve_wf as W
    from strategy.backtest_daily import run_backtest

    def _fake_load_daily(code: str, count: int = 260):
        d = panel.get(code)
        if not d:
            return None
        m = len(d["close"])
        k = min(m, max(count, 120))
        return {kk: list(vv[-k:]) for kk, vv in d.items()}

    W.load_daily = _fake_load_daily
    W._CACHE.clear()
    d0, aligned = W.aligned_universe({c: _fake_load_daily(c, 1200) for c in panel},
                                     list(panel))
    ks = [k for k in aligned.keys() if k not in W.INDEX_CODES]
    base = W.base_cfg()
    print("\n【IS 对照】全样本 900 根")
    print(f"{'配置':<26}{'收益%':>10}{'Sh':>7}{'MDD%':>9}{'成交':>7}{'胜率%':>8}"
          f"{'持仓d':>7}{'暴露%':>8}")
    for name, cfg in (("P0_现状(关闭)", base),
                      ("Y_q95", W.replace(base, mom_rel_fallback=95.0)),
                      ("Y_q90", W.replace(base, mom_rel_fallback=90.0))):
        r = run_backtest(ks, cfg, count=900, preloaded=aligned)
        print(f"{name:<26}{r['total_return']*100:>+9.2f}%{r['sharpe']:>+7.2f}"
              f"{r['max_drawdown']*100:>8.2f}%{r['n_trades']:>6}"
              f"{r['win_rate']*100:>7.1f} {r['avg_hold']:>6.1f}"
              f"{r['exposure']*100:>7.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

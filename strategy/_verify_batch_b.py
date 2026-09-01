# -*- coding: utf-8 -*-
"""批次 B 验证：T+1 约束的影响 + 动量闸门作用域(all vs static) 的对照。

回答两个问题：
  Q1  回测原本允许「建仓当日即止损/破位卖出」（A 股 T+1 下不可能）。
      修正后收益/Sharpe/回撤各变化多少？此前所有已验证结论要打几折？
  Q2  批次 A 把动量闸门从「仅静态池排名、动态池无条件放行」改为「静态+动态
      统一横截面排名」，这是策略语义变更。用同一套 walk-forward 折验证两种
      口径，看是改善还是恶化。
      注意：回测的候选池 = wide_universe()（静态 ∪ SECTOR_CONFIG），本身
      没有「动态池」概念，因此 Q2 只能用**候选池规模**来代理：
        static 口径 ≈ 在 12 只静态池里取 top-N
        all    口径 ≈ 在 23 只全池里取 top-N
      这不是完美等价，但能隔离出「排名基数变大」这一个变量。

用法（conda env qmt）：
    python strategy/_verify_batch_b.py --count 750 --folds 7
输出：logs/verify_batch_b.json + 控制台表格
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from config.settings import BASE_DIR, STOCK_CODES        # noqa: E402
from strategy.backtest_daily import run_backtest          # noqa: E402
from strategy.opt_harness import (                        # noqa: E402
    base_cfg, preload, slice_by_index, wide_universe,
)

OUT = BASE_DIR / "logs" / "verify_batch_b.json"


def _metrics(r: dict) -> dict:
    if not r or "error" in r:
        return {"error": (r or {}).get("error", "unknown")}
    return {
        "ret": round(r.get("total_return", 0.0) * 100, 2),
        "sharpe": round(r.get("sharpe", 0.0), 2),
        "mdd": round(r.get("max_drawdown", 0.0) * 100, 2),
        "alpha": round(r.get("alpha", 0.0) * 100, 2),
        "trades": r.get("n_trades", 0),
        "winrate": round(r.get("win_rate", 0.0) * 100, 1),
    }


def _fold_bounds(n: int, folds: int, warmup: int):
    """把 [warmup, n) 均分为 folds 段，返回 [(lo, hi), ...]（含各自 warmup）。"""
    usable = n - warmup
    if usable <= 0 or folds <= 0:
        return []
    seg = usable // folds
    out = []
    for k in range(folds):
        hi = warmup + seg * (k + 1) if k < folds - 1 else n
        out.append((0, hi))          # 从头带 warmup，只移动右端点（滚动扩窗）
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=750, help="日线根数")
    ap.add_argument("--folds", type=int, default=7)
    args = ap.parse_args()

    codes = wide_universe()
    static_codes = sorted(STOCK_CODES)
    print(f"[load] 候选池 {len(codes)} 只（静态 {len(static_codes)} 只），"
          f"count={args.count} ...")
    data = preload(codes, args.count)
    if not data:
        print("[error] 无日线数据，请确认 miniQMT 已启动")
        return 1
    n = min(len(d["close"]) for d in data.values())
    print(f"[load] 完成：{len(data)} 只，对齐长度 {n}")

    base = base_cfg()
    warmup = max(120, int(base.min_warmup or 0))

    # ---------- Q1: T+1 的影响（全样本）----------
    print("\n" + "=" * 78)
    print("Q1  T+1 约束的影响（全样本 full-sample）")
    print("=" * 78)
    variants = {
        "T+0(旧,错误)": replace(base, t1_restriction=False),
        "T+1(修正后)": replace(base, t1_restriction=True),
    }
    q1 = {}
    for tag, cfg in variants.items():
        r = run_backtest(codes, cfg, count=args.count, preloaded=data)
        q1[tag] = _metrics(r)
        print(f"  {tag:<14} " + "  ".join(
            f"{k}={v}" for k, v in q1[tag].items()))
    if "error" not in q1["T+1(修正后)"] and "error" not in q1["T+0(旧,错误)"]:
        a, b = q1["T+0(旧,错误)"], q1["T+1(修正后)"]
        print(f"\n  → T+1 修正的代价: ret {a['ret']:+.2f}% → {b['ret']:+.2f}% "
              f"({b['ret'] - a['ret']:+.2f}pt) | "
              f"Sharpe {a['sharpe']} → {b['sharpe']} "
              f"({b['sharpe'] - a['sharpe']:+.2f}) | "
              f"MDD {a['mdd']}% → {b['mdd']}% ({b['mdd'] - a['mdd']:+.2f}pt)")
        if a["ret"] != 0:
            print(f"  → 此前所有基于 T+0 的已验证结论，收益需打约 "
                  f"{b['ret'] / a['ret'] * 100:.0f}% 折扣")

    # ---------- Q2: 动量闸门作用域（walk-forward 多折）----------
    print("\n" + "=" * 78)
    print(f"Q2  动量闸门作用域 walk-forward（{args.folds} 折滚动扩窗，均含 T+1）")
    print("=" * 78)
    bounds = _fold_bounds(n, args.folds, warmup)
    t1 = replace(base, t1_restriction=True)
    scopes = {
        "static(12只池)": (static_codes, t1),
        "all(23只池)": (codes, t1),
    }
    q2 = {k: [] for k in scopes}
    print(f"  {'fold':<6}{'bars':<7}" + "".join(f"{k:<26}" for k in scopes))
    for fi, (lo, hi) in enumerate(bounds, 1):
        sl = slice_by_index(data, lo, hi)
        row = f"  {fi:<6}{hi - lo:<7}"
        for tag, (pool, cfg) in scopes.items():
            sub = {c: d for c, d in sl.items() if c in pool}
            r = run_backtest(list(sub.keys()), cfg, count=args.count,
                             preloaded=sub)
            m = _metrics(r)
            q2[tag].append(m)
            row += (f"{'ERR':<26}" if "error" in m else
                    f"{('ret%+.1f%% Sh%+.2f α%+.1f' % (m['ret'], m['sharpe'], m['alpha'])):<26}")
        print(row)

    print("\n  折均值：")
    summary = {}
    for tag, rows in q2.items():
        ok = [m for m in rows if "error" not in m]
        if not ok:
            print(f"    {tag:<16} 全部失败")
            continue
        avg = {k: round(sum(m[k] for m in ok) / len(ok), 2)
               for k in ("ret", "sharpe", "mdd", "alpha")}
        pos_alpha = sum(1 for m in ok if m["alpha"] > 0)
        summary[tag] = {**avg, "pos_alpha": f"{pos_alpha}/{len(ok)}",
                        "folds": len(ok)}
        print(f"    {tag:<16} ret {avg['ret']:+7.2f}%  Sharpe {avg['sharpe']:+5.2f}  "
              f"MDD {avg['mdd']:+7.2f}%  α {avg['alpha']:+6.2f}pt  "
              f"正α {pos_alpha}/{len(ok)} 折")

    if len(summary) == 2:
        s, a = summary.get("static(12只池)"), summary.get("all(23只池)")
        if s and a:
            print(f"\n  → all 相对 static: Sharpe {a['sharpe'] - s['sharpe']:+.2f}, "
                  f"ret {a['ret'] - s['ret']:+.2f}pt, α {a['alpha'] - s['alpha']:+.2f}pt, "
                  f"MDD {a['mdd'] - s['mdd']:+.2f}pt")
            verdict = ("all 更优 → 保留 momentum_scope='all'"
                       if a["sharpe"] >= s["sharpe"]
                       else "static 更优 → 建议把 momentum_scope 改回 'static'")
            print(f"  → 结论: {verdict}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(
        {"count": args.count, "folds": args.folds, "n_bars": n,
         "n_codes": len(data), "q1_t1_impact": q1,
         "q2_scope_folds": q2, "q2_summary": summary},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[out] {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

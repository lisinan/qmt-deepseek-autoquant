# -*- coding: utf-8 -*-
"""
研究脚本：实时 RiskManager −10% 全局暂停 vs 回测收益（"RiskManager 协调"缺口）

背景（2026-08-29 L 轮标记的独立研究项）：
  生产配置（regime 关 + trend + vol + max_positions=5）下，risk_per_trade 是唯一
  经 IS/OOS 显示单调改善的参数（0.01→0.04，OOS Sharpe +1.76→+1.92），但 0.03/0.04
  的 MDD ~−20% 会撞上实时 RiskManager 的 max_drawdown_pct=−0.10 全局暂停。

  关键建模缺口：回测从未建模该暂停。而实时引擎 RiskManager.on_asset_update 在
  组合净值自峰值回撤 <= −10% 时**永久熔断**（halted=True，只拦新开仓、不平已有、
  且无自动恢复）。换言之，live 里一旦回撤 −10%，策略变成"只出不进"的僵尸，再也
  无法捕捉反弹——这正是 risk_per_trade 放大后回测 Sharpe 在实盘可能无法兑现的根因。

  本脚本在回测中忠实复现该行为（rm_dd_pause_pct），并对比三种情形：
    1) OFF      : 回测原状（无暂停），衡量"理想 Sharpe"
    2) LIVE     : 忠实建模实盘（永久熔断，无恢复），衡量"实盘可兑现 Sharpe"
    3) RECOVER  : 建议修复方向（净值创新高自动恢复），量化"永久熔断"的机会成本

  然后按 IS/OOS + 多折 walk-forward 严谨裁决：
    · 若 LIVE 模式下各 risk_per_trade 的 MDD 均 < −10%（暂停从不触发）→ 提升
      risk_per_trade 安全且显著改善收益，暂停仅作安全网 → 建议并入。
    · 若 LIVE 模式下高 risk_per_trade 触发永久熔断（rm_halted_ever=True）→ 实盘
      无法兑现回测收益 → 暂停是危险潜伏 bug → 建议改为 RECOVER 式可恢复断路器，
      而非简单提升 risk_per_trade。

用法：
    python strategy/_opt_rmpause.py
"""
from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy.opt_harness import (  # noqa: E402
    wide_universe, preload, slice_by_index, base_cfg, FIXED_WARMUP,
)
from strategy.backtest_daily import run_backtest  # noqa: E402
from config.settings import INDEX_CODES  # noqa: E402

COUNT = 750
FOLD = 75
IS_END = 350

PAUSE_MODES = {
    "OFF":     dict(rm_dd_pause_pct=0.0,  rm_dd_pause_recoverable=False),
    "LIVE":    dict(rm_dd_pause_pct=-0.10, rm_dd_pause_recoverable=False),
    "RECOVER": dict(rm_dd_pause_pct=-0.10, rm_dd_pause_recoverable=True),
}
RPT_LEVELS = [0.015, 0.02, 0.025, 0.03, 0.04, 0.05]


def _g(c, **kw):
    return replace(c, **kw)


def main():
    codes = wide_universe()
    t0 = time.time()
    data = preload(codes + ["399006.SZ", "000300.SH"], COUNT)
    print(f"[数据] {len(data)} 只 × {COUNT} 根日线  载入 {time.time()-t0:.1f}s")
    if not data:
        print("无数据，退出")
        return
    n = min(len(d["close"]) for d in data.values())
    is_end = min(IS_END, n - 120)
    oos_lo = max(0, is_end - FIXED_WARMUP)
    is_data = slice_by_index(data, 0, is_end)
    oos_data = slice_by_index(data, oos_lo, n)
    print(f"[窗口] 总 {n} 根 | IS=[{FIXED_WARMUP},{is_end}) | "
          f"OOS=[{is_end},{n}) 无重叠")
    print(f"       IS {is_data[codes[0]]['date'][0]}→{is_data[codes[0]]['date'][-1]}")
    print(f"       OOS {oos_data[codes[0]]['date'][0]}→{oos_data[codes[0]]['date'][-1]}")

    folds = []
    starts = list(range(FIXED_WARMUP, n - FOLD + 1, FOLD))
    for s in starts:
        lo, hi = s - FIXED_WARMUP, min(s + FOLD, n)
        folds.append((s, slice_by_index(data, lo, hi)))
    print(f"[folds] {len(folds)} 个窗口")

    def bt(cfg, dset):
        ks = [k for k in dset.keys() if k not in INDEX_CODES]
        return run_backtest(ks, cfg, count=COUNT, preloaded=dset)

    base = base_cfg()

    def fmt(tag, r):
        if not r or "error" in r:
            return f"{tag:<26} ERROR {r.get('error','') if r else ''}"
        flag = " [HALTED]" if r.get("rm_halted_ever") else ""
        return (f"{tag:<26} ret={r['total_return']*100:+7.2f}% "
                f"Sh={r['sharpe']:+5.2f} MDD={r['max_drawdown']*100:7.2f}% "
                f"a={r['alpha']*100:+7.2f}pt exp={r['exposure']*100:4.1f}% "
                f"halt={r['rm_halted_days']}d{flag}")

    # ---- 主扫描：risk_per_trade × pause_mode ----
    out_rows = []
    print("\n" + "=" * 150)
    print("主扫描：risk_per_trade × 暂停模式（IS / OOS）")
    print("=" * 150)
    for rpt in RPT_LEVELS:
        print(f"\n### risk_per_trade = {rpt} ###")
        for mode, kw in PAUSE_MODES.items():
            cfg = _g(base, risk_per_trade=rpt, **kw)
            ri = bt(cfg, is_data)
            ro = bt(cfg, oos_data)
            tag = f"rpt={rpt} {mode}"
            print("  " + fmt(tag + " IS", ri))
            print("  " + fmt(tag + " OOS", ro))
            out_rows.append({
                "risk_per_trade": rpt, "pause_mode": mode,
                "IS": {k: ri.get(k) for k in
                       ("total_return", "sharpe", "max_drawdown", "alpha",
                        "exposure", "rm_halted_ever", "rm_halted_days")},
                "OOS": {k: ro.get(k) for k in
                        ("total_return", "sharpe", "max_drawdown", "alpha",
                         "exposure", "rm_halted_ever", "rm_halted_days")},
            })

    # ---- 多折 walk-forward（仅 LIVE 与 RECOVER 对比，聚焦"实盘可兑现"）----
    print("\n" + "=" * 150)
    print("多折 walk-forward：聚焦实盘可兑现性（LIVE vs RECOVER，rm_pct=−0.10）")
    print("=" * 150)
    fold_summary = {}
    for mode, kw in (("LIVE", PAUSE_MODES["LIVE"]),
                     ("RECOVER", PAUSE_MODES["RECOVER"])):
        print(f"\n#### 暂停模式 = {mode} ####")
        print(f"{'rpt':<8}" + "".join(f"{'F'+str(k+1):>13}"
                                      for k in range(len(folds)))
              + f"{'均值Sh':>9}{'正α':>7}{'触发折':>8}{'平均MDD':>9}")
        for rpt in RPT_LEVELS:
            cfg = _g(base, risk_per_trade=rpt, **kw)
            shs, pos_a, halt_folds, mdds = [], 0, 0, []
            line = f"{rpt:<8}"
            for s, dsub in folds:
                r = bt(cfg, dsub)
                if "error" in r:
                    line += f"{'ERR':>13}"
                    continue
                line += f"{r['total_return']*100:>+12.1f}%"
                shs.append(r["sharpe"])
                if r["alpha"] > 0:
                    pos_a += 1
                mdds.append(r["max_drawdown"])
                if r.get("rm_halted_ever"):
                    halt_folds += 1
            msh = sum(shs) / len(shs) if shs else 0
            amd = sum(mdds) / len(mdds) if mdds else 0
            line += f"{msh:>+9.2f}{pos_a:>5}/{len(shs)}{halt_folds:>6}/{len(shs)}{amd*100:>+8.1f}%"
            print(line)
            fold_summary.setdefault(rpt, {})[mode] = {
                "mean_sharpe": msh, "pos_alpha": pos_a, "n_folds": len(shs),
                "halt_folds": halt_folds, "mean_mdd": amd,
            }

    # ---- 保存 ----
    out = ROOT / "logs" / "opt_rmpause.json"
    out.parent.mkdir(exist_ok=True)
    payload = {
        "count": COUNT, "is_end": is_end, "n": n,
        "is_dates": [is_data[codes[0]]['date'][0], is_data[codes[0]]['date'][-1]],
        "oos_dates": [oos_data[codes[0]]['date'][0], oos_data[codes[0]]['date'][-1]],
        "folds": [{"start": s, "date_from": data[codes[0]]['date'][s],
                   "date_to": data[codes[0]]['date'][min(s+FOLD, n)-1]}
                  for s, _ in folds],
        "rpt_levels": RPT_LEVELS, "pause_modes": list(PAUSE_MODES.keys()),
        "scan": out_rows, "fold_summary": fold_summary,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"\n[保存] {out}")


if __name__ == "__main__":
    main()

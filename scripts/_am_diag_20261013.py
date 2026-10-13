# -*- coding: utf-8 -*-
"""AM-EVOLVE R28（2026-10-13）诊断：实盘「动量闸门后候选池为空」是否真实。

今日上午日志 7 次（09:15~10:15，数据源=xtdata 真实）：

    无入场信号：动量闸门后候选池为空（当前宇宙无 60 日正动量标的）

绑定约束与过去 5 个交易日不同（此前是「评分 4.0 阈值」）。
本脚本用 tushare 独立数据源复核：截至最近交易日，实盘宇宙（静态 12 + 动态活跃 N）
里到底有几只 60 日动量为正。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, ROOT)

from config.settings import STOCK_CODES  # noqa: E402

END = "20261013"
START = "20211201"


def live_universe() -> list:
    """实盘订阅宇宙 = 静态 STOCK_CODES + 动态活跃池。"""
    codes = set(STOCK_CODES)
    dyn = []
    try:
        d = json.load(open(ROOT / "storage" / "dynamic_universe.json", encoding="utf-8"))
        for _ind, items in (d.get("by_industry") or {}).items():
            for it in items:
                c = it.get("code") if isinstance(it, dict) else it
                if c:
                    dyn.append(c)
    except Exception as e:  # noqa: BLE001
        print("dynamic universe load fail:", e)
    # active_pool_size=30 按市值 Top-N：文件里 by_industry 已含全部，取前 30 不可靠
    # ⇒ 直接用全量并标注
    return sorted(codes | set(dyn)), sorted(codes), sorted(set(dyn))


def mom(close: list, n: int = 60) -> float:
    if len(close) <= n:
        return 0.0
    return close[-1] / close[-1 - n] - 1.0


def main() -> int:
    from scripts._pm_ts_panel_20261012 import load_ts_panel
    all_codes, static, dyn = live_universe()
    print(f"[宇宙] 静态 {len(static)} + 动态 {len(dyn)} = 合计 {len(all_codes)}")
    panel = load_ts_panel(all_codes, count=901, end=END, start=START)
    print(f"[面板] {len(panel)} 只")
    if not panel:
        return 2
    sample = list(panel)[0]
    print(f"[末根] {sample} {panel[sample]['date'][-1]} "
          f"bars={len(panel[sample]['close'])}")

    rows = []
    for c in sorted(panel):
        cl = panel[c]["close"]
        rows.append((c, mom(cl, 60), mom(cl, 20), panel[c]["date"][-1], len(cl)))
    pos60 = [r for r in rows if r[1] > 0]
    print(f"\n[60日动量>0] {len(pos60)} / {len(rows)} 只")
    rows.sort(key=lambda x: -x[1])
    for c, m60, m20, d, n in rows[:20]:
        print(f"  {c}  60d={m60*100:+6.2f}%  20d={m20*100:+6.2f}%  末根={d} n={n}")
    print("  ...")
    for c, m60, m20, d, n in rows[-5:]:
        print(f"  {c}  60d={m60*100:+6.2f}%  20d={m20*100:+6.2f}%  末根={d} n={n}")

    # 只算「实盘真正订阅的」= 静态 + 活跃前 30
    print("\n[注] 若上面 60d>0 只数 > 0，则实盘「宇宙无 60 日正动量标的」为"
          "**假阴性**（数据/实现问题），须继续定位；若 = 0 则为真实市场状态。")
    out = {c: {"mom60": r[1], "mom20": r[2], "last": r[3], "n": r[4]}
           for c, *r in [(x[0], x[1], x[2], x[3], x[4]) for x in rows]}
    (ROOT / "reports" / "_am_momentum_diag_20261013.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

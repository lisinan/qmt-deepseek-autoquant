# -*- coding: utf-8 -*-
"""AM-EVOLVE R28（2026-10-13）：用 tushare 真实日线面板驱动自研验证器 _evolve_wf。

与 R27 的 `_pm_run_evolve_20261012.py` 同构，但面板末根推进到 **20261013**。

背景：本自动化 shell 内 xtdata 不可达（miniQMT 未运行）⇒ qmt_client 回落 mock ⇒
`load_daily` 返回合成价且 date 全为当天 ⇒ `aligned_universe` 后只剩 1 根，验证器崩溃。
这里把 tushare 真实面板注入 load_daily，恢复回测能力（项目双源方案本来就允许）。

用法：
    python scripts/_am_run_evolve_20261013.py --mode compare
    python scripts/_am_run_evolve_20261013.py --mode grid --param X --values a,b,c
    python scripts/_am_run_evolve_20261013.py --mode consensus
    python scripts/_am_run_evolve_20261013.py --mode span
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import (  # noqa: E402
    STOCK_CODES, SECTOR_CONFIG, MARKET_INDEX_CODE)
from scripts._pm_ts_panel_20261012 import load_ts_panel  # noqa: E402

END = "20261013"
START = "20211201"

s = set(STOCK_CODES)
for _k, v in SECTOR_CONFIG["sectors"].items():
    for c, _n in v["stocks"]:
        s.add(c)
s.add(MARKET_INDEX_CODE)
s.add("000300.SH")
CODES = sorted(s)

PANEL = load_ts_panel(CODES, count=901, end=END, start=START)


def _fake_load_daily(code: str, count: int = 260):
    d = PANEL.get(code)
    if not d:
        return None
    n = len(d["close"])
    k = min(n, max(count, 120))
    return {kk: list(vv[-k:]) for kk, vv in d.items()}


def main() -> int:
    import strategy._evolve_wf as W
    W.load_daily = _fake_load_daily       # 注入真实面板
    W._CACHE.clear()
    argv = sys.argv[1:]
    if not argv:
        argv = ["--mode", "compare"]
    if "--count" not in argv:
        argv += ["--count", "901"]
    sys.argv = ["_evolve_wf.py"] + argv
    return W.main()


if __name__ == "__main__":
    sys.exit(main())

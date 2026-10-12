# -*- coding: utf-8 -*-
"""PM-EVOLVE R27：在本 shell（xtdata 不可达）内用 tushare 真实面板驱动回归门禁。

本 shell 内 ``core.qmt_client`` 回落 mock ⇒ ``load_daily`` 返回合成价且 date
全为当天 ⇒ 所有依赖真实行情的用例（walk-forward / span / proxy 系列）都因
「0 折、1 根数据」而失败。这里在**导入测试模块之前**把真实 tushare 面板注入
``load_daily``，从而还原这些用例的真实语义。

用途：**证明失败是环境（xtdata 不可达）所致，而非本轮代码改动所致**。
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

s = set(STOCK_CODES)
for _k, v in SECTOR_CONFIG["sectors"].items():
    for c, _n in v["stocks"]:
        s.add(c)
s.add(MARKET_INDEX_CODE)
s.add("000300.SH")
CODES = sorted(s)

PANEL = load_ts_panel(CODES)


def _fake_load_daily(code: str, count: int = 260):
    d = PANEL.get(code)
    if not d:
        return None
    n = len(d["close"])
    k = min(n, max(count, 120))
    return {kk: list(vv[-k:]) for kk, vv in d.items()}


def main() -> int:
    import strategy.backtest_daily as B
    B.load_daily = _fake_load_daily
    import strategy._evolve_wf as W
    W.load_daily = _fake_load_daily
    W._CACHE.clear()
    import run_all as R
    return R.main()


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "tests"))
    sys.exit(main())

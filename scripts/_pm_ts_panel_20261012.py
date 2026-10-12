# -*- coding: utf-8 -*-
"""PM-EVOLVE R27（2026-10-12 收官）真实数据源兜底。

背景：本自动化 shell 内 **xtdata 不可达**（``miniQMT probe failed: 无法连接
xtquant服务``）⇒ ``core.qmt_client`` 回落 mock ⇒ ``load_daily`` 返回的是
**合成价、且 date 全为当天** ⇒ ``aligned_universe`` 按日期对齐后只剩 1 根，
``_evolve_wf`` 直接崩溃（``[数据] 23 只 × 1 根``）。

记忆里「xtdata 在自动化 shell 内可达」已失效（miniQMT 未运行）。

兜底：项目本来就是「**tushare 为主数据、xtdata 为实盘**」双源方案
（data/tushare_client.py）。这里用 tushare ``pro.daily`` 拉真实日线构造面板，
并 monkeypatch 进 ``strategy._evolve_wf.load_daily``，使回测跑在真实行情上。

用法（不直接跑，供 _pm_run_evolve_20261012.py 调用）：
    panel = load_ts_panel(codes, count)
"""
from __future__ import annotations

import json
import pickle
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CACHE_DIR = ROOT / "storage" / "hist_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _ts_code(code: str) -> str:
    return code  # 已是 603986.SH / 000977.SZ 形式


def fetch_one(pro, code: str, start: str, end: str):
    import tushare as ts  # noqa: F401
    for attempt in range(4):
        try:
            df = pro.daily(ts_code=_ts_code(code), start_date=start,
                           end_date=end)
            if df is not None and len(df):
                return df
            return None
        except Exception as e:  # noqa: BLE001
            if attempt == 3:
                print(f"  tushare FAIL {code}: {e}")
                return None
            time.sleep(2.5 * (attempt + 1))
    return None


def load_ts_panel(codes, count: int = 901, end: str = "20261012",
                  start: str = "20211201", refresh: bool = False):
    """返回 {code: {date/open/high/low/close/volume}}（**按日期升序**）。"""
    key = f"{len(codes)}_{count}_{start}_{end}"
    cf = CACHE_DIR / f"ts_panel_{key}.pkl"
    if cf.exists() and not refresh:
        return pickle.loads(cf.read_bytes())

    from data.tushare_client import tushare_client as T
    import tushare as ts
    ts.set_token(T.token)
    pro = ts.pro_api(timeout=30)

    out = {}
    for i, c in enumerate(codes):
        df = fetch_one(pro, c, start, end)
        if df is None:
            print(f"  [skip] {c} 无 tushare 数据")
            continue
        df = df.sort_values("trade_date")
        out[c] = {
            "date": [str(x) for x in df["trade_date"].tolist()],
            "open": [float(x) for x in df["open"].tolist()],
            "high": [float(x) for x in df["high"].tolist()],
            "low": [float(x) for x in df["low"].tolist()],
            "close": [float(x) for x in df["close"].tolist()],
            # tushare vol 单位是「手」，xtdata 是「股」⇒ ×100 对齐量纲
            # （量纲只影响量比/量能类因子的**比值**，故其实无影响；此处仅为口径整洁）
            "volume": [float(x) * 100.0 for x in df["vol"].tolist()],
        }
        if (i + 1) % 5 == 0:
            print(f"  fetched {i+1}/{len(codes)}")
        time.sleep(0.35)

    cf.write_bytes(pickle.dumps(out))
    print(f"cached -> {cf}  ({len(out)} codes)")
    return out


if __name__ == "__main__":
    from config.settings import STOCK_CODES, SECTOR_CONFIG, MARKET_INDEX_CODE
    s = set(STOCK_CODES)
    for _k, v in SECTOR_CONFIG["sectors"].items():
        for c, _n in v["stocks"]:
            s.add(c)
    s.add(MARKET_INDEX_CODE)
    s.add("000300.SH")
    codes = sorted(s)
    p = load_ts_panel(codes, refresh="--refresh" in sys.argv)
    for c in list(p)[:3]:
        d = p[c]
        print(c, len(d["close"]), d["date"][0], d["date"][-1],
              round(d["close"][-1], 2))

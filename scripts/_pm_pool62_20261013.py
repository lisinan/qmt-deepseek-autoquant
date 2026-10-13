# -*- coding: utf-8 -*-
"""PM-EVOLVE R29：把**历史上所有进过动态池**的标的（62 只）补齐到面板。

`_pm_mvmember_20261013.py` 重建出的逐期成员共 **62 只**（今天只有 30 只在池），
而回测面板只有 41 只 ⇒ 缺 21+ 只「曾经很大、后来缩水」的标的。它们历史上
会与今日赢家**争夺 Top-3 名额**，缺失会系统性**高估**扩宇宙的效果
（幸存者偏差的另一半）。本脚本把它们补齐。

输出：storage/hist_cache/ts_panel_pool62_20211201_20261013.pkl
"""
from __future__ import annotations

import pickle
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CACHE_DIR = ROOT / "storage" / "hist_cache"
UNION = CACHE_DIR / "ts_panel_union41_20211201_20261013.pkl"
MEM = CACHE_DIR / "mv_member_20261013.pkl"
OUT = CACHE_DIR / "ts_panel_pool62_20211201_20261013.pkl"
END = "20261013"
START = "20211201"


def main() -> int:
    panel = pickle.loads(UNION.read_bytes())
    mem = pickle.loads(MEM.read_bytes())
    need = sorted(set(mem["first_hit"]) - set(panel))
    print(f"[面板] 现有 {len(panel)} 只；历史池成员 {len(mem['first_hit'])} 只"
          f" ⇒ 需补拉 {len(need)} 只")
    if need:
        from data.tushare_client import tushare_client as T
        import tushare as ts
        from scripts._pm_ts_panel_20261012 import fetch_one
        ts.set_token(T.token)
        pro = ts.pro_api(timeout=30)
        for i, c in enumerate(need):
            df = fetch_one(pro, c, START, END)
            if df is None:
                print(f"  [skip] {c}")
                continue
            df = df.sort_values("trade_date")
            panel[c] = {
                "date": [str(x) for x in df["trade_date"].tolist()],
                "open": [float(x) for x in df["open"].tolist()],
                "high": [float(x) for x in df["high"].tolist()],
                "low": [float(x) for x in df["low"].tolist()],
                "close": [float(x) for x in df["close"].tolist()],
                "volume": [float(x) * 100.0 for x in df["vol"].tolist()],
            }
            if (i + 1) % 10 == 0:
                print(f"  fetched {i+1}/{len(need)}")
            time.sleep(0.3)
        OUT.write_bytes(pickle.dumps(panel))
        print(f"[面板] 已缓存 {len(panel)} 只 -> {OUT}")
    # 统计：62 只成员里有多少已有数据
    have = [c for c in mem["first_hit"] if c in panel]
    print(f"[面板] 历史池成员可回测 {len(have)}/{len(mem['first_hit'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

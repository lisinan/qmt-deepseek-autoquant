# -*- coding: utf-8 -*-
"""PM-EVOLVE R29：动态活跃池的**无前视**成员重建（按历史总市值 Top-30）。

实盘口径（`data/dynamic_universe.py::_pick_active_pool`）：
    374 只 AI 产业链股（通信设备/半导体/IT设备）中按 **total_mv 取 Top-30**。

直接把「今天的 30 只」塞进 2023 年的回测 = **成员前视 + 幸存者偏差**
（今天的赢家被回填到过去）。本脚本用 tushare `daily_basic(trade_date=...)`
（全市场快照，实测可用，单次 ~5200 行）重建**逐期**成员：

    - 采样日：面板交易日轴上每 21 根取一个（≈每月）
    - 成员：该采样日在 374 只里 total_mv 排名前 30 的代码
    - 每只标的的「入池日」= 它第一次进入前 30 的**采样日**（此后默认持续在池）

输出：{code: 入池日 YYYYMMDD 或 None}，缓存到 storage/hist_cache。
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
OUT = CACHE_DIR / "mv_member_20261013.pkl"
POOL_SIZE = 30
STEP = 21


def load_industry_universe() -> set:
    d = json.loads((ROOT / "storage" / "dynamic_universe.json").read_text(encoding="utf-8"))
    return set((d.get("universe") or {}).keys())


def main() -> int:
    if OUT.exists() and "--refresh" not in sys.argv:
        mem = pickle.loads(OUT.read_bytes())
        print(f"[缓存] {OUT}  {len(mem)} 只")
    else:
        base = pickle.loads((CACHE_DIR / "ts_panel_25_901_20211201_20261013.pkl").read_bytes())
        dates = base["300308.SZ"]["date"]
        universe = load_industry_universe()
        print(f"[成员] 行业宇宙 {len(universe)} 只，交易日 {dates[0]}~{dates[-1]}")

        from data.tushare_client import tushare_client as T
        import tushare as ts
        ts.set_token(T.token)
        pro = ts.pro_api(timeout=30)

        samples = [d for d in dates if d >= "20230101"][::STEP]
        if samples[-1] != dates[-1]:
            samples.append(dates[-1])
        print(f"[成员] 采样 {len(samples)} 个交易日（每 {STEP} 根）")

        first_hit: dict = {}
        snap = {}
        for i, d in enumerate(samples):
            df = None
            for attempt in range(4):
                try:
                    df = pro.daily_basic(trade_date=d, fields="ts_code,total_mv")
                    break
                except Exception as e:  # noqa: BLE001
                    if attempt == 3:
                        print(f"  FAIL {d}: {e}")
                    time.sleep(2.0 * (attempt + 1))
            if df is None or not len(df):
                continue
            sub = df[df["ts_code"].isin(universe)]
            sub = sub.sort_values("total_mv", ascending=False)
            top = list(sub["ts_code"].head(POOL_SIZE))
            snap[d] = top
            for c in top:
                first_hit.setdefault(c, d)
            if (i + 1) % 10 == 0:
                print(f"  sampled {i+1}/{len(samples)}")
            time.sleep(0.3)
        mem = {"first_hit": first_hit, "snapshot_size": len(snap), "samples": samples}
        OUT.write_bytes(pickle.dumps(mem))
        print(f"[成员] 已缓存 {len(first_hit)} 只曾入池 -> {OUT}")

    fh = mem["first_hit"]
    from scripts._pm_universe_20261013 import extra_codes, base_codes
    ex = extra_codes()
    print("\n[动态池 18 只的首次入池日（按历史 total_mv Top-30 口径）]")
    for c in ex:
        print(f"  {c}  {fh.get(c)}")
    in_base = sorted(c for c in base_codes() if c in fh)
    print(f"\n[回测 23 只里曾入池池的] {len(in_base)}/{len(base_codes())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

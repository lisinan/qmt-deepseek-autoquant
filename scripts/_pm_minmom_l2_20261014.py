# -*- coding: utf-8 -*-
"""PM-EVOLVE R31（2026-10-14 收官）候选 B：momentum_min_mom 在 **L2 杠杆基线**下重扫。

★ 为什么可以重开这条轴（不是 churn）
    R28（2026-10-13 AM）把 0.0→0.15，但其四窗共识跑在 **L1 前**的旧基线上
    （risk_per_trade=0.012 / max_single_position_pct=0.19 / fixed_amount=18万）。
    当日夜间 R29+L2 把杠杆整体放大到 0.04 / 0.40 / 40万 ⇒ **IS MDD 由 −7.04%
    放大到 −13.05%**。§六 纪律：尺度型改动会改变**相对断言的斜率**
    （R29 已实测轮动磨损缩 6 倍）⇒ 在旧尺度上标定的「0.15 是峰」未必在 L2 下仍成立。
    且 R28 落盘至今已 >24h（10-13 ~12:00 → 10-14 18:00），不触防 churn 锁。

用法：
    python scripts/_pm_minmom_l2_20261014.py --mode consensus
    python scripts/_pm_minmom_l2_20261014.py --mode grid --param momentum_min_mom --values=0.0,0.05,0.1,0.15,0.2
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE_CACHE = ROOT / "storage" / "hist_cache" / "ts_panel_25_901_20211201_20261013.pkl"
COUNT = 901


def main() -> int:
    panel = pickle.loads(BASE_CACHE.read_bytes())
    print(f"[R31] 注入 tushare 面板 {len(panel)} 只 <- {BASE_CACHE.name}")

    import strategy._evolve_wf as W

    def _fake_load_daily(code: str, count: int = 260):
        d = panel.get(code)
        if not d:
            return None
        n = len(d["close"])
        k = min(n, max(count, 120))
        return {kk: list(vv[-k:]) for kk, vv in d.items()}

    W.load_daily = _fake_load_daily
    W._CACHE.clear()

    base = W.base_cfg()
    tests = {"P0_当前生产基线": base}
    for v in (0.0, 0.05, 0.10, 0.15, 0.20, 0.25):
        tests[f"M2_minmom{int(v*100):03d}"] = W.replace(base, momentum_min_mom=v)
    W.candidates_final = lambda: tests

    a = list(sys.argv[1:])
    if not a:
        a = ["--mode", "consensus"]
    if "--count" not in a:
        a += ["--count", str(COUNT)]
    sys.argv = ["_evolve_wf.py"] + a
    return W.main()


if __name__ == "__main__":
    sys.exit(main())

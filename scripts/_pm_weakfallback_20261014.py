# -*- coding: utf-8 -*-
"""PM-EVOLVE R31（2026-10-14 收官）候选 A：弱市「相对动量门槛回退」剂量扫描。

只跑 Y 系列（+ P0 对照），避免 consensus 全量 40+ 候选的开销。
用法：
    python scripts/_pm_weakfallback_20261014.py --mode grid --param mom_rel_fallback --values 0.0,50,70,80,90
    python scripts/_pm_weakfallback_20261014.py --mode consensus
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

    import os
    qs = [float(x) for x in os.environ.get("R31_QS", "50,70,80,90").split(",")]
    base = W.base_cfg()
    tests = {"P0_当前生产基线": base}
    for q in qs:
        tests[f"Y_q{int(q)}"] = W.replace(base, mom_rel_fallback=q)
        tests[f"Y_q{int(q)}_floor0"] = W.replace(base, mom_rel_fallback=q,
                                                 mom_rel_floor=0.0)
    tests["Y0_关闭_惰性对照"] = W.replace(base, mom_rel_fallback=0.0)
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

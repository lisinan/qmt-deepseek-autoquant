# -*- coding: utf-8 -*-
"""PM-EVOLVE（2026-10-15 18:00 收官）候选：momentum_top_n × momentum_min_mom 的 3x3 交互实验。

来源：AM R32（10-15 午休）§7.7 显式交接给 PM 的新疑点：
    「top_n 的峰位（3，已确认 3 次）是在 min_passing_candidates=2 + min_mom=0.15
     双重收紧之后测的。若解绑任一条，top_n 峰位可能移动 ⇒ 做 2x3/3x3 交互实验
     （单点重扫已闭合，交互未测）。」

网格：top_n ∈ {2, 3, 4} × min_mom ∈ {0.10, 0.15, 0.20}，共 9 格；
      基线 = 当前生产 (top_n=3, min_mom=0.15)，含在网格内。

落盘纪律（§7）：两参数在实盘均有热读取点
    top_n    -> engine/event_engine.py:2054  STRATEGY_PARAMS.get("momentum_top_n", 6)
    min_mom  -> strategy/daily_context.py:584  STRATEGY_PARAMS.get("momentum_min_mom", 0.0)
    => 无「回测有效、实盘无效」背离；热读，次日开盘生效。

环境：xtdata 在自动化 shell 内不可达（R27 起），注入确定性 tushare 面板
    storage/hist_cache/ts_panel_25_901_20211201_20261013.pkl（23 只 × 911 根）。

用法：
    python scripts/_pm_interaction_20261015.py --mode consensus
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE_CACHE = ROOT / "storage" / "hist_cache" / "ts_panel_25_901_20211201_20261013.pkl"
COUNT = 911  # 面板实际根数（对齐后）


def main() -> int:
    panel = pickle.loads(BASE_CACHE.read_bytes())
    print(f"[交互] 注入 tushare 面板 {len(panel)} 只 <- {BASE_CACHE.name}")

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
    tests = {"P0_当前生产基线": base}  # 默认 --baseline 键；当前生产 = (top_n=3, min_mom=0.15)
    grid_tn = [2, 3, 4]
    grid_mm = [0.10, 0.15, 0.20]
    for tn in grid_tn:
        for mm in grid_mm:
            tag = f"I_tn{tn}_mm{mm:.2f}"
            tests[tag] = W.replace(base, momentum_top_n=tn, momentum_min_mom=mm)
    W.candidates_final = lambda: tests

    a = list(sys.argv[1:])
    if not a:
        a = ["--mode", "consensus"]
    if "--count" not in a:
        a += ["--count", "901"]  # 与 AM R32 既定基线口径对齐（911 根对齐日线）
    sys.argv = ["_evolve_wf.py"] + a
    return W.main()


if __name__ == "__main__":
    raise SystemExit(main())

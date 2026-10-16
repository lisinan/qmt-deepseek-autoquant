# -*- coding: utf-8 -*-
"""AM-EVOLVE（2026-10-16 午休，第 34 轮）候选 A：L2 基线下的单仓上限剂量轴
`max_single_position_pct`（当前生产 0.40）。

★ 为什么这是合法新候选（不是 churn）
  1) 历史判定「上限不绑定」的实测是在 **risk_per_trade=0.012 时代** 做出的
     （`strategy/_evolve_wf.py` base_cfg 内 R14 注释：0.16~0.40 全 +0.000，
     因 0.012/(ATR%×2.9) < 上限）。
  2) R29+L2 于 2026-10-13 把 risk_per_trade 抬到 **0.040**（3.33 倍）后，
     期望仓位 = 0.040/(ATR%×2.9)：ATR%=3% ⇒ 46%、ATR%=4% ⇒ 34%
     ⇒ **对低波动强势股，40% 上限已经绑定**，旧结论可能失效。
  3) 该轴在 L2 基线上**从未扫过**（R31/R32/R33 均未覆盖）。

★ 机理（为什么可能是 alpha 型而非纯杠杆）
  总敞口由现金夹紧（3 名候选 × 上限 ≥ 100% ⇒ 恒满仓），故本轴改变的是
  **组合内部的权重集中度**（上限 0.20 ⇒ ~5 只均摊；上限 0.60 ⇒ ~2 只集中），
  而不是总杠杆。集中度是本宇宙 alpha（动量头部集中）的直接剂量。

★ 实盘读取点（落盘纪律 §7）：`RISK_PARAMS["max_single_position_pct"]`
  → `risk/position_sizer.py`（PositionSizer），引擎 `__init__` 期缓存
  ⇒ 若落盘，**需重启，次日生效**。

用法：
    python scripts/_am_singlecap_20261016.py --mode grid
      --param max_single_position_pct --values 0.20,0.25,0.30,0.35,0.40,0.50,0.60
    python scripts/_am_singlecap_20261016.py --mode consensus
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE_CACHE = ROOT / "storage" / "hist_cache" / "ts_panel_25_901_20211201_20261013.pkl"


def main() -> int:
    panel = pickle.loads(BASE_CACHE.read_bytes())
    print(f"[AM34] 注入 tushare 面板 {len(panel)} 只 <- {BASE_CACHE.name}")

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
    # 候选集 A：单仓上限剂量轴（含当前生产 0.40 作为基线对照）
    # 候选集 B：L2 基线下的入场宽度闸门 min_passing_candidates（热读，当天生效）
    tests = {"P0_当前生产基线": base}
    for v in [0.20, 0.25, 0.30, 0.35, 0.40, 0.50, 0.60]:
        tests[f"S_cap{v:.2f}"] = W.replace(base, max_single_position_pct=v)
    for k in [0, 1, 2, 3, 4]:
        tests[f"K_k{k}"] = W.replace(base, min_passing_candidates=k)
    # 候选集 C：L2 基线下的 atr_stop_mult（热读；同时决定止损宽度与仓位，单笔风险恒定）
    for v in [2.5, 2.7, 2.9, 3.1, 3.3, 3.6]:
        tests[f"A_atr{v:.1f}"] = W.replace(base, atr_stop_mult=v)

    # 共识只跑「单窗最优的非基线候选」，避免 18 个候选 × 4 窗的无谓开销。
    import os
    only = os.environ.get("AM34_ONLY", "").strip()
    if only:
        keys = [k for k in only.split(",") if k]
        tests = {"P0_当前生产基线": base, **{k: tests[k] for k in keys if k in tests}}
    W.candidates_final = lambda: tests

    a = list(sys.argv[1:])
    if not a:
        a = ["--mode", "grid", "--param", "max_single_position_pct",
             "--values", "0.20,0.25,0.30,0.35,0.40,0.50,0.60"]
    if "--count" not in a:
        a += ["--count", "901"]  # 与 R32/R33 同口径（911 根对齐日线）
    sys.argv = ["_evolve_wf.py"] + a
    return W.main()


if __name__ == "__main__":
    raise SystemExit(main())

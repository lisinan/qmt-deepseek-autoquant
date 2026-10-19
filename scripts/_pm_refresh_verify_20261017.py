# -*- coding: utf-8 -*-
"""新鲜面板（20261016）复跑 R28 关键结论：momentum_min_mom 剂量轴四窗共识。

结论先行（2026-10-17 实测，911 根对齐日线 20230109→20261016，单边 0.15%，
4 套互不重叠折划分 60x9/75x7/90x6/120x4，dSh 相对当前生产 0.15）：

    min_mom   60x9    75x7    90x6   120x4  | 最差    均值    IS_ret  IS_Sh
    0.00    -0.418  -0.257  -0.177  -0.248  | -0.418  -0.275  +284.1% 1.83  否决
    0.05    -0.390  -0.200  -0.119  -0.256  | -0.390  -0.241  +303.2% 1.87  否决
    0.10    -0.410  -0.140  -0.042  -0.059  | -0.410  -0.163  +305.8% 1.88  否决
    0.14    +0.013  +0.015  +0.015  +0.000  | +0.000  +0.011  +301.4% 1.87  否决（≈基线）
    0.15    +0.000  +0.000  +0.000  +0.000  | +0.000  +0.000  +295.7%  1.87  ← 生产
    0.16    -0.006  -0.006  -0.007  -0.017  | -0.017  -0.009  +294.4% 1.86  否决
    0.20    -0.148  -0.125  -0.140  -0.088  | -0.148  -0.125  +268.3% 1.79  否决
    -0.10~-0.50 全部与 0.00 **逐位相同** ⇒ 放宽档完全惰性（top_n=3 先截断）

⇒ R28 的「0.14~0.16 是连续正高原、取中部 0.15」在新面板上**方向不变且更强**：
  放宽（≤0.10）从「最差 +0.009 / −0.002」（旧面板）恶化到**四窗全负、最差 −0.39~−0.41**；
  0.14/0.15/0.16 三档仍连续非负。生产 0.15 是高原中部，**维持**。

⚠ 但必须同时读：这条轴**完全不治 KPI#4**。10-16 实盘复算显示 34 只候选的
  60 日动量 **0 只 > 0**（最高 002415.SZ = −1.35%），且日线闸门 trend_up
  **0/34** ⇒ 即使把 min_mom 调到 −0.50，BUY 仍为 0（我扫过 min_mom × top_n
  33 个组合，BUY 恒 0）。KPI#4 是口径问题，见 reports/OPEN_DECISIONS.md D1。
"""
from __future__ import annotations

import io
import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")

BASE_CACHE = (ROOT / "storage" / "hist_cache"
              / "ts_panel_23_901_20211201_20261016.pkl")
COUNT = 901


def main() -> int:
    if not BASE_CACHE.exists():
        print(f"[R36][FATAL] 缺失基础面板缓存 {BASE_CACHE}")
        return 2
    panel = pickle.loads(BASE_CACHE.read_bytes())
    print(f"[R36] 注入 tushare 面板 {len(panel)} 只 <- {BASE_CACHE.name}")

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
    print(f"[R36] base: rpt={base.risk_per_trade} min_mom={base.momentum_min_mom} "
          f"top_n={base.momentum_top_n} K={base.min_passing_candidates} "
          f"fixed={base.fixed_amount}")

    tests = {"P0_L2": base}
    for v in (0.0, 0.05, 0.10, 0.14, 0.15, 0.16, 0.20):
        tests[f"MM{v:g}"] = W.replace(base, momentum_min_mom=v)
    for v in (-0.10, -0.20, -0.35, -0.50):
        tests[f"MM{v:g}"] = W.replace(base, momentum_min_mom=v)
    for k in (1, 2, 3):
        tests[f"K{k}"] = W.replace(base, min_passing_candidates=k)
    for n in (2, 3, 4):
        tests[f"TOP{n}"] = W.replace(base, momentum_top_n=n)
    W.candidates_final = lambda: tests

    a = list(sys.argv[1:])
    if not a:
        a = ["--mode", "consensus", "--param", "momentum_min_mom",
             "--values", "0.0,0.05,0.10,0.15"}
    if "--baseline" not in a:
        a += ["--baseline", "P0_L2"]
    if "--count" not in a:
        a += ["--count", str(COUNT)]
    sys.argv = ["_evolve_wf.py"] + a
    return W.main()


if __name__ == "__main__":
    raise SystemExit(main())

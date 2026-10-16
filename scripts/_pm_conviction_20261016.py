# -*- coding: utf-8 -*-
"""PM-EVOLVE（2026-10-16 收官，第 35 轮）候选 A：L2 基线下的「信念仓位」剂量轴
`conviction_boost`（R29 路径②，回测已建模、live 引擎无读点）。

★ 为什么这是 AM R34 §9-7 显式交接的唯一未闭合项
  R29（10-13）在 **L1 基线（risk_per_trade=0.020）** 上量过本轴：
    信念 1.5×  IS +181.61% / Sh 1.87 / MDD −9.97% / 拼接 Sh 2.203 / 最差窗 MDD −7.47%
    信念 2.0×  IS +237.13% / Sh 1.88 / MDD −11.89% / 拼接 Sh 2.144 / 最差窗 MDD −13.70%
  当晚 L2 把 risk_per_trade 抬到 0.040（2×）⇒ **信念乘子与 L2 的复合杠杆从未量过**，
  且 IS MDD 基线已从 −7.04% 抬到 −13.05% ⇒ 旧「最差窗 MDD −7.47%」结论**必然失效**。
  本轮在 L2 基线上重扫剂量轴（boost 1.0/1.25/1.5/2.0 × thr 0.5/0.6/0.7 的部分组合）。

★ 尺度型判据（§六纪律）：本轴是**杠杆型**改动（只放大新开仓目标金额），
  Sharpe/Calmar 在纯缩放下应为不变量 ⇒ **不可套「最差窗 dSh ≥ +0.10」**，
  改看：Calmar（应不降）+ MDD 预算使用率（≤100% 且留余量）+ 正收折不退化 + 最差折 > −15%。

用法：
  python scripts/_pm_conviction_20261016.py --mode grid --param conviction_boost --values 1.0,1.25,1.5,2.0
  PM35_ONLY=B1.5,B2.0 python scripts/_pm_conviction_20261016.py --mode consensus
"""
from __future__ import annotations

import os
import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE_CACHE = ROOT / "storage" / "hist_cache" / "ts_panel_25_901_20211201_20261013.pkl"


def main() -> int:
    if not BASE_CACHE.exists():
        print(f"[PM35][FATAL] 缺失基础面板缓存 {BASE_CACHE}")
        return 2
    panel = pickle.loads(BASE_CACHE.read_bytes())
    print(f"[PM35] 注入 tushare 面板 {len(panel)} 只 <- {BASE_CACHE.name}")

    import strategy._evolve_wf as W

    # ★ conviction_mode 是「总开关」，网格 --param 只替换单个字段 ⇒ 必须先把基线
    #   切成 mode="breadth" + boost=1.0（乘 1.0 = 数学上恒等），否则整条轴恒惰性。
    #   切换后必须核对 P0 仍为 +295.74%/1.87/−13.05%（不变性检验，防基线漂移）。
    _orig_base_cfg = W.base_cfg

    def _patched_base_cfg():
        c = _orig_base_cfg()
        c.conviction_mode = "breadth"
        c.conviction_boost = 1.0
        c.conviction_breadth_thr = 0.6
        return c

    W.base_cfg = _patched_base_cfg

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
    print(f"[PM35] base: rpt={base.risk_per_trade} cap={base.max_single_position_pct} "
          f"fixed={base.fixed_amount} atr={base.atr_stop_mult} "
          f"conv={base.conviction_mode}/{base.conviction_boost}/{base.conviction_breadth_thr}")

    tests = {"P0_L2": base}
    # 剂量轴：boost（thr 固定 0.6）
    for b in [1.0, 1.05, 1.1, 1.15, 1.2, 1.25, 1.35, 1.5, 1.75, 2.0]:
        tests[f"B{b:g}"] = W.replace(base, conviction_boost=b)
    # 触发阈值轴（boost 固定 1.25 = 单窗 argmax）
    for t in [0.4, 0.5, 0.7, 0.8]:
        tests[f"T{t:g}"] = W.replace(base, conviction_boost=1.25,
                                     conviction_breadth_thr=t)

    only = os.environ.get("PM35_ONLY", "").strip()
    if only:
        keys = [k for k in only.split(",") if k]
        tests = {"P0_L2": base, **{k: tests[k] for k in keys if k in tests}}
    W.candidates_final = lambda: tests

    a = list(sys.argv[1:])
    if not a:
        a = ["--mode", "grid", "--param", "conviction_boost",
             "--values", "1.0,1.25,1.5,2.0"]
    if "--count" not in a:
        a += ["--count", "901"]  # 与 R32/R33/R34 同口径（911 根对齐日线）
    sys.argv = ["_evolve_wf.py"] + a
    return W.main()


if __name__ == "__main__":
    raise SystemExit(main())

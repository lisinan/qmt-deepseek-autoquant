# -*- coding: utf-8 -*-
"""PM-EVOLVE（2026-10-19 收官，第 38 轮）

候选 A（口径/缺陷通道）：生产**组合级回撤断路器**（risk/manager.py::on_asset_update）
  vs 回测 P0 基线 —— 本项目第 **17** 例「实盘有、回测无」型背离的量化与定价。

★ 与 R37 的两条降级通道的差别（勿混淆）
  R37 量的是「连亏阶梯 + 日内亏损熔断」两条**买入侧冻结**通道（已定价：均值 −0.017）。
  本轮量的是**组合净值自峰值回撤**触发的 halt（`max_drawdown_pct`），
  即 risk/manager.py:129-135：
      if total_asset > peak: peak = total_asset; return
      dd = (total_asset - peak) / peak
      if dd <= max_drawdown_pct:  → halt(reason="max_drawdown ...")
  恢复语义（::_maybe_recover:294-305）：冷却 `dd_recover_days`(=3) 个**自然日**
  自动解除，并 `_peak_asset = total_asset` **重置回撤基线**。
  ⇒ 是「买入侧冻结 + 冷却自愈 + 峰值重置」三者合一，与 R37 两条都不同。

★ 为什么要补 `rm_dd_pause_bars`（本轮工程改动，默认 0 = 零行为变化）
  旧代理只有两种语义：永久熔断（比生产严得多）/ 创新高才解除（仍比生产严）。
  都不是生产真行为 ⇒ 必须先补代理才配谈度量（第 14 次同型坑）。

候选：
  P0_验证器基线     rm_dd_pause_pct = 0.0（关闭，现行口径）
  DD20_Permanent    −0.20 + 永久熔断（上界严厉度）
  DD20_NewHigh      −0.20 + 创新高才解除
  DD20_B2 / DD20_B3 −0.20 + 冷却 2 / 3 个交易日解除并重置峰值（**生产真语义**）
  DD25_B3           −0.25 + 冷却 3（历史验证器口径值）
  INV_永不触发      −0.99 + 冷却（不变性检验：应与 P0 逐位相同）
"""
from __future__ import annotations

import io
import os
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
        print(f"[PM38A][FATAL] 缺失基础面板缓存 {BASE_CACHE}")
        return 2
    panel = pickle.loads(BASE_CACHE.read_bytes())
    print(f"[PM38A] 注入 tushare 面板 {len(panel)} 只 <- {BASE_CACHE.name}")

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
    print(f"[PM38A] base: rm_dd_pause_pct={base.rm_dd_pause_pct} "
          f"recoverable={base.rm_dd_pause_recoverable} "
          f"bars={base.rm_dd_pause_bars}")

    tests = {
        "P0_验证器基线": base,
        "DD20_Permanent": W.replace(base, rm_dd_pause_pct=-0.20,
                                    rm_dd_pause_recoverable=False,
                                    rm_dd_pause_bars=0),
        "DD20_NewHigh": W.replace(base, rm_dd_pause_pct=-0.20,
                                  rm_dd_pause_recoverable=True,
                                  rm_dd_pause_bars=0),
        "DD20_B2_生产冷却2": W.replace(base, rm_dd_pause_pct=-0.20,
                                     rm_dd_pause_bars=2),
        "DD20_B3_生产冷却3": W.replace(base, rm_dd_pause_pct=-0.20,
                                     rm_dd_pause_bars=3),
        "DD25_B3": W.replace(base, rm_dd_pause_pct=-0.25,
                             rm_dd_pause_bars=3),
        "INV_永不触发": W.replace(base, rm_dd_pause_pct=-0.99,
                                rm_dd_pause_bars=3),
    }
    W.candidates_final = lambda: tests

    a = list(sys.argv[1:])
    if not a:
        a = ["--mode", "consensus"]
    if "--baseline" not in a:
        a += ["--baseline", "P0_验证器基线"]
    if "--count" not in a:
        a += ["--count", str(COUNT)]
    if "--track" not in a and os.environ.get("PM38A_TRACK"):
        a += ["--track", os.environ["PM38A_TRACK"]]
    sys.argv = ["_evolve_wf.py"] + a
    return W.main()


if __name__ == "__main__":
    raise SystemExit(main())

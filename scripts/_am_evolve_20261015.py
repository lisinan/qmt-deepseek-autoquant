# -*- coding: utf-8 -*-
"""AM-EVOLVE R32（2026-10-15 午休）候选 A：`max_positions` 在 **L2 杠杆基线**下重扫。

★ 为什么这是一条**新打开的轴**（不是 churn，也不是第 N 次重扫同样东西）

    R29+L2（2026-10-13 夜间）把 risk_per_trade 0.012→0.040、
    max_single_position_pct 0.19→0.40、max_order_amount 18万→40万。
    关键结构性后果：**单仓上限从 19% 抬到 40%** ⇒
      · L1 时代：max_positions=5 ⇒ 每仓被 19% 夹住，总敞口 95%（现金夹紧恰好重合），
        max_positions=2/3 与 5 **几乎等价**（都被 19% 夹成同一个数）⇒ 该轴在 L1 下
        「低档位根本没有分辨率」，旧结论「5 优于 8」只覆盖了 5~8 这一段。
      · L2 时代：单仓可到 40% ⇒ max_positions=2 真正能把 80% 押在 2 只上，
        max_positions=3 ⇒ 27%/只。**2/3/4 这些低档位在 L2 下首次具备真实剂量分辨率**。
    即：L2 不是把旧轴平移，而是**打开了一片旧基线够不到的参数区域**。
    这正是 §六「尺度型改动会改变相对断言的斜率」的正面用例（R29 实测轮动磨损缩 6 倍）。

    ★ 本参数为**热读**（engine/event_engine.py:1161
      `_mp = int(STRATEGY_PARAMS.get("max_positions", self.max_positions))`）
      ⇒ 若达标，下午 13:00 开盘即生效，当天可被真实数据检验。

    防 churn：`max_positions` 上次改动远早于 24h（生产值 5 自 09-16 起未变）。

用法：
    python scripts/_am_evolve_20261015.py --mode consensus
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
    print(f"[R32] 注入 tushare 面板 {len(panel)} 只 <- {BASE_CACHE.name}")

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
    # 剂量轴：L2 单仓上限 40% ⇒ 2/3/4 首次有分辨率
    for v in (2, 3, 4, 5, 6, 8):
        tests[f"A_maxpos{v}"] = W.replace(base, max_positions=v)
    # 组合对照（top_n × max_positions 协同）：集中度 vs 宽度
    tests["A_tn3_mp3"] = W.replace(base, momentum_top_n=3, max_positions=3)
    tests["A_tn2_mp2"] = W.replace(base, momentum_top_n=2, max_positions=2)
    tests["A_tn4_mp4"] = W.replace(base, momentum_top_n=4, max_positions=4)
    W.candidates_final = lambda: tests

    a = list(sys.argv[1:])
    if not a:
        a = ["--mode", "consensus"]
    if "--count" not in a:
        a += ["--count", str(COUNT)]
    sys.argv = ["_evolve_wf.py"] + a
    return W.main()


if __name__ == "__main__":
    raise SystemExit(main())

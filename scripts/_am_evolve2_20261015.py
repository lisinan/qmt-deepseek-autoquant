# -*- coding: utf-8 -*-
"""AM-EVOLVE R32 候选 B：`hard_stop_pct` 在 **L2 杠杆基线**下重扫（左尾/隔夜跳空）。

★ 为什么可以在「已证伪轴」上重开（L2 打开了旧基线够不到的区域）
    R25（10-09）判 hard_stop「惰性 ⇒ 左尾已被 trend_break 截断」，但那是在
    **L1 尺度**（risk_per_trade 0.012 / 单仓 19% / IS MDD −7.04%）上量的。
    R29+L2 把杠杆放大后 IS MDD = **−13.05%**：单仓 40% 时，一个 −18% 的个股硬止损
    等于 **−7.2% 权益**。旧基线整体 MDD 只有 −7%，−18% 档位几乎永不触及；
    L2 下它**首次进入可达区间** ⇒ 这是一条被尺度变化重新打开分辨率的轴。
    §六 纪律的同型正面用例：R29 实测「杠杆放大后轮动磨损缩 6 倍」。

    ★ 热读（engine/event_engine.py:1811 / 1942 STRATEGY_PARAMS.get("hard_stop_pct")）
      ⇒ 若达标，下午 13:00 开盘即生效。
    ★ 防 churn：hard_stop_pct 生产值 −0.18 自 09 月起未变（≫24h）。

用法： python scripts/_am_evolve2_20261015.py --mode consensus
"""
from __future__ import annotations
import pickle, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
BASE_CACHE = ROOT / "storage" / "hist_cache" / "ts_panel_25_901_20211201_20261013.pkl"
COUNT = 901

def main() -> int:
    panel = pickle.loads(BASE_CACHE.read_bytes())
    print(f"[R32B] 注入 tushare 面板 {len(panel)} 只 <- {BASE_CACHE.name}")
    import strategy._evolve_wf as W
    def _fake_load_daily(code: str, count: int = 260):
        d = panel.get(code)
        if not d: return None
        n = len(d["close"]); k = min(n, max(count, 120))
        return {kk: list(vv[-k:]) for kk, vv in d.items()}
    W.load_daily = _fake_load_daily
    W._CACHE.clear()
    base = W.base_cfg()
    tests = {"P0_当前生产基线": base}
    for v in (-0.08, -0.10, -0.12, -0.15, -0.18, -0.25, -0.99):
        tests[f"B_hs{int(abs(v)*100):03d}"] = W.replace(base, hard_stop_pct=v)
    # 组合：候选 A 的最优点 max_positions=2 × 更紧硬止损
    tests["B_mp2_hs12"] = W.replace(base, max_positions=2, hard_stop_pct=-0.12)
    tests["B_mp2_hs15"] = W.replace(base, max_positions=2, hard_stop_pct=-0.15)
    W.candidates_final = lambda: tests
    a = list(sys.argv[1:])
    if not a: a = ["--mode", "consensus"]
    if "--count" not in a: a += ["--count", str(COUNT)]
    sys.argv = ["_evolve_wf.py"] + a
    return W.main()

if __name__ == "__main__":
    raise SystemExit(main())

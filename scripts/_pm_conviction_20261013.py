# -*- coding: utf-8 -*-
"""PM-EVOLVE R29 风险路径②（用户授权：AI 强势期主动放大暴露 = conviction sizing）。

与路径①（纯杠杆，恒放大）正交：本路径只在 AI 动量广度（宇宙内站上 MA 的比例）
≥ 阈值时，放大新开仓目标仓位（conviction_boost > 1.0）。弱 AI 景气期维持常态，
避免在不可避免空仓期也背杠杆成本。预期：强 AI 趋势段吃到更多收益，
且因只在高置信期加仓，回撤放大应小于同量级恒杠杆。

数据口径：同路径①，强制注入 R28 同款 tushare 面板，基线 P0 与 +131.93% 对齐。

用法：
  python scripts/_pm_conviction_20261013.py --boost 1.5 [--thr 0.6] [--mode consensus|span]
  python scripts/_pm_conviction_20261013.py --boost 2.0 --mode span
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
THR_DEFAULT = 0.6


def _parse(argv):
    argv = list(argv)
    boost = 1.5
    thr = THR_DEFAULT
    if "--boost" in argv:
        boost = float(argv[argv.index("--boost") + 1])
    if "--thr" in argv:
        thr = float(argv[argv.index("--thr") + 1])
    # 剔除 --boost/--thr 及其紧跟的值，剩余交给 _evolve_wf
    out = []
    skip = False
    for x in argv:
        if skip:
            skip = False
            continue
        if x in ("--boost", "--thr"):
            skip = True
            continue
        out.append(x)
    return boost, thr, out


def main() -> int:
    raw = sys.argv[1:]
    boost, thr, rest = _parse(raw)
    print(f"[信心] boost={boost} thr={thr}")

    if not BASE_CACHE.exists():
        print(f"[信心][FATAL] 缺失基础面板缓存 {BASE_CACHE}")
        return 2
    panel = pickle.loads(BASE_CACHE.read_bytes())
    print(f"[信心] 注入 tushare 面板 {len(panel)} 只 <- {BASE_CACHE.name}")

    import strategy._evolve_wf as W
    _orig_base_cfg = W.base_cfg

    def _patched_base_cfg():
        c = _orig_base_cfg()
        c.conviction_mode = "breadth"
        c.conviction_boost = boost
        c.conviction_breadth_thr = thr
        return c

    W.base_cfg = _patched_base_cfg
    _demo = _patched_base_cfg()
    print(f"[信心] base_cfg 已覆盖: conviction_mode={_demo.conviction_mode} "
          f"boost={_demo.conviction_boost} thr={_demo.conviction_breadth_thr}")

    def _fake_load_daily(code: str, count: int = 260):
        d = panel.get(code)
        if not d:
            return None
        n = len(d["close"])
        k = min(n, max(count, 120))
        return {kk: list(vv[-k:]) for kk, vv in d.items()}

    W.load_daily = _fake_load_daily
    W._CACHE.clear()

    a = list(rest)
    if not a:
        a = ["--mode", "span"]
    if "--count" not in a:
        a += ["--count", str(COUNT)]
    sys.argv = ["_evolve_wf.py"] + a
    return W.main()


if __name__ == "__main__":
    sys.exit(main())

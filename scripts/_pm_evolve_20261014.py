# -*- coding: utf-8 -*-
"""PM-EVOLVE R31（2026-10-14 收官）回测 harness：注入确定性 tushare 面板。

★ 为什么需要它
    本自动化 shell 内 **xtdata 不可达**（miniQMT 未运行）⇒ ``core.qmt_client``
    回落 mock ⇒ ``load_daily`` 返回合成价且 ``date`` 全为当天 ⇒
    ``aligned_universe`` 对齐后只剩 1 根，验证器崩溃（R27 首次记录的第 15 条教训）。
    这里强制注入与 R28/R29/R30 **同源** 的 tushare 面板缓存
    ``storage/hist_cache/ts_panel_25_901_20211201_20261013.pkl``（25 只 AI 宇宙、
    末根 20261013），使本轮所有读数与生产基线守卫口径一致。

用法：
    python scripts/_pm_evolve_20261014.py --mode compare
    python scripts/_pm_evolve_20261014.py --mode grid --param momentum_min_mom --values 0.0,0.05,0.1,0.15,0.2
    python scripts/_pm_evolve_20261014.py --mode consensus
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
    if not BASE_CACHE.exists():
        print(f"[R31][FATAL] 缺失基础面板缓存 {BASE_CACHE}")
        return 2
    panel = pickle.loads(BASE_CACHE.read_bytes())
    print(f"[R31] 注入 tushare 面板 {len(panel)} 只 <- {BASE_CACHE.name}")
    _d0 = list(panel.values())[0]
    print(f"[R31] 末根 {_d0['date'][-1]}  根数 {len(_d0['close'])}")

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

    a = list(sys.argv[1:])
    if not a:
        a = ["--mode", "compare"]
    if "--count" not in a:
        a += ["--count", str(COUNT)]
    sys.argv = ["_evolve_wf.py"] + a
    return W.main()


if __name__ == "__main__":
    sys.exit(main())

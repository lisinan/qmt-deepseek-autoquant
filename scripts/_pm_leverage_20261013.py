# -*- coding: utf-8 -*-
"""PM-EVOLVE R29 风险路径①（用户授权：放开 risk_per_trade>0.02 纯杠杆）。

测量「协调放大仓位上限」对回测的影响。杠杆是 Sharpe 不变、绝对收益线性放大的
尺度型改动（R23 已证），本脚本量化其在当前基线下到底放大多少绝对收益、回撤是否
仍在 KPI#3(≥−22%) 预算内。

协调改法（回测侧 base_cfg + 实盘侧 RISK/STRATEGY 参数须同步）：
  risk_per_trade       0.020 -> L1=0.030 / L2=0.040
  max_single_position_pct 0.19 -> L1=0.30 / L2=0.40
  fixed_amount         180000 -> L1=300000 / L2=400000   (回测侧单仓金额上限)
（实盘侧对应 max_position_amount / max_order_amount 同额放大）

数据口径：强制注入 R28 同款 tushare 面板（ts_panel_25_901_20211201_20261013.pkl），
确保本脚本打出的「基线 P0」与 R28 文档基线 +131.93%/Sh1.89/MDD−7.04% 逐位对齐，
杠杆增量只看「候选 vs 自身基线」的 dSh / 回撤预算使用率。

用法：python scripts/_pm_leverage_20261013.py --level 1 [--mode consensus|span]
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LEVELS = {
    "0": dict(risk_per_trade=0.020, max_single_position_pct=0.19, fixed_amount=180000.0),
    "1": dict(risk_per_trade=0.030, max_single_position_pct=0.30, fixed_amount=300000.0),
    "2": dict(risk_per_trade=0.040, max_single_position_pct=0.40, fixed_amount=400000.0),
}

BASE_CACHE = ROOT / "storage" / "hist_cache" / "ts_panel_25_901_20211201_20261013.pkl"
COUNT = 901


def _parse_level(argv):
    """从 argv 中抽取 level（支持 --level X 或裸位置参数 1/2），返回 (lvl, 剩余argv)。"""
    argv = list(argv)
    lvl = "1"
    if "--level" in argv:
        i = argv.index("--level")
        lvl = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    elif argv and argv[0] in LEVELS:
        lvl = argv[0]
        argv = argv[1:]
    return lvl, argv


def main() -> int:
    raw = sys.argv[1:]
    lvl, argv = _parse_level(raw)
    cfg = LEVELS[lvl]
    print(f"[杠杆] Level {lvl}: {cfg}")

    if not BASE_CACHE.exists():
        print(f"[杠杆][FATAL] 缺失基础面板缓存 {BASE_CACHE}")
        return 2
    panel = pickle.loads(BASE_CACHE.read_bytes())
    print(f"[杠杆] 注入 tushare 面板 {len(panel)} 只 <- {BASE_CACHE.name}")

    import strategy._evolve_wf as W
    _orig_base_cfg = W.base_cfg

    def _patched_base_cfg():
        c = _orig_base_cfg()
        c.risk_per_trade = cfg["risk_per_trade"]
        c.max_single_position_pct = cfg["max_single_position_pct"]
        c.fixed_amount = cfg["fixed_amount"]
        return c

    W.base_cfg = _patched_base_cfg
    _demo = _patched_base_cfg()
    print(f"[杠杆] base_cfg 已覆盖: rpt={_demo.risk_per_trade} "
          f"max_pct={_demo.max_single_position_pct} "
          f"fixed_amount={_demo.fixed_amount}")

    def _fake_load_daily(code: str, count: int = 260):
        d = panel.get(code)
        if not d:
            return None
        n = len(d["close"])
        k = min(n, max(count, 120))
        return {kk: list(vv[-k:]) for kk, vv in d.items()}

    W.load_daily = _fake_load_daily
    W._CACHE.clear()

    a = list(argv)
    if not a:
        a = ["--mode", "span"]
    if "--count" not in a:
        a += ["--count", str(COUNT)]
    sys.argv = ["_evolve_wf.py"] + a
    return W.main()


if __name__ == "__main__":
    sys.exit(main())

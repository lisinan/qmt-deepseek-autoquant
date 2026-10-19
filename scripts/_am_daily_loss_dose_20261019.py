# -*- coding: utf-8 -*-
"""【2026-10-19 AM-EVOLVE 第 37 轮】候选 B：日内已实现亏损熔断阈值 `daily_loss_pct` 剂量轴。

为什么扫这一条（承接 `_am_derisk_ladder_20261019.py` 的发现）
  R37 共识里 **DL_仅日内熔断 是唯一四窗同号为正**的变体
    （+0.043 / +0.049 / +0.049 / +0.052，最差 +0.043、均值 +0.049），
  而连亏阶梯是负项（CL_仅连亏阶梯 最差 −0.103、均值 −0.066）。
  ⇒ 两条通道里只有日内熔断是**正贡献**，值得做剂量扫描看是否存在更优阈值。

  生产语义（R23 落盘 DAILY_LOSS_UNION_MAX）：生效阈值 = max(5000, |pct| × 权益)
    ⇒ 大账户下就是 pct 生效，回测代理用 ``daily_loss_pct`` + ``daily_loss_abs_pct=0`` 复现。
  对照基线 = **PROD_实盘现状**（连亏阶梯 + 日内熔断，即真实生产），
    这样 dSh 直接就是「相对今天实盘」的真增量。
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
        print(f"[R37B][FATAL] 缺失基础面板缓存 {BASE_CACHE}")
        return 2
    panel = pickle.loads(BASE_CACHE.read_bytes())
    print(f"[R37B] 注入 tushare 面板 {len(panel)} 只 <- {BASE_CACHE.name}")

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
    CL = dict(consec_loss_scale=True, consec_loss_trigger=3,
              consec_loss_halt=5, consec_loss_batch=True,
              consec_loss_halt_bars=1, consec_loss_floor=0.0)
    DL = dict(daily_loss_halt=True, daily_loss_pct=-0.03,
              daily_loss_abs_pct=0.0, daily_loss_halt_bars=1)

    tests = {
        "PROD_实盘现状(-3%)": W.replace(base, **CL, **DL),
        "DL02_-2%": W.replace(base, **CL, **{**DL, "daily_loss_pct": -0.02}),
        "DL025_-2.5%": W.replace(base, **CL, **{**DL, "daily_loss_pct": -0.025}),
        "DL04_-4%": W.replace(base, **CL, **{**DL, "daily_loss_pct": -0.04}),
        "DL05_-5%": W.replace(base, **CL, **{**DL, "daily_loss_pct": -0.05}),
        "DL08_-8%": W.replace(base, **CL, **{**DL, "daily_loss_pct": -0.08}),
        "DLb1_冷却3日": W.replace(base, **CL, **{**DL, "daily_loss_halt_bars": 3}),
        "DLOFF_关闭熔断": W.replace(base, **CL),
    }
    W.candidates_final = lambda: tests

    a = list(sys.argv[1:])
    if not a:
        a = ["--mode", "consensus", "--param", "daily_loss_pct",
             "--values", "-0.02,-0.03,-0.05"]
    if "--baseline" not in a:
        a += ["--baseline", "PROD_实盘现状(-3%)"]
    if "--count" not in a:
        a += ["--count", str(COUNT)]
    sys.argv = ["_evolve_wf.py"] + a
    return W.main()


if __name__ == "__main__":
    raise SystemExit(main())

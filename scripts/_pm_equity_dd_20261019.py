# -*- coding: utf-8 -*-
"""PM-EVOLVE（2026-10-19 收官，第 38 轮）

候选 B（结构/机制型）：**组合级权益回撤防御态** `equity_dd_stop_*`
  —— 一个**已实现但从未验证**的两段式状态机（backtest_daily.py:939-941，默认关闭）。

★ 为什么现在测它（不是第 N 次单参数网格）
  R18~R37 已把「退出侧」各类**个股级**离场旋钮全部闭合：
    R25 更快离场整类（ATR 止损死代码 / 吊灯单调负 / hard_stop 惰性）
    R26 更慢离场整类（trend_break_confirm 2~5 / buffer 0.005~0.05 全封顶 +0.017）
  但它们**全是「个股级」**：单只票的噪声就能触发，故无论快慢都磨损 alpha。
  本旋钮是**组合级**：只有**整个账户**自峰值回撤超阈值才触发 ⇒ 单票噪声不触发，
  只有系统性下跌才触发。这正是 2026 H2 负折的共同指纹（AM R37 实测
  75×7 F7 20260626~20261016 = −5.38%、90×6 F6 −13.17%）。

★ 与已否决项的本质区别（务必区分，勿误当 churn）
  与「指数 MA60 持久闸门 / tailhedge」不同：本旋钮的触发器是**组合自身权益曲线**
  （只有账户真在流血才清仓），不需要外部指数数据、不会「指数跌而我没跌」时被误杀。
  与 dd_ctrl（只缩新开仓、不平已有）不同：这是**硬止损**（触发即清空全部持仓转现金）。
  两段式（入场上看峰值回撤 / 出场看低点反弹）规避「出得来回不去」的长期踏空。

★ 纪律：
  ① 开关型参数必须先打开总开关（enable=True）再扫剂量，否则整轴恒 0（第 14 次同型坑）。
  ② 不变性检验：enable=True + 剂量永远不会触发的档位，应与 P0 逐位相同。
  ③ 本旋钮是**风险型**改动（降低暴露），不是杠杆型 ⇒ 走 alpha 通道 + 闸门②。

用法：
  PM38_PHASE=stop   python scripts/_pm_equity_dd_20261019.py --mode consensus
  PM38_PHASE=resume python scripts/_pm_equity_dd_20261019.py --mode consensus
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
        print(f"[PM38][FATAL] 缺失基础面板缓存 {BASE_CACHE}")
        return 2
    panel = pickle.loads(BASE_CACHE.read_bytes())
    print(f"[PM38] 注入 tushare 面板 {len(panel)} 只 <- {BASE_CACHE.name}")

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
    print(f"[PM38] base: rpt={base.risk_per_trade} min_mom={base.momentum_min_mom} "
          f"top_n={base.momentum_top_n} K={base.min_passing_candidates} "
          f"fixed={base.fixed_amount} eq_dd={base.equity_dd_stop_enable}")

    phase = os.environ.get("PM38_PHASE", "stop").strip().lower()

    if phase == "stop":
        # 剂量轴：清仓阈值（resume 固定 0.10）
        tests = {"P0_当前生产基线": base}
        for p in (-0.08, -0.10, -0.12, -0.15, -0.20, -0.25):
            tests[f"E{abs(p) * 100:.0f}_清仓{abs(p) * 100:.0f}%"] = W.replace(
                base, equity_dd_stop_enable=True,
                equity_dd_stop_pct=p, equity_dd_resume_pct=0.10)
        # 不变性对照：enable=True 但阈值设为永不可达（-0.99）⇒ 应与 P0 逐位相同
        tests["INV_开关开但永不触发"] = W.replace(
            base, equity_dd_stop_enable=True,
            equity_dd_stop_pct=-0.99, equity_dd_resume_pct=0.10)
    else:
        # 恢复轴：清仓阈值固定（由 stop 相决定），扫低点反弹幅度
        stop_p = float(os.environ.get("PM38_STOP", "-0.15"))
        tests = {"P0_当前生产基线": base}
        for r in (0.05, 0.10, 0.20, 0.30):
            tests[f"R{r * 100:.0f}_反弹{r * 100:.0f}%"] = W.replace(
                base, equity_dd_stop_enable=True,
                equity_dd_stop_pct=stop_p, equity_dd_resume_pct=r)
        print(f"[PM38] resume 轴：清仓阈值固定 {stop_p}")

    W.candidates_final = lambda: tests

    a = list(sys.argv[1:])
    if not a:
        a = ["--mode", "consensus"]
    if "--baseline" not in a:
        a += ["--baseline", "P0_当前生产基线"]
    if "--count" not in a:
        a += ["--count", str(COUNT)]
    sys.argv = ["_evolve_wf.py"] + a
    return W.main()


if __name__ == "__main__":
    raise SystemExit(main())

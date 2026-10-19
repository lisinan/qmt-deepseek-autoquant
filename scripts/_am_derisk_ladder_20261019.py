# -*- coding: utf-8 -*-
"""【2026-10-19 AM-EVOLVE 第 37 轮】实盘两条「风控降级通道」的口径背离量化 + 剂量轴。

★ 本轮假设（开工前先查证，见 config/settings.py / risk/manager.py）
  生产实盘**开启**两条回测 P0 基线**从未建模**的降级通道：
    ① 连亏阶梯：亏 → +1（同日只计 1 次，R23 修复），≥3 降仓 [1.0,0.8,0.6,0.4,0.0]，
                ≥5 halt（冻结一切新开仓），冷却 1 日自愈。
    ② 日内已实现亏损熔断：_daily_pnl ≤ −max(5000, 3%×权益) ⇒ halt，冷却 1 日。
  而 ``strategy/_evolve_wf.py::base_cfg`` 两者均为默认关闭
    （consec_loss_scale=False / daily_loss_halt=False）
  ⇒ **自 R6/R7（09-24）建代理至今，P0 基线与全部 dSh/KPI 都建立在「无降级通道」
     的偏乐观口径上**。这是本项目第 15/16 例「实盘有、回测无」型背离。

★ 为什么现在才测（不是重复劳动）
  R6/R7 只在 **rpt=0.012、25×713 根**的旧基线上量过；此后 L2 把单仓放大到
  ≈2.24×（rpt 0.040 / 单仓 40%）⇒ 单笔亏损幅度同比放大 ⇒ 两条通道的**触发频率
  与代价**都已改变。且 R23 修复了「同批退出计 N 次」，实盘语义已变为 batch。
  ⇒ 在 **L2 基线 + 刷新面板（末根 20261016）** 上重测是新的量测，不是 churn。

候选（production 语义 = trigger3 / halt5 / batch / bars1 / floor0.0 / pct −0.03）：
  P0_验证器基线      两条都关（现行验证器口径，不变性对照）
  PROD_实盘现状      两条都开（真实生产口径）
  CL_仅连亏阶梯      只开 ①
  DL_仅日内熔断      只开 ②
  FL04 / FL06        阶梯末端保留地板 0.4 / 0.6（降仓但不冻结）
  T4                 降仓触发 3 → 4
  H7 / H9            连亏熔断 5 → 7 / 9
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
        print(f"[R37][FATAL] 缺失基础面板缓存 {BASE_CACHE}")
        return 2
    panel = pickle.loads(BASE_CACHE.read_bytes())
    print(f"[R37] 注入 tushare 面板 {len(panel)} 只 <- {BASE_CACHE.name}")

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
    print(f"[R37] base: rpt={base.risk_per_trade} min_mom={base.momentum_min_mom} "
          f"top_n={base.momentum_top_n} K={base.min_passing_candidates} "
          f"fixed={base.fixed_amount} consec={base.consec_loss_scale} "
          f"daily_halt={base.daily_loss_halt}")

    # ---- 生产语义的两条通道 ----
    CL = dict(consec_loss_scale=True, consec_loss_trigger=3,
              consec_loss_halt=5, consec_loss_batch=True,
              consec_loss_halt_bars=1, consec_loss_floor=0.0)
    DL = dict(daily_loss_halt=True, daily_loss_pct=-0.03,
              daily_loss_abs_pct=0.0, daily_loss_halt_bars=1)

    tests = {
        "P0_验证器基线": base,
        "PROD_实盘现状": W.replace(base, **CL, **DL),
        "CL_仅连亏阶梯": W.replace(base, **CL),
        "DL_仅日内熔断": W.replace(base, **DL),
        "FL04_地板0.4": W.replace(base, **{**CL, "consec_loss_floor": 0.4}, **DL),
        "FL06_地板0.6": W.replace(base, **{**CL, "consec_loss_floor": 0.6}, **DL),
        "T4_触发4": W.replace(base, **{**CL, "consec_loss_trigger": 4}, **DL),
        "H7_熔断7": W.replace(base, **{**CL, "consec_loss_halt": 7}, **DL),
        "H9_熔断9": W.replace(base, **{**CL, "consec_loss_halt": 9,
                                      "consec_loss_floor": 0.0}, **DL),
    }
    W.candidates_final = lambda: tests

    a = list(sys.argv[1:])
    if not a:
        a = ["--mode", "consensus", "--param", "consec_loss_scale",
             "--values", "0,1"]
    if "--baseline" not in a:
        a += ["--baseline", "P0_验证器基线"]
    if "--count" not in a:
        a += ["--count", str(COUNT)]
    sys.argv = ["_evolve_wf.py"] + a
    return W.main()


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""AM-EVOLVE R30（2026-10-14 午休）候选 A：动量回看窗口 × 入池动量下限的交互轴。

★ 为什么重开这条「已闭合」的轴
    `momentum_lookback`（现 60）在 §五 已证伪清单里标注「60 闭合」。但那次扫描
    是在 **`momentum_min_mom = 0.0`** 的旧基线上做的——当时入池条件是「动量 > 0」，
    而 top_n=3 只取前三名，放宽/收紧回看窗口根本排不进前 3 ⇒ 该轴**完全惰性**（dSh≈0）。
    2026-10-13 R28 把 `momentum_min_mom` 由 0.0 收紧到 **0.15** 后，入池条件变成
    「60 日涨幅 > +15%」这一**绝对值硬门槛**，闸门从「相对排序」变成「绝对过滤」
    ⇒ 回看窗口重新成为**绑定约束**：窗口越短，+15% 越容易达成。

★ 今日实盘铁证（本轮起因，非凭空假设）
    2026-10-14 上午 09:15~11:25 连续 14 次
      「无入场信号：动量闸门后候选池为空（当前宇宙无 60 日正动量标的）」
    tushare 独立复核（面板末根 20261013，23 只 AI 宇宙）：
      60 日动量最高 **+5.52%**（300570.SZ），> +15% 的 **0 只**，> 0 的仅 3 只
    ⇒ 60 日窗口下整个宇宙在 60 个交易日内没有一只涨超 15%，闸门恒定关闭。
    若窗口缩短到 20/30/40 日，同样的近期反弹更可能越过 +15%。

★ 与「KPI#4 低暴露」的关系
    本轴是**唯一**在不放宽入场质量的前提下恢复暴露能力的旋钮：它不改变
    「只买最强的、不矮子里拔将军」的语义（min_mom 仍 0.15），只把「最强」的
    度量周期缩短。风险是短窗口噪声更大、换手更高。

★ 数据口径
    强制注入 R28/R29 同款 tushare 面板 ts_panel_25_901_20211201_20261013.pkl，
    与生产基线常量同源，避免自动化 shell 内 xtdata 回落 mock（§六 纪律）。

用法：
    python scripts/_am_lookback_20261014.py --mode grid --param momentum_lookback --values 20,30,40,50,60,80,100,120
    python scripts/_am_lookback_20261014.py --mode consensus --param momentum_lookback --values 40,50
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
        print(f"[R30][FATAL] 缺失基础面板缓存 {BASE_CACHE}")
        return 2
    panel = pickle.loads(BASE_CACHE.read_bytes())
    print(f"[R30] 注入 tushare 面板 {len(panel)} 只 <- {BASE_CACHE.name}")

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
        a = ["--mode", "grid", "--param", "momentum_lookback",
             "--values", "20,30,40,50,60,80,100,120"]
    if "--count" not in a:
        a += ["--count", str(COUNT)]
    sys.argv = ["_evolve_wf.py"] + a
    return W.main()


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""
离线复算「板块轮动 · 谁先被换出」优先级。

用真实日线（tushare pro.daily，受 .env 的 TUSHARE_TOKEN 驱动）对当前 5 只持仓
逐一跑 DailyContext._compute（与 live/回测严格同口径的 6 因子日线评分），
按 score 升序输出「最弱 → 最强」，即轮动机制「先卖最弱」的换出顺序。

不依赖 xtdata / miniQMT / 桌面会话，纯离线复算。
"""
from __future__ import annotations
import sys, os
from datetime import datetime, timedelta

# 让 config.settings 的 _load_dotenv() 能从根目录 .env 载入 TUSHARE_TOKEN
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from config.settings import TUSHARE_TOKEN
from data.tushare_client import tushare_client
from strategy.daily_context import DailyContext

# 来自 storage/qmt.db engine_state (id=1, trade_date 2026-09-07) 的 5 只持仓
HOLDINGS = {
    "688072.SH": {"qty": 100, "avg": 660.00, "last": 651.92},
    "688120.SH": {"qty": 200, "avg": 260.93, "last": 246.20},
    "002415.SZ": {"qty": 6700, "avg": 35.85, "last": 34.87},   # 海康威视
    "688082.SH": {"qty": 200, "avg": 281.50, "last": 281.40},
    "301165.SZ": {"qty": 700, "avg": 119.87, "last": 111.99},
}

NAMES = {
    "688072.SH": "翱捷科技", "688120.SH": "华海清科", "002415.SZ": "海康威视",
    "688082.SH": "盛美上海", "301165.SZ": "锐捷网络",
}


def fetch_daily(code: str, count: int = 120) -> dict | None:
    if not tushare_client._connect():
        print("Tushare 未连接（token 无效或缺失）", file=sys.stderr)
        return None
    start = (datetime.now() - timedelta(days=count * 2)).strftime("%Y%m%d")
    end = datetime.now().strftime("%Y%m%d")
    df = tushare_client._pro.daily(
        ts_code=code, start_date=start, end_date=end,
        fields="trade_date,open,high,low,close,vol",
    )
    if df is None or df.empty:
        return None
    # tushare 偶发把 trade_date 作为索引返回，统一规整
    if "trade_date" not in df.columns:
        df = df.reset_index()
    df = df.sort_values("trade_date")
    return {
        "open": df["open"].tolist(),
        "high": df["high"].tolist(),
        "low": df["low"].tolist(),
        "close": df["close"].tolist(),
        "volume": df["vol"].tolist(),
    }


def momentum_60d(closes: list, lookback: int = 60) -> float:
    if not closes or len(closes) < lookback + 1:
        return 0.0
    base = closes[-(lookback + 1)]
    if base <= 0:
        return 0.0
    return closes[-1] / base - 1.0


def main():
    if not TUSHARE_TOKEN:
        print("⚠️ 未找到 TUSHARE_TOKEN（config/settings 读取为空）", file=sys.stderr)
        return
    print(f"TUSHARE_TOKEN 长度={len(TUSHARE_TOKEN)}，开始离线复算 5 只持仓日线评分...\n")

    rows = []
    raw = {}
    for code, info in HOLDINGS.items():
        d = fetch_daily(code)
        if not d or len(d["close"]) < 60:
            print(f"  {code} 日线数据不足（{len(d['close']) if d else 0} 根），跳过", file=sys.stderr)
            continue
        f = DailyContext._compute_for_test(code, d)
        pnl = (info["last"] - info["avg"]) / info["avg"] * 100
        rows.append((code, f, pnl))
        raw[code] = d["close"]

    if not rows:
        print("无可用日线，无法复算。", file=sys.stderr)
        return

    # 升序：score 越小越弱 → 越先被轮动换出
    rows.sort(key=lambda r: (r[1].score, r[2]))

    print(f"{'排名':<4}{'代码':<10}{'名称':<10}{'日线评分':>8}{'浮动盈亏':>9}{'trend_up':>9}{'bias':>7}{'60d动量':>9}")
    print("-" * 72)
    for i, (code, f, pnl) in enumerate(rows, 1):
        mom = momentum_60d(raw.get(code, [])) * 100
        print(f"{i:<4}{code:<10}{NAMES.get(code,''):<10}{f.score:>8.2f}{pnl:>+8.2f}%{str(f.trend_up):>9}{f.bias:>7.2f}{mom:>+8.1f}%")

    print("\n=== 轮动换出优先级（最弱先出）===")
    for i, (code, f, pnl) in enumerate(rows, 1):
        print(f"  {i}. {code} {NAMES.get(code,'')}  评分={f.score:.2f}  浮亏={pnl:+.2f}%")
        print(f"     因子={f.factors}")

    print("\n说明：当「热板块候选」（LLM rerank top ∪ 板块推荐池 top，"
          "rotation_hot_top_n=15）出现且当日涨幅≥rotation_intraday_breakout_pct"
          "(1.5%=突破) 或评分差≥阈值时，引擎取「最弱持仓」先卖再买候选。"
          "上表 score 升序即换出顺序；同分时以浮亏更大者优先（排序键第二字段）。")


if __name__ == "__main__":
    main()

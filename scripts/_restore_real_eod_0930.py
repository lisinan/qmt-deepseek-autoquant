# -*- coding: utf-8 -*-
"""补记 2026-09-30 的**真实**账户收盘（假成交已回滚）。

为什么需要本脚本：
  repair_mock_fills_20260930.py 把 09-30 全天 262 条被合成行情污染的快照
  **整体删除**了（当日曲线作废，这是对的）。但由此造成两个后果：
    ① 账本缺 09-30 一行 ⇒ KPI #4「近 4 周滚动收益」少一天；
    ② 更关键：`record_ledger.prev_snapshot_day('2026-10-08')` 会跳过 09-30
       直接取 09-29 ⇒ **节后首日的跨日收益会跨过一个交易日算错**。
  故这里用**真实收盘价**还原 09-30 的 EOD，补一条快照 + 一行账本。

真实 EOD 的来历（可复核）：
  现金 483,926.60（回滚后的引擎状态）
  + 5 仓按 09-30 真实收盘价估值
      300394.SZ 200 × 260.77 =  52,154.00
      000977.SZ 1500 ×  65.93 =  98,895.00
      300308.SZ  100 × 808.44 =  80,844.00
      688008.SH  400 × 202.31 =  80,924.00
      002415.SZ 5500 ×  32.46 = 178,530.00
                                 -----------
      市值合计                    491,347.00
  = **975,273.60**（对比 09-29 EOD 978,840.60 ⇒ 跨日 **−0.36%**）
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB = ROOT / "storage" / "qmt.db"

REAL_EOD_0930 = 975273.60
CASH_0930 = 483926.60
MV_0930 = 491347.00
POS_0930 = 5
PREV_0930 = 978840.60          # 09-29 EOD
RANGE_BASE = 1_000_000.0       # 2026-09-21 账户复位


def main() -> None:
    con = sqlite3.connect(str(DB))
    cur = con.cursor()

    cur.execute("SELECT COUNT(*) FROM equity_snapshots "
                "WHERE substr(ts,1,10)='2026-09-30'")
    have = cur.fetchone()[0]
    if have == 0:
        cur.execute(
            "INSERT INTO equity_snapshots "
            "(ts,total_asset,cash,market_value,positions_count,drawdown_pct,mode) "
            "VALUES (?,?,?,?,?,?,?)",
            ("2026-09-30T15:00:00", REAL_EOD_0930, CASH_0930,
             MV_0930, POS_0930, 0.0, "paper"))
        print(f"[ok] 补 09-30 真实 EOD 快照：{REAL_EOD_0930:,.2f} "
              f"（cash {CASH_0930:,.2f} + mv {MV_0930:,.2f}, {POS_0930} 仓）")
    else:
        print(f"[skip] 09-30 已有 {have} 条快照，不重复插入")
    con.commit()
    con.close()

    # ---- 账本行 ----
    import strategy.record_ledger as RL  # noqa: E402
    rows = RL.read_ledger()
    if any(r.get("date") == "2026-09-30" for r in rows):
        print("[skip] 账本已有 09-30 行")
        return
    row = {
        "date": "2026-09-30",
        "prev_asset": round(PREV_0930, 2),
        "eod_asset": round(REAL_EOD_0930, 2),
        "daily_ret_pct": round((REAL_EOD_0930 / PREV_0930 - 1) * 100, 2),
        "daily_pnl": round(REAL_EOD_0930 - PREV_0930, 2),
        "range_pct": round((REAL_EOD_0930 / RANGE_BASE - 1) * 100, 2),
        "stable": None, "safety": None, "accuracy": None,
        "efficiency": None, "overall": None,
        "halt_count": None, "max_consec_loss": None,
        "fills_n": 0, "eod_positions_n": POS_0930,
        "source": "manual_repair",
        "note": ("09-30 合成行情假成交已回滚；本行按真实收盘价重算 "
                 "(现金 + 5 仓真实市值)，非引擎快照"),
    }
    RL.append_markdown(row)
    RL.append_jsonl(row)
    print(f"[ok] 账本补记 09-30：{row['daily_ret_pct']:+.2f}%  "
          f"EOD {row['eod_asset']:,.2f}  [source=manual_repair]")


if __name__ == "__main__":
    main()

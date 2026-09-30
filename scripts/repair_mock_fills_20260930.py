# -*- coding: utf-8 -*-
"""【OWNER 手工修复工具 · 默认只打印计划，不落库】

2026-09-30 上午，引擎因 miniQMT 探活失败落到 ``_MockClient``（合成行情），
却照常成交，用假价格把 5 只持仓全部「硬止损」卖出：

    标的          数量    成本      mock 成交价   真实收盘(20260929)   假亏
    300394.SZ      200   267.93       89.818         255.99        -35,622.40
    000977.SZ     1500    69.90       44.873          67.18        -37,540.50
    300308.SZ      100   800.00      132.376         813.01        -66,762.40
    688008.SH      400   209.18      106.499         212.60        -41,072.40
    002415.SZ     5500    32.63       26.747          32.11        -32,356.50
                                                    合计            -213,354.20

并触发 daily_loss_abs 熔断、清空持仓、账户冻结（总资产 978,840.60 → 772,145.40）。

本脚本把账面**回滚到 2026-09-29 收盘状态**（撤 5 笔假成交、还原 5 只持仓与现金、
解除熔断）。

用法（**必须先停引擎**，否则引擎每 60s 会把内存状态写回覆盖）：
    python scripts/repair_mock_fills_20260930.py            # 只打印计划
    python scripts/repair_mock_fills_20260930.py --apply    # 落库（先自动备份）

修复的是**账本**不是收益：真实市场当日并无这些亏损，故回滚是还原事实。
"""
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB = Path("storage/qmt.db")
BACKUP = Path("storage/qmt.db.bak_20260930_mock_repair")

FILL_IDS = [365, 366, 367, 368, 369]
ORDER_IDS = [428, 429, 430, 431, 432]

# 2026-09-29 收盘状态（equity_snapshots 末条 + fills 反推，真实价）
EOD_CASH = 483_926.60
EOD_MV = 494_914.00
POSITIONS = [
    # code, name, qty, avg_cost, close(20260929), open_date, stop_price
    ("300394.SZ", "天孚通信", 200, 267.93, 255.99, "2026-09-25T09:15:25", 171.26),
    ("000977.SZ", "浪潮信息", 1500, 69.90, 67.18, "2026-09-28T09:15:05", 51.62),
    ("300308.SZ", "中际旭创", 100, 800.00, 813.01, "2026-09-29T09:15:49", 543.04),
    ("688008.SH", "澜起科技", 400, 209.18, 212.60, "2026-09-29T11:26:23", 143.18),
    ("002415.SZ", "海康威视", 5500, 32.63, 32.11, "2026-09-28T09:15:05", 26.76),
]


def main() -> int:
    apply = "--apply" in sys.argv
    if not DB.exists():
        print(f"找不到 {DB}")
        return 1

    conn = sqlite3.connect(str(DB))
    cur = conn.cursor()

    print("=" * 78)
    print("修复计划（回滚 2026-09-30 上午的 mock 假成交）")
    print("=" * 78)
    cur.execute("select id,ts,code,side,quantity,price,amount from fills "
                "where id in (%s) order by id" % ",".join("?" * len(FILL_IDS)),
                FILL_IDS)
    rows = cur.fetchall()
    print(f"1) 删除 fills {len(rows)} 笔：")
    for r in rows:
        print("   ", r)
    cur.execute("select id,ts,code,side from orders where id in (%s) order by id"
                % ",".join("?" * len(ORDER_IDS)), ORDER_IDS)
    print(f"2) 删除 orders {len(cur.fetchall())} 笔")

    pos_json = json.dumps([
        {"code": c, "name": n, "quantity": q, "avg_cost": a,
         "last_price": px, "open_date": od,
         "peak_price": max(a, px), "stop_price": s, "target_price": 0.0}
        for (c, n, q, a, px, od, s) in POSITIONS
    ], ensure_ascii=False)
    print(f"3) 还原 engine_state：cash={EOD_CASH:,.2f} "
          f"positions={len(POSITIONS)} 只 mv={EOD_MV:,.2f} "
          f"total={EOD_CASH + EOD_MV:,.2f}")
    print("4) 解除熔断：risk_state.halted=false、consec_loss=0、daily_pnl=0")
    print("5) 删除 2026-09-30 的 equity_snapshots（当日曲线整体作废）")

    if not apply:
        print()
        print("（dry-run，未落库。确认后加 --apply。运行前请先停止交易引擎）")
        conn.close()
        return 0

    shutil.copy2(str(DB), str(BACKUP))
    print(f"\n已备份 -> {BACKUP}")

    cur.execute("delete from fills where id in (%s)"
                % ",".join("?" * len(FILL_IDS)), FILL_IDS)
    cur.execute("delete from orders where id in (%s)"
                % ",".join("?" * len(ORDER_IDS)), ORDER_IDS)
    cur.execute("delete from equity_snapshots where ts like '2026-09-30%'")
    risk = json.dumps({
        "halted": False, "halt_reason": "", "halt_day": "",
        "consec_loss_date": "", "last_loss_day": "",
    }, ensure_ascii=False)
    cur.execute(
        "update engine_state set cash=?, positions=?, daily_pnl=0, "
        "consec_loss=0, daily_trade_count=0, trade_date='2026-09-29', "
        "risk_state=? where id=1",
        (EOD_CASH, pos_json, risk))
    conn.commit()
    print(f"已回滚：cash={EOD_CASH:,.2f} + mv={EOD_MV:,.2f} "
          f"= {EOD_CASH + EOD_MV:,.2f}（= 2026-09-29 EOD 978,840.60）")
    print("提示：重启引擎后确认日志出现『数据源=xtdata』再放行交易。")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

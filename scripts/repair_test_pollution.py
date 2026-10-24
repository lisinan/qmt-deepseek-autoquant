# -*- coding: utf-8 -*-
"""WP-M（2026-10-21 测试污染）清理脚本：只删数据，不动 schema。

背景：tests/ 下的夹具曾直接用真实 Storage（未重定向 QMT_DB），
把伪造成交写进了生产库 storage/qmt.db。污染指纹：
  - price 恰好等于魔数 100.0（测试桩默认价）；
  - 发生在非交易时段（21:48 ~ 22:30）；
  - order_id 只能是 ""（paper 桩）或 "o1"（live 桩）。

实测污染范围（2026-10-22 核对）：
  - fills  id 375-383（9 行：375-377 是与 378 同批同秒的 300308.SZ paper 幽灵成交，
           381-383 是 600519.SH 假 live 成交）；
  - orders id 438-443（6 行：与 fills 375-380 一一同 ts 对应的委托侧记录；
           600519.SH 的 live 三笔成交在 orders 无对应行）。
  - signals / ai_analyses / llm_rerank 同窗口 0 行，未污染；
  - risk_snapshots / engine_state 同窗口的行均为引擎正常写入，非污染。

用法：
  python scripts/repair_test_pollution.py            # 默认 dry-run，只列出
  python scripts/repair_test_pollution.py --apply    # 真删（先备份到 logs/）

铁律对齐：筛选条件全部写成下面的具名常量，绝不按 id 硬删；
删除前把受影响行备份到 logs/test_pollution_backup_<date>.json；
生产库正被引擎（WAL 模式）写入，本脚本只用短事务 DELETE，不动 schema、不 kill 进程。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "storage" / "qmt.db"

# ---------------- 具名筛选常量（绝不按 id 硬删） ----------------
PRICE_MAGIC = 100.0            # 测试桩默认价：恰好 100.0
TS_FLOOR = "2026-10-21T21:00:00"   # 污染窗口下界（非交易时段 21:48 起）
TS_CEIL = "2026-10-21T23:59:59"    # 污染窗口上界（22:30 后无污染行，留裕量）
ORDER_IDS = ("", "o1")         # paper 桩空串 / live 桩 "o1"

FILL_COLS = ("id", "ts", "code", "side", "quantity", "price", "order_id", "mode")
ORDER_COLS = ("id", "ts", "code", "side", "quantity", "price",
              "order_type", "account", "order_id", "mode")


def _match_where() -> tuple[str, tuple]:
    """指纹 WHERE 子句（fills / orders 同构：两表都有这些列）。"""
    placeholders = ",".join("?" * len(ORDER_IDS))
    return (
        f"price = ? AND ts >= ? AND ts <= ? AND order_id IN ({placeholders})",
        (PRICE_MAGIC, TS_FLOOR, TS_CEIL) + ORDER_IDS,
    )


def _fetch(c: sqlite3.Connection, table: str, cols: tuple) -> list[dict]:
    where, params = _match_where()
    rows = c.execute(
        f"SELECT {', '.join(cols)} FROM {table} WHERE {where} ORDER BY id",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description="清理测试夹具污染的生产库行")
    parser.add_argument("--apply", action="store_true",
                        help="真删（默认 dry-run 只列出）")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"[ERR] 数据库不存在: {DB_PATH}")
        return 1

    # 引擎正以 WAL 写入库；timeout 给足重试，只做短事务，不动 schema。
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        fills = _fetch(conn, "fills", FILL_COLS)
        orders = _fetch(conn, "orders", ORDER_COLS)

        before_fills_total = conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
        before_orders_total = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        print(f"[{'APPLY' if args.apply else 'DRY-RUN'}] "
              f"fills 总数={before_fills_total}, orders 总数={before_orders_total}")
        print(f"\n候选 fills（指纹命中 {len(fills)} 行）:")
        for r in fills:
            print("  ", json.dumps(r, ensure_ascii=False))
        print(f"\n候选 orders（指纹命中 {len(orders)} 行）:")
        for r in orders:
            print("  ", json.dumps(r, ensure_ascii=False))

        if not args.apply:
            print("\n[DRY-RUN] 未删除任何行。加 --apply 执行真删。")
            return 0

        if not fills and not orders:
            print("\n[APPLY] 指纹命中 0 行，无需删除（此前已清理）。")
            return 0

        # ---- 备份受影响行（删前，务必成功才继续） ----
        backup = ROOT / "logs" / (
            f"test_pollution_backup_{datetime.now():%Y%m%d}.json")
        backup.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "created_at": datetime.now().isoformat(),
            "db": str(DB_PATH),
            "fingerprint": {
                "PRICE_MAGIC": PRICE_MAGIC,
                "TS_FLOOR": TS_FLOOR, "TS_CEIL": TS_CEIL,
                "ORDER_IDS": list(ORDER_IDS),
            },
            "fills": fills,
            "orders": orders,
        }
        backup.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[APPLY] 已备份 {len(fills)} fills + {len(orders)} orders -> {backup}")

        # ---- 真删：一个短事务，按指纹删，不按 id ----
        where, params = _match_where()
        with conn:  # 事务：要么全删要么全不删
            cur_f = conn.execute(f"DELETE FROM fills WHERE {where}", params)
            cur_o = conn.execute(f"DELETE FROM orders WHERE {where}", params)
            print(f"[APPLY] DELETE fills 指纹命中 {cur_f.rowcount} 行")
            print(f"[APPLY] DELETE orders 指纹命中 {cur_o.rowcount} 行")

        after_fills_total = conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
        after_orders_total = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        print(f"\n[APPLY] 删后计数: fills {before_fills_total} -> {after_fills_total} "
              f"(-{before_fills_total - after_fills_total}), "
              f"orders {before_orders_total} -> {after_orders_total} "
              f"(-{before_orders_total - after_orders_total})")

        # 复核：指纹应命中 0 行
        residual = (_fetch(conn, "fills", FILL_COLS)
                    + _fetch(conn, "orders", ORDER_COLS))
        print(f"[APPLY] 复核指纹残留: {len(residual)} 行（应为 0）")
        return 0 if not residual else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

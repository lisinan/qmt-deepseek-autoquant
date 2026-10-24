# -*- coding: utf-8 -*-
"""DB 增长巡检（只读）。

每日盘后跑一次，输出各表行数 / 时间跨度 / db 文件大小，供增长趋势监控。
**绝不写库**：只开只读连接（mode=ro），不触发 schema 迁移，不影响运行中的引擎。

用法：
    python scripts/report_db_growth.py
    python scripts/report_db_growth.py --json     # 机读格式（接巡检管道）
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import BASE_DIR  # noqa: E402

# 全部业务表（含 table_stats() 未覆盖的 llm_rerank / engine_state，消除巡检盲区）。
TABLES = (
    "signals", "orders", "fills", "ai_analyses",
    "risk_snapshots", "equity_snapshots",
    "sector_recommendations", "llm_rerank", "engine_state",
)

# 账务证据 / 引擎状态：永不删除（见 reports/DB_MAINTENANCE_2026-10-22.md §4）。
PROTECTED = ("orders", "fills", "equity_snapshots", "engine_state")


def _db_path() -> Path:
    override = (os.environ.get("QMT_DB") or "").strip()
    return Path(override) if override else BASE_DIR / "storage" / "qmt.db"


def collect() -> dict:
    path = _db_path()
    out = {
        "db": str(path),
        "size_mb": round(path.stat().st_size / 1024 / 1024, 1) if path.exists() else 0.0,
        "tables": [],
    }
    # mode=ro：即使误传写路径也物理无法写；WAL 库只读打开对运行中引擎安全。
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        for t in TABLES:
            row = {"table": t, "protected": t in PROTECTED}
            try:
                row["rows"] = conn.execute(
                    f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                lo, hi = conn.execute(
                    f"SELECT MIN(substr(ts,1,10)), MAX(substr(ts,1,10)) "
                    f"FROM {t}").fetchone()
                row["first"], row["last"] = lo, hi
            except sqlite3.Error as e:  # 表不存在等
                row["rows"], row["error"] = -1, str(e)
            out["tables"].append(row)
    finally:
        conn.close()
    return out


def _render(res: dict) -> str:
    lines = [
        f"[db-growth] {res['db']}  ({res['size_mb']:,.1f} MB)",
        f"{'table':<26}{'rows':>12}  {'span':<26}{'note'}",
    ]
    for r in res["tables"]:
        if r.get("rows", -1) < 0:
            lines.append(f"{r['table']:<26}{'-':>12}  {r.get('error', '')}")
            continue
        span = f"{r.get('first') or '-'} .. {r.get('last') or '-'}"
        note = "PROTECTED(账务/状态,不prune)" if r["protected"] else ""
        lines.append(f"{r['table']:<26}{r['rows']:>12,}  {span:<26}{note}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="DB 增长巡检（只读）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（机读）")
    args = ap.parse_args()
    res = collect()
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(_render(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

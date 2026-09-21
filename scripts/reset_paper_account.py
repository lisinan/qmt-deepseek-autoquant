# -*- coding: utf-8 -*-
"""paper 账本复位：**金额初始化 1,000,000，从指定交易日重新计量**。

背景：账号自 09-16 强平后冻结在 803,680.18（−19.63%），且连亏计数把仓位压到 0。
修复代码后需要一个干净的起点重新计量收益与回撤。

本脚本做四件事（**可逆**：先整库备份，旧数据一律归档不删除）：
  1. 把 storage/qmt.db 复制为 storage/qmt_backup_<时间戳>.db；
  2. 把计量相关表现有数据归档到 *_pre_reset 表（equity_snapshots / orders / fills）；
  3. 重置 engine_state → 现金 1,000,000、零持仓、风控基线清零；
  4. 写入一条 1,000,000 的起始权益快照，作为新周期的计量基线。

用法：
  python scripts/reset_paper_account.py            # 预演（只打印计划，不改数据）
  python scripts/reset_paper_account.py --apply    # 实际执行
  python scripts/reset_paper_account.py --apply --start-date 2026-09-22

⚠️ 执行前请先**停止交易引擎**。引擎每 60s 会把内存里的旧状态写回 engine_state，
   在运行中执行本脚本，改动会在一分钟内被覆盖（脚本会检测并告警）。
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "storage" / "qmt.db"
PID_FILE = ROOT / "logs" / "engine.pid"
INITIAL_CAPITAL = 1_000_000.0

# 归档这三张表：它们直接决定「收益/回撤/成交」的计量口径
ARCHIVE_TABLES = ("equity_snapshots", "orders", "fills")


def engine_alive() -> bool:
    """引擎是否还在跑（运行中改库会被内存状态覆盖）。"""
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
    except Exception:
        return False
    if sys.platform != "win32":
        return Path(f"/proc/{pid}").exists()
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际执行（默认仅预演）")
    ap.add_argument("--start-date", default="", help="新周期首个交易日，默认明天")
    args = ap.parse_args()

    start = (date.fromisoformat(args.start_date) if args.start_date
             else date.today() + timedelta(days=1))

    if not DB.exists():
        print(f"找不到数据库：{DB}")
        return 1

    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM engine_state WHERE id=1").fetchone()
    cur_cash = float(row["cash"]) if row and row["cash"] else 0.0
    cur_pos = len(__import__("json").loads(row["positions"] or "[]")) if row else 0
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ARCHIVE_TABLES}

    print("=" * 72)
    print(f"paper 账本复位{'（实际执行）' if args.apply else '（预演，未改动任何数据）'}")
    print("=" * 72)
    print(f"  新周期起始交易日 : {start}")
    print(f"  初始资金         : {INITIAL_CAPITAL:,.2f}")
    print(f"  当前现金 / 持仓  : {cur_cash:,.2f} / {cur_pos} 只")
    print(f"  待归档记录数     : " + "  ".join(f"{t}={n}" for t, n in counts.items()))
    print(f"  整库备份         : storage/qmt_backup_<时间戳>.db（约 {DB.stat().st_size/1e6:.1f} MB）")

    if engine_alive():
        print("\n" + "!" * 72)
        print("!! 警告：交易引擎仍在运行（logs/engine.pid）。")
        print("!! 引擎每 60 秒会把内存状态写回 engine_state，本次改动将被覆盖。")
        print("!! 请先停止引擎，再执行本脚本，然后重启引擎。")
        print("!" * 72)
        if not args.apply:
            return 2

    if not args.apply:
        print("\n预演结束。确认无误后加 --apply 执行。")
        return 0

    # ---- 1) 整库备份 ----
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = ROOT / "storage" / f"qmt_backup_{ts}.db"
    con.close()                      # 先关连接，避免 WAL 未落盘
    shutil.copy2(str(DB), str(backup))
    con = sqlite3.connect(str(DB))
    print(f"\n[1/4] 已备份 → {backup.name}")

    # ---- 2) 归档计量表（逐步提交，避免部分失败留下不一致状态）----
    for t in ARCHIVE_TABLES:
        arch = f"{t}_pre_reset"
        con.execute(f"DROP TABLE IF EXISTS {arch}")
        con.commit()
        con.execute(f"CREATE TABLE {arch} AS SELECT * FROM {t}")
        con.commit()
        con.execute(f"DELETE FROM {t}")
        con.commit()
        print(f"[2/4] 归档 {t} → {arch}（{counts[t]} 行），主表已清空")
    # 归档表打时间戳，便于日后分辨
    try:
        con.execute("CREATE TABLE IF NOT EXISTS reset_meta (k TEXT PRIMARY KEY, v TEXT)")
        con.execute("INSERT OR REPLACE INTO reset_meta VALUES ('archived_at', ?)",
                    (datetime.now().isoformat(),))
    except Exception as e:
        print("   （reset_meta 写入失败，忽略）", e)

    # ---- 3) 重置 engine_state（资金 + 风控基线）----
    # 裸 sqlite3 连接不会触发 Storage 的幂等迁移，这里显式补列（已存在则忽略）。
    try:
        con.execute("ALTER TABLE engine_state ADD COLUMN risk_state TEXT")
        con.commit()
    except Exception:
        pass   # 列已存在
    con.execute("DELETE FROM engine_state")
    con.execute(
        "INSERT INTO engine_state("
        "id, ts, cash, positions, daily_trade_count, daily_pnl, consec_loss,"
        " peak_asset, day_open_asset, tick_count, peak_equity, trade_date, risk_state)"
        " VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?)",
        (datetime.now().isoformat(), INITIAL_CAPITAL, "[]", 0, 0.0, 0,
         INITIAL_CAPITAL, INITIAL_CAPITAL, 0, INITIAL_CAPITAL,
         start.isoformat(),
         '{"halted": false, "halt_reason": "", "halt_day": "", "consec_loss_date": ""}'),
    )
    con.commit()
    print(f"[3/4] engine_state 已复位：现金 {INITIAL_CAPITAL:,.2f}、零持仓、"
          f"连亏/日内盈亏/回撤峰值全部清零")

    # ---- 4) 写入起始权益快照（新周期计量基线）----
    con.execute(
        "INSERT INTO equity_snapshots("
        "ts, total_asset, cash, market_value, positions_count, drawdown_pct, mode)"
        " VALUES (?,?,?,?,?,?,?)",
        (f"{start.isoformat()}T09:30:00", INITIAL_CAPITAL, INITIAL_CAPITAL,
         0.0, 0, 0.0, "paper"),
    )
    con.commit()
    print(f"[4/4] 起始权益快照已写入 {start} 09:30 → {INITIAL_CAPITAL:,.2f}")

    # ---- 5) 落复位标志：引擎启动时会在**内存里**再复位一次并自动删除标志 ----
    #   解决「引擎运行中改库会被内存状态覆盖」的时序问题：无论何时重启都能生效一次。
    flag = ROOT / "storage" / ".reset_paper.flag"
    flag.write_text(
        '{"capital": %s, "start_date": "%s"}' % (INITIAL_CAPITAL, start.isoformat()),
        encoding="utf-8")
    print(f"[5/5] 复位标志已写入 storage/.reset_paper.flag"
          f"      → 引擎下次启动时在内存中复位并自动清除该标志（幂等）")

    print("\n完成。请重启交易引擎以加载复位后的账本。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

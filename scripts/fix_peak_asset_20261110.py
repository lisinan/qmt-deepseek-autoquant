# -*- coding: utf-8 -*-
"""一次性数据修正：把 ``engine_state.peak_asset`` 拉回真实历史峰值。

## 为什么需要这一步

``risk/manager.py:352-353``（2026-11-10 前）在**任何类型**熔断冷却恢复时都会
``self._peak_asset = total_asset``，于是 −20% 硬线的基线被系统性下移。实盘实证
（``risk_snapshots``，两次都发生在 09:30:1x）：

    11-03   66,542.31 → 63,207.94   （−3,334.37）
    11-09   67,453.62 → 58,162.49   （−9,291.13）
    11-10   60,568.49 → 61,846.18（当日随行情上行）⇒ 当前落库值 61,846.18

补丁 ``P2-halt-recover-no-peak-rebase`` 只保证**今后**不再下移，**不会自动修复
已经被写坏的存量值**。若不修，恢复出来的仍是 61,846.18：

    −20% 线 = 49,476.94   应为 53,962.90   差 4,485.96 元 = 权益 7.42%

## 安全设计（三道闸，任一不满足即拒绝执行）

1. **引擎必须已停**：``engine_state.ts`` 新鲜度 > 120s。引擎在跑时每 ~60s 写一次，
   直接在库里改会被内存状态覆盖回去 ⇒ 必须停引擎后改。
2. **双源取证且必须一致可用**：
   - ``risk_snapshots.payload_json.peak_asset`` 的 live 期最大值
   - ``equity_snapshots.total_asset``（``mode='live'``）按日聚合的最大值
   ★ ``equity_snapshots`` 里混有 **paper 的 1,020,446.6** 轨迹，**必须**按
   ``mode='live'`` 过滤（``credible_peak_from_daily`` 的零方差判据剔不掉它）。
3. **只上不下 + 合理性上限**：新值必须 ≥ 旧值，且 ≤ 当前权益 × 1.5（防写入脏值）。

用法：
    python scripts/fix_peak_asset_20261110.py --dry-run    # 只看不写
    python scripts/fix_peak_asset_20261110.py              # 实际修正（自动备份 DB）
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "storage" / "qmt.db"
STALE_MIN_SEC = 120.0          # engine_state 超过 2 分钟没更新 ⇒ 判定引擎已停
SANE_MULT = 1.5                # 新峰值不得超过当前权益的 1.5 倍


def _load_rows(db: Path):
    """只读打开，返回 (engine_state 行, risk_snapshots 行, equity 日聚合行)。"""
    con = sqlite3.connect("file:%s?mode=ro" % str(db).replace("\\", "/"), uri=True)
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT ts, peak_asset, peak_equity FROM engine_state ORDER BY id DESC LIMIT 1")
        es = cur.fetchone()
        cur.execute(
            "SELECT ts, payload_json FROM risk_snapshots WHERE ts >= '2026-10-30' "
            "ORDER BY id")
        rs = cur.fetchall()
        cur.execute(
            "SELECT COUNT(*), MAX(total_asset), MIN(total_asset) "
            "FROM equity_snapshots WHERE mode='live' GROUP BY substr(ts,1,10)")
        eq = cur.fetchall()
        cur.execute(
            "SELECT total_asset FROM equity_snapshots WHERE mode='live' "
            "ORDER BY id DESC LIMIT 1")
        last_eq = cur.fetchone()
        return es, rs, eq, last_eq
    finally:
        con.close()


def _peak_from_risk_snapshots(rows) -> float:
    peak = 0.0
    for _ts, pj in rows:
        try:
            d = json.loads(pj)
        except Exception:                            # noqa: BLE001
            continue
        v = d.get("peak_asset")
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v > peak:
            peak = v
    return peak


def _peak_from_equity_daily(rows) -> float:
    """按日聚合的 MAX(total_asset)：剔除「多快照零方差」脏日（与生产同判据）。"""
    peak = 0.0
    for n, mx, mn in rows:
        try:
            n = int(n or 0)
            mx = float(mx or 0.0)
            mn = float(mn or 0.0)
        except (TypeError, ValueError):
            continue
        if mx <= 0.0:
            continue
        if n >= 5 and mx == mn:
            continue
        if mx > peak:
            peak = mx
    return peak


def _live_engine_listening(port: int = 5000, timeout: float = 0.5) -> bool:
    """live 引擎是否仍在监听 :5000（= 进程活着）。

    为什么不能只看 ``engine_state.ts`` 新鲜度：午休（11:30–13:00）与收盘后引擎
    不写状态，实测 11:34 的 ts 在 12:48 看是「4481 秒前」，会**误判成已停**。
    端口是唯一与「进程是否活着」严格同号的信号。
    """
    import socket
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="跳过「引擎已停」检查（仅在确认已 stop 时用）")
    args = ap.parse_args()

    if not DB.exists():
        print("[ABORT] 找不到 live 库: %s" % DB)
        return 2

    es, rs_rows, eq_rows, last_eq = _load_rows(DB)
    if not es:
        print("[ABORT] engine_state 无数据")
        return 2
    ts, old_peak, old_peak_eq = es
    old_peak = float(old_peak or 0.0)
    cur_asset = float((last_eq or [0.0])[0] or 0.0)

    # ---- 闸 1：引擎必须已停 ----
    try:
        age = (datetime.now() - datetime.fromisoformat(str(ts))).total_seconds()
    except Exception:                                # noqa: BLE001
        age = 1e9
    alive = _live_engine_listening(5000)
    print("engine_state.ts          = %s（%.0f 秒前）" % (ts, age))
    print("engine_state.peak_asset  = %.2f" % old_peak)
    print("engine_state.peak_equity = %.2f" % float(old_peak_eq or 0.0))
    print("最新 live 总资产          = %.2f" % cur_asset)
    print("live 引擎 :5000          = %s" % ("**仍在监听（运行中）**" if alive else "未监听（已停）"))
    if alive and not args.force:
        print("\n[ABORT] live 引擎仍在监听 :5000 ⇒ 进程活着。"
              "\n        在库里改会被内存状态覆盖回去。请先 `python main.py --stop`"
              "（或 scripts\\run_live.bat 的停止流程）再执行本脚本。")
        return 3
    if age < STALE_MIN_SEC and not args.force:
        print("\n[ABORT] engine_state 刚被写过（%.0fs < %.0fs）⇒ 引擎疑似仍在运行。"
              % (age, STALE_MIN_SEC))
        return 3

    # ---- 闸 2：双源取证 ----
    p_rs = _peak_from_risk_snapshots(rs_rows)
    p_eq = _peak_from_equity_daily(eq_rows)
    print("\nrisk_snapshots 峰值(live期) = %.2f   ← 主证据（风控模块自己的 HWM）" % p_rs)
    print("equity_snapshots 峰值(mode=live, 剔脏日) = %.2f   ← 交叉校验" % p_eq)
    if p_eq <= 0.0 and p_rs <= 0.0:
        print("[ABORT] 两个源都取不到可信峰值，拒绝写入")
        return 4
    if p_eq > 0.0 and p_rs > 0.0 and p_eq > p_rs * 1.05:
        print("[ABORT] 两源相差过大（%.2f vs %.2f）⇒ 取证不可信，拒绝写入"
              % (p_eq, p_rs))
        return 4
    target = max(p_rs, p_eq)

    # ---- 闸 3：只上不下 + 合理性 ----
    if target <= old_peak:
        print("\n[SKIP] 取证峰值 %.2f <= 当前落库 %.2f，无需修正" % (target, old_peak))
        return 0
    if cur_asset > 0 and target > cur_asset * SANE_MULT:
        print("[ABORT] 取证峰值 %.2f > 当前权益 %.2f × %.1f ⇒ 疑为脏值，拒绝写入"
              % (target, cur_asset, SANE_MULT))
        return 5

    print("\n拟修正：peak_asset %.2f → %.2f（+%.2f）" % (old_peak, target, target - old_peak))
    print("  −20%% 硬线：%.2f → %.2f" % (old_peak * 0.8, target * 0.8))
    if args.dry_run:
        print("\n[DRY-RUN] 未写入。")
        return 0

    # ---- 备份 + 写入 ----
    # ★ 必须用 sqlite3 在线备份 API，不能 shutil.copy2 单个 .db 文件：
    #   本库存在 storage/qmt.db-wal（实测 3.9MB），最近的提交可能还在 WAL 里没回写
    #   主库文件 ⇒ 只复制 .db 会得到一个**缺最新提交的备份**，回滚时等于把账本
    #   退回到某个未定义时刻。con.backup() 会产出一致性快照（含 WAL 内容）。
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = DB.with_suffix(".db.bak-peakfix-%s" % stamp)
    src_con = sqlite3.connect("file:%s?mode=ro" % str(DB).replace("\\", "/"), uri=True)
    dst_con = sqlite3.connect(str(bak))
    try:
        src_con.backup(dst_con)
    finally:
        dst_con.close()
        src_con.close()
    print("\n[BAK ] %s（sqlite3 在线备份，含 WAL 内容）" % bak.name)

    # 备份自检：能打开、engine_state 可读、peak_asset 等于旧值
    chk = sqlite3.connect("file:%s?mode=ro" % str(bak).replace("\\", "/"), uri=True)
    try:
        row = chk.execute(
            "SELECT peak_asset FROM engine_state ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        chk.close()
    if not row or abs(float(row[0]) - old_peak) > 0.01:
        print("[FAIL] 备份自检不通过（peak_asset=%r，期望 %.2f）⇒ 拒绝继续写入"
              % (row, old_peak))
        return 8
    print("[ OK ] 备份自检通过（peak_asset=%.2f）" % float(row[0]))

    con = sqlite3.connect(str(DB))
    try:
        cur = con.cursor()
        cur.execute("UPDATE engine_state SET peak_asset=? WHERE id=(SELECT MAX(id) FROM engine_state)",
                    (round(target, 2),))
        if cur.rowcount != 1:
            con.rollback()
            print("[FAIL] 更新行数 = %d（期望 1）" % cur.rowcount)
            return 6
        con.commit()
        cur.execute("SELECT peak_asset FROM engine_state ORDER BY id DESC LIMIT 1")
        got = float(cur.fetchone()[0])
    finally:
        con.close()

    if abs(got - round(target, 2)) > 0.01:
        print("[FAIL] 回读校验不一致: %.2f" % got)
        return 7
    print("[ OK ] 回读校验 peak_asset = %.2f" % got)

    log = ROOT / "logs" / "PEAK_FIX_20261110.log"
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("\n=== %s peak_asset 修正 ===\n" % datetime.now().isoformat())
        fh.write("  旧值 %.2f -> 新值 %.2f\n" % (old_peak, got))
        fh.write("  证据 risk_snapshots=%.2f / equity(live)=%.2f\n" % (p_rs, p_eq))
        fh.write("  备份 %s\n" % bak.name)
    print("[ OK ] 日志 -> %s" % log.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())

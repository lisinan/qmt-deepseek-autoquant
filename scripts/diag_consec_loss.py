# -*- coding: utf-8 -*-
"""连亏信号质量排查：复算真实成交 round-trip PnL + 连亏序列 + 建仓信号还原。
仅读取 storage/qmt.db，不改写任何数据、不改配置。
"""
from __future__ import annotations
import sqlite3
from collections import deque, defaultdict
from datetime import datetime, date
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "storage" / "qmt.db"


def parse_ts(s: str) -> datetime:
    s = (s or "").replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return datetime.min


def d(s: str) -> date:
    return parse_ts(s).date()


def main():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    print("=== fills 数据范围 ===")
    lo, hi, n = cur.execute("SELECT MIN(ts), MAX(ts), COUNT(*) FROM fills").fetchone()
    print(f"  ts: {lo} ~ {hi}  (共 {n} 笔成交)")

    # 建仓信号缓存：code -> [(ts, score, reason)]
    sig_rows = cur.execute(
        "SELECT ts, code, score, reason FROM signals WHERE side='BUY' ORDER BY ts"
    ).fetchall()
    sig_by_code = defaultdict(list)
    for ts, code, score, reason in sig_rows:
        sig_by_code[code].append((parse_ts(ts), score, reason))

    def entry_signal(code: str, fill_ts: datetime):
        best = None
        for sts, sc, rsn in sig_by_code.get(code, []):
            if sts <= fill_ts:
                best = (sts, sc, rsn)
            else:
                break
        return best

    fills = cur.execute(
        "SELECT ts, code, side, quantity, price, mode FROM fills ORDER BY ts"
    ).fetchall()

    # 每 code 持仓 lot 队列: dict(ts, qty, price, score, reason)
    lots = defaultdict(deque)
    consec = 0
    sells = []
    for ts, code, side, qty, price, mode in fills:
        fts = parse_ts(ts)
        if side == "BUY":
            sig = entry_signal(code, fts)
            lots[code].append({
                "ts": fts, "qty": qty, "price": price,
                "score": sig[1] if sig else None,
                "reason": sig[2] if sig else None,
            })
        else:  # SELL
            remaining = qty
            total_qty = sum(l["qty"] for l in lots[code])
            total_cost = sum(l["qty"] * l["price"] for l in lots[code])
            avg_cost = (total_cost / total_qty) if total_qty > 0 else price
            pnl = (price - avg_cost) * qty
            # 消耗最老 lot 还原建仓信号
            entry_score = None
            entry_reason = None
            entry_ts = None
            while remaining > 0 and lots[code]:
                l = lots[code][0]
                take = min(l["qty"], remaining)
                if entry_ts is None:
                    entry_ts = l["ts"]
                    entry_score = l["score"]
                    entry_reason = l["reason"]
                remaining -= take
                if take >= l["qty"]:
                    lots[code].popleft()
                else:
                    l["qty"] -= take
            if pnl < 0:
                consec += 1
            else:
                consec = 0
            sells.append({
                "ts": ts, "code": code, "price": price, "avg_cost": avg_cost,
                "qty": qty, "pnl": pnl,
                "pnl_pct": (price - avg_cost) / avg_cost * 100 if avg_cost else 0,
                "consec": consec, "mode": mode,
                "entry_ts": entry_ts.strftime("%Y-%m-%d") if entry_ts else "?",
                "hold_days": (fts.date() - entry_ts.date()).days if entry_ts else -1,
                "entry_score": entry_score, "entry_reason": entry_reason,
            })

    print(f"\n=== 全部 SELL 成交（含 round-trip PnL 与连亏计数），共 {len(sells)} 笔 ===")
    print(f"{'ts':20} {'code':8} {'exit':>9} {'avgcost':>9} {'pnl':>10} {'%':>7} {'con':>3} {'hold':>5} {'escore':>7} reason")
    for s in sells:
        es = f"{s['entry_score']:.2f}" if isinstance(s['entry_score'], (int, float)) else "n/a"
        print(f"{s['ts'][:19]:20} {s['code']:8} {s['price']:>9.2f} {s['avg_cost']:>9.2f} "
              f"{s['pnl']:>10.1f} {s['pnl_pct']:>6.2f}% {s['consec']:>3} {s['hold_days']:>5} "
              f"{es:>7} {str(s['entry_reason'])[:24]}")

    # 定位最大连亏序列
    maxc = max((s["consec"] for s in sells), default=0)
    print(f"\n=== 最大连亏序列：{maxc} 次 ===")
    # 找连亏>=3 的区间
    run = []
    for s in sells:
        if s["consec"] >= 3:
            run.append(s)
    for s in run:
        print(f"  {s['ts'][:10]} {s['code']} pnl={s['pnl']:.0f} entry={s['entry_ts']} "
              f"hold={s['hold_days']}d escore={s['entry_score']} reason={s['entry_reason']}")

    # 仅看 09-15 附近
    print("\n=== 2026-09-15 附近（±10 日）SELL ===")
    for s in sells:
        if abs((d(s["ts"]) - date(2026, 9, 15)).days) <= 10:
            print(f"  {s['ts'][:19]} {s['code']} pnl={s['pnl']:.0f} consec={s['consec']} "
                  f"entry={s['entry_ts']} hold={s['hold_days']}d escore={s['entry_score']}")


if __name__ == "__main__":
    main()

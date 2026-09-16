# -*- coding: utf-8 -*-
"""总盈亏分析：复算 storage/qmt.db 全部成交的已实现 round-trip PnL。
聚合：总盈亏、胜率、平均盈/亏、盈亏比(profit factor)、每笔期望、按日累计曲线、
按建仓口径分 cohort（修复前 legacy vs 修复后 post-fix）、当前未平持仓。
仅读取，不改写任何数据 / 配置。
"""
from __future__ import annotations
import sqlite3
from collections import deque, defaultdict
from datetime import datetime, date
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "storage" / "qmt.db"

FIX_DATE = date(2026, 9, 2)          # 2026-09-02 P0 补丁落地日
DIRTY_MAX = date(2026, 8, 25)        # 修复前脏样本日（memory: 不作收益基准）


def parse_ts(s: str) -> datetime:
    s = (s or "").replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return datetime.min


def main():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    fills = cur.execute(
        "SELECT ts, code, side, quantity, price, mode FROM fills ORDER BY ts"
    ).fetchall()

    lots = defaultdict(deque)
    sells = []
    for ts, code, side, qty, price, mode in fills:
        fts = parse_ts(ts)
        if side == "BUY":
            lots[code].append({"ts": fts, "qty": qty, "price": price})
        else:
            total_qty = sum(l["qty"] for l in lots[code])
            total_cost = sum(l["qty"] * l["price"] for l in lots[code])
            avg_cost = (total_cost / total_qty) if total_qty > 0 else price
            pnl = (price - avg_cost) * qty
            remaining = qty
            entry_ts = None
            while remaining > 0 and lots[code]:
                l = lots[code][0]
                take = min(l["qty"], remaining)
                if entry_ts is None:
                    entry_ts = l["ts"]
                remaining -= take
                if take >= l["qty"]:
                    lots[code].popleft()
                else:
                    l["qty"] -= take
            sells.append({
                "date": fts.date(), "code": code, "pnl": pnl,
                "entry_date": entry_ts.date() if entry_ts else None,
            })

    def stats(rows):
        n = len(rows)
        wins = [r["pnl"] for r in rows if r["pnl"] > 0]
        loss = [r["pnl"] for r in rows if r["pnl"] < 0]
        scratch = [r["pnl"] for r in rows if r["pnl"] == 0]
        tot = sum(r["pnl"] for r in rows)
        gw = sum(wins)
        gl = sum(loss)
        wr = (len(wins) / n * 100) if n else 0
        pf = (gw / abs(gl)) if gl else float("inf")
        aw = (gw / len(wins)) if wins else 0
        al = (gl / len(loss)) if loss else 0
        exp = (tot / n) if n else 0
        return dict(n=n, tot=tot, wins=len(wins), loss=len(loss),
                    scratch=len(scratch), win_amt=gw, loss_amt=gl,
                    win_rate=wr, profit_factor=pf, avg_win=aw, avg_loss=al,
                    expectancy=exp)

    def fmt(s):
        pf = "∞" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
        return (f"笔数={s['n']} 总盈亏={s['tot']:.0f} 胜={s['wins']} 负={s['loss']} "
                f"平={s['scratch']} 胜率={s['win_rate']:.1f}% 盈亏比={pf} "
                f"均盈={s['avg_win']:.0f} 均亏={s['avg_loss']:.0f} 每笔期望={s['expectancy']:.0f}")

    print("=== 全部成交已实现盈亏 ===")
    print("  " + fmt(stats(sells)))
    print("\n=== 剔除 2026-08-25 脏样本（memory: 不作收益基准）===")
    clean = [r for r in sells if r["date"] > DIRTY_MAX]
    print("  " + fmt(stats(clean)))

    print("\n=== 按建仓口径分 cohort（修复前 09-02 前 vs 修复后 09-02 起）===")
    legacy = [r for r in clean if r["entry_date"] and r["entry_date"] < FIX_DATE]
    post = [r for r in clean if r["entry_date"] and r["entry_date"] >= FIX_DATE]
    print("  修复前(legacy, entry<09-02): " + fmt(stats(legacy)))
    print("  修复后(post,  entry>=09-02): " + fmt(stats(post)))

    print("\n=== 连亏 4 笔贡献（688120/300394/688072/688082）===")
    streak_codes = {"688120.SH", "300394.SZ", "688072.SH", "688082.SH"}
    streak = [r for r in clean if r["code"] in streak_codes]
    print("  " + fmt(stats(streak)))
    print(f"  → 占干净样本总盈亏比例: {sum(r['pnl'] for r in streak)/stats(clean)['tot']*100:.1f}%")

    print("\n=== 按日累计已实现盈亏曲线（干净样本）===")
    by_date = defaultdict(float)
    for r in clean:
        by_date[r["date"]] += r["pnl"]
    cum = 0.0
    for d in sorted(by_date):
        cum += by_date[d]
        print(f"  {d}  当日={by_date[d]:>9.0f}  累计={cum:>10.0f}")

    # 当前未平持仓
    open_cost = 0.0
    open_n = 0
    for code, q in lots.items():
        for l in q:
            open_cost += l["qty"] * l["price"]
            open_n += 1
    print(f"\n=== 当前未平持仓（仅成本，无可实现盈亏）===\n  剩余 lot 数={open_n} 总成本={open_cost:.0f}")


if __name__ == "__main__":
    main()

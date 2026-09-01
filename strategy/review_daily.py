# -*- coding: utf-8 -*-
"""
盘后复盘工具（交易日后一键验证优化）

从 ``storage/qmt.db``（fills / signals / orders / risk_snapshots /
equity_snapshots / ai_analyses）与 ``logs/notices.log``（耐久系统提示）重建
指定交易日的真实表现，并与回测验证基准（logs/verify_live_quality.json）对照，
输出：

  - reports/review_YYYY-MM-DD.html   人类可读复盘报告
  - logs/review_YYYY-MM-DD.json      结构化原始数据（供程序化二次分析）

覆盖维度：
  ① 成交与盈亏（含 EOD 持仓市值 / 未实现盈亏）
  ② 信号 → 成交转化（含逐笔信号明细）
  ③ 风控事件（risk_snapshots + notices 双源，避免漏记熔断）
  ④ 与回测基准对照
  ⑤ 系统提示流水
  ⑥ 权益曲线 / 当日收益率 / 日内最大回撤（equity_snapshots，缺失时回退解析心跳）
  ⑦ 逐笔交易明细（FIFO 配对，含建仓/平仓价、持仓天数、收益率）
  ⑧ AI 分析（若启用）

用法：
  python strategy/review_daily.py                 # 自动取最近一个有成交的交易日
  python strategy/review_daily.py --date 2026-08-31
  python strategy/review_daily.py --date 2026-08-31 --out-dir reports

注意：必须在 conda env ``qmt`` 下运行（sqlite3 依赖该环境 DLL）。
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Windows 控制台 UTF-8（本文件会打印中文进度）
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ❗ 必须先 import config.settings，再 import sqlite3。
# 原因：config/settings.py 里有一段 Windows ``os.add_dll_directory`` 修复，用于
# 把 conda env 的 ``Library\bin``（含 sqlite3.dll）加入搜索路径。若先 import
# sqlite3，在**未执行 conda activate** 的场景（如直接用结果解释器路径调用
# ``<env>/python.exe strategy/review_daily.py``，或由定时任务/自动化拉起）会直接抛
# ``ImportError: DLL load failed while importing _sqlite3``，复盘工具彻底跑不起来。
from config.settings import BASE_DIR, STRATEGY_PARAMS   # noqa: E402  (须先于 sqlite3)

import sqlite3                             # noqa: E402

from core.notices import notices_on_date   # noqa: E402
from storage.db import default_db_path     # noqa: E402

# 支持 QMT_DB 环境变量覆盖（与 Storage 一致），使单测/工具不碰生产库。
DB = default_db_path()
BASELINE = BASE_DIR / "logs" / "verify_live_quality.json"

_TA_PAT = re.compile(r"总资产=([\d,]+\.?\d*)")
_CASH_PAT = re.compile(r"现金=([\d,]+\.?\d*)")
_HALT_PAT = re.compile(r"触发熔断:\s*([^（]+)")


def E(v) -> str:
    """HTML 转义（报表里所有外部文本均须过这里）。

    为什么必需：报告会直接拼接 notice.msg / signal.reason / **LLM 的 summary**，
    而 LLM 输出是不可控文本——一个 ``<`` 就能把整份报表版式打烂。
    """
    return html.escape("" if v is None else str(v), quote=True)


# ----------------------------------------------------------------------------
# 数据读取
# ----------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    _ensure_equity_table(c)
    return c


def _ensure_equity_table(c: sqlite3.Connection) -> None:
    try:
        c.execute(
            "CREATE TABLE IF NOT EXISTS equity_snapshots ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, "
            "total_asset REAL, cash REAL, market_value REAL, "
            "positions_count INTEGER, drawdown_pct REAL)")
    except Exception:
        pass


def _day_prefix(target: str) -> str:
    return target + "T"


def _rows(c: sqlite3.Connection, table: str, target: str) -> list:
    p = _day_prefix(target)
    try:
        return [dict(r) for r in c.execute(
            f"SELECT * FROM {table} WHERE ts >= ? AND ts < ? ORDER BY ts",
            (p, target + "T23:59:59.999999"),
        ).fetchall()]
    except Exception as e:
        print(f"  [warn] 读取 {table} 失败: {e}")
        return []


def _rows_since(c: sqlite3.Connection, table: str, target: str,
                lookback_days: int) -> list:
    """读取 [target - lookback_days, target 末] 区间的记录（按 ts 升序）。

    【为何不直接读全史】复盘需要回溯到建仓腿（否则跘日 round-trip 会整笔
    丢弃，holdingsdays 恒 0），但数据库里可能堆着**多个彼此独立的旧 paper 会话**
    （本库实测：2026-08-25 多进程时代留下 250 笔成交，每个实例都从全新 100 万
    账本开始、重启即弃仓）。无限回溯会把这些已废弃的仓位当成今日持仓，
    算出「未实现盈亏 -794 万」这类无意义数字。
    因此默认回溯窗口取策略的最长持仓周期（trend_max_hold_days），
    既能覆盖真实建仓腿，又不把古代会话拉进来；可用 --lookback-days 调整。
    """
    try:
        d0 = date.fromisoformat(target) - timedelta(days=max(0, lookback_days))
        start = d0.isoformat()
    except Exception:
        start = "0000-01-01"
    try:
        return [dict(r) for r in c.execute(
            f"SELECT * FROM {table} WHERE ts >= ? AND ts < ? ORDER BY ts",
            (start, target + "T23:59:59.999999"),
        ).fetchall()]
    except Exception as e:
        print(f"  [warn] 读取 {table}(回溯窗) 失败: {e}")
        return []


def _state_positions(c: sqlite3.Connection) -> dict:
    """从 engine_state 读持仓（paper 模式的**权威账本**）。无则返回 {}。

    比 fills 重放可靠：它是引擎自己落盘的当前账本，不受历史会话污染。
    """
    try:
        row = c.execute("SELECT positions FROM engine_state WHERE id=1").fetchone()
        if not row or not row["positions"]:
            return {}
        out = {}
        for p in (json.loads(row["positions"]) or []):
            q = int(p.get("quantity") or 0)
            if q > 0:
                out[p["code"]] = {"qty": q, "avg": float(p.get("avg_cost") or 0.0)}
        return out
    except Exception:
        return {}


def _last_snapshot_positions_count(c: sqlite3.Connection, target: str):
    """当日最后一个权益快照里的持仓只数（用作交叉校验基准）。"""
    try:
        row = c.execute(
            "SELECT positions_count FROM equity_snapshots "
            "WHERE ts >= ? AND ts < ? ORDER BY id DESC LIMIT 1",
            (target + "T", target + "T23:59:59.999999"),
        ).fetchone()
        return int(row["positions_count"]) if row and row[0] is not None else None
    except Exception:
        return None


def _rows_upto(c: sqlite3.Connection, table: str, target: str) -> list:
    """读取**目标日及之前全部**记录（按 ts 升序）。

    【P1 修正 2026-09-02】原来复盘只读当天 fills 就去做 FIFO 配对，于是：
      ① ``_match_trades``的 lots 从空开始 → 「昨天买、今天卖」的 round-trip 因
         entry_qty=0 被整笔丢弃，``holding_days`` 恒为 0。而本策略是
         trend_max_hold_days=120 的趋势骑行——实际上**几乎所有交易都会被漏掉**，
         报表的「⑦ 逐笔交易明细」永远近似空表。
      ② ``_replay_fills``的 cost_basis 只算当天买入，而 market_value 包含所有
         持仓 → ``unrealized = market_value - cost_basis`` 对隔夜仓**严重虚高**。
    现在从建仓源头重放，只在输出层按目标日过滤。
    """
    try:
        return [dict(r) for r in c.execute(
            f"SELECT * FROM {table} WHERE ts < ? ORDER BY ts",
            (target + "T23:59:59.999999",),
        ).fetchall()]
    except Exception as e:
        print(f"  [warn] 读取 {table}(全史) 失败: {e}")
        return []


def _count(c: sqlite3.Connection, table: str, target: str) -> int:
    p = _day_prefix(target)
    try:
        return c.execute(
            f"SELECT COUNT(*) FROM {table} WHERE ts >= ? AND ts < ?",
            (p, target + "T23:59:59.999999"),
        ).fetchone()[0]
    except Exception:
        return 0


def _latest_trade_date(c: sqlite3.Connection) -> str:
    try:
        r = c.execute(
            "SELECT substr(ts,1,10) d, COUNT(*) n FROM fills "
            "GROUP BY d ORDER BY d DESC LIMIT 1"
        ).fetchone()
        if r:
            return r["d"]
    except Exception:
        pass
    return date.today().isoformat()


# ----------------------------------------------------------------------------
# 盈亏 / 持仓重建（扩展：返回 EOD 成本价，供未实现盈亏）
# ----------------------------------------------------------------------------

def _replay_fills(fills: list, target: str = None) -> dict:
    """平均成本法重建每个代码的已实现盈亏与 EOD 净持仓。

    :param fills:  **建议传入全史 fills**（目标日及之前）。
    :param target: 目标交易日 'YYYY-MM-DD'。传入时，已实现盈亏 / 买卖金额
        只统计**当日**成交，而持仓与成本基础仍由全史重放得出——这正是
        隔夜仓未实现盈亏能算对的关键。为 None 时退化为旧行为（全部计入）。
    """
    pos = {}          # code -> {"qty":int, "avg":float}
    realized = defaultdict(float)
    buy_amt = sell_amt = 0.0

    def _on_target(ts: str) -> bool:
        if target is None:
            return True
        return str(ts or "").startswith(target)

    for f in fills:
        code = f["code"]
        side = (f["side"] or "").upper()
        qty = int(f["quantity"] or 0)
        price = float(f["price"] or 0.0)
        today = _on_target(f.get("ts"))
        if side == "BUY":
            cur = pos.get(code, {"qty": 0, "avg": 0.0})
            tot = cur["qty"] + qty
            cur["avg"] = (cur["avg"] * cur["qty"] + price * qty) / tot if tot else 0.0
            cur["qty"] = tot
            pos[code] = cur
            if today:
                buy_amt += float(f["amount"] or price * qty)
        elif side == "SELL":
            cur = pos.get(code, {"qty": 0, "avg": 0.0})
            q = min(qty, cur["qty"])
            pnl = (price - cur["avg"]) * q
            if today:
                realized[code] += pnl
                sell_amt += float(f["amount"] or price * qty)
            cur["qty"] -= q
            if cur["qty"] <= 0:
                cur = {"qty": 0, "avg": 0.0}
            pos[code] = cur
    eod = {c: v["qty"] for c, v in pos.items() if v["qty"] != 0}
    eod_avg = {c: v["avg"] for c, v in pos.items() if v["qty"] != 0}
    cost_basis = round(sum(eod_avg[c] * eod[c] for c in eod), 2)
    return {
        "realized_total": round(sum(realized.values()), 2),
        "realized_by_code": {c: round(v, 2) for c, v in realized.items()},
        "eod_positions": eod,
        "eod_avg": eod_avg,
        "cost_basis": cost_basis,
        "buy_amount": round(buy_amt, 2),
        "sell_amount": round(sell_amt, 2),
    }


def _holding_days(entry_ts: str, exit_ts: str) -> int:
    try:
        d0 = datetime.fromisoformat(entry_ts).date()
        d1 = datetime.fromisoformat(exit_ts).date()
        return (d1 - d0).days
    except Exception:
        return 0


def _match_trades(fills: list, target: str = None) -> list:
    """FIFO 配对 BUY/SELL，重建逐笔 round-trip 交易（含持仓天数/收益率）。

    :param fills:  **建议传入全史 fills**；只传当天会使跘日交易的建仓腿缺失，
        entry_qty=0 而被整笔丢弃（趋势策略下几乎等于丢弃全部交易）。
    :param target: 传入时只返回**平仓发生在该日**的 round-trip（报表只关心当日），
        但建仓价/持仓天数仍来自全史配对，因而准确。

    同时记录建仓/平仓所属的执行模式（PAPER/LIVE），便于 paper→live 切换后核实。
    """
    lots = defaultdict(list)   # code -> [[ts, price, qty, mode], ...]
    trades = []
    for f in fills:
        code = f["code"]
        side = (f["side"] or "").upper()
        qty = int(f["quantity"] or 0)
        price = float(f["price"] or 0.0)
        ts = f["ts"]
        fmode = (f.get("mode") or "unknown")
        if side == "BUY":
            lots[code].append([ts, price, qty, fmode])
        elif side == "SELL" and qty > 0:
            remaining = qty
            entry_val = 0.0
            entry_qty = 0
            entry_ts = None
            entry_mode = None
            while remaining > 0 and lots[code]:
                lot = lots[code][0]
                take = min(remaining, lot[2])
                entry_val += take * lot[1]
                entry_qty += take
                if entry_ts is None:
                    entry_ts = lot[0]
                if entry_mode is None:
                    entry_mode = lot[3]
                lot[2] -= take
                remaining -= take
                if lot[2] <= 0:
                    lots[code].pop(0)
            if entry_qty > 0:
                ep = entry_val / entry_qty
                pnl = (price - ep) * entry_qty
                trades.append({
                    "code": code,
                    "entry_ts": entry_ts,
                    "exit_ts": ts,
                    "entry_price": round(ep, 3),
                    "exit_price": round(price, 3),
                    "qty": entry_qty,
                    "pnl": round(pnl, 2),
                    "holding_days": _holding_days(entry_ts, ts),
                    "return_pct": round((price / ep - 1) * 100, 2) if ep > 0 else 0.0,
                    "entry_mode": entry_mode,
                    "exit_mode": fmode,
                })
    if target is not None:
        trades = [t for t in trades
                  if str(t.get("exit_ts") or "").startswith(target)]
    trades.sort(key=lambda t: t["exit_ts"])
    return trades


# ----------------------------------------------------------------------------
# 权益曲线（equity_snapshots 优先；缺失时回退解析心跳）
# ----------------------------------------------------------------------------

def _parse_equity_from_notices(notices: list) -> list:
    series = []
    for n in notices:
        m = _TA_PAT.search(n.get("msg", ""))
        if not m:
            continue
        ta = float(m.group(1).replace(",", ""))
        mc = _CASH_PAT.search(n.get("msg", ""))
        cash = float(mc.group(1).replace(",", "")) if mc else 0.0
        series.append((n.get("ts", ""), ta, cash))
    series.sort(key=lambda x: x[0])
    return series


def _load_equity_series(c: sqlite3.Connection, target: str,
                        notices: list):
    """返回 (series, source)。series: [(ts, total_asset, cash), ...]"""
    rows = _rows(c, "equity_snapshots", target)
    if len(rows) >= 2:
        series = [(r["ts"], float(r["total_asset"] or 0.0),
                   float(r["cash"] or 0.0)) for r in rows]
        return series, "equity_snapshots"
    series = _parse_equity_from_notices(notices)
    return series, "notices(心跳)"


def _equity_stats(series: list) -> dict:
    if not series or len(series) < 2:
        return None
    tas = [s[1] for s in series]
    start, end = tas[0], tas[-1]
    peak, trough = max(tas), min(tas)
    run_peak = tas[0]
    max_dd = 0.0
    for v in tas:
        run_peak = max(run_peak, v)
        if run_peak > 0:
            max_dd = min(max_dd, (v - run_peak) / run_peak)
    daily_return_pct = (end / start - 1) * 100 if start > 0 else 0.0
    return {
        "start": round(start, 2),
        "end": round(end, 2),
        "peak": round(peak, 2),
        "trough": round(trough, 2),
        "daily_return_pct": round(daily_return_pct, 2),
        "intraday_max_dd_pct": round(max_dd * 100, 2),
        "points": len(series),
    }


def _equity_svg(series: list) -> str:
    if not series or len(series) < 2:
        return '<p style="color:#888">当日权益曲线数据不足（需 ≥2 个快照）。</p>'
    W, H, pad = 920, 240, 44
    # 兼容 tuple(...,total_asset,...) 与 dict(含 total_asset) 两种格式
    def _ta(s):
        if isinstance(s, dict):
            return float(s.get("total_asset") or 0.0)
        return float(s[1] if len(s) > 1 else 0.0)
    tas = [_ta(s) for s in series]
    lo, hi = min(tas), max(tas)
    if hi == lo:
        hi = lo + 1
    n = len(tas)

    def X(i):
        return pad + (W - 2 * pad) * i / (n - 1)

    def Y(v):
        return H - pad - (H - 2 * pad) * (v - lo) / (hi - lo)

    pts = [f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(tas)]
    base = tas[0]
    by = Y(base)
    line = " ".join(pts)
    area = (f"M{X(0):.1f},{Y(tas[0]):.1f} " +
            " ".join(f"L{p}" for p in pts[1:]) +
            f" L{X(n-1):.1f},{H-pad} L{X(0):.1f},{H-pad} Z")
    # 网格 + 标签
    grid = ""
    for g in range(4):
        gy = pad + (H - 2 * pad) * g / 3
        gv = hi - (hi - lo) * g / 3
        grid += (f'<line x1="{pad}" y1="{gy:.1f}" x2="{W-pad}" y2="{gy:.1f}" '
                 f'stroke="#e3e8ef" stroke-width="1"/>'
                 f'<text x="{W-pad+4}" y="{gy+4:.1f}" font-size="11" fill="#9aa7b4">'
                 f'{gv:,.0f}</text>')
    svg = f'''<svg viewBox="0 0 {W} {H}" width="100%" preserveAspectRatio="none"
 style="background:#fff;border:1px solid #eef0f3;border-radius:8px">
 {grid}
 <path d="{area}" fill="rgba(47,191,113,0.12)" stroke="none"/>
 <line x1="{pad}" y1="{by:.1f}" x2="{W-pad}" y2="{by:.1f}"
   stroke="#94a3b8" stroke-width="1" stroke-dasharray="5,4"/>
 <polyline points="{line}" fill="none" stroke="#2fbf71" stroke-width="2"/>
 <circle cx="{X(0):.1f}" cy="{Y(tas[0]):.1f}" r="3" fill="#2fbf71"/>
 <circle cx="{X(n-1):.1f}" cy="{Y(tas[-1]):.1f}" r="3" fill="#2fbf71"/>
 <text x="{pad}" y="{H-12}" font-size="11" fill="#9aa7b4">开 {tas[0]:,.0f}</text>
 <text x="{W-pad-90}" y="{H-12}" font-size="11" fill="#9aa7b4">收 {tas[-1]:,.0f}</text>
</svg>'''
    return svg


# ----------------------------------------------------------------------------
# 风控事件（risk_snapshots + notices 双源）
# ----------------------------------------------------------------------------

def _in_trading_window(ts: str) -> bool:
    """仅统计 A 股真实交易时段内的熔断。

    【修正 2026-09-02】边界从硬编码的 ``9 <= hour < 15`` 改为复用
    ``core.market_calendar.DEFAULT_SESSIONS``。原实现把 **15:00—15:05 收盘
    竞价窗口内的真熔断也排除了**（引擎的交易时段守卫本身跑到 15:05）。

    还要说明的是：这个过滤器当初是为了绕开单测写进生产库/日志的测试桩
    （“理由=test”的假成交、非交易时段的假熔断）——那是在给症状打补丁。
    现已从源头治好（QMT_DB / QMT_NOTICES_LOG 测试隔离），本函数仅作为
    历史脏数据的兼容层保留。
    """
    try:
        dt = datetime.fromisoformat(ts)
    except Exception:
        return False
    try:
        from core.market_calendar import DEFAULT_SESSIONS
        t = dt.time()
        first_open = datetime.strptime(DEFAULT_SESSIONS[0][0], "%H:%M").time()
        last_close = datetime.strptime(DEFAULT_SESSIONS[-1][1], "%H:%M").time()
        return first_open <= t <= last_close
    except Exception:
        return 9 <= dt.hour < 15


def _analyze_risk(snaps: list, notices: list) -> dict:
    halted_events = []
    reasons_counter = defaultdict(int)
    prev_halted = False
    consec_max = 0
    last_daily_pnl = None
    excluded = 0
    # 源1：risk_snapshots
    for s in snaps:
        try:
            p = json.loads(s["payload_json"])
        except Exception:
            continue
        halted = bool(p.get("halted"))
        if not _in_trading_window(s["ts"]):   # 测试桩/收盘后 → 不计入复盘
            if halted:
                excluded += 1
            continue
        reason = p.get("halt_reason", "") or "unknown"
        if halted and not prev_halted:
            halted_events.append({"ts": s["ts"], "reason": reason, "src": "snapshot"})
            reasons_counter[reason] += 1
        elif halted:
            reasons_counter[reason] += 1
        prev_halted = halted
        consec_max = max(consec_max, int(p.get("consecutive_losses", 0) or 0))
        ddp = p.get("daily_pnl")
        if ddp is not None:
            last_daily_pnl = float(ddp)
    # 源2：notices（tag=风控），避免快照未捕获的熔断被漏记
    for n in notices:
        if n.get("tag") != "风控":
            continue
        msg = n.get("msg", "")
        if "触发熔断" not in msg:
            continue
        if not _in_trading_window(n.get("ts", "")):   # 测试桩/收盘后 → 不计入复盘
            excluded += 1
            continue
        m = _HALT_PAT.search(msg)
        reason = (m.group(1).strip() if m else "unknown")
        halted_events.append({"ts": n.get("ts", ""), "reason": reason, "src": "notice"})
        reasons_counter[reason] += 1
    halted_events.sort(key=lambda x: x["ts"])
    return {
        "halt_events": halted_events[:50],
        "halt_count": len(halted_events),
        "halt_reasons": dict(reasons_counter),
        "excluded_halt_count": excluded,
        "max_consecutive_losses": consec_max,
        "last_daily_pnl": last_daily_pnl,
    }


# ----------------------------------------------------------------------------
# 信号 / AI 明细
# ----------------------------------------------------------------------------

def _buy_signals(signals: list) -> list:
    out = []
    for s in signals:
        if (s.get("side") or "").upper() != "BUY":
            continue
        out.append({
            "ts": s.get("ts"),
            "code": s.get("code"),
            "name": s.get("name"),
            "score": s.get("score"),
            "price": s.get("price"),
            "reason": s.get("reason"),
        })
    return out


def _ai_list(ai_rows: list) -> list:
    out = []
    for a in ai_rows:
        out.append({
            "ts": a.get("ts"),
            "code": a.get("code"),
            "stance": a.get("stance"),
            "confidence": a.get("confidence"),
            "summary": a.get("summary"),
        })
    return out


# ----------------------------------------------------------------------------
# 基准对照
# ----------------------------------------------------------------------------

def _load_baseline() -> dict:
    if BASELINE.exists():
        try:
            return json.loads(BASELINE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _compare_to_baseline(target: str, fills: list, risk: dict, baseline: dict) -> dict:
    flags = []
    if risk["halt_count"] > 0:
        reasons = [h["reason"] for h in risk["halt_events"]]
        flags.append({
            "level": "warn",
            "item": "风控熔断",
            "detail": f"当日触发 {risk['halt_count']} 次熔断（双源统计）：{reasons}。"
                      f"已修复为可冷却恢复断路器，复盘时确认次日已 resume 即可。",
        })
    else:
        flags.append({"level": "ok", "item": "风控熔断", "detail": "当日无熔断，策略平稳运行。"})
    distinct_codes = sorted({f["code"] for f in fills})
    mp = (baseline.get("config", {}) or {}).get("max_positions", 5)
    if len(distinct_codes) > mp:
        flags.append({
            "level": "info", "item": "成交代码数",
            "detail": f"当日涉及 {len(distinct_codes)} 只代码（>max_positions={mp}），"
                      f"多为日内轮换属正常；若 EOD 净持仓 > {mp} 只才需警惕超限。",
        })
    # 基准对照：verify_live_quality.json 实际字段为
    #   full.{total_return, sharpe, max_drawdown, alpha}（ratio 值）
    #   + 顶层 folds_mean_sharpe / folds_pos_alpha（walk-forward）
    # 旧映射 full_sample/metrics + return_pct/max_dd_pct/alpha_pt 全错 → 永远 null。
    full = baseline.get("full", {}) or {}

    def _pct(v):
        return round(v * 100, 2) if isinstance(v, (int, float)) else None

    return_pct = _pct(full.get("total_return"))
    max_dd_pct = _pct(full.get("max_drawdown"))
    sharpe = round(full["sharpe"], 2) if isinstance(full.get("sharpe"), (int, float)) else None
    alpha_pt = _pct(full.get("alpha"))
    folds_sharpe = (round(baseline["folds_mean_sharpe"], 2)
                    if isinstance(baseline.get("folds_mean_sharpe"), (int, float)) else None)
    folds_pos_alpha = baseline.get("folds_pos_alpha")
    return {
        "flags": flags,
        "distinct_codes": distinct_codes,
        "baseline_summary": {
            "return_pct": return_pct,
            "sharpe": sharpe,
            "max_dd_pct": max_dd_pct,
            "alpha_pt": alpha_pt,
            "folds_sharpe": folds_sharpe,
            "folds_pos_alpha": folds_pos_alpha,
        },
    }


# ----------------------------------------------------------------------------
# 报告渲染
# ----------------------------------------------------------------------------

def _render_html(target: str, rep: dict) -> str:
    risk = rep["risk"]
    pnl = rep["pnl"]
    cmp_ = rep["compare"]
    notices = rep["notices_sample"]
    fills_n = rep["fills_n"]
    sig_n = rep["signals_n"]
    equity = rep["equity"]
    eod = rep["eod"]
    trades = rep["trades"]
    buy_sigs = rep["buy_signals"]
    ai_list = rep["ai_list"]

    def badge(level):
        color = {"ok": "#2e7d32", "warn": "#ef6c00", "info": "#1565c0",
                 "err": "#c62828"}.get(level, "#555")
        return (f'<span style="color:#fff;background:{color};padding:2px 8px;'
                f'border-radius:10px;font-size:12px">{level.upper()}</span>')

    # ① 成交与盈亏
    realized_rows = "".join(
        f"<tr><td>{E(c)}</td><td>{v:,.2f}</td></tr>"
        for c, v in sorted(pnl["realized_by_code"].items(), key=lambda x: -x[1])
    ) or '<tr><td colspan="2" style="color:#888">无已实现盈亏</td></tr>'
    eod_rows = "".join(
        f"<tr><td>{E(c)}</td><td>{q}</td><td>{pnl['realized_by_code'].get(c,0):,.2f}</td>"
        f"<td>{pnl['eod_avg'].get(c,0):,.3f}</td></tr>"
        for c, q in sorted(pnl["eod_positions"].items())
    ) or '<tr><td colspan="4" style="color:#888">无净持仓（当日已平仓）</td></tr>'

    # ② 信号明细
    sig_rows = "".join(
        f"<tr><td>{E(s['ts'])}</td><td>{E(s['code'])}</td><td>{E(s['name'])}</td>"
        f"<td>{E(s['score'])}</td><td>{E(s['price'])}</td><td>{E(s['reason'])}</td></tr>"
        for s in buy_sigs[:40]
    ) or '<tr><td colspan="6" style="color:#888">当日无 BUY 信号</td></tr>'

    # ③ 风控
    flag_rows = "".join(
        f"<tr><td>{badge(f['level'])}</td><td>{E(f['item'])}</td><td>{E(f['detail'])}</td></tr>"
        for f in cmp_["flags"]
    )
    halt_rows = "".join(
        f"<tr><td>{E(h['ts'])}</td><td>{E(h['reason'])}</td><td>{E(h.get('src',''))}</td></tr>"
        for h in risk["halt_events"]
    ) or '<tr><td colspan="3" style="color:#888">当日无熔断</td></tr>'

    # ⑥ 权益
    if equity:
        eq_kpi = (f"<div class='kpi'><div class='v'>{equity['daily_return_pct']:+.2f}%</div>"
                  f"<div class='l'>当日收益率</div></div>"
                  f"<div class='kpi'><div class='v'>{equity['intraday_max_dd_pct']:+.2f}%</div>"
                  f"<div class='l'>日内最大回撤</div></div>"
                  f"<div class='kpi'><div class='v'>{equity['end']:,.0f}</div>"
                  f"<div class='l'>EOD 总资产</div></div>"
                  f"<div class='kpi'><div class='v'>{equity['points']}</div>"
                  f"<div class='l'>快照点数</div></div>")
        eq_block = (f"<div class='kpis'>{eq_kpi}</div>"
                    f"<p style='font-size:12px;color:#888;margin:8px 0'>数据源：{rep['equity_source']}</p>"
                    f"{_equity_svg(rep['equity_series'])}")
    else:
        eq_block = '<p style="color:#888">无权益曲线数据（心跳与 equity_snapshots 均无总资产记录）。</p>'

    # ⑦ 逐笔交易
    def _mode_badge(m):
        if (m or "").upper() == "LIVE":
            return '<span style="color:#fff;background:#c62828;padding:1px 7px;border-radius:8px;font-size:11px">LIVE</span>'
        return '<span style="color:#fff;background:#1565c0;padding:1px 7px;border-radius:8px;font-size:11px">PAPER</span>'
    trade_rows = "".join(
        f"<tr><td>{E(t['code'])}</td><td>{E(t['entry_ts'])}</td><td>{E(t['exit_ts'])}</td>"
        f"<td>{t['entry_price']}</td><td>{t['exit_price']}</td><td>{t['qty']}</td>"
        f"<td>{t['holding_days']}</td>"
        f"<td>{_mode_badge(t.get('entry_mode'))}/{_mode_badge(t.get('exit_mode'))}</td>"
        f"<td class='{('neg' if t['pnl']>=0 else 'pos')}'>{t['pnl']:,.2f}</td>"
        f"<td class='{('neg' if t['return_pct']>=0 else 'pos')}'>{t['return_pct']:+.2f}%</td></tr>"
        for t in trades
    ) or '<tr><td colspan="10" style="color:#888">当日无平仓（无 round-trip 交易）</td></tr>'

    # 执行模式记录卡（paper→live 切换核实）
    def _mode_summary(mc: dict) -> str:
        if not mc:
            return '<span style="color:#888">无记录</span>'
        return " ｜ ".join(
            f"{_mode_badge(m)} ×{cnt}" for m, cnt in sorted(mc.items())
        )
    mode_card = (
        f"<div class='card'><h2>执行模式记录（PAPER / LIVE 可区分核实）</h2>"
        f"<p style='font-size:13px;color:#6b7280'>本交易日成交记录标注的执行模式分布："
        f"{_mode_summary(rep['mode_counts'])}</p>"
        f"<p style='font-size:13px;color:#6b7280'>下单记录模式分布："
        f"{_mode_summary(rep['order_mode_counts'])}</p>"
        f"<p style='font-size:12px;color:#888'>切换至 LIVE 实盘后，所有订单/成交/权益快照均以 "
        f"mode=LIVE 标记；引擎每次重启会在系统提示(⑤)固化当前模式，二者互证即可还原"
        f"「何时从 PAPER 切到 LIVE」，便于盘后对照复盘与对账。</p></div>"
    )

    # ⑧ AI
    ai_rows_html = "".join(
        f"<tr><td>{E(a['ts'])}</td><td>{E(a['code'])}</td><td>{E(a['stance'])}</td>"
        f"<td>{E(a['confidence'])}</td><td>{E(a['summary'])}</td></tr>"
        for a in ai_list[:40]
    ) or '<tr><td colspan="5" style="color:#888">当日无 AI 分析记录（未启用或无网络）</td></tr>'

    # ⑤ 系统提示
    notice_rows = "".join(
        f"<tr><td>{E(n.get('ts',''))}</td><td>{E(n.get('tag',''))}</td>"
        f"<td>{E(n.get('level',''))}</td><td>{E(n.get('msg',''))}</td></tr>"
        for n in notices
    ) or '<tr><td colspan="4" style="color:#888">当日无系统提示记录</td></tr>'

    # 持仓源告警横幅（数据不可信时必须显眼，不能让读报表的人误信一个假数字）
    if rep.get("position_warning"):
        warn_banner = (
            f"<div class='card' style='background:#fff4e5;border-left:5px solid #ef6c00'>"
            f"<h2 style='border-left:none;padding-left:0;color:#b35300'>⚠ 数据一致性告警</h2>"
            f"<p style='font-size:13px;line-height:1.7'>{E(rep['position_warning'])}</p></div>"
        )
    else:
        warn_banner = ""

    bm = cmp_["baseline_summary"]
    if bm.get("return_pct") is not None:
        bm_str = (f"全样本 +{bm['return_pct']}% / Sharpe {bm['sharpe']} / "
                  f"MDD {bm['max_dd_pct']}% / α {bm['alpha_pt']}pt"
                  f"｜ Walk-forward Sharpe {bm.get('folds_sharpe')}（{bm.get('folds_pos_alpha')} 折正 α）")
    else:
        bm_str = "（无基准文件 verify_live_quality.json）"

    pos_cls = 'neg' if pnl['realized_total'] >= 0 else 'pos'
    eod_cls = 'neg' if eod['total_return_pct'] >= 0 else 'pos'

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>盘后复盘 {target}</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
  margin:0; background:#f5f7fa; color:#1f2937; }}
.wrap {{ max-width: 1040px; margin: 24px auto; padding: 0 16px; }}
h1 {{ font-size: 22px; margin: 0 0 4px; }}
.sub {{ color:#6b7280; font-size: 13px; margin-bottom: 18px; }}
.card {{ background:#fff; border-radius: 12px; padding: 18px 20px; margin-bottom: 16px;
  box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
.card h2 {{ font-size: 16px; margin: 0 0 12px; border-left: 4px solid #1565c0; padding-left: 10px; }}
table {{ width:100%; border-collapse: collapse; font-size: 13px; }}
th,td {{ text-align:left; padding: 7px 8px; border-bottom: 1px solid #eef0f3; }}
th {{ color:#6b7280; font-weight:600; }}
.kpis {{ display:flex; gap:12px; flex-wrap:wrap; }}
.kpi {{ flex:1; min-width:130px; background:#f8fafc; border-radius:10px; padding:12px 14px; }}
.kpi .v {{ font-size:20px; font-weight:700; }}
.kpi .l {{ font-size:12px; color:#6b7280; }}
.pos {{ color:#c62828; }} .neg {{ color:#2e7d32; }}
code {{ background:#eef2f7; padding:1px 5px; border-radius:4px; }}
</style></head><body><div class="wrap">
<h1>📊 盘后复盘报告 · {target}</h1>
<div class="sub">生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ｜ 数据源 storage/qmt.db + logs/notices.log
｜ 持仓源 {E(rep.get('position_source', '?'))} ｜ FIFO 回溯窗 {rep.get('lookback_days', '?')} 天</div>

{warn_banner}

<div class="card"><div class="kpis">
  <div class="kpi"><div class="v">{fills_n}</div><div class="l">成交笔数</div></div>
  <div class="kpi"><div class="v">{sig_n}</div><div class="l">信号笔数</div></div>
  <div class="kpi"><div class="v {pos_cls}">{pnl['realized_total']:,.2f}</div><div class="l">已实现盈亏(¥)</div></div>
  <div class="kpi"><div class="v {eod_cls}">{eod['unrealized']:,.2f}</div><div class="l">未实现盈亏(¥)</div></div>
  <div class="kpi"><div class="v">{risk['halt_count']}</div><div class="l">风控熔断次数</div></div>
  <div class="kpi"><div class="v">{risk['max_consecutive_losses']}</div><div class="l">最大连亏</div></div>
</div></div>

{mode_card}

<div class="card"><h2>① 成交与盈亏</h2>
<table><tr><th>代码</th><th>买入金额</th><th>卖出金额</th></tr>
<tr><td>合计</td><td>{pnl['buy_amount']:,.2f}</td><td>{pnl['sell_amount']:,.2f}</td></tr></table><h3 style="font-size:14px;margin:14px 0 6px">已实现盈亏（按代码）</h3>
<table><tr><th>代码</th><th>已实现盈亏(¥)</th></tr>{realized_rows}</table>
<h3 style="font-size:14px;margin:14px 0 6px">EOD 净持仓（含成本价）</h3>
<table><tr><th>代码</th><th>净持仓(股)</th><th>当日实现盈亏(¥)</th><th>成本价</th></tr>{eod_rows}</table>
<p style="font-size:13px;color:#6b7280">EOD 总资产 <b>{eod['total_asset']:,.2f}</b> ｜ 现金 <b>{eod['cash']:,.2f}</b> ｜
持仓市值 <b>{eod['market_value']:,.2f}</b> ｜ 未实现盈亏 <b>{eod['unrealized']:,.2f}</b> ｜
当日收益(总资产) <b class="{eod_cls}">{eod['total_return_pct']:+.2f}%</b></p>
</div>

<div class="card"><h2>② 信号明细（BUY）</h2>
<table><tr><th>时间</th><th>代码</th><th>名称</th><th>评分</th><th>价</th><th>理由</th></tr>{sig_rows}</table>
<p style="font-size:13px;color:#6b7280">信号笔数（BUY）{sig_n} ｜ 成交笔数 {fills_n} ｜
涉及代码 {len(cmp_['distinct_codes'])}（{E(', '.join(cmp_['distinct_codes'][:12]) or '—')}）</p>
</div>

<div class="card"><h2>③ 风控事件（risk_snapshots + notices 双源）</h2>
<p style="font-size:13px;color:#6b7280">当日真实交易时段（09:00–15:00）触发 <b>{risk['halt_count']}</b> 次（仅列前 50 条明细）。已剔除测试/收盘后时段误报 <b>{risk.get('excluded_halt_count', 0)}</b> 次。原因分布：</p>
<table><tr><th>熔断原因</th><th>次数</th></tr>
{''.join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in sorted(risk['halt_reasons'].items(), key=lambda x:-x[1])) or '<tr><td colspan="2" style="color:#888">当日无熔断</td></tr>'}
</table>
<p style="font-size:12px;color:#888">明细（前 50）：</p>
<table><tr><th>时间</th><th>熔断原因</th><th>来源</th></tr>{halt_rows}</table>
<p style="font-size:13px;color:#6b7280">最大连亏 {risk['max_consecutive_losses']} 次 ｜ 末笔日内盈亏 {risk['last_daily_pnl']}</p>
</div>

<div class="card"><h2>⑥ 权益曲线 / 当日盈亏 / 日内最大回撤</h2>{eq_block}</div>

<div class="card"><h2>⑦ 逐笔交易明细（FIFO 配对，建仓腿回溯至全史）</h2>
<p style="font-size:12px;color:#888">配对基于目标日及之前的全部成交（共 {rep.get('fills_history_n', '?')} 笔），
因此跘日持仓的 round-trip 与真实持仓天数均可正确重建（旧版只读当日成交，跘日交易会被整笔丢弃、holding_days 恒为 0）。</p>
<table><tr><th>代码</th><th>建仓</th><th>平仓</th><th>建仓价</th><th>平仓价</th><th>数量</th><th>持仓(天)</th><th>模式</th><th>盈亏(¥)</th><th>收益率</th></tr>{trade_rows}</table>
</div>

<div class="card"><h2>④ 与回测基准对照</h2>
<p style="font-size:13px;color:#6b7280">回测验证基准（非当日收益，仅供策略一致性参照）：{bm_str}</p>
<table><tr><th>状态</th><th>项目</th><th>说明</th></tr>{flag_rows}</table>
</div>

<div class="card"><h2>⑧ AI 分析（若启用）</h2>
<table><tr><th>时间</th><th>代码</th><th>立场</th><th>置信度</th><th>结论</th></tr>{ai_rows_html}</table>
</div>

<div class="card"><h2>⑤ 系统提示流水（{len(notices)} 条）</h2>
<table><tr><th>时间</th><th>分类</th><th>级别</th><th>内容</th></tr>{notice_rows}</table>
</div>

<div class="card" style="background:#eef6ff"><h2>📌 复盘结论与优化线索</h2>
<p style="font-size:13px;line-height:1.7">
• 本工具从结构化记录重建当日表现，覆盖成交/盈亏/权益曲线/逐笔交易/信号/风控/AI/系统提示。<br>
• 权益曲线优先读 equity_snapshots（引擎每 ~60s 落盘）；缺失时回退解析心跳，二者皆无则标注缺数据。<br>
• 若当日触发熔断：确认其在冷却窗口（连亏/日亏 1 日、回撤 5 日）后已自动恢复，否则手动 <code>RiskManager.resume()</code>。<br>
• 实盘收益对照回测基准：若长期 α 显著为负，回到策略层排查（参数高原已证无调参空间，优先查数据源/执行滑点）。<br>
• 优化迭代：将本日 JSON（logs/review_{target}.json）与历史日对比，定位回归。<br>
• 执行模式核实：本日成交/订单/权益快照已标注 PAPER 或 LIVE（见「执行模式记录」卡）。从模拟盘切到实盘后，记录与系统提示(⑤)中「模式」通知互证，可干净区分两段收益、方便对账与复盘。
</p></div>
</div></body></html>"""
    return html


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="盘后复盘工具")
    ap.add_argument("--date", help="交易日 YYYY-MM-DD（默认取最近有成交日）")
    ap.add_argument("--out-dir", default=str(BASE_DIR / "reports"), help="HTML 输出目录")
    ap.add_argument("--lookback-days", type=int,
                    default=int(STRATEGY_PARAMS.get("trend_max_hold_days", 120)),
                    help="FIFO 配对/持仓重放的回溯天数（默认=策略最长持仓周期）")
    args = ap.parse_args()

    c = _conn()
    target = args.date or _latest_trade_date(c)
    print(f"[复盘] 目标交易日: {target}")

    fills = _rows(c, "fills", target)
    # 回溯窗内的成交：FIFO 配对与持仓成本重放必须从建仓腿起算，否则跘日
    # round-trip（本策略 trend_max_hold_days=120，几乎全部属此）会被整笔漏掉。
    fills_all = _rows_since(c, "fills", target, args.lookback_days)
    sig_n = _count(c, "signals", target)
    signals = _rows(c, "signals", target)
    orders = _rows(c, "orders", target)
    snaps = _rows(c, "risk_snapshots", target)
    ai_rows = _rows(c, "ai_analyses", target)
    notices = notices_on_date(target)

    eq_series, eq_source = _load_equity_series(c, target, notices)
    state_pos = _state_positions(c)
    snap_pos_n = _last_snapshot_positions_count(c, target)
    c.close()

    pnl = _replay_fills(fills_all, target=target)
    trades = _match_trades(fills_all, target=target)

    # ---- 持仓源交叉校验【关键】----
    # fills 重放得出的持仓可能不可靠（旧会话弃仓 / 测试桩 / 回溯窗截掉了建仓腿）。
    # 优先用 engine_state（引擎落盘的权威账本）；否则用重放结果，但与当日权益
    # 快照的 positions_count 对账，**不一致就显式告警**——而不是默默输出一个
    # 像「未实现盈亏 -794 万」那样无意义的数字。
    pos_source = "fills重放"
    pos_warn = None
    if state_pos:
        pnl["eod_positions"] = {k: v["qty"] for k, v in state_pos.items()}
        pnl["eod_avg"] = {k: v["avg"] for k, v in state_pos.items()}
        pnl["cost_basis"] = round(
            sum(v["qty"] * v["avg"] for v in state_pos.values()), 2)
        pos_source = "engine_state(权威账本)"
    replayed_n = len(pnl["eod_positions"])
    if snap_pos_n is not None and replayed_n != snap_pos_n:
        pos_warn = (
            f"持仓只数对不上：{pos_source} 算出 {replayed_n} 只，而当日末权益快照记录"
            f"为 {snap_pos_n} 只。本报表的「EOD 持仓 / 成本价 / 未实现盈亏」不可信。"
            f"常见原因：① 数据库含多个彼此独立的旧 paper 会话（重启即弃仓，"
            f"只有 BUY 没有对应 SELL）；② 历史测试桩写过生产库；③ 回溯窗"
            f"({args.lookback_days} 天) 截掉了建仓腿。"
            f"建议：用 python main.py --prune-db N 清理旧数据后重跑，"
            f"并确认 engine_state 已在落盘。"
        )
        print(f"  [warn] {pos_warn}")
    risk = _analyze_risk(snaps, notices)
    baseline = _load_baseline()
    cmp_ = _compare_to_baseline(target, fills, risk, baseline)
    equity = _equity_stats(eq_series)

    # EOD 汇总：优先用权益序列末点，其次心跳
    if eq_series:
        eod_total = eq_series[-1][1]
        eod_cash = eq_series[-1][2]
    else:
        eod_total = 0.0
        eod_cash = 0.0
    market_value = round(eod_total - eod_cash, 2)
    unrealized = round(market_value - pnl["cost_basis"], 2)
    total_return_pct = equity["daily_return_pct"] if equity else 0.0

    eod = {
        "total_asset": round(eod_total, 2),
        "cash": round(eod_cash, 2),
        "market_value": market_value,
        "unrealized": unrealized,
        "total_return_pct": round(total_return_pct, 2),
    }

    buy_sigs = _buy_signals(signals)
    ai_list = _ai_list(ai_rows)

    # paper→live 切换核实：统计当日成交/订单标注的执行模式分布
    mode_counts = defaultdict(int)
    for f in fills:
        mode_counts[(f.get("mode") or "unknown").upper()] += 1
    order_mode_counts = defaultdict(int)
    for o in orders:
        order_mode_counts[(o.get("mode") or "unknown").upper()] += 1

    rep = {
        "target_date": target,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "fills_n": len(fills),
        "fills_history_n": len(fills_all),
        "lookback_days": args.lookback_days,
        "position_source": pos_source,
        "position_warning": pos_warn,
        "signals_n": sig_n,
        "orders_n": len(orders),
        "risk_snapshots_n": len(snaps),
        "notices_n": len(notices),
        "mode_counts": dict(mode_counts),
        "order_mode_counts": dict(order_mode_counts),
        "pnl": pnl,
        "trades": trades,
        "risk": risk,
        "equity": equity,
        "equity_source": eq_source,
        "equity_series": [{"ts": s[0], "total_asset": s[1], "cash": s[2]} for s in eq_series],
        "eod": eod,
        "compare": cmp_,
        "buy_signals": buy_sigs,
        "ai_list": ai_list,
        "notices_sample": notices[:50],
    }

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = BASE_DIR / "logs" / f"review_{target}.json"
    html_path = out_dir / f"review_{target}.html"
    json_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    html_path.write_text(_render_html(target, rep), encoding="utf-8")
    print(f"[复盘] JSON -> {json_path}")
    print(f"[复盘] HTML -> {html_path}")
    print(f"[复盘] 成交 {len(fills)} 笔 / 信号 {sig_n} 笔 / 已实现 ¥{pnl['realized_total']:,.2f} "
          f"/ 未实现 ¥{unrealized:,.2f} / 当日收益 {total_return_pct:+.2f}% / 熔断 {risk['halt_count']} 次")
    return rep


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""自进化闭环的「度量（OBSERVE → 记账）」环节。

把每日盘后复盘产出的**真实**账户表现，追加写入自进化账本，作为后续
EVOLVE（进化）环节计算「与优秀收益目标的差距」的唯一事实来源。

产出（全部仅追加，绝不改写历史）：
  reports/EVOLUTION_LEDGER.md   人读表格（复盘/审计用）
  logs/evolution_ledger.jsonl   机器读单行 JSON（EVOLVE 自动化消费）

设计要点：
  * 采用**跨日口径**的真实收益（prev_close → 当日 close，含隔夜重估），
    而非当日日内涨跌——后者会漏掉隔夜跳空，导致账户真的在亏钱时复盘
    却显示「健康」（2026-09-16 事故：日内 -0.25%，跨日真实 -16.4%）。
  * 任一字段缺失降级为 None，绝不因单点异常中断记账。

用法：
    python strategy/record_ledger.py                 # 自动取最新 review
    python strategy/record_ledger.py --date 2026-09-16
    python strategy/record_ledger.py --recent 10     # 只看最近 N 日摘要
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import date as date_cls
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
REPORT_DIR = ROOT / "reports"
LEDGER_MD = REPORT_DIR / "EVOLUTION_LEDGER.md"
LEDGER_JSONL = LOG_DIR / "evolution_ledger.jsonl"
# 【2026-09-30 PM-EVOLVE 第 15 轮】账本不再只依赖 OBSERVE 的 review JSON。
# 见下方 build_row_from_db：DB 是「账户真相」的原始出处，review JSON 只是它的
# 加工品；加工链断掉时不应让度量一起断。测试可 monkeypatch 本变量指向临时库。
DB_PATH = ROOT / "storage" / "qmt.db"
# 区间累计收益的锚点：2026-09-21 账户复位为 1,000,000（与 review_daily 口径一致）。
# 可用环境变量覆盖，便于测试。
RANGE_BASE = float(os.environ.get("QMT_RANGE_BASE", "1000000"))

_MD_HEADER = """# EVOLUTION_LEDGER — 自进化账本（真实收益反馈）

> 本文件由 `strategy/record_ledger.py` **仅追加**写入，历史一律不改写。
> 它是自进化闭环的「度量」层：EVOLVE 环节据此计算与优秀收益目标的差距，
> 达标前持续迭代。机读版见 `logs/evolution_ledger.jsonl`。

## 考核目标（达标即停止激进迭代）
| 指标 | 目标值 |
|---|---|
| walk-forward 样本外均值 Sharpe | ≥ 1.6（原 2.0，2026-09-19 OWNER 修订） |
| walk-forward 正收益折数 | ≥ 6/7 |
| walk-forward 最大回撤 | ≥ -22% |
| live paper 近 4 周滚动收益 | > 0% |
| 相对等权买入持有全样本 alpha | ≥ -30pt（新增，2026-09-19） |

## 逐日实绩
| 交易日 | 跨日真实收益% | 当日盈亏(元) | 期末权益 | 累计区间% | 稳定 | 安全 | 准确 | 高效 | 综合 | 熔断 | 最大连亏 | 成交数 | 收盘持仓 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
"""


def _latest_review_date() -> str | None:
    """取最新的 review JSON 对应日期。"""
    files = sorted(LOG_DIR.glob("review_*.json"))
    if not files:
        return None
    stem = files[-1].stem                      # review_2026-09-16
    return stem.split("_", 1)[1] if "_" in stem else None


def load_review(target: str) -> dict:
    p = LOG_DIR / f"review_{target}.json"
    if not p.exists():
        raise FileNotFoundError(f"复盘文件不存在: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def build_row(rep: dict) -> dict:
    """从复盘 JSON 抽取「账户真实表现」行。缺字段降级 None。"""
    acct = rep.get("account_daily") or {}
    rng = rep.get("account_range") or {}
    opt = rep.get("optimization") or {}
    scores = opt.get("scores") or {}
    risk = rep.get("risk") or {}
    pnl = rep.get("pnl") or {}
    eod_pos = pnl.get("eod_positions") or {}

    def _num(v):
        try:
            return round(float(v), 2)
        except (TypeError, ValueError):
            return None

    return {
        "date": rep.get("date") or _latest_review_date(),
        # 跨日真实口径（含隔夜重估）——账户表现的唯一基准
        "prev_asset": _num(acct.get("prev_asset")),
        "eod_asset": _num(acct.get("eod_asset")),
        "daily_ret_pct": _num(acct.get("daily_ret_pct")),
        "daily_pnl": _num(acct.get("daily_pnl")),
        "range_pct": _num(rng.get("range_pct")),
        # 健康评分
        "stable": scores.get("稳定"),
        "safety": scores.get("安全"),
        "accuracy": scores.get("准确"),
        "efficiency": scores.get("高效"),
        "overall": opt.get("overall"),
        # 风险/执行
        "halt_count": risk.get("halt_count"),
        "max_consec_loss": risk.get("max_consecutive_losses"),
        "fills_n": pnl.get("fills_n"),
        "eod_positions_n": len(eod_pos) if isinstance(eod_pos, dict) else None,
    }


# ---------------------------------------------------------------------------
# 【2026-09-30 PM-EVOLVE 第 15 轮】★ DB 直读回退（账本断供修复）
#
# 缺陷（P0，量测层）：账本的唯一输入是 ``logs/review_<date>.json``，而它由
# 15:35 OBSERVE 自动化产出 ⇒ 存在**三级单点故障**：
#     OBSERVE 未运行 / 中途异常 / 无成交日取错目标日  →  无 review JSON
#     →  record_ledger 直接 return 1  →  账本静默断供，无告警
# 实盘铁证：``logs/evolution_ledger.jsonl`` 只到 2026-09-25，而
#   ``equity_snapshots`` 明明有 09-28 / 09-29 / 09-30 三日完整快照 ⇒
#   KPI #4「paper 近 4 周滚动收益」连续 4 个交易日**无法计算**，
#   而没人收到任何报错（09-28 / 09-29 / 09-30 三轮自进化都只能在报告里
#   手写「账本断供」）。这是与 09-24 日线滞后 / 09-26 快照冻结同型的
#   「量测通道本身不可信」缺陷。
#
# 修复原则：DB 的 ``equity_snapshots`` 才是账户真相的**原始出处**，
#   review JSON 只是它的加工品。加工链断掉时，退回原始出处而不是放弃度量。
# 可逆性：新增函数均为纯读取；main() 只在「review 缺失」时才走回退路径，
#   有 review 时行为与改动前**逐位相同**。
# ---------------------------------------------------------------------------


def _conn(path=None):
    con = sqlite3.connect(str(path or DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def snapshot_days(path=None) -> list:
    """equity_snapshots 中出现过的全部日期（升序）。"""
    if not Path(path or DB_PATH).exists():
        return []
    with _conn(path) as con:
        rows = con.execute(
            "SELECT DISTINCT substr(ts,1,10) AS d FROM equity_snapshots "
            "WHERE ts IS NOT NULL ORDER BY d").fetchall()
    return [r["d"] for r in rows]


def eod_asset(day: str, path=None) -> float | None:
    """某日**最后一条**权益快照的总资产 = 当日 EOD 权益（跨日口径的锚）。

    【2026-10-07 PM-EVOLVE 第 21 轮】加入**陈旧盯市污染校正**：
    若当日被 :func:`stale_mark_suspect_day` 判为污染（零成交 + 权益零波动 +
    却相对前一日跳变 > 0.5%），则该日的「EOD」视为**不可信**，
    回溯到最近一个可信交易日的 EOD（最多回退 10 天）。
    语义：没有成交、盯市价也没动的日子，**不应产生任何盈亏** ⇒ 记为 0%。
    """
    if not Path(path or DB_PATH).exists():
        return None
    raw = eod_asset_raw(day, path)
    if raw is None:
        return None
    if not stale_mark_suspect_day(day, path):
        return raw
    d = day
    for _ in range(10):
        d = prev_snapshot_day(d, path)
        if not d:
            return raw
        if not stale_mark_suspect_day(d, path):
            v = eod_asset_raw(d, path)
            return v if v is not None else raw
    return raw


def day_stats(day: str, path=None) -> tuple:
    """当日快照的 (条数, 最小权益, 最大权益, 成交笔数)。

    用于「陈旧盯市污染」检测（见 :func:`stale_mark_suspect_day`）。
    """
    if not Path(path or DB_PATH).exists():
        return (0, None, None, 0)
    with _conn(path) as con:
        r = con.execute(
            "SELECT COUNT(*) c, MIN(total_asset) lo, MAX(total_asset) hi "
            "FROM equity_snapshots WHERE substr(ts,1,10)=? "
            "AND total_asset IS NOT NULL", (day,)
        ).fetchone()
        f = con.execute(
            "SELECT COUNT(*) c FROM fills WHERE substr(ts,1,10)=?", (day,)
        ).fetchone()
    if r is None or not r["c"]:
        return (0, None, None, int(f["c"]) if f else 0)
    return (int(r["c"]), float(r["lo"]), float(r["hi"]),
            int(f["c"]) if f else 0)


def stale_mark_suspect_day(day: str, path=None, flat_tol: float = 1e-9,
                           move_tol: float = 0.005) -> bool:
    """当日权益是否被「陈旧盯市」污染（True = 不可用作 EOD 锚）。

    【2026-10-07 PM-EVOLVE 第 21 轮】同伴缺陷：AM 时段才修完「陈旧报价
    抬高盯市」的根因（`engine/event_engine.py` 的
    ``MARK_STALE_FREEZE_VALUATION``），但**已被污染的历史快照仍然留在库里**：
    2026-10-07（国庆休市、休市日）全天快照恒为 1,020,446.6，而 09-30 真实
    EOD 是 975,273.6 ⇒ 虚增 +45,173（+4.63%）。若不拦截，10-08 的
    ``prev_asset`` 会取到虚高值，账本会记一笔 **−4.4% 的假亏损**
    （与 R14 合成行情假成交同型），KPI #4 符号再次失真。

    判据（全部来自 DB 自身，不依赖任何外部行情源 ⇒ 稳定可测）：
      ① 当日**零成交**（组合未变动）；
      ② 当日快照权益**零波动**（max−min ≤ tol）——真实交易日里 5 只持仓
         的市值不可能逐分钟完全相同 ⇒ 说明盯市价根本没在动（陈旧/休市）；
      ③ 当日 EOD 相对**前一个可信日**偏离 > move_tol(0.5%)
         ——零成交 + 零波动却「赚了钱」，只能是脏价格。
    三者同时成立 ⇒ 判定为陈旧盯市污染。

    判据的可证伪性：真实交易日即使全天涨停/跌停也会留下**成交**（①不成立），
    且单日组合波动超过 0.5% 的极端日子几乎必然伴随成交或前一个交易日的
    真实价格位移 ⇒ 不会误伤。极端情况下的代价是「当日记为 0% 并留痕」，
    而非静默采用可疑数字——这正是本守卫的目的。
    """
    cnt, lo, hi, fills = day_stats(day, path)
    if cnt < 2 or fills:
        return False
    if lo is None or hi is None or (hi - lo) > flat_tol:
        return False
    cur = eod_asset_raw(day, path)
    anchor = _credible_anchor(day, path, cur)
    if cur is None or anchor is None or anchor <= 0:
        return False
    return abs(cur / anchor - 1.0) > move_tol


def _credible_anchor(day: str, path, cur: float | None) -> float | None:
    """向前找到**取值不同**的最近一天的 EOD，作为对比锚。

    为什么不能直接比「前一天」：污染可能**连续多日**（整个长假），此时每天都
    与前一天逐位相同 ⇒ 互相掩护、谁都不显得异常（实测 10-06 / 10-07 双污染日
    第一天会被抓到、第二天 100% 漏网）。这里跳过所有「与当前值相同」的日子，
    一路回溯到真正发生位移的那天再比。
    """
    if cur is None:
        return None
    d = prev_snapshot_day(day, path)
    for _ in range(10):
        if not d:
            return None
        v = eod_asset_raw(d, path)
        if v is not None and abs(v / cur - 1.0) > 1e-9:
            return v
        d = prev_snapshot_day(d, path)
    return None


def eod_asset_raw(day: str, path=None) -> float | None:
    """某日最后一条快照的权益（**不**做污染校正，仅供内部/诊断使用）。"""
    if not Path(path or DB_PATH).exists():
        return None
    with _conn(path) as con:
        r = con.execute(
            "SELECT total_asset FROM equity_snapshots "
            "WHERE substr(ts,1,10)=? ORDER BY ts DESC, id DESC LIMIT 1", (day,)
        ).fetchone()
    return None if r is None or r["total_asset"] is None else float(r["total_asset"])


def prev_snapshot_day(day: str, path=None) -> str | None:
    """该日之前最近一个有快照的交易日（跨日口径的 prev_asset 来源）。"""
    days = [d for d in snapshot_days(path) if d < day]
    return days[-1] if days else None


def day_fills_n(day: str, path=None) -> int | None:
    if not Path(path or DB_PATH).exists():
        return None
    with _conn(path) as con:
        r = con.execute(
            "SELECT COUNT(*) c FROM fills WHERE substr(ts,1,10)=?", (day,)
        ).fetchone()
    return int(r["c"]) if r else None


def day_eod_positions_n(day: str, path=None) -> int | None:
    if not Path(path or DB_PATH).exists():
        return None
    with _conn(path) as con:
        r = con.execute(
            "SELECT positions_count FROM equity_snapshots "
            "WHERE substr(ts,1,10)=? ORDER BY ts DESC, id DESC LIMIT 1", (day,)
        ).fetchone()
    return None if r is None or r["positions_count"] is None else int(r["positions_count"])


def build_row_from_db(target: str, path=None, note: str = "") -> dict | None:
    """不依赖 review JSON，直接从 DB 构造账本行。

    只填 DB 能确证的核心字段（跨日真实收益 / 盈亏 / EOD 权益 / 区间），
    需要复盘流水线的健康评分与熔断明细降级为 None（遵循「缺字段降级」设计）。
    """
    eod = eod_asset(target, path)
    if eod is None:
        return None
    # 陈旧盯市污染留痕：原文,dict 里必须能看出「这一天的原始快照被否决了」，
    # 否则审计时无法区分「真的没波动」与「脏数据被中和」。
    raw_eod = eod_asset_raw(target, path)
    contaminated = (raw_eod is not None and abs(raw_eod - eod) > 1e-6)
    prev_day = prev_snapshot_day(target, path)
    prev = eod_asset(prev_day, path) if prev_day else None

    def _n(v):
        try:
            return round(float(v), 2)
        except (TypeError, ValueError):
            return None

    ret = pnl = None
    if prev:
        pnl = eod - prev
        ret = (eod / prev - 1) * 100.0 if prev else None
    rng = (eod / RANGE_BASE - 1) * 100.0 if RANGE_BASE else None
    row = {
        "date": target,
        "prev_asset": _n(prev),
        "eod_asset": _n(eod),
        "daily_ret_pct": _n(ret),
        "daily_pnl": _n(pnl),
        "range_pct": _n(rng),
        # 复盘流水线专属字段：DB 无法确证，一律降级 None（绝不猜测）
        "stable": None, "safety": None, "accuracy": None,
        "efficiency": None, "overall": None,
        "halt_count": None, "max_consec_loss": None,
        "fills_n": day_fills_n(target, path),
        "eod_positions_n": day_eod_positions_n(target, path),
        # ★ 溯源标记：EVOLVE 消费时能一眼看出该行是 DB 直读而非复盘加工品，
        #   避免把「降级行」当成「完整行」使用。
        "source": "db_fallback",
        "note": (note or ("" if not contaminated else ""))
                + (f"陈旧盯市污染已中和：原始快照 {raw_eod:,.2f} 被判为脏值，"
                   f"改用最近可信交易日 EOD {eod:,.2f}"
                   if contaminated else ""),
    }
    return row


def missing_days(path=None, ledger_rows=None, limit: int = 40,
                 since: str | None = None) -> list:
    """有快照但账本里没有的交易日 = 断供日（治理对象）。

    ``since``：只返回 **该日之后** 的断供日。补记时务必要用——账本首行之前
    的历史可能跨越「账户复位」边界（本项目 2026-09-21 复位为 100 万），
    跨边界算出来的「跨日收益」是复位金额差，不是策略盈亏。
    """
    rows = ledger_rows if ledger_rows is not None else read_ledger()
    have = {r.get("date") for r in rows}
    days = snapshot_days(path)
    # 休市日/陈旧盯市日不产出账本行（2026-10-07 起）：它们没有真实盈亏，
    # 补记只会制造「零成交却大涨/大跌」的假记录。
    days = [d for d in days if not stale_mark_suspect_day(d, path)]
    out = [d for d in days[-limit:] if d not in have]
    if since:
        out = [d for d in out if d > since]
    return out


def last_ledger_date(ledger_rows=None) -> str | None:
    rows = ledger_rows if ledger_rows is not None else read_ledger()
    ds = [r.get("date") for r in rows if r.get("date")]
    return max(ds) if ds else None


def _fmt(v, suffix: str = "") -> str:
    return "—" if v is None else f"{v}{suffix}"


def append_markdown(row: dict) -> bool:
    """人读表格追加；首次写入补表头。返回是否新增了行。"""
    LEDGER_MD.parent.mkdir(parents=True, exist_ok=True)
    exists = LEDGER_MD.exists()
    prev = LEDGER_MD.read_text(encoding="utf-8") if exists else ""
    if row["date"] and f"| {row['date']} " in prev:
        return False                                   # 幂等：同日不重复写
    if not exists or "## 逐日实绩" not in prev:
        LEDGER_MD.write_text(_MD_HEADER, encoding="utf-8")
    line = (
        f"| {row['date']} "
        f"| {_fmt(row['daily_ret_pct'], '%')} "
        f"| {_fmt(row['daily_pnl'])} "
        f"| {_fmt(row['eod_asset'])} "
        f"| {_fmt(row['range_pct'], '%')} "
        f"| {_fmt(row['stable'])} | {_fmt(row['safety'])} "
        f"| {_fmt(row['accuracy'])} | {_fmt(row['efficiency'])} "
        f"| {_fmt(row['overall'])} "
        f"| {_fmt(row['halt_count'])} | {_fmt(row['max_consec_loss'])} "
        f"| {_fmt(row['fills_n'])} | {_fmt(row['eod_positions_n'])} |\n"
    )
    with LEDGER_MD.open("a", encoding="utf-8") as f:
        f.write(line)
    return True


def append_jsonl(row: dict) -> bool:
    """机读 JSONL 追加；同日幂等（去重后重写该行）。"""
    LEDGER_JSONL.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    if LEDGER_JSONL.exists():
        for ln in LEDGER_JSONL.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                rows.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    replaced = False
    for i, r in enumerate(rows):
        if r.get("date") == row["date"]:
            rows[i] = row
            replaced = True
            break
    if not replaced:
        rows.append(row)
    rows.sort(key=lambda r: r.get("date") or "")
    with LEDGER_JSONL.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return not replaced


def read_ledger() -> list:
    if not LEDGER_JSONL.exists():
        return []
    out = []
    for ln in LEDGER_JSONL.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="记录当日真实收益到自进化账本")
    ap.add_argument("--date", default=None, help="交易日 YYYY-MM-DD（默认最新）")
    ap.add_argument("--recent", type=int, default=0, help="打印最近 N 日摘要")
    # ---- 第 15 轮新增：断供治理入口 ----
    ap.add_argument("--check-gap", action="store_true",
                    help="只检查断供：列出有权益快照但账本缺失的交易日，不写账本")
    ap.add_argument("--backfill", action="store_true",
                    help="把断供日全部用 DB 直读补记（默认只补到最近，见 --limit）")
    ap.add_argument("--exclude", default="",
                    help="逗号分隔、明确**不**补记的日期（数据被污染/待回滚时用）")
    ap.add_argument("--limit", type=int, default=40, help="回溯的交易日上限")
    ap.add_argument("--all", action="store_true",
                    help="补记时也覆盖账本首行之前的日期（可能跨越账户复位，慎用）")
    args = ap.parse_args()

    if args.recent:
        rows = read_ledger()[-args.recent:]
        for r in rows:
            print(
                f"{r['date']}  真实收益 {_fmt(r['daily_ret_pct'], '%')}"
                f"  盈亏 {_fmt(r['daily_pnl'])}"
                f"  累计 {_fmt(r['range_pct'], '%')}"
                f"  综合 {_fmt(r['overall'])}"
            )
        return 0

    # ---- 断供检查（只读，不写）----
    if args.check_gap:
        gap = missing_days(limit=args.limit)
        if not gap:
            print("[record_ledger] 无断供：所有有快照的交易日都已在账本中")
            return 0
        print(f"[record_ledger] ★ 账本断供 {len(gap)} 日：")
        for d in gap:
            print(f"    {d}  EOD {_fmt(eod_asset(d))}  "
                  f"（可 --backfill 补记）")
        return 2

    # ---- 断供补记 ----
    if args.backfill:
        excl = {x.strip() for x in args.exclude.split(",") if x.strip()}
        # ★ 默认只补「账本最后一日之后」的缺口：更早的日期可能跨越账户复位
        #   （本项目 2026-09-21 复位为 100 万），跨边界算出的「跨日收益」是
        #   复位金额差而非策略盈亏。--all 关闭该保护。
        since = None if args.all else last_ledger_date()
        targets = [d for d in missing_days(limit=args.limit, since=since)
                   if d not in excl]
        if since and args.all is False:
            skipped = [d for d in missing_days(limit=args.limit)
                       if d <= since and d not in excl]
            if skipped:
                print(f"[record_ledger] 跳过账本首行之前/复位边界前的 {len(skipped)} 日"
                      f"（{skipped[0]}…{skipped[-1]}），"
                      f"跨边界收益无意义；需补记请显式 --all")
        if not targets:
            print("[record_ledger] 无断供日可补记")
            return 0
        n = 0
        for d in targets:
            row = build_row_from_db(d)
            if not row:
                print(f"    {d}  跳过（无 EOD 快照）")
                continue
            append_markdown(row)
            append_jsonl(row)
            n += 1
            print(f"    {d}  补记  收益 {_fmt(row['daily_ret_pct'], '%')}  "
                  f"EOD {_fmt(row['eod_asset'])}  [source=db_fallback]")
        print(f"[record_ledger] DB 直读补记 {n} 日"
              + (f"；排除 {sorted(excl)}" if excl else ""))
        return 0

    target = args.date or _latest_review_date()
    if not target:
        # 【第 15 轮】旧行为：无 review JSON 直接放弃 → 账本静默断供。
        # 新行为：退回 DB 直读（equity_snapshots 才是账户真相的原始出处）。
        latest_days = snapshot_days()
        if not latest_days:
            print("[record_ledger] 未找到任何 review JSON，且 DB 无权益快照，跳过")
            return 1
        target = latest_days[-1]
        row = build_row_from_db(target, note="review JSON 缺失，DB 直读回退")
        if not row:
            print("[record_ledger] 未找到任何 review JSON，且 DB 回退失败，跳过")
            return 1
        append_markdown(row)
        append_jsonl(row)
        print(
            f"[record_ledger] {target}（DB 直读回退）真实收益 "
            f"{_fmt(row['daily_ret_pct'], '%')}  累计 {_fmt(row['range_pct'], '%')}"
        )
        return 0
    try:
        rep = load_review(target)
    except FileNotFoundError:
        row = build_row_from_db(target, note=f"review_{target}.json 缺失，DB 直读回退")
        if not row:
            print(f"[record_ledger] {target} 无 review JSON 且 DB 无快照，跳过")
            return 1
        append_markdown(row)
        append_jsonl(row)
        print(
            f"[record_ledger] {target}（DB 直读回退）真实收益 "
            f"{_fmt(row['daily_ret_pct'], '%')}  累计 {_fmt(row['range_pct'], '%')}"
        )
        return 0
    row = build_row(rep)
    row.setdefault("source", "review")
    added_md = append_markdown(row)
    added_json = append_jsonl(row)
    print(
        f"[record_ledger] {target} 真实收益 {_fmt(row['daily_ret_pct'], '%')}"
        f"  累计 {_fmt(row['range_pct'], '%')}"
        f"  综合 {_fmt(row['overall'])}"
        f"  (md={'新增' if added_md else '已存在'}, jsonl={'新增' if added_json else '更新'})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

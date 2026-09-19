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
import sqlite3
from datetime import date as date_cls
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
REPORT_DIR = ROOT / "reports"
LEDGER_MD = REPORT_DIR / "EVOLUTION_LEDGER.md"
LEDGER_JSONL = LOG_DIR / "evolution_ledger.jsonl"

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

    target = args.date or _latest_review_date()
    if not target:
        print("[record_ledger] 未找到任何 review JSON，跳过")
        return 1
    rep = load_review(target)
    row = build_row(rep)
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

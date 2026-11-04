# -*- coding: utf-8 -*-
"""一次性数据修正：账本 2026-10-30 行的 EOD 用盘中快照冒充收盘价。

★ 缺陷（2026-11-01 复核发现）
    账本 ``eod_asset`` 取自「当日最后一条权益快照」（``eod_asset_raw``）。
    2026-10-30 引擎在 11:29:43 之后进入休眠（13:00–15:00 无任何快照，见
    ``docs/AUTOMATION_CONTRACT.md`` §4.3 的整机休眠判据），因此最后一条快照是
    **11:29 的盘中价**，不是收盘价：
        11:29 快照 total_asset = 65,748.42（300308 按 573.64 盯市）
        收盘真实总资产     = 63,684.42（300308 收盘 553.00，100 股 ⇒ 差 2,064.00）
    ⇒ 账本把当日记成 −1.19%，真实是 −4.29%，**方向不错但幅度低估 3.1pt**。

★ 双源交叉验证（为什么可以确定 63,684.42 是收盘值）
    ① 按 2026-10-30 收盘价重算：300308 100×553.00 + 510050 500×2.862 + 现金 6,953.42
       = 55,300.00 + 1,431.00 + 6,953.42 = 63,684.42
    ② 2026-11-01 引擎重启时从券商读取的 total_asset = 63,684.34（差 0.08 = 当日费用计提）
    两源一致 ⇒ 修正值取 ①（价格重算，不依赖交易日历）。

★ 安全性
    - 只改 ``date == "2026-10-30"`` 这一行，**其它行逐字节保持原样**（按行处理，
      不整体重新序列化，避免格式漂移污染 paper 时代历史行）。
    - 改动前自动备份到 ``logs/evolution_ledger.jsonl.bak-<today>-preclean``（已存在则跳过）。
    - ``--dry-run`` 只打印差异不落盘。
    - 修正后行内 ``note`` 写明修正原因与双源证据，``fix`` 字段留机器可读标记。
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "logs" / "evolution_ledger.jsonl"

TARGET_DAY = "2026-10-30"
# 按 2026-10-30 收盘价重算（见模块 docstring 双源验证）
NEW_EOD = 63684.42
NEW_PREV = 66542.31          # MODE_BREAKS 里登记的 live 基线，不跨模式链接
NOTE_APPEND = ("[2026-11-01 数据修正] EOD 原值 65,748.42 是 11:29 盘中快照"
               "（引擎 11:29 后休眠、当日无收盘快照）⇒ 按 10-30 收盘价重算为 "
               "63,684.42（300308 553.00×100 + 510050 2.862×500 + 现金 6,953.42），"
               "并与 2026-11-01 引擎券商读数 63,684.34 双源核对一致。")


def _rebuild(row: dict) -> dict:
    eod = NEW_EOD
    prev = NEW_PREV
    ret = round((eod / prev - 1) * 100.0, 2)
    row["prev_asset"] = round(prev, 2)
    row["eod_asset"] = round(eod, 2)
    row["daily_pnl"] = round(eod - prev, 2)
    row["daily_ret_pct"] = ret
    # range_pct 以断点基线为分母（与 apply_mode_boundary 同义）
    row["range_pct"] = ret
    base_note = row.get("note") or ""
    if "[2026-11-01 数据修正]" not in base_note:
        row["note"] = (base_note + " " + NOTE_APPEND).strip()
    row["fix"] = "ledger_eod_1030_close_recalc"
    # 【2026-11-04 修复】修补行原先**没有** ``eod_ts`` / ``eod_is_close`` 键（键都不存在，
    # 不是 None），而全账本唯一真出过「盘中值当收盘」事故的就是这一行。
    #
    # 编码（team-lead 2026-11-04 裁决，采纳 data-ledger-auditor-2 的异议）：
    #   eod_ts       = None   —— 诚实：当日无 ≥15:00 快照，不能编时间戳；
    #   eod_is_close = True   —— 诚实：63,684.42 是**按真实收盘价重算**的收盘口径，
    #                            可与其他交易日比较。此前一度写成 False，那是错的：
    #                            False 的定义是「盘中快照、不可跨日比较」，而本值可以比。
    #                            且已有消费端 ``scripts/_audit_*.py`` 用
    #                            ``eod_is_close is not True`` 筛异常行，写 False 会让
    #                            10-30 永久挂在告警里、「只取 True」的 KPI 把 live
    #                            序列从 3 天砍成 2 天 —— 恰好在「修好它」的名义下
    #                            删掉 −4.29% 这一天。
    #   eod_basis    = "close_recalc" —— 来源差异不污染「可比性」判断。
    #
    # 不调用 ``annotate_eod_freshness``：它会把当日最后一条快照（11:29:43 的
    # 65,748.42，正是本行修掉的那个值）写成本行 ``eod_ts``，并断言「该 eod_asset
    # 为盘中值」—— 与已修正的 63,684.42 事实相反 ⇒ 伪造溯源，比缺字段更坏。
    row["eod_ts"] = None
    row["eod_is_close"] = True
    row["eod_basis"] = "close_recalc"
    fresh = ("[EOD 口径] 无 ≥15:00 快照，故 eod_ts 留空；eod_asset 为按收盘价重算的"
             "收盘口径（553.00x100 + 2.862x500 + 现金 6,953.42 = 63,684.42，与券商 "
             "2026-11-01 读数 63,684.34 交叉核对，差 0.08）⇒ eod_is_close=True，"
             "可与其它交易日比较；来源见 eod_basis=close_recalc。")
    if fresh not in row["note"]:
        row["note"] = (row["note"] + " " + fresh).strip()
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not LEDGER.exists():
        print(f"账本不存在: {LEDGER}")
        return 1

    lines = LEDGER.read_text(encoding="utf-8").splitlines(keepends=True)
    hit = 0
    out = []
    for ln in lines:
        s = ln.strip()
        if not s:
            out.append(ln)
            continue
        try:
            row = json.loads(s)
        except Exception:
            out.append(ln)
            continue
        if row.get("date") != TARGET_DAY:
            out.append(ln)
            continue
        hit += 1
        before = {k: row.get(k) for k in
                  ("eod_asset", "prev_asset", "daily_pnl", "daily_ret_pct", "range_pct")}
        row = _rebuild(row)
        after = {k: row.get(k) for k in before}
        print(f"[{TARGET_DAY}] 修正前后：")
        for k in before:
            print(f"  {k:16s} {before[k]!s:>14s}  →  {after[k]!s:>14s}")
        tail = "\n" if ln.endswith("\n") else ""
        out.append(json.dumps(row, ensure_ascii=False) + tail)

    if hit == 0:
        print(f"未找到 {TARGET_DAY} 行，退出（不做任何改动）")
        return 2
    if hit > 1:
        print(f"警告：匹配到 {hit} 行 {TARGET_DAY}，请人工确认后再跑")
        return 3

    if args.dry_run:
        print("\n[dry-run] 未写盘")
        return 0

    bak = LEDGER.with_name(LEDGER.name + f".bak-{date.today():%Y%m%d}-preclean")
    if not bak.exists():
        shutil.copy2(LEDGER, bak)
        print(f"\n已备份 → {bak}")
    else:
        print(f"\n备份已存在，保留原备份 → {bak}")

    tmp = LEDGER.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(out), encoding="utf-8")
    tmp.replace(LEDGER)
    print(f"已写盘 → {LEDGER}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

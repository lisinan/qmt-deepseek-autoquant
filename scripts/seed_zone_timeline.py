# -*- coding: utf-8 -*-
"""把闸门卡点时间线（gate_timeline.jsonl）的历史行共享给另一个区。

## 为什么需要它

`gate_timeline.jsonl` 记录的是「固定 23 只宇宙里，某交易日 7 个条件各有几只
通过」。这是**市场数据的函数**，与账户、持仓、执行模式无关 —— 两个区用同一份
固定宇宙（`Observability._fixed_universe()`）和同一份日线算出来的行**本该相等**。

但 zone 隔离后两个区各写各的 `LOG_DIR`：

    logs/gate_timeline.jsonl        872 行（2023-04-07 → 2026-11-06）
    logs_paper/gate_timeline.jsonl  不存在

于是观察区（paper）页面上的「闸门卡点时间线」是空的 —— 不是坏了，是它今晚才
新建、且当天是周日（非交易日不产生行）。等它自己积累，前几天几乎没有参照价值。

本脚本把源区**已有**的日期行补进目标区，**只补缺失的 date，绝不覆盖**。

## 诚实性

补进去的行会加一个 `origin_zone` 字段（如 `"live"`），标明这行不是本区跑出来的。
读端（`web/routes.py::api_gate_timeline`）只取固定字段，多一个字段不影响渲染。

## 用法

    # 默认 dry-run：只打印会补多少行，不写盘
    python scripts/seed_zone_timeline.py --to paper

    # 实际写入（只补 logs_paper/ 里没有的日期）
    python scripts/seed_zone_timeline.py --to paper --apply

    # 只要最近 120 个交易日
    python scripts/seed_zone_timeline.py --to paper --days 120 --apply

## 安全边界

- 目标只能是 `paper`（观察区）。传 `--to live` 会被拒绝：往实盘区的时间线里
  塞别的区的行没有任何正当理由，且实盘行要留给回填脚本
  （`scripts/_res_gate_timeline_backfill.py` 靠 `source=="live"` 保留行）。
- 写入方向是「读 live → 写 paper」，**绝不写 live 的任何文件**。
- 幂等：重复跑不会产生重复日期。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.zone import zone_config  # noqa: E402


def _load_by_date(path: Path) -> dict:
    """读 jsonl → {date: row}；同日多行取最后一条（与读端口径一致）。"""
    out: dict = {}
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                row = json.loads(ln)
            except Exception:
                continue
            if isinstance(row, dict) and row.get("date"):
                out[str(row["date"])] = row
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="共享闸门卡点时间线历史到观察区")
    ap.add_argument("--to", default="paper", help="目标区（只允许 paper）")
    ap.add_argument("--from", dest="src_zone", default="live", help="源区（默认 live）")
    ap.add_argument("--days", type=int, default=0,
                    help="只取源区最近 N 个交易日；0 = 全部（默认）")
    ap.add_argument("--apply", action="store_true",
                    help="实际写盘；不加则为 dry-run")
    ap.add_argument("--root", default=None,
                    help="项目根目录（默认按脚本位置推断；测试用，勿在生产指定）")
    args = ap.parse_args()

    root = Path(args.root).resolve() if args.root else ROOT
    dst_zone = str(args.to).strip().lower()
    src_zone = str(args.src_zone).strip().lower()
    if dst_zone != "paper":
        print(f"[拒绝] --to 只能是 paper，收到 {dst_zone!r}。"
              f"往实盘区时间线里塞别的区的行没有正当理由。")
        return 2
    if src_zone == dst_zone:
        print(f"[拒绝] 源区与目标区相同（{src_zone}），无需共享。")
        return 2

    src_path = Path(zone_config(src_zone, root).log_dir) / "gate_timeline.jsonl"
    dst_path = Path(zone_config(dst_zone, root).log_dir) / "gate_timeline.jsonl"

    print(f"源：{src_path}")
    print(f"目标：{dst_path}")
    if not src_path.exists():
        print(f"[中止] 源文件不存在，无法共享。")
        return 1

    src = _load_by_date(src_path)
    dst = _load_by_date(dst_path)
    dates = sorted(src)
    if args.days and args.days > 0:
        dates = dates[-args.days:]

    missing = [d for d in dates if d not in dst]
    already = [d for d in dates if d in dst]
    skipped_no_universe = [
        d for d in missing if not (src[d].get("universe_n") or 0)]

    print(f"源区可用日期：{len(src)} 个（{min(src) if src else '-'} → "
          f"{max(src) if src else '-'}）")
    print(f"本次窗口：{len(dates)} 个交易日")
    print(f"目标区已有：{len(dst)} 个日期")
    print(f"  → 将补入：{len(missing)} 行")
    print(f"  → 已存在跳过：{len(already)} 行")
    if skipped_no_universe:
        print(f"  → 其中 universe_n=0 的 {len(skipped_no_universe)} 行"
              f"（仍会补，但属「当天没数据」的空行）")

    if not missing:
        print("\n目标区已覆盖这些日期，无需写入（幂等）。")
        return 0

    print(f"\n将补入的日期范围：{missing[0]} → {missing[-1]}")
    sample = src[missing[-1]]
    print("最后一行样例（会加 origin_zone 字段）：")
    preview = dict(sample)
    preview["origin_zone"] = src_zone
    print("  " + json.dumps(preview, ensure_ascii=False))

    if not args.apply:
        print("\n[dry-run] 未写盘。确认无误后加 --apply 执行。")
        return 0

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    # 【不能简单追加】目标文件可能已经有比缺失日期更晚的行（例如本区引擎昨天
    # 已经写过一行、今天才补更早的历史），append 到末尾会让文件失去日期单调性。
    # 改成「读目标 → 合并 → 按 date 排序 → 原子替换」：
    #   · 本区已有的行**优先**（不覆盖引擎自己写的）；
    #   · 缺的日期用源区的行 + origin_zone 补上；
    #   · 顺带把目标文件里的重复 date 去重（与读端口径一致，每天只该一行）。
    merged: dict = dict(dst)
    for d in missing:
        row = dict(src[d])
        row["origin_zone"] = src_zone
        merged[str(d)] = row

    tmp = dst_path.with_name(dst_path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for d in sorted(merged):
            f.write(json.dumps(merged[d], ensure_ascii=False) + "\n")
    os.replace(tmp, dst_path)

    after = _load_by_date(dst_path)
    print(f"\n[已写入] {len(missing)} 行 → {dst_path}")
    print(f"目标区现有日期数：{len(after)}"
          f"（{min(after) if after else '-'} → {max(after) if after else '-'}）")
    print("整个文件已按 date 升序重写；读端也会按 date 去重后按升序返回。")
    print("⚠ 建议在目标区引擎未运行时执行，避免与它的追加写并发。")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    raise SystemExit(main())

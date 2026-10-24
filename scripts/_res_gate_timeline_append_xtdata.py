# -*- coding: utf-8 -*-
"""把 gate_timeline 补齐到「最近交易日」（用 xtdata 本地日线）。

为什么需要它：`scripts/_res_gate_timeline_backfill.py` 用的是 **tushare 面板缓存**，
而面板 pkl 的末根通常是几天前（当前 20261013）⇒ 时间线右端会缺最近若干个交易日，
面板看起来"停在两周前"。

本脚本：
  1) 读现有 `logs/gate_timeline.jsonl`，取最后日期；
  2) 用 `strategy.backtest_daily.load_daily` 拉各标的**本地日线**（必要时由 miniQMT 就地补拉），
     按日期对齐；
  3) 对「最后日期之后」的每个交易日，用与回填脚本**完全相同**的口径
     （复用其 `compute_code_series` / `PASS_KEYS` / `BLOCKED_KEY_ORDER`）算 7 项通过数、
     trend_up、all_pass_n 与 best；
  4) 追加写入（不覆盖历史行；同日若已存在则跳过）。

用法：
    python scripts/_res_gate_timeline_append_xtdata.py            # 补到今天
    python scripts/_res_gate_timeline_append_xtdata.py --dry-run  # 只打印不写
    python scripts/_res_gate_timeline_append_xtdata.py --days 30  # 只回看最近 30 个交易日
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import STRATEGY_PARAMS                      # noqa: E402
from strategy.backtest_daily import align_panel, load_daily      # noqa: E402

# 复用回填脚本的口径实现（同文件同函数，避免两份逻辑漂移）
sys.path.insert(0, str(ROOT / "scripts"))
import _res_gate_timeline_backfill as B                          # noqa: E402

TIMELINE = ROOT / "logs" / "gate_timeline.jsonl"


def _load_existing() -> list:
    if not TIMELINE.exists():
        return []
    out = []
    for ln in TIMELINE.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def _last_date(rows: list) -> str:
    dates = [r.get("date") for r in rows if r.get("date")]
    return max(dates) if dates else ""


def _fetch_panel(codes: list, count: int, force_refresh: bool = False):
    """拉各标的本地日线并按日期对齐。返回 (dates, panel)。

    ``force_refresh``：对「末端明显落后于其它标的」的票先调 miniQMT 的
    ``download_history`` 就地补拉。

    ★ 为什么需要它（2026-10-24 实测）：miniQMT 的 1d 本地缓存只在客户端主动同步时
    更新，实测有 7 只票停在 2026-10-19，而 ``load_daily`` 的陈旧阈值是「末根距今 >5 天」
    才补拉 —— 10-19 → 10-24 恰好不到 5 天，于是**静默不补**，宇宙从 23 只掉到 16 只，
    通过率的分母被悄悄换掉。必须显式刷新。
    """
    from core.qmt_client import qmt_client

    data = {}
    for c in codes:
        d = load_daily(c, count)
        if d and d.get("date"):
            data[c] = d
    if force_refresh and data:
        newest = max(d["date"][-1] for d in data.values())
        stale = [c for c, d in data.items() if d["date"][-1] < newest]
        if stale:
            print(f"[刷新] {len(stale)} 只本地日线落后于 {newest}，尝试补拉: {stale[:6]}"
                  f"{' ...' if len(stale) > 6 else ''}")
            for c in stale:
                try:
                    qmt_client.download_history(c, period="1d")
                except Exception as exc:
                    print(f"   补拉失败 {c}: {exc}")
            for c in stale:
                d = load_daily(c, count)
                if d and d.get("date"):
                    data[c] = d
    missing = [c for c in codes if c not in data]
    if missing:
        print(f"[警告] {len(missing)} 只取不到日线（跳过）: {missing[:6]}"
              f"{' ...' if len(missing) > 6 else ''}")
    if not data:
        return [], {}
    dates, panel = align_panel(data)
    return dates, panel


def build_rows_after(dates: list, panel: dict, codes: list,
                     after: str, params: dict) -> list:
    """为 ``after`` 之后的每个交易日生成时间线行（口径 = 回填脚本）。"""
    series = {}
    for c in codes:
        if c in panel:
            series[c] = B.compute_code_series(panel[c], params)
    rows = []
    for i, d in enumerate(dates):
        ds = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else str(d)
        if ds <= after:
            continue
        uni = 0
        tally = {k: 0 for k in B.PASS_KEYS}
        trend_n = 0
        all_pass_n = 0
        best = None
        for c in codes:
            s = series.get(c)
            if s is None:
                continue
            if not panel[c].get("valid", [True] * len(dates))[i]:
                continue
            uni += 1
            for k in B.PASS_KEYS:
                if s[k][i]:
                    tally[k] += 1
            if s["trend_up"][i]:
                trend_n += 1
            cand_n = sum(1 for k in B.PASS_KEYS if s[k][i])
            if cand_n == len(B.PASS_KEYS):
                all_pass_n += 1
            cand_score = s["score"][i]
            cand_mom = s["mom60"][i]
            if best is None or (
                cand_n, cand_score, (cand_mom if cand_mom is not None else -9.9)
            ) > (
                best["passed_n"], best["score"],
                (best["mom60"] if best["mom60"] is not None else -9.9)
            ):
                bkey = ""
                for k in B.BLOCKED_KEY_ORDER:
                    if not s[k][i]:
                        bkey = k
                        break
                best = {"code": c, "passed_n": cand_n, "blocked_key": bkey,
                        "score": cand_score, "mom60": cand_mom}
        if uni == 0:
            continue
        rows.append({
            "date": ds, "universe_n": uni, "pass": tally,
            "trend_up": trend_n, "all_pass_n": all_pass_n,
            "best": best, "source": "xtdata",
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=200,
                    help="回看多少根日线（默认 200，够覆盖最近缺失段）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-refresh", action="store_true", default=True,
                    help="对落后的票先用 download_history 就地补拉（默认开）")
    args = ap.parse_args()

    from config.settings import STOCK_CODES, SECTOR_CONFIG
    codes = sorted(set(STOCK_CODES) | {
        c for v in SECTOR_CONFIG["sectors"].values() for c, _ in v["stocks"]
    })

    existing = _load_existing()
    last = _last_date(existing)
    print(f"[现状] 已有 {len(existing)} 行，末端 {last or '(空)'}")

    dates, panel = _fetch_panel(codes, args.days, force_refresh=args.force_refresh)
    if not dates:
        print("[错误] 取不到日线，退出")
        return 2
    print(f"[数据] {len(panel)} 只 × {len(dates)} 根对齐日线：{dates[0]} → {dates[-1]}")

    params = dict(STRATEGY_PARAMS)
    rows = build_rows_after(dates, panel, codes, last, params)
    if not rows:
        print("[完成] 没有需要补的交易日（时间线已是最新）")
        return 0
    print(f"[新增] {len(rows)} 个交易日：{rows[0]['date']} → {rows[-1]['date']}")
    for r in rows:
        b = r.get("best") or {}
        print(f"   {r['date']}  universe={r['universe_n']:<3} trend_up={r['trend_up']:<3} "
              f"mom_ok={r['pass']['mom_ok']:<3} all_pass={r['all_pass_n']:<3} "
              f"best={b.get('code', '-')} passed={b.get('passed_n')} "
              f"blocked={b.get('blocked_key')!r}")

    if args.dry_run:
        print("[dry-run] 未写文件")
        return 0
    with open(TIMELINE, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"[写] 已追加到 {TIMELINE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


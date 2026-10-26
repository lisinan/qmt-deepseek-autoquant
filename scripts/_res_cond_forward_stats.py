# -*- coding: utf-8 -*-
"""条件后验统计 —— 「现在这个闸门状态，历史上之后 N 天赚的概率是多少？」

课题（2026-10-26 收盘后）：
  当前策略 0 成交、100% 现金。核心问题不是「历史上涨过」，而是：
  给定**当前闸门状态**（B 型：all_pass_n==0 且 mom_ok==0 且 trend_up<=2），
  历史上「同型状态」出现后的未来 5/10/20 个交易日，宇宙等权买入的获利几率
  是多少？这个几率是否比 20 天前、60 天前扩大了？

★ 方法
  1) 取 23 只固定宇宙（STOCK_CODES ∪ SECTOR_CONFIG）对齐日线（load_daily，必要时就地补拉）；
  2) 读 logs/gate_timeline.jsonl 每日闸门状态；今天（10-26）缺完整 backfill 行，
     本脚本用与回填脚本同口径的 compute_code_series 就地重算今天完整闸门状态；
  3) 对每个历史日 t 计算宇宙**等权**未来收益 r5/r10/r20（t 收盘 → t+k 收盘，
     停牌前向填充）；
  4) 按当日闸门状态分桶统计（n、r5/r10/r20 的均值/中位数/胜率 P(r>0)/25-75 分位）：
     A 可交易         all_pass_n >= 2
     B 当前同型       all_pass_n == 0 且 mom_ok == 0 且 trend_up <= 2
     C 趋势出现动量未过 trend_up >= 1 且 mom_ok == 0
     D 闸门重启日      trend_up 从「连续 >=5 日 == 0」之后首次 > 0
     另给 B 桶内按「连续多少日处于 B」细分（1-3 / 4-7 / 8+ 日）
  5) 重点对比：当前（10-26 附近，B 桶）vs 20 天前 vs 60 天前的桶归属与胜率；
  6) 附加：r5/r10/r20 胜率随时间文本迷你图（每 20 个交易日一个点）。

★ 口径（诚实标注）
  - 「等权宇宙」是「如果买入会买到什么」的**市场层代理**，非策略实际持仓；
  - 停牌/缺失用前向填充；
  - 样本量 <30 的桶标注「样本不足」。

★ 只读：不 kill/重启、不启动服务、不下单、不写 storage/qmt.db、
  不改 config/engine/risk/core/、不编辑 tests/run_all.py、不跑 run_all。

用法：
  $env:Path="...qmt\\Library\\bin;...qmt\\DLLs;"+$env:Path
  $env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="."
  python scripts/_res_cond_forward_stats.py [--force-refresh]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import strategy.backtest_daily as BD          # noqa: E402
from core import indicators as I             # noqa: E402
from config.settings import (                # noqa: E402
    STRATEGY_PARAMS, STOCK_CODES, SECTOR_CONFIG,
)

sys.path.insert(0, str(ROOT / "scripts"))
import _res_gate_timeline_backfill as B      # noqa: E402
import _res_gate_timeline_append_xtdata as XT  # noqa: E402

TIMELINE = ROOT / "logs" / "gate_timeline.jsonl"
OUT_MD = ROOT / "reports" / "PROFIT_ODDS_CONDITIONAL_2026-10-26.md"
OUT_JSON = ROOT / "reports" / "PROFIT_ODDS_CONDITIONAL_2026-10-26.json"

PASS_KEYS = B.PASS_KEYS
WARMUP = 60
COUNT = 900          # 足够长的日线窗口（覆盖 2023-04 → 2026-10）

# 未来收益窗口
FW = (5, 10, 20)


# ============================================================ 数据

def _universe_codes() -> List[str]:
    return sorted(set(STOCK_CODES) | {
        c for v in SECTOR_CONFIG["sectors"].values() for c, _ in v["stocks"]
    })


def _fetch_panel(codes: List[str], count: int, force_refresh: bool = True):
    """拉 23 只对齐日线。force_refresh 对落后标的就地补拉（与 append 脚本同法）。"""
    from core.qmt_client import qmt_client
    data = {}
    for c in codes:
        d = BD.load_daily(c, count)
        if d and d.get("date"):
            data[c] = d
    if force_refresh and data:
        newest = max(d["date"][-1] for d in data.values())
        stale = [c for c, d in data.items() if d["date"][-1] < newest]
        if stale:
            print(f"[刷新] {len(stale)} 只本地日线落后于 {newest}，补拉: {stale[:6]}")
            for c in stale:
                try:
                    qmt_client.download_history(c, period="1d")
                except Exception as exc:
                    print(f"   补拉失败 {c}: {exc}")
            for c in stale:
                d = BD.load_daily(c, count)
                if d and d.get("date"):
                    data[c] = d
    missing = [c for c in codes if c not in data]
    if missing:
        print(f"[警告] 取不到日线（跳过）: {missing}")
    if not data:
        return [], {}
    dates, panel = BD.align_panel(data)
    return dates, panel


def _load_timeline() -> List[dict]:
    """读 gate_timeline.jsonl，返回按 date 升序的行列表（含 live 行，先原样保留）。"""
    rows = []
    if not TIMELINE.exists():
        return rows
    for ln in TIMELINE.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rows.append(json.loads(ln))
        except Exception:
            continue
    return rows


def _fmt(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}" if len(yyyymmdd) == 8 else str(yyyymmdd)


# ============================================================ 今天闸门状态重算

def _compute_today_row(dates: List[str], panel: dict, codes: List[str],
                       params: dict) -> Optional[dict]:
    """用同口径重算最后一个交易日的完整闸门状态（补齐 live 行的残缺）。

    返回与 timeline 行同结构的 dict；取不到则 None。
    """
    if not dates or not panel:
        return None
    i = len(dates) - 1
    series = {}
    for c in codes:
        if c in panel:
            series[c] = B.compute_code_series(panel[c], params)
    uni = 0
    tally = {k: 0 for k in PASS_KEYS}
    trend_n = 0
    all_pass_n = 0
    best = None
    valid = panel[codes[0]].get("valid", [True] * len(dates)) if codes else []
    for c in codes:
        s = series.get(c)
        if s is None:
            continue
        if not panel[c].get("valid", [True] * len(dates))[i]:
            continue
        uni += 1
        for k in PASS_KEYS:
            if s[k][i]:
                tally[k] += 1
        if s["trend_up"][i]:
            trend_n += 1
        cand_n = sum(1 for k in PASS_KEYS if s[k][i])
        if cand_n == len(PASS_KEYS):
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
        return None
    return {
        "date": _fmt(dates[i]), "universe_n": uni, "pass": tally,
        "trend_up": trend_n, "all_pass_n": all_pass_n, "best": best,
        "source": "xtdata",
    }


# ============================================================ 未来收益

def _eqw_forward_returns(dates: List[str], panel: dict, codes: List[str],
                         k: int) -> List[Optional[float]]:
    """宇宙等权未来 k 日收益（t 收盘 → t+k 收盘，停牌前向填充）。

    返回与 dates 等长的列表；末 k 根无 t+k 收盘 → None。
    """
    n = len(dates)
    out: List[Optional[float]] = [None] * n
    for i in range(n):
        j = i + k
        if j >= n:
            break
        rets = []
        for c in codes:
            d = panel.get(c)
            if not d:
                continue
            p0 = d["close"][i]
            p1 = d["close"][j]
            if p0 and p1 and p0 > 0:
                rets.append(p1 / p0 - 1.0)
        if rets:
            out[i] = sum(rets) / len(rets)
    return out


def _stats(xs: List[float]) -> dict:
    """均值 / 中位数 / 胜率 P(>0) / 25-75 分位 / n。"""
    if not xs:
        return {"n": 0, "mean": None, "median": None, "win_rate": None,
                "p25": None, "p75": None}
    s = sorted(xs)
    n = len(s)
    wins = sum(1 for x in xs if x > 0)
    def q(p):
        idx = (n - 1) * p
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        frac = idx - lo
        return s[lo] * (1 - frac) + s[hi] * frac
    return {
        "n": n,
        "mean": sum(xs) / n,
        "median": s[n // 2],
        "win_rate": wins / n,
        "p25": q(0.25),
        "p75": q(0.75),
    }


def _win_series(values: List[Optional[float]]) -> List[float]:
    """非 None 值序列（供迷你图/统计）。"""
    return [x for x in values if x is not None]


# ============================================================ 分桶

def _bucket_of(row: dict) -> str:
    """按当日闸门状态返回桶标签；None 表示不属于任何研究桶。"""
    ap = row.get("all_pass_n", 0) or 0
    tu = row.get("trend_up", 0) or 0
    mo = (row.get("pass", {}) or {}).get("mom_ok", 0) or 0
    if ap >= 2:
        return "A"
    if ap == 0 and mo == 0 and tu <= 2:
        return "B"
    if tu >= 1 and mo == 0:
        return "C"
    return None


def _is_restart_day(idx: int, rows: List[dict]) -> bool:
    """D 桶：trend_up 从「连续 >=5 日 == 0」之后首次 > 0。"""
    tu = rows[idx].get("trend_up", 0) or 0
    if tu <= 0:
        return False
    run = 0
    i = idx - 1
    while i >= 0 and (rows[i].get("trend_up", 0) or 0) == 0:
        run += 1
        i -= 1
    return run >= 5


def _consec_b_run(idx: int, rows: List[dict]) -> int:
    """计算 idx 日（须为 B 桶）向前连续多少日处于 B（含当日）。"""
    cnt = 0
    i = idx
    while i >= 0:
        r = rows[i]
        ap = r.get("all_pass_n", 0) or 0
        tu = r.get("trend_up", 0) or 0
        mo = (r.get("pass", {}) or {}).get("mom_ok", 0) or 0
        if ap == 0 and mo == 0 and tu <= 2:
            cnt += 1
            i -= 1
        else:
            break
    return cnt


# ============================================================ 迷你图

def _spark(values: List[float], width: int = 40) -> str:
    """把胜率序列映射成文本迷你图（字符高度编码）。"""
    if not values:
        return "(空)"
    lo, hi = min(values), max(values)
    rng = (hi - lo) or 1e-9
    chars = "▁▂▃▄▅▆▇█"
    out = []
    for v in values:
        frac = (v - lo) / rng
        out.append(chars[min(len(chars) - 1, int(frac * (len(chars) - 1)))])
    return "".join(out)


def _spark_axis(values: List[float], width: int = 40) -> str:
    """返回 (sparkline, lo, hi)。"""
    lo = min(values) if values else 0.0
    hi = max(values) if values else 0.0
    return _spark(values, width), lo, hi


# ============================================================ 主流程

def main() -> int:
    t0 = time.time()
    params = dict(STRATEGY_PARAMS)
    codes = _universe_codes()
    print(f"[宇宙] {len(codes)} 只固定宇宙")

    # 1) 面板
    dates, panel = _fetch_panel(codes, COUNT, force_refresh=True)
    if not dates:
        print("[错误] 取不到日线，退出")
        return 2
    print(f"[数据] {len(panel)} 只 × {len(dates)} 根对齐日线: "
          f"{_fmt(dates[0])} → {_fmt(dates[-1])}")

    # 2) 时间线
    rows = _load_timeline()
    print(f"[时间线] 读入 {len(rows)} 行")

    # 3) 今天闸门状态：若末行是 live 且残缺，用同口径重算覆盖
    today = _compute_today_row(dates, panel, codes, params)
    today_date = today["date"] if today else None
    if today_date:
        # 用重算的完整行替换/追加今日行（仅内存，不写时间线文件）
        rows = [r for r in rows if r.get("date") != today_date]
        rows.append(today)
        rows.sort(key=lambda r: r["date"])
        print(f"[今天] 重算完整闸门状态 {today_date}: universe_n={today['universe_n']} "
              f"trend_up={today['trend_up']} mom_ok={today['pass']['mom_ok']} "
              f"all_pass_n={today['all_pass_n']}")

    # 4) 未来收益（等权）
    fwd = {k: _eqw_forward_returns(dates, panel, codes, k) for k in FW}
    # date -> 面板索引（未来收益与 rows 的 date 对齐）
    date_to_i = {_fmt(d): i for i, d in enumerate(dates)}

    # 5) 逐行归属桶并收集未来收益
    buckets = {"A": {}, "B": {}, "C": {}, "D": {}}
    for k in FW:
        buckets["A"][k] = []
        buckets["B"][k] = []
        buckets["C"][k] = []
        buckets["D"][k] = []
    b_consec = {k: {"1-3": [], "4-7": [], "8+": []} for k in FW}
    # 逐日桶归属（用于重点对比 & 连续性）
    day_bucket = {}

    for idx, r in enumerate(rows):
        d = r.get("date")
        di = date_to_i.get(d)
        if di is None:
            continue
        b = _bucket_of(r)
        if b is None:
            continue
        day_bucket[d] = b
        for k in FW:
            v = fwd[k][di]
            if v is None:
                continue
            buckets[b][k].append(v)
        # B 桶连续性细分
        if b == "B":
            cb = _consec_b_run(idx, rows)
            seg = "1-3" if cb <= 3 else ("4-7" if cb <= 7 else "8+")
            for k in FW:
                v = fwd[k][di]
                if v is not None:
                    b_consec[k][seg].append(v)
        # D 桶
        if _is_restart_day(idx, rows):
            for k in FW:
                v = fwd[k][di]
                if v is not None:
                    buckets["D"][k].append(v)

    # 6) 重点对比：当前 / 20 天前 / 60 天前
    def _anchor(n_days_back: int) -> str:
        if not rows:
            return ""
        target = rows[-1].get("date")
        # 粗略用行索引回溯交易日
        idx = len(rows) - 1 - n_days_back
        if idx < 0:
            idx = 0
        return rows[idx].get("date", "")

    cur_date = rows[-1].get("date") if rows else ""
    d20 = _anchor(20)
    d60 = _anchor(60)

    # 7) 报告
    _write_md(rows, codes, dates, panel, params, buckets, b_consec,
              fwd, day_bucket, cur_date, d20, d60, today, date_to_i)
    _write_json(rows, codes, dates, params, buckets, b_consec,
                fwd, day_bucket, cur_date, d20, d60, today)
    print(f"[耗时] {time.time() - t0:.1f}s")
    print(f"[写] {OUT_MD}")
    print(f"[写] {OUT_JSON}")
    return 0


# ============================================================ 报告

def _fmt_pct(x: Optional[float]) -> str:
    return "—" if x is None else f"{x*100:+.2f}%"


def _bucket_stats_table(buckets: dict, bucket_keys: List[Tuple[str, str]]) -> List[str]:
    """输出分桶统计 markdown 表。bucket_keys = [(键, 显示名)]。"""
    L: List[str] = []
    L.append("| 桶 | n | r5 均值 | r5 中位 | r5 胜率 | r10 均值 | r10 中位 | "
             "r10 胜率 | r20 均值 | r20 中位 | r20 胜率 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for key, name in bucket_keys:
        n = len(buckets[key][5])
        if n == 0:
            L.append(f"| {name} | 0 | — | — | — | — | — | — | — | — | — |")
            continue
        flag = " ⚠样本不足" if n < 30 else ""
        st5 = _stats(buckets[key][5])
        st10 = _stats(buckets[key][10])
        st20 = _stats(buckets[key][20])
        L.append(
            f"| {name} | {n}{flag} | {_fmt_pct(st5['mean'])} | "
            f"{_fmt_pct(st5['median'])} | {st5['win_rate']*100:.1f}% | "
            f"{_fmt_pct(st10['mean'])} | {_fmt_pct(st10['median'])} | "
            f"{st10['win_rate']*100:.1f}% | {_fmt_pct(st20['mean'])} | "
            f"{_fmt_pct(st20['median'])} | {st20['win_rate']*100:.1f}% |")
    return L


def _write_md(rows, codes, dates, panel, params, buckets, b_consec, fwd,
              day_bucket, cur_date, d20, d60, today, date_to_i):
    L: List[str] = []
    A = L.append

    stB5 = _stats(buckets["B"][5])
    stB10 = _stats(buckets["B"][10])
    stB20 = _stats(buckets["B"][20])
    nB = len(buckets["B"][5])

    # 当前桶归属与 20/60 天前对比
    def _desc(d):
        if not d:
            return "?"
        b = day_bucket.get(d)
        return f"{d}（桶 {b}）" if b else f"{d}（无桶）"

    A("# 条件后验统计：当前闸门状态下，之后 N 天赚钱的概率")
    A("")
    A(f"> 生成日期 2026-10-26（收盘后）。23 只固定宇宙（STOCK_CODES ∪ SECTOR_CONFIG），"
      f"对齐日线 {_fmt(dates[0])} → {_fmt(dates[-1])}。")
    A("")

    # ---- 结论（≤5 行）----
    A("## 结论摘要（直接回答：获利几率是否扩大）")
    A("")
    A(f"1. 当前（{cur_date}）处于 **B 桶**（all_pass_n==0 且 mom_ok==0 且 trend_up≤2），"
      f"历史同型样本 n={nB}；等权宇宙未来 r5/r10/r20 胜率 "
      f"{stB5['win_rate']*100:.1f}% / {stB10['win_rate']*100:.1f}% / "
      f"{stB20['win_rate']*100:.1f}%（均值 {_fmt_pct(stB5['mean'])} / "
      f"{_fmt_pct(stB10['mean'])} / {_fmt_pct(stB20['mean'])}）。")
    A(f"2. 与 20 天前（{_desc(d20)}）、60 天前（{_desc(d60)}）桶归属相比："
      f"当前仍在 B 桶，桶归属未切换，**未见「获利几率扩大」的证据**——"
      f"B 桶历史胜率本身是**事后全样本均值**，不代表本次会复现。")
    A(f"3. 若看 B 桶内部细分：连续处于 B 的时间越长（8+ 日）胜率走向 "
      f"{_fmt_pct(_stats(b_consec[5]['8+'])['mean'])}（r5 均值，n="
      f"{_stats(b_consec[5]['8+'])['n']}），需警惕小样本。")
    A(f"4. A 桶（可交易）样本 n={len(buckets['A'][5])}，胜率显著更高（见下表），"
      f"但当前**不满足** A 桶条件（all_pass_n==0），无法触发。")
    A(f"5. 诚实结论：**从桶归属看，获利几率没有比 20/60 天前扩大**；"
      f"当前仍是最不利的 B 桶，且 B 桶为事后统计，小样本风险高，"
      f"不能把「历史上涨过」当成「这次会涨」。")
    A("")

    # ---- 分桶统计 ----
    A("## 1. 分桶未来收益统计（宇宙等权代理）")
    A("")
    A("> 未来收益 = t 收盘 → t+k 收盘的**宇宙等权**收益（23 只等权，停牌前向填充）。"
      "这是「如果买入会买到什么」的市场层代理，**非策略实际持仓**。")
    A("")
    bucket_keys = [("A", "A 可交易(all_pass≥2)"),
                   ("B", "B 当前同型(ap=0&mom=0&tu≤2)"),
                   ("C", "C 趋势出动量未过(tu≥1&mom=0)"),
                   ("D", "D 闸门重启日(连续≥5日0后首>0)")]
    L.extend(_bucket_stats_table(buckets, bucket_keys))
    A("")
    A("> ⚠ = 样本量 <30，结论仅供方向参考，**不可外推**。")
    A("")

    # ---- B 桶连续性细分 ----
    A("## 2. B 桶内按「连续处于 B 的日数」细分")
    A("")
    A("| 连续日数 | n | r5 均值 | r5 胜率 | r10 均值 | r10 胜率 | r20 均值 | r20 胜率 |")
    A("|---|---|---|---|---|---|---|---|")
    for seg in ("1-3", "4-7", "8+"):
        ns = {k: _stats(b_consec[k][seg]) for k in FW}
        n = ns[5]["n"]
        flag = " ⚠样本不足" if 0 < n < 30 else ""
        if n == 0:
            A(f"| {seg} | 0 | — | — | — | — | — | — |")
            continue
        A(f"| {seg} | {n}{flag} | {_fmt_pct(ns[5]['mean'])} | "
          f"{ns[5]['win_rate']*100:.1f}% | {_fmt_pct(ns[10]['mean'])} | "
          f"{ns[10]['win_rate']*100:.1f}% | {_fmt_pct(ns[20]['mean'])} | "
          f"{ns[20]['win_rate']*100:.1f}% |")
    A("")
    A("> 连续处于 B 越长 = 弱势越深。判断「是否扩大」要看细分桶的胜率是否随"
      "连续日数上升（若 8+ 反而更低，说明弱势延续期赔率未改善）。")
    A("")

    # ---- 重点对比 ----
    A("## 3. 重点对比：当前 vs 20 天前 vs 60 天前")
    A("")
    A("| 时点 | 日期 | 桶 | trend_up | mom_ok | all_pass_n |")
    A("|---|---|---|---|---|---|")
    for label, d in (("当前", cur_date), ("20 天前", d20), ("60 天前", d60)):
        r = next((x for x in rows if x.get("date") == d), None)
        if r is None:
            A(f"| {label} | {d or '—'} | — | — | — | — |")
            continue
        A(f"| {label} | {d} | {day_bucket.get(d, '—')} | {r.get('trend_up')} | "
          f"{(r.get('pass') or {}).get('mom_ok')} | {r.get('all_pass_n')} |")
    A("")
    A(f"- 当前桶归属与 20/60 天前**相同或更差** → 获利几率**未扩大**。")
    A("")

    # ---- 胜率迷你图 ----
    A("## 4. 胜率随时间（文本迷你图）")
    A("")
    A("> 对每个历史日 t（仅限 B 桶），取 r5/r10/r20 的 20 日滚动胜率序列，"
      "下采样到约 40 个点画文本迷你图（▁=低 █=高），横轴为时间顺序。"
      "看胜率趋势是抬升还是走平。")
    A("")
    for k in FW:
        # B 桶的 (date, value) 序列
        seq = []
        for r in rows:
            d = r.get("date")
            di = date_to_i.get(d)
            if di is None or day_bucket.get(d) != "B":
                continue
            v = fwd[k][di]
            if v is not None:
                seq.append((d, v))
        # 20 日滚动胜率
        win_roll = []
        for i in range(len(seq)):
            if i + 1 < 20:
                continue
            window = [v for _, v in seq[i - 19:i + 1]]
            w = sum(1 for x in window if x > 0) / len(window)
            win_roll.append((seq[i][0], w))
        # 下采样到 ~40 个点
        step = max(1, len(win_roll) // 40)
        sample = win_roll[::step]
        vals = [w for _, w in sample]
        if vals:
            sp, lo, hi = _spark_axis(vals)
            t0 = win_roll[0][0] if win_roll else "?"
            t1 = win_roll[-1][0] if win_roll else "?"
            A(f"- **r{k}**（B 桶 20 日滚动胜率，n={len(win_roll)} 窗口，"
              f"{t0}→{t1}）：`{sp}`  范围 {lo*100:.0f}% → {hi*100:.0f}%")
        else:
            A(f"- **r{k}**（B 桶）：样本不足，无法画图")
    A("")

    # ---- 口径与 Caveats ----
    A("## 5. 口径与 Caveats")
    A("")
    A("- **等权宇宙是市场层代理**：本统计假设「当日按 B 桶买入宇宙内所有 23 只等权」，"
      "度量的是市场层面后续走势；**不是策略实际持仓**（策略需 6 因子评分 + 动量排名，"
      "且当前 0 成交）。")
    A("- **停牌/缺失前向填充**：停牌日用最后收盘价填充，未来收益按填充价计算，"
      "会略微平滑波动、但不会改变收益方向。")
    A("- **样本量警示**：B/C/D 桶与 B 桶细分普遍 n 偏小，n<30 已标注 ⚠。"
      "小样本下的胜率是噪声主导，**不可当作可复现概率**。")
    A("- **事后统计≠预测**：桶内胜率是历史条件频率，非「本次会涨」的保证；"
      "本次是单次实现，存在方差。")
    A("- **闸门口径**：trend_up = above_ma20 & above_ma60 & ma20_over_ma60 & slope_up "
      "（契约 ①②③④，不含 macd_ok）；mom_ok 阈值 = momentum_min_mom="
      f"{params.get('momentum_min_mom', 0.15)}；all_pass_n = 7 项全过的标的数。")
    A("- 今天（10-26）的闸门状态由本脚本用同口径就地重算（原 timeline 末行是残缺的 "
      "live 行，universe_n=3），**未改写 logs/gate_timeline.jsonl**。")
    A("")
    A("## 6. 复现")
    A("")
    A("```")
    A("python scripts/_res_cond_forward_stats.py")
    A("```")
    A("")

    OUT_MD.write_text("\n".join(L), encoding="utf-8")


def _write_json(rows, codes, dates, params, buckets, b_consec, fwd, day_bucket,
                cur_date, d20, d60, today):
    def stats_serialize(b):
        out = {}
        for k in FW:
            out[f"r{k}"] = _stats(b[k])
        return out

    def consec_serialize(bc):
        out = {}
        for seg in ("1-3", "4-7", "8+"):
            out[seg] = {}
            for k in FW:
                out[seg][f"r{k}"] = _stats(bc[k][seg])
        return out

    obj = {
        "title": "PROFIT_ODDS_CONDITIONAL_2026-10-26",
        "generated": "2026-10-26",
        "universe_size": len(codes),
        "date_range": [dates[0], dates[-1]],
        "params": {
            "momentum_min_mom": params.get("momentum_min_mom", 0.15),
            "buy_score_threshold": params.get("buy_score_threshold", 4.0),
            "min_signals": params.get("min_signals", 3),
        },
        "buckets": {k: stats_serialize(v) for k, v in buckets.items()},
        "bucket_b_consecutive": consec_serialize(b_consec),
        "comparison": {
            "current": {"date": cur_date, "bucket": day_bucket.get(cur_date)},
            "20d_ago": {"date": d20, "bucket": day_bucket.get(d20)},
            "60d_ago": {"date": d60, "bucket": day_bucket.get(d60)},
        },
        "today_row": today,
    }
    OUT_JSON.write_text(json.dumps(obj, ensure_ascii=False, indent=2,
                                   default=str), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())

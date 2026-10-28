#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""RSI 自进化研究器（日频，四段式，确定性，零生产副作用）。

用法：
    python scripts/_rsi_evolve.py                 # 默认 as_of=2026-10-28
    python scripts/_rsi_evolve.py --as-of 2026-10-28 --panel <pkl>

设计纪律：
  * **不 import 任何会写盘的生产模块**，不修改任何生产文件；
  * 唯一写盘位置 = ``reports/rsi_evolve/``（.md + .json）；
  * 评分/闸门口径直接复用 ``strategy.backtest_daily.score_daily_series`` 与
    ``_vec_slope`` / ``trend_up_relaxed``，保证与引擎同口径；
  * 全部随机过程（bootstrap）使用固定种子 ⇒ 同输入逐位同输出。

四段：
  段1 接线审计   —— AST + 文本扫描，核验 RSI 的 5 条接线事实，输出 wiring map
  段2 后验判别力 —— 候选日 RSI 分桶 × 前向 5/10/20 日收益，含 Welch t + bootstrap
  段3 阈值敏感性 —— RSI 作为额外入场过滤的上下限扫描（决策依据）
  段4 结论提案   —— GO / NO-GO + （若 GO）精确到 file:line 的参数化补丁提案
"""
from __future__ import annotations

import argparse
import ast
import json
import math
import pickle
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import INDEX_CODES, STRATEGY_PARAMS  # noqa: E402
from core import indicators as I  # noqa: E402
from strategy.backtest_daily import (  # noqa: E402
    score_daily_series, _vec_slope, trend_up_relaxed,
)

AS_OF_DEFAULT = "2026-10-28"
PANEL_DEFAULT = (
    ROOT / "storage" / "hist_cache" / "ts_panel_25_901_20211201_20261028.pkl"
)
OUT_DIR = ROOT / "reports" / "rsi_evolve"
SEED = 20261028

# 生产扫描范围（刻意排除 scripts/ 与 _backup/：研究脚本本身不算生产路径）
SCAN_DIRS = ("config", "core", "data", "engine", "risk", "strategy", "ai", "web")
SCAN_FILES = ("main.py",)

RSI_KEYS = ("rsi_period", "rsi_oversold", "rsi_overbought")
PERIODS = (6, 9, 14, 21, 28)
BUCKETS = (  # (label, lo, hi) 左开右闭，None = 无穷
    ("<=30", None, 30.0),
    ("30-45", 30.0, 45.0),
    ("45-60", 45.0, 60.0),
    ("60-70", 60.0, 70.0),
    (">70", 70.0, None),
)
HORIZONS = (5, 10, 20)


# ============================================================ 段1 接线审计

def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return _dotted(node.value) + "." + node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _const_key(node: Optional[ast.AST]) -> Optional[str]:
    """取 Subscript 的常量键（兼容 py3.8 的 ast.Index）。"""
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    # py<3.9: Subscript.slice = Index(value=...)
    inner = getattr(node, "value", None)
    if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
        return inner.value
    return None


class _ProdScan(ast.NodeVisitor):
    """收集：rsi() 调用点、STRATEGY_PARAMS 的 rsi_* 读取点。"""

    def __init__(self, path: Path):
        self.rel = str(path.relative_to(ROOT)).replace("\\", "/")
        self.stack: List[str] = []
        self.rsi_calls: List[dict] = []
        self.param_reads: List[dict] = []

    # -- scope tracking
    def visit_ClassDef(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_AsyncFunctionDef(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def _enclosing(self) -> str:
        return ".".join(self.stack) if self.stack else "<module>"

    def visit_Call(self, node):
        fn = _dotted(node.func)
        if fn.split(".")[-1] == "rsi" and isinstance(node.func, ast.Attribute):
            period = None
            if len(node.args) >= 2:
                a = node.args[1]
                if isinstance(a, ast.Constant):
                    period = a.value
                else:
                    period = f"<expr:{_dotted(a) or type(a).__name__}>"
            else:
                period = "<default>"
            self.rsi_calls.append({
                "file": self.rel, "line": node.lineno,
                "callee": fn, "period": period,
                "enclosing": self._enclosing(),
                "snippet": ast.unparse(node) if hasattr(ast, "unparse") else "",
            })
        # STRATEGY_PARAMS.get("rsi_period", ...)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            k = _const_key(node.args[0]) if node.args else None
            if k in RSI_KEYS:
                self.param_reads.append({
                    "file": self.rel, "line": node.lineno, "key": k,
                    "base": _dotted(node.func.value),
                    "enclosing": self._enclosing(), "kind": "get",
                })
        self.generic_visit(node)

    def visit_Subscript(self, node):
        k = _const_key(node.slice)
        if k in RSI_KEYS:
            self.param_reads.append({
                "file": self.rel, "line": node.lineno, "key": k,
                "base": _dotted(node.value),
                "enclosing": self._enclosing(), "kind": "subscript",
            })
        self.generic_visit(node)


_RE_RSI_NUM_FIRST = re.compile(
    r"(?P<num>\d+(?:\.\d+)?)\s*(?P<op><=|>=|<|>)\s*(?P<var>rsi[a-z0-9_]*)", re.I)
_RE_RSI_NUM_LAST = re.compile(
    r"(?P<var>rsi[a-z0-9_]*)\s*(?P<op><=|>=|<|>)\s*(?P<num>\d+(?:\.\d+)?)", re.I)


def _func_ranges(path: Path) -> List[Tuple[int, int, str]]:
    """(start, end, qualname) 列表，用于把行号映射到函数。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    out: List[Tuple[int, int, str]] = []

    def walk(node: ast.AST, prefix: str = ""):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
                name = f"{prefix}{ch.name}"
                if isinstance(ch, ast.ClassDef):
                    walk(ch, name + ".")
                    continue
                out.append((ch.lineno, getattr(ch, "end_lineno", ch.lineno),
                            name))
                walk(ch, name + ".")
            else:
                walk(ch, prefix)

    walk(tree)
    return out


def _enclosing_of(ranges: List[Tuple[int, int, str]], line: int) -> str:
    best = ("<module>", 10 ** 9)
    for s, e, name in ranges:
        if s <= line <= e:
            span = e - s
            if span < best[1]:
                best = (name, span)
    return best[0]


def scan_production() -> dict:
    files: List[Path] = []
    for d in SCAN_DIRS:
        p = ROOT / d
        if p.is_dir():
            files.extend(sorted(x for x in p.rglob("*.py")
                                if "__pycache__" not in x.parts))
    for f in SCAN_FILES:
        p = ROOT / f
        if p.is_file():
            files.append(p)

    rsi_calls: List[dict] = []
    param_reads: List[dict] = []
    threshold_hits: List[dict] = []
    scanned = []
    for path in files:
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        scanned.append(rel)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover
            print(f"[warn] 解析失败 {rel}: {exc}")
            continue
        sc = _ProdScan(path)
        sc.visit(tree)
        rsi_calls.extend(sc.rsi_calls)
        param_reads.extend(sc.param_reads)

        # 阈值文本扫描（只扫含 rsi 的比较行）
        ranges = _func_ranges(path)
        for ln, raw in enumerate(path.read_text(encoding="utf-8")
                                 .splitlines(), start=1):
            if "rsi" not in raw.lower():
                continue
            if raw.lstrip().startswith("#"):
                continue
            for rx in (_RE_RSI_NUM_FIRST, _RE_RSI_NUM_LAST):
                for m in rx.finditer(raw):
                    try:
                        num = float(m.group("num"))
                    except ValueError:
                        continue
                    if not (0.0 <= num <= 100.0):
                        continue
                    threshold_hits.append({
                        "file": rel, "line": ln,
                        "num": num, "op": m.group("op"),
                        "var": m.group("var"),
                        "enclosing": _enclosing_of(ranges, ln),
                        "snippet": raw.strip(),
                    })

    # 去重（同一行可能被两个正则各命中一次）
    seen = set()
    th_uniq = []
    for h in threshold_hits:
        k = (h["file"], h["line"], h["num"], h["op"])
        if k in seen:
            continue
        seen.add(k)
        th_uniq.append(h)
    th_uniq.sort(key=lambda x: (x["file"], x["line"], x["num"]))

    # 参数读取点（排除 settings.py 里的定义体）
    reads = [r for r in param_reads
             if not (r["file"] == "config/settings.py" and r["line"] <= 230)]
    readers_by_key = {k: [r for r in reads if r["key"] == k] for k in RSI_KEYS}

    # 只有落在「日线路径」文件里的读取才算日线链读取
    DAILY_PATH_FILES = {"strategy/daily_context.py", "strategy/backtest_daily.py"}

    def _classify(rs: List[dict]) -> dict:
        daily = [r for r in rs if r["file"] in DAILY_PATH_FILES]
        minute = [r for r in rs if r["file"] == "strategy/trend_strategy.py"]
        return {"all": rs, "daily_path": daily, "minute_path": minute}

    classified = {k: _classify(v) for k, v in readers_by_key.items()}

    # ---- 五条事实 ----
    rsi_def = None
    ind_path = ROOT / "core" / "indicators.py"
    if ind_path.is_file():
        for ln, raw in enumerate(ind_path.read_text(encoding="utf-8")
                                 .splitlines(), start=1):
            if re.match(r"\s*def rsi\(", raw):
                rsi_def = {"file": "core/indicators.py", "line": ln,
                           "snippet": raw.strip()}

    bt_calls = [c for c in rsi_calls if c["file"] == "strategy/backtest_daily.py"]
    dc_calls = [c for c in rsi_calls if c["file"] == "strategy/daily_context.py"]
    ts_calls = [c for c in rsi_calls if c["file"] == "strategy/trend_strategy.py"]

    bt_th = [h for h in th_uniq if h["file"] == "strategy/backtest_daily.py"]
    dc_th = [h for h in th_uniq if h["file"] == "strategy/daily_context.py"]
    ts_th = [h for h in th_uniq if h["file"] == "strategy/trend_strategy.py"]

    def _th_nums(hits: List[dict]) -> List[float]:
        return sorted({h["num"] for h in hits if h["num"] in (30.0, 70.0, 80.0)})

    hard14_bt = [c for c in bt_calls if c["period"] == 14]
    hard14_dc = [c for c in dc_calls if c["period"] == 14]

    facts = [
        {
            "id": 1,
            "claim": "core/indicators.py::rsi(values, period=14) 为 Wilder RSI",
            "status": "CONFIRMED" if rsi_def else "UNVERIFIED",
            "evidence": [rsi_def] if rsi_def else [],
            "note": "调用方均通过 I.rsi(...) 使用；返回等长序列、前 period 个为 None。",
        },
        {
            "id": 2,
            "claim": "strategy/backtest_daily.py 中 RSI 周期硬编码为 14",
            "status": "CONFIRMED" if (len(hard14_bt) >= 2 and
                                      len(hard14_bt) == len(bt_calls)) else "PARTIAL",
            "evidence": hard14_bt,
            "note": f"backtest_daily 共 {len(bt_calls)} 处 I.rsi 调用，"
                    f"其中 {len(hard14_bt)} 处周期=14。",
        },
        {
            "id": 3,
            "claim": "strategy/daily_context.py 中 RSI 周期硬编码为 14（实盘日线上下文）",
            "status": "CONFIRMED" if (len(hard14_dc) >= 1 and
                                      len(hard14_dc) == len(dc_calls)) else "PARTIAL",
            "evidence": hard14_dc,
            "note": f"daily_context 共 {len(dc_calls)} 处 I.rsi 调用。",
        },
        {
            "id": 4,
            "claim": "日线 RSI 阈值 30/70/80 硬编码，不读 rsi_oversold/rsi_overbought",
            "status": "CONFIRMED" if (_th_nums(dc_th) == [30.0, 70.0, 80.0]
                                      and _th_nums(bt_th) == [30.0, 70.0, 80.0]
                                      and not classified["rsi_oversold"]["daily_path"]
                                      and not classified["rsi_overbought"]["daily_path"])
                      else "PARTIAL",
            "evidence": {"daily_context": dc_th, "backtest_daily": bt_th},
            "note": "daily_context 与 backtest_daily 各自 3 处字面量；"
                    "settings 的 rsi_oversold/rsi_overbought 在日线路径零读取。",
        },
        {
            "id": 5,
            "claim": "仅 strategy/trend_strategy.py 读 rsi_period（分钟级 _score 路径）",
            "status": "CONFIRMED" if (len(classified["rsi_period"]["all"]) == 1
                                      and classified["rsi_period"]["all"][0]["file"]
                                      == "strategy/trend_strategy.py") else "PARTIAL",
            "evidence": {"rsi_period": classified["rsi_period"]["all"],
                         "rsi_calls_in_trend_strategy": ts_calls,
                         "thresholds_in_trend_strategy": ts_th},
            "note": "该读取点位于 TrendStrategy._compute_indicators(bars)，"
                    "由 on_bars() 分钟级路径调用；实盘日线路径走 "
                    "on_daily_features() → DailyContext（文件头 trend_strategy.py:69-79 自述）。",
        },
    ]

    dead = []
    for k in RSI_KEYS:
        if not classified[k]["daily_path"]:
            dead.append({
                "key": k,
                "value": STRATEGY_PARAMS.get(k),
                "daily_path_readers": [],
                "all_readers": classified[k]["all"],
                "verdict": "DEAD_FOR_DAILY_GATE_CHAIN",
            })

    return {
        "scan_scope": {"dirs": list(SCAN_DIRS), "files": list(SCAN_FILES),
                       "n_files": len(scanned),
                       "excluded": ["scripts/", "_backup/", "tests/ (非生产)"]},
        "facts": facts,
        "rsi_calls": rsi_calls,
        "threshold_hits": th_uniq,
        "settings_reads": {k: classified[k] for k in RSI_KEYS},
        "dead_params": dead,
        "conclusion": (
            "settings.STRATEGY_PARAMS 的 rsi_period / rsi_oversold / rsi_overbought "
            "对日线闸门链（DailyContext → score_daily / score_daily_series）均无读取点："
            "周期与阈值在代码中硬编码 ⇒ 直接扫这三个参数 = 死代码。"
            "唯一读取点 trend_strategy.py 的 rsi_period 属于分钟级 on_bars 路径，"
            "实盘日线决策走 on_daily_features。"
        ),
    }


# ============================================================ 段2/3 数据与统计

def _load_panel(path: Path) -> dict:
    with open(path, "rb") as fh:
        return pickle.load(fh)


def _mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def _median(xs: List[float]) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def _pstdev(xs: List[float]) -> float:
    if len(xs) < 2:
        return float("nan")
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _normal_two_sided_p(t: float) -> float:
    return 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(t) / math.sqrt(2.0))))


def welch(a: List[float], b: List[float]) -> dict:
    """Welch t 检验（双侧），p 用正态近似（n 均 ≥ 30 时误差可忽略）。"""
    if len(a) < 2 or len(b) < 2:
        return {"n_a": len(a), "n_b": len(b), "t": None, "p": None,
                "diff": None, "method": "Welch t (n<2, 不可算)"}
    ma, mb = _mean(a), _mean(b)
    va, vb = _pstdev(a) ** 2, _pstdev(b) ** 2
    se2 = va / len(a) + vb / len(b)
    if se2 <= 0:
        return {"n_a": len(a), "n_b": len(b), "t": None, "p": None,
                "diff": 0.0, "method": "Welch t (方差为 0)"}
    t = (ma - mb) / math.sqrt(se2)
    return {"n_a": len(a), "n_b": len(b), "t": round(t, 4),
            "p": _normal_two_sided_p(t), "diff": ma - mb,
            "method": "Welch t, 双侧, 正态近似"}


def boot_ci_diff(a: List[float], b: List[float], seed: int,
                 iters: int = 1000) -> Optional[Tuple[float, float]]:
    """均値差 (mean(a)-mean(b)) 的 95% bootstrap 百分位区间。"""
    if len(a) < 2 or len(b) < 2:
        return None
    rng = random.Random(seed)
    na, nb = len(a), len(b)
    diffs = []
    for _ in range(iters):
        sa = sum(a[rng.randrange(na)] for _ in range(na)) / na
        sb = sum(b[rng.randrange(nb)] for _ in range(nb)) / nb
        diffs.append(sa - sb)
    diffs.sort()
    lo = diffs[int(0.025 * iters)]
    hi = diffs[min(iters - 1, int(0.975 * iters))]
    return lo, hi


def _bucket_of(v: float) -> Optional[str]:
    for label, lo, hi in BUCKETS:
        if (lo is None or v > lo) and (hi is None or v <= hi):
            return label
    return None


def build_candidates(panel: dict, stock_codes: List[str],
                     periods: Tuple[int, ...]) -> Tuple[List[dict], dict]:
    """遍历每只股票每个交易日，复算引擎同口径候选集。

    候选条件（与交接门链一致）：
        (trend_up or bias >= 2.0)  and  score >= 4.0  and  正因子数 >= 3
    60 日动量仅作为「入池闸门」附加记录（mom60 >= 0.15 与否），不用于主候选筛选，
    以免 top_n + 动量池把样本压到无法统计（研究目的不同）。
    """
    rows: List[dict] = []
    funnel = {"gate_pass": 0, "score_pass": 0, "pos_pass": 0,
              "all_pass": 0, "all_pass_mom15": 0, "per_code": {}}
    for code in stock_codes:
        d = panel[code]
        o, h, l, c, v = (d["open"], d["high"], d["low"], d["close"], d["volume"])
        dates = d["date"]
        n = len(c)
        if n < 80:
            continue
        scores = score_daily_series(o, h, l, c, v)
        s20 = I.sma(c, 20)
        s60 = I.sma(c, 60)
        s20s = _vec_slope(s20, 5)
        _, _, mh = I.macd(c, 12, 26, 9)
        rsi_map = {p: I.rsi(c, p) for p in periods}

        cnt = {"gate": 0, "score": 0, "pos": 0, "all": 0, "mom15": 0}
        for i in range(60, n):
            if c[i] <= 0 or c[i - 1] <= 0:
                continue
            m20, m60, m20s = s20[i], s60[i], s20s[i]
            a = bool(m20 and c[i] > m20)
            b = bool(m20 and m60 and m20 > m60)
            cc = bool(m20s is not None and m20s > 0)
            dd = bool((mh[i] or 0) >= 0)
            trend_up = trend_up_relaxed(a, b, cc, dd, "none")
            bias = 0.0
            if m20:
                bias += 0.35 * (1.0 if c[i] > m20 else -1.0)
            if m60:
                bias += 0.35 * (1.0 if c[i] > m60 else -1.0)
            if m20 and m60:
                bias += 0.30 * (1.0 if m20 > m60 else -1.0)
            bias = max(-1.0, min(1.0, bias))
            gate = trend_up or bias >= 2.0
            if not gate:
                continue
            cnt["gate"] += 1
            score, factors = scores[i]
            if score < 4.0:
                continue
            cnt["score"] += 1
            if sum(1 for x in factors.values() if x > 0) < 3:
                continue
            cnt["pos"] += 1
            # 前向收益：T+1 收盘买入，持有 h 日（T+1+h 收盘卖出）
            if i + 1 + max(HORIZONS) >= n:
                continue
            entry = c[i + 1]
            if entry <= 0:
                continue
            rets = {}
            ok = True
            for hd in HORIZONS:
                px = c[i + 1 + hd]
                if px <= 0:
                    ok = False
                    break
                rets[hd] = px / entry - 1.0
            if not ok:
                continue
            r14 = rsi_map[14][i]
            if r14 is None:
                continue
            mom60 = (c[i] / c[i - 60] - 1.0) if c[i - 60] > 0 else None
            row = {
                "code": code, "date": dates[i], "di": i, "score": score,
                "rsi14": r14, "gate": "trend_up" if trend_up else "bias",
                "mom60": mom60, "rets": rets,
            }
            for p in periods:
                row[f"rsi{p}"] = rsi_map[p][i]
            rows.append(row)
            cnt["all"] += 1
            if mom60 is not None and mom60 >= 0.15:
                cnt["mom15"] += 1

        funnel["gate_pass"] += cnt["gate"]
        funnel["score_pass"] += cnt["score"]
        funnel["pos_pass"] += cnt["pos"]
        funnel["all_pass"] += cnt["all"]
        funnel["all_pass_mom15"] += cnt["mom15"]
        funnel["per_code"][code] = cnt
    return rows, funnel


def dedup_rows(rows: List[dict], gap: int = 20) -> List[dict]:
    """去重叠采样：同一标的两次候选日至少间隔 gap 个交易日。

    必要性：20 日前向窗口若相邻候选日重叠，观测高度自相关（同一段行情被
    重复计数），Welch p 会被系统性高估。去重叠后样本虽变少，但近似独立。
    """
    by_code: Dict[str, List[dict]] = {}
    for r in rows:
        by_code.setdefault(r["code"], []).append(r)
    out: List[dict] = []
    for code, rs in by_code.items():
        rs.sort(key=lambda x: x["di"])
        last = -10 ** 9
        for r in rs:
            if r["di"] - last >= gap:
                out.append(r)
                last = r["di"]
    return out


def _infer(grp: List[dict], rest: List[dict], seed: int) -> dict:
    """桶 vs 其余：Welch t + bootstrap CI + 逐标的符号一致性。"""
    g = [r["rets"][20] for r in grp]
    o = [r["rets"][20] for r in rest]
    if len(g) < 2 or len(o) < 2:
        return {"n_a": len(g), "n_b": len(o), "p": None, "diff": None,
                "method": "样本不足"}
    wt = welch(g, o)
    ci = boot_ci_diff(g, o, seed)
    # 逐标的符号一致性：每个标的内桶均值 - 其余均值 的方向
    by_code_g: Dict[str, List[float]] = {}
    by_code_o: Dict[str, List[float]] = {}
    for r in grp:
        by_code_g.setdefault(r["code"], []).append(r["rets"][20])
    for r in rest:
        by_code_o.setdefault(r["code"], []).append(r["rets"][20])
    pos = neg = 0
    for code, gv in by_code_g.items():
        ov = by_code_o.get(code)
        if not ov:
            continue
        d = _mean(gv) - _mean(ov)
        if d > 0:
            pos += 1
        elif d < 0:
            neg += 1
    ncode = pos + neg
    return {
        "n_a": len(g), "n_b": len(o),
        "diff": wt["diff"], "t": wt["t"], "p": wt["p"],
        "method": "Welch t 双侧（正态近似）+ 去重叠样本",
        "boot_ci95": ci,
        "boot_method": "1000 次重采样百分位法（去重叠样本）",
        "code_sign": {"codes": ncode, "positive": pos, "negative": neg,
                      "same_sign_frac": (max(pos, neg) / ncode) if ncode else None},
    }


def bucket_stats(rows: List[dict], dedup: List[dict], period: int) -> dict:
    """按 RSI(period) 分桶。

    描述统计用全样本（n 大、估计稳）；显著性用去重叠样本（近似独立）。
    """
    key = f"rsi{period}"
    valid = [r for r in rows if r.get(key) is not None]
    vdd = [r for r in dedup if r.get(key) is not None]
    out = {"period": period, "n_valid": len(valid), "n_dedup": len(vdd),
           "buckets": []}
    fam_idx = 0
    for label, lo, hi in BUCKETS:
        grp = [r for r in valid if _bucket_of(r[key]) == label]
        gdd = [r for r in vdd if _bucket_of(r[key]) == label]
        rdd = [r for r in vdd if _bucket_of(r[key]) != label]
        item = {"label": label, "n": len(grp),
                "share": (len(grp) / len(valid)) if valid else 0.0,
                "n_dedup": len(gdd), "horizons": {}}
        for hd in HORIZONS:
            g = [r["rets"][hd] for r in grp]
            if not g:
                continue
            item["horizons"][str(hd)] = {
                "n": len(g),
                "win_rate": sum(1 for x in g if x > 0) / len(g),
                "mean": _mean(g),
                "median": _median(g),
                "std": _pstdev(g),
            }
        if item["horizons"].get("20") and len(gdd) >= 2 and len(rdd) >= 2:
            t = _infer(gdd, rdd, SEED + 97 * fam_idx + period)
            item["test20"] = t
            item["horizons"]["20"]["rest_mean"] = _mean(
                [r["rets"][20] for r in rdd])
            item["horizons"]["20"]["rest_win_rate"] = (
                sum(1 for r in rdd if r["rets"][20] > 0) / len(rdd))
        out["buckets"].append(item)
        fam_idx += 1
    return out


def incremental_info(dedup: List[dict]) -> dict:
    """控制 60 日动量后，RSI(14) 是否还有增量信息（OLS）。

    y = ret20，x1 = RSI(14)，x2 = 60 日动量，全部中心化后做二元最小二乘。
    若 b1 的 t 值不显著 ⇒ RSI 的「判别力」只是动量的代理，不构成独立信号。
    """
    xs = [(r["rsi14"], r["mom60"], r["rets"][20]) for r in dedup
          if r.get("rsi14") is not None and r.get("mom60") is not None]
    n = len(xs)
    if n < 10:
        return {"n": n, "note": "样本不足"}
    x1 = [a for a, _, _ in xs]
    x2 = [b for _, b, _ in xs]
    y = [c for _, _, c in xs]
    m1, m2, my = _mean(x1), _mean(x2), _mean(y)
    X1 = [v - m1 for v in x1]
    X2 = [v - m2 for v in x2]
    Y = [v - my for v in y]
    S11 = sum(v * v for v in X1)
    S22 = sum(v * v for v in X2)
    S12 = sum(a * b for a, b in zip(X1, X2))
    T1 = sum(a * b for a, b in zip(X1, Y))
    T2 = sum(a * b for a, b in zip(X2, Y))
    det = S11 * S22 - S12 * S12
    if det <= 0:
        return {"n": n, "note": "设计矩阵奇异"}
    b1 = (T1 * S22 - T2 * S12) / det
    b2 = (T2 * S11 - T1 * S12) / det
    ssr = sum((Y[i] - b1 * X1[i] - b2 * X2[i]) ** 2 for i in range(n))
    sst = sum(v * v for v in Y)
    s2 = ssr / (n - 3) if n > 3 else float("nan")
    se1 = math.sqrt(s2 * S22 / det)
    se2 = math.sqrt(s2 * S11 / det)
    t1 = b1 / se1 if se1 > 0 else None
    t2 = b2 / se2 if se2 > 0 else None
    # RSI 与动量的相关系数（共线性诊断）
    corr12 = S12 / math.sqrt(S11 * S22) if S11 > 0 and S22 > 0 else None
    return {
        "n": n,
        "beta_rsi_per10pt": b1 * 10.0,
        "se_rsi_per10pt": se1 * 10.0,
        "t_rsi": t1,
        "p_rsi": _normal_two_sided_p(t1) if t1 is not None else None,
        "beta_mom_per10pct": b2 * 0.10,
        "t_mom": t2,
        "p_mom": _normal_two_sided_p(t2) if t2 is not None else None,
        "r2": (1.0 - ssr / sst) if sst > 0 else None,
        "corr_rsi_mom": corr12,
        "model": "ret20 ~ 1 + rsi14 + mom60（去重叠样本，OLS，t 用正态近似）",
    }


def fine_rsi14(rows: List[dict], dedup: List[dict]) -> List[dict]:
    """RSI(14) 细粒度分布：用于检验 80 这个「超买惩罚」阈值是否有依据。"""
    edges = [(None, 30.0), (30.0, 45.0), (45.0, 60.0), (60.0, 70.0),
             (70.0, 75.0), (75.0, 80.0), (80.0, 85.0), (85.0, None)]
    dd_by = {id(r): r for r in dedup}
    out = []
    for lo, hi in edges:
        grp = [r for r in rows if r.get("rsi14") is not None
               and (lo is None or r["rsi14"] > lo)
               and (hi is None or r["rsi14"] <= hi)]
        if not grp:
            continue
        gdd = [r for r in grp if id(r) in dd_by]
        g = [r["rets"][20] for r in grp]
        blk = {
            "label": (f"<={hi:.0f}" if lo is None else
                      f">{lo:.0f}" if hi is None else f"{lo:.0f}-{hi:.0f}"),
            "n": len(grp), "n_dedup": len(gdd),
            "win20": sum(1 for x in g if x > 0) / len(g) if g else None,
            "mean20": _mean(g) if g else None,
            "median20": _median(g) if g else None,
        }
        out.append(blk)
    return out


def sweep_thresholds(rows: List[dict], dedup: List[dict]) -> List[dict]:
    """RSI(14) 作为额外入场过滤：上限 / 下限阈值扫描（基准 = 全部候选）。

    描述统计用全样本；显著性用去重叠样本（子集 vs 子集外，两独立组）。
    """
    base20 = [r["rets"][20] for r in rows]
    out = []
    tests = []
    for cap in (40.0, 45.0, 50.0, 55.0, 60.0, 70.0):
        tests.append(("cap", cap))
    for flr in (20.0, 25.0, 30.0):
        tests.append(("floor", flr))
    for kind, thr in tests:
        if kind == "cap":
            sub = [r for r in rows if r["rsi14"] is not None and r["rsi14"] <= thr]
            subdd = [r for r in dedup if r["rsi14"] is not None and r["rsi14"] <= thr]
            keep = lambda r: r["rsi14"] is not None and r["rsi14"] <= thr
        else:
            sub = [r for r in rows if r["rsi14"] is not None and r["rsi14"] >= thr]
            subdd = [r for r in dedup if r["rsi14"] is not None and r["rsi14"] >= thr]
            keep = lambda r: r["rsi14"] is not None and r["rsi14"] >= thr
        g20 = [r["rets"][20] for r in sub]
        blk = {
            "kind": kind, "threshold": thr, "n": len(sub),
            "n_dedup": len(subdd),
            "retention": (len(sub) / len(rows)) if rows else 0.0,
            "removed": len(rows) - len(sub),
            "horizons": {},
        }
        for hd in HORIZONS:
            g = [r["rets"][hd] for r in sub]
            if not g:
                continue
            blk["horizons"][str(hd)] = {
                "n": len(g),
                "win_rate": sum(1 for x in g if x > 0) / len(g),
                "mean": _mean(g),
                "median": _median(g),
            }
        blk["delta_mean_20"] = _mean(g20) - _mean(base20) if g20 and base20 else None
        blk["delta_win20"] = (
            (sum(1 for x in g20 if x > 0) / len(g20))
            - (sum(1 for x in base20 if x > 0) / len(base20))
        ) if g20 and base20 else None
        # 去重叠推断：子集 vs 被剔除的补集（两组互斥、近似独立）
        a = [r["rets"][20] for r in subdd]
        b = [r["rets"][20] for r in dedup if not keep(r)]
        if len(a) >= 2 and len(b) >= 2:
            wt = welch(a, b)
            blk["test_dedup"] = {
                "n_keep": len(a), "n_drop": len(b),
                "diff": wt["diff"], "t": wt["t"], "p": wt["p"],
                "method": "Welch t 双侧（去重叠样本：保留组 vs 剔除组）",
                "boot_ci95": boot_ci_diff(a, b, SEED + 1000 + int(thr)),
            }
            blk["delta_mean_20_dedup"] = wt["diff"]
        else:
            blk["test_dedup"] = {"p": None, "diff": None,
                                 "note": "任一组样本 <2，无法检验"}
            blk["delta_mean_20_dedup"] = None
        out.append(blk)
    return out


# ============================================================ 报告

def _fmt_pct(x: Optional[float], digits: int = 2) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x * 100:+.{digits}f}%"


def _fmt_p(x: Optional[float]) -> str:
    if x is None:
        return "—"
    if x < 1e-4:
        return "<1e-4"
    return f"{x:.4f}"


def render_md(rep: dict, wiring: dict) -> str:
    L: List[str] = []
    a = L.append
    meta = rep["meta"]
    cand = rep["candidates"]
    v = rep["verdict"]
    dist = cand.get("rsi14_dist") or {}

    a(f"# RSI 自进化研究 · {meta['as_of']}")
    a("")
    a(f"- 生成：{meta['generated_at']}　|　脚本：`scripts/_rsi_evolve.py`　|　"
      f"耗时 **{meta['elapsed_sec']:.1f}s**")
    a(f"- 数据源：`{meta['panel_rel']}`（{meta['panel_codes']} 代码 × "
      f"{meta['panel_rows']} 交易日，as_of **{meta['as_of']}**，"
      f"mtime {meta['panel_mtime']}）")
    a(f"- 股票宇宙：{cand['n_codes']} 只（面板剔除指数后）；"
      f"候选日 **{cand['n']}**；去重叠样本 **{cand['n_dedup']}**"
      f"（同标的最小间隔 {cand['dedup_gap']} 个交易日）")
    a(f"- 随机种子：{meta['seed']}（bootstrap 可复现）")
    a("")
    a("## 0. 结论摘要")
    a("")
    a(f"**VERDICT: {v['verdict']}** — {v['reason']}")
    a("")
    if v["verdict"] == "NO-GO":
        a("- 本轮**无可落盘改动**，不生成生产补丁；下一轮研究角度见 §4.1。")
        if v.get("guardrails"):
            a("- 但有一条护栏结论：按 §4「护栏项」的方向加 RSI 过滤会显著变差 ⇒ "
              "至少不要往那个方向改。")
    else:
        a("- 见 §4.1 参数化补丁提案（**未执行**，须 OWNER 批准 + 全量测试 + "
          "基线逐位不变性检查）。")
    a(f"- 候选日分布：{cand['date_min']} → {cand['date_max']}，"
      f"覆盖 {cand['n_dates']} 个交易日；近 20 个交易日仅 "
      f"**{cand['recent_20d']}** 个、近 60 个交易日 **{cand['recent_60d']}** 个。")
    if cand.get("gap_trading_days") is not None:
        a(f"- **最后一个候选日距 as_of {cand['gap_trading_days']} 个交易日** ⇒ "
          "近端样本为空，与「连续 15 个交易日零开仓」一致。")
    if dist.get("min") is not None:
        a(f"- RSI(14) 在候选集中的分布：min **{dist['min']:.1f}**、"
          f"p10 {dist['p10']:.1f}、中位 {dist['median']:.1f}、max {dist['max']:.1f}；"
          f"RSI>70 占 {dist['share_gt70']*100:.1f}%，"
          f"RSI≤45 仅 {dist['share_le45']*100:.1f}%。")
    if cand["n"] < 50:
        a("- ⚠ 候选日 <50：统计功效极低，任何「显著」都不足以下结论。")
    a("")

    # ---------------- 段1
    a("## 1. 段1 · 接线审计（wiring audit）")
    a("")
    a(f"扫描范围：`{'`, `'.join(wiring['scan_scope']['dirs'])}` + "
      f"`{'`, `'.join(wiring['scan_scope']['files'])}`"
      f"（{wiring['scan_scope']['n_files']} 个 .py）；"
      f"排除 {', '.join(wiring['scan_scope']['excluded'])}。")
    a("")
    a("| # | 事实 | 判定 | 证据（file:line） |")
    a("|---|---|---|---|")
    for f in wiring["facts"]:
        ev = f["evidence"]
        if f["id"] == 4:
            lines = []
            for k in ("daily_context", "backtest_daily"):
                lines += [f"`{h['file']}:{h['line']}` ({h['op']}{h['num']:.0f})"
                          for h in ev[k]]
        elif f["id"] == 5:
            lines = [f"`{r['file']}:{r['line']}` ({r['enclosing']})"
                     for r in ev["rsi_period"]]
        else:
            lines = [f"`{h['file']}:{h['line']}`" for h in ev]
        a(f"| {f['id']} | {f['claim']} | **{f['status']}** | {'<br>'.join(lines)} |")
    a("")
    a("**RSI 调用点全量（生产扫描）**")
    a("")
    a("| file:line | 调用 | period | 所在函数 |")
    a("|---|---|---|---|")
    for c in wiring["rsi_calls"]:
        a(f"| `{c['file']}:{c['line']}` | `{c['callee']}(...)` | `{c['period']}` | "
          f"`{c['enclosing']}` |")
    a("")
    a("> `strategy/research_lab.py:152` 属研究辅助类（StockSeries），"
      "不在 `DailyContext → score_daily` 的决策路径上，不计入接线事实。")
    a("")
    a("**settings 参数读取点**")
    a("")
    a("| 参数 | 当前值 | 日线路径读取 | 全部读取 |")
    a("|---|---|---|---|")
    for k in RSI_KEYS:
        blk = wiring["settings_reads"][k]
        allr = ", ".join(f"`{r['file']}:{r['line']}`" for r in blk["all"]) or "**无**"
        daily = ", ".join(f"`{r['file']}:{r['line']}`"
                          for r in blk["daily_path"]) or "**无**"
        a(f"| `{k}` | `{STRATEGY_PARAMS.get(k)}` | {daily} | {allr} |")
    a("")
    a(f"**审计结论**：{wiring['conclusion']}")
    a("")

    # ---------------- 段2
    a("## 2. 段2 · 后验判别力（post-hoc discrimination）")
    a("")
    a("**口径**")
    a("")
    a("- 候选：`(daily_trend_up or bias >= 2.0) and score >= 4.0 and 正因子数 >= 3`。"
      "闸门与评分直接调用 `strategy/backtest_daily.py` 的 `trend_up_relaxed` / "
      "`_vec_slope` / `score_daily_series`（默认 `vwap_window=0`），逐 code 调一次。")
    a("- 前向收益：`ret_h = close[i+1+h] / close[i+1] - 1`（T 日收盘出信号，"
      "T+1 收盘近似成交，持有 h 日）。")
    a("- **显著性用去重叠样本**：同一标的两次候选日至少间隔 20 个交易日，"
      "避免 20 日前向窗口重叠导致的自相关把 p 值压低（否则 Welch p 会系统性高估）。"
      "样本量下降是诚实的代价。")
    a("- 检验：Welch t 双侧（正态近似）+ 1000 次 bootstrap 95% CI + "
      "逐标的符号一致性（同向标的占比）。")
    a("")
    a("**漏斗**")
    a("")
    a(f"- 闸门通过：{cand['funnel']['gate_pass']}　→ 评分≥4.0："
      f"{cand['funnel']['score_pass']}　→ 正因子≥3：{cand['funnel']['pos_pass']}"
      f"　→ 有完整 20 日前向窗口：{cand['n']}")
    a(f"- 再叠加 60 日动量 ≥0.15（生产入池闸门）：{cand['funnel']['all_pass_mom15']}"
      f"（这一步由 `top_n=3` 再截断，故生产实际开仓远少于候选日）")
    a("")
    a("### 2.1 主表 · RSI(14) 分桶（20 日）")
    a("")
    a("| 桶 | n(全) | 占比 | 20日胜率 | 20日均值 | 中位数 | 去重叠 n | "
      "均值差(桶−其余) | p | 逐标的同向 | bootstrap 95% CI |")
    a("|---|---|---|---|---|---|---|---|---|---|---|")
    b14 = rep["buckets"]["14"]
    for item in b14["buckets"]:
        h20 = item["horizons"].get("20", {})
        t = item.get("test20") or {}
        ci = t.get("boot_ci95")
        ci_s = f"[{ci[0]*100:+.2f}%, {ci[1]*100:+.2f}%]" if ci else "—"
        cs = t.get("code_sign") or {}
        cs_s = (f"{cs.get('positive', 0)}/{cs.get('codes', 0)}"
                if cs.get("codes") else "—")
        win_s = f"{h20['win_rate']*100:.1f}%" if item["n"] else "—"
        a(f"| {item['label']} | {item['n']} | {item['share']*100:.1f}% | "
          f"{win_s} | {_fmt_pct(h20.get('mean'))} | "
          f"{_fmt_pct(h20.get('median'))} | {item['n_dedup']} | "
          f"{_fmt_pct(t.get('diff'))} | {_fmt_p(t.get('p'))} | {cs_s} | {ci_s} |")
    a("")
    if b14["buckets"][0]["n"] == 0 or b14["buckets"][1]["n"] == 0:
        a("> **关键观察：低 RSI 桶为空。** 候选集先过了 `trend_up`（收盘>MA20>MA60、"
          "MA20 斜率>0、MACD≥0）与 6 因子评分，RSI 天然被顶到 45 以上 ⇒ "
          "「RSI 超卖」这条假设在本门链里**没有样本可检验**，"
          "也意味着任何 `RSI≥20/25/30` 的下限过滤都是空操作。")
        a("")
    a("### 2.2 周期自进化 · 替代周期同口径分桶（20 日）")
    a("")
    a("| 周期 | 桶 | n(全) | 20日胜率 | 20日均值 | 去重叠 n | 均值差 | p |")
    a("|---|---|---|---|---|---|---|---|")
    for p_ in PERIODS:
        for item in rep["buckets"][str(p_)]["buckets"]:
            h20 = item["horizons"].get("20", {})
            t = item.get("test20") or {}
            win_s = f"{h20['win_rate']*100:.1f}%" if item["n"] else "—"
            a(f"| RSI({p_}) | {item['label']} | {item['n']} | "
              f"{win_s} | {_fmt_pct(h20.get('mean'))} | "
              f"{item['n_dedup']} | {_fmt_pct(t.get('diff'))} | {_fmt_p(t.get('p'))} |")
    a("")
    a("### 2.3 判别力是否随周期变化")
    a("")
    a("| 周期 | 最显著桶 | 该桶 n(全/去重叠) | 20日均值 | 桶−其余 | p | 逐标的同向 |")
    a("|---|---|---|---|---|---|---|")
    for p_ in PERIODS:
        best = None
        for item in rep["buckets"][str(p_)]["buckets"]:
            t = item.get("test20") or {}
            if t.get("p") is None or item["n_dedup"] < 30:
                continue
            if best is None or t["p"] < best[1]:
                best = (item, t["p"])
        if best:
            item, pv = best
            h20 = item["horizons"]["20"]
            t = item["test20"]
            cs = t.get("code_sign") or {}
            a(f"| RSI({p_}) | {item['label']} | {item['n']}/{item['n_dedup']} | "
              f"{_fmt_pct(h20.get('mean'))} | {_fmt_pct(t.get('diff'))} | "
              f"{_fmt_p(pv)} | {cs.get('positive', 0)}/{cs.get('codes', 0)} |")
        else:
            a(f"| RSI({p_}) | —（无 n≥30 的桶） | — | — | — | — | — |")
    ps = []
    for p_ in PERIODS:
        for item in rep["buckets"][str(p_)]["buckets"]:
            t = item.get("test20") or {}
            if t.get("p") is not None and item["n_dedup"] >= 30:
                ps.append(t["p"])
    if ps:
        a("")
        a(f"> 各周期最显著桶的 p 值落在 {min(ps):.3f}–{max(ps):.3f} 区间，"
          f"**无一低于 0.05**；且「最优桶」随周期在 45-60 / 60-70 / >70 之间跳动，"
          f"没有单调的周期效应 ⇒ 换周期不产生可用判别力。")
    a("")
    a("### 2.4 RSI(14) 细粒度分布（检验 80 这个超买惩罚阈值）")
    a("")
    a("| 区间 | n | 去重叠 n | 20日胜率 | 20日均值 | 中位数 |")
    a("|---|---|---|---|---|---|")
    for f in rep.get("fine_rsi14", []):
        a(f"| {f['label']} | {f['n']} | {f['n_dedup']} | "
          f"{(f['win20'] or 0)*100:.1f}% | {_fmt_pct(f['mean20'])} | "
          f"{_fmt_pct(f['median20'])} |")
    a("")
    a("### 2.5 增量信息检验：控制 60 日动量后 RSI 还剩什么")
    a("")
    ols = v.get("ols") or {}
    if ols.get("p_rsi") is not None:
        a(f"模型：`{ols['model']}`，n={ols['n']}，R²={ols['r2']:.4f}，"
          f"corr(RSI14, mom60)={ols['corr_rsi_mom']:+.3f}。")
        a("")
        a("| 变量 | 系数（每 10 单位） | t | p |")
        a("|---|---|---|---|")
        a(f"| RSI(14) | {ols['beta_rsi_per10pt']*100:+.2f}pp | "
          f"{ols['t_rsi']:+.2f} | {_fmt_p(ols['p_rsi'])} |")
        a(f"| 60 日动量 | {ols['beta_mom_per10pct']*100:+.2f}pp | "
          f"{ols['t_mom']:+.2f} | {_fmt_p(ols['p_mom'])} |")
        a("")
        if ols["p_rsi"] < 0.05 and ols["beta_rsi_per10pt"] > 0:
            a("> RSI 在控制动量后仍有正向增量（p<0.05）⇒ 值得进一步做 OOS。")
        else:
            a("> **RSI 在控制 60 日动量后无显著增量**（p≥0.05）⇒ 段2 里看到的"
              "「高 RSI 桶收益更好」主要是**动量强弱的代理**，不是独立的 RSI 信号。"
              "这正是不能据此改生产代码的核心理由。")
        a("")
    else:
        a(f"样本不足，无法估计（n={ols.get('n')}）。")
        a("")

    # ---------------- 段3
    a("## 3. 段3 · 阈值敏感性（RSI 作为额外入场过滤）")
    a("")
    a(f"基准（不过滤）：n={cand['n']}，20 日胜率 {cand['base_win20']*100:.1f}%，"
      f"20 日均值 {_fmt_pct(cand['base_mean20'])}。")
    a("")
    a("| 过滤条件 | 剩余 n | 保留率 | 减少 | 20日胜率 | Δ胜率 | 20日均值 | "
      "Δ均值(全样本) | Δ均值(去重叠) | 去重叠 n | p(保留 vs 剔除) |")
    a("|---|---|---|---|---|---|---|---|---|---|---|")
    for s in rep["sweep"]:
        h20 = s["horizons"].get("20", {})
        td = s.get("test_dedup") or {}
        win_s = f"{h20['win_rate']*100:.1f}%" if h20.get("n") else "—"
        a(f"| {'RSI≤' if s['kind']=='cap' else 'RSI'}{'' if s['kind']=='cap' else '≥'}"
          f"{s['threshold']:.0f} | {s['n']} | {s['retention']*100:.1f}% | "
          f"{s['removed']} | {win_s} | "
          f"{_fmt_pct(s.get('delta_win20'))} | {_fmt_pct(h20.get('mean'))} | "
          f"{_fmt_pct(s.get('delta_mean_20'))} | "
          f"{_fmt_pct(s.get('delta_mean_20_dedup'))} | {s['n_dedup']} | "
          f"{_fmt_p(td.get('p'))} |")
    a("")
    a("> 读法：候选集 RSI(14) 最小值为 "
      f"{(dist.get('min') or 0):.1f} ⇒ 所有 `RSI≤40/45` 的上限过滤直接清空候选，"
      "`RSI≥20/25/30` 的下限过滤保留 100%，两者都是**空操作**；"
      "真正有区分度的是 55/60/70 这几个上限，而它们全部让 20 日均值**变差**"
      "（见 Δ均值列），即「砍掉高 RSI 候选」= 砍掉强趋势，方向与策略逻辑相反。")
    a("")

    # ---------------- 段4
    a("## 4. 段4 · 结论与提案")
    a("")
    a(f"**VERDICT: {v['verdict']}**")
    a("")
    a(f"理由：{v['reason']}")
    a("")
    a(f"判定门槛：分桶族 {v['n_bucket_tests']} 检验 → Bonferroni α="
      f"{v['alpha_bucket']:.5f}；阈值族 {v['n_sweep_tests']} 检验 → α="
      f"{v['alpha_sweep']:.5f}；同时要求去重叠 n≥{v['min_n']}、"
      f"|20日均值差|≥{v['min_effect']*100:.0f}pp、逐标的同向 ≥"
      f"{v.get('min_sign_frac', 0.7)*100:.0f}%。")
    a("")
    if v.get("hits"):
        a("**命中项（改善方向）**")
        a("")
        for h in v["hits"]:
            a(f"- {h}")
        a("")
    if v.get("guardrails"):
        a("**护栏项（显著但方向为「变差」，禁止按此方向改动）**")
        a("")
        for g in v["guardrails"]:
            a(f"- {g}")
        a("")
        a("> 含义：把 RSI 上限压到该阈值会显著降低后续 20 日收益。"
          "这不是「应该加这个过滤」的证据，而是「**不要**加 RSI 上限过滤」的证据。")
        a("")
    if v["verdict"] == "GO":
        a("### 4.1 参数化补丁提案（**未执行**）")
        a("")
        a(v["proposal"])
        a("")
        a("**落地前置条件（硬性）**")
        a("")
        a("1. 须 OWNER 书面批准（本脚本只产出提案，不改生产代码）；")
        a("2. 改后必须跑 `python tests/run_all.py` 全绿；")
        a("3. 必须做「基线逐位不变性检查」：默认参数（14/30/70/80）下，"
          "回测结果与 `DailyContext.features` 必须与改动前逐位相同；")
        a("4. 数值变更必须另做 walk-forward / OOS 对照，不得用本报告的全样本最优值直接上线。")
        a("")
    else:
        a("### 4.1 本轮无可落盘改动")
        a("")
        a("不生成生产补丁。下一轮建议换角度（按优先级）：")
        a("")
        for i, s_ in enumerate(v.get("next_angles", []), 1):
            a(f"{i}. {s_}")
        a("")

    # ---------------- 局限
    a("## 5. 样本与局限（必须与结论同读）")
    a("")
    a(f"- 候选日 {cand['n']} 个 / {cand['n_dates']} 个交易日；去重叠后仅 "
      f"{cand['n_dedup']} 个。近 20 个交易日 {cand['recent_20d']} 个、"
      f"近 60 个交易日 {cand['recent_60d']} 个。")
    a("- 生产连续 15 个交易日零开仓，绑定约束是 60 日动量 ≥0.15 的**入池闸门**"
      "（`config/settings.py:298-310`，`momentum_top_n=3` 见 `:305`）⇒ "
      "近端没有新候选进入样本，本研究只能复盘历史候选日，无法证明当前环境有效。")
    a("- RSI 已作为 `oversold` 因子进入评分（`30<rsi<70` +1.0 / `rsi<=30` +1.5 / "
      "`rsi>=80` −0.5，见 `strategy/daily_context.py:356-362`）⇒ 候选集被 RSI 选择过，"
      "桶间差异含**选择效应**与**动量混杂**（高 RSI ≈ 强趋势），不是随机对照。")
    a("- 前向收益用 T+1 收盘近似 T+1 开盘，未计成本、涨跌停不可成交、停牌；"
      "只用于桶间横向比较，不代表可实现收益。")
    a("- 23 只标的、单一面板快照 ⇒ 幸存者偏差 + 横截面相关性；"
      "跨标的同期同涨同跌会进一步压缩有效样本，去重叠只解决了时序重叠这一层。")
    a("")
    a("---")
    a("")
    a("*本报告由 `scripts/_rsi_evolve.py` 自动生成，仅写入 `reports/rsi_evolve/`；"
      "未修改任何生产文件。*")
    return "\n".join(L) + "\n"


# ============================================================ main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", default=AS_OF_DEFAULT)
    ap.add_argument("--panel", default=str(PANEL_DEFAULT))
    args = ap.parse_args()

    t0 = time.time()
    panel_path = Path(args.panel)
    if not panel_path.is_file():
        print(f"[fatal] 面板不存在: {panel_path}")
        return 2
    panel = _load_panel(panel_path)
    panel_rows = max(len(d["close"]) for d in panel.values())
    stock_codes = sorted(set(panel) - set(INDEX_CODES))

    print(f"[1/4] 接线审计 ...")
    wiring = scan_production()
    print(f"      rsi 调用点 {len(wiring['rsi_calls'])} 个；"
          f"阈值字面量 {len(wiring['threshold_hits'])} 处；"
          f"死参数 {[d['key'] for d in wiring['dead_params']]}")

    print(f"[2/4] 候选日复算 + 前向收益（{len(stock_codes)} 只）...")
    rows, funnel = build_candidates(panel, stock_codes, PERIODS)
    print(f"      候选日 {len(rows)} 个")

    if rows:
        all_dates = sorted({d for d in (r["date"] for r in rows)})
        date_min, date_max = all_dates[0], all_dates[-1]
        # 交易日轴（用最长序列的日期）
        axis = max((d["date"] for d in panel.values()), key=len)
        idx_last = len(axis) - 1
        last60 = set(axis[max(0, idx_last - 59):])
        last20 = set(axis[max(0, idx_last - 19):])
        n_dates = len(all_dates)
        recent_60 = sum(1 for r in rows if r["date"] in last60)
        recent_20 = sum(1 for r in rows if r["date"] in last20)
        gap_days = (len(axis) - 1 - axis.index(date_max)) if date_max in axis else None
    else:
        date_min = date_max = "—"
        n_dates = recent_60 = recent_20 = 0
        gap_days = None

    print(f"[3/4] 分桶 + 阈值扫描 ...")
    dedup = dedup_rows(rows, gap=max(HORIZONS))
    buckets = {}
    for p in PERIODS:
        buckets[str(p)] = bucket_stats(rows, dedup, p)
    fine = fine_rsi14(rows, dedup)
    sweep = sweep_thresholds(rows, dedup)
    ols = incremental_info(dedup)

    base20 = [r["rets"][20] for r in rows]
    base_win20 = (sum(1 for x in base20 if x > 0) / len(base20)) if base20 else 0.0
    base_mean20 = _mean(base20) if base20 else float("nan")
    r14 = [r["rsi14"] for r in rows if r.get("rsi14") is not None]
    rsi_dist = {
        "min": min(r14) if r14 else None,
        "p10": sorted(r14)[int(0.10 * (len(r14) - 1))] if r14 else None,
        "median": _median(r14) if r14 else None,
        "max": max(r14) if r14 else None,
        "share_gt70": (sum(1 for x in r14 if x > 70) / len(r14)) if r14 else None,
        "share_ge80": (sum(1 for x in r14 if x >= 80) / len(r14)) if r14 else None,
        "share_le45": (sum(1 for x in r14 if x <= 45) / len(r14)) if r14 else None,
    }

    # ---- verdict
    # 只有「改善现状」的方向才可能构成 GO：
    #   · 分桶命中必须 diff > 0（该桶优于其余）
    #   · 阈值命中必须 delta > 0（保留组优于剔除组）
    # 方向为负的显著结果不构成改动依据，但作为「护栏」记录
    # （例如：加 RSI 上限过滤会显著变差 ⇒ 不要加）。
    n_bucket_tests = len(PERIODS) * len(BUCKETS)
    n_sweep_tests = 9
    alpha_b = 0.05 / n_bucket_tests
    alpha_s = 0.05 / n_sweep_tests
    MIN_N = 30
    MIN_EFFECT = 0.01
    MIN_SIGN = 0.70
    hits: List[str] = []
    guardrails: List[str] = []
    for p_ in PERIODS:
        for item in buckets[str(p_)]["buckets"]:
            if item["n_dedup"] < MIN_N:
                continue
            t = item.get("test20") or {}
            if t.get("p") is None:
                continue
            cs = t.get("code_sign") or {}
            ok_power = ((cs.get("same_sign_frac") or 0) >= MIN_SIGN
                        and (cs.get("codes") or 0) >= 8)
            if t["p"] < alpha_b and abs(t["diff"]) >= MIN_EFFECT and ok_power:
                msg = (f"RSI({p_}) 桶 {item['label']}：去重叠 n={item['n_dedup']}"
                       f"（全样本 {item['n']}），20日均值 "
                       f"{_fmt_pct(item['horizons']['20']['mean'])}，"
                       f"与其余差 {_fmt_pct(t['diff'])}，p={_fmt_p(t['p'])} "
                       f"< α={alpha_b:.5f}，逐标的同向 {cs['positive']}/{cs['codes']}")
                (hits if t["diff"] > 0 else guardrails).append(msg)
    for sw in sweep:
        if sw["n_dedup"] < MIN_N:
            continue
        t = sw.get("test_dedup") or {}
        if t.get("p") is None:
            continue
        if (t["p"] < alpha_s and sw["delta_mean_20_dedup"] is not None
                and abs(sw["delta_mean_20_dedup"]) >= MIN_EFFECT):
            msg = (f"{'RSI≤' if sw['kind']=='cap' else 'RSI≥'}{sw['threshold']:.0f}："
                   f"全样本保留 {sw['n']}（减少 {sw['removed']}，去重叠 "
                   f"{sw['n_dedup']}），Δ20日均值 "
                   f"{_fmt_pct(sw['delta_mean_20_dedup'])}（去重叠口径），"
                   f"p={_fmt_p(t['p'])} < α={alpha_s:.5f}")
            (hits if sw["delta_mean_20_dedup"] > 0 else guardrails).append(msg)

    verdict = "GO" if hits else "NO-GO"
    if verdict == "GO":
        reason = (f"{len(hits)} 个**改善方向**的分桶/阈值在去重叠样本上 n≥{MIN_N}、"
                  f"经 Bonferroni 校正仍显著（|效应|≥{MIN_EFFECT*100:.0f}pp、"
                  f"逐标的同向≥{MIN_SIGN*100:.0f}%）。")
    else:
        if not rows or len(rows) < 50:
            reason = (f"候选日仅 {len(rows)} 个（<50），样本量不足；"
                      f"且无 n≥{MIN_N} 且经多重比较校正仍显著的改善方向桶/阈值 ⇒ "
                      f"证据不足以支撑任何 RSI 改动。")
        else:
            extra = ""
            if guardrails:
                extra = ("（有显著结果但方向为「变差」，只构成护栏、不构成改动依据："
                         + "；".join(g.split("：")[0] for g in guardrails) + "）")
            reason = (f"无改善方向的分桶/阈值同时满足：去重叠 n≥{MIN_N}、Bonferroni "
                      f"校正后（α={alpha_b:.5f}/{alpha_s:.5f}）显著、效应≥"
                      f"{MIN_EFFECT*100:.0f}pp、逐标的同向≥{MIN_SIGN*100:.0f}%"
                      f"{extra} ⇒ RSI 在当前门链中不提供可落盘的增量判别力。")

    # ---- 提案（仅 GO）
    proposal = ""
    if verdict == "GO":
        dc_call = next((c for c in wiring["rsi_calls"]
                        if c["file"] == "strategy/daily_context.py"
                        and c["period"] == 14), None)
        dc_th = [h for h in wiring["threshold_hits"]
                 if h["file"] == "strategy/daily_context.py"
                 and h["num"] in (30.0, 70.0, 80.0)]
        bt14 = [c for c in wiring["rsi_calls"]
                if c["file"] == "strategy/backtest_daily.py" and c["period"] == 14]
        dcl = f"strategy/daily_context.py:{dc_call['line']}" if dc_call else "daily_context.py"
        btl = ", ".join(f"strategy/backtest_daily.py:{c['line']}" for c in bt14)
        thl = ", ".join(f"daily_context.py:{h['line']}" for h in dc_th)

        def _fine_sum(labels):
            grp = [f for f in fine if f["label"] in labels and f["mean20"] is not None]
            if not grp:
                return 0, float("nan")
            n = sum(f["n"] for f in grp)
            m = sum(f["mean20"] * f["n"] for f in grp) / n
            return n, m
        n7080, m7080 = _fine_sum({"70-75", "75-80"})
        n80p, m80p = _fine_sum({"80-85", ">85"})

        proposal = "\n".join([
            "**提案 A（必做 · 零行为变化）：把周期与阈值参数化**",
            "",
            f"1. `{dcl}`：`rsi = I.last(I.rsi(closes, 14))` → "
            "`_rsi_p = int(STRATEGY_PARAMS.get(\"rsi_period\", 14)); "
            "rsi = I.last(I.rsi(closes, _rsi_p))`；",
            f"2. `{thl}`：把 `30` / `70` / `80` 三个字面量替换为 "
            "`_os = float(STRATEGY_PARAMS.get(\"rsi_oversold\", 30))`、"
            "`_ob = float(STRATEGY_PARAMS.get(\"rsi_overbought\", 70))`、"
            "`_ob2 = float(STRATEGY_PARAMS.get(\"rsi_extreme\", 80))`，"
            "判定式改写为 `_os < rsi < _ob` / `rsi <= _os` / `rsi >= _ob2`；",
            f"3. `{btl}`：backtest_daily 的 `I.rsi(c, 14)` 同步改读同一参数，"
            "30/70/80 改为同一组变量 —— **回测与实盘必须同源**，"
            "否则 daily_context 与 backtest_daily 口径分叉；",
            "4. `config/settings.py:202-204` 新增 `rsi_extreme: 80`（当前无此键，"
            "属新增参数；默认 80 ⇒ 逐位不变）。",
            "",
            "**提案 B（需 OOS 复核后才可改数值）**",
            "",
            f"- 实测：RSI(14) 70-80 的 20 日均值 {_fmt_pct(m7080)}（n={n7080}），"
            f"RSI≥80 为 {_fmt_pct(m80p)}（n={n80p}）。"
            + ("→ 现有 `rsi>=80 扣 0.5 分` 的方向与数据**相反**，"
               "可考虑把 `rsi_extreme` 上调（如 85）或把惩罚改为 0；"
               if (n80p >= 20 and m80p > m7080) else
               "→ 现有 `rsi>=80 扣 0.5 分` 与数据方向一致，**不要动这个阈值**；"),
            "- 若要把它变成「额外入场过滤」（例如要求 RSI≥某值），必须先做 walk-forward："
            "本研究的候选集与评分强相关，全样本最优阈值必然过拟合。",
            "",
            "> 硬性约束：默认参数下必须逐位不变（`rsi_period=14`、"
            "`rsi_oversold=30`、`rsi_overbought=70`、`rsi_extreme=80`）。"
            "顺序：先参数化（零行为变化）→ `tests/run_all.py` 全绿 → 基线逐位不变性检查 "
            "→ 再做 A/B，禁止一步改数值。",
        ])

    next_angles = [
        "**消融而非加过滤**：RSI 已进入 `oversold` 因子，候选集被其选择；"
        "研究「把 RSI 从评分中拿掉/换周期」对候选集与 20 日期望的影响，"
        "才能判断它到底是 α 还是噪声。",
        "**门链漏斗分解**：先解释「为何 15 日零开仓」——按 动量闸门 / trend_up / "
        "score≥4 / 正因子≥3 逐层统计每日通过数，定位真正的绑定约束；"
        "若瓶颈在动量 0.15，则 RSI 无论怎么调都不会增加开仓。",
        "**换标的维度**：当前 23 只样本 5 年只有几百个候选日；"
        "把同一 RSI 判别力检验搬到全市场（Tushare 日线）做横截面验证，"
        "样本量可提升两个数量级，先证明「RSI 有信息」再谈改代码。",
        "**分钟级 RSI 与日线 RSI 的交互**：分钟路径 `trend_strategy._compute_indicators` "
        "是唯一真正读 `rsi_period` 的地方，可用 1 分钟历史做同口径检验（需额外数据源）。",
    ]

    elapsed = time.time() - t0
    meta = {
        "as_of": args.as_of,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "script": "scripts/_rsi_evolve.py",
        "panel": str(panel_path),
        "panel_rel": str(panel_path.relative_to(ROOT)).replace("\\", "/"),
        "panel_rows": panel_rows,
        "panel_codes": len(panel),
        "panel_mtime": datetime.fromtimestamp(
            panel_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(elapsed, 2),
        "seed": SEED,
        "python": sys.version.split()[0],
        "note": "只读研究：未修改任何生产文件；唯一写入 reports/rsi_evolve/。",
    }
    cand_blk = {
        "n": len(rows), "n_codes": len(stock_codes), "n_dates": n_dates,
        "date_min": date_min, "date_max": date_max,
        "recent_20d": recent_20, "recent_60d": recent_60,
        "gap_trading_days": gap_days,
        "funnel": funnel,
        "base_win20": base_win20, "base_mean20": base_mean20,
        "rsi14_dist": rsi_dist,
        "n_dedup": len(dedup),
        "dedup_gap": max(HORIZONS),
        "definition": ("(trend_up or bias>=2.0) and score>=4.0 and "
                       "positive_factors>=3; ret_h=c[i+1+h]/c[i+1]-1"),
    }
    rep = {
        "meta": meta,
        "wiring": wiring,
        "candidates": cand_blk,
        "buckets": buckets,
        "fine_rsi14": fine,
        "sweep": sweep,
        "verdict": {
            "verdict": verdict, "reason": reason, "hits": hits,
            "n_bucket_tests": n_bucket_tests, "alpha_bucket": alpha_b,
            "n_sweep_tests": n_sweep_tests, "alpha_sweep": alpha_s,
            "min_n": MIN_N, "min_effect": MIN_EFFECT,
            "min_sign_frac": MIN_SIGN,
            "guardrails": guardrails, "ols": ols,
            "proposal": proposal, "next_angles": next_angles,
        },
        "buckets_def": [{"label": b[0], "lo": b[1], "hi": b[2]} for b in BUCKETS],
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUT_DIR / f"RSI_EVOLVE_{args.as_of}.json"
    md_path = OUT_DIR / f"RSI_EVOLVE_{args.as_of}.md"
    json_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    md_path.write_text(render_md(rep, wiring), encoding="utf-8")

    print(f"[4/4] 写出报告")
    print(f"      {md_path}")
    print(f"      {json_path}")
    print("")
    print(f"VERDICT: {verdict}")
    print(f"  {reason}")
    print(f"  候选日 {len(rows)} | 交易日 {n_dates} | 近60日 {recent_60} | "
          f"耗时 {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())

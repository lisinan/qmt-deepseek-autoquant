# -*- coding: utf-8 -*-
"""历史回填「闸门卡点时间线」（一次性研究脚本，不登记 tests/run_all.py）。

★ 目的
    逐日重算七项独立通过数（above_ma20 / above_ma60 / ma20_over_ma60 /
    slope_up / macd_ok / mom_ok / score_ok）+ 四条件 trend_up，回答
    「哪条闸门整体最紧」、下跌段里哪条先失效。产出：
      - logs/gate_timeline.jsonl        （append，按 date 升序，幂等）
      - reports/GATE_TIMELINE_2026-10-23.md

★ 数据（确定性、不联网、不触发 xtdata 下载）
    复用 tests/_baseline_tushare_panel._pick_panel() 选中最新 tushare 面板
    （storage/hist_cache/ts_panel_*.pkl），再用 strategy._evolve_wf 的
    wide_universe() + aligned_universe() 对齐到统一交易日轴。**禁止**
    load_daily（会触发 xtdata 下载补拉）。

★ 七项口径（与实盘同源，逐条对齐源码）
    1. above_ma20      = close[i] > SMA20(i)
    2. above_ma60      = close[i] > SMA60(i)
    3. ma20_over_ma60  = SMA20(i) > SMA60(i)
    4. slope_up        = _vec_slope(SMA20, 5)[i] > 0
       【口径对齐 _vec_slope / daily_trend_up / daily_context._compute】：
       I.slope(ma20, lookback=5) 是最近 5 个有效值的线性斜率
       ((last-first)/(len-1))，其符号 == sma20[i] > sma20[i-4]（5 值窗口）。
       注意：任务描述写的「SMA20(i) > SMA20(i-5)」是**简化写法**，源码真实
       口径是 5 值线性斜率 > 0（i-4，不是 i-5），本脚本按源码对齐。
    5. macd_ok         = MACD 柱 hist[i] >= 0（复用 I.macd(close,12,26,9)[2]）
    6. mom_ok          = 60 日动量 > momentum_min_mom（从 STRATEGY_PARAMS 读，
                         当前 0.15；动量 = close[i]/close[i-60] - 1）
    7. score_ok        = score[i] >= buy_score_threshold 且正因子数 >= min_signals
                         （两者均从 STRATEGY_PARAMS 读；score 复用
                         backtest_daily.score_daily_series）
    trend_up           = ①②③④ 同时成立 = above_ma20 & above_ma60 &
                         ma20_over_ma60 & slope_up
    （注意：本契约的 trend_up 四条件是 ①②③④，**不含 macd_ok**；
     与源码里的两套「trend_up」都不完全等同——
       · backtest_daily.daily_trend_up = above_ma20 & ma20_over_ma60 &
                                          slope_up & macd_ok（无 above_ma60）
       · daily_context._compute.trend_up = above_ma20 & above_ma60 &
                                            slope_up & macd_ok（无 ma20_over_ma60）
     契约明确写「四条件（①②③④）」，故按契约口径执行，见报告 Caveats。）

★ 预热
    warmup = 60 根（与 daily_trend_up 的 i < 60 → False、momentum_60d 需
    lookback+1 根、SMA60 需 60 根一致）。对齐面板索引 i < 60 的日期直接跳过
    （不写行）。有效起始日 = 对齐轴第 61 个交易日（索引 60）。

★ 幂等（保留 live 行）
    整体重写 logs/gate_timeline.jsonl：先读旧文件，把 source=="live" 的行
    按 date 存入 dict；再用本脚本的 backfill 行覆盖同日；最后按 date 升序
    写出。live 行永远不被 backfill 覆盖。

★ 用法（只读，不 kill/重启任何进程、不写 storage/qmt.db、不下单）：
    $env:Path="...qmt\Library\bin;...qmt\DLLs;"+$env:Path
    $env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="."
    python scripts/_res_gate_timeline_backfill.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import strategy._evolve_wf as W          # noqa: E402
import strategy.backtest_daily as BD     # noqa: E402
from core import indicators as I         # noqa: E402
from config.settings import (            # noqa: E402
    STRATEGY_PARAMS, INDEX_CODES, MARKET_INDEX_CODE,
)

WARMUP = 60                      # 预热：前 60 根跳过（与 daily_trend_up / SMA60 一致）
COUNT = 900                      # 与 _baseline_tushare_panel._COUNT 一致
TIMELINE = ROOT / "logs" / "gate_timeline.jsonl"
OUT_MD = ROOT / "reports" / "GATE_TIMELINE_2026-10-23.md"

PASS_KEYS = ["above_ma20", "above_ma60", "ma20_over_ma60",
             "slope_up", "macd_ok", "mom_ok", "score_ok"]

# blocked_key 判定顺序（与实盘 gate_detail 同一顺序，全过 → ""）：
# 动量 → MA60 → 均线排列 → 斜率 → MACD → 评分（不含 above_ma20 / trend_up）。
BLOCKED_KEY_ORDER = ["mom_ok", "above_ma60", "ma20_over_ma60",
                     "slope_up", "macd_ok", "score_ok"]


def _fmt_date(yyyymmdd: str) -> str:
    """'20230110' -> '2023-01-10'。"""
    return f"{yyyymmdd[0:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


def build_panel() -> Tuple[List[str], List[str], Dict[str, dict]]:
    """构造对齐面板：返回 (codes, dates, panel)。

    codes = wide_universe()（23 只）；dates = 对齐交易日轴（YYYYMMDD）；
    panel[code] = {"open","high","low","close","volume","valid","date"}。
    """
    from tests._baseline_tushare_panel import _pick_panel
    raw = _pick_panel()
    codes = W.wide_universe()
    data = {}
    for c in codes:
        d = raw.get(c)
        if not d:
            raise RuntimeError(f"tushare 面板缺 {c}")
        n = len(d["close"])
        k = min(n, max(COUNT, 120))
        data[c] = {kk: list(vv[-k:]) for kk, vv in d.items()}
    dates, panel = W.aligned_universe(data, codes)
    if not dates:
        raise RuntimeError("对齐失败")
    return codes, dates, panel


def _vec_slope_series(sma20: List[float]) -> List[float]:
    """全序列 SMA20 的 5 值线性斜率（与 BD._vec_slope 同口径，返回值 None 置 0）。

    直接用 BD._vec_slope（已与 daily_trend_up 逐位一致），仅把 None 转 0.0
    便于布尔比较（None 参与比较在源码里由 `or 0` 处理为 0）。
    """
    sl = BD._vec_slope(sma20, 5)
    return [0.0 if x is None else x for x in sl]


def compute_code_series(d: dict, params: dict) -> Dict[str, List[float]]:
    """对单只标的预计算全序列指标 + 布尔数组（长度 = n）。

    一次算完，逐日 tally 时只查下标，避免 O(n²) 重算。
    """
    c = d["close"]
    h = d["high"]
    l = d["low"]
    o = d["open"]
    v = d["volume"]
    n = len(c)

    sma20 = I.sma(c, 20)
    sma60 = I.sma(c, 60)
    _, _, hist = I.macd(c, 12, 26, 9)
    slope20 = _vec_slope_series(sma20)

    buy_thr = float(params.get("buy_score_threshold", 4.0))
    min_sig = int(params.get("min_signals", 3))
    mom_min = float(params.get("momentum_min_mom", 0.0) or 0.0)
    mom_lb = int(params.get("momentum_lookback", 60))

    # score 序列（复用 backtest_daily.score_daily_series，逐位对齐实盘评分）
    scored = BD.score_daily_series(o, h, l, c, v)

    above_ma20 = [False] * n
    above_ma60 = [False] * n
    ma20_over = [False] * n
    slope_ok = [False] * n
    macd_ok = [False] * n
    mom_ok = [False] * n
    score_ok = [False] * n
    trend_ok = [False] * n
    score_series = [0.0] * n
    mom60_series = [None] * n

    for i in range(n):
        m20 = sma20[i]
        m60 = sma60[i]
        hi = hist[i]
        a = bool(m20 and c[i] > m20)
        b = bool(m60 and c[i] > m60)
        cc = bool(m20 and m60 and m20 > m60)
        dd = bool(slope20[i] > 0)
        ee = bool((hi or 0) >= 0)
        above_ma20[i] = a
        above_ma60[i] = b
        ma20_over[i] = cc
        slope_ok[i] = dd
        macd_ok[i] = ee

        # 60 日动量 = close[i]/close[i-mom_lb] - 1
        if i >= mom_lb and c[i - mom_lb] > 0:
            mom60 = c[i] / c[i - mom_lb] - 1.0
            mom60_series[i] = mom60
            mom_ok[i] = mom60 > mom_min
        else:
            mom60_series[i] = None
            mom_ok[i] = False

        sc, fac = scored[i]
        score_series[i] = sc
        posf = sum(1 for x in fac.values() if x and x > 0)
        score_ok[i] = (sc >= buy_thr and posf >= min_sig)

        # 契约 trend_up = ①②③④ = above_ma20 & above_ma60 & ma20_over & slope
        trend_ok[i] = a and b and cc and dd

    return {
        "above_ma20": above_ma20, "above_ma60": above_ma60,
        "ma20_over_ma60": ma20_over, "slope_up": slope_ok,
        "macd_ok": macd_ok, "mom_ok": mom_ok, "score_ok": score_ok,
        "trend_up": trend_ok, "score": score_series, "mom60": mom60_series,
    }


def _load_live_rows() -> Dict[str, dict]:
    """读旧 timeline，返回 date -> 行（仅 source=='live' 的行被保留）。"""
    out: Dict[str, dict] = {}
    if not TIMELINE.exists():
        return out
    for line in TIMELINE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("source") == "live":
            out[obj.get("date")] = obj
    return out


def build_rows(codes, dates, panel, params) -> List[dict]:
    """逐日 tally 七项独立通过数 + trend_up，返回按 date 升序的行列表。

    预热（i < WARMUP）直接跳过。universe_n = 当日 valid==True 的标的数
    （停牌前向填充日不计入有效标的）。
    """
    series = {c: compute_code_series(panel[c], params) for c in codes}
    n = len(dates)
    rows: List[dict] = []
    for i in range(WARMUP, n):
        # universe_n = 当日有真实交易（非停牌）的标的数
        uni = 0
        for c in codes:
            if panel[c].get("valid", [True] * n)[i]:
                uni += 1
        if uni == 0:
            continue
        tally = {k: 0 for k in PASS_KEYS}
        trend_n = 0
        all_pass_n = 0
        # 当日最强候选：passed_n 降序 → score 降序 → mom60 降序，取第一名
        best = None
        for c in codes:
            s = series[c]
            if not panel[c].get("valid", [True] * n)[i]:
                continue
            for k in PASS_KEYS:
                if s[k][i]:
                    tally[k] += 1
            if s["trend_up"][i]:
                trend_n += 1
            # 该票 7 个独立布尔的通过数（与 tally 同口径）
            cand_n = sum(1 for k in PASS_KEYS if s[k][i])
            if cand_n == len(PASS_KEYS):
                all_pass_n += 1        # 单独"够格"；组合闸门还要 ≥2 只
            cand_score = s["score"][i]
            cand_mom = s["mom60"][i]
            if best is None or (
                cand_n, cand_score, (cand_mom if cand_mom is not None else -9.9)
            ) > (
                best["passed_n"], best["score"],
                (best["mom60"] if best["mom60"] is not None else -9.9)
            ):
                # 找第一个失败（BLOCKED_KEY_ORDER 顺序），全过 → ""
                bkey = ""
                for k in BLOCKED_KEY_ORDER:
                    if not s[k][i]:
                        bkey = k
                        break
                best = {
                    "code": c, "passed_n": cand_n, "blocked_key": bkey,
                    "score": cand_score, "mom60": cand_mom,
                }
        rows.append({
            "date": _fmt_date(dates[i]),
            "universe_n": uni,
            "pass": tally,
            "trend_up": trend_n,
            "all_pass_n": all_pass_n,
            "best": best,
            "source": "backfill",
        })
    return rows


def write_timeline(rows: List[dict]) -> None:
    """整体重写 timeline：live 行按 date 保留，backfill 行覆盖同日，按 date 升序。"""
    live = _load_live_rows()
    merged: Dict[str, dict] = dict(live)
    for r in rows:
        merged[r["date"]] = r          # backfill 覆盖同日（不含 live，live 已在上方）
    ordered = sorted(merged.keys())
    TIMELINE.parent.mkdir(parents=True, exist_ok=True)
    with TIMELINE.open("w", encoding="utf-8") as f:
        for dt in ordered:
            f.write(json.dumps(merged[dt], ensure_ascii=False) + "\n")


# ============================================================ 报告

def _rates_series(rows: List[dict], key: str) -> List[float]:
    """返回某条件的逐日通过率序列（pass[key] / universe_n）。

    key=="trend_up" 是顶层字段（r["trend_up"]），其余在 r["pass"][key]。
    """
    out = []
    for r in rows:
        uni = r["universe_n"]
        if uni <= 0:
            continue
        if key == "trend_up":
            out.append(r["trend_up"] / uni)
        else:
            out.append(r["pass"][key] / uni)
    return out


def _median(xs: List[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[len(s) // 2]


def _mean(xs: List[float]) -> float:
    if not xs:
        return 0.0
    return sum(xs) / len(xs)


def _index_regime(panel, codes, dates) -> List[int]:
    """用宇宙等权合成指数近似市场涨跌阶段（面板无指数代码 399006.SZ）。

    对每日取 universe 的等权平均收盘，算 SMA20；+1 = 上涨段（合成价 > MA20
    且 MA20 斜率 > 0）、-1 = 下跌段（合成价 <= MA20）、0 = 中性。
    返回与 dates 等长的阶段标签。
    """
    n = len(dates)
    eqw = []
    for i in range(n):
        vals = []
        for c in codes:
            try:
                v = panel[c]["close"][i]
            except (KeyError, IndexError):
                continue
            if v and v > 0 and panel[c].get("valid", [True] * n)[i]:
                vals.append(v)
        eqw.append(sum(vals) / len(vals) if vals else 0.0)
    ma20 = I.sma(eqw, 20)
    slope = BD._vec_slope(ma20, 5)
    regime = [0] * n
    for i in range(n):
        m20 = ma20[i]
        if i < WARMUP or not m20:
            regime[i] = 0
            continue
        sl = slope[i] if slope[i] is not None else 0.0
        if eqw[i] > m20 and sl > 0:
            regime[i] = 1
        elif eqw[i] <= m20:
            regime[i] = -1
        else:
            regime[i] = 0
    return regime


def _bottleneck_line(rows: List[dict]) -> str:
    """找长期通过率最低的条件（按整段均值）。"""
    means = {}
    for k in PASS_KEYS:
        means[k] = _mean(_rates_series(rows, k))
    worst = min(means, key=means.get)
    return worst, means


def _down_phase_first_fail(rows, panel, codes, dates) -> str:
    """下跌段里「最先失效」的条件：比较下跌段各条件通过率相对上涨段的降幅。"""
    regime = _index_regime(panel, codes, dates)
    # 按阶段聚合通过率
    up = {k: [] for k in PASS_KEYS}
    dn = {k: [] for k in PASS_KEYS}
    for idx, r in enumerate(rows):
        # rows 的索引 i 对应 dates 的 i（因从 WARMUP 起连续）
        i = WARMUP + idx
        rg = regime[i]
        uni = r["universe_n"]
        if uni <= 0:
            continue
        for k in PASS_KEYS:
            rate = r["pass"][k] / uni
            if rg == 1:
                up[k].append(rate)
            elif rg == -1:
                dn[k].append(rate)
    mu_up = {k: _mean(v) if v else 0.0 for k, v in up.items()}
    mu_dn = {k: _mean(v) if v else 0.0 for k, v in dn.items()}
    # 降幅 = up - down（正 = 下跌段跌得多），取降幅最大者 = 最先失效
    drop = {k: mu_up[k] - mu_dn[k] for k in PASS_KEYS}
    first = max(drop, key=drop.get)
    return first, drop, mu_up, mu_dn


def _write_md(rows, codes, dates, panel, params, n_bars):
    L: List[str] = []
    A = L.append
    n_rows = len(rows)
    start = rows[0]["date"] if rows else "?"
    end = rows[-1]["date"] if rows else "?"

    worst, means = _bottleneck_line(rows)
    first_fail, drop, mu_up, mu_dn = _down_phase_first_fail(
        rows, panel, codes, dates)

    A("# 闸门卡点时间线（历史回填）")
    A("")
    A(f"> 数据：确定性 tushare 面板，{len(codes)} 只（wide_universe）× "
      f"{n_bars} 根对齐日线；有效区间 {start} → {end}（预热跳过前 {WARMUP} 根）。")
    A("")
    A("## 结论摘要（≤5 行）")
    A("")
    A(f"1. **长期瓶颈 = `{worst}`**：全样本通过率均值 "
      f"{means[worst]*100:.1f}%（七条件里最低），其次 "
      f"`{min((k for k in PASS_KEYS if k != worst), key=means.get)}` "
      f"{means[min((k for k in PASS_KEYS if k != worst), key=means.get)]*100:.1f}%。")
    A(f"2. 四条件 `trend_up`（①②③④ 同时成立）通过率均值 "
      f"{_mean(_rates_series(rows,'trend_up'))*100:.1f}%，"
      f"中位 {_median(_rates_series(rows,'trend_up'))*100:.1f}%。")
    A(f"3. 下跌段最先失效的是 `{first_fail}`（上涨段通过率 "
      f"{mu_up[first_fail]*100:.1f}% → 下跌段 {mu_dn[first_fail]*100:.1f}%，"
      f"降幅 {drop[first_fail]*100:.1f}pt）。")
    A(f"4. 最近 60 日均值：`{worst}` "
      f"{_mean(_rates_series(rows,worst)[-60:])*100:.1f}%；"
      f"`trend_up` {_mean(_rates_series(rows,'trend_up')[-60:])*100:.1f}%。")
    A(f"5. 产出 {n_rows} 行（{start} → {end}），已幂等写入 "
      f"`logs/gate_timeline.jsonl`（live 行保留、backfill 覆盖同日）。")
    A("")

    # ---- 通过率分布表 ----
    A("## 1. 各条件历史通过率分布")
    A("")
    A("| 条件 | 中位 | 最低 | 最近60日均值 | 全样本均值 |")
    A("|---|---|---|---|---|")
    for k in PASS_KEYS:
        rs = _rates_series(rows, k)
        last60 = rs[-60:]
        A(f"| {k} | {_median(rs)*100:.1f}% | {min(rs)*100:.1f}% | "
          f"{_mean(last60)*100:.1f}% | {_mean(rs)*100:.1f}% |")
    rs_t = _rates_series(rows, "trend_up")
    A(f"| **trend_up（四条件）** | {_median(rs_t)*100:.1f}% | "
      f"{min(rs_t)*100:.1f}% | {_mean(rs_t[-60:])*100:.1f}% | "
      f"{_mean(rs_t)*100:.1f}% |")
    A("")
    A("> 通过率 = 当日该条件通过标的数 / 当日有效标的数（universe_n）。")
    A("")

    # ---- 涨/跌拆分 ----
    A("## 2. 指数阶段（涨/跌）拆分")
    A("")
    A("> 阶段标签用宇宙等权合成收盘价 + SMA20 近似（面板不含指数 399006.SZ）："
      "上涨 = 合成价 > MA20 且 MA20 斜率 > 0；下跌 = 合成价 <= MA20。")
    A("")
    A("| 条件 | 上涨段通过率 | 下跌段通过率 | 降幅 |")
    A("|---|---|---|---|")
    for k in PASS_KEYS:
        A(f"| {k} | {mu_up[k]*100:.1f}% | {mu_dn[k]*100:.1f}% | "
          f"{drop[k]*100:.1f}pt |")
    A("")
    A(f"**下跌段最先失效：`{first_fail}`**（降幅 {drop[first_fail]*100:.1f}pt）。")
    A("")

    # ---- 最近 30 日逐日表 ----
    A("## 3. 最近 30 日逐日表")
    A("")
    A("| date | universe_n | trend_up | above_ma20 | above_ma60 | "
      "ma20_over_ma60 | slope_up | macd_ok | mom_ok | score_ok |")
    A("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows[-30:]:
        p = r["pass"]
        A(f"| {r['date']} | {r['universe_n']} | {r['trend_up']} | "
          f"{p['above_ma20']} | {p['above_ma60']} | {p['ma20_over_ma60']} | "
          f"{p['slope_up']} | {p['macd_ok']} | {p['mom_ok']} | {p['score_ok']} |")
    A("")

    # ---- 口径与 Caveats ----
    A("## 4. 口径与 Caveats")
    A("")
    A("- **slope_up** 按源码 `I.slope(ma20, lookback=5)`（5 值线性斜率 > 0，"
      "符号 == `sma20[i] > sma20[i-4]`），不是任务描述里简写的 "
      "`sma20[i] > sma20[i-5]`（那会差一根 bar）。")
    A("- **trend_up 四条件** = above_ma20 & above_ma60 & ma20_over_ma60 & "
      "slope_up（契约 ①②③④，**不含 macd_ok**）。这与源码两套 trend_up "
      "（`daily_trend_up` 用 ma20_over_ma60+macd、`daily_context._compute` 用 "
      "above_ma60+macd）都不完全等同——按契约口径执行。")
    A("- `mom_ok` 阈值 = `momentum_min_mom`="
      f"{params.get('momentum_min_mom', 0.0)}；`score_ok` 阈值 = "
      f"`buy_score_threshold`={params.get('buy_score_threshold',4.0)}、"
      f"`min_signals`={params.get('min_signals',3)}（均从 STRATEGY_PARAMS 读，未硬编码）。")
    A("- 市场涨跌阶段用宇宙等权合成价近似（面板无 399006.SZ 指数），"
      "仅用于定性「下跌段哪条先失效」，非精确指数择时。")
    A("- 停牌日（valid=False）不计入 universe_n 与各项通过数。")
    A("")
    A("## 5. 复现")
    A("")
    A("```")
    A("python scripts/_res_gate_timeline_backfill.py")
    A("```")
    A("")
    A(f"确定性 tushare 面板：`storage/hist_cache/ts_panel_*.pkl`（`_pick_panel` "
      f"选中 {len(codes)} 只），无随机种子，数据区间 {start} → {end}。")
    A("")

    OUT_MD.write_text("\n".join(L), encoding="utf-8")


def main():
    t0 = time.time()
    params = dict(STRATEGY_PARAMS)
    codes, dates, panel = build_panel()
    n = len(dates)
    print(f"[数据] {len(codes)} 只 × {n} 根  {_fmt_date(dates[0])} -> "
          f"{_fmt_date(dates[-1])}")

    rows = build_rows(codes, dates, panel, params)
    print(f"[计算] 有效行 {len(rows)}（跳过前 {WARMUP} 根预热）")

    write_timeline(rows)
    print(f"[写] {TIMELINE}")

    _write_md(rows, codes, dates, panel, params, n)
    print(f"[写] {OUT_MD}")
    print(f"[耗时] {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()

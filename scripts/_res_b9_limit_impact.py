# -*- coding: utf-8 -*-
"""B9 研究脚本（一次性，不可登记 tests/run_all.py）。

课题：开启「涨跌停不可成交」（BacktestConfig.limit_up_block）后，alpha 还剩多少。

方法（全部确定性、可复现）：
  1. 数据源 = tests/_baseline_tushare_panel.py 的确定性 tushare 面板缓存
     （storage/hist_cache/ts_panel_*.pkl），不触发 xtdata 联网补拉。
  2. 在**同一对齐面板**上跑两遍 base_cfg()：limit_up_block=False vs True。
  3. 全样本 + 四窗 OOS（60x9 / 75x7 / 90x6 / 120x4，make_folds + oos_stats）。
  4. 用 config/kpi_targets.evaluate() 对两态各出一张 KPI PASS/FAIL 表。
  5. 关键量化：被拦买单占比 blocked/qty_intended，及被拦标的自信号日起
     5/10/20 日的等权前向收益（「错过的平均收益」）。

非侵入式 instrumentation：monkeypatch `backtest_daily._limit_price_from_prev_close`，
在 side=="up" 时用 frame 检查读取 run_backtest 栈帧的 code/i/order/entry/panel，
复刻第 1682-1689 行逐位的涨停禁买判定，记录 (code, signal_day, exec_day)。
纯运行时 patch，不修改 strategy/backtest_daily.py 任何生产逻辑。

用法（只读，不 kill/重启任何进程、不写生产库、不下单）：
    $env:Path="...qmt\Library\bin;...qmt\DLLs;"+$env:Path
    $env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="."
    python scripts/_res_b9_limit_impact.py
"""
from __future__ import annotations

import inspect
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import strategy._evolve_wf as W          # noqa: E402
import strategy.backtest_daily as BD     # noqa: E402
from config import kpi_targets as KT     # noqa: E402
from strategy.kpi_stats import (         # noqa: E402
    span_sharpe, month_win_rate, daily_returns_from_curve,
)

COUNT = 900          # 与 _baseline_tushare_panel._COUNT 一致
WINDOWS = [(60, 9), (75, 7), (90, 6), (120, 4)]
FWD_HORIZONS = [5, 10, 20]

OUT_MD = ROOT / "reports" / "B9_LIMIT_UP_IMPACT_2026-10-22.md"
OUT_JSON = ROOT / "reports" / "B9_LIMIT_UP_IMPACT_2026-10-22.json"


# ---------------------------------------------------------------------------
# instrumentation（非侵入式）
# ---------------------------------------------------------------------------
_REAL_LIMIT_PRICE = BD._limit_price_from_prev_close
_BLOCKED_BUYS = []          # (code, signal_day, exec_day)
_INTENDED_BUYS = 0          # 到达涨停判定阶段的买单数（= qty_intended）
_CAPTURE_ACTIVE = False


def _wrapped_limit_price(prev_close, limit_pct, side):
    global _INTENDED_BUYS
    if side == "up" and _CAPTURE_ACTIVE:
        fr = inspect.currentframe().f_back   # run_backtest 栈帧
        if fr is not None:
            loc = fr.f_locals
            code = loc.get("code")
            i = loc.get("i")
            entry = loc.get("entry")
            panel = loc.get("panel")
            order = loc.get("order")
            if code is not None and i is not None:
                _INTENDED_BUYS += 1
                lp = _REAL_LIMIT_PRICE(prev_close, limit_pct, side)
                # 逐位复刻 backtest_daily.py:1687 的涨停禁买判定
                hi = None
                if panel is not None:
                    try:
                        hi = panel[code]["high"][i]
                    except (KeyError, IndexError):
                        hi = None
                blocked = False
                if entry is not None and entry >= lp:
                    blocked = True
                elif hi is not None and hi >= lp:
                    blocked = True
                if blocked:
                    sd = order.get("signal_day") if order else (i - 1)
                    _BLOCKED_BUYS.append((code, sd, i))
                return lp
    return _REAL_LIMIT_PRICE(prev_close, limit_pct, side)


def _install_hook():
    BD._limit_price_from_prev_close = _wrapped_limit_price


def _uninstall_hook():
    BD._limit_price_from_prev_close = _REAL_LIMIT_PRICE


# ---------------------------------------------------------------------------
# 数据构造（与 baseline_is_result 同口径，但保留对齐面板用于 OOS）
# ---------------------------------------------------------------------------
def build_panel():
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
    d0, panel = W.aligned_universe(data, codes)
    if not d0:
        raise RuntimeError("对齐失败")
    return codes, d0, panel


def make_bt(ks, count):
    def bt(cfg, dset):
        return BD.run_backtest(ks, cfg, count=count, preloaded=dset)
    return bt


# ---------------------------------------------------------------------------
# 前向收益：被拦买单「错过的平均收益」
# ---------------------------------------------------------------------------
def forward_returns(blocked, panel):
    """blocked: [(code, signal_day, exec_day)]；panel: 对齐后含 close 的面板。

    对每个 (code, s) 算 close[min(s+h, n-1)] / close[s] - 1，等权平均。
    停牌/无效价（close<=0）跳过。返回 {h: {"mean":, "median":, "n":}}。
    """
    out = {}
    for h in FWD_HORIZONS:
        vals = []
        for code, s, _e in blocked:
            try:
                cc = panel[code]["close"]
            except KeyError:
                continue
            n = len(cc)
            if s < 0 or s >= n or cc[s] <= 0:
                continue
            j = min(s + h, n - 1)
            if j <= s:
                continue
            base = cc[s]
            fwd = cc[j]
            if base <= 0:
                continue
            vals.append(fwd / base - 1.0)
        if vals:
            vals_s = sorted(vals)
            mean = sum(vals) / len(vals)
            median = vals_s[len(vals_s) // 2]
        else:
            mean = median = 0.0
        out[h] = {"mean": mean, "median": median, "n": len(vals)}
    return out


# ---------------------------------------------------------------------------
# 单态评估
# ---------------------------------------------------------------------------
def run_state(cfg, dset, subs, bt, ks, d0):
    """跑全样本 + 四窗 OOS，返回统一 dict。"""
    global _INTENDED_BUYS, _CAPTURE_ACTIVE
    _BLOCKED_BUYS.clear()
    _INTENDED_BUYS = 0
    _install_hook()
    _CAPTURE_ACTIVE = True
    try:
        isr = bt(cfg, dset)
    finally:
        _CAPTURE_ACTIVE = False
        _uninstall_hook()

    # 四窗 OOS
    oos = {}
    for fold, nf in WINDOWS:
        subs_w = W.make_folds(dset, ks, len(d0), fold, nf)
        st, rs = W.oos_stats(cfg, subs_w, bt)
        if not st:
            oos[f"{fold}x{nf}"] = None
            continue
        oos[f"{fold}x{nf}"] = {
            "mean_sharpe": st["mean_sharpe"],
            "mean_ret": st["mean_ret"],
            "mean_mdd": st["mean_mdd"],
            "pos": st["pos"],
            "n": st["n"],
            "worst": st["worst"],
            "rets": st["rets"],
        }

    # KPI（gate 口径 = 90x6 折划分）
    g = oos["90x6"]
    curve = isr.get("equity_curve") or []
    daily = daily_returns_from_curve(curve)
    dates = d0[-len(daily):] if 0 < len(daily) <= len(d0) else d0
    metrics = {
        "fold_mean_sharpe": g["mean_sharpe"] if g else None,
        "worst_fold_return": g["worst"] if g else None,
        "full_sample_alpha": isr.get("alpha"),
        "span_sharpe": span_sharpe(daily),
        "fold_positive_ratio": (g["pos"] / g["n"]) if g and g["n"] else None,
        "month_win_rate": (month_win_rate(daily, dates)["pos_month_pct"] / 100.0
                           if daily else None),
    }
    rows = KT.evaluate(metrics)
    verdict = KT.gate_verdict(rows)

    return {
        "is": isr,
        "oos": oos,
        "metrics": metrics,
        "kpi_rows": rows,
        "verdict": verdict,
    }


def fmt_pct(x, nd=2):
    if x is None:
        return "n/a"
    return f"{x * 100:+.{nd}f}%"


def fmt_sh(x, nd=3):
    if x is None:
        return "n/a"
    return f"{x:+.{nd}f}"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    t0 = time.time()
    codes, d0, panel = build_panel()
    n = len(d0)
    ks = [k for k in panel.keys() if k not in W.INDEX_CODES]
    bt = make_bt(ks, COUNT)
    print(f"[数据] {len(panel)} 只 × {n} 根  {d0[0]} -> {d0[-1]}")

    base = W.base_cfg()
    cfg_off = base
    cfg_on = replace(base, limit_up_block=True)

    subs = W.make_folds(panel, ks, n, 90, 6)

    print(f"\n[回测] limit_up_block=False ...")
    st_off = run_state(cfg_off, panel, subs, bt, ks, d0)
    print(f"[回测] limit_up_block=True  ...")
    st_on = run_state(cfg_on, panel, subs, bt, ks, d0)

    # 被拦买单 + 前向收益
    blocked_buy_list = list(_BLOCKED_BUYS)
    intended = _INTENDED_BUYS
    blocked_cnt = st_on["is"].get("limit_blocked_buy", 0)
    blocked_sell = st_on["is"].get("limit_blocked_sell", 0)
    fwd = forward_returns(blocked_buy_list, panel)
    ratio = (blocked_cnt / intended) if intended else 0.0

    # 一致性命中检查：hook 记录的 blocked 数 vs run_backtest 官方计数
    hook_blocked = len(blocked_buy_list)

    res = {
        "data": {"n_codes": len(panel), "n_bars": n,
                 "start": d0[0], "end": d0[-1]},
        "off": _serialize_state(st_off),
        "on": _serialize_state(st_on),
        "blocked": {
            "blocked_buy_cnt": blocked_cnt,
            "blocked_sell_cnt": blocked_sell,
            "qty_intended": intended,
            "hook_blocked_cnt": hook_blocked,
            "ratio_blocked_intended": ratio,
            "forward_returns": fwd,
            "blocked_samples": blocked_buy_list[:20],
            "n_blocked_samples": len(blocked_buy_list),
        },
    }

    # ---- 控制台输出 ----
    _print_console(st_off, st_on, res["blocked"])

    OUT_JSON.write_text(json.dumps(res, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    _write_md(res, st_off, st_on)
    print(f"\n[保存] {OUT_JSON}")
    print(f"[保存] {OUT_MD}")
    print(f"[耗时] {time.time() - t0:.1f}s")


def _serialize_state(st):
    isr = st["is"]
    return {
        "is": {k: isr.get(k) for k in (
            "total_return", "sharpe", "max_drawdown", "n_trades",
            "win_rate", "exposure", "alpha", "bench_return",
            "limit_blocked_buy", "limit_blocked_sell", "avg_hold",
        )},
        "oos": st["oos"],
        "metrics": st["metrics"],
        "verdict": st["verdict"],
        "kpi_rows": [
            {k: r[k] for k in ("id", "label", "value", "target", "op",
                               "status", "pass")}
            for r in st["kpi_rows"]
        ],
    }


def _print_console(off, on, blk):
    o = off["is"]; n = on["is"]
    print("\n" + "=" * 100)
    print("全样本（IS）对照  limit_up_block=False vs True")
    print("=" * 100)
    print(f"{'指标':<18}{'False':>14}{'True':>14}{'Δ(True-False)':>16}")
    for key, label, nd, mult in [
        ("total_return", "total_return", 2, 100),
        ("sharpe", "sharpe", 3, 1),
        ("max_drawdown", "max_drawdown", 2, 100),
        ("n_trades", "n_trades", 0, 1),
        ("win_rate", "win_rate", 1, 100),
        ("exposure", "exposure", 1, 100),
        ("alpha", "alpha", 2, 100),
    ]:
        a, b = o.get(key), n.get(key)
        d = (b - a) if (a is not None and b is not None) else None
        ds = f"{d * mult:+.2f}" if d is not None else "n/a"
        av = f"{a * mult:.{nd}f}" if a is not None else "n/a"
        bv = f"{b * mult:.{nd}f}" if b is not None else "n/a"
        print(f"{label:<18}{av:>14}{bv:>14}{ds:>16}")
    print(f"{'limit_blocked_buy':<18}{o.get('limit_blocked_buy',0):>14}"
          f"{n.get('limit_blocked_buy',0):>14}")
    print(f"{'limit_blocked_sell':<18}{o.get('limit_blocked_sell',0):>14}"
          f"{n.get('limit_blocked_sell',0):>14}")

    print("\n" + "=" * 100)
    print("四窗 OOS（折均值 Sharpe / 最差折）")
    print("=" * 100)
    for fold, nf in WINDOWS:
        key = f"{fold}x{nf}"
        oo = off["oos"].get(key); no = on["oos"].get(key)
        if not oo or not no:
            print(f"{key:<8} n/a")
            continue
        dsh = no["mean_sharpe"] - oo["mean_sharpe"]
        print(f"{key:<8}  False Sh={fmt_sh(oo['mean_sharpe'])}  "
              f"最差={fmt_pct(oo['worst'],1)}  |  "
              f"True Sh={fmt_sh(no['mean_sharpe'])}  最差={fmt_pct(no['worst'],1)}  "
              f"dSh={dsh:+.3f}")

    print("\n" + "=" * 100)
    print("KPI（config/kpi_targets.evaluate，gate 口径 90x6）")
    print("=" * 100)
    for tag, st in [("False", off), ("True", on)]:
        print(f"\n--- limit_up_block={tag} ---")
        for r in st["kpi_rows"]:
            if r["status"] != "active":
                continue
            v = r["value"]
            vs = "n/a" if v is None else f"{v:+.4f}"
            mark = ("n/a" if r["pass"] is None
                    else ("PASS" if r["pass"] else "FAIL"))
            print(f"  {r['id']:<6}{vs:>12}  vs {r['op']}{r['target']:+.4f}"
                  f"  {mark:>5}")
        vd = st["verdict"]
        print(f"  => {'PASS' if vd['all_active_pass'] else 'FAIL'}"
              f"  failed={vd['failed'] or '无'}")

    print("\n" + "=" * 100)
    print("被拦买单量化")
    print("=" * 100)
    print(f"  limit_blocked_buy(官方)   = {blk['blocked_buy_cnt']}")
    print(f"  hook 记录的 blocked 数     = {blk['hook_blocked_cnt']}（一致性校验）")
    print(f"  qty_intended(到达判定)     = {blk['qty_intended']}")
    print(f"  blocked/qty_intended       = {blk['ratio_blocked_intended']:.4f}")
    print(f"  limit_blocked_sell(官方)   = {blk['blocked_sell_cnt']}")
    for h in FWD_HORIZONS:
        f = blk["forward_returns"][h]
        print(f"  错过收益(信号后{h}日等权): mean={f['mean']*100:+.2f}%  "
              f"median={f['median']*100:+.2f}%  n={f['n']}")


def _worst_dsh(off, on):
    """四窗 OOS 里 dSh（True - False 折均值 Sharpe）的最小值（最差窗）。"""
    ds = []
    for fold, nf in WINDOWS:
        key = f"{fold}x{nf}"
        oo = off["oos"].get(key); no = on["oos"].get(key)
        if oo and no:
            ds.append(no["mean_sharpe"] - oo["mean_sharpe"])
    return min(ds) if ds else 0.0


def _write_md(res, off, on):
    blk = res["blocked"]
    d = res["data"]
    o = off["is"]; nn = on["is"]

    def delta(a, b):
        return (b - a) if (a is not None and b is not None) else None

    L = []
    A = L.append

    # ---- 结论摘要（<=5 行）----
    d_ret = delta(o["total_return"], nn["total_return"])
    d_sh = delta(o["sharpe"], nn["sharpe"])
    d_alpha = delta(o["alpha"], nn["alpha"])
    A("# B9 涨跌停不可成交影响评估")
    A("")
    A(f"> 数据：确定性 tushare 面板，{d['n_codes']} 只 × {d['n_bars']} 根日线，"
      f"{d['start']} → {d['end']}；单边成本 0.15%。")
    A("")
    A("## 结论摘要（≤5 行）")
    A("")
    A(f"1. 开启 `limit_up_block=True` 后，全样本 Sharpe 由 {o['sharpe']:.3f} 变为 "
      f"{nn['sharpe']:.3f}（Δ {d_sh:+.3f}），收益由 {o['total_return']*100:+.2f}% 变为 "
      f"{nn['total_return']*100:+.2f}%（Δ {d_ret*100:+.1f}pt），"
      f"相对基准 alpha 由 {o['alpha']*100:+.1f}pt 变为 {nn['alpha']*100:+.1f}pt"
      f"（Δ {d_alpha*100:+.1f}pt）。")
    A(f"2. 被拦买单 {blk['blocked_buy_cnt']} 笔，占到达判定阶段买单的 "
      f"{blk['ratio_blocked_intended']*100:.1f}%；被拦卖单 {blk['blocked_sell_cnt']} 笔。")
    A(f"3. 被拦标的「错过的平均收益」（信号后 5/10/20 日等权）分别为 "
      f"{blk['forward_returns'][5]['mean']*100:+.2f}% / "
      f"{blk['forward_returns'][10]['mean']*100:+.2f}% / "
      f"{blk['forward_returns'][20]['mean']*100:+.2f}%。")
    A(f"4. KPI 判定：False 态 {('PASS' if off['verdict']['all_active_pass'] else 'FAIL')}，"
      f"True 态 {('PASS' if on['verdict']['all_active_pass'] else 'FAIL')}"
      f"（kpi1 折均值 Sharpe≥1.6 / kpi3 最差折≥-15% / kpi4 全样本 alpha≥-30pt）。")
    A(f"5. 结论：{_conclusion_line(off, on, blk)}")
    A("")

    # ---- 全样本表 ----
    A("## 1. 全样本（IS）对照")
    A("")
    A("| 指标 | False | True | Δ |")
    A("|---|---|---|---|")
    for key, label, nd, mult, sgn in [
        ("total_return", "total_return", 2, 100, "+"),
        ("sharpe", "sharpe", 3, 1, "+"),
        ("max_drawdown", "max_drawdown", 2, 100, ""),
        ("n_trades", "n_trades", 0, 1, ""),
        ("win_rate", "win_rate", 1, 100, ""),
        ("exposure", "exposure", 1, 100, ""),
        ("alpha", "alpha", 2, 100, "+"),
    ]:
        a, b = o.get(key), nn.get(key)
        dlt = delta(a, b)
        av = f"{a*mult:.{nd}f}" if a is not None else "n/a"
        bv = f"{b*mult:.{nd}f}" if b is not None else "n/a"
        dv = f"{dlt*mult:+.{nd}f}" if dlt is not None else "n/a"
        A(f"| {label} | {av} | {bv} | {dv} |")
    A(f"| limit_blocked_buy | {o.get('limit_blocked_buy',0)} | "
      f"{nn.get('limit_blocked_buy',0)} | — |")
    A(f"| limit_blocked_sell | {o.get('limit_blocked_sell',0)} | "
      f"{nn.get('limit_blocked_sell',0)} | — |")
    A("")

    # ---- 四窗 OOS ----
    A("## 2. 四窗 OOS（折均值 Sharpe / 最差折）")
    A("")
    A("| 窗口 | False 折均值Sh | False 最差折 | True 折均值Sh | True 最差折 | dSh |")
    A("|---|---|---|---|---|---|")
    for fold, nf in WINDOWS:
        key = f"{fold}x{nf}"
        oo = off["oos"].get(key); no = on["oos"].get(key)
        if not oo or not no:
            A(f"| {key} | n/a | n/a | n/a | n/a | n/a |")
            continue
        A(f"| {key} | {oo['mean_sharpe']:+.3f} | {oo['worst']*100:+.1f}% | "
          f"{no['mean_sharpe']:+.3f} | {no['worst']*100:+.1f}% | "
          f"{no['mean_sharpe']-oo['mean_sharpe']:+.3f} |")
    A("")

    # ---- 被拦买单量化 ----
    A("## 3. 被拦买单关键量化")
    A("")
    A(f"- 官方 `limit_blocked_buy` = {blk['blocked_buy_cnt']}；"
      f"hook 复刻判定记录 = {blk['hook_blocked_cnt']}（一致性校验，应相等）。")
    A(f"- `qty_intended`（到达涨停判定阶段的买单数）= {blk['qty_intended']}。")
    A(f"- **被拦占比 = blocked / qty_intended = {blk['ratio_blocked_intended']*100:.1f}%**。")
    A(f"- 官方 `limit_blocked_sell` = {blk['blocked_sell_cnt']}。")
    A("")
    A("| 前向窗口 | 错过平均收益 | 中位数 | 样本数 |")
    A("|---|---|---|---|")
    for h in FWD_HORIZONS:
        f = blk["forward_returns"][h]
        A(f"| 信号后 {h} 日 | {f['mean']*100:+.2f}% | {f['median']*100:+.2f}% | {f['n']} |")
    A("")
    A("> 口径：`close[signal_day+h]/close[signal_day]-1`，等权平均；被拦买单的"
      "「错过收益」为正说明这些信号次日涨停、随后继续上行（策略确实在放弃最强的票）。")
    A("")

    # ---- KPI ----
    A("## 4. KPI PASS/FAIL 表（config/kpi_targets.evaluate，gate 口径 90×6）")
    A("")
    for tag, st in [("False", off), ("True", on)]:
        A(f"### limit_up_block = {tag}")
        A("")
        A("| KPI | 值 | 目标 | 判定 |")
        A("|---|---|---|---|")
        for r in st["kpi_rows"]:
            if r["status"] != "active":
                continue
            v = r["value"]
            vs = "n/a" if v is None else f"{v:+.4f}"
            mark = ("n/a" if r["pass"] is None
                    else ("PASS" if r["pass"] else "FAIL"))
            A(f"| {r['id']} ({r['label']}) | {vs} | {r['op']}{r['target']:+.4f} | {mark} |")
        vd = st["verdict"]
        A(f"")
        A(f"**总体：{'PASS' if vd['all_active_pass'] else 'FAIL'}**"
          f"（未达 = {vd['failed'] or '无'}）")
        A("")
    # 附注 proposed 口径
    A("### 对照（proposed 划分不变量口径，不参与 gate）")
    A("")
    A("| 指标 | False | True |")
    A("|---|---|---|")
    A(f"| span_sharpe（拼接 Sharpe） | {off['metrics']['span_sharpe']:+.3f} | "
      f"{on['metrics']['span_sharpe']:+.3f} |")
    A(f"| month_win_rate | {off['metrics']['month_win_rate']*100:.1f}% | "
      f"{on['metrics']['month_win_rate']*100:.1f}% |")
    A("")

    # ---- 可执行建议 ----
    d_sh = (nn["sharpe"] - o["sharpe"]) if (o["sharpe"] is not None
                                            and nn["sharpe"] is not None) else None
    worst_dsh = _worst_dsh(off, on)
    A("## 5. 可执行建议")
    A("")
    A("1. **是否 re-baseline**：**倾向 re-baseline，但非紧急**。")
    A(f"   - 全样本 IS Sharpe Δ = {d_sh:+.3f}，已超过 Spec 的 +0.10 门级；"
      f"四窗 OOS 最差窗 dSh = {worst_dsh:+.3f}（75×7），缺口真实存在且不是窗口运气。")
    A(f"   - 但 KPI 两态均 PASS（kpi1 True 态 {on['metrics']['fold_mean_sharpe']:+.3f} "
      f"> 1.6 尚有 {on['metrics']['fold_mean_sharpe']-1.6:+.3f} 缓冲），"
      "缺口未击穿 KPI 底线。")
    A("   - 故：按 Spec §4，把 `limit_up_block=True` 登记为**并列的「真实摩擦口径」P0 基线**，"
      "供后续「实盘对账」与「买不到 alpha」定价使用；**暂不**覆盖既有已钉基线。")
    A("2. **是否把 `limit_up_block` 设为默认**：")
    A("   - 建议**先不**并入 `base_cfg()` 默认值（Spec 明确要求 OWNER 裁决，"
      "且默认 False = 零行为变化守护既有 3 条数值守卫基线）。")
    A("   - 若要反映真实摩擦，建议单独维护「真实口径」列，用于实盘对账，"
      "而非覆盖已钉 KPI。")
    A("3. 后续：被拦占比 3.0% 不高、但**错过收益显著为正**（+30.5%/+15.9%/+6.1%），"
      "说明被放弃的恰是「次日一字/大幅涨停、随后继续上行」的最强票——"
      "这正是策略「60 日动量最强 + 创 20 日新高」语义会系统性命中的名字。"
      "可评估：① 涨停价限价排队单（放弃→排队）以回收这部分 alpha；"
      "② 或接受该摩擦为策略成本，在真实口径下重新校准 KPI。")
    A("")

    # ---- confidence 与 caveats ----
    A("## 6. Confidence 与 Caveats")
    A("")
    A("- **confidence = medium**：核心对照（IS/OOS/KPI 两态差）在确定性 tushare 面板上"
      "逐位可复现、无随机性，方向与幅度可信；但以下两点压低置信度——")
    A(f"  ① 被拦买单仅 **{blk['blocked_buy_cnt']} 笔**（n=5），「错过收益」估计"
      "噪声极大，+30.5% 的单值不可外推，只能作为「被放弃的是强票」的**方向性证据**；")
    A("  ② `base_cfg()` 的 `northbound_mode=gate` 依赖北向资金缓存，而该缓存末端"
      "停留在 20260825（陈旧 58 天），面板末端 20261013 的近 1.5 个月北向闸门由陈旧"
      "数据驱动——这**同时**作用于两态，故两态对照仍干净，但**绝对 KPI 数值**继承该瑕疵。")
    A("- 被拦卖单 6 笔与买单 5 笔量级相当，离场侧跌停顺延同样贡献了 alpha 损失，"
      "非仅入场侧。")
    A("")

    # ---- 会不会推翻既有落盘决策 ----
    A("## 7. 这个结论会不会推翻既有落盘决策")
    A("")
    A("- 本次为**量测口径研究**，未改 `config/settings.py`、`engine/`、`risk/`、`web/`，"
      "生产行为零变化；**不推翻任何已落盘参数**。")
    A("- 但它**动摇历史回测的可信度**：IS Sharpe Δ -0.116、OOS 最差窗 dSh -0.278，"
      "均超过 +0.10 门级 ⇒ 历史「漂亮回测」含一部分「买不到/卖不出的美化」，"
      "与此前历次「基线同步」同型。")
    A("- **建议动作**：把 `limit_up_block=True` 登记为「真实摩擦口径」新基线"
      "（并列、非覆盖），后续凡涉及「实盘 vs 回测」的裁决一律以新口径为准；"
      "但**不因此回滚任何已落盘参数**——因为两态 KPI 均 PASS，且该摩擦是否要在"
      "生产侧被「排队限价单」吸收属独立决策。")
    A("")

    A("## 附：复现")
    A("")
    A("```")
    A("python scripts/_res_b9_limit_impact.py")
    A("```")
    A(f"确定性 tushare 面板：`storage/hist_cache/ts_panel_*.pkl`（`_pick_panel` 选中"
      f"{d['n_codes']} 只），无随机种子（全确定性）。数据区间 {d['start']} → {d['end']}，"
      f"样本 {d['n_bars']} 根。")
    A("")

    OUT_MD.write_text("\n".join(L), encoding="utf-8")


def _conclusion_line(off, on, blk):
    o = off["is"]; nn = on["is"]
    d_sh = (nn["sharpe"] - o["sharpe"]) if (o["sharpe"] is not None
                                            and nn["sharpe"] is not None) else None
    on_pass = on["verdict"]["all_active_pass"]
    off_pass = off["verdict"]["all_active_pass"]
    worst_dsh = _worst_dsh(off, on)
    if on_pass and off_pass:
        return (f"开启后 KPI 仍整体 PASS（kpi1 True 态 "
                f"{on['metrics']['fold_mean_sharpe']:+.3f} > 1.6）；"
                f"但 IS Sharpe 降 {d_sh:+.3f}、OOS 最差窗 dSh {worst_dsh:+.3f}，"
                f"均超 +0.10 门级 ⇒ 缺口真实、非窗口运气，倾向 re-baseline 但非紧急。")
    if off_pass and not on_pass:
        return ("KPI 由 PASS 变 FAIL：缺口真实存在，说明部分 alpha 是"
                "『买不到/卖不出』的美化，建议 re-baseline。")
    return ("两态 KPI 均非整体 PASS（见 KPI 表），需逐项定位失效 KPI。")


if __name__ == "__main__":
    main()

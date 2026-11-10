# -*- coding: utf-8 -*-
"""T4 结构型收益改进假设研究器（2026-11-10）。

【这是什么】
本仓库已明令**禁止扫参**（`reports/OPEN_DECISIONS.md §三`：36 个轴全部闭合或已定价；
「正收益折数」已被证明是划分依赖型噪声）。本脚本不重扫任何清单内的轴，只做 3 个
**可证伪的结构型假设**，每个都给出「机制 → 最小改动点 → 四窗共识 → 逐折指纹 →
最差折 → 结论」：

  A 宽度闸门替代：把「本轮 ≥2 只 BUY」换成「单只但评分 ≥X 且动量分位 ≥Y」
  B 退出管理：固定 −18% 豁免线 vs 让盈利奔跑（trailing / 盈利仓豁免单日暴跌）
  C 资金效率：结构性不满仓的暴露率、现金拖累、单位资金 Calmar

【口径纪律（照抄项目铁律）】
  * 决策用 `span_sharpe` / Calmar / 最差折，**不用**「正收益折数」当优化目标
    （D9：同一条 +218.8% 曲线切 9 段说 77.8%、切 2 段说 50.0%）。
  * 四窗共识（60x9 / 75x7 / 90x6 / 120x4）取**最差窗**，拒绝尖峰。
  * 所有候选与基线跑在**同一条已对齐面板**上（`aligned_universe`，与 _evolve_wf 同源）。

【实现方式（为什么有一份 backtest 副本）】
三个假设都需要在回测主循环里加钩子（条件放行 / 条件豁免 / 逐日资金占用），而
`strategy/backtest_daily.py` 是既有文件、本轮**禁止修改**。故：
  `strategy/_lab_bt_20261110.py` = backtest_daily.py 的**逐字副本** + 6 个 lab 字段 + 4 处钩子，
  全部 lab 字段保持默认时与原文件**逐位相同**，并由本脚本 `--stage verify` 自动核对
  （IS 8 项指标 + 四窗全部折的 ret/sharpe/mdd 必须逐位相等，否则中止）。

用法（重 CPU：只在 11:30–13:00 与 15:05 之后跑）：
    python strategy/_lab_20261110.py --stage verify
    python strategy/_lab_20261110.py --stage diag        # 单只通过者的质量画像 + 资金占用画像
    python strategy/_lab_20261110.py --stage all --json logs/_tmp/lab_20261110.json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import INDEX_CODES, MARKET_INDEX_CODE  # noqa: E402
from strategy._evolve_wf import (  # noqa: E402
    wide_universe, preload, aligned_universe, base_cfg, make_folds,
    PROD_MANUAL_ENTRY_CODES,
)

# 生产观察篮清单（R15 后已清空 ⇒ 当前为空元组；此处**直接读生产配置**，不复制常量）
PROD_MAN_CODES = tuple(PROD_MANUAL_ENTRY_CODES)
import strategy.backtest_daily as BT0  # noqa: E402
import strategy._lab_bt_20261110 as LAB  # noqa: E402

WINDOWS = [(60, 9), (75, 7), (90, 6), (120, 4)]
PANEL_N = 900

_BASE = base_cfg()   # 生产基线的 dataclass 实例（strategy.backtest_daily.BacktestConfig）


def L(**kw):
    """构造 **lab 副本**的配置对象（原字段 + 6 个 lab 字段）。

    ⚠ 必须这样做：`base_cfg()` 返回的是 *原* BacktestConfig（无 lab_* 字段），
    直接把它喂给 lab 引擎会 AttributeError（本轮已实测踩到）。
    """
    d = dict(vars(_BASE))
    d.update(kw)
    return LAB.BacktestConfig(**d)


def is_identical(a, b, keys=('total_return', 'sharpe', 'max_drawdown',
                             'calmar', 'n_trades', 'win_rate', 'exposure')):
    return all(a[k] == b[k] for k in keys)
BARS_PER_YEAR = 245


# ============================================================ 基础工具

def bt(mod, cfg, dset):
    """与 _evolve_wf.bt 同口径（同 preloaded 面板、同 count）。"""
    ks = [k for k in dset if k not in INDEX_CODES]
    return mod.run_backtest(ks, cfg, count=PANEL_N, preloaded=dset)


def st_folds(rs):
    ok = [r for r in rs if r and "error" not in r]
    if not ok:
        return None
    n = len(ok)
    return dict(
        n=n,
        mean_sh=sum(r["sharpe"] for r in ok) / n,
        mean_ret=sum(r["total_return"] for r in ok) / n,
        mean_mdd=sum(r["max_drawdown"] for r in ok) / n,
        mean_calmar=sum(r["calmar"] for r in ok) / n,
        worst_ret=min(r["total_return"] for r in ok),
        worst_calmar=min(r["calmar"] for r in ok),
        pos=sum(1 for r in ok if r["total_return"] > 0),
        rets=[r["total_return"] for r in ok],
        shs=[r["sharpe"] for r in ok],
        calmars=[r["calmar"] for r in ok],
    )


def is_row(r):
    if not r or "error" in r:
        return None
    return dict(
        ret=r["total_return"], sh=r["sharpe"], mdd=r["max_drawdown"],
        calmar=r["calmar"], n_trades=r["n_trades"], win=r["win_rate"],
        hold=r["avg_hold"], exposure=r["exposure"],
        exits=dict(r.get("exit_reasons") or {}),
    )


def fmt_pct(x, nd=2):
    return f"{x * 100:+.{nd}f}%"


# ============================================================ 面板

class Panel:
    def __init__(self):
        t0 = time.time()
        codes = wide_universe()
        raw = preload(codes + [MARKET_INDEX_CODE, "000300.SH"], PANEL_N)
        d0, panel = aligned_universe(raw, codes)
        self.codes = codes
        self.dates = d0
        self.panel = panel
        self.n = len(d0)
        self.load_sec = time.time() - t0

    def folds(self, fold, nfolds):
        return make_folds(self.panel, self.codes, self.n, fold, nfolds)

    def describe(self):
        print(f"[面板] {len(self.panel)} 只 × {self.n} 根 "
              f"{self.dates[0]} -> {self.dates[-1]}  载入 {self.load_sec:.1f}s")


# ============================================================ verify

VERIFY_KEYS = ("total_return", "sharpe", "max_drawdown", "calmar",
               "n_trades", "win_rate", "exposure", "alpha")


def stage_verify(P: Panel):
    """副本 vs 原文件：lab 字段全默认时必须**逐位相同**。"""
    print("\n" + "=" * 100)
    print("VERIFY：strategy/_lab_bt_20261110.py（钩子全关）必须与 strategy/backtest_daily.py 逐位相同")
    print("=" * 100)
    b = L()
    a = bt(BT0, b, P.panel)
    c = bt(LAB, b, P.panel)
    bad = []
    for k in VERIFY_KEYS:
        va, vc = a[k], c[k]
        same = (va == vc)
        print(f"  IS {k:<14}{va!r:>22}  vs {vc!r:>22}  {'SAME' if same else 'DIFF <<<'}")
        if not same:
            bad.append(k)
    # 折级（四窗全部折）
    for fold, nf in WINDOWS:
        subs = P.folds(fold, nf)
        for j, (_s, _e, ds) in enumerate(subs):
            x = bt(BT0, b, ds)
            y = bt(LAB, b, ds)
            for k in ("total_return", "sharpe", "max_drawdown"):
                if x[k] != y[k]:
                    bad.append(f"{fold}x{nf}-F{j+1}:{k}")
    print(f"\n  四窗 {sum(nf for _f, nf in WINDOWS)} 折 × 3 指标逐位核对完毕")
    if bad:
        print(f"  [FAIL] 副本与原文件不一致：{bad}")
        raise SystemExit(1)
    print("  [OK] 副本逐位等价 —— 后续所有候选的增量只来自 lab 钩子")
    return True


# ============================================================ H-A / H-B 候选

def cand_a(b, sc, pct):
    """H-A：宽度闸门替代。sc/pct = 单只放行的评分/动量分位下限。

    ★ 2026-11-10 12:4x 变向：A 已被 T2 用更根本的证据解掉 ——
      回测宽度=全宇宙通过数 / 生产宽度=动量 top_n=3 截断后的 BUY 数（上限 3）
      ⇒ 两侧根本不是同一道闸门，K=2 在生产里必然高频触发。
      本组仅在 --stage a 显式指定时运行，默认不跑，避免重复劳动。
    """
    return [
        ("A0_基线_K2(生产)", b),
        ("A1_K1_复现R21", replace(b, min_passing_candidates=1)),
        ("A2_K0_无宽度闸门", replace(b, min_passing_candidates=0)),
        ("A3_W1_单只半仓_已测", replace(b, entry_half_when_one=True)),
        ("A4_单只放行_半仓", replace(b, lab_a_min_score=sc, lab_a_min_mom_pct=pct,
                                      lab_a_single_half=True)),
        ("A5_单只放行_满仓", replace(b, lab_a_min_score=sc, lab_a_min_mom_pct=pct,
                                      lab_a_single_half=False)),
        ("A6_单只放行_低门槛_半仓", replace(b, lab_a_min_score=4.5,
                                            lab_a_min_mom_pct=50.0,
                                            lab_a_single_half=True)),
    ]


def cand_b(b):
    """H-B：退出管理（让盈利奔跑）。"""
    return [
        ("B0_基线_固定-18%", b),
        ("B1_trailing_激活10%/回撤5%", replace(
            b, trend_trailing=True, trend_trail_activation=0.10,
            trend_trail_stop=-0.05, trend_trail_floor=0.0)),
        ("B2_trailing_激活20%/回撤8%", replace(
            b, trend_trailing=True, trend_trail_activation=0.20,
            trend_trail_stop=-0.08, trend_trail_floor=0.05)),
        ("B3_盈利仓豁免crash_0%", replace(b, lab_b_profit_exempt=0.0)),
        ("B4_盈利仓豁免crash_+5%", replace(b, lab_b_profit_exempt=0.05)),
        ("B5_盈利仓豁免crash_+10%", replace(b, lab_b_profit_exempt=0.10)),
        # ---- 实盘政策直译：两只持仓 stop 都是 −18% 豁免线 ⇒「只留硬止损」----
        ("B6_全仓-18%豁免(实盘政策)", replace(b, lab_exempt_all=True)),
        ("B7_观察篮清单+豁免(生产现状)", replace(
            b, manual_entry_codes=PROD_MAN_CODES,
            manual_entry_exit_exempt=True)),
        ("B8_观察篮清单_关闭豁免", replace(
            b, manual_entry_codes=PROD_MAN_CODES,
            manual_entry_exit_exempt=False)),
    ]


# ============================================================ 共识评估

def evaluate(P: Panel, cands, title, note=""):
    """四窗共识 + IS + 逐折指纹。返回结构化结果。"""
    print("\n" + "=" * 116)
    print(title)
    if note:
        print(note)
    print("=" * 116)
    name0 = cands[0][0]
    out = {"title": title, "note": note, "candidates": []}

    # IS
    print(f"\n【样本内 IS】全样本 {P.n} 根（含 0.15% 单边成本）")
    print(f"{'配置':<28}{'IS收益':>10}{'IS_Sh':>8}{'IS_MDD':>9}{'IS_Calmar':>11}"
          f"{'成交':>6}{'胜率':>7}{'持仓d':>7}{'暴露':>7}  退出原因")
    is_res = {}
    for name, cfg in cands:
        r = bt(LAB, cfg, P.panel)
        is_res[name] = r
        row = is_row(r)
        if row is None:
            print(f"{name:<28} ERROR {r.get('error')}")
            continue
        ex = "/".join(f"{k}:{v}" for k, v in sorted(row["exits"].items(),
                                                    key=lambda kv: -kv[1]))
        print(f"{name:<28}{fmt_pct(row['ret'], 1):>10}{row['sh']:>+8.2f}"
              f"{fmt_pct(row['mdd'], 2):>9}{row['calmar']:>+11.2f}"
              f"{row['n_trades']:>6}{row['win'] * 100:>6.1f}%{row['hold']:>7.1f}"
              f"{row['exposure'] * 100:>6.1f}%  {ex}")

    # OOS 四窗
    base_name = name0
    per_win = {}
    for fold, nf in WINDOWS:
        subs = P.folds(fold, nf)
        per_win[f"{fold}x{nf}"] = {name: st_folds([bt(LAB, cfg, ds) for _s, _e, ds in subs])
                                   for name, cfg in cands}
    bs = {w: per_win[w][base_name] for w in per_win}

    print(f"\n【样本外 OOS】四窗共识（判定看**最差窗**，不看平均）")
    hdr = (f"{'配置':<28}" + "".join(f"{w:>12}" for w in per_win)
           + f"{'最差窗':>9}{'均值':>8}{'最差折':>9}{'均值Calmar':>11}{'最差Calmar':>11}")
    print(hdr)
    print("-" * 116)
    rows = []
    for name, _cfg in cands:
        cells, dshs, dcals = [], [], []
        for w in per_win:
            s = per_win[w][name]
            bsw = bs[w]
            if s is None or bsw is None:
                cells.append(f"{'ERR':>12}")
                dshs.append(-9.9)
                dcals.append(-9.9)
                continue
            dsh = s["mean_sh"] - bsw["mean_sh"]
            dcal = s["mean_calmar"] - bsw["mean_calmar"]
            dshs.append(dsh)
            dcals.append(dcal)
            cells.append(f"{dsh:>+7.3f}/{dcal:>+4.1f}")
        mcal = sum(per_win[w][name]["mean_calmar"] for w in per_win
                   if per_win[w][name]) / max(1, sum(
                       1 for w in per_win if per_win[w][name]))
        wcal = min(per_win[w][name]["mean_calmar"] for w in per_win
                   if per_win[w][name])
        wf = min(per_win[w][name]["worst_ret"] for w in per_win
                 if per_win[w][name])
        mn, avg = min(dshs), sum(dshs) / len(dshs)
        print(f"{name:<28}" + "".join(f"{c:>12}" for c in cells)
              + f"{mn:>+9.3f}{avg:>+8.3f}{fmt_pct(wf, 1):>9}"
              + f"{mcal:>+11.2f}{wcal:>+11.2f}")
        pw = {}
        for i, w in enumerate(per_win):
            s = per_win[w][name]
            pw[w] = dict(d_sh=dshs[i], d_calmar=dcals[i],
                         mean_sh=(s["mean_sh"] if s else None),
                         mean_calmar=(s["mean_calmar"] if s else None),
                         mean_ret=(s["mean_ret"] if s else None),
                         worst_ret=(s["worst_ret"] if s else None),
                         pos=(s["pos"] if s else None),
                         n=(s["n"] if s else None),
                         rets=(s["rets"] if s else None))
        rows.append(dict(name=name, per_window=pw,
                         min_d_sh=mn, mean_d_sh=avg, worst_fold=wf,
                         mean_calmar=mcal, worst_calmar=wcal,
                         is_=(is_row(is_res[name]) if name in is_res else None)))
    print("  单元格 = 该窗 dSharpe / dCalmar（相对基线）；最后一列起为绝对 Calmar")

    # 逐折指纹（90x6 主窗 + 四窗逐折 dRet）
    print(f"\n【逐折指纹】各窗逐折收益差（pt，相对基线；正=该折变好）")
    for fold, nf in WINDOWS:
        w = f"{fold}x{nf}"
        print(f"  [{w}]")
        for r in rows[1:]:
            base_rets = bs[w]["rets"]
            mine = r["per_window"][w]["rets"]
            if mine is None:
                continue
            diffs = [(m - b_) * 100 for m, b_ in zip(mine, base_rets)]
            print(f"    {r['name']:<28}"
                  + " ".join(f"{d:>+6.1f}" for d in diffs)
                  + f"   ({sum(d > 0 for d in diffs)}/{len(diffs)} 折为正)")
    out["candidates"] = rows
    out["baseline"] = {w: dict(mean_sh=bs[w]["mean_sh"], mean_ret=bs[w]["mean_ret"],
                              mean_calmar=bs[w]["mean_calmar"],
                              worst_ret=bs[w]["worst_ret"], pos=bs[w]["pos"],
                              n=bs[w]["n"], rets=bs[w]["rets"]) for w in per_win}
    return out


# ============================================================ H-C 资金效率

def _fold_expo(run):
    """单折/单段的资金占用画像 + 两个反事实（满仓 / 现金计息）。"""
    dl = run.get("lab_daily") or []
    if not dl:
        return None
    inv = [d["invested"] for d in dl]
    npos = [d["n_pos"] for d in dl]
    cashp = [d["cash"] / d["equity"] for d in dl if d["equity"] > 0]
    hist = {}
    for k in npos:
        hist[k] = hist.get(k, 0) + 1

    # ---- 反事实 1：同一批交易，每天把权益填满 ----
    #   r_t = ē_{t-1} · a_t  ⇒  a_t = r_t / ē_{t-1}（空仓日无法放大 ⇒ 记 0，不制造暴露）
    a = []
    for k in range(1, len(dl)):
        if dl[k - 1]["equity"] <= 0:
            continue
        e_prev = dl[k - 1]["invested"]
        r_ = dl[k]["equity"] / dl[k - 1]["equity"] - 1
        a.append(r_ / e_prev if e_prev > 1e-9 else 0.0)
    curve = [1.0]
    for x in a:
        curve.append(curve[-1] * (1.0 + x))
    m = sum(a) / len(a) if a else 0.0
    var = (sum((x - m) ** 2 for x in a) / (len(a) - 1)) if len(a) > 1 else 0.0
    sd = math.sqrt(var)
    sh_full = (m / sd * math.sqrt(BARS_PER_YEAR)) if sd > 0 else 0.0
    pk, mdd = curve[0], 0.0
    for e in curve:
        pk = max(pk, e)
        mdd = min(mdd, e / pk - 1)
    yrs = len(a) / BARS_PER_YEAR
    cagr_full = (curve[-1] ** (1 / yrs) - 1) if (yrs > 0 and curve[-1] > 0) else 0.0
    calmar_full = cagr_full / abs(mdd) if mdd else 0.0

    # ---- 反事实 2：闲置现金按无风险收益计息（回测引擎里现金收益恒为 0）----
    rf_out = {}
    for rf in (0.015, 0.020):
        rf_d = rf / BARS_PER_YEAR
        ccurve = [1.0]
        for k in range(1, len(dl)):
            if dl[k - 1]["equity"] <= 0:
                continue
            e_cash = dl[k - 1]["cash"] / dl[k - 1]["equity"]
            rr = dl[k]["equity"] / dl[k - 1]["equity"] - 1 + e_cash * rf_d
            ccurve.append(ccurve[-1] * (1.0 + rr))
        pk2, mdd2 = ccurve[0], 0.0
        for e in ccurve:
            pk2 = max(pk2, e)
            mdd2 = min(mdd2, e / pk2 - 1)
        ys = len(ccurve) / BARS_PER_YEAR
        cagr2 = (ccurve[-1] ** (1 / ys) - 1) if (ys > 0 and ccurve[-1] > 0) else 0.0
        rf_out[f"{rf * 100:.1f}%"] = dict(
            ret=ccurve[-1] - 1.0, mdd=mdd2,
            calmar=(cagr2 / abs(mdd2) if mdd2 else 0.0),
            gain_pt=(ccurve[-1] - 1.0 - run["total_return"]) * 100)
    return dict(
        bars=len(dl), mean_invested=sum(inv) / len(inv),
        mean_cash_pct=(sum(cashp) / len(cashp) if cashp else 0.0),
        share_lt_95=sum(1 for x in inv if x < 0.95) / len(inv),
        share_lt_50=sum(1 for x in inv if x < 0.50) / len(inv),
        mean_npos=sum(npos) / len(npos),
        share_npos0=sum(1 for x in npos if x == 0) / len(npos),
        max_npos=max(npos), hist=hist,
        ret_actual=run["total_return"], mdd_actual=run["max_drawdown"],
        calmar_actual=run["calmar"], sharpe_actual=run["sharpe"],
        ret_full=curve[-1] - 1.0, mdd_full=mdd, calmar_full=calmar_full,
        sharpe_full=sh_full, cash_drag_pt=(curve[-1] - 1.0 - run["total_return"]) * 100,
        rf=rf_out)


def _agg(expos, key):
    v = [e[key] for e in expos if e]
    return sum(v) / len(v) if v else 0.0


def stage_c(P: Panel):
    """H-C：结构性不满仓的定量画像（暴露率 / 现金拖累 / 单位资金 Calmar）。

    统计口径说明（很重要，别混）：
      * `run_backtest()['exposure']` = **有仓位的交易日占比**（时间口径）
      * 本段 `ē` = 每日 **持仓市值/权益** 的均值（**资金口径**，才是「几成仓」）
    """
    print("\n" + "=" * 116)
    print("H-C 资金效率：结构性不满仓的机会成本（四窗全部折聚合，每折内按日聚合）")
    print("=" * 116)
    b = L(lab_log_daily=True)
    out = {}

    def show(tag, expos):
        e0 = expos[0] if expos else None
        if e0 is None:
            print(f"  [{tag}] 无数据")
            return
        print(f"\n  [{tag}] 折数 {len(expos)}")
        print(f"    日均资金占用 ē = {_agg(expos, 'mean_invested') * 100:.1f}%   "
              f"日均现金 = {_agg(expos, 'mean_cash_pct') * 100:.1f}%")
        print(f"    占用<95% 的交易日占 {_agg(expos, 'share_lt_95') * 100:.1f}%   "
              f"<50% 占 {_agg(expos, 'share_lt_50') * 100:.1f}%   "
              f"空仓日占 {_agg(expos, 'share_npos0') * 100:.1f}%")
        print(f"    平均持仓数 {_agg(expos, 'mean_npos'):.2f}   最大持仓 "
              f"{max(e['max_npos'] for e in expos if e)}（max_positions=5）")
        # 持仓数分布（跨折求和）
        hist = {}
        for e in expos:
            if e:
                for k, v in e["hist"].items():
                    hist[k] = hist.get(k, 0) + v
        tot = sum(hist.values())
        print("    持仓数分布 " + "  ".join(
            f"{k}只:{(hist[k] / tot * 100):.1f}%" for k in sorted(hist)))
        print(f"    实际：收益均值 {_agg(expos, 'ret_actual') * 100:+.2f}%  "
              f"MDD 均值 {_agg(expos, 'mdd_actual') * 100:.2f}%  "
              f"Calmar 均值 {_agg(expos, 'calmar_actual'):+.2f}  "
              f"Sharpe 均值 {_agg(expos, 'sharpe_actual'):+.2f}")
        print(f"    ★ 反事实A（同一批交易、每天把权益填满）：收益均值 "
              f"{_agg(expos, 'ret_full') * 100:+.2f}%  MDD 均值 "
              f"{_agg(expos, 'mdd_full') * 100:.2f}%  Calmar 均值 "
              f"{_agg(expos, 'calmar_full'):+.2f}  Sharpe 均值 "
              f"{_agg(expos, 'sharpe_full'):+.2f}")
        print(f"      ⇒ 填满的净效果：收益 {_agg(expos, 'cash_drag_pt'):+.1f}pt（均值，每折累计）"
              f"  Calmar {_agg(expos, 'calmar_actual'):+.2f} → "
              f"{_agg(expos, 'calmar_full'):+.2f}")
        for rfk in ("1.5%", "2.0%"):
            g = [e["rf"][rfk]["gain_pt"] for e in expos if e]
            c0 = _agg(expos, "calmar_actual")
            c1 = sum(e["rf"][rfk]["calmar"] for e in expos if e) / len(
                [e for e in expos if e])
            print(f"    ○ 反事实B（闲置现金按无风险 {rfk} 计息）：收益增益均值 "
                  f"{sum(g) / len(g):+.2f}pt  Calmar {c0:+.2f} → {c1:+.2f}")
        out[tag] = dict(
            n_folds=len(expos),
            mean_invested=_agg(expos, "mean_invested"),
            mean_cash_pct=_agg(expos, "mean_cash_pct"),
            share_lt_95=_agg(expos, "share_lt_95"),
            share_lt_50=_agg(expos, "share_lt_50"),
            share_npos0=_agg(expos, "share_npos0"),
            mean_npos=_agg(expos, "mean_npos"),
            max_npos=max(e["max_npos"] for e in expos if e),
            npos_hist={str(k): hist.get(k, 0) / tot for k in sorted(hist)},
            ret_actual=_agg(expos, "ret_actual"),
            mdd_actual=_agg(expos, "mdd_actual"),
            calmar_actual=_agg(expos, "calmar_actual"),
            sharpe_actual=_agg(expos, "sharpe_actual"),
            ret_full=_agg(expos, "ret_full"), mdd_full=_agg(expos, "mdd_full"),
            calmar_full=_agg(expos, "calmar_full"),
            sharpe_full=_agg(expos, "sharpe_full"),
            cash_drag_pt=_agg(expos, "cash_drag_pt"),
            rf={rfk: dict(gain_pt=sum(e["rf"][rfk]["gain_pt"] for e in expos if e)
                          / len([e for e in expos if e]),
                          calmar=sum(e["rf"][rfk]["calmar"] for e in expos if e)
                          / len([e for e in expos if e]),
                          ret=sum(e["rf"][rfk]["ret"] for e in expos if e)
                          / len([e for e in expos if e]))
                for rfk in ("1.5%", "2.0%")})

    # IS 全样本
    r_is = bt(LAB, b, P.panel)
    show("IS全样本(1折)", [_fold_expo(r_is)])

    # 四窗全部折
    for fold, nf in WINDOWS:
        subs = P.folds(fold, nf)
        expos = [_fold_expo(bt(LAB, b, ds)) for _s, _e, ds in subs]
        show(f"{fold}x{nf}（{nf} 折）", expos)

    # 口径对照
    r0 = bt(LAB, L(), P.panel)
    out["_exposure_def"] = dict(
        exposure_days=r0["exposure"],
        note="run_backtest.exposure = 有仓位的交易日占比（时间口径）；ē = 资金口径")
    print(f"\n  [口径对照] run_backtest['exposure'] = {r0['exposure'] * 100:.1f}% "
          f"（**时间口径**：有仓位的天数占比）")
    print(f"             本段 ē（**资金口径**）= {out['IS全样本(1折)']['mean_invested'] * 100:.1f}% "
          f"⇒ 「不满仓」的严重程度被时间口径**低估**了约 "
          f"{r0['exposure'] / max(1e-9, out['IS全样本(1折)']['mean_invested']):.1f} 倍")
    return out


# ============================================================ H-C2 满仓结构诊断

def stage_c2(P: Panel):
    """C2：「永不填满」的结构成本 —— 瓶颈是 top_n 还是 max_positions？

    只做**诊断**，不投任何参数：R32 已判 max_positions≥4 逐位惰性、R33 已判
    (top_n=3, min_mom=0.15) 为联合最优。此处量的是「把动量池放到与 mp 匹配」后
    资金占用与 Calmar 到底变不变 —— 若只是把瓶颈从 top_n 挪到 mp、总暴露不变，
    则「本系统本质上无法满仓」是一条结构性结论，不该改。
    """
    print("\n" + "=" * 116)
    print("C2 满仓结构诊断：瓶颈在 top_n（候选供给）还是 max_positions（槽位）？")
    print("=" * 116)
    out = {}
    cfgs = [
        ("P0_top3_mp5(生产)", L(lab_log_daily=True)),
        ("top3_mp2_截断", L(lab_log_daily=True, max_positions=2)),
        ("top5_mp5_匹配", L(lab_log_daily=True, momentum_top_n=5, max_positions=5)),
        ("top8_mp8_匹配", L(lab_log_daily=True, momentum_top_n=8, max_positions=8)),
    ]
    var = {}
    for name, cfg in cfgs:
        is_r = bt(LAB, cfg, P.panel)
        dl = is_r.get("lab_daily") or []
        inv = [d["invested"] for d in dl]
        npos = [d["n_pos"] for d in dl]
        hist = {}
        for k in npos:
            hist[k] = hist.get(k, 0) + 1
        # 四窗 OOS 的均值资金占用 / Calmar
        oos = {}
        for fold, nf in WINDOWS:
            subs = P.folds(fold, nf)
            rs, dlall = [], []
            for _s, _e, ds in subs:
                rr = bt(LAB, cfg, ds)
                rs.append(rr)
                dlall.append(rr.get("lab_daily") or [])
            inv_w = [d["invested"] for z in dlall for d in z]
            np_w = [d["n_pos"] for z in dlall for d in z]
            oos[f"{fold}x{nf}"] = dict(
                mean_invested=sum(inv_w) / len(inv_w) if inv_w else 0.0,
                mean_npos=sum(np_w) / len(np_w) if np_w else 0.0,
                mean_calmar=sum(r["calmar"] for r in rs) / len(rs),
                mean_ret=sum(r["total_return"] for r in rs) / len(rs),
                worst_ret=min(r["total_return"] for r in rs),
                share_npos0=sum(1 for x in np_w if x == 0) / len(np_w) if np_w else 0.0,
            )
        var[name] = dict(
            is_ret=is_r["total_return"], is_calmar=is_r["calmar"],
            is_sharpe=is_r["sharpe"], is_mdd=is_r["max_drawdown"],
            n_trades=is_r["n_trades"], avg_hold=is_r["avg_hold"],
            mean_invested=sum(inv) / len(inv) if inv else 0.0,
            mean_npos=sum(npos) / len(npos) if npos else 0.0,
            max_npos=max(npos) if npos else 0,
            share_npos0=sum(1 for x in npos if x == 0) / len(npos) if npos else 0.0,
            hist=hist, oos=oos)
        print(f"\n  [{name}] IS ret {fmt_pct(is_r['total_return'], 1)} / Calmar {is_r['calmar']:.2f}"
              f" / Sh {is_r['sharpe']:.2f} / MDD {fmt_pct(is_r['max_drawdown'])}")
        print(f"    日均资金占用 {var[name]['mean_invested'] * 100:.1f}%   "
              f"日均持仓数 {var[name]['mean_npos']:.2f}  最大 {var[name]['max_npos']}"
              f"  空仓日 {var[name]['share_npos0'] * 100:.1f}%")
        print(f"    持仓数分布 {dict(sorted(hist.items()))}")
        for w in oos:
            o = oos[w]
            print(f"    {w}: 占用 {o['mean_invested'] * 100:.1f}%  持仓数 {o['mean_npos']:.2f}"
                  f"  Calmar {o['mean_calmar']:+.2f}  ret {o['mean_ret'] * 100:+.1f}%"
                  f"  最差折 {o['worst_ret'] * 100:+.1f}%")
    print("\n  ★ 判读：若 top5/top8 的日均资金占用与 top3 基本相同 ⇒ 瓶颈不在 mp，"
          "而在**候选供给（动量闸门通过数）** ⇒ 满仓物理不可达，属结构结论。")
    return var


# ============================================================ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all",
                    choices=["verify", "diag", "a", "b", "c", "all"])
    ap.add_argument("--a-score", type=float, default=6.0,
                    help="H-A 单只放行的评分下限（默认 6.0，对齐 2026-11-10 实盘 688981=6.86）")
    ap.add_argument("--a-pct", type=float, default=80.0,
                    help="H-A 单只放行的 60 日动量分位下限（0~100）")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    P = Panel()
    P.describe()
    result = {}

    if args.stage in ("verify", "all"):
        stage_verify(P)

    if args.stage in ("diag", "all"):
        print("\n" + "=" * 116)
        print("DIAG-1：全宇宙「当日唯一通过全部入场门」的日子，其质量画像（H-A 定阈用）")
        print("=" * 116)
        b = L(lab_log_daily=True)
        r = bt(LAB, b, P.panel)
        sd = r.get("lab_single_days") or []
        hist = r.get("lab_breadth_hist") or {}
        tot = sum(hist.values())
        print(f"  宽度分布（当日通过全部入场门的票数）：")
        for k in sorted(hist):
            print(f"    breadth={k}: {hist[k]:>4} 天（{hist[k] / tot * 100:5.1f}%）")
        print(f"  ⇒ breadth=1 的天数 = {hist.get(1, 0)}（占 {hist.get(1, 0) / tot * 100:.1f}%）"
              f"；breadth=0 = {hist.get(0, 0)}；breadth<2 = {hist.get(0, 0) + hist.get(1, 0)}")
        if sd:
            scs = sorted(x["score"] for x in sd)
            pcts = sorted(x["mom_pct"] for x in sd)
            def q(a, p):
                return a[min(len(a) - 1, int(p * (len(a) - 1)))]
            print(f"  breadth≥1 的 {len(sd)} 天里，当日**首个**通过者的评分分布："
                  f"min {scs[0]:.2f} / p25 {q(scs, .25):.2f} / 中位 {q(scs, .5):.2f}"
                  f" / p75 {q(scs, .75):.2f} / max {scs[-1]:.2f}")
            print(f"  同一批的动量分位分布："
                  f"min {pcts[0]:.1f} / p25 {q(pcts, .25):.1f} / 中位 {q(pcts, .5):.1f}"
                  f" / p75 {q(pcts, .75):.1f} / max {pcts[-1]:.1f}")
            for sc, pct in [(6.0, 80.0), (4.5, 50.0), (5.0, 70.0)]:
                k = sum(1 for x in sd if x["score"] >= sc and x["mom_pct"] >= pct)
                print(f"    score≥{sc} 且 mom_pct≥{pct} 的天数 = {k}"
                      f"（{k / len(sd) * 100:.1f}% of breadth==1）")
            result["diag_single"] = dict(
                n=len(sd), hist=hist,
                score_med=q(scs, .5), pct_med=q(pcts, .5),
                score_p25=q(scs, .25), pct_p25=q(pcts, .25),
                per_day=[dict(date=x["date"], code=x["code"],
                              score=round(x["score"], 3),
                              mom_pct=round(x["mom_pct"], 2)) for x in sd])
        result["diag_breadth_hist"] = hist

    if args.stage in ("a",):
        result["A"] = evaluate(
            P, cand_a(L(), args.a_score, args.a_pct),
            "H-A 宽度闸门替代：单只但「评分≥X 且动量分位≥Y」能否替代「本轮≥2 只 BUY」",
            f"★ 先复现 R21 的否定口径（A1_K1 必须与基线逐位相同），再判 A4/A5 是否只是换名字。\n"
            f"  本轮预注册阈：评分 ≥ {args.a_score}（对齐 2026-11-10 实盘 688981=6.86）且动量分位 ≥ {args.a_pct}")

    if args.stage in ("b", "all"):
        result["B"] = evaluate(
            P, cand_b(L()),
            "H-B 退出管理：固定 −18% 豁免线 vs 让盈利奔跑（trailing / 盈利仓豁免单日暴跌）",
            "★ 注意：chandelier 与 trend_atr_stop_mult k≥2.5 已被 R25 判「严格单调负 / 惰性」，"
            "本组不重扫它们；B1/B2 走 trend_trailing（此前从未进过勿重试清单），"
            "B3~B5 是「结构型」变体：盈利仓不参与单日暴跌清仓。")

    if args.stage in ("c", "all"):
        result["C"] = stage_c(P)
        result["C2"] = stage_c2(P)

    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2,
                                              default=str), encoding="utf-8")
        print(f"\n[保存] {args.json}")


if __name__ == "__main__":
    main()

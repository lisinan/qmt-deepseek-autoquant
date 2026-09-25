"""【2026-09-25 AM-EVOLVE 第 8 轮】宇宙规模 walk-forward 实验器。

背景
----
此前所有 wf 结论都建立在 **23~25 只 AI 产业链股票** 上（``config/settings.py::UNIVERSE``）。
这 25 只同属一条产业链、高度同向，横截面动量排名在内部几乎无区分度
⇒ 组合是"伪分散"，Sharpe 存在结构性天花板（实测 ~1.35）。
历史上「扩宇宙」被判定为**被本地数据卡死**，但 2026-09-24 PM 修复了日线陈旧缺陷后，
实测 ``xtdata.get_market_data_ex`` 批量取 **200 只仅 0.3s、100% 覆盖 ≥700 根**，
且末日 = 最新交易日 ⇒ **该阻塞已解除**，扩宇宙首次可验证。

本实验器：在同一 BacktestConfig（= 生产 P0）下，仅改变**股票池规模**，
跑 IS + 90×6 walk-forward，看 OOS 均值 Sharpe 是否随宇宙规模单调上升。
若存在单调剂量反应，说明「宇宙宽度」是此前从未检验过的一维真实 alpha。

用法::

    python strategy/_evolve_universe.py --sizes 25,50,100,200,400 --seed 7
    python strategy/_evolve_universe.py --sizes 200 --folds 6 --json reports/_uni.json

零行为变化：本文件是**独立实验工具**，不 import 进生产路径，不改任何生产配置。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

try:                       # 无 miniQMT 的环境（CI/测试）下仍需可 import 纯函数
    from xtquant import xtdata
except Exception:          # pragma: no cover
    xtdata = None

from strategy._evolve_wf import (  # 复用生产验证器的口径：base_cfg / make_folds / oos_stats
    FIXED_WARMUP, INDEX_CODES, MARKET_INDEX_CODE, base_cfg, make_folds, oos_stats,
    wide_universe,
)
from strategy.backtest_daily import run_backtest

MIN_BARS = 700          # 低于此长度的标的（次新/长期停牌）直接剔除
EXCLUDE_SUFFIX = (".BJ",)   # 北交所流动性差，剔除


# ---------------------------------------------------------------- 数据
def _to_series(df) -> dict:
    return {
        "date": [str(x)[:10].replace("-", "") for x in df.index],
        "open": [float(v) for v in df["open"]],
        "high": [float(v) for v in df["high"]],
        "low": [float(v) for v in df["low"]],
        "close": [float(v) for v in df["close"]],
        "volume": [float(v) for v in df["volume"]],
    }


def _valid_count(series: dict) -> int:
    """有效收盘价根数（NaN / <=0 视为无效）。

    ★ 关键陷阱（2026-09-25 实测）：``get_market_data_ex`` 对**本地未缓存**的标的
    照样返回长度 900 的 DataFrame，但值全为 NaN。只看 ``len()`` 会把 5166 只
    空壳误判成"数据齐全"，灌进回测后横截面排名被 NaN 污染 ⇒ **成交数归零**
    （实测 N=50 时 n_trades 0 vs N=25 的 39）。必须按有效值计数。
    """
    return sum(1 for x in series["close"]
               if isinstance(x, float) and x == x and x > 0)


def fetch_all_a(count: int = 900, verbose: bool = True,
                candidates: list | None = None) -> dict:
    """拉取沪深A股日线；对本地未缓存的候选标的先 ``download_history_data`` 补拉。

    实测（2026-09-25）：批量读取 5225 只约 6s；``download_history_data`` 补拉
    **0.05s/只**（20 只 1.0s，18 只转为有效）⇒ 扩宇宙的取数成本可忽略。
    """
    codes = [c for c in xtdata.get_stock_list_in_sector("沪深A股")
             if not c.endswith(EXCLUDE_SUFFIX)]
    t0 = time.time()
    raw = xtdata.get_market_data_ex([], codes, period="1d", count=count,
                                    dividend_type="front")
    out = {}
    for code, df in raw.items():
        try:
            s = _to_series(df)
        except Exception:
            continue
        if len(s["close"]) >= MIN_BARS and _valid_count(s) >= MIN_BARS:
            out[code] = s
    n_local = len(out)
    # ---- 补拉（只对显式指定的候选，避免全市场 5000 只的无谓网络开销）----
    if candidates:
        todo = [c for c in candidates if c not in out]
        if todo:
            for c in todo:
                try:
                    xtdata.download_history_data(c, period="1d")
                except Exception:
                    pass
            raw2 = xtdata.get_market_data_ex([], todo, period="1d",
                                             count=count, dividend_type="front")
            for code, df in raw2.items():
                try:
                    s = _to_series(df)
                except Exception:
                    continue
                if len(s["close"]) >= MIN_BARS and _valid_count(s) >= MIN_BARS:
                    out[code] = s
    if verbose:
        print(f"[数据] 沪深A股 {len(codes)} 只：本地已缓存有效 {n_local} 只，"
              f"补拉 {len(candidates or [])} 只候选后有效 {len(out)} 只，"
              f"耗时 {time.time()-t0:.1f}s")
    return out


def _aligned_length(data: dict, codes: list) -> int:
    return min(len(data[c]["close"]) for c in codes if c in data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="25,50,100,200,400")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--fold", type=int, default=90)
    ap.add_argument("--folds", type=int, default=6)
    ap.add_argument("--json", default="")
    ap.add_argument("--ai-first", type=int, default=1,
                    help="1=扩充宇宙时在随机股之外优先保留现有 AI 宇宙（默认 1）")
    ap.add_argument("--seeds", default="",
                    help="多种子稳健性：逗号分隔。**同一进程内**跑完所有种子，"
                         "保证各 seed 看到完全相同的底层数据")
    ap.add_argument("--pool", type=int, default=0,
                    help="统一预补拉的候选池规模（0=按最大 size 自动）。"
                         "多种子模式下应设为一个覆盖全部 seed 的固定值")
    args = ap.parse_args()

    cfg = base_cfg()
    cur = sorted(wide_universe())
    print(f"[基线] 当前生产宇宙 = {len(cur)} 只（AI 产业链）")

    sizes = sorted(int(s) for s in args.sizes.split(","))
    seeds = [int(s) for s in args.seeds.split(",") if s.strip()] or [args.seed]
    # 先按最大规模确定候选池（各规模取前缀 ⇒ 宇宙逐层嵌套，剂量反应可比）
    allc = [c for c in xtdata.get_stock_list_in_sector("沪深A股")
            if not c.endswith(EXCLUDE_SUFFIX) and c not in set(cur)]
    random.Random(12345).shuffle(allc)      # 主洗牌：固定，保证候选池与 seed 无关
    need = args.pool or max(0, max(sizes) - len(cur))
    data = fetch_all_a(candidates=allc[:need])
    if not data:
        print("无数据，退出")
        return
    # 指数（regime 过滤用）也必须有
    for ic in (MARKET_INDEX_CODE, "000300.SH"):
        if ic not in data:
            try:
                d = xtdata.get_market_data_ex([], [ic], period="1d", count=900,
                                              dividend_type="front")
                df = d[ic]
                data[ic] = {
                    "date": [str(x)[:10].replace("-", "") for x in df.index],
                    "open": [float(v) for v in df["open"]],
                    "high": [float(v) for v in df["high"]],
                    "low": [float(v) for v in df["low"]],
                    "close": [float(v) for v in df["close"]],
                    "volume": [float(v) for v in df["volume"]],
                }
            except Exception as e:
                print(f"[警告] 指数 {ic} 取数失败：{e}")

    pool = [c for c in allc if c in data and c not in INDEX_CODES]

    rows = []
    print(f"\n{'=' * 104}")
    print(f"宇宙规模实验（其余参数固定 = 生产 P0；seeds={seeds}）— 找单调剂量反应")
    print("=" * 104)
    print(f"{'seed':<7}{'宇宙N':<8}{'IS_ret':>10}{'IS_Sh':>8}{'IS_MDD':>9}"
          f"{'OOS均值Sh':>11}{'dSh':>8}{'均值ret':>10}{'均值MDD':>9}"
          f"{'正收':>7}{'最差折':>9}")

    for seed in seeds:
        rng = random.Random(seed)
        # 各 seed 从**同一**已补拉候选池里抽样，仅抽样顺序不同
        pool = [c for c in allc if c in data and c not in INDEX_CODES]
        rng.shuffle(pool)
        base_sh = None
        for size in sizes:
            base = list(cur) if args.ai_first else []
            extra = [c for c in pool if c not in set(base)][:max(0, size - len(base))]
            codes = base + extra
            if len(codes) < size:
                print(f"{seed:<7}{size:<8} 可用标的不足（{len(codes)}），跳过")
                continue
            codes = sorted(set(codes))
            dset = {c: data[c] for c in codes if c in data}
            for ic in (MARKET_INDEX_CODE, "000300.SH"):
                if ic in data:
                    dset[ic] = data[ic]
            n = _aligned_length(dset, [c for c in codes if c in dset])
            subs = make_folds(dset, codes, n, args.fold, args.folds)

            def bt(c, dd):
                ks = [k for k in dd if k not in INDEX_CODES]
                return run_backtest(ks, c, count=n, preloaded=dd)

            t0 = time.time()
            isr = bt(cfg, dset)
            st, _rs = oos_stats(cfg, subs, bt)
            if not st:
                print(f"{seed:<7}{size:<8} ERROR")
                continue
            if base_sh is None:
                base_sh = st["mean_sharpe"]
            dsh = st["mean_sharpe"] - base_sh
            print(f"{seed:<7}{size:<8}{isr['total_return']*100:>+9.1f}%{isr['sharpe']:>+8.2f}"
                  f"{isr['max_drawdown']*100:>8.2f}%{st['mean_sharpe']:>+11.3f}"
                  f"{dsh:>+8.3f}{st['mean_ret']*100:>+9.1f}%{st['mean_mdd']*100:>8.2f}%"
                  f"{st['pos']:>4}/{st['n']}{st['worst']*100:>+8.1f}%"
                  f"   ({time.time()-t0:.0f}s)")
            rows.append(dict(seed=seed, size=size, is_ret=isr["total_return"],
                             is_sharpe=isr["sharpe"],
                             is_mdd=isr["max_drawdown"], oos_sharpe=st["mean_sharpe"],
                             d_sharpe=dsh, oos_ret=st["mean_ret"], oos_mdd=st["mean_mdd"],
                             pos=st["pos"], n=st["n"], worst=st["worst"],
                             rets=st["rets"], bars=n))

    # ---- 跨 seed 汇总：真效应应在**每个 seed 上同号**，而非靠某个幸运种子 ----
    if rows:
        print(f"\n{'-' * 76}")
        print(f"{'宇宙N':<8}{'跨seed均值dSh':>16}{'最小':>10}{'最大':>10}"
              f"{'同号seed':>10}{'判定':>14}")
        for size in sizes:
            g = [r for r in rows if r["size"] == size]
            if not g:
                continue
            ds = [r["d_sharpe"] for r in g]
            mean = sum(ds) / len(ds)
            same = sum(1 for x in ds if (x > 0) == (mean > 0))
            verdict = ("噪声（跨seed异号）" if same < len(ds)
                       else ("正向" if mean >= 0.10 else "不足"))
            print(f"{size:<8}{mean:>+16.3f}{min(ds):>+10.3f}{max(ds):>+10.3f}"
                  f"{same:>7}/{len(ds)}{verdict:>14}")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"[保存] {args.json}")


if __name__ == "__main__":
    sys.exit(main())

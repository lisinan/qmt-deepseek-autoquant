# -*- coding: utf-8 -*-
"""PM-EVOLVE R29（2026-10-13 收官）候选 A：实盘宇宙 ≠ 回测宇宙 的量测与剂量。

来源：AM-EVOLVE R28 §9-4 标记的「下一步聚焦方向」——

    实盘订阅 = 静态 12 只 + 动态活跃池 30 只（374 只 AI 产业链股里按总市值
    Top-30，`data/dynamic_universe.py::_pick_active_pool`）；回测
    `wide_universe()` = 静态 12 + SECTOR_CONFIG 11 = **23 只**，且动态池
    在回测里**完全没建模** ⇒ 实盘是「38~42 选 3」，回测是「23 选 3」。

本脚本做两件事：

1. `--overlap`：量测背离本身（**不落盘、不改参数**）
   - 复刻回测动量闸门（60 日动量、> momentum_min_mom、取 Top-3）在
     「回测宇宙」与「实盘宇宙」两个候选集上各算一次，统计逐日
     Top-3 集合差异率 / 动态池股票入选占比。
2. `--ext`：把 18 只「实盘有、回测无」的标的加进回测宇宙，跑验证器
   （`--mode span` / `compare` / `consensus`），得到扩宇宙后的 IS/OOS，
   与基线相减即为本次候选的 dSh（**唯一决策依据**）。

口径纪律：
- 只追加**数据**，不改任何生产参数；面板来自 tushare 真实日线（缓存复用
  R28 的 `ts_panel_25_901_20211201_20261013.pkl`）。
- 上市晚的标的在上市前 `valid=False`（`align_panel` 前填 + 有效标记），
  不会产生前视成交。
- 不能复刻「按总市值每期重算 Top-30」（需 374×900 条 daily_basic，预算外），
  故本量测是**上界**口径：动态池成员按其**存续期**参与，而非按其历史市值排名。
  这一点在报告里显式标注。
"""
from __future__ import annotations

import pickle
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

END = "20261013"
START = "20211201"
COUNT = 901

CACHE_DIR = ROOT / "storage" / "hist_cache"
BASE_CACHE = CACHE_DIR / f"ts_panel_25_901_{START}_{END}.pkl"
UNION_CACHE = CACHE_DIR / f"ts_panel_union41_{START}_{END}.pkl"

# 动态活跃池 30 只（storage/dynamic_universe.json, 2026-10-13 19:46 刷新）
ACTIVE_POOL = [
    '601138.SH', '688981.SH', '300308.SZ', '688256.SH', '300502.SZ',
    '688041.SH', '002371.SZ', '688347.SH', '002415.SZ', '601869.SH',
    '688012.SH', '300394.SZ', '688008.SH', '603986.SH', '688072.SH',
    '688498.SH', '300604.SZ', '000063.SZ', '688082.SH', '688361.SH',
    '301308.SZ', '600487.SH', '002281.SZ', '688120.SH', '300857.SZ',
    '600584.SH', '603019.SH', '301165.SZ', '688702.SH', '600522.SH',
]


def base_codes() -> list:
    from config.settings import STOCK_CODES, SECTOR_CONFIG
    s = set(STOCK_CODES)
    for _k, v in SECTOR_CONFIG["sectors"].items():
        for c, _n in v["stocks"]:
            s.add(c)
    return sorted(s)


def extra_codes() -> list:
    return sorted(set(ACTIVE_POOL) - set(base_codes()))


def _subset(key: str) -> list:
    """把 18 只动态池标的拆成两组（按 ACTIVE_POOL 顺序交替），做**分半不变性检验**。

    判据：若 +dSh 只来自其中半组（即 1~2 只明星股），则是「挑中今日赢家」的
    幸存者偏差；若两半各自都给出可比的 +dSh，才是「宇宙构成」的真实效应。
    """
    ex = extra_codes()
    if key == "all":
        return ex
    if key == "ha":
        return [c for i, c in enumerate(ex) if i % 2 == 0]
    if key == "hb":
        return [c for i, c in enumerate(ex) if i % 2 == 1]
    return [c for c in ex if c in set(key.split(","))]


def build_panel(refresh: bool = False) -> dict:
    if UNION_CACHE.exists() and not refresh:
        return pickle.loads(UNION_CACHE.read_bytes())
    base = pickle.loads(BASE_CACHE.read_bytes())
    need = [c for c in extra_codes() if c not in base]
    print(f"[面板] 复用缓存 {len(base)} 只，需新拉 {len(need)} 只：{need}")

    from data.tushare_client import tushare_client as T
    import tushare as ts
    from scripts._pm_ts_panel_20261012 import fetch_one
    ts.set_token(T.token)
    pro = ts.pro_api(timeout=30)
    out = dict(base)
    for i, c in enumerate(need):
        df = fetch_one(pro, c, START, END)
        if df is None:
            print(f"  [skip] {c} 无 tushare 数据")
            continue
        df = df.sort_values("trade_date")
        out[c] = {
            "date": [str(x) for x in df["trade_date"].tolist()],
            "open": [float(x) for x in df["open"].tolist()],
            "high": [float(x) for x in df["high"].tolist()],
            "low": [float(x) for x in df["low"].tolist()],
            "close": [float(x) for x in df["close"].tolist()],
            "volume": [float(x) * 100.0 for x in df["vol"].tolist()],
        }
        print(f"  fetched {i+1}/{len(need)} {c} {len(out[c]['close'])} 根 "
              f"{out[c]['date'][0]}~{out[c]['date'][-1]}")
        time.sleep(0.35)
    UNION_CACHE.write_bytes(pickle.dumps(out))
    print(f"[面板] 已缓存 {len(out)} 只 -> {UNION_CACHE}")
    return out


# ---------------------------------------------------------------- 背离量测
def measure_overlap(panel: dict, lb: int = 60, min_mom: float = 0.15,
                    top_n: int = 3) -> dict:
    """复刻回测动量闸门，比较两个宇宙逐日 Top-3 集合。"""
    from strategy.backtest_daily import align_panel
    base_set = set(base_codes())
    ext_set = set(base_codes()) | set(ACTIVE_POOL)

    def top3(universe, src, dates):
        # src = 已对齐面板（含 valid 标记），长度与 dates 一致
        codes_u = [c for c in universe if c in src]
        n = len(dates)
        res = [None] * n
        moms = {}
        for code in codes_u:
            d = src[code]
            cc = d["close"]
            for i in range(lb, n):
                if cc[i] <= 0 or cc[i - lb] <= 0:
                    continue
                if not d.get("valid", [True] * n)[i]:
                    continue
                raw = cc[i] / cc[i - lb] - 1
                if raw <= min_mom:
                    continue
                moms.setdefault(i, []).append((raw, code))
        for i, lst in moms.items():
            lst.sort(reverse=True)
            res[i] = [c for _, c in lst[:top_n]]
        return res

    # 对齐面板（含 valid 标记）
    dates, apanel = align_panel({k: v for k, v in panel.items()
                                 if k in ext_set})
    n = len(dates)
    tb = top3(base_set, apanel, dates)
    te = top3(ext_set, apanel, dates)

    days = 0            # 两侧都至少产生过比较的日期（有 up 交易日）
    diff_days = 0
    ext_hit_days = 0
    ext_hits = 0
    nonempty = 0
    for i in range(n):
        a, b = tb[i], te[i]
        if a is None and b is None:
            continue
        days += 1
        a = a or []
        b = b or []
        if b:
            nonempty += 1
        if set(a) != set(b):
            diff_days += 1
        new = [c for c in b if c not in base_set]
        if new:
            ext_hit_days += 1
            ext_hits += len(new)
    return {
        "bars": n, "dates": (dates[0], dates[-1]),
        "compared_days": days, "nonempty_ext_days": nonempty,
        "top3_diff_days": diff_days,
        "top3_diff_rate": (diff_days / days) if days else None,
        "ext_stock_hit_days": ext_hit_days,
        "ext_stock_hit_rate": (ext_hit_days / days) if days else None,
        "ext_slots_per_day": (ext_hits / days) if days else None,
    }


def main() -> int:
    argv = sys.argv[1:]
    panel = build_panel(refresh="--refresh" in argv)
    if "--overlap" in argv:
        r = measure_overlap(panel)
        print("\n[背离量测] 回测宇宙(23) vs 实盘宇宙(41) 逐日 Top-3 动量集合")
        for k, v in r.items():
            print(f"  {k}: {v}")
        return 0

    # ---- 扩展宇宙跑验证器 ----
    import strategy._evolve_wf as W
    from config.settings import MARKET_INDEX_CODE

    subset = "all"
    if "--extra" in argv:
        subset = argv[argv.index("--extra") + 1]
    ex = _subset(subset)
    _base = base_codes()
    if "--live" in argv:
        # 纯实盘等价宇宙 = 静态订阅（始终在池） + 动态池成员（按 tenure 切片）
        from config.settings import STOCK_CODES
        _base = sorted(set(STOCK_CODES))
    universe = _base + [c for c in ex if c not in _base]
    print(f"[宇宙] base {len(_base)} + extra {len(ex)} = {len(universe)}"
          f"（subset={subset}{' live等价' if '--live' in argv else ''}）")
    W.wide_universe = lambda: list(universe)

    # ---- 无前视成员口径（--mv）：动态池标的只从「历史首次进入 total_mv Top-30」
    # 之后才存在（切片掉入池前的历史），其余完全不变。
    mv_entry = {}
    if "--mv" in argv or "--pool62" in argv:
        import pickle as _pk
        mem = _pk.loads((CACHE_DIR / "mv_member_20261013.pkl").read_bytes())
        mv_entry = mem["first_hit"]
        print(f"[成员口径] 按历史 total_mv Top-30 重建，{len(mv_entry)} 只曾入池")
        if "--pool62" in argv:
            # 历史**全部**进过池的标的（含后来缩水的），每只按入池日切片
            ex = [c for c in sorted(mv_entry) if c not in _base]
            universe = _base + ex
            print(f"[宇宙] 历史池全集：base {len(_base)} + 成员 {len(ex)}"
                  f" = {len(universe)}")
    if "--panel" in argv:
        pkey = argv[argv.index("--panel") + 1]
        if pkey == "pool62":
            panel = pickle.loads(
                (CACHE_DIR / "ts_panel_pool62_20211201_20261013.pkl").read_bytes())
            print(f"[面板] pool62：{len(panel)} 只")

    def _fake_load_daily(code: str, count: int = 260):
        d = panel.get(code)
        if not d:
            return None
        n = len(d["close"])
        k = min(n, max(count, 120))
        out = {kk: list(vv[-k:]) for kk, vv in d.items()}
        if mv_entry and code in ex:
            ent = mv_entry.get(code)
            if not ent:
                return None            # 历史上从未进入 Top-30 ⇒ 该标的当时不可交易
            dts = out["date"]
            if not dts or dts[-1] < ent:
                return None
            j = 0
            for j, dt in enumerate(dts):
                if dt >= ent:
                    break
            out = {kk: list(vv[j:]) for kk, vv in out.items()}
        return out

    W.load_daily = _fake_load_daily
    W._CACHE.clear()
    a = [x for x in argv if not x.startswith("--refresh")]
    # 去掉本脚本私有参数，其余原样透传给 _evolve_wf.main
    for flag in ("--extra", "--panel"):
        if flag in a:
            i = a.index(flag)
            a = a[:i] + a[i + 2:]
    a = [x for x in a if x not in ("--mv", "--pool62", "--live")]
    if not a:
        a = ["--mode", "span"]
    if "--count" not in a:
        a += ["--count", str(COUNT)]
    sys.argv = ["_evolve_wf.py"] + a
    return W.main()


if __name__ == "__main__":
    sys.exit(main())

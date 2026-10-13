# -*- coding: utf-8 -*-
"""PM-EVOLVE R29/R30 候选 B：跨行业分散化（用户 4 机制之机制 4：23 只 AI 高度同向，
不是 23 个独立样本 ⇒ 回测 Sharpe 被高估，本质是板块 bet 的重复）。

本脚本测一个**与历史「扩宇宙（5 种子均负）」本质不同的假设**：

  历史「扩宇宙」= 在 AI 产业链内加更多票（同板块 ⇒ 相关性更高 ⇒ 反而更差）。
  本假设 = 加入**与 AI 硬件链低相关**的其他行业龙头，让动量 Top-3 选择器
          在 AI 回调时也能切换到别的趋势，从而：
            (a) 降低组合相关性（机制 4：真实独立性↑，Sharpe 更可信）；
            (b) 减少空仓拖累（机制 2：AI 弱市时有别的趋势可持，暴露↑）。
          若分散化带来「同等收益 + 更低波动」⇒ OOS Sharpe 改善且回撤更可控。

口径纪律：
- 只追加**数据**，不改任何生产参数；面板来自 tushare 真实日线。
- 上市晚的标的在上市前 valid=False，不产生前视成交。
- 不落盘：本脚本只产出 dSh 供裁决；是否落盘由 OWNER / 闸门决定。
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
DIV_CACHE = CACHE_DIR / f"ts_panel_diversify_{START}_{END}.pkl"

# 与 AI 硬件链低相关的行业龙头（分散化候选；非 AI 产业链）
DIVERSE = [
    "600519.SH",  # 贵州茅台   白酒/消费
    "000858.SZ",  # 五粮液     白酒
    "600276.SH",  # 恒瑞医药   医药创新药
    "300760.SZ",  # 迈瑞医疗   医疗器械
    "600036.SH",  # 招商银行   银行
    "601318.SH",  # 中国平安   保险
    "600900.SH",  # 长江电力   公用事业
    "000333.SZ",  # 美的集团   家电
    "600760.SH",  # 中航沈飞   军工
    "600887.SH",  # 伊利股份   食品饮料
    "601398.SH",  # 工商银行   银行（大行，与招行互补）
    "601888.SH",  # 中国中免   免税/旅游消费
]


def base_codes() -> list:
    from config.settings import STOCK_CODES, SECTOR_CONFIG
    s = set(STOCK_CODES)
    for _k, v in SECTOR_CONFIG["sectors"].items():
        for c, _n in v["stocks"]:
            s.add(c)
    return sorted(s)


def build_panel(refresh: bool = False) -> dict:
    if DIV_CACHE.exists() and not refresh:
        return pickle.loads(DIV_CACHE.read_bytes())
    base = pickle.loads(BASE_CACHE.read_bytes())
    need = [c for c in DIVERSE if c not in base]
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
    DIV_CACHE.write_bytes(pickle.dumps(out))
    print(f"[面板] 已缓存 {len(out)} 只 -> {DIV_CACHE}")
    return out


def main() -> int:
    argv = sys.argv[1:]
    panel = build_panel(refresh="--refresh" in argv)

    import strategy._evolve_wf as W
    _base = base_codes()
    universe = _base + [c for c in DIVERSE if c not in _base]
    print(f"[宇宙] base {len(_base)} + diverse {len(DIVERSE)} = {len(universe)}")
    W.wide_universe = lambda: list(universe)

    def _fake_load_daily(code: str, count: int = 260):
        d = panel.get(code)
        if not d:
            return None
        n = len(d["close"])
        k = min(n, max(count, 120))
        return {kk: list(vv[-k:]) for kk, vv in d.items()}

    W.load_daily = _fake_load_daily
    W._CACHE.clear()
    a = [x for x in argv if not x.startswith("--refresh")]
    if not a:
        a = ["--mode", "span"]
    if "--count" not in a:
        a += ["--count", str(COUNT)]
    sys.argv = ["_evolve_wf.py"] + a
    return W.main()


if __name__ == "__main__":
    sys.exit(main())

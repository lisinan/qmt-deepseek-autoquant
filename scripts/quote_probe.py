# -*- coding: utf-8 -*-
"""tushare 行情源只读探针（S 阶段：不碰交易、不下单、不改任何状态）。

用途
----
1. 周一开盘前后各跑一次，确认 ``rt_tick`` 在**盘中**真的按秒刷新
   （休市时只能验证数据正确性，不能验证实时性 —— 2026-11-08 首次实测的局限）。
2. 核对沪市 ETF（510050.SH）是否由新浪兜底补上。
3. 打印 ``get_history("1d")`` 末几根，肉眼对照券商软件的收盘价。
4. 打印限频余量与节流状态，确认不会撞『频率超限』。

用法
----
    python scripts/quote_probe.py                 # 默认宇宙：持仓 + 观察篮 + 基准
    python scripts/quote_probe.py 300308.SZ 510050.SH
    python scripts/quote_probe.py --json          # 机器可读

退出码：0 正常 / 2 行情源不可用 / 3 数据缺失（能连但拿不到标的）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 默认标的：实盘持仓 + 观察篮 + 基准指数（都是引擎真正会用的）
DEFAULT_CODES = ["300308.SZ", "510050.SH", "000001.SH", "000300.SH"]


def main(argv: list) -> int:
    as_json = "--json" in argv
    args = [a for a in argv if not a.startswith("-")]
    codes = args or DEFAULT_CODES

    from core.tushare_quote import TushareQuoteClient

    try:
        client = TushareQuoteClient()
    except Exception as e:                            # noqa: BLE001
        print(f"[FAIL] 行情源不可用: {type(e).__name__}: {e}")
        return 2

    health = client.health()
    # 先取一次再自述：health() 里的「缓存标的数 / 距上次抓取」要在抓取之后才有意义
    ticks = client.get_ticks(codes)
    health = client.health()

    hist = {}
    for c in codes:
        bars = client.get_history(c, "1d", 5)
        hist[c] = [
            {"ts": b["ts"].strftime("%Y-%m-%d"), "close": b["close"],
             "volume": b["volume"]}
            for b in (bars or [])
        ]

    missing = [c for c in codes if c not in ticks]
    payload = {
        "health": health,
        "requested": codes,
        "got": sorted(ticks),
        "missing": missing,
        "ticks": ticks,
        "history_tail": hist,
    }

    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0 if ticks else 3

    print("=" * 72)
    print("tushare 行情源 · 只读探针")
    print("=" * 72)
    print(f"模式            : {health['mode']}")
    print(f"rt_tick 可用    : {health['rt_tick_ok']}")
    print(f"限频余量(小时)  : {health['rt_tick_used_1h']}/{health['rt_tick_limit_1h']} 次已用")
    print(f"限频余量(全天)  : {health['rt_tick_used_1d']}/{health['rt_tick_limit_1d']} 次已用")
    print(f"节流窗口        : {health['min_refresh_sec']}s")
    print(f"缓存标的数      : {health['cached_codes']}")
    _age = health.get("last_fetch_age_sec")
    print(f"距上次抓取      : {'—' if _age is None else f'{_age}s'}")
    if health["backoff_sec"]:
        print(f"⚠ 静默中        : 还有 {health['backoff_sec']}s（撞过频率超限）")

    print("-" * 72)
    hdr = f"{'代码':<12}{'最新价':>10}{'开盘':>10}{'最高':>10}{'最低':>10}{'昨收':>10}  {'陈旧':<8}{'行情时间':<20}"
    print(hdr)
    for c in codes:
        t = ticks.get(c)
        if not t:
            print(f"{c:<12}{'—— 缺失 ——':>50}")
            continue
        from datetime import datetime as _dt
        ts_txt = ""
        if t.get("time"):
            try:
                ts_txt = _dt.fromtimestamp(t["time"] / 1000.0).strftime("%m-%d %H:%M:%S")
            except Exception:
                ts_txt = str(t["time"])
        age = t.get("stale_age_sec")
        stale_txt = "陈旧" if t.get("_stale") else ("新鲜" if age is not None else "未知")
        print(f"{c:<12}{t['lastPrice']:>10.3f}{t['open']:>10.3f}{t['high']:>10.3f}"
              f"{t['low']:>10.3f}{t['lastClose']:>10.3f}  {stale_txt:<8}{ts_txt:<20}"
              f"{'' if age is None else f'({age:.0f}s)'}")

    print("-" * 72)
    print("日线末 3 根（对照券商软件收盘价）：")
    for c in codes:
        rows = hist.get(c) or []
        if not rows:
            print(f"  {c:<12} —— 无日线 ——")
            continue
        tail = "  ".join(f"{r['ts']}:{r['close']}" for r in rows[-3:])
        print(f"  {c:<12} {tail}")

    print("-" * 72)
    if missing:
        print(f"⚠ 缺失 {len(missing)} 只：{missing}")
        print("  （若含 510050.SH 这类 ETF，说明新浪兜底也失效了，需立刻告知 OWNER）")
    else:
        print("全部标的均有报价。")
    print()
    print("提示：休市时段『陈旧』是正常的；要验证盘中实时性，请在 09:35 后再跑一次，")
    print("      看『行情时间』是否随真实时钟推进、陈旧秒数是否在 10s 以内。")
    return 0 if ticks else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

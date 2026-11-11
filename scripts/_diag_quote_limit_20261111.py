# -*- coding: utf-8 -*-
"""实测 tushare 两个实时行情接口的真实频率上限（2026-11-11 盘后诊断）。

## 为什么要有这个脚本

2026-11-11 13:15 起 live 行情冻结，根因是 `core/tushare_quote.py:244` 把
`RT_TICK_PER_MIN` 写成 10（次/分钟），而 tushare 的真实限制是

    「抱歉，您访问接口(rt_tick)频率超限(100次/小时)」

单次调用实测原文见 `reports/` 里的记录。10 次/分钟 与 100 次/小时 差 6 倍，
8 秒节流（7.5 次/分钟 = 450 次/小时）⇒ **开盘 13 分钟就把一小时额度打穿**，
之后进入「撞限-静默 60s-再撞限」死循环，`_refresh` 又只给「无缓存」标的补新浪
（`tushare_quote.py:521`），38 只全有缓存 ⇒ 价格全冻 ⇒ 13:17「已暂停建仓」。

改代码之前必须先知道**另一条路** `realtime_quote` 的上限是多少，否则修完只是
把问题从一个接口搬到另一个接口。本脚本就是量这件事。

## 用法（必须绕代理：本机 127.0.0.1:7897 是 Clash，关掉后连不上）

    export PATH=/c/Users/lisinan/.conda/envs/qmt/Library/bin:/c/Users/lisinan/.conda/envs/qmt/DLLs:$PATH
    NO_PROXY="api.waditu.com,waditu.com" no_proxy="api.waditu.com,waditu.com" \
    /c/Users/lisinan/.conda/envs/qmt/python.exe scripts/_diag_quote_limit_20261111.py

## 输出

逐次打印 OK/ERR 与累计耗时；撞到第一次错误就停，并打印**原文**（上限就写在里面）。
两个接口各量一遍，结果写进 `reports/QUOTE_LIMIT_20261111.md`。
"""
from __future__ import annotations

import os
import sys
import time

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_k, None)
os.environ.setdefault("NO_PROXY", "api.waditu.com,waditu.com")
os.environ.setdefault("no_proxy", "api.waditu.com,waditu.com")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import TUSHARE_TOKEN  # noqa: E402

import tushare as ts  # noqa: E402

MAX_CALLS = int(os.environ.get("PROBE_MAX_CALLS", "200"))
INTERVAL = float(os.environ.get("PROBE_INTERVAL", "2.0"))


def probe(name: str, fn, code: str) -> dict:
    """连续打同一个接口，直到报错。返回 {n, elapsed, err}。"""
    print("\n=== %s（间隔 %.1fs，最多 %d 次）===" % (name, INTERVAL, MAX_CALLS),
          flush=True)
    t0 = time.time()
    n = 0
    for i in range(1, MAX_CALLS + 1):
        try:
            df = fn(code)
            n = i
            rows = 0 if df is None else len(df)
            if i <= 3 or i % 20 == 0:
                print("  #%d OK rows=%d  t=%.0fs" % (i, rows, time.time() - t0),
                      flush=True)
        except Exception as e:                       # noqa: BLE001
            elapsed = time.time() - t0
            err = str(e)[:300]
            print("  #%d ERR  t=%.0fs" % (i, elapsed), flush=True)
            print("  原文: %s" % err, flush=True)
            return {"n": n, "elapsed": elapsed, "err": err}
        time.sleep(INTERVAL)
    elapsed = time.time() - t0
    print("  %d 次全成功，未撞限（t=%.0fs）" % (n, elapsed), flush=True)
    return {"n": n, "elapsed": elapsed, "err": ""}


def main() -> int:
    ts.set_token(TUSHARE_TOKEN)
    pro = ts.pro_api()

    res = {}
    res["rt_tick"] = probe("pro.rt_tick", lambda c: pro.rt_tick(ts_code=c), "000001.SZ")
    # realtime_quote 是模块级函数，不走 pro
    res["realtime_quote"] = probe("ts.realtime_quote",
                                  lambda c: ts.realtime_quote(ts_code=c), "510050.SH")

    print("\n=== 汇总 ===", flush=True)
    for k, v in res.items():
        print("%-16s 成功 %3d 次 / 耗时 %.0fs / err=%s"
              % (k, v["n"], v["elapsed"], (v["err"] or "无")[:120]), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

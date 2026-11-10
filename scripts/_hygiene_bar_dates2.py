# -*- coding: utf-8 -*-
"""量测卫生巡检 #1b：有效候选集（静态 ∪ 动态池）日线末根日期（只读）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import INDEX_CODES, STOCK_CODES, VALIDATION_UNIVERSE  # noqa: E402


def _bars_via_tushare(codes, days: int = 5):
    """tushare 兜底：返回 {code: 末根交易日 'YYYYMMDD' | None}；不可用返回 None。

    用 `daily(trade_date=)` / `fund_daily(trade_date=)` 的**按日全市场**口径，
    而不是逐票拉 —— 后者 38 只就要 38 次请求，容易撞限频且慢。
    """
    try:
        from data.tushare_client import tushare_client
    except Exception as e:  # noqa: BLE001
        print(f"[tushare] 导入失败: {e}")
        return None
    if not getattr(tushare_client, "enabled", False) or not tushare_client._connect():
        print("[tushare] 未启用或未连接（token 为空 / 网络不可用）")
        return None
    pro = getattr(tushare_client, "_pro", None)
    if pro is None:
        print("[tushare] _pro 不存在")
        return None

    import datetime as _dt
    from data.tushare_client import _to_ts_code

    # 候选交易日：从今天往前取 days 个工作日（法定假日会多试几次，拿不到即空）
    cand, d0 = [], _dt.date.today()
    while len(cand) < days:
        if d0.weekday() < 5:
            cand.append(d0.strftime("%Y%m%d"))
        d0 -= _dt.timedelta(days=1)

    want = {_to_ts_code(c): c for c in codes}
    found: dict[str, str] = {}
    for d in cand:                     # 从新到旧，先命中的即末根
        for api, label in (("daily", "股票"), ("fund_daily", "ETF/基金")):
            try:
                df = getattr(pro, api)(trade_date=d)
            except Exception as e:  # noqa: BLE001
                print(f"[tushare] {api}({d}) 失败: {str(e)[:80]}")
                continue
            if df is None or df.empty:
                continue
            for ts_code in df["ts_code"].tolist():
                code = want.get(ts_code)
                if code and code not in found:
                    found[code] = d
            print(f"[tushare] {d} {label} {len(df)} 条")
        if len(found) == len(want):
            break
    return {c: found.get(_to_ts_code(c)) for c in codes}


def main() -> int:
    from xtquant import xtdata

    dyn_path = ROOT / "storage" / "dynamic_universe.json"
    active = []
    if dyn_path.exists():
        d = json.loads(dyn_path.read_text(encoding="utf-8"))
        active = list(d.get("active_pool") or [])

    # 【2026-11-06 DAILY-REVIEW SAFE 修复】原检查池漏掉 VALIDATION_UNIVERSE。
    # 后果：510050.SH 是唯一"只靠 tick、不在 _daily_codes() 里"的标的，其日线
    # 自 2026-11-03 起结构性断更（今日实测真实末根 20261103，落后 3 个交易日），
    # 而本脚本只查 37 只 ⇒ 断更对巡检**完全不可见**，连续 4 天报"全部最新"。
    # 回滚：删掉下面 VALIDATION_UNIVERSE 一项即可（纯观测脚本，不受控于引擎）。
    # 注意：fill_data=False 必须保留 —— 默认 True 会给缺失标的**前值填充假 bar**
    # （实测批量取数时 510050 被补出 11-04/05/06 三根 2.884 的假收盘，dropna 也拦不住）。
    codes = list(dict.fromkeys(list(sorted(STOCK_CODES)) + active
                               + list(sorted(INDEX_CODES))
                               + list(VALIDATION_UNIVERSE or ())))
    print(f"[有效集] 静态 {len(STOCK_CODES)} + 动态 {len(active)} + 指数 {len(INDEX_CODES)} "
          f"+ 验证 {len(VALIDATION_UNIVERSE or ())} → 去重 {len(codes)}")

    # 最近交易日（xtdata 交易日历）
    last_td = None
    try:
        cal = xtdata.get_trading_dates("SH", start_time="20260901", end_time="20261231")
        last_td = max(cal) if cal else None
        import datetime
        print("[交易日历] 最近交易日 =",
              datetime.datetime.fromtimestamp(last_td / 1000).strftime("%Y%m%d")
              if last_td else "未获取")
    except Exception as e:  # noqa: BLE001
        print(f"[交易日历] 未获取: {e}")

    import datetime
    from collections import defaultdict
    buckets: dict[str, list[str]] = defaultdict(list)

    # 【2026-11-10 DAILY-REVIEW SAFE 修复】xtdata 通道**结构性不可用**时整脚本抛
    # RuntimeError（`func:getTradingDatesByMarket ... not authenticated`），
    # 结果是「量测卫生」的日线末根这一条**当天完全取不到值**，报告只能写"未获取"。
    # 2026-11-08 起交易与行情都走「大 QMT + 文件桥 + tushare」，外部 python 连
    # xtdata 永远拿不到鉴权 ⇒ 这不是偶发超时，而是必然。故补 tushare 兜底：
    #   `pro.daily(trade_date=D)` 一次返回**全市场**当日 bar（含 ts_code），
    #   `pro.fund_daily(trade_date=D)` 覆盖 ETF（510050/510500 只能从这里取到）。
    #   每个候选交易日各一次调用 ⇒ 只 3~5 次网络请求，不像逐票拉那样打满限频。
    # 回滚：把 _bars_via_tushare 调用与下面的 except 分支删掉，恢复纯 xtdata 路径。
    from_xt = None
    try:
        data = xtdata.get_market_data_ex(
            field_list=["time", "close"], stock_list=codes, period="1d",
            count=5, dividend_type="none", fill_data=False,
        )
        from_xt = {}
        for code in codes:
            df = data.get(code)
            if df is None or len(df) == 0:
                from_xt[code] = None
                continue
            ts = float(df.iloc[-1]["time"])
            from_xt[code] = datetime.datetime.fromtimestamp(
                ts / 1000).strftime("%Y%m%d")
    except Exception as e:  # noqa: BLE001
        print(f"[xtdata] 不可用，改用 tushare 兜底: {type(e).__name__}: {e}")

    if from_xt is None:
        from_xt = _bars_via_tushare(codes, days=5)
        if from_xt is None:
            print("[末根日期] 未获取：xtdata 与 tushare 均不可用（见上方原因）")
            return 3

    for code in codes:
        d = from_xt.get(code)
        buckets[d or "NO_DATA"].append(code)

    for d in sorted(buckets):
        names = buckets[d]
        print(f"[{d}] {len(names)} 只: {', '.join(names) if len(names) <= 40 else str(len(names))+'只'}")

    # 【2026-11-06 DAILY-REVIEW SAFE】把「落后几个交易日」显式算出来 —— 原来只分组打印，
    # 落后标的混在另一个日期桶里，人眼扫不出来，正是 510050 断更 4 天没人发现的原因。
    # 【2026-11-10】xtdata 日历不可用时（大 QMT 下必然），基准改用「各票末根的最大值」，
    # 否则整段落后汇总被静默跳过 ⇒ 断更再次不可见。
    if not last_td:
        _dates = [d for d in buckets if d != "NO_DATA"]
        if _dates:
            print(f"[交易日历] xtdata 不可用，基准改用样本内最新末根 = {max(_dates)}")
            last_td = max(_dates)          # 已是 YYYYMMDD 字符串
    if last_td:
        if isinstance(last_td, str):
            want = last_td
        else:
            want = datetime.datetime.fromtimestamp(last_td / 1000).strftime("%Y%m%d")
        lag = {d: v for d, v in buckets.items() if d != want}
        if lag:
            print(f"[落后] 相对最近交易日 {want}："
                  + "; ".join(f"{d} → {len(v)} 只（{', '.join(v)}）" for d, v in sorted(lag.items())))
        else:
            print(f"[落后] 无，全部 {len(codes)} 只末根 = {want}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""只读诊断：2026-11-03 账本/量测审计（D1 表格 + D4 xtdata 日线末端新鲜度）。

★ 只读：不改任何生产文件/参数，只 print。
   用法: python scripts/_audit_20261103_d1_d4.py

★ 退出码契约（2026-11-04 定，D4 尤其重要）：
    0 = 跑了且干净
    2 = 跑了，但结论不可用（订阅只数对不上 / 有标的完全无日线）
    3 = **没跑**（xtdata 不可达，D4 未执行）
    1 = 跑了但工具自身抛异常（traceback）
  四者永不混用：把「没跑」记成 0 会被读成健康；把「不可达」抛成 traceback
  又会被当成工具 bug 而无人追。CI 判 `!= 0` 一律告警。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LEDGER = ROOT / "logs" / "evolution_ledger.jsonl"

# D4 日线查询窗口起点。**不得少于 60 个交易日**：bars 是窗口的函数，收窄会让
# 「MA60 可行性」在数据没变的情况下翻脸（2026-11-04 审计官指出）。
DAILY_WINDOW_START = "20250101"


def d1() -> None:
    rows = [json.loads(l) for l in LEDGER.read_text(encoding="utf-8").splitlines() if l.strip()]
    print("=" * 96)
    print("D1 账本逐行体检（全部行；>=2026-10-29 为 live 关注段）")
    print("=" * 96)
    hdr = f"{'date':<11}{'mode':<7}{'eod_asset':>12}{'prev_asset':>12}{'ret%':>8}{'range%':>8}{'eod_ts':<21}{'close':<6}"
    print(hdr)
    prev_eod = None
    prev_date = None
    for r in rows:
        mode = r.get("mode", "<缺>")
        ts = r.get("eod_ts", "<缺>")
        cl = r.get("eod_is_close", "<缺>")
        ea = r.get("eod_asset")
        pa = r.get("prev_asset")
        flag = ""
        if prev_eod is not None and pa is not None and r["date"] >= "2026-10-29":
            if abs(pa - prev_eod) > 0.02:
                flag = f"  <<< 链条断: prev={pa} != 上一行eod={prev_eod}"
        print(f"{r['date']:<11}{str(mode):<7}"
              f"{ea if ea is not None else 'None':>12}{pa if pa is not None else 'None':>12}"
              f"{r.get('daily_ret_pct') if r.get('daily_ret_pct') is not None else 'None':>8}"
              f"{r.get('range_pct') if r.get('range_pct') is not None else 'None':>8}"
              f"{str(ts):<21}{str(cl):<6}{flag}")
        prev_eod = ea
        prev_date = r["date"]
    print(f"\n总行数={len(rows)}  末行日期={rows[-1]['date']}")
    has_1103 = any(r["date"] >= "2026-11-03" for r in rows)
    print(f"是否已有 2026-11-03 行: {has_1103}")
    miss_mode = [r["date"] for r in rows if "mode" not in r]
    print(f"缺 mode 字段行数={len(miss_mode)}  日期范围={miss_mode[0]}..{miss_mode[-1] if miss_mode else '-'}")
    print(f"缺 mode 且日期>=2026-10-29 的行: {[d for d in miss_mode if d >= '2026-10-29'] or '无'}")
    bad_close = [(r["date"], r.get("eod_is_close", "<缺>")) for r in rows
                 if r["date"] >= "2026-10-29" and r.get("eod_is_close") is not True]
    print(f"live 段 eod_is_close != true 的行: {bad_close}")


def _subscription_codes() -> tuple[list, dict]:
    """按**引擎自己的实现**取订阅清单，不在本脚本里重写一遍。

    ``EventEngine._subscribe_codes()``（event_engine.py:693）是订阅清单的单一事实源：
        UNIVERSE(16) + VALIDATION_UNIVERSE(1) + dynamic.active_codes(30) + 当前持仓
    这里用一个 shim 直接调用该方法 —— 不构造 EventEngine（避免连接/线程等副作用），
    也不复制它的拼接逻辑（本项目反复踩过「同一事实两份实现必然漂移」）。

    【2026-11-04 修复】原实现 import 的是 ``STOCK_CODES``(12)，那是候选池的静态
    子集、**不是**订阅清单；再加 ``DYNAMIC_UNIVERSE_CODES``（不存在）被 except 吞成
    ``[]`` ⇒ 只覆盖 13 只却「看起来正常」，而引擎实订阅 38 只。
    """
    import sqlite3
    from types import SimpleNamespace

    from config.settings import UNIVERSE, VALIDATION_UNIVERSE
    import engine.event_engine as EE

    pool = []
    jf = ROOT / "storage" / "dynamic_universe.json"
    if jf.exists():
        try:
            pool = list(json.loads(jf.read_text(encoding="utf-8")).get("active_pool") or [])
        except Exception as e:                       # 不再静默
            print(f"  [WARN] 读 {jf.name} 失败：{type(e).__name__}: {e}（动态池按空处理）")

    positions = {}
    db = ROOT / "storage" / "qmt.db"
    if db.exists():
        try:
            con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT positions FROM engine_state WHERE id=1").fetchone()
            con.close()
            for p in (json.loads(row["positions"]) if row and row["positions"] else []):
                q = int(p.get("quantity") or 0)
                if q > 0:
                    positions[p["code"]] = SimpleNamespace(quantity=q)
        except Exception as e:
            print(f"  [WARN] 读 engine_state 持仓失败：{type(e).__name__}: {e}")

    shim = SimpleNamespace(
        dynamic_universe=SimpleNamespace(active_codes=pool),
        _positions=positions,
    )
    codes = EE.EventEngine._subscribe_codes(shim)
    return codes, {"UNIVERSE": len(UNIVERSE), "VALIDATION": len(VALIDATION_UNIVERSE),
                   "dynamic_active": len(pool), "positions": len(positions)}


def _log_subscription_count() -> int | None:
    """从引擎日志抓最后一条「分钟线预热完成…（共 N 只）」。

    断言锚点用**日志里的实际订阅数**，不在本脚本里把期望值写死：宇宙一变，
    写死的数字要么静默过、要么静默炸（同型第 24 条）。
    """
    import re
    lf = ROOT / "logs" / "quant_system.log"
    if not lf.exists():
        return None
    pat = re.compile(r"分钟线预热完成.*?共\s*(\d+)\s*只")
    last = None
    with lf.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            m = pat.search(line)
            if m:
                last = int(m.group(1))
    return last


def _fetch_daily(codes, start: str, end: str, timeout: float = 20.0):
    """取日线；把「xtdata 不可达」与「工具自身坏了」区分开。

    退出码契约（team-lead / data-ledger-auditor-2 2026-11-04 定）：
        0 = 跑了且干净；3 = **没跑**（xtdata 不可达）；1 = 跑了但工具坏了。
    绝不混用 —— 把「没跑」记成 0 会被读成健康；把「不可达」抛成 traceback
    又会被当成工具 bug 而无人追（人会对 traceback 脱敏）。

    返回 (ok, data_or_reason)。**仅当异常属于「代码自身写错」时才向上抛**，
    保证这种情况仍是 traceback + exit 1。
    """
    import threading
    box = {}

    def _work():
        try:
            from xtquant import xtdata          # 正确入口；顶层 `xtdata` 不存在
            box["data"] = xtdata.get_local_data(
                field_list=["time", "close"], stock_list=codes, period="1d",
                start_time=start, end_time=end, fill_data=False)
        except BaseException as e:              # noqa: BLE001 —— 要区分类型
            box["err"] = e

    t = threading.Thread(target=_work, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return False, f"xtdata 无响应（>{timeout:.0f}s 超时）"
    if "err" in box:
        e = box["err"]
        if isinstance(e, (ImportError, AttributeError, TypeError, KeyError, ValueError)):
            raise e                              # 代码/环境写错 ⇒ traceback + exit 1
        return False, f"{type(e).__name__}: {e}"  # 连接失败等 ⇒ SKIP + exit 3
    return True, box["data"]


def _probe_xtdata(timeout: float = 5.0) -> bool:
    """只验证「xtdata 能不能取到数」，不 import engine。

    为什么必须先探：``import engine.event_engine`` **本身就会连接 xtdata**
    （实测 0.89s，并在 stdout 打连接横幅）—— 它是模块级 singleton 的副作用。
    所以若先 import engine，xtdata 没开时会在那一行炸成 traceback，
    ``[SKIP]`` 分支根本来不及执行，而且「行情没开 / xtquant 没装 / engine 有语法错」
    三种情况长得一模一样 —— 恰是退出码契约要避免的不可区分。

    ``from xtquant import xtdata`` 本身**不连接**（实测），连接发生在
    ``get_local_data``。故用一次最小取数作为探针。
    """
    ok, _ = _fetch_daily(["000001.SH"], "20261101", "20261104", timeout)
    return ok


def d4() -> None:
    from datetime import datetime
    from collections import Counter

    print()
    print("=" * 96)
    log_n = _log_subscription_count()
    print(f"D4 xtdata 日线末端日期（引擎日志锚点 {log_n if log_n is not None else '?'} 只）")
    print("=" * 96)
    # ① 先探行情：不可达 ⇒ 没跑（exit 3），且必须在 import engine 之前
    if not _probe_xtdata():
        print("  [SKIP] xtdata 不可达，D4 未执行（非健康）")
        print(f"覆盖只数: 0 / {log_n if log_n is not None else '?'}（未执行）")
        raise SystemExit(3)

    # ② 行情可达 ⇒ 再取订阅清单（import engine 此时不会因「行情没开」而炸）
    codes, parts = _subscription_codes()
    print(f"  构成：UNIVERSE={parts['UNIVERSE']} + VALIDATION={parts['VALIDATION']} "
          f"+ dynamic_active={parts['dynamic_active']} + 持仓={parts['positions']} "
          f"⇒ 去重 {len(codes)}（日志锚点 {log_n}）")
    if log_n is None:
        print("  [WARN] 日志里没有「分钟线预热完成…共 N 只」→ 无法交叉校验只数")
    elif len(codes) != log_n:
        print(f"  [FAIL] 订阅只数对不上：本脚本算出 {len(codes)} 只，"
              f"引擎日志记录 {log_n} 只 —— 覆盖率不正确，下面的结论不可用。")
        print(f"         实算集合（{len(codes)}）：{codes}")
        raise SystemExit(2)

    # 窗口要足够长：既判「末端是否新鲜」，也要能支撑 MA60 可行性判断。
    # 原来 start=20260901 只取到 ~40 根，读者会误以为「bars=40 ⇒ MA60 不可算」。
    # ⚠ 该常量**不得少于 60 个交易日**：否则「MA60 可行性」会在数据没变的情况下翻脸
    #   （bars 是窗口的函数，不是数据质量的函数）。
    start, end = DAILY_WINDOW_START, "20261110"
    ok, data = _fetch_daily(codes, start, end)
    if not ok:
        print(f"  [SKIP] xtdata 取数失败，D4 未完成（非健康）—— {data}")
        print(f"覆盖只数: 0 / {len(codes)}（未执行）")
        raise SystemExit(3)
    cnt = Counter()
    for c in codes:
        df = data.get(c)
        last = None
        if df is not None and len(df):
            last = datetime.fromtimestamp(int(df["time"].iloc[-1]) / 1000).strftime("%Y%m%d")
        cnt[last] += 1
        print(f"{c:<12} last_daily={last}  bars={0 if df is None else len(df)}")
    print("\n末端日期分布:", dict(cnt))
    print(f"覆盖只数: {len(codes)} / 引擎日志锚点 {log_n}（一致）")
    zero = [c for c in codes if data.get(c) is None or len(data.get(c)) == 0]
    if zero:
        # 跑了、但结论不可用 ⇒ 2（与「覆盖只数不符」同一语义）。
        # 不能用 3：3 专指「没跑」，混用会让「没跑」和「跑了发现坏数据」不可区分。
        print(f"[FAIL] 完全无日线数据的标的（{len(zero)}）：{zero}")
        raise SystemExit(2)
    # MA60 可行性（只告警，不判失败：本段的契约是「末端新鲜度」）
    short = [(c, 0 if data.get(c) is None else len(data.get(c))) for c in codes
             if data.get(c) is None or len(data.get(c)) < 60]
    print(f"MA60 可行性: {len(codes) - len(short)}/{len(codes)} 只 bars ≥ 60")
    if short:
        print(f"[WARN] bars<60（MA60 不可算）：{short}")


if __name__ == "__main__":
    d1()
    d4()

# -*- coding: utf-8 -*-
"""开盘接口能力预验证 + 实盘数据准确性对账（纯只读）。

## 为什么要有这个脚本

2026-11-08 的教训：live 进程活着、面板有数，但 `data_mode=mock` + `broker=disconnected`
+ `halted=true` + `asset_trusted=false` —— **四个字段同时错，任何一个都足以让成交量为零**，
而单独看每一个都像"小问题"。等到收盘才发现，已经没有补救窗口。

所以开盘前后跑一遍：把「接口能不能用」和「数据准不准」都变成可判定的结论。

## ★ 一条硬约束：不建立第二个交易会话

`xttrader` 的 session 是**按账号**独占的。脚本里再 `XtQuantTrader(...).start().connect()`
就会和引擎抢同一个 session（`trading_config.json::session_id`），轻则引擎被踢下线，
重则下单通道错乱 —— 对真实资金来说这是不可接受的风险。

所以交易侧**只做两件事**：
  1. 验证 `xtquant.xttrader / xttype` 的 API 面（模块在、类在、方法在）；
  2. 验证配置与桥接文件就绪；
  3. **连通性以引擎自报为准**（`/api/snapshot.broker_mode / broker_connected / broker_reconnects`）。

真正「能不能下单」只有引擎自己重启后连上才知道 —— 这脚本会明确把这条标成 SKIP 而不是假装通过。

## 用法

    python scripts/iface_probe.py               # 全量
    python scripts/iface_probe.py --json        # 机器可读（给自动化消费）
    python scripts/iface_probe.py --quiet       # 只打印非 PASS

退出码：0 = 无 FAIL；1 = 有 FAIL。

## 边界

只读：xtdata 只做 `get_full_tick` / `get_market_data_ex`（不 download、不 subscribe）；
SQLite 一律 `mode=ro`；HTTP 只 GET；**不写任何库、不下任何单、不改任何配置**。
账户号一律脱敏输出。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from core.zone import zone_config  # noqa: E402

HTTP_TIMEOUT = 8.0
# 观察篮（OWNER 2026-11-05 裁决维持豁免）—— 白名单核验的重点对象
WATCH_CODES = ("510050.SH", "300308.SZ")

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
_ICON = {PASS: "✓", WARN: "!", FAIL: "✗", SKIP: "-"}

_rows: list = []


def add(level: str, sec: str, item: str, detail: str) -> None:
    _rows.append({"level": level, "sec": sec, "item": item, "detail": detail})


def _mask(v) -> str:
    s = str(v or "")
    return s if len(s) <= 4 else s[:2] + "***" + s[-2:]


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _ro(db: Path):
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)


# --------------------------------------------------------------------- A 行情
def probe_market() -> None:
    sec = "A 行情"
    try:
        from xtquant import xtdata
    except Exception as e:                                   # noqa: BLE001
        add(FAIL, sec, "模块导入", f"{type(e).__name__}: {e}")
        return
    add(PASS, sec, "模块导入", "xtquant.xtdata 可用")

    # 认证书
    authed = True
    try:
        tick = xtdata.get_full_tick(["510050.SH"])
    except Exception as e:                                   # noqa: BLE001
        authed = False
        msg = str(e)
        add(FAIL, sec, "get_full_tick",
            f"{type(e).__name__}: {msg[:160]}"
            + ("（not authenticated ⇒ 行情源不可用，引擎会退到 mock/tushare）"
               if "auth" in msg.lower() else ""))
    else:
        t = (tick or {}).get("510050.SH") or {}
        if not t:
            authed = False
            add(FAIL, sec, "get_full_tick", "返回空 ⇒ 未认证或标的不在订阅范围")
        else:
            add(PASS, sec, "get_full_tick",
                f"lastPrice={t.get('lastPrice')} t={t.get('timetag') or t.get('time')}")

    # 日线末根：白名单逐只核（用单码 count=-1，绝不用批量 —— 批量 fill_data 会造假日线）
    for code in WATCH_CODES:
        try:
            d = xtdata.get_market_data_ex(["close"], [code], period="1d",
                                          count=-1, fill_data=False)
            df = (d or {}).get(code)
            if df is None or len(df) == 0:
                add(WARN, sec, f"日线 {code}", "无本地缓存（未 download，属正常）")
                continue
            last_idx = str(df.index[-1])
            add(PASS, sec, f"日线 {code}",
                f"末根 {last_idx} close={float(df['close'].iloc[-1]):.3f}")
        except Exception as e:                               # noqa: BLE001
            add(WARN, sec, f"日线 {code}", f"{type(e).__name__}: {str(e)[:80]}")

    if not authed:
        add(WARN, sec, "结论", "行情接口不可用 ⇒ 引擎会降级；此时任何「按价成交」都不可信")


# --------------------------------------------------------------------- B 交易
def probe_trader(snap: dict | None) -> None:
    sec = "B 交易"
    # 1) API 面
    try:
        from xtquant.xttrader import XtQuantTrader                # noqa: F401
        from xtquant import xttype                                # noqa: F401
        from xtquant.xttrader import XtQuantTraderCallback        # noqa: F401
        add(PASS, sec, "API 面", "XtQuantTrader / xttype / Callback 可导入")
    except Exception as e:                                        # noqa: BLE001
        add(FAIL, sec, "API 面", f"{type(e).__name__}: {e}")
        return
    for m in ("order_stock", "cancel_order_stock", "query_stock_asset",
              "query_stock_positions", "query_stock_orders"):
        if hasattr(XtQuantTrader, m):
            add(PASS, sec, f"方法 {m}", "存在")
        else:
            add(FAIL, sec, f"方法 {m}", "缺失 ⇒ 该版本 xtquant 不兼容")

    # 2) 配置
    cfg_p = BASE_DIR / "trading_config.json"
    if not cfg_p.exists():
        add(FAIL, sec, "trading_config", "文件不存在")
    else:
        try:
            cfg = json.loads(cfg_p.read_text(encoding="utf-8"))
            mode = cfg.get("broker_qmt_mode")
            add(PASS if mode else WARN, sec, "broker_qmt_mode", str(mode))
            accs = cfg.get("accounts")
            n = len(accs) if isinstance(accs, (list, dict)) else 0
            add(PASS if n else WARN, sec, "accounts",
                f"{n} 个账号；session_id={cfg.get('session_id')}")
            # 值不打印，只报是否存在
            add(PASS if cfg.get("tushare_token") else WARN, sec, "tushare_token",
                "已配置" if cfg.get("tushare_token") else "未配置（tushare 源不可用）")
        except Exception as e:                                    # noqa: BLE001
            add(FAIL, sec, "trading_config", f"解析失败 {type(e).__name__}: {e}")

    # 3) 连通性 —— 以引擎自报为准，绝不另起会话
    if snap is None:
        add(SKIP, sec, "连通性", "引擎快照不可得")
    else:
        bm, bc = snap.get("broker_mode"), snap.get("broker_connected")
        rc = snap.get("broker_reconnects")
        if bc and bm not in (None, "disconnected"):
            add(PASS, sec, "连通性", f"引擎自报 broker_mode={bm} connected=True 重连={rc}")
        else:
            add(FAIL, sec, "连通性",
                f"引擎自报 broker_mode={bm} connected={bc} 重连={rc} ⇒ 当前无法下单")
    add(SKIP, sec, "下单实测", "本脚本不下单；passorder/order_stock 只能在引擎连上后于盘中验证")


# --------------------------------------------------------------------- C 文件桥
def probe_bridge() -> None:
    sec = "C 文件桥"
    root = Path(r"C:\pazq_qmt_bridge")
    if root.exists():
        files = sorted(p.name for p in root.iterdir())[:8]
        add(PASS, sec, "桥目录", f"{root} 存在，条目 {len(list(root.iterdir()))}：{files}")
    else:
        add(SKIP, sec, "桥目录", f"{root} 不存在（broker_qmt_mode 未切到 bridge 时属正常）")

    dep = Path(r"C:\pazq_qmt\python\QMTIDE_BRIDGE.py")
    if dep.exists():
        st = dep.stat()
        add(PASS, sec, "策略本体",
            f"已部署 {st.st_size} 字节，mtime {datetime.fromtimestamp(st.st_mtime):%m-%d %H:%M}")
    else:
        add(WARN, sec, "策略本体", "未部署 ⇒ 若切 bridge 模式会无人接单")

    # ---- 心跳：QMT 侧策略是否真的在跑，以及**跑的是哪一版代码** ----
    # 为什么必须查版本：只部署文件、不在 QMT 里重新运行策略时，QMT 会一直跑
    # 内存里的旧代码 —— 部署看起来成功、boot.log 也可能有历史记录，但改动完全没生效。
    # 心跳里的 rev 是被动读取的，不需要往 signal.json 写东西（那是下单通道，探针绝不碰）。
    hb = root / "heartbeat.json"
    if hb.exists():
        age = time.time() - hb.stat().st_mtime
        try:
            data = json.loads(hb.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        rev = data.get("rev")
        pid = data.get("pid")
        add(PASS if age <= 15 else FAIL, sec, "心跳",
            f"{age:.1f}s 前，pid={pid}，rev={rev}"
            + ("" if age <= 15 else " ⇒ 桥已死/被停，切 bridge 后无人接单"))
        want = _repo_bridge_rev()
        if want:
            same = (rev == want)
            add(PASS if same else FAIL, sec, "代码版本",
                f"QMT 在跑 {rev!r}，仓库是 {want!r}"
                + ("" if same else " ⇒ QMT 里是**旧代码**，必须在 QMT 里重新运行策略"))
    elif root.exists():
        add(WARN, sec, "心跳", "桥目录存在但没有 heartbeat.json ⇒ 策略从未成功 init")


def _repo_bridge_rev() -> str | None:
    """读仓库里 qmt_bridge/qmtide_bridge.py 的 CODE_REV（正则读，不 import）。

    不 import 的原因：那个模块 import 期会跑自测并往 boot.log 写东西，
    探针不该污染 QMT 侧的诊断日志。
    """
    src = BASE_DIR / "qmt_bridge" / "qmtide_bridge.py"
    try:
        m = re.search(r'^CODE_REV\s*=\s*"([^"]+)"', src.read_text(encoding="utf-8"), re.M)
        return m.group(1) if m else None
    except Exception:
        return None


# --------------------------------------------------------------------- D 数据准确性
def probe_accuracy(snap: dict | None) -> None:
    sec = "D 数据准确性"
    cfg = zone_config("live")

    # 引擎自报
    eng_asset = (snap or {}).get("total_asset")
    eng_cash = (snap or {}).get("cash")
    eng_pos = (snap or {}).get("positions") or {}
    trust = ((snap or {}).get("risk") or {}).get("asset_trusted")
    add(PASS if trust else FAIL, sec, "asset_trusted",
        f"{trust}"
        + ("" if trust else " ⇒ 引擎总资产不是券商真值，面板数字不可用于决策"))

    # 账本末值
    db = cfg.db_path
    last_ts = last_asset = None
    if db.exists():
        try:
            conn = _ro(db)
            try:
                row = conn.execute(
                    "SELECT ts, total_asset FROM equity_snapshots "
                    "ORDER BY ts DESC LIMIT 1").fetchone()
                if row:
                    last_ts, last_asset = row[0], float(row[1] or 0)
                st = conn.execute(
                    "SELECT cash, positions, peak_asset FROM engine_state WHERE id=1"
                ).fetchone()
            finally:
                conn.close()
        except Exception as e:                                    # noqa: BLE001
            add(WARN, sec, "账本读取", f"{type(e).__name__}: {e}")
            st = None
        if last_asset is not None:
            add(PASS, sec, "账本末快照", f"{last_ts} = {last_asset:,.2f}")
            if eng_asset:
                drift = (float(eng_asset) - last_asset) / last_asset * 100
                lvl = PASS if abs(drift) < 0.5 else FAIL
                add(lvl, sec, "引擎 vs 账本",
                    f"引擎 {float(eng_asset):,.2f} − 账本 {last_asset:,.2f} = {drift:+.2f}%"
                    + ("" if lvl is PASS else " ⇒ 背离过大，引擎自报口径不可信"))
        if st:
            try:
                pos_obj = json.loads(st[1] or "[]")
                db_codes = sorted({p.get("code") for p in pos_obj
                                   if isinstance(p, dict) and p.get("code")})
            except Exception:                                     # noqa: BLE001
                db_codes = []
            add(PASS, sec, "engine_state",
                f"cash={st[0]} 持仓条目={len(db_codes)} peak_asset={st[2]}")
            # ★ 关键准确性断言：引擎内存持仓 vs 账本持仓
            #   live 不恢复本地账本（restore_ledger=False），持仓只能靠
            #   `_sync_broker_positions` 从券商回灌。券商断链 ⇒ 引擎持仓为空 ⇒
            #   现有仓位不受任何退出逻辑管理（−18% 硬止损、趋势破位全不作用）。
            mem_codes = sorted(eng_pos)
            if db_codes and not mem_codes:
                add(FAIL, sec, "引擎持仓 vs 账本",
                    f"账本有 {db_codes}，引擎内存为空 ⇒ 这些仓位不受止损/趋势退出管理"
                    f"（要等券商连上后由 _sync_broker_positions 回灌）")
            elif db_codes == mem_codes:
                add(PASS, sec, "引擎持仓 vs 账本", f"一致：{db_codes}")
            elif not db_codes and not mem_codes:
                add(PASS, sec, "引擎持仓 vs 账本", "两处皆空（空仓）")
            else:
                add(WARN, sec, "引擎持仓 vs 账本",
                    f"账本 {db_codes} vs 引擎 {mem_codes}")
    else:
        add(FAIL, sec, "账本", f"{db} 不存在")

    # 白名单持仓核对：观察篮必须在册（豁免仓不动是 OWNER 裁决）
    #   ⚠ 白名单不是模块常量，而是 STRATEGY_PARAMS 字典里的键（settings.py:613/723）。
    #     直接 getattr(settings, "exit_exempt_codes") 会静默拿到空 —— 这条坑踩过一次。
    add(PASS if eng_pos else WARN, sec, "引擎持仓",
        f"{len(eng_pos)} 只：" + (", ".join(sorted(eng_pos)[:6]) or "空"))
    try:
        import config.settings as S
        sp = getattr(S, "STRATEGY_PARAMS", {}) or {}
        exempt = list(sp.get("exit_exempt_codes") or [])
        manual = list(sp.get("manual_entry_codes") or [])
        add(PASS, sec, "白名单定义",
            f"exit_exempt={exempt} · manual_entry={manual}")
        for c in WATCH_CODES:
            in_e, in_m = c in exempt, c in manual
            add(PASS if (in_e or in_m) else WARN, sec, f"白名单 {c}",
                f"exit_exempt={in_e} manual_entry={in_m}")
        # 引擎持仓与白名单的对账：白名单里有、引擎持仓里没有 ⇒ 豁免仓失去意义
        #   根因归到上面那条「引擎持仓 vs 账本」，这里只标 WARN 避免同一根因报 3 次 FAIL
        for c in WATCH_CODES:
            if (c in exempt or c in manual) and c not in eng_pos:
                add(WARN, sec, f"豁免仓 {c} 不在引擎持仓",
                    "白名单要求豁免，但引擎内存里没有这只仓（同上根因：券商断链未回灌）")
    except Exception as e:                                        # noqa: BLE001
        add(WARN, sec, "白名单定义", f"读取失败 {type(e).__name__}: {e}")


# --------------------------------------------------------------------- E 白名单完整性
def probe_whitelist(snap: dict | None) -> None:
    sec = "E 白名单完整性"
    f = BASE_DIR / "storage" / "dynamic_universe.json"
    if not f.exists():
        add(FAIL, sec, "候选池文件", f"{f} 不存在")
        return
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:                                        # noqa: BLE001
        add(FAIL, sec, "候选池文件", f"JSON 解析失败 {type(e).__name__}: {e}")
        return

    pool = d.get("active_pool") or []
    n_total = d.get("n_total")
    by_ind = d.get("by_industry") or {}
    add(PASS if len(pool) else FAIL, sec, "磁盘池",
        f"{len(pool)} 只活跃 / 全量 {n_total} 只")
    add(PASS if sum(by_ind.values()) == n_total else WARN, sec, "行业合计",
        f"{by_ind} 合计 {sum(by_ind.values())} vs n_total {n_total}")

    # ★ 关键不变量：live 引擎内存池必须与磁盘文件一致
    #   （2026-11-08 paper 曾覆写该文件；若两者背离，说明有人在我们看着的时候改过池子）
    mem = ((snap or {}).get("dynamic_universe") or {}).get("active_pool") or []
    if not mem:
        add(SKIP, sec, "内存池对照", "引擎快照未给 active_pool")
    elif mem == pool:
        add(PASS, sec, "内存池对照", f"内存 {len(mem)} 只 == 磁盘 {len(pool)} 只（顺序也相同）")
    elif set(mem) == set(pool):
        add(WARN, sec, "内存池对照", "集合相同但顺序不同（通常无害，留意并列排序）")
    else:
        add(FAIL, sec, "内存池对照",
            f"内存 {len(mem)} vs 磁盘 {len(pool)}；仅内存 {sorted(set(mem)-set(pool))}；"
            f"仅磁盘 {sorted(set(pool)-set(mem))} ⇒ 有人改过池子")

    # 观察篮是否在池内（不在池内也可豁免持仓，但要知道）
    for c in WATCH_CODES:
        add(PASS, sec, f"{c} 在池",
            "是" if c in pool else "否（该票靠 manual_entry/exit_exempt 单独处理，不进动量池）")

    try:
        import config.settings as S
        add(PASS, sec, "静态宇宙",
            f"UNIVERSE={len(getattr(S,'UNIVERSE',[]) or [])} "
            f"STOCK_CODES={len(getattr(S,'STOCK_CODES',set()) or set())} "
            f"VALIDATION_UNIVERSE={len(getattr(S,'VALIDATION_UNIVERSE',set()) or set())}")
    except Exception as e:                                        # noqa: BLE001
        add(WARN, sec, "静态宇宙", f"读取失败 {type(e).__name__}")


def main() -> int:
    ap = argparse.ArgumentParser(description="开盘接口能力预验证（纯只读）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--quiet", action="store_true", help="只打印非 PASS")
    args = ap.parse_args()

    try:                                    # Windows 控制台中文可能被 GBK 卡住
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                       # noqa: BLE001
        pass

    snap = None
    try:
        snap = _get_json(f"http://127.0.0.1:{zone_config('live').web_port}/api/snapshot")
    except Exception:                       # noqa: BLE001
        add(FAIL, "0 引擎", "快照", "live /api/snapshot 取不到 ⇒ 引擎未运行")

    probe_market()
    probe_trader(snap)
    probe_bridge()
    probe_accuracy(snap)
    probe_whitelist(snap)

    n = {lv: sum(1 for r in _rows if r["level"] == lv) for lv in (PASS, WARN, FAIL, SKIP)}
    if args.json:
        print(json.dumps({"generated_at": datetime.now().isoformat(timespec="seconds"),
                          "summary": n, "rows": _rows}, ensure_ascii=False, indent=2))
        return 1 if n[FAIL] else 0

    print(f"开盘接口预验证  {datetime.now():%Y-%m-%d %H:%M:%S}  （纯只读）")
    print("=" * 84)
    cur = None
    for r in _rows:
        if r["sec"] != cur:
            cur = r["sec"]
            print(f"\n[{cur}]")
        if args.quiet and r["level"] == PASS:
            continue
        print(f"  {_ICON[r['level']]} {r['level']:<4} {r['item']:<16} {r['detail']}")
    print("\n" + "=" * 84)
    print(f"PASS {n[PASS]}  WARN {n[WARN]}  FAIL {n[FAIL]}  SKIP {n[SKIP]}")
    if n[FAIL]:
        print("\n不可依赖项（会直接导致零成交/错数据）：")
        for r in _rows:
            if r["level"] == FAIL:
                print(f"  · [{r['sec']}] {r['item']}：{r['detail']}")
    return 1 if n[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())

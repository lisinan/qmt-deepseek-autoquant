# -*- coding: utf-8 -*-
"""miniQMT 交互健康快照（Web「miniQMT 交互」设置面板的数据源）。

【为什么需要它】
用户对 miniQMT 的核心担忧是「它到底还在不在正常响应」。可这个答案平时散落在：
  · `core/qmt_client` 的探活结果（只在 logger 里打一行，且被节流）；
  · `engine._market_stale*`（陈旧判定在内存里）；
  · `storage` 的 orders / fills 表；
  · `signals.blocked_by`（哪一类闸门在拦）。
任何一项出事，页面上都看不出来 —— 只有等「该买没买」才事后翻日志。

本模块把这些**收敛成一次只读快照**，分三块呈现：
  ① 数据采集：模式（xtdata/mock）、探活档案、订阅数、tick 陈旧度、1m bar 末根；
  ② 交易订单：交易通道连接、今日委托/成交/未匹配、最近委托、拒单原因 Top；
  ③ 引擎运行：执行模式、熔断、连亏、仓位系数、回撤、持仓、现金。
最后给一个 `status`（ok / warn / bad）与 `issues` 列表，页面据此上色。

【铁律（务必遵守，改这个文件的人也是）】
1. **绝不抛异常**：任何一段采集失败都只降级成 ``{"error": ...}``。
   面板是给人看的，它自己崩掉比没有更糟。外层另有兜底。
2. **绝不发起阻塞 IO**：不直接调 xtdata / xttrader（它们可能卡住几十秒甚至永久）。
   只读**已经被别人写入内存的状态**。代价是「引擎没在跑时部分字段为空」——
   这个代价是有意接受的：宁可显示「未知」，也不要让一个看板把交易进程拖住。
3. **只读**：不写库、不改配置、不动引擎状态。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 引擎里的行情陈旧阈值（秒）。与 EventEngine.MARKET_STALE_SEC 保持一致，
# 但这里刻意写死常量：面板只做展示，不跟随引擎调参而改变自己的判定口径。
_DEFAULT_STALE_SEC = 120.0


def _iso(ts: Optional[float]) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _safe(fn, default):
    """采集一段失败不影响整体。"""
    try:
        return fn()
    except Exception as e:            # noqa: BLE001 - 看板绝不允许抛
        logger.debug("qmt_health 片段失败: %s", e)
        return default


# ------------------------------------------------------------------ 时段
def _session_section() -> Dict[str, Any]:
    def _go():
        from core.market_calendar import (calendar_health, is_trading_day,
                                          is_trading_time, session_label)
        now = datetime.now()
        ch = calendar_health() if callable(calendar_health) else {}
        return {
            "now": now.strftime("%Y-%m-%d %H:%M:%S"),
            "is_trading_day": bool(is_trading_day()),
            "in_trading": bool(is_trading_time()),
            "label": session_label(),
            "calendar_degraded": bool(ch.get("degraded")) if isinstance(ch, dict) else None,
            "calendar_source": ch.get("source") if isinstance(ch, dict) else None,
        }
    return _safe(_go, {"error": "交易日历不可用"})


# ------------------------------------------------------------ ① 数据采集
def _market_section(engine) -> Dict[str, Any]:
    out: Dict[str, Any] = {}

    def _client():
        # ★ 这里要的是**单例实例**，不是模块。
        #   初版写成 ``from core import qmt_client`` —— 那取到的是**模块**，
        #   模块上没有 health() / _mode ⇒ health() 不存在、mode 也读不到，
        #   于是整块恒为 {"mode": ""}：面板最爱问的「现在是不是 mock」
        #   永远显示空，而空值在页面上看起来像「没问题」，比没有更危险。
        from core.qmt_client import qmt_client as _qc
        h = _qc.health() if hasattr(_qc, "health") else {}
        return h if isinstance(h, dict) else {"error": "health() 返回非 dict"}
    out["client"] = _safe(_client, {"error": "qmt_client 不可用"})

    # 陈旧度与 bar 末根：只在引擎存在时才有意义（读内存状态，零 IO）
    if engine is None:
        out["engine_seen"] = False
        return out
    out["engine_seen"] = True

    def _stale():
        thr = float(getattr(engine, "MARKET_STALE_SEC", _DEFAULT_STALE_SEC) or
                    _DEFAULT_STALE_SEC)
        max_age = float(getattr(engine, "_market_stale_max_age", 0.0) or 0.0)
        codes = list(getattr(engine, "_market_stale_codes", []) or [])
        return {
            "threshold_sec": thr,
            "max_age_sec": round(max_age, 1),
            "stale": bool(getattr(engine, "_market_stale", False)),
            "stale_n": len(codes),
            "stale_sample": codes[:5],
            "last_round_max_age_sec": round(
                float(getattr(engine, "_last_market_max_age", -1.0)), 1),
        }
    out["staleness"] = _safe(_stale, {"error": "陈旧状态不可读"})

    def _bars():
        bars = getattr(engine, "_bars", None) or {}
        last_ts, first_ts, n_ready = None, None, 0
        for _code, dq in bars.items():
            try:
                if len(dq) >= 60:
                    n_ready += 1
                if not dq:
                    continue
                # deque 是时间升序：左端最旧、右端最新。
                # ★ 必须分别取两端：只取一端会让「最早」与「最新」相等，
                #   面板就永远看不出缓冲到底覆盖了多长时间（初版踩过）。
                lo, hi = dq[0].ts, dq[-1].ts
                first_ts = lo if first_ts is None or lo < first_ts else first_ts
                last_ts = hi if last_ts is None or hi > last_ts else last_ts
            except Exception:
                continue
        return {
            "codes": len(bars),
            "ready_ge_60": n_ready,
            "latest_bar_ts": last_ts.strftime("%Y-%m-%d %H:%M") if last_ts else "",
            "oldest_bar_ts": first_ts.strftime("%Y-%m-%d %H:%M") if first_ts else "",
        }
    out["bars_1m"] = _safe(_bars, {"error": "分钟线缓冲不可读"})

    # ★ 引擎自己认定的数据源。与上面 client.mode **不是同一件事**：
    #   client.mode 是模块单例在 import/reattach 时刻的结论；
    #   engine.data_mode 才是 `mock_trade_block_reason()` 真正用来拦单的那个值。
    #   两者理论上同步（reattach 时会赋值），但页面必须显示**生效的那个**，
    #   否则会出现「面板说 xtdata、成交却全被拦」的诡异现象。
    out["engine_data_mode"] = _safe(
        lambda: str(getattr(engine, "data_mode", "") or ""), "")
    return out


# ------------------------------------------------------------ ② 交易订单
def _trading_section(engine, storage) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    today = datetime.now().strftime("%Y-%m-%d")

    def _broker():
        from core.broker import qmt_broker
        return {"connected": bool(qmt_broker.is_connected),
                "mode": qmt_broker.mode}
    out["broker"] = _safe(_broker, {"error": "broker 不可用"})

    out["exec_mode"] = _safe(
        lambda: getattr(engine, "exec_mode", ""), "")

    def _orders():
        if storage is None:
            return {"error": "storage 不可用"}
        conn = storage._conn_get()
        like = f"{today}%"
        rows = conn.execute(
            "SELECT ts, code, side, quantity, price, order_id FROM orders "
            "WHERE ts LIKE ? ORDER BY id DESC", (like,)).fetchall()
        fills = conn.execute(
            "SELECT order_id, SUM(quantity) FROM fills WHERE ts LIKE ? "
            "GROUP BY order_id", (like,)).fetchall()
        filled = {f[0]: int(f[1] or 0) for f in fills}

        def _as_dict(r):
            if hasattr(r, "keys"):
                return dict(r)
            keys = ("ts", "code", "side", "quantity", "price", "order_id")
            return dict(zip(keys, r))

        norm = [_as_dict(r) for r in rows]
        recent = []
        for d in norm[:6]:
            q = int(d.get("quantity") or 0)
            fq = int(filled.get(d.get("order_id"), 0) or 0)
            d["filled_qty"] = fq
            d["state"] = ("全部成交" if fq and fq >= q
                          else ("部分成交" if fq else "未成交/无回报"))
            recent.append(d)
        unmatched = sum(1 for d in norm
                        if int(filled.get(d.get("order_id"), 0) or 0) == 0)
        return {"today_orders": len(norm), "today_filled_orders": len(filled),
                "today_unmatched": unmatched, "recent": recent}
    out["orders"] = _safe(_orders, {"error": "订单读取失败"})

    def _fills():
        if storage is None:
            return {}
        conn = storage._conn_get()
        n = conn.execute("SELECT COUNT(*) FROM fills WHERE ts LIKE ?",
                         (f"{today}%",)).fetchone()[0]
        return {"today_fills": int(n)}
    out["fills"] = _safe(_fills, {})

    # 拒单/未成交原因分布 —— 直接来自 signals.blocked_by（2026-10-29 新增列）。
    # 这是面板最有价值的一格：它回答「今天到底卡在哪一类闸门」。
    def _rejects():
        if storage is None:
            return {}
        conn = storage._conn_get()
        rows = conn.execute(
            "SELECT COALESCE(NULLIF(blocked_by,''),'(无阻断)') AS why, COUNT(*) n "
            "FROM signals WHERE ts LIKE ? AND side='BUY' "
            "GROUP BY why ORDER BY n DESC LIMIT 6", (f"{today}%",)).fetchall()
        return {"top": [{"why": r[0], "n": int(r[1])} for r in rows]}
    out["rejects"] = _safe(_rejects, {})

    def _risk():
        r = getattr(engine, "risk", None)
        if r is None:
            return {}
        # 【2026-10-30 修复】RiskManager 的真实成员是 `is_halted`（property）、
        # `_halt_reason`、`_consec_loss`。原实现读的是 `halted` / `halt_reason` /
        # `consec_loss` —— **三个都不存在**，getattr 全部走默认值 ⇒ 面板恒报
        # 「未熔断 / 连亏 0」。实盘铁证：当日 11:24:53 已 `daily_stop` 熔断且
        # position_scale=0，面板仍显示 halted=false，把这条最关键的告警藏掉了。
        return {"halted": bool(getattr(r, "is_halted", False)),
                "halt_reason": str(getattr(r, "_halt_reason", "") or ""),
                "consec_loss": int(getattr(r, "_consec_loss", 0) or 0),
                "position_scale": float(getattr(r, "position_scale", 1.0) or 0.0),
                "daily_pnl": round(float(getattr(r, "_daily_pnl", 0.0) or 0.0), 2),
                "daily_trades": int(getattr(engine, "_daily_trade_count", 0) or 0)}
    out["risk"] = _safe(_risk, {})

    def _pending():
        po = getattr(engine, "_pending_orders", None) or {}
        ps = getattr(engine, "_pending_sells", None) or {}
        return {"pending_buy": len(po), "pending_sell": len(ps),
                "pending_codes": list(po.keys())[:5]}
    out["pending"] = _safe(_pending, {})

    # ★ 权威答案：引擎**自己**判定「为什么现在不能成交」。
    #   直接复用交易路径上同一个函数（mock_trade_block_reason），
    #   而不是在这里复刻一份判断 —— 复刻必然漂移，而面板最怕说假话。
    def _mock_block():
        if engine is None:
            return None
        from engine.event_engine import mock_trade_block_reason
        return mock_trade_block_reason(engine) or None
    out["mock_block"] = _safe(_mock_block, None)
    return out


# ------------------------------------------------------------ ③ 引擎运行
def _engine_section(engine, storage) -> Dict[str, Any]:
    if engine is None:
        out = {"running": False}
    else:
        out = _safe(lambda: {
            "running": True,
            "exec_mode": getattr(engine, "exec_mode", ""),
            "data_mode": getattr(engine, "data_mode", ""),
            "strategy": getattr(engine, "strategy_mode", ""),
            "positions": len([p for p in
                              (getattr(engine, "_positions", {}) or {}).values()
                              if getattr(p, "quantity", 0) > 0]),
            "max_positions": getattr(engine, "max_positions", None),
            "cash": round(float(getattr(engine, "_cash", 0.0) or 0.0), 2),
        }, {"error": "引擎状态不可读"})

    def _snap():
        if storage is None:
            return {}
        conn = storage._conn_get()
        r = conn.execute(
            "SELECT ts,total_asset,cash,market_value,positions_count,"
            "drawdown_pct FROM equity_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not r:
            return {}
        return {"ts": r[0], "total_asset": r[1], "cash": r[2],
                "market_value": r[3], "positions": r[4],
                "drawdown_pct": r[5]}
    out["snapshot"] = _safe(_snap, {})
    return out


# ------------------------------------------------------------ 综合判定
def _verdict(session, market, trading) -> Dict[str, Any]:
    """把散落事实翻译成「能不能放心让它跑」的一句话 + 问题清单。

    ★ 判据顺序很重要：mock 模式是最严重的（它会**静默禁止全部成交**），
    必须排在陈旧度之前 —— 否则页面只会提示「行情旧」，而真正原因是数据源降级。
    """
    issues: List[Dict[str, str]] = []
    client = market.get("client") or {}
    # ★ 取「生效值」优先：engine.data_mode 才是拦单依据；单例 mode 是补充信息。
    mode = str(market.get("engine_data_mode") or client.get("mode") or "")
    st = market.get("staleness") or {}
    broker = trading.get("broker") or {}
    risk = trading.get("risk") or {}

    # ★ 引擎未接入时，绝大多数字段都是「未知」而不是「正常」。
    #   若不报这一条，页面会给一个绿色 ok —— 那是在用「没数据」冒充「没问题」，
    #   与本模块的存在理由（替用户盯住 miniQMT）正好相反。
    if not market.get("engine_seen"):
        issues.append({"level": "warn", "tag": "引擎",
                       "msg": "引擎未接入 ⇒ 行情陈旧度 / 1m 缓冲 / 风控 / 保证金系数"
                              "均为未知，本面板的绿色不代表正常"})

    if mode and mode != "xtdata":
        issues.append({"level": "bad", "tag": "数据源",
                       "msg": f"行情源已降级为 {mode}（合成行情）⇒ 全部买卖被禁止。"
                              f"最近探活失败：{client.get('probe_err') or '未知'}"})
    if trading.get("mock_block"):
        issues.append({"level": "bad", "tag": "成交闸门",
                       "msg": f"引擎当前会拦截全部成交：{trading.get('mock_block')}"})
    if client.get("probe_ok") is False:
        issues.append({"level": "bad", "tag": "探活",
                       "msg": f"最近一次 miniQMT 探活失败（{client.get('probe_err') or '未知'}）"})

    in_tr = bool(session.get("in_trading"))
    if st.get("stale"):
        lvl = "bad" if in_tr else "warn"
        issues.append({"level": lvl, "tag": "行情陈旧",
                       "msg": f"{st.get('stale_n')} 只标的行情年龄 "
                              f"{st.get('max_age_sec')}s > 阈值 {st.get('threshold_sec')}s"
                              f"（{'交易时段，已暂停建仓' if in_tr else '非交易时段，属正常'}）"})
    elif in_tr and st.get("last_round_max_age_sec") is not None \
            and float(st.get("last_round_max_age_sec") or -1) < 0:
        issues.append({"level": "warn", "tag": "行情",
                       "msg": "交易时段内尚无有效 tick 年龄记录（引擎可能刚启动或未接上行情）"})

    if broker and broker.get("connected") is False and \
            str(trading.get("exec_mode") or "") == "live":
        issues.append({"level": "bad", "tag": "交易通道",
                       "msg": "live 模式下交易通道未连接（disconnected）"})

    if risk.get("halted"):
        issues.append({"level": "bad", "tag": "风控",
                       "msg": f"已熔断：{risk.get('halt_reason') or '原因未记录'}"})
    ps = risk.get("position_scale")
    if ps is not None and float(ps) <= 0:
        issues.append({"level": "bad", "tag": "风控",
                       "msg": "仓位系数 = 0（连亏降仓已压到 0）⇒ 无法开新仓"})

    if any(i["level"] == "bad" for i in issues):
        status = "bad"
    elif issues:
        status = "warn"
    else:
        status = "ok"
    return {"status": status, "issues": issues}


def snapshot(engine=None, storage=None) -> Dict[str, Any]:
    """一次只读快照。任何异常都降级为空块，绝不向上抛。"""
    t0 = time.time()
    session = _session_section()
    market = _market_section(engine)
    trading = _trading_section(engine, storage)
    eng = _engine_section(engine, storage)
    verdict = _verdict(session, market, trading)
    return {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_ms": round((time.time() - t0) * 1000, 1),
        "session": session,
        "market": market,
        "trading": trading,
        "engine": eng,
        "status": verdict["status"],
        "issues": verdict["issues"],
    }

# -*- coding: utf-8 -*-
"""
只读观测层（C1 第二步·保守切片）——从 EventEngine 切出的独立模块。

职责：把「观测 / 快照」这一整块**只读**逻辑（组装前端 / SSE / 诊断用的
大字典）从 EventEngine 中切出，交由本类的实例承载。全部数据经 ``self._owner``
（引擎实例）在**调用时**动态读取，本类**不保存任何快照值**，事件线程安全性
与改造前逐位一致（原来是直接读 ``self`` 的属性，现在读 ``owner`` 的同名属性）。

设计约束（务必遵守，否则既有源码守卫 / monkeypatch 测试会红）：

1. **只读、零状态变更**：本类绝不写回引擎的任何状态（不下单 / 不成交 /
   不改风控 / 不落库）。``_entry_funnel_stats`` / ``_maybe_log_no_signal``
   因前者被源码守卫钉在 ``event_engine.py``、后者会写 ``_no_entry_reason``，
   **有意留在** EventEngine，本类只经 owner 读它们的结果。

2. **模块级名字一律调用时解析**：``qmt_broker`` / ``is_trading_time`` /
   ``session_label`` / ``latest_notices`` 会被测试 monkeypatch 为
   ``engine.event_engine.<名字>``。故本类**绝不**在模块顶层缓存这些名字的
   绑定，而是每次调用经 ``engine.event_engine`` 的模块命名空间动态取，
   使 ``patch.object(EE, "qmt_broker", ...)`` 这类 monkeypatch 照常生效。

3. **依赖经 owner 动态解析**：``_total_asset`` / ``_regime_ok`` / ``_nb_state`` /
   ``_market_data_ts`` / ``_benchmark_snapshot`` / ``_entry_funnel_stats`` 等
   都是引擎实例方法或属性，本类一律通过 ``self._owner`` 回调，保证薄委托与
   monkeypatch 语义逐位不变。
"""
from __future__ import annotations

from typing import Dict, List, Optional


class Observability:
    """只读观测层：快照 + 各类 latest_* / health / 诊断（见模块 docstring）。"""

    def __init__(self, owner):
        # 引擎引用：全部数据 / 依赖都在调用时经 owner 解析。
        self._owner = owner

    # ------------------------------------------------------------------
    # 模块级单例 / 函数的动态解析（避免缓存会被 monkeypatch 的名字）
    # ------------------------------------------------------------------
    def _ee(self):
        """返回 ``engine.event_engine`` 模块命名空间（调用时解析，绝不在顶层缓存）。"""
        import engine.event_engine as _ee
        return _ee

    # ------------------------------------------------------------------
    # 主快照
    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        o = self._owner
        ee = self._ee()
        total_asset = round(o._total_asset(), 2)
        snap = {
            "tick": o._tick_count,
            # 【2026-09-22】行情与主循环新鲜度：页面据此显示「行情滞后 N 秒」，
            # 用户一眼能区分「行情停更」与「策略本来就没信号」。
            "market_data_ts": o._market_data_ts(),
            "round_ms": round(o._last_round_ms, 1),
            "slow_rounds": o._slow_rounds,
            "strategy_mode": o.strategy_mode,
            "data_mode": o.data_mode,
            "broker_mode": o.broker_mode,
            "broker_connected": ee.qmt_broker.is_connected,
            "broker_reconnects": (o._broker_reconnector.reconnect_count
                                  if o._broker_reconnector else 0),
            "exec_mode": o.exec_mode,
            "session": {
                "guard": o.session_guard,
                "state": ee.session_label(),
                "trading_now": ee.is_trading_time(),
            },
            "regime": {
                "mode": o.regime_mode,
                "index": o.regime_index,
                "ma": o.regime_ma,
                "ok": o._regime_ok(),
                "force_exit": o.regime_force_exit,
            },
            "northbound": o._nb_state(),
            "cash": round(o._cash, 2),
            "positions": {
                code: {
                    "name": p.name,
                    "quantity": p.quantity,
                    "avg_cost": round(p.avg_cost, 3),
                    "last_price": round(p.last_price, 3),
                    "pnl": round(p.pnl, 2),
                    "pnl_pct": round(p.pnl_pct * 100, 3),
                    "open_date": p.open_date.isoformat() if p.open_date else None,
                }
                for code, p in o._positions.items() if p.quantity > 0
            },
            "total_asset": total_asset,
            "risk": o._build_risk_snapshot(total_asset),
            "analyst_enabled": o.analyst.enabled,
            "analyst_client": type(o.analyst.client).__name__,
            "sector_scorer": (o.sector_scorer.snapshot()
                              if o.sector_scorer else None),
            "dynamic_universe": (o.dynamic_universe.snapshot()
                                  if o.dynamic_universe else None),
            "llm_rerank": (o.latest_llm_rerank()),
            "llm_reranker": (o.llm_reranker.snapshot()
                              if o.llm_reranker else None),
            "data_health": o.data_health(),
            "notices": o.latest_notices(20),
            # 【2026-10-21 C8】决策可观测字段（无数据时均可安全 JSON 序列化）。
            "gate_funnel": o._gate_funnel_snapshot(),
            "no_entry_reason": o._no_entry_reason,
            "ai_veto_n": o._ai_veto_n,
            "degraded": o._degraded_list(),
            "benchmark": o._benchmark_snapshot(),
        }
        if o._portfolio:
            snap["portfolio"] = {
                "max_positions": o._portfolio.max_positions,
                "score_threshold": o._portfolio.score_threshold,
                "max_single_pct": o._portfolio.max_single_pct,
            }
        return snap

    # ------------------------------------------------------------------
    # 风控 / 漏斗 / 降级（读 owner 的既有薄委托）
    # ------------------------------------------------------------------
    def _build_risk_snapshot(self, total_asset: float) -> dict:
        """在 RiskManager.snapshot() 基础上补充「日内盈亏」（真实日内总盈亏）。

        daily_pnl 仅统计已成交流水（realized），无 round-trip 时恒为 0，仪表板
        看起来像「没数据」。intraday_pnl = 当前总资产 − 今日开盘资产，融合了当日
        浮动盈亏，交易时段始终有真实数值。基线 _day_open_asset 优先取今日首个
        权益快照（跨重启仍有效），否则取本会话首 tick 总资产。
        """
        o = self._owner
        snap = o.risk.snapshot()
        open_asset = o._day_open_asset
        if open_asset and open_asset > 0:
            intraday = total_asset - open_asset
            snap["intraday_pnl"] = round(intraday, 2)
            snap["intraday_pnl_pct"] = round(intraday / open_asset * 100, 3)
            snap["day_open_asset"] = round(open_asset, 2)
        else:
            snap["intraday_pnl"] = 0.0
            snap["intraday_pnl_pct"] = 0.0
            snap["day_open_asset"] = None
        return snap

    def _gate_funnel_snapshot(self) -> dict:
        """最近一次入场漏斗（C8）：复用 _entry_funnel_stats，缺失填 0。"""
        o = self._owner
        keys = ("universe_n", "momentum_pass_n", "trend_up_n",
                "bias_ok_n", "score_ok_n")
        out = {k: 0 for k in keys}
        try:
            st = o._entry_funnel_stats(0)
            for k in keys:
                out[k] = int(st.get(k) or 0)
        except Exception:
            pass
        return out

    def gate_detail(self) -> dict:
        """入场闸门**逐只明细**（学习/排障用，只读）。

        为什么需要它：``gate_funnel`` 只给计数（38 只里 trend_up 通过 0 只），
        但「为什么这只没通过」必须能看到**四个子条件各自的真假**，否则
        「今天为什么没买」只能靠临时脚本重算。

        返回 ``{"summary": {...}, "rows": [...]}``：
        - 每行含 close / ma20 / ma60 / ma20_slope / macd_hist / 动量60日 /
          评分 / 四个子条件布尔 / 是否 trend_up / 第一处卡点中文说明；
        - rows 按「通过条件数降序、动量降序」排序 ⇒ 最接近通过的排在最上面，
          一眼看出「差哪一条」。
        数据全部来自 ``owner.daily``（DailyContext），无网络、无副作用。
        """
        o = self._owner
        daily = getattr(o, "daily", None)
        if daily is None:
            return {"summary": {"ready": False}, "rows": []}
        try:
            codes = sorted(daily.covered_codes())
        except Exception:
            return {"summary": {"ready": False}, "rows": []}

        # 参数一律经 engine 命名空间动态取（与模块内其它依赖同一纪律）。
        _p = getattr(self._ee(), "STRATEGY_PARAMS", {}) or {}
        min_mom = float(_p.get("momentum_min_mom", 0.0) or 0.0)
        lookback = int(_p.get("momentum_lookback", 60) or 60)
        score_th = float(_p.get("buy_score_threshold", 4.0) or 4.0)
        min_sig = int(_p.get("min_signals", 3) or 3)
        rows = []
        n = {"universe": len(codes), "above_ma20": 0, "ma20_over_ma60": 0,
             "slope_up": 0, "macd_ok": 0, "trend_up": 0, "mom_pass": 0,
             "score_ok": 0}
        for code in codes:
            try:
                f = daily.features(code)
            except Exception:
                f = None
            if f is None:
                continue
            try:
                mom = daily.momentum_60d(code, lookback)
            except Exception:
                mom = None
            above20 = bool(getattr(f, "above_ma20", False))
            above60 = bool(getattr(f, "above_ma60", False))
            ma20 = float(getattr(f, "ma20", 0.0) or 0.0)
            ma60 = float(getattr(f, "ma60", 0.0) or 0.0)
            slope = float(getattr(f, "ma20_slope", 0.0) or 0.0)
            macd_hist = float(getattr(f, "macd_hist", 0.0) or 0.0)
            ma20_over_ma60 = bool(ma20 > 0 and ma60 > 0 and ma20 > ma60)
            slope_up = bool(slope > 0)
            macd_ok = bool(macd_hist >= 0)
            trend_up = bool(getattr(f, "trend_up", False))
            score = float(getattr(f, "score", 0.0) or 0.0)
            factors = getattr(f, "factors", {}) or {}
            pos_factors = sum(1 for v in factors.values()
                              if isinstance(v, (int, float)) and v > 0)
            mom_ok = bool(mom is not None and mom > min_mom)
            score_ok = bool(score >= score_th and pos_factors >= min_sig)

            # 第一处卡点（按闸门的实际执行顺序）：先日内动量池，再日线趋势，最后评分
            blocked = ""
            if not mom_ok:
                blocked = (f"动量不足（{lookback}日 "
                           f"{(mom * 100 if mom is not None else float('nan')):.1f}%"
                           f" ≤ {min_mom * 100:.0f}%）")
            elif not above60:
                blocked = "收盘 < MA60（中期趋势未修复）"
            elif not ma20_over_ma60:
                blocked = "MA20 ≤ MA60（均线未多头排列）"
            elif not slope_up:
                blocked = "MA20 斜率 ≤ 0（中期均线未上翘）"
            elif not macd_ok:
                blocked = "MACD 柱 < 0（动能为负）"
            elif not score_ok:
                blocked = (f"评分不足（{score:.2f} < {score_th:.1f}"
                           f" 或正因子 {pos_factors} < {min_sig}）")
            elif not trend_up:
                blocked = "trend_up 未成立（四条件未同时满足）"

            # blocked_key：按实盘 gate_detail 的同一顺序找第一个失败（不含 trend_up）。
            # 顺序与实盘「动量 → MA60 → 均线排列 → 斜率 → MACD → 评分」一致；
            # 七项全过 → ""（当日该票可买入）。
            blocked_key = ""
            _key_checks = (("mom_ok", mom_ok), ("above_ma60", above60),
                           ("ma20_over_ma60", ma20_over_ma60),
                           ("slope_up", slope_up), ("macd_ok", macd_ok),
                           ("score_ok", score_ok))
            for _k, _ok in _key_checks:
                if not _ok:
                    blocked_key = _k
                    break

            passed = sum([above20, above60, ma20_over_ma60, slope_up,
                          macd_ok, mom_ok, score_ok])
            n["above_ma20"] += int(above20)
            n["ma20_over_ma60"] += int(ma20_over_ma60)
            n["slope_up"] += int(slope_up)
            n["macd_ok"] += int(macd_ok)
            n["trend_up"] += int(trend_up)
            n["mom_pass"] += int(mom_ok)
            n["score_ok"] += int(score_ok)
            rows.append({
                "code": code,
                "name": getattr(f, "name", "") or "",
                "close": round(float(getattr(f, "close", 0.0) or 0.0), 3),
                "ma20": round(ma20, 3), "ma60": round(ma60, 3),
                "ma20_slope": round(slope, 4),
                "macd_hist": round(macd_hist, 4),
                "mom60": (round(float(mom), 4) if mom is not None else None),
                "score": round(score, 2),
                "pos_factors": pos_factors,
                "day_change_pct": round(float(getattr(f, "day_change_pct", 0.0) or 0.0), 4),
                "above_ma20": above20, "above_ma60": above60,
                "ma20_over_ma60": ma20_over_ma60, "slope_up": slope_up,
                "macd_ok": macd_ok, "mom_ok": mom_ok, "score_ok": score_ok,
                "trend_up": trend_up, "passed_n": passed,
                "blocked_by": blocked, "blocked_key": blocked_key,
            })

        rows.sort(key=lambda r: (-r["passed_n"], -(r["mom60"] or -9.9)))

        breadth60 = None
        try:
            breadth60 = daily.breadth_ratio(60)
        except Exception:
            pass
        summary = {
            "ready": True,
            "universe_n": n["universe"],
            "above_ma20_n": n["above_ma20"],
            "ma20_over_ma60_n": n["ma20_over_ma60"],
            "slope_up_n": n["slope_up"],
            "macd_ok_n": n["macd_ok"],
            "trend_up_n": n["trend_up"],
            "mom_pass_n": n["mom_pass"],
            "score_ok_n": n["score_ok"],
            "breadth60": (round(float(breadth60), 4)
                          if breadth60 is not None else None),
            "params": {"min_mom": min_mom, "lookback": lookback,
                       "score_threshold": score_th, "min_signals": min_sig},
        }
        return {"summary": summary, "rows": rows}

    def _pick_best_candidate(self, rows: List[dict]) -> Optional[dict]:
        """从 gate_detail() 的逐只明细里挑「当日最强候选」。

        排序规则（与实盘同源）：
          passed_n 降序 → score 降序 → mom60 降序，取第一名。
        返回 {"code","passed_n","blocked_key","score","mom60"}；rows 为空返回 None。
        """
        if not rows:
            return None
        best = max(
            rows,
            key=lambda r: (
                r.get("passed_n") or 0,
                (r.get("score") if isinstance(r.get("score"), (int, float)) else -9.9),
                (r.get("mom60") if isinstance(r.get("mom60"), (int, float)) else -9.9),
            ),
        )
        return {
            "code": best.get("code") or "",
            "passed_n": best.get("passed_n") or 0,
            "blocked_key": best.get("blocked_key") or "",
            "score": (best.get("score") if isinstance(best.get("score"), (int, float)) else 0.0),
            "mom60": (best.get("mom60") if isinstance(best.get("mom60"), (int, float)) else None),
        }

    def gate_timeline_row(self, universe=None) -> dict:
        """当日闸门时间线一行（与 logs/gate_timeline.jsonl 历史行同口径）。

        【2026-10-24 口径统一 A 方案】时间线序列只统计**固定宇宙**
        （``STOCK_CODES ∪ SECTOR_CONFIG`` = 与历史回填/xtdata 增量同源的那 23 只），
        否则 live 行（动态宇宙 38 只）会与历史（23 只）在 ``universe_n`` 上跳变，
        通过率虽可比、绝对数不可比，长期看图会失真。

        ⚠ 逐只明细面板（``gate_detail``）**仍然**用实盘的动态宇宙（38 只）——
        那是"今天有哪些票"，两者职责不同。

        ``universe``：仅测试/排障用，显式指定统计范围；默认取固定宇宙。
        """
        from datetime import date as _date
        try:
            rows = self.gate_detail().get("rows") or []
        except Exception:
            rows = []
        if universe is None:
            universe = self._fixed_universe()
        if universe is not None:
            uni_set = set(universe)
            rows = [r for r in rows if r.get("code") in uni_set]
        keys = ("above_ma20", "above_ma60", "ma20_over_ma60", "slope_up",
                "macd_ok", "mom_ok", "score_ok")
        pass_n = {k: 0 for k in keys}
        trend_up = 0
        all_pass_n = 0
        for r in rows:
            for k in keys:
                if r.get(k):
                    pass_n[k] += 1
            if r.get("trend_up"):
                trend_up += 1
            # 七项全通过 = 这只票单独"够格"；但组合闸门还要求
            # min_passing_candidates（默认 2）只同时够格才放行。
            if all(r.get(k) for k in keys):
                all_pass_n += 1
        return {
            "date": _date.today().isoformat(),
            "universe_n": len(rows),
            "pass": pass_n,
            "trend_up": trend_up,
            "all_pass_n": all_pass_n,
            "best": self._pick_best_candidate(rows),
            "source": "live",
        }

    @staticmethod
    def _fixed_universe():
        """时间线用的固定宇宙（与 ``_evolve_wf.wide_universe()`` 同源）。

        取 ``STOCK_CODES ∪ SECTOR_CONFIG.sectors[*].stocks`` 的代码集合；
        任何异常返回 None（=不过滤，退化为当前 covered_codes，宁可少过滤也不静默清零）。
        """
        try:
            from config.settings import SECTOR_CONFIG, STOCK_CODES
            codes = set(STOCK_CODES)
            for v in (SECTOR_CONFIG.get("sectors") or {}).values():
                for item in (v.get("stocks") or []):
                    codes.add(item[0])
            return codes or None
        except Exception:
            return None

    def _degraded_list(self) -> list:
        """决策可观测的降级状态（C8）：元素为简短中文串，可安全 JSON 序列化。"""
        o = self._owner
        degraded: list = []
        if o.data_mode != "xtdata":
            degraded.append(f"行情源非 xtdata（{o.data_mode}）")
        try:
            if o.risk.is_halted:
                degraded.append("风控熔断")
        except Exception:
            pass
        if o._market_stale:
            degraded.append("行情陈旧")
        return degraded

    # ------------------------------------------------------------------
    # latest_* / health / staleness（只读，读 owner 属性）
    # ------------------------------------------------------------------
    def latest_bars(self) -> Dict[str, List[dict]]:
        """返回所有股票的最近 bar（用于 SSE 推送）"""
        o = self._owner
        out = {}
        for code, dq in o._bars.items():
            if not dq:
                continue
            last = dq[-1]
            out[code] = {
                "ts": last.ts.isoformat(),
                "open": last.open, "high": last.high,
                "low": last.low, "close": last.close,
                "volume": last.volume,
            }
        return out

    def latest_notices(self, limit: int = 50) -> list:
        """最近的系统提示（用于 Web 仪表板展示 / SSE 推送）。"""
        return self._ee().latest_notices(limit)

    def latest_ticks(self) -> Dict[str, dict]:
        """最近一次拉到的 tick（用于 SSE 推送）"""
        return self._owner._last_ticks

    def _market_data_ts(self) -> Optional[str]:
        """最新行情时间（ISO）。取所有标的中最新的一只，代表本轮数据新鲜度。"""
        o = self._owner
        latest = None
        for t in (o._last_ticks or {}).values():
            v = t.get("ts") if isinstance(t, dict) else None
            if v and (latest is None or v > latest):
                latest = v
        return latest

    def latest_sector_heat(self) -> dict:
        """产业链热度（每个环节的 heat_score）。"""
        o = self._owner
        if o.sector_scorer is None:
            return {}
        # 【2026-09-22】补齐 avg_volume_ratio 与领跌字段：前者本来就在 SectorScore
        # 里却从未下发（量能是热度三大构成之一，页面看不到）；后者用于算分化度，
        # 避免「单只暴涨 + 平均涨幅高」被误读成板块普涨。
        return {k: {"heat_score": v.heat_score,
                    "avg_change_pct": v.avg_change_pct,
                    "avg_volume_ratio": getattr(v, "avg_volume_ratio", 1.0),
                    "strength": v.strength,
                    "n_up": v.n_up, "n_stocks": v.n_stocks,
                    "best_code": v.best_code,
                    "best_name": v.best_name,
                    "best_change_pct": getattr(v, "best_change_pct", 0.0),
                    "worst_code": getattr(v, "worst_code", ""),
                    "worst_name": getattr(v, "worst_name", ""),
                    "worst_change_pct": getattr(v, "worst_change_pct", 0.0),
                    "label": v.label}
                for k, v in o.sector_scorer.sector_scores.items()}

    def latest_recommendations(self) -> list:
        """当前推荐池（asdict 列表）。"""
        o = self._owner
        if o.sector_scorer is None:
            return []
        from dataclasses import asdict
        return [asdict(r) for r in o.sector_scorer.recommendations]

    def latest_llm_rerank(self) -> Optional[dict]:
        """最新 LLM 重排序结果。

        【2026-09-22】附带 health：之前 LLM 调用失败（典型是代理没开导致
        ProxyError）时前端**完全没有反馈**——按钮点下去返回 200，页面继续显示
        上一次结果，用户只能得出「LLM 重排没工作」的结论。现在把 enabled /
        last_error / 上次成功时间一起回传，页面可显示明确失败原因。
        """
        o = self._owner
        if o._llm_last_result is None:
            return None
        from dataclasses import asdict
        out = asdict(o._llm_last_result)
        out["health"] = o.llm_health()
        return out

    def llm_health(self) -> dict:
        """LLM 重排健康状态（供前端展示失败原因，见 latest_llm_rerank 注释）。

        【2026-10-21 C-B5-4】合并 reranker.snapshot() 与 deepseek.health()：
        旧实现 getattr(self.llm_reranker, "client", None) 永远是 None（空转）。
        """
        o = self._owner
        health: dict = {
            "enabled": bool(o.llm_reranker and o.llm_reranker.enabled),
            "has_result": o._llm_last_result is not None,
        }
        if o.llm_reranker is not None:
            try:
                health.update(o.llm_reranker.snapshot())
            except Exception:
                pass
        try:
            from ai.deepseek_client import deepseek
            health["deepseek"] = deepseek.health()
        except Exception:
            pass
        return health

    def data_health(self) -> dict:
        """数据源健康（tushare 基本面）。熔断时页面应能看出「基本面降级」。"""
        try:
            from data.tushare_client import tushare_client
            return tushare_client.health()
        except Exception as e:
            return {"enabled": False, "error": str(e)}

    def latest_dynamic_universe_summary(self) -> dict:
        """动态候选池摘要（不传全 373 只代码，只传统计）。"""
        o = self._owner
        if o.dynamic_universe is None:
            return {}
        return {
            "enabled": o.dynamic_universe.enabled,
            "n_total": len(o.dynamic_universe.codes),
            "active_pool_size": len(o.dynamic_universe.active_codes),
            "by_industry": o.dynamic_universe.industries_breakdown(),
            "last_refresh": o.dynamic_universe.last_refresh_str,
        }

    def market_staleness(self) -> dict:
        """行情新鲜度（供前端/复盘诊断，不影响任何交易决策）。"""
        o = self._owner
        return {
            "stale": o._market_stale,
            "stale_codes_n": len(o._market_stale_codes),
            "stale_codes": o._market_stale_codes[:10],
            "max_age_sec": round(o._market_stale_max_age, 1),
            "threshold_sec": o.MARKET_STALE_SEC,
        }

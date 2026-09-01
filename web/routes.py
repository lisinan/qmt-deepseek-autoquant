# -*- coding: utf-8 -*-
"""
Web 路由

页面：
  GET /                      仪表板主页

REST：
  GET /api/snapshot          当前快照（现金/持仓/风控）
  GET /api/ticks             最近一次拉到的 ticks
  GET /api/bars              最近 bars
  GET /api/signals?limit=50  信号历史
  GET /api/fills?limit=50    成交历史
  GET /api/ai?limit=20       AI 分析历史
  GET /api/risk              风控状态
  GET /api/portfolio         组合配置
  POST /api/engine/start     异步启动主循环（已运行的会忽略）
  POST /api/engine/stop      停止
  POST /api/engine/snapshot  强制刷新一次

SSE：
  GET /api/stream            行情 + 信号 + 成交 + 风控 实时推送
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Generator

from flask import Blueprint, Response, current_app, jsonify, render_template

from config.settings import UNIVERSE
from storage.db import Storage
from data.stock_names import build_name_map, background_refresh

logger = logging.getLogger(__name__)

bp = Blueprint("main", __name__)

# 模块级共享 Storage【P1 修正 2026-09-02】
# 原实现每个请求 ``return Storage()``：
#   ① Storage.__init__ 会跑 19 条 CREATE TABLE/INDEX + 3 条 ALTER TABLE，
#      /api/signals 这类高频端点每次都重跑一遗建表迁移；
#   ② 返回后**从不调 close()**，靠 GC 回收 sqlite 连接 → 句柄泄漏；
#   ③ SSE 每个客户端再额外开一条常驻连接。
# Storage 内部已有 RLock + check_same_thread=False，本身就是为多线程共享设计，
# 模块级单例既安全，又把建表迁移成本降为一次。
_STORAGE = None
_STORAGE_LOCK = threading.Lock()


def _engine():
    return current_app.config["engine"]


def _storage() -> Storage:
    """进程内共享的 Storage 单例（线程安全，建表迁移只跑一次）。"""
    global _STORAGE
    if _STORAGE is None:
        with _STORAGE_LOCK:
            if _STORAGE is None:
                _STORAGE = Storage()
    return _STORAGE


# ============================================================ 页面

@bp.route("/")
def index():
    e = _engine()
    # 完整 code->name 映射（静态 + 动态候选池 + 已缓存全市场），供前端 NAME() 使用
    stock_names = build_name_map(
        dynamic_universe=e.dynamic_universe if e else None)
    # 后台富化全市场名称（不阻塞本次响应；下次页面加载即含完整名称）
    background_refresh()
    return render_template("index.html",
                           universe=UNIVERSE,
                           stock_names=stock_names)


# ============================================================ REST

@bp.route("/api/snapshot")
def api_snapshot():
    return jsonify(_engine().snapshot())


@bp.route("/api/ticks")
def api_ticks():
    return jsonify(_engine().latest_ticks())


@bp.route("/api/bars")
def api_bars():
    return jsonify(_engine().latest_bars())


@bp.route("/api/signals")
def api_signals():
    from flask import request
    limit = int(request.args.get("limit", 50))
    code = request.args.get("code") or None
    return jsonify(_storage().get_signals(limit=limit, code=code))


@bp.route("/api/fills")
def api_fills():
    from flask import request
    limit = int(request.args.get("limit", 50))
    code = request.args.get("code") or None
    return jsonify(_storage().get_fills(limit=limit, code=code))


@bp.route("/api/ai")
def api_ai():
    from flask import request
    limit = int(request.args.get("limit", 20))
    code = request.args.get("code") or None
    return jsonify(_storage().get_ai_analyses(limit=limit, code=code))


@bp.route("/api/risk")
def api_risk():
    return jsonify(_engine().risk.snapshot())


@bp.route("/api/portfolio")
def api_portfolio():
    e = _engine()
    info = {"strategy_mode": e.strategy_mode}
    if e._portfolio:
        info.update({
            "max_positions": e._portfolio.max_positions,
            "score_threshold": e._portfolio.score_threshold,
            "max_single_pct": e._portfolio.max_single_pct,
        })
    return jsonify(info)


@bp.route("/api/notices")
def api_notices():
    """系统提示通道：最新启动结论 / 分析结论 / 交易 / 风控 / 运行异常。"""
    from flask import request
    from core.notices import latest_notices
    limit = int(request.args.get("limit", 50))
    return jsonify(latest_notices(limit))


@bp.route("/api/sector/recommendations")
def api_sector_recommendations():
    """产业链推荐池（当前 + 历史）。"""
    from flask import request
    limit = int(request.args.get("limit", 50))
    sector = request.args.get("sector") or None
    code = request.args.get("code") or None
    storage = _storage()
    rows = storage.get_sector_recommendations(limit=limit, sector=sector, code=code)
    # 当前实时推荐（来自 in-memory scorer）
    e = _engine()
    current = None
    if e.sector_scorer is not None:
        current = e.sector_scorer.snapshot()
    return jsonify({"current": current, "history": rows})


@bp.route("/api/sector/score")
def api_sector_score():
    """当前产业链热度快照（每个环节的 heat/strength/best）。"""
    e = _engine()
    if e.sector_scorer is None:
        return jsonify({"enabled": False})
    return jsonify(e.sector_scorer.snapshot())


@bp.route("/api/universe/dynamic")
def api_universe_dynamic():
    """动态候选池（从 Tushare 按行业拉的扩展池）。"""
    e = _engine()
    if e.dynamic_universe is None:
        return jsonify({"enabled": False})
    snap = e.dynamic_universe.snapshot()
    snap["sample"] = list(e.dynamic_universe.universe_map().items())[:10]
    return jsonify(snap)


@bp.route("/api/universe/refresh", methods=["POST"])
def api_universe_refresh():
    """强制刷新动态候选池（异步执行，避免超时）。"""
    import threading
    e = _engine()
    if e.dynamic_universe is None:
        return jsonify({"ok": False, "reason": "dynamic_universe not enabled"})
    def _do():
        try:
            e.dynamic_universe.refresh(force=True)
            logger.info("DynamicUniverse 异步刷新完成")
        except Exception as ex:
            logger.warning("DynamicUniverse 异步刷新失败: %s", ex)
    t = threading.Thread(target=_do, name="universe-refresh", daemon=True)
    t.start()
    return jsonify({"ok": True, "async": True,
                     "msg": "refresh started in background, check /api/universe/dynamic"})


@bp.route("/api/llm/rerank", methods=["POST"])
def api_llm_rerank():
    """手动触发 LLM 重排序（异步）。"""
    import threading
    e = _engine()
    if e.llm_reranker is None or not e.llm_reranker.enabled:
        return jsonify({
            "ok": False,
            "reason": "llm not enabled / no key",
            "hint": ("未配置 LLM 密钥。在项目根目录创建 .env 文件写入 "
                     "OPENROUTER_API_KEY=sk-or-... （免费模型 "
                     "deepseek/deepseek-chat-v3-0324:free 可用），或设置环境变量 "
                     "DEEPSEEK_API_KEY / OPENROUTER_API_KEY 后重启 Web 服务即可启用 "
                     "LLM 重排序。.env 已加入 .gitignore，密钥不会进版本库。"),
        })
    recs = e.latest_recommendations()
    # 兜底：推荐池为空时，用最近一次行情即时重建（引擎每 tick 自动生成，
    # 手动点击可能在自动生成前到达；重建失败说明确实无行情则给出明确提示）。
    if not recs:
        try:
            e.ensure_recommendations()
            recs = e.latest_recommendations()
        except Exception as ex:
            logger.debug("ensure_recommendations 失败: %s", ex)
    if not recs:
        return jsonify({
            "ok": False,
            "reason": "no recommendations yet",
            "hint": ("推荐池暂为空。引擎已在单/组合两种模式下每 5 tick 自动生成产业链推荐池"
                     "（纯观察，与策略无关）。若刚重启且此前无持久化记录，需等交易时段行情"
                     "就绪（或首条 tick 到达）后再点；也可先确认引擎正在运行、已连接行情"
                     "（非交易时段 get_full_tick 返回空会回落 mock，不生成实时推荐）。"
                     "注意：修改后须重启 Web 服务（python main.py --stop 再 --web）方可加载新代码。"),
        })
    heat = e.sector_scorer.sector_scores if e.sector_scorer else {}

    def _do():
        try:
            from dataclasses import asdict
            # 把 recs dict 反向还原成 dataclass-like 给 rerank 用
            from strategy.sector_scorer import StockRecommendation
            rec_objs = [
                StockRecommendation(
                    ts=r["ts"] if hasattr(r["ts"], "timestamp") else __import__("datetime").datetime.fromisoformat(r["ts"]),
                    code=r["code"], name=r["name"],
                    sector=r["sector"], sector_label=r["sector_label"],
                    composite=r["composite"],
                    heat_contribution=r["heat_contribution"],
                    tech_score=r["tech_score"],
                    fundamental_score=r["fundamental_score"],
                    pe=r["pe"], roe=r["roe"],
                    change_pct=r["change_pct"],
                ) for r in recs
            ]
            result = e.llm_reranker.rerank(rec_objs, heat, {})
            if result:
                e._llm_last_result = result
                logger.info("LLM rerank 手动触发完成")
        except Exception as ex:
            logger.warning("LLM rerank 失败: %s", ex)

    t = threading.Thread(target=_do, name="llm-rerank-manual", daemon=True)
    t.start()
    return jsonify({"ok": True, "async": True,
                     "msg": "rerank started, see /api/llm/rerank/latest"})


@bp.route("/api/llm/rerank/latest")
def api_llm_rerank_latest():
    """最新一次 LLM 重排序结果。"""
    e = _engine()
    return jsonify({
        "result": e.latest_llm_rerank(),
        "reranker": (e.llm_reranker.snapshot() if e.llm_reranker else None),
    })


@bp.route("/api/engine/start", methods=["POST"])
def api_engine_start():
    e = _engine()
    if e._tick_count > 0 and not e._stop_flag.is_set():
        return jsonify({"ok": False, "reason": "engine already running"})
    e._stop_flag.clear()

    def _run():
        try:
            e.run()
        except Exception as ex:
            logger.exception("engine thread: %s", ex)

    t = threading.Thread(target=_run, name="engine-web", daemon=True)
    t.start()
    return jsonify({"ok": True})


@bp.route("/api/engine/stop", methods=["POST"])
def api_engine_stop():
    _engine().stop()
    return jsonify({"ok": True})


# ============================================================ SSE

@bp.route("/api/stream")
def api_stream():
    """SSE 实时推送：每 1s 一次，包含 snapshot + ticks + recent signals/fills。"""
    # 在进入 generator 前把 engine 引用提出来，避免跨线程的 application context 问题
    e = _engine()
    storage = _storage()          # 共享单例，不再每个 SSE 客户端开一条连接

    def gen() -> Generator[str, None, None]:
        last_signal_id = 0
        last_fill_id = 0
        while True:
            try:
                sigs = storage.get_signals(limit=10)
                sigs = [s for s in sigs if int(s.get("id") or 0) > last_signal_id][-5:]
                if sigs:
                    last_signal_id = max(last_signal_id,
                                         max(int(s.get("id") or 0) for s in sigs))
                fills = storage.get_fills(limit=10)
                fills = [f for f in fills if int(f.get("id") or 0) > last_fill_id][-5:]
                if fills:
                    last_fill_id = max(last_fill_id,
                                       max(int(f.get("id") or 0) for f in fills))
                # 注：原实现在这里把 payload 字典**构建了两遁**（第一个立即被第二个
                # 覆盖），纯属浪费。已合并为一次。
                payload = {
                    "ts": time.time(),
                    "snapshot": e.snapshot(),
                    "ticks": e.latest_ticks(),
                    "bars": e.latest_bars(),
                    "new_signals": sigs,
                    "new_fills": fills,
                    "sector_heat": e.latest_sector_heat(),
                    "recommendations": e.latest_recommendations(),
                    "llm_rerank": e.latest_llm_rerank(),
                    "dynamic_universe_summary": e.latest_dynamic_universe_summary(),
                    "notices": e.latest_notices(20),
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
            except GeneratorExit:
                raise
            except Exception as ex:
                logger.debug("sse tick err: %s", ex)
                yield f"data: {json.dumps({'error': str(ex)})}\n\n"
            time.sleep(1.0)

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})
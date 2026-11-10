# -*- coding: utf-8 -*-
"""实盘区买卖交易模块（OWNER 手工下单面板）。

## 它是什么 / 不是什么

**是**：live 面板（:5000）主页上的「实盘买卖」区 + 交易 API，让 OWNER 用表单
而不是手写 JSONL 来下「买/卖 N 股某代码」。
【2026-11-10 合并】原独立页 `/trade` 已并入主页 `/`（锚点 `#trade`），
`/trade` 仅作兼容入口渲染同一份主页，返回码与开关判定不变；
三个页面级差异（模板 / CSS / JS）因此收敛为一套，避免样式与交互分叉。

**不是**：一条新的下单通道。它**只做一件事**——往既有的
``storage/owner_orders.jsonl`` 追加一行，然后由引擎主循环里的
``core.owner_order.run_step`` 交给 `_handle_buy` / `_handle_sell`（全系统唯一落单口）。

## 五条设计红线

1. **绝不碰 broker**。本模块不 import `qmt_broker` / `core.broker*` / `core.qmt_client`。
   `tests/test_trade_panel.py::test_module_never_touches_broker` 用静态断言钉住。
2. **校验只有一份**。提交前的预检直接调 `core.owner_order.decide()`，与引擎执行时
   用的是同一个纯函数 ⇒ 面板说「会被拒」和引擎真拒绝**必然一致**，不会出现
   「面板放行、引擎拒绝」的双口径。面板的拒绝不是新增闸门，只是把引擎的拒绝提前。
3. **append-only**。只追加队列，从不改写历史行（历史行是审计证据）。
4. **取消靠 done 账本**。把 id 记进 `owner_orders_done.json`（status=cancelled）
   即等价作废——`run_step` 跳过一切已在 done 的 id，因此不需要重写队列文件。
5. **默认只在 live 区生效**。paper 区需 `QMT_TRADE_UI_ALLOW_PAPER=1`；
   整模块开关 `QMT_TRADE_UI=0` 可一键关闭（关闭后 `/trade` 返回 403）。

## 为什么必须有一次性令牌

`GET /api/trade/token` 发一个 120 秒、单次有效的令牌，`POST /api/trade/submit`
必须带 `X-Trade-Token`。作用不是加密，是**防误触与防跨站**：
面板上「点错方向」和「浏览器里的其它页面顺手 POST 到 127.0.0.1:5000」都是真实风险，
而这个接口动的是真实资金。

## 审计

每次提交/取消都追加一行到 `logs/owner_trade_ui.log`（JSONL，含 remote_addr 与
预检结论）。出事后能回答「这单是谁在什么时候从哪台机器点出来的」。

## 生效条件

Flask app 在引擎进程启动时创建 ⇒ **改完必须重启 live 引擎**页面才存在
（全仓无 `importlib.reload`）。
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import secrets
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from flask import (Blueprint, current_app, jsonify, render_template, request)

from config.settings import STRATEGY_PARAMS, UNIVERSE
from data.stock_names import build_name_map
from core import owner_order as OO

logger = logging.getLogger(__name__)

bp = Blueprint("trade", __name__)

TOKEN_TTL_SEC = 120
AUDIT_NAME = "owner_trade_ui.log"

# A 股代码：6 位数字 + 交易所后缀。允许池外代码（OWNER 可能买白名单外的票），
# 但**必须**带上合法后缀——没有后缀的代码在桥侧一定下不出去。
_CODE_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")

# 允许的买卖方向（前端是单选，但服务端必须自己判）
_SIDES = ("BUY", "SELL")


# ============================================================ 开关与上下文

def ui_enabled() -> bool:
    """整模块开关。默认开启；``QMT_TRADE_UI=0`` 一键关闭。"""
    return str(os.environ.get("QMT_TRADE_UI", "1")).strip() not in ("0", "false", "False", "")


def _engine():
    return current_app.config["engine"]


def zone_of(engine) -> str:
    """``live`` / ``paper``。引擎没暴露 exec_mode 时按 live 处理（保守：宁可拒绝）。"""
    m = str(getattr(engine, "exec_mode", "") or "").strip().lower()
    return m if m in ("live", "paper") else "live"


def zone_allowed(zone: str) -> bool:
    if zone == "live":
        return True
    return str(os.environ.get("QMT_TRADE_UI_ALLOW_PAPER", "0")).strip() in (
        "1", "true", "True")


def _audit(payload: dict) -> None:
    """追加审计行。任何异常都吞掉——审计失败不得影响下单。"""
    try:
        p = OO.base_dir(_engine()) / AUDIT_NAME
        with io.open(str(p), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as e:                           # noqa: BLE001
        logger.warning("[交易面板] 审计落盘失败（忽略）: %s", e)


# ============================================================ 行情 / 账户

def _price_of(raw: Any) -> float:
    """从 tick 里取价格。引擎的 ``latest_ticks()`` 返回 dict，也可能被替换成对象。"""
    try:
        if isinstance(raw, dict):
            for k in ("price", "lastPrice", "last_price"):
                v = raw.get(k)
                if v is not None:
                    return float(v)
            return 0.0
        return float(getattr(raw, "price", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _tick_shim(raw: Any):
    """``decide()`` 只认 ``tick.price``（见 core/owner_order.py:345-347）。

    这里把引擎的行情字典包成同样形态的最小对象——**不复制校验逻辑**，
    只是喂给它同一种输入。
    """
    if raw is None:
        return None
    px = _price_of(raw)
    if px <= 0:
        return None

    class _T:
        pass

    t = _T()
    t.price = px
    return t


def _ticks_map(engine) -> Dict[str, Any]:
    try:
        return dict(engine.latest_ticks() or {})
    except Exception as e:                           # noqa: BLE001
        logger.warning("[交易面板] 取行情失败: %s", e)
        return {}


def _positions_payload(engine) -> List[dict]:
    out: List[dict] = []
    try:
        for code, p in (getattr(engine, "_positions", {}) or {}).items():
            qty = int(getattr(p, "quantity", 0) or 0)
            if qty <= 0:
                continue
            out.append({
                "code": code,
                "quantity": qty,
                "available": int(getattr(p, "available", 0) or 0),
                "avg_cost": round(float(getattr(p, "avg_cost", 0.0) or 0.0), 3),
                "last_price": round(float(getattr(p, "last_price", 0.0) or 0.0), 3),
            })
    except Exception as e:                           # noqa: BLE001
        logger.warning("[交易面板] 取持仓失败: %s", e)
    out.sort(key=lambda x: x["code"])
    return out


def _risk_payload(engine) -> dict:
    risk = getattr(engine, "risk", None)
    halted = bool(getattr(risk, "_halted", False))
    return {
        "halted": halted,
        "halt_reason": str(getattr(risk, "_halt_reason", "") or ""),
        "peak_asset": round(float(getattr(risk, "_peak_asset", 0.0) or 0.0), 2),
    }


# ============================================================ 队列视图

def _channel_cfg() -> dict:
    return dict(STRATEGY_PARAMS.get("owner_order") or {})


def _used_today(engine) -> int:
    day = datetime.now().date().isoformat()
    n = 0
    for row in (OO.load_done(engine, logger=logger) or {}).values():
        if not isinstance(row, dict):
            continue
        if str(row.get("ts") or "")[:10] == day:
            n += 1
    return n


def _queue_payload(engine, limit: int = 60) -> List[dict]:
    """队列 + 账本合并成一张可读表（新的在前）。"""
    done = OO.load_done(engine, logger=logger) or {}
    rows: List[dict] = []
    for ins in OO.read_instructions(engine, logger=logger):
        oid = str(ins.get("id") or "").strip()
        if not oid:
            continue
        d = done.get(oid) or {}
        rows.append({
            "id": oid,
            "code": str(ins.get("code") or ""),
            "side": str(ins.get("side") or ""),
            "lots": ins.get("lots"),
            "amount": ins.get("amount"),
            "price_cap": ins.get("price_cap"),
            "valid_until": ins.get("valid_until"),
            "note": str(ins.get("note") or ""),
            "status": str(d.get("status") or "pending"),
            "reason": str(d.get("reason") or ""),
            "ts": str(d.get("ts") or ""),
        })
    # 只保留最近 limit 条（队列是 append-only，会越来越长）
    return rows[-limit:][::-1]


# ============================================================ 校验

def _normalize(body: dict) -> Tuple[Optional[dict], str]:
    """把请求体整理成一条候选指令；返回 (ins, 错误原因)。"""
    code = str(body.get("code") or "").strip().upper()
    if not _CODE_RE.match(code):
        return None, "code 必须形如 600000.SH（6 位数字 + .SH/.SZ/.BJ）"
    side = str(body.get("side") or "").strip().upper()
    if side not in _SIDES:
        return None, "side 必须是 BUY 或 SELL"
    try:
        lots = int(body.get("lots"))
    except (TypeError, ValueError):
        return None, "lots 必须是整数（股数）"
    if lots <= 0:
        return None, "lots 必须为正"
    note = str(body.get("note") or "").strip()
    if len(note) > 80:
        return None, "note 最多 80 字"
    cap = body.get("price_cap")
    try:
        cap = float(cap) if cap not in (None, "") else 0.0
    except (TypeError, ValueError):
        return None, "price_cap 必须是数字（留空=不限价）"
    if cap < 0:
        return None, "price_cap 不能为负"
    vu = str(body.get("valid_until") or "").strip() or date.today().isoformat()
    try:
        if date.fromisoformat(vu) < date.today():
            return None, "valid_until 不能早于今天（过期指令不会执行）"
    except ValueError:
        return None, "valid_until 格式必须是 YYYY-MM-DD"
    ins = {"code": code, "side": side, "lots": lots,
           "valid_until": vu, "note": note}
    if cap > 0:
        ins["price_cap"] = cap
    return ins, ""


def _new_id() -> str:
    return "ui-%s-%s" % (datetime.now().strftime("%Y%m%d-%H%M%S"),
                         secrets.token_hex(3))


def _precheck(engine, ins: dict) -> Tuple[str, str, int]:
    """复用引擎同一个纯函数做预检（core/owner_order.py::decide）。"""
    cfg = _channel_cfg()
    ticks = _ticks_map(engine)
    tick = _tick_shim(ticks.get(ins["code"]))
    try:
        return OO.decide(ins, now=datetime.now(),
                         positions=getattr(engine, "_positions", {}) or {},
                         tick=tick, day_n=_used_today(engine), cfg=cfg,
                         today=date.today())
    except Exception as e:                           # noqa: BLE001
        logger.warning("[交易面板] 预检异常（按跳过处理）: %s", e)
        return OO.ST_SKIP, "预检异常：%s" % e, 0


# ============================================================ 令牌

def _token_store() -> Dict[str, float]:
    store = current_app.config.get("_trade_tokens")
    if not isinstance(store, dict):
        store = {}
        current_app.config["_trade_tokens"] = store
    return store


def _issue_token() -> Tuple[str, int]:
    tok = secrets.token_urlsafe(16)
    now = time.time()
    store = _token_store()
    for k in [k for k, exp in store.items() if exp < now]:
        store.pop(k, None)
    store[tok] = now + TOKEN_TTL_SEC
    return tok, TOKEN_TTL_SEC


def _consume_token(tok: str) -> bool:
    """单次有效：取走即删。"""
    store = _token_store()
    exp = store.pop(str(tok or ""), None)
    return bool(exp) and float(exp) >= time.time()


# ============================================================ 页面

@bp.route("/trade")
def trade_page():
    """兼容入口：实盘买卖已合并进主页 /，本路由渲染同一份主页（见 trade.html）。

    开关（QMT_TRADE_UI=0 / paper 分区）与 200/403 返回码保持原语义不变；
    页面加载后由 index.html 末尾脚本把地址归一到 /#trade 并滚动定位。
    """
    if not ui_enabled():
        return ("实盘交易面板已被 QMT_TRADE_UI=0 关闭", 403)
    e = _engine()
    if not zone_allowed(zone_of(e)):
        return ("paper 区未开启交易面板（需 QMT_TRADE_UI_ALLOW_PAPER=1）", 403)
    try:
        names = build_name_map(dynamic_universe=getattr(e, "dynamic_universe", None))
    except Exception:                                # noqa: BLE001
        names = {}
    return render_template("trade.html", universe=UNIVERSE, stock_names=names,
                           zone=zone_of(e))


# ============================================================ API

@bp.route("/api/trade/status")
def api_trade_status():
    if not ui_enabled():
        return jsonify({"ok": False, "reason": "QMT_TRADE_UI=0"}), 403
    e = _engine()
    zone = zone_of(e)
    cfg = _channel_cfg()
    # 【必须区分「券商权威资产」与「构造初值」】live 引擎刚启动（或断链期）时，
    # 内存里的 _cash 还是 INITIAL_CASH=1,000,000、_positions 为空 —— 这是构造
    # 初值，不是账户真相（_save_engine_state 的 2026-11-08 注释有同样记载）。
    # 面板若把它当「可用现金」显示出来，等于让人对着一个假数字决定真金白银的仓位。
    # 判据用风控层同一个字段 _broker_total_asset（>0 才可信）。
    trusted = float(getattr(e, "_broker_total_asset", 0.0) or 0.0) > 0.0
    total = None
    if trusted:
        try:
            total = e._total_asset()
        except Exception:                            # noqa: BLE001
            total = None
    return jsonify({
        "ok": True,
        "zone": zone,
        "zone_allowed": zone_allowed(zone),
        "engine": {
            "exec_mode": str(getattr(e, "exec_mode", "")),
            "data_mode": str(getattr(e, "data_mode", "")),
            # 不可信时一律给 None，前端显示「未同步券商」，绝不显示构造初值
            "asset_trusted": bool(trusted),
            "cash": (round(float(getattr(e, "_cash", 0.0) or 0.0), 2) if trusted else None),
            "total_asset": (round(float(total), 2) if (trusted and total) else None),
        },
        "channel": {
            "enabled": bool(cfg.get("enabled")),
            "max_amount": float(cfg.get("max_amount") or 0.0),
            "max_per_day": int(cfg.get("max_per_day") or 0),
            "used_today": _used_today(e),
        },
        "risk": _risk_payload(e),
        "positions": (_positions_payload(e) if trusted else []),
        "queue": _queue_payload(e),
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


@bp.route("/api/trade/token", methods=["POST"])
def api_trade_token():
    if not ui_enabled():
        return jsonify({"ok": False, "reason": "QMT_TRADE_UI=0"}), 403
    if not zone_allowed(zone_of(_engine())):
        return jsonify({"ok": False, "reason": "paper 区未开启"}), 403
    tok, ttl = _issue_token()
    return jsonify({"ok": True, "token": tok, "ttl_sec": ttl})


@bp.route("/api/trade/submit", methods=["POST"])
def api_trade_submit():
    if not ui_enabled():
        return jsonify({"ok": False, "reason": "QMT_TRADE_UI=0"}), 403
    e = _engine()
    zone = zone_of(e)
    if not zone_allowed(zone):
        return jsonify({"ok": False, "reason": "paper 区未开启交易面板"}), 403

    tok = request.headers.get("X-Trade-Token") or (request.form.get("token") if request.form else "")
    if not _consume_token(tok):
        return jsonify({"ok": False,
                        "reason": "令牌无效或已过期（页面会自动重取，请重试）"}), 400

    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict) or not body:
        return jsonify({"ok": False, "reason": "请求体必须是 JSON 对象"}), 400

    ins, err = _normalize(body)
    if err:
        _audit({"ts": datetime.now().isoformat(), "event": "submit_reject",
                "reason": err, "body": body, "remote": request.remote_addr})
        return jsonify({"ok": False, "reason": err}), 400

    # 通道关闭时直接拒——写进去也永远不会被执行，只会让队列看起来"有单"。
    cfg = _channel_cfg()
    if not cfg.get("enabled"):
        _audit({"ts": datetime.now().isoformat(), "event": "submit_reject",
                "reason": "channel disabled", "body": ins, "remote": request.remote_addr})
        return jsonify({"ok": False,
                        "reason": "OWNER 指令通道未启用（STRATEGY_PARAMS.owner_order.enabled=False）"}), 400

    # 幂等：id 由服务端生成，且必须不在 done 里
    # 【别写成 `load_done(...) or {}`】load_done 会把 dict 缓存在 engine._owner_done；
    # 账本为空时它是**假值**，`or {}` 会把它换成一个临时 dict ⇒ record() 的写入
    # 只落到文件、内存缓存仍是空 ⇒ 同一进程内后续读取看不到刚写的状态
    # （2026-11-10 通过 test_cancel_only_pending_and_marks_done 抓到）。
    done = OO.load_done(e, logger=logger)
    if not isinstance(done, dict):
        done = {}
    for _ in range(5):
        oid = _new_id()
        if oid not in done:
            break
    else:
        return jsonify({"ok": False, "reason": "生成指令 id 失败，请重试"}), 500
    ins = dict(ins)
    ins["id"] = oid
    if ins.get("note"):
        pass
    else:
        ins["note"] = "面板下单"

    # ---- 预检（与引擎同一个纯函数）----
    status, reason, qty = _precheck(e, ins)
    if status == OO.ST_REJECT:
        _audit({"ts": datetime.now().isoformat(), "event": "submit_reject",
                "reason": reason, "ins": ins, "remote": request.remote_addr})
        return jsonify({"ok": False, "reason": "引擎侧会拒绝：%s" % reason,
                        "precheck": {"status": status, "reason": reason}}), 400

    # ---- 追加队列（append-only）----
    path = OO.queue_path(e)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        need_nl = False
        if path.exists() and path.stat().st_size > 0:
            with io.open(str(path), "rb") as fh:
                fh.seek(-1, os.SEEK_END)
                need_nl = fh.read(1) != b"\n"
        with io.open(str(path), "a", encoding="utf-8", newline="\n") as fh:
            if need_nl:
                fh.write("\n")
            fh.write(json.dumps(ins, ensure_ascii=False) + "\n")
    except Exception as e:                           # noqa: BLE001
        logger.warning("[交易面板] 队列写入失败: %s", e)
        return jsonify({"ok": False, "reason": "队列写入失败：%s" % e}), 500

    _audit({"ts": datetime.now().isoformat(), "event": "submit",
            "ins": ins, "precheck": {"status": status, "reason": reason, "qty": qty},
            "remote": request.remote_addr})
    logger.warning("[交易面板] %s %s %s 股 已入队（预检 %s）",
                   oid, ins["side"], ins["lots"], status or "-")
    return jsonify({"ok": True, "id": oid, "instruction": ins,
                    "precheck": {"status": status, "reason": reason, "qty": qty}})


@bp.route("/api/trade/cancel", methods=["POST"])
def api_trade_cancel():
    if not ui_enabled():
        return jsonify({"ok": False, "reason": "QMT_TRADE_UI=0"}), 403
    e = _engine()
    if not zone_allowed(zone_of(e)):
        return jsonify({"ok": False, "reason": "paper 区未开启交易面板"}), 403

    tok = request.headers.get("X-Trade-Token") or ""
    if not _consume_token(tok):
        return jsonify({"ok": False, "reason": "令牌无效或已过期"}), 400

    body = request.get_json(silent=True) or {}
    oid = str(body.get("id") or "").strip()
    if not oid:
        return jsonify({"ok": False, "reason": "缺少 id"}), 400

    # 【别写成 `load_done(...) or {}`】load_done 会把 dict 缓存在 engine._owner_done；
    # 账本为空时它是**假值**，`or {}` 会把它换成一个临时 dict ⇒ record() 的写入
    # 只落到文件、内存缓存仍是空 ⇒ 同一进程内后续读取看不到刚写的状态
    # （2026-11-10 通过 test_cancel_only_pending_and_marks_done 抓到）。
    done = OO.load_done(e, logger=logger)
    if not isinstance(done, dict):
        done = {}
    prev = done.get(oid)
    if prev:
        # 已记账（submit/ok/reject）⇒ 可能已经下过单，取消会让人误以为没下过。
        return jsonify({"ok": False,
                        "reason": "该指令已记账（status=%s），不能取消"
                                  % (prev.get("status") if isinstance(prev, dict) else prev)}), 400

    # 队列里确实存在才允许取消（防随手编个 id 污染账本）
    exists = any(str(x.get("id") or "").strip() == oid
                 for x in OO.read_instructions(e, logger=logger))
    if not exists:
        return jsonify({"ok": False, "reason": "队列中不存在该 id"}), 404

    OO.record(e, done, oid, "cancelled", "在面板上取消（未执行）",
              logger=logger)
    _audit({"ts": datetime.now().isoformat(), "event": "cancel", "id": oid,
            "remote": request.remote_addr})
    logger.warning("[交易面板] 指令 %s 已取消（记入 done，引擎将跳过）", oid)
    return jsonify({"ok": True, "id": oid})

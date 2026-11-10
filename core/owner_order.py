# -*- coding: utf-8 -*-
"""OWNER 指令单通道（v1.1 增量，2026-11-10 建立）。

## 为什么需要它

系统原有两条「绕过信号闸门」的通道，都**故意**拒绝给已持仓的票加仓：

  · `manual_entry`（观察篮）：`if code in held: continue`，且只有金额没有数量；
  · `owner_reentry`（回补）：首行 `if 已持仓: return`，且只买不卖。

于是「我此刻要买/卖 N 股某代码」这件事在系统里没有通道。直接手改 `signal.json`
更糟：绕过引擎账本 ⇒ 无 `fills` 行 ⇒ `open_date` 回灌查不到 ⇒ T+1 与建仓保护期
对该仓双双失效（2026-11-09 刚修的那个 P0 的反面）。

## 本模块的定位

**只做决策，不落单。** 落单一律交回引擎的 `_handle_buy` / `_handle_sell`
（全系统唯一落单口），因此熔断、日内次数、现金夹紧、行情新鲜度、总资产可信度、
T+1、建仓保护期、`max_positions` 全部照旧生效——这条是设计红线。

## 数据

    storage/owner_orders.jsonl       追加式指令队列（OWNER 写，一行一条）
    storage/owner_orders_done.json   幂等账本 {id: {status, ts, reason, order_id}}

指令字段：

    id          必填，唯一。**幂等的唯一依据**，重复 id 永远不会重复提交。
    code        必填，如 "510050.SH"
    side        必填，"BUY" / "SELL"
    lots        股数（必须是 100 的整数倍）；与 amount 二选一
    amount      买入金额（元），按现价向下取整到 100 股
    price_cap   买入价格上限（>0 时不追高，现价高于它就一直等，不消耗 id）
    valid_until 有效期（"YYYY-MM-DD"，含当天）；过期标记 expired 不再重试
    note        备注，写进信号 reason 与日志

## 状态机

    skip    —— 本轮条件未满足（价格未落入区间/无行情），**不记账**，下一轮再看
    reject  —— 明确不执行（超上限/无持仓/T+1/非法字段），**记账**，不再重试
    submit  —— 已交给落单口（买入）；**记账**，同 id 不再提交
    ok      —— 卖出确认成交（`_handle_sell` 返回 True）；记账
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

QUEUE_NAME = "owner_orders.jsonl"
DONE_NAME = "owner_orders_done.json"

DEFAULT_MAX_AMOUNT = 1000.0
DEFAULT_MAX_PER_DAY = 5

# 指令状态
ST_SKIP = "skip"
ST_REJECT = "reject"
ST_SUBMIT = "submit"
ST_OK = "ok"


def base_dir(engine) -> Path:
    """指令文件目录。默认与账本同目录（live=storage/，paper=storage_paper/）。

    允许 `QMT_OWNER_ORDERS_DIR` 覆盖（测试用），与项目其它路径覆盖保持同一风格。
    """
    env = os.environ.get("QMT_OWNER_ORDERS_DIR")
    if env:
        return Path(env)
    try:
        return Path(engine.storage.db_path).parent
    except Exception:
        return Path("storage")


def queue_path(engine) -> Path:
    return base_dir(engine) / QUEUE_NAME


def done_path(engine) -> Path:
    return base_dir(engine) / DONE_NAME


def load_done(engine, logger=None) -> Dict[str, Any]:
    """读幂等账本。解析失败按空处理（宁可重读一遍，也不要整条通道失效）。"""
    cached = getattr(engine, "_owner_done", None)
    if isinstance(cached, dict):
        return cached
    done: Dict[str, Any] = {}
    p = done_path(engine)
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                done = raw
        except Exception as e:                       # noqa: BLE001
            if logger:
                logger.warning("[OWNER指令] 幂等账本解析失败，按空处理: %s", e)
    engine._owner_done = done
    return done


def save_done(engine, done: Dict[str, Any], logger=None) -> None:
    p = done_path(engine)
    tmp = p.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(done, f, ensure_ascii=False, sort_keys=True)
        os.replace(str(tmp), str(p))
    except Exception as e:                           # noqa: BLE001
        if logger:
            logger.warning("[OWNER指令] 幂等账本写盘失败（本轮指令已执行，重启后可能重试）: %s", e)


def record(engine, done: Dict[str, Any], oid: str, status: str,
           reason: str = "", logger=None, notify=None) -> None:
    done[oid] = {
        "status": status,
        "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "reason": reason,
    }
    save_done(engine, done, logger=logger)
    if reason and status in (ST_REJECT,):
        if logger:
            logger.warning("[OWNER指令] %s 拒绝: %s", oid, reason)
        if notify:
            try:
                notify("WARNING", "交易", "[OWNER指令] %s 被拒：%s" % (oid, reason))
            except Exception:                        # noqa: BLE001
                pass


def read_instructions(engine, logger=None) -> List[dict]:
    """读指令队列。坏行跳过并告警，绝不让一行坏数据打断整批。"""
    p = queue_path(engine)
    out: List[dict] = []
    if not p.exists():
        return out
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:                           # noqa: BLE001
        if logger:
            logger.warning("[OWNER指令] 队列读取失败: %s", e)
        return out
    for i, raw in enumerate(text.splitlines(), 1):
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        try:
            ins = json.loads(raw)
        except Exception:                            # noqa: BLE001
            if logger:
                logger.warning("[OWNER指令] 第 %d 行无法解析，跳过: %.60s", i, raw)
            continue
        if isinstance(ins, dict):
            out.append(ins)
        elif logger:
            logger.warning("[OWNER指令] 第 %d 行不是对象，跳过", i)
    return out


def _norm_side(v: Any) -> str:
    s = str(v or "").strip().upper()
    return s if s in ("BUY", "SELL") else ""


def _int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def decide(ins: dict, *, now: datetime, positions: Dict[str, Any],
           tick: Any, day_n: int, cfg: dict,
           today: Optional[date] = None) -> Tuple[str, str, int]:
    """单条指令的判定（纯函数，不碰 IO，不落单）。

    返回 ``(status, reason, qty)``：
      - status 为 ``skip`` 时 qty 无意义（本轮不做，下轮再看）
      - ``reject`` 时 reason 给出可审计的原因
      - ``submit`` / ``ok`` 时 qty 为要交易的数量（股）
    """
    today = today or now.date()
    oid = str(ins.get("id") or "").strip()
    if not oid:
        return ST_REJECT, "缺少 id（幂等键，必需）", 0
    code = str(ins.get("code") or "").strip().upper()
    if not code:
        return ST_REJECT, "缺少 code", 0
    side = _norm_side(ins.get("side"))
    if not side:
        return ST_REJECT, "side 必须是 BUY / SELL", 0

    vu = str(ins.get("valid_until") or "").strip()
    if vu:
        try:
            if today > date.fromisoformat(vu):
                return ST_REJECT, "已过期（valid_until=%s）" % vu, 0
        except ValueError:
            return ST_REJECT, "valid_until 格式非法（应为 YYYY-MM-DD）", 0

    max_amt = _float(cfg.get("max_amount")) or DEFAULT_MAX_AMOUNT
    max_n = _int(cfg.get("max_per_day")) or DEFAULT_MAX_PER_DAY
    if max_n > 0 and day_n >= max_n:
        return ST_REJECT, "已达每日指令上限 %d 条" % max_n, 0

    price = 0.0
    if tick is not None:
        price = _float(getattr(tick, "price", 0)) or 0.0

    if side == "BUY":
        cap = _float(ins.get("price_cap")) or 0.0
        lots = _int(ins.get("lots")) or 0
        amount = _float(ins.get("amount")) or 0.0
        if lots <= 0 and amount <= 0:
            return ST_REJECT, "lots 与 amount 至少要给一个", 0
        if lots > 0 and lots % 100 != 0:
            return ST_REJECT, "lots 必须是 100 的整数倍（当前 %d）" % lots, 0
        if price <= 0:
            return ST_SKIP, "暂无行情价", 0
        if cap > 0 and price > cap:
            return ST_SKIP, "现价 %.3f 高于上限 %.3f，等回落" % (price, cap), 0
        qty = lots if lots > 0 else int(amount // price // 100 * 100)
        if qty <= 0:
            return ST_REJECT, "按现价 %.3f 买不到 100 股（amount=%.2f）" % (price, amount), 0
        if qty * price > max_amt:
            return ST_REJECT, "金额 %.2f 超过单笔上限 %.2f" % (qty * price, max_amt), 0
        pos = positions.get(code)
        if pos is not None and int(getattr(pos, "quantity", 0) or 0) > 0:
            # 允许加仓——这是本通道相对 manual_entry/owner_reentry 的唯一放宽点
            pass
        return ST_SUBMIT, "", qty

    # SELL
    pos = positions.get(code)
    if pos is None or int(getattr(pos, "quantity", 0) or 0) <= 0:
        return ST_REJECT, "无持仓，无法卖出", 0
    lots = _int(ins.get("lots")) or 0
    if lots <= 0:
        return ST_REJECT, "卖出必须给 lots（股数）", 0
    if lots % 100 != 0 and lots != int(getattr(pos, "quantity", 0) or 0):
        return ST_REJECT, "lots 必须是 100 的整数倍", 0
    held = int(getattr(pos, "quantity", 0) or 0)
    if lots > held:
        return ST_REJECT, "卖出 %d 股超过持仓 %d 股" % (lots, held), 0
    if price <= 0:
        return ST_SKIP, "暂无行情价", 0
    return ST_SUBMIT, "", lots


def run_step(engine, ticks: Dict[str, Any], current_prices: Dict[str, float],
             *, cfg: dict, logger=None, notify=None, signal_cls=None,
             get_name=None, t1_lock=None, save_signal=None,
             handle_buy=None, handle_sell=None, now: Optional[datetime] = None) -> dict:
    """引擎每轮调用一次：把队列里「未处理且条件满足」的指令交给落单口。

    设计上所有副作用都通过入参传入（save_signal / handle_buy / handle_sell），
    这样本模块可以完全脱机测试，也避免与 engine 的循环 import。
    """
    stats = {"submit": 0, "reject": 0, "skip": 0, "ok": 0}
    if not isinstance(cfg, dict) or not cfg.get("enabled"):
        return stats
    # ★ mock（合成行情）期间整条通道不动作。
    # 为什么必须在**这里**拦、而不是等 _handle_buy 自己拦：_handle_buy 没有返回值，
    # 引擎在 mock 下拦掉单子后我的账本仍会记 submit —— 于是指令被"消耗"却没有任何成交，
    # 事后看 done 文件会以为下过单。宁可整轮不动、id 留到下轮。
    if str(getattr(engine, "data_mode", "xtdata")) == "mock" \
            and not getattr(engine, "allow_mock_trading", False):
        if logger:
            logger.debug("[OWNER指令] 行情源=mock，本通道整体跳过（不消耗 id）")
        return stats

    now = now or datetime.now()
    today = now.date()
    done = load_done(engine, logger=logger)

    # 每日计数跨日重置
    if getattr(engine, "_owner_day", "") != today.isoformat():
        engine._owner_day = today.isoformat()
        engine._owner_day_n = 0
    day_n = int(getattr(engine, "_owner_day_n", 0) or 0)

    for ins in read_instructions(engine, logger=logger):
        oid = str(ins.get("id") or "").strip()
        if not oid or oid in done:
            continue
        code = str(ins.get("code") or "").strip().upper()
        side = _norm_side(ins.get("side"))
        tick = ticks.get(code) if code else None

        status, reason, qty = decide(
            ins, now=now, positions=engine._positions, tick=tick,
            day_n=day_n, cfg=cfg, today=today)

        if status == ST_SKIP:
            stats["skip"] += 1
            continue                                   # 不记账，下轮再看
        if status == ST_REJECT:
            record(engine, done, oid, ST_REJECT, reason, logger=logger, notify=notify)
            stats["reject"] += 1
            continue

        price = _float(getattr(tick, "price", 0)) or 0.0
        note = str(ins.get("note") or "")
        reason_txt = "owner_order(%s)" % (note or oid)

        if side == "BUY":
            sig = signal_cls(ts=now, code=code, name=get_name(code) if get_name else code,
                             side="BUY", score=0.0, price=price, reason=reason_txt)
            if save_signal:
                save_signal(sig)
            # 用金额锁定股数：fixed_amount 在 _handle_buy 内「÷现价向下取整到 100 股」
            if handle_buy:
                handle_buy(sig, tick, current_prices, fixed_amount=qty * price)
            record(engine, done, oid, ST_SUBMIT, "买入 %d 股 @%.3f 已交落单口" % (qty, price),
                   logger=logger, notify=notify)
            engine._owner_day_n = day_n = day_n + 1
            stats["submit"] += 1
            if logger:
                logger.warning("[OWNER指令] %s 买入 %s %d 股 @%.3f（%s）",
                               oid, code, qty, price, note or "-")
            continue

        # SELL：T+1 与保护期的判定交给 _handle_sell，这里先做一次显式预检，
        # 让「被 T+1 拦下」在账本里留下 reject 而不是含糊的 submit。
        pos = engine._positions.get(code)
        if t1_lock is not None and pos is not None:
            try:
                if t1_lock(getattr(pos, "open_date", None), today):
                    record(engine, done, oid, ST_REJECT, "T+1 锁定（当日买入不可卖）",
                           logger=logger, notify=notify)
                    stats["reject"] += 1
                    continue
            except Exception:                           # noqa: BLE001
                pass
        sig = signal_cls(ts=now, code=code, name=get_name(code) if get_name else code,
                         side="SELL", score=0.0, price=price, reason=reason_txt)
        if save_signal:
            save_signal(sig)
        ok = False
        if handle_sell and pos is not None:
            try:
                ok = bool(handle_sell(sig, pos, now))
            except Exception as e:                      # noqa: BLE001
                if logger:
                    logger.warning("[OWNER指令] 卖出异常 %s: %s", oid, e)
        if ok:
            record(engine, done, oid, ST_OK, "卖出 %d 股已清仓" % qty,
                   logger=logger, notify=notify)
            engine._owner_day_n = day_n = day_n + 1
            stats["ok"] += 1
            if logger:
                logger.warning("[OWNER指令] %s 卖出 %s %d 股 @%.3f（%s）",
                               oid, code, qty, price, note or "-")
        else:
            record(engine, done, oid, ST_REJECT, "落单口未成交（保护期/委托失败/零仓位）",
                   logger=logger, notify=notify)
            stats["reject"] += 1

    return stats

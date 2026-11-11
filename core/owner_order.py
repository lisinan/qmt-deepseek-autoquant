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
    price_floor 卖出价格下限（>0 时现价低于它就一直等，不消耗 id）
    max_amount  单笔金额上限（元），覆盖全局 owner_order.max_amount，只对本条生效
    allow_large_position
                买入时绕过「单仓上限 40%」（显式股数、不乘连亏降仓系数）。
                默认关闭；仅 OWNER 明确要接回大额底仓时置 true。
    valid_until 有效期（"YYYY-MM-DD"，含当天）；过期标记 expired 不再重试
    note        备注，写进信号 reason 与日志

## 状态机

    skip    —— 本轮条件未满足（价格未落入区间/无行情），**不记账**，下一轮再看
    reject  —— 明确不执行（超上限/无持仓/T+1/非法字段），**记账**，不再重试
    submit  —— 已交给落单口（买入）；**记账**，同 id 不再提交
    ok      —— 卖出确认成交（`_handle_sell` 返回 True）；记账

## 池外代码怎么拿到行情（2026-11-10 补）

`_subscribe_codes()` 只订阅「UNIVERSE + VALIDATION_UNIVERSE + 动态池 + 持仓」，
所以「盘中买一只池外代码」时没有 tick，`decide()` 会永远 `skip / 暂无行情价`。
`fill_missing_ticks()` 由引擎在 `run_step` 之前调用，按需给这些代码补一条行情，
**不改订阅清单**；成交后持仓会把该代码带进 `_subscribe_codes()`，由持仓同步
那条自愈路径接管常态订阅。
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


def pending_codes(engine, logger=None) -> List[str]:
    """队列里**尚未记账**的指令代码（去重、只保留 BUY/SELL）。

    唯一用途：让引擎给「订阅清单之外的代码」按需补行情，见 ``fill_missing_ticks``。
    不做任何记账、不落单、不改状态。
    """
    done = load_done(engine, logger=logger)
    out: List[str] = []
    for ins in read_instructions(engine, logger=logger):
        oid = str(ins.get("id") or "").strip()
        if not oid or oid in done:
            continue
        if not _norm_side(ins.get("side")):
            continue
        code = str(ins.get("code") or "").strip().upper()
        if code:
            out.append(code)
    return list(dict.fromkeys(out))


def fill_missing_ticks(engine, ticks: Dict[str, Any], *, get_ticks=None,
                       to_tick=None, logger=None, max_codes: int = 5) -> int:
    """给指令队列里「不在 ``ticks`` 中」的代码按需补一条行情；返回补齐只数。

    ## 为什么必须有这一步（2026-11-10，真实卡点）

    行情订阅清单由 ``engine._subscribe_codes()`` 决定：
    ``UNIVERSE + VALIDATION_UNIVERSE + 动态活跃池 + 持仓``。
    OWNER 想在盘中买一只**池外代码**（实测 510300.SH）时，它不在这四项里
    ⇒ 没有 tick ⇒ ``decide()`` 永远返回 ``skip / 暂无行情价``，通道看起来
    「活着但什么也不做」。而绕开它的两种做法都不可接受：
      · 改 ``config/settings.py`` 加白名单 —— 盘中改配置必须重启实盘，且该文件
        对 Agent 只读（WinError 5）；
      · 直接手写 ``signal.json`` —— 绕过引擎账本，无 ``fills`` 行 ⇒
        ``open_date`` 回灌失效 ⇒ T+1 与建仓保护期对该仓双双失效。

    而 ``tushare`` 适配器的 ``get_ticks()`` 本来就能按需拉任意代码（冷代码走
    ``_fetch_realtime_quote``），只是没人调它。所以这里只补「缺的那几只」，
    不动订阅清单本身 —— 一旦成交，持仓会把该代码带进 ``_subscribe_codes()``，
    由券商持仓同步那条自愈路径接管常态订阅。

    ## 边界
      · 只读队列，不记账；行情取不到就返回 0，本轮按「无行情」照常 skip；
      · 任何异常都不外抛（这是挂在主循环里的东西）；
      · 单轮最多补 ``max_codes`` 只，防止队列里塞了几百条时每轮打一轮网络。
    """
    if not isinstance(ticks, dict) or get_ticks is None or to_tick is None:
        return 0
    try:
        missing = [c for c in pending_codes(engine, logger=logger) if c not in ticks]
    except Exception as e:                          # noqa: BLE001
        if logger:
            logger.debug("[OWNER指令] 待补行情代码解析失败(忽略): %s", e)
        return 0
    if not missing:
        return 0
    if len(missing) > max_codes:
        if logger:
            logger.warning("[OWNER指令] 待补行情 %d 只超过单轮上限 %d，本轮只补前 %d 只",
                           len(missing), max_codes, max_codes)
        missing = missing[:max_codes]
    try:
        raw = get_ticks(missing) or {}
    except Exception as e:                          # noqa: BLE001
        if logger:
            logger.warning("[OWNER指令] 按需取行情失败（本轮按无行情处理）: %s", e)
        return 0

    # 同一只票「每轮都要补」是常态（等 price_cap / price_floor 期间），
    # 所以 INFO 只按「代码 + 交易日」打一次，其余降 DEBUG，避免 8s 一行刷屏。
    _day = datetime.now().date().isoformat()
    if getattr(engine, "_owner_fill_day", "") != _day:
        engine._owner_fill_day = _day
        engine._owner_fill_logged = set()
    logged = getattr(engine, "_owner_fill_logged", None)
    if not isinstance(logged, set):
        logged = set()
        engine._owner_fill_logged = logged

    added = 0
    for code in missing:
        r = raw.get(code)
        if not isinstance(r, dict):
            continue
        try:
            t = to_tick(code, r)
        except Exception as e:                      # noqa: BLE001
            if logger:
                logger.debug("[OWNER指令] 组装 tick 失败 %s: %s", code, e)
            continue
        if t is None or not getattr(t, "is_valid", False):
            continue
        ticks[code] = t
        added += 1
        if logger:
            if code in logged:
                logger.debug("[OWNER指令] %s 按需行情 @%.3f", code,
                             float(getattr(t, "price", 0) or 0))
            else:
                logged.add(code)
                logger.info("[OWNER指令] %s 不在订阅清单，已按需补行情 @%.3f（源=%s）",
                            code, float(getattr(t, "price", 0) or 0),
                            getattr(t, "source", "?"))
    return added


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
    # 单笔金额上限的**逐条覆盖**（2026-11-10）。全局 1000 元是"先小后抬"的
    # 保守值，但 OWNER 有时需要一次大额（如接回 300308 一手 4.7 万）。
    # 只影响本条目，全局配置不变；金额上限仍是硬约束，只是基准可被 OWNER
    # 在指令里显式上调 —— 这条通道本来就是 OWNER 亲笔写文件，无需二次授权。
    _own_max = _float(ins.get("max_amount"))
    if _own_max is not None and _own_max > 0:
        max_amt = _own_max
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
    # 卖出价格下限（2026-11-10 新增）：OWNER 常要「涨到成本以上再卖」，
    # 而只有 price_cap（买单不追高）没有镜像字段。低于下限时返回 skip ——
    # 不记账、不消耗 id、不计入每日条数，下一轮继续看。
    floor = _float(ins.get("price_floor")) or 0.0
    if floor > 0 and price < floor:
        return ST_SKIP, "现价 %.3f 低于卖出下限 %.3f，等涨" % (price, floor), 0
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
            _prev_buy = getattr(engine, "_last_buy_placed", None)
            # 清掉上一轮的拦截原因，避免把旧原因读成本轮原因
            engine._last_buy_block_reason = ""
            if handle_buy:
                # allow_large_position（2026-11-10）：显式股数 + 绕过「单仓上限 40%」。
                # 走 _handle_buy 既有的 owner_reentry 分支 —— 该分支本来就是为
                # 「OWNER 显式指定股数个，接回底仓」建的：股数不乘连亏降仓系数
                # （100×0.5=50 股不是整百，券商必拒），且 can_open 的
                # allow_large_position=True 放行超配。
                # 默认 False ⇒ 一切照旧（40% 上限、连亏系数全部生效）。
                if ins.get("allow_large_position"):
                    handle_buy(sig, tick, current_prices,
                               fixed_amount=qty * price, owner_reentry=True)
                else:
                    handle_buy(sig, tick, current_prices, fixed_amount=qty * price)
            _now_buy = getattr(engine, "_last_buy_placed", None)
            # 只有本轮**新产生**的留痕才算数（复用 SELL 侧同一判据，见 :516）
            _placed_buy = (_now_buy
                           if (_now_buy is not None and _now_buy is not _prev_buy)
                           else None)
            _block = str(getattr(engine, "_last_buy_block_reason", "") or "")

            if _placed_buy and _placed_buy.get("ok"):
                record(engine, done, oid, ST_SUBMIT,
                       "买入 %d 股已提交（委托号 %s，成交待回报）"
                       % (int(_placed_buy.get("qty") or qty),
                          _placed_buy.get("order_id") or "-"),
                       logger=logger, notify=notify)
                engine._owner_day_n = day_n = day_n + 1
                stats["submit"] += 1
                if logger:
                    logger.warning("[OWNER指令] %s 买入 %s %d 股 @%.3f（%s）",
                                   oid, code, int(_placed_buy.get("qty") or qty),
                                   price, note or "-")
                continue

            # 【2026-11-11 P0】买入失败不再记成成功。
            # 原实现无条件 `record(..., ST_SUBMIT, "已交落单口")`，而 _handle_buy
            # 恒返回 None、被任一闸门拦下也照样记 submit ⇒ 面板/账本显示"已交落单口"
            # 但账户一股没买（11-10 三条 BUY 全 status=submit，实际是否成交无法区分）。
            if _placed_buy:
                _why = "买入委托被券商/桥拒绝（ok=False）"
                record(engine, done, oid, ST_REJECT, _why,
                       logger=logger, notify=notify)
                stats["reject"] += 1
                if logger:
                    logger.warning("[OWNER指令] %s 买入被拒：%s", oid, _why)
                continue

            _why = ("落单口未下单：%s" % _block) if _block else (
                "落单口未下单（行情陈旧/熔断/现金不足/持仓上限等闸门拦截，"
                "详见引擎日志同刻行）")
            record(engine, done, oid, ST_REJECT, _why, logger=logger, notify=notify)
            stats["reject"] += 1
            if logger:
                logger.warning("[OWNER指令] %s 买入未下单：%s", oid, _why)
            continue

        # SELL：T+1 与保护期的判定交给 _handle_sell，这里先做一次显式预检，
        # 让「被 T+1 拦下」在账本里留下 reject 而不是含糊的 submit。
        # 【2026-11-10 P1】预检也改成「券商可用量优先」：live 下 `pos.available`
        # 来自券商 `m_nCanUseVolume`，已扣掉当日买入；用合并后的 `open_date` 判
        # 会在同票加仓后把老仓一起锁死（实测 510050 可卖 500 股却被判全锁）。
        pos = engine._positions.get(code)
        _avail = getattr(pos, "available", None) if pos is not None else None
        if pos is not None and _avail is not None:
            if int(_avail or 0) <= 0:
                record(engine, done, oid, ST_REJECT, "券商可用量为 0（当日买入不可卖）",
                       logger=logger, notify=notify)
                stats["reject"] += 1
                continue
            if qty > int(_avail or 0):
                record(engine, done, oid, ST_REJECT,
                       "卖出 %d 股超过券商可用 %d 股" % (qty, int(_avail or 0)),
                       logger=logger, notify=notify)
                stats["reject"] += 1
                continue
        elif t1_lock is not None and pos is not None:
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
        _placed = None
        if handle_sell and pos is not None:
            try:
                # lots=qty 是本次要卖的股数（2026-11-10 P0 修复）：
                # 落单口原签名没有这个入参，会把「卖 1 手」执行成「清仓可用量」。
                _prev_placed = getattr(engine, "_last_sell_placed", None)
                ok = bool(handle_sell(sig, pos, now, lots=qty))
                _now_placed = getattr(engine, "_last_sell_placed", None)
                # 只有本轮**新产生**的委托才算数（避免读到上一轮残留）
                if _now_placed is not None and _now_placed is not _prev_placed:
                    _placed = _now_placed
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
        elif _placed and _placed.get("ok"):
            # live 分支下单成功后**故意返回 False**（未成交不释放槽位），
            # 所以这里靠落单口回传的 _last_sell_placed 判定，而不是布尔返回值。
            record(engine, done, oid, ST_SUBMIT,
                   "卖出 %d 股已提交（委托号 %s，成交待回报）"
                   % (int(_placed.get("qty") or qty), _placed.get("order_id") or "-"),
                   logger=logger, notify=notify)
            engine._owner_day_n = day_n = day_n + 1
            stats["submit"] += 1
            if logger:
                logger.warning("[OWNER指令] %s 卖出 %s %d 股已提交 @%.3f（%s）",
                               oid, code, int(_placed.get("qty") or qty), price,
                               note or "-")
        else:
            # 【2026-11-11 P1】这句仍是兜底（落单口只回布尔、不带原因），但至少
            # 把闸门清单与排查入口写全；真正的「哪一闸」需要在
            # `engine/event_engine.py::_handle_sell` 的各个 return False 前留痕，
            # 属下一步（见 reports/OPEN_DECISIONS.md）。别再往里加新原因混进来。
            _why = ("落单口未成交（建仓保护期/T+1/行情陈旧/委托失败/零仓位，"
                    "详见 logs/quant_system.log 同刻行）")
            if _placed and not _placed.get("ok"):
                _why = "卖出委托被券商/桥拒绝（ok=False，详见 broker_bridge 日志）"
            record(engine, done, oid, ST_REJECT, _why, logger=logger, notify=notify)
            stats["reject"] += 1

    return stats

# -*- coding: utf-8 -*-
"""大 QMT 文件桥交易通道（替代 xttrader 的 ``QMTBroker``）

为什么需要它
------------
QMT 2.1.32.0（2026-11-07 自动升级）之后，外部 python 经 ``XtQuantTrader``
连接交易通道被服务端判 ``illegal pid`` 拒绝，提权到管理员仍被拒；而
``userdata_mini`` 下连 ``up_queue_xtquant`` 都没有（官方判据：无下单权限）。
两条路都要券商侧动作。

本模块把交易指令改成**文件桥**：本地写 ``signal.json``，QMT 内运行的
``qmt_bridge/qmt_executor.py`` 用 ``passorder`` 执行并写 ``result.json``。
这样下单动作发生在 QMT 策略主线程，不经过 xttrader，天然绕过白名单。

设计约束（每一条都是为了不重演 2026-11-07 那次「静默失真」）
------------------------------------------------------------
1. **接口与 ``QMTBroker`` 逐字段一致** ⇒ 引擎零改动（分流只发生在
   ``core/broker.py`` 的单例处）。
2. **``None`` 必须原样上抛**：查询失败（超时 / QMT 侧报错 / 心跳过期）
   一律返回 ``None``，绝不用 ``[]`` 或 0 伪装。这是 2026-11-07 修的那条
   P0（``_sync_broker_positions`` 把「查不到」读成「真空仓」会清空账本、
   让止损失去监控对象）。
3. **每次请求都带唯一 ``signal_id``**，QMT 侧据其幂等 ⇒ 本地超时重发不会
   造成重复下单。
4. **超时必判失败**，不无限等。默认 10s（心跳 1s，轮询 500ms）。

已知代价（OWNER 已确认接受）
----------------------------
- 秒级延迟：``passorder`` 异步，成交要靠 ``QUERY_TRADE`` 轮询确认。
- 回执弱：``place_order`` 返回 ``ok=True`` 只代表**已提交**，不代表已成交。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_BRIDGE_ROOT = r"C:\pazq_qmt_bridge"
DEFAULT_TIMEOUT = 10.0          # 单次请求等 result 的上限（秒）
HEARTBEAT_STALE_SEC = 6.0       # 心跳超过此值视为 QMT 侧已死
POLL_INTERVAL = 0.05            # 本地轮询 result 的间隔（秒）


def _atomic_write_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    # ★ QMT 侧读者可能正持有 signal.json 的句柄。若读者只申请了 FILE_SHARE_READ，
    # 这次 replace 会拿到 Windows ERROR_SHARING_VIOLATION（映射成 PermissionError），
    # 而 `_request` 会把它当成「写 signal 失败」直接 return None —— 若那是一条
    # SELL，就是一次静默漏卖。这里重试，写回去的是**同一个 signal_id**，桥接按
    # id 去重，不会重复下单，因此重试是安全的。
    # 2026-11-08 实测 8 次请求里 1 次触发；根因已在 qmtide_bridge 侧一并修掉
    # （ctypes 读改为 FILE_SHARE_READ|WRITE|DELETE），此处是纵深防御。
    last = None
    for i in range(8):
        try:
            os.replace(tmp, path)
            return
        except OSError as e:
            last = e
            time.sleep(0.02 * (i + 1))
    raise last


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


class BridgeBroker:
    """与 ``QMTBroker`` 同接口的文件桥实现。"""

    def __init__(self, cfg: Optional[dict] = None,
                 root: Optional[str] = None,
                 timeout: float = DEFAULT_TIMEOUT):
        if cfg is None:
            # 延迟导入，避免与 core.broker 形成循环导入
            try:
                from core.broker import _load_config
                cfg = _load_config()
            except Exception:
                cfg = {}
        self._cfg = cfg or {}
        self._root = root or self._cfg.get("bridge_root") or DEFAULT_BRIDGE_ROOT
        self._timeout = float(timeout)
        self._signal_path = os.path.join(self._root, "signal.json")
        self._result_path = os.path.join(self._root, "result.json")
        self._hb_path = os.path.join(self._root, "heartbeat.json")

        self._lock = threading.RLock()
        self._connected = False
        self._on_disconnect = None
        self._on_reconnected = None
        self._stats = {"requests": 0, "ok": 0, "timeout": 0, "error": 0}

    # ------------------------------------------------------------------
    # 属性 / 回调（与 QMTBroker 对齐）
    # ------------------------------------------------------------------
    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def mode(self) -> str:
        return "bridge" if self._connected else "disconnected"

    def set_on_disconnect(self, cb) -> None:
        self._on_disconnect = cb

    def set_on_reconnected(self, cb) -> None:
        self._on_reconnected = cb

    def _mark_disconnected(self) -> None:
        with self._lock:
            was = self._connected
            self._connected = False
        if was and self._on_disconnect:
            try:
                self._on_disconnect()
            except Exception:
                logger.exception("on_disconnect 回调异常")

    def disconnect(self) -> None:
        with self._lock:
            self._connected = False

    # ------------------------------------------------------------------
    # 连接 = 「目录可写 + QMT 侧心跳新鲜」
    # ------------------------------------------------------------------
    def _heartbeat_age(self) -> Optional[float]:
        # 读一次瞬时失败（正撞上 QMT 侧重写文件）不代表断线：重试一次再判。
        hb = _read_json(self._hb_path)
        if not isinstance(hb, dict):
            time.sleep(0.01)
            hb = _read_json(self._hb_path)
        if not isinstance(hb, dict):
            return None
        try:
            ts = float(hb.get("ts") or 0)
        except Exception:
            return None
        if ts <= 0:
            return None
        # QMT 与本机同时区，直接用本机时间比
        age = time.time() - ts
        # 时钟回拨时 age 可能是大负数，按「不新鲜」处理更安全
        if age < -60:
            return None
        return age

    def connect(self, force: bool = False) -> bool:
        with self._lock:
            if self._connected and not force:
                # 已连也要确认心跳没凉，避免拿着死通道当活着用
                age = self._heartbeat_age()
                if age is not None and age <= HEARTBEAT_STALE_SEC:
                    return True
                logger.warning("BridgeBroker 心跳过期（age=%s），判为未连接", age)
                self._connected = False
                return False

            if not os.path.isdir(self._root):
                logger.warning("BridgeBroker 目录不存在: %s", self._root)
                self._connected = False
                return False

            age = self._heartbeat_age()
            if age is None:
                logger.warning(
                    "BridgeBroker 未就绪：%s 不存在或不可解析"
                    "（QMT 侧策略是否已启动？）", self._hb_path)
                self._connected = False
                return False
            if age > HEARTBEAT_STALE_SEC:
                logger.warning(
                    "BridgeBroker 未就绪：心跳已 %.1fs 未更新（阈值 %.0fs）",
                    age, HEARTBEAT_STALE_SEC)
                self._connected = False
                return False

            was = self._connected
            self._connected = True
            logger.info("BridgeBroker 已连接（root=%s, heartbeat_age=%.1fs）",
                        self._root, age)
            if not was and self._on_reconnected:
                try:
                    self._on_reconnected()
                except Exception:
                    logger.exception("on_reconnected 回调异常")
            return True

    # ------------------------------------------------------------------
    # 请求-响应
    # ------------------------------------------------------------------
    def _request(self, action: str, account: str = "cash",
                 params: Optional[dict] = None) -> Optional[dict]:
        """发一条指令并等 result。超时/失败返回 None（调用方一律按失败上抛）。"""
        if not self.connect():
            return None
        if not str(action).upper() == "PING" and self._heartbeat_age() is None:
            return None

        sid = uuid.uuid4().hex
        payload = {
            "signal_id": sid,
            "action": str(action).upper(),
            "account": account,
            "params": params or {},
            "ts": time.time(),
        }
        deadline = time.time() + self._timeout
        with self._lock:
            self._stats["requests"] += 1
            try:
                _atomic_write_json(self._signal_path, payload)
            except Exception as e:
                logger.warning("写 signal 失败: %s", e)
                self._stats["error"] += 1
                return None

            while time.time() < deadline:
                res = _read_json(self._result_path)
                if isinstance(res, dict) and str(res.get("signal_id")) == sid:
                    if str(res.get("status")) == "ok":
                        self._stats["ok"] += 1
                        return res.get("data")
                    self._stats["error"] += 1
                    logger.warning("桥接执行失败 action=%s: %s", action,
                                   res.get("message"))
                    return None
                time.sleep(POLL_INTERVAL)

            self._stats["timeout"] += 1
            logger.warning("桥接超时 action=%s（%.1fs 无 result）", action, self._timeout)
        # 超时视为通道异常，标记断开并触发上层回调（引擎据此安排重连）
        self._mark_disconnected()
        return None

    # ------------------------------------------------------------------
    # 查询（失败必须 None，不得伪装成空）
    # ------------------------------------------------------------------
    def get_asset(self, account: str = "cash") -> Optional[dict]:
        data = self._request("QUERY_ASSET", account)
        if not isinstance(data, dict):
            return None
        try:
            return {
                "cash": float(data.get("cash") or 0),
                "frozen": float(data.get("frozen") or 0),
                "market_value": float(data.get("market_value") or 0),
                "total_asset": float(data.get("total_asset") or 0),
            }
        except Exception as e:
            logger.warning("get_asset 解析失败: %s (raw=%r)", e, data)
            return None

    def get_positions(self, account: str = "cash") -> Optional[List[dict]]:
        data = self._request("QUERY_POSITION", account)
        if not isinstance(data, list):
            return None
        out = []
        for p in data:
            if not isinstance(p, dict):
                continue
            out.append({
                "code": str(p.get("code") or ""),
                "quantity": int(p.get("quantity") or 0),
                "available": int(p.get("available") or 0),
                "avg_cost": float(p.get("avg_cost") or 0),
                "market_value": float(p.get("market_value") or 0),
            })
        return out

    def get_orders(self, account: str = "cash") -> Optional[List[dict]]:
        data = self._request("QUERY_ORDER", account)
        if not isinstance(data, list):
            return None
        out = []
        for o in data:
            if not isinstance(o, dict):
                continue
            out.append({
                "order_id": str(o.get("order_id") or ""),
                "code": str(o.get("code") or ""),
                "side": str(o.get("side") or ""),
                "quantity": int(o.get("quantity") or 0),
                "traded": int(o.get("traded") or 0),
                "price": float(o.get("price") or 0),
                "status": str(o.get("status") or ""),
            })
        return out

    def get_trades(self, account: str = "cash") -> Optional[List[dict]]:
        data = self._request("QUERY_TRADE", account)
        if not isinstance(data, list):
            return None
        out = []
        for t in data:
            if not isinstance(t, dict):
                continue
            out.append({
                "order_id": str(t.get("order_id") or ""),
                "code": str(t.get("code") or ""),
                "traded_price": float(t.get("traded_price") or 0),
                "traded_volume": int(t.get("traded_volume") or 0),
                "traded_time": str(t.get("traded_time") or ""),
            })
        return out

    # ------------------------------------------------------------------
    # 交易
    # ------------------------------------------------------------------
    # 【2026-11-08 安全】``passorder`` 没有返回值，委托号只能事后反查。
    # 反查预算：柜台受理通常 <300ms；给到 3s 是为了扛住集合竞价前后的排队。
    ORDER_ATTR_TIMEOUT_SEC = 3.0
    ORDER_ATTR_POLL_SEC = 0.15

    def _order_id_set(self, account: str) -> Optional[set]:
        """当前 ORDER 列表的委托号集合。取不到返回 ``None``（不可用作基线）。"""
        orders = self.get_orders(account)
        if orders is None:
            return None
        return {o["order_id"] for o in orders if o.get("order_id")}

    def _attribute_order_id(self, code: str, side: str, quantity: int,
                            account: str, before: set) -> Optional[str]:
        """下单后从 ORDER 列表反查「刚发的这一笔」的委托号。

        ★ 为什么这是资金安全级：
        引擎拿到 ``ok=True`` 但 ``order_id=None`` 时**不会**调用
        ``_register_pending_order``（``event_engine.py:3572/3748``）⇒ 卖出侧
        ``pos.quantity`` 不扣减、槽位不释放、``_mark_sold_today`` 不置位
        ⇒ **下一轮会对同一批股票再卖一次**。真实资金下 100 股可能卖成 200 股。

        ★ 为什么按"打分"而不是严格过滤：
        ``m_nOffsetFlag`` 到底是不是 23/24 取决于 QMT 版本（DUMP 才能确认），
        若方向映射判错，严格按 side 过滤会让反查**恒失败**。所以只强制匹配
        代码，方向与数量作为加分项 —— 宁可取到一个同代码的新委托号，
        也胜过返回 None 让引擎重复下单。
        """
        deadline = time.time() + self.ORDER_ATTR_TIMEOUT_SEC
        code_u = str(code).upper()
        while time.time() < deadline:
            orders = self.get_orders(account)
            if orders:
                best, best_score = None, -1
                for o in orders:
                    oid = str(o.get("order_id") or "")
                    if not oid or oid in before:
                        continue                       # 不是这一笔
                    if str(o.get("code") or "").upper() != code_u:
                        continue
                    score = 0
                    if str(o.get("side") or "").upper() == side:
                        score += 2
                    if int(o.get("quantity") or 0) == int(quantity):
                        score += 1
                    if score >= best_score:            # 同分取更靠后（更新的）那条
                        best, best_score = o, score
                if best is not None:
                    logger.info("[BRIDGE] 委托号反查成功 %s %s x%s → id=%s (score=%s)",
                                side, code, quantity, best["order_id"], best_score)
                    return str(best["order_id"])
            time.sleep(self.ORDER_ATTR_POLL_SEC)
        logger.error(
            "[BRIDGE] %s %s x%s 已提交，但 %.1fs 内未在 ORDER 列表中找到委托号。"
            "引擎将无法跟踪这笔成交（下一轮可能重复下单），请立即核对券商持仓。",
            side, code, quantity, self.ORDER_ATTR_TIMEOUT_SEC)
        return None

    def place_order(self, code: str, side: str, quantity: int,
                    price: float = 0.0, account: str = "cash") -> Dict:
        """返回 {"ok", "order_id", "reason"}。``ok=True`` 仅代表**已提交**。

        桥接不下市价单：``price <= 0`` 直接拒绝。原因是 ``passorder`` 的价格
        类型要显式指定，猜错会被柜台判废单，而废单在桥接模式下只能靠
        ``QUERY_ORDER`` 事后发现 —— 宁可不发。
        """
        if float(price) <= 0:
            return {"ok": False, "order_id": None,
                    "reason": "bridge 不接受 price<=0（必须给指定价）"}
        side_u = str(side).upper()
        if side_u not in ("BUY", "SELL"):
            return {"ok": False, "order_id": None, "reason": f"invalid side: {side}"}
        if int(quantity) <= 0:
            return {"ok": False, "order_id": None,
                    "reason": f"invalid quantity: {quantity}"}

        # 下单前先记基线：反查必须靠"差集"才能确定哪一笔是刚刚这笔。
        # 代价是每笔委托多一次 QUERY_ORDER（~50ms），而委托一天只有几笔，
        # 换来的是「绝不因为拿不到委托号而重复下单」。
        before = self._order_id_set(account)

        data = self._request(side_u, account, {
            "code": code,
            "volume": int(quantity),
            "price": float(price),
        })
        if not isinstance(data, dict):
            return {"ok": False, "order_id": None,
                    "reason": "bridge timeout/error（详见 broker_bridge 日志）"}
        oid = data.get("order_id")
        if not oid and before is not None:
            # passorder 无返回值 ⇒ 反查。这是本方法唯一的安全兜底。
            oid = self._attribute_order_id(code, side_u, int(quantity),
                                           account, before)
        logger.info("[BRIDGE] 下单 %s %s %s x %s @ %s → id=%s",
                    account, side_u, code, quantity, price, oid)
        return {"ok": True, "order_id": (str(oid) if oid else None), "reason": ""}

    def cancel_order_stock(self, account: str, order_id: str) -> Dict:
        if not str(order_id).lstrip("-").isdigit():
            return {"ok": False, "reason": f"invalid order_id: {order_id!r}"}
        data = self._request("CANCEL", account, {"order_id": str(order_id)})
        if data is None:
            return {"ok": False, "reason": "bridge timeout/error"}
        return {"ok": True, "reason": ""}

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------
    def dump(self) -> Optional[dict]:
        """让 QMT 侧回传实际字段名，用于联调期校准。"""
        return self._request("DUMP")

    def stats(self) -> Dict:
        with self._lock:
            return dict(self._stats)

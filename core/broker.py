# -*- coding: utf-8 -*-
"""
miniQMT 交易客户端（XtQuantTrader 封装）

参考 qmtIDE-kimik3/core/account.py，但更轻量：
- 无自动重连线程（由上层 engine 控制节奏）
- 不缓存资产/持仓快照（每次实时查）
- mode: "xttrader" / "disconnected"
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from config.settings import QMT_USERDATA_PATH, TRADING_CONFIG_FILE

logger = logging.getLogger(__name__)


_DEFAULT_CONFIG = {
    "userdata_path": QMT_USERDATA_PATH,
    "session_id": None,
    "broker_qmt_mode": "XtMiniQmt",
    "accounts": {"cash": "", "credit": ""},
    "auto_subscribe": True,
}


def _load_config() -> dict:
    if not TRADING_CONFIG_FILE.exists():
        return dict(_DEFAULT_CONFIG)
    try:
        with TRADING_CONFIG_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        merged = {**_DEFAULT_CONFIG, **data}
        merged["accounts"] = {**_DEFAULT_CONFIG["accounts"],
                              **data.get("accounts", {})}
        return merged
    except Exception as e:
        logger.warning("读取 %s 失败: %s", TRADING_CONFIG_FILE, e)
        return dict(_DEFAULT_CONFIG)


class _BrokerDisconnectCallback:
    """XtQuantTrader 断开回调（鸭式匹配 XtQuantTraderCallback 接口）。

    仅 on_disconnected 有实质逻辑：断线时把 broker 标记为未连，并触发上层
    注册的回调（引擎据此让 AutoReconnector 重连）。其余回调均为 no-op，
    避免扩展层调用未实现方法时抛 AttributeError。
    """

    def __init__(self, broker: "QMTBroker"):
        self._broker = broker

    def on_connected(self):
        pass

    def on_disconnected(self):
        logger.warning("XtQuantTrader 断开(on_disconnected 回调触发)")
        self._broker._mark_disconnected()

    def on_account_status(self, status):
        pass

    def on_stock_asset(self, asset):
        pass

    def on_stock_order(self, order):
        pass

    def on_stock_trade(self, trade):
        pass

    def on_stock_position(self, position):
        pass

    def on_order_error(self, order_error):
        """委托被拒回调。

        【2026-10-30 修复】原实现是 ``pass``，于是 ``order_stock`` 只返回 -1，
        而**真正的原因（errmsg）被这里吞掉**。实盘首日 523 笔全败，日志里只有
        ``order_stock rc=-1``，必须去翻 miniQMT 自己的 log 才能看到
        ``invalid price type [0]`` —— 排查成本被凭空放大。
        现把 order_id / error_id / error_msg 原样落日志，下次同类问题一眼可见。

        回滚：把本方法体换回 ``pass``。
        """
        try:
            logger.error(
                "[BROKER] 委托被拒 order_id=%s error_id=%s msg=%s",
                getattr(order_error, "order_id", "?"),
                getattr(order_error, "error_id", "?"),
                getattr(order_error, "error_msg", ""),
            )
        except Exception:
            logger.error("[BROKER] 委托被拒（回调解析失败）: %r", order_error)

    def on_cancel_error(self, cancel_error):
        pass


class QMTBroker:
    def __init__(self):
        self._lock = threading.RLock()
        self._trader = None
        self._connected = False
        self._cfg = _load_config()
        self._subscribed: Dict[str, object] = {}
        self._on_disconnect = None
        self._on_reconnected = None
        self._disconnect_cb_registered = False

    # ---------- 连接 ----------

    def connect(self, force: bool = False) -> bool:
        # 1) 已连且非强制 → 直接返回（幂等，避免自伤重连）
        with self._lock:
            if self._connected and not force:
                return True
        # 2) 确保 trader 实例存在（仅创建一次；正常重连复用同一对象/同 session，
        #    否则 force 重建会让旧 listener 仍占着 session → XtQuantTrader.connect rc=-1）
        created = False
        with self._lock:
            if self._trader is None:
                try:
                    from xtquant.xttrader import XtQuantTrader
                    sess = self._cfg.get("session_id")
                    if not sess:
                        sess = int(time.time()) % 1_000_000
                        self._cfg["session_id"] = sess
                    t = XtQuantTrader(self._cfg["userdata_path"], int(sess))
                    t.start()
                    self._trader = t
                    created = True
                except Exception as e:
                    logger.warning("XtQuantTrader 创建失败: %s", e)
                    self._connected = False
                    return False
        # 3) 注册断开回调（锁外，避免 on_disconnected 回调重入死锁）
        if created:
            self._register_disconnect_cb(self._trader)
        # 4) 真正 connect（锁外，避免阻塞期间回调重入死锁）
        with self._lock:
            was_connected = self._connected
            self._connected = False
        try:
            rc = self._trader.connect()
        except Exception as e:
            logger.warning("XtQuantTrader.connect 异常: %s", e)
            with self._lock:
                self._connected = False
            return False
        with self._lock:
            if rc == 0 or rc == -1:
                self._connected = True
                if rc == 0:
                    logger.info("XtQuantTrader 已连接 (path=%s, session=%s)",
                                self._cfg["userdata_path"], self._cfg["session_id"])
                else:
                    logger.info("XtQuantTrader.connect rc=-1（已连接/重复 connect，按已连处理）"
                                " (path=%s, session=%s)",
                                self._cfg["userdata_path"], self._cfg["session_id"])
                # 【2026-09-16 优化】交易连接（重）建立后，通知上层恢复行情订阅与持仓同步。
                # 行情(xtdata)与交易(XtQuantTrader)是两个独立连接：交易端断连恢复不会
                # 自动重建行情端订阅，易导致「断连 27 次」后数据/下单路径不同步。
                # 仅当本次是从「未连」状态恢复（非幂等重复 connect）时才回调，避免每轮刷。
                if not was_connected and self._on_reconnected:
                    try:
                        self._on_reconnected()
                    except Exception as e:
                        logger.debug("on_reconnected 回调异常(忽略): %s", e)
                return True
            logger.warning("XtQuantTrader.connect rc=%s", rc)
            self._connected = False
            return False

    def _teardown(self):
        # 仅用于 disconnect()；正常重连不再销毁实例，避免 session 冲突。
        self._trader = None
        self._connected = False
        self._subscribed.clear()
        self._disconnect_cb_registered = False

    def _mark_disconnected(self) -> None:
        """断线回调：标记未连并触发上层回调（引擎据此让 AutoReconnector 重连）。"""
        with self._lock:
            self._connected = False
        if self._on_disconnect:
            try:
                self._on_disconnect()
            except Exception:
                pass

    def set_on_disconnect(self, cb) -> None:
        self._on_disconnect = cb

    def set_on_reconnected(self, cb) -> None:
        """交易连接（重）建立后回调：上层据此恢复行情订阅 + 持仓同步。"""
        self._on_reconnected = cb

    def disconnect(self) -> None:
        with self._lock:
            self._connected = False
            t = self._trader
            self._trader = None
            self._disconnect_cb_registered = False
            if t is not None:
                try:
                    t.stop()
                except Exception:
                    pass

    def _register_disconnect_cb(self, trader) -> None:
        if self._disconnect_cb_registered:
            return
        try:
            trader.register_callback(_BrokerDisconnectCallback(self))
            self._disconnect_cb_registered = True
        except Exception as e:
            logger.debug("注册 broker 断开回调失败(忽略): %s", e)

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def mode(self) -> str:
        return "xttrader" if self._connected else "disconnected"

    # ---------- 账户 ----------

    def _account_obj(self, account: str):
        """StockAccount 实例（懒构造，缓存）。

        本地 xtquant 250807 只有 xtquant.xttype.StockAccount，
        没有 CreditAccount。信用账户也用 StockAccount，但根据
        atype 参数区分 ("STOCK" vs "CREDIT")。
        """
        acc_id = (self._cfg.get("accounts") or {}).get(account)
        if not acc_id:
            return None
        key = f"{account}:{acc_id}"
        if key in self._subscribed:
            return self._subscribed[key]
        obj = None
        try:
            from xtquant.xttype import StockAccount
            atype = "CREDIT" if account == "credit" else "STOCK"
            # 不同版本 StockAccount 签名不同：
            #   StockAccount(acc_id)         # 老
            #   StockAccount(acc_id, atype)  # 新（参考 qmtIDE-kimik3）
            try:
                obj = StockAccount(acc_id, atype)
            except TypeError:
                obj = StockAccount(acc_id)
        except Exception as e:
            logger.warning("构造账户对象失败 (%s): %s", account, e)
            return None
        self._subscribed[key] = obj
        return obj

    @staticmethod
    def _round_to_tick(code: str, price: float) -> float:
        """把委托价夹到标的最小变动单位：ETF/LOF 0.001，股票 0.01。

        为什么需要：引擎侧的价来自 tick，浮点运算后常带噪声
        （实盘日志里出现过 ``560.3000000000001``、``548.5600000000001``）。
        QMT 对超出最小变动单位的价会判「价格非法」拒单，与本次
        ``invalid price type`` 同属「参数格式」类静默失败。零成本、无副作用。

        回滚：删除本方法并把 ``self._round_to_tick(code, price)`` 换回 ``price``。
        """
        try:
            head = str(code).split(".")[0]
            # 5xxxxx 沪市基金 / 1[5-6]xxxx 深市基金 / 58xxxx 科创板基金
            is_fund = head.startswith(("51", "50", "58", "15", "16", "18"))
            return round(float(price), 3 if is_fund else 2)
        except Exception:
            return price

    def place_order(self, code: str, side: str, quantity: int,
                    price: float = 0.0, account: str = "cash") -> Dict:
        """
        side: BUY / SELL；account: cash / credit
        返回 {"ok": bool, "order_id": str|None, "reason": str}
        """
        if not self.connect():
            return {"ok": False, "order_id": None, "reason": "broker not connected"}
        acc = self._account_obj(account)
        if acc is None:
            return {"ok": False, "order_id": None, "reason": f"no {account} account"}
        try:
            from xtquant.xtconstant import STOCK_BUY, STOCK_SELL, CREDIT_BUY, CREDIT_SELL
            if account == "credit":
                order_type = CREDIT_BUY if side == "BUY" else CREDIT_SELL
            else:
                order_type = STOCK_BUY if side == "BUY" else STOCK_SELL
            # ★【2026-10-30 P0 修复】限价单价格类型必须用 FIX_PRICE(=11)。
            #
            # 原实现写死字面量 ``0`` 并注释成「限价单（LIMIT_PRICE=0）」——但
            # xtquant **没有** ``LIMIT_PRICE`` 这个常量，``0`` 也不是任何合法价格类型。
            # miniQMT 收到后直接判非法并在本地拒单（不进交易所）：
            #   [orderservice] [quant] order failed, invalid price type [0]
            #   onOrderResponse ... orderid:-1, errmsg:invalid price type [0]
            # ⇒ ``order_stock`` 返回 -1，``place_order`` 报 ``order_stock rc=-1``。
            #
            # 实证：2026-10-30 是 live（10-29 15:05 切换）后第一个真正开盘日，
            # 全天下单 523 笔（300308 卖出重试 522 + 510050 探针买入 1）**0 成交**。
            # paper 模式从不调用本函数，所以这条路径此前从未被执行过 —— 典型的
            # 「实盘有、回测无」口径背离，不是参数问题。
            #
            # 价格同时夹到最小变动单位：股票 0.01 / ETF·LOF 0.001。实测 tick 价
            # 会带浮点噪声（如 560.3000000000001），券商侧可能判「价格非法」。
            #
            # 回滚：把 ``FIX_PRICE`` 改回字面量 ``0``（恢复旧的错误行为），
            # 或整段恢复为 ``LATEST_PRICE if price <= 0 else 0``。
            from xtquant.xtconstant import LATEST_PRICE, FIX_PRICE
            _px = self._round_to_tick(code, price) if price > 0 else 0
            order_id = self._trader.order_stock(
                acc, code, order_type, quantity,
                LATEST_PRICE if price <= 0 else FIX_PRICE, _px,
            )
            if isinstance(order_id, int) and order_id < 0:
                return {"ok": False, "order_id": None, "reason": f"order_stock rc={order_id}"}
            logger.info("[BROKER] 下单 %s %s %s x %s @ %s → id=%s",
                        account, side, code, quantity, price, order_id)
            return {"ok": True, "order_id": str(order_id), "reason": ""}
        except Exception as e:
            logger.exception("place_order 异常: %s", e)
            return {"ok": False, "order_id": None, "reason": str(e)}

    # ---------- 查询 ----------

    def get_positions(self, account: str = "cash") -> Optional[List[dict]]:
        """查询持仓。None 表示查询失败（未连接/账户缺失/异常）；[] 表示确认无持仓。"""
        if not self.connect():
            return None
        acc = self._account_obj(account)
        if acc is None:
            return None
        try:
            raw = self._trader.query_stock_positions(acc)
            out = []
            for p in raw or []:
                out.append({
                    "code": getattr(p, "stock_code", ""),
                    "quantity": int(getattr(p, "volume", 0) or 0),
                    "available": int(getattr(p, "can_use_volume", 0) or 0),
                    "avg_cost": float(getattr(p, "open_price", 0) or 0),
                    "market_value": float(getattr(p, "market_value", 0) or 0),
                })
            return out
        except Exception as e:
            logger.warning("query_stock_positions 失败: %s", e)
            return None

    def get_orders(self, account: str = "cash") -> Optional[List[dict]]:
        """查询当日委托。None 表示查询失败（未连接/账户缺失/异常）；[] 表示确认无委托。"""
        if not self.connect():
            return None
        acc = self._account_obj(account)
        if acc is None:
            return None
        try:
            raw = self._trader.query_stock_orders(acc)
            out = []
            for o in raw or []:
                out.append({
                    "order_id": str(getattr(o, "order_id", "")),
                    "code": getattr(o, "stock_code", ""),
                    "side": "BUY" if getattr(o, "order_type", 0) in (23, 24) else "SELL",
                    "quantity": int(getattr(o, "order_volume", 0) or 0),
                    "traded": int(getattr(o, "traded_volume", 0) or 0),
                    "price": float(getattr(o, "price", 0) or 0),
                    "status": str(getattr(o, "order_status", "")),
                })
            return out
        except Exception as e:
            logger.warning("query_stock_orders 失败: %s", e)
            return None

    def get_trades(self, account: str = "cash") -> Optional[List[dict]]:
        """查询当日成交。None 表示查询失败（未连接/账户缺失/异常）；[] 表示确认无成交。"""
        if not self.connect():
            return None
        acc = self._account_obj(account)
        if acc is None:
            return None
        try:
            raw = self._trader.query_stock_trades(acc)
            out = []
            for t in raw or []:
                out.append({
                    "order_id": str(getattr(t, "order_id", "")),
                    "code": getattr(t, "stock_code", ""),
                    "traded_price": float(getattr(t, "traded_price", 0) or 0),
                    "traded_volume": int(getattr(t, "traded_volume", 0) or 0),
                    "traded_time": str(getattr(t, "traded_time", "")),
                })
            return out
        except Exception as e:
            logger.warning("query_stock_trades 失败: %s", e)
            return None

    def cancel_order_stock(self, account: str, order_id: str) -> Dict:
        """撤单封装。返回 {"ok": bool, "reason": str}；order_id 非法或异常时 ok=False，不抛异常。"""
        try:
            if not str(order_id).lstrip("-").isdigit():
                return {"ok": False, "reason": f"invalid order_id: {order_id!r}"}
            if not self.connect():
                return {"ok": False, "reason": "broker not connected"}
            acc = self._account_obj(account)
            if acc is None:
                return {"ok": False, "reason": f"no {account} account"}
            rc = self._trader.cancel_order_stock(acc, int(order_id))
            if rc == 0:
                logger.info("[BROKER] 撤单 %s %s → rc=0", account, order_id)
                return {"ok": True, "reason": ""}
            logger.warning("[BROKER] 撤单失败 %s %s rc=%s", account, order_id, rc)
            return {"ok": False, "reason": f"cancel_order_stock rc={rc}"}
        except Exception as e:
            logger.warning("cancel_order_stock 异常: %s", e)
            return {"ok": False, "reason": str(e)}

    def get_asset(self, account: str = "cash") -> Optional[dict]:
        if not self.connect():
            return None
        acc = self._account_obj(account)
        if acc is None:
            return None
        try:
            a = self._trader.query_stock_asset(acc)
            return {
                "cash": float(getattr(a, "cash", 0) or 0),
                "frozen": float(getattr(a, "frozen_cash", 0) or 0),
                "market_value": float(getattr(a, "market_value", 0) or 0),
                "total_asset": float(getattr(a, "total_asset", 0) or 0),
            }
        except Exception as e:
            logger.warning("query_stock_asset 失败: %s", e)
            return None


# 模块级单例
qmt_broker = QMTBroker()
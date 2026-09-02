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
        pass

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
            # 限价单（LIMIT_PRICE=0）；market 用最新价
            from xtquant.xtconstant import LATEST_PRICE
            order_id = self._trader.order_stock(
                acc, code, order_type, quantity, LATEST_PRICE if price <= 0 else 0,
                price if price > 0 else 0,
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

    def get_positions(self, account: str = "cash") -> List[dict]:
        if not self.connect():
            return []
        acc = self._account_obj(account)
        if acc is None:
            return []
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
            return []

    def get_orders(self, account: str = "cash") -> List[dict]:
        if not self.connect():
            return []
        acc = self._account_obj(account)
        if acc is None:
            return []
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
            return []

    def get_trades(self, account: str = "cash") -> List[dict]:
        if not self.connect():
            return []
        acc = self._account_obj(account)
        if acc is None:
            return []
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
            return []

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
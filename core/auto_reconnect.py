# -*- coding: utf-8 -*-
"""
自动重连器（带指数退避）

包装任意 `connect() -> bool` 函数，断线后自动在后台重连。
- 状态：connected / reconnecting / failed
- 重连间隔：min(interval * 2^retries, max_interval)
- 最大连续失败：max_retries（之后持续退避尝试）
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class AutoReconnector:
    def __init__(self, name: str, connect_fn: Callable[[], bool],
                 interval: float = 3.0, max_interval: float = 60.0,
                 max_retries: int = 5):
        self.name = name
        self._connect_fn = connect_fn
        self._interval = interval
        self._max_interval = max_interval
        self._max_retries = max_retries
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._connected = False
        self._reconnect_count = 0
        self._last_error = ""

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def reconnect_count(self) -> int:
        return self._reconnect_count

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run,
                                           name=f"reconnect-{self.name}",
                                           daemon=True)
            self._thread.start()
            logger.info("AutoReconnector[%s] 启动", self.name)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._connected = False

    def notify_disconnect(self, reason: str = "") -> None:
        """外部通知断线（broker 回调）。"""
        with self._lock:
            if self._connected:
                logger.warning("AutoReconnector[%s] 断线: %s", self.name, reason)
            self._connected = False
        self.start()

    def _run(self) -> None:
        retries = 0
        while not self._stop.is_set():
            # 已连上：空闲等待，靠 notify_disconnect() 把 _connected 置 False 再重连。
            # 切勿周期性主动 reconnect——会把健康连接自身拆掉
            # （旧逻辑：connect(force=True) 每 interval 重建 trader 实例，与仍在跑的
            #  旧 listener 抢同一 session → XtQuantTrader.connect rc=-1，永久 disconnected）。
            with self._lock:
                connected = self._connected
            if connected:
                retries = 0
                self._stop.wait(self._interval)
                continue
            # 未连接：尝试 connect_fn
            try:
                ok = bool(self._connect_fn())
            except Exception as e:
                ok = False
                self._last_error = f"{type(e).__name__}: {e}"
                logger.debug("AutoReconnector[%s] 异常: %s", self.name, e)
            if ok:
                with self._lock:
                    if not self._connected:
                        logger.info("AutoReconnector[%s] 已恢复", self.name)
                    self._connected = True
                    self._reconnect_count += 1
                # 连上后回到循环顶：若仍 _connected 则空闲等待（不主动拆链）
                continue
            # 失败 → 退避重试
            retries += 1
            wait = min(self._max_interval,
                       self._interval * (2 ** min(retries, 6)))
            logger.info("AutoReconnector[%s] 第 %d 次重连失败，%ss 后重试",
                        self.name, retries, wait)
            self._stop.wait(wait)
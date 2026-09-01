# -*- coding: utf-8 -*-
"""
miniQMT 行情客户端（qmtIDE-deepseek 自实现版）

三层降级：
1. xtdata push 订阅（subscribe_quote）+ 快照（get_full_tick）
2. xtdata.get_market_data_ex('1m') 兜底拉最新一根 K 线
3. 内置 _MockClient 随机游走（miniQMT 未运行时）

参考 qmtIDE-kimik3/core/qmt_client.py 的三层模式，剥离无关逻辑（trade/score/preset）。
"""
from __future__ import annotations

import logging
import math
import random
import sys
import threading
import time
from datetime import datetime
from typing import Dict, Iterable, List, Optional

# 确保 Windows 控制台能输出中文（cp1252 → utf-8）
try:
    if hasattr(sys.stdout, "reconfigure") and sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure") and sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

logger = logging.getLogger(__name__)


# ============================================================ Mock 兜底

class _MockClient:
    mode = "mock"

    BASE_PRICES = {
        "000001.SH": 3400.0, "399001.SZ": 11000.0, "399006.SZ": 2200.0,
        "000300.SH": 4100.0,
        "300308.SZ": 130.0, "300502.SZ": 110.0, "300394.SZ": 90.0,
        "688256.SH": 600.0, "688981.SH": 85.0,
        "002371.SZ": 350.0, "603019.SH": 70.0,
        "000977.SZ": 45.0, "002230.SZ": 55.0,
        "688111.SH": 280.0, "002415.SZ": 32.0, "300033.SZ": 105.0,
    }

    def __init__(self):
        self._last_prices: Dict[str, float] = dict(self.BASE_PRICES)
        self._last_close: Dict[str, float] = dict(self.BASE_PRICES)

    def subscribe(self, codes: Iterable[str]) -> None:
        pass

    def get_ticks(self, codes: Iterable[str]) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        for code in codes:
            base = self.BASE_PRICES.get(code, 100.0)
            prev = self._last_prices.get(code, base)
            chg = random.uniform(-0.3, 0.3)
            price = round(prev * (1 + chg / 100), 3)
            self._last_prices[code] = price
            lc = self._last_close.setdefault(code, base)
            out[code] = {
                "lastPrice": price,
                "open": round(base * 0.99, 3),
                "high": max(round(base * 1.01, 3), price),
                "low": min(round(base * 0.98, 3), price),
                "lastClose": lc,
                "volume": random.randint(1_000_000, 80_000_000),
                "amount": random.randint(100_000_000, 8_000_000_000),
            }
        return out

    def get_history(self, code: str, period: str = "1d", count: int = 100):
        """生成合成的 K 线序列（仅供测试）。"""
        base = self.BASE_PRICES.get(code, 100.0)
        bars = []
        price = base
        for i in range(count):
            o = price
            c = o * (1 + random.uniform(-0.02, 0.02))
            h = max(o, c) * (1 + abs(random.uniform(0, 0.01)))
            lo = min(o, c) * (1 - abs(random.uniform(0, 0.01)))
            v = random.randint(500_000, 50_000_000)
            ts = datetime.now()
            bars.append({
                "ts": ts,
                "open": round(o, 3),
                "high": round(h, 3),
                "low": round(lo, 3),
                "close": round(c, 3),
                "volume": v,
                "amount": round(v * c, 2),
            })
            price = c
        return bars


# ============================================================ xtdata 真实客户端

class _XtdClient:
    mode = "xtdata"

    def __init__(self):
        from xtquant import xtdata
        self.xtdata = xtdata
        self.subscribed: set[str] = set()
        self._push_cache: Dict[str, dict] = {}
        self._lock = threading.RLock()
        # 单例 callback
        self._cb = self._make_callback()
        # 探活：分别捕获网络异常和数据异常
        try:
            test = xtdata.get_full_tick(["000001.SH"])
            if not (test and "000001.SH" in test):
                raise RuntimeError("get_full_tick 返回空")
        except UnicodeError:
            # 控制台编码问题不影响 xtdata 本体，重抛 RuntimeError 让 QMTClient 降级
            raise RuntimeError("console encoding blocked get_full_tick")
        except Exception as e:
            raise RuntimeError(f"miniQMT probe failed: {type(e).__name__}: {e}")

    def _make_callback(self):
        parent = self

        class _CB:
            def on_tick(self, data):
                try:
                    if not isinstance(data, dict):
                        return
                    with parent._lock:
                        for code, tick in data.items():
                            if not isinstance(tick, dict):
                                continue
                            parent._push_cache[code] = {
                                "lastPrice": float(tick.get("lastPrice", 0) or 0),
                                "open": float(tick.get("open", 0) or 0),
                                "high": float(tick.get("high", 0) or 0),
                                "low": float(tick.get("low", 0) or 0),
                                "lastClose": float(tick.get("lastClose", 0) or 0),
                                "volume": int(tick.get("volume", 0) or 0),
                                "amount": float(tick.get("amount", 0) or 0),
                                "_pushed_at": time.time(),
                            }
                except Exception as e:
                    logger.debug("push cb err: %s", e)
        return _CB()

    def subscribe(self, codes: Iterable[str]) -> None:
        new = [c for c in codes if c not in self.subscribed]
        if not new:
            return
        try:
            self.xtdata.subscribe_quote(new, period="tick", count=-1, callback=self._cb)
            self.subscribed.update(new)
        except Exception as e:
            logger.debug("subscribe_quote 部分失败（可忽略）: %s", e)
            self.subscribed.update(new)

    def _normalize(self, code: str, tick: dict) -> Optional[dict]:
        if not isinstance(tick, dict):
            return None
        lp = float(tick.get("lastPrice", 0) or 0)
        if lp <= 0:
            return None
        lc = float(tick.get("lastClose", 0) or 0) or lp
        return {
            "lastPrice": lp,
            "open": float(tick.get("open", 0) or lp),
            "high": float(tick.get("high", 0) or lp),
            "low": float(tick.get("low", 0) or lp),
            "lastClose": lc,
            "volume": int(tick.get("volume", 0) or 0),
            "amount": float(tick.get("amount", 0) or 0),
        }

    def get_ticks(self, codes: Iterable[str]) -> Dict[str, dict]:
        codes = list(codes)
        result: Dict[str, dict] = {}
        missing: List[str] = []

        with self._lock:
            for code in codes:
                p = self._push_cache.get(code)
                if p:
                    n = self._normalize(code, p)
                    if n:
                        result[code] = n
                    else:
                        missing.append(code)
                else:
                    missing.append(code)

        if missing:
            try:
                snap = self.xtdata.get_full_tick(missing)
                for code in missing:
                    tick = snap.get(code) if snap else None
                    if not tick:
                        continue
                    n = self._normalize(code, tick)
                    if n:
                        result[code] = n
            except Exception as e:
                logger.debug("get_full_tick 失败: %s", e)

        return result

    def get_history(self, code: str, period: str = "1d", count: int = 100) -> Optional[List[dict]]:
        try:
            # 转换周期名：xtdata 接受 "1d" / "5m" / "1m"
            xt_period = period if period != "D" else "1d"
            raw = self.xtdata.get_market_data_ex(
                field_list=["time", "open", "high", "low", "close", "volume", "amount"],
                stock_list=[code], period=xt_period, count=count,
            )
            if not raw or code not in raw:
                return None
            data = raw[code]
            if hasattr(data, "columns"):  # DataFrame
                data = {col: list(data[col]) for col in data.columns}
            if not isinstance(data, dict) or "time" not in data:
                return None
            ts_list = data["time"]
            n = len(ts_list)
            bars: List[dict] = []
            for i in range(n):
                t = ts_list[i]
                if isinstance(t, str):
                    try:
                        ts = datetime.strptime(t[:14], "%Y%m%d%H%M%S")
                    except Exception:
                        try:
                            ts = datetime.strptime(t[:8], "%Y%m%d")
                        except Exception:
                            ts = datetime.now()
                else:
                    try:
                        v = int(t)
                        ts = datetime.fromtimestamp(v / 1000 if v > 1e12 else v)
                    except Exception:
                        ts = datetime.now()
                bars.append({
                    "ts": ts,
                    "open": float(data.get("open", [0] * n)[i] or 0),
                    "high": float(data.get("high", [0] * n)[i] or 0),
                    "low": float(data.get("low", [0] * n)[i] or 0),
                    "close": float(data.get("close", [0] * n)[i] or 0),
                    "volume": int(data.get("volume", [0] * n)[i] or 0),
                    "amount": float(data.get("amount", [0] * n)[i] or 0),
                })
            return bars
        except Exception as e:
            logger.debug("get_history 失败: %s", e)
            return None


# ============================================================ 统一入口

class QMTClient:
    """对上层暴露的统一客户端。自动在 xtdata / mock 之间切换。"""

    def __init__(self):
        self._impl = None
        self._mode = "mock"
        try:
            self._impl = _XtdClient()
            self._mode = "xtdata"
            # 用 ASCII-safe 消息，避免 cp1252 编码失败
            logger.info("QMTClient: connected to miniQMT (xtdata)")
        except UnicodeError:
            # 控制台编码异常不算 xtdata 不可用，重试一次（先 ASCII 输出）
            try:
                self._impl = _XtdClient()
                self._mode = "xtdata"
                logger.info("QMTClient: connected to miniQMT (xtdata)")
            except Exception as e:
                logger.warning("QMTClient: xtdata unavailable, fallback to mock (%s: %s)",
                               type(e).__name__, e)
                self._impl = _MockClient()
                self._mode = "mock"
        except Exception as e:
            logger.warning("QMTClient: xtdata unavailable, fallback to mock (%s: %s)",
                           type(e).__name__, e)
            self._impl = _MockClient()
            self._mode = "mock"

    @property
    def mode(self) -> str:
        return self._mode

    def subscribe(self, codes: Iterable[str]) -> None:
        self._impl.subscribe(codes)

    def get_ticks(self, codes: Iterable[str]) -> Dict[str, dict]:
        return self._impl.get_ticks(codes)

    def get_history(self, code: str, period: str = "1d", count: int = 100):
        return self._impl.get_history(code, period, count)


# 模块级单例
qmt_client = QMTClient()
# -*- coding: utf-8 -*-
"""
核心数据模型。

统一使用 dataclass，便于在事件驱动引擎、策略、风控、存储之间传递。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Tick:
    """实时行情快照（已归一化）。"""
    ts: datetime
    code: str
    name: str = ""
    price: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    pre_close: float = 0.0
    volume: int = 0
    amount: float = 0.0
    change: float = 0.0
    change_pct: float = 0.0
    source: str = "unknown"       # "xtdata" / "mock"

    @property
    def is_valid(self) -> bool:
        return self.price > 0


@dataclass
class Bar:
    """K 线（日线 / 分钟线）。"""
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    amount: float = 0.0


@dataclass
class Signal:
    """策略信号。"""
    ts: datetime
    code: str
    name: str = ""
    side: str = "BUY"             # "BUY" / "SELL" / "HOLD"
    score: float = 0.0            # 多因子评分
    price: float = 0.0
    reason: str = ""              # 触发原因（人类可读）
    factors: dict = field(default_factory=dict)   # 各因子明细
    ai_comment: str = ""          # AI 分析补充（可选）
    ai_confidence: float = 0.0    # AI 置信度 -1~1


@dataclass
class Order:
    """下单请求。"""
    ts: datetime
    code: str
    side: str                     # BUY / SELL
    quantity: int
    price: float = 0.0
    order_type: str = "market"    # market / limit
    account: str = "cash"         # cash / credit


@dataclass
class Fill:
    """成交回报。"""
    ts: datetime
    code: str
    side: str
    quantity: int
    price: float
    amount: float
    account: str = "cash"


@dataclass
class Position:
    """持仓。"""
    code: str
    name: str = ""
    quantity: int = 0
    avg_cost: float = 0.0
    last_price: float = 0.0
    open_date: Optional[datetime] = None
    peak_price: float = 0.0      # 持仓以来最高价（移动止损用）
    stop_price: float = 0.0      # 入场时按 ATR 计算的保护止损价
    target_price: float = 0.0    # 入场时按 ATR 计算的目标止盈价

    @property
    def market_value(self) -> float:
        return self.quantity * self.last_price

    @property
    def cost_value(self) -> float:
        return self.quantity * self.avg_cost

    @property
    def pnl(self) -> float:
        return self.market_value - self.cost_value

    @property
    def pnl_pct(self) -> float:
        if self.cost_value <= 0:
            return 0.0
        return self.pnl / self.cost_value


@dataclass
class AIAnalysis:
    """AI 分析结果（结构化）。"""
    ts: datetime
    code: str
    model: str
    summary: str = ""             # 一句话结论
    stance: str = "neutral"       # bullish / bearish / neutral
    confidence: float = 0.0       # -1.0 ~ 1.0
    risks: str = ""
    raw: str = ""                 # 原始返回（调试）
    cached: bool = False

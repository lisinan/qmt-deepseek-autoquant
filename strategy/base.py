# -*- coding: utf-8 -*-
"""
策略抽象基类。

子类需实现：
  - on_bars(code, name, bars) -> Signal  评估入场
  - on_exit(code, position, current_price, bars) -> Optional[Signal]  评估离场
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from core.data_models import Bar, Position, Signal


class BaseStrategy(ABC):
    name: str = "base"

    @abstractmethod
    def on_bars(self, code: str, name: str, bars: List[Bar]) -> Signal:
        """每根 K 线触发；返回 HOLD 表示无操作。"""

    @abstractmethod
    def on_exit(self, code: str, position: Position,
                current_price: float, bars: List[Bar]) -> Optional[Signal]:
        """持仓评估；返回 SELL Signal 或 None。"""
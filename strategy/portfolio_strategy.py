# -*- coding: utf-8 -*-
"""
Portfolio 组合策略（qmtIDE-deepseek 自实现版）

逻辑：
  1. 对 UNIVERSE 所有 STOCK_CODES 跑 TrendStrategy.on_bars() 打分
  2. 取评分 >= threshold 的 Top-N（按 score 降序）
  3. 调仓：
     - 现有持仓但不在目标 → SELL
     - 目标但未持仓 → BUY（等权分配剩余资金，单标的不超 max_single_pct）
     - 已持仓且在目标 → HOLD
  4. 返回 Order 列表，EventEngine 执行

参数：PORTFOLIO_CONFIG（config/settings.py）
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from config.settings import (
    INDEX_CODES, PORTFOLIO_CONFIG, STOCK_CODES, UNIVERSE,
)
from core.data_models import Bar, Order, Position, Signal
from strategy.trend_strategy import TrendStrategy
from data.tushare_client import tushare_client

logger = logging.getLogger(__name__)


class PortfolioStrategy:
    name = "portfolio"

    def __init__(self, trend: Optional[TrendStrategy] = None,
                 max_positions: int = None,
                 score_threshold: float = None,
                 max_single_pct: float = None,
                 cash_buffer_pct: float = 0.05,
                 use_fundamental_filter: bool = True):
        self.trend = trend or TrendStrategy()
        self.max_positions = max_positions or PORTFOLIO_CONFIG["max_positions"]
        self.score_threshold = (score_threshold
                                or PORTFOLIO_CONFIG["score_threshold"])
        self.max_single_pct = (max_single_pct
                               or PORTFOLIO_CONFIG["max_single_pct"])
        # 保留 5% 现金避免满仓
        self.cash_buffer_pct = cash_buffer_pct
        # 基本面过滤（可选）
        self.use_fundamental_filter = use_fundamental_filter

    # ============================================================ 选股

    def select(self,
               codes_to_bars: Dict[str, Tuple[str, List[Bar]]],
               exclude: Optional[set] = None
               ) -> List[Signal]:
        """
        对每只股票打分，返回 Top-N BUY Signal 列表（按 score 降序）。
        exclude: 不参与评分的代码集合（如指数）
        启用基本面过滤时，PE/PB/市值/ROE 不达标的会被剔除。
        """
        exclude = exclude or set()
        scored: List[Signal] = []
        fundamental_filtered: List[str] = []   # 被基本面过滤掉的（用于调试）
        for code, (name, bars) in codes_to_bars.items():
            if code in exclude or code in INDEX_CODES:
                continue
            if len(bars) < 60:
                continue
            sig = self.trend.on_bars(code, name, bars)
            if sig.side != "BUY":
                continue
            if sig.score < self.score_threshold:
                continue
            # 基本面过滤
            if self.use_fundamental_filter and tushare_client.enabled:
                if not tushare_client.passes_filter(code):
                    fundamental_filtered.append(code)
                    continue
            scored.append(sig)
        scored.sort(key=lambda s: (s.score, s.ts.timestamp()), reverse=True)
        if fundamental_filtered:
            logger.info("Portfolio: 基本面过滤剔除 %d 只: %s",
                        len(fundamental_filtered),
                        ",".join(fundamental_filtered[:10]))
        return scored[:self.max_positions]

    # ============================================================ 调仓

    def plan_rebalance(self,
                       targets: List[Signal],
                       positions: Dict[str, Position],
                       current_prices: Dict[str, float],
                       cash: float,
                       total_asset: float
                       ) -> List[Order]:
        """
        根据目标 vs 当前持仓，生成调仓订单列表。

        返回订单顺序：先 SELL（释放资金）后 BUY（用新释放的资金）。
        """
        orders: List[Order] = []
        target_codes = {s.code for s in targets}

        # ---- 1) 平掉不在目标的持仓
        for code, pos in positions.items():
            if pos.quantity <= 0:
                continue
            if code not in target_codes:
                price = current_prices.get(code, pos.last_price) or pos.last_price
                orders.append(Order(
                    ts=datetime.now(), code=code, side="SELL",
                    quantity=pos.quantity, price=price,
                    order_type="limit", account="cash",
                ))
                logger.info("Portfolio: 平 %s x %s (不在 Top-%d)",
                            code, pos.quantity, len(targets))

        # ---- 2) 给新目标分配资金
        held_target_codes = {c for c in target_codes
                             if c in positions and positions[c].quantity > 0}
        new_targets = [s for s in targets if s.code not in held_target_codes]
        if not new_targets:
            return orders

        # 可用资金 = 总现金 - 现金缓冲 - 已持仓占用市值（保守估计）
        available_cash = max(0.0,
                             total_asset * (1 - self.cash_buffer_pct)
                             - sum(p.market_value for p in positions.values()
                                   if p.quantity > 0))
        if available_cash <= 0:
            # 每轮都可能命中（满仓时是常态），放 INFO 会刷爆日志 → DEBUG
            logger.debug("Portfolio: 无可用资金，跳过建仓")
            return orders

        # 等权分配给 new_targets（每个不超过 max_single_pct * total_asset）
        slot_cash = min(available_cash / len(new_targets),
                        total_asset * self.max_single_pct)
        for sig in new_targets:
            price = current_prices.get(sig.code, sig.price) or sig.price
            if price <= 0:
                continue
            raw_qty = slot_cash / price
            qty = max(100, int(raw_qty // 100) * 100)   # 整百股
            if qty <= 0:
                continue
            orders.append(Order(
                ts=datetime.now(), code=sig.code, side="BUY",
                quantity=qty, price=price,
                order_type="limit", account="cash",
            ))
            # 这里只是「计划」，订单还要过风控（daily_trades/额度/regime）。
            # 计划每轮都会重算，放 INFO 会把同一条被拒的计划反复刷进日志
            # （实测 1.02GB 日志里最高频的两行就是「建 xxx」+「BUY 拒绝」）。
            # 真正成交由 engine 的 fill 日志记 INFO，这里降为 DEBUG。
            logger.debug("Portfolio: 建 %s x %s @ %s (score=%.2f)",
                         sig.code, qty, price, sig.score)
        return orders

    # ============================================================ 持仓评估（复用 TrendStrategy）

    def evaluate_exit(self, code: str, name: str, position: Position,
                      current_price: float, bars: List[Bar]
                      ) -> Optional[Signal]:
        return self.trend.on_exit(code, position, current_price, bars)
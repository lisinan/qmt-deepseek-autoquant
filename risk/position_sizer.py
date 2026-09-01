# -*- coding: utf-8 -*-
"""
波动率目标仓位（Volatility-Target Position Sizing）

替换原来"每只股票无脑 5 万"的固定仓位。核心思想：让每笔交易承担
的*金额风险*大致相等 —— 高波动标的少买、低波动标的多买，从而：

  - 降低单笔被打止损的破坏性（不再因波动率不同而盈亏忽大忽小）
  - 提升收益/风险比（Sharpe），回撤更平滑

公式：
  risk_budget = equity * risk_per_trade          # 单笔允许亏损的金额
  stop_pct    = max(fixed_stop, atr_pct * atr_stop_mult)   # 止损距离（小数）
  target_value = risk_budget / stop_pct           # 目标市值
  target_value = min(target_value, equity*max_pct, max_order_amount)
  qty = floor(target_value / price / 100) * 100
"""
from __future__ import annotations

from config.settings import RISK_PARAMS, STRATEGY_PARAMS


class PositionSizer:
    def __init__(self, params: dict = None):
        p = dict(STRATEGY_PARAMS)
        if params:
            p.update(params)
        self.p = p
        # 单笔风险比例（占总资产）；回测/调参时覆盖
        self.risk_per_trade = float(p.get("risk_per_trade", 0.01))
        self.atr_stop_mult = float(p.get("atr_stop_mult", 2.0))
        self.max_pct = float(p.get("max_single_position_pct",
                                   RISK_PARAMS["max_single_position_pct"]))
        self.max_order_amount = float(p.get("max_order_amount",
                                            RISK_PARAMS["max_order_amount"]))

    def stop_pct(self, fixed_stop: float, atr_pct: float) -> float:
        """止损距离（小数，正数）。优先用 ATR，保底用固定止损。"""
        atr_stop = atr_pct * self.atr_stop_mult
        # 取两者中较大的，保证止损足够远、不被噪音触发
        return max(abs(fixed_stop), atr_stop)

    def size(self, price: float, equity: float, atr_pct: float,
             fixed_stop: float = None, stop_pct: float = None) -> int:
        """返回应买入的股数（整百）。价格/权益无效返回 0。

        :param stop_pct: 显式指定的真实止损距离（正小数，如 0.18）。传入时
            **直接采用**，不再走 ``max(|fixed_stop|, atr%×atr_stop_mult)`` 推导。
            存在的原因：trend 退出范式的真实止损是 max(18%, atr%×6)，而推导
            出来的是 max(4%, atr%×2.5)——相差 ~2.7 倍，使名义 risk_per_trade
            与实际单笔风险不一致。调用方需要「名实相符」时用本参数。
        """
        if price <= 0 or equity <= 0:
            return 0
        if stop_pct is not None:
            sp = abs(float(stop_pct))
        else:
            if fixed_stop is None:
                fixed_stop = self.p.get("stop_loss", -0.03)
            sp = self.stop_pct(fixed_stop, atr_pct)
        if sp <= 0:
            return 0
        risk_budget = equity * self.risk_per_trade
        target_value = risk_budget / sp
        target_value = min(target_value,
                           equity * self.max_pct,
                           self.max_order_amount)
        if target_value <= 0:
            return 0
        qty = int(target_value // (price * 100)) * 100
        return max(0, qty)

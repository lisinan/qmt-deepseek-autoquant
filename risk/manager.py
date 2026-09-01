# -*- coding: utf-8 -*-
"""
风控管理器

熔断规则（参数在 RISK_PARAMS）：
- 日内累计亏损 <= daily_loss_limit_pct × total_asset → halt
- 日内累计亏损 <= -daily_loss_limit_abs → halt
- 连续亏损 >= max_consecutive_losses → 降仓（position_scale 1.0→0.8→0.6→0.4→0.0）
- 连续亏损 >= max_consecutive_losses_halt → halt
- 调用方通过 on_asset_update(asset) 注入总资产，触发 max_drawdown_pct 后 halt
- 全部 halt 均为「可恢复断路器」：冷却 N 个自然日后自动解除并重置风险基线
  （max_drawdown 用 dd_recover_days；consec_loss / daily_loss_abs 用 halt_recover_days），
  避免任何一类熔断成为「永久杀死开关」导致实盘停止开仓、收益蜕变为 ~0%。
"""
from __future__ import annotations

import logging
import threading
from datetime import date
from typing import Dict, Optional, Tuple

from config.settings import RISK_PARAMS
from core.data_models import Fill, Order, Position
from core.notices import system_notice

logger = logging.getLogger(__name__)


_SCALE_LADDER = [1.0, 0.8, 0.6, 0.4, 0.0]


class RiskManager:
    def __init__(self, params: Optional[dict] = None):
        self.p = dict(RISK_PARAMS)
        if params:
            self.p.update(params)
        # 保护后台线程（异步成交回报）与主循环之间的状态更新，避免竞态。
        self._lock = threading.Lock()
        self._today: date = date.today()
        self._daily_pnl: float = 0.0
        self._consec_loss: int = 0
        self._halted: bool = False
        self._halt_reason: str = ""
        self._peak_asset: float = 0.0
        self._daily_trade_count: int = 0
        self._halt_day: date = date.today()   # 回撤熔断触发日（用于冷却自动恢复）

    # ---------- 每日重置 ----------

    def reset_daily(self) -> None:
        today = date.today()
        if today != self._today:
            self._today = today
            self._daily_pnl = 0.0
            self._daily_trade_count = 0
            # 日切不重置连续亏损与 peak_asset

    # ---------- 资金 ----------

    def on_asset_update(self, total_asset: float) -> None:
        if total_asset <= 0:
            return
        today = date.today()
        with self._lock:
            # 全部 halt 的「冷却自动恢复」（2026-08-30 统一断路器修正）：
            # 原实现仅 max_drawdown 类可恢复；consec_loss / daily_loss_abs 类在 _halt()
            # 后**永久停牌**——本趋势策略难免连亏，会触发 consec_loss=5 后停止开仓，
            # 使回测收益在实盘蜕变为 ~0%（2026-08-25 模拟盘已触发 consec_loss=5 佐证）。
            # 现在：任何 halt 冷却 N 个自然日后自动解除并重置风险基线
            # （max_drawdown 用 dd_recover_days；其余用 halt_recover_days），成为真「断路器」。
            self._maybe_recover(today, total_asset)
            if total_asset > self._peak_asset:
                self._peak_asset = total_asset
                return
            dd = (total_asset - self._peak_asset) / self._peak_asset
            if dd <= self.p["max_drawdown_pct"]:
                self._halt(reason=f"max_drawdown {dd*100:.2f}%")
                return

    # ---------- 持仓 ----------

    def account_block_reason(self, daily_trade_count: int) -> str:
        """账户级硬阻断原因（与具体订单无关）。无阻断返回 ""。

        为什么单独拆出来：``can_open`` 需要一个完整 Order（含数量/价格），而
        构造 Order 前要先算日线 ATR + 波动率目标仓位，是每轮最贵的一段。
        当账户已经熔断或打满日内交易次数时，这些计算 100% 是白做的。
        实盘日志里出现过 63 万条 "BUY 拒绝 daily_trades>10"，即每轮都完整
        算一遍仓位再被同一个理由拒掉。调用方应先查这里再决定是否继续。
        """
        self.reset_daily()
        self._maybe_recover(date.today())
        if self._halted:
            return f"halted:{self._halt_reason}"
        if daily_trade_count >= self.p["max_daily_trades"]:
            return f"daily_trades>{self.p['max_daily_trades']}"
        return ""

    def can_open(self, order: Order, positions: Dict[str, Position],
                 total_asset: float, daily_trade_count: int) -> Tuple[bool, str]:
        self.reset_daily()
        self._maybe_recover(date.today())
        if self._halted:
            return False, f"halted:{self._halt_reason}"
        # 单笔金额
        amount = order.quantity * order.price if order.price > 0 else 0
        if amount > self.p["max_order_amount"]:
            return False, f"amount>{self.p['max_order_amount']}"
        # 单标的上限
        if total_asset > 0 and amount > 0:
            ratio = amount / total_asset
            if ratio > self.p["max_single_position_pct"]:
                return False, f"pos_ratio>{self.p['max_single_position_pct']}"
        # 日内次数
        if daily_trade_count >= self.p["max_daily_trades"]:
            return False, f"daily_trades>{self.p['max_daily_trades']}"
        # 日内亏损熔断
        if total_asset > 0:
            loss_pct = self._daily_pnl / total_asset
            if loss_pct <= self.p["daily_loss_limit_pct"]:
                self._halt(reason=f"daily_loss_pct {loss_pct*100:.2f}%")
                return False, f"halted:{self._halt_reason}"
        if self._daily_pnl <= -self.p["daily_loss_limit_abs"]:
            self._halt(reason=f"daily_loss_abs {self._daily_pnl:.2f}")
            return False, f"halted:{self._halt_reason}"
        return True, ""

    # ---------- 成交 ----------

    def on_fill(self, fill: Fill, avg_cost: float = 0.0,
                total_asset: float = 0.0) -> None:
        with self._lock:
            self.reset_daily()
            self._daily_trade_count += 1
            if avg_cost <= 0 or fill.quantity <= 0:
                return
            if fill.side == "SELL":
                pnl = (fill.price - avg_cost) * fill.quantity
                self._daily_pnl += pnl
                if pnl < 0:
                    self._consec_loss += 1
                elif pnl > 0:
                    self._consec_loss = 0
                # 连续亏损降仓 / 熔断
                if self._consec_loss >= self.p["max_consecutive_losses_halt"]:
                    self._halt(reason=f"consec_loss={self._consec_loss}")
                elif self._consec_loss >= self.p["max_consecutive_losses"]:
                    idx = min(len(_SCALE_LADDER) - 1, self._consec_loss
                              - self.p["max_consecutive_losses"] + 1)
                    logger.info("连续亏损 %s 次，仓位倍数 → %s",
                                self._consec_loss, _SCALE_LADDER[idx])
                # 日内亏损熔断（on_fill 时即可触发，不依赖 can_open）
                if self._daily_pnl <= -self.p["daily_loss_limit_abs"]:
                    self._halt(reason=f"daily_loss_abs {self._daily_pnl:.2f}")
                elif total_asset > 0:
                    loss_pct = self._daily_pnl / total_asset
                    if loss_pct <= self.p["daily_loss_limit_pct"]:
                        self._halt(reason=f"daily_loss_pct {loss_pct*100:.2f}%")

    # ---------- 冷却自动恢复（统一断路器）----------

    def _maybe_recover(self, today, total_asset: float = 0.0) -> bool:
        """熔断后冷却 N 个自然日自动解除。max_drawdown 用 dd_recover_days，
        consec_loss / daily_loss_abs 用 halt_recover_days。返回是否本 tick 解除。

        必须被每轮调用（on_asset_update / account_block_reason / can_open），
        以保证「空仓停牌」也会在日历冷却后恢复，而非永久死亡。
        """
        if not self._halted:
            return False
        held = (today - self._halt_day).days
        if self._halt_reason.startswith("max_drawdown"):
            recover_days = self.p.get("dd_recover_days", 5)
        else:
            recover_days = self.p.get("halt_recover_days", 1)
        if held >= recover_days:
            self._halted = False
            self._halt_reason = ""
            self._consec_loss = 0
            self._daily_pnl = 0.0
            if total_asset and total_asset > 0:
                self._peak_asset = total_asset   # 重置基线，避免解除后立即再熔断
            logger.info("RiskManager 熔断自动恢复（冷却 %s 日，重置风险基线）", held)
            system_notice("SUCCESS", "风控",
                          f"熔断自动恢复（冷却 {held} 日，已重置连亏/日内盈亏，恢复开仓）")
            return True
        return False

    # ---------- 手动恢复 ----------

    def resume(self, reason: str = "manual") -> None:
        self._halted = False
        self._halt_reason = ""
        self._consec_loss = 0
        self._daily_pnl = 0.0
        logger.info("RiskManager 恢复: %s", reason)
        system_notice("SUCCESS", "风控", f"熔断手动恢复: {reason}")

    def _halt(self, reason: str) -> None:
        if self._halted:
            return
        self._halted = True
        self._halt_reason = reason
        self._halt_day = date.today()   # 记录触发日，供 _maybe_recover 计算冷却窗口
        logger.warning("RiskManager 熔断: %s", reason)
        system_notice("ERROR", "风控",
                      f"触发熔断: {reason}（已暂停新开仓，冷却 {self.p.get('halt_recover_days', 1)} 日后自动恢复或手动 resume）")

    # ---------- 状态 ----------

    @property
    def is_halted(self) -> bool:
        return self._halted

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def consecutive_losses(self) -> int:
        return self._consec_loss

    @property
    def position_scale(self) -> float:
        if self._halted:
            return 0.0
        if self._consec_loss < self.p["max_consecutive_losses"]:
            return 1.0
        idx = min(len(_SCALE_LADDER) - 1,
                  self._consec_loss - self.p["max_consecutive_losses"] + 1)
        return _SCALE_LADDER[idx]

    def snapshot(self) -> dict:
        return {
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            "daily_pnl": round(self._daily_pnl, 2),
            "consecutive_losses": self._consec_loss,
            "position_scale": self.position_scale,
            "peak_asset": round(self._peak_asset, 2),
            "daily_trade_count": self._daily_trade_count,
        }
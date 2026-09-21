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
        # 【2026-09-21】连亏起算日。只存 consec_loss 数值而不存「何时开始亏」，
        # 重启后就无法判定这段连亏是否已过冷却期 → 冷却自愈永远算不出 held>=1，
        # 账户被 position_scale=0 永久冻结。此后该字段随 engine_state 持久化。
        self._consec_loss_date: Optional[date] = None
        # 【2026-09-16 优化】日内硬止损+强平标志。当日账户相对开盘资产亏损达
        #   daily_stop_flatten_pct 时置位，引擎据此强平全部可卖持仓（不只停牌）。
        self._flatten_requested: bool = False
        self._day_open_asset: float = 0.0

    # ---------- 每日重置 ----------

    def reset_daily(self) -> None:
        today = date.today()
        if today != self._today:
            self._today = today
            self._daily_pnl = 0.0
            self._daily_trade_count = 0
            # 日切不重置连续亏损与 peak_asset
            # 日内强平标志随新交易日复位（开盘资产口径会重新校准）
            self._flatten_requested = False
            self._day_open_asset = 0.0

    # ---------- 资金 ----------

    def on_asset_update(self, total_asset: float,
                        day_open_asset: float = 0.0) -> None:
        if total_asset <= 0:
            return
        # 【2026-09-21 P0 修复】日切重置原先只挂在 on_fill() 上：账户一旦被冻结
        # （零成交）就再也不会触发，导致 _daily_pnl 把 09-16 的 -188,766 元
        # 一直背到 09-21（实测值），日内亏损口径失真。改为每轮资产更新都校准日切。
        # 注意顺序：先 reset_daily（跨日会把 _day_open_asset 清零），再写入本次口径。
        self.reset_daily()
        if day_open_asset and day_open_asset > 0:
            self._day_open_asset = day_open_asset
        today = date.today()
        with self._lock:
            # 全部 halt 的「冷却自动恢复」（2026-08-30 统一断路器修正）：
            # 原实现仅 max_drawdown 类可恢复；consec_loss / daily_loss_abs 类在 _halt()
            # 后**永久停牌**——本趋势策略难免连亏，会触发 consec_loss=5 后停止开仓，
            # 使回测收益在实盘蜕变为 ~0%（2026-08-25 模拟盘已触发 consec_loss=5 佐证）。
            # 现在：任何 halt 冷却 N 个自然日后自动解除并重置风险基线
            # （max_drawdown 用 dd_recover_days；其余用 halt_recover_days），成为真「断路器」。
            self._maybe_recover(today, total_asset)
            # ---- 日内硬止损+强平（2026-09-16 新增）----
            # 此前 daily_loss_limit 只「暂停新开仓」，老仓完整吃了隔夜/盘中跌幅。
            # 这里用**开盘资产口径**（含隔夜重估）判断当日真实亏损：达
            #   daily_stop_flatten_pct 即置 flatten_requested 并停牌，引擎据此强平全部可卖持仓。
            if self._day_open_asset > 0:
                dlp = (total_asset - self._day_open_asset) / self._day_open_asset
                if dlp <= self.p.get("daily_stop_flatten_pct", -0.06):
                    if not self._flatten_requested:
                        logger.warning("RiskManager 日内硬止损触发: 当日亏损 %.2f%%",
                                      dlp * 100)
                        system_notice(
                            "ERROR", "风控",
                            f"日内硬止损: 当日账户亏损 {dlp*100:+.2f}%（相对开盘）"
                            f"已达强平阈值 {self.p.get('daily_stop_flatten_pct', -0.06)*100:.0f}%，"
                            f"强平全部可卖持仓并暂停新开仓")
                    self._flatten_requested = True
                    if not self._halted:
                        self._halt(reason=f"daily_stop {dlp*100:.2f}%")
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
                    if self._consec_loss == 0:
                        self._consec_loss_date = date.today()   # 连亏起算日
                    self._consec_loss += 1
                elif pnl > 0:
                    self._consec_loss = 0
                    self._consec_loss_date = None
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
            # 【2026-09-21】未熔断但仓位被陈旧连亏压到 0 的「僵尸冻结」自愈
            return self._maybe_recover_zombie(today)
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
            self._flatten_requested = False
            if total_asset and total_asset > 0:
                self._peak_asset = total_asset   # 重置基线，避免解除后立即再熔断
            logger.info("RiskManager 熔断自动恢复（冷却 %s 日，重置风险基线）", held)
            system_notice("SUCCESS", "风控",
                          f"熔断自动恢复（冷却 {held} 日，已重置连亏/日内盈亏，恢复开仓）")
            return True
        return False

    def _maybe_recover_zombie(self, today) -> bool:
        """【2026-09-21 P0 修复】「僵尸冻结」自愈：仓位倍数为 0 但未处于 halt。

        根因链（实盘 paper 已复现，2026-09-17/18/21 连续三日 100% 现金、0 成交）：
          1. 09-16 强平 5 笔全部亏损 → _consec_loss 累加到 9（≥ max_consecutive_losses_halt=5）；
          2. 连亏降仓阶梯 _SCALE_LADDER 末端为 0.0 → position_scale = 0.0；
          3. 引擎 `_handle_buy` 在 `if scale <= 0: return` 处直接返回，
             **任何买入信号（含 manual_entry 观察篮）都无法建仓**；
          4. _halted 标志**未持久化**，而 _consec_loss 从 engine_state 恢复为陈旧值 →
             重启后 halted=False，而原先的 `_maybe_recover` 首行即
             `if not self._halted: return False` → 冷却恢复**永不触发**；
          5. reset_daily() 只在 on_fill() 里调用 → 零成交时日切重置也永不执行。
        于是账户陷入「未熔断、却永久无法开仓」的僵尸态，且无自愈路径。

        修复：对「未 halt 但连亏已达 halt 阈值」的陈旧状态，套用**同一冷却窗口**
        （halt_recover_days）重置连亏与日内盈亏，使账户恢复到可交易状态。
        · 不重置 _peak_asset —— 保留真实回撤基线，max_drawdown 保护不弱化；
        · 不改变任何风险底线参数，仅恢复「可恢复断路器」的设计语义。

        ★ 冷却天数以 **_consec_loss_date（连亏起算日，已持久化）** 为准，
        不能用 _halt_day：后者不持久化、每次 __init__ 都被重置为 date.today()，
        用它算 held 恒为 0 → 自愈永远触发不了（这是首版修复的实际缺陷）。
        """
        if self._consec_loss < self.p["max_consecutive_losses_halt"]:
            return False
        start = self._consec_loss_date
        if start is None:
            # 无时间戳（老库升级上来的历史僵尸态）：无法证明这段连亏是「今天」发生的，
            # 而它已跨多个交易日滞留在 engine_state → 按已过冷却处理，立即解封。
            self._heal_zombie(0)
            logger.warning("RiskManager 僵尸冻结自愈（无连亏起算日，按已过冷却处理）："
                           "重置连亏/日内盈亏，仓位倍数恢复 1.0（回撤基线保留）")
            return True
        held = (today - start).days
        if held < self.p.get("halt_recover_days", 1):
            return False
        self._heal_zombie(held)
        logger.warning("RiskManager 僵尸冻结自愈（冷却 %s 日）：重置连亏/日内盈亏，"
                       "仓位倍数恢复 1.0（回撤基线保留）", held)
        return True

    def _heal_zombie(self, held: int) -> None:
        """执行僵尸解封：只清「连亏计数 + 日内盈亏」，**保留 peak_asset**。"""
        self._consec_loss = 0
        self._consec_loss_date = None
        self._daily_pnl = 0.0
        self._flatten_requested = False
        system_notice("SUCCESS", "风控",
                      f"仓位冻结自愈（冷却 {held} 日）：连亏计数与日内盈亏已重置，恢复开仓能力")

    # ---------- 状态持久化 ----------

    def export_state(self) -> dict:
        """供 engine_state.risk_state 落盘的风控状态（2026-09-21 新增）。"""
        return {
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            "halt_day": self._halt_day.isoformat() if self._halt_day else "",
            "consec_loss_date": (self._consec_loss_date.isoformat()
                                 if self._consec_loss_date else ""),
        }

    def load_state(self, state) -> None:
        """恢复风控状态（JSON 字符串或 dict；老库无此列时传 None 即可）。"""
        if not state:
            return
        if isinstance(state, str):
            import json as _json
            try:
                state = _json.loads(state)
            except Exception:
                return
        if not isinstance(state, dict):
            return
        self._halted = bool(state.get("halted"))
        self._halt_reason = str(state.get("halt_reason") or "")

        def _d(v):
            if not v:
                return None
            try:
                return date.fromisoformat(v) if isinstance(v, str) else v
            except Exception:
                return None

        hd = _d(state.get("halt_day"))
        if hd:
            self._halt_day = hd
        self._consec_loss_date = _d(state.get("consec_loss_date"))

    # ---------- 手动恢复 ----------

    def resume(self, reason: str = "manual") -> None:
        self._halted = False
        self._halt_reason = ""
        self._consec_loss = 0
        self._daily_pnl = 0.0
        self._flatten_requested = False
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
    def flatten_requested(self) -> bool:
        """日内硬止损强平请求（引擎据此平掉全部可卖持仓）。"""
        return self._flatten_requested

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
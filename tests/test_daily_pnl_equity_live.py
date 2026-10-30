# -*- coding: utf-8 -*-
"""live 日内盈亏口径：权益口径（总资产 − 当日开盘资产）取代「已实现盈亏」口径。

背景（实盘铁证，2026-10-30）
---------------------------
`engine_state.daily_pnl` 实测 **+37,448.30**，而当日真实盈亏约 **−140 元**。

原因：旧口径在 `on_fill` 里按 ``(成交价 − avg_cost) × qty`` 累加，而 live 的
``avg_cost`` 来自券商**摊薄成本**（含历史已实现盈亏）。300308 的摊薄成本是
194.246，10-30 以 569.24 卖出 100 股 ⇒ 记为 +37,499 的「当日盈利」，
而真实当日该笔是相对昨收的约 −140 元。

后果（双向都有害）：
- **正向掩盖**：``daily_loss_limit_abs=5,000`` 的日内亏损熔断被 +37k 完全掩盖 ⇒
  live 下这道熔断**事实上不存在**（今天若真亏 5,000 也不会停）；
- **反向误杀**：相对摊薄成本「亏损」卖出（实际当日可能盈利）会一次性触发假熔断。

修复：live 传 ``equity_daily_pnl=True`` ⇒ ``_daily_pnl`` 改为
「当前总资产 − 当日开盘资产」，每轮 ``on_asset_update`` 重算；``on_fill`` 不再累加
（否则双计）。paper / 回测不传该实参 ⇒ 逐位保持旧口径（P0 不变性）。

本文件钉住六条：
  1. 权益口径下 daily_pnl 等于权益差；
  2. ★ 对照：旧口径确实把摊薄成本算成巨额「当日盈利」，同一亏损路径下不熔断；
  3. 权益口径下同一亏损路径**必须**触发 daily_loss_abs（证明 2 不是恒绿）；
  4. 权益口径下 on_fill 不得再累加已实现盈亏（防双计）；
  5. paper 默认仍走已实现口径（回归锁，防「顺手把回测口径也改了」）；
  6. 引擎接线：live 传 True、paper 传 False，且 live 不恢复历史 daily_pnl。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.data_models import Fill, Order   # noqa: E402
from risk.manager import RiskManager       # noqa: E402

COST = 194.246          # 300308 券商摊薄成本（含历史已实现盈亏）
SELL_PX = 569.24        # 2026-10-30 策略卖出价
BIG = 200_000.0         # 用大资产把 3% 阈值抬到 6,000，避免撞上 daily_stop −6%
LOSS = 6_000.0          # 恰好等于 _daily_loss_limit(BIG) = max(5000, 3%×200k)


def _sell_fill() -> Fill:
    return Fill(ts=__import__("datetime").datetime(2026, 10, 30, 10, 8, 11),
                code="300308.SZ", side="SELL", quantity=100,
                price=SELL_PX, amount=SELL_PX * 100, account="cash")


def _probe_order() -> Order:
    """一笔必然通过其它闸门的小买单：只让「日内亏损」这一条决定放行与否。"""
    return Order(ts=__import__("datetime").datetime(2026, 10, 30, 14, 0, 0),
                 code="600000.SH", side="BUY", quantity=100, price=10.0,
                 order_type="limit", account="cash")


def _can_open(rm: RiskManager, total: float) -> bool:
    """日内亏损熔断在 ``can_open`` 里判定（不在 on_asset_update），必须走它。"""
    ok, _reason = rm.can_open(_probe_order(), {}, total, 0)
    return ok


# ---------------------------------------------------------------- ① 权益口径

def test_equity_caliber_equals_asset_delta():
    """权益口径：daily_pnl = 当前总资产 − 当日开盘资产。"""
    rm = RiskManager()
    rm.set_daily_pnl_equity_mode(True)
    rm.on_asset_update(BIG, BIG)                 # 开盘基线
    assert abs(rm.daily_pnl) < 1e-9, f"开盘时日内盈亏应为 0，实际 {rm.daily_pnl}"
    rm.on_asset_update(BIG - 1_234.0, BIG)
    assert abs(rm.daily_pnl - (-1_234.0)) < 1e-6, f"实际 {rm.daily_pnl}"
    rm.on_asset_update(BIG + 2_000.0, BIG)       # 盈利方向同样成立
    assert abs(rm.daily_pnl - 2_000.0) < 1e-6, f"实际 {rm.daily_pnl}"


# ------------------------------------------- ② 旧口径（对照：证明缺口真实存在）

def test_realized_caliber_masks_the_loss():
    """★ 对照：旧口径把摊薄成本差记成 +37,499「当日盈利」，同一亏损路径不熔断。

    这条**复现缺陷**，不是验证修复。若哪天有人把 on_fill 的累加逻辑改回去而
    以为「反正 live 传了权益口径」，这条会提醒口径确实曾失效（R46）。
    """
    rm = RiskManager()                            # 默认 = 已实现口径
    rm.on_asset_update(BIG, BIG)
    rm.on_fill(_sell_fill(), avg_cost=COST, total_asset=BIG)
    masked = rm.daily_pnl
    assert masked > 30_000, f"旧口径应记成巨额盈利，实际 {masked}"

    # 同一天真实权益亏 6,000：被 +37k 掩盖 ⇒ 仍可开仓（这正是缺陷）
    rm.on_asset_update(BIG - LOSS, BIG)
    assert _can_open(rm, BIG - LOSS), "旧口径竟然拦住了开仓，测试前提失效"
    assert not rm.is_halted, "旧口径竟然熔断了，测试前提失效"


# ------------------------------------------- ③ 权益口径（缺陷必须被堵住）

def test_equity_caliber_fires_daily_loss_abs():
    """权益口径下同一路径必须触发 daily_loss_abs —— 证明 ② 不是恒绿。"""
    rm = RiskManager()
    rm.set_daily_pnl_equity_mode(True)
    rm.on_asset_update(BIG, BIG)
    rm.on_asset_update(BIG - LOSS, BIG)
    assert abs(rm.daily_pnl + LOSS) < 1e-6, f"实际 {rm.daily_pnl}"
    assert not _can_open(rm, BIG - LOSS), "权益口径下日内亏损熔断仍未生效"
    assert rm.is_halted, "未置 halted 标志"
    assert "daily_loss" in rm._halt_reason, f"halt 原因异常: {rm._halt_reason}"


# ---------------------------------------------------------------- ④ 防双计

def test_equity_caliber_does_not_double_count_on_fill():
    """权益口径下 on_fill 不得再累加已实现盈亏（权益变化已全量重算）。"""
    rm = RiskManager()
    rm.set_daily_pnl_equity_mode(True)
    rm.on_asset_update(BIG, BIG)
    rm.on_fill(_sell_fill(), avg_cost=COST, total_asset=BIG)
    assert abs(rm.daily_pnl) < 1e-9, (
        f"on_fill 在权益口径下仍在累加已实现盈亏 ⇒ 双计，实际 {rm.daily_pnl}")


def test_equity_caliber_recomputes_after_manual_stale_value():
    """权益口径每轮重算 ⇒ 历史残留值（如 +37,448）会被立刻覆盖。"""
    rm = RiskManager()
    rm.set_daily_pnl_equity_mode(True)
    rm._daily_pnl = 37_448.30                    # 模拟 DB 恢复出来的旧值
    rm.on_asset_update(BIG, BIG)
    assert abs(rm.daily_pnl) < 1e-9, f"残留值未被重算覆盖，实际 {rm.daily_pnl}"


# ---------------------------------------------------------------- ⑤ 回归锁

def test_paper_default_keeps_realized_caliber():
    """paper/回测不传实参 ⇒ 仍是已实现口径（逐位保持旧行为）。"""
    rm = RiskManager()
    rm.on_asset_update(BIG, BIG)
    rm.on_fill(_sell_fill(), avg_cost=COST, total_asset=BIG)
    assert rm.daily_pnl > 30_000, (
        "paper 口径被一起改掉了 —— P0 不变性与回测可比性会被破坏")
    # 显式传 False 也应回到旧口径
    rm2 = RiskManager()
    rm2._equity_daily_pnl = True
    rm2.on_asset_update(BIG, BIG, equity_daily_pnl=False)
    rm2.on_fill(_sell_fill(), avg_cost=COST, total_asset=BIG)
    assert rm2.daily_pnl > 30_000


# ---------------------------------------------------------------- ⑥ 引擎接线

def test_engine_wires_equity_caliber_for_live_only():
    """源码契约：引擎调用点必须按 exec_mode 传参，且 live 不恢复历史 daily_pnl。

    为什么用源码断言：这是跨层接线（engine → risk），行为测试需要整套引擎桩；
    而漏接线的后果是**静默回到旧口径**（熔断再次失效），必须有一条能立刻变红的钉子。
    """
    src = (ROOT / "engine" / "event_engine.py").read_text(encoding="utf-8")
    assert "equity_daily_pnl=(getattr(self, \"exec_mode\", \"paper\") == \"live\")" in src, (
        "on_asset_update 调用点没有按 exec_mode 传 equity_daily_pnl ⇒ live 会退回旧口径")
    assert 'if restore_ledger:' in src and "self.risk._daily_pnl = 0.0" in src, (
        "live 恢复路径没有清零 daily_pnl ⇒ 历史残留值会在首轮前掩盖日内亏损")

# -*- coding: utf-8 -*-
"""OWNER 指定价位回补通道（`owner_reentry`）守卫【2026-11-02】。

为什么需要这条通道（这是算术，不是参数没调好）
----------------------------------------------
2026-11-02 假熔断误卖了 OWNER 要求长期持有的 300308.SZ（100 股）。OWNER 指令
「明天找合适的价格给我把中际旭创接回来」。但：

    权益 63,207.98  一手 300308 = 550×100 = 55,000 = 权益 **87.0%**
    max_single_position_pct = 0.40 ⇒ equity×0.40 = 25,283 < 55,000
    ⇒ PositionSizer.size() 恒返回 **0 股**

即使绕过 sizing，``RiskManager.can_open`` 仍以 ``pos_ratio>0.40`` 拒绝。
**「接回原仓位」在 40% 单仓上限下数学上不可能** —— 任何「调价格/调金额」的
尝试都不会成功，只会得到 0 股 + 一条 ``pos_ratio>0.4`` 的拒绝记录。

本文件钉死的契约
----------------
1. 价格必须落在 ``[min_price, max_price]``：高于上限**不追**，低于下限**不接**
   （下限 = 前低，破位即支撑失守，接飞刀）；
2. ``valid_until`` 到期作废，且只告警一次（不留永久后门、不刷屏）；
3. 已持仓即视为完成 —— 回补不得退化成**加仓**；
4. 金额超 ``max_amount`` 不下单（硬上限，防价格跳动放大敞口）；
5. ``owner_reentry=True`` 时：**不乘**连亏降仓系数（100×0.5=50 股不是整百，
   券商必拒而日志会假装「已提交」）、**不受** position_scale=0 僵尸态阻断；
6. ``allow_large_position=True`` **只**跳过单仓比例上限；熔断 / 单笔金额 /
   日内次数照旧（越权范围必须能一句话说清）；
7. ★ 正向对照：**不带** ``owner_reentry`` 时，同一笔 55,000 元订单必须仍被
   ``pos_ratio>0.4`` 拒绝 —— 否则说明「放开单仓上限」被实现成了全局放开，
   那比缺陷本身危险得多。
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import engine.event_engine as EE                       # noqa: E402
import config.settings as settings                     # noqa: E402
from core.data_models import Order, Position, Signal   # noqa: E402
from risk.manager import RiskManager                   # noqa: E402

CODE = "300308.SZ"
EQUITY = 63207.98
PRICE = 550.0


# ------------------------------------------------------------------ 配置夹具

BASE_CFG = {
    "code": CODE,
    "lots": 1,
    "min_price": 537.0,
    "max_price": 545.0,
    "max_amount": 56000.0,
    "earliest": "",      # 测试不关心时段（真实配置 09:35/14:50）
    "latest": "",
    "valid_until": "2099-12-31",
}


@contextmanager
def _with_cfg(**over):
    """临时改写 owner_reentry，用完还原（含「键缺失」这种原始状态）。"""
    missing = "owner_reentry" not in settings.STRATEGY_PARAMS
    old = settings.STRATEGY_PARAMS.get("owner_reentry")
    cfg = dict(BASE_CFG)
    cfg.update(over)
    settings.STRATEGY_PARAMS["owner_reentry"] = cfg
    try:
        yield cfg
    finally:
        if missing:
            settings.STRATEGY_PARAMS.pop("owner_reentry", None)
        else:
            settings.STRATEGY_PARAMS["owner_reentry"] = old


@contextmanager
def _capture_notices():
    """截获 system_notice，验证「告警只发一次」而不刷屏。"""
    old = EE.system_notice
    box = []
    EE.system_notice = lambda level, cat, msg: box.append((level, cat, msg))
    try:
        yield box
    finally:
        EE.system_notice = old


# ------------------------------------------------------- _owner_reentry_step

def _step_engine(held=()):
    """只装载 _owner_reentry_step 需要的最小属性集合。"""
    eng = SimpleNamespace()
    eng._positions = {c: Position(code=c, name=c, quantity=100,
                                  avg_cost=500.0, last_price=PRICE)
                      for c in held}
    eng._buys = []
    eng._total_asset = lambda: EQUITY

    def _buy(sig, tick, cp, fixed_amount=0.0, owner_reentry=False):
        eng._buys.append({"code": sig.code, "fixed_amount": fixed_amount,
                          "owner_reentry": owner_reentry})
        eng._positions[sig.code] = Position(
            code=sig.code, name=sig.name, quantity=100,
            avg_cost=sig.price, last_price=sig.price)

    eng._handle_buy = _buy
    eng._saved = []
    eng._save_signal = lambda s: eng._saved.append(s)
    return eng


def _ticks(price, code=CODE):
    return {code: SimpleNamespace(price=price)}


def test_buys_inside_price_band():
    eng = _step_engine()
    with _with_cfg():
        EE.EventEngine._owner_reentry_step(eng, _ticks(540.0), {})
    assert len(eng._buys) == 1, eng._buys
    b = eng._buys[0]
    assert b["code"] == CODE
    assert b["owner_reentry"] is True, "必须走 owner_reentry 分支（否则被 40% 上限拒）"
    # fixed_amount = 100×540×1.005（0.5% 余量防浮点 floor 把 100 股算成 0 股）
    assert abs(b["fixed_amount"] - 100 * 540.0 * 1.005) < 1.0, b["fixed_amount"]
    assert len(eng._saved) == 1, "买入决策必须写 signals 表（事后可审计）"
    assert eng._saved[0].side == "BUY"
    assert "owner_reentry" in eng._saved[0].reason


def test_does_not_chase_above_max_price():
    """高于上限不追 —— 550 收盘价直接买是「追」，不是「接」。"""
    eng = _step_engine()
    with _with_cfg():
        EE.EventEngine._owner_reentry_step(eng, _ticks(PRICE), {})
    assert eng._buys == [], eng._buys


def test_does_not_catch_falling_knife_below_min_price():
    """跌破前低（537）不接 —— 支撑失守时接飞刀，比买贵 2% 危险得多。"""
    eng = _step_engine()
    with _capture_notices() as box:
        with _with_cfg():
            EE.EventEngine._owner_reentry_step(eng, _ticks(530.0), {})
    assert eng._buys == [], eng._buys
    assert len(box) == 1, f"破位告警应恰好一次，实际 {len(box)}"
    assert "跌破下限" in box[0][2]


def test_break_notice_is_not_repeated_within_same_day():
    """破位告警同日只发一次（每个 tick 一条会把日志淹掉）。"""
    eng = _step_engine()
    with _capture_notices() as box:
        with _with_cfg():
            for _ in range(5):
                EE.EventEngine._owner_reentry_step(eng, _ticks(530.0), {})
    assert len(box) == 1, f"同日重复告警 {len(box)} 次"


def test_skips_when_already_held():
    """已持仓 ⇒ 回补完成。绝不能退化成「加仓」。"""
    eng = _step_engine(held=[CODE])
    with _with_cfg():
        EE.EventEngine._owner_reentry_step(eng, _ticks(540.0), {})
    assert eng._buys == [], eng._buys


def test_expired_config_does_nothing_and_warns_once():
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    eng = _step_engine()
    with _capture_notices() as box:
        with _with_cfg(valid_until=yesterday):
            for _ in range(3):
                EE.EventEngine._owner_reentry_step(eng, _ticks(540.0), {})
    assert eng._buys == [], "过期配置绝不许下单（防止永久后门）"
    assert len(box) == 1, f"过期告警应恰好一次，实际 {len(box)}"
    assert "过期" in box[0][2]


def test_respects_max_amount_hard_cap():
    """金额硬上限：545×100 = 54,500 > 50,000 ⇒ 不下单。"""
    eng = _step_engine()
    with _with_cfg(max_amount=50000.0):
        EE.EventEngine._owner_reentry_step(eng, _ticks(540.0), {})
    assert eng._buys == [], eng._buys


def test_respects_time_window():
    """时段外不下单（真实配置 09:35~14:50，避开开盘乱波与尾盘）。"""
    eng = _step_engine()
    with _with_cfg(earliest="23:59", latest="23:59"):
        EE.EventEngine._owner_reentry_step(eng, _ticks(540.0), {})
    assert eng._buys == [], eng._buys


def test_empty_or_malformed_config_is_noop():
    """配置为空 / 类型不对 ⇒ 完全不动（绝不能把买入路径搞崩）。"""
    for bad in ({}, None, [], "300308.SZ", {"code": ""},
                {"code": CODE, "lots": 0}, {"code": CODE, "lots": "x"}):
        eng = _step_engine()
        old = settings.STRATEGY_PARAMS.get("owner_reentry")
        settings.STRATEGY_PARAMS["owner_reentry"] = bad
        try:
            EE.EventEngine._owner_reentry_step(eng, _ticks(540.0), {})
        finally:
            settings.STRATEGY_PARAMS["owner_reentry"] = old
        assert eng._buys == [], f"配置 {bad!r} 时不得下单"


def test_lots_scale_qty():
    """lots 是股数的唯一来源（2 手 = 200 股）；顺带证明金额上限随之放大才放行。"""
    eng = _step_engine()
    with _with_cfg(lots=2, max_amount=120000.0):
        EE.EventEngine._owner_reentry_step(eng, _ticks(540.0), {})
    assert len(eng._buys) == 1
    assert abs(eng._buys[0]["fixed_amount"] - 200 * 540.0 * 1.005) < 1.0


def test_lots_two_blocked_by_default_amount_cap():
    """2 手 = 108,000 元 超默认 56,000 上限 ⇒ 不得下单（金额上限独立生效）。"""
    eng = _step_engine()
    with _with_cfg(lots=2):
        EE.EventEngine._owner_reentry_step(eng, _ticks(540.0), {})
    assert eng._buys == [], eng._buys


# ------------------------------------------------- _handle_buy 的股数与越权

class _FakeRisk:
    def __init__(self, scale=1.0, block=""):
        self.position_scale = scale
        self._block = block
        self.calls = []

    def account_block_reason(self, daily_trade_count):
        return self._block

    def can_open(self, order, positions, total_asset, n,
                 allow_large_position=False):
        self.calls.append({"qty": order.quantity, "price": order.price,
                           "allow": allow_large_position})
        return False, "stop_here(测试桩截断，不落单)"


class _FakeDaily:
    def atr_pct(self, code):
        return 0.0803            # 300308 实测 ATR14 = 8.03%

    def conviction_multiplier(self):
        return 1.0


class _FakeSizer:
    """镜像生产 40% 上限的结果：一手 55,000 > 25,283 ⇒ 0 股。"""

    def size(self, *a, **kw):
        return 0


def _buy_probe(scale=1.0, fixed_amount=0.0, owner_reentry=False,
               cash=EQUITY, price=545.0):
    eng = EE.EventEngine.__new__(EE.EventEngine)
    eng.data_mode = "xtdata"
    eng._market_stale = False
    eng._risk_asset_basis = "broker"
    eng._daily_trade_count = 0
    eng._cash = cash
    eng._positions = {}
    eng._log_reject_once = lambda c, r: None
    eng._total_asset = lambda: cash
    eng._clamp_to_buying_power = lambda code, qty, price: qty
    eng.daily = _FakeDaily()
    eng.sizer = _FakeSizer()
    eng.risk = _FakeRisk(scale)
    sig = Signal(ts=datetime.now(), code=CODE, name="中际旭创",
                 side="BUY", score=0.0, price=price, reason="test")
    EE.EventEngine._handle_buy(eng, sig, SimpleNamespace(price=price), {},
                               fixed_amount=fixed_amount,
                               owner_reentry=owner_reentry)
    return eng.risk.calls


def test_owner_reentry_keeps_lots_and_requests_cap_bypass():
    calls = _buy_probe(fixed_amount=100 * 545.0 * 1.005, owner_reentry=True)
    assert len(calls) == 1, calls
    assert calls[0]["qty"] == 100, f"OWNER 指定 1 手必须原样 100 股，实际 {calls[0]['qty']}"
    assert calls[0]["allow"] is True, "必须向 can_open 请求跳过单仓上限"


def test_owner_reentry_ignores_position_scale():
    """★ 100×(1.0×0.5)=50 股不是整百 ⇒ 券商必拒，而日志会假装「已提交」。"""
    calls = _buy_probe(fixed_amount=100 * 545.0 * 1.005,
                       owner_reentry=True, scale=0.5)
    assert calls and calls[0]["qty"] == 100, (
        f"OWNER 回补不得被连亏降仓系数砍半，实际 {calls}")


def test_owner_reentry_survives_position_scale_zero():
    """僵尸冻结（scale=0）时通道不得静默失效，否则「接回来」会永远不成交。"""
    calls = _buy_probe(fixed_amount=100 * 545.0 * 1.005,
                       owner_reentry=True, scale=0.0)
    assert calls and calls[0]["qty"] == 100, calls


def test_positive_control_non_owner_path_still_scaled_and_capped():
    """★ 正向对照：不带 owner_reentry 时，同一笔单必须 (a) 乘降仓系数、
    (b) 不请求越过单仓上限。若这条变「100 股 + allow=True」，说明越权被
    实现成了全局放开 —— 那比原缺陷危险得多。"""
    calls = _buy_probe(fixed_amount=100 * 545.0 * 1.005,
                       owner_reentry=False, scale=0.5)
    assert calls and calls[0]["qty"] == 50, f"观察篮仍应乘降仓系数，实际 {calls}"
    assert calls[0]["allow"] is False, "非回补路径不得请求跳过单仓上限"


def test_positive_control_scale_zero_blocks_non_owner_path():
    calls = _buy_probe(fixed_amount=100 * 545.0 * 1.005,
                       owner_reentry=False, scale=0.0)
    assert calls == [], "僵尸冻结仍应阻断观察篮（原行为不得被削弱）"


# --------------------------------------------------- RiskManager 越权范围

def _order(qty=100, price=545.0):
    return Order(ts=datetime.now(), code=CODE, side="BUY",
                 quantity=qty, price=price, order_type="limit",
                 account="cash")


def test_can_open_default_rejects_over_single_cap():
    rm = RiskManager()
    ok, reason = rm.can_open(_order(), {}, EQUITY, 0)
    assert not ok, "默认路径必须拒绝 87% 单仓（这是风控铁律）"
    assert "pos_ratio" in reason, reason


def test_can_open_allow_large_position_bypasses_cap_only():
    rm = RiskManager()
    ok, reason = rm.can_open(_order(), {}, EQUITY, 0, allow_large_position=True)
    assert ok, f"回补通道应放行单仓比例，实际 reason={reason}"


def test_can_open_allow_large_position_still_enforces_amount_cap():
    """越权范围必须精确：只跳过「单仓比例」，不跳过「单笔金额上限」。"""
    rm = RiskManager()
    big = _order(qty=1000, price=545.0)          # 545,000 > max_order_amount
    ok, reason = rm.can_open(big, {}, EQUITY, 0, allow_large_position=True)
    assert not ok, "单笔金额上限不得被越权穿透"
    assert "amount>" in reason, reason


def test_can_open_allow_large_position_still_enforces_halt():
    """越权不得穿透熔断 —— 否则「回补」会变成绕过熔断的后门。"""
    rm = RiskManager()
    rm._halted = True
    rm._halt_reason = "daily_stop -6.00%"
    ok, reason = rm.can_open(_order(), {}, EQUITY, 0, allow_large_position=True)
    assert not ok, "熔断期间回补通道也必须被拦"
    assert "halted" in reason, reason


def test_can_open_allow_large_position_still_enforces_daily_trades():
    rm = RiskManager()
    n = int(rm.p["max_daily_trades"]) + 1
    ok, reason = rm.can_open(_order(), {}, EQUITY, n, allow_large_position=True)
    assert not ok, "日内次数上限不得被越权穿透"
    assert "daily_trades" in reason, reason


if __name__ == "__main__":
    for k, v in sorted(globals().items()):
        if k.startswith("test_"):
            v()
            print(f"  ok {k}")
    print("OWNER REENTRY TESTS PASSED")

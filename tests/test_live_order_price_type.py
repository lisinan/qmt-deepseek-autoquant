# -*- coding: utf-8 -*-
"""live 委托「价格类型」根因守卫（2026-10-30 P0）。

背景（真实现象，非假设）
------------------------
2026-10-30 是切到 live（2026-10-29 15:05）后的**第一个真正开盘日**。
当日下单 **523 笔、成交 0 笔**：

  - 300308.SZ 趋势破位卖出重试 **522 次**（每轮 ~3s 一次），全部失败；
  - 510050.SH 观察篮探针买入 **1 次**，同样失败。

引擎日志只有 ``order_stock rc=-1``；真正原因在 miniQMT 自己的日志里：

    [orderservice] [quant] order failed, invalid price type [0]
    onOrderResponse ... orderid:-1, errmsg:invalid price type [0]

根因：``core/broker.py::place_order`` 把价格类型写成字面量 ``0``，并注释成
「限价单（LIMIT_PRICE=0）」。而 xtquant **没有** ``LIMIT_PRICE`` 常量，
限价是 ``FIX_PRICE = 11``，``0`` 不是任何合法价格类型 ⇒ 本地拒单（不进交易所）。
paper 模式不走 ``place_order`` ⇒ 该路径从未被执行 ⇒ 潜伏至首个 live 交易日。

本文件钉住三件事，防止回归：
  1. 限价单必须传 ``FIX_PRICE``、市价单传 ``LATEST_PRICE``，**任何情况下都不是 0**；
  2. 委托价必须夹到最小变动单位（股票 0.01 / ETF 0.001），去掉浮点噪声；
  3. **失败委托不得吃掉日内交易额度**（原实现 522 笔失败把计数刷到 497，
     超过 max_daily_trades=10 后把所有 BUY 锁死，掩盖了真实根因）。

★ 含正向对照（``test_successful_order_consumes_daily_budget``）：断言成功委托
  **必须**计数。否则「恒不计数」的退化实现会让第 3 组测试全绿（R43 第 16 条）。

全部离线：FakeTrader 取代真实 xttrader，FakeBroker 取代 qmt_broker 单例。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 与同批测试一致的隔离：禁止本模块写到生产库。
_TMP = Path(tempfile.mkdtemp(prefix="qmt_pxtype_"))
os.environ.setdefault("QMT_DB", str(_TMP / "pxtype_qmt.db"))
os.environ.setdefault("QMT_NOTICES_LOG", str(_TMP / "pxtype_notices.log"))

import core.broker as BK                                    # noqa: E402
from tests.test_order_lifecycle_b1b2 import (               # noqa: E402
    FakeBroker, _make_engine, _old_position, _sell_sig, _quiet_notices,
)
import engine.event_engine as EE                            # noqa: E402

STOCK = "300308.SZ"
ETF = "510050.SH"


# ----------------------------------------------------------------- 桩

class _FakeTrader:
    """记录型 xttrader 桩：只记 order_stock 的入参，不连券商。"""

    def __init__(self, rc=12345):
        self.calls = []          # [(code, order_type, qty, price_type, price)]
        self._rc = rc

    def order_stock(self, acc, code, order_type, quantity, price_type, price):
        self.calls.append((code, order_type, int(quantity), int(price_type),
                           float(price)))
        return self._rc


def _broker_with(trader):
    """构造一个「已连接」的 QMTBroker，绕开 connect() 的真实网络路径。"""
    b = BK.QMTBroker.__new__(BK.QMTBroker)
    b._trader = trader
    b._connected = True
    b._subscribed = {"cash": object()}
    b.connect = lambda force=False: True
    b._account_obj = lambda account: object()
    return b


class _FailBroker(FakeBroker):
    """下单必失败的券商桩（复现 order_stock rc=-1 的真实返回形态）。"""

    def place_order(self, code, side, qty, price, account="cash"):
        self.place_calls.append((code, side, int(qty), price, account))
        return {"ok": False, "order_id": None, "reason": "order_stock rc=-1"}


# ===================================================== 1. 价格类型（根因）

def test_limit_order_uses_fix_price_not_zero():
    """限价单价格类型必须是 FIX_PRICE(11)；传 0 会被 miniQMT 判 invalid。"""
    trader = _FakeTrader()
    res = _broker_with(trader).place_order(ETF, "BUY", 500, 2.846, "cash")
    assert res["ok"], res
    assert len(trader.calls) == 1, trader.calls
    _code, _otype, _qty, price_type, _px = trader.calls[0]
    assert price_type == BK_FIX_PRICE(), (
        f"限价单 price_type={price_type}，应为 FIX_PRICE={BK_FIX_PRICE()}；"
        f"传 0 会被 miniQMT 直接拒绝（invalid price type [0]）")
    assert price_type != 0, "price_type 绝不允许是 0"


def test_market_order_uses_latest_price():
    """price<=0 视为市价单 ⇒ LATEST_PRICE(5)，同样不是 0。"""
    trader = _FakeTrader()
    _broker_with(trader).place_order(STOCK, "SELL", 100, 0.0, "cash")
    _code, _otype, _qty, price_type, px = trader.calls[0]
    assert price_type == BK_LATEST_PRICE(), price_type
    assert price_type != 0, "市价单也不允许传 0"
    assert px == 0, px


def test_negative_rc_is_reported_as_failure():
    """order_stock 返回负数 ⇒ ok=False，且 reason 里带上真实的 rc。"""
    trader = _FakeTrader(rc=-1)
    res = _broker_with(trader).place_order(ETF, "BUY", 500, 2.846, "cash")
    assert res["ok"] is False, res
    assert "rc=-1" in res["reason"], res


# ===================================================== 2. 价格夹到最小变动

def test_stock_price_rounded_to_cent():
    """股票 0.01 最小变动：浮点噪声 560.3000000000001 必须收敛成 560.3。"""
    trader = _FakeTrader()
    _broker_with(trader).place_order(STOCK, "SELL", 100, 560.3000000000001, "cash")
    assert trader.calls[0][4] == 560.3, trader.calls[0]


def test_etf_price_keeps_three_decimals():
    """ETF 0.001 最小变动：2.846 不得被夹成 2.85（会偏离 1 个 tick 以上）。"""
    trader = _FakeTrader()
    _broker_with(trader).place_order(ETF, "BUY", 500, 2.8461, "cash")
    assert trader.calls[0][4] == 2.846, trader.calls[0]


# ===================================================== 3. 失败不吃日内额度

def test_failed_sell_does_not_consume_daily_budget():
    """失败卖单不得计数（原实现：522 笔失败把计数刷到 497 → 锁死所有 BUY）。"""
    eng = _make_engine("live")
    pos = _old_position(code=STOCK, qty=100, avail=100)
    eng._positions[pos.code] = pos
    eng._daily_trade_count = 0
    with _quiet_notices(), patch.object(EE, "qmt_broker", _FailBroker()):
        eng._handle_sell(_sell_sig(code=STOCK, price=560.0), pos)
    assert eng._daily_trade_count == 0, (
        f"失败委托被计入日内额度：count={eng._daily_trade_count}。"
        f"这会重演 2026-10-30「真实根因被 daily_trades>10 掩盖」的事故")


def test_successful_order_consumes_daily_budget():
    """★ 正向对照：成功委托**必须**计数。

    若这条也绿，说明计数逻辑已退化成「恒不计数」——那上面的
    ``test_failed_sell_does_not_consume_daily_budget`` 就毫无意义。
    """
    eng = _make_engine("live")
    pos = _old_position(code=STOCK, qty=100, avail=100)
    eng._positions[pos.code] = pos
    eng._daily_trade_count = 0
    with _quiet_notices(), patch.object(EE, "qmt_broker", FakeBroker()):
        eng._handle_sell(_sell_sig(code=STOCK, price=560.0), pos)
    assert eng._daily_trade_count == 1, (
        f"成功委托未计数：count={eng._daily_trade_count} ⇒ "
        f"max_daily_trades 闸门将永久失效")


# ===================================================== 4. 拒单原因必须落日志

def test_order_error_callback_logs_reason():
    """on_order_error 必须把 errmsg 写日志（原实现是 pass，原因被吞）。"""
    import logging

    class _E:
        order_id = 0
        error_id = -1
        error_msg = "invalid price type [0]"

    captured = []

    class _H(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    h = _H()
    BK.logger.addHandler(h)
    try:
        BK._BrokerDisconnectCallback(_broker_with(_FakeTrader())).on_order_error(_E())
    finally:
        BK.logger.removeHandler(h)
    assert any("invalid price type" in m for m in captured), (
        f"拒单原因没有落日志，captured={captured}")
    assert any("error_id" in m for m in captured), captured


# ----------------------------------------------------------------- 常量代理
# 通过 broker 模块间接取常量：若将来 xtconstant 变更，本测试会一起变红，
# 而不是硬编码 11 / 5 之后失效（R43：断言钉契约，不钉副产品）。

def BK_FIX_PRICE():
    from xtquant.xtconstant import FIX_PRICE
    return FIX_PRICE


def BK_LATEST_PRICE():
    from xtquant.xtconstant import LATEST_PRICE
    return LATEST_PRICE

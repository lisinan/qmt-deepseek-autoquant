# -*- coding: utf-8 -*-
"""持仓订阅 + 盯市价兜底守卫（2026-10-30 P1）。

背景（真实事故，非假设）
------------------------
2026-10-30 下午 OWNER 手工买回 300308.SZ（100 股）。引擎 17:32 重启后面板显示
`total_asset = 27,812.98`，而券商真实总资产约 65,748 —— 低估 **57.7%**。

根因链（两个独立缺口，缺一不可）：
  ① `_subscribe_codes()` = UNIVERSE + VALIDATION_UNIVERSE + 动态活跃池，
     **不含持仓**。300308 被策略卖出后已不在候选池 ⇒ 重启后收不到 tick；
  ② `_mark_price_fallback()` 第 ② 档查 `DailyContext`，而日线上下文同样只覆盖
     候选池 ⇒ 池外持仓掉到第 ④ 档 = `avg_cost`（300308 被记成 194.246）。
  ⇒ `last_price = avg_cost` ⇒ `_total_asset()` 用成本价拼装。

为什么危险：`_total_asset()` 在 live 模式优先用券商总资产，但只在 120s 内新鲜时
有效。券商查询连续失败 / 断连超过 120s，拼装口径立刻接管，而 `risk/manager.py`
的 `max_drawdown` 判定没有陈旧行情守卫 ⇒ −57.7% 直接打出账户级熔断并冻结开仓。
（这与 2026-10-29 的 −58.2% 熔断是同一根因的第二次复现。）

本文件钉住六条：
  1. 持仓代码必须在订阅清单里（事故场景）；
  2. ★ 正向对照：数量为 0 的残留记录**不得**进入订阅（防「全都订阅」式假绿）；
  3. 盘中同步到新持仓后必须补订阅（自愈，OWNER 手工建仓也能收到行情）；
  4. 池外持仓的兜底价取日线收盘，而不是成本价；
  5. 兜底取数失败要短期缓存，避免 mv 恒缺失时每轮同步都打网络；
  6. ★ 正向对照：有 tick 时必须用 tick，且**不发**任何日线请求。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="qmt_possub_"))
os.environ.setdefault("QMT_DB", str(_TMP / "possub_qmt.db"))
os.environ.setdefault("QMT_NOTICES_LOG", str(_TMP / "possub_notices.log"))

import engine.event_engine as EE          # noqa: E402

OWNER_CODE = "300308.SZ"     # 中际旭创：事故标的（注意它在 UNIVERSE 内）
POOL_EXTERNAL = "999001.SZ"  # 池外代码：只可能因「持仓」被订阅
COST = 194.246               # 券商摊薄成本价（错值来源）
CLOSE = 573.43               # 最近日线收盘（正确盯市价）


class _FakeClient:
    """行情客户端桩：记录订阅与日线取数次数。"""

    def __init__(self, history=None):
        self.subscribed_calls = []
        self.history_calls = 0
        self._history = history

    def subscribe(self, codes):
        self.subscribed_calls.append(list(codes))

    def get_history(self, code, period="1d", count=100):
        self.history_calls += 1
        return self._history


def _engine(codes_held=(OWNER_CODE,), qty=100, last_ticks=None, daily=None):
    """最小引擎桩：只构造被测方法读取的状态。"""
    eng = EE.EventEngine.__new__(EE.EventEngine)
    eng._positions = {
        c: EE.Position(code=c, name="x", quantity=qty, available=qty,
                       avg_cost=COST, last_price=COST)
        for c in codes_held
    }
    eng._last_ticks = last_ticks or {}
    eng._mark_close_cache = {}
    eng.daily = daily
    eng.dynamic_universe = None          # 不引入动态池噪声
    return eng


def _fake_broker(positions, cash=6953.42, total=65748.42):
    return SimpleNamespace(
        is_connected=True,
        get_asset=lambda acct: {"cash": cash, "total_asset": total},
        get_positions=lambda acct: positions,
    )


# ---------------------------------------------------------------- 订阅清单

def test_held_position_is_subscribed():
    """事故场景：池外的持仓代码必须进订阅清单，否则收不到 tick。

    ★ 这里刻意用**池外**代码而非 300308 —— 300308 本身在 UNIVERSE 里，
    用它断言「在订阅清单中」即使没有本修复也会通过，是典型的假绿。
    """
    assert POOL_EXTERNAL not in EE.UNIVERSE, "对照组失效：该代码竟然在 UNIVERSE 内"
    assert POOL_EXTERNAL not in EE.VALIDATION_UNIVERSE, "对照组失效：该代码在验证篮内"
    eng = _engine(codes_held=(POOL_EXTERNAL,))
    codes = eng._subscribe_codes()
    assert POOL_EXTERNAL in codes, (
        "持仓不在订阅清单 ⇒ 收不到 tick ⇒ 盯市价退回成本价 ⇒ 总资产被低估")


def test_zero_quantity_record_not_subscribed():
    """★ 正向对照：数量 0 的残留记录不进订阅。

    `_sync_broker_positions` 只把数量置 0、保留元数据以便复盘；若订阅清单不筛
    数量，历史交易过的每一只都会永久订阅（无限增长）。这条保证第 1 条不是因为
    「什么都订阅」而通过的。
    """
    eng = _engine(codes_held=(POOL_EXTERNAL,), qty=0)
    assert POOL_EXTERNAL not in eng._subscribe_codes(), (
        "数量为 0 的残留记录也进了订阅 ⇒ 订阅清单会无限膨胀")


def test_sync_broker_positions_subscribes_new_code():
    """盘中同步到池外新持仓后必须补订阅（OWNER 手工建仓自愈）。"""
    eng = _engine(codes_held=())
    eng._cash = 0.0
    eng._broker_total_asset = 0.0
    eng._broker_total_ts = 0.0
    eng._sync_fail_count = 0
    eng._pos_meta = {}
    client = _FakeClient(history=[{"close": CLOSE}])

    orig_broker, orig_client = EE.qmt_broker, EE.qmt_client
    EE.qmt_broker = _fake_broker([{"code": POOL_EXTERNAL, "quantity": 100,
                                   "avg_cost": COST, "market_value": 57343.0}])
    EE.qmt_client = client
    try:
        eng._sync_broker_positions()
    finally:
        EE.qmt_broker, EE.qmt_client = orig_broker, orig_client

    assert client.subscribed_calls, "同步到新持仓后没有补发订阅"
    assert POOL_EXTERNAL in client.subscribed_calls[-1], (
        "新持仓代码未出现在补订阅清单中")


# ---------------------------------------------------------------- 兜底价

def test_pool_external_position_marks_at_close_not_cost():
    """池外持仓（日线上下文没有）必须用日线收盘作盯市价，而不是成本价。"""
    eng = _engine(daily=None)
    client = _FakeClient(history=[{"close": 570.0}, {"close": CLOSE}])
    orig = EE.qmt_client
    EE.qmt_client = client
    try:
        px = eng._mark_price_fallback(OWNER_CODE, COST)
    finally:
        EE.qmt_client = orig
    assert abs(px - CLOSE) < 1e-6, f"盯市价仍是 {px}（成本价 {COST}），未取日线收盘"
    assert client.history_calls == 1


def test_failed_history_lookup_is_cached_briefly():
    """取数失败后 300s 内不重复请求（mv 恒缺失时每轮同步只有一次网络开销）。"""
    eng = _engine(daily=None)
    client = _FakeClient(history=None)          # 模拟行情源拉不到
    orig = EE.qmt_client
    EE.qmt_client = client
    try:
        a = eng._mark_price_fallback(OWNER_CODE, COST)
        b = eng._mark_price_fallback(OWNER_CODE, COST)
    finally:
        EE.qmt_client = orig
    assert a == COST and b == COST, "取不到日线时应回退成本价（保底不抛异常）"
    assert client.history_calls == 1, (
        f"失败未缓存，300s 内重复请求了 {client.history_calls} 次")


def test_tick_wins_and_no_history_request():
    """★ 正向对照：有 tick 时必须用 tick，且完全不触发日线请求。"""
    eng = _engine(last_ticks={OWNER_CODE: {"price": 573.9}})
    client = _FakeClient(history=[{"close": CLOSE}])
    orig = EE.qmt_client
    EE.qmt_client = client
    try:
        px = eng._mark_price_fallback(OWNER_CODE, COST)
    finally:
        EE.qmt_client = orig
    assert abs(px - 573.9) < 1e-6
    assert client.history_calls == 0, "已有 tick 仍去打日线请求（多一层无用网络）"


def test_negative_cache_expires_after_300s():
    """负缓存到期后必须重试（否则一次瞬时失败会锁死一整天）。"""
    eng = _engine(daily=None)
    client = _FakeClient(history=None)
    orig = EE.qmt_client
    EE.qmt_client = client
    try:
        eng._mark_price_fallback(OWNER_CODE, COST)
        key = (OWNER_CODE, time.strftime("%Y-%m-%d"))
        ts, close = eng._mark_close_cache[key]
        eng._mark_close_cache[key] = (ts - 301.0, close)     # 伪造过期
        eng._mark_price_fallback(OWNER_CODE, COST)
    finally:
        EE.qmt_client = orig
    assert client.history_calls == 2, "负缓存过期后没有重试"

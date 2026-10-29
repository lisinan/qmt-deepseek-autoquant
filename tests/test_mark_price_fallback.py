# -*- coding: utf-8 -*-
"""盯市价兜底守卫：券商 ``market_value`` 缺失时不得回退成本价（2026-10-29 RSI 修复）。

【实盘铁证（本轮触发原因）】
  2026-10-29 live 首日，``storage/qmt.db`` 的 ``engine_state`` 记录：

      300308.SZ  last_price = 194.757   ← 恰好等于 avg_cost
      真实价（xtdata 实取）  = 582.05

  根因在 ``_sync_broker_positions``：``last = mv/qty if mv>0 else avg_cost``。
  券商返回的 ``market_value`` 缺失时，盯市价被压成**成本价**，于是

      total_asset = 8,337.28 + 100×194.757 = 27,812.98
      peak_asset  = 66,542.31
      dd          = **−58.2%**

  而 ``risk/manager.py:129-135`` 的 ``max_drawdown`` 判定**没有陈旧行情守卫**
  （与 ``daily_stop`` 不同，后者至少被 ``_market_stale`` 挡了一层）⇒ 会立即
  ``_halt()``，冻结开仓 ``dd_recover_days=3`` 天。

  本轮修复后：mv 缺失时依次用「本轮 tick 快照 → 日线收盘 → 成本价」兜底。

本文件锁死四件事：
  ① 券商正常返回市值时**逐位不变**（零回归，不能被兜底逻辑污染）；
  ② 三级兜底各自的优先级；
  ③ 端到端：mv 缺失时不会打出假回撤熔断；
  ④ **正向对照**：若仍回退成本价则必然熔断 —— 防止「检测器退化成恒绿」
     （R43 第 16 条「合谋式假绿」）。
"""
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import engine.event_engine as EE
from core.data_models import Position
from engine.event_engine import EventEngine
from risk.manager import RiskManager
from strategy.daily_context import DailyFeatures

CODE = "300308.SZ"
PEAK = 66542.31          # 2026-10-29 15:05 切 live 时重置的 peak_asset
CASH = 8337.28
AVG = 194.757            # 真实成本价（原始仓位）
TRUE_PX = 582.05         # 2026-10-29 xtdata 实时价


# ---------------------------------------------------------------- 测试替身

class _FakeBroker:
    """只为 _sync_broker_positions 提供最小接口。"""

    def __init__(self, positions, cash=CASH):
        self.is_connected = True
        self._positions = positions
        self._cash = cash

    def get_asset(self, account="cash"):
        return {"cash": self._cash}

    def get_positions(self, account="cash"):
        return self._positions


class _FakeDaily:
    """只实现 has / features（兜底只用到这两个）。"""

    def __init__(self, closes=None):
        self._closes = dict(closes or {})

    def has(self, code):
        return code in self._closes

    def features(self, code):
        if code not in self._closes:
            return None
        return DailyFeatures(code=code, close=self._closes[code])


def _bare_engine(last_ticks=None, daily=None, positions=None):
    """绕过 __init__（会连 xtdata / 起线程）的最小引擎对象。"""
    eng = EventEngine.__new__(EventEngine)
    eng._positions = positions if positions is not None else {
        CODE: Position(code=CODE, name="中际旭创", quantity=100,
                       avg_cost=AVG, last_price=AVG, peak_price=AVG),
    }
    eng._pos_meta = {}
    eng._cash = 0.0
    eng._last_ticks = dict(last_ticks or {})
    eng.daily = daily
    eng._sync_fail_count = 0
    eng._sync_fail_notice_day = None
    return eng


def _broker_row(mv=0.0):
    """券商原始持仓行。mv=0 模拟「market_value 字段缺失/为 0」。"""
    return {"code": CODE, "quantity": 100, "avg_cost": AVG,
            "market_value": mv, "available": 100}


@contextmanager
def _patched_broker(fake):
    """临时替换模块级 qmt_broker。

    ★ 不用 pytest 的 monkeypatch fixture：``tests/run_all.py`` 是自定义 runner，
      直接 ``fn()`` 调用，不支持 fixture 注入（本轮第一次跑就是这么红的）。
      自管理 patch 使本文件在 pytest 与 run_all 下都能跑。
    """
    old = getattr(EE, "qmt_broker", None)
    EE.qmt_broker = fake
    try:
        yield
    finally:
        EE.qmt_broker = old


def _sync(eng, mv=0.0):
    with _patched_broker(_FakeBroker([_broker_row(mv)])):
        eng._sync_broker_positions()
    return eng._positions[CODE]


# ---------------------------------------------------------------- ① 零回归

def test_valid_market_value_wins_over_ticks():
    """券商给了市值 ⇒ 必须用 mv/qty，且**不受** tick/日线影响。

    这条是零回归门锁：兜底逻辑绝不能覆盖权威源。
    """
    eng = _bare_engine(
        last_ticks={CODE: {"price": 999.99}},          # 干扰项
        daily=_FakeDaily({CODE: 888.88}),              # 干扰项
    )
    pos = _sync(eng, mv=TRUE_PX * 100)

    assert abs(pos.last_price - TRUE_PX) < 1e-6, (
        f"有市值时应取 mv/qty={TRUE_PX}，实际 {pos.last_price}")


# ---------------------------------------------------------------- ② 三级兜底

def test_missing_mv_uses_live_tick():
    """mv 缺失 → 用本轮 tick 快照（第一优先）。"""
    eng = _bare_engine(last_ticks={CODE: {"price": TRUE_PX}},
                       daily=_FakeDaily({CODE: 888.88}))
    pos = _sync(eng, mv=0.0)

    assert abs(pos.last_price - TRUE_PX) < 1e-6, (
        f"应优先用 tick 价 {TRUE_PX}，实际 {pos.last_price}（若为 {AVG} 说明回退了成本价）")


def test_missing_mv_and_tick_uses_daily_close():
    """mv 与 tick 都没有 → 用日线最新收盘（第二优先）。"""
    eng = _bare_engine(last_ticks={}, daily=_FakeDaily({CODE: 570.0}))
    pos = _sync(eng, mv=0.0)

    assert abs(pos.last_price - 570.0) < 1e-6, (
        f"应回退日线收盘 570.0，实际 {pos.last_price}")


def test_all_sources_missing_falls_back_to_cost():
    """三源全无 → 才回退成本价（旧行为，保底不抛异常）。"""
    eng = _bare_engine(last_ticks={}, daily=None)
    pos = _sync(eng, mv=0.0)

    assert abs(pos.last_price - AVG) < 1e-6, (
        f"全无数据时应回退 avg_cost={AVG}，实际 {pos.last_price}")


def test_tick_with_zero_price_is_ignored():
    """tick 存在但价格为 0（停牌/脏数据）→ 不能让 0 价进入账本。"""
    eng = _bare_engine(last_ticks={CODE: {"price": 0.0}},
                       daily=_FakeDaily({CODE: 570.0}))
    pos = _sync(eng, mv=0.0)

    assert pos.last_price > 0, "盯市价不得为 0"
    assert abs(pos.last_price - 570.0) < 1e-6


# ---------------------------------------------------------------- ③ 端到端

def test_missing_mv_does_not_produce_fake_drawdown_halt():
    """mv 缺失 + 有 tick ⇒ 总资产不被低估，不触发 −20% 熔断。"""
    eng = _bare_engine(last_ticks={CODE: {"price": TRUE_PX}})
    _sync(eng, mv=0.0)

    total = eng._total_asset()
    dd = (total - PEAK) / PEAK

    assert dd > -0.20, (
        f"总资产 {total:,.2f} 对 peak {PEAK:,.2f} 为 {dd*100:+.2f}%，"
        f"会触发 max_drawdown 熔断 —— 盯市价没被兜底修正")

    rm = RiskManager()
    rm.on_asset_update(PEAK, PEAK)          # 先建立峰值基线
    rm.on_asset_update(total, PEAK)
    assert not rm.is_halted, f"风控被误熔断：{rm._halt_reason}"
    assert not rm.flatten_requested, (
        "触发了日内硬止损强平请求 —— 这会**卖掉真实持仓**，比熔断更严重")


# ---------------------------------------------------------------- ④ 正向对照

def test_cost_fallback_would_have_halted():
    """正向对照：若仍回退成本价，则必然熔断 —— 证明上一条不是恒绿。

    这条用例故意**复现旧实现**（mv=0 → avg_cost），验证它确实会打出
    −58% 回撤并 halt。若有一天风控加了守卫使这里不再 halt，本用例会变红，
    提醒重新评估「兜底是否还必要」，而不是让整组测试无声失效。
    """
    eng = _bare_engine(last_ticks={}, daily=None)   # 强制回退成本价
    _sync(eng, mv=0.0)
    assert abs(eng._positions[CODE].last_price - AVG) < 1e-6

    total = eng._total_asset()
    dd = (total - PEAK) / PEAK
    assert dd <= -0.20, "对照组未复现出假回撤，测试前提失效"

    rm = RiskManager()
    rm.on_asset_update(PEAK, PEAK)
    rm.on_asset_update(total, PEAK)
    assert rm.is_halted, (
        "对照组本该熔断却没有 —— 说明风控已加守卫，请复核兜底的必要性")
    # ★ 实测危害比「熔断」更高：day_open_asset 若也取到成本价口径，
    #   daily_stop_flatten 会先命中 ⇒ flatten_requested=True ⇒ **强平真实持仓**。
    assert rm.flatten_requested, (
        "对照组应同时置起强平请求（这是本缺陷最危险的后果）")

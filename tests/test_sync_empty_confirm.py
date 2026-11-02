# -*- coding: utf-8 -*-
"""持仓同步闸门：单次「券商返回空持仓」不得销毁本地账本（2026-11-02 P0 上游）。

真实事故链（2026-11-02 10:34:10，真实资金，非假设）
--------------------------------------------------
1. 券商 `get_positions()` 某一轮返回 **[]**（空列表），而 `get_asset()` 同时刻
   `total_asset` 不可用；
2. 旧实现把 [] 当「确认空仓」⇒ 本地 `_positions` 全部 quantity 置 0；
3. `_total_asset()` 退化为纯现金 6,953.34，对开盘 64,683.84 算得 **−89.25%**；
4. 命中 daily_stop −6% ⇒ 强平 300308×100 @548.61 + 510050×500 @2.867（真实成交）；
5. 10:34:58 权益恢复 63,258.84 ⇒ 单轮毛刺。

同日已修的是**下游**（熔断改吃 `_risk_total_asset()`，不可信就跳过 —— 见
`test_risk_asset_trust.py`）。本文件钉的是**上游**：不让错误状态被生产出来。
为什么上游必须单独修：熔断只是「不再按错数字开火」，但本地账本被清空后，
策略会认为自己是空仓 ⇒ `max_positions` 失效、可能对同一标的重复买入。

契约：
  1. 单次 [] + 本地有仓 ⇒ **本轮不动账本**，计数 +1；
  2. 连续 SYNC_EMPTY_CONFIRM_N 轮 [] ⇒ 确认为真空仓，归零并上首屏告警；
  3. 中间夹一次非空 ⇒ 计数清零（真实数据能否决毛刺）；
  4. 正向对照：正常非空返回必须照常更新（闸门不得把正常同步一起拦掉）；
  5. `get_positions()` 返回 None（查询失败）仍按旧语义「本轮不动」。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="qmt_syncempty_"))
os.environ.setdefault("QMT_DB", str(_TMP / "syncempty_qmt.db"))
os.environ.setdefault("QMT_NOTICES_LOG", str(_TMP / "syncempty_notices.log"))

from core.data_models import Position          # noqa: E402
import engine.event_engine as EE               # noqa: E402

CODE = "300308.SZ"
CASH = 6953.34
BROKER_TOTAL = 63344.84


class _FakeBroker:
    """最小券商桩：positions 可为 list / None。"""

    def __init__(self, positions, total=BROKER_TOTAL, cash=CASH):
        self.is_connected = True
        self._positions = positions
        self._total = total
        self._cash = cash

    def get_asset(self, account="cash"):
        return {"cash": self._cash, "total_asset": self._total}

    def get_positions(self, account="cash"):
        return self._positions


class _FakeClient:
    mode = "xtdata"

    def subscribe(self, codes):
        return None


def _engine():
    eng = EE.EventEngine(
        exec_mode="live",
        auto_init_positions=False,
        enable_sector_scorer=False,
        enable_dynamic_universe=False,
        enable_llm_reranker=False,
    )
    eng._cash = CASH
    eng._positions = {
        CODE: Position(code=CODE, name="中际旭创", quantity=100,
                       avg_cost=195.0, last_price=549.94),
    }
    return eng


def _sync(eng, positions):
    """用桩跑一轮 _sync_broker_positions，返回本轮日志（便于断言告警）。"""
    old_broker, old_client = EE.qmt_broker, EE.qmt_client
    EE.qmt_broker = _FakeBroker(positions)
    EE.qmt_client = _FakeClient()
    try:
        eng._sync_broker_positions()
    finally:
        EE.qmt_broker, EE.qmt_client = old_broker, old_client


def _qty(eng, code=CODE):
    pos = eng._positions.get(code)
    return int(getattr(pos, "quantity", 0)) if pos else 0


# ------------------------------------------------ 1. 核心防线：单次空返回不清账

def test_single_empty_does_not_wipe_ledger():
    eng = _engine()
    _sync(eng, [])
    assert _qty(eng) == 100, (
        "单次「券商返回空持仓」不得销毁本地账本（11-02 事故的直接成因），"
        f"实际 quantity={_qty(eng)}")
    assert eng._sync_empty_count == 1, (
        f"应记 1 次待确认，实际 {eng._sync_empty_count}")


def test_two_empty_confirmed_wipes_ledger():
    eng = _engine()
    _sync(eng, [])
    assert _qty(eng) == 100
    _sync(eng, [])
    assert _qty(eng) == 0, (
        f"连续 {eng.SYNC_EMPTY_CONFIRM_N} 轮空返回应确认为真空仓并归零")
    assert eng._sync_empty_count == 0, "确认清空后计数必须归零，否则会误伤下一轮"


# ------------------------------------------------ 2. 真实数据能否决毛刺

def test_nonempty_resets_counter():
    eng = _engine()
    _sync(eng, [])                    # 第 1 次毛刺
    _sync(eng, [{"code": CODE, "quantity": 100, "avg_cost": 195.0,
                 "market_value": 54994.0, "available": 100}])
    assert eng._sync_empty_count == 0, (
        f"中间出现一次非空应清零计数，实际 {eng._sync_empty_count}")
    # 再空一次仍然只是「第 1 次」⇒ 不能清账
    _sync(eng, [])
    assert _qty(eng) == 100, "计数被清零后再遇单次空返回，仍不得清账"
    assert eng._sync_empty_count == 1


# ------------------------------------------------ 3. 正向对照：正常同步不受影响

def test_normal_sync_still_updates():
    eng = _engine()
    _sync(eng, [{"code": CODE, "quantity": 200, "avg_cost": 190.0,
                 "market_value": 110000.0, "available": 200}])
    assert _qty(eng) == 200, (
        f"闸门不得拦截正常同步（正向对照），实际 quantity={_qty(eng)}")
    assert abs(eng._positions[CODE].last_price - 550.0) < 0.01, (
        f"盯市价应由 market_value/qty 得出 550.0，实际 {eng._positions[CODE].last_price}")


def test_partial_position_change_still_clears_disappeared_code():
    """非空返回里少了一个代码 ⇒ 属真实变化，应照常归零（闸门只管「整体空」）。"""
    eng = _engine()
    other = "510050.SH"
    eng._positions[other] = Position(code=other, name="50ETF", quantity=500,
                                     avg_cost=2.8, last_price=2.867)
    _sync(eng, [{"code": CODE, "quantity": 100, "avg_cost": 195.0,
                 "market_value": 54994.0, "available": 100}])
    assert _qty(eng, other) == 0, "券商明确返回非空但不含该代码 ⇒ 应归零"
    assert _qty(eng, CODE) == 100


# ------------------------------------------------ 4. 查询失败仍不动账本（旧语义）

def test_none_positions_leaves_ledger_untouched():
    eng = _engine()
    _sync(eng, None)
    assert _qty(eng) == 100, "查询失败(None)时不得改动本地账本"
    assert eng._sync_fail_count == 1
    assert eng._sync_empty_count == 0, (
        "查询失败不应计入「空返回确认」次数，否则两类故障会互相污染")


def test_confirm_threshold_is_configurable_and_documented():
    """阀值必须是类常量（可回滚置 1 恢复旧行为），不得散落魔法数。"""
    assert EE.EventEngine.SYNC_EMPTY_CONFIRM_N == 2, (
        f"当前应为 2，实际 {EE.EventEngine.SYNC_EMPTY_CONFIRM_N}")
    src = (ROOT / "engine" / "event_engine.py").read_text(encoding="utf-8")
    assert "SYNC_EMPTY_CONFIRM_N = 2" in src
    assert "回滚：置 1 即恢复旧行为" in src, "必须留下回滚说明"


if __name__ == "__main__":
    import traceback

    fns = [(k, v) for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    ok = fail = 0
    for name, fn in fns:
        try:
            fn()
            print(f"[PASS] {name}")
            ok += 1
        except Exception:
            print(f"[FAIL] {name}")
            traceback.print_exc()
            fail += 1
    print(f"\n{ok} passed, {fail} failed")
    sys.exit(1 if fail else 0)

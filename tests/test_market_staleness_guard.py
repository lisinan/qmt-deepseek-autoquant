# -*- coding: utf-8 -*-
"""行情陈旧守卫回归测试（2026-09-26 PM-EVOLVE R9 · 缺陷修复通道）。

背景（实盘铁证，储量测通道可信性）：
  2026-09-25 全天 ``xtdata.get_full_tick`` 返回的 ``300308.SZ`` 快照
  ``time = 2026-09-24 15:30:00``——陈旧 **47 小时**。而引擎对**快照降级路径**
  （``get_ticks`` 的 missing 分支）**完全没有新鲜度校验**：push 缓存路径有
  ``_pushed_at`` 校验，快照路径无条件接受。后果：
    · 09-25 全天 258 个权益快照总资产恒定 991,309.60（min == max）
    · 53 条 signals 的价格一字不差（300308 恒为 895.86）
    · 3 笔建仓（17.4 万元）全部按 09-24 15:30 的冻结价成交
    · 日志零告警 ⇒ 此后一切归因（方向 / 摩擦 / 隔夜暴露）都建立在假价格上

本文件锁死修复语义：行情新鲜时行为**逐位不变**（零回归），行情陈旧时才生效。
"""
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import core.qmt_client as qmt_client
from core.qmt_client import _XtdClient
from engine.event_engine import EventEngine


def _now_ms(offset_sec: float = 0.0) -> int:
    return int((time.time() - offset_sec) * 1000)


# ---------------------------------------------------------------- 客户端侧

def test_tick_age_parses_exchange_ms_timestamp():
    """交易所行情时间（毫秒戳）必须被正确换算成「年龄（秒）」。"""
    age = _XtdClient._tick_age_sec({"time": _now_ms(300)}, time.time())
    assert age is not None, "带 time 的 tick 必须能算出年龄"
    assert 299.0 <= age <= 303.0, f"年龄应约 300s，实际 {age}"


def test_tick_age_unknown_without_any_timestamp():
    """既无交易所时间也无推送时间 → 必须返回 None（无法判定），**不是** 0/新鲜。

    把「取不到时间」当成「新鲜」正是历史上静默用旧价的根源之一。
    """
    assert _XtdClient._tick_age_sec({}, time.time()) is None


def test_tick_age_falls_back_to_pushed_at():
    """没有交易所时间时，退回用推送到达时间判年龄。"""
    age = _XtdClient._tick_age_sec({"_pushed_at": time.time() - 45}, time.time())
    assert age is not None and 44.0 <= age <= 47.0, f"实际 {age}"


def test_normalize_flags_stale_snapshot():
    """陈旧快照必须被打上 _stale=True 并给出年龄；不得静默当新鲜价用。"""
    impl = _XtdClient.__new__(_XtdClient)
    age = _XtdClient.SNAPSHOT_STALE_SEC + 600          # 远超阈值
    out = impl._normalize("300308.SZ", {
        "time": _now_ms(age), "lastPrice": 895.86, "lastClose": 922.5,
    })
    assert out is not None
    assert out["_stale"] is True, "陈旧快照必须标记 _stale=True"
    assert out["stale_age_sec"] >= _XtdClient.SNAPSHOT_STALE_SEC


def test_normalize_keeps_fresh_snapshot_unflagged():
    """新鲜快照不得被标记（这是「零行为变化」的核心保证）。"""
    impl = _XtdClient.__new__(_XtdClient)
    out = impl._normalize("300308.SZ", {
        "time": _now_ms(2), "lastPrice": 895.86, "lastClose": 922.5,
    })
    assert out is not None
    assert out["_stale"] is False
    assert out["stale_age_sec"] < _XtdClient.SNAPSHOT_STALE_SEC


def test_snapshot_fallback_path_propagates_stale_flag():
    """★ 本缺陷的要害：get_full_tick 降级路径必须透传陈旧标记（原实现丢失）。

    构造：订阅推送缓存为空 ⇒ 全部走 missing → get_full_tick，该快照陈旧 2 小时。
    """
    class _FakeXtd:
        def get_full_tick(self, codes):
            return {c: {"time": _now_ms(7200), "lastPrice": 895.86,
                        "lastClose": 922.5} for c in codes}

    impl = _XtdClient.__new__(_XtdClient)
    impl.xtdata = _FakeXtd()
    impl._push_cache = {}
    impl._lock = threading.RLock()
    impl.subscribed = set()

    out = impl.get_ticks(["300308.SZ"])
    assert "300308.SZ" in out, "快照必须仍被返回（丢弃会让主循环空转，更糟）"
    assert out["300308.SZ"]["_stale"] is True, (
        "快照降级路径必须透传陈旧标记——这正是 09-25 静默用旧价的根因")


# ---------------------------------------------------------------- 引擎侧

def _bare_engine() -> EventEngine:
    """不触发 __init__（会连 xtdata / 起线程）的最小引擎对象。"""
    eng = EventEngine.__new__(EventEngine)
    eng._market_stale = False
    eng._market_stale_codes = []
    eng._market_stale_max_age = 0.0
    eng._last_market_stale_warn_ts = 0.0
    return eng


def test_engine_flags_global_stale_when_majority_stale():
    """多数标的陈旧 ⇒ 判定行情源整体失效。"""
    eng = _bare_engine()
    age = EventEngine.MARKET_STALE_SEC + 600
    raw = {f"C{i}": {"stale_age_sec": age, "lastPrice": 10.0} for i in range(10)}
    raw["FRESH"] = {"stale_age_sec": 1.0, "lastPrice": 10.0}
    eng._update_market_staleness(raw)
    assert eng._market_stale is True, "10/11 陈旧应判定整体失效"
    assert len(eng._market_stale_codes) == 10


def test_engine_not_stale_when_majority_fresh():
    """★ 零回归核心：行情新鲜时 _market_stale 恒为 False（与修复前逐位一致）。"""
    eng = _bare_engine()
    raw = {f"C{i}": {"stale_age_sec": 1.0, "lastPrice": 10.0} for i in range(10)}
    raw["OLD"] = {"stale_age_sec": EventEngine.MARKET_STALE_SEC + 600,
                  "lastPrice": 10.0}
    eng._update_market_staleness(raw)
    assert eng._market_stale is False, "仅个别陈旧不得判定整体失效"


def test_engine_treats_missing_age_as_unjudged():
    """数据源不提供行情时间（mock / 旧版 xtdata）→ 不计入分母，不误判失效。"""
    eng = _bare_engine()
    eng._update_market_staleness({f"C{i}": {"lastPrice": 10.0} for i in range(5)})
    assert eng._market_stale is False


def test_buy_blocked_when_market_stale():
    """★ 收益相关：行情失效时禁止建仓（用冻结价下单等于盲打）。"""
    eng = _bare_engine()
    eng._market_stale = True
    rejected = []
    eng._log_reject_once = lambda code, reason: rejected.append((code, reason))
    eng.risk = type("R", (), {"position_scale": 1.0})()
    eng._handle_buy(
        type("S", (), {"code": "300308.SZ"})(),
        type("T", (), {"price": 895.86})(), {})
    assert rejected, "行情陈旧时必须拒绝建仓并留下可审计的拒绝原因"
    assert "market_stale" in rejected[0][1]


def test_buy_not_blocked_when_market_fresh():
    """★ 零回归核心：行情新鲜时不得因本修复拒绝任何建仓。

    这里只验证「不会被 market_stale 这一条拦下」——price<=0 会正常早退，
    两种早退都不产生 market_stale 拒绝记录即为通过。
    """
    eng = _bare_engine()
    eng._market_stale = False
    rejected = []
    eng._log_reject_once = lambda code, reason: rejected.append((code, reason))
    eng.risk = type("R", (), {"position_scale": 1.0})()
    eng._handle_buy(
        type("S", (), {"code": "300308.SZ"})(),
        type("T", (), {"price": 0.0})(), {})      # 价格非法 → 正常早退
    assert all("market_stale" not in r[1] for r in rejected), (
        "行情新鲜时绝不许出现 market_stale 拒绝")


def test_constants_are_conservative():
    """阈值必须是「保守但可用」：120s 足以容忍休市/午休，又不放过隔日冻结。"""
    assert _XtdClient.SNAPSHOT_STALE_SEC == 120.0
    assert EventEngine.MARKET_STALE_SEC == 120.0
    assert 0.5 <= EventEngine.MARKET_STALE_RATIO <= 1.0


if __name__ == "__main__":
    for k, v in sorted(globals().items()):
        if k.startswith("test_"):
            v()
            print(f"  ok {k}")
    print("MARKET STALENESS GUARD TESTS PASSED")

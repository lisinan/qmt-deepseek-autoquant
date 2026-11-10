# -*- coding: utf-8 -*-
"""2026-11-10 落盘补丁 v2 的守卫测试（反向断言）。

★ 本仓测试不得使用任何 pytest fixture：``tests/run_all.py`` 是自研 runner，
  直接 ``fn()`` 无参调用，带形参一律 TypeError。

★ 每条都断言「缺陷复发时会红」，不是断言「现在没崩」。
★ 覆盖 v2 的四项：峰值基线单调化 + 重新武装阈值、peak 兜底 mode 过滤、
  未知态占位（登记/墙钟自解/先于 get_trades）、宽度闸门全量池口径。

注意：OWNER 指令单 SELL 丢 lots 那条已由 OWNER 自己用 `lots=` +
`_last_sell_placed` 修复，守卫在 `tests/test_fixed_lots.py`，本文件不重复。
"""
from __future__ import annotations

import io
import os
import sys
from datetime import date, timedelta
from types import SimpleNamespace

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

TRUE_PEAK = 67453.62


# ============================================================ 峰值基线
def _rm(halted_reason: str, peak: float = TRUE_PEAK):
    from risk.manager import RiskManager
    r = RiskManager()
    r._peak_asset = peak
    r._halted = True
    r._halt_reason = halted_reason
    r._halt_day = date.today() - timedelta(days=30)   # 确保冷却已过
    return r


def test_non_drawdown_halt_recovery_never_lowers_peak():
    """连亏 / 日亏类熔断恢复**不得**动峰值基线。

    实盘事故（risk_snapshots 实证，都在 09:30:1x）：
        11-03  66,542.31 → 63,207.94（−3,334.37）
        11-09  67,453.62 → 58,162.49（−9,291.13）
    ⇒ −20% 硬线实际要跌到真实峰值 −26.65% 才触发。
    """
    for reason in ("daily_loss_pct -3.10%", "consec_loss=5", "daily_loss_abs -5200"):
        r = _rm(reason)
        r.on_asset_update(60483.78, day_open_asset=60483.78)
        assert abs(r._peak_asset - TRUE_PEAK) < 1e-6, (
            f"{reason} 恢复后 peak 变成 {r._peak_asset:.2f}（应保持 {TRUE_PEAK}）"
            f" ⇒ −20% 硬线基线被侵蚀")


def test_drawdown_halt_recovery_keeps_peak_and_arms_floor():
    """回撤类熔断恢复：峰值不下移，改为武装「重新武装阈值」。"""
    r = _rm("max_drawdown -21.30%")
    r.on_asset_update(60483.78, day_open_asset=60483.78)
    assert abs(r._peak_asset - TRUE_PEAK) < 1e-6, (
        f"回撤类恢复把 peak 压到 {r._peak_asset:.2f} ⇒ 每次熔断都重开一份回撤预算")
    assert abs(r._dd_rearm_floor - 60483.78) < 1e-6, (
        f"未武装 rearm 阈值（{r._dd_rearm_floor}）⇒ 解除后同一 tick 会立刻再熔断")


def test_drawdown_recovery_does_not_freeze_buying_forever():
    """★ 反永久冻结：解除后在**同一权益水平**不得立刻再熔断。

    v1 的 `max(total, old_peak*0.9)` 实测反例：asset=40000 / peak=67453.62
    ⇒ peak 变 60,708.26、dd −34.11%，同一 tick 再 _halt 且 _halt_day 重置，
    每 3 日循环一次 ⇒ 买入永久冻结。v2 必须不再出现。
    """
    r = _rm("max_drawdown -40.70%")
    r.on_asset_update(40000.0, day_open_asset=40000.0)
    assert not r._halted, (
        "解除后同一 tick 又熔断了 ⇒ 买入永久冻结复发（v1 的 P0 缺陷）")
    # 再跌破解除水平才允许再次熔断
    r.on_asset_update(39999.0, day_open_asset=40000.0)
    assert r._halted, (
        "权益已跌破重新武装阈值却未熔断 ⇒ rearm 阈值没起作用")


def test_rearm_floor_survives_state_roundtrip():
    """重新武装阈值必须随 risk_state 持久化，否则重启后形同虚设。

    注意持久化用的是 ``export_state()``（引擎写 engine_state.risk_state 时调它，
    engine/event_engine.py:4347），不是 ``snapshot()``（后者只喂 risk_snapshots）。
    """
    r = _rm("max_drawdown -21.30%")
    r.on_asset_update(60483.78, day_open_asset=60483.78)
    st = r.export_state()
    assert "dd_rearm_floor" in st, "export_state 未包含 dd_rearm_floor ⇒ 重启后丢失"
    from risk.manager import RiskManager
    r2 = RiskManager()
    r2.load_state(st)
    assert abs(r2._dd_rearm_floor - 60483.78) < 1e-6, (
        f"load_state 未恢复 rearm 阈值（{r2._dd_rearm_floor}）")


def test_credible_peak_sql_filters_mode():
    """``_credible_peak_equity`` 必须按 mode 过滤（本库混有 paper 的 100 万轨迹）。"""
    src = io.open(os.path.join(ROOT, "engine", "event_engine.py"),
                  "r", encoding="utf-8").read()
    i = src.index("def _credible_peak_equity")
    seg = src[i:i + 1600]
    assert "WHERE mode=?" in seg, (
        "credible peak 的 SQL 未按 mode 过滤 ⇒ paper 的百万级轨迹会被当成 live 兜底")


# ============================================================ 未知态占位
def _stub():
    return SimpleNamespace(_pending_orders={}, _pending_sells={},
                           ORDER_TIMEOUT_SEC={"SELL": 30})


def test_unknown_order_engages_sell_dedup():
    """ok=True 但无 order_id ⇒ 必须登记占位并进 _pending_sells（去重闸才生效）。"""
    from engine.event_engine import EventEngine
    stub = _stub()
    key = EventEngine._register_unknown_order(
        stub, "300308.SZ", "SELL", 100, 474.36, 501.5679, "cash")
    assert key.startswith("UNK:300308.SZ:")
    assert stub._pending_sells.get("300308.SZ") == key, (
        "未知态未进 _pending_sells ⇒ 退出信号会无限重发")
    assert stub._pending_orders[key]["unknown"] is True
    assert int(stub._pending_orders[key]["qty"]) == 100
    assert stub._pending_orders[key]["filled"] == 0, (
        "占位不得预填 filled —— 伪造成交会污染日内亏损熔断")


def test_unknown_order_expiry_is_clock_driven():
    """占位到期必须按墙钟自解，且**不依赖** get_trades 是否返回 None。

    评审 P0：v1 把扫描放在 `if trades is None: return` 之后，桥 QUERY_TRADE 超时
    时 get_trades 返回 None ⇒ 占位永不释放 ⇒ 该 code 的去重闸永久占住
    ⇒ 连 force=True 强平都被拦。
    """
    from engine.event_engine import EventEngine
    stub = _stub()
    key = EventEngine._register_unknown_order(
        stub, "300308.SZ", "SELL", 100, 474.36, 501.5679, "cash")
    stub._pending_orders[key]["ts"] -= 10_000       # 假装已过期
    EventEngine._expire_unknown_orders(stub)
    assert key not in stub._pending_orders, "过期占位未被释放"
    assert "300308.SZ" not in stub._pending_sells, (
        "过期占位未释放去重闸 ⇒ 该 code 的退出路径被永久挡住")

    # 未过期的必须留着
    stub2 = _stub()
    k2 = EventEngine._register_unknown_order(
        stub2, "510050.SH", "SELL", 100, 2.9, 2.89, "cash")
    EventEngine._expire_unknown_orders(stub2)
    assert k2 in stub2._pending_orders, "未过期的占位被误释放"


def test_expiry_sweep_runs_before_get_trades():
    """静态断言：`_expire_unknown_orders()` 必须出现在 get_trades 之前。"""
    src = io.open(os.path.join(ROOT, "engine", "event_engine.py"),
                  "r", encoding="utf-8").read()
    i = src.index("def _reconcile_pending_orders")
    seg = src[i:i + 1200]
    i_sweep = seg.find("self._expire_unknown_orders()")
    i_trades = seg.find("trades = qmt_broker.get_trades")
    assert i_sweep >= 0, "_reconcile_pending_orders 未调用 _expire_unknown_orders"
    assert i_trades >= 0
    assert i_sweep < i_trades, (
        "过期扫描在 get_trades 之后 ⇒ get_trades 返回 None 时占位永不过期")


def test_unknown_order_is_not_a_daily_quota_guard():
    """占位必须是时间属性、会自解，不得是「同 code 每天 N 次」的计数上限。"""
    src = io.open(os.path.join(ROOT, "engine", "event_engine.py"),
                  "r", encoding="utf-8").read()
    i = src.index("def _register_unknown_order")
    seg = src[i:i + 2400]
    for banned in ("max_per_day", "_unknown_count", "unknown_quota"):
        assert banned not in seg, f"占位实现里出现了计数上限形状: {banned}"


# ============================================================ 宽度口径
def test_breadth_gate_counts_universe_pool_before_momentum_gate():
    """宽度判据必须是「动量截断前全量池」的计数（回测同口径）。"""
    src = io.open(os.path.join(ROOT, "engine", "event_engine.py"),
                  "r", encoding="utf-8").read()
    assert "pool_all = set(candidate_codes)" in src, "全量池快照缺失"
    i_pool = src.index("pool_all = set(candidate_codes)")
    i_gate = src.index("candidate_codes = self._apply_momentum_gate(candidate_codes)",
                       i_pool)
    assert i_pool < i_gate, "pool_all 必须在动量截断之前取值"
    assert "_breadth_n < _min_breadth" in src, "宽度闸门判据未切到 _breadth_n"
    assert "len(pending_buys) < _min_breadth" not in src, (
        "旧的截断口径判据仍在 ⇒ 口径背离复发")


def test_breadth_basis_default_and_contract_registration():
    """回滚开关默认 universe，且必须登记在「仅实盘」契约表。"""
    from config.settings import STRATEGY_PARAMS
    assert STRATEGY_PARAMS.get("breadth_gate_basis") == "universe", (
        "默认口径必须与回测一致；truncated 等于回退口径背离")
    import strategy._param_audit as PA
    assert "breadth_gate_basis" in PA.CONTRACT_LIVE_ONLY, "未在契约表登记 ⇒ 契约测试会红"

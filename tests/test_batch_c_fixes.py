# -*- coding: utf-8 -*-
"""批次 C 修复的回归测试（复盘正确性 / 测试隔离 / 数据库治理 / HTML 转义）。

覆盖：
  #8  复盘读回溯窗内 fills（原只读当天 → 跨日 round-trip 整笔丢失、
      holding_days 恒 0、隔夜仓 unrealized 虚高）
  #12 QMT_DB / QMT_NOTICES_LOG 环境变量隔离（原单测直写生产库与生产日志）
  #7  ts 索引 + prune 保留窗口（orders/fills 属账务证据，永不删）
      + HOLD 信号不入库 + 风控快照按变化写
  #13 报告 HTML 转义（LLM summary / notice msg 是不可控文本）
  附  _in_trading_window 覆盖 15:00–15:05 收盘竞价窗口
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.data_models import Signal                     # noqa: E402
from datetime import datetime                           # noqa: E402
from strategy import review_daily as R                   # noqa: E402
from storage.db import Storage, default_db_path          # noqa: E402
import core.notices as N                                 # noqa: E402
import engine.event_engine as EE                         # noqa: E402


def _fill(ts, code, side, qty, price, mode="paper"):
    return {"ts": ts, "code": code, "side": side, "quantity": qty,
            "price": price, "amount": qty * price, "mode": mode}


# ===================================== #8 跨日 round-trip 不再丢失

def test_match_trades_finds_cross_day_roundtrip():
    """昨天买、今天卖：旧实现（只喂当天 fills）会整笔丢弃，这里必须配上。"""
    fills = [
        _fill("2026-08-31T10:00:00", "300308.SZ", "BUY", 1000, 100.0),
        _fill("2026-09-01T14:00:00", "300308.SZ", "SELL", 1000, 110.0),
    ]
    trades = R._match_trades(fills, target="2026-09-01")
    assert len(trades) == 1, f"跨日 round-trip 应被配对，实际 {trades}"
    t = trades[0]
    assert t["holding_days"] == 1, f"持仓天数应为 1，实际 {t['holding_days']}"
    assert abs(t["entry_price"] - 100.0) < 1e-6
    assert abs(t["return_pct"] - 10.0) < 1e-6
    assert abs(t["pnl"] - 10000.0) < 1e-6


def test_match_trades_only_day_fills_loses_the_trade():
    """反向确认 bug 的存在：只喂当天 fills 时该笔交易必然消失。"""
    only_today = [_fill("2026-09-01T14:00:00", "300308.SZ", "SELL", 1000, 110.0)]
    assert R._match_trades(only_today, target="2026-09-01") == [], \
        "只读当天 fills 时卖出腿找不到建仓腿——这正是被修复的缺陷"


def test_match_trades_target_filter_excludes_other_days():
    fills = [
        _fill("2026-08-20T10:00:00", "300308.SZ", "BUY", 1000, 100.0),
        _fill("2026-08-25T10:00:00", "300308.SZ", "SELL", 1000, 105.0),  # 非目标日
        _fill("2026-08-28T10:00:00", "300502.SZ", "BUY", 500, 50.0),
        _fill("2026-09-01T10:00:00", "300502.SZ", "SELL", 500, 60.0),    # 目标日
    ]
    trades = R._match_trades(fills, target="2026-09-01")
    assert [t["code"] for t in trades] == ["300502.SZ"], \
        f"只应保留目标日平仓的 round-trip，实际 {trades}"
    assert trades[0]["holding_days"] == 4


# ===================================== #8 已实现/未实现口径

def test_replay_fills_realized_only_counts_target_day():
    fills = [
        _fill("2026-08-20T10:00:00", "300308.SZ", "BUY", 1000, 100.0),
        _fill("2026-08-25T10:00:00", "300308.SZ", "SELL", 1000, 105.0),  # 旧日实现
        _fill("2026-08-28T10:00:00", "300502.SZ", "BUY", 1000, 50.0),
        _fill("2026-09-01T10:00:00", "300502.SZ", "SELL", 1000, 60.0),   # 当日实现
    ]
    r = R._replay_fills(fills, target="2026-09-01")
    assert abs(r["realized_total"] - 10000.0) < 1e-6, \
        f"当日已实现应只含 300502 的 +10000，实际 {r['realized_total']}"
    assert "300308.SZ" not in r["realized_by_code"], "旧日实现不应计入当日"
    assert abs(r["sell_amount"] - 60000.0) < 1e-6, "卖出金额应只统计当日"
    assert r["buy_amount"] == 0.0, "当日无买入，买入金额应为 0"


def test_replay_fills_cost_basis_covers_overnight_position():
    """隔夜仓的成本基础必须算进 cost_basis，否则 unrealized 虚高。"""
    fills = [_fill("2026-08-20T10:00:00", "300308.SZ", "BUY", 1000, 100.0)]
    r = R._replay_fills(fills, target="2026-09-01")
    assert r["eod_positions"] == {"300308.SZ": 1000}, r["eod_positions"]
    assert abs(r["cost_basis"] - 100000.0) < 1e-6, \
        f"隔夜仓 cost_basis 应为 100000，实际 {r['cost_basis']}（旧实现为 0 → 未实现盈亏虚高）"
    assert r["realized_total"] == 0.0


def test_replay_fills_backward_compatible_without_target():
    """target=None 时退化为旧行为（全部计入），保证既有调用不被破坏。"""
    fills = [
        _fill("2026-08-20T10:00:00", "X", "BUY", 100, 10.0),
        _fill("2026-08-21T10:00:00", "X", "SELL", 100, 12.0),
    ]
    r = R._replay_fills(fills)
    assert abs(r["realized_total"] - 200.0) < 1e-6


# ===================================== 收盘竞价窗口

def test_in_trading_window_includes_close_auction():
    assert R._in_trading_window("2026-09-01T15:02:00"), \
        "15:00–15:05 收盘竞价窗口内的真熔断不应被排除（引擎守卫跑到 15:05）"
    assert R._in_trading_window("2026-09-01T09:20:00")
    assert not R._in_trading_window("2026-09-01T18:19:00"), "盘后不计入"
    assert not R._in_trading_window("2026-09-01T00:01:00"), "凌晨不计入"


# ===================================== #13 HTML 转义

def _minimal_rep():
    return {
        "fills_n": 1, "signals_n": 1, "orders_n": 1, "risk_snapshots_n": 0,
        "notices_n": 1, "fills_history_n": 1, "lookback_days": 120,
        "position_source": "fills重放", "position_warning": None,
        "mode_counts": {"PAPER": 1}, "order_mode_counts": {"PAPER": 1},
        "pnl": {"realized_total": 0.0, "realized_by_code": {},
                "eod_positions": {}, "eod_avg": {}, "cost_basis": 0.0,
                "buy_amount": 0.0, "sell_amount": 0.0},
        "trades": [], "equity": None, "equity_source": "n/a",
        "equity_series": [],
        "eod": {"total_asset": 0.0, "cash": 0.0, "market_value": 0.0,
                "unrealized": 0.0, "total_return_pct": 0.0},
        "risk": {"halt_events": [], "halt_count": 0, "halt_reasons": {},
                 "excluded_halt_count": 0, "max_consecutive_losses": 0,
                 "last_daily_pnl": None},
        "compare": {"flags": [], "distinct_codes": [],
                    "baseline_summary": {"return_pct": None}},
        "buy_signals": [], "ai_list": [], "notices_sample": [],
    }


def test_report_escapes_untrusted_text():
    rep = _minimal_rep()
    payload = '<script>alert("x")</script>'
    rep["notices_sample"] = [{"ts": "2026-09-01 10:00:00", "tag": "交易",
                              "level": "INFO", "msg": payload}]
    rep["ai_list"] = [{"ts": "t", "code": "c", "stance": "bullish",
                       "confidence": 0.9, "summary": payload}]
    rep["buy_signals"] = [{"ts": "t", "code": "c", "name": "n", "score": 1,
                           "price": 1, "reason": payload}]
    html_out = R._render_html("2026-09-01", rep)
    assert "<script>" not in html_out, \
        "未转义的 <script> 进入报告（LLM summary / notice msg 是不可控文本）"
    assert "&lt;script&gt;" in html_out, "应出现被转义后的文本"


def test_report_surfaces_position_warning():
    rep = _minimal_rep()
    rep["position_warning"] = "持仓只数对不上：算出 15 只，快照记录 5 只"
    html_out = R._render_html("2026-09-01", rep)
    assert "数据一致性告警" in html_out, \
        "持仓源不一致时必须在报告顶部显式告警，而不是静默输出错误数字"


# ===================================== #12 环境变量隔离

def test_notices_log_path_env_override():
    old = os.environ.get("QMT_NOTICES_LOG")
    try:
        with tempfile.TemporaryDirectory() as td:
            p = str(Path(td) / "n.log")
            os.environ["QMT_NOTICES_LOG"] = p
            assert str(N.notices_log_path()) == p
            N.system_notice("INFO", "测试", "隔离验证")
            assert Path(p).exists() and Path(p).stat().st_size > 0, \
                "system_notice 应写入被覆盖的路径"
    finally:
        if old is None:
            os.environ.pop("QMT_NOTICES_LOG", None)
        else:
            os.environ["QMT_NOTICES_LOG"] = old


def test_default_db_path_env_override():
    old = os.environ.get("QMT_DB")
    try:
        os.environ["QMT_DB"] = r"X:\tmp\override.db"
        assert str(default_db_path()) == r"X:\tmp\override.db"
        os.environ.pop("QMT_DB")
        assert default_db_path().name == "qmt.db", "未设置时回落默认路径"
    finally:
        if old is not None:
            os.environ["QMT_DB"] = old


# ===================================== #7 索引 / prune / 写入粒度

def test_ts_indexes_created():
    with tempfile.TemporaryDirectory() as td:
        st = Storage(Path(td) / "t.db")
        try:
            names = {r[0] for r in st._conn_get().execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
        finally:
            st.close()
    for idx in ("idx_signals_ts", "idx_fills_ts", "idx_risk_ts",
                "idx_equity_ts", "idx_sector_ts"):
        assert idx in names, f"缺少 {idx}（复盘的纯 ts 范围查询会退化为全表扫）"


def test_prune_never_deletes_orders_or_fills():
    from core.data_models import Fill, Order
    with tempfile.TemporaryDirectory() as td:
        st = Storage(Path(td) / "t.db")
        try:
            old = datetime(2000, 1, 1)
            st.save_order(Order(ts=old, code="X", side="BUY", quantity=100,
                                price=1.0))
            st.save_fill(Fill(ts=old, code="X", side="BUY", quantity=100,
                              price=1.0, amount=100.0))
            st.save_signal(Signal(ts=old, code="X", side="BUY"))
            st.save_risk_snapshot({"halted": False})   # ts=now，不该被删
            res = st.prune(keep_days=1)
            assert res["signals"] == 1, f"陈旧 signals 应被删除，实际 {res}"
            c = st._conn_get()
            assert c.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1, \
                "orders 是账务证据，永不删除"
            assert c.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1, \
                "fills 是账务证据，永不删除"
            assert c.execute(
                "SELECT COUNT(*) FROM risk_snapshots").fetchone()[0] == 1, \
                "保留窗口内的行不该被删"
        finally:
            st.close()


class _CountingStorage:
    def __init__(self):
        self.signals = []
        self.risk_snaps = 0

    def save_signal(self, s):
        self.signals.append(s)
        return 1

    def save_risk_snapshot(self, p):
        self.risk_snaps += 1
        return 1


def _engine():
    eng = EE.EventEngine(exec_mode="paper", auto_init_positions=False,
                         enable_sector_scorer=False,
                         enable_dynamic_universe=False,
                         enable_llm_reranker=False)
    eng.analyst = type("FakeAnalyst", (), {"enabled": False})()
    return eng


def test_hold_signals_are_not_persisted():
    eng = _engine()
    eng.storage = _CountingStorage()
    eng._save_signal(Signal(ts=datetime.now(), code="X", side="HOLD",
                            score=1.0, reason="score<4.0"))
    eng._save_signal(Signal(ts=datetime.now(), code="X", side="BUY", score=9.0))
    sides = [s.side for s in eng.storage.signals]
    assert sides == ["BUY"], \
        f"HOLD 不应入库（实测曾使 signals 表达 319 万行），实际 {sides}"


def test_risk_snapshot_written_on_change_but_throttled_when_identical():
    eng = _engine()
    eng.storage = _CountingStorage()
    eng._maybe_save_risk_snapshot()
    assert eng.storage.risk_snaps == 1, "首次必写"
    for _ in range(20):
        eng._maybe_save_risk_snapshot()
    assert eng.storage.risk_snaps == 1, \
        f"状态未变时应节流（原实现每 10 tick 无条件写），实际写了 {eng.storage.risk_snaps} 次"
    eng.risk._halt("test_change")
    eng._maybe_save_risk_snapshot()
    assert eng.storage.risk_snaps == 2, "状态变化（熔断）必须立刻写，不能被节流吞掉"


def test_engine_state_becomes_authoritative_position_source():
    """闭环验证：paper 引擎落盘 engine_state 后，复盘应改用它作权威持仓源。

    为何重要：历史数据里 fills 重放算出 10 只持仓、而当日末权益快照只有 5 只
    （旧引擎每次重启都用全新账本重新买入、从不卖出，且 engine_state 未落盘）。
    这类历史不一致无法追溯修复，但只要 engine_state 开始落盘，复盘就应
    自动摆脱对 fills 重放的依赖。本例验证这条链路真的通。
    """
    from core.data_models import Position
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "t.db"
        eng = EE.EventEngine(exec_mode="paper", auto_init_positions=False,
                             enable_sector_scorer=False,
                             enable_dynamic_universe=False,
                             enable_llm_reranker=False,
                             storage=Storage(db))
        try:
            eng.analyst = type("FakeAnalyst", (), {"enabled": False})()
            eng._cash = 100_000.0
            eng._positions = {"300308.SZ": Position(
                code="300308.SZ", name="中际旭创", quantity=1000,
                avg_cost=120.0, last_price=130.0)}
            eng._last_equity_ts = 0.0        # 解除 60s 节流
            eng._persist_equity(eng._total_asset())

            c = eng.storage._conn_get()
            assert c.execute(
                "SELECT COUNT(*) FROM engine_state").fetchone()[0] == 1, \
                "paper 模式下 _persist_equity 应落盘 engine_state"
            sp = R._state_positions(c)
            assert sp == {"300308.SZ": {"qty": 1000, "avg": 120.0}}, sp
            # 快照持仓只数与 engine_state 一致 → 不应再告警
            assert eng.storage._conn_get().execute(
                "SELECT positions_count FROM equity_snapshots "
                "ORDER BY id DESC LIMIT 1").fetchone()[0] == 1
        finally:
            eng.storage.close()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

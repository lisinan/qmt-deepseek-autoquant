# -*- coding: utf-8 -*-
"""
strategy/review_daily.py 纯函数回归测试（不触达数据库）。

覆盖：
  - _replay_fills：已实现盈亏 / EOD 净持仓（平均成本法）
  - _match_trades：FIFO 配对逐笔 round-trip
  - _equity_stats / _equity_svg：权益曲线统计与渲染（dict 与 tuple 双格式）
  - _analyze_risk：risk_snapshots + notices 双源熔断统计
"""
from __future__ import annotations

import json

from strategy import review_daily as R


def test_replay_fills_round_trip_zero_position():
    fills = [
        {"code": "300308", "side": "BUY", "quantity": 100,
         "price": 100.0, "amount": 10000.0, "ts": "2026-08-31T09:35:00"},
        {"code": "300308", "side": "SELL", "quantity": 100,
         "price": 110.0, "amount": 11000.0, "ts": "2026-08-31T14:50:00"},
    ]
    r = R._replay_fills(fills)
    assert r["realized_total"] == 1000.0, r
    assert r["eod_positions"] == {}, r  # 全平，EOD 无净持仓


def test_replay_fills_eod_position_and_avg():
    fills = [
        {"code": "300308", "side": "BUY", "quantity": 100,
         "price": 100.0, "amount": 10000.0, "ts": "2026-08-31T09:35:00"},
        {"code": "300308", "side": "BUY", "quantity": 100,
         "price": 120.0, "amount": 12000.0, "ts": "2026-08-31T10:35:00"},
    ]
    r = R._replay_fills(fills)
    assert r["eod_positions"]["300308"] == 200
    assert abs(r["eod_avg"]["300308"] - 110.0) < 1e-6, r
    assert r["cost_basis"] == 22000.0, r


def test_match_trades_fifo_one_roundtrip():
    fills = [
        {"code": "300308", "side": "BUY", "quantity": 100,
         "price": 100.0, "amount": 10000.0, "ts": "2026-08-31T09:35:00"},
        {"code": "300308", "side": "SELL", "quantity": 100,
         "price": 110.0, "amount": 11000.0, "ts": "2026-08-31T14:50:00"},
    ]
    trades = R._match_trades(fills)
    assert len(trades) == 1, trades
    t = trades[0]
    assert t["entry_price"] == 100.0
    assert t["exit_price"] == 110.0
    assert t["pnl"] == 1000.0
    assert abs(t["return_pct"] - 10.0) < 1e-6, t


def test_match_trades_fifo_partial_close():
    # 先买 200，再卖 100 → 1 笔 round-trip + 100 股仍持仓（无第二笔）
    fills = [
        {"code": "300308", "side": "BUY", "quantity": 200,
         "price": 100.0, "amount": 20000.0, "ts": "2026-08-31T09:35:00"},
        {"code": "300308", "side": "SELL", "quantity": 100,
         "price": 110.0, "amount": 11000.0, "ts": "2026-08-31T14:50:00"},
    ]
    trades = R._match_trades(fills)
    assert len(trades) == 1, trades
    assert trades[0]["qty"] == 100
    assert trades[0]["pnl"] == 1000.0


def test_equity_stats_daily_return():
    series = [("2026-08-31T09:30:00", 1000000.0, 0.0),
              ("2026-08-31T15:00:00", 1027000.0, 0.0)]
    s = R._equity_stats(series)
    assert s is not None
    assert s["start"] == 1000000.0
    assert s["end"] == 1027000.0
    assert abs(s["daily_return_pct"] - 2.7) < 1e-6, s
    assert s["points"] == 2


def test_equity_svg_tuple_and_dict_formats():
    # tuple 格式（equity_snapshots 路径）
    svg_t = R._equity_svg([("t1", 100.0, 50.0), ("t2", 110.0, 40.0)])
    assert "<polyline" in svg_t, "tuple 格式应渲染折线"
    # dict 格式（rep['equity_series'] 路径 —— 修复 KeyError:1 的来源）
    svg_d = R._equity_svg([{"ts": "t1", "total_asset": 100.0, "cash": 50.0},
                           {"ts": "t2", "total_asset": 110.0, "cash": 40.0}])
    assert "<polyline" in svg_d, "dict 格式应渲染折线（不可再 KeyError）"
    # 数据点不足 → 占位提示
    svg_short = R._equity_svg([{"ts": "t1", "total_asset": 100.0}])
    assert "不足" in svg_short


def test_analyze_risk_notice_source():
    # 仅 notices 含熔断（快照未捕获）——双源必须补记
    notices = [{"tag": "风控", "msg": "触发熔断: consec_loss=5",
                "ts": "2026-08-31T10:00:00"}]
    res = R._analyze_risk([], notices)
    assert res["halt_count"] == 1, res
    assert "consec_loss=5" in res["halt_reasons"], res


def test_analyze_risk_snapshot_source():
    snaps = [{"ts": "2026-08-31T11:00:00",
              "payload_json": json.dumps(
                  {"halted": True, "halt_reason": "max_drawdown -20.00%",
                   "consecutive_losses": 0})}]
    res = R._analyze_risk(snaps, [])
    assert res["halt_count"] == 1, res
    assert "max_drawdown -20.00%" in res["halt_reasons"], res


def test_analyze_risk_dual_source_merge():
    snaps = [{"ts": "2026-08-31T11:00:00",
              "payload_json": json.dumps(
                  {"halted": True, "halt_reason": "max_drawdown -11.00%"})}]
    notices = [{"tag": "风控", "msg": "触发熔断: consec_loss=5",
                "ts": "2026-08-31T10:00:00"}]
    res = R._analyze_risk(snaps, notices)
    assert res["halt_count"] == 2, res
    assert res["halt_reasons"].get("max_drawdown -11.00%") == 1
    assert res["halt_reasons"].get("consec_loss=5") == 1


def test_compare_to_baseline_reads_full_key():
    # verify_live_quality.json 真实 schema：full.{total_return,sharpe,
    # max_drawdown,alpha}(ratio) + 顶层 folds_mean_sharpe / folds_pos_alpha
    baseline = {
        "full": {"total_return": 2.936, "sharpe": 1.599,
                 "max_drawdown": -0.1985, "alpha": 0.0487},
        "folds_mean_sharpe": 1.692,
        "folds_pos_alpha": 5,
    }
    risk = {"halt_count": 0, "halt_events": []}
    fills = [{"code": "300308.SZ"}]
    cmp_ = R._compare_to_baseline("2026-08-31", fills, risk, baseline)
    bs = cmp_["baseline_summary"]
    # ratio → 百分比 映射必须正确（旧实现因读错 key 永远返回 null）
    assert bs["return_pct"] == 293.6, bs
    assert abs(bs["sharpe"] - 1.6) < 0.01, bs
    assert bs["max_dd_pct"] == -19.85, bs
    assert bs["alpha_pt"] == 4.87, bs
    assert bs["folds_sharpe"] == 1.69
    assert bs["folds_pos_alpha"] == 5


def test_analyze_risk_filters_afterhours():
    # 00:01 / 18:49 属测试桩+收盘后，应剔除；仅 10:14（真实交易时段）计入
    notices = [
        {"tag": "风控", "msg": "触发熔断: consec_loss=5", "ts": "2026-08-31 00:01:52"},
        {"tag": "风控", "msg": "触发熔断: max_drawdown -20.00%", "ts": "2026-08-31 10:14:00"},
        {"tag": "风控", "msg": "触发熔断: consec_loss=5", "ts": "2026-08-31 18:49:39"},
    ]
    res = R._analyze_risk([], notices)
    assert res["halt_count"] == 1, res          # 仅 10:14 计入
    assert res["excluded_halt_count"] == 2, res  # 00:01 + 18:49 剔除
    # 计入的是 10:14 的 max_drawdown -20.00%；consec_loss=5 两个均在时段外被剔除
    assert res["halt_reasons"].get("max_drawdown -20.00%") == 1
    assert "consec_loss=5" not in res["halt_reasons"]


def test_analyze_risk_snapshot_filters_afterhours():
    snaps = [
        {"ts": "2026-08-31T00:01:52",
         "payload_json": json.dumps({"halted": True, "halt_reason": "consec_loss=5"})},
        {"ts": "2026-08-31T10:30:00",
         "payload_json": json.dumps({"halted": True, "halt_reason": "max_drawdown -11.00%"})},
    ]
    res = R._analyze_risk(snaps, [])
    assert res["halt_count"] == 1, res
    assert res["excluded_halt_count"] == 1, res


def test_analyze_risk_same_event_snapshot_and_notice_dedup():
    # 真实 Bug 回归：引擎 09:15:38 触发一次熔断后持续 halted 一整天。
    # snapshot 上升沿 + notice 各记一次会被算成 2 次 —— 必须是 1 次。
    snaps = [
        {"ts": "2026-09-16T09:15:38.407699",
         "payload_json": json.dumps(
             {"halted": True, "halt_reason": "consec_loss=5", "consecutive_losses": 5})},
        {"ts": "2026-09-16T10:00:01.785358",
         "payload_json": json.dumps(
             {"halted": True, "halt_reason": "consec_loss=5", "consecutive_losses": 8})},
    ]
    # notice 时间戳用空格格式（notices.log 实际格式），与 snapshot 的 T 格式不同，
    # 去重必须能归一化二者。
    notices = [{"tag": "风控",
                "msg": "触发熔断: consec_loss=5（已暂停新开仓，冷却 1 日后自动恢复或手动 resume）",
                "ts": "2026-09-16 09:15:38"}]
    res = R._analyze_risk(snaps, notices)
    assert res["halt_count"] == 1, res          # ← 修复点：双源重复计数
    assert res["halt_reasons"].get("consec_loss=5") == 1, res  # ← 修复点：不被 54 个快照累加


def test_analyze_risk_persisted_halted_not_double_counted():
    # 54 个持续 halted 快照（同一事件）只能算 1 次，reasons_counter 也只能是 1。
    snaps = [{"ts": f"2026-09-16T09:15:38.{i:06d}",
              "payload_json": json.dumps(
                  {"halted": True, "halt_reason": "consec_loss=5", "consecutive_losses": 5})}
             for i in range(54)]
    res = R._analyze_risk(snaps, [])
    assert res["halt_count"] == 1, res
    assert res["halt_reasons"] == {"consec_loss=5": 1}, res
    # 连亏峰值仍正确透传（不依赖计数逻辑）
    assert res["max_consecutive_losses"] == 5, res


def test_clean_fills_excludes_dirty_sample():
    # 默认截止 2026-09-01：2026-08-25 多进程遗留脏样本必须被剔除，
    # 09-02 起的真实成交保留。
    fills = [
        {"ts": "2026-08-25T10:14:01", "code": "X", "side": "BUY",
         "quantity": 100, "price": 1.0, "amount": 100.0},
        {"ts": "2026-09-02T10:14:01", "code": "Y", "side": "BUY",
         "quantity": 100, "price": 10.0, "amount": 1000.0},
        {"ts": "2026-09-15T14:00:00", "code": "Y", "side": "SELL",
         "quantity": 100, "price": 11.0, "amount": 1100.0},
    ]
    cleaned = R._clean_fills(fills)
    assert len(cleaned) == 2, cleaned
    assert all(str(f["ts"]).split("T")[0] >= "2026-09-01" for f in cleaned), cleaned
    # 环境变量可覆盖截止日
    import os as _os
    _os.environ["REVIEW_DIRTY_CUTOFF"] = "2026-09-10"
    try:
        cleaned2 = R._clean_fills(fills)
        assert len(cleaned2) == 1 and cleaned2[0]["ts"].startswith("2026-09-15"), cleaned2
    finally:
        _os.environ.pop("REVIEW_DIRTY_CUTOFF", None)


def test_state_positions_semantics():
    # 2026-09-16 修复：state_pos 返回语义必须区分「无引擎行」与「引擎落盘空仓」，
    # 否则收盘清仓(positions=[]) 会被误报为若干只残留持仓。
    import sqlite3 as _sq
    def _mem():
        c = _sq.connect(":memory:")
        c.row_factory = _sq.Row
        return c
    # 场景1：无 engine_state 行 → None（调用方回退 fills 重放）
    c1 = _mem()
    assert R._state_positions(c1) is None
    # 场景2：空仓（positions 为空数组）→ {}（权威空仓，*不得*回退 fills）
    c2 = _mem()
    c2.execute("CREATE TABLE engine_state(id INTEGER PRIMARY KEY, positions TEXT)")
    c2.execute("INSERT INTO engine_state(id,positions) VALUES(1,'[]')")
    assert R._state_positions(c2) == {}, "收盘空仓必须返回 {} 而非回退 fills"
    # 场景3：有持仓 → dict（code -> {qty, avg}）
    c3 = _mem()
    c3.execute("CREATE TABLE engine_state(id INTEGER PRIMARY KEY, positions TEXT)")
    c3.execute("INSERT INTO engine_state(id,positions) VALUES("
               "1,'[{\"code\":\"300308.SZ\",\"quantity\":100,\"avg_cost\":50.0}]')")
    sp = R._state_positions(c3)
    assert sp == {"300308.SZ": {"qty": 100, "avg": 50.0}}, sp


def test_account_daily_crossday():
    # 账户真实当日收益必须含隔夜重估（跨日口径），而非只看日内涨跌。
    # 模拟 9-16：前一日末 961286 → 当日末 803680 = -16.4%（隔夜缺口）。
    import sqlite3 as _sq
    c = _sq.connect(":memory:")
    c.row_factory = _sq.Row
    c.execute("CREATE TABLE equity_snapshots(ts TEXT, total_asset REAL, mode TEXT)")
    c.execute("INSERT INTO equity_snapshots VALUES('2026-09-15T15:04:52','961286.0','paper')")
    c.execute("INSERT INTO equity_snapshots VALUES('2026-09-16T09:30:00','805667.0','paper')")
    c.execute("INSERT INTO equity_snapshots VALUES('2026-09-16T15:04:42','803680.18','paper')")
    d = R._account_daily(c, "2026-09-16")
    assert d["prev_asset"] == 961286.0, d
    assert d["eod_asset"] == 803680.18, d
    assert abs(d["daily_ret_pct"] - (-16.4)) < 0.1, d   # 含隔夜重估
    assert abs(d["daily_pnl"] - (-157605.82)) < 1.0, d


def test_optimization_accuracy_lowered_by_real_account_loss():
    # 2026-09-16 修复：账户真实在亏钱时，「准确」维度必须下降并给出 P0 finding，
    # 不能再虚假 100 分（旧逻辑只看滑点/信号零成交）。
    rep = {
        "position_warning": None,
        "account_ret_pct": -16.4,
        "account_pnl": -157605.82,
        "account_daily": {"prev_asset": 961286.0, "eod_asset": 803680.18,
                          "daily_pnl": -157605.82, "daily_ret_pct": -16.4},
        "eod": {"total_return_pct": -0.25},
        "account_state": {},
        "risk": {"halt_count": 1, "halt_reasons": {"consec_loss=5": 1},
                 "max_consecutive_losses": 9, "last_daily_pnl": -188766},
        "equity": {"intraday_max_dd_pct": -0.87, "points": 262, "daily_return_pct": -0.25},
        "pnl": {"eod_positions": {}, "realized_total": -189441.52},
        "mode_counts": {}, "order_mode_counts": {},
    }
    opt = R._optimization_insights(rep, [], [], [])
    assert opt["scores"]["准确"] < 100, opt["scores"]          # 必须拉低
    assert opt["scores"]["准确"] <= 55, opt["scores"]          # 亏16.4%→约55
    found = [f for f in opt["findings"] if f["axis"] == "准确" and f["priority"] == "P0"]
    assert found, opt["findings"]
    assert "账户真实当日亏损" in found[0]["title"], found[0]

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

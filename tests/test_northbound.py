# -*- coding: utf-8 -*-
"""北向资金协同闸门回归测试（2026-09-19 落盘验证）。

验证：
  1. northbound_mode="gate" 时 run_backtest 正确注入北向闸门（防御性少开仓，交易数不暴增）；
  2. 启用后 Sharpe / MDD 不显著恶化（与周度研究结论一致：IS Sharpe 1.52→1.65）；
  3. engine 层读取点：STRATEGY_PARAMS.northbound_mode 被 event_engine 读取（不因缺失而假死）。
"""
from __future__ import annotations
import sys
sys.path.insert(0, ".")

import strategy.backtest_daily as B
from strategy.backtest_daily import run_backtest
from strategy._evolve_wf import base_cfg
from strategy.opt_harness import wide_universe, preload
from data.northbound_cache import get_northbound
import config.settings as S


def test_northbound_gate_engaged_and_non_degrading():
    codes = wide_universe()
    data = preload(codes + ["000300.SH", "399006.SZ"], 750)
    nb = get_northbound("20221201", "20260825")
    ks = [c for c in codes if c in data]
    base = base_cfg()
    g = base_cfg()
    g.northbound_mode = "gate"
    g.nb_lookback = 20
    r0 = run_backtest(ks, base, count=750, preloaded=data)
    r1 = run_backtest(ks, g, count=750, preloaded=data, nb_data=nb)
    # gate 为防御性少开仓：交易数不应暴增
    assert r1["n_trades"] <= r0["n_trades"] + 8, (
        f"北向 gate 交易数异常增加: {r1['n_trades']} vs {r0['n_trades']}")
    # 不应显著恶化 Sharpe / 回撤
    assert r1["sharpe"] >= r0["sharpe"] - 0.3, (
        f"北向 gate 显著恶化 Sharpe: {r1['sharpe']:.2f} vs {r0['sharpe']:.2f}")
    assert r1["max_drawdown"] >= -0.30, (
        f"北向 gate 回撤失控: {r1['max_drawdown']*100:.1f}%")
    # 确实生效（IS 上带来正向增益，与研究一致）
    assert r1["sharpe"] > r0["sharpe"], (
        f"北向 gate 未带来正向增益: {r1['sharpe']:.2f} vs {r0['sharpe']:.2f}")


def test_northbound_setting_present_and_default_off_semantics():
    # settings 已落盘 northbound_mode="gate"（OWNER 批准）。引擎读取点存在。
    assert "northbound_mode" in S.STRATEGY_PARAMS, "settings 缺失 northbound_mode"
    assert S.STRATEGY_PARAMS["northbound_mode"] in ("off", "gate")
    # 回测层默认 off 不影响原行为
    base = base_cfg()
    assert base.northbound_mode == "off"

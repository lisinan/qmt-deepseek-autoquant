# -*- coding: utf-8 -*-
"""北向资金协同闸门回归测试（2026-09-19 落盘验证）。

验证：
  1. northbound_mode="gate" 时 run_backtest 正确注入北向闸门（防御性少开仓，交易数不暴增）；
  2. 启用后 Sharpe / MDD 不显著恶化（与周度研究结论一致：IS Sharpe 1.52→1.65）；
  3. engine 层读取点：STRATEGY_PARAMS.northbound_mode 被 event_engine 读取（不因缺失而假死）。
"""
from __future__ import annotations
import sys
from dataclasses import replace
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
    # 【2026-09-28 第 10 轮】base_cfg 已与生产同步为 northbound_mode="gate"
    # （否则 P0 基线不含北向闸门 ⇒ OOS 均值 Sharpe 少算 +0.207）。
    # 本用例的语义是「gate vs off 的对照」，故对照侧必须**显式注入** off，
    # 不能再依赖 base_cfg 的默认值为 off。
    g = base_cfg()
    assert g.northbound_mode == "gate", "base_cfg 应与生产一致为 gate"
    base = replace(g, northbound_mode="off")
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
    # 【2026-09-28 第 10 轮 AM-EVOLVE】验证器 base_cfg 已与生产同步为 "gate"。
    #   此前此处断言 base_cfg 为 "off"，导致 P0 基线不含北向闸门、系统性低估
    #   生产（OOS 均值 Sharpe 少算 +0.207）。现断言二者一致，防止基线再次漂移。
    #   完整守卫见 tests/test_evolve_baseline_sync.py。
    base = base_cfg()
    assert base.northbound_mode == S.STRATEGY_PARAMS["northbound_mode"], (
        f"验证器 base_cfg 的 northbound_mode={base.northbound_mode!r} 与生产 "
        f"{S.STRATEGY_PARAMS['northbound_mode']!r} 不一致 —— P0 基线漂移会让所有"
        f" dSh 增量失真（2026-09-18 / 2026-09-28 各踩一次）")
    assert int(base.nb_lookback) == int(S.STRATEGY_PARAMS["nb_lookback"])

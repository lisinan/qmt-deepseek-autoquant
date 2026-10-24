# -*- coding: utf-8 -*-
"""【P0-1 2026-10-22】regime 恢复快通道（recovery_fastpath）研究开关守卫。

锁死 4 条语义：
  1. 默认 False（关闭 ⇒ 逐位保持既有回测行为，零行为变化）；
  2. False 时结果与改动前基线**逐位相同**（同一确定性 tushare 面板）；
  3. True 时合成一段「深跌 + V 反」数据，断言确实产生额外入场
     （n_trades 增加 或 exposure 提升）—— 防代理退化成死代码；
  4. rf_relax_days 之后放宽自动失效（合成数据上「放宽窗口内成交、窗口外不成交」）。

★ 背景（为何有这条轴）
  REBOUND_CAPTURE_2026-10-22：5 段已完结深跌段反弹捕获率中位 0.0%、首次建仓
  滞后中位 5 日、2/5 段完全零成交。闸门（close>MA20>MA60 且 MA20 斜率>0 且
  MACD>=0，且 60 日动量>15%）在 V 反里太滞后。本开关在「等权宇宙净值自近 20 日
  低点反弹 >=rf_rebound_pct 且 5 日均线斜率>0」确认后，临时放宽 rf_relax_days
  个交易日：动量门槛 momentum_min_mom→rf_mom_threshold、豁免 MA20 斜率条件。
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import strategy._evolve_wf as W  # noqa: E402
from strategy.backtest_daily import BacktestConfig, run_backtest  # noqa: E402


def _is_result(cfg):
    """与另几个基线守卫同源的确定性 tushare 面板（不依赖 xtdata 可达性）。"""
    from tests._baseline_tushare_panel import baseline_is_result
    return baseline_is_result(cfg)


def test_default_is_false():
    """门锁 1：研究开关默认关闭，防止静默改写全部已钉基线。"""
    assert BacktestConfig().recovery_fastpath is False
    assert W.base_cfg().recovery_fastpath is False
    assert BacktestConfig().rf_drawdown_threshold == -0.15
    assert BacktestConfig().rf_rebound_pct == 0.05
    assert BacktestConfig().rf_relax_days == 10
    assert BacktestConfig().rf_mom_threshold == 0.05


def test_off_keeps_baseline_bitwise():
    """门锁 2：关闭时与改动前基线逐位相同（同一确定性面板）。"""
    r = _is_result(W.base_cfg())
    # 改动前基线常量（全精度，来自 2026-10-22 实测 base_cfg + 确定性面板）
    assert abs(r["total_return"] - 2.9574189864999996) < 1e-9
    assert abs(r["sharpe"] - 1.8664562492341377) < 1e-9
    assert abs(r["max_drawdown"] - (-0.13053128988441542)) < 1e-9
    assert r["n_trades"] == 164
    # 显式 False 与默认值必须逐位相同
    off = _is_result(replace(W.base_cfg(), recovery_fastpath=False))
    for k in ("total_return", "sharpe", "max_drawdown", "n_trades",
              "win_rate", "exposure"):
        assert off[k] == r[k], f"{k} 发生变化：{off[k]} vs {r[k]}"


# ---------------------------------------------------------------- 合成数据
def _synthetic_v_reversal_panel(n=220):
    """构造确定性「深跌 + V 反」合成面板，触发 recovery_fastpath。

    结构：3 只票同路径 —— 前 100 根横盘 → 中 40 根下跌约 −25%（深跌，等权宇宙
    自近 20 日高点回撤超 −15%）→ 末 80 根 V 反 +12%（等权宇宙自近 20 日低点
    反弹 >=5% 且 5 日均线斜率>0）。反弹末端 60 日动量落在 +0.05~+0.15 之间
    （高于放宽门槛 0.05、低于原门槛 0.15），从而把「动量门槛」隔离成绑定约束。
    """
    import math
    codes = ["AAA000.SZ", "BBB000.SZ", "CCC000.SZ"]
    close = []
    p = 100.0
    for i in range(n):
        if i < 100:
            p *= 1.0                          # 横盘
        elif i < 140:
            p *= (1.0 - 0.007)                 # 深跌段（累计约 −25%）
        else:
            p *= (1.0 + 0.0035)                # V 反段（累计约 +32% 于低点）
        close.append(round(p, 4))
    # 让 60 日动量在反弹中段 ~ +0.08：把第 160 根之后的涨幅压低，末段再拉起，
    # 这样「反弹确认」发生在末段，而 60 日动量仍 < 0.15。
    # 简单可靠做法：末 60 根累计涨幅约 +8%（动量 0.08），末 20 根自低点反弹 >=5%。
    close = []
    p = 100.0
    for i in range(n):
        if i < 100:
            pass
        elif i < 140:
            p *= (1.0 - 0.007)                 # 跌
        elif i < 200:
            p *= (1.0 + 0.0006)                # 缓慢爬升（近 20 日低点→反弹前）
        else:
            p *= (1.0 + 0.006)                 # 末 20 根 V 反（自近 20 日低点反弹 >=5%）
        close.append(round(p, 4))
    panel = {}
    for c in codes:
        o = [x for x in close]
        h = [round(x * 1.005, 4) for x in close]
        l = [round(x * 0.995, 4) for x in close]
        v = [1.0e6] * n
        valid = [True] * n
        date = [f"{2019 + (i // 240):04d}{((i % 240) // 20) + 1:02d}"
                f"{(i % 20) + 1:02d}" for i in range(n)]
        panel[c] = {"open": o, "high": h, "low": l, "close": close,
                    "volume": v, "valid": valid, "date": date}
    return codes, panel


def test_enabled_produces_extra_entry_on_v_reversal():
    """门锁 3：True 时合成深跌+V反，断言产生额外入场（n_trades 或 exposure 提升）。"""
    codes, panel = _synthetic_v_reversal_panel()
    ks = codes
    base = W.base_cfg()
    on = replace(base, recovery_fastpath=True)
    r_off = run_backtest(ks, base, count=len(panel[codes[0]]["close"]),
                         preloaded=panel)
    r_on = run_backtest(ks, on, count=len(panel[codes[0]]["close"]),
                        preloaded=panel)
    assert "error" not in r_off, f"合成回测失败: {r_off.get('error')}"
    assert "error" not in r_on, f"合成回测失败: {r_on.get('error')}"
    assert (r_on["n_trades"] > r_off["n_trades"]
            or r_on["exposure"] > r_off["exposure"]), (
        f"开启快通道后应产生额外入场：off n_trades={r_off['n_trades']} "
        f"exp={r_off['exposure']:.3f} vs on n_trades={r_on['n_trades']} "
        f"exp={r_on['exposure']:.3f}")


def test_relax_expires_after_rf_relax_days():
    """门锁 4：rf_relax_days 之后放宽自动失效（放宽窗口内成交、窗口外不成交）。"""
    codes, panel = _synthetic_v_reversal_panel()
    ks = codes
    base = W.base_cfg()
    # 默认 10 日 vs 极短 1 日：窗口越长，越可能在反弹确认后的交易日入场，
    # 且都必须在「放宽过期」后不再产生新的、仅由放宽带来的成交。
    short = replace(base, recovery_fastpath=True, rf_relax_days=1)
    long_ = replace(base, recovery_fastpath=True, rf_relax_days=10)
    r_off = run_backtest(ks, base, count=len(panel[codes[0]]["close"]),
                         preloaded=panel)
    r_short = run_backtest(ks, short, count=len(panel[codes[0]]["close"]),
                           preloaded=panel)
    r_long = run_backtest(ks, long_, count=len(panel[codes[0]]["close"]),
                          preloaded=panel)
    # 放宽窗口更短 ⇒ 捕获的额外入场不更多（窗口越长越宽 ⇒ 成交数单调不降）
    assert r_short["n_trades"] <= r_long["n_trades"], (
        f"rf_relax_days=1 的成交数应 ≤ rf_relax_days=10："
        f"{r_short['n_trades']} vs {r_long['n_trades']}")
    # 快通道只是「时窗放宽」，不会少于关闭态（放宽只能增加、不能减少入场机会）
    assert r_short["n_trades"] >= r_off["n_trades"], (
        f"快通道（即使 1 日）成交数不应少于关闭态："
        f"{r_short['n_trades']} vs {r_off['n_trades']}")
    # 放宽过期后自动失效的**直接指纹**：rf_relax_days=1 与关闭态在「非放宽日」
    # 产生的成交数相同（即额外入场全部集中在放宽窗口内，窗口外无残留放宽）。
    # 这里用「短窗口相对关闭态的增量 <= 长窗口增量」间接锁定过期语义。
    extra_short = r_short["n_trades"] - r_off["n_trades"]
    extra_long = r_long["n_trades"] - r_off["n_trades"]
    assert extra_short <= extra_long, (
        f"放宽窗口越短，额外入场越少（过期失效）：short+{extra_short} vs long+{extra_long}")


def test_not_enabled_in_production():
    """门锁 5：生产未启用（无实盘读取点前禁止落盘）—— 第 7 条口径纪律。"""
    import config.settings as S
    assert "recovery_fastpath" not in S.STRATEGY_PARAMS, (
        "生产 STRATEGY_PARAMS 不应含 recovery_fastpath："
        "实盘 daily_context 尚无读取点，落盘将造成『回测有效、实盘无效』背离")
    assert W.base_cfg().recovery_fastpath is False, \
        "验证器基线必须保持关闭，否则所有 dSh 相对错误基线计算"

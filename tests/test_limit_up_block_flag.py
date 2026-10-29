# -*- coding: utf-8 -*-
"""【2026-10-21 B9】涨跌停不可成交（limit_up_block）守卫。

## 为什么有这条轴

评审报告 §3.1-P1（``reports/OPTIMIZATION_REVIEW_2026-10-21_multimodel.md``）认定：
回测 ``run_backtest`` 让买单在次日 ``open`` 无条件成交，唯一拦截是
``panel[code]["valid"]``（=停牌）。而策略语义是「买 60 日动量最强、创 20 日新高的票」，
这类票信号次日开盘常一字/大幅涨停，实盘买不到；离场侧在跌停封死时同样卖不出。
回测因此在最关键的极端场景**双向美化**（买在最想买却买不到的票上、卖在卖不出的
跌停板上）。

本开关（``BacktestConfig.limit_up_block``）补上该摩擦：开启后，
  * 买单：若成交日 ``open`` 或 ``high`` 触及涨停价（前一收盘 ×(1+limit) 四舍五入
    到 0.01）⇒ 视为不可成交，**放弃**该笔（见 docs/Spec_B9_backtest_realism.md
    的「放弃 vs 顺延」理由）；
  * 卖单：若跌停封死（当日 ``low`` 触及跌停价）⇒ **顺延**到下一交易日（持仓保留，
    次日 B 段重新判定）。
涨停价/跌停价用 panel 里已有的 ``close`` 序列自算前收，**不新增任何数据依赖**；
停牌（``valid=False``）逻辑保持不变。

## 本文件锁死的语义（R17 守则：默认 off 零行为变化 + 开启后真变）
  1. 默认值必须是 False（误改成 True 会静默改写全部已钉基线）
  2. ``=False`` ⇒ 结果**逐位等同** ``base_cfg()``（零行为变化）
  3. ``=True`` ⇒ ``limit_blocked_buy`` / ``limit_blocked_sell`` 真变（防代理死代码）
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import strategy._evolve_wf as W  # noqa: E402
from strategy.backtest_daily import (  # noqa: E402
    BacktestConfig, run_backtest,
    _limit_pct_for, _limit_price_from_prev_close, _round_to_cent,
)


# ------------------------------------------------------------------ helpers

def _synth_panel(n, price, limit_day=None, limit_side="up"):
    """构造一只标的的日线面板（无 date ⇒ align_panel 走尾部截断对齐分支）。

    默认所有 bar 平在 ``price``；``limit_day`` 指定某一天的 OHLC 改为涨/跌停价
    （以 ``price`` 为前收计算）。返回可直接喂给 ``run_backtest(preloaded=)`` 的 dict。
    """
    open_ = [price] * n
    high = [price] * n
    low = [price] * n
    close = [price] * n
    if limit_day is not None and 0 <= limit_day < n:
        pct = 0.10
        if limit_side == "up":
            lp = _round_to_cent(price * (1 + pct))
            open_[limit_day] = lp
            high[limit_day] = lp
            low[limit_day] = lp
            close[limit_day] = lp
        else:
            ld = _round_to_cent(price * (1 - pct))
            open_[limit_day] = ld
            high[limit_day] = ld
            low[limit_day] = ld
            close[limit_day] = ld
    return {
        "TEST.SZ": {
            "open": open_, "high": high, "low": low, "close": close,
            "volume": [1000] * n,
        }
    }


def _run_synth(cfg, panel, count=80):
    return run_backtest(["TEST.SZ"], cfg, count=count, preloaded=panel)


def _manual_cfg(**kw):
    """最小配置：观察篮绕过一切信号闸门，保证确定性建仓。

    momentum_rank/use_gate 关闭、exit_mode="scalp" + trailing 关闭 + atr 倍数归零，
    使 stop_dist=abs(stop_loss)、target_dist=take_profit 完全确定。
    """
    base = dict(
        manual_entry_codes=("TEST.SZ",),
        manual_entry_slots=1,
        manual_entry_exit_exempt=True,
        max_positions=1,
        momentum_rank=False,
        use_gate=False,
        exit_mode="scalp",
        trailing=False,
        atr_stop_mult=0.0,
        tp_atr_mult=0.0,
        stop_loss=-0.05,
        take_profit=0.50,
    )
    base.update(kw)
    return BacktestConfig(**base)


# ------------------------------------------------------------------ 门锁

def test_default_is_false():
    """门锁 1：研究开关默认关闭，防止静默改写全部已钉基线。"""
    assert BacktestConfig().limit_up_block is False
    assert W.base_cfg().limit_up_block is False
    assert BacktestConfig().limit_pct_main == 0.10
    assert BacktestConfig().limit_pct_growth == 0.20


def test_off_keeps_legacy_baseline():
    """门锁 2：默认 False 必须与既有基线逐位相同（tushare 面板跑 base_cfg）。

    常量 = 本开关落盘前对 ``base_cfg()`` 在共享确定性 tushare 面板上的复现值，
    与其它守卫同源（tests/_baseline_tushare_panel.py）。若开关默认被误改为 True，
    此处会漂移并暴露。
    """
    from tests._baseline_tushare_panel import baseline_is_result

    r_base = baseline_is_result(W.base_cfg())
    r_off = baseline_is_result(replace(W.base_cfg(), limit_up_block=False))
    for k in ("total_return", "sharpe", "max_drawdown", "n_trades", "win_rate"):
        assert r_base[k] == r_off[k], f"{k} 发生变化：{r_base[k]} vs {r_off[k]}"
    # 关闭态计数恒为 0（零行为变化）
    assert r_base["limit_blocked_buy"] == 0
    assert r_base["limit_blocked_sell"] == 0
    # 钉死当前基线（防未来某次「顺手改默认」静默漂移）
    # 【2026-10-29 R44 第六次重钉】entry_max_ext_ma60 0→0.8 ⇒ +307.57% / Sh 1.93。
    assert abs(r_base["total_return"] - 3.0756905) < 0.02, \
        f"基线漂移：total_return={r_base['total_return']:.6f}"
    assert abs(r_base["sharpe"] - 1.9312593) < 0.05, \
        f"基线漂移：sharpe={r_base['sharpe']:.6f}"


def test_on_blocks_limit_up_buy():
    """门锁 3a：合成「次日一字涨停」票，True 时该笔买入被拦且 limit_blocked_buy>0。

    构造：第 65 根收盘 10.0（信号日，观察篮绕过闸门生成买单），第 66 根一字涨停
    11.0（= round(10×1.1)）⇒ open/high 触及涨停价，买单被放弃。
    """
    panel = _synth_panel(80, 10.0, limit_day=66, limit_side="up")
    r_off = _run_synth(_manual_cfg(limit_up_block=False), panel)
    r_on = _run_synth(_manual_cfg(limit_up_block=True), panel)
    assert r_off["limit_blocked_buy"] == 0, \
        "关闭态不应拦截（对照组）"
    assert r_on["limit_blocked_buy"] > 0, \
        "一字涨停日的买单应被拦，limit_blocked_buy 必须 > 0"
    # 被拦后，次日（第 67 根）以正常开盘价 10.0 重买 ⇒ 入场价低于对照组的 11.0，
    # 期末权益与对照组不同（这就是「涨停美化」被修正后真实摩擦的体现）。
    assert r_on["end_equity"] != r_off["end_equity"], \
        "涨停被拦必须改变成交结果（入场价 10.0 vs 11.0），否则代理未生效"


def test_on_defers_limit_down_sell():
    """门锁 3b：合成「次日一字跌停」票，True 时卖单被顺延且 limit_blocked_sell>0。

    构造：第 66 根正常买入（open 10.0），第 67 根一字跌停 9.0
    （= round(10×0.9)），触发 stop_loss（stop_price=9.5，low 9.0 ≤ 9.5）但
    low 触及跌停价 ⇒ 卖单顺延；第 68 根 low=9.0 > 8.1（新前收 9.0 的跌停价）⇒ 卖出。
    """
    n = 80
    open_ = [10.0] * n
    high = [10.0] * n
    low = [10.0] * n
    close = [10.0] * n
    # 第 67 根：一字跌停（以第 66 根收盘 10.0 为前收）
    for k in (67,):
        open_[k] = high[k] = low[k] = close[k] = 9.0
    panel = {"TEST.SZ": {"open": open_, "high": high, "low": low,
                         "close": close, "volume": [1000] * n}}

    r_off = _run_synth(_manual_cfg(limit_up_block=False), panel)
    r_on = _run_synth(_manual_cfg(limit_up_block=True), panel)
    assert r_off["limit_blocked_sell"] == 0, \
        "关闭态不应顺延（对照组，跌停日直接按 stop 价卖出）"
    assert r_on["limit_blocked_sell"] > 0, \
        "跌停封死日的卖单应被顺延，limit_blocked_sell 必须 > 0"


# ------------------------------------------------------------------ 纯函数

def test_limit_price_helpers():
    """涨停/跌停价四舍五入到 0.01，创业板/科创板用 20%。"""
    cfg = BacktestConfig()
    assert _limit_pct_for("000001.SZ", cfg) == 0.10
    assert _limit_pct_for("300308.SZ", cfg) == 0.20
    assert _limit_pct_for("301123.SZ", cfg) == 0.20
    assert _limit_pct_for("688008.SH", cfg) == 0.20
    assert _limit_pct_for("603986.SH", cfg) == 0.10
    # round(10.0 × 1.1, 2) = 11.0 ; round(10.0 × 0.9, 2) = 9.0
    assert _limit_price_from_prev_close(10.0, 0.10, "up") == 11.0
    assert _limit_price_from_prev_close(10.0, 0.10, "down") == 9.0
    # 四舍五入到 0.01：10.03 × 1.1 = 11.033 → 11.03
    assert _limit_price_from_prev_close(10.03, 0.10, "up") == 11.03

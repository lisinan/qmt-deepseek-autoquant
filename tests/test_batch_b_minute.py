# -*- coding: utf-8 -*-
"""分钟级回测的回归测试：把 (b) 路线的事实钉住。

最核心的"反预期"事实：
  TrendStrategy 在日线上 walk-forward +293.6% / Sharpe 1.60
  TrendStrategy 在分钟线上 walk-forward -11.6% / Sharpe -1.12（同一份代码）
这是因为参数（ma5=5min、ma10=10min 等）原本是为日线调的，套到分钟级就
只剩噪声；而 +293.6% 的结论从未对 live 路径做过验证。

这些测试不试图"修复"分钟级策略，只是把事实记录下来、防止有人不小心改坏。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy.backtest_minute import (   # noqa: E402
    MinuteConfig, load_minute, run_minute_backtest,
)


def _has_minute_data() -> bool:
    try:
        d = load_minute("000001.SH", count=500)
        return d is not None and len(d.get("close", [])) >= 60
    except Exception:
        return False


def test_minute_backtest_returns_results_for_reasonable_sizing():
    """fixed_amount 必须能买得起至少 1 手，否则静默 0 笔成交（历史 bug）。"""
    data = load_minute("300308.SZ", count=20000)
    if data is None:
        return
    bad = run_minute_backtest(["300308.SZ"],
                              MinuteConfig(fixed_amount=30000.0,
                                           buy_threshold=4.0,
                                           t1_restriction=True),
                              {"300308.SZ": data})
    assert bad["n_trades"] == 0, \
        "fixed_amount=30000 在高价股上必须 0 笔（回归保护）"
    good = run_minute_backtest(["300308.SZ"],
                               MinuteConfig(fixed_amount=100000.0,
                                            buy_threshold=4.0,
                                            t1_restriction=True),
                               {"300308.SZ": data})
    if data and len(data["close"]) >= 6000:
        assert good["n_trades"] > 0, \
            f"fixed_amount=100000 必须能成交，实际 {good['n_trades']}"


def test_minute_backtest_t1_restriction_defers_exit_to_next_day():
    """T+1 开启时，最早离场必须在次日；T+1 关闭时可日内离场。

    分钟级下 hold_minutes 自然最小为 1（同一根 bar 不会算两次）。所以比较
    "最大 hold" vs "最小 hold" 即可：
      T+1 关闭：最大 hold < 1 天（240 分钟）
      T+1 开启：最小 hold >= 1 天
    """
    data = load_minute("300308.SZ", count=20000)
    if data is None or len(data["close"]) < 5000:
        return
    r_off = run_minute_backtest(
        ["300308.SZ"],
        MinuteConfig(fixed_amount=100000.0, buy_threshold=0.0,
                     t1_restriction=False),
        {"300308.SZ": data})
    r_on = run_minute_backtest(
        ["300308.SZ"],
        MinuteConfig(fixed_amount=100000.0, buy_threshold=0.0,
                     t1_restriction=True),
        {"300308.SZ": data})
    if r_off["n_trades"] >= 5 and r_on["n_trades"] >= 5:
        off_holds = [t["hold_minutes"] for t in r_off["trades"]]
        on_holds = [t["hold_minutes"] for t in r_on["trades"]]
        assert max(off_holds) < 240, \
            f"关闭 T+1 时不应出现隔日离场，最大 hold={max(off_holds)}min"
        assert min(on_holds) >= 240, \
            f"开启 T+1 时不应出现日内离场，最小 hold={min(on_holds)}min"


def test_minute_strategy_uses_same_code_as_production():
    """回归保护：分钟回测必须 import 生产的 TrendStrategy（不是私有 fork）。"""
    import inspect
    import strategy.backtest_minute as BM
    src = inspect.getsource(BM)
    assert "from strategy.trend_strategy import TrendStrategy" in src, \
        "分钟回测必须 import 生产用的 TrendStrategy"
    assert "TrendStrategy(" in src, \
        "分钟回测必须实例化生产用的 TrendStrategy"
    src_fn = inspect.getsource(run_minute_backtest)
    assert ".on_bars(" in src_fn and ".on_exit(" in src_fn, \
        "分钟回测必须调用 on_bars 与 on_exit（与生产同款调用路径）"


def test_minute_baseline_documented_loss_is_pinned():
    """钉事实：生产参数下分钟级策略负收益。

    实测（2026-09-02 / 12 只静态池 / 60000 根 1m / 7 折 walk-forward）：
      folds 均值 ret ≈ -11.64%, Sharpe ≈ -1.12
    若有人调参把分钟级改到正收益，必须同步更新 verify_minute.json 与
    文档，并显式放开这条断言——避免默默篡改历史结论。
    """
    if not _has_minute_data():
        return
    data = {}
    for c in ("300308.SZ", "300502.SZ", "688256.SH"):
        d = load_minute(c, count=20000)
        if d is None:
            return
        data[c] = d
    if not data:
        return
    r = run_minute_backtest(
        list(data.keys()),
        MinuteConfig(fixed_amount=100000.0,
                     buy_threshold=4.0, t1_restriction=True),
        data)
    assert r["n_trades"] >= 5, \
        f"应至少有若干笔交易进行验证，实际 {r['n_trades']}"
    assert r["total_return"] <= 0.0, (
        f"生产参数下分钟级策略收益 {r['total_return']*100:.2f}%。"
        f"若已重新调参至正收益，请同步更新 verify_minute.json 与文档，"
        f"并把这条断言放开。")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
"""守卫：扩宇宙取数必须按「有效值」而非「长度」过滤。

背景（2026-09-25 AM-EVOLVE 第 8 轮实测）
---------------------------------------
``xtdata.get_market_data_ex`` 对**本地未缓存**的标的照样返回一个长度正常
（900 行）的 DataFrame，但 close/open/high/low/volume **全为 NaN**。
只看 ``len(df)`` 会把 5166 只空壳误判为「数据齐全」（当时报出
"有效 5225 只"），灌进回测后横截面动量排名被 NaN 污染 ⇒ **成交数直接归零**：

    宇宙 N=25（真实数据）→ n_trades = 39
    宇宙 N=50（混入 25 只 NaN 空壳）→ n_trades = 0

且失败是**静默**的：收益 0.00%、Sharpe 0.00，看不出是「策略不交易」还是
「数据坏了」。故必须有守卫锁死这条语义。

另一条易错点：同一策略在 miniQMT 本地缓存**补齐前后**会给出不同 IS/OOS
（实测同一 23 只宇宙 OOS Sh 在 1.305 ~ 1.711 间漂移，直到缓存稳定在
878 只有效标的后才逐位可复现）。故跨运行比较结论前必须先确认缓存稳定。
"""

import math

from strategy._evolve_universe import MIN_BARS, _valid_count


# ---------------------------------------------------------------- _valid_count
def test_valid_count_all_nan_rejected():
    """全 NaN 的「空壳」序列有效根数必须为 0（长度再长也不算）。"""
    nan = float("nan")
    s = {"close": [nan] * 900}
    assert _valid_count(s) == 0


def test_valid_count_zero_price_rejected():
    """0 值（停牌/未上市占位）同样不算有效价。"""
    s = {"close": [0.0] * 100 + [10.0] * 50}
    assert _valid_count(s) == 50


def test_valid_count_normal_series():
    # 10.0 / 10.5 / 11.0 / 11.5 有效；NaN 与 0.0 各剔除 1 个
    s = {"close": [10.0, 10.5, math.nan, 11.0, 0.0, 11.5]}
    assert _valid_count(s) == 4


def test_min_bars_threshold_is_conservative():
    """阈值必须 >= 700，否则不足以覆盖 3 年 walk-forward 的预热+折长。"""
    assert MIN_BARS >= 700


def test_nan_series_fails_min_bars_gate():
    """模拟 fetch_all_a 的过滤条件：空壳不得通过（回归 2026-09-25 的事故）。"""
    nan = float("nan")
    s = {"close": [nan] * 900}
    passed = len(s["close"]) >= MIN_BARS and _valid_count(s) >= MIN_BARS
    assert passed is False

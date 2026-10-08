# -*- coding: utf-8 -*-
"""业绩预告上修信号提取（data/earnrev_cache）的数据完整性守卫。

【2026-10-08 修复】原 ``_rev_from_forecast`` 用 ``float(v) or 0.0`` 处理
p_change，但 Python 里 ``float('nan') or 0.0`` 返回 ``nan``（NaN 是 truthy），
导致「扭亏为盈/由盈转亏」这类 p_change 无法定义的修订（Tushare 返回 NaN）被
算成 ``rev = mid - nan = nan``，再经 ``json.dumps`` 写出**非法 JSON** 损坏缓存。
本守卫锁死：p_change 两端任一无效（None/NaN/非数值）即跳过，绝不产出 NaN。
"""
from __future__ import annotations

import math

import pandas as pd

from data.earnrev_cache import _rev_from_forecast


def _df(rows):
    return pd.DataFrame(rows, columns=[
        "ann_date", "end_date", "type", "p_change_min", "p_change_max",
        "net_profit_min", "net_profit_max",
    ])


def test_skips_nan_p_change_row():
    """NaN 修订必须被跳过，不得污染其余修正序列（json.dumps(nan) 会写坏缓存）。"""
    df = _df([
        # 同一报告期 20251231 两次指引：首次 p_change 为 NaN（扭亏/由盈转亏），
        # 第二次有效。旧实现会算 rev = mid - nan = nan。
        ("20250829", "20251231", "修正", float("nan"), float("nan"), 0, 0),
        ("20260131", "20251231", "预增", 508.99, 575.31, 0, 0),
    ])
    out = _rev_from_forecast(df)
    # 首次 NaN 被跳过 ⇒ 第二次成为「首条指引」、无基线 ⇒ 无修正事件
    assert out == {}
    assert not any(math.isnan(v) for v in out.values())


def test_skips_none_p_change_row():
    df = _df([
        ("20250829", "20251231", "修正", None, None, 0, 0),
        ("20260131", "20251231", "预增", 508.99, 575.31, 0, 0),
    ])
    out = _rev_from_forecast(df)
    assert out == {}


def test_valid_revision_computes_signed_midpoint_delta():
    """有效区间：rev = 本次中点 - 上次中点，符号区分上/下修。"""
    df = _df([
        ("20250829", "20251231", "预增", 100.0, 200.0, 0, 0),   # 中点 150
        ("20260131", "20251231", "预增", 200.0, 260.0, 0, 0),   # 中点 230
    ])
    out = _rev_from_forecast(df)
    assert out == {"20260131": 80.0}


def test_never_emits_nan_in_output():
    """随机 NaN 夹杂也不得让输出出现任何 NaN。"""
    df = _df([
        ("20250829", "20251231", "修正", float("nan"), 10.0, 0, 0),
        ("20260131", "20251231", "预增", 508.99, 575.31, 0, 0),
        ("20240715", "20240630", "预增", 180.0, 230.0, 0, 0),
        ("20250124", "20240630", "预增", 200.0, 240.0, 0, 0),  # 与上一条同报告期→修正
    ])
    out = _rev_from_forecast(df)
    assert all(not math.isnan(v) for v in out.values())

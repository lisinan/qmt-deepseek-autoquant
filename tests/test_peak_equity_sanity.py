# -*- coding: utf-8 -*-
"""守卫：【2026-10-08 AM-EVOLVE 第 22 轮】峰值自愈（peak_equity 脏值夹紧）。

背景（实盘铁证，见 engine/event_engine.py::PEAK_EQUITY_STALE_SANITY）：
    2026-09-21 复位后可信日内最高 = 09-22 的 **1,005,860.0**；
    而 engine_state.peak_equity / peak_asset = **1,020,446.6**，唯一来源是
    10-07 休市日 `get_full_tick` 的伪快照（R20 逐票举证虚增 +45,173）。
    R20 只阻断**新增**污染，脏值却随 `_restore_engine_state` 跨重启永久继承
    ⇒ 回撤虚高 1.38pp、`max_drawdown_pct=-0.20` 断路器提前 1.2% 触发。

本守卫钉死三件事：
    DR-a  纯函数在默认输入下**只向下夹紧**，且开关关闭时零行为变化（可逆）；
    DR-b  真实脏日形态（多快照零方差）被正确剔除，正常波动日被正确保留；
    DR-c  量测通道故障（查询失败/空表）时保守原样保留，绝不误改风控基线。

★ 陷阱（本项目踩过）：`tests/run_all.py` **只收集模块级 `test_*` 函数**，
  不收集 unittest.TestCase 类方法 ⇒ 写成类的用例会「静默不执行且 PASSED 不增」。
  故本文件全部写成模块级函数。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QMT_IGNORE_INTERPRETER_CHECK", "1")

from engine.event_engine import (  # noqa: E402
    EventEngine, credible_peak_from_daily, sanitize_peak_equity,
)

# 真实数据形态（storage/qmt.db，2026-09-21 复位后逐日聚合）
#   (快照数, 日内最高, 日内最低)
_REAL_DAILY = [
    (158, 1005860.0, 995571.0),   # 09-22  ← 真实峰值
    (257, 1000594.0, 996314.8),   # 09-23
    (262, 996189.6, 991309.6),    # 09-24
    (258, 991309.6, 991309.6),    # 09-25  零方差 → 脏日（R9 盯市未刷新缺陷）
    (263, 991109.6, 973493.6),    # 09-28
    (262, 981200.6, 971023.6),    # 09-29
    (1,   975273.6, 975273.6),    # 09-30  仅 1 条快照，样本不足，不判脏
    (263, 1020446.6, 1020446.6),  # 10-07  零方差 → 脏日（休市伪快照）
    (134, 980541.6, 974289.6),    # 10-08
]


def test_credible_peak_excludes_zero_variance_days():
    """真实逐日聚合下，可信峰值应剔除 10-07 脏日，回到 1,005,860。"""
    got = credible_peak_from_daily(_REAL_DAILY)
    assert got == 1005860.0, f"可信峰值应为 1005860.0（09-22），实得 {got}"


def test_credible_peak_keeps_low_sample_days():
    """样本不足（9-30 仅 1 条）的零方差日**不**判脏，避免误杀。"""
    rows = [(1, 975273.6, 975273.6)]
    assert credible_peak_from_daily(rows) == 975273.6


def test_credible_peak_empty_is_zero():
    assert credible_peak_from_daily([]) == 0.0
    assert credible_peak_from_daily(None) == 0.0


def test_sanitize_clamps_dirty_peak_down():
    """核心：脏峰值 1,020,446.6 被夹到可信峰值 1,005,860。"""
    got = sanitize_peak_equity(1020446.6, 1005860.0)
    assert got == 1005860.0, f"应夹紧到 1005860.0，实得 {got}"


def test_sanitize_never_raises():
    """存储峰值低于可信峰值时**原样返回**（只向下，永不上调）。"""
    assert sanitize_peak_equity(1000000.0, 1005860.0) == 1000000.0


def test_sanitize_passthrough_when_unknown():
    """无法判定（任一侧 <= 0）时原样返回，绝不凭空改风控基线。"""
    assert sanitize_peak_equity(0.0, 1005860.0) == 0.0
    assert sanitize_peak_equity(1020446.6, 0.0) == 1020446.6
    assert sanitize_peak_equity(1020446.6, -1.0) == 1020446.6


def test_sanitize_reversible_by_switch():
    """DR-a 可逆性：开关置 False 时完全恢复「原样读回」行为。"""
    old = EventEngine.PEAK_EQUITY_STALE_SANITY
    try:
        EventEngine.PEAK_EQUITY_STALE_SANITY = False
        assert sanitize_peak_equity(1020446.6, 1005860.0) == 1020446.6
    finally:
        EventEngine.PEAK_EQUITY_STALE_SANITY = old
    # 恢复后必须重新生效
    assert sanitize_peak_equity(1020446.6, 1005860.0) == 1005860.0


def test_sanitize_no_op_when_peak_credible():
    """回归：峰值本身可信时（DR-a 零行为变化）结果逐位不变。"""
    assert sanitize_peak_equity(1005860.0, 1005860.0) == 1005860.0
    assert sanitize_peak_equity(975273.6, 1005860.0) == 975273.6


def test_engine_method_clamps_both_peaks():
    """`_sanitize_restored_peaks` 应同时夹紧 peak_equity 与 risk._peak_asset。"""
    eng = _bare_engine()
    eng._peak_equity = 1020446.6
    eng.risk._peak_asset = 1020446.6
    eng._credible_peak_equity = lambda: 1005860.0     # 注入可信峰值（不碰 DB）
    eng._sanitize_restored_peaks()
    assert eng._peak_equity == 1005860.0
    assert eng.risk._peak_asset == 1005860.0


def test_engine_method_noop_when_measurement_fails():
    """DR-c：可信峰值取不到（量测故障）时保守原样保留。"""
    eng = _bare_engine()
    eng._peak_equity = 1020446.6
    eng.risk._peak_asset = 1020446.6
    eng._credible_peak_equity = lambda: 0.0
    eng._sanitize_restored_peaks()
    assert eng._peak_equity == 1020446.6
    assert eng.risk._peak_asset == 1020446.6


def test_engine_method_disabled_by_switch():
    old = EventEngine.PEAK_EQUITY_STALE_SANITY
    try:
        EventEngine.PEAK_EQUITY_STALE_SANITY = False
        eng = _bare_engine()
        eng._peak_equity = 1020446.6
        eng.risk._peak_asset = 1020446.6
        eng._credible_peak_equity = lambda: 1005860.0
        eng._sanitize_restored_peaks()
        assert eng._peak_equity == 1020446.6
        assert eng.risk._peak_asset == 1020446.6
    finally:
        EventEngine.PEAK_EQUITY_STALE_SANITY = old


def test_drawdown_recomputed_on_real_numbers():
    """端到端口径：真实权益 974,471.6 在修复前后的回撤差异。

    修复前（脏峰值）：974471.6/1020446.6 − 1 = −4.505%
    修复后（真峰值）：974471.6/1005860.0 − 1 = −3.121%
    ⇒ 虚高约 1.38pp，正是本缺陷的量级。
    """
    equity = 974471.6
    dirty_peak = 1020446.6
    clean_peak = sanitize_peak_equity(dirty_peak, credible_peak_from_daily(_REAL_DAILY))
    dd_before = equity / dirty_peak - 1.0
    dd_after = equity / clean_peak - 1.0
    assert clean_peak == 1005860.0
    assert abs(dd_before - (-0.04505)) < 0.0001, f"{dd_before}"
    assert abs(dd_after - (-0.03121)) < 0.0001, f"{dd_after}"
    assert dd_after > dd_before      # 修复后回撤更小（虚高被剔除）


def _bare_engine():
    """构造一个只带 risk 桩的引擎壳，避免触碰 DB / 行情 / 主循环。"""
    class _Risk:
        def __init__(self):
            self._peak_asset = 0.0

    eng = EventEngine.__new__(EventEngine)      # 不走 __init__（避免连行情）
    eng.risk = _Risk()
    eng._peak_equity = 0.0
    return eng

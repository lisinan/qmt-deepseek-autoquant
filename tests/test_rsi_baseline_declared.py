# -*- coding: utf-8 -*-
"""守卫：P0 基线「已声明分歧」登记表（`config/_baseline_divergence.py`）。

背景（2026-11-08 R50-RSI）：
  同一条事实曾有两份判据 ——
    · `tests/test_evolve_baseline_sync.py` 认为 `manual_entry_codes`（回测 () /
      生产 ['510050.SH']）是**已登记、已写理由、已钉值**的刻意差异；
    · `scripts/_recursive_self_improve.py::audit_baseline_drift()` 认为它是
      **P0 基线漂移**，于是 R49/R50 连续两周 `priority_actions()` 首位都是这条
      永远修不了的假 P0（修它要么改 P0 基线，要么丢掉 live 对账对象）。
  现在表移到 `config/_baseline_divergence.py`，两侧同 import（单一事实源）。

本用例钉住四件事，缺一条这张表就会退化成「按字段名豁免」的橡皮图章：
  1. 两侧用的是同一张表（不得再有第二份字面量抄回去）；
  2. **豁免只对登记的逐位相同的值生效** —— 生产加一只票 / 回测把探针抄进基线，
     都必须重新变成真漂移（这是唯一的安全边界，反向对照）；
  3. 登记的生产值必须等于 `config/settings.py` 的真实值（表自己不许说谎）；
  4. 登记的字段不得同时出现在 MUST_MATCH（双重归类会让任一侧改动被两套口径解释）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config.settings as S                                        # noqa: E402
from config._baseline_divergence import (                          # noqa: E402
    KNOWN_DIVERGENCE, declared_pair, is_declared,
)
from strategy._evolve_wf import base_cfg                           # noqa: E402


def test_single_source_of_truth():
    """两侧必须引用同一张表 —— 不得再抄一份字面量回测试文件。"""
    from tests import test_evolve_baseline_sync as guard
    assert guard.KNOWN_DIVERGENCE is KNOWN_DIVERGENCE, (
        "tests/test_evolve_baseline_sync.py 里的 KNOWN_DIVERGENCE 已不是 "
        "config/_baseline_divergence.py 的同一对象 ⇒ 出现了第二份判据，"
        "必然与 RSI 审计静默漂移（第 24 条）")


def test_declared_pass_through_is_exact_value_only():
    """正向：登记的「那一对值」判为已声明；任一侧再变必须回到真漂移。"""
    assert is_declared("manual_entry_codes", (), ("510050.SH",)), (
        "当前登记对 ((), ('510050.SH',)) 应判为已声明分歧")

    # 反向对照 ①：生产多加一只 → 不再豁免（否则真漂移被吞）
    assert not is_declared("manual_entry_codes", (), ("510050.SH", "300308.SZ")), (
        "生产侧多一只观察篮后仍被判「已声明」⇒ 豁免变成了按字段名的橡皮图章")
    # 反向对照 ②：回测基线把探针抄进去 → 不再豁免（P0 定义被污染，必须报红）
    assert not is_declared("manual_entry_codes", ("510050.SH",), ("510050.SH",)), (
        "回测基线把 510050 抄进来后仍被判「已声明」⇒ 观察篮污染 P0 基线却无人报警")
    # 反向对照 ③：生产清空（OWNER 裁决撤篮）→ 与 pin 不符，须显式更新登记表
    assert not is_declared("manual_entry_codes", (), ()), (
        "生产清空后应要求显式更新 KNOWN_DIVERGENCE，而不是静默沿用旧 pin")
    # 反向对照 ④：未登记字段的任何差异都不得被豁免
    assert not is_declared("risk_per_trade", 0.012, 0.040), (
        "未登记字段被豁免 ⇒ 真漂移会被这张表吞掉")
    assert declared_pair("risk_per_trade") is None


def _norm(v):
    """把 list/tuple 归一成 tuple；bool/数值原样返回（登记表里两种都有）。"""
    if isinstance(v, (list, tuple)):
        return tuple(v)
    return v


def test_registry_matches_production_and_baseline():
    """表里钉的生产值/回测值必须等于现网真实值（表自己不许「绿着说谎」）。"""
    b = base_cfg()
    for field, (back_pin, prod_pin) in KNOWN_DIVERGENCE.items():
        assert field in S.STRATEGY_PARAMS, f"生产 STRATEGY_PARAMS 缺失 {field}"
        got_prod = S.STRATEGY_PARAMS[field]
        assert _norm(got_prod or ()) == _norm(prod_pin or ()), (
            f"{field}: 表里钉的生产值 {prod_pin!r} ≠ 现网 {got_prod!r} ⇒ "
            f"须显式更新 config/_baseline_divergence.py 并说明原因")
        assert hasattr(b, field), f"验证器 base_cfg 缺字段 {field}"
        got_bt = getattr(b, field)
        assert _norm(got_bt or ()) == _norm(back_pin or ()), (
            f"{field}: 表里钉的回测值 {back_pin!r} ≠ base_cfg {got_bt!r} ⇒ "
            f"P0 基线被改，须显式更新登记表（并确认是量测口径修正而非 alpha 改动）")


def test_declared_fields_are_classified_not_drift():
    """集成：RSI 审计必须把登记字段判「已声明分歧」，且不进 drift 列表。

    这是本轮修复的**行为断言**：旧实现在这里给的是 ★漂移。
    """
    from scripts._recursive_self_improve import audit_baseline_drift
    rep = audit_baseline_drift()
    rows = {r["field"]: r for r in rep["rows"]}
    for field in KNOWN_DIVERGENCE:
        row = rows.get(field)
        assert row is not None, f"审计未覆盖登记字段 {field}"
        assert row["status"] in ("已声明分歧", "一致"), (
            f"{field} 被判 {row['status']} ⇒ 已声明的刻意差异重新占用 P0 首位")
    drift_fields = {r["field"] for r in rep["drift"]}
    assert not (drift_fields & set(KNOWN_DIVERGENCE)), (
        "登记字段出现在 drift 列表：" + ", ".join(sorted(drift_fields & set(KNOWN_DIVERGENCE))))
    assert rep["declared_n"] == len(rep["declared"])


def test_no_double_registration():
    """同一字段不得同时登记为「已声明分歧」与「必须一致」。"""
    from tests.test_evolve_baseline_sync import MUST_MATCH, RISK_MUST_MATCH
    both = set(KNOWN_DIVERGENCE) & (set(MUST_MATCH) | set(RISK_MUST_MATCH))
    assert not both, (
        "以下字段既在 KNOWN_DIVERGENCE 又在 MUST_MATCH：" + ", ".join(sorted(both))
        + " ⇒ 任一侧改动会被两套口径分别解释，断言结果不可信")

# -*- coding: utf-8 -*-
"""B10 KPI 代码化守卫：config/kpi_targets.py 的判定逻辑（纯函数，无回测）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import kpi_targets as KT  # noqa: E402


def test_kpi_registry_has_single_code_source():
    """七个 KPI 均登记，且每个都有 metric/target/op/status。

    2026-10-22 P0-2 追加 `kpi_sig`（晋升闸门②显著性，proposed 不 gate）。
    """
    assert set(KT.KPI.keys()) == {
        "kpi1", "kpi1_span", "kpi2", "kpi2b", "kpi3", "kpi4", "kpi4_live",
        "kpi_sig"}
    for kid, spec in KT.KPI.items():
        assert spec.get("metric"), f"{kid} 缺 metric"
        assert "target" in spec, f"{kid} 缺 target"
        assert spec.get("op") in (">=", "<="), f"{kid} op 非法"
        assert spec.get("status") in ("active", "proposed", "deprecated"), kid


def test_evaluate_pass_fail_and_deprecated():
    m = {
        "fold_mean_sharpe": 1.7,   # >= 1.6 → PASS
        "worst_fold_return": -0.20,  # >= -0.15 → FAIL
        "full_sample_alpha": -0.10,  # >= -0.30 → PASS
    }
    rows = {r["id"]: r for r in KT.evaluate(m)}
    assert rows["kpi1"]["pass"] is True
    assert rows["kpi3"]["pass"] is False
    assert rows["kpi4"]["pass"] is True
    assert rows["kpi2"]["status"] == "deprecated"
    # deprecated 项即使可算也不参与 gate（见 gate_verdict）
    assert rows["kpi4_live"]["pass"] is None  # 未注入 → n/a


def test_gate_verdict_only_counts_active_non_none():
    m = {
        "fold_mean_sharpe": 1.7,
        "worst_fold_return": -0.10,
        "full_sample_alpha": -0.20,
    }
    verdict = KT.gate_verdict(KT.evaluate(m))
    assert verdict["all_active_pass"] is True
    assert verdict["gated"] == ["kpi1", "kpi3", "kpi4"]  # kpi4_live 缺失不参与
    assert verdict["failed"] == []

    m2 = dict(m, worst_fold_return=-0.20)
    verdict2 = KT.gate_verdict(KT.evaluate(m2))
    assert verdict2["all_active_pass"] is False
    assert verdict2["failed"] == ["kpi3"]


def test_kpi4_is_full_sample_alpha_semantics():
    """KPI#4 度量必须绑定 run_backtest().alpha（策略 vs 等权买入持有）。"""
    assert KT.KPI["kpi4"]["metric"] == "full_sample_alpha"
    assert KT.KPI["kpi4"]["status"] == "active"
    # 主 KPI：目标 ≥ -30pt（即 -0.30）
    assert abs(KT.KPI["kpi4"]["target"] - (-0.30)) < 1e-9

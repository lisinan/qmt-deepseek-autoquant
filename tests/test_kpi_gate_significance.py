# -*- coding: utf-8 -*-
"""P0-2（2026-10-22）：显著性护栏接进 KPI gate 的单元测试。

覆盖：
  1. SIGNIFICANCE 常量组存在且 dsr_min ∈ (0,1)；
  2. kpi_sig 已注册、status=proposed（不参与默认总体裁决）；
  3. 纯函数 strict_verdict：dsr 低 → FAIL、高 → PASS、None → FAIL、
     base FAIL → FAIL；
  4. 默认模式不受影响：kpi_sig 不进入 gate_verdict 的 gated 清单。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import kpi_targets as KT
from strategy._evolve_wf import strict_verdict


def test_significance_constants_exist():
    sig = KT.SIGNIFICANCE
    for k in ("dsr_min", "p_max", "n_trials_conservative", "n_trials_optimistic"):
        assert k in sig, f"SIGNIFICANCE 缺 {k}"
    assert 0.0 < sig["dsr_min"] < 1.0
    assert 0.0 < sig["p_max"] < 1.0
    assert sig["n_trials_conservative"] > sig["n_trials_optimistic"] >= 1
    print("PASS test_significance_constants_exist")


def test_kpi_sig_registered_proposed():
    spec = KT.KPI.get("kpi_sig")
    assert spec is not None, "kpi_sig 未注册"
    assert spec["status"] == "proposed"
    assert spec["metric"] == "gate_significance"
    assert "C5_SIGNIFICANCE_AUDIT" in spec["note"]
    # proposed 不参与总体裁决
    rows = KT.evaluate({"gate_significance": 0.0, "fold_mean_sharpe": 2.0,
                        "worst_fold_return": 0.0, "full_sample_alpha": 0.0})
    v = KT.gate_verdict(rows)
    assert "kpi_sig" not in v["gated"]
    assert v["all_active_pass"] is True
    print("PASS test_kpi_sig_registered_proposed")


def test_strict_verdict_low_dsr_fails():
    r = strict_verdict(True, dsr=0.897, p=0.04)
    assert r["pass"] is False
    assert "0.897" in r["reason"]
    print("PASS test_strict_verdict_low_dsr_fails")


def test_strict_verdict_high_dsr_passes():
    r = strict_verdict(True, dsr=0.97, p=0.01)
    assert r["pass"] is True
    print("PASS test_strict_verdict_high_dsr_passes")


def test_strict_verdict_none_or_base_fail():
    assert strict_verdict(True, dsr=None)["pass"] is False      # 算不出→保守 FAIL
    r = strict_verdict(False, dsr=0.99)
    assert r["pass"] is False and "基础 KPI gate" in r["reason"]
    print("PASS test_strict_verdict_none_or_base_fail")


def test_default_mode_unchanged():
    """默认（非 strict）语义：gate_verdict 只看 active 项，kpi_sig 不参与。"""
    rows = KT.evaluate({"gate_significance": 0.10,  # 即使极低
                        "fold_mean_sharpe": 2.0,
                        "worst_fold_return": 0.0,
                        "full_sample_alpha": 0.0})
    v = KT.gate_verdict(rows)
    assert v["all_active_pass"] is True
    assert v["failed"] == []
    # active 集合未被新增项污染
    assert set(v["gated"]) == {"kpi1", "kpi3", "kpi4"}
    print("PASS test_default_mode_unchanged")


if __name__ == "__main__":
    test_significance_constants_exist()
    test_kpi_sig_registered_proposed()
    test_strict_verdict_low_dsr_fails()
    test_strict_verdict_high_dsr_passes()
    test_strict_verdict_none_or_base_fail()
    test_default_mode_unchanged()
    print("\nALL 6 TESTS PASS")

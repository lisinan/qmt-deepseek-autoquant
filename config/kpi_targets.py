# -*- coding: utf-8 -*-
"""KPI 目标与口径的**单一代码来源**（B10，2026-10-21）。

【为什么需要这个文件】
KPI 目标值此前只散落在 ``docs/AUTOMATIONS.md`` 与各轮提示词里，机器无法复核，
每一轮自进化都要「读 7000 行账本 → 复述不可达目标 → 再写一份报告」。
本文件把「目标值 / 口径 / 度量函数 / 状态」收敛成代码，供
``strategy/_evolve_wf.py --mode gate`` 用同一套判据输出 PASS/FAIL 表。

【口径纪律（MEMORY §四 + R40）】
- 折/段聚合型指标（折均值 Sharpe、正收折数）是**划分方式依赖**的统计量，
  不是策略属性；只能做对照，不能做唯一裁决依据（R18/R40 已证）。
- 决策口径用 ``strategy/kpi_stats.py`` 的 ``span_sharpe``（划分不变量）与
  ``month_win_rate``（大样本），但**是否用它替代旧口径属 OWNER 裁决（D4/D9）**，
  本文件只登记为 ``proposed``，不擅自切换。

【状态语义】
- ``active``    ：参与 gate 的 PASS/FAIL 判定（当前生效的考核目标）。
- ``proposed``  ：度量已可算，但替代旧口径的裁决未下（D4/D9），只展示不 gate。
- ``deprecated``：已被证伪（如正收折数），保留登记以留痕，不 gate、不展示 PASS。
"""
from __future__ import annotations

from typing import Any, Dict, List


# 【显著性门槛（P0-2，2026-10-22）】
# 证据：reports/C5_SIGNIFICANCE_AUDIT_2026-10-22.md —— 4 个已落盘参数的
# dSR 95%CI 全部跨 0、DSR@100 均 ~0.897 < 0.95；晋升闸门②从未做多重检验
# 校正。本组常量是闸门②显著性护栏的单一来源：
#   dsr_min             —— Deflated Sharpe 显著度下限（>0.95 视为显著）；
#   p_max               —— 置换检验（sign-flip）单尾 p 值上限（诊断用）；
#   n_trials_conservative / n_trials_optimistic —— DSR 的试验次数假设。
SIGNIFICANCE: Dict[str, Any] = {
    "dsr_min": 0.95,
    "p_max": 0.05,
    "n_trials_conservative": 100,
    "n_trials_optimistic": 10,
}


# 每个 KPI 的判定：op 取值 ">=" 或 "<="，pass = op(value, target)。
# metric 字段是字符串键，由 gate 侧（_evolve_wf）负责计算后塞进 metrics dict。
KPI: Dict[str, Dict[str, Any]] = {
    "kpi1": {
        "label": "wf 折均值 Sharpe",
        "metric": "fold_mean_sharpe",
        "target": 1.6,
        "op": ">=",
        "status": "active",
        "note": "OWNER 2026-09-19 由 2.0 修订为 1.6（天花板 +1.52，仅新数据轴可突破）。"
                "R18 已证其划分依赖；span_sharpe 替代口径见 kpi1_span（D4 待裁决）。",
    },
    "kpi1_span": {
        "label": "拼接 Sharpe（划分不变量）",
        "metric": "span_sharpe",
        "target": 1.6,
        "op": ">=",
        "status": "proposed",
        "note": "D4 待裁决是否替代 kpi1 作考核口径（R18 实测拼接口径 ≈1.98，折均值 ≈1.67）。",
    },
    "kpi2": {
        "label": "wf 正收益折数（≥6/7）",
        "metric": "fold_positive_ratio",
        "target": 6.0 / 7.0,
        "op": ">=",
        "status": "deprecated",
        "note": "R40 已证划分依赖（同一曲线 50%~77.8% 摆动、7 种划分全 FAIL）；"
                "D9 建议撤销或改大样本口径，见 kpi2b。",
    },
    "kpi2b": {
        "label": "日历月正收益占比",
        "metric": "month_win_rate",
        "target": 0.55,
        "op": ">=",
        "status": "proposed",
        "note": "D9 待裁决的替代口径（R40 实测 53.6%，尚未达 0.55 线）。",
    },
    "kpi3": {
        "label": "最差单折收益",
        "metric": "worst_fold_return",
        "target": -0.15,
        "op": ">=",
        "status": "active",
        "note": "任一分段收益不得低于 -15%（防尾部爆仓）。",
    },
    "kpi4": {
        "label": "相对等权买入持有 alpha（全样本）",
        "metric": "full_sample_alpha",
        "target": -0.30,
        "op": ">=",
        "status": "active",
        "note": "★ 主 KPI（OWNER 2026-09-19 定位：策略真价值=风控，非收益生成）。"
                "= run_backtest().alpha = 策略 total_return − 等权买入持有 total_return。",
    },
    "kpi_sig": {
        "label": "晋升闸门②显著性（DSR/置换p）",
        "metric": "gate_significance",
        "target": 0.95,
        "op": ">=",
        "status": "proposed",
        "note": "P0-2 显著性护栏：基线全样本日收益的 Deflated Sharpe（"
                "n_trials=100）须 >= SIGNIFICANCE['dsr_min']；证据见 "
                "reports/C5_SIGNIFICANCE_AUDIT_2026-10-22.md（DSR@100~0.897）。"
                "仅 --strict-significance 时参与总体裁决，默认不 gate。",
    },
    "kpi4_live": {
        "label": "实盘相对基准超额",
        "metric": "live_excess_pct",
        "target": 0.0,
        "op": ">=",
        "status": "active",
        "note": "由 B4 基准管道 snapshot()['benchmark'].excess_pct 提供；"
                "gate CLI 需 --live <json> 注入，否则显示 n/a 且不参与判定。",
    },
}


def evaluate(metrics: Dict[str, float]) -> List[Dict[str, Any]]:
    """按 KPI 注册表对已算好的 metrics 打分，返回逐项结果行。

    ``metrics`` 键 = 各 KPI 的 ``metric`` 字符串。缺失的度量值记为 None，
    该项 ``pass=None``（无法判定），active 且缺失时不影响总体（见 caller）。
    """
    rows: List[Dict[str, Any]] = []
    for kid, spec in KPI.items():
        value = metrics.get(spec["metric"])
        if value is None:
            passed = None
        elif spec["op"] == ">=":
            passed = bool(value >= spec["target"])
        elif spec["op"] == "<=":
            passed = bool(value <= spec["target"])
        else:
            passed = None
        rows.append({
            "id": kid,
            "label": spec["label"],
            "value": value,
            "target": spec["target"],
            "op": spec["op"],
            "status": spec["status"],
            "pass": passed,
            "note": spec["note"],
        })
    return rows


def gate_verdict(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """仅对 ``active`` 且可判定（pass 非 None）的项做总体裁决。

    返回 {"all_active_pass": bool, "failed": [id...], "gated": [id...]}。
    """
    gated = [r for r in rows if r["status"] == "active" and r["pass"] is not None]
    failed = [r["id"] for r in gated if r["pass"] is False]
    return {
        "all_active_pass": (not failed),
        "failed": failed,
        "gated": [r["id"] for r in gated],
    }

# B10 实施规格（KPI 代码化）

> 依据 `reports/OPTIMIZATION_REVIEW_2026-10-21_multimodel.md` §4 的 B10。
> 目标：把 KPI 目标值/口径/状态从文档收敛到代码单一来源，并用 `--mode gate` 机器判定。

## 交付

1. `config/kpi_targets.py`（新建）
   - `KPI` 注册表：id → {label, metric, target, op, status, note}。
   - `status`：`active`（参与 gate）/ `proposed`（D4·D9 待裁决替代口径，只展示）/ `deprecated`（已证伪，留痕不 gate）。
   - `evaluate(metrics) -> rows`：逐项 PASS/FAIL/n-a。
   - `gate_verdict(rows) -> {all_active_pass, failed, gated}`：仅 active 且可判定项参与总体裁决。

2. `strategy/_evolve_wf.py`
   - `--mode` 增加 `gate`；新增 `--live <json>`（注入 `excess_pct` 供 kpi4_live）。
   - gate 分支：复用已算好的 `base_is`/`b_stats`，算 `span_sharpe`/`month_win_rate`（`strategy.kpi_stats`），
     `full_sample_alpha = base_is["alpha"]`，调用 `KT.evaluate`/`KT.gate_verdict` 输出表格，
     `sys.exit(0 if all_active_pass else 3)`。

3. `tests/test_kpi_gate.py`（4 例，纯函数，无回测）并注册进 `tests/run_all.py`。

## 口径

| id | 度量 | 目标 | 状态 |
|---|---|---|---|
| kpi1 | 折均值 Sharpe | ≥1.6 | active |
| kpi1_span | 拼接 Sharpe（划分不变量） | ≥1.6 | proposed（D4） |
| kpi2 | 正收折数 | ≥6/7 | deprecated（R40） |
| kpi2b | 日历月正收占比 | ≥0.55 | proposed（D9） |
| kpi3 | 最差单折收益 | ≥−15% | active |
| kpi4 | 相对等权买入持有 alpha | ≥−30pt | active（主 KPI） |
| kpi4_live | 实盘相对基准超额（B4 管道） | ≥0 | active（需 `--live`） |

## 落地时的口径发现

`--mode gate` 实测当前基线 `kpi4 = +172.9pt`，与文档长期引用的「−111pt/−90pt」**不同口径**
（后者来自旧一次性脚本/旧参数）。代码化后该漂移第一次被自动暴露；结论以本 gate 为准。

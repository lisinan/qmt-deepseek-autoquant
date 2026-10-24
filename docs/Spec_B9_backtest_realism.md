# B9 回测真实性 —— 涨跌停不可成交 + 时点宇宙（Spec）

> 依据 `reports/OPTIMIZATION_REVIEW_2026-10-21_multimodel.md` §3.1 的 B9 / P1。
> 本文件只覆盖 B9 的**涨跌停不可成交**子项（时点宇宙 ST/退市/未上市剔除另行交付）。

## 问题（评审已认定 §3.1-P1）

`strategy/backtest_daily.py::run_backtest` 让买单在次日 `open` 无条件成交，
唯一拦截是 `panel[code]["valid"]`（=停牌）。而策略语义是「买 60 日动量最强、
创 20 日新高的票」——这类票信号次日开盘常一字/大幅涨停，实盘买不到；
离场侧在跌停封死时同样卖不出。回测因此在最关键的极端场景**双向美化**。

## 开关语义

`BacktestConfig` 新增三个字段（默认值全部 = 现状，**逐位零行为变化**）：

| 字段 | 默认 | 含义 |
|---|---|---|
| `limit_up_block` | `False` | 涨停禁买 / 跌停禁卖总开关 |
| `limit_pct_main` | `0.10` | 主板（含中小板）涨跌停幅度 |
| `limit_pct_growth` | `0.20` | 创业板 `300/301`、科创板 `688` 涨跌停幅度 |

### 为什么默认 False

P0 基线被 3 条数值守卫钉死（`tests/_baseline_tushare_panel.py`、
`test_entry_half_when_one`、`test_trend_break_slower_exit` 等），其常量在
「按次日 open 无条件成交」的旧语义下校准。一旦默认改为 True，极端场景（一字
涨停/跌停）的成交结果全部改变 ⇒ 全部历史结论与 KPI 作废。故与其它「回测有、
实盘无」的代理开关同守则：**默认 False = 零行为变化**，显式 True 才启用。

### 成交判定（`limit_up_block=True` 时生效）

涨停价 = `round(前收 × (1 + limit), 2)`；跌停价 = `round(前收 × (1 - limit), 2)`。
前收用 panel 里**已有的 close 序列**自算（成交日的前一交易日收盘），
**不新增任何数据依赖**；停牌（`valid=False`）逻辑保持不变。

- **买单（涨停禁买）**：若成交日 `open` 或 `high` 触及涨停价 ⇒ 视为不可成交，
  **放弃**该笔，`limit_blocked_buy += 1`。
  - 理由（放弃 vs 顺延）：动量信号是短周期，「创 20 日新高」信号若顺延数日直到
    不再涨停才买，买到的已是**钝化、充分延展**的名字，不再是当初被验证的信号；
    且放弃无需跨日状态机、无「陈旧信号复活」偏差，能以最保守方向度量错过成本。
- **卖单（跌停禁卖）**：若跌停封死（当日 `low` 触及跌停价）⇒ **顺延**到下一
  交易日，持仓保留、次日 B 段重新判定，`limit_blocked_sell += 1`。
  - 理由：持仓已在手，「跌停卖不出」的自然语义就是**持有过夜、次日再试**，不是
    放弃；顺延不引入新的仓位状态，次日离场判定天然复用现有逻辑。

## 返回 dict 增量

`limit_blocked_buy` / `limit_blocked_sell` 两个计数（默认 False 时恒为 0），
便于观察开关开启后的影响面。

## 开启后必须 re-baseline 的流程

`limit_up_block` 默认关闭，**不影响任何既有基线**。只有当研究需要评估「真实
摩擦后的 alpha」时，才显式 `replace(base_cfg(), limit_up_block=True)`。开启后：

1. **不要**改 `base_cfg()` 的默认值，也不要改 `_evolve_wf.py`；
2. 用 `tests/_baseline_tushare_panel.py::baseline_is_result` 分别在
   `limit_up_block=False / True` 上跑，比对 `total_return / sharpe /
   max_drawdown / n_trades` 与 `limit_blocked_buy / limit_blocked_sell`；
3. 若开启后的 Sharpe 相对 P0 下降、且下降幅度超过 +0.10 门级，说明「回测漂亮、
   实盘亏钱」的缺口真实存在 ⇒ 需将结论登记为**新口径下的 P0 基线**，并把
   所有后续 dSh 增量改以该口径为基准（与历次「基线同步」同型，见
   `strategy/_evolve_wf.py::base_cfg` 注释）；
4. 除非 OWNER 明确裁决，**不得**把 `limit_up_block=True` 并入 `base_cfg()`，
   以免静默改写全部已钉 KPI。

## 测试

`tests/test_limit_up_block_flag.py`：默认 False 逐位等同基线；合成「次日一字涨停」
票断言买入被拦且 `limit_blocked_buy>0`；合成「次日跌停」票断言卖单顺延且
`limit_blocked_sell>0`；纯函数验证涨停价四舍五入与 20% 板块判定。

## 并列基线登记（2026-10-22，B9 收尾）

依据 `reports/B9_LIMIT_UP_IMPACT_2026-10-22.md`，`limit_up_block=True` 在此登记为
**「真实摩擦口径」并列基线**：它不是对既有 P0 基线的覆盖，而是与其**并列**的
第二口径，专门用于「实盘预期校准」——凡涉及「实盘 vs 回测」差异、买不到/卖不出
alpha 定价的裁决，一律以真实摩擦口径为准；凡涉及策略研究增量（dSh 演进）的
对比，仍以默认口径（False）为基准，保证与全部已钉数值守卫可比。

登记内容（确定性 tushare 面板，23 只 × 910 根日线，20230105 → 20261013）：

| 口径 | limit_up_block | IS Sharpe | 全样本 alpha | 说明 |
|---|---|---|---|---|
| 默认口径（既有 P0，不变） | False | 1.866 | +119.6pt | 次日 open 无条件成交，全部历史结论与 KPI 沿用 |
| **真实摩擦口径（本登记）** | True | **1.750** | **+81.8pt** | 涨停禁买/跌停禁卖，实盘预期校准用 |

配套量测：被拦买单 5 笔，占到达判定阶段买单（qty_intended=167）的 **3.0%**；
被拦标的信号后 5 日等权平均收益 **+30.5%**（即策略放弃的恰是继续上行的最强票）。
OOS 四窗最差 dSh -0.278（75×7）；两态 KPI 均 PASS。

**kpi4 口径注**（记于本 Spec，不改 `config/kpi_targets.py` 的目标值或 note——
避免与并行 agent 的写入冲突；如主线后续决定固化，再把此句追加进 kpi4 note）：
> 口径注：limit_up_block=True 下 alpha 为 +81.8pt（真实摩擦口径），默认口径 +119.6pt。

裁决状态：并列登记、非 re-baseline——默认值保持 False，`base_cfg()` 不动，
既有 3 条数值守卫与全部已钉 KPI 不受影响；是否并入默认待 OWNER 明确裁决。

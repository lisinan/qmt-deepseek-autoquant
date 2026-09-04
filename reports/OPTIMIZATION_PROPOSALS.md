# 迭代优化提案（OPTIMIZATION PROPOSALS）

> ⚠️ **待用户确认后由人工落地，本自动化绝不自动改参数。**
> 本文件仅收录 FORBIDDEN 类发现（涉及生产配置旋钮：STRATEGY_PARAMS、regime_mode、max_drawdown_pct、dd_recover_days、max_positions、exit_mode、risk_per_trade、max_single_position_pct、EXECUTION_MODE、断路器阈值/持仓上限/风险预算/执行模式等）。SAFE 类（纯代码健壮性修复）若已落地则直接改代码并跑测试，不在此列。

---

## 2026-09-02 ｜ 每日风险预算 WARNING 仍出现 7 次（max_single_position_pct 自洽性待确认）

- **证据**：notices 含「风险预算/max_single_position_pct」关键词 7 次。
- **建议**：确认 `config/settings.py` 的 `max_single_position_pct=0.19` 已与现金夹紧（max_position_amount=300000 / max_order_amount=300000）自洽；若告警持续，进一步排查波动率目标仓位对低波大票的裁剪是否过激（可能把本可容纳的仓位被单标的占比上限砍掉，触发每日风险预算 WARNING）。
- **定位**：`config/settings.py`（第 291-323 行区域，STRATEGY_PARAMS 与 risk 配置段）。
- **背景（当前铁律）**：生产配置 `max_positions=5`、`max_single_position_pct=0.19`、`risk_per_trade=0.02`、`max_position_amount=300000`、`EXECUTION_MODE=paper`。任何偏离须经用户明确确认。
- **用户确认后核查结论（2026-09-04）**：根因是 `strategy/review_daily.py` 的**误计数**（SAFE 代码缺陷）——`_notice_risk_budget` 的常规 SYSTEM 级播报（当前参数下最坏情形≈-17% < 断路器-25%、over=False）也含「风险预算实测值」字样，被原第 724–726 行无条件 +1，误标为 7 次 WARNING。真实配置自洽，**无需调参**。
- **落地动作**：仅把「真实 WARNING 级」风险预算播报计入 `budget_n`（`review_daily.py` 第 724–729 行），已修复；未改动任何生产配置旋钮。
- **状态：已确认并修复（SAFE 代码修复，未改参数）**

# 迭代优化提案（OPTIMIZATION PROPOSALS）

> ✅ **2026-09-16 起策略已变更（用户授权主动进化）**：
> 本文「FORBIDDEN / 绝不自动改参数 / 待用户确认」的路由规则**已作废**。
> 策略参数与交易规则不再是禁区——用户明确要求撤销该限制，因为它使系统在账户
> 真实亏损 -19.6% 期间也只能写提案、无法落地任何改善。
> **新规则**：所有候选改动统一走「晋升闸门」科学验证，通过后即**自动落盘** `config/settings.py`
> 并记账 `reports/EVOLUTION_DECISIONS.md`；未通过则记 REJECTED + 负面证据。
> 晋升闸门：① IS 有提升 ② OOS walk-forward 均值 Sharpe 提升 ≥ +0.10 ③ OOS 与 IS Sharpe 差距
> ≤ 25% ④ walk-forward MDD ≥ -22% ⑤ 无单折收益 < -15% ⑥ `tests/run_all.py` 全通过
> ⑦ 风险底线不动（max_drawdown_pct ≤ -0.15、risk_per_trade ≤ 0.02、T+1 保持、max_positions ≥ 3）
> ⑧ EXECUTION_MODE 保持 paper。
> 以下历史条目保留备查，其中标 FORBIDDEN 者现按上述新规则重新候选，不再需要人工确认。

---

## 2026-09-02 ｜ 每日风险预算 WARNING 仍出现 7 次（max_single_position_pct 自洽性待确认）

- **证据**：notices 含「风险预算/max_single_position_pct」关键词 7 次。
- **建议**：确认 `config/settings.py` 的 `max_single_position_pct=0.19` 已与现金夹紧（max_position_amount=300000 / max_order_amount=300000）自洽；若告警持续，进一步排查波动率目标仓位对低波大票的裁剪是否过激（可能把本可容纳的仓位被单标的占比上限砍掉，触发每日风险预算 WARNING）。
- **定位**：`config/settings.py`（第 291-323 行区域，STRATEGY_PARAMS 与 risk 配置段）。
- **背景（当前铁律）**：生产配置 `max_positions=5`、`max_single_position_pct=0.19`、`risk_per_trade=0.02`、`max_position_amount=300000`、`EXECUTION_MODE=paper`。任何偏离须经用户明确确认。
- **用户确认后核查结论（2026-09-04）**：根因是 `strategy/review_daily.py` 的**误计数**（SAFE 代码缺陷）——`_notice_risk_budget` 的常规 SYSTEM 级播报（当前参数下最坏情形≈-17% < 断路器-25%、over=False）也含「风险预算实测值」字样，被原第 724–726 行无条件 +1，误标为 7 次 WARNING。真实配置自洽，**无需调参**。
- **落地动作**：仅把「真实 WARNING 级」风险预算播报计入 `budget_n`（`review_daily.py` 第 724–729 行），已修复；未改动任何生产配置旋钮。
- **状态：已确认并修复（SAFE 代码修复，未改参数）**

---

## 2026-09-15 ｜ 连亏 4 次触发（halt_recover_days 断路器冷却，待人工确认是否调整）

- **证据**：2026-09-15 当日最大连亏 4 次（688072 盛美上海 −3593、688082 −756 两笔水下老仓退出），notices 记录连亏计数上升。
- **建议**：连亏/日亏熔断冷却 `halt_recover_days=1` 应已自动恢复；若频繁触发，应查**信号质量**（如水下老仓 688072/688082 退出属趋势破位止损，系正常风控动作）而非放宽阈值。**本自动化不改动任何断路器/风险旋钮**。
- **定位**：`config/settings.py` 第 336 行 `halt_recover_days`（连亏/日亏熔断冷却 N 日自动恢复，生产铁律=1）。属 FORBIDDEN 类生产配置旋钮（断路器阈值）。
- **背景（当前铁律）**：生产配置 `halt_recover_days=1`、`max_drawdown_pct=-0.25`、`dd_recover_days=5`、`max_positions=5`、`exit_mode=trend+波动率目标仓位`、`risk_per_trade=0.02`、`EXECUTION_MODE=paper`。任何偏离须经用户明确确认。
- **状态：待确认**

---

## 2026-09-16 ｜ P0 触发 2 次熔断（consec_loss=5 / 回撤断路器冷却，待人工确认是否调整）

- **证据**：`logs/review_2026-09-16.json` 的 `risk.halt_events` 当日 2 次 `consec_loss=5`（09:15:38 notice 源 + snapshot 源双源统计一致）；`halt_reasons={"consec_loss=5": 55}`；`max_consecutive_losses=9`；`last_daily_pnl=-188766.82`、当日 realized -189441、日收益 -0.25%。
- **建议**：连亏/日亏熔断冷却 `halt_recover_days=1`、回撤断路器冷却 `dd_recover_days=5` 均为「可自动恢复断路器」，复盘时确认**次日已 resume** 即可；若频繁触发应查**信号质量**（688012.SH/688082.SH 等水下老仓趋势破位止损属正常风控动作），**勿放宽** `consec_loss=5` 与 `max_consecutive_losses` 阈值。本自动化不改动任何断路器/风险旋钮。
- **定位**：`core/risk_manager.py`（断路器阈值 `consec_loss=5`、`max_consecutive_losses`）；`config/settings.py` 的 `max_drawdown_pct` / `dd_recover_days`（回撤断路器）。均属 FORBIDDEN 类生产配置旋钮（断路器阈值 / 回撤窗口）。
- **背景（当前铁律）**：`max_drawdown_pct=-0.25`、`dd_recover_days=5`、`halt_recover_days=1`、`max_positions=5`、`exit_mode=trend+波动率目标仓位`、`risk_per_trade=0.02`、`EXECUTION_MODE=paper`。任何偏离须经用户明确确认。
- **状态：已确认（2026-09-16，用户确认维持现状，不放松断路器阈值）**

---

## 2026-09-16 ｜ P0 EOD 净持仓 9 > max_positions=5（疑似旧数据假象，待 prune-db 复核定性）

- **证据**：`logs/review_2026-09-16.json` 的 `eod_positions` 列 9 只（000977/601138/688041/688347/601869/002415/300604/301165/688120）；但**同一报表 `position_warning` 已明示「持仓只数对不上：fills 重放算出 9 只，而当日末权益快照记录为 0 只」**，且 `eod.market_value=0.0`、`eod.cash=803680.18`（全现金）。结合 `equity_series` 收盘后 total_asset==cash 全现金，强烈提示为**旧 paper 会话 / 测试桩遗留数据假象**，非真实超限。
- **建议**：**先** `python main.py --prune-db N` 清理旧会话数据重跑，确认是否真实超限；若确超限再查建仓并发闸门（engine 现金夹紧 / 日线闸门 / `--stop` 后重启加载），**勿直接放宽 `max_positions`**。本自动化不改动任何持仓上限/风险旋钮，也不执行 prune-db（属用户账本操作，须人工确认）。
- **定位**：`config/settings.py` 的 `max_positions`（持仓上限旋钮，FORBIDDEN）；`engine/event_engine.py` 现金夹紧 / 建仓并发闸门（仅「若确超限」才查，不应自动放宽阈值）。
- **背景（当前铁律）**：`max_positions=5`、`enable_rotation=True`（弱换强，满仓时分批换仓上限 3/次、全局冷却 1800s）。任何偏离须经用户明确确认。
- **状态：已确认（2026-09-16，用户确认维持 max_positions=5；9 只持仓按 review 提示疑为旧会话/测试桩假象，已执行 --db-stats + --prune-db 120 --dry-run 只读复核，见下）**
- **复核结论（2026-09-16 只读）**：`--db-stats` 显示全部数据起于 2026-08-25（无超 120 天旧会话）；`--prune-db 120 --dry-run` 命中 0 行（无需清理）；`equity_snapshots` 09-16 EOD `market_value=0 / cash=803,680` → 引擎权威账本当前 **0 持仓、全现金**。**判定为假阳性**：「9 只」系 review 脚本跨旧 paper 会话重放 fills（有 BUY 无 SELL）的口径假象，非真实超限。`max_positions=5` 维持，无删数据动作、未跑 --purge-test-stubs。

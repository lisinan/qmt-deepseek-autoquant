# 递归自进化 · 元审计（2026-11-01 21:08）

> 二阶优化：不改策略参数，改「改进流程本身」。
> 扫描文件：实盘侧 34 个，研究侧 4 个（注释已剥离，只数真实代码读点）

## 〇、活性三态（首屏必读）

**① 正常产出 6 / ② 运行但自我停机 2 / ③ 完全未运行 2**（共 10 个任务）

| 任务 | 态 | 归因 | 证据 |
|---|---|---|---|
| 盘前就绪 PRE-MARKET | ③ 完全未运行 | 醒着但零留痕（任务自身/宿主未触发） | 槽位 10-30 09:05 后无 memory、无产物；窗口内 17 条心跳，最大空档 6 分钟 ⇒ 机器醒着却零留痕，问题在任务自身 |
| 盘后复盘 OBSERVE | ② 运行但自我停机 | prompt 门禁自停 | 末条含停机标记 不执行 Step/整体停机/硬闸门/闸门未通过，且 10-30 15:35 起窗口内无产物；槽位窗口机器休眠（整窗口无心跳） |
| 工程维护 17:00 | ② 运行但自我停机 | prompt 门禁自停 | 末条含停机标记 停止一切落盘，且 10-30 17:00 起窗口内无产物；槽位窗口机器休眠（最大心跳空档 36.9 分钟） |
| 周中自进化 PM-EVOLVE | ③ 完全未运行 | 醒着但零留痕（任务自身/宿主未触发） | 槽位 10-28 18:00 后无 memory、无产物；窗口内 16 条心跳，最大空档 5 分钟 ⇒ 机器醒着却零留痕，问题在任务自身 |

> 归因判据（契约 §4.3）：`logs/guardian.log` 300s 心跳缺失 = 整机休眠；心跳连续却零产物 = 任务自身/prompt 门禁。

## 一、搜索空间审计

生产参数 **79** 个，其中**双向可验证 50 个（63%）**。

| 分类 | 数量 | 含义 |
|---|---:|---|
| 双向可验证 | 50 | 同名读取，闸门可验证 |
| 仅实盘 | 25 | 回测器不读该键 → A/B 测的是另一个策略，结论只能记「未验证」 |
| 死参数 | 3 | 全仓库生产代码无读点 → 任何网格纯浪费 |
| 别名可验证 | 1 | 经别名接线：max_order_amount → 回测字段 fixed_amount |

### 死参数（3 个）

| 参数 | 实盘 | 回测 | 别名接线 | 实盘位置 |
|---|---:|---:|---|---|
| `rsi_oversold` | 0 | 0 | — | — |
| `rsi_overbought` | 0 | 0 | — | — |
| `max_position_amount` | 0 | 0 | — | — |

### 仅实盘（25 个）

| 参数 | 实盘 | 回测 | 别名接线 | 实盘位置 |
|---|---:|---:|---|---|
| `ma_short` | 1 | 0 | — | `trend_strategy.py` |
| `ma_medium` | 1 | 0 | — | `trend_strategy.py` |
| `ma_long` | 3 | 0 | — | `event_engine.py`, `_probe_livedata.py`, `trend_strategy.py` |
| `rsi_period` | 1 | 0 | — | `trend_strategy.py` |
| `macd_fast` | 1 | 0 | — | `trend_strategy.py` |
| `macd_slow` | 1 | 0 | — | `trend_strategy.py` |
| `macd_signal` | 1 | 0 | — | `trend_strategy.py` |
| `kdj_period` | 1 | 0 | — | `trend_strategy.py` |
| `boll_period` | 1 | 0 | — | `trend_strategy.py` |
| `boll_std` | 1 | 0 | — | `trend_strategy.py` |
| `atr_period` | 1 | 0 | — | `trend_strategy.py` |
| `volume_surge` | 1 | 0 | — | `trend_strategy.py` |
| `require_daily_data` | 2 | 0 | — | `event_engine.py`, `trend_strategy.py` |
| `momentum_scope` | 1 | 0 | — | `event_engine.py` |
| `manual_entry_amount` | 1 | 0 | — | `event_engine.py` |
| `exit_exempt_codes` | 1 | 0 | — | `event_engine.py` |
| `enable_rotation` | 1 | 0 | — | `event_engine.py` |
| `rotation_cooldown_sec` | 1 | 0 | — | `event_engine.py` |
| `daily_loss_limit_pct` | 1 | 0 | — | `manager.py` |
| `daily_loss_limit_abs` | 1 | 0 | — | `manager.py` |
| `dd_recover_days` | 2 | 0 | — | `event_engine.py`, `manager.py` |
| `halt_recover_days` | 1 | 0 | — | `manager.py` |
| `max_consecutive_losses` | 3 | 0 | — | `manager.py`, `record_ledger.py`, `review_daily.py` |
| `max_consecutive_losses_halt` | 1 | 0 | — | `manager.py` |
| `max_daily_trades` | 1 | 0 | — | `manager.py` |

## 二、反馈信号可信度：P0 基线漂移

逐字段比对 `_evolve_wf.base_cfg()`（验证器基线）与 `config/settings.py`（生产配置）：可比 **53** 项，**漂移 1 项**，未映射 75 项。

| 回测字段 | 回测值 | 生产键 | 生产值 | 判定 |
|---|---:|---|---:|---|
| `manual_entry_codes` | () | `manual_entry_codes` | ['510050.SH'] | ★漂移 |

> 漂移后果：此后所有「dSh 增量 / KPI」都相对错误基线计算。
> 本项目 docstring 已记录过 13 次同类漂移（top_n / risk_per_trade / fixed_amount / northbound_mode / momentum_min_mom …）。

未映射回测字段（75，需人工确认是否应接生产键）：`use_gate`、`entry_half_when_one`、`limit_up_block`、`limit_pct_main`、`limit_pct_growth`、`t1_restriction`、`trend_up_relax`、`max_per_sector`、`vwap_factor_fixed`、`vwap_window`、`breakout_bypass_gate`、`breakout_bypass_score`、`rotation_enabled`、`rotation_weak_to_strong`、`rotation_oversize_on_t1_block`、`consec_loss_scale`、`consec_loss_halt_bars`、`consec_loss_floor`、`consec_loss_batch`、`daily_loss_halt`、`daily_loss_halt_bars`、`manual_entry_slots`、`trend_breakout_near_high`、`trend_breakout_window`、`mom_rel_fallback`、`mom_rel_floor`、`min_warmup`、`regime_confirm_days`、`regime_buffer_pct`、`regime_slope_days`、`tail_index`、`tail_drawdown_pct`、`tail_recover_pct`、`tail_force_exit`、`mom_metric`、`trend_trailing`、`trend_trail_activation`、`trend_trail_stop`、`trend_trail_floor`、`trend_atr_stop_mult`、`trend_chandelier_mult`、`trend_break_confirm_days`、`trend_break_buffer_pct`、`dd_ctrl`、`dd_ctrl_start`、`dd_ctrl_floor`、`dd_ctrl_full`、`reentry_cooldown`、`recovery_fastpath`、`rf_drawdown_threshold`、`rf_lookback`、`rf_rebound_pct`、`rf_relax_days`、`rf_mom_threshold`、`entry_max_day_gain_pct`、`momentum_weight`、`vol_confirm`、`vol_confirm_mult`、`vol_confirm_ma`、`vol_confirm_tol`、`risk_parity`、`moneyflow_mode`、`moneyflow_window`、`moneyflow_min_amount`、`moneyflow_weight`、`earnrev_mode`、`earnrev_window`、`earnrev_min`、`earnrev_weight`、`equity_dd_stop_enable`、`equity_dd_stop_pct`、`equity_dd_resume_pct`、`rm_dd_pause_pct`、`rm_dd_pause_recoverable`、`rm_dd_pause_bars`

## 三、循环活性（逐任务明细）

| 任务 | 应跑槽位 | 最近 memory | 态 | 归因 | 错过槽位 |
|---|---|---|---|---|---:|
| 盘前就绪 PRE-MARKET | 2026-10-30 09:05 | 2026-10-29 09:31 | ③ 完全未运行 | 醒着但零留痕（任务自身/宿主未触发） | 1 |
| 上午自进化 AM-EVOLVE | 2026-10-30 11:40 | 2026-10-30 11:47 | ① 正常产出 | — | — |
| 盘后复盘 OBSERVE | 2026-10-30 15:35 | 2026-10-30 17:27 | ② 运行但自我停机 | prompt 门禁自停 | — |
| 开仓归因 WHY-NO-ENTRY | 2026-10-30 15:55 | 2026-10-30 17:30 | ① 正常产出 | — | — |
| 量测卫生 METRIC-HYGIENE | 2026-10-30 16:20 | 2026-10-30 17:36 | ① 正常产出 | — | — |
| 工程维护 17:00 | 2026-10-30 17:00 | 2026-10-30 17:42 | ② 运行但自我停机 | prompt 门禁自停 | — |
| 守护兜底 GUARD | 2026-10-30 16:00 | 2026-10-30 22:32 | ① 正常产出 | — | — |
| 周中自进化 PM-EVOLVE | 2026-10-28 18:00 | 2026-10-21 18:31 | ③ 完全未运行 | 醒着但零留痕（任务自身/宿主未触发） | 1 |
| 周度深度 SYNTHESIZE | 2026-10-25 20:00 | 2026-11-01 20:20 | ① 正常产出 | — | — |
| 递归自进化 RSI | 2026-10-25 21:00 | 2026-10-29 20:19 | ① 正常产出 | — | — |

> 「应跑槽位」按契约 §五 的时段表计算（`scripts/_loop_liveness.py::TASKS`），不是按 rrule 字面：GUARD 的 rrule 全天每小时，但职责只到 15:00。
> 判据：② = 槽位后写过 memory 且末条含停机标记且窗口内无产物；③ = 槽位后既无 memory 也无产物。

## 四、决策账本

- 体积 642.1 KB / 9010 行
- 轮次标记 **66**，落盘提及 **12**，否决/回滚提及 **209**，已证伪/勿重试提及 **175**
- 「落盘 ÷ 轮次」≈ **12/66** —— ★ **这不是迭代效率**：右式三项都是**词频**（正则 `REJECTED|已否决|回滚` 等），不是结构化事件数。
  实证：本文件每追加一段带「否决/回滚」字样的说明，计数就 +1~2，而轮次未变。⇒ 真正的命中率/浪费率不可由词频推出（P0-b 待做）。

## 五、循环能力

| 能力 | 状态 | 缺失后果 |
|---|---|---|
| 划分不变量指标（span_sharpe / month_win_rate） | ✓ | 折均值/正收折数已被证为划分假象（R40） |
| 多窗口共识（4 套折划分取最差窗） | ✓ | 单一折划分可被窗口运气欺骗 |
| P0 不变性基线（逐位相同断言） | ✓ | 研究代码改动后必须证明 P0 未漂移（R39 靠它抓到 off-by-one） |
| 单仓上限守卫（防 wf 绕过 position sizing） | ✓ | R39：资本倾斜类历史 IS 数字曾虚增 +9.4pt |
| 末端 bar 收盘守卫（防未完成 bar 进统计） | ✓ | P2 疑点：回测面板曾并入当日未收盘 bar |
| 参数读点扫描（防死参数/单侧参数进网格） | ✓ | R41 才发现 7 个参数在回测中无读取点 |

## 六、下一轮该改的环节（按优先级）

### [P0] 反馈信号可信度（最高优先） — 修复 1 处 P0 基线漂移

- 对象：`manual_entry_codes` 回测 () ≠ 生产 `manual_entry_codes` ['510050.SH']
- 理由：验证器基线是手抄镜像，漂移后所有 dSh 相对错误基线计算 ⇒ 循环在用一个说谎的尺子量自己

### [P0] 反馈信号可信度 — 把 base_cfg() 改为从 config/settings.py 自动派生 + 漂移守卫测试

- 对象：当前 75 个回测字段无生产键映射
- 理由：根治手抄漂移：让基线无法不同步（可加显式 IGNORE 白名单并注明理由）

### [P1] 搜索空间 — 删除或接线 3 个死参数

- 对象：rsi_oversold、rsi_overbought、max_position_amount
- 理由：任何在这些键上跑的网格，结论必然是 0 变化，纯浪费迭代预算

### [P2] 口径背离 — 把 25 个「仅实盘有读点」参数补进回测器

- 对象：ma_short、ma_medium、ma_long、rsi_period、macd_fast、macd_slow、macd_signal、kdj_period、boll_period、boll_std、atr_period、volume_surge、require_daily_data、momentum_scope、manual_entry_amount、exit_exempt_codes、enable_rotation、rotation_cooldown_sec、daily_loss_limit_pct、daily_loss_limit_abs、dd_recover_days、halt_recover_days、max_consecutive_losses、max_consecutive_losses_halt、max_daily_trades
- 理由：回测测不到该行为 ⇒ 对该键的 A/B 结论不可信（非「无价值」）

### [P0] 循环活性（三态） — 处置 4 个活性异常：自我停机 2 / 未运行 2

- 对象：盘前就绪 PRE-MARKET→③ 完全未运行（醒着但零留痕（任务自身/宿主未触发））；盘后复盘 OBSERVE→② 运行但自我停机（prompt 门禁自停）；工程维护 17:00→② 运行但自我停机（prompt 门禁自停）；周中自进化 PM-EVOLVE→③ 完全未运行（醒着但零留痕（任务自身/宿主未触发））
- 理由：② 自我停机的任务是**在跑但什么都没做**（旧 L3 判为正常）⇒ 修 prompt 门禁；③ 未运行要先看 guardian 心跳：无心跳=整机休眠（无需改代码，改时段），有心跳=任务自身/宿主未触发（改 prompt 或排期）


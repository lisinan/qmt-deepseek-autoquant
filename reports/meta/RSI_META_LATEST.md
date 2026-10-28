# 递归自进化 · 元审计（2026-10-28 23:10）

> 二阶优化：不改策略参数，改「改进流程本身」。
> 扫描文件：实盘侧 33 个，研究侧 4 个（注释已剥离，只数真实代码读点）

## 一、搜索空间审计

生产参数 **76** 个，其中**双向可验证 49 个（64%）**。

| 分类 | 数量 | 含义 |
|---|---:|---|
| 双向可验证 | 49 | 同名读取，闸门可验证 |
| 仅实盘 | 23 | 回测器不读该键 → A/B 测的是另一个策略，结论只能记「未验证」 |
| 死参数 | 3 | 全仓库生产代码无读点 → 任何网格纯浪费 |
| 别名可验证 | 1 | 经别名接线：max_order_amount → 回测字段 fixed_amount |

### 死参数（3 个）

| 参数 | 实盘 | 回测 | 别名接线 | 实盘位置 |
|---|---:|---:|---|---|
| `rsi_oversold` | 0 | 0 | — | — |
| `rsi_overbought` | 0 | 0 | — | — |
| `max_position_amount` | 0 | 0 | — | — |

### 仅实盘（23 个）

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

逐字段比对 `_evolve_wf.base_cfg()`（验证器基线）与 `config/settings.py`（生产配置）：可比 **52** 项，**漂移 0 项**，未映射 75 项。

✅ 基线一致，当前无需修口径。

未映射回测字段（75，需人工确认是否应接生产键）：`use_gate`、`entry_half_when_one`、`limit_up_block`、`limit_pct_main`、`limit_pct_growth`、`t1_restriction`、`trend_up_relax`、`max_per_sector`、`vwap_factor_fixed`、`vwap_window`、`breakout_bypass_gate`、`breakout_bypass_score`、`rotation_enabled`、`rotation_weak_to_strong`、`rotation_oversize_on_t1_block`、`consec_loss_scale`、`consec_loss_halt_bars`、`consec_loss_floor`、`consec_loss_batch`、`daily_loss_halt`、`daily_loss_halt_bars`、`manual_entry_slots`、`trend_breakout_near_high`、`trend_breakout_window`、`mom_rel_fallback`、`mom_rel_floor`、`min_warmup`、`regime_confirm_days`、`regime_buffer_pct`、`regime_slope_days`、`tail_index`、`tail_drawdown_pct`、`tail_recover_pct`、`tail_force_exit`、`mom_metric`、`trend_trailing`、`trend_trail_activation`、`trend_trail_stop`、`trend_trail_floor`、`trend_atr_stop_mult`、`trend_chandelier_mult`、`trend_break_confirm_days`、`trend_break_buffer_pct`、`dd_ctrl`、`dd_ctrl_start`、`dd_ctrl_floor`、`dd_ctrl_full`、`reentry_cooldown`、`recovery_fastpath`、`rf_drawdown_threshold`、`rf_lookback`、`rf_rebound_pct`、`rf_relax_days`、`rf_mom_threshold`、`entry_max_day_gain_pct`、`momentum_weight`、`vol_confirm`、`vol_confirm_mult`、`vol_confirm_ma`、`vol_confirm_tol`、`risk_parity`、`moneyflow_mode`、`moneyflow_window`、`moneyflow_min_amount`、`moneyflow_weight`、`earnrev_mode`、`earnrev_window`、`earnrev_min`、`earnrev_weight`、`equity_dd_stop_enable`、`equity_dd_stop_pct`、`equity_dd_resume_pct`、`rm_dd_pause_pct`、`rm_dd_pause_recoverable`、`rm_dd_pause_bars`

## 三、循环活性

| 任务 | 最近运行 | 距今天数 | 判定 |
|---|---|---:|---|
| 盘后复盘 OBSERVE | 2026-10-21 15:39 | 7.3 | ⚠ 停摆 |
| 盘前就绪 PRE-MARKET | 2026-10-20 09:14 | 8.6 | ⚠ 停摆 |
| 上午自进化 AM-EVOLVE | 2026-10-21 11:55 | 7.5 | ⚠ 停摆 |
| 周中自进化 PM-EVOLVE | 2026-10-21 18:31 | 7.2 | ⚠ 停摆 |
| 周度深度 SYNTHESIZE | 2026-10-10 22:23 | 18.0 | ⚠ 停摆 |
| 守护兜底 GUARD | 2026-10-28 22:18 | 0.0 | ✓ 运行中 |
| 工程维护 17:00 | 2026-10-21 17:05 | 7.3 | ⚠ 停摆 |
| 开仓归因日报 | — | — | 待首跑（新增） |
| 量测卫生守卫 | — | — | 待首跑（新增） |
| 递归自进化 | — | — | 待首跑（新增） |

## 四、决策账本

- 体积 569.5 KB / 8030 行
- 轮次标记 **58**，落盘提及 **9**，否决/回滚提及 **191**，已证伪/勿重试提及 **172**
- 「落盘 ÷ 轮次」≈ **9/58** —— ★ **这不是迭代效率**：右式三项都是**词频**（正则 `REJECTED|已否决|回滚` 等），不是结构化事件数。
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

### [P1] 搜索空间 — 删除或接线 3 个死参数

- 对象：rsi_oversold、rsi_overbought、max_position_amount
- 理由：任何在这些键上跑的网格，结论必然是 0 变化，纯浪费迭代预算

### [P2] 口径背离 — 把 23 个「仅实盘有读点」参数补进回测器

- 对象：ma_short、ma_medium、ma_long、rsi_period、macd_fast、macd_slow、macd_signal、kdj_period、boll_period、boll_std、atr_period、volume_surge、require_daily_data、momentum_scope、enable_rotation、rotation_cooldown_sec、daily_loss_limit_pct、daily_loss_limit_abs、dd_recover_days、halt_recover_days、max_consecutive_losses、max_consecutive_losses_halt、max_daily_trades
- 理由：回测测不到该行为 ⇒ 对该键的 A/B 结论不可信（非「无价值」）

### [P0] 循环活性 — 恢复 6 个停摆/未运行任务

- 对象：盘后复盘 OBSERVE、盘前就绪 PRE-MARKET、上午自进化 AM-EVOLVE、周中自进化 PM-EVOLVE、周度深度 SYNTHESIZE、工程维护 17:00
- 理由：停摆期间没有任何观测与裁决 ⇒ 自进化链条断掉（10-22~10-28 已发生一次）


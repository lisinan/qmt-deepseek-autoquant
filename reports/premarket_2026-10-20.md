# 盘前排雷报告（PRE-MARKET）— 2026-10-20 09:11 GMT+8

> 闭环环节：盘前就绪确认（PRE-MARKET）。目标：开盘前确认系统可交易、双周期自进化改动已生效、无卡住的熔断。
> 执行方式：只读检查 + 告警；未改动任何策略参数 / 生产配置 / live 进程（未 kill 引擎 / miniQMT）。

## 一、在线状态 ✅

| 组件 | 状态 | 依据 |
|---|---|---|
| miniQMT（XtMiniQmt.exe） | **在线** | `scripts/health_check.py --json` → `miniqmt=true` |
| 交易引擎（5000 端口） | **在线** | `health_check.py --json` → `engine=true`；`/api/risk`、`/api/snapshot` 均可达 |
| 数据源 | **实时 xtdata** | `/api/snapshot` → `data_mode=xtdata`（非 mock，R14 闸门生效） |

无需自启动恢复（两者均存活）。

## 二、执行模式（paper 硬要求）✅

| 检查项 | 结果 | 说明 |
|---|---|---|
| `EXECUTION_MODE` | **"paper"** ✅ | `config/settings.py:778` 双确认 + `/api/snapshot.exec_mode=paper` 运行时双确认 |
| `max_drawdown_pct` | −0.20 ≤ −0.15 ✅ | 未放松（严于模板下限） |
| `T1_RESTRICTION` | True ✅ | 未关闭 |
| `max_positions` | 5 ≥ 3 ✅ | 未收紧 |
| `risk_per_trade` | 0.040 | ⚠ **超过任务模板 ≤0.02 旧约束**，但系 **OWNER 2026-10-15 正式固化为 sanctioned 铁律**（R29+L2，commit 184812f 已 push），非未授权放松；与 `max_single_position_pct=0.40`、`max_position_amount=max_order_amount=400000` 协同放宽（合成 ≈2.24× 杠杆）。**按 10-14/10-15/10-16/10-19 既定先例，记为非阻塞、不告警**。 |

**结论：始终 paper，真实交易由用户手动切换；无 live 模式、无风险底线越界告警。**

## 三、陈旧熔断检查 ✅ 无

| 数据源 | 字段 | 值 | 判定 |
|---|---|---|---|
| `storage/qmt.db` engine_state.risk_state | `halted` | false | 无熔断 |
| | `halt_reason` | "" | — |
| | `halt_day` | 2026-10-08 | **已过期**（远早于今日） |
| `/api/risk` 实时 | `halted` | false | 无熔断 |
| | `position_scale` | 1.0 | 仓位未锁死 |
| | `consecutive_losses` | 1 | 连亏计数正常 |
| | `daily_pnl` | 0.0 | 跨日复位生效（R30 修复已随 09:15 重启加载） |
| 持仓 | `positions` | []（0 持仓） | 当前 100% 现金 |

- 无冷却等待、无死锁（09-18/09-21 `position_scale=0.0` 死锁已彻底解除）。
- 单实例锁修复（2026-09-16 `_pid_alive` 改用 `GetExitCodeProcess`）持续生效，进程被强杀后残留旧 PID 锁自动失效。

## 四、双周期进化落地核验（最近两条）

读取 `reports/EVOLUTION_DECISIONS.md` 最近两条记录：

| 周期 | 记录 | 落盘性质 | 结论 |
|---|---|---|---|
| **2026-10-19 AM-EVOLVE（第 37 轮）** | 量化生产两条「风控降级通道」口径背离（连亏阶梯 / 日内亏损熔断） | **生产参数零改动** | 仅新增 `tests/test_derisk_channel_parity.py` + 研究脚本 + `OPEN_DECISIONS.md D6`；无 config 改动 |
| **2026-10-19 PM-EVOLVE（第 38 轮，收官）** | 第 17 例「实盘有、回测无」定价 + 组合级回撤断路器「吸收态」缺陷 | **生产参数零改动** | 落盘清单明示 `config/settings.py` **零改动**；仅 `backtest_daily.py` 新增研究旋钮 `rm_dd_pause_bars`（默认 0，零行为变化）+ 测试 + 研究脚本 |

**核验结论：**

- 两条记录均未改动 `config/settings.py` 任何运行参数 ⇒ **无配置-账本（statebook）偏差**，无需逐项比对当前值。
- 当前 `settings.py` 仍保持 **L2 终态**（已现场复核）：`risk_per_trade=0.040` / `max_single_position_pct=0.40` / `max_position_amount=max_order_amount=400000` / `momentum_min_mom=0.15` / `down_day_exit_pct=-3.25` / `enable_rotation=False` / `momentum_top_n=3` 等，全部与终态一致。
- 无任何 `__init__` 缓存类参数（regime_mode / enable_rotation / rotation_*）在本轮被改 ⇒ **无待重启生效项**。
- 今日 11:40 AM-EVOLVE（第 39 轮）尚未运行（盘前 09:11），符合预期。

## 五、就绪结论 ✅ 可交易

**系统就绪，可开盘。** 无 live 模式、无风险底线越界、无陈旧熔断、无待生效/待重启参数，双周期（昨日 AM R37 + 昨日 PM R38）均为研究/测试类改动、生产参数零改动。

**需用户介入事项：无硬阻塞项。**
- 仅提示（非阻塞）：KPI#4（近 4 周 live 收益）仍为负、连续第 11+ 交易日零 BUY —— 根因是「无标的过入场闸门」（弱市 60 日动量无正值标的），`halted=false` 已确认非熔断所致，属已知口径问题（OWNER 待裁决 D1/D4），不影响开盘。
- `risk_per_trade=0.040` 为 OWNER sanctioned 铁律值，按既定先例不告警；如 OWNER 欲回退需显式指令。

**未改动任何策略参数 / 生产配置 / 进程（未 kill 引擎 / miniQMT）。**

---
*附：本次执行未触发 [PREMARKET-ALERT]（4 关键项——在线 / paper / 熔断 / 双周期落地——均通过）。*

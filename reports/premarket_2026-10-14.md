# 盘前排雷报告 · 2026-10-14（周三）

> 自动执行器：PRE-MARKET 闭环（98ffc7a2）
> 执行时刻：2026-10-14 09:13（GMT+8）
> 引擎进程：PID 7916，启动于 **2026-10-14 08:14:53**
> config/settings.py 最后写入：**2026-10-13 23:05:49**（commit `05e90b9` R29 L2）
> 启动时刻 ≥ 配置 mtime ⇒ 运行实例已加载最新参数 ⇢ 落盘≠生效 已核对通过

## 一、就绪结论

| 项 | 状态 | 说明 |
|---|---|---|
| **可交易判定** | ✅ **可交易** | 系统就绪，无硬阻塞项 |
| 执行模式 | ✅ **paper** | 双确认（config `EXECUTION_MODE="paper"` + /api/snapshot `exec_mode=paper`）|
| 在线状态 | ✅ 双在线 | miniQMT=True、引擎(5000)=True（health_check --json）|
| 陈旧熔断 | ✅ 无 | `halted=false`、`position_scale=1.0`、`consec_loss=1`（未达熔断阈值 5）|
| 双周期进化落地 | ✅ 已生效 | R28 + R29 L2 改动均与 config 相符，且引擎今晨重启后加载 |
| 数据模式 | ✅ xtdata 实时 | 非 mock（对比 09-16 历史 mock 假亏损事件已规避）|

## 二、paper 模式（唯一硬要求）

- `config/settings.py:733` → `EXECUTION_MODE = "paper"` ✅
- 运行时 /api/snapshot `exec_mode=paper` ✅ 双确认
- **未切 live**，无需告警、无需停止后续动作。

## 三、风险底线核对（⚠ 有一项越界，属已知授权例外）

| 底线项 | 要求 | 当前值 | 判定 |
|---|---|---|---|
| `max_drawdown_pct` | ≤ −0.15 | **−0.20** | ✅ 未放松 |
| `T1_RESTRICTION` | = True | **True** | ✅ 未放松 |
| `max_positions` | ≥ 3 | **5** | ✅ 未放松 |
| `risk_per_trade` | ≤ 0.02 | **0.040** | ⚠️ **越界** |

> ⚠ **告警（非阻塞）**：`risk_per_trade=0.040` 超过铁律 ≤0.02，但系 **2026-10-13 用户显式授权越过**（`R29+L2` 杠杆路径，commit `05e90b9`，working-memory 已记录）。属已知、文档化、可逆的授权例外，**非未授权放松**。其余三项底线（含 `max_drawdown_pct −0.20`/`T1_RESTRICTION True`/`max_positions 5`）仍不可动。
>
> 运行时已实测生效：引擎启动通知显示「名义 risk_per_trade=4.0% / 单仓上限=40%」，与 L2 终态一致。最差窗拼接MDD **−13.53%** 仍在 −20% 风险底线与 −22% KPI#3 之内。

## 四、陈旧熔断核对（storage/qmt.db）

- DB `engine_state.risk_state`：`{"halted": false, "halt_reason": "", "halt_day": "2026-10-08", "consec_loss_date": "2026-10-09", "last_loss_day": "2026-10-09"}`
- 实时 /api/risk：`halted=false`、`position_scale=1.0`、`consecutive_losses=1`
- **结论**：无熔断、无死锁、无需手动 `RiskManager.resume()`、无冷却等待。
- 附注：`daily_pnl` 显示 −7964 但持仓=0、position_scale=1.0 —— 为盘前空仓态的估值残值，不构成熔断/杠杆锁，正常。
- 单实例锁 09-16 修复已生效（PID 7916 为今晨干净启动，无旧锁残留）。

## 五、双周期进化落地核验（最近两条）

> 注：今日 11:40 AM-EVOLVE 尚未运行（盘前 09:13），故最近两条均属 2026-10-13（R28 AM + R29 PM），符合「昨日下午 + 今日之前的上午」口径。

### R28 · 2026-10-13 AM-EVOLVE（第 28 轮，实际 19:47 补跑）
- **落盘项**：`STRATEGY_PARAMS["momentum_min_mom"]`：`0.0 → 0.15`（收紧动量入池下限）
- **config 当前值**：`momentum_min_mom = 0.15`（`config/settings.py:328`）✅ 相符
- **生效方式**：`daily_context.top_momentum` 读 `STRATEGY_PARAMS`，但 settings 模块在引擎启动时导入 ⇒ 实际为**配置导入型**；引擎今晨 08:14:53 重启晚于落盘(10-13) ⇒ **已生效**。

### R29 · 2026-10-13 PM-EVOLVE（第 29 轮，@18:00 → 21:0x 续跑，含用户授权）
- **落盘项（L2 杠杆终态）**：
  - `risk_per_trade`：`0.020 → 0.040`
  - `max_single_position_pct`：`0.19 → 0.40`
  - `max_position_amount` / `max_order_amount`：`180000 → 400000`（约 2.24× 杠杆）
- **config 当前值**：`risk_per_trade=0.040`(`:573`)、`max_single_position_pct=0.40`(`:715`)、`max_position_amount=400000`(`:574`)、`max_order_amount=400000`(`:717`) ✅ 全部相符
- **生效方式**：`PositionSizer.__init__` 缓存（event_engine.py:288 读 max_pct/order；:676 读 risk_per_trade；:2162 读 max_position_amount）⇒ **需重启引擎**。引擎今晨 08:14:53 重启晚于落盘(10-13 23:0x) ⇒ **已生效**（运行时通知实测 4.0%/40% 一致）。
- 路径 2（信念仓位 conviction sizing）量测达标但**未落盘**（live 引擎无读取点，按纪律不实装死代码）。

### 待用户介入项
- **无**。R28 / R29 两项改动所需的引擎重启已于今晨 08:14:53 完成（PID 7916），运行实例已从磁盘最新 config 加载。
- 路径 2 信念仓位如需启用，须先补 live 引擎接线（daily_context 算 AI 宽度 + event_engine 乘子 + settings 读取 + 守卫），且需再次重启生效——当前未启用，不影响交易。

## 六、其余被动观测（非阻塞）
- 北向闸门：`loaded=true / mode=gate / blocked=false / credible=false / guard_reason=stale` ⇒ 守卫 fail-open（预期死闸门，自 2024-08-19 起恒正窄带，不拦截建仓）。
- broker=disconnected（paper 正常）。
- 账本 15:35 OBSERVE 历史断供（连续多日）仍走 DB 直读回退，不影响盘前判定。

## 七、结论摘要
✅ **可交易**。paper 模式确认；在线双在线；无熔断无死锁；R28 `momentum_min_mom=0.15` 与 R29 L2 杠杆四参数均已落盘、与 config 相符、且引擎今晨重启后真实生效。
⚠ **唯一告警**：`risk_per_trade=0.040` 超过 ≤0.02 铁律，但为 2026-10-13 用户显式授权例外（R29 L2），非未授权放松，已实测生效。
👤 **无需用户介入**即可开盘；如需启用路径 2 信念仓位则需另行批准并实施 live 接线。

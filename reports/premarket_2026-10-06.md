# 盘前排雷报告 · 2026-10-06（周二，国庆休市期间）

> 环节：PRE-MARKET 闭环排雷（自动化 98ffc7a2）
> 执行时间：2026-10-06 23:31 (GMT+8)
> 结论：**✅ 可交易（系统就绪，待 10-08 开市）** —— 无 live/参数/进程硬阻塞；未改动任何配置或进程。

---

## 一、环境
- 解释器：`C:\Users\lisinan\.conda\envs\qmt\python.exe`
- `PATH` 注入 `Library/bin` + `DLLs`；`PYTHONPATH=.`（sqlite3 不报 DLL 缺失）
- 工作目录：`C:\Users\lisinan\Desktop\qmtIDE-deepseek`

## 二、在线检查（`scripts/health_check.py --json`）
| 组件 | 状态 | 处置 |
|---|---|---|
| miniQMT | `miniqmt=true` ✅ | 在线，无需自启动 |
| 引擎(5000) | `engine=true` ✅ | 在线，无需自启动 |
| 数据源 | `data_mode=xtdata` ✅ | 真实行情（非 mock）；`ALLOW_MOCK_TRADING=False` 闸门生效 |

引擎进程启动于 **2026-10-06 23:30**（早于本次核查），本次未触发任何进程改动。

## 三、唯一硬要求：paper 模式
- **EXECUTION_MODE = "paper" ✅**（config/settings.py:595，且 `/api/snapshot` 实时 `exec_mode=paper` 双确认）
- 若为 live 将立即显著告警并停止后续动作 —— 本次为 paper，无告警。

### 风险底线未被放松
| 参数 | 当前值 | 阈值 | 判定 |
|---|---|---|---|
| `max_drawdown_pct` | −0.20 | ≤ −0.15 | ✅ |
| `risk_per_trade` | 0.012 | ≤ 0.02 | ✅ |
| `T1_RESTRICTION` | True | = True | ✅ |
| `max_positions` | 5 | ≥ 3 | ✅ |

## 四、陈旧熔断检查（`storage/qmt.db` engine_state + 实时 /api/risk）
- DB `engine_state.risk_state`：`halted=false`、`halt_reason=""`、`halt_day=""`、`consec_loss_date=""`、`last_loss_day=""`
- 实时 `/api/risk`：`halted=false`、`consecutive_losses=0`、`position_scale=1.0`
- **结论：未熔断，无需 resume()，无冷却等待。**
- 历史备注：2026-09-30 合成行情假亏损（`daily_loss_abs -35622`）导致的 `halted=true` 已随 09-30 20:44 重启彻底清除；单实例锁误判修复（main.py::_pid_alive 改用 GetExitCodeProcess）已生效，残留旧 PID 锁不会复现。

## 五、双周期进化落地核验（最近两条记录均来自 2026-10-03）
| 记录 | 类型 | 改动 | config 当前值 | 相符 | 生效方式 | 当前状态 |
|---|---|---|---|---|---|---|
| 十七 · R16（10-03 PM-EVOLVE 第16轮） | 参数落盘 | `down_day_exit_pct` −5.0 → **−3.25** | −3.25（settings.py:364） | ✅ | **需重启引擎**（构造期快照 `self.p=dict(STRATEGY_PARAMS)`，`trend_strategy.py:53` / `event_engine.py:248`） | 引擎 10-06 23:30 重启晚于落盘日(10-03) → **已加载生效** |
| 十八 · SYNTHESIZE（10-03 周六，国庆休市） | 周度研究 | 零参数落盘（config diff 空；仅量测通道/基线守卫修复） | — | — | — | 无可核对项 |

### 生效方式说明
- **热读参数**（下次调用即生效，无需重启）：`momentum_top_n/lookback/rank/scope`、`max_positions`、`exit_mode`、`hard_stop_pct`、`risk_per_trade`、`stop_loss`、`require_daily_data`、`rotation_*`、`northbound_mode`、`manual_entry_codes` 等。本轮无此类新改动。
- **`__init__` 缓存参数**（需重启引擎才生效）：`regime_mode`、`enable_rotation`、`rotation_*`（部分）。本轮两条记录均未改此类参数 → **无待重启项**。
- R16 的 `down_day_exit_pct` 属构造期快照类（需重启）。10-03 当时标注「需 10-08 前重启」，今日(10-06)引擎已重启，该改动现已生效，不再是需要介入的阻塞项。

## 六、需用户留意（非阻塞）
1. **休市日历**：10-06 处于国庆休市（10-01~10-07），无实际交易；首个交易日为 **2026-10-08**。本检查确认的是 10-08 开市前的就绪状态。
2. **北向可信度守卫**：`northbound.credible=false`、`guard_reason="stale"`、`guard_on=true` → fail-open（不拦建仓）。属预期行为（2024-08-19 起官方停披露每日净买入，nb 为「死闸门」），无需处理。
3. **券商连接**：`broker=disconnected`（paper 模式正常）。
4. **双周期节奏**：国庆假期（10-01~10-07）AM/PM-EVOLVE 多为 OBSERVE/SYNTHESIZE，仅 10-03 有 R16 落盘（已生效）；10-08 起恢复正常双周期。

## 七、处置记录
- 未改动任何策略参数 / 生产配置。
- 未杀死/重启任何正在运行的交易引擎或 miniQMT 进程（仅读取进程启动时间作核对）。
- 未触发任何阻断性告警。

---
_生成：qmtIDE-deepseek PRE-MARKET 自动化（98ffc7a2）｜只读检查，无副作用。_

# 盘前就绪报告 — 2026-09-16（周三）

- 生成时间：2026-09-16 09:06 (GMT+8)
- 执行环境：conda env `qmt` (`C:\Users\lisinan\.conda\envs\qmt\python.exe`) + PATH(conda qmt `Library/bin`, `DLLs`) + `PYTHONPATH=.`
- 检查员：98ffc7a2 盘前就绪自动化（仅检查与保障，未改动任何策略参数/生产配置）

## 一、关键项检查

| 检查项 | 结果 | 说明 |
|--------|------|------|
| miniQMT 在线 | ✅ 在线 | `scripts/health_check.py --start` 返回 `miniQMT=True`（进程 `C:\pazq_qmt\bin.x64\XtItClient.exe` 在运行） |
| 引擎在线 (5000) | ✅ 在线 | `health_check.py --start` 返回 `engine(5000)=True`；`/api/risk`、`/api/snapshot` 均可访问 |
| 执行模式 | ✅ `paper`（模拟盘） | `config/settings.py` 中 `EXECUTION_MODE="paper"`；引擎快照 `exec_mode="paper"`，符合预期（live 未启用） |
| 陈旧 halt | ✅ 无 | `/api/risk` 返回 `halted=false`、`halt_reason=""`；无未恢复熔断，无需 `RiskManager.resume()` |

## 二、风控临近阈值观察（非阻断，需盘中关注）

- 连亏计数 `consecutive_losses = 4` / 阈值 `max_consecutive_losses_halt = 5`（仅差 1 次即触发熔断）
- 日内亏损 `daily_pnl = −4349.0` / 阈值 `daily_loss_limit_abs = −5000`（仅差 651 即触发熔断）
- 当前 `position_scale = 0.6`（已按波动率目标下调仓位）
- 以上均 **未触发 halt**，属风险预算内的正常回调，冷却自动恢复机制（连亏/日亏 1 日、回撤 5 日）处于休眠状态

## 三、⚠️ 数据模式警告（建议开盘前处理）

- 引擎快照 `data_mode = mock`（非实时 xtdata）。引擎启动于 2026-09-15 22:56 时未连上实时行情，已回退为模拟数据。
- miniQMT 当前在线（`health_check` 已确认），但本引擎实例仍运行在 mock 行情下。
- 影响：paper 模拟盘下 mock 数据仅用于自检（引擎自身标注「仅用于自检」），不会动用真实资金；但在真实交易时段用 mock 价格做策略验证意义有限。
- **建议**：由用户手动重启引擎（`python main.py --web`）以重新连接 xtdata 实时行情。按铁律本检查**不主动 kill / 重启 live 进程**，重启交由用户负责。

## 四、结论

盘前就绪：**基本就绪（含 1 项数据模式警告）**。4 项关键检查全部通过，无未恢复熔断，无需 `RiskManager.resume()`。建议用户视需要在开盘前重启引擎以接入实时行情（xtdata），确保当日 paper 验证基于真实价格。

---
> 备注：本报告由自动化检查员生成，仅做就绪确认与隐患提示，未对策略参数、生产配置、live 进程做任何改动。

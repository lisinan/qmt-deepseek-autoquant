# 盘前就绪检查 · 2026-10-29（周四）

> 闭环 PRE-MARKET 环节 · 助手：盘前排雷
> 环境：conda `qmt` + PATH(Library/bin,DLLs) + PYTHONPATH=. · 工作目录 C:\Users\lisinan\Desktop\qmtIDE-deepseek

## 结论：✅ 可交易（系统就绪）

- **paper 模式确认**（config + 引擎 API 双确认，未切 live）
- **无陈旧熔断 / 无死锁**（position_scale=1.0、halted=false）
- **双周期进化最近两条记录均零生产参数落盘** → 无配置-账本偏差、无待重启生效项
- 需用户介入事项均为**非硬阻塞**（见末尾）

---

## 1. 在线状态

| 项目 | 结果 | 说明 |
|---|---|---|
| miniQMT | ✅ 在线 | `health_check --json` → `miniqmt=true` |
| 引擎(5000) | ✅ 在线 | `engine=true`；`/api/risk`、`/api/snapshot` 可访问；`degraded=[]`、`engine_log_fresh=true`；**无需自启动恢复** |
| 引擎进程 | 07:50:50 重启 | 单实例锁=开，无残留 PID 锁（2026-09-16 单实例锁修复已生效）；现运行正常 |

## 2. 执行模式 + 风险底线（硬要求）

- **EXECUTION_MODE = `"paper"`** ✅（config/settings.py 双确认 + `/api/snapshot.exec_mode=paper`）
- **ALLOW_MOCK_TRADING = False** ✅（R14 合成行情闸门生效，mock 模式禁止成交）
- 风险底线核对：

| 指标 | 当前值 | 模板约束 | 判定 |
|---|---|---|---|
| max_drawdown_pct | **-0.20** | ≤ -0.15 | ✅ 更严，未放松 |
| risk_per_trade | **0.040** | ≤ 0.02 | ⚠ 超出模板，但系 **OWNER 2026-10-15 显式授权并固化为 sanctioned 铁律**（R29 L2, commit 05e90b9），**非未授权放松** → 记为已知例外告警，不阻断 |
| T1_RESTRICTION | **True** | = True | ✅ |
| max_positions | **5** | ≥ 3 | ✅ |
| max_single_position_pct / max_order_amount | 0.40 / 400000 | — | L2 协同放宽值，与 sanctioned 铁律一致 |

> 注：模板 `≤0.02 / ≤-0.15` 旧约束已因 OWNER 10-15 授权作废；本报告对 risk_per_trade=0.040 仅作**上下文告警**，确认其为既定授权值，无新越界。

## 3. 陈旧熔断核对

- **DB `engine_state`**：`halted=false`、`halt_reason=""`、`halt_day=2026-10-08`（早已过期）、`consec_loss=1`、`daily_pnl=0.0`、持仓 `[]`
- **实时 `/api/risk`**：`halted=false`、`position_scale=1.0`、`consecutive_losses=1`、`daily_pnl=0.0`
- 判定：**无死锁**（position_scale=1.0 ≠ 0.0）、**无冷却等待**、**无熔断** → 无需 `RiskManager.resume()` 或手动恢复 ✅

## 4. 双周期进化落地核验（最近两条记录）

来源：`reports/EVOLUTION_DECISIONS.md` 末尾两条（均 2026-10-28）：

| 记录 | 内容 | 生产参数落盘 |
|---|---|---|
| **R42-RSI 递归自进化**（首跑·测量） | 新增 `scripts/_recursive_self_improve.py`、`docs/RECURSIVE_SELF_IMPROVEMENT.md`；改自动化 `4a425d4f` 为周六 11:00 递归自进化 | **零改动**（`config/settings.py` 零改动，明示「不改策略参数」） |
| **R43 递归自进化·第1轮交付**（处方） | 新增 `strategy/_param_audit.py`、`tests/test_search_space_contract.py`(12 用例)；改 `tests/run_all.py`、`scripts/_recursive_self_improve.py` | **零改动**（`config/settings.py` 零改动、策略参数零改动） |

- 两条均明示「**不改策略参数 / config/settings.py 零改动 / 生产参数零改动**」→ **无配置-账本偏差**、**无待重启生效项**（无 `__init__` 缓存类参数改动，无需用户重启引擎）。
- ⚠ 归属说明：此二条属「递归自进化 RECURSIVE-SELF-IMPROVE」自动化（周六 11:00）产物，**非**每日 11:40/18:00 的 AM/PM-EVOLVE 周期；最近一条每日双周期记录停在 **2026-10-21**（R41 AM / R42 PM·周中降频首轮），10-22~10-27 无新双周期记录。无论如何归属，最近两条记录均无生产参数落盘，故无落地风险。

## 5. 数据模式（非阻断提示）

- 当前 `data_mode = mock`：盘前 07:50 重启时 miniQMT 探活失败落入；**R14 闸门已禁止成交**（不会用合成价交易）。
- 引擎将在交易时段开启（**09:30**）自动 `_reattach_market_data` 重连 xtdata —— 2026-10-28 同日 **09:30:34** 已实证成功（`行情源已重连 miniQMT(xtdata)，退出 mock 模式`）。
- `broker_connected=False` / `broker_mode=disconnected` 为 **paper 模式正常态**（非 live）。
- **建议**：09:30 后复核 `/api/snapshot.data_mode == "xtdata"`。

## 6. 需用户介入事项（均非硬阻塞）

1. **09:30 后复核数据模式**：确认 `data_mode` 已从 `mock` 自动切到 `xtdata`（机制已实证，但建议人工看一眼）。
2. **risk_per_trade=0.040**：确认仍按 OWNER 已授权的 sanctioned 铁律执行（无需任何改动）。

## 7. 纪律合规

- 未改动任何策略参数 / 生产配置 / live 进程；未 kill 引擎 / miniQMT。
- 本环节以检查与告警为主，未触发任何晋升闸门落地动作。

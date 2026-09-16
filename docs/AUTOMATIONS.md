# 定时任务清单（自进化闭环）

> 最后更新：2026-09-16
> 项目：qmtIDE-deepseek ｜ 执行模式：`paper`
> 所有自动化统一使用 `C:\Users\lisinan\.conda\envs\qmt\python.exe`，运行前需 `export PATH=/c/Users/lisinan/.conda/envs/qmt/Library/bin:/c/Users/lisinan/.conda/envs/qmt/DLLs:$PATH`（否则 sqlite3 报 DLL 缺失），并 `export PYTHONPATH=.`。

## 一、闭环总览

```
GUARD 存活兜底(每小时) ─┐
                        ├─► PRE-MARKET 盘前排雷 09:05
                        │        │
                        │        ▼
                        │   【盘中交易】
                        │        │
                        │        ▼
                        │   OBSERVE 盘后复盘+度量 15:35 ──► EVOLUTION_LEDGER 账本
                        │                                        │
                        │   工程维护+策略改进 17:00 ◄────────────┤
                        │                                        │
                        │   EVOLVE 自进化执行器 18:00 ◄──────────┘
                        │        │  (晋升闸门：OOS 验证达标才落盘)
                        │        ▼
                        └── config/settings.py + EVOLUTION_DECISIONS.md
                             周度深度 SYNTHESIZE 周六 10:00（结构性假设 + 全量复验）
```

**关键设计**：过去闭环只"记录发现"、从不落地能改收益的变更。**EVOLVE 执行器**是 2026-09-16 补齐的缺失环节——它按晋升闸门真的把改动落盘到 `config/settings.py` 并记账，未达考核目标前每个交易日持续迭代。

## 二、任务明细

| # | 环节 | 自动化 ID | 名称 | 触发时间 | 核心职责 |
|---|---|---|---|---|---|
| 1 | GUARD | `ebde5d30` | 守护兜底（交易时段每小时） | 周一至周五 每小时 | 确认引擎(5000)与 miniQMT 在线；掉线触发自启动并复验。**只告警不拉起 miniQMT**（用户 2026-09-15 已禁用自动拉起，改为手动登录） |
| 2 | PRE-MARKET | `98ffc7a2` | 盘前就绪 09:05 | 周一至周五 09:05 | 在线检查、确认 `EXECUTION_MODE=paper`、查陈旧熔断、核对昨夜进化改动是否生效 → 产出 `reports/premarket_YYYY-MM-DD.md` |
| 3 | OBSERVE | `ae0e2431` | 盘后复盘 + 度量 15:35 | 周一至周五 15:35 | `strategy/review_daily.py` → 四维评分(稳定/安全/准确/高效)；`strategy/record_ledger.py` → **跨日真实收益**写入账本；追加 `OPTIMIZATION_BACKLOG.md`；交付 HTML 复盘报告 |
| 4 | 工程维护 | `2534a859` | 工程维护 + 策略改进落地 17:00 | 周一至周五 17:00 | db 体积体检/按需裁剪、日志轮转体检、`tests/run_all.py` 回归门禁、重复告警降噪；发现收益项可直接走晋升闸门落地 |
| 5 | **EVOLVE** | `d8013e93` | **自进化驱动 18:00** | 周一至周五 18:00 | **唯一能真的改策略的环节**：读账本算差距 → 选 1~2 个候选 → 样本内回测 + 样本外 walk-forward → 过闸门则落盘 `config/settings.py` 并记 `EVOLUTION_DECISIONS.md` |
| 6 | SYNTHESIZE | `f093d93b` | 周度深度 周六 10:00 | 周六 10:00 | 汇总本周真实表现、跑 `strategy/_verify_live_quality.py` 全量复验、提出**结构性**候选（非微调参数）并验证 → 产出 `reports/EVOLUTION_PROPOSAL_YYYY-MM-DD.md` |

## 三、考核目标（未达之前持续迭代）

| 指标 | 目标 | 当前基线 |
|---|---|---|
| walk-forward 样本外均值 Sharpe | **≥ 2.0** | 1.69 |
| walk-forward 正收益折数 | **≥ 6/7** | 7/7（收益口径） |
| walk-forward 最大回撤 | **≥ −22%** | −19.85% |
| live paper 近 4 周滚动收益 | **> 0%** | 区间 −19.63%（2026-09-02→09-16） |

达标后可降级为低频观察；未达标则每个交易日继续迭代。

## 四、晋升闸门（EVOLVE / 工程维护落地参数前**全部**满足，缺一即拒绝）

1. 样本内（IS）收益或 Sharpe 相对当前配置有提升
2. **样本外（OOS）walk-forward 均值 Sharpe 较当前配置提升 ≥ +0.10**（OOS 是唯一决策依据）
3. OOS 与 IS 的 Sharpe 差距 ≤ 25%（差距过大 = 过拟合，拒绝）
4. walk-forward 最大回撤 ≥ −22%
5. 任一单折收益 > −15%（不得有灾难折）
6. `python tests/run_all.py` 全量通过
7. **风险底线不可动**：`max_drawdown_pct ≤ −0.15`、`risk_per_trade ≤ 0.02`、T+1 约束保持、`max_positions ≥ 3`
8. `EXECUTION_MODE` 保持 `paper`（严禁切 live）

未通过则**不改动任何文件**，只在 `reports/EVOLUTION_DECISIONS.md` 记 REJECTED 与完整负面证据（务必保留，避免重复踩坑）。

## 五、事实来源（先读，再动手）

| 文件 | 内容 |
|---|---|
| `logs/evolution_ledger.jsonl` | 逐日**真实**账户表现（跨日口径含隔夜重估），机器可读 |
| `reports/EVOLUTION_LEDGER.md` | 同上的人读表格 |
| `reports/EVOLUTION_DECISIONS.md` | 每次迭代的 IS/OOS 结果、通过/否决结论、实际落盘的参数改动 |
| `reports/OPTIMIZATION_BACKLOG.md` | 盘后复盘累积发现（P0>P1>P2），**仅追加不改写** |
| `config/settings.py` | 当前生效的 `STRATEGY_PARAMS` / `RISK_PARAMS` |

## 六、当前生产铁律（2026-09-16 收紧后）

| 参数 | 值 | 说明 |
|---|---|---|
| `EXIT_MODE` | `trend` | 趋势骑行退出（保留已验证 alpha） |
| `max_positions` | 5 | 集中最强 5 只 |
| `max_drawdown_pct` | **−0.20** | 原 −0.25 低于回测 MDD −19.85%，形同虚设；收紧后更早介入 |
| `dd_recover_days` | **3** | 回撤熔断冷却恢复 |
| `halt_recover_days` | 1 | 连亏/日亏熔断冷却恢复（可恢复断路器） |
| `daily_stop_flatten_pct` | **−0.06（新增）** | 日内硬止损 + 强平全部可卖持仓（T+1 仍生效，每日仅一次） |
| `risk_per_trade` | **0.012** | 单笔风险（原 0.02） |
| `max_position_amount` / `max_order_amount` | **18 万** | 单标的/单笔上限（原 30 万） |
| `regime_mode` | `off` | 已证伪（7/7 折均跑输） |
| `enable_rotation` | `True` | 板块轮换 |
| `EXECUTION_MODE` | `paper` | 严禁切 live |

> 参数禁区已于 2026-09-16 由用户（OWNER）明确撤销：不再存在"绝不自改参数"的限制，但**任何改动必须走上面的晋升闸门**，以样本外证据为准绳，不得凭直觉调参。风险底线（断路器有效、单笔风险上限、T+1、paper）是防裸奔，不是禁区。

## 七、运行环境与运维

- **解释器**：`C:\Users\lisinan\.conda\envs\qmt\python.exe`（其他解释器会缺 xtquant/sqlite3）
- **PATH 前置**：`...\qmt\Library\bin` 与 `...\qmt\DLLs`（sqlite3 DLL 依赖）
- **交易引擎**：`python main.py --web`（端口 5000），需在可达 xtdata 的**桌面持久会话**运行
- **miniQMT**：`C:\pazq_qmt\bin.x64\XtItClient.exe`（用户手动启动 + 勾自动登录）
- **守护**：`scripts/guardian.py`（5min 轮询）+ `scripts/health_check.py`（存活检测/自启动）
- **单实例锁**：`logs/engine.pid`；2026-09-16 修复 `main.py::_pid_alive` 致命 bug（原用 `OpenProcess` 单判据，会把强杀后句柄未回收的死进程误判存活 → 永久拒绝启动、守护反复拉起却起不来），现改用 `GetExitCodeProcess(STILL_ACTIVE=259)` 判定

# OWNER INBOX（发现路由总入口）

> 规则见 `docs/AUTOMATION_CONTRACT.md` §四.1。**任何自动化产出的发现都必须在这里留一行**，
> 否则等于结论躺在 `reports/` 里没人消费（2026-10-30 的盯市价退化就是这么漏掉的）。
>
> 格式：`- [P0|P1|P2] [SAFE|FORBIDDEN|INFO] <一句话结论> — <证据>:<位置> — 发现于 <任务> <日期>`
>
> 语义：
> - **SAFE** = 纯工程/正确性/观测修复，可自动落地（改完 `tests/run_all.py` 必须全绿）
> - **FORBIDDEN** = 改策略参数 / 改 EXECUTION_MODE / 需重启 / 真实资金操作 ⇒ **只报，不执行**
> - **INFO** = 观察项
> - 状态后缀：`[已修 <commit>]` / `[已裁决]` / `[待 OWNER]` / `[跟踪中]`
>
> **17:00 工程维护**是当日汇总去重负责人：读当日全部报告 → 合并本文件 → SAFE 项当场修 →
> FORBIDDEN 项在输出里点名。**P0 必须出现在当日输出首屏。**

---

## OPEN（按优先级）

- [P0] [FORBIDDEN] [跟踪中] T+1 卖出后同标的立即回买触发 `306050` 报单风暴（11:26–11:30 每 30s 一单全被拒，同时 `BUY 拒绝: pending_sell`）— `reports/R45_AM_LIVE_T1_ORDER_STORM_20261030.md` — 发现于 AM-EVOLVE 2026-10-30；OWNER 2026-10-30 裁定「暂时不动」，现由 `exit_exempt_codes` 抑制 300308 空转，通用修法未做
- [P0] [FORBIDDEN] [已修 10fbfc1] live 日内盈亏曾走「已实现」口径，而券商 `avg_cost` 是摊薄成本（300308=194.246）⇒ 亏 6,000 仍可开仓，日内亏损熔断事实失效；已改权益口径 `_daily_pnl = 总资产 − 当日开盘资产` — `risk/manager.py::on_asset_update` / `engine/event_engine.py` 调用点 — 发现于 R46-b 2026-10-30
- [P0] [SAFE] [已修 010ff14] 启动持仓初始化自带副本 `last_price=avg_cost`，绕过四档兜底并覆盖 `peak/stop/open_date` ⇒ 总资产低估 57.7%（27,812.98 vs 63,684.42）— `engine/event_engine.py::_init_positions_from_broker` → 已改为委托 `_sync_broker_positions()` — 发现于 R46 2026-10-30
- [P1] [FORBIDDEN] [待 OWNER] 5 个自动化曾因各自 prompt 的「必须 paper」门禁自我停机 2 个交易日（PRE-MARKET 无开盘前体检、OBSERVE 无复盘、KPI#4 账本未建立）— `docs/AUTOMATION_CONTRACT.md` §〇 证据表 — 发现于自动化评估 2026-11-01；已建契约+重写 10 个 prompt，门禁改为**模式感知**（不一致只告警不停机）
- [P1] [INFO] [跟踪中] KPI#4 账本 2026-10-29 缺 EOD 快照（14:48 退出）⇒ 首个 live 行是 10-30；跨模式断点已由 `MODE_BREAKS` 钉住（baseline 66,542.31）— `strategy/record_ledger.py` / `logs/evolution_ledger.jsonl` — 发现于 R47 2026-11-01
- [P2] [SAFE] [待 OWNER] 回测面板末端首次并入**当日未收盘 bar**（20261021 11:29，历史末端 20261019）⇒ 建议加守卫「末端 bar 须属已收盘交易日」— `tests/_baseline_tushare_panel.baseline_is_result` — 发现于 R41 2026-10-21（OPEN_DECISIONS P2）
- [P2] [INFO] [待 OWNER] 「正收益折数」类指标已证为划分依赖型噪声（同一条 +218.8% 曲线，切 9 段 77.8%、切 2 段 50.0%）⇒ D4/D9 建议一次性裁决，改为大样本口径（拼接 Sharpe + 月度胜率）— `reports/EVOLUTION_DECISIONS.md` R40 — 发现于 R40 2026-10-30

## CLOSED（近期）

- [P0] [SAFE] [已修 331ab21] 持仓未订阅 + 盯市价缺第三档 ⇒ 池外持仓被低估 — `tests/test_position_subscription_mark.py` 9 条 — 2026-10-30
- [P1] [SAFE] [已修] 2026-10-30 下午「零扫描」被误诊为时段守卫唤醒缺陷，实为 11:47–17:24 整机休眠（guardian 6h 无心跳）⇒ 判据写入契约 §4.3 — 2026-11-01

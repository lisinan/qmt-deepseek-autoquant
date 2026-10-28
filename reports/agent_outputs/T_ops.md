# T_ops.md — 运维与自动化检查员报告

> 核查时间：**2026-10-28（周三）15:30 盘后** ｜ 子agent：运维与自动化检查员
> 纪律：只读检查。**未 kill 任何进程、未碰 miniQMT、未修改任何配置/自动化**。每个结论附命令/文件证据。

---

## ① 进程与端口健康

| 进程 | PID | 角色（命令行实证） | 启动时间 | 运行时长 | 内存(WS) | CPU 时间 |
|---|---|---|---|---|---|---|
| python.exe | 23580 | live 引擎 `main.py --web` | 10-28 07:25:44 | ≈8.1h | 127–133 MB | 119.6s |
| pythonw.exe | 25296 | GUARD 巡检 `scripts/guardian.py` | 10-28 07:25:36 | ≈8.1h | 45–47 MB | 3.6s |
| XtMiniQmt.exe | 23508 | 行情终端 `linkMini` | 10-28 07:41:15 | ≈7.8h | 365–382 MB | 3261.5s |

取数：`tasklist | grep -i -E "python|miniQMT"` + PowerShell `Get-Process` / `Get-CimInstance Win32_Process`（CommandLine）。

**端口与 Web（只读探测）**：
- `netstat -ano | grep :5000` → `TCP 127.0.0.1:5000 LISTENING 23580`（**仅回环监听**，无外网暴露）；`ESTABLISHED 127.0.0.1:57486 ↔ 5000`（本地客户端 PID 22624，Web/SSE）。
- `curl -s -m 5 http://localhost:5000/` → **HTTP 200，0.163s**；engine_stdout.log 留有 `15:31:59 "GET / HTTP/1.1" 200` 记录（即本次探测）。
- 单实例锁一致：`logs/engine.pid = 23580`，与 LISTENING PID 相符 ✓。
- 引擎工况：15:05:00 起 `非交易时段（post_close）：休眠中，距下一时段约 1105 分钟`（明日 09:30 复醒）；15:02:02 触发陈旧暂停建仓（见⑤-R5，每日固定模式）。
- 生产模式：`config/settings.py:789 EXECUTION_MODE = "paper"` ✓（铁律维持）。

**结论：三进程齐全、端口健康、Web 可访问、paper 模式。miniQMT 与引擎均未触碰。**

---

## ② 自动化盘点

### 2.1 automation-1787667324019 是什么
- 位置：`.workbuddy/automations/automation-1787667324019/memory.md`（11,154 B，mtime **2026-09-01 18:21**）。
- 性质：早期「qmt-deepseek-workbuddy」**研究/优化自动化**的执行记忆（2026-08-25~08-30：regime 闸门验证、max_positions=5、断路器修复、僵尸引擎清理等）。
- 调度规律：按记忆条目为**不定期的长回合优化轮**（非固定时钟）。
- 末次执行：2026-08-30 轮（系统提示通道/可移植/稳定性工程改进，「生产配置不变」收尾）。
- 现状：已被 2026-09-16 重配后的 6+1 自动化闭环取代，**近 2 个月无新条目，属历史遗留目录**。

### 2.2 docs/AUTOMATIONS.md 定义的自动化及今日状态

| 环节 | ID | 触发时间 | 核心职责（摘要） | 末次执行证据 | 今日(10-28)状态 |
|---|---|---|---|---|---|
| GUARD 守护兜底 | `ebde5d30` | 工作日每小时（**实际每 5 分钟**，启动行 `interval=300s`） | 引擎(5000)+miniQMT 在线检查、掉线自启、paper 断言 | guardian.log 今日 100 条 | ✅ 07:25:36 起正常 |
| PRE-MARKET 盘前 | `98ffc7a2` | 工作日 09:05 | 在线检查/paper 断言/核对进化改动生效 | memory 10-20；`premarket_2026-10-20.md` 为末份 | ❌ **今日无产物**（自 10-21 起缺失） |
| AM-EVOLVE 上午自进化 | `789d671e` | 工作日 11:40 | 半日度量→候选→闸门落盘 | memory 10-21 11:55；EVOLUTION_DECISIONS 末条 = 10-21 AM 第41轮 | ❌ **今日无条目** |
| OBSERVE 盘后复盘 | `ae0e2431` | 工作日 15:35 | review_daily 四维评分 + record_ledger 账本 + 追加 OPTIMIZATION_BACKLOG | memory 10-21 15:39；`review_2026-10-21.json` 为末份 | ⏳ **15:35 待触发**（此前 10-22~10-27 连续 4 个交易日缺失） |
| 工程维护 | `2534a859` | 工作日 17:00 | db 体检/日志轮转/回归门禁 | memory 10-21 17:05 | ⏳ 未到点 |
| PM-EVOLVE 下午自进化 | `d8013e93` | 工作日 18:00 | 全日度量→consensus→落盘(次日生效) | memory 10-21 18:31；DECISIONS 末条 = 10-21 PM 第42轮 | ⏳ 未到点（今日上午环节亦未跑） |
| SYNTHESIZE 周度深度 | `f093d93b` | 周六 10:00 | 周度复验 + 结构性候选 | memory 10-10 22:23 | ⚠ 10-17 / 10-24 两个周六 memory 未更新 |
| （历史）北向验证 | `498cb169` | 一次性 | 北向轴重启生效验证 | memory 09-21 | 历史遗留 |
| （历史）1787667324019 | — | 不定期 | 早期优化自动化 | memory 09-01 | 历史遗留 |

> 自动化在宿主内的 ACTIVE/PAUSED 状态**未获取**（本会话无自动化管理工具，上表据文件产物推断）。

### 2.3 memory 目录与 ae0e2431 的对应关系
- `.workbuddy/memory/automations/` 共 9 个目录，其中 `ae0e2431-1e6c-4d65-a29a-95ae0e3028e8/memory.md`（62,433 B，mtime **2026-10-21 15:39**）即 **OBSERVE 盘后复盘的专属执行记忆**，逐次记录 15:35 运行的产物/四维评分/账本/backlog 去重行为。
- 沿革（grep 全文）：08-30 创建（`memory/2026-08-30.md:33`）→ 09-02 重写为「消费 optimization 段→追加 OPTIMIZATION_BACKLOG」型 → 09-16 闭环重配 → 10-09 修复「静默停产」（回补 09-30/10-08/10-09 报告 + prompt 加固 + `record_ledger --backfill` 安全网）。
- 末次执行记录（**2026-10-21 15:35**）：目标交易日 10-21，成交 0/信号 0/halt_count=0/EOD 0 仓；四维评分 稳定 60/安全 100/准确 100/高效 100｜综合 90；err_n=3（09:27「28 只快照陈旧 2 分钟」、12:58「38 只陈旧 89 分钟」、15:02「36 只陈旧 2 分钟」）；**备注「OPTIMIZATION_BACKLOG.md 当前不在工作区（不存在）」**。
- 日记忆 `.workbuddy/memory/*.md`：10-21 之后无新日记，直到 **10-28 08:12**（今日股票分析流水线所写），10-22/23/26/27 缺失——与自动化套件停摆互洽。

### 2.4 15:35 盘后复盘自动化今日可执行性评估

| 检查项 | 结果 | 证据（核查时间 15:30） |
|---|---|---|
| 消费对象 `reports/OPTIMIZATION_BACKLOG.md` 存在？ | ✅ 存在 | 20,117 B / 134 行 |
| 内容非空？ | ✅ 非空 | 末节 `## 2026-10-07（PM-EVOLVE 第21轮收官补记）` |
| 最近更新时间 | ⚠ **陈旧 21 天** | `stat` mtime = **2026-10-07 18:44:45** |
| 其他输入（qmt.db / review_daily.py / conda 解释器） | ✅ 就绪 | storage/qmt.db 26.19MB；引擎在跑；解释器铁律已固化 |
| 历史执行连续性 | ❌ 10-22/23/26/27 **连续 4 个交易日无 review 产物** | `logs/review_*.json` 末份 = 10-21；`review_2026-10-21.html` 为末份 HTML |
| 宿主今日活跃度 | ⚠ 仅 GUARD（07:25 起）+ benchmark（15:11）有今日产物 | `grep -c "2026-10-28" reports/EVOLUTION_DECISIONS.md` = **0**；无 `premarket_2026-10-28.md` |

**评估结论**：输入条件齐全，15:35 到点后该自动化**具备执行条件**；但它已连续 4 个交易日未产出、且今日上午环节（PRE-MARKET/AM-EVOLVE）也无产物，调度宿主状态无法从本会话直接确认。**今日 15:35 后须人工复核** `logs/review_2026-10-28.json` / `reports/review_2026-10-28.html` 是否生成；若再缺失，应检查自动化是否被置 PAUSED。另注意：BACKLOG 在 10-21 时曾缺失、现存文件为 10-07 内容，**累计断档 3 周**，EVOLVE 的 backlog 消费链已陈旧。

---

## ③ 巡检日志今日概况（核查时间 15:30）

**logs/guardian.log（GUARD 巡检，5 分钟节拍）**：
- 今日 **100 条**，`07:25:36 Guardian 启动 pid=25296 interval=300s` → 末条 `[2026-10-28 15:30:48] miniQMT=True engine=True`；编排层所述 `15:25:45 miniQMT=True engine=True` 已在列 ✓。
- 早晨 4 条 `miniQMT=False`（07:25–07:41，用户 07:41:15 手动登录 miniQMT 后全部转 True）。
- 今日唯一告警：`[2026-10-28 08:16:25] [GUARDIAN-ALERT] degraded=engine_log_stale engine_progressing=None engine=True miniQMT=True`（盘前引擎日志推进停滞，**瞬时自愈**，后续节拍正常）；`.guardian_alert_last = 2026-10-28|engine_log_stale`。
- 覆盖面（按日计数）：10-21: 572 / 10-22: 543 / **10-23: 200（末条 16:42:47）** / **10-26: 2** / **10-27: 0** / 10-28: 100 → **10-23 傍晚起断裂，10-26 晨两次「已有 guardian 实例 pid=21828，退出」（旧实例僵持占锁），10-27 全天空白**，10-28 07:25:36 随开机自启恢复。

**logs/health_check.log（存活检测/自启动）**：
- 今日 5 条：`07:25:42 miniQMT 自动启动已禁用…跳过拉起` → `07:25:44 [ACTION] 已尝试启动交易引擎 main.py --web` → 07:30:49 / 07:35:55 / 07:41:00 三条跳过拉起；末条 **07:41:00**。miniQMT 登录、引擎在线后无动作即静默（历史行为一致，仅记录动作）。
- 10-26 / 10-27 均 **0 条** → 空档期无健康检查兜底。

**logs/notices.log（引擎系统提示，3.24MB，JSON 行）**：
- 今日 **38 条** = INFO 25 / SYSTEM 7 / WARNING 3 / ERROR 2 / SUCCESS 1。
- 两条 ERROR：① `07:25:47 数据源=mock（合成行情）：已禁止全部买卖成交`（miniQMT 07:41 才登录，开机顺序所致，`ALLOW_MOCK_TRADING=False` 正确拦截、无成交）；② `15:02:02 行情源失效：34 只标的快照已陈旧 2 分钟…已暂停建仓`（见⑤-R5）。
- 三条 WARNING：风险预算 200% 理论敞口提示（L2 杠杆已知项，现金夹紧封顶 95%）、mock 数据提示、北向闸门 fail-open（序列末端 20260825，已知）。
- **10-27 佐证（GUARD 空档期引擎仍存活）**：notices.log 10-27 共 30 条，含 `09:30:48 交易时段开启`、`15:00:45 心跳（持仓=0 总资产=972,533.60 券商重连=0）`、`15:02:03 陈旧 ERROR`；quant_system.log 旧实例末条 `10-27 22:06:46`（Web UI 曾有 SSE 连接），今晨 07:25:44 由 health_check 重新拉起新实例（23580）。

---

## ④ 磁盘与日志增长

`logs/` 合计 **38MB**；`storage/` 合计 **134MB**。

| 文件 | 总大小 | 今日增量（首条 10-28 偏移→文件尾） | 说明 |
|---|---|---|---|
| quant_system.log | 8.76 MB | +42.0 KB | < 20MB 轮转上限，健康；归档 `quant_system.log.1` = 20.97MB（09-22 轮转产生） |
| engine_stdout.log | 1.35 MB | +42.2 KB | 引擎 stdout（werkzeug/event_engine） |
| notices.log | 3.24 MB | +8.4 KB | 38 条系统提示 |
| guardian.log | 711 KB | +5.0 KB | 100 条巡检 |
| gate_timeline.jsonl | 287 KB | +308 B | 末条 10-28：`mom_ok=0 / all_pass_n=0`（16 只宇宙） |
| benchmark_daily.jsonl | 1.5 KB | +~215 B | `15:11:52` 条目（每日基准脚本仍在跑，10-22~10-28 每日不断） |
| health_check.log | 40 KB | +0 | 末条 07:41 |
| evolution_ledger.jsonl | 6.2 KB | +0 | 末条 **10-21**（OBSERVE 停摆所致） |

**storage/qmt.db\***：`qmt.db` 26.19MB + `qmt.db-wal` 4.15MB + `qmt.db-shm` 32KB；备份 `qmt.db.bak-20261022` 45.51MB + `qmt_backup_20260921_161453/161552.db` 各 23.53MB（**旧备份合计约 92MB**，属工程维护 db 体检的归档候选，本轮未动）。

**异常膨胀：无。** 各日志今日增量均为 KB 级（零 BUY 防御态下引擎静默）；最大单体为 09-22 的轮转归档 20.97MB（旋转机制正常工作）。

---

## ⑤ 运维风险清单

| # | 级别 | 事项 | 证据 | 建议 |
|---|---|---|---|---|
| R1 | **高** | 交易日自动化套件停摆：OBSERVE / AM-EVOLVE / PM-EVOLVE 自 **10-22** 起无产物；PRE-MARKET 自 **10-21** 起无产物；今日上午环节亦无 | EVOLUTION_DECISIONS 末条 10-21；review JSON/HTML 末份 10-21；premarket 末份 10-20；memory 日记 10-22~10-27 缺失；今日 DECISIONS 10-28 条目=0 | 人工检查 WorkBuddy 自动化是否被置 PAUSED / 宿主是否运行；15:35 后复核今日 review 是否生成 |
| R2 | **高** | BACKLOG 消费对象陈旧且曾缺失：mtime **10-07 18:44**（21 天未追加）；10-21 时文件不在工作区（memory 记载），现存为恢复态 | `stat` + ae0e2431 memory 10-21 备注 | OBSERVE 恢复后确认追加正常；EVOLVE 的 backlog 消费链已断档 3 周 |
| R3 | 中 | GUARD 巡检空档：10-23 16:42:47 后断裂；10-26 晨两次「已有 guardian 实例 pid=21828，退出」（僵持占锁）；**10-27 全天 0 条**；10-28 07:25:36 恢复 | guardian.log 按日计数 572/543/200/2/0/100 | 空档期引擎掉线将无人兜底（health_check 同期 0 条）；排查 pid 21828 僵持原因 |
| R4 | 中 | 今日 08:16:25 `GUARDIAN-ALERT engine_log_stale`（瞬时自愈） | guardian.log + `.guardian_alert_last` | 观察是否复现；盘前引擎静默属预期，告警阈值可评估 |
| R5 | 低（**预期行为**） | **15:02 快照陈旧暂停建仓 = 每日固定模式**：10-27 15:02:03（36 只）与 10-28 15:02:02（34 只）**都有**；10-16/19/20/21 亦见于 OBSERVE 记录（err_n 均含 15:02 条目） | notices.log 两日对比 | 收盘后 miniQMT 停推 tick + `SNAPSHOT_STALE_SEC=120` 的设计行为，非故障；仅影响 15:02 后建仓（尾盘时段） |
| R6 | 低（预期） | 07:25:47 mock 回落 ERROR：引擎先于 miniQMT（07:41:15）启动 | notices.log + 进程启动时间 | 开机自启顺序所致，无成交无风险；可保持现状 |
| R7 | 低 | 账户零 BUY 防御态延续：10-13 起 0 成交，权益恒 972,533.60；今日 gate_timeline `mom_ok=0 / all_pass_n=0`；benchmark_daily 相对宇宙超额 **+12.08pt** | gate_timeline.jsonl / benchmark_daily.jsonl | 策略预期防御（动量闸门候选池空，60 日动量无一达 0.15），非故障；EVOLVE 停摆期间无参数迭代 |
| R8 | 低 | 旧 DB 备份堆积约 92MB（qmt.db.bak-20261022 45.5MB + 2×09-21 备份 23.5MB） | storage/ 列表 | 归档/清理属工程维护（17:00）职责，待其恢复后处理 |

---

## 附：未获取项（及原因）
1. 各自动化在宿主内的 ACTIVE/PAUSED 状态——本会话无自动化管理工具，仅能据文件产物推断。
2. 10-27 旧引擎实例的 PID 与停机原因——仅见 quant_system.log 末条 10-27 22:06:46，进程已退出。
3. 10-26/10-27 自动化宿主未运行的原因（机器关机/WorkBuddy 未运行/自动化被暂停）——无直接证据可判别。

> 铁律遵守声明：本会话未 kill 任何进程、未修改任何配置、未改动任何自动化、未碰 miniQMT 进程；重启类动作已留待用户负责。

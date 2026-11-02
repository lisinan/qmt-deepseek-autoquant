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

> **【2026-11-02 17:00 分诊汇总 · 工程维护】** 当日 **3 个 P0 全为资金安全级**（假强平真实成交 + 面板口径反号 + 修复未进内存），均 **FORBIDDEN 待 OWNER 处置**；**SAFE 已修 2 项**：① 账本 `prev_asset` 穿透（`record_ledger.py` + 已写入的 11-02 行数据修正，`run_all` 754/754 全绿）；② `_why_no_entry_momentum.py` 标签硬编码改取实际末根日期。回归 **754/754 全绿**；DB 27.9MB / 日志均 <100MB，无需裁剪或轮转；notices 当日 48 行无同模板数百次噪声。

- [P0] [SAFE] [已修 60497d8 · **已生效**：guardian 23:04:24 拉起引擎 pid 7336，启动时刻晚于 `event_engine.py` mtime 22:53:48] **假强平事故的「上游诱因」已堵**：`_sync_broker_positions` 原先对**单次**「券商返回空持仓」零确认 ⇒ 一次 `[]` 就把本地持仓全部归零 ⇒ `_total_asset()` 塌成纯现金（= 11-02 10:34 事故第 2 步）。现改为**连续 `SYNC_EMPTY_CONFIRM_N`(=2) 轮空返回才确认**，中途出现非空即清零重计；确认为真空仓时上首屏 `[ERROR][风控]` 告警（此前只有一行 logger.warning，首屏不可见）。为什么必须单独修：20:11 那次只堵了下游（熔断不再按错数字开火），但本地账本被清空后策略会**误判空仓** ⇒ `max_positions` 失效、可能对同一标的重复买入。回滚：`SYNC_EMPTY_CONFIRM_N = 1`。回归 **782/782**（新增 `tests/test_sync_empty_confirm.py` 7 例 + 改写 2 个旧契约用例，均含正向对照「券商连续确认后仍会清账」）— `engine/event_engine.py::_sync_broker_positions` / `SYNC_EMPTY_CONFIRM_N` — 发现于 总设计师体检 2026-11-02
- [P0] [FORBIDDEN] [已修 2026-11-02 20:11 · 引擎重启 pid 21492→**14832**，`event_engine.py` mtime 20:08:34 < 启动 20:11:21 ⇒ 新代码已进内存；同日 16:45 那次重启已被本次覆盖] **假强平的修复代码未生效：`event_engine.py` mtime 11-02 14:41，引擎进程 PID 3044 创建于 11-01 15:51:09（差 22h50m），且该文件已改未提交 ⇒ 明日 09:30 仍走旧 `_total_asset()` 熔断路径，同型事故第 4 次的窗口仍开着**（纪律第 9 条「落盘≠生效」）— `engine/event_engine.py:4372/2121` + `Get-CimInstance Win32_Process PID 3044.CreationDate` — 发现于 METRIC-HYGIENE 2026-11-02
- [P0] [FORBIDDEN] [待 OWNER] **明日 09:35~14:50 将以 537~545 限价区间接回 300308×100（OWNER 指令），单票集中度 86%~87% = 策略设计意图（7.2%）的 2.15 倍**：一手 55,000 > `equity×40% = 25,283` ⇒ 现有任何买入路径算出的股数**恒为 0**（算术问题，非价格问题），故新开独立通道 `owner_reentry`（只跳过单仓比例这一条，熔断/日内次数/现金夹紧/行情新鲜度/总资产可信度/单笔金额全保留，`valid_until=2026-11-03` 到期作废）。若开盘直接跌破 537 引擎**只告警不下单**（不接飞刀），是否强接由 OWNER 决定 — `config/settings.py::owner_reentry` + `engine/event_engine.py:2600/2719` + `reports/OWNER_REENTRY_300308_2026-11-03.md` — 发现于 OWNER 直连 2026-11-02
- [P1] [SAFE] [已修 2026-11-02] **`owner_reentry` 未登记读点契约 ⇒ `test_search_space_contract` 立即报红**（「实测仅实盘但未登记」）：已在 `strategy/_param_audit.py::CONTRACT_LIVE_ONLY` 登记为 live-only（回测无「外部指定价位」概念），按 R43 第 23 条该通道结论只能记**未验证**、不得记已证伪 — `strategy/_param_audit.py:163` — 发现于 OWNER 直连 2026-11-02
- [P2] [INFO] [待 OWNER] 明日收盘后应把 `STRATEGY_PARAMS["owner_reentry"]` 置回 `{}`，避免配置长期悬挂（虽已 `valid_until` 兜底，但悬挂配置会让下轮审计把「惰性参数」误读为「已启用」） — `config/settings.py` — 发现于 OWNER 直连 2026-11-02
- [P0] [FORBIDDEN] [待 OWNER] [已并入下方 P0 行51·同主题] **面板基准条三数字全错且符号相反**：`account_pct` 分母仍是 `INITIAL_CASH=1,000,000`（paper 初始资金）⇒ 显示 **−93.68%**，live 真实 **−5.01%**（差 88.67pp）；`excess_pct` 显示 **−72.21pp** 而真实 **+16.46pp（跑赢等权）**；`cash_pct` 硬编码 `0.0`（今日 100% 现金）。10-29 切 live 只重定了账本基线、漏了面板；且 `tests/test_benchmark_and_rerank_b4b5.py:250` 把 `cash_pct==0.0` 焊死为断言 — `engine/benchmark_feed.py:106/120` + `config/settings.py:887` — 发现于 METRIC-HYGIENE 2026-11-02
- [P1] [SAFE] [已修 2026-11-02] **定时任务归集 10 → 3**（OWNER 指令「核实所有定时任务，归集为每日一次复盘」）：新建 `DAILY-REVIEW`（交易日 15:40，合并原 7 个工作日任务）；PM-EVOLVE 并入周日 SYNTHESIZE；其余 8 个 PAUSED（`[已归集→…]`，可回滚）。代码同步 `scripts/_loop_liveness.py::TASKS`（3 条）+ `tests/test_loop_liveness.py`（加「不得登记已停用 id」断言）+ 契约 §五 重写，`run_all` **775/775 PASS** — `docs/AUTOMATION_CONTRACT.md` §五 + §六 变更日志 — 发现于 OWNER 直连 2026-11-02
- [P1] [INFO] [待 OWNER 知情] **归集带来两个盲窗**：① 盘中无小时级 LLM 巡检（改由 `core/guardian.py` 300s + `health_check --start` 承担，DAILY-REVIEW 只做事后回溯）⇒ 盘中失效最早 15:40 才知道；② 无 09:05 盘前体检（改为 B 块「源码 mtime vs 进程启动时刻」事后核验 + 次日自检清单）⇒ 若昨晚改代码今早带旧代码开盘，要到 15:40 才发现。**补偿纪律**：需重启才生效的改动，改完当轮就重启并核对 mtime < 启动时刻 — `docs/AUTOMATION_CONTRACT.md` §5.1 — 发现于 OWNER 直连 2026-11-02
- [P2] [INFO] **归集期的核实结论（与原审计不同，供后续参照）**：`logs` 显示工程维护 11-02 17:00 **实际跑过**（INBOX 有分诊汇总、列了 3 个 P0 + SAFE 修 2 项），但活性审计判 ③ —— 根因不是审计逻辑错（情况 B 已覆盖「有产物无 memory」），而是**它登记的产品 `reports/DB_MAINTENANCE_*.md` 自 10-22 后从未再生**、memory 停在 10-30 ⇒ 该次运行无法归因。教训：**任务的产品清单必须与 prompt 实际要求产出的文件严格一致**，否则「跑了」也会显示成「没跑」 — `scripts/_loop_liveness.py:104` 注册表 — 发现于 OWNER 直连 2026-11-02
- [P2] [INFO] [待 OWNER] 510050.SH 日线末根仍停在 **20261030**（38 只订阅里唯一，其余 37 只已到 20261102）：它是 `VALIDATION_UNIVERSE` 唯一成员、按设计只收 tick 不进日线上下文，故未刷新属预期；但今日被强平卖出后若经观察篮补回，首日将无日线特征 — `xtdata.get_market_data_ex` 37/38 分档 — 发现于 METRIC-HYGIENE 2026-11-02
- [P2] [SAFE] 假基数行判据需修正：今日漏斗「宇宙 37」< 订阅 38 应判**通过**而非假基数（少的 510050 是设计排除）⇒ 判据应为「比 `len(UNIVERSE)+len(dynamic.active)` 而非比订阅总数」，否则每次都会误报 — `reports/measure_hygiene/HYGIENE_20261102.md` §一 #4 — 发现于 METRIC-HYGIENE 2026-11-02
- [P1] [SAFE] **账本 `prev_asset` 穿透未修正值（OBSERVE 今日 P1 的根因补充）**：`record_ledger.eod_asset(day)` 只查 `equity_snapshots`，而 10-30 的修正（65,748.42→63,684.42）只写进**账本行**⇒ 11-02 行 prev 又取回 65,748.42，当日收益记 −3.86% 而正确链接为 **−0.75%**（虚增亏损 3.11pt / 2,064 元）。`stale_mark_suspect_day` 拦不住：10-30 有 2 笔成交 ⇒ 判据①不成立（「盘中快照冒充收盘」与「陈旧盯市」是同一根因的两个判据）。修法：prev 优先取上一行账本的 `eod_asset`，DB 仅兜底 — `strategy/record_ledger.py:263/523` + `logs/evolution_ledger.jsonl`(2026-11-02) — 发现于 METRIC-HYGIENE 2026-11-02
- [P1] [INFO] [待 OWNER] **引擎全天只在 09:30:35 刷新一次日线上下文 ⇒ trend_up 全跑在 T−1 日线上，对「当日刚越线」的标的系统性滞后 1 个交易日**：11-02 引擎 `trend_up=1` vs 实盘同源复算 `2`，差 603501.SH（今日 83.15 才站上 MA60 82.52）；按 `drop=1`（末根落到 20261030）复算即逐位回到 1，证明是数据日期差非代码分叉。影响=当天刚转强的票当天永远进不了候选（10-29 引擎 2/复算 1 的反向偏差同源）— `engine/event_engine.py` 日线刷新点 + `logs/quant_system.log` 唯一 1 条「日线上下文刷新完成」(09:30:35) — 发现于 WHY-NO-ENTRY 2026-11-02
- [P2] [SAFE] [待 OWNER] `_why_no_entry_momentum.py` 的 `as_of` 标签**硬编码 `20261027/20261028`**，而脚本每次现拉数据 ⇒ 标签永久停在 10-28、数据实为当日（已核 37/37 收盘价与 `live_close` 逐位相同）。曾导致 10-29 / 10-30 两期日报的动量数字带着错误日期标签（数据对、标签错） — `scripts/_why_no_entry_momentum.py:50` — 发现于 WHY-NO-ENTRY 2026-11-02
- [P0] [FORBIDDEN] [待 OWNER] **live 假强平已造成真实成交（本项目首次资金实际受损）**：10:34:10 `_total_asset()` 退化为纯现金 6,953.34（券商口径静默失效 ≥2min + 回落拼装 Σmv=0），风控算出 `dlp=−89.25%`（真实 −2.07%）⇒ 触发 `daily_hard_stop_flatten` **真实卖出 300308×100 @~548.3 + 510050×500 @2.867**，账户 100% 现金 63,207.98、熔断冷却至明日自动恢复；当日实际 −1,475.86（−2.28%）。三个缺口：A 强平判定无资产合理性校验（需连续 2 轮采样确认）；B Σmv=0 时回落拼装应拒绝输出；C 强平路径 bypass `exit_exempt_codes` ⇒ OWNER 底仓 300308 被卖（vs 10-30 买回价 568.29 亏 ~3.5%）— `reports/R50_AM_LIVE_FAKE_FLATTEN_20261102.md` — 发现于 AM-EVOLVE 2026-11-02
- [P1] [INFO] [待 OWNER] **OBSERVE 复核确认 11-02 假强平，并暴露账本 prev_asset 仍链接旧口径**：review/DB 独立佐证 10:34:10 `risk_state.halt_reason="daily_stop -89.25%"` 而 `account_state.daily_pnl=-1475.86`（真实 −2.28%），300308.SZ 估值塌至 ~0（真实 ~55,300）触发全盘强平、EOD 持仓=0；另 `evolution_ledger.jsonl` 11-02 行 `prev_asset=65748.42` 取的是 10-30 **未修正盘中快照**（契约 §3.3 已修正为 63,684.42），致 KPI#4 当日收益 −3.86% 偏误，修正链接后应为 −0.75%；三口径（review −2.28% / ledger −3.86% / 修正链接 −0.75%）偏差 >1%，符合 P0 扫描项① — `logs/review_2026-11-02.json`(risk.halt_reason/account_state) + `logs/evolution_ledger.jsonl`(2026-11-02) — 发现于 OBSERVE 2026-11-02
- [P0] [SAFE] [已修 2026-11-01] 账本 2026-10-30 EOD 用 **11:29 盘中快照**冒充收盘价（引擎 11:29 后休眠、当日无收盘快照）⇒ 记 −1.19%，真实 **−4.29%**，低估 3.1pt；已按收盘价重算为 63,684.42 并双源核对（券商读数 63,684.34）。**根因未修**：`eod_asset_raw` 仍取「当日最后一条快照」，故新增 `eod_ts`/`eod_is_close` 字段供消费方识别 — `strategy/record_ledger.py::annotate_eod_freshness` + `tests/test_ledger_eod_freshness.py` 6 条 — 发现于盘前准备 2026-11-01
- [P1] [SAFE] [待 OWNER] `_pos_meta`（建仓日 / ATR 止损价 / 目标价 / 峰值）**只在内存、从不持久化** ⇒ 每次重启后 `open_date=null`、ATR 止损退化为 `avg×(1-18%)` 宽幅兜底。实测 510050 真实 ATR 止损应为 2.174，现为 2.353（**放宽 8%**）；300308 由 457→159.281（因 `exit_exempt_codes` 豁免，仅剩 −18% 灾难止损，影响可接受）。修法=把 `_pos_meta` 写入 `engine_state`，需重启生效 — `engine/event_engine.py:512/3298/3974` — 发现于盘前准备 2026-11-01
- [P1] [INFO] [跟踪中] 北向资金缓存已陈旧 **68 天**（末端 20260825），引擎每轮 `fail-open` 并打 ERROR ⇒ 闸门 `northbound_mode="gate"` 名义开启、实际从不拦截（与 09-28 结论「死闸门」一致）。属噪声而非资金风险；改动需 re-baseline（牵连 test_northbound/_evolve_wf/opt_harness），不在盘前动 — `data/northbound_cache.py` — 发现于盘前准备 2026-11-01
- [P0] [FORBIDDEN] [跟踪中] T+1 卖出后同标的立即回买触发 `306050` 报单风暴（11:26–11:30 每 30s 一单全被拒，同时 `BUY 拒绝: pending_sell`）— `reports/R45_AM_LIVE_T1_ORDER_STORM_20261030.md` — 发现于 AM-EVOLVE 2026-10-30；OWNER 2026-10-30 裁定「暂时不动」，现由 `exit_exempt_codes` 抑制 300308 空转，通用修法未做
- [P0] [FORBIDDEN] [已修 10fbfc1] live 日内盈亏曾走「已实现」口径，而券商 `avg_cost` 是摊薄成本（300308=194.246）⇒ 亏 6,000 仍可开仓，日内亏损熔断事实失效；已改权益口径 `_daily_pnl = 总资产 − 当日开盘资产` — `risk/manager.py::on_asset_update` / `engine/event_engine.py` 调用点 — 发现于 R46-b 2026-10-30
- [P0] [SAFE] [已修 010ff14] 启动持仓初始化自带副本 `last_price=avg_cost`，绕过四档兜底并覆盖 `peak/stop/open_date` ⇒ 总资产低估 57.7%（27,812.98 vs 63,684.42）— `engine/event_engine.py::_init_positions_from_broker` → 已改为委托 `_sync_broker_positions()` — 发现于 R46 2026-10-30
- [P1] [FORBIDDEN] [待 OWNER] 5 个自动化曾因各自 prompt 的「必须 paper」门禁自我停机 2 个交易日（PRE-MARKET 无开盘前体检、OBSERVE 无复盘、KPI#4 账本未建立）— `docs/AUTOMATION_CONTRACT.md` §〇 证据表 — 发现于自动化评估 2026-11-01；已建契约+重写 10 个 prompt，门禁改为**模式感知**（不一致只告警不停机）
- [P1] [INFO] [跟踪中] KPI#4 账本 2026-10-29 缺 EOD 快照（14:48 退出）⇒ 首个 live 行是 10-30；跨模式断点已由 `MODE_BREAKS` 钉住（baseline 66,542.31）— `strategy/record_ledger.py` / `logs/evolution_ledger.jsonl` — 发现于 R47 2026-11-01
- [P2] [SAFE] [待 OWNER] 回测面板末端首次并入**当日未收盘 bar**（20261021 11:29，历史末端 20261019）⇒ 建议加守卫「末端 bar 须属已收盘交易日」— `tests/_baseline_tushare_panel.baseline_is_result` — 发现于 R41 2026-10-21（OPEN_DECISIONS P2）
- [P2] [INFO] [待 OWNER] 「正收益折数」类指标已证为划分依赖型噪声（同一条 +218.8% 曲线，切 9 段 77.8%、切 2 段 50.0%）⇒ D4/D9 建议一次性裁决，改为大样本口径（拼接 Sharpe + 月度胜率）— `reports/EVOLUTION_DECISIONS.md` R40 — 发现于 R40 2026-10-30

- [P2] [INFO] [跟踪中] 实盘引擎自检告警：max_single_position_pct=40% × max_positions=5 = **理论敞口 200%** > 现金夹紧后实际上限 95%，配置不自洽；建议降到 ≤19% 使配置自洽。当前由现金夹紧封顶（不会负现金），**无即时风险**，属配置整洁度 — `/api/snapshot` 2026-11-01 15:51 — 发现于 SYNTHESIZE 2026-11-01
- [P0] [FORBIDDEN] [跟踪中] **SYNTHESIZE 评定：T+1 报单风暴仍是本周最大资金风险敞口**（仅 300308 被 exit_exempt_codes 豁免，其他当日买入持仓次日仍会每 3s 重试 306050 拒单；通用修法需 OWNER 授权 + 重启）— `reports/R45_AM_LIVE_T1_ORDER_STORM_20261030.md` §四 A — 复评于 SYNTHESIZE 2026-11-01

- [P0] [FORBIDDEN] [待 OWNER] **盘前就绪 PRE-MARKET 在机器醒着的情况下连续零留痕**：槽位 2026-10-30 09:05 无 memory、无产物，而 `logs/guardian.log` 同期 17 条心跳（最大空档 6 分钟）⇒ **不是整机休眠**，是任务自身/宿主未触发（旧 L3 只会报「停摆」，无法区分两者）— `scripts/_loop_liveness.py` 三态 ③ / `reports/meta/RSI_META_LATEST.md` §〇 — 发现于 R49-RSI 2026-11-01
- [P1] [FORBIDDEN] [待 OWNER] **周中自进化 PM-EVOLVE 错过 2026-10-28 18:00 槽位**（memory 停在 10-21 18:31，窗口 16 条心跳说明机器醒着）⇒ 周中周期空转一周，非休眠所致 — 同上 — 发现于 R49-RSI 2026-11-01
- [P1] [SAFE] [已修] **循环活性审计盲区**：旧 L3 只看 memory mtime（>3 天判停摆），把「在跑但整体停机」判成正常（OBSERVE 10-30 写了 memory「⛔ 硬闸门拦截」却零产出，判「✓ 运行中」）⇒ 已升级为三态+归因，契约 §4.2 同步，`tests/test_loop_liveness.py` 15 例（740/740 全绿）— `scripts/_loop_liveness.py` — 发现并修复于 R49-RSI 2026-11-01
- [P2] [INFO] [待 OWNER] L2 基线漂移 1 项：`manual_entry_codes` 回测 `()` ≠ 生产 `['510050.SH']`（OWNER 的 ETF 验证标的，属「实盘有、回测无」第 19 例）⇒ 建议走**显式 IGNORE 白名单 + 理由**，不要把它抄进回测基线（那会改 P0）— `strategy/_evolve_wf.py::base_cfg` — 发现于 R49-RSI 2026-11-01

- [P0] [FORBIDDEN] [待 OWNER] **KPI#4「实盘相对基准超额」被跨模式基线污染**：`excess_pct` 现报 **−70.49%**（account_pct **−93.63%** = live 63,684.34 ÷ **paper `INITIAL_CASH` 1,000,000**，宇宙 BH −23.14%）⇒ 终身无法转正，属口径断裂不是策略表现；账本已有 `MODE_BREAKS`，基准管道没有 — `engine/benchmark_feed.py:110`（`account_pct = _total_asset()/INITIAL_CASH - 1`）/ `config/settings.py:887` / `logs/benchmark_daily.jsonl` 2026-11-02 行 — 发现于答疑 2026-11-02

- [P0] [FORBIDDEN] [待 OWNER] **熔断按拼装值开火 ⇒ 误强平全部真实持仓（今日 10:34:10）**：拼装值塌成纯现金 6,953.34 ⇒ 判 −89.25% 假日内亏损（真实 −2.1%，10:34:58 权益即恢复 63,258）⇒ 卖出 300308×100 @548.61 + 510050×500 @2.867。已修：熔断改吃券商真实口径、不可信则**跳过判定**（不回落拼装）+ 3 轮中位数去毛刺 + 不可信禁建仓/不落脏快照 + 面板显示判定依据；`tests/run_all.py` 747/747。**需重启引擎才生效**，今日 halted 状态待 OWNER 处置 — `reports/P0_RISK_ASSET_TRUST_2026-11-02.md` — 发现于 OWNER 指令 2026-11-02
- [P1] [FORBIDDEN] [待 OWNER] `_sync_broker_positions` 对**单次**「券商返回空持仓」零确认：一次 `[]` 即把本地持仓全部归零（已加 WARNING 留痕）。建议改「连续 2 次空返回才清空」，但需评估真清仓场景下的幽灵持仓风险 — `engine/event_engine.py:4038` — 发现于 2026-11-02

## CLOSED（近期）

- [P0] [SAFE] [已修 331ab21] 持仓未订阅 + 盯市价缺第三档 ⇒ 池外持仓被低估 — `tests/test_position_subscription_mark.py` 9 条 — 2026-10-30
- [P1] [SAFE] [已修] 2026-10-30 下午「零扫描」被误诊为时段守卫唤醒缺陷，实为 11:47–17:24 整机休眠（guardian 6h 无心跳）⇒ 判据写入契约 §4.3 — 2026-11-01

- [P0] [INFO] [已自愈·跟踪中] 盘前估值口径瞬时背离：09:34–09:40 开盘前引擎行情滞后（300308 last_price 卡 553.0 vs 券商~561），②/③=63,684.34 对券商 64,691~64,802 瞬时 −1.56%~−1.72%（>1% 阈值）；09:38 随 xtdata 刷新自愈（②=64,482/③=64,581/①=64,581，最大偏差 0.15%）⇒ 三态现一致，属开盘数据新鲜度观察项 — `/api/snapshot` + `core/broker.get_asset` 2026-11-02 09:38 — 发现于 PRE-MARKET 2026-11-02
- [P2] [INFO] [已恢复] PRE-MARKET 上次运行 2026-10-29，距 2026-11-02 停摆 4 天（10-30 撞旧 paper 门禁自我停机③ + 周末）；契约 §一已改模式感知，今日恢复 — `memory.md` — 发现于 PRE-MARKET 2026-11-02

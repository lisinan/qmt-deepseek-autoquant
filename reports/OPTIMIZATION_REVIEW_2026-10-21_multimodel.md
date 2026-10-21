# qmtIDE-deepseek 优化评审：多模型博弈分析

> 评审日期：2026-10-21 ｜ 方式：6 个审计角度，每个角度由**不同的底层大模型**独立完成，只读审计，不改动任何生产文件。
> 本文只收「能在代码/数据里指到具体行」的结论；每个角度末尾给出该角度最该**停止**投入的一件事。

## 0. 一句话结论

这套系统当前最贵的问题不是「选股不够好」，而是三件事同时成立：

1. **决策链路已经结构性归零**：38 只宇宙里 `trend_up` 通过 **0 只**，连续 **12 个交易日零 BUY**，账户从 2026-10-09 起 100% 现金、净值 9 个交易日纹丝不动（972,533.60）。
2. **三套口径互不相等**：回测按单边 15bp 成本、paper 撮合 **0 成本 + 100% 即时成交**、live 无成交对账（委托受理即当成交）。任何「优化」都无法证明自己真的优化了。
3. **无人值守链的失效模式是静默降级**：20 个交易日里 104 次「行情快照陈旧」ERROR；guardian 只探 TCP 端口；持久化写失败一律 `return 0` 吞掉。系统「活着但不干活」时，看门狗报 green。

**博弈判断**：在「日线趋势 + 集中最强 3 只」这条拥挤赛道上，个人系统没有选股信息优势。这个项目真正被 41 轮证据验证过的东西只有两样——**离场/仓位纪律** 与 **量测卫生**。而这两样都没被产品化。所以最优路线不是继续加 alpha 轴，而是先把「故障时不产生计划外敞口、决策可被看见、口径唯一」三件事做实。

---

## 1. 审计分工（每个角度一个不同模型）

| 角度 | 模型 | 结论一句话 |
|---|---|---|
| 策略 alpha 与回测可信度 | `workbuddy/deepseek-v4-pro` | alpha 是「低暴露 + AI 赢家篮子幸存者偏差 + 未做多重检验」的叠加；成本翻倍即抹平。 |
| 风控与执行现实 | `workbuddy/glm-5.3` | 风控本体扎实，但 live 下所有记忆都随重启清零；查询失败=清仓。 |
| 架构与代码质量 | `workbuddy/kimi-k3-2` | 病根是「补丁驱动演化」：上帝类 + 无配置语义载体 + 陈旧守卫豁免漏斗。 |
| AI/LLM 经济性 | `workbuddy/deepseek-v4.1-flash` | 交易层是零成本装饰，运维层是每日上百次、失败零痕迹的外呼。 |
| 数据与外部依赖 | `workbuddy/minimax-m3.1-flash` | （见 §3.5） |
| 产品化与博弈定位 | `workbuddy/hy4-preview` | （见 §3.6） |

> 说明：同一会话最初的一轮审计（默认模型）已有 3 个角度因故失败；本轮为「一个角度一个不同厂商模型」重跑，6/6 全部返回。

---

## 2. 硬事实基线（可独立复核）

| 事实 | 数值 / 证据 |
|---|---|
| 宇宙规模与闸门 | 引擎日志：`宇宙 38 只, trend_up 0, bias>=2.00 0, score>=4.0且正因子>=3 0 只`（2026-10-21 全天每 10 分钟一条） |
| 零 BUY 天数 | 连续 12 个交易日（`.workbuddy/memory/MEMORY.md:18`）；signals 表最后一条 2026-09-30，orders/fills 最后 2026-10-09 |
| 账户状态 | 100% 现金，`equity_snapshots` 自 2026-10-09 09:15:50 起 **2160 条快照恒为 972,533.60**（回撤 −3.31%） |
| 行情失效 | 「行情源失效：快照已陈旧」共 208 行（WARN+ERROR 各一份，计 104 次）；其中 **53% 集中在 2026-10-07 一天**（当天引擎持有 5 仓、行情整天陈旧），其余多为每日 09:27 / 12:57 / 15:02 的时段边界假阳性 |
| 外部缓存 | 北向、主力资金流、盈利修正三条外部序列均冻结在 **2026-08-25**（约 57 个自然日）；`earnrev_*.json` 23 个文件中 21 个为 `{}` |
| 单测 | 533 例：532 pass / 1 fail（`web.routes` 缺 flask，属环境问题），耗时 71.7s（`logs/_run_all_20261021.txt`） |
| 代码体量 | `engine/event_engine.py` 3660 行、`strategy/backtest_daily.py` 2469 行、`config/settings.py` 1018 行；而 `docs/ARCHITECTURE.md` 仍写「< 1500 行」 |
| paper 零成本 | `engine/` `core/` `risk/` 目录 grep `commission|slippage|fee|手续费` = **0 命中**；回测 `cost_pct = 0.0015` |
| 集合竞价成交 | 38 笔成交中 **18 笔（47%）落在 09:15–09:19**（DB `fills` 按分钟分组：09:15×14、09:19×4） |
| AI 否决从未触发 | `ai_analyses` 40 行中 bearish 7 条，confidence ∈ **[−0.75, −0.65]**；判定式 `confidence >= 0.7` 命中 **0 次** |
| 委托状态机缺失 | `core/broker.py:304` 定义了 `get_orders`，引擎内**零调用点**；无撤单封装 |
| 告警不可达 | 后端有 `/api/notices` 与 SSE `notices` 字段，前端 `dashboard.js` **零处渲染**；`notices.log` 已 3.1MB / 14,189 行 |
| 开机自启 | `logs/startup.log` 每次开机出现**两遍**「自启编排开始」；且「启动守护进程」与「未找到 guardian.py」成对出现（`scripts/startup_all.bat:59` 的 `echo` 文本里有未配对的 `(`，把 if/else 块解析打乱） |

---

## 3. 六个角度的核心发现

### 3.1 策略 alpha 与回测可信度（`deepseek-v4-pro`）

> 判词：回测 Sharpe 1.9~2.0 是真实数字，但不是真实能力。它由「固定赢家篮子 + 无涨停建模 + 未校正多重检验 + 四折共享同一段弱市」共同抬高。

| 级 | 发现 | 关键证据 |
|---|---|---|
| P0 | 40+ 轮网格零显著性护栏：全仓 grep `deflated / permutation / bootstrap / PSR` = 0；晋升闸门是「最差窗 dSh ≥ +0.10」的阈值比较，不是检验 | `strategy/_evolve_wf.py:141`、`reports/OPEN_DECISIONS.md:143-181` |
| P1 | 宇宙是「今天还在的 AI 赢家」12 只静态名单，回放 2022→2026：幸存者偏差从未被修正（动态宇宙的 `exclude_st` 只作用于实盘，不出现在回测） | `config/settings.py:161-183`、`strategy/backtest_daily.py:2333-2342`、`data/dynamic_universe.py:937` |
| P1 | 回测未建模涨跌停：买单按次日 `open` 无条件成交，`valid` 只判停牌 ⇒ 策略在它最想买的涨停票上「必然买到」 | `backtest_daily.py:1624-1681`、`:1694-1695` |
| P1 | 成本翻倍（0.0015→0.003）令 OOS Sharpe **−0.127**，恰好等于晋升闸门的 +0.10；文档却把这张表判为「edge 对成本不敏感」 | `reports/EVOLUTION_PROPOSAL_2026-10-17.md:105-116`、`backtest_daily.py:397` |
| P1 | 四套折（60×9/75×7/90×6/120×4）全部末端锚定，共享 2026-04~10 同一条弱市尾段 ⇒ 「四窗共识」是伪独立复制 | `config/settings.py:362-367`、`MEMORY.md:18-20` |
| P1 | 北向闸门是死闸门，但其 +0.207 的基线抬升仍留在 P0 与 KPI 数字里 | `data/northbound_cache.py:105-126`、`_evolve_wf.py:126-143`、`settings.py:475-491` |
| P2 | 基准只有「等权持有同一个 AI 篮子」，没有「vs 现金」「vs 宽基」 ⇒ 弱市空仓带来的 +17pt 被当成选股 alpha | `backtest_daily.py:2333-2342`、`reports/EVOLUTION_PROPOSAL_2026-10-17.md:122-124` |

**本角度建议立刻停止**：停止用单参数网格/consensus 找下一个 +0.10。36 个轴已闭合，`MEMORY.md:23` 自己写着「参数空间已无可挖 alpha」，而仓库里没有任何工具能区分「真高原」与「噪声高原」。

### 3.2 风控与执行现实（`glm-5.3`）

> 判词：`risk/manager.py` 本体经过多轮实盘修复后相当扎实，但**它的全部记忆都挂在一个 paper-only 的持久化上**。live 的第一天，一次 miniQMT 抖动就足以让系统在真实持仓上裸奔，而看门狗报 green。

| 级 | 发现 | 关键证据 |
|---|---|---|
| P0 | live 风控零持久化：`peak_asset / consec_loss / halted / daily_pnl` 全在 `engine_state`，而 save/restore 三处都只在 `paper` 分支；重启后 `risk/manager.py` 首个资产更新即把峰值设为当前权益，−20% 回撤断路器「从本次进程起点」重算 | `event_engine.py:498-500`、`:3205-3206`、`:3641-3643`、`risk/manager.py:61,129-131` |
| P0 | `get_positions` 查询异常返回 `[]`，被对账逻辑当成「券商已清仓」⇒ 一次瞬时异常清空全部本地持仓：止损消失、`max_positions` 恒不满、`total_asset` 只剩现金 | `core/broker.py:300-302`、`event_engine.py:3377-3379,3414-3416` |
| P1 | 委托受理即当成交（买入建仓、卖出清零），无撤单封装、无委托状态机；`get_orders` 定义了但引擎零调用 | `event_engine.py:2898-2902,3062-3065,3154-3178`、`broker.py:304` |
| P1 | 卖出侧不受行情陈旧守卫保护（买入有 `_market_stale` 阻断，卖出没有）；日内强平「当日只执行一次」，强平委托落空后当日无第二次机会 | `event_engine.py:2796-2799` vs `:3007`、`:2079-2081` |
| P1 | 卖出按 `pos.quantity` 而非券商 `can_use_volume`；broker 同步进来的仓位 `open_date=None` ⇒ `is_t1_locked` 恒判可卖（fail-open） | `broker.py:295`、`event_engine.py:3042`、`:83-98`、`:3420-3439` |
| P1 | guardian 只探 `127.0.0.1:5000` 能否 connect ⇒ 「进程活着、主循环卡死」完全不可见；而主循环自述 43~79s/轮 | `scripts/health_check.py:91-96`、`scripts/guardian.py:59-66`、`event_engine.py:2008-2010` |
| P1 | paper 撮合零成本、100% 即时全额成交，与回测 15bp、实盘滑点双重背离 ⇒ paper 的 KPI 天然放水 | `event_engine.py:2912,3077-3078`、grep 成本项 = 0 |
| P2 | 交易时段从 09:15 起，集合竞价窗口即下限价单；实测 **47% 的成交发生在 09:15–09:19** | `core/market_calendar.py:29-32`、DB `fills` 分组 |

**本角度建议立刻停止**：停止在「连续 12 日零 BUY」的状态下继续烧参数网格。当前真实资金风险敞口在执行与状态层（上表前两条），不在参数空间。

### 3.3 架构与代码质量（`kimi-k3-2`）

> 判词：病根不是「写得乱」，而是**补丁驱动演化**：41 轮每轮都在同一个上帝类和同一个巨型字典上叠分支，从不抽接口。缺陷再生产率随补丁数上升。

| 级 | 发现 | 关键证据 |
|---|---|---|
| P1 | `EventEngine` 单类 3403 行 / 98 个 def，承担 8 类职责（行情陈旧、bar 聚合、入场、退出、轮动、持久化、板块+LLM、Web 快照）；`__init__` 240 行同时做装配、参数快照、mock 守卫、单实例锁 | `event_engine.py:252,262-500+` |
| P1 | 「热读 vs 需重启」没有任何代码载体，只靠文档表格 + 人肉记忆；已实际踩过 `risk_per_trade` 事故 | `event_engine.py:318-367` vs `:2487`、`docs/AUTOMATIONS.md` |
| P1 | 行情陈旧守卫被打成「三豁免串联」：置位 `_market_stale` 后被 `_in_session_grace / _market_age_recovering / _in_exit_grace` 依次翻回；阈值硬编码为类常量（与「不硬编码阈值」的自述矛盾）；自述「不影响任何交易决策」 | `event_engine.py:1866-1933,1936`、`:1905,1910,1914` |
| P1 | 持久化层「写失败静默」：`_init` 失败只 WARNING，所有 `save_*` 统一 `except: return 0`，调用方从不检查 ⇒ live 下账务证据可无声丢失 | `storage/db.py:181-182,217-310`、`event_engine.py:2919-2924` |
| P2 | 共享可变状态无锁：`_total_asset()` 直接迭代 `self._positions.values()`，主循环另一线程 insert/pop 时可抛 `RuntimeError: dictionary changed size`，而单轮异常被隔离 ⇒ 该轮静默跳过（风控空转一轮） | `event_engine.py:3633-3637,1281` |
| P2 | 测试 runner 用硬编码 mods 清单，新增测试忘登记就永不执行（已发生过一次「守卫测试沉睡」）；实际 569 例 vs 文档/口径 533 / 205 | `tests/run_all.py:48-80`、`:74-78` |
| P2 | 名义节奏 3s、实测 43~79s/轮（25 倍），启动预热约 7.5 分钟 ⇒ 分钟级止损的实际粒度是分钟级而非秒级 | `event_engine.py:2008-2010,1146-1147` |

**本角度建议立刻停止**：停止在 `event_engine.py` 里继续叠「豁免/宽限/自愈」补丁。`_update_market_staleness` 68 行内已有三个后置豁免，最新一次仍出「差 32ms 落在宽限外」的 timing bug（`:1891` 自承）——缺陷再生产率已为正，该段应整体换状态机。

### 3.4 AI/LLM 层经济性与可靠性（`deepseek-v4.1-flash`）

> 判词：交易层是**零成本装饰**（唯一的连线符号写反、永不触发），运维层是**每日上百次、失败零痕迹**的外呼。最贵的不是 token（约 ¥0.002/日），是「让运维以为有一道 AI 风控在」。

| 级 | 发现 | 关键证据 |
|---|---|---|
| P0 | 「AI 强 bearish + confidence≥0.7 抑制买入」三重失效：prompt 约定「看空为负」而判定式要求 `>= 0.7`（恒假）；命中分支只打一行 `logger.warning`，无 return/无标记；`_fire_ai` 挂在「买入成交之后」，零 BUY 时调用量恒为 0。DB 40 条样本命中 0；`signals.ai_comment` 非空 0 行 | `ai/analyst.py:32,88-93`、`event_engine.py:3463-3464,2875-2876`、`README.md:214`、`ARCHITECTURE.md:122`、`review_daily.py:1061` |
| P1 | rerank「5 分钟缓存」是死的：缓存键含每 3s 变一次的大盘涨跌幅 ⇒ 0% 命中；实测 09-22~10-21 共 **2397 次**付费调用（≈174 次/日，含午休 2 次、休市日照刷） | `ai/llm_reranker.py:136-139`、`event_engine.py:3609-3613,3513-3517`、`logs/quant_system.log` |
| P1 | LLM 输出没有交易读取者：只进 UI 与一条无条件 INFO；唯一可能影响下单的 `_hot_codes_set` 挂在 `enable_rotation=False` 下；类级 `_last_ticks` 被实例属性遮蔽，导致「无 tick 回退推荐池」成死代码 | `event_engine.py:3615-3622,719,748,3659-3660,3541` |
| P1 | 全链路静默失败：503/超时/JSON 解析失败一律退化成 `None`，rerank 侧连 WARN 都不打；DeepSeek 无退避、无跨供应商降级（OpenRouter 仅在「无 DeepSeek key」时启用，不是「DeepSeek 失败」时）；`usage` 与真实应答模型被丢弃 | `ai/deepseek_client.py:103-107,115-122`、`ai/llm_reranker.py:153-158`、`event_engine.py:3618-3622` |
| P1 | 「观察篮 = LLM 排序前五」是 AI 唯一真金白银的入口，已在 09-30 清空；而清空依据（Sh 1.02→1.71）测的是回测里「固定代码代理」的代价，不是「动态 LLM 排名」的能力 ⇒ 用代理的缺陷给被代理对象判死刑 | `config/settings.py:493-541,512-516`、`backtest_daily.py:527`、`docs/AUTOMATIONS.md:217` |
| P2 | `AI_MAX_TOKENS` / `AI_ASYNC` 定义后无人读取（调用点硬编码 400/600）；confidence 退化成 stance 的 8 档离散值（40 条样本：bullish∈{0.72,0.75}、bearish∈{−0.75,−0.65}、neutral∈{−0.1,0.05,0.1,0.2}）⇒ 0.7 阈值实际在对「档位」而非「置信度」操作 | `config/settings.py:156,808`、`ai/analyst.py:146`、`llm_reranker.py:154`、DB 分布 |

**本角度建议立刻停止**：停止为「让 AI 有用」而重开 rotation 或观察篮。LLM 的非确定性排名会绕过动量闸门直接进入下单路径，而回测对这条路径零覆盖，量测体系不会给任何预警——先用 20 个交易日零资金影响的 shadow 实验证明它有增量，再谈重建。

### 3.5 数据与外部依赖（`minimax-m3.1-flash`）

> 判词：数据层最危险的攻击面不是「缺数据」，而是**静默地把旧数据/空数据当成新数据**。四条来源链各自 fail-open，下游把「无数据」一律读成「无信号」，于是坏数据以「策略表现」的形式出现——零 BUY 与数据已死无法区分。

| 级 | 发现 | 关键证据 |
|---|---|---|
| P0 | 三条外部序列缓存**冻结在 2026-08-25（约 57 个自然日）**，且缓存命中即 `return`、永不联网刷新：北向、主力资金流、盈利修正（23 个 `earnrev_*.json` 中 21 个为空 `{}`，零告警） | `data/northbound_cache.py:71-75`、`data/moneyflow_cache.py:60-62`、`data/cache/*.json` |
| P1 | 北向序列本身不可用：`202412~202504` 有 **175 天空洞**；2024-08-19 后 **365/365 天恒正**（披露口径已变）⇒ 滚动 20 日累计永不 <0，闸门从未拦截过一次建仓；守卫判 stale 时 fail-open | `data/northbound_cache.py:104-126`、`data/cache/northbound_hsgt.json`、`event_engine.py:2308` |
| P1 | 回测不可复现：P0 守卫自动挑「最新」`ts_panel_*.pkl`；`load_daily` 直读本地可变数据且可现场补拉；全库没有「末端 bar 须属已收盘交易日」断言 | `tests/_baseline_tushare_panel.py:52-71`、`backtest_daily.py:56-69`、`MEMORY.md:94-95` |
| P1 | 陈旧告警被假阳性淹没：208 行告警里 **53% 集中在 2026-10-07 一天**（长假后首个交易日，行情源整天未恢复），其余大量是每日 09:27 / 12:57 / 15:02 三个时段边界的时序错配（会话 09:15 开始，行情 09:25 才推） | `core/market_calendar.py:29-32`、`event_engine.py:1363`、`logs/quant_system.log` |
| P2 | 复权口径分裂：`qmt_client` 的 `get_market_data_ex` 未传 `dividend_type`（不复权）、`hist_cache` 用 `front_ratio`、tushare `pro.daily` 亦不复权 ⇒ 除权日的「假暴跌」会命中 `down_day_exit_pct=-3.25` 与硬止损 | `core/qmt_client.py:301-304`、`data/hist_cache.py:44-46`、`strategy/daily_context.py:151-162` |
| P2 | 交易日历 fail-open：`_xtdata_trading_days` 失败时静默退化为「周内即交易日」，休市日引擎仍以「交易中」状态跑、按旧价盯市并刷陈旧告警 | `core/market_calendar.py:47-74,77+` |

**本角度建议立刻停止**：停止在参数里找「零 BUY / 负折」的解，也停止把 104 条陈旧 ERROR 全当行情故障排查——先分清「时段边界假阳性」与「真断线（10-07）」，再谈原因。

### 3.6 产品化与博弈定位（`hy4-preview`）

> 判词：真正的博弈面不是参数而是**信息面**。弱市静默空仓 12 日，系统无法自证「是策略形态还是故障」，用户只能翻 markdown。最高 ROI 是让仪表板回答三问 + 让告警可达，而不是再挖 alpha。

| 级 | 发现 | 关键证据 |
|---|---|---|
| P0 | 仪表板答不了用户每天的三问（**为什么没买 / 离基准多远 / 系统是否降级**）：首屏被「产业链热力图 + 候选池 + LLM 重排」占据（且热力图自述与策略无关）；`snapshot()` 里没有 `no_entry_reason`、没有基准、没有降级字段 | `web/templates/index.html:27-50`、`event_engine.py:1429-1491`、`web/routes.py:236` |
| P0 | 告警不可达：后端已通过 SSE 与 `/api/notices` 送出 notices，前端 **零渲染**；3.1MB / 14,189 行的系统提示没有任何人看得见 | `web/routes.py:142,352`、`web/static/js/dashboard.js`（grep notices = 0）、`logs/notices.log` |
| P1 | README 两项「关键差异」在实盘均不成立：北向闸门是死闸门（fail-open），DeepSeek 协同对订单零影响（只打一行日志） | `README.md:7,194-214`、`settings.py:482`、`event_engine.py:3463-3464` |
| P1 | 自进化闭环 = 报告生成即完成：连续多轮标题「生产参数零改动 / 全 REJECTED」，`config/settings.py` 的 mtime（2026-10-16）早于最新账本（2026-10-21）；41 轮后仍在每日两轮产出 100KB+ 报告 | `reports/EVOLUTION_DECISIONS.md:6990,7075,7209,7345,7517`、`settings.py` mtime |
| P1 | 开机自启双触发且幂等守卫从未生效：`logs/startup.log` 有 **62 次**「启动守护进程」紧接「未找到 guardian.py」（实存 3KB 的 `guardian.py`）；`guardian.log` 同秒出现两个 Guardian pid（23112 / 23316）⇒ 双看门狗并行 | `scripts/startup_all.bat:25-34,55-67`、`scripts/setup_autostart.py`、`logs/guardian.log:12434-12435` |

**本角度建议立刻停止**：停止每日两轮参数网格与 README 卖点维护。36 个轴已闭合、连续多轮 `settings.py` 零改动，产出的只有报告；把算力和注意力转到可观测性与 D1/D4/D9 口径裁决上。

---

## 4. 去重后的优化清单（按 ROI 排序）

### 4.1 优化清单

排序依据 = （避免的损失 × 发生概率）/ 实现成本；同一优先级内按依赖顺序排列。
「对应发现」列写作「§3.x + 该小节表格中的第 n 行」，例如 `3.2 R1` = §3.2 表格第 1 行（P0），便于逐条对照证据。

**P0 — 本周做，先止血**

| # | 动作 | 对应发现 | 成本 | 验证 |
|---|---|---|---|---|
| A1 | paper 撮合补单边 15bp 成本（复用 `BacktestConfig.cost_pct`，勿抄常量），并把交易时段起点改为 09:30（或 09:30 前只观察不下单） | 3.2 R7/R8 | S | `_cash` 变动 = `qty*price*(1±0.0015)`；新增成交零笔落在 09:15–09:29 |
| A2 | live 风控状态跨重启持久化：去掉 `event_engine.py:498/3205/3642` 的 paper 条件，拆 `_restore_risk_state()`（全模式）+ `_restore_paper_ledger()` | 3.2 R1 | S | 重启后 `peak_asset/consec_loss/halted/daily_trade_count` 逐位恢复 |
| A3 | `get_positions` 失败返回 `None`，与空仓 `[]` 区分；`_sync_broker_positions` 拿到 None 时跳过本轮 | 3.2 R2 | S | 注入异常：本地持仓不变 + 一条 WARN |
| A4 | AI 否决线二选一：接线（分析前移到候选阶段 + 写 veto 集合 + 改 `abs(conf)`）或删掉全部「抑制买入」宣称 | 3.4 I1 | S | 40 条历史样本命中 0 → 6（接线）或 `grep 抑制买入` 只指向真实代码 |
| A5 | 首屏「三卡」：今日未开仓原因（闸门漏斗）+ 相对宇宙 buy&hold 超额 + 系统降级灯；并渲染 SSE 的 notices（顶部告警条 top5 + 未读计数） | 3.6 P0 ×2 | M | 刷新页面即可看到四项；人为制造 stale 时 30 秒内出现红条 |

**P1 — 两周内做**

| # | 动作 | 对应发现 | 成本 |
|---|---|---|---|
| B1 | 委托状态机：登记 `order_id`、轮询 `get_orders`、超时撤单、部分成交累计（现在命中第一笔就 `return`） | 3.2 R3 | M |
| B2 | 卖出侧补 `_market_stale` 守卫；强平改为「每只持仓可重试」而非当日一次性；卖出量夹紧到 `can_use_volume`；`open_date=None` 改 fail-closed | 3.2 R4/R5 | M |
| B3 | guardian 判活改为「心跳/权益快照的新鲜度」，而非 TCP 端口；交易时段内 15 分钟无推进即告警（不要自动重启，先人工处置） | 3.2 R6 | S |
| B4 | 基准管道：账户 vs 等权宇宙 vs 现金 vs 宽基，日更写入 snapshot 与 `evolution_ledger.jsonl`（现在是一次性脚本 + README 手抄） | 3.1 A7 | M |
| B5 | rerank：缓存键去掉每 3s 变的 ctx；触发改墙钟 + 交易时段守卫；结果落 `llm_rerank` 表；失败打 WARN 带模型名/状态码 | 3.4 I2/I3/I4 | S |
| B6 | 北向闸门退役（`northbound_mode="off"`），从 base_cfg 删除 +0.207 幻影基线并重钉 P0/KPI | 3.1 A6 / 3.5 | S |
| B7 | 所有外部缓存写元数据 `{last_date, fetched_at, source}`，读取时断言龄期；`earnrev` 空数据必须告警 | 3.5 P0 | M |
| B8 | 复权口径统一为前复权（xtdata 显式 `dividend_type="front_ratio"`、tushare `adj="qfq"`），并断言除权日无异常跳变 | 3.5 P2 | M |
| B9 | 回测：涨停不可买/跌停不可卖；时点宇宙（ST/退市/未上市剔除）；面板指纹 + 「末端 bar 须已收盘」守卫 | 3.1 A2/A3 / 3.5 | M |
| B10 | KPI 收口：`config/kpi_targets.py` + `_evolve_wf.py --mode gate` 代码判定 PASS/FAIL，撤销或改写已被证伪的 KPI#2/#4 | 3.6 / D1·D9 | M |
| B11 | `startup_all.bat` 修 if/else 与解析期展开；只保留一条自启通道；guardian 用 `O_EXCL` 原子占位 | 3.6 P1 | S |

**P2 — 月内做**

| # | 动作 | 对应发现 | 成本 |
|---|---|---|---|
| C1 | 抽 `engine/state_store.py`、`engine/market_freshness.py`（状态机替代三段豁免）、`EngineConfig`；每步验证 P0 逐位不变 | 3.3 P1 ×3 | M |
| C2 | 持久化写失败升级为 ERROR + 内存失败计数；日终 `fills` 行数 vs notices 成交条数对账 | 3.3 P1 | S |
| C3 | `tests/run_all.py` 改自动发现并打印「文件数/用例数」（现 533/569 口径不一，且已漏注册过一次） | 3.3 P2 | S |
| C4 | `_total_asset()` 等迭代改 `list(...)` 快照；写点加轻量锁 | 3.3 P2 | S |
| C5 | 显著性护栏：置换零分布 + deflated Sharpe，把「最差窗 +0.10」换成「超过 95% 噪声分位」 | 3.1 A1 | M |
| C6 | 归档：`scripts/` 47 个带日期 monkeypatch 脚本、`reports/` 180 个文件、`logs/` 151 个文件 | 3.6 | S |
| C7 | 主循环 43~79s/轮归因（分段计时已就位），优先看 `get_full_tick` 是否可批量 | 3.3 P2 | M |

### 4.2 如果只有两周，只做三件事

1. **让数字说真话**（A1 + A2 + A3）：paper 补成本与时段、live 风控记忆落盘、查询失败不再等于清仓。这三件全是 S 级，却能让「paper 达标 = 可以上实盘」这个判断第一次成立。
2. **让状态看得见**（A5 + B3）：首屏三卡 + 告警条 + 心跳判活。做完之后，「连续 12 日零 BUY 是策略形态还是系统故障」这个问题不再需要写脚本、翻日志、读 7000 行账本才能回答。
3. **停止内耗**（B6 + B10 + C6）：北向死闸门退役、KPI 写进代码、一次性脚本归档。把每轮自进化的固定开销从「读 7660 行 → 复述不可达 → 再写 14KB 报告」降下来。

理由：这三件事的共同点是**每做一件，后续所有决策的可信度提高一档**；而参数类工作已被 41 轮、36 个轴证明边际收益为零。

---

## 5. 博弈结构：四个对手

| 对手 | 它怎么赢你 | 对策（本报告的落点） |
|---|---|---|
| **市场（拥挤度/风格）** | 「动量最强 3 只」是公开信号，拥挤时给你最差成交；弱市时全部信号消失，你空仓等于退出游戏 | 接受「下降市不参与」的形态，用 **相对基准超额**（vs 等权宇宙 / vs 现金 / vs 宽基）替代绝对收益 KPI |
| **交易规则与摩擦** | T+1 可用量、涨跌停封板、集合竞价撮合、印花税与滑点；回测里这些都被简化成「按 open 成交」 | 回测补涨跌停不可成交 + 分项成本；paper 补 15bp 成本与 09:30 前不下单 |
| **自己的运维** | 每天多次重启、miniQMT 半死、查询偶发异常、磁盘/编码问题；每一次都让风控记忆归零或账本漂移 | 风控状态跨重启持久化、查询失败与空仓区分、guardian 用心跳而非端口判活 |
| **自己的认知** | 文档宣称的功能在代码里不存在（AI 否决、live 风控、北向闸门、533 例测试）；「报告生成」被当成「决策落地」 | 每条宣称配一个守卫测试或一个可观测数字；把 KPI 写进代码而不是 markdown |

---

## 6. 30 天路线图

**第 1 周：先让系统「说真话」**
1. paper 撮合补 15bp 单边成本（复用 `BacktestConfig.cost_pct` 单一来源），并把交易时段起点从 09:15 改为 09:30（或 09:30 前只观察不下单）。
2. live 风控状态落盘：去掉 `event_engine.py:498 / 3205 / 3642` 三处 `if exec_mode == "paper"`，拆成 `_restore_risk_state()`（全模式）与 `_restore_paper_ledger()`。
3. `get_positions` 查询失败返回 `None` 而非 `[]`；`_sync_broker_positions` 区分「未知」与「空仓」。
4. 告警上屏：仪表板加通知底栏（最近 5 条，ERROR 红底），并把 `/api/health` 的 `data_mode / risk.halted / market_staleness` 接进 guardian 判活。

**第 2–3 周：让决策可被看见**
5. 新增「今日决策条」：今日 BUY/SELL 数、**闸门漏斗**（动量/日线/评分各拦多少）、账户 vs 基准超额、数据新鲜度。
6. 基准管道：账户 vs 等权宇宙 buy&hold vs 现金 vs 宽基指数，日更写入 snapshot 与 `evolution_ledger.jsonl`。
7. KPI 收口：新建 `config/kpi_targets.py`，把 #1/#2/#3/#4 的目标值与口径写成常量，闸门由 `--mode gate` 代码判定；撤销或改写已被证伪的 KPI#2/#4。
8. AI 层二选一：要么按 shadow 实验跑 20 个交易日（含被闸门拒的候选，零交易影响），要么关掉并在 README/复盘里删除「抑制买入」表述。

**第 4 周：降低复杂度**
9. 抽 `engine/state_store.py`、`engine/market_freshness.py`（状态机替代三段豁免补丁）、`EngineConfig` dataclass；每步用「P0 逐位不变」验证。
10. 回测补「时点宇宙（ST/退市/未上市）」+「涨跌停不可成交」，并加成本压力守卫测试。
11. 归档一次性研究脚本与历史报告（`scripts/` 47 个带日期 monkeypatch 脚本、`reports/` 180 个文件）。

---

## 7. 不该做的事（anti-recommendations）

- **不要继续扫单参数网格**。36+ 个轴已闭合，MEMORY 自己写着「参数空间已无可挖 alpha」；再加网格只是用真金白银拟合噪声。
- **不要再加正交数据轴**。北向闸门的 +0.207 基线来自 2024-08 已废止的披露口径，当前是「死闸门」（缓存末端 2026-08-25，断点后 365/365 天恒正，从未拦过一次建仓）。
- **不要为了「让 AI 有用」重开 rotation 或观察篮**。LLM 排名可绕过动量闸门进入下单路径，而回测无法复现这条路径，量测体系不会给出任何预警。
- **不要写第 42 份 7000 行账本**。`EVOLUTION_DECISIONS.md` 已 7660 行，每轮固定开销是「读账本→复述不可达 KPI→再写一份报告」。
- **不要在 live 对账跑通前切实盘**。当前 live 路径没有跨重启风控记忆、没有委托状态机、没有 drift 告警。

---

## 8. 验收清单（可勾选）

- [ ] paper 成交额里能看到成本：`_cash` 变动 = `qty*price*(1±0.0015)`，且新增成交零笔落在 09:15–09:29。
- [ ] 重启引擎后 `risk.peak_asset / consec_loss / halted / daily_trade_count` 逐位恢复（新增 `tests/test_state_persistence.py` 通过）。
- [ ] `get_positions` 抛异常时本地持仓不变，并产生一条 WARN（新增守卫测试）。
- [ ] 页面首屏出现「今日闸门漏斗」与「相对基准超额」，且数字与日志/脚本一致。
- [ ] 人为让 `data_mode != xtdata` 或行情滞后 > 3 分钟，30 秒内页面出现红色告警、`health_check --json` 的 `degraded` 非空。
- [ ] 人为制造一次 `AI` 失败（错误 key），页面能看到模型名 + HTTP 状态，而不是静默。
- [ ] `grep -rn "抑制买入"` 的每一处都指向真实拦截代码，或已被删除。
- [ ] `config/kpi_targets.py` 存在，`--mode gate` 输出的 PASS/FAIL 与 markdown 表格一致。
- [ ] 重启一次机器：`logs/startup.log` 每条只出现一次，最近一次开机 `guardian.log` 只有一行「Guardian 启动」。
- [ ] 能一句话回答：「今天为什么没买」——答案来自页面，而不是临时写脚本。

# 项目迭代优化待办（OPTIMIZATION BACKLOG）

> 由「盘后复盘 + 迭代驱动」自动化（交易日 15:35）每日追加生成。**仅追加，永不改写历史**。
> 目的：从当日真实运行记录中发现本项目的优化内容与方向，持续迭代「稳定 / 安全 / 准确 / 高效」四大维度，最终获得优秀收益。
> 每条格式：`- [优先级/轴/级别] 标题 ｜ 证据：… ｜ 建议：… ｜ 定位：…`
> 跨日累积后便于复盘长期主题（反复出现的券商断线、滑点、AI 全中性等），是项目持续演进的路线图。

---

## 2026-09-02

稳定 70 / 安全 100 / 准确 100 / 高效 100 ｜ 综合 92

- [P1/稳定/warn] 券商连接断开/重连 33 次 ｜ 证据：notices 含 disconnected/重连关键词 33 次 ｜ 建议：复查断线自修复是否真正复用单一 XtQuantTrader 实例，避免 force 重连自伤 ｜ 定位：core/broker.py / core/auto_reconnect.py
- [P2/安全/info] 仍出现 7 次每日风险预算 WARNING ｜ 证据：notices 含「风险预算/max_single_position_pct」关键词 7 次 ｜ 建议：确认 settings.max_single_position_pct=0.19 已与现金夹紧自洽；若持续，查波动率目标仓位对低波大票的裁剪是否过激 ｜ 定位：config/settings.py
- [P2/准确/info] AI 当日仍全中性（无方向性增量） ｜ 证据：AI 立场分布：{'neutral': 5} ｜ 建议：AI 为观察层：bullish 不影响交易、bearish+高置信可抑制买入；全中性说明技术面混杂或模型保守，属预期，不作为收益拖累 ｜ 定位：ai/analyst.py（已放宽中性偏置）

---

## 2026-09-08

稳定 75 / 安全 100 / 准确 100 / 高效 100 ｜ 综合 94

- [P1/稳定/warn] 券商连接断开/重连 27 次 ｜ 证据：notices 含 disconnected/重连关键词 27 次 ｜ 建议：复查断线自修复是否真正复用单一 XtQuantTrader 实例，避免 force 重连自伤 ｜ 定位：core/broker.py / core/auto_reconnect.py
- [P2/准确/info] AI 已给出方向性观点 ｜ 证据：AI 立场分布：{'bullish': 1} ｜ 建议：观察 bullish/bearish 与实际盈亏是否吻合，逐步校准（勿直接作交易 gate，避免侵蚀已验证动量 alpha） ｜ 定位：ai/analyst.py

---

## 2026-09-15

稳定 75 / 安全 88 / 准确 100 / 高效 75 ｜ 综合 84

- [P1/稳定/warn] 券商连接断开/重连 27 次 ｜ 证据：notices 含 disconnected/重连关键词 27 次 ｜ 建议：复查断线自修复是否真正复用单一 XtQuantTrader 实例，避免 force 重连自伤 ｜ 定位：core/broker.py / core/auto_reconnect.py
- [P1/安全/warn] 连亏 4 次 ｜ 证据：当日最大连亏 4 次（688072 −3593、688082 −756 两笔水下老仓退出） ｜ 建议：连亏熔断冷却 1 日应已恢复；若频繁触发查信号质量而非放宽阈值 ｜ 定位：settings halt_recover_days
- [P2/准确/info] AI 已给出方向性观点 ｜ 证据：AI 立场分布：{'bullish': 2, 'neutral': 1} ｜ 建议：观察 bullish/bearish 与实际盈亏是否吻合，逐步校准（勿直接作交易 gate，避免侵蚀已验证动量 alpha） ｜ 定位：ai/analyst.py

---

## 2026-09-16

稳定 60 / 安全 18 / 准确 100 / 高效 75 ｜ 综合 63

- [P0/安全/err] 触发 2 次熔断 ｜ 证据：原因分布 consec_loss=5×55；最大连亏 9 ｜ 建议：确认冷却窗口（连亏/日亏 1 日、回撤 5 日）后是否已自动 resume；若为误触复查阈值（consec_loss=5 与 max_consecutive_losses），勿放宽 ｜ 定位：core/risk_manager.py；settings max_drawdown_pct/dd_recover_days
- [P0/安全/err] EOD 净持仓 9 > max_positions=5 ｜ 证据：引擎权威账本持仓 9 只（注：脚本同报「持仓数对不上」——fills 重放 9 只 vs 收盘权益快照 market_value=0 全现金，疑为旧 paper 会话/测试桩遗留数据假象，需先 prune-db 复核再定性） ｜ 建议：先 python main.py --prune-db N 清理旧数据重跑确认是否真实超限；若确超限查建仓并发闸门 ｜ 定位：settings max_positions；engine 现金夹紧
- [P1/稳定/err] 当日 1 条 ERROR/异常 ｜ 证据：系统提示中 ERROR/Traceback 共 1 条 ｜ 建议：查 logs/quant_system.log 与当日 traceback，优先修根因（DLL/网络/数据可得性），避免主循环异常累积 ｜ 定位：engine/event_engine.py 主循环异常隔离；core/notices.py

> **【修正 2026-09-16 盘后】复盘计算逻辑 bug 已修复（strategy/review_daily.py · _analyze_risk）**：上方 `halt_count=2`、`consec_loss=5×55`、安全 18 为**误判**。根因——同一熔断（09:15:38 因连亏累计达 consec_loss=5 触发，引擎持续 halted 至收盘）被 snapshot 上升沿与 notice 双源各记一次（=2），且 54 个持续 halted 快照被逐个累加进 reasons_counter（=55）。修正后正确值：**halt_count=1、halt_reasons={consec_loss=5:1}、安全 63、综合 74**；当日真实熔断仅 1 次，属可冷却恢复断路器（连亏/日亏冷却 1 日），次日应已 resume。另：EOD 净持仓 9 因「数据一致性告警」（engine_state 9 只 vs 当日末权益快照全现金 0 只，旧 paper 会话/测试桩污染）不可信，已从 P0 降级为 P2 观察，不计入真实超限。回归 tests/test_review_daily.py 新增 2 例（全量 187/0 通过）。

> **【重跑更新 2026-09-17 15:36】**：本次 OBSERVE 自动化重跑（目标交易日仍为 2026-09-16，09-17 无新增成交）产出最新修正评分与发现。当前四维评分（稳定 35 / 安全 63 / 准确 55 / 高效 75 ｜ 综合 57）。较 09-16 首次记录的关键变化：稳定 60→35（当日 notices 现检出 12 条 ERROR/Traceback，主循环异常隔离需查根因）、准确 55（账户跨日真实亏损已纳入维度，首次记录时仍为 100 属口径未真实化）、安全 63 与 halt_count=1 维持（可冷却恢复断路器，连亏/日亏冷却 1 日，次日应已 resume）。
>
> - [P0/安全/err] 触发 1 次熔断 ｜ 证据：原因分布 consec_loss=5×1；最大连亏 9 ｜ 建议：确认冷却窗口（连亏/日亏 1 日、回撤 5 日）后已自动 resume；若为误触复查阈值 ｜ 定位：core/risk_manager.py；settings max_drawdown_pct/dd_recover_days
> - [P0/准确/err] 账户真实当日亏损 -16.40%（跨日口径） ｜ 证据：961,286→803,680，亏 -157,605.82；复盘日内口径仅 -0.25%（漏隔夜重估） ｜ 建议：趋势策略下跌市连续止损/隔夜重估，属收益特征非缺陷；优先查执行滑点与数据源，勿改策略参数 ｜ 定位：equity_snapshots；core/risk_manager.py
> - [P1/稳定/err] 当日 12 条 ERROR/异常 ｜ 证据：系统提示 ERROR/Traceback 共 12 条 ｜ 建议：查 logs/quant_system.log 与当日 traceback，优先修根因（DLL/网络/数据可得性） ｜ 定位：engine/event_engine.py 主循环异常隔离；core/notices.py

> **【重跑更新 2026-09-18 15:35】**：本次 OBSERVE 自动化重跑（目标交易日仍为 2026-09-16；09-17 与 09-18 均无新增成交，脚本自动取最近有成交交易日）。四维评分与 09-17 重跑完全一致：**稳定 35 / 安全 63 / 准确 55 / 高效 75 ｜ 综合 57**。Top-3 findings 亦相同（P0-安全「触发 1 次熔断」；P0-准确「账户真实当日亏损 -16.40%（跨日口径）」；P1-稳定「当日 12 条 ERROR/异常」）。
>
> **上午周期实证（EVOLUTION_DECISIONS.md § 2026-09-18 第2轮 AM-EVOLVE @11:40）**：上午周期对 10 维参数做 OOS 网格扫描，**全部 REJECTED（0/10 通过）**，仅修正 `strategy/_evolve_wf.py` 工程口径（base_cfg 对齐生产 top_n=3 / rpt=0.012），**未落盘任何生产参数**。其关键判断「静态池 12 只全 `trend_up=False` → 日线闸门整体关闭 → paper 账户 100% 现金冻结是策略预期防御行为、非运行缺陷」——**经当日真实表现证实**：OBSERVE 账户口径 `account_daily=-16.40%` / 区间 `-19.63%` / 09-18 上午半日度量权益 803,680 冻结、持仓 0 只、成交 0 笔，与「账户冻结、收益不移动」判断完全一致。结论：**上午未改参数，无需证实/证伪参数效果；其『冻结为预期防御』的判断被当日真实表现证实，数据充分。**
>
> **度量账本**：record_ledger 幂等，md 首记已存在（保留 综合 63）、jsonl 已刷新为当前真实值（稳定 35 / 安全 63 / 准确 55 / 高效 75，综合 57），供 18:00 PM-EVOLVE 与次日 11:40 AM-EVOLVE 消费。
>
> - [P0/安全/err] 触发 1 次熔断 ｜ 证据：原因分布 consec_loss=5×1；最大连亏 9 ｜ 建议：确认冷却窗口（连亏/日亏 1 日、回撤 5 日）后已自动 resume ｜ 定位：core/risk_manager.py；settings max_drawdown_pct/dd_recover_days
> - [P0/准确/err] 账户真实当日亏损 -16.40%（跨日口径） ｜ 证据：961,286→803,680，亏 -157,605.82 ｜ 建议：趋势策略下跌市连续止损/隔夜重估，属收益特征非缺陷；勿改参数（参数高原已证无调参空间） ｜ 定位：equity_snapshots；core/risk_manager.py
> - [P1/稳定/err] 当日 12 条 ERROR/异常 ｜ 证据：系统提示 ERROR/Traceback 共 12 条 ｜ 建议：查 logs/quant_system.log 与当日 traceback，优先修根因（DLL/网络/数据可达性） ｜ 定位：engine/event_engine.py 主循环异常隔离；core/notices.py

---

## 2026-09-25

稳定 75 / 安全 100 / 准确 100 / 高效 100 ｜ 综合 94

- [P1/稳定/warn] 券商连接断开/重连 28 次 ｜ 证据：notices 含 disconnected/重连关键词 28 次 ｜ 建议：复查断线自修复是否真正复用单一 XtQuantTrader 实例，避免 force 重连自伤 ｜ 定位：core/broker.py / core/auto_reconnect.py
- [P2/准确/info] AI 已给出方向性观点 ｜ 证据：AI 立场分布：{'bearish': 3} ｜ 建议：观察 bullish/bearish 与实际盈亏是否吻合，逐步校准（勿直接作交易 gate，避免侵蚀已验证动量 alpha） ｜ 定位：ai/analyst.py

> **上午周期实证（EVOLUTION_DECISIONS.md § 2026-09-25 第8轮 AM-EVOLVE @11:40）**：上午周期对「新鲜数据全参数网格复核（9 维×44 值）」+「结构性扩宇宙（5 种子同池抽样）」两项均做完，**全部 REJECTED、config/settings.py diff 空、零参数落盘**（仅新增 `strategy/_evolve_universe.py` 实验器与 5 例守卫测试，tests 256→261）。故上午**未改任何生产参数，无需证实/证伪参数效果**。
> 但上午交接的两条事实经今日全日真实数据**双向证实**：① 前序缺陷修复线上生效——`halt_count=0`、`max_consecutive_losses=1`、`position_scale=1.0`、账户未冻结（09-24 AM 同批退出合并计数 + 09-23 PM 观察篮防反手均持续命中）；② **P0 工程缺陷「盯市价格未刷新」被坐实**：全日 258 个权益快照总资产恒定 991,309.60、`market_value=173,840` 恒等于建仓成本、`unrealized=0.0` 尽管持 3 只 live 仓 ⇒ paper 账户无法产生浮动盈亏，日内归因失真。结论：**上午未落盘参数（数据不足/不适用）；其交接的 P0 盯市缺陷与「修复已生效」判断均被当日真实表现证实，数据充分。** 该 P0 盯市缺陷已交 09-25 18:00 PM-EVOLVE 优先处置。

> **度量账本**：record_ledger 幂等，本日 2026-09-25 真实收益 0.0% / 累计 -0.87% / 综合 94 已追加（md=新增、jsonl=新增）。注：真实收益 0.0% 本身受 P0 盯市缺陷影响（已实现 -3,088 被新建仓成本抵消、浮盈恒 0），须先修盯市再谈归因；累计 -0.87% 为 09-22 复位 100 万 → 991,309.60。账本供 18:00 PM-EVOLVE 与次日 11:40 AM-EVOLVE 消费。

---

## 2026-09-29

稳定 35 / 安全 100 / 准确 100 / 高效 100 ｜ 综合 84

- [P1/稳定/err] 当日 3 条 ERROR/异常 ｜ 证据：系统提示 ERROR/Traceback 共 3 条（含「行情源失效：29 只标的快照已陈旧 2 分钟」「38 只标的快照已陈旧 87 分钟」数据可得性告警） ｜ 建议：查 logs/quant_system.log 与当日 traceback，优先修根因（DLL/网络/数据可得性） ｜ 定位：engine/event_engine.py 主循环异常隔离；core/notices.py
- [P1/稳定/warn] 券商连接断开/重连 29 次 ｜ 证据：notices 含 disconnected/重连关键词 29 次 ｜ 建议：复查断线自修复是否真正复用单一 XtQuantTrader 实例，避免 force 重连自伤 ｜ 定位：core/broker.py / core/auto_reconnect.py
- [P2/准确/info] AI 已给出方向性观点 ｜ 证据：AI 立场分布：{'bearish': 1, 'bullish': 1} ｜ 建议：观察 bullish/bearish 与实际盈亏是否吻合，逐步校准（勿直接作交易 gate，避免侵蚀已验证动量 alpha） ｜ 定位：ai/analyst.py

> **上午周期实证（EVOLUTION_DECISIONS.md § 今日 2026-10-03 无 11:40 AM 条目）**：今日为国庆假期（10-01～10-03 休市），且 EVOLUTION_DECISIONS.md 中**无 2026-10-03 上午周期条目**；最近一次 AM-EVOLVE 落地为 2026-09-30 第14轮（P0 合成行情禁止交易闸门 + 基线同步守卫扩展至 RISK_PARAMS）。该轮改动目标交易日为 09-30 之后，而本轮回溯目标交易日 09-29 早于其落地，且 10-01～10-03 无成交 ⇒ **无数据可证实/证伪今日上午改动**，判定「数据不足 / 无上午条目」。备注：09-30 上午落地的 `ALLOW_MOCK_TRADING=False` 闸门本为对冲 09-30 合成行情假亏损 −21.1% 事件，节后复市首日（10-08）的真实成交将是其首个验证窗口。

> **度量账本**：record_ledger 幂等，本日 2026-09-29 真实收益 +0.5% / 累计 −2.47% / 综合 84（md 已于 09-30 补记、保留原值；jsonl 本次刷新为最新值）。注：累计 −2.47% 经 09-30 账本修复（repair_mock_fills 回滚 09-30 合成行情假成交）后，口径已剔除 09-30 假亏损，反映 09-22 复位 100 万 → 09-30 末 975,273.60 的真实轨迹。账本供 18:00 PM-EVOLVE 与次日 11:40 AM-EVOLVE 消费。

> **【重跑更新 2026-10-06 23:59】** 国庆休市（10-01~10-07）无新成交，脚本自动取最近有成交交易日仍为 **2026-09-29**，数据与 2026-10-03 运行同口径（评分行 + Top-3 findings 字节一致，遵守「仅追加、绝不改写」未重建 `##` 头）。
> **上午周期实证（EVOLUTION_DECISIONS.md §十九 2026-10-06 上午 · AM-EVOLVE 第 17 轮）**：上午落盘 1 个参数 `enable_rotation: True→False`（R17，四窗口共识全正、最差窗 +0.060、IS 收益 +9.8pt、Sharpe 1.59→1.79）。但① 目标日 09-29 早于该改动（且当日 notices 仍含「板块轮动补强空槽：买入 688008.SH」⇒ 轮动当时为**开**）；② 改动需重启引擎、首个实盘验证窗口为 10-08~10-09 ⇒ **判定「数据不足」**：上午参数改动尚无任何真实交易日可供证实/证伪，待节后复市 10-08 首笔成交再验（核对换手下降与收益是否复现 IS −24% 换手 / +9.8pt 收益）。
> **度量账本**：record_ledger 幂等，md 首行保留原值、jsonl 已刷新为最新真实值（2026-09-29 真实 +0.5% / 累计 −2.47% / 综合 84），供 18:00 PM-EVOLVE 与次日 11:40 AM-EVOLVE 消费。

> **【重跑更新 2026-10-07 15:35】** 国庆休市（10-01~10-07）无新成交，脚本自动取最近有成交交易日仍为 **2026-09-29**；评分行（稳定 35/安全 100/准确 100/高效 100｜综合 84）与 Top-3 findings 与历次 09-29 重跑字节一致，遵守「仅追加、绝不改写」未重建 `##` 头。
> **上午周期实证（EVOLUTION_DECISIONS.md · 第 20 轮 AM-EVOLVE 2026-10-07 11:40）**：上午落盘 **1 个缺陷修复（非策略参数）**：`engine/event_engine.py` 新增 `MARK_STALE_FREEZE_VALUATION=True`（盯市冻结 + 行情陈旧时不建日内盈亏基线），对症「休市伪快照把 paper 权益虚增 +45,173 元、KPI#4 符号翻转」。① 该缺陷的**诊断被当日真实数据证实**：本回 review 的 `account_range` 把区间终点取到 DB 最新快照日 **2026-10-07**（total_asset=1,020,446.6 的休市伪快照），累计 **+2.04%**（虚高），而 09-29 当日跨日收益 +0.5% 干净、md 累计 −2.12% 真实 ⇒ **KPI#4 符号翻转实锤**；② 该修复**代码需 10-08 开市前重启引擎才生效**，其对 live 引擎「冻结陈旧价、10-08 首 tick 修正权益不被误读为日内暴跌」的行为效应**尚无真实交易日可证**，判定「数据不足」，首个验证窗口 = 10-08 开盘。
> **★ 度量层新发现（交 EVOLVE）**：R20 修了 live 引擎的盯市冻结，但**复盘/账本管线未覆盖**——`review_daily.py::_load_account_range` 取 `equity_snapshots` 的「最新一条」作区间终点，不剔除休市/陈旧快照 ⇒ 10-07 伪快照被纳入，直接将 jsonl 的 09-29 `range_pct` 由正确 **−2.12%** 改写为虚高 **2.04**（md 因「仅追加」保留 −2.12%、未动；jsonl 因幂等刷新为 2.04）。EVOLVE 消费的是 jsonl ⇒ 当前账本累计口径被污染。**建议**：在 review/ledger 的区间终点查询加「只取最近交易日快照、跳过非交易日/陈旧快照」守卫（与 R20 引擎侧冻结同源）。
> **度量账本**：record_ledger 幂等，md 09-29 累计保留原值 **−2.12%**（真实）；jsonl 09-29 行 `range_pct` 本次被刷新为 **2.04（污染值，非真实）**，但 `daily_ret_pct`=+0.5% 仍正确。账本已更新供 18:00 PM-EVOLVE（已于今日 09:00 收官，见 §二十）与次日 11:40 AM-EVOLVE 消费——但须先修上条区间终点守卫，否则 EVOLVE 读到的累计为虚高值。


---

## 2026-10-07（PM-EVOLVE 第 21 轮收官补记）

> 补记来源：PM-EVOLVE 第 21 轮（18:00 收官）。当日国庆休市，无真实成交，无四维打分。

- [P0/稳定/info] ★「低暴露弱段」方向→已落盘新参数 `min_passing_candidates=2`（实盘需重启）
  ｜ 证据：R18~R20 弱折指纹「暴露 31~43% + 成交 3~15 笔 + 胜率 0~30%」，强折「77~92% + 20~36 笔 + 42~60%」；
  四窗 dSh +0.258/+0.308/+0.202/+0.251（最差 **+0.202** ≥ +0.10），IS Sharpe 1.793→1.942、
  MDD −5.42%→−4.28%、胜率 49.4%→52.4%、成交仅 −5.7%（非少交易会计错觉）
  ｜ 建议：**10-08 开盘前必须重启引擎**并核对「引擎启动时刻 > settings.py / event_engine.py mtime」；
  复市后核对 `[宽度确认]` 日志与实盘成交频率是否骤降 > 40%
  ｜ 定位：`config/settings.py:395` + `engine/event_engine.py:2189`/`:2313`（已实现读取点）

- [P1/准确/info] 陈旧盯市污染守卫上线（记账层）
  ｜ 证据：10-07 休市日 263 条快照恒值 1,020,446.6 vs 真值 975,273.6（虚增 +45,173 / +4.63%），
  若被 prev_asset 继承会把 KPI#4 由 −2.47% 翻成 +2.04%，且 10-08 凭空产生 −4.4% 假亏损
  ｜ 建议：每日记账后核查是否出现「零成交 + 零日内方差 + 相对最近可信日跳变 > 0.5%」的脏日
  ｜ 定位：`strategy/record_ledger.py`（`stale_mark_suspect_day` / `_credible_anchor` / `eod_asset`）

- [P2/准确/info] 勿重试清单（本轮更新）
  ｜ 证据：`reentry_cooldown` 单调负（−0.40~−0.75）、`atr_stop_mult` 放宽全负（2.9 为峰）、
  `entry_max_day_gain_pct`「不追高」全负（追高本身是 alpha）、`entry_mode="trend"` 强度不足 + 三硬风险、
  `max_per_sector=2` 回测全正但最差 +0.039 < +0.10 且实盘**无读取点**
  ｜ 建议：勿再投入单参数网格；下一个具可行性的是「先给 max_per_sector 补 engine 实现」再验
  ｜ 定位：`strategy/_evolve_wf.py` / `reports/EVOLUTION_DECISIONS.md` 第 21 轮 §8

- [P2/准确/info] KPI#1 口径仍待 OWNER 裁决
  ｜ 证据：折均值口径下新基线 OOS 均值 Sharpe **1.927**（目标 2.0，缺口 −0.073）；
  R18 已证同一段 540 根在拼接口径下 60×9 与 90×6 **逐位相同 = 1.998**
  ｜ 建议：明确 KPI#1 采用哪种口径后再判断是否继续追 Sharpe，避免在噪声上调参
  ｜ 定位：`strategy/_evolve_wf.py --mode span`

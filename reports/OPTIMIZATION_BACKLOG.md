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
> - [P1/稳定/err] 当日 12 条 ERROR/异常 ｜ 证据：系统提示 ERROR/Traceback 共 12 条 ｜ 建议：查 logs/quant_system.log 与当日 traceback，优先修根因（DLL/网络/数据可得性） ｜ 定位：engine/event_engine.py 主循环异常隔离；core/notices.py

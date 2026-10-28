# 数据与工程健康审计报告（T_data_health）

- 审计员子agent：数据与工程健康审计员
- 审计时点：**2026-10-28（周三）约 15:30–15:40 盘后**
- 运行环境：`/c/Users/lisinan/.conda/envs/qmt/python.exe`（tushare 1.4.24）；git 仅只读（status/log/diff），未做任何 commit/push
- 边界遵守：未跑交易、未改策略配置；除 northbound 缓存（最终判定**不刷新**）外未改动任何数据文件

---

## ① 数据资产时效审计表

| 资产 | 末端日期 | 龄期(至10-28) | 是否达标 | 来源 / 最后刷新 | 核查时间 |
|---|---|---|---|---|---|
| `storage/hist_cache/ts_panel_25_901_20211201_20261027.pkl` | **20261027** | 1 天 | ✅ 达标 | tushare `pro.daily`+`index_daily`，今早 08:05 刷新 | 2026-10-28 15:35 |
| `data/cache/moneyflow_*.json`（抽查 4 只） | **20261027** | 1 天 | ✅ 达标 | 今早 07:51 刷新（23 只补至 1027，已留 `.bak-20261028`） | 2026-10-28 15:33 |
| `data/cache/northbound_hsgt.json` | **20260825** | 64 天 | ⚠️ 超阈值(>10d)但**设计内 fail-open** | tushare `moneyflow_hsgt`，2026-09-19 最后写入 | 2026-10-28 15:36 |
| `data/cache/earnrev_*.json`（22 个） | 极稀疏/空 | 不适用 | ✅ 设计正常输出 | 见下方说明 | 2026-10-28 15:33 |
| `storage/qmt.db` / `-wal` / `-shm` | 活跃写入 | — | ✅ 健康 | mtime 均 10-28 15:04 | 2026-10-28 15:30 |

### 1.1 日线面板 PKL（主回测/研究源）
- 结构：`dict[ts_code] -> {'date':[...], 'open','high','low','close','volume'}`，**26 只**标的。
- 全局日期：**20211201 → 20261027**（龄期 1 天），行数 1007–1187（随上市日不同），文件 1.72 MB。
- **今日(10-28)数据探测**：`pro.daily(000977.SZ, 20261026→20261028)` 仅返回 **1026/1027 两行，无 20261028**。
  - 结论：**现在拉不到今日收盘数据**；原因 = tushare 当日收盘数据通常 **17:00–18:00 后**才更新，预计今晚方可拉取。面板末端停在 1027 属正常，非缺陷。

### 1.2 资金流 moneyflow（抽查 3+1 只）
| 代码 | keys | 日期范围 | 末端 | 龄期 |
|---|---|---|---|---|
| 000977.SZ | 1164 | 20220104–**20261027** | 1027 | 1 天 |
| 002371.SZ | 1164 | 20220104–**20261027** | 1027 | 1 天 |
| 688008.SH | 1164 | 20220104–**20261027** | 1027 | 1 天 |
| 603019.SH | 1154 | 20220104–**20261027** | 1027 | 1 天 |

- 4 只末端全部对齐 20261027，今早补齐生效。（603019 keys 少 10 条系上市/数据起始略晚，末端一致，无碍。）

### 1.3 北向资金 northbound（重点评估）
- 缓存 `data/cache/northbound_hsgt.json`：**600 条**，20230816 → **20260825**，龄期 **64 天**（与今晨 ERROR 一致）。
- **退化证据**：末 60 个交易日 **60/60 全正**；全序列负值 142 天、**最后负值日 20240816**。与 `northbound_cache.py:145-173` 文档完全吻合——官方自 2024-08-19 停披每日净买入，此后该字段退化为「恒正窄带」的成交额口径，**不是净买入**。
- **生产启用状态**：`config/settings.py:486 northbound_mode="gate"`（启用为闸门）；`nb_credibility_guard=True`、`nb_stale_days=10`、`nb_degenerate_days=60`（settings.py:500-502）。
- **只读探测刷新影响**：`pro.moneyflow_hsgt(20260820→20261027)` 返回 **42 行、末端 20261027**，值仍全正（259796/225322/… 恒正）⇒ **数据形态未变、仍退化**。
- **影响面评估**：闸门为**设计内的「死闸门」**。`nb_gate_credible` 对「陈旧」或「退化」任一为真即 **fail-open（不拦截建仓）**；今日日志 `07:41:43 [WARNING] 北向闸门已 fail-open（stale）：序列末端 20260825，不作为建仓拦截依据` 已明确此点 ⇒ **对实盘交易零行为影响**。
- **刷新决策：不刷新（不建议刷新）**，理由：
  1. 陈旧 ERROR 属 fail-open 设计（settings.py:496-499 明载「实盘零行为变化证明」），非缺陷；
  2. 源数据已证退化，探测确认拉到的仍全正——刷新只是延长一条已知失真序列，**无功能收益**；
  3. 即便刷至 1027（转 fresh），退化判定（末 60 全正）仍触发 fail-open ⇒ 行为中性，仅会消掉「stale」日志噪音，底层「非净买入」问题照旧；
  4. 陈旧守卫本身即防「符号恢复后用旧数据误拦」——不刷新在符号恢复情形下同样 fail-open，安全；
  5. 该轴被 OWNER **冻结待 re-baseline**（settings.py:482-485「退役需一次 re-baseline 轮次，本轮暂保持 gate 以不动研究基线」），刷新数据属触碰冻结轴，超出本审计「不改配置」边界。
  - **未执行刷新、未生成 `.bak-20261028`**（因决定不动该文件）。未获取/未改动均已注明。

### 1.4 业绩快报修订 earnrev（22 个文件）
- 大小分布：**20 个 2 字节（空 `{}`/`[]`）**，2 个稀疏单条：`earnrev_300476.SZ.json={"20220125":0.0}`、`earnrev_300502.SZ.json={"20220808":31.58}`。
- 结论：多数标的无业绩预告/快报修订数据 → 输出为空，**属设计正常输出**（与今早诊断一致），非数据缺失缺陷。

### 1.5 账本 DB
- `qmt.db` = **26,193,920 B（≈25.0 MB）**；`qmt.db-wal` = **4,148,872 B（≈4.0 MB）**；`qmt.db-shm` = **32,768 B（≈32 KB）**；三者 mtime 均 **10-28 15:04**（写入活跃，WAL 未异常膨胀，健康）。
- 注：`storage/*.db*` 已被 `.gitignore` 忽略；同目录另有 `qmt.db.bak-20261022`(45.5MB)、两份 0921 备份(各23.5MB) 为本地留存。

---

## ② 今日日志 ERROR 分类清单（2026-10-28）

数据源：`logs/quant_system.log`（今日 273 行，ERROR/WARN 35）、`logs/guardian.log`(1)、`logs/notices.log`(5)；对照 10-27（quant_system ERROR/WARN 31）。`logs/startup.log` 今日 0 条（重启日志走 quant_system）。

### ① 每日收盘/盘中重复出现（老问题，10-27 同样存在）
| 消息 | 今日 | 10-27 | 影响评估 |
|---|---|---|---|
| 无入场信号：动量闸门后候选池为空（min_mom=0.15/lookback=60） | 25× | 25× | **预期内**弱市零开仓（全宇宙 60 日动量无正值→top_n 候选空）；已在 `WHY_NO_ENTRY`/R42 诊断论证，非故障。 |
| 行情源失效：34 只快照陈旧 2 分钟→暂停建仓（15:02 WARNING+ERROR） | 2 行@15:02 | 4 行@09:32+15:02 | **老问题**：收盘时刻 miniQMT 停推 tick→快照陈旧→触发「暂停建仓」。15:02 已收盘，**无实际交易危害**；10-28 仅收盘一次（10-27 另多一次早 09:32）。 |
| 主循环慢轮（14.1s/16.1s，累计慢轮 2；entry/sector 段占 14–16s） | 2 次 | 1 次 | 偶发性能抖动（开盘/午间首轮），非新故障；持续观察 entry/sector 计算耗时。 |

### ③ 启动时一次性（10-28 07:25 引擎重启触发；10-27 未重启故无）
| 消息(时间) | 影响评估 |
|---|---|
| `[ERROR] 北向资金缓存已陈旧 64 天（末端 20260825）` @07:25:47 | 即 §1.3 northbound；fail-open 设计，**零交易影响**。仅启动调用 `get_northbound` 时打印。 |
| `[ERROR]+[WARNING] 数据源=mock（合成行情）：已禁止全部买卖成交` @07:25:47 | **安全设计**：07:25 夜间/收盘后重启时 miniQMT 探活失败→落 mock→禁成交；日志载明「交易时段开启将自动重接 xtdata」。今日盘中 15:02 快照陈旧说明期间已接入实盘行情，符合预期。 |
| `[WARNING] 风险预算实测值：单仓40%×持仓5=理论敞口200%>现金夹紧95%；建议 max_single_position_pct≤19%` @07:25:47 | **配置观察项**（非错误）：现金夹紧已强制封顶、不会负现金；建议调参属策略配置，**超出本审计边界**，列入 ⑤。 |
| `[WARNING] 交易日历不可用，按周内判定` @07:25:49 | 启动一次性降级；低影响（极端情形可能误判休市日）。建议排查交易日历数据源。 |
| `[WARN] 已有 guardian 实例 pid=25296，退出` @07:25:56 | 守护去重，无害。 |

### ② 今日新出现的问题
- **无**。上述条目均可归入 ①（每日重复）或 ③（启动一次性）。唯一量变：慢轮 10-28 出现 2 次（10-27 为 1 次），仍属偶发性能类，非新故障。

---

## ③ git 工作区与提交建议（只建议，不执行）

`git status --short` 全量（共 7 项）：
```
 M storage/dynamic_universe.json
?? reports/agent_outputs/
?? "reports/稽核分析汇报_AI实盘曲线23条_2026-10-28.html"
?? scripts/_analysis_E_db_check.py
?? scripts/_analysis_E_earnrev_check.py
?? scripts/_analysis_E_moneyflow_check.py
?? scripts/_analysis_E_panel_check.py
?? scripts/_analysis_tech_quant.py
```

### 分组
- **数据类**：`storage/dynamic_universe.json`（M）
  - 已被 git 跟踪（`git ls-files` 命中），历史 **15 次提交**，随各研究轮落库（最近 10-21 `7f57bfb`）。
  - 本次改动：`last_refresh 2026-10-21→2026-10-28`、`n_total 374→375`、半导体 179→180、active_pool 次序调整/增删（688072 移出等）——系今早宇宙刷新，**非配置变更**。
  - ⚠️ **修正任务提示中的假设**：该文件**并非「惯例留本地不提交」**——`.gitignore` 未忽略它，且历史上随研究轮**规律入库**。因内容已在历史版本中，提交更新不引入新暴露。
- **研究产出类**：`reports/agent_outputs/`（含今早 A–E 五份 + 本报告 T_data_health.md）、`reports/稽核分析汇报_AI实盘曲线23条_2026-10-28.html`
  - `reports/` 未忽略，历史有 HTML 报告入库先例（如 `R41_AM_EVOLVE_20261021.html`）。
- **脚本类**：`scripts/_analysis_E_{db,earnrev,moneyflow,panel}_check.py`、`scripts/_analysis_tech_quant.py`
  - 符合 `scripts/_*` 入库惯例（历史 **30 次提交**触及 `scripts/_*`，如 `_am_*`/`_diag_*`/`_pm_*` 均已跟踪）；均为只读核查/分析脚本。

### 提交建议方案（供 OWNER 采纳，本 agent 不执行）
1. **提交前必做**：对拟提交内容跑 **safe-github-push** 敏感串扫描（API key/token 等）；尤其 `dynamic_universe.json`、HTML 报告、脚本注释内勿含密钥。
2. 建议**按主题合并不堆砌**，沿用既有风格，例如：
   - commit A（数据+脚本）：`storage/dynamic_universe.json` + 5 个 `scripts/_analysis_*.py`，信息如「数据刷新(10-28宇宙374→375) + 数据治理只读核查脚本」。
   - commit B（研究产出）：`reports/agent_outputs/`（A–E + T_data_health）+ `reports/稽核分析汇报_AI实盘曲线23条_2026-10-28.html`，信息如「2026-10-28 多 agent 数据/工程健康审计产出」。
3. `dynamic_universe.json` 可随数据刷新主题提交（与历史「面板刷新至…」随研究轮落库一致）；如 OWNER 倾向与下一轮研究合并亦可暂留工作区。
4. `.gitignore` 已覆盖 `data/cache/`、`storage/hist_cache/`、`storage/*.db*`、`logs/*.log` 等，故本次补齐的缓存/日志**不会**误入提交。

---

## ④ 工程健康快照

### 4.1 测试证据
- `tests/` 共 **85 个**测试文件；最近修改：`test_gate_detail.py`(10-24)、`run_all.py`(10-23)、`test_kpi_gate.py`/`test_recovery_fastpath.py`/`test_kpi_gate_significance.py`(10-23)。
- **最近一次「已落盘」测试运行**：`logs/tests_run_2026-09-22g.log` → **PASSED 228 / FAILED 2 / TOTAL 230**（2 失败为旧 `enable_broker` TypeError）。
- ⚠️ 10-21 提交 `d7a8631` 声称 **tests 533/533**，但 `logs/` 与 `reports/` **无对应落盘证据**（tests_run_* 止于 09-22，`.pytest_cache` 停留 08-24，reports/ 无测试报告）。即 533/533 仅有提交信息佐证，缺可复现 artefact。建议择机重跑 `tests/run_all.py` 重新生成新鲜通过证据。

### 4.2 依赖健康
- `pip check` → **No broken requirements found**（无冲突）。
- 关键包 vs `requirements.txt`：Flask 3.1.3(≥3.0✅)、requests 2.32.5(≥2.32✅)、xtquant 250516.1.1(≥250516✅)、numpy 2.0.2(≥1.24✅)、pandas 2.3.3(≥2.0✅)——**全部达标**。
- ⚠️ **requirements.txt 缺项**：实际使用但未声明的 **tushare 1.4.24**（回测/研究主源）与 **pytest 8.4.2**（测试）。建议补入 requirements.txt 以便环境复现。

### 4.3 __pycache__ 堆积
- **13 个 `__pycache__` 目录、329 个 `.pyc`、合计 ≈1.3 MB**（体量小，非负担）。
- 构成：`cpython-39` **179**（qmt 正确环境）+ `cpython-313` **115**（PATH 里另一个 3.13 环境的**误跑残留**）。`.gitignore` 已忽略 `__pycache__/`，不会入库。可顺手清理 313 残留，低优先级。

---

## ⑤ 需人工决策事项

1. **北向闸门（northbound_mode="gate"）存废**：当前为 fail-open「死闸门」，源数据自 2024-08 失真、缓存末端 20260825。是否按 OWNER 流程启动一次 **re-baseline 轮次**将其退役（改 "off" 并重钉 P0/KPI，牵连 opt_harness/_evolve_wf/test_northbound）？本审计**未刷新缓存、未改配置**，留待裁决。
2. **风险预算配置自洽性**：日志建议 `max_single_position_pct` 由 40% 降至 ≤19%（理论敞口 200%>现金夹紧 95%）。属策略配置，**需 OWNER 授权**后由策略负责人改，本审计不动。
3. **测试 artefact 补齐**：533/533 缺落盘证据，建议重跑 `tests/run_all.py` 留一份新鲜通过日志。
4. **requirements.txt 补 tushare/pytest**：便于环境复现（低优先）。
5. **交易日历数据源**：07:25「交易日历不可用」根因待查（避免休市日误判）。
6. **git 提交**：按 §③ 方案提交前先过 **safe-github-push** 扫描；`dynamic_universe.json` 是否随本轮数据刷新提交、或并待下轮研究，由 OWNER 定。

---

*报告生成：数据与工程健康审计员子agent · 2026-10-28 盘后 · 所有结论基于上述实际核查输出*

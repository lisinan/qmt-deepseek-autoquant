# DB 增长治理报告（P2）— 2026-10-22

> 只读体检 + dry-run 预演。**本次未对生产库执行任何 DELETE / VACUUM**（引擎运行中，pid 见 logs/engine.pid）。
> 复现命令：`python main.py --db-stats`、`python main.py --prune-db 30 --dry-run`。

## 结论摘要（≤5 行）

1. storage/qmt.db 现 43.4 MB，膨胀主因是 sector_recommendations **123,239 行**（每 5 tick × 38 只全量持久化），占观测类行数 91%。
2. `--prune-db 30 --dry-run` 预演：可删 **78,808 行**（signals 16,463 + risk_snapshots 6,492 + sector_recommendations 55,853），orders/fills/equity_snapshots 不在可删范围。
3. **llm_rerank 未纳入 `PRUNABLE_TABLES`**（当前 0 行、刚启用，暂无增长）；建议主线以极保守阈值（如 90 天）将其加入可 prune 列表，本次仅报告、不改 storage/db.py。
4. 增长速率：sector_recommendations 约 3,600 行/交易日（38 只 × ~95 次/日），按此速率 30 天窗口稳态约 10.8 万行、约 30+ MB/年，可控但需定期 prune+vacuum。
5. 绝对不能删：orders、fills、equity_snapshots、engine_state（账务证据/重启状态）。

## 1. 现状表（`--db-stats`，2026-10-23 采集）

| 表 | 行数 | 时间跨度 | 性质 |
|---|---:|---|---|
| sector_recommendations | **123,239** | 2026-09-01 .. 2026-10-22 | 高频观测（每 5 tick × 38 只） |
| signals | 16,872 | 2026-08-30 .. 2026-09-30 | 高频观测 |
| risk_snapshots | 7,468 | 2026-08-30 .. 2026-10-22 | 高频观测 |
| equity_snapshots | 4,390 | 2026-09-22 .. 2026-10-22 | **账务证据（保）** |
| ai_analyses | 40 | 2026-08-25 .. 2026-09-29 | 观测（量小） |
| orders | 38 | 2026-09-22 .. 2026-10-09 | **账务证据（保）** |
| fills | 38 | 2026-09-22 .. 2026-10-09 | **账务证据（保）** |
| llm_rerank | 0 | —（新表，2026-10-21 启用） | 观测（未纳入 prune） |
| engine_state | 1 | 2026-10-22 | **重启状态（保）** |
| 文件大小 | 43.4 MB | | |

## 2. dry-run 预演（`--prune-db 30 --dry-run`，cutoff=2026-09-23）

| 表 | 预演删除行数 |
|---|---:|
| signals | -16,463 |
| risk_snapshots | -6,492 |
| sector_recommendations | -55,853 |
| orders / fills / equity_snapshots | 未动（账务证据） |
| **合计** | **-78,808** |

> 注：signals 时间跨度止于 2026-09-30（近月引擎未再写 signals），故 30 天窗口几乎全删。
> 实际执行需主线裁决：`python main.py --prune-db 30`，随后在**引擎停止窗口**跑 `--vacuum-db`（WAL 下 VACUUM 需独占，引擎在跑会失败）。

## 3. prune_before 保留表清单核查

`storage/db.py::Storage.PRUNABLE_TABLES = ("signals", "risk_snapshots", "sector_recommendations")`。

- **llm_rerank 未纳入**（2026-10-21 C-B5-3 新增表）。当前 0 行，但启用后每 tick 一次重排序（含 ranked_json/raw 全文），单行体积大，若 LLM 每 5 tick 调用一次，量级与 sector_recommendations 同阶。
- `table_stats()` 的表清单同样未含 llm_rerank / engine_state（巡检盲区，本报告用 scripts/report_db_growth.py 补齐）。
- **建议（仅报告，不改代码）**：主线在 prune 逻辑中将 llm_rerank 加入 `PRUNABLE_TABLES`，阈值建议**极保守的 90 天**（raw 全文对 LLM 行为复盘有价值，不宜 30 天）；同时在 `table_stats()` 表清单补 `llm_rerank`、`engine_state`。ai_analyses 仅 40 行，暂不必纳入。

## 4. 绝对不能删的表（清单）

| 表 | 理由 |
|---|---|
| **orders / fills** | 交易流水账务证据，删了无法重建持仓与盈亏（db.py 注释明示「永不删除」） |
| **equity_snapshots** | 权益曲线/收益率/最大回撤的复盘依据（prune_before 明确保留） |
| **engine_state** | 单例 id=1，引擎重启延续的本地账本（paper 持仓/现金/连亏状态） |
| ai_analyses | 量小（40 行），且是 AI 结论复盘证据，默认保留 |

## 5. 建议的定期维护命令与频率

| 频率 | 命令 | 说明 |
|---|---|---|
| 每日（盘后） | `python main.py --db-stats` 或 `python scripts/report_db_growth.py` | 只读巡检，行数/跨度/文件大小入日志 |
| 每月（引擎停止窗口） | `python main.py --prune-db 30` | 删 30 天前的高频观测行（主线裁决后执行） |
| 每次 prune 后（引擎停止窗口） | `python main.py --vacuum-db` | DELETE 不缩小文件，必须 VACUUM 回收；WAL 下需独占，先 `python main.py --stop` |
| 一次性治理 | `python main.py --prune-db-before YYYY-MM-DD --dry-run` → 确认后去掉 `--dry-run` | 膨胀集中在最近几天时相对窗口删不掉，用绝对日期 |

> 停引擎窗口的顺序：`--stop` → `--prune-db N` → `--vacuum-db` → 重启。VACUUM 期间生产不可写，属计划内停机。

## 6. 这个结论会不会推翻既有落盘决策

- **不会**。本次全部为只读查询（`--db-stats`、`--prune-db 30 --dry-run`、sqlite 直连 COUNT）+ 新增只读巡检脚本；未 DELETE、未 VACUUM、未改 `storage/db.py`、未改 `config/settings.py`、未动 `PRUNABLE_TABLES`。
- 生产库删除（`--prune-db --apply` 与 `--vacuum-db`）**由主线决定**；本报告仅给出预演数字与建议频率。
- 唯一的代码增量是新增 `scripts/report_db_growth.py`（只读）与 `main.py`/`storage/db.py` 的**建议清单**（未实施）——不改变任何现有行为。

## 附：复现

```
python main.py --db-stats
python main.py --prune-db 30 --dry-run
python scripts/report_db_growth.py          # 本报告新增的每日巡检（只读）
```

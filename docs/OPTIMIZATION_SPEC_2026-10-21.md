# 优化实施规格（2026-10-21）

> 依据：`reports/OPTIMIZATION_REVIEW_2026-10-21_multimodel.md`
> 本文件是**实施契约**：所有并行 agent 必须严格按此执行，文件所有权不可越界。

## 0. 总则（所有 agent 必须遵守）

1. **只改自己名下的文件**（见 §1）。发现必须改别人文件的地方 → 在报告里写「跨包需求」，不要动手。
2. **最小改动**：不加新依赖、不重排格式、不改无关注释、不做"顺手重构"。
3. 改完必须 `python -m py_compile <改过的每个 .py>`（解释器：`C:\Users\lisinan\.conda\envs\qmt\python.exe`）。
4. **不要跑完整测试套件**（`tests/run_all.py` 约 72s，且会与其它 agent 抢 SQLite）；只允许跑自己新增的定向单测（若有）。
5. **不要启动** `main.py` / Flask / guardian / 任何自动化，不要下单，不要联网写操作。
6. 每处改动都要能被 `git diff` 看清；不要新增临时文件（测试除外）。
7. 报告格式（最后一条消息）：`改了什么 / 行号 / 契约是否偏离 / 需要别人配合什么 / 已知风险`，≤ 40 行。

## 1. 文件所有权

| 工作包 | 模型 | 独占文件 |
|---|---|---|
| WP-A 引擎核心 | `workbuddy/deepseek-v4-pro` | `engine/event_engine.py`、`risk/manager.py`、`config/settings.py`、`strategy/backtest_daily.py`（仅 cost_pct 默认值一行） |
| WP-B 券商与模型 | `workbuddy/glm-5.3` | `core/broker.py`、`core/data_models.py` |
| WP-C 数据层 | `workbuddy/minimax-m3.1-flash` | `data/northbound_cache.py`、`data/moneyflow_cache.py`、`core/qmt_client.py`、`core/market_calendar.py`、`data/hist_cache.py`（只读校验，不要求改） |
| WP-D 仪表板 | `workbuddy/kimi-k3-2` | `web/templates/index.html`、`web/static/js/dashboard.js`、`web/static/css/main.css`、`web/routes.py` |
| WP-E 运维链 | `workbuddy/deepseek-v4.1-flash` | `scripts/health_check.py`、`scripts/guardian.py`、`scripts/startup_all.bat` |
| WP-F 测试与文档 | `workbuddy/space-bunny` | `tests/**`、`README.md`、`docs/ARCHITECTURE.md`（在 A–E 合并后执行） |

## 2. 契约（C1–C12）

### C1 `Position.available`（WP-B）
`core/data_models.py::Position` 增加字段 `available: int = 0`（放在 `quantity` 之后），含义「券商可用/可卖数量」。paper 模式由引擎按 T+1 维护；live 由券商同步写入。

### C2 `get_positions` 失败语义（WP-B）
`core/broker.py::QMTBroker.get_positions(account="cash") -> Optional[List[dict]]`：
- connect 失败 / 账户缺失 / 查询异常 → **返回 `None`**（与 `get_asset` 一致）；
- 成功 → 返回 list（可能为空 list）。
同步修改 `get_orders` / `get_trades` 为同样语义（None=未知）。docstring 写明「None 表示查询失败，[] 表示确认无持仓」。

### C3 撤单封装（WP-B）
`core/broker.py` 新增：
```python
def cancel_order_stock(self, account: str, order_id: str) -> Dict:
    """返回 {"ok": bool, "reason": str}；order_id 非法或异常时 ok=False。"""
```
使用 `self._trader.cancel_order_stock(acc, int(order_id))`；失败路径只 log warning，不抛。

### C4 对账区分「未知」与「空仓」（WP-A）
`engine/event_engine.py::_sync_broker_positions`：
- `raw = broker.get_positions(acct)`；若 `raw is None` → `logger.warning` + `self._sync_fail_count += 1`（新属性，`__init__` 初始化为 0），**本轮直接 return，不改动 `self._positions`**；
- 成功拿到 list 时 `self._sync_fail_count = 0`，按原逻辑合并，并写入 `pos.available = int(p.get("available") or 0)`；
- 连续 3 次失败 → `system_notice("ERROR", "风控", "券商持仓查询连续失败 N 次，本地账本未更新")`（按天去重，复用现有 `_notice_*` 风格即可）。

### C5 paper 成本（WP-A）
- `config/settings.py` 新增 `TRADE_COST_PCT = 0.0015  # 单边综合成本（佣金+印花税+滑点）`；
- `strategy/backtest_daily.py::BacktestConfig.cost_pct` 默认值改为 `TRADE_COST_PCT`（`from config.settings import TRADE_COST_PCT`，注意循环导入：backtest 已 import config 则安全，若不能则用 `float(os.getenv(...))` 兜底并在注释说明）；
- `engine/event_engine.py` paper 分支：BUY `self._cash -= qty*price*(1+TRADE_COST_PCT)`；SELL `self._cash += qty*price*(1-TRADE_COST_PCT)`；
- `Fill.amount` 保持 `qty*price` 不变（避免污染报表），成本体现在现金与权益。

### C6 live 风控状态持久化（WP-A）
- `_save_engine_state()` 在 **所有模式** 被调用（`_persist_equity`、`_shutdown`、`_reset_daily_if_needed` 附近的三处 `if self.exec_mode == "paper"` 去掉）；
- `_restore_engine_state()` 增加参数 `restore_ledger: bool = True`：`restore_ledger=False` 时**只恢复** risk 字段（`peak_asset / daily_pnl / consec_loss / halted / daily_trade_count / risk_state`），不恢复 positions/cash；live 启动时调用 `_restore_engine_state(restore_ledger=False)`；
- live 下若 DB 无 peak_asset，则用 `SELECT MAX(total_asset) FROM equity_snapshots` 兜底（沿用 `_credible_peak_equity` / `sanitize_peak_equity` 已有逻辑）；
- 启动横幅追加一行：`恢复风控状态: peak=... consec_loss=... halted=... daily_trade_count=...`（便于肉眼验证）。

### C7 AI 否决接线（WP-A）
- `config/settings.py` 新增 `AI_VETO_MODE = "log"`（`"off"` / `"log"` / `"veto"`）；
- `_fire_ai` 的回调里：`if ai.stance == "bearish" and abs(ai.confidence) >= 0.7:` → 写入 `self._ai_veto[code] = (date.today(), round(ai.confidence,3))`，`logger.warning("AI 看空 %s conf=%.2f（%s 模式）", ...)`；
- `_handle_buy` 开头：`if settings.AI_VETO_MODE == "veto" and self._ai_veto_blocks(code): self._log_reject_once(code, "ai_veto"); system_notice(...); return False`；
- `_ai_veto` 在 `__init__` 初始化为 `{}`，`_reset_daily_if_needed` 中清空；
- README 语义保持「标记下一轮不买入」，默认 `log` 不改变任何交易行为。

### C8 决策可观测字段（WP-A）
`EventEngine.snapshot()` 增加：
- `"gate_funnel": {...}`：最近一次入场漏斗（`universe_n / momentum_pass_n / trend_up_n / bias_ok_n / score_ok_n`，缺失填 0）；
- `"no_entry_reason": str`：最近一次的「无入场信号」原因（复用 `_maybe_log_no_signal` 的文案）；
- `"ai_veto_n": int`：当日 AI 否决数；
- `"degraded": list[str]`：由 `data_mode != "xtdata"`、`risk.halted`、`market_staleness`.stale 组成，元素为简短中文串。
所有字段必须在无数据时也可安全序列化（JSON）。

### C9 交易时段与日历（WP-C）
- `core/market_calendar.py::DEFAULT_SESSIONS` 改为 `(("09:30","11:30"), ("13:00","15:05"))`；
- `_xtdata_trading_days` 返回 None（回退到「周内判定」）时，`logger.warning("交易日历不可用，按周内判定（可能把休市日当交易日）")`，并把 `use_calendar` 结果缓存到 `_HOLIDAY_CACHE["degraded"]=True`；
- 新增 `calendar_health() -> dict`（`{"degraded": bool, "source": "xtdata"|"weekday"}`）供 health_check 读取。

### C10 外部缓存龄期断言（WP-C）
`data/northbound_cache.py` 与 `data/moneyflow_cache.py`：
- 新增模块级常量 `MAX_STALE_DAYS = 10`（自然日）；
- 读取函数在命中缓存后校验「序列末端日期」与今天差；超限 → `logger.error("...缓存已陈旧 N 天（末端 YYYYMMDD），数据可能失效")`，并**照常返回数据**（保持 fail-open，但必须可见）；
- 暴露 `cache_health() -> {"last_date": "YYYYMMDD", "age_days": N, "stale": bool}`（从已加载序列算，不联网）。

### C11 复权口径（WP-C）
`core/qmt_client.py` 的 `get_market_data_ex` 调用补 `dividend_type="front_ratio"`（若该 API 在本机 xtquant 版本不支持该 kwarg，则保持原样并在注释里写明「版本不支持」——以 `py_compile` + 一次离线调用验证为准，不联网）。研究侧 `data/hist_cache.py` 已是 `front_ratio`，无需改。

### C12 运维链（WP-E）
- `scripts/health_check.py::status()` 增加 `engine_progressing`：读 `storage/qmt.db` 的 `equity_snapshots` 最新 `ts`（只读连接），若当前处于交易时段且最新快照距今 > 15 分钟 → `degraded.append("engine_stalled")`；
- 新增 `--json` 输出里包含 `degraded` 列表（若已有 `--json` 就扩展）；
- `scripts/guardian.py`：单实例守卫改为原子占位 `os.open(PID_FILE, O_CREAT|O_EXCL|O_WRONLY)`；已被占用则读旧 pid，存活即退出；`degraded` 非空时写一条 `[GUARDIAN-ALERT]` 日志（按天去重）；
- `scripts/startup_all.bat`：修 `:59` 的 `echo` 未配对 `(`（改成 `echo ... [pythonw]`）；把 pid 判定移出括号块（`for /f` + 单独 `if defined`）；保持文件为 CRLF。

## 3. 验收（由 WP-F 与主线复核）

1. `python -m py_compile` 全部通过；
2. `python tests/run_all.py` 全绿（允许把因本次改动而需要更新的旧断言一并修改，但**不得删除用例**）；
3. 新增 `tests/test_optimization_20261021.py` 覆盖：C1/C2/C4（None 不清仓）、C5（paper 成本）、C6（live 恢复风控字段）、C7（veto 开关语义）、C9（时段）、C10（龄期断言触发）；
4. `git diff --stat` 只包含 §1 授权文件 + 测试 + 文档。

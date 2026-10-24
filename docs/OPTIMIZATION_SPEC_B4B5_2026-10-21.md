# B4 + B5 实施规格（基准管道 / rerank 缓存与落库）

> 依据 `reports/OPTIMIZATION_REVIEW_2026-10-21_multimodel.md` §4 的 B4、B5。
> 两个工作包，**文件所有权互斥**，可按波次并行/串行。

## 0. 铁律

1. 不改资金路径（`_handle_buy/_handle_sell/_reconcile_pending_orders`、`risk/manager.py`）。
2. paper/live 行为不变；B5 只改「何时调 LLM / 结果落哪」，不改变交易决策。
3. 最小改动；改完 `py_compile`；不跑 `tests/run_all.py`（由 WP-L 统一跑）。
4. 不启动 main.py / Flask / guardian；不联网写操作。

## 1. 文件所有权

| 工作包 | 模型 | 独占文件 |
|---|---|---|
| WP-J（B5） | `workbuddy/glm-5.3` | `ai/llm_reranker.py`、`ai/deepseek_client.py`、`storage/db.py`、`config/settings.py`、`engine/event_engine.py`（仅 `_fire_llm_rerank` / `_evaluate_sectors` 触发点 / `llm_health`） |
| WP-K（前端） | `workbuddy/kimi-k3-2` | `web/templates/index.html`、`web/static/js/dashboard.js`、`web/static/css/main.css` |
| WP-I（B4，第二波） | `workbuddy/deepseek-v4-pro` | `strategy/benchmark.py`（新建）、`engine/event_engine.py`（仅 `snapshot` / 基准缓存） |

## 2. 契约

### C-B4-1 `strategy/benchmark.py`（WP-I）
```python
def compute(since: str, codes: list, index_code: str,
            count: int = 160) -> dict:
    """等权买入持有基准。
    since: "YYYY-MM-DD"；codes: 股票池；index_code: 宽基（如 MARKET_INDEX_CODE）。
    返回 {"universe_bh_pct": float|None, "index_pct": float|None,
          "n_codes": int, "since": str, "as_of": "YYYY-MM-DD",
          "note": str}   # 数据不足时 *_pct=None 并在 note 说明
    """
```
- 数据源优先 `strategy.backtest_daily.load_daily(code, count)`（已有缓存），失败则返回 None 值，**不得抛异常**。
- 等权：`since` 当日收盘为基点，逐日等权（停牌沿用上一价），收益率 = 末值/基点 − 1。
- 宽基同法。禁止联网；只用本地缓存。

### C-B4-2 `snapshot()["benchmark"]`（WP-I）
```json
{"ts": "ISO", "since": "YYYY-MM-DD", "as_of": "YYYY-MM-DD",
 "account_pct": 0.0123, "universe_bh_pct": 0.0234, "index_pct": 0.0111,
 "cash_pct": 0.0, "excess_pct": -0.0111, "basis": "initial_cash",
 "n_codes": 38}
```
- `account_pct = (当前总资产 / INITIAL_CASH) - 1`，`basis="initial_cash"`（当前账户自 2026-09-21 复位，初始资金即 100 万）；
- `excess_pct = account_pct - universe_bh_pct`（任一为 None 则 None）；
- **缓存**：最多每 30 分钟重算一次（`self._benchmark_ts`），`snapshot` 高频调用不得每轮拉日线；
- 计算放 try/except，失败时返回 `{"basis": "unavailable"}`，不得抛。
- 每日首次算成时追加一行到 `logs/benchmark_daily.jsonl`（`{"date","account_pct","universe_bh_pct","index_pct","excess_pct","n_codes"}`，按日期去重，跨进程安全用 `a` 模式）。

### C-B5-1 rerank 缓存键（WP-J）
`ai/llm_reranker.py::rerank` 的缓存键去掉 `market_ctx`（它每 3 秒变一次，导致 0% 命中），改为：
```python
cache_key = json.dumps({"codes": codes, "heat": heat_sig}, sort_keys=True)
```
（`heat_sig` 已是 0.1 精度；如仍抖动，把 heat 再 round 到 1 位整数。）
保留 TTL 语义；命中时 `result.cached=True` 且计入 `cached_hits`。

### C-B5-2 失败可诊断（WP-J）
- `LLMReranker` 新增：`calls`、`cached_hits`、`last_ok_ts`、`last_error_ts`、`last_error`（含模型名与状态码，例如 `"deepseek-chat HTTP 503"`）。
- 失败分支（无解析结果 / ranked 为空 / 客户端异常）必须 `logger.warning("LLM rerank 失败 model=%s err=%s", model, err)`，不能静默。
- `snapshot()` 返回：`{"enabled","model","calls","cached_hits","last_call_ts","last_ok_ts","last_error","last_error_ts","cache_size"}`。
- `ai/deepseek_client.py`：非 200 时把 `f"{base_url} HTTP {status}"` 写进 `_last_error`（若已有等价字段则复用），供上面拼接。

### C-B5-3 结果落库（WP-J）
`storage/db.py`：
```sql
CREATE TABLE IF NOT EXISTS llm_rerank (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, model TEXT, macro_view TEXT,
  ranked_json TEXT, adjustments_json TEXT, raw TEXT, cached INTEGER
);
```
新增 `save_llm_rerank(ts: str, model: str, macro_view: str, ranked: list, adjustments: dict, raw: str, cached: bool) -> int`（沿用现有 `save_*` 的 `except: logger.warning; return 0` 风格）。

### C-B5-4 触发改为墙钟 + 交易时段（WP-J）
- `config/settings.py` 新增 `LLM_RERANK_INTERVAL_SEC = 600`。
- `engine/event_engine.py::_evaluate_sectors` 里把 `self._tick_count % self._llm_rerank_interval == 0` 改为：
  ```python
  if (self.llm_reranker is not None and self.llm_reranker.enabled
          and now_ts - self._last_llm_rerank_ts >= LLM_RERANK_INTERVAL_SEC
          and is_trading_time(datetime.now())
          and self.sector_scorer.recommendations):
      self._last_llm_rerank_ts = now_ts
      self._fire_llm_rerank(market_data)
  ```
  新增属性 `self._last_llm_rerank_ts: float = 0.0`；`is_trading_time` 从 `core.market_calendar` 导入。
- `_fire_llm_rerank` 成功回调里调 `storage.save_llm_rerank(...)`；失败（result 为 None）打一条 WARN（含 `llm_reranker.last_error`）。
- `llm_health()` 改为合并 `self.llm_reranker.snapshot()` 与 `deepseek.health()`（当前 `getattr(self.llm_reranker, "client", None)` 是空转）。

### C-B5-5 前端（WP-K）
- 首屏「今日决策条」增加第 4 项：**相对基准超额**，读 `snapshot.benchmark.excess_pct`（百分比 2 位小数，正绿负红；缺失显示「—」），并在 title 里显示 `account / universe_bh / index` 三行。
- LLM 面板状态行显示 `calls / cached_hits / last_error`（读 `snapshot.llm_rerank.health` 或 `/api/llm/rerank/latest`，读现有结构，字段缺失显示「—」）。
- 不新增请求；不改变现有渲染顺序。

## 3. 验收（WP-L）
1. `strategy/benchmark.compute` 用合成面板单测（不联网）：等权 BH 数值正确、数据不足返回 None、不抛异常。
2. `snapshot()["benchmark"]` 含全部键，30 分钟内重复调用不重复计算（用计数器断言）。
3. rerank 缓存：同一 codes/heat、不同 market_ctx 两次调用只打一次 client（`cached_hits==1`）。
4. 失败分支产生 WARN 且 `last_error` 含模型名。
5. `save_llm_rerank` 落库可查（内存 sqlite）。
6. 触发条件：非交易时段不调用；间隔未到不调用。
7. 全量 `tests/run_all.py` 全绿（当前基线 572）。

## 4. 实施结果（2026-10-21 晚）

实现包：WP-J（B5，glm-5.3）+ WP-I（B4，deepseek-v4-pro）+ WP-K（前端，kimi-k3-2）+ WP-L（验收，space-bunny）。
最终：`tests/run_all.py` **586/586 通过**（B4/B5 新增 14 条用例）。

落地清单：
- B4：`strategy/benchmark.py::compute`（等权 BH / 宽基，纯本地、异常不外抛）；`snapshot()["benchmark"]` 十键契约；后台线程 + 1800s 缓存（绝不在同步路径调 load_daily）；`logs/benchmark_daily.jsonl` 按日 upsert。
- B5：缓存键去掉 `market_ctx`（命中率从 0% 恢复）；`calls/cached_hits/last_ok_ts/last_error_ts/model` 可诊断字段；`llm_rerank` 表 + `save_llm_rerank`；触发改「墙钟 600s + is_trading_time」；`llm_health` 合并 reranker + deepseek 健康度（替换原先 getattr(..., "client", None) 的空转实现）。
- 前端：决策条新增「相对基准超额」，LLM 状态行显示 calls / 缓存命中 / 最近错误。

实施期修正（相对本规格）：
1. `RerankResult` 缓存命中改为返回 `dataclasses.replace(..., cached=True)` 副本（原实现改写共享对象）。
2. `benchmark_daily.jsonl` 由「进程内首次 append」改为「每次刷新按日期 upsert + 原子替换」：解决重启重复写行、以及「早盘首次口径被永久钉住」。
3. 基准计算失败时保留上一份成功快照（不再把已算成结果抹成 unavailable）。
4. `benchmark.compute` 的「只用本地缓存」表述修正为「本地日线缓存 + 可能触发本地 xtdata 补拉（0.1s/只）」，故必须只在后台线程调用。

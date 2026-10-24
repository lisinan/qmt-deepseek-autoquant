# C1 上帝类拆分 —— 进度与路线图

> 2026-10-22 起。目标：把 `engine/event_engine.py` 的 `EventEngine`（原 4178 行 / 95 个方法）
> 按职责切成多个模块，**每一步都必须做到「行为逐位不变 + 609 例全绿」**。

## 0. 为什么只能小步切

`EventEngine` 是 41 轮自进化堆出来的：交易路径、状态持久化、行情新鲜度、观测快照、
板块/LLM、轮动全在一个 `self` 上。一次性重构等于赌整个资金路径，收益却是"代码好看"。
因此定下四条纪律：

1. **只搬不改**：搬移不得改任何业务逻辑、默认值、执行顺序。
2. **薄委托**：EventEngine 保留同名方法，方法体只有一行 `return self._x.method()`，
   外部（`web/routes.py`、`tests/`）零改动。
3. **owner 回调 + 动态解析**：新模块不缓存任何会被 monkeypatch 的模块级名字
   （`LOG_DIR`、`qmt_broker`、`is_trading_time`…），一律调用时经
   `engine.event_engine` 命名空间动态取，否则 `patch.object(EE, ...)` 类测试会静默失效。
4. **不碰源码守卫方法体**：下列 4 个方法的**方法体**被守卫测试用源码级检查钉住，只允许"加一行委托"，
   不允许把逻辑搬走：
   - `_restore_engine_state`（必须仍调用 `_reset_daily_if_needed`）
   - `_run_once`（必须仍含 `self.SECTOR_EVAL_EVERY_N_ROUNDS` 与 `portfolio_every_n`）
   - `__init__`、`_fetch_daily_tushare`

## 1. 已完成（2 步，共 −258 行）

| 步骤 | 切出内容 | 新模块 | 行数变化 | 验证 |
|---|---|---|---|---|
| C1-1 | **基准管道**：`_benchmark_snapshot/_benchmark_since/_compute_benchmark/_benchmark_daily_append` + 5 个状态字段；并删除死代码 `_daily_flatten_done`、`_llm_rerank_interval` | `engine/benchmark_feed.py`（182 行，`BenchmarkFeed`） | 4178 → 4105（−73） | 609/609 |
| C1-2 | **只读观测层**：`snapshot` / `_build_risk_snapshot` / `_gate_funnel_snapshot` / `_degraded_list` / `data_health` / `llm_health` / `market_staleness` / `latest_*` 共 15 个方法 | `engine/observability.py`（313 行，`Observability`） | 4105 → 3920（−185） | 609/609 |

实现要点（值得沿用）：

- `BenchmarkFeed(owner)`、`Observability(owner)` 均以 **owner 回调** 读引擎状态，自身不持有快照值；
- 基准日度落盘的 `log_dir` **由调用方每次传入**（`append_daily(snap, log_dir=LOG_DIR)`），
  因为 `tests/test_benchmark_and_rerank_b4b5.py` 会 monkeypatch `engine.event_engine.LOG_DIR`；
- `EventEngine._obs` 做成**懒加载 property**，而不是在 `__init__` 里赋值 —— 这样
  `EventEngine.__new__(EventEngine)` 的裸实例（`test_sector_heat`、`test_web_freshness`）仍能调用，
  且完全不动被守卫的 `__init__`。

## 2. 后续切片（按风险从低到高）

| 步骤 | 计划切出 | 目标模块 | 预估收益 | 风险 |
|---|---|---|---|---|
| C1-3 | 持久化**纯函数**部分：`_save_engine_state`、`_sanitize_restored_peaks`、`_credible_peak_equity`、`state_payload` 组装/解析 | `engine/state_store.py` | 约 −120 行 | 中（`_restore_engine_state` 主体须留在原处，只搬被它调用的 helper） |
| C1-4 | 行情新鲜度：`_update_market_staleness` 及三段豁免 → **显式状态机** | `engine/market_freshness.py` | 约 −90 行 | 中高（会改变陈旧判定语义，需单独一轮 + 专项测试） |
| C1-5 | 交易路径：`_handle_buy` / `_handle_sell` / `_reconcile_pending_orders` / `_apply_trade` | `engine/orders.py` | 约 −350 行 | 高（资金路径，必须最后做，且先补全对账/回放的确定性测试） |
| C1-6 | 板块与 LLM：`_evaluate_sectors` / `_fire_llm_rerank` / `ensure_recommendations` | `engine/sectors.py` | 约 −200 行 | 中（`_run_once` 内的调度行不能动） |

## 3. 每一步的验证配方（固定）

```powershell
$py = "C:\Users\lisinan\.conda\envs\qmt\python.exe"
$env:Path="C:\Users\lisinan\.conda\envs\qmt\Library\bin;C:\Users\lisinan\.conda\envs\qmt\DLLs;"+$env:Path
$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="."

# 1) 编译
& $py -m py_compile engine/event_engine.py engine/<新模块>.py

# 2) 全量（必须 609+/0）
& $py tests/run_all.py

# 3) 行数取证
(Get-Content engine/event_engine.py).Count
```

**验收标准**：全量测试 `FAILED: 0`，且 `event_engine.py` 行数单调下降；
任一守卫测试变红即视为该步失败，回滚该步（不改测试去迁就）。

## 4. 不建议做的事

- 不要为了"减少行数"把 `_run_once` 的调度逻辑搬走——它被源码守卫钉住，搬走等于骗过守卫；
- 不要在切分时顺便"改进"逻辑（例如把陈旧守卫改成状态机）——改进与搬移必须分两步提交，
  否则测试变红时无法判断是搬移出错还是逻辑改动出错。

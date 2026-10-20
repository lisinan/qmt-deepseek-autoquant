# 今日（2026-10-19）未开仓归因 + 行情守卫补强（R37 PM）

> 承接同日诊断（上午会话的 `reports/R37_AM_DERISK_CHANNELS_20261019.md`
> 与我的 `reports/WHY_NO_ENTRY_2026-10-16.md`）。
> **本轮生产参数零改动**：`config/settings.py` 的 `STRATEGY_PARAMS` / `RISK_PARAMS`
> 一个值都没动。改的全是工程层（告警准确性 + 测试卫生）。

---

## 一、今天为什么没开仓

**结论：与 10-13 以来同源 —— 三层入场闸门在当前弱市全部关闭。** 引擎全天正常
（27 次心跳、总资产恒 972,533.60、持仓 0、风控正常、券商重连 0、`halted=false`），
零成交，**连续第 10 个交易日零 BUY**。

### 今天日志的原话（27 条，每 10 分钟一条）

```
2026-10-19 09:15:30 [WARNING] 无入场信号：动量闸门后候选池为空
（min_mom=0.15 lookback=60 top_n=3 ⇒ 要求 60 日涨幅 > 15%）
| 漏斗分层: 宇宙 38 只, trend_up 0, bias>=2.00 0, score>=4.0且正因子>=3 1 只（最高分 688072.SH）
```

这行是 10-16 新加的漏斗诊断，一句话说清**四层全灭**：

| 层 | 判据 | 今日通过数 |
|---|---|---|
| 1 动量闸门 | 60 日涨幅 > +15% | **0/38** |
| 2 日线闸门 | `trend_up`（或 bias ≥ 2.00，后者不可达） | **0/38** |
| 3 评分阈值 | score ≥ 4.0 且正因子 ≥ 3 | **1/38**（688072.SH） |
| 4 宽度确认 | 同日 ≥ 2 只 BUY | 到不了这一层 |

### 今日真实数据（tushare 独立复核，末根 20261019）

```
688111.SH  60d= +3.74%   300857.SZ  60d= +1.88%   002415.SZ  60d= +0.12%
688361.SH  60d= -3.21%   300394.SZ  60d= -3.82%   ...  688347.SH  60d= -46.07%

60d > +0.00: 3/34    60d > +0.05: 0/34    60d > +0.15: 0/34    20d > +0.15: 0/34
```

**今天有一个新变化**：这是 10-13 以来第一次出现 3 只 60 日动量**转正**的标的
（688111.SH +3.74% / 300857.SZ +1.88% / 002415.SZ +0.12%）—— 最弱的票开始修复。
但最高才 +3.74%，离 `momentum_min_mom=0.15`（要求 +15%）差一个数量级，
所以动量闸门仍然全灭。

而且这再次印证上周的结论：**即使把动量闸门完全放开，今天仍然开不了仓** ——
日线闸门 `trend_up` 的四个分量里 `above_ma60` 通过数 **0/34** ⇒ `trend_up`
逻辑上恒为 False。688072.SH 拿到 4.50 分（唯一过评分线的），但它在日线闸门
之前就被挡下。

### 今天额外踩到的一次：行情源误报（本轮修复对象）

```
12:58:41 [ERROR] 行情源失效：38 只标的快照已陈旧 89 分钟，已暂停建仓
15:02:02 [ERROR] 行情源失效：24 只标的快照已陈旧 2 分钟，已暂停建仓
```

两条都是**误报**，暴露了 10-16 那轮只修了一半：

- **15:02:02（收盘后）** —— 15:00 竞价结束后最后一根行情必然开始变旧。
  我上周的宽限只覆盖「**进入**交易时段」那一刻，**没覆盖「离开」**。
- **12:58:41（午休边界，age=89 分钟）** —— 进段宽限 90s 到期时首根真实 tick
  仍未到达（参考点停在 11:30 上午收盘）。

**今天没有 09:27 那条** ⇒ 上周修的「开盘宽限」确实生效了。

---

## 二、本轮修复：行情陈旧守卫的两个补强

### 补强 1：age 正在下降 ⇒ 视为恢复中（治午休边界）

单纯把宽限从 90s 加到 600s 是**错的** —— 那等于允许开盘后 10 分钟用旧价建仓，
而 10-16 的 09:27 误报（age 仅 2 分钟）说明开盘恢复通常只要十几秒。

正确判据是**方向**而非时长：时段切换后只要 age 在**下降**，说明推送已经恢复、
正在追上来，此时无论 age 绝对值多大都不该报警。只有 age **停止下降**才说明真断了。

**安全性**：真断线时 age 只增不减（09-25 停更 47 小时即此形态）⇒
「age 下降」判据**永不成立** ⇒ 宽限到期后照常报警。

### 补强 2：已离开交易时段单独宽限（治收盘后）

新增 `MARKET_STALE_EXIT_GRACE_SEC = 300.0`：`_log_session_transition(False)`
（离开时段）时开启。收盘/午休初的必然陈旧静默。

**安全性**：同样有上限。过期后若 age 继续涨仍会报警 ——
防止「收盘后 miniQMT 整个挂掉」被永久静默。午休（11:30→12:57）本身就在时段外、
主循环休眠，不会跑 `_update_market_staleness` ⇒ 这个宽限实际只作用于**收盘后那几分钟**。

### 顺带清掉的历史脏数据

进段/离段时重置 `_last_market_max_age = -1.0`，避免下一段用错基准
（否则「下降」判据会拿收盘前的 age 当基准，第一轮就误判为「正在恢复」）。

---

## 三、顺带发现并修复：一个测试假失败的根因（`.pyc` 陈旧）

### 现象

今日基线跑出 **498/501**，其中一例是上周没有的：

```
tests.test_daily_pnl_crossday_reset.test_restore_path_invokes_cross_day_reset
AssertionError: 恢复路径不再调用 _reset_daily_if_needed，跨日复位将失效
```

而 `engine/event_engine.py:3183` **明明有这一行调用**。

### 定位过程（值得记录，因为是「幽灵失败」）

1. 单独跑该模块：6/6 通过
2. `import` 全部模块后跑：通过
3. 逐个执行全部用例后跑：通过
4. 在目标用例前后插 `inspect.getsource` 探针：**通过**
5. 给 `inspect.getsource` 装 hook 后跑完整 `run_all.main()`：**通过**（hook 本身扰动了他）
6. 监控 `EventEngine` 类的 id、模块命名空间、linecache 内容：全部正常

⇒ 每一步单独复现都通过，只有完整跑才失败。典型的**状态依赖**问题。

### 根因

`inspect.getsource()` 依赖 `linecache`，而 linecache 的首个条目来自 **`.pyc` 里
记录的源文件大小/mtime**。当 `__pycache__` 里留着旧版 `.pyc` 时
（今日 `engine/event_engine.py` 被上午会话与我先后改动，`.pyc` 滞后），
`co_firstlineno` 指向**旧文件**的行号，`getsource` 在**新文件**上按旧行号切块
⇒ 返回**另一个函数**的源码。

**实锤**：清掉 `__pycache__` 后重跑，498 → **499**（该例转绿）。

### 修复（两处）

1. **清掉 `__pycache__`**（即时恢复）
2. **把该守卫改成直接读磁盘源码**（`Path.read_text` + 正则切方法体），
   与 `.pyc`/行号彻底解耦。这也让它对「源码被改但 pyc 未更新」同样有效。

> 这类「结构性源码守卫」（`inspect.getsource` 检查某方法是否含某调用）
> 在本项目里有 6 处（`test_daily_pnl_crossday_reset` / `test_batch_b_minute` /
> `test_daily_stale_fallback` / `test_sector_heat` / `test_web_freshness` /
> `test_batch_e_daily_decision`）。本轮只改了**今天实际失败**的那一处；
> 其余 5 处同型脆弱，建议后续统一改成读磁盘（未改，避免扩大本轮范围）。

> ⚠ 另注：今日工作区有多个会话并行改动（上午的 AM-EVOLVE R37、下午的
> PM-EVOLVE 第 38 轮，以及本轮）。`tests/test_batch_b_minute.py` 的确定性重写
> 来自第 38 轮，非本轮。

---

## 七、追加：加固其余 5 处 `inspect.getsource` 结构守卫（本轮收尾）

### 背景

第三节定位到「`.pyc` 陈旧 ⇒ `inspect.getsource` 返回错位函数源码」这一
**幽灵失败**模式后，本项目里还有 5 处同型守卫在用 `inspect.getsource`：

| 文件 | 守卫对象 |
|---|---|
| `tests/test_batch_b_minute.py` | `backtest_minute` 模块 + `run_minute_backtest` |
| `tests/test_daily_pnl_crossday_reset.py` | `backtest_daily` 模块 |
| `tests/test_daily_stale_fallback.py` | `DailyContext._fetch_daily_tushare` |
| `tests/test_sector_heat.py` | `EventEngine._run_once` |
| `tests/test_web_freshness.py` | `EventEngine.__init__` |

只要 `.pyc` 一滞后，它们就会**随机红**——而本仓库今日实测有 3 个会话并行改动，
`.pyc` 滞后是常态而非例外。

### 做法

新增 `tests/source_guard.py`（共享 helper，磁盘直读 + 按 `def 名(` 切块），
把 6 处守卫全部切过去：

- `method_source(rel_path, name)` —— 按缩进切方法体
- `module_source(rel_path)` —— 整文件
- `fn_source(fn)` —— 模块级函数（只用 `__code__.co_filename` 定位文件，
  不经过 linecache 的源码缓存）

### 防「helper 自身退化」的 4 例自守卫

| 门锁 | 钉住什么 |
|---|---|
| `test_source_guard_helper_extracts_known_methods` | 4 个真实方法都能取到且含关键串 |
| `test_source_guard_is_pyc_independent` | 往 linecache 注入假缓存后返回值**不变** |
| `test_source_guard_returns_none_for_missing_symbol` | 找不到符号返回 None，不抛不错块 |
| `test_source_guard_fn_source_resolves_module_level_function` | 模块级函数能解析（首版实现的正则 bug 回归保护） |

**反向验证**（证明守卫不是空转）：在内存里把 4 个关键调用/赋值删掉，
用同一套正则重切，4/4 全部被检出。

### 踩坑记录（首版 `fn_source` 的 bug）

首版用 `__qualname__` 逐级 `getattr` 解析，对模块级函数（qualname 不含 `"."`）
走了错误分支 ⇒ **恒返回 None** ⇒ 使我刚加固的那个测试假失败。
已改用「只取 `__code__.co_filename` 定位文件 + `__name__` 切块」，
并补门锁 23 钉死该场景。

### 本轮后测试基线

| | R37 PM 完成后 | 本轮加固后 |
|---|---|---|
| PASSED | 512 | **516** |
| FAILED | 1 | 1（flask 未装，环境问题） |
| TOTAL | 513 | 517 |

⇒ **`.pyc` 幽灵失败模式已从本项目消除**（6 处守卫 + 4 例自守卫 + 反向验证）。

---

## 四、测试基线

| | 改动前 | 清 pycache 后 | 本轮完成后 |
|---|---|---|---|
| PASSED | 498 | 499 | **504** |
| FAILED | 3 | 2 | **2** |
| TOTAL | 501 | 501 | 506（+5 新守卫） |

**剩余 2 例失败均与本轮无关**：

1. `test_batch_d_fixes.test_web_storage_is_singleton` —— conda qmt env 未装 flask
   （`ModuleNotFoundError: No module named 'flask'`），环境问题。
2. `test_batch_b_minute.test_minute_backtest_t1_restriction_defers_exit_to_next_day`
   —— 上午 R37 轮已记录「改前改后均失败，与 R37 无关」，本轮同样未触碰。

---

## 五、落盘清单

| 文件 | 改动 | 生效时点 |
|---|---|---|
| `engine/event_engine.py` | `MARKET_STALE_EXIT_GRACE_SEC=300` / `MARKET_STALE_RECOVER_ROUNDS=6` / `_in_exit_grace()` / `_market_age_recovering()` / `_update_market_staleness` 两条新分支 / `_log_session_transition` 进出段都重置 age 历史 | 需重启 |
| `tests/test_observability_fixes_20261016.py` | 新增 5 例守卫（门锁 15~19） | 立即 |
| `tests/test_daily_pnl_crossday_reset.py` | 源码守卫改为读磁盘（修 `.pyc` 假失败） | 立即 |
| `reports/WHY_NO_ENTRY_2026-10-19.md` | 本报告 | 立即 |
| `tests/source_guard.py` | **新增**：源码守卫共享 helper（磁盘直读 + 切块） | 立即 |
| `tests/test_batch_b_minute.py` / `test_daily_stale_fallback.py` / `test_sector_heat.py` / `test_web_freshness.py` | 4 处 `inspect.getsource` 守卫改用 helper | 立即 |
| `tests/test_observability_fixes_20261016.py` | 追加 4 例 helper 自守卫（门锁 20~23） | 立即 |

`config/settings.py` **零改动**。

---

## 六、下一步

今日归因本身**没有新的可行动项** —— 它再次确认了 `reports/OPEN_DECISIONS.md`
D1 的判断：KPI#4 是口径问题，不是参数问题（第 11 条独立证据）。
唯一新增的观察是**宇宙最弱的票开始修复**（3 只 60 日动量转正），
若该趋势延续，动量闸门可能在数周内重新有候选 —— 但**日线闸门
（`above_ma60` 0/34）才是更紧的那一层**，需要它先松。

# 工程缺陷修复报告（2026-10-16）

> 承接同日诊断 `reports/WHY_NO_ENTRY_2026-10-16.md`。
> **本轮零策略参数改动**：`config/settings.py` 的 `STRATEGY_PARAMS` / `RISK_PARAMS`
> 一个值都没动（守卫测试 `test_production_min_daily_bias_unchanged_and_unreachable`
> 把 `min_daily_bias == 2.0` 钉死）。改的全部是**工程层**：可观测性、度量口径、
> 告警准确性、测试卫生。
>
> 测试：**461/478 → 492/493**（+15 新守卫，16 例假失败归零）。
> 唯一剩余失败 `test_web_storage_is_singleton` 是 conda qmt env 未装 flask
> 的**环境问题**，与本轮改动无关（改动前同样失败）。

---

## 修复清单

| # | 缺陷 | 严重度 | 改动 | 守卫 |
|---|---|---|---|---|
| 1 | sqlite 连接泄漏 | **P0** | `strategy/record_ledger.py` | 3 例 |
| 2 | `disconnect_n` 语义误判 | **P1** | `strategy/review_daily.py` | 3 例 |
| 3 | 行情陈旧守卫时段边界误报 | **P1** | `engine/event_engine.py` | 4 例 |
| 4 | 无信号日志只报漏斗第 1 层 | P2 | `engine/event_engine.py` | 2 例 |
| 5 | `min_daily_bias` 不可达无播报 | P2 | `engine/event_engine.py` | 3 例 |

新增文件：`tests/test_observability_fixes_20261016.py`（15 例）、本报告。

---

## 1. sqlite 连接泄漏（P0 · 测试卫生）

### 缺陷

`strategy/record_ledger.py::_conn()` 返回裸 `sqlite3.connect(...)`，而所有调用方
写成 `with _conn(path) as con:`。

**`sqlite3.Connection` 的 `with` 协议只管事务（commit/rollback），从不 `close()`
连接。** 于是每个 helper 调用泄漏一个文件句柄 + WAL shm 映射，直到进程退出。

### 实盘铁证（我先写复现脚本证实，再动手）

```
$ python _tmp_leak_repro.py
eod_asset = 974013.6        # 逻辑正确
rmtree FAILED（连接泄漏实锤）: [WinError 32] 另一个程序正在使用此文件
```

`tests/run_all.py` 基线 **461/478**，16 例失败中 15 例全是这一条
（`PermissionError: [WinError 32]` 删不掉临时库），外加 1 例 flask 缺失。
⇒ **账本逻辑完全正确，但测试全红** —— 这是最坏的组合：下次任何改动都会
被这 16 条假失败掩盖真实回归。

生产侧同样受影响：OBSERVE 每次跑 `--check-gap` 都多留一个句柄，
与 `storage/qmt.db-wal` 长期不归零直接相关。

### 修复

包一层 `_ClosingConn`：进入时建连、退出时必 `close()`，其余属性/方法原样转发
⇒ **调用方 `with _conn(...)` 的写法一行都不用改**（接口不变）。

```python
def _conn(path=None):
    con = sqlite3.connect(str(path or DB_PATH))
    con.row_factory = sqlite3.Row
    return _ClosingConn(con)      # __exit__ 里真正 close()
```

### 验证

- 复现脚本 `rmtree OK（无泄漏）`
- `test_conn_context_manager_really_closes`：退出 with 后临时目录可删
- `test_repeated_calls_do_not_leak_handles`：连续 100 次调用不累积泄漏
- `test_ledger_helpers_still_work_after_close_fix`：**零行为变化**
  （`eod_asset_raw / eod_asset / snapshot_days / day_fills_n /
  stale_mark_suspect_day` 返回值逐一比对）

---

## 2. `disconnect_n` 语义误判（P1 · 度量层）

### 缺陷

`review_daily` 用纯关键词匹配统计断线次数：

```python
if ("disconnect" in mlow or "断开" in msg or "重连" in msg ...):
    disconnect_n += 1
```

而 **paper 模式每次启动都播报一条 SYSTEM 级横幅**：

```
引擎已启动 | 策略=single 执行=paper 数据源=xtdata 券商=disconnected | 订阅标的=38
```

`"disconnected"` 在这里是**常态状态描述**（paper 本就不连券商），不是断线事件。

### 实盘铁证

`logs/review_2026-10-16.json`：

```json
"券商连接断开/重连 28 次", "evidence": "notices 含 disconnected/重连关键词 28 次"
```

而当日**真实断线为 0 次**（那 28 条几乎全是历次启动横幅 + 心跳里的
「券商重连=0」字样）。后果：

- `stability` 被扣 `min(25, 28×8) = 25` 分（**满扣**）；
- 产出一条 P1 发现，`action` 指向 `core/broker.py` 的自修复 ⇒ 把人引向
  一个根本不存在的 broker 缺陷；
- 连续多个交易日同型误报 ⇒ 稳定分长期钉在低位，**且让人对真断线脱敏**。

### 修复

三条同时满足才计为断线：① 关键词命中 ② **WARNING/ERROR 级**（真断线一定告警，
状态横幅是 SYSTEM）③ 不是启动横幅/心跳。

```python
if ((...关键词...)
        and lvl in ("WARNING", "WARN", "ERROR", "ERR")
        and "引擎已启动" not in msg
        and "心跳" not in msg):
    disconnect_n += 1
```

同步更新发现的 evidence 文案，说明已剔除例行播报。

### 验证

- `test_disconnect_count_ignores_paper_startup_banner`：启动横幅 + 心跳 ⇒ 0
- `test_disconnect_count_still_counts_real_warning_disconnect`：
  两条真 WARNING 断线播报 ⇒ 2（**防过度修正退化成死代码**）

---

## 3. 行情陈旧守卫在时段边界误报（P1 · 告警层）

### 缺陷

10-15 与 10-16 各 3 次 ERROR「行情源失效…请检查 miniQMT 行情连接」：

| 时刻 | 陈旧时长 | 实际原因 |
|---|---|---|
| 09:27:01 | 2 分钟 | 引擎 09:15:46 进段并订阅，推送预热中；age 参考点停在**上一交易日收盘**（隔夜 17.5h） |
| 12:57:10 | 87 分钟 | 午休结束立刻进段（`DEFAULT_SESSIONS` 第二段 12:57 起），推送重连中 |
| 15:02:01 | 2 分钟 | 已过收盘 15:00/15:05，最后一根行情必然开始变旧 |

三个时刻**全部是交易时段刚切换的那一刻**，age 的参考点本身正常，只是新行情
还没到。这与 R9 守卫要防的「用 N 小时前冻结价下单」不是一回事。

### 后果

ERROR 进当日 notices ⇒ `err_n` 上升、稳定分被扣。**更糟的是脱敏**：
连续多日稳定的 3 条 ERROR 会让真正的行情断线（如 09-25 那种停更 47 小时）
不再引人注意 —— 而 R9 守卫的全部价值就在于「真断线时必须有人注意到」。

### 修复

新增 `MARKET_STALE_SESSION_GRACE_SEC = 90.0`：进入交易时段时开启宽限，
宽限内**不置位** `_market_stale`（不告警、不阻断建仓），但**仍照常记录**
`_market_stale_codes` / `max_age`（页面新鲜度徽标继续可用）。

```python
if self._market_stale and self._in_session_grace():
    self._market_stale = False
```

宽限只在「非交易时段 → 交易时段」切换点由 `_log_session_transition` 开启。

**为什么不影响真失效判定**：宽限 90s ≪ 失效阈值 120s；且宽限内 age 仍在增长，
推送一旦恢复 age 归零、判定立即恢复正常。宽限期一过若推送仍未恢复，age 只会
更大 ⇒ 判定照常触发。宽限只是「等首根真实 tick 到达」，不是「允许行情停更更久」。

### 验证

- `test_session_grace_suppresses_boundary_false_alarm`：宽限内 10/10 陈旧 ⇒ 不判失效
- `test_stale_still_detected_once_grace_expires`：宽限过后同样数据 ⇒ **照常判失效**
- `test_grace_never_blocks_fresh_market`：新鲜行情无论宽限与否都绝不判失效
- `test_grace_window_is_short_and_below_threshold`：`0 < 90 < 120`
  （防有人把宽限改成大于阈值，那等于偷偷抬高阈值）

---

## 4. 无信号日志只报漏斗第 1 层（P2 · 可观测性）

### 缺陷

原日志在 `n_cand == 0` 时只说：

```
无入场信号：动量闸门后候选池为空（当前宇宙无 60 日正动量标的）
```

两个问题：

1. **文案谎报**：「无 60 日正动量标的」在 `momentum_min_mom == 0` 时代准确；
   R28（10-13）把入池下限抬到 **0.15** 后，真实约束是「无 60 日涨幅
   **> +15%** 的标的」。下次若出现 −1%~0% 的名字，文案会继续误导。
2. **只报第 1 层**：10-16 复算显示日线闸门 `trend_up` 通过数 **0/34**
   才是真正的锁（`above_ma60` 0/34 ⇒ `trend_up` 逻辑上恒 False）。
   ⇒ 照字面意思把 `momentum_min_mom` 调回 0.0，**BUY 仍是 0**，
   但日志让人以为那样就行。下一次排查仍要从零复算一遍才能发现。


### 修复

新增 `_entry_funnel_stats()`，逐层统计全宇宙（只读 DailyContext 缓存，不拉网络，
每 10 分钟最多执行一次）：

```
无入场信号：动量闸门后候选池为空（min_mom=0.15 lookback=60 top_n=3
  ⇒ 要求 60 日涨幅 > 15%）| 漏斗分层: 宇宙 34 只, trend_up 0,
  bias>=2.00 0, score>=4.0且正因子>=3 3 只（最高分 002415.SZ）
```

一行就能看出「该动哪个参数」（或「动参数也没用」）。

### 验证

- `test_funnel_stats_reports_every_layer`：各层通过数逐一断言
- `test_no_signal_log_mentions_actual_min_mom`：
  告警必须含 `min_mom` 实参 + 「漏斗分层」，旧谎报文案不得再出现在该方法里

---

## 5. `min_daily_bias=2.0` 不可达无播报（P2 · 可发现性）

### 缺陷

`bias` 由 `daily_context._compute` 算得并被夹在 **[-1, +1]**：

```python
bias = 0.35*(±1) + 0.35*(±1) + 0.30*(±1)
bias = max(-1.0, min(1.0, bias))
```

而 `STRATEGY_PARAMS["min_daily_bias"] = 2.0` ⇒ `bias >= 2.0` **永为 False**，
入场闸门 `trend_up or bias >= min_daily_bias`（`trend_strategy.py:90-96`）
退化为**只认 `trend_up` 一个条件**。

这是 R22 有意的「关闭」取值（四窗共识 dSh 全负，见 settings 注释），**不是 bug**。
但有两个可发现性问题：

1. 「关闭态」与「工作态」共用同一个字段，且**关闭态取值落在值域外**。
   若有人把 2.0 改成 0.5 以为「略微放宽」，实际会从**永久关闭**跳到一个
   真实生效的阈值 —— 一次「看起来无害」的调整就改写了策略行为。
2. 读代码 / 读日志的人都无法一眼看出「这一项目前完全不生效」。

### 修复

**刻意不改生产值**（改它 = 改策略，须走自进化四窗共识；既有守卫
`test_on_daily_features_prod_gate_closes_bias_channel` 也钉着 2.0）。
只补启动播报，把「静默的死通道」变成「可见的死通道」：

```
引擎启动 → [SYSTEM][策略] 入场日线闸门：bias 通道当前不可达
（min_daily_bias=2.00 超出 bias 值域 [-1,1]）⇒ 等价于仅 trend_up 单通道生效。
这是有意的「关闭」取值（R22 四窗共识 dSh 全负），
如需重新启用请走自进化共识，勿单手改小。
```

### 验证

- `test_production_min_daily_bias_unchanged_and_unreachable`：
  值仍是 2.0 **且** 确实 > 1.0（播报的「不可达」结论为真，不是又一处谎报）
- `test_bias_channel_is_unreachable_in_practice`：
  bias ∈ {-1,-0.5,0,0.5,1.0} 全部被 `daily-gate` 拒
- `test_engine_broadcasts_unreachable_bias_channel`：启动播报确实存在

---

## 测试基线对比

| | 改动前 | 改动后 |
|---|---|---|
| PASSED | 461 | **492** |
| FAILED | 17 | **1**（flask 缺失，环境问题，改动前同样失败） |
| TOTAL | 478 | 493（+15 新守卫） |

16 例假失败的构成：连接泄漏 15 例 + flask 1 例。修复后连接泄漏 15 例全部转绿。

## 生效时点

全部为 `.py` 改动 ⇒ **需重启引擎生效**。当前引擎（PID 20336，10-16 07:53:36 启动）
早于本次改动 ⇒ 尚未加载。因全部是工程层修复（不触碰任何策略参数），
**无行为差异、无需紧急重启**；下次例行重启时自动加载。

## 遗留（非本轮范围）

- `engine/event_engine.py` 仍是 3410+ 行、`strategy/backtest_daily.py` 142KB、
  `reports/EVOLUTION_DECISIONS.md` 6812 行 —— 规模治理见 `WHY_NO_ENTRY_2026-10-16.md` §「典型问题」第 6 条。
- `tests/test_batch_d_fixes.py::test_web_storage_is_singleton` 需在 conda qmt env
  装 flask 才能跑（`pip install flask`），属环境配置而非代码问题。

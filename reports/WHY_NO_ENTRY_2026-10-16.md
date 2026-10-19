# 为什么没开仓 —— 诊断报告（2026-10-16）

> 只读分析，未改动任何生产代码/参数。所有结论都附复算证据。

## 一句话结论

**不是 bug，是「三层入场闸门在当前弱市全部关闭」的确定性结果**：动量闸门
把 34 只候选清零（`momentum_min_mom=0.15` 要求 60 日涨幅 > +15%，全宇宙
0 只满足）；即使动量闸门完全放开，日线闸门 `trend_up` 也是 **0/34**；
`min_passing_candidates=2` 的宽度闸门再兜一层。连续 9 个交易日零 BUY、
100% 现金（972,533.60），与 `reports/EVOLUTION_DECISIONS.md` R34/R35 的
记录一致。

## 证据链（我实际复算过，不是读日志猜的）

### 1) 日志侧：全日 14 次同一条 WARNING

`logs/quant_system.log`（10-16，09:15→14:57 每 10 分钟一条，节流所致）：

```
无入场信号：动量闸门后候选池为空（当前宇宙无 60 日正动量标的）
```

来源：`engine/event_engine.py:2351-2353`（`_maybe_log_no_signal`，
`n_cand == 0` 分支）。当日 `fills=0 / orders=0 / signals=0`，
`positions=0 / cash=972,533.60 / market_value=0`（`logs/review_2026-10-16.json`）。

### 2) 数据侧：动量闸门确实把宇宙清零

实盘候选 = 静态 12 只（`STOCK_CODES`）+ 动态活跃池 30 只
（`storage/dynamic_universe.json` 的 `active_pool`，10-16 07:54 刷新）
− 指数 = **34 只**。用 tushare 真实日线复算 60 日动量：

- **60 日动量 > 0 的：0/34 只**（最高 002415.SZ = −1.35%，688111.SH = −2.81%）
- **> +15%（`momentum_min_mom=0.15`）的：0/34 只**
- 20 日动量 > +15% 的：0/34 只

⇒ `top_momentum()`（`strategy/daily_context.py:577-591`）先按
`m > min_mom` 过滤，一只都不剩，`top & candidate_codes` = 空集。
**这就是「无 60 日正动量标的」的实况，不是数据缺陷**（引擎本地 xtdata
日线与 tushare 口径一致，我两边都算过：34/34 覆盖、`trend_up` 均为 False）。

### 3) 关键：即使放开动量闸门，也仍然开不了仓

这是本次分析最重要的发现——**动量闸门只是第一道，真正锁死的是日线闸门**。
我用引擎同一条代码路径（`DailyContext._compute`）复算 34 只的分量：

| 分量 | 通过数 |
|---|---|
| `above_ma20` | 4/34 |
| `above_ma60` | **0/34** |
| `ma20_slope > 0` | 2/34 |
| `macd_hist >= 0` | 4/34 |
| `trend_up`（四者 AND） | **0/34** |

`trend_up` 定义在 `strategy/daily_context.py:333-334`，四个条件同时为真才
为 True；当前宇宙 **above_ma60 通过数为 0** ⇒ 逻辑上 `trend_up` 恒为 False。

而入场闸门是 `trend_up or bias >= min_daily_bias`
（`strategy/trend_strategy.py:90-96`），`min_daily_bias=2.0` 而 `bias ∈ [-1,1]`
**永远不可达** ⇒ 该通道等价于关闭（`config/settings.py:231` 注释明确写了
「2.0=关闭，与回测口径对齐」）。

⇒ **无论 `momentum_min_mom` 取 0.15 / 0.0 / −0.5，BUY 信号都是 0 只**
（我把 min_mom 从 +0.15 扫到 −0.50、top_n 3/5/8 全组合跑过，池最多放到
34 只，BUY 仍为 0）。这也解释了 R31 的实测「q=95/97 ≈ 惰性、q=99 ≡ 关闭」。

评分阈值这一层甚至没被触及：有 3 只 `score >= 4`（002415.SZ 5.82 /
688120.SH 5.59 / 688072.SH 4.50），但都在 `trend_up` 之前就被挡下。
宽度闸门 `min_passing_candidates=2`（`config/settings.py:423`）要求同日
≥2 只 BUY，单独一只也不开——这是 R21 有意为之的弱市防御。

## 项目里的典型问题（按严重度）

### ★★★ 1. 「回测有效、实盘无效」的参数死锁：KPI#4 数学上不可能达标

`momentum_min_mom=0.15` 是 R28 基于「25 只 AI 宇宙 × 911 根日线」的四窗
共识落盘的（+0.115/+0.167/+0.239/+0.408，最差窗过 +0.10 闸门）。但该参数
在**当前实盘宇宙**上的效果是：34 只候选全部出局，**连续 9 个交易日零 BUY**
⇒ 收益恒 0 ⇒ KPI#4（live 近 4 周滚动收益 > 0%）**不可能达标**。
EVOLUTION_DECISIONS.md 已连续 9 次把这条推给 OWNER 裁决（R31 定价：
恢复弱市暴露 = OOS 折均值 Sharpe −0.11~−0.22，最差窗 −0.40）。
**这是治理问题，不是代码 bug**：自进化闭环以「最差窗 dSh ≥ +0.10」为闸门，
系统性否决一切提高暴露的改动，而当前唯一未达 KPI 恰恰只能靠提高暴露。

### ★★★ 2. `min_daily_bias=2.0` 是一个「不可达的关闭开关」

`bias` 由 `daily_context.py:324-331` 算得，范围被 `max(-1, min(1, ...))`
夹在 **[−1, +1]**；而 `min_daily_bias=2.0`。于是
`bias >= min_daily_bias` **永为 False**，日线闸门实际退化为
**只认 `trend_up` 一个条件**。注释说这是「与回测口径对齐、2.0=关闭」，
但代价是：日线闸门从「两通道 OR」变成「单通道」，且该单通道当前 0/34 通过。
更值得警惕的是**可发现性**：如果哪天有人把 `min_daily_bias` 设成 0.5 以为
「略微放宽」，实际会从「永久关闭」跳到一个真实生效的阈值——这种
「关闭态与工作态共用同一个字段、且关闭态取值在值域外」的设计容易误操作。
建议：改用一个显式布尔/哨兵（如 `None` 或 `-inf`）表示关闭，并加一条
断言 `min_daily_bias <= 1.0 or min_daily_bias is None`。

### ★★ 3. 五层闸门串联，任何一层都是单点否决

入场漏斗（我逐层复算的当前值）：

| 层 | 位置 | 参数 | 当前通过 |
|---|---|---|---|
| 1 动量 | `event_engine.py:2274` → `daily_context.py:588` | `momentum_rank/min_mom/top_n` | **0/34** |
| 2 日线 | `trend_strategy.py:90-96` | `trend_up` / `min_daily_bias` | **0/34** |
| 3 评分 | `trend_strategy.py:104` | `buy_score_threshold=4.0` / `min_signals=3` | 未触及（3 只 ≥4） |
| 4 宽度 | `event_engine.py:2326` | `min_passing_candidates=2` | — |
| 5 行情新鲜度 | `event_engine.py:2546` | `MARKET_STALE_SEC=120s` / `RATIO=0.6` | 当日 3 次触发 |

第 5 层是**今天额外踩到的一次**：09:27 与 12:57、15:02 三次
「行情源失效：27/38 只快照已陈旧 2~87 分钟，已暂停建仓」
（`logs/review_2026-10-16.json` 的 notices 同记录载）。即便前 4 层放行，
`_handle_buy` 也会因 `_market_stale=True` 直接拒绝下单。
**这是 miniQMT 推送在开盘/午盘初段延迟的系统性问题**（10-15 同日同样出现），
不是策略问题，但会让「本来能开的仓」在关键几分钟内开不进去。

### ★★ 4. 可观测性误导：告警文案与真实绑定约束不符

日志说「当前宇宙**无 60 日正动量**标的」，这句字面意思（`momentum_min_mom=0`）
在 10-13 之前是准确的；但 R28 之后真实约束是「无 60 日涨幅 **> +15%** 的标的」。
今天的数据（60d>0 的就已经是 0 只）让两句话恰好等价，但**下次若出现
−1%~0% 的标的，文案会继续谎报**。同理，`_maybe_log_no_signal` 只在
`n_cand==0` 时打 WARNING；如果是 `n_cand>0` 但被日线闸门挡下，只会打
「候选 N 只、最高分 X」（且受 10 分钟节流）——**今天这道闸门其实是双重的，
日志只报了第一道**。建议：把 `min_mom` 的实际值与日线闸门通过数一起打进
这条 WARNING（例如「动量闸门后 0 只（min_mom=0.15）；日线闸门 trend_up
0/34」），否则下一次排查仍要像今天这样从零复算。

### ★★ 5. 「热读档」与「缓存档」混用，改动生效时点不一致

`momentum_min_mom` / `min_passing_candidates` / `conviction_*` 是热读
（改完下一轮生效），而 `trend_vol_sizing` / `risk_per_trade` /
`min_daily_bias`（`TrendStrategy.__init__` 的 `self.p = dict(STRATEGY_PARAMS)`
快照，`trend_strategy.py:53`）**必须重启**。`config/settings.py` 里每个
参数都写了「需重启」或「热读」，但**没有一个集中的生效时点清单**，
盘前检查（`reports/premarket_2026-10-16.md` §六）只能靠比对文件 mtime
与引擎启动时刻来人工推断。建议：给 STRATEGY_PARAMS 的每个键加一个
`_hot: bool` 元数据，启动时打印一份「待重启生效项」清单。

### ★ 6. 规模性问题（会持续消耗排查精力）

- `engine/event_engine.py` **188 KB / 3410 行**，`strategy/backtest_daily.py`
  142 KB，`reports/EVOLUTION_DECISIONS.md` **6812 行**——诊断结论散落在
  一个 68k 行的 markdown 里，第 9 次重复同一个 OWNER 裁决请求。
  建议把「KPI 口径 / 待 OWNER 裁决项」抽成单独的 `reports/OPEN_DECISIONS.md`，
  每轮只追加一行引用。
- `scripts/` 与 `strategy/` 下有 **60+ 个 `_pm_*` / `_am_*` / `_opt_*` 一次性
  脚本**（今天 `_tmp_*` 我又加了 6 个，已清理）。它们大多 monkeypatch
  `W.load_daily` / `W.base_cfg`，是「自动化 shell 内 xtdata 不可达」的
  历史产物。建议：把 tushare 面板注入抽成一个公共 helper
  （`scripts/_panel.py`），其余脚本只留参数。
- LLM 依赖：10-15 全天 DeepSeek 503 三十余次（`ai/deepseek_client.py`），
  reranker 只能空转；今天恢复。重排序不影响入场闸门（纯观察层），
  但 AI 观察层的「macro=bullish」与实盘「0  BUY」并列出现时，容易让人
  误以为 AI 层在驱动交易——它并不。

## 如果要让策略重新开仓，可选路径（代价都已定价）

| 路径 | 改动 | 已定价的代价 |
|---|---|---|
| A 放宽动量下限 | `momentum_min_mom` 0.15 → 0.05~0.10 | 四窗最差 dSh −0.002~+0.060（R28 数据），**且仍被日线闸门挡死** |
| B 关动量闸门 | `momentum_rank=False` | **无效**（日线闸门 trend_up 0/34，我实测） |
| C 放宽日线闸门 | `min_daily_bias` 2.0 → 0.2 | R22 实测四窗 dSh 全负（−0.298/−0.197/−0.238/−0.117） |
| D 放宽宽度闸门 | `min_passing_candidates` 2 → 1 | R21 实测 K=1 即基线，Sharpe 1.94→1.79 |
| E 恢复轮动/观察篮 | `enable_rotation=True` 或 `manual_entry_codes=[...]` | R15/R17 实测 Sharpe 1.71→1.02 / +0.186 改善来自关闭 |
| F 改 KPI 口径 | 改考核「相对宇宙 buy&hold 超额 > 0」 | R23 实测同期跑赢宇宙 7.1~15.9pp（治理动作，非参数） |

⇒ 结构性结论与 EVOLUTION_DECISIONS.md 第 9 次提的一致：**没有「既保
Sharpe 又让 KPI#4 转正」的参数档**。今天不动任何参数。

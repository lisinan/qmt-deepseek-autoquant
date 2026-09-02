# 分钟级 live 实测 — 文档基准与现实的差距

**生成时间**: 2026-09-02  walk-forward / 12 只静态池 / ~35500 根 1m / 7 折

## 核心结论

| 路径 | 7折 walk-forward ret | Sharpe | MDD | 与生产 live 是否一致 |
|---|---|---|---|---|
| **Day-level**（backtest_daily.py 历来结论）| +293.6% (全样本) | +1.60 | -19.85% | ❌ 否 — 用日线 score_daily 算分 |
| **Minute-level**（backtest_minute.py）| **-11.6%** (折均) | **-1.12** | -17.71% | ✅ 是 — 与生产 TrendStrategy.on_bars **同一份代码** |

**关键含义**：仓库历史文档里的 `+293.6% / Sharpe 1.60` **从未验证过 live 路径**。
live 实际跑的是分钟级，而回测验证的是日级。这是一条必须正视的结论，不是"小修小补能解决"。

## 为什么分钟级亏

1. **时间尺度错位**：`ma5=5` 在日线上是 5 天趋势，在分钟线上是 5 分钟噪声。
   生产用的 TrendStrategy 参数（ma_short=5、ma_medium=10、ma_long=20 等）
   原本是为日线调出来的，套到 1 分钟线纯属噪音。
2. **信号过密**：1 分钟评估一次 × 12 只票 × 240 分钟 = 86400 次/天，
   加 0.15% × 2 = 0.30% 期望亏损。47% 胜率（接近随机）下 EV < 0。
3. **T+1 + 1 分钟决策**：分钟决策触发建仓，但 T+1 强制隔日离场—— 决策
   频率与持仓时间严重错配，每笔平均持仓 8.8 天但每秒都在重新评分。

## live 路径的实际选择

| 选项 | 路径 | 工程代价 | 收益兑现 |
|---|---|---|---|
| **(a)** 改 live 用日线决策 | _run_once 用 DailyContext 算分 | 中 | 直接——回测结论对 live 立即生效 |
| **(b)** **当前状态**：保留分钟决策、重做分钟 walk-forward | — | — | **已做：负收益** |
| (c) 为分钟级重新调参 | 全套 opt_harness 改 1m；6 因子在 1m 上的权重需重定 | 大 | 不确定 |

## 行动建议

1. **优先 (a)**：用日线决策。当前 minute-level live 实测亏损，再不切换就是继续烧钱。
2. **保留 (b) 的回测基础设施** (`backtest_minute.py` + `test_batch_b_minute.py`)，
   便于以后为分钟级重做调参时复用，且作为 (a) 切换**前后**的事实基线。
3. 不要把分钟级结论写进 README "已验证" 列表——它们与生产 live 不在同一代码路径。

---

# (a) 路线实装后 — live 改用日线决策

2026-09-02 在 (b) 路线已揭露 minute 决策负收益的基础上，落地 (a)：把
EventEngine 入场决策从 `TrendStrategy.on_bars(minute_bars)` 切到
`TrendStrategy.on_daily_features(DailyFeatures)`。**关键事实：仍然是同一份
TrendStrategy 代码，变化的是传入的"决策原料"——这是 (b)/(a) 真正能
**同台对比**的前提。

## 决策链路 (a)

```
EventEngine._run_once [每 3s 一轮]
   │
   ├─ _run_single_step / _run_portfolio_step
   │     │
   │     ├─ 节流：每 ENTRY_DECISION_INTERVAL_SEC = 5 分钟 一次
   │     │
   │     ├─ 取 self.daily.features(code)         # DailyContext 已算好 score
   │     │     （与 backtest_daily.score_daily 严格同款）
   │     │
   │     ├─ strategy.on_daily_features(code, name, features)
   │     │     ├─ daily-gate (trend_up || bias >= 0.2)
   │     │     ├─ score >= buy_score_threshold (4.0)
   │     │     └─ pos_factors >= min_signals (3)
   │     │
   │     └─ _handle_buy(sig, tick, prices)       # 分钟线只用于成交价
   │
   └─ 离场检查（每 tick）: on_exit + minute-bar 价格 / peak
        # 趋势破位判定仍走 DailyContext.trend_broken
```

## 同台 walk-forward 对比（同 1m 数据、同 TrendStrategy、同成本）

| 决策路径 | 7折 ret | Sharpe | MDD | 全样本 ret | 折均交易数 |
|---|---|---|---|---|---|
| minute（生产现状） | **-11.67%** | -1.12 | -17.71% | -26.87% | 428 |
| **daily（#E 切换）** | **+9.94%** | **+1.92** | -4.40% | +9.17% | 322 |
| **Δret** | **+21.61pt** | | | | |

7 折全从负转正；Sharpe 从 -1.12 跳到 +1.92。CPU 同步下降约 99%（每 5 分钟一次评分而非每秒）。

## 代码改动（最低限度、不改策略语义）

1. `DailyFeatures` 加字段 `score: float` + `factors: dict`，由 `DailyContext._compute` 在 refresh 时算好（与 backtest 同款）。
2. `TrendStrategy.on_daily_features(code, name, features)` 镜像 `on_bars` 的 BUY/HOLD 判定逻辑（趋势闸门 + 阈值 + 因子数），只是用 `features.score` 代替分钟线重算。
3. `EventEngine._run_single_step` 改用 `on_daily_features` + `ENTRY_DECISION_INTERVAL_SEC=300` 节流。
4. `EventEngine._run_portfolio_step` 改用 `PortfolioStrategy.select_daily`。
5. `strategy/backtest_minute.py` 加 `--decision both` 模式，跑两条路径同台对比。

## 经验证的事实

1. **同一份 TrendStrategy 代码** + 同一份 1m 数据 + 同一份成本模型 + 同一份 T+1，
   决策路径不同 → 7折 ret 从 -11.67% 翻到 +9.94%（+21.61pt）。
2. **CPU 降低约 99%**：每 5 分钟评分一次而非每 tick × 每只股。
3. **回测与 live 同口径**：`DailyContext._compute` 的 score 严格等于
   `backtest_daily.score_daily` 的输出（同一份指标 + 同一份阈值），
   live 跑出的信号理论上与回测结论可对接。
4. **回归保护**：`tests/test_batch_e_daily_decision.py` 11 例把"必须用
   on_daily_features / 不能是 on_bars / 节流 / DailyFeatures.score
   必须存在"等关键事实钉死。

## 仍待验证

- (F) 批次：EventEngine 拆分 + `_bench_cache.py` 等 24 个 _opt_*.py 整理
- 实盘连续 2–4 周验证 daily 决策在 live 端的稳定性

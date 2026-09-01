# qmtIDE-deepseek 系统架构

> 设计版本 v1.0 ｜ 2026-08-24
> 设计模型：minimax-M3（与 deepseek-v3 同级别的代码推理能力）
> 实现模型：minimax-M3

## 0. 设计目标

| 目标 | 说明 |
|------|------|
| **离线可开发** | miniQMT 未启动 / 无网络时也能写代码、跑测试 |
| **三层降级** | 行情通道、交易通道、AI 通道全部 graceful degradation |
| **策略与 AI 解耦** | 规则策略 + LLM 解释器叠加，主决策仍是确定性的 |
| **数据驱动** | 所有参数可调、可回测、可在 paper 模式跑长周期验证 |
| **小而精** | 总代码 < 1500 行，9 个核心文件，单进程无外部服务依赖 |

## 1. 分层架构

```
┌─────────────────────────────────────────────────────────┐
│  Application Layer                                      │
│    main.py ── EventEngine.run()                         │
├─────────────────────────────────────────────────────────┤
│  Strategy Layer                                         │
│    BaseStrategy                                         │
│      └── TrendStrategy (6 因子，0~10 分)                │
├─────────────────────────────────────────────────────────┤
│  AI Layer                                               │
│    AIAnalyst ── OpenRouterClient (主模型 + 免费 fallback) │
├─────────────────────────────────────────────────────────┤
│  Risk Layer                                             │
│    RiskManager (can_open / on_fill / on_asset_update)    │
├─────────────────────────────────────────────────────────┤
│  Data Adapters                                          │
│    QMTClient (行情) ── QMTBroker (交易)                 │
├─────────────────────────────────────────────────────────┤
│  Storage Layer                                          │
│    Storage (SQLite WAL)                                  │
├─────────────────────────────────────────────────────────┤
│  Foundation                                             │
│    data_models / indicators / settings                   │
└─────────────────────────────────────────────────────────┘
```

**依赖方向**（自顶向下单向）：
- Application → Strategy / AI / Risk / Storage
- Strategy → Indicators / data_models
- AI → indicators（间接通过 strategy._compute_indicators）
- Risk → data_models
- Adapters → xtquant（运行时 try/except 导入）

**禁止反向依赖**：
- Storage 不知道 Strategy
- Risk 不知道 AI
- Adapters 不依赖 Strategy

## 2. 模块清单与职责

| 文件 | 行数级 | 职责 |
|------|--------|------|
| `config/settings.py` | ~180 | 配置中心（路径、UNIVERSE、策略/风控参数） |
| `core/data_models.py` | ~100 | dataclass：Tick / Bar / Signal / Order / Fill / Position / AIAnalysis |
| `core/indicators.py` | ~250 | SMA / EMA / RSI / MACD / KDJ / BOLL / ATR / VWAP（纯 Python） |
| `core/qmt_client.py` | ~280 | 三层降级行情客户端（push + snapshot + K线 + mock） |
| `core/broker.py` | ~240 | XtQuantTrader 封装（cash / credit 双账户） |
| `ai/openrouter_client.py` | ~180 | OpenRouter HTTP（主模型 + 免费 fallback + 3 次重试） |
| `ai/analyst.py` | ~150 | 指标 → prompt → LLM → AIAnalysis（5 分钟缓存） |
| `strategy/base.py` | ~30 | 抽象策略基类 |
| `strategy/trend_strategy.py` | ~250 | 6 因子评分（趋势/动量/超买卖/量价/位置/VWAP） |
| `risk/manager.py` | ~180 | 三层风控（单笔/集中度/日内/熔断/连续亏损/回撤） |
| `storage/db.py` | ~200 | SQLite WAL（signals / orders / fills / ai_analyses / risk_snapshots） |
| `engine/event_engine.py` | ~350 | 主事件循环（拉 tick → 聚 bar → 算指标 → 跑策略 → AI → 风控 → 下单） |

## 3. 关键数据流

### 3.1 单轮 tick 处理（每 `REFRESH_INTERVAL=3s`）

```
QMTClient.get_ticks(universe)
        ↓ {code: raw_tick}
聚合 → self._bars[code] (1m K 线，maxlen=120)
        ↓
持仓评估（先平后开，避免反复）
  TrendStrategy.on_exit(pos, current_price, bars) → SELL Signal?
    ↓ 是
  RiskManager.snapshot()  # 不拦截平仓
  [paper] 更新 ledger
  [live]  QMTBroker.place_order(SELL)
  Storage.save_fill / save_order
        ↓
入场评估（STOCK_CODES 且无持仓）
  TrendStrategy.on_bars(code, bars) → Signal(BUY/HOLD)
  Storage.save_signal(signal)
  if BUY:
    RiskManager.can_open(order) → ok/reject
    ↓ ok
    AIAnalyst.analyze() # 异步线程，不阻塞
    [paper] 更新 ledger
    [live]  QMTBroker.place_order(BUY)
        ↓
每 10 tick → Storage.save_risk_snapshot()
```

### 3.2 AI 异步流（不阻塞主循环）

```
主循环发出 BUY Signal
  ↓
event_engine._fire_ai(code, name, bars)
  ↓ 启动 daemon 线程
worker:
  strategy._compute_indicators(bars)
    ↓ dict
  AIAnalyst.analyze(code, ind)
    ↓ 检查缓存 (key=hash(ind), TTL=300s)
    ↓ 无 key / 无网 → return None
  OpenRouterClient.chat_json(messages)
    ↓ 主模型失败 → OPENROUTER_FREE_FALLBACKS[1..n]
  解析 JSON → AIAnalysis
    ↓
  Storage.save_ai(ai)
  if stance=='bearish' and confidence>=0.7: 标记抑制
```

## 4. 关键设计决策

### 4.1 为什么不用 EventBus？

qmtIDE-kimik3 的 quant_system 用 EventBus 解耦。但对这个 9 文件小项目，EventBus 是过度设计：
- 引入 `event_types.py` + `event_bus.py` 两个文件
- 增加订阅/发布的间接调用
- 单测要 mock bus

直接用函数调用 + dataclass 在层间传值更简单、更易测试。**只在 AI 异步层用了一个真正的 EventLoop（threading.Thread），其他全同步。**

### 4.2 为什么策略分 6 个因子而不是 1 个总评分？

- **可解释**：每个因子 0~2 分，谁贡献一目了然（Signal.factors）
- **可调权重**：因子粒度比单 score 易调（不需要重训）
- **可禁用**：配置开关可关掉任何因子（`enable_*`）
- **回测友好**：网格搜索时按因子调参（未来扩展）

### 4.3 为什么 AI 是叠加层而不是主决策？

- **可观测**：AI stance/confidence 写入 DB，可回放分析
- **可降级**：无 key 时主策略 0 影响
- **可对比**：未来可对比"有 AI vs 无 AI"的回测差异
- **风险可控**：AI 出错最多是"错过一次买入"，不会乱下单

### 4.4 为什么 paper 模式维护内存 ledger？

- **零延迟**：不用每次查 broker
- **可重放**：初始 cash = INITIAL_CASH，重启可重置
- **易测试**：`Storage.save_fill` 直接喂数据

代价：内存 ledger 不会反映真实已发生的手动交易。生产环境必须用 live 模式从 broker 查。

### 4.5 为什么指标纯 Python 不依赖 numpy？

- **离线测试无需 numpy**：哪怕只有标准库也能跑
- **少一层依赖**：项目唯一第三方依赖就是 `requests`
- **可读性高**：循环 < 向量化操作，新人友好
- **性能不是瓶颈**：单标的 60 根 bar 计算 < 1ms

未来真上量再加 numpy 加速。

## 5. 故障与降级矩阵

| 故障 | 检测 | 降级行为 |
|------|------|----------|
| miniQMT 未启动 | `xtquant` import 失败 / 探活 get_full_tick 抛异常 | `QMTClient.mode="mock"`，行情走随机游走 |
| minibroker 未启动 | `XtQuantTrader.connect()` rc≠0 | `QMTBroker.mode="disconnected"`，仅 paper 可用 |
| OpenRouter 无 key | `OPENROUTER_API_KEY=""` | `AIAnalyst.enabled=False`，`analyze()` 返回 None |
| OpenRouter 网络断 | requests 抛 Timeout / ConnectionError | `chat()` 返回 None，AI 不阻塞主循环 |
| OpenRouter 主模型 4xx | HTTP 4xx 响应 | 按 `OPENROUTER_FREE_FALLBACKS` 顺序重试 |
| OpenRouter JSON 解析失败 | `json.loads` 抛异常 | `chat_json()` 返回 None，AI 不阻塞主循环 |
| sqlite 锁 / IO 错误 | sqlite3 抛异常 | `Storage.save_*` 返回 0 并 logging warning，**不抛** |
| 风控熔断 | `RiskManager.is_halted=True` | `can_open()` 返回 False，BUY 被拒 |
| 连续亏损降仓 | `consecutive_losses >= max_consecutive_losses` | `position_scale` 从 1.0 → 0.8 → 0.6 → 0.4 → 0.0 |

## 6. 持久化表结构

```sql
CREATE TABLE signals (
    id INTEGER PRIMARY KEY,
    ts TEXT, code TEXT, name TEXT,
    side TEXT, score REAL, price REAL,
    reason TEXT, factors_json TEXT,
    ai_comment TEXT, ai_confidence REAL
);

CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    ts TEXT, code TEXT, side TEXT,
    quantity INTEGER, price REAL,
    order_type TEXT, account TEXT, order_id TEXT
);

CREATE TABLE fills (
    id INTEGER PRIMARY KEY,
    ts TEXT, code TEXT, side TEXT,
    quantity INTEGER, price REAL, amount REAL,
    account TEXT, order_id TEXT
);

CREATE TABLE ai_analyses (
    id INTEGER PRIMARY KEY,
    ts TEXT, code TEXT, model TEXT,
    summary TEXT, stance TEXT,
    confidence REAL, risks TEXT, raw TEXT
);

CREATE TABLE risk_snapshots (
    id INTEGER PRIMARY KEY,
    ts TEXT, payload_json TEXT
);
```

`PRAGMA journal_mode=WAL`：读写不互斥，主循环写 + UI 读可同时进行。

## 7. 演进路线

### v1.1（短期）
- [ ] Web UI（Flask + SSE，复用 qmtIDE-kimik3 的前端）
- [ ] 多策略并行（PortfolioStrategy，Top-N 建仓）
- [ ] 回测引擎（基于历史 K 线 + tick 模拟）

### v1.2（中期）
- [ ] 异步事件总线（asyncio + aiohttp 替换 requests）
- [ ] 多账户并行（同时跑 paper + live 对比）
- [ ] 因子可视化（PyQt / Streamlit 仪表板）

### v2.0（远期）
- [ ] 分布式：策略进程 + 数据进程 + 交易进程分离
- [ ] 模型微调：本地 LoRA 微调小模型替代 OpenRouter
- [ ] 多市场：港股 / 美股 / 期货扩展

## 8. 测试覆盖

| 模块 | 测试文件 | 覆盖点 |
|------|----------|--------|
| `core/indicators.py` | `test_indicators.py` | SMA/EMA/RSI/MACD/KDJ/BOLL/ATR/VWAP 边界 |
| `core/data_models.py` | `test_data_models.py` | dataclass 字段、property |
| `risk/manager.py` | `test_risk.py` | 单笔/次数/集中度/连续亏损/熔断/恢复 |
| `strategy/trend_strategy.py` | `test_strategy.py` | 指数跳过/warmup/上行买入/止损止盈 |
| `storage/db.py` | `test_storage.py` | save/query/字段过滤/WAL |
| `ai/*` | `test_ai_client.py` | 无 key 跳过/JSON 解析/缓存命中 |

总计 ~35 测试用例，纯 Python 断言，无需 pytest / 网络 / miniQMT。

## 9. 与 qmtIDE-kimik3 的关系

qmtIDE-kimik3 是 Flask + SSE 的完整 Web 监控平台（45 个 REST 路由、84 项功能测试）。
qmtIDE-deepseek 是精简版：

| 维度 | qmtIDE-kimik3 | qmtIDE-deepseek |
|------|---------------|-----------------|
| 总代码 | ~30000 行 | ~2000 行 |
| 文件数 | ~60 | 9 核心 + 6 测试 + 2 自检 + 文档 |
| Web UI | ✅ Flask + SSE | ❌ 命令行 |
| AI 层 | ❌ 无 | ✅ OpenRouter + 缓存 + fallback |
| 推荐引擎 | ✅ sector scorer | ❌ 无 |
| Portfolio 策略 | ✅ Top-N | ❌ 单标的 |
| 回测 | ✅ GridSearch | ❌ 无（未来加） |
| 适合场景 | 完整监控平台 | 学习/研究/AI 协同实验 |

**两者独立可运行，不共享任何代码**。
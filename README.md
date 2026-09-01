# qmtIDE-deepseek

A股量化交易系统（基于 miniQMT xtquant + OpenRouter 免费 LLM），策略与 AI 协同的多因子趋势跟踪。

## 项目结构

```
qmtIDE-deepseek/
├── config/
│   └── settings.py            # 全局配置（QMT 路径 / OpenRouter / UNIVERSE / 策略 / 风控）
├── core/
│   ├── data_models.py         # Tick / Bar / Signal / Order / Fill / Position / AIAnalysis
│   ├── indicators.py          # SMA/EMA/RSI/MACD/KDJ/BOLL/ATR/VWAP（纯 Python）
│   ├── qmt_client.py          # miniQMT 行情（push + snapshot + K线 + mock 降级）
│   └── broker.py              # miniQMT 交易（XtQuantTrader）
├── ai/
│   ├── openrouter_client.py   # OpenRouter HTTP 客户端（主模型 + 免费 fallback）
│   └── analyst.py             # AI 分析师（指标 → 结构化 JSON → AIAnalysis）
├── strategy/
│   ├── base.py                # 抽象基类
│   └── trend_strategy.py      # 6 因子趋势策略（0~10 分）
├── risk/
│   └── manager.py             # 风控（熔断 / 连续亏损降仓 / 仓位集中度）
├── storage/
│   └── db.py                  # SQLite WAL 持久化
├── engine/
│   └── event_engine.py        # 主事件循环（拉 tick → 聚 bar → 算指标 → 跑策略 → AI → 风控 → 下单）
├── tests/                     # 离线单元测试（不依赖 pytest）
│   ├── test_indicators.py
│   ├── test_data_models.py
│   ├── test_risk.py
│   ├── test_strategy.py
│   ├── test_storage.py
│   ├── test_ai_client.py
│   └── run_all.py             # 自建测试 runner
├── docs/
│   └── ARCHITECTURE.md        # 系统架构文档
├── main.py                    # 入口（python main.py）
├── test_connection.py         # miniQMT 连接自检
├── test_ai.py                 # OpenRouter AI 自检
├── trading_config.json        # 交易账户配置（首次运行自动生成示例）
└── requirements.txt
```

## 安装

环境：**conda env `qmt`**（Python 3.9，已装 xtquant / Flask / numpy / pandas / requests）。

```powershell
conda activate qmt
pip install -r requirements.txt
```

## 配置

### 1. miniQMT 路径（默认 `C:\pazq_qmt\userdata_mini`）

编辑 `config/settings.py`：
```python
QMT_PYTHON_PATH = r"C:\pazq_qmt\bin.x64\Lib\site-packages"
QMT_USERDATA_PATH = r"C:\pazq_qmt\userdata_mini"
```

### 2. 交易账户（首次运行自动生成 `trading_config.json`）

```json
{
  "userdata_path": "C:\\pazq_qmt\\userdata_mini",
  "session_id": null,
  "broker_qmt_mode": "XtMiniQmt",
  "accounts": {
    "cash": "你的普通账户ID",
    "credit": "你的信用账户ID"
  },
  "auto_subscribe": true
}
```

> **重要**：要让交易通道连上，miniQMT 必须以"独立交易"模式启动（minibroker.exe 在跑）。
> 参考 `~/Desktop/qmtIDE-kimik3/account_setup_guide.md`。

### 3. OpenRouter API Key（可选）

```powershell
$env:OPENROUTER_API_KEY = "sk-or-..."
```

无 key 时 AI 层自动跳过，主策略照常运行。

### 4. 自定义模型

```powershell
$env:OPENROUTER_MODEL = "deepseek/deepseek-chat-v3-0324:free"   # 默认
```

## 运行

### 单元测试（离线，无需 miniQMT / 网络）

```powershell
cd C:\Users\lisinan\Desktop\qmtIDE-deepseek
python tests/run_all.py
```

### miniQMT 连接自检

```powershell
python test_connection.py
```

### AI 自检

```powershell
python test_ai.py
```

### 主程序（paper 模式，无限循环）

```powershell
python main.py
```

### 主程序（实盘模式，需 minibroker.exe 在跑）

```powershell
python main.py --mode live
```

### 主程序（自检模式，跑 10 轮后退出）

```powershell
python main.py --ticks 10
```

### 主程序（仅打印一次快照）

```powershell
python main.py --snapshot
```

## 设计要点

### 三层降级（行情通道）

1. **xtdata push 订阅**：`subscribe_quote(period="tick")` + 缓存最近 tick
2. **xtdata 快照兜底**：`get_full_tick(codes)` 拉最新价
3. **xtdata K 线兜底**：指数代码用 `get_market_data_ex('1m', count=1)` 反推
4. **mock 兜底**：xtquant import 失败时随机游走

### 三层风控

1. **下单前**：`RiskManager.can_open()` 检查单笔金额、仓位集中度、日内次数、熔断状态
2. **成交后**：`RiskManager.on_fill()` 累计日内盈亏、更新连续亏损计数、触发降仓
3. **全局监控**：`on_asset_update(total_asset)` 跟踪最大回撤，超阈值全停

### AI 协同（OpenRouter）

- **触发时机**：BUY 信号产生后，异步调用 LLM，不阻塞主循环
- **输入**：策略产出的指标 + 大盘上下文（< 500 字 prompt）
- **输出**：结构化 JSON `{stance, confidence, summary, risks}`
- **缓存**：相同指标 5 分钟内复用，节省 token
- **降级**：无 key / 网络失败 / JSON 解析失败 → 返回 None，主策略不受影响
- **抑制**：LLM 强 bearish + confidence ≥ 0.7 时，标记下一轮不买入

### 数据流

```
QMTClient.get_ticks()
        ↓
EventEngine._aggregate_bar() (内存 1m K 线)
        ↓
TrendStrategy.on_bars() → Signal(BUY/HOLD)
        ↓
RiskManager.can_open() → ok / reject
        ↓
AIAnalyst.analyze() (异步，后台线程) → AIAnalysis
        ↓
[paper] EventEngine 内 ledger 更新
[live]  QMTBroker.place_order() + 查询回报
        ↓
Storage.save_signal / save_order / save_fill / save_ai
```

## 风险提示

本项目为研究/教学用途，**不构成投资建议**。

`EXECUTION_MODE = "paper"` 是默认值，**不会真下单**。要切到实盘请：

1. 确认 miniQMT 在"独立交易"模式运行（`Get-Process minibroker`）
2. `trading_config.json` 填入真实账户 ID
3. 用 `--mode live` 启动
4. 先用小资金验证 1 周

## 与其他 qmtIDE 的关系

| 项目 | 模型 | 关键差异 |
|------|------|----------|
| qmtIDE | 未知 | 初版单体 |
| qmtIDE-kimik3 | kimi k3 | Flask + SSE + sector 推荐引擎 |
| qmtIDE-minimax | minimax | 早期重构 |
| qmtIDE-buddyloop | buddy loop | 组合策略 |
| qmtIDE-workbuddy | workbuddy | 另一轮重构 |
| **qmtIDE-deepseek** | **MiniMax-M3 (deepseek-equivalent)** | **精简 + AI 协同 + 无 Flask** |

代码独立可运行，不依赖其他 qmtIDE。

## License

仅供学习。
# qmtIDE-deepseek

A股量化交易系统（miniQMT xtquant + DeepSeek LLM 协同）。

**架构**：日线驱动买卖信号、分钟线驱动执行与日内止损。
**策略内核**：价格动量 + 趋势骑行 + 波动率目标仓位 + 集中最强 3 只（本宇宙 alpha 已收敛于此）。
**正交增强**：**北向资金闸门**（沪深港通净买入，2026-09-19 落盘，与价格动量正交 r≈0.09，用于对冲外资撤离/隔夜跳空）。
**配套**：Flask 实时仪表板（`--web`，127.0.0.1:5000）+ 自进化闭环（双周期 EVOLVE + 周度深度研究）。

> 默认 `EXECUTION_MODE = "paper"`，**不会真下单**；切实盘需人工确认。

## 项目结构

```
qmtIDE-deepseek/
├── config/
│   └── settings.py            # 全局配置（QMT 路径 / LLM / UNIVERSE / 策略 / 风控 / 北向）
├── core/
│   ├── data_models.py         # Tick / Bar / Signal / Order / Fill / Position / AIAnalysis
│   ├── indicators.py          # SMA/EMA/RSI/MACD/KDJ/BOLL/ATR/VWAP（纯 Python）
│   ├── qmt_client.py          # miniQMT 行情（push + snapshot + K线 + mock 降级）
│   ├── broker.py              # miniQMT 交易（XtQuantTrader）
│   └── notices.py             # 系统提示通道（耐久 JSONL logs/notices.log）
├── ai/
│   ├── deepseek_client.py     # DeepSeek 客户端（主用）
│   ├── openrouter_client.py   # OpenRouter 客户端（备用，默认禁用）
│   ├── analyst.py             # AI 分析师（指标 → 结构化 JSON → AIAnalysis）
│   └── llm_reranker.py        # 候选池 LLM 重排序
├── data/
│   ├── northbound_cache.py    # 北向资金（沪深港通 north_money，单位万元）缓存 ← 正交轴
│   ├── moneyflow_cache.py     # 个股主力资金流缓存（诊断证伪，保留备用）
│   ├── tushare_client.py      # Tushare 数据客户端
│   └── stock_names.py         # 全市场股票名解析
├── strategy/
│   ├── backtest_daily.py      # 日线回测引擎（含 moneyflow / northbound 集成）
│   ├── daily_context.py       # 日线多周期上下文（regime / 动量闸门）
│   ├── opt_harness.py         # 回测参数基座（base_cfg 须与生产对齐）
│   ├── _evolve_wf.py          # walk-forward 进化（grid / compare / consensus）
│   ├── _research_*.py         # 结构性 / 新数据轴研究脚本
│   └── record_ledger.py       # 进化账本记录
├── risk/
│   └── manager.py             # 风控（熔断 / 连亏降仓 / 集中度 / 回撤断路器 / 日内硬止损）
├── storage/
│   └── db.py                  # SQLite WAL 持久化（含 engine_state 单例）
├── engine/
│   └── event_engine.py        # 主事件循环 + 北向闸门 + 观察篮 + 板块轮动
├── web/
│   ├── app.py / routes.py     # Flask 仪表板后端（/api/snapshot 等）
│   ├── templates/index.html   # 仪表板页面（含「北向资金」卡片）
│   └── static/js/dashboard.js
├── scripts/
│   ├── guardian.py            # 守护进程（探活 127.0.0.1:5000，停止则自拉起引擎）
│   ├── health_check.py        # 存活检查 + 自启动
│   ├── verify_northbound_boot.py  # 北向轴重启生效验证（PASS/NEED-RESTART/FAIL）
│   └── startup_all.bat        # 开机自启编排
├── tests/                     # 离线单元测试（tests/run_all.py，当前 205 例）
├── docs/
│   ├── ARCHITECTURE.md        # 系统架构
│   └── AUTOMATIONS.md         # 自进化自动化权威清单 + 考核目标
├── reports/                   # 进化决策 / 提案 / 复盘报告
├── main.py                    # 入口（--web / --snapshot / --mode live / --force）
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

### 3. DeepSeek API Key（主用，可选）

```powershell
$env:DEEPSEEK_API_KEY = "sk-..."
```

也可写入项目根目录 `.env`（**已被 .gitignore 忽略，切勿提交**）。
无 key 时 AI 层自动跳过，主策略照常运行。

### 4. OpenRouter（备用，默认禁用）

```powershell
$env:OPENROUTER_API_KEY = "sk-or-..."
$env:OPENROUTER_MODEL = "deepseek/deepseek-chat-v3-0324:free"
```

> 当前 **OpenRouter 已禁用**，LLM reranker 固定走 DeepSeek。

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

### Web 实时仪表板（推荐）

```powershell
python main.py --web
# 浏览器打开 http://127.0.0.1:5000
```

仪表板含：产业链热力图、动态候选池、LLM 重排序、实时行情/信号/成交、持仓与风控，
以及 **「北向资金」卡片**（闸门状态 / 滚动净买入 / 最新一日 / 覆盖天数）。

> Web 需在**可达 xtdata 的持久桌面会话**中启动。
> 若提示 `已有引擎在运行 (pid=N)` 但确认无实例，可加 `--force` 绕过单实例锁，
> 或先 `del logs\engine.pid` 再启动（常见于 pid 被无关进程复用的残留锁）。

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

### 北向资金正交闸门（2026-09-19 落盘）

- **数据**：Tushare `moneyflow_hsgt` 的 `north_money`（沪深港通每日净买入，**单位万元**），
  缓存于 `data/northbound_cache.py`，覆盖约 600 个交易日。
- **为何正交**：与价格动量 / 指数价格相关性极低（r≈0.09），是独立于价格范式的新 alpha 轴。
- **机制**：滚动 `nb_lookback`（默认 20）日累计净买入 **< 0** → 视为外资系统性撤离，
  gate 模式下**不放行新开仓**（已有持仓照常管理，观察篮不受拦）。
- **效果**（样本外 4 窗口共识）：OOS 均值 Sharpe **+0.101**；IS Sharpe 1.52→1.65，
  相对等权买入持有 alpha 由 −111pt 改善至 −90pt（边界值，若后续 OOS<0.10 应回退 `off`）。
- **开关**：`config/settings.py` 的 `northbound_mode`（`"gate"` / `"off"`，**属缓存参数，改后须重启引擎**）、`nb_lookback`。
- **可视化**：仪表板「北向资金」卡片；也可用 `python scripts/verify_northbound_boot.py`
  验证重启后是否真正生效（输出 PASS / NEED-RESTART / FAIL）。

### AI 协同（DeepSeek）

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

> **铁律**：自进化自动化一律保持 `EXECUTION_MODE="paper"`，
> **严禁由自动化改写为 `live`**；切实盘只能由本人手工完成。

## 与其他 qmtIDE 的关系

| 项目 | 模型 | 关键差异 |
|------|------|----------|
| qmtIDE | 未知 | 初版单体 |
| qmtIDE-kimik3 | kimi k3 | Flask + SSE + sector 推荐引擎 |
| qmtIDE-minimax | minimax | 早期重构 |
| qmtIDE-buddyloop | buddy loop | 组合策略 |
| qmtIDE-workbuddy | workbuddy | 另一轮重构 |
| **qmtIDE-deepseek** | **DeepSeek** | **趋势骑行 + 波动率目标 + 北向正交闸门 + Flask 仪表板 + 自进化闭环** |

代码独立可运行，不依赖其他 qmtIDE。

## License

仅供学习。
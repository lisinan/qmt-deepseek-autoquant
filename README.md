# qmtIDE-deepseek

**A 股 AI 产业链量化交易系统** — miniQMT 实盘执行 · 日线定方向 / 分钟线管执行 · 双周期自进化闭环。

> 不是「预测涨跌」的系统。它的 alpha 形态是**低暴露 + 长骑行 + 深回撤**：
> 大部分时间不参与（结构上宁可不做），参与时集中持有最强 3 只并让盈利奔跑，
> 代价是单段回撤较深。真价值在**风控与不参与**，不在收益率数字（见 §四 `kpi4`）。

```text
执行模式  live（真实资金）   │   数据源  xtdata             
行情订阅  38 只              │   交易    xttrader           
单元测试  680 例 / 全绿      │   费率    单边 0.15%         
基线      23 只 × 903 根     │   区间    20230208 → 20261029
```

---

## 一、当前运行状态

| 项 | 值 | 说明 |
|---|---|---|
| `EXECUTION_MODE` | **`live`** | **真实资金下单**。2026-10-29 由 OWNER 手工切换 |
| 切换方式 | 改 `config/settings.py:869` | 不用 `--mode live` 的原因是守护进程无参拉起，若 settings 仍是 paper 会把实盘引擎**静默重启成 paper** |
| `ALLOW_MOCK_TRADING` | `False` | 禁止 mock 成交污染账本 |
| `T1_RESTRICTION` | `True` | 只做 T+1，不隔日回转 |
| 风控底线 | MDD −20% / 回撤恢复 3 日 | **任何自动化不得放松** |

> **⚠️ 切回 paper（回滚实盘）**
> 1. `config/settings.py:869` 改回 `EXECUTION_MODE = "paper"`
> 2. 重启引擎（`.py` 改动**必须重启**才重新 import —— 全仓库无 `importlib.reload`）
> 3. 若刚切换，需同步重置 `engine_state` 的 `peak_asset / peak_equity / day_open_asset` 为本金，
>    否则首轮会以旧峰值为基准算出假回撤（历史已踩过一次：−93% 假熔断）
>
> **铁律**：自进化自动化**严禁**把 `EXECUTION_MODE` 改为 `live`；切实盘只能由本人手工完成。

---

## 二、策略内核

**入场是三重合取**（全部满足才买）：

| 条件 | 参数 | 当前值 |
|---|---|---|
| 动量排名进 Top N（默认 60 日涨幅） | `momentum_top_n` / `momentum_lookback` | 3 / 60 |
| 动量下限 | `momentum_min_mom` | 0.15（+15%） |
| 趋势向上（收盘 > MA60 等四子条件） | — | 强约束，弱市里**它**才是绑定点 |
| 不追高（偏离 MA60 上限） | `entry_max_ext_ma60` | 0.8 |
| 宽度确认（至少 N 只同时通过） | — | 2 |
| 大盘日偏置 | `min_daily_bias` | 2.0 |

**退出（`exit_mode = "trend"`）**：趋势破位 + ATR 止损（`atr_stop_mult = 2.9`）+ 单日暴跌清仓（`down_day_exit_pct = −3.25`）。

**仓位**：波动率目标制。`risk_per_trade = 4%`、`max_single_position_pct = 40%`、`max_positions = 5`。
三者相乘名义上限 200%，实际受 `max_positions` 与 ATR 共同夹紧。

**不参与也是一种仓位**：2026-04 → 2026-10 弱市期连续零 BUY 是**设计行为**，不是故障。
回测权益在该区间同样零变动 ⇒ 回测与实盘行为一致。

---

## 三、系统架构

```
┌────────────────────────── 数据层 ──────────────────────────┐
│ xtdata push 订阅 → get_full_tick 快照 → 1m K 线兜底        │
│ Tushare（日线缓存 / 北向资金 / 股票名）                    │
└──────────────────────────────┬─────────────────────────────┘
                               ↓
┌────────────────────────── 决策层 ──────────────────────────┐
│ strategy/daily_context.py   日线上下文（regime / 动量闸门）│
│ strategy/trend_strategy.py  日线信号 → Signal(BUY / HOLD)  │
│ strategy/sector_scorer.py   板块热度（仅排序，不改决策）   │
└──────────────────────────────┬─────────────────────────────┘
                               ↓
┌────────────────────────── 执行层 ──────────────────────────┐
│ engine/event_engine.py  主事件循环 · 分钟执行 · 日内止损   │
│  ├─ 入场漏斗（动量 → trend_up → 宽度 → 追高 → 风控）       │
│  ├─ 券商持仓同步（每轮以 broker 为权威源）                 │
│  └─ 观察篮 / 轮动 / 基准管道 / 盯市兜底                    │
│ risk/manager.py         熔断 · 连亏降仓 · 集中度 · 回撤    │
│ core/broker.py          XtQuantTrader 下单 + 回报查询      │
└──────────────────────────────┬─────────────────────────────┘
                               ↓
┌────────────────────────── 观测层 ──────────────────────────┐
│ storage/db.py     SQLite WAL（signals / orders / fills）   │
│                  + equity_snapshots / engine_state         │
│ web/              Flask 仪表板 127.0.0.1:5000 + SSE        │
│ logs/notices.log  耐久告警通道（JSONL）                    │
└────────────────────────────────────────────────────────────┘
```

**为什么日线定方向、分钟线管执行**：日线信号慢但噪声小（决定「买不买」），
分钟线快但假信号多（只管「怎么买 / 何时止损」）。混用会让两者互相污染。

---

## 四、基线表现（2026-10-29 实测）

`python strategy/_evolve_wf.py --mode gate`，23 只 AI 宇宙 × **903 根**日线（2023-02-08 → 2026-10-29），单边费率 0.15%：

| 指标 | 值 |
|---|---|
| 样本内（IS）收益 | **+313.12%** |
| IS Sharpe / 最大回撤 | **+1.99** / **−12.97%** |
| OOS 6 折（90 根/折）均值 Sharpe | **+1.828** |
| OOS 正收益折数 | 4 / 6 |
| OOS 均值 MDD / 最差单折 | **−8.82%** / **−3.0%** |
| 拼接 Sharpe（划分不变量） | **+1.9908** |
| 相对等权买入持有 alpha | **+191.8pt** |

KPI 判定（目标值来源 `config/kpi_targets.py`，单一代码来源）：

| ID | 指标 | 目标 | 当前 | 判定 |
|---|---|---|---|---|
| `kpi1` | wf 折均值 Sharpe | ≥ 1.60 | 1.8282 | **PASS** |
| `kpi3` | 最差单折收益 | ≥ −15% | −2.98% | **PASS** |
| `kpi4` | 全样本 alpha | ≥ −30pt | +191.8pt | **PASS**（主 KPI） |
| `kpi1_span` | 拼接 Sharpe | ≥ 1.60 | 1.9908 | proposed（D4 待裁决是否替代 kpi1） |
| `kpi2b` | 日历月正收益占比 | ≥ 55% | 51.3% | proposed（未达） |
| `kpi2` | wf 正收折数 ≥6/7 | — | 4/6 | **deprecated**（已证划分依赖） |
| `kpi_sig` | DSR@100 显著性 | ≥ 0.95 | 0.8265 | proposed（多重检验校正后不显著） |

> **口径纪律**：`kpi2`（折/段聚合型指标）已被证是**划分方式依赖的统计量**，不是策略属性 ——
> 同一条 +218.8% 的曲线，切 9 段报 77.8% 正收、切 2 段报 50.0%，而拼接 Sharpe 恒为 2.1596。
> 任何按「折 / 段」聚合的指标都必须先问「换个切法结论还成立吗」。

---

## 五、项目结构

```text
qmtIDE-deepseek/
├── config/
│   ├── settings.py             # 全局配置唯一真源（QMT 路径 / UNIVERSE / 策略 / 风控 / 模式）
│   └── kpi_targets.py          # KPI 目标 / 口径 / 状态（active / proposed / deprecated）
├── core/
│   ├── data_models.py          # Tick / Bar / Signal / Order / Fill / Position / AIAnalysis
│   ├── indicators.py           # SMA/EMA/RSI/MACD/KDJ/BOLL/ATR/VWAP（纯 Python）
│   ├── qmt_client.py           # 行情（push + snapshot + K线 + mock 降级）
│   ├── broker.py               # 交易（XtQuantTrader）
│   ├── qmt_health.py           # 健康探针（引擎 / 行情 / 交易 / 风控）
│   └── notices.py              # 耐久告警通道（logs/notices.log）
├── ai/
│   ├── deepseek_client.py      # DeepSeek 客户端（主用）
│   ├── openrouter_client.py    # OpenRouter（备用，默认禁用）
│   ├── analyst.py              # 指标 → 结构化 JSON → AIAnalysis
│   └── llm_reranker.py         # 候选池重排序（只改展示顺序，不改决策）
├── data/
│   ├── northbound_cache.py     # 北向资金缓存（正交轴，已 fail-open，见 §九）
│   ├── moneyflow_cache.py      # 主力资金流缓存（已证伪，保留备用）
│   ├── tushare_client.py       # Tushare 客户端
│   └── stock_names.py          # 代码 → 名称解析
├── strategy/
│   ├── trend_strategy.py       # 趋势策略本体
│   ├── daily_context.py        # 日线多周期上下文（regime / 动量 / 趋势闸门）
│   ├── backtest_daily.py       # 日线回测引擎（研究主入口）
│   ├── benchmark.py            # 等权买入持有基准管道
│   ├── kpi_stats.py            # 划分不变量统计（span_sharpe / month_win_rate）
│   ├── _evolve_wf.py           # 验证器（grid / compare / consensus / span / gate）
│   ├── _param_audit.py         # 参数读点审计（哪些参数根本测不了）
│   ├── opt_harness.py          # 回测参数基座（base_cfg 须与生产对齐）
│   ├── record_ledger.py        # 跨日真实收益 → 进化账本
│   └── review_daily.py         # 盘后复盘 + 四维评分
├── risk/
│   └── manager.py              # 熔断 / 连亏降仓 / 集中度 / 回撤断路器 / 日内止损
├── engine/
│   └── event_engine.py         # 主事件循环（入场漏斗 / 持仓同步 / 盯市兜底 / 观察篮）
├── storage/
│   └── db.py                   # SQLite WAL 持久化（含 engine_state 单例）
├── web/
│   ├── app.py / routes.py      # Flask 后端
│   ├── templates/index.html    # 仪表板（今日决策条 / 资产风控 / 持仓 / 行情 / 信号）
│   └── static/js/dashboard.js
├── scripts/
│   ├── guardian.py             # 守护进程（5 分钟轮询，掉线自拉起引擎）
│   ├── health_check.py         # 存活检查 + 自启动（--start 才真拉起）
│   ├── _recursive_self_improve.py  # ★ 二阶优化元审计（四层）
│   ├── verify_northbound_boot.py   # 北向轴重启生效验证
│   └── startup_all.bat         # 开机自启编排
├── tests/                      # 680 例离线测试（tests/run_all.py，无需 miniQMT/网络）
├── docs/
│   ├── ARCHITECTURE.md         # 系统架构
│   ├── AUTOMATIONS.md          # 自动化权威清单 + 考核目标 + 晋升闸门
│   └── RECURSIVE_SELF_IMPROVEMENT.md  # 二阶优化设计
├── reports/
│   ├── EVOLUTION_DECISIONS.md  # 进化决策账本（每轮固定字段 + 回滚方式）
│   ├── OPEN_DECISIONS.md       # 待 OWNER 裁决清单
│   └── meta/RSI_META_LATEST.md # 元审计报告（四层）
├── main.py                     # 入口
└── requirements.txt
```

---

## 六、快速开始

### 环境

**conda env `qmt`**（Python 3.9，已装 xtquant / Flask / numpy / pandas / requests）。

```powershell
conda activate qmt
pip install -r requirements.txt
```

> Windows 下需把 `Library\bin` 与 `DLLs` 加入 `PATH`，否则 sqlite3 DLL 加载失败：
> ```bash
> export PATH=/c/Users/<you>/.conda/envs/qmt/Library/bin:/c/Users/<you>/.conda/envs/qmt/DLLs:$PATH
> export PYTHONPATH=.
> ```

### miniQMT 配置

`config/settings.py`：

```python
QMT_PYTHON_PATH   = r"C:\pazq_qmt\bin.x64\Lib\site-packages"
QMT_USERDATA_PATH = r"C:\pazq_qmt\userdata_mini"
```

首次运行生成 `trading_config.json`（**含账户 ID，已在 .gitignore 内，切勿提交**）：

```json
{
  "userdata_path": "C:\\pazq_qmt\\userdata_mini",
  "broker_qmt_mode": "XtMiniQmt",
  "accounts": { "cash": "你的普通账户ID", "credit": "你的信用账户ID" },
  "auto_subscribe": true
}
```

> 交易通道要求 miniQMT 以**独立交易**模式启动（`minibroker.exe` 在跑）。

### LLM（可选）

```powershell
$env:DEEPSEEK_API_KEY = "sk-..."      # 无 key 时 AI 层自动跳过，主策略照常运行
```

### 运行

```powershell
python tests/run_all.py        # 离线测试（680 例，约 90s，无需 miniQMT）
python test_connection.py      # miniQMT 连接自检
python main.py --snapshot      # 打印一次快照
python main.py --web           # ★ 推荐：引擎 + 仪表板 http://127.0.0.1:5000
python main.py --ticks 10      # 自检模式，跑 10 轮退出
python main.py --stop          # 优雅停止（协作式，勿 kill）
```

### CLI 全量

| 参数 | 用途 |
|---|---|
| `--web` / `--port` / `--host` | 启动引擎 + Flask 仪表板 |
| `--ticks N` | 跑 N 轮后退出（自检） |
| `--snapshot` | 只打印一次快照 |
| `--mode paper\|live` | 临时覆盖执行模式（**生产请改 settings**） |
| `--stop` | 优雅停止运行中的引擎 |
| `--force` | 绕过单实例锁（pid 残留时用） |
| `--init-positions` | 以券商持仓初始化本地账本 |
| `--db-stats` / `--prune-db N` / `--vacuum-db` | 数据库体检 / 按天裁剪 / 压缩 |

### 验证器（研究主入口）

```powershell
# 基线 + KPI 判定表（约 3s）
python strategy/_evolve_wf.py --mode gate

# 单参数网格扫描（--values 用逗号分隔）
python strategy/_evolve_wf.py --mode grid --param atr_stop_mult --values 2.5,2.9,3.2

# 多窗口共识（晋升闸门的硬依据）
python strategy/_evolve_wf.py --mode consensus --track trend

# 划分不变量口径（同一条曲线换切法）
python strategy/_evolve_wf.py --mode span --count 900

# 参数读点审计：哪些参数在回测里根本没有读取点
python strategy/_param_audit.py
```

---

## 七、自进化闭环

三层结构，职责严格分离 —— 混淆这三层是过去半年最大的时间浪费来源。

| 层 | 优化对象 | 执行者 | 频率 |
|---|---|---|---|
| **L1 一阶** | **策略参数** | AM-EVOLVE / PM-EVOLVE | 每交易日 11:40 / 18:00 |
| **L2 二阶** | **改进流程本身**（目标函数 / 验证器 / 搜索空间 / 观测 / 账本） | RSI（递归自进化） | 每周六 11:00 |
| L3 三阶 | 「L2 的改动是否真的提升了 L1 命中率」 | RSI 长期职责 | 尚未建立度量 |

L1 的任务清单（`docs/AUTOMATIONS.md`）：

| 环节 | 时间 | 职责 |
|---|---|---|
| GUARD | 每小时（工作日） | 引擎 / miniQMT 在线检查，掉线自拉起 |
| PRE-MARKET | 09:05 | 盘前就绪 + 确认上一轮改动生效方式（热读 / 需重启） |
| **AM-EVOLVE** | **11:40** | 半日度量 → 候选 → consensus → 过闸门落盘（下午即生效） |
| OBSERVE | 15:35 | 盘后复盘 + 四维评分 + 账本记录 + HTML 报告 |
| 工程维护 | 17:00 | DB / 日志体检 + 全量回归 + 告警降噪 |
| **PM-EVOLVE** | **18:00** | 全日度量 → 接力 AM 方向 → consensus → 落盘（次日生效） |
| SYNTHESIZE | 周六 10:00 | 周度深度研究 + 结构性候选 |
| **RSI** | **周六 11:00** | 审计改进流程本身，每周只改一条 |

**晋升闸门（改参数前必须全中，缺一即拒）**：IS 有提升 / OOS 均值 Sharpe **+0.10** /
OOS−IS 差距 ≤25% / MDD ≥ −22% / 无单折 < −15% / 测试全绿 / 风险底线不动 /
`EXECUTION_MODE` 由 OWNER 手工控制（自动化无权改写）。

### RSI 元审计（L2）

```powershell
python scripts/_recursive_self_improve.py    # → reports/meta/RSI_META_LATEST.md / .json
```

| 层 | 审计内容 | 最近结果（2026-10-29） |
|---|---|---|
| L1 搜索空间 | 双向可验证 / 仅实盘 / 仅回测 / 死参数 | 77 参数，可用率 **65%**（双向 50 + 别名 1），仅实盘 23，死参数 3 |
| L2 基线漂移 | `_evolve_wf.base_cfg()` vs `settings.py` 逐字段 | 可比 53 项，漂移 **0** |
| L3 循环活性 | 各自动化最近运行时间 | 停摆 2/10（均为「已启用但触发未落地」，非被暂停） |
| L4 循环能力 | 划分不变量 / 多窗口共识 / 不变性 / 守卫 | 6/6 齐备 |

**为什么需要这一层**：L1 已经撞到天花板 —— 参数空间无可挖 alpha。
4 个负折**全部**落在 2026-04 → 2026-10 的同一段日历区间，此前 22 折全正。
在这种结构下继续扫参数是徒劳，真正卡住收益的是**循环自身的缺陷**。

---

## 八、量测卫生（本项目最值钱的部分）

这套纪律不是洁癖，每一条都是被真实错误换来的。**做研究前请先读这一节。**

| 纪律 | 为什么 |
|---|---|
| **划分不变量优先** | 折均值 Sharpe、正收折数是**划分方式**的函数，不是策略属性。决策用 `kpi_stats.span_sharpe` / `month_win_rate` |
| **「Sharpe 升」不能单独作为决策依据** | 必须同时看收益与 Calmar，并做**逐折指纹**检查：若正向全在基线亏钱折、负向全在基线大赚折 ⇒ 那是削峰填谷（波动平滑），不是 alpha |
| **不变性检验** | 改研究代码后，P0 基线必须**逐位相同**；重构后必须复跑（曾靠这条抓到 rank off-by-one） |
| **正向对照** | 某旋钮 dSh 全 0.000 时，先压到「必触发档」跑一遍，确认代理真的接上了。**检测器退化成恒绿**是最危险的失败模式 |
| **dSh 恒 0.000 的第一解释是「测错了对象」** | 先查 `_param_audit.py`：仅实盘参数在回测里没有读取点，测的是空气 |
| **落盘 ≠ 生效** | 核对「引擎启动时刻 ≥ 文件 mtime」。改 `.py` 必须重启（无 `importlib.reload`） |
| **同一事实只允许一份实现** | 订阅清单、参数读点表都曾出现两份副本静默漂移，报告读 A、守卫钉 B |
| **写测试要钉契约，不钉副产品** | 否则数据源一变，测试就从「守卫」变成「绊脚石」 |

守卫测试：`tests/test_search_space_contract.py`（12 例，钉住全部生产参数的读点分类，
并断言「可用率 ≥40% + 关键项逐个点名」，防检测器退化成恒空集）、
`tests/test_evolve_baseline_sync.py`（生产配置 vs 验证器基线漂移）、
`tests/test_mark_price_fallback.py`（含正向对照，断言旧实现必然触发假回撤）。

---

## 九、已知边界与已证伪方向

**项目当前最大的认知资产**：哪些方向已经试过、为什么不该再试。省下的每一轮迭代都是真实的。

### 已证伪（勿重试）

| 维度 | 已闭合的轴 |
|---|---|
| 入场 | `min_daily_bias`（全档饱和）· `trend_up` 各档严格度 · `buy_score_threshold`（≤4.0 恒不绑定）· `min_signals` · `momentum_lookback` / `momentum_top_n` · 放宽动量闸门 · 扩宇宙 |
| 退出 | 固定止损 · 移动止损 / 吊灯 · 快慢两向 MA 破位 · `trend_max_hold_days` · `trend_exit_ma` 快慢全档 · 再入冷却 |
| 仓位尺度 | `max_positions` ≥4（惰性）· `max_single_position_pct` · `risk_per_trade` · conviction 加仓全剂量 |
| 风控降级 | 连亏降仓全轴 · 日内亏损阈值全剂量 · 权益回撤断路器（**吸收态**）· 波动率确认 · 风险平价 |
| 结构 | 板块上限 · VWAP 因子 · 「不追高」严格度 · 北向闸门（数据源失效，见下） |

### 结构性的天花板

- **KPI#2「折均值 / 正收折数」不可达**：已证是划分假象，非参数缺陷，正在等 OWNER 裁决撤销（`reports/OPEN_DECISIONS.md` D9）。
- **显著性不足**：4 个已落盘参数的 dSR 95%CI 全部跨 0，DSR@100 ≈ 0.83 < 0.95（`kpi_sig`）。
- **北向闸门已 fail-open**：交易所自 2024-08-19 起停止披露每日净买入额，`north_money` 变为成交额口径（全正窄带），「滚动净买入 < 0」的拦截条件**自 2025 年起从未触发**。`northbound_mode="gate"` 不再拦截任何建仓，退役需一次 re-baseline 轮次。
- **弱市里真正绑定的不是动量而是 `trend_up`**：2026-10-21 实测 38 只候选中 0 只通过「收盘 > MA60」。放宽动量闸门（0.15 → 0.0）会让 OOS 均值 Sharpe **−0.207**、最差折跌破 −15% 底线。

### 当前 live 的现实约束

账户资金量有限，一手 100 股 ⇒ 候选池里只有少数标的买得起一手（高价股如 300308 单手即需数万元）。
因此 live 的可执行集明显小于回测宇宙，**两者收益不可直接横比**。

---

## 十、运维 Runbook

### 启动 / 停止

```powershell
python main.py --stop          # 优雅停止（写 STOP_ENGINE，等主循环自退）
python scripts/health_check.py --start   # 存活检查，缺失则自拉起
```

> **⚠️ 不要在交互式 shell 里后台拉起引擎**
> `cmd /c run_live.bat &`、`Popen`、甚至 `DETACHED_PROCESS` 拉起的进程
> **都会随调用方进程树被回收**（沙箱作业对象），表现为「启动成功 → 25 秒后静默消失，日志无异常」。
> WMI `Win32_Process.Create` 通常被安全策略拦截。
> **唯一可靠路径**：让已在独立会话的 `guardian.py` 拉起（5 分钟轮询）。

### 重启后必查

```powershell
curl -s http://127.0.0.1:5000/api/qmt_health
```

| 字段 | 期望 |
|---|---|
| `engine.exec_mode` | `live`（或 `paper`，按当前计划） |
| `engine.data_mode` | `xtdata` |
| `market.client.subscribed` | 38（含探针 1 只） |
| `trading.broker.connected` | `true` |
| `trading.risk.halted` | `false` |
| `issues` | `[]` |
| `logs/quant_system.log` | 出现 `执行=live 数据源=xtdata 券商=xttrader \| 订阅标的=38` |

**常见误判**：盘后 `/api/ticks` 返回 0 条是正常的（非交易时段休眠）；
Web 面板「总资产」取 `equity_snapshots` 最新一条，切 live 当天若已收盘会显示旧 paper 值 ——
**不要据此判断引擎是否生效**。

### 数据维护

```powershell
python main.py --db-stats            # 体积 / 表分布
python main.py --prune-db 180        # 保留 180 天
python main.py --vacuum-db           # 压缩
```

---

## 十一、AI 协同（可选）

- **触发**：BUY 信号产生后异步调用，不阻塞主循环；相同指标 5 分钟内复用（省 token）
- **输出**：结构化 JSON `{stance, confidence, summary, risks}`
- **降级**：无 key / 网络失败 / JSON 解析失败 → 返回 `None`，主策略不受影响
- **否决**：`AI_VETO_MODE` 当前 = **`log`** —— 只记录标记与告警，**不阻断任何交易**。
  设为 `"veto"` 才实际否决（该口径尚未验证，勿默认开启）
- **LLM 重排**：`ai/llm_reranker.py` 只影响**展示排序**，不改变交易决策

---

## 十二、风险提示

本项目为研究 / 工程实践用途，**不构成投资建议**。

- 当前 `EXECUTION_MODE = "live"`，**会真实下单**。实盘前请确认：账户 ID 正确、miniQMT 独立交易模式已启动、先用小额验证 1 周。
- 策略的 alpha 形态决定它会在某些月份**完全不参与**，也会出现较深单段回撤（历史 MDD 约 −13%，设计上限 −20%）。
- 回测含 0.15% 单边费率与 T+1 限制，但**不含**滑点、涨跌停无法成交、停牌等真实摩擦。
- 任何自动化都不被允许修改 `EXECUTION_MODE`、放松风控底线、或 kill 交易引擎。

## 十三、与其他 qmtIDE 的关系

| 项目 | 模型 | 关键差异 |
|---|---|---|
| qmtIDE | 未知 | 初版单体 |
| qmtIDE-kimik3 | kimi k3 | Flask + SSE + sector 推荐引擎 |
| qmtIDE-minimax | minimax | 早期重构 |
| qmtIDE-buddyloop | buddy loop | 组合策略 |
| qmtIDE-workbuddy | workbuddy | 另一轮重构 |
| **qmtIDE-deepseek** | **DeepSeek** | **趋势骑行 + 波动率目标 + 三层自进化闭环 + 量测卫生体系** |

代码独立可运行，不依赖其他 qmtIDE。

## License

仅供学习。

# 启动顺序与接口调用优先级（唯一事实源）

> 建立于 2026-11-13 09:40，依据：当日实测日志 + 代码读点。
> **任何与此不符的运行时行为都按缺陷处理**，不要用"大概是这样"绕过。
> 相关文档：`docs/ZONE.md`（双功能区隔离）｜`docs/QMT_BRIDGE.md`（文件桥协议）｜`docs/AUTOMATION_CONTRACT.md`（自动化门禁）

---

## 一、进程启动顺序

整机开机后的真实次序（大 QMT 方案，`broker_qmt_mode="bridge"`）：

| # | 进程 / 动作 | 谁拉起 | 存活判据 |
|---|---|---|---|
| 1 | `XtItClient.exe`（QMT 客户端，~1GB） | 开机自启 | 进程存在 |
| 2 | QMT 里手工点「**运行策略**」→ 桥 `init()` | OWNER | `heartbeat.json` 新鲜 + `boot.log` 有 `init done` |
| 3 | `scripts/guardian.py`（300s 巡检） | 开机自启 | `logs/guardian.log` 每 300s 一行 |
| 4 | `python main.py --web`（引擎 + Flask） | guardian（缺失时拉起） | `/api/snapshot` 有响应 |

### 两条判据，别混用

- **桥存活看心跳，不看进程名。** 大 QMT 方案下桥跑在 `XtItClient.exe` 内部，
  `XtMiniQmt.exe` 在本机**永不出现**；用进程名判会得到"桥没起来"的假告警。
  正确判据：`python scripts/bridge_health.py`（读 `heartbeat.json`，退出码 0 = 新鲜）。
- **引擎存活看 API，不看进程名。** `tasklist` 里有两个 `python.exe` 是常态
  （guardian 的 `pythonw.exe` + 引擎），数量不是判据。

### 端口

| 端口 | 用途 | 定义 |
|---|---|---|
| 5000 | live 仪表板 + 交易面板 | `core/zone.py:119` `_LIVE_WEB_PORT` |
| 5001 | paper 仪表板 | `core/zone.py:105` `_LIVE_WEB_PORT + 1` |
| 5002 | 只读看板 `zone_dashboard.py`（独立进程，不 import 项目模块） | `scripts/zone_dashboard.py` |

---

## 二、引擎内部初始化顺序

`main.py:609` `EventEngine(...)` → `main.py:640` `run_web(...)`。构造函数内的次序：

| # | 步骤 | 日志特征 / 依据 |
|---|---|---|
| 1 | 单实例锁 | `main.py:605` `_acquire_singleton`（`logs/engine.pid`） |
| 2 | 行情客户端 `QMTClient` → 源选择 | 见 §三 |
| 3 | **风控状态恢复** | `恢复风控状态: peak=.. consec_loss=.. halted=..` |
| 4 | 交易通道 `_make_broker()` | `core/broker.py:608`，**模块导入期定型一次** |
| 5 | 持仓初始化 + `open_date` 回灌 | `持仓初始化: N 只, cash=..`／`open_date 回灌: CODE ← ...（来自 fills#N）` |
| 6 | 分钟线预热 | `分钟线预热完成：就绪 N / ...` |
| 7 | 北向缓存检查 | `北向闸门已 fail-open（stale）`（**已知陈旧，不作为拦截依据**） |
| 8 | 启动 Web | `werkzeug` 开始打请求日志 |
| 9 | 首轮 `_run_once` | 交易时段守卫：非交易时段 **60s 休眠，不拉行情/不聚合 bar/不下单** |

> 第 3 步是**最容易出事的一步**：`halted` / `peak_asset` 从 DB 原样恢复。
> 历史事故：11-12 因口径错误熔断，状态落库，11-13 启动两次都原样恢复 ⇒ 连续两天不可交易。

---

## 三、行情源优先级

入口：`core/qmt_client.py:381` `_preferred_sources()`，受 `QMT_QUOTE_SOURCE` 控制。

```
QMT_QUOTE_SOURCE=auto（默认）  →  xtdata  →  tushare  →  mock
QMT_QUOTE_SOURCE=xtdata        →  xtdata  →  mock
QMT_QUOTE_SOURCE=tushare       →  tushare →  mock
```

- 逐源试连（`:395`），任一成功即返回（`:399-404`）
- 全部失败 → `mode="mock"`（`:412`）⇒ **当日禁止一切买卖**
  （`mock_trade_block_reason`，只拦交易不拦行情读取）
- **实测常态**：`xtdata` 必然失败（`miniQMT probe failed: not authenticated`，
  大 QMT 2.1.32.0 框架白名单把外部 python 判 illegal pid）⇒ 实际生效的永远是 tushare
- 交易时段开启后仍有重探：09:22 落入 mock → 09:26:06 恢复 tushare（实测时间线见 §八）

### tushare 内部：主路 + 辅路

| 角色 | 接口 | 节流 | 真实额度（实测原文） |
|---|---|---|---|
| **主路** | `ts.realtime_quote` | `MIN_REFRESH_SEC=8.0`（`tushare_quote.py:271`） | 连续 150 次 / 324 秒零失败，无可见日限额 |
| **辅路** | `pro.rt_tick` | `RT_TICK_MIN_INTERVAL=180.0`（`:270`） | 「100 次/**小时**」+「200 次/**天**」（`:268-269`） |

- 探活 `_probe()`（`:367`）：`rt_tick` 有数 → 主路可用；无权限 → 整源回落 `realtime_quote`；全无 → 抛错让 `QMTClient` 降级
- **撞限**（`:272` `BACKOFF_SEC=60`）：记静默期，且**无条件回落 `realtime_quote`**
  （2026-11-11 前的实现只给"无缓存"标的补价 ⇒ 38 只全冻、开盘 13 分钟行情死）
- `realtime_quote` 覆盖沪市 ETF（`rt_tick` 不覆盖 510050/510300/512880），所以它必须是主路而不是兜底

---

## 四、交易通道优先级

`core/broker.py:608` `_make_broker()` 在**模块导入期**执行一次（`qmt_broker = _make_broker()`），
此后全进程共用该对象 ⇒ 单点定型，**无法中途翻案**（改配置必须重启）。

| 优先级 | 条件 | 通道 | 依据 |
|---|---|---|---|
| 1 | `QMT_ZONE=paper` | `_NoTradeBroker`（构造期拒单） | `core/broker.py:614` |
| 2 | `trading_config.json::broker_qmt_mode="bridge"` | `BridgeBroker`（文件桥 → `passorder`） | `core/broker.py:620-626` |
| 3 | 其余（默认 `XtMiniQmt`） | `XtQuantTrader`（xttrader） | `core/broker.py:28` |

**当前生产 = 2（bridge）**。原因：QMT 2.1.32.0 白名单把外部 python 判 `illegal pid`，
xttrader/xtdata 全废；桥跑在 QMT 进程内部，不受此限。

> 第 1 优先级的注意点：`_NoTradeBroker` 在**构造期**就位，连 `xtquant.xttrader` 都不会 import
> ⇒ paper 区不可能误下真单，也不可能占 xttrader session。

### 文件桥协议（bridge 模式）

```
引擎 --写--> C:\pazq_qmt_bridge\signal.json
QMT 侧轮询（POLL_MS=500）--> 执行 --> result.json
引擎 --读--> result.json（按 signal_id 匹配，超时判断开）
心跳 --> heartbeat.json（含 CODE_REV）
```

- **`CODE_REV` 是"部署≠生效"的唯一验收凭据**：改完桥文件必须在 QMT 里「停止策略 → 运行策略」，
  然后 `heartbeat.json.rev` 必须变。rev 不变 = 改的是磁盘上的文件，不是 QMT 里跑的代码。
- `passorder` 是**异步 fire-and-forget**：桥侧判定 `ok` 只代表"已投递给 QMT"，
  不代表柜台受理。成交只能靠 `QUERY_ORDER` / `QUERY_TRADE` 反查，或下一轮 `_sync_broker_positions` 校正。

---

## 五、接口调用频率（引擎侧）

| 通道 | 频率 | 依据 |
|---|---|---|
| 主循环 | 3s | `REFRESH_INTERVAL` |
| 行情取价 | 8s（`realtime_quote`） | `tushare_quote.py:271` |
| `rt_tick` 校准 | 180s | `tushare_quote.py:270` |
| 入场决策 | 300s | `ENTRY_DECISION_INTERVAL_SEC` |
| 退出/止损/对账 | 每轮 ≈8s | 主循环 |
| 权益快照落库 | ≈60s | `_persist_equity` 节流 |
| 心跳/存活确认 | ≈10min | `心跳` 通知 |
| 券商持仓/资产同步 | 每轮 | `_sync_broker_positions` |

**非交易时段**：60s 休眠，不拉行情、不聚合 bar、不下单（`交易时段守卫已启用`）。

---

## 六、降级链与自愈

| 故障 | 检测点 | 自愈 / 人工动作 |
|---|---|---|
| 行情源全挂 | `_preferred_sources` 全失败 | 落 `mock`（禁交易），交易时段每 120s 重探 |
| `rt_tick` 撞限 | 异常文本含「频率」「超限」 | 静默 60s + **回落 `realtime_quote`**（行情不中断） |
| 行情陈旧 >120s | `_market_stale` | 暂停建仓；**注意：卖出也被这条锁住**（措辞只说"建仓"） |
| 桥超时（无 result.json） | `_request` deadline | 标通道断开 → `AutoReconnector` 重连 |
| 引擎进程消失 | guardian 300s 巡检 | 自动拉起 `main.py --web`（不手动起，避免双实例） |
| 熔断 `halted=True` | 恢复风控状态日志 | **不自动解除**，需 OWNER 授权改库后重启 |
| 桥代码未加载 | `heartbeat.rev` 与仓库不一致 | 只在 QMT 里重跑策略才能解决，重启引擎无效 |

---

## 七、启动自检（三条命令）

```bash
# 1) 桥：rev 是否最新 + 心跳是否新鲜（退出码 0 = 新鲜）
python scripts/bridge_health.py

# 2) 引擎：口径 + 风控状态 + 行情年龄
curl -s --noproxy '*' http://127.0.0.1:5000/api/snapshot
curl -s --noproxy '*' http://127.0.0.1:5000/api/qmt_health    # issues 里看 stale / 熔断 / 仓位系数

# 3) 行情额度与延迟（休市也能跑）
python scripts/quote_probe.py
```

**健康基线**（2026-11-13 实测）：`exec=live`、`data=tushare`、`broker_connected=True`、
`asset_trusted=True`、`halted=False`、`position_scale=1.0`、issues 里无 `bad`。

---

## 八、2026-11-13 启动时间线（实测证据）

```
09:18:27  恢复风控状态: peak=67,453.62 consec_loss=1 halted=True     ← 11-12 误熔断遗留
09:21:32  ERROR 数据源=mock（合成行情）：已禁止全部买卖成交
09:22:04  分钟线预热完成：就绪 39 / 无数据 0
09:22:04  实盘券商已连接 (XtQuantTrader)                             ← 文案残留，实际走 bridge
09:22:05  引擎已启动 | 数据源=mock 券商=bridge | 订阅标的=39
09:26:06  TushareQuoteClient: rt_tick 可用（000001.SZ）
09:26:06  行情源已重连至 tushare，退出 mock 模式                       ← mock→tushare 自愈
09:26:09  行情恢复后已重建日线特征（38 只）
09:31:42  guardian 自动拉起（PID 23540）
09:32:32  交易时段开启 | 动态候选池=30只 当前持仓=2 现金=62,323.99 风控=正常
```

同日验收（11-11 的行情限频修复）：
`grep -c 频率超限` = **0**、`grep -c 行情源失效` = **0**、issues 无 `stale`。

> ⚠ 该验收**尚未完成**：引擎 09:32 才重启，而旧缺陷是「开盘 13 分钟必打穿 rt_tick 小时额度」。
> 需 09:45 后再核一次才能定论。

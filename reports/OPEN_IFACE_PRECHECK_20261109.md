# 开盘接口能力预验证与数据准确性对账 — 2026-11-09

> 执行时刻：09:36:24（zone_preflight）／09:36:30（iface_probe）／补充核对至 09:40
> 模式：**live 真实资金**（`exec_mode=live`），**全程只读**：未下单、未撤单、未杀进程、未重启、未改任何配置。
> 环境：`PATH=/c/Users/lisinan/.conda/envs/qmt/{Library/bin,DLLs}`、`PYTHONPATH=.`、解释器 `qmt` conda

---

## 〇、首屏（5 行）

1. **实盘今日可依赖性判定：有条件可信** —— 交易链路（桥/券商/风控/落盘）全绿，行情走 tushare 替代源（真实数据、约 8s 刷新），xtdata 授权不可用属**已知且预期**。
2. **无 P0 执行故障**：`data_mode=tushare`（非 mock）、`broker_connected=true`、`halted=false`、`asset_trusted=true`、两只持仓已回灌并被管理。
3. **P0 是两个头，不是一个**（详见 §四）：①11-06 熔断今日 09:30:10 恢复时 `risk._peak_asset` 被重置 67,453.62 → 58,162.49（`risk/manager.py:352-353`）；②曲线分母 `_peak_equity` **每次 live 重启都归零**（`event_engine.py:565` 初值 0.0、`:4021` 早退跳过 `:4055` 恢复）。二者今日恰好都等于 58,162.49 ⇒ 面板回撤 **−3.21%**，真实峰值回撤 **−16.55%**，−20% 硬线绝对触发位降低 **7,432.91 元**。
4. **七项判定：6 PASS / 1 FAIL（预期内）**；FAIL 项为 `xtdata.get_full_tick → not authenticated`，引擎已降级（实际实时价走**新浪**，非 tushare，见 §五 N6），未影响今日决策；今日 `signals`/`orders`/`fills` 三表均 **0 行**，零成交归因为无信号。
5. **待 OWNER 一句话裁决**：熔断恢复是否应保留 `risk._peak_asset`（改法 A），以及是否修 `_peak_equity` 不随重启归零（改法 B，属 SAFE 但需重启生效）。今日盘中均不做。

---

## 一、两个脚本完整输出原文

### 1.1 `scripts/zone_preflight.py`

```
双功能区开盘自检  2026-11-09 09:36:24
==========================================================================
  ✓ [PASS] 实盘区  端口 5000      在监听
  ✓ [PASS] 观察区  端口 5001      在监听
  ✓ [PASS] 看板   端口 5002      在监听
  ✓ [PASS] 实盘区  快照           tick=86 会话=open
  ✓ [PASS] 实盘区  exec_mode    live
  ✓ [PASS] 实盘区  data_mode    tushare
  ✓ [PASS] 实盘区  broker_mode  bridge（已连接）
  ✓ [PASS] 实盘区  熔断           未熔断
  ✓ [PASS] 实盘区  总资产口径        可信
  ✓ [PASS] 观察区  快照           tick=107 会话=open
  ✓ [PASS] 观察区  exec_mode    paper
  ✓ [PASS] 观察区  data_mode    tushare
  ✓ [PASS] 观察区  broker_mode  paper_no_trade（拒单桩，符合预期）
  ✓ [PASS] 观察区  熔断           未熔断
  ✓ [PASS] 隔离   越界检查         live 候选池末次写入 11-09 08:41:01 与 live 自己的刷新日志对得上 ⇒ 是 live 写的，不是 paper 越界
  ✓ [PASS] 隔离   paper 候选池    独立文件 30 只活跃
  ✓ [PASS] 隔离   DB路径         分离
  ✓ [PASS] 隔离   日志路径         分离
==========================================================================
PASS 18  WARN 0  FAIL 0
```

退出码 **0**。

### 1.2 `scripts/iface_probe.py`

```
C:\Users\lisinan\.conda\envs\qmt\lib\site-packages\xtquant\__init__.py:8: UserWarning: pkg_resources is deprecated as an API. See https://setuptools.pypa.io/en/latest/pkg_resources.html. The pkg_resources package is slated for removal as early as 2025-11-30. Refrain from using this package or pin to Setuptools<81.
  from pkg_resources import get_distribution
xtquant250807.1.2已经发布,前往 http://dict.thinktrader.net/nativeApi/download_xtquant.html 查看更新说明

***** xtdata连接成功 2026-11-09 09:29:36*****
服务信息: None
服务地址: None
数据路径: None
设置xtdata.enable_hello = False可隐藏此消息

开盘接口预验证  2026-11-09 09:36:30  （纯只读）
====================================================================================

[A 行情]
  ✓ PASS 模块导入             xtquant.xtdata 可用
  ✗ FAIL get_full_tick    RuntimeError: func:getFullTick, error:{ "error" : { "error" : "not authenticated" } }（not authenticated ⇒ 行情源不可用，引擎会退到 mock/tushare）
  ! WARN 日线 510050.SH     RuntimeError: func:getTradingDatesByMarket, error:{ "error" : { "error" : "not authenticated" 
  ! WARN 日线 300308.SZ     RuntimeError: func:getTradingDatesByMarket, error:{ "error" : { "error" : "not authenticated" 
  ! WARN 结论               行情接口不可用 ⇒ 引擎会降级；此时任何「按价成交」都不可信

[B 交易]
  ✓ PASS API 面            XtQuantTrader / xttype / Callback 可导入
  ✓ PASS 方法 order_stock   存在
  ✓ PASS 方法 cancel_order_stock 存在
  ✓ PASS 方法 query_stock_asset 存在
  ✓ PASS 方法 query_stock_positions 存在
  ✓ PASS 方法 query_stock_orders 存在
  ✓ PASS broker_qmt_mode  bridge
  ✓ PASS accounts         2 个账号；session_id=None
  ✓ PASS tushare_token    已配置
  ✓ PASS 连通性              引擎自报 broker_mode=bridge connected=True 重连=1
  - SKIP 下单实测             本脚本不下单；passorder/order_stock 只能在引擎连上后于盘中验证

[C 文件桥]
  ✓ PASS 桥目录              C:\pazq_qmt_bridge 存在，条目 7：['boot.log', 'boot.log.1', 'boot.log.20261108-0145-standalone', 'heartbeat.json', 'processed_ids.json', 'result.json', 'signal.json']
  ✓ PASS 策略本体             已部署 46841 字节，mtime 11-08 21:32
  ✓ PASS 心跳               0.2s 前，pid=17080，rev=2026-11-08T21:31-mv-from-positions
  ✓ PASS 代码版本             QMT 在跑 '2026-11-08T21:31-mv-from-positions'，仓库是 '2026-11-08T21:31-mv-from-positions'

[D 数据准确性]
  ✓ PASS asset_trusted    True
  ✓ PASS 账本末快照            2026-11-09T09:36:16.001658 = 56,829.99
  ✓ PASS 引擎 vs 账本         引擎 56,724.49 − 账本 56,829.99 = -0.19%
  ✓ PASS engine_state     cash=11611.49 持仓条目=2 peak_asset=58162.49
  ✓ PASS 引擎持仓 vs 账本       一致：['300308.SZ', '510050.SH']
  ✓ PASS 引擎持仓             2 只：300308.SZ, 510050.SH
  ✓ PASS 白名单定义            exit_exempt=['300308.SZ', '510050.SH'] · manual_entry=['510050.SH']
  ✓ PASS 白名单 510050.SH    exit_exempt=True manual_entry=True
  ✓ PASS 白名单 300308.SZ    exit_exempt=True manual_entry=False

[E 白名单完整性]
  ✓ PASS 磁盘池              30 只活跃 / 全量 375 只
  ✓ PASS 行业合计             {'通信设备': 126, '半导体': 180, 'IT设备': 69} 合计 375 vs n_total 375
  ✓ PASS 内存池对照            内存 30 只 == 磁盘 30 只（顺序也相同）
  ✓ PASS 510050.SH 在池     否（该票靠 manual_entry/exit_exempt 单独处理，不进动量池）
  ✓ PASS 300308.SZ 在池     是
  ✓ PASS 静态宇宙             UNIVERSE=16 STOCK_CODES=12 VALIDATION_UNIVERSE=1

====================================================================================
PASS 30  WARN 3  FAIL 1  SKIP 1

不可依赖项（会直接导致零成交/错数据）：
  · [A 行情] get_full_tick：RuntimeError: func:getFullTick, error:{ "error" : { "error" : "not authenticated" } }（not authenticated ⇒ 行情源不可用，引擎会退到 mock/tushare）
```

退出码 **1**（唯一原因即 A 段 FAIL）。

---

## 二、七件事逐条判定

| # | 检查项 | 判定 | 数字证据 |
|---|---|---|---|
| 1 | live `data_mode` 是否仍为 mock | **PASS** | `data_mode=tushare`（zone_preflight「实盘区 data_mode」；`/api/snapshot.data_mode`）；引擎启动行 `08:41:02 data=tushare broker=bridge exec=live codes=38`（`logs/quant_system.log`）。**不是 mock ⇒ `mock_trade_block_reason()` 不生效** |
| 2 | `broker_connected` | **PASS** | `/api/snapshot.broker_connected=true`、`broker_mode=bridge`、`broker_reconnects=1`；桥 `heartbeat.json` age≈1s（09:39:26 vs ts 1794188366.47）、pid=17080、rev 与仓库一致 |
| 3 | `halted` 是否翻转 | **PASS（已解除）** | `/api/risk.halted=false`、`halt_reason=""`；`engine_state.risk_state={"halted": false, "halt_day": "2026-11-06"}`；日志 `09:30:10,531 RiskManager 熔断自动恢复（冷却 3 日，重置风险基线）` |
| 4 | `asset_trusted` / 引擎与账本一致性 | **PASS（但口径见 P0）** | `asset_trusted=True`（`observability.py:165-166` 判据=basis=="broker"）。**同刻**对比：09:38:17 快照 56,174.49 vs 引擎 56,174.49 ⇒ **偏差 0.00%**（09:36 的 −0.19% 是两次读数相隔 2 分钟的价格漂移，非估值错）。注意：账本末值已不是基线 57,862.57，而是今日盘中值 |
| 5 | 引擎内存持仓是否回灌 | **PASS** | `/api/snapshot.positions=['300308.SZ','510050.SH']`；`engine_state.positions` 两只（510050 500股@2.907、300308 100股@437.65）；桥 `result.json` 券商侧 510050 500股(available 500)、300308 100股(available 100) ⇒ **券商未断链，两只仓受退出逻辑管理**（且二者均在 `exit_exempt_codes`，不会被自动卖） |
| 6 | `xtdata.get_full_tick` / 白名单日线末根 | **FAIL（预期内）+ 日线 PASS** | tick：`not authenticated`（见下方根因）。日线：实测 `strategy/daily_context.py::_fetch_daily` 对两只票均返回 120 根，**末根 close 448.000 / 2.902 = 2026-11-06 收盘**；`_expected_last_trade_date()`=**2026-11-06**（盘中口径期望值）⇒ 日线新鲜，**PASS** |
| 7 | paper 区 5001 是否在运行 | **PASS（仅报告，未启动）** | 5001 在监听，`exec_mode=paper`、`data_mode=tushare`、`broker_mode=paper_no_trade`（拒单桩）、持仓 510050、`total_asset=999,998.32`；看板 5002 亦在监听。**本轮未启动任何进程** |

### 关于第 6 项的两个必须说清的点

**(a) `not authenticated` 是已知约束，不是今日新故障。**
`QMT_QUOTE_SOURCE=auto` 下引擎降级链为 xtdata → tushare；本机 xtdata **网络行情**被大 QMT 2.1.32.0 白名单判 `illegal pid` 拦掉（2026-11-08 已定性并切换 `broker_qmt_mode="bridge"`）。引擎日志实证降级成功：`数据源=tushare（非实时 xtdata）`+`/api/snapshot.degraded=["行情源为 tushare 替代源（数据真实，刷新约 8s）"]`。
**关键澄清**：xtdata 的**本地历史日线**仍可读（`qmt_client.get_history` 正常返回），所以日线上下文没有断供 —— FAIL 只打掉了 tick 级实时推送，引擎用 tushare 快照替代。

**(b)「日线末根更新到今天」这个判据本身不成立。**
盘中（09:30~15:00）当日日线尚未收线，`_expected_last_trade_date()`（`daily_context.py:176-191`）明确返回**上一交易日**。今日期望值就是 **2026-11-06**，实测两只票都是 11-06 ⇒ 新鲜。今日 11-09 的 bar 会在 15:05 后出现。

---

## 三、FAIL 项根因与 OWNER 动作

### F1｜`get_full_tick → not authenticated`（唯一 FAIL）

- **根因**：大 QMT 2.1.32.0 的白名单机制对外部 python 进程判 `illegal pid`，xtquant 的**网络**行情接口一律返回 `not authenticated`。这是 2026-11-08 已定性并采用的架构前提（交易走文件桥、行情走 tushare），**非新引入故障**。
- **实际影响**：tick 级实时推送缺失，引擎改用 tushare 快照（约 8s 刷新）。日线/分钟线特征计算不受影响（本地日线可读）。
- **需要 OWNER 做什么**：**无动作**（维持现状即可）。若确要恢复 tick 级行情，唯一路径是让 QMT 客户端在**策略进程内**（「运行策略」）授权 xtdata，属架构变更，须单独评估，**不在今日盘中范围**。

### F2｜补充：`510050.SH` 的 tushare 日线兜底是死路径

- **根因**：`strategy/daily_context.py:151` 用 `pro.daily` 取日线，而 510050.SH 是 ETF，tushare 该接口对 ETF 返回**空表**（实测根数 0）；数据实际存在于 `pro.fund_daily`（实测 203 根、末根 11-06 close 2.902）。今日由「xtdata 本地日线」兜住了，所以没暴露。
- **风险**：一旦 xtdata 本地数据停更（历史上曾冻结在 2026-08-25），510050 会同时失去本地与 tushare 两条路 ⇒ 日线特征缺失。
- **需要 OWNER 做什么**：**暂不需要**（510050 在 `exit_exempt_codes`，不参与动量/趋势卖出）。若希望修，属 SAFE 工程项（在 `_fetch_daily_tushare` 按代码前缀/类型分流 `fund_daily`），可交给交付工程师下一轮落地。

---

## 四、P0 口径问题：熔断恢复把回撤基线「刷新」了

### 事实链（可逐行复核）

1. 11-06 触发 `daily_stop`（日内 −9.32%），`risk_state.halt_day=2026-11-06`。
2. 11-07/11-08 为周六/周日，冷却按自然日计 3 日。
3. 今日 `09:30:10,531` 自动解除：`RiskManager 熔断自动恢复（冷却 3 日，重置风险基线）`。
4. 解除时执行 `risk/manager.py:352-353`：
   ```python
   if total_asset and total_asset > 0:
       self._peak_asset = total_asset   # 重置基线，避免解除后立即再熔断
   ```
   当时 `total_asset=58,162.49` ⇒ `peak_asset` 从 **67,453.62 → 58,162.49**。
5. 今日 09:38:17 快照 `drawdown_pct=-0.0342`（−3.42%），即**从新 peak 起算**。

### 两套口径对照（总资产按 09:39 的 56,292.99；09:38:17 为 56,174.49，差 0.2%，结论不变）

| 口径 | peak | 当前回撤 | 距 −20% 剩余 | −20% 触发的资产位 |
|---|---|---|---|---|
| 引擎在用（新 peak） | 58,162.49 | **−3.21%** | 16.79 pp | 46,529.99 |
| 真实历史峰值 | 67,453.62 | **−16.55%** | **3.45 pp** | **53,962.90** |

（若按 09:38:17 的 56,174.49：−3.42% / −16.72%，剩余 16.58 pp / 3.28 pp，硬线位不变。）

**差额：7,432.91 元**（53,962.90 − 46,529.99）。也就是说，若账户继续下跌，`max_drawdown_pct=-0.20`（`settings.py:924`）这道最后防线**不会在真实亏损 20% 时触发，而会等到真实亏损约 31%**。

### 为什么这条必须报（而不是当「设计如此」放过）

同一份代码里有**两个相反设计**，说明项目自己已经判断过这个取舍：

- `risk/manager.py:352-353`（熔断恢复）：**重置** peak，注释「避免解除后立即再熔断」。
- `risk/manager.py:376`（僵尸解封）：**不重置** peak，注释「保留真实回撤基线，**max_drawdown 保护不弱化**」。

即：僵尸解封路径为了不弱化 max_drawdown 刻意不重置，而日亏熔断恢复路径却重置了 —— 后者把 max_drawdown 的基线一并抹掉，属**副作用超出原意**。

另需注意：`asset_trusted=True` 与「peak 被重置」**不矛盾**。`observability.py:165-166` 只校验当期 `total_asset` 的取值口径（basis=="broker"），**完全不校验 peak 的历史连续性**。所以「口径可信」不代表「回撤可信」。

### 需要 OWNER 做什么（一条可执行动作）

**裁决口径，二选一**（两者都需改代码 + 重启，今日盘中不做）：

- **A（保守，推荐）**：把 `:352-353` 改为只重置 `_consec_loss` / `_daily_pnl`，**保留 `_peak_asset`** —— 与 `:376` 的僵尸解封口径统一。代价：解除后若价格仍在低位，可能因 max_drawdown 再次熔断（即 `dd_recover_days=3` 才完全恢复）。
- **B（维持现状）**：接受「熔断恢复即刷新回撤基线」，但须在面板上**同时显示两个回撤**（对 peak_asset / 对历史最高峰值），避免 OWNER 只看到 −3.21% 而误以为安全垫充足。

### 4.2 第二个分母：`_peak_equity` 每次 live 重启归零（P0，比 4.1 更常触发）

`equity_snapshots.drawdown_pct`（盘后复盘与 KPI 读的就是它）**不是**用 `risk._peak_asset` 算的，而是用引擎自己的 `_peak_equity`：

| 位置 | 代码 | 后果 |
|---|---|---|
| `event_engine.py:565` | `self._peak_equity: float = 0.0` | 初值 0 |
| `event_engine.py:3944-3949` | `self._peak_equity = max(self._peak_equity, total_asset)`；`dd=(total−peak)/peak` | 分母随首 tick 起算 |
| `event_engine.py:4021-4032` | `if not restore_ledger:` → 恢复风控字段后 **`return`** | **早退，跳过 `:4055` 的 `_peak_equity` 恢复与 `:4056` 的 `_sanitize_restored_peaks()`** |

live 引擎以 `restore_ledger=False` 启动（账本以券商为权威），因此**每一次 live 重启，`_peak_equity` 都从 0.0 重新开始，第一次落盘即等于当日首个 tick 的总资产**。今日实证：`engine_state.peak_equity = day_open_asset = 58,162.49`（09:30 首 tick），三者完全相等。

**这条解决了工作记忆里一个「未定性」的悬案**：11-06 表内回撤 −10.48% 与按真实峰值算的 −14.22% 相差 3.74pp —— 原因是表内分母用的是「**11-05 会话内高点≈64,635.7**」（11-05 进程重启后重新起算的 peak），而非 67,453.62。同理今日：表内 `drawdown_pct=-0.0321`，真实 −16.55%。

> **口径出处结论**：`drawdown_pct`（快照表）＝对「本次进程运行期内的最高总资产」；`risk._peak_asset`（风控）＝对「DB 继承峰值，但会被熔断恢复重置」。**两者都不是对 OWNER 真实历史峰值**，且今日二者巧合相等 58,162.49，所以看不出分歧 —— 这正是它比 4.1 更危险的地方：**不熔断的日子也在悄悄刷新分母**。

**修法 B（SAFE，不碰交易参数）**：在 `:4021` 的早退分支里补一行 `self._peak_equity = max(_credible_peak_equity(), 0.0)`，或让 `_persist_equity` 改用 `max(_peak_equity, risk._peak_asset)`。需重启生效（归 OWNER）。

---

## 五、其它观察（不阻塞今日）

- **N1｜`open_date=null` 仍在**：`engine_state.positions` 两只均 `open_date: null`，DB 层无回灌（历史缺陷）。今日未重启引擎 ⇒ 进程内 `_pos_meta` 的 T+1 与 30 分钟保护期对**新仓**仍生效；这两只老仓因豁免本就不自动卖。
- **N2｜iface_probe 的「引擎 vs 账本」比对存在时间差伪差**：D 段用「引擎当前值」比「最近一条快照」，而快照间隔约 60s。今日 09:36 报 −0.19%，09:38 同刻复算 0.00%。盘中波动大时可能误报 >0.5%。建议脚本改为「按 ts 取同一分钟」或放宽到 2 分钟窗口（SAFE）。
- **N3｜零委托归因为「无信号」，非接口故障**：`09:30:10 无入场信号：动量闸门后候选池为空（min_mom=0.15 lookback=60 top_n=3）| 漏斗分层: 宇宙 37 只, trend_up 3, bias>=2.00 0, score>=4.0且正因子>=3 8 只`。09:30~09:40 **零委托、零拒单、零 market_stale**。
- **N4｜`benchmark.account_pct=-0.9421`（basis=initial_cash）** 仍按初值 100 万计算，属已知展示口径缺陷（`/api/snapshot.benchmark`），不参与风控判定。
- **N5｜桥的 frozen 未计入总资产**（11-09 早间已登记 P0）：当前 `frozen=0` 无差；一旦挂单未成交会低估总资产 ⇒ 假回撤。今日尚未挂单，暂无影响。
- **N6｜实时价其实来自「新浪」，但代码与面板都写「tushare」**（`core/tushare_quote.py` 内 `realtime_quote`）：今日 tushare 的 `rt_tick` **无权限**，引擎自动走新浪实时报价（约 8s、只有最新价、无五档）。后果：①面板与开屏告警把数据源标成 tushare，标签失实；②`event_engine.py:1377-1379` 仍写「行情可能为模拟/回退数据，仅用于自检」——**该告警对真实行情是假警报**（历史 P2 再次复现）。属文案/可观测性问题，不影响撮合价（本策略市价单为主）。
- **N7｜代码与进程版本匹配，无「改了没重启」**：引擎 pid 17488 起于 `08:40:27`，`event_engine.py` mtime `11-08 21:32`、`risk/manager.py` mtime `11-08 22:12` 均早于启动时刻 ⇒ 生效。桥侧 `XtItClient.exe` pid **17080 于 08:52:52 启动过一次**（对应 `broker_reconnects=1`），属早间自愈，`rev` 与仓库一致，无需处理。
- **N8｜今日三张交易表全空**：`signals` / `orders` / `fills` 均为 **0 行**（今日），与日志「无入场信号」互证 —— 不是「下了单没记」，而是根本没下单。
- **N9｜账本基线数字修正**：11-06 收盘应为 **57,862.57**、现金 **11,611.57**（`equity_snapshots` 11-06 末行），工作记忆里记的 57,862.49 / 11,611.49 差 0.08（佣金舍入）。今日 `cash` 仍为 11,611.49（引擎口径）。

---

## 六、留痕

- 本轮未做：未启停任何进程、未改配置、未动 DB、未下单；`reports/OWNER_INBOX.md` 仅追加、未改动已有行。
- 取证分工：主理人独立实测（两脚本 + SQL + 代码复核）＋ `live-ops-engineer`（行情源实证、进程版本）＋ `data-ledger-auditor`（`_peak_equity` 早退路径、两套分母定性与回撤复算，三个数字与主理人完全一致）。
- 下一轮建议唯一动作：修 `_peak_equity` 不随重启归零（§4.2 修法 B，SAFE，需重启生效）；顺带把 `iface_probe.py` 的「引擎 vs 账本」改为同刻对齐（N2）。
- 需 OWNER 一句话裁决：**熔断恢复是否保留 `risk._peak_asset`（A）／是否修 `_peak_equity` 分母（B）**。两者都是回撤口径，建议一并裁。
- 已知未修、但需 OWNER 知情的复发项：`open_date=null`（N1）、行情源标签与假警报（N6）、桥 `frozen` 未计入（N5）。

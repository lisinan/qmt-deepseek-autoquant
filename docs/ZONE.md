# 双功能区（zone）：live 实盘区 / paper 观察区

> 2026-11-08 建立。OWNER 决定：项目分成两个功能区，**两个实例可同时在跑**，
> 功能互相隔离。paper 区要真实行情，但绝不允许下真实单。

---

## 一、两个区各自是干什么的

| | **live 区** | **paper 区** |
|---|---|---|
| 定位 | 唯一交易区，真实资金 | **AI 产业链热点/行业观察台** |
| 起始资金 | 券商实际（当前 5.78 万） | **100 万**（`INITIAL_CASH`） |
| 目的 | 执行策略、承担盈亏 | 看信号层跑得对不对、哪条细分链在走强、哪些标的在出信号 |
| 下单 | 真实下单 | **结构上不可能**（三层阻断，见 §四） |
| 行情 | xtdata → tushare → mock | 同一条链（刻意不设 mock：观察台必须在真实价上评估） |

### 为什么 paper 用 100 万反而合理

live 的真实行为被一条硬约束主导：**一手 100 股门槛**。5.78 万现金下 41 只候选
只有 13 只买得起一手，这决定了候选池、决定了实际持仓数、决定了暴露。

paper 在 100 万下**根本遇不到这个约束**，所以它能把 AI 链条上的标的都跑起来
—— 这正是「观察台」要的。

**代价（必须记住）**：paper 验证的是**信号层 / 逻辑层**，不是 live 的资金可执行性。
绝对收益、持仓数量、暴露比例都**不可与 live 横比**，只能看比率的形状。

---

## 二、两个区的全部隔离点

| 项 | live | paper | 由谁决定 |
|---|---|---|---|
| 账本 DB | `storage/qmt.db` | `storage_paper/qmt.db` | `core/zone.py` → `QMT_DB` |
| 日志 | `logs/` | `logs_paper/` | `main.py` 注入 `config.settings.LOG_DIR` |
| 系统提示 | `logs/notices.log` | `logs_paper/notices.log` | `QMT_NOTICES_LOG` |
| 单例 pid | `logs/engine.pid` | `logs_paper/engine.pid` | `LOG_DIR` |
| 停止哨兵 | `logs/STOP_ENGINE` | `logs_paper/STOP_ENGINE` | `LOG_DIR` |
| Web 端口 | 5000 | 5001 | `--port` 默认随 zone 走 |
| 交易通道 | 按 `trading_config.json` | `_NoTradeBroker` 拒单桩 | `QMT_ZONE` |
| 复位标志 | `storage/.reset_paper.flag` | `storage_paper/.reset_paper.flag` | 由 DB 路径推导 |

`engine_state` 是 `CHECK(id=1)` 单行表，**天然只能靠分库隔离**。

### 为什么必须分库（资金安全级，不是洁癖）

`engine_state` 是单行表，没有 mode 列。若共用库：

```
paper 的 peak_asset 量级        ≈ 1,000,000
live 真值 total_asset           =    57,862.57
live 下次重启在 event_engine.py:3943 读到 peak_asset = 1,000,000
⇒ drawdown = (57,862.57 − 1,000,000) / 1,000,000 = −94.21%
```

`max_drawdown_pct = −0.20`，**−94.21% 直接击穿** ⇒ 熔断 + 强平真实持仓。
而且 live 的恢复分支在 `event_engine.py:3969` 提前 `return`，**跳过了
`:3995` 的 `_sanitize_restored_peaks()`** —— 没有任何夹紧保护。

这正是 2026-10-29 切 live 时踩过的同一个机制（当时要手工重置 `peak_asset`
并归档删除 paper 权益快照，否则首轮 −93% 回撤直接熔断卖掉真实持仓）。

---

## 三、怎么起、怎么停

### 启动

```bat
:: live（原有方式不变；run_live.bat 不带 --zone，缺省就是 live）
cmd /c C:\Users\lisinan\Desktop\qmtIDE-deepseek\scripts\run_live.bat

:: paper（新增）
cmd /c C:\Users\lisinan\Desktop\qmtIDE-deepseek\scripts\run_paper.bat
```

`run_paper.bat` 只设 `QMT_ZONE=paper` 与 `QMT_QUOTE_SOURCE=auto`，其余路径全部由
`core/zone.py` 推导 —— **单一事实源，不在 bat 里重复写 `QMT_DB`**，避免两处漂移。

**建议错峰**：首次预热 46 只 1m 历史约 7.5 分钟，两个实例同时预热会双倍 IO。
建议 live 先起、预热完再起 paper。

### 停止（只停一个，不互相影响）

```bat
:: 停 paper
python.exe main.py --zone paper --stop

:: 停 live
python.exe main.py --stop
```

哨兵与 pid 都分别在各自的 zone 目录下，`--stop` 不会误杀另一个区。

---

## 四、paper 为什么不可能下真实单（三层，互相独立）

1. **构造期换桩**：`core/broker.py::_make_broker()` 见到 `QMT_ZONE=paper`
   直接返回 `_NoTradeBroker`，**连 `xtquant.xttrader` 都不 import**，也不会构造
   `BridgeBroker`（不往 bridge root 写 `signal.json`）。该函数在模块导入期执行一次
   （`qmt_broker = _make_broker()`），此后全进程共用 ⇒ 单点定型、无法中途翻案。
2. **起动即拒**：`main.py` 在 `zone=paper` 下拒绝 `--mode live` 与 `--init-positions`，
   并在缺省时**强制 `mode=paper`**（所以 `--zone paper` 单独用也安全）。
3. **调用点本来就 live-gated**：`exec_mode != "live"` 时
   `event_engine.py:3532`（BUY）/ `:3733`（SELL）的 `qmt_broker.place_order` 不执行，
   走 `:3571` / `:3761` 的本地记账分支。

桩还有两条硬约束，都能对应到真实事故口径：

- 查询一律返回 **`None`** 而**不是 `[]`**。`[]` 在 `_sync_broker_positions` 里表示
  「券商确认空仓」，会清空本地账本（2026-11-02 事故口径）；`None` 才表示「本轮不可用」。
- `place_order` 永远 `ok=False` 且 `logger.error` 留证，可审计。

---

## 五、实现要点（改这块代码前必读）

### 5.1 zone 注入必须早于 engine 导入

`engine/event_engine.py:38` 是模块级 `from config.settings import ... LOG_DIR`；
`STOP_SENTINEL`（`:1419`）是**类属性、导入时求值**；`web/routes.py:129` 也是
`from config.settings import LOG_DIR`。

⇒ 只有在 `from engine.event_engine import EventEngine` **之前**改写
`config.settings.LOG_DIR`，才能让一个进程内的日志/哨兵/pid 整体落到 zone 目录。

`tests/test_zone_isolation.py::test_main_applies_zone_before_importing_engine`
用源码顺序把这个不变量钉住 —— 一旦有人把注入挪到 import 之后，代码照样能跑、
测试照样绿，但 paper 的日志/哨兵会静默落回 live 目录。

### 5.2 `_singleton_pid_path` 必须走 LOG_DIR（改错了会打死实盘）

`engine/event_engine.py:1436` 原实现硬编码 `BASE_DIR/"logs"/"engine.pid"`，
与 `main.py:78 PID_FILE` 是**同一事实的两份实现**。双实例下两种错配：

| 改法 | 后果 |
|---|---|
| 只改 `main.PID_FILE` | paper 每轮读到 live 的 pid ⇒ 判定「我已非持有者」⇒ **paper 自己退出** |
| 只改 `_singleton_pid_path` | paper 把自己的 pid 写进 `logs/engine.pid` ⇒ **live 下一轮主动退出（实盘自杀）** |
| 两处都改 | 成立 |

### 5.3 复位标志由 DB 路径推导，不是 LOG_DIR

`_apply_pending_reset` 原用 `Path(LOG_DIR).parent / "storage"`，隐含假设
「LOG_DIR 的父目录就是 BASE_DIR」。双实例下这会把 paper 的复位标志写进 **live 的**
storage，而同一函数后半段的 `clear_equity_before()` 会**删掉 live 的权益快照**。
已改为 `Path(self.storage.db_path).parent`（live 下逐位相同）。

### 5.4 `bridge_root` 无法按实例隔离（已知限制）

`QMT_BRIDGE_ROOT` 全仓唯一读取点是 `qmt_bridge/qmtide_bridge.py:77`（**QMT 进程内部**），
引擎侧零引用。引擎侧读的是 `core/broker_bridge.py:94` 的 `cfg.get("bridge_root")`
← `TRADING_CONFIG_FILE = BASE_DIR/"trading_config.json"`（`settings.py:110` 硬编码、无 env 覆盖）。

⇒ 若两个实例都进 `broker_qmt_mode="bridge"`，**必然共用同一个 `signal.json`，无法隔离**。
paper 靠 `_NoTradeBroker` 不从这条路过，所以当前不构成风险；将来若要让 paper 也走桥接，
必须先给 `bridge_root` 加 per-instance 覆盖。

---

## 六、统一看板（一个界面看两个区）

`scripts/zone_dashboard.py` —— **独立进程、纯只读**，默认 `127.0.0.1:5002`。

```bash
python scripts/zone_dashboard.py                 # 起服务，浏览器开 5002
python scripts/zone_dashboard.py --once          # 只打一次聚合 JSON（自检）
python scripts/zone_dashboard.py --port 5010     # 换端口
```

### 为什么不改主 Web

主 Web（`web/app.py`）是「单引擎绑定」结构：`create_app(engine)` 把一个
`EventEngine` 塞进 `app.config`，所有路由从它取数。要在同一页面看两个区，必须改
`web/routes.py` 加跨区取数 —— 而 **live 正在跑，任何 web 层改动都要重启 live 才生效**。

所以看板做成独立进程：**不 import 任何项目模块**（不碰 `config.settings`、不碰
`core.broker`），对两个引擎零影响；数据只有两条只读路径：

1. HTTP 取两边的 `/api/snapshot`（`SNAP_TIMEOUT=3s`）；
2. `sqlite3` 以 **`mode=ro`** 只读打开两个库，取 `equity_snapshots` 与今日
   `orders`/`fills` 计数。

任一区挂掉，页面显示「区域离线」+ 错误串，**不会白屏**（两个区一起看不见是最坏结果）。

### 归一化：只比形状，不比金额

paper 起始 100 万、live 真实 5.8 万，差约 17 倍。绝对金额同图，live 会被压成贴地线。
所以权益曲线统一**以窗口首点 = 100 归一**（`total_asset / base * 100`），页面有常驻
提示说明这一点。首点 ≤ 0 时不出点（避免除零 / 符号翻转成反向曲线）。

其余为**逐项对照表**：取值不同则整行高亮 + `≠` 标记 —— 这正是「两个区基础设施同构、
但身份/通道/资金不同」最直观的呈现。

### 页面看什么

| 区块 | 内容 |
|---|---|
| 区卡片 ×2 | 状态点 / `exec_mode` / `data_mode` / `broker_mode` / 总资产 / 现金 / 持仓数 / `halted` pill / 今日委托·成交 / 漏斗与 `no_entry_reason` |
| 权益曲线 | 双线归一化，带 100 基准虚线，5s 轮询 `/api/zones` |
| 对照表 | 逐项并列，差异行高亮 |

### 守卫

`tests/test_zone_dashboard.py`（16 例）钉死：`ZONES` 的端口/库路径与
`core.zone.zone_config()` **逐项相等且互不相交**（防「安静地展示另一个区的数据」）、
sqlite 必须 `mode=ro` 且源码无写语句、归一化口径、单区离线不抛、看板不得 import
任何项目模块。**该模块必须在 `tests/run_all.py::_collect_tests()` 注册**，
否则整文件隐形不执行。

---

## 七、共享可写文件的隔离（2026-11-08 修复的越界）

分库只解决了 `engine_state`。**缓存文件此前仍与 zone 无关**，paper 会直接写 live 的文件。

### 实证（不是推断）

```
storage/dynamic_universe.json   mtime = 2026-11-08 20:09:43.394506
logs_paper/quant_system.log     2026-11-08 20:09:43,394   DynamicUniverse 刷新完成
```

毫秒级吻合 ⇒ **paper 覆写了 live 的候选池文件**。`active_pool` 决定 live 评估/买入
哪些票，而 paper 走 tushare、live 走 xtdata，两边池子本可以不同；一次 paper 刷新
失败写入半截 JSON，live 重启就读到空池/残池 —— 而日志里**不会报任何错**。

### 五个共享路径与现在的隔离方式

| 模块 | 改造前（live 原值，必须逐位不变） | paper 现在 |
|---|---|---|
| `data/dynamic_universe.py` | `BASE_DIR/storage/dynamic_universe.json` | `storage_paper/dynamic_universe.json` |
| `data/hist_cache.py` | `ROOT/storage/hist_cache` | `storage_paper/hist_cache` |
| `data/earnrev_cache.py` | `BASE_DIR/data/cache` | `storage_paper/data_cache` |
| `data/moneyflow_cache.py` | `BASE_DIR/data/cache` | `storage_paper/data_cache` |
| `data/northbound_cache.py` | `BASE_DIR/data/cache` | `storage_paper/data_cache` |

全部走 `core/zone.py` 的 `zone_storage_dir()` / `zone_data_cache_dir()`。
**live 分支返回值与改造前逐位相等**，由 `tests/test_zone_shared_paths.py` 的 7 个用例
钉死（不是靠 review，是靠断言）。

⚠ `northbound_hsgt.json` 是**单文件**，两进程同写会读到半截 JSON —— 这条最隐蔽。

### paper 需要自己的候选池种子

隔离之后 paper 不再蹭 live 的池子，首次启动会因「Tushare 返回空」而
`动态候选池: 0 只活跃`，**信号层直接空转**（面板上看却是「运行中」）。
`refresh()` 失败时**不清空已有池**（只在成功时赋值），所以只要有一次有效缓存就能顶住。

2026-11-08 已做一次性迁移：`storage/dynamic_universe.json` → `storage_paper/dynamic_universe.json`
（读 live、写 paper，live 文件 mtime 不变）。之后 paper 刷新成功会用自己的数据覆盖。

### 开盘自检

```bash
python scripts/zone_preflight.py           # 全量
python scripts/zone_preflight.py --quiet   # 只看问题
```

纯只读（HTTP GET + `mode=ro` + `stat`），退出码 0/1。它把「能不能干活」拆成可判定断言：
`data_mode=mock`、`broker=disconnected`、`halted`、`asset_trusted=false`、候选池 0 只、
漏斗 `universe_n=0` 全部会被点名 —— 这些都是**单独看不出问题、合起来就是零成交**的字段。

其中一条是运行时越界断言：`storage/dynamic_universe.json` 的 mtime 必须**早于**
`logs_paper/engine.pid` 的 mtime，否则说明 paper 启动后又写过 live 的候选池。

---

## 八、验收清单

```
1. curl 127.0.0.1:5001/api/snapshot  ⇒ exec_mode=paper / broker_mode=paper_no_trade
2. curl 127.0.0.1:5000/api/snapshot  ⇒ exec_mode=live（不受影响）
3. storage_paper/qmt.db 存在；storage/qmt.db 的 mtime 不变
4. logs_paper/quant_system.log 在增长；logs/quant_system.log 只反映 live
5. logs_paper/engine.pid 的 pid ≠ logs/engine.pid 的 pid
6. python tests/run_all.py 全绿（除 xtdata 环境类失败）
7. python scripts/zone_dashboard.py --once ⇒ 两区都 online，且 live/paper 的
   port 与 db 与 core/zone.py 一致（tests/test_zone_dashboard.py 已钉住）
```

---

## 九、回滚

代码层：`git revert` zone 相关 commit。live 分支零赋值，顺序无关。

运行层：**不带 `--zone`** 启动即回到改造前行为（zone 缺省 live），
然后删掉 `storage_paper/` 与 `logs_paper/` 两个目录即可。**不会影响 live 账本。**

---

## 十、已知未决

| # | 内容 | 影响 |
|---|---|---|
| 1 | xtdata 双进程同时订阅未实测 | 若冲突，paper 自动退到 tushare（仍真实行情，代价是抢 live 的 token 频次） |
| 2 | `scripts/reset_paper_account.py:31-32` 硬编码生产库与 `logs/engine.pid` | **不要对 paper 执行该脚本**，等它加 `--db/--pid` |
| 3 | `health_check.py` 只探 5000、只拉起无参 `main.py --web` | guardian 看不见 paper。**不要给 paper 配 guardian**，否则会被拉成 live |
| 4 | `ensure_engine()` 无条件 `return True`（`health_check.py:293`） | 「尝试启动」被当成「启动成功」，属既有缺陷 |
| 5 | `_credible_peak_equity()` 与 `equity_snapshots` 的 `MAX(ts)` 无 mode 过滤 | 分库后不再互相污染，同库仍会 |

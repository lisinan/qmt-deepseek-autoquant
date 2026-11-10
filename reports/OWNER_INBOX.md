# OWNER INBOX（发现路由总入口）

> 规则见 `docs/AUTOMATION_CONTRACT.md` §四.1。**任何自动化产出的发现都必须在这里留一行**，
> 否则等于结论躺在 `reports/` 里没人消费（2026-10-30 的盯市价退化就是这么漏掉的）。
>
> 格式：`- [P0|P1|P2] [SAFE|FORBIDDEN|INFO] <一句话结论> — <证据>:<位置> — 发现于 <任务> <日期>`
>
> 语义：
> - **SAFE** = 纯工程/正确性/观测修复，可自动落地（改完 `tests/run_all.py` 必须全绿）
> - **FORBIDDEN** = 改策略参数 / 改 EXECUTION_MODE / 需重启 / 真实资金操作 ⇒ **只报，不执行**
> - **INFO** = 观察项
> - 状态后缀：`[已修 <commit>]` / `[已裁决]` / `[待 OWNER]` / `[跟踪中]`
>
> **17:00 工程维护**是当日汇总去重负责人：读当日全部报告 → 合并本文件 → SAFE 项当场修 →
> FORBIDDEN 项在输出里点名。**P0 必须出现在当日输出首屏。**

---

## OPEN（按优先级）

- [P0] [INFO] **live 引擎当前看不见真实持仓，账上凭空 100 万**（2026-11-08 19:50）
  — `GET /api/snapshot` 实读 `total_asset=1000000.0 / cash=1000000.0 / positions={} /
  exec_mode=live / data_mode=mock / broker_mode=disconnected`，同时
  `risk.peak_asset=67453.62`、`risk.asset_trusted=false`、`risk.asset_basis=unknown`。
  成因：live 不恢复本地账本（`event_engine.py:568-569` 传 `restore_ledger=False`），
  `self._cash = INITIAL_CASH`（`:450`）之后再没被改写；券商断链时 `_total_asset()`
  回落到本地拼装（`:4612-4615`）。**唯一护栏是 `asset_trusted=false` 那道自检**，
  以及 `:2132 risk_asset is None ⇒ 跳过判定/不落盘`。
  **今天没出事，是因为 `data_mode=mock` 让 mock 闸门拦住了买卖，不是因为它不想买。**
  — 发现于 双实例可行性核查（主理人亲测）2026-11-08 — `[待 OWNER]`

- [P0] [INFO] **live 引擎今日空转 10 小时，零交易能力**（2026-11-08 19:50）
  — pid 18256 `python.exe main.py --web`，启动 `2026-11-08 09:39:42`，
  **无 `--mode`、无 `--init-positions`** ⇒ 由 guardian 拉起而非 `run_live.bat`。
  `XtMiniQmt.exe` **不在进程表**（只有 `XtItClient.exe` pid 3688）；日志
  `Engine 启动: mode=single data=mock broker=disconnected exec=live codes=38`，
  `XtQuantTrader.connect rc=-1` 第 580 次重连失败；`halted=true halt_reason="daily_stop -9.32%"`。
  ⇒ **附带结论：live 靠 guardian 自愈重启会常态丢掉 `--init-positions`**。
  — 发现于 双实例可行性核查 2026-11-08 — `[待 OWNER]`

- [P0] [FORBIDDEN] **桥接已武装待命，但 `broker_qmt_mode` 仍是 `XtMiniQmt`**
  （2026-11-08 19:50）
  — `trading_config.json` mtime **2026-08-24 23:35**（从没改过），`broker_qmt_mode="XtMiniQmt"`
  ⇒ `core/broker.py:613-622` 走 QMTBroker(xttrader) ⇒ 必 rc=-1；
  而 `C:\pazq_qmt_bridge\boot.log` 显示 QMT 内已 `MODE=embedded init() called
  (passorder available)`、account 已注入、心跳新鲜 ⇒ **真实资金通道是「武装待命」状态**。
  ⇒ 今天唯一阻止它动真钱的是 `data_mode=mock`。
  **切 `"bridge"` 需 OWNER 执行 + 重启**（改完重启窗口内 live 无监控）。
  — 发现于 双实例可行性核查 2026-11-08 — `[待 OWNER]`

- [P1] [FORBIDDEN] **`_shutdown()` 会把内存里的 100 万写进 `engine_state`**
  （2026-11-08 19:55）
  — `engine/event_engine.py:4707-4712` 无条件 `_save_engine_state()`，而当前 live 进程
  内存里是 `cash=1,000,000 / positions=[]`。`peak_asset` 仍是 67,453.62 故不会假熔断，
  但账本字段会被污染，之后任何 paper 若误连该库会继承它。
  ⇒ **在 `QMT_DB` 分库 + 该缺陷修好之前，不要让当前 live 进程优雅退出。**
  — 发现于 双实例可行性核查 2026-11-08 — `[待 OWNER]`

- [P1] [SAFE] **zone 双实例隔离已落地并通过全量回归**（2026-11-08 20:00）
  — `core/zone.py`（新）+ `main.py --zone` 注入（必须早于 engine 导入，因为
  `event_engine.py:38` 是模块级 `from config.settings import LOG_DIR`、
  `STOP_SENTINEL:1419` 是类属性导入时求值）+ `core/broker.py::_NoTradeBroker` 拒单桩
  + `engine/event_engine.py:1436 _singleton_pid_path` 改走 `LOG_DIR`（原硬编码
  `BASE_DIR/"logs"`，与 `main.PID_FILE` 是同一事实两份实现，不同步会导致**实盘自杀**）
  + `:630` 复位标志改由 DB 路径推导（原 `Path(LOG_DIR).parent` 会落到 live 的 storage，
  且同函数 `clear_equity_before()` 会删掉 live 的快照）+ `logs_paper/ storage_paper/` 忽略
  + `scripts/run_paper.bat` + `tests/test_zone_isolation.py`（12 例，已注册 run_all）。
  实测：`--zone paper --snapshot` ⇒ `exec_mode=paper / broker_mode=paper_no_trade /
  cash=1000000 / data_mode=tushare`，而 `storage/qmt.db` 与 `logs/engine.pid` mtime
  **一秒未动**、`logs/` 文件数不变（174）、live 5000 端口正常。
  — 发现于 双实例改造 2026-11-08 — `[待重启生效]`

- [P0] [SAFE] **「本机发信号 → QMT 执行 → 回结果」闭环已跑通**（2026-11-08 02:12）
  — 判定线三条全部命中，`boot.log` 原文：
  `[BRIDGE] poll #1 alive` / `[BRIDGE] file reader=ctypes 可用（上一次=<未定>）` /
  `[BRIDGE] got signal_id=144133bb... action=PING reader=ctypes`，
  `result.json` = `{"status":"ok","data":{"pong":...}}`；`scripts/bridge_probe.py`
  四连请求 **4 ok / 0 timeout / 0 error**（PING / DUMP / QUERY_ASSET / QUERY_POSITION）。
  **之前卡住的真因不是读链**：`02:09:13` 那轮 `_io_selftest()` 里的 socket 探针
  两个阻塞 connect 各 0.5s，用户恰在那 1 秒内点「停止」⇒ `init` 被
  `KeyboardInterrupt` 打断、**从未返回** ⇒ 已注册的 `run_time` 定时器永不调度 ⇒
  `bridge_poll` 一次都没跑。日志铁证：该轮 selftest 打完 7 个路径就断、**连 socket
  行都没有**，而 02:04 那轮（旧非阻塞探针、秒回）有。修法：自测移入后台 daemon
  线程（诊断不是启动前置）+ 探针压到单端口 0.3s + 补 `poll #N alive` /
  `got signal_id=` 两条观测日志（缺它们才把「poll 没跑」误读成「读不到」）。
  — 证据：`C:\pazq_qmt_bridge\boot.log` / `result.json`；`qmt_bridge/qmtide_bridge.py:757,895`
- [P0] [FORBIDDEN] **信号通道两个会「静默吞单 / 重复下单」的缺陷（已修，待重启生效）**
  - **① 共享冲突吞掉整条信号**：`_read_bytes_ctypes` 只申请 `FILE_SHARE_READ`，
    QMT 侧一持有 `signal.json` 句柄，本机 `os.replace` 就撞
    `ERROR_SHARING_VIOLATION`，`BrokerBroker._request` 直接计 error 并
    **return None 丢掉整条信号**（若为 SELL = 静默漏卖）。
    2026-11-08 实测 **8 次请求里 1 次**触发，而 `bridge_probe` 把它误报成
    「DUMP 超时」（`dump()` 返回 None 有超时/写失败两条路径，文案只写超时）
    ⇒ 白查了一轮。修法：掩码改 `FILE_SHARE_READ|WRITE|DELETE`（`SHARE_ALL=0x7`）；
    引擎侧 `_atomic_write_json` 对 replace 冲突重试 8 次（重写同一 signal_id，
    桥接按 id 去重，不会重复下单）作纵深防御；探针文案按 stats 分流。
    钉住：`tests/test_bridge_broker.py::test_ctypes_reader_shares_write_and_delete` /
    `::test_atomic_write_retries_on_sharing_violation`
  - **② QMT 重启会重放上一条信号**：`_processed` 只在内存，而 `signal.json` 长期
    残留最后一条 ⇒ 重启后**再执行一遍**。2026-11-08 02:16 实证：重启后把上一轮
    的 `QUERY_POSITION` 又跑了一次。查询无害，**但若那条是已成交的 BUY 就是重复
    买入真实资金**。修法：已成功处理的 id 落盘 `processed_ids.json`，`init` 时
    恢复，命中即回 `status="duplicate"` 且**不执行**；**失败信号不入黑名单**
    （允许重启后重试）。钉住：`tests/test_qmtide_bridge.py::TestCrossRestartIdempotency`
    三例（含「失败可重试」）。
  — 证据：`qmt_bridge/qmtide_bridge.py::_read_bytes_ctypes/_load_seen/bridge_poll`；
  `core/broker_bridge.py::_atomic_write_json`；`scripts/bridge_probe.py`
- [P0] [FORBIDDEN] **bridge 的 `total_asset` 虚增 2.37 倍，切 `broker_qmt_mode="bridge"`
  前必须确认已修 —— 否则 `max_drawdown` 熔断永久失效**（2026-11-08 02:20 已修，待重启生效）
  — 实测：`QUERY_ASSET` 返 `total_asset=137,321.50`（取 `m_dTotalAsset` 不存在
  ⇒ 静默回落 `m_dBalance`），而同一快照 `m_dAvailable`(11,611.49) +
  `m_dInstrumentValue`(46,251.00) = **57,862.49**；引擎 xtquant 直连 11-06 收盘读到
  **57,862.57**，差 0.08（佣金精度）⇒ `m_dBalance` 是坏值，137,321.50 用任何字段
  组合都拼不出来。
  **危害链**：`engine/event_engine.py:4612` 在 live 模式下**优先采用券商
  `total_asset`**（`_broker_total_asset`，2026-10-30 P0 修复引入）⇒ 峰值虚高 2.37 倍
  ⇒ 回撤恒为 ~0 ⇒ `max_drawdown_pct=-0.20` 永不触发；`risk_per_trade=0.040`
  (5,493 vs 2,315)、`max_single_position_pct=0.40` (54,929 vs 23,145)、
  `daily_stop_flatten_pct=-6%`(−8,239 vs −3,472 ⇒ 实际强平线挪到 −14.2%) 同步放大。
  修法：`_total_asset_of()` 券商字段与「cash+市值」差 >1% 即判坏值并回落，同时记
  一次日志。**这不是 2026-10-30 禁止的那种拼装**：此处 cash 与市值取自
  **同一个 `get_trade_detail_data` 返回的同一个对象**，是同一时刻快照，
  不存在跨查询的最终一致性窗口。由
  `tests/test_qmtide_bridge.py::TestQueries::test_query_asset_rejects_insane_balance` 钉住。
  **顺带修 DBL_MAX**：`_f()` 原先把 QMT 的「字段不适用」哨兵
  `1.7976931348623157e+308` 当真值放行（实测 `m_dCurrMargin`/`m_dPreBalance`/
  `m_dAvgOpenPrice`/`m_dStaticHoldMargin`/`m_dStockLastPrice` 全是它），
  进入账本后任何运算都变 inf/nan。
  — 证据：`qmt_bridge/qmtide_bridge.py:_f()` / `_total_asset_of()`；DUMP 原始字段见下条
- [P1] [SAFE] **桥接两处修正（不改变当前生产，切 bridge 时才生效）**
  - **幂等守卫的日志炸弹（我引入的，已修）**：`拒绝重放` 分支每 500ms 命中一次，
    而我让它每次都写日志 + 重写 `result.json` ⇒ 7 分钟 **1,288 行**、把 `boot.log`
    顶到 200KB 上限**轮转掉一轮诊断历史**（`boot.log.1` 已满 204,875 B）。
    修法：按 id 去重只报一次；`result.json` 只在答案不同时写。
    钉住 `test_duplicate_branch_logs_only_once`。
    ⇒ 纪律：**凡在轮询路径上的日志，默认必须去重**。
  - **`_q_orders` 的 side 是朝「卖」的 fail-open**：旧代码
    `side = "BUY" if ot in (23,33,48,49) else "SELL"` —— 认不出就猜 SELL；
    回落链里含 `m_nOrderPriceType`（**价格类型**）当方向用；且 49（平仓）被算成 BUY。
    改为只认 `m_nOffsetFlag`，48/23/33→BUY、49/24/34→SELL、其余 `UNKNOWN`。
    影响面已核：`get_orders` 仅被 `_attribute_order_id` 用于反查委托号，方向只是
    加分项（`core/broker_bridge.py:344` 自述「宁可取同代码新委托号，胜过返回 None」）。
    ⚠ 48/49 仍是猜测值，**必须在有真实委托时用 DUMP 校准**（周日 ORDER count=0）。
- [P1] [INFO] **撤回上一条建议：`broker_qmt_mode` 没有「只切查询」的路径**
  — `core/broker.py:536` 是**全有或全无**单开关（`"bridge"` / `"XtMiniQmt"`），
  切了就是查询与下单同时换通道。若确实要「先只切查询」，需另加 flag，
  属新增能力，需 OWNER 决定是否值得。
- [P2] [INFO] **`equity_snapshots.drawdown_pct` 系统性低估回撤 3.74pp，且与熔断实际口径不是同一个峰值**
  — 熔断判定走 `risk/manager.py:158` → `risk._peak_asset` = **67,453.62**
  ⇒ 11-06 收盘真实回撤 **−14.22%**，距 `max_drawdown_pct=−0.20` 仅剩 **5.78pp**；
  而权益快照走 `engine/event_engine.py:3889` → `_peak_equity` = **64,636.1**
  ⇒ 落库 `drawdown_pct=−10.48%`。两个峰值字段差 **4,817 元 / 3.74pp**，
  方向是「熔断更严、报表更乐观」⇒ **不是资金安全缺陷，但是观测误导**：
  看快照会以为还有 9.5pp 缓冲。同一事实两份实现，建议统一或至少在快照里并列记录。
  — 证据：`storage/qmt.db::engine_state(peak_asset,peak_equity)` 与
  `equity_snapshots(id=10427, drawdown_pct=-0.10479)`；`risk/manager.py:158`、
  `engine/event_engine.py:3889`
- [P2] [INFO] **`runMode="1"`（另两个自建策略为 `0`）经交叉验证不是「模拟盘」开关，仍未动**
  — bridge 读数与引擎 xtquant 直连读数在三项上吻合（现金 11,611.49 vs 11,611.57、
  持仓数 2 vs 2、持仓市值 46,251.00），说明它连的就是实盘账户。
  `accountType=2`（普通）而另两个是 `3`（信用），`runMode` 可能编码的是账户/模式组合，
  语义未定性 ⇒ **保持原值，不试错**。`passorder` 是否真能落到实盘仍未经实单验证。
- [P2] [INFO] **`m_nOffsetFlag` 的 23/24 买卖判定仍未经实测**：DUMP 时 `ORDER count=0`
  （周末无在途单），没有可对照的样本。首次真实下单前必须再跑一次 DUMP 校准，
  否则 `_q_orders` 的 side 可能整体判反。
- [P2] [INFO] **桥接读到的持仓比 10-29 基线多一个**：现有 `510050.SH` 500 股
  （成本 2.879、现价 2.902、市值 1,451）+ `300308.SZ` 100 股（成本 501.57、
  市值 44,800）。引擎 11-06 心跳也是「持仓=2」⇒ 一致，是 11-05~11-06 期间建的仓，
  不是 bridge 读到了别的账户。工作记忆里的 66,542.31 / 单持仓基线已过时。

- [P0] [FORBIDDEN] **桥接已切到内嵌引擎（`simpleRun=0` 生效），但被「`sys` 无 `argv`」
  和「`ContextInfo` 挂属性致重复下单」两处绊住，均已修**
  — 生效证据：`boot.log` 出现 `name='_M_QMTIDE_BRIDGE_0003005' passorder=True
  stdout=<PythonIOWrapper>`（非 `__main__` ⇒ 内嵌引擎；`passorder=True` ⇒ 已注入），
  QMT 日志同步出现 `PythonFormula::loadArgvs ... argvs:[account:...,accountType:STOCK,...]`。
  **致命坑**：`XtClient_Formula_20261108.log:773` —
  `[pythonformula] run script failed!, msg: AttributeError: module 'sys' has no attribute 'argv'`
  ⇒ QMT 内嵌解释器的 `sys` 是精简 shim、**没有 `argv`**；我的启动探针第 2 行就读它
  ⇒ **模块加载中断、`def init` 都没定义到** ⇒ 策略显示「运行中」但什么都不做
  （`boot.log` 只有 1 行、无 `heartbeat.json`）。修法：探针逐字段容错 + 模块级再加
  一层 try/except —— 模块加载一抛错，QMT 只打一行 `pyerror` 就放弃。
  **资金安全坑**：原 `_place_order` 在 `try` 里写 `ContextInfo._bridge_po_form = form`；
  若 ContextInfo 是 C++ 绑定对象拒绝挂属性，异常会被外层 `except` 当成「这种 passorder
  形式失败」⇒ 换下一种形式**再发一次单**。改为模块级 `_po_form` + `_set_po_form()`
  吞异常，并由 `tests/test_qmtide_bridge.py::TestRigidContextInfo` 钉住。
  **量测卫生坑**：`run_all.py:443` 用 `inspect.getmembers(mod, inspect.isfunction)`
  ⇒ **只采集模块级 `test_*` 函数，TestCase 类不采集**。本轮 33 个类用例一度贡献 **0 条**
  而 run_all 仍报绿（同 10-21「两个模块静默消失却全绿」同型）。已在文件末尾加
  `_expose_case` 薄壳桥接，修后 930 total / 本模块 33 条全部纳入。
  — 回归 **897 PASS / 33 FAIL / 930**（33 个全是 xtdata `not authenticated`）
  — 发现于 手动排查 2026-11-08

- [P0] [FORBIDDEN] **桥接「一点运行就停止」真因已定位：QMT 用独立进程方式跑，`init()` 从未被调用**
  — `XtClient_20261108.log:11894-11902`：`doRun execude cmd: -u "…\QMTIDE_BRIDGE.py"` →
  存活 **159ms** → `doRun return code:1`。用 QMT 自带 `bin.x64\pythonw.exe`（3.6.8）
  按同样命令行实跑 **退出码 0**（250ms）；再用 ctypes 复刻「CreateProcess + 无标准句柄」
  仍为 **0**（`scripts/_repro_qmt_launch.py`）⇒ **不是崩溃，是脚本跑完自己退了**。
  QMT 有两种执行方式且不等价：A 内嵌引擎（模型交易，调 `init` + 注入 `passorder`）／
  B 独立进程（`pythonw -u <脚本>`，`__name__='__main__'`、无 `passorder`、`sys.stdout=None`）。
  本策略是给 A 写的，QMT 却按 B 拉起 ⇒ `C:\pazq_qmt_bridge\` 里连 `heartbeat.json` 都没有。
  **判别依据**：`config/indexUserConfig.xml:242` 该策略 `simpleRun="1"`，而同目录下用户
  自建的 4 个实盘策略（`GLM5日内回转`/`信用账户测试DOUBAO`/`日内回转加风险控制AMDLOCAL`/
  `尾盘闲置资金自动逆回购`）全是 `simpleRun="0"`，只有 QMT 自带示例和本策略是 `1`。
  **已落地（SAFE 部分）**：策略源码正式入库 `qmt_bridge/qmtide_bridge.py`（此前**未纳入
  版本控制**，只存在于 QMT 目录）；部署改为 `scripts/deploy_bridge.py`（GBK+CRLF 写出，
  不再被 QMT 改写成 583 个问号）；加 import 期启动自证 `boot.log` + `sys.excepthook` 落地
  （pythonw 会丢 traceback）；新增 `tests/test_qmtide_bridge.py` **26/26**
  （此前该文件零覆盖），`run_all` **864 PASS / 33 FAIL / 897**（33 个全是 xtdata
  `not authenticated` 的环境失败，与本次无关）。
  **需 OWNER 动作（FORBIDDEN 部分）**：把该策略运行方式从「简单运行」改成「模型交易/实盘」。
  **2026-11-08 01:49 已由 OWNER 关停 QMT 后由我改配置**：`simpleRun="1"` → `"0"`
  （`scripts/qmt_set_simple_run.py --apply QMTIDE_BRIDGE`；逐字节 diff 只变 1 个字节，
  XML 仍合法，备份 `config/indexUserConfig.xml.bak-20261108-014909`）。
  剩余动作只有一步：打开 QMT → 模型交易 → 点运行 → 看 `C:\pazq_qmt_bridge\boot.log`
  是否出现 `[BRIDGE] MODE=embedded init() called`。
  ⚠ 同一条目里 `runMode="1"`（另两个自建实盘策略都是 `0`）疑似「模拟/实盘」开关，
  **未改** —— 若 simpleRun 改完仍是 standalone 或成交不下到真实账户，那才是下一个要动的开关。
  — 发现于 手动排查 2026-11-08

- [P0] [SAFE] **桥接 `passorder` 参数顺序错误：`accountType` 被塞进资金账号位，后面整体错位一格**
  — 以 QMT 随附可跑示例 `C:\pazq_qmt\python\双均线实盘示例PY.py:77` 为准，正确形式是
  **11 参、不含 accountType**：`passorder(op, otype, acct, code, prtype, price, vol,
  name, 1, msg, ContextInfo)`。原实现 `atype_aid_qt` 传的是
  `(op, otype, atype, aid, code, prtype, price, vol, name, 2, C)` ⇒ `args[2]` 是 `"STOCK"`
  而不是账号 ⇒ 会在错误账号报单或直接废单。
  **已修** `qmt_bridge/qmtide_bridge.py::_PASSORDER_FORMS`（改为 `aid_qt` 11 参优先，
  再回落 12/10/10 参），并由 `tests/test_qmtide_bridge.py::test_sell_uses_11arg_form_
  with_account_id_in_position_3` 断言 `args[2] == "000000000000"` 且 `!= "STOCK"`。
  **未修**：`qmt_bridge/qmt_executor.py` 里仍是老写法（该文件**未部署**，仅参考）。
  要启用它必须先同步本修正，并同步改 `tests/test_bridge_executor_e2e.py:317-321`
  （`fail_forms` 与最终形式断言会随之变化）。
  — 发现于 手动排查 2026-11-08

- [P0] [SAFE] **桥接端到端仿真上线：发现并修复「卖出可能重复下单」的资金安全缺陷**
  — 新增 `tests/test_bridge_executor_e2e.py`（**15/15**）：按**真身**加载
  `qmt_bridge/qmt_executor.py`（只把 QMT 内建 `passorder`/`cancel`/
  `get_trade_detail_data` 注入成桩），再用**真实** `BridgeBroker` 驱动 ⇒
  覆盖的是「真正发货的那份代码」，而非手写 fake。
  **抓到的缺陷**：`passorder` 没有返回值 ⇒ 执行器只能回 `{"order_id": None}`
  ⇒ 引擎 `ok=True` 但拿不到委托号 ⇒ **不调用 `_register_pending_order`**
  （`event_engine.py:3572/3748`）⇒ 卖出侧 `quantity` 不扣减、槽位不释放、
  `_mark_sold_today` 不置位 ⇒ **下一轮对同一批股票再卖一次**，100 股可能卖成 200 股。
  **修法**（`core/broker_bridge.py::place_order`）：下单前记 `QUERY_ORDER` 基线，
  下单后 3s 内按「代码必匹配 + 方向/数量打分」反查新委托号；反查失败打 `ERROR`
  并返回 None（不伪装成失败 —— 伪装失败会让引擎重发，变成真的重复下单）。
  — 证据：`core/broker_bridge.py:305-395`、`tests/test_bridge_executor_e2e.py::test_order_id_attribution`
  — 发现于 桥接部署 2026-11-08 [已修 + 已测]

- [P1] [INFO] **QMT 侧部署已就绪，卡在最后 3 次鼠标点击（我无法代替操作 GUI）**
  — 执行器已复制到 QMT 策略目录：`C:\pazq_qmt\python\qmtide_bridge.py`（18,602 B）；
  共享目录 `C:\pazq_qmt_bridge\` 已创建；步骤见 `qmt_bridge/README_DEPLOY.md`。
  **QMT 重启后策略不会自动恢复**（`Config.xml` 无 autostart 项，实测 grep 零命中）
  ⇒ 每次重开 QMT 都要在模型交易里手动「运行」一次。
  探针 `scripts/bridge_probe.py` 在心跳缺失时会直接给出这 3 步提示。
  — 证据：`qmt_bridge/README_DEPLOY.md`、`scripts/bridge_probe.py:38-46`
  — 发现于 桥接部署 2026-11-08 [待 OWNER 点 3 下]

- [P0] [SAFE] **行情通道已修复：`TushareQuoteClient` 已落地（零成本方案），全量回归无新增失败**
  — 新增 `core/tushare_quote.py`（与 `_XtdClient` 同接口）、`scripts/quote_probe.py`（只读探针）、
  `tests/test_tushare_quote.py`（**29/29**）；`core/qmt_client.py` 降级链改为
  **xtdata → tushare → mock**（原为 xtdata → mock，等于行情断供时直接落合成价）。
  实测四只标的逐项对齐：300308 tick 448.0 == 日线 448.0；510050 由新浪兜底 2.902；
  000001.SH 3649.177 / 000300.SH 4054.277（指数走 `index_daily`）。
  **零配置生效**：不设 `QMT_QUOTE_SOURCE` 即 auto；`QMT_QUOTE_SOURCE=tushare|xtdata` 可强制。
  — 证据：`core/tushare_quote.py:1-120`（设计依据）、`scripts/quote_probe.py` 实跑输出
  — 发现于 行情源选型 2026-11-08 [已交付，待周一盘中验证实时性]

- [P0] [FORBIDDEN] **⚠ 换源后 mock 闸门不再拦截成交，而券商通道仍是断的 ⇒ 周一若直接起引擎，所有买卖会「尝试后失败」**
  — 判据是 `mock_trade_block_reason`：只有 `data_mode == "mock"` 才拦。
  行情换到 tushare 后 `data_mode="tushare"` ⇒ **卖出链（含 −18% 硬止损）会真的发起委托**，
  而 `broker_qmt_mode` 仍是 `"XtMiniQmt"`（xttrader 被 illegal pid 拒连）⇒ 每轮失败重试。
  **不是资金风险**（下单必失败），但是「以为止损发得出去、其实没发」的假安全感。
  建议：周一先跑 `scripts/bridge_probe.py` 的 S3 只读联调，通过后再切 `broker_qmt_mode="bridge"`；
  在此之前**不要**让引擎在 live 下空转，或明确接受「订单失败」这一状态。
  — 证据：`engine/event_engine.py:71-83`（闸门只认 mock）、`tests/test_tushare_quote.py::test_tushare_mode_passes_mock_gate`
  — 发现于 行情源选型 2026-11-08 [待 OWNER 裁决]

- [P0] [INFO] **tushare 有实时行情，且本 token 已能调 `rt_tick`（实测可用）；`rt_k` 明确无权限**
  — 实测 `pro.rt_tick(ts_code='300308.SZ')` 返回当日完整累计 OHLC（open=433.5/high=463.33/
  low=427.34/price=448/vol/amount），与 `pro.daily` 20261106 逐项吻合 ⇒ **可作 xtdata 日线 bar 替代**；
  但 `pro.rt_k` 报「您没有接口(rt_k)访问权限」、`pro.rt_etf_k` 同样无权限。
  实测频次：`rt_tick` **10 次/分钟**、`rt_min` **5 次/分钟**（两者均有访问权，非无权限）。
  覆盖缺口：**沪市 ETF 不在 `rt_tick` 内**（510050/510300/512880 缺失，深市 159915 有）；
  `ts.realtime_quote`（新浪源，免积分）可补齐 510050 且含完整当日 OHLC。
  官方独立权限价目（与积分无关）：实时日线 `rt_k` **200 元/月**（50 次/分、单次全市场）、
  ETF 实时日线 `rt_etf_k` **200 元/月**、指数实时日线 200 元/月、实时分钟 `rt_min` 1000 元/月。
  — 证据：`https://tushare.pro/document/1?doc_id=290` 表二 — 发现于 行情源选型 2026-11-08
  [已裁决：OWNER 选「一分钱不用花」＝ rt_tick + realtime_quote，不购 rt_k / rt_etf_k]

- [P1] [INFO] **行情换源的落地成本已被压到最低：只需实现 4 个方法**
  — `core/qmt_client.py` 把行情收敛在 `QMTClient` 一个类里，外部只依赖
  `subscribe / get_ticks / download_history / get_history`（`:475/:478/:481/:491`）。
  `rt_tick` 字段与 `_normalize()` 的输出契约（`:219-236`）几乎一一对应：
  `price→lastPrice`、`open_price→open`、`high_price→high`、`low_price→low`、
  `pre_price→lastClose`、`volume/amount` 同名 ⇒ 新增 `TushareQuoteClient` 即可 drop-in。
  — 证据：`core/qmt_client.py:210-236,475-495` — 发现于 行情源选型 2026-11-08

> **【2026-11-08 00:55 · 大 QMT 文件桥已交付（方案 A）· 待周一开盘只读联调】**
> **新增**：`qmt_bridge/qmt_executor.py`（QMT 模型交易里跑，`passorder` 下单）＋
> `core/broker_bridge.py`（与 `QMTBroker` 同接口）＋ `core/broker.py::_make_broker()` 分流
> ＋ `tests/test_bridge_broker.py`（**16/16，连跑 3 轮稳定**）＋ `scripts/bridge_probe.py`（只读探针）。
> **切换开关**（`trading_config.json`，gitignored）：`broker_qmt_mode: "XtMiniQmt" → "bridge"`。
> **回滚**：改回 `"XtMiniQmt"` 并重启，零残留。
> **S3 步骤（周一 09:31）**：① QMT 模型交易加载 `qmtide_bridge.py`、选自己的资金账号、实盘、
> 看到 `[BRIDGE] init done` ② 跑 `python scripts/bridge_probe.py` ③ 先看 DUMP 的 `schema`
> 校准字段名，再确认 `QUERY_ASSET` / `QUERY_POSITION` 非 None ④ 通了才改开关。
> ---
> **[P0] [FORBIDDEN] 提权复测已排除权限**：`admin=True` 仍 `illegal pid 3028`；官方
> 「C 盘需管理员」那条对本案不适用。两条路都要券商侧动作，故走桥接。
> **[P0] [INFO] 行情（xtdata）也断了**（00:50 实测）：`服务地址: None` /
> `get_full_tick: not authenticated`；`~/.xtquant/` 下 6 个实例目录**全无 `xtdata.cfg`**。
> 时间线：`XtMiniQmt.exe` 00:13:07 退出后，全客户端**不发布**发现配置。
> ⇒ **桥接只解决交易，行情需另找来源**（`trading_config.json` 已有 tushare token 可作后备），
> 这是 S3 之后紧接着要定的事。
> **[P1] [INFO] 全量回归 803 PASS / 33 FAIL / 836**：33 个失败**全部归因环境**
> （回测取不到数据 `KeyError 'total_return'` / `min() arg is empty`；引擎买入断言消息直书
> 「行情源已降级为 mock ⇒ 全部买卖被禁止」；`test_qmt_health` 断言里带 `not authenticated` 原文）。
> 13 个失败模块中 **10 个不引用 broker**（逐个 grep 确认）⇒ 结构上不可能由本次改动引入。
> **不得拿这 33 个当成本次改动的回归责任。**


> **【2026-11-08 00:25 · QMT 2.1.32.0 升级后 xttrader 全线不可用 · 根因已定位到「框架白名单」】**
> **结论**：不是平安停用 miniQMT、不是模式选错、不是 xtquant 版本不匹配。是 2.1.32.0 新增的
> **进程/框架白名单**把外部 python 判成 `illegal pid` 直接拒连。
> **决定性证据**（`C:\pazq_qmt\userdata\log\XtClient_20261108.log`）：
> `00:18:34 [TC::COrderServiceQuantAdaptor::onConnected] quant session 876636, illegal pid 22420`
> `00:19:04 ... quant session 760421, illegal pid 15988` —— 两个 session 号与我两次实测逐位对应，
> 说明**连接到达了服务端并被明确拒绝**（不是没连上、不是超时无响应）。
> **佐证**：`OrderService.dll` 内含 `m_bUseWhiteList` / `m_bUsePrevWhitelist` / `queryFrameworkWhitelist` /
> `onQueryFrameworkWhiteList` / `importwhitelist` / `sigFrameworkWhitelistChanged` / `quant session {0}, illegal pid {1}`。
> **服务端确实在跑**：同一日志 `00:13:28 Allow the XtQuantServer to start.` /
> `[orderservice] start quant server thread [C:\pazq_qmt\userdata]` / `[quant]XtQuantServer::start lock xtquant=0`，
> 且 `userdata\` 下队列族完整（`up_queue_xtquant` 75,497,752B / `up_queue_win_xtquant` / `winsemaphore_flag_xtquant`）。
> **权限未被收回**（推翻我 11-07 的误判）：11-07 23:54 与 11-08 00:10 两次 `[auth]success` 的
> `m_userPermission` 串**逐字节相同**（2418 字符、50 项非零），含 `algoTrade=1`、`m_nResearchMiniQmt=1`、
> `m_nLoginLiteMode=1`、`m_nXtTrader=1`、`m_nMiniPython=1`。
> **「没有极简模式入口」不是缺陷**：`极简` 在 2.1.32.0 全部 UI 资源里**不存在**（扫 config/translations/bin.x64/
> resource/indexConfig，GBK 与 UTF-8 双编码），该版本只有一个「独立交易」入口；miniQMT 改为由
> `XtItClient.exe` 经 `CMultiClientManager::doStartProcess` 拉起 `XtMiniQmt.exe`（00:07:03 / 00:10:56 两条）。
> **[P0] [FORBIDDEN] 需 OWNER 操作**：在 QMT 客户端里找到「白名单 / 框架白名单 / 策略白名单」并把
> `C:\Users\lisinan\.conda\envs\qmt\python.exe` 加入。UI 里找不到就找平安客户经理要 2.1.17.0 安装包回滚
> （手上只有 9 个 exe 备份，DLL 未备份，**不能完整回滚**）。
> **[P1] [INFO] 数据目录已变**：全客户端把 xtquant 服务挂在 `C:\pazq_qmt\userdata`（而非 `userdata_mini`），
> 且 00:10 那次 `XtMiniQmt.exe` 运行期间 `userdata_mini` **从未起过** xtquant 服务（日志无 `Allow the XtQuantServer`）。
> ⇒ 白名单放行后，`QMT_USERDATA_PATH` 需指向 `C:\pazq_qmt\userdata`（可用同名环境变量覆盖，`settings.py:74` 支持，**不必改文件**）。
> **仓位安全**：引擎已停，券商推送 `balance 57,862.49 = 11,611.49 + 46,251.00` 与 11-06 收盘逐项吻合。



> **【2026-11-03 09:00 完整复盘 · DAILY-REVIEW（OWNER 手动触发，盘前）】** 详细证据见 `reports/DAILY_REVIEW_2026-11-03.md`。
> 三源估值一致（券商/API **63,207.94** 逐位相同，DB 63,207.98 差 0.04）；昨日改动**四项全绿**（mtime 全部早于 pid 7336 启动 23:04:22，commit 链 `d52c569←60497d8←097b440←3457cb3` 完整）；11-02 心跳 **288 条零空档** ⇒ 排除整机休眠。
> **本轮 4 个 P0 全部与「风险读数失真」相关**，且**互不重复**：① 止损价可能低报 4.7 倍（影响今日唯一一笔真实下单）② 账本盈亏归零 ③ 回撤预算可累加 ④ 闸门吃了坏指标。

> **【2026-11-03 19:00 特殊日复盘 · 盘中决策链专项】** 详细证据见 `reports/SPECIAL_REVIEW_20261103.md`。
> 当日 −3.38%（−2,134.33）**全部来自唯一一笔人工覆盖买入**（300308 @501.51，10:59:01）；策略自身全天零成交且结论正确（277 次漏斗、候选池恒空）。
> 三件事叠加：**整机掉电 10:54:28（Kernel-Power 41，引擎停摆 4 分 52 秒）** + 人工把 `min_price` 537→0（10:53:56）+ 光模块赛道崩（300394 −12.80 / 300308 −12.69 / 300502 −9.46，同期创业板 −2.73、510050 **+0.45**）⇒ 高低切换日。

> **【2026-11-03 23:16 OWNER 指令「510050.SH 加进」· 已落盘】**
> `config/settings.py:723` → `exit_exempt_codes: ["300308.SZ", "510050.SH"]`。
> **实测复核（不是读代码猜的）**：`_is_manual_exempt("510050.SH")=True`；`_split_flatten_candidates()` 返回
> `flat=[]` / `exempt=['300308.SZ','510050.SH']`；`TrendStrategy.on_exit` 在日线宇宙不含 510050 时，
> 价位 2.884/2.70/2.40 **全部 None**，只有 2.35 才出 `硬止损 -18.37%` ⇒ **豁免没削掉任何保护**，阈值等价（2.361）。
> ⇒ 净变化**只有一条**：510050 不再进 −6% 日亏强平候选。副作用（已知并接受）：两只持仓都在豁免清单内，
> **熔断不再减仓，退化为「只停新开仓」**。`tests/run_all.py` **793/793 PASS**。
> ⚠ **需重启生效**（pid 20084 仍为 10:58:26）；今晚的 `force=True` 修复也在同一份重启里一起生效。

- [P1] [INFO] **[已裁决·已改] 510050 的观察篮豁免确实丢了且不自愈，但明日不会被趋势破位卖出**：撤回理由（我按代码核的）：`510050.SH` **只在 `VALIDATION_UNIVERSE`**（`settings.py:214-216`），**不在 `UNIVERSE`（n=16）/ `STOCK_CODES`（n=12）/ `INDEX_CODES`（n=4）**（实测 `from config import settings` 四者谓词均为 False）；`_daily_codes():685-693` = STOCK_CODES + INDEX_CODES + `dynamic_universe.active_codes`，**不含 510050**；`daily.refresh` 只有 `:738/:747` 两处、都传 `_daily_codes()`。⇒ 引擎内 `_closes["510050.SH"]` **不存在**，`trend_broken:557 if not c or len(c) < exit_ma: return False` 恒 False；单日暴跌需日涨跌幅（取不到）；无 tick 兜底 `:3805-3807 features(code) is None → return`；超时因 `open_date=None` 跳过。⇒ **四条策略退出全部为空，唯一出口只剩 −18% 硬止损 = 2.879×0.82 = 2.361**（与 `engine_state.stop_price=2.361` 吻合）。
  ★ **我的错误（记下来）**：我用 xtdata 原始日线算出 `trend_broken=True`（2.884 < MA60 2.9673），但引擎的 `DailyContext` 里没有这只票 ⇒ **「指标算得对」≠「引擎会这么算」**。这是「同一事实两份实现」的第 21 次复现，也是我本轮唯一一个未验证「路径是否可达」就定 P0 的错误。
  ★ **真实风险（这才是要留的）**：豁免丢失后 510050 从「**强平豁免仓**」变成「**强平对象**」——`_split_flatten_candidates:2482` 按 `_is_manual_exempt` 剔除，重启前它在 `_manual_positions` 会被剔除，重启后不再剔除 ⇒ **若 300308 跌到 443.56 触发 −6% 日亏强平，510050（1,442 元 / 2.4%）会被卖掉**（重启前不会）。规模小，但机理是真的。
  ★ **更该留的形态**：`_manual_positions` 不持久化（`:389`）+ 已持仓永不重新登记（`:2573`）⇒ 任何重启都会让观察篮豁免永久丢失。**一旦某只观察篮票被纳入 `UNIVERSE` / 日线上下文，这条缺失的豁免会立刻变成真卖出。** 今天没咬人，是因为 510050 不在日线宇宙 — `config/settings.py:214-216` + `engine/event_engine.py:389/2573/685-693/738/747/2482` + `strategy/daily_context.py:557` — 发现于 live-ops 特殊日复核（含我的误判与撤回）2026-11-03
- [P0] [FORBIDDEN] [待 OWNER · 决定今晚有没有修法] **「热读」在代码层面是假的：全仓库无 `importlib.reload`，改 `settings.py` 必须重启才生效**：`config/settings.py:624` 原注释自认「settings 只在 import 时执行一次，全仓库无 importlib.reload」；`STRATEGY_PARAMS`（`:224`）/ `RISK_PARAMS`（`:884`）是模块级常量，`engine/event_engine.py:34 from config.settings import (...)` 一次性绑定；全仓 grep `importlib.reload|reload(` **仅命中 2 处注释、0 处调用**。⇒ 与 `settings.py:654`「热读，无需重启」、`:562`「改清单无需重启」等多处注释**直接矛盾**（该 P2 已在案，现补铁证）。⇒ **推论：今晚任何修复（改 `force=True` / 加 510050 进豁免清单 / 关保护期）都必须重启**，不存在「热读改一下就行」的路径。反证依据：今日 `min_price` 从 537 改成 0 后，旧进程 09:35 仍按 537 拒单，**10:58:26 重启后**的 10:59 才按 `0~545` 成交 ⇒ 实测印证需重启 — `config/settings.py:224/624/884` + `engine/event_engine.py:34` — 发现于 特殊日复盘 2026-11-03
- [P0] [FORBIDDEN] [待 OWNER · ★ 这条决定明早盲窗是否存在] **live 每次重启把全部持仓的 `open_date` 清成 None ⇒ T+1 与建仓保护期同时失效（2026-09-14 T+1 修复在 live 下是回归的）**：`:565-570` live 分支只调 `_restore_engine_state(restore_ledger=False)`；`:3940 if not restore_ledger:` 只恢复风控字段（`load_state`/`reset_daily`）**不恢复持仓**；`_pos_meta` 是纯内存字典（`:526` 初始化为 `{}`），仅在 `:3540` 买入时写入、`:4239` 券商同步时读取，**从不落盘** ⇒ 重启后 `_sync_broker_positions` 取 `meta.get("open_date")` 恒为 None。**实证（今天账上就有）**：510050 于 11-03 09:30:03 买入 → 10:58:26 重启 → 现 `engine_state` 里 `open_date=null`；300308 因 10:59 买入晚于重启，`open_date` 才保留。**后果**：① `is_t1_locked(None)` 恒 False ⇒ **重启后当日买入的仓位可被当日卖出**（券商必拒，制造实盘不可能成交的 round-trip；正是 `:3666` 注释里 2026-09-14 要修的那个 bug）；② `is_in_entry_protection(None)` 立即 False ⇒ **保护期消失**；③ `trend_max_hold_days` 永久不触发。**★ 由此产生的关键推论**：**明早那 30 分钟盲窗是否存在，取决于引擎今晚/明早是否重启过**，而 guardian 会自动拉起（今天 10:58 就是它拉起来的）⇒ **这个开关不在 OWNER 控制内**：引擎活到 09:30 = 有盲窗；期间任何一次重启 = 盲窗消失（同时 T+1 也没了，对 300308 无影响因明日已解禁）。⇒ **改代码+重启（方案 a）与「什么都不改但重启一次」在明早效果相同；区别只在「以后每次」——(a) 永久修对，靠重启偶然修好则下次复现** — `engine/event_engine.py:565-570/3940/526/3540/4239/117-131/3666` — 发现于 live-ops 特殊日复核 2026-11-03
- [P0] [INFO] **★ 明日实际只有三条线（不是四条）：「亏 5,000 停开仓」在**本账户规模下**不生效，且它是「规模相关 + 买入门控」双重受限，不是代码层永死**：`on_asset_update:145-155` 每轮按 `daily_stop_flatten_pct=-0.06` 判 **相对 `_day_open_asset`** 的日亏，一旦达标即 `_flatten_requested=True` + `_halt()`；此后 `account_block_reason:184 if self._halted: return "halted:..."` **先于** `can_open:223` 的日亏判定 ⇒ 本账户下 `_daily_loss_limit` 恒不可达。数值：`max(5000, 0.03×61,073.65=1,832)` = **5,000 元 = −8.19%**，而 −6% 仅 3,664 元 ⇒ −6% 恒抢先。★ **措辞必须收窄**：这是**规模相关**而非代码永死——判据 `D0 ≤ 83,333` 时 −6% 抢先（本账户 D0=61,073.65 属此档）；`83,333 < D0 ≤ 166,667` 时 5,000 先到、该线**活**；`D0 > 166,667` 回到 3% 分支。入金/复利都会翻转结论。★ **且它是「买入门控」**：`can_open` 全文件**唯一调用点** `:3509`（在 `_handle_buy` 内），`account_block_reason` 唯一调用点 `:3428` ⇒ **只有引擎正在尝试买入那一刻才评估**；`regime=down` 无信号的日子里可能一次都不评估。★ 不应归入「降级型风控（买入侧冻结反而放大 MDD）」那 3 条同向证据——机理不同（那 3 条是冻结开仓导致更差，这条是被更早的线支配 + 门控不评估），单独归一类。
  **明日三条线（平开 480.20 基准；cash 11,611.65 + 510050 500×2.884 = 不变部分 13,053.65）**：① **443.56** = −6% 日亏强平（`manager.py:142`，分母 `_day_open_asset`）——但 300308 属豁免仓，`_split_flatten_candidates:2482` 把它剔出，**只卖 510050 的 1,442 元（2.4%）**；② **411.29** = −18% 硬止损（501.5679×0.82，`event_engine.py:2229-2235`）——300308 唯一出口，且有 11-04 10:00 前盲窗 + stale/无tick/限价三道阻碍；③ **380.24** = −20% 断路器（0.8×63,846.65 − 13,053.65，再 /100）——**只 `_halt` 停买、不平仓**。★ **创业板明日跌停价 384.16 > 380.24 ⇒ −20% 断路器明天物理上不可达**；可达的只有 ① 与 ②。 （注：430.20 = 亏 5,000 元对应价，**该线不生效**，勿用于盯盘）— 定价来源 risk-controller v3，我按 `manager.py:145-155/184/223/246-262` 独立复算一致 — 发现于 特殊日裁决 2026-11-03
- [P1] [INFO] **明日盯盘两个技术陷阱**：① `_day_open_baseline:1990` 是 `if self._day_open_asset is None and not self._market_stale` ⇒ **行情陈旧时不开日内基线**，低开+开盘陈旧会让 D0 下移到「首个非陈旧 tick」，−6%/−3% 两条线随之下移，**不能用 09:30 快照当分母**；② `_log_reject_once:3181-3205` 日志前缀硬编码 `"BUY 拒绝"`，卖出被 stale 挡住也打成「BUY 拒绝」，grep 到它不代表买入侧有问题；`/api/snapshot` 无 `market_staleness` 字段，只有 `market_data_ts`（`observability.py:66`），判 stale 要用它 + 快照是否推进 — 发现于 live-ops 复 2026-11-03
- [P0] [SAFE] [已修 1503705 · **需重启生效，重启由 OWNER 本人执行**] **豁免仓的 −18% 硬止损在「首个可卖日早盘 30 分钟」内被静默拦截，且拦它的分支漏传 `force=True`**：`ENTRY_PROTECT_MINUTES=30`（`settings.py:994`）+ `_handle_sell:3689 if (not force and is_in_entry_protection(...))`。豁免分支 `:2229-2235` 调用 `_handle_sell(sig, pos)` **不传 `force`**，而日内强平 `:2165` 传了 `force=True`（`:3688` 注释明写「force=True 时绕过本保护期——硬止损优先级高于防误伤」）⇒ **300308 `open_date=2026-11-03T10:59` 的首个可卖日 = 11-04，`protect_until=11-04 10:00:00`；该窗口内即使跌破 411.2857 也不会卖出**，★ **「静默拦截」系我误述，已更正**：`_should_emit_block_notice:3607` 按 `(code, open_date, type)` 去重、**每日一次**⇒ 11-04 开盘首轮会正常看到一条 `[保护期拦截]`，之后当日无声滑落（不是完全不可见，是「只报一次」）。★ **且该窗无下界**：`is_in_entry_protection:130 return now < protect_until` 没有起点约束 ⇒ 实际是「**2026-11-03 10:59:01 买入起连续遮挡到 11-04 10:00:00**」的单一连续窗，今天那一段被 T+1 掩盖（两者叠加效果相同，但机理不是「次日早盘窗」）。**要关掉需改代码 + 11-04 09:30 前重启**（pid 20084 启动于 11-03 10:58:26）⇒ 只报不执行。**行号以运行版为准**（pid 20084 跑 `fcad597^`，与磁盘版偏移约 +35 行，判定逻辑不变）：`_handle_sell:3623` / **stale 守卫 `:3654-3656`（块范围；`:3655` 是块内调用行）** / **保护期 `:3681`** / 日内强平 `force=True:2165` / `_should_emit_block_notice:3607` — `engine/event_engine.py` + `config/settings.py:994`
  ★ **修复落盘（1503705）**：`:2229-2246` 豁免分支 `_handle_sell(..., force=True)`；新增 `tests/test_exempt_hard_stop_force.py` 4 用例（主用例驱动真实 `_run_once`；正向对照 A「不传 force 必须被拦」、B「未破阈值必须不卖」；边界「force 不得绕过 T+1」）。**变异验证**：删掉 `force=True` 主用例立刻失败、三条对照仍绿 ⇒ 不是假绿。`run_all 793/793 PASS`。
  ⚠ **这条修复对「明早」其实不改变结果**：OWNER 重启 ⇒ `_pos_meta` 纯内存不恢复 ⇒ 300308 的 `open_date` 也变 None ⇒ `is_in_entry_protection` 立即 False ⇒ 盲窗本来就会消失。**收益在「以后每一次有 open_date 的时刻」**，不在明早。重启消除的是「这一次」，本修复消除的是「这一类」。
  ⚠ **两条残留未修**：① stale 闸（`:3654`）仍排在 `force` 之前 ⇒ 行情陈旧时灾难止损依旧过不去；② `_pos_meta.open_date` 仍不持久化 ⇒ 重启后 T+1 与保护期同时失效。
- [P0] [INFO] **★ 顺序修正：stale 守卫排在 `force` 之前 ⇒ 行情陈旧时账户没有任何卖出通道（连组合强平也过不去）**：运行版 `_handle_sell` 的拦截顺序为 `:3654` stale `return False` → `:3681` 保护期 `if not force` → T+1。因 stale 判定在 `force` **之前**，即使 `daily_hard_stop_flatten` 传 `force=True` 也会被 stale 挡回 ⇒ **报价陈旧期间「强制平仓」在代码层面不成立**，而今天 15:02:02 已实测触发过一次（33 只陈旧 2 分钟、账户级 ≥60% 即置位）。这不仅影响 300308，是整个账户的卖出通道可用性问题 — `engine/event_engine.py:3654/3681/2165` — 发现于 risk-controller 特殊日裁决 2026-11-03
- [P0] [INFO] **★ 代价升级：盲窗把回撤预算使用率从 75.7% 推到 96.9%**：正常路径最坏 −15.14%（预算 75.7%）；**若开盘掉进 30 分钟盲窗**，按创业板 −20% 跌停（开 384.16）计 ⇒ −11,741 元 / 权益 19.22% / 组合回撤 **−19.39%** / 预算使用率 **96.9%**，离穿线只剩 0.61pt；300308 需回到 **380.24** 才穿 −20%（此点只 `_halt` 停买、不平仓）— 定价来源 risk-controller，算式：`61,073.65` 起、`500×2.884+现金 11,611.65` 为不变部分 — 发现于 risk-controller 特殊日裁决 2026-11-03
- [P1] [INFO] **今晚不存在「热读就能修好」的路径**：`ENTRY_PROTECT_MINUTES` 由 `event_engine.py:376` 一次性取模块常量 ⇒ 改必重启；`exit_exempt_codes`/`hard_stop_pct` 虽热读，但把 300308 **移出豁免清单反向有害**（会连 −18% 硬止损一起丢掉，剩下的趋势破位在保护期内照样被挡）。⇒ 实质只有三条路：改代码+重启 / 全局关保护期+重启 / 今晚不动、盘中手动 — 发现于 risk-controller 特殊日裁决 2026-11-03
- [P0] [INFO] **[已裁决] 「跌破 411.29 就会卖」的说法撤回**：411.2857 是**触发价不是出口**。四道门可挡/可拖：① 建仓保护期（见上，明早 09:30–10:00 盲窗）② 行情陈旧 `:3662-3664 return False`（**今天 15:02:02 已实测触发一次**）③ 无 tick 时豁免仓**跳过** `_daily_fallback_exit`（`:2202-2216`）⇒ 零退出评估 ④ 限价按 tick 价挂 `core/broker.py:317-322`，急跌买盘在价下 ⇒ 实际成交价不受 411.2857 约束。**不挡**：T+1（11-04 解禁）/ 现金夹紧 / halted — 发现于 risk-controller 裁决 2026-11-03
- [P0] [FORBIDDEN] [待 OWNER] **组合层唯一会强制减仓的机制对 78.63% 的敞口结构性失效，且触发点只卖 2.4%**：`_split_flatten_candidates:2482-2500` 把豁免仓剔除 ⇒ 当日亏 −6% 时 `daily_stop_flatten_pct` 只平 510050 500 股（≈1,442 元 = 总资产 **2.4%**），300308 原封不动；触发价 ≈ **443.56**（300308 相对平开 −7.6%）。−20% 断路器对应 300308 ≈ **380.24**，只 `_halt` 停买不 flatten（`risk/manager.py:159-160`）⇒ **整段下跌过程里账户唯一的自动动作是卖掉那 1,442 元 ETF** — `engine/event_engine.py:2482-2500/2167-2177` — 发现于 risk-controller 裁决 2026-11-03
- [P1] [SAFE] **面板/`engine_state` 把 `stop_price=247.716` 当「止损价」暴露，真实操作止损是 411.2857**（差 33.6%，方向是**仪表盘显示比实际更宽松**）。payload 见 `engine/event_engine.py:4051 "stop_price": p.stop_price` + `core/data_models.py:105` 注释「入场时按 ATR 计算的保护止损价」；`engine_state` 实测该字段 = 247.716。与 09:00 那条「ATR×6 是死字段」同源，但**消费端是仪表盘而非日志** ⇒ 建议面板改显示 `avg_cost×0.82`（豁免仓）或 `hard_stop_pct`（普通仓），ATR 值仅作参考 — `core/data_models.py:105` + `event_engine.py:4051` — 发现于 risk-controller 裁决 2026-11-03
- [P0] [INFO] **300308 现在只有一个出口（−18% 硬止损 411.29），三个逃生口堵死两个半**：① 趋势破位 → 被 `exit_exempt_codes=["300308.SZ"]`（`settings.py:694`，10-30 OWNER「长期持有」指令）豁免；② 单日暴跌 −3.25% → 同被豁免；③ 组合 −6% 日内强平 → `_split_flatten_candidates:2482` 把豁免仓剔出强平名单，**只平 510050（≈1,440 元 = 2.4%）**；④ −20% 断路器只停开仓不平仓。⇒ 明日 300308 从 480.20 到 411.29 之间**引擎不会有任何动作**。尾部量化：走满硬止损亏损 9,027.90 = 权益 14.8%，组合 −15.1%（不触断路器 ⇒ **正因为不触断路器，才没有任何机制中途阻止它**）— `engine/event_engine.py:2223-2236/2482/2167-2177` + `config/settings.py:694` — 发现于 特殊日复盘 2026-11-03
- [P0] [INFO] **[已裁决] 09:00 那条「止损可能低报 4.7 倍」的 P0 今日未兑现，建议关闭**：分歧点是券商回真实成交价还是摊薄成本。实测 `engine_state` `avg_cost=501.5679`、`fills#390` 成交价 501.50 ⇒ 走**乐观分支**，止损 411.29 / 亏损 9,027.90（14.8% 权益），**不是**悲观分支的 159.28 / 38,572 元（61.0%）。触发条件仍在（下次券商回摊薄 avg_cost 会重演），只是今天没赶上 — `storage/qmt.db::engine_state` + `fills#390` — 发现于 特殊日复盘 2026-11-03
- [P1] [INFO] **`owner_reentry` 的 docstring 说「本通道 = 绕过单仓上限 40%」，实际绕过的是全部信号闸门**：`_owner_reentry_step:2611-2720` 直接构造 `Signal(score=0.0)` 调 `_handle_buy`，**从不评估 `trend_up`（收盘>MA60）**。300308 自 9-22（927.72）跌到今日 480.20（**−48.24%**），MA20 648.86 / MA60 814.18，**「收盘<MA60」自 9 月起持续为真** ⇒ 若有该检查，10:59 这笔买入会被拒。docstring 措辞会让审计者以为信号闸门仍在，属**口径误导**（`settings.py:648` 注释同样只提单仓上限）— `engine/event_engine.py:2613-2621` + 实测 MA60 — 发现于 特殊日复盘 2026-11-03
- [P1] [SAFE] **本地日线在 510050 上截断但不报错**：19:06 首拉 `510050.SH` 日线**只到 20261030**（缺 11-02、11-03）、今日 1m 只有 **90 根**（止于 11:00），末值 **2.863**；手动 `download_history_data` 后变 **241 根 / 2.884**（与引擎一致）。⇒ 引擎用 tick 定价不受影响，但**任何依赖本地日线的复盘/回测会静默少 2 天，并给出「看起来正常」的错价**（2.863 vs 真值 2.884）。建议日线面板加载后显式断言「末根日期 == 最近交易日」，当前靠人肉补下载 — xtdata 实测 — 发现于 特殊日复盘 2026-11-03
- [P1] [INFO] **收盘边沿 miniQMT 推送断流已实测发生**：`15:02` 日志「33 只标的快照已陈旧 2 分钟（阈值 2 分钟），已暂停建仓」。今日发生在收盘前无影响；但若断流落在 14:57，**R20「陈旧报价冻结盯市」会把谷值 61,071.65 冻结成一个看起来没那么糟的读数**（保护机制反过来掩盖真实跌幅）。与 `MARK_STALE_FREEZE_VALUATION` 的交互需专项验证 — `logs/quant_system.log`(15:02) — 发现于 特殊日复盘 2026-11-03
- [P2] [SAFE] **`config/settings.py:654` 与 `:667` 对「改本文件是否需重启」自相矛盾**：`:654`（既有）写「热读，无需重启进程内的下一次 step」，`:667`（11-03 我写的 min_price 注释）写「**改本文件必须重启引擎才生效**（全仓无 importlib.reload）」。实测支持后者：`0~545` 只在 10:58:26 重启后出现。建议统一为「需重启」并删掉「热读」表述 — `config/settings.py:654/667` — 发现于 特殊日复盘 2026-11-03
- [P2] [SAFE] **`range_pct` 字段语义与名不符，连续两日被误读**：11-03 记 **−8.22**，而「收盘/昨收」是 **−3.38**、盘中峰谷是 **−4.35**；11-02 的 −5.01 同型。审计者看到 −8.22 会以为是「最大回撤 8%」。建议改名 `intraday_range_pct` 或加注释 — `logs/evolution_ledger.jsonl` 11-02/11-03 行 — 发现于 特殊日复盘 2026-11-03

- [P0] [FORBIDDEN] [待 OWNER · **09:30 前需一句话**] **`owner_reentry` 的「唯一生效止损」可能落在 −70.8%，而 OWNER 11-02 授权时的认知是「−18% 兜底」**：豁免路径读 `pos.avg_cost`（`:2223-2236`），而该值每轮被券商摊薄成本覆盖（`:4247`）⇒ 若券商返 194.246，止损价 **159.28**，亏损 **38,572 元 = 61.0% 权益**；若返 545 则 446.90 / 9,810 元 = 15.52%。**4.7 倍差距盘前不可验证**（账户已 100% 现金）。另：日志打印的 `stop_price=269.197`（**实测 ATR 8.4343%**，08:47 现场跑 `daily.atr_pct()`；`settings.py:650` 注释写的 8.03% 已过时）是装饰，退出判定从不读它。**风险实质变化 ⇒ 原授权不自动延续** — `event_engine.py:2223-2237/4247` + `settings.py:657-666` + `reports/DAILY_REVIEW_2026-11-03.md` §八 P0-6 — 发现于 DAILY-REVIEW 2026-11-03
- [P0] [SAFE] **账本双重静默失效：300308 昨日 54,861 元真实卖出在 `trades` 整笔消失、`pnl` 记 0.00**（真实约 **−1,966**，区间 −1,961~−1,971）。根因 `_replay_fills:376-381`（`q=min(100,0)=0` ⇒ `pnl=0` 但 `sell_amt` 无条件累加）+ `_match_trades:436-449`（`entry_qty=0` ⇒ 整笔丢弃）。上游是 **OWNER 10-30 手工买回不入账**（`settings.py:688` 原文「2026-10-30 手工买回」；`BUY 300308` 在 `orders` 表 **0 笔**）。极差 **≈37,402 元**（−1,966 / +35,436 摊薄虚拟 / 0）。修它必须同步改 `tests/test_batch_c_fixes.py:55-58`（该用例把「裸卖单被丢弃」写成期望行为）— `review_daily.py:376-381/436-449` + `logs/review_2026-11-02.json`(realized_by_code) — 发现于 DAILY-REVIEW 2026-11-03
- [P0] [FORBIDDEN] **熔断恢复重置 `peak_asset` ⇒ 「−20% 回撤预算」在 live 下不可加**：`manager.py:353` 解除时 `_peak_asset = total_asset`。今日 −20% 线从 **50,566.35** 起算（按未重置的旧 peak 66,542.31 应为 53,233.85）⇒ 凭空多给 **2,667.50 元**下行额度；「峰值→谷底」累计 **−24.01%** 在本地读数仍显示 −20%；连 3 次 −6.5% 强平后累计 −18.3%，读数始终只有 −6.5% ⇒ **「没越 −20% 线」不能当安全证明** — `risk/manager.py:341-357` — 发现于 DAILY-REVIEW 2026-11-03
- [P0] [FORBIDDEN] **`kpi4_live` 直接吃坏掉的 `excess_pct`**：`kpi_targets.py:107` 判 `live_excess_pct >= 0.0`，输入来自 `benchmark_feed.py:120`（真实 −1.36%，报 −72.21%）⇒ 一旦 `--live` 注入即判 **−72.21pt**。这比面板显示严重：它接进了 **EVOLVE 裁决闸门**；`tests/test_benchmark_and_rerank_b4b5.py:143-148` 只用合成值测过 — `config/kpi_targets.py:107` + `strategy/_evolve_wf.py:1033` — 发现于 DAILY-REVIEW 2026-11-03
- [P0] [FORBIDDEN] **`ATR×6` 止损是死字段（不是「止损被放宽」，是「日志打假数字」）**：`event_engine.py:3433-3441` 按 `max(18%, atr_pct×6)` 算出 `stop_price` 并在 `:3517` 打印，但 `exit_mode="trend"`（`settings.py:337`）下 `trend_strategy.py:230-245` 只读 `avg_cost + hard=−0.18`，**从不读 `position.stop_price`**（只有 scalp 分支 `:279` 读）。全仓 `pos.stop_price` **只有 2 处写入（`:2039`/`:4253`）、0 处读取做退出判定**。实测 300308 `atr_pct=8.4343%` ⇒ 日志打 **269.197（−50.6%）**，而实际生效线是 `avg_cost×0.82`。**翻日志看到「止损 269.197」会误以为有 51% 缓冲** — `event_engine.py:3433-3441/3517/2039/4253` + `strategy/trend_strategy.py:230-245` — 发现于 DAILY-REVIEW 2026-11-03
- [P0] [FORBIDDEN] **mock 合成数据翻转了 `regime` 标签（同一份 T−1 数据，两次启动结论相反）**：11-02 **21:39:03** 正常 live 启动判 `regime=up`；同日 **23:05:10** mock 窗口判 `regime=down`。`regime` 是二值闸门（决定清仓/放行）⇒ mock 窗口不只产出假成交，**它换了一套策略行为**。任何在该窗口内产生的「实盘反例」**连方向都不可信**。建议：mock 模式强制标 `regime_basis="mock"` 并禁止写入 notices/账本 — 11-02 双次启动日志 + `ALLOW_MOCK_TRADING` 无互斥保护 — 发现于 DAILY-REVIEW 2026-11-03

- [P1] [INFO] [待 OWNER] **日内 −6% 强平跳过豁免仓 ⇒「熔断触发」与「风险下降」脱钩**：`_split_flatten_candidates`（`:2482`）把 `exit_exempt_codes` 剔出强平名单，`:2167-2177` 只发一条 WARNING。今日若 300308 成交后触 −6% 线（59,415.46 / 300308≈507.08），**强平只卖掉 510050（≈1,420 元），86.3% 仓位原封不动**；叠加 T+1（`:3663-3677`）今日买入今日不可卖 ⇒ 任何止损今天都是纸面动作。这是 11-02 15:40「强平排除豁免仓」的设计副作用 — `event_engine.py:2482/2167-2177/3663-3677` — 发现于 DAILY-REVIEW 2026-11-03
- [P1] [SAFE] **下单失败无退避 ⇒ 3 秒报单风暴（今日可复现）**：`_pending_sells` 只在下单成功拿到 oid 后登记（`:3213-3224`）⇒ 无 oid 的失败**永不登记 ⇒ 去重形同不存在 ⇒ 每 3 秒重发**。现场：10-30 有 **656 笔委托 / 空 oid 643 / 受理 13 / 成交 2**（三个比率必须分开报，61% 空 oid 是连亏+空仓时的启动噪声）。反证：10:07:45 拿到 oid 后立刻静默 76 分钟 ⇒ 去重逻辑本身没问题，缺的是「失败也退避」— `event_engine.py:3658-3660/3213-3224` — 发现于 DAILY-REVIEW 2026-11-03
- [P1] [SAFE] **`core/qmt_health.py:339-345` 编造病因**：报「仓位系数 = 0（连亏降仓已压到 0）⇒ 无法开新仓」，而 `consec_loss=0`（`risk_state`）。真相是 `position_scale` 是 property（`manager.py:517-525`），`halted` 时返回 0.0 ⇒ 当前 0.0 **只是 halted 的投影**，与连亏无关，**也不存在僵尸冻结**。该条 `level=bad` 使 status=bad ⇒ **每次 halted 恢复前都会误导诊断** — `core/qmt_health.py:339-345` — 发现于 DAILY-REVIEW 2026-11-03
- [P1] [SAFE] **观察篮 `manual_entry_codes=["510050.SH"]` 悬挂 4 个交易日 + 注释自相矛盾**：今日 09:30 首轮会自动买回 ≈1,435 元（2.27% 权益，最坏 −143.5 元 = 0.23%，非资金风险）。`settings.py:562` 称「改清单无需重启」而 `:625-627` 称「需重启」，`:654` 第三处又错 —— **全仓无 `importlib.reload` ⇒ 改文件必须重启**；`:567` 的「观察篮已清空（09-30）」标题与 `:613` 实际值直接冲突，误导所有后续审计 — `config/settings.py:562/567/613/654/625-627` — 发现于 DAILY-REVIEW 2026-11-03
- [P1] [INFO] **日线上下文开盘后被冻结一整天**（比「T−1 滞后」更强）：`daily_context.py:36` `FEATURE_TTL_SEC=6h` + 刷新点只有启动/新交易日首轮（`:1244-1249/:2096-2102/:738-739`）⇒ 11-02 全天仅 09:30:35 一条刷新。当日越线的标的（603501.SH 83.15>MA60 82.52）**当天进不了候选**。今日边际影响≈0（动量闸门已 0/33）。★ 同型风险：TTL 只认「时间」不认「数据源切换/交易日推进」⇒ 若收盘后刷新过一次并缓存，重启后会把「收盘价=真值」锁死 — `strategy/daily_context.py:36` + `engine/event_engine.py:1244-1249/2096-2102` — 发现于 DAILY-REVIEW 2026-11-03
- [P1] [INFO] **北向闸门已 fail-open 70 天 ⇒ 历史「northbound 证伪」结论无效**：`northbound_mode="gate"`（`settings.py:536`）是**开着的**，但数据序列末端停在 **20260825**（距 11-03 已 70 天 > `nb_stale_days=10`）⇒ `nb_gate_credible` 判陈旧 ⇒ 强制放行。现场：`logs/quant_system.log` 有 **77 条**「北向闸门已 fail-open（stale）：序列末端 20260825」。⇒ 2026-08-25 之后任何 `northbound_mode` 网格都在「闸门恒放行」状态下跑，`dSh≈0` 是**测错了对象**，不是参数无效 ⇒ MEMORY §三 该条应改标「结论无效」而非「已证伪」。守卫逻辑本身正确（`:2394-2403`），缺的是数据源修复 + 重跑 — `config/settings.py:536/551` + `data/northbound_cache.py::nb_gate_credible` + `logs/quant_system.log`(77 条) — 发现于 DAILY-REVIEW 2026-11-03
- [P2] [SAFE] **`settings.py:650` 注释里的 `ATR14 = 8.03%` 已过时**（实测 8.4343%），且该值会进入 `stop_price = price×(1−max(0.18, atr×6))` 的计算叙述 ⇒ 任何人引用该注释都会算错（本轮团队 3 人各算出 282.42/269.34/446.90）。建议改为「运行时取值，见 `daily.atr_pct()`」不再写死数字 — `config/settings.py:650` — 发现于 DAILY-REVIEW 2026-11-03
- [P2] [SAFE] **`pos.stop_price` 在 `avg_cost ≤ 0` 时「每轮重算且永远设不成」**：`:2030-2039` 守卫 `if pos.stop_price <= 0` + 赋值 `round(pos.avg_cost*(1-wide),3)` ⇒ `avg_cost ≤ 0` 时结果恒 ≤0 ⇒ 守卫恒真 ⇒ 「只设一次」退化为「每轮都设、永远未设置」。与「无人读」叠加 ⇒ 该类票上该字段**不写不读也无值** — `engine/event_engine.py:2030-2039` — 发现于 DAILY-REVIEW 2026-11-03
- [P1] [FORBIDDEN] **`kpi3`（最差单折收益）是划分依赖指标，却是唯一还在闸门上的 active KPI**：`kpi_targets.py:79-86` `status="active"`、`metric="worst_fold_return"`（取所有折的**极值**，样本量 1），note **无任何划分依赖提示**。对比：`kpi1` 已有「R18 已证其划分依赖」标注（`:52`）、`kpi2` 已 `deprecated`（`:67`）⇒ **唯独 kpi3 漏了，而它是「防尾部爆仓」那道闸**。`gate_verdict:149-160` 只筛 active ⇒ 硬约束。成员实测（未复算）：同一净值曲线只改 fold 切分，`worst_fold_return` **−9.97% PASS → −17.9% FAIL**。⇒ MEMORY「KPI#3 PASS」是某套划分下的运气。处置建议并入 D4/D9 裁决：加「3 种切分取中位数」稳健版或改 `proposed` 移出闸门，**不建议直接删** — `config/kpi_targets.py:79-86/149-160` — 发现于 DAILY-REVIEW 2026-11-03
- [P1] [SAFE] **`stop_price` 有两套 `wide` 口径、先写者胜 ⇒ 为「修 P0-2 让止损可读」埋雷**：`grep "wide = "` 仅两处但定义不同 —— `:4223`（券商同步）`abs(hard_stop_pct)=0.18`；`:2037`（每轮盯市 trend 分支）`max(abs(hard), ap*6.0)=0.5061`。两处同受 `if pos.stop_price <= 0` 守卫 ⇒ 先到先得。10-30 实际落盘 `159.701`（=`194.757×0.82`，DB 实证）⇒ ATR 那套 96.199 永久失效，**相差 1.66 倍**。当前无害（字段无人读），但修 P0-2 前必须先统一单一事实源，否则「让止损生效」的效果取决于启动顺序 — `event_engine.py:2037/4223/2033/4252` — 发现于 DAILY-REVIEW 2026-11-03
- [P1] [SAFE] **live 下 `engine_state.positions` 只写不读 ⇒ 持仓历史不可回溯**：`_restore_engine_state(restore_ledger=False)` 在 `:3952 return`，不恢复 positions/cash（设计正确：live 以 broker 为权威源），副作用是 `engine_state` 的 id 恒为 1 覆盖写 ⇒ **只有「当前一帧」，无历史**。本轮为确认 10-30 的 `avg_cost` 只能靠现金轨迹倒算 + 一个**偶然存在的** `qmt_pre_pxtype_*.db` 备份。建议新建 append-only `position_snapshots(ts, code, qty, avg_cost, last_price, stop_price, peak_price, source)`；否则每次追问「当时成本是多少」都要靠运气 — `event_engine.py:3900-3952` — 发现于 DAILY-REVIEW 2026-11-03
- [P1] [SAFE] **[今日 09:31 必须执行一次] `halted` 必转 false，判据是「`_maybe_recover` 有没有被调用」**：已逐项核实 —— `halt_reason="daily_stop -89.25%"`（不以 `max_drawdown` 开头）⇒ 走 `else` ⇒ `halt_recover_days=1`（`settings.py:899`，**不是** `dd_recover_days=3`）；`halt_day="2026-11-02"` ⇒ `held=1` ⇒ `1>=1` **今日必恢复**（`risk/manager.py:343-348`）。**四列必须同时翻**：`halted→false` / `halt_reason→""` / `asset_basis→"broker"` / `position_scale→1.0`（三缺一 = 未恢复干净）。**若 09:31 仍 `true` 且引擎已有 tick ⇒ P0，当日整天零买入能力。** 探针 `logs/_tmp/_probe_0931.py` 已就绪（只读）。另附口径可疑（P2）：由「回撤」触发的 `daily_stop` 走**日亏类 1 天**冷却，而真 `max_drawdown` 要 **3 天** —— 同为回撤、两套冷却，仅由字符串前缀决定 — `risk/manager.py:341-357` + `settings.py:891-899` + `engine_state.risk_state`(ts 11-02T20:08:20) — 发现于 DAILY-REVIEW 2026-11-03
- [P0] [SAFE] **[今日 15:40 就会出错] `total_pnl` 把未实现盈亏算两次 —— 回补建仓后立即生效**：`review_daily.py:1647` `realized_eff = risk.get("last_daily_pnl")`，`:1650` `total_pnl = realized_eff + eod["unrealized"]`。**但 live 下 `daily_pnl` 本身就是「总资产 − 当日开盘资产」在每轮 `on_asset_update` 重算（含未实现盯市）**，不是注释所称的「逐笔 SELL 累加」（`:1649` 注释与实现不符，同 MEMORY「注释声称一个口径、实现是另一个」）。⇒ 有持仓时 `total_pnl` **双重计数浮亏**。**11-02 恰好全现金（`unrealized=0`）所以未暴露**；今日回补 300308 后 `unrealized≠0` ⇒ 今晚复盘数字即错。旁证：11-02 `realized_eff=-1,475.86`（open-to-close，含隔夜跳空 +999.42）而权益变化仅 **−476.44**（close-to-close），两者差正是那 999.42 ⇒ 同一份报告里 `total_pnl` 与 `total_return_pct` 口径不一致。修法：live 下 `realized_eff` 应取 `pnl["realized_total"]`（按 fills 重算）或显式区分 `equity_pnl` 与 `realized_pnl` 两字段 — `strategy/review_daily.py:1647-1650/1468` — 发现于 DAILY-REVIEW 2026-11-03
- [P1] [SAFE] **全现金日的 `eod_is_close` 误杀正确资产值，并写假成因（已复现）**：`record_ledger.py:430` 判据只有 `hhmm>=1500`，不看持仓。全现金日（`positions_count=0`）资产≡现金，**11:29 的值与 15:00 完全相同**。复现（临时库 `('2026-11-02T11:29:14',63207.98,63207.98,0.0,0)`）：`eod_asset=63207.98`（值对）/ `eod_is_close=False`（判成错）/ `note='[EOD 非收盘]...该 eod_asset 为盘中值'`（**假成因**）。后果：① 用 `is True` 过滤的消费方会**剔除正确收益值**，与「拆闸提样本量」目标冲突 ② 假成因误导诊断（让人以为那天休眠，实际值本来就对）。**暴露面不是边界**：`10-12~10-28` 有 12 个零暴露日全是此类。修法：`positions_count==0` 时判 `True` + `eod_basis="cash_only"`，测试与 `test_ledger_eod_freshness.py:84` 并存 — `strategy/record_ledger.py:403-436` — 发现于 DAILY-REVIEW 2026-11-03（**归属 data-ledger-auditor**，我先前误驳，已更正）
- [P2] [SAFE] **孤儿卖出修复的两处同型残留（已修「整笔孤儿」，以下未修未测）**：① **部分孤儿** —— `0 < 账本可卖量 < 卖出量` 时，未匹配那部分的盈亏仍被静默忽略（当前只处理「完全找不到建仓腿」的整笔孤儿，以免改变非孤儿行数字；11-02 无此形态）② **非当日孤儿只算不报** —— `_replay_fills` 对回溯窗内非目标日的孤儿（如 10-30 那笔 300308）不记 `orphan_sells`、不告警，账本缺成本仍是盲区 — `strategy/review_daily.py::_replay_fills`（501a151 后） — 发现于 DAILY-REVIEW 2026-11-03
- [P1] [SAFE] **账本三层审计：数值层干净，语义层/元数据层有洞**（实跑 25 行核出）：① `eod_ts` / `eod_is_close` 覆盖率 **1/25 = 4%**（`no_eod_ts=24`）⇒ 契约 §3.3.4「消费方须检查 `eod_is_close`」在 **96% 行上是空条款**，属「实现正确 ≠ 数据受保护」（与 P1-1 同型）② `range_pct`（`record_ledger.py:516`）语义是 **`eod/base−1` 距模式基线累计收益率**，不是「当日振幅」，**命名误导** ③ 逐行复算仅 1 行不自洽：`2026-09-29` 落盘 **+2.04** vs 应为 **−2.12**（差 **4.16pt**），因该行走 `source="review"` **绕过了 `:516` 算式** ⇒ 同字段双写入者 ④ **正面样本**：`prev_asset` 链 0 断链，09-22 是「缺就写缺」的正确实现（无快照则早退，不编 0 不沿用） — `strategy/record_ledger.py:403-436/516` + `logs/evolution_ledger.jsonl`(25 行实跑) — 发现于 DAILY-REVIEW 2026-11-03

- [P2] [SAFE] `bars_1m` 有 **34/38 只冻在 20261030**，而 `log_health` 取 `max(last_bar)` ⇒ 一只新鲜把其余 3 天陈旧全遮掉（**与「面板分母用 paper 基线」「仓位系数报连亏」同族：聚合/派生值掩盖真实原因**）— `scripts/_metric_hygiene.py::log_health` — 发现于 DAILY-REVIEW 2026-11-03
- [P2] [SAFE] 10-12~10-28 共 **13 个零暴露日**（`eod_asset` 恒 972,533.60、`eod_positions_n` 全 0、cash 无变动）**性质是「真实空仓」不是「伪造数据」**，但它们以 0.00% 参与波动率计算 ⇒ 稀释 σ、Sharpe 虚高。建议标 `zero_exposure` 并在 KPI 中排除（**标签问题是真缺陷**）— `logs/evolution_ledger.jsonl` 10-12~10-28 行 — 发现于 DAILY-REVIEW 2026-11-03
- [P2] [SAFE] 账本 `10-13` 只有 **65 条快照、末条 10:20:01**（同为盘中值）却**无 `eod_is_close` 标注** ⇒ `annotate_eod_freshness` 只覆盖近期行，未回填历史 — `strategy/record_ledger.py` — 发现于 DAILY-REVIEW 2026-11-03
- [P2] [INFO] `_sold_today_codes` **跨日泄漏**：只在 `reset_daily()` 清空，只在 `on_fill()` 调用 ⇒ 零成交日若重启，从 `engine_state` 恢复旧值，当日该单会被 `block_reentry` 误拦（方向保守，非资金风险）— `risk/manager.py` `_sold_today_codes` — 发现于 DAILY-REVIEW 2026-11-03

- [P2] [SAFE] [可 CLOSED] 下列条目状态已过期，事实已变化：行 23（上游诱因，pid 7336 复验通过）、行 24（修复未进内存，pid 已变 7336）、行 60（熔断按拼装值开火，已修 `097b440` 且已进内存）、行 61（单次空持仓零确认，已修 `60497d8`，与行 23 同主题去重）、行 34（账本 prev_asset，已修 `3457cb3`，11-02 行 −0.75% 已验证）、行 36（动量脚本 as_of 硬编码，已修 `3457cb3`）— 本轮复核 — 发现于 DAILY-REVIEW 2026-11-03
- [P2] [INFO] [措辞需改] 行 28 说面板 `excess_pct`「符号相反」**诊断有误**：代码是 `account − universe`（正=跑赢），方向正确；真正错的是**分母**（1e6 vs 66,542.31）与**窗口**（`since` 取 `MIN(ts)` 抓到 paper 段 09-22，须改 `WHERE mode='live'`），把 −1.36% 放大成 −72.21%（53 倍）。**按「符号相反」去修会修错地方** — `engine/benchmark_feed.py:88-91/106/120` — 发现于 DAILY-REVIEW 2026-11-03

> **【2026-11-02 17:00 分诊汇总 · 工程维护】** 当日 **3 个 P0 全为资金安全级**（假强平真实成交 + 面板口径反号 + 修复未进内存），均 **FORBIDDEN 待 OWNER 处置**；**SAFE 已修 2 项**：① 账本 `prev_asset` 穿透（`record_ledger.py` + 已写入的 11-02 行数据修正，`run_all` 754/754 全绿）；② `_why_no_entry_momentum.py` 标签硬编码改取实际末根日期。回归 **754/754 全绿**；DB 27.9MB / 日志均 <100MB，无需裁剪或轮转；notices 当日 48 行无同模板数百次噪声。

- [P0] [SAFE] [已修 60497d8 · **已生效**：guardian 23:04:24 拉起引擎 pid 7336，启动时刻晚于 `event_engine.py` mtime 22:53:48] **假强平事故的「上游诱因」已堵**：`_sync_broker_positions` 原先对**单次**「券商返回空持仓」零确认 ⇒ 一次 `[]` 就把本地持仓全部归零 ⇒ `_total_asset()` 塌成纯现金（= 11-02 10:34 事故第 2 步）。现改为**连续 `SYNC_EMPTY_CONFIRM_N`(=2) 轮空返回才确认**，中途出现非空即清零重计；确认为真空仓时上首屏 `[ERROR][风控]` 告警（此前只有一行 logger.warning，首屏不可见）。为什么必须单独修：20:11 那次只堵了下游（熔断不再按错数字开火），但本地账本被清空后策略会**误判空仓** ⇒ `max_positions` 失效、可能对同一标的重复买入。回滚：`SYNC_EMPTY_CONFIRM_N = 1`。回归 **782/782**（新增 `tests/test_sync_empty_confirm.py` 7 例 + 改写 2 个旧契约用例，均含正向对照「券商连续确认后仍会清账」）— `engine/event_engine.py::_sync_broker_positions` / `SYNC_EMPTY_CONFIRM_N` — 发现于 总设计师体检 2026-11-02
- [P0] [FORBIDDEN] [已修 2026-11-02 20:11 · 引擎重启 pid 21492→**14832**，`event_engine.py` mtime 20:08:34 < 启动 20:11:21 ⇒ 新代码已进内存；同日 16:45 那次重启已被本次覆盖] **假强平的修复代码未生效：`event_engine.py` mtime 11-02 14:41，引擎进程 PID 3044 创建于 11-01 15:51:09（差 22h50m），且该文件已改未提交 ⇒ 明日 09:30 仍走旧 `_total_asset()` 熔断路径，同型事故第 4 次的窗口仍开着**（纪律第 9 条「落盘≠生效」）— `engine/event_engine.py:4372/2121` + `Get-CimInstance Win32_Process PID 3044.CreationDate` — 发现于 METRIC-HYGIENE 2026-11-02
- [P0] [FORBIDDEN] [待 OWNER] **明日 09:35~14:50 将以 537~545 限价区间接回 300308×100（OWNER 指令），单票集中度 86%~87% = 策略设计意图（7.2%）的 2.15 倍**：一手 55,000 > `equity×40% = 25,283` ⇒ 现有任何买入路径算出的股数**恒为 0**（算术问题，非价格问题），故新开独立通道 `owner_reentry`（只跳过单仓比例这一条，熔断/日内次数/现金夹紧/行情新鲜度/总资产可信度/单笔金额全保留，`valid_until=2026-11-03` 到期作废）。若开盘直接跌破 537 引擎**只告警不下单**（不接飞刀），是否强接由 OWNER 决定 — `config/settings.py::owner_reentry` + `engine/event_engine.py:2600/2719` + `reports/OWNER_REENTRY_300308_2026-11-03.md` — 发现于 OWNER 直连 2026-11-02
- [P1] [SAFE] [已修 2026-11-02] **`owner_reentry` 未登记读点契约 ⇒ `test_search_space_contract` 立即报红**（「实测仅实盘但未登记」）：已在 `strategy/_param_audit.py::CONTRACT_LIVE_ONLY` 登记为 live-only（回测无「外部指定价位」概念），按 R43 第 23 条该通道结论只能记**未验证**、不得记已证伪 — `strategy/_param_audit.py:163` — 发现于 OWNER 直连 2026-11-02
- [P2] [INFO] [待 OWNER] 明日收盘后应把 `STRATEGY_PARAMS["owner_reentry"]` 置回 `{}`，避免配置长期悬挂（虽已 `valid_until` 兜底，但悬挂配置会让下轮审计把「惰性参数」误读为「已启用」） — `config/settings.py` — 发现于 OWNER 直连 2026-11-02
- [P0] [FORBIDDEN] [待 OWNER] [已并入下方 P0 行51·同主题] **面板基准条三数字全错且符号相反**：`account_pct` 分母仍是 `INITIAL_CASH=1,000,000`（paper 初始资金）⇒ 显示 **−93.68%**，live 真实 **−5.01%**（差 88.67pp）；`excess_pct` 显示 **−72.21pp** 而真实 **+16.46pp（跑赢等权）**；`cash_pct` 硬编码 `0.0`（今日 100% 现金）。10-29 切 live 只重定了账本基线、漏了面板；且 `tests/test_benchmark_and_rerank_b4b5.py:250` 把 `cash_pct==0.0` 焊死为断言 — `engine/benchmark_feed.py:106/120` + `config/settings.py:887` — 发现于 METRIC-HYGIENE 2026-11-02
- [P1] [SAFE] [已修 2026-11-02] **定时任务归集 10 → 3**（OWNER 指令「核实所有定时任务，归集为每日一次复盘」）：新建 `DAILY-REVIEW`（交易日 15:40，合并原 7 个工作日任务）；PM-EVOLVE 并入周日 SYNTHESIZE；其余 8 个 PAUSED（`[已归集→…]`，可回滚）。代码同步 `scripts/_loop_liveness.py::TASKS`（3 条）+ `tests/test_loop_liveness.py`（加「不得登记已停用 id」断言）+ 契约 §五 重写，`run_all` **775/775 PASS** — `docs/AUTOMATION_CONTRACT.md` §五 + §六 变更日志 — 发现于 OWNER 直连 2026-11-02
- [P1] [INFO] [待 OWNER 知情] **归集带来两个盲窗**：① 盘中无小时级 LLM 巡检（改由 `core/guardian.py` 300s + `health_check --start` 承担，DAILY-REVIEW 只做事后回溯）⇒ 盘中失效最早 15:40 才知道；② 无 09:05 盘前体检（改为 B 块「源码 mtime vs 进程启动时刻」事后核验 + 次日自检清单）⇒ 若昨晚改代码今早带旧代码开盘，要到 15:40 才发现。**补偿纪律**：需重启才生效的改动，改完当轮就重启并核对 mtime < 启动时刻 — `docs/AUTOMATION_CONTRACT.md` §5.1 — 发现于 OWNER 直连 2026-11-02
- [P2] [INFO] **归集期的核实结论（与原审计不同，供后续参照）**：`logs` 显示工程维护 11-02 17:00 **实际跑过**（INBOX 有分诊汇总、列了 3 个 P0 + SAFE 修 2 项），但活性审计判 ③ —— 根因不是审计逻辑错（情况 B 已覆盖「有产物无 memory」），而是**它登记的产品 `reports/DB_MAINTENANCE_*.md` 自 10-22 后从未再生**、memory 停在 10-30 ⇒ 该次运行无法归因。教训：**任务的产品清单必须与 prompt 实际要求产出的文件严格一致**，否则「跑了」也会显示成「没跑」 — `scripts/_loop_liveness.py:104` 注册表 — 发现于 OWNER 直连 2026-11-02
- [P2] [INFO] [待 OWNER] 510050.SH 日线末根仍停在 **20261030**（38 只订阅里唯一，其余 37 只已到 20261102）：它是 `VALIDATION_UNIVERSE` 唯一成员、按设计只收 tick 不进日线上下文，故未刷新属预期；但今日被强平卖出后若经观察篮补回，首日将无日线特征 — `xtdata.get_market_data_ex` 37/38 分档 — 发现于 METRIC-HYGIENE 2026-11-02
- [P2] [SAFE] 假基数行判据需修正：今日漏斗「宇宙 37」< 订阅 38 应判**通过**而非假基数（少的 510050 是设计排除）⇒ 判据应为「比 `len(UNIVERSE)+len(dynamic.active)` 而非比订阅总数」，否则每次都会误报 — `reports/measure_hygiene/HYGIENE_20261102.md` §一 #4 — 发现于 METRIC-HYGIENE 2026-11-02
- [P1] [SAFE] **账本 `prev_asset` 穿透未修正值（OBSERVE 今日 P1 的根因补充）**：`record_ledger.eod_asset(day)` 只查 `equity_snapshots`，而 10-30 的修正（65,748.42→63,684.42）只写进**账本行**⇒ 11-02 行 prev 又取回 65,748.42，当日收益记 −3.86% 而正确链接为 **−0.75%**（虚增亏损 3.11pt / 2,064 元）。`stale_mark_suspect_day` 拦不住：10-30 有 2 笔成交 ⇒ 判据①不成立（「盘中快照冒充收盘」与「陈旧盯市」是同一根因的两个判据）。修法：prev 优先取上一行账本的 `eod_asset`，DB 仅兜底 — `strategy/record_ledger.py:263/523` + `logs/evolution_ledger.jsonl`(2026-11-02) — 发现于 METRIC-HYGIENE 2026-11-02
- [P1] [INFO] [待 OWNER] **引擎全天只在 09:30:35 刷新一次日线上下文 ⇒ trend_up 全跑在 T−1 日线上，对「当日刚越线」的标的系统性滞后 1 个交易日**：11-02 引擎 `trend_up=1` vs 实盘同源复算 `2`，差 603501.SH（今日 83.15 才站上 MA60 82.52）；按 `drop=1`（末根落到 20261030）复算即逐位回到 1，证明是数据日期差非代码分叉。影响=当天刚转强的票当天永远进不了候选（10-29 引擎 2/复算 1 的反向偏差同源）— `engine/event_engine.py` 日线刷新点 + `logs/quant_system.log` 唯一 1 条「日线上下文刷新完成」(09:30:35) — 发现于 WHY-NO-ENTRY 2026-11-02
- [P2] [SAFE] [待 OWNER] `_why_no_entry_momentum.py` 的 `as_of` 标签**硬编码 `20261027/20261028`**，而脚本每次现拉数据 ⇒ 标签永久停在 10-28、数据实为当日（已核 37/37 收盘价与 `live_close` 逐位相同）。曾导致 10-29 / 10-30 两期日报的动量数字带着错误日期标签（数据对、标签错） — `scripts/_why_no_entry_momentum.py:50` — 发现于 WHY-NO-ENTRY 2026-11-02
- [P0] [FORBIDDEN] [待 OWNER] **live 假强平已造成真实成交（本项目首次资金实际受损）**：10:34:10 `_total_asset()` 退化为纯现金 6,953.34（券商口径静默失效 ≥2min + 回落拼装 Σmv=0），风控算出 `dlp=−89.25%`（真实 −2.07%）⇒ 触发 `daily_hard_stop_flatten` **真实卖出 300308×100 @~548.3 + 510050×500 @2.867**，账户 100% 现金 63,207.98、熔断冷却至明日自动恢复；当日实际 −1,475.86（−2.28%）。三个缺口：A 强平判定无资产合理性校验（需连续 2 轮采样确认）；B Σmv=0 时回落拼装应拒绝输出；C 强平路径 bypass `exit_exempt_codes` ⇒ OWNER 底仓 300308 被卖（vs 10-30 买回价 568.29 亏 ~3.5%）— `reports/R50_AM_LIVE_FAKE_FLATTEN_20261102.md` — 发现于 AM-EVOLVE 2026-11-02
- [P1] [INFO] [待 OWNER] **OBSERVE 复核确认 11-02 假强平，并暴露账本 prev_asset 仍链接旧口径**：review/DB 独立佐证 10:34:10 `risk_state.halt_reason="daily_stop -89.25%"` 而 `account_state.daily_pnl=-1475.86`（真实 −2.28%），300308.SZ 估值塌至 ~0（真实 ~55,300）触发全盘强平、EOD 持仓=0；另 `evolution_ledger.jsonl` 11-02 行 `prev_asset=65748.42` 取的是 10-30 **未修正盘中快照**（契约 §3.3 已修正为 63,684.42），致 KPI#4 当日收益 −3.86% 偏误，修正链接后应为 −0.75%；三口径（review −2.28% / ledger −3.86% / 修正链接 −0.75%）偏差 >1%，符合 P0 扫描项① — `logs/review_2026-11-02.json`(risk.halt_reason/account_state) + `logs/evolution_ledger.jsonl`(2026-11-02) — 发现于 OBSERVE 2026-11-02
- [P0] [SAFE] [已修 2026-11-01] 账本 2026-10-30 EOD 用 **11:29 盘中快照**冒充收盘价（引擎 11:29 后休眠、当日无收盘快照）⇒ 记 −1.19%，真实 **−4.29%**，低估 3.1pt；已按收盘价重算为 63,684.42 并双源核对（券商读数 63,684.34）。**根因未修**：`eod_asset_raw` 仍取「当日最后一条快照」，故新增 `eod_ts`/`eod_is_close` 字段供消费方识别 — `strategy/record_ledger.py::annotate_eod_freshness` + `tests/test_ledger_eod_freshness.py` 6 条 — 发现于盘前准备 2026-11-01
- [P1] [SAFE] [待 OWNER] `_pos_meta`（建仓日 / ATR 止损价 / 目标价 / 峰值）**只在内存、从不持久化** ⇒ 每次重启后 `open_date=null`、ATR 止损退化为 `avg×(1-18%)` 宽幅兜底。实测 510050 真实 ATR 止损应为 2.174，现为 2.353（**放宽 8%**）；300308 由 457→159.281（因 `exit_exempt_codes` 豁免，仅剩 −18% 灾难止损，影响可接受）。修法=把 `_pos_meta` 写入 `engine_state`，需重启生效 — `engine/event_engine.py:512/3298/3974` — 发现于盘前准备 2026-11-01
- [P1] [INFO] [跟踪中] 北向资金缓存已陈旧 **68 天**（末端 20260825），引擎每轮 `fail-open` 并打 ERROR ⇒ 闸门 `northbound_mode="gate"` 名义开启、实际从不拦截（与 09-28 结论「死闸门」一致）。属噪声而非资金风险；改动需 re-baseline（牵连 test_northbound/_evolve_wf/opt_harness），不在盘前动 — `data/northbound_cache.py` — 发现于盘前准备 2026-11-01
- [P0] [FORBIDDEN] [跟踪中] T+1 卖出后同标的立即回买触发 `306050` 报单风暴（11:26–11:30 每 30s 一单全被拒，同时 `BUY 拒绝: pending_sell`）— `reports/R45_AM_LIVE_T1_ORDER_STORM_20261030.md` — 发现于 AM-EVOLVE 2026-10-30；OWNER 2026-10-30 裁定「暂时不动」，现由 `exit_exempt_codes` 抑制 300308 空转，通用修法未做
- [P0] [FORBIDDEN] [已修 10fbfc1] live 日内盈亏曾走「已实现」口径，而券商 `avg_cost` 是摊薄成本（300308=194.246）⇒ 亏 6,000 仍可开仓，日内亏损熔断事实失效；已改权益口径 `_daily_pnl = 总资产 − 当日开盘资产` — `risk/manager.py::on_asset_update` / `engine/event_engine.py` 调用点 — 发现于 R46-b 2026-10-30
- [P0] [SAFE] [已修 010ff14] 启动持仓初始化自带副本 `last_price=avg_cost`，绕过四档兜底并覆盖 `peak/stop/open_date` ⇒ 总资产低估 57.7%（27,812.98 vs 63,684.42）— `engine/event_engine.py::_init_positions_from_broker` → 已改为委托 `_sync_broker_positions()` — 发现于 R46 2026-10-30
- [P1] [FORBIDDEN] [待 OWNER] 5 个自动化曾因各自 prompt 的「必须 paper」门禁自我停机 2 个交易日（PRE-MARKET 无开盘前体检、OBSERVE 无复盘、KPI#4 账本未建立）— `docs/AUTOMATION_CONTRACT.md` §〇 证据表 — 发现于自动化评估 2026-11-01；已建契约+重写 10 个 prompt，门禁改为**模式感知**（不一致只告警不停机）
- [P1] [INFO] [跟踪中] KPI#4 账本 2026-10-29 缺 EOD 快照（14:48 退出）⇒ 首个 live 行是 10-30；跨模式断点已由 `MODE_BREAKS` 钉住（baseline 66,542.31）— `strategy/record_ledger.py` / `logs/evolution_ledger.jsonl` — 发现于 R47 2026-11-01
- [P2] [SAFE] [待 OWNER] 回测面板末端首次并入**当日未收盘 bar**（20261021 11:29，历史末端 20261019）⇒ 建议加守卫「末端 bar 须属已收盘交易日」— `tests/_baseline_tushare_panel.baseline_is_result` — 发现于 R41 2026-10-21（OPEN_DECISIONS P2）
- [P2] [INFO] [待 OWNER] 「正收益折数」类指标已证为划分依赖型噪声（同一条 +218.8% 曲线，切 9 段 77.8%、切 2 段 50.0%）⇒ D4/D9 建议一次性裁决，改为大样本口径（拼接 Sharpe + 月度胜率）— `reports/EVOLUTION_DECISIONS.md` R40 — 发现于 R40 2026-10-30

- [P2] [INFO] [跟踪中] 实盘引擎自检告警：max_single_position_pct=40% × max_positions=5 = **理论敞口 200%** > 现金夹紧后实际上限 95%，配置不自洽；建议降到 ≤19% 使配置自洽。当前由现金夹紧封顶（不会负现金），**无即时风险**，属配置整洁度 — `/api/snapshot` 2026-11-01 15:51 — 发现于 SYNTHESIZE 2026-11-01
- [P0] [FORBIDDEN] [跟踪中] **SYNTHESIZE 评定：T+1 报单风暴仍是本周最大资金风险敞口**（仅 300308 被 exit_exempt_codes 豁免，其他当日买入持仓次日仍会每 3s 重试 306050 拒单；通用修法需 OWNER 授权 + 重启）— `reports/R45_AM_LIVE_T1_ORDER_STORM_20261030.md` §四 A — 复评于 SYNTHESIZE 2026-11-01

- [P0] [FORBIDDEN] [待 OWNER] **盘前就绪 PRE-MARKET 在机器醒着的情况下连续零留痕**：槽位 2026-10-30 09:05 无 memory、无产物，而 `logs/guardian.log` 同期 17 条心跳（最大空档 6 分钟）⇒ **不是整机休眠**，是任务自身/宿主未触发（旧 L3 只会报「停摆」，无法区分两者）— `scripts/_loop_liveness.py` 三态 ③ / `reports/meta/RSI_META_LATEST.md` §〇 — 发现于 R49-RSI 2026-11-01
- [P1] [FORBIDDEN] [待 OWNER] **周中自进化 PM-EVOLVE 错过 2026-10-28 18:00 槽位**（memory 停在 10-21 18:31，窗口 16 条心跳说明机器醒着）⇒ 周中周期空转一周，非休眠所致 — 同上 — 发现于 R49-RSI 2026-11-01
- [P1] [SAFE] [已修] **循环活性审计盲区**：旧 L3 只看 memory mtime（>3 天判停摆），把「在跑但整体停机」判成正常（OBSERVE 10-30 写了 memory「⛔ 硬闸门拦截」却零产出，判「✓ 运行中」）⇒ 已升级为三态+归因，契约 §4.2 同步，`tests/test_loop_liveness.py` 15 例（740/740 全绿）— `scripts/_loop_liveness.py` — 发现并修复于 R49-RSI 2026-11-01
- [P2] [INFO] [待 OWNER] L2 基线漂移 1 项：`manual_entry_codes` 回测 `()` ≠ 生产 `['510050.SH']`（OWNER 的 ETF 验证标的，属「实盘有、回测无」第 19 例）⇒ 建议走**显式 IGNORE 白名单 + 理由**，不要把它抄进回测基线（那会改 P0）— `strategy/_evolve_wf.py::base_cfg` — 发现于 R49-RSI 2026-11-01

- [P0] [FORBIDDEN] [待 OWNER] **KPI#4「实盘相对基准超额」被跨模式基线污染**：`excess_pct` 现报 **−70.49%**（account_pct **−93.63%** = live 63,684.34 ÷ **paper `INITIAL_CASH` 1,000,000**，宇宙 BH −23.14%）⇒ 终身无法转正，属口径断裂不是策略表现；账本已有 `MODE_BREAKS`，基准管道没有 — `engine/benchmark_feed.py:110`（`account_pct = _total_asset()/INITIAL_CASH - 1`）/ `config/settings.py:887` / `logs/benchmark_daily.jsonl` 2026-11-02 行 — 发现于答疑 2026-11-02

- [P0] [FORBIDDEN] [待 OWNER] **熔断按拼装值开火 ⇒ 误强平全部真实持仓（今日 10:34:10）**：拼装值塌成纯现金 6,953.34 ⇒ 判 −89.25% 假日内亏损（真实 −2.1%，10:34:58 权益即恢复 63,258）⇒ 卖出 300308×100 @548.61 + 510050×500 @2.867。已修：熔断改吃券商真实口径、不可信则**跳过判定**（不回落拼装）+ 3 轮中位数去毛刺 + 不可信禁建仓/不落脏快照 + 面板显示判定依据；`tests/run_all.py` 747/747。**需重启引擎才生效**，今日 halted 状态待 OWNER 处置 — `reports/P0_RISK_ASSET_TRUST_2026-11-02.md` — 发现于 OWNER 指令 2026-11-02
- [P1] [FORBIDDEN] [待 OWNER] `_sync_broker_positions` 对**单次**「券商返回空持仓」零确认：一次 `[]` 即把本地持仓全部归零（已加 WARNING 留痕）。建议改「连续 2 次空返回才清空」，但需评估真清仓场景下的幽灵持仓风险 — `engine/event_engine.py:4038` — 发现于 2026-11-02

- [P1] [FORBIDDEN] [待 OWNER] **11-03 回补单今日几乎必然作废（时限 14:50）**：300308.SZ 低开破位——开 539（曾在 537~545 区间内，但被 `earliest=09:35` 跳过）→ 09:35 已 516.48 → 09:39 510.32（−7.2%，日内低 507），跌破下限 537 达 −5.0%，须反弹 **+5.23%** 才成交。通道本身健康（熔断已解/现金 61,768>54,500/单仓 40% 已绕过）。值守建议：**延长 `valid_until` 到 11-04/11-05，保持 537~545 不改价**（唯一不改风控语义的选项；下调 min_price = 接飞刀）— `reports/OPEN_GUARD_20261103.md` §8/§三 — 发现于 开盘值守 2026-11-03
- [P1] [INFO] [已验收] **11-02 熔断 P0 修复首次回归通过**：`asset_basis="broker"`、`asset_trusted=true`、快照 63,193.44 对券商 63,192.44（偏差 **0.0016%**）、`halted=false` 且 09:30:03 按冷却 1 日自动解冻、风险基线重置为 63,207.94 ⇒ 原 P0「需重启引擎才生效」条件已满足，可转 CLOSED — `reports/OPEN_GUARD_20261103.md` §6 — 发现于 开盘值守 2026-11-03
- [P2] [INFO] [待 OWNER] 观察篮 510050.SH 于 09:30 自动回补 500 份 @2.869（真实资金 1,434.50 元）：昨日误强平后 `manual_entry_codes` 走「不持有则补买」路径（settings.py:684 已注明的副作用），非故障；占权益 2.3%，**不阻塞回补单**（剩余现金 61,768 > 54,500）— `reports/OPEN_GUARD_20261103.md` §7 — 发现于 开盘值守 2026-11-03
- [P2] [SAFE] **`config/settings.py` 被某进程以 `share=Read|Delete` 持有 ⇒ 程序化写入一律失败，会让「自动化改配置」静默不成立**：今晚加 510050 时实测三项同时出现——① Bash 里的 Python `open(...,'w')` / `os.replace()` → `PermissionError [WinError 5]`；② 宿主 shell 工具 `WriteAllText` → `UnauthorizedAccessException`；③ `Edit` 工具 → `ModifyBackup failed: path index collision`（该路径的备份索引也坏了）。**但 ACL 上 `lisinan` 是 FullControl、`os.rename()` 成功** ⇒ 不是权限问题，是**句柄锁只共享读+删除**。绕法：`os.rename(p → p+'.locked-old')` 写新文件后再删旧件。**风险点**：任何定时任务/自动化若按 `open('w')` 改 `settings.py`，会**报错中断或静默无效**；`config/` 目录本身无限制（新建文件可改），只有这一个 inode 被锁 — `config/settings.py` — 发现于 510050 加豁免清单 2026-11-03

## 2026-11-03 15:40 定时槽位（DAILY-REVIEW 首次运行）

- [P1] [FORBIDDEN] [待 OWNER] **300308 的有效止损是 −50.6%，不是配置注释写的 −18%**：`stop_price=247.716`（`wide=max(hard_stop_pct 0.18, atr_pct×6)` 反推 `atr_pct≈8.44%`，高波动把宽幅止损撑开），该仓占权益 **78.6%** ⇒ 理论最大亏损 **≈41% 权益**；且 `exit_exempt_codes=['300308.SZ']` 已豁免趋势/超时/单日暴跌退出 ⇒ 这是该仓**唯一**的止损 — `engine/event_engine.py:2028-2039` + `/api/snapshot.positions` 15:41 — 发现于 DAILY-REVIEW 2026-11-03
- [P1] [FORBIDDEN] [待 OWNER] **`owner_reentry` 今日成交 1 手后已到期但未置空**（`valid_until=2026-11-03`）⇒ 配置悬挂：明日开盘仍会以 ≤545 重试买入（现金仅 1.16 万，实际会被现金夹紧成 0 股，但每日重试+告警）。建议收盘后置 `{}` — `config/settings.py:657-673` / `fills#390` — 发现于 DAILY-REVIEW 2026-11-03
- [P1] [SAFE] [已修] **开仓归因脚本自结构统一以来从未跑完过**：`raw[-1]["time"]` 恒 `KeyError`（`get_history` 统一返回 `ts`，无 `time` 键）⇒ 动量归因链断在最后一环，而它是「为何没开仓」的唯一量化证据 — `scripts/_why_no_entry_momentum.py:62`，已改为 datetime 取值 + 回滚注释 — 发现于 DAILY-REVIEW 2026-11-03
- [P1] [SAFE] [已修·需重启] **观察篮建仓通知在 live 下仍自称 paper**：硬编码「（paper，绕过信号闸门…）」与 `fills#389 mode=live / 真实成交 1,434.50` 同日并存 ⇒ 会把真实买入读成模拟。已改为跟随 `exec_mode`，`tests/run_all.py` 789/789；**需重启引擎才进内存** — `engine/event_engine.py:2601` — 发现于 DAILY-REVIEW 2026-11-03
- [P2] [FORBIDDEN] [待 OWNER] **基准 `excess_pct` 今日双重失效（新增第二重）**：① 分母仍是 paper `INITIAL_CASH=1,000,000`（`account_pct=−93.89%`）；② **`as_of=2026-11-02` 而账户是 11-03**，`universe_bh_pct` 与 11-02 行逐位相同（−0.2146496413960277）⇒ `excess_pct=−72.43%` 是「11-02 的基准 − 11-03 的账户」跨日期相减。修法：`as_of != 当日` ⇒ `excess_pct=None` + 分母改 `MODE_BREAKS` live 基线（**勿动 `INITIAL_CASH`，那会改回测 P0**） — `engine/benchmark_feed.py:106-122` / `logs/benchmark_daily.jsonl` — 发现于 DAILY-REVIEW 2026-11-03
- [P2] [INFO] [跟踪中] **研究面板落后 4 个交易日且在暴跌段**：最新 `ts_panel_25_901_..._20261028.pkl` 末日 20261028；以 10-28 收盘对 live 收盘，**19 只可比 19 只不一致**（300394 −38.6% / 300502 −32.5% / 300308 −24.5%）⇒ 任何 10-28 之后的面板结论（含搜索政策主线 A「弱市期诊断」）看不到这段 — `storage/hist_cache/` — 发现于 DAILY-REVIEW 2026-11-03
- [P2] [INFO] [跟踪中] **北向闸门已事实停用 46 个交易日**：缓存序列末端 `20260825`、`guard_reason=stale`、fail-open 不拦截（今日 2 条同模板告警）⇒ 任何「北向闸门有效」的表述都不成立 — `data/cache/northbound_hsgt.json` / `/api/snapshot.northbound` — 发现于 DAILY-REVIEW 2026-11-03
- [P2] [INFO] [已结案] **「漏斗宇宙 37 vs 订阅 38」不是假基数**：差集恰为 `{510050.SH}`（`VALIDATION_UNIVERSE` 只订阅不参选，设计如此）⇒ 以后不要再按「假基数」告警 — `config/settings.py::UNIVERSE/VALIDATION_UNIVERSE` + `engine/event_engine.py:693-722` — 发现于 DAILY-REVIEW 2026-11-03
- [P2] [FORBIDDEN] [待 OWNER] **guardian 午休假告警**：12:04:07 `[GUARDIAN-ALERT] degraded=engine_log_stale`，实为 11:30–13:00 午休引擎不写日志 ⇒ `engine_log_stale` 判据应排除午休与收盘后时段。改动需重启 guardian，故不由本任务代劳 — `logs/guardian.log` 12:04:07 / `core/guardian.py` — 发现于 DAILY-REVIEW 2026-11-03

## CLOSED（近期）

- [P0] [SAFE] [已修 331ab21] 持仓未订阅 + 盯市价缺第三档 ⇒ 池外持仓被低估 — `tests/test_position_subscription_mark.py` 9 条 — 2026-10-30
- [P1] [SAFE] [已修] 2026-10-30 下午「零扫描」被误诊为时段守卫唤醒缺陷，实为 11:47–17:24 整机休眠（guardian 6h 无心跳）⇒ 判据写入契约 §4.3 — 2026-11-01

- [P0] [INFO] [已自愈·跟踪中] 盘前估值口径瞬时背离：09:34–09:40 开盘前引擎行情滞后（300308 last_price 卡 553.0 vs 券商~561），②/③=63,684.34 对券商 64,691~64,802 瞬时 −1.56%~−1.72%（>1% 阈值）；09:38 随 xtdata 刷新自愈（②=64,482/③=64,581/①=64,581，最大偏差 0.15%）⇒ 三态现一致，属开盘数据新鲜度观察项 — `/api/snapshot` + `core/broker.get_asset` 2026-11-02 09:38 — 发现于 PRE-MARKET 2026-11-02
- [P2] [INFO] [已恢复] PRE-MARKET 上次运行 2026-10-29，距 2026-11-02 停摆 4 天（10-30 撞旧 paper 门禁自我停机③ + 周末）；契约 §一已改模式感知，今日恢复 — `memory.md` — 发现于 PRE-MARKET 2026-11-02

## 2026-11-03 15:50 回补单结账（REENTRY-SETTLE）

- [P1] [INFO] [已结案] **接回完成：300308.SZ 100 股 @501.50 于 10:59:10 成交（50,150.00 元，1 手整，单笔无异常）**，但成交价低于原「不接飞刀」下限 537 达 −6.6

## 2026-11-03 15:50 回补单结账（REENTRY-SETTLE）

- [P1] [INFO] [已结案] **接回完成：300308.SZ 100 股 @501.50 于 10:59:10 成交（50,150.00 元，1 手整，单笔无异常）**，但成交价低于原「不接飞刀」下限 537 达 −6.6%（OWNER 10:55 授权关闭 `min_price` 后重启生效，`settings.py` mtime 10:53:56 < 引擎启动 10:59:00，守卫通过）⇒ 当日即浮亏 −2,136.79 元（−4.26%），建仓价至收盘再跌 4.23%，构成权益 −3.377% 的全部来源；**原 537 下限在今日是有效的**（全天最高仅 539.00、09:35 即 516.48），改价代价已量化，勿把「改价」当常规操作 — `reports/REENTRY_SETTLE_20261103.md` §1.3/§2.1 · `storage/qmt.db` fills#390 · `logs/quant_system.log:81460,81754` — 发现于 回补结账 2026-11-03

- [P1] [SAFE] [待 OWNER] **`owner_reentry` 到期清理是纯洁净度问题，无功能风险——并更正 15:40 DAILY-REVIEW 的「明日仍会重试买入」误述**：`_owner_reentry_step` 有双重短路，① 已持仓首行即 return（`event_engine.py:2635`，今日 10:59 起永久命中）② `valid_until` 过期即 return 且只告警一次（`:2645`）⇒ 明天起不会重试/下单/刷屏。建议将 `STRATEGY_PARAMS["owner_reentry"]` 改为 `{}`（`settings.py:657-673`），仅为避免下次读配置时误判「回补仍在进行」 — `reports/REENTRY_SETTLE_20261103.md` §3 · `engine/event_engine.py:2635-2652` — 发现于 回补结账 2026-11-03

- [P1] [INFO] [已验收] **11-02 熔断 P0 修复通过首个完整交易日**：`asset_basis` 全程 `broker`（09:31 实测偏差 0.0016% + 09:30–10:53 零 untrusted 告警 + 收盘 `asset_trusted=true/asset_untrusted_n=0`），今日权益单边下行至最低 61,071.65（−4.29%）**未再触发任何误强平**（今日 fills 仅 2 笔 BUY、0 笔 SELL）；三态对账自洽差 0.00 元，偏差 −3.377% 全部来自 300308 浮亏 −2,136.79 与 510050 浮盈 +2.50 — `reports/REENTRY_SETTLE_20261103.md` §4 · `/api/snapshot.risk` 15:04 — 发现于 回补结账 2026-11-03

- [P2] [FORBIDDEN] [待 OWNER] **收盘后「行情源失效」ERROR 每日必刷**：15:02:02 报「33 只标的快照陈旧 2 分钟，已暂停建仓」，此时 `session.state=post_close`（15:00 已收盘，行情自然停更）⇒ 判据未排除收盘时段，与已知「guardian 午休误报 `engine_log_stale`」同型；属噪声但会钝化真 ERROR 敏感度 — `logs/quant_system.log:82089` · `engine/event_engine.py` 行情新鲜度判据 — 发现于 回补结账 2026-11-03

## 2026-11-04 08:12 盘前资金安全复核（两处深夜改动 / 价位阶梯）

- [P0] [FORBIDDEN] [待 OWNER] **回撤预算的分母被「熔断恢复重置 peak」系统性抬高**：`peak_asset=63,846.65` 是 11-03 09:30:03 熔断自动恢复后由 `risk/manager.py:352-353`（`_peak_asset = total_asset`）写入的**新基线**，而 10-30 真实权益峰值是 **66,101.28**（`equity_snapshots` max；11-02 日志另记 66,542.31）。同一"双跌停且止损排不上"的合法情形：现行口径 **−19.61%/预算 98.1%**（看似未过线），**真实 HWM 口径 −22.87%/114.3%（已超支 1,908.40 元）**；快照可见峰值 66,101.28 为 −22.35%/111.8%（交叉校验）。**当前（昨收 61,073.65）主口径回撤 = −8.22%、预算 41.1%**（交叉校验：现口径 −4.34%/21.7%、快照峰值 −7.61%/38.0%）——这是唯一"现在还没越线"的数，对外统一用它。**真实 HWM = 66,542.31 已确证**：主证是 **2026-10-29 paper→live 迁移第 6 步的手工写入**（`.workbuddy/memory/2026-10-29.md` 原文「`engine_state`: peak_asset/peak_equity/day_open_asset 972,533.60 → **66,542.31**」+「真实账户 66,542.31（现金 8,337.31 + 100 股）」）；辅证 8,337.31 + 100×**582.05**（= 300308 10-29 收盘，xtdata 实拉：10-27 577.00 / 10-28 636.30 / 10-29 582.05 / 10-30 553.00）= 66,542.31 逐位相等，而 10-30 现金 8,337.28 只能得 66,542.28（**该 0.03 元不构成排他证明，券商估值/舍入同样可能报 .31**）。⇒ 语义定稿：**66,542.31 是 10-29 迁移时点按当日收盘价计的账户真实市值，被手工植入后成为 live 唯一回撤基线，此后从未被重算覆盖**——不是棘轮产物、不是 10-30 盘中高点，**责任人明确可审计**。请 OWNER 书面确认回撤分母用哪个峰值（建议不可重置的历史最高权益）。**该故障存活的结构性原因**（risk-controller-2 发现、我已复算到分位）：`_credible_peak_equity():4008-4013` 的 SQL 缺 `WHERE mode='live'`（原句实跑 credible=**1,005,860.00**，加过滤才是 66,101.28）+ `sanitize_peak_equity:239-252` 是 `min()` **单向向下** ⇒ 分母低于 100 万时自愈永远判"无需夹紧"，**结构上不可能发现虚低**，重启也救不回来（live `:3949-3965` 提前 return）— `reports/RISK_20261104_0812.md` §5-P0-1 · `risk/manager.py:352-353` · `storage/qmt.db equity_snapshots` — 发现于 盘前复核 2026-11-04
- [P1] [FORBIDDEN] [待 OWNER] **`-6%` 熔断在本账户已降级为「只停新开仓、不减仓」**：300308 与 510050 同在 `exit_exempt_codes`（`config/settings.py:723`）⇒ `_split_flatten_candidates:2491` 全部归 exempt、flat=[] ⇒ 触发后**一手卖不出去**。`settings.py:916` 原文"强平全部可卖持仓"已与实盘不符，契约必须改写；昨夜 510050 加入豁免的风险增量上限已量化为 **144.20 元 = 总资产 0.236% = 回撤预算 1.13%**（可接受），但语义降级不可再对外表述为"强平"— `reports/RISK_20261104_0812.md` §1.5 — 发现于 盘前复核 2026-11-04
- [P1] [FORBIDDEN] [待 OWNER] **更正 11-03 15:40 条目：300308 的有效止损是 411.2857（自成本 −18%、距昨收 −14.35%），不是 247.716（−50.6%）**：`stop_price` 只在 **scalp** 分支被读（`strategy/trend_strategy.py:280-282`）；`exit_mode="trend"` 与豁免仓分支（`engine/event_engine.py:2229`）**都只按 `avg_cost×(1−18%)`** 触发 ⇒ 豁免分支给出的止损比 `stop_price` **更紧**。**单票最大亏损须 from-cost = −9,028.22 元 = 权益 14.78%**（含双边费 14.88%）；自现价起算的 6,891.43 元/11.28% 只是增量，不得单独标为"最大亏损"。原"有效止损 −50.6%／最大亏损 41% 权益"整行作废，请勿据此给 300308 定风险上限 — `reports/RISK_20261104_0812.md` §5-P1 — 发现于 盘前复核 2026-11-04
- [P2] [FORBIDDEN] [待 OWNER] **今日唯一会真实下单的路径已定位**：300308 触及 **411.2857** 的 −18% 灾难止损卖出 100 股（`event_engine.py:2240-2244`, force=True）；其余 10 条路径（策略出场/日线兜底/regime/−6% 强平/510050 止损 2.3608/观察篮/OWNER 回补/轮动）今日**全部不会成交**。注意 −6% 线是**相对线**（随开盘价 0.94 斜率下移：低开到 58,000 时落在 414.66，与硬止损只差 0.8%），今日唯一不随 D0 变动的价是 411.2857；−20% 断路器触线 380.24 **低于跌停 384.16 ⇒ 今日物理不可达** — `reports/RISK_20261104_0812.md` §2/§4 — 发现于 盘前复核 2026-11-04
- [P2] [SAFE] **重启后 `open_date=None` 的真实风险不是"当日买当日卖"**：live 买入成功即在 `:3544-3554` 乐观建仓并写 `open_date`，本进程内 T+1 仍拦得住；缺口是①今日 09:30–10:00 保护窗被重启关掉 ②**今日买入+之后再次重启**才会生成被拒 round-trip（豁免分支每轮重试、无次数上限）。今日开仓概率判 <5%（动量池空 11-03 全天 + 宽度确认≥2 + 现金仅够 ≤116.11 元/股），**不构成今日限制开仓的理由**；条件：若今日成交任何 BUY，下一次重启后须人工核 `open_date` — `reports/RISK_20261104_0812.md` §3 — 发现于 盘前复核 2026-11-04

## 2026-11-04 08:15 主理人裁决（盘前核查 · 三线汇编）

> 详细见 `reports/PREOPEN_20261104.md`；资金安全定价见 `reports/RISK_20261104_0812.md`。
> **三态 = ①（盘前待机，非故障）**：引擎 PID 4484 @08:08:03 `exec=live`，`pre_open` 按 `event_engine.py:1385` 设计休眠，一轮未跑故无新 `engine_state` 行。
> **裁决：今日不改参数、不重启、不下单干预**；值守只盯 **411.2857**（今日唯一不随 D0 变动的价）。

- [P0] [FORBIDDEN] [待 OWNER 一句话] **KPI#4「实盘超额」的分母是 paper 时代的 1,000,000，自 10-29 起结构性必败**：`logs/benchmark_daily.jsonl` 的 `account_pct = total_asset/1,000,000 − 1`；11-03 = −0.93892635 逐位等于 61,073.65/1e6 ⇒ 分母确证为 2026-09-22 的 paper 初始资金。断点日 10-29 excess 从 +13.68pp 直落 −77.98pp，11-03 = **−70.28pp**，而 KPI#4 目标 `>= 0` ⇒ 失败与策略能力无关，任何"实盘超额"趋势都会得出「策略实盘失效」的错误结论。**请裁决是否改为相对 live 基线 66,542.31** — `config/kpi_targets.py::kpi4_live` · `logs/benchmark_daily.jsonl` — 发现于 盘前账本审计 2026-11-04
- [P0] [SAFE] **`review_*.json` 的 `total_pnl` 双计一倍（正收益日会反向虚增）**：`review_daily.py:1647-1650 total_pnl = realized_eff + eod.unrealized`，而 live 下 `realized_eff` 自 10-30 起已改为「总资产 − 当日开盘资产」（**已含盯市**）⇒ 11-03 = −2,134.29 + (−2,134.29) = **−4,268.58，恰为真值 2 倍**，零告警。HTML 标注「已实现+未平盯市」本身就是重复计。纯展示层，**不涉参数、不需重启**，可收盘后修 — `scripts/review_daily.py:1647-1650` — 发现于 盘前账本审计 2026-11-04
- [P0] [SAFE] **`benchmark_daily.jsonl` 每早晨复制一行伪观测**：11-04 08:08:52 新增行与 11-03 位级相同（差 3e-8，`universe_bh_pct/index_pct` 全同）⇒ 非交易日/盘前写入的是上一日快照副本，会进入任何滚动聚合把方差压小。同类重复已系统化：10-24≡10-23、10-31≡10-30、11-01 亦同；而 10-25 同为非交易日却给出 −14.47%→−0.80% 的完全不同值，无规则可循 ⇒ 该文件当前**不能作为基准序列**用 — `logs/benchmark_daily.jsonl` — 发现于 盘前账本审计 2026-11-04
- [P1] [INFO] **09:31 硬检查点：首条权益快照的 `positions_count` 必须 = 2**：昨日 09:30:03 首条快照为 `total=63,207.94 / cash=63,207.94 / positions_count=0`（持仓尚未同步就落纯现金快照），当日 `day_open` 基线即由此而来。今日持仓已于 08:08:40 从券商同步完（`持仓初始化: 2 只, cash=11611.62`）⇒ 若 09:31 仍为 0，说明同步时序退化，−6% 分母将错 — `engine/event_engine.py:_persist_equity` — 发现于 盘前链路核查 2026-11-04
- [P1] [INFO] **`range_pct` 名不符实 + 10-30 账本行缺 `eod_is_close` 键**：① `range_pct` 实为「自断点基线起的累计收益」（`record_ledger.py:516 (eod/base-1)*100`），11-03 `-8.22` vs 真实日内振幅 `-4.29`，差近一倍，谁拿它当日内波动设阈值就错一倍；② 10-30 行**没有** `eod_is_close` 这个 key（非 null），因该行是 `scripts/_fix_ledger_1030_eod_20261101.py` 手工修补、未走 `annotate_eod_freshness` ⇒ 恰恰是唯一真出过「盘中值当收盘」的一行没有新鲜度标记，消费端 `row.get("eod_is_close", True)` 会把它当真 — `logs/evolution_ledger.jsonl` · `scripts/record_ledger.py:516` — 发现于 盘前账本审计 2026-11-04

### 2026-11-04 08:35 修订（风险审计官 v12 撤回本文件 08:15 段的两处口径）

> **我（主理人）08:20 给你的「回撤预算 75.7%」与「380.24 今日物理不可达」两句作废。** 已独立复核（`equity_snapshots`：live 期 n=538，`max(total_asset)=66101.28` @2026-10-30T09:59:04；11-03 首条 `09:30:03.142309 total=63207.94 positions_count=0 drawdown_pct=0.0`）。所有引用这两句的段落一律按下面双口径重读。

- [P0] [FORBIDDEN] [待 OWNER] **回撤分母在 11-03 09:30:03 被熔断恢复重写，真实 HWM 66,101.28 → 63,846.65（虚低 2,254.63 元 / 3.411pt，断路器线低 1,803.70 元）**：`logs/quant_system.log:81432` `RiskManager 熔断自动恢复（冷却 1 日，重置风险基线）` + `risk/manager.py:352-353 if total_asset: self._peak_asset = total_asset`。后果**不是"多亏钱"，而是"不会再被告知"**：现口径最坏 −19.61%/98.1%（**不越线**）、真实口径 −22.35%/111.8%（**越线 1,555.57 元**）；300308 反解到断路器线，现口径需 380.2367（< 跌停 384.16 ⇒ 今日引擎不会响）、真实口径需 **398.2737（可达，距 411.2857 止损仅 −3.16%）**。⇒ **今天账户可能真实越过 −20%，而断路器全程不响。请裁决分母是否改为不可重置的历史 live 最高权益** — `risk/manager.py:352-353/331/136` · `settings.py:925`（"重置风险基线"）vs `risk/manager.py:376`（"不重置 _peak_asset，保留真实回撤基线"）**同库自相矛盾** — 发现于 风险审计 v12 2026-11-04
- [P0] [FORBIDDEN] **峰值自愈对 6.6 万账户结构性失效（重启也救不回来）**：`engine/event_engine.py:4008-4013 _credible_peak_equity()` 的 SQL **缺 `WHERE mode='live'`** ⇒ 实跑 credible = **1,005,860.00**（paper 100 万初值污染）vs 加过滤后 66,101.28；而 `sanitize_peak_equity:252` 是 `min(stored, cred)` **单向向下**，只砍虚高（10-07 那次 1,020,446 即它砍的），**结构上不可能发现虚低**。⇒ `:3939` 重启后原样恢复 63,846.65。**重启不可逆清单新增第 5 项** — `engine/event_engine.py:4008-4013/252/3939` — 发现于 风险审计 v12 2026-11-04
- [P0] [FORBIDDEN] **`equity_snapshots.drawdown_pct` 在 live 下是「日内回撤」，不是组合回撤——禁止用它论证 −20% 预算**：live 分支 `:3949-3965` 提前 return、不恢复 `_peak_equity`，每次进程启动 `:640 self._peak_equity = capital` ⇒ 该列分母是「本次进程内最高权益」。实证：10-30 全部 54 条 `dd` 恒为 0.0，而当日总资产动过 3,700 元。**只认 `risk._peak_asset`** — `engine/event_engine.py:3881-3886/3949-3965/640` — 发现于 风险审计 v12 2026-11-04
- [P1] [SAFE] **「单票最大亏损」必须 from-cost，否则低报 2,136.79 元**：risk-controller-3 给的「78.63%×14.35%≈11.3% 权益（6,891 元）」只算自现价起的增量，漏掉已沉没的 −2,136.79；from-cost 实为 `(411.2857−501.5679)×100 = −9,028.22`（含费 −9,089.91）= **现权益 14.78%**。同源作废：OWNER_INBOX 11-03 那条「有效止损 −50.6% / 单票最大亏损 41% 权益」两数均由 `stop_price=247.716` 推出，**前提一错、两数一起作废，建议整行撤而非改一个** — `strategy/trend_strategy.py:237/242/280-282` · `settings.py:337` — 发现于 风险审计 v12 2026-11-04

### 2026-11-04 08:50 修订 · 二（HWM 定区间、断路器可达性、熔断卖 0 手）

- [P0] [FORBIDDEN] [待 OWNER] **真实 HWM 只能写「66,101.28 ~ 66,542.31」区间，不许写单点值**：`risk_snapshots` 11-02 全部 **1,223 条**（09:30:50.531→15:00:53.534）`peak_asset` 恒 **66,542.31**；10-30 全部 14 条同值；备份库 `storage/qmt_pre_pxtype_20261030_100319.db` 的 `engine_state`（ts=10-30T09:56:04）亦为 66,542.31 ⇒ **10-30 植入、10-30 与 11-02 全程未丢，丢失点唯一 = 11-03 09:30:03.141，金额 3,334.37 元**。但 66,542.31 自身来源未查实（10-30 首条 risk_snapshot 09:31:07 即就位，而当日 equity 峰值只有 66,101.28@09:59:04，差 441.03；反解标价 @582.05 vs @540.74）⇒ **会计来源分不出，不选边**。★ 该 P0 **不挂任何一次提交**，挂在恢复逻辑本身：「熔断自动恢复」全库仅 **4 次**，其中 3 次下砍：09-29 −20,410.0 / 10-09 −47,913.0 / 11-03 −3,334.37 ⇒ **再熔断恢复一次就再白送一段回撤** — `risk/manager.py:346→352-353` · `risk_snapshots` · `storage/qmt_pre_pxtype_20261030_100319.db` — 发现于 live-ops v2 复核 2026-11-04
- [P0] [INFO] **断路器可达性按区间重报：300308 反解 398.27~401.80（均 > 跌停 384.16 ⇒ 可达）**；且 **411.2857 止损成交后预算只剩 7.1%~9.8%**（不是我先前算的 24.3%）。三个分母并列：现 63,846.65 → 触线 51,077.32 / 反解 380.2367（引擎今日不响）；66,101.28 → 52,881.02 / **398.2737**；66,542.31 → 53,233.85 / **401.802**。双跌停权益 51,325.45 在后续两个分母下**均越过 −20%** — `reports/PREOPEN_20261104.md` §十 — 发现于 live-ops v2 / risk-controller v12 复核 2026-11-04
- [P0] [FORBIDDEN] **★ 更正「明天只卖 510050 的 1,442 元」——熔断卖 0 手，一手都卖不出去**：昨夜 `ab416b2` 把 510050 加入 `exit_exempt_codes` 后，`_split_flatten_candidates:2491` 把两只**全部**归入 exempt ⇒ `flat=[]` ⇒ −6% 触发后**只有 `_halt()` 与一条 WARNING，没有任何卖出**。live-ops-engineer-2 的「443.56 → 只卖 510050 1,442 元 + halt」是**加豁免之前的旧口径**，已作废。今日真实动作只有一条：300308 触及 411.2857 → 卖 100 股 — `config/settings.py:723` · `engine/event_engine.py:2491` — 发现于 risk-controller-3 更正 2026-11-04
- [P1] [SAFE] [已修 8e81800 未 push] **live 下 `total_pnl` 双计一倍已修**：`strategy/review_daily.py` 新增 `_day_exec_mode()` + 纯函数 `_compose_total_pnl()`；live 不再叠加未实现（引擎侧 `risk/manager.py:128/277` 早已防住，是复盘侧没跟上）。11-03 −4,268.58 → **−2,134.29**，11-02 不变，paper 日逐位不变。报表标签同步改诚实（「已实现(引擎累计 daily_pnl)」实为权益曲线 → 改「当日权益变动」）。新增 `tests/test_review_pnl_caliber.py` 12 例，`run_all 806/806 PASS` — `strategy/review_daily.py` — 发现于 ledger-fixer 2026-11-04
- [P1] [INFO] **11-03 复盘的「准确」维度分数偏高，勿引用**：`_account_daily()` 硬编码 `mode='paper'`（切 live 后「账户真实收益」卡恒「—」，失去账户亏损锚点）⇒ 11-03 返回 `{}` ⇒ 当日 −3.38% **没有被扣分**。试改「按当日 mode 取」是陷阱：11-02 会取到 10-30 的 11:29 盘中快照 65,748.42，又造出 −3.86% 假亏损（账本已修正为 −0.75%，等于复活旧 bug）⇒ 正确解是让 review 复用账本行的 `daily_pnl`，属设计决策，未修 — `scripts/review_daily.py::_account_daily` — 发现于 ledger-fixer 2026-11-04

### 2026-11-04 08:57 归因锁定（日志实证，可替代代码推断）

- [P0] [INFO] **11-03 的分母下砍 100% 来自 `_maybe_recover`，峰值自愈当天未参与**：`logs/quant_system.log` 全库「峰值自愈」仅 **2 条，均在 2026-10-08 15:54:26**（`peak_equity 1020446.60 → 1005860.00` / `peak_asset 1020446.60 → 1005860.00`），**11-03 一条都没有** ⇒ 归因无歧义，可作实证引用。旁证 441.03 缺口非夹紧所致（夹紧没跑）⇒ 66,542.31 是 10-30 早盘真实进入风控状态的值。★ 但这**不改变**自愈缺陷的结论：`:3993-3995` 缺 `WHERE mode='live'` ⇒ credible 被 paper 期拉到 1,005,860 ⇒ `min(66,542.31, 1,005,860)` 恒等于原值 ⇒ **对 6 万级账户永远 no-op，且两个方向都取 min，结构上只能砍虚高** — `logs/quant_system.log` · `engine/event_engine.py:3993-3995/4015-4034` — 发现于 live-ops v2 2026-11-04

### 2026-11-04 09:20 三改：主口径定为 66,542.31 + 撤回「dd 恒 0.0」+ 断路器 401.80

- [P0] [FORBIDDEN] [待 OWNER] **主口径改用 66,542.31（有日志出处），三口径并列；断路器 401.8020，距 411.2857 止损只有 9.48 元 / 2.31%**：`logs/quant_system.log:80623` `2026-11-02 23:04:23 恢复风控状态: peak=66542.31` ⇒ 该值是 RiskManager 自己棘轮出来并持久化过的 HWM（66,101.28 只是快照可见值，`:3862` 有 60s 节流、10-30 存在 6 分钟快照空洞，故它只是下界）。三口径表：双跌停 51,325.45 ⇒ **−19.61%/98.1%（不越）｜−22.35%/111.8%（越 1,555.57）｜−22.87%/114.3%（越 1,908.40）**；411.2857 成交 54,182.22 ⇒ 75.7%｜90.2%｜**92.9%**；断路器反解 300308 ⇒ 380.2367（不可达）｜398.2737｜**401.8020（可达）** — `logs/quant_system.log:80623/81432/82104` · `risk/manager.py:352-353` — 发现于 风险审计 v13 2026-11-04
- [P0] [INFO] **分母问题的准确表述不是"改了个数"，是"历史亏损被抹掉了四分之一预算"**：11-02 收盘系统自记回撤为 **−5.011%**（63,207.98 / 66,542.31，占预算 25.1%）；**11-03 09:30:03 一次熔断恢复把它清成 0.0%** ⇒ 此后同一笔亏损要再走 5.011pt 才开始计入预算。今晨 `:82104` 又把 63,846.65 原样恢复 ⇒ **重启不但没修，还把脏值写死** — 同上 — 发现于 风险审计 v13 2026-11-04
- [P0] [FORBIDDEN] **账户级安全网已对两只持仓同时失效：熔断 0 笔委托**：`settings.py:723` 两只全在 `exit_exempt_codes` + `settings.py:743 regime_force_exit=False`（这是全仓**唯一**能卖豁免仓的路径，位于 `:2217` 豁免分支之前且不查 `_is_manual_exempt`）⇒ `flat=[]` ⇒ −6% 触发后只有 `_halt` + 一行 WARNING。**且这是设计意图**（`:2476` docstring「熔断 = 停止进攻，不是清掉 OWNER 的战略持仓」）⇒ **若 OWNER 以为"跌多了熔断会帮我卖掉一点"，那是误读** — `config/settings.py:723/743` · `engine/event_engine.py:2466/2476/2491` — 发现于 风险审计 v13 2026-11-04
- [P1] [SAFE] **撤回本文件 08:35 段的「10-30 全部 54 条 dd 恒为 0.0」——这句是错的（我引用的）**：实测 **40/54 条为负、min −1.5945%**。该列的正确语义是「**自上次进程启动以来**的回撤」而非"日内"：硬证 —— 11-03 10:58:26 盘中重启后，10:59:01 首条 `dd` 由 −0.0103% → **0.0** 并重锚 63,199.94 ⇒ **盘中重启会抹掉重启前的回撤**。结论不变（禁止用作组合回撤证据），但机理要写对 — `engine/event_engine.py:640/3881-3886/3949-3965` · `equity_snapshots` — 发现于 风险审计 v13 自纠 2026-11-04
- [P1] [INFO] **300308 的有效阻挡从「5 道闸」降为「2 道」，但 stale 是实测风险不是理论**：`force=True` 生效后，T+1（11-04 已过期）与建仓保护期（force 绕过）都不挡 ⇒ 前置有 tick + mock`:3664` + **stale`:3671`（force 不可绕）** + pending`:3676`（仅已有挂单时）。实证：`:82088` 11-03 15:02:02 全市场 33 只陈旧 2 分钟，**样例首只就是 300308.SZ** — `engine/event_engine.py:2208/3664/3671/3676/3698` — 发现于 风险审计 v13 2026-11-04
- [P1] [INFO] **v13 勘误里 `-7.61%` 的括注算错了；主口径下当前回撤是 −8.22% / 预算 41.1%，而且这个数被账本独立验证**：`(61,073.65−66,542.31)/66,542.31 = −8.218%`，−7.61% 对应的是 `66,101.28` 分母。★ 关键旁证：账本 11-03 行的 `range_pct = -8.22`（自断点基线累计）逐位等于此数 ⇒ **账本自己一直用 66,542.31 当基线**，主口径因此自洽。三口径下的当前回撤：21.7%（63,846.65）/ 38.0%（66,101.28）/ **41.1%（66,542.31）**；411.2857 成交后 75.7% / 90.2% / **92.9%** — `logs/evolution_ledger.jsonl` range_pct · 我按 `(61,073.65−den)/den` 复算 — 发现于 主理人复核 2026-11-04

### 2026-11-04 08:30 定稿：66,542.31 位级证实为真实 HWM；今日唯一出口 411.2857；**分母 bug 今日无交易后果，可收盘后再修**

- [P0] [INFO] **66,542.31 已定稿为「10-29 迁移时点手写植入的 live 唯一回撤基线」，语义必须这么写**：① **主证（书面）** `.workbuddy/memory/2026-10-29.md` 迁移第 6 步原文「`engine_state`: `peak_asset`/`peak_equity`/`day_open_asset` 972,533.60 → **66,542.31**」+ 同文件「真实账户 66,542.31（现金 8,337.31 + 100 股）」；② **辅证（不排他）** `8,337.31 + 100×582.05 = 66,542.31` 逐位相等（xtdata 实测 10-29 收盘 582.05），但券商估值/舍入同样可能报 .31 ⇒ **0.03 元不构成排他证明**。★ **撤销我 08:30 写的两处错误**：（a）「`:80623` ⇒ RiskManager 自己棘轮并持久化过」—— **错**，`2026-11-02 23:04:23 恢复风控状态: peak=66542.31` 是**启动恢复时忠实读出**，不是棘轮；（b）「只能来自 10-29 迁移」这条**建立在 0.03 元精度上，而 0.03 是跨日两个时点的同步差不是舍入**，推理不成立，改以书面记录为主证。**它不是棘轮产物、不是 10-30 盘中高点 —— 否则 OWNER 会以为 10-30 摸到过 6.65 万。** 当前 `peak=63,846.65` 比该基线少 **2,695.66 元** — `.workbuddy/memory/2026-10-29.md:92` · `xtdata 10-29 close` · `logs/quant_system.log:80623` — 主理人 2026-11-04（经风险审计 v2/v3 两次收窄）
- [P0] [INFO] **★ 今日全仓唯一会真正下单的线只有一条：300308 = 411.2857（硬止损，卖 100 股）**，对应今日跌幅 **−14.35%**，距跌停 384.16 还有 **5.65%** 空间。成交后资产 **54,182.19**、只剩 510050 500 股；真实口径回撤 **−18.58% / 预算 92.9%**，系统口径 −15.14% / 75.7%。**其余三条线全部不产生委托**：443.56 只 `_halt`；401.802 只 `_halt` 不平仓；510050 硬止损 2.3608 **低于跌停 2.5956 ⇒ 今日物理不可达** — `risk/manager.py` · 券商实测 61,073.62 — 主理人 2026-11-04
- [P0] [SAFE] **★ 熔断 halt 只冻结开仓、绝不拦卖出 —— 所以分母 bug 今日没有交易后果，不必为它今天重启**：`risk/manager.py` 中 `_halted` 仅出现在 `prefilter:184` 与 `can_open:205` 两处，**卖出/退出路径完全不读它**（`engine/event_engine.py` 只在 `:1505/:1539` 用它做状态显示）。因此今日最坏情形（双跌停 51,325.42）无论熔断与否，**委托笔数都是 0**：系统口径 −19.61%/98.1%（余 248.10 不越）vs 真实口径 −22.87%/114.3%（越 1,908.43）—— 差异只是"日志里多不多一条 halt"。**结论：分母修复随时可做，最优窗口是今日收盘后（15:00 后）而非盘中**（盘中改需重启，重启会把 `open_date` 全部清成 None，凭空引入新变量） — `risk/manager.py:184/205` · `engine/event_engine.py:1505/1539` — 主理人 2026-11-04
- [P1] [INFO] **风险审计 v3 第⑤项已实测结案：尾部 `risk_snapshots` 全部是 63,846.65，65,748.42 没有回钉**：11-03 `15:00:05 / 14:57:04 / 14:56:55` 三条 `payload_json` 的 `peak_asset` 均为 **63,846.65**、`halted=False`、`position_scale=1.0` ⇒ 口径 04 成立，口径 05（真实值回到 66,101.28）**不成立**。另：`risk_snapshots` 止于 15:00:05 而 `engine_state` 到 15:04:42，二者停止时刻不同步（P2 量测卫生） — `storage/qmt.db::risk_snapshots` — 主理人 2026-11-04
- [P1] [INFO] **`8e81800` 的 P&L 修复被引擎自记值位级证实**：11-03 `risk_snapshots` 的 `daily_pnl` 恒为 **−2,134.29**（15:00:05 / 14:57:04 一致），与修复后复盘 `total_pnl = −2,134.29` **逐位相等**；修复前 −4,268.58 恰好是其 2 倍 ⇒ 双计一倍坐实，修复方向正确 — `storage/qmt.db::risk_snapshots` · commit `8e81800` — 主理人 2026-11-04
- [P1] [INFO] **口径提醒：−8.22% 不是"从峰顶回落"，是"自转入实盘以来累计亏损"**：66,542.31 是 10-29 15:05 切 live 当刻的组合估值，**此后从未创出新高**（10-30 63,684.42 → 11-02 63,207.98 → 11-03 61,073.65，单调下行）⇒ 称其为 HWM 技术上成立，但语义上是"实盘起点"。向 OWNER 汇报时用后者更诚实 — `logs/evolution_ledger.jsonl` — 主理人 2026-11-04
- [P1] [INFO] **★ 上一条的补充更正：这 3 个交易日里账户换过一次血，"累计亏损"里没有交易亏损，全是行情**：`11-02 10:34` 日内硬止损**全部清仓**（300308 100@548.61 + 510050 500@2.867，回款 **56,294.50**）⇒ `11-03 09:30` 观察篮重建 510050 500@2.869、`11-03 10:59` **OWNER 回补** 300308 100@501.50 ⇒ **今天手里这 100 股是 11-03 的新仓，成本 501.5679，不是 10-29 那批 194.76**。因此「−8.22%」的准确读法是"账户净值自 10-29 起点下行 8.22%，期间经历过一次全平与重建，成本基础已完全更换"，**不是**"一路持有亏了 8.22%" — `orders` #1100-1103 / `fills` #386-390 — 主理人 2026-11-04
- [P0] [FORBIDDEN] **★ 「跌多了熔断会保护账户」这条通路必须分两层说：−20% 层是「从未跑过」，−6% 层是「跑过两次、两次皆假」**：全库 82,216 行日志里 **`max_drawdown` 类熔断 0 次运行 ⇒ 未测，不是被推翻**（它拦不拦得住下跌市，目前没有任何证据）；唯一响过的 `daily_stop`（−6% 日内强平）**共 2 次，两次都是假数** —— 10-30 11:24:53 报 **−86.58%**（委托被券商以 `306050 股份资产不足` **拒单，0 成交**）、11-02 10:34:10 报 **−89.25%**（**真成交**，因为当天可用量正常）。根因：`_sync_broker_positions` 失败时 `_total_asset()` **退化成"纯现金"**（10-30 = 6,953.34）⇒ 真实下跌 ~2.3% 被放大 38 倍。**⇒ 今天唯一的保护是 411.2857 这一条个股 −18% 止损，其余（−6% 日损线、−20% 断路器）全部是显示器** — `logs/quant_system.log` · `risk/manager.py` 硬止损分支 — 风险审计 v3 2026-11-04（主理人已复核 10-30 拒单/11-02 成交差异）
- [P0] [INFO] **★ 昨夜把 510050 加进豁免，恰好堵住了这条"假熔断真清仓"的通路**：若 510050 仍不豁免，假 `daily_loss_pct` 一旦触发，`_split_flatten_candidates` 会把它当可平仓标的提交 ⇒ 又是一次假数据驱动的真实卖出。现在两只全豁免 ⇒ `flat=[]` ⇒ **即使再出假数，也是 0 笔委托**。这是 `ab416b2` 的一个未被记录的正面副作用，值得记住 — `config/settings.py:723` · `engine/event_engine.py:2482/2491` — 主理人 2026-11-04
- [P1] [FORBIDDEN] **★ 单仓集中度才是今天真正的风险敞口，而它不在任何风控门禁里**：300308 市值 48,020 / 权益 61,073.62 = **78.6%**（11-03 OWNER 回补时是 **79.4%**，因 `allow_large_position=True` 绕过了 `max_single_position_pct=40%`）。⇒ 该股 −18% 止损 = **账户 −14.35%**；换句话说**一只股票的走势决定整个账户的命运，这不是组合而是单一押注**。现价 480.20 距 411.2857 只有 −14.35%，距跌停 384.16 还有 5.65% — `engine/event_engine.py` OWNER 回补 · `risk/manager.py:can_open` — 主理人 2026-11-04
- [P0] [FORBIDDEN] **★ 上一条要再加重一档：这套账户级卖出安全网不是"打不响"，它响过一次，卖的是真仓，触发它的是假数**。11-02 逐单还原：`1082285654` SELL 300308 ×100 @549.94 **撤单**(0/100) → `1082285806` @549.17 **券商拒单**(306050 股份资产不足) → `1082285951` @548.61 **成交**；同时 `1082285656` SELL 510050 ×500 @2.867 **全成**。`fills #386/387/388` ⇒ **合计 56,294.50 元真实卖出**。因果链：口径错 → `daily_stop_flatten_pct` → `flatten_requested` → `:2141` → `:2162 _handle_sell(force=True)` → 3 次重试。**反面必须同时写清**：10-30 那次 −86.58% **没卖出**（当日唯一成交 `#385 10:08:11`，比熔断早 76 分钟，无因果）⇒ 准确说法是「**11-02 卖了、10-30 没卖**」，不是"两次都卖了" — `orders` #1100-1103 · `fills` #386-390 — live-ops 2026-11-04
- [P1] [INFO] **新量测缺陷：`orders` 表把「尝试」记成「委托」，委托全生命周期在账本层不可追溯**：10-30 `orders` 有 **656 行**，但 `order_id` 非空仅 **13 行**、为空 **643 行**，与当日日志 `[BROKER] 下单` **13 条**逐条吻合 ⇒ 643 行是**重试/被风控挡下的尝试**，不是真委托。schema 只有 `id,ts,code,side,quantity,price,order_type,account,order_id,mode`，**无 `status`/`filled_qty`/`cancelled_at`** ⇒ 撤单、废单、部成、全成全分不出来，任何"委托数/成交率/撤单率"都会失真。旁证 `:3739-3741` 注释「失败卖单无限重试，每 3 秒吃一个额度 ⇒ 一上午刷穿 `max_daily_trades`」证实这 656 次是真实事故而非账本幻觉。★ **更关键的自证失败（我实测复核）**：11-02 那四笔卖单 `#1100 @549.94 / #1101 @2.867 / #1102 @549.17 / #1103 @548.61` **order_id 全部非空、形态完全一致**，但真相是 **#1100 撤单、#1102 拒单、#1103 成交、#1101 全成** ⇒ **`orders` 表无法自证任何一笔委托的结局**，必须靠 `fills` + 日志交叉。**P1（量测），不与今日任何 P0 混级** — `storage/qmt.db::orders`（实测 656/643/13、11-02 四笔单号齐）· `logs/quant_system.log` — live-ops 2026-11-04，主理人复核
- [P2] [INFO] **观察项（非 P1）：2 小时内 643 次被拒的券商调用可能撞单机锁/限流**：10-30 `09:30:53→11:29:59` 每 ~11s 重试一笔、共 **643 次**直接打到券商（全为 `SELL 300308`、`order_stock rc=-1`），约 **360 次/小时**。即便全部被拒，这个量级也可能与盘后同步抢同一把 miniQMT 进程内锁（`settings.py` 句柄锁先例）。与今日无关，仅登记 — `logs/quant_system.log` 10-30 段 — 风险审计 2026-11-04

### 2026-11-04 08:35 盘前终校：两个数改成条件句 + 「制度性下移」定名 + 两条值守陷阱

- [P0] [INFO] **★ −8.22% 与 −6% 线 443.56 都是条件句，不是既成事实 —— 08:27 直读引擎实测**：`GET /api/snapshot`（引擎进程内只读接口，`web/app.py:47 threaded=True, use_reloader=False`，不碰券商连接）返回 `risk.asset_basis = "local"`、`risk.day_open_asset = null`、`market_data_ts = null`、`total_asset = 61,073.62` ⇒ ① 分子 61,073.62 是**盘前本地拼装值**，开盘后会被券商口径替换；② **今日日内基准尚未建立**（`_day_open_baseline:1990` 要求首轮 `not _market_stale`）。正确表述：**「若开盘首轮 total ≈ 61,073（平开），则当前回撤 −8.22%、−6% 线在 443.56」**。分母 63,846.65 确定，分子不确定。**向 OWNER 报数必须带这个前提，否则开盘一低开就会被当成"报告算错"** — `/api/snapshot` 实测 · `engine/event_engine.py:1982-1992` — 风险审计 v14 2026-11-04（live-ops-engineer-2 提出）
- [P0] [INFO] **★ `open_date` 不必靠"内存没出口"推断，引擎自带只读窗口**：同一次 `/api/snapshot` 实测 `300308.SZ.open_date = null`、`510050.SH.open_date = null`，**两只同时为 None** ⇒ `_pos_meta` 当前为空，B 分支确认，不必等 09:31、也不必从 `engine_state` 残留倒推。同批读到 `risk.halted=false`、`peak_asset=63,846.65`、`daily_trade_count=0`、`consecutive_losses=0` — `/api/snapshot.positions/.risk` — 风险审计 v14 2026-11-04
- [P0] [FORBIDDEN] **★ 「下移」要定名为制度性下移，不是偶发缺陷 —— 两个恢复钩子是同一个动作**：`max_drawdown` 熔断走 `dd_recover_days=3`（`settings.py:925`），连亏/日亏熔断走 `halt_recover_days=1`（`settings.py:931`），二者**同一钩子同一动作**（`risk/manager.py:346 → :352-353 self._peak_asset = total_asset`），只差间隔。**全库 4 次恢复（09-25 / 09-29 / 10-09 / 11-03）4/4 全部把峰值砍低，无一次例外** ⇒ 每恢复一次就重新发放一次回撤预算。**今日直接后果：若今天触发 −6% ⇒ `_halt`（只停开仓）⇒ 明日 09:30 由 `halt_recover_days=1` 自动解冻并再执行一次 `_peak_asset = total_asset` —— 今天吃掉的回撤，明天开盘会被"重新发放"一次。凡写"预算使用率"，必须注明该分母有效期只到下次熔断恢复** — `risk/manager.py:340-357` · `settings.py:925/931` — 风险审计 v14 2026-11-04（live-ops-engineer-2 提出，我重定性）
- [P1] [INFO] **66,542.31 的语义要两句相加，缺一句会被判成臆造**：(i) **值** = 10-29 收盘 582.05 的真实市值；(ii) **写入方式** = 迁移时**手工植入 `engine_state`，不是引擎棘轮出来的**。只写 (i)，下一个人会去 `risk/manager.py:155-157` 找棘轮路径，找不到就判"报告编数"。**第二处独立编码已核**：`strategy/record_ledger.py:84-86` `MODE_BREAKS = {"2026-10-29": {"mode": "live", "baseline": 66542.31}}` —— 迁移日基线同时写进账本断点常量，两处互证。算术闭环 `66,542.31 − 8,337.31 = 58,205.00 = 100 × 582.05` 分毫不差 ⇒ **它是被种进去的，此后无限期无息（引擎只在 `total > peak` 时改写，而账户再没到过 66,542）** — `.workbuddy/memory/2026-10-29.md` 第 6 步 · `strategy/record_ledger.py:84-86` — 风险审计 v14 2026-11-04（live-ops-engineer-2 提出）
- [P1] [FORBIDDEN] **★ 值守陷阱一：日志前缀不可信 —— 卖出被 stale 挡掉时，日志打成 `BUY 拒绝`**。`_log_reject_once` 定义在 `engine/event_engine.py:3190`，里面是**硬编码** `logger.info("BUY 拒绝 %s: %s", code, reason)`（**`:3214`**），而该函数**买卖两侧共用** ⇒ 判 411.2857 是否被挡，**看 `code` 与 `reason`，不看前缀**。（live-ops-engineer-2 先前给的 `:3197` 是旧运行版行号，已由其本人自纠；正确为 def `:3190` / 日志 `:3214`。）— `engine/event_engine.py:3190/3214` — 风险审计 v14 2026-11-04
- [P1] [SAFE] **值守陷阱二：三样东西同时不动 = 主循环没跑，不是"没成交"**。`_persist_equity` 在 `_run_once` 内 ⇒ 若 **09:31 后** ① `engine_state.ts` 仍停 `11-03T15:04:42`、② `equity_snapshots` 今日 0 行、③ `risk_snapshots` 今日 0 行，**三者齐停**即主循环未运行（进程僵死 / pre_open 未退出），**先按引擎不工作上报，别先判"无行情"**。分母另有第二出处可判活：`logs/quant_system.log:82104` 已印 08:08:04 进程内实际加载的 `peak=63,846.65` ⇒ 不必依赖 `engine_state.ts` — `engine/event_engine.py::_run_once/_persist_equity` · `logs/quant_system.log:82104` — 风险审计 v14 2026-11-04（live-ops-engineer-2 提出）
- [P1] [INFO] **归因必须写对：11-03 的峰值是 09:30:03 砍的，不是重启砍的**。不能说"`:3939` 把峰值从 66,542.31 恢复成 63,846.65"——`logs/quant_system.log:81724`（11-03 **10:58:26**）已是 `peak=63207.94`，即 11-03 上午 DB 里**就已经不是** 66,542.31（09:30:03 恢复钩子已先砍过）；今晨 63,846.65 与昨夜落库值逐位相同 ⇒ **`:3939` 是忠实读取，重启对分母净变 0**。写成"重启砍的"会把修复指到错误的函数。另：`cash` 那 0.03 元**不是舍入链、是两个时点**（11,611.62 = 今日 08:08:40 券商同步真值，三处一致；11,611.65 = 11-03 落库值） — `logs/quant_system.log:81724/82104` — 风险审计 v14 2026-11-04（live-ops-engineer-2 自纠，我复核）

### 2026-11-04 08:55 执行链：卖出被拒后空转 30 秒（常量）+ 247.716 是地雷 + 我自纠一个编造的数

- [P0] [FORBIDDEN] **★ 卖出被拒后不会失败，而是"空转 30 秒/轮"，且这个 30 秒是硬编码常量**：`ORDER_TIMEOUT_SEC = {"BUY": 90, "SELL": 30}`（`engine/event_engine.py:494`）。10-30 实测一轮：`11:24:56.499 下单 @567.88 → 11:24:56.511 拒单 306050（12ms）→ 11:25:26.714 撤单 rc=0（撤一张 30.2 秒前就已死的单）→ 11:25:26.775 重下（61ms）→ 11:25:26.803 再拒`。11 轮、5 分 3 秒、**0 成交**。根因：`:3291-3320` 撤单只判 `now - ts > timeout`，**不看"是否已被拒"**。⇒ **11-02 的「触发→成交 63.652 秒」= 两个固定 30 秒周期，不是滑点延迟、与行情无关** — `engine/event_engine.py:494/3291-3320` · `logs/quant_system.log:72658~72947` — 风险审计 v15 2026-11-04（risk-controller-2 提出，我复核并修正机制）
- [P1] [INFO] **12 次 `306050` 是两个根因（n=11 / n=1），不能合并引用**：**10-30 的 `avail=0` 是真信息** —— 300308 当天 `10:08:11` 已卖出成交（`fills#385` @569.24），11:24 去卖的是**已经不存在的 100 股**，而 `:3714-3720` 只告警"券商侧可能拒单"就按整仓提交，**覆盖了一个正确的护栏**；**11-02 的 `avail=0` 才是"撤单后冻结未回补"**。⇒ "60~70ms 重下必吃 306050"在 11-02 **只有 n=1**，不得借 10-30 那 11 次的确定性。附带：`FLATTEN_MAX_ATTEMPTS = 3`（`:496`）本该封在 3 次，**10-30 实际拒 11 次 = 上限的 3.7 倍，上限没兜住** — `logs/quant_system.log:72640-72990` · `engine/event_engine.py:3714-3720/496` — 风险审计 v15 2026-11-04
- [P0] [FORBIDDEN] **★ 300308 `stop_price = 247.716` 不是脏值，是"当前不可达 + 一颗地雷"**：`247.716 = 501.5679 × (1 − 0.50612)` ⇒ `wide = max(0.18, atr_pct×6) = 50.61%` ⇒ `atr_pct = 8.44%` ⇒ **两个并列分支、均未验证**：**(a) avg 501.5679 + wide 50.61%**（需 11-03 当日 `atr_pct≈8.44%`；旁证 300308 已知成本只有 194.757 与 501.5679）；**(b) avg 302.09 + wide 18.00%**（`247.716/0.82=302.0927`，恰好吃下限；但 302.09 在现存记录里不存在）。**已排除**"194.757 时代残留"——算术不可能：`wide≥0.18 ⇒ stop≤0.82×avg`，avg=194.757 时 stop 上限仅 **159.70**，247.716 需要 `wide=−27%`。定论只差 `daily.atr_pct('300308.SZ')`（11-03），**未跑、不选边**。今日不咬：`exit_mode="trend"` ⇒ `:2030` 走 else、`:2033 if pos.stop_price <= 0` ⇒ 只设一次永不上移；唯一读它的 `trend_strategy.py:280` 位于 `# ---- 剥头皮模式 ----` 之后、trend 在此之前已 return ⇒ 不可达。**★ 但"取消豁免会更危险"这句我 09:2x 降级了（我自己的夸大，live-ops-engineer-2 提 `peak_price` 也归零，我顺算后自纠）**：scalp 下 `:2028 chand = peak_price × (1 − atr_stop_mult×atr_pct)`，`atr_stop_mult = 2.9`（settings:325）⇒ 系数 0.7552 ⇒ `peak_price=480.2` 时 chand≈**362.7**、`peak_price=508.01` 时≈**383.7** ⇒ **只要 `daily` 可用且 `atr_pct>0`，247.716 在 scalp 第一个 tick 就被抬到 ~363–384（自成本 −23%~−28%），轮不到 −50.6%。** 残余风险收窄为：**`daily is None` 或 `atr_pct<=0`（日线/行情不可用）时 chand 算不出 ⇒ stop 停在 247.716 ⇒ 它成为唯一下行判据**。⇒ 严重度由"必然 −50.6%"降为"**数据不可用日的 −50.6%**"，降一档、归零不了。**另：`peak_price` 每次重启清零不是小事——头皮模式的移动止损锚点每天重锚到当前价。** ★ 推论：**「取消 300308 豁免」在未对齐 `exit_mode` 之前是让保护变差**——现行为 −18%（411.2857）来自豁免分支 `:2229-2245`，撤掉后走 strategy，trend 模式不提供 −18%，而 247.716 又不被读 — `storage/qmt.db engine_state.positions` · `engine/event_engine.py:2028-2039` · `strategy/trend_strategy.py:250-282` — 风险审计 v15 2026-11-04
- [P0] [FORBIDDEN] **★ 拒单事件引擎看不见 ⇒ 那 30 秒压不下去；且被拒的委托照样吃掉日交易额度**：① `_pending_sells` 全库仅四处写（`:491` 初始化 / `:1094` 清空 / `:3241` 登记 / `:3309`+`:3342` 才 pop），**无第 5 处**；`core/broker.py:81-100 on_order_error` **只 `logger.error`、不回传引擎**，`_reconcile_pending_orders`（`:3278-3348`）也不读 `get_orders` 状态 ⇒ **30.2s 是等超时，不是等拒单**，修复须新增 broker→engine 回调（可压到 ~0.06s）。② `place_order` 下单瞬间返回 `ok=True`（拒单为异步回调）⇒ `:3741 if res.get("ok"): _daily_trade_count += 1` **对每张被拒委托也各记 1 笔**，`max_daily_trades=10` ⇒ **10-30 的 11 次拒单把当日额度刷穿、买入侧当天全废**。⇒ **`FLATTEN_MAX_ATTEMPTS=3` 是有效的（撤回我上一版"3.7 倍没兜住"）**：逐笔看 `理由=`，前 **3 笔** `daily_hard_stop_flatten`（用满即停 ✓），后 **8 笔** `趋势破位离场 +193.92%~195.33%` 来自**另一条无次数上限的路径** `_trend.on_exit → _handle_sell`，把同样的 30.2 秒循环又续了 **2 分 32 秒** ⇒ **上限有效 ≠ 保护有效；要修的是"额度耗尽后哪条路径还能下单"**。★ 旁证：那 8 笔反解 `avg_cost ≈ 194.17`（真实 582.05）⇒ **是"用错成本算出的离场信号"在驱动卖出**。另撤回"10-30 买入侧当天全废"：当日 `12: 13: 14: 15: 16:` **连续五小时零行**（对照 11-02 的 `13:` 1306 行、`14:` 1396 行 ⇒ 排除"日志只记上午"）⇒ **引擎下午高度可能未运行；只读日志无法区分"未运行"与"未落盘"** ⇒ 额度是否刷穿**不可观测**。★ 这条独立成案：10-30 上午 10:08:11 已卖出 300308 @569.24、11:24 起又对**不存在的 100 股**连下 11 单、12:00–16:59 五小时无日志 ⇒ **那五小时里没有止损/强平/T+1 判定/对账**，可能是"300308 那天没人管"的真正答案，建议单独立案查明。轮询周期实测 **3.015s**（不是 43~79s，我读错 `:2045` 历史注释）⇒ 修法收益 30.15s→3.0s（10×） — `engine/event_engine.py:3241/3309/3342/3741/3278-3348` · `core/broker.py:81-100` · `logs/quant_system.log` 10-30 小时分布 — 风险审计 v15 2026-11-04（risk-controller-2 回纠，我复核成立）
- [P1] [SAFE] **`open_date = null` 与 T+1 的交互已查实（关掉"未验证"）**：`engine/event_engine.py:96-97` `if open_date is None: return False`（docstring「视为可卖，不误杀」）⇒ **null → 不锁 → 卖单会送到券商**。反证（有 open_date 时确实拦）：10-30 11:24:53.420 `[T+1] 拦截当日卖出 510050.SH：买入于 2026-10-30 10:07:37`。**今日无差异**（两只均 11-03 买入、已过 T+1）。⚠ 但 `engine_state.positions` 里 300308 `open_date="2026-11-03T10:59:01.277211"`、`/api/snapshot` 里是 `null` ⇒ **DB 残留 ≠ 内存，别拿 DB 判 T+1** — `engine/event_engine.py:86-101` — 风险审计 v15 2026-11-04 — **【观测窗口 · 2026-11-05 00:5x 补记】上述 DB 残留值只存在于 `engine_state` ts=`2026-11-03T15:04:42` 那一行；自 `2026-11-04T15:04:44.779680` 起 DB 与 API 两处均为 null** ⇒ 明天有人去核会发现两边一致，**不要因此判定本条是误报**。另：`engine_state` 最后一笔写入仍是 11-04T15:04:44.779680 ⇒ **11-05 00:02 那次 pre_open 重启根本没写过 `engine_state`**（`_save_engine_state` 只在 `_persist_equity`/复位路径被调，pre_open 无快照），它既没造成也没「延续」这个 null，只是没有改善。**【结论升级 · 21:0x 补记】本条结论应从「拿 DB 核」升级为「DB 与内存都不可判 ⇒ 本地 T+1 闸对所有跨过重启的仓失效」** —— 旧结论只在 11-03 15:04:42 那个时点成立；**明日实际影响 = 0**，因为两只都是 11-03 建仓、早已过 T+1（即便 `open_date` 有值也不锁），且都在 `exit_exempt_codes` 里。**取数建议同上一条（别跑 `--date`）：用 `engine_state.ts = 2026-11-04T15:04:44.779680` 那一行作为唯一可核时点**
- [P0] [FORBIDDEN] **★ `max_daily_trades=10` 不是硬上限：额度双向失真 —— 假委托往上刷、重启往下抹**：① **往上刷**：10-30 `09:35:54,561 BUY 拒绝 510050.SH: daily_trades>10` 是当日第一次被锁，而此刻全天**只下过单、一次都没被受理**（`order_stock rc=-1`），按磁盘版 `:3742 if res.get("ok")` 计数应为 **0**，实际已 `>10` ⇒ **当时跑的是旧构建，`+=1` 在 `if` 之外，每次失败调用都记一笔**；09:35 前 **96 笔全失败 ⇒ 计数≈96** ⇒ 这给 `:3739-3741` 注释补上实证：**零成交就把额度打穿**。② **往下抹**：`:3929 self._daily_trade_count = int(row.get("daily_trade_count") or 0)` ⇒ **重启恢复的是上次落盘值**，未落盘的内存增量全丢 —— 实证 `10:07:04` 恢复 **0**（内存≈96）、`11:34:54` 恢复 **2**（内存 13）。⇒ **"11 笔 ok:True 应 2→13 但恢复 2"不是矛盾，是"计数不落盘"（内存 13 / 磁盘 2 / 重启取磁盘）。** ⇒ **任何"靠日交易笔数限损"的假设都不成立；盘中重启一次即还额度。** ⇒ 同时定死"买入侧全废"不成立：实际是 **09:35:54–10:07:04 锁约 31 分钟**，靠重启把计数读回 0 解开，`10:07:37` 即下 `510050 BUY 500 @2.86`（`orders#1087`）并于 `10:07:45` 成交（`fills#384`）—— **不是逻辑修好的，是数被抹掉的** — `engine/event_engine.py:3929/4066/3742` · `logs/quant_system.log:70662~72362` — 风险审计 v16 2026-11-04（live-ops-engineer-2 取证，我复核并解掉其标记的矛盾）
- [P1] [SAFE] **自纠：我编造过"两只持仓 avail=100/500"，已撤回**。`/api/snapshot.positions` 与 `engine_state.positions` JSON **都不含 `available` 字段**（落库键仅 `code/name/quantity/avg_cost/last_price/open_date/peak_price/stop_price/target_price`）⇒ **avail 当前不可观测、无出处**。机制上它**不阻挡卖出**：`:3712-3720`「avail>0 夹紧 / avail<=0 **只告警仍按整仓提交**」⇒ avail=0 不会让卖单消失，只会让它带着"券商侧可能拒单"出去 —— 而这正是上一条 11 连拒的入口 — 风险审计 v15 自纠 2026-11-04
- [P2] [INFO] **口径规则：总值 / 净值不可混行**。`54,182.22`（= 11,611.65 + 41,128.57 + 1,442.00）是**成交瞬时总值**；扣 `TRADE_COST_PCT=0.0015`（settings:966）后 `41,128.57×0.15% = 61.69` ⇒ **净值 `54,120.53`** ⇒ 回撤 **−18.67%（预算 93.3%）**（总值口径为 −18.58%/92.9%）。差 0.0925pp，**不改变任何判**，但**"54,120.53 配 −18.58%"是混口径，复核会核不上**（risk-controller-2 表格第 1 行即此情形，其第 2/3/4 行均为净值口径、自洽） — `config/settings.py:966` — 风险审计 v15 2026-11-04

### 2026-11-04 09:10 三项：撤回一条旁证 + 三源静默改判主机级 + 落盘静默失败

- [P0] [FORBIDDEN] **★ 撤回：10-30 那 8 笔趋势离场的 `+194%` 是真实盈利，`avg_cost` 没有算错**。我先前写"反解 `avg_cost≈194.17`、真实 582.05 ⇒ 是用错成本算出的离场信号在驱动卖出"——**错**。三行独立反解逐位相同：`570.710−37654.13/100 = 567.880−37371.13/100 = 573.430−37926.13/100 = **194.1687**` ⇒ **194.17 就是真实历史成本**（该仓低价区建仓）；**582.05 是 10-29 的市价**，`:4250` 那条修的是 **`last_price`**，不是 `avg_cost`。⇒ 按"口径错在发作"写会派人去修不存在的东西。**真正的荒谬在别处、更该写：同一瞬间系统一边判出 −86.58% 日亏损并触发强平，一边在委托单上记 `盈亏 +37,371`**（与 `risk_snapshots` 10-30 末条 `halt_reason="daily_stop -86.58%"` / `daily_pnl=+37,448.30` 同源）⇒ **禁止用任何单一字段描述 10-30。** — `logs/quant_system.log` 10-30 提交卖出委托行 · `engine/event_engine.py:4250` — 风险审计 v17 2026-11-04（risk-controller-2 逐位反解，我复核并撤回）
- [P0] [FORBIDDEN] **★ 10-30 下午不是"引擎崩了"，是"三源同时静默"的主机级断档 —— 5h42m 无止损/无强平/无 T+1/无对账**：`quant_system.log` werkzeug 末行 **11:47:25.774** → 恢复 **17:24:40.631**（**5h37m15s**）；`guardian.log` 心跳末行 **11:45:03** → 恢复 **17:26:55**（**5h41m52s**）。且当日有**两次**事件：`11:33:02 检测到外部 STOP_ENGINE → 优雅退出`；**`11:34:54 又起来了`** ⇒ **所以"下午没引擎"不是 11:33 停的，是 11:34 重启后、11:47 整个主机一起没了（守护进程自己也停 ⇒ 崩溃解释被排除，指向休眠/关机）**。★ 当日下午实际敞口只有 510050 500 股 ≈1,430 元（300308 已 10:08:11 卖出）⇒ **未造成损失是运气不是设计**；同一模式若落在 300308 占 78.63% 的某天，就是五个多小时无任何保护。 — `logs/quant_system.log:72952-72958/73243` · `logs/guardian.log` — 风险审计 v17 2026-11-04（risk-controller-2 查 guardian，我复核全部时间戳）
- [P1] [FORBIDDEN] **★ `engine_state` 落盘失败是静默的，而它是重启时唯一被恢复的表**：`:3887-3892 save_equity_snapshot` 与 `:3895→:4063 save_engine_state` 是**两个独立 try**，后者异常只打 **`logger.debug("保存引擎状态失败")`（`:4076`，默认不输出）**。实证：`11:24:53.418` 已 `halted=True`，**`11:34:54` 重启却恢复 `halted=False` / `peak=66,542.31`** ⇒ 落库行停在 11:24:53 之前；同期 `equity_snapshots` 一直在写（11:25:41…11:29:43 全有）⇒ **一张成功、另一张静默停滞**。**重启恢复的持仓/现金/`peak_asset`/`peak_equity`/`daily_trade_count`/`risk_state` 全来自它** ⇒ 它陈旧一格，重启就把账户恢复成旧持仓+旧分母 ⇒ **66,542.31 那类分母漂移的同类土壤**。要求：把该失败升 `logger.error` 并在 `/api/snapshot` 暴露"上次落盘时刻"。 — `engine/event_engine.py:3887-3895/4063/4076` — 风险审计 v17 2026-11-04（risk-controller-2 发现，我复核）

### 2026-11-04 08:50 两项：11 笔计数"未解"（撤回我的解释）+ 10-30 跑的是 10-29 构建

- [P1] [INFO] **撤回：那 11 笔不是"发生在最后一次落盘之后"—— 我的解释被证伪，"是否计入额度"至今未解**。`equity_snapshots` 10-30 实查 `11:24:41 / 11:25:41 / 11:26:42 / 11:27:42 …`（≈60.3s/条，**每条都过 60s 节流闸**）⇒ `_save_engine_state()`（`:3898` 全模式落库）在该期间至少跑 5 次，**前 9 笔都早于 `11:29:43`** ⇒ 若各加一，落盘应带 ≥9，实测恢复 **2**。另排除两种解释：`storage/db.py:380-393` 为 `INSERT OR REPLACE ... id=1`、`load:430-437` 为 `SELECT * WHERE id=1`（**单行覆盖，不存在读旧行**）；全库 `保存引擎状态失败` **0 次**。★ 代码考古：`c1ff141`(10-29 22:50，当天运行版) 与 `5de0188^` **两版卖出侧都是无条件 `+=1`**（在 `if res.get("ok")` 之外）⇒ **按代码必带 13，恢复却是 2，矛盾不能靠"构建不同"消解**。⇒ 定稿：**机制（往下抹）已证；"是否计入"= 未解，不猜**。**`=2` 不许当"没计入"、`=13` 不许当"计入了"，更不许据此写"在 306050 回调里把额度减回去"**（若从未加过，减回去会打成负数或误放行）—— **修法须等此数定案。** — `storage/qmt.db equity_snapshots` · `storage/db.py:380-437` · `git show c1ff141:engine/event_engine.py` — 风险审计 v18 2026-11-04（live-ops-engineer-2 证伪，我复核并考古）
- [P1] [FORBIDDEN] **★ 纪律（已自我推翻一版，现为定稿）：凡引用 10-30 盘中行为，必须标注"那是当天那个**未提交工作树**的行为，git 里复现不了"** —— 我先前写的"10-30 跑的是 `c1ff141`"**已撤回**。硬证：`git show c1ff141:core/broker.py:81-82` 是 `def on_order_error(...): pass`（一个字节都不打），而 10-30 日志有 `11:24:56,511 [BROKER] 委托被拒 order_id=1082242609 error_id=306050` ⇒ **运行时已含 17:26 才提交的改动** ⇒ **既非 `c1ff141`、也非 `5de0188^`**。⇒ 两个反向风险：**拿某个提交去批 10-30 会修错地方；拿 10-30 观测去否定当前代码会得到假矛盾**。★ 配套权威数字（`5de0188` 提交信息原文）：「**全天 643 笔委托 0 成交（300308 卖出重试 642 次 + 510050 探针买入 1 次）**」+「原实现 **642 次失败把计数刷到 643** > `max_daily_trades=10`」 ⇒ ① 我们数到的 **11 笔只是带 `306050` 的那部分**，其余 631 笔是本地 `invalid price type` 直接拒（不产生 `[BROKER] 委托被拒` 行）⇒ **"11 笔"是小样本不是全量**；② **内存 643 / 落盘恢复 2，差 641** ⇒ **`engine_state.daily_trade_count` 与实际委托数不同源，今天不能用于任何事后判定**，只能 `orders`+`fills`+日志三处对。 — `git show c1ff141:core/broker.py` · `git show 5de0188` 提交信息 · `logs/quant_system.log` — 风险审计 v19 2026-11-04（先自设一版、再自推翻）

### 2026-11-04 08:56 两项定稿：重启把额度 497→0（P0 量级 ×5）+ 落盘失败不可见

- [P0] [FORBIDDEN] **★ `max_daily_trades=10` 在本系统里根本不构成上限：已涨到上限 50 倍（497），一次重启归零**。备份库 `qmt_pre_pxtype_20261030_100319.db`（`mode=ro`）实读：`09:56:04 engine_state.daily_trade_count = **497**` 且 `tick_count = **497**` 逐位相等 ⇒ `09:30:53→09:56:04 = 1,511s ÷ 3.04 ≈ 497 轮`，即**每轮一笔失败卖单、"失败也计数 + 每轮一笔"定量证实**；而同一天 `10:07:04 重启恢复 = **0**`（`:72362`）。⇒ **一次重启把 497 归零**。⇒ 结论从"不是硬上限"升级为"**不构成上限**"：**任何靠日交易笔数限损的设计在本系统内无意义**（涨到上限 50 倍、再一键回 0）。★ 另：11:24–11:29 那 11 笔是否计入**仍未解**（内存 643 / 落盘 2 差 641），三个分辨器仍未闭环，**不猜**；**修法须等此数定案**（若从未加过，"在 306050 回调里减回去"会打成负数或误放行）。 — `storage/qmt_pre_pxtype_20261030_100319.db` · `logs/quant_system.log:72362` — 风险审计 v20 2026-11-04（live-ops 取数，我复核）
- [P1] [FORBIDDEN] **★ "落盘失败"不能用"日志里没有"来排除 —— 该日志是 `logger.debug`，`LOG_LEVEL=INFO` 永远不打**：`_save_engine_state` 的 `except` 在 `:4075-4076` 是 `logger.debug("保存引擎状态失败")`；且异常若发生在 dict 组装阶段（`p.open_date.isoformat()` / `self.risk.export_state()` / `json.dumps`）**根本到不了 db 层的 `logger.warning`**。⇒ **"全库 0 次"只证明"日志看不见"，不证明"没有失败"**（我和 live-ops 各自都犯过这条推理，两侧同时撤回）。⇒ **"静默落盘失败"成为 11 笔之谜的最强候选**（`engine_state` 自某刻停在旧值），但仍**未验证**。**今天可现场判定**：`select max(ts) from equity_snapshots` 前进而 `engine_state.ts` 停住 >2 分钟 ⇒ 判定成立。要求：升 `logger.error` + `/api/snapshot` 暴露"上次落盘时刻"。 — `engine/event_engine.py:4063/4075-4076` · `config/settings.py:1081` — 风险审计 v20 2026-11-04
- [P2] [INFO] **引用备份库必须写「内容时点 = 其中 `max(ts)`」，不是文件名时点**：备份只复制 `.db` 未复制 `-wal` ⇒ 内容落后约 7 分钟（名字 `10:03:19`，内部 `equity max=09:56:04` / `risk max=09:56:16` / `orders max=09:56:52`，主库 09:57–10:03 另有 7 条）。⇒ **建议团队约定：引用任何历史数字须给「版本 / 时点 / 载体」三件套**（本轮已在三种变体上各摔一次：用错版本 `c1ff141`、用错时点 `day_open=peak=63,207.94`、用错载体时点）。 — `storage/qmt_pre_pxtype_20261030_100319.db` — 风险审计 v20 2026-11-04（live-ops 提出，我采纳）
- [P1] [INFO] **★ 拒单"无消费者"今天可实测，但判据是时间差 Δ，不是"有没有被清掉"（我第一版判据是错的，会假阳性）**：`core/broker.py on_order_error` 只 `logger.error`，**五项状态全不碰**（`_pending_orders` / `_pending_sells` / `_daily_trade_count` / `_reject_latest` / `_avail0_warned`）⇒ 连"记一次拒单"都不做。**⚠ 但"`_pending_sells` 被清掉 ⇒ 已修"是错的判据** —— 全库只有两个 pop 点（`:3309` 成交、`:3342` **超时撤单**），而 `ORDER_TIMEOUT_SEC["SELL"]=30`（`:494`）⇒ **无论有无消费者，30 秒必被清** ⇒ 该判据今天 100% 假阳性。**正确判据 = Δ（`306050` 出现 → `_pending_sells.pop`）**：**Δ < 5 秒 ⇒ 有消费者（版本已修）；Δ ≈ 25~31 秒 ⇒ 无消费者（等的是超时）**。★ 今天可预设：`core/broker.py` `git diff HEAD` 空、函数体只有 `logger.error`、mtime **10-30 09:58:04 < 进程启动 11-04 08:08:40** ⇒ **今天 Δ 必然 ≈30 秒**；**若真观察到 Δ < 5 秒，那异常不是"M1 错了"，而是"有人在盘中改了 `broker.py` 并重启"** —— 这才是要立刻上报的。 — `core/broker.py:81-100` · `engine/event_engine.py:3241/3309/3342` · `settings` 无 · `:494` — 风险审计 v21 2026-11-04（risk-controller-2 打回判据，我采纳并改 Δ）

### 2026-11-04 15:40 DAILY-REVIEW（归集后唯一每日任务 · 七块全跑）

- [P0] [SAFE] **★ 已修｜`review_daily.py` 的 live 双计「修复已落盘但已失效」——根因是传了已 close() 的连接**：11-04 08:30 已加 `_compose_total_pnl` 按模式分流，但今日仍输出 `total_pnl=7,943.21`（= 4,740.00 已实现 + 3,203.21 未实现，双计）。根因：`c.close()` 在 `:1656`，`_eff_mode = _day_exec_mode(c, ...)` 在 `:1730` ⇒ `sqlite3.ProgrammingError` 被函数内 `except Exception: pass` **静默吞掉** ⇒ 返回 `''` ⇒ 回落 paper 加法。实证：连接打开时返回 `'live'`，关闭后返回 `''`。修复：连接关闭前先判 `day_mode`（`:1676-1683`），`:1746` 复用。验证 11-04 = `4,740.00`（逐位 = 66,411.12−61,671.12）、11-03 回归 `-2,134.29`（原 −4,268.58）。守卫：`tests/test_review_pnl_caliber.py` +3 例，含 **AST 接线守卫**（字符串 index 第一版被自己的注释骗过）— `strategy/review_daily.py:1656/1676-1683/1746` · `tests/test_review_pnl_caliber.py` — 发现于 DAILY-REVIEW 2026-11-04
- [P1] [SAFE] **已修｜零成交日无参运行 `review_daily.py` 会静默覆盖上一日复盘**：今日 0 成交，`python strategy/review_daily.py`（DAILY-REVIEW 的调用形式正是无参）目标日解析为 **11-03**，把 `logs/review_2026-11-03.json` / `reports/review_2026-11-03.html` **重写覆盖**，屏幕打印上一日日期、看起来"正常完成"，而**当日复盘根本没生成**。修复：新增 `_default_target_date()` —— 今天是交易日就取今天，否则回落最近有成交日 — `strategy/review_daily.py::_default_target_date` — 发现于 DAILY-REVIEW 2026-11-04
- [P0] [FORBIDDEN] **[待 OWNER 重启] `engine/observability.py` 改动未进内存（08:45:38 > 进程启动 08:08:40）**：该模块由 `engine/event_engine.py:66` 导入、`:1555 Observability(self)` 实例化 ⇒ 今天跑的是旧版。修复内容是把 `asset_basis` 兜底从 `""→"local"` 改为 `"unknown"`、`asset_trusted` 从补集写法改为显式白名单（**原缺陷：「风控一轮没跑」被显示成「用本地口径在判定，且可信」并点亮绿色 safe 徽章**）。**影响面仅 `/api/snapshot` 的展示语义，不参与下单/风控判定；今日实测 `asset_basis="broker"`，兜底分支未被走到 ⇒ 今日无实际错报**。已本地提交，内容未改 — `engine/observability.py:147-166` · `engine/event_engine.py:66/1555` — 发现于 DAILY-REVIEW 2026-11-04
- [P1] [FORBIDDEN] **★ 收盘后「行情源失效」是每交易日的常态 ERROR（假告警）**：`2026-11-04 15:02:05 [ERROR] SYSTEM: [系统提示][行情] 行情源失效：35 只标的快照已陈旧 2 分钟…请检查 miniQMT 行情连接`。15:00 收盘后快照不再更新，2 分钟阈值必然触发 ⇒ **每天用一条 ERROR 收尾、写着「请检查行情连接」，而真实原因是市场已收盘**。历史统计：11-02 / 11-03 / 11-04 每日均 1~2 对 WARNING+ERROR，10 月各交易日同样。连续误报会让真行情故障失去区分度。修法：stale 判定加「非交易时段不告警」守卫（引擎代码，需重启）— `logs/quant_system.log` 11-04 15:02:05 · `engine/event_engine.py` stale 判定 — 发现于 DAILY-REVIEW 2026-11-04
- [P1] [FORBIDDEN] **[待 OWNER] `benchmark_daily.jsonl` 11-04 是「账户分量新、基准分量旧」的混合行 ⇒ `excess_pct` 无效**：`account_pct=-0.93358888`（分母仍是 paper 1,000,000），但 `universe_bh_pct=-0.23609898` / `index_pct=-0.29655867` **与 11-03 逐位相同** ⇒ 今日基准未刷新，`excess_pct=-0.6974899` 是「今天的账户 − 昨天的基准」。叠加盘前已查明的「非交易日复制上一日行」，**同一文件有两种污染方式** ⇒ KPI#4 无法从它读任何结论 — `logs/benchmark_daily.jsonl` 11-04 行 · `engine/benchmark_feed.py` — 发现于 DAILY-REVIEW 2026-11-04
- [P1] [INFO] **研究面板末日停在 2026-10-28，落后 5 个交易日，缺的正是反弹最强的 5 天**：`ts_panel_25_901_20211201_20261028.pkl`（10-28 19:57）。旁证：live vs panel 末日差 `300394 −32.5% / 300502 −23.5% / 300308 −16.2%`。⇒ 任何用该面板做的研究都会**系统性低估当前动量**，用它判断「现在该不该开仓」会得到相反结论。**只报不刷** — `storage/hist_cache/ts_panel_*_20261028.pkl` · `reports/measure_hygiene/HYGIENE_20261104.md` — 发现于 DAILY-REVIEW 2026-11-04
- [P0] [INFO] **今日账户创 live 新高 `peak_asset = 67,453.62` > 原真实 HWM 66,542.31 ⇒ 「回撤分母虚低」当前已自然消解，但缺陷未修**：`risk_snapshots` 今日 3,315 条，`peak_asset` 09:31:08 `63,846.65` → 13:45 起逐笔棘轮上行 → 15:00:05 `67,453.62`。⇒ 前序 P0-1（恢复钩子重置峰值）**不再有当前后果，但下次熔断恢复仍会再砍一次**。凡写「预算使用率」仍须注明分母有效期 — `risk_snapshots` 11-04 · `risk/manager.py:352-353` — 发现于 DAILY-REVIEW 2026-11-04
- [P2] [INFO] **`range_pct` 名不符实（第 2 次量化）**：今日账本 `range_pct=-0.2`，真实日内振幅 **12.16%**（60,095.62 @13:16:10 → 67,404.12 @14:35:34）。它实为「自 MODE_BREAK 基线 66,542.31 起的累计收益」（66,411.12/66,542.31−1 = −0.197%）。任何拿它当日内波动设阈值的用法都会错两个数量级 — `logs/evolution_ledger.jsonl` 11-04 行 · `scripts/record_ledger.py:516` — 发现于 DAILY-REVIEW 2026-11-04

### 2026-11-05 盘前 预算定价审查 v22（只读 · 改前必读）

- [P0] [FORBIDDEN] **★ 明日的分母只有一个，是 `67,453.62`；`66,542.31` 不参与任何熔断判定**：`engine_state(11-04 15:04:44) peak_asset=67453.62`，`logs/quant_system.log:83345` 00:02:16 `恢复风控状态: peak=67453.62` ⇒ 当前回撤 (66,411.12−67,453.62)/67,453.62 = **−1.5455%、占 −20% 预算 7.73%**；按 MODE_BREAK 66,542.31 算则是 **−0.1972% / 0.99%** ⇒ **两者差 6.74pp 预算、触发线下移 729.05 元**。凡报告写"预算使用率"必须写明分母 = 67,453.62 — `risk/manager.py:155-161` · `config/settings.py:924` · `storage/qmt.db engine_state` — 风险审计 v22 2026-11-05（risk-controller-4）
- [P0] [FORBIDDEN] **★ 洗分母的触发条件收敛到「单靠 −6% 日线一天就够」，不必等 max_drawdown 熔断**：`_maybe_recover` 首行 `if not self._halted: return _maybe_recover_zombie()`（`manager.py:338-340`）⇒ **当前 halted=false，明日盘中 `:352-353` 不会触发**；但 −6% 单日强平走 `_halt(reason="daily_stop …")`（`:141-154`）不是 `max_drawdown` 前缀 ⇒ 冷却取 `halt_recover_days=1`（`settings.py:931`）⇒ **11-06 首轮 held=1≥1 即 `_peak_asset = total_asset`**。算例：明日收 62,426.45（−6%）⇒ 峰值改为 62,426.45 ⇒ −20% 线从 53,962.90 掉到 **49,941.16**，**凭空多让出 3,984.67 元（正好等于刚亏的那笔）**，页面回撤显示 0% — `risk/manager.py:331-358` · `settings.py:925/931` — 风险审计 v22 2026-11-05（承接 v14 第 248 行，给出最小触发条件）
- [P0] [SAFE] **明日 −18% 硬止损物理上不可达；真触发后**不**越 −20%，只剩 238.82 元缓冲**：创业板 ±20%（`strategy/backtest_daily.py:438/1071-1073`）⇒ 跌停价 533.38×0.8 = **426.704 > 411.286** ⇒ 单日不可达（最快 11-06）。若成交：亏 (533.38−411.286)×100 = **12,209.40 = 总资产 18.39%**、= 持仓市值 **22.89%**；余额 **54,201.72**（扣 0.0015 成本 **54,140.03**）；回撤 **−19.646%**（净 −19.737%）⇒ **未越 −20%，缓冲仅 238.82 元 = 0.441% 权益**（等价 510050 再跌 16.34%，或 300308 滑价 2.388 元 / 0.58%）。⇒ **−18% 止损线与 −20% 断路器只差 0.58%，是"同一条线"，不构成两道防线** — `engine/event_engine.py:2223-2245` · `settings.py:339/924` — 风险审计 v22 2026-11-05
- [P0] [FORBIDDEN] **★ `daily_loss_limit_pct=-3%` 在 6.6 万账户上是死参数，日内亏损熔断比 −6% 强平线还宽**：`_daily_loss_limit = max(5000, 3%×66,411.12=1,992.33) = 5,000`（`manager.py:246-262` 取较宽松者）⇒ 5,000 元 = **权益 7.53% > 6%**，且该线**只在 `can_open/on_fill` 判、资产更新路径不判**（`:141-162` 无 daily_loss 分支）⇒ 它永远追不上 3,984.67 的 −6% 强平线，**不是独立的第二道闸**。另：题目常问的 `daily_loss_pct` / `halt_dd_recover` 两个键名**不存在**，真值是 `daily_loss_limit_pct` / `dd_recover_days` — `risk/manager.py:222-229/246-262` · `settings.py:910/911/925` — 风险审计 v22 2026-11-05
- [P0] [FORBIDDEN] **★ 80.31% 不是"涨出来的超标"，是结构性不可合规：一手 300308 = 40% 上限的 2.01 倍，卖了也买不回来**：53,338 / 66,411.12 = **80.31%**，上限 = 66,411.12×0.40 = **26,564.45** ⇒ 倍数 **2.01×**；买入时成本 50,156.79 = 今日权益 **75.52%**（当时权益更低 ⇒ 占比更高），**建仓时刻就已越线**，唯一通道是 `owner_reentry` 的 `allow_large_position=True`（`manager.py:190-215` 只放开这一条，原文：「不跳过这一条就永远接不回原仓位」）。减仓 = **整仓清空**（100 股不可分割），落袋 +3,181.21、账户转 97.8% 现金；**但 `owner_reentry.valid_until=2026-11-03` 已过期（`engine:2652-2661`），常规路径被 `pos_ratio>0.40` 拒单（`manager.py:211-215`）⇒ 卖了必须 OWNER 重开配置才买得回** — `engine/event_engine.py:2620-2637` · `settings.py:657-673/934` — 风险审计 v22 2026-11-05
- [P1] [SAFE] **「熔断不拦卖出」是无害但不救 —— 让 −6% 线失效的是豁免，不是 `_halted`**：`_halted` 只被 `account_block_reason(:184)` / `can_open(:205)` 读 ⇒ 只冻开仓；而强平本来就剔除豁免仓（`engine:2156 → :2488-2493`，两只都命中 `exit_exempt_codes`）⇒ **−6% 触发时提交 0 笔**，只打 WARNING「仅保留 −18% 灾难止损」。所以 11-02 那种"最危急时刻清掉战略底仓"已修好，代价是 **80% 权益只剩一条 22.89% 深、且限价单每 30 秒才重试一次的出口**（`ORDER_TIMEOUT_SEC["SELL"]=30` @`:494`，撤单只判超时 `:3317-3319`，`broker.py:81-100` 拒单无消费者 ⇒ 63 秒 = 两个 30 秒周期）— `engine/event_engine.py:2488-2524/494/3317-3319` · `core/broker.py:81-100` — 风险审计 v22 2026-11-05
- [P1] [FORBIDDEN] **★ 唯一给 OWNER 的裁决项：300308 是否继续留在 `exit_exempt_codes`**。不裁决的最坏路径（逐笔）：D1 跌停 426.70 → 权益 55,743.52（−17.36%）；D2 一字 341.36 → 权益 **47,209.44（−30.01%）**；全程 −6% 线不卖、−18% 止损挂在跌停价上 30 秒一轮空转、−20% 断路器只 `_halt` 不卖 ⇒ **账户 −30.01% 而全程 0 笔卖出**；之后恢复钩子把 peak 重置为 ~47,209 ⇒ 新 −20% 线 37,767.55、**回撤显示 0%**，再让出 9,441.89 元。★ 另：`exit_exempt_codes` / `owner_reentry` **均需重启才生效**（`settings.py:624/666` 自述 + 全仓 grep `importlib.reload|reload(` 仅命中注释、0 处调用），**"热读"在代码层面是假的** — `settings.py:723/657/624` · `risk/manager.py:352-353` — 风险审计 v22 2026-11-05

### 2026-11-05 盘前 三条新事实的否决式审查 v23（mock / open_date / 账本）

- [P0] [FORBIDDEN] **★ mock 是"卖出链的 0/1 可用性前提"，不是降级——它让 v22 的 −18%/−6% 全部变成纸面数字**：`mock_trade_block_reason` 在 `_handle_sell` 第二次判定处直接 return（`engine/event_engine.py:3664-3667`）⇒ **`ALLOW_MOCK_TRADING=False` 下所有卖出（含 −18% 硬止损、510050 灾难止损、日内强平）一律不提交**；自愈只有一次机会：`_reattach_market_data()` 仅在 `_log_session_transition(True)` 的 False→True 那一瞬被调（`:1470-1488`），失败即 `:597-600` 报"本时段全部买卖已禁用"且当段不再重试。敞口 **54,799.5 = 82.52% 权益**在 mock 窗口内**零出口**：一日跌停 −10,667.60（−16.06%），两日 −19,201.68（−28.91%，对峰值 −30.01%、金额 −20,244.18）。★ 熔断不会误触发（`:2127` 走 `_risk_total_asset()` 只认券商值，`:4629-4680` 不可信即 return None），**所以是"没有保护"而不是"保护错乱"** — `core/qmt_client.py:383-405` · `engine/event_engine.py:3664-3667/1470-1488/597-600` — 风险审计 v23 2026-11-05（risk-controller-4 复核 live-ops 取证）
- [P1] [FORBIDDEN] **★ A 与 B 不会同时叠加，但会在同一天接力，且"同一次重启既是 A 的解药、又是 B 的病因"**：mock 窗口内 `_handle_sell` 在发单前就 return ⇒ **B 的 306050 根本发不出去**，所以"既发不出也发错了"**不成立**。真实序列是：重启后新进程 `_session_state=False` ⇒ 首个开盘 tick 再走一次 False→True ⇒ **重启=多给 A 一次自愈**；而同一次重启清空 `_pos_meta`（全仓仅 4 个触点 `:526/:3549/:3339/:4248`，`_pos_meta` **无任何 DB→内存回灌**，`:3965 return` 在带 open_date 的反序列化 `:3970-3985` 之前）⇒ **造出 B**。同日最坏合计：mock 窗口跌停 −10,667.60 + 当日新仓（≤11,611.62，现金上限）因 B 无法止损 −2,322.32 = **−12,989.92 = −19.56% 权益**，正好压在 −20% 线下沿 — `engine/event_engine.py:3949/3965/3970-3985/4248` — 风险审计 v23 2026-11-05
- [P0] [FORBIDDEN] **★ 账本覆盖是无条件的，比"缺字段才降级"更宽：三份 review JSON 的 `date` 字段全是 None**：实读 `logs/review_2026-11-02/03/04.json` 三者 `date = None` ⇒ `build_row` 的 `"date": rep.get("date") or _latest_review_date()`（`record_ledger.py:158`）**恒回落到最新文件 = 2026-11-04**；`append_jsonl` 按 `r["date"] == row["date"]` **整行替换**（`:701-715`）⇒ **`--date 2026-11-02` 把 11-02 的数字写进 11-04 那一行**，而屏幕打印的是 `target`（`:849-856`）⇒ 打印 11-02、实际毁 11-04。`_is_repaired` 只保护带 `fix/eod_basis` 的 2/27 行，11-02/03/04 三行**可被无条件覆盖**，行数不变 ⇒ `--check-gap` 仍报"无断供"。⇒ **在修好 `date` 字段前，禁止跑任何带 `--date` 的 record_ledger** — `strategy/record_ledger.py:125-135/158/701-715` · `logs/review_2026-11-0*.json` — 风险审计 v23 2026-11-05
- [P1] [SAFE] **★ 自纠两条（我方 v15 与 v22 各错一处）**：① **v15「撤掉豁免后 trend 模式不提供 −18%」是错的**：`strategy/trend_strategy.py:243-245` 在 `exit_mode=="trend"` 分支第一句就是 `hard = hard_stop_pct; if pnl_pct <= hard: 硬止损` ⇒ 移除豁免**不会**失去 −18%，反而多出「−6% 日内强平 + 趋势破位 + 120 日超时 + 暴跌」四条。② **v22「卖了买不回来」只对引擎自动路径成立**：`owner_reentry` 已过期 + `can_open` 会以 `pos_ratio>0.40` 拒单（`manager.py:211-215`）⇒ 引擎不会自动回补；但 **OWNER 在券商端手工买回可行**，`_sync_broker_positions` 会同步进来（`open_date=None`）、`_is_manual_exempt` 仍命中 `exit_exempt_codes` ⇒ **−18% 照常生效** — `strategy/trend_strategy.py:243-245` · `engine/event_engine.py:4242-4283` · `risk/manager.py:211-215` — 风险审计 v23 2026-11-05（自纠）

### 2026-11-05 00:5x 主理人收口（去重：mock / open_date / 账本 `--date` 三条已由 v23 登记，此处不重复）

- [P0] [INFO] **★ 销账｜`engine/observability.py`「改动未进内存」已闭**：整机 00:00:52 重启、引擎 00:02:15 起，实测 `/api/snapshot` = `asset_basis=unknown / asset_trusted=false`。判据是**字符串而非 mtime**：全仓 `grep "unknown"` 只命中 `observability.py:159`（`event_engine.py:463` 初值为空串）⇒ 面板出现 unknown 只能是新文件在内存。旧代码在 pre_open（风控一轮未跑）会给 `local` + `trusted=true`。⚠ 新歧义：09:31 后应变为 `broker/true`，那是设计正确值，不要误读成「又坏了」 — `engine/observability.py:159/165-166` — 发现于 复核 2026-11-05（live-ops-engineer-4）
- [P2] [INFO] **【已推翻→降级】「mock 会写出假价 EOD」在 live 下不成立**：原主张（mock 不禁快照 ⇒ 合成价 EOD 混入账本）被 live-ops 用不变量证明推翻，主理人已读码复核确认 —— `_total_asset()`（`:4589-4618`）只在 `bt>0 and age<=120` 时返券商值，`_risk_total_asset()`（`:4656-4682`）的不可信判据 `bt<=0 or age>120`（`:4664`）与之**严格互补**，而 `risk_asset is None` 时 `_persist_equity` **直接 return、一条快照都不写** ⇒「快照被写入」与「写的是券商总资产」互为充要。**残余**：① 两函数各自读时钟，120s 边界有 1/数千轮的双结论窗口；② 该不变量靠**行级巧合**维持、**无测试钉住**，谁把 `_total_asset()` 的 TTL 改成 300s 缺口立即打开；③ `_risk_total_asset()` 注释只说「不可信返 None」，**没写「返 None 时必须不写快照」**。⇒ 正确修法不是给账本加 `quote_src`，而是**把这个不变量写成一条测试**（mock 价 + 券商陈旧 ⇒ `equity_snapshots` 零新增） — `engine/event_engine.py:4589-4618/4656-4682` — 主张于 data-ledger-auditor-3，**推翻于 live-ops-engineer-4 2026-11-05**
- [P2] [INFO] **零成交日现金仍会变动 ⇒「cash 未变」不能反证「无隐藏成交」**（并作废主理人 00:20 的错归因）：11-04 `fills=0`，但隔夜券商 cash `11,611.62 → 11,611.60`（−0.02，结算/费用尾差）。11-05 券商现值 66,411.10 = 本地拼装 **逐位相同**，与 11-04 落库 EOD 66,411.12 的 0.02 差是**同一笔钱在两个时点**，不是哪一路拼装坏了。要证「无隐藏成交」只能查 `fills` 表 — `equity_snapshots` 11-04 末条 · `engine_state` — 发现于 复核 2026-11-05（主理人自纠）
- [P1] [INFO] **11-03 有「盘中重启 + 9.5 秒后成交」实证，别把「重启=当天休战」当默认**：重启 `10:59:00` → `fills id=390` 300308 BUY @501.5 `10:59:10`。直接抬高 v23 那条「盘中重启造出 B（T+1 闸失效）」的现实相关度 — `logs/quant_system.log` 11-03 10:59 · `fills id=390` — 发现于 复核 2026-11-05（主理人）
- [P2] [INFO] **账本 `eod_basis` 三行全 None 是「日常路径根本不写」，不是漏写**：全仓唯一写入点 `scripts/_fix_ledger_1030_eod_20261101.py:81`；`annotate_eod_freshness`（`record_ledger.py:403-438`）只写 `eod_ts`/`eod_is_close`。后果：10-30 那行 `eod_ts=null` 却说 `eod_is_close=true`（当天末条快照 11:29），全靠旁边一个 `eod_basis` 撑着 —— **该字段真在干活，但只在这一行干活** — `strategy/record_ledger.py:403-438/664-665` — 发现于 复核 2026-11-05（data-ledger-auditor-3）
- [P3] [INFO] **【live 不可达→降级工程债】「`_apply_pending_reset` 会清掉 live 全序列并落 1,000,000 假行」两个机理均被证伪**：① **调用点受分支守卫** —— `engine/event_engine.py:565 if self.exec_mode == "paper":` → `:567 self._apply_pending_reset()`（全仓唯一调用点），`:568 elif live:` 只调 `_restore_engine_state(restore_ledger=False)` ⇒ **live 会话永不执行，`.reset_paper.flag` 在 live 下是死文件**；② **DELETE 带模式锁** —— `storage/db.py::clear_equity_before` 的 SQL 为 `DELETE FROM equity_snapshots WHERE mode='paper' AND ts < ?` ⇒ 即便执行也删不到 live 行。顺带纠正：`scripts/reset_paper_account.py` **是存在的**（7,782 bytes，mtime 2026-09-21 16:15:46），不存在的是 `storage/.reset_paper.flag` ⇒ 该路径不是「已废弃」，是「开关未按」。**残余（P3）**：`db.py:341 mode:str="paper"` 默认值 + `:357 (mode or "paper").lower()` 兜底 + 调用时不传 `mode=` ⇒ **模式标签靠默认值兜底**，现行代码恰好写对，但换调用点就误标 — `engine/event_engine.py:565-568` · `storage/db.py:341/357` — 主张于 data-ledger-auditor-3，**两次证伪于 live-ops-engineer-4 2026-11-05**
- [P2] [INFO] **「我们知道得最少的日子，恰好是从不被检查的日子」——`cnt < 2` 短路让单快照日跳过全部三条判据（设计缺陷成立；但 live 触发路径已被证伪，故不挂资金安全票）**：`strategy/record_ledger.py:338-339 if cnt < 2 or fills: return False` ⇒ 当日只有 1 条快照时 `stale_mark_suspect_day` **直接返回 False，三条判据一条都不跑**。原判「1,000,000 假日 +1,537% 进账本」的触发链**在 live 下不可达**（上条两个保险）⇒ 现实风险降为「未来若有任何新写入路径产出单快照日，当日零校验」。★ 全库唯一单快照日 `2026-09-30`（1 条、15:00:00、975,273.6）恰好是 `source=manual_repair` 的人工行 ⇒ **该洞从未被触发过，纯属运气**。修法：`cnt==1` 不得判可信，应判 `unknown` 并留痕 `eod_basis="single_snapshot"`（`eod_basis` 的第一个日常写入点，承载「判断不了」而非「判断过了」）— `strategy/record_ledger.py:338-339` · `storage/qmt.db equity_snapshots 2026-09-30` — 发现于 复核 2026-11-05（data-ledger-auditor-3）
- [P1] [SAFE] **★ 根治方式不是写注释，而是三条接线断言（AST/字面量级）**：① `_apply_pending_reset` 的调用点必须在 `exec_mode=="paper"` 分支内；② `clear_equity_before` 的 SQL 必须含 `mode='paper'`；③ **`:4613 bt>0 and (time.time()-ts)<=120`** 与 **`:4664 bt<=0 or (time.time()-ts)>120`** 必须共用同一常量（**行号已校正**：`:4611/:4612` 是注释、`:4666-4667` 是计数器自增，判据在 `:4613`/`:4664`；`:2197` 闸门行号无误） —— 这条是**唯一真正护住每日 EOD 的**，谁单独放宽成 300s 或加中位数窗口，它先红。理由：本轮两条被推翻的主张（mock 污染 EOD、reset 落假行）**都是靠「行级巧合」维持的安全**，巧合无测试就会被下一个人删掉 — `engine/event_engine.py:4611/4664` · `storage/db.py::clear_equity_before` — 发现于 复核 2026-11-05（live-ops-engineer-4）
- [P2] [SAFE] **★ 正式票｜三项「未声明的不变量」缺关系断言 —— 现有守卫只覆盖安全的两侧**（tests-only，不动产品代码、不需重启、不动参数；归属 data-ledger-auditor-3 承接）。**为什么不是「再加点测试」**：不变量本体是 `:2197 if risk_asset is not None: self._persist_equity(total_asset)` 的安全性，建立在 `:4612`（`bt>0 and age<=120 ⇒ 券商值`）与 `:4667`（`bt<=0 or age>120 ⇒ None`）**互为补集**之上 —— 这个等价关系**没有任何一行代码声明**，只靠两处字面量恰好相同。**已验证的失效模式**：只把 `:4667` 的 120 改成 300（`:4612` 仍 120）⇒ `age=200` 时 `_total_asset()` 返回本地拼装而 `risk_asset is not None` ⇒ 闸门放行 ⇒ **一条 `mode="live"` 的脏 EOD 落库**；而现有 `tests/test_risk_asset_trust.py` 取点只有 `age=0` 与 `age=300` ⇒ **两端全绿、age=200 完全不可见**。（反向改 `:4612`→300 是安全的：`ra is None` ⇒ 只缺行不写脏行 ⇒ 扫 age 轴可同时给两个方向报警。）**三条断言（按成本 ①→③→②）**：① `clear_equity_before` 作用域契约（新文件；`tests/` 当前对该函数**零覆盖**）：插 paper+live 两行 → 删 → 断言 live 行仍在且值未变、paper 已删；对象是**行为契约**不是 SQL 字面量。③ **补集跨 age 轴**（扩展 `test_risk_asset_trust.py`，别新建）：`broker=100,000` vs 本地拼装 `50,000`（刻意不等），扫 `age∈{0,60,119,119.9,120.1,300,3600}`，断言 `(risk_asset is None) == (total_asset == 50_000)`。**三个返工点**：(a) **必须冻结时间**（两函数各自调 `time.time()`，边界样本会因 ε 分居两侧 ⇒ 偶发红）；(b) **必须显式 `eng=_engine("live")`**（paper 分支恒返回 `_total_asset()` 且永不为 None ⇒ 该断言在 paper 下必假）；(c) **只断言「常量相同」是错的**（合理调参一起改也会红 ⇒ 最后被 `# noqa` 掉）。② live 会话不消费 paper 复位 flag（成本最高，放最后）：插 flag → 以 live 走启动路径 → 断言 `_cash`/`_positions`/快照行数不变且 flag 仍在；**风险**：既有测试全是直构 + 手工塞字段、没走过 `:565`，且 `:559 auto_init_positions` 会真连券商、须先挡；若为省事加 spy 就退回「钉调用点」等于没做 ⇒ 要么先做启动路径可测性，要么明确接受暂不做。**验收 = pytest 全绿 + 把 `:4667` 的 120 改成 300 后断言③必须变红（这条变异测试才是真正验收标准，否则又是一枚装饰）** — `engine/event_engine.py:2197/4612/4667` · `storage/db.py::clear_equity_before` · `tests/test_risk_asset_trust.py:96` — 规格于 live-ops-engineer-4 · 承接于 data-ledger-auditor-3 2026-11-05
- [P0] [FORBIDDEN] **★★★ 行情一断，411.2857 静默消失 —— 不评估、不告警、不留痕（本轮唯一真 P0，与豁免与否无关）**：`event_engine.py:2208 if code not in ticks:` → `:2214` 日线兜底被 `not self._is_manual_exempt(code)` **挡掉** → `:2216 continue` ⇒ **豁免分支 `:2223-2245` 永不进入**，唯一出口 −18% 不执行；`_mark_price_fallback` 第③档会注入 `avg_cost`（`:712-716`）**但只供估值、不供退出评估** ⇒ 面板有价、退出逻辑用「没有」，**看起来一切正常**。**三重静默**：不评估（`continue`）、不告警（该路径零日志）、不留痕（无计数器无 notice）。★ 这给 v17「10-30 下午 5h42m 无止损/无强平/无 T+1/无对账」补上代码级解释：**不只是引擎没跑，就算跑着，无 tick 也不评估任何退出** —— 当时未损失是运气不是设计。★ **唯一在线探测器正是我们每天当假告警忽略的那条**：`行情源失效：35 只标的快照已陈旧 2 分钟`（交易时段触发=真信号，收盘后触发=假告警）⇒ **「加非交易时段守卫」不是降噪，是保住一个 P0 探测器的可信度**；盘中看到它必须当真告警。★ 四分法（逐码裁定）：正常/B(有真 tick 缺 1m) ⇒ **保留占优**；A2(xtdata 断但未落 mock，**无 tick**) ⇒ 移除占优（日线兜底，需 daily 就绪）；A1(落 mock ⇒ **有假 tick** `_mock:True`、300308 BASE=130) ⇒ 豁免分支照常算出 −74% 并调 `_handle_sell`、**被 `:3664-3667` mock 闸门拦掉**，移除后同样被拦 ⇒ **两方皆零，符号不反转（rc-2「A 下移除占优」不成立，rc-4 对）** — `engine/event_engine.py:2208/2214/2216/2223-2245/712-716/3664-3667` · `core/qmt_client.py:57-79` — 发现于 risk-controller-2 v47，**主理人逐码裁定 2026-11-05**
- [P2] [INFO] **510050 的 `peak_price` 一旦被 mock 污染就是永久的（≈100 vs 真实 2.884），切 `scalp` 那刻第一个 tick 就清仓**：510050 **不在 `BASE_PRICES` 里** ⇒ `BASE_PRICES.get(code, 100.0)` 返回默认 **100**；mock 会话下 `_update_position_marks` 会把它的 `peak_price` 抬到 ≈100，而 `_sync_broker_positions:4269 if pos.peak_price <= 0` **只补零、从不降低** ⇒ 污染永久并经 `_save_engine_state` 落库。今日不咬（`exit_mode="trend"` 不读 510050 的 `peak_price`）；**一旦切 `scalp`：`chand = 100 × (1 − 2.9×atr_pct) ≈ 99 ≫ 2.884` ⇒ 止损线在价格上方 ⇒ 立即清仓**。★ 与 300308 的 `stop_price=247.716` **恰好配对**：一个「太低 ⇒ 假保护」（因为没人读），一个「太高 ⇒ 假触发」（一读就爆） — `core/qmt_client.py:60` · `engine/event_engine.py:1994-2021/4269` — 发现于 risk-controller-2 v48 2026-11-05
- [P1] [INFO] **A1/A2 现场判据（明天就用这个分辨「mock」还是「断行情」）**：`300308 ≈ 130` 或 `510050 ≈ 100` ⇒ **A1（mock，有 `[mock 闸门] 拦截 X` error，60s 节流）**；**一行 mock 日志都没有 + ticks 全空** ⇒ **A2（断行情，全静默）**。⇒ **A1 有告警、A2 全静默，而 A2 恰是最需要人决策的那一格 ——「系统不会报警」本身就是 A2 的定义** — `core/qmt_client.py:57-79` · `engine/event_engine.py:2208-2216` — 发现于 risk-controller-2 v48 2026-11-05
- [P1] [SAFE] **★ 自纠（v23 我自己那条「移除豁免多四条」）：应改为「多 3 条 + 1 条兜底 − 1 条超时，并额外少一层 5 分钟窗口」**：① **减一条** —— 超时退出要 `position.open_date` 真值（`trend_strategy.py:252`），而重启后两仓 `open_date=None`（`:3965 return` 早于反序列化）⇒ 移除豁免后**超时反而永不触发**；② **加一条** —— `:2214 if not regime_block and not self._is_manual_exempt(code)` ⇒ 豁免期间无 tick 持仓**连日线兜底都不跑**，移除后 `_daily_fallback_exit` 才跑（`:3818-3823` 调 `_handle_sell` **不带 force**），但它只多一次**委托尝试**、不改善成交价（10-30 已证一次失败尝试可冻整仓）⇒ 是「多一次尝试」不是「多一层保护」；③ **最关键的时间窗** —— strategy 路径的 −18% 在 `:2246-2248 bars<5 continue` **之后**，而豁免路径的 −18% 在 `:2223-2245` **之前** ⇒ 重启后 `_bars` 为空、预热是异步线程（`:1265`，`BAR_WARMUP_BUDGET_SEC=240` vs 实测 ~9.75s/只 × 38 只 ≈ 371s ⇒ 必有标的不在预算内）⇒ **`_bars` 是 1 分钟 bar，攒满 5 根 = 5 分钟，这段时间 strategy 路径的 −18% 根本不被调用，恰好覆盖开盘/重启后的跳空窗口**；④ `force` 不对称**不计入**：`is_in_entry_protection(None)` 在 `:116` 直接 return False ⇒ `:3698` 的 force 闸门对存量两仓恒不触发（对重启后新买的仓才生效，而那种仓当天本就 T+1 锁死） — `strategy/trend_strategy.py:242-256` · `engine/event_engine.py:2214/2246-2248/1265/3818-3823` — 风险审计 v24 2026-11-05（risk-controller-2 指正后我复核）
- [P1] [SAFE] **★ 自纠（v24 那句话再精一格）：不是「5 分钟」，是「5 个分钟桶」；且对两只存量仓位，该窗口只在下载失败时出现**：① `_aggregate_bar` 的 `bucket = tick.ts.replace(second=0, microsecond=0)`（`:3171`），只在**换分钟**时 `dq.append`（`:3175-3184`）⇒ `len(bars)>=5` 要跨 5 个不同分钟桶，**无 tick 的分钟不计** ⇒ 实际 ≈**4–5 分钟**，稀疏 tick / 停牌复牌更长（与无 tick 盲区同源）；② 预热清单长度是**运行时**的：`:1254 codes=_subscribe_codes()`（UNIVERSE + VALIDATION_UNIVERSE(1) + 动态活跃池），静态 `STOCK_CODES` 实测仅 **12** 只，`BAR_WARMUP_BUDGET_SEC=240` vs `:1264` 自述「46 只约 7.5 分钟」⇒ 两种口径都超预算，结论不变；③ 但 `:1166` 超预算只**跳过下载**，本地已有 1m 历史的仍 ready，失败面是「需下载且排在后段」那部分；④ **`:1170-1174` 的 ordered 把持仓排第一**（`held + STOCK_CODES + rest`）⇒ **对 300308/510050，这个窗口几乎只在「下载失败」时出现，而下载失败与 A（xtdata 不可用）同源** ⇒ 丙 在最危险的 mock 场景下窗口更长、保护更少 — `engine/event_engine.py:3171-3184/1166/1170-1174/1254` · `config/settings.py STOCK_CODES=12/BAR_WARMUP_BUDGET_SEC=240` — 风险审计 v25 2026-11-05（risk-controller-2 二轮指正后我复核，结论方向不变）
- [P0] [FORBIDDEN] **★ A 必须拆成 A1(mock) / A2(真无 tick)，否则符号会算反：mock 下 tick 是**有**的（假价）**：`_MockClient.get_ticks` 对**每个请求 code** 都返回（`qmt_client.py:57-79`，带 `_mock: True`，300308 `BASE_PRICES=130.0`）⇒ mock 时 `code in ticks` 为真 ⇒ 豁免分支 `:2223-2245` **会**评估（假价对成本 501.5679 = **−74%**）并调用 `_handle_sell`，然后被 `:3664-3667` mock 闸门挡回 ⇒ **A1 下"移除豁免"零增益**（日线兜底同样终止于 `_handle_sell`）。**符号反转只发生在 A2**：xtdata 盘中挂掉但 `data_mode` 仍是 `xtdata`（未落 mock）⇒ `:2208 if code not in ticks: → `:2214`（`not exempt` 才调日线兜底）→ `:2216 continue` ⇒ **豁免仓的 −18% 连评估都不发生**，移除后才有日线兜底（若 `daily` 就绪）。⇒ 正确四分法：正常(有 tick/预热成功)=保留占优｜B(有 tick/1m 缺或龄>4 天且下载失败)=保留占优（−18% 迟 4–5 分钟）｜**A2(真无 tick)=移除占优**｜**A1(mock)=两方皆零**。另：`_read_fresh_1m` 返回 None 有三条分支（空/全被滤空/`age_days>4`，`settings.py:1043 BAR_WARMUP_MAX_STALE_DAYS=4`）⇒ **长假后首个交易日必然命中**，`download_history` 只救第一条 — `core/qmt_client.py:57-79` · `engine/event_engine.py:2208-2216/2223-2245/3664-3667/1205-1233` — 风险审计 v26 2026-11-05（risk-controller-2 三分法指正后我复核并二分）
- [P1] [FORBIDDEN] **★ A1(mock) 独有的两颗「越界值」地雷，且都被同一个 continue 挡住——引爆需同时改两件事**：① `510050.SH` **不在** `_MockClient.BASE_PRICES`（`qmt_client.py:40-47` 共 16 只，实读 `has 510050 = False`）⇒ 走兜底 `BASE_PRICES.get(code, 100.0)` ⇒ **合成价 ≈100.0 vs 真实 2.884**；`_update_position_marks:2019-2020` 的 `if peak_price <= 0 or p > peak_price: peak_price = p` **只上不下**，`_sync_broker_positions:4272-4273` 只在 `<=0` 时补、**永不回落**，并经 `:4060` 落库 ⇒ **peak_price 从 2.928 被永久污染到 ~100**。② 但**当前读不到**：豁免分支 `:2223-2245` 直接 `continue`，`_trend.on_exit`（唯一消费 `stop_price`/`peak_price` 的地方）与 `_daily_fallback_exit`（`:2214` 被 `not exempt` 挡）**都不执行**。⇒ 与 300308 的 `247.716` 是**同一道门后面的两颗地雷**：一颗太低（虚保护，数据不可用日才咬）、一颗太高（scalp 下 chand≈97 > 市价 2.88 ⇒ 首个 tick 判跌破、无谓清仓；但成本 2.879/市价 2.884 ⇒ 平进平出，伤害是「白卖一次」不是亏损）。**两颗都需「切 scalp」+「移除豁免」两个动作同时发生才引爆** ⇒ 单独改任一个都不咬，但没人会记得第二个条件 — `core/qmt_client.py:40-47` · `engine/event_engine.py:2019-2020/2214/2223-2245/4272-4273/4060` — 风险审计 v27 2026-11-05（risk-controller-2 报地雷，我补双条件）
- [P1] [SAFE] **★ 值守判据（明日可直接用）：A1 有日志、A2 全静默——而必须报警的恰好是 A2**：A1 下 `code in ticks` 为**真**（mock 对每个请求 code 都返回）⇒ `:2208` 的 `code not in ticks` 为假 ⇒ **日线兜底根本不会被调用**（「移除后靠日线兜底不触发」这一格不成立），两条路径都终止于 `:3664-3667` 的 mock 闸门并打 `_warn_mock_blocked`（60s 节流）⇒ **A1 有日志 + 面板价格是假价（300308≈130 / 510050≈100）即可秒判**；**A2（xtdata 断但不落 mock）则 `:2216 continue` 一行都不落** ⇒ 唯一无日志的格，也是唯一「符号反转」的格 ⇒ 只能靠外部判据发现（`engine_state.ts` 停走、`equity_snapshots` 今日 0 行、面板价格冻结但 `data_basis=broker` 正常） — `engine/event_engine.py:2208-2216/3664-3667` — 风险审计 v27 2026-11-05
- [P0] [FORBIDDEN] **★ 更正 line 260（v15 那条）：那句话已过期，且过期方向会让人把正确告警判成误报**。原句「`engine_state` 里 300308 `open_date=2026-11-03T10:59:01`、`/api/snapshot` 是 `null` ⇒ **DB 残留 ≠ 内存，别拿 DB 判 T+1**」——我 11-05 00:0x `mode=ro` 复测：`engine_state` `ts` 仍是 `2026-11-04T15:04:44.779680`，**两只 `open_date` 均为 null**（`peak_price` 仍有值：300308=544.0 / 510050=2.928）⇒ **DB 与内存现在完全一致，"两处不一致"这个现象已不存在**。⇒ 结论必须升级为：**「DB 与内存都不可判 ⇒ 本地 T+1 闸对所有存量仓失效」**，而不是「别用 DB」。精确边界：失效只针对 `open_date=None` 的**存量仓**；当日新买由 `_pos_meta`（`:3549` 写入、存活到下次重启）提供 open_date，**一旦盘中重启，当段新仓也一起失效** ⇒ 失效面=「所有跨过重启的仓」。★ **2026-11-05 00:02:15 那次重启不是始作俑者**：全仓 `_save_engine_state()` 仅三个调用点 `:656`（复位后）/`:3895`（`_persist_equity` 内）/`:4706`（`_shutdown`），pre_open 休眠期一个都没触发 ⇒ `ts` 停在 15:04:44，复测一致。⚠ 两条我**无法复核**、只能引 live-ops-engineer-4 取证：`510050` 丢于 11-03 10:59 重启、`300308` 丢于 11-04 08:08:40 重启——因为 `engine_state` 是**单行覆盖**（`db.py:390 INSERT OR REPLACE` / `:436 WHERE id=1`）**无历史版本**，事后无法反查。★ 另：重启后 300308 移动止损锚 `peak_price` 从 DB 的 544.0 退化为 `last=533.38`（`:4279-4280 peak_price=meta.get(...) or last`，meta 空 dict）⇒ **吊灯线名义下移 1.95%**，但**当前不生效**：`exit_mode="trend"`（`settings.py:337`）+ 豁免分支 `:2223-2245` 直接 `continue` ⇒ `peak_price` 无人读 ⇒ 它属于与 `247.716`、`510050→~100` **同一道门后的第三颗「越界值」地雷**，切 scalp ∧ 移除豁免才引爆 — `storage/qmt.db engine_state` · `engine/event_engine.py:656/3895/4706/3949-3965/4279-4280/2223-2245` · `storage/db.py:390/436` — 风险审计 v28 2026-11-05（live-ops-engineer-4 取证，我复测并补边界）
- [P1] [SAFE] **★ A2 的可执行判据（三方，比"三个都停"多一层分辨）：`tick` 在涨 + `_bars` 冻结 + `engine_state.ts` 冻结**。根因 `:2059-2061 raw = qmt_client.get_ticks(codes); if not raw: return` ⇒ A2 下整轮**提前返回**：`_update_position_marks`/`_aggregate_bar`/`_persist_equity` 全不跑 ⇒ `_bars` 冻结、`last_price` 冻结、账本不写。**但 `_tick_count` 仍在涨**：`:1391 self._tick_count += 1` 在 `:1396 self._run_once(codes)` **之前** ⇒ 这一层把 A2 与"引擎死了"分开：**`tick` 涨 + `_bars` 冻 = 引擎活着但瞎；`tick` 也停 = 引擎死了**。两个观测口都已存在：`observability.py:63 "tick": o._tick_count`、`:449 latest_bars()`。另 A1 下 `_MockClient.get_history`（`qmt_client.py:81-101`）每根 bar `ts=datetime.now()`、经 `:1217 replace(second=0,microsecond=0)` 后**60 根全塌成同一分钟** ⇒ `age_days=0` 过、`len>=60` 过 ⇒ **mock 下预热必然 ready、`:2247 len(bars)<5` 在 A1 永不生效** ⇒ A1 零不对称（两条路径从第一轮起都评估、都打 mock 闸门），且 **ma/atr/趋势破位/单日暴跌在 A1 下全部无意义**（同一时间戳的随机游走） — `engine/event_engine.py:2059-2061/1391/1396/2247` · `core/qmt_client.py:81-101` · `engine/observability.py:63/449` — 风险审计 v29 2026-11-05（risk-controller-2 补判据，我校正 tick 一层）
- [P1] [FORBIDDEN] **★ 510050 是观察篮**唯一**品种（实读 `manual_entry_codes=['510050.SH']`、`manual_entry_amount=1500`），清掉它是系统性损害不是零成本**：其语义见 `settings.py:558-562`「绕过动量/日线/评分闸门建仓，用于把尚未纳入策略的候选真正拿在手里观察」+ `:2526-2532`「买入 LLM 排序前五，有持仓才能更好评估」⇒ **卖掉 510050 = 断掉闭环的度量对象**，价格平进平出 ≠ 零成本。⇒ 上一版把 510050 地雷写成"伤害是白卖一次"要补这句 — `config/settings.py:558-562` · `STRATEGY_PARAMS.manual_entry_codes`（运行时实读） — 风险审计 v29 2026-11-05（RC-2 提出，我实读证实）
- [P0] [SAFE] **★ 放开 510050 退出豁免：改动已备好并逐项验证，但 `config/settings.py` 有文件级写保护，Agent 落不了盘 ⇒ 需你一条命令**。现象：`Get-Acl` 显示 `DESKTOP-FFCD4C2\lisinan` 对它有 **FullControl**，`whoami` 也确实是 `lisinan` 且**不在** `CodexSandboxUsers` 组，但 `open('wb')` / `os.replace()` / `[IO.File]::WriteAllText` 在**提权后**仍全部 `WinError 5 / Access denied`；同一目录的 `tests/`、`reports/`、`.bak` 新建全部成功 ⇒ **只有这一个文件被单独保护**（生产配置保护，合理）。⇒ 命令：`cp config/settings.py.proposed-20261105 config/settings.py`，然后**由你重启引擎**（settings 只在 import 时执行一次，全仓 0 处 `importlib.reload`；重启属你，我不代劳 —— 契约 §一.4）。改动 4 处：`manual_entry_codes: ['510050.SH']→[]` / `manual_entry_amount: 1500.0→0.0` / `exit_exempt_codes: ['300308.SZ','510050.SH']→['300308.SZ']` / `tests/test_evolve_baseline_sync.py` pin `((), ('510050.SH',))→((), ())`。**为什么必须连买入清单一起清**：510050 若被 −6% 日亏强平卖出，观察篮**次日**自动补买（`_manual_entry_step:2588` 的 `_manual_sold_today` 只管当日），补买成功后 `:2604 _manual_positions.add(code)` 重新登记 ⇒ `_is_manual_exempt` 判据 ① 复活 ⇒ **豁免自动恢复，放开被静默撤销** — `config/settings.py:613/626/723` · `engine/event_engine.py:2501-2524/2563/2588/2604` — DAILY-REVIEW 2026-11-05 08:1x
- [P0] [INFO] **★ 放开 510050 的真实效果 = 平时几乎不变，别误判为「改动没生效」**：实盘重算 `avg_cost=2.879` / 11-04 收盘 `2.884` ⇒ pnl **+0.17%** > `hard_stop_pct(-18%)` ⇒ 硬止损不触发；`510050.SH` **不在** `STOCK_CODES`（12 只）**也不在** `dynamic_universe.active_codes`（实读 `n=0`）⇒ `_daily_codes()` 无此票 ⇒ `trend_broken` 恒 False、`_day_change_pct` 取不到；`open_date=None`（跨重启丢失）⇒ 超时分支跳过 ⇒ **`exit_mode='trend'`（`settings.py:337`）下四条退出全部不命中 ⇒ 510050 平时仍不会被卖**。唯一真实新增出口是 `_split_flatten_candidates:2482` 重新把它纳入 `daily_stop_flatten_pct=-6%` 日亏强平候选（仓位 1,442 元 / 权益 **2.17%**）⇒ **改动的语义价值 = 「−6% 强平时它会被卖掉」，不是「它明天就被清仓」** — `strategy/trend_strategy.py:236-268` · `strategy/daily_context.py:542-564` · `engine/event_engine.py:685-722/2208-2248` — DAILY-REVIEW 2026-11-05 08:1x
- [P1] [FORBIDDEN] **★ 本次改动与 v29 那条 `[FORBIDDEN]`（510050 是观察篮唯一品种、清掉=断掉度量对象）的关系，必须由你裁决**：v29 反对的是**卖掉 510050**，本次不主动卖它；但清空 `manual_entry_codes` 会让「被卖出后不再补买」⇒ **提高了触发 v29 那条的概率**。若你认为「观察篮的度量价值 > 510050 的自主退出」，则应**只清 `exit_exempt_codes`、保留篮子**——代价是：一旦 −6% 强平卖出，次日补买并重新获得豁免（放开被自反撤销）。两条路都可切换，改口只需回滚 `manual_entry_codes`/`manual_entry_amount` 两行 — `config/settings.py:558-562` · `engine/event_engine.py:2588/2604` — DAILY-REVIEW 2026-11-05 08:1x（沿用 v29 RC-2 的实读结论）
- [P1] [SAFE] **★ 验证证据（可复跑）**：① `.proposed` 经 `py_compile` + `ast.parse` 双过；② 用 `SourceFileLoader` 真加载后三值断言通过（`[]` / `0.0` / `['300308.SZ']`；`manual_entry_exit_exempt` 仍 `True`、`exit_mode` 仍 `trend`、`hard_stop_pct` 仍 `-0.18`）；③ **语义断言**：`EventEngine.__new__` + `_manual_positions=set()` 下，现行 `{300308:True, 510050:True}` → 新配置 `{300308:True, 510050:False}`，总开关置 False 后三者全 False；④ 豁免相关 5 个测试文件：**现状 1 failed / 24 passed**（唯一红的是 `test_known_divergences_are_pinned`，报「生产侧已从 () 变为 ['510050.SH']」—— 正是 pin 与未落盘配置的**配对中间态**，自解释）；进程内 patch 成新值后 **25 passed**，证明执行 `cp` 后即转全绿；⑤ 订阅不受影响：`_subscribe_codes:707` 走 `UNIVERSE + VALIDATION_UNIVERSE`（510050 在后者）+ `:720-721` 持仓兜底 ⇒ **不会掉 tick** — `config/settings.py.proposed-20261105` · `tests/test_evolve_baseline_sync.py:133` — DAILY-REVIEW 2026-11-05 08:1x
- [P0] [INFO] **★ 撤回（覆盖上方 08:1x 那四条）：OWNER 09:00 裁决「510050 是观察舱，一直保留；300308 也保持不动」⇒ 改动整体作废，配置与测试已恢复原状，无任何待办**。采纳的是 v29 那条 `[P1] [FORBIDDEN]`（观察篮 = 「有持仓才能评估」的唯一度量对象，清掉它是系统性损害）。实际落地核验：① `config/settings.py` **从未被修改** —— `git status` 无 `M` 项、仍是 **86,570 bytes / Nov 3 23:18**（写保护拦截，本就不该绕）；② `tests/test_evolve_baseline_sync.py:141` pin 已回滚为 `((), ("510050.SH",))`，`git diff` 只剩一行说明性注释、**值行零差异**；③ 豁免相关 5 个测试文件 **25 passed**；④ 临时产物 `settings.py.proposed-20261105` 与 `settings.py.bak-20261105-0800` 已删除。⇒ **不需要你执行任何命令，也不需要重启。** 保留的只有三条技术事实（已进 `MEMORY.md`）：`config/settings.py` 对 Agent 只读；`exit_exempt_codes` 若单独清会被「卖出→次日补买→`:2604` 重新登记」自反恢复；`exit_mode='trend'` 下 300308 已趋势破位（close 533.38 vs MA60 806.03 = −33.8%），这正是它**不能**放开豁免的原因 — `config/settings.py`（未改）· `tests/test_evolve_baseline_sync.py:141` — DAILY-REVIEW 2026-11-05 09:0x

### 2026-11-05 15:40 DAILY-REVIEW（每日复盘 · 七块全跑，当日新增 4 条）

- [P0] [FORBIDDEN] **★★★ 日亏熔断的「开盘资产」分母不含隔夜跳空 —— 今天是「−6.03% 的一天」，引擎报 +0.87%，两次越过 −6% 线全程未动**：`risk/manager.py:141-143` 的注释写「开盘资产口径（含隔夜重估）」，但 `_day_open_asset` 由 `engine/event_engine.py:1990-1991` 在**当日第一次拿到估值时**赋值，即跳空之后（今日 09:30:46）。实测：前收 66,411.12 → `day_open_asset` **61,870.10**（已含 −6.84% 跳空）→ 收 62,409.60；引擎 `daily_pnl = +539.50（+0.87%）`，账本跨日 **−4,001.52（−6.03%）**。−6% 真线 62,426.45 vs 引擎线 58,157.89，**相差 4,268.56 元**；今日收盘低于真线 16.85 元，**244 个快照里 157 个（64%）低于真线**。唯一兜底是峰值回撤 −7.4778%（占 −20% 预算 37.39%）。⇒ 名义两道日级防线（−3% 已被 `max(5000,3%)` 吃成 7.53%、−6% 强平）**都只管「开盘之后」**，隔夜跳空无任何日级约束。改分母＝改风控语义，需 OWNER 裁口径（① 用账本 `prev_asset`；② 另加一条日环比线；③ 明确接受只防日内并在文档写清） — `risk/manager.py:141-143` · `engine/event_engine.py:1990-1991` · `storage/qmt.db equity_snapshots` — 发现于 DAILY-REVIEW 2026-11-05
- [P2] [INFO] **★ 510050.SH 日线末根停在 20261103，落后 2 个交易日 —— 不是「写入延迟」而是结构性断更**：11-04 把它判为「15:40 当日日线未生成」，今日不成立（其余 37 只已到 20261105，唯独它停在 11-03，缺 11-04+11-05）。根因：`_daily_codes()` = `STOCK_CODES + INDEX_CODES + dynamic.active_codes`，**不含 `VALIDATION_UNIVERSE`**，而 510050 只存在于后者（其注释明确「验证标的只需要 tick」）⇒ 引擎从不请求其日线。影响面：当前 510050 在 `exit_exempt_codes` 内、退出不走日线 ⇒ **今日零后果**；但 `_mark_price_fallback` 第②档会拿 11-03 的 2.884 当现价，且若日后放开豁免，`_day_change_pct`/趋势破位对它**恒取不到值** —— 与 08:1x 那条「放开后 trend 四条退出全不命中」互相印证 — `engine/event_engine.py:671-691` · `config/settings.py VALIDATION_UNIVERSE` — 发现于 DAILY-REVIEW 2026-11-05
- [P2] [SAFE] **★ 已修｜复盘报告新增「跨日真实收益」，从此不再只显示日内口径**：`_account_daily()` 硬编码 `mode='paper'`（live 恒返回 `{}`）⇒ `account_ret_pct=null` ⇒ 报告头版只剩 `equity.daily_return_pct = +0.87%`，**把 −6.03% 的一天显示成赚钱的一天**（差 6.90pp）。新增 `strategy/review_daily.py:342 _day_chain()`（前快照日末→目标日末，与账本同源）+ `:1742` 调用 + `:1874` JSON + `:1423` HTML 两张卡 + `:1896` 控制台行；**纯新增字段，`equity`/`account_*` 语义一字未动**；前值不在 ≥14:30 时打 `⚠`（防 10-30「盘中值当收盘」假亏损复发）。实跑 `-6.03% (¥-4,001.52：66,411.12 → 62,409.60)` 与账本**逐位一致**。守卫 `tests/test_review_day_chain.py` 7 例（含反向对照：改成「取当天首条快照」立刻变红）+ `run_all` 注册；**833/833 PASS** — `strategy/review_daily.py:342/1742/1874` · `tests/test_review_day_chain.py` — 修复于 DAILY-REVIEW 2026-11-05
- [P2] [FORBIDDEN] **【复现，非新条目】`benchmark` 三分量连续第 2 天同时失效**：11-05 行 `universe_bh_pct=-0.1898133579297904 / index_pct=-0.25153481055939075` 与 11-04 **逐位相同**（今日未刷新）；`account_pct=-0.9375904` = 62,409.60/1,000,000−1 ⇒ **分母仍是 paper `INITIAL_CASH`**（真实应为 62,409.60/66,542.31−1 = **−6.21%**）；`as_of` 仍标 `2026-11-04`。⇒ `excess_pct=-74.78%` 是「今天的账户减昨天的基准」，KPI#4 全链无有效数字。承接 11-04 那条，**仍未修**（需改 `engine/benchmark_feed.py` + 重启） — `logs/benchmark_daily.jsonl` 末 3 行 · `/api/snapshot.benchmark` — 发现于 DAILY-REVIEW 2026-11-05

### 2026-11-06 15:40 DAILY-REVIEW（每日复盘 · 七块全跑，当日新增 5 条 + 复现 4 条）

- [P0] [FORBIDDEN] **★★★ live 下熔断首次真实触发（−9.32%），但强平对 100% 仓位无效 —— 全天零处置，是反弹救了账户而不是风控**：`09:30:23` 开盘 10 秒内 `daily_stop -9.32%` 触发，`risk_state={"halted":true,"halt_reason":"daily_stop -9.32%","halt_day":"2026-11-06"}`；两只持仓 **全部** 在 `exit_exempt_codes=['300308.SZ','510050.SH']` 内 ⇒ 强平候选集为空 ⇒ `fills=0 / orders=0 / daily_trade_count=0`。账户从开盘 −9.32% 裸奔到收盘 −7.29%（盘中低 55,870.57 / 高 59,265.57）。**唯一残余防线是 300308 的 −18% 单票止损（411.286，现价 448.00，仅 8.93% 空间），触发时账户 =54,191.17 = 相对峰值 −18.56%，距 −20% 断路器（53,233.85）仅 957.32 元 / 1.44pp** ⇒ 两道线数值上几乎重合，中间无第二道防线 — `storage/qmt.db engine_state.risk_state` · `config/settings.py exit_exempt_codes` · `logs/quant_system.log` 09:30:23 — 发现于 DAILY-REVIEW 2026-11-06
- [P0] [FORBIDDEN] **★★★ 熔断分母 `day_open_asset` 是竞态：同一份代码、同一进程（未重启），连续两天给出相反的语义**：11-05 基线 `61,870.10` = 09:30:46 首估值（**跳空之后**）⇒ 跳空不算、引擎报 +0.87% 而真值 −6.03%、−6% 线全程未触发；11-06 基线 `62,409.60` = 11-05 **收盘**（**跳空之前**）⇒ 跳空算进去、引擎报 −7.29% 与账本**逐位一致**、开盘第 10 秒即触发。赋值点 `event_engine.py:1990-1991 _day_open_baseline()` 取的是「行情首次不陈旧那一轮的 broker 总资产」，落点全凭时序 ⇒ **过去 6 个 live 交易日的 halt 记录不能作为「当天未跌破 −6%」的证据**。建议裁决：基线显式取上一交易日末快照（与账本 `prev_asset` 同源），消除时序依赖 — `engine/event_engine.py:1990-1991` · `risk/manager.py:141-143` · `storage/qmt.db equity_snapshots` — 发现于 DAILY-REVIEW 2026-11-06（承接 11-05 那条，性质从「时点选错」升级为「不可复现竞态」）
- [P1] [FORBIDDEN] **熔断日志刷屏 4,794 条，且措辞与事实相反**：`日内硬止损强平：全部持仓已清空或重试次数耗尽` 全天出现 **4,794 次**（约每轮一次），而此时 2 只持仓完好无损。⇒ ① 日志量级噪声（占 `quant_system.log` 当日绝对多数）② 人读这句会得出「账户已被清空」的错误结论。修法：区分「0 只可卖（全豁免）」与「已清空」两种终态，并对重复态加节流 — `logs/quant_system.log` 09:30–15:00 · 强平分支 — 发现于 DAILY-REVIEW 2026-11-06
- [P1] [FORBIDDEN] **单票集中度 77.42% 远超 `max_single_position_pct=0.40`，但该上限只约束新开仓**：300308 市值 44,800 / 权益 57,862.57 = **77.42%**（510050 2.51%、现金 20.07%）。该值由 11-04 的 80.31% 下跌被动降至 77.42%，**不是因为减仓** ⇒ `-18%` 止损一旦成交，单票风险直接等于账户风险（见 P0-1 的 957 元缓冲） — `/api/snapshot.positions` · `config/settings.py RISK_PARAMS` — 发现于 DAILY-REVIEW 2026-11-06
- [P2] [SAFE] **已修｜日线新鲜度巡检的检查池漏掉 `VALIDATION_UNIVERSE`，导致 510050 结构性断更连续 4 天不可见**：`scripts/_hygiene_bar_dates2.py` 原检查池 = 静态 12 + 动态 30 + 指数 4 = 37 只，**不含 510050**，因此断更对它完全隐形。修复：池子补入 `VALIDATION_UNIVERSE` + 新增显式「落后」汇总行；修后实测输出 `[20261103] 1 只: 510050.SH` 与 `[落后] 相对最近交易日 20261106：20261103 → 1 只（510050.SH）`。纯只读脚本、不进程、不需重启、不动参数，回滚=删该项。`run_all` **833/833 PASS** — `scripts/_hygiene_bar_dates2.py` — 修复于 DAILY-REVIEW 2026-11-06
- [P2] [INFO] **★ 新陷阱：批量取数默认 `fill_data=True` 会给缺失标的前值填充「假 bar」，且 `dropna()` 与 `index[-1]` 都拦不住**：今日首跑 `get_market_data_ex(fields,[38只],count=8)` 得到「38/38 全部 20261106」的**假绿**，实际 510050 被补出 11-04/05/06 三根、收盘**全是 2.884**（真实 11-06 收盘 **2.902**）；改用单码 `count=-1` 全历史才看到真末根 **20261103**（落后 3 个交易日）。⇒ 任何「末根最新」的结论必须用单码全历史二次确认，批量+长度/NaN 判据都可能是假绿；既有脚本带 `fill_data=False` 是对的，别删 — `xtdata.get_market_data_ex` · `scripts/_hygiene_bar_dates2.py:40-43` — 发现于 DAILY-REVIEW 2026-11-06
- [P2] [FORBIDDEN] **【复现第 3 天】`benchmark` 三分量仍未刷新，且今天是「第二种外观」——`account_pct` 变了但两个基准分量逐位没变**：11-06 行 `universe_bh_pct=-0.19113155538045842` 与 `index_pct=-0.24504261998258203` 同 11-05 **逐位相同**，`account_pct=-0.94213743` 仍是 paper `INITIAL_CASH=1e6` 分母（真实应 −13.04%），`as_of` 仍标 11-05。★ 别用「有字段变了」反证「基准刷新了」——本次变的只有账户侧。`excess_pct=-0.751` = 今天的账户 − 昨天的基准，KPI#4 超额项仍无有效数字 — `logs/benchmark_daily.jsonl` 末 3 行 · `/api/snapshot.benchmark` — 发现于 DAILY-REVIEW 2026-11-06（承接 11-04/11-05）
- [P2] [FORBIDDEN] **【复现】510050.SH 日线停在 20261103，落后 3 个交易日，结构性断更确认**：根因仍是 `_daily_codes()` 不含 `VALIDATION_UNIVERSE`。今日零后果（豁免仓退出不走日线），但 `_mark_price_fallback` 第②档会拿 2.884 当日线兜底价，而真收盘 2.902（差 0.62%） — `engine/event_engine.py:671-691` — 发现于 DAILY-REVIEW 2026-11-06
- [P2] [INFO] **【复现】研究面板末日 2026-10-28，落后 7 个交易日；moneyflow 缓存 2026-10-28、northbound 2026-09-19（≈7 周，snapshot `end_date=20260825 / guard_reason=stale`）** ⇒ 面板与本周边界（含 11-04 +8.74%、11-05/06 两连跌）完全不重叠 — `storage/hist_cache/` · `data/cache/` — 发现于 DAILY-REVIEW 2026-11-06
- [P1] [INFO] **E 块口径提醒（次日直接可用）：`min_mom` 放到 0.00 也只有 1 只（603893 +4.77%），而它不在 `trend_up`（仅 2 只：002415 −5.9% / 603501 −3.4%）里 ⇒ 进场数仍为 0** ⇒ 双闸同闭，弱市绑定约束是 `trend_up` 不是动量闸门，**不要再提放宽 `min_mom`** — `reports/why_no_entry/DAILY_20261106.md` · `scripts/_why_no_entry_momentum.py` — 发现于 DAILY-REVIEW 2026-11-06

### 2026-11-08 周度 SYNTHESIZE 发现追加
- [P1] [INFO] **SYNTHESIZE 2026-11-08：全量复验受阻**——miniQMT 周日未登录 ⇒ `get_history` 降级 mock（750 根全戳 2026-11-08），`align_panel` 塌成 1 交易日，`run_backtest` 报 `data too short`。回测/WF 全链路本周不可跑，回测 KPI 沿用 2026-11-01 陈旧值（样本末日 20261030）。行情源恢复后须重跑 `strategy/_verify_live_quality.py`。
- [P0] [INFO] **live 本周 −9.14%、累计 −13.04%、峰值回撤 −14.22%**：80% 集中 300308（豁免 downtrend，较 MA60 −33.8%），11-04/05/06 连续零成交零信号——D10「下降市不参与」的 live 实证；回撤走向由 OWNER 对 300308 豁免裁决决定。
- [P0] [FORBIDDEN] **放开 510050 退出豁免**滞留 3 天（11-05→11-08）：`config/settings.py` 文件级写保护，Agent 落不了盘，需 OWNER 一条 `cp` + 重启（改动已备好并验证）。
- [P2] [INFO] **北向缓存 stale 75 天（末端 2026-08-25）+ 研究面板 stale 7 周（末端 2026-10-28）**：09-19 落盘的 `northbound_mode="gate"` 跑在陈旧数据上；回测样本不含本周弱市段（含 11-04 +8.74%、11-05/06 连跌）。
- [P2] [INFO] **归集后首整周循环质量**：DAILY-REVIEW 应跑 4（11-03/04/05/06，11-02 槽位早于任务建档不计漏跑）/ 实跑 4 / 停摆 0；无自我停机/未运行；INBOX 信息密度未降，归集未丢信息。

### 2026-11-08 双功能区优化（OWNER 授权「区域二一定不要影响实盘」）

- [P0] [INFO] **★ 明天（11-09）开盘 live 仍会零成交 —— 四个字段同时错，单独看每个都像小问题**：
  `scripts/zone_preflight.py` 实跑照出 `data_mode=mock`（`mock_trade_block_reason()` 首行
  `if data_mode != "mock": return None` ⇒ **mock 下所有买卖被拦**）、`broker_mode=disconnected`
  （无法下单）、`halted=true`（`daily_stop -9.32%`）、`asset_trusted=false`（面板 100 万不是券商真值）。
  四个里任意一个都足以让成交量为零，`halted` 即便明天按 `halt_recover_days` 自动翻转，前两个仍在。
  ⇒ **要恢复 live 交易必须重启 live（OWNER 动作）**：miniQMT 登录 → 重启 → `preflight` 复验。
  Agent 未动 live 任何配置、未重启 —— 依 OWNER「区域一不要影响」。
  — 证据：`python scripts/zone_preflight.py`（PASS 19 / WARN 1 / **FAIL 4**，四条全在实盘区）
  — 发现于 双功能区优化 2026-11-08 — `[待 OWNER]`

- [P0] [SAFE] **[已修 b4f21d4] paper 确实在写 live 的文件 —— 候选池已被覆写（毫秒级实证）**：
  `storage/dynamic_universe.json` mtime `2026-11-08 20:09:43.394506` 与 paper 日志
  `20:09:43,394 DynamicUniverse 刷新完成` **毫秒级吻合** ⇒ paper 覆写了 live 的候选池。
  `active_pool` 决定 live 评估/买入哪些票，而 paper 走 tushare、live 走 xtdata，两边池子本可不同；
  一次 paper 刷新失败写半截 JSON，live 重启就读到空池/残池，**且日志不报任何错**。
  根因：DB 分库只解决了 `engine_state`，缓存文件仍硬编码 `BASE_DIR/"storage"`。
  已改 5 处走 `core/zone.py::zone_storage_dir()/zone_data_cache_dir()`，
  **live 分支逐位等于改造前字面量**（`tests/test_zone_shared_paths.py` 7 例钉死）。
  ⚠ 最隐蔽的是 `northbound_hsgt.json`（**单文件**，两进程同写会读到半截 JSON）。
  — 证据：`docs/ZONE.md::七` · `data/{dynamic_universe,hist_cache,earnrev_cache,moneyflow_cache,northbound_cache}.py`
  — 发现于 双功能区优化 2026-11-08 — `[已修 b4f21d4]`

- [P1] [INFO] **修完越界后的功能回归：paper 候选池塌成 0 只，信号层直接空转（面板却显示「运行中」）**：
  隔离后 paper 不再蹭 live 的池子，首启 `Tushare 返回空` ⇒ `动态候选池: 0 只活跃`。
  已做一次性迁移 `storage/dynamic_universe.json → storage_paper/dynamic_universe.json`
  （读 live、写 paper，live 文件 mtime 未变），实测恢复 `375 只 / 活跃 30`、漏斗 `37→3→0→0→8`。
  `refresh()` 失败时**不清空已有池**（只在成功时赋值），所以一次有效缓存即可顶住。
  ⇒ **若日后有人删掉 `storage_paper/dynamic_universe.json`，paper 会静默 0 只空转**，
  `preflight` 的「候选池」检查就是为这个场景加的。
  — 证据：`logs_paper/quant_system.log`（`动态候选池: 0 只活跃` → 迁移后 `30 只活跃`）
  — 发现于 双功能区优化 2026-11-08 — `[已处置]`

- [P1] [INFO] **paper 需要 OWNER 手动持久化启动（会话级后台任务会被回收）**：
  当前 paper 由本会话的后台任务拉起（pid 28968 @5001）。会话结束后可能被回收 ⇒ 明天开盘
  paper 不在。**明早 09:00-09:25 双击 `scripts\run_paper.bat`** 即可（自带 `QMT_ZONE=paper`、
  独立 pid 文件；若已有实例在跑，新实例会被单例锁挡下退出，不会双开）。
  ⚠ **绝不能给 paper 配 guardian**：`health_check.py:288` 只探 5000、只拉无参 `main.py --web`
  ⇒ 会把 paper 拉成**第二个 live 引擎**。
  — 证据：`scripts/run_paper.bat` · `docs/ZONE.md::三`
  — 发现于 双功能区优化 2026-11-08 — `[待 OWNER]`

- [P2] [INFO] **paper 重启后首个 `/api/snapshot` 耗时 13.6s（冷启动一次性），热身后 0.01s**：
  冷启动要网络拉 37 只日线。看板 `SNAP_TIMEOUT=3s` ⇒ 重启后约 15 秒内 panel 会短暂显示
  「区域离线」，属显示误判不是引擎故障。开盘前重启 paper 时请知悉这一点。
  — 证据：`/api/snapshot` 连测（13.59s → 0.01/0.00/0.02/0.00s）
  — 发现于 双功能区优化 2026-11-08 — `[跟踪中]`

- [P2] [INFO] **live 在本轮优化中零改动（自证）**：未重启、未改 `config/settings.py`、
  未切 `broker_qmt_mode`；`storage/qmt.db` 与 `storage/dynamic_universe.json` 的 mtime
  全程未变（后者停在 20:09:43.394506），live pid 18256 与 5000 端口持续在线。
  唯一对 live 的读取是 `mode=ro` 只读查询（看板与 preflight）。
  — 证据：`stat` 前后对照 + `zone_preflight` 的「越界检查」行（live 池末次写入早于 paper 启动）
  — 发现于 双功能区优化 2026-11-08 — `[已核实]`

### 2026-11-08 接口能力预验证 + 白名单审计（OWNER 指令「原有白名单股票数据不要出错」）

- [P0] [INFO] **★ live 引擎内存里持仓是 0 只，但账本里有 2 只 —— 现有仓位不受任何退出逻辑管理**：
  `/api/snapshot.positions = {}`，而 `engine_state.positions` 有 `300308.SZ` + `510050.SH`。
  成因是 live 不恢复本地账本（`restore_ledger=False`），持仓只能靠 `_sync_broker_positions`
  从券商回灌；而当前 `broker_mode=disconnected` ⇒ 回灌从未发生 ⇒ **−18% 硬止损、趋势破位、
  日亏强平这些退出逻辑，对这 2 只真实仓位全部不生效**（它们不是「被豁免」，是「不存在」）。
  一旦券商连上会自动回灌，所以这不是账本损坏，而是**断链期间的管理真空**。
  同日另一条：引擎自报 `total_asset=1,000,000`（=INITIAL_CASH）vs 账本末值 `57,862.57`
  ⇒ 偏离 **+1628.23%**，`asset_trusted=false`。
  ⇒ 恢复顺序必须是：miniQMT 登录 → 重启 live → 券商回灌持仓 → 再做任何数据判断。
  — 证据：`scripts/iface_probe.py` 的「D 数据准确性」段（引擎持仓 vs 账本 / 引擎 vs 账本）
  — 发现于 接口预验证 2026-11-08 — `[待 OWNER]`

- [P0] [SAFE] **[已修] 白名单加了「值级别」钉子，此前只有行为测试 —— 名单被改短时测试全绿**：
  审计结论：**白名单本身未被改动**。`config/settings.py` git diff 为空；
  `STRATEGY_PARAMS.exit_exempt_codes = ["300308.SZ","510050.SH"]`、
  `manual_entry_codes = ["510050.SH"]` 与基线逐项一致；`UNIVERSE 16 / STOCK_CODES 12 /
  VALIDATION_UNIVERSE 1` 未变；live 内存池（09:40 加载）与磁盘文件**逐位一致**（30 只、顺序相同）。
  但仓库里所有白名单测试钉的都是**行为**（声明了就豁免 / 豁免仓不被硬止损打掉），
  没有一份钉「到底是哪几只票」—— 名单被改短时行为不变、测试全绿，而实盘那只票会在下次强平里被卖。
  已新增 `tests/test_whitelist_pin.py`（11 例）逐项钉死，并加「两条名单不得合并」断言
  （合并会产生「豁免→被强平→次日自动补买→判据复活」的自反恢复）。
  — 证据：`tests/test_whitelist_pin.py` · `tests/run_all.py` 注册项
  — 发现于 接口预验证 2026-11-08 — `[已修]`

- [P1] [INFO] **接口能力实测（2026-11-08 20:41，休市）：PASS 21 / WARN 4 / FAIL 3 / SKIP 1**：
  · 行情 FAIL：`xtdata.get_full_tick` → `not authenticated`；白名单两只日线末根均不可读
  · 交易 FAIL：引擎自报 `broker_mode=disconnected connected=False 重连=0`
  · 交易 PASS：`XtQuantTrader` / `order_stock` / `cancel_order_stock` / `query_stock_asset` /
    `query_stock_positions` / `query_stock_orders` 六个 API 面全部存在；`broker_qmt_mode=XtMiniQmt`；
    `accounts` 2 个；`tushare_token` 已配置
  · 文件桥 PASS：`C:/pazq_qmt_bridge` 7 个条目（signal/result/processed_ids/heartbeat/boot.log）；
    `QMTIDE_BRIDGE.py` 已部署 43,443 字节（mtime 11-08 02:38）
  ⇒ 结论：**代码与配置都就绪，唯独「认证 + 券商连接」两件事没到位**，两者都需要 OWNER 侧动作。
  — 证据：`scripts/iface_probe.py`（新增，纯只读）— 发现于 接口预验证 2026-11-08 — `[跟踪中]`

- [P1] [INFO] **已排 2026-11-09 09:35 一次性自动化做同样验证**（`df5eb9ad`）：
  跑 `zone_preflight.py` + `iface_probe.py`，逐条判定七个关键字段并追加本文件。
  若届时有 FAIL，说明 live 当天不可能正常交易，需 OWNER 在开盘前处理。
  — 发现于 接口预验证 2026-11-08 — `[跟踪中]`

- [P2] [INFO] **白名单查找陷阱：`exit_exempt_codes` 不是模块常量，而是 `STRATEGY_PARAMS` 的键**：
  `getattr(config.settings, "exit_exempt_codes", [])` 会**静默返回空**（我第一版预验证脚本
  就这么写错了一次，把两条真实豁免报成了「不在白名单」）。正确读法是
  `settings.STRATEGY_PARAMS.get("exit_exempt_codes")`（`settings.py:613/723`）。
  — 证据：`config/settings.py:613,723` — 发现于 接口预验证 2026-11-08 — `[已修]`

- [P2] [INFO] **候选池文件与 git 快照差 4 只票，属正常换手、不是本次改动引入**：
  当前 375/30 vs HEAD(2026-10-21) 374/30，新增 `000977/301165/603893/688036`、
  移除 `002281/300857/688498/688702`。判据：**live 内存池（09:40 从文件加载）与当前磁盘文件
  逐位一致**（30 只、顺序也相同）⇒ paper 20:09 那次写没有改变内容；这 4 只是 10-21 以来
  live 自己每日刷新造成的正常换手。
  — 证据：`/api/snapshot.dynamic_universe.active_pool` 与 `storage/dynamic_universe.json` 对比
  — 发现于 接口预验证 2026-11-08 — `[已核实]`

---

## 2026-11-08 夜 · 明早（11-09）执行清单 + 两笔已修

### ★ 明早开盘前，请按此顺序做（前四条必须做，否则 live 当日零成交）
1. **登录 miniQMT**（现在 `xtdata.get_full_tick` 报 `not authenticated`，行情/日线全不可读）。
2. **重启 live 引擎**（`data_mode=mock` / `broker=disconnected` / `halted=true` 三项只有重启才会变）。
   ⚠ **今天停 live 只能用硬杀，不能用 `--stop`**，原因见下面 P0 第一条：
   `taskkill /F /PID 18256`（先 `tasklist /FI "PID eq 18256"` 确认）。
   重启后新代码生效，之后才可以用优雅停止。
3. **09:00–09:25 双击 `scripts\run_paper.bat`**（paper 现在是会话级后台任务，会话回收就没了）。
4. **09:35 前后看一眼收件箱**：一次性自动化 `df5eb9ad` 会自动跑 `zone_preflight.py` + `iface_probe.py`，
   逐条判定 data_mode / broker / halted / asset_trusted / 候选池 / 白名单 / 持仓对账。
5. 09:30 后打开 <http://127.0.0.1:5002> 看双区看板（live 左 / paper 右，同屏对照）。

---

- [P0] [SAFE] **live 断链期「优雅停止」会把真实账本覆盖成构造初值 —— 已修，但今日进程还没加载新代码**：
  live 走 `restore_ledger=False`，内存里只有 `cash=1,000,000.0` / `positions={}`
  （2026-11-08 20:47 `/api/snapshot` 实测），而 `storage/qmt.db::engine_state` 是 11-06 收盘的
  真实账本：`cash=11,611.57` / 2 只持仓 / `day_open_asset=62,409.6` / `peak_asset=67,453.62`。
  `_shutdown()`（`:4729`）里 `_save_engine_state()` 是**无条件**调用，而 `main.py --stop`
  正是写哨兵 → 主循环退出 → `_shutdown()`（`:1421`）⇒ **一次优雅停止 = day_open_asset 变 None、
  持仓清零、cash 变 100 万**，次日日内跌幅基准全错、熔断口径失真。
  已加闸门（`abb13e7`）：`exec_mode=="live"` 且 `_broker_total_asset<=0`（整个进程从未拿到
  券商总资产）时不落盘；券商连上后照常落盘；paper 不受影响。
  **验证**：`tests/test_live_state_save_guard.py` 5 例（真 sqlite，不是打桩）；
  **正向对照已做** —— 去掉闸门后断言必红（实测 `cash 11,611.57 → 1,000,000.0`）；
  全量回归 **984 PASS / 33 FAIL / 1017 TOTAL**，失败集合与基线逐模块一致（xtdata 未认证级联）。
  ⚠ **生效条件**：需下一次重启（即明早第 2 步）。在那之前，pid 18256 跑的仍是旧代码
  ⇒ **今天停 live 请用 `taskkill /F`，不要用 `python main.py --stop`**。
  — 证据：`engine/event_engine.py:4079` 起（闸门）· `tests/test_live_state_save_guard.py`
  — 发现于 2026-11-08 夜 — `[已修，待重启生效]`

- [P0] [SAFE] **含明文券商账号的一次性探针脚本已挡住（仓库是 public）**：
  `scripts/_probe_xttrader_admin.py` 第 19 行曾硬编码真实 12 位券商账号。已核实
  **从未进入 git 历史**（`git log --all -S<账号>` 为空）、**从未被 track**
  ⇒ 没有任何一次 push 泄露过。已做两件事：① 脚本内改为
  `os.environ.get("QMT_ACCOUNT_ID", "000000000000")`，与 `settings.py` 口径一致；
  ② `.gitignore` 单列 4 个一次性探针产物（`_probe_xttrader_admin.py` / `_probe_admin.bat` /
  `_probe_admin_result.txt` / `_repro_qmt_launch.py`），`git check-ignore` 已验证命中。
  ⇒ 现在任何一次 `git add -A` 都带不走账号。
  — 证据：`.gitignore` 尾部区块 · commit `230c557` — 发现于 2026-11-08 夜 — `[已修]`

- [P1] [INFO] **明早第一件要盯的事：券商连上后 2 只真实仓位会不会被回灌**：
  现在 `snapshot.positions={}` 而账本有 `300308.SZ` + `510050.SH`。这两只在
  `_sync_broker_positions` 回灌之前**不受任何退出逻辑管理**（−18% 硬止损 / 趋势破位 /
  日亏强平都不作用）。明早 `iface_probe.py` 的「引擎持仓 vs 账本」一条会直接回答：
  若仍报「账本有、引擎内存为空」，说明券商没连上，当日不能交易。
  — 证据：`scripts/iface_probe.py` D 段 — 发现于 2026-11-08 夜 — `[跟踪中]`

- [P1] [INFO] **活性 L3 把「讨论停机」当成「真的停机」——本任务今晚把自己判成了 ②（误报）**：
  `halt_hits()` 直接正则 memory 末条原文，而 11-01 的记忆正文在**描述**这批标记
  （引用了 `[MODE-ALERT]` / `整体停机` 字样）⇒ 判据命中自身文档。同一任务还有第二个自伪：
  产物名固定 `reports/meta/RSI_META_LATEST.md`，本周重跑把上周证据覆盖掉了，
  于是「11-01 明明产出了 431 行模块 + 报告」仍被判「窗口内无产物」。
  后果：任何一个任务只要在记忆里复盘「今天差点撞门禁」，下周就会被判 ②。
  ⇒ 下一轮第一顺位（改法方向：标记只认「结论位置」或排除引用；产物改当日归档快照）。
  — 证据：`scripts/_loop_liveness.py:241,245` · `reports/meta/RSI_META_LATEST.md` §三 —
  发现于 RSI 2026-11-08

- [P1] [INFO] **`tests/run_all.py` 现在给不出绿灯：33 项失败全是环境性，但会淹没真回归**：
  今晚实测 989 PASSED / 33 FAILED / 1022。失败集中在需要日线的回测套件
  （`test_span_measure` / `test_exit_next_open_proxy` / `test_conviction_boost` / …）
  与 `test_qmt_health` 两条。根因：`load_daily('300308.SZ',200)` 只返回 **6 根 bar**
  （数据源未认证，回测返回空 dict ⇒ `KeyError: 'total_return'`；health 面板直接报
  `miniQMT probe failed: not authenticated`）。**不是代码回归**：改前/改后失败集合
  `diff` 完全一致（984/1017 → 989/1022，+5 = 本轮新测试）。
  ⇒ 需要一条「环境红灯」与「回归红灯」的分离判据，否则「全绿」这一纪律事实上已失效。
  — 证据：`/tmp/runall_verify2.txt` · `strategy/backtest_daily.load_daily` — 发现于 RSI 2026-11-08

- [P2] [SAFE] **已修：L2「P0 基线漂移」判据二态 → 三态（一致 / 已声明分歧 / ★漂移）**：
  `manual_entry_codes`（回测 `()` / 生产 `['510050.SH']`，510050 是 live 链路探针）
  早在 `tests/test_evolve_baseline_sync.py::KNOWN_DIVERGENCE` 登记为刻意差异，
  但审计脚本自带一份「不相等即漂移」的判据 ⇒ R49/R50 连续两轮把它报成 **P0 首位**，
  且它**修不了**（修＝污染 P0 基线或丢对账对象）。现两侧同 import
  `config/_baseline_divergence.py`，豁免**只对逐位相同的登记值**生效。
  结果：`[基线漂移] 漂移 0 项 / 已声明分歧 1 项`，P0 首位让回给真漂移。
  — 证据：`config/_baseline_divergence.py:97` · `scripts/_recursive_self_improve.py:295` ·
  `docs/AUTOMATION_CONTRACT.md:98`（§3.4）— 发现于 RSI 2026-11-08 — `[已修]`

---

## 2026-11-08 21:45 · 大 QMT 方案核查：桥已通、开关已切、**但 QMT 里跑的还是旧代码**

### 一、历史核查结论（你问的「之前是不是已经让你换掉 miniQMT」）
是，方案是你定的，也已经建完并入库，但**开关一直没拨**，所以「替换」严格说从未生效：

| 时间 | 事件 |
|---|---|
| 11-07~11-08 | 按你的指令落地文件桥：`qmt_bridge/qmtide_bridge.py` + `core/broker_bridge.py` + 部署器 + 三份测试（commit `3768b1e`） |
| 11-08 01:43 | `simpleRun` 由 `1`→`0`，QMT 侧确认 `MODE=embedded`、`passorder=True`（内嵌引擎，能下单） |
| 11-08 02:35 | 桥接策略正式入库；`trading_config.json` **保持 `XtMiniQmt`**（当时约定：探针通过才切） |
| 11-08 21:25 | 桥仍活着：pid 29664、心跳新鲜、`QUERY 4/4 ok` |
| 11-08 21:35 | **本次**把 `broker_qmt_mode` 切成 `"bridge"`（已备份 `trading_config.json.bak-20261108-2135`） |

⇒ 也就是说：**代码与桥都早就绪，只差「拨开关 + 重启引擎」这一步**，而这两步一直没做。
`trading_config.json` 的 mtime 停在 2026-08-24 就是证据（今晚之前从未被改过）。

### 二、今晚现场实测（不是推断）
`python scripts/bridge_probe.py` —— **只读，只发 PING/DUMP/QUERY_***：

```
QUERY_ASSET    → cash 11,611.49  market_value 50,798.00  total_asset 62,409.49
QUERY_POSITION → 510050.SH 500股 @2.879 (mv 1,451) + 300308.SZ 100股 @501.5679 (mv 44,800)
stats          → requests 4 / ok 4 / timeout 0 / error 0
```

### 三、★★ 顺带查出一个会直接歪曲熔断的缺陷（已修，需重跑策略才生效）
同一轮查询里，**ACCOUNT 与 POSITION 的市值不是同一个价格**：

| 来源 | 数值 | 用的是哪天的价 |
|---|---|---|
| `ACCOUNT.m_dInstrumentValue` | 50,798.00 = 493.33×100 + 2.93×500 | **11-05 收盘**（落后一天） |
| `POSITION.m_dMarketValue` 汇总 | 46,251.00 = 448.00×100 + 2.902×500 | **11-06 收盘**（最新） |

差额 **4,547.00** 与你账本 11-06 的 `daily_pnl −4,547.03` 分毫不差；
tushare 日线独立复核：`20261105 close=493.33` / `20261106 close=448.00`。
若直接用 ACCOUNT 口径：总资产 62,409.49（真值 57,862.49）⇒ 回撤显示 **−7.5%**
而非 **−14.2%**，熔断缓冲凭空多 **6.7pp**，而你现在距 −20% 只剩 **5.78pp**。
已改为「持仓汇总优先」（查询失败才回落 ACCOUNT；矛盾态不清零，避免假回撤）。
— 证据：`qmt_bridge/qmtide_bridge.py::_q_account` · `tests/test_qmtide_bridge.py`（+4 例）
— `019836c`

### 四、另一条真实事故：VPN 关了就把行情打死（已修）
09:40 引擎由 guardian 拉起，继承 `HTTP_PROXY=127.0.0.1:7897`，而当时 VPN 没开
⇒ `tushare` 全部 `ProxyError`（日志 `WinError 10061`）⇒ 行情源按
`xtdata → tushare → mock` 降到 **mock** ⇒ `mock_trade_block_reason` 当日禁掉**所有**买卖。
即：**一个跟行情无关的环境变量，让一个交易日零成交**。
已改为只给 tushare 域名加 `NO_PROXY`（不动全局代理，GitHub 仍走代理）。
— 证据：`core/tushare_quote.py::bypass_proxy_for_tushare` · `tests/test_tushare_proxy_bypass.py`（4 例）

### 五、★ 明早顺序**不能颠倒**（顺序错了会带着错口径开盘）
1. **先在 QMT 里重新运行策略**（QMT → 策略 → 停止 → 运行）。
   验证：`C:/pazq_qmt_bridge/heartbeat.json` 里出现
   `"rev": "2026-11-08T21:31-mv-from-positions"`；`boot.log` 出现 `init done ... rev=...`。
   ⚠ **不重跑策略，新代码就不生效** —— 部署看起来成功、boot.log 有历史记录，但跑的是旧代码。
2. 再重启引擎（硬杀：`taskkill /F /PID 18256`，原因见本轮上一条 P0：优雅停止会覆盖账本）。
3. 起来后跑 `python scripts/iface_probe.py`，C 段必须看到
   `✓ 代码版本  QMT 在跑 '2026-11-08T21:31-mv-from-positions'`；
   若仍报 old/None ⇒ 第 1 步没做成。
4. 校验数据：`total_asset` 应约 **57,862**（不是 62,409），`positions` 应有 2 只。
5. miniQMT 不必再装/再登（行情走 tushare、交易走大 QMT）—— 但**若你想用 xtdata 行情，
   仍需要 miniQMT 登录**；两者可共存，`QMT_QUOTE_SOURCE=auto` 会优先 xtdata。

### 六、回滚（一条命令，零残留）
`trading_config.json` 里 `"broker_qmt_mode": "bridge"` → `"XtMiniQmt"`，重启引擎。
备份就在同目录 `trading_config.json.bak-20261108-2135`。

---

## 2026-11-08 22:05 · 重启后复核：**如期**，自检 PASS 16 / WARN 2 / FAIL 0

### 一、最担心的一件事：账本没被关机污染
| 字段 | 值 | 判定 |
|---|---|---|
| `engine_state.ts` | 2026-11-06T15:04:54 | 关机没写库 |
| `cash` | 11,611.57 | 未被写成 1,000,000 |
| `day_open_asset` | 62,409.6 | 未被写成 NULL |
| `peak_asset` | 67,453.62 | 保留 |
| 持仓 | 2 只 | 未清零 |
| `storage/qmt.db` mtime | 11-08 02:13:46 | 全程未变 |

⇒ Windows 关机是 TerminateProcess，没有走 `finally: _shutdown()`。之前那条「优雅停止会覆盖账本」的闸门（`abb13e7`）现在也已随重启生效，以后 `--stop` 也安全了。

### 二、桥与行情都自己起来了（这次没让人动手）
- `BridgeBroker 已连接（root=C:/pazq_qmt_bridge, heartbeat_age=0.3s）` @21:59:55
- `Engine 启动: mode=single data=tushare broker=bridge exec=live codes=38`
- QMT 重启后**策略自动恢复运行**，心跳 `pid=13768 rev=2026-11-08T21:31-mv-from-positions` ⇒ 新代码在跑
- 券商口径回灌：`cash 11,611.49 / 总资产 57,862.49 / 持仓 2 只`（300308 −10.68%、510050 +0.80%）⇒ 与账本 57,862.57 差 **0.08**（佣金精度）
- `data_mode=tushare`（不是 mock）⇒ **NO_PROXY 修复生效**：VPN 没开也能取到真实行情

### 三、两个 WARN 都将在周一开盘首轮自动消失
1. **熔断 `halted=true`（daily_stop −9.32%，11-06 触发）**：`halt_recover_days=1`，已冷却 2 日
   ⇒ 开盘首轮 `_maybe_recover()` 自动解除。
   ★ **但要知道代价**：解除时 `peak_asset` 会被**重置为当日资产（≈57.8k）**，
   回撤预算从当日重新起算 ⇒ 之前的「距 −20% 只剩 5.78pp」在开盘后**不再成立**，
   新地板约 46.3k（旧地板 53.96k）。这是既有设计（避免解除后立刻再熔断），不是本周改动引入。
2. **`asset_trusted=false`**：休市时引擎在时段守卫里休眠，`_run_once` 不跑 ⇒
   `_risk_total_asset()` 从未被调用。开盘首轮变 true。**不是故障。**

### 四、已补的启动/自检缺口（本次重启暴露的）
- **paper 区与看板不会自启**（`startup_all.bat` 只拉 QMT + guardian + 引擎）⇒ 本次已手动拉起：
  paper `5001` pid 5416（`paper_no_trade` 拒单桩）、看板 `5002` pid 23128。
- `zone_preflight.py` 两处假阳性已修（`a4ee7f5` 之后的新 commit）：
  ① 越界判据改为「谁在**同一秒**说自己刷新过候选池」——按 mtime 比较会把
     「live 自己写 live 的文件」误判成 paper 越界（22:00 实测踩到）；
  ② paper 冷启动第一包实测 15.4s，原 6s 超时必然误报「不可用」⇒ 单独放宽到 30s（live 仍 6s）。
  ③ `halted` / `asset_trusted` 按冷却天数与会话状态分级，不再每天报同一个假警报。

---

## 2026-11-08 22:40 · 挂起项（OWNER：明天交易日后再评估）

- [P2] [INFO] **两处 11-04 遗留的未提交文档改动，暂不提交，待交易日后再评估**：
  · `docs/AUTOMATION_CONTRACT.md`（+40 行：KPI 口径表的表格分隔符改动 + 新增一节）
  · `reports/DAILY_REVIEW_20261104_SUPPLEMENT.md`（+14 行）
  与 11-08 本轮改动无关，工作树里保留原样；本次推送（`4ac84f0`）刻意没带上它们。
  — 证据：`git diff --stat` 显示 2 files / +52 −2 — 发现于 主理人 2026-11-08 — `[挂起，等 OWNER]`

- [P2] [INFO] **待 OWNER 一句话决定：paper 区与看板是否做「每交易日 09:05 自动启动」**：
  现在 `scripts/startup_all.bat` 只拉 QMT + guardian + 引擎（live）。
  2026-11-08 22:02 整机重启实测：paper `5001` 与看板 `5002` 都不会自启，需人工拉起。
  若要自动化，必须建成「只拉 `--zone paper`、启动后自检 5001 端口与 `storage_paper/` 库路径」的形式，
  绝不能出现指向 5000 的兜底 —— 那会把观察区拉成第二个 live。
  — 证据：`scripts/startup_all.bat`（无 paper 分支）· `scripts/run_paper.bat`
  — 发现于 主理人 2026-11-08 — `[挂起，等 OWNER]`

---

## 2026-11-08 23:00 · 「观察区没有数据」根因核查（只读实测）

**结论：观察区没坏。它在线、快照完整，是账本里一行数据都没有 —— 因为库是今晚新建的，且今天是周日休市。**

### 一、三层证据（同一时刻实测）

| 层 | 实测值 | 含义 |
|---|---|---|
| 进程 | pid **5416** 存活，124 MB | 引擎活着 |
| HTTP | `GET /api/snapshot` → **200 / 0.08s** | 取数正常，**不是超时** |
| 快照 | `exec_mode=paper` `data_mode=tushare` `broker=paper_no_trade`，候选池 30 只 | 身份/通道/池都对 |
| 会话 | `state=closed_non_trading_day` `trading_now=false` **`tick=0`** | 主循环在守卫里休眠，**一轮都没跑过** |
| **账本** | `storage_paper/qmt.db` 10 张表：`equity_snapshots` **0**、`signals` **0**、`sector_recommendations` **0**、`fills` **0**、`orders` **0** | **空的** |
| 对比 live | `storage/qmt.db` `equity_snapshots` **1,216 行 / 244 点** | live 有历史，paper 没有 |

`tick=0` 是铁证：`event_engine.py:1396` 的 `_tick_count += 1` 在守卫 `continue` **之后**，
所以「从未进入交易时段」必然表现为 `tick=0`。这不是主循环挂掉。

### 二、为什么 live 有、paper 没有

- **分库不继承**：zone 隔离后 paper 用 `storage_paper/qmt.db`（11-08 19:57 新建），
  live 的 6,629 条历史一条都没迁过来。看板 `_read_series()` 有「窗口空则退回全表末 N 行」的兜底，
  但全表也是 0 行 ⇒ 曲线 0 点、板块/个股/信号 0 条 ⇒ 页面显示「该区尚无板块数据（交易日盘中产生）」。
- **写入只在轮次里**：`equity_snapshots` 由 `_save_equity_with_ledger()`（`event_engine.py:3914`）
  在**每轮**写、**60 秒**节流；`sector_recommendations` 每 **5 tick** 写。跑 0 轮 ⇒ 写 0 行。

### 三、之前怀疑的 3 秒超时已排除

`scripts/zone_dashboard.py:67 SNAP_TIMEOUT=3.0`，而 22:00 首次实测 5001 根路径 15.36s ——
曾怀疑是超时。**现在实测 0.079s**，看板 `_read_series` 对 paper 库 `error=None`（能读，只是 0 行）。
15.36s 只是**首次冷启动预热**（补拉 38 只日线 + 5,579 只名称缓存），之后常驻热。此怀疑作废。

### 四、明天开盘会发生什么（无需人工干预）

- `event_engine.py:1394` 每 `IDLE_REFRESH_INTERVAL` 秒醒来问一次 `is_trading_time()`，
  09:30 自动破守卫 ⇒ `tick` 从 0 开始涨。
- **首轮就写第一条 `equity_snapshots`**，之后约每分钟一点；`sector_recommendations` 第 5 tick 起有行。
- 09:30 后约 5 分钟，看板观察区的曲线/板块/个股/信号应全部出现。

### 五、[P1] 需要 OWNER 知道的一个真实风险

**paper 区从建库至今从未跑过任何一轮**（`tick=0`）⇒ **明天 09:30 是观察区的首次实跑**。
tushare 行情 → 评分 → 漏斗 → `paper_no_trade` 拒单桩这条链路，在 paper 区**一次都没验证过**。
live 侧链路是通的（今天已验），但 paper 是独立进程、独立库、独立风控状态，不能假定等同。
建议明天开盘后 5 分钟看一次 5001 的 `tick` 与 `storage_paper/qmt.db` 的 `equity_snapshots` 行数。

— 证据：`storage_paper/qmt.db`（10 表全 0，`engine_state` 初始值）· `curl 5001/api/snapshot`（`tick=0`）·
`event_engine.py:1394/3914/4474` · `scripts/zone_dashboard.py:67/95-140`
— 发现于 主理人 2026-11-08 — `[INFO，无需处置；第五节是明天的观察点]`

---

## 2026-11-08 23:10 · 观察区「闸门卡点时间线」空 —— 根因 + 已修四处 + 已播种历史

**结论：不是故障，但确实有三个真缺陷被它掩盖着。已修完并给观察区补上 868 行同口径历史。**

### 一、空的三层原因

1. **文件根本没生成**：`logs_paper/gate_timeline.jsonl` 不存在（live 的是 `logs/gate_timeline.jsonl`，872 行）。
   该文件由引擎在**交易日**的轮次里追加，今天周日 + paper 区 `tick=0`（从未跑过轮次）⇒ 一行没有。
2. **分库把「市场属性」也分了**：`gate_timeline` 记的是「固定 23 只宇宙里，每天 7 个条件各有几只通过」，
   只取决于那天的日线与固定宇宙 —— 与账户/持仓/执行模式**无关**，两区算出来的行本就该相等。
   但它跟着 `LOG_DIR` 一起分区，于是观察区新建后从零累积。这是**分库设计的一个疏漏**，已写进 `docs/ZONE.md §7.x`。
3. **空态没有解释**：前端只显示「暂无时间线数据（等待每日回填/实时追加）」，
   看不出是坏了还是本来就没有 —— 所以你才需要问这一句。

### 二、顺带查出的三个真缺陷（都已修）

| # | 缺陷 | 生产上的实证 |
|---|---|---|
| 1 | `gate_timeline_row()` 的 `source` **硬编码 `"live"`** | paper 写出的行也自称 live，两区日志无法区分来源 |
| 2 | 去重标志 `_gate_timeline_date` **只在内存**，盘中重启即丢 | `logs/gate_timeline.jsonl` 已有 `10-29`×3、`10-30`×2、`11-03`×2 共 **6 行重复** |
| 3 | 读端 `/api/gate_timeline` **不按 date 去重** | 重复日会在热力矩阵里画成多个格子、**日期横向错位**（末 8 天里 10-30 与 11-03 各出现两次） |

修法：
1. `source` 改取 `owner.exec_mode`，无该属性时回落 `"live"`（保住 `test_gate_detail.py:155` 的既有断言）。
2. 首次调用时读文件尾回灌 `_read_last_timeline_date()`（与 `open_date` 的「DB→内存回灌」同源）。
3. 读端按 date 去重（同日保留**最后一条**），并返回 `hint` / `file` / `error` 供空态显示。

### 三、已做的处置：给观察区播种 868 行同口径历史

```
python scripts/seed_zone_timeline.py --to paper            # dry-run
python scripts/seed_zone_timeline.py --to paper --apply    # 实际写（幂等）
```

- 实测结果：`logs_paper/gate_timeline.jsonl` **868 行**（2023-04-07 → 2026-11-06），
  每行带 `origin_zone="live"` 标明**不是本区跑出来的**。
- **live 文件逐位未动**：仍是 872 行、mtime `2026-11-06 09:30`。
- 幂等已验证：再跑一次输出「无需写入」。
- 安全边界：`--to live` 直接拒绝（退出码 2）；目标文件按 date 升序**整体重写**而非 append
  （append 会让「补更早的日期」破坏单调性 —— 这是写完第一版才发现的真 bug，已改）。
- **观察区 5001 无需重启就已生效**（只是文件存在了）：实测 `--days 5` 返回
  11-02/11-03/11-04/11-05/11-06 五行。

### 四、需要你决定/执行的两件事

1. **[需重启] paper 进程重启后**，空态文案与「按 date 去重 / hint」才生效（现在跑的是旧代码）。
   **不重启也能看**，只是 live 侧那 6 行重复还会画成多格。
2. **[需重启] live 进程重启后**，读端去重才生效 —— 现在 live 页面末 8 天里
   10-30 与 11-03 各画了两次（数据没错，只是格子多）。改动不影响下单路径，可以等下次自然重启。

### 五、验证

- 新增 `tests/test_gate_timeline_zone.py` **15 例**（含正向对照：把 `source` 改回硬编码 `"live"` 必须变红）。
- 已登记进 `tests/run_all.py` 的显式清单（**不登记就永远不执行** —— 这个坑 runner 注释里写过）。
- 全量回归：**1012 PASS / 33 FAIL / 1045**（997+15 新增全绿；33 个失败与基线逐一致，全是 xtdata `not authenticated` 环境级联）。
- 隔离自检 `zone_preflight.py`：**PASS 16 / WARN 2 / FAIL 0**，与重启后一致，未见新越界告警。

— 证据：`logs_paper/gate_timeline.jsonl`（868 行）· `logs/gate_timeline.jsonl`（872 行，未改）·
`engine/observability.py:404-421` · `engine/event_engine.py:255-291,3946-3970` · `web/routes.py:121-192`
— 发现于 主理人 2026-11-08 — `[已修，两处重启后生效]`

---

## 2026-11-09 08:52 · 盘前检查（距开盘 38 分钟）— 三态①准备态，2 个 P0

完整报告：`reports/PREOPEN_CHECK_20261109.md`

- [P0] [INFO] **熔断今天首轮必解除，回撤保护被削 7,672.90 元**：`halt_day=11-06`，`held=3 ≥ halt_recover_days=1` ⇒
  解除时 `risk/manager.py:352-353` 无条件 `_peak_asset = total_asset` ⇒ −20% 线从 **53,962.90 抬到 46,289.99**。
  **但存在第二结局**：若首轮券商值不新鲜（>120s）致 `_risk_total_asset()` 返 None，则解除但 peak 不重置，旧线仍有效。
  两者差 7,672.90 元保护。**开盘首轮后请以 `/api/snapshot` 的 `risk.peak_asset` 实测确认走了哪条。**
  — 证据：`engine_state.risk_state.halt_day=2026-11-06` · `risk/manager.py:341-353` — 发现于 主理人 2026-11-09

- [P0] [FORBIDDEN→待 OWNER] **两只持仓 `open_date=null`，T+1 与建仓保护期双双失效**：`is_t1_locked(None)` 返 False
  （`event_engine.py:94`）。今天两只都是老仓（300308 建仓 11-03）不触发；**若今天开新仓且盘中重启 ⇒ 新仓当日可卖 ⇒ 违反 T+1 的废单**。
  修法：从 `fills` 回灌建仓日到 `engine_state`（只写 DB、不改参数、不需重启）。**未执行，等 OWNER 一句话。**
  — 证据：`engine_state.positions[300308.SZ].open_date=null` — 发现于 主理人 2026-11-09

- [P1] [INFO] **今天大概率仍是零 BUY**：`gate_funnel.momentum_pass_n=0 / universe_n=37`（live 与 paper 逐位相同），
  `min_mom=0.15` 是最后硬卡点。⇒ 熔断即使解除，`passorder` 真实落单首验**仍无法完成**（ORDER 累计仍 0）。
  — 证据：`/api/snapshot` gate_funnel — 发现于 主理人 2026-11-09

- [P1] [INFO] **首次可动用现金仅 8,700 元，30 只候选里 11 只买得起一手**：现金 11,611.49 − 5% 缓冲 2,893.12 ⇒
  单价上限 **87.00 元/股**，19 只（63%）一手都买不起。最便宜 600522.SH 23.15（2,315 元/手）。
  — 证据：独立探针 `daily(trade_date=20261106)` 全量取价 — 发现于 主理人 2026-11-09

- [P1] [INFO] **卖出空档仅 0.48%**：解熔后 −6% 日线（300308≈413.28）距 −18% 灾难止损 411.286 只差约 2.00 元，
  期间两只豁免仓只停开仓不减仓。「卖出被完全拦掉」**不成立**（修正 risk-controller-4 初稿）。
  — 证据：`engine_state.positions[300308].stop_price=411.286` · `settings.py:exit_exempt_codes` — 发现于 risk-controller-4 / 主理人复核 2026-11-09

- [P1] [SAFE 已处置] **观察区 5001 与看板 5002 进程已死，本次已拉起**：`logs_paper/engine.pid` 仍写 5416 但进程不存在。
  已用 `QMT_ZONE=paper QMT_QUOTE_SOURCE=auto` 重启 paper（首包 22.4s 冷启动后就绪，候选池 30 只 @08:45:48），看板 5002 0.002s。
  `scripts/startup_all.bat` **仍无 paper/看板分支** ⇒ 每次整机重启都要人工拉起（见下方 22:40 挂起项）。
  — 证据：`tasklist` 仅 17488 · `curl :5001` `:5002` — 发现于 主理人 2026-11-09

- [P2] [INFO] 北向缓存陈旧 76 天（fail-open）、分钟线预热就绪 1/38（tushare stk_mins 限频 1 次/小时，评分走日线不受影响）、
  交易日历不可用按周内判定（今天周一判定正确）。
  — 证据：`logs/quant_system.log` 08:40-08:43 — 发现于 主理人 2026-11-09

- [P2] [INFO] **两项历史疑点已验证无碍**：① `mock_trade_block_reason()` 首行 `data_mode != "mock" → return None`
  （`event_engine.py:81`）仍在 ⇒ tushare 不会被误判成 mock 全禁单；② 桥的 `m_dTotalAsset=137,321.50` 坏值已被
  `_total_asset_of()` 拦下，回落 cash+市值 57,862.49，与账本差 0.08（佣金）。
  — 证据：`C:/pazq_qmt_bridge/boot.log` 08:41:01 — 发现于 主理人 2026-11-09

- [P1] [INFO] **桥本身正常**（重要澄清）：`XtItClient.exe` pid 19364（969 MB）存活，心跳 **age=1 秒**、`rev=2026-11-08T21:31-mv-from-positions`，
  引擎 `broker_connected=True`（`broker_bridge.py:46` 阈值 6s）。**桥不需要任何操作。**
  — 证据：`heartbeat.json` mtime 08:47:11 — 发现于 主理人 2026-11-09

- [P0] [FORBIDDEN-待 OWNER] **桥的总资产漏加冻结资金（frozen），挂单未成交会造成假回撤**：
  `qmt_bridge/qmtide_bridge.py:614 derived = cash + mv`，而 `cash` 取的是 `m_dAvailable`（**可用**），
  `frozen`（`:577` 单独返回）**没有计入**。现在 frozen=0 无差；**一旦今天挂出未成交买单，可用资金下降而冻结未加回
  ⇒ total_asset 被低估 ⇒ 假回撤 ⇒ 可能触发 `daily_stop_flatten_pct=-6%` 与 daily_limit 熔断**（与 11-02「−89.25% 假亏→强平」同类机制）。
  **实际后果评估**：两只持仓都在 `exit_exempt_codes` ⇒ 强平提交 0 手 ⇒ **不会造成真实亏损，只会让当日停开仓空转**。
  修法 `derived = cash + frozen + mv`，需改 QMT 侧代码 + 重新部署 + 在 QMT 里重跑策略 ⇒ **开盘前无法完成，只能登记 + 监控**。
  — 证据：`qmtide_bridge.py:546/577/614` · 发现于 live-ops-engineer-zone 2026-11-09（主理人已复核代码）

- [P1] [INFO] **熔断解除的时机已在买入决策之前，所以大概率走「重置 peak」分支**：`_run_once` 内
  `_sync_broker_positions()`（:2155）→ `risk.on_asset_update()`（:2194）→ `_maybe_recover(today, total_asset)`（manager.py:136）
  ⇒ `_handle_buy` 读 `position_scale` 时已是 1.0。**前提是同一轮 `_risk_total_asset()` 非 None（桥活着即成立）**。
  这修正了上一版报告「两种结局概率相当」的说法：**重置是默认路径，不重置只在桥被判断开时发生**。
  — 证据：`event_engine.py:2155/2194` · `risk/manager.py:136` — 发现于 live-ops-engineer-zone 2026-11-09

- [P1] [INFO] **09:30 首 1~2 轮可能以 `market_stale` 拒单**（不是无信号）：`MARKET_STALE_SEC=120`，
  tushare 首轮可能带上一交易日收盘时间戳（age 数小时）⇒ 判陈旧 ⇒ `_handle_buy` 拒单；等 09:30 价刷出后自动恢复。
  — 证据：`event_engine.py:1790/3486` · `core/tushare_quote.py:249/583-587` — 发现于 live-ops-engineer-zone 2026-11-09

- [P1] [INFO] **桥判死阈值只有 2 倍余量**：`HEARTBEAT_STALE_SEC=6.0`（`broker_bridge.py:46`），实测心跳间隔
  1.50/1.505/3.002 秒（空闲时）。若某次 poll 阻塞 >6s（如 `passorder` 阻塞）⇒ 引擎判 bridge 断开 ⇒
  `_sync_broker_positions` 首行 return ⇒ 券商口径停更 ⇒ 熔断/强平判定跳过（不会瞎下单，但会"看不见账户"）。
  **今日 watch 点**：`C:/pazq_qmt_bridge/heartbeat.json` 的 ts 间隔出现 >6s 即会打 `心跳过期（age=…）` 日志。
  — 证据：`qmtide_bridge.py:82-83`（POLL_MS=500/HEARTBEAT_SEC=1.0）+ 实测采样 — 发现于 live-ops-engineer-zone 2026-11-09

- [P2] [INFO] **两处文案会让复盘误判通道与数据质量**（不影响任何逻辑）：
  ① `event_engine.py:1317` 硬编码 `"实盘券商已连接 (XtQuantTrader)"`，实际走 bridge；
  ② `data_mode=tushare` 的告警写「行情可能为模拟/回退数据，仅用于自检」，而 tushare 是**真实行情**（优先级 xtdata→tushare→mock）。
  — 证据：`engine/event_engine.py:1317/1376-1378` — 发现于 live-ops-engineer-zone 2026-11-09

- [P2] [INFO] **今天开盘第一批落盘会有「11-06 → 11-09」跨日断层**：`_persist_equity` 只在 `risk_asset is not None` 时落盘，
  11-07/11-08 两天 broker 不可用 ⇒ 全程未写 `engine_state`（ts 仍 11-06T15:04:54，wal 0 字节）。
  对今日 EOD 复盘口径有影响，已告知 data-ledger 方向。
  — 证据：`event_engine.py:2261-2262` · `storage/qmt.db` mtime — 发现于 live-ops-engineer-zone 2026-11-09

- [P1] [INFO] **今天新买的仓位不受 `open_date=null` 影响**（修正前一版说法）：`_pos_meta` 在进程内写入
  `open_date=datetime.now()`（`:3612`），T+1 与 30 分钟保护期对新仓照常生效。**前提：今天不再重启引擎**
  （重启后 `_pos_meta` 丢失，从 DB 恢复时 open_date 仍为 null）。老仓的 open_date=null 在 DB 里已无从恢复。
  — 证据：`event_engine.py:2005/3612/4361` — 发现于 live-ops-engineer-zone 2026-11-09

---

## 2026-11-09 09:36 · 开盘首轮实测（零委托，原因=动量闸门；熔断已解除、peak 已重写）

- [P1] [INFO] **今日零委托，唯一拦单环节是 `momentum`**：`no_entry_reason='动量闸门后候选池为空（min_mom=0.15 lookback=60 top_n=3）'`，
  `gate_funnel` 恒为 `universe=37 / trend_up=3 / score_ok=8 / momentum_pass=0 / bias_ok=0`。
  逐条排除：**不是** `market_stale`（桥每 4s 返回新市值）、**不是** `insufficient_cash`（cash 11,611.49 全程未动）、
  **不是** `position_scale=0`（09:30:17 已解除为 1.0）。⇒ `passorder` 真实落单首验**继续推迟**，ORDER 累计仍 0。
  — 证据：`/api/snapshot` no_entry_reason + gate_funnel — 发现于 live-ops-engineer-zone / 主理人复核 2026-11-09

- [P0] [INFO] **熔断已于 09:30:17 解除，`peak_asset` 被重写 67,453.62 → 58,162.49**（走了「重置」分支，此前预测的默认路径）：
  新 −20% 线 = 58,162.49×0.8 = **46,529.99**，旧线 53,962.90 ⇒ **保护被削 7,432.91 元**。
  注意实测 reset 值是 **58,162.49** 而非预估的 57,862.49（复位瞬间按当时价重估，差 +300）。
  `halted=False`、`position_scale=1.0`，即**今天账户已具备开仓能力**，只是没有信号。
  — 证据：`/api/snapshot` 09:30:17 risk 段 — 发现于 live-ops-engineer-zone 2026-11-09

- [P1] [INFO] **`frozen` 缺陷今天未被触发**：`frozen` 全程 **0.0**，`cash` 恒 11,611.49 未动。
  期间 `total_asset` 的跳降（58,162.49 → 57,861.99 → 57,372.99）**全部跟随 300308 市值波动**
  （market_value 46,250.5 → 45,761.5），不是「恰好等于一笔买入金额」的台阶。判定依据：frozen 缺陷要求 cash 下降且 frozen>0。
  — 证据：QUERY_ASSET 四字段连续三笔原值（09:30:27 / 09:30:44 / 09:31:15）— 发现于 live-ops-engineer-zone 2026-11-09

- [P1] [INFO] **心跳空洞与查询负载无关（已反证）**：开盘后 7 分钟内 QUERY_ASSET/POSITION/TRADE 各 **95 次**（≈每 4.4s 一轮），
  而该段 `max_dts` 仅 **2.50s**、`gaps=[]`、无 PID 变更、无 init 重载。
  ⇒ 08:52 的 20.94s 与 09:14 的 11.07s **只与 `init()` 重载相关**。今天 QMT 侧共 4 次 init，策略名后缀全不同
  （`00030010 / 0003003 / 00030016 / 30030823`），pid 始终 17080 ⇒ **多个策略条目在同一进程内被反复加载**，
  `WinError 183` 与「多写者争抢同一 heartbeat.json.tmp」假说一致。**需 OWNER 在 QMT 界面确认条目数并停掉多余的。**
  — 证据：`boot.log` 四次 init 原文 + `/api/snapshot` 采样 — 发现于 live-ops-engineer-zone 2026-11-09

- [P2] [INFO] **`signals` 表今日 0 行，`blocked_by` 无法交付**：最后一次写入仍是 11-03 10:59，11-06 与今天均为 0。
  已用 `no_entry_reason` + `gate_funnel` 替代，并明确「表里没有」而非编造原因。
  — 证据：`select count(*) from signals where ts>='2026-11-09'` = 0（7 次轮询）— 发现于 live-ops-engineer-zone 2026-11-09

### 2026-11-09 09:40 开盘 10 分钟实盘复测（含此前未暴露的日内熔断路径冲突）

- [P1] [BUG] **日内亏损熔断存在「判定不一致」：`can_open` 说可以开，`on_fill` 说成交即熔断。**
  `can_open` 取 `_daily_loss_limit=max(5000, 3%×权益)=5000 元`（`risk/manager.py:222-229`）；
  但 `on_fill` 在 abs 分支之下多挂了一个**未合并的原始 pct 兜底**（`:324-327`）：
  `elif total_asset>0: if loss_pct <= daily_loss_limit_pct(-0.03): _halt(...)`。
  **今天正好落在两者之间的缝里**：`daily_pnl=-1,911.00`（`total_asset` 56,251.49）= **-3.40%**，
  已越过 -3% 却远未到 5,000 元 ⇒ `can_open` 判「可开仓」，而**只要今天成交第一笔**，`on_fill` 立即
  以 `daily_loss_pct -3.40%` 熔断，当天再开不了第二个仓。
  - 今日未触发的原因：`daily_trade_count=0`，`on_fill` 一次都没跑 ⇒ 不是「风控放行」，是「风控根本没被调用」。
  - 不亏真钱：`_halt()` 只置 `_halted`，**不设 `_flatten_requested`**（`risk/manager.py:_halt`），只停开仓不强平。
  - 建议修法（收盘后）：把 `:324-327` 的 `elif` 并进 `_daily_loss_limit` 口径，两处用**同一个函数**判定。
  — 证据：`/api/snapshot` risk 块 09:38:xx；`risk/manager.py:222-229 / 324-327` — 发现于主理人 2026-11-09

- [P1] [INFO] **盘中真实回撤 -3.26%，但风险阶梯的最严一档在 -6%（豁免名单使其不成交）**。
  `total_asset` 56,264.49（09:39）/ `peak` 58,162.49 ⇒ **-3.26%**；同口径对旧 peak 67,453.62 是 **-16.59%**。
  可执行阶梯：`daily_stop_flatten_pct=-6%`（3,490 元，距 1,579 元）→ 触发则请求强平全部，但两只均豁免 ⇒ 提交 0 手；
  `300308` 灾难止损 `hard_stop_pct=-0.18` ⇒ 501.568×0.82=**411.29**，现价 432.02 距 **-4.8%**（20.73 元/股）；
  账户熔断 `max_drawdown=-0.20` ⇒ 新线 **46,529.99**，距 **-17.30%**（旧线 53,962.90，重写后退了 7,432.90 元）。
  — 证据：`/api/snapshot` 09:38-09:39 三次采样；`config/settings.py` RISK/STRATEGY 参数读取 — 发现于主理人 2026-11-09

- [P2] [INFO] **持仓浮亏口径**：`300308` 501.568→432.02（**-13.87%**，浮亏 6,954.79），`510050` 2.879→2.906（+0.94%，+13.50）。
  今日跌的是 300308（-3.57%），510050 微涨；`cash` 全天 11,611.49 未动。
  — 证据：`/api/snapshot.positions` — 发现于主理人 2026-11-09

- [P0] [FORBIDDEN-待 OWNER] **熔断恢复把回撤基线重置了，−20% 硬线被放宽 7,432.91 元**：11-06 日亏熔断于今日 09:30:10 自动解除，
  `_peak_asset` 由 67,453.62 → 58,162.49（`risk/manager.py:352-353`「重置基线，避免解除后立即再熔断」）⇒ 面板回撤 −3.42%，
  而真实峰值回撤 −16.72%；−20% 触发位由 53,962.90 降到 46,529.99，等于要真实亏损约 31% 才触发最后防线。
  对照 `risk/manager.py:376` 僵尸解封路径**刻意不重置** peak（注释「max_drawdown 保护不弱化」）⇒ 两条恢复路径口径自相矛盾。
  `asset_trusted=True` 不覆盖此问题：`observability.py:165-166` 只校验当期 basis==broker，不校验 peak 历史连续性。
  — 证据：`engine_state.peak_asset=58162.49` · `risk/manager.py:352-353/376` · `logs/quant_system.log` 09:30:10 — 发现于 开盘接口预验证 2026-11-09

- [P1] [INFO] **七项开盘预验证 6 PASS / 1 FAIL（预期内）**：`data_mode=tushare`（非 mock ⇒ `mock_trade_block_reason()` 不生效）、
  `broker_connected=true`（桥心跳 age≈1s）、`halted=false`、`asset_trusted=true`、持仓 2 只已回灌（券商 available 全额）、paper 5001 在跑。
  唯一 FAIL = `xtdata.get_full_tick → not authenticated`（大 QMT 白名单已知问题，引擎已降级 tushare，非新故障）。
  今日 09:30~09:40 零委托、零拒单、零 market_stale；漏斗 `trend_up 3 / bias>=2 0 只` ⇒ 零成交归因为**无信号**，非接口故障。
  — 证据：`scripts/zone_preflight.py` PASS18 WARN0 FAIL0 · `scripts/iface_probe.py` PASS30 WARN3 FAIL1 — 发现于 开盘接口预验证 2026-11-09

- [P1] [INFO] **引擎与账本同刻一致（0.00%），09:36 报的 −0.19% 是时间差伪差**：09:38:17 快照 56,174.49 == 引擎同刻 56,174.49。
  `iface_probe.py` D 段用「引擎当前值比最近一条快照」（快照间隔 ~60s），盘中波动大时会误报 >0.5%，属脚本口径问题非估值错。
  — 证据：`equity_snapshots` 09:38:17.024212 · `/api/snapshot.total_asset` — 发现于 开盘接口预验证 2026-11-09

- [P2] [INFO] **510050.SH 的 tushare 日线兜底是死路径**：`daily_context.py:151` 用 `pro.daily`，ETF 返回空表（实测 0 根）；
  数据在 `pro.fund_daily`（203 根，末根 11-06 close 2.902）。今日靠 xtdata 本地日线兜住；xtdata 本地一旦停更则两条路全断。
  — 证据：`strategy/daily_context.py:142-174` · 实测 `pro.daily` 0 根 / `fund_daily` 203 根 — 发现于 开盘接口预验证 2026-11-09

- [P2] [INFO] **「日线更新到今天」判据在盘中不成立（口径修正）**：`_expected_last_trade_date()` 盘中返回上一交易日，
  实测两只票末根均为 2026-11-06（300308 close 448.000 / 510050 close 2.902）= 期望值 ⇒ 日线新鲜、未滞后。
  — 证据：`strategy/daily_context.py:176-191` — 发现于 开盘接口预验证 2026-11-09

- [P0] [SAFE-待修/需重启] **权益曲线的回撤分母 `_peak_equity` 每次 live 重启都归零**（与上面那条 `risk._peak_asset` 是两个独立缺陷）：
  `event_engine.py:565` 初值 0.0；live 以 `restore_ledger=False` 启动，`:4021` 的 `if not restore_ledger:` 分支恢复风控字段后**早退**，
  跳过 `:4055` 的 `_peak_equity` 恢复与 `:4056` `_sanitize_restored_peaks()` ⇒ 第一次落盘即等于当日首 tick 资产（今日 58,162.49 == day_open_asset）。
  **这解释了记忆里悬而未决的「两套 peak 差 3.74pp」**：11-06 表内 −10.48% 的分母≈64,635.7（11-05 重启后重新起算），而非真实峰值 67,453.62。
  今日 `equity_snapshots.drawdown_pct=-0.0321`，真实峰值回撤 −16.55%。**不熔断的日子也在悄悄刷分母**，比熔断重置更常触发。
  修法（不碰交易参数）：早退分支补 `_peak_equity = max(_credible_peak_equity(), 0)`，或 `_persist_equity` 改用 `max(_peak_equity, risk._peak_asset)`。
  — 证据：`event_engine.py:565/3944-3949/4021-4032/4055-4056` · `engine_state.peak_equity=58162.49` — 发现于 开盘接口预验证 2026-11-09（data-ledger-auditor 定性，主理人复核代码）

- [P2] [INFO] **实时价其实来自新浪，代码与面板都写「tushare」**：今日 tushare `rt_tick` 无权限，`core/tushare_quote.py` 自动走新浪实时（~8s、仅最新价、无五档）。
  连带 `event_engine.py:1377-1379` 的告警仍写「行情可能为模拟/回退数据，仅用于自检」⇒ 对真实行情是**假警报**。不影响市价单撮合。
  — 证据：`core/tushare_quote.py` realtime_quote · `engine/event_engine.py:1377-1379` — 发现于 开盘接口预验证 2026-11-09（live-ops-engineer 实证）

- [P2] [INFO] **今日三张交易表全空 + 账本基线数字修正**：`signals`/`orders`/`fills` 今日均 0 行，与「无入场信号」互证（不是下了单没记）。
  11-06 收盘应为 **57,862.57**、现金 **11,611.57**（记忆里 57,862.49/11,611.49 差 0.08 佣金舍入）。
  — 证据：`storage/qmt.db` signals/orders/fills · `equity_snapshots` 11-06 末行 — 发现于 开盘接口预验证 2026-11-09

- [P2] [UPDATE 09:50] 上条 P1（`on_fill` pct 冲突）**今日已休眠**：`total_asset` 回升至 57,917.49（09:46 分段最高），
  对 `day_open_asset=58,162.49` 仅 **-0.42%**，已回到 `-3%` 线内 ⇒ 除非再跌穿 -3%，第一笔成交不会触发该分支。
  缺陷本身未修，仍成立，只是今天大概率踩不到。
  — 证据：`/api/snapshot` 09:46:02；live-ops-engineer-zone 720 次心跳采样 — 发现于主理人 2026-11-09

### 2026-11-09 10:00 盘中收尾（盯盘任务结束点）

- [P2] [INFO] **日内已转正，09:40 那份「-3.26%」是早盘瞬时值，勿作结论**：10:00:58 `total_asset=59,106.99`
  （`day_open_asset=58,162.49` ⇒ **+1.62%**，`daily_pnl=+944.50`）；`300308` 从 432.02 反弹至 **460.40**（浮亏收窄到 -8.21%）。
  — 证据：`/api/snapshot` 10:00:58 — 发现于主理人 2026-11-09

- [P2] [INFO] **`peak_asset` 解除后「尾随新高上行」已确认**：58,162.49（09:30）→ 58,796.99（09:59）→ **59,115.99**（10:00:58）。
  ⇒ `-20%` 线 **47,292.79**、缓冲 11,814.20 元（19.99%）。
  **但不要读成「保护变宽」**：新口径回撤 -0.02%、真实口径（对历史峰值 67,453.62）仍是 **-12.37%**；
  上行的是分母，不是账户。历史高点丢失这件事没有因为反弹而恢复。
  — 证据：`/api/snapshot.risk`；`risk/manager.py:352-353` — 发现于主理人 2026-11-09

- [P3] [INFO] 心跳：盘前 2 次断档（08:52:50 档 20.94s 换进程 / 09:14:40 档 11.07s 同进程重载），
  **09:14:51 之后 46 分钟、约 2,750 次 1s 采样零缺口**（max_dts 1.51~2.50s）。
  采样盲区 9 段合计约 280s（68 分钟的 7%），落在盲区内的 >6s 缺口会漏。— live-ops-engineer-zone 2026-11-09

- [P2] [INFO] **「观察区没有数据」已完全闭环（11-09 10:01）**：`storage_paper/qmt.db` 现为 220 KB，
  `equity_snapshots=31 / sector_recommendations=441 / signals=1 / orders=1 / fills=1`，仍在写入。
  唯一那笔是 **09:30:52 `manual_entry(观察篮·绕过信号闸门)` 510050.SH 500 股 @2.905**（`mode=paper`，`_NoTradeBroker` 内存撮合）
  ⇒ **不是 `passorder` 验证**，实盘落单链路仍为未验证项。
  — 证据：`sqlite3 storage_paper/qmt.db` 只读查询 — 发现于主理人 2026-11-09

- [P2] [VALIDATED] **「闸门卡点时间线是市场属性」被今日实盘数据证实**：两区 `gate_timeline.jsonl` 今日（2026-11-09）各写一行，
  除 `source`（live/paper）与 `origin_zone` 外**逐字段相同**：`universe_n=16`、`above_ma20=7`、`above_ma60=1`、
  `macd_ok=11`、`mom_ok=0`、`best=002415.SZ(passed_n=5, blocked_key=mom_ok, mom60=-0.0587)`。
  ⇒ 昨晚「同一固定宇宙 + 同一份日线 ⇒ 两区应算出一模一样的行」的论证成立，分库只影响累积起点，不影响内容。
  — 证据：`logs/gate_timeline.jsonl` 与 `logs_paper/gate_timeline.jsonl` 尾行对照 — 发现于主理人 2026-11-09

- [P1] [DISPUTE] **不采纳 live-ops-engineer-zone 对 `WinError 183` 多写者假说的撤回**：他撤回的依据是「只有 1 个 QMT 实例」，
  但那句话是我 09:19 **已经公开收回**的判断，不能再当前提。`pid` 恒定 **不能**证明单写者 ——
  同一进程内多个策略条目各起线程写同一 `heartbeat.json`，pid 依然只有一个；今日 4 次 `init`、策略名后缀全不同
  （`00030010 / 0003003 / 00030016 / 30030823`）正说明同一进程内被反复加载。
  ⇒ 该假说维持「未定」，唯一判定手段仍是 OWNER 在 QMT 界面看 `_M_QMTIDE_BRIDGE*` 条目数。
  — 证据：`boot.log` 四次 init 原文 — 主理人 2026-11-09

### 2026-11-09 10:08 【P0 升级】`WinError 183` 盘中复现，已造成真实断线（根因改写）

- [P0] [BUG] **`result.json` 原子写失败 → 查询超时 → 通道被判断开 → 重订阅 38 只 + 重同步持仓。今天 10:03 已发生一次。**
  ```
  C://pazq_qmt_bridge//boot.log
  [10:03:24] pid=17080 result write failed: [WinError 183] result.json.tmp -> result.json
  logs/quant_system.log
  [10:03:34] WARNING core.broker_bridge: 桥接超时 action=QUERY_TRADE（10.0s 无 result）
  [10:03:37] INFO 券商重连成功：已重发行情订阅(38 只)并同步持仓   broker_reconnects 1→2
  ```
  **根因（非多写者）**：引擎侧 `core/broker_bridge.py::_read_json` 用普通 `open()`，Windows 下不共享 `FILE_SHARE_DELETE`；
  桥侧 `qmt_bridge/qmtide_bridge.py:254-267` 的 `os.replace(tmp,path)` 要求目标文件所有打开者都给 DELETE 共享 ⇒ 撞上即 183。
  调用密度极高：`_request()` 每次先 `connect()`→`_heartbeat_age()` 读 heartbeat（失败还 sleep 0.01 重读一次），
  等 result 时每 50ms 读一次 result.json。
  **不对称**：引擎侧写 signal 的 `_atomic_write_json` 已有 8 次退避重试；桥侧写 result/heartbeat **一次重试都没有**。
  另：`:259-267` 的 `except` 回退是 `os.remove(path)` + `os.rename`，非原子且再失败时**已删掉有效 result.json**，应删除该回退。
  — 证据：两份日志原文 + `boot.log.1` 08:48:52 heartbeat 183（pid 19364，同一机制的另一受害文件）— 主理人 2026-11-09

- [P0] [OPEN] **重复下单风险待证**：回执丢失 ≠ 未成交。若 `place_order` 超时后引擎用**新 signal_id** 重发，桥侧幂等失效 ⇒ 可能重复下单。
  已派 release-engineer-zone 只读取证（要求给 file:line，不接受推断），结论未回。
  — 主理人 2026-11-09

- [P2] [UPDATE] **QMT 策略条目数一条从 P0 降级**：183 已由「读写共享模式竞争」完整解释，**单写者即可复现**，
  不再需要「多个 `_M_QMTIDE_BRIDGE*` 并存」这个假说。条目数仍值得看一眼（多条目会放大 `init` 频率与心跳空洞），
  但已不是今天唯一未闭环的解释。同时**主理人收回 09:5x 对多写者假说的坚持**。
  — 主理人 2026-11-09

- [P2] [DECISION] **`bridge_probe.py` 不批准执行（含午休）**：单槽文件桥 + 引擎实测每 1.5s 连发三条 QUERY
  （`boot.log` 10:03:02/03/03 三条连排）⇒ 探针写入必然与引擎互相覆盖，探针只会超时，且会让引擎多误判一次断线。
  另：`qmtide_bridge.py:577` 的 `_attr(a, ["m_dFrozenCash","m_dFrozen"], 0)` **字段缺失即回落 0**，
  ⇒ frozen 补丁在字段不存在时自动退化为现值，**不需要前置 DUMP 验证**，验证法随之降级。
  — 主理人 2026-11-09

- [P0] [UPDATE 应答上一条「重复下单风险待证」] **机制已闭证，且真实主路径不是超时，是 `ok=True` + `order_id=None`。**
  ① 幂等确实失效：`core/broker_bridge.py:23` 与 `:57` 两句注释声称"重发复用同一 signal_id"，而 `:213 sid = uuid.uuid4().hex`
  每次请求都新建 ⇒ 引擎层任何重发在桥接看来都是全新一笔（`:978-982` 的跨重启黑名单也拦不到）。
  ② 更常见的重复路径完全不经过 `ok=False`：QMT 侧 `qmt_bridge/qmtide_bridge.py:804` 在 passorder 成功后返回 `{"order_id": None}`
  （该文件 `:769` 自述 "usually None"）⇒ `place_order:412-416` 反查 3s 若查不到 ⇒ `:419` 返回 **`ok=True, order_id=None`**
  ⇒ `event_engine.py:3607` 判定成功、`:3616` 建乐观仓、`:3627-3629` 只打 warning 不登记跟踪
  ⇒ 下一轮 `:4390-4397` 以券商为准把这个幻影仓归零 ⇒ 信号重新出现 ⇒ **第二次 passorder**。
  **触发条件正是「资金不足 / 涨停无对手盘 / 柜台静默废单」——最常见的三种拒单都会走这条路。**
  ③ 更坏一格：`place_order:413` 的 `before is not None` 意味着下单前 `QUERY_ORDER` 失败时**反查被整体跳过**，
  必然落进 ②。桥一抖，每一笔买入都变幻影。
  **要 OWNER 知道的一句**：这不是理论风险，是"明天开盘就可能发生、且发生时账面上完全看不出来（只留一行 warning）"的形态。
  **今晚已批准的对策（B2d，不含守卫）**：`place_order` 增加 `submitted` 三态（True 只在真拿到 oid 时给），
  超时/无 oid 统一落 `None` + 可 grep 的 `reason`；**BUY 侧延后到后天**（先跑一天看这条路径的真实频次）。
  — 风控审计官 魏守线 2026-11-09

- [P0] [FORBIDDEN] **今晚禁止在 `engine/event_engine.py` 给 SELL 侧加任何"在途/丢回执"守卫。**
  理由（已量化，非原则）：300308 的 −18%（411.286）是豁免仓**唯一**的自动出场路径，靠 `:2286-2304` 每轮重试。
  一旦被守卫按 code 封住，价格滑到 **353.863** 就触发 −20% 熔断且**仓仍在手上**：
  从止损价再亏 **5,742.30 元 = 剩余回撤空间的 51.13%**（峰值 60,568.49，10:41 权益 59,684.49）。
  而它要防的重复买入最坏只有 **1,552.91 元**（8,627.27 上界 × −18%）。**保护 : 代价 = 1 : 3.70，且失败方式是止损失效。**
  — 风控审计官 魏守线 2026-11-09

- [P0] [SAFE] **B2d（通道三态区分）今晚可上，条件已写明**：`signal.json` 的 payload 字段集合必须逐字节不变；
  `_request` 既有调用点返回值不得变化；`submitted` 不得用 `False` 表示"不知道"；不得顺手收紧 `_attribute_order_id` 的松匹配；不得动 `qmt_bridge/`。
  另：B2d 会在超时路径追加一次反查，**单轮最长由 3.015s 拉长到约 20s**（`get_orders` 自身超时 10s，`_attribute_order_id:353` 的 deadline 只在循环顶部检查）
  ⇒ 该轮行情/退出检查被跳过 6~7 轮。已要求实现压成"最多一次尝试"。**这不是零成本改动，只是成本远小于收益。**
  — 风控审计官 魏守线 2026-11-09

- [P0] [SAFE 附加条件] **`_daily_trade_count` 口径改为 `submitted is True` 必须与「幻影退避冷却」同批上，单独上不批准。**
  原因（量化）：现行 `if res.get("ok")` 会把 `ok=True, order_id=None` 的幻影单也计数 ⇒ 30 秒内刷满 `max_daily_trades=10`
  ⇒ 10-30 那次闸门关 36 分 05.8 秒。但**单把计数改成不数幻影**，幻影重试就从"30 秒后被拦住"变成"每 3 秒一次、全天不停"
  （约 4,800 次/日：4,800 次 `signal.json` 写入 + 4,800 行 `orders`，因为 `:3799` 在 ok 判断之前落库）
  ⇒ 单槽文件桥被打满 ⇒ `get_positions` 返回 None ⇒ `_risk_asset_basis=untrusted` ⇒ `asset_untrusted` 把所有新开仓关掉。
  **即：从"30 秒后显性关闸"变成"5 小时后隐性关闸"，且日志里看不出根因。**
  附带条件：判据必须写 `res.get("submitted", True) is True`（默认 `True`），否则 `QMTBroker`（无该键）下计数恒不增、闸门整体失效。
  — 风控审计官 魏守线 2026-11-09

- [P0] [FORBIDDEN] **幻影重试的限幅器不得实现为「同 code 同方向每天最多 N 次」。**
  必须是有时间属性、会自解的退避（30s→60→120→240→300 上限）。无时间属性的计数上限一旦用完，
  就会把 300308 的 −18%（411.286，豁免仓唯一自动出场路径）**永久封死**——即我们已否决过的那条止损失效，只是换了名字。
  — 风控审计官 魏守线 2026-11-09


- [P0-闭合] **桥接写失败 WinError 183：根因已闭证，修复已上线并验收通过（11:24:58 写盘 / 11:37:24 重载）。**
- 以下各块合起来才是完整结论，缺一块就会重犯「把次生错误当根因」的老毛病。

  **① 三步因果链（此前一直查错方向）**
  `os.replace(tmp, result.json)` 失败（WinError 5，被 `except Exception:` 无声吞掉）
  → 回退 `os.remove(result.json)` 失败（PermissionError，被 `except: pass` 吞掉）
  → 回退第二步 `os.rename(tmp, result.json)` 撞上仍存在的目标 ⇒ **`FileExistsError [WinError 183]`**。
  **日志里那句 183 是第三步报的，不是 `os.replace` 报的。** 前三步中真正该被看见的两个错误全被静默吞掉，
  ⇒ 过去所有排查都在追一个"只出现在错误链末端"的错。
  实测补充（不改结论）：给足 `FILE_SHARE_DELETE` **也不能**让 `os.replace` 在目标被任何进程持句柄时成功
  （本进程/跨进程一律 WinError 5）；共享位只能让 `os.remove` 通过。所以**主修只能是写侧重试**，
  读侧共享位只用于堵住那条次生链的入口。至于 `os.replace` 为什么在给足共享位时仍失败（Defender / 索引器 / QMT 自身瞬时占用）
  —— **明确列为已知未知，不深挖**：8 次退避共 0.72s 的语义本来就是"不管谁短暂占着，等它松手"。

  **② 今日 183 与断线 4/4 一一对应（不是"偶发噪声"）**

  | # | 183 时间戳 | 当日 `broker_reconnects` | 备注 |
  |---|---|---|---|
  | 1 | 10:03:24 | 1 → 2 | 盘初 |
  | 2 | 10:23:33 | 2 → 3 | +20 min |
  | 3 | 10:38:41 | 3 → 4 | +15 min |
  | 4 | 11:27:30 | 4 → 5 | +49 min（**修复未生效期**，见下） |

  **4 次写失败 ⇒ 4 次重连，一次不落。** 机制：写失败 ⇒ `result.json` 没更新 ⇒ 引擎等结果超时 ⇒ 重连。
  第 4 条 11:27:30 的日志原文（`result.json.tmp` → `result.json`，正是上面的 `os.rename` 那一步）：
  ```
  [2026-11-09 11:27:30] result write failed: [WinError 183] 当文件已存在时，无法创建该文件。:
                        C:/pazq_qmt_bridge/result.json.tmp -> C:/pazq_qmt_bridge/result.json
  ```
  这条特别有价值：11:24:58 补丁版文件**已在盘上**，但 OWNER 尚未重载 ⇒ 进程跑的仍是旧代码 ⇒ **旧代码又失败了一次**。
  这是天然的"修复前"对照，也再次证明**只改文件不重启 = 没上线**。
  另：间隔为 20 / 15 / 49 分钟 ⇒ **阵发型，不是定时器**。所以**不能**用"重载后 N 分钟没出现"当主判据。

  **③ 上线验收四项全 PASS（11:37:24）**

  | # | 判据 | 实测 | 结果 |
  |---|---|---|---|
  | 1 | `heartbeat.rev` 变新值 | `2026-11-09T1125-write-retry` | PASS |
  | 2 | 出现第 2 条 `init done` 且 rev 为新值 | `11:37:24` 新条 rev=新值；旧的 `10:53:27` 留在 `boot.log.1` | PASS |
  | 3 | 重载后新增 183 = 0 | `boot.log*` 仅 `11:27:30` 一条，早于重载时刻 | PASS |
  | 4 | `broker_reconnects` 不再 +1 | 仍为 **5** | PASS |

  附带三条硬旁证（不是从心跳倒推的）：
  - `python=3.6.8 (default, Dec 19 2022) [MSC v.1900 64 bit]` —— QMT 内嵌解释器**实测**，
    补丁版经它编译并跑通 `init done`。此前我们只能做静态扫描，措辞只能写"无 3.6 不支持语法"；现在可以写"实测兼容"。
  - `poll #1 alive` → `poll #2 alive` —— `run_time 500nMilliSecond` 定时器真在跑。
  - `[IO] ...QMTIDE_BRIDGE.py -> ctypes_ok=48066` —— 桥自己把部署版读成 **48066 字节**，
    与写盘字节数逐字节吻合，证明进程读的就是新文件。
  - `seen 已恢复 200 条（跨重启幂等）` —— 重启后幂等去重表仍在。

  **④ 顺带关掉一个悬着的 P2**：本次重载 `boot name='_M_QMTIDE_BRIDGE_30030831'` 只起了 **1 个**条目，
  未见同名重复启动 ⇒ "策略列表里多个残留条目互抢 signal" 这条至少在本日重载现场**不成立**。
  原"待 OWNER 数条目"可结案。

  **⑤ 还没做、留给收盘后的**：驱动脚本 `scripts/apply_b1_20261109.py` 的 `find_backup` 用 mtime 判"刚生成的备份"，
  而备份用 `shutil.copy2`（**连 mtime 一起复制**）⇒ 刚生成的备份被判成老文件 ⇒ 自检 1 假红，
  且**回滚分支调同一个 finder，最需要它的时候是哑的**（这次它想回滚但找不到备份，等于什么都没还原，
  万幸补丁本身是对的）。已定位、已记录，**收盘后修 + 补守卫测试**，13:00 开盘前不动工具代码。
  — 交付与版本工程师 毕提交 2026-11-09

### 2026-11-10 11:58 盘中发现（主理人）

- [P1] [SAFE] **行情从 mock 恢复后不会重建日线特征** ⇒ 启动早期行情未就绪的进程会带着合成 MA/动量/宽度跑一整天。
  证据：`core/qmt_client.py:82-95` 的 mock 生成器（`c = o*(1+random.uniform(...))`，基准价任意）；
  `engine/event_engine.py:617 _reattach_market_data()` 只切换实时报价源，未触发 `daily.refresh(force=True)`；
  实测 paper 今日 09:19 `data=mock` 启动 → 09:19/09:30 两次「日线上下文刷新完成 n=40」用的都是合成序列 →
  11:58 接口实测 paper `300308.SZ close=122.22`，而新拉真实日线 = **452.03**（同一台机、同一份代码、两个 zone 都拉到 452.03）。
  后果：闸门逐只明细/动量/宽度全部失真；若发生在有持仓且需靠 MA60 破位退出的日子，会据假数据做退出决策。
  live 今日 09:30–11:35 同样被污染（`trend_up 13` / `动量通过 1` 均为合成数据算出），是 11:35 重启洗掉的，非机制修复。
  建议修法：`_reattach_market_data()` 成功切到真实源后强制 `daily.refresh(codes=..., force=True)`。
  影响面：paper 面板全部不可信；live 仅在"启动早于行情就绪"的交易日受影响。

- [P1] [SAFE] **每次引擎重启都会强制重选动态候选池** ⇒ 同一交易日上下午的池子可以不同，策略结论不可横比。
  证据：`engine/event_engine.py:786 self.dynamic_universe.refresh(force=True)`；
  实测 2026-11-10：09:24 池 vs 11:35 池 交集仅 **20/30**，移出 10 只全为 688 科创板、新增 10 只全为主板/创业板；
  随之 `universe_n 43→37`、`trend_up_n 13→3`、`mom_pass_n 1→0`、`score_ok_n 25→7`。
  处置：OWNER 2026-11-10 裁定 **A（不冻结）**，今日不修；待办是把 `refresh(force=True)` 改为「同一交易日只刷一次」。

### 2026-11-10 12:15 下单前排查（主理人）

- [P1] [SAFE] **同代码分笔建仓被券商合并成一个持仓 ⇒ 引擎丢失「当日新仓」的 T+1 语义**。
  现场：`engine_state` 显示 `510050.SH qty=600 avg=2.893 open_date=2026-11-03T09:30:21`，
  而其中 **100 股是 2026-11-10 手工买入**（`storage` 内今日 `orders=0 / fills=0`，即非经引擎成交，
  仓位与均价来自 `_sync_broker_positions` 的券商回灌）。
  后果：引擎认为 600 股全部可卖（open_date 为 11-03），实际券商侧只有 500 股可卖；
  若下 `SELL 600`，券商整单或部分拒单，`_handle_sell` 的"清仓"语义与真实成交不一致。
  当前不阻断：OWNER 指令单 `max_amount=1000`（510050 约 290 元/手），本日只会测 100 股，券商侧可满足。
  建议修法（收盘后）：`_sync_broker_positions` 对"券商数量 > 本地已知数量"的差额单独记一个
  `open_date=today` 的影子批次，卖出可用量 = 券商数量 − 当日新增批次；或直接按 `fills` 分笔维护批次。

- [P1] [SAFE] **OWNER 指令单加仓会把整仓 `open_date` 重置为当日 ⇒ 同日老仓也卖不掉**。
  现场 2026-11-10 13:04:40：510050.SH 原 600 股（`open_date=2026-11-03`，券商侧当日可卖 500 股）
  加仓 100 股成交后，`Position.open_date` 被写成 `2026-11-10T13:04:40` ⇒ `is_t1_locked` 对**整仓 700 股**
  判锁定 ⇒ 当日任何卖出请求都会被 `run_step` 的 T+1 预检拒掉（`core/owner_order.py:339-347`）。
  券商会拒绝卖当日买入部分，所以不构成违规下单；真正的损失是"老仓也动不了"，
  若当日暴跌需要靠止损离场，这条持仓将无法退出。
  附注：`target_price=28.97`（=现价×10）**不是 bug**，见 `engine/event_engine.py:3578` 的 `# 实质不触发`。
  建议修法（与 12:15 那条合并做）：按 `fills` 维护分笔批次，`open_date` 取各批次加权/最早可用批次，
  卖出可用量 = 券商数量 − 当日新增批次。

- [P0] [SAFE] **OWNER 指令单的 `lots` 在卖出方向被完全忽略 ⇒ 要卖 1 手实际卖了 5 手**（真实资金已发生）。
  现场 2026-11-10 13:52：指令 `{"side":"SELL","lots":100}`，实际 `orders#1108 SELL 510050.SH 500股 @2.898`
  → `fills#393` 成交 500 股 1,449.00，持仓 800 → 300，实现亏损约 −4.45 元 + 双边佣金 10 元。
  根因：`engine/event_engine.py:3888 qty = int(pos.quantity)`，只按券商可用量夹紧，
  **没有接受"这次卖多少"的入参** —— `_handle_sell` 的语义一直是"清仓该持仓"，
  而 OWNER 通道按"指定股数"理解。修法：给 `_handle_sell` 加 `lots: int|None = None`，
  None 保持清仓语义（所有既有调用点不变），OWNER 通道传 `qty`。
- [P1] [SAFE] **OWNER 通道把 `_handle_sell` 的 False 当成"卖出失败"，但 live 分支成功下单后也返回 False**。
  依据 `engine/event_engine.py:3932-3933` 的注释："卖出委托已提交但尚未成交：…返回 False 调用方据此不补位"。
  于是 13:52 那笔**下单成功**的卖单在 `owner_orders_done.json` 里被记成
  `reject / 落单口未成交（保护期/委托失败/零仓位）` —— 账本与事实相反，且 `max_per_day` 未计数。
  修法：给落单口加一个"是否真的提交了委托"的显式回传（如 `self._last_placed_order`），
  而不是靠布尔返回值推断。

### 2026-11-10 收盘落地（主理人）

- [P0-已落地] **峰值基线不再被熔断恢复下移（+4,485.95 元回撤预算）**
  根因不是 README #3 说的「重启归零」，是 `risk/manager.py:352-353` 在**任何类型**熔断
  解除时把 `_peak_asset` 重置为当前总资产。实证（`risk_snapshots`，两次都在 09:30:1x）：
  11-03 66,542.31→63,207.94（−3,334.37）、11-09 67,453.62→58,162.49（−9,291.13）。
  修复：峰值**只上不下**；「解除即再熔断」改由 `_dd_rearm_floor` 承担（解除时记当前权益，
  再跌破才熔断），随 `risk_state` 持久化。存量值已由 61,846.18 修正为 **67,453.62**，
  −20% 硬线 49,476.94 → **53,962.90**。备份 `storage/qmt.db.bak-peakfix-20261110-150407`
  （`sqlite3.backup()` 一致快照，**不能** copy2 单拷主库 —— WAL 里还有最近提交）。

- [P0-已落地] **未知态委托（`ok=True` 但无 `order_id`）登记占位 + 按墙钟自解**
  `_handle_sell` 原实现只 `logger.warning` 就 `return False` ⇒ 同一退出信号每轮重发。
  新增 `_register_unknown_order` + `_expire_unknown_orders`；**过期扫描必须在
  `get_trades` 之前**（`if trades is None: return` 会让扫描永不执行 ⇒ `_pending_sells[code]`
  永久占住 ⇒ 连 `force=True` 强平都被拦）。集成测试已验：11 条守卫全绿。

- [P1-已落地] **宽度闸门口径对齐回测（第 20 例「回测有、实盘无」）**
  生产数的是 momentum top_n 截断后的 BUY 数（上限恒 3，`mom_pass_n<=1` 时恒 1 ⇒ K=2
  必然触发；今日 688981 被挡 24 次）；回测数的是**全宇宙**通过入场门的票数。
  已改 `pool_all` 快照 + 全量池计数 + 判据切 `_breadth_n`；回滚开关
  `STRATEGY_PARAMS["breadth_gate_basis"]="truncated"`。
  价签：四窗收益差均值 **+11.56pt**、四窗全正、window t=+7.12（Calmar 幅度不可引用）。

- [仪器缺陷-已登记] **逐折 Calmar 的小分母会把均值撑爆**（某折 MDD 3.8% ⇒ Calmar 236.89）。
  OWNER 今日把 Calmar 定为验收口径，后续提案应改用「四窗 Calmar 符号全正 + 逐折中位数」
  或 window 级收益差 t 值，否则任何提案都能靠「让某折 MDD 变小」通过。

- [P0-待办·最优先] **假 `daily_stop` 触发强平（今日 14:17 实盘发生）**
  14:17:21 买 510500 500 股（3,347 元）→ 14:17:36 总资产少算一个持仓（资金已扣、持仓未刷新）
  → 读出 `daily_stop −7.04%`（真实约 −1.7%）→ 置 `_flatten_requested` 并熔断，随后每 4.5s
  空转一次强平到收盘（实测 14:18 后 `place_order` 为 0，未再造成新损失）。
  去抖补丁（连续 3 轮确认）已写进 `scripts/patch_20261110_v2.py::V7` 且锚点通过，但
  `ENABLE_V7=False`：它会改风控底座语义并打破 4 条既有测试（含
  `test_risk_asset_trust.test_positive_control_old_path_still_halts` 正向对照）。
  **下一轮头号任务**：更新那 4 条测试 + 定确认轮数 + 补双向守卫，然后启用。
  另：13:52:30 的 `SELL 510050 500` 是否为更早的同类假信号所致，**尚未取证**。

- [观测纠正] 今日 4 笔 live 委托的 `order_id` **全部为真值**
  （0165885057/0165954023/0165958027/0165315089）⇒ 桥侧 `_attribute_order_id` 可用，
  「ok=True 但无 order_id」**不是**默认结局，未知态占位只是兜底。
  此前「8 天 live 经桥委托 0 笔」的判断今日被推翻。

- [验收] 全量回归 **1112 通过 / 36 失败 / 1148 例**（落盘前沙箱同构基线 1101/36/1137；
  失败数未上升，新增 11 例全部是新守卫且全绿）。七项验收 A1~A6 全 PASS，
  A5（宽度日志出现 `口径=universe`）需下一交易日开盘后触发才可见。
  运行版已含本批补丁（引擎启动 15:04:12 > 全部源码 mtime）。

- [待 OWNER 裁决] `risk_per_trade` 0.040→0.030（Calmar 23.68→26.31、IS MDD −12.95%→−10.58%，
  代价 IS 收益 −28.4pt）。详见 `reports/EXPOSURE_PROPOSAL_20261110.md`。
  在 300308 清仓前不建议动它 —— 一手即占 78.43% 权益，降 rpt 救不了存量集中度。

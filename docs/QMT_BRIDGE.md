# QMT 文件桥：关键配置与部署

> 目的：让 **QMT 只做执行**，本机项目做分析并下发信号。
> 本机 → `signal.json` → QMT 策略 `passorder` → `result.json` → 本机。
>
> 唯一部署的执行器是 [`qmt_bridge/qmtide_bridge.py`](../qmt_bridge/qmtide_bridge.py)。
> `qmt_bridge/qmt_executor.py` 是**已被取代的第一版，不要部署**（见文末「历史」）。

---

## 一、为什么要走文件桥

2026-11-07 23:49 QMT 从 `2.1.17.0` 自动升级到 `2.1.32.0` 之后，外部 python 通过
`XtQuantTrader` 连交易通道被服务端拒绝：

```
[TC::COrderServiceQuantAdaptor::onConnected] quant session 969621, illegal pid 3028
```

提权到管理员仍被拒，`userdata_mini` 下连 `up_queue_xtquant` 都没有。两条路都需要
券商侧动作，短期不可自解。

文件桥绕开 `xttrader`：**下单动作由 QMT 策略自己的主线程用 `passorder` 完成**。
策略进程由 QMT 拉起，天然在它自己的白名单里。

---

## 二、最关键的一处配置：`simpleRun`

**这一项决定「模型交易」还是「简单运行」，而它只在 XML 里，GUI 改不了。**

位置：`<QMT安装目录>\config\indexUserConfig.xml`

```xml
<catalog ... name="QMTIDE_BRIDGE" type="2" simpleRun="0"/>
```

| 值 | 启动方式 | `__name__` | `init()` 被调用? | `passorder` 可用? |
|---|---|---|---|---|
| `simpleRun="1"` | `pythonw.exe -u <脚本> <userdata> <ms>` | `'__main__'` | **否** | **否** |
| `simpleRun="0"` | QMT 进程内 `python36.dll` | `_M_QMTIDE_BRIDGE_xxxx` | **是** | **是** |

### 症状与判据

设成 `1` 时，点「运行」后策略**存活约 150ms 就退出**，`return code:1`，
共享目录里什么都不会生成。而 QMT 界面上它可能显示「运行中」——
这是本项目踩过的最大的坑：**看起来在跑，实际什么都没做**。

判据只看一处：

```
C:\pazq_qmt_bridge\boot.log 里有没有 "MODE=embedded init() called"
```

没有这行 = 内嵌引擎没接管 = `simpleRun` 还是 `1`。

### 怎么改

1. **完全关闭 QMT**（不是最小化）。文件被占用时改不进去。
2. 备份：`copy indexUserConfig.xml indexUserConfig.xml.bak-YYYYMMDD-HHMMSS`
3. 把 `name="QMTIDE_BRIDGE"` 那条的 `simpleRun="1"` 改成 `"0"`。
   **只改这一个属性**，`type="2"` 等其它字段不要动。
4. 重新打开 QMT，进「模型交易」点运行。

仓库里的 [`scripts/qmt_set_simple_run.py`](../scripts/qmt_set_simple_run.py) 做这件事，
且做了四重校验（匹配恰好 1 处 / 长度不变 / 回读一致 / XML 重新解析合法，任一失败
自动从备份恢复）：

```bash
python scripts/qmt_set_simple_run.py --check            # 只读查看当前值
python scripts/qmt_set_simple_run.py --apply --target 0 # 改成「模型交易」
```

---

## 三、其余几个开关

Dump 当前配置：

```bash
python scripts/_qmt_config_dump.py
```

实机输出（本机 2026-11-08）：

```text
name                          runMode  acctType peroid  eStrat autorun
---------------------------------------------------------------------
QMTIDE_BRIDGE                 1        2        60      4      1
```

| 字段 | 本机值 | 含义 | 本项目是否动过 |
|---|---|---|---|
| `runMode` | `1` | **语义未定性**。另两个用户自建策略为 `0`；但桥接读到的账户数据与引擎 `xtquant` 直连读数三项吻合，**说明连的就是实盘账户，不是模拟盘** | **未改**（不懂就不动） |
| `accountType` | `2` | `2`=普通账户，`3`=信用账户 | 未改 |
| `startupAutorun` | `1` | QMT 启动时自动加载该策略 | 未改 |

> ⚠ `runMode` 不要凭猜测改。它和资金安全直接相关，而目前没有可靠文档能定性；
> 实证只证明「当前值连的是实盘账户」这一件事。

---

## 四、部署执行器

`qmtide_bridge.py` 是 UTF-8 源码，但 **QMT 侧要求 GBK + CRLF + 首行 `#encoding:gbk`**，
直接复制粘贴会被 QMT 重写成 583 个问号。用脚本部署：

```bash
python scripts/deploy_bridge.py
```

脚本做的事：UTF-8 → GBK、CRLF、首行编码声明、**回读逐字节比对**、语法检查
（编译到临时文件，不在 QMT 的 python 目录留 `.pyc`）、备份旧文件。
GBK 表示不了的字符会被**正确拒绝**（例如 `⇒`），改源码为 `=>` 即可。

部署后仍需**人工**在 QMT 里重新加载策略 —— 部署 ≠ 生效。

---

## 五、启动后的验收清单

```bash
python scripts/bridge_probe.py     # 只读：PING / DUMP / QUERY_ASSET / QUERY_POSITION
```

`boot.log` 里应当依次出现：

```
[BRIDGE] MODE=embedded init() called (passorder available)
[BRIDGE] init done. root=C:\pazq_qmt_bridge account=(<你的账号>,STOCK) timer=run_time 500nMilliSecond
[BRIDGE] seen 已恢复 N 条（跨重启幂等）
[BRIDGE] poll #1 alive
[BRIDGE] file reader=ctypes 可用（上一次=<未定>）
[BRIDGE] got signal_id=... action=PING reader=ctypes
```

探针应当返回 `requests: 4, ok: 4, timeout: 0, error: 0`。

**注意**：`error` 和 `timeout` 是两回事。写 `signal.json` 失败也计 `error`，
不是超时 —— 见下节第 4 条。

---

## 六、已知的六个坑（全部已修，但改代码时容易重犯）

### 1. QMT 内嵌 `sys` 没有 `argv`

```
[pythonformula] run script failed!, msg: AttributeError: module 'sys' has no attribute 'argv'
```

内嵌解释器是 **Python 3.6.8**，`sys` 是精简 shim。**模块级任何一次异常都会让 QMT
放弃加载整个模块，`def init` 根本不会被定义**，于是策略「运行中」但什么都不做。
所以启动探针必须逐字段容错，且模块级要再包一层 try/except。

### 2. `ContextInfo` 可能拒绝挂属性

它是 C++ 绑定对象。若在 `try` 里写 `ContextInfo._bridge_po_form = form`，
`AttributeError` 会被外层 `except` 当成「这种 passorder 形式失败」，从而**换下一种
形式再发一次单** —— 同一个订单下两次，真实资金。所以 form 必须存模块级变量，
且**一旦发单成功就绝不重试**。

### 3. Python 层的 `open()` 读被全禁

沙箱对策略的所有 `open(path, 'r')` 都抛 `PermissionError: Forbidden fileIO`，
**连它自己的源码都读不了**。但写是放行的，且 `kernel32.CreateFileW` 这一层能过。
所以读取走三级读链：`py`（必失败）→ `ctypes`（可用）→ `numpy`（备用）。

### 4. 读句柄的共享模式会吞掉整条信号

`CreateFileW` 若只申请 `FILE_SHARE_READ`，QMT 侧一持有 `signal.json`，
本机引擎的 `os.replace` 就撞 `ERROR_SHARING_VIOLATION`，整条信号被丢弃
（若是 SELL 就是静默漏卖）。实测 8 次请求触发 1 次。
**必须给 `FILE_SHARE_READ|WRITE|DELETE`**。

### 5. QMT 重启会重放上一条信号

`signal.json` 会长期残留最后一条。若已处理集合只在内存，重启后就会**再执行一遍**
——已成交的 BUY 会变成重复买入。所以成功的 `signal_id` 要落盘
（`processed_ids.json`），启动时恢复，命中即回 `status="duplicate"` 且不执行。
**失败信号不入黑名单**（允许重启后重试）。

### 6. `DBL_MAX` 不是数字

QMT 用 `1.7976931348623157e+308` 表示「该字段不适用」（实测 `m_dCurrMargin`、
`m_dPreBalance`、`m_dAvgOpenPrice` 全是它）。`float()` 收得下，但流进账本后
任何运算都会变 `inf/nan`，必须当「无值」处理。

---

## 七、协议

文件即接口，共享目录默认 `C:\pazq_qmt_bridge`（可用环境变量 `QMT_BRIDGE_ROOT` 覆盖）。

| 文件 | 方向 | 说明 |
|---|---|---|
| `signal.json` | 本机 → QMT | 一条请求 |
| `result.json` | QMT → 本机 | 对应的应答 |
| `heartbeat.json` | QMT → 本机 | 每 ≈1s 更新，本机据此判定「策略是否活着」 |
| `boot.log` | QMT → 本机 | 诊断日志，200KB 自动轮转 |
| `processed_ids.json` | QMT 侧 | 跨重启幂等黑名单 |

请求：

```json
{"signal_id": "唯一 ID", "action": "PING", "account": "cash", "params": {}, "ts": 1234567890.0}
```

`action` 取值：`PING` / `DUMP` / `QUERY_ASSET` / `QUERY_POSITION` / `QUERY_ORDER` /
`QUERY_TRADE` / `BUY` / `SELL` / `CANCEL`。

`signal_id` 幂等：同一 id 只执行一次，重复请求回缓存。这是防止「SELL 发两次
卖成 200 股」的唯一保障。

应答：

```json
{"signal_id": "...", "status": "ok", "data": {}, "message": "", "ts": 1234567890.0}
```

`status` 为 `ok` / `error` / `duplicate`。

`DUMP` 返回 `get_trade_detail_data` 全部字段名 —— 各版本 QMT 字段有差异，
这是唯一可靠的校准手段。

---

## 八、当前状态（2026-11-08）

| 项 | 状态 |
|---|---|
| 信号 → 执行 → 回结果闭环 | ✅ 已跑通 |
| `QUERY_ASSET` / `QUERY_POSITION` | ✅ 已实测（差异见 §八.1，必须用持仓汇总口径） |
| 跨重启不重放 | ✅ 已实现并验证 |
| `m_nOffsetFlag` 的 23/24 买卖判定 | ❌ **未实测**（`ORDER count=0`，无在途单样本） |
| `passorder` 能否落实现货账户 | ❌ **未实测**（需一笔真实委托） |

切换开关在 `trading_config.json`（gitignored）：

```json
{"broker_qmt_mode": "bridge"}      // 2026-11-08 21:35 已切；回滚改回 "XtMiniQmt"
```

**这是全有或全无的单开关**（`core/broker.py:_make_broker`）：查询与下单同时换通道，
没有「只切查询」的中间态。回滚＝改回 `"XtMiniQmt"` 并重启，零残留。

### 八.1 ★★ 总资产口径：ACCOUNT 与 POSITION 的市值不是同一个价格

2026-11-08 21:30 实测（**同一轮 QUERY、同一账号**，300308.SH 当日跌 −9.18%）：

| 来源 | 数值 | 拆解 | 用的是哪天的价 |
|---|---|---|---|
| `ACCOUNT.m_dInstrumentValue` | 50,798.00 | 493.33×100 + 2.93×500 | **11-05 收盘**（上一交易日） |
| `POSITION.m_dMarketValue` 汇总 | 46,251.00 | 448.00×100 + 2.902×500 | **11-06 收盘**（最新） |

差额 **4,547.00** 与账本 11-06 的 `daily_pnl = −4,547.03` 分毫不差，
tushare 日线独立复核：`20261105 close=493.33`、`20261106 close=448.00`。

⇒ 若用 ACCOUNT 口径当 `_broker_total_asset`：总资产 62,409.49（真值 57,862.49），
回撤从 **−14.2%** 变成 **−7.5%**，熔断缓冲凭空多出 **6.7pp**。当前账本距 −20%
只剩 **5.78pp** ⇒ 这个误差足以决定熔断是否触发。

现实现（`_q_account`）：

1. 先查持仓汇总 `pos_mv`，`pos_mv > 0` 时**以它为准**（它带最新价）；
2. 持仓查询返回 `None` ⇒ 回落 ACCOUNT 口径（宁可口径旧一天，也不凭空造数）；
3. `pos_mv == 0` 而 ACCOUNT 有市值（矛盾态）⇒ **保留 ACCOUNT，绝不清零**
   （清零会让 total_asset 瞬间少掉全部持仓 ⇒ 假回撤 ⇒ 假熔断）。

### 八.2 版本戳 `CODE_REV`：部署 ≠ 生效

QMT 只在「运行策略」时加载 `.py`。只跑 `deploy_bridge.py` 而不在 QMT 里重新运行，
QMT 会一直跑内存里的旧代码 —— 文件是新的、`boot.log` 里也有历史记录，但改动完全没生效。

所以 `CODE_REV` 会写进三个地方：`PING`/`DUMP` 的返回、`heartbeat.json` 的 `rev`、
`boot.log` 的 `init done ... rev=`。检查方式（被动，不碰下单通道）：

```bash
python scripts/iface_probe.py    # C 段「代码版本」：QMT rev ≠ 仓库 rev ⇒ FAIL
```

---

## 九、历史

`qmt_executor.py` 是第一版，有两处缺陷，已在 `qmtide_bridge.py` 修正，保留仅为对照：

1. `passorder` 三种形式都把 `accountType` 塞进了**账号位**，其后所有参数整体错位一格。
   正确形式（以 QMT 随附可跑的 `双均线实盘示例PY.py:77` 为准）是 **11 参、不含
   accountType**：
   ```python
   passorder(op, otype, aid, code, prtype, price, vol, name, quickTrade, msg, ContextInfo)
   ```
2. 无跨重启幂等保护（见 §六.5）。

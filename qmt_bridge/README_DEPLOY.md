# 大 QMT 桥接：部署与排障

> 背景：QMT 2.1.17.0 → 2.1.32.0 之后，外部 python 经 `XtQuantTrader` 连接被服务端判
> `illegal pid` 拒连（提权也无效，`userdata_mini\up_queue_xtquant` 这个锁被 QMT 自己
> 占着）。桥接把下单动作交给 **QMT 策略内的 `passorder`**，不经过 xttrader。

---

## 一、两个文件别搞混

| 文件 | 角色 | 状态 |
|---|---|---|
| `qmt_bridge/qmtide_bridge.py` | **当前部署的那份**，源文件（UTF-8） | 已部署为 `C:\pazq_qmt\python\QMTIDE_BRIDGE.py` |
| `qmt_bridge/qmt_executor.py` | 更早的同设计实现 | **未部署**，仅留作参考；它的 `passorder` 参数顺序有已知缺陷（见 §五） |

仓库里**只维护 `qmtide_bridge.py`**。部署不要手工复制，用脚本：

```bash
python scripts/deploy_bridge.py --check   # 只校验编码/结构
python scripts/deploy_bridge.py           # 备份旧文件 + 写 GBK+CRLF + 回读校验 + 语法检查
```

**为什么必须用脚本**：QMT 编辑器只认 GBK。用 UTF-8 存过去的文件，QMT 会按 GBK 解码
失败，把**所有非 ASCII 字符换成 `?`**（实测 583 个问号、中文注释全丢），还会塞一个
`# -*- coding: utf-8 -*-` 头并转成 CRLF。部署脚本会：把首行改成 `#encoding:gbk`、
GBK 编码、CRLF、清掉旧 `.pyc`、回读逐字节比对。

---

## 二、「一点运行就停止」的真因（2026-11-08 定位）

QMT 日志（`C:\pazq_qmt\userdata\log\XtClient_*.log`）里是这样：

```
01:29:52,563 [TC::CTradeStrategyData::doRun] execude cmd:  -u "C:\pazq_qmt\python\QMTIDE_BRIDGE.py" "C:\pazq_qmt\userdata" 1794072592563
01:29:52,571 Monitor add pid:29596
01:29:52,722 [TradeStrategy]try stop : ID:14, fromula:QMTIDE_BRIDGE
01:29:52,722 doRun return code:1
```

存活 **159ms**。用 QMT 自带的 `bin.x64\pythonw.exe`（Python **3.6.8**）按同样命令行
实跑，退出码是 **0**，耗时 250ms。再用 ctypes 复刻 QMT 的
「`CreateProcess` + 无标准句柄」启动方式，退出码**仍然 0**
（复现脚本：`scripts/_repro_qmt_launch.py`）。

⇒ **不是崩溃，是脚本自己跑完退出了。**

原因是 QMT 用两种完全不同的方式执行 python 策略，而它们不等价：

| | A 内嵌引擎（模型交易） | B 独立进程（简单运行） |
|---|---|---|
| 启动方式 | QMT 进程内 python36.dll | `pythonw.exe -u <脚本> <userdata> <ms>` |
| `__name__` | 模块名 | `'__main__'` |
| `init(ContextInfo)` | **会被调用** | **永远不调用** |
| `ContextInfo.run_time` | 可用 | 无 ContextInfo |
| `passorder` / `get_trade_detail_data` | **注入模块全局** | **不存在** |
| `sys.stdout` | 有 | **None**（任何 `print` 都抛异常） |

本策略是给 A 写的（`init` + `run_time` 计时器）。QMT 却按 B 拉起来 ⇒ 从上到下执行
一遍、`init` 没被调用、`C:\pazq_qmt_bridge\` 里连 `heartbeat.json` 都没有、159ms 后
进程结束 ⇒ QMT 记 `return code:1`。

**判别依据**：`config/indexUserConfig.xml` 里策略目录项
`<catalog ... name="QMTIDE_BRIDGE" type="2" simpleRun="1"/>`。
同一目录下用户自建的实盘策略（`GLM5日内回转` / `信用账户测试DOUBAO` /
`日内回转加风险控制AMDLOCAL` / `尾盘闲置资金自动逆回购`）全是 `simpleRun="0"`；
只有 QMT 自带示例（`双均线实盘示例PY`、各 `*回测示例`、`测试脚本`）和本策略是 `1`。

---

## 三、现在的自证机制（部署后自带）

`qmtide_bridge.py` 在 **import 时**就往 `C:\pazq_qmt_bridge\boot.log` 写一段启动证据，
并接管 `sys.excepthook`（pythonw 会把 traceback 丢掉，所以必须落到文件）：

```
=== boot name='__main__' passorder=False stdout=None
argv=['C:/pazq_qmt/python/QMTIDE_BRIDGE.py', 'C:/pazq_qmt/userdata', '1794072592563']
cwd=... exe='C:\pazq_qmt\bin.x64\pythonw.exe'
python=3.6.8 ...
env={'PYTHONPATH': ...}
[BRIDGE] MODE=standalone, passorder NOT available -> this run can never trade; exiting ...
```

判定规则只有一条：

```
boot.log 里出现  "[BRIDGE] MODE=embedded init() called"  ⇒ 真的起来了
只有 MODE=standalone                                    ⇒ QMT 又用错了运行方式
```

用 A 方式起来时还会有一行：

```
[BRIDGE] init done. root=C:\pazq_qmt_bridge account=(<你的资金账号>,STOCK) timer=run_time 500nMilliSecond
```

`timer=UNAVAILABLE: ...` 表示该版本没有 `run_time`，靠 `handlebar` 兜底也能跑，
但刷新会跟随 bar 周期（建议周期设 1 分钟）。

---

## 四、部署后要做的（必须人工，GUI 点不了）

1. QMT 里确认策略的**运行方式不是「简单运行」**，而是**模型交易 / 实盘**。
   若界面找不到这个开关，改 `config/indexUserConfig.xml` 里该策略的
   `simpleRun="1"` → `"0"`（**必须在 QMT 关闭时改**，QMT 退出会用内存副本覆盖配置文件）。
2. 点运行，**立刻**看 `C:\pazq_qmt_bridge\boot.log`。
3. `boot.log` 出现 `MODE=embedded` ⇒ 再跑只读探针：

```bash
python scripts/bridge_probe.py
```

探针只做 `PING` / `DUMP` / `QUERY_ASSET` / `QUERY_POSITION`，**不下单**；`DUMP` 回传
本机真实的字段名，用来校准 `get_trade_detail_data` 映射（重点确认
`m_nOffsetFlag` 到底是不是 23/24，若不是 `_q_orders` 的 side 判定要改）。

探针全绿之后，才把 `trading_config.json` 的 `broker_qmt_mode` 从 `"XtMiniQmt"`
切成 `"bridge"`。

> ⚠ QMT 重启**不会**自动恢复策略，即使 `startupAutorun="1"` 写在配置里。

---

## 五、协议与已知边界

| 文件 | 方向 | 说明 |
|---|---|---|
| `signal.json` | 本地 → QMT | `{signal_id, action, account, params, ts}` |
| `result.json` | QMT → 本地 | `{signal_id, status, data, message, ts}` |
| `heartbeat.json` | QMT → 本地 | 每秒一次；超过 6s 未更新判为断开 |
| `boot.log` | QMT → 本地 | 启动自证 + 所有 `print` 的落地副本（`_log()`） |

- `signal_id` **幂等**：同一 id 只执行一次，重复请求回放上次结果 ⇒ 本地超时重发不会
  重复下单。这条由 `tests/test_qmtide_bridge.py` 钉住（26 用例）。
- `action`：`PING` / `DUMP` / `QUERY_ASSET` / `QUERY_POSITION` / `QUERY_ORDER` /
  `QUERY_TRADE` / `BUY` / `SELL` / `CANCEL`。
- 只下**指定价**：`price<=0` 直接拒（价格类型猜错会判废单）。
- **`passorder` 参数顺序**（2026-11-08 修正）：以 QMT 随附可跑示例
  `C:\pazq_qmt\python\双均线实盘示例PY.py` 为准，是 **11 参、不含 accountType**：
  ```
  passorder(op, otype, acct, code, prtype, price, vol, name, 1, msg, ContextInfo)
  ```
  早期实现把 `accountType` 塞进了账号位（`op, otype, atype, aid, code, ...`），
  后面全部参数错位一格 ⇒ 会在错误账号报单或直接废单。现在是 4 个候选形式按
  11/12/10/10 逐个回落，成功一次就记住。**`qmt_executor.py` 里还是老写法，未部署，
  要启用它必须先同步这个修正。**
- **委托号**：`passorder` 没有返回值。broker 侧下单前记 `QUERY_ORDER` 基线、下单后
  3s 内按「代码+方向+数量」反查。反查不到会打 `ERROR` 并返回 `order_id=None`
  ⇒ 引擎无法跟踪成交，**下一轮可能重复下单**。看到该日志必须人工核对持仓。
- **刷新粒度**：行情走 tushare 替代源约 8s；`run_time` 计时器 500ms 只是轮询
  `signal.json` 的间隔，两者不是一回事。
- **QMT 单实例**：点快捷方式只激活已有窗口，不会新起进程。要重载策略必须在模型交易
  里手动「停止 → 启动」。

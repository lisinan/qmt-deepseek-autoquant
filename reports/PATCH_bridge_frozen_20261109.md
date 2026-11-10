# bridge 补丁规格 · total_asset 漏 frozen（P0）/ heartbeat 账号（P2）/ CODE_REV

> 备件时间：**2026-11-09 09:1x（周一，盘前）**
> 状态：**已备齐、未部署、未提交**（`qmt_bridge/qmtide_bridge.py` 与 `core/` 一行未动，未 `git add`）
> 目标：OWNER 一句话批准后 **5 分钟内落地**
> 涉及文件：部署版 `C:\pazq_qmt\python\QMTIDE_BRIDGE.py`（GBK/CRLF）、仓库版 `qmt_bridge/qmtide_bridge.py`（UTF-8/LF）
> 一键脚本：`scripts/patch_bridge_frozen_20261109.py`（默认 dry-run）
> 守卫测试：`tests/test_bridge_frozen_total.py`（已注册进 `tests/run_all.py`）

---

## 0. 一句话结论

`_total_asset_of()` 用「**可用**资金 + 市值」当总资产，挂买单时资金被券商划到冻结、
可用立刻减少 ⇒ 总资产被低估 ⇒ 风控把「冻结」读成「亏掉了」⇒ 命中
`daily_stop_flatten_pct(-6%)` **强平全部可卖持仓**。
今日量化：基线 57,862.57 ⇒ 阈值只有 **3,472 元**，而单笔仓位 15,000~23,145 元
（`max_single_position_pct=0.40`）⇒ **一笔未即时成交的买单就是阈值的 4.3~6.7 倍**，
污染 `RISK_ASSET_MEDIAN_N=3`（≈9 秒）后即开火。

与 2026-11-02「−89.25% 假亏 → 强平真实持仓」属同一类机制，入口从「持仓表空返回」
换成「frozen 漏加」。

---

## 1. 三处改动（字节级 old → new）

行号为**仓库版**实测行号；部署版除第 1 行编码头外逐行等价，行号相同。

### patch A（P0）—— `_total_asset_of()`，第 **614** 行

```diff
     """
-    derived = cash + mv
+    frozen = _f(_attr(a, ["m_dFrozenCash", "m_dFrozen"], 0))
+    derived = cash + frozen + mv
     broker = _f(_attr(a, ["m_dTotalAsset"], 0), 0.0)
```

字节串（**含行首 4 空格**）：

```
old = b'    derived = cash + mv'
new = b'    frozen = _f(_attr(a, ["m_dFrozenCash", "m_dFrozen"], 0))\r\n    derived = cash + frozen + mv'   # 部署版 CRLF
new = b'    frozen = _f(_attr(a, ["m_dFrozenCash", "m_dFrozen"], 0))\n    derived = cash + frozen + mv'     # 仓库版 LF
```

不改函数签名（`_total_asset_of(a, cash, mv)` 不含 frozen）：在函数体内重取一次，
比「改签名 + 改唯一调用点 `:579`」diff 更小、波及面更小。
`_q_account():577` 已经用**同一表达式**取 frozen 填 dict，两处取值必然一致（同一账户对象）。

### patch B（P2）—— `_write_heartbeat()`，第 **465** 行

```diff
         _atomic_write_json(HEARTBEAT_FILE, {
             "ts": t,
-            "account": getattr(ContextInfo, "accountID", DEFAULT_ACCOUNT_ID),
+            "account": _account_of(ContextInfo, None)[0],
             "account_type": getattr(ContextInfo, "account_type", DEFAULT_ACCOUNT_TYPE),
```

字节串（**含行首 12 空格**，必须整行替换，否则新内容会叠在原缩进后面变成 24 空格）：

```
old = b'            "account": getattr(ContextInfo, "accountID", DEFAULT_ACCOUNT_ID),'
new = b'            "account": _account_of(ContextInfo, None)[0],'
```

依据：`_account_of()`（`:516-533`）会依次试 `ContextInfo.accountID` →
`ContextInfo.account_id` → **模块全局 `account`**，最后才回落常量。QMT 把账号注入成
模块全局变量（`boot.log` 实测 `qmt_globals account='326819285998'`），所以原写法
**永远**落到 `DEFAULT_ACCOUNT_ID="000000000000"`，该字段零信息量。
改后与下单路径（`:773 aid, atype = _account_of(ContextInfo, None)`）同源
⇒「心跳里的账号」≡「实际下单用的账号」。
安全性：globals 缺 `account` 时 `_account_of` 回落常量，**不会抛异常**（已被测试覆盖）。

### patch C（必做）—— `CODE_REV`，第 **116** 行

```diff
-CODE_REV = "2026-11-08T21:31-mv-from-positions"
+CODE_REV = "2026-11-09T0915-frozen-in-total"
```

字节串：

```
old = b'CODE_REV = "2026-11-08T21:31-mv-from-positions"'
new = b'CODE_REV = "2026-11-09T0915-frozen-in-total"'
```

**不是顺手改**：改文件 ≠ 生效。QMT 里跑的是**已加载**的旧代码，不重新运行策略，
改动等于没做；而 `rev` 是唯一能证明「跑的是哪一版」的凭据（`:470` heartbeat、`:857`
result、`:887` PING、`:1095` boot.log 四处都带它）。这与我们判断「源码 mtime < 进程
启动时刻」是同一条规矩。

---

## 2. 唯一性依据（写盘前的硬闸门）

脚本对三处目标串各断言 **`bytes.count() == 1`**，任一 ≠1 立即中止且**不写盘**。
实测（2026-11-09 盘前，逐字节统计）：

| 目标串 | 仓库版 | 部署版 |
|---|---|---|
| `    derived = cash + mv` | 1 | 1 |
| `            "account": getattr(ContextInfo, "accountID", DEFAULT_ACCOUNT_ID),` | 1 | 1 |
| `CODE_REV = "2026-11-08T21:31-mv-from-positions"` | 1 | 1 |

复现命令：

```bat
python -c "d=open(r'C:\pazq_qmt\python\QMTIDE_BRIDGE.py','rb').read();print(d.count(b'    derived = cash + mv'))"
```

另设一道「已打过」闸门：若文件里已存在 `derived = cash + frozen + mv` 则直接中止，
避免重复打补丁把 `frozen` 加两次。

---

## 3. 为什么必须 rb / wb，不能读成 str 再整文件重写

| 项 | 仓库版 | 部署版 |
|---|---|---|
| 编码 | **UTF-8** | **GBK**（首行 `#encoding:gbk`） |
| 行尾 | **LF**（1159 个） | **CRLF**（1159 个） |
| 大小 | 48,027 B | 46,841 B |
| 内容 | — | 除第 1 行外**逐行等价**（实测差异 **1 行**） |

仓库源码含 `★ → − ≤ ⚠` 等 **GBK 字符集装不下**的符号。若「读成 str → 改 → 整文件
写回」，这些字符会被替换成 `?`，注释与日志串全毁；若统一成 UTF-8/LF，则与 QMT 侧
运行环境（ANSI/GBK + CRLF）不一致。**所以脚本全程不解码、不规范化行尾**：
`rb` 读 → ASCII 子串替换（换行按目标文件原有风格生成）→ `wb` 写。

---

## 4. 部署顺序（OWNER 批准后照做）

```bat
REM 步骤 1：预演（不写盘，只打印三处命中数与自校验结论）
python scripts/patch_bridge_frozen_20261109.py

REM 步骤 2：正式写盘（自动备份 + 回读复核）
python scripts/patch_bridge_frozen_20261109.py --apply

REM 步骤 3：（可选但推荐）仓库版同步，保证两版同源
python scripts/patch_bridge_frozen_20261109.py --apply --repo
```

| 步 | 动作 | 通过判据 |
|---|---|---|
| 1 | dry-run | 三处 `hits=1`；`self-check OK: only 3 expected hunks changed`；`lines 1159 -> 1160 (expect +1)` |
| 2 | `--apply` | 自动备份到 `C:\pazq_qmt\python\QMTIDE_BRIDGE.py.bak-20261109-<HHMM>`；打印 `[OK] written`；回读 `back == new_raw` |
| 3 | **OWNER 在 QMT GUI 里：停止策略 → 重新运行** | QMT 日志出现 `init done ... rev=2026-11-09T0915-frozen-in-total` |
| 4 | 回读心跳 | `heartbeat.json` 的 `rev == "2026-11-09T0915-frozen-in-total"` |
| 5 | 复核两版同源 | `qmt_bridge/qmtide_bridge.py` 与部署版「除第 1 行编码头外逐行等价」 |

**生效判据只有一条**：`heartbeat.json` 的 `rev`。仍是
`2026-11-08T21:31-mv-from-positions` ⇒ 没生效，等于没修，不要去看账面数字自我安慰。

### 时间窗（2026-11-09 10:20 修订：原「09:15-09:25 最佳」**已过期作废**）

盘中重载的代价和盘前**不是同一个量级**，理由是拿到的新证据：

| 时段 | 重载代价 | 结论 |
|---|---|---|
| **盘前（<09:30）** | 心跳断 >6s ⇒ `_mark_disconnected()` ⇒ `_risk_total_asset()` 返回 None ⇒ 只跳过熔断判定与权益落盘 | 已错过，不再适用 |
| **盘中（09:30-11:30 / 13:00-15:05）** | 同上，**外加**：① `_sync_broker_positions` / `_persist_equity` 在空洞里被跳过 ⇒ **持仓盯市中断**，而 `peak_asset` 今日 09:30:17 刚被重写成 58,162.49、随后尾随新高抬到 59,067.49，盯市一断回撤口径就不可信；② `ORDER_TIMEOUT_SEC["BUY"]=90` 窗口内任何一次空洞都会让**在途单作废** | **不要做** |
| **午间休市（11:30-13:00）** | 无 tick、无委托、无盯市 ⇒ 空洞零代价 | ✅ 首选 |
| **收盘后（>15:05）** | 同午间 | ✅ 次选 |

⇒ **修订建议：推到 11:30-13:00 休市窗口，或 15:05 之后**。若 OWNER 今天就要生效，
唯一可接受的口子是午休；盘中一律不动（哪怕动量闸门今天 0/37、大概率无挂单，
也不能用「今天大概没单」去赌一次 90 秒的在途单窗口）。


---

## 5. 回滚（一条命令）

```bat
copy /Y "C:\pazq_qmt\python\QMTIDE_BRIDGE.py.bak-20261109-<HHMM>" "C:\pazq_qmt\python\QMTIDE_BRIDGE.py"
```

并在 QMT 里**同样重新运行一次策略**才算回滚成功（判据仍是 `rev` 变回旧值）。
仓库版回滚 = `git checkout -- qmt_bridge/qmtide_bridge.py`（若已提交则 `git revert`）。
备份文件路径会在 `--apply` 时打印；仓库内的目标把备份放在 `_backup/patch_bridge_20261109/`
（已被 `.gitignore` 的 `_backup/` 覆盖，不污染工作区）。

---

## 6. 未验证项与 Plan B 触发条件

### 未验证：`m_dFrozenCash` 在普通 STOCK 账户上是否真的存在、挂单时是否真非零

**从未观察到非零值**：`boot.log` / `boot.log.1` 里没有任何 frozen 记录；11-08 做的
DUMP 字段清单也没有落盘。代码里写了这个字段名（`:577`）说明当时是照 DUMP 清单填的，
但**值的行为**没有实证。

| 情形 | 补丁后结果 | 结论 |
|---|---|---|
| 字段存在且挂单时 >0 | `derived` 正确回升 | 修好了 |
| 字段不存在 | `_attr` 返回 0 → `_f` 返回 0 → **等于今天** | **不更坏，但也没治病** |
| 字段返回 `DBL_MAX`（QMT「不适用」哨兵） | `_f()` 过滤成 0 | 不更坏（已有测试钉住） |

**判定法（2026-11-09 10:20 修订：不必等挂单，今天就能判定「字段存在性」）**

原方案「等第一笔挂单看 frozen」有前提缺陷：**今天动量闸门 0/37，`no_entry_reason='动量闸门后候选池为空（min_mom=0.15 lookback=60 top_n=3）'`，signals/orders/fills = 0/0/0，一整天可能都没有挂单** ⇒ 这个验证法今天大概率用不上，不能把「验证」押在一个不发生的事件上。

改用**现成的只读探针**（`scripts/bridge_probe.py`，它只发 PING / DUMP / QUERY_ASSET / QUERY_POSITION，不下单）：

```
python scripts/bridge_probe.py
```

`_dump()`（`qmtide_bridge.py:831-862`）对 ACCOUNT 第一行**遍历 `dir(o)` 里所有 `m_*` 属性并
`repr(getattr(o, n))`**，即**同时返回字段名与字段值**。因此一次 DUMP 就能回答：

| 观察结果 | 结论 |
|---|---|
| `schema.ACCOUNT.fields` 里**有** `m_dFrozenCash`，值为 `'0.0'` | 字段存在且当前无冻结 ⇒ 补丁 A 的取值路径成立，只差「挂单时是否真变非零」 |
| **没有** `m_dFrozenCash` 这个键 | 字段不存在 ⇒ 补丁 A **无效**，直接判 Plan B |
| 有键但值不可读 / `'1.7976931348623157e+308'` | `_f()` 会过滤成 0 ⇒ 补丁 A **无效**，判 Plan B |

**运行时机与风险**：DUMP 会写 `signal.json`，与引擎下单**共用同一个信号槽**（bridge 每
500ms 轮询、按 `signal_id` 幂等）。若恰有在途买卖单被覆盖，那笔单会静默丢失。
⇒ 只在**当日 orders/fills 均为 0 且引擎无 `_pending_orders`** 时执行；最稳的是午间休市。
（今天实测 signals/orders/fills = 0/0/0，可执行，但**仍需 OWNER 批准**——这是往实盘下单
通道写东西，哪怕只是查询。）

**残留未验证项**（这一步 DUMP 也答不了，且不必专门去证）：`m_dFrozenCash` 在**挂单瞬间**
是否真的从 0 变非零。这是券商侧的标准行为，且 QMT 官方文档 `xtquant/doc/xttrader.md:497`
把 `frozen_cash` 定义为「冻结金额」、`:671` 把 `m_dFrozenCash` 定义为「冻结金额」——
**语义明确**。取不到反例即视为成立，**首笔真实挂单时顺手复核一次即可，不值得为它单独造一笔单**。


**Plan B 触发条件**：首笔挂单后 `frozen` 仍为 `0`（或字段缺失），即**立即判定补丁无效**，
转 Plan B —— 引擎侧兜底，不需要动 QMT：

```python
# engine/event_engine.py::_sync_broker_positions 内（bridge 模式）
self._broker_total_asset = max(
    float(asset.get("total_asset") or 0.0),
    float(asset.get("cash") or 0.0)
    + float(asset.get("frozen") or 0.0)
    + float(asset.get("market_value") or 0.0),
)
```

依据：`core/broker_bridge.py:257-262` 已经把 4 个字段都返回了，引擎侧无需改 QMT。
`max` 只抬不降：真跌时 `market_value` 同步下降，两边一起降，**不会掩盖真实亏损**；
只在「券商 total 因漏 frozen 而偏低」时兜住。
**代价**：改的是 live 风控输入路径（11-02 刚打补丁的地方），必须全量测试 + 重启引擎
⇒ **只能收盘后做**。

---

## 7. 备件阶段已完成的验证（都是可复现的）

| # | 验证 | 结果 |
|---|---|---|
| 1 | `scripts/patch_bridge_frozen_20261109.py` 对**部署版** dry-run | `codec=gbk newline=CRLF`；三处 `hits=1`；`lines 1159 -> 1160`；`self-check OK`；退出码 0 |
| 2 | 同上，对**仓库版** dry-run | `codec=utf-8 newline=LF`；三处 `hits=1`；`self-check OK`；退出码 0 |
| 3 | `tests/test_bridge_frozen_total.py` 单文件直跑（**补丁前**） | 8/8 PASS（含 `[PENDING-DEPLOY]` 分支） |
| 4 | 模拟「补丁已上线」：把仓库版打在临时副本上再加载 | 9/9 PASS：`frozen=0` 逐位相等 / 字段缺失退化 / `DBL_MAX` 过滤 / 坏 `m_dBalance` 仍被拒 / 券商字段自洽仍优先 / `frozen>0` 计入 / `CODE_REV` 已递增 / heartbeat 可写 / `rev==CODE_REV` |
| 5 | 两版内容等价性 | 1159 行，除第 1 行编码头外**差异 1 行**（只有编码声明本身） |
| 6 | 部署版心跳现状 | `heartbeat.json` 的 `"account": "000000000000"`（P2 实证） |

> 完整 `tests/run_all.py` 尚未在本次备件后重跑（需要约 10 分钟且要避开开盘动作）；
> 部署前必须跑一次全量绿，这是落盘纪律的一部分。

---

## 8. 风险登记

| 风险 | 影响 | 处置 |
|---|---|---|
| 重载期间心跳断 >6s | 引擎主循环跳过熔断判定 + 权益落盘（`_risk_total_asset()` → None，**安全方向**） | 开盘前完成即可；盘中不要做 |
| 重载后 `rev` 未变 | 改动没生效（最常见的「以为修了」） | 唯一判据：`heartbeat.json` 的 `rev` |
| 两版不同源 | 下次排障看到两个不同的 bridge，行号对不上 | 步骤 3 打仓库版 + 步骤 5 复核 |
| 补丁生效后仍出假回撤 | `m_dFrozenCash` 字段不可用 | 走 §6 Plan B（收盘后） |
| `HEARTBEAT_STALE_SEC=6.0` 余量偏薄 | 今日实测：盘中 46 分钟约 2,700 次 1s 采样，**最大相邻心跳间隔 2.50s**（开盘前两次 20.94s/11.07s 全部由 `init()` 重载造成，与查询负载无关）⇒ 6.0 只剩 2.4 倍。单次 `passorder`/慢 `QUERY` 阻塞 >6s ⇒ 引擎判 `_mark_disconnected` ⇒ 券商口径停更 | 建议 6→**12**（4.8 倍余量，且失明窗口不超过一次 60s 轮询）。不是 15：断链期间引擎是「跳过风控判定」，放太宽 = 风控长时间失明。属另一个 commit，收盘后 |
| `_io_selftest` 是否在热路径 | **不在**（init 时 daemon 线程跑一次，`:1104`） | 队友原先的「挪出热路径」建议**不需要执行** |

---

## 9. 附：同批核实的一条旁证（与本次补丁无关，但别再重复排查）

**「paper 区连不上 xtdata」不是双进程并发问题，是 QMT 2.1.32.0 的框架白名单。**
live-ops-engineer-zone 2026-11-09 现场取证（只读）：

```
logs/engine_stdout.log:
  ***** xtdata连接成功 2026-11-09 08:28:40*****      ← 只证明 socket 通了
  行情源 xtdata 不可用: RuntimeError: miniQMT probe failed:
      func:getFullTick, error:{ "error" : "not authenticated" }
~/.xtquant/ 下 6 个 hash 目录，每个只有 0 字节 running_status，**全盘无 xtdata.cfg**
```

⇒ 外部 python 进程一律 `not authenticated`，live 与 paper **各自独立**退到 tushare，
与「两个进程同时连」无关。`core/qmt_client.py:370-372` 的注释早已写明这条机理，
本次是第一次拿到正面证据。

**别被 `***** xtdata连接成功` 那行骗**：它只说明 socket 连上，不说明能取数；
判断行情源可用性要看 `getFullTick` 探针是否通过。
要修只能动 QMT 白名单/升级客户端 —— OWNER 级动作，代码补不了。

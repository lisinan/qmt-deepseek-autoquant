# AUDIT_LIVE_20261110 — 实盘执行链完整性取证与定价

> **作者**：T1 实盘链路工程师（live-integrity） ｜ **时间**：2026-11-10 12:23–13:05
> **范围**：只读取证。不写生产代码、不重启进程、不改配置。
> **唯一可写文件**：本报告（`reports/AUDIT_LIVE_20261110.md`）。
> **临时脚本**：`logs/_tmp/t1_*.py`（gitignore），全部只读：`sqlite3 ... mode=ro` / 日志文本解析 / `GET /api/snapshot`。
> **任务板**：`task-1`（subject「T1 实盘执行链完整性取证与定价」）。

---

## 〇、一页结论（先看这张表）

| # | 项 | 现状裁决 | 折成元/年（基点=60,484） | 收盘后可安全落地？ |
|---|---|---|---|---|
| 1 | 幻影单闭环 | **机制成立、但 live 从未按 README 描述发生过**；README #1 的因果归属需改写。真正未修的是「`ok=True` 的语义 = 信号已投递」 | **800–4,000 元/年 = 1.3–6.6 pt/年**（随 unknown 概率 p_u 变化） | ✅ 可改，但**必须重启引擎**才生效 |
| 2 | 断链期持仓不受退出管理 | **方向对、代价可忽略**（实测窗口 13.3 s，年化 ≈ 0 元）。真正该做的是「断链 >60 s 告警」，**不是**在退出路径加动作 | **≈0 元/年**；尾部（断链永久 + 止损失效）= 5,742.30 元 = **9.5 pt/次** | ✅（纯告警，低风险） |
| 3 | oid 空 → 回灌失效 → 幻影仓归零 → 二次下单 | **链条 6 步全部可指到行**；但「下一轮二次下单」在 **BUY 侧不成立**（300 s 节流 + `held_codes`），只在 **SELL 侧成立**（每轮 3.0–4.3 s） | 并入 #1 | 与 #1 同批 |
| 4 | T+1 与手工买入语义 | **「SELL 600 被拒」是伪命题（被夹紧成 500）；真问题是卖完 500 后剩下的 100 股会引发 SELL 风暴**，且 `_handle_sell` 恒返回 False | 一次性 +0.84 pt（残仓占槽）+ 关掉一个风暴入口 | ✅ 可改，需重启；**今天下午的正确处置 = 不卖 510050** |
| 5 | WinError 183 主修是否在运行代码里 | **PASS（已生效，有 5 条硬判据）**，包含桥侧 8 次退避 + 引擎侧 8 次退避 | 已经省下来的就是 11-09 那 4 次断线 | 无需动作 |
| **N1** | **【新·今天下午】`owner_order` 的 SELL 忽略 `lots`** | **P0**：OWNER 下 `SELL 100`，引擎会提交 **`SELL 500`**（5 倍意图，≈1,447 元），且账本记成 `reject` | 单次 **+1,157 元 = +1.91 pt** 的超额敞口 | ⚠ 改代码来不及（需重启）⇒ **今天 13:00 后不要用指令单测 SELL** |

**本次最重要的三条纠正**（都与既有文档/共识冲突，请优先看）：

1. **10-30 那 654 笔 `300308.SZ SELL` 不是幻影单，是「被拒的失败单」**：`{'ok': False, 'order_id': None, 'reason': 'order_stock rc=-1'}`。全 8 个 live 交易日里，`ok=True` 且 `order_id` 空、触发那条 `买入/卖出委托返回 ok 但无 order_id` warning 的记录 = **0 次**（日志全量检索）。打满 `daily_trade_count` 的是**旧代码把失败单也计数**（已于 10-30 17:26 `commit 5de0188` 之后的版本修正，现行 `event_engine.py:3861-3864` 已在 `if res.get("ok")` 内）。
2. **但 README #1 没有过期，它只是换了入口**：2026-11-08 21:35 交易通道切到文件桥之后，`passorder` 无返回值 ⇒ 桥**结构性地**返回 `ok=True, order_id=None`。也就是说「幻影单」从「偶发缺陷」变成了「经桥落单的默认结局」，只因**8 天 live 里经桥的委托是 0 笔**（`orders` 止于 11-03）才一直没爆。
3. **运行版引擎不含今天 12:02 的补丁**：PID 26576 启动于 `11:34:58`，而 `engine/event_engine.py` mtime = `12:02:00`、HEAD commit = `12:05:32`。今天任何写进 `engine/` 的改动都是「文件已新、进程仍旧」。**收盘后落地的交付清单必须包含一次重启。**

---

## 〇.1 计价口径（后面所有元/pt 都用这一套）

| 量 | 值 | 出处 |
|---|---|---|
| 权益基数 | **60,483.78 元** | `equity_snapshots` id=10781，`2026-11-10T11:29:51`（最新一行，盘中） |
| 1 pt | **604.84 元** | 权益基数 × 1% |
| 年化基数 | **244 个交易日** | A 股年均交易日 |
| 现金 | 11,315.18 元 = 18.71% 权益 | `engine_state.cash`；`/api/snapshot.cash` 12:26 实测一致 |
| 期望入场笔数 | **0.18 笔/交易日** | R38 IS 面板：164 笔 / ≈900 交易日（`reports/EVOLUTION_DECISIONS.md`，**未独立复跑**） |
| 每笔期望收益 | **+0.84% 权益** | 同上：IS 累计 +295.74% 按 164 笔复利反解 `(1+x)^164 = 3.9574` |
| ⇒ 一个交易日的入场预算期望值 | **91 元** | 0.18 × 0.84% × 60,484 |
| 单档仓位（现金夹紧上限） | 11,315 元 = 18.7% 权益 | `_clamp_to_buying_power` |
| 单档停止损失（−18%） | **2,037 元 = 3.37 pt** | 11,315 × 18% |
| 主循环节拍（今天实测） | **4.28 s/轮** | 心跳：09:50:22 轮次 278 → 10:00:25 轮次 419 = 141 轮 / 603 s |
| 主循环节拍（10-30 风暴期实测） | **3.03 s/笔** | 643 笔 / 1951 s（09:30:53 → 10:03:24） |

> **口径声明**：p_u（经桥落单出现 `ok=True` 且 `order_id` 空的概率）**没有任何 live 样本**。下文所有「元/年」在涉及 p_u 的地方都给 **0.2 / 0.5 / 1.0 三档**，并显式标注这是**假设**而非测量值。凡实测得出的数字，都给了 `file:line` 或 DB 行/日志行。

---

## 一、必查项 1：幻影单闭环根因

### 1.1 「为什么 passorder 拿不到 order_id」——桥侧是结构性无解，不是 bug

| 证据 | 位置 |
|---|---|
| `passorder returns nothing usable, so order_id is best-effort (usually None)` | `qmt_bridge/qmtide_bridge.py:788`（函数 docstring） |
| `return True, (str(ret) if ret not in (None, 0) else None), ""` —— 只要 passorder 返回 `None/0`（**QMT 的正常成功形态**），桥就返回 `ok=True, order_id=None` | `qmt_bridge/qmtide_bridge.py:823` |
| 唯一例外：`if isinstance(ret, int) and ret < 0: return False, None, ...` —— 只有 passorder **抛负返回码**才算失败 | `qmt_bridge/qmtide_bridge.py:821-822` |
| 桥的 result.json 对 BUY/SELL 只回 `{"order_id": oid}`，`oid` 就来自上面那个 `None` ⇒ **result.json 里根本没有真委托号可落** | `qmt_bridge/qmtide_bridge.py:923` |

**关键判据（lead 提出的那条）成立**：桥侧确实没有把 order_id 写进 result.json，所以 `ok=True` 的语义是「**signal 已投递 + passorder 已调用**」，**不是**「委托已受理」。`res.get("ok")` 这个布尔在这一层上语义就是错的。

**引擎侧的补偿链（三级，全部会断）**：

| 级 | 代码 | 断点 |
|---|---|---|
| ① 取桥返回值 | `core/broker_bridge.py:412` `oid = data.get("order_id")` | 恒为 `None` |
| ② 反查 `QUERY_ORDER` 差集 | `:413-416` `if not oid and before is not None: oid = self._attribute_order_id(...)` | **`before` 的构造是 `_order_id_set()`，`QUERY_ORDER` 返回 `None` 时它返回 `None`（`:328-333`）⇒ 整段反查被跳过**，直接落 `ok=True, order_id=None` |
| ③ 反查预算 | `:335-379`，`ORDER_ATTR_TIMEOUT_SEC = 3.0`（`:325`），15 次轮询 | 即使查询正常，只要 ORDER 列表 3 s 内没出现这笔，就返回 `None`（`:375-379` 打 ERROR） |

`docs/QMT_BRIDGE.md` §八 自己写着：`m_nOffsetFlag` 的 23/24 买卖判定 **❌ 未实测（`ORDER count=0`，无在途单样本）**；`passorder` 能否落实现货账户 **❌ 未实测**。⇒ **「反查能不能命中」在仓库里被明确标注为未实测项**，所以不能假设它成立。

**今天 11:35:39 的 `result.json` 真实样本**（`C:\pazq_qmt_bridge\result.json`）证明桥能正确回查询类请求、但该文件里**没有任何一笔委托类应答**：

```json
{"signal_id": "3986b474...", "status": "ok", "data": [{"code": "510050.SH", "quantity": 600, "available": 500, ...},
                                                     {"code": "300308.SZ", "quantity": 100, "available": 100, ...}], ...}
```

用的正是 `QUERY_POSITION`（同目录 `signal.json` 的 `action`）——即桥的 request/response 通路是活的，**缺的只是「委托类请求的历史样本」，因为一笔都没有过**。

### 1.2 「引擎为何判成功」——两处 live 分支都只看 `ok`

**卖出侧**（`engine/event_engine.py:3848-3875`）：

```
3849  res = qmt_broker.place_order(sig.code, "SELL", qty, price, "cash")
3861  if res.get("ok"):
3862      self._daily_trade_count += 1          ← 幻影单也 +1
3866      _oid = str(res.get("order_id") or "")
3867      if _oid:
3868          self._register_pending_order(...) ← 只有拿到 oid 才登记
3871      else:
3872          logger.warning("卖出委托返回 ok 但无 order_id（%s），无法跟踪成交")
3874      return False                          ← 调用方看到「没卖出去」
```

**买入侧同构**（`:3647-3685`），且多一条**乐观建仓**：

```
3663  if res.get("ok"):
3664      self._daily_trade_count += 1
3668      self._pos_meta[sig.code] = {...open_date=now...}
3672      self._positions[sig.code] = Position(quantity=qty, ...)   ← 幻影仓
3679      _oid = str(res.get("order_id") or "")
3683      else: logger.warning("买入委托返回 ok 但无 order_id（%s），无法跟踪成交")
```

⇒ 一次 unknown 的全部后果：**计数 +1、建幻影仓、不登记 pending、只留一行 WARNING、仓位一个股数都没动**。

### 1.3 「daily_trade_count 如何被刷满」——两条完全不同的机制，必须分开

`max_daily_trades = 10`（`config/settings.py:948`），闸门在 `risk/manager.py:217-218`：`if daily_trade_count >= 10: return False, "daily_trades>10"`。

| 机制 | 触发条件 | 节拍 | 刷满用时 | 状态 |
|---|---|---|---|---|
| **历史机制（10-30 实况）** | 计数写在 `if res.get("ok")` **之外** ⇒ **失败单也 +1** | 3.03 s/笔 | 10 笔 / 30 s | **已修**（10-30 17:26 起；现行 `3861`/`3663` 都在 ok 内） |
| **现行机制（未修）· SELL 侧** | `ok=True` + oid 空 ⇒ 不登记 `_pending_sells`（`:3867-3870`）⇒ `:3795` 那道去重闸形同不存在 ⇒ 退出信号每轮重现 ⇒ 每轮一笔 | **每轮 1 笔 = 4.28 s** | **10 笔 / 43 s** | **未修** |
| **现行机制（未修）· BUY 侧** | 同上，但受两道节流保护：`held_codes` 在 `:2878` 计算、`:2931` `if code in held_codes: continue`；入场决策 `ENTRY_DECISION_INTERVAL_SEC = 300`（`:300`） | **每 5 min 1 笔** | **10 笔 / 50 min** | **未修，但危害小一个量级** |

> **这条必须写进 README 修正**：README §十二 #1 写的「30 秒可刷满 `max_daily_trades`」**只对 SELL 侧成立**；BUY 侧被 300 s 节流 + `held_codes` 挡成 50 分钟。同一份文档把两者的节拍混为一谈，会误导后来人把 BUY 侧当成急症。

**10-30 的实测节拍**（DB `orders` 逐条 + 日志逐行对齐，n=656）：

| 时段 | 笔数 | 内容 |
|---|---|---|
| 09:30:53 | 2 | `300308 SELL 100 @540.74` + `510050 BUY 500 @2.846`（**均 ok=False**） |
| 09:30:53 → 10:03:24 | **643** | `300308 SELL 100`，`reason='order_stock rc=-1'`，**每 3.03 s 一笔**，共 33 分钟 |
| 10:03:24 | — | 检测到外部 `STOP_ENGINE`，引擎优雅退出（`quant_system.log:72356`） |
| 10:07:04 | — | 重启；`恢复风控状态: peak=66542.31 ... daily_trade_count=0`（`quant_system.log:72362`）⇒ **计数器被重启清零** |
| 10:07:37 / 10:07:45 | 2 | `510050 BUY 500`（oid=1082183428）/ `300308 SELL 100`（oid=1082183556）——**ok=True 且带真 oid** |
| 11:24:56 → 11:29:59 | ≈11 | 假 `−86.58%` 日内熔断强平链（见 §1.5），全部带真 oid |

⇒ **买入闸门实际关闭时刻：≤ 09:35:54**（`quant_system.log:70662` 首条 `BUY 拒绝 510050.SH: daily_trades>10`），即开盘 **5 分 01 秒**。当日 `daily_trades>10` 拒绝共 6 条（同型只出现在 2026-09-23 的 37 条）。

### 1.4 「一次幻影风暴的真实代价」——分档定价

**A 档：闸门关到收盘（必损项）**

风暴起于 T，10 笔刷满后 `daily_trade_count ≡ ≥10` 直到收盘（跨日才在 `_reset_daily_if_needed:1155-1158` 归零）。
- 若风暴发生在 09:30–09:35（10-30 的形态）⇒ 丢掉当日**全部**入场预算 = **91 元**
- 若发生在 10:30（SELL 风暴）⇒ 丢掉半天 ≈ **45 元**
- 取 09:30 形态：**91 元/次 = 0.15 pt/次**

**B 档：重复买入尾巴（有条件项）**

⚠ **纠正一个流传的说法**：BUY 侧**不会**「下一轮就二次下单」。`_run_single_step:2878` 在入场段开头算 `held_codes`，`:2931` 直接 `continue`；且整段被 `ENTRY_DECISION_INTERVAL_SEC=300` 节流。幻影乐观仓（`:3672`）一进 `_positions`，下一轮就被 `held_codes` 挡住。
⇒ 只有在「幻影仓被 `_sync_broker_positions` 以券商为准归零（`:4490-4500`）」之后，**下一个 5 分钟窗口**才可能再买一次。
⇒ 尾部上限 = 1 档仓位 = 现金夹紧后的 11,315 元 × −18% = **2,037 元 = 3.37 pt**，且要求「回执丢了但实际成交了，而券商持仓同步滞后超过 5 分钟」——概率不是 1。

**C 档：桥单槽被打满（INBOX 2026-11-09 已定价，我复核后维持）**

持续 unknown 重试 = 每轮 1 次 `signal.json` 写 + 1 行 `orders`。若把计数口径单独改掉而不配退避，SELL 侧恒定 4.28 s/笔 ≈ **4,800 次/日** ⇒ 单槽文件桥被打满 ⇒ `get_positions` 返回 `None` ⇒ `_risk_asset_basis=untrusted` ⇒ `asset_untrusted` 把所有新开仓关掉。
⇒ 这是「从 43 秒后显性关闸」变成「几小时后隐性关闸」，不是免费修法。

**D 档：`fills` 缺失的连带损伤**（不产生现金流，但抬高 #4 的止损失效概率）

`_apply_trade:3367-3369` 以 `oid` 为键：`if oid not in self._pending_orders: return 0`。unknown 单永远不登记 pending ⇒ **永远不产生 `fills` 行** ⇒ 重启后 `_backfill_open_date:4355-4375` 查不到该票的 BUY ⇒ `open_date = None` ⇒ `is_t1_locked` 与 `is_in_entry_protection` **同时失效**（该函数 docstring `:4319-4331` 自证）。这条链在**今天就有一个真实样本**：手工买入的 100 股 510050 因为 `storage` 里今日 `orders=0/fills=0`，`open_date` 沿用了 11-03（见 §4）。

**年化（A 档 + B 档）**

| p_u（假设） | 落单日/年 | 风暴日/年 | A 档 | B 档（取 10% 命中） | 合计 | pt/年 |
|---|---|---|---|---|---|---|
| 0.2 | 44 | 8.8 | 800 元 | 179 元 | **979 元** | **1.6 pt** |
| 0.5 | 44 | 22 | 2,002 元 | 448 元 | **2,450 元** | **4.1 pt** |
| 1.0 | 44 | 44 | 4,004 元 | 896 元 | **4,900 元** | **8.1 pt** |

（落单日/年 = 244 × 0.18；B 档 = 风暴日 × 10% × 2,037）

> **诚实的边界**：p_u 无 live 样本，这三行是**假设推演**，不是测量。唯一能把它变成测量的动作是**拿一笔真实小单标定**（见 §7 建议第 5 条）。历史频率（1/8 live 日 = 12.5%/日 = 30.5 日/年）属于**已被修掉的旧机制**，不能外推。

### 1.5 10-30 当天的实际损失：**可测量地接近 0**（不要把它当「幻影单造成 6 万」）

诚实核对：
- 300308 首次卖单 09:30:53 @540.74，实际成交 10:07:45 之前 @569.24（`fills#385`）⇒ **晚卖 37 分钟，反而多卖 2,850 元**（569.24 − 540.74 = 28.50 × 100 股）。所以这一天「卖晚了」不是亏损，是运气。
- 真正可归因的损失 = **买入闸门关闭的 31 分钟**（09:35:54 → 10:07:04 重启）⇒ 31/240 × 0.18 笔 ≈ 0.023 笔 × 508 元 ≈ **12 元**。
- 11:24:53 那次假 `−86.58%` 日内熔断+强平（`quant_system.log:72641-72648`）是**另外 3 个 P0 的复合**（估值口径 / daily_pnl 口径 / 价格类型非法），不能记在幻影单账上。

⇒ **裁决：机制是真缺陷，但把 10-30 的灾难归因给它，会让后来人去修错地方。** 该修的是 §1.2 的语义，不是「限制重试次数」。

### 1.6 对 lead 补丁形状的逐条裁决

| lead 的第几条 | 裁决 | 理由与改法 |
|---|---|---|
| ① 三态 `ok` / `submitted` / `order_id`，unknown 单列 | **同意** | `core/broker_bridge.py:383` docstring 已写「`ok=True` 仅代表已提交」，代码却没把这个区分暴露出来。`submitted=True` 只在真拿到 oid 时给；`ok=True+oid空` → `submitted=None`（**不得用 `False` 表示不知道**，INBOX 2026-11-09 已钉） |
| ② unknown 时**不计数、不建仓、不登记 pending** | **不计数 / 不建仓 = 同意；「不登记 pending」= 反对** | `_pending_sells` 是**唯一**给 SELL 去重的东西，而它只在「有 oid」时建立（`:3867-3870`，`:3359-3360`）。unknown 时若完全不登记，300308 的 −18%（豁免仓**唯一**自动出场路径）每轮重发 —— 这就是 10-30 的形态。**改法：unknown 时登记一个 `oid=None` 的占位 pending（写入 `_pending_sells[code] = ""`），让 `:3795` 的去重继续生效；重试改由「退避计时器到点 ⇒ 主动清占位」驱动。** 这样「不丢唯一出口」和「不刷爆单槽」同时成立 |
| ③ 退避带时间属性、30→300 s 上限、不得用「每天 N 次」 | **同意，且必须补一条** | `FORBIDDEN` 的依据（INBOX 2026-11-09）成立。**补的这条**：退避的**解除条件必须是「拿到 oid」或「券商持仓/可用量发生变化」或「计时到点」三者之一**，不能只有计时——否则急跌里「一次 unknown ⇒ 5 分钟不再卖」= 停摆 5 分钟。另外退避状态必须**跨 `_run_once` 存活但在 `_reset_daily_if_needed:1165-1168` 的跨日清理里一并清掉**，否则隔夜残留会把次日第一笔卖出挡掉 |
| ④ 不对 SELL 侧加「在途/丢回执即拦截」守卫 | **同意** | 1:3.70 的价签（INBOX：保护 1,552.91 元 vs 代价 5,742.30 元）我复核后成立：`411.286 → 353.863` 差 57.423 元/股 × 100 股 = **5,742.30 元**（= 9.5 pt），而重复买入上限 8,627.27 × 18% = 1,552.91 元。 |
| ⑤ 计数口径与退避必须同批上线 | **同意，并补测** | 单改计数 = 从「43 秒关闸」变「全天隐性关闸」（C 档）。测试要求见 §7 |

---

## 二、必查项 2：断链期持仓是否真的不受退出管理

### 2.1 实测数据（不是推断）

`broker_reconnects` 全历史（日志 + 心跳双源）：

| 日期 | 重连次数 | 时刻 | 成因 |
|---|---|---|---|
| 2026-10-29 | 1 | 15:14:24 | 启动期 XtQuantTrader 断开（非交易时段，无关） |
| 2026-11-09 | **4** | 10:03:37 / 10:23:43 / 10:38:53 / 11:27:41 | **全部由 WinError 183 造成**，与 4 次 `result write failed` 一一对应 |
| 2026-11-10 | **0** | — | 全天心跳 `券商重连=1`（= 计数器初值，未递增）；`boot.log`/`boot.log.1` 无 `write failed` |

**单次断链的完整时间线（11-09 10:03，逐行日志）**：

```
10:03:24  [bridge boot.log] pid=17080 result write failed: [WinError 183] result.json.tmp -> result.json
10:03:34,054  core.broker_bridge: 桥接超时 action=QUERY_TRADE（10.0s 无 result）
10:03:34,054  core.auto_reconnect: AutoReconnector[broker] 断线
10:03:34,055  主循环慢轮 10.0s（轮次=461，累计慢轮 1）分段: ticks=0ms bars=0ms positions=10018ms exit=0ms ...
10:03:36,590  BridgeBroker 已连接（heartbeat_age=1.1s）
10:03:37,285  券商重连成功：已重发行情订阅(38 只)并同步持仓
```

⇒ **盲窗 = 10:03:24 → 10:03:37 = 13.3 s**。这 13 s 里：
- 主循环**单线程阻塞在 `positions` 分段 10.0 s**（日志原文 `positions=10018ms`），该轮 `exit` 分段 `0ms`；
- 期间 `_handle_sell` 即使被调用也只会拿到 `ok=False`（`_request` → `connect()` 失败直接 `return None`，`core/broker_bridge.py:208-209`）；
- `_sync_broker_positions` 在未连接时**直接 return**（`engine/event_engine.py:4389-4390`），本地账本冻结在最后一次成功同步的值。

### 2.2 「持仓真的不受退出管理吗」——准确表述

| 说法 | 判定 |
|---|---|
| 「断链时止损/趋势退出**不作用于**真实仓位」 | **字面为真，但同义反复**：通道断了，任何委托都到不了券商。引擎并非「不评估退出」——`_run_once` 的退出段（`:2294-2340`）在断链恢复后的**下一轮照常跑**，且信号不会被清掉 |
| 「断链期引擎会脏写持仓/权益」 | **已防住**：`_sync_broker_positions:4389` 早退；`_total_asset` 走 `_broker_total_asset` 且 `_risk_total_asset()` 返回 `None` 时**不判定、不强平、不写快照**（`:2216-2218`） |
| 「断链会造成重复下单」 | **未证伪**：超时后 `_request` 用**新** `uuid4`（`core/broker_bridge.py:213`），而桥按 `signal_id` 幂等（`processing_ids.json`）⇒ 引擎层的任何重发在桥看来都是全新一笔。**这条仍是开机风险**，归入 §1 的 unknown 一笔账 |

### 2.3 定价

**常态（13.3 s 窗口）**：3,008 只 A 股的日内波动在这个量级上可忽略。
- 300308 ATR14 ≈ 8.03%（`config/settings.py:650` 附近注释）⇒ 13 s 的 σ ≈ 8.03% × √(13/14400) ≈ **0.24%**
- 单事件最大漂移 = 47,436 元 × 0.24% ≈ **113 元**
- 且只在「止损恰好在这 13 s 内被击穿」时发生，P ≈ 13 s / (4 h × 3600) ≈ **0.09%**
- ⇒ 期望 ≈ **0.1 元/事件**

**年化**：4 事件/日 × 0.1 元 = 0.4 元/日 ×（若 183 未修，全年）= **≈98 元/年 = 0.16 pt/年**；**修后 0 事件 ⇒ 0 元/年**。

**尾部（断链不是 13 s，而是永久）**：QMT 崩 / 策略被停 / 桥单槽死锁时，引擎会看到 `broker_connected=False` 但**不会告警升级**（只有面板上一行状态）。此时 300308 的 −18% 是「唯一自动出场路径」（`_split_flatten_candidates` 结构性剔除豁免仓、`_daily_fallback_exit` 在 `:2303` 也被豁免掉）⇒ 价格从 411.286 滑到 −20% 熔断线对应的 353.863，**仓位仍在手上**：
`(411.286 − 353.863) × 100 = 5,742.30 元 = 9.49 pt`。
频率：8 个 live 日 0 次（无样本）。

### 2.4 修复代价与落地

- **不建议**在退出路径里加任何「断链期动作」——那正是 INBOX 2026-11-09 已判 `FORBIDDEN` 的同一形态（保护:代价 = 1:3.70，失败方式是止损失效）。
- **建议**：把「`broker_connected=False` 持续 > 60 s」升级成 `system_notice(ERROR)` 走 notices + guardian 告警。改动量 = 1 个条件判断 + 1 条 notice，无副作用。
- **能否收盘后安全落地**：✅ 可以，但**同样需要重启**才生效（进程启动 11:34:58 < 事件引擎 mtime 12:02，见 §0 第 3 条）。

---

## 三、必查项 3：`order_id` 空 → 成交回灌失效 → 幻影仓归零 → 信号重现 → 二次下单

### 3.1 完整因果链（6 步，每步可指到行）

| 步 | 机制 | 代码/数据 |
|---|---|---|
| ① 桥返回 unknown | passorder 无返回值 ⇒ `ok=True, order_id=None` | `qmtide_bridge.py:823` |
| ② 引擎判成功、计数、乐观建仓、**不登记 pending** | `if res.get("ok")` 分支 | `event_engine.py:3663-3685`（BUY）/ `3861-3874`（SELL） |
| ③ 无 pending ⇒ **无成交回灌** | `_apply_trade` 首行以 oid 为键 | `:3367-3369` `if oid not in self._pending_orders: return 0` |
| ④ 无 fills ⇒ 重启后 `open_date` 只能从 fills 回灌 | `_backfill_open_date` 取「最近一笔 BUY」 | `:4355` / `:4373-4375` |
| ⑤ 幻影仓被券商权威归零 | `_sync_broker_positions` 清掉 broker 已无持仓的本地记录 | `:4490-4500`（注释自述「这一步是销毁性的」） |
| ⑥ 信号重现 ⇒ 再下一笔 | 卖出：退出条件仍在（`pos.quantity>0` 从未被扣减）⇒ 每轮重发；买入：`held_codes` 挡住，只在下一个 300 s 窗口重发 | `:2294-2340` / `:2878`+`:2931`+`:300` |

### 3.2 实测哪几步成立

| 步 | 实测状态 |
|---|---|
| ① | **静态确定**（代码只有这一条成功路径），但**无 live 样本**（经桥委托 0 笔） |
| ② | **代码在运行版里**（`event_engine.py` 的这两段未被今天 12:02 的改动触及；见 §5 的版本核查方法） |
| ③ | **静态确定**；同源证据：今天手工 100 股「无 fills」已导致 `open_date` 错位（§4） |
| ④ | **已有一个真实样本**：510050 的 `open_date` 只能回灌到 11-03（因为手工 100 股无 fills） |
| ⑤ | **有历史实证**：`:4490-4500` 的注释记录了 2026-11-02 10:34:10 那次「单次空返回 ⇒ 本地持仓全零 ⇒ 假 −89.25% ⇒ 强平真仓」，现在改成「连续 2 轮确认」（`SYNC_EMPTY_CONFIRM_N`） |
| ⑥ | **SELL 侧成立**（无节流，10-30 的 654 笔就是它的前身形态）；**BUY 侧不成立**（`held_codes` + 300 s 节流） |

### 3.3 定价

本项**不直接产生现金流**，它的代价全部落在别的项上：
- 把 §1 的 A 档从「一次性」变成「每轮重复」；
- 把 §4 的「止损失效」概率从 0 抬到 >0（`open_date=None` ⇒ T+1 与保护期双失效）；
- 把 §2 的尾部（9.5 pt）从「理论」变成「可达」（因为没有 fills 的仓位在重启后连 T+1 都锁不住）。

⇒ **不单独定价，与 #1 / #4 合并计价**，避免重复计算。

---

## 四、必查项 4：T+1 与手工买入语义

### 4.1 三条硬证据（全部来自今天，非推断）

| # | 证据 | 来源 |
|---|---|---|
| ① | `510050.SH` 券商侧 **`quantity=600, available=500`**；`300308.SZ` **`quantity=100, available=100`** | `C:\pazq_qmt_bridge\result.json`，`QUERY_POSITION` 应答，`ts=1794281739`（今日 **11:35:39**） |
| ② | 本地 `engine_state.positions`：`510050 qty=600 avg=2.8930 open_date=2026-11-03T09:30:21.508940`；DB `fills` 里 510050 只有 11-03 的 500 股（`fills#389`，`order_id=1082248068`）；**今日 `orders=0 / fills=0`** | `storage/qmt.db::engine_state`（只读）；`fills` 表全表扫描 |
| ③ | 「按整仓提交，券商侧可能拒单」这条告警**全日志只有 2 次**：`2026-10-30 11:24:56` 与 `2026-11-02 10:34:40`（均为 xttrader 时代）；11-03 之后 **0 次** | `logs/quant_system.log:72650` / `:74910` |

### 4.2 「本地以为 600 股可卖、券商只有 500」的准确后果 —— 原命题需要修正

**关键：`_handle_sell` 会夹紧到 `available`，所以它根本不会提交 `SELL 600`。**

```
3830  qty = int(pos.quantity)                    # 600
3831  avail = int(getattr(pos, "available", 0) or 0)   # 500（由 _sync_broker_positions:4459 每轮写入）
3832  if self.exec_mode == "live" and avail > 0:
3833      qty = min(qty, avail)                  # ⇒ 500
3834  elif self.exec_mode == "live" and avail <= 0:
3839      logger.warning("可用量字段为 0，按整仓提交（券商侧可能拒单）")
```

证据链：`_sync_broker_positions:4459-4465` 每轮把券商的 `available` 写进 `Position.available`；`Position.available` 默认 0（`core/data_models.py:100`）。桥的 `QUERY_POSITION` 确实回 `available: 500`（证据①），且证据③ 证明 11-03 之后没再走过 `avail<=0` 分支。

⇒ **`SELL 600` 的真实拒单概率 = 0（被夹紧成 500）。** 但——

**夹紧一旦失效（`avail` 为 0/None），拒单概率 = 100%**。样本：`2026-10-30 11:24:56` 走 `avail<=0` 分支提交整仓 → 同一秒柜台拒绝：

```
[BROKER] 委托被拒 order_id=1082242609 error_id=306050
         msg=限价卖出 [SZ300308] [COUNTER] 股份资产不足[证券代码：300308，资产账户：...，尚需(股份可用)...]
```
（`logs/quant_system.log:72650-72658`）

### 4.3 真正的语义后果（比「拒单」严重）

**(i)「清仓」永远不会成立，且会自触发一轮 SELL 风暴** —— 这是本条的核心发现

1. 卖 500 股（`min(600,500)`）→ 券商侧剩 100 股，这 100 股是**今天买的** ⇒ 券商 `available=0`；
2. 下一轮 `_sync_broker_positions` 把 `pos.quantity=100 / pos.available=0` 写回；
3. −18% 或任何退出条件**仍然成立**（`pos.quantity>0` 从未到 0）⇒ 退出段再次调 `_handle_sell`；
4. `qty=100, avail=0` ⇒ 走 `:3834-3839` **按整仓提交** ⇒ 柜台以 T+1「股份可用不足」拒绝；
5. 但**桥回 `ok=True`**（passorder 无返回，§1.1）⇒ 引擎计数 +1、不登记 pending ⇒ **每轮重发**；
6. 4.28 s/轮 ⇒ **43 秒后 `daily_trade_count=10`，当日买入端全关**。

而且这条路径在**代码层面是通的**：本地 `open_date=2026-11-03`（证据②）⇒ `is_t1_locked`（`:3803`）**不锁**，T+1 这道唯一的语义保护被「手工买入没有 fills 行」绕过了。

**(ii) `_handle_sell` 在 live 下**恒**返回 False（即使委托已提交成功）**

```
3861  if res.get("ok"):
...
3874      return False     ← 已提交成功也 return False
```
⇒ 所有调用方（含 `core/owner_order.py:355-369`）都把「已提交」读成「未成交」。`owner_order` 因此把一次真实委托记成 `reject: 落单口未成交`（`owner_order.py:367-370`），幂等账本与事实相反。

**(iii) 残仓占槽**：卖 500 后剩 100 股（≈290 元 = 0.48% 权益）仍使 `len([p for p in positions if p.quantity>0])` 计数 +1（`:2890`、`:2936`）⇒ 在 5 槽满载时等于少一个入场机会。

### 4.4 定价

| 子项 | 金额 | pt |
|---|---|---|
| (i) 一轮 SELL 风暴 | = §1 A 档（91 元）+ B 档尾巴 | 0.15–3.4 pt |
| (ii) 账本误记 | 0 元（信息损失，但会让 OWNER 重复下令） | 0 |
| (iii) 残仓占槽（一次性） | ≈ 508 元（0.84% × 60,484 的 1 次机会） | **0.84 pt** |
| **510050 `SELL 600` 整单被拒的直接损失** | **0**（被夹紧）→ 若夹紧失效：600 × 2.361 = 1,417 元上限（全损） | 0 → 2.34 pt |

**年化**：510050 唯一的自动出口是 −18%（2.361），需从 2.895 跌 18.4%，ETF 有 ±10% 单日限制 ⇒ 至少 2 个跌停日。粗略 P(年) ≈ 5% ⇒ (iii) 年化 ≈ 25 元/年 ≈ 0.04 pt/年；(i) 的年化并入 §1。**结论：这不是一笔大钱，但它是一个「一旦触发就同时废掉止损 + 关死买入端」的共同入口，所以优先级高于它的金额。**

### 4.5 修复代价、风险、能否收盘后落地

- **修法（推荐，与 INBOX 2026-11-10 12:15 的建议一致）**：`_sync_broker_positions` 对「券商数量 > 本地已记账买入数量」的差额记一个 **影子批次**，`open_date = today`；`_handle_sell` 的可用量 = 券商数量 − 当日新增批次。
- **更小的等效修法**（1 行级）：`_handle_sell` 在 live 下若 `avail <= 0`，**不要**「按整仓提交」，而是直接 `return False` + 一条 ERROR 告警（因为该量在券商侧必然被拒）。这样把「43 秒风暴」换成「一行可见的告警」，代价是「真的能卖却报 avail=0」的情形会漏卖一次（但那一情形已经 6 天没出现过，且漏卖只影响该轮，下一轮重试）。
- **风险**：改的是 live 唯一落单口 + 持仓同步，必须配 3 条测试（见 §7）。
- **能否收盘后安全落地**：✅ 可以，**需重启**。⚠ **今天下午不可卖 510050**：卖 600 会变 500（不清仓），卖 500 会引发 (i)。

---

## 五、必查项 5：WinError 183 的主修是否已在运行中的代码里生效

### 5.1 时间线（逐项实测，不引用二手结论）

| 时刻 | 事件 | 判据/来源 |
|---|---|---|
| 11-09 11:24:58 | `qmt_bridge/qmtide_bridge.py` 落盘，`CODE_REV = "2026-11-09T1125-write-retry"`（`:116`） | 文件 mtime + 源码常量 |
| 11-09 11:27:30 | **旧代码**又报一次 183（`result.json.tmp -> result.json`，正是 `os.rename` 那一步） | INBOX §P0-闭合 ② 原文 |
| 11-09 11:37:24 | QMT 侧「停止策略 → 运行策略」，`heartbeat.rev` 变新值；`boot.log` 出现第 2 条 `init done` | INBOX §P0-闭合 ③ 四项 PASS |
| 11-09 11:37:24 → 当日收盘 | `write failed` 新增 **0** 次；`broker_reconnects` 停在第 5 不动 | 同上 ③ `#3`/`#4` |
| **11-10 09:17:46** | **`XtItClient.exe`（pid 18180）当日重启** ⇒ 桥从磁盘**重新加载一次** | `Get-Process 18180 \| StartTime` |
| **11-10 12:26** | `heartbeat.json` = `{"pid": 18180, "rev": "2026-11-09T1125-write-retry", ...}` —— **运行进程自己写的 rev = 磁盘文件的 rev** | 实测读 `C:\pazq_qmt_bridge\heartbeat.json` |
| 11-09 11:37:24 → 11-10 12:26 | `boot.log` + `boot.log.1` 全文检索 `write failed` = **0 次** | 两份 boot.log 全量 grep |
| 11-10 全天 | `券商重连成功` = 0 次；心跳 `券商重连=1`（初值未递增） | `logs/quant_system.log` 11-10 段 |

### 5.2 裁决：**PASS（在运行代码里生效）**

三条硬判据（互相独立，不能只靠一条）：

1. **`CODE_REV` 是模块级常量**（`qmtide_bridge.py:116`），只有**正在运行的那份代码**才会把它写进 `heartbeat.json`；实测 `rev` = 磁盘值 ⇒ 运行版 = 打过补丁的版本。
2. **磁盘 mtime（11-09 11:24:58）< 进程启动时刻（11-10 09:17:46）** ⇒ 进程加载的一定是新文件（不存在「改了但跑旧的」）。
3. **行为判据**：修前 4 次 183 = 4 次断线（一一对应）；修后 1 个完整交易日 + 今天半个上午 = **0 次 183、0 次重连**。

**引擎侧那一半也在运行版里**：`core/broker_bridge.py:50-69` 的 `_atomic_write_json` 8 次退避（0.02+0.04+…+0.16 = 0.72 s），文件 mtime `2026-11-08 02:18:23` < 进程启动 `11-10 11:34:58` ⇒ 生效。

### 5.3 残余风险（明确列为「已知未知」，与 INBOX 一致）

- 重试总预算 0.72 s，仍远小于请求超时 10 s ⇒ 预算充足；但**若 8 次全部失败**，`result.json` 依旧丢失，引擎仍会等满 10 s 再判超时（即 183 的**症状**仍可能以「偶发 10 s 卡顿」的形式出现，只是不再中断到重连）。
- `os.replace` 在给足 `FILE_SHARE_DELETE` 时仍报 WinError 5 —— **根因未解释**（Defender / 索引器 / QMT 自身瞬时占用）。INBOX 已明确「不深挖」。

### 5.4 修复代价与落地

**无需任何动作**。本项只作为后续改动的前置事实：**「改完 = 上线」这句话在 11-09 已经被验证过一次（183 修复），也在今天被证伪过一次（12:02 的 event_engine 改动没进 11:34:58 的进程）**。见 §6.2。

---

## 六、本次新发现（不在原任务清单内）

### 6.1 【P0·今天下午就会烧钱】`owner_order` 的 SELL 通道忽略 `lots`，且把成功记成失败

| 项 | 内容 |
|---|---|
| 代码 | `core/owner_order.py:348-355`：`ok = bool(handle_sell(sig, pos, now))` —— **不传 qty、不传 force**；`engine/event_engine.py:3830` 的 `_handle_sell` 用 `pos.quantity`，只被 `available` 夹紧 |
| 现状 | `owner_order.enabled = True`，`max_amount = 1000.0`（`config/settings.py:665-669`）；`storage/owner_orders.jsonl` 里已有一条 `probe-20261110-1`（`side=HOLD`，会被 `decide()` 判 reject，不产生委托） |
| 后果 A | OWNER 下 `SELL 100`（510050）⇒ 引擎实际提交 **`SELL 500`**（`min(600, available=500)`）≈ **1,447 元**，是意图的 **5 倍**（超额 400 股 × 2.895 ≈ **1,157 元 = 1.91 pt**） |
| 后果 B | `_handle_sell` 成功提交后仍 `return False`（`:3874`）⇒ `owner_order.py:367-370` 记 `reject: 落单口未成交`，`owner_orders_done.json` 与事实相反（OWNER 会以为没下出去） |
| 后果 C | 若下 `SELL 600`：夹到 500 提交 ⇒ 触发 §4.3(i) 的 SELL 风暴（43 s 关死买入端） |
| 今天 13:00 前的最小处置 | **向 OWNER 明确「今天不要用指令单测 SELL」**。改 `settings.py:666 enabled=False` **无效**（改 settings 必须重启引擎；进程启动 11:34:58 < settings mtime 10:22:38 本身没问题，但改文件不会进运行内存） |
| 收盘后修法 | `run_step(...)` 增加 `handle_sell(sig, pos, now, qty=?)` 或新增 `_handle_sell_qty`；同时修返回值语义（区分「已提交」与「已成交」）。改动 ≈ 5 行 + 2 条测试 |

> 注：`core/owner_order.py` mtime = `11:35:19`，而引擎 PID 26576 启动于 `11:34:58`。但该模块是**函数内惰性 import**（`event_engine.py:2727` `from core.owner_order import run_step`），首次调用发生在 11:30–13:00 的非交易时段之外（即 13:00 后第一轮）⇒ **13:00 起加载的是 11:35:19 的版本**（含 `mock 行情整条通道不动作` 那条修复）。此判断是**推断**（依赖「11:30–13:00 期间 `_run_once` 未执行」），已列入未验证项。

### 6.2 【P1】「部署 ≠ 生效」今天又发生一次

| 项 | 值 |
|---|---|
| 运行版引擎 | PID **26576**，`main.py --web`，启动 **11:34:58**（`Get-Process` StartTime + `logs/engine.pid`） |
| `engine/event_engine.py` mtime | **12:02:00**（晚于进程启动 27 分钟） |
| HEAD commit | `1b65e97 2026-11-10 12:05:32 fix(engine): 行情从 mock 恢复后强制重建日线特征` |
| ⇒ 结论 | **今天这条 mock 恢复修复不在运行版里**。今天 11:35 之后引擎仍按旧逻辑跑（也就是 INBOX 11-10 11:58 那条 P1 的修法尚未生效） |
| 对本报告的直接影响 | 任何现在写进 `engine/`、`core/`、`config/` 的改动，**在 OWNER 重启前一律不生效**。收盘后落地的交付清单必须显式包含「重启引擎」+ 重启后核对「源码 mtime < 进程启动时刻」 |

### 6.3 【P1·文档】README §十二 #1 的因果归属需要改写

三条都要改：
1. 「`passorder` 成功但拿不到 `order_id`」——补一句：**这在文件桥下是默认路径**（`qmtide_bridge.py:823`），不是偶发；
2. 「30 秒可刷满 `max_daily_trades`」——限定为 **SELL 侧**（每轮 4.28 s）；BUY 侧是 **50 分钟**（300 s 节流 + `held_codes`）；
3. 「下一轮 → 信号重现 → 二次下单」——BUY 侧是「下一个 5 分钟入场窗口」，不是下一轮。

---

## 七、建议修复顺序（按元/年 ROI 排序）

| 序 | 项 | 收益 | 成本 | 风险 | 落地窗口 |
|---|---|---|---|---|---|
| **1** | **OWNER 指令单 SELL 语义（§6.1）** | 单次避免 1,157 元超额（1.91 pt）；恢复通道可信度（账本不再反着记） | 5 行 + 2 测试 | 低（新通道，无历史包袱） | **今天先靠「不测 SELL」止损**；代码收盘后 + 重启 |
| **2** | **unknown 三态 + 计数口径 + 退避（§1.6）** | **979–4,900 元/年（1.6–8.1 pt/年）**；唯一「一次事件关死当日买入端」的机制 | 约 40 行（`place_order` + 两处判断 + 退避状态机）+ 6 条测试 | **中高**：改 live 唯一落单口 | 收盘后 + 重启；**上之前必须先用一笔 100 股真实小单标定 p_u** |
| **3** | **T+1 影子批次 / `avail<=0` 不再整仓提交（§4.5）** | 关掉「卖不掉 + 关死买入端」的共同入口；一次性 0.84 pt（残仓占槽） | 10–20 行 + 3 测试 | 低 | 收盘后 + 重启 |
| **4** | **断链 > 60 s 告警（§2.4）** | 0 元/年，但尾部 9.5 pt 的唯一预警 | 3 行 | 极低 | 收盘后 + 重启 |
| **5** | **183（§5）** | 已 PASS，无需动作 | 0 | — | — |
| **6** | README §十二 措辞修正（§6.3） | 防止后人修错地方 | 纯文档 | 无 | 随时（不需重启） |

**落到今天 13:00–15:00 的硬约束**：以上 1–4 全部需要重启才生效 ⇒ **今天下午唯一能做的实质防护是「不下 SELL 指令 + 不卖 510050」**。这也是本报告最需要 OWNER 现在就知道的一句。

### 7.1 修完之后必须补的守卫测试（反向断言，逐条给断言对象）

1. `test_bridge_unknown_does_not_count` —— 桩 broker 返回 `{"ok": True, "order_id": None, "submitted": None}` ⇒ 断言 `_daily_trade_count` **不变**；
2. **正向对照** —— 返回 `{"ok": True, "order_id": "108..."}` ⇒ 断言计数 **+1**（防「一律不计数」式假绿）；
3. `test_unknown_sell_still_dedups` —— unknown 卖出后 `_pending_sells` **必须有该 code**（防「不登记 pending」这个更危险的版本）；
4. `test_unknown_sell_retries_within_backoff` —— 300308 在 `px/cost ≤ −18%` 且连续 unknown 时，**≤ 300 s 内必须有下一次 `_handle_sell`**（钉死「退避不得变成永久封死」）；
5. `test_no_daily_cap_on_backoff` —— 断言退避状态里**不存在**「每日次数上限」这类只增不减的计数（INBOX `FORBIDDEN` 项）；
6. `test_sell_never_sends_more_than_available` —— `available=500, quantity=600` ⇒ 断言发给 `place_order` 的 qty = **500**（钉死 §4.2 的夹紧）；
7. `test_available_zero_does_not_submit_whole_position` —— `available=0` ⇒ 断言**不提交**（或提交被显式标记，二选一，必须先定语义）；
8. `test_owner_order_sell_respects_lots` —— `lots=100` 且持仓 600 ⇒ 断言提交 qty = **100**。

---

## 八、未验证项（诚实清单，不得用「应该/大概」替代）

| # | 未验证的事 | 为什么没验 | 怎么验（成本） |
|---|---|---|---|
| 1 | **p_u = 经桥落单出现 `ok=True` 且 `order_id` 空的真实概率** | live 经桥委托 **0 笔**（`orders` 止于 11-03）。README 里所有关于幻影单的频率论断都缺这个数 | 用 OWNER 指令单下 **1 笔 BUY 100 股 510050（≈290 元）**，看 `orders.order_id` 是否为空 + `[BRIDGE] 委托号反查成功` 是否出现。代价 ≈ 290 元 + 一次真实成交 |
| 2 | **`QUERY_ORDER` 在「有 1 笔在途单」时是否返回该单** | `docs/QMT_BRIDGE.md` §八 明确标 ❌ 未实测（`ORDER count=0`）；我没有任何能产生在途单的只读手段 | 同上，一笔小单即可标定；`bridge_probe.py` 的 `QUERY_ORDER` 段也能给字段名 |
| 3 | **`pos.available` 在进程内的实际值** | 我没读进程内存（`/api/snapshot` 不暴露 `available`）。§4.2 的结论由「`result.json` 回 500」+「11-03 后无 `avail<=0` 告警」**间接推出** | 在 `/api/snapshot` 的 positions 段加 `available` 字段（**只读展示**，1 行），或读 `/api/gate_detail` |
| 4 | **10-30 真实成交是否只有 2 笔** | DB `fills` 是引擎写的唯一事实源；券商流水/对账单未纳入本次取证（无读取通道） | 券商端对账单或 QMT `QUERY_TRADE` 历史 |
| 5 | **E[入场笔/日] = 0.18 与 E[每笔 +0.84%]** | 取自 R38 的 IS 面板（`reports/EVOLUTION_DECISIONS.md`），**本次未复跑**（13:00–15:00 禁止重 CPU） | 收盘后跑一次 `strategy/_evolve_wf.py` 取笔数与累计收益 |
| 6 | **P(510050 年内在 −18% 触发) ≈ 5%** | 纯假设，未做任何波动率/几何布朗建模 | 若需要入账，用 2 年日线做 barrier-crossing 估计（收盘后） |
| 7 | **`core/owner_order.py` 在运行进程里是哪个版本** | 惰性 import 的时刻取决于「11:30–13:00 是否有 `_run_once`」，我无法从外部观测 | 13:00 后看日志是否有 `[OWNER指令] 行情源=mock，本通道整体跳过` 这类新代码专有字符串；或查 `sys.modules` 不可行 ⇒ 只能行为观察 |
| 8 | **`_pending_sells` 占位（空 oid）是否会被 `_reconcile_pending_orders` 误处理** | 目前代码里 `_pending_orders` 以 oid 为键，空 oid 会与 `""` 冲突；**这是 lead 补丁形状 ② 的落地障碍，必须在实现时先定** | 设计阶段确定：占位应存在**独立的** `_pending_sell_placeholder: set[code]`，不复用 `_pending_orders` 字典 |

---

## 十、三态补丁形状（lead 2026-11-10 13:0x 裁决，本节为准）

> 本节是**唯一**的补丁形状定义。§1.6 是我的初版评估，lead 已采纳其中「占位 pending」一条并改成下面这版；**实现时以本节为准，§1.6 只作为理由留存**。

### 10.1 三态定义（`core/broker_bridge.py::place_order` 的返回值）

| 字段 | 语义 | 取值规则 |
|---|---|---|
| `submitted` | **信号已到达桥并调用了 passorder**（不含任何「柜台是否受理」信息） | 桥的 `_request` 有应答 ⇒ `True`；超时/写 signal 失败 ⇒ `None`（**禁止用 `False` 表示「不知道」**，INBOX 2026-11-09 已钉） |
| `ok` | **受托且拿到了 `order_id`** | 仅当 `oid` 非空时 `True` |
| `order_id` | 反查/回填得到的委托号 | 优先桥返回值 → `_attribute_order_id` 反查 → `""`/`None` |
| `unknown` | 派生态，非独立字段：`submitted is True and not ok` | 不落成新字段，避免第四态 |

计数口径固定为 `res.get("submitted", True) is True`（**默认 `True`**）。
理由（lead 已确认，我复核同意）：`QMTBroker`（xttrader 路径）的 `place_order` 返回里**没有** `submitted` 键，若默认取 `False`，切回 xttrader 时计数恒不增、`max_daily_trades` 整体失效——这是比幻影单更严重的失效。

### 10.2 `unknown` 时的动作表（逐条对应代码行）

| 动作 | 现行代码 | 改后 | 理由 |
|---|---|---|---|
| 计数 | `:3664` / `:3862` `if res.get("ok")` 内 +1 | **改到 `submitted is True` 下 +1**（即 unknown 也照抄计数一次） | 与 lead 裁决一致：`unknown` 可能真成交了，不计数会让「真成交却零计数」，反而掩盖问题 |
| 乐观建仓 | `:3668-3676` 建 `_pos_meta` + `_positions` | **不建仓** | 幻影仓是 §3 链条的第 ② 步，去掉它链条就断在这里 |
| SELL 去重占位 | `:3867-3870` 仅 `_oid` 非空时 `_register_pending_order` | **unknown 时立即** `self._pending_sells[code] = "UNK:<code>:<epoch>"` | 让 `:3795` 那道**已有**的去重闸**当轮**生效（不是 30 s 后）。复用既有闸，不新增拦截逻辑 ⇒ 不违反 INBOX `FORBIDDEN` |
| 重试 | 无（每轮重发） | 占位 + 既有 `ORDER_TIMEOUT_SEC`（SELL 30 s / BUY 90 s，`:531`）自然解 | **不引入新计数器、不做「同 code 同方向每天 N 次」** |
| 记录 | 只 `logger.warning` | `orders` 行 `order_id=''` + 一行可 grep 的 `reason` | 事后可对账 |

### 10.3 实现时必须先定的一个结构问题（§八 #8 的答案）

`_pending_orders` 是 **以 oid 为键的 dict**（`:3354`），空 oid 会与 `""` 相撞。所以：

- 占位**不得**复用 `_pending_orders`；只写 `_pending_sells[code] = "UNK:..."`（纯字符串标记，`_register_pending_order:3359-3360` 的既有消费者只读 `code in _pending_sells`，不解析值）；
- 占位需要携带 `side` / `qty` / `ts` 才能支持 §11 的成交回填 ⇒ 新增一张**旁挂表** `self._pending_unknown[code] = {"side","qty","ts"}`，与 `_pending_sells` 同生同灭（`:3428` / `:3461` 的两处 `pop` 要同步清理）；
- 跨日必须清（`_reset_daily_if_needed:1165-1168` 已经清 `_pending_sells`，把旁挂表加进同一段）。

### 10.4 落地成本与风险

| 项 | 估 |
|---|---|
| 代码量 | `core/broker_bridge.py`（`place_order` 返回值 + `submitted`）≈ 15 行；`engine/event_engine.py`（两处计数口径 + 移除乐观建仓 + 占位 + 旁挂表）≈ 25 行；共 ≈ 40 行 |
| 测试 | 必补 8 条，逐条断言对象见 §7.1 |
| 风险 | **中高**：改的是 live 唯一落单口。缓解：`submitted` 默认 `True` 保证 xttrader 路径逐位不变；`ok` 语义不变（仍是「有 oid」），因此所有既有 `if res.get("ok")` 的其他消费者行为不变 |
| 能否收盘后安全落地 | ✅ 可以。**但必须重启引擎**（PID 26576 启动 `11:34:58` < `event_engine.py` mtime `12:02:00`，见 §6.2） |
| 前置条件 | **先用一笔真实小单标定**（见 §12.3）：p_u 与 `QUERY_TRADE` 是否非空，两者决定 §12 的成交回填要不要一起上 |

---

## 十一、`owner_order` 5× 超卖：完整因果与修复点

### 11.1 五步因果（每步可指到行）

| 步 | 机制 | 位置 |
|---|---|---|
| ① OWNER 写一条 `{"id","code":"510050.SH","side":"SELL","lots":100}` 到 `storage/owner_orders.jsonl` | 队列格式见模块 docstring | `core/owner_order.py:23-35` |
| ② `decide()` 只校验「lots ≤ 持仓」，**不把 lots 传出去**（它返回的 `qty` 在 SELL 分支被丢弃） | SELL 分支 `return ST_SUBMIT, "", lots` | `core/owner_order.py:250-260` |
| ③ 落单时只传 3 个参数，**qty 与 force 都没传** | `ok = bool(handle_sell(sig, pos, now))` | `core/owner_order.py:353-355` |
| ④ `_handle_sell` 用**持仓量**而不是订单量，只被券商可用量夹紧 | `qty = int(pos.quantity)` → `qty = min(qty, avail)` | `engine/event_engine.py:3830-3833` |
| ⑤ 结果：`min(600, available=500) = 500` ⇒ **提交 SELL 500**，而意图是 100 | 券商权威可用量 | `C:\pazq_qmt_bridge\result.json` 11:35:39：`510050.SH quantity=600, available=500` |

⇒ **超额 400 股 × 2.895 ≈ 1,157 元 = 1.91 pt**（单次）。

### 11.2 两个附加缺陷（同一处代码）

| 缺陷 | 证据 | 后果 |
|---|---|---|
| `_handle_sell` 在 live 提交成功后**恒返回 False** | `:3861` 进 `if res.get("ok")` → `:3874 return False`（无论有没有 oid） | `owner_order.py:359-370` 把所有成功委托记成 `reject: 落单口未成交`；`owner_orders_done.json` 与事实相反 ⇒ OWNER 会以为没下出去而重复下令（虽然 id 幂等会拦住，但他会去券商端手动补） |
| 若下 `SELL 600` | 夹到 500 ⇒ 剩 100 股（今日买入、`available=0`）⇒ 触发 §4.3(i) 的 SELL 风暴 | 43 s 后 `daily_trade_count=10`，当日买入端关死 |

### 11.3 修复点（3 处，共 ≈ 5 行）

1. `core/owner_order.py:353-355`：SELL 也要能把数量传下去 ⇒ 给 `_handle_sell` 增加可选形参 `qty=None`（默认 `None` ⇒ 用 `pos.quantity`，**既有 5 个调用点行为逐位不变**），owner_order 传 `qty=decide()` 返回的 `lots`；
2. `engine/event_engine.py:3874`：live 分支区分「已提交」与「已成交」——保持 `return False`（契约不变，因为「卖出是否生效」的语义确实是「未成交」），但让 `owner_order` 改读 `res`/新增回执，而不是靠返回值判断「有没有下出去」；
3. `_handle_sell` 的 `avail <= 0` 分支（`:3834-3839`）→ 见 §12.2 的零风险替代。

### 11.4 今天下午的处置（已与 lead 对齐）

- `storage/owner_orders.jsonl` 当前只有一条 `side="HOLD"` 的自检探针（非法字段 ⇒ 只会 `reject`，不产生任何委托）⇒ **无待执行 SELL**，风险是「OWNER 若下 SELL」才触发。
- 改 `settings.py::owner_order.enabled=False` **无效**（改文件不进运行内存，必须重启）。
- ⇒ **交付给 OWNER 的一句话：今天 13:00 后不要用指令单测 SELL；要测就测 BUY（`max_amount=1000` 已经很小）。**

---

## 十二、成交回填（无 oid 时怎么把 `fills` 补回来）——两个候选的裁决

### 12.1 裁决：**(a) 选它，但今天不上；必须先用一笔小单标定。同时 (b) 明确拒绝。**

**先说一个改变结论的事实**（我在核代码时发现的，lead 提问时还未纳入）：**DEAL 记录本身就带真实委托号**，桥已经把它取出来了：

```
qmt_bridge/qmtide_bridge.py:710-726  _q_trades()
720:  "order_id": str(_attr(t, ["m_strOrderSysID", "m_strOrderID"], "")),
```

⇒ 所谓「无 oid」只是**ORDER 列表**里拿不到，**成交明细里是有的**。所以候选 (a) 的正确形状不是「用 (code, side, qty, time) 猜一笔成交」，而是：

> **用 DEAL 记录里的真实 `order_id` 回填占位，然后走完全未经改动的既有流水线**：
> `_apply_trade` 认领 → `_register_pending_order` → `_reconcile_pending_orders` 落实 `fills` → `_mark_sold_today` → `_backfill_open_date` 下次重启能用。

这条形状**不制造任何新的事实**：fills 行的 oid 来自柜台，可回溯、可对账。它唯一做的推断是「这笔真实成交属于那个占位」，而推断条件可以做得极窄（见 12.3）。

| | (a) 扩展 `_apply_trade` | (b) 持仓差额反推 |
|---|---|---|
| 数据来源 | `QUERY_TRADE`（**桥模式下从未返回过非空列表**，未实测） | `QUERY_POSITION`（**今天 11:35:39 刚有真实 payload，已验**） |
| 能否恢复 `_backfill_open_date` | ✅ 完全恢复（fills 行带真实 ts） | ✅ 能恢复，但 ts = 检测时刻，比真实成交晚 1 轮（对 T+1 无影响，对 30 分钟建仓保护期有 ≤1 轮误差） |
| 能否恢复 `_mark_sold_today` | ✅ 走既有 `:3429`，语义不变 | ⚠ 需要在 `_sync_broker_positions` 里额外判断，且**无法区分卖出与其它减少** |
| 误匹配风险来源 | ①同一 code 同时有 BUY/SELL 两个占位；②OWNER 手工交易恰好落在占位窗口内；③`traded_time` 格式未实测（`m_strTradeTime` 可能是 `"093015"` 或 `"09:30:15"`，无法直接与 epoch 比较） | **①OWNER 手工交易 = 引擎从未下过的单 ⇒ 被伪造成 fills 行。这不是理论风险，今天就有活样本**：`510050` 券商 600 / 本地 500，差额 100 股是 OWNER 手工买的（`storage` 今日 `orders=0/fills=0` 已证） |
| 最坏后果 | 一笔真实成交被记到错误的占位下（金额/数量仍真，只有归属错） | **伪造成交 + 伪造已实现盈亏**：`_apply_trade:3387-3393` 在 SELL 时会调 `risk.on_fill(...)`，而该值直接喂 **日内亏损熔断**（`risk/manager.py:222-229`）⇒ 一笔假 fill 可以触发真实 halt。这正是 2026-10-30 那次假 `−86.58%` 强平的同类事故 |

⇒ **(b) 必须拒绝**：它把「未知」变成「凭空生成一笔成交」，而伪造成交的后果会穿透到风控熔断层。lead 的「宁可留 bug」在我这里得到同向结论，而且理由比「可能有误匹配」更硬：**它会污染熔断输入。**

### 12.2 零风险替代（今天就能上，3 行，不碰 fills）

**核心洞察：`available` 本身就是 T+1 的权威事实，所以「fills 缺失 ⇒ T+1 失效」这条链不需要靠恢复 fills 来断。**

券商回的 `available`（`510050: quantity=600, available=500`）已经精确表达了「有 100 股今日买入不可卖」。`_handle_sell:3832-3833` **已经在用它夹紧**（⇒ 提交 500 而不是 600）。唯一的漏洞是 `avail <= 0` 那个分支（`:3834-3839`）选择了「按整仓提交」：

```python
# 现在
elif self.exec_mode == "live" and avail <= 0:
    qty = int(pos.quantity)        # 整仓提交 ⇒ 券商必拒 ⇒ 桥回 ok=True ⇒ 风暴
# 改成
elif self.exec_mode == "live" and avail <= 0:
    # 券商权威可用量为 0 ⇒ 这笔在柜台必然被拒（2026-10-30 11:24:56 error_id=306050 实证）
    # 提交只会制造幻影单 + 风暴，故不提交，每日每 code 告警一次
    self._log_reject_once(pos.code, "avail0")
    return False
```

三段行为（对照 §4.3）：**round1 卖 500（正确）→ round2 `avail=0` 不提交 + ERROR 告警（无风暴）→ 次日 `avail=100` 卖出剩余 100**。不伪造任何数据、不新增拦截器、仍允许今天就把可卖的那 500 股卖掉。

**它修不了什么（诚实边界）**：`fills` 仍然缺行 ⇒ `_backfill_open_date` 仍会给 510050 返回 11-03；但那时 `open_date` 已经**不再是安全关键路径**（T+1 由 `available` 兜住、保护期失效的代价是 30 分钟内可能早卖，而早卖被 `available` 夹紧后最多卖出可卖部分）。`_mark_sold_today` 仍不恢复 ⇒ 观察篮（`manual_entry_codes=['510050.SH']`）在同日卖出后**可能当日补买** ⇒ 需在报告与 INBOX 里记为**已知缺陷**。

### 12.3 把 (a) 变成可上补丁所需的一次标定（成本 ≈ 290 元）

1. 用 OWNER 指令单下 **1 笔 `BUY 110 股 510050`**（`max_amount=1000` 内，约 290 元）——**注意选 BUY，不要选 SELL（§11）**；
2. 同时观察三个事实：
   - `storage/qmt.db::orders` 这一行的 `order_id` 是否为空（⇒ 标定 p_u）；
   - `boot.log`/`cmd_system.log` 有无 `[BRIDGE] 委托号反查成功`（⇒ ORDER 反查是否可用）；
   - **`QUERY_TRADE` 是否返回该笔（`order_id` 是否非空）**（⇒ (a) 的可行性）；
3. 判据：
   - `QUERY_TRADE` 返回且 `order_id` 非空 ⇒ **(a) 安全**，(a) + 三态补丁可以同批上（≈15 行）；
   - `QUERY_TRADE` 空/oid 空 ⇒ **成交回填在这套桥下不可恢复** ⇒ 今天不修，把「live 无 fills」写成已知缺陷，(c') 为唯一现实解。

> **给 lead 的直接回答**：两个候选里 **(a) 更能恢复 `_backfill_open_date` 与 `_mark_sold_today`（它走既有流水线，语义不变）**；**(b) 的误匹配风险更大且更危险（伪造成交 ⇒ 污染熔断）**。但 (a) 仍依赖一个**未实测**的查询，所以我建议：**今天不修这条，只记录为已知缺陷**，同时上 (c') 那 3 行（零风险、不碰 fills、关掉风暴入口）。这与你的「宁可留 bug」一致，并且多出来的那 3 行不引入任何新的事实来源。

---

## 十三、WinError 183 的 PASS 验收表（把 §5 的结论做成可勾选项）

> 判据全部**独立**（不靠心跳倒推）。任何一条 FAIL ⇒ 不得宣布修复生效。

| # | 判据 | 期望 | 实测 | 结果 |
|---|---|---|---|---|
| 1 | 运行进程自报的 `rev` = 磁盘 `CODE_REV` | `2026-11-09T1125-write-retry` | `heartbeat.json`: `rev=2026-11-09T1125-write-retry`, `pid=18180`（12:26 读） | **PASS** |
| 2 | 源码 mtime **早于** 进程启动时刻 | mtime < StartTime | 源码 `2026-11-09 11:24:58` < `XtItClient.exe` 启动 `2026-11-10 09:17:46` | **PASS** |
| 3 | 补丁后新增 `write failed` 次数 = 0 | 0 | `boot.log` + `boot.log.1` 全文 grep = 0（11-09 11:37:24 → 11-10 12:26） | **PASS** |
| 4 | 补丁后新增断线 = 0 | 0 | `券商重连成功` 0 次；心跳 `券商重连=1` 未递增；今日 `broker_reconnects` 无变化 | **PASS** |
| 5 | 引擎侧写侧重试也在运行版里 | 8 次退避 | `core/broker_bridge.py:50-69`，mtime `2026-11-08 02:18:23` < 引擎启动 `11-10 11:34:58` | **PASS** |
| 6 | 对照：补丁**前**确有失败 | 4 次 = 4 次断线 | 11-09 10:03:24 / 10:23:33 / 10:38:41 / 11:27:30，`broker_reconnects` 1→2→3→4→5 | **PASS（正向对照成立）** |
| 7 | 已知未知（不得声称已解决） | 根因未解释 | `os.replace` 在给足 `FILE_SHARE_DELETE` 时仍 WinError 5；重试预算 0.72 s / 超时 10 s | **记录在案** |

**结论：183 主修已在运行代码中生效（7 项全 PASS/记录），本项无需任何动作。**

---

## 十四、附录：本次取证用到的全部可复核命令

```powershell
# 环境（缺 PATH 前缀会 ImportError: DLL load failed ... _sqlite3）
$env:PATH = "C:\Users\lisinan\.conda\envs\qmt\Library\bin;C:\Users\lisinan\.conda\envs\qmt\DLLs;" + $env:PATH
$env:PYTHONIOENCODING = "utf-8"

# 1) live 账本（严格只读）
python logs/_tmp/t1_db_orders.py        # orders/fills/engine_state/equity_snapshots
# 2) 日志里的 [LIVE] 下单行 → 逐日 ok/fail/oid 统计（本报告 §1.3 那张表）
python logs/_tmp/t1_live_orders.py
# 3) 桥的运行版本（§5 的关键判据）
Get-Content C:\pazq_qmt_bridge\heartbeat.json -Raw      # rev 必须 = qmt_bridge/qmtide_bridge.py:116 的 CODE_REV
Get-Process -Id 18180 | Select-Object Id,ProcessName,StartTime   # 进程启动时刻 vs 源码 mtime
# 4) 运行版引擎到底跑的是哪份代码（§6.2）
Get-Process -Id 26576 | Select-Object StartTime
Get-ChildItem engine,core,config -Filter *.py -Recurse | Where-Object { $_.LastWriteTime -gt (Get-Date "2026-11-10 00:00") }
```

**一次性结论（给后来人）**：本报告里凡是标了「实测」的数字，都能用上面的命令在 1 分钟内复现；凡是标了「假设/推断/未验证」的，请不要在下游文档里把它写成事实。

# A3 只读取证：`place_order` 失败后引擎会不会重发、会不会重复下单

日期：2026-11-09　性质：**只读取证**（未改一行代码）　用途：向 OWNER 汇报

## 结论（一句话）

**会。**「不重试」这个结论只在单个函数内部成立：`_handle_buy` / `_handle_sell`
里各只有**一次** `place_order` 调用，没有循环。但引擎主循环每 **3.015 秒**重跑
一遍同一套入场/退出判定，而**失败路径上没有任何状态被封存**，于是同一只票每轮
都重新下单，且**每次都是新的 `signal_id`** —— 桥侧「按 `signal_id` 幂等」因此
完全失效。

⇒ 一旦发生「QMT 侧已成交、但 result 没回到引擎」（超时/写失败），下一次重发
就是**真实的重复下单**。**该风险今天尚未发生**（今日桥接模式 `place_order`
超时 0 次），但链路是通的。

## 一、代码证据（file:line）

### 1. 单次调用，函数内无重试

| 位置 | 事实 |
|---|---|
| `engine/event_engine.py:3592` | 买入：`res = qmt_broker.place_order(...)`，**全函数唯一一次** |
| `engine/event_engine.py:3793` | 卖出：同上，唯一一次 |
| `grep -n place_order engine/event_engine.py` | 全文件只有这两个调用点 |

### 2. 失败路径不封存状态 ⇒ 下一轮必然重来

| 位置 | 事实 |
|---|---|
| `engine/event_engine.py:3607` | `if res.get("ok"):` 之后才建 `_positions[code]` / `_pos_meta` / `_register_pending_order`。失败 ⇒ **仓位不存在**、`_pending_orders` 无记录 |
| `engine/event_engine.py:2822` | `held_codes = {c for c, p in self._positions.items() if p.quantity > 0}` |
| `engine/event_engine.py:2875` | `if code in held_codes: continue` —— 买入失败后该票**仍不在** `held_codes`，下一轮照旧进候选池 |
| `engine/event_engine.py:3805-3818` | 卖出：`if res.get("ok"):` 才 `_register_pending_order`；失败直接 `return False`，`pos.quantity` 未变、`_mark_sold_today` 未调 |
| `engine/event_engine.py:3303-3304` | `if side == "SELL": self._pending_sells[code] = str(oid)` —— 只有登记过 oid 才写 |
| `engine/event_engine.py:3739` | `if pos.code in getattr(self, "_pending_sells", {}): ... return False` —— 这条「防重复卖」**只对 `ok=True` 且拿到 oid 的路径有效**，超时/失败路径**完全不受保护** |

### 3. 每次重发都是新 `signal_id` ⇒ 桥侧幂等失效

| 位置 | 事实 |
|---|---|
| `core/broker_bridge.py:213` | `sid = uuid.uuid4().hex` —— **每调用一次 `_request` 就生成一个新 id** |
| `core/broker_bridge.py:404-408` | `place_order` → 恰好一次 `_request`；`before = self._order_id_set(account)`（:402）**只用于事后反查委托号，从不用于抑制重复下单** |
| `qmt_bridge/qmtide_bridge.py:929` | QMT 侧 `sid = str(sig["signal_id"])`，`_seen`/`_processed` 全部按 id 去重 |
| `qmt_bridge/qmtide_bridge.py:89-98` | `_seen` 是「处理过的 id 黑名单」，**同 id 才拦**；不同 id 一律重新执行 |

⇒ 「本地超时重发不会造成重复下单」（`core/broker_bridge.py:23` 的设计初衷）
**只在重发沿用同一 id 时成立**，而现状是**每次重发都换 id**。

## 二、实盘证据（不是推断）

`logs/notices.log` 里「提交卖出委托」共 660 条，其中：

```
09-19 ~ 11-03 各类合计 18 条（正常）
2026-10-30  SELL → False  642 条   ← 同一只 300308.SZ、同一账户
2026-10-30  SELL → True    12 条
```

`2026-10-30 09:30:53 → 11:29:59`（跨 119.1 分钟）654 条，相邻间隔分布：

```
3.0 秒 × 629 次   ← 就是主循环周期
30.0/31.0 秒 × 10 次
其余零星
```

样本（连续 5 条）：`09:31:10 / 09:31:13 / 09:31:16 / 09:31:20 / 09:31:23`
—— 每 3 秒一次，同一方向、同一数量、价格随之变动，`→ False`。

引擎自己的代码注释也承认这件事：

- `engine/event_engine.py:3802-3804`：「失败卖单会无限重试，原实现每 3 秒吃掉
  一个额度 ⇒ 一上午就刷穿 `max_daily_trades`」。
- `engine/event_engine.py:3602-3605`：「实盘首日 522 笔失败卖单把计数刷到 497」。

（10-30 的修复只让**失败的委托不再消耗日内额度**，**没有**停止重发本身。）

## 三、必须同时说清的限定（避免被读成「已经在重复成交」）

1. **10-30 那批不是桥接模式**：桥接 commit `3768b1e` 的日期是 **2026-11-08**，
   10-30 走的是 `QMTBroker`(xttrader)。那 642 条失败原因是 QMT 侧**拒单**
   （价格类型等），不是「已成交但回执丢失」，所以**没有**产生重复成交。
2. **今天（11-09）桥接模式下 `place_order` 超时为 0 次**：
   `logs/quant_system.log:115851` 只有一条 `桥接超时 action=QUERY_TRADE`
   （查询，不是下单）。
3. 因此准确表述是：**重复下单的机制已确认存在、已被 642 次实测重发证明；
   但「重复成交」这一步在真实资金上尚未发生。** 触发前提是
   「QMT 侧已成交 + result 回执丢失」，而 11-09 那两次写失败（`WinError 183`）
   正是造出这个前提的那类故障。

## 四、由此推出的两个动作（均已另案交付，未部署）

- **B1**（P0）：QMT 侧写失败重试 + 删掉非原子回退 ⇒ 减少「回执丢失」的发生率。
- **B2**（P1）：引擎读句柄加 `FILE_SHARE_DELETE` ⇒ 掐掉 `WinError 183` 链的起点。
- **未做、需 OWNER 决策**：引擎侧「同一 code 同一方向在途时不再发单」与
  「重发沿用同一 `signal_id`」。前者要动下单主路径（`_handle_buy` /
  `_handle_sell`），后者要动 `_request` 的 id 生成语义（`core/broker_bridge.py:213`），
  都会改变 live 行为，**不在我的权限内**，只列为建议。

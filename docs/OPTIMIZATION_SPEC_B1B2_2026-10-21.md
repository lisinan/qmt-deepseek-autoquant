# B1 + B2 实施规格（委托生命周期 / 卖出侧保护）

> 依据 `reports/OPTIMIZATION_REVIEW_2026-10-21_multimodel.md` §4 的 B1、B2。
> 只改 `engine/event_engine.py`（WP-G），测试由 WP-H 单独补。

## 0. 铁律

1. **paper 路径行为不变**：本包所有新逻辑只在 `exec_mode == "live"` 生效；paper 仍即时全额成交。
2. 不改 `risk/manager.py`、`core/broker.py`（后者已有 `cancel_order_stock` / `get_orders` / `get_trades`）。
3. 最小改动 + 用 read 定位后 edit 局部替换；改完 `py_compile`。
4. 不要跑 `tests/run_all.py`（由 WP-H 统一跑）。

## 1. B2 卖出侧保护

### B2-1 行情陈旧守卫（对称买入）
`_handle_sell` 在 mock 闸门之后、T+1 判断之前插入：
```python
if self._market_stale:
    self._log_reject_once(pos.code, "stale")
    return False
```

### B2-2 待成交卖单防重复
`_handle_sell` 入口（紧跟 B2-1）：
```python
if pos.code in self._pending_sells:
    self._log_reject_once(pos.code, "pending_sell")
    return False
```

### B2-3 卖出量夹紧到券商可用量（仅 live）
把 `qty = pos.quantity` 改为：
```python
qty = int(pos.quantity)
avail = int(getattr(pos, "available", 0) or 0)
if self.exec_mode == "live" and avail > 0:
    qty = min(qty, avail)
if qty <= 0:
    return False
```
若 live 且 `avail <= 0`：按 `pos.quantity` 提交，但**每个 code 每日一次** `logger.warning("可用量字段为 0，按整仓提交（券商侧可能拒单）")`。

### B2-4 日内强平改为「可重试」
- 删除 `_daily_flatten_done` 的一次性语义（保留属性以兼容旧引用或直接移除，二选一但需 grep 全部引用）。
- 新增：`self._flatten_attempts: Dict[str, int] = {}`、`self._flatten_day: str = ""`、`self._flatten_notified: bool = False`。
- 把 `_run_once` 里的强平块改为**每轮**执行（`if self.risk.flatten_requested:`）：
  - 跨日（`self._flatten_day != today`）时清空 attempts / notified；
  - 遍历 `self._positions`：`quantity > 0` 且 `code not in self._pending_sells` 且 `attempts.get(code,0) < self.FLATTEN_MAX_ATTEMPTS(=3)` 的，调 `_handle_sell(..., force=True)` 并 `attempts[code] += 1`；
  - 首次触发（`not _flatten_notified`）时发一条 `system_notice("ERROR","交易", ...)`，之后不重复；全部清空或尝试耗尽后写一条 INFO 日志。
- `_reset_daily_if_needed` 里同步清空这三个字段（新交易日重新计数）。

### B2-5 paper 的 available 维护
- `_reset_daily_if_needed` 跨日时：对每个持仓 `pos.available = pos.quantity`（前一日已过 T+1）。
- paper 新建仓（`_handle_buy` paper 分支）保持 `available` 默认 0。

## 2. B1 委托生命周期（仅 live）

### 2-1 新增状态（`__init__`）
```python
self._pending_orders: Dict[str, dict] = {}   # oid -> {code, side, qty, price, ts, filled, avg_cost, account, cancelled}
self._pending_sells: Dict[str, str] = {}     # code -> oid
self._seen_fills: set = set()                # (oid, traded_time, traded_volume) 去重
self.ORDER_TIMEOUT_SEC = {"BUY": 90, "SELL": 30}   # 类常量
```

### 2-2 新方法
- `_register_pending_order(oid, code, side, qty, price, avg_cost, account)`：登记；SELL 时同时写 `_pending_sells[code]=oid`。
- `_apply_trade(trade) -> int`：按 `(oid, traded_time, traded_volume)` 去重；累加 `filled`；`storage.save_fill(..., order_id=oid, mode="live")`；SELL 时调 `risk.on_fill(...)`（与现逻辑一致）；返回本次增量成交量。
- `_reconcile_pending_orders()`：
  1. `trades = qmt_broker.get_trades("cash")`（None 视为本轮无回报，直接返回）；
  2. 对每条 trade，若 oid 在 `_pending_orders` 且未见 → `_apply_trade`；
  3. 遍历 `_pending_orders` 副本：
     - `filled >= qty` → 完成：SELL 时 `pos.quantity = max(0, pos.quantity - filled)`、从 `_pending_sells` 删除；BUY 时若 `pos.quantity != filled` 则校正为 filled；删除 pending；
     - 否则若 `now - ts > ORDER_TIMEOUT_SEC[side]` → `qmt_broker.cancel_order_stock(account, oid)`，然后**回滚未成交部分**：
       - BUY：`pos.quantity -= (qty - filled)`；若 `<= 0` 则 `pop` 持仓与 `_pos_meta`；
       - SELL：不动数量（未成交即为未卖出），从 `_pending_sells` 删除；
       - `system_notice("WARNING","交易", "委托超时未成交，已撤单 …")`，`logger.warning`；
       - 删除 pending。
  4. 任何异常只 `logger.warning`，不得打断主循环。

### 2-3 接入点
- `_handle_buy` live 分支：`res.ok` 后保留乐观建仓，但改为 `_register_pending_order(...)`，**删除** `_poll_and_record_fill` 线程调用。
- `_handle_sell` live 分支：`res.ok` 后**不再** `self._positions[code].quantity = 0`；改为 `_register_pending_order(...)`，并 **返回 False**（委托已提交但未成交，槽位未释放——调用方据此不补位）。保留 `_mark_sold_today` 仅在成交确认后调用（由 `_reconcile_pending_orders` 在完成时调）。
- `_run_once` live 分支：在 `_sync_broker_positions()` 之后调用 `_reconcile_pending_orders()`。
- `_poll_and_record_fill`：保留函数但标注 deprecated（不再被调用），或删除并 grep 确认无引用。
- `_reset_daily_if_needed`：清空 `_pending_orders` / `_pending_sells` / `_seen_fills`（隔夜未成交委托不应跨日残留）。

## 3. 验收（WP-H 负责测试）

新增 `tests/test_order_lifecycle_b1b2.py`：
1. `_handle_sell` 在 `_market_stale=True` 时返回 False 且未调用 `place_order`；
2. live 卖单提交后本地 `quantity` **不归零**，且同 code 再次卖出被拒（`pending_sell`）；
3. `available=300` / `quantity=1000` 时报单量 = 300；`available=0` 且非 T+1 时仍按整仓并 WARN；
4. 同一 oid 分两笔成交（400+600）→ `filled=1000`，`save_fill` 两次，`risk.on_fill` 两次；
5. 超时未成交 → `cancel_order_stock` 被调用一次、BUY 回滚未成交数量、SELL 数量不变；
6. 强平在同一日内最多重试 3 次（`_flatten_attempts`）；
7. paper 跨日后 `pos.available == pos.quantity`；
8. 全部旧测试保持通过（尤其 `tests/test_event_engine_live.py`、`test_t1_sell_guard.py`、`test_daily_fallback_exit.py`）。

## 4. 实施结果（2026-10-21 晚）

实现包：WP-G（engine/event_engine.py，deepseek-v4-pro）+ WP-H（tests，space-bunny）。
最终：`tests/run_all.py` **572/572 通过**（新增 14 条 B1/B2 用例）。

落地清单：
- B2-1 卖出侧 stale 守卫；B2-2 `_pending_sells` 去重；B2-3 卖出量夹紧 `available`（=0 时整仓 + 每日一次 WARN）；B2-4 强平每轮可重试 ≤3 次/日（行情陈旧时暂停推进，避免烧额度）；B2-5 paper 跨日 `available=quantity`。
- B1：`_pending_orders` / `_pending_sells` / `_seen_fills`；`_register_pending_order` / `_apply_trade`（逐笔去重累计）/ `_reconcile_pending_orders`（成交落实、超时撤单、部分成交回滚）；`_handle_buy`/`_handle_sell` 改为登记委托（不再乐观清仓、不再起 6×1s 轮询线程）；`_run_once` 先对账再同步。

实施期修正（相对本规格）：
1. `_run_once` 顺序改为**先 reconcile 后 sync**（规格原写反，会重复扣减成交）。
2. BUY 超时回滚改为 `pos.quantity = min(pos.quantity, filled)`（原按未成交量相减，遇到已同步校正的数量会算出负数、误删持仓与 `_pos_meta`）。
3. `place_order` 返回 ok 但无 `order_id` 时不登记（避免空 key 进入待成交表），仅告警。
4. `open_date=None` **不做 fail-closed**：以券商 `available` 为 live 侧 T+1 权威（fail-closed 会让 broker 同步进来的历史仓永久不可卖）。详见评审报告 §8。

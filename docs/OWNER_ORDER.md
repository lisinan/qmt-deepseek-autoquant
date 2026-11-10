# OWNER 指令单通道（`owner_order`）

> v1.1 增量能力。**允许对已持仓加仓，也允许卖出** —— 这是全系统唯一能这样做的路径。
> 落单仍走 `_handle_buy` / `_handle_sell`（唯一落单口），风控一条都没放开。

## 一、为什么需要

| 通道 | 能做什么 | 卡在哪 |
|---|---|---|
| 策略信号 | 按动量/趋势自动买卖 | 闸门不开就不买（如 `breadth=1<2`） |
| `manual_entry`（观察篮） | 绕过信号闸门买 | 已持仓的代码 `continue`，且只有金额没有数量 |
| `owner_reentry`（回补） | 按价格区间回补 | 首行 `if 已持仓: return`，只买不卖 |
| 直接写 `signal.json` | 桥会执行 | **绕过引擎账本**：无 `fills`、`open_date` 缺失、T+1 失效、无风控 |

缺口是「我说买/卖 N 股，系统照办并记账」。本通道补这个口。

## 二、数据文件

与账本同目录（live = `storage/`，paper = `storage_paper/`）：

```
storage/owner_orders.jsonl        指令队列（追加，一行一条，# 开头为注释）
storage/owner_orders_done.json    幂等账本 {id: {status, ts, reason}}
```

### 指令字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | ✅ | 唯一键，**幂等的唯一依据**。同 id 永不重复提交 |
| `code` | ✅ | 如 `510050.SH` |
| `side` | ✅ | `BUY` / `SELL` |
| `lots` | 买/卖 | 股数，必须是 100 的整数倍。卖出时不得超过持仓 |
| `amount` | 买 | 金额（元），按现价向下取整到 100 股。与 `lots` 二选一 |
| `price_cap` | 买 | 价格上限。现价高于它就一直等，**不消耗 id** |
| `valid_until` | | `YYYY-MM-DD`（含当天）。过期标 `rejected`，不重试 |
| `note` | | 写进信号 `reason` 与日志，事后追责用 |

## 三、状态机

| status | 含义 | 是否重试 |
|---|---|---|
| `skip` | 条件未满足（无行情 / 未到价格） | **不记账**，下轮再看 |
| `reject` | 明确拒绝（超上限/无持仓/T+1/字段非法） | 不再重试，写 notices |
| `submit` | 买入已交落单口 | 不再提交 |
| `ok` | 卖出确认成交（`_handle_sell` 返回 True） | 不再提交 |

## 四、配置

`config/settings.py` → `STRATEGY_PARAMS` 字典内加一项（**Agent 无写权限，由 OWNER 加**）：

```python
    # ===== OWNER 指令单通道（2026-11-10）=====
    # enabled=False 时通道完全惰性：不读文件、不写日志、对既有行为零影响。
    "owner_order": {
        "enabled": False,        # ← 启用改 True
        "max_amount": 1000.0,    # 单笔金额上限（元）
        "max_per_day": 5,        # 每日指令条数上限
    },
```

> 该字典**每轮热读**，改它不需要重启；但**挂载代码需要重启**（`.py` 非热读）。

## 五、上线步骤

```bash
# 1) 预演（不碰生产文件）
python scripts/patch_owner_order.py                      # dry-run：锚点/编译/反向还原 三道自检
python scripts/patch_owner_order.py --apply --root logs/_tmp/rehearsal_owner

# 2) 应用（会先备份 .bak-时间戳，落盘后再编译一次，失败自动回滚）
python scripts/patch_owner_order.py --apply

# 3) 全量测试
python tests/run_all.py

# 4) 改 settings.py 的 owner_order（OWNER 手工）→ 重启引擎

# 5) 首次实战：1 手，`id` 用唯一值
echo '{"id":"t-20261111-1","code":"510050.SH","side":"BUY","lots":100,"price_cap":3.0,"valid_until":"2026-11-11","note":"首单验证"}' >> storage/owner_orders.jsonl
```

## 六、回滚

```bash
# 删掉挂载段（两处）：搜索 "OWNER 指令单挂载点" 整段删除 +
# 删除 self._owner_order_step(ticks, current_prices) 那一行；或直接
cp engine/event_engine.py.bak-<时间戳> engine/event_engine.py
python tests/run_all.py     # 必须回到 1046/33/1079 同项
```

或把 `owner_order.enabled` 改回 `False` 后重启——**通道惰性，等价于没装**。

## 七、边界（红线）

- 落单只走 `_handle_buy` / `_handle_sell`，**不新开下单路径**
- 熔断 / 日内次数 / 现金夹紧 / 行情新鲜度 / 总资产可信度 / T+1 / 建仓保护期 / `max_positions` 全部照旧
- 单笔金额上限 + 每日条数上限，防止「配置写错把账户刷空」
- 卖出不绕过保护期：`_handle_sell` 返回 False 时记 `rejected`，不重试
- 通道异常被 `try/except` 兜住，**绝不允许拖垮主循环**

## 八、运维约定

- 每天开盘前看一眼 `owner_orders_done.json`，确认昨日指令终态
- `reject` 且 reason 含「T+1」属正常（当日买入不可卖），不是故障
- 指令文件只追加、不修改；作废请用新 id 下达反向指令，不要改历史行

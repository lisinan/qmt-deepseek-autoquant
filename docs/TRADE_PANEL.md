# TRADE_PANEL — 实盘区买卖交易模块

> 2026-11-10 建立 ｜ 入口 `http://127.0.0.1:5000/trade` ｜ 代码 `web/trade_api.py`
> 关联：`core/owner_order.py`（指令通道）、`docs/AUTOMATION_CONTRACT.md`（红线）

---

## 一、它是什么

live 面板上的一个**下单表单**，让 OWNER 用界面而不是手写 JSONL 来下「买/卖 N 股某代码」。

**它不是一条新的下单通道。** 它只做一件事：往既有的
`storage/owner_orders.jsonl` 追加一行，然后由引擎主循环里的
`core.owner_order.run_step` 交给 `_handle_buy` / `_handle_sell`（全系统唯一落单口）。

```
浏览器表单
   │  POST /api/trade/submit（带一次性令牌）
   ▼
web/trade_api.py ──① 校验（复用 owner_order.decide）──► 拒绝则不入队
   │
   ├─② 追加 storage/owner_orders.jsonl（append-only）
   └─③ 审计 logs/owner_trade_ui.log
          │
          ▼
   引擎主循环 _owner_order_step → core.owner_order.run_step
          │  （熔断 / 日内次数 / 现金夹紧 / 行情新鲜度 / 总资产可信度 /
          │    T+1 / 建仓保护期 / max_positions 全部照旧生效）
          ▼
   _handle_buy / _handle_sell（唯一落单口）→ 文件桥 → QMT
```

---

## 二、五条红线（改动前必读）

| # | 红线 | 由谁钉住 |
|---|---|---|
| 1 | **绝不碰 broker**。本模块不 import `qmt_broker` / `core.broker*` / `core.qmt_client` / `xtquant`，也不调 `place_order`/`passorder`/`cancel_order_stock` | `test_module_never_touches_broker`（AST 级静态断言） |
| 2 | **校验只有一份**。提交前预检直接调 `core.owner_order.decide()`，与引擎执行时同一个纯函数 ⇒ 面板说「会被拒」和引擎真拒绝必然一致 | `test_precheck_reuses_engine_decide` |
| 3 | **被引擎会拒的指令不得入队**（否则队列堆满永远不执行的单，人以为已下单） | `test_rejected_order_is_never_appended` |
| 4 | **append-only**，从不改写历史行（历史行是审计证据） | `test_append_preserves_existing_lines` |
| 5 | **取消靠 done 账本**，不改写队列；已记账的 id 不得被"取消"成没下过单 | `test_cancel_only_pending_and_marks_done` |

---

## 三、API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/trade` | 下单页 |
| GET | `/api/trade/status` | 账户/通道/持仓/队列状态（5s 轮询） |
| POST | `/api/trade/token` | 取一次性令牌（120s、单次有效） |
| POST | `/api/trade/submit` | 提交指令，需 `X-Trade-Token` |
| POST | `/api/trade/cancel` | 取消尚未记账的指令，需 `X-Trade-Token` |

### 提交体

```json
{"side":"BUY|SELL","code":"510050.SH","lots":100,
 "price_cap":2.95,"valid_until":"2026-11-11","note":"最多80字"}
```

- `code` 必须 `6位数字 + .SH/.SZ/.BJ`（池外代码允许，但后缀必须合法）
- `lots` 必须为正整数；**是否整百由 `decide()` 判**（SELL 允许等于整仓非整百）
- `price_cap` 可空 = 不限价（买入不追高用）
- `valid_until` 默认今天，不得早于今天
- **不支持** 逐条上调 `max_amount`：`decide()` 支持指令级覆盖，但面板刻意不暴露它 ——
  那等于在网页上开一个「绕过单笔上限」的入口

---

## 四、开关与分区

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `QMT_TRADE_UI` | `1`（开） | 设 `0` 关闭整个模块：`/trade` 与全部 `/api/trade/*` 返回 403 |
| `QMT_TRADE_UI_ALLOW_PAPER` | `0`（关） | paper 区默认禁用；设 `1` 才在 :5001 开启 |

**改完必须重启 live 引擎**：Flask app 在引擎启动时创建（全仓无 `importlib.reload`），
不重启则页面不存在。

---

## 五、两条容易被忽略的行为

### 5.1 资产未同步时**不给数字**

live 引擎刚启动或断链期，内存里 `_cash = INITIAL_CASH（100 万）`、`_positions = {}`
是**构造初值而不是账户真相**。面板用风控层同一个字段 `_broker_total_asset > 0` 判定
「券商资产是否可信」；不可信时现金/总资产返回 `null`、持仓返回空，前端显示
**「未同步券商」** 并出警告条。

理由：对着一个假数字决定真金白银的仓位，是这类面板最危险的失败方式。

### 5.2 mock 行情 / 熔断会显式提示，但不阻断提交

- `data_mode == "mock"`：整条指令通道不动作（`run_step` 首行即返回），
  单子会一直 `pending` ⇒ 面板出警告条。
- `halted == true`：买入会被拒、卖出仍按各自规则判定 ⇒ 面板出警告条。

**刻意不在提交时硬拦**：熔断期 OWNER 仍可能需要卖出减仓，而「提交后由引擎按各自
规则判定」是既有语义，面板不该冒充第二套闸门。

---

## 六、状态含义

| status | 含义 |
|---|---|
| `pending` | 队列里有、引擎尚未处理（会一直重试到 `valid_until`） |
| `submit` | 已交落单口，等待成交确认 |
| `ok` | 已确认成交（清仓语义） |
| `reject` | 被拒，原因见 `reason` |
| `cancelled` | 在面板上取消（记入 done，引擎跳过） |

---

## 七、审计

每次提交/取消追加一行到 `logs/owner_trade_ui.log`（JSONL），含
`ts / event(submit|submit_reject|cancel) / ins / precheck / remote_addr`。

出事后能回答「这单是谁在什么时候从哪台机器点出来的」。

---

## 八、已知限制

1. 面板**不开新仓上限以外的放宽**：所有阈值仍以 `STRATEGY_PARAMS.owner_order`
   （`enabled / max_amount=1000 / max_per_day=5`）为准。要大额需改配置或走 `owner_reentry`。
2. 没有「撤单」：这里的「取消」只作用于**尚未被引擎消费**的指令。已提交到券商的委托
   撤单仍走引擎 `_reconcile_pending_orders` 的超时撤单，不在面板范围。
3. 无鉴权/无 HTTPS：面板只绑 `127.0.0.1`，令牌是防误触与防跨站，不是身份认证。
   不要把该端口暴露到局域网或公网。
4. 队列是 append-only 的纯文本，长期会变大；面板只显示最近 60 条。

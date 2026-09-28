# 盘前就绪检查 2026-09-25（09:06 · PRE-MARKET 闭环）

> 执行器：98ffc7a2-9285-401e-9251-688fb0744f49
> 解释器：`C:\Users\lisinan\.conda\envs\qmt\python.exe` + PATH(Library/bin,DLLs) + PYTHONPATH=.
> 未改动任何策略参数 / 生产配置 / 运行中的 live 进程。

---

## 一、在线状态（检查清单 1）

| 项 | 结果 |
|---|---|
| miniQMT（本地 5000 客户端） | ✅ `health_check --json` → `miniqmt=true` |
| 引擎（Web 5000） | ✅ `engine=true`（/api/risk、/api/snapshot 可访问） |
| 数据模式 | ✅ `data_mode=xtdata`（实时，非 mock） |
| 券商连接 | `broker_connected=false` / `disconnected` —— **paper 模式正常**（无需券商通道） |

两路均无中断，无需自启动恢复。

---

## 二、执行模式与风险底线（唯一硬要求）

**EXECUTION_MODE = `"paper"`** ✅ —— `config/settings.py` 与 `/api/snapshot` 双确认，未切 `live`，无告警、无阻断动作。

**风险底线未放松**（任一越界即告警，本次全部通过）：

| 底线 | 要求 | 当前 | 结论 |
|---|---|---|---|
| max_drawdown_pct | ≤ −0.15 | **−0.20** | ✅ 更严 |
| risk_per_trade | ≤ 0.02 | **0.012** | ✅ |
| T1_RESTRICTION | = True | **True** | ✅ |
| max_positions | ≥ 3 | **5** | ✅ |

---

## 三、陈旧熔断（检查清单 2）

**实况（来自 `/api/risk` 实时内存态，权威）：**
`halted=true`、`halt_reason="consec_loss=5"`、`consecutive_losses=7`、`position_scale=0.0`、`daily_pnl=-5602.4`

- **类型**：连亏熔断（**非回撤类**）→ 适用 `halt_recover_days=1`。
- **触发日** `halt_day=2026-09-24`；今日 `2026-09-25` → `held=1 ≥ 1` → **冷却已满足**。
- **来源**：09-24 10:00 同批 4 笔亏损 SELL（相隔 3 毫秒，板块级回调）在 J1 修复（同批退出合并计数）生效**前**的旧语义下 `consec` 3→7 越阈；19:03 重启已载入 J1 修复，但已累计的 `consec_loss=7` + `halted` 被持久化。
- **处置**：冷却已满足，引擎 `risk/manager.py::_maybe_recover` 将在今日**首个交易 tick（09:30 后，由 `on_asset_update` / `can_open` 每轮调用）自动清零** `halted` / `consec_loss` / `position_scale→1.0`，无需手动干预即可恢复开仓。
- **确定性选项**：若希望开盘前立即清冻结，可在桌面会话对运行引擎执行一次 `RiskManager.resume()`。
- **非永久停牌**：本实例 `halted` 已随 09-21 僵尸修复正确持久化，`_halt_day` 也持久化，`_maybe_recover` 冷却链路完整（非 09-18/09-21 的僵尸死锁）。
- **预计恢复时间**：2026-09-25 09:30 开市首 tick 自动解除（或开盘前 resume 立即解除）。

---

## 四、双周期进化落地核验（检查清单 3，最近两条）

| 周期 | 记录 | 落盘内容 | config 改动 | 生效方式 |
|---|---|---|---|---|
| **09-24 AM-EVOLVE**（第6轮 11:41） | J1 修复「同批退出被计成 N 次连亏」 | `risk/manager.py`（代码语义修复）+ 测试 | **零改动** | **需重启引擎**（.py 改动）；与 09-23 PM ×2 同批于 19:03 重启生效 |
| **09-24 PM-EVOLVE**（第7轮 18:00） | 两候选（日内强平 / 日内熔断 abs） | 全部 REJECTED，生产零改动 | **零改动** | 无落地 |

**核对结论**：两条记录 `config/settings.py` 均 **diff 空**（零参数落盘）→ 与当前 `config/settings.py` **无任何偏差，无需告警**。当前已落地参数（momentum_top_n=3、min_daily_bias=2.0、rotation_require_daily_gate=True、northbound_mode="gate"、max_positions=5 等）与既有周期一致。

**⚠ 待重启生效项（非参数，代码修复）**：
- **09-24 补充（19:05 OWNER 重启后）** 落盘的「日线滞后 4 周」根因修复（`strategy/daily_context.py` + `backtest_daily.py`，代码修复，config 零改动）。该记录明确要求「**再次重启引擎使日线修复生效**」，而 OWNER 上次重启为 **19:03（修复落盘前）** → **该修复当前未生效**。
- 后果：运行实例仍跑**陈旧日线**（末日停在 08-25 的口径未解除）→ 今日主路径（动量 + 日线闸门）将**零 BUY**（`regime=down` 预期防御，非故障），且日线滞后问题在运行实例中仍未解除，**直至重启**。

---

## 五、就绪结论

### 结论：**可交易（条件）** —— 无硬阻塞（live / 参数 / 进程级均通过），熔断将于 09:30 自动解除。

- ✅ **paper 模式确认**（config + 实时接口双确认），风险底线未放松。
- ✅ 在线正常（miniQMT + 引擎 5000，data_mode=xtdata 实时）。
- ⚠ **当前处于熔断冻结**（`position_scale=0.0`），但冷却已满足，**09:30 首 tick 自动解除**；或开盘前 `resume()` 立即解除。
- ✅ 双周期最近两条记录**零参数落盘 → config 无偏差**。

### 需用户（OWNER）介入事项
1. **（可选·确定性强）** 开盘前对运行引擎执行一次 `RiskManager.resume()`，立即清除 `halted`；或等 09:30 自动恢复。
2. **（建议）重启引擎**，使 09-24 补充的「日线滞后修复」生效 —— 否则今日仍跑陈旧日线、主路径零 BUY、日线滞后未解除。**自动化不得代为重启，请在桌面会话处理。**
3. **（OWNER 待裁决·非阻塞）** 09-24 PM 已记录：① 日内强平 `daily_stop_flatten_pct=-6%` 为「纯成本」（两轮一致负、MDD 无改善），是否移除；② 观察篮在 `regime=down` 时是否应遵守日线闸门。

### 风险提示
即便恢复开仓，当前 `regime=down`（23 只宇宙 `above_ma60=0`），主路径零 BUY，账户盈亏 100% 来自**观察篮（5 只手工指定、绕过闸门）/ 轮动旁路**，不可归因于策略有效性 —— 与近几日盘后复盘结论一致。

---

> 证据锚点：`scripts/health_check.py --json`、`/api/risk`、`/api/snapshot`、`storage/qmt.db::engine_state`、`config/settings.py`、`reports/EVOLUTION_DECISIONS.md`（09-24 AM/PM/补充）、`risk/manager.py::_maybe_recover`（226–253 行）。

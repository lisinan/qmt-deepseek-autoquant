# 盘前就绪检查 · 2026-09-18（PRE-MARKET / 98ffc7a2）

> 执行时间：2026-09-18 09:13（GMT+8）｜解释器：conda qmt｜本环节只检查与告警，未改动任何策略参数 / 生产配置 / live 进程。

## 一、就绪结论：⛔ 不可交易（存在硬阻塞，需用户介入）

引擎在线、paper 模式正确、风险底线未放松，但 **RiskManager 连续亏损计数陈旧(=9) 把仓位缩放锁死在 0.0**，今日开盘将无法开任何新仓。该阻塞需用户在桌面会话手动解除。

---

## 二、在线状态 ✅

| 项目 | 状态 | 说明 |
|---|---|---|
| miniQMT（xtquant 通道） | ✅ 在线 | health_check --json → `miniqmt=true` |
| 引擎 Web（5000） | ✅ 在线 | health_check --json → `engine=true`；`/api/risk`、`/api/snapshot` 均可访问 |
| 数据源 | ✅ xtdata（实时） | 快照 `data_mode=xtdata`（对比 09-16 曾回退 mock，本次已正常） |
| 券商连接 | ➖ disconnected | paper 模式无需券商通道，非阻塞 |

> 两服务在线，无需触发自启动恢复。

---

## 三、执行模式 & 风险底线 ✅（符合硬要求）

| 校验项 | 要求 | 实测 | 结果 |
|---|---|---|---|
| EXECUTION_MODE | `"paper"` | `"paper"`（config + 实测 `/api/snapshot.exec_mode`） | ✅ |
| max_drawdown_pct | ≤ -0.15 | -0.20 | ✅ |
| risk_per_trade | ≤ 0.02 | 0.012 | ✅ |
| T1_RESTRICTION | True | True | ✅ |
| max_positions | ≥ 3 | 5 | ✅ |

**未切 live、风险底线未放松。** 保持 paper 测试收益铁律无异常。

---

## 四、陈旧熔断检查 ⛔ 发现死锁（非 halted，但仓位被锁 0.0）

### 实测（来自运行引擎 `/api/risk` + `risk_snapshots` + `engine_state`）
- `halted = false`（**当前未熔断**）✅
- `halt_reason = ""`
- `consecutive_losses = 9`（陈旧，来自 09-17 真实连亏）
- `position_scale = 0.0` ⛔ ← **阻塞点**
- 最新 risk_snapshot（09-18 09:15:03）：`halted=False`

### 根因
1. `_restore_engine_state`（event_engine.py:1980）每次启动从 `engine_state` 恢复 `consec_loss=9`，但**不恢复 `_halted`/`_halt_day`** → 引擎以 `halted=False + consec_loss=9` 启动。
2. `position_scale` 由 `consec_loss` 经 `_SCALE_LADDER=[1.0,0.8,0.6,0.4,0.0]` 推导：consec_loss=9 → 索引 4 → **0.0**（manager.py:259-266）。
3. 自动恢复 `_maybe_recover` **仅在 `halted=True` 时**才清零 `consec_loss`（manager.py:207）。本例 `halted=False`，故计数永不自动清零。
4. 仓位缩放=0.0 → 不开新仓 → 无成交 → 没有盈利卖单把 `consec_loss` 归零（manager.py:171）→ **死锁**，今日无法交易。

### 处置建议（需用户介入，自动化不得代为重启/改 live 进程）
- **正确做法**：在桌面会话对运行中的引擎调用 `RiskManager.resume()`（清零 `_consec_loss` / `_daily_pnl` / `halted` / `flatten`）。resume 后引擎会在下次 `engine_state` 落盘把 9 写成 0，后续重启不再复锁。
- **仅重启不足以解决**：重启会再次从 `engine_state` 读回 consec_loss=9 复锁。若走重启路线，须先把 `storage/qmt.db` 的 `engine_state.consec_loss` 置 0 再重启。
- **当前无 web 端 resume 接口**（web/routes.py 仅 `GET /api/risk`，无控制端点），故只能由用户在桌面进程侧执行。
- 说明：09-16 单实例锁误判修复（`_pid_alive` 改用 `GetExitCodeProcess`）已生效，进程被强杀后的旧 PID 锁自动失效，与本死锁无关。

---

## 五、双周期自进化落地核验

> 双周期（AM 11:40 + PM 18:00）自 09-17 起生效。当前仅 1 条决策记录（09-17 PM）；**今日 AM-EVOLVE(11:40) 尚未运行**（现 09:13 盘前），故「最近两条」实际只有 1 条。

### 最近决策记录（reports/EVOLUTION_DECISIONS.md）
| 日期/轮次 | 落盘参数 | 原值→新值 | 生效方式 | config 当前值 | 是否相符 |
|---|---|---|---|---|---|
| 2026-09-17（PM，第1轮） | `STRATEGY_PARAMS.momentum_top_n` | 6 → **3** | **热读**（event_engine.py:1376 直接读取，无需重启） | `3` | ✅ 相符，已生效 |

- 闸门裁决：8/8 全通过（IS +2.96pt、OOS 均值 Sharpe +0.216、IS/OOS 差 13.1%、MDD -13.86%、tests 全通过、风险底线/paper 不动）。
- **无 `__init__` 缓存类参数（regime_mode / enable_rotation / rotation_*）改动** → 本期无「需重启才生效」的待办。
- 待观察：今日 11:40 AM-EVOLVE 产出后将纳入下一轮盘前核对；若落盘缓存类参数，会在此提示需重启。

---

## 六、需用户介入事项（汇总）

1. ⛔ **【阻塞】解除 position_scale=0.0 死锁**：在桌面会话对运行引擎执行 `RiskManager.resume()`；或清 `engine_state.consec_loss=0` 后于桌面重启。否则今日无法开新仓。
2. ℹ️ 今日 AM-EVOLVE（11:40）将在开盘后运行，其产出（若有参数落盘）将于下一交易日前由本环节核对生效方式。
3. ℹ️ 本环节未改动任何文件 / 进程 / 参数。

---

## 七、关键状态快照（备查）
- `/api/risk`：`{"consecutive_losses": 9, "daily_pnl": -188766.82, "halted": false, "halt_reason": "", "peak_asset": 1000000.0, "position_scale": 0.0}`
- `/api/snapshot`：`exec_mode=paper, data_mode=xtdata, broker=disconnected, 订阅标的=38`
- `engine_state`(id=1)：trade_date=2026-09-17, cash=803680.18, positions=[], consec_loss=9

# 盘前就绪检查 — 2026-09-21（周一）· 闭环 PRE-MARKET 环节

> 执行器：98ffc7a2（每日 09:05）。环境：conda qmt + PATH(Library/bin,DLLs) + PYTHONPATH=.

## 一、在线状态

| 项 | 结果 | 说明 |
|---|---|---|
| miniQMT | ✅ 在线 | `health_check.py --json` → `miniqmt=true` |
| 引擎(5000) | ✅ 在线 | `engine=true`；`/api/risk`、`/api/snapshot` 可访问 |
| data_mode | ✅ xtdata（实时） | 对比 09-16 的 mock 已正常 |
| 券商连接 | ⚪ disconnected | paper 模式无需 broker，符合预期 |

任一停止均未发生，未触发自启动恢复。

## 二、执行模式（唯一硬要求）

- **EXECUTION_MODE = "paper"** ✅（config 与 `/api/snapshot` 双确认，未切 live；若为 live 本会立即告警并停止）
- 风险底线未放松 ✅：
  - `max_drawdown_pct = -0.20`（≤ -0.15）✅
  - `risk_per_trade = 0.012`（≤ 0.02）✅
  - `T1_RESTRICTION = True` ✅
  - `max_positions = 5`（≥ 3）✅

## 三、陈旧熔断 / 死锁检查

- `halted = false`、`halt_reason = ""` → **无处于冷却的熔断** ✅
- ⛔ **但发现遗留死锁（同 09-18 盘前，至今未解除）**：
  - 实时 `/api/risk`：`consecutive_losses = 9`、`position_scale = 0.0`
  - 机理：`_restore_engine_state` 每次启动从 `storage/qmt.db` 的 `engine_state` 恢复 `consec_loss = 9`（DB 仍记 9）；`halted=False` → `_maybe_recover` 仅在 `halted` 时清零 → 计数永不归零 → `position_scale=0.0` → `_handle_buy` 在 `scale<=0` 直接 `return`（`engine/event_engine.py:1764-1766`）→ 永不新开仓。
  - 当前引擎于 **09-21 00:16:51 重启**，但 `consec_loss=9` 被重新载入，死锁仍在。
  - 无 web resume 接口，须用户手动处置（见第五节）。

## 四、双周期进化落地核验（最近两条记录）

最近两条记录均属 **2026-09-19**（09-20 周日市场休市无进化；09-21 上午 AM-EVOLVE 11:40 尚未运行）：

| 记录 | 落盘内容 | config 核对 | 生效方式 / 现状 |
|---|---|---|---|
| §八 2026-09-19 OWNER 裁决（修订目标 + 批准新数据轴研发） | **无参数落盘**（仅目标修订 + R&D 批准） | — | 无待生效项 |
| §九 2026-09-19 下午 北向资金轴 R&D 落盘 | `northbound_mode="gate"`、`nb_lookback=20`（热读，可改回 "off" 恢复） | `northbound_mode="gate"` ✅、`nb_lookback=20` ✅ 与账本一致 | 原记录标注为 `__init__` 缓存参数「须重启引擎生效」；引擎已于 09-21 00:16:51 重启，`/api/snapshot` 显示 `northbound loaded=true / mode=gate / lookback=20 / blocked=false` → **已随本次重启生效**，无需再重启 |

**结论**：双周期最近改动均已落地且生效，无「配置与账本不符」告警。

## 五、就绪结论

- ⛔ **不可交易** —— 阻塞原因为 `position_scale=0.0` 死锁（陈旧 `consec_loss=9`），非熔断冷却。
- ✅ paper 模式确认；✅ 风险底线未放松；✅ 在线状态正常；✅ 双周期改动生效。
- **需用户介入（唯一阻塞项）**：在桌面会话对**运行中的引擎实例**执行
  `engine.risk.resume()`（清零连亏计数并落盘，约 60s 内写回 `engine_state.consec_loss=0`），
  即可解除仓位锁、恢复开仓。
  - 注意：**仅重启不够**——`_restore_engine_state` 会重读 DB 的 `consec_loss=9` 复锁。
    若选择重启路径，须先 `UPDATE engine_state SET consec_loss=0 WHERE id=1;` 再启动。
  - 本环节未改动任何策略参数 / 生产配置 / live 进程。

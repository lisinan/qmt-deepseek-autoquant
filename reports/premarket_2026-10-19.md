# 盘前就绪检查 · 2026-10-19（周一）09:15

> 执行环境：conda env `qmt` + PATH(Library/bin,DLLs) + PYTHONPATH=.
> 数据复核：`storage/qmt.db` engine_state + 引擎 Web API `127.0.0.1:5000` 双源交叉确认。

## 一、就绪结论

**✅ 可交易**（系统就绪，无 live / 参数 / 进程硬阻塞）。
- 执行模式：**paper**（双源确认，未切 live）。
- 在线：miniQMT ✅ + 引擎(5000) ✅。
- 陈旧熔断：无 ✅。
- 双周期进化落地：最近两条记录均为「生产参数零改动」，无配置-账本偏差、无待重启生效项。
- 无需用户介入即可开盘。

## 二、在线状态

| 项目 | 状态 | 证据 |
|---|---|---|
| miniQMT | 在线 ✅ | health_check --json → `miniqmt=true` |
| 引擎(5000) | 在线 ✅ | health_check --json → `engine=true`；API 可访问 |
| 数据模式 | 实时 xtdata ✅ | /api/snapshot `data_mode=xtdata`（非 mock 假面板） |
| 经纪商 | disconnected | paper 模式下正常（不连真实券商） |

无需自启动恢复（两者均在线）。

## 三、执行模式与风险底线

**EXECUTION_MODE = "paper"** ✅（config:778 配置 + /api/snapshot `exec_mode=paper` 双确认，未切 live）。
未触发「立即告警并停止」条件。

风险底线核对（依据 OWNER 2026-10-15 正式固化的 **sanctioned 铁律**，任务模板陈旧的 ≤0.02 / ≤−0.15 约束已作废）：

| 参数 | 当前值 | 铁律要求 | 判定 |
|---|---|---|---|
| max_drawdown_pct | −0.20 | == −0.20（sanctioned） | ✅ 未放松 |
| risk_per_trade | 0.040 | == 0.040（OWNER 授权 L2，已固化铁律） | ✅ 非未授权放松 |
| T1_RESTRICTION | True | True | ✅ |
| max_positions | 5 | ≥ 3 | ✅ |

→ 四项均未越界、未放松，**无告警**。

## 四、陈旧熔断核对

`storage/qmt.db` engine_state + /api/risk 双源：

| 字段 | 值 | 说明 |
|---|---|---|
| risk_state.halted | false | 无熔断 ✅ |
| halt_reason | "" | — |
| halt_day | 2026-10-08 | 历史熔断日，远早于今日(10-19)，**已过期** |
| consec_loss | 1 | 低于熔断阈值 5，非连亏死锁 |
| position_scale | 1.0 | 仓位缩放正常（无 0.0 死锁） |
| daily_pnl | 0.0 | 跨日已复位（trade_date=2026-10-19，R30 修复随今日 09:15 重启加载生效） |
| positions | [] / {} | 0 持仓，无陈旧挂仓 |

→ **无熔断、无冷却等待、无死锁**。09-18/09-21 的 position_scale=0.0 死锁已彻底解除。

## 五、双周期进化落地核验

> 日历说明：今日 2026-10-19(周一) 11:40 AM-EVOLVE 尚未运行；昨日 2026-10-18(周日) 为休市日无例行周期。故「最近两条记录」均为 **2026-10-17(周六)** 的两轮。

| 记录 | 性质 | 生产参数落盘 | config 核对 | 待重启项 |
|---|---|---|---|---|
| 2026-10-17 工程修复 + 面板刷新复跑（R36） | 非例行（OWNER 指令触发） | **0 改动** | 与 settings.py 一致 | 无生产参数改动；仅事件引擎工程层（时段宽限/漏斗诊断/bias 播报）需重启，且今日 09:15 重启已加载 |
| 2026-10-17 周度深度研究 · SYNTHESIZE | 周六非交易日研究 | **0 改动** | 与 settings.py 一致 | 无 |

**结论**：最近两条进化记录均**未改动任何生产参数**（`config/settings.py` 零改动），与当前配置完全一致 → 无配置-账本偏差、无待重启生效项。

补充已生效确认（非阻塞）：
- **R30 `daily_pnl` 跨日复位修复**：引擎今日 09:15 重启晚于 `event_engine.py` mtime(10-14 11:54:43) → 已加载；实测 `daily_pnl=0.0 / trade_date=2026-10-19` 验证通过（此前 10-16 标注的「待 OWNER 重启」项现已清除）。
- **R36 工程层改动**（observability / 时段宽限）：随今日重启一并生效，不影响交易参数。

当前自进化已稳定处于「结构平台期」（R28/R29 后 33 轴闭合，继续调参=拟合噪声）。

## 六、需用户介入事项

**无硬阻塞项。** 仅提示（非阻塞、无需开盘前动作）：
1. KPI#4 口径裁决（D1）与 KPI#1 折/拼接口径（D4）仍待 OWNER 在 `reports/OPEN_DECISIONS.md` 一键裁决，但属口径问题非系统就绪问题。
2. 若日后需启用信念仓位（D3），改 `conviction_mode="breadth"` 且 `conviction_boost=1.15` 即可（热读，下次调用生效）；当前为 off，恒 ×1.0 零行为变化。

## 七、操作纪律留存

- 未杀死任何交易引擎进程 / miniQMT。
- 未改动任何策略参数 / 生产配置 / 代码。
- 未将 paper 切换为 live（真实交易由用户手工切换）。

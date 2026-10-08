# 盘前就绪检查 2026-10-02（PRE-MARKET 排雷）

> 执行时间：2026-10-02 20:39 (GMT+8) ｜ 自动化 98ffc7a2
> 环境：conda qmt + PATH(Library/bin,DLLs) + PYTHONPATH=.
> 注：2026-10-02 为**国庆休市日**（10-01~10-07 休市，10-08 开市），本次为系统层面就绪复核。

## 一、在线状态 ✅

| 项 | 结果 | 说明 |
|---|---|---|
| miniQMT (58610) | 在线 ✅ | `health_check.py --json` → `miniqmt=true` |
| 引擎 (5000) | 在线 ✅ | `engine=true`，`/api/snapshot` 可访问 |
| 自启动恢复 | 未触发 | 两者均在线，无需恢复 |

## 二、执行模式与风险底线（硬要求）✅

- **`EXECUTION_MODE = "paper"`** ✅（config/settings.py:557 双读 + `/api/snapshot exec_mode=paper` 实时双确认，**非 live**）
- 风险底线未放松：

| 参数 | 当前值 | 阈值 | 判定 |
|---|---|---|---|
| `max_drawdown_pct` | -0.20 | ≤ -0.15 | ✅ 更紧 |
| `risk_per_trade` | 0.012 | ≤ 0.02 | ✅ |
| `T1_RESTRICTION` | True | = True | ✅ |
| `max_positions` | 5 | ≥ 3 | ✅ |

## 三、陈旧熔断核对 ✅（无熔断、无死锁）

- `/api/risk` 实时：`halted=False`、`halt_reason=""`、`consecutive_losses=0`、`position_scale=1.0`
- `storage/qmt.db` engine_state：`risk_state={"halted":false,...}`、`consec_loss=0`
- 对比 09-18/09-21 的 `position_scale=0.0` 死锁已**彻底解除**（R15 §10 回滚+重启后恢复为 1.0）
- **`data_mode=xtdata`**（真实行情，非 mock）—— R14「合成行情禁止交易」闸门已生效，09-30 假成交根因已消除

## 四、双周期进化落地核验（最近两条：2026-09-30 AM R14 / 2026-09-30 PM R15）

> 因国庆假期（10-01~10-07 休市），10-01/10-02 无自进化记录；最近两条均为 09-30。

### R14（09-30 11:40 AM）落盘项
| 改动 | config 当前值 | 相符 | 生效方式 |
|---|---|---|---|
| `ALLOW_MOCK_TRADING = False`（新增开关） | `False` (settings.py:585) | ✅ | **需重启引擎**；R15 §10 已于 09-30 20:44 重启并验证 `data_mode=xtdata` ⇒ **已生效** |

### R15（09-30 18:00 PM，§10 收官执行 · OWNER 授权重启）落盘项
| 改动 | config 当前值 | 相符 | 生效方式 |
|---|---|---|---|
| 观察篮清空 `manual_entry_codes: [...] → []` | `[]` (settings.py:401) | ✅ | **热读**（`_manual_entry_step` 每次读取），随 R15 重启落地，**已生效** |
| `manual_entry_exit_exempt` 维持 `True` | `True` (settings.py:409) | ✅ | 惰性（篮子空后无豁免对象），与回测默认一致 |

其余 R15 落盘项（账本断供 DB 直读回退、观察篮回测侧建模、候选 `entry_mode="trend"` REJECTED）均不涉及 config 运行参数；R15 §10 重启验证：data_mode=xtdata / paper / 日线补齐至 09-30 / 分钟预热陈旧拒用 0 / 北向 guard fail-open / **ERROR 0**。

**结论：双周期改动全部落地、与 config 相符、均生效；无未生效或待重启项。**

## 五、就绪结论

**✅ 可交易（系统层面就绪）** —— 但 2026-10-02 处于国庆休市，实际无交易至 **10-08 开市**。

- paper 模式：已确认 ✅（唯一硬要求满足）
- 双周期改动：已落地、相符、均生效 ✅
- 熔断/死锁：无 ✅
- 行情来源：xtdata 真实数据 ✅（合成行情禁交易闸门生效）

## 六、需用户介入事项

- **无阻塞项**，本环节未改动任何策略参数 / 生产配置 / live 进程。
- 10-08 开市前建议：确认引擎启动日志仍显示「数据源=xtdata」（当前实时 `/api/snapshot` 显示 `data_mode=xtdata`，健康）。本引擎自 09-30 20:44 持续运行，假期无需重启。
- 若用户决定假期停机：**须先暂停 GUARD 自动化再停**，否则会被自动拉起（夜间启动可能落 mock；但 `ALLOW_MOCK_TRADING=False` 已双向阻断买卖，不会再造假成交）。
- 北向守卫 `credible=False`（序列末端 2026-08-25 陈旧）按设计 fail-open，不影响建仓，属已知常态。

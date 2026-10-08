# 盘前就绪检查报告 · 2026-10-07（周三）

> 环节：PRE-MARKET 盘前排雷（闭环，98ffc7a2）
> 环境：conda qmt（C:\Users\lisinan\.conda\envs\qmt\python.exe）+ PATH(Library/bin,DLLs) + PYTHONPATH=.
> 注：今日仍在国庆休市区间（10-01~10-07），首个交易日 = **2026-10-08（周四）**。

## 一、在线状态

| 组件 | 状态 | 处置 |
|---|---|---|
| miniQMT | **在线** ✅（`health_check.py --json` → `miniqmt=true`） | 无需自启动恢复 |
| 交易引擎(5000) | **在线** ✅（`engine=true`，且 `/api/snapshot`、`/api/risk` 可访问） | 无需自启动恢复 |
| 数据源 | **xtdata（真实行情）** ✅（`/api/snapshot.data_mode=xtdata`） | 非 mock，ALLOW_MOCK_TRADING=False 闸门生效 |
| broker | disconnected（paper 正常） | — |

## 二、执行模式（唯一硬要求）

- **`EXECUTION_MODE = "paper"`** ✅ 双重确认：`config/settings.py:641` + 实时 `/api/snapshot.exec_mode=paper`。
- 未切 live，**未触发任何告警、未做任何改动**。真实交易仍由用户手工切换。

### 风险底线复核（未被放松）

| 参数 | 当前值 | 要求 | 结论 |
|---|---|---|---|
| max_drawdown_pct | −0.20 | ≤ −0.15 | ✅ |
| risk_per_trade | 0.012 | ≤ 0.02 | ✅ |
| T1_RESTRICTION | True | = True | ✅ |
| max_positions | 5 | ≥ 3 | ✅ |

## 三、陈旧熔断

- 实时 `/api/risk`：`halted=false`、`consecutive_losses=0`、`position_scale=1.0`、`daily_pnl=0`。
- DB `engine_state.risk_state`：`halted=false`、`halt_reason=""`。
- **无熔断，无连亏死锁**，无需 `RiskManager.resume()`，无冷却等待。
- 单实例锁（09-16 修复）继续生效，进程启动时间 2026-10-06 23:30:44，非僵尸残留。

## 四、双周期进化落地核验（昨日下午 + 今日之前的上午）

最近两条进化记录：**R17（2026-10-06 上午 AM，第17轮）** 与 **R18（2026-10-06 收官 PM，第18轮）**（10-07 属休市，无新周期）。

### R17（第17轮 · 2026-10-06 上午）落盘项
- **`enable_rotation`：True → False**（关闭板块轮动，证据：四窗口共识 dSh +0.060/+0.089/+0.268/+0.327，最差 +0.060，纯磨损型改进）。
- `config/settings.py:542` 当前值 = **False** ✅ 与落盘相符。
- **生效方式：⚠️ 需重启引擎**（`EventEngine.__init__` 构造期缓存，event_engine.py:298-299；非热读）。
- **生效判定：当前运行实例【未生效】**。引擎进程启动于 **2026-10-06 23:30:44**，而 R17 落盘发生在 **23:34** 之后 → 实例仍缓存旧值 **True**。需 10-08 开市前重启才生效。

### R18（第18轮 · 2026-10-06 收官）落盘项
- **config/settings.py 零改动**（仅新增回测量测工具 `--mode span`、死代码识别、测试 `tests/test_span_measure.py` 4 例）。
- 无可核对项，无新增待重启项。

### 关联历史项复核
- `down_day_exit_pct = -3.25`（R16，2026-10-03 落盘，__init__ 构造期快照需重启）→ 引擎 2026-10-06 重启晚于落盘日 → **已生效** ✅。
- `regime_mode = "off"`（2026-08-29，早已生效）。
- 其余近期轮子均为 REJECTED / 死代码 / 回测器内部，**无 config 改动**。

## 五、就绪结论

### ✅ 可交易（系统就绪，无硬阻塞）

- paper 模式确认 ✅；风险底线未放松 ✅；miniQMT + 引擎在线 ✅；数据源真实(xtdata) ✅；无熔断/无死锁 ✅。
- 双周期改动核对：
  - R18 零参数落盘 → 无偏差；
  - R17 `enable_rotation=False` 已写入 config 且与落盘相符，但**运行实例仍按旧值 True 执行，需重启生效**（见下）。

### ⚠️ 需用户介入事项（非阻塞）

1. **【必须】10-08 开市前重启交易引擎**，使 R17 `enable_rotation=False` 与 R16 `down_day_exit_pct=-3.25` 一并生效（前者当前未生效）。
   - 重启请在**桌面会话**手动操作；本自动化**不代为重启**（属 __init__ 缓存类参数，且铁律禁止自动化 kill 交易引擎）。
   - 重启后请确认：① 日志出现「数据源=xtdata」；② `_maybe_rotate` 不再产生「板块轮动补强空槽/批量换仓」日志。
   - **切勿误杀 miniQMT（58610）进程**，仅重启本项目 `main.py --web`（当前 PID 16096）。
2. **R17 实证窗口（10-08~10-09）**：重启后对比换手（预期 231→176 笔量级）与收益，验证「轮动=纯磨损」结论；若不成立按 R17 §4 回滚裁决。
3. **OWNER 裁决（R18 提议）**：KPI #1 是否改按「拼接口径」考核（拼接 Sharpe 1.980 ≈ 达标，折均值 1.672 受小样本噪声污染）——执行器未私改口径，待用户裁定。
4. 今日（10-07）休市，实际无交易；以上准备面向 10-08 开市。

## 六、本次未改动

未修改任何策略参数、生产配置、live 进程；未触发任何告警阻断。

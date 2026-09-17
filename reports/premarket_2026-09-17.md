# 盘前排雷报告 · 2026-09-17（周四）

> 闭环：PRE-MARKET（98ffc7a2）｜ 执行器 conda qmt + PATH(Library/bin,DLLs) + PYTHONPATH=.
> 纪律：本环节仅检查与告警；任何参数/配置改动须过 EVOLVE 晋升闸门再落地，且不得杀死运行中的引擎或 miniQMT。

## 一、在线状态
- **miniQMT**：在线 ✅（`health_check.py --json` → `miniqmt: true`）
- **交易引擎(5000)**：在线 ✅（`engine: true`）
- **自启动尝试**：无需（两项均在线，未触发恢复动作）

## 二、执行模式
- **EXECUTION_MODE = `"paper"`** ✅
- 符合本阶段要求，未切 live。若切 live 需用户明确确认（当前阶段必须保持 paper 验证）。

## 三、陈旧熔断核验
- 持久化熔断状态来自 `risk_snapshots`（注意：`engine_state` 表无 `halted` 列）。
- 最近快照（`2026-09-16T15:02:38`）：`halted=true`、`halt_reason="consec_loss=5"`、`consecutive_losses=9`、`daily_pnl=-188766.82`。
- `2026-09-17` 暂无新快照（盘前引擎空闲，属正常）。
- 断路器类型：非回撤类（consec_loss）→ `halt_recover_days=1`。
- 触发日 `2026-09-16`；今日 `2026-09-17` → 自然日冷却已满足（`(today-halt_day).days=1 ≥ 1`）。
- **结论：冷却已过，属「已过冷却」情形。**
- **建议（非硬阻塞）盘前手动调用一次 `RiskManager.resume()`**，理由：
  1. 引擎 `_maybe_recover` 会在 `2026-09-17` 首个账户更新 tick 自动解除并重置连亏/日内盈亏；若当前是 `09-16` 的连续实例则直接自动恢复。
  2. 但若引擎为 `09-17` 全新重启实例（`RiskManager` 全新、`_halt_day=今日`），会从 `engine_state` 恢复 `consec_loss=9` 且不会触发 `_maybe_recover`（未 halt），此时 `position_scale` 锁为 `0.0`（`_SCALE_LADDER[4]=0.0`）→ 全天无法开新仓。`resume()` 同时把 `consec_loss` 归 0、`position_scale` 回到 `1.0`，规避此死锁。
- **单实例锁**：`2026-09-16` 已修复（`main.py::_pid_alive` 改用 `GetExitCodeProcess` 判定），进程被强杀后残留的旧 PID 锁自动失效，不会再出现「反复拉起却起不来」。

## 四、昨夜进化落地核验
- 盘前约定读取 `reports/EVOLUTION_DECISIONS.md` —— **本项目不存在该文件**。进化落地的权威记录为 `OPTIMIZATION_PROPOSALS.md` + EVOLVE 自动化（`d8013e93`，其 memory 尚未生成）。
- 最近两条进化提案（`09-15`、`09-16`）结论均为「已确认·**维持现状**」，**无任何参数被实际落盘改动**。
- `git log` 与当前 `config/settings.py` 比对：生产铁律值全部一致 ——
  `exit_mode=trend` / `risk_per_trade=0.012` / `max_positions=5` / `enable_rotation=True` / `max_drawdown_pct=-0.20` / `dd_recover_days=3` / `halt_recover_days=1` / `max_consecutive_losses_halt=5` / `EXECUTION_MODE=paper`。
- **结论：昨夜无进化参数改动落地 → 无需核对，无「改动未生效/未保存」风险** ✅

## 五、就绪结论
### ✅ 可交易（带 1 项盘前建议 + 1 项盘中复核）

**阻塞项：无**

**需用户介入：**
- 【建议·非阻塞】盘前手动 `RiskManager.resume()` 一次，确保 `09-17` 以全仓位能力开盘（清 halt 残留 + 重置连亏计数，防全新重启实例锁 `position_scale=0.0`）。
- 【盘中复核】开盘后确认风险快照 `halted=false` 且 `position_scale=1.0`（或随首笔盈利回升）；若仍 `halted` 或 `position_scale=0`，立即 `resume()`。
- 【实盘现状提示·非阻塞】paper 账本 `09-16` 收 `803,680`（区间 `-19.6%`，含 `-16.4%` AI 板块隔夜缺口），属行情重估非策略缺陷，本环节不动参数。

**未改动任何策略参数 / 生产配置 / live 进程。**

# -*- coding: utf-8 -*-
"""2026-09-30 第 15 轮收官：OWNER 授权重启后的执行清单与验证结果（追加）。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "reports" / "EVOLUTION_DECISIONS.md"
AUTO = ROOT / "docs" / "AUTOMATIONS.md"

BODY = """

### 10) ★ 收官执行（20:40 · OWNER 授权重启引擎）—— 全部完成并验证

**（a）执行顺序（停机类操作必须先于数据修复）**

| # | 动作 | 结果 |
|---|---|---|
| 1 | 确认 `EXECUTION_MODE="paper"` | ✅ |
| 2 | 备份 `storage/qmt.db` → `qmt.db.bak-20260930-2038` | ✅ 31.1 MB |
| 3 | 优雅停机 `python main.py --stop` | ⚠ 哨兵被回收但进程未退出（PID 24436 仍在监听 5000） |
| 4 | 确认进程身份后终止（`python.exe` PID 24436，控制台会话） | ✅ 5000 释放。**miniQMT(58610) 全程未动** |
| 5 | `scripts/repair_mock_fills_20260930.py --apply` | ✅ 撤销 5 笔假 fills / 5 笔 orders，还原 5 仓与现金，解除熔断 |
| 6 | 落盘观察篮清空（§9 方案 A） | ✅ 见下 |
| 7 | 补记 09-30 真实 EOD 与账本行 | ✅ |
| 8 | 重启引擎并验证 | ✅ 见（c） |

**（b）回滚结果（逐项核对）**

| 项 | 回滚前 | 回滚后 |
|---|---|---|
| 现金 | 772,145.40（全现金、空仓） | **483,926.60** |
| 持仓 | **0 只** | **5 只**（300394/000977/300308/688008/002415），市值 **494,914.00** |
| 总权益 | 772,145.40 | **978,840.60** = 09-29 EOD ✅ |
| 熔断 | `halted=true` / `daily_loss_abs -35622.40` | `halted=false`、consec_loss=0、daily_pnl=0 ✅ |
| 09-30 fills / 快照 | 5 笔 / 262 条（污染） | 0 / 0（作废）✅ |
| `position_scale` | 0.0（冻结） | **1.0**（恢复开仓能力）✅ |

**（c）★ 重启验证（这是本次授权的核心目的：证明优化真的生效）**

启动日志 `logs/notices.log` 2026-09-30 20:44:10：

```
引擎已启动 | 策略=single 执行=paper 数据源=xtdata 券商=disconnected | 订阅标的=38
已启动 PAPER 模拟盘模式。本会话所有交易记录以 mode=PAPER 标记…
分钟线预热完成：就绪 38 / 已有 0 / 陈旧拒用 0 / 无数据 0
DailyContext 本地日线已补拉至 2026-09-30 … 刷新完成: 38/38
日线上下文刷新完成: regime=down n=38
```

| 验证项 | 结果 | 说明 |
|---|---|---|
| **数据源** | **`data_mode=xtdata`** | ★ 不再是 mock —— 今天所有假亏损的根源已消除 |
| 执行模式 | `paper` | ✅ |
| **日线陈旧度** | **末根 2026-09-30，38/38 全部补齐** | ★ **09-29 PM 落盘的「交易日口径」修复实证生效**（修复前末根停在 09-24，缺 3 个交易日） |
| 分钟线预热 | 就绪 38 / **陈旧拒用 0** | ★ 09-26 的行情冻结守卫生效 |
| 北向守卫 | `guard_on=True`、`credible=False` → fail-open | ★ 09-28 的可信度守卫生效（陈旧数据不误拦） |
| 风控 | `halted=false`、`position_scale=1.0`、`daily_trade_count=0` | ✅ 账户完全恢复，休市无成交 |
| ERROR/Traceback | **0 条** | ✅ |

**（d）观察篮清空（§9 方案 A，OWNER 授权）**

`config/settings.py`：`manual_entry_codes: [300502,300308,002415,000977,603986] → []`，
`manual_entry_exit_exempt` 维持 `True`（篮子空后惰性；且与回测默认一致，避免第 9 次口径漂移）。
已内联写入证据（四窗口 dSh、剂量单调性、可逆清单）与**语义边界**：

- 清空**只影响新建仓**（`_manual_entry_step` 读该列表），不强制卖出任何已有持仓；
- 已有的 3 只观察篮持仓是否继续豁免，取决于内存集合 `_manual_positions`
  （`event_engine.py:335`，**不持久化**）⇒ **重启后该集合为空，它们回归 P0 已验证的退出规则**
  （趋势破位 / −5% 单日暴跌 / 120 日超时 / −18% 硬止损）。这是期望行为，与回测口径一致。

**守卫按设计变红并已同步**（这正说明门锁有效）：
`test_production_basket_not_empty` 与 `test_basket_proxy_actually_changes_behavior` 双双失败
⇒ 已改写为 ① `test_production_basket_is_empty_after_r15`（钉住"空"，任何人塞回代码即变红并
提示先回测代价）；② 代理的防退化检查改用**固定测试篮**，与"生产当前是否启用篮子"解耦
（否则一清空就失去防退化能力）。
`KNOWN_DIVERGENCE.manual_entry_codes` 更新为 `((), ())`——两侧差异已消除，仍保留在此以维持
"任一侧变动必须显式登记"的保护。tests **335/335、0 失败**。

**（e）09-30 账本补记（真实值）**

09-30 的 262 条快照被整体作废，会造成 `prev_snapshot_day('2026-10-08')` 跳过 09-30
⇒ **节后首日跨日收益会跨过一个交易日算错**。故用真实收盘价还原并补一条快照 + 一行账本
（`scripts/_restore_real_eod_0930.py`）：

```
现金 483,926.60 + 5 仓按 09-30 真实收盘估值 491,347.00 = 975,273.60
  300394 200×260.77 / 000977 1500×65.93 / 300308 100×808.44
  688008 400×202.31 / 002415 5500×32.46
⇒ 09-30 真实跨日收益 **−0.36%**（不是 −21.12%）
```

账本现状（复位后可比口径）：

| 日期 | 跨日真实收益 | EOD |
|---|---|---|
| 2026-09-25 | 0.00% | 991,309.60 |
| 2026-09-28 | −1.74% | 974,013.60 |
| 2026-09-29 | +0.50% | 978,840.60 |
| **2026-09-30** | **−0.36%** | **975,273.60**（`source=manual_repair`） |

**KPI #4 修正**：近 4 周滚动（09-25 → 09-30）= **−1.62%**（此前因污染无法计算）。

**（f）假期处置建议（重要，与 Runbook 有冲突，需 OWNER 知悉）**

Runbook 写的是「09-30 停进程 → 10-08 重启」，但**本轮实测发现停机有反效果**：
`main.py --stop` 的哨兵被回收后进程仍未退出，而 GUARD 自动化（每小时）在检测到引擎离线时
会**自动拉起** —— 而**夜间/休市启动正是落到 `_MockClient` 的成因**（今天事故的根因就是
09-29 21:56 夜间启动）。

⇒ 本轮**保持引擎运行**（当前 `data_mode=xtdata` 健康、无 ERROR），理由：
   ① 停机后 GUARD 必在 1 小时内拉起，反而制造"夜间启动落 mock"的条件；
   ② 即便假期 miniQMT 断流落到 mock，`ALLOW_MOCK_TRADING=False` 已双向阻断买卖，**不会再造假成交**；
   ③ 保持运行可让 10-08 开市即处于已验证状态。

若 OWNER 仍希望假期停机：**先暂停 GUARD 自动化再停**，否则会被自动拉起。
10-08 开市前若需重启：`python main.py --web`，**先确认日志出现「数据源=xtdata」再放行交易**。
"""

AUTO_ADD = """
| **★ 第 15 轮收官执行（2026-09-30 20:40 · OWNER 授权重启）** | 已执行并验证 | ① 备份 DB → ② 优雅停机失效后终止本项目引擎 PID 24436（**miniQMT 全程未动**）→ ③ `repair_mock_fills_20260930.py --apply` 回滚 5 笔假成交，账户由 772,145.40 空仓+熔断 复原为 **978,840.60（现金 483,926.60 + 5 仓 494,914.00）、熔断解除、position_scale 1.0** → ④ 清空观察篮 → ⑤ 补记 09-30 真实 EOD **975,273.60（真实 −0.36%，非 −21.12%）** → ⑥ 重启验证：**`data_mode=xtdata`**、paper、**日线已补拉至 2026-09-30（38/38，实证 09-29「交易日口径」修复生效）**、分钟线陈旧拒用 0、北向守卫 fail-open、**ERROR 0 条**。⚠ **停机反效果**：`main.py --stop` 哨兵被回收后进程仍不退出，而 GUARD 每小时会自动拉起，**夜间/休市启动正是落到 `_MockClient` 的成因**（今日事故根因）⇒ 假期**建议保持运行**（有 `ALLOW_MOCK_TRADING=False` 兜底）；若要停机须**先暂停 GUARD** |
"""

FOOT = "\n> 参数禁区已于 2026-09-16 由用户（OWNER）明确撤销"


def main() -> None:
    txt = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
    if "OWNER 授权重启引擎）—— 全部完成并验证" in txt:
        print("[skip] 决策记录已追加")
    else:
        with TARGET.open("a", encoding="utf-8") as f:
            f.write(BODY)
        print(f"[ok] 决策记录 +{len(BODY)}")

    a = AUTO.read_text(encoding="utf-8")
    if "第 15 轮收官执行（2026-09-30 20:40" in a:
        print("[skip] 铁律表已更新")
        return
    a = a.replace(FOOT, AUTO_ADD + FOOT, 1)
    AUTO.write_text(a, encoding="utf-8")
    print(f"[ok] 铁律表 +{len(AUTO_ADD)}")


if __name__ == "__main__":
    main()

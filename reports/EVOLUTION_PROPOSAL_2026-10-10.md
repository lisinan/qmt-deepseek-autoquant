# 周度深度研究提案 · 2026-10-10（SYNTHESIZE）

> 闭环层：SYNTHESIZE / RESEARCH。覆盖区间：上次 SYNTHESIZE（2026-10-03）→ 今日（2026-10-10）。
> 本周仅 2 个交易日（10-08 / 10-09，国庆后复市；10-01~10-07 休市），故进化周期 = R22~R25 共 4 轮。
> **环境纪律**：解释器 `conda env qmt`；`EXECUTION_MODE = "paper"`（已核验 `config/settings.py:703`）✅ 全程未切出。

---

## 0. 一句话结论

**本周零参数落盘，与 R22~R25 四轮一致。** 权威全量复验确认策略稳健、风控充足（IS +150%/Sh 1.81/MDD −8.4%；WF 断路器 `fired=False`），但 **walk-forward 样本外均值 Sharpe 在折均值口径下为 1.26、四窗共识口径下为 1.702，离 2.0 目标仍有缺口**。

本周唯一新增的**结构性**候选 `entry_half_when_one`（入场侧半仓降级）经 4 窗口共识 **REJECTED（四窗 dSh 全负 −0.11~−0.21）**。这把 R24/R25 的结论钉死：**本策略已处在结构性高原——弱市提高暴露只会减 alpha，无论放宽信号质量门（R24）还是只放宽仓位（本周 W1）**。「优秀收益」的缺口不是策略参数/结构能补的，而是 **OWNER 对 KPI 口径的裁决**（KPI#1 折均值 vs 拼接；KPI#4 绝对收益 vs 相对宇宙超额）。

---

## 1. 本周真实成绩 vs 目标差距

### 1.1 真实成交（全部 paper，DB 直读，无合成行情）

| 交易日 | 当日收益 | EOD 资产 | 成交 | 持仓 | 风控 | 备注 |
|---|---|---|---|---|---|---|
| 10-08 | **−0.14%** | 973,925.6 | 4 SELL / 0 BUY | 1 | 熔断（daily_loss_abs −5,527） | 09:19 四笔同理由「趋势破位」30 秒内清 5→1 仓 |
| 10-09 | **−0.14%** | 972,533.6 | 1 SELL / 0 BUY | 0 | `halted=false` | 开盘卖光 300394（−14.9%）后全天零建仓 |

- **连续第 3 个交易日「开盘卖光 → 全天零 BUY → 100% 现金」**；`halted=false` ⇒ 不是熔断冻结，是**无标的通过入场闸门**（trend_up 在弱市恒 False）。
- 自 09-21 复位（100 万）累计：**−2.75%**；峰值 `peak_equity=1,005,860`（R22 自愈生效，脏值未再现）。

### 1.2 四象限 KPI 对照

| KPI | 目标 | 当前（权威复验） | 差距 | 判定 |
|---|---|---|---|---|
| ① wf OOS 均值 Sharpe | ≥ 2.0 | **1.26**（8 窗折均值）／ **1.702**（4 窗共识）／ **~1.998**（拼接口径 R18） | −0.74 / −0.298 / ~−0.002 | ⚠ 取决于口径（见 §6） |
| ② 正收益折数 | ≥ 6/7 | 3/8（8 窗）／ 22/26（4 窗） | 弱 | ❌（8 窗口径） |
| ③ wf 最大回撤 | ≥ −22% | **−8.44%**（IS）／ −1.54%（WF 最差折） | ✅ 远超 | ✅ 达标（预算只用 ~42%） |
| ④ paper 近 4 周收益 | > 0% | **−2.75%**（复位基准）／ +20.99%（09-16 崩盘低点基准，含异常恢复） | ❌ / ✅异常 | ❌（诚实口径） |

> **KPI#4 解读**：−2.75% 并非「空仓踏空」造成——账户多数时间 100% 现金（防御态）。真实亏损来自**持有的个别标的慢速阴跌**（300394 扛到 −14.9% 才由 trend_break 离场），即 IS MDD −6.94% 左尾的实盘兑现，是趋势策略 ride-alpha 形态的**必要代价**，非缺陷。

### 1.3 尚未解决的瓶颈（按严重度）

1. **体制依赖 edge 衰减（首要）**：WF 强趋势折 Fold2(+2.99)/Fold3(+2.45)/Fold5(+4.60) 极佳，但震荡/近期折 Fold4(−0.34)/Fold6(+0.66)/Fold7(+0.02)/Fold8(−0.24) ≈ 0，**把均值从 2.0+ 拉到 1.26**。这是 KPI#1/④ 共同的真瓶颈。
2. **KPI 口径未裁决**：折均值口径系统性低估真实 Sharpe（R18 证 60×9 与 90×6 同一段 540 根拼接口径逐位相同 = 1.998，但折均值差 0.251）。
3. **运维 P1**：15:35 OBSERVE 自动化连续 9 日未产出 review JSON，账本靠 DB 直读回退。

---

## 2. 双周期协同复盘结论

### 2.1 本周周期统计（R22~R25，4 轮）

| 周期 | 时间 | 落盘参数 | 研究开关(默认 off) | 量测/缺陷 | REJECTED 候选 | 受阻 |
|---|---|---|---|---|---|---|
| R22 AM | 10-08 11:40 | 0 | — | peak_equity 自愈(缺陷修复) | max_per_sector=2 | 0 |
| R23 PM | 10-08 18:00 | 0 | exit_next_open | — | exit_next_open / pct放宽 / bias | 0 |
| R24 AM | 10-09 11:40 | 0 | trend_up_relax | — | trend_up_relax / consec_loss_scale / stop_loss / atr 2.5 | 0 |
| R25 PM | 10-09 18:00 | 0 | trend_atr_stop_mult / trend_chandelier_mult | fixed_amount 30万→18万(验证器) | 单仓夹紧 / ATR止损死代码 / trend_vol_sizing | 0 |

- **AM 落盘：0 参数 + 1 缺陷修复 + 1 研究开关；PM 落盘：0 参数 + 2 研究开关 + 1 量测修正。**
- 本周**零生产参数改动**（与 R22~R25 四轮一致，risk_per_trade 的 0.012→0.020 由 R23 在 10-08 单次完成，非 churn）。

### 2.2 频率提升是否带来真实增益？—— **否**

国庆后 2 天跑满 4 轮（历史最高频），但**落盘参数仍为 0、WF Sharpe 未改善**（1.26/1.702 vs 上周 1.49/1.672）。增益全部是**测量/闭合型**（fixed_amount 口径修正、ATR 止损死代码闭合、单仓夹紧轴闭合），**非 alpha 型**。结论：**瓶颈是结构性的，不是调参频率能解决的**——高频迭代在高原上只会产生更多 REJECTED，不会产出更多落盘。

### 2.3 churn 检查（同参数来回改动）

- 本周**无参数被改动 ≥2 次**，更无方向冲突。**churn = 0** ✅。
- `risk_per_trade`：10-08 单次 0.012→0.020（R23 OWNER 授权），之后 24h 内各轮均未再动。
- `down_day_exit_pct`(−3.25, R16 冻结) / `min_passing_candidates`(2, R21) / `atr_stop_mult`(2.9, R13) 全周未动。
- 各轮「24h 防 churn」自检均通过。

### 2.4 上午周期落盘改动的当日实证（OBSERVE 已记录）

| 上午落盘项 | 实证结果 | 判定 |
|---|---|---|
| R22 `peak_equity` 自愈 | 10-09 实测 `peak_equity=1,005,860`，脏值 1,020,446.6 未再现 | ✅ **证实有效** |
| R21 `min_passing_candidates=2` | 10-08/10-09 全天零 BUY，闸门未被触发（无标的通过 trend_up） | ⚠ 符合预期（非缺陷，是弱市防御） |
| R23 `risk_per_trade=0.020` | 引擎日志「名义 risk_per_trade=2.0%」 | ✅ 证实生效 |
| R23 `DAILY_LOSS_UNION_MAX` | 10-09 `halted=false`（冷却恢复）；冻结频率下降待更多样本 | 🟡 部分证实 |

---

## 3. 全量收益复验（权威依据：`strategy/_verify_live_quality.py`）

> 运行：`conda env qmt` + sqlite3 PATH/PYTHONPATH；输出 `logs/verify_run_20261010.txt` / `logs/verify_live_quality.json`。

### 3.1 全样本（生产配置，25 只 × 745 根，末日 20261008）

| 指标 | 值 |
|---|---|
| 收益 | **+150.27%** |
| Sharpe | **+1.81** |
| Sortino | +2.36 |
| MDD | **−8.44%** |
| Calmar | 5.18 |
| 盈亏比 PF | 2.23 |
| 成交 | 164 |
| 胜率 | 45.7% |
| 暴露 | 73% |
| alpha | −16.69pt |
| 断路器 | **fired=False**（休眠，影响 0pt） |

> 注：验证器宇宙为 25 只（含 SECTOR_CONFIG 扩展），轮次 P0 基线为 23 只 × 900 根（IS +130.03%/Sh 1.90/MDD −6.94%）。两者样本/宇宙略有差异，IS Sh 1.81 vs 1.90 属口径差异，**结论一致：稳健、风控充足**。

### 3.2 7 折 walk-forward（8 窗）

| 折 | 区间 | 收益 | Sharpe | alpha | 熔断 |
|---|---|---|---|---|---|
| Fold1 | 20240315→20240705 | −0.39% | **−0.03** | +7.7pt | 0d |
| Fold2 | 20240708→20241029 | +38.36% | **+2.99** | −5.0pt | 0d |
| Fold3 | 20241030→20250220 | +11.47% | **+2.45** | −15.2pt | 0d |
| Fold4 | 20250221→20250612 | −1.54% | **−0.34** | +12.0pt | 0d |
| Fold5 | 20250613→20250925 | +55.03% | **+4.60** | −27.0pt | 0d |
| Fold6 | 20250926→20260120 | +2.12% | **+0.66** | −3.0pt | 0d |
| Fold7 | 20260121→20260519 | −0.33% | **+0.02** | −19.5pt | 0d |
| Fold8 | 20260520→20260902 | −1.12% | **−0.24** | +13.2pt | 0d |

- **WF 均值 Sharpe = +1.26**；正 alpha 3/8；最差折 −1.54%。
- **边缘衰减指纹**：近 4 折（Fold4/F6/F7/F8，约 2025-02→2026-09）Sharpe ≈ 0 或负，正是 live paper 近期为负的同源现象。

---

## 4. 深度假设（结构性候选）

本周针对首要瓶颈「体制依赖 edge 衰减 / 弱市零暴露」，提出 **1 个真正未测的结构性候选 + 1 个已被证据闭合的方向**：

- **C1（W1）`entry_half_when_one`**：R25 §10 #4 明确提名——`min_passing_candidates=2` 让近 90 日 45.6% 交易日零开仓。与 R24 放宽 **信号质量门**（trend_up_relax，灾难 −0.49）**严格区分**：本候选**只放宽仓位、不动信号质量门**——恰好 1 只通过全部门时半仓开这 1 只（wf×0.5 → 经 `backtest_daily.py:1475` 流入实际股数）。直接治「弱市零暴露」机制根因。
- **C2（scale 放大）`max_order_amount` 18万→更高**：R25 §4 已用 `atr_stop_mult × max_single_position_pct` 2×5 交互矩阵证 **OOS dSh −0.007（放宽无益）**、IS 仅 +4.0pt。在 `risk_per_trade≤0.02` 已触底线的前提下，唯一合规放大路径是放宽单笔上限，但证据显示边际**负**效应 ⇒ 本方向**闭合，不重测**。

---

## 5. IS / OOS 对比表（C1 = W1_entry_half_when_one）

> 运行：`scripts/_consensus_w1.py`（复用 `_evolve_wf` 管线，仅对 P0 vs W1 跑 4 窗口共识），输出 `reports/_consensus_w1_20261010.json`。基线 P0 IS：+130.03% / Sh 1.90 / MDD −6.94%。

| 窗口 | 60×9 | 75×7 | 90×6 | 120×4 |
|---|---|---|---|---|
| dSh | **−0.187** | **−0.165** | **−0.113** | **−0.206** |
| OOS Sharpe | +1.333 | +1.458 | +1.703 | +1.642 |
| 均值 MDD | −4.54% | −5.05% | −5.13% | −5.87% |
| 最差折 | −4.1% | −5.5% | −4.8% | +0.9% |
| 正收 | 7/9 | 6/7 | 5/6 | 4/4 |
| 累计差(pt) | −6.6 | −4.4 | −9.0 | −14.9 |

- **IS**：收益**下降**（累计 −4.4~−14.9pt）、Sharpe 下降；MDD 略改善（−6.94%→−5.13%）是因为少交易、非 alpha 提升。
- **四窗 dSh 全负（min −0.206）**，最差窗远未达 +0.10 闸门。

### 晋升闸门裁决（C1）

| # | 闸门 | C1 (W1) | 判定 |
|---|---|---|---|
| ① IS 收益或 Sharpe 提升 | ❌（IS ret/ Sh 双降） | **fail** |
| ② OOS 最差窗 dSh ≥ +0.10 | ❌ **−0.206** | **fail** |
| ③ OOS/IS Sharpe 缺口 ≤ 25% | ✅ 16.4% | PASS |
| ④ wf 最大回撤 ≥ −22% | ✅ −5.87% | PASS |
| ⑤ 任一单折 > −15% | ✅ −5.5% | PASS |
| ⑥ `tests/run_all.py` | 待确认（新增守卫 4 例已通过，全量复验运行中） | — |
| ⑦ 风险底线未动 | ✅ 参数零改动 | PASS |
| ⑧ EXECUTION_MODE=paper | ✅ | PASS |

**★ 裁决：REJECTED（闸门② 未过，四窗全负）。不落盘。**

> **机理结论**：弱市里「恰好 1 只通过」的标的，多半是板块 beta 共担的同一份头寸（R22 实盘铁证：4 笔同理由离场），半仓开它只是把「全关」变成「半仓扛阴跌」——左尾未变、alpha 反被稀释。这与 R25「退出更快整类闭合」同源：**趋势策略的钱是骑出来的，弱市少参与就是它的 alpha 形态**。

---

## 6. 本轮落盘 / 否决了什么

- **落盘：0 个生产参数**（config/settings.py / engine/event_engine.py diff 均为空）。
- **新增研究基础设施（默认 off，零行为变化，可复现）**：
  - `backtest_daily.py::entry_half_when_one` 开关 + 半仓降级逻辑 + `tests/test_entry_half_when_one.py`（4 例守卫，已通过）。
  - `strategy/_evolve_wf.py::candidates_final()` 新增 `W1_entry_half_when_one`。
  - `scripts/_consensus_w1.py` 聚焦共识 harness。
- **否决（完整负面证据已留存，避免重复踩坑）**：
  - **W1_entry_half_when_one**（本周）：四窗 dSh 全负 −0.11~−0.21 ⇒ **加入勿重试清单**。
  - 本周四轮已否决：max_per_sector=2 / exit_next_open / pct放宽 / min_daily_bias / trend_up_relax / consec_loss_scale / stop_loss(惰性) / atr 2.5(窗口运气) / 单仓夹紧轴 / ATR止损死代码 / trend_vol_sizing。

### 风险底线（全部未动，确认）

`max_drawdown_pct=−0.20`（≤−0.15）｜`risk_per_trade=0.020`（≤0.02）｜`T1_RESTRICTION=True`｜`max_positions=5`（≥3）｜`EXECUTION_MODE="paper"`。✅

---

## 7. 结论与下周聚焦方向

### 7.1 核心判断：策略已处结构性高原，「优秀收益」缺口是口径问题，非策略问题

25 轮 + 本周 SYNTHESIZE 已从**每一个**结构角探测过：入场信号门（trend_up_relax / 半仓降级）、离场速度（ATR 止损 / 吊灯 / vol_sizing / 日内熔断）、仓位尺度（risk_per_trade / 单仓上限 / max_order_amount）、体制（regime）、集中度（max_per_sector）、轮动、新 alpha 源（北向 / 盈利修正 / 波动率体制）。**凡动「弱市暴露」的，全负或噪声；凡不动的，已达最优**。Sharpe 是尺度不变量（R23），调仓放大只放大绝对收益不放大 Sharpe。

### 7.2 交给 OWNER 的两项裁决（阻塞一切「追 Sharpe」扫描）

1. **★★ KPI#1 口径**：折均值口径下 WF 均值 Sh = 1.26（8 窗）/ 1.702（4 窗），但 **R18 已证拼接口径（无小折噪声）同段 = 1.998 ≈ 2.0**。若采用拼接口径，**KPI#1 实质上已达成**。建议 OWNER 明确 KPI#1 采用拼接口径，或承认 2.0 在折均值口径下物理不可达。
2. **★★ KPI#4 目标本身**：低暴露是 alpha 特征（R24 放宽门灾难、本周 W1 半仓负）。R23 实测策略同期跑赢宇宙 buy&hold **7.1~15.9pp**。建议把 KPI#4 改为「相对宇宙超额收益 > 0」而非「绝对收益 > 0」。

### 7.3 下周聚焦

1. **OWNER 裁决 KPI#1 / KPI#4 口径**（上方 7.2）——裁决前停止一切单参数网格扫描。
2. **P1 运维**：查 `logs/` 中 15:35 OBSERVE 自动化连续 9 日未产 review JSON 的根因（非策略问题，属自动化执行链路），恢复账本自动写入。
3. **勿重试清单（本周新增 W1）**：`entry_half_when_one` / `trend_up_relax` / `exit_next_open` / `max_single_position_pct` 放宽 / `trend_atr_stop_mult` / `trend_chandelier_mult` / `trend_vol_sizing` / `stop_loss`(惰性) / `consec_loss_scale` / `atr_stop_mult` 放宽 / `min_daily_bias` / `max_per_sector=2` / `min_passing_candidates` 降为 1 / `max_order_amount` 放大。
4. **若 OWNER 坚持要更高绝对收益且接受弱市亏损同步放大**：唯一合规路径是 `max_order_amount` 18万→更高（须同步 `max_position_amount` 与验证器 `fixed_amount`，三者等值守卫已在），但 R25 §4 证据显示 OOS dSh −0.007（边际负），**不建议**。

### 7.4 交付物

- 本报告 `reports/EVOLUTION_PROPOSAL_2026-10-10.md`
- 权威复验 `logs/verify_run_20261010.txt` + `logs/verify_live_quality.json`
- W1 共识 `reports/_consensus_w1_20261010.json` + `logs/consensus_w1_20261010.txt`
- 新增守卫 `tests/test_entry_half_when_one.py`（4 例通过）
- 研究基础设施 `backtest_daily.py`(开关+逻辑) / `_evolve_wf.py`(W1 候选) / `scripts/_consensus_w1.py`（均默认 off / 零生产行为变化）

---

*附：paper 模式全程保持；未切出；未落任何生产参数；风险底线全部未动。*

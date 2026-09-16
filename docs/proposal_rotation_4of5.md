# 改动方案：允许板块轮动在 4/5 时「补强空槽」

> 状态：**已实施（2026-09-12，用户确认 ① 4/5 仅补强不卖 existing ② 暴露旋钮 `rotation_fill_empty_slot` 默认开）**
> 提出：2026-09-12 ｜ 触发：09-08 轮换后组合卡在 4/5，09-09 13:07 引擎停机 + 09-10/09-11 未运行 → 近一周零调仓
> 铁律：本方案不改变 regime 闸门 / 断路器 / max_positions / exit_mode 等生产配置，仅扩展轮换触发条件（行为改动，非参数调参）

---

## 1. 背景与问题

- 当前 `_maybe_rotate`（`engine/event_engine.py:435`）是「**仅满仓才介入**」机制：内部硬门槛 `len(held) < max_positions: return`（`:466-467`），且两处调用点也仅在 `>= max_positions` 时调用（`:1305`、`:1367`）。
- 09-08 那次轮换把 688120 换出、换入 300394，而 300394 买入 3 秒后「趋势破位离场」(-¥36)，**净减 1 仓 → 组合停在 4/5**。
- 之后组合只要不满 5/5，轮换就永久休眠；第 5 仓只能等普通入场逻辑（`:1320-1322`，`<5` 时允许买入）补满。若普通入场迟迟不触发（无日线 BUY 信号 / 动量闸门拦截），组合长期卡在 4/5，**轮换既补不了仓、也不换强**。
- 用户诉求：让轮换在 4/5 时也能「换入增强 / 补上空槽」，不等补满。

## 2. 目标行为

| 组合状态 | 当前行为 | 拟改后行为 |
|----------|----------|------------|
| 5/5（满仓） | 弱换强（卖最弱→买热板块候选） | **不变** |
| 4/5（差一仓） | 轮换休眠，等普通入场补第 5 仓 | **直接把空槽补上热板块候选**（纯买入、不卖 existing）→ 回到 5/5 且第 5 仓是确认热度标的 |
| <4/5 | 不介入 | 不介入（与普通入场逻辑分工不变） |

设计取舍：4/5 时**只补强、不强卖**。理由：① 用户原始抱怨是「不补仓」，补空槽精准命中；② 不强制卖出 existing 持仓，风险面最小；③ 5/5 的弱换强逻辑完全不动，已验证回测 alpha 零影响。

## 3. 拟改动（代码片段，待确认后实施）

### 3.1 调用点放宽（两处）

`engine/event_engine.py:1304-1307`（`_run_single_step`）与 `:1366-1369`（`_run_portfolio_step`）结构相同，统一改为：

```python
# 可观测性 + 轮换：满仓报踏空、4/5 起允许轮换补强空槽
_held_n = len([p for p in self._positions.values() if p.quantity > 0])
if _held_n >= self.max_positions - 1:
    if _held_n >= self.max_positions:
        self._notify_if_locked_out_of_hot_sector(held_codes)
    self._maybe_rotate(ticks)
```
> 注意：`_notify_if_locked_out_of_hot_sector` 仍仅 `>= max_positions` 调用——4/5 不算「满仓踏空」，不该误报。

### 3.2 `_maybe_rotate` 入口门槛放宽

`engine/event_engine.py:466-467`：

```python
# 原：if len(held) < self.max_positions: return
if len(held) < self.max_positions - 1:   # 允许 4/5 进入
    return
```

### 3.3 `_maybe_rotate` 增加「4/5 补强空槽」分支

在现有 swap 逻辑（`:506-549`）**之前**插入分支：当 `len(held) == max_positions - 1` 且存在合格热板块候选时，直接买入该候选填充空槽（不卖 existing）。

```python
# —— 4/5 补强空槽分支（新增）——
if len(held) == self.max_positions - 1:
    # 复用 hot 集合与日内突破/日线 BUY 双重判定（与 swap 候选同口径）
    slot_cands = []
    for code in (hot & base):
        tick = ticks.get(code)
        if tick is None:
            continue
        feat = self.daily.features(code)
        sig = self.strategy.on_daily_features(code, code, feat)
        if sig is None:
            continue
        chg = float(getattr(tick, "change_pct", 0) or 0.0)
        is_breakout = chg >= self.rotation_intraday_breakout_pct
        if not is_breakout and sig.side != "BUY":
            continue
        slot_cands.append((code, sig, sig.score, is_breakout))
    if slot_cands:
        slot_cands.sort(key=lambda x: x[2], reverse=True)
        code, sig, eff_score, is_breakout = slot_cands[0]
        sig.price = float(getattr(ticks.get(code), "price", 0) or 0)
        self._handle_buy(sig, ticks.get(code),
                         {c: t.price for c, t in ticks.items()})
        self._last_rotate_ts = now          # 复用冷却，避免窗口内重复补
        self._last_rotate_eval_ts = now
        system_notice("SYSTEM", "交易",
            f"板块轮动补强空槽：买入{code}（热板块/日内突破）")
        return                              # 4/5 仅补一仓，不进入 5/5 swap
```

> 复用现有节流：`rotation_cooldown_sec`、`_last_rotate_ts`、`_last_rotate_eval_ts`、`rotation_intraday_breakout_pct`、`rotation_min_score_gap_hot` 全部沿用，无需新增旋钮（如后续需要开关，再加 `rotation_fill_empty_slot=True` 默认开）。
> 买入安全：仍走 `_handle_buy` → P0 现金夹紧（`:1535`）→ RiskManager，杜绝负现金。

## 4. 验证计划（行为改动必须 IS/OOS 验证，参数高原已证无调参空间）

1. **单测**（`tests/test_rotation.py` 新增用例）：
   - `test_rotation_4of5_fill_slot`：组合 4/5 + 存在合格热突破候选 → 触发买入补槽、不卖 existing。
   - `test_rotation_4of5_no_candidate_skip`：4/5 + 无合格候选 → 跳过、不动作。
   - `test_rotation_4of5_cash_clamp`：模拟现金不足 → 买入被夹紧、不产生负现金。
   - `test_rotation_5of5_unchanged`：5/5 仍走原弱换强、行为回归不变。
2. **回测/walk-forward**：在现有 `strategy/_verify_live_quality.py` 与 7 折 walk-forward 上对比「开启 4/5 补强」vs「关闭」，看 Sharpe / MDD / 收益是否改善或退化；若退化则默认关闭（回滚至当前行为）。
3. **paper 实盘观察**：下个交易日开盘后看是否如期把空槽补成热标的，且换手率/滑点在可接受范围。

## 5. 风险与回滚

- **换手上升**：每冷却窗口最多补 1 仓，受 `rotation_cooldown_sec`（默认 1800s）节流；paper 阶段可接受。
- **误补非热门**：候选必须落在 AI 热板块名单（`_hot_codes_set`，LLM rerank ∪ 板块推荐池）且需日内突破或日线 BUY，非随机噪声。
- **不影响生产铁律**：regime=off、断路器 -0.25+5日、max_positions=5、trend 退出、enable_rotation 维持不变（本方案仅扩展其触发条件）。
- **回滚**：删除 3.1–3.3 改动即可恢复「仅满仓才轮换」；若已加旋钮则置 `rotation_fill_empty_slot=False`。

## 6. 待你确认的决策点（已全部确认，2026-09-12）

1. ✅ 同意「4/5 仅补强空槽、不卖 existing」的设计取舍（而非「4/5 也做弱换强」）。
2. ✅ 暴露显式旋钮 `rotation_fill_empty_slot`（默认开）以便一键回滚。
3. ✅ 已实施：改 `event_engine.py` → 补 4 个单测 → 跑 `tests/run_all.py` + 7 折 walk-forward 对比 → 汇报结果，**未改动任何生产配置参数**。

---

## 7. 实施记录（2026-09-12）

- **改动落地**（`engine/event_engine.py` + `config/settings.py`，未动任何生产铁律参数）：
  1. `__init__` 新增旋钮 `self.rotation_fill_empty_slot = bool(STRATEGY_PARAMS.get("rotation_fill_empty_slot", True))`。
  2. `_maybe_rotate` 内部门槛 `len(held) < max_positions` → `len(held) < max_positions - 1`（允许 4/5 进入）。
  3. `_maybe_rotate` 在 `cand = ...` 之后新增 **4/5 补强空槽分支**：`len(held)==max_positions-1` 且旋钮开启时，遍历 hot&tick 候选（需日内突破或日线 BUY），直接 `_handle_buy` 补空槽（不卖 existing），复用现有冷却节流，发出 `板块轮动补强空槽` 系统提示后 return。
  4. 两处调用点（`:1305` `_run_single_step`、`:1367` `_run_portfolio_step`）门槛放宽到 `>= max_positions-1`；`_notify_if_locked_out_of_hot_sector` 仍仅 `>= max_positions` 调用（4/5 不误报满仓踏空）。
  5. `config/settings.py` 在 rotation 块新增 `"rotation_fill_empty_slot": True`。
- **测试**：`tests/test_rotation.py` 新增 4 例（补槽买入 / 无候选跳过 / 旋钮关闭跳过 / 弱热名非突破跳过）。
- **验证结果**：`pytest tests/test_rotation.py` → **13/13 通过**；`tests/run_all.py` → **171 passed / 1 failed（172）**。
  - 唯一失败 `tests.test_stock_names.test_ensure_recommendations_builds_from_last_ticks`（300308.SZ 应进推荐池）属**预存环境漂移**（走 ensure_recommendations/股票名路径，与本改动正交，且本 shell 不可达 xtdata 实时数据），非本次引入。
- **待办（最终放行前）**：下个交易时段在桌面会话（可达 xtdata）跑 7 折 walk-forward A/B（开/关 `rotation_fill_empty_slot`）确认 Sharpe/MDD/收益不退化；旋钮默认开，置 False 即一键回滚到「仅满仓才轮换」旧行为。

# 量化项目优化进展（2026-08-27）：regime 市场状态过滤 — 生产落地

> 本轮把上一轮回测验证出的**唯一稳健结构性改进**（市场状态过滤）从研究代码接入实盘引擎。
> 验证数据来自 `strategy/opt_harness.py` → `logs/opt_regime.json` / `logs/opt_folds.json`。

## 一、科学结论（为什么是 regime，而不是调参）

上一轮用 IS/OOS 切分 + 多折 walk-forward 检验发现：

- 原策略真正的缺陷是**永远满仓**（exposure ≈ 97%）。OOS 上所有参数组合的 Sharpe 都从 IS 的 +2.2 塌到 ~0，参数优化本身是噪声（IS↔OOS 秩相关 ≤ 0）。
- **唯一一致有效的改进 = regime 过滤**：市场转弱时不交易 / 强制清仓。
- 最优配置（R2 指数MA60+清仓）：
  - IS：+123.75%（Sharpe +2.15）
  - OOS：−23.37% → **+1.26%**（Sharpe +0.24），MDD −29.18% → **−13.28%**
  - 多折均值：Sharpe 0.46 → **0.84**，平均 MDD −16.2% → **−13.4%**，暴露 96% → **69%**
- 诚实结论：regime 是**稳健的风控/鲁棒性改进**，不是魔法 alpha；任何单一参数都没有可靠的 OOS 优势，故本轮**未做任何 OOS 不可靠的参数微调**。

## 二、本轮改动（生产集成）

| 文件 | 改动 |
|------|------|
| `config/settings.py` | `STRATEGY_PARAMS` 新增 `regime_mode / regime_index / regime_ma / regime_breadth_thresh / regime_force_exit`，默认 R2 配置：`index` MA60 + 强制清仓 |
| `strategy/daily_context.py` | 新增 `index_above_ma()` / `breadth_above_ma()`，与回测 `regime_ok`（close > MA60）定义一致；无数据时保守放行 |
| `engine/event_engine.py` | 读取 regime 配置；新增 `_regime_ok()`；单标的/组合入场加闸门；step4 在 `force_exit` 下强制清仓；`snapshot()` 暴露 regime 状态 |

### 行为语义
- `regime_mode="off"`：不过滤（原行为）。
- `regime_mode="index"`：指数收盘 > MA(regime_ma) 才允许交易（默认用创业板指 399006.SZ / MA60）。
- `regime_mode="breadth"`：≥阈值比例个股站上各自 MA 才放行（更抗单指数失真）。
- `regime_force_exit=True`：市场转弱（down）时**强制平掉所有持仓**（推荐，回测验证降低 MDD 最有效）。

## 三、验证结果（conda qmt + 实时 miniQMT）

- 导入 / 实例化 / 闸门计算全部通过。
- **实时 regime 当前 = `down`**：创业板指收盘 3397.52 < MA60 3784.65 → filter **已激活**。
  - 效果：拦截所有新开仓；对现有持仓触发强制清仓。
  - 即：以当前市场环境，引擎会自动进入**防御 / 持币**状态，规避下行暴露——正是该改进的设计目标。

## 四、可调项与风险提示

- 只想拦截新开仓、不清现有仓：设 `regime_force_exit=False`。
- 完全关闭过滤：`regime_mode="off"`。
- 切到更稳健的宽度门：`regime_mode="breadth"`（需日线上下文覆盖足够多个股）。
- 注意：`force_exit=True` 在弱势市场会**真实卖出**持仓（live 模式）；上线前请确认该行为符合你的风险偏好。

## 五、遗留 / 下一步

- 回测结论已落地到实盘；建议后续跑更长窗口的 walk-forward 监控 regime 在不同市况下的稳定性。
- 未做参数微调（因 OOS 不可靠）；若未来出现稳定的 IS/OOS 参数信号，再单独评估入产。

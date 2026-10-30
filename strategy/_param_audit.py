# -*- coding: utf-8 -*-
"""参数读点审计 · 单一事实源（供 RSI 元审计脚本与测试共用）。

【为什么需要这个模块（2026-10-28 R42 递归自进化第 1 轮）】
R41 曾把「回测无反应」当作「参数无价值」，后来才发现其中 7 个参数在
`backtest_daily.py` 里**根本没有读取点** ⇒ 那些「证伪」测的是空气。
同类风险无法靠人记：参数会增删、读取点会搬家。

本模块把「每个生产参数究竟被谁读」变成**可计算的事实**，并由
`tests/test_search_space_contract.py` 钉住一张**显式清单**：
  · 双向 / 别名可验证 → 闸门可信
  · 仅实盘            → 回测测不到，结论只能标「未验证」，不得记「已证伪」
  · 死参数            → 任何网格结论必然是 0 变化，纯浪费迭代
任何新增参数、或某键突然获得/失去读点，测试立即红，强制显式归类。

【识别口径（必须知道，否则会误读）】
读点 = AST 里出现该名字，覆盖四种形态：
  1. STRATEGY_PARAMS.get("x") / params["x"]   → 字符串常量
  2. cfg.x / self.p.x                         → 属性访问
  3. replace(cfg, x=...)                      → 关键字实参
  4. 直接引用 / 函数名                          → Name.id
注释与 docstring 天然不计（AST 不含注释；整段 docstring 是一个常量，不等于键名）。
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, Set

ROOT = Path(__file__).resolve().parent.parent

# 非生产目录：不参与「读点」判定
EXCLUDE_DIRS = {"tests", "scripts", "config", "docs", "reports", "__pycache__",
                ".git", ".workbuddy", "storage", "logs", "node_modules"}

# 研究侧文件（回测/验证器/优化台/统计）；其中 kpi_stats 只看指标不带参数读点
RESEARCH_FILES = ("backtest_daily.py", "backtest_minute.py",
                  "_evolve_wf.py", "opt_harness.py", "kpi_stats.py")

# 回测字段 ← 生产键 的**已确认**别名（同语义、同单位、且该路径在 P0 中真实生效）。
# ★ 刻意只留一条：
#   · fixed_amount ↔ max_order_amount  —— 仓位金额，P0 中始终生效；
#   · consec_loss_trigger/halt、daily_loss_pct 虽然数值等价，但对应**闸门在 P0 中未启用**
#     （consec_loss_scale=False / daily_loss_halt=False，见 reports/OPEN_DECISIONS.md 选项 B）
#     ⇒ 数值接线≠行为接线，仍按「仅实盘·闸门未建模」登记，避免给出「可验证」的错觉；
#   · daily_loss_abs_pct 是**比例**、daily_loss_limit_abs 是**金额**，量纲不同，不是同一件事。
ALIAS: Dict[str, str] = {
    "fixed_amount": "max_order_amount",
}

# 本模块自身列出全部参数名（契约表），必须从读点扫描中排除，否则会自污染
SELF_EXCLUDE = {Path(__file__).resolve()}


def source(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def identifiers(path: Path) -> Set[str]:
    src = source(path)
    if not src:
        return set()
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return set()
    out: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            out.add(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.add(node.value)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            out.add(node.name)
    return out


def _collect(group: str) -> Dict[Path, Set[str]]:
    if group == "research":
        files = [ROOT / "strategy" / n for n in RESEARCH_FILES]
        return {p: identifiers(p) for p in files if p.exists()}
    research = {(ROOT / "strategy" / n).resolve() for n in RESEARCH_FILES}
    files = []
    for p in ROOT.rglob("*.py"):
        parts = p.relative_to(ROOT).parts
        if parts and parts[0] in EXCLUDE_DIRS:
            continue
        if p.resolve() in research or p.resolve() in SELF_EXCLUDE:
            continue
        files.append(p)
    return {p: identifiers(p) for p in files}


def classify() -> Dict[str, dict]:
    """返回 {参数名: {class, live, research, alias, files}}。"""
    from config.settings import RISK_PARAMS, STRATEGY_PARAMS

    live = _collect("live")
    research = _collect("research")
    research_ids: Set[str] = set().union(*research.values()) if research else set()
    # 别名：仅当该回测字段确实被研究侧代码消费时才算「可验证」
    alias_ok = {k: f for f, k in ALIAS.items() if f in research_ids}

    out: Dict[str, dict] = {}
    for key in list(STRATEGY_PARAMS) + list(RISK_PARAMS):
        live_hits = sorted(p.relative_to(ROOT).as_posix()
                           for p, ids in live.items() if key in ids)
        res_hits = sorted(p.relative_to(ROOT).as_posix()
                          for p, ids in research.items() if key in ids)
        ln, rn = len(live_hits), len(res_hits)
        alias = alias_ok.get(key)
        if not (ln or rn or alias):
            cls = "死参数"
        elif ln and (rn or alias):
            cls = "双向可验证" if rn else "别名可验证"
        elif ln:
            cls = "仅实盘"
        else:
            cls = "仅回测"
        out[key] = {"class": cls, "live": ln, "research": rn,
                    "alias": alias, "live_files": live_hits,
                    "research_files": res_hits}
    return out


# ---- 契约：必须与 classify() 实际结果一致，由测试强制 ----
# 仅实盘（回测测不到）→ 其 A/B 结论只能标「未验证」，**不得记「已证伪」**
CONTRACT_LIVE_ONLY: Dict[str, str] = {
    # 分钟级技术指标：回测为日线驱动，分钟路径未建模
    "ma_short": "分钟级指标", "ma_medium": "分钟级指标", "ma_long": "分钟级指标",
    "rsi_period": "分钟级指标", "macd_fast": "分钟级指标", "macd_slow": "分钟级指标",
    "macd_signal": "分钟级指标", "kdj_period": "分钟级指标",
    "boll_period": "分钟级指标", "boll_std": "分钟级指标",
    "atr_period": "分钟级指标", "volume_surge": "分钟级指标",
    # 熔断/风控：回测对应字段是硬编码常量，或闸门在 P0 中未建模
    #   （daily_loss_halt=False / consec_loss_scale=False，见 OPEN_DECISIONS 选项 B）
    "daily_loss_limit_pct": "回测闸门未建模（daily_loss_halt=False）",
    "daily_loss_limit_abs": "回测闸门未建模（daily_loss_halt=False）",
    "halt_recover_days": "回测无恢复期建模",
    "max_consecutive_losses": "回测闸门未建模（consec_loss_scale=False）",
    "max_consecutive_losses_halt": "回测闸门未建模（consec_loss_scale=False）",
    "max_daily_trades": "回测无日内笔数限制",
    "dd_recover_days": "回测无回撤恢复期建模",
    # 结构开关：回测器无同名开关（★ enable_rotation 的历史「证伪」无回测支撑）
    "enable_rotation": "回测器无 rotation 总开关；历史「轮换无价值」应改标未验证",
    "rotation_cooldown_sec": "轮动冷却以交易日/秒两套口径，回测未建模秒级冷却",
    "momentum_scope": "动量统计范围（分钟/日线）回测固定日线",
    "require_daily_data": "实盘「无日线则拒绝入场」的保护，回测天然有日线",
    # 链路验证专用（2026-10-29）：观察篮「单笔固定金额」只在实盘引擎读取，
    #   回测 BacktestConfig 无此概念，且它对 P0 无任何读取点 ⇒ 不可回测验证。
    "manual_entry_amount": "live-only 链路验证参数（回测无观察篮固定金额概念）",
    # 【2026-10-30】声明式退出豁免清单：只影响实盘「是否自动卖出」，回测器没有
    #   「外部/手工持仓」这一概念（回测持仓全部由策略自己开）⇒ 无处读取。
    "exit_exempt_codes": "live-only 声明式退出豁免（回测无手工持仓概念）",
}

# 死参数（全仓库生产代码无读点）→ 任何网格纯浪费
CONTRACT_DEAD: Dict[str, str] = {
    "rsi_oversold": "RSI 阈值硬编码 30/70/80（daily_context.py / backtest_daily.py）",
    "rsi_overbought": "RSI 阈值硬编码 30/70/80",
    "max_position_amount": "遗留键；实际生效为 max_order_amount"
                           "（tests/test_leverage_scale_invariance.py 仍对死键断言）",
}


def contract_violations() -> list:
    """返回契约与实测不一致的条目（测试用；空列表 = 通过）。"""
    actual = classify()
    bad = []
    for key, reason in CONTRACT_LIVE_ONLY.items():
        row = actual.get(key)
        if row is None:
            bad.append(f"{key}: 契约登记为「仅实盘」，但生产配置里已不存在（{reason}）")
        elif row["class"] != "仅实盘":
            bad.append(f"{key}: 契约=仅实盘，实测={row['class']}"
                       f"（live={row['live']} research={row['research']}"
                       f" alias={row['alias']}）—— 若已接线请从本表移除")
    for key, reason in CONTRACT_DEAD.items():
        row = actual.get(key)
        if row is None:
            bad.append(f"{key}: 契约登记为「死参数」，但生产配置里已不存在（{reason}）")
        elif row["class"] != "死参数":
            bad.append(f"{key}: 契约=死参数，实测={row['class']}"
                       f"（live={row['live']} research={row['research']}）"
                       f"—— 若已接线请从本表移除")
    # 反向：实测为「仅实盘/死参数」却未登记 → 漏登记（新参数必须归类）
    for key, row in actual.items():
        if row["class"] == "仅实盘" and key not in CONTRACT_LIVE_ONLY:
            bad.append(f"{key}: 实测「仅实盘」但未登记（回测测不到 ⇒ 结论不得记已证伪）")
        if row["class"] == "死参数" and key not in CONTRACT_DEAD:
            bad.append(f"{key}: 实测「死参数」但未登记（跑网格纯浪费）")
    return bad


def summary() -> Dict[str, int]:
    out: Dict[str, int] = {}
    for row in classify().values():
        out[row["class"]] = out.get(row["class"], 0) + 1
    return out


if __name__ == "__main__":
    rows = classify()
    print("分类统计:", summary())
    for cls in ("死参数", "仅实盘", "仅回测", "别名可验证"):
        ks = [k for k, v in rows.items() if v["class"] == cls]
        if ks:
            print(f"\n[{cls}] {len(ks)}: {ks}")
    v = contract_violations()
    print(f"\n契约校验: {'通过' if not v else f'{len(v)} 项不一致'}")
    for x in v:
        print("  -", x)

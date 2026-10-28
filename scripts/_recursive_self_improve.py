# -*- coding: utf-8 -*-
"""递归自进化 · 元审计（Recursive Self-Improvement Meta Audit）

定位：本项目已进入「参数空间无可挖 alpha」阶段（R40/R41）。
      继续调参数是**一阶**优化；本脚本做**二阶**优化 —— 审计「改进流程本身」，
      找出流程里哪些环节在浪费迭代、哪些能力缺失，并给出下一轮该改的环节。

审计四层：
  L1 搜索空间审计：65 个生产参数，逐个统计「实盘读点 / 回测读点」
        · 双向可验证 = 两侧都有读点 → 可被闸门验证
        · 仅实盘     = 回测无读点 → 研究侧测不到（会写出「回测有效实盘无效」的假结论）
        · 仅回测     = 实盘无读点 → 落盘即死代码（项目已记录 reentry_cooldown 属此类）
        · 死参数     = 两侧都无读点 → 任何网格扫描都是纯浪费
  L2 循环活性：各自动化最近一次运行时间（memory.md mtime），识别停摆
  L3 决策账本：轮次数 / 否决数 / 落盘数 / 迭代效率（落盘 ÷ 轮次）
  L4 循环能力：划分不变量指标、多窗口共识、不变性测试、单仓上限守卫 是否具备

只读、零副作用、不写任何生产文件。
"""
from __future__ import annotations

import ast
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import RISK_PARAMS, STRATEGY_PARAMS  # noqa: E402

OUT = ROOT / "reports" / "meta"
OUT.mkdir(parents=True, exist_ok=True)

AUTOS = {
    "ae0e2431": "盘后复盘 OBSERVE", "98ffc7a2": "盘前就绪 PRE-MARKET",
    "789d671e": "上午自进化 AM-EVOLVE", "d8013e93": "周中自进化 PM-EVOLVE",
    "f093d93b": "周度深度 SYNTHESIZE", "ebde5d30": "守护兜底 GUARD",
    "2534a859": "工程维护 17:00", "1c6e4605": "开仓归因日报",
    "6265093f": "量测卫生守卫", "4a425d4f": "递归自进化",
}

# ★ 单一事实源（2026-10-28 R42）：读点扫描逻辑（identifiers / _collect /
#   分类 / 别名）**全部**在 strategy/_param_audit.py，并由
#   tests/test_search_space_contract.py 钉住。
#   为什么删掉本脚本自带的副本：同一批参数，脚本报「仅实盘 24」而契约模块报
#   23 —— 双份实现从诞生起就在静默漂移，而报告与守卫读的是不同那份。
#   历史教训：判「无读点」必须扫**全仓库**（曾只扫 engine/+strategy/+main.py，
#   漏掉 risk/manager.py ⇒ 5 个熔断键被误判死参数）；误报比漏报更伤人。
from strategy import _param_audit as PA  # noqa: E402

# 新增：待首跑的新任务不应算「停摆」
NEW_AUTOS = {"1c6e4605", "6265093f", "4a425d4f"}

# 注：本脚本原本还转出了 EXCLUDE_DIRS / RESEARCH_STRATEGY 两个别名和一个
# count_hits()，去重后均无调用点 —— 已删。
# （一个审计「死参数」的脚本自己留死代码，是同一类问题的镜像，故一并清掉。）


def _str_consts(node) -> set:
    """取表达式里出现的全部字符串常量（含 STRATEGY_PARAMS["x"] 这种下标）。"""
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.add(sub.value)
    return out


def audit_wiring() -> dict:
    """解析研究侧「生产键 → 回测字段」的显式/别名映射。

    背景（必须处理，否则结论失真）：
      · 回测用别名建模：`consec_loss_trigger` ← RISK_PARAMS["max_consecutive_losses"]，
        `daily_loss_pct`       ← RISK_PARAMS["daily_loss_limit_pct"]；
      · 只按同名键判读点会把这类正确接线误判成「实盘有、回测无」。
    做法：扫 `BacktestConfig(...)` / `replace(cfg, ...)` 的关键字实参，
          若实参值里出现生产键名，则记录 key → 回测字段 的映射；
          再确认该字段在回测代码里确实被读取。
    """
    keys = set(STRATEGY_PARAMS) | set(RISK_PARAMS)
    research_ids = PA._collect("research")
    mapping: dict = {}
    for p, ids in research_ids.items():
        try:
            tree = ast.parse(PA.source(p))
        except (SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fname = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if fname not in ("BacktestConfig", "replace"):
                continue
            for kw in node.keywords:
                if not kw.arg:
                    continue
                for k in _str_consts(kw.value) & keys:
                    ent = mapping.setdefault(k, {"fields": set(), "files": set()})
                    ent["fields"].add(kw.arg)
                    ent["files"].add(p.name)
    # 该字段是否真的被回测代码消费
    consumed = set().union(*research_ids.values()) if research_ids else set()
    for k, ent in mapping.items():
        ent["fields_consumed"] = sorted(f for f in ent["fields"] if f in consumed)
        ent["wired"] = bool(ent["fields_consumed"])
        ent["fields"] = sorted(ent["fields"])
        ent["files"] = sorted(ent["files"])
    return mapping


def audit_search_space() -> dict:
    """搜索空间分辨率：76 个生产参数里有几个「回测真能验证」。

    分类结果**直接来自** `strategy/_param_audit.py`（单一事实源），本函数只做
    报告层加工（补 note / wiring 展示），绝不自行重算分类 —— 否则报告与
    `tests/test_search_space_contract.py` 的守卫会读到两套数字。
    """
    rows = PA.classify()
    wiring = audit_wiring()
    live_files = sorted({f for r in rows.values() for f in r["live_files"]})
    research_files = sorted({f for r in rows.values() for f in r["research_files"]})

    note_of = {
        "死参数": "全仓库生产代码无读点 → 任何网格纯浪费",
        "仅实盘": "回测器不读该键 → A/B 测的是另一个策略，结论只能记「未验证」",
        "仅回测": "实盘无读点 → 落盘即死代码（回测收益含实盘没有的来源）",
        "双向可验证": "同名读取，闸门可验证",
        "别名可验证": "经别名接线，闸门可验证",
    }
    out_rows = []
    for key, r in rows.items():
        w = wiring.get(key) or {}
        note = note_of[r["class"]]
        if r["alias"]:
            note = f"经别名接线：{key} → 回测字段 {r['alias']}"
        elif r["class"] != "双向可验证" and w.get("fields_consumed"):
            # 有接线但闸门在 P0 未启用（daily_loss_halt / consec_loss_scale = False）
            note += f"（虽有接线 {', '.join(w['fields_consumed'])}，但闸门在 P0 未启用）"
        out_rows.append({"param": key, "live": r["live"], "research": r["research"],
                         "alias": [r["alias"]] if r["alias"] else [],
                         "class": r["class"], "note": note,
                         "live_files": {f: 1 for f in r["live_files"]},
                         "research_files": {f: 1 for f in r["research_files"]}})
    return {"rows": out_rows, "live_files": live_files,
            "research_files": research_files,
            "wiring": {k: {"fields": v["fields"], "wired": v["wired"]}
                       for k, v in wiring.items()}}


def audit_loop_activity() -> list:
    base = ROOT / ".workbuddy" / "memory" / "automations"
    now = time.time()
    seen = {}
    for d in sorted(base.glob("*")) if base.exists() else []:
        uid = d.name.split("-")[0]
        if uid not in AUTOS:
            continue                          # 只统计已登记任务，避免历史残留目录噪声
        mem = d / "memory.md"
        if mem.exists():
            age = (now - mem.stat().st_mtime) / 86400.0
            seen[uid] = {
                "id": uid, "name": AUTOS[uid],
                "last_run": datetime.fromtimestamp(
                    mem.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                "age_days": round(age, 1),
                "stale": age > 3.0,
                "status": "⚠ 停摆" if age > 3.0 else "✓ 运行中"}
    out = []
    for uid, name in AUTOS.items():
        if uid in seen:
            out.append(seen[uid])
        else:
            is_new = uid in NEW_AUTOS
            out.append({"id": uid, "name": name, "last_run": None,
                        "age_days": None, "stale": not is_new,
                        "status": "待首跑（新增）" if is_new else "⚠ 从未运行"})
    return out


def audit_ledger() -> dict:
    p = ROOT / "reports" / "EVOLUTION_DECISIONS.md"
    if not p.exists():
        return {"exists": False}
    txt = p.read_text(encoding="utf-8", errors="ignore")
    rounds = len(re.findall(r"(?m)^#{2,4}\s*.*?(第\s*\d+\s*轮|R\d{2}\b)", txt))
    rejected = len(re.findall(r"REJECTED|已否决|回滚", txt))
    landed = len(re.findall(r"LANDED|已落盘|已实施", txt))
    falsified_hits = len(re.findall(r"已证伪|勿重试|闭合", txt))
    return {"exists": True, "size_kb": round(p.stat().st_size / 1024, 1),
            "rounds": rounds, "rejected_mentions": rejected,
            "landed_mentions": landed, "falsified_mentions": falsified_hits,
            "lines": txt.count("\n") + 1}


def audit_capability() -> list:
    caps = []

    def has(path: str, pattern: str) -> bool:
        f = ROOT / path
        if not f.exists():
            return False
        return re.search(pattern, PA.source(f)) is not None

    caps.append({"cap": "划分不变量指标（span_sharpe / month_win_rate）",
                 "ok": has("strategy/kpi_stats.py",
                           r"def\s+(span_sharpe|month_win_rate)"),
                 "why": "折均值/正收折数已被证为划分假象（R40）"})
    caps.append({"cap": "多窗口共识（4 套折划分取最差窗）",
                 "ok": has("strategy/_evolve_wf.py", r"consensus"),
                 "why": "单一折划分可被窗口运气欺骗"})
    caps.append({"cap": "P0 不变性基线（逐位相同断言）",
                 "ok": any(ROOT.glob("tests/_baseline*")) or has(
                     "tests/run_all.py", r"baseline"),
                 "why": "研究代码改动后必须证明 P0 未漂移（R39 靠它抓到 off-by-one）"})
    caps.append({"cap": "单仓上限守卫（防 wf 绕过 position sizing）",
                 "ok": has("strategy/_evolve_wf.py", r"max_single_position"),
                 "why": "R39：资本倾斜类历史 IS 数字曾虚增 +9.4pt"})
    caps.append({"cap": "末端 bar 收盘守卫（防未完成 bar 进统计）",
                 "ok": has("strategy/daily_context.py", r"_expected_last_trade_date"),
                 "why": "P2 疑点：回测面板曾并入当日未收盘 bar"})
    caps.append({"cap": "参数读点扫描（防死参数/单侧参数进网格）",
                 "ok": (ROOT / "scripts" / "_rsi_meta_audit.py").exists()
                       or (ROOT / "scripts" / "_recursive_self_improve.py").exists(),
                 "why": "R41 才发现 7 个参数在回测中无读取点"})
    return caps


# 回测字段 → 生产键 的**已确认**别名（来源：base_cfg() 内联注释的亲口对应）。
# ★ 与 `_param_audit.ALIAS` 是**不同口径**，别合并：
#   · 本表回答「能不能比值」—— 纯数值比对，闸门开不开都可比 ⇒ 4 条；
#   · PA.ALIAS 回答「网格有没有分辨率」—— 必须闸门在 P0 里真正生效 ⇒ 1 条。
#   混用会把「数值相同但闸门关闭」的 3 对算成可验证，凭空高估搜索空间。
# 刻意保守：语义或量纲不完全等价的一律不放进来（例如
#   daily_loss_abs_pct 是「比例」而 daily_loss_limit_abs 是「金额 5000 元」；
#   vol_sizing 与 trend_vol_sizing 语义不同 —— 误配会制造假漂移）。
DRIFT_ALIAS = {
    "fixed_amount": "max_order_amount",
    "consec_loss_trigger": "max_consecutive_losses",
    "consec_loss_halt": "max_consecutive_losses_halt",
    "daily_loss_pct": "daily_loss_limit_pct",
}
SKIP_FIELDS = {"cost_pct", "trailing", "chandelier", "entry_mode",
               "exit_next_open", "momentum_weight_pow", "daily_loss_abs_pct",
               "vol_sizing"}


def audit_baseline_drift() -> dict:
    """★核心：验证器的 P0 基线是否仍等于生产配置（动态比对真实值）。

    为什么这是自进化的要害：
      `_evolve_wf.base_cfg()` 是**人手抄写**的生产参数镜像，其 docstring 里
      记录过 13 次「基线漂移」（top_n / risk_per_trade / fixed_amount /
      northbound_mode / momentum_min_mom ...）。每次漂移都会让此后所有
      「dSh 增量」相对错误基线计算 ⇒ 循环的**反馈信号本身在说谎**。
      静态读点扫描只能证明「代码读了该键」，唯有逐字段比值才能证明「读数一致」。
    """
    import dataclasses

    from strategy.backtest_daily import BacktestConfig  # noqa: PLC0415
    from strategy._evolve_wf import base_cfg           # noqa: PLC0415

    cfg = base_cfg()
    rows = []
    prod = dict(STRATEGY_PARAMS)
    prod.update(RISK_PARAMS)
    for f in dataclasses.fields(BacktestConfig):
        name = f.name
        if name in SKIP_FIELDS:
            continue
        key = name if name in prod else DRIFT_ALIAS.get(name)
        if not key or key not in prod:
            rows.append({"field": name, "key": None, "prod": None,
                         "bt": getattr(cfg, name), "status": "未映射"})
            continue
        pv, bv = prod[key], getattr(cfg, name)
        try:
            same = abs(float(pv) - float(bv)) < 1e-9
        except (TypeError, ValueError):
            if isinstance(pv, (list, tuple)) and isinstance(bv, (list, tuple)):
                same = list(pv) == list(bv)      # [] 与 () 语义相同，不算漂移
            else:
                same = pv == bv
        rows.append({"field": name, "key": key, "prod": pv, "bt": bv,
                     "status": "一致" if same else "★漂移"})
    drift = [r for r in rows if r["status"] == "★漂移"]
    unmapped = [r for r in rows if r["status"] == "未映射"]
    return {"rows": rows, "drift": drift, "unmapped": unmapped,
            "checked": len(rows) - len(unmapped),
            "drift_n": len(drift), "unmapped_n": len(unmapped)}


def priority_actions(space: dict, activity: list, caps: list,
                     drift: dict | None = None) -> list:
    acts = []
    if drift and drift["drift_n"]:
        acts.append({
            "pri": "P0", "target": "反馈信号可信度（最高优先）",
            "action": f"修复 {drift['drift_n']} 处 P0 基线漂移",
            "detail": "、".join(
                f"`{r['field']}` 回测 {r['bt']} ≠ 生产 `{r['key']}` {r['prod']}"
                for r in drift["drift"]),
            "why": "验证器基线是手抄镜像，漂移后所有 dSh 相对错误基线计算 ⇒ "
                   "循环在用一个说谎的尺子量自己"})
        acts.append({
            "pri": "P0", "target": "反馈信号可信度",
            "action": "把 base_cfg() 改为从 config/settings.py 自动派生 + 漂移守卫测试",
            "detail": f"当前 {drift['unmapped_n']} 个回测字段无生产键映射",
            "why": "根治手抄漂移：让基线无法不同步（可加显式 IGNORE 白名单并注明理由）"})
    dead = [r for r in space["rows"] if r["class"] == "死参数"]
    onesided_live = [r for r in space["rows"] if r["class"] == "仅实盘"]
    onesided_res = [r for r in space["rows"] if r["class"] == "仅回测"]
    if dead:
        acts.append({
            "pri": "P1", "target": "搜索空间",
            "action": f"删除或接线 {len(dead)} 个死参数",
            "detail": "、".join(r["param"] for r in dead),
            "why": "任何在这些键上跑的网格，结论必然是 0 变化，纯浪费迭代预算"})
    if onesided_res:
        acts.append({
            "pri": "P1", "target": "口径背离",
            "action": f"补齐 {len(onesided_res)} 个「仅回测有读点」参数的实盘读取点",
            "detail": "、".join(r["param"] for r in onesided_res),
            "why": "回测收益里含实盘没有的保护（移动止损等）⇒ 基线被系统性高估"})
    if onesided_live:
        acts.append({
            "pri": "P2", "target": "口径背离",
            "action": f"把 {len(onesided_live)} 个「仅实盘有读点」参数补进回测器",
            "detail": "、".join(r["param"] for r in onesided_live),
            "why": "回测测不到该行为 ⇒ 对该键的 A/B 结论不可信（非「无价值」）"})
    stale = [a for a in activity if a["stale"]]
    if stale:
        acts.append({
            "pri": "P0", "target": "循环活性",
            "action": f"恢复 {len(stale)} 个停摆/未运行任务",
            "detail": "、".join(a["name"] for a in stale),
            "why": "停摆期间没有任何观测与裁决 ⇒ 自进化链条断掉（10-22~10-28 已发生一次）"})
    missing = [c for c in caps if not c["ok"]]
    if missing:
        acts.append({
            "pri": "P1", "target": "循环能力",
            "action": f"补齐 {len(missing)} 项缺失能力",
            "detail": "、".join(c["cap"] for c in missing),
            "why": "缺一项就等于放弃了对应类别的错误防线"})
    return acts


def main() -> None:
    t0 = time.time()
    space = audit_search_space()
    activity = audit_loop_activity()
    ledger = audit_ledger()
    caps = audit_capability()
    try:
        drift = audit_baseline_drift()
    except Exception as e:                     # noqa: BLE001
        drift = {"error": f"{type(e).__name__}: {e}", "drift": [],
                 "unmapped": [], "checked": 0, "drift_n": 0, "unmapped_n": 0,
                 "rows": []}
    acts = priority_actions(space, activity, caps, drift)

    cls_count = {}
    for r in space["rows"]:
        cls_count[r["class"]] = cls_count.get(r["class"], 0) + 1
    total = len(space["rows"])
    usable = cls_count.get("双向可验证", 0)
    eff = (f"{ledger['landed_mentions']}/{ledger['rounds']}"
           if ledger.get("rounds") else "—")

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "search_space": {"total": total, "by_class": cls_count,
                         "usable_ratio": round(usable / total, 3) if total else 0,
                         "rows": space["rows"],
                         "live_files": space["live_files"],
                         "research_files": space["research_files"]},
        "loop_activity": activity, "ledger": ledger,
        "capability": caps, "baseline_drift": {
            "checked": drift.get("checked"), "drift_n": drift.get("drift_n"),
            "unmapped_n": drift.get("unmapped_n"),
            "drift": drift.get("drift"), "unmapped": drift.get("unmapped"),
            "error": drift.get("error")},
        "actions": acts,
        "elapsed_sec": round(time.time() - t0, 2),
    }
    (OUT / "RSI_META_LATEST.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    L = []
    L.append(f"# 递归自进化 · 元审计（{datetime.now():%Y-%m-%d %H:%M}）\n\n")
    L.append("> 二阶优化：不改策略参数，改「改进流程本身」。\n")
    L.append(f"> 扫描文件：实盘侧 {len(space['live_files'])} 个，"
             f"研究侧 {len(space['research_files'])} 个（注释已剥离，只数真实代码读点）\n\n")

    L.append("## 一、搜索空间审计\n\n")
    L.append(f"生产参数 **{total}** 个，其中**双向可验证 {usable} 个"
             f"（{usable/total*100:.0f}%）**。\n\n")
    L.append("| 分类 | 数量 | 含义 |\n|---|---:|---|\n")
    for k, v in sorted(cls_count.items(), key=lambda x: -x[1]):
        L.append(f"| {k} | {v} | {next(r['note'] for r in space['rows'] if r['class']==k)} |\n")

    for cls in ("死参数", "仅回测", "仅实盘"):
        rows = [r for r in space["rows"] if r["class"] == cls]
        if not rows:
            continue
        L.append(f"\n### {cls}（{len(rows)} 个）\n\n")
        # 「底层」列已删（R42）：旧实现把「既不在生产目录也不在研究文件」的第三种
        # 命中记为 other，但去重后分类只由 _param_audit 给出，不存在第三态。
        # 换成更有用的「别名接线」，让「回测为何能读到」一眼可见。
        L.append("| 参数 | 实盘 | 回测 | 别名接线 | 实盘位置 |\n|---|---:|---:|---|---|\n")
        for r in rows:
            locs = ", ".join(f"`{k.split('/')[-1]}`" for k in r["live_files"]) or "—"
            alias = ", ".join(f"`{a}`" for a in r["alias"]) or "—"
            L.append(f"| `{r['param']}` | {r['live']} | {r['research']} | "
                     f"{alias} | {locs} |\n")

    L.append("\n## 二、反馈信号可信度：P0 基线漂移\n\n")
    if drift.get("error"):
        L.append(f"- ⚠ 检测失败：{drift['error']}\n")
    else:
        L.append(f"逐字段比对 `_evolve_wf.base_cfg()`（验证器基线）与 "
                 f"`config/settings.py`（生产配置）：可比 **{drift['checked']}** 项，"
                 f"**漂移 {drift['drift_n']} 项**，未映射 {drift['unmapped_n']} 项。\n\n")
        if drift["drift"]:
            L.append("| 回测字段 | 回测值 | 生产键 | 生产值 | 判定 |\n")
            L.append("|---|---:|---|---:|---|\n")
            for r in drift["drift"]:
                L.append(f"| `{r['field']}` | {r['bt']} | `{r['key']}` | "
                         f"{r['prod']} | ★漂移 |\n")
            L.append("\n> 漂移后果：此后所有「dSh 增量 / KPI」都相对错误基线计算。\n"
                     "> 本项目 docstring 已记录过 13 次同类漂移（top_n / risk_per_trade / "
                     "fixed_amount / northbound_mode / momentum_min_mom …）。\n")
        else:
            L.append("✅ 基线一致，当前无需修口径。\n")
        if drift["unmapped"]:
            L.append(f"\n未映射回测字段（{drift['unmapped_n']}，需人工确认是否应接生产键）："
                     + "、".join(f"`{r['field']}`" for r in drift["unmapped"]) + "\n")

    L.append("\n## 三、循环活性\n\n| 任务 | 最近运行 | 距今天数 | 判定 |\n|---|---|---:|---|\n")
    for a in activity:
        L.append(f"| {a['name']} | {a['last_run'] or '—'} | "
                 f"{a['age_days'] if a['age_days'] is not None else '—'} | {a['status']} |\n")

    L.append("\n## 四、决策账本\n\n")
    if ledger.get("exists"):
        L.append(f"- 体积 {ledger['size_kb']} KB / {ledger['lines']} 行\n"
                 f"- 轮次标记 **{ledger['rounds']}**，落盘提及 **{ledger['landed_mentions']}**，"
                 f"否决/回滚提及 **{ledger['rejected_mentions']}**，"
                 f"已证伪/勿重试提及 **{ledger['falsified_mentions']}**\n"
                 f"- 「落盘 ÷ 轮次」≈ **{eff}** —— ★ **这不是迭代效率**：右式三项都是"
                 f"**词频**（正则 `REJECTED|已否决|回滚` 等），不是结构化事件数。\n"
                 f"  实证：本文件每追加一段带「否决/回滚」字样的说明，计数就 +1~2，"
                 f"而轮次未变。⇒ 真正的命中率/浪费率不可由词频推出（P0-b 待做）。\n")
    else:
        L.append("- 账本不存在\n")

    L.append("\n## 五、循环能力\n\n| 能力 | 状态 | 缺失后果 |\n|---|---|---|\n")
    for c in caps:
        L.append(f"| {c['cap']} | {'✓' if c['ok'] else '✗ 缺失'} | {c['why']} |\n")

    L.append("\n## 六、下一轮该改的环节（按优先级）\n\n")
    for a in acts:
        L.append(f"### [{a['pri']}] {a['target']} — {a['action']}\n\n"
                 f"- 对象：{a['detail']}\n- 理由：{a['why']}\n\n")
    if not acts:
        L.append("流程侧无阻塞项，可回到一阶（策略）优化。\n")

    (OUT / "RSI_META_LATEST.md").write_text("".join(L), encoding="utf-8")

    # ---- console ----
    print(f"[搜索空间] {total} 个参数：" +
          "  ".join(f"{k}{v}" for k, v in cls_count.items()) +
          f"  可用率 {usable/total*100:.0f}%")
    dead = [r["param"] for r in space["rows"] if r["class"] == "死参数"]
    print(f"[死参数] {len(dead)}: {dead}")
    print(f"[仅回测] {[r['param'] for r in space['rows'] if r['class']=='仅回测']}")
    print(f"[仅实盘] {len([r for r in space['rows'] if r['class']=='仅实盘'])} 个: "
          f"{[r['param'] for r in space['rows'] if r['class']=='仅实盘']}")
    print(f"\n[循环活性] 停摆 {sum(1 for a in activity if a['stale'])}/"
          f"{len(activity)}")
    for a in activity:
        if a["stale"] or a["last_run"] is None:
            print(f"   {a['status']:<12} {a['name']:<22} 最近 {a['last_run'] or '—'}")
    # ★ 文案必须带「提及」二字：这三个数都是正则词频，不是事件数。
    #   旧文案打印「落盘 9 否决 189 效率 16%」，读起来像 9/189 个轮次事件，
    #   实际只是文本里这些词出现了多少次（改一段说明就会变）。
    print(f"\n[账本] 轮次标记 {ledger.get('rounds')}；"
          f"「落盘」提及 {ledger.get('landed_mentions')} / "
          f"「否决·回滚」提及 {ledger.get('rejected_mentions')}"
          f"（词频，非事件数；比值 {eff} 不可当迭代效率读）")
    if drift.get("error"):
        print(f"[基线漂移] 检测失败: {drift['error']}")
    else:
        print(f"[基线漂移] 可比 {drift['checked']} 项，漂移 {drift['drift_n']} 项，"
              f"未映射 {drift['unmapped_n']} 项")
        for r in drift["drift"]:
            print(f"   ★ {r['field']:<26} 回测 {str(r['bt']):<10} ≠ "
                  f"生产 {r['key']} = {r['prod']}")
    miss = [c["cap"] for c in caps if not c["ok"]]
    print(f"[能力] 缺失 {len(miss)}: {miss}")
    print(f"\n[结论] 优先级动作 {len(acts)} 条 → {[a['action'] for a in acts]}")
    print(f"[已写] {OUT/'RSI_META_LATEST.md'}（{payload['elapsed_sec']}s）")


if __name__ == "__main__":
    main()

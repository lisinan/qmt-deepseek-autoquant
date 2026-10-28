# -*- coding: utf-8 -*-
"""搜索空间契约守卫（R42 递归自进化第 1 轮，2026-10-28）。

【本测试存在的唯一理由】
R41 曾在 `momentum_min_mom` / `min_signals` / `trend_exit_ma` 等轴上跑了几十格网格，
并据此写下「已证伪」。事后才发现其中一批参数在 `backtest_daily.py` 里
**根本没有读取点**（例如 `atr_period` / `momentum_scope` / `volume_surge` /
`ma_short` / `rsi_period` / `macd_*` / `boll_*` 只在 settings 里存在）
⇒ 那些网格测的是空气，「证伪」结论无回测支撑。

这类错误无法靠人记：参数会增删，读取点会搬家。所以把「每个生产参数被谁读」
变成**可计算事实**（`strategy/_param_audit.py`），并由本测试钉住一张**显式清单**：

  · 双向/别名可验证 → 回测 A/B 对这个键有意义，结论可记「已证伪/已采纳」
  · 仅实盘（CONTRACT_LIVE_ONLY）→ 回测测不到 ⇒ 结论只能记「未验证」
  · 死参数（CONTRACT_DEAD）   → 全仓库无读点 ⇒ 跑网格纯浪费迭代

【断言纪律（第 19 条教训：钉契约不钉副产品）】
  1. 只钉「分类 → 结论可否成立」这条逻辑链，不钉具体收益/Sharpe 数字。
  2. 必须有**正向对照**（第 6 条教训）：若 `identifiers()` 因重构而永远返回空集，
     `contract_violations()` 会「全部变成死参数」，只要把契约表一起改掉就能假绿。
     故 `test_detector_is_not_degenerate` 强制要求双向可验证占比达标 + 关键参数
     逐个点名，防止「检测器坏了、契约跟着坏」的合谋式假绿。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from config.settings import RISK_PARAMS, STRATEGY_PARAMS  # noqa: E402
import strategy._param_audit as PA  # noqa: E402

ALL_CLASSES = {"双向可验证", "别名可验证", "仅实盘", "仅回测", "死参数"}

# 这些参数是历史上真正扫过、真正驱动过决策的旋钮。若它们中任意一个被判成
# 「仅实盘 / 死参数」，说明检测器或目录划分坏了（而不是策略变简单了）。
# ★ 只放**生产参数**（STRATEGY_PARAMS / RISK_PARAMS 内的键）。研究旋钮
#   （如 momentum_weight / trend_vol_sizing 只存在于 BacktestConfig）不在
#   classify() 的作用域内 —— 初版把 momentum_weight 写进来，测试立刻红，
#   正好证明这条断言确实在按「生产参数」口径工作。
MUST_BE_VERIFIABLE = (
    "momentum_top_n",       # 动量池大小（P0=3）
    "momentum_lookback",    # 动量子窗口（P0=60）
    "momentum_min_mom",     # 入池动量下限（P0=0.15，R41 主战场）
    "atr_stop_mult",        # ATR 止损倍数（P0=2.9）
    "max_positions",        # 最大持仓数（P0=5）
    "down_day_exit_pct",    # 单日暴跌退出（P0=-3.25）
    "buy_score_threshold",  # 评分阈值（P0=4.0，R40 全档闭合）
    "min_signals",          # 正因子数下限（P0=3，R41 全档闭合）
    "risk_per_trade",       # 单笔风险预算（P0=0.040）
    "max_single_position_pct",  # 单仓上限（L2=0.40）
    "min_daily_bias",       # bias 通道（2.0 = 关闭哨兵值）
    "northbound_mode",      # 北向闸门（P0="gate"）
)


# --------------------------------------------------------------- 检测器单元测试
def _write(tmp: Path, name: str, body: str) -> Path:
    p = tmp / name
    p.write_text(body, encoding="utf-8")
    return p


def test_detector_covers_four_read_forms():
    """四种参数读取形态必须都能被识别（字符串常量/属性/关键字实参/Name）。"""
    body = (
        "def f(cfg, params):\n"
        "    a = STRATEGY_PARAMS.get('k_const')\n"      # 1 字符串常量
        "    b = params['k_sub']\n"                     # 1 字符串常量（下标）
        "    c = cfg.k_attr\n"                          # 2 属性访问
        "    d = self.p.k_self\n"                       # 2 属性访问
        "    return replace(cfg, k_kwarg=1)\n"          # 3 关键字实参
    )
    with tempfile.TemporaryDirectory() as td:
        p = _write(Path(td), "m.py", body)
        ids = PA.identifiers(p)
    for want in ("k_const", "k_sub", "k_attr", "k_self", "k_kwarg"):
        assert want in ids, f"检测器漏掉读取形态: {want}（ids={sorted(ids)}）"


def test_detector_ignores_comments_and_sentences():
    """注释里的参数名、整句 docstring 都不算读点（否则契约会被文字污染）。"""
    body = (
        '"""这里解释 k_only_in_prose 为什么会这样，整段是一句自然语言。"""\n'
        "# 注释里提到 k_only_in_comment 也不算读取\n"
        "X = 1  # 行尾注释 k_trailing\n"
    )
    with tempfile.TemporaryDirectory() as td:
        p = _write(Path(td), "m.py", body)
        ids = PA.identifiers(p)
    for bad in ("k_only_in_prose", "k_only_in_comment", "k_trailing"):
        assert bad not in ids, (
            f"{bad!r} 只出现在注释/散文里，却被判成读点 ⇒ 契约会被文字污染")


def test_self_module_is_excluded_from_scan():
    """本模块自身列出全部参数名，必须被 SELF_EXCLUDE 排除，否则自污染。

    自污染的后果是**静默**的：3 个死参数会变成「仅实盘」、别名对会变成
    「双向可验证」，契约随之被改写成一个看起来更健康的假象。
    """
    rows = PA.classify()
    me = Path(PA.__file__).resolve().relative_to(PA.ROOT).as_posix()
    polluted = [k for k, v in rows.items()
                if me in v["live_files"] or me in v["research_files"]]
    assert not polluted, f"{me} 被当成读点来源 ⇒ 自污染未生效：{polluted[:5]}"


# ----------------------------------------------------------------- 契约一致性
def test_contract_matches_actual():
    """契约表与实测分类必须逐项一致（双向：错登记 + 漏登记都会红）。"""
    v = PA.contract_violations()
    assert not v, "契约与实测不一致（%d 项）：\n  - %s" % (len(v), "\n  - ".join(v))


def test_contract_tables_are_disjoint_and_explained():
    overlap = set(PA.CONTRACT_LIVE_ONLY) & set(PA.CONTRACT_DEAD)
    assert not overlap, f"同一参数不能既登记仅实盘又登记死参数: {sorted(overlap)}"
    for name, table in (("CONTRACT_LIVE_ONLY", PA.CONTRACT_LIVE_ONLY),
                        ("CONTRACT_DEAD", PA.CONTRACT_DEAD)):
        for k, reason in table.items():
            assert isinstance(reason, str) and len(reason.strip()) >= 4, (
                f"{name}[{k!r}] 缺少有意义的理由 ⇒ 后来者无法判断能否移除")


def test_every_param_is_classified():
    """任何生产参数都必须落入五类之一（含仅回测），不留未归类。"""
    rows = PA.classify()
    keys = set(STRATEGY_PARAMS) | set(RISK_PARAMS)
    assert keys <= set(rows), f"有参数未被 classify 覆盖: {sorted(keys - set(rows))}"
    bad = {k: v["class"] for k, v in rows.items() if v["class"] not in ALL_CLASSES}
    assert not bad, f"出现未知分类: {bad}"


def test_only_backtest_class_is_empty_or_registered():
    """「仅回测」= 实盘无读点 ⇒ 落盘即死代码，必须显式登记才允许存在。

    当前实测为空集（P0 口径）。若将来出现，这里会红，强制在
    `CONTRACT_*` 中登记并说明「回测收益含实盘没有的来源」。
    """
    rows = PA.classify()
    only_bt = sorted(k for k, v in rows.items() if v["class"] == "仅回测")
    assert not only_bt, (
        "出现「仅回测」参数（实盘无读点，落盘即死代码）：%s\n"
        "→ 若确认属实，请在 strategy/_param_audit.py 中登记后再放行" % only_bt)


# ------------------------------------------------------------- 正向/负向对照
def test_detector_is_not_degenerate():
    """正向对照：检测器必须仍有分辨率，关键旋钮必为「双向可验证」。

    这条断言是**反合谋**的：若 `identifiers()` 因目录改名/重构而返回空集，
    所有参数会一起变成「死参数」，此时只要把契约表一起改掉，
    `test_contract_matches_actual` 就会假绿。本测试阻止这种「契约跟着检测器
    一起坏」的情形。
    """
    rows = PA.classify()
    total = len(rows)
    verifiable = sum(1 for v in rows.values()
                     if v["class"] in ("双向可验证", "别名可验证"))
    ratio = verifiable / total if total else 0.0
    assert ratio >= 0.40, (
        f"可验证参数占比仅 {ratio:.1%}（{verifiable}/{total}）—— 检测器很可能已失效，"
        "先查 EXCLUDE_DIRS / RESEARCH_FILES 是否与目录结构脱节")

    bad = [(k, rows[k]["class"] if k in rows else "未收录")
           for k in MUST_BE_VERIFIABLE
           if rows.get(k, {}).get("class") not in ("双向可验证", "别名可验证")]
    assert not bad, (
        "以下历史主战场旋钮不再「可验证」，说明扫描口径坏了：%s" % bad)


def test_dead_params_really_have_zero_readers():
    """负向对照：登记为「死参数」的必须 live=0 / research=0 / 无别名。

    特别包括 `max_position_amount`：它是**遗留键**，实际生效的是
    `max_order_amount`；而 `tests/test_leverage_scale_invariance.py` 仍在
    对死键断言 —— 那条断言守的是空气，正是本契约要暴露的形态。
    """
    rows = PA.classify()
    for k in PA.CONTRACT_DEAD:
        r = rows[k]
        assert r["live"] == 0 and r["research"] == 0 and not r["alias"], (
            f"{k} 登记为死参数，实测 live={r['live']} research={r['research']} "
            f"alias={r['alias']} ⇒ 已接线就应从 CONTRACT_DEAD 移除")


def test_live_only_really_absent_from_research_scan():
    """「仅实盘」的必须 research=0 且无别名接线；否则就是可验证的却标成了不可验证。"""
    rows = PA.classify()
    bad = []
    for k in PA.CONTRACT_LIVE_ONLY:
        r = rows[k]
        if r["research"] or r["alias"]:
            bad.append((k, r["research"], r["alias"]))
    assert not bad, "以下「仅实盘」参数其实回测可读到（应改判双向可验证）：%s" % bad


def test_alias_targets_exist_in_settings():
    """别名两端都必须真实存在；且别名值必须是生产键（不能指向另一个回测字段）。"""
    prod = set(STRATEGY_PARAMS) | set(RISK_PARAMS)
    for bt_field, prod_key in PA.ALIAS.items():
        assert prod_key in prod, f"别名 {bt_field} → {prod_key} 的目标不在生产配置里"
        assert bt_field not in prod, (
            f"别名 {bt_field} 本身就是生产键，不构成别名（会造成双计数）")


# ------------------------------------------------------------------ 防分裂
def test_meta_audit_script_reuses_this_module():
    """元审计脚本不得自带一份副本 —— 两份实现必然漂移，且漂移是静默的。

    `scripts/_recursive_self_improve.py` 曾自带 `identifiers()` / `collect()`
    副本；本模块成为单一事实源后，它必须 import 而非复制。
    """
    script = PA.ROOT / "scripts" / "_recursive_self_improve.py"
    if not script.exists():
        return
    src = script.read_text(encoding="utf-8", errors="ignore")
    assert "strategy._param_audit" in src or "strategy import _param_audit" in src, (
        "元审计脚本未复用 strategy/_param_audit.py ⇒ 两份读点逻辑会静默分叉")
    assert "def identifiers(" not in src, (
        "元审计脚本里仍有自己的 identifiers() 实现，删掉并改为 import")

# -*- coding: utf-8 -*-
"""ETF 小额链路验证的配置守卫【2026-10-29 live 切换当日新增】。

【本测试存在的唯一理由】
OWNER 在切到 live（真实资金）后要求「加一个小金额的 ETF 验证系统功能」。
这件事有**三个可能悄悄出错的点**，每一个都不会报错，只会在某天以
「买多了 / 买不到 / 行为变了」的形式暴露：

  1. **验证标的混进候选池**：若把 ETF 直接放进 ``UNIVERSE``，它会变成动量
     候选，trend_up 成立时被正常路径按「单仓 40% × 现金夹紧」买入
     （现金 8,337 ⇒ ≈7,900 元）——那不是小额验证，是重仓，且改变了已验证策略。
  2. **订阅清单分叉**：行情订阅原本在「启动」与「券商重连」各写一份
     ``list(UNIVERSE.keys()) + dyn``。任何一边漏改，就会出现
     「重启能收到行情、断连重连后收不到」的静默失效（R43 第 24 条同型缺陷）。
  3. **金额从「小额」变「满仓」**：``_handle_buy`` 默认按风险预算 sizing，
     8,337 现金会被夹到 ≈95%。故必须有固定金额入口，且默认值必须为 0
     （= 不生效），否则主信号路径也会被一起改掉。

【断言纪律】
  · 只钉「结构关系」，不钉具体价格/收益（价格会变，金额由 OWNER 调）。
  · 必须有**反向断言**：验证标的必须在 STOCK_CODES **之外**。
  · 必须钉**单一事实源**：订阅只允许走 ``_subscribe_codes()``，不允许再出现
    第二份 ``list(UNIVERSE.keys())`` 裸实现。
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, ".")

from config.settings import (  # noqa: E402
    STOCK_CODES, STRATEGY_PARAMS, UNIVERSE, VALIDATION_UNIVERSE,
)

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine" / "event_engine.py"

# 单笔验证金额的**结构上限**（元）。不是投资建议，只是防止某个手滑把
# manual_entry_amount 写成 400000（= max_order_amount）进而变成重仓验证。
MAX_PROBE_AMOUNT = 20_000.0


def test_validation_codes_are_not_strategy_candidates():
    """验证标的绝不能进 STOCK_CODES（否则会变成动量候选并参与排名/breadth）。"""
    assert VALIDATION_UNIVERSE, "VALIDATION_UNIVERSE 为空 —— 验证机制形同不存在"
    leaked = set(VALIDATION_UNIVERSE) & set(STOCK_CODES)
    assert not leaked, (
        f"验证标的混进了策略候选池: {sorted(leaked)}\n"
        "→ 它们会被 trend_up 路径按单仓 40% 建仓，既不是小额验证，也改变了已验证策略。\n"
        "→ 正确做法：留在 VALIDATION_UNIVERSE（只进订阅 + 观察篮）。")
    # 反向：也不允许出现在 UNIVERSE 里（UNIVERSE 是候选池的上游）
    in_universe = set(VALIDATION_UNIVERSE) & set(UNIVERSE)
    assert not in_universe, f"验证标的出现在 UNIVERSE 里: {sorted(in_universe)}"


def test_manual_basket_only_holds_validation_codes():
    """观察篮里只允许放验证标的（防有人借验证之名塞入普通个股）。"""
    basket = list(STRATEGY_PARAMS.get("manual_entry_codes") or [])
    if not basket:
        return  # 空篮 = 关闭状态，合法
    foreign = [c for c in basket if c not in VALIDATION_UNIVERSE]
    assert not foreign, (
        f"观察篮含非验证标的: {foreign}\n"
        "→ R15 已用四窗共识证伪「观察篮装 LLM 选股」（Sh 1.02 vs 1.71）。\n"
        "→ 若要重开选股观察，须先跑 consensus 量代价，不能顺手塞。")


def test_manual_entry_amount_is_small_and_explicit():
    """固定金额必须是**正数且小额**；0 表示关闭（旧行为），也是合法状态。"""
    amt = float(STRATEGY_PARAMS.get("manual_entry_amount", 0) or 0)
    assert amt >= 0, f"manual_entry_amount 不能为负: {amt}"
    if amt == 0:
        return  # 关闭 = 回到 R15 已验证行为
    assert amt <= MAX_PROBE_AMOUNT, (
        f"manual_entry_amount={amt:.0f} 超过小额验证上限 "
        f"{MAX_PROBE_AMOUNT:.0f} 元 —— 这不是验证，是建仓")
    # 篮子为空却设了金额 = 配置自相矛盾（容易让人误以为在生效）
    if not (STRATEGY_PARAMS.get("manual_entry_codes") or []):
        raise AssertionError(
            "manual_entry_amount 有值但 manual_entry_codes 为空 ⇒ 配置自相矛盾，"
            "机制不会生效（易误判为「已启用」）")


def test_subscription_has_single_source_of_truth():
    """订阅清单只允许一处实现，且必须包含 VALIDATION_UNIVERSE。

    为什么钉死：原来「启动订阅」与「券商重连」各写一份，漏改任一处都会造成
    「重启能收行情、断连后收不到」。这类分叉不会报错，只会静默少几只标的。
    """
    src = ENGINE.read_text(encoding="utf-8")
    # 1) 订阅清单只允许一份**赋值语句**（注释/docstring 里的说明文字不算）
    #    ★ 这里踩过「自污染」：初版按子串计数，把本函数上方 docstring 里
    #      引用的 ``list(UNIVERSE.keys())`` 也计了进去 ⇒ 立刻假红。
    #      与 R43「契约模块列出参数名会污染扫描」同型，故只认赋值行。
    bare = [ln.strip() for ln in src.splitlines()
            if ln.strip().startswith("codes = list(UNIVERSE.keys())")]
    assert len(bare) == 1, (
        "订阅清单出现多份实现（应只在 _subscribe_codes 内）:\n  "
        + "\n  ".join(bare) + "\n→ 请统一走 _subscribe_codes()。")
    assert "VALIDATION_UNIVERSE" in bare[0], (
        f"唯一订阅实现未并入验证标的: {bare[0]!r}")
    # 2) 该唯一实现必须并入 VALIDATION_UNIVERSE
    assert "VALIDATION_UNIVERSE" in src, "event_engine 未引用 VALIDATION_UNIVERSE ⇒ 验证标的收不到行情"
    # 3) 两处 subscribe 调用都必须用 _subscribe_codes()
    n_sub = src.count("qmt_client.subscribe(codes)")
    n_helper = src.count("codes = self._subscribe_codes()")
    assert n_sub == n_helper == 2, (
        f"subscribe 调用 {n_sub} 处 / _subscribe_codes() 使用 {n_helper} 处，"
        "数量不等 ⇒ 有一处仍在自建清单")


def test_handle_buy_fixed_amount_is_inert_by_default():
    """`_handle_buy` 的 fixed_amount 默认值必须为 0（主路径零行为影响）。

    这是本机制「可逆且不改策略」的**唯一保证**：主信号/轮动路径不传此参，
    默认值一旦被改成非 0，全部买入都会被强制成固定金额 —— 属于策略级事故。
    """
    from engine.event_engine import EventEngine  # noqa: PLC0415

    sig = inspect.signature(EventEngine._handle_buy)
    assert "fixed_amount" in sig.parameters, "_handle_buy 缺少 fixed_amount 参数"
    default = sig.parameters["fixed_amount"].default
    assert float(default) == 0.0, (
        f"_handle_buy(fixed_amount={default!r}) 默认值必须为 0 —— "
        "非 0 会把主信号路径的 sizing 一并干掉")


def test_etf_probe_does_not_enter_daily_context():
    """验证标的不进日线上下文（避免污染 momentum/breadth 输入）。"""
    from engine.event_engine import EventEngine  # noqa: PLC0415

    src = inspect.getsource(EventEngine._daily_codes)
    assert "VALIDATION_UNIVERSE" not in src, (
        "_daily_codes 不应包含 VALIDATION_UNIVERSE：\n"
        "日线上下文会喂 momentum 排名 / breadth / regime，\n"
        "加进去 ETF 就可能挤进动量 top-N 并改变已验证策略的选择。")

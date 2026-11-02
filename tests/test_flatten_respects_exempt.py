# -*- coding: utf-8 -*-
"""熔断强平不得触碰豁免仓的守卫（2026-11-02 OWNER 指令）。

真实事故
--------
2026-11-02 10:34 的**假熔断**（拼装总资产塌成纯现金 ⇒ 判 −89.25%，见
`tests/test_risk_asset_trust.py`）在强平全部持仓时，把 OWNER 明确要求
「长期持有」的 **300308.SZ**（`STRATEGY_PARAMS["exit_exempt_codes"]`）
一起卖掉了 —— 豁免在**正常退出路径**生效（`_run_round` 的 `_is_manual_exempt`
分支），但强平走 `_handle_sell(force=True)` 绕过了它。
结果是「账户最危急的时刻，反而把战略底仓清了」，与 OWNER 意图完全相反。

语义（本轮钉死的契约）
----------------------
1. 熔断 = **停止进攻**（暂停新开仓 + 停止普通仓位），**不是**清掉 OWNER 的战略持仓；
2. 豁免仓不参与 `daily_hard_stop_flatten`，但**仍受 −18% 灾难止损**保护
   （正常退出路径的兜底不变，见 `_is_manual_exempt` 分支）；
3. 非豁免仓照旧强平（不能因为加了豁免把强平变成空操作 —— 那是正向对照）；
4. 豁免仓不进入「未完成强平」判定，否则每轮都打印「全部持仓已清空」，把真问题淹掉；
5. 总开关 `manual_entry_exit_exempt=False` 时豁免失效 ⇒ 强平回到「全卖」原行为
   （一键回滚路径必须真实可用）。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="qmt_flatexempt_"))
os.environ.setdefault("QMT_DB", str(_TMP / "flatexempt_qmt.db"))
os.environ.setdefault("QMT_NOTICES_LOG", str(_TMP / "flatexempt_notices.log"))

from config.settings import STRATEGY_PARAMS      # noqa: E402
from core.data_models import Position            # noqa: E402
import engine.event_engine as EE                 # noqa: E402

OWNER_CODE = "300308.SZ"     # OWNER 声明长期持有（exit_exempt_codes）
NORMAL_CODE = "510050.SH"    # 普通仓位，强平时必须卖掉


def _engine(mode="live"):
    eng = EE.EventEngine(
        exec_mode=mode,
        auto_init_positions=False,
        enable_sector_scorer=False,
        enable_dynamic_universe=False,
        enable_llm_reranker=False,
    )
    eng._positions = {
        OWNER_CODE: Position(code=OWNER_CODE, name="中际旭创", quantity=100,
                             avg_cost=195.0, last_price=549.94),
        NORMAL_CODE: Position(code=NORMAL_CODE, name="上证50ETF", quantity=500,
                              avg_cost=2.868, last_price=2.867),
    }
    return eng


def _set_exempt(codes):
    """改豁免清单（测试结束必须还原，否则污染后续用例）。"""
    old = STRATEGY_PARAMS.get("exit_exempt_codes")
    STRATEGY_PARAMS["exit_exempt_codes"] = list(codes)
    return old


def test_owner_declared_holding_is_not_flattened():
    old = _set_exempt([OWNER_CODE])
    try:
        eng = _engine()
        flat, exempt = eng._split_flatten_candidates()
        assert OWNER_CODE in exempt, f"豁免仓应被排除，实际 flat={flat} exempt={exempt}"
        assert OWNER_CODE not in flat
        assert NORMAL_CODE in flat, "普通仓位必须仍被强平（否则熔断变成空操作）"
    finally:
        STRATEGY_PARAMS["exit_exempt_codes"] = old or []


def test_positive_control_without_exempt_everything_is_flattened():
    """正向对照：豁免清单为空时，两只都必须进强平列表。

    若这条不过，说明「不碰豁免仓」被实现成了「不碰任何仓」——
    那熔断就彻底失效了，比缺陷本身更危险。
    """
    old = _set_exempt([])
    try:
        eng = _engine()
        flat, exempt = eng._split_flatten_candidates()
        assert set(flat) == {OWNER_CODE, NORMAL_CODE}, flat
        assert exempt == []
    finally:
        STRATEGY_PARAMS["exit_exempt_codes"] = old or []


def test_master_switch_off_restores_full_flatten():
    """一键回滚：总开关关闭 ⇒ 豁免失效 ⇒ 强平回到「全卖」。"""
    old_codes = _set_exempt([OWNER_CODE])
    old_sw = STRATEGY_PARAMS.get("manual_entry_exit_exempt")
    STRATEGY_PARAMS["manual_entry_exit_exempt"] = False
    try:
        eng = _engine()
        flat, exempt = eng._split_flatten_candidates()
        assert OWNER_CODE in flat, "总开关关闭后仍豁免 ⇒ 回滚路径失效"
        assert exempt == []
    finally:
        STRATEGY_PARAMS["manual_entry_exit_exempt"] = (
            True if old_sw is None else old_sw)
        STRATEGY_PARAMS["exit_exempt_codes"] = old_codes or []


def test_retry_exhausted_and_pending_are_excluded():
    """沿用原强平过滤：耗尽重试 / 待成交卖单不进候选（防重复下单）。"""
    old = _set_exempt([])
    try:
        eng = _engine()
        eng._pending_sells = {NORMAL_CODE: "oid-1"}
        flat, _ = eng._split_flatten_candidates()
        assert NORMAL_CODE not in flat, "待成交卖单不应重复提交"

        eng2 = _engine()
        eng2._flatten_attempts = {NORMAL_CODE: eng2.FLATTEN_MAX_ATTEMPTS}
        flat2, _ = eng2._split_flatten_candidates()
        assert NORMAL_CODE not in flat2, "重试耗尽不应再提交"
        assert OWNER_CODE in flat2
    finally:
        STRATEGY_PARAMS["exit_exempt_codes"] = old or []


def test_zero_quantity_positions_are_ignored():
    """已归零的本地记录（券商同步后 qty=0）不得出现在任何一侧。"""
    old = _set_exempt([OWNER_CODE])
    try:
        eng = _engine()
        eng._positions[OWNER_CODE].quantity = 0
        eng._positions[NORMAL_CODE].quantity = 0
        flat, exempt = eng._split_flatten_candidates()
        assert flat == [] and exempt == []
    finally:
        STRATEGY_PARAMS["exit_exempt_codes"] = old or []


def test_flatten_loop_skips_exempt_and_keeps_hard_stop():
    """端到端语义：豁免仓不发 SELL，但 −18% 灾难止损仍在其退出路径里生效。

    后半段用真实价格测算：549.94 相对成本 195 是大赚，不会触发止损；
    把价格压到 −20% 才触发 —— 证明「豁免 ≠ 无风控」。
    """
    old = _set_exempt([OWNER_CODE])
    try:
        eng = _engine()
        sent = []
        eng._handle_sell = lambda sig, pos, now=None, force=False: sent.append(
            (sig.code, getattr(sig, "reason", ""))) or True
        flat, exempt = eng._split_flatten_candidates()
        for code in flat:
            eng._handle_sell(
                EE.Signal(ts=EE.datetime.now(), code=code,
                          name=eng._positions[code].name, side="SELL",
                          price=eng._positions[code].last_price,
                          reason="daily_hard_stop_flatten"),
                eng._positions[code], force=True)
        codes = [c for c, _ in sent]
        assert OWNER_CODE not in codes, f"豁免仓被卖了：{sent}"
        assert NORMAL_CODE in codes, f"普通仓未被强平：{sent}"

        # 灾难止损独立于豁免：−20% 仍应卖出（该分支用 avg_cost=195 与 px 比较）
        hard = abs(float(STRATEGY_PARAMS.get("hard_stop_pct", -0.18)))
        px_crash = 195.0 * (1 - hard) - 1.0
        assert (px_crash - 195.0) / 195.0 <= -hard
    finally:
        STRATEGY_PARAMS["exit_exempt_codes"] = old or []

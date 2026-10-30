# -*- coding: utf-8 -*-
"""声明式退出豁免（exit_exempt_codes）守卫，2026-10-30 OWNER 指令。

背景（真实场景，非假设）
------------------------
2026-10-30 上午，策略因「趋势破位」卖出 300308.SZ（中际旭创）100 股 @569.24；
OWNER 随后要求买回并长期持有（当日 11:24 以 568.29 成交）。

问题：`_is_manual_exempt` 原本只认 `self._manual_positions`，而该集合**仅在
观察篮真的买过时**才登记（`_manual_entry_step` 里 `self._manual_positions.add(code)`）。
手工买回的持仓永远不会进这个集合 ⇒ 豁免拿不到，下一交易日策略会再次卖出。

为什么不能简单地把代码塞进 `manual_entry_codes`：那会让它变成**观察篮买入清单**，
一旦该票被卖出、变成不持有，观察篮会在下一轮自动补买（按 manual_entry_amount
或风险预算），行为不可控。

因此新增独立清单 `STRATEGY_PARAMS["exit_exempt_codes"]`：只表达「不要自动卖」，
不产生任何买入动作。

本文件钉住四条：
  1. 已声明代码即使不是观察篮买的，也必须豁免（事故场景）；
  2. ★ 正向对照：未声明的代码必须不豁免（防止整条判定退化成恒 True）；
  3. 观察篮买入的老路径仍然豁免（向后兼容）；
  4. 总开关 manual_entry_exit_exempt=False 时，两种来源一起失效（一键回滚）。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="qmt_exempt_"))
os.environ.setdefault("QMT_DB", str(_TMP / "exempt_qmt.db"))
os.environ.setdefault("QMT_NOTICES_LOG", str(_TMP / "exempt_notices.log"))

import engine.event_engine as EE          # noqa: E402

OWNER_CODE = "300308.SZ"     # 中际旭创：OWNER 长期持有
OTHER_CODE = "510050.SH"     # 探针 ETF：无豁免


def _engine(manual_positions=(), declared=(OWNER_CODE,), master=True):
    """桩：只构造 _is_manual_exempt 需要的两个状态 + 覆盖 STRATEGY_PARAMS。"""
    eng = EE.EventEngine.__new__(EE.EventEngine)
    eng._manual_positions = set(manual_positions)
    params = dict(EE.STRATEGY_PARAMS)
    params["exit_exempt_codes"] = list(declared)
    params["manual_entry_exit_exempt"] = master
    return eng, params


def test_declared_code_is_exempt_even_if_not_basket_bought():
    """事故场景：OWNER 手工买回的 300308 不在 _manual_positions，仍须豁免。"""
    eng, params = _engine(manual_positions=())          # 关键：篮子从没买过它
    with patch.object(EE, "STRATEGY_PARAMS", params):
        assert eng._is_manual_exempt(OWNER_CODE) is True, (
            "声明式豁免失效 ⇒ 下一交易日策略会再次卖出 OWNER 要求长期持有的仓位")


def test_undeclared_code_is_not_exempt():
    """★ 正向对照：没声明的代码必须**不**豁免。

    若这条也过，说明 `_is_manual_exempt` 退化成了恒 True —— 那所有持仓都不再
    受趋势退出管理，策略等于被悄悄关掉。这一条是整组测试的存活证明。
    """
    eng, params = _engine(manual_positions=())
    with patch.object(EE, "STRATEGY_PARAMS", params):
        assert eng._is_manual_exempt(OTHER_CODE) is False, (
            "未声明代码也被豁免 ⇒ 判定退化成恒 True，退出逻辑实际上被关掉了")


def test_basket_bought_position_still_exempt():
    """向后兼容：观察篮买入的老路径（code in _manual_positions）仍然豁免。"""
    eng, params = _engine(manual_positions=(OTHER_CODE,), declared=())
    with patch.object(EE, "STRATEGY_PARAMS", params):
        assert eng._is_manual_exempt(OTHER_CODE) is True


def test_master_switch_disables_all_exemptions():
    """总开关关闭 ⇒ 两种来源一起失效（一键回滚到已验证的原行为）。"""
    eng, params = _engine(manual_positions=(OTHER_CODE,),
                          declared=(OWNER_CODE,), master=False)
    with patch.object(EE, "STRATEGY_PARAMS", params):
        assert eng._is_manual_exempt(OWNER_CODE) is False, "总开关关闭后声明式豁免仍生效"
        assert eng._is_manual_exempt(OTHER_CODE) is False, "总开关关闭后观察篮豁免仍生效"


def test_empty_declared_list_keeps_strategy_in_full_control():
    """清单置空 ⇒ 不产生任何豁免（默认安全态）。"""
    eng, params = _engine(manual_positions=(), declared=())
    with patch.object(EE, "STRATEGY_PARAMS", params):
        assert eng._is_manual_exempt(OWNER_CODE) is False

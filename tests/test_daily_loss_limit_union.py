# -*- coding: utf-8 -*-
"""日内亏损熔断「abs / pct 取较宽松者」守卫（2026-10-08 修复）。

背景（实盘铁证，勿删）：
  ``daily_loss_limit_abs=¥5000`` 与 ``daily_loss_limit_pct=-3%`` 是**同一意图**的两道
  阈值，但 abs 判定写在前、pct 是 elif ⇒ **abs 永远先触发，pct 自上线起就是死代码**。
  而 ¥5000 在 98 万账户上只有 **-0.51%**，比单笔计划风险
  （risk_per_trade=0.012 ⇒ ≈¥11,763）**还小一半** ⇒ 策略允许单笔亏 1.2 万，却规定
  「当日累计亏 5000 就全天冻结」，逻辑自相矛盾。
  实盘触发：09-24 / 09-28 / 10-08，约每 2~3 个交易日冻结全天，冻结后 95% 现金空转。

修复：生效阈值 = max(abs, pct × 当前权益) —— 大账户由 pct 接管（随规模缩放），
小账户由 abs 兜底（绝对额下限保护）。开关关闭时严格恢复旧行为。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import RISK_PARAMS  # noqa: E402
import risk.manager as rm  # noqa: E402
from risk.manager import RiskManager  # noqa: E402
from datetime import datetime  # noqa: E402
from core.data_models import Order  # noqa: E402


def _order(qty=100, price=10.0):
    return Order(ts=datetime.now(), code="TEST.SZ", side="BUY",
                 quantity=qty, price=price, order_type="limit")


def _mgr(**over):
    p = dict(RISK_PARAMS)
    p.update(over)
    return RiskManager(p)


def _try_open(m, total_asset, daily_pnl):
    """构造一次小额买入并判定是否放行。"""
    m._daily_pnl = daily_pnl
    return m.can_open(_order(), {}, total_asset, 0)


def test_union_takes_looser_of_two_thresholds():
    """大账户：pct 阈值远大于 abs ⇒ 生效阈值应为 pct×权益（随规模缩放）。"""
    m = _mgr(daily_loss_limit_abs=5000, daily_loss_limit_pct=-0.03)
    assert m._daily_loss_limit(980_231.6) == 0.03 * 980_231.6


def test_union_falls_back_to_abs_for_small_account():
    """小账户：pct×权益 < abs ⇒ abs 兜底生效（保留绝对额下限保护）。"""
    m = _mgr(daily_loss_limit_abs=5000, daily_loss_limit_pct=-0.03)
    # 10 万账户：3% = 3000 < 5000
    assert m._daily_loss_limit(100_000.0) == 5000.0


def test_switch_off_restores_legacy_behavior():
    """可逆性：开关关闭时严格返回旧的固定 abs，行为逐位不变。"""
    old = rm.DAILY_LOSS_UNION_MAX
    rm.DAILY_LOSS_UNION_MAX = False
    try:
        m = _mgr(daily_loss_limit_abs=5000, daily_loss_limit_pct=-0.03)
        assert m._daily_loss_limit(980_231.6) == 5000.0
        assert m._loss_limit_by_pct(980_231.6) is False
    finally:
        rm.DAILY_LOSS_UNION_MAX = old


def test_production_default_switch_is_on():
    assert rm.DAILY_LOSS_UNION_MAX is True


def test_real_incident_no_longer_halts():
    """★ 10-08 实盘事故：亏 ¥5,527（-0.56%）不该再冻结 98 万账户。"""
    m = _mgr(daily_loss_limit_abs=5000, daily_loss_limit_pct=-0.03)
    ok, why = _try_open(m, 980_231.6, -5527.0)
    assert ok is True, f"-5527 元(-0.56%) 不应触发 -3% 熔断，实际: {why}"


def test_still_halts_on_real_daily_crash():
    """保护未被削弱：当日亏到 -3%（≈¥29,407）仍然熔断。"""
    m = _mgr(daily_loss_limit_abs=5000, daily_loss_limit_pct=-0.03)
    ok, _why = _try_open(m, 980_231.6, -30_000.0)
    assert ok is False, "-3% 级别的日内亏损必须熔断"


def test_halt_reason_names_the_effective_threshold():
    """熔断原因要说明实际生效的是 pct 还是 abs，便于事后归因。"""
    m = _mgr(daily_loss_limit_abs=5000, daily_loss_limit_pct=-0.03)
    _try_open(m, 980_231.6, -30_000.0)
    assert "daily_loss_pct" in (m._halt_reason or ""), m._halt_reason


def test_threshold_scales_with_account_size():
    """★ 修复的核心：阈值必须随账户规模缩放（旧 abs 是固定额，账户越大越严）。"""
    m = _mgr(daily_loss_limit_abs=5000, daily_loss_limit_pct=-0.03)
    small = m._daily_loss_limit(200_000.0)
    big = m._daily_loss_limit(2_000_000.0)
    assert big > small * 5, f"阈值应随规模线性放大: {small} -> {big}"

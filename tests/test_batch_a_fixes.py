# -*- coding: utf-8 -*-
"""批次 A 的三条 P0 修复回归测试。

覆盖：
  P0-1 日线闸门覆盖动态候选池
       ① DailyContext.refresh(codes=...) 会把动态代码并入 _codes（不再只认静态池）
       ② TrendStrategy：上下文「已就绪但查不到该标的」→ 拒绝入场（原为无条件放行，
          导致实盘所有动态池信号带 [no-daily] 标记、只靠 1 分钟噪音下单）
       ③ 上下文「未就绪(warmup)」→ 仍放行，不冻结引擎
       ④ _apply_momentum_gate：静态+动态统一横截面排名（原动态池无条件放行）
  P0-3 买入力现金夹紧：下单数量受可用现金约束，paper 账本不可能出现负现金
  P0-2 PositionSizer.size(stop_pct=...) 显式止损口径（供 trend_vol_sizing A/B）
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.data_models import Bar, Signal          # noqa: E402
from risk.position_sizer import PositionSizer     # noqa: E402
from strategy.daily_context import DailyContext   # noqa: E402
from strategy.trend_strategy import TrendStrategy  # noqa: E402
import engine.event_engine as EE                  # noqa: E402


# ---------------------------------------------------------------- helpers

def _gen_bars(n: int, base: float = 10.0, slope: float = 0.1,
              noise: float = 0.01, seed: int = 7) -> list:
    """强上行合成 K 线（足以触发 BUY，用于隔离出闸门的作用）。"""
    import random
    rng = random.Random(seed)
    bars = []
    price = base
    start = datetime.now() - timedelta(minutes=n)
    for i in range(n):
        o = price
        c = o + slope + rng.gauss(0, noise)
        h = max(o, c) + abs(rng.gauss(0, noise * 0.5))
        lo = min(o, c) - abs(rng.gauss(0, noise * 0.5))
        bars.append(Bar(ts=start + timedelta(minutes=i), open=o, high=h,
                        low=lo, close=c, volume=rng.randint(100000, 500000)))
        price = c
    return bars


class _FakeDaily:
    """可控的 DailyContext 替身：显式指定就绪状态与已覆盖标的。"""

    def __init__(self, ready: bool, feats: dict = None, atr: float = 0.03):
        self._ready = ready
        self._f = feats or {}
        self._atr = atr

    def is_ready(self):
        return self._ready

    def features(self, code):
        return self._f.get(code)

    def atr_pct(self, code):
        # 无条件返回设定值：让测试能独立控制 sizing 用的波动率，
        # 不受 features() 是否返回 None 影响。
        return self._atr

    def trend_broken(self, code, exit_ma=60):
        return False


class _FakeDynUniverse:
    def __init__(self, codes):
        self._codes = list(codes)

    @property
    def active_codes(self):
        return list(self._codes)


# ---------------------------------------------- P0-1 ① refresh 合并动态代码

def test_daily_context_refresh_merges_dynamic_codes():
    """refresh(codes=...) 必须把新代码并入 _codes，否则后续无参 refresh 又会丢。"""
    dc = DailyContext(codes=["300308.SZ"], index_code="399006.SZ")
    # _fetch_daily 打桩：不触网
    dc._fetch_daily = lambda code, count=120: None
    dc.refresh(codes=["300308.SZ", "601138.SH", "688347.SH"])
    assert "601138.SH" in dc._codes, "动态代码应被并入 DailyContext._codes"
    assert "688347.SH" in dc._codes
    # 再次无参 refresh 仍应覆盖动态代码
    seen = []
    dc._fetch_daily = lambda code, count=120: seen.append(code) or None
    dc.refresh()
    assert "601138.SH" in seen, "无参 refresh 应仍覆盖此前并入的动态代码"


# ------------------------------------------- P0-1 ②③ no-daily 不再是通行证

def test_daily_gate_blocks_when_ready_but_code_uncovered():
    """上下文已就绪却查不到该标的 → 必须拒绝入场（原实现放行）。"""
    s = TrendStrategy(daily=_FakeDaily(ready=True, feats={"300308.SZ": None}))
    sig = s.on_bars("601138.SH", "工业富联", _gen_bars(120))
    assert sig.side == "HOLD", f"应被日线闸门拦下，实际 {sig.side}"
    assert "no-daily-data" in sig.reason, sig.reason


def test_daily_gate_permissive_during_warmup():
    """上下文未就绪（启动 warmup）→ 放行，避免冻结引擎。"""
    s = TrendStrategy(daily=_FakeDaily(ready=False))
    sig = s.on_bars("601138.SH", "工业富联", _gen_bars(120))
    assert sig.side == "BUY", f"warmup 阶段应放行，实际 {sig.side}/{sig.reason}"
    assert "daily-warmup" in sig.reason, sig.reason


def test_require_daily_data_switch_is_reversible():
    """require_daily_data=False 应回到旧的宽松行为（可逆开关）。"""
    s = TrendStrategy(params={"require_daily_data": False},
                      daily=_FakeDaily(ready=True, feats={"300308.SZ": None}))
    sig = s.on_bars("601138.SH", "工业富联", _gen_bars(120))
    assert sig.side == "BUY", "关掉开关后应放行（保证可回滚）"


# ----------------------------------- P0-1 ④ 动量闸门统一排名（含动态池）

def _make_engine(**kw):
    eng = EE.EventEngine(
        exec_mode="paper", auto_init_positions=False,
        enable_sector_scorer=False, enable_dynamic_universe=False,
        enable_llm_reranker=False, **kw)
    eng.analyst = type("FakeAnalyst", (), {"enabled": False})()
    return eng


def test_daily_codes_includes_dynamic_active_pool():
    eng = _make_engine()
    eng.dynamic_universe = _FakeDynUniverse(["601138.SH", "688347.SH"])
    codes = eng._daily_codes()
    assert "601138.SH" in codes and "688347.SH" in codes, \
        "_daily_codes 必须包含动态活跃池（否则日线闸门对其失效）"
    assert "300308.SZ" in codes, "静态池不应丢失"
    assert len(codes) == len(set(codes)), "不应有重复代码"


def test_momentum_gate_ranks_dynamic_codes_too():
    """scope=all 时动态代码必须参与排名，不再无条件放行。"""
    eng = _make_engine()

    class _D:
        def top_momentum(self, codes, top_n, lookback):
            # 只认这两只有正动量
            return [c for c in ("300308.SZ", "601138.SH") if c in codes][:top_n]

    eng.daily = _D()
    cands = {"300308.SZ", "601138.SH", "688347.SH"}
    kept = eng._apply_momentum_gate(cands)
    assert kept == {"300308.SZ", "601138.SH"}, \
        f"动量排名外的动态代码应被剔除，实际 {kept}"


# ------------------------------------------------- P0-3 买入力现金夹紧

def test_buying_power_clamp_never_overdraws_cash():
    eng = _make_engine()
    eng._cash = 50_000.0
    eng._positions = {}
    # 单价 100 → 现金理论上最多买 500 股，扣 5% 预留后更少
    qty = eng._clamp_to_buying_power("300308.SZ", 3000, 100.0)
    assert qty <= 500, f"夹紧后不应超过现金可负担股数，实际 {qty}"
    assert qty % 100 == 0, "必须整百股"
    assert qty * 100.0 <= eng._cash, "成交金额不得超过可用现金"


def test_buying_power_clamp_rejects_when_broke():
    eng = _make_engine()
    eng._cash = 500.0
    eng._positions = {}
    assert eng._clamp_to_buying_power("300308.SZ", 1000, 100.0) == 0, \
        "现金不足 1 手时必须返回 0（而非透支）"


def test_paper_buy_cannot_produce_negative_cash():
    """端到端：paper 模式连续买入不可能把现金打成负数。

    场景选型依据（必须精确，否则测不出 bug）：
      仓位量 = min(equity×risk_per_trade/stop_pct, equity×max_single_pct, max_order_amount)
      当 ATR 大到使 stop_pct = atr×2.5 = 0.10 时，equity×0.02/0.10 = equity×0.2，
      正好是 1/max_positions，5 笔精确用完 100% 现金——**恰好不透支**，测不出问题。
      所以这里取 atr_pct=0.01，使 stop_pct 被 4% 下限接管 → 目标仓位顶到 30 万
      上限，**第 4 笔**就会把 100 万现金打到 -20 万（修复前实测）。
      负现金会污染 _total_asset() → RiskManager 回撤计算 → 断路器误触发。
    """
    eng = _make_engine()
    eng._cash = 1_000_000.0
    eng._positions = {}
    eng.daily = _FakeDaily(ready=False, feats={}, atr=0.01)

    class _T:
        price = 100.0
        name = "X"

    codes = ("300308.SZ", "300502.SZ", "300394.SZ", "688256.SH", "002371.SZ")
    for i, code in enumerate(codes, start=1):
        sig = Signal(ts=datetime.now(), code=code, name="X", side="BUY",
                     score=9.0, price=100.0, reason="test")
        eng._handle_buy(sig, _T(), {code: 100.0})
        assert eng._cash >= 0.0, \
            f"第 {i} 笔买入后现金为负 ({eng._cash:.2f})，现金夹紧失效"
    # 额外确认：持仓市值 + 现金 ≈ 初始资金（账本守恒，无凭空仓位）
    mv = sum(p.quantity * 100.0 for p in eng._positions.values())
    assert abs((eng._cash + mv) - 1_000_000.0) < 1.0, \
        f"账本不守恒: cash={eng._cash:.2f} mv={mv:.2f}"


# -------------------------------------------- P0-2 sizer 显式止损口径

def test_sizer_stop_pct_overrides_derived_stop():
    sz = PositionSizer({"risk_per_trade": 0.02, "atr_stop_mult": 2.5,
                        "stop_loss": -0.04})
    equity = 1_000_000.0
    tight = sz.size(100.0, equity, atr_pct=0.027)            # 推导口径 ≈6.75%
    wide = sz.size(100.0, equity, atr_pct=0.027, stop_pct=0.18)  # 真实趋势止损
    assert wide < tight, (
        f"真实(宽)止损应算出更小仓位: wide={wide} tight={tight}")
    # 名实相符校验：仓位 × 止损 ≈ risk_per_trade × equity
    risked = wide * 100.0 * 0.18
    assert abs(risked - equity * 0.02) / (equity * 0.02) < 0.05, \
        f"stop_pct 口径下单笔风险应≈2%，实际 {risked:.0f}"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

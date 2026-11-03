# -*- coding: utf-8 -*-
"""豁免仓 −18% 灾难止损必须传 ``force=True`` 的守卫（2026-11-03）。

事故链
------
``exit_exempt_codes`` 里的仓位（OWNER 声明长期持有，如 300308.SZ）**没有任何
其它出口**：

  - 策略三类退出（趋势破位 / 超时 / 单日暴跌）在 ``_run_once`` 的
    ``if self._is_manual_exempt(code): ... continue`` 处被短路；
  - 日线兜底 ``_daily_fallback_exit`` 在 ``:2214`` 同样被豁免；
  - 熔断强平 ``_split_flatten_candidates`` **结构性剔除**豁免品种。

于是 ``_run_once`` 里那一行 ``_handle_sell(观察篮硬止损)`` 是唯一出口。
而 ``_handle_sell`` 的五道闸门中，只有**建仓保护期**带 ``not force``：

    if (not force and self.entry_protect_minutes > 0
            and is_in_entry_protection(pos.open_date, ...)):
        return False

``is_in_entry_protection`` 只有上界 ``return now < protect_until``、**无下界**，
保护窗自"买入时刻"连续延伸到**次一交易日 10:00**（ENTRY_PROTECT_MINUTES=30）。
实盘样本：2026-11-03 10:59:01 买入 300308，窗口 = [10:59:01, 11-04 10:00)。
即"买入后立刻"与"次日开盘后半小时"两段都在窗内。

原实现此处没传 ``force`` ⇒ 灾难止损在窗内被**静默拦下**（每天只提示一次），
且因豁免仓无其它出口 ⇒ 该时间段内 −18% 保护**完全不存在**。

本轮钉死的契约
--------------
1. 豁免仓硬止损调用 ``_handle_sell`` 必须 ``force=True``（与 ``:2165`` 每日强平一致）；
2. **正向对照 A**：同一夹具下不传 ``force`` 必须被拦下（否则说明保护期闸门在
   夹具里根本没生效，测试变「合谋式假绿」）；
3. **正向对照 B**：跌幅未达 ``hard_stop_pct`` 时不得卖出（防"无条件卖"式假绿）；
4. ``force=True`` **不得**绕过 T+1 —— A 股规则是硬约束，不是防误伤优先级问题。
   这条防的是"以后有人把 force 做成万能钥匙"。
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="qmt_exemptforce_"))
os.environ.setdefault("QMT_DB", str(_TMP / "exemptforce_qmt.db"))
os.environ.setdefault("QMT_NOTICES_LOG", str(_TMP / "exemptforce_notices.log"))

from config.settings import STRATEGY_PARAMS      # noqa: E402
from core.data_models import Position, Signal    # noqa: E402
import engine.event_engine as EE                 # noqa: E402

CODE = "300308.SZ"                     # exit_exempt_codes 里的观察篮品种
COST = 500.0                           # 成本价
HARD = abs(float(STRATEGY_PARAMS.get("hard_stop_pct", -0.18)))
OPEN_DATE = datetime(2026, 10, 29, 10, 59)   # 非今日 ⇒ T+1 放行


# ---------------------------------------------------------------- 夹具

def _fake_tick(price: float) -> dict:
    return {
        "lastPrice": price, "lastClose": COST, "open": price,
        "high": price, "low": price, "volume": 100, "amount": price * 100,
        "timetag": "20261103103000",
    }


class _Harness:
    """真实引擎 + 只替换"外部世界"（行情、入场段、下单），保留真实退出循环。"""

    def __init__(self, price: float = COST * (1 - HARD) - 5.0):
        self.price = price
        self._saved = {}
        self.calls = []

    # -- 外部世界 --
    def _patch(self):
        self._saved["qmt_client"] = EE.qmt_client
        self._saved["prot"] = EE.is_in_entry_protection
        self._saved["run_single"] = EE.EventEngine._run_single_step
        EE.qmt_client = SimpleNamespace(
            mode="test",
            get_ticks=lambda codes: {CODE: _fake_tick(self.price)},
        )
        # 保护期闸门：夹具里恒为"在窗内"。窗内数学本身已有 test_t1_sell_guard
        # 覆盖，本文件只验证"这条路上的 force 是否传了"。
        EE.is_in_entry_protection = lambda *a, **k: True
        EE.EventEngine._run_single_step = lambda self, ticks: None

    def _restore(self):
        EE.qmt_client = self._saved["qmt_client"]
        EE.is_in_entry_protection = self._saved["prot"]
        EE.EventEngine._run_single_step = self._saved["run_single"]

    def _set_exempt(self, codes):
        old = STRATEGY_PARAMS.get("exit_exempt_codes")
        STRATEGY_PARAMS["exit_exempt_codes"] = list(codes)
        return old

    def __enter__(self):
        self._old_codes = self._set_exempt([CODE])
        self._patch()
        self.eng = EE.EventEngine(
            exec_mode="paper",
            auto_init_positions=False,
            enable_sector_scorer=False,
            enable_dynamic_universe=False,
            enable_llm_reranker=False,
        )
        self.eng._last_daily_date = date.today()   # 不触发日线刷新线程
        self.eng._positions = {
            CODE: Position(code=CODE, name="中际旭创", quantity=100,
                           avg_cost=COST, last_price=self.price,
                           open_date=OPEN_DATE),
        }
        return self

    def __exit__(self, *exc):
        self._restore()
        STRATEGY_PARAMS["exit_exempt_codes"] = self._old_codes or []
        return False

    # -- 驱动 --
    def run_exit_loop(self):
        """跑一轮真实 ``_run_once``（入场段已置空），记录 _handle_sell 调用。"""
        def _rec(sig, pos, **kw):
            self.calls.append((sig, pos, kw))
            return True
        self.eng._handle_sell = _rec
        self.eng._run_once([CODE])
        return self.calls


# ---------------------------------------------------------------- 主用例

def test_exempt_hard_stop_reaches_handle_sell_with_force():
    """豁免仓跌破 −18% ⇒ 唯一出口必须以 force=True 调 _handle_sell。"""
    with _Harness() as h:
        calls = h.run_exit_loop()

    assert len(calls) == 1, f"跌破硬止损应恰好触发一次退出，实际 {len(calls)} 次"
    sig, pos, kw = calls[0]
    assert sig.side == "SELL"
    assert "硬止损" in (sig.reason or ""), sig.reason
    assert kw.get("force") is True, (
        "豁免仓硬止损未传 force=True ⇒ 建仓保护窗内会被静默拦下，"
        "而豁免仓没有其它出口，等于 −18% 保护在该时段失效")


def test_positive_control_gate_blocks_without_force():
    """正向对照 A：同一夹具、真实 _handle_sell、不传 force ⇒ 必须被保护期拦下。

    若这条变绿失败（即没被拦），说明保护期闸门在夹具里没生效 ——
    那么上一条的 force=True 断言就失去了证明力（合谋式假绿）。
    """
    with _Harness() as h:
        pos = h.eng._positions[CODE]
        sig = Signal(ts=datetime.now(), code=CODE, side="SELL",
                     price=h.price, reason="正向对照")
        ok = EE.EventEngine._handle_sell(h.eng, sig, pos)

    assert ok is False, "未传 force 时保护期必须拦下（否则对照失效）"
    assert pos.quantity == 100, "被拦下时持仓不得变动"


def test_positive_control_above_threshold_does_not_sell():
    """正向对照 B：跌幅未达 hard_stop_pct ⇒ 一次都不许调用 _handle_sell。"""
    with _Harness(price=COST * (1 - HARD / 2)) as h:   # −9%，远未到 −18%
        calls = h.run_exit_loop()
    assert calls == [], f"未破硬止损却触发退出：{[(c[0].reason) for c in calls]}"


def test_force_does_not_bypass_t1():
    """force=True 只放开建仓保护期，**不得**放开 T+1（A 股规则硬约束）。"""
    with _Harness() as h:
        pos = h.eng._positions[CODE]
        pos.open_date = datetime.now()          # 当日买入 ⇒ T+1 锁定
        sig = Signal(ts=datetime.now(), code=CODE, side="SELL",
                     price=h.price, reason="硬止损")
        ok = EE.EventEngine._handle_sell(h.eng, sig, pos, force=True)
        qty = pos.quantity

    assert ok is False, "force=True 不得绕过 T+1"
    assert qty == 100, "T+1 拦下时持仓不得变动"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

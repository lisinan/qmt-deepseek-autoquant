# -*- coding: utf-8 -*-
"""
回归守卫：日线取数「本地陈旧 → 改走 tushare」【2026-09-24 PM-EVOLVE 第 7 轮 P0 修复】

为什么需要（两处叠加缺陷，共同造成「日线滞后 4 周」）：

  缺陷①  ``_fetch_daily`` 原逻辑「本地 xtdata 取到 >= 60 根就无条件直接返回」，
         从不检查这批数据的最后一根是哪天。本地 miniQMT 的 1d 数据只在客户端
         主动同步时更新 ⇒ 一旦停止同步就**永久冻结在最后一次同步日**，
         而 tushare 兜底因为「本地已够 60 根」永远走不到。
  缺陷②  tushare 兜底的 ``fields="open,high,low,close,vol"`` **漏了 trade_date**，
         而紧接着要 ``df.sort_values("trade_date")`` ⇒ 必然 KeyError，
         被 except 吞成 None ⇒ **兜底路径从未成功过，一直是不可达的死代码**。

  实测铁证（2026-09-24 19:05，收盘后）：
      xtdata 末 5 根 = 20260819/20/21/24/**20260825**（滞后 30 个自然日）
      tushare 末 6 根 = 20260917/18/21/22/23/**20260924**
      修复后 _fetch_daily 末收盘 895.86 / 69.90 / 32.63，
      与实盘 engine_state.last_price **逐位一致** ⇒ 滞后 30 天 → 0 天。

本测试用桩件（不打网络、不依赖 miniQMT）锁死：
  · 陈旧判定阈值语义（> DAILY_STALE_DAYS 个自然日才算陈旧）
  · 本地陈旧 ⇒ 优先用 tushare；tushare 不可用 ⇒ 回退本地（降级不丢数据）
  · 本地新鲜 ⇒ 不打扰 tushare（零额外额度消耗）
  · **trade_date 必须在 fields 里**（缺陷② 的直接守卫，防止再次漏掉）

运行（conda env qmt）：python tests/run_all.py
"""
from __future__ import annotations

import datetime as _dt
import inspect

from strategy.daily_context import DAILY_STALE_DAYS, DailyContext


def _bars(last_date: _dt.date, n: int = 120):
    """构造 n 根日线，最后一根的日期为 last_date。"""
    out = []
    for k in range(n):
        d = last_date - _dt.timedelta(days=(n - 1 - k))
        out.append({"ts": _dt.datetime(d.year, d.month, d.day),
                    "open": 10.0 + k, "high": 11.0 + k, "low": 9.0 + k,
                    "close": 10.5 + k, "volume": 1000 + k, "amount": 1.0})
    return out


class _FakeQmt:
    def __init__(self, bars):
        self.bars = bars
        self.calls = 0

    def get_history(self, code, period="1d", count=100):
        self.calls += 1
        return self.bars


class _FakePro:
    """最小 tushare pro 桩：只实现 daily，并校验 fields 是否含 trade_date。"""

    def __init__(self):
        self.seen_fields = None
        self.raise_on_missing = True

    def daily(self, ts_code=None, start_date=None, end_date=None, fields=None):
        self.seen_fields = fields
        if self.raise_on_missing and "trade_date" not in (fields or ""):
            # 复现真实 tushare 行为：没要 trade_date 就没有这一列
            raise KeyError("trade_date")
        import pandas as pd
        return pd.DataFrame({
            "trade_date": ["20260923", "20260924"],
            "open": [1.0, 2.0], "high": [1.5, 2.5],
            "low": [0.5, 1.5], "close": [1.2, 2.2], "vol": [10, 20],
        })


class _FakeTushare:
    def __init__(self, pro=None):
        self.enabled = True
        self._pro = pro
        self.connect_calls = 0

    def _connect(self):
        self.connect_calls += 1
        return self._pro is not None


# ---------------------------------------------------------- 陈旧判定

def test_stale_days_constant_sane():
    """阈值必须能覆盖周末(3 天)+调休，又不能大到放过整周停滞。"""
    assert 3 <= DAILY_STALE_DAYS <= 10


def test_fresh_local_is_not_stale():
    """末根 = 期望交易日（收盘后为今日、盘中/周末为上一交易日）⇒ 不陈旧。

    【2026-09-29 第 13 轮】原断言用「自然日差 = DAILY_STALE_DAYS ⇒ 不陈旧」，
    那正是漏判 3 个交易日的旧语义（见 DAILY_STALE_TDAYS 注释里的实盘铁证），
    已改为**交易日**口径：只有「末根 >= 期望交易日」才算新鲜。
    """
    dc = DailyContext()
    today = _dt.date.today()
    assert dc._is_stale_bars(_bars(today)) is False
    expect = DailyContext._expected_last_trade_date()
    assert dc._is_stale_bars(_bars(expect)) is False


def test_old_local_is_stale():
    """末根早于期望交易日（缺 1 个完整交易日）⇒ 陈旧。"""
    dc = DailyContext()
    expect = DailyContext._expected_last_trade_date()
    old = expect - _dt.timedelta(days=7)
    assert dc._is_stale_bars(_bars(old)) is True


def test_empty_bars_treated_as_stale():
    dc = DailyContext()
    assert dc._is_stale_bars([]) is True


# ---------------------------------------------------------- 取数路径

class _patch:
    """极简属性替换上下文（本仓 tests/run_all.py 直接调 fn()，无 pytest fixture）。"""

    def __init__(self, **kw):
        self.kw = kw

    def __enter__(self):
        import strategy.daily_context as D
        self.saved = {k: getattr(D, k) for k in self.kw}
        for k, v in self.kw.items():
            setattr(D, k, v)
        return self

    def __exit__(self, *exc):
        import strategy.daily_context as D
        for k, v in self.saved.items():
            setattr(D, k, v)
        return False


def _install(dc, bars, pro):
    """把日线取数的两个外部依赖换成桩件，返回 (本地桩, tushare桩)。"""
    fake_qmt = _FakeQmt(bars)
    fake_ts = _FakeTushare(pro)
    return fake_qmt, fake_ts


def test_stale_local_prefers_tushare():
    """本地陈旧 ⇒ 必须用 tushare 的新鲜数据，而不是本地旧数据。"""
    dc = DailyContext()
    old = _dt.date.today() - _dt.timedelta(days=30)
    pro = _FakePro()
    fake_qmt, fake_ts = _install(dc, _bars(old, 120), pro)
    with _patch(qmt_client=fake_qmt, tushare_client=fake_ts):
        d = dc._fetch_daily("300308.SZ", count=120)
    assert d is not None
    # tushare 桩返回 2 根，close = [1.2, 2.2]
    assert d["close"] == [1.2, 2.2]
    assert fake_ts.connect_calls >= 1          # 确实尝试了 tushare


def test_stale_local_includes_trade_date_in_fields():
    """缺陷② 守卫：fields 必须含 trade_date，否则 sort_values 必 KeyError。"""
    dc = DailyContext()
    old = _dt.date.today() - _dt.timedelta(days=30)
    pro = _FakePro()
    fake_qmt, fake_ts = _install(dc, _bars(old, 120), pro)
    with _patch(qmt_client=fake_qmt, tushare_client=fake_ts):
        d = dc._fetch_daily("300308.SZ", count=120)
    assert pro.seen_fields is not None, "tushare 兜底根本没被调用"
    assert "trade_date" in pro.seen_fields, (
        "tushare daily 的 fields 漏了 trade_date ⇒ sort_values 必 KeyError ⇒ "
        "兜底路径再次变成死代码")


def test_fresh_local_does_not_call_tushare():
    """本地新鲜 ⇒ 不该消耗 tushare 额度。"""
    dc = DailyContext()
    pro = _FakePro()
    fake_qmt, fake_ts = _install(dc, _bars(_dt.date.today(), 120), pro)
    with _patch(qmt_client=fake_qmt, tushare_client=fake_ts):
        d = dc._fetch_daily("300308.SZ", count=120)
    assert d is not None
    assert len(d["close"]) == 120               # 用的本地 120 根
    assert fake_ts.connect_calls == 0           # 零额度消耗


def test_tushare_unavailable_falls_back_to_local():
    """tushare 拿不到时不得丢数据 —— 必须回退本地（保持修复前的降级行为）。"""
    dc = DailyContext()
    old = _dt.date.today() - _dt.timedelta(days=30)
    fake_qmt, fake_ts = _install(dc, _bars(old, 120), pro=None)   # tushare 不可用
    with _patch(qmt_client=fake_qmt, tushare_client=fake_ts):
        d = dc._fetch_daily("300308.SZ", count=120)
    assert d is not None
    assert len(d["close"]) == 120               # 回退到本地旧数据，不是 None


def test_tushare_error_falls_back_to_local():
    """tushare 抛异常（限流/断网）时同样回退本地，不能让特征整体失效。"""
    dc = DailyContext()
    old = _dt.date.today() - _dt.timedelta(days=30)

    class _Boom:
        def daily(self, **kw):
            raise RuntimeError("tushare 限流")

    fake_qmt, fake_ts = _install(dc, _bars(old, 120), _Boom())
    with _patch(qmt_client=fake_qmt, tushare_client=fake_ts):
        d = dc._fetch_daily("300308.SZ", count=120)
    assert d is not None and len(d["close"]) == 120


def test_source_module_has_no_hardcoded_old_fields():
    """源码级守卫：_fetch_daily_tushare 里不得再出现缺 trade_date 的 fields。"""
    import strategy.daily_context as D
    src = inspect.getsource(D.DailyContext._fetch_daily_tushare)
    assert 'fields="trade_date,open,high,low,close,vol"' in src, (
        "tushare daily 的 fields 被改回了缺 trade_date 的版本")

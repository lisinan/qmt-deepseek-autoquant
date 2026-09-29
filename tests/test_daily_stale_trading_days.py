# -*- coding: utf-8 -*-
"""
回归守卫：日线「陈旧度」必须是**交易日**口径，不是自然日口径
【2026-09-29 PM-EVOLVE 第 13 轮 P0 修复】

缺陷（实盘铁证，2026-09-29 18:26 收盘后在本机 miniQMT 实测）：
    xtdata.get_market_data_ex(period='1d') 末根 = **20260924**
    缺失 3 个交易日：09-25(五) / 09-28(一) / 09-29(二)
    而旧判据 ``(now.date() - last).days > DAILY_STALE_DAYS(5)``：
        09-29 − 09-24 = **5 个自然日** ⇒ ``5 > 5`` 为 False ⇒ **判为新鲜，不补拉**
    手动 download_history_data 后立刻补齐到 20260929，暴露出这 3 天里：
        300394.SZ  09-28 单日 **-8.63%**（267.93 → 244.80）
        300308.SZ  09-28 单日 **-9.03%**（895.86 → 815.00）
    后果：全部日线特征（MA60 / trend_up / bias / ATR / 动量排名）以及本轮刚落盘的
    ``down_day_exit_pct=-5.0`` 单日暴跌退出（DailyFeatures.day_change_pct）都在用
    **3 个交易日前**的行情判断「今天」。300394 在 09-28 已跌 -8.63% 本应触发 -5%
    退出，而引擎看到的是 09-24 的 -2.71% ⇒ 退出被静默吞掉。

根因：数据按**交易日**变老，阈值却是**自然日**；只要隔一个周末（2 个自然日）就
    必然放过至少 1 个交易日，隔周末+周一就是 3 个交易日。

修复：期望末根 = 今日（15:05 后）/ 上一交易日（盘中与周末）；
    末根 < 期望 ⇒ 陈旧。自然日阈值降级为兜底。

本测试（纯桩件、不联网、不依赖 miniQMT）锁死：
  · 3 个交易日缺失（本次实盘铁证）⇒ 必须判陈旧（旧逻辑判新鲜）
  · 盘中 / 周末不得误判陈旧（否则每次刷新都白补拉）
  · 收盘后缺当日 ⇒ 必须判陈旧（旧逻辑同样判新鲜）
  · 空数据 / 缺 ts ⇒ 陈旧；未来日期 ⇒ 不崩且不判陈旧
"""
from __future__ import annotations

import datetime as _dt

from strategy.daily_context import (
    DAILY_CLOSE_HOUR,
    DAILY_CLOSE_MIN,
    DAILY_STALE_DAYS,
    DAILY_STALE_TDAYS,
    DailyContext,
)


def _bars(last_date: _dt.date, n: int = 120):
    out = []
    for k in range(n):
        d = last_date - _dt.timedelta(days=(n - 1 - k))
        out.append({"ts": _dt.datetime(d.year, d.month, d.day),
                    "open": 10.0 + k, "high": 11.0 + k, "low": 9.0 + k,
                    "close": 10.5 + k, "volume": 1000 + k, "amount": 1.0})
    return out


# 2026-09-24 是周四；09-25 五 / 09-28 一 / 09-29 二
_THU_0924 = _dt.date(2026, 9, 24)
_MON_0928 = _dt.date(2026, 9, 28)
_TUE_0929 = _dt.date(2026, 9, 29)
_NOW_TUE_1826 = _dt.datetime(2026, 9, 29, 18, 26)


def test_regression_three_missing_trading_days_is_stale():
    """★ 本次实盘铁证：末根 09-24、now=09-29 收盘后 ⇒ 必须判陈旧。

    旧的自然日判据下 (5 > 5) = False（判为新鲜 ⇒ 不补拉），这正是缺陷本身；
    这里同时把「旧判据会漏」钉死，防止有人把阈值改回自然日口径。
    """
    dc = DailyContext()
    assert dc._is_stale_bars(_bars(_THU_0924), now=_NOW_TUE_1826) is True
    # 反证：旧口径确实漏判
    assert (_NOW_TUE_1826.date() - _THU_0924).days == DAILY_STALE_DAYS
    assert ((_NOW_TUE_1826.date() - _THU_0924).days > DAILY_STALE_DAYS) is False


def test_refreshed_to_today_is_not_stale():
    dc = DailyContext()
    assert dc._is_stale_bars(_bars(_TUE_0929), now=_NOW_TUE_1826) is False


def test_missing_yesterday_after_close_is_stale():
    """收盘后仍缺当日日线 ⇒ 陈旧（旧逻辑 (1 > 5) = False，同样漏判）。"""
    dc = DailyContext()
    assert dc._is_stale_bars(_bars(_MON_0928), now=_NOW_TUE_1826) is True


def test_intraday_with_yesterday_bar_is_not_stale():
    """盘中：期望末根 = 上一交易日，有了就不陈旧（不得每次刷新都补拉）。"""
    dc = DailyContext()
    now = _dt.datetime(2026, 9, 29, 10, 30)
    assert dc._is_stale_bars(_bars(_MON_0928), now=now) is False
    # 盘中却连上一交易日都没有 ⇒ 陈旧
    assert dc._is_stale_bars(_bars(_THU_0924), now=now) is True


def test_weekend_does_not_false_positive():
    """周末不得误判陈旧（自然日口径下周一/假日后最容易误伤）。"""
    dc = DailyContext()
    fri = _dt.date(2026, 9, 25)
    assert dc._is_stale_bars(_bars(fri), now=_dt.datetime(2026, 9, 26, 9, 0)) is False
    assert dc._is_stale_bars(_bars(fri), now=_dt.datetime(2026, 9, 27, 22, 0)) is False


def test_monday_preopen_expects_friday():
    """周一盘前期望末根 = 周五，拿到周五即算新鲜。"""
    dc = DailyContext()
    fri = _dt.date(2026, 9, 25)
    assert dc._is_stale_bars(_bars(fri), now=_dt.datetime(2026, 9, 28, 8, 40)) is False


def test_expected_last_trade_date_semantics():
    """期望交易日：收盘后=当日；盘中=上一交易日；周末回退到周五。"""
    E = DailyContext._expected_last_trade_date
    assert E(_dt.datetime(2026, 9, 29, 18, 30)) == _dt.date(2026, 9, 29)
    assert E(_dt.datetime(2026, 9, 29, 10, 0)) == _dt.date(2026, 9, 28)
    assert E(_dt.datetime(2026, 9, 26, 12, 0)) == _dt.date(2026, 9, 25)   # 周六
    assert E(_dt.datetime(2026, 9, 27, 12, 0)) == _dt.date(2026, 9, 25)   # 周日
    assert E(_dt.datetime(2026, 9, 28, 9, 0)) == _dt.date(2026, 9, 25)    # 周一盘前


def test_constants_sane():
    assert DAILY_STALE_TDAYS == 1, "缺 1 个完整交易日即须判陈旧"
    assert 3 <= DAILY_STALE_DAYS <= 10, "自然日阈值保留为兜底，量级不应乱改"
    assert (DAILY_CLOSE_HOUR, DAILY_CLOSE_MIN) >= (15, 0)


def test_empty_or_missing_ts_is_stale():
    dc = DailyContext()
    assert dc._is_stale_bars([]) is True
    assert dc._is_stale_bars([{"close": 1.0}]) is True


def test_future_bar_does_not_crash():
    """数据日期晚于 now（时钟/时区异常）⇒ 不崩，且不判陈旧。"""
    dc = DailyContext()
    future = _dt.date(2026, 10, 6)
    assert dc._is_stale_bars(_bars(future), now=_NOW_TUE_1826) is False

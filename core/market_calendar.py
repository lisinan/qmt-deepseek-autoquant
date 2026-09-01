# -*- coding: utf-8 -*-
"""A 股交易时段日历（轻量、零外部依赖、可选 xtdata 校准）

为什么需要：原引擎主循环 24×7 全速运行，非交易时段仍每 3s 拉一次「陈旧
快照 tick」，然后照常聚合 bar、跑策略、尝试下单。两个后果都很严重：

  1. **信号污染**：xtdata 在非交易时段返回的是收盘快照（价格恒定）。
     ``_aggregate_bar`` 按分钟建桶，于是每分钟生成一根「平价 bar」。
     从 15:00 收盘到次日 09:30 开盘共 1110 分钟，而 bar 缓冲区只有
     ``ma_long*6 = 120`` 根 —— 缓冲区被隔夜平价 bar **完整覆写 9.2 次**。
     次日开盘瞬间，120 根 bar 全是同一价格：MA5≡MA20、ATR≈0、
     突破阈值退化，8 因子里的分钟级指标集体失效。
  2. **算力浪费**：非交易时段占一天的 83%，这部分全速轮询 + 组合打分
     + 行业评估纯属白烧 CPU（实测泄漏进程 4 天烧掉 18.9 CPU 小时）。

本模块只回答一个问题：「现在该不该干活」。默认用「周内 + 时段」判定，
若本机 xtdata 可用则用其交易日历校准（自动剔除节假日），失败即静默回退。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime, timedelta
from typing import List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# 连续竞价 + 集合竞价缓冲。09:15 起开始接受集合竞价行情，15:05 留出
# 收盘后成交回报的落账窗口。
DEFAULT_SESSIONS: Tuple[Tuple[str, str], ...] = (
    ("09:15", "11:30"),
    ("12:57", "15:05"),
)

_HOLIDAY_CACHE: dict = {"date": None, "trading_days": None}


def _parse_hhmm(s: str) -> dtime:
    hh, mm = s.split(":")
    return dtime(int(hh), int(mm))


def _sessions_as_time(sessions: Sequence[Tuple[str, str]]
                      ) -> List[Tuple[dtime, dtime]]:
    return [(_parse_hhmm(a), _parse_hhmm(b)) for a, b in sessions]


def _xtdata_trading_days(ref: date) -> Optional[set]:
    """用 xtdata 交易日历校准（含节假日）。缓存到当天，失败返回 None。"""
    if _HOLIDAY_CACHE["date"] == ref and _HOLIDAY_CACHE["trading_days"]:
        return _HOLIDAY_CACHE["trading_days"]
    try:
        from xtquant import xtdata  # type: ignore
        start = (ref - timedelta(days=30)).strftime("%Y%m%d")
        end = (ref + timedelta(days=30)).strftime("%Y%m%d")
        raw = xtdata.get_trading_dates("SH", start_time=start, end_time=end)
        if not raw:
            return None
        days = set()
        for v in raw:
            # xtdata 可能返回毫秒时间戳或 YYYYMMDD 字符串
            try:
                if isinstance(v, (int, float)) and v > 10_000_000_000:
                    days.add(datetime.fromtimestamp(v / 1000).date())
                else:
                    days.add(datetime.strptime(str(v)[:8], "%Y%m%d").date())
            except Exception:
                continue
        if not days:
            return None
        _HOLIDAY_CACHE["date"] = ref
        _HOLIDAY_CACHE["trading_days"] = days
        return days
    except Exception:
        return None


def is_trading_day(d: Optional[date] = None, use_calendar: bool = True) -> bool:
    """是否交易日。

    ⚠️ 关键陷阱（本轮实测踩到并修掉）：``xtdata.get_trading_dates`` **只返回
    到最后一个已完成的交易日**，不含任何未来日期（实测 2026-08-29 查询
    ±30 天，返回 22 个日期，最后一个是 2026-08-28）。若无条件用「d 是否在
    日历里」判定，则**所有未来日期都会被判成非交易日** —— 交易时段守卫会
    让引擎永久休眠、再也不开盘。因此日历只在其**覆盖区间内**可信；
    超出覆盖范围（即未来日期）一律退化为「非周末」启发式。
    """
    d = d or date.today()
    if use_calendar:
        days = _xtdata_trading_days(d)
        if days:
            last_known = max(days)
            first_known = min(days)
            if first_known <= d <= last_known:
                return d in days       # 日历覆盖范围内：权威（含节假日）
            # 超出覆盖范围（未来日）→ 落到下面的周末启发式
    return d.weekday() < 5


def is_trading_time(now: Optional[datetime] = None,
                    sessions: Sequence[Tuple[str, str]] = DEFAULT_SESSIONS,
                    use_calendar: bool = True) -> bool:
    """当前是否处于可交易时段（交易日 且 落在任一时段内）。"""
    now = now or datetime.now()
    if not is_trading_day(now.date(), use_calendar=use_calendar):
        return False
    t = now.time()
    for a, b in _sessions_as_time(sessions):
        if a <= t <= b:
            return True
    return False


def seconds_to_next_session(now: Optional[datetime] = None,
                            sessions: Sequence[Tuple[str, str]] = DEFAULT_SESSIONS,
                            use_calendar: bool = True) -> float:
    """距下一个交易时段开始还有多少秒（用于日志提示；上限 7 天）。"""
    now = now or datetime.now()
    parsed = _sessions_as_time(sessions)
    for day_offset in range(0, 8):
        d = (now + timedelta(days=day_offset)).date()
        if not is_trading_day(d, use_calendar=use_calendar):
            continue
        for a, _b in parsed:
            start = datetime.combine(d, a)
            if start > now:
                return (start - now).total_seconds()
    return 7 * 24 * 3600.0


def session_label(now: Optional[datetime] = None,
                  sessions: Sequence[Tuple[str, str]] = DEFAULT_SESSIONS,
                  use_calendar: bool = True) -> str:
    """人类可读的时段状态，用于日志/快照。"""
    now = now or datetime.now()
    if not is_trading_day(now.date(), use_calendar=use_calendar):
        return "closed_non_trading_day"
    if is_trading_time(now, sessions, use_calendar=use_calendar):
        return "open"
    t = now.time()
    if t < _parse_hhmm(sessions[0][0]):
        return "pre_open"
    if t > _parse_hhmm(sessions[-1][1]):
        return "post_close"
    return "lunch_break"

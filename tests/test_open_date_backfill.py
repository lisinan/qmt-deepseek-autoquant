# -*- coding: utf-8 -*-
"""open_date 回灌守卫（2026-11-09 P0）。

背景：``_restore_engine_state`` 在 live 分支（``restore_ledger=False``）恢复持仓
之前就 return，而 ``_sync_broker_positions`` 建仓时只取内存 ``_pos_meta``
⇒ 进程重启后 live 持仓 ``open_date`` 恒为 None ⇒ ``is_t1_locked()`` 与
``is_in_entry_protection()`` 双双失效（两者在 None 时都返回「不锁定」）。
后果：当日买入的票当天可能被卖出（券商拒单 306050）、建仓保护期形同不存在。

这里只测回灌函数本身的行为契约，不构造完整引擎（引擎构造会连库/连行情）。
"""
from __future__ import annotations

import types
from datetime import datetime

import pytest

from engine.event_engine import EventEngine


class _FakeStorage:
    def __init__(self, rows, boom=False):
        self.rows = rows
        self.boom = boom
        self.calls = 0

    def get_fills(self, limit=200, code=None):
        self.calls += 1
        if self.boom:
            raise RuntimeError("db gone")
        return list(self.rows)


def _fake_self(rows, boom=False):
    o = types.SimpleNamespace()
    o.storage = _FakeStorage(rows, boom=boom)
    o._open_date_looked = set()
    return o


def _call(obj, code="300308.SZ"):
    return EventEngine._backfill_open_date(obj, code)


def test_picks_latest_buy_not_sell():
    """get_fills 按 id 倒序 ⇒ 第一条 BUY 就是最近一笔买入。"""
    rows = [
        {"id": 3, "side": "SELL", "ts": "2026-11-05T10:00:00"},
        {"id": 2, "side": "BUY", "ts": "2026-11-03T10:59:01"},
        {"id": 1, "side": "BUY", "ts": "2026-10-30T09:35:00"},
    ]
    od = _call(_fake_self(rows))
    assert od == datetime(2026, 11, 3, 10, 59, 1)


def test_no_buy_returns_none_and_warns():
    # ★ 不能用 pytest 的 caplog filler：本项目 tests/run_all.py 是自研 runner，
    #   直接 fn() 无参调用，任何 fixture 形参都会 TypeError（已踩）。
    import logging
    rows = [{"id": 1, "side": "SELL", "ts": "2026-11-05T10:00:00"}]
    obj = _fake_self(rows)
    records = []

    class _H(logging.Handler):
        def emit(self, rec):
            records.append(rec.getMessage())

    lg = logging.getLogger("engine.event_engine")
    h = _H()
    lg.addHandler(h)
    try:
        od = _call(obj)
    finally:
        lg.removeHandler(h)

    assert od is None
    assert any("open_date 回灌未命中" in m for m in records), records
    # 未命中也要记入 looked，避免每轮重查
    assert "300308.SZ" in obj._open_date_looked


def test_queries_storage_only_once_per_code():
    obj = _fake_self([{"id": 1, "side": "BUY", "ts": "2026-11-03T10:59:01"}])
    assert _call(obj) is not None
    assert _call(obj) is None          # 第二次：looked 命中，直接 None
    assert obj.storage.calls == 1      # 且没有第二次查库


def test_storage_exception_does_not_raise():
    obj = _fake_self([], boom=True)
    assert _call(obj) is None          # 绝不因查库失败拖垮主循环


def test_bad_ts_returns_none():
    obj = _fake_self([{"id": 9, "side": "BUY", "ts": "not-a-date"}])
    assert _call(obj) is None


def test_missing_ts_returns_none():
    obj = _fake_self([{"id": 9, "side": "BUY", "ts": None}])
    assert _call(obj) is None


def test_paper_path_unaffected():
    """paper 走 restore_ledger=True，open_date 从库里的 positions 字段解析，
    不经过本函数——这里只钉住「本函数不改 paper 语义」：它只对 fills 读，
    没有任何写操作。"""
    obj = _fake_self([{"id": 1, "side": "BUY", "ts": "2026-11-03T10:59:01"}])
    _call(obj)
    assert obj.storage.calls == 1      # 只有读，没有 write 方法可调


def test_both_call_sites_wired():
    """静态守卫：两个调用点都必须存在（存量仓 + 新建仓）。
    少任何一个，都会有一半路径的 open_date 仍为 None。"""
    import io
    src = io.open("engine/event_engine.py", "r", encoding="utf-8").read()
    assert src.count("self._backfill_open_date(code)") == 2, \
        "open_date 回灌必须同时接在「存量仓」与「新建仓」两个分支"
    assert "pos.open_date = self._backfill_open_date(code)" in src
    assert 'or self._backfill_open_date(code),' in src

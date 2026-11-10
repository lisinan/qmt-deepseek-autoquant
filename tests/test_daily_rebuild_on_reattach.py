# -*- coding: utf-8 -*-
"""行情从 mock 恢复后必须重建日线特征（2026-11-10 盘中缺陷的守卫）。

缺陷回顾：`_reattach_market_data()` 只切换实时报价源，不重建日线特征。
引擎在行情未就绪时启动（夜间/盘前/开机自启）会落到 mock，此时算出的
MA20/MA60/动量/宽度全来自合成序列（`core/qmt_client.py:82-95` 基准价任意），
而 reattach 成功后 `data_mode` 变成 tushare、面板显示"已恢复"——特征却还是假的。
2026-11-10 实测 paper 300308 close=122.22 vs 真值 452.03。

★ 本仓测试不得使用 pytest fixture（runner 无参调用）。
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import engine.event_engine as EE  # noqa: E402


class _FakeQc(object):
    """冒充模块级 qmt_client：从 mock 切到目标源（或失败）。"""

    def __init__(self, mode_after="tushare", ok=True):
        self.mode = "mock"
        self._after = mode_after
        self._ok = ok
        self.reattach_n = 0

    def reattach(self):
        self.reattach_n += 1
        if self._ok:
            self.mode = self._after
        return self._ok


class _FakeDaily(object):
    def __init__(self):
        self.calls = []

    def refresh(self, codes=None, force=False):
        self.calls.append((len(codes or []), bool(force)))
        return len(codes or [])


def _fake_engine():
    o = types.SimpleNamespace()
    o.data_mode = "mock"
    o.daily = _FakeDaily()
    o._daily_codes = lambda: ["X.SH", "Y.SZ"]
    return o


def _run(eng, qc):
    old = EE.qmt_client
    EE.qmt_client = qc
    try:
        return EE.EventEngine._reattach_market_data(eng)
    finally:
        EE.qmt_client = old


def test_mock_to_real_triggers_forced_refresh():
    """mock → tushare：必须重算特征，且 force=True（默认 False 会跳过假缓存）。"""
    eng = _fake_engine()
    ok = _run(eng, _FakeQc("tushare"))
    assert ok is True
    assert eng.data_mode == "tushare"
    assert eng.daily.calls == [(2, True)], eng.daily.calls


def test_mock_to_xtdata_also_rebuilds():
    eng = _fake_engine()
    _run(eng, _FakeQc("xtdata"))
    assert eng.daily.calls == [(2, True)]


def test_reattach_failure_does_not_rebuild():
    """切源失败（仍是 mock）不许重建——那只会把一个假特征换成另一个假特征。"""
    eng = _fake_engine()
    ok = _run(eng, _FakeQc("mock", ok=False))
    assert ok is False
    assert eng.daily.calls == []


def test_already_real_returns_early_without_refresh():
    """本来就在真实源上：直接返回，不做任何重建（避免每轮白刷）。"""
    eng = _fake_engine()
    qc = _FakeQc("tushare")
    qc.mode = "tushare"                 # 已在真实源
    ok = _run(eng, qc)
    assert ok is True
    assert qc.reattach_n == 0, "已在真实源时不应调用 reattach"
    assert eng.daily.calls == []


def test_rebuild_exception_does_not_break_reattach():
    """重建失败绝不能阻断"行情已恢复"这件事本身（开盘不能被特征刷新拖垮）。"""
    eng = _fake_engine()

    class _Boom(_FakeDaily):
        def refresh(self, codes=None, force=False):
            raise RuntimeError("tushare 限流")

    eng.daily = _Boom()
    ok = _run(eng, _FakeQc("tushare"))
    assert ok is True
    assert eng.data_mode == "tushare"

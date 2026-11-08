# -*- coding: utf-8 -*-
"""live 断链期**禁止**用构造初值覆盖真实账本（2026-11-08 P0）。

背景（实测证据）：
  live 进程走 ``restore_ledger=False``，内存里只有构造初值
  ``cash=1,000,000.0`` / ``positions={}``（``/api/snapshot`` 2026-11-08 20:47 实测），
  而 ``storage/qmt.db::engine_state`` 是 2026-11-06 收盘的**真实账本**：
  ``cash=11,611.57`` / 2 只持仓 / ``day_open_asset=62,409.6`` / ``peak_asset=67,453.62``。

  ``_shutdown()`` 里 ``_save_engine_state()`` 是**无条件**调用的
  ⇒ 任何一次优雅停止（``main.py --stop`` / Ctrl+C / guardian 重启）都会把
  「初始值」写进那行真实账本：cash 变 100 万、持仓清零、**day_open_asset 变 None**。
  次日开盘后日内跌幅基准全错，熔断/强平口径直接失真 —— 这是资金安全级。

修法（本模块钉住的三条不变量）：
  1. ``exec_mode=="live"`` 且 ``_broker_total_asset<=0``（整个进程从未拿到券商总资产）
     ⇒ **不落盘**，真实账本原样保留；
  2. 一旦券商连上（``_broker_total_asset>0``）⇒ 照常落盘，C6「live 记忆不丢」语义不变；
  3. paper 不受影响（永远有本地账本，与券商是否连通无关）。

全部离线：临时 sqlite，不连 broker、不起主循环、不触网。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import engine.event_engine as EE                       # noqa: E402
from core.data_models import Position                   # noqa: E402
from storage.db import Storage                          # noqa: E402


# ------------------------------------------------------------------ helpers

def _tmpdir():
    """Windows 上 TemporaryDirectory 清 sqlite 会偶发 NotADirectoryError，
    沿用其它 zone/看板测试的 mkdtemp + rmtree(ignore_errors) 写法。"""
    import shutil
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="qmt_save_guard_"))
    return d, lambda: shutil.rmtree(d, ignore_errors=True)


# 2026-11-06 收盘真实账本（值取自 storage/qmt.db，只读核对于 2026-11-08）
GOOD_STATE = {
    "cash": 11611.57,
    "positions": ('[{"code": "300308.SZ", "name": "中际旭创", "quantity": 100,'
                  ' "avg_cost": 501.5679, "last_price": 448.0, "open_date": null},'
                  ' {"code": "510050.SH", "name": "510050", "quantity": 500,'
                  ' "avg_cost": 2.879, "last_price": 2.902, "open_date": null}]'),
    "daily_trade_count": 0,
    "daily_pnl": -4547.03,
    "consec_loss": 0,
    "peak_asset": 67453.62,
    "day_open_asset": 62409.6,
    "tick_count": 9693,
    "peak_equity": 64636.1,
    "trade_date": "2026-11-06",
    "risk_state": '{"halted": true, "halt_reason": "daily_stop -9.32%"}',
}


def _make_engine(storage, exec_mode="live"):
    eng = EE.EventEngine(
        exec_mode=exec_mode,
        storage=storage,
        auto_reconnect=False,
        auto_init_positions=False,
        enable_sector_scorer=False,
        enable_dynamic_universe=False,
        enable_llm_reranker=False,
    )
    eng.analyst = type("FakeAnalyst", (), {"enabled": False})()
    eng.analyst.client = type("FakeClient", (), {})()
    return eng


def _seed_good_ledger(st):
    st.save_engine_state(dict(GOOD_STATE))
    return st.load_engine_state()


# ------------------------------------------------------------------ 1

def test_live_disconnected_does_not_overwrite_ledger():
    """核心回归：live 断链期保存不得覆盖 11-06 的真实账本。"""
    d, cleanup = _tmpdir()
    try:
        st = Storage(d / "q.db")
        try:
            before = _seed_good_ledger(st)
            eng = _make_engine(st, "live")
            # 复刻 2026-11-08 20:47 实测的内存状态
            eng._cash = 1_000_000.0
            eng._positions = {}
            eng._broker_total_asset = 0.0
            eng._save_engine_state()
            after = st.load_engine_state()
            assert float(after["cash"]) == float(before["cash"]), \
                f"现金被构造初值覆盖: {before['cash']} → {after['cash']}"
            assert after["positions"] == before["positions"], \
                "持仓被清空（真实 2 只仓位的账本记录消失）"
            assert after["day_open_asset"] == before["day_open_asset"], \
                "day_open_asset 被改写 ⇒ 次日日内跌幅基准失真"
            assert float(after["peak_asset"]) == float(before["peak_asset"])
        finally:
            st.close()
    finally:
        cleanup()


# ------------------------------------------------------------------ 2

def test_live_connected_persists_normally():
    """券商连上后必须照常落盘（C6：live 风控记忆不能随重启丢）。"""
    d, cleanup = _tmpdir()
    try:
        st = Storage(d / "q.db")
        try:
            _seed_good_ledger(st)
            eng = _make_engine(st, "live")
            eng._cash = 57862.49
            eng._positions = {
                "300308.SZ": Position(
                    code="300308.SZ", name="中际旭创", quantity=100,
                    avg_cost=501.57, last_price=448.0, open_date=None,
                    peak_price=533.38, stop_price=411.286, target_price=0.0),
            }
            eng._broker_total_asset = 57862.49      # 已连上券商
            eng._trade_date = __import__("datetime").date(2026, 11, 9)
            eng._save_engine_state()
            after = st.load_engine_state()
            assert abs(float(after["cash"]) - 57862.49) < 1e-6, \
                "券商连上后必须写回真实现金"
            assert "300308.SZ" in after["positions"]
            assert after["trade_date"] == "2026-11-09"
        finally:
            st.close()
    finally:
        cleanup()


# ------------------------------------------------------------------ 3

def test_paper_still_persists_without_broker():
    """paper 永远有本地账本，禁止被这道 live 闸门连带挡住。"""
    d, cleanup = _tmpdir()
    try:
        st = Storage(d / "q.db")
        try:
            _seed_good_ledger(st)
            eng = _make_engine(st, "paper")
            eng._cash = 999_000.0
            eng._positions = {}
            eng._broker_total_asset = 0.0           # paper 本来就不连券商
            eng._save_engine_state()
            after = st.load_engine_state()
            assert abs(float(after["cash"]) - 999_000.0) < 1e-6, \
                "paper 账本必须照常落盘"
        finally:
            st.close()
    finally:
        cleanup()


# ------------------------------------------------------------------ 4

def _save_body_src() -> str:
    src = (ROOT / "engine" / "event_engine.py").read_text(encoding="utf-8")
    start = src.index("def _save_engine_state")
    end = src.index("def _on_broker_reconnected")
    return src[start:end]


def test_guard_is_inside_save_body():
    """闸门必须在 `_save_engine_state` 体内、且早于真正的 storage 写入。"""
    body = _save_body_src()
    assert "尚未拿到券商总资产" in body, "live 闸门文案丢失（可能被误删）"
    assert body.index("尚未拿到券商总资产") < body.index("save_engine_state({"), \
        "闸门必须在写库之前生效"
    assert 'exec_mode' in body


def test_save_call_sites_still_exist():
    """调用点不得被删：`_persist_equity`（60s 节流）与 `_shutdown`（优雅退出）都必须还在。

    本模块只改「写什么」，不改「写不写」—— 若调用点被顺手删掉，
    live 连上券商后风控记忆就会随重启清零（C6 回归）。
    """
    src = (ROOT / "engine" / "event_engine.py").read_text(encoding="utf-8")
    call_lines = [ln for ln in src.splitlines()
                  if "_save_engine_state()" in ln and "def " not in ln]
    assert len(call_lines) >= 3, f"落盘调用点疑似被删，实际 {len(call_lines)} 处"

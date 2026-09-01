# -*- coding: utf-8 -*-
"""
回归测试：
1. build_name_map：静态 UNIVERSE + SECTOR_CONFIG + 动态候选池 合并，
   解决「行情表/重排序/持仓只显示代码」问题。
2. EventEngine.ensure_recommendations：推荐池为空时用最近行情即时重建，
   解决「LLM rerank 失败: no recommendations yet」误报。
3. EventEngine 重启延续：paper 账本（持仓/现金/日内计数/峰值）持久化与恢复，
   解决「重启外部服务后交易数据丢失、无法连续多日测试」问题。
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import engine.event_engine as EE  # noqa: E402
from strategy.sector_scorer import SectorScorer  # noqa: E402
from data.stock_names import build_name_map  # noqa: E402
from core.data_models import Position  # noqa: E402


def _make_engine():
    eng = EE.EventEngine(
        exec_mode="paper",
        auto_init_positions=False,
        enable_sector_scorer=False,
        enable_dynamic_universe=False,
        enable_llm_reranker=False,
    )
    # AIAnalyst.enabled 是只读属性：用轻量桩替换，避免触发 AI 线程
    eng.analyst = type("FakeAnalyst", (), {"enabled": False})()
    # snapshot() 会读 analyst.client，补一个桩避免 AttributeError
    eng.analyst.client = type("FakeClient", (), {})()
    return eng


def test_build_name_map_static():
    nm = build_name_map(dynamic_universe=None)
    assert nm.get("300308.SZ") == "中际旭创"
    assert nm.get("000300.SH") == "沪深300"


def test_build_name_map_merges_dynamic():
    class FakeDU:
        def universe_map(self):
            return {"600000.SH": {"name": "浦发银行"},
                    "000001.SZ": {"name": "平安银行"}}

    nm = build_name_map(dynamic_universe=FakeDU())
    assert nm.get("600000.SH") == "浦发银行"
    assert nm.get("000001.SZ") == "平安银行"
    # 静态名称优先于动态（不被覆盖）
    assert nm.get("300308.SZ") == "中际旭创"


def _fake_storage(rows=None):
    """内存桩 Storage：避免单测触碰真实 SQLite / 网络。"""
    class _FS:
        def __init__(self, rows):
            self._rows = rows if rows is not None else []

        def get_sector_recommendations(self, limit=50, sector=None, code=None):
            return self._rows[:limit]

        def save_sector_recommendation(self, rec):
            return 0

        def save_risk_snapshot(self, snap):
            return 0

        def save_equity_snapshot(self, *a, **k):
            return 0
    return _FS(rows)


def _patch_tushare():
    """把 tushare.summary 替换为恒返回 None，避免单测触发网络。"""
    from data.tushare_client import tushare_client
    prev = tushare_client.summary
    try:
        tushare_client.summary = lambda code: None  # noqa: E731
    except Exception:
        pass
    return prev


def _restore_tushare(prev):
    from data.tushare_client import tushare_client
    try:
        tushare_client.summary = prev
    except Exception:
        pass


def test_ensure_recommendations_builds_from_last_ticks():
    eng = _make_engine()
    eng.sector_scorer = SectorScorer()
    eng.storage = _fake_storage([])
    # 模拟最近一次行情（光模块两只领涨）
    eng._last_ticks = {
        "300308.SZ": {"price": 160.0, "change_pct": 3.5, "volume": 1000},
        "300502.SZ": {"price": 90.0, "change_pct": 2.1, "volume": 800},
    }
    eng._bars = {}
    # 趋势桩：固定分数，避免依赖真实 TrendStrategy
    eng._trend = type("FakeTrend", (), {
        "on_bars": staticmethod(
            lambda code, name, bars: type("Sig", (), {"score": 5.0})())
    })()

    assert eng.sector_scorer.recommendations == [], "初始推荐池应为空"
    ok = eng.ensure_recommendations()
    assert ok is True, "应能基于 _last_ticks 即时重建推荐池"
    recs = eng.latest_recommendations()
    assert len(recs) >= 1, "重建后应有推荐"
    codes = {r["code"] for r in recs}
    assert "300308.SZ" in codes, "光模块领涨股应进入推荐池"


def test_ensure_recommendations_no_ticks_returns_false():
    eng = _make_engine()
    eng.sector_scorer = SectorScorer()
    eng.storage = _fake_storage([])   # 无持久化记录
    eng._last_ticks = {}
    eng._bars = {}
    ok = eng.ensure_recommendations()
    assert ok is False, "无行情且无持久化时应返回 False（不下伪造推荐）"


def test_ensure_recommendations_falls_back_to_sqlite():
    """无实时行情（刚重启 / 非交易时段）时，回退 SQLite 持久化推荐池。"""
    eng = _make_engine()
    eng.sector_scorer = SectorScorer()
    eng._last_ticks = {}   # 模拟刚重启 / 非交易时段无实时行情
    eng._bars = {}
    rows = [
        {"ts": "2026-09-01T10:30:00", "code": "300308.SZ", "name": "中际旭创",
         "sector": "optical", "sector_label": "光模块", "composite": 7.5,
         "heat_contribution": 8.0, "tech_score": 6.0, "fundamental_score": 5.0,
         "pe": 40.0, "roe": 22.0, "change_pct": 3.2, "reason": "fallback"},
        {"ts": "2026-09-01T10:30:00", "code": "300502.SZ", "name": "新易盛",
         "sector": "optical", "sector_label": "光模块", "composite": 6.8,
         "heat_contribution": 7.5, "tech_score": 5.5, "fundamental_score": 5.0,
         "pe": 50.0, "roe": 18.0, "change_pct": 2.4, "reason": "fallback"},
    ]
    eng.storage = _fake_storage(rows)
    ok = eng.ensure_recommendations()
    assert ok is True, "无实时行情时应回退 SQLite 持久化推荐池"
    recs = eng.latest_recommendations()
    codes = {r["code"] for r in recs}
    assert "300308.SZ" in codes and "300502.SZ" in codes, "应载入持久化推荐"


def test_build_risk_snapshot_intraday():
    """snapshot 应补充真实日内总盈亏（已实现 + 当日浮动），无 round-trip 也不再恒为 0。"""
    eng = _make_engine()
    eng._day_open_asset = 1000.0
    snap = eng._build_risk_snapshot(1050.0)
    assert snap["intraday_pnl"] == 50.0, "日内盈亏应为 当前资产-开盘资产"
    assert abs(snap["intraday_pnl_pct"] - 5.0) < 1e-6, "日内收益率应为 5%"
    assert snap["day_open_asset"] == 1000.0
    # 无基线时回退 0（不报 None，前端 fmt 显示 0 而非 --）
    eng2 = _make_engine()
    eng2._day_open_asset = None
    snap2 = eng2._build_risk_snapshot(1050.0)
    assert snap2["intraday_pnl"] == 0.0
    assert snap2["day_open_asset"] is None


def test_snapshot_exposes_intraday_pnl():
    eng = _make_engine()
    eng._day_open_asset = 1000.0
    snap = eng.snapshot()
    assert "intraday_pnl" in snap["risk"], "snapshot.risk 应含 intraday_pnl"
    assert isinstance(snap["risk"]["intraday_pnl"], float)
    assert "intraday_pnl_pct" in snap["risk"]


def test_restore_engine_state_applies_book():
    """重启后应恢复 paper 账本（持仓/现金/日内计数/峰值），连续测试不丢数据。"""
    eng = _make_engine()
    state = {
        "cash": 123456.0,
        "positions": json.dumps([{
            "code": "300308.SZ", "name": "中际旭创", "quantity": 100,
            "avg_cost": 150.0, "last_price": 160.0,
            "open_date": "2026-09-01T09:35:00", "peak_price": 162.0,
            "stop_price": 140.0, "target_price": 180.0,
        }]),
        "daily_trade_count": 3, "daily_pnl": 1234.5, "consec_loss": 1,
        "peak_asset": 200000.0, "day_open_asset": 100000.0,
        "tick_count": 42, "peak_equity": 200000.0,
        "trade_date": date.today().isoformat(),
    }

    class _FS:
        def load_engine_state(self):
            return state

        def save_engine_state(self, s):
            return 1

    eng.storage = _FS()
    eng._restore_engine_state()
    assert abs(eng._cash - 123456.0) < 1e-6, "现金应恢复"
    assert "300308.SZ" in eng._positions, "持仓应恢复"
    assert eng._positions["300308.SZ"].quantity == 100
    assert eng._daily_trade_count == 3
    assert abs(eng.risk._daily_pnl - 1234.5) < 1e-6, "已实现日内盈亏应恢复"
    assert eng._tick_count == 42


def test_save_engine_state_serializes_positions():
    """保存时应把当前 paper 账本序列化进 storage。"""
    eng = _make_engine()
    eng._cash = 99999.0
    eng._positions = {
        "300308.SZ": Position(
            code="300308.SZ", name="中际旭创", quantity=100,
            avg_cost=150.0, last_price=160.0, open_date=None,
            peak_price=162.0, stop_price=140.0, target_price=180.0),
    }
    captured = {}

    class _FS:
        def load_engine_state(self):
            return None

        def save_engine_state(self, s):
            captured.update(s)
            return 1

    eng.storage = _FS()
    eng._save_engine_state()
    assert abs(captured["cash"] - 99999.0) < 1e-6
    assert "300308.SZ" in captured["positions"]
    assert json.loads(captured["positions"])[0]["quantity"] == 100


def test_single_mode_evaluates_sectors():
    """单标的模式下主循环每 N tick 也自动生成产业链推荐池（修复 no recommendations yet）。"""
    import core.qmt_client as qc
    from datetime import date as _date
    orig = qc.qmt_client.get_ticks
    eng = _make_engine()
    eng.sector_scorer = SectorScorer()
    eng._portfolio = None                 # 单模式
    from collections import defaultdict, deque
    eng._bars = defaultdict(deque)        # _aggregate_bar 需要 defaultdict(deque)
    eng._last_daily_date = _date.today()  # 跳过每日刷新线程（避免网络）
    eng._tick_count = 5                   # 触发 %5==0 的 sector 评估
    eng._trend = type("FakeTrend", (), {
        "on_bars": staticmethod(
            lambda code, name, bars: type("Sig", (), {"score": 5.0})())
    })()
    eng.storage = _fake_storage([])
    fake = {
        "300308.SZ": {"lastPrice": 160.0, "lastClose": 155.0, "open": 158.0,
                      "high": 161.0, "low": 157.0, "volume": 1000, "amount": 160000.0},
        "300502.SZ": {"lastPrice": 90.0, "lastClose": 88.0, "open": 89.0,
                      "high": 91.0, "low": 88.0, "volume": 800, "amount": 72000.0},
    }
    qc.qmt_client.get_ticks = lambda codes: fake
    prev_ts = _patch_tushare()
    try:
        eng._run_once(["300308.SZ", "300502.SZ"])
    finally:
        qc.qmt_client.get_ticks = orig
        _restore_tushare(prev_ts)
    recs = eng.latest_recommendations()
    assert len(recs) >= 1, "单模式下应自动生成推荐池"
    assert "300308.SZ" in {r["code"] for r in recs}, "光模块领涨股应进入推荐池"

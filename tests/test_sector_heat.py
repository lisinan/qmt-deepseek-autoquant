# -*- coding: utf-8 -*-
"""产业热力图数据回归测试（2026-09-22 展示优化）。

背景：热力图原来只有「热度 / 平均涨幅 / 上涨数 / 领涨股」四项，而 SectorScore
里已有的 ``avg_volume_ratio`` 从未下发，也没有"领跌"，于是出现了实质性误读：
实测 PCB互联 avg +7.14% 看起来一片火热，但上涨只有 1/3 —— 平均涨幅是被单只
暴涨拉高的，板块内多数票在跌。页面对此毫无提示。

本文件锁定：
  1. evaluate_sectors 正确记录领跌（worst_*）；
  2. latest_sector_heat 把量比、领涨、领跌一并下发给前端；
  3. 极端分化场景（少数大涨 + 多数下跌）的数据能被算出来供前端标注。
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from strategy.sector_scorer import SectorScorer  # noqa: E402


def _md(pairs):
    return {c: {"change_pct": p, "volume_ratio": 1.0} for c, p in pairs}


def test_sector_score_tracks_worst():
    """领跌必须与领涨同时记录，否则算不出分化度。"""
    s = SectorScorer()
    s.evaluate_sectors(_md([("300308.SZ", 7.5), ("300502.SZ", -2.0),
                            ("300394.SZ", -3.1)]))
    sc = s.sector_scores["光模块"]
    assert sc.best_code == "300308.SZ"
    assert sc.best_change_pct == 7.5
    assert sc.worst_code == "300394.SZ"
    assert sc.worst_change_pct == -3.1


def test_divergence_is_detectable():
    """单只暴涨 + 多数下跌：平均涨幅为正但上涨占比很低。

    前端据此标注「内部分化」，避免把平均涨幅读成普涨。
    """
    s = SectorScorer()
    s.evaluate_sectors(_md([("300308.SZ", 7.5), ("300502.SZ", -2.0),
                            ("300394.SZ", -3.1)]))
    sc = s.sector_scores["光模块"]
    spread = sc.best_change_pct - sc.worst_change_pct
    assert sc.avg_change_pct > 0, "平均涨幅为正（被单只拉动）"
    assert sc.n_up < sc.n_stocks, "但并未普涨"
    assert spread >= 5.0, f"价差应达到分化阈值，实际 {spread}"


def test_worst_defaults_when_no_data():
    """某环节完全没有行情数据时不能残留 1e9 之类的哨兵值。"""
    s = SectorScorer()
    s.evaluate_sectors({})
    assert s.sector_scores == {}, "无数据时不产生评分"


def test_all_up_sector_has_positive_worst():
    """普涨板块的领跌也应是正的（不能误用 1e9 哨兵值）。

    光模块环节共 4 只（300308/300502/300394/300570），必须给全，
    否则 n_stocks(配置 4) 与 n_up(有行情 3) 不相等。
    """
    s = SectorScorer()
    s.evaluate_sectors(_md([("300308.SZ", 3.0), ("300502.SZ", 1.5),
                            ("300394.SZ", 0.8), ("300570.SZ", 2.2)]))
    sc = s.sector_scores["光模块"]
    assert sc.worst_change_pct == 0.8
    assert sc.n_up == sc.n_stocks == 4


def test_latest_sector_heat_exposes_volume_and_worst():
    """引擎下发给前端的字段必须包含量比与领跌，否则前端无从展示。"""
    from engine.event_engine import EventEngine

    e = EventEngine.__new__(EventEngine)

    class Scorer:
        def __init__(self):
            self.sector_scores = {"光模块": __import__(
                "strategy.sector_scorer", fromlist=["SectorScore"]
            ).SectorScore(
                sector="光模块", label="光模块/光器件", heat_score=7.1,
                avg_change_pct=1.2, avg_volume_ratio=1.35, strength=0.67,
                n_stocks=4, n_up=2, best_code="300308.SZ", best_name="中际旭创",
                best_change_pct=3.4, worst_code="300394.SZ", worst_name="天孚通信",
                worst_change_pct=-1.8)}

    e.sector_scorer = Scorer()
    out = e.latest_sector_heat()
    d = out["光模块"]
    for k in ("heat_score", "avg_change_pct", "avg_volume_ratio", "strength",
              "n_up", "n_stocks", "best_name", "best_change_pct",
              "worst_name", "worst_change_pct", "label"):
        assert k in d, f"latest_sector_heat 缺字段 {k}"
    assert d["avg_volume_ratio"] == 1.35
    assert d["worst_name"] == "天孚通信"
    assert d["worst_change_pct"] == -1.8


def _real_engine():
    """构造一个真实引擎实例（关掉外部依赖），用于读实例级旋钮。"""
    from engine.event_engine import EventEngine
    return EventEngine(exec_mode="paper", auto_init_positions=False,
                       enable_sector_scorer=True,
                       enable_dynamic_universe=False,
                       enable_llm_reranker=False)


def test_sector_eval_every_round_by_default():
    """单模式下热度默认每轮评估，才能跟上 3s 行情节奏。

    原实现与组合模式共用 ``portfolio_every_n = 5``，热力图比行情慢 5 倍。
    """
    e = _real_engine()
    assert e.SECTOR_EVAL_EVERY_N_ROUNDS == 1, "默认应为每轮评估（与 3s 行情同频）"


def test_sector_eval_knob_is_reversible():
    """旋钮改成 5 必须立刻恢复「每 5 轮评估一次」的旧行为（可逆性要求）。"""
    import inspect
    from engine.event_engine import EventEngine
    e = _real_engine()
    e.SECTOR_EVAL_EVERY_N_ROUNDS = 5
    assert e.SECTOR_EVAL_EVERY_N_ROUNDS == 5
    # _run_once 必须真的读这个旋钮，而不是仍然写死 5 或 1
    src = inspect.getsource(EventEngine._run_once)
    assert "self.SECTOR_EVAL_EVERY_N_ROUNDS" in src
    # 组合模式仍按 portfolio_every_n 节流（不能一并放开，select() 更重）
    assert "portfolio_every_n" in src

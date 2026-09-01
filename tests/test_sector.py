# -*- coding: utf-8 -*-
"""strategy/sector_scorer.py 单元测试。"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy.sector_scorer import SectorScorer, SectorScore, StockRecommendation


def _market_data():
    return {
        "300308.SZ": {"change_pct": 5.2, "volume_ratio": 1.5, "price": 870.0},
        "300502.SZ": {"change_pct": 4.8, "volume_ratio": 1.8},
        "300394.SZ": {"change_pct": -1.2, "volume_ratio": 0.8},
        "300570.SZ": {"change_pct": 3.5, "volume_ratio": 1.3},
        "688256.SH": {"change_pct": 8.1, "volume_ratio": 2.5},
        "688041.SH": {"change_pct": 5.5, "volume_ratio": 1.7},
        "300474.SZ": {"change_pct": 2.0, "volume_ratio": 1.1},
        "603986.SH": {"change_pct": -2.1, "volume_ratio": 0.7},
        "688981.SH": {"change_pct": 1.5, "volume_ratio": 0.9},
        "002371.SZ": {"change_pct": 3.2, "volume_ratio": 1.4},
        "688012.SH": {"change_pct": 2.8, "volume_ratio": 1.2},
        "603019.SH": {"change_pct": -3.5, "volume_ratio": 0.6},
        "000977.SZ": {"change_pct": 4.1, "volume_ratio": 1.6},
        "300383.SZ": {"change_pct": 1.0, "volume_ratio": 0.9},
        "603881.SH": {"change_pct": 0.5, "volume_ratio": 0.8},
        "002230.SZ": {"change_pct": 2.5, "volume_ratio": 1.2},
        "688111.SH": {"change_pct": 6.2, "volume_ratio": 2.0},
        "300033.SZ": {"change_pct": 1.5, "volume_ratio": 1.0},
        "300496.SZ": {"change_pct": 3.8, "volume_ratio": 1.5},
        "002415.SZ": {"change_pct": -0.5, "volume_ratio": 0.9},
    }


def test_evaluate_basic():
    s = SectorScorer()
    scores = s.evaluate_sectors(_market_data())
    assert isinstance(scores, dict)
    # 至少 3 个环节有数据
    assert len(scores) >= 3
    for name, sc in scores.items():
        assert isinstance(sc, SectorScore)
        assert 0 <= sc.heat_score <= 10
        assert sc.n_stocks > 0


def test_evaluate_picks_best_in_sector():
    s = SectorScorer()
    scores = s.evaluate_sectors(_market_data())
    # 光模块最强应该是 300308.SZ (涨幅 5.2%)
    guang = scores.get("光模块")
    if guang:
        assert guang.best_code == "300308.SZ"
        assert guang.best_change_pct == 5.2


def test_evaluate_empty_market():
    s = SectorScorer()
    scores = s.evaluate_sectors({})
    # 空数据 → 没环节有分
    assert scores == {}


def test_evaluate_skips_unknown_codes():
    """不在配置里的代码应被忽略。"""
    s = SectorScorer()
    md = {"XXXXXX.SZ": {"change_pct": 100, "volume_ratio": 100}}
    scores = s.evaluate_sectors(md)
    assert scores == {}


def test_build_recommendations_returns_top_n():
    s = SectorScorer()
    md = _market_data()
    s.evaluate_sectors(md)
    recs = s.build_recommendations(md, tech_scores={})
    assert len(recs) <= s.config["recommendation_pool_size"]
    # 综合分降序
    for i in range(len(recs) - 1):
        assert recs[i].composite >= recs[i + 1].composite


def test_build_recommendations_filter_by_score():
    s = SectorScorer()
    s.config["min_composite_for_pool"] = 9.0   # 极高门槛
    s.config["adaptive_threshold"] = False   # 关掉自适应（为该测试）
    md = _market_data()
    s.evaluate_sectors(md)
    recs = s.build_recommendations(md, tech_scores={})
    # 应该没有推荐（综合分都不可能到 9）
    assert recs == []
    # 还原不影响其他测试
    s.config["min_composite_for_pool"] = 4.5
    s.config["adaptive_threshold"] = True


def test_top_sectors_sorted():
    s = SectorScorer()
    s.evaluate_sectors(_market_data())
    top = s.top_sectors(3)
    for i in range(len(top) - 1):
        assert top[i].heat_score >= top[i + 1].heat_score


def test_best_target():
    s = SectorScorer()
    md = _market_data()
    s.evaluate_sectors(md)
    s.build_recommendations(md, tech_scores={})
    best = s.best_target()
    if best:
        assert isinstance(best, StockRecommendation)
        recs = s.recommendations
        assert best.code == recs[0].code


def test_snapshot():
    s = SectorScorer()
    md = _market_data()
    s.evaluate_sectors(md)
    s.build_recommendations(md, tech_scores={})
    snap = s.snapshot()
    assert "sector_scores" in snap
    assert "recommendations" in snap
    assert "best_target" in snap


def test_history_appended():
    s = SectorScorer()
    md = _market_data()
    s.evaluate_sectors(md)
    s.build_recommendations(md, tech_scores={})
    assert len(s.history) >= 1
    h = s.history[-1]
    assert "top_code" in h
    assert "pool_codes" in h
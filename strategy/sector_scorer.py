# -*- coding: utf-8 -*-
"""
AI 产业链热度评分引擎

逻辑（参考 qmtIDE-kimik3/sector_scorer.py）：

  1. 把 Universe 按产业链分为 6 个环节（光模块/AI 芯片/晶圆代工/服务器/PCB/AI 应用）
  2. 每轮基于实时行情，计算每个环节的热度：
     heat = strength * 4 + max(0, avg_change_pct) * 0.5 + min(2, avg_vol_ratio) * 1.5
     其中 strength = 上涨家数 / 总家数（0~1）
  3. 从热度最高的环节里挑代表股 + 叠加基本面 + 技术面 → 综合分
  4. 综合分 >= threshold 的进入推荐池（按分排序，Top-N）
  5. 推荐池持久化到 SQLite

数据源：
  - 实时行情：core.qmt_client.QMTClient（push + snapshot）
  - 基本面：data.tushare_client.TushareClient（PE/PB/ROE）
  - 技术面：strategy.trend_strategy.TrendStrategy（6 因子评分）
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from config.settings import SECTOR_CONFIG
from data.tushare_client import tushare_client
from storage.db import Storage

logger = logging.getLogger(__name__)


@dataclass
class SectorScore:
    """单个环节的热度评分。"""
    sector: str
    label: str
    heat_score: float           # 综合热度 0~10
    avg_change_pct: float       # 环节平均涨幅 %
    avg_volume_ratio: float      # 环节平均量比
    strength: float             # 上涨家数比例 0~1
    n_stocks: int               # 环节内总股票数
    n_up: int                   # 上涨家数
    best_code: str = ""
    best_name: str = ""
    best_change_pct: float = 0.0


@dataclass
class StockRecommendation:
    """单只股票的推荐。"""
    ts: datetime
    code: str
    name: str
    sector: str
    sector_label: str
    composite: float = 0.0     # 综合分 0~10
    heat_contribution: float = 0.0
    tech_score: float = 0.0    # TrendStrategy 评分
    fundamental_score: float = 0.0
    pe: Optional[float] = None
    roe: Optional[float] = None
    change_pct: float = 0.0
    reason: str = ""


class SectorScorer:
    def __init__(self, config: Optional[dict] = None):
        # 深拷贝顶层 dict，防止测试原地修改污染全局 SECTOR_CONFIG
        import copy
        self.config = copy.deepcopy(config) if config else copy.deepcopy(SECTOR_CONFIG)
        self._sector_scores: Dict[str, SectorScore] = {}
        self._recommendations: List[StockRecommendation] = []
        self._history: deque = deque(maxlen=self.config.get("history_limit", 100))
        self._lock = threading.RLock()
        self._last_eval_ts: float = 0

    # ============================================================ 评分

    def evaluate_sectors(self,
                         market_data: Dict[str, dict]
                         ) -> Dict[str, SectorScore]:
        """
        对每个环节计算热度。

        market_data: {code: {"change_pct": float, "volume_ratio": float, ...}}
                     通常来自 QMTClient.get_ticks() 后的归一化数据
        """
        out: Dict[str, SectorScore] = {}
        for sector, cfg in self.config["sectors"].items():
            stocks = cfg["stocks"]
            changes: List[float] = []
            vol_ratios: List[float] = []
            n_up = 0
            best_code = ""
            best_name = ""
            best_chg = -1e9

            for code, name in stocks:
                md = market_data.get(code)
                if not md:
                    continue
                chg = float(md.get("change_pct") or 0)
                vr = float(md.get("volume_ratio") or 1.0)
                changes.append(chg)
                vol_ratios.append(vr)
                if chg > 0:
                    n_up += 1
                if chg > best_chg:
                    best_chg = chg
                    best_code = code
                    best_name = name

            n = len(changes)
            if n == 0:
                continue

            strength = n_up / n
            avg_chg = sum(changes) / n
            avg_vr = sum(vol_ratios) / n

            # 综合热度（0~10）
            # strength（0~1）→ 0~4 分
            # 涨幅（>0 部分，每 1% → 0.5 分，上限 3）
            # 量比（0~2）→ 0~3 分
            heat = (
                strength * 4.0
                + max(0.0, min(6.0, avg_chg)) * 0.5
                + min(2.0, max(0.0, avg_vr)) * 1.5
            )
            heat = min(10.0, max(0.0, heat))

            out[sector] = SectorScore(
                sector=sector,
                label=cfg["label"],
                heat_score=round(heat, 2),
                avg_change_pct=round(avg_chg, 2),
                avg_volume_ratio=round(avg_vr, 2),
                strength=round(strength, 2),
                n_stocks=len(stocks),
                n_up=n_up,
                best_code=best_code,
                best_name=best_name,
                best_change_pct=round(best_chg, 2),
            )

        with self._lock:
            self._sector_scores = out
            self._last_eval_ts = time.time()
        return out

    # ============================================================ 推荐池

    def build_recommendations(self,
                              market_data: Dict[str, dict],
                              tech_scores: Optional[Dict[str, float]] = None,
                              ) -> List[StockRecommendation]:
        """
        生成推荐池：
        - 候选：所有环节的代表股（去重）
        - 综合分 = 0.4 * 热度贡献 + 0.3 * 技术分 + 0.3 * 基本面分
        - 取综合分 >= min_composite_for_pool 的 Top-N
        """
        tech_scores = tech_scores or {}
        with self._lock:
            scores = dict(self._sector_scores)
            sector_scores_snapshot = dict(self._sector_scores)

        candidates: Dict[str, dict] = {}  # code -> {sector, score}
        for sector, sc in scores.items():
            for code, name in self.config["sectors"][sector]["stocks"]:
                if code not in candidates:
                    candidates[code] = {"name": name, "sector": sector,
                                        "sector_label": sc.label,
                                        "sector_score": sc.heat_score}
                else:
                    # 同代码出现在多个环节：取热度高的
                    if sc.heat_score > candidates[code]["sector_score"]:
                        candidates[code]["sector"] = sector
                        candidates[code]["sector_label"] = sc.label
                        candidates[code]["sector_score"] = sc.heat_score

        recs: List[StockRecommendation] = []
        for code, info in candidates.items():
            md = market_data.get(code, {})
            chg = float(md.get("change_pct") or 0)
            vr = float(md.get("volume_ratio") or 1)

            # 热度贡献：该股所在环节的 heat_score
            heat_contrib = info["sector_score"]

            # 技术面分（来自 TrendStrategy 调用结果）
            tech = float(tech_scores.get(code, 0))

            # 基本面分：基于 PE/ROE
            fund = self._fundamental_score(code)

            # 综合分
            composite = 0.4 * heat_contrib + 0.3 * tech + 0.3 * fund
            composite = round(min(10.0, composite), 2)

            pe = roe = None
            if tushare_client.enabled:
                s = tushare_client.summary(code) or {}
                pe = s.get("pe")
                roe = s.get("roe")

            rec = StockRecommendation(
                ts=datetime.now(),
                code=code, name=info["name"],
                sector=info["sector"], sector_label=info["sector_label"],
                composite=composite,
                heat_contribution=round(heat_contrib, 2),
                tech_score=round(tech, 2),
                fundamental_score=round(fund, 2),
                pe=pe, roe=roe,
                change_pct=round(chg, 2),
                reason=f"环节[{info['sector_label']}]热度={info['sector_score']:.1f} "
                       f"技术={tech:.1f} 基本面={fund:.1f}",
            )
            recs.append(rec)

        # 过滤 + 排序
        min_score = self.config.get("min_composite_for_pool", 4.5)
        pool_size = self.config.get("recommendation_pool_size", 5)
        recs_filtered = [r for r in recs if r.composite >= min_score]
        if (not recs_filtered
                and self.config.get("adaptive_threshold", True)):
            floor = self.config.get("min_threshold_floor", 1.5)
            for new_th in (3.0, floor):
                recs_filtered = [r for r in recs if r.composite >= new_th]
                if recs_filtered:
                    logger.info("推荐池为空，降阈值到 %.1f（出 %d 只）",
                                new_th, len(recs_filtered))
                    break
        recs = recs_filtered
        recs.sort(key=lambda r: r.composite, reverse=True)
        recs = recs[:pool_size]

        with self._lock:
            self._recommendations = recs
            # 推一条到历史
            if recs:
                self._history.append({
                    "ts": datetime.now().isoformat(),
                    "top_code": recs[0].code,
                    "top_name": recs[0].name,
                    "top_score": recs[0].composite,
                    "pool_codes": [r.code for r in recs],
                })
        return recs

    def _fundamental_score(self, code: str) -> float:
        """基本面分（0~10）。拿不到数据返回中性 5.0。"""
        if not tushare_client.enabled:
            return 5.0
        info = tushare_client.summary(code)
        if not info:
            return 5.0
        score = 5.0
        # PE 越低越好（但 PE < 0 是亏损）
        pe = info.get("pe")
        if pe is not None:
            if pe < 0:
                score -= 2.0
            elif pe < 30:
                score += 1.5
            elif pe < 60:
                score += 0.5
            elif pe < 100:
                score -= 0.5
            elif pe < 200:
                score -= 1.5
            else:
                score -= 2.5
        # ROE 越高越好
        roe = info.get("roe")
        if roe is not None:
            if roe > 20:
                score += 1.5
            elif roe > 10:
                score += 0.5
            elif roe < 0:
                score -= 1.5
        # 营收同比
        or_yoy = info.get("or_yoy")
        if or_yoy is not None:
            if or_yoy > 50:
                score += 0.5
            elif or_yoy < -20:
                score -= 0.5
        return max(0.0, min(10.0, score))

    # ============================================================ 查询

    @property
    def sector_scores(self) -> Dict[str, SectorScore]:
        with self._lock:
            return dict(self._sector_scores)

    @property
    def recommendations(self) -> List[StockRecommendation]:
        with self._lock:
            return list(self._recommendations)

    @property
    def history(self) -> List[dict]:
        with self._lock:
            return list(self._history)

    def top_sectors(self, n: int = 3) -> List[SectorScore]:
        return sorted(self.sector_scores.values(),
                      key=lambda s: s.heat_score, reverse=True)[:n]

    def best_target(self) -> Optional[StockRecommendation]:
        recs = self.recommendations
        return recs[0] if recs else None

    def load_recommendations(self, recs: List["StockRecommendation"]) -> None:
        """从外部（如 SQLite 持久化）载入推荐池，供无实时行情时兜底重排序。"""
        with self._lock:
            self._recommendations = list(recs)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "last_eval_ts": self._last_eval_ts,
                "sector_scores": {k: asdict(v) for k, v in
                                  self._sector_scores.items()},
                "recommendations": [asdict(r) for r in self._recommendations],
                "top_sector": (self.top_sectors(1)[0].sector
                                if self.top_sectors(1) else None),
                "best_target": (asdict(self.best_target())
                                if self.best_target() else None),
            }

    # ============================================================ 持久化

    def save_to_storage(self) -> int:
        """把推荐池写进 SQLite（sector_recommendations 表）。"""
        try:
            storage = Storage()
            for r in self.recommendations:
                storage._conn_get().execute(
                    "INSERT INTO sector_recommendations("
                    "ts, code, name, sector, sector_label, composite,"
                    " heat_contribution, tech_score, fundamental_score,"
                    " pe, roe, change_pct, reason)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r.ts.isoformat(), r.code, r.name,
                     r.sector, r.sector_label, r.composite,
                     r.heat_contribution, r.tech_score, r.fundamental_score,
                     r.pe, r.roe, r.change_pct, r.reason),
                )
            storage._conn_get().commit()
            storage.close()
            return len(self.recommendations)
        except Exception as e:
            logger.warning("save_to_storage 失败: %s", e)
            return 0


# 模块级单例
sector_scorer = SectorScorer()
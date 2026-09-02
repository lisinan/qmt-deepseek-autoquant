# -*- coding: utf-8 -*-
"""
日线多周期上下文（MTF, Multi-TimeFrame）

解决核心问题：
  - 原策略只在 1 分钟 K 线上算 MA5/10/20（即 5/10/20 *分钟*），
    所谓"趋势"本质是分钟级噪音，期望值低、来回打脸。
  - 本模块在引擎启动时（以及每日刷新）拉取每个候选标的的日线，
    计算"日线级趋势偏置 + 波动率"，供分钟级策略做：
      1) 入场闸门：只在日线主升（close>MA20 且 MA20 斜率>0 且 close>MA60）时才允许买入；
      2) 波动率目标仓位（ATR）：用日线 ATR% 做风险平价，替换原来"无脑 5 万"。

数据优先级：xtdata.get_market_data_ex('1d')（本地 miniQMT，零额度）> tushare pro.daily。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from config.settings import INDEX_CODES, MARKET_INDEX_CODE
from core import indicators as I
from core.qmt_client import qmt_client
from data.tushare_client import tushare_client

logger = logging.getLogger(__name__)

# 日线回看窗口（足够算 MA60 + 斜率 + ATR）
DAILY_COUNT = 120

# 特征新鲜度：同一交易日内重复 refresh 时，未过期的标的跳过网络拉取。
# 让「动态候选池刷新后追加拉取新代码」这种增量刷新几乎零成本。
FEATURE_TTL_SEC = 6 * 3600


@dataclass
class DailyFeatures:
    code: str
    close: float = 0.0
    ma5: float = 0.0
    ma10: float = 0.0
    ma20: float = 0.0
    ma60: float = 0.0
    ma20_slope: float = 0.0      # 每根日线绝对变化量（近似斜率）
    macd_hist: float = 0.0
    macd_dif: float = 0.0
    macd_dea: float = 0.0
    atr_pct: float = 0.0         # ATR / close，用于风险平价
    rsi: float = 0.0
    boll_mid: float = 0.0
    boll_upper: float = 0.0
    boll_lower: float = 0.0
    vwap: float = 0.0
    vol_avg5: float = 0.0
    above_ma20: bool = False
    above_ma60: bool = False
    bias: float = 0.0            # -1 ~ +1 综合趋势偏置
    trend_up: bool = False       # 日线主升（可入场）
    # 【2026-09-02 #E】日级 6 因子评分（与 backtest_daily.score_daily 严格一致）。
    # 这样 EventEngine 可直接用 features.score 作为 BUY 判定，**与回测同口径**——
    # 解决了“分钟级回测实测负收益、live 路径从未被验证”这件事。
    score: float = 0.0
    factors: dict = field(default_factory=dict)
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        d = {
            "code": self.code, "close": round(self.close, 3),
            "ma20": round(self.ma20, 3), "ma60": round(self.ma60, 3),
            "ma20_slope": round(self.ma20_slope, 4),
            "macd_hist": round(self.macd_hist, 4),
            "atr_pct": round(self.atr_pct, 4), "rsi": round(self.rsi, 2),
            "above_ma20": self.above_ma20, "above_ma60": self.above_ma60,
            "bias": round(self.bias, 3), "trend_up": self.trend_up,
            "score": round(self.score, 2),
        }
        if self.factors:
            d["factors"] = {k: round(v, 2) for k, v in self.factors.items()}
        return d


class DailyContext:
    """线程安全的日线特征缓存。"""

    def __init__(self, codes: Optional[List[str]] = None,
                 index_code: str = MARKET_INDEX_CODE):
        self._codes = list(codes or [])
        self._index_code = index_code
        self._feats: Dict[str, DailyFeatures] = {}
        # 额外缓存原始收盘价序列（足够算任意周期 MA + 动量），
        # 供趋势破位判定与动量排名使用。
        self._closes: Dict[str, List[float]] = {}
        self._lock = threading.RLock()
        self._last_refresh: float = 0
        self._last_error: str = ""

    # ---------------------------------------------------------- 数据获取

    def _fetch_daily(self, code: str, count: int = DAILY_COUNT
                     ) -> Optional[Dict[str, List[float]]]:
        """返回 {open,high,low,close,volume} 列表，或 None。"""
        # 1) xtdata 本地（零额度、实时）
        try:
            raw = qmt_client.get_history(code, period="1d", count=count)
            if raw and len(raw) >= 60:
                return {
                    "open": [b["open"] for b in raw],
                    "high": [b["high"] for b in raw],
                    "low": [b["low"] for b in raw],
                    "close": [b["close"] for b in raw],
                    "volume": [b["volume"] for b in raw],
                }
        except Exception as e:
            logger.debug("DailyContext xtdata 失败 %s: %s", code, e)
        # 2) tushare 兜底
        if tushare_client.enabled:
            try:
                from data.tushare_client import _to_ts_code
                df = tushare_client._pro.daily(  # type: ignore
                    ts_code=_to_ts_code(code),
                    start_date=(datetime.now()
                                - timedelta(days=count * 2)).strftime("%Y%m%d"),
                    end_date=datetime.now().strftime("%Y%m%d"),
                    fields="open,high,low,close,vol",
                )
                if df is not None and not df.empty:
                    df = df.sort_values("trade_date")
                    return {
                        "open": df["open"].tolist(),
                        "high": df["high"].tolist(),
                        "low": df["low"].tolist(),
                        "close": df["close"].tolist(),
                        "volume": df["vol"].tolist(),
                    }
            except Exception as e:
                logger.debug("DailyContext tushare 失败 %s: %s", code, e)
        return None

    # ---------------------------------------------------------- 特征计算

    def _compute(self, code: str, d: Dict[str, List[float]]) -> DailyFeatures:
        # 主要特征计算由下方函数体提供。

        closes = d["close"]
        highs = d["high"]
        lows = d["low"]
        vols = d["volume"]
        # 与 backtest_daily.score_daily 严格对齐的指标集
        ma5 = I.last(I.sma(closes, 5)) or 0.0
        ma10 = I.last(I.sma(closes, 10)) or 0.0
        ma20 = I.last(I.sma(closes, 20)) or 0.0
        ma60 = I.last(I.sma(closes, 60)) or 0.0
        ma20_series = I.sma(closes, 20)
        ma20_slope = I.slope(ma20_series, lookback=5) or 0.0
        macd_dif, macd_dea, hist = I.macd(closes, 12, 26, 9)
        macd_hist = I.last(hist) or 0.0
        macd_dif_v = I.last(macd_dif) or 0.0
        macd_dea_v = I.last(macd_dea) or 0.0
        boll_mid, boll_up, boll_lo = I.boll(closes, 20, 2.0)
        boll_mid_v = I.last(boll_mid) or 0.0
        boll_up_v = I.last(boll_up) or 0.0
        boll_lo_v = I.last(boll_lo) or 0.0
        atr = I.last(I.atr(highs, lows, closes, 14)) or 0.0
        rsi = I.last(I.rsi(closes, 14)) or 0.0
        typ = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
        vwap = I.last(I.vwap(typ, vols)) or 0.0
        vol_avg5 = (sum(vols[-6:-1]) / 5) if len(vols) >= 6 else 0.0
        close = closes[-1]
        above_ma20 = bool(ma20 and close > ma20)
        above_ma60 = bool(ma60 and close > ma60)
        atr_pct = (atr / close) if close > 0 else 0.0

        # 综合偏置（-1 ~ +1）
        bias = 0.0
        if ma20:
            bias += 0.35 * (1 if above_ma20 else -1)
        if ma60:
            bias += 0.35 * (1 if above_ma60 else -1)
        if ma20 and ma60:
            bias += 0.30 * (1 if ma20 > ma60 else -1)
        bias = max(-1.0, min(1.0, bias))

        trend_up = (above_ma20 and above_ma60 and ma20_slope > 0
                    and macd_hist >= 0)

        # === 6 因子评分（与 backtest_daily.score_daily 严格一致）===
        # 这是 (a) 路线的核心：让 live 直接复用**已验证**的日线评分逻辑。
        factors: Dict[str, float] = {}
        # 1) trend (0~2)
        tr = 0.0
        if ma5 and ma10 and ma20:
            if ma5 > ma10 > ma20:
                tr += 1.0
                if ma20_slope > 0:
                    tr += 1.0
            elif ma5 > ma10:
                tr += 0.5
        factors["trend"] = round(tr, 2)
        # 2) momentum (0~1，仅 macd_hist > 0；与 backtest 一致)
        mm = 0.0
        if macd_hist is not None and macd_hist > 0:
            mm += 1.0
        factors["momentum"] = round(mm, 2)
        # 3) oversold (0~2)
        ob = 0.0
        if rsi is not None:
            if 30 < rsi < 70:
                ob += 1.0
            elif rsi <= 30:
                ob += 1.5
            elif rsi >= 80:
                ob -= 0.5
        # KDJ 暂未计算（与 backtest 一致的话需要 series；先按 backtest 简化为只 RSI）
        factors["oversold"] = round(max(0.0, min(2.0, ob)), 2)
        # 4) volume (0~1)
        vp = 0.0
        if vols and vol_avg5 > 0 and vols[-1] > 0:
            ratio = vols[-1] / vol_avg5
            if ratio >= 1.2:
                vp = 1.0
            elif ratio >= 1.0:
                vp = 0.5
        factors["volume"] = round(vp, 2)
        # 5) position (0~1)
        pos = 0.0
        if close and boll_mid_v and boll_up_v and boll_lo_v:
            if boll_lo_v <= close <= boll_up_v:
                pos = (0.5 + 0.5 * (close - boll_mid_v) /
                       (boll_up_v - boll_mid_v)
                       if boll_up_v > boll_mid_v else 0.5)
            elif close > boll_up_v:
                pos = 0.0 if close > boll_up_v * 1.02 else 0.5
            elif close < boll_lo_v:
                pos = 0.3 if close > boll_lo_v * 0.98 else 0.0
        factors["position"] = round(pos, 2)
        # 6) vwap (0~2)
        vw = 0.0
        if close and vwap:
            diff = (close - vwap) / vwap
            if diff > 0.0008:
                vw = 2.0
            elif diff > -0.004:
                vw = 1.0
        factors["vwap"] = round(vw, 2)

        score = round(sum(factors.values()), 2)

        return DailyFeatures(
            code=code, close=close, ma5=ma5, ma10=ma10, ma20=ma20, ma60=ma60,
            ma20_slope=ma20_slope,
            macd_hist=macd_hist, macd_dif=macd_dif_v, macd_dea=macd_dea_v,
            atr_pct=atr_pct, rsi=rsi,
            boll_mid=boll_mid_v, boll_upper=boll_up_v, boll_lower=boll_lo_v,
            vwap=vwap, vol_avg5=vol_avg5,
            above_ma20=above_ma20, above_ma60=above_ma60, bias=bias,
            trend_up=trend_up, score=score, factors=factors,
            updated_at=time.time(),
        )

    @staticmethod
    def _compute_for_test(code: str, d: Dict[str, List[float]]) -> "DailyFeatures":
        """静态版：供 backtest_minute 在不同时间切片上复用 _compute 逻辑。

        backtest_minute 每日调一次，对每个 code 截到该日之前的 bar 重算
        score。与 DailyContext 实例上的 _compute 输出严格一致。
        """
        tmp = DailyContext(codes=[code])
        return tmp._compute(code, d)

    # ---------------------------------------------------------- 刷新

    def refresh(self, codes: Optional[List[str]] = None,
                force: bool = False) -> int:
        """拉取并刷新日线特征。返回成功刷新（或已有新鲜缓存）的标的数。

        :param codes:  本次要覆盖的标的。传入后会**并入** ``self._codes``，
                       使后续无参 refresh 也记得动态候选池（P0 修正：原实现
                       只认 ``__init__`` 时传入的静态池，动态池永远拿不到日线，
                       日线闸门 / ATR 风险平价 / 动量排名对其全部失效）。
        :param force:  True = 无条件重拉；False = 跳过 FEATURE_TTL_SEC 内已刷新的标的
                       （原实现 force 参数是死参数，从未被使用）。
        """
        if codes is not None:
            with self._lock:
                merged = list(dict.fromkeys(list(self._codes) + list(codes)))
                self._codes = merged
            target = list(codes)
        else:
            with self._lock:
                target = list(self._codes)
        if self._index_code and self._index_code not in target:
            target.append(self._index_code)
        if not target:
            return 0
        now = time.time()
        ok = 0
        fetched = 0
        for code in target:
            try:
                if not force:
                    with self._lock:
                        cached = self._feats.get(code)
                    if cached and (now - cached.updated_at) < FEATURE_TTL_SEC:
                        ok += 1
                        continue          # 缓存仍新鲜，免一次网络往返
                d = self._fetch_daily(code)
                fetched += 1
                if not d or len(d["close"]) < 60:
                    self._last_error = f"{code} 日线不足"
                    continue
                f = self._compute(code, d)
                with self._lock:
                    self._feats[code] = f
                    self._closes[code] = list(d["close"])
                ok += 1
            except Exception as e:
                self._last_error = f"{code}: {e}"
                logger.debug("DailyContext refresh err %s: %s", code, e)
        with self._lock:
            self._last_refresh = time.time()
            total_known = len(self._feats)
        logger.info("DailyContext 刷新完成: %d/%d（本次网络拉取 %d，累计覆盖 %d 只）",
                    ok, len(target), fetched, total_known)
        return ok

    # ---------------------------------------------------------- 就绪状态

    def is_ready(self) -> bool:
        """是否已至少成功刷新过一次（且拿到了特征）。

        供策略层区分两种「查不到日线特征」的语义：
          - 未就绪（warmup / 启动瞬间）→ 放行，不冻结引擎；
          - 已就绪但查不到该标的  → **该标的确实没有日线数据** → 应拒绝入场。
        """
        with self._lock:
            return self._last_refresh > 0 and bool(self._feats)

    def has(self, code: str) -> bool:
        """是否已缓存该标的的日线特征。"""
        with self._lock:
            return code in self._feats

    def covered_codes(self) -> List[str]:
        """已拿到日线特征的标的列表（供动量排名等只在有数据的集合上工作）。"""
        with self._lock:
            return list(self._feats.keys())

    # ---------------------------------------------------------- 查询

    def features(self, code: str) -> Optional[DailyFeatures]:
        with self._lock:
            return self._feats.get(code)

    def bias(self, code: str) -> float:
        with self._lock:
            f = self._feats.get(code)
        return f.bias if f else 0.0

    def trend_up(self, code: str) -> bool:
        with self._lock:
            f = self._feats.get(code)
        return f.trend_up if f else False

    def atr_pct(self, code: str) -> float:
        with self._lock:
            f = self._feats.get(code)
        return f.atr_pct if f else 0.0

    def trend_broken(self, code: str, exit_ma: int = 60) -> bool:
        """趋势破位判定（趋势骑行退出用）。

        当 MA20 下穿 exit_ma(默认 MA60)，或收盘价跌破 exit_ma，
        视为中期趋势反转，应离场。等价于"骑行至趋势破位才下车"。
        """
        with self._lock:
            c = self._closes.get(code)
        if not c or len(c) < exit_ma:
            return False
        ma20 = I.last(I.sma(c, 20))
        ma_exit = I.last(I.sma(c, exit_ma))
        close = c[-1]
        if ma20 is None or ma_exit is None or close <= 0:
            return False
        return (ma20 < ma_exit) or (close < ma_exit)

    def momentum_60d(self, code: str, lookback: int = 60) -> float:
        """60 日动量（区间收益率），用于动量排名。无数据返回 0。"""
        with self._lock:
            c = self._closes.get(code)
        if not c or len(c) < lookback + 1:
            return 0.0
        base = c[-(lookback + 1)]
        if base <= 0:
            return 0.0
        return c[-1] / base - 1.0

    def top_momentum(self, codes: List[str], top_n: int = 6,
                     lookback: int = 60) -> List[str]:
        """返回动量前 N 名的代码（用于只交易最强趋势，规避死水股）。"""
        scored = []
        for code in codes:
            m = self.momentum_60d(code, lookback)
            if m > 0:   # 只保留正动量，避免在下跌股里"矮子里拔将军"
                scored.append((m, code))
        scored.sort(reverse=True)
        return [code for _, code in scored[:top_n]]

    def market_regime(self) -> str:
        """基于指数日线：up / down / neutral。"""
        f = self.features(self._index_code)
        if f is None:
            return "unknown"
        if f.trend_up:
            return "up"
        if not f.above_ma20 and f.ma20_slope <= 0:
            return "down"
        return "neutral"

    def index_above_ma(self, code: str = None, ma: int = 60) -> bool:
        """指数收于 MA(ma) 之上 → True。

        用于 regime 入场闸门与强制清仓（与回测引擎 regime_ok 一致：
        close > MA60 即视为市场状态健康）。
        无数据 / 数据不足时返回 True（保守放行，避免 warmup 阶段冻结引擎）。
        """
        code = code or self._index_code
        with self._lock:
            c = self._closes.get(code)
        if not c or len(c) < ma:
            return True
        ma_v = I.last(I.sma(c, ma))
        if ma_v is None or ma_v <= 0:
            return True
        return c[-1] > ma_v

    def breadth_above_ma(self, ma: int = 60, thresh: float = 0.5) -> bool:
        """宽度门：缓存个股中收盘 > 各自 MA(ma) 的比例 >= thresh → True。

        比单一指数更稳健，避免单指数失真。无数据 / 无可评估标的时返回 True。
        """
        with self._lock:
            closes = dict(self._closes)
        if not closes:
            return True
        above = 0
        total = 0
        for code, c in closes.items():
            if code in INDEX_CODES or len(c) < ma:
                continue
            total += 1
            ma_v = I.last(I.sma(c, ma))
            if ma_v and ma_v > 0 and c[-1] > ma_v:
                above += 1
        if total == 0:
            return True
        return (above / total) >= thresh

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "index": self._index_code,
                "regime": self.market_regime(),
                "n": len(self._feats),
                "last_refresh": self._last_refresh,
                "features": {c: f.to_dict() for c, f in self._feats.items()},
                "last_error": self._last_error,
            }

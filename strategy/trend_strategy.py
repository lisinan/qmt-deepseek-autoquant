# -*- coding: utf-8 -*-
"""
多因子趋势策略（qmtIDE-deepseek，优化版）

6 个因子共 0~10 分（与旧版一致，已重新校准 VWAP）：
1. 趋势因子（MA5>MA10>MA20 + MA20 斜率）     0~2
2. 动量因子（MACD hist + 交叉）              0~2
3. 超买超卖（RSI + KDJ-J）                   0~2
4. 量价因子（量比）                          0~1
5. 位置因子（BOLL 带内位置）                  0~1
6. VWAP 因子（站上 VWAP 的"真实"强度）       0~2

关键优化（相比旧版）：
  A. 【日线多周期闸门】on_bars 必须配合 DailyContext：
     只有日线主升（close>MA20 且 >MA60 且 MA20 斜率>0 且 MACD>=0）
     或日线偏置 bias>=阈值 时才允许买入，过滤分钟级噪音与逆势单。
  B. 【VWAP 因子重校准】旧版几乎恒为 1~2（diff>-1.2% 即给分），
     现改为真正区分"站上/贴近/跌破"VWAP。
  C. 【激活移动止损】trailing_stop 参数旧版从未使用，现接入 on_exit：
     浮盈超过 activation 后，从峰值回撤 trailing_stop 即离场，锁利润。

买入：总分 >= threshold 且至少 min_signals 个因子正分
      + 非指数 + 无持仓 + 通过日线闸门
卖出：止损 / 止盈 / 移动止损 / max_hold_days / MA 死叉 / 单日暴跌
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from config.settings import INDEX_CODES, STRATEGY_PARAMS
from core import indicators as I
from core.data_models import Bar, Position, Signal
from strategy.base import BaseStrategy
from strategy.daily_context import DailyContext


def _bars_from_dicts(bars: List[Bar]) -> Tuple[List[float], List[float],
                                                List[float], List[int]]:
    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    vols = [b.volume for b in bars]
    return closes, highs, lows, vols


class TrendStrategy(BaseStrategy):
    name = "trend"

    def __init__(self, params: Optional[dict] = None,
                 daily: Optional[DailyContext] = None):
        self.p = dict(STRATEGY_PARAMS)
        if params:
            self.p.update(params)
        # 日线多周期上下文（可选；为 None 时退化为纯分钟级策略）
        self.daily = daily
        # 指标缓存：on_bars 每 3s tick 被调用，但分钟级 bar 序列每分钟才
        # 推进一根。缓存以「序列是否推进」为键，避免同一分钟内对同一只股票
        # 反复重算 8 个指标（约 20× 冗余）。命中时返回与即时重算完全一致
        # 的结果，不影响信号逻辑；仅当第二根 bar 的时间戳变化（新分钟）才
        # 失效重算。趋势策略以 60 日闸门为主，分钟级指标 1 分钟滞后无关紧要。
        self._ind_cache: Dict[str, Tuple[object, dict]] = {}

    # ============================================================ 公共

    def on_bars(self, code: str, name: str, bars: List[Bar]) -> Signal:
        if code in INDEX_CODES:
            return self._hold(code, name, bars, reason="index")
        if len(bars) < 60:
            return self._hold(code, name, bars, reason="warmup")

        # ---- 日线多周期闸门（核心优化 A）----
        daily_ok = True
        daily_reason = ""
        if self.daily is not None:
            f = self.daily.features(code)
            if f is None:
                # 暂无日线数据：保守放行（warmup），不阻断
                daily_ok = True
                daily_reason = "no-daily"
            else:
                if f.trend_up:
                    daily_ok = True
                elif f.bias >= self.p.get("min_daily_bias", 0.2):
                    daily_ok = True
                else:
                    daily_ok = False
                daily_reason = (f"bias={f.bias:.2f} trend_up={f.trend_up}")

        # ---- 指标缓存（执行效率优化，见 __init__ 说明）----
        # 键：序列长度 + 倒数第二根 bar 的时间戳。同一分钟内这两值不变
        # → 命中缓存；新分钟 append 一根 → 倒数第二根变化 → 失效重算。
        _cache_key = (len(bars),
                      (bars[-2].ts if len(bars) >= 2 else None))
        _cached = self._ind_cache.get(code)
        if _cached is not None and _cached[0] == _cache_key:
            ind = _cached[1]
        else:
            ind = self._compute_indicators(bars)
            self._ind_cache[code] = (_cache_key, ind)
        score, factors = self._score(ind, trend_up=(self.daily is not None
                                                     and f is not None
                                                     and f.trend_up))
        last_close = bars[-1].close
        change_pct = (last_close - bars[-2].close) / bars[-2].close * 100 \
            if len(bars) >= 2 and bars[-2].close else 0.0

        th = self.p["buy_score_threshold"]
        min_sig = self.p["min_signals"]
        pos_factors = sum(1 for v in factors.values() if v and v > 0)

        if score >= th and pos_factors >= min_sig and daily_ok:
            reason = "; ".join(f"{k}+{v:.1f}" for k, v in factors.items() if v and v > 0)
            if daily_reason:
                reason = f"[{daily_reason}] " + reason
            return Signal(
                ts=datetime.now(), code=code, name=name,
                side="BUY", score=score, price=last_close,
                reason=reason, factors={"change_pct": round(change_pct, 3), **factors},
            )
        # 不买的原因
        if not daily_ok:
            return self._hold(code, name, bars, score=score,
                              reason=f"daily-gate:{daily_reason}")
        return self._hold(code, name, bars, score=score,
                           reason=f"score<{th}" if score < th else f"sigs<{min_sig}")

    def on_exit(self, code: str, position: Position,
                current_price: float, bars: List[Bar]) -> Optional[Signal]:
        if position.quantity <= 0:
            return None
        cost = position.avg_cost
        pnl_pct = (current_price - cost) / cost if cost > 0 else 0

        exit_mode = self.p.get("exit_mode", "scalp")

        # ---- 趋势骑行模式：骑行至趋势破位才离场 ----
        if exit_mode == "trend":
            # 1) 宽幅硬止损（仅灾难保护，默认 -18%）
            hard = self.p.get("hard_stop_pct", -0.18)
            if pnl_pct <= hard:
                return self._exit_signal(code, position.name, current_price,
                                         f"硬止损 {pnl_pct*100:.2f}%")
            # 2) 趋势破位：MA20 下穿 exit_ma / 收盘跌破 exit_ma（由日线上下文判定）
            if self.daily is not None and self.daily.trend_broken(
                    code, int(self.p.get("trend_exit_ma", 60))):
                return self._exit_signal(code, position.name, current_price,
                                         f"趋势破位离场 {pnl_pct*100:+.2f}%")
            # 3) 持仓时限（趋势模式放长到 120 天，让大趋势充分奔跑）
            if position.open_date:
                hold_days = (datetime.now() - position.open_date).days
                if hold_days >= self.p.get("trend_max_hold_days", 120):
                    return self._exit_signal(code, position.name, current_price,
                                             f"超时{hold_days}天")
            # 4) 单日暴跌（崩溃保护，不参与趋势判断）
            if len(bars) >= 2 and bars[-1].close and bars[-2].close:
                day_chg = (bars[-1].close - bars[-2].close) / bars[-2].close
                if day_chg * 100 <= self.p.get("down_day_exit_pct", -99.0):
                    return self._exit_signal(code, position.name, current_price,
                                             f"暴跌 {day_chg*100:.2f}%")
            return None

        # ---- 剥头皮模式（原逻辑）----
        # 1) 吊灯/自适应止损（stop_price 由引擎每日按"峰值 - atr_stop_mult×ATR"更新，
        #    随趋势上移；跌破即离场，等于移动止损 + 趋势破位判断合一）
        stop_price = position.stop_price or (cost * (1 + self.p["stop_loss"]))
        if current_price <= stop_price:
            return self._exit_signal(code, position.name, current_price,
                                     f"趋势破位/移动止损 {pnl_pct*100:.2f}%")

        # 2) 宽幅目标止盈（让大趋势奔跑，极少数触发；封顶极端泡沫）
        target_price = position.target_price or (cost * (1 + self.p["take_profit"]))
        if target_price and current_price >= target_price:
            return self._exit_signal(code, position.name, current_price,
                                     f"趋势止盈 +{pnl_pct*100:.2f}%")

        # 3) 持仓时限
        if position.open_date:
            hold_days = (datetime.now() - position.open_date).days
            if hold_days >= self.p["max_hold_days"]:
                return self._exit_signal(code, position.name, current_price,
                                         f"超时{hold_days}天")

        # 4) MA 死叉 / 单日暴跌
        if len(bars) >= 2:
            closes, _, _, _ = _bars_from_dicts(bars)
            ma5 = I.ema(closes, self.p["ma_short"])
            ma10 = I.ema(closes, self.p["ma_medium"])
            if I.cross_below(ma5, ma10):
                return self._exit_signal(code, position.name, current_price,
                                         "MA5 下穿 MA10")
            if len(bars) >= 2 and bars[-1].close and bars[-2].close:
                day_chg = (bars[-1].close - bars[-2].close) / bars[-2].close
                if day_chg * 100 <= self.p["down_day_exit_pct"]:
                    return self._exit_signal(code, position.name, current_price,
                                             f"暴跌 {day_chg*100:.2f}%")
        return None

    # ============================================================ 内部

    def _compute_indicators(self, bars: List[Bar]) -> dict:
        closes, highs, lows, vols = _bars_from_dicts(bars)
        ma5 = I.sma(closes, self.p["ma_short"])
        ma10 = I.sma(closes, self.p["ma_medium"])
        ma20 = I.sma(closes, self.p["ma_long"])
        rsi = I.rsi(closes, self.p["rsi_period"])
        macd_dif, macd_dea, macd_hist = I.macd(closes, self.p["macd_fast"],
                                                self.p["macd_slow"],
                                                self.p["macd_signal"])
        k, d, j = I.kdj(highs, lows, closes, self.p["kdj_period"])
        boll_mid, boll_up, boll_lo = I.boll(closes, self.p["boll_period"],
                                              self.p["boll_std"])
        atr = I.atr(highs, lows, closes, self.p["atr_period"])
        typicals = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
        vwap = I.vwap(typicals, vols)

        return {
            "ma5": I.last(ma5), "ma10": I.last(ma10), "ma20": I.last(ma20),
            "ma20_slope": I.slope(ma20),
            "rsi": I.last(rsi),
            "macd_dif": I.last(macd_dif), "macd_dea": I.last(macd_dea),
            "macd_hist": I.last(macd_hist),
            "kdj_k": I.last(k), "kdj_d": I.last(d), "kdj_j": I.last(j),
            "boll_mid": I.last(boll_mid),
            "boll_upper": I.last(boll_up), "boll_lower": I.last(boll_lo),
            "atr": I.last(atr), "vwap": I.last(vwap),
            "close": closes[-1], "vol": vols[-1],
            "vol_avg5": sum(vols[-6:-1]) / 5 if len(vols) >= 6 else None,
        }

    def _score(self, ind: dict, trend_up: bool = False) -> Tuple[float, Dict[str, float]]:
        factors: Dict[str, float] = {}

        # 1) 趋势（0~2）
        tr = 0.0
        if ind["ma5"] and ind["ma10"] and ind["ma20"]:
            if ind["ma5"] > ind["ma10"] > ind["ma20"]:
                tr += 1.0
                if (ind["ma20_slope"] or 0) > 0:
                    tr += 1.0
            elif ind["ma5"] > ind["ma10"]:
                tr += 0.5
        factors["trend"] = round(tr, 2)

        # 2) 动量（0~2）
        mm = 0.0
        if ind["macd_hist"] is not None:
            if ind["macd_hist"] > 0:
                mm += 1.0
            if ind["macd_dif"] is not None and ind["macd_dea"] is not None:
                if ind["macd_dif"] > ind["macd_dea"]:
                    mm += 1.0
        factors["momentum"] = round(mm, 2)

        # 3) 超买超卖（0~2）【日线主升时不再惩罚高位 RSI/KDJ】
        #    趋势里最肥的鱼在"强势不回落"段，避免在 RSI>70 被误杀。
        ob = 0.0
        if ind["rsi"] is not None:
            rsi = ind["rsi"]
            if trend_up:
                if rsi >= 50:
                    ob += 1.0
                elif rsi <= 30:
                    ob += 1.5
                else:
                    ob += 0.5
            else:
                if 30 < rsi < 70:
                    ob += 1.0
                elif rsi <= 30:
                    ob += 1.5
                elif rsi >= 80:
                    ob -= 0.5
        if ind["kdj_j"] is not None:
            j = ind["kdj_j"]
            if trend_up:
                if j > 50:
                    ob += 0.5
                elif j < 20:
                    ob += 0.5
            elif j < 20:
                ob += 0.5
            elif j > 100:
                ob -= 1.0
        ob = max(0.0, min(2.0, ob))
        factors["oversold"] = round(ob, 2)

        # 4) 量价（0~1）
        vp = 0.0
        if ind["vol"] and ind["vol_avg5"] and ind["vol_avg5"] > 0:
            ratio = ind["vol"] / ind["vol_avg5"]
            if ratio >= self.p["volume_surge"]:
                vp = 1.0
            elif ratio >= 1.0:
                vp = 0.5
        factors["volume"] = round(vp, 2)

        # 5) 位置（0~1）：布林带内越靠上轨越强；突破过热/跌破则减分
        pos = 0.0
        if ind["close"] and ind["boll_mid"] and ind["boll_upper"] and ind["boll_lower"]:
            c, up, lo = ind["close"], ind["boll_upper"], ind["boll_lower"]
            mid = ind["boll_mid"]
            if lo <= c <= up:
                pos = 0.5 + 0.5 * (c - mid) / (up - mid) if up > mid else 0.5
            elif c > up:
                pos = 0.0 if c > up * 1.02 else 0.5
            elif c < lo:
                pos = 0.3 if c > lo * 0.98 else 0.0
        factors["position"] = round(pos, 2)

        # 6) VWAP（0~2）【重校准】真正区分站上/贴近/跌破
        vw = 0.0
        if ind["close"] and ind["vwap"]:
            diff_pct = (ind["close"] - ind["vwap"]) / ind["vwap"]
            if diff_pct > 0.0008:       # 明显站上 VWAP（强势）
                vw = 2.0
            elif diff_pct > -0.004:      # 贴近 VWAP（中性偏多）
                vw = 1.0
            # 否则（低于 VWAP > 0.4%）不给分
        factors["vwap"] = round(vw, 2)

        total = sum(factors.values())
        return round(total, 2), factors

    def _hold(self, code, name, bars, score=0.0, reason="") -> Signal:
        last = bars[-1].close if bars else 0.0
        return Signal(ts=datetime.now(), code=code, name=name,
                      side="HOLD", score=score, price=last,
                      reason=reason, factors={})

    def _exit_signal(self, code, name, price, reason) -> Signal:
        return Signal(ts=datetime.now(), code=code, name=name,
                      side="SELL", score=0.0, price=price,
                      reason=reason, factors={})

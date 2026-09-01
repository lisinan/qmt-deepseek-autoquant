# -*- coding: utf-8 -*-
"""
研究级组合回测引擎（日期对齐 / 指标预计算 / 可插拔模块）

为什么重写而不是改旧的 backtest_daily.py
----------------------------------------
旧引擎有两个致命问题：
  1. **日期错位**：用数组位置 i 跨标的取数，而各标的本地历史起点差
     19 个月，i=100 对 A 是 2022-10、对 B 是 2024-05。横截面动量排名
     和组合权益曲线因此全部无效。
  2. **O(n^2) 重算**：循环里反复 `I.sma(c[:i+1], 20)`，1855 天 × 多标的
     直接卡死，无法做参数扫描。

本引擎：
  - 统一交易日轴（data.hist_cache.AlignedPanel），每个标的记录自己的
    有效日下标映射，未上市/停牌自动跳过；
  - 每个标的的指标一次性预计算成数组，回测循环只做 O(1) 查表；
  - 信号在 T 日收盘产生，T+1 开盘成交（含滑点），无未来函数；
  - 模块可插拔：入场 / 退出 / 仓位 / 指数 regime / 动量轮动。

用法
----
    python strategy/research_lab.py --suite core
    python strategy/research_lab.py --suite ablation
    python strategy/research_lab.py --suite walkforward
"""
from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import indicators as I                       # noqa: E402
from data.hist_cache import AlignedPanel, load_panel   # noqa: E402


# ============================================================ 配置

@dataclass
class LabConfig:
    name: str = "cfg"

    # ---- 组合 ----
    max_positions: int = 6
    cash_buffer_pct: float = 0.02      # 预留现金比例

    # ---- 入场 ----
    # "factor"   = 6 因子评分（原策略语义）
    # "breakout" = 唐奇安通道突破（N 日新高）
    # "trend"    = 日线主升 + 逼近 20 日新高
    entry_mode: str = "factor"
    buy_score_threshold: float = 4.0
    min_signals: int = 3
    use_gate: bool = True              # 日线趋势闸门 close>MA20>MA60 且斜率>0
    breakout_lookback: int = 55        # 唐奇安突破回看
    vwap_mode: str = "cumulative"      # "cumulative"(原) | "rolling"(20日)

    # ---- 横截面动量 ----
    momentum_rank: bool = False
    momentum_top_n: int = 6
    momentum_lookback: int = 60
    momentum_min: float = 0.0          # 动量下限（低于则不买）

    # ---- 指数 regime 过滤 ----
    regime_filter: bool = False
    regime_ma: int = 60                # 指数收盘 > MA(N) 才允许新开仓
    regime_exit: bool = False          # regime 转弱时清空持仓
    regime_slope_days: int = 0         # >0 时额外要求指数 MA 斜率为正

    # ---- 退出 ----
    # "scalp"      = 固定止损/止盈 + 紧移动止损（原策略）
    # "trend"      = 骑行至均线破位 + 宽幅硬止损
    # "chandelier" = 吊灯止损（峰值 - k*ATR，随峰值上移）
    exit_mode: str = "trend"
    stop_loss: float = -0.04
    take_profit: float = 0.12
    atr_stop_mult: float = 2.5
    tp_atr_mult: float = 4.0
    trailing: bool = True
    trailing_activation: float = 0.06
    trailing_stop: float = -0.03
    trailing_floor: float = -0.005
    trend_exit_ma: int = 60
    hard_stop_pct: float = -0.18
    chandelier_mult: float = 3.0
    max_hold_days: int = 120
    time_stop_days: int = 0            # >0：持仓 N 天仍未盈利则退出（去死钱）
    down_day_exit_pct: float = -99.0   # 单日暴跌清仓（-99 关闭）

    # ---- 仓位 ----
    # "risk"      = 单笔风险预算 / 止损距离
    # "equal"     = 等权 equity / max_positions
    # "voltarget" = 目标波动率：仓位 ∝ target_vol / 标的年化波动
    sizing: str = "risk"
    risk_per_trade: float = 0.02
    target_vol: float = 0.35
    max_pos_pct: float = 0.30          # 单标的占总资产上限（比例，规模无关）
    min_pos_pct: float = 0.02

    # ---- 轮动 ----
    rotate: bool = False
    rotate_edge: float = 0.20          # 新候选动量需超过最弱持仓该幅度才换

    # ---- 成本 ----
    cost_pct: float = 0.0010           # 佣金+印花税（单边）
    slippage_pct: float = 0.0008       # 滑点（单边）

    # ---- 其他 ----
    allow_short: bool = False
    warmup: int = 130                  # 需要 MA120/动量的预热天数


# ============================================================ 单标的预计算

class StockSeries:
    """把某标的在面板上的有效日抽成紧凑序列，并一次性算好全部指标。"""

    __slots__ = ("code", "pidx", "p2c", "o", "h", "l", "c", "v",
                 "ma5", "ma10", "ma20", "ma60", "ma120", "ma_exit",
                 "rsi", "macd_hist", "kdj_j", "boll_mid", "boll_up",
                 "boll_lo", "atr", "atrp", "vwap", "ma20_slope",
                 "hh", "n")

    def __init__(self, panel: AlignedPanel, code: str, cfg: LabConfig):
        self.code = code
        cl = panel.close[code]
        self.pidx: List[int] = [k for k, x in enumerate(cl) if x is not None]
        self.p2c: Dict[int, int] = {p: j for j, p in enumerate(self.pidx)}
        g = lambda f: [getattr(panel, f)[code][k] for k in self.pidx]  # noqa: E731
        self.o = g("open")
        self.h = g("high")
        self.l = g("low")
        self.c = g("close")
        self.v = [x or 0 for x in g("volume")]
        self.n = len(self.c)

        c, h, l, v = self.c, self.h, self.l, self.v
        self.ma5 = I.sma(c, 5)
        self.ma10 = I.sma(c, 10)
        self.ma20 = I.sma(c, 20)
        self.ma60 = I.sma(c, 60)
        self.ma120 = I.sma(c, 120)
        self.ma_exit = (self.ma60 if cfg.trend_exit_ma == 60 else
                        I.sma(c, cfg.trend_exit_ma))
        self.rsi = I.rsi(c, 14)
        _, _, self.macd_hist = I.macd(c, 12, 26, 9)
        _k, _d, self.kdj_j = I.kdj(h, l, c, 9)
        self.boll_mid, self.boll_up, self.boll_lo = I.boll(c, 20, 2.0)
        self.atr = I.atr(h, l, c, 14)
        self.atrp = [(a / c[i]) if (a and c[i]) else None
                     for i, a in enumerate(self.atr)]
        # VWAP
        typ = [(h[i] + l[i] + c[i]) / 3 for i in range(self.n)]
        if cfg.vwap_mode == "rolling":
            self.vwap = _rolling_vwap(typ, v, 20)
        else:
            self.vwap = I.vwap(typ, v)
        # MA20 斜率数组（等价 I.slope(ma20, 5)，但 O(n)）
        self.ma20_slope = _slope_array(self.ma20, 5)
        # 滚动最高收盘（突破用）
        self.hh = _rolling_max(c, cfg.breakout_lookback)

    # -------- 查询（j 为紧凑下标） --------
    def j_at(self, k: int) -> Optional[int]:
        """面板下标 k → 紧凑下标（该标的当日无数据返回 None）。"""
        return self.p2c.get(k)

    def gate_ok(self, j: int) -> bool:
        """日线趋势闸门：close > MA20 > MA60 且 MA20 斜率>0 且 MACD>=0。"""
        m20, m60 = self.ma20[j], self.ma60[j]
        if not m20 or not m60:
            return False
        sl = self.ma20_slope[j]
        mh = self.macd_hist[j]
        return (self.c[j] > m20 > m60 and (sl or 0) > 0 and (mh or 0) >= 0)

    def score(self, j: int, cfg: LabConfig) -> Tuple[float, Dict[str, float]]:
        """6 因子评分（与 trend_strategy / 旧回测器同语义）。"""
        f: Dict[str, float] = {}
        ma5, ma10, ma20 = self.ma5[j], self.ma10[j], self.ma20[j]
        tr = 0.0
        if ma5 and ma10 and ma20:
            if ma5 > ma10 > ma20:
                tr += 1.0
                if (self.ma20_slope[j] or 0) > 0:
                    tr += 1.0
            elif ma5 > ma10:
                tr += 0.5
        f["trend"] = tr
        f["momentum"] = 1.0 if (self.macd_hist[j] or 0) > 0 else 0.0
        ob = 0.0
        r = self.rsi[j]
        if r is not None:
            if 30 < r < 70:
                ob += 1.0
            elif r <= 30:
                ob += 1.5
            elif r >= 80:
                ob -= 0.5
        jj = self.kdj_j[j]
        if jj is not None:
            if jj < 20:
                ob += 0.5
            elif jj > 100:
                ob -= 1.0
        f["oversold"] = max(0.0, min(2.0, ob))
        vp = 0.0
        if j >= 6:
            base = sum(self.v[j - 5:j]) / 5
            if base > 0:
                ratio = self.v[j] / base
                vp = 1.0 if ratio >= 1.2 else (0.5 if ratio >= 1.0 else 0.0)
        f["volume"] = vp
        pos = 0.0
        close = self.c[j]
        bm, bu, bl = self.boll_mid[j], self.boll_up[j], self.boll_lo[j]
        if close and bm and bu and bl:
            if bl <= close <= bu:
                pos = (0.5 + 0.5 * (close - bm) / (bu - bm)) if bu > bm else 0.5
            elif close > bu:
                pos = 0.0 if close > bu * 1.02 else 0.5
            else:
                pos = 0.3 if close > bl * 0.98 else 0.0
        f["position"] = pos
        vw = 0.0
        wv = self.vwap[j]
        if close and wv:
            diff = (close - wv) / wv
            vw = 2.0 if diff > 0.0008 else (1.0 if diff > -0.004 else 0.0)
        f["vwap"] = vw
        return sum(f.values()), f

    def momentum(self, j: int, lookback: int) -> Optional[float]:
        if j < lookback:
            return None
        base = self.c[j - lookback]
        return (self.c[j] / base - 1) if base else None

    def ann_vol(self, j: int, lookback: int = 40) -> Optional[float]:
        if j < lookback + 1:
            return None
        rets = []
        for t in range(j - lookback + 1, j + 1):
            p0 = self.c[t - 1]
            if p0:
                rets.append(self.c[t] / p0 - 1)
        if len(rets) < 5:
            return None
        m = sum(rets) / len(rets)
        var = sum((x - m) ** 2 for x in rets) / len(rets)
        return math.sqrt(var) * math.sqrt(245)


def _rolling_vwap(typ: List[float], vol: List[float], win: int):
    n = len(typ)
    out: List[Optional[float]] = [None] * n
    pv = 0.0
    vv = 0.0
    for i in range(n):
        pv += typ[i] * vol[i]
        vv += vol[i]
        if i >= win:
            pv -= typ[i - win] * vol[i - win]
            vv -= vol[i - win]
        if i >= win - 1 and vv > 0:
            out[i] = pv / vv
    return out


def _slope_array(vals, lookback: int):
    """等价 I.slope(vals[:i+1], lookback) 的 O(n) 数组版。"""
    n = len(vals)
    out: List[Optional[float]] = [None] * n
    valid_pos: List[int] = []
    for i in range(n):
        if vals[i] is not None:
            valid_pos.append(i)
        if len(valid_pos) < 2:
            continue
        tail = valid_pos[-lookback:]
        if len(tail) < 2:
            tail = valid_pos[-2:]
        first, last_ = vals[tail[0]], vals[tail[-1]]
        out[i] = ((last_ - first) / (len(tail) - 1)) if first else None
    return out


def _rolling_max(vals: List[float], win: int):
    n = len(vals)
    out: List[Optional[float]] = [None] * n
    from collections import deque
    dq: deque = deque()
    for i in range(n):
        while dq and vals[dq[-1]] <= vals[i]:
            dq.pop()
        dq.append(i)
        while dq[0] <= i - win:
            dq.popleft()
        if i >= win - 1:
            out[i] = vals[dq[0]]
    return out


# ============================================================ regime

class Regime:
    """指数级市场状态（多头 / 空头）。"""

    def __init__(self, panel: AlignedPanel, code: str, cfg: LabConfig):
        self.ok: List[bool] = [False] * len(panel.dates)
        cl = panel.close.get(code)
        if not cl:
            self.ok = [True] * len(panel.dates)     # 无指数数据 → 不过滤
            self.available = False
            return
        self.available = True
        vp = [k for k, x in enumerate(cl) if x is not None]
        c = [cl[k] for k in vp]
        ma = I.sma(c, cfg.regime_ma)
        slope = _slope_array(ma, cfg.regime_slope_days or 5)
        cur = False
        j = 0
        for k in range(len(panel.dates)):
            if j < len(vp) and vp[j] == k:
                m = ma[j]
                if m:
                    cur = c[j] > m
                    if cfg.regime_slope_days > 0:
                        cur = cur and (slope[j] or 0) > 0
                j += 1
            self.ok[k] = cur


# ============================================================ 回测

@dataclass
class Position:
    qty: int = 0
    entry: float = 0.0
    entry_k: int = 0
    peak: float = 0.0
    stop_price: float = 0.0
    target_price: float = 0.0


def run_lab(panel: AlignedPanel, codes: Sequence[str], cfg: LabConfig,
            index_code: str = "399006.SZ",
            init_cash: float = 1_000_000.0) -> dict:
    codes = [c for c in codes if c in panel.close]
    if not codes:
        return {"error": "no codes"}
    series: Dict[str, StockSeries] = {c: StockSeries(panel, c, cfg)
                                      for c in codes}
    regime = Regime(panel, index_code, cfg)
    nd = len(panel.dates)

    cash = init_cash
    equity = init_cash
    positions: Dict[str, Position] = {}
    curve: List[float] = []
    curve_dates: List[str] = []
    rets: List[float] = []
    trades: List[dict] = []
    exposure: List[float] = []

    for k in range(nd - 1):
        # 预热：要求所有指标可用
        active = {}
        for code, s in series.items():
            j = s.j_at(k)
            if j is not None and j >= cfg.warmup:
                active[code] = j
        if not active and not positions:
            continue

        # 当日市值（用收盘）
        mv = 0.0
        for code, pos in positions.items():
            s = series[code]
            j = s.j_at(k)
            px = s.c[j] if j is not None else pos.entry
            mv += pos.qty * px
        prev_equity = cash + mv

        # ---------------- 1) 离场 ----------------
        regime_bad = cfg.regime_exit and not regime.ok[k]
        for code in list(positions.keys()):
            pos = positions[code]
            s = series[code]
            j = s.j_at(k)
            if j is None:
                continue                     # 停牌，无法交易
            hi, lo, cl = s.h[j], s.l[j], s.c[j]
            pos.peak = max(pos.peak, hi)
            exit_px: Optional[float] = None
            reason = ""

            if regime_bad:
                exit_px, reason = cl, "regime_exit"
            elif cfg.exit_mode == "trend":
                me = s.ma_exit[j]
                m20 = s.ma20[j]
                if me and ((m20 and m20 < me) or cl < me):
                    exit_px, reason = cl, "trend_break"
                elif (cl / pos.entry - 1) <= cfg.hard_stop_pct:
                    exit_px, reason = cl, "hard_stop"
            elif cfg.exit_mode == "chandelier":
                ap = s.atrp[j]
                if ap:
                    ch = pos.peak * (1 - cfg.chandelier_mult * ap)
                    if ch > pos.stop_price:
                        pos.stop_price = ch
                if lo <= pos.stop_price:
                    exit_px, reason = pos.stop_price, "chandelier"
                elif (cl / pos.entry - 1) <= cfg.hard_stop_pct:
                    exit_px, reason = cl, "hard_stop"
            else:   # scalp
                if lo <= pos.stop_price:
                    exit_px, reason = pos.stop_price, "stop_loss"
                if exit_px is None and hi >= pos.target_price:
                    exit_px, reason = pos.target_price, "take_profit"
                if exit_px is None and cfg.trailing and pos.entry > 0:
                    if (pos.peak - pos.entry) / pos.entry >= cfg.trailing_activation:
                        trail = max(pos.peak * (1 + cfg.trailing_stop),
                                    pos.entry * (1 + cfg.trailing_floor))
                        if lo <= trail:
                            exit_px, reason = trail, "trailing"
            # 单日暴跌
            if exit_px is None and j >= 1 and s.c[j - 1] > 0:
                if (cl / s.c[j - 1] - 1) * 100 <= cfg.down_day_exit_pct:
                    exit_px, reason = cl, "crash"
            # 时间止损（不盈利就走）
            if exit_px is None and cfg.time_stop_days > 0:
                held = k - pos.entry_k
                if held >= cfg.time_stop_days and cl <= pos.entry:
                    exit_px, reason = cl, "time_stop"
            # 最长持仓
            if exit_px is None and (k - pos.entry_k) >= cfg.max_hold_days:
                exit_px, reason = cl, "timeout"

            if exit_px is not None:
                fill = exit_px * (1 - cfg.slippage_pct)
                cash += pos.qty * fill * (1 - cfg.cost_pct)
                net_in = pos.entry * (1 + cfg.cost_pct + cfg.slippage_pct)
                net_out = fill * (1 - cfg.cost_pct)
                trades.append({
                    "code": code, "pnl_pct": (net_out - net_in) / net_in,
                    "hold": k - pos.entry_k, "reason": reason,
                    "exit_date": panel.dates[k],
                })
                del positions[code]

        # ---------------- 2) 候选筛选 ----------------
        regime_ok = (not cfg.regime_filter) or regime.ok[k]
        cand: List[Tuple[float, str, float]] = []   # (score, code, momentum)
        if regime_ok:
            # 横截面动量池
            allowed: Optional[set] = None
            mom_map: Dict[str, float] = {}
            for code, j in active.items():
                m = series[code].momentum(j, cfg.momentum_lookback)
                if m is not None:
                    mom_map[code] = m
            if cfg.momentum_rank:
                ranked = sorted(((m, c) for c, m in mom_map.items()
                                 if m > cfg.momentum_min), reverse=True)
                allowed = {c for _, c in ranked[:cfg.momentum_top_n]}

            for code, j in active.items():
                if code in positions:
                    continue
                if allowed is not None and code not in allowed:
                    continue
                s = series[code]
                if cfg.entry_mode == "breakout":
                    hh = s.hh[j]
                    if not hh or s.c[j] < hh - 1e-9:
                        continue
                    if cfg.use_gate and not s.gate_ok(j):
                        continue
                    sc = 10.0
                elif cfg.entry_mode == "trend":
                    if not s.gate_ok(j):
                        continue
                    hi20 = max(s.c[max(0, j - 20):j + 1])
                    if s.c[j] < hi20 * 0.98:
                        continue
                    sc = 10.0
                else:   # factor
                    sc, f = s.score(j, cfg)
                    if sc < cfg.buy_score_threshold:
                        continue
                    if sum(1 for x in f.values() if x > 0) < cfg.min_signals:
                        continue
                    if cfg.use_gate and not s.gate_ok(j):
                        continue
                cand.append((sc, code, mom_map.get(code, 0.0)))
            # 排序：动量优先（若启用排名），否则按评分
            if cfg.momentum_rank:
                cand.sort(key=lambda x: (x[2], x[0]), reverse=True)
            else:
                cand.sort(key=lambda x: (x[0], x[2]), reverse=True)

        # ---------------- 3) 轮动（满仓时换弱为强） ----------------
        if cfg.rotate and cand and len(positions) >= cfg.max_positions:
            held_mom = []
            for code in positions:
                s = series[code]
                j = s.j_at(k)
                if j is None:
                    continue
                m = s.momentum(j, cfg.momentum_lookback)
                if m is not None:
                    held_mom.append((m, code))
            if held_mom:
                held_mom.sort()
                weak_m, weak_code = held_mom[0]
                best_m = cand[0][2]
                if best_m - weak_m >= cfg.rotate_edge:
                    s = series[weak_code]
                    j = s.j_at(k)
                    if j is not None:
                        pos = positions[weak_code]
                        fill = s.c[j] * (1 - cfg.slippage_pct)
                        cash += pos.qty * fill * (1 - cfg.cost_pct)
                        net_in = pos.entry * (1 + cfg.cost_pct + cfg.slippage_pct)
                        net_out = fill * (1 - cfg.cost_pct)
                        trades.append({
                            "code": weak_code,
                            "pnl_pct": (net_out - net_in) / net_in,
                            "hold": k - pos.entry_k, "reason": "rotate_out",
                            "exit_date": panel.dates[k],
                        })
                        del positions[weak_code]

        # ---------------- 4) 建仓（T+1 开盘） ----------------
        for sc, code, mom in cand:
            if len(positions) >= cfg.max_positions:
                break
            s = series[code]
            j_next = s.j_at(k + 1)
            if j_next is None:
                continue                       # 次日停牌，放弃
            entry = s.o[j_next] * (1 + cfg.slippage_pct)
            if entry <= 0:
                continue
            j = s.j_at(k)
            ap = s.atrp[j] or abs(cfg.stop_loss)
            stop_dist = max(abs(cfg.stop_loss), ap * cfg.atr_stop_mult)
            tgt_dist = max(abs(cfg.take_profit), ap * cfg.tp_atr_mult)

            # 仓位
            if cfg.sizing == "equal":
                budget = equity / cfg.max_positions
            elif cfg.sizing == "voltarget":
                av = s.ann_vol(j) or cfg.target_vol
                budget = equity * min(1.0, cfg.target_vol / av) / cfg.max_positions
            else:   # risk
                budget = equity * cfg.risk_per_trade / stop_dist
            budget = min(budget, equity * cfg.max_pos_pct)
            if budget < equity * cfg.min_pos_pct:
                continue
            usable = cash - equity * cfg.cash_buffer_pct
            budget = min(budget, usable)
            unit = entry * 100 * (1 + cfg.cost_pct)
            qty = int(budget // unit) * 100
            if qty <= 0:
                continue
            cost = qty * entry * (1 + cfg.cost_pct)
            if cost > cash:
                continue
            cash -= cost
            positions[code] = Position(
                qty=qty, entry=entry, entry_k=k + 1, peak=entry,
                stop_price=entry * (1 - stop_dist),
                target_price=entry * (1 + tgt_dist),
            )

        # ---------------- 5) 记账 ----------------
        mv = 0.0
        for code, pos in positions.items():
            s = series[code]
            j = s.j_at(k)
            px = s.c[j] if j is not None else pos.entry
            mv += pos.qty * px
        equity = cash + mv
        curve.append(equity)
        curve_dates.append(panel.dates[k])
        exposure.append(mv / equity if equity > 0 else 0.0)
        if prev_equity > 0 and len(curve) > 1:
            rets.append(equity / prev_equity - 1)

    return _metrics(cfg, curve, curve_dates, rets, trades, exposure,
                    len(codes))


# ============================================================ 绩效

def _metrics(cfg, curve, dates, rets, trades, exposure, n_codes) -> dict:
    if len(curve) < 2:
        return {"error": "curve too short", "config": cfg}
    total = curve[-1] / curve[0] - 1
    years = len(curve) / 245.0
    cagr = (curve[-1] / curve[0]) ** (1 / years) - 1 if years > 0 else 0.0
    peak = curve[0]
    mdd = 0.0
    for e in curve:
        peak = max(peak, e)
        mdd = min(mdd, e / peak - 1)
    m = sum(rets) / len(rets) if rets else 0.0
    var = (sum((x - m) ** 2 for x in rets) / len(rets)) if len(rets) > 1 else 0.0
    sd = math.sqrt(var)
    sharpe = (m / sd * math.sqrt(245)) if sd > 0 else 0.0
    downs = [x for x in rets if x < 0]
    dsd = math.sqrt(sum(x * x for x in downs) / len(downs)) if downs else 0.0
    sortino = (m / dsd * math.sqrt(245)) if dsd > 0 else 0.0
    calmar = (cagr / abs(mdd)) if mdd < 0 else 0.0
    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] <= 0]
    gp = sum(t["pnl_pct"] for t in wins)
    gl = abs(sum(t["pnl_pct"] for t in losses))
    reasons: Dict[str, int] = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    return {
        "config": cfg, "name": cfg.name, "n_codes": n_codes,
        "total_return": total, "cagr": cagr, "max_drawdown": mdd,
        "sharpe": sharpe, "sortino": sortino, "calmar": calmar,
        "n_trades": len(trades),
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "avg_win": (gp / len(wins)) if wins else 0.0,
        "avg_loss": (-gl / len(losses)) if losses else 0.0,
        "profit_factor": (gp / gl) if gl > 0 else float("inf"),
        "avg_hold": (sum(t["hold"] for t in trades) / len(trades))
                    if trades else 0.0,
        "exposure": sum(exposure) / len(exposure) if exposure else 0.0,
        "curve": curve, "dates": dates, "trades": trades,
        "exit_reasons": reasons,
        "start": dates[0], "end": dates[-1],
    }


# ============================================================ 基准

def buy_hold_benchmark(panel: AlignedPanel, codes: Sequence[str],
                       cost_pct: float = 0.0010,
                       init_cash: float = 1_000_000.0,
                       warmup: int = 130) -> dict:
    """等权买入持有：每只在其"预热完成后的首个交易日"等权买入并持有到底。

    未上市的标的资金留现金，上市后按当时权益等分补入 —— 避免把
    "后上市的大牛股从第一天就满仓持有"这种未来函数塞进基准。
    """
    codes = [c for c in codes if c in panel.close]
    series = {c: StockSeries(panel, c, LabConfig()) for c in codes}
    nd = len(panel.dates)
    cash = init_cash
    qty: Dict[str, int] = {}
    curve, dates, rets = [], [], []
    n_total = len(codes)
    for k in range(nd):
        mv = 0.0
        for code, q in qty.items():
            s = series[code]
            j = s.j_at(k)
            if j is not None:
                mv += q * s.c[j]
        prev = cash + mv
        # 新上市（预热完成）标的等权买入
        for code in codes:
            if code in qty:
                continue
            s = series[code]
            j = s.j_at(k)
            if j is None or j < warmup:
                continue
            budget = (cash + mv) / n_total
            budget = min(budget, cash)
            px = s.c[j]
            q = int(budget // (px * 100 * (1 + cost_pct))) * 100
            if q > 0:
                cash -= q * px * (1 + cost_pct)
                qty[code] = q
        mv = 0.0
        for code, q in qty.items():
            s = series[code]
            j = s.j_at(k)
            if j is not None:
                mv += q * s.c[j]
        eq = cash + mv
        curve.append(eq)
        dates.append(panel.dates[k])
        if prev > 0 and len(curve) > 1:
            rets.append(eq / prev - 1)
    cfg = LabConfig(name="buy_hold")
    return _metrics(cfg, curve, dates, rets, [], [1.0] * len(curve), len(codes))


def index_benchmark(panel: AlignedPanel, code: str) -> dict:
    cl = panel.close.get(code) or []
    vp = [k for k, x in enumerate(cl) if x is not None]
    if len(vp) < 2:
        return {"error": "no index"}
    curve = [cl[k] for k in vp]
    dates = [panel.dates[k] for k in vp]
    base = curve[0]
    curve = [1_000_000.0 * x / base for x in curve]
    rets = [curve[i] / curve[i - 1] - 1 for i in range(1, len(curve))]
    return _metrics(LabConfig(name=f"index:{code}"), curve, dates, rets,
                    [], [1.0] * len(curve), 1)


# ============================================================ 报告

def fmt_row(r: dict) -> str:
    if "error" in r:
        return f"{r.get('name','?'):<26} ERROR {r['error']}"
    return (f"{r['name']:<26} "
            f"{r['total_return']*100:>+9.1f}% "
            f"{r['cagr']*100:>+8.1f}% "
            f"{r['max_drawdown']*100:>8.1f}% "
            f"{r['sharpe']:>7.2f} "
            f"{r['calmar']:>7.2f} "
            f"{r['n_trades']:>6d} "
            f"{r['win_rate']*100:>6.1f}% "
            f"{r['profit_factor']:>7.2f} "
            f"{r['avg_hold']:>7.1f} "
            f"{r['exposure']*100:>6.1f}%")


HEADER = (f"{'策略':<26} {'累计':>10} {'年化':>9} {'回撤':>9} "
          f"{'Sharpe':>7} {'Calmar':>7} {'笔数':>6} {'胜率':>7} "
          f"{'盈亏比':>7} {'持仓天':>7} {'仓位':>7}")


def print_table(rows: List[dict], title: str = ""):
    if title:
        print(f"\n{'='*126}\n{title}\n{'='*126}")
    print(HEADER)
    print("-" * 126)
    for r in rows:
        print(fmt_row(r))

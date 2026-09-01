# -*- coding: utf-8 -*-
"""
日线回测器（真实历史数据，xtdata 本地零额度）

目的：用真实日线数据，对"原策略逻辑" vs "优化后逻辑"做 A/B，
量化本次优化的贡献（趋势闸门 / 移动止损 / 波动率目标仓位）。

说明：
  - 真实系统是在 1 分钟 K 线上跑 6 因子 + 日线趋势闸门。1 分钟历史
    需 Tushare 积分，这里用日线复刻同一套因子与闸门逻辑（日线级），
    并额外用 MA60 作为"更高周期"趋势过滤，等价体现 MTF 思想。
  - 信号在 day i 收盘产生，day i+1 开盘成交（避免未来函数）。
  - 离场用当日 high/low 判断是否触发止损/止盈/移动止损。

用法：
  python strategy/backtest_daily.py            # 跑 baseline + optimized 对比
  python strategy/backtest_daily.py --mode optimized
  python strategy/backtest_daily.py --code 300308.SZ
"""
from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import (  # noqa: E402
    STOCK_CODES, UNIVERSE, MARKET_INDEX_CODE, SECTOR_OF,
)
from core import indicators as I  # noqa: E402
from core.qmt_client import qmt_client  # noqa: E402


# ============================================================ 数据

def load_daily(code: str, count: int = 260) -> Optional[Dict[str, List[float]]]:
    """优先 xtdata 本地日线。同时返回交易日 date（用于跨标的对齐）。"""
    try:
        raw = qmt_client.get_history(code, period="1d", count=count)
        if raw and len(raw) >= 120:
            return {
                "date": [b["ts"].strftime("%Y%m%d") for b in raw],
                "open": [b["open"] for b in raw],
                "high": [b["high"] for b in raw],
                "low": [b["low"] for b in raw],
                "close": [b["close"] for b in raw],
                "volume": [b["volume"] for b in raw],
            }
    except Exception:
        pass
    return None


def align_panel(data: Dict[str, dict]) -> Tuple[List[str], Dict[str, dict]]:
    """把各标的日线对齐到统一交易日轴。

    为什么必须做：原实现用 ``n = min(len(close))`` 截断，索引 i 在不同
    标的上指向**不同日期**（任一标的停牌/上市晚就错位），横截面动量排名
    和组合权益都会失真。

    停牌日处理：前向填充（o/h/l/c 全取上一收盘，量为 0）并标记
    ``valid[i]=False``，该日禁止成交但持仓照常按最后价格估值。
    """
    if not data:
        return [], {}

    # 兼容无 date 的旧数据/mock：退化为尾部截断对齐
    if not all("date" in d for d in data.values()):
        n = min(len(d["close"]) for d in data.values())
        out: Dict[str, dict] = {}
        for code, d in data.items():
            out[code] = {k: list(d[k][-n:])
                         for k in ("open", "high", "low", "close", "volume")}
            out[code]["valid"] = [True] * n
        return [str(k) for k in range(n)], out

    all_dates = sorted({dt for d in data.values() for dt in d["date"]})
    out = {}
    for code, d in data.items():
        idx = {dt: k for k, dt in enumerate(d["date"])}
        o, h, l, c, v, valid = [], [], [], [], [], []
        last_close = None
        for dt in all_dates:
            k = idx.get(dt)
            if k is None:
                # 停牌 / 未上市
                if last_close is None:
                    o.append(0.0); h.append(0.0); l.append(0.0)
                    c.append(0.0); v.append(0.0); valid.append(False)
                else:
                    o.append(last_close); h.append(last_close)
                    l.append(last_close); c.append(last_close)
                    v.append(0.0); valid.append(False)
            else:
                o.append(d["open"][k]); h.append(d["high"][k])
                l.append(d["low"][k]); c.append(d["close"][k])
                v.append(d["volume"][k]); valid.append(True)
                last_close = d["close"][k]
        out[code] = {"open": o, "high": h, "low": l,
                     "close": c, "volume": v, "valid": valid}
    return all_dates, out


# ============================================================ 因子（与 trend_strategy 等价）

def score_daily_series(o, h, l, c, v) -> List[Tuple[float, Dict[str, float]]]:
    """向量化 6 因子评分：预计算所有指标序列一次，逐索引取 [i]。

    与 ``score_daily`` 数值完全一致（I.last(series[:i+1]) == series[i]，
    因指标在预热后恒为非负值、无 None 反弹）。返回长度 n 的列表，
    每项 = (score, factors_dict)。run_backtest 用它把 O(n^2) 的逐 bar
    重算降为 O(n)，加速 walk-forward 验证（结果不变）。
    """
    n = len(c)
    sma5 = I.sma(c, 5)
    sma10 = I.sma(c, 10)
    sma20 = I.sma(c, 20)
    rsi = I.rsi(c, 14)
    _, _, hist = I.macd(c, 12, 26, 9)
    k, d, j = I.kdj(h, l, c, 9)
    bmid, bup, blo = I.boll(c, 20, 2.0)
    atr = I.atr(h, l, c, 14)
    sma20_slope = _vec_slope(sma20, 5)   # 全序列斜率（与 daily_trend_up 等价）
    typ = [(hh + ll + cc) / 3 for hh, ll, cc in zip(h, l, c)]
    vwap = I.vwap(typ, v)
    out: List[Tuple[float, Dict[str, float]]] = []
    for i in range(n):
        close = c[i]
        ma5 = sma5[i]; ma10 = sma10[i]; ma20 = sma20[i]
        rsi_i = rsi[i]; macd_hist = hist[i]
        # KDJ 无内置预热：全序列从第 0 根起算。但原 score_daily 传入的是
        # 截断序列 c[:i+1]，长度 < 9 时 I.kdj 整体返回 None。为与逐 bar 版本
        # 在每根 i 上完全一致，i+1 < 9 时置 None（仅影响预热区，不参与交易）。
        kdj_j = j[i] if (i + 1) >= 9 else None
        boll_mid = bmid[i]; boll_up = bup[i]; boll_lo = blo[i]
        atr_i = atr[i]; vwap_i = vwap[i]
        factors: Dict[str, float] = {}
        # 趋势
        tr = 0.0
        if ma5 and ma10 and ma20:
            if ma5 > ma10 > ma20:
                tr += 1.0
                ma20_s = sma20_slope[i] if sma20_slope[i] is not None else 0.0
                if ma20_s > 0:
                    tr += 1.0
            elif ma5 > ma10:
                tr += 0.5
        factors["trend"] = round(tr, 2)
        # 动量
        mm = 0.0
        if macd_hist is not None and macd_hist > 0:
            mm += 1.0
        factors["momentum"] = round(mm, 2)
        # 超买超卖
        ob = 0.0
        if rsi_i is not None:
            if 30 < rsi_i < 70:
                ob += 1.0
            elif rsi_i <= 30:
                ob += 1.5
            elif rsi_i >= 80:
                ob -= 0.5
        if kdj_j is not None:
            if kdj_j < 20:
                ob += 0.5
            elif kdj_j > 100:
                ob -= 1.0
        factors["oversold"] = round(max(0.0, min(2.0, ob)), 2)
        # 量价
        vp = 0.0
        if i >= 5 and v and sum(v[i - 5:i]) > 0:
            ratio = v[i] / (sum(v[i - 5:i]) / 5)
            if ratio >= 1.2:
                vp = 1.0
            elif ratio >= 1.0:
                vp = 0.5
        factors["volume"] = round(vp, 2)
        # 位置（BOLL）
        pos = 0.0
        if close and boll_mid and boll_up and boll_lo:
            if boll_lo <= close <= boll_up:
                pos = (0.5 + 0.5 * (close - boll_mid) / (boll_up - boll_mid)
                       if boll_up > boll_mid else 0.5)
            elif close > boll_up:
                pos = 0.0 if close > boll_up * 1.02 else 0.5
            elif close < boll_lo:
                pos = 0.3 if close > boll_lo * 0.98 else 0.0
        factors["position"] = round(pos, 2)
        # VWAP（重校准版）
        vw = 0.0
        if close and vwap_i:
            diff = (close - vwap_i) / vwap_i
            if diff > 0.0008:
                vw = 2.0
            elif diff > -0.004:
                vw = 1.0
        factors["vwap"] = round(vw, 2)
        out.append((round(sum(factors.values()), 2), factors))
    return out


def score_daily(o, h, l, c, v) -> Tuple[float, Dict[str, float]]:
    """对一组日线序列（截至当前）计算 6 因子评分。c[-1] 为当前收盘。

    兼容旧调用：等价于 score_daily_series(...)[-1]。
    """
    ma5 = I.last(I.sma(c, 5))
    ma10 = I.last(I.sma(c, 10))
    ma20 = I.last(I.sma(c, 20))
    rsi = I.last(I.rsi(c, 14))
    _, _, hist = I.macd(c, 12, 26, 9)
    macd_hist = I.last(hist)
    k, d, j = I.kdj(h, l, c, 9)
    kdj_j = I.last(j)
    _bmid, _bup, _blo = I.boll(c, 20, 2.0)
    boll_mid, boll_up, boll_lo = I.last(_bmid), I.last(_bup), I.last(_blo)
    atr = I.last(I.atr(h, l, c, 14))
    typ = [(hh + ll + cc) / 3 for hh, ll, cc in zip(h, l, c)]
    vwap = I.last(I.vwap(typ, v))
    close = c[-1]

    factors: Dict[str, float] = {}
    # 趋势
    tr = 0.0
    if ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20:
            tr += 1.0
            ma20_s = I.slope(I.sma(c, 20), 5) or 0
            if ma20_s > 0:
                tr += 1.0
        elif ma5 > ma10:
            tr += 0.5
    factors["trend"] = round(tr, 2)
    # 动量
    mm = 0.0
    if macd_hist is not None and macd_hist > 0:
        mm += 1.0
    factors["momentum"] = round(mm, 2)
    # 超买超卖
    ob = 0.0
    if rsi is not None:
        if 30 < rsi < 70:
            ob += 1.0
        elif rsi <= 30:
            ob += 1.5
        elif rsi >= 80:
            ob -= 0.5
    if kdj_j is not None:
        if kdj_j < 20:
            ob += 0.5
        elif kdj_j > 100:
            ob -= 1.0
    factors["oversold"] = round(max(0.0, min(2.0, ob)), 2)
    # 量价
    vp = 0.0
    if v and len(v) >= 6 and sum(v[-6:-1]) > 0:
        ratio = v[-1] / (sum(v[-6:-1]) / 5)
        if ratio >= 1.2:
            vp = 1.0
        elif ratio >= 1.0:
            vp = 0.5
    factors["volume"] = round(vp, 2)
    # 位置（BOLL）
    pos = 0.0
    if close and boll_mid and boll_up and boll_lo:
        if boll_lo <= close <= boll_up:
            pos = 0.5 + 0.5 * (close - boll_mid) / (boll_up - boll_mid) if boll_up > boll_mid else 0.5
        elif close > boll_up:
            pos = 0.0 if close > boll_up * 1.02 else 0.5
        elif close < boll_lo:
            pos = 0.3 if close > boll_lo * 0.98 else 0.0
    factors["position"] = round(pos, 2)
    # VWAP（重校准版）
    vw = 0.0
    if close and vwap:
        diff = (close - vwap) / vwap
        if diff > 0.0008:
            vw = 2.0
        elif diff > -0.004:
            vw = 1.0
    factors["vwap"] = round(vw, 2)

    return round(sum(factors.values()), 2), factors


def daily_trend_up(c, h, l, i: int) -> bool:
    """更高周期趋势闸门：close>MA20>MA60 且 MA20 斜率>0 且 MACD>=0。"""
    if i < 60:
        return False
    ma20 = I.last(I.sma(c[:i + 1], 20))
    ma60 = I.last(I.sma(c[:i + 1], 60))
    ma20_s = I.slope(I.sma(c[:i + 1], 20), 5) or 0
    _, _, hist = I.macd(c[:i + 1], 12, 26, 9)
    macd_hist = I.last(hist)
    close = c[i]
    return (ma20 and ma60 and close > ma20 > ma60
            and ma20_s > 0 and (macd_hist or 0) >= 0)


def atr_pct_at(h, l, c, i: int) -> float:
    atr = I.last(I.atr(h[:i + 1], l[:i + 1], c[:i + 1], 14))
    return (atr / c[i]) if (atr and c[i]) else 0.0


# ============================================================ 回测

@dataclass
class BacktestConfig:
    use_gate: bool = True           # 日线趋势闸门
    stop_loss: float = -0.04         # 固定止损下限
    take_profit: float = 0.12        # 固定止盈下限
    atr_stop_mult: float = 2.5       # 止损距离 = ATR% * 该倍数
    tp_atr_mult: float = 4.0         # 止盈距离 = ATR% * 该倍数
    trailing: bool = True
    trailing_activation: float = 0.06
    trailing_stop: float = -0.03
    trailing_floor: float = -0.005   # 移动止损保本线下限
    max_hold_days: int = 20
    vol_sizing: bool = True         # ATR 波动率目标仓位
    risk_per_trade: float = 0.01
    fixed_amount: float = 50000.0
    cost_pct: float = 0.0015         # 单边交易成本（佣金+印花税+滑点），计入
    chandelier: bool = False         # True=用峰值时 ATR 吊灯止损（趋势跟随）
    entry_mode: str = "factor"       # "factor"=6因子评分; "trend"=日线突破追涨
    max_positions: int = 8
    # ---- 分行业分散持仓（组合层风险约束，研究用）----
    # 与 max_positions（组合总上限）正交：在"总上限"之内，进一步限制
    # 同一 AI 产业链环节（光模块/AI芯片/晶圆代工/…）最多持几只。
    # 经济逻辑：AI 产业链各环节内高度同向（如光模块三杰齐涨齐跌），
    # 动量排名前 6 常挤在同一环节，导致"伪分散"。限制 per-sector 持仓数
    # 强制组合跨环节铺开，降低环节内相关性带来的隐性集中风险，提升
    # Sharpe / Calmar。默认 0 = 关闭（行为与旧版完全一致）。
    # 须经 IS/OOS + 多折 walk-forward 严格验证后才并入生产。
    max_per_sector: int = 0
    buy_score_threshold: float = 4.0
    min_signals: int = 3
    # ---- 退出范式 + 动量（本次优化）----
    exit_mode: str = "scalp"        # "scalp"=紧移动止损; "trend"=趋势骑行至破位
    trend_exit_ma: int = 60          # 趋势破位判定均线
    hard_stop_pct: float = -0.18     # 趋势模式宽幅硬止损（灾难保护）
    trend_max_hold_days: int = 120   # 趋势模式最长持仓
    momentum_rank: bool = False      # 只交易 60 日动量前 N 名
    momentum_top_n: int = 6
    momentum_lookback: int = 60
    down_day_exit_pct: float = -99.0  # 单日暴跌清仓阈值（-99 关闭）
    # 强制最小预热长度。walk-forward 调参时必须固定，否则不同参数
    # （如 momentum_lookback 20 vs 120）会产生不同的交易起点，窗口不可比。
    min_warmup: int = 0
    # ---- 市场状态过滤（regime filter）----
    # 原策略 exposure 恒 ~97%（永远满仓），熊市/震荡市直接骑着下跌。
    #   "off"     = 不过滤（原行为）
    #   "index"   = 指数在 MA(regime_ma) 之上才允许开新仓
    #   "breadth" = 宇宙内「站上 MA(regime_ma) 的比例」超过阈值才开新仓
    regime_mode: str = "off"
    regime_index: str = "399006.SZ"
    regime_ma: int = 60
    regime_breadth_thresh: float = 0.5
    regime_force_exit: bool = False   # True=状态转差时强制清仓（防御）
    # ---- regime 抗抖动（anti-whipsaw）----
    # 二值闸门 close>MA60 在均线附近会反复翻转：每次翻转都触发「全部清仓
    # → 隔日重新建仓」，双向各付 0.15% 成本，还会错杀刚起步的趋势。
    # 下面三个旋钮各自独立，全部取默认值时行为与原二值闸门完全一致：
    #   confirm_days=1 / buffer_pct=0 / slope_days=0
    regime_confirm_days: int = 1      # 需连续 N 日满足条件才翻转状态
    regime_buffer_pct: float = 0.0    # 缓冲带：上穿需 >MA*(1+b)，下穿需 <MA*(1-b)
    regime_slope_days: int = 0        # >0 时额外要求 MA 的 N 日斜率为正
    # ---- 尾部风险对冲（指数级峰值回撤熔断，研究用，全新方向）----
    # 与「已否决的指数 MA60 持久闸门」**本质不同**：MA60 闸门在弱势期
    # 「出得来、回不去」——只要指数低于 MA60 就持续空仓，强趋势反弹里
    # 反复踏空。本熔断用**两段式状态机**规避该缺陷：
    #   · 进入防御态（清仓）：指数自「运行峰值」回撤 <= tail_drawdown_pct
    #     （真崩盘才触发，正常上涨途中的小回撤不触发）。
    #   · 解除防御态（重开仓）：防御态期间指数自「崩盘低点(trough)」反弹
    #     >= tail_recover_pct，**或**创出新高（peak 上移）。
    # 即入场门槛看「峰值回撤」、离场门槛看「低点反弹」——二者解耦，
    # 崩盘后只需反弹 tail_recover_pct 即回场，不必回到旧峰值，从而
    # 避免 MA60 闸门「回不去」、长期踏空反弹的问题。
    # 默认关闭（regime_mode="off" 不加载）：须经 IS/OOS + 多折 walk-forward
    # 严格验证后才并入生产；可逆（regime_mode 改回 "off" 即恢复）。
    tail_index: str = "399006.SZ"      # 跟踪的指数（创业板指，与 AI 宇宙高β）
    tail_drawdown_pct: float = -0.12   # 指数自峰值回撤超此值 → 进入防御态
    tail_recover_pct: float = 0.15     # 防御态内自低点反弹超此值 → 解除防御
    tail_force_exit: bool = True       # 防御态强制清仓（资本保全）
    # ---- 横截面动量指标（选股口径）----
    # "raw"      = 区间涨幅（原口径，天然偏好高波动股）
    # "sharpe"   = 风险调整动量（区间日收益 均值/标准差），偏好走得稳的趋势
    # "residual" = 相对 regime 指数的超额动量，剥离市场 beta
    # 三者都保留「原始涨幅必须为正」的入池条件，只改变**排序**口径，
    # 以便把「选股口径」这一变量单独隔离出来检验。
    mom_metric: str = "raw"
    # ---- 趋势模式移动止损（锁定利润，防回吐）----
    # 趋势骑行虽然能吃到主升，但"破位才下车"会把巨大浮盈在回调里吐回去。
    # 宽幅移动止损：浮盈超过 activation 后，从峰值回撤 stop 即离场，
    # floor 保本线（默认 0=成本）。与 scalp 的紧移动止损不同，这里更宽，
    # 只在中期反转时触发，不被正常波动震出。
    trend_trailing: bool = False
    trend_trail_activation: float = 0.15   # 浮盈 >= 15% 才激活
    trend_trail_stop: float = -0.12        # 从峰值回撤 12% 离场
    trend_trail_floor: float = 0.0         # 止损下限 = 成本×(1+0)，保本
    # ---- 趋势模式波动率目标仓位（修正 sizing 失真）----
    # 原 sizing 用紧止损(-4% 或 atr×2.5)算仓位，但趋势模式真实止损是
    # 宽幅硬止损(-18% / atr×6)，导致 budget/stop_dist 恒大于 30% 上限 →
    # 每笔都满到 30% 上限，risk_per_trade 形同虚设、且高波动股反而仓位更重。
    # 开启后改用真实趋势止损距离做风险平价：高波动少买、低波动多买。
    trend_vol_sizing: bool = False
    # ---- 组合级动态暴露（回撤控制 DD control，风险预算层）----
    # 独立于 regime 的"组合级风险预算"：当组合净值自峰值回撤超过
    # dd_ctrl_start 时，对新开仓的目标仓位按比例缩减（exp_scale 由 1.0
    # 线性降至 dd_ctrl_floor）；净值创新高（峰值上移）后自动恢复满仓。
    # 只在「新开仓」上生效（不强制平已有持仓），属防御性减仓，不触发
    # 「低位卖出」。旨在降低最坏回撤、提升 Calmar / Sharpe。
    # 默认关闭：需经 IS/OOS + 多折 walk-forward 验证后才并入生产。
    dd_ctrl: bool = False
    dd_ctrl_start: float = -0.05       # 回撤超过该值开始减仓
    dd_ctrl_floor: float = 0.25        # 回撤达到 dd_ctrl_full 时最低暴露比例
    dd_ctrl_full: float = -0.25        # 回撤达到该值暴露降至 floor
    # ---- 再入场冷静期（降低无谓换手 / 削减成本侵蚀）----
    # 标的离场后，间隔不足 reentry_cooldown 个交易日不予重新开仓。
    # 动量每日重排名会在回调里把刚卖出的股票又买回，产生双向成本且易 whipaw；
    # 设冷静期可压缩换手、提升净 alpha。默认 0 = 关闭（需验证后启用）。
    reentry_cooldown: int = 0
    # ---- 动量排名加权仓位（横截面 alpha 杠杆，研究用）----
    # 选股仍用动量排名做闸门（只持前 N），但仓位不再等权：按动量大小对前 N
    # 名做 rank-weight（最强者更大、最弱者更小，均值=1），把资本集中于
    # 动量最确定的名字。经济逻辑：动量因子存在截面持续性（Jegadeesh-Titman），
    # 头部名字的跟随收益更可靠。默认关闭：须经 IS/OOS + 多折 walk-forward
    # 严格验证（OOS alpha 为正且多折稳健）后才并入生产。
    momentum_weight: bool = False
    # ---- 量能突破确认（入场质量门，研究用，全新特征）----
    # 与上文所有「参数级」旋钮不同，这是**信号质量层的新特征**：
    # 仅在「因子共振 + 日线闸门」都通过之后，再要求入场当日出现
    #   (1) 放量：当日成交量 > vol_confirm_mult × 近 vol_confirm_ma 日均量；
    #   (2) 贴近阶段高位：收盘价 >= 近 vol_confirm_ma 日最高价的 (1-tol)。
    # 即「放量 + 突破」才算有效突破，过滤无量假突破（whipsaw），
    # 提升胜率与盈亏比。只在 factor 入场模式生效（trend 突破模式本身已含突破语义）。
    # 默认关闭：须经 IS/OOS + 多折 walk-forward 严格验证后才并入生产。
    vol_confirm: bool = False
    vol_confirm_mult: float = 1.5     # 当日量 >= 1.5 × 近 ma 日均量
    vol_confirm_ma: int = 20          # 量/价窗口（日）
    vol_confirm_tol: float = 0.03     # 收盘价距阶段高位 <= 3% 视为突破区
    # ---- 横截面风险平价（资本倾斜，研究用，全新特征）----
    # 与 momentum_weight（按动量强弱集中资本）相反：按个股 ATR% 的**倒数**
    # 把新增资本分配给新入选名字（权重 = (1/atr_k) / Σ(1/atr)），让低波动(平静)
    # 的名字拿更多仓位、高波动(剧烈)的名字拿更少，在总风险预算不变的前提下
    # 降低组合波动、提升 Sharpe/Calmar。注意：当前 vol_sizing 已按"每笔风险预算"
    # 做了一次风险平价（高波动→每笔下注更小），本开关是在其之上**进一步**按
    # 横截面波动率倾斜资本。默认关闭：须经 IS/OOS + 多折 walk-forward 严格验证
    # 后才并入生产。
    risk_parity: bool = False
    # ---- 主力资金流 alpha（全新数据轴，研究用）----
    # 与上文所有「参数级 / 市场择时」旋钮都不同：这是**新数据**——Tushare
    # 逐日「主力资金净流入」(net_mf_amount)，捕捉机构/大户净买卖方向，
    # 与价格动量正交（经典「聪明钱」效应，A 股有独立预测力）。
    #   "off"  = 不使用资金流（原行为）
    #   "gate" = 入场质量门：动量/因子/闸门都通过后，再要求近 moneyflow_window
    #            日主力净流>0（机构在Accumulate）才开仓，过滤「无量/派发」假突破。
    #   "rank" = 横截面动量增强：在动量排名前 N 里，按 (价格动量, 主力净流)
    #            双因子加权重排，把资本向「量价齐升+机构加持」的强名字倾斜。
    # 默认关闭：须经 IS/OOS + 多折 walk-forward 严格验证（OOS alpha 为正且稳健）
    # 后才并入生产。数据来自 data/moneyflow_cache（本地磁盘缓存，零网络重复消耗）。
    moneyflow_mode: str = "off"     # "off" | "gate" | "rank"
    moneyflow_window: int = 5       # gate/rank 用的近 N 日主力净流窗口
    moneyflow_min_amount: float = 0.0   # gate：近 N 日净流阈值(元)，默认>0(净流入)
    moneyflow_weight: float = 0.30  # rank：主力净流在双因子里的权重(0~1)
    # ---- 业绩预告上修（盈利修正，全新基本面数据轴，研究用）----
    # 与 moneyflow（资金流，对价格动量仅弱相关ρ≈0.36）不同：这是**公司自身披露的
    # 前瞻盈利指引上修**（同一报告期、后一次指引高于前一次），属"盈利动量 / 预告
    # 漂移(PEAD)"经典因子，与 20 日价格动量**正交**——捕捉"预期在变好"的边际信息，
    # 而非价格已反映的趋势。这是本宇宙当前唯一尚未测试、与价格正交的另类 alpha
    # 来源（记忆点名的下一个突破点：盈利修正 / 评级；consensus/analyst_forecast/
    # stock_rating 在当前 token 档位不可用，forecast 可用且含 p_change 区间）。
    #   "off"  = 不使用上修信号（原行为）
    #   "gate" = 入场质量门：因子/闸门/动量都通过后，再要求近 earnrev_window 交易日
    #            内出现过**上修事件**(signed_rev>0)才开仓，过滤"无基本面催化"的纯技术突破。
    #   "rank" = 横截面倾斜：在动量前 N 名里，按 (价格动量分, 上修幅度) 双因子加权重排，
    #            把资本向"量价齐升 + 盈利上修"的强名字倾斜。
    # 默认关闭：须经 IS/OOS + 多折 walk-forward 严格验证（OOS alpha 为正且稳健）后才并入生产。
    # 数据来自 data/earnrev_cache（本地磁盘缓存，零网络重复消耗）。无上修数据的标的
    # 在 gate 模式下被剔除、rank 模式下 rev 记 0（不影响其它信号），降级安全。
    earnrev_mode: str = "off"       # "off" | "gate" | "rank"
    earnrev_window: int = 60        # gate/rank 用的近 N 交易日窗口（约 3 个月）
    earnrev_min: float = 0.0        # gate：上修幅度阈值(%)，默认>0（须净上修）
    earnrev_weight: float = 0.30    # rank：上修幅度在双因子里的权重(0~1)
    # ---- 组合权益回撤硬止损（全新结构性方向，研究用）----
    # 与「指数 MA60 闸门 / tailhedge 指数熔断」本质不同：后者用**指数**作代理，
    # 本旋钮直接用**组合自身权益曲线**作触发器——只有当你 actual 的账户在
    # bleeding（自峰值回撤超限）时才清仓，而非「指数跌了但我持仓没跌」时被误杀。
    # 也与 dd_ctrl（只缩新开仓、不平已有）不同：这是**硬止损**——触发即清空
    # 全部持仓转现金；净值自 trough 反弹达到 resume_pct（或创新高）才重新入场。
    # 两段式设计规避「出得来回不去」：入场门看峰值回撤、离场门看低点反弹（解耦），
    # 反弹即回场、不必回旧高峰。默认关闭：须经 IS/OOS + 多折 walk-forward 严格
    # 验证（OOS alpha 为正且多折稳健）后才并入生产。
    equity_dd_stop_enable: bool = False
    equity_dd_stop_pct: float = -0.15    # 组合净值自峰值回撤超此值 → 清仓转现金
    equity_dd_resume_pct: float = 0.10  # 防御态内自低点反弹超此值 → 解除、重开仓
    # ---- 实时 RiskManager −10% 全局暂停（live 一致性建模，研究用）----
    # 真实引擎 RiskManager.on_asset_update 在组合净值自峰值回撤 <=
    # max_drawdown_pct(-0.10) 时**永久熔断**（halted=True：只拦新开仓、不平已有、
    # 且无自动恢复——全代码无 resume() 调用）。回测默认"无暂停"，与现实之间存在
    # 建模缺口：回测给出的 Sharpe 在实盘可能因 −10% 暂停而折损。本开关在回测里
    # 忠实复现该行为，用于评估 risk_per_trade 放大后是否会触发实盘无法实现的回撤。
    #   rm_dd_pause_pct = 0.0   → 关闭（与旧回测完全一致，默认）
    #   rm_dd_pause_pct = -0.10 → 建模实盘（净值自峰值回撤 <= -10% 即熔断）
    #   rm_dd_pause_recoverable = True → 净值创新高后自动解除（建议的修复方向，
    #       用于量化"永久熔断"的机会成本，决定该旋钮是否应改为可恢复式断路器）。
    rm_dd_pause_pct: float = 0.0
    rm_dd_pause_recoverable: bool = False


def _vec_slope(series: List[float], lookback: int = 5) -> List[Optional[float]]:
    """向量化 I.slope：返回长度 n 的逐索引斜率序列。

    在每一个 i 上，值与 ``I.slope(series[:i + 1], lookback)`` 完全一致
    （I.slope 只取截断序列里最后 lookback 个有效值做线性斜率）。用于把
    daily_trend_up / score_daily 里的逐 bar 斜率重算预计算为 O(n) 序列。
    """
    n = len(series)
    out: List[Optional[float]] = [None] * n
    for i in range(n):
        valid = [v for v in series[:i + 1] if v is not None]
        if len(valid) < 2:
            continue
        tail = valid[-lookback:] if len(valid) >= lookback else valid[-2:]
        first, last = tail[0], tail[-1]
        out[i] = (last - first) / (len(tail) - 1) if first else None
    return out


def run_backtest(codes: List[str], cfg: BacktestConfig,
                 count: int = 260, preloaded: dict = None,
                 mf_data: dict = None, er_data: dict = None) -> dict:
    """日线组合回测（正确事件时序版）。

    事件时序（每个交易日 i）：
      A) 执行前一日收盘生成的挂单 —— 按 **今日开盘价** 成交
      B) 用今日 high/low/close 检查离场
      C) 用今日收盘生成信号 → 挂单，等明日开盘执行
      D) 用今日收盘标记权益，记录日收益

    这修正了原实现的两个记账错误：
      1. 原来把「次日开盘成交的持仓」在**当日**就按当日收盘估值，
         把隔夜跳空当成当日盈亏灌进日收益 → Sharpe 严重失真
         （实测 -0.94 vs 正确 +0.58）。
      2. 原来用 min(len) 截断做跨标的对齐，索引与日期不对应。
    """
    # 先加载所有数据（或复用外部预加载，避免重复网络拉取）
    data: Dict[str, Dict[str, List[float]]] = {}
    if preloaded:
        data = {c: d for c, d in preloaded.items() if c in codes}
    if not data:
        for code in codes:
            d = load_daily(code, count)
            if d:
                data[code] = d
    if not data:
        return {"error": "no data"}

    # ---- regime 用的指数一起对齐（但不参与交易/基准）----
    # 注意：调用方（opt_harness）传入的 preloaded 含指数代码，但 run_backtest
    # 内部为排除指数出交易池，把 data 限制在 codes（不含 INDEX_CODES）内，
    # 导致 regime 指数被一并过滤。这里与 tailhedge 一致，显式从 preloaded
    # 取回指数（或直接 load_daily），否则 regime_panel 为 None、闸门恒为
    # False、所有交易被静默拦截——这会伪造「regime 全输」的假象。2026-08-29 修复。
    regime_code = None
    if cfg.regime_mode in ("index", "dual"):
        regime_code = cfg.regime_index
    elif cfg.regime_mode == "tailhedge":
        regime_code = cfg.tail_index
    if regime_code is not None and regime_code not in data:
        _idx = None
        if preloaded and regime_code in preloaded:
            _idx = preloaded[regime_code]
        else:
            _idx = load_daily(regime_code, count)
        if _idx:
            data = dict(data)
            data[regime_code] = _idx
        else:
            regime_code = None      # 拿不到指数则退化为不过滤

    # ---- 跨标的对齐到统一交易日轴 ----
    dates, panel = align_panel(data)
    regime_panel = panel.pop(regime_code, None) if regime_code else None
    n = len(dates)
    if not panel:
        return {"error": "no tradable data"}

    # ---- 主力资金流（新数据轴）对齐到统一交易日轴 ----
    # moneyflow_mode != off 时才加载；mf_data 来自外部预取（回测复用，零重复网络）。
    mf_net: Dict[str, list] = {}
    if cfg.moneyflow_mode in ("gate", "rank"):
        if mf_data is None:
            from data.moneyflow_cache import preload_moneyflow
            _mf_start = dates[0] if dates else "20230101"
            _mf_end = dates[-1] if dates else ""
            mf_data = preload_moneyflow(list(panel.keys()),
                                        start=_mf_start, end=_mf_end)
        for code, d in panel.items():
            raw = mf_data.get(code, {})
            arr = [float(raw.get(dt, {}).get("net_mf_amount"))
                   if raw.get(dt) else None for dt in dates]
            mf_net[code] = arr

    # ---- 业绩预告上修（盈利修正，全新基本面数据轴）对齐到统一交易日轴 ----
    # earnrev_mode != off 时才加载；er_data 来自外部预取（回测复用，零重复网络）。
    # 与 mf_net 一致：把离散的"上修事件(ann_date -> signed_rev)"映射为逐交易日序列，
    # 仅保留窗口内(近 earnrev_window 交易日)的最近一次上修，过时归 0（避免陈旧事件常驻）。
    er_net: Dict[str, list] = {}
    if cfg.earnrev_mode in ("gate", "rank"):
        if er_data is None:
            from data.earnrev_cache import preload_earnrev
            _er_start = dates[0] if dates else "20200101"
            _er_end = dates[-1] if dates else ""
            er_data = preload_earnrev(list(panel.keys()),
                                     start=_er_start, end=_er_end)
        _win = max(1, int(cfg.earnrev_window))
        _date_to_idx = {d: k for k, d in enumerate(dates)}
        for code, d in panel.items():
            raw = er_data.get(code, {})
            # 事件列表：(交易日索引, signed_rev)，ann_date 映射到最近的不晚于它的交易日
            ev_list: List[Tuple[int, float]] = []
            for _ev_date, _rev in raw.items():
                if _ev_date in _date_to_idx:
                    _idx = _date_to_idx[_ev_date]
                else:
                    _idx = None
                    for _kk in range(len(dates) - 1, -1, -1):
                        if dates[_kk] <= _ev_date:
                            _idx = _kk
                            break
                if _idx is not None:
                    ev_list.append((_idx, float(_rev)))
            ev_list.sort()
            arr = [0.0] * n
            _ptr = 0
            _last_idx = None
            _last_rev = 0.0
            for _i in range(n):
                while _ptr < len(ev_list) and ev_list[_ptr][0] <= _i:
                    _last_idx, _last_rev = ev_list[_ptr]
                    _ptr += 1
                if _last_idx is not None and (_i - _last_idx) <= _win:
                    arr[_i] = _last_rev
                else:
                    arr[_i] = 0.0
            er_net[code] = arr

    # 预计算 MA 数组（趋势破位判定用；exit_ma 跟随配置，原来硬编码 60）
    exit_ma = max(2, int(cfg.trend_exit_ma))
    ma20_arr: Dict[str, list] = {}
    ma_exit_arr: Dict[str, list] = {}
    for code, d in panel.items():
        ma20_arr[code] = I.sma(d["close"], 20)
        ma_exit_arr[code] = I.sma(d["close"], exit_ma)

    # ---- 预计算（性能优化：把 O(n^2) 的逐 bar 指标重算改为一次 O(n)）----
    # 窗口内序列不变，原实现在每个交易日对每个候选股重算 daily_trend_up /
    # atr_pct_at / score_daily，使 run_backtest 为 O(n^2)。预计算为全序列数组
    # 后按 [i] 索引（数值与 I.last(series[:i+1]) 完全一致，已验证 base 指标不变）。
    trend_up_arr: Dict[str, list] = {}
    atr_pct_arr: Dict[str, list] = {}
    score_arr: Dict[str, list] = {}
    # 量能突破确认（vol_confirm）预计算：放量比 + 是否贴近阶段高位。
    # 同样改为 O(n) 预计算，避免 walk-forward 里逐 bar 重算。
    vol_ratio_arr: Dict[str, list] = {}
    near_high_arr: Dict[str, list] = {}
    for code, d in panel.items():
        o = d["open"]; cl = d["close"]
        hi = d["high"]; lo = d["low"]; vo = d["volume"]
        n_c = len(cl)
        # 趋势闸门布尔序列（与 daily_trend_up 等价）
        _s20 = I.sma(cl, 20); _s60 = I.sma(cl, 60)
        _s20s = _vec_slope(_s20, 5)
        _, _, _mh = I.macd(cl, 12, 26, 9)
        _tu = [False] * n_c
        for i in range(n_c):
            if i < 60:
                continue
            m20 = _s20[i]; m60 = _s60[i]; m20s = _s20s[i]
            if (m20 and m60 and cl[i] > m20 > m60
                    and m20s is not None and m20s > 0
                    and (_mh[i] or 0) >= 0):
                _tu[i] = True
        trend_up_arr[code] = _tu
        # ATR% 序列（与 atr_pct_at 等价）
        _atr = I.atr(hi, lo, cl, 14)
        _ap = [0.0] * n_c
        for i in range(n_c):
            a = _atr[i]
            if a and cl[i]:
                _ap[i] = a / cl[i]
        atr_pct_arr[code] = _ap
        # 6 因子评分序列（与 score_daily 等价）
        score_arr[code] = score_daily_series(o, hi, lo, cl, vo)
        # 量能突破确认预计算
        _vma = I.sma(vo, cfg.vol_confirm_ma)
        _vr = [0.0] * n_c
        _nh = [False] * n_c
        _vma_w = max(2, int(cfg.vol_confirm_ma))
        for i2 in range(n_c):
            vma_i = _vma[i2]
            if vma_i and vma_i > 0:
                _vr[i2] = vo[i2] / vma_i
            if i2 >= _vma_w - 1:
                win = cl[i2 - _vma_w + 1: i2 + 1]
                hi_win = max(win)
                _nh[i2] = (cl[i2] >= hi_win * (1 - cfg.vol_confirm_tol))
        vol_ratio_arr[code] = _vr
        near_high_arr[code] = _nh

    # ---- 预计算市场状态（regime）逐日布尔序列 ----
    r_ma = max(2, int(cfg.regime_ma))
    use_index = (cfg.regime_mode in ("index", "dual")) and regime_panel is not None
    use_breadth = cfg.regime_mode in ("breadth", "dual")
    idx_ok: List[bool] = [False] * n
    br_ok: List[bool] = [True] * n
    if use_index:
        icl = regime_panel["close"]
        ima = I.sma(icl, r_ma)
        buf = max(0.0, float(cfg.regime_buffer_pct))
        need = max(1, int(cfg.regime_confirm_days))
        slope_days = max(0, int(cfg.regime_slope_days))
        # 带缓冲带 + N 日确认的状态机。默认参数 (need=1,buf=0,slope=0) 下
        # 每日 up_trig == (close>MA)，翻转立即生效，与原二值实现等价。
        state = False
        run_up = run_dn = 0
        for i in range(n):
            m = I.last(ima[:i + 1])
            if not m:
                idx_ok[i] = False
                continue
            slope_ok = True
            if slope_days > 0:
                slope_ok = (I.slope(ima[:i + 1], slope_days) or 0.0) > 0
            up_trig = (icl[i] > m * (1 + buf)) and slope_ok
            dn_trig = icl[i] < m * (1 - buf)
            if up_trig:
                run_up += 1
                run_dn = 0
            elif dn_trig:
                run_dn += 1
                run_up = 0
            else:
                # 落在缓冲带内：既不确认上穿也不确认下穿，维持现状
                run_up = run_dn = 0
            if (not state) and run_up >= need:
                state = True
            elif state and run_dn >= need:
                state = False
            idx_ok[i] = state
    if use_breadth:
        ma_b = {c: I.sma(d["close"], r_ma) for c, d in panel.items()}
        for i in range(n):
            tot = above = 0
            for c, d in panel.items():
                m = I.last(ma_b[c][:i + 1])
                if m and d["close"][i] > 0:
                    tot += 1
                    if d["close"][i] > m:
                        above += 1
            br_ok[i] = bool(tot and (above / tot) >= cfg.regime_breadth_thresh)

    # ---- 尾部风险对冲（指数峰值回撤熔断）两段式状态机 ----
    # 非防御态：跟踪运行峰值 peak；自峰值回撤 <= dd_th 进入防御态（记录崩盘点 trough）。
    # 防御态：跟踪崩盘低点 trough；自低点反弹 >= rec_th（或创出新高）解除防御，
    #         并把 peak 重置为当前价（以反弹后价位为新的参考高峰）。
    # 入场看「峰值回撤」、离场看「低点反弹」——解耦，避免长期踏空反弹。
    tail_defensive = [False] * n
    if cfg.regime_mode == "tailhedge" and regime_panel is not None:
        icl = regime_panel["close"]
        dd_th = float(cfg.tail_drawdown_pct)
        rec_th = max(0.0, float(cfg.tail_recover_pct))
        peak = None
        trough = None
        state = False
        for i in range(n):
            c = icl[i]
            if c and c > 0:
                if not state:
                    if peak is None or c > peak:
                        peak = c
                    dd = (c / peak - 1.0) if peak else 0.0
                    if peak and dd <= dd_th:
                        state = True
                        trough = c          # 进入防御，开始跟踪崩盘低点
                else:
                    if trough is None or c < trough:
                        trough = c
                    bounce = (c / trough - 1.0) if trough else 0.0
                    new_high = (peak is not None and c > peak)
                    if (trough and bounce >= rec_th) or new_high:
                        state = False
                        peak = c            # 重置参考峰值为当前价
                        trough = None
                tail_defensive[i] = state

    if cfg.regime_mode == "index":
        regime_ok = list(idx_ok)
    elif cfg.regime_mode == "breadth":
        regime_ok = list(br_ok)
    elif cfg.regime_mode == "dual":
        # 双过滤：指数站上 MA60「且」宽度健康，才放行。
        # 比单指数更抗个股/指数失真，避免弱势反弹里误开仓。
        regime_ok = [a and b for a, b in zip(idx_ok, br_ok)]
    elif cfg.regime_mode == "tailhedge":
        # 防御态=不开新仓；regime_ok[i] = not defensive[i]
        regime_ok = [not d for d in tail_defensive]
    else:
        regime_ok = [True] * n

    equity = 1_000_000.0
    cash = equity
    positions: Dict[str, dict] = {}   # code -> {qty, entry, entry_day, peak}
    last_exit_day: Dict[str, int] = {}  # code -> 最后离场日（再入场冷静期用）
    pending: List[dict] = []          # 待明日开盘执行的买单
    equity_curve: List[float] = [equity]
    trades: List[dict] = []
    daily_rets: List[float] = []
    days_in_market = 0

    WARMUP = max(65, int(cfg.momentum_lookback) + 5, exit_ma + 5,
                 int(cfg.min_warmup))
    if n <= WARMUP + 10:
        return {"error": f"data too short: n={n} warmup={WARMUP}"}

    prev_equity = equity
    peak_equity = equity          # 组合级 DD 控制：跟踪净值峰值
    # 组合权益回撤硬止损状态机（研究用，默认关闭）
    eq_dd_active = False
    eq_dd_trough = None
    # 实时 RiskManager −10% 全局暂停建模（研究用，默认关闭）：
    # 净值自峰值回撤 <= rm_dd_pause_pct 即熔断，只拦新开仓、不平已有。
    rm_halted = False
    rm_halted_ever = False
    rm_halted_days = 0
    for i in range(WARMUP, n):
        # ================= A) 执行昨日挂单（今日开盘成交） =================
        # 组合级动态暴露（DD control）：仅在「新开仓」上缩放目标仓位，
        # 不强制平已有持仓。净值创新高后 exp_scale 自动回到 1.0。
        exp_scale = 1.0
        if cfg.dd_ctrl:
            dd = (equity / peak_equity - 1.0) if peak_equity > 0 else 0.0
            if dd < cfg.dd_ctrl_start:
                frac = min(1.0, (cfg.dd_ctrl_start - dd)
                           / (cfg.dd_ctrl_start - cfg.dd_ctrl_full))
                exp_scale = (cfg.dd_ctrl_floor
                             + (1.0 - cfg.dd_ctrl_floor) * (1.0 - frac))
        # 组合权益回撤硬止损（防御态）：不执行任何买单，待净值反弹再恢复。
        _pending_exec = pending
        if cfg.equity_dd_stop_enable and eq_dd_active:
            _pending_exec = []
        for order in _pending_exec:
            code = order["code"]
            if code in positions or len(positions) >= cfg.max_positions:
                continue
            if not panel[code]["valid"][i]:
                continue          # 停牌不成交
            entry = panel[code]["open"][i]
            if entry <= 0:
                continue
            stop_dist = order["stop_dist"]
            target_dist = order["target_dist"]
            wf = order.get("wf", 1.0)   # 动量排名加权因子（均值=1，强者>1弱<1）
            rpw = order.get("weight", 1.0)  # 横截面风险平价权重（均值=1，低波动>1）
            # 仓位（波动率目标）—— 用昨日收盘标记的权益，无未来函数
            if cfg.vol_sizing:
                budget = equity * cfg.risk_per_trade
                tgt = min(budget / stop_dist, equity * 0.30, cfg.fixed_amount)
            else:
                tgt = min(cfg.fixed_amount, equity * 0.30)
            tgt = tgt * wf * rpw
            # 组合级 DD 控制：缩放新开仓目标仓位（exp_scale<=1.0）
            if cfg.dd_ctrl and exp_scale < 1.0:
                tgt = tgt * exp_scale
            qty = int(tgt // (entry * 100)) * 100
            if qty <= 0:
                continue
            cost = qty * entry * (1 + cfg.cost_pct)
            if cost > cash:
                qty = int((cash / (1 + cfg.cost_pct)) // (entry * 100)) * 100
                if qty <= 0:
                    continue
                cost = qty * entry * (1 + cfg.cost_pct)
            cash -= cost
            positions[code] = {
                "qty": qty, "entry": entry, "entry_day": i,
                "peak": entry,
                "stop_price": entry * (1 - stop_dist),
                "target_price": entry * (1 + target_dist),
            }
        pending = []

        # ================= B) 离场检查（今日 high/low/close） =================
        for code in list(positions.keys()):
            pos = positions[code]
            if not panel[code]["valid"][i]:
                continue          # 停牌：持仓不动
            hi = panel[code]["high"][i]
            lo = panel[code]["low"][i]
            cl = panel[code]["close"][i]
            entry = pos["entry"]
            pos["peak"] = max(pos["peak"], hi)
            exit_price = None
            reason = ""
            # 组合权益回撤硬止损（防御态）：清仓全部持仓转现金
            if (exit_price is None and cfg.equity_dd_stop_enable
                    and eq_dd_active):
                exit_price, reason = cl, "equity_dd_stop"
            if cfg.exit_mode == "trend":
                # 趋势骑行：MA20 下穿 exit_ma 或 收盘跌破 exit_ma → 离场
                _m20 = I.last(ma20_arr[code][:i + 1])
                _mex = I.last(ma_exit_arr[code][:i + 1])
                if (_m20 and _mex and _m20 < _mex) or (cl < _mex if _mex else False):
                    exit_price, reason = cl, "trend_break"
                elif (cl / entry - 1) <= cfg.hard_stop_pct:
                    exit_price, reason = cl, "hard_stop"
                elif (i >= 1 and panel[code]["close"][i - 1] > 0
                      and (cl / panel[code]["close"][i - 1] - 1) * 100
                      <= cfg.down_day_exit_pct):
                    exit_price, reason = cl, "crash"
                elif cfg.trend_trailing and entry > 0:
                    # 宽幅移动止损：浮盈足够后从峰值回撤即锁定利润
                    pnl = cl / entry - 1
                    if pnl >= cfg.trend_trail_activation:
                        trail = pos["peak"] * (1 + cfg.trend_trail_stop)
                        floor = entry * (1 + cfg.trend_trail_floor)
                        if trail < floor:
                            trail = floor
                        if lo <= trail:
                            exit_price, reason = trail, "trend_trail"
            elif cfg.chandelier:
                # 吊灯止损：stop = 峰值 - atr_stop_mult * ATR；随峰值上移
                ap = atr_pct_at(panel[code]["high"][:i + 1],
                                panel[code]["low"][:i + 1],
                                panel[code]["close"][:i + 1], i)
                if ap > 0:
                    chand = pos["peak"] * (1 - cfg.atr_stop_mult * ap)
                    if chand > pos["stop_price"]:
                        pos["stop_price"] = chand
                if lo <= pos["stop_price"]:
                    exit_price, reason = pos["stop_price"], "chandelier"
            else:
                # 旧逻辑：固定止损价 + 目标价 + 可选移动止损
                sl_price = pos["stop_price"]
                if lo <= sl_price:
                    exit_price, reason = sl_price, "stop_loss"
                tp_price = pos["target_price"]
                if exit_price is None and hi >= tp_price:
                    exit_price, reason = tp_price, "take_profit"
                if exit_price is None and cfg.trailing and entry > 0:
                    if (pos["peak"] - entry) / entry >= cfg.trailing_activation:
                        trail = pos["peak"] * (1 + cfg.trailing_stop)
                        floor = entry * (1 + cfg.trailing_floor)
                        trail = max(trail, floor)
                        if lo <= trail:
                            exit_price, reason = trail, "trailing"
            # 市场状态转差 → 强制清仓（防御，可选）
            # tailhedge 模式：防御态恒强制清仓（资本保全），与 regime_force_exit 解耦
            if (exit_price is None
                    and (cfg.regime_force_exit or cfg.regime_mode == "tailhedge")
                    and not regime_ok[i]):
                exit_price, reason = cl, "regime_exit"
            # 超时（趋势模式放长到 trend_max_hold_days）
            _hold_cap = (cfg.trend_max_hold_days if cfg.exit_mode == "trend"
                         else cfg.max_hold_days)
            if exit_price is None and (i - pos["entry_day"]) >= _hold_cap:
                exit_price, reason = cl, "timeout"
            if exit_price is not None:
                cash += pos["qty"] * exit_price * (1 - cfg.cost_pct)
                net_entry = entry * (1 + cfg.cost_pct)
                net_exit = exit_price * (1 - cfg.cost_pct)
                trades.append({
                    "code": code,
                    "pnl_pct": (net_exit - net_entry) / net_entry,
                    "hold": i - pos["entry_day"], "reason": reason,
                })
                last_exit_day[code] = i   # 再入场冷静期：记录最后离场日
                del positions[code]

        # ================= C) 生成信号（今日收盘）→ 明日开盘执行 =================
        # 市场状态闸门：状态不佳时不开新仓（已有持仓按原规则管理）
        # rm_halted：实时 RiskManager −10% 全局暂停（忠实建模 live 行为），
        # 熔断期间只拦新开仓，已有持仓照常走离场逻辑。
        if (len(positions) < cfg.max_positions and i < n - 1 and regime_ok[i]
                and not (cfg.equity_dd_stop_enable and eq_dd_active)
                and not rm_halted):
            # 动量闸门：只交易动量前 N 名（且动量>0）
            allowed = None
            mom_val: Dict[str, float] = {}
            if cfg.momentum_rank:
                lb = int(cfg.momentum_lookback)
                # 指数区间涨幅（residual 口径用；取不到则退化为 raw）
                idx_ret = None
                if cfg.mom_metric == "residual" and regime_panel:
                    _ic = regime_panel["close"]
                    if i >= lb and _ic[i] > 0 and _ic[i - lb] > 0:
                        idx_ret = _ic[i] / _ic[i - lb] - 1
                moms = []
                for code, d in panel.items():
                    cc = d["close"]
                    if i < lb or cc[i] <= 0:
                        continue
                    base = cc[i - lb]
                    if base <= 0:
                        continue
                    raw = cc[i] / base - 1
                    if raw <= 0:
                        continue          # 入池条件不变：原始动量必须为正
                    if cfg.mom_metric == "sharpe":
                        seg = cc[i - lb:i + 1]
                        rr = [seg[k] / seg[k - 1] - 1
                              for k in range(1, len(seg)) if seg[k - 1] > 0]
                        if len(rr) < 10:
                            continue
                        mu = sum(rr) / len(rr)
                        sd = math.sqrt(sum((x - mu) ** 2 for x in rr) / len(rr))
                        val = (mu / sd) if sd > 0 else 0.0
                    elif cfg.mom_metric == "residual" and idx_ret is not None:
                        val = raw - idx_ret
                    else:
                        val = raw
                    moms.append((val, code))
                moms.sort(reverse=True)
                allowed = set(c for _, c in moms[:cfg.momentum_top_n])
                mom_val = {c: v for v, c in moms}
            scored = []
            for code, d in panel.items():
                if code in positions:
                    continue
                if allowed is not None and code not in allowed:
                    continue
                # 再入场冷静期：近期刚离场的标的，间隔不足则不重新开仓，
                # 降低动量重排名造成的无谓换手、削减交易成本侵蚀。
                if (cfg.reentry_cooldown > 0 and code in last_exit_day
                        and (i - last_exit_day[code]) < cfg.reentry_cooldown):
                    continue
                if not d["valid"][i]:
                    continue      # 停牌股不产生信号
                c, h, l, v = d["close"], d["high"], d["low"], d["volume"]
                # 量能突破确认（全新特征，研究用）：放量 + 贴近阶段高位
                # 才视为有效突破，过滤无量假突破（whipsaw）。仅 factor 模式生效。
                if cfg.vol_confirm and cfg.entry_mode != "trend":
                    if (vol_ratio_arr[code][i] < cfg.vol_confirm_mult
                            or not near_high_arr[code][i]):
                        continue
                if cfg.entry_mode == "trend":
                    # 日线突破追涨：处于日线主升 + 价格逼近 20 日新高
                    if not trend_up_arr[code][i]:
                        continue
                    hi20 = max(c[max(0, i - 20):i + 1])
                    if c[i] < hi20 * 0.98:
                        continue
                    score = 10.0
                else:
                    score, factors = score_arr[code][i]
                    if score < cfg.buy_score_threshold:
                        continue
                    if sum(1 for x in factors.values() if x > 0) < cfg.min_signals:
                        continue
                    if cfg.use_gate and not trend_up_arr[code][i]:
                        continue
                scored.append((score, code))
            scored.sort(key=lambda x: x[0], reverse=True)

            # ---- 主力资金流：入场质量门(gate) / 双因子重排(rank) ----
            if cfg.moneyflow_mode in ("gate", "rank") and mf_net:
                w = max(1, int(cfg.moneyflow_window))
                lo = max(0, i - w + 1)
                if cfg.moneyflow_mode == "gate":
                    # 质量门：要求近 w 日主力净流 > 阈值（机构 Accumulate），
                    # 过滤「无量 / 派发」假突破。无数据则降级为不设防。
                    thr = float(cfg.moneyflow_min_amount)
                    gated = []
                    for sc, code in scored:
                        arr = mf_net.get(code)
                        if arr is None:
                            gated.append((sc, code))
                            continue
                        seg = [x for x in arr[lo:i + 1] if x is not None]
                        trailing = sum(seg) if seg else 0.0
                        if trailing > thr:
                            gated.append((sc, code))
                    scored = gated
                else:  # rank：因子分排名 + 主力净流排名 加权
                    items = list(scored)
                    by_score = sorted(items, key=lambda t: t[0], reverse=True)
                    score_rank = {c: r for r, (_, c) in enumerate(by_score)}
                    flow_vals = {}
                    for sc, code in items:
                        arr = mf_net.get(code)
                        seg = ([x for x in arr[lo:i + 1] if x is not None]
                               if arr else [])
                        flow_vals[code] = sum(seg) if seg else 0.0
                    by_flow = sorted(items, key=lambda t: flow_vals[t[1]],
                                     reverse=True)
                    flow_rank = {c: r for r, (_, c) in enumerate(by_flow)}
                    wt = max(0.0, min(1.0, float(cfg.moneyflow_weight)))
                    n_items = len(items)
                    def _blend(t):
                        _, code = t
                        sr = score_rank.get(code, n_items)
                        fr = flow_rank.get(code, n_items)
                        return wt * fr + (1.0 - wt) * sr   # 名次小=好
                    scored = sorted(items, key=_blend)
            # ---- 业绩预告上修（盈利修正）：入场质量门(gate) / 双因子倾斜(rank) ----
            if cfg.earnrev_mode in ("gate", "rank") and er_net:
                if cfg.earnrev_mode == "gate":
                    # 质量门：近 earnrev_window 交易日内出现过"上修事件"(signed_rev>阈值)
                    # 才开仓，过滤「无基本面催化」的纯技术突破。无上修数据(rev=0)则剔除。
                    thr = float(cfg.earnrev_min)
                    gated = []
                    for sc, code in scored:
                        arr = er_net.get(code)
                        if arr is None:
                            gated.append((sc, code))   # 无数据降级为不设防
                            continue
                        if arr[i] > thr:
                            gated.append((sc, code))
                    scored = gated
                else:  # rank：因子分排名 + 上修幅度排名 加权
                    items = list(scored)
                    by_score = sorted(items, key=lambda t: t[0], reverse=True)
                    score_rank = {c: r for r, (_, c) in enumerate(by_score)}
                    rev_vals = {}
                    for sc, code in items:
                        arr = er_net.get(code)
                        rev_vals[code] = arr[i] if arr is not None else 0.0
                    by_rev = sorted(items, key=lambda t: rev_vals[t[1]],
                                    reverse=True)
                    rev_rank = {c: r for r, (_, c) in enumerate(by_rev)}
                    wt = max(0.0, min(1.0, float(cfg.earnrev_weight)))
                    n_items = len(items)

                    def _blend_er(t):
                        _, code = t
                        sr = score_rank.get(code, n_items)
                        rr = rev_rank.get(code, n_items)
                        return wt * rr + (1.0 - wt) * sr   # 名次小=好
                    scored = sorted(items, key=_blend_er)
            # ---- 分行业分散持仓（max_per_sector>0 时生效）----
            # 在"总持仓上限"之内，进一步限制同一产业链环节最多持几只，
            # 强制组合跨环节铺开、降低环节内隐性集中风险。
            # 默认 max_per_sector=0 → 退化为原"取前 N 名"行为。
            picks = []
            if cfg.max_per_sector > 0:
                _held_sec: Dict[str, int] = {}
                for _c in positions:
                    _s = SECTOR_OF.get(_c)
                    _held_sec[_s] = _held_sec.get(_s, 0) + 1
                _sel_sec: Dict[str, int] = {}
                _slots = cfg.max_positions - len(positions)
                for _score, _code in scored:
                    if len(picks) >= _slots:
                        break
                    _s = SECTOR_OF.get(_code)
                    _cur = _held_sec.get(_s, 0) + _sel_sec.get(_s, 0)
                    if _s is not None and _cur >= cfg.max_per_sector:
                        continue
                    picks.append((_score, _code))
                    _sel_sec[_s] = _sel_sec.get(_s, 0) + 1
            else:
                picks = scored[:cfg.max_positions - len(positions)]
            # 动量排名加权：在前 N 名新入选里，按动量降序赋 rank-weight
            # （最强者更大、最弱者更小，均值=1），把资本集中于动量最确定的名字。
            wf_map: Dict[str, float] = {}
            if cfg.momentum_weight and cfg.momentum_rank and mom_val and picks:
                ranked = sorted(picks, key=lambda sc: mom_val.get(sc[1], 0.0),
                                reverse=True)
                k = len(ranked)
                mean_u = (k + 1) / 2.0
                for rank, (_, code) in enumerate(ranked, start=1):
                    wf_map[code] = (k - rank + 1) / mean_u
            # 横截面风险平价（资本倾斜，全新特征，研究用）：新入选名字按 ATR%
            # 倒数分配权重（低波动拿更多、高波动拿更少），在总风险预算不变下
            # 降低组合波动。权重之和=1，单名权重<=1，仅缩小、不放大，安全。
            rp_map: Dict[str, float] = {}
            if cfg.risk_parity and picks:
                _inv = {}
                for _, code in picks:
                    a = atr_pct_arr[code][i]
                    if a and a > 0:
                        _inv[code] = 1.0 / a
                if _inv:
                    _tot = sum(_inv.values())
                    for code, v in _inv.items():
                        rp_map[code] = v / _tot
            for score, code in picks:
                d = panel[code]
                ap = atr_pct_arr[code][i]
                if ap <= 0:
                    ap = abs(cfg.stop_loss)
                # 仓位用真实止损距离（趋势模式开启 trend_vol_sizing 时）
                if cfg.exit_mode == "trend" and cfg.trend_vol_sizing:
                    # 真实趋势止损 = 宽幅硬止损 或 atr×6（与引擎 _handle_buy 一致）
                    stop_dist = max(abs(cfg.hard_stop_pct), ap * 6.0)
                else:
                    stop_dist = max(abs(cfg.stop_loss), ap * cfg.atr_stop_mult)
                pending.append({
                    "code": code,
                    "stop_dist": stop_dist,
                    "target_dist": max(abs(cfg.take_profit), ap * cfg.tp_atr_mult),
                    "wf": wf_map.get(code, 1.0),
                    "weight": rp_map.get(code, 1.0),
                })

        # ================= D) 标记权益（今日收盘） =================
        mv = 0.0
        for code, pos in positions.items():
            mv += pos["qty"] * panel[code]["close"][i]
        equity = cash + mv
        if equity > peak_equity:          # 组合级 DD 控制：更新净值峰值
            peak_equity = equity
        # ---- 实时 RiskManager −10% 全局暂停建模（忠实复现 live 行为）----
        # on_asset_update 在净值自峰值回撤 <= max_drawdown_pct 时永久熔断，
        # 无自动恢复。recoverable=True 时净值创新高即解除（建议的修复方向）。
        if cfg.rm_dd_pause_pct < 0 and equity > 0:
            _rm_dd = equity / peak_equity - 1.0
            if rm_halted:
                if cfg.rm_dd_pause_recoverable and equity >= peak_equity:
                    rm_halted = False      # 创新高解除（可恢复式断路器）
            elif _rm_dd <= cfg.rm_dd_pause_pct:
                rm_halted = True
                rm_halted_ever = True
        if rm_halted:
            rm_halted_days += 1
        # ---- 组合权益回撤硬止损状态机（两段式）----
        # 入场门看「峰值回撤」：净值自峰值回撤超 stop_pct → 进入防御态（清仓）。
        # 离场门看「低点反弹」：防御态内自 trough 反弹超 resume_pct 或创新高
        # → 解除防御、以反弹后价位重置参考峰值。解耦规避「出得来回不去」。
        if cfg.equity_dd_stop_enable:
            if eq_dd_active:
                if eq_dd_trough is None or equity < eq_dd_trough:
                    eq_dd_trough = equity
                bounce = (equity / eq_dd_trough - 1.0) if eq_dd_trough > 0 else 0.0
                if bounce >= cfg.equity_dd_resume_pct or equity >= peak_equity:
                    eq_dd_active = False
                    eq_dd_trough = None
                    peak_equity = equity   # 重置参考峰值到反弹后价位
            else:
                cur_dd = (equity / peak_equity - 1.0) if peak_equity > 0 else 0.0
                if cur_dd <= cfg.equity_dd_stop_pct:
                    eq_dd_active = True
                    eq_dd_trough = equity
        equity_curve.append(equity)
        if prev_equity > 0:
            daily_rets.append(equity / prev_equity - 1)
        prev_equity = equity
        if positions:
            days_in_market += 1

    # ============================== 指标 ==============================
    def _curve_stats(curve: List[float], periods: int) -> dict:
        tot = curve[-1] / curve[0] - 1 if curve and curve[0] else 0.0
        yrs = periods / 245.0
        cg = ((curve[-1] / curve[0]) ** (1 / yrs) - 1) if (yrs > 0 and curve[0] > 0) else 0.0
        pk, dd = curve[0], 0.0
        for e in curve:
            pk = max(pk, e)
            dd = min(dd, e / pk - 1)
        rr = [curve[k] / curve[k - 1] - 1
              for k in range(1, len(curve)) if curve[k - 1] > 0]
        m = sum(rr) / len(rr) if rr else 0.0
        vr = (sum((x - m) ** 2 for x in rr) / len(rr)) if len(rr) > 1 else 0.0
        sd = math.sqrt(vr)
        sh = (m / sd * math.sqrt(245)) if sd > 0 else 0.0
        # Sortino：只罚下行波动
        dn = [x for x in rr if x < 0]
        dsd = math.sqrt(sum(x * x for x in dn) / len(dn)) if dn else 0.0
        so = (m / dsd * math.sqrt(245)) if dsd > 0 else 0.0
        return {"total_return": tot, "cagr": cg, "max_drawdown": dd,
                "sharpe": sh, "sortino": so, "vol": sd * math.sqrt(245)}

    bars = n - WARMUP
    st = _curve_stats(equity_curve, bars)

    # ---- 等权买入持有基准（同窗口，含一次买入成本）----
    bench_curve = [1_000_000.0]
    valid_codes = [c for c in panel
                   if panel[c]["close"][WARMUP] > 0 and panel[c]["valid"][WARMUP]]
    if valid_codes:
        w = 1_000_000.0 / len(valid_codes) * (1 - cfg.cost_pct)
        base_px = {c: panel[c]["close"][WARMUP] for c in valid_codes}
        for i in range(WARMUP, n):
            tot = sum(w * panel[c]["close"][i] / base_px[c] for c in valid_codes)
            bench_curve.append(tot)
    bst = _curve_stats(bench_curve, bars)

    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] <= 0]
    gross_win = sum(t["pnl_pct"] for t in wins)
    gross_loss = abs(sum(t["pnl_pct"] for t in losses))
    reasons: Dict[str, int] = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1

    return {
        "config": cfg,
        "n_stocks": len(panel),
        "n_bars": bars,
        "start_date": dates[WARMUP] if n > WARMUP else "",
        "end_date": dates[-1] if dates else "",
        "start_equity": equity_curve[0],
        "end_equity": equity_curve[-1],
        "total_return": st["total_return"],
        "cagr": st["cagr"],
        "max_drawdown": st["max_drawdown"],
        "sharpe": st["sharpe"],
        "sortino": st["sortino"],
        "vol": st["vol"],
        "calmar": (st["cagr"] / abs(st["max_drawdown"])) if st["max_drawdown"] else 0.0,
        "n_trades": len(trades),
        "win_rate": len(wins) / len(trades) if trades else 0,
        "avg_win": (gross_win / len(wins)) if wins else 0,
        "avg_loss": (-gross_loss / len(losses)) if losses else 0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else 0.0,
        "avg_hold": (sum(t["hold"] for t in trades) / len(trades)) if trades else 0,
        "exposure": days_in_market / bars if bars else 0.0,
        "exit_reasons": reasons,
        "equity_curve": equity_curve,
        # 基准
        "bench_return": bst["total_return"],
        "bench_sharpe": bst["sharpe"],
        "bench_mdd": bst["max_drawdown"],
        "bench_cagr": bst["cagr"],
        "bench_curve": bench_curve,
        "alpha": st["total_return"] - bst["total_return"],
        # 实时 RiskManager −10% 全局暂停建模指标（研究用，默认 0）
        "rm_halted_ever": rm_halted_ever,
        "rm_halted_days": rm_halted_days,
    }


# ============================================================ 入口

def _cfg_from_preset(name: str) -> BacktestConfig:
    if name == "baseline":
        # 原策略语义：无趋势闸门、固定 3%/10%、无移动止损、固定 5 万仓位、不计成本
        return BacktestConfig(
            use_gate=False, stop_loss=-0.03, take_profit=0.10,
            atr_stop_mult=0.0, tp_atr_mult=0.0,
            trailing=False, vol_sizing=False, max_hold_days=5,
            cost_pct=0.0,
        )
    # optimized（本次优化）：日线闸门 + 趋势骑行退出 + 动量排名 + 信念仓位 + 成本
    return BacktestConfig(
        use_gate=True, stop_loss=-0.04, take_profit=0.12,
        atr_stop_mult=2.0, tp_atr_mult=4.0,
        trailing=True, trailing_activation=0.06, trailing_stop=-0.03,
        trailing_floor=-0.005, vol_sizing=True,
        max_hold_days=20, cost_pct=0.0015, chandelier=False,
        down_day_exit_pct=-9.0,
        # ---- 本次优化核心 ----
        exit_mode="trend", trend_exit_ma=60, hard_stop_pct=-0.18,
        trend_max_hold_days=120,
        momentum_rank=True, momentum_top_n=6, momentum_lookback=60,
        risk_per_trade=0.02, fixed_amount=300000.0,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["baseline", "optimized", "both"],
                    default="both")
    ap.add_argument("--code", default=None, help="只回测单只（用 UNIVERSE 名称）")
    ap.add_argument("--top", type=int, default=0,
                    help="只取市值/表现靠前的 N 只（默认全宇宙）")
    args = ap.parse_args()

    if args.code:
        codes = [args.code]
    else:
        codes = list(STOCK_CODES)

    modes = (["baseline", "optimized"] if args.mode == "both"
             else [args.mode])

    print("=" * 78)
    print(f"日线回测  |  标的数={len(codes)}  |  时间≈{datetime.now():%Y-%m-%d}")
    print("=" * 78)

    results = {}
    for m in modes:
        cfg = _cfg_from_preset(m)
        r = run_backtest(codes, cfg)
        results[m] = r
        if "error" in r:
            print(f"[{m}] 错误: {r['error']}")
            continue
        print(f"\n### {m.upper()} ###")
        print(f"  期末资产 : {r['end_equity']:,.0f}  (起始 {r['start_equity']:,.0f})")
        print(f"  累计收益 : {r['total_return']*100:+.2f}%")
        print(f"  年化收益 : {r['cagr']*100:+.2f}%")
        print(f"  最大回撤 : {r['max_drawdown']*100:.2f}%")
        print(f"  Sharpe   : {r['sharpe']:.2f}")
        print(f"  交易次数 : {r['n_trades']}  胜率 {r['win_rate']*100:.1f}%"
              f"  均盈 {r['avg_win']*100:+.2f}% / 均亏 {r['avg_loss']*100:+.2f}%")
        print(f"  平均持仓 : {r['avg_hold']:.1f} 天")

    if args.mode == "both" and "baseline" in results and "optimized" in results:
        b, o = results["baseline"], results["optimized"]
        if "total_return" in b and "total_return" in o:
            print("\n--- 对比（优化 - 原）---")
            print(f"  累计收益 : { (o['total_return']-b['total_return'])*100:+.2f} pt")
            print(f"  年化收益 : { (o['cagr']-b['cagr'])*100:+.2f} pt")
            print(f"  最大回撤 : { (o['max_drawdown']-b['max_drawdown'])*100:+.2f} pt"
                  f"（负=更优）")
            print(f"  Sharpe   : { o['sharpe']-b['sharpe']:+.2f}")
            print(f"  胜率     : { (o['win_rate']-b['win_rate'])*100:+.1f} pt")


if __name__ == "__main__":
    main()

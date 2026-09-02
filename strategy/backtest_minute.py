# -*- coding: utf-8 -*-
"""
分钟级回测引擎（qmtIDE-deepseek 自实现版）

为什么需要：
  日级回测（backtest_daily.py）使用的是日线 K 线 + 自定义的 score_daily 因子；
  但生产引擎（EventEngine._run_once → TrendStrategy.on_bars）跑的是
  **分钟线** + 同名 6 因子。这两条路径用同一份 score 阈值（4.0），
  却在不同的时间尺度上做决策 —— 等于**回测验证了一条路，跑的是另一条**。
  此前所有「+162% / Sharpe 1.69」的结论从未真正验证过 live 路径。

本模块让两条路统一：
  ① 直接复用 TrendStrategy（生产用同一份代码）。
  ② 直接复用 Bar / Signal / Position dataclass。
  ③ 资金、组合、风险、T+1 在日级边界上执行（细节见各 _* 函数）。

设计约束：
  · 分钟级 step 是 1 分钟（240/日），一年 ≈ 58K bars/stock × 12 = ~700K 调用。
  · TrendStrategy 自带指标缓存（基于 bar 序列指纹），重复 step 无成本。
  · T+1 在**日切**时检查（不允许当日买当日卖）；离场在分钟粒度上判断。
  · 回测窗口短于 backtest_daily（只有约 244 天数据）—— 这是 live 标的池
    1m 数据可获取的现实，不是工程问题；walk-forward 折数会自动缩减。

用法（conda env qmt）：
    python strategy/backtest_minute.py --folds 7
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from config.settings import STRATEGY_PARAMS        # noqa: E402
from core.data_models import Bar, Fill, Position, Signal  # noqa: E402
from strategy.daily_context import DailyContext    # noqa: E402
from strategy.trend_strategy import TrendStrategy   # noqa: E402

logger = logging.getLogger("backtest_minute")

DEFAULT_COST_PCT = 0.0015            # 单边成本（佣金+印花税+滑点，A 股惯例）
# 生产仓位计算（risk_per_trade / max_pct）以单标的 max_position_amount=300000 为准。
# 分钟回测里**简化**为 fixed_amount=100000（一手价值 ~10万，足以覆盖候选池里
# 所有高价股），与生产相比敞口略保守；如需与生产完全一致可改用 PositionSizer。
DEFAULT_FIXED_AMOUNT = 100_000.0
DEFAULT_INITIAL_CASH = 1_000_000.0
DEFAULT_MAX_POSITIONS = 5
DEFAULT_BUY_THRESHOLD = 4.0


# ============================================================ 1m 数据加载

def _to_bars(raw_rows: List[Tuple]) -> Optional[Dict[str, List]]:
    """xtdata 返回的是 [(ts, open, high, low, close, volume, amount), ...]。
    转成回测用的字典结构 + 按日期拆分。"""
    out = {"open": [], "high": [], "low": [], "close": [],
           "volume": [], "amount": [], "ts": [], "date": [], "time": []}
    for row in raw_rows:
        if not row or len(row) < 6:
            continue
        try:
            ts_val = row[0]
            if isinstance(ts_val, str):
                dt = datetime.strptime(ts_val[:14], "%Y%m%d%H%M%S")
            elif isinstance(ts_val, (int, float)):
                if ts_val > 1e12:
                    dt = datetime.fromtimestamp(ts_val / 1000)
                else:
                    dt = datetime.fromtimestamp(ts_val)
            else:
                continue
            o, h, l, c, v = (float(x) for x in row[1:6])
            if c <= 0:
                continue
            out["ts"].append(dt)
            out["date"].append(dt.strftime("%Y%m%d"))
            out["time"].append(dt.strftime("%H%M"))
            out["open"].append(o); out["high"].append(h); out["low"].append(l)
            out["close"].append(c)
            out["volume"].append(int(v))
            out["amount"].append(row[6] if len(row) > 6 else float(v) * c)
        except Exception:
            continue
    if not out["close"]:
        return None
    return out


def load_minute(code: str, count: int = 60000) -> Optional[Dict[str, List]]:
    """从 xtdata 加载 1m K 线（无网络；走本地缓存）。

    返回格式与 backtest_daily.py 的 load_daily 兼容：
        {"date": ["20260901", ...], "time": ["0931", ...],
         "open": [...], "high": [...], "low": [...],
         "close": [...], "volume": [...], "amount": [...],
         "ts": [datetime, ...]}

    实测 xtdata.get_market_data_ex 返回结构：
        ① DataFrame，索引是 YYYYMMDDHHMMSS 数字/字符串，列 = field_list；
        ② 或嵌套 dict。两种都处理。
    """
    try:
        from xtquant import xtdata  # type: ignore
        raw = xtdata.get_market_data_ex(
            field_list=["time", "open", "high", "low", "close", "volume", "amount"],
            stock_list=[code], period="1m", count=count)
        if not raw or code not in raw:
            return None
        d = raw[code]
        # DataFrame 路径（实测主要走这条）
        if hasattr(d, "columns") and hasattr(d, "index"):
            out = {"open": [], "high": [], "low": [], "close": [],
                   "volume": [], "amount": [], "ts": [], "date": [], "time": []}
            for ts_idx, row in d.iterrows():
                try:
                    if isinstance(ts_idx, str):
                        dt = datetime.strptime(ts_idx[:14], "%Y%m%d%H%M%S")
                    elif isinstance(ts_idx, (int, float)):
                        v = int(ts_idx)
                        if v > 10**12:
                            dt = datetime.fromtimestamp(v / 1000)
                        else:
                            dt = datetime.strptime(str(v)[:14], "%Y%m%d%H%M%S")
                    else:
                        continue
                    c = float(row["close"])
                    if c <= 0:
                        continue
                    out["ts"].append(dt)
                    out["date"].append(dt.strftime("%Y%m%d"))
                    out["time"].append(dt.strftime("%H%M"))
                    out["open"].append(float(row.get("open", c)))
                    out["high"].append(float(row.get("high", c)))
                    out["low"].append(float(row.get("low", c)))
                    out["close"].append(c)
                    out["volume"].append(int(row.get("volume", 0)))
                    out["amount"].append(float(row.get("amount", 0)))
                except Exception:
                    continue
            return out if out["close"] else None
        # dict / list of tuples 路径（备用）
        return _to_bars(list(d) if not isinstance(d, dict) else
                        list(d.values()))
    except Exception as e:
        logger.warning("load_minute %s 失败: %s", code, e)
        return None


def load_minute_universe(codes: List[str],
                         count: int = 60000) -> Dict[str, Dict[str, List]]:
    """批量加载多只标的的 1m K 线。"""
    out = {}
    for c in codes:
        d = load_minute(c, count)
        if d:
            out[c] = d
    return out


# ============================================================ 配置

@dataclass
class MinuteConfig:
    cost_pct: float = DEFAULT_COST_PCT
    initial_cash: float = DEFAULT_INITIAL_CASH
    max_positions: int = DEFAULT_MAX_POSITIONS
    fixed_amount: float = DEFAULT_FIXED_AMOUNT
    buy_threshold: float = DEFAULT_BUY_THRESHOLD
    min_signals: int = 3
    use_daily_gate: bool = True              # 用 DailyContext 做日级闸门
    t1_restriction: bool = True             # T+1：A 股当日买次日才能卖
    exit_mode: str = "trend"                 # 复用 production 同名参数
    hard_stop_pct: float = -0.18
    trend_exit_ma: int = 60
    trend_max_hold_days: int = 30           # 分钟回测取 30 天够 walk-forward
    max_bars_per_code: int = 120            # 喂给 TrendStrategy 的最大 bar 数

    # 与 production STRATEGY_PARAMS 兼容
    strategy_params: dict = field(default_factory=dict)


# ============================================================ 单次回测

@dataclass
class TradeRecord:
    code: str
    entry_ts: str
    exit_ts: str
    entry_price: float
    exit_price: float
    qty: int
    pnl: float
    pnl_pct: float
    hold_minutes: int
    reason: str
    pnl_net: float = 0.0       # 含双边成本后的净 pnl


def run_minute_backtest(codes: List[str], cfg: MinuteConfig,
                        data: Dict[str, Dict[str, List]],
                        daily: Optional[DailyContext] = None) -> dict:
    """分钟级回测主函数。

    输入：多只标的的 1m K 线字典（同一天开始、长度对齐到 [0, n)）。
    输出：含 n_trades / total_return / sharpe / max_drawdown / trades / equity_curve 的字典。

    设计要点：
      · 决策粒度 = 1 分钟，与 live 引擎完全一致（同样的 TrendStrategy 调用路径）。
      · T+1 在**日切时**检查（不允许当日买当日卖）；离场在分钟粒度上判断。
      · 资金按 fixed_amount 等权分配（与 backtest_daily 的 fixed_amount 一致）。
      · 不复利回测（cash + market_value 平直计算）。
    """
    if not data:
        return {"error": "no data"}

    # 对齐到共同时间轴（按日期 + 时间字符串）
    # 用 (date, time) 元组作为统一索引；不存在的格子前向填充
    all_dt: set = set()
    for d in data.values():
        for dt_s, tm_s in zip(d["date"], d["time"]):
            all_dt.add((dt_s, tm_s))
    sorted_dt = sorted(all_dt)
    if not sorted_dt:
        return {"error": "no timestamps"}

    # 构造按 (code) 的快速索引
    series: Dict[str, Dict] = {}
    for code, d in data.items():
        idx = {(ds, ts): k for k, (ds, ts) in enumerate(zip(d["date"], d["time"]))}
        series[code] = {"idx": idx, **d}

    n = len(sorted_dt)
    cash = cfg.initial_cash
    equity_peak = cfg.initial_cash
    positions: Dict[str, dict] = {}        # code -> {qty, avg, entry_ts, entry_date, peak, stop_price}
    trades: List[TradeRecord] = []
    equity_curve: List[float] = []
    n_signals = 0

    # 构造策略实例（与 production 同一份代码）
    strategy_params = {**STRATEGY_PARAMS, **cfg.strategy_params}
    strategy_params["buy_score_threshold"] = cfg.buy_threshold
    strategy_params["min_signals"] = cfg.min_signals
    strategy_params["exit_mode"] = cfg.exit_mode
    strategy_params["hard_stop_pct"] = cfg.hard_stop_pct
    strategy_params["trend_exit_ma"] = cfg.trend_exit_ma
    strategy_params["trend_max_hold_days"] = cfg.trend_max_hold_days
    strategy = TrendStrategy(params=strategy_params, daily=daily)

    # 喂给策略的 bar deque（每个 code 一个；只保留最近 max_bars_per_code 根）
    bar_buf: Dict[str, deque] = {c: deque(maxlen=cfg.max_bars_per_code)
                                  for c in codes}

    def _get(code: str, k_idx: int):
        """取 code 在 sorted_dt[k_idx] 的 (open, high, low, close, volume)。"""
        dt_k = sorted_dt[k_idx]
        s = series[code]
        idx_map = s["idx"]
        k = idx_map.get(dt_k)
        if k is None:
            return None
        return (s["open"][k], s["high"][k], s["low"][k],
                s["close"][k], s["volume"][k], s["ts"][k])

    # 每个 code 的 last valid timestamp（处理停牌/缺失）
    last_valid: Dict[str, int] = {}

    for i in range(n):
        cur_date, cur_time = sorted_dt[i]
        cur_dt = datetime.strptime(cur_date + cur_time, "%Y%m%d%H%M")
        is_eod = cur_time >= "1500"          # 收盘后不再决策（与交易时段一致）

        # 1) 更新 bar 缓冲：所有 code 在本分钟若有数据，append 一根 Bar
        for code in codes:
            row = _get(code, i)
            if row is None:
                continue
            o, h, l, c, v, ts = row
            if c <= 0:
                continue
            buf = bar_buf[code]
            # 同一分钟内只 append 一次（XTdata 通常 1 分钟 1 根，但有时连续）
            if buf and buf[-1].ts == ts:
                continue
            buf.append(Bar(ts=ts, open=o, high=h, low=l, close=c, volume=v,
                           amount=v * c))
            last_valid[code] = i

        # 2) 计算当前总资产
        mv_total = 0.0
        for code, pos in positions.items():
            row = _get(code, i)
            if row is None:
                lp = pos["avg"]            # 缺失时按成本估值
            else:
                lp = row[2] if False else row[3]   # row[3] = close
            mv_total += pos["qty"] * lp
        equity = cash + mv_total
        equity_curve.append(equity)
        if equity > equity_peak:
            equity_peak = equity

        # 3) 离场检查（所有持仓，逐 code）
        # 注意：T+1 限制下，entry_date == cur_date 的持仓当日不卖（与日级回测一致）
        held_codes = list(positions.keys())
        for code in held_codes:
            if code not in last_valid:
                continue
            pos = positions[code]
            row = _get(code, i)
            if row is None:
                continue
            lp = row[3]
            pos["peak"] = max(pos.get("peak", lp), row[1])

            # T+1：建仓当日不卖
            if cfg.t1_restriction and pos["entry_date"] == cur_date:
                continue

            # 构造 Position 给 TrendStrategy.on_exit
            held_pos = Position(
                code=code, name=code, quantity=pos["qty"], avg_cost=pos["avg"],
                last_price=lp, open_date=pos["entry_ts_obj"],
                peak_price=pos["peak"], stop_price=pos.get("stop_price", 0.0),
                target_price=0.0)
            bars = list(bar_buf[code])
            if len(bars) < 5:
                continue
            exit_sig = strategy.on_exit(code, held_pos, lp, bars)
            if exit_sig and exit_sig.side == "SELL":
                # 执行卖出
                proceeds = pos["qty"] * lp * (1 - cfg.cost_pct)
                pnl = (lp - pos["avg"]) * pos["qty"]
                cash += proceeds
                held_min = int((cur_dt - pos["entry_ts_obj"]).total_seconds() // 60)
                trades.append(TradeRecord(
                    code=code, entry_ts=pos["entry_ts"], exit_ts=cur_dt.isoformat(),
                    entry_price=pos["avg"], exit_price=lp,
                    qty=pos["qty"], pnl=round(pnl, 2),
                    pnl_pct=round((lp / pos["avg"] - 1) * 100, 2),
                    hold_minutes=held_min,
                    reason=exit_sig.reason,
                    pnl_net=round(proceeds - pos["qty"] * pos["avg"], 2)))
                del positions[code]

        # 4) 入场信号（portfolio 风格的简化版：扫描全部候选）
        if is_eod:
            continue
        n_held = len(positions)
        if n_held >= cfg.max_positions:
            continue

        # 按候选池扫描：取评分最高的 top-N
        scores: List[Tuple[float, str]] = []
        for code in codes:
            if code in positions:
                continue
            if cfg.use_daily_gate and daily is not None:
                feats = daily.features(code)
                if feats is not None:
                    if not (feats.trend_up or feats.bias >= 0.2):
                        continue
            bars = list(bar_buf[code])
            if len(bars) < 60:
                continue
            sig = strategy.on_bars(code, code, bars)
            n_signals += 1
            if sig.side == "BUY" and sig.score >= cfg.buy_threshold:
                scores.append((sig.score, code))

        scores.sort(reverse=True)
        slots_left = cfg.max_positions - len(positions)
        for score, code in scores[:slots_left]:
            row = _get(code, i)
            if row is None:
                continue
            price = row[3]
            if price <= 0:
                continue
            qty = int(cfg.fixed_amount // (price * 100)) * 100
            if qty <= 0:
                # 单价过高导致连 1 手都买不起 —— 跳过而不是拑（避免价格为 0、
                # 浮点误差或仓位 > 可用现金时静默跳出）
                continue
            cost = qty * price * (1 + cfg.cost_pct)
            if cost > cash:
                qty = int((cash / (1 + cfg.cost_pct)) // (price * 100)) * 100
                if qty <= 0:
                    continue
                cost = qty * price * (1 + cfg.cost_pct)
            cash -= cost
            positions[code] = {
                "qty": qty, "avg": price,
                "entry_ts": cur_dt.isoformat(),
                "entry_ts_obj": cur_dt,
                "entry_date": cur_date,
                "peak": row[1],          # 当根 high
                "stop_price": price * (1 - abs(cfg.hard_stop_pct)),
            }

    # ---- 收尾统计 ----
    final_equity = equity_curve[-1] if equity_curve else cfg.initial_cash
    total_return = (final_equity / cfg.initial_cash) - 1

    # Sharpe / MDD / 胜率
    daily_eq: List[float] = []
    last_close_eq: Optional[float] = None
    for i in range(len(equity_curve)):
        dt_k = sorted_dt[i]
        if i == 0 or dt_k[0] != sorted_dt[i - 1][0]:
            if last_close_eq is not None:
                daily_eq.append(last_close_eq)
            last_close_eq = equity_curve[i]
        else:
            last_close_eq = equity_curve[i]
    if last_close_eq is not None:
        daily_eq.append(last_close_eq)

    rets = []
    for i in range(1, len(daily_eq)):
        prev, cur = daily_eq[i - 1], daily_eq[i]
        if prev > 0:
            rets.append(cur / prev - 1)
    sharpe = 0.0
    if len(rets) >= 2:
        import statistics
        mu = statistics.mean(rets)
        sd = statistics.pstdev(rets)
        sharpe = (mu / sd) * (252 ** 0.5) if sd > 0 else 0.0

    peak = max(equity_curve) if equity_curve else cfg.initial_cash
    mdd = 0.0
    run_peak = equity_curve[0] if equity_curve else cfg.initial_cash
    for v in equity_curve:
        run_peak = max(run_peak, v)
        if run_peak > 0:
            mdd = min(mdd, (v - run_peak) / run_peak)

    wins = [t for t in trades if t.pnl_net > 0]
    losses = [t for t in trades if t.pnl_net <= 0]
    avg_hold = (sum(t.hold_minutes for t in trades) // max(1, len(trades))
                if trades else 0)

    exit_reasons = defaultdict(int)
    for t in trades:
        exit_reasons[t.reason.split()[0] if t.reason else "unknown"] += 1

    return {
        "n_bars": n,
        "n_codes": len(data),
        "n_signals": n_signals,
        "n_trades": len(trades),
        "total_return": total_return,
        "sharpe": sharpe,
        "max_drawdown": mdd,
        "win_rate": len(wins) / max(1, len(trades)),
        "avg_hold_min": avg_hold,
        "avg_win": (sum(t.pnl_net for t in wins) / max(1, len(wins))),
        "avg_loss": (sum(t.pnl_net for t in losses) / max(1, len(losses))),
        "profit_factor": (sum(t.pnl_net for t in wins) /
                          max(1, abs(sum(t.pnl_net for t in losses)))),
        "exit_reasons": dict(exit_reasons),
        "final_equity": final_equity,
        "trades": [asdict(t) for t in trades[-200:]],   # 限制长度
        "equity_curve_len": len(equity_curve),
    }


# ============================================================ walk-forward

def walk_forward_minute(codes: List[str], cfg: MinuteConfig,
                        data: Dict[str, Dict[str, List]],
                        folds: int = 7) -> List[dict]:
    """滚动扩窗 walk-forward：每个 fold 是 [0, k] 的累积窗口。

    返回每个 fold 的回测指标 dict 列表。
    """
    if not data:
        return []
    n = min(len(d["close"]) for d in data.values())
    # 按可用的索引把全数据集切成 folds 段
    if n < folds:
        return []
    seg = n // folds
    out = []
    for k in range(1, folds + 1):
        end = seg * k if k < folds else n
        sliced = {}
        for c, d in data.items():
            sliced[c] = {k_: list(v[:end]) for k_, v in d.items()}
        r = run_minute_backtest(codes, cfg, sliced, daily=None)
        r["fold"] = k
        r["n_bars_fold"] = end
        out.append(r)
    return out


# ============================================================ CLI

def _build_codes() -> List[str]:
    """生产用候选池：静态 STOCK_CODES。"""
    from config.settings import STOCK_CODES
    return list(STOCK_CODES)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", nargs="*",
                    help="override codes (default = STATIC_CODES)")
    ap.add_argument("--count", type=int, default=60000,
                    help="每只标的拉多少根 1m K 线")
    ap.add_argument("--folds", type=int, default=7)
    ap.add_argument("--out", default=str(ROOT / "logs" / "verify_minute.json"))
    args = ap.parse_args()

    codes = args.codes or _build_codes()
    print(f"[load] {len(codes)} 只 1m K 线（count={args.count}）...")
    data = load_minute_universe(codes, count=args.count)
    if not data:
        print("[error] 无 1m 数据（miniQMT 未启动 / 数据未下载）")
        return 1
    print(f"[load] {len(data)}/{len(codes)} 只就绪")

    cfg = MinuteConfig()
    print(f"[run ] {args.folds} 折 walk-forward...")
    folds = walk_forward_minute(codes, cfg, data, folds=args.folds)

    summary = []
    print(f"\n{'fold':<6}{'bars':<8}{'ret%':<10}{'sharpe':<10}"
          f"{'mdd%':<10}{'trades':<8}{'win%':<8}{'avg_hold_min':<12}")
    for r in folds:
        summary.append({
            "fold": r.get("fold"), "bars": r.get("n_bars_fold"),
            "ret": r.get("total_return"), "sharpe": r.get("sharpe"),
            "mdd": r.get("max_drawdown"), "n_trades": r.get("n_trades"),
            "win_rate": r.get("win_rate"),
            "avg_hold_min": r.get("avg_hold_min")})
        print(f"{r.get('fold'):<6}{r.get('n_bars_fold'):<8}"
              f"{r.get('total_return', 0)*100:<+10.2f}"
              f"{r.get('sharpe', 0):<+10.2f}"
              f"{r.get('max_drawdown', 0)*100:<+10.2f}"
              f"{r.get('n_trades', 0):<8}"
              f"{r.get('win_rate', 0)*100:<+8.1f}"
              f"{r.get('avg_hold_min', 0):<12}")

    ok = [s for s in summary if s.get("sharpe") is not None]
    if ok:
        avg = {
            "ret": sum(s["ret"] for s in ok) / len(ok),
            "sharpe": sum(s["sharpe"] for s in ok) / len(ok),
            "mdd": sum(s["mdd"] for s in ok) / len(ok),
        }
        print(f"\n[folds 均值] ret {avg['ret']*100:+.2f}%  "
              f"Sharpe {avg['sharpe']:+.2f}  MDD {avg['mdd']*100:+.2f}%")
        if "sharpe" in summary[-1]:
            full = run_minute_backtest(
                codes, cfg, data, daily=None)
            print(f"\n[全样本 full] ret {full['total_return']*100:+.2f}%  "
                  f"Sharpe {full['sharpe']:+.2f}  "
                  f"MDD {full['max_drawdown']*100:+.2f}%  "
                  f"trades {full['n_trades']}  "
                  f"win% {full['win_rate']*100:.1f}")
            summary.append({"fold": "full", "bars": len(data[next(iter(data))]["close"]),
                            **{k: full.get(k) for k in
                               ("ret", "sharpe", "mdd", "n_trades", "win_rate")}})

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2,
                                   default=str), encoding="utf-8")
    print(f"\n[out] {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
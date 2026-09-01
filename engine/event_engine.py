# -*- coding: utf-8 -*-
"""
事件驱动引擎（qmtIDE-deepseek 主循环）

每轮 (REFRESH_INTERVAL 秒)：
  1. 从 QMTClient 拉全部 UNIVERSE tick
  2. 按 1m 频率聚合 bars（内存，截尾 N 根）
  3. 单标的模式 / 组合模式：
     a) 单标的：每只 STOCK_CODES 独立 TrendStrategy.on_bars() → Signal
     b) 组合：对所有打分 → Top-N → rebalance 计划 → Order 列表
  4. RiskManager.can_open() 拦截
  5. paper 维护本地 ledger；live 调用 broker.place_order
  6. Storage.save_signal / save_order / save_fill / save_ai
  7. sleep 到下一轮

支持：
  - graceful shutdown (Ctrl-C / stop flag)
  - 自动重连 broker (core.auto_reconnect.AutoReconnector)
  - 自动从 broker 拉真实持仓初始化（live + auto_init_positions=True）
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from datetime import date, datetime
from typing import Deque, Dict, List, Optional, Tuple

from config.settings import (
    BAR_WARMUP, BAR_WARMUP_BUDGET_SEC, BAR_WARMUP_DOWNLOAD,
    BAR_WARMUP_MAX_STALE_DAYS, EXECUTION_MODE, IDLE_REFRESH_INTERVAL,
    INDEX_CODES, INITIAL_CASH, LOG_DIR, MARKET_INDEX_CODE,
    PERSIST_HOLD_SIGNALS, PORTFOLIO_CONFIG, REFRESH_INTERVAL, RISK_PARAMS,
    RISK_SNAPSHOT_MIN_INTERVAL, SESSION_GUARD, SINGLETON_LOCK, STOCK_CODES,
    STRATEGY_MODE, STRATEGY_PARAMS, UNIVERSE,
)
from core.notices import system_notice, latest_notices
from core.auto_reconnect import AutoReconnector
from core.market_calendar import (
    is_trading_time, seconds_to_next_session, session_label,
)
from risk.position_sizer import PositionSizer
from strategy.daily_context import DailyContext
from core.broker import qmt_broker
from core.data_models import Bar, Fill, Order, Position, Signal, Tick
from core.qmt_client import qmt_client
from ai.analyst import AIAnalyst, ai_analyst
from ai.llm_reranker import LLMReranker, llm_reranker
from data.dynamic_universe import DynamicUniverse, dynamic_universe
from risk.manager import RiskManager
from storage.db import Storage
from strategy.base import BaseStrategy
from strategy.portfolio_strategy import PortfolioStrategy
from strategy.sector_scorer import SectorScorer, sector_scorer
from strategy.trend_strategy import TrendStrategy

logger = logging.getLogger(__name__)

# 心跳间隔（秒）：每 10 分钟输出一次「存活 + 效率」系统提示，
# 让运维侧随时确认引擎没有卡死、CPU/内存无异常，且无需刷屏级 DEBUG。
HEARTBEAT_SEC = 600


def _pid_alive(pid: int) -> bool:
    """判断 pid 是否存活（Windows 安全版）。

    严禁用 ``os.kill(pid, 0)``——CPython 在 Windows 上会把它翻译成
    TerminateProcess，等于把目标进程杀掉。改用 kernel32.OpenProcess 探测。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        SYNCHRONIZE = 0x00100000
        try:
            h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        except Exception:
            return True
        if not h:
            return False
        try:
            ctypes.windll.kernel32.CloseHandle(h)
        except Exception:
            pass
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return True


def _to_tick(code: str, raw: dict) -> Tick:
    name = UNIVERSE.get(code, code)
    lp = float(raw.get("lastPrice") or 0)
    pre = float(raw.get("lastClose") or lp or 0)
    chg = lp - pre if pre else 0.0
    pct = (chg / pre * 100) if pre else 0.0
    return Tick(
        ts=datetime.now(), code=code, name=name, price=lp,
        open=float(raw.get("open") or lp),
        high=float(raw.get("high") or lp),
        low=float(raw.get("low") or lp),
        pre_close=pre,
        volume=int(raw.get("volume") or 0),
        amount=float(raw.get("amount") or 0.0),
        change=round(chg, 3),
        change_pct=round(pct, 3),
        source=qmt_client.mode,
    )


class EventEngine:
    def __init__(self,
                 strategy=None,
                 risk: RiskManager = None,
                 storage: Storage = None,
                 analyst: AIAnalyst = None,
                 exec_mode: str = None,
                 strategy_mode: str = None,
                 auto_reconnect: bool = True,
                 auto_init_positions: bool = False,
                 enable_sector_scorer: bool = True,
                 enable_dynamic_universe: bool = True,
                 enable_llm_reranker: bool = True,
                 llm_rerank_interval: int = 30,   # 每 N tick 重排一次
                 session_guard: Optional[bool] = None):
        self.strategy_mode = (strategy_mode or STRATEGY_MODE).lower()
        self.exec_mode = (exec_mode or EXECUTION_MODE).lower()
        # 交易时段守卫：非交易时段不拉行情/不聚合 bar/不下单（详见 settings 注释）
        self.session_guard = (SESSION_GUARD if session_guard is None
                              else bool(session_guard))
        self._session_state: Optional[bool] = None   # 用于状态切换时只记一条日志

        # strategy 解析（"single" 用 TrendStrategy，"portfolio" 用 PortfolioStrategy）
        # 日线多周期上下文（MTF）：为分钟级策略提供趋势偏置 + 波动率
        self.daily = DailyContext(
            codes=list(STOCK_CODES) + list(INDEX_CODES),
        )
        self.sizer = PositionSizer()
        if strategy is not None:
            self.strategy = strategy
        elif self.strategy_mode == "portfolio":
            self.strategy = PortfolioStrategy()
        else:
            self.strategy = TrendStrategy()
        # 注入日线上下文（让入场受日线趋势闸门约束）
        if isinstance(self.strategy, PortfolioStrategy):
            self.strategy.trend.daily = self.daily
        else:
            self.strategy.daily = self.daily
        # PortfolioStrategy 同时需要底层 trend 用于 evaluate_exit
        if isinstance(self.strategy, PortfolioStrategy):
            self._portfolio = self.strategy
            self._trend = self.strategy.trend
        else:
            self._portfolio = None
            self._trend = self.strategy

        self.risk = risk or RiskManager()
        self.storage = storage or Storage()

        # 市场环境过滤（regime filter）——回测验证唯一稳健的结构性改进。
        # 解决原策略"永远满仓"的暴露问题：市场转弱时不再新开仓并强制清仓。
        self.regime_mode = STRATEGY_PARAMS.get("regime_mode", "off")
        self.regime_index = STRATEGY_PARAMS.get(
            "regime_index", MARKET_INDEX_CODE)
        self.regime_ma = int(STRATEGY_PARAMS.get("regime_ma", 60))
        self.regime_breadth_thresh = float(
            STRATEGY_PARAMS.get("regime_breadth_thresh", 0.5))
        self.regime_force_exit = bool(
            STRATEGY_PARAMS.get("regime_force_exit", False))
        # 并发持仓上限（集中度控制）：经 IS/OOS + 多折双验证，5 显著优于 8。
        self.max_positions = int(STRATEGY_PARAMS.get("max_positions", 8))
        self.analyst = analyst or AIAnalyst()
        self.sector_scorer = SectorScorer() if enable_sector_scorer else None
        self.dynamic_universe = DynamicUniverse() if enable_dynamic_universe else None
        self.llm_reranker = LLMReranker() if enable_llm_reranker else None
        self._llm_rerank_interval = llm_rerank_interval
        self._llm_last_result = None
        self.data_mode = qmt_client.mode
        self.broker_mode = qmt_broker.mode

        # 内存状态
        self._bars: Dict[str, Deque[Bar]] = defaultdict(
            lambda: deque(maxlen=STRATEGY_PARAMS["ma_long"] * 6))
        self._positions: Dict[str, Position] = {}
        self._cash: float = INITIAL_CASH
        self._pending_ai: Dict[str, threading.Thread] = {}

        self._stop_flag = threading.Event()
        self._tick_count: int = 0
        self._last_heartbeat: float = 0.0
        self._daily_trade_count: int = 0
        # 日内交易计数跨日重置锚点（见 _reset_daily_if_needed）
        self._trade_date = date.today()
        # 日内盈亏基线：今日首个权益快照（优先，跨重启仍有效）或本会话首 tick 总资产。
        # 「日内盈亏」= 当前总资产 − 今日开盘资产（已实现 + 当日浮动），避免无 round-trip
        # 时 daily_pnl 恒为 0 看起来「没数据」。
        self._day_open_asset: Optional[float] = None
        try:
            self._day_open_asset = self.storage.first_equity_today()
        except Exception:
            self._day_open_asset = None
        # 拒单日志去重：code -> 上次记录过的拒绝原因 / 时间戳
        self._last_reject: Dict[str, str] = {}
        self._last_reject_ts: Dict[str, float] = {}
        # tick.volume/amount 是「当日累计量」，不是本轮增量。实测（logs/
        # probe_livedata.json）三只样本盘后累计量 13.4万/45.9万/51.2万手，
        # 而原 _aggregate_bar 直接 `b.volume += tick.volume` 把累计量按轮叠加：
        # 一分钟 20 轮轮询会把 bar 成交量放大约 2116 倍。于是 volume_surge
        # 因子（8 因子之一）在 live 路径上算的是垃圾，而回测用的是历史 K 线
        # 的「每根真实成交量」——live 与回测口径不一致，正是实盘偏离回测的典型来源。
        # 修法：只累加累计量的增量。
        self._last_cum_vol: Dict[str, int] = {}
        self._last_cum_amt: Dict[str, float] = {}
        # 本地持仓元数据（broker 不返回）：止损价/目标价/峰值价/开仓日。
        # live 模式由 _sync_broker_positions 合并回同步来的持仓，确保退出逻辑
        # /移动止损在实盘同样生效。
        self._pos_meta: Dict[str, dict] = {}
        # 权益曲线快照（供盘后复盘重建当日收益/最大回撤）：峰值与节流时间戳
        self._peak_equity: float = 0.0
        self._last_equity_ts: float = 0.0
        # 风控快照写入节流：仅在「状态变化」或「超过最小间隔」时写库。
        # 原实现每 10 tick（~30s）无条件写一行，实测累积 28.8 万行，绝大多数
        # 是内容完全相同的重复快照，对复盘零信息量。
        self._last_risk_fp: Optional[str] = None
        self._last_risk_snap_ts: float = 0.0

        # 自动重连
        self._auto_reconnect_enabled = auto_reconnect
        self._broker_reconnector: Optional[AutoReconnector] = None

        # 启动时尝试从 broker 同步真实持仓
        if auto_init_positions and self.exec_mode == "live":
            self._init_positions_from_broker()

        # 重启延续：paper 模式下从 SQLite 恢复本地账本（持仓/现金/日内计数/峰值），
        # 保证连续多日 Paper 测试在每次项目重启后记录依然延续。live 以 broker 为权威源。
        if self.exec_mode == "paper":
            self._restore_engine_state()

    # ============================================================ 公开

    def _daily_codes(self) -> List[str]:
        """日线上下文应该覆盖的全量标的 = 静态个股 + 指数 + **动态候选池活跃股**。

        【P0 修正 2026-09-02】原实现只在 ``__init__`` 里传了
        ``STOCK_CODES + INDEX_CODES``，而 ``_run_single_step`` /
        ``_run_portfolio_step`` 的候选池还包含 ``dynamic_universe.active_codes``
        （~30 只）。后果：这 30 只股的 ``features()`` 永远为 None，于是
          ① 日线趋势闸门（策略核心优化 A）对它们 100% 不生效；
          ② ``atr_pct()`` 返回 0 → 波动率目标仓位退化为固定 4% 估算；
          ③ ``top_momentum()`` 排不到它们 → 动量闸门无条件放行。
        实盘证据（storage/qmt.db, 2026-09-01）：601138.SH / 688347.SH /
        300604.SZ 等全部买入信号均带 ``[no-daily]`` 标记，即只靠 1 分钟级
        噪音在下单。现在每次刷新都合并活跃池，三道闸门全覆盖。
        """
        codes = list(STOCK_CODES) + list(INDEX_CODES)
        if self.dynamic_universe is not None:
            try:
                codes += list(self.dynamic_universe.active_codes)
            except Exception as e:
                logger.debug("_daily_codes 取动态活跃池失败(忽略): %s", e)
        return list(dict.fromkeys(codes))

    def _refresh_universe_async(self) -> None:
        """后台刷新动态候选池（不阻塞 engine.run()）。

        刷新完成后**立即追一次日线刷新**，把新进活跃池的代码补上日线特征，
        否则新代码在本交易日内仍是「无日线」状态。DailyContext.refresh 带
        TTL，已有新鲜特征的标的不会重复拉网络，成本只有新增部分。
        """
        try:
            logger.info("正在刷新动态候选池...")
            self.dynamic_universe.refresh(force=True)
            logger.info("动态候选池刷新完成")
        except Exception as e:
            logger.warning("动态候选池刷新失败: %s", e)
        try:
            n = self.daily.refresh(codes=self._daily_codes(), force=False)
            logger.info("动态池日线特征补齐完成: 覆盖 %d 只", n)
        except Exception as e:
            logger.warning("动态池日线特征补齐失败: %s", e)

    def _refresh_daily_async(self, force: bool = True) -> None:
        """后台刷新日线多周期上下文（不阻塞 engine.run()）。"""
        try:
            logger.info("正在刷新日线多周期上下文...")
            self.daily.refresh(codes=self._daily_codes(), force=force)
            logger.info("日线上下文刷新完成: regime=%s n=%d",
                        self.daily.market_regime(),
                        len(self.daily._feats))
        except Exception as e:
            logger.warning("日线上下文刷新失败: %s", e)

    def _notice_risk_budget(self, max_positions: int) -> None:
        """如实播报真实风险预算（而不是名义值）。

        为什么必需：原启动横幅声称「实盘断路器阈值远高于策略自然回撤」，但：
          单仓上限   = min(max_single_position_pct, max_order_amount/equity)
          理论敞口   = 单仓上限 × max_positions
          最坏情形   = 实际敞口 × |真实止损|
        当前参数下 0.30×5 = 150% 理论敞口（现金夹紧后 95%）、最坏情形 ≈ -17%，
        与 -25% 断路器的安全边距并不宽。把这几个数字当场算出来写进系统提示，
        使校准关系可被直接核验，而不是靠一句无数据支持的声称。
        """
        try:
            equity = self._total_asset() or INITIAL_CASH
            buffer_pct = float(PORTFOLIO_CONFIG.get("cash_buffer_pct", 0.0) or 0.0)
            cap_pct = float(RISK_PARAMS["max_single_position_pct"])
            if equity > 0:
                cap_pct = min(cap_pct,
                              float(RISK_PARAMS["max_order_amount"]) / equity)
            gross = cap_pct * max(1, int(max_positions))
            investable = 1.0 - buffer_pct
            if STRATEGY_PARAMS.get("exit_mode", "scalp") == "trend":
                real_stop = abs(STRATEGY_PARAMS.get("hard_stop_pct", -0.18))
            else:
                real_stop = abs(STRATEGY_PARAMS.get("stop_loss", -0.04))
            actual_gross = min(gross, investable)   # 现金夹紧后的真实上限
            worst = actual_gross * real_stop
            dd_limit = abs(float(self.risk.p["max_drawdown_pct"]))
            nominal = float(STRATEGY_PARAMS.get("risk_per_trade", 0.0)) * 100
            per_trade_real = cap_pct * real_stop * 100
            over = gross > investable
            level = "WARNING" if (worst >= dd_limit or over) else "SYSTEM"
            msg = (
                f"风险预算实测值：单仓上限={cap_pct * 100:.0f}% × 持仓上限"
                f"{int(max_positions)} = 理论敞口{gross * 100:.0f}%；现金夹紧后"
                f"实际上限{actual_gross * 100:.0f}%（预留现金{buffer_pct * 100:.0f}%）。"
                f"真实止损{real_stop * 100:.0f}% → 单笔真实风险≈{per_trade_real:.1f}%"
                f"（名义 risk_per_trade={nominal:.1f}%），满仓同时打止损最坏情形≈"
                f"-{worst * 100:.0f}%，回撤断路器-{dd_limit * 100:.0f}%。"
            )
            if over:
                msg += (f"⚠ 理论敞口{gross * 100:.0f}% > 可投资上限"
                        f"{investable * 100:.0f}%，配置本身允许超配，现已由现金夹紧"
                        f"强制封顶（不再可能负现金）；建议把 max_single_position_pct 降到"
                        f"≤{investable / max(1, int(max_positions)) * 100:.0f}% 使配置自洽。")
            if worst >= dd_limit:
                msg += ("⚠ 最坏情形已触及断路器阈值，二者未拉开安全边距。")
            system_notice(level, "风控", msg)
        except Exception as e:
            logger.debug("_notice_risk_budget 失败(忽略): %s", e)

    def _reset_daily_if_needed(self) -> None:
        """跨交易日重置日内交易计数，避免 max_daily_trades 在进程长跑后永久拦截。

        原实现：``_daily_trade_count`` 在 ``__init__`` 归零后只增不减（仅 paper 分支
        在 794/836 行自增），且**从未按日重置**——paper 模式累计满 ``max_daily_trades``
        (默认10) 笔后，所有 BUY 被 ``daily_trades>10`` 永久拒绝、引擎空转刷屏
        （实盘日志曾出现 63 万条同类记录；本次观测到的 40604 进程即因此卡死）。
        现与 ``RiskManager.reset_daily`` 对齐：日期切换即归零，使「日内」限额名副其实。
        """
        today = date.today()
        if today != self._trade_date:
            self._trade_date = today
            self._daily_trade_count = 0
            self._day_open_asset = None  # 新交易日重新基线
            logger.info("日内交易计数跨日重置: daily_trade_count=0 (date=%s)", today)

    def _warmup_bars(self, codes: List[str], download: bool = None,
                     budget_sec: float = None) -> dict:
        """用历史 1 分钟 K 线预热 ``self._bars``，消除「每次重启后瞎 60 分钟」。

        【P1 修正 2026-09-02】``_bars`` 是纯内存 deque，而 ``on_bars`` 要求
        ``len(bars) >= 60``。于是每次进程重启 / 每天开盘都得先攒满 60 根
        1 分钟 bar 才能开始工作——等于每天前一小时是盘区（占交易时长 25%，
        而早盘恰好是趋势股成交最活跃、突破最多发的时段）。

        ❗ 为何必须校验新鲜度（实测踩到的坑）：``get_market_data_ex`` 只读
        miniQMT **本地已缓存**的数据。2026-09-02 实测直读本地 1m：
        300308.SZ 拿到的是 **07-22**（早 6 周）的 bar，收盘价 1060.8 vs
        真实 859.3（差 19%）；300502/688256/000001 完全无数据。把这种陈旧价
        当成当前行情灌进 MA/ATR/VWAP，比不预热**更危险**。所以：
          ① 最后一根 bar 超过 BAR_WARMUP_MAX_STALE_DAYS 天一律拒用；
          ② 本地无/陈旧时先 download_history_data 补拉再重读；
          ③ 补拉实测 ~9.75s/只（46 只约 7.5 分钟），所以本方法由后台线程
            调用，并按「持仓 → 静态池 → 其余」优先级 + 总时间预算推进。

        并发安全：不在原 deque 上 clear+append（主循环可能同时在 append/读，
        ``if dq and dq[-1]`` 两步之间被 clear 会抛 IndexError），而是另建一个
        deque 后**原子换入** ``self._bars[code]``。
        """
        if download is None:
            download = BAR_WARMUP_DOWNLOAD
        if budget_sec is None:
            budget_sec = BAR_WARMUP_BUDGET_SEC
        maxlen = STRATEGY_PARAMS["ma_long"] * 6
        deadline = time.time() + max(0.0, budget_sec)
        stat = {"ready": 0, "already": 0, "stale": 0, "empty": 0,
                "downloaded": 0, "budget_skipped": 0}

        # 优先级：持仓（退出逻辑最急需 bar）→ 静态池 → 其余
        held = [c for c, p in self._positions.items() if p.quantity > 0]
        ordered = list(dict.fromkeys(
            [c for c in held if c in codes]
            + [c for c in codes if c in STOCK_CODES]
            + list(codes)))

        for code in ordered:
            try:
                if len(self._bars[code]) >= 60:
                    stat["already"] += 1
                    continue
                bars = self._read_fresh_1m(code, maxlen)
                if bars is None and download and time.time() < deadline:
                    if qmt_client.download_history(code, "1m"):
                        stat["downloaded"] += 1
                        bars = self._read_fresh_1m(code, maxlen)
                elif bars is None and download:
                    stat["budget_skipped"] += 1
                if not bars or len(bars) < 60:
                    stat["empty" if not bars else "stale"] += 1
                    continue
                dq: Deque[Bar] = deque(bars[-maxlen:], maxlen=maxlen)
                # 保留比历史更新的实时 bar（预热期间主循环可能已聚合出几根）
                last_ts = dq[-1].ts
                for b in list(self._bars[code]):
                    if b.ts > last_ts:
                        dq.append(b)
                self._bars[code] = dq          # 原子换入
                stat["ready"] += 1
            except Exception as e:
                stat["empty"] += 1
                logger.debug("bar 预热失败 %s: %s", code, e)

        logger.info("分钟线预热: 就绪 %d / 已有 %d / 陈旧拒用 %d / 无数据 %d / "
                    "补拉 %d / 超预算跳过 %d（共 %d 只，缓冲区 %d 根）",
                    stat["ready"], stat["already"], stat["stale"], stat["empty"],
                    stat["downloaded"], stat["budget_skipped"],
                    len(ordered), maxlen)
        system_notice(
            "SYSTEM" if stat["ready"] or stat["already"] else "WARNING", "数据",
            f"分钟线预热完成：就绪 {stat['ready']} / 已有 {stat['already']} / "
            f"陈旧拒用 {stat['stale']} / 无数据 {stat['empty']}（共 {len(ordered)} 只，"
            f"补拉 {stat['downloaded']} 只）。就绪的标的无需再等 60 分钟即可评分；"
            f"陈旧/无数据的标的已**拒绝使用**（避免把旧价当现价），仍走现场聚合。")
        return stat

    def _read_fresh_1m(self, code: str, count: int) -> Optional[List[Bar]]:
        """读本地 1m K 线并做新鲜度校验。陈旧/缺失返回 None。"""
        raw = qmt_client.get_history(code, period="1m", count=count)
        if not raw:
            return None
        out: List[Bar] = []
        for b in raw:
            close = float(b.get("close") or 0)
            ts = b.get("ts")
            if close <= 0 or not isinstance(ts, datetime):
                continue
            out.append(Bar(
                ts=ts.replace(second=0, microsecond=0),
                open=float(b.get("open") or close),
                high=float(b.get("high") or close),
                low=float(b.get("low") or close),
                close=close,
                volume=int(b.get("volume") or 0),
                amount=float(b.get("amount") or 0.0),
            ))
        if not out:
            return None
        age_days = (date.today() - out[-1].ts.date()).days
        if age_days > BAR_WARMUP_MAX_STALE_DAYS:
            logger.debug("预热拒用 %s：最后 bar %s 已陈旧 %d 天（上限 %d）",
                         code, out[-1].ts.date(), age_days,
                         BAR_WARMUP_MAX_STALE_DAYS)
            return None
        return out

    def run(self, max_ticks: int = 0) -> None:
        """主循环。max_ticks=0 表示无限循环。"""
        # 启动时刷新动态候选池（后台线程，不阻塞 engine.run()）
        if self.dynamic_universe is not None:
            threading.Thread(
                target=self._refresh_universe_async,
                name="universe-refresh-init",
                daemon=True,
            ).start()
        # 启动时刷新日线多周期上下文（MTF 趋势偏置 + 波动率）
        threading.Thread(
            target=self._refresh_daily_async,
            name="daily-refresh-init",
            daemon=True,
        ).start()

        # 合并静态 UNIVERSE + 动态活跃池 → 订阅（优先用缓存的活跃池）
        codes = list(UNIVERSE.keys())
        if self.dynamic_universe is not None:
            dynamic_codes = self.dynamic_universe.active_codes
            codes = list(dict.fromkeys(codes + dynamic_codes))   # 保留顺序去重
            logger.info("动态候选池: %d 只活跃 (总动态 %d 只)",
                        len(dynamic_codes),
                        len(self.dynamic_universe.codes))
        qmt_client.subscribe(codes)
        # 分钟线预热：后台线程（首次补拉 1m 历史实测 ~9.75s/只，46 只约 7.5 分钟，
        # 绝不能阻塞启动）。预热完成前主循环照常现场聚合，二者会在换入时合并。
        if BAR_WARMUP:
            threading.Thread(target=self._warmup_bars, args=(list(codes),),
                             name="bar-warmup", daemon=True).start()
        logger.info("Engine 启动: mode=%s data=%s broker=%s exec=%s codes=%d",
                    self.strategy_mode, self.data_mode,
                    self.broker_mode, self.exec_mode, len(codes))

        # ===== 系统提示：启动结论（清晰的系统级横幅）=====
        # 把这台引擎的「身份 + 策略结论 + 连接 + 风控」一次性讲清楚，
        # 任何客户端（日志 / Web）都能一眼读到，避免和逐轮调试噪声混在一起。
        _mp = int(STRATEGY_PARAMS.get("max_positions", self.max_positions))
        system_notice(
            "SYSTEM", "系统",
            f"引擎已启动 | 策略={self.strategy_mode} 执行={self.exec_mode} "
            f"数据源={self.data_mode} 券商={self.broker_mode} | "
            f"订阅标的={len(codes)} 单实例锁={'开' if SINGLETON_LOCK else '关'}"
        )
        self._notice_risk_budget(_mp)
        system_notice(
            "SYSTEM", "分析结论",
            f"策略配置已加载：趋势骑行退出(trend) + 波动率目标仓位 + "
            f"并发持仓上限={_mp} + regime 闸门=关闭(追收益)。个股/组合风控全保留；"
            f"日线闸门现已覆盖静态+动态候选池（require_daily_data="
            f"{STRATEGY_PARAMS.get('require_daily_data', True)}），买入已受现金夹紧约束。"
            f"回撤断路器 -{abs(self.risk.p['max_drawdown_pct'])*100:.0f}%，"
            f"冷却 {self.risk.p['dd_recover_days']} 日自动恢复。"
        )
        if self.exec_mode == "live" and not qmt_broker.is_connected:
            system_notice("WARNING", "系统",
                          "实盘券商未连接：下单不会成交，请检查 miniQMT 是否已启动。")
        elif self.data_mode not in ("xtdata", "live"):
            system_notice("WARNING", "系统",
                          f"数据源={self.data_mode}（非实时 xtdata），"
                          f"行情可能为模拟/回退数据，仅用于自检。")

        # ===== 执行模式审计（paper→live 切换的可核实记录）=====
        # 每次重启/切换都以系统提示固化当前执行模式，复盘时可通过 notices.log
        # 还原「何时从 PAPER 切到 LIVE」，并与订单/成交记录的 mode 标记互证。
        if self.exec_mode == "live":
            system_notice(
                "WARNING", "模式",
                "已启动/切换至 LIVE 实盘模式（真实资金）。本会话所有下单与成交记录"
                "将以 mode=LIVE 标记，盘后复盘(review_daily)可据此与 PAPER 模拟盘记录"
                "区分核实；请确认账户资金与风控限额已就绪。"
            )
        else:
            system_notice(
                "SYSTEM", "模式",
                "已启动 PAPER 模拟盘模式。本会话所有交易记录以 mode=PAPER 标记，"
                "仅用于策略验证，不涉及真实资金。"
            )

        if self.exec_mode == "live":
            ok = qmt_broker.connect()
            if not ok:
                logger.warning("Live broker 未连接，但继续运行（仅 paper 不会真下单）")

        # 启动 broker 自动重连
        if self._auto_reconnect_enabled and self.exec_mode == "live":
            self._broker_reconnector = AutoReconnector(
                name="broker",
                connect_fn=lambda: qmt_broker.connect(force=True),
            )
            self._broker_reconnector.start()
            # 启动时尝试一次 connect（如果未连）
            if not qmt_broker.is_connected:
                qmt_broker.connect(force=True)

        # 自检模式（--ticks N）绕过时段守卫：允许任何时间跑固定轮数验证。
        guard_active = self.session_guard and not max_ticks
        if guard_active:
            logger.info("交易时段守卫已启用: 非交易时段将休眠（间隔 %ds），"
                        "不拉行情/不聚合 bar/不下单", IDLE_REFRESH_INTERVAL)
        self._last_heartbeat = time.time()
        try:
            while not self._stop_flag.is_set():
                self._reset_daily_if_needed()
                # 单实例自愈：若 engine.pid 已被更新的实例接管，主动让出。
                # 每轮只读一次文件 + 一次 OpenProcess，开销可忽略；不匹配才退出。
                if not self._verify_singleton_holder():
                    logger.warning(
                        "检测到本进程(pid=%d)已非单实例持有者，主动让出并退出",
                        os.getpid())
                    break
                if self._stop_requested_externally():
                    logger.info("检测到外部停止信号（%s），正常退出",
                                self.STOP_SENTINEL.name)
                    break
                if guard_active and not is_trading_time():
                    self._log_session_transition(False)
                    self._idle_wait(IDLE_REFRESH_INTERVAL)
                    continue
                if guard_active:
                    self._log_session_transition(True)
                self._tick_count += 1
                # 稳定性：单轮异常隔离。原实现里 _run_once 抛出的任何异常都会
                # 直接冲出主循环、杀死整台引擎（曾导致无人值守时引擎静默退出）。
                # 现在一律捕获、记系统提示、继续下一轮，单点故障不再连累全局。
                try:
                    self._run_once(codes)
                except Exception as exc:
                    system_notice(
                        "ERROR", "ENGINE",
                        f"主循环单轮异常已隔离(继续运行): {type(exc).__name__}: {exc}")
                    logger.exception("主循环单轮异常(已隔离，继续运行):")
                # 心跳：每 HEARTBEAT_SEC 输出一次存活 + 效率系统提示
                self._maybe_heartbeat()
                if max_ticks and self._tick_count >= max_ticks:
                    logger.info("达到 max_ticks=%d，正常退出", max_ticks)
                    break
                self._stop_flag.wait(REFRESH_INTERVAL)
        except KeyboardInterrupt:
            logger.info("用户中断 (Ctrl-C)")
        finally:
            self._shutdown()

    # ----- 时段守卫 / 外部停止 -----

    STOP_SENTINEL = LOG_DIR / "STOP_ENGINE"

    def _stop_requested_externally(self) -> bool:
        """哨兵文件停止开关：``python main.py --stop`` 会创建它。

        为什么需要：此前多轮自动化把引擎跑成后台进程后无从收敛，实测积压
        34 个僵尸引擎（4 天烧掉 18.9 CPU 小时、日志涨到 1.02GB）。有了协作
        式停止开关，任何时候都能一条命令收干净。
        """
        try:
            return self.STOP_SENTINEL.exists()
        except Exception:
            return False

    # ----- 单实例自愈（防重复引擎抢 CPU / 并发交易）-----

    def _singleton_pid_path(self) -> Path:
        return Path(__file__).resolve().parent.parent / "logs" / "engine.pid"

    def _verify_singleton_holder(self) -> bool:
        """本进程是否仍是注册的单实例持有者。

        返回 ``False`` 表示 ``logs/engine.pid`` 已指向**另一个存活的 pid**
        （即有更新的实例取代本进程）。此时应主动让出（graceful 退出），
        避免两个引擎并发交易 / 抢 CPU / 重复写日志。

        任何读取异常一律返回 ``True``（绝不因瞬时错误自杀）。
        """
        try:
            p = self._singleton_pid_path()
            if not p.exists():
                return True
            txt = p.read_text(encoding="utf-8", errors="ignore").strip()
            if not txt:
                return True
            other = int(txt)
            if other == os.getpid():
                return True
            if not _pid_alive(other):
                return True  # 陈旧 pid 文件，本进程即事实持有者
            return False     # 另有存活实例持有锁 → 让出
        except Exception:
            return True

    def _idle_wait(self, total: float) -> None:
        """非交易时段休眠：切成 5s 分片，保证能及时响应停止信号。"""
        waited = 0.0
        while waited < total and not self._stop_flag.is_set():
            if self._stop_requested_externally():
                return
            self._stop_flag.wait(min(5.0, total - waited))
            waited += 5.0

    def _log_session_transition(self, now_open: bool) -> None:
        if self._session_state == now_open:
            return
        self._session_state = now_open
        if now_open:
            logger.info("交易时段开启（%s）：恢复行情轮询", session_label())
            # 开盘即给出一次「市场分析结论」系统提示：把当前市场状态 /
            # 候选池 / 资金状况浓缩成一条清晰可读的提示，便于巡检。
            try:
                regime_ok = self._regime_ok()
                n_dyn = (len(self.dynamic_universe.active_codes)
                         if self.dynamic_universe else 0)
                n_pos = sum(1 for p in self._positions.values()
                            if p.quantity > 0)
                system_notice(
                    "SYSTEM", "分析结论",
                    f"交易时段开启 | regime闸门={self.regime_mode}("
                    f"{'放行' if regime_ok else '拦截'}) "
                    f"动态候选池={n_dyn}只 当前持仓={n_pos} "
                    f"现金={self._cash:,.2f} 风控="
                    f"{'熔断' if self.risk.is_halted else '正常'}"
                )
            except Exception:
                pass
        else:
            mins = seconds_to_next_session() / 60.0
            logger.info("非交易时段（%s）：休眠中，距下一时段约 %.0f 分钟",
                        session_label(), mins)

    def _maybe_heartbeat(self) -> None:
        """每 HEARTBEAT_SEC 输出一次存活 + 效率系统提示。"""
        now = time.time()
        if now - self._last_heartbeat < HEARTBEAT_SEC:
            return
        self._last_heartbeat = now
        n_pos = sum(1 for p in self._positions.values() if p.quantity > 0)
        total = self._total_asset()
        state = "交易中" if is_trading_time() else "非交易时段"
        reconnects = (self._broker_reconnector.reconnect_count
                      if self._broker_reconnector else 0)
        system_notice(
            "INFO", "心跳",
            f"存活确认 | 状态={state} 轮次={self._tick_count} 持仓={n_pos} "
            f"总资产={total:,.2f} 现金={self._cash:,.2f} "
            f"风控={'熔断' if self.risk.is_halted else '正常'} 券商重连={reconnects}"
        )

    def stop(self) -> None:
        self._stop_flag.set()

    def snapshot(self) -> dict:
        total_asset = round(self._total_asset(), 2)
        snap = {
            "tick": self._tick_count,
            "strategy_mode": self.strategy_mode,
            "data_mode": self.data_mode,
            "broker_mode": self.broker_mode,
            "broker_connected": qmt_broker.is_connected,
            "broker_reconnects": (self._broker_reconnector.reconnect_count
                                  if self._broker_reconnector else 0),
            "exec_mode": self.exec_mode,
            "session": {
                "guard": self.session_guard,
                "state": session_label(),
                "trading_now": is_trading_time(),
            },
            "regime": {
                "mode": self.regime_mode,
                "index": self.regime_index,
                "ma": self.regime_ma,
                "ok": self._regime_ok(),
                "force_exit": self.regime_force_exit,
            },
            "cash": round(self._cash, 2),
            "positions": {
                code: {
                    "name": p.name,
                    "quantity": p.quantity,
                    "avg_cost": round(p.avg_cost, 3),
                    "last_price": round(p.last_price, 3),
                    "pnl": round(p.pnl, 2),
                    "pnl_pct": round(p.pnl_pct * 100, 3),
                    "open_date": p.open_date.isoformat() if p.open_date else None,
                }
                for code, p in self._positions.items() if p.quantity > 0
            },
            "total_asset": total_asset,
            "risk": self._build_risk_snapshot(total_asset),
            "analyst_enabled": self.analyst.enabled,
            "analyst_client": type(self.analyst.client).__name__,
            "sector_scorer": (self.sector_scorer.snapshot()
                              if self.sector_scorer else None),
            "dynamic_universe": (self.dynamic_universe.snapshot()
                                  if self.dynamic_universe else None),
            "llm_rerank": (self.latest_llm_rerank()),
            "llm_reranker": (self.llm_reranker.snapshot()
                              if self.llm_reranker else None),
            "notices": self.latest_notices(20),
        }
        if self._portfolio:
            snap["portfolio"] = {
                "max_positions": self._portfolio.max_positions,
                "score_threshold": self._portfolio.score_threshold,
                "max_single_pct": self._portfolio.max_single_pct,
            }
        return snap

    def _build_risk_snapshot(self, total_asset: float) -> dict:
        """在 RiskManager.snapshot() 基础上补充「日内盈亏」（真实日内总盈亏）。

        daily_pnl 仅统计已成交流水（realized），无 round-trip 时恒为 0，仪表板
        看起来像「没数据」。intraday_pnl = 当前总资产 − 今日开盘资产，融合了当日
        浮动盈亏，交易时段始终有真实数值。基线 _day_open_asset 优先取今日首个
        权益快照（跨重启仍有效），否则取本会话首 tick 总资产。
        """
        snap = self.risk.snapshot()
        open_asset = self._day_open_asset
        if open_asset and open_asset > 0:
            intraday = total_asset - open_asset
            snap["intraday_pnl"] = round(intraday, 2)
            snap["intraday_pnl_pct"] = round(intraday / open_asset * 100, 3)
            snap["day_open_asset"] = round(open_asset, 2)
        else:
            snap["intraday_pnl"] = 0.0
            snap["intraday_pnl_pct"] = 0.0
            snap["day_open_asset"] = None
        return snap

    def latest_bars(self) -> Dict[str, List[dict]]:
        """返回所有股票的最近 bar（用于 SSE 推送）"""
        out = {}
        for code, dq in self._bars.items():
            if not dq:
                continue
            last = dq[-1]
            out[code] = {
                "ts": last.ts.isoformat(),
                "open": last.open, "high": last.high,
                "low": last.low, "close": last.close,
                "volume": last.volume,
            }
        return out

    def latest_notices(self, limit: int = 50) -> list:
        """最近的系统提示（用于 Web 仪表板展示 / SSE 推送）。"""
        return latest_notices(limit)

    def latest_ticks(self) -> Dict[str, dict]:
        """最近一次拉到的 tick（用于 SSE 推送）"""
        return self._last_ticks

    def latest_sector_heat(self) -> dict:
        """产业链热度（每个环节的 heat_score）。"""
        if self.sector_scorer is None:
            return {}
        return {k: {"heat_score": v.heat_score,
                     "avg_change_pct": v.avg_change_pct,
                     "strength": v.strength,
                     "n_up": v.n_up, "n_stocks": v.n_stocks,
                     "best_code": v.best_code,
                     "best_name": v.best_name,
                     "label": v.label}
                for k, v in self.sector_scorer.sector_scores.items()}

    def latest_recommendations(self) -> list:
        """当前推荐池（asdict 列表）。"""
        if self.sector_scorer is None:
            return []
        from dataclasses import asdict
        return [asdict(r) for r in self.sector_scorer.recommendations]

    def latest_llm_rerank(self) -> Optional[dict]:
        """最新 LLM 重排序结果。"""
        if self._llm_last_result is None:
            return None
        from dataclasses import asdict
        return asdict(self._llm_last_result)

    def latest_dynamic_universe_summary(self) -> dict:
        """动态候选池摘要（不传全 373 只代码，只传统计）。"""
        if self.dynamic_universe is None:
            return {}
        return {
            "enabled": self.dynamic_universe.enabled,
            "n_total": len(self.dynamic_universe.codes),
            "active_pool_size": len(self.dynamic_universe.active_codes),
            "by_industry": self.dynamic_universe.industries_breakdown(),
            "last_refresh": self.dynamic_universe.last_refresh_str,
        }

    # ============================================================ 单轮

    def _run_once(self, codes: List[str]) -> None:
        # 1) 拉 tick
        raw = qmt_client.get_ticks(codes)
        if not raw:
            return
        ticks = {c: _to_tick(c, r) for c, r in raw.items()
                 if (r.get("lastPrice") or 0) > 0}
        self._last_ticks = {c: {
            "price": t.price, "open": t.open, "high": t.high, "low": t.low,
            "pre_close": t.pre_close, "change": t.change, "change_pct": t.change_pct,
            "volume": t.volume, "amount": t.amount, "source": t.source,
        } for c, t in ticks.items()}

        # 2) 聚合 bars
        for code, tick in ticks.items():
            self._aggregate_bar(code, tick)

        # 2.5) live 模式：以 broker 为权威源同步本地账本（持仓/现金/总资产）。
        # 修复 live 路径长期 Bug——原实现只在 paper 分支维护 self._positions，
        # 导致 live 下单后退出逻辑永不触发、持仓上限失效、total_asset 恒为
        # INITIAL_CASH（风控回撤保护瘫痪）。每轮拉取真实持仓/资产并合并本地
        # 止损/峰值元数据（broker 不返回），使 live 与 paper 行为一致。
        if self.exec_mode == "live":
            self._sync_broker_positions()

        # 3) 更新持仓最新价 + 峰值价（移动止损用）+ 总资产
        today = datetime.now().date()
        if getattr(self, "_last_daily_date", None) != today:
            self._last_daily_date = today
            # 新的一天：刷新日线上下文（趋势偏置会变化）
            threading.Thread(
                target=self._refresh_daily_async, args=(False,),
                name="daily-refresh-day", daemon=True,
            ).start()
        for code, pos in self._positions.items():
            if code in ticks:
                p = ticks[code].price
                pos.last_price = p
                if pos.peak_price <= 0 or p > pos.peak_price:
                    pos.peak_price = p
                # 止损价更新（按退出范式分两类）
                if STRATEGY_PARAMS.get("exit_mode", "scalp") == "scalp":
                    # 吊灯止损：随峰值上移（峰 - atr_stop_mult×ATR），锁定趋势利润
                    if self.daily is not None:
                        ap = self.daily.atr_pct(code)
                        if ap > 0:
                            chand = pos.peak_price * (1 - STRATEGY_PARAMS["atr_stop_mult"] * ap)
                            if pos.stop_price <= 0 or chand > pos.stop_price:
                                pos.stop_price = chand
                else:
                    # 趋势骑行模式：止损价只设一次（宽幅硬止损，灾难保护），
                    # 不随峰值上移，避免把趋势里的正常回撤误杀。
                    if pos.stop_price <= 0:
                        ap = self.daily.atr_pct(code) if self.daily else 0.0
                        if ap <= 0:
                            ap = abs(STRATEGY_PARAMS.get("stop_loss", -0.03))
                        wide = max(abs(STRATEGY_PARAMS.get("hard_stop_pct", -0.18)),
                                   ap * 6.0)
                        pos.stop_price = round(pos.avg_cost * (1 - wide), 3)
        total_asset = self._total_asset()
        # 首次观测到总资产即作为今日日内盈亏基线（若 init 时未从 equity 快照取到）
        if self._day_open_asset is None:
            self._day_open_asset = total_asset
        self.risk.on_asset_update(total_asset)

        # 6.5) 持久化权益快照（节流 ~60s）——供盘后复盘重建权益曲线 / 当日收益 /
        # 日内最大回撤，避免复盘只能依赖解析自由文本心跳（格式易变、易丢）。
        self._persist_equity(total_asset)

        # 4) 评估退出（所有持仓）——轻量级，总是跑
        # regime 强制清仓：市场转弱时，先无条件平掉所有持仓（regime filter 核心）
        regime_block = (self.regime_force_exit and not self._regime_ok())
        if regime_block:
            logger.warning("regime 强制清仓：市场状态转弱(%s MA%d)，平掉全部持仓",
                           self.regime_index, self.regime_ma)
        for code, pos in list(self._positions.items()):
            if pos.quantity <= 0 or code not in ticks:
                continue
            if regime_block:
                self._handle_sell(Signal(
                    ts=datetime.now(), code=code, name=pos.name,
                    side="SELL", price=ticks[code].price,
                    reason="regime_force_exit"), pos)
                continue
            bars = list(self._bars.get(code, []))
            if len(bars) < 5:
                continue
            exit_sig = self._trend.on_exit(code, pos, ticks[code].price, bars)
            if exit_sig and exit_sig.side == "SELL":
                self._handle_sell(exit_sig, pos)

        # 5) 评估入场 + sector
        # Portfolio select() 调 on_bars 需要 8 指标计算，每只股票~0.5s，30 只需 15s
        # 超过 REFRESH_INTERVAL=3s 会导致上一轮卡住。所以改成每 N tick 跑一次
        portfolio_every_n = 5   # 每 5 tick 跑一次 select + sector
        if self._portfolio is not None:
            if self._tick_count % portfolio_every_n == 0:
                self._run_portfolio_step(ticks)
            else:
                # 其他轮只跑轻量 sector 评估（30ms）
                if self.sector_scorer is not None:
                    self._evaluate_sectors(ticks)
        else:
            self._run_single_step(ticks)
            # 单标的模式：产业链推荐池与策略正交（纯观察），同样每 N tick 轻量维护，
            # 使 /api/sector/recommendations 与 LLM 重排序在单模式下也能工作。
            if self.sector_scorer is not None and \
                    self._tick_count % portfolio_every_n == 0:
                self._evaluate_sectors(ticks)

        # 6) 持久化风控快照（仅状态变化或超过最小间隔时）
        self._maybe_save_risk_snapshot()

    def _maybe_save_risk_snapshot(self) -> None:
        """风控快照写库：状态变化即写，否则按 RISK_SNAPSHOT_MIN_INTERVAL 节流。

        原实现每 10 tick（~30s）无条件写一行 → 累计 28.8 万行，而其中绝大多数
        是字段完全相同的重复快照（未熔断、无交易时 payload 一字不变），对复盘
        零信息量。保留「变化即写」确保熔断/降仓等事件一个不漏。
        """
        try:
            snap = self.risk.snapshot()
            fp = json.dumps(snap, sort_keys=True, ensure_ascii=False,
                            default=str)
            now = time.time()
            changed = (fp != self._last_risk_fp)
            stale = (now - self._last_risk_snap_ts) >= RISK_SNAPSHOT_MIN_INTERVAL
            if not (changed or stale):
                return
            self.storage.save_risk_snapshot(snap)
            self._last_risk_fp = fp
            self._last_risk_snap_ts = now
        except Exception as e:
            logger.debug("风控快照写入失败(继续): %s", e)

    def _save_signal(self, sig: Signal) -> None:
        """信号入库（HOLD 默认不入库）。

        原实现把每个候选股每 tick 的信号全部写库，含大量「HOLD / score<4.0」
        这类零信息量行：实测 signals 表 319 万行（单日最高 81.8 万），而同期
        fills 只有 301 行。HOLD 仍以 DEBUG 日志保留可诊断性。
        开关：config.settings.PERSIST_HOLD_SIGNALS。
        """
        if sig.side == "HOLD" and not PERSIST_HOLD_SIGNALS:
            logger.debug("HOLD %s score=%.2f %s", sig.code, sig.score, sig.reason)
            return
        self.storage.save_signal(sig)

    # ----- single mode -----

    def _apply_momentum_gate(self, candidate_codes: set) -> set:
        """动量闸门：只保留 N 日动量前 N 名（且动量必须为正）。

        【P0 修正 2026-09-02】原实现把候选池拆成 static / dynamic 两半，只对
        static 排名，dynamic **无条件放行**（注释声称「其自有 sector_scorer 动量
        机制」，但 sector_scorer 只是观察用途，不参与入场决策）—— 结果是占候选池
        ~70% 的动态股完全不受动量筛选。现在（momentum_scope="all"）静态+动态统一
        排名，语义与回测的横截面动量排名一致；设为 "static" 可回到旧行为。

        无日线数据的标的无法参与动量排名（momentum_60d 返回 0），因此依赖
        ``require_daily_data`` 在策略层拦下，而不是在这里默默放行。
        """
        if not STRATEGY_PARAMS.get("momentum_rank", False):
            return candidate_codes
        if self.daily is None:
            return candidate_codes
        top_n = int(STRATEGY_PARAMS.get("momentum_top_n", 6))
        lookback = int(STRATEGY_PARAMS.get("momentum_lookback", 60))
        scope = STRATEGY_PARAMS.get("momentum_scope", "all")
        if scope == "static":
            # 旧行为（仅供回归对照）：只对静态池排名，动态池放行
            static = [c for c in candidate_codes if c in STOCK_CODES]
            dynamic = candidate_codes - set(static)
            top = set(self.daily.top_momentum(static, top_n, lookback))
            return (top | dynamic) & candidate_codes
        # 默认：静态 + 动态统一横截面排名
        top = set(self.daily.top_momentum(
            sorted(candidate_codes), top_n, lookback))
        return top & candidate_codes

    def _regime_ok(self) -> bool:
        """市场环境是否允许交易（与回测 regime_ok 一致）。

        - "off"：恒为 True（不过滤）
        - "index"：regime_index 收盘 > MA(regime_ma) 才放行
        - "breadth"：>= thresh 比例个股站上各自 MA(regime_ma) 才放行
        无日线数据时保守放行（避免 warmup 冻结）。
        """
        if self.regime_mode == "off" or self.daily is None:
            return True
        try:
            if self.regime_mode == "index":
                return self.daily.index_above_ma(
                    self.regime_index, self.regime_ma)
            if self.regime_mode == "breadth":
                return self.daily.breadth_above_ma(
                    self.regime_ma, self.regime_breadth_thresh)
        except Exception as e:
            logger.debug("regime 计算失败，保守放行: %s", e)
        return True

    def _run_single_step(self, ticks: Dict[str, Tick]) -> None:
        held_codes = {c for c, p in self._positions.items() if p.quantity > 0}
        current_prices = {c: t.price for c, t in ticks.items()}
        # 候选 = 静态 + 动态
        candidate_codes = set(STOCK_CODES)
        if self.dynamic_universe is not None:
            candidate_codes.update(self.dynamic_universe.active_codes)
        candidate_codes -= INDEX_CODES
        candidate_codes = self._apply_momentum_gate(candidate_codes)

        # regime 入场闸门：市场状态不佳时不开新仓（已有持仓由 step4 强制清仓处理）
        regime_ok = self._regime_ok()
        if not regime_ok:
            logger.debug("regime 门关闭，跳过单标的入场")

        for code, tick in ticks.items():
            if code in held_codes or code not in candidate_codes:
                continue
            if not regime_ok:
                continue
            # 并发持仓上限：达上限则不再开新仓（已持仓照常管理/退出）
            if len([p for p in self._positions.values() if p.quantity > 0]) >= self.max_positions:
                continue
            bars = list(self._bars.get(code, []))
            if len(bars) < 60:
                continue
            sig = self.strategy.on_bars(code, tick.name, bars)
            self._save_signal(sig)
            if sig.side != "BUY":
                continue
            self._handle_buy(sig, tick, current_prices)

    # ----- portfolio mode -----

    def _run_portfolio_step(self, ticks: Dict[str, Tick]) -> None:
        # 注意：以下逐轮追踪日志一律 DEBUG。主循环每 3s 一轮、每 5 轮进这里，
        # 放在 INFO 会把 quant_system.log 撑爆（实测 535MB / 63 万条同类记录）。
        logger.debug("_run_portfolio_step: enter, ticks=%d", len(ticks))
        # regime 入场闸门：市场状态不佳时不开新仓（已有持仓由 step4 强制清仓处理）
        regime_ok = self._regime_ok()
        if not regime_ok:
            logger.debug("_run_portfolio_step: regime 门关闭，跳过组合入场")

        # 候选 = 静态 STOCK_CODES + 动态活跃池
        candidate_codes = set(STOCK_CODES)
        if self.dynamic_universe is not None:
            candidate_codes.update(self.dynamic_universe.active_codes)
        candidate_codes -= INDEX_CODES
        candidate_codes = self._apply_momentum_gate(candidate_codes)
        logger.debug("_run_portfolio_step: candidate_codes=%d regime_ok=%s",
                     len(candidate_codes), regime_ok)

        if regime_ok:
            codes_to_bars = {
                code: (ticks[code].name if code in ticks else code,
                       list(self._bars.get(code, [])))
                for code in candidate_codes
            }
            targets = self._portfolio.select(codes_to_bars)
            # 保存所有目标信号
            for sig in targets:
                self._save_signal(sig)
            current_prices = {c: t.price for c, t in ticks.items()}
            total_asset = self._total_asset()
            held_positions = {c: p for c, p in self._positions.items()
                              if p.quantity > 0}
            orders = self._portfolio.plan_rebalance(
                targets=targets,
                positions=held_positions,
                current_prices=current_prices,
                cash=self._cash,
                total_asset=total_asset,
            )
            for order in orders:
                self._execute_order(order, targets_map={s.code: s for s in targets})
            logger.debug("_run_portfolio_step: orders=%d", len(orders))

        # 产业链热度评估 + 推荐池（每轮跑，不受 regime 门影响，供观察）
        if self.sector_scorer is not None:
            self._evaluate_sectors(ticks)
            logger.debug("_run_portfolio_step: sectors=%d",
                         len(self.sector_scorer.sector_scores))

    def _execute_order(self, order: Order, targets_map: Dict[str, Signal]) -> None:
        """执行 Order（被 plan_rebalance 调用）。"""
        sig = targets_map.get(order.code)
        if order.side == "SELL":
            pos = self._positions.get(order.code)
            if pos and pos.quantity > 0:
                sig_obj = Signal(ts=datetime.now(), code=order.code,
                                 name=pos.name, side="SELL",
                                 price=order.price, reason="portfolio rebalance")
                self._handle_sell(sig_obj, pos)
        elif order.side == "BUY":
            if sig is None:
                return
            tick_price = order.price
            class _Tick:
                pass
            tick = _Tick()
            tick.price = tick_price
            tick.name = UNIVERSE.get(order.code, order.code)
            sig_obj = Signal(ts=datetime.now(), code=order.code, name=tick.name,
                             side="BUY", score=sig.score, price=tick_price,
                             reason="portfolio target")
            self._handle_buy(sig_obj, tick, {order.code: tick_price})

    # ============================================================ bars

    def _volume_delta(self, code: str, tick: Tick) -> Tuple[int, float]:
        """把「当日累计量」换算成本轮增量。

        tick.volume/amount 来自 xtdata 全推快照，语义是**当日累计**（实测
        盘后恒定：13.4万/45.9万/51.2万手）。原实现按轮 ``+=`` 累计量，
        一分钟 20 轮会把 bar 成交量放大 ~2116×，导致 volume_surge 因子失真、
        live 与回测口径不一致。这里只取增量；累计量回退（换日/重连）时重置。
        """
        cum_v = int(tick.volume or 0)
        cum_a = float(tick.amount or 0.0)
        prev_v = self._last_cum_vol.get(code)
        prev_a = self._last_cum_amt.get(code)
        self._last_cum_vol[code] = cum_v
        self._last_cum_amt[code] = cum_a
        if prev_v is None:
            # 首次见到该标的：无从判断增量，记 0，避免把全天量灌进第一根 bar
            return 0, 0.0
        if cum_v < prev_v:
            # 累计量回退 = 换日或数据源重置（mock 模式为随机值）：以当前值为增量
            return cum_v, cum_a
        return cum_v - prev_v, max(0.0, cum_a - (prev_a or 0.0))

    def _aggregate_bar(self, code: str, tick: Tick) -> None:
        dq = self._bars[code]
        bucket = tick.ts.replace(second=0, microsecond=0)
        d_vol, d_amt = self._volume_delta(code, tick)
        if dq and dq[-1].ts == bucket:
            b = dq[-1]
            b.high = max(b.high, tick.high)
            b.low = min(b.low, tick.low)
            b.close = tick.price
            b.volume += d_vol
            b.amount += d_amt
        else:
            dq.append(Bar(
                ts=bucket, open=tick.open if not dq else dq[-1].close,
                high=tick.high, low=tick.low, close=tick.price,
                volume=d_vol, amount=d_amt,
            ))

    # ============================================================ 下单

    def _log_reject_once(self, code: str, reason: str) -> None:
        """同一 (标的, 原因) 至少 60s 内只记一次，原因变化或换标的才立即再记。

        避免主循环把同一条拒绝理由每轮刷一遍（实盘曾刷出 63 万条同类日志），
        同时允许长期持续的同一拒绝以分钟级频率保留可见性，便于人工观察。
        """
        now = time.time()
        if (self._last_reject.get(code) == reason
                and now - self._last_reject_ts.get(code, 0.0) < 60.0):
            return
        self._last_reject[code] = reason
        self._last_reject_ts[code] = now
        logger.info("BUY 拒绝 %s: %s", code, reason)

    def _handle_buy(self, sig: Signal, tick, current_prices: Dict) -> None:
        scale = self.risk.position_scale
        if scale <= 0:
            return
        price = float(getattr(tick, "price", 0) or 0)
        if price <= 0:
            return
        # 账户级硬阻断先查（熔断 / 日内次数打满）——放在昂贵的 ATR+仓位
        # 计算之前，避免每轮白算一遍再被同一理由拒掉。
        blocked = self.risk.account_block_reason(self._daily_trade_count)
        if blocked:
            self._log_reject_once(sig.code, blocked)
            return
        # 波动率目标仓位：用日线 ATR% 做风险平价（替换固定的 5 万）
        atr_pct = self.daily.atr_pct(sig.code) if self.daily else 0.0
        # 若取不到日线 ATR，退化为固定金额估算（波动约 3%）
        if atr_pct <= 0:
            atr_pct = abs(STRATEGY_PARAMS.get("stop_loss", -0.03))
        # ---- 先算真实止损/止盈距离（下面 sizing 可选择复用它）----
        if STRATEGY_PARAMS.get("exit_mode", "scalp") == "trend":
            # 趋势骑行：宽幅硬止损作灾难保护；不设固定止盈（让趋势奔跑）
            stop_dist = max(abs(STRATEGY_PARAMS.get("hard_stop_pct", -0.18)),
                            atr_pct * 6.0)
            stop_price = round(price * (1 - stop_dist), 3)
            target_price = round(price * 10.0, 3)   # 实质不触发
        else:
            stop_dist = max(abs(STRATEGY_PARAMS["stop_loss"]),
                            atr_pct * STRATEGY_PARAMS["atr_stop_mult"])
            target_dist = max(abs(STRATEGY_PARAMS["take_profit"]),
                              atr_pct * STRATEGY_PARAMS["tp_atr_mult"])
            stop_price = round(price * (1 - stop_dist), 3)
            target_price = round(price * (1 + target_dist), 3)
        # ---- 仓位计算 ----
        # sizing_stop 默认用「紧止损」口径，**与已验证的回测基准一致**
        # （BacktestConfig.trend_vol_sizing 默认 False）。置 True 则改用真实趋势
        # 止损做风险平价（单笔风险名实相符，但敞口会大幅下降）—— 切换前必须
        # 先跑 walk-forward A/B，详见 settings.STRATEGY_PARAMS["trend_vol_sizing"]。
        if (STRATEGY_PARAMS.get("exit_mode", "scalp") == "trend"
                and STRATEGY_PARAMS.get("trend_vol_sizing", False)):
            raw_qty = self.sizer.size(price, self._total_asset(), atr_pct,
                                      stop_pct=stop_dist)
        else:
            raw_qty = self.sizer.size(price, self._total_asset(), atr_pct,
                                      fixed_stop=STRATEGY_PARAMS.get("stop_loss"))
        qty = int(raw_qty * scale)
        # ---- 买入力（buying power）夹紧【P0 修正 2026-09-02】----
        # 原实现完全没有现金校验，paper 分支直接 ``self._cash -= qty*price``。
        # 配置上 max_single_position_pct(0.30) × max_positions(5) = 150%，本身就
        # 允许超配；目前未爆只是运气（最新快照 cash=93,999 / 已满仓 90%），
        # 再多一个槽位或一次高价入场现金就为负 → 污染 _total_asset() → 风控回撤
        # 计算 → 断路器误判。回测侧一直有这个夹紧（backtest_daily.py:887-890），
        # 这里补齐，使 live/paper 与回测的资金约束一致。
        qty = self._clamp_to_buying_power(sig.code, qty, price)
        if qty <= 0:
            return
        order = Order(ts=datetime.now(), code=sig.code, side="BUY",
                      quantity=qty, price=price, order_type="limit",
                      account="cash")
        ok, reason = self.risk.can_open(
            order, self._positions, self._total_asset(),
            self._daily_trade_count)
        if not ok:
            self._log_reject_once(sig.code, reason)
            return
        self._last_reject.pop(sig.code, None)   # 放行后复位，下次拒绝可再记
        if self.analyst.enabled:
            self._fire_ai(sig.code, sig.name, self._bars_snapshot(sig.code))
        _reason = getattr(sig, "reason", "") or ""
        if self.exec_mode == "live":
            res = qmt_broker.place_order(sig.code, "BUY", qty, price, "cash")
            logger.info("[LIVE] BUY %s x %s @ %s → %s", sig.code, qty, price, res)
            system_notice(
                "SUCCESS", "交易",
                f"提交买入委托 {sig.code} {sig.name} ×{qty} @{price:.3f} "
                f"止损{stop_price:.3f} 理由={_reason} → {res.get('ok')}")
            self.storage.save_order(order, res.get("order_id"),
                                     mode=self.exec_mode)
            # 日内交易计数：live 与 paper 一致地计入（原实现漏计 → 实盘
            # max_daily_trades 闸值永不触发）。
            self._daily_trade_count += 1
            if res.get("ok"):
                # live 同样维护本地账本（原实现只在 paper 分支维护，导致实盘
                # 退出逻辑/持仓上限/总资产追踪全失效）。下单即乐观建仓，成交后
                # 由 _sync_broker_positions 以 broker 为权威源校正。
                self._pos_meta[sig.code] = {
                    "stop_price": stop_price, "target_price": target_price,
                    "peak_price": price, "open_date": datetime.now(),
                }
                self._positions[sig.code] = Position(
                    code=sig.code, name=sig.name, quantity=qty,
                    avg_cost=price, last_price=price, open_date=datetime.now(),
                    peak_price=price, stop_price=stop_price,
                    target_price=target_price)
                if res.get("order_id"):
                    # 异步轮询成交回报，避免阻塞主行情循环（原来同步 sleep 1s）
                    threading.Thread(
                        target=self._poll_and_record_fill,
                        args=(res["order_id"], sig.code, "BUY", qty, price,
                              order.account),
                        name=f"fill-{sig.code}-buy", daemon=True,
                    ).start()
        else:
            self._cash -= qty * price
            self._positions[sig.code] = Position(
                code=sig.code, name=sig.name, quantity=qty,
                avg_cost=price, last_price=price, open_date=datetime.now(),
                peak_price=price, stop_price=stop_price,
                target_price=target_price)
            self._daily_trade_count += 1
            self.storage.save_order(order, mode=self.exec_mode)
            self.storage.save_fill(Fill(
                ts=datetime.now(), code=sig.code, side="BUY",
                quantity=qty, price=price, amount=qty * price,
                account="cash",
            ), mode=self.exec_mode)
            logger.info("[PAPER] BUY %s x %s @ %s (止损%.2f 目标%.2f) cash=%.2f",
                        sig.code, qty, price, stop_price, target_price,
                        self._cash)
            system_notice(
                "SUCCESS", "交易",
                f"买入成交 {sig.code} {sig.name} ×{qty} @{price:.3f} "
                f"止损{stop_price:.3f} 现金余{self._cash:,.2f} 理由={_reason}")

    def _clamp_to_buying_power(self, code: str, qty: int,
                               price: float) -> int:
        """把下单数量夹紧到可用现金内（整百股）。不够买 100 股则返回 0。

        为什么必需：既有实现下单前只过 RiskManager（它只看单笔金额 / 占总资产
        比例，**不看钱够不够**），paper 分支直接扣现金。max_single_position_pct
        × max_positions = 0.30 × 5 = 150%，结构上就允许透支。透支后：
          ① _total_asset() 被负现金拉低 → RiskManager 回撤计算失真 → 断路器误触发；
          ② paper 回报与真实可执行性脱钩（券商会直接拒单）；live 则会乐观建仓
            一个根本没成交的仓位。
        回测侧一直有这个夹紧（``if cost > cash: qty = ...``），这里补齐。
        """
        if qty <= 0 or price <= 0:
            return 0
        buffer_pct = float(PORTFOLIO_CONFIG.get("cash_buffer_pct", 0.0) or 0.0)
        reserve = self._total_asset() * buffer_pct
        avail = self._cash - reserve
        if avail <= 0:
            self._log_reject_once(
                code, f"insufficient_cash: 可用{avail:.0f} ≤ 0"
                      f"（现金{self._cash:.0f} 预留{reserve:.0f}）")
            return 0
        affordable = int((avail / price) // 100) * 100
        if affordable <= 0:
            self._log_reject_once(
                code, f"insufficient_cash: 可用{avail:.0f} 不足 1 手"
                      f"（{price:.3f}×100={price*100:.0f}）")
            return 0
        if affordable < qty:
            logger.info("BUY 数量受现金夹紧 %s: %d → %d 股（可用现金 %.0f）",
                        code, qty, affordable, avail)
            return affordable
        return qty

    def _handle_sell(self, sig: Signal, pos: Position) -> None:
        qty = pos.quantity
        price = sig.price or pos.last_price
        _reason = getattr(sig, "reason", "") or ""
        _pnl = (price - pos.avg_cost) * qty
        order = Order(ts=datetime.now(), code=sig.code, side="SELL",
                      quantity=qty, price=price, order_type="limit",
                      account="cash")
        if self.exec_mode == "live":
            res = qmt_broker.place_order(sig.code, "SELL", qty, price, "cash")
            logger.info("[LIVE] SELL %s x %s @ %s → %s", sig.code, qty, price, res)
            system_notice(
                "WARNING", "交易",
                f"提交卖出委托 {sig.code} {pos.name} ×{qty} @{price:.3f} "
                f"盈亏{_pnl:+.2f} 理由={_reason} → {res.get('ok')}")
            self.storage.save_order(order, res.get("order_id"),
                                     mode=self.exec_mode)
            # 日内交易计数：live 与 paper 一致地计入。
            self._daily_trade_count += 1
            if res.get("ok"):
                # 乐观清仓本地账本（成交后由 _sync_broker_positions 校正）
                if sig.code in self._positions:
                    self._positions[sig.code].quantity = 0
                if res.get("order_id"):
                    # 异步轮询成交回报，避免阻塞主行情循环
                    threading.Thread(
                        target=self._poll_and_record_fill,
                        args=(res["order_id"], sig.code, "SELL", qty, price,
                              order.account, pos.avg_cost),
                        name=f"fill-{sig.code}-sell", daemon=True,
                    ).start()
        else:
            proceeds = qty * price
            self._cash += proceeds
            self.risk.on_fill(Fill(
                ts=datetime.now(), code=pos.code, side="SELL",
                quantity=qty, price=price, amount=proceeds, account="cash",
            ), avg_cost=pos.avg_cost,
               total_asset=self._total_asset())
            self.storage.save_order(order, mode=self.exec_mode)
            self.storage.save_fill(Fill(
                ts=datetime.now(), code=pos.code, side="SELL",
                quantity=qty, price=price, amount=proceeds, account="cash",
            ), mode=self.exec_mode)
            self._daily_trade_count += 1
            pos.quantity = 0
            logger.info("[PAPER] SELL %s x %s @ %s, pnl=%.2f cash=%.2f",
                        pos.code, qty, price,
                        (price - pos.avg_cost) * qty, self._cash)
            system_notice(
                "WARNING", "交易",
                f"卖出成交 {sig.code} {pos.name} ×{qty} @{price:.3f} "
                f"盈亏{_pnl:+.2f} 现金余{self._cash:,.2f} 理由={_reason}")

    def _poll_and_record_fill(self, order_id: str, code: str, side: str,
                              qty: int, price: float, account: str,
                              avg_cost: float = 0.0):
        """异步轮询成交回报（不阻塞主行情循环）。

        实盘委托可能数秒后才完全成交（大单 / 非活跃时段），原实现只 sleep 1s
        轮询一次，会漏记延迟成交 → 数据库 / 复盘缺失该笔。改为最多轮询 6 次
        （间隔 1s，共 ~6s）覆盖常见延迟；命中即落库（卖出同步更新风控已实现
        盈亏），全部轮询仍未命中则记 WARN 便于排查（可能已撤单 / 未成交）。
        """
        for _attempt in range(6):
            time.sleep(1.0)
            try:
                trades = qmt_broker.get_trades(account)
            except Exception:
                trades = []
            for t in trades:
                if str(t.get("order_id")) == str(order_id):
                    fill_price = float(t.get("traded_price") or price)
                    fill_qty = int(t.get("traded_volume") or qty)
                    self.storage.save_fill(Fill(
                        ts=datetime.now(), code=code, side=side,
                        quantity=fill_qty, price=fill_price,
                        amount=fill_price * fill_qty, account=account,
                    ), order_id=order_id, mode=self.exec_mode)
                    if side == "SELL" and avg_cost > 0:
                        self.risk.on_fill(Fill(
                            ts=datetime.now(), code=code, side=side,
                            quantity=fill_qty, price=fill_price,
                            amount=fill_price * fill_qty, account=account,
                        ), avg_cost=avg_cost,
                           total_asset=self._total_asset())
                    return
        logger.warning("成交回报轮询超时未命中 order_id=%s（可能已撤单/未成交）",
                       order_id)

    # ============================================================ 持仓同步

    def _persist_equity(self, total_asset: float) -> None:
        """节流持久化权益快照（~60s 一次）到 storage.qmt.db。

        供盘后复盘（strategy/review_daily.py）重建当日权益曲线、当日收益率、
        日内最大回撤。原实现只把总资产写进自由文本心跳，复盘需脆弱地解析日志；
        这里落结构化表，复盘数据更稳更全。首轮与回撤刷新时必存，其余节流。
        """
        now = time.time()
        if now - self._last_equity_ts < 60.0:
            return
        self._peak_equity = max(self._peak_equity, total_asset)
        market_value = total_asset - self._cash
        positions_count = len([p for p in self._positions.values()
                                if p.quantity > 0])
        dd = ((total_asset - self._peak_equity) / self._peak_equity
              if self._peak_equity > 0 else 0.0)
        try:
            self.storage.save_equity_snapshot(
                total_asset, self._cash, market_value, positions_count, dd,
                mode=self.exec_mode)
        except Exception as e:
            logger.debug("权益快照持久化失败(继续): %s", e)
        # 重启延续：同步持久化 paper 账本（持仓/现金/日内计数/峰值）
        if self.exec_mode == "paper":
            self._save_engine_state()
        self._last_equity_ts = now

    # ============================================================ 重启延续
    # paper 模式本地账本（持仓/现金/日内计数/峰值）持久化，跨重启不丢。

    def _restore_engine_state(self) -> None:
        """从 SQLite 恢复 paper 账本，使连续多日测试在重启后延续。"""
        try:
            row = self.storage.load_engine_state()
            if not row:
                return
            self._cash = float(row.get("cash") or 0.0)
            positions: Dict[str, "Position"] = {}
            for p in (json.loads(row.get("positions") or "[]") or []):
                try:
                    od = p.get("open_date")
                    positions[p["code"]] = Position(
                        code=p["code"], name=p.get("name", ""),
                        quantity=int(p.get("quantity") or 0),
                        avg_cost=float(p.get("avg_cost") or 0.0),
                        last_price=float(p.get("last_price") or 0.0),
                        open_date=datetime.fromisoformat(od) if od else None,
                        peak_price=float(p.get("peak_price") or 0.0),
                        stop_price=float(p.get("stop_price") or 0.0),
                        target_price=float(p.get("target_price") or 0.0),
                    )
                except Exception as ex:
                    logger.warning("恢复持仓失败 %s: %s", p, ex)
            self._positions = positions
            self._daily_trade_count = int(row.get("daily_trade_count") or 0)
            self.risk._daily_pnl = float(row.get("daily_pnl") or 0.0)
            self.risk._consec_loss = int(row.get("consec_loss") or 0)
            self.risk._peak_asset = float(row.get("peak_asset") or 0.0)
            self._day_open_asset = (float(row["day_open_asset"])
                                    if row.get("day_open_asset") is not None else None)
            self._tick_count = int(row.get("tick_count") or 0)
            self._peak_equity = float(row.get("peak_equity") or 0.0)
            self._trade_date = (date.fromisoformat(row["trade_date"])
                                if row.get("trade_date") else date.today())
            self._reset_daily_if_needed()  # 跨日归一（新交易日重置日内计数）
            logger.info("恢复引擎状态: 持仓 %d 现金 %.2f 日内交易 %d 已实现 %.2f",
                        len(self._positions), self._cash,
                        self._daily_trade_count, self.risk._daily_pnl)
        except Exception as e:
            logger.warning("恢复引擎状态失败（从初始状态启动）: %s", e)

    def _save_engine_state(self) -> None:
        """持久化当前 paper 账本（节流由调用方控制，约 60s 一次 + 优雅退出时）。"""
        try:
            positions = [{
                "code": p.code, "name": p.name, "quantity": p.quantity,
                "avg_cost": p.avg_cost, "last_price": p.last_price,
                "open_date": (p.open_date.isoformat() if p.open_date else None),
                "peak_price": p.peak_price, "stop_price": p.stop_price,
                "target_price": p.target_price,
            } for p in self._positions.values() if p.quantity > 0]
            self.storage.save_engine_state({
                "cash": self._cash,
                "positions": json.dumps(positions, ensure_ascii=False),
                "daily_trade_count": self._daily_trade_count,
                "daily_pnl": self.risk._daily_pnl,
                "consec_loss": self.risk._consec_loss,
                "peak_asset": self.risk._peak_asset,
                "day_open_asset": self._day_open_asset,
                "tick_count": self._tick_count,
                "peak_equity": self._peak_equity,
                "trade_date": self._trade_date.isoformat(),
            })
        except Exception as e:
            logger.debug("保存引擎状态失败: %s", e)

    def _sync_broker_positions(self) -> None:
        """live 模式：每轮以 broker 为权威源，把本地账本与真实持仓/资产对齐。

        修复 live 路径长期 Bug：原实现只在 paper 分支维护 ``self._positions``
        与 ``self._cash``，实盘下单后本地账本为空 → ① 退出逻辑（step4）永不
        触发、止损/趋势破位失效；② ``max_positions`` 并发上限永不生效、可无限
        加仓；③ ``total_asset`` 恒为 ``INITIAL_CASH``，风控回撤断路器在实盘
        完全瘫痪。这里从 broker 拉取真实持仓与资产，合并本地止损/峰值元数据
        （broker 不返回），使实盘与回测/模拟盘行为一致。所有异常吞掉，绝不因
        一次查询失败拖垮主循环。
        """
        if not qmt_broker.is_connected:
            return
        try:
            asset = qmt_broker.get_asset("cash")
            if asset:
                self._cash = float(asset.get("cash") or 0.0)
            raw = qmt_broker.get_positions("cash") or []
            held: set = {p.get("code") for p in raw
                         if (p.get("quantity") or 0) > 0}
            wide = abs(STRATEGY_PARAMS.get("hard_stop_pct", -0.18))
            for p in raw:
                code = p.get("code")
                qty = int(p.get("quantity") or 0)
                if qty <= 0 or not code:
                    continue
                avg = float(p.get("avg_cost") or 0.0)
                mv = float(p.get("market_value") or 0.0)
                meta = self._pos_meta.get(code, {})
                last = (mv / qty) if qty > 0 and mv > 0 else (avg or 0.0)
                if code in self._positions:
                    pos = self._positions[code]
                    pos.quantity = qty
                    pos.avg_cost = avg
                    pos.last_price = last
                    # 合并/兜底止损价（本策略开的仓用 ATR 止损；broker 同步来的
                    # 历史仓无元数据则给一个宽幅硬止损作灾难保护）
                    if pos.stop_price <= 0:
                        pos.stop_price = round(avg * (1 - wide), 3) \
                            if avg > 0 else 0.0
                    if pos.peak_price <= 0:
                        pos.peak_price = last
                else:
                    self._positions[code] = Position(
                        code=code, name=UNIVERSE.get(code, code),
                        quantity=qty, avg_cost=avg, last_price=last,
                        open_date=meta.get("open_date"),
                        peak_price=meta.get("peak_price", 0.0) or last,
                        stop_price=meta.get("stop_price", 0.0)
                        or (round(avg * (1 - wide), 3) if avg > 0 else 0.0),
                        target_price=meta.get("target_price", 0.0),
                    )
            # 清掉 broker 已无持仓的本地记录（数量置 0，退出逻辑自然跳过；
            # 元数据保留以便复盘）。
            for code in list(self._positions.keys()):
                if code not in held:
                    self._positions[code].quantity = 0
        except Exception as e:
            logger.debug("broker 持仓同步失败(继续): %s", e)

    def _init_positions_from_broker(self) -> None:
        """从 broker 拉真实持仓初始化 ledger（live 模式启动时）。"""
        try:
            if not qmt_broker.connect():
                logger.warning("无法连接 broker，跳过持仓初始化")
                return
            asset = qmt_broker.get_asset("cash") or {}
            self._cash = float(asset.get("cash") or 0)
            for p in qmt_broker.get_positions("cash"):
                code = p.get("code")
                if not code:
                    continue
                self._positions[code] = Position(
                    code=code,
                    name=UNIVERSE.get(code, code),
                    quantity=int(p.get("quantity") or 0),
                    avg_cost=float(p.get("avg_cost") or 0),
                    last_price=float(p.get("avg_cost") or 0),
                    open_date=None,
                )
            logger.info("持仓初始化: %d 只, cash=%.2f",
                        sum(1 for p in self._positions.values() if p.quantity > 0),
                        self._cash)
        except Exception as e:
            logger.warning("持仓初始化失败: %s", e)

    # ============================================================ AI

    def _fire_ai(self, code: str, name: str, bars: List[Bar]) -> None:
        if code in self._pending_ai and self._pending_ai[code].is_alive():
            return

        def _worker():
            try:
                ind = self._trend._compute_indicators([Bar(
                    ts=b.ts, open=b.open, high=b.high, low=b.low,
                    close=b.close, volume=b.volume, amount=b.amount,
                ) for b in bars])
                ai = self.analyst.analyze(code, name, ind, market_ctx={})
                if ai:
                    self.storage.save_ai(ai)
                    logger.info("AI %s → %s conf=%.2f",
                                code, ai.stance, ai.confidence)
                    if ai.stance == "bearish" and ai.confidence >= 0.7:
                        logger.warning("AI 抑制买入 %s", code)
            except Exception as e:
                logger.debug("AI worker err: %s", e)
            finally:
                self._pending_ai.pop(code, None)

        t = threading.Thread(target=_worker, name=f"ai-{code}", daemon=True)
        self._pending_ai[code] = t
        t.start()

    # ============================================================ 产业链评分

    def _evaluate_sectors(self, ticks: Dict[str, Tick]) -> None:
        """每轮调 SectorScorer 评估环节热度 + 生成推荐池。

        market_data 只用 ticks 里有效的股票代码（保证 size 可预测）。
        """
        market_data = {}
        for code, t in ticks.items():
            market_data[code] = {
                "change_pct": t.change_pct,
                "volume_ratio": 1.0,
                "price": t.price,
            }
        try:
            self.sector_scorer.evaluate_sectors(market_data)
            # 用 TrendStrategy 评分作为 tech_scores
            tech_scores: Dict[str, float] = {}
            for code in list(market_data.keys()):
                bars = list(self._bars.get(code, []))
                if len(bars) >= 60:
                    sig = self._trend.on_bars(code, market_data.get(code, {}).get("name", code), bars)
                    tech_scores[code] = sig.score
            self.sector_scorer.build_recommendations(market_data, tech_scores)
            # 每 5 tick 持久化一次
            if self._tick_count % 5 == 0:
                for r in self.sector_scorer.recommendations:
                    self.storage.save_sector_recommendation(r)
                # 推荐池内容不变时不重复记 INFO：原实现每 5 tick 刷一条，
                # 一天可刷出上万条同内容日志（是 1.02GB 日志的成因之一）。
                best = self.sector_scorer.best_target()
                fp = (best.code if best else "none",
                      len(self.sector_scorer.recommendations))
                if fp != getattr(self, "_last_reco_fp", None):
                    self._last_reco_fp = fp
                    logger.info("产业链推荐池更新: top=%s 数量=%d", fp[0], fp[1])
                else:
                    logger.debug("产业链推荐池未变: top=%s 数量=%d", fp[0], fp[1])
            # 触发 LLM 重排序（每 N tick 一次，异步）
            if (self.llm_reranker is not None
                    and self.llm_reranker.enabled
                    and self._tick_count % self._llm_rerank_interval == 0
                    and self.sector_scorer.recommendations):
                self._fire_llm_rerank(market_data)
        except Exception as e:
            import traceback
            # 单行 WARN 足矣，避免每轮把全量 traceback 刷进日志（曾是 CPU 空转主因）。
            # 完整 traceback 仅 DEBUG 级别保留，便于排障且不污染 INFO/默认日志。
            logger.warning("sector 评估失败: %s", e)
            logger.debug("sector 评估失败 traceback:\n%s",
                         "".join(traceback.format_exception(type(e), e, e.__traceback__)))

    def ensure_recommendations(self) -> bool:
        """推荐池为空时，尝试补建（供手动 LLM 重排序兜底）。

        兜底顺序：
          1) 引擎主循环每 N tick 会自动调 _evaluate_sectors 生成推荐池；但手动点击
             rerank 可能在首条推荐生成前到达，或单模式自动循环尚未跑过 sector。
          2) 有最近一次实时行情（_last_ticks）时，直接复用即时重建。
          3) 无实时行情（刚重启 / 非交易时段 mock 回落）时，回退读取 SQLite 持久化的
             最近一次推荐池，使重排序即便在无 tick 场景下也能工作，而非恒报失败。
        """
        if self.sector_scorer is None:
            return False
        if self.sector_scorer.recommendations:
            return True
        # 兜底 2：用最近一次实时行情重建
        ticks = getattr(self, "_last_ticks", None)
        if ticks:
            market_data = {}
            for code, t in ticks.items():
                if isinstance(t, dict):
                    chg = t.get("change_pct") or 0
                    price = t.get("price") or 0
                else:
                    chg = getattr(t, "change_pct", 0) or 0
                    price = getattr(t, "price", 0) or 0
                market_data[code] = {
                    "change_pct": float(chg),
                    "volume_ratio": 1.0,
                    "price": float(price),
                }
            try:
                self.sector_scorer.evaluate_sectors(market_data)
                tech_scores: Dict[str, float] = {}
                for code in list(market_data.keys()):
                    bars = list(self._bars.get(code, []))
                    if len(bars) >= 60:
                        sig = self._trend.on_bars(
                            code, market_data.get(code, {}).get("name", code), bars)
                        tech_scores[code] = sig.score
                self.sector_scorer.build_recommendations(market_data, tech_scores)
                if self.sector_scorer.recommendations:
                    return True
            except Exception as e:
                logger.warning("ensure_recommendations(实时重建) 失败: %s", e)
        # 兜底 3：回退 SQLite 持久化的最近推荐池（无需实时 tick）
        try:
            from strategy.sector_scorer import StockRecommendation
            pool_size = self.sector_scorer.config.get("recommendation_pool_size", 5)
            rows = self.storage.get_sector_recommendations(limit=pool_size)
            if rows:
                recs = []
                for r in rows:
                    try:
                        recs.append(StockRecommendation(
                            ts=datetime.fromisoformat(r["ts"]),
                            code=r["code"], name=r["name"],
                            sector=r["sector"], sector_label=r["sector_label"],
                            composite=float(r.get("composite") or 0.0),
                            heat_contribution=float(r.get("heat_contribution") or 0.0),
                            tech_score=float(r.get("tech_score") or 0.0),
                            fundamental_score=float(r.get("fundamental_score") or 0.0),
                            pe=r.get("pe"), roe=r.get("roe"),
                            change_pct=float(r.get("change_pct") or 0.0),
                            reason=r.get("reason") or "",
                        ))
                    except Exception:
                        continue
                if recs:
                    self.sector_scorer.load_recommendations(recs)
                    logger.info("ensure_recommendations: 回退 SQLite 推荐池 %d 只",
                                len(recs))
                    return True
        except Exception as e:
            logger.debug("ensure_recommendations(SQLite 回退) 失败: %s", e)
        return False

    def _fire_llm_rerank(self, market_data: dict) -> None:
        """异步触发 LLM 重排序（不阻塞主循环）。"""
        if self.llm_reranker is None or not self.llm_reranker.enabled:
            return
        sector_scores = self.sector_scorer.sector_scores
        recs = list(self.sector_scorer.recommendations)
        # 大盘上下文（取指数 tick）
        market_ctx = {}
        for idx_code in ("000001.SH", "399006.SZ", "000300.SH"):
            md = market_data.get(idx_code)
            if md:
                market_ctx[f"指数_{idx_code}"] = f"{md['change_pct']:+.2f}%"

        def _worker():
            try:
                result = self.llm_reranker.rerank(recs, sector_scores, market_ctx)
                if result:
                    self._llm_last_result = result
                    logger.info("LLM 重排序完成: macro=%s top3=%s",
                                result.macro_view,
                                result.ranked_codes[:3])
            except Exception as e:
                logger.warning("LLM rerank 失败: %s", e)

        threading.Thread(target=_worker, name="llm-rerank", daemon=True).start()

    def _bars_snapshot(self, code: str) -> List[Bar]:
        return list(self._bars.get(code, []))

    # ============================================================ 辅助

    def _total_asset(self) -> float:
        v = self._cash
        for p in self._positions.values():
            v += p.market_value
        return v

    def _shutdown(self) -> None:
        # 重启延续：优雅退出前先落盘 paper 账本，确保下次启动时交易记录延续
        try:
            if self.exec_mode == "paper":
                self._save_engine_state()
        except Exception as e:
            logger.debug("退出前保存引擎状态失败: %s", e)
        if self._broker_reconnector:
            self._broker_reconnector.stop()
        try:
            self.storage.close()
        except Exception:
            pass
        system_notice("SYSTEM", "系统", "引擎已停止（优雅退出）")
        logger.info("Engine 已停止")

    # 初始化 last_ticks 避免 SSE 推送时报错
    _last_ticks: Dict[str, dict] = {}
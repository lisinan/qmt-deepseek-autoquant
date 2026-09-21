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
from datetime import date, datetime, timedelta, time as dtime
from typing import Deque, Dict, List, Optional, Tuple

from config.settings import (
    BAR_WARMUP, BAR_WARMUP_BUDGET_SEC, BAR_WARMUP_DOWNLOAD,
    BAR_WARMUP_MAX_STALE_DAYS, EXECUTION_MODE, IDLE_REFRESH_INTERVAL,
    INDEX_CODES, INITIAL_CASH, LOG_DIR, MARKET_INDEX_CODE,
    PERSIST_HOLD_SIGNALS, PORTFOLIO_CONFIG, REFRESH_INTERVAL, RISK_PARAMS,
    RISK_SNAPSHOT_MIN_INTERVAL, SESSION_GUARD, SINGLETON_LOCK, STOCK_CODES,
    STRATEGY_MODE, STRATEGY_PARAMS, T1_RESTRICTION, ENTRY_PROTECT_MINUTES,
    UNIVERSE,
)
from core.notices import system_notice, latest_notices
from core.auto_reconnect import AutoReconnector
from core.market_calendar import (
    is_trading_day, is_trading_time, seconds_to_next_session, session_label,
)
from risk.position_sizer import PositionSizer
from strategy.daily_context import DailyContext
from core.broker import qmt_broker
from core.data_models import Bar, Fill, Order, Position, Signal, Tick
from core.qmt_client import qmt_client
from ai.analyst import AIAnalyst, ai_analyst
from ai.llm_reranker import LLMReranker, llm_reranker
from data.dynamic_universe import DynamicUniverse, dynamic_universe
from data.stock_names import get_stock_name
from risk.manager import RiskManager
from storage.db import Storage
from strategy.base import BaseStrategy
from strategy.portfolio_strategy import PortfolioStrategy
from strategy.sector_scorer import SectorScorer, sector_scorer
from strategy.trend_strategy import TrendStrategy

logger = logging.getLogger(__name__)


def is_t1_locked(open_date, trade_date: date) -> bool:
    """A 股 T+1：判断某持仓在 trade_date 当天是否处于「当日买入不可卖」锁定。

    - ``open_date`` 为 None（历史账本缺字段 / 测试桩）→ 视为可卖，不误杀；
    - ``open_date`` 的日期 == 当前交易日 → 锁定（当日买入当日不能卖）；
    - 否则（上一交易日及更早买入）→ 可卖。

    语义与回测 ``BacktestConfig.t1_restriction`` 一致（按建仓日整仓粒度），
    不区分「加仓部分」——本策略不日内加仓，整仓 open_date 即建仓日。
    """
    if open_date is None:
        return False
    try:
        return open_date.date() == trade_date
    except AttributeError:
        return False


def is_in_entry_protection(open_date, protect_minutes: int,
                           now: Optional[datetime] = None) -> bool:
    """建仓保护期：仓位是否仍处于「首个可卖交易日开盘后 protect_minutes 分钟内」。

    与 T+1 叠加——T+1 锁当日，本函数锁首个可卖交易日的早盘窗口，专门消除轮动
    换入候选被分钟级波动「换入即误伤」的隐患。语义（A 股连续竞价 09:30 开盘）：
      - open_date 之后第一个交易日 09:30 起，向后 protect_minutes 分钟为保护窗；
      - 窗口内：不触发任何退出（破位/止损/日线兜底/轮动换出/regime 强平）；
      - protect_minutes<=0 → 关闭；open_date 为 None → 不锁定（保守，不误杀）。
    仅在「卖出被提上日程」时调用（非每 tick），日历开销可忽略。
    ``now`` 可注入（默认 datetime.now()），便于单元测试确定性。
    """
    if protect_minutes <= 0 or open_date is None:
        return False
    try:
        now = now or datetime.now()
        first_sellable = None
        for off in range(1, 9):
            d = (open_date.date() + timedelta(days=off))
            if is_trading_day(d):
                first_sellable = d
                break
        if first_sellable is None:
            return False
        session_open = datetime.combine(first_sellable, dtime(9, 30))
        protect_until = session_open + timedelta(minutes=protect_minutes)
        return now < protect_until
    except AttributeError:
        return False

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
    # ---- 决策频率控制（2026-09-02 #E 路径）----
    # Live 不再每 tick 调 TrendStrategy.on_bars（其本质是 minute-bar 评分，
    # 与生产已验证的 backtest_daily.score_daily 口径不一致）。改为：
    #   ① 每 ENTRY_DECISION_INTERVAL_SEC 调一次 on_daily_features 算 BUY；
    #   ② minute bar 仍每 tick 走 _aggregate_bar（供 last_price / peak / 盘中止损）；
    #   ③ on_exit 仍每 tick 调（trend 模式下走 DailyContext，破位判断每日稳定）。
    # 结果：CPU 下降 ~99%（12 只股 × 240 分钟/天 × 6 指标 评分 → 每 N 分钟 1 次）。
    ENTRY_DECISION_INTERVAL_SEC = 300     # 5 分钟（远低于 backtest_daily 频率，
                                          # 但对日线 score 足够频，不会过 trading）
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
        # 北向资金协同闸门（全新正交轴，2026-09-19 落盘）：引擎启动时预热北向序列
        self.northbound_mode = STRATEGY_PARAMS.get("northbound_mode", "off")
        self.nb_lookback = int(STRATEGY_PARAMS.get("nb_lookback", 20))
        self.nb_series = {}
        self.nb_dates = []
        if self.northbound_mode != "off":
            try:
                from data.northbound_cache import preload_northbound
                self.nb_series = preload_northbound()
                self.nb_dates = sorted(self.nb_series.keys())
                logger.info("北向序列已加载: %d 日", len(self.nb_series))
            except Exception as e:
                logger.warning("北向数据加载失败，北向闸门失效(降级为无北向): %s", e)
                self.nb_series = {}
        self.regime_index = STRATEGY_PARAMS.get(
            "regime_index", MARKET_INDEX_CODE)
        self.regime_ma = int(STRATEGY_PARAMS.get("regime_ma", 60))
        self.regime_breadth_thresh = float(
            STRATEGY_PARAMS.get("regime_breadth_thresh", 0.5))
        self.regime_force_exit = bool(
            STRATEGY_PARAMS.get("regime_force_exit", False))
        # 并发持仓上限（集中度控制）：经 IS/OOS + 多折双验证，5 显著优于 8。
        self.max_positions = int(STRATEGY_PARAMS.get("max_positions", 8))
        # 板块轮动 / 弱换强（默认关闭，确认后开启；不改变默认交易行为）
        self.enable_rotation = bool(
            STRATEGY_PARAMS.get("enable_rotation", False))
        self.rotation_min_score_gap = float(
            STRATEGY_PARAMS.get("rotation_min_score_gap", 2.0))
        self.rotation_cooldown_sec = float(
            STRATEGY_PARAMS.get("rotation_cooldown_sec", 1800))
        self.rotation_hot_top_n = int(
            STRATEGY_PARAMS.get("rotation_hot_top_n", 15))
        self.rotation_intraday_breakout_pct = float(
            STRATEGY_PARAMS.get("rotation_intraday_breakout_pct", 1.5))
        self.rotation_min_score_gap_hot = float(
            STRATEGY_PARAMS.get("rotation_min_score_gap_hot", 0.0))
        self.rotation_max_swaps_per_eval = int(
            STRATEGY_PARAMS.get("rotation_max_swaps_per_eval", 3))
        # 4/5 补强空槽（2026-09-12 新增，用户确认开启）：组合差一仓时，直接把
        # 空槽补上合格热板块候选（纯买入、不卖 existing）。默认开；置 False 即回滚到
        # 仅满仓才轮换的旧行为。
        self.rotation_fill_empty_slot = bool(
            STRATEGY_PARAMS.get("rotation_fill_empty_slot", True))
        # A 股 T+1 硬约束（2026-09-14）：当日买入的仓位当日不可卖。统一在
        # _handle_sell 单点拦截；仅交易非 A 股时置 False（settings.T1_RESTRICTION）。
        self.t1_restriction = bool(T1_RESTRICTION)
        # 建仓保护期（2026-09-14，叠加于 T+1）：首个可卖交易日开盘后 N 分钟内不退出，
        # 避免轮动换入候选被分钟级波动误伤。N<=0 关闭（settings.ENTRY_PROTECT_MINUTES）。
        self.entry_protect_minutes = int(ENTRY_PROTECT_MINUTES)
        # 卖出拦截提示去重缓存（2026-09-17 降噪）：同一持仓同一拦截类型每日仅提示一次，
        # 避免 T+1 / 建仓保护期拦截对单只持仓逐 tick 重复刷 notices.log（实测单日可达数千条）。
        # key=(code, open_date, type) -> 已提示日期；跨日自动清理。移除本缓存即恢复逐次全量提示。
        self._sell_block_notice_cache: dict = {}
        self._sell_block_notice_day: str = ""
        # ---- 观察篮（manual entry）状态【2026-09-18】----
        # _manual_positions：当前由观察篮建仓、且仍在持有的代码集合。
        #   每轮用「实际持仓」做交集修剪，故被卖掉（硬止损 / 日内强平 / 手工卖出）
        #   后自动移出，无需在 _handle_sell 各处埋点。
        # _manual_sold_today：当日被卖出的观察篮代码 —— 当日不再自动补回，
        #   否则「日内 -6% 强平 → 观察篮立即补仓」会形成买入/平仓空转。
        #   跨自然日自动清空。
        self._manual_positions: set = set()
        self._manual_sold_today: set = set()
        self._manual_sold_date: str = ""
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
        # 日内硬止损强平当日执行标记（每交易日最多触发一次，防每轮重复提交）
        self._daily_flatten_done: bool = False
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
        # 【2026-09-02 #E】日线决策节流戳：上次调 on_daily_features 的时间。
        self._last_entry_decision_ts: float = 0.0

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
            self._apply_pending_reset()

    def _apply_pending_reset(self) -> None:
        """应用待处理的 paper 账本复位（一次性、幂等）。

        【2026-09-21】存在 ``storage/.reset_paper.flag`` 时，把**内存中**的现金、
        持仓、风控基线复位到初始资金，写回 DB 后删除标志。

        为什么需要它：引擎每 60s 把内存状态写回 ``engine_state``，直接在库里改
        会被覆盖。用「启动期一次性标志」复位，就不用强求「先停引擎再改库」的时序，
        无论何时重启都能确保复位生效一次。
        """
        flag = Path(LOG_DIR).parent / "storage" / ".reset_paper.flag"
        if not flag.exists():
            return
        import json as _json
        try:
            cfg = _json.loads(flag.read_text(encoding="utf-8") or "{}")
            capital = float(cfg.get("capital") or INITIAL_CASH)
            start = str(cfg.get("start_date") or "")
        except Exception as e:
            logger.warning("复位标志解析失败，忽略: %s", e)
            flag.unlink(missing_ok=True)
            return
        self._cash = capital
        self._positions = {}
        self._peak_equity = capital
        self._day_open_asset = capital
        self._daily_trade_count = 0
        self.risk.reset_all(capital)
        if start:
            try:
                self._trade_date = date.fromisoformat(start)
            except Exception:
                pass
        # 清掉复位前的脏快照：脚本归档清空后、引擎若仍在运行会继续写入旧资产值，
        # 不清会在新周期曲线上留下「80 万 → 100 万」的假暴涨。
        try:
            self.storage.clear_equity_before(f"{start or date.today().isoformat()}T00:00:00")
        except Exception as e:
            logger.debug("清理复位前快照失败: %s", e)
        try:
            self._save_engine_state()
            self.storage.save_equity_snapshot(
                self._total_asset() or capital, self._cash,
                self._market_value(), len(self._positions), 0.0)
        except Exception as e:
            logger.warning("复位后落盘失败: %s", e)
        flag.unlink(missing_ok=True)
        logger.warning("paper 账本已复位：现金 %.2f、零持仓、风控/回撤基线清零"
                       "（新周期起始交易日 %s）", capital, start or date.today())
        system_notice("SUCCESS", "风控",
                      f"paper 账本已复位：初始资金 {capital:,.0f}，"
                      f"从 {start or date.today()} 重新计量")

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
        当前参数下 0.19×5 = 95% 理论敞口（已与现金夹紧上限 95% 自洽）、最坏情形 ≈ -17%，
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

    def _notify_if_locked_out_of_hot_sector(self, held_codes: set) -> None:
        """可观测性：组合已满且未持有当前热点板块时主动提示（不改变交易行为）。

        背景：策略为「集中最强 5 只 + 趋势骑行」，一旦 5 槽满且持仓未破趋势，
        入场循环（_run_single_step / _run_portfolio_step）会直接跳过所有候选，
        既不评分也不轮动，于是无法参与新热板块（如光模块）的上涨。这里仅把
        这一「错过热板块」状态显式播报出来，便于 notices / 盘后复盘发现，
        而不是静默错过。真正的修复（板块轮动 / 弱换强）需用户确认后开启。
        """
        try:
            now = time.time()
            if now - getattr(self, "_last_locked_note_ts", 0.0) < 600:
                return
            # 当前推荐热点：优先 LLM 重排 top，回退板块评分推荐池
            hot: list = []
            res = getattr(self, "_llm_last_result", None)
            if res is not None and getattr(res, "ranked_codes", None):
                hot = list(res.ranked_codes[:5])
            elif self.sector_scorer is not None:
                recs = list(self.sector_scorer.recommendations)[:5]
                hot = [getattr(r, "code", None) for r in recs]
                hot = [c for c in hot if c]
            if not hot:
                return
            missing = [c for c in hot if c not in held_codes]
            if not missing:
                return  # 热点已在持仓中，无需提示
            self._last_locked_note_ts = now
            shown = ", ".join(missing[:3])
            system_notice(
                "SYSTEM", "风控",
                f"组合已满({len(held_codes)}/{self.max_positions})且未持有当前热点"
                f"[{shown} 等]，将错过热板块行情。如需参与请在确认后开启板块轮动"
                f"(enable_rotation)。")
        except Exception:
            pass

    def _hot_codes_set(self) -> set:
        """当前「热板块」候选代码集合：LLM rerank top + 板块推荐池 top。

        仅当某候选落在 AI 观测层明确看多的名单里，才允许其参与轮换换入，
        避免把资金轮动到无热度的边缘标的。两组来源任一为空都不影响另一组。
        """
        hot: set = set()
        res = getattr(self, "_llm_last_result", None)
        if res is not None and getattr(res, "ranked_codes", None):
            hot.update(res.ranked_codes[:5])
        if self.sector_scorer is not None:
            try:
                recs = list(self.sector_scorer.recommendations)[:self.rotation_hot_top_n]
                hot.update(r.code for r in recs if getattr(r, "code", None))
            except Exception:
                pass
        return hot

    def _maybe_rotate(self, ticks: Dict[str, Tick]) -> None:
        """板块轮动 / 弱换强（opt-in，enable_rotation=True 时生效）。

        仅当组合已满（无空槽）时介入——有空槽时普通入场逻辑已处理。核心目标：在
        满仓时也能捕捉新热板块（如光模块）的开盘/盘中上涨，而不是被水下老仓锁死。

        关键修正（2026-09-07 盘后，解决「开盘踏空」）：
        原实现完全依赖**日线**信号（动量前 N + on_daily_features 评分差），而日线在
        开盘跳空/盘中突破时严重滞后——光模块开盘放量拉升，日线评分与 60 日动量要等
        收盘才更新，于是「热板块候选明明在涨、AI 观察层也 bullish，轮动却因日线评分差
        不够而迟迟不换」。本次改为**日内信号驱动**：
          1) 热板块候选（LLM rerank top ∪ 板块推荐池 top）**绕过动量闸门**——动量前 N
             是日线滞后指标，不该挡住 AI 已确认的热板块突破。
          2) 候选带**日内突破**（tick.change_pct ≥ rotation_intraday_breakout_pct）时，
             用「AI 热度 + 日内突破」替代滞后的日线评分差：只要候选评分 ≥ 最弱持仓
             （gap 阈值降为 rotation_min_score_gap_hot，默认 0），即可换入。
          3) 支持**批量换仓**（rotation_max_swaps_per_eval）：一个板块常多只联动
             （如光模块 300308/300502/300394），一次性把多只最弱老仓换出，避免
             一次只换 1 只、冷却 1800s 导致错过整段行情。
        防抖动/防反复被割：依旧只在满仓时介入；只换出**最弱**持仓；候选必须落在 AI
        热板块名单（非随机噪声）；rotation_cooldown_sec 仍作全局冷却。
        """
        if not getattr(self, "enable_rotation", False):
            return
        now = time.time()
        if now - getattr(self, "_last_rotate_ts", 0.0) < self.rotation_cooldown_sec:
            return
        # 评估节流：on_daily_features 有一定开销，避免每轮全量重算
        if now - getattr(self, "_last_rotate_eval_ts", 0.0) < 120.0:
            return
        held = {c: p for c, p in self._positions.items() if p.quantity > 0}
        if len(held) < self.max_positions - 1:
            return
        if self.daily is None or self.strategy is None:
            return

        # 候选池：全宇宙 − 指数 − 已持仓；过动量闸门。
        base = set(STOCK_CODES)
        if self.dynamic_universe is not None:
            base.update(self.dynamic_universe.active_codes)
        base -= INDEX_CODES
        base -= set(held.keys())
        gated = self._apply_momentum_gate(base)
        hot = self._hot_codes_set()
        # 热板块候选绕过动量闸门（日线滞后不该挡 AI 已确认的热度）
        cand = gated | (hot & base)

        # —— 4/5 补强空槽分支（2026-09-12 新增；用户确认开启）——
        # 组合差一仓（len==max_positions-1）时，不强制卖 existing，直接把空槽补上
        # 一个合格热板块候选（AI 热板块 + 日内突破/日线 BUY 双判定），回到 5/5 且
        # 第 5 仓是确认热度标的。复用现有 hot 集合、突破/BUY 判定与冷却节流。
        # 仅补一仓、不进入 5/5 的弱换强逻辑；旋钮 rotation_fill_empty_slot 可一键回滚。
        if len(held) == self.max_positions - 1:
            if not getattr(self, "rotation_fill_empty_slot", True):
                return
            slot_cands = []
            for code in cand:
                tick = ticks.get(code)
                if tick is None:
                    continue
                if code not in hot:
                    continue
                feat = self.daily.features(code)
                sig = self.strategy.on_daily_features(code, code, feat)
                if sig is None:
                    continue
                chg = float(getattr(tick, "change_pct", 0) or 0.0)
                is_breakout = chg >= self.rotation_intraday_breakout_pct
                if not is_breakout and sig.side != "BUY":
                    continue
                slot_cands.append((code, sig, sig.score, is_breakout))
            self._last_rotate_eval_ts = now
            if not slot_cands:
                return
            slot_cands.sort(key=lambda x: x[2], reverse=True)
            code, sig, eff_score, is_breakout = slot_cands[0]
            bp = ticks[code]
            sig.price = float(getattr(bp, "price", 0) or 0)
            self._handle_buy(sig, bp, {c: t.price for c, t in ticks.items()})
            self._last_rotate_ts = now
            system_notice(
                "SYSTEM", "交易",
                f"板块轮动补强空槽：买入{code}（热板块/日内突破）")
            return

        # 评估每只候选：日内突破 + 日线评分（用于与最弱持仓比差）
        cands = []
        for code in cand:
            tick = ticks.get(code)
            if tick is None:
                continue
            if code not in hot:
                continue
            feat = self.daily.features(code)
            sig = self.strategy.on_daily_features(code, code, feat)
            if sig is None:
                continue
            chg = float(getattr(tick, "change_pct", 0) or 0.0)
            is_breakout = chg >= self.rotation_intraday_breakout_pct
            # 非突破时必须日线 BUY 才考虑；突破时日内强度已替代日线评分作 conviction
            if not is_breakout and sig.side != "BUY":
                continue
            cands.append((code, sig, sig.score, is_breakout))
        if not cands:
            self._last_rotate_eval_ts = now
            return
        # 候选按评分降序（突破候选也带真实日线分，排序天然合理）
        cands.sort(key=lambda x: x[2], reverse=True)

        # 批量换仓：每轮把若干「最弱老仓」换成「更强热板块候选」
        remaining = dict(held)
        swaps = 0
        swap_pairs = []
        for code, sig, eff_score, is_breakout in cands:
            if swaps >= self.rotation_max_swaps_per_eval:
                break
            if not remaining:
                break
            # 当前最弱持仓
            weakest_code, weakest_score = None, float("inf")
            for c, p in remaining.items():
                f = self.daily.features(c)
                s = self.strategy.on_daily_features(c, p.name, f)
                sc = s.score if s is not None else 0.0
                if sc < weakest_score:
                    weakest_score, weakest_code = sc, c
            if weakest_code is None:
                break
            req_gap = (self.rotation_min_score_gap_hot if is_breakout
                       else self.rotation_min_score_gap)
            if eff_score - weakest_score < req_gap:
                continue
            # 执行：先卖最弱（释放槽位+现金），再买候选
            wpos = remaining[weakest_code]
            wsig = Signal(
                ts=datetime.now(), code=weakest_code, name=wpos.name,
                side="SELL", price=wpos.last_price,
                reason=f"板块轮动换出(评分{weakest_score:.1f}<候选{eff_score:.1f}"
                       f"{'|日内突破' if is_breakout else ''})")
            self._handle_sell(wsig, wpos)
            self._last_rotate_ts = now
            bp = ticks[code]
            sig.price = float(getattr(bp, "price", 0) or 0)
            self._handle_buy(sig, bp, {c: t.price for c, t in ticks.items()})
            swap_pairs.append((weakest_code, code))
            del remaining[weakest_code]
            swaps += 1
        if swaps > 0:
            self._last_rotate_eval_ts = now
            pairs = "; ".join(f"{a}→{b}" for a, b in swap_pairs)
            system_notice(
                "SYSTEM", "交易",
                f"板块轮动(批量×{swaps})：{pairs}（热板块/日内突破）")

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
            self._daily_flatten_done = False  # 新交易日允许再次日内强平
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
        # 实盘：尽早连接 broker（在系统提示前），使启动横幅与持仓初始化准确
        if self.exec_mode == "live":
            if qmt_broker.connect():
                system_notice("SYSTEM", "系统", "实盘券商已连接 (XtQuantTrader)。")
                self._init_positions_from_broker()  # 启动即拉真实持仓
            else:
                logger.warning("Live broker 连接失败，继续运行（下单不会成交）")
        self.broker_mode = qmt_broker.mode
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

        # 启动 broker 自动重连（仅在真实断线时重连，不主动拆链）
        if self._auto_reconnect_enabled and self.exec_mode == "live":
            self._broker_reconnector = AutoReconnector(
                name="broker",
                connect_fn=lambda: qmt_broker.connect(force=False),
            )
            qmt_broker.set_on_disconnect(
                lambda: self._broker_reconnector.notify_disconnect()
                if self._broker_reconnector else None)
            # 【2026-09-16 优化】交易连接重建后恢复行情订阅 + 持仓同步，避免
            #   「断连 27 次」后行情端订阅失效、数据/下单路径不同步。
            qmt_broker.set_on_reconnected(self._on_broker_reconnected)
            self._broker_reconnector.start()

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
            "northbound": self._nb_state(),
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
        self.risk.on_asset_update(total_asset, self._day_open_asset or total_asset)

        # 4.4) 日内硬止损强平（2026-09-16 新增）：risk 判定当日亏损达
        #   daily_stop_flatten_pct 后置 flatten_requested，这里平掉全部可卖持仓。
        #   每只经 _handle_sell 单点拦截（T+1 仍生效；force=True 绕过建仓保护期）。
        #   当日仅执行一次（_daily_flatten_done），避免每轮重复提交。
        if self.risk.flatten_requested and not self._daily_flatten_done:
            self._daily_flatten_done = True
            _flat_n = 0
            for _code, _pos in list(self._positions.items()):
                if _pos.quantity <= 0:
                    continue
                self._handle_sell(Signal(
                    ts=datetime.now(), code=_code, name=_pos.name,
                    side="SELL", price=_pos.last_price or _pos.avg_cost,
                    reason="daily_hard_stop_flatten"), _pos, force=True)
                _flat_n += 1
            if _flat_n:
                logger.warning("日内硬止损强平：已提交 %d 只可卖持仓平仓", _flat_n)
                system_notice(
                    "ERROR", "交易",
                    f"日内硬止损强平：当日账户亏损达阈值，已提交 {_flat_n} 只可卖持仓平仓"
                    f"（T+1 锁定的当日新仓于次交易日自动处理）")

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
            if pos.quantity <= 0:
                continue
            if code not in ticks:
                # 【A 加固 2026-09-03】日线兜底强平：原实现此处直接 continue，
                # 导致无 tick 持仓永远跳过 on_exit（开盘跳空+当轮无tick 的分钟止损
                # 盲区）。无 tick 且非 regime 强平时，改用日线 close 作价格代理重算
                # on_exit，使退出条件仍周期性触发；regime 强平维持原语义（仅在有 tick
                # 时按实时价提交），故无 tick + regime_block 时跳过。
                if not regime_block and not self._is_manual_exempt(code):
                    self._daily_fallback_exit(code, pos)
                continue
            if regime_block:
                self._handle_sell(Signal(
                    ts=datetime.now(), code=code, name=pos.name,
                    side="SELL", price=ticks[code].price,
                    reason="regime_force_exit"), pos)
                continue
            if self._is_manual_exempt(code):
                # 观察篮仓位：只保留 -18% 硬止损（灾难保护），豁免趋势破位/超时/
                # 单日暴跌退出。原因：这些标的多在 MA60 下方，若照常跑 on_exit，
                # 建仓后下一轮就会被「趋势破位离场」卖出 → 买入/卖出空转。
                px = float(getattr(ticks[code], "price", 0) or 0)
                cost = float(getattr(pos, "avg_cost", 0) or 0)
                hard = abs(float(STRATEGY_PARAMS.get("hard_stop_pct", -0.18)))
                if px > 0 and cost > 0 and (px - cost) / cost <= -hard:
                    self._handle_sell(Signal(
                        ts=datetime.now(), code=code, name=pos.name,
                        side="SELL", price=px,
                        reason=f"观察篮硬止损 {(px - cost) / cost * 100:.2f}%"),
                        pos)
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

    def _nb_state(self) -> dict:
        """北向资金轴状态（只读）：闸门决策与 Web 展示共用同一真相源。

        与 ``_regime_ok`` 内的北向分支等价：滚动 ``nb_lookback`` 日累计净买入
        < 0 视为「外资系统性撤离」，gate 模式下不放行新开仓。
        """
        mode = self.northbound_mode
        lookback = self.nb_lookback
        out = {
            "mode": mode,
            "lookback": lookback,
            "loaded": bool(self.nb_series),
            "days": len(self.nb_series),
            "end_date": self.nb_dates[-1] if self.nb_dates else None,
            "rolling_sum": None,
            "latest": None,
            "blocked": False,
        }
        if not self.nb_series:
            return out
        _today = datetime.now().strftime("%Y%m%d")
        keys = [d for d in self.nb_dates if d <= _today]
        win = keys[-lookback:] if lookback > 0 else []
        if win:
            out["rolling_sum"] = round(
                sum(self.nb_series.get(d, 0.0) for d in win), 2)
            out["latest"] = round(self.nb_series.get(win[-1], 0.0), 2)
            out["blocked"] = bool(mode == "gate" and out["rolling_sum"] < 0)
        return out

    def _regime_ok(self) -> bool:
        """市场环境是否允许交易（与回测 regime_ok 一致）。

        - "off"：恒为 True（不过滤）
        - "index"：regime_index 收盘 > MA(regime_ma) 才放行
        - "breadth"：>= thresh 比例个股站上各自 MA(regime_ma) 才放行
        无日线数据时保守放行（避免 warmup 冻结）。
        """
        # 北向协同闸门（全新正交轴，2026-09-19 落盘）：与 regime 正交、独立生效
        # （regime 关闭时也运行），不拦截观察篮（观察篮走 _handle_buy 不经此闸门）。
        # 滚动 nb_lookback 日累计净买入<0 → 不开新仓，对冲外资系统性撤离/隔夜跳空。
        if self._nb_state().get("blocked"):
            return False
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

    def _is_manual_exempt(self, code: str) -> bool:
        """该持仓是否属于「观察篮且豁免趋势类退出」。

        只有同时满足两点才豁免：① 确实由观察篮建仓且仍持有；
        ② STRATEGY_PARAMS.manual_entry_exit_exempt 为 True（可随时关回）。
        关闭豁免或清空观察篮清单，退出逻辑即完全回到已验证的原行为。
        """
        return (code in self._manual_positions
                and bool(STRATEGY_PARAMS.get("manual_entry_exit_exempt", True)))

    def _manual_entry_step(self, ticks: Dict[str, Tick],
                           current_prices: Dict[str, float]) -> None:
        """观察篮建仓【2026-09-18】：绕过入场闸门买入 STRATEGY_PARAMS.manual_entry_codes。

        为什么需要它：2026-09-16 强平后宇宙内全部 trend_up=False，日线闸门整体关闭，
        paper 账户连续两日 100% 现金、零成交 —— 无持仓可评估，自进化闭环失去度量对象。
        用户据此明确要求「买入 LLM 排序前五，有持仓才能更好评估」（明确为 paper 单）。

        绕过什么：动量闸门（momentum_top_n）/ 日线闸门（trend_up or bias）/ 评分阈值。
        **不绕过什么**：_handle_buy 全程照旧 —— 风控熔断、日内次数上限、现金夹紧、
        max_positions、max_position_amount 均生效，只放行「信号闸门」而非「资金闸门」。

        清单每次热读 STRATEGY_PARAMS，改清单无需重启；置空列表即完全恢复策略原行为。
        """
        codes = STRATEGY_PARAMS.get("manual_entry_codes") or []
        if not codes:
            return

        # 跨日清空「当日已卖」集合
        today = datetime.now().strftime("%Y-%m-%d")
        if self._manual_sold_date != today:
            self._manual_sold_date = today
            self._manual_sold_today = set()

        held = {c for c, p in self._positions.items() if p.quantity > 0}
        # 上一轮在观察篮里、现已不在持仓中 → 视为已被卖出，当日不再自动补回
        for c in (self._manual_positions - held):
            self._manual_sold_today.add(c)
        self._manual_positions &= held

        for code in codes:
            if code in held or code in self._manual_sold_today:
                continue
            tick = ticks.get(code)
            if tick is None:
                continue
            price = float(getattr(tick, "price", 0) or 0)
            if price <= 0:
                continue
            sig = Signal(ts=datetime.now(), code=code,
                         name=get_stock_name(code), side="BUY",
                         score=0.0, price=price,
                         reason="manual_entry(观察篮·绕过信号闸门)")
            self._handle_buy(sig, tick, current_prices)
            if code in {c for c, p in self._positions.items() if p.quantity > 0}:
                self._manual_positions.add(code)
                held.add(code)
                logger.info("观察篮建仓成功 %s @ %.3f", code, price)
                system_notice(
                    "INFO", "交易",
                    f"观察篮建仓 {code} {get_stock_name(code)} @{price:.3f}"
                    f"（paper，绕过信号闸门，风控/现金夹紧照旧）")

    def _run_single_step(self, ticks: Dict[str, Tick]) -> None:
        """单标的模式的入场决策（【2026-09-02 #E】已切换为日线路径）。

        之前：每 tick 对每只候选股调 TrendStrategy.on_bars(minute_bars)，
              是 minute-bar 评分，与生产已验证结论不对口（实测负收益）。
        现在：每 ENTRY_DECISION_INTERVAL_SEC 调一次 on_daily_features，
              score 来自 DailyContext（与 backtest_daily 同口径）。
              资金、持仓、趋势入场、cash 夹紧等完整保留。
        """
        # 节流：日线 score 不会变快于 DailyContext 刷新；避免每 tick 重复评分。
        now = time.time()
        if (now - self._last_entry_decision_ts
                < self.ENTRY_DECISION_INTERVAL_SEC):
            return
        self._last_entry_decision_ts = now

        held_codes = {c for c, p in self._positions.items() if p.quantity > 0}
        current_prices = {c: t.price for c, t in ticks.items()}
        # 观察篮：先于信号闸门建仓（独立于动量/日线闸门与评分阈值）
        self._manual_entry_step(ticks, current_prices)
        # 候选 = 静态 + 动态
        candidate_codes = set(STOCK_CODES)
        if self.dynamic_universe is not None:
            candidate_codes.update(self.dynamic_universe.active_codes)
        candidate_codes -= INDEX_CODES
        candidate_codes = self._apply_momentum_gate(candidate_codes)

        # 可观测性 + 轮换：满仓报踏空；4/5 起允许轮换补强空槽（不改变默认交易行为）
        _held_n = len([p for p in self._positions.values() if p.quantity > 0])
        if _held_n >= self.max_positions - 1:
            if _held_n >= self.max_positions:
                self._notify_if_locked_out_of_hot_sector(held_codes)
            self._maybe_rotate(ticks)

        # regime 入场闸门：市场状态不佳时不开新仓（已有持仓由 step4 强制清仓处理）
        regime_ok = self._regime_ok()
        if not regime_ok:
            logger.debug("regime 门关闭，跳过单标的入场")

        for code in candidate_codes:
            if code in held_codes:
                continue
            if not regime_ok:
                continue
            # 并发持仓上限：达上限则不再开新仓
            if len([p for p in self._positions.values()
                    if p.quantity > 0]) >= self.max_positions:
                continue
            # 【#E】日线决策：不再用 minute bars，用 DailyContext.features
            feat = self.daily.features(code) if self.daily else None
            sig = self.strategy.on_daily_features(code, code, feat)
            self._save_signal(sig)
            if sig.side != "BUY":
                continue
            # 成交价用最新 tick（分钟线上的实时价）。若无 tick 则跳过（保护性）。
            tick = ticks.get(code)
            if tick is None:
                continue
            self._handle_buy(sig, tick, current_prices)

    # ----- portfolio mode -----

    def _run_portfolio_step(self, ticks: Dict[str, Tick]) -> None:
        """组合模式的入场决策（【2026-09-02 #E】已切换为日线路径）。

        之前：每 5 tick 用 minute bars 调 PortfolioStrategy.select →
              同样掉进 (b) 揭露的「minute-bar 评分与已验证回测不对口」陷阱。
        现在：节流后用 DailyFeatures 调 select_daily，score 来自 DailyContext，
              与生产已验证的 backtest_daily 同口径。
        """
        logger.debug("_run_portfolio_step: enter, ticks=%d", len(ticks))
        # 节流：日线 score 不会比 DailyContext 刷新更快。
        now = time.time()
        if (now - self._last_entry_decision_ts
                < self.ENTRY_DECISION_INTERVAL_SEC):
            return
        self._last_entry_decision_ts = now

        # regime 入场闸门
        regime_ok = self._regime_ok()
        if not regime_ok:
            logger.debug("_run_portfolio_step: regime 门关闭，跳过组合入场")

        # 候选 = 静态 STOCK_CODES + 动态活跃池
        candidate_codes = set(STOCK_CODES)
        if self.dynamic_universe is not None:
            candidate_codes.update(self.dynamic_universe.active_codes)
        candidate_codes -= INDEX_CODES
        candidate_codes = self._apply_momentum_gate(candidate_codes)

        # 可观测性：组合已满且无热点暴露时提示（不改变交易行为）
        _held = {c for c, p in self._positions.items() if p.quantity > 0}
        if len(_held) >= self.max_positions - 1:
            if len(_held) >= self.max_positions:
                self._notify_if_locked_out_of_hot_sector(_held)
            self._maybe_rotate(ticks)

        logger.debug("_run_portfolio_step: candidate_codes=%d regime_ok=%s",
                     len(candidate_codes), regime_ok)

        if regime_ok:
            # 【#E】日线决策：传 (code, name, features) 而非 (code, name, minute bars)
            codes_to_features = {}
            for code in candidate_codes:
                name = ticks[code].name if code in ticks else code
                feat = self.daily.features(code) if self.daily else None
                codes_to_features[code] = (name, feat)
            targets = self._portfolio.select_daily(codes_to_features)
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
        # 持仓名称解析为规范中文名（兜底回退代码），避免仪表板持仓只显示代码
        disp_name = get_stock_name(sig.code)
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
                    code=sig.code, name=disp_name, quantity=qty,
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
                code=sig.code, name=disp_name, quantity=qty,
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

    def _should_emit_block_notice(self, pos: Position, block_type: str) -> bool:
        """卖出拦截提示去重：同一持仓同一拦截类型当日只发一次 notices 告警。

        返回 True 表示本次应发出（首次或跨日首现），False 表示当日已提示过应跳过。
        最小可逆：删除本方法与两处调用即可恢复逐 tick 全量提示，不影响任何交易拦截逻辑。
        """
        today = date.today().strftime("%Y-%m-%d")
        if self._sell_block_notice_day != today:
            self._sell_block_notice_cache.clear()
            self._sell_block_notice_day = today
        key = f"{pos.code}|{pos.open_date:%Y-%m-%d %H:%M}|{block_type}"
        if self._sell_block_notice_cache.get(key) == today:
            return False
        self._sell_block_notice_cache[key] = today
        return True

    def _handle_sell(self, sig: Signal, pos: Position,
                     now: Optional[datetime] = None, force: bool = False) -> None:
        # ---- A 股 T+1 约束（2026-09-14 修复）----
        # 当日买入的仓位当日不可卖出，否则会生成实盘不可能成交的「同日 round-trip」
        # （如 2026-09-08 300394 于 10:21:14 买入、10:21:17 即被「趋势破位」卖出）。
        # 所有卖出路径（破位/止损/日线兜底/轮动换出/regime 强平）都经此处，单点拦截。
        if self.t1_restriction and is_t1_locked(pos.open_date, date.today()):
            logger.warning(
                "[T+1] 拦截当日卖出 %s：买入于 %s（T+1 当日不可卖，锁定至下一交易日）",
                pos.code, pos.open_date)
            if self._should_emit_block_notice(pos, "T1"):
                system_notice(
                    "WARNING", "交易",
                    f"[T+1 拦截] {pos.code} 于 {pos.open_date:%Y-%m-%d %H:%M} 买入，"
                    f"当日不可卖出（锁定至下一交易日）；跳过理由={getattr(sig, 'reason', '')}")
            return
        # ---- 建仓保护期（2026-09-14，叠加于 T+1）----
        # 首个可卖交易日开盘后 ENTRY_PROTECT_MINUTES 分钟内不退出，避免轮动换入候选
        # 被分钟级波动「换入即误伤」。与 T+1 同理，所有卖出路径经此处单点拦截。
        # force=True（日内硬止损强平）时**绕过**本保护期——硬止损优先级高于防误伤。
        if (not force and self.entry_protect_minutes > 0
                and is_in_entry_protection(pos.open_date, self.entry_protect_minutes, now)):
            logger.warning(
                "[保护期] 拦截卖出 %s：买入于 %s，首个可卖日早盘保护窗内（%d 分钟）",
                pos.code, pos.open_date, self.entry_protect_minutes)
            if self._should_emit_block_notice(pos, "PROT"):
                system_notice(
                    "WARNING", "交易",
                    f"[保护期拦截] {pos.code} 于 {pos.open_date:%Y-%m-%d %H:%M} 买入，"
                    f"首个可卖日开盘后 {self.entry_protect_minutes} 分钟内不退出"
                    f"（防分钟级误伤）；跳过理由={getattr(sig, 'reason', '')}")
            return
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

    def _daily_fallback_exit(self, code: str, pos: "Position",
                             reason_prefix: str = "") -> None:
        """日线兜底强平：闭合「无 tick 持仓」的分钟止损盲区（加固 A，2026-09-03）。

        主循环原实现在 ``code not in ticks`` 时直接 ``continue``，导致当轮没有 tick
        的持仓永远不进入 ``TrendStrategy.on_exit``。开盘跳空、一字跌停、长停复盘或
        稀疏 tick 场景下，硬止损 -18% / 趋势破位 / 持仓超时等退出条件会滞后到下一轮
        有 tick 才触发——这正是实盘分钟止损的结构性盲区。

        此处改用 ``DailyContext`` 最新日线 close 作为价格代理重算 ``on_exit``，使无
        tick 持仓也每隔 tick 得到一次退出再评估（零额外数据成本，日线本就每轮就绪）：
          - self.daily 未就绪或 features(code) 为 None → 安全跳过（不误杀、不卡死）；
          - bars 传空列表：on_exit 内「单日暴跌」分支需连续分钟 bar，此处无需
            （日线 close 已含当日跌幅），故跳过该分支；
          - 复用 on_exit 全部 trend 退出条件（硬止损/趋势破位/超时），与实时路径同口径。
        """
        if self.daily is None:
            return
        feat = self.daily.features(code)
        if feat is None:
            return
        daily_close = feat.close
        if not daily_close or daily_close <= 0:
            return
        exit_sig = self._trend.on_exit(code, pos, daily_close, [])
        if exit_sig and exit_sig.side == "SELL":
            if reason_prefix:
                exit_sig.reason = f"{reason_prefix}|{exit_sig.reason}"
            self._handle_sell(exit_sig, pos)

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
            # 【2026-09-21】恢复风控熔断/连亏时间戳。必须在 _reset_daily_if_needed
            # 之前：僵尸冻结自愈要用 persist 下来的「连亏起算日」算冷却天数。
            self.risk.load_state(row.get("risk_state"))
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
                "risk_state": json.dumps(self.risk.export_state()),
            })
        except Exception as e:
            logger.debug("保存引擎状态失败: %s", e)

    def _on_broker_reconnected(self) -> None:
        """交易连接（重）建立后的恢复钩子（2026-09-16 新增）。

        行情(xtdata)与交易(XtQuantTrader)是两个独立连接：交易端断连恢复**不会**
        自动重建行情端订阅，历史上导致「断连 27 次」后行情订阅失效、数据/下单路径
        不同步。此处重发行情订阅 + 立即重同步真实持仓，使整条交易路径恢复一致。
        """
        try:
            codes = list(UNIVERSE.keys())
            if self.dynamic_universe is not None:
                dyn = self.dynamic_universe.active_codes
                codes = list(dict.fromkeys(codes + dyn))
            qmt_client.subscribe(codes)
            if self.exec_mode == "live":
                self._sync_broker_positions()
            system_notice(
                "SUCCESS", "系统",
                f"券商重连成功：已重发行情订阅({len(codes)} 只)并同步持仓，"
                f"数据/下单路径恢复一致。")
            logger.info("券商重连恢复：重订阅 %d 只行情", len(codes))
        except Exception as e:
            logger.warning("券商重连恢复钩子异常(忽略): %s", e)

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
            qmt_broker.disconnect()
        except Exception:
            pass
        try:
            self.storage.close()
        except Exception:
            pass
        system_notice("SYSTEM", "系统", "引擎已停止（优雅退出）")
        logger.info("Engine 已停止")

    # 初始化 last_ticks 避免 SSE 推送时报错
    _last_ticks: Dict[str, dict] = {}
# -*- coding: utf-8 -*-
"""Tushare 实时行情客户端 —— xtdata 断供时的等价替代行情源。

================================================================================
为什么需要它（2026-11-08）
================================================================================
QMT 升级到 2.1.32.0 后新增「框架/进程白名单」，外部 python 被判 ``illegal pid``
直接拒连（证据：``C:\\pazq_qmt\\userdata\\log\\XtClient_20261108.log`` 里
``quant session <id>, illegal pid <pid>``，session 号与实测逐位对应）。
连带的后果是 ``~/.xtquant/<inst>/xtdata.cfg`` 全部不再生成 ⇒ ``xtdata`` 报
``not authenticated``，**行情与交易两条通道同时失效**。

  - 交易侧：已用大 QMT 文件桥解决（``core/broker_bridge.py`` +
    ``qmt_bridge/qmt_executor.py``，``passorder`` 在策略主线程下单）。
  - 行情侧：就是本模块。

================================================================================
为什么用 ``rt_tick`` 而不是 ``rt_k``
================================================================================
2026-11-08 用 ``trading_config.json`` 里现有 token 实测：

==================  ========  =============  ==================================
接口                结果      限频           备注
==================  ========  =============  ==================================
``pro.rt_k``        无权限    —              需单独开通「A股日线RT」（200 元/月）
``pro.rt_etf_k``    无权限    —              需单独开通「ETF日线RT」（200 元/月）
``pro.rt_tick``     **可用**  **10 次/分钟**  返回**当日累计**日线，非瞬时价
``pro.rt_min``      可用      5 次/分钟      只给最后一根分钟 bar
``ts.realtime_quote`` **可用**  —             新浪源，免积分，能补 ETF
==================  ========  =============  ==================================

决定性事实：``rt_tick`` 的字段是**当日累计**而不是瞬时快照 ——

    pro.rt_tick('300308.SZ')
    → open_price=433.5  high_price=463.33  low_price=427.34  price=448
      pre_price=493.33  volume=54110132    amount=2.397e10

与 ``pro.daily('300308.SZ', '20261106')`` 的
``open=433.50 high=463.33 low=427.34 close=448.00`` **逐项吻合**
⇒ 等价于 xtdata 的当日日线 bar，可直接喂 ``_to_tick``。

选它而不是 ``rt_min``：``rt_min`` 每次只回最新**一根** 1 分钟 bar，要自己攒
当日 OHLC（跨多次调用拼接，中间丢包就错），且限频更紧（5 次/分钟）。

================================================================================
覆盖缺口（必须知道）
================================================================================
``rt_tick`` **不含沪市 ETF** —— 实测 510050.SH / 510300.SH / 512880.SH 全部缺失，
而深市 159915.SZ 有；指数 000300.SH / 399006.SZ 也缺。

这部分自动回落到 ``ts.realtime_quote``（新浪），它同样带当日 OHLC
（``OPEN/HIGH/LOW/PRICE/PRE_CLOSE/VOLUME/AMOUNT``），且**不消耗 tushare 额度**。
观察篮 510050.SH 正是靠这条兜底。

================================================================================
限频与缓存（本模块最容易踩的坑）
================================================================================
引擎主循环 ``REFRESH_INTERVAL=3s``。若每轮都打一次 API，1 分钟就是 20 次，
**超过 ``rt_tick`` 的 10 次上限**。因此四道闸：

1. ``MIN_REFRESH_SEC`` 全局节流（默认 8s ⇒ 上限 7.5 次/分钟，留 25% 余量）；
2. 滑动窗口计数，``rt_tick`` 满 ``RT_TICK_PER_MIN - 1`` 次即停手（留 1 次给探活）；
3. 节流期间**读缓存**，不是返回空 —— 见下条；
4. 撞到「频率超限」记 ``_backoff_until``，60s 内不再重试（否则每轮都撞、
   每轮都刷一条 warning）。

================================================================================
绝不返回空（R9 血泪）
================================================================================
``_XtdClient.SNAPSHOT_STALE_SEC`` 的注释记录过一次真实事故（2026-09-25）：
为了「干净」而丢弃陈旧报价，会让 ``raw`` 变空 → ``_run_once`` 直接 ``return``
→ 整轮不决策，比用旧价更糟。

所以本模块**只打 ``_stale`` 标记、不丢数据**，是否停止按该价建仓交给引擎的
``_update_market_staleness`` 判。同时**绝不**输出 ``_mock=True`` —— 那是合成
行情的专属标记，误标会让 ``ALLOW_MOCK_TRADING=False`` 下的卖出链整段禁用
（``event_engine.py:3664``）。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


# ============================================================ 小工具

def _f(v) -> Optional[float]:
    """安全转 float；None / '' / NaN / 脏值一律 None（不要 0.0，会污染均价）。"""
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x:            # NaN
        return None
    return x


def _i(v) -> int:
    x = _f(v)
    return int(x) if x is not None else 0


def _parse_ts_ms(date_str, time_str=None) -> Optional[int]:
    """把 tushare 的日期/时间串转成**毫秒** epoch（引擎 ``_tick_ts`` 的约定）。

    支持三种输入：
      - ``"2026-11-06 16:59:54"``（rt_tick 的 ``trade_time``）
      - ``("20261106", "16:29:30")``（realtime_quote 的 ``DATE`` + ``TIME``）
      - ``"20261106"``（仅日期）

    解析不出来返回 ``None`` —— 调用方必须按「无法判定」处理，不能当成新鲜。
    """
    try:
        if date_str is None:
            return None
        s = str(date_str).strip()
        if not s:
            return None
        if time_str:
            t = str(time_str).strip()
            # "16:29:30" 或 "162930"
            if ":" not in t and len(t) == 6 and t.isdigit():
                t = f"{t[0:2]}:{t[2:4]}:{t[4:6]}"
            s = f"{s} {t}"
        if len(s) >= 19 and "-" in s:
            dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        else:
            # 纯数字路径。必须先把分隔符全部去掉：
            # realtime_quote 的 ``DATE + TIME`` 拼出来是 ``"20261106 15:00:00"``
            # （8 位 + 空格 + 8 位），直接切 ``s[:14]`` 会得到
            # ``"20261106 15:00"`` 而解析失败 —— 一旦返回 None，该条报价就被
            # 判成「无行情时间 ⇒ 陈旧」，ETF（510050 等）整日无法交易。
            digits = "".join(ch for ch in s if ch.isdigit())
            if len(digits) >= 14:
                dt = datetime.strptime(digits[:14], "%Y%m%d%H%M%S")
            elif len(digits) >= 8:
                dt = datetime.strptime(digits[:8], "%Y%m%d")
            else:
                return None
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _row_to_dict(row) -> Dict[str, Any]:
    """pandas Series / dict / namedtuple 统一成 dict。"""
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    to_dict = getattr(row, "to_dict", None)
    if callable(to_dict):
        try:
            return dict(to_dict())
        except Exception:
            return {}
    try:
        return dict(row._asdict())        # namedtuple
    except Exception:
        return {}


def _iter_rows(df) -> Iterable[Dict[str, Any]]:
    """DataFrame / list[dict] / None → 逐行 dict。"""
    if df is None:
        return []
    empty = getattr(df, "empty", None)
    if empty is True:
        return []
    if isinstance(df, list):
        return [dict(r) for r in df if r is not None]
    to_dict = getattr(df, "to_dict", None)
    if callable(to_dict):
        try:
            return [dict(r) for r in to_dict("records")]
        except Exception:
            return []
    return []


# ============================================================ 默认 realtime_quote

def _default_realtime_quote(codes: List[str]):
    """新浪源的免费实时快照（不消耗 tushare 额度）。

    单独抽成模块级函数是为了给测试留注入点 —— 单测绝不能打真实网络。
    """
    import tushare as ts
    return ts.realtime_quote(ts_code=",".join(codes))


# ============================================================ 客户端

class TushareQuoteClient:
    """与 ``_XtdClient`` 同接口的行情源（``mode = "tushare"``）。

    对外只有 4 个方法，与 ``QMTClient`` 转发面完全对齐：
    ``subscribe / get_ticks / download_history / get_history``。
    """

    mode = "tushare"

    # ---- 限频（实测值，不要凭感觉调大）----
    RT_TICK_PER_MIN = 10          # pro.rt_tick 官方/实测上限
    MIN_REFRESH_SEC = 8.0         # 全局节流：60/8 = 7.5 次/分钟 < 10
    BACKOFF_SEC = 60.0            # 撞到「频率超限」后的静默期

    # ---- 陈旧度（与 _XtdClient 对齐，引擎按同一阈值判 _stale）----
    SNAPSHOT_STALE_SEC = 120.0

    # ---- 缺失重试间隔：某只票 rt_tick + 新浪都拿不到时，60s 内不再追问 ----
    MISS_RETRY_SEC = 60.0

    # ---- 历史 bar 缓存（避免引擎每轮对 38 只各打一次 daily）----
    HISTORY_TTL_SEC = 600.0

    PROBE_CODE = "000001.SZ"

    def __init__(self, pro=None, token: Optional[str] = None,
                 realtime_quote_fn=None):
        self.subscribed: set = set()
        self._lock = threading.RLock()
        self._cache: Dict[str, dict] = {}
        self._miss_at: Dict[str, float] = {}
        self._hist_cache: Dict[str, tuple] = {}
        self._tick_calls: List[float] = []
        self._last_refresh = 0.0
        self._backoff_until = 0.0
        self._rt_tick_ok = True       # 探活后确认；无权限则整源回落新浪
        self._unparsable_warned = False
        self._realtime_quote = realtime_quote_fn or _default_realtime_quote
        self._token = token

        if pro is not None:
            # 测试/诊断注入：跳过 token 解析与探活
            self._pro = pro
            return

        self._pro = self._connect(token)
        if self._pro is None:
            raise RuntimeError("tushare 行情不可用：无 token 或 tushare 未安装")
        self._probe()

    # ---------------------------------------------------------- 连接 / 探活

    @staticmethod
    def _resolve_token(token: Optional[str]) -> str:
        """token 来源优先级：显式传参 > 环境变量 > config.settings（单一事实源）。

        刻意**不**自己读 ``trading_config.json``：``config/settings.py:1089``
        已经实现了「env > trading_config.json」这条优先级，再写一份就是
        「同一事实两份实现 ⇒ 必然静默漂移」。
        """
        if token:
            return str(token).strip()
        import os
        env = (os.environ.get("TUSHARE_TOKEN") or "").strip()
        if env:
            return env
        try:
            from config.settings import TUSHARE_TOKEN
            return (TUSHARE_TOKEN or "").strip()
        except Exception as e:                       # noqa: BLE001
            logger.debug("读取 TUSHARE_TOKEN 失败: %s", e)
            return ""

    def _connect(self, token: Optional[str]):
        tok = self._resolve_token(token)
        if not tok:
            logger.warning("TushareQuoteClient: 无 TUSHARE_TOKEN，无法启用")
            return None
        try:
            import tushare as ts
        except Exception as e:                       # noqa: BLE001
            logger.warning("TushareQuoteClient: tushare 未安装 (%s)", e)
            return None
        # 必须带超时：本类在 ``QMTClient`` 模块单例构造时被实例化，即
        # **import 期**就会探活一次。若不带超时，网络不通时代理会卡满默认
        # 超时，把引擎启动一起拖住。
        timeout = 15.0
        try:
            from config.settings import TUSHARE_TIMEOUT
            timeout = float(TUSHARE_TIMEOUT)
        except Exception:                            # noqa: BLE001
            pass
        try:
            return ts.pro_api(tok, timeout=timeout)
        except TypeError:
            # 老版本 tushare 的 pro_api 不接受 timeout
            try:
                return ts.pro_api(tok)
            except Exception as e:                   # noqa: BLE001
                logger.warning("TushareQuoteClient: pro_api 初始化失败: %s", e)
                return None
        except Exception as e:                       # noqa: BLE001
            logger.warning("TushareQuoteClient: pro_api 初始化失败: %s", e)
            return None

    def _probe(self) -> None:
        """一次真实调用确认究竟哪条路可用；全不可用就抛错让 QMTClient 降级。

        三条结局：
          1. ``rt_tick`` 有返回  → 主路可用（``_rt_tick_ok=True``）
          2. ``rt_tick`` 无权限  → 整源回落新浪，``_rt_tick_ok=False``（仍可交易）
          3. 两条都不行          → 抛 ``RuntimeError``，QMTClient 落 mock

        注意：这里**不能**只因为 ``rt_k`` 无权限就判定「tushare 没有实时行情」。
        2026-11-08 实测：``rt_k`` 无权限但 ``rt_tick`` 可用，两者是独立权限。
        """
        rows = {}
        try:
            rows = self._fetch_rt_tick([self.PROBE_CODE], time.time())
            self._tick_calls = []                     # 探活不占引擎配额
        except Exception as e:                        # noqa: BLE001
            logger.debug("rt_tick 探活异常: %s", e)

        if rows:
            self._rt_tick_ok = True
            logger.info("TushareQuoteClient: rt_tick 可用（%s）", self.PROBE_CODE)
            return

        # rt_tick 不可用 —— 试试新浪源能不能单独撑起来
        try:
            q = self._fetch_realtime_quote([self.PROBE_CODE], time.time())
        except Exception as e:                        # noqa: BLE001
            q = {}
            logger.debug("realtime_quote 探活异常: %s", e)
        if q:
            self._rt_tick_ok = False
            logger.warning(
                "TushareQuoteClient: rt_tick 不可用，全部回落新浪 realtime_quote"
                "（覆盖 ETF 但无五档深度；请核对 token 的实时日线权限）")
            return

        raise RuntimeError("tushare 行情探活失败：rt_tick 与 realtime_quote 均无数据")

    # ---------------------------------------------------------- 限频

    @staticmethod
    def _prune(calls: List[float], now: float) -> int:
        cutoff = now - 60.0
        calls[:] = [t for t in calls if t > cutoff]
        return len(calls)

    def _tick_quota_ok(self, now: float) -> bool:
        used = self._prune(self._tick_calls, now)
        return used < (self.RT_TICK_PER_MIN - 1)

    # ---------------------------------------------------------- 取数

    def _fetch_rt_tick(self, codes: List[str], now: float) -> Dict[str, dict]:
        """``pro.rt_tick`` 批量取当日累计日线。失败返回 ``{}``（绝不抛给上层）。"""
        if not codes:
            return {}
        try:
            df = self._pro.rt_tick(ts_code=",".join(codes))
        except Exception as e:                        # noqa: BLE001
            msg = str(e)
            if "频率" in msg or "超限" in msg:
                self._backoff_until = now + self.BACKOFF_SEC
                logger.warning("rt_tick 频率超限，静默 %.0fs（期间读缓存）",
                               self.BACKOFF_SEC)
            elif "没有接口" in msg or "权限" in msg:
                self._rt_tick_ok = False
                logger.warning("rt_tick 无权限，整源回落 realtime_quote: %s",
                               msg[:120])
            else:
                logger.debug("rt_tick 失败: %s", msg[:200])
            return {}

        self._tick_calls.append(now)
        out: Dict[str, dict] = {}
        for row in _iter_rows(df):
            code = str(row.get("ts_code") or "").strip()
            if not code:
                continue
            ent = self._build_entry(
                code=code,
                price=_f(row.get("price")),
                open_=_f(row.get("open_price")),
                high=_f(row.get("high_price")),
                low=_f(row.get("low_price")),
                pre=_f(row.get("pre_price")),
                volume=_i(row.get("volume")),
                amount=_f(row.get("amount")),
                ts_ms=_parse_ts_ms(row.get("trade_time")),
                now=now,
            )
            if ent:
                out[code] = ent
        return out

    def _fetch_realtime_quote(self, codes: List[str], now: float) -> Dict[str, dict]:
        """新浪源兜底（补 ETF / rt_tick 不覆盖的标的）。不消耗 tushare 额度。"""
        if not codes:
            return {}
        try:
            df = self._realtime_quote(codes)
        except Exception as e:                        # noqa: BLE001
            logger.debug("realtime_quote 失败: %s", str(e)[:200])
            return {}

        out: Dict[str, dict] = {}
        for row in _iter_rows(df):
            raw = {str(k).upper(): v for k, v in row.items()}
            code = str(raw.get("TS_CODE") or "").strip()
            if not code:
                continue
            ent = self._build_entry(
                code=code,
                price=_f(raw.get("PRICE")),
                open_=_f(raw.get("OPEN")),
                high=_f(raw.get("HIGH")),
                low=_f(raw.get("LOW")),
                pre=_f(raw.get("PRE_CLOSE")),
                volume=_i(raw.get("VOLUME")),
                amount=_f(raw.get("AMOUNT")),
                ts_ms=_parse_ts_ms(raw.get("DATE"), raw.get("TIME")),
                now=now,
            )
            if ent:
                out[code] = ent
        return out

    @staticmethod
    def _build_entry(code: str, price: Optional[float], open_: Optional[float],
                     high: Optional[float], low: Optional[float],
                     pre: Optional[float], volume: int, amount: Optional[float],
                     ts_ms: Optional[int], now: float) -> Optional[dict]:
        """统一成内部缓存条目。``price<=0`` 直接丢弃（停牌/脏数据）。"""
        if price is None or price <= 0:
            return None
        lp = float(price)
        return {
            "lastPrice": lp,
            "open": float(open_) if open_ else lp,
            "high": float(high) if high else lp,
            "low": float(low) if low else lp,
            "lastClose": float(pre) if pre else lp,
            "volume": int(volume or 0),
            "amount": float(amount or 0.0),
            "time": ts_ms,
            "_pushed_at": now,
            "_fetched_at": now,
        }

    def _needs_refresh(self, code: str, now: float) -> bool:
        with self._lock:
            ent = self._cache.get(code)
        if ent is not None and (now - float(ent.get("_fetched_at") or 0)) < self.MIN_REFRESH_SEC:
            return False
        # 刚确认过拿不到 → 别每轮都追问（会白烧配额）
        miss = self._miss_at.get(code)
        if miss is not None and (now - miss) < self.MISS_RETRY_SEC:
            return False
        return True

    def _refresh(self, pending: List[str], now: float) -> None:
        self._last_refresh = now
        filled: Dict[str, dict] = {}

        # rt_tick 这条路当前是否可用：有权限 + 未撞限频静默 + 配额未耗尽
        tick_ok = (self._rt_tick_ok
                   and now >= self._backoff_until
                   and self._tick_quota_ok(now))
        if tick_ok:
            filled.update(self._fetch_rt_tick(pending, now))
            if now < self._backoff_until:
                tick_ok = False           # 刚撞到「频率超限」

        rest = [c for c in pending if c not in filled]
        if rest:
            if tick_ok or not self._rt_tick_ok:
                # 正常路径：rt_tick 不覆盖的标的（沪市 ETF / 部分指数）用新浪补
                filled.update(self._fetch_realtime_quote(rest, now))
            else:
                # rt_tick 停摆（无权限 / 配额耗尽 / 频率静默）：
                # 只给**从没拿到过**的标的补一次新浪，避免返回空；
                # 已有缓存的继续用缓存 —— 不把整个宇宙每轮都推给另一个
                # provider（既慢又让同一轮里两套快照时点混在一起）。
                cold = [c for c in rest if c not in self._cache]
                if cold:
                    filled.update(self._fetch_realtime_quote(cold, now))

        with self._lock:
            for code, ent in filled.items():
                self._cache[code] = ent
                self._miss_at.pop(code, None)
            for code in pending:
                # 只对「本来就没有」的标的记负缓存：已有报价的不该被标成缺失
                if code not in filled and code not in self._cache:
                    self._miss_at[code] = now

    # ---------------------------------------------------------- 公开接口

    def subscribe(self, codes: Iterable[str]) -> None:
        """记录订阅集（本无推送，纯为 ``health()`` 展示与 ``reattach`` 复用）。"""
        with self._lock:
            for c in codes:
                if c:
                    self.subscribed.add(c)

    def get_ticks(self, codes: Iterable[str]) -> Dict[str, dict]:
        """与 ``_XtdClient.get_ticks`` 同返回形状；**任何异常都不外抛**。"""
        codes = [c for c in dict.fromkeys(codes) if c]
        if not codes:
            return {}

        now = time.time()
        pending = [c for c in codes if self._needs_refresh(c, now)]
        if pending and (now - self._last_refresh) >= self.MIN_REFRESH_SEC:
            try:
                self._refresh(pending, now)
            except Exception as e:                    # noqa: BLE001
                logger.debug("tushare 行情刷新异常(忽略): %s", e)

        out: Dict[str, dict] = {}
        for c in codes:
            n = self._normalize(c, now)
            if n:
                out[c] = n
        return out

    def _normalize(self, code: str, now: float) -> Optional[dict]:
        with self._lock:
            ent = self._cache.get(code)
        if not ent:
            return None
        t = ent.get("time")
        if t:
            try:
                age: Optional[float] = max(0.0, now - float(t) / 1000.0)
            except (TypeError, ValueError):
                age = None
        else:
            age = None

        # 取不到行情时间 ⇒ **判陈旧**，而不是当成新鲜。
        # `_XtdClient._tick_age_sec` 的 docstring 已经写明「None 含义是无法判定
        # 而非新鲜」，但它自己的 `_stale` 却把 None 判成不陈旧（潜在缺陷）。
        # 这里按 docstring 的语义执行：拿不到时间就不许按这个价建仓。
        if age is None:
            if not self._unparsable_warned:
                self._unparsable_warned = True
                logger.warning(
                    "行情条目缺 trade_time（%s），已按陈旧处理：不会据此建仓。"
                    "若持续出现说明上游字段变了，需核对 rt_tick/realtime_quote 输出。",
                    code)
            stale = True
        else:
            stale = age > self.SNAPSHOT_STALE_SEC

        return {
            "lastPrice": float(ent["lastPrice"]),
            "open": float(ent["open"]),
            "high": float(ent["high"]),
            "low": float(ent["low"]),
            "lastClose": float(ent["lastClose"]),
            "volume": int(ent["volume"]),
            "amount": float(ent["amount"]),
            "time": t,
            "_pushed_at": ent.get("_pushed_at"),
            "stale_age_sec": (round(age, 1) if age is not None else None),
            "_stale": bool(stale),
        }

    def download_history(self, code: str, period: str = "1m") -> bool:
        """tushare 没有「本地缓存」概念，数据随时可拉 ⇒ 恒返回 True。

        保留这个方法是接口对齐：``DailyContext._fetch_daily`` 在拿不到足够
        bar 时会调 ``download_history`` 再重试一次，返回 False 会被记成失败。
        """
        return True

    def get_history(self, code: str, period: str = "1d",
                    count: int = 100) -> Optional[List[dict]]:
        """历史 bar（``ts`` 为 ``datetime``，升序）。失败返回 ``None``（不是 ``[]``）。

        与 ``_XtdClient.get_history`` 的返回契约一致：``None`` 表示「拿不到」，
        ``[]`` 才表示「确实没有」。
        """
        p = str(period or "1d").strip().lower()
        try:
            if p in ("1d", "d", "day", "daily"):
                return self._history_daily(code, int(count))
            if p in ("1m", "5m", "15m", "30m", "60m"):
                return self._history_minute(code, p, int(count))
        except Exception as e:                        # noqa: BLE001
            logger.debug("tushare get_history 失败 %s/%s: %s", code, p, e)
        return None

    # ---- 历史：日线 ----

    @staticmethod
    def _daily_api_order(code: str) -> tuple:
        """按代码推断该先试哪个日线接口，避免每只 ETF 都白打两次空请求。

        tushare 的日线是**分资产**的，互不通用（2026-11-08 实测）：

            pro.daily      股票；ETF 返回 0 行；指数返回 0 行
            pro.fund_daily ETF/LOF（510050.SH 正常返回，vol 单位=手）
            pro.index_daily 指数（000300.SH / 000001.SH 上证指数）

        注意 ``000001.SZ`` 是平安银行（股票），``000001.SH`` 才是上证指数 ——
        只能靠后缀区分，不能只看数字前缀。
        """
        c = str(code or "").upper()
        num = c.split(".")[0]
        if num.startswith(("15", "16", "50", "51", "56", "58")):
            return ("fund_daily", "daily", "index_daily")       # ETF / LOF
        if num.startswith(("399", "899")):
            return ("index_daily", "fund_daily", "daily")       # 深证/北证指数
        if num.startswith("000") and c.endswith(".SH"):
            return ("index_daily", "fund_daily", "daily")       # 上证系列指数
        if num.startswith(("880", "999")):
            return ("index_daily", "fund_daily", "daily")
        return ("daily", "index_daily", "fund_daily")           # 默认按个股

    def _history_daily(self, code: str, count: int) -> Optional[List[dict]]:
        count = max(1, min(int(count or 100), 5000))
        key = f"d:{code}:{count}"
        hit = self._hist_cache.get(key)
        if hit and (time.time() - hit[1]) < self.HISTORY_TTL_SEC:
            return hit[0]

        end = datetime.now()
        # 自然日 → 交易日约 0.7 折算，再留一倍缓冲
        start = end - timedelta(days=max(30, int(count * 2.2)))
        s8, e8 = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")

        rows: List[dict] = []
        for api in self._daily_api_order(code):
            fn = getattr(self._pro, api, None)
            if fn is None:
                continue
            try:
                df = fn(ts_code=code, start_date=s8, end_date=e8)
            except Exception as e:                    # noqa: BLE001
                logger.debug("%s(%s) 失败: %s", api, code, str(e)[:160])
                continue
            rows = _iter_rows(df)
            if rows:
                break

        if not rows:
            return None

        bars: List[dict] = []
        for r in rows:
            td = str(r.get("trade_date") or "")
            if len(td) != 8 or not td.isdigit():
                continue
            try:
                ts = datetime.strptime(td, "%Y%m%d")
            except Exception:
                continue
            o, h, lo, c = _f(r.get("open")), _f(r.get("high")), _f(r.get("low")), _f(r.get("close"))
            if not c or c <= 0:
                continue
            bars.append({
                "ts": ts,
                "open": float(o if o is not None else c),
                "high": float(h if h is not None else c),
                "low": float(lo if lo is not None else c),
                "close": float(c),
                # tushare 的 vol 单位是「手」、amount 是「千元」；xtdata 分别是
                # 「股」与「元」⇒ 必须 ×100 / ×1000 对齐口径。
                # 实测校验：510050.SH 20261106 fund_daily vol=6,231,907 手
                # ⇒ ×100 = 623,190,700 股，与 realtime_quote 的 VOLUME 逐位相同。
                "volume": int((_f(r.get("vol")) or 0.0) * 100),
                "amount": float((_f(r.get("amount")) or 0.0) * 1000.0),
            })

        if not bars:
            return None
        bars.sort(key=lambda b: b["ts"])
        out = bars[-count:]
        self._hist_cache[key] = (out, time.time())
        return out

    # ---- 历史：分钟 ----

    def _history_minute(self, code: str, period: str,
                        count: int) -> Optional[List[dict]]:
        count = max(1, min(int(count or 100), 5000))
        key = f"m:{code}:{period}:{count}"
        hit = self._hist_cache.get(key)
        if hit and (time.time() - hit[1]) < self.HISTORY_TTL_SEC:
            return hit[0]

        freq = {"1m": "1min", "5m": "5min", "15m": "15min",
                "30m": "30min", "60m": "60min"}.get(period, "1min")
        fn = getattr(self._pro, "stk_mins", None)
        if fn is None:
            return None

        # 先按近 7 天取（覆盖周末），不够再放宽到 40 天
        rows: List[dict] = []
        for days in (7, 40):
            end = datetime.now()
            start = end - timedelta(days=days)
            try:
                df = fn(ts_code=code, freq=freq,
                        start_date=start.strftime("%Y-%m-%d %H:%M:%S"),
                        end_date=end.strftime("%Y-%m-%d %H:%M:%S"))
            except Exception as e:                    # noqa: BLE001
                logger.debug("stk_mins(%s,%s) 失败: %s", code, freq, str(e)[:160])
                continue
            rows = _iter_rows(df)
            if len(rows) >= count:
                break

        if not rows:
            return None

        bars: List[dict] = []
        for r in rows:
            tt = r.get("trade_time") or r.get("time")
            ts_ms = _parse_ts_ms(tt)
            if ts_ms is None:
                continue
            try:
                ts = datetime.fromtimestamp(ts_ms / 1000.0)
            except Exception:
                continue
            c = _f(r.get("close"))
            if not c or c <= 0:
                continue
            o, h, lo = _f(r.get("open")), _f(r.get("high")), _f(r.get("low"))
            bars.append({
                "ts": ts,
                "open": float(o if o is not None else c),
                "high": float(h if h is not None else c),
                "low": float(lo if lo is not None else c),
                "close": float(c),
                # stk_mins 的 vol 已是「股」，与 xtdata 同口径，不再乘 100
                "volume": int(_f(r.get("vol")) or 0),
                "amount": float(_f(r.get("amount")) or 0.0),
            })

        if not bars:
            return None
        bars.sort(key=lambda b: b["ts"])
        out = bars[-count:]
        self._hist_cache[key] = (out, time.time())
        return out

    # ---------------------------------------------------------- 诊断

    def health(self) -> dict:
        """自述快照（Web 面板 / 探针用）。绝不抛异常、绝不发起网络请求。"""
        now = time.time()
        with self._lock:
            cached = len(self._cache)
            last = max((float(e.get("_fetched_at") or 0)
                        for e in self._cache.values()), default=0.0)
        return {
            "mode": self.mode,
            "rt_tick_ok": bool(self._rt_tick_ok),
            "rt_tick_used_1min": self._prune(self._tick_calls, now),
            "rt_tick_limit": self.RT_TICK_PER_MIN,
            "backoff_sec": round(max(0.0, self._backoff_until - now), 1),
            "cached_codes": cached,
            "last_fetch_age_sec": (round(now - last, 1) if last else None),
            "min_refresh_sec": self.MIN_REFRESH_SEC,
        }

    def dump(self) -> dict:
        """完整内部状态（诊断脚本用）。"""
        out = self.health()
        out["cache"] = {c: dict(e) for c, e in list(self._cache.items())[:5]}
        out["miss"] = list(self._miss_at.keys())[:10]
        return out

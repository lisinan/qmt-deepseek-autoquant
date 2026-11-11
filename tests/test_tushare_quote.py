# -*- coding: utf-8 -*-
"""TushareQuoteClient 回归测试（2026-11-08）。

**全部离线**：用 ``FakePro`` 冒充 tushare 的 pro 对象、用假函数冒充
``ts.realtime_quote``，一个网络调用都不打。

钉死的契约（每条都对应一个线上会出事的具体后果）：

1. **字段映射**：``price/open_price/high_price/low_price/pre_price`` →
   ``lastPrice/open/high/low/lastClose``。映射错了等于拿错价下单。
2. **``time`` 必须是毫秒 epoch**：引擎 ``_tick_ts`` 除以 1000，若给秒级时间戳
   会被解析到 1970 年，``stale_age_sec`` 变天文数字 ⇒ 全天不敢交易。
3. **沪市 ETF 缺失必须回落到新浪**：``rt_tick`` 实测不含 510050.SH，
   而它正是 OWNER 的长期豁免持仓，漏了等于不盯市。
4. **限频不可突破**：``rt_tick`` 10 次/分钟。引擎 3s 一轮若每轮都打就是
   20 次/分钟，必然被服务端拒；本客户端靠节流 + 滑动窗口自我限制。
5. **绝不返回空**：R9 事故（2026-09-25 全天用 09-24 冻结价）正是「为了干净
   而丢弃陈旧报价」导致 ``raw`` 变空 → 整轮不决策。只打 ``_stale`` 标记。
6. **绝不标 ``_mock``**：``ALLOW_MOCK_TRADING=False`` 下该标记会静默禁用
   整条卖出链（含 −18% 硬止损）。真实数据被误标 = 真实持仓无法离场。
"""
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ★ 必须在 import core.qmt_client 之前锁死选源：该模块有自己的**模块级单例**
#   ``qmt_client = QMTClient()``，构造时会对真实行情源探活一次。若放成 auto，
#   单测 import 就会打真实 tushare 网络（还要等满超时）。这里强制只试 xtdata，
#   让它像此前一样干脆地落到 mock。
os.environ.setdefault("QMT_QUOTE_SOURCE", "xtdata")

from core.tushare_quote import TushareQuoteClient, _parse_ts_ms  # noqa: E402


# ============================================================ 夹具

def _fresh_str(offset_sec: float = 0.0) -> str:
    return (datetime.now() + timedelta(seconds=offset_sec)).strftime("%Y-%m-%d %H:%M:%S")


def _tick_row(code: str, price: float, pre: float, *, trade_time=None,
              open_=None, high=None, low=None, vol=1_000_000, amount=1e8):
    return {
        "ts_code": code,
        "price": price,
        "pre_price": pre,
        "open_price": open_ if open_ is not None else pre,
        "high_price": high if high is not None else price,
        "low_price": low if low is not None else price,
        "volume": vol,
        "amount": amount,
        "trade_time": _fresh_str() if trade_time is None else trade_time,
    }


class FakePro:
    """冒充 tushare pro_api。只实现本客户端会调的三个接口。"""

    def __init__(self, tick_rows=None, daily_rows=None, index_rows=None,
                 fund_rows=None, mins_rows=None, tick_error=None):
        self.tick_rows = list(tick_rows or [])
        self.daily_rows = list(daily_rows or [])
        self.index_rows = list(index_rows or [])
        self.fund_rows = list(fund_rows or [])
        self.mins_rows = list(mins_rows or [])
        self.tick_error = tick_error
        self.tick_calls = 0
        self.quote_calls = 0          # 由 realtime_quote_fn 计数
        self.daily_calls = 0
        self.index_calls = 0
        self.fund_calls = 0
        self.mins_calls = 0
        self.tick_args = []

    def rt_tick(self, ts_code):
        self.tick_calls += 1
        self.tick_args.append(ts_code)
        if self.tick_error:
            raise Exception(self.tick_error)
        want = set(str(ts_code).split(","))
        rows = [r for r in self.tick_rows if r["ts_code"] in want]
        return rows or None

    def daily(self, ts_code=None, start_date=None, end_date=None):
        self.daily_calls += 1
        return self.daily_rows or None

    def index_daily(self, ts_code=None, start_date=None, end_date=None):
        self.index_calls += 1
        return self.index_rows or None

    def fund_daily(self, ts_code=None, start_date=None, end_date=None):
        self.fund_calls += 1
        return self.fund_rows or None

    def stk_mins(self, ts_code=None, freq=None, start_date=None, end_date=None):
        self.mins_calls += 1
        return self.mins_rows or None


def _make_quote_fn(pro: FakePro, rows_by_code: dict):
    """构造假的 ts.realtime_quote：入参是 code 列表，返回大写字段的行。"""
    def fn(codes):
        pro.quote_calls += 1
        out = []
        for c in codes:
            r = rows_by_code.get(c)
            if r:
                out.append(r)
        return out
    return fn


def _client(pro: FakePro, quote_rows=None) -> TushareQuoteClient:
    return TushareQuoteClient(pro=pro,
                              realtime_quote_fn=_make_quote_fn(pro, quote_rows or {}))


# ============================================================ 1. 字段映射

def test_tick_field_mapping():
    """rt_tick 的当日累计字段必须一一映射到引擎的 Tick 契约。"""
    pro = FakePro(tick_rows=[_tick_row(
        "300308.SZ", price=448.0, pre=493.33,
        open_=433.5, high=463.33, low=427.34, vol=54_110_132, amount=2.397e10)])
    c = _client(pro)

    out = c.get_ticks(["300308.SZ"])
    assert "300308.SZ" in out, "必须返回请求的代码"
    t = out["300308.SZ"]

    assert t["lastPrice"] == 448.0
    assert t["open"] == 433.5, f"open 应取 open_price，实际 {t['open']}"
    assert t["high"] == 463.33
    assert t["low"] == 427.34
    assert t["lastClose"] == 493.33, f"lastClose 应取 pre_price，实际 {t['lastClose']}"
    assert t["volume"] == 54_110_132
    assert abs(t["amount"] - 2.397e10) < 1.0


def test_time_is_epoch_milliseconds():
    """``time`` 必须是毫秒 epoch —— 引擎 ``_tick_ts`` 会除以 1000。"""
    ts_str = "2026-11-06 14:30:00"
    pro = FakePro(tick_rows=[_tick_row("300308.SZ", 448.0, 493.33, trade_time=ts_str)])
    c = _client(pro)

    t = c.get_ticks(["300308.SZ"])["300308.SZ"]
    want_ms = int(datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
    assert t["time"] == want_ms, f"time 应为毫秒 epoch，期望 {want_ms} 实际 {t['time']}"
    # 值域自检：毫秒时间戳在 1e12 以上，秒级在 1e9 附近
    assert t["time"] > 1_000_000_000_000, "毫秒时间戳必须 > 1e12（秒级会解析到 1970）"


def test_parse_ts_ms_handles_three_formats():
    """三种上游格式都要能解析：rt_tick / realtime_quote / 纯日期。"""
    a = _parse_ts_ms("2026-11-06 14:30:00")
    b = _parse_ts_ms("20261106", "14:30:00")
    d = _parse_ts_ms("20261106")
    assert a == b, "两种时间格式应解析成同一毫秒值"
    assert d is not None and d < a
    assert _parse_ts_ms("") is None
    assert _parse_ts_ms(None) is None
    assert _parse_ts_ms("垃圾") is None


# ============================================================ 2. ETF 回落

def test_etf_missing_falls_back_to_sina():
    """沪市 ETF 不在 rt_tick 覆盖内（实测 510050 缺失）⇒ 必须走新浪补齐。"""
    pro = FakePro(tick_rows=[_tick_row("300308.SZ", 448.0, 493.33)])
    quote_rows = {
        "510050.SH": {
            "TS_CODE": "510050.SH", "NAME": "上证50ETF华夏",
            "DATE": "20261106", "TIME": "15:00:00",
            "PRICE": 2.902, "OPEN": 2.891, "HIGH": 2.906, "LOW": 2.886,
            "PRE_CLOSE": 2.930, "VOLUME": 623_190_700, "AMOUNT": 1.81e9,
        }
    }
    c = _client(pro, quote_rows)

    out = c.get_ticks(["300308.SZ", "510050.SH"])
    assert "300308.SZ" in out, "rt_tick 覆盖的标的应正常返回"
    assert "510050.SH" in out, "510050 必须由新浪兜底补上（它就是 rt_tick 的缺口）"
    etf = out["510050.SH"]
    assert etf["lastPrice"] == 2.902
    assert etf["open"] == 2.891
    assert etf["high"] == 2.906
    assert etf["low"] == 2.886
    assert etf["lastClose"] == 2.930
    assert etf["volume"] == 623_190_700
    # ★ 这条是本次写测试抓出来的真缺陷：``DATE + TIME`` 拼成
    #   ``"20261106 15:00:00"`` 后按 ``s[:14]`` 切会解析失败 → ``time=None``
    #   → 该条被判「无行情时间 ⇒ 陈旧」→ **ETF 全天无法交易**（含 510050）。
    assert etf["time"] is not None, "DATE+TIME 必须能解析成毫秒 epoch"
    assert etf["time"] > 1_000_000_000_000
    assert etf["stale_age_sec"] is not None


# ============================================================ 3. 限频

def test_min_refresh_serves_cache():
    """8s 节流内二次调用读缓存，不打第二次 API。"""
    pro = FakePro(tick_rows=[_tick_row("300308.SZ", 448.0, 493.33)])
    c = _client(pro)
    assert c.MIN_REFRESH_SEC >= 6.0, "节流窗口必须留足余量（60/8=7.5 < 10）"

    c.get_ticks(["300308.SZ"])
    first = pro.tick_calls
    c.get_ticks(["300308.SZ"])
    c.get_ticks(["300308.SZ"])
    assert pro.tick_calls == first, (
        f"节流期内不应再打 API：首次 {first} 次，三次调用后 {pro.tick_calls} 次")
    assert first == 1


def test_rt_tick_rate_limited_and_fallback_takes_over():
    """rt_tick 额度极小 ⇒ 必须自我限制在低频；静默期由 realtime_quote 无条件接管。

    【2026-11-11 改写】原用例断言「配额耗尽时不应改用新浪顶全量标的」，
    那条正是事故根因：撞限后只给「无缓存」标的补价，38 只全有缓存 ⇒ 全冻。
    实测原文：rt_tick 限「100 次/小时」+「200 次/天」，realtime_quote
    连续 150 次 / 324 秒零失败。所以正确契约是「rt_tick 低频 + 兜底无条件接管」。
    """
    pro = FakePro(tick_rows=[_tick_row("300308.SZ", 448.0, 493.33)])
    c = _client(pro)
    c.MIN_REFRESH_SEC = 0.0      # 人工取消节流，逼出滑动窗口限频
    c.MISS_RETRY_SEC = 0.0

    for _ in range(25):
        c.get_ticks(["300308.SZ"])

    assert pro.tick_calls == 1, (
        f"180s 内只允许 1 次 rt_tick（实际 {pro.tick_calls}）——"
        f"按分钟计数去防小时额度就是 11-11 事故的成因")
    assert pro.quote_calls >= 20, (
        f"rt_tick 静默期间必须由 realtime_quote 接管（实际 {pro.quote_calls} 次），"
        f"否则行情冻结")
    assert c._prune(c._tick_calls, time.time(), 3600.0) <= c.RT_TICK_PER_HOUR - 5


def test_rate_limit_message_triggers_backoff():
    """服务端回『频率超限』后必须静默，不能每轮重撞。"""
    pro = FakePro(tick_error="抱歉，您访问接口(rt_tick)频率超限(10次/分钟)")
    c = _client(pro)

    c.get_ticks(["300308.SZ"])
    assert c._backoff_until > time.time(), "撞限频后必须进入静默期"
    assert c.health()["backoff_sec"] > 0

    before = pro.tick_calls
    c.MIN_REFRESH_SEC = 0.0
    for _ in range(5):
        c.get_ticks(["300308.SZ"])
    assert pro.tick_calls == before, "静默期内不得再打 rt_tick"


# ============================================================ 4. 绝不返回空

def test_never_returns_empty_after_source_fails():
    """数据源失效时仍返回缓存（打标记），而不是空字典。

    R9 事故：丢弃陈旧报价 ⇒ ``raw`` 为空 ⇒ ``_run_once`` 直接 return ⇒
    整轮不决策，比用旧价更糟。
    """
    pro = FakePro(tick_rows=[_tick_row("300308.SZ", 448.0, 493.33)])
    c = _client(pro)
    c.MIN_REFRESH_SEC = 0.0
    c.MISS_RETRY_SEC = 0.0

    assert "300308.SZ" in c.get_ticks(["300308.SZ"])

    pro.tick_error = "network unreachable"
    out = c.get_ticks(["300308.SZ"])
    assert "300308.SZ" in out, "源失效后必须返回缓存，不能返回空"
    assert out["300308.SZ"]["lastPrice"] == 448.0


def test_stale_flag_from_trade_time():
    """陈旧报价只打 ``_stale`` 标记，不丢数据。"""
    old = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    pro = FakePro(tick_rows=[_tick_row("300308.SZ", 448.0, 493.33, trade_time=old)])
    c = _client(pro)

    t = c.get_ticks(["300308.SZ"])["300308.SZ"]
    assert t["_stale"] is True, f"2 小时前的报价应判陈旧（阈值 {c.SNAPSHOT_STALE_SEC}s）"
    assert t["stale_age_sec"] is not None and t["stale_age_sec"] > 3600


def test_fresh_tick_not_marked_stale():
    """正常盘中快照不能误标陈旧（否则全天不敢建仓）。"""
    pro = FakePro(tick_rows=[_tick_row("300308.SZ", 448.0, 493.33)])
    c = _client(pro)
    t = c.get_ticks(["300308.SZ"])["300308.SZ"]
    assert t["_stale"] is False
    assert t["stale_age_sec"] is not None and t["stale_age_sec"] < 5


def test_missing_trade_time_is_conservatively_stale():
    """上游缺 ``trade_time`` 时必须判陈旧，不能当新鲜。

    ``_XtdClient._tick_age_sec`` 的 docstring 写明「None 含义是无法判定而非
    新鲜」，但它自己的 ``_stale`` 把 None 判成不陈旧 —— 本客户端按 docstring
    的语义执行：拿不到行情时间就不许据此建仓。
    """
    row = _tick_row("300308.SZ", 448.0, 493.33)
    row["trade_time"] = None
    pro = FakePro(tick_rows=[row])
    c = _client(pro)

    t = c.get_ticks(["300308.SZ"])["300308.SZ"]
    assert t["time"] is None
    assert t["_stale"] is True, "无行情时间必须保守判陈旧"
    assert t["stale_age_sec"] is None


def test_never_marks_mock():
    """真实数据绝不能被标 ``_mock`` —— 该标记会禁用整条卖出链。"""
    pro = FakePro(tick_rows=[_tick_row("300308.SZ", 448.0, 493.33)])
    c = _client(pro)
    t = c.get_ticks(["300308.SZ"])["300308.SZ"]
    assert "_mock" not in t, (
        "tushare 是真实行情，标 _mock 会让 ALLOW_MOCK_TRADING=False 下的"
        "卖出链（含 −18% 硬止损）整段静默禁用")
    assert c.mode == "tushare"


def test_invalid_price_row_dropped():
    """``price<=0``（停牌/脏数据）直接丢弃，不产出 0 元报价。"""
    rows = [_tick_row("300308.SZ", 0.0, 493.33), _tick_row("600519.SH", 1453.0, 1452.0)]
    pro = FakePro(tick_rows=rows)
    c = _client(pro)
    out = c.get_ticks(["300308.SZ", "600519.SH"])
    assert "300308.SZ" not in out, "price=0 必须丢弃"
    assert "600519.SH" in out


# ============================================================ 5. 权限降级

def test_rt_tick_permission_error_disables_tick_path():
    """rt_tick 无权限 ⇒ 整源回落新浪，而不是整源判死。

    2026-11-08 实测：``rt_k`` 无权限但 ``rt_tick`` 可用；反向也要能退化 ——
    哪怕 rt_tick 也不可用，新浪仍能提供当日 OHLC，交易不该因此瘫痪。
    """
    pro = FakePro(tick_error="抱歉，您没有接口(rt_tick)访问权限")
    quote_rows = {
        "300308.SZ": {
            "TS_CODE": "300308.SZ", "DATE": "20261106", "TIME": "15:00:00",
            "PRICE": 448.0, "OPEN": 433.5, "HIGH": 463.33, "LOW": 427.34,
            "PRE_CLOSE": 493.33, "VOLUME": 54_110_132, "AMOUNT": 2.397e10,
        }
    }
    c = _client(pro, quote_rows)

    out = c.get_ticks(["300308.SZ"])
    assert c._rt_tick_ok is False, "无权限后应关闭 rt_tick 分支"
    assert "300308.SZ" in out, "应改由新浪提供数据"
    assert out["300308.SZ"]["lastPrice"] == 448.0
    assert pro.quote_calls >= 1


def test_probe_raises_when_all_sources_dead():
    """探活全失败必须抛错，让 ``QMTClient`` 诚实地落到 mock。"""
    pro = FakePro(tick_error="boom")
    c = _client(pro, quote_rows={})     # 新浪也返回空
    try:
        c._probe()
    except RuntimeError:
        return
    raise AssertionError("两源皆无数据时 _probe 必须抛 RuntimeError")


def test_probe_ok_with_rt_tick():
    """rt_tick 有数据时探活通过且不占用引擎配额。"""
    pro = FakePro(tick_rows=[_tick_row("000001.SZ", 11.81, 11.64)])
    c = _client(pro)
    c._probe()
    assert c._rt_tick_ok is True
    assert c._tick_calls == [], "探活不应计入引擎的滑动窗口配额"


# ============================================================ 6. 历史 bar

def _daily_rows(n: int, vol_lots: float = 1000.0):
    """tushare 的 daily 是**倒序**（最新在前），这里刻意照原样给。"""
    base = datetime(2026, 11, 6)
    rows = []
    for i in range(n):
        d = base - timedelta(days=i)
        rows.append({
            "ts_code": "300308.SZ", "trade_date": d.strftime("%Y%m%d"),
            "open": 400.0 + i, "high": 420.0 + i, "low": 390.0 + i, "close": 410.0 + i,
            "vol": vol_lots, "amount": 1000.0,
        })
    return rows


def test_history_daily_ascending_and_count():
    """日线必须转成升序并截取 count 根（策略的 MA/动量全靠顺序）。"""
    pro = FakePro(daily_rows=_daily_rows(30))
    c = _client(pro)

    bars = c.get_history("300308.SZ", "1d", 10)
    assert bars is not None and len(bars) == 10
    ts = [b["ts"] for b in bars]
    assert ts == sorted(ts), "日线必须按时间升序"
    assert ts[-1] == datetime(2026, 11, 6), "末根应是最新交易日"
    for b in bars:
        assert set(b) == {"ts", "open", "high", "low", "close", "volume", "amount"}


def test_history_daily_volume_unit_converted():
    """tushare 的 vol 单位是「手」，必须 ×100 对齐 xtdata 的「股」。

    不换算会让放量/缩量类因子整体差 100 倍。
    """
    pro = FakePro(daily_rows=_daily_rows(5, vol_lots=1234.0))
    c = _client(pro)
    bars = c.get_history("300308.SZ", "1d", 5)
    assert bars[0]["volume"] == 123_400, f"手→股 换算错误：{bars[0]['volume']}"


def test_history_daily_index_fallback():
    """指数不在 ``daily`` 里 ⇒ 自动改用 ``index_daily``。

    注意 ``000001.SZ`` 是平安银行（个股）而 ``000001.SH`` 才是上证指数 ——
    只能靠后缀区分，本用例顺带钉死这条。
    """
    pro = FakePro(daily_rows=[], index_rows=_daily_rows(20))
    c = _client(pro)
    bars = c.get_history("000300.SH", "1d", 20)
    assert bars is not None and len(bars) == 20
    assert pro.index_calls >= 1, "指数必须走 index_daily"
    assert pro.daily_calls == 0, "指数不应先白打一次 daily（按后缀已定向）"


def test_history_daily_etf_uses_fund_daily():
    """ETF 日线在 ``fund_daily`` 里（``daily`` 对 ETF 返回 0 行）。

    2026-11-08 实测：``daily(510050.SH)`` = 0 行，``fund_daily(510050.SH)`` 正常。
    漏了这条会让 510050 的"日线末 3 根"是空的 —— 探针第一次跑就是这么暴露的。
    """
    pro = FakePro(daily_rows=[], fund_rows=_daily_rows(20, vol_lots=6_231_907.0))
    c = _client(pro)
    bars = c.get_history("510050.SH", "1d", 20)
    assert bars is not None and len(bars) == 20
    assert pro.fund_calls >= 1, "ETF 必须走 fund_daily"
    assert pro.daily_calls == 0, "ETF 不应先白打一次 daily（按前缀已定向）"
    # 实测校验：6,231,907 手 × 100 = 623,190,700 股，与新浪 VOLUME 逐位相同
    assert bars[-1]["volume"] == 623_190_700


def test_history_daily_order_heuristic():
    """``_daily_api_order`` 的资产分类必须正确（错一个就会白打空请求）。"""
    order = TushareQuoteClient._daily_api_order
    assert order("510050.SH")[0] == "fund_daily"
    assert order("159915.SZ")[0] == "fund_daily"
    assert order("000300.SH")[0] == "index_daily"
    assert order("399006.SZ")[0] == "index_daily"
    assert order("300308.SZ")[0] == "daily"
    assert order("600519.SH")[0] == "daily"
    # 000001 只能靠后缀区分：.SZ 平安银行（个股） / .SH 上证指数
    assert order("000001.SZ")[0] == "daily", "000001.SZ 是股票，不是指数"
    assert order("000001.SH")[0] == "index_daily", "000001.SH 才是上证指数"


def test_history_daily_returns_none_when_empty():
    """拿不到必须返回 ``None``（不是 ``[]``）—— 调用方靠它区分「失败」与「确实没有」。"""
    pro = FakePro(daily_rows=[])
    c = _client(pro)
    assert c.get_history("999999.SZ", "1d", 10) is None


def test_history_minute_ascending():
    """分钟线同样升序；stk_mins 的 vol 已是「股」，不再 ×100。"""
    now = datetime.now()
    rows = [{
        "ts_code": "300308.SZ",
        "trade_time": (now - timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S"),
        "open": 448.0, "high": 449.0, "low": 447.0, "close": 448.5,
        "vol": 52_072.8, "amount": 2.33e8,
    } for i in range(120)]
    pro = FakePro(mins_rows=rows)
    c = _client(pro)

    bars = c.get_history("300308.SZ", "1m", 60)
    assert bars is not None and len(bars) == 60
    ts = [b["ts"] for b in bars]
    assert ts == sorted(ts), "分钟线必须升序"
    assert bars[-1]["volume"] == 52_072, "stk_mins 的 vol 已是股，不能再乘 100"


def test_history_cached_within_ttl():
    """历史 bar 有 TTL 缓存：引擎每轮对 38 只各打一次会白烧额度。"""
    pro = FakePro(daily_rows=_daily_rows(30))
    c = _client(pro)
    c.get_history("300308.SZ", "1d", 10)
    n = pro.daily_calls
    c.get_history("300308.SZ", "1d", 10)
    assert pro.daily_calls == n, "TTL 内重复请求不应再打 API"


# ============================================================ 7. 接口面

def test_subscribe_and_download_history():
    pro = FakePro(tick_rows=[])
    c = _client(pro)
    c.subscribe(["300308.SZ", "510050.SH"])
    assert c.subscribed == {"300308.SZ", "510050.SH"}
    assert c.health()["mode"] == "tushare"
    # tushare 无本地缓存概念，恒 True（DailyContext 拿不到 bar 时会调它再重试）
    assert c.download_history("300308.SZ", "1d") is True


def test_empty_codes_returns_empty_without_network():
    pro = FakePro(tick_rows=[])
    c = _client(pro)
    assert c.get_ticks([]) == {}
    assert pro.tick_calls == 0


def test_unknown_period_returns_none():
    pro = FakePro()
    c = _client(pro)
    assert c.get_history("300308.SZ", "3d", 10) is None


def test_preferred_sources_env_switch():
    """``QMT_QUOTE_SOURCE`` 必须能强制选源（诊断/回归用）。"""
    from core.qmt_client import QMTClient
    old = os.environ.get("QMT_QUOTE_SOURCE")
    try:
        os.environ["QMT_QUOTE_SOURCE"] = "tushare"
        assert QMTClient._preferred_sources() == ["tushare"]
        os.environ["QMT_QUOTE_SOURCE"] = "xtdata"
        assert QMTClient._preferred_sources() == ["xtdata"]
        os.environ["QMT_QUOTE_SOURCE"] = "auto"
        assert QMTClient._preferred_sources() == ["xtdata", "tushare"], (
            "auto 必须在 xtdata 之后插入 tushare，而不是直接落 mock")
        os.environ.pop("QMT_QUOTE_SOURCE", None)
        assert QMTClient._preferred_sources() == ["xtdata", "tushare"]
    finally:
        if old is None:
            os.environ.pop("QMT_QUOTE_SOURCE", None)
        else:
            os.environ["QMT_QUOTE_SOURCE"] = old


def test_tushare_mode_passes_mock_gate():
    """``data_mode="tushare"`` 必须**不被** mock 闸门拦截。

    闸门只认 ``"mock"`` 这一个值（``mock_trade_block_reason``）。若换源后被
    误拦，真实持仓的 −18% 硬止损就永远发不出去。
    """
    # ``engine.event_engine`` 会拉 storage ⇒ 必须先指向临时 DB/日志，
    # 否则单跑本文件会把测试桩写进生产库（见 run_all 顶部纪律）。
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="qmt_tsg_"))
    os.environ.setdefault("QMT_DB", str(tmp / "t.db"))
    os.environ.setdefault("QMT_NOTICES_LOG", str(tmp / "t.log"))

    from types import SimpleNamespace
    from engine.event_engine import mock_trade_block_reason

    eng = SimpleNamespace(data_mode="tushare", allow_mock_trading=False)
    assert mock_trade_block_reason(eng) is None, "tushare 是真实行情，不得拦截成交"

    eng_mock = SimpleNamespace(data_mode="mock", allow_mock_trading=False)
    assert mock_trade_block_reason(eng_mock) is not None, "mock 必须仍被拦截（正向对照）"


# ============================================================ main

if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  PASS {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)


# ============================================================ 3b. 11-11 事故回归
def test_hourly_cap_does_not_freeze_cached_codes():
    """rt_tick 撞限后，**已有缓存**的标也必须由 realtime_quote 拿到新价。

    2026-11-11 真实事故：13:00:25 开市，rt_tick 限额 100 次/小时被 8s 节流
    （450 次/小时）打穿 ⇒ `_backoff_until=+60s` ⇒ 旧实现只给「无缓存」标的
    补新浪 ⇒ 38 只全有缓存 ⇒ 价格全部冻结到 13:15 那一刻，13:17 引擎报
    「行情源失效：38 只快照陈旧 2 分钟，已暂停建仓」。此后建仓停、止损判不了。

    本用例钉住：撞限 ≠ 行情停止。
    """
    pro = FakePro(tick_rows=[_tick_row("300308.SZ", 448.0, 493.33)])
    quote_rows = {
        "300308.SZ": {
            "TS_CODE": "300308.SZ", "DATE": "20261111", "TIME": "13:20:00",
            "PRICE": 449.5, "OPEN": 452.0, "HIGH": 455.0, "LOW": 440.0,
            "PRE_CLOSE": 448.0, "VOLUME": 9_000, "AMOUNT": 4.0e6,
        }
    }
    c = _client(pro, quote_rows)

    # 第一轮：rt_tick 正常，价格 448
    first = c.get_ticks(["300308.SZ"])
    assert float(first["300308.SZ"]["lastPrice"]) == 448.0
    assert pro.quote_calls == 0, "rt_tick 有额度时不该走兜底"

    # 第二轮：服务端开始报「频率超限(100次/小时)」，且缓存已过 8s 节流
    pro.tick_error = "抱歉，您访问接口(rt_tick)频率超限(100次/小时)"
    c._last_tick_ts = 0.0            # 绕开 180s 间隔，模拟额度耗尽后再次尝试
    c._cache["300308.SZ"]["_fetched_at"] -= c.MIN_REFRESH_SEC + 1
    c._last_refresh -= c.MIN_REFRESH_SEC + 1

    second = c.get_ticks(["300308.SZ"])
    assert c._backoff_until > time.time(), "撞限后应进入静默期"
    assert pro.quote_calls >= 1, "撞限后必须由 realtime_quote 接管"
    assert float(second["300308.SZ"]["lastPrice"]) == 449.5, (
        "已有缓存的标的也必须拿到新价，否则就是 11-11 的行情冻结")

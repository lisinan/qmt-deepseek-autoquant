# -*- coding: utf-8 -*-
"""陈旧报价「盯市冻结」守卫回归测试（2026-10-07 AM-EVOLVE 第 20 轮 · P0 缺陷修复）。

背景（实盘铁证，量测通道可信性问题）：
  2026-10-07（国庆休市，A 股 10-01~10-07 无行情）09:15 引擎重启后，同一组
  5 只持仓（**零成交**）的 market_value 由 09-30 收盘的 491,347.0 变为
  **536,520.0**，凭空 +45,173.0 元（+9.2%），paper 总资产
  975,273.6 → **1,020,446.6**。逐票核对：

    300308.SZ  真实 09-30 收盘 808.44 → 引擎盯市 **900.00**（+11.3%）
    688008.SH  真实 09-30 收盘 202.31 → 引擎盯市 **232.50**（+14.9%）
    002415.SZ  真实 09-30 收盘  32.46 → 引擎盯市 **35.71**（+10.0%）
    000977.SZ  真实 09-30 收盘  65.93 → 引擎盯市 **69.97**（+6.1%）
    300394.SZ  真实 09-30 收盘 260.77 → 引擎盯市 **260.80**（+0.0%）

  ``xtdata.get_full_tick`` 直读确认这些报价的 timetag 是 **2026-10-06 11:30**，
  一个**根本没有交易**的休市日，而其 ``open`` 字段反而与真实 09-30 收盘一致
  （300308 open=808.44）⇒ 报价是 miniQMT 本地缓存的**伪快照**，且被系统性抬高。

后果（为什么必须修，而不是「不交易就没事」）：
  ① KPI#4 符号翻转：复位（2026-09-21，100 万）至今真实累计 **−2.47%**，
     虚高后读作 **+2.04%** ⇒ 考核项「近 4 周滚动收益 > 0%」被假数据判为达标。
  ② 风控基线被抬高：peak_equity / day_open_asset 双双取到 1,020,446.6。
  ③ 09-26 R9 的陈旧守卫**故意**写了「退出/盯市不受影响，按可得的最好价格计」，
     隐含假设是「陈旧价 = 冻结的旧价，最坏只是不动」。本轮证明该假设不成立：
     陈旧价可以是**虚高的脏数据**，用它盯市等于自己给自己发钱。

本文件锁死修复语义：行情新鲜时逐位不变（零回归），行情陈旧时才冻结盯市/峰值。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.data_models import Position, Tick
from datetime import datetime
from engine.event_engine import EventEngine


def _pos(code: str = "300308.SZ", last: float = 808.44, peak: float = 808.44,
         avg: float = 800.0, stop: float = 0.0) -> Position:
    return Position(code=code, name="X", quantity=100, avg_cost=avg,
                    last_price=last, open_date=datetime.now(),
                    peak_price=peak, stop_price=stop, target_price=0.0)


def _tick(price: float, code: str = "300308.SZ") -> Tick:
    return Tick(ts=datetime.now(), code=code, price=price, open=price,
                high=price, low=price, pre_close=price, source="xtdata")


def _bare_engine(stale_codes=(), positions=None, freeze: bool = True) -> EventEngine:
    """不触发 __init__（会连 xtdata / 起线程）的最小引擎对象。"""
    eng = EventEngine.__new__(EventEngine)
    eng._market_stale = False
    eng._market_stale_codes = list(stale_codes)
    eng._market_stale_code_set = set(stale_codes)
    eng._market_stale_max_age = 0.0
    eng._last_market_stale_warn_ts = 0.0
    eng.daily = None
    eng._positions = positions if positions is not None else {}
    # 用类属性副本模拟「可逆开关置 False」的回归路径
    eng.MARK_STALE_FREEZE_VALUATION = freeze
    return eng


# ---------------------------------------------------------------- 核心语义

def test_stale_quote_freezes_mark_price():
    """★ 缺陷本体：陈旧（虚高）报价**不得**改写盯市价。"""
    p = _pos(last=808.44)
    eng = _bare_engine(stale_codes={"300308.SZ"}, positions={"300308.SZ": p})
    eng._update_position_marks({"300308.SZ": _tick(900.00)})   # +11.3% 伪快照
    assert p.last_price == 808.44, (
        f"陈旧报价不得改写盯市价：期望保持 808.44，实际 {p.last_price}")


def test_stale_quote_freezes_peak_price():
    """★ 峰值必须一起冻结——峰值是移动止损与回撤基线的锚，抬高它等于放松风控。"""
    p = _pos(last=808.44, peak=808.44)
    eng = _bare_engine(stale_codes={"300308.SZ"}, positions={"300308.SZ": p})
    eng._update_position_marks({"300308.SZ": _tick(900.00)})
    assert p.peak_price == 808.44, (
        f"陈旧报价不得抬高峰值价：期望 808.44，实际 {p.peak_price}")


def test_fresh_quote_updates_mark_and_peak():
    """★ 零回归核心：行情新鲜时行为逐位不变（盯市价与峰值照常更新）。"""
    p = _pos(last=808.44, peak=808.44)
    eng = _bare_engine(stale_codes=set(), positions={"300308.SZ": p})
    eng._update_position_marks({"300308.SZ": _tick(820.00)})
    assert p.last_price == 820.00, f"新鲜报价必须更新盯市价，实际 {p.last_price}"
    assert p.peak_price == 820.00, f"新鲜报价必须抬高峰值，实际 {p.peak_price}"


def test_freeze_does_not_lower_peak_on_real_decline():
    """冻结期间若真有新低价也不得**下调**峰值（只冻结，不制造新的风控动作）。"""
    p = _pos(last=808.44, peak=900.00)
    eng = _bare_engine(stale_codes={"300308.SZ"}, positions={"300308.SZ": p})
    eng._update_position_marks({"300308.SZ": _tick(700.00)})
    assert p.peak_price == 900.00, "冻结只跳过更新，不得反向改写峰值"


def test_stale_quote_still_seeds_zero_price():
    """首次建仓（last_price<=0）必须取价，否则市值变 0 是更坏的后果。"""
    p = _pos(last=0.0, peak=0.0)
    eng = _bare_engine(stale_codes={"300308.SZ"}, positions={"300308.SZ": p})
    eng._update_position_marks({"300308.SZ": _tick(900.00)})
    assert p.last_price == 900.00, "尚无盯市价时必须取价，避免市值归零"


def test_freeze_does_not_block_stop_price_init():
    """止损价初始化**不受**冻结影响（它只用 avg_cost / ATR，不用当前价）。"""
    p = _pos(last=808.44, stop=0.0, avg=800.0)
    eng = _bare_engine(stale_codes={"300308.SZ"}, positions={"300308.SZ": p})
    eng._update_position_marks({"300308.SZ": _tick(900.00)})
    assert p.stop_price > 0, (
        "冻结盯市不得连带冻结止损价初始化——否则新仓将无止损保护")


def test_switch_off_restores_r9_behavior():
    """★ 可逆性：开关置 False 即逐位恢复 R9 旧行为（盯市照常更新）。"""
    p = _pos(last=808.44, peak=808.44)
    eng = _bare_engine(stale_codes={"300308.SZ"}, positions={"300308.SZ": p},
                       freeze=False)
    eng._update_position_marks({"300308.SZ": _tick(900.00)})
    assert p.last_price == 900.00 and p.peak_price == 900.00, (
        "开关置 False 必须完全恢复旧行为")


def test_non_stale_code_in_set_is_unaffected():
    """只有**被判陈旧**的标的被冻结；同轮其它标的照常更新（粒度是标的级）。"""
    pa = _pos("AAA", last=10.0, peak=10.0)
    pb = _pos("BBB", last=20.0, peak=20.0)
    eng = _bare_engine(stale_codes={"AAA"}, positions={"AAA": pa, "BBB": pb})
    eng._update_position_marks({"AAA": _tick(99.0), "BBB": _tick(21.0)})
    assert pa.last_price == 10.0, "陈旧标的必须冻结"
    assert pb.last_price == 21.0, "新鲜标的不得被误冻结"


# ---------------------------------------------------------------- 引擎侧接线

def test_update_market_staleness_populates_code_set():
    """陈旧集合必须由 _update_market_staleness 权威填充（盯市冻结的唯一判据源）。"""
    eng = _bare_engine()
    age = EventEngine.MARKET_STALE_SEC + 600
    eng._update_market_staleness({
        "OLD1": {"stale_age_sec": age, "lastPrice": 10.0},
        "OLD2": {"stale_age_sec": age, "lastPrice": 10.0},
        "FRESH": {"stale_age_sec": 1.0, "lastPrice": 10.0},
    })
    assert eng._market_stale_code_set == {"OLD1", "OLD2"}, (
        f"逐标的陈旧集合错误：{eng._market_stale_code_set}")


def test_missing_age_code_never_frozen():
    """数据源不给行情时间（无法判定）⇒ 不得冻结（宁可盯市，不可市值归零/卡死）。"""
    eng = _bare_engine()
    eng._update_market_staleness({"UNK": {"lastPrice": 10.0}})
    assert eng._market_stale_code_set == set(), (
        "无法判定新鲜度时不得冻结盯市")


def test_day_open_baseline_not_set_while_stale():
    """★ 行情陈旧时不得建立日内基线，否则「虚增 → 校正」会被误读成日内暴跌。"""
    eng = _bare_engine()
    eng._day_open_asset = None
    eng._market_stale = True
    base = eng._day_open_baseline(1020446.6)      # 伪快照虚增后的权益
    assert eng._day_open_asset is None, "行情陈旧时不得把虚增值写成今日基线"
    assert base == 1020446.6, "基线缺失时退回 total_asset ⇒ 日内盈亏恒为 0"


def test_day_open_baseline_set_when_fresh():
    """★ 零回归：行情新鲜时必须照常建立基线（与修复前逐位一致）。"""
    eng = _bare_engine()
    eng._day_open_asset = None
    eng._market_stale = False
    assert eng._day_open_baseline(975273.6) == 975273.6
    assert eng._day_open_asset == 975273.6
    # 基线一旦建立就不再随日内波动改写
    assert eng._day_open_baseline(960000.0) == 975273.6


def test_switch_default_enabled():
    """默认必须开启：这是 P0 修复，关掉等于退回被伪快照污染的量测。"""
    assert EventEngine.MARK_STALE_FREEZE_VALUATION is True


def test_extracted_method_exists():
    """抽出 _update_position_marks 是本次可测性的前提（守卫覆盖范围本身要审计）。"""
    assert callable(getattr(EventEngine, "_update_position_marks", None))


if __name__ == "__main__":
    for k, v in sorted(globals().items()):
        if k.startswith("test_"):
            v()
            print(f"  ok {k}")
    print("STALE VALUATION GUARD TESTS PASSED")

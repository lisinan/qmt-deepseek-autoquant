# -*- coding: utf-8 -*-
"""miniQMT 交互健康快照守卫（2026-10-29）。

【为什么需要这个面板】
用户的核心担忧是「miniQMT 到底还在不在正常响应」。这个答案原先散落在：
探活日志（且被 60s 节流）、``engine._market_stale*``（只在内存里）、
orders/fills 表、``signals.blocked_by``。
最容易漏掉的失败模式是 **静默降级为 mock**：它不报错、不熔断、不告警，
只是悄悄把全部买卖禁掉（实盘铁证：2026-09-30 上午 5 笔卖出按合成价成交，
凭空实现 −213,354.20 元假亏损）。

【本测试钉住三条纪律（比字段更重要的部分）】
1. **绝不抛异常**：看板自己崩掉比没有更糟 —— 会让「一切正常」与「查询失败」
   长得一模一样。任何缺件都降级成 ``{"error": ...}``。
2. **绝不发阻塞 IO**：不调 xtdata/xttrader（可能卡几十秒甚至永久）。因此
   「引擎没在跑 ⇒ 部分字段为空」是**预期行为**，不是缺陷。
3. **判定顺序**：mock 必须排在行情陈旧之前 —— 否则页面只提示「行情旧」，
   而真正原因是数据源已降级、全部成交被禁。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, ".")

from core import qmt_health                    # noqa: E402
from core.qmt_health import snapshot          # noqa: E402
from storage.db import Storage                # noqa: E402


def _tmp_storage():
    import os
    import tempfile
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Storage(p)


class _FakeEngine:
    """最小可用引擎替身：只提供面板读取的那些属性。

    刻意不构造真 EventEngine —— 看板不该依赖引擎的内部装配，
    否则引擎一改构造顺序，面板就静默取不到值。
    """

    MARKET_STALE_SEC = 120.0

    def __init__(self, data_mode="xtdata", stale=False, max_age=3.0,
                 bars=None, halted=False, scale=1.0):
        self.data_mode = data_mode
        self.exec_mode = "paper"
        self.strategy_mode = "single"
        self.max_positions = 5
        self._cash = 972533.6
        self._market_stale = stale
        self._market_stale_codes = ["300308.SZ"] * (3 if stale else 0)
        self._market_stale_max_age = max_age
        self._last_market_max_age = max_age
        self._daily_trade_count = 0
        self._positions = {}
        self._bars = bars or {}
        self._pending_orders = {"300308.SZ": {}}
        self._pending_sells = {}
        self.risk = SimpleNamespace(halted=halted, halt_reason="测试" if halted else "",
                                    consec_loss=0, position_scale=scale,
                                    _daily_pnl=0.0)


def _bar(ts):
    return SimpleNamespace(ts=ts)


# ------------------------------------------------------------- 不抛异常
def test_snapshot_never_raises_without_engine_or_storage():
    """两个句柄都为 None（web 单独起）也必须出完整结构，不能抛。"""
    d = snapshot(engine=None, storage=None)
    assert set(["ts", "session", "market", "trading", "engine", "status",
                "issues"]) <= set(d), d.keys()
    assert d["status"] in ("ok", "warn", "bad")


def test_snapshot_is_json_serializable():
    """Flask jsonify 会序列化它 —— 出现 datetime / set 就 500。"""
    d = snapshot(engine=_FakeEngine(), storage=_tmp_storage())
    json.dumps(d, ensure_ascii=False)          # 不抛即通过


def test_partial_engine_does_not_break_snapshot():
    """半成品引擎（object.__new__ 风格，属性缺失）不得让面板崩。

    与 signals.blocked_by 那次同一个坑：测试与诊断脚本会用
    `object.__new__` 拼引擎，不走 __init__。
    """
    d = snapshot(engine=SimpleNamespace(), storage=None)
    assert d["status"] in ("ok", "warn", "bad")
    json.dumps(d, ensure_ascii=False)


# ------------------------------------------------------------- 判定顺序
def test_mock_mode_is_bad_and_takes_priority():
    """mock 是最高优先级告警：它会静默禁止全部成交。"""
    d = snapshot(engine=_FakeEngine(data_mode="mock"), storage=None)
    assert d["status"] == "bad", d["issues"]
    tags = [i["tag"] for i in d["issues"]]
    assert "数据源" in tags, f"mock 未产生数据源告警: {tags}"
    assert "禁止" in " ".join(i["msg"] for i in d["issues"]), \
        "告警文案必须点明「会禁止成交」，否则用户不知道后果"


def test_mock_reason_appears_before_stale():
    """mock 与陈旧同时存在时，数据源告警必须排在前面（先看病根）。"""
    d = snapshot(engine=_FakeEngine(data_mode="mock", stale=True), storage=None)
    tags = [i["tag"] for i in d["issues"]]
    assert tags and tags[0] == "数据源", f"判定顺序错误: {tags}"


def test_stale_in_trading_is_bad_but_offhours_is_warn():
    """同一份陈旧状态，交易时段是「必须告警」，非交易时段属正常。"""
    d = snapshot(engine=_FakeEngine(stale=True), storage=None)
    lv = {i["tag"]: i["level"] for i in d["issues"]}
    if d["session"].get("in_trading") is True:
        assert lv.get("行情陈旧") == "bad"
    elif d["session"].get("in_trading") is False:
        assert lv.get("行情陈旧") == "warn"


def test_zero_position_scale_is_bad():
    """position_scale=0（连亏降仓压到 0）会让策略永久无法开仓，必须红色。"""
    d = snapshot(engine=_FakeEngine(scale=0.0), storage=None)
    assert any("仓位系数" in i["msg"] for i in d["issues"]), d["issues"]
    assert d["status"] == "bad"


def test_halted_is_bad():
    d = snapshot(engine=_FakeEngine(halted=True), storage=None)
    assert any(i["tag"] == "风控" for i in d["issues"]), d["issues"]


def test_healthy_engine_reports_ok():
    """健康态不得报 warn/bad（防止「永远红」导致告警疲劳）。"""
    d = snapshot(engine=_FakeEngine(), storage=None)
    assert d["status"] == "ok", f"健康态被误判: {d['issues']}"


# ------------------------------------------------------------- 采集字段
def test_bars_section_reads_buffer_without_io():
    """deque 是**时间升序**（引擎 append 在右端），故 dq[-1] 才是最新一根。

    这里刻意按升序构造——初版我写成降序，测试立刻抓到
    「latest_bar_ts 报成了最早那根」，正好证明这条断言在盯着真实语义。
    """
    now = datetime(2026, 10, 29, 11, 30)
    eng = _FakeEngine(bars={
        "300308.SZ": [_bar(now - timedelta(minutes=(69 - i))) for i in range(70)],
        "688981.SH": [_bar(now - timedelta(minutes=(29 - i))) for i in range(30)],
    })
    d = snapshot(engine=eng, storage=None)
    b = d["market"]["bars_1m"]
    assert b["codes"] == 2, b
    assert b["ready_ge_60"] == 1, "只有 ≥60 根的才算可评分"
    assert b["latest_bar_ts"] == "2026-10-29 11:30", b
    assert b["oldest_bar_ts"] == "2026-10-29 10:21", b


def test_staleness_fields_are_exposed():
    d = snapshot(engine=_FakeEngine(stale=True, max_age=133.5), storage=None)
    st = d["market"]["staleness"]
    assert st["stale"] is True and st["max_age_sec"] == 133.5
    assert st["threshold_sec"] == 120.0
    assert st["stale_n"] == 3 and st["stale_sample"]


# ------------------------------------------------------------- 交易与拒单
def test_today_orders_and_unmatched():
    """委托与成交按 order_id 对齐；未匹配必须单独计数。"""
    from core.data_models import Order
    s = _tmp_storage()
    now = datetime.now()
    s.save_order(Order(ts=now, code="688981.SH", side="BUY", quantity=100,
                       price=99.7), order_id="o1")
    s.save_order(Order(ts=now, code="300308.SZ", side="BUY", quantity=100,
                       price=636.3), order_id="o2")
    d = snapshot(engine=_FakeEngine(), storage=s)
    od = d["trading"]["orders"]
    assert od["today_orders"] == 2, od
    assert od["today_unmatched"] == 2, "两笔都无成交回报，应计 2 笔未匹配"


def test_reject_distribution_comes_from_blocked_by():
    """拒单分布直接读 signals.blocked_by —— 面板最有价值的一格。"""
    from core.data_models import Signal
    s = _tmp_storage()
    now = datetime.now()
    for code in ("688981.SH", "688981.SH", "300308.SZ"):
        s.save_signal(Signal(ts=now, code=code, side="BUY", score=4.78))
    s.mark_blocked([r["id"] for r in s.get_signals(limit=10)][:2],
                   "breadth_lt_2(本轮仅 1 只 BUY < 2)")
    d = snapshot(engine=_FakeEngine(), storage=s)
    top = d["trading"]["rejects"]["top"]
    assert top and top[0]["why"] == "breadth_lt_2(本轮仅 1 只 BUY < 2)", top
    assert top[0]["n"] == 2, top


def test_no_io_contract_no_xtdata_import_path():
    """纪律 2 的可检查形式：模块**不得**在导入/快照路径上调用 xtdata/xttrader。

    这里只做静态检查（源码里不出现这些调用），比运行时抓包更稳：
    真去调用会卡住测试，反而证明不了任何东西。
    """
    src = open(qmt_health.__file__, encoding="utf-8").read()
    for forbidden in ("get_full_tick", "get_market_data_ex",
                      "download_history_data", "XtQuantTrader"):
        assert forbidden not in src, (
            f"qmt_health 里出现了 {forbidden} —— 看板绝不允许向 miniQMT 发请求"
            "（可能阻塞数十秒，把交易进程一起拖住）")


# ------------------------------------------------------------- 路由端到端
def test_route_returns_200_and_json_even_on_failure():
    """`/api/qmt_health` 必须永远 200 + JSON。

    契约是「永不 500」：看板挂了会让「一切正常」与「查询失败」长得一样，
    那比没有看板更危险。此处用 flask test_client 真跑一遍路由。

    Storage 用替身注入：路由默认走 ``_storage()`` → ``Storage()`` → 生产库路径，
    测试**绝不能**碰生产库（见 tests/test_no_production_db_pollution.py）。
    """
    from web.app import create_app
    import web.routes as R

    sto = _tmp_storage()
    orig = R._storage
    R._storage = lambda: sto
    try:
        app = create_app(engine=_FakeEngine())
        r = app.test_client().get("/api/qmt_health")
        assert r.status_code == 200, r.status_code
        d = r.get_json()
        assert d and d.get("status") in ("ok", "warn", "bad"), d
        assert "issues" in d and isinstance(d["issues"], list)
    finally:
        R._storage = orig
        sto.close()


def test_route_never_500_when_engine_explodes():
    """引擎内部抛异常时，路由仍须 200（降级为 bad + 说明），不得 500。"""
    from web.app import create_app
    import web.routes as R

    class _Boom:
        def __getattr__(self, name):
            raise RuntimeError("engine 炸了")

    sto = _tmp_storage()
    orig = R._storage
    R._storage = lambda: sto
    try:
        app = create_app(engine=_Boom())
        r = app.test_client().get("/api/qmt_health")
        assert r.status_code == 200, r.status_code
        d = r.get_json()
        # 要么成功降级出快照（多数片段被 _safe 兜住），要么给出明确的 bad 说明；
        # 唯一不允许的是 HTTP 5xx / 空 body。
        assert d is not None
        assert d.get("status") in ("ok", "warn", "bad")
    finally:
        R._storage = orig
        sto.close()


# ------------------------------------------------- 2026-10-29 复盘补的两条
def test_client_block_reads_the_singleton_not_the_module():
    """★ 门锁 17：client 块必须来自**单例实例**的 health()。

    初版实现写成 ``from core import qmt_client`` —— 取到的是**模块**，
    模块上没有 ``health()`` 也没有 ``_mode``，于是整块恒为 ``{"mode": ""}``。
    这个 bug 极隐蔽：不报错、不降级、JSON 结构完全合法，只是**最关键的那一格
    永远空白**；而空白在页面上与「正常」长得很像 —— 正是本面板要防的那种谎报。
    因此这里断言 health() 的私有档案字段（probe_n / impl）确实读到了。
    """
    d = snapshot(engine=None, storage=None)
    client = d["market"]["client"]
    assert "error" not in client, client
    assert "probe_n" in client, (
        f"client 块不是来自 qmt_client 单例的 health()：{client}")
    assert "impl" in client, client
    assert client.get("mode"), (
        f"模式读成了空值 —— 面板最爱问的「现在是不是 mock」必须能回答：{client}")


def test_engine_absent_is_warn_not_ok():
    """★ 门锁 18：引擎未接入 ⇒ warn，绝不允许给绿色 ok。

    设计取舍：宁可显示「未知」，也不能用「没数据」冒充「没问题」。
    若非如此，看板在引擎挂掉时会显示一片绿 —— 比没有看板更危险。
    """
    d = snapshot(engine=None, storage=None)
    assert d["status"] == "warn", f"引擎未接入却给了 {d['status']}: {d['issues']}"
    tags = [i["tag"] for i in d["issues"]]
    assert "引擎" in tags, d["issues"]

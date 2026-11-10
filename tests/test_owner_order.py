# -*- coding: utf-8 -*-
"""OWNER 指令单通道的守卫测试（2026-11-10）。

★ 本仓测试**不得使用任何 pytest fixture**（含 tmp_path / monkeypatch / capsys）：
  tests/run_all.py 是自研 runner，直接 ``fn()`` 无参调用，带形参一律 TypeError。
  这里用 tempfile + 手工存取环境变量。

守卫重点：这条通道允许「对已持仓加仓」和「卖出」，是全系统唯一能这样做的路径，
所以每条失败分支都必须被钉死 —— 尤其是**幂等**（防重复买入）与**上限**。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
from datetime import date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import owner_order as oo  # noqa: E402

_ENV = "QMT_OWNER_ORDERS_DIR"


class _Sig(object):
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Tick(object):
    def __init__(self, price):
        self.price = price


class _Pos(object):
    def __init__(self, qty, open_date=None, available=None):
        self.quantity = qty
        self.open_date = open_date
        # None = 该 Position 没有券商可用量字段（回退到 open_date 判 T+1）
        self.available = available


class _Storage(object):
    def __init__(self, db_path):
        self.db_path = db_path


class _Eng(object):
    def __init__(self, d):
        self.storage = _Storage(os.path.join(d, "qmt.db"))
        self._positions = {}
        self._owner_done = None
        self._owner_day = ""
        self._owner_day_n = 0
        self.signals = []
        self.buys = []
        self.buy_large = []
        self.sells = []
        self.sell_lots = []
        self.sell_placed_false = False
        self._last_sell_placed = None
        self.sell_ret = True

    # 与引擎同名，便于把真实方法直接传进去
    def _save_signal(self, sig):
        self.signals.append(sig)

    def _handle_buy(self, sig, tick, prices, fixed_amount=0.0, owner_reentry=False):
        self.buys.append((sig.code, fixed_amount))
        self.buy_large.append(owner_reentry)

    def _handle_sell(self, sig, pos, now=None, lots=None):
        self.sells.append(sig.code)
        self.sell_lots.append(lots)
        # 模拟 live 分支：下单成功但返回 False（未成交不释槽），
        # 真实引擎用 _last_sell_placed 回传「确实提交了」。
        if self.sell_placed_false and lots:
            self._last_sell_placed = {
                "ts": now, "code": sig.code, "qty": int(lots),
                "order_id": "SIM%d" % (len(self.sells)), "ok": True,
            }
            return False
        return self.sell_ret


class _Env(object):
    """临时目录 + 环境变量覆盖，退出时完整还原。"""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="ow_")
        self.old = os.environ.get(_ENV)
        os.environ[_ENV] = self.dir
        self.eng = _Eng(self.dir)
        return self

    def queue(self, *items):
        p = os.path.join(self.dir, oo.QUEUE_NAME)
        with io.open(p, "w", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
        return p

    def raw(self, text):
        p = os.path.join(self.dir, oo.QUEUE_NAME)
        io.open(p, "w", encoding="utf-8").write(text)
        return p

    def done(self):
        p = os.path.join(self.dir, oo.DONE_NAME)
        if not os.path.exists(p):
            return {}
        return json.loads(io.open(p, "r", encoding="utf-8").read() or "{}")

    def __exit__(self, *a):
        if self.old is None:
            os.environ.pop(_ENV, None)
        else:
            os.environ[_ENV] = self.old
        shutil.rmtree(self.dir, ignore_errors=True)
        return False


CFG_ON = {"enabled": True, "max_amount": 1000.0, "max_per_day": 5}


def _run(e, cfg=CFG_ON, ticks=None, now=None):
    prices = {c: t.price for c, t in (ticks or {}).items()}
    return oo.run_step(
        e.eng, ticks or {}, prices, cfg=cfg,
        signal_cls=_Sig, get_name=lambda c: c,
        t1_lock=lambda od, td: bool(od) and od.date() == td,
        save_signal=e.eng._save_signal,
        handle_buy=e.eng._handle_buy, handle_sell=e.eng._handle_sell,
        now=now or datetime(2026, 11, 10, 9, 45))


BUY_ONE = {"id": "t1", "code": "510050.SH", "side": "BUY", "lots": 100,
           "price_cap": 3.0, "valid_until": "2026-11-10"}


# ---------------------------------------------------------------- 关闭态
def test_disabled_does_nothing():
    with _Env() as e:
        e.queue(BUY_ONE)
        st = _run(e, cfg={"enabled": False}, ticks={"510050.SH": _Tick(2.9)})
        assert st["submit"] == 0 and e.eng.buys == [] and e.done() == {}


def test_enabled_missing_reads_nothing_when_queue_absent():
    with _Env() as e:
        st = _run(e, ticks={"510050.SH": _Tick(2.9)})
        assert st == {"submit": 0, "reject": 0, "skip": 0, "ok": 0}


# ---------------------------------------------------------------- 买入
def test_buy_submits_with_exact_amount():
    """买入必须用「qty × 现价」当 fixed_amount，才能落成整数手。"""
    with _Env() as e:
        e.queue(BUY_ONE)
        _run(e, ticks={"510050.SH": _Tick(2.9)})
        assert e.eng.buys == [("510050.SH", 290.0)], e.eng.buys
        assert e.done()["t1"]["status"] == oo.ST_SUBMIT


def test_buy_allows_topup_on_held_position():
    """本通道相对 manual_entry/owner_reentry 的唯一放宽点：已持仓也能加仓。"""
    with _Env() as e:
        e.eng._positions["510050.SH"] = _Pos(500)
        e.queue(BUY_ONE)
        _run(e, ticks={"510050.SH": _Tick(2.9)})
        assert len(e.eng.buys) == 1


def test_identical_id_never_submits_twice():
    """幂等：同 id 连跑三轮只买一次（防重复买入的唯一保险）。"""
    with _Env() as e:
        e.queue(BUY_ONE)
        for _ in range(3):
            _run(e, ticks={"510050.SH": _Tick(2.9)})
        assert len(e.eng.buys) == 1, e.eng.buys


def test_expired_instruction_rejected():
    with _Env() as e:
        e.queue(dict(BUY_ONE, valid_until="2026-11-09"))
        _run(e, ticks={"510050.SH": _Tick(2.9)})
        assert e.eng.buys == []
        assert e.done()["t1"]["status"] == oo.ST_REJECT
        assert "过期" in e.done()["t1"]["reason"]


def test_price_cap_waits_then_buys():
    """高于上限时不买、也**不消耗 id**；价格回落后必须还能买。"""
    with _Env() as e:
        e.queue(dict(BUY_ONE, price_cap=2.8))
        _run(e, ticks={"510050.SH": _Tick(2.9)})
        assert e.eng.buys == [] and e.done() == {}
        _run(e, ticks={"510050.SH": _Tick(2.75)})
        assert len(e.eng.buys) == 1


def test_amount_over_cap_rejected():
    with _Env() as e:
        e.queue(dict(BUY_ONE, lots=1000))          # 1000×2.9 = 2900 > 1000
        _run(e, ticks={"510050.SH": _Tick(2.9)})
        assert e.eng.buys == []
        assert "超过单笔上限" in e.done()["t1"]["reason"]


def test_bad_lots_rejected():
    with _Env() as e:
        e.queue(dict(BUY_ONE, lots=150))
        _run(e, ticks={"510050.SH": _Tick(2.9)})
        assert e.eng.buys == []
        assert "100 的整数倍" in e.done()["t1"]["reason"]


def test_daily_limit_blocks_second_instruction():
    with _Env() as e:
        e.queue(dict(BUY_ONE, id="a"), dict(BUY_ONE, id="b"))
        _run(e, cfg=dict(CFG_ON, max_per_day=1), ticks={"510050.SH": _Tick(2.9)})
        assert len(e.eng.buys) == 1
        assert e.done()["b"]["status"] == oo.ST_REJECT


def test_no_tick_keeps_id_unconsumed():
    """无行情价时必须 skip 且不记账，否则一次行情断档就永久丢掉指令。"""
    with _Env() as e:
        e.queue(BUY_ONE)
        _run(e, ticks={})
        assert e.done() == {}
        _run(e, ticks={"510050.SH": _Tick(2.9)})
        assert len(e.eng.buys) == 1


# ---------------------------------------------------------------- 卖出
def test_sell_t1_locked_rejected_without_calling_handler():
    with _Env() as e:
        e.eng._positions["510050.SH"] = _Pos(500, open_date=datetime(2026, 11, 10, 9, 40))
        e.queue({"id": "s1", "code": "510050.SH", "side": "SELL", "lots": 100})
        _run(e, ticks={"510050.SH": _Tick(2.9)})
        assert e.eng.sells == []
        assert "T+1" in e.done()["s1"]["reason"]


def test_sell_success_marks_ok():
    with _Env() as e:
        e.eng._positions["510050.SH"] = _Pos(500, open_date=datetime(2026, 11, 3, 9, 30))
        e.queue({"id": "s2", "code": "510050.SH", "side": "SELL", "lots": 100})
        _run(e, ticks={"510050.SH": _Tick(2.9)})
        assert e.eng.sells == ["510050.SH"]
        assert e.done()["s2"]["status"] == oo.ST_OK


def test_sell_more_than_held_rejected():
    with _Env() as e:
        e.eng._positions["510050.SH"] = _Pos(100, open_date=datetime(2026, 11, 3, 9, 30))
        e.queue({"id": "s3", "code": "510050.SH", "side": "SELL", "lots": 500})
        _run(e, ticks={"510050.SH": _Tick(2.9)})
        assert e.eng.sells == [] and "超过持仓" in e.done()["s3"]["reason"]


def test_sell_without_position_rejected():
    with _Env() as e:
        e.queue({"id": "s4", "code": "300308.SZ", "side": "SELL", "lots": 100})
        _run(e, ticks={"300308.SZ": _Tick(480)})
        assert e.done()["s4"]["status"] == oo.ST_REJECT


def test_sell_handler_returning_false_is_rejected():
    """保护期/委托失败时 _handle_sell 返回 False —— 必须记 reject 而不是 ok。"""
    with _Env() as e:
        e.eng._positions["510050.SH"] = _Pos(500, open_date=datetime(2026, 11, 3, 9, 30))
        e.eng.sell_ret = False
        e.queue({"id": "s5", "code": "510050.SH", "side": "SELL", "lots": 100})
        _run(e, ticks={"510050.SH": _Tick(2.9)})
        assert e.done()["s5"]["status"] == oo.ST_REJECT


# ---------------------------------------------------------------- 卖出价格下限
# 2026-11-10 新增 price_floor：OWNER 要「涨到成本以上再卖」，而原来只有买单的
# price_cap（不追高），没有镜像字段 ⇒ 挂上去会无条件按下一笔现价成交。
def test_sell_price_floor_waits_below_floor():
    """现价低于下限 ⇒ skip：不落单、不记账、不消耗 id。"""
    with _Env() as e:
        e.eng._positions["510050.SH"] = _Pos(500, open_date=datetime(2026, 11, 3, 9, 30))
        e.queue({"id": "f1", "code": "510050.SH", "side": "SELL",
                 "lots": 100, "price_floor": 3.0})
        st = _run(e, ticks={"510050.SH": _Tick(2.9)})
        assert e.eng.sells == []
        assert st["skip"] == 1 and st["submit"] == 0
        assert e.done() == {}          # ★ 关键：没记账，下一轮还能再看


def test_sell_price_floor_executes_at_or_above_floor():
    with _Env() as e:
        e.eng._positions["510050.SH"] = _Pos(500, open_date=datetime(2026, 11, 3, 9, 30))
        e.queue({"id": "f2", "code": "510050.SH", "side": "SELL",
                 "lots": 100, "price_floor": 2.9})
        _run(e, ticks={"510050.SH": _Tick(2.9)})   # 恰好等于下限 ⇒ 执行
        assert e.eng.sells == ["510050.SH"]
        assert e.done()["f2"]["status"] == oo.ST_OK


def test_sell_price_floor_survives_waiting_days():
    """等待期间不消耗每日条数：等多久都不影响 max_per_day。"""
    with _Env() as e:
        e.eng._positions["510050.SH"] = _Pos(500, open_date=datetime(2026, 11, 3, 9, 30))
        e.queue({"id": "f3", "code": "510050.SH", "side": "SELL",
                 "lots": 100, "price_floor": 3.0})
        for _ in range(20):                         # 模拟 20 轮 tick 都没涨到
            _run(e, ticks={"510050.SH": _Tick(2.95)})
        assert e.eng._owner_day_n == 0 and e.done() == {}
        _run(e, ticks={"510050.SH": _Tick(3.01)})   # 涨过了下限
        assert e.eng.sells == ["510050.SH"]
        assert e.eng._owner_day_n == 1


def test_sell_without_price_floor_stays_immediate():
    """兼容旧指令：不给 price_floor 就按原样立刻卖，行为零变化。"""
    with _Env() as e:
        e.eng._positions["510050.SH"] = _Pos(500, open_date=datetime(2026, 11, 3, 9, 30))
        e.queue({"id": "f4", "code": "510050.SH", "side": "SELL", "lots": 100})
        _run(e, ticks={"510050.SH": _Tick(2.1)})    # 价格很低也照卖
        assert e.eng.sells == ["510050.SH"]


def test_sell_price_floor_still_respects_t1_lock():
    """下限满足但当日买入 ⇒ 仍必须被 T+1 拦下（两道闸门不许互相覆盖）。"""
    with _Env() as e:
        e.eng._positions["510050.SH"] = _Pos(100, open_date=datetime(2026, 11, 10, 13, 4))
        e.queue({"id": "f5", "code": "510050.SH", "side": "SELL",
                 "lots": 100, "price_floor": 2.0})
        _run(e, ticks={"510050.SH": _Tick(5.0)})
        assert e.eng.sells == []
        assert "T+1" in e.done()["f5"]["reason"]


# ---------------------------------------------------------------- 鲁棒性
def test_broken_json_line_does_not_block_others():
    with _Env() as e:
        e.raw('{"id":"bad", "code":\n'
              + json.dumps(BUY_ONE) + "\n")
        _run(e, ticks={"510050.SH": _Tick(2.9)})
        assert len(e.eng.buys) == 1


def test_unknown_side_rejected():
    with _Env() as e:
        e.queue(dict(BUY_ONE, id="x", side="HOLD"))
        _run(e, ticks={"510050.SH": _Tick(2.9)})
        assert e.done()["x"]["status"] == oo.ST_REJECT


# ---------------------------------------------------------------- 挂载点静态守卫
def test_mount_point_references_only_existing_names():
    """把挂载段插进真实源码后，它引用的模块级名字必须全都存在。

    这条守的是最容易漏的一类事故：补丁编译通过（SyntaxError 检查抓不到），
    但运行时 NameError —— 而它发生在主循环里，会被宽 except 吞成一行 warning。
    """
    import ast
    src = io.open("engine/event_engine.py", "r", encoding="utf-8").read()
    sys.path.insert(0, "scripts")
    import patch_owner_order as patcher
    patched = patcher.apply_transform(src)
    # 注意：apply_transform 是幂等的 —— 已挂载时原样返回。所以这里**不能**断言
    # patched != src（落盘后就恒等了）；合法性由下面的 AST 结构与名字检查来证明。

    tree = ast.parse(patched)
    # 1) 方法必须存在，且被调用一次
    methods = [n.name for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_owner_order_step"]
    assert methods == ["_owner_order_step"], methods
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "_owner_order_step"]
    assert len(calls) == 1, len(calls)

    # 2) 模块级已存在的名字集合
    top = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
            top.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    top.add(t.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                top.add((a.asname or a.name).split(".")[0])

    # 3) 挂载段里真正引用的模块级标识符
    need = {"logger", "system_notice", "Signal", "get_stock_name",
            "is_t1_locked", "STRATEGY_PARAMS"}
    missing = need - top
    assert not missing, "挂载段引用了源码里不存在的名字: %s" % missing


def test_mock_data_blocks_whole_channel_without_consuming_id():
    """mock 行情下整条通道不动作：既不提交，也不写 done（id 留到下轮）。

    为什么单独守这一条：_handle_buy 没有返回值，引擎在 mock 下拦掉单子后
    账本若仍记 submit，指令就被"消耗"了却没有任何成交 —— 事后无法区分
    "下过单但被拦" 与 "根本没下"。
    """
    with _Env() as e:
        e.eng.data_mode = "mock"
        e.eng.allow_mock_trading = False
        e.queue(BUY_ONE)
        st = _run(e, ticks={"510050.SH": _Tick(2.9)})
        assert st["submit"] == 0 and e.eng.buys == [] and e.done() == {}
        # 行情恢复后同一条指令必须还能执行
        e.eng.data_mode = "tushare"
        _run(e, ticks={"510050.SH": _Tick(2.9)})
        assert len(e.eng.buys) == 1


# ------------------------------------------------- 单笔金额覆盖 / 大额放行（13:50）
def test_buy_per_instruction_max_amount_overrides_global():
    """全局 max_amount=1000，但本条目写了 max_amount=50000 ⇒ 必须放行。

    场景（2026-11-10 实盘）：接回 300308 一手 = 100 股 × 470 元 ≈ 4.7 万，
    远大于全局 1000 元的小额验证上限。全局值不动，只给这一条开口子。
    """
    with _Env() as e:
        e.queue({"id": "big1", "code": "300308.SZ", "side": "BUY", "lots": 100,
                 "max_amount": 50000.0, "valid_until": "2026-11-10"})
        st = _run(e, ticks={"300308.SZ": _Tick(470.0)})
        assert st["submit"] == 1 and st["reject"] == 0
        assert e.eng.buys[0][1] == 47000.0


def test_buy_without_override_still_hits_global_cap():
    """没写 max_amount 的条目，必须继续被全局 1000 元拦住（默认行为不变）。"""
    with _Env() as e:
        e.queue({"id": "big2", "code": "300308.SZ", "side": "BUY", "lots": 100,
                 "valid_until": "2026-11-10"})
        _run(e, ticks={"300308.SZ": _Tick(470.0)})
        assert e.eng.buys == []
        assert "超过单笔上限" in e.done()["big2"]["reason"]


def test_buy_allow_large_position_flag_reaches_handle_buy():
    """allow_large_position=true 必须以 owner_reentry=True 传到落单口。

    不传就等于只放开了金额、没放开 40% 单仓上限 —— 4.7 万占 6 万权益的 78%，
    risk.can_open 仍会拒。两个开关必须成对，这条钉的就是这个耦合。
    """
    with _Env() as e:
        e.queue({"id": "big3", "code": "300308.SZ", "side": "BUY", "lots": 100,
                 "max_amount": 50000.0, "allow_large_position": True,
                 "valid_until": "2026-11-10"})
        _run(e, ticks={"300308.SZ": _Tick(470.0)})
        assert e.eng.buy_large == [True]


def test_buy_default_does_not_allow_large_position():
    with _Env() as e:
        e.queue(dict(BUY_ONE))
        _run(e, ticks={"510050.SH": _Tick(2.9)})
        assert e.eng.buy_large == [False]


def test_sell_is_not_subject_to_max_amount():
    """卖出不受单笔金额上限约束：一手 300308 4.7 万必须能卖。

    金额上限是「建仓」的护栏（防手滑超配），卖出是减仓、永远是降风险方向，
    套上限只会让止损卖不出去。这条钉住「以后不要顺手把上限加到 SELL 分支」。
    """
    with _Env() as e:
        e.eng._positions["300308.SZ"] = _Pos(100, open_date=datetime(2026, 11, 3, 10, 59))
        e.queue({"id": "big4", "code": "300308.SZ", "side": "SELL", "lots": 100})
        st = _run(e, ticks={"300308.SZ": _Tick(470.0)})
        assert st["ok"] == 1 and st["reject"] == 0 and e.eng.sells == ["300308.SZ"]


# ------------------------------------------- 券商可用量优先于 open_date（2026-11-10）
def test_sell_uses_broker_available_over_merged_open_date():
    """加仓把 open_date 刷成当日，但券商可用 500 股 ⇒ 必须能卖 100 股。

    实盘现场：510050 券商可卖 500，13:36 加仓后 open_date=当日，
    旧逻辑对整仓 800 股判 T+1 锁定，连 11-03 买的老仓都卖不出去。
    """
    with _Env() as e:
        e.eng._positions["510050.SH"] = _Pos(
            800, open_date=datetime(2026, 11, 10, 13, 36), available=500)
        e.queue({"id": "av1", "code": "510050.SH", "side": "SELL", "lots": 100})
        st = _run(e, ticks={"510050.SH": _Tick(2.9)})
        assert st["ok"] == 1 and e.eng.sells == ["510050.SH"]


def test_sell_rejected_when_broker_available_is_zero():
    """券商明说可用 0（今天买的）⇒ 拒绝，哪怕 open_date 是老的。"""
    with _Env() as e:
        e.eng._positions["510050.SH"] = _Pos(
            100, open_date=datetime(2026, 11, 3, 9, 30), available=0)
        e.queue({"id": "av2", "code": "510050.SH", "side": "SELL", "lots": 100})
        _run(e, ticks={"510050.SH": _Tick(2.9)})
        assert e.eng.sells == []
        assert "可用量为 0" in e.done()["av2"]["reason"]


def test_sell_rejected_when_lots_exceed_broker_available():
    """可用 200 却要卖 300 ⇒ 拒绝，并说清上限来源。"""
    with _Env() as e:
        e.eng._positions["510050.SH"] = _Pos(
            800, open_date=datetime(2026, 11, 3, 9, 30), available=200)
        e.queue({"id": "av3", "code": "510050.SH", "side": "SELL", "lots": 300})
        _run(e, ticks={"510050.SH": _Tick(2.9)})
        assert e.eng.sells == []
        assert "超过券商可用 200" in e.done()["av3"]["reason"]


def test_sell_falls_back_to_open_date_when_available_unknown():
    """拿不到 available（字段为 None）⇒ 仍按 open_date 判 T+1，行为不变。"""
    with _Env() as e:
        e.eng._positions["510050.SH"] = _Pos(
            300, open_date=datetime(2026, 11, 10, 9, 31), available=None)
        e.queue({"id": "av4", "code": "510050.SH", "side": "SELL", "lots": 100})
        _run(e, ticks={"510050.SH": _Tick(2.9)})
        assert e.eng.sells == []
        assert "T+1" in e.done()["av4"]["reason"]


# ------------------------------------- lots 入参 / 提交成功但返回 False（2026-11-10 P0）
def test_sell_passes_requested_lots_to_engine():
    """指令写 lots=100，落单口必须收到 lots=100 —— 而不是让它自己去清仓。

    实盘事故：13:52 指令 lots=100，实际卖掉 500 股（整仓可用量）。
    根因是 `_handle_sell` 原签名没有"这次卖多少"的入参。
    """
    with _Env() as e:
        e.eng._positions["510050.SH"] = _Pos(
            800, open_date=datetime(2026, 11, 3, 9, 30), available=500)
        e.queue({"id": "lp1", "code": "510050.SH", "side": "SELL", "lots": 100})
        _run(e, ticks={"510050.SH": _Tick(2.9)})
        assert e.eng.sell_lots == [100]


def test_sell_submitted_but_false_is_recorded_as_submit():
    """live 落单成功却返回 False（未成交不释槽）⇒ 账本必须记 submit，不是 reject。

    事故表现：13:52 那笔下单成功（order_id=0165958027），账本写成
    `reject / 落单口未成交` —— 与事实相反，且没计入 max_per_day。
    """
    with _Env() as e:
        e.eng._positions["510050.SH"] = _Pos(
            800, open_date=datetime(2026, 11, 3, 9, 30), available=500)
        e.eng.sell_placed_false = True
        e.queue({"id": "lp2", "code": "510050.SH", "side": "SELL", "lots": 100})
        st = _run(e, ticks={"510050.SH": _Tick(2.9)})
        assert st["submit"] == 1 and st["reject"] == 0
        assert e.done()["lp2"]["status"] == oo.ST_SUBMIT
        assert "已提交" in e.done()["lp2"]["reason"]
        assert e.eng._owner_day_n == 1     # 提交也要计入每日条数


def test_sell_blocked_without_order_still_records_reject():
    """真的被拦（没产生任何委托）仍必须记 reject —— 别把"没提交"也记成 submit。"""
    with _Env() as e:
        e.eng._positions["510050.SH"] = _Pos(
            800, open_date=datetime(2026, 11, 3, 9, 30), available=500)
        e.eng.sell_ret = False              # 返回 False 且不产生 _last_sell_placed
        e.queue({"id": "lp3", "code": "510050.SH", "side": "SELL", "lots": 100})
        st = _run(e, ticks={"510050.SH": _Tick(2.9)})
        assert st["reject"] == 1
        assert e.done()["lp3"]["status"] == oo.ST_REJECT

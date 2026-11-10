# -*- coding: utf-8 -*-
"""实盘区买卖交易模块（web/trade_api.py）的守卫测试（2026-11-10）。

★ 本仓测试不得使用 pytest fixture：``tests/run_all.py`` 是自研 runner，
  直接 ``fn()`` 无参调用，带形参一律 TypeError。

守卫重点（每条都断言「缺陷复发时会红」）：

  T1 只写队列、绝不碰 broker —— 静态断言模块源码里没有 broker/下单调用
  T2 面板的拒绝必须与引擎同口径 —— 预检复用 core.owner_order.decide()
  T3 被引擎会拒的指令**不得入队**（否则队列里会堆永远不执行的单）
  T4 必须有一次性令牌（防误触 / 防跨站）
  T5 paper 区默认禁用，live 才生效，QMT_TRADE_UI=0 一键关闭
  T6 追加只写一行，且不改写历史行（append-only）
  T7 取消只能作用于「尚未记账」的 id；已记账的不得被"取消"成没下过单
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from types import SimpleNamespace

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

_ENVD = "QMT_OWNER_ORDERS_DIR"
CODE = "510050.SH"


# ============================================================ 假引擎
def _engine(mode="live", ticks=None, positions=None, data_mode="tushare",
            halted=False, total=60000.0, cash=8817.48):
    eng = SimpleNamespace(
        exec_mode=mode,
        data_mode=data_mode,
        _cash=cash,
        _broker_total_asset=total,
        _total_asset=lambda: total,
        latest_ticks=lambda: (ticks if ticks is not None
                              else {CODE: {"price": 2.900}}),
        _positions=(positions if positions is not None
                    else {CODE: SimpleNamespace(quantity=600, available=500,
                                                avg_cost=2.893, last_price=2.900)}),
        risk=SimpleNamespace(_halted=halted,
                             _halt_reason=("daily_stop -7.04%" if halted else ""),
                             _peak_asset=67453.62),
        dynamic_universe=None,
    )
    return eng


class _Env(object):
    """临时指令目录 + 环境变量还原。"""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="tradeui_")
        self.old = os.environ.get(_ENVD)
        os.environ[_ENVD] = self.dir
        self.old_ui = os.environ.get("QMT_TRADE_UI")
        self.old_paper = os.environ.get("QMT_TRADE_UI_ALLOW_PAPER")
        return self

    def __exit__(self, *a):
        for k, v in ((_ENVD, self.old), ("QMT_TRADE_UI", self.old_ui),
                     ("QMT_TRADE_UI_ALLOW_PAPER", self.old_paper)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.dir, ignore_errors=True)
        return False

    @property
    def queue(self):
        return os.path.join(self.dir, "owner_orders.jsonl")

    @property
    def done(self):
        return os.path.join(self.dir, "owner_orders_done.json")

    def write_queue(self, *items):
        with io.open(self.queue, "w", encoding="utf-8", newline="\n") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
        return self.queue

    def read_queue(self):
        if not os.path.exists(self.queue):
            return []
        with io.open(self.queue, "r", encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip() and not x.strip().startswith("#")]

    def read_done(self):
        if not os.path.exists(self.done):
            return {}
        with io.open(self.done, "r", encoding="utf-8") as f:
            return json.loads(f.read() or "{}")


def _client(engine=None):
    from web.app import create_app
    app = create_app(engine or _engine())
    app.config["TESTING"] = True
    return app.test_client()


def _token(cli):
    r = cli.post("/api/trade/token", json={})
    assert r.status_code == 200, r.data[:200]
    return r.get_json()["token"]


def _submit(cli, tok, **body):
    return cli.post("/api/trade/submit", json=body,
                    headers={"X-Trade-Token": tok})


TODAY = date.today().isoformat()


# ============================================================ T1 不碰 broker
def test_module_never_touches_broker():
    """★ 静态断言：交易面板只能写队列，落单必须走引擎唯一落单口。

    若这里失败，说明有人为了「让面板能下单」直接在 Flask 里调了 broker ——
    那条路绕过 _handle_buy/_handle_sell 的全部风控（熔断/T+1/保护期/现金夹紧/
    行情新鲜度/总资产可信度），是本项目最明确的一条红线。

    用 AST 而不是字符串匹配：docstring 里写着「本模块不 import qmt_broker」
    也含那个词，纯 grep 会假红（第一版就踩了）。
    """
    import ast

    src = io.open(os.path.join(ROOT, "web", "trade_api.py"),
                  "r", encoding="utf-8").read()
    tree = ast.parse(src)
    banned_mods = ("qmt_broker", "core.broker", "core.broker_bridge",
                   "core.qmt_client", "qmt_bridge", "xtquant")
    banned_calls = ("place_order", "passorder", "cancel_order_stock",
                    "order_stock", "order_value")

    def _bad(mod: str) -> bool:
        return any(mod == b or mod.startswith(b + ".") for b in banned_mods)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert not _bad(mod), (
                "web/trade_api.py 从 %r 导入 ⇒ 面板可能绕过唯一落单口" % mod)
        elif isinstance(node, ast.Import):
            for a in node.names:
                assert not _bad(a.name), (
                    "web/trade_api.py 导入 %r ⇒ 面板可能绕过唯一落单口" % a.name)
        elif isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            assert name not in banned_calls, (
                "web/trade_api.py 调用了 %r ⇒ 直接下单，绕过全部风控" % name)


def test_load_done_cache_is_not_dropped_by_or_default():
    """禁止写 `load_done(...) or {}`：账本为空时它是假值，`or` 会换掉缓存对象，
    导致 record() 的写入只落文件、内存缓存仍空 ⇒ 同进程内后续读取看不到状态。

    （2026-11-10 由 test_cancel_only_pending_and_marks_done 抓到。）
    """
    src = io.open(os.path.join(ROOT, "web", "trade_api.py"),
                  "r", encoding="utf-8").read()
    assert "load_done(e, logger=logger) or {}" not in src, (
        "又出现了 `load_done(...) or {}` ⇒ 会在账本为空时丢掉缓存对象")


# ============================================================ T2/T3 同口径
def test_precheck_reuses_engine_decide():
    """面板预检必须复用 core.owner_order.decide（同一份校验，不是另写一套）。"""
    src = io.open(os.path.join(ROOT, "web", "trade_api.py"),
                  "r", encoding="utf-8").read()
    assert "OO.decide(" in src, "预检没有调用 owner_order.decide ⇒ 会出现双口径"
    # 不允许在面板里重复实现「100 股整数倍」这类业务阈值
    assert "lots % 100" not in src, "面板里重新实现了 lots 整数倍校验（应复用 decide）"


def test_rejected_order_is_never_appended():
    """引擎会拒的指令不得入队 —— 否则队列里堆满永远不执行的单，人以为已下单。"""
    with _Env() as e:
        cli = _client()
        tok = _token(cli)
        # 1000 股 × 2.9 = 2,900 元 > owner_order.max_amount(1000) ⇒ decide 必拒
        r = _submit(cli, tok, side="BUY", code=CODE, lots=1000,
                    valid_until=TODAY, note="超上限")
        assert r.status_code == 400, (r.status_code, r.data[:200])
        assert "拒绝" in r.get_json()["reason"]
        assert e.read_queue() == [], "被拒的指令仍被写进了队列"


def test_legal_order_appends_exactly_one_line():
    with _Env() as e:
        cli = _client()
        tok = _token(cli)
        r = _submit(cli, tok, side="BUY", code=CODE, lots=100, price_cap=2.95,
                    valid_until=TODAY, note="面板测试")
        assert r.status_code == 200, (r.status_code, r.data[:300])
        j = r.get_json()
        assert j["ok"], j
        rows = e.read_queue()
        assert len(rows) == 1, rows
        ins = rows[0]
        assert ins["id"] == j["id"] and ins["id"].startswith("ui-")
        for k, v in (("code", CODE), ("side", "BUY"), ("lots", 100),
                     ("price_cap", 2.95), ("valid_until", TODAY),
                     ("note", "面板测试")):
            assert ins[k] == v, (k, ins)
        assert j["precheck"]["status"] in ("submit", "skip"), j["precheck"]


def test_append_preserves_existing_lines():
    """append-only：既有历史行一个字节都不能被改写（它们是审计证据）。"""
    with _Env() as e:
        e.write_queue({"id": "old-1", "code": CODE, "side": "BUY", "lots": 100,
                       "valid_until": TODAY, "note": "历史行"})
        before = io.open(e.queue, "rb").read()
        cli = _client()
        _submit(cli, _token(cli), side="SELL", code=CODE, lots=100,
                valid_until=TODAY, note="追加")
        after = io.open(e.queue, "rb").read()
        assert after.startswith(before), "既有行被改写了（append-only 被破坏）"
        assert len(e.read_queue()) == 2


# ============================================================ T4 令牌
def test_submit_requires_valid_one_shot_token():
    with _Env() as e:
        cli = _client()
        body = dict(side="BUY", code=CODE, lots=100, valid_until=TODAY)
        r = cli.post("/api/trade/submit", json=body)      # 无令牌
        assert r.status_code == 400, r.status_code
        tok = _token(cli)
        assert _submit(cli, tok, **body).status_code == 200
        # 同一个令牌不得复用（单次有效）
        r2 = _submit(cli, tok, **body)
        assert r2.status_code == 400, "令牌被复用 ⇒ 重放保护失效"
        assert len(e.read_queue()) == 1


# ============================================================ T5 开关与分区
def test_disabled_switch_blocks_everything():
    with _Env():
        os.environ["QMT_TRADE_UI"] = "0"
        cli = _client()
        assert cli.get("/trade").status_code == 403
        assert cli.get("/api/trade/status").status_code == 403
        assert cli.post("/api/trade/token", json={}).status_code == 403


def test_paper_zone_disabled_by_default():
    with _Env():
        os.environ.pop("QMT_TRADE_UI_ALLOW_PAPER", None)
        cli = _client(_engine(mode="paper"))
        assert cli.get("/trade").status_code == 403, "paper 区默认不得开买卖面板"
        assert cli.post("/api/trade/token", json={}).status_code == 403


def test_live_zone_page_and_status_ok():
    with _Env():
        cli = _client()
        assert cli.get("/trade").status_code == 200
        j = cli.get("/api/trade/status").get_json()
        assert j["zone"] == "live" and j["zone_allowed"] is True
        assert j["channel"]["enabled"] is True
        assert j["engine"]["exec_mode"] == "live"
        assert any(p["code"] == CODE for p in j["positions"])


def test_asset_not_trusted_hides_construction_defaults():
    """★ 引擎未经券商同步时，面板不得显示构造初值（cash=1,000,000）。

    live 引擎刚启动/断链期，内存里 `_cash=INITIAL_CASH(100万)`、`_positions={}`
    是**构造初值而不是账户真相**（engine 的 `_save_engine_state` 有同款记载）。
    面板若把它当「可用现金」显示，等于让人对着假数字决定真金白银的仓位。
    """
    with _Env():
        eng = _engine()
        eng._broker_total_asset = 0.0        # 未拿到券商总资产
        eng._cash = 1000000.0                # 构造初值
        eng._total_asset = lambda: 1000000.0
        eng._positions = {}
        cli = _client(eng)
        j = cli.get("/api/trade/status").get_json()
        assert j["engine"]["asset_trusted"] is False
        assert j["engine"]["cash"] is None, "未同步却把构造初值 100 万当现金返回"
        assert j["engine"]["total_asset"] is None
        assert j["positions"] == []


def test_asset_trusted_shows_real_numbers():
    """正向对照：券商资产到位时必须正常显示（防「一律隐藏」把面板做成废物）。"""
    with _Env():
        cli = _client(_engine(total=60119.78, cash=8817.48))
        j = cli.get("/api/trade/status").get_json()
        assert j["engine"]["asset_trusted"] is True
        assert abs(j["engine"]["cash"] - 8817.48) < 0.01
        assert abs(j["engine"]["total_asset"] - 60119.78) < 0.01
        assert any(p["code"] == CODE for p in j["positions"])


def test_status_surfaces_mock_and_halt_warnings():
    """mock 行情 / 熔断必须在状态里显式暴露（前端据此提示「单子会一直 pending」）。"""
    with _Env():
        cli = _client(_engine(data_mode="mock", halted=True))
        j = cli.get("/api/trade/status").get_json()
        assert j["engine"]["data_mode"] == "mock"
        assert j["risk"]["halted"] is True
        assert "daily_stop" in j["risk"]["halt_reason"]


# ============================================================ T6 输入校验
def test_field_validation_rejects_bad_input():
    with _Env() as e:
        cli = _client()
        bad = [
            dict(side="BUY", code="510050", lots=100),          # 缺后缀
            dict(side="HOLD", code=CODE, lots=100),             # 非法方向
            dict(side="BUY", code=CODE, lots=0),                # 非正股数
            dict(side="BUY", code=CODE, lots=150),              # 非整百（decide 会拒）
            dict(side="BUY", code=CODE, lots=100, price_cap=-1),
            dict(side="BUY", code=CODE, lots=100, valid_until="2020-01-01"),
        ]
        for body in bad:
            body.setdefault("valid_until", TODAY)
            r = _submit(cli, _token(cli), **body)
            assert r.status_code == 400, (body, r.status_code, r.data[:200])
        assert e.read_queue() == [], "非法输入仍有行被写进队列"


# ============================================================ T7 取消
def test_cancel_only_pending_and_marks_done():
    with _Env() as e:
        cli = _client()
        j = _submit(cli, _token(cli), side="BUY", code=CODE, lots=100,
                    valid_until=TODAY).get_json()
        oid = j["id"]
        r = cli.post("/api/trade/cancel", json={"id": oid},
                     headers={"X-Trade-Token": _token(cli)})
        assert r.status_code == 200, r.data[:200]
        d = e.read_done()
        assert d.get(oid, {}).get("status") == "cancelled", d
        # 已记账的 id 不得再被取消（否则会把「已提交」洗成「没下过单」）
        r2 = cli.post("/api/trade/cancel", json={"id": oid},
                      headers={"X-Trade-Token": _token(cli)})
        assert r2.status_code == 400, "已记账的指令还能被取消 ⇒ 账本可能被洗白"


def test_cancel_unknown_id_rejected():
    with _Env():
        cli = _client()
        r = cli.post("/api/trade/cancel", json={"id": "no-such-id"},
                     headers={"X-Trade-Token": _token(cli)})
        assert r.status_code == 404, r.status_code


def test_cancelled_id_is_skipped_by_engine_channel():
    """取消要真的生效：run_step 必须跳过 done 里的 id（不执行、不消耗额度）。"""
    from core import owner_order as OO
    with _Env() as e:
        cli = _client()
        j = _submit(cli, _token(cli), side="BUY", code=CODE, lots=100,
                    valid_until=TODAY).get_json()
        oid = j["id"]
        cli.post("/api/trade/cancel", json={"id": oid},
                 headers={"X-Trade-Token": _token(cli)})

        eng = _engine()
        submitted = []

        def _handle_buy(sig, tick, prices, fixed_amount=0.0):
            submitted.append(sig.code)

        class _Sig:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        stats = OO.run_step(
            eng, {CODE: SimpleNamespace(price=2.900)}, {CODE: 2.900},
            cfg={"enabled": True, "max_amount": 1000.0, "max_per_day": 5},
            signal_cls=_Sig, get_name=lambda c: c,
            t1_lock=lambda od, td: False, save_signal=lambda s: None,
            handle_buy=_handle_buy, handle_sell=lambda *a, **k: False)
        assert submitted == [], "已取消的指令仍被执行了"
        assert stats["submit"] == 0 and stats["ok"] == 0, stats
        assert e.read_done().get(oid, {}).get("status") == "cancelled"

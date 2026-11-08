# -*- coding: utf-8 -*-
"""qmt_bridge/qmtide_bridge.py 的端到端测试。

这个文件是**真实下单的那一段代码**，此前零覆盖。测试按真身加载发货代码
（importlib 从 qmt_bridge/qmtide_bridge.py 直接加载），然后把 QMT 注入的
``passorder`` / ``get_trade_detail_data`` / ``cancel`` 换成假实现。

重点钉住三件事：
1. passorder 的参数顺序。QMT 官方随附的可跑示例
   ``C:\\pazq_qmt\\python\\双均线实盘示例PY.py`` 用的是 11 参、不含 accountType:
       passorder(op, otype, acct, code, prtype, price, vol, name, 1, msg, C)
   早期实现把 accountType 塞进了账号位，后面所有参数整体错位一格 —— 会下错单
   或在错误账号上报单。这里断言 args[2] 必须是资金账号而不是 "STOCK"。
2. 同一个 signal_id 只能执行一次。否则引擎重发信号会把 100 股卖成 200 股。
3. 拿不到行情/账号时返回 error 而不是抛异常，避免 QMT 侧计时器被拖死。
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "qmt_bridge", "qmtide_bridge.py")

ACCT = "000000000000"        # 假账号：真实账号由 QMT 注入，绝不写进仓库
ATYPE = "STOCK"


class Row(object):
    """get_trade_detail_data 返回的对象：属性访问即可。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeContext(object):
    accountID = ACCT
    account_type = ATYPE
    _bridge_po_form = None

    def __init__(self):
        self.timers = []

    def run_time(self, name, period, start):
        self.timers.append((name, period, start))


class Module(object):
    """加载被测模块 + 假 QMT 环境。"""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="qmtbridge_")
        os.environ["QMT_BRIDGE_ROOT"] = self.dir
        try:
            spec = importlib.util.spec_from_file_location(
                "qmtide_bridge_under_test", SRC)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        finally:
            os.environ.pop("QMT_BRIDGE_ROOT", None)
        self.mod = mod
        mod.SKIP_SOCKET_PROBE = True      # 别让每个用例白等 0.5s 的 connect

        self.po_calls = []
        self.trade_rows = {"ACCOUNT": [], "POSITION": [], "ORDER": [], "DEAL": []}
        self.cancel_calls = []
        self.po_fail_first = 0

        def fake_passorder(*args):
            if self.po_fail_first > 0:
                self.po_fail_first -= 1
                raise TypeError("passorder() takes %d positional arguments"
                                % (len(args) - 1))
            self.po_calls.append(args)
            return None

        def fake_trade(aid, atype, kind):
            self.last_trade_args = (aid, atype, kind)
            return self.trade_rows.get(kind, [])

        def fake_cancel(*args):
            self.cancel_calls.append(args)
            return None

        mod.passorder = fake_passorder
        mod.get_trade_detail_data = fake_trade
        mod.cancel = fake_cancel

    @property
    def signal_file(self):
        return os.path.join(self.dir, "signal.json")

    @property
    def result_file(self):
        return os.path.join(self.dir, "result.json")

    @property
    def heartbeat_file(self):
        return os.path.join(self.dir, "heartbeat.json")

    @property
    def boot_log(self):
        return os.path.join(self.dir, "boot.log")

    def put_signal(self, signal_id, action, **params):
        payload = {
            "signal_id": signal_id,
            "action": action,
            "account": "cash",
            "params": params,
            "ts": 1.0,
        }
        with open(self.signal_file, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        return payload

    def result(self):
        with open(self.result_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.m = Module()
        self.ctx = FakeContext()

    def tearDown(self):
        self.m.cleanup()


class TestBootLog(Base):
    def test_boot_log_recorded_at_import(self):
        with open(self.m.boot_log, "r", encoding="utf-8") as f:
            text = f.read()
        self.assertIn("passorder=False", text,
                      "导入时没注入 passorder，应记为 False")
        self.assertIn("name='qmtide_bridge_under_test'", text)

    def test_has_passorder_detects_injection(self):
        # Module() 已经把假 passorder 注入进模块全局，此时应判 True
        self.assertTrue(self.m.mod._has_passorder(),
                        "注入后应判 True（否则 standalone 分支会白白退出）")
        del self.m.mod.passorder
        del self.m.mod.get_trade_detail_data
        self.assertFalse(self.m.mod._has_passorder(),
                         "未注入时应判 False")


class TestInit(Base):
    def test_init_registers_timer_and_writes_heartbeat(self):
        self.m.mod.init(self.ctx)
        self.assertEqual(len(self.ctx.timers), 1)
        name, period, _start = self.ctx.timers[0]
        self.assertEqual(name, "bridge_poll")
        self.assertEqual(period, "500nMilliSecond")
        self.assertTrue(os.path.isfile(self.m.heartbeat_file))
        with open(self.m.heartbeat_file, "r", encoding="utf-8") as f:
            hb = json.load(f)
        self.assertEqual(hb["account"], ACCT)
        self.assertEqual(hb["account_type"], ATYPE)
        self.assertEqual(hb["pid"], os.getpid())

    def test_init_survives_run_time_failure(self):
        class Bad(FakeContext):
            def run_time(self, *a):
                raise RuntimeError("no timer")
        self.m.mod.init(Bad())          # 不许抛
        self.assertTrue(os.path.isfile(self.m.heartbeat_file))


class TestPing(Base):
    def test_ping_roundtrip(self):
        self.m.put_signal("s1", "PING")
        self.m.mod.init(self.ctx)
        self.m.mod.bridge_poll(self.ctx)
        r = self.m.result()
        self.assertEqual(r["signal_id"], "s1")
        self.assertEqual(r["status"], "ok")
        self.assertIn("pong", r["data"])


class TestPassOrder(Base):
    def test_sell_uses_11arg_form_with_account_id_in_position_3(self):
        self.m.put_signal("s1", "SELL", code="300308.SZ", volume=100,
                          price=533.38, prtype=11)
        self.m.mod.init(self.ctx)
        self.m.mod.bridge_poll(self.ctx)

        self.assertEqual(len(self.m.po_calls), 1)
        args = self.m.po_calls[0]
        self.assertEqual(len(args), 11,
                         "QMT 示例用 11 参形式，实际 %d 参: %r" % (len(args), args))
        self.assertEqual(args[0], 24, "SELL 的 op 必须是 24")
        self.assertEqual(args[1], 1101, "orderType 必须是 1101")
        self.assertEqual(args[2], ACCT,
                         "args[2] 必须是资金账号；若是 'STOCK' 说明 accountType "
                         "被塞进了账号位，后面参数全错位")
        self.assertNotEqual(args[2], ATYPE)
        self.assertEqual(args[3], "300308.SZ")
        self.assertEqual(args[4], 11, "prtype")
        self.assertAlmostEqual(args[5], 533.38, places=4)
        self.assertEqual(args[6], 100, "volume")
        self.assertEqual(args[7], "qmtide_bridge", "strategyName")
        self.assertEqual(args[8], 1, "quickTrade")
        self.assertIsInstance(args[9], str, "userOrderId 必须是字符串备注")
        self.assertIs(args[10], self.ctx, "最后一个参数必须是 ContextInfo")

    def test_buy_uses_op_23(self):
        self.m.put_signal("s1", "BUY", code="510050.SH", volume=100, price=2.9)
        self.m.mod.init(self.ctx)
        self.m.mod.bridge_poll(self.ctx)
        self.assertEqual(self.m.po_calls[0][0], 23)

    def test_bare_code_gets_exchange_suffix(self):
        self.m.put_signal("s1", "BUY", code="600000", volume=100, price=10.0)
        self.m.mod.init(self.ctx)
        self.m.mod.bridge_poll(self.ctx)
        self.assertEqual(self.m.po_calls[0][3], "600000.SH")

    def test_price_le_zero_never_reaches_passorder(self):
        self.m.put_signal("s1", "BUY", code="300308.SZ", volume=100, price=0)
        self.m.mod.init(self.ctx)
        self.m.mod.bridge_poll(self.ctx)
        self.assertEqual(self.m.po_calls, [], "price<=0 必须直接拒单，不下市价单")
        self.assertEqual(self.m.result()["status"], "error")

    def test_volume_le_zero_never_reaches_passorder(self):
        self.m.put_signal("s1", "BUY", code="300308.SZ", volume=0, price=10.0)
        self.m.mod.init(self.ctx)
        self.m.mod.bridge_poll(self.ctx)
        self.assertEqual(self.m.po_calls, [])
        self.assertEqual(self.m.result()["status"], "error")

    def test_falls_back_to_next_form_and_pins_it(self):
        self.m.po_fail_first = 1          # 第一个形式失败
        self.m.put_signal("s1", "SELL", code="300308.SZ", volume=100, price=10.0)
        self.m.mod.init(self.ctx)
        self.m.mod.bridge_poll(self.ctx)
        self.assertEqual(len(self.m.po_calls), 1, "回落后应再试一次并成功")
        self.assertEqual(self.ctx._bridge_po_form, "atype_aid_qt",
                         "成功的形式要记住，下次直接用")

    def test_all_forms_fail_returns_error(self):
        self.m.po_fail_first = 10
        self.m.put_signal("s1", "SELL", code="300308.SZ", volume=100, price=10.0)
        self.m.mod.init(self.ctx)
        self.m.mod.bridge_poll(self.ctx)
        r = self.m.result()
        self.assertEqual(r["status"], "error")
        self.assertIn("all forms failed", r["message"])
        self.assertIsNone(r["data"].get("order_id"))


class TestIdempotency(Base):
    def test_same_signal_id_executes_only_once(self):
        self.m.mod.init(self.ctx)
        self.m.put_signal("s1", "SELL", code="300308.SZ", volume=100, price=10.0)
        self.m.mod.bridge_poll(self.ctx)
        self.m.mod.bridge_poll(self.ctx)   # 引擎重发同一 id
        self.m.mod.bridge_poll(self.ctx)
        self.assertEqual(len(self.m.po_calls), 1,
                         "同一 signal_id 必须只下一次单，否则 100 股会卖成 200 股")

    def test_different_signal_ids_both_execute(self):
        self.m.mod.init(self.ctx)
        self.m.put_signal("s1", "SELL", code="300308.SZ", volume=100, price=10.0)
        self.m.mod.bridge_poll(self.ctx)
        self.m.put_signal("s2", "SELL", code="300308.SZ", volume=100, price=10.0)
        self.m.mod.bridge_poll(self.ctx)
        self.assertEqual(len(self.m.po_calls), 2)

    def test_cache_is_capped(self):
        self.m.mod._MAX_CACHED = 3
        self.m.mod.init(self.ctx)
        for i in range(6):
            self.m.put_signal("s%d" % i, "PING")
            self.m.mod.bridge_poll(self.ctx)
        self.assertLessEqual(len(self.m.mod._processed), 3)


class TestCrossRestartIdempotency(Base):
    """QMT 策略重启后，signal.json 里残留的旧信号**绝不能被重放**。

    2026-11-08 02:16 实证：重启后上一轮的 QUERY_POSITION 被又执行了一次。
    对查询无害；若那条是已成交的 BUY，就是重复买入真实资金。
    """

    def _restart(self):
        """模拟 QMT 重启：内存缓存全丢，只剩磁盘。"""
        self.m.mod._processed.clear()
        self.m.mod._processed_order[:] = []
        self.m.mod._seen.clear()
        self.m.mod._load_seen()

    def test_buy_not_replayed_after_restart(self):
        self.m.put_signal("buy-1", "BUY", code="300308.SZ", volume=100, price=448.0)
        self.m.mod.init(self.ctx)
        self.m.mod.bridge_poll(self.ctx)
        self.assertEqual(self.m.result()["status"], "ok")
        self.assertEqual(len(self.m.po_calls), 1)

        self._restart()                       # ← 策略重启，内存缓存清空
        self.m.mod.bridge_poll(self.ctx)      # ← 读到的还是那条 buy-1

        self.assertEqual(len(self.m.po_calls), 1,
                         "重启后重放了已成交的 BUY —— 等于重复买入真实资金")
        self.assertEqual(self.m.result()["status"], "duplicate")

    def test_seen_ids_survive_on_disk(self):
        self.m.put_signal("p1", "PING")
        self.m.mod.init(self.ctx)
        self.m.mod.bridge_poll(self.ctx)
        path = os.path.join(self.m.dir, "processed_ids.json")
        self.assertTrue(os.path.exists(path), "已处理 id 必须落盘，否则重启就重放")
        with open(path, encoding="utf-8") as f:
            ids = json.load(f)["ids"]
        self.assertIn("p1", ids)

    def test_duplicate_branch_logs_only_once(self):
        """拒绝重放只许报一次。

        这条分支每 500ms 命中一次。2026-11-08 02:25 实测：每轮都写日志，
        7 分钟刷了 1288 行、把 boot.log 顶到 200KB 上限**轮转掉了一轮诊断历史**。
        """
        self.m.put_signal("buy-1", "BUY", code="300308.SZ", volume=100, price=448.0)
        self.m.mod.init(self.ctx)
        self.m.mod.bridge_poll(self.ctx)

        self._restart()
        for _ in range(20):                   # 模拟 20 轮 poll（≈10 秒）
            self.m.mod.bridge_poll(self.ctx)

        with open(self.m.boot_log, encoding="utf-8") as f:
            n = f.read().count("拒绝重放")
        self.assertEqual(n, 1, "拒绝重放重复写日志会刷爆 boot.log 并轮转掉诊断历史")
        self.assertEqual(len(self.m.po_calls), 1)

    def test_failed_signal_may_retry_after_restart(self):
        """失败信号不进黑名单：QMT 当时可能只是暂时拒单，重启后应允许重试。"""
        self.m.trade_rows["ACCOUNT"] = None
        self.m.put_signal("q1", "QUERY_ASSET")
        self.m.mod.init(self.ctx)
        self.m.mod.bridge_poll(self.ctx)
        self.assertEqual(self.m.result()["status"], "error")

        self._restart()
        self.m.trade_rows["ACCOUNT"] = [Row(m_dAvailable=1.0, m_dInstrumentValue=2.0)]
        self.m.mod.bridge_poll(self.ctx)
        self.assertEqual(self.m.result()["status"], "ok",
                         "上次失败的信号重启后应能重试成功，而不是被判 duplicate")


class TestQueries(Base):
    def test_ping_reports_code_rev(self):
        """PING 必须带代码版本号 —— 用来证明 QMT 里跑的是新代码还是旧代码。

        只部署文件、不重新运行策略时，QMT 会一直跑内存里的旧代码（改动静默无效）。
        2026-11-08 21:31 改的「市值取持仓汇总」如果没生效，总资产仍会虚高 4,547 元，
        而账面一切正常 —— 没有版本号就只能靠猜。
        """
        self.m.put_signal("s1", "PING")
        self.m.mod.init(self.ctx)
        self.m.mod.bridge_poll(self.ctx)
        d = self.m.result()["data"]
        self.assertEqual(d["rev"], self.m.mod.CODE_REV)
        self.assertTrue(d["rev"], "CODE_REV 不得为空")

    def test_query_asset_maps_fields(self):
        self.m.trade_rows["ACCOUNT"] = [
            Row(m_dAvailable=8337.31, m_dInstrumentValue=58204.99,
                m_dTotalAsset=66542.31)]
        self.m.put_signal("s1", "QUERY_ASSET")
        self.m.mod.init(self.ctx)
        self.m.mod.bridge_poll(self.ctx)
        d = self.m.result()["data"]
        self.assertAlmostEqual(d["cash"], 8337.31, places=2)
        self.assertAlmostEqual(d["market_value"], 58204.99, places=2)
        self.assertAlmostEqual(d["total_asset"], 66542.31, places=2)
        self.assertEqual(self.m.last_trade_args, (ACCT, ATYPE, "ACCOUNT"))

    def test_query_asset_prefers_consistent_broker_total(self):
        """券商口径自洽时就照用，别自作聪明改写。"""
        self.m.trade_rows["ACCOUNT"] = [
            Row(m_dAvailable=11611.49, m_dInstrumentValue=46251.0,
                m_dBalance=57862.49)]
        self.m.put_signal("s1", "QUERY_ASSET")
        self.m.mod.init(self.ctx)
        self.m.mod.bridge_poll(self.ctx)
        d = self.m.result()["data"]
        self.assertAlmostEqual(d["total_asset"], 57862.49, places=2)

    def test_query_asset_rejects_insane_balance(self):
        """2026-11-08 实盘实测：m_dBalance 返 137,321.50，而可用+市值只有
        57,862.49（与引擎 xtquant 直连的 57,862.57 吻合）。差 2.37 倍。
        若原样当作 total_asset，切 bridge 后 event_engine.py:4612 会拿它当
        _broker_total_asset ⇒ 回撤恒为 0 ⇒ max_drawdown 熔断永久失效。
        """
        self.m.trade_rows["ACCOUNT"] = [
            Row(m_dAvailable=11611.49, m_dInstrumentValue=46251.0,
                m_dBalance=137321.5)]
        self.m.put_signal("s1", "QUERY_ASSET")
        self.m.mod.init(self.ctx)
        self.m.mod.bridge_poll(self.ctx)
        d = self.m.result()["data"]
        self.assertAlmostEqual(
            d["total_asset"], 57862.49, places=2,
            msg="必须回落到 cash+市值，绝不能把 137321.5 灌进风控")

    def test_query_asset_uses_position_mv_when_account_is_stale(self):
        """★★ 2026-11-08 21:30 实盘实测：ACCOUNT 与 POSITION 的市值**不是同一个价格**。

        ACCOUNT.m_dInstrumentValue 停在**上一交易日收盘**：
            50,798.00 = 493.33×100 + 2.93×500   （11-05 收盘价）
        POSITION.m_dMarketValue 才是最新价：
            46,251.00 = 448.00×100 + 2.902×500  （11-06 收盘价，当日 300308 −9.18%）

        差额 4,547.00 与账本 11-06 的 daily_pnl −4,547.03 分毫不差。
        若取 ACCOUNT 口径：总资产 62,409.49（真值 57,862.49）⇒ 回撤从 −14.2% 变 −7.5%，
        熔断缓冲凭空多 6.7pp（当时距 −20% 只剩 5.78pp）⇒ 必须用持仓汇总。
        """
        self.m.trade_rows["ACCOUNT"] = [
            Row(m_dAvailable=11611.49, m_dInstrumentValue=50798.0)]
        self.m.trade_rows["POSITION"] = [
            Row(m_strInstrumentID="300308", m_strExchangeID="SZ",
                m_nVolume=100, m_dMarketValue=44800.0),
            Row(m_strInstrumentID="510050", m_strExchangeID="SH",
                m_nVolume=500, m_dMarketValue=1451.0),
        ]
        self.m.put_signal("s1", "QUERY_ASSET")
        self.m.mod.init(self.ctx)
        self.m.mod.bridge_poll(self.ctx)
        d = self.m.result()["data"]
        self.assertAlmostEqual(d["market_value"], 46251.0, places=2,
                               msg="必须用持仓汇总，不能用上一交易日收盘价的 50798")
        self.assertAlmostEqual(d["total_asset"], 57862.49, places=2,
                               msg="总资产必须等于 11-06 收盘真值，不是 62409.49")

    def test_query_asset_does_not_zero_mv_when_positions_look_empty(self):
        """矛盾态：持仓查得到但汇总为 0，而 ACCOUNT 说有市值。

        此时**绝不能**清零 —— total_asset 会瞬间少掉全部持仓 ⇒ 假回撤 −X% ⇒ 假熔断。
        宁可沿用 ACCOUNT 口径（旧一天），也不肯凭空造一次瀑布。
        """
        self.m.trade_rows["ACCOUNT"] = [
            Row(m_dAvailable=11611.49, m_dInstrumentValue=46251.0)]
        self.m.trade_rows["POSITION"] = []      # 查询成功但为空的矛盾态
        self.m.put_signal("s1", "QUERY_ASSET")
        self.m.mod.init(self.ctx)
        self.m.mod.bridge_poll(self.ctx)
        d = self.m.result()["data"]
        self.assertAlmostEqual(d["market_value"], 46251.0, places=2)
        self.assertAlmostEqual(d["total_asset"], 57862.49, places=2)

    def test_query_asset_falls_back_when_position_query_fails(self):
        """持仓查询返回 None（未知）时回落 ACCOUNT 口径，且不得把 None 当 0。"""
        self.m.trade_rows["ACCOUNT"] = [
            Row(m_dAvailable=11611.49, m_dInstrumentValue=46251.0)]
        self.m.trade_rows["POSITION"] = None
        self.m.put_signal("s1", "QUERY_ASSET")
        self.m.mod.init(self.ctx)
        self.m.mod.bridge_poll(self.ctx)
        d = self.m.result()["data"]
        self.assertAlmostEqual(d["total_asset"], 57862.49, places=2)

    def test_dbl_max_sentinel_becomes_default(self):
        """QMT 用 DBL_MAX 表示「字段不适用」（实测 m_dCurrMargin /
        m_dPreBalance / m_dAvgOpenPrice 全是它）。float() 收得下，但它不是数字。"""
        fmax = 1.7976931348623157e+308
        self.assertEqual(self.m.mod._f(fmax, 0.0), 0.0)
        self.assertEqual(self.m.mod._f(-fmax, 0.0), 0.0)
        self.assertEqual(self.m.mod._f(float("nan"), -1.0), -1.0)
        self.assertAlmostEqual(self.m.mod._f("2.902", 0.0), 2.902, places=6)

    def test_order_side_is_unknown_not_sell_when_flag_unrecognised(self):
        """认不出的 m_nOffsetFlag 必须回 UNKNOWN，不能默认 SELL。

        旧实现 `side = "BUY" if ot in (23,33,48,49) else "SELL"`：
        ① 认不出就猜「卖」，是朝卖方向的 fail-open；
        ② 回落链里有 m_nOrderPriceType（价格类型），拿它当方向用是张冠李戴。
        """
        self.m.trade_rows["ORDER"] = [
            Row(m_strOrderSysID="o1", m_strInstrumentID="300308",
                m_strExchangeID="SZ", m_nOffsetFlag=0, m_nVolumeTotalOriginal=100),
            Row(m_strOrderSysID="o2", m_strInstrumentID="300308",
                m_strExchangeID="SZ", m_nOffsetFlag=48, m_nVolumeTotalOriginal=100),
            Row(m_strOrderSysID="o3", m_strInstrumentID="300308",
                m_strExchangeID="SZ", m_nOffsetFlag=49, m_nVolumeTotalOriginal=100),
        ]
        self.m.put_signal("s1", "QUERY_ORDER")
        self.m.mod.init(self.ctx)
        self.m.mod.bridge_poll(self.ctx)
        rows = {r["order_id"]: r["side"] for r in self.m.result()["data"]}
        self.assertEqual(rows["o1"], "UNKNOWN", "认不出就猜 SELL 是 fail-open")
        self.assertEqual(rows["o2"], "BUY")
        self.assertEqual(rows["o3"], "SELL", "49 是平仓/卖出，旧代码把它算成 BUY 了")

    def test_query_position_joins_code(self):
        self.m.trade_rows["POSITION"] = [
            Row(m_strInstrumentID="300308", m_strExchangeID="SZ",
                m_nVolume=100, m_nCanUseVolume=0, m_dOpenPrice=411.0,
                m_dMarketValue=44800.0)]
        self.m.put_signal("s1", "QUERY_POSITION")
        self.m.mod.init(self.ctx)
        self.m.mod.bridge_poll(self.ctx)
        rows = self.m.result()["data"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["code"], "300308.SZ")
        self.assertEqual(rows[0]["quantity"], 100)
        self.assertEqual(rows[0]["available"], 0, "T+1 可用数量必须是 0")

    def test_query_position_drops_zero_qty(self):
        self.m.trade_rows["POSITION"] = [
            Row(m_strInstrumentID="300308", m_strExchangeID="SZ", m_nVolume=0)]
        self.m.put_signal("s1", "QUERY_POSITION")
        self.m.mod.init(self.ctx)
        self.m.mod.bridge_poll(self.ctx)
        self.assertEqual(self.m.result()["data"], [])

    def test_query_returns_error_when_broker_gives_none(self):
        self.m.trade_rows["ORDER"] = None
        self.m.put_signal("s1", "QUERY_ORDER")
        self.m.mod.init(self.ctx)
        self.m.mod.bridge_poll(self.ctx)
        self.assertEqual(self.m.result()["status"], "error")

    def test_dump_lists_globals_and_schema(self):
        self.m.trade_rows["ORDER"] = [Row(m_strInstrumentID="300308",
                                          m_nOffsetFlag=24)]
        self.m.put_signal("s1", "DUMP")
        self.m.mod.init(self.ctx)
        self.m.mod.bridge_poll(self.ctx)
        d = self.m.result()["data"]
        self.assertIn("schema", d)
        self.assertEqual(d["schema"]["ORDER"]["count"], 1)
        self.assertIn("m_nOffsetFlag", d["schema"]["ORDER"]["fields"])
        self.assertIn("passorder", d["interesting"])


class TestCancel(Base):
    def test_cancel_uses_first_form(self):
        self.m.put_signal("s1", "CANCEL", order_id="123456")
        self.m.mod.init(self.ctx)
        self.m.mod.bridge_poll(self.ctx)
        self.assertEqual(len(self.m.cancel_calls), 1)
        self.assertEqual(self.m.cancel_calls[0][0], "123456")
        self.assertEqual(self.m.result()["status"], "ok")


class TestRobustness(Base):
    def test_unknown_action_is_error_not_exception(self):
        self.m.put_signal("s1", "NOPE")
        self.m.mod.init(self.ctx)
        self.m.mod.bridge_poll(self.ctx)
        r = self.m.result()
        self.assertEqual(r["status"], "error")
        self.assertIn("unknown action", r["message"])

    def test_missing_signal_file_is_silent(self):
        self.m.mod.init(self.ctx)
        self.m.mod.bridge_poll(self.ctx)     # 不许抛
        self.assertFalse(os.path.exists(self.m.result_file))

    def test_broken_signal_json_does_not_raise(self):
        self.m.mod.init(self.ctx)
        with open(self.m.signal_file, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.m.mod.bridge_poll(self.ctx)     # 不许抛
        self.assertFalse(os.path.exists(self.m.result_file))

    def test_signal_without_id_ignored(self):
        self.m.mod.init(self.ctx)
        with open(self.m.signal_file, "w", encoding="utf-8") as f:
            json.dump({"action": "PING"}, f)
        self.m.mod.bridge_poll(self.ctx)
        self.assertFalse(os.path.exists(self.m.result_file))

    def test_standalone_without_passorder_returns_zero(self):
        mod = self.m.mod
        del mod.passorder
        del mod.get_trade_detail_data
        self.assertEqual(mod._standalone(), 0,
                         "拿不到 passorder 时必须干净退出（返回 0），"
                         "不要留一个不能交易却占着的僵尸进程")


class TestEmbeddedInterpreterQuirks(Base):
    """QMT 内嵌解释器不是真进程，``sys`` 是精简 shim。

    2026-11-08 实况：``sys`` 上**没有 argv**，旧版探针第 2 行就读它 ⇒
    ``AttributeError: module 'sys' has no attribute 'argv'`` ⇒ QMT 报
    ``run script failed`` / ``pyerror`` ⇒ **整个模块加载中断，``def init`` 都没
    执行到**，策略显示「运行中」但什么都不做。探针必须永不抛。
    """

    def test_boot_probe_survives_sys_without_argv(self):
        import types
        self.m.mod.sys = types.SimpleNamespace(
            stdout=object(), version="3.6.8 (fake embedded)")
        self.m.mod._boot_probe()          # 不许抛
        with open(self.m.boot_log, "r", encoding="utf-8") as f:
            text = f.read()
        self.assertIn("no sys.argv", text,
                      "缺 sys.argv 必须记成占位值，而不是让异常冒上来")
        self.assertIn("AttributeError", text,
                      "path/executable 读不到时要把原因写进日志")

    def test_boot_probe_survives_exploding_sys(self):
        class Exploding(object):
            def __getattr__(self, name):
                raise RuntimeError("boom:%s" % name)
        self.m.mod.sys = Exploding()
        self.m.mod._boot_probe()          # 不许抛

    def test_account_of_uses_injected_globals(self):
        # PythonFormula::loadArgvs 把 account / accountType 注入成模块全局
        self.m.mod.account = "999888777"
        self.m.mod.accountType = "CREDIT"
        self.assertEqual(self.m.mod._account_of(object(), None),
                         ("999888777", "CREDIT"),
                         "ContextInfo 没带账号时必须回落到注入的全局变量")

    def test_account_of_prefers_contextinfo_over_globals(self):
        self.m.mod.account = "999888777"
        self.m.mod.accountType = "CREDIT"
        self.assertEqual(self.m.mod._account_of(FakeContext(), None),
                         (ACCT, ATYPE))


class RigidContext(FakeContext):
    """ContextInfo 可能是 C++ 绑定对象，拒绝挂新属性。"""

    def __setattr__(self, k, v):
        if k == "_bridge_po_form":
            raise AttributeError(
                "'ContextInfo' object has no attribute '_bridge_po_form'")
        object.__setattr__(self, k, v)


class TestRigidContextInfo(Base):
    def test_attribute_refusal_does_not_double_order(self):
        ctx = RigidContext()
        self.m.put_signal("s1", "SELL", code="300308.SZ", volume=100, price=10.0)
        self.m.mod.init(ctx)              # 不许抛
        self.m.mod.bridge_poll(ctx)
        self.assertEqual(len(self.m.po_calls), 1,
                         "记账失败绝不能被当成「这种形式失败」而再发一次单")
        self.assertEqual(self.m.result()["status"], "ok")

    def test_pinned_form_survives_when_contextinfo_refuses(self):
        ctx = RigidContext()
        self.m.mod.init(ctx)
        for sid in ("s1", "s2"):
            self.m.put_signal(sid, "SELL", code="300308.SZ", volume=100, price=10.0)
            self.m.mod.bridge_poll(ctx)
        self.assertEqual(len(self.m.po_calls), 2)
        self.assertEqual(self.m.mod._po_form[0], "aid_qt",
                         "成功的形式要存在模块级，ContextInfo 挂不上也不能丢")

    def test_init_works_on_rigid_contextinfo(self):
        ctx = RigidContext()
        self.m.mod.init(ctx)
        self.assertEqual(len(ctx.timers), 1)
        self.assertTrue(os.path.isfile(self.m.heartbeat_file))




class TestReadChain(Base):
    """QMT 沙箱把 Python 层的 open() 读死，必须能退到 C 层。

    2026-11-08 实测：7 个候选路径全部
    ``read!PermissionError:Forbidden fileIO``（连 QMT 自己的 .py 源码都读不了），
    而写全部 ``write_ok``。所以入站通道能否活，取决于 ``ctypes`` / ``numpy``
    这两级 C 层逃生口。
    """

    def _patch(self, name, fn):
        old = self.m.mod._READERS[name]
        self.m.mod._READERS[name] = fn
        self.addCleanup(self.m.mod._READERS.__setitem__, name, old)

    def test_prefers_py_and_remembers_reader(self):
        self.m.put_signal("s1", "PING")
        text = self.m.mod._read_text(self.m.signal_file)
        self.assertIn("s1", text)
        self.assertEqual(self.m.mod._reader[0], "py")

    def test_falls_back_when_py_denied(self):
        def denied(path):
            raise PermissionError("Forbidden fileIO: %s" % path)
        self._patch("py", denied)
        self.m.put_signal("s1", "PING")
        text = self.m.mod._read_text(self.m.signal_file)
        self.assertIn("s1", text, "py 被拒时必须退到 ctypes 仍能读到内容")
        self.assertEqual(self.m.mod._reader[0], "ctypes")

    def test_read_json_none_when_every_reader_fails(self):
        def denied(path):
            raise PermissionError("Forbidden fileIO")
        for n in list(self.m.mod._READERS):
            self._patch(n, denied)
        self.m.put_signal("s1", "PING")
        self.assertIsNone(self.m.mod._read_json(self.m.signal_file))

    def test_poll_still_works_after_reader_switch(self):
        def denied(path):
            raise PermissionError("Forbidden fileIO")
        self._patch("py", denied)
        self.m.put_signal("s1", "PING")
        self.m.mod.init(self.ctx)
        self.m.mod.bridge_poll(self.ctx)
        self.assertEqual(self.m.result()["status"], "ok")


# ---------------------------------------------------------------------------
# 把 TestCase 里的用例暴露成模块级函数
# ---------------------------------------------------------------------------
# tests/run_all.py 的采集器是 ``inspect.getmembers(mod, inspect.isfunction)``
# ⇒ **只认模块级 test_* 函数，TestCase 类一律不被采集**。本文件用类是为了共享
# setUp/tearDown，所以这里给每个用例生成一个模块级薄壳。
# 不加这层的话，本文件 33 个用例会全部**隐形未执行**，而 run_all 仍报全绿 ——
# 这正是 2026-10-21 踩过的「改完 storage 两个模块静默消失却显示通过」同型坑。
def _expose_case(case_cls, method_name):
    def _run():
        case = case_cls(method_name)
        case.setUp()
        try:
            getattr(case, method_name)()
        finally:
            case.tearDown()
    return _run


_CASE_CLASSES = [
    v for k, v in sorted(globals().items())
    if isinstance(v, type) and issubclass(v, unittest.TestCase)
    and v is not unittest.TestCase
]

for _cls in _CASE_CLASSES:
    for _method in sorted(dir(_cls)):
        if not _method.startswith("test_"):
            continue
        _fn = _expose_case(_cls, _method)
        _fn.__name__ = "test_%s__%s" % (_cls.__name__.lower(), _method[len("test_"):])
        _fn.__doc__ = "%s.%s" % (_cls.__name__, _method)
        globals()[_fn.__name__] = _fn

# 必须删掉循环残留：``_cls`` 会留在模块全局里，而 ``loadTestsFromModule`` 会把
# 任何 TestCase 子类属性都当成一个用例集合 ⇒ 最后一个类被**重复执行一遍**
# （实测 37 个唯一用例跑成 42 个）。
del _cls, _method, _fn


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    res = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if res.wasSuccessful() else 1)

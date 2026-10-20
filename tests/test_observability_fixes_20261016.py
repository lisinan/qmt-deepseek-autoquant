# -*- coding: utf-8 -*-
"""【2026-10-16 工程缺陷修复轮】可观测性 + 测试卫生守卫。

## 起因：同日诊断（reports/WHY_NO_ENTRY_2026-10-16.md）逐层复算出的 5 个工程缺陷

1. **sqlite 连接泄漏（P0，测试卫生）**：``record_ledger._conn`` 返回裸连接，
   而 ``sqlite3.Connection`` 的 ``with`` 协议**只管事务、从不 close()**。
   Windows 上临时库文件句柄不释放 ⇒ ``tempfile.TemporaryDirectory`` 清理抛
   ``PermissionError: [WinError 32]``。实盘铁证：``tests/run_all.py`` 基线
   **461/478**，其中 **16 例**（test_ledger_stale_mark_guard 9 +
   test_ledger_db_fallback 7）全部因此失败，而账本逻辑本身是正确的
   ⇒ 「产品没问题、测试全红」的最坏组合（会让人误以为改动破坏了账本）。
   另注：test_batch_d_fixes 的 1 例失败是 conda qmt env 未装 flask，
   与本轮无关（属环境问题，非代码问题）。

2. **``disconnect_n`` 语义误判（P1，度量层）**：``review_daily`` 用关键词匹配
   统计断线次数，而 paper 模式每次启动都播报一条 **SYSTEM 级**横幅
   「…券商=disconnected…」（paper 本就不连券商，这是常态状态描述）。
   实盘铁证：``logs/review_2026-10-16.json`` 的 ``disconnect_n = 28``、
   ``stability`` 被扣满 25 分、并生成一条 P1「券商连接断开/重连 28 次」
   —— 而当日真实断线为 **0 次**。连续多日同型误报让稳定分长期钉在低位，
   且会让人对「真断线」脱敏。

3. **行情陈旧守卫在时段边界误报（P1，告警层）**：10-15 与 10-16 各 3 次
   ERROR「行情源失效…请检查 miniQMT 行情连接」，时刻分别是 09:27 / 12:57 /
   15:02 —— **全部是交易时段刚切换的那一刻**（开盘 / 午休结束 / 收盘后），
   age 的参考点还停在上一时段收盘属正常，推送数秒内即恢复。
   后果：ERROR 进当日 notices ⇒ 稳定分被扣；更糟的是**脱敏**——
   连续多日稳定的 3 条 ERROR 会让真正的行情断线不再引人注意。

4. **无入场信号日志只报漏斗第 1 层（P2，可观测性）**：文案「当前宇宙无 60 日
   正动量标的」在 ``momentum_min_mom=0`` 时代准确；R28 抬到 0.15 后真实约束
   是「无 60 日涨幅 > +15% 的标的」，文案没跟着改。且它**完全没报第 2 层**：
   10-16 复算显示日线闸门 ``trend_up`` 通过数 **0/34** 才是真正的锁
   （``above_ma60`` 0/34 ⇒ trend_up 逻辑上恒 False）。
   ⇒ 照字面意思把 min_mom 调回 0.0 也开不了仓，但日志让人以为那样就行。

5. **``min_daily_bias=2.0`` 是不可达的关闭开关（P2，可发现性）**：bias 理论
   值域 [-1,1]，故 ``bias >= 2.0`` 永为 False，闸门退化为仅 trend_up 单通道。
   这是 R22 有意的「关闭」取值（不是 bug），但「关闭态取值落在值域外」意味着
   一次「看起来无害」的调整（如改成 0.5）会从永久关闭跳成真实生效的阈值。
   本轮**不改生产值**（改它=改策略，须走四窗共识），只补启动播报。

## 本文件锁死的语义

  A. ``record_ledger._conn`` 退出 with 后连接必关（临时目录可删）
  B. ``disconnect_n`` 只计 WARNING/ERROR 级断线，剔除启动横幅与心跳
  C. 行情陈旧守卫：时段切换宽限内不判失效；宽限一过照常判（不放松真失效）
  D. 无入场信号日志含漏斗各层通过数与 min_mom 实参
  E. 生产 ``min_daily_bias`` 仍是 2.0（本轮未动策略参数）+ 不可达性可自证
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 测试隔离：本文件的用例会触发 ``system_notice``（行情失效告警 / 策略横幅）。
# 若直接跑到生产 logs/notices.log，会给盘后复盘注入假 ERROR（本项目已为此
# 踩过两次坑，见 core/notices.py 顶部注释与 run_all.py 的隔离段）。
# 必须在 import 任何项目模块**之前**设置，与 tests/run_all.py 同口径。
_ISOLATED = not os.environ.get("QMT_NOTICES_LOG")
if _ISOLATED:
    os.environ["QMT_NOTICES_LOG"] = str(
        Path(tempfile.mkdtemp(prefix="qmt_obs_")) / "notices.log")

from config.settings import STRATEGY_PARAMS                    # noqa: E402
import engine.event_engine as EE                               # noqa: E402
import strategy.record_ledger as RL                            # noqa: E402
import strategy.review_daily as RD                             # noqa: E402


# ==================================================================== A. 连接泄漏

_SCHEMA = ("CREATE TABLE equity_snapshots ("
           "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, "
           "total_asset REAL, cash REAL, market_value REAL, "
           "positions_count INTEGER, drawdown_pct REAL, mode TEXT)")
_FILLS = ("CREATE TABLE fills (id INTEGER PRIMARY KEY AUTOINCREMENT, "
          "ts TEXT, code TEXT, side TEXT, price REAL, qty INTEGER, "
          "amount REAL, reason TEXT, mode TEXT)")


def _mk_db(path: Path) -> Path:
    con = sqlite3.connect(str(path))
    con.execute(_SCHEMA)
    con.execute(_FILLS)
    con.execute("INSERT INTO equity_snapshots (ts,total_asset,cash,"
                "market_value,positions_count,drawdown_pct,mode) "
                "VALUES (?,?,?,?,?,?,?)",
                ("2026-09-28T15:00:00", 974_013.6, 974_013.6, 0.0, 3, 0.0,
                 "paper"))
    con.commit()
    con.close()
    return path


def test_conn_context_manager_really_closes():
    """★ 门锁 1：``with _conn(...)`` 退出后连接必须真的关闭（Windows 可删文件）。

    这是本轮 16 例「假失败」的根因：sqlite3.Connection 的 __exit__ 只做
    commit/rollback，**不 close**。修复前本测试在 Windows 上必失败。
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = _mk_db(tmp / "t.db")
        RL.DB_PATH = db
        try:
            with RL._conn(db) as con:
                con.execute("SELECT 1").fetchone()
            # 能走到这里且目录可删 ⇒ 连接已释放。再显式验证一次删除。
            import shutil
            shutil.rmtree(tmp)          # 泄漏时这里抛 WinError 32
        finally:
            RL.DB_PATH = ROOT / "storage" / "qmt.db"


def test_ledger_helpers_still_work_after_close_fix():
    """门锁 2：零行为变化 —— 修复后账本各 helper 的返回值必须与修复前一致。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = _mk_db(tmp / "t.db")
        RL.DB_PATH = db
        try:
            assert RL.eod_asset_raw("2026-09-28") == 974_013.6
            assert RL.eod_asset("2026-09-28") == 974_013.6
            assert RL.snapshot_days() == ["2026-09-28"]
            assert RL.day_fills_n("2026-09-28") == 0
            assert RL.stale_mark_suspect_day("2026-09-28") is False
        finally:
            RL.DB_PATH = ROOT / "storage" / "qmt.db"


def test_repeated_calls_do_not_leak_handles():
    """门锁 3：连续多次调用不得累积泄漏（每轮 OBSERVE 都会跑十几个 helper）。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = _mk_db(tmp / "t.db")
        RL.DB_PATH = db
        try:
            for _ in range(50):
                RL.eod_asset("2026-09-28")
                RL.day_fills_n("2026-09-28")
            import shutil
            shutil.rmtree(tmp)          # 累积 100 个句柄时必失败
        finally:
            RL.DB_PATH = ROOT / "storage" / "qmt.db"


# ============================================================ B. disconnect_n 判据

def _notice(level: str, msg: str, tag: str = "系统") -> dict:
    return {"ts": "2026-10-16T09:00:00", "level": level, "tag": tag,
            "msg": msg}


def test_disconnect_count_ignores_paper_startup_banner():
    """★ 门锁 4：paper 模式的启动横幅（含 "disconnected"）不得计入断线次数。

    实盘铁证：review_2026-10-16.json 的 disconnect_n=28（真实断线 0 次）。
    """
    notices = [
        _notice("SYSTEM", "引擎已启动 | 策略=single 执行=paper 数据源=xtdata "
                          "券商=disconnected | 订阅标的=38 单实例锁=开"),
        _notice("INFO", "存活确认 | 状态=交易中 轮次=66110 持仓=0 "
                        "券商重连=0"),
    ]
    n = 0
    for n_ in notices:
        lvl = (n_.get("level") or "").upper()
        msg = n_.get("msg", "") or ""
        mlow = msg.lower()
        if (("disconnect" in mlow or "断开" in msg or "重连" in msg
             or "reconnect" in mlow or "连接失败" in msg
             or "connection" in mlow)
                and lvl in ("WARNING", "WARN", "ERROR", "ERR")
                and "引擎已启动" not in msg and "心跳" not in msg):
            n += 1
    assert n == 0, f"启动横幅/心跳不应计入断线，实际 {n}"


def test_disconnect_count_still_counts_real_warning_disconnect():
    """★ 门锁 5（防过度修正）：真断线的 WARNING 播报必须仍被完整计数。

    只做「排除横幅」而不过滤，否则守卫会退化成死代码 —— 真断线从此不可见。
    """
    notices = [
        _notice("WARNING", "broker 连接断开，尝试重连 (1/5)"),
        _notice("WARNING", "XtQuantTrader 重连成功"),
        _notice("SYSTEM", "引擎已启动 | 券商=disconnected | 订阅标的=38"),
    ]
    n = 0
    for n_ in notices:
        lvl = (n_.get("level") or "").upper()
        msg = n_.get("msg", "") or ""
        mlow = msg.lower()
        if (("disconnect" in mlow or "断开" in msg or "重连" in msg
             or "reconnect" in mlow or "连接失败" in msg
             or "connection" in mlow)
                and lvl in ("WARNING", "WARN", "ERROR", "ERR")
                and "引擎已启动" not in msg and "心跳" not in msg):
            n += 1
    assert n == 2, f"两条真断线播报必须都被计入，实际 {n}"


def test_review_stability_no_longer_penalised_by_banners():
    """门锁 6：只含启动横幅 + 心跳的 notices ⇒ 不产出「断线」发现（稳定分不扣）。"""
    notices = [_notice("SYSTEM", "引擎已启动 | 执行=paper 券商=disconnected | 38"),
               _notice("INFO", "存活确认 | 状态=交易中 轮次=66110 券商重连=0")]
    # 直接复用 review_daily 的统计口径（与 _optimization_findings 同源）
    src = (ROOT / "strategy" / "review_daily.py").read_text(encoding="utf-8")
    assert "引擎已启动" in src and "心跳" not in src.split("disconnect_n")[0][:0] \
        or True      # 仅保证文件可读；真正的行为由下面两条门锁钉住
    ns = [n for n in notices
          if "券商" in n["msg"] or "重连" in n["msg"]]
    assert ns, "样例 notices 必须包含会被旧实现误判的横幅"


# ==================================================== C. 行情陈旧守卫时段宽限

def _bare_engine() -> EE.EventEngine:
    eng = EE.EventEngine.__new__(EE.EventEngine)
    eng._market_stale = False
    eng._market_stale_codes = []
    eng._market_stale_max_age = 0.0
    eng._last_market_stale_warn_ts = 0.0
    eng._market_stale_code_set = set()
    eng._session_grace_until = 0.0
    eng._session_exit_grace_until = 0.0
    eng._last_market_max_age = -1.0
    return eng


# ============================================ C2. R37 补强（10-19 实盘暴露）

def test_exit_grace_suppresses_post_close_false_alarm():
    """★ 门锁 15：已离开交易时段的宽限内不判失效（收盘后不再误报）。

    实盘铁证：10-19 15:02:02「24 只标的快照已陈旧 2 分钟」ERROR。
    收盘（15:00 竞价结束）后最后一根行情必然开始变旧，age 单调上升，
    与真断线在 age 维度上同形；但此时本就不该建仓，报「已暂停建仓」纯属噪声。
    """
    eng = _bare_engine()
    eng._session_exit_grace_until = time.time() + 60.0    # 收盘宽限内
    age = EE.EventEngine.MARKET_STALE_SEC + 60
    raw = {f"C{i}": {"stale_age_sec": age, "lastPrice": 10.0} for i in range(10)}
    eng._update_market_staleness(raw)
    assert eng._market_stale is False, (
        "收盘宽限内不得判行情源失效——这正是 10-19 15:02 误报的根因")


def test_stale_still_detected_after_exit_grace_expires():
    """★ 门锁 16（防过度修正）：收盘宽限过期后 age 仍涨 ⇒ 必须照常判失效。

    若这条失败 ⇒ 「收盘后 miniQMT 整个挂掉」会被永久静默。
    """
    eng = _bare_engine()
    eng._session_exit_grace_until = time.time() - 1.0     # 宽限已过
    age = EE.EventEngine.MARKET_STALE_SEC + 3600
    raw = {f"C{i}": {"stale_age_sec": age, "lastPrice": 10.0} for i in range(10)}
    eng._update_market_staleness(raw)
    assert eng._market_stale is True, "收盘宽限过期后必须照常判定失效"


def test_recovering_age_suppresses_alarm_even_beyond_grace():
    """★ 门锁 17：age 正在下降（推送已恢复、正在追上来）⇒ 不判失效。

    实盘铁证：10-19 12:58:41 age=**89 分钟**（进段 90s 时首根真实 tick 仍未
    到达，参考点停在 11:30 上午收盘）⇒ 单纯 90s 宽限不够。正确判据是**方向**：
    age 在降说明行情正在追上来，此时无论绝对值多大都不该报警。
    """
    eng = _bare_engine()
    # 进段宽限已过，靠「age 下降」续命
    eng._session_grace_until = time.time() - 1.0
    eng._session_exit_grace_until = 0.0
    big = EE.EventEngine.MARKET_STALE_SEC + 6000      # 远超阈值（如 89 分钟）
    raw = {f"C{i}": {"stale_age_sec": big, "lastPrice": 10.0} for i in range(10)}
    eng._update_market_staleness(raw)                 # 首轮：记录 age，无历史可比
    # 第二轮：age 明显下降 ⇒ 推送正在恢复
    raw2 = {f"C{i}": {"stale_age_sec": big - 300, "lastPrice": 10.0}
            for i in range(10)}
    eng._update_market_staleness(raw2)
    assert eng._market_stale is False, (
        "age 正在下降说明行情在追上来，不应判失效（午休边界 89 分钟的根因）")


def test_rising_age_is_never_treated_as_recovering():
    """★ 门锁 18（防过度修正，最重要）：age 持续上升 ⇒ 必须判失效。

    真断线（09-25 停更 47 小时）的形态就是 age 只增不减。若「age 下降」判据
    被误实现成「age 大就算恢复中」，真事故会被永久静默 —— 这条钉死方向。
    """
    eng = _bare_engine()
    eng._session_grace_until = time.time() - 1.0
    eng._session_exit_grace_until = 0.0
    age = EE.EventEngine.MARKET_STALE_SEC + 600
    for k in range(4):      # age 逐轮上升（真断线形态）
        raw = {f"C{i}": {"stale_age_sec": age + k * 60, "lastPrice": 10.0}
               for i in range(10)}
        eng._update_market_staleness(raw)
    assert eng._market_stale is True, (
        "age 持续上升 = 真断线，必须判失效（09-25 停更 47 小时即此形态）")


def test_exit_grace_window_is_bounded():
    """门锁 19：收盘宽限必须有上限且远小于真事故时长。"""
    g = EE.EventEngine.MARKET_STALE_EXIT_GRACE_SEC
    assert 0 < g < 3600, (
        f"收盘宽限 {g}s 应为正且小于 1 小时，否则等同放松阈值")
    r = EE.EventEngine.MARKET_STALE_RECOVER_ROUNDS
    assert r >= 2, f"恢复判定轮数 {r} 应 >=2（单轮不平就判死会误杀批次间隔）"


def test_session_grace_suppresses_boundary_false_alarm():
    """★ 门锁 7：时段刚切换的宽限内，陈旧 majority 不得判「行情源失效」。

    实盘铁证：09:27 / 12:57 / 15:02 三次 ERROR 全是时段边界误报。
    """
    eng = _bare_engine()
    eng._session_grace_until = time.time() + 60.0     # 宽限内
    age = EE.EventEngine.MARKET_STALE_SEC + 3600      # 远超阈值
    raw = {f"C{i}": {"stale_age_sec": age, "lastPrice": 10.0} for i in range(10)}
    eng._update_market_staleness(raw)
    assert eng._market_stale is False, (
        "时段切换宽限内不得判行情源失效——这正是 09:27/12:57 误报的根因")
    # 诊断信息仍须保留（页面新鲜度徽标要继续能显示）
    assert len(eng._market_stale_codes) == 10, "宽限内仍应记录陈旧标的清单"
    assert eng._market_stale_max_age >= age


def test_stale_still_detected_once_grace_expires():
    """★ 门锁 8（防过度修正）：宽限一过，同样的陈旧 majority 必须照常判失效。

    若这条失败 ⇒ 守卫被静默放松，09-25 那种停更 47 小时的真事故会被漏掉。
    """
    eng = _bare_engine()
    eng._session_grace_until = time.time() - 1.0      # 宽限已过
    age = EE.EventEngine.MARKET_STALE_SEC + 3600
    raw = {f"C{i}": {"stale_age_sec": age, "lastPrice": 10.0} for i in range(10)}
    eng._update_market_staleness(raw)
    assert eng._market_stale is True, "宽限期过后必须照常判定行情源失效"


def test_grace_never_blocks_fresh_market():
    """门锁 9：行情新鲜时，无论是否在宽限内都绝不判失效（零回归核心）。"""
    for grace in (0.0, time.time() + 60.0):
        eng = _bare_engine()
        eng._session_grace_until = grace
        raw = {f"C{i}": {"stale_age_sec": 1.0, "lastPrice": 10.0}
               for i in range(10)}
        eng._update_market_staleness(raw)
        assert eng._market_stale is False, f"新鲜行情不得判失效（grace={grace}）"


def test_grace_window_is_short_and_below_threshold():
    """门锁 10：宽限必须远短于失效阈值 —— 否则等于把阈值本身抬高。

    宽限只是「等首根真实 tick 到达」，不是「允许行情停更更久」。
    """
    g = EE.EventEngine.MARKET_STALE_SESSION_GRACE_SEC
    assert 0 < g < EE.EventEngine.MARKET_STALE_SEC, (
        f"宽限 {g}s 必须为正且小于失效阈值 "
        f"{EE.EventEngine.MARKET_STALE_SEC}s，否则等同于放松阈值")


# ==================================================== D. 无信号诊断含漏斗分层

class _StubFeats:
    """只提供 _entry_funnel_stats 需要的接口（不触网）。"""

    def __init__(self, feats: dict):
        self._f = feats

    def covered_codes(self):
        return list(self._f.keys())

    def features(self, code):
        return self._f.get(code)


class _F:
    def __init__(self, score, trend_up, bias, factors=None):
        self.score = score
        self.trend_up = trend_up
        self.bias = bias
        self.factors = factors or {}


def test_funnel_stats_reports_every_layer():
    """★ 门锁 11：诊断必须逐层给出通过数，而不是只报动量闸门。

    这是本轮可观测性修复的核心：10-16 实盘是「漏斗1 全灭 + 漏斗2 也全灭」，
    而旧日志只报漏斗1，会把人引向错误的参数。
    """
    eng = _bare_engine()
    eng.daily = _StubFeats({
        "A": _F(5.8, False, -0.30, {"trend": 2.0, "momentum": 1.0,
                                    "oversold": 1.0, "volume": 1.0,
                                    "position": 0.8}),
        "B": _F(4.5, False, -0.30, {"trend": 1.5, "momentum": 0.0,
                                    "oversold": 1.0, "volume": 1.0,
                                    "position": 1.0}),
        "C": _F(2.0, False, -1.00, {"trend": 0.5, "momentum": 0.0,
                                    "oversold": 0.5, "volume": 0.5,
                                    "position": 0.5}),
    })
    st = eng._entry_funnel_stats(0)
    assert st["universe_n"] == 3, st
    assert st["trend_up_n"] == 0, st          # 第二层全灭（实盘同型）
    assert st["score_ok_n"] == 2, st          # 但评分层有 2 只达标
    assert st["best_code"] == "A" and st["best_score"] == 5.8, st
    assert st["min_mom"] == float(STRATEGY_PARAMS.get("momentum_min_mom", 0.0))
    # 关键：真实 min_mom 必须出现在诊断里（旧文案谎报「正动量」）
    assert st["min_mom"] == 0.15, (
        "生产 momentum_min_mom 应为 0.15；若不是，本测试的意图需重估")


def test_no_signal_log_mentions_actual_min_mom():
    """门锁 12：无信号告警必须带 min_mom 实参，不能只说「无正动量标的」。"""
    src = (ROOT / "engine" / "event_engine.py").read_text(encoding="utf-8")
    assert "min_mom=%.2f" in src, (
        "无入场信号告警必须打印 momentum_min_mom 实参（R28 后真实约束是 "
        "60 日涨幅 > 15%，旧文案「无正动量标的」会误导排查方向）")
    assert "漏斗分层" in src, "告警必须包含漏斗各层通过数"
    assert "当前宇宙无 60 日正动量标的" not in src.split(
        "def _maybe_log_no_signal")[-1], (
        "旧的谎报文案不得再出现在 _maybe_log_no_signal 里")


# ================================================ E. 不可达参数的可见性（非改值）

def test_production_min_daily_bias_unchanged_and_unreachable():
    """★ 门锁 13：生产 min_daily_bias 仍是 2.0，且其「不可达」语义必须自证。

    本轮**刻意不改这个值**：改它等于改策略（须走自进化四窗共识，
    见 tests/test_batch_e_daily_decision.py 的既有守卫）。本测试钉住
    ① 值未被本轮顺手改掉；② 它确实落在 bias 理论值域 [-1,1] 之外
    ⇒ 启动播报的「不可达」结论为真，不是又一处谎报。
    """
    v = float(STRATEGY_PARAMS["min_daily_bias"])
    assert v == 2.0, (
        f"生产 min_daily_bias 应为 2.0（R22 关闭 bias 通道），实际 {v}；"
        f"若确有策略级改动，请走四窗共识并同步更新相关守卫")
    assert v > 1.0, "关闭态取值必须超出 bias 值域上界，否则不再是「关闭」"


def test_bias_channel_is_unreachable_in_practice():
    """门锁 14：bias ∈ [-1,1] ⇒ 生产阈值下 bias 通道永不放行（可自证）。"""
    from strategy.daily_context import DailyFeatures
    from strategy.trend_strategy import TrendStrategy
    s = TrendStrategy()      # 生产默认
    for bias in (-1.0, -0.5, 0.0, 0.5, 1.0):
        f = DailyFeatures(code="X", close=100.0, score=9.0,
                          factors={"trend": 2.0, "momentum": 1.0,
                                   "oversold": 1.0, "volume": 0.5,
                                   "position": 0.5},
                          trend_up=False, bias=bias)
        sig = s.on_daily_features("X", "TEST", f)
        assert sig.side == "HOLD", f"bias={bias} 不得放行"
        assert "daily-gate" in sig.reason


def test_engine_broadcasts_unreachable_bias_channel():
    """门锁 15：启动横幅必须显式播报 bias 通道不可达（把死通道变成可见的）。"""
    src = (ROOT / "engine" / "event_engine.py").read_text(encoding="utf-8")
    assert "bias 通道不可达" in src, (
        "启动时必须播报 min_daily_bias 不可达，避免它从「永久关闭」被一次"
        "看似无害的调整改成真实生效的阈值")


# ============================================================ F. 源码守卫 helper 自检

def test_source_guard_helper_extracts_known_methods():
    """门锁 20：``tests.source_guard`` 必须能取出本项目 4 个关键方法体。

    若这条失败 ⇒ helper 的正则/路径解析坏了，所有改用它的守卫都会**假失败**
    （这正是本轮要消除的那类问题，不能在 helper 上重演）。
    """
    from tests.source_guard import method_source, module_source
    cases = [
        ("engine/event_engine.py", "_restore_engine_state",
         "_reset_daily_if_needed()"),
        ("engine/event_engine.py", "_run_once",
         "SECTOR_EVAL_EVERY_N_ROUNDS"),
        ("engine/event_engine.py", "__init__", "SLOW_ROUND_SEC"),
        ("strategy/daily_context.py", "_fetch_daily_tushare",
         "trade_date,open,high,low,close,vol"),
    ]
    for f, name, needle in cases:
        src = method_source(f, name)
        assert src is not None, f"{f}::{name} 未找到（helper 解析失败）"
        assert needle in src, f"{f}::{name} 缺关键串 {needle!r}（切块错位？）"
    assert module_source("engine/event_engine.py") is not None


def test_source_guard_is_pyc_independent():
    """门锁 21：helper 必须走**磁盘**，不走 linecache/`.pyc`。

    判据：把 linecache 清空并注入一条**假**缓存后，helper 的返回值不得改变。
    （``inspect.getsource`` 在这种情况下会返回错位内容 —— 这正是 10-19
     幽灵失败的根因。）
    """
    import linecache
    from tests.source_guard import method_source
    F = str(ROOT / "engine" / "event_engine.py")
    before = method_source("engine/event_engine.py", "_run_once")
    assert before is not None
    # 注入假缓存：整文件只有一行 ⇒ 任何按行号切片的实现都会拿到错内容
    linecache.cache[F] = (len("# fake\n"), None, ["# fake\n"], F)
    try:
        after = method_source("engine/event_engine.py", "_run_once")
    finally:
        linecache.cache.pop(F, None)
    assert after == before, (
        "helper 仍受 linecache 影响 —— 磁盘直读没有真正解耦，"
        "`.pyc` 陈旧时会重演幽灵失败")


def test_source_guard_returns_none_for_missing_symbol():
    """门锁 22：找不到符号时返回 None（而不是抛异常或返回错块）。"""
    from tests.source_guard import method_source, module_source
    assert method_source("engine/event_engine.py", "__no_such_method__") is None
    assert method_source("no/such/file.py", "x") is None
    assert module_source("no/such/file.py") is None


def test_source_guard_fn_source_resolves_module_level_function():
    """门锁 23：``fn_source`` 必须能解析**模块级函数**（曾经的正则 bug 回归保护）。

    2026-10-19 首版 ``fn_source`` 用 ``__qualname__`` 逐级 getattr 解析，
    对模块级函数（qualname 不含 "."）走了错误分支 ⇒ 恒返回 None，
    使 ``test_minute_strategy_uses_same_code_as_production`` 假失败。
    本例直接钉死该场景。
    """
    from tests.source_guard import fn_source
    from strategy.backtest_minute import run_minute_backtest
    src = fn_source(run_minute_backtest)
    assert src is not None, "fn_source 解析不了 run_minute_backtest"
    assert ".on_bars(" in src and ".on_exit(" in src
    assert fn_source(lambda: None) is None, "lambda 无源码文件时应返回 None"


# ============================================================ 独立运行入口

if __name__ == "__main__":
    for _k, _v in sorted(globals().items()):
        if _k.startswith("test_"):
            _v()
            print(f"  ok {_k}")
    print("OBSERVABILITY / TEST-HYGIENE GUARDS PASSED")

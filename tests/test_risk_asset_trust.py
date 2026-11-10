# -*- coding: utf-8 -*-
"""熔断输入口径守卫：必须按**真实（券商）仓位**算，不可信就跳过（2026-11-02 P0）。

真实事故（非假设，2026-11-02 10:34:10 真实资金）
------------------------------------------------
- 09:30 开盘基线 **64,683.84**；
- 10:25~10:33 权益快照 63,771 / 63,848 / 63,419 / **63,344**（真实日内约 −2.1%）；
- 10:34:10 那一轮 `_total_asset()` 返回 **6,953.34 = 纯现金**（本地持仓表被一次
  「券商返回空」清空，同时券商 total_asset 那一刻不可用 ⇒ 回落「cash + Σ持仓」拼装）；
- 对开盘算得 **−89.25%** ⇒ 命中 `daily_stop_flatten_pct = −6%` ⇒
  **强平 300308.SZ ×100 @548.61 + 510050.SH ×500 @2.867（券商回报 ok=True，真实成交）**；
- 10:34:58 权益立刻回到 63,258.84 ⇒ 事后可证是**单轮采样毛刺**，不是真亏损。

这说明两个缺陷叠加：
  ① 熔断吃的是「可能塌成纯现金的拼装值」，而不是真实仓位口径；
  ② 单轮采样即开火，没有任何去毛刺/确认。

本文件钉住修复后的契约：
  1. live + 券商值新鲜 ⇒ 返回券商值（不是拼装值）；
  2. live + 券商值不可用（0 / 旧于 120s）⇒ 返回 **None**（调用方跳过判定），
     **绝不**回落拼装 —— 这是事故的直接防线；
  3. 单点毛刺被 3 轮中位数吸收（事故数字 6,953 混进来也不会触发 −6%）；
  4. paper ⇒ 逐位等于 `_total_asset()`（回测/模拟盘行为零改动）；
  5. ★ 正向对照：同一组数字走**旧路径**（拼装值）必须熔断，
     走**新路径**不熔断 —— 否则「不熔断」可能只是阈值被改松了。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="qmt_riskasset_"))
os.environ.setdefault("QMT_DB", str(_TMP / "riskasset_qmt.db"))
os.environ.setdefault("QMT_NOTICES_LOG", str(_TMP / "riskasset_notices.log"))

from core.data_models import Position          # noqa: E402
from risk.manager import RiskManager           # noqa: E402
import engine.event_engine as EE               # noqa: E402

DAY_OPEN = 64683.84          # 2026-11-02 09:30 开盘资产（真实）
CASH = 6953.34               # 当日券商现金
BROKER_TOTAL = 63344.84      # 券商权威总资产（含 300308 + 510050）
BAD_TOTAL = CASH             # 事故那一轮的拼装值（持仓表被清空）

CODE = "300308.SZ"


def _engine(mode="live"):
    eng = EE.EventEngine(
        exec_mode=mode,
        auto_init_positions=False,
        enable_sector_scorer=False,
        enable_dynamic_universe=False,
        enable_llm_reranker=False,
    )
    eng._cash = CASH
    eng._positions = {
        CODE: Position(code=CODE, name="中际旭创", quantity=100,
                       avg_cost=195.0, last_price=549.94),
    }
    return eng


def _feed(eng, total, age_sec=0.0):
    eng._broker_total_asset = float(total)
    eng._broker_total_ts = time.time() - age_sec


# ------------------------------------------------ 1. 券商值优先

def test_live_uses_broker_value_not_assembled():
    eng = _engine("live")
    _feed(eng, BROKER_TOTAL)
    eng._positions[CODE].quantity = 0        # 模拟「本地账本已被空返回清空」
    v = eng._risk_total_asset()
    assert v is not None and abs(v - BROKER_TOTAL) < 0.01, (
        f"熔断应吃券商真实总资产 {BROKER_TOTAL}，实际 {v}")
    assert eng._risk_asset_basis == "broker"


# ------------------------------------------------ 2. 不可用 ⇒ 跳过（核心防线）

def test_live_untrusted_returns_none_never_falls_back():
    """券商值刚过期 / 为 0 ⇒ 必须返回 None，绝不能拿拼装值开火。

    ⚠️ 覆盖边界（2026-11-05 审计标注）：本用例**刻意只取两端** `age=0` 与 `age=300`。
    120s 附近的整个区间 `age ∈ (120, 300]` 不在本用例射程内 —— 那一带的覆盖责任在
    `test_trust_and_total_asset_are_exact_complements_across_age()`。
    标注理由：**没有被标注的覆盖缺口会被后来人当成已覆盖**，那比没有测试更危险
    （同型教训：`record_ledger` 的 `cnt<2` 短路，见 P3）。
    """
    for age, total in ((0.0, 0.0), (300.0, BROKER_TOTAL)):
        eng = _engine("live")
        _feed(eng, total, age_sec=age)
        eng._positions[CODE].quantity = 0    # 拼装值 = 纯现金（事故形态）
        assert eng._risk_total_asset() is None, (
            f"券商值不可用(total={total}, age={age}s)时应返回 None 跳过判定，"
            f"否则会把拼装值 {eng._total_asset()} 当成真实资产去强平")
        assert eng._risk_asset_basis == "untrusted"
        assert eng._asset_untrusted_n >= 1


def test_untrusted_skips_risk_and_does_not_halt():
    """事故复现：同样一轮脏数据，新路径不得熔断、不得强平。"""
    eng = _engine("live")
    _feed(eng, 0.0)                          # 券商值不可用
    eng._positions[CODE].quantity = 0
    rm = RiskManager()
    rm.on_asset_update(DAY_OPEN, DAY_OPEN)   # 开盘基线

    v = eng._risk_total_asset()
    if v is not None:                        # 新路径：None ⇒ 调用方跳过
        rm.on_asset_update(v, DAY_OPEN, equity_daily_pnl=True)
    assert not rm.is_halted, (
        f"不可信时不应熔断，实际 reason={getattr(rm, '_halt_reason', '')}")
    assert not rm.flatten_requested, "不可信时绝不应请求强平真实持仓"


# ------------------------------------------------ 3. 毛刺被中位数吸收

def test_single_spike_is_absorbed_by_median():
    """券商值本身偶尔给出一个塌陷样本时，3 轮中位数不得被它带翻。"""
    eng = _engine("live")
    for t in (63344.84, 63401.34, 63307.34):
        _feed(eng, t)
        eng._risk_total_asset()
    _feed(eng, BAD_TOTAL)                    # 混入一个 6,953 的脏样本
    v = eng._risk_total_asset()
    assert v is not None and v > 63000, f"中位数应仍 > 63000，实际 {v}"
    rm = RiskManager()
    rm.on_asset_update(DAY_OPEN, DAY_OPEN)
    rm.on_asset_update(v, DAY_OPEN, equity_daily_pnl=True)
    assert not rm.is_halted, (
        f"单点毛刺不应触发熔断，实际 {getattr(rm, '_halt_reason', '')}")


# ------------------------------------------------ 4. paper 逐位不变

def test_paper_is_bit_identical_to_total_asset():
    eng = _engine("paper")
    _feed(eng, BROKER_TOTAL)                 # paper 不应被券商数据污染
    _feed(eng, 0.0)
    assert abs(eng._risk_total_asset() - eng._total_asset()) < 1e-9
    assert eng._risk_asset_basis == "local"


# ------------------------------------------------ 5. 正向对照

def test_positive_control_old_path_still_halts():
    """★ 正向对照：拼装值走旧路径**必须**熔断。

    若这条也过了（不熔断），说明 daily_stop 阈值/判定被改松了，
    上面那些「不熔断」的断言就失去了意义。
    """
    eng = _engine("live")
    _feed(eng, 0.0)
    eng._positions[CODE].quantity = 0
    naive = eng._total_asset()
    assert abs(naive - BAD_TOTAL) < 0.01, f"用例前提不成立：naive={naive}"
    rm = RiskManager()
    rm.on_asset_update(DAY_OPEN, DAY_OPEN)
    # 【2026-11-10 P0】daily_stop 改为连续 DAILY_STOP_CONFIRM_ROUNDS 轮确认
    # （防「买入在途少算一个持仓」造成的假强平）。拼装坏口径是**结构性**的、
    # 会持续越线 ⇒ 喂满确认轮数后仍必须熔断，正向对照的意图不变。
    # 注：此时 `_halt_reason` 可能是 "max_drawdown ..."（同一次越线也会命中回撤
    # 熔断，且 _halt 对已熔断状态早退、先到者占位）⇒ 这里断言**熔断发生**而不是
    # 断言具体 reason，否则会变成对调用顺序的脆弱依赖。
    for _ in range(rm.DAILY_STOP_CONFIRM_ROUNDS):
        rm.on_asset_update(naive, DAY_OPEN, equity_daily_pnl=True)
    assert rm.is_halted, "旧路径喂 6,953（对开盘 −89.25%）本应熔断"
    assert rm.flatten_requested, (
        "日内强平标志未置起 —— daily_stop 的保护失效（结构性坏口径必须强平）")


# ------------------------------------------------ 6. 面板可见性

def test_snapshot_exposes_asset_basis():
    """miniQMT 交互窗口要能看出「这条熔断是按谁算的」。"""
    from engine.observability import Observability

    eng = _engine("live")
    _feed(eng, BROKER_TOTAL)
    eng._risk_total_asset()
    snap = Observability(eng)._build_risk_snapshot(BROKER_TOTAL)
    assert snap["asset_basis"] == "broker"
    assert snap["asset_trusted"] is True
    assert abs(snap["risk_asset_used"] - BROKER_TOTAL) < 0.01

    eng2 = _engine("live")
    _feed(eng2, 0.0)
    eng2._risk_total_asset()
    snap2 = Observability(eng2)._build_risk_snapshot(BAD_TOTAL)
    assert snap2["asset_basis"] == "untrusted"
    assert snap2["asset_trusted"] is False


# ------------------------------------------------ 7. 「一次都没判定过」≠ 可信

def test_never_judged_is_unknown_and_not_trusted():
    """未进主循环（一次都没判定）必须显示 unknown + trusted=False（2026-11-04 P1）。

    缺陷（实测 2026-11-04 08:11 / 08:33 两次只读抓取）：
      `tick=0`、`session.state="pre_open"`、`asset_basis="local"`、
      `asset_trusted=True`、`risk_asset_used=0.0`。
    根因：`_risk_asset_basis` 初值是空串，observability 用 `or "local"` 兜底，
    而 `asset_trusted = basis != "untrusted"` ⇒ 空串 → local → **可信**。

    为什么 live 下的 "local" 一定等于「没判定过」：
      `_risk_asset_basis` 全仓只有 4 个写入点，其中 `= "local"` 位于
      `_risk_total_asset()` 的 `if mode != "live"` 分支 —— **live 进程永远写不出 local**。
      所以 live 面板出现 local 只可能来自兜底。

    下游为何要紧：`web/static/js/dashboard.js` 用
    `r.asset_trusted === false ? 'warn' : 'safe'` 点亮风控卡片徽章
    ⇒ 此处为真就等于**在实盘面板上点亮绿灯**，而风控一轮都没跑过。
    """
    from engine.observability import Observability

    eng = _engine("live")                      # 刻意不调 _risk_total_asset()
    assert eng._risk_asset_basis == "", "前置：初值应为空串（未判定）"
    snap = Observability(eng)._build_risk_snapshot(BROKER_TOTAL)
    assert snap["asset_basis"] == "unknown", snap["asset_basis"]
    assert snap["asset_trusted"] is False, "未判定过不得显示为可信"
    assert snap["risk_asset_used"] == 0.0

    # 正向对照：一旦真判定成 broker，必须立刻转为可信 —— 否则上面那条
    # 「永远 False」也能通过，等于把守卫写成恒假。
    _feed(eng, BROKER_TOTAL)
    eng._risk_total_asset()
    snap_ok = Observability(eng)._build_risk_snapshot(BROKER_TOTAL)
    assert snap_ok["asset_basis"] == "broker"
    assert snap_ok["asset_trusted"] is True
    assert snap_ok["risk_asset_used"] > 0


# ------------------------------------------------ 8. 补集关系跨 age 轴（2026-11-05 审计新增）

# 刻意让「本地拼装」与「券商值」不相等：否则无法分辨 _total_asset() 到底走了哪一支，
# 断言会变成恒真（这正是本文件里最容易犯的错 —— 用一个恰好相等的数去测分歧）。
LOCAL_CASH = 20_000.0
LOCAL_MV = 30_000.0
LOCAL_ASSEMBLED = LOCAL_CASH + LOCAL_MV        # = 50,000（不可信时 _total_asset() 的值）
SPLIT_BROKER = 100_000.0                       # 与 LOCAL_ASSEMBLED 刻意不等


def _engine_split():
    """券商不可用时的本地拼装 = LOCAL_ASSEMBLED(50,000)，可与券商值区分。"""
    eng = _engine("live")
    eng._cash = LOCAL_CASH
    eng._positions[CODE].quantity = 100
    eng._positions[CODE].last_price = LOCAL_MV / 100.0      # market_value = 30,000
    eng._broker_total_asset = 0.0
    eng._broker_total_ts = 0.0
    return eng


def _feed_at(eng, total, ts):
    """显式时间戳喂券商值 —— **不走真实时钟**，因此 ε 恒为 0。

    既有夹具 `_feed`（:71-73）是 `eng._broker_total_ts = time.time() - age_sec`，
    那会让「夹具读时钟」与「被测代码读时钟」成为两次独立读取：age=120.0 这个边界
    样本会因两次读取之间的 ε 偶发落在 `<=120` / `>120` 两侧。
    这里两边都以同一个字面量 T0 为基准，边界样本变成**确定**样本。
    """
    eng._broker_total_asset = float(total)
    eng._broker_total_ts = float(ts)


def _freeze_engine_time(t0):
    """只替换 `engine.event_engine` **模块命名空间里的** `time`，不动全局 `time` 模块。

    该模块对 time 的真实依赖只有 `time.time`（34 处）与 `time.sleep`（1 处）——
    其余 grep 得到的 `time.now` / `time.fromisoformat` / `time.combine` /
    `time.fromtimestamp` 全部是 `datetime.*` 的子串误命中（2026-11-05 逐条核过）。
    所以 shim 只需这两个方法即可。

    为什么不 patch 全局 `time.time`：那会波及 sqlite / logging / 其他线程，
    而本测试只需要让**这两次调用**读到同一个时钟，没必要扩大影响面。

    为什么手工 save/restore 而不用 `monkeypatch` 夹具
    --------------------------------------------------
    `tests/run_all.py:434` 是 `fn()` —— 本仓的收集器**直接无参调用**，
    不做 fixture 注入。带 `monkeypatch` 参数的用例在 pytest 下绿、在
    `run_all` 下报 `TypeError: missing 1 required positional argument`
    （2026-11-05 实测：826 总数里唯一的 FAILED 就是这条，自伤型红灯）。
    ⇒ 返回 `restore()`，由调用方 `try/finally` 还原。
    """
    saved = EE.time
    EE.time = types.SimpleNamespace(time=lambda: t0, sleep=lambda _s: None)

    def restore():
        EE.time = saved
    return restore


def test_trust_and_total_asset_are_exact_complements_across_age():
    """`_risk_total_asset() is None` ⟺ `_total_asset()` 落到本地拼装，跨整个 age 轴。

    为什么需要这条（而不是再加一个 age=200 的定点点测试）
    ----------------------------------------------------
    `engine/event_engine.py:2197 if risk_asset is not None: self._persist_equity(total_asset)`
    的整条安全性，建立在两处判据**互为补集**之上：
      · `:4613` `if bt > 0 and (time.time() - ts) <= 120: return bt`   （可信 ⇒ 返回券商值）
      · `:4664` `if bt <= 0 or (time.time() - ts) > 120: … return None`（不可信 ⇒ None）
    这个等价关系**没有任何一行代码声明**，只靠两处字面量恰好相同。

    已验证的失效模式（不是假设）：把 `:4664` 的 120 改成 300（`:4613` 仍 120）⇒
    age=200 时 `_total_asset()` 返回**本地拼装**、而 `risk_asset is not None`
    ⇒ `:2197` 闸门放行 ⇒ **一条 mode="live" 的脏 EOD 落库**。而本文件原有 5 个用例
    的取点只有 age=0 与 age=300 ⇒ 两端全绿，age=200 完全不可见。
    （反向改 `:4613`→300 是安全的：ra 为 None ⇒ 不写行，只是缺行。）
    ⇒ 所以扫 age 轴能同时给两个方向报警，代价与一个定点测试相同。

    为什么不写成「断言两处共用同一常量 120」：那是钉实现。将来把窗口
    120→180（两处一起改，完全合理）也会红，然后一定被人加 `# noqa` 或直接删掉。
    契约是**补集关系**，不是**常量相等**。
    """
    T0 = 1_700_000_000.0                    # 固定字面量时钟基准（不用真实时钟）
    ages = (0.0, 60.0, 119.0, 119.9, 120.0, 120.1, 121.0, 200.0, 300.0, 3600.0)

    for age in ages:
        # 引擎用真实时钟构造：init 期间可能有内部计时/线程，不冻结更安全
        eng = _engine_split()
        restore = _freeze_engine_time(T0)        # 只冻结 event_engine 模块的 time
        try:
            _feed_at(eng, SPLIT_BROKER, T0 - age)   # 显式 ts：两边同基准，ε = 0
            ra = eng._risk_total_asset()
            ta = eng._total_asset()
        finally:
            restore()                            # 还原，下一轮重新构造

        trusted = ra is not None
        fell_back = abs(ta - LOCAL_ASSEMBLED) < 1e-9
        assert trusted != fell_back, (
            f"age={age}s 时补集关系被破坏：risk_asset={ra!r}（可信={trusted}）、"
            f"total_asset={ta!r}（落到本地拼装={fell_back}）。"
            f"两者必须严格互为补集 —— 一旦「可信」却能拿到拼装值，"
            f"engine/event_engine.py:2197 的闸门就会把脏 EOD 以 mode='live' 落库。")

        # 正向取值：可信时必须真的是券商值，不可信时必须真的是拼装值
        if trusted:
            assert abs(ta - SPLIT_BROKER) < 1e-9, (
                f"age={age}s 判为可信，但 total_asset={ta} 不是券商值 {SPLIT_BROKER}")
        else:
            assert ra is None and fell_back, f"age={age}s 判为不可信，却未回落到拼装值"


def test_complement_holds_for_zero_broker_value_any_age():
    """券商值 = 0 时，无论多新都必须不可信（`bt > 0` 与 `bt <= 0` 的另一半）。

    与上一条配对：上一条扫时间轴，这一条扫「券商值塌成 0」——事故形态
    （2026-11-02 那次持仓表被空返回清空）正是 bt=0 而本地拼装还在。
    """
    for age in (0.0, 1.0, 119.0):
        eng = _engine_split()
        _feed_at(eng, 0.0, time.time() - age)   # 券商值塌成 0
        assert eng._risk_total_asset() is None, (
            f"券商值=0 且 age={age}s 时必须返回 None，不得放行")
        assert abs(eng._total_asset() - LOCAL_ASSEMBLED) < 1e-9, (
            "本地拼装值应仍可算出（用作对照），但不得被当作可信")
        assert eng._risk_asset_basis == "untrusted"

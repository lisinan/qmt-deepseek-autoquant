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
    """券商值刚过期 / 为 0 ⇒ 必须返回 None，绝不能拿拼装值开火。"""
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
    rm.on_asset_update(naive, DAY_OPEN, equity_daily_pnl=True)
    assert rm.is_halted, "旧路径喂 6,953（对开盘 −89.25%）本应熔断"
    assert "daily_stop" in rm._halt_reason


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

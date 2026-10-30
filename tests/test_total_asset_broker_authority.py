# -*- coding: utf-8 -*-
"""总资产口径守卫：live 必须用券商权威 total_asset（2026-10-30 P0）。

真实事故（非假设）
------------------
2026-10-30 11:24:53，OWNER 手动买回 300308.SZ 100 股（约 5.68 万）后，引擎下一轮：

  - `query_stock_asset` 已返回扣款后的 cash = 6,953.42；
  - `query_stock_positions` 尚未刷新出这笔新持仓，只有 ETF；
  - 引擎 `_total_asset()` 自行拼装 = 6,953 + 1,434 = **8,387**，
    对当日开盘 62,411.28 为 **−86.58%**；
  - `daily_stop_flatten_pct = -6%` 与 `max_drawdown_pct = -20%` 同时命中 ⇒
    **误触发强平 + 熔断**，账户被冻结、当日禁止开仓（13:00 还会继续尝试平仓）。

券商侧同一时刻的真实 total_asset 是 65,187 —— 它自身口径一致，不会出现这种
「资金已扣、持仓未到」的缺口。因此 live 模式必须直接用券商的数，不自行拼装。

本文件钉住四条：
  1. live + 券商口径新鲜 ⇒ 用券商 total_asset（即使本地缺一个持仓）；
  2. live + 券商口径陈旧（>120s）⇒ 回落本地拼装（断链不长期主导风控）；
  3. paper ⇒ 永远用本地拼装（不受券商影响，保证回测/实盘口径不互相污染）；
  4. ★ 事故复现：同样的数字在**修复前**必然熔断，修复后不熔断。
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

_TMP = Path(tempfile.mkdtemp(prefix="qmt_total_"))
os.environ.setdefault("QMT_DB", str(_TMP / "total_qmt.db"))
os.environ.setdefault("QMT_NOTICES_LOG", str(_TMP / "total_notices.log"))

from core.data_models import Position          # noqa: E402
from risk.manager import RiskManager           # noqa: E402
import engine.event_engine as EE               # noqa: E402

CASH = 6953.42          # 事故时刻券商返回的可用资金
ETF_MV = 1434.00        # 唯一的本地持仓（500 份 × 2.868）
NAIVE_TOTAL = CASH + ETF_MV          # 8,387.42 ← 少算了一个持仓
BROKER_TOTAL = 65187.42              # 券商权威口径（含 300308 的 56,800）
DAY_OPEN = 62411.28                  # 当日开盘资产（2026-10-30 09:30）

CODE = "510050.SH"


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
        CODE: Position(code=CODE, name="上证50ETF", quantity=500,
                       avg_cost=2.868, last_price=2.868),
    }
    return eng


def _feed_broker_total(eng, total, age_sec=0.0):
    eng._broker_total_asset = float(total)
    eng._broker_total_ts = time.time() - age_sec


# ============================================== 1. 新鲜券商口径优先

def test_live_prefers_fresh_broker_total():
    """即使本地账本少一个持仓，也应以券商 total_asset 为准。"""
    eng = _engine("live")
    _feed_broker_total(eng, BROKER_TOTAL, age_sec=0)
    naive = eng._cash + sum(p.market_value for p in eng._positions.values())
    assert abs(naive - NAIVE_TOTAL) < 0.01, f"用例前提不成立：naive={naive}"
    assert abs(eng._total_asset() - BROKER_TOTAL) < 0.01, (
        f"live 下应返回券商权威 total={BROKER_TOTAL}，"
        f"实际 {eng._total_asset()}（若等于 {NAIVE_TOTAL} 说明又退回本地拼装）")


# ============================================== 2. 陈旧则回落

def test_live_falls_back_when_broker_total_is_stale():
    """券商口径超过 120s 未刷新（断链/查询失败）⇒ 回落本地拼装。"""
    eng = _engine("live")
    _feed_broker_total(eng, BROKER_TOTAL, age_sec=300)
    assert abs(eng._total_asset() - NAIVE_TOTAL) < 0.01, (
        f"陈旧券商口径不应继续主导风控，实际 {eng._total_asset()}")


# ============================================== 3. paper 不受影响

def test_paper_ignores_broker_total():
    """paper 模式永远本地拼装 —— 回测/模拟盘口径不得被实盘数据污染。"""
    eng = _engine("paper")
    _feed_broker_total(eng, BROKER_TOTAL, age_sec=0)
    assert abs(eng._total_asset() - NAIVE_TOTAL) < 0.01, (
        f"paper 下不得使用券商 total，实际 {eng._total_asset()}")


# ============================================== 4. 事故复现（含正向对照）

def test_incident_numbers_would_halt_but_broker_total_does_not():
    """★ 直接复现 2026-10-30 11:24:53 的误熔断，并证明修复有效。

    正向对照：同样的「本地拼装值」喂给风控**必须**触发熔断——
    若这一半不炸，说明风控阈值/判定被改松了，后一半的「不熔断」就毫无意义。
    """
    # --- 对照：用本地拼装值（修复前的口径）--- 
    rm_bad = RiskManager()
    rm_bad.on_asset_update(DAY_OPEN, DAY_OPEN)      # 建立开盘基线
    rm_bad.on_asset_update(NAIVE_TOTAL, DAY_OPEN)
    assert rm_bad.is_halted, (
        "正向对照失败：喂 8,387（对开盘 −86.58%）本应熔断。"
        "若这里不熔断，说明风控判定已被改松，请复核 daily_stop / max_drawdown")

    # --- 修复后：用券商权威值 ---
    eng = _engine("live")
    _feed_broker_total(eng, BROKER_TOTAL, age_sec=0)
    rm_ok = RiskManager()
    rm_ok.on_asset_update(DAY_OPEN, DAY_OPEN)
    rm_ok.on_asset_update(eng._total_asset(), DAY_OPEN)
    assert not rm_ok.is_halted, (
        f"修复后仍被误熔断：total={eng._total_asset()} vs 开盘 {DAY_OPEN}，"
        f"原因={getattr(rm_ok, '_halt_reason', '')}")
    assert rm_ok.position_scale == 1.0, rm_ok.position_scale

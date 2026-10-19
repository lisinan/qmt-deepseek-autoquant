# -*- coding: utf-8 -*-
"""【AM-EVOLVE R37 2026-10-19】生产「两条风控降级通道」↔ 回测代理的**语义对齐**守卫。

背景（本守卫为什么必须存在）
  生产实盘长期开启两条回测 P0 基线**从未建模**的降级通道：
    ① 连亏阶梯  risk/manager.py: 亏 → +1（同日只计 1 次，R23 修复）；
       ≥ max_consecutive_losses(3) 按 _SCALE_LADDER=[1.0,.8,.6,.4,.0] 降仓；
       ≥ max_consecutive_losses_halt(5) → _halt()（冻结一切新开仓）；
       冷却 halt_recover_days(1) 日自愈。
    ② 日内已实现亏损熔断  _daily_pnl ≤ −max(5000, |daily_loss_limit_pct| × 权益)
       ⇒ _halt()（R23 DAILY_LOSS_UNION_MAX 取较宽松者）。
  回测侧 R6/R7 已建代理（``consec_loss_*`` / ``daily_loss_*``），但
  ``strategy/_evolve_wf.py::base_cfg`` **两者均保持默认关闭** ⇒ 自 09-24 起
  所有 dSh 与 KPI 都建立在「无降级通道」的偏乐观口径上（第 15/16 例口径背离）。

R37 实测价签（23 只 × 911 根 tushare 面板 20230109→20261016，单边 0.15%，
4 套互不重叠折划分，dSh 相对验证器 P0 基线 IS +295.74%/1.87/−13.05%）：
    PROD 两条全开   -0.058 +0.066 -0.044 -0.033 | 最差 -0.058 均值 -0.017
                    IS +266.56% / Sh 1.77 / MDD **-15.61%**（回撤预算使用率 65%→78%）
    CL 仅连亏阶梯   -0.103 +0.016 -0.092 -0.084 | 最差 -0.103 均值 -0.066（负项）
    DL 仅日内熔断   +0.043 +0.049 +0.049 +0.052 | 最差 +0.043 均值 +0.049（正项）
  剂量轴（相对 PROD）：-2.5%≡-3%（惰性）、-2% +0.003、-4% -0.060、-5% -0.046、
    关闭/(-8%) -0.049 ⇒ **-3% 恰为峰**；阶梯触发 4 更差(-0.082)、熔断 7≡9 更差
    (-0.119)、地板 0.4/0.6 **逐位惰性** ⇒ 两条轴均无过闸方向，已闭合。

本守卫钉死四件事（不改任何生产参数值）：
  1. 生产两条通道的**参数值**与回测代理字段默认值逐一对齐（漂移即红）；
  2. 验证器 P0 基线**确为关闭态**（若有人打开，必须显式重测并重钉基线）；
  3. 连亏阶梯末端 0.0 **永不可达**（halt 阈值先于阶梯末端触发）⇒ 地板类改动
     是空操作，防止日后重复投入；
  4. 日内熔断 union-max 语义（大账户走 pct、小账户走 abs）不被静默改回。
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

import config.settings as S
import risk.manager as RM
from strategy.backtest_daily import BacktestConfig


def _bt() -> BacktestConfig:
    return BacktestConfig()


# ---------------------------------------------------------------- 1) 值对齐
def test_ladder_trigger_matches_proxy_default():
    """生产降仓触发阈值 == 回测代理默认 consec_loss_trigger。"""
    assert S.RISK_PARAMS["max_consecutive_losses"] == _bt().consec_loss_trigger


def test_ladder_halt_matches_proxy_default():
    """生产连亏熔断阈值 == 回测代理默认 consec_loss_halt。"""
    assert S.RISK_PARAMS["max_consecutive_losses_halt"] == _bt().consec_loss_halt


def test_recover_days_match_proxy_halt_bars():
    """生产冷却天数 == 两条通道代理的 halt_bars（日线粒度下 = 封锁 1 根 bar）。"""
    d = S.RISK_PARAMS["halt_recover_days"]
    assert d == _bt().consec_loss_halt_bars
    assert d == _bt().daily_loss_halt_bars


def test_daily_loss_pct_matches_proxy_default():
    """生产日内熔断百分比阈值 == 回测代理默认 daily_loss_pct。"""
    assert S.RISK_PARAMS["daily_loss_limit_pct"] == _bt().daily_loss_pct


# ------------------------------------------------- 2) 验证器基线确为关闭态
def test_validator_baseline_channels_are_off():
    """P0 基线两条通道必须保持关闭（口径已知，不得悄悄打开使历史结论失真）。

    若日后要把它们并入 P0，必须**同轮**重钉所有 KPI 并在
    reports/EVOLUTION_DECISIONS.md 留痕；本用例会先变红强制走这一步。
    """
    from strategy import _evolve_wf as W

    b = W.base_cfg()
    assert b.consec_loss_scale is False
    assert b.daily_loss_halt is False
    # 关闭态下阈值字段仍须与生产一致（防「值对齐」被单侧改掉）
    assert b.consec_loss_trigger == S.RISK_PARAMS["max_consecutive_losses"]
    assert b.daily_loss_pct == S.RISK_PARAMS["daily_loss_limit_pct"]


# --------------------------------------- 3) 阶梯末端地板永不可达（勿再投入）
def test_scale_ladder_terminal_zero_is_unreachable():
    """halt 阈值先于阶梯末端触发 ⇒ consec_loss_floor 是恒等空操作。

    实盘：consec=3→0.8、4→0.6、**5→halt**（末端 0.0 永远走不到）。
    R37 实测 floor 0.4 / 0.6 与 baseline **逐位相同**，与此断言一致。
    ⇒ 任何「给连亏阶梯加地板」的改动都必须**同时**上调 halt 阈值，否则是死代码。
    """
    ladder = RM._SCALE_LADDER
    assert ladder[-1] == 0.0                     # 末端＝完全冻结
    trig = S.RISK_PARAMS["max_consecutive_losses"]
    halt = S.RISK_PARAMS["max_consecutive_losses_halt"]
    # 达到末端所需的连亏次数
    need_for_terminal = trig + len(ladder) - 1
    assert halt < need_for_terminal, (
        f"halt={halt} < 阶梯末端所需 {need_for_terminal} ⇒ 末端 0.0 不可达，"
        "floor 类改动为空操作；若要启用地板必须先上调 max_consecutive_losses_halt")


# ------------------------------------------------- 4) union-max 语义不被改回
def test_daily_loss_limit_union_max_semantics():
    """生效阈值 = max(abs, |pct| × 权益)：大账户走 pct（随规模缩放），小账户走 abs。"""
    assert RM.DAILY_LOSS_UNION_MAX is True
    rm = RM.RiskManager()
    big = 1_000_000.0
    assert abs(rm._daily_loss_limit(big) - 0.03 * big) < 1e-6   # 30,000 > 5,000
    small = 100_000.0
    assert abs(rm._daily_loss_limit(small) - 5_000.0) < 1e-6    # 5,000 > 3,000


def test_daily_loss_limit_disabled_falls_back_to_abs():
    """可逆开关：DAILY_LOSS_UNION_MAX=False 即回到 abs 独断（保留回退路径可用）。"""
    old = RM.DAILY_LOSS_UNION_MAX
    try:
        RM.DAILY_LOSS_UNION_MAX = False
        rm = RM.RiskManager()
        assert abs(rm._daily_loss_limit(1_000_000.0) - 5_000.0) < 1e-6
    finally:
        RM.DAILY_LOSS_UNION_MAX = old

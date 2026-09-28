# -*- coding: utf-8 -*-
"""北向闸门「可信度守卫」守卫测试（2026-09-28 PM-EVOLVE 第 11 轮）。

背景（实盘铁证）：tushare `moneyflow_hsgt.north_money` **自 2024-08-19 起不是净买入**
（官方当日停止披露每日净买入额）。实测本地缓存：

    区间                     n    正占比   均值       断点
    2023-01 ~ 2024-08-16    235   0.44     -53       ← 真净买入（有负号）
    2024-08-19 ~ 2026-08-25 365   1.00     272,396   ← 恒正窄带（成交额口径）

断点前最长连续正值仅 **8 天**、60 日滚动全正窗口 **0/175**；断点后 **365/365 全正**。
⇒ 滚动 20 日累计<0 在 2025 年 **0/128** 天、2026 年 **0/132** 天成立
⇒ **实盘 nb gate 自 2025 年起是死闸门，从未真正拦截过一次建仓。**

守卫契约：序列「陈旧」或「退化」→ 判定不可信 → 调用方必须 **fail-open（不拦截）**。
本文件钉死该契约，防止后续改动把「恒正成交额序列」重新当成净买入去拦建仓。
"""
from __future__ import annotations

import json
from pathlib import Path

from data.northbound_cache import (NB_DEGENERATE_DAYS, NB_STALE_DAYS,
                                   nb_gate_credible)
from config import settings as S

CACHE = Path(S.BASE_DIR) / "data" / "cache" / "northbound_hsgt.json"


def _series():
    if not CACHE.exists():
        return None, None
    d = json.loads(CACHE.read_text(encoding="utf-8"))
    return {k: float(v) for k, v in d.items()}, sorted(d)


# ---------------------------------------------------------------- 单元契约

def test_guard_returns_credible_on_healthy_series():
    """健康（有正有负）序列 + 新鲜 ⇒ 可信。"""
    s = {"20260101": 10.0, "20260102": -5.0, "20260103": 3.0}
    assert nb_gate_credible(s, sorted(s), "20260103") == (True, "ok")


def test_guard_fail_open_on_empty():
    assert nb_gate_credible({}, [], "20260103") == (False, "empty")
    assert nb_gate_credible({"20270101": 1.0}, ["20270101"],
                            "20260103") == (False, "empty")


def test_guard_fail_open_when_stale():
    """序列末端距评估日 > nb_stale_days ⇒ stale ⇒ fail-open。"""
    s = {"20260101": -10.0, "20260102": 5.0}
    # 末端 20260102，评估日 20260120（相距 18 天 > 10）
    assert nb_gate_credible(s, sorted(s), "20260120") == (False, "stale")
    # 相距 5 天 ⇒ 不判陈旧
    assert nb_gate_credible(s, sorted(s), "20260107")[0] is True


def test_guard_fail_open_when_degenerate():
    """trailing N 日全为正 ⇒ 不是净买入序列 ⇒ fail-open。"""
    days = [f"20260{i:02d}01" for i in range(1, 13)]
    s = {d: 100.0 + i for i, d in enumerate(days)}
    # degenerate_days 调到 10（<= 12 个样本）⇒ 全正 ⇒ 退化
    assert nb_gate_credible(s, days, "20261201",
                            stale_days=0, degenerate_days=10) == (
        False, "degenerate")
    # 把最早一天改成负值 ⇒ 不再退化
    s2 = dict(s)
    s2[days[3]] = -1.0
    assert nb_gate_credible(s2, days, "20261201",
                            stale_days=0, degenerate_days=10) == (True, "ok")


def test_guard_disabled_by_zero_thresholds():
    """两个阈值任一为 0 即关闭该项检查（可逆性开关）。"""
    s = {"20260101": 1.0, "20260102": 2.0}
    assert nb_gate_credible(s, sorted(s), "20260301",
                            stale_days=0, degenerate_days=60) == (True, "ok")


def test_defaults_are_safe_against_real_netflow_regime():
    """默认阈值不得误杀真实净买入：断点前最长连正仅 8 天 ≪ 60。"""
    assert NB_DEGENERATE_DAYS >= 30
    assert NB_STALE_DAYS >= 5


# ------------------------------------------------- 真实缓存上的结构性断点

def test_real_cache_has_structural_break_at_policy_date():
    """2024-08-19 前后分布必须呈现断点（恒正 + 均值跳变），钉死数据性质。"""
    s, keys = _series()
    if not s:
        print("  [skip] 无北向缓存")
        return
    pre = [s[k] for k in keys if k <= "20240816"]
    post = [s[k] for k in keys if k >= "20240819"]
    if len(pre) < 50 or len(post) < 50:
        print("  [skip] 缓存样本不足")
        return
    pre_pos = sum(1 for v in pre if v > 0) / len(pre)
    post_pos = sum(1 for v in post if v > 0) / len(post)
    assert pre_pos < 0.7, f"断点前应是有正有负的净买入序列，实测正占比 {pre_pos:.2f}"
    assert post_pos > 0.99, f"断点后应恒正（成交额口径），实测正占比 {post_pos:.2f}"


def test_real_cache_gate_would_never_fire_in_2025_2026():
    """死闸门事实：2025/2026 滚动 20 日累计<0 的天数必须为 0。"""
    s, keys = _series()
    if not s:
        print("  [skip] 无北向缓存")
        return
    for year in ("2025", "2026"):
        sub = [s[k] for k in keys if k.startswith(year)]
        if len(sub) <= 20:
            continue
        fire = sum(1 for i in range(20, len(sub) + 1)
                   if sum(sub[i - 20:i]) < 0)
        assert fire == 0, (
            f"{year} 年出现 {fire} 天可触发 nb 拦截，与「死闸门」结论冲突，"
            f"须重新核对数据源与守卫阈值")


def test_live_current_cache_is_not_credible():
    """★ 实盘零行为变化证明：当前缓存判定为不可信 ⇒ fail-open；
       而旧逻辑下 rolling_sum>0 本就不拦截 ⇒ 守卫上线前后实盘行为完全一致。"""
    s, keys = _series()
    if not s:
        print("  [skip] 无北向缓存")
        return
    ok, why = nb_gate_credible(s, keys, "20260928",
                               stale_days=NB_STALE_DAYS,
                               degenerate_days=NB_DEGENERATE_DAYS)
    assert ok is False, (
        f"当前缓存竟被判为可信（{why}）：守卫将开始用陈旧/恒正序列拦建仓，"
        f"须立即复核 data/cache/northbound_hsgt.json")
    assert why in ("stale", "degenerate")


# ------------------------------------------------- 生产接线（防回测-only）

def test_engine_reads_guard_params():
    """守卫参数必须在实盘引擎有读取点（禁止只写 settings 制造口径背离）。"""
    src = (Path(S.BASE_DIR) / "engine" / "event_engine.py").read_text(
        encoding="utf-8")
    for p in ("nb_credibility_guard", "nb_stale_days", "nb_degenerate_days"):
        assert f'"{p}"' in src, f"engine 未读取 STRATEGY_PARAMS.{p}"
    assert "nb_gate_credible" in src, "engine 未调用 nb_gate_credible"


def test_backtest_and_engine_share_same_guard():
    """回测与实盘必须共用同一个守卫函数，否则又是口径背离。"""
    bt = (Path(S.BASE_DIR) / "strategy" / "backtest_daily.py").read_text(
        encoding="utf-8")
    assert "nb_gate_credible" in bt, "回测未接入 nb_gate_credible"
    assert "nb_credibility_guard" in bt


def test_settings_defaults_guard_on():
    assert S.STRATEGY_PARAMS.get("nb_credibility_guard") is True
    assert int(S.STRATEGY_PARAMS.get("nb_stale_days", 0)) == NB_STALE_DAYS
    assert (int(S.STRATEGY_PARAMS.get("nb_degenerate_days", 0))
            == NB_DEGENERATE_DAYS)

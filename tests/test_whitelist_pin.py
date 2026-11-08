# -*- coding: utf-8 -*-
"""生产白名单**值级别**钉子（2026-11-08 OWNER 指令：「原有白名单股票数据不要出错」）。

## 为什么需要值级别的钉子

仓库里已有好几份白名单相关的测试，但它们钉的都是**行为**：
`test_exit_exempt_declared` 验证「声明了就豁免、没声明就不豁免」，
`test_exempt_hard_stop_force` 验证「豁免仓不被 −18% 硬止损打掉」，
`test_manual_entry` 验证观察篮买入路径。
**没有任何一份测试钉住「生产配置里到底是哪几只票」。**

于是出现过这种事：一次改动把 `exit_exempt_codes` 从 `["300308.SZ","510050.SH"]`
改成了只清一半，测试全绿（行为没变，只是名单短了），而实盘里 510050 会在下一次
−6% 日亏强平里被卖掉。行为测试对「名单本身」是瞎的。

本文件把名单**逐项钉死**。故意改任何一只票，这里都必须红 —— 红了就去 OWNER 那里
要一句明确裁决，而不是顺手改测试。

## 三层白名单（互不相同，别混）

| 层 | 位置 | 作用 |
|---|---|---|
| 静态宇宙 | `UNIVERSE` / `STOCK_CODES` / `VALIDATION_UNIVERSE` | 引擎订阅与评分的固定标的 |
| 观察篮买入 | `STRATEGY_PARAMS["manual_entry_codes"]` | 绕过闸门直接建仓（会触发买入） |
| 退出豁免 | `STRATEGY_PARAMS["exit_exempt_codes"]` | **只表达「不要自动卖」，不产生买入** |
| 动态池 | `storage/dynamic_universe.json::active_pool` | 每日刷新的候选池（会变，只钉不变量） |

纪律：本文件**只读**，不写任何库、不写任何 json、不改 settings。
"""
from __future__ import annotations

import json
from pathlib import Path

import config.settings as S

ROOT = Path(__file__).resolve().parents[1]

# ---- 基线（2026-11-08 实测值；改动必须经 OWNER 裁决后同步更新本文件）----------
EXPECT_UNIVERSE = [
    "000001.SH", "000300.SH", "000977.SZ", "002230.SZ",
    "002371.SZ", "002415.SZ", "300033.SZ", "300308.SZ",
    "300394.SZ", "300502.SZ", "399001.SZ", "399006.SZ",
    "603019.SH", "688111.SH", "688256.SH", "688981.SH",
]
EXPECT_INDEX = ["000001.SH", "000300.SH", "399001.SZ", "399006.SZ"]
EXPECT_STOCK = [
    "000977.SZ", "002230.SZ", "002371.SZ", "002415.SZ",
    "300033.SZ", "300308.SZ", "300394.SZ", "300502.SZ",
    "603019.SH", "688111.SH", "688256.SH", "688981.SH",
]
EXPECT_VALIDATION = ["510050.SH"]
EXPECT_EXEMPT = ["300308.SZ", "510050.SH"]      # OWNER 2026-11-05 裁决：维持豁免
EXPECT_MANUAL = ["510050.SH"]


# ---------------------------------------------------------------- 静态宇宙
def test_universe_is_pinned():
    """UNIVERSE 必须逐项等于基线（顺序无关，集合相等 + 长度相等）。"""
    assert sorted(S.UNIVERSE) == EXPECT_UNIVERSE, (
        f"UNIVERSE 被改动：{sorted(set(S.UNIVERSE) ^ set(EXPECT_UNIVERSE))}")
    assert len(S.UNIVERSE) == len(EXPECT_UNIVERSE)


def test_stock_codes_is_pinned():
    assert sorted(S.STOCK_CODES) == EXPECT_STOCK, (
        f"STOCK_CODES 被改动：{sorted(set(S.STOCK_CODES) ^ set(EXPECT_STOCK))}")


def test_index_codes_is_pinned():
    assert sorted(S.INDEX_CODES) == EXPECT_INDEX


def test_stock_codes_is_universe_minus_index():
    """STOCK_CODES 必须是 UNIVERSE 去掉指数 —— 防止有人单独改一边造成错位。"""
    assert set(S.STOCK_CODES) == set(S.UNIVERSE) - set(S.INDEX_CODES)


def test_validation_universe_is_pinned():
    """VALIDATION_UNIVERSE 是 live 链路验证用的探针 ETF（510050）。"""
    assert sorted(S.VALIDATION_UNIVERSE) == EXPECT_VALIDATION


# ---------------------------------------------------------------- 策略白名单
def test_exempt_codes_is_pinned():
    """退出豁免名单必须逐项相等 —— 少一只就意味着一只真实持仓会被自动卖出。"""
    got = list(S.STRATEGY_PARAMS.get("exit_exempt_codes") or [])
    assert got == EXPECT_EXEMPT, (
        f"exit_exempt_codes 被改成 {got}，基线是 {EXPECT_EXEMPT}。"
        f"这份名单直接决定真实持仓会不会被自动卖出，改动必须先经 OWNER 裁决。")


def test_manual_entry_codes_is_pinned():
    """观察篮买入清单必须逐项相等 —— 多一只会凭空建仓，少一只会不再补仓。"""
    got = list(S.STRATEGY_PARAMS.get("manual_entry_codes") or [])
    assert got == EXPECT_MANUAL, (
        f"manual_entry_codes 被改成 {got}，基线是 {EXPECT_MANUAL}")


def test_exempt_and_manual_are_not_conflated():
    """★ 两条名单不得被合并。

    历史坑（`settings.py:688-691` 有记载）：把豁免票塞进 `manual_entry_codes`
    会变出「补仓」副作用 —— 该票一旦被卖出变成不持有，观察篮会**自动买回来**，
    于是「豁免 → 被强平卖出 → 次日自动买回 → 判据复活」形成自反恢复。
    """
    exempt = set(S.STRATEGY_PARAMS.get("exit_exempt_codes") or [])
    manual = set(S.STRATEGY_PARAMS.get("manual_entry_codes") or [])
    assert exempt != manual, "两条名单被合并成同一个 —— 会产生自反补仓"
    # 豁免名单里可以有观察篮票（510050 就是这种），但反之不成立
    assert manual.issubset(set(EXPECT_VALIDATION) | {"510050.SH"}), \
        f"观察篮里出现了非预期代码：{manual}"


def test_exempt_stocks_are_not_in_stock_codes_expectation():
    """观察篮/豁免票（510050）不进静态宇宙 —— 它不是动量策略的候选。

    注意 300308 同时在 UNIVERSE 与豁免名单里：它既是被评分的标的，也豁免自动卖。
    这是 OWNER 裁决的结果，不是冲突。
    """
    assert "510050.SH" not in S.STOCK_CODES, "510050 不应进静态宇宙（它是探针 ETF）"
    assert "300308.SZ" in S.STOCK_CODES, "300308 应仍在静态宇宙（豁免只管退出）"


# ---------------------------------------------------------------- 动态池不变量
def _pool():
    f = ROOT / "storage" / "dynamic_universe.json"
    return json.loads(f.read_text(encoding="utf-8"))


def test_dynamic_pool_shape_invariants():
    """动态池每天都会变，所以只钉**不变量**，不钉具体名单。"""
    d = _pool()
    pool = d.get("active_pool") or []
    by_ind = d.get("by_industry") or {}
    assert len(pool) == d.get("active_pool_size"), \
        f"active_pool 长度 {len(pool)} != active_pool_size {d.get('active_pool_size')}"
    assert sum(by_ind.values()) == d.get("n_total"), \
        f"by_industry 合计 {sum(by_ind.values())} != n_total {d.get('n_total')}"
    assert set(by_ind) == set(d.get("industries") or []), "行业名与 industries 不一致"
    assert len(pool) > 0, "候选池为空 ⇒ 信号层会静默空转"
    assert all(isinstance(c, str) and c.endswith((".SH", ".SZ")) for c in pool)


def test_paper_pool_is_independent_file():
    """paper 的池子必须是独立文件（2026-11-08 之前它与 live 共用同一个文件）。"""
    f = ROOT / "storage_paper" / "dynamic_universe.json"
    assert f.exists(), "paper 候选池文件缺失 ⇒ paper 首启会 0 只空转"
    assert f.resolve() != (ROOT / "storage" / "dynamic_universe.json").resolve()
    d = json.loads(f.read_text(encoding="utf-8"))
    assert len(d.get("active_pool") or []) > 0

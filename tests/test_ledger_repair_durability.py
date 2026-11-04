# -*- coding: utf-8 -*-
"""账本「人工修正不可持久」守卫：单日重记不得抹掉已修正的 EOD。

背景（2026-11-04，data-ledger-auditor-2 复核发现）
--------------------------------------------------
``python -m strategy.record_ledger --date 2026-10-30`` 会把
``logs/evolution_ledger.jsonl`` 里 2026-10-30 那一行**整行替换**：

    eod_asset    63,684.42（按收盘价重算，双源核对）→ 65,748.42（11:29 盘中快照）
    daily_ret_pct      -4.29%  →  -1.19%
    fix          ledger_eod_1030_close_recalc  →  <键消失>
    eod_basis    close_recalc  →  <键消失>

触发链（逐行核实）：
  1. ``logs/review_2026-10-30.json`` 不存在 ⇒ main() 走 DB 直读回退；
  2. ``eod_asset()`` 的 stale 守卫只拦「零成交 + 权益零波动 + 跳变>0.5%」，
     而 10-30 有 2 笔成交且权益大幅波动 ⇒ 三条都不满足 ⇒ 返回盘中快照；
  3. 旧 ``append_jsonl`` 无条件 ``rows[i] = row``。

最讽刺的是：**这条命令的典型动机就是「核对 10-30 为什么是 −4.29%」**——
审计动作本身销毁证据，事后连「这行被修过」都查不到。

本文件钉住三条：
  1. 带 ``fix`` / ``eod_basis`` / ``source=manual_repair`` 的行，DB 原始值
     与之不同 ⇒ **拒绝覆盖**，账本字节不变；
  2. 拒绝时**整个文件不写**（不能只跳过该行却重排其它行）；
  3. 正向对照：没有修正标记的行仍然允许正常覆盖（守卫不得过宽）。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config.settings  # noqa: E402,F401  （须先于 sqlite3：DLL 路径）
import strategy.record_ledger as RL   # noqa: E402

DAY = "2026-10-30"
REPAIRED_EOD = 63684.42
INTRADAY_EOD = 65748.42     # DB 最后一条快照（11:29:43）的值


def _ledger(rows) -> Path:
    p = Path(tempfile.mkdtemp(prefix="qmt_repair_")) / "l.jsonl"
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                 encoding="utf-8")
    return p


def _repaired_row():
    return {
        "date": DAY, "mode": "live", "eod_asset": REPAIRED_EOD,
        "prev_asset": 66542.31, "daily_pnl": -2857.89, "daily_ret_pct": -4.29,
        "range_pct": -4.29, "fix": "ledger_eod_1030_close_recalc",
        "eod_basis": "close_recalc", "eod_ts": None, "eod_is_close": True,
        "source": "manual_repair", "note": "已按收盘价重算",
    }


def _raw_row():
    """模拟 ``build_row_from_db('2026-10-30')`` 的输出（DB 直读口径）。"""
    return {
        "date": DAY, "mode": "live", "eod_asset": INTRADAY_EOD,
        "prev_asset": 66542.31, "daily_pnl": -794.0, "daily_ret_pct": -1.19,
        "range_pct": -1.19, "eod_ts": "2026-10-30T11:29:43.961251",
        "eod_is_close": False, "source": "db_fallback",
        "note": "[EOD 非收盘] 最后一根快照 11:29 早于 15:00，该 eod_asset 为盘中值",
    }


def test_repaired_row_is_not_overwritten_by_db_values():
    old = RL.LEDGER_JSONL
    p = _ledger([_repaired_row()])
    before = p.read_bytes()
    try:
        RL.LEDGER_JSONL = p
        appended = RL.append_jsonl(_raw_row())
        assert appended is False, "命中了既有行 ⇒ 不应报告为追加"
        after = p.read_bytes()
        assert after == before, "拒绝覆盖时必须整个文件不写（连重排都不允许）"
        row = json.loads(p.read_text(encoding="utf-8").strip())
        assert row["eod_asset"] == REPAIRED_EOD, (
            f"人工修正值被 DB 原始值覆盖：{row['eod_asset']}（应为 {REPAIRED_EOD}）")
        assert row["fix"] == "ledger_eod_1030_close_recalc", "fix 标记必须存活"
        assert row["eod_basis"] == "close_recalc", "eod_basis 必须存活"
        assert row["eod_is_close"] is True, "收盘口径不得被改回 False"
        assert row["daily_ret_pct"] == -4.29, row["daily_ret_pct"]
    finally:
        RL.LEDGER_JSONL = old


def test_guard_does_not_block_unrepaired_rows():
    """正向对照：普通行仍可被重记覆盖（否则守卫就从「保护」变成「冻结」）。"""
    old = RL.LEDGER_JSONL
    p = _ledger([{"date": DAY, "mode": "live", "eod_asset": 1.0,
                  "source": "db_fallback"}])
    try:
        RL.LEDGER_JSONL = p
        RL.append_jsonl(_raw_row())
        row = json.loads(p.read_text(encoding="utf-8").strip())
        assert row["eod_asset"] == INTRADAY_EOD, "无修正标记的行必须能正常重记"
    finally:
        RL.LEDGER_JSONL = old


def test_guard_allows_rewrite_when_value_unchanged():
    """值相同（幂等重写）时不必拒绝 —— 否则重复执行会永远报 WARN。"""
    old = RL.LEDGER_JSONL
    row = _repaired_row()
    p = _ledger([row])
    try:
        RL.LEDGER_JSONL = p
        same = dict(row, fills_n=2)
        appended = RL.append_jsonl(same)
        assert appended is False
        got = json.loads(p.read_text(encoding="utf-8").strip())
        assert got["fills_n"] == 2, "值未变时应正常合并新字段"
        assert got["eod_asset"] == REPAIRED_EOD
    finally:
        RL.LEDGER_JSONL = old


def test_is_repaired_marker_set():
    assert RL._is_repaired({"fix": "x"}) is True
    assert RL._is_repaired({"eod_basis": "close_recalc"}) is True
    assert RL._is_repaired({"source": "manual_repair"}) is True
    assert RL._is_repaired({"source": "db_fallback", "eod_is_close": True}) is False
    assert RL._is_repaired({}) is False

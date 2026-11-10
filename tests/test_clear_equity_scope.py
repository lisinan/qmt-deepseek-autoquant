# -*- coding: utf-8 -*-
"""`Storage.clear_equity_before` 的作用域契约：只清 paper，绝不触及 live（2026-11-05 审计新增）。

为什么需要这条（P2「未声明的不变量」，见 OWNER_INBOX）
------------------------------------------------------
2026-09-21 引入的 `storage/.reset_paper.flag` 复位机制，会在引擎启动期执行
`clear_equity_before(f"{今日}T00:00:00")`，把「今天之前」的权益快照删掉，使新周期
曲线从初始资金干净起算。该动作**不可逆**（快照没有备份），且在 live 时代后果不对称：
paper 行可以重建，live 行（真实资金）删掉就再也算不出当日/区间收益。

契约：**paper 复位不得触及任何非 paper 行。**

现行实现靠两道**独立**保险维持它：
  ① 调用点守卫 `engine/event_engine.py:565-570`
     `if self.exec_mode == "paper": self._restore_engine_state(); self._apply_pending_reset()`
     `elif self.exec_mode == "live": self._restore_engine_state(restore_ledger=False)`
     ⇒ live 分支根本不调 `_apply_pending_reset`，flag 在 live 下是死文件。
  ② SQL 过滤 `storage/db.py:423`
     `DELETE FROM equity_snapshots WHERE mode='paper' AND ts < ?`

本文件钉住的是 **② 的行为契约**，不是它的字面量。将来实现换成「先 select id 再按 id 删」
「改 mode 白名单」「改 EXISTS 子查询」，只要契约不变就照样绿；只有「非 paper 行被删」才红。

⚠️ 故意**不**写成 ``assert "mode='paper'" in sql``
   那种写法锁的是实现而不是语义：它既拦不住「先 select 后删」这种正确重写，
    又会在实现一改时逼人去改断言（本项目已发生多次：守卫被改断言绕过，而不是被修复）。
    断言的对象必须是**行还在不在**。

关于 NULL：`save_equity_snapshot` 用 `(mode or "paper").lower()` 兜底，写不出 NULL；
    生产实测 `equity_snapshots` 分布 live 781 / paper 5359 / NULL 0（2026-11-05 00:40）。
    故不构造 NULL 行——测一个生产上产生不出来的状态只会制造噪声。
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from storage.db import Storage  # noqa: E402

LIVE_TOTAL = 66411.12        # 2026-11-04 真实 EOD（券商口径）
LIVE_CASH = 11611.62
LIVE_MV = 54799.50
PAPER_TOTAL = 1000000.0      # paper 复位后的初始资金


def _st(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "scope.db")


def _seed(st: Storage) -> None:
    """一行 paper + 一行 live，两行都在「今天之前」的语义范围内。"""
    st.save_equity_snapshot(PAPER_TOTAL, PAPER_TOTAL, 0.0, 0, 0.0, mode="paper")
    st.save_equity_snapshot(LIVE_TOTAL, LIVE_CASH, LIVE_MV, 2, 0.0, mode="live")


def _rows(st: Storage):
    c = st._conn_get()
    return c.execute(
        "SELECT mode, total_asset, cash, market_value, positions_count "
        "FROM equity_snapshots ORDER BY id").fetchall()


def test_clear_equity_before_deletes_paper_only(tmp_path):
    """核心契约：cutoff 早于全部行时，只删 paper，live 行一条不动、值也不动。

    cutoff 取「明天 00:00」而不是「今天 00:00」，是为了让两条种子行都落在
    `ts < cutoff` 成立的那一侧 ⇒ 此时唯一能保护 live 行的就只有 `mode='paper'`
    这一个条件。这样测出来的才是**作用域契约**，而不是时间条件。
    """
    st = _st(tmp_path)
    _seed(st)
    assert len(_rows(st)) == 2, "用例前提：两条种子行都要落进去"

    cutoff = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
    n = st.clear_equity_before(cutoff)

    rows = _rows(st)
    modes = [r[0] for r in rows]

    # ① live 行必须还在，且逐位未变（不是"还在但被改过"）
    live = [r for r in rows if r[0] == "live"]
    assert len(live) == 1, f"live 行被 paper 复位波及了：剩余 rows={rows}"
    assert abs(live[0][1] - LIVE_TOTAL) < 1e-9, f"live total_asset 被改动：{live[0][1]}"
    assert abs(live[0][2] - LIVE_CASH) < 1e-9, f"live cash 被改动：{live[0][2]}"
    assert abs(live[0][3] - LIVE_MV) < 1e-9, f"live market_value 被改动：{live[0][3]}"
    assert live[0][4] == 2, f"live positions_count 被改动：{live[0][4]}"

    # ② paper 行必须被删（否则本测试会因为"什么都没做"而空过）
    assert "paper" not in modes, f"paper 行没被清掉，cutoff 没生效：{rows}"

    # ③ 返回码 = 实际删除行数，防止实现改成"返回 0 也算过"
    assert n == 1, f"clear_equity_before 应只删 1 行（paper），实际 {n}"


def test_clear_equity_before_respects_cutoff(tmp_path):
    """时间条件仍然生效：cutoff 在过去 ⇒ 一行都不删（两个 mode 都保留）。

    这条与上一条配对：上一条证明 `mode` 过滤在起作用，这一条证明实现没有
    被改写成「无条件清空 paper」（那会让 09-30 之前的历史一起消失）。
    """
    st = _st(tmp_path)
    _seed(st)

    cutoff = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
    n = st.clear_equity_before(cutoff)

    rows = _rows(st)
    assert len(rows) == 2, f"cutoff 在过去时不应删任何行，实际剩 {rows}"
    assert n == 0, f"无行可删时应返回 0，实际 {n}"
    assert sorted(r[0] for r in rows) == ["live", "paper"]


def test_clear_equity_before_is_idempotent(tmp_path):
    """第二次调用删不到东西 ⇒ 必须返回 0 且 live 仍在（复位可能被重复触发）。"""
    st = _st(tmp_path)
    _seed(st)
    cutoff = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")

    assert st.clear_equity_before(cutoff) == 1
    assert st.clear_equity_before(cutoff) == 0, "第二次调用不应再删任何行"
    assert [r[0] for r in _rows(st)] == ["live"]

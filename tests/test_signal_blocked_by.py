# -*- coding: utf-8 -*-
"""信号「未成交原因」（blocked_by）守卫（2026-10-29）。

【为什么需要这个字段 —— 实盘案例】
2026-10-29 早盘，中芯国际 688981.SH 全天 24 轮都给出 BUY（score 4.78，
threshold 4.0 已过），但零成交。真实原因是「宽度确认 <2 只」
（``min_passing_candidates=2``，config/settings.py:428），
可这条信息**只存在于一行 logger.info 里**：

    [宽度确认] 本轮 BUY 信号仅 1 只 < 2 ⇒ 当日不开新仓（688981.SH）

而 ``signals`` 表里全是裸 ``side='BUY'``（``_save_signal`` 在闸门**之前**调用），
Web「实时信号」面板也照原样显示「BUY」。用户只能人工翻日志 + 数信号数才能还原，
而且极易误判成「策略没反应」或「分数不够」。

更糟的是同一轮那条 warning 的文案本身有歧义：
「最高分 688981.SH **仅** 4.78（阈值 4.0）」—— 4.78 > 4.0，它明明通过了阈值。

【设计取舍（为什么不改 _handle_buy 签名）】
``_handle_buy`` 内有 8 个静默 ``return`` 分支（mock 闸门 / AI 否决 /
``position_scale<=0`` / ``price<=0`` / 行情陈旧 / 熔断 / 日内次数 / 现金不足）。
逐个改成返回原因＝动下单主路径，风险远大于收益。改为读它**已经维护好**的
``self._last_reject[code]`` / ``_last_reject_ts[code]``（由 ``_log_reject_once``
写入）⇒ 覆盖全部拒绝分支，且将来新增分支自动被覆盖。

【本测试钉住什么】
  1. ``Signal.blocked_by`` 默认空 ⇒ 旧调用方零改动；
  2. 旧库（signals 无该列）在 ``Storage`` 初始化时被幂等迁移补列；
  3. ``save_signal`` 落库该字段、``mark_blocked`` 只改指定 id 且不碰原始事实；
  4. ``get_signals`` 直接透出该字段（Web API 无需改动）；
  5. 重复迁移不报错（幂等）。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, ".")

from core.data_models import Signal          # noqa: E402
from storage.db import Storage               # noqa: E402


def _tmp_db():
    fd, p = tempfile.mkstemp(suffix=".db")
    import os
    os.close(fd)
    return Path(p)


def _sig(code="688981.SH", side="BUY", score=4.78, blocked_by=""):
    return Signal(ts=datetime(2026, 10, 29, 9, 50, 14), code=code, name="中芯国际",
                  side=side, score=score, price=99.70,
                  reason="[bias=1.00 trend_up=True] momentum+1.0; volume+1.0",
                  factors={"momentum": 1.0}, blocked_by=blocked_by)


def test_signal_default_blocked_by_is_empty():
    """默认空字符串 ⇒ 现有构造点（不传该参数）无需改动，语义为「无阻断」。"""
    s = Signal(ts=datetime.now(), code="300308.SZ")
    assert s.blocked_by == "", f"默认值应为空串，实际 {s.blocked_by!r}"


def test_legacy_table_gets_column_on_init():
    """旧库 signals 表没有 blocked_by 列 ⇒ Storage 初始化时应幂等补列。

    这是**生产库的现实情况**：signals 已有 319 万行历史数据，
    不可能重建表；只能 ALTER 补列（历史行该列为 NULL ⇒ 读出来当「无阻断」）。
    """
    db = _tmp_db()
    # 手工造一个「旧版」signals 表（无 blocked_by），模拟升级前的库
    c = sqlite3.connect(db)
    c.execute("""CREATE TABLE signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL, code TEXT NOT NULL, name TEXT, side TEXT NOT NULL,
        score REAL, price REAL, reason TEXT, factors_json TEXT,
        ai_comment TEXT, ai_confidence REAL)""")
    c.execute("INSERT INTO signals(ts,code,side) VALUES ('2026-10-28T09:30:00','X','BUY')")
    c.commit()
    cols_before = [r[1] for r in c.execute("PRAGMA table_info(signals)")]
    assert "blocked_by" not in cols_before
    c.close()

    s = Storage(db)                      # 触发 _migrate
    cols_after = [r[1] for r in s._conn_get().execute("PRAGMA table_info(signals)")]
    assert "blocked_by" in cols_after, f"迁移未补列：{cols_after}"
    # 历史行必须仍在（迁移不得丢数据），且该列为 NULL
    rows = s.get_signals(limit=10)
    assert len(rows) == 1 and rows[0]["code"] == "X"
    assert rows[0].get("blocked_by") is None
    s.close()


def test_migration_is_idempotent():
    """重复初始化同一库不得报错（production 每次启动都会跑一遍 _migrate）。"""
    db = _tmp_db()
    for _ in range(3):
        s = Storage(db)
        s.close()


def test_save_signal_persists_blocked_by():
    db = _tmp_db()
    s = Storage(db)
    _id = s.save_signal(_sig(blocked_by="breadth_lt_2(本轮仅 1 只 BUY < 2)"))
    assert _id > 0, "save_signal 应返回自增 id（调用方靠它事后补写 blocked_by）"
    row = s.get_signals(limit=1)[0]
    assert row["blocked_by"] == "breadth_lt_2(本轮仅 1 只 BUY < 2)"
    s.close()


def test_mark_blocked_updates_only_targeted_rows():
    """只改指定 id；未指定的信号必须保持原样（防止把「已执行」误标成「未成交」）。"""
    db = _tmp_db()
    s = Storage(db)
    a = s.save_signal(_sig(code="688981.SH"))
    b = s.save_signal(_sig(code="300308.SZ"))
    n = s.mark_blocked([a], "breadth_lt_2(本轮仅 1 只 BUY < 2)")
    assert n == 1
    rows = {r["code"]: r for r in s.get_signals(limit=10)}
    assert rows["688981.SH"]["blocked_by"] == "breadth_lt_2(本轮仅 1 只 BUY < 2)"
    assert rows["300308.SZ"]["blocked_by"] == ""
    s.close()


def test_mark_blocked_keeps_original_facts():
    """补写只加 blocked_by，score/reason/side 等原始事实必须原封不动。

    这些字段是「信号当时确实成立过」的证据；一旦被覆盖，
    「究竟是真信号被拦」与「信号本身就不成立」就再也分不清了。
    """
    db = _tmp_db()
    s = Storage(db)
    _id = s.save_signal(_sig())
    before = s.get_signals(limit=1)[0]
    s.mark_blocked([_id], "market_stale(行情快照陈旧)")
    after = s.get_signals(limit=1)[0]
    for k in ("ts", "code", "side", "score", "price", "reason", "factors_json"):
        assert before[k] == after[k], f"补写污染了原始字段 {k}"
    assert after["blocked_by"] == "market_stale(行情快照陈旧)"
    s.close()


def test_mark_blocked_tolerates_empty_and_garbage():
    """空列表 / None / 全 0 必须安静返回 0，不得抛异常。

    调用点在下单主路径的收尾处：一旦抛错就会吃掉整轮建仓，
    而这个字段本身只是可观测性 —— 绝不能让它有能力影响交易。
    """
    db = _tmp_db()
    s = Storage(db)
    for bad in ([], None, [0], [0, 0], ["", None]):
        assert s.mark_blocked(bad, "x") == 0, f"入参 {bad!r} 应返回 0"
    s.close()


def test_breadth_reason_is_greppable_and_self_describing():
    """宽度拦截原因必须是「机器可搜索 + 人能读懂」的形态。

    约定：``breadth_lt_<N>(...人话...)``。
    - 前缀便于日志/SQL 聚合（统计「多少轮被宽度拦下」）；
    - 括号里写清实际只数，避免再出现「仅 4.78（阈值 4.0）」那种反向歧义。
    """
    why = "breadth_lt_2(本轮仅 1 只 BUY < 2)"
    assert why.startswith("breadth_lt_2("), "机器可搜索前缀约定被破坏"
    assert "仅 1 只" in why, "必须写清实际通过只数"
    assert why.endswith(")"), "人话部分需用括号包裹，便于展示层截断"

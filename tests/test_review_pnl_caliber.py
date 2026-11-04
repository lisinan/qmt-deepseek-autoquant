# -*- coding: utf-8 -*-
"""组合总盈亏（``total_pnl``）口径守卫：live 不得把未实现盯市算两遍。

背景（2026-11-03 审计，data-ledger-auditor-2 报告）
----------------------------------------------------
``review_daily.py`` 原来无条件执行::

    total_pnl = risk.daily_pnl + eod.unrealized

但同一个 ``risk.daily_pnl`` 字段在两种模式下**口径相反**：

  - live（``engine/event_engine.py:2133`` 传 ``equity_daily_pnl=True``）：
    ``daily_pnl = 当前总资产 − 当日开盘资产``，**已经包含未平盯市**
    ⇒ 再加 ``unrealized`` 就是双计；
  - paper：``daily_pnl`` 仍是逐笔 SELL 的已实现累加 ⇒ 必须加 ``unrealized``。

实盘铁证（2026-11-03，``logs/review_2026-11-03.json``）::

    realized_eff = -2,134.29   (= 61,073.65 − 63,207.94，含盯市)
    eod.unrealized = -2,134.29 (同一笔钱)
    total_pnl = -4,268.58      ← 恰好 2 倍

反向风险同样致命：**正收益日会虚增收益一倍**，而报表标签写着
「组合总盈亏(已实现+未平盯市)」，读的人不会怀疑。

本文件钉住四条：
  1. live ⇒ ``total_pnl == daily_pnl``（不加未实现），caliber='equity'；
  2. paper / 模式未知 ⇒ 仍为「已实现 + 未平盯市」（旧行为逐位不变）；
  3. 当日执行模式的判定：当日快照 ``mode`` 优先，退到当日成交 ``mode``，
     都没有则返回 ''（**不猜**，按 paper 旧口径处理）；
  4. 报表标签随口径变化 —— 不能指着一根权益曲线说「这是已实现盈亏」。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config.settings  # noqa: E402,F401  （须先于 sqlite3：DLL 路径）
from strategy import review_daily as R  # noqa: E402

# 2026-11-03 真实数字（读自 logs/review_2026-11-03.json，非编造）
D1103_DAILY_PNL = -2134.29
D1103_UNREALIZED = -2134.29
D1103_BEFORE_FIX = -4268.58      # 旧实现输出，恰好 2 倍


# ============================================ ① live：不得重复相加

def test_live_total_pnl_does_not_add_unrealized_again():
    total, caliber = R._compose_total_pnl(
        D1103_DAILY_PNL, D1103_UNREALIZED, "live")
    assert caliber == "equity", caliber
    assert total == D1103_DAILY_PNL, (
        f"live 的 daily_pnl 已含盯市，再加未实现就是双计："
        f"应为 {D1103_DAILY_PNL}，旧实现给 {D1103_BEFORE_FIX}，实际 {total}")
    assert total != D1103_BEFORE_FIX


def test_live_positive_day_is_not_doubled():
    """反向风险：赚钱的日子不能被报成赚两倍。"""
    total, _ = R._compose_total_pnl(1000.0, -200.0, "live")
    assert total == 1000.0, total          # 旧实现会给 800.0（口径错位）


# ============================================ ② paper / 未知：逐位不变

def test_paper_keeps_realized_plus_mtm():
    total, caliber = R._compose_total_pnl(3.5, -100.0, "paper")
    assert caliber == "realized+mtm", caliber
    assert total == -96.5, total


def test_unknown_mode_falls_back_to_legacy():
    """取不到模式时不得猜成 live（paper 的加法才是旧安全行为）。"""
    assert R._compose_total_pnl(3.5, -100.0, "") == (-96.5, "realized+mtm")
    assert R._compose_total_pnl(3.5, -100.0, None) == (-96.5, "realized+mtm")


def test_none_realized_treated_as_zero():
    assert R._compose_total_pnl(None, -50.0, "paper")[0] == -50.0
    assert R._compose_total_pnl(None, -50.0, "live")[0] == 0.0


# ============================================ ③ 模式判定

def _db(snaps=(), with_mode=True):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    if with_mode:
        c.execute("CREATE TABLE equity_snapshots(ts TEXT, mode TEXT, total_asset REAL)")
        c.executemany("INSERT INTO equity_snapshots VALUES(?,?,?)",
                      [(ts, m, 1.0) for ts, m in snaps])
    else:
        c.execute("CREATE TABLE equity_snapshots(ts TEXT, total_asset REAL)")
    return c


def test_day_exec_mode_prefers_snapshot():
    c = _db([("2026-11-03T09:30:00", "live"), ("2026-11-03T15:04:42", "live")])
    assert R._day_exec_mode(c, "2026-11-03") == "live"


def test_day_exec_mode_uses_last_snapshot_of_that_day():
    """模式必须取**当日**快照：前一日 paper、当日 live 时不能沿用 paper。"""
    c = _db([("2026-10-29T15:00:00", "paper"),
             ("2026-10-30T11:29:43", "live")])
    assert R._day_exec_mode(c, "2026-10-30") == "live"
    assert R._day_exec_mode(c, "2026-10-29") == "paper"


def test_day_exec_mode_falls_back_to_fills():
    c = _db([])     # 当日无快照（引擎早退/无 tick）
    fills = [{"mode": "LIVE"}, {"mode": "live"}]
    assert R._day_exec_mode(c, "2026-11-03", fills) == "live"


def test_day_exec_mode_unknown_returns_empty():
    c = _db([])
    assert R._day_exec_mode(c, "2026-11-03") == ""
    # 混合模式（paper→live 当天）无法判定 ⇒ 返回 ''，交由旧口径兜底
    assert R._day_exec_mode(c, "2026-11-03",
                            [{"mode": "paper"}, {"mode": "live"}]) == ""


def test_day_exec_mode_tolerates_old_db_without_mode_column():
    c = _db([], with_mode=False)
    assert R._day_exec_mode(c, "2026-11-03") == "", "旧库无 mode 列不得抛错"


# ============================================ ④ 报表标签随口径

def _rep(caliber):
    return {
        "fills_n": 3, "signals_n": 0, "orders_n": 0, "risk_snapshots_n": 0,
        "notices_n": 0, "fills_history_n": 3, "lookback_days": 120,
        "position_source": "engine_state(权威账本)", "position_warning": None,
        "mode_counts": {"LIVE": 3}, "order_mode_counts": {},
        "pnl": {"realized_total": 0.0, "realized_by_code": {}, "orphan_sells": [],
                "eod_positions": {"300308.SZ": 100}, "eod_avg": {"300308.SZ": 501.5679},
                "cost_basis": 50156.79, "buy_amount": 51584.5, "sell_amount": 0.0},
        "realized_eff": D1103_DAILY_PNL, "total_pnl": D1103_DAILY_PNL,
        "pnl_caliber": caliber,
        "trades": [], "equity": None, "equity_source": "n/a", "equity_series": [],
        "eod": {"total_asset": 61073.65, "cash": 11611.65, "market_value": 49462.0,
                "unrealized": D1103_UNREALIZED, "total_return_pct": -3.38},
        "risk": {"halt_events": [], "halt_count": 0, "halt_reasons": {},
                 "excluded_halt_count": 0, "max_consecutive_losses": 0,
                 "last_daily_pnl": None},
        "compare": {"flags": [], "distinct_codes": ["300308.SZ"],
                    "baseline_summary": {"return_pct": None}},
        "buy_signals": [], "ai_list": [], "notices_sample": [],
        "account_state": {}, "account_range": {}, "account_pnl": None,
        "account_ret_pct": None,
    }


def test_live_caliber_is_labelled_equity_not_realized():
    html = R._render_html("2026-11-03", _rep("equity"))
    assert "当日开盘→收盘" in html, "live 口径必须明示是「当日开盘→收盘」"
    assert "不含隔夜跳空" in html, (
        "必须写明不含隔夜跳空 —— 11-02 日内 −1,475.86 vs 跨日 −476.44 差 999.42")
    assert "当日权益变动" in html
    assert "已实现(引擎累计 daily_pnl)" not in html, (
        "live 的 daily_pnl 不是已实现盈亏，标签不能这么写")
    assert "已含在上式中，勿重复相加" in html, "必须显式提示不得再加未实现"
    assert "账本的 daily_ret_pct 是跨日口径" in html, "必须点明与账本口径的差异"


def test_paper_caliber_keeps_old_label():
    html = R._render_html("2026-11-03", _rep("realized+mtm"))
    assert "已实现(引擎累计 daily_pnl)" in html, "paper 口径标签必须保持原样"
    assert "当日权益变动" not in html


# ============================================ ⑤ 接线（2026-11-04 失效事故）
#
# 教训：①②③④ 全绿，但线上仍然双计 —— 因为 `_compose_total_pnl` 本身是对的，
# 错在**调用点把已经 close() 的连接**传给了 `_day_exec_mode`：函数内
# `except Exception: pass` 把 `sqlite3.ProgrammingError` 吞掉 ⇒ 返回 ''
# ⇒ 回落 paper 口径 ⇒ live 双计。**测了纯函数，没测接线**，等于没修。
# 这四条钉住接线：判定必须在 close 之前，且关闭后必须"看得见"降级。

def test_day_exec_mode_on_closed_connection_degrades_silently():
    """记录陷阱本身：关闭的连接不会抛错给调用方，只会静默返回 ''。"""
    c = _db([("2026-11-03T15:04:42", "live")])
    assert R._day_exec_mode(c, "2026-11-03") == "live"
    c.close()
    assert R._day_exec_mode(c, "2026-11-03") == "", (
        "若这里开始抛错，说明 _day_exec_mode 已改为显式失败 —— "
        "那也是可接受的，但必须同步改 main() 的调用顺序")


def test_main_judges_mode_before_closing_connection():
    """接线守卫：main() 里**所有** `_day_exec_mode(...)` 调用都必须先于 `c.close()`。

    用 AST 而不是字符串 index —— 注释里也会出现 ``c.close()``/``_day_exec_mode(``，
    字符串比对会被自己的注释骗过（第一版就是这么假阳性的）。
    """
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(R.main)))
    mode_lines, close_lines = [], []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Name) and f.id == "_day_exec_mode":
            mode_lines.append(n.lineno)
        elif isinstance(f, ast.Attribute) and f.attr == "close":
            close_lines.append(n.lineno)
    assert mode_lines, "main 必须自己判定当日模式（不能只靠 fills 的 mode）"
    assert close_lines, "main 必须显式关闭连接"
    assert max(mode_lines) < min(close_lines), (
        f"`_day_exec_mode` 传的是 c，必须全部在 close() 之前调用"
        f"（mode@{mode_lines} vs close@{close_lines}）；否则 ProgrammingError "
        f"被 `except Exception: pass` 静默吞掉 → 回落 paper → live 的 total_pnl 双计。"
        f"11-04 事故原文即 `_eff_mode = _day_exec_mode(c, target, fills)` 写在 close 之后")



def test_default_target_date_prefers_today_when_trading_day():
    """零成交日必须复盘「今天」；否则会静默覆盖上一日的 review 文件。"""
    c = _db([])
    got = R._default_target_date(c)
    from datetime import date
    from core.market_calendar import is_trading_day
    expect = date.today().isoformat() if is_trading_day(date.today()) else got
    assert got == expect


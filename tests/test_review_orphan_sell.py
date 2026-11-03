# -*- coding: utf-8 -*-
"""孤儿卖出（找不到建仓腿的 SELL）账本回归测试。

背景（2026-11-03 修复的 P0 账本缺陷，源自真实事故）
-----------------------------------------------------
2026-11-02 卖出 ``300308.SZ ×100 @548.61 = 54,861 元`` —— 该仓位是 OWNER 手工
买回的，``fills`` 表里没有对应 BUY，于是：

  ① ``_replay_fills``：``cur=pos.get(code,{"qty":0})`` ⇒ ``q = min(100, 0) = 0``
     ⇒ ``pnl = (price - 0.0) * 0 = 0.0`` ⇒ ``realized[code] += 0.0``
     —— 把「盈亏不可判定」静默伪装成「这笔没赚钱」（真实约 −1,966 元）；
     而 ``sell_amt += amount`` 是**无条件**的 ⇒ 成交额进了、盈亏归零。
  ② ``_match_trades``：``lots[code]`` 为空 ⇒ while 空转 ⇒ ``entry_qty=0``
     ⇒ ``if entry_qty > 0`` 不成立 ⇒ **整笔静默丢弃**（fills 3 笔卖出 /
     trades 只有 2 笔，54,861 元的真实成交在报表里凭空消失）。
  ③ ``tests/test_batch_c_fixes.py`` 有一条断言把①的「丢弃」写成了期望行为，
     修缺陷时 CI 会亮假绿灯。

修复后的契约（本文件逐条钉死）：
  1. 孤儿卖出 ``pnl is None`` 而非 ``0``；不进 ``realized_by_code``；
     单独进 ``orphan_sells``（含 code/date/qty/price/amount/reason）。
  2. 该笔必须出现在 ``trades`` 里且 ``orphan=True``；
     ``trades`` 长度 == 当日 SELL 笔数。
  3. ``sell_amount`` 仍包含它 —— 成交额是真实发生的现金流，不能丢。
  4. 正常配对（有 BUY 有 SELL）的 ``pnl`` 与修改前**逐位相同**
     （下面用的是 2026-11-02 真实库里的 510050 两笔：2.80 / 0.70）。
  5. 孤儿卖出必须打 WARNING（禁止静默），报表渲染不得因 ``pnl=None`` 崩溃。
"""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy import review_daily as R           # noqa: E402

TARGET = "2026-11-02"


def _fill(ts, code, side, qty, price, mode="live"):
    return {"ts": ts, "code": code, "side": side, "quantity": qty,
            "price": price, "amount": qty * price, "mode": mode}


def _real_1102_fills():
    """2026-11-02 真实成交（storage/qmt.db，只读抄录，非测试桩）。

    510050.SH 有两笔正常卖出（建仓腿 2026-10-30 BUY 500 @2.86），
    300308.SZ 那笔是孤儿（OWNER 手工建仓，fills 无对应 BUY）。
    """
    return [
        _fill("2026-10-30T10:07:45", "510050.SH", "BUY", 500, 2.86),
        _fill("2026-11-02T10:34:13", "510050.SH", "SELL", 400, 2.867),
        _fill("2026-11-02T10:34:13", "510050.SH", "SELL", 100, 2.867),
        _fill("2026-11-02T10:35:13", "300308.SZ", "SELL", 100, 548.61),
    ]


# ===================================== ① pnl 必须是 None，不是 0

def test_orphan_sell_pnl_is_none_not_zero():
    r = R._replay_fills(_real_1102_fills(), target=TARGET)
    assert "300308.SZ" not in r["realized_by_code"], (
        f"孤儿卖出不得进 realized_by_code（旧行为是 0.0，把「不知道」伪装成"
        f"「没赚钱」）：{r['realized_by_code']}")
    o = [x for x in r["orphan_sells"] if x["code"] == "300308.SZ"]
    assert len(o) == 1, f"孤儿卖出应单独记录，实际 {r['orphan_sells']}"
    o = o[0]
    assert o["date"] == TARGET
    assert o["qty"] == 100
    assert abs(o["price"] - 548.61) < 1e-9
    assert abs(o["amount"] - 54861.0) < 1e-9
    assert "找不到建仓腿" in o["reason"], o
    # 已实现合计只含正常配对（510050），孤儿不参与求和
    assert r["realized_total"] == 3.5, r["realized_total"]


# ===================================== ② trades 必须保留该笔 + orphan=True

def test_orphan_sell_appears_in_trades_with_orphan_flag():
    fills = _real_1102_fills()
    trades = R._match_trades(fills, target=TARGET)
    sell_n = len([f for f in fills
                  if f["side"] == "SELL" and str(f["ts"]).startswith(TARGET)])
    assert len(trades) == sell_n, \
        f"trades 长度必须等于当日 SELL 笔数（{sell_n}），实际 {len(trades)}：孤儿被丢弃了"
    orph = [t for t in trades if t.get("orphan")]
    assert len(orph) == 1, f"应恰好 1 笔孤儿，实际 {trades}"
    t = orph[0]
    assert t["code"] == "300308.SZ"
    assert t["exit_ts"].startswith(TARGET)
    assert t["pnl"] is None, f"孤儿 pnl 必须不可判定（None），实际 {t['pnl']}"
    assert t["entry_qty"] is None and t["entry_price"] is None and t["entry_ts"] is None
    assert abs(t["exit_price"] - 548.61) < 1e-9 and t["qty"] == 100
    # 正常配对的两笔不得被误标
    assert all(not x["orphan"] for x in trades if x["code"] == "510050.SH"), trades


# ===================================== ③ 成交额不能丢

def test_sell_amount_still_includes_orphan():
    r = R._replay_fills(_real_1102_fills(), target=TARGET)
    expected = 400 * 2.867 + 100 * 2.867 + 100 * 548.61
    assert r["sell_amount"] == round(expected, 2), \
        f"卖出金额必须仍包含孤儿那笔（不得因修 pnl 而丢成交额）：{r['sell_amount']} vs {expected}"
    assert r["sell_amount"] == 56294.5, r["sell_amount"]
    assert r["buy_amount"] == 0.0, "当日无买入"


# ===================================== ④ 正常配对 pnl 逐位不变

def test_normal_pairing_pnl_bit_identical():
    """正常配对的盈亏必须与修复前逐位相同（钉死「只影响孤儿行」）。"""
    r = R._replay_fills(_real_1102_fills(), target=TARGET)
    # 修复前实测（真实库 2026-11-02）：300308=0.0，510050=3.5（2.8+0.7）
    assert r["realized_by_code"] == {"510050.SH": 3.5}, r["realized_by_code"]
    assert r["realized_total"] == 3.5, r["realized_total"]

    trades = R._match_trades(_real_1102_fills(), target=TARGET)
    pnls = [t["pnl"] for t in trades if t["code"] == "510050.SH"]
    assert pnls == [2.8, 0.7], \
        f"正常配对逐笔盈亏必须与修复前逐位相同（[2.8, 0.7]），实际 {pnls}"

    # 单笔满仓 round-trip：100 股 100→110 仍是精确 1000.0（非近似）
    simple = [_fill("2026-08-31T10:00:00", "X", "BUY", 100, 100.0),
              _fill("2026-09-01T14:00:00", "X", "SELL", 100, 110.0)]
    assert R._match_trades(simple) == [{
        "code": "X", "entry_ts": "2026-08-31T10:00:00",
        "exit_ts": "2026-09-01T14:00:00", "entry_price": 100.0,
        "exit_price": 110.0, "qty": 100, "entry_qty": 100, "pnl": 1000.0,
        "holding_days": 1, "return_pct": 10.0, "entry_mode": "live",
        "exit_mode": "live", "orphan": False,
    }], "正常配对的交易结构不得改变"


# ===================================== ⑤ 禁止静默 + 报表不得崩

def test_orphan_sell_emits_warning():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        R._replay_fills(_real_1102_fills(), target=TARGET)
    out = buf.getvalue()
    assert "WARNING" in out, f"孤儿卖出必须打 WARNING，实际输出 {out!r}"
    assert "孤儿卖出：找不到建仓腿，pnl 不可判定" in out, out
    assert "300308.SZ" in out and TARGET in out, out
    assert "54,861.00" in out, f"告警必须带成交额，实际 {out!r}"


def test_render_html_handles_orphan_without_crash():
    """``pnl=None`` 若直接喂 ``{:,.2f}`` 会抛 TypeError —— 必须渲染为「不可判定」。"""
    pnl = R._replay_fills(_real_1102_fills(), target=TARGET)
    rep = {
        "fills_n": 3, "signals_n": 0, "orders_n": 0, "risk_snapshots_n": 0,
        "notices_n": 0, "fills_history_n": 4, "lookback_days": 120,
        "position_source": "engine_state(权威账本)", "position_warning": None,
        "mode_counts": {"LIVE": 3}, "order_mode_counts": {},
        "pnl": pnl,
        "realized_eff": pnl["realized_total"], "total_pnl": 0.0,
        "trades": R._match_trades(_real_1102_fills(), target=TARGET),
        "equity": None, "equity_source": "n/a", "equity_series": [],
        "eod": {"total_asset": 63207.98, "cash": 63207.98, "market_value": 0.0,
                "unrealized": 0.0, "total_return_pct": -2.28},
        "risk": {"halt_events": [], "halt_count": 0, "halt_reasons": {},
                 "excluded_halt_count": 0, "max_consecutive_losses": 0,
                 "last_daily_pnl": None},
        "compare": {"flags": [], "distinct_codes": ["300308.SZ", "510050.SH"],
                    "baseline_summary": {"return_pct": None}},
        "buy_signals": [], "ai_list": [], "notices_sample": [],
        "account_state": {}, "account_range": {}, "account_pnl": None,
        "account_ret_pct": None,
    }
    html_out = R._render_html(TARGET, rep)
    assert "不可判定" in html_out, "孤儿卖出在报表里必须显示「不可判定」，而不是 0.00"
    assert "孤儿" in html_out
    assert "548.61" in html_out, "孤儿那笔的价格/成交额必须在报表里可见"


# ===================================== ⑥ 无孤儿时不产生噪声

def test_no_orphan_returns_empty_list():
    fills = [_fill("2026-08-31T10:00:00", "X", "BUY", 100, 100.0),
             _fill("2026-09-01T14:00:00", "X", "SELL", 100, 110.0)]
    r = R._replay_fills(fills, target="2026-09-01")
    assert r["orphan_sells"] == [], r["orphan_sells"]
    assert r["realized_by_code"] == {"X": 1000.0}, r["realized_by_code"]

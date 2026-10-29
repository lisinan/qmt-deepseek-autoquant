# -*- coding: utf-8 -*-
"""【2026-10-29】「为什么没买」播报诚实性守卫。

## 起因：同日上午的一次真实误判（中芯国际 688981.SH）

用户问「实时信号显示中芯国际 BUY 方向，为什么没开仓」。查下来：

  * `signals` 表当日 **只有一只** BUY —— 688981.SH，重复 **24 轮**，最高分 **4.78**
  * 生产 `buy_score_threshold = 4.0` ⇒ 4.78 **已经过阈值**，信号本来就是有效的 BUY
  * 真正拦住它的是下游**宽度确认**：`min_passing_candidates = 2`，本轮只有 1 只
    ⇒ 日志原话「[宽度确认] 本轮 BUY 信号仅 1 只 < 2 ⇒ 当日不开新仓」

而同一时刻 `_maybe_log_no_signal` 输出的却是：

    「无入场信号：候选 N 只、最高分 688981.SH **仅** 4.78（阈值 4.0）原因=…」

**4.78 > 4.0，它明明通过了阈值。** 文案里那个「仅」把「过线了但被闸门挡住」
说成了「分数不够」，直接把排查引向 `buy_score_threshold` —— 而 R40 已经
逐档证明该参数 >4.0 全负、≤4.0 惰性，**根本无解**。真凶在下一行。

## 本文件锁死的语义

  A. 本轮真下过单（`n_ok > 0`）⇒ 静默，且**不占用** 600s 节流额度
  B. 有 BUY 但零成交 ⇒ 播报**拦截层**（宽度确认 / 持仓已满 / 拒绝原因原样），
     绝不出现评分口径的措辞
  C. 无 BUY ⇒ 按策略真实 reason 前缀判层，只有真的低于阈值才允许说「不足」
  D. 源码守卫：误导性的「最高分仅」字面量不得再出现；
     且调用点必须把 n_buy / n_ok / stage 三个事实传进来（否则本修复会退化）
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import engine.event_engine as EE  # noqa: E402


class _LogRec:
    """把 event_engine 的 logger 临时换掉，记录 (level, text)。

    本 runner 不用 pytest，没有 caplog；而 `logger` 是模块级全局，
    函数体里的 `logger.warning(...)` 在调用时才解析 ⇒ 替换 EE.logger 即生效。
    """

    def __init__(self):
        self.rows = []

    def _cap(self, level):
        def _f(msg, *a, **k):
            try:
                text = (msg % a) if a else str(msg)
            except Exception:
                text = f"{msg} | args={a!r}"
            self.rows.append((level, text))
        return _f

    def __getattr__(self, name):
        return self._cap(name)

    def joined(self) -> str:
        return " || ".join(t for _lv, t in self.rows)


def _eng():
    """不入 __init__ 的半成品引擎：本函数只依赖 _no_entry_reason / 节流时间戳。"""
    e = EE.EventEngine.__new__(EE.EventEngine)
    e._last_no_signal_log_ts = 0.0
    e._no_entry_reason = ""
    e.daily = None
    return e


def _call(eng, **kw):
    rec = _LogRec()
    old = EE.logger
    EE.logger = rec
    try:
        eng._maybe_log_no_signal(**kw)
    finally:
        EE.logger = old
    return rec, eng._no_entry_reason


# ============================================ A. 真下过单 ⇒ 静默

def test_ordered_this_round_logs_nothing_and_keeps_throttle():
    """★ 门锁 1：本轮确实成交了 ⇒ 一条都不许播，且不消耗节流额度。

    改前这个函数是**无条件**每 10 分钟播一次的（哪怕刚买进 3 只），
    标题却叫「无入场信号」。本次顺带修正：既然后面 `n_ok` 能数出来，
    就不该在有成交时继续喊「没信号」。
    """
    eng = _eng()
    rec, reason = _call(eng, n_cand=23, best=("A", 4.8, ""), regime_ok=True,
                        n_buy=2, n_ok=2)
    assert rec.rows == [], f"有成交时不得播报，实际: {rec.rows}"
    assert reason == "", "有成交时不改写 no_entry_reason"
    assert eng._last_no_signal_log_ts == 0.0, (
        "不得占用 600s 节流额度——否则真正需要的那条告警会被挤掉")


# ============================================ B. 有 BUY 但零成交

def test_breadth_block_names_the_gate_not_the_score():
    """★ 门锁 2（复现 2026-10-29 中芯国际）：必须指名宽度确认。

    关键断言是「不得出现评分口径的措辞」：4.78 已过 4.0，说它「仅 4.78」
    等于谎报，且会把排查引向已被 R40 全档证伪的 `buy_score_threshold`。
    """
    eng = _eng()
    stage = "breadth_lt_2(本轮仅 1 只 BUY < 2)"
    rec, reason = _call(
        eng, n_cand=23,
        best=("688981.SH", 4.78, "[bias=-0.12 trend_up=True] momentum+1.0"),
        regime_ok=True, n_buy=1, n_ok=0, stage=stage)

    text = rec.joined()
    assert "breadth_lt_2" in text, f"必须指出真实拦截层，实际: {text}"
    assert "全部未成交" in text, text
    assert "4.78" in text, "最高分仍应露出，便于核对"
    assert "已达标" in text, "必须明确说出「分数是够的」，这是本修复的要点"
    for bad in ("最高分仅", "仅 4.78", "评分不足", "分数不够"):
        assert bad not in text, f"零成交场景不得用评分措辞「{bad}」: {text}"
    assert reason.startswith("BUY 1 只全部未成交"), reason


def test_max_positions_block_is_reported_verbatim():
    """★ 门锁 3：持仓已满（另一种零成交）同样只报拦截层。"""
    eng = _eng()
    rec, reason = _call(eng, n_cand=23, best=("B", 5.1, ""), regime_ok=True,
                        n_buy=2, n_ok=0, stage="max_positions(并发持仓已满)")
    text = rec.joined()
    assert "max_positions(并发持仓已满)" in text, text
    assert "最高分仅" not in text, text
    assert "持仓已满" in reason, reason


def test_missing_stage_does_not_invent_a_score_blame():
    """★ 门锁 4（防过度修正）：stage 缺失时宁可说「未记录」，也不许编造评分不足。"""
    eng = _eng()
    rec, reason = _call(eng, n_cand=23, best=("C", 4.9, ""), regime_ok=True,
                        n_buy=1, n_ok=0)
    text = rec.joined()
    assert "未记录具体拦截层" in text, text
    assert "评分不足" not in text and "最高分仅" not in text, text


# ============================================ C. 无 BUY ⇒ 按真实 reason 判层

def test_no_buy_daily_gate_is_not_called_score_shortage():
    """★ 门锁 5：`daily-gate` ⇒ 分数够、缺 trend_up，不得说「评分不足」。

    这正是 2026-10-16 那次诊断的形态：34 只里评分层有 3 只达标，
    全被日线闸门（`above_ma60` 0/34）挡住。
    """
    eng = _eng()
    rec, reason = _call(eng, n_cand=23,
                        best=("D", 4.78, "daily-gate:bias=-0.50 trend_up=False"),
                        regime_ok=True, n_buy=0, n_ok=0)
    text = rec.joined()
    assert "日线闸门" in text, text
    assert "评分不足" not in text, f"daily-gate 场景不得归咎评分: {text}"
    assert "分数够" in text, text
    assert "日线闸门" in reason, reason


def test_no_buy_score_below_threshold_says_insufficient():
    """★ 门锁 6：确实低于阈值 ⇒ 这时才可以（也应该）说「不足」。"""
    eng = _eng()
    rec, reason = _call(eng, n_cand=23, best=("E", 3.10, "score<4.0"),
                        regime_ok=True, n_buy=0, n_ok=0)
    text = rec.joined()
    assert "评分不足" in text, text
    assert "3.10" in text, text
    assert "评分不足" in reason, reason


def test_no_buy_positive_factor_shortage_is_distinguished():
    """★ 门锁 7：`sigs<` 是「分数够但正因子数不足」，与 score< 不同层。

    生产 min_signals=3；R41 已证 >3 单调劣、≤3 惰性，故这是纯诊断信息。
    """
    eng = _eng()
    rec, reason = _call(eng, n_cand=23, best=("F", 4.60, "sigs<3"),
                        regime_ok=True, n_buy=0, n_ok=0)
    text = rec.joined()
    assert "正因子数不足" in text, text
    assert "评分不足" not in text, f"score 已过阈值，不得说评分不足: {text}"
    assert "分数够" in text, text


def test_regime_off_is_reported_before_any_score_talk():
    """★ 门锁 8：regime 门关闭时根本没评估候选 ⇒ 不许拿最高分说事。"""
    eng = _eng()
    rec, reason = _call(eng, n_cand=23, best=("G", 6.00, ""), regime_ok=False,
                        n_buy=0, n_ok=0)
    text = rec.joined()
    assert "regime" in text, text
    assert "6.00" not in text, "regime 关闭时播报最高分无意义，会误导"
    assert "regime" in reason, reason


# ============================================ D. 回归保护

def test_throttle_still_applies_when_not_ordered():
    """★ 门锁 9：分流之后节流照旧，不许把日志刷成噪声（会脱敏）。"""
    eng = _eng()
    rec1, _ = _call(eng, n_cand=23, best=("H", 3.0, "score<4.0"),
                    regime_ok=True, n_buy=0, n_ok=0)
    assert len(rec1.rows) == 1, rec1.rows
    rec2, _ = _call(eng, n_cand=23, best=("H", 3.0, "score<4.0"),
                    regime_ok=True, n_buy=0, n_ok=0)
    assert rec2.rows == [], f"10 分钟内第二次调用应被节流，实际: {rec2.rows}"


def test_empty_universe_keeps_funnel_text():
    """★ 门锁 10（防退化）：候选池为空仍必须带漏斗分层（10-16 的修复不许被本轮吃掉）。"""
    eng = _eng()
    rec, reason = _call(eng, n_cand=0, best=None, regime_ok=True)
    text = rec.joined()
    assert "漏斗分层" in text, text
    assert "min_mom=" in text, text
    assert "动量闸门后候选池为空" in reason, reason


def test_source_guard_no_misleading_wording_and_call_site_passes_facts():
    """★ 门锁 11：源码级守卫 —— 两处一旦回退，本修复即静默失效。

    ① `_maybe_log_no_signal` 里不得再出现「最高分仅」这个谎报字面量；
    ② `_run_single_step` 的调用点必须把 n_buy / n_ok / stage 传进来。
       若只剩三个老参数，函数会退化成「永远 n_buy=0」⇒ 又回到
       「有 BUY 却说分数不够」的老路，而所有功能测试仍会绿。
    """
    src = (ROOT / "engine" / "event_engine.py").read_text(encoding="utf-8")
    body = src.split("def _maybe_log_no_signal")[-1]
    # 先剥掉函数 docstring：本轮修复说明里**有意引用**了旧文案作反例
    #（"日志说「最高分仅 4.78」"），那是注释不是活文案，不该被本条守卫命中。
    _q1 = body.find('"""')
    _q2 = body.find('"""', _q1 + 3) if _q1 >= 0 else -1
    code = body[_q2 + 3:] if _q2 > 0 else body
    assert "最高分仅" not in code, (
        "「最高分仅 X（阈值 Y）」在 X 已过阈值时是谎报（2026-10-29 中芯国际）")
    assert "n_ok > 0" in code, "必须有「本轮已成交 ⇒ 静默」的分流"
    assert "n_buy > 0" in code, "必须有「有 BUY 但零成交 ⇒ 报拦截层」的分流"

    assert "n_buy=_buy_n" in src and "n_ok=_ok_n" in src and "stage=_stage" in src, (
        "调用点必须传 n_buy / n_ok / stage；否则本修复只是改了文案、事实没接上")
    assert "_buy_n += 1" in src, "_buy_n 必须在 BUY 分支真实累加"

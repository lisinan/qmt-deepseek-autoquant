# -*- coding: utf-8 -*-
"""活性三态契约守卫（R49-RSI 递归自进化，2026-11-01）。

【本测试存在的唯一理由】
旧 L3 只比 `memory.md` 的 mtime（距今 > 3 天 = 停摆），于是 2026-10-29 切 live 后
真实发生的事它一件都看不见：

  · OBSERVE 10-30 **写了** memory「⛔ 硬闸门拦截，整体停机告警」，什么都没做；
    旧 L3 判「✓ 运行中」 ⇒ 真实资金账户 2 个交易日没有盘后复盘，而活性报告全绿。
  · PRE-MARKET 10-30 09:05 干脆连 memory 都没写；机器醒着（心跳 17 条 / 空档 6 分钟），
    旧 L3 只能报「停摆」，无法回答「是机器睡了还是任务自己停了」。

⇒ 把「活性」变成可计算事实：三态（①正常产出 / ②运行但自我停机 / ③完全未运行）
  + 归因（prompt 门禁 / 整机休眠 / 醒着零留痕）。

【断言纪律（第 19 条：钉契约不钉副产品）】
  1. 全部用**合成夹具**（临时目录 + 人造 memory/产物 + 人造心跳），不依赖本机
     真实 mtime、不依赖真实 guardian.log —— 否则换台机器就红/绿乱跳。
  2. 必须包含**退化的反例**：把「机器休眠」和「自我停机」对调，断言判定跟着变。
     否则「三态」可以退化成「恒返回某一态」而测试仍绿（第 6 条正向对照）。
  3. 必须钉住「同一逻辑不得出现第二份实现」（第 24 条）：主审计脚本只 import，
     不得自带 `memory.md mtime > 3 天` 那套旧判据。
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import _loop_liveness as LL  # noqa: E402

# 固定「现在」，避免用例随真实时钟漂移。
#   NOW   = 2026-11-01 21:00（周日）—— 用于 weekly/hourly 槽位用例
#   NOW_D = 2026-10-30 23:30（周五）—— 用于 daily 三态用例（当日 17:00 槽位已过）
NOW = datetime(2026, 11, 1, 21, 0)
NOW_D = datetime(2026, 10, 30, 23, 30)
ALL_DAYS = lambda d: True                       # noqa: E731 测试用：每天都是交易日


# --------------------------------------------------------------------- 夹具
def _mk_memory(base: Path, uid: str, text: str, mtime: datetime) -> None:
    d = base / f"{uid}-test"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "memory.md"
    p.write_text(text, encoding="utf-8")
    ts = mtime.timestamp()
    os.utime(p, (ts, ts))


def _beats(start: datetime, end: datetime, step_min: int = 5) -> list:
    out, t = [], start
    while t <= end:
        out.append(t)
        t += timedelta(minutes=step_min)
    return out


def _task(uid="98ffc7a2", cadence="daily", at="09:05", product=(),
          grace_min=45, ignore_markers=(), hours=(), weekday=None):
    return LL.Task(uid, "测试任务", cadence, at, weekday=weekday, hours=hours,
                   product=product, grace_min=grace_min,
                   ignore_markers=ignore_markers)


# --------------------------------------------------------------- 槽位计算
def test_slot_skips_current_run_within_grace():
    """正在跑的本次槽位不能算「没跑」——否则每周日的 RSI 会自己报自己停摆。"""
    t = _task(cadence="weekly", at="21:00", weekday=6, grace_min=90)
    # now=周日 21:00 → 本槽位在 grace 内 ⇒ 应取上周日
    assert LL.expected_slot(t, NOW, ALL_DAYS) == datetime(2026, 10, 25, 21, 0)
    # 90 分钟后再看，本次槽位已成「该跑完」⇒ 取 11-01
    assert LL.expected_slot(t, NOW + timedelta(minutes=91), ALL_DAYS) == \
        datetime(2026, 11, 1, 21, 0)


def test_daily_slot_uses_trading_day_predicate():
    """daily 槽位必须走交易日谓词；全 false 时不应产出任何槽位（防把休市当漏跑）。"""
    t = _task(at="09:05")
    assert LL.expected_slot(t, NOW_D, ALL_DAYS) == datetime(2026, 10, 30, 9, 5)
    assert LL.expected_slot(t, NOW_D, lambda d: False) is None


def test_hourly_slot_respects_declared_hours():
    """GUARD 的 rrule 是全天的，但职责只到 15:00 ⇒ 槽位不得跨出 hours。"""
    t = _task(uid="ebde5d30", cadence="hourly", at="00:00",
              hours=tuple(range(9, 17)), grace_min=20)
    # 周日 21:00 + 每天都是交易日 ⇒ 当天 16:00 是最近槽位（17~23 点不在职责内）
    assert LL.expected_slot(t, NOW, ALL_DAYS) == datetime(2026, 11, 1, 16, 0)


# --------------------------------------------------------------- 心跳归因
def test_window_awake_true_when_heartbeats_continuous():
    beats = _beats(NOW - timedelta(hours=1), NOW + timedelta(hours=1))
    r = LL.window_awake(beats, NOW - timedelta(minutes=30), NOW)
    assert r["awake"] is True and r["max_gap_min"] <= 15


def test_window_awake_false_on_long_gap_even_if_edges_present():
    """窗口两端都有心跳、中间断了 6 小时 —— 必须判「休眠」。

    这是 2026-10-30 的真实形状（11:29 最后一条 → 17:24 才恢复），
    只数「窗口内有无心跳」会把 6 小时休眠判成醒着。
    """
    start, end = NOW - timedelta(hours=6), NOW - timedelta(hours=5)
    # 窗口（含 10 分钟 pad）两端各一条心跳，中间 6 小时空档都没人值班
    beats = [start - timedelta(minutes=9), end + timedelta(minutes=9)]
    r = LL.window_awake(beats, start, end)
    assert r["awake"] is False and r["max_gap_min"] > 15


def test_window_awake_false_when_no_heartbeats():
    r = LL.window_awake([], NOW, NOW + timedelta(minutes=30))
    assert r["awake"] is False and r["max_gap_min"] is None


# --------------------------------------------------------------- 三态判定
def test_state_halted_when_memory_says_stopped_and_no_product(tmp_path=None):
    """② —— 旧 L3 的漏报原型：memory 更新了（判「运行中」），但内容是自己停机了。

    真实文案取自 2026-10-30 OBSERVE 的 memory。
    """
    base = Path(tempfile.mkdtemp(prefix="l3_halt_"))
    _mk_memory(base, "ae0e2431",
               "## 2026-10-30 15:35 运行（OBSERVE · ⛔ 硬闸门拦截，整体停机告警）\n"
               "- ⛔ Step 1 闸门未通过：不执行 Step 2-4，不产出任何新报告。\n",
               datetime(2026, 10, 30, 15, 40))
    t = _task(uid="ae0e2431", at="15:35", product=("reports/__nonexistent__/*.md",))
    row = LL.classify_task(t, NOW_D, base=base, beats=[], trade_day=ALL_DAYS)
    assert row["state"] == LL.STATE_HALTED, row["evidence"]
    assert row["verdict"] == "prompt_gate"
    assert "硬闸门" in row["halt_markers"]
    assert row["stale"] is True


def test_state_normal_when_product_present_despite_gate_wording():
    """反向对照：同样含「门禁」字样，但窗口内有产物 ⇒ 不能判 ②。

    否则任何「如实报告门禁已解除」的正常 run 都会被误报（误报比漏报更伤人）。
    """
    base = Path(tempfile.mkdtemp(prefix="l3_ok_"))
    _mk_memory(base, "ae0e2431",
               "## 2026-10-30 15:40 运行\n- 门禁已按新契约改为模式感知，整体停机不再触发。\n",
               datetime(2026, 10, 30, 15, 45))
    prod = ROOT / "reports" / "__l3_fixture__.md"      # 临时产物，用完即删
    ts = datetime(2026, 10, 30, 15, 45).timestamp()
    prod.write_text("x", encoding="utf-8")
    os.utime(prod, (ts, ts))
    try:
        t = _task(uid="ae0e2431", at="15:35", product=("reports/__l3_fixture__.md",))
        row = LL.classify_task(t, NOW_D, base=base, beats=[], trade_day=ALL_DAYS)
        assert row["state"] == LL.STATE_NORMAL, row["evidence"]
        assert row["product"] and "__l3_fixture__" in row["product"]
    finally:
        prod.unlink(missing_ok=True)


def test_state_missed_machine_sleep_vs_prompt_gate():
    """③ 的两种归因必须可区分 —— 这是本轮要修的第二半。

    同一份「没跑」的事实，喂不同心跳得到不同归因：
      · 窗口无心跳  → machine_sleep（该改时段/叫醒机器）
      · 心跳连续    → awake_no_artifact（该改 prompt/排期）
    """
    base = Path(tempfile.mkdtemp(prefix="l3_miss_"))
    _mk_memory(base, "98ffc7a2", "## 2026-10-29 09:31 执行\n- 一切正常\n",
               datetime(2026, 10, 29, 9, 31))
    t = _task(at="09:05")

    slept = LL.classify_task(t, NOW_D, base=base, beats=[], trade_day=ALL_DAYS)
    assert slept["state"] == LL.STATE_MISSED
    assert slept["verdict"] == "machine_sleep", slept["evidence"]

    awake_beats = _beats(datetime(2026, 10, 30, 8, 0),
                         datetime(2026, 10, 30, 10, 0), step_min=5)
    awake = LL.classify_task(t, NOW_D, base=base, beats=awake_beats, trade_day=ALL_DAYS)
    assert awake["state"] == LL.STATE_MISSED
    assert awake["verdict"] == "awake_no_artifact", awake["evidence"]
    assert awake["missed_slots"] >= 1


def test_guard_false_alarm_marker_is_not_halted():
    """GUARD 每小时打 `[GUARDIAN-ALERT]` 是契约已判定的**假告警**：它做了 health_check。

    若把它算进停机判据，L3 会每小时误报一次 ② —— 那正是本项目花了两天纠正的病
    （用假告警把安全网自己关掉）。
    """
    base = Path(tempfile.mkdtemp(prefix="l3_guard_"))
    _mk_memory(base, "ebde5d30",
               "## 2026-10-30 22:32 (run #N+204)\n"
               "- paper 守卫告警：EXECUTION_MODE=live，按 GUARD 指令输出 `[GUARDIAN-ALERT]`。\n",
               datetime(2026, 10, 30, 22, 32))
    t = _task(uid="ebde5d30", cadence="hourly", at="00:00",
              hours=tuple(range(9, 17)), grace_min=20,
              ignore_markers=("[GUARDIAN-ALERT]",))
    row = LL.classify_task(t, NOW_D, base=base, beats=[], trade_day=ALL_DAYS)
    assert row["state"] == LL.STATE_NORMAL, row["evidence"]
    assert row["notes"] and "假告警" in row["notes"][0]


def test_product_window_must_hug_the_slot():
    """产物只能属于它自己那个槽位。

    真事故：初期实现用 `[slot-1h, now]` 判产物，把 11-01 工程维护写的
    `PREMARKET_READINESS_20261102.md` 当成 PRE-MARKET 10-30 槽位的产物，
    把一个真实停摆洗成 ①（漏报）。这里用「槽位后 2 天才产出的文件」复现。
    """
    base = Path(tempfile.mkdtemp(prefix="l3_window_"))
    t = _task(uid="98ffc7a2", at="09:05", product=("reports/__l3_late__.md",))
    prod = ROOT / "reports" / "__l3_late__.md"
    ts = datetime(2026, 11, 1, 17, 49).timestamp()   # 槽位 10-30 09:05 之后 2 天
    prod.write_text("x", encoding="utf-8")
    os.utime(prod, (ts, ts))
    try:
        row = LL.classify_task(t, NOW_D, base=base, beats=[], trade_day=ALL_DAYS)
        assert row["state"] == LL.STATE_MISSED, row["evidence"]
        assert row["product"] is None
    finally:
        prod.unlink(missing_ok=True)


# --------------------------------------------------------------- 契约一致性
def test_summarize_counts_three_states():
    rows = [
        {"state": LL.STATE_NORMAL, "name": "a", "state_label": "①", "verdict_label": "—"},
        {"state": LL.STATE_HALTED, "name": "b", "state_label": "②", "verdict_label": "x"},
        {"state": LL.STATE_MISSED, "name": "c", "state_label": "③", "verdict_label": "y"},
        {"state": LL.STATE_MISSED, "name": "d", "state_label": "③", "verdict_label": "y"},
    ]
    s = LL.summarize(rows)
    assert s["counts"][LL.STATE_NORMAL] == 1
    assert s["counts"][LL.STATE_HALTED] == 1
    assert s["counts"][LL.STATE_MISSED] == 2
    assert len(s["abnormal"]) == 3
    assert "② 1" in s["headline"] and "③ 2" in s["headline"]


def test_task_registry_matches_contract_schedule():
    """注册表 = 契约 §五 的时段表。两边任一侧改了没同步，L3 就用错槽位。

    这里只钉「契约里写明的时刻/日期」，不钉实现细节（第 19 条）。
    """
    expect = {
        # 【2026-11-02 归集】10 条 → 3 条。原 7 个工作日任务并入 15:40 DAILY-REVIEW，
        # PM-EVOLVE 并入周日 SYNTHESIZE。被归集的 8 条在自动化侧已 PAUSED，
        # 故不登记（登记已停用的任务 ⇒ 每天 8 条假「未运行」，把真问题淹掉）。
        "d1cdbd85": ("daily", "15:40", None),    # 每日复盘（唯一每日任务）
        "f093d93b": ("weekly", "20:00", 6),      # 周日
        "4a425d4f": ("weekly", "21:00", 6),      # 周日
    }
    for uid, (cad, at, wd) in expect.items():
        t = LL.BY_ID[uid]
        assert (t.cadence, t.at, t.weekday) == (cad, at, wd), f"{uid} 与契约 §五 不一致"
    # 归集后**不得**再登记已停用任务：留一条旧 id 就会每天多报一条假告警。
    for stale in ("98ffc7a2", "ebde5d30", "789d671e", "ae0e2431",
                  "1c6e4605", "6265093f", "2534a859", "d8013e93"):
        assert stale not in LL.BY_ID, (
            f"{stale} 已归集停用，仍在注册表里 ⇒ L3 会每天报一条假「未运行」")
    # 3 个任务都在册（漏一个就等于放弃了那条链路的观测）
    assert len(LL.TASKS) == 3, LL.TASKS


def test_rsi_script_has_no_second_implementation():
    """第 24 条：同一事实两份实现必然静默漂移。

    主审计脚本曾经自带「mtime > 3 天」判据；现在必须只 import 本模块。
    """
    src = (ROOT / "scripts" / "_recursive_self_improve.py").read_text(
        encoding="utf-8", errors="ignore")
    assert "_loop_liveness" in src, "主审计脚本未复用 scripts/_loop_liveness.py"
    assert "age > 3.0" not in src, "主审计脚本里仍有旧的 3 天阈值判据（第二份实现）"
    assert "def audit_loop_activity" in src, (
        "调用点被删掉了 —— 那会让 L3 从报告里静默消失（比判错更危险）")


def test_liveness_module_is_read_only():
    """审计模块不得写生产文件（它每周日 21:00 跑在 live 账户旁）。"""
    src = (ROOT / "scripts" / "_loop_liveness.py").read_text(
        encoding="utf-8", errors="ignore").replace("`", "")
    for bad in ("write_text(", "open(\"w", "unlink(", "rename("):
        assert bad not in src, f"_loop_liveness.py 出现写操作：{bad}"

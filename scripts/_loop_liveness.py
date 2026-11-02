# -*- coding: utf-8 -*-
"""循环活性审计（L3）· 活性三态 —— 单一事实源（R49-RSI 2026-11-01）

【为什么必须有这个模块（血泪）】
旧 L3 只读 `memory.md` 的 mtime，判据是「距今 > 3 天 ⇒ 停摆」。它有三个盲区：

  ① **「在跑」≠「在做」**。2026-10-29 15:05 账户切 live 后，10 个自动化里有 5 个
     按各自 prompt 里的旧门禁「整体停机并显著告警」。其中 OBSERVE 确实写了 memory
     （`2026-10-30 15:35 运行（⛔ 硬闸门拦截，整体停机告警）`），旧 L3 判它
     「✓ 运行中」—— 于是**真实资金账户连续 2 个交易日没有盘前体检/盘后复盘**，
     而活性报告全绿。这正是本轮要修的缺陷。
  ② **归因错误**。旧实现把「机器休眠没触发」与「任务自我停机」混成一档，
     修复方向恰好相反（叫醒机器 vs 改 prompt）。
  ③ **阈值与节奏不匹配**。3 天阈值对小时级任务（GUARD）过宽：停 2 天也判正常。

【本模块的做法：三态 + 归因】
  ① normal  正常产出（槽位已过且 memory 已更新，末条无停机标记）
  ② halted  跑了但自我停机（memory 末条含门禁/停机标记 **且** 窗口内无产物）
  ③ missed  完全未运行（该有的槽位过去了，memory 没更新、窗口内也无产物）

  ②③ 再归因（判据见 `docs/AUTOMATION_CONTRACT.md` §4.3）：
    · prompt_gate       —— memory 里自己写了门禁/停机（② 的默认归因）
    · machine_sleep     —— `logs/guardian.log` 在槽位窗口内无 300s 心跳 ⇒ 整机休眠
    · awake_no_artifact —— 心跳连续（机器醒着）却零产物零留痕 ⇒ 任务自身/宿主未触发

【纪律】
  · 只读：不写任何生产文件、不发网络请求（交易日历失败自动退化为「非周末」）。
  · 时间与文件系统全部可注入（`now` / `base` / `guardian_log` / `trade_day`），
    测试用合成夹具，不依赖真实 mtime（第 19 条教训：钉契约不钉副产品）。
  · 本模块是分类逻辑的**单一事实源**：`scripts/_recursive_self_improve.py` 只 import，
    不得自带副本（`tests/test_loop_liveness.py` 钉住这一条，同 R43 第 24 条）。
"""
from __future__ import annotations

import re
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent

STATE_NORMAL = "normal"
STATE_HALTED = "halted"
STATE_MISSED = "missed"

STATE_LABEL = {
    STATE_NORMAL: "① 正常产出",
    STATE_HALTED: "② 运行但自我停机",
    STATE_MISSED: "③ 完全未运行",
}

VERDICT_LABEL = {
    "prompt_gate": "prompt 门禁自停",
    "machine_sleep": "整机休眠",
    "awake_no_artifact": "醒着但零留痕（任务自身/宿主未触发）",
    "": "—",
}


class Task:
    """一个自动化的「应该什么时候跑 / 产物长什么样」。

    cadence:
      · ``daily``  —— 交易日的固定时刻（`hourly=False`）
      · ``weekly`` —— 每周某个星期几的固定时刻
      · ``hourly`` —— 交易日的整点区间内每小时一次（GUARD），`hours` 给区间
    """

    __slots__ = ("id", "name", "cadence", "at", "weekday", "hours",
                 "product", "grace_min", "ignore_markers")

    def __init__(self, id: str, name: str, cadence: str, at: str,
                 weekday: Optional[int] = None,
                 hours: Tuple[int, ...] = (),
                 product: Tuple[str, ...] = (),
                 grace_min: int = 45,
                 ignore_markers: Tuple[str, ...] = ()):
        self.id = id
        self.name = name
        self.cadence = cadence
        self.at = at
        self.weekday = weekday            # 0=周一
        self.hours = hours
        self.product = product            # 产物 glob（相对 ROOT）；空 = 本任务不产出文件
        self.grace_min = grace_min        # 当前槽位仍在跑时不判「没跑」
        # 该任务「虽然打印了告警、但其实干完了活」的标记（假告警），不计入停机判据。
        self.ignore_markers = ignore_markers


# ★ 注册表 = 契约 §五 的时段表。改时段必须同时改这里与契约，否则 L3 会用错槽位
#   把「正常」判成「未运行」（误报比漏报更伤人）。
#
# 【2026-11-02 归集】OWNER 指令「核实所有定时任务，归集为每日一次复盘」。
#   原 10 条 → 3 条：7 个工作日任务并入 15:40 DAILY-REVIEW；PM-EVOLVE 并入
#   周日 SYNTHESIZE。被归集的 8 条在自动化侧已 PAUSED（保留配置可回滚），
#   故这里**不再登记**——登记已停用的任务会让 L3 每天报 8 条假「未运行」，
#   把真问题淹掉（这正是归集要治的病）。
TASKS: Tuple[Task, ...] = (
    # 唯一的每日任务，也是整个闭环的节拍器：它停摆 = 全线失明。
    # 产物清单只认 DAILY_REVIEW_*.md（七块内容都在这一份里），不散列子报告。
    Task("d1cdbd85", "每日复盘 DAILY-REVIEW", "daily", "15:40",
         product=("reports/DAILY_REVIEW_*.md",)),
    Task("f093d93b", "周度深度 SYNTHESIZE", "weekly", "20:00", weekday=6,
         grace_min=90, product=("reports/EVOLUTION_PROPOSAL_*.md",)),
    Task("4a425d4f", "递归自进化 RSI", "weekly", "21:00", weekday=6,
         grace_min=90, product=("reports/meta/RSI_META_LATEST.md",)),
)

BY_ID = {t.id: t for t in TASKS}

# 停机标记：来自 2026-10-30 实盘事故的**真实文案**（不是想象的关键词）。
# 只放「明确表示本次运行什么都没做」的强标记；「未改动参数」这类正常收尾语不行。
HALT_MARKERS: Tuple[str, ...] = (
    r"\[MODE-ALERT\]",
    r"\[GUARDIAN-ALERT\]",
    r"硬闸门",
    r"闸门拦截",
    r"闸门未通过",
    r"整体停机",
    r"自我停机",
    r"立即停止",
    r"停止一切落盘",
    r"不执行\s*Step",
    r"只读\s*\+\s*顶部告警",
)
_HALT_RE = re.compile("|".join(HALT_MARKERS))

_BEAT_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")


# --------------------------------------------------------------------- 基础件
def load_heartbeats(path: Optional[Path] = None) -> list:
    """读 `logs/guardian.log` 的 300s 心跳时间戳（失败返回空表 = 无证据）。"""
    p = Path(path) if path else (ROOT / "logs" / "guardian.log")
    if not p.exists():
        return []
    out = []
    try:
        with p.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                m = _BEAT_RE.match(line)
                if not m:
                    continue
                try:
                    out.append(datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
                except ValueError:
                    continue
    except OSError:
        return []
    out.sort()
    return out


def window_awake(beats: Sequence[datetime], start: datetime, end: datetime,
                 tol_min: int = 15, pad_min: int = 10) -> dict:
    """槽位窗口内机器是否醒着（心跳间隔 ≤ tol_min 视为连续）。

    判据来源：`docs/AUTOMATION_CONTRACT.md` §4.3 —— guardian 每 300s 一条心跳，
    缺失 = 整机休眠/关机，不是引擎故障。tol 取 15 分钟 = 连丢 3 条心跳。
    """
    ws = start - timedelta(minutes=pad_min)
    we = end + timedelta(minutes=pad_min)
    inwin = [b for b in beats if ws <= b <= we]
    if not inwin:
        return {"awake": False, "beats": 0, "max_gap_min": None,
                "detail": f"{ws:%m-%d %H:%M}~{we:%m-%d %H:%M} 无任何心跳"}
    seq = [ws] + inwin + [we]
    max_gap = max((b - a).total_seconds() / 60.0
                  for a, b in zip(seq, seq[1:]))
    return {"awake": max_gap <= tol_min, "beats": len(inwin),
            "max_gap_min": round(max_gap, 1),
            "detail": (f"窗口内 {len(inwin)} 条心跳，最大空档 "
                       f"{max_gap:.0f} 分钟")}


def _weekday_trade_day(d: date) -> bool:
    return d.weekday() < 5


def default_trade_day(d: date) -> bool:
    """交易日判定：优先 xtdata 日历（可在休市日判否），失败退化为「非周末」。"""
    try:
        from core.market_calendar import is_trading_day  # noqa: PLC0415
        return bool(is_trading_day(d))
    except Exception:                                   # noqa: BLE001
        return _weekday_trade_day(d)


def iter_slots(task: Task, now: datetime, trade_day: Callable[[date], bool],
               days_back: int = 14) -> Iterable[datetime]:
    """按时间**倒序**产出该任务的槽位（最近的在前）。"""
    hh, mm = (int(x) for x in task.at.split(":"))
    at = dtime(hh, mm)
    for back in range(days_back):
        d = now.date() - timedelta(days=back)
        if task.cadence == "weekly":
            if d.weekday() != task.weekday:
                continue
            yield datetime.combine(d, at)
        elif task.cadence == "daily":
            if not trade_day(d):
                continue
            yield datetime.combine(d, at)
        elif task.cadence == "hourly":
            if not trade_day(d):
                continue
            for h in sorted(task.hours, reverse=True):
                yield datetime.combine(d, dtime(h, mm))
        else:
            raise ValueError(f"未知 cadence: {task.cadence}")


def expected_slot(task: Task, now: datetime,
                  trade_day: Callable[[date], bool]) -> Optional[datetime]:
    """最近一个「本该已经跑完」的槽位。

    grace：任务可能正在跑（memory 是运行末尾才写）。当前槽位还没过 grace 窗口时，
    取上一个槽位，避免把「正在执行的自己」判成没跑。
    """
    cut = now - timedelta(minutes=task.grace_min)
    for s in iter_slots(task, now, trade_day):
        if s <= cut:
            return s
    return None


def count_slots_between(task: Task, start: datetime, end: datetime,
                        trade_day: Callable[[date], bool]) -> int:
    """(start, end] 之间该任务错过了几个槽位。"""
    return sum(1 for s in iter_slots(task, end, trade_day)
               if start < s <= end)


def last_entry(text: str) -> str:
    """memory.md 的最后一段（最近一次执行的高层结论）。"""
    parts = re.split(r"(?m)^##\s+", text)
    parts = [p for p in parts if p.strip()]
    return parts[-1] if parts else ""


def halt_hits(entry: str) -> list:
    return sorted({m.group(0) for m in _HALT_RE.finditer(entry or "")})


def product_fresh(task: Task, start: datetime, end: datetime) -> Optional[str]:
    """窗口内是否有产物文件（返回最新的一个，用于证据引用）。"""
    if not task.product:
        return None
    best, best_mt = None, None
    for pat in task.product:
        for f in ROOT.glob(pat):
            try:
                mt = datetime.fromtimestamp(f.stat().st_mtime)
            except OSError:
                continue
            if start <= mt <= end and (best_mt is None or mt > best_mt):
                best, best_mt = f, mt
    return f"{best.relative_to(ROOT)}（{best_mt:%m-%d %H:%M}）" if best else None


# --------------------------------------------------------------------- 判定
def classify_task(task: Task, now: datetime,
                  base: Optional[Path] = None,
                  beats: Optional[Sequence[datetime]] = None,
                  trade_day: Optional[Callable[[date], bool]] = None) -> dict:
    """单任务活性三态判定。纯函数式（除读文件），全部输入可注入。"""
    base = Path(base) if base else (ROOT / ".workbuddy" / "memory" / "automations")
    beats = beats if beats is not None else load_heartbeats()
    trade_day = trade_day or default_trade_day

    dirs = list(base.glob(f"{task.id}*")) if base.exists() else []
    mem = dirs[0] / "memory.md" if dirs else None
    mem_mtime = None
    text = ""
    if mem is not None and mem.exists():
        mem_mtime = datetime.fromtimestamp(mem.stat().st_mtime)
        text = mem.read_text(encoding="utf-8", errors="ignore")

    slot = expected_slot(task, now, trade_day)
    row = {
        "id": task.id, "name": task.name, "cadence": task.cadence,
        "last_run": f"{mem_mtime:%Y-%m-%d %H:%M}" if mem_mtime else None,
        "age_days": (round((now - mem_mtime).total_seconds() / 86400.0, 1)
                     if mem_mtime else None),
        "slot": f"{slot:%Y-%m-%d %H:%M}" if slot else None,
        "state": STATE_NORMAL, "state_label": STATE_LABEL[STATE_NORMAL],
        "verdict": "", "verdict_label": "—",
        "evidence": "", "halt_markers": [], "product": None, "notes": [],
        "missed_slots": 0, "slot_awake": None, "max_gap_min": None,
        "stale": False,
    }
    if slot is None:
        row["evidence"] = "近 14 天无该任务槽位（新任务/时段刚调整）"
        return row

    # ---- 情况 A：槽位之后写过 memory（跑过）
    if mem_mtime is not None and mem_mtime >= slot:
        hits_all = halt_hits(last_entry(text))
        hits = [h for h in hits_all if h not in task.ignore_markers]
        notes = [f"含 {h}（已判定为假告警，不计入停机）"
                 for h in hits_all if h in task.ignore_markers]
        # 产物窗口必须**贴着槽位**（slot-10min ~ min(mem_mtime, slot+4h)）：
        # 初期实现用了 [slot-1h, now]，结果把 11-01 工程维护写的
        # PREMARKET_READINESS_20261102.md 当成了 PRE-MARKET 10-30 槽位的产物，
        # 把一个真实停摆判成了 ①（漏报）。产物只能属于它自己的那个槽位。
        p_end = min(mem_mtime, slot + timedelta(hours=4)) + timedelta(minutes=30)
        prod = product_fresh(task, slot - timedelta(minutes=10), p_end)
        row["halt_markers"] = hits
        row["product"] = prod
        row["notes"] = notes
        # product=() 的任务（GUARD）不以「无产物」作为停机证据
        missing_product = (not prod) and bool(task.product)
        if hits and missing_product:
            # ② 在跑但什么都没做：这是旧 L3 看不见的那一类
            awake = window_awake(beats, slot, min(mem_mtime, slot + timedelta(minutes=90)))
            gap_txt = ("整窗口无心跳" if awake["max_gap_min"] is None
                       else f"最大心跳空档 {awake['max_gap_min']} 分钟")
            row.update({
                "state": STATE_HALTED,
                "state_label": STATE_LABEL[STATE_HALTED],
                "verdict": "prompt_gate",
                "verdict_label": VERDICT_LABEL["prompt_gate"],
                "slot_awake": awake["awake"],
                "max_gap_min": awake["max_gap_min"],
                "evidence": (f"末条含停机标记 {'/'.join(hits)}，且 "
                             f"{slot:%m-%d %H:%M} 起窗口内无产物；"
                             f"槽位窗口机器{'醒着' if awake['awake'] else '休眠'}"
                             f"（{gap_txt}）"),
            })
        else:
            row.update({
                "state": STATE_NORMAL,
                "state_label": STATE_LABEL[STATE_NORMAL],
                "evidence": (f"槽位 {slot:%m-%d %H:%M} 已跑"
                             + (f"，产物 {prod}" if prod else "")
                             + ("（末条含门禁字样但有产物）" if hits else "")),
                "product": prod,
            })
        row["stale"] = row["state"] != STATE_NORMAL
        return row

    # ---- 情况 B：槽位之后没写过 memory
    # 同上：产物窗口贴槽位，绝不用「到 now 为止」——周末一次补跑会把
    # 两个交易日前的停摆洗成正常。
    prod_late = product_fresh(task, slot - timedelta(minutes=10),
                              slot + timedelta(hours=4))
    if prod_late:
        # 跑出了产物但忘了记账 ⇒ 活性算正常，但账本缺口要报（信息项）
        row.update({
            "state": STATE_NORMAL,
            "state_label": STATE_LABEL[STATE_NORMAL],
            "product": prod_late,
            "evidence": f"memory 未更新，但产物在：{prod_late}（记账缺口）",
        })
        row["stale"] = False
        return row

    # ③ 完全未运行 —— 必须给出「机器休眠 vs 任务自身」的归因
    awake = window_awake(beats, slot, slot + timedelta(minutes=60))
    verdict = "machine_sleep" if not awake["awake"] else "awake_no_artifact"
    row.update({
        "state": STATE_MISSED,
        "state_label": STATE_LABEL[STATE_MISSED],
        "verdict": verdict,
        "verdict_label": VERDICT_LABEL[verdict],
        "slot_awake": awake["awake"],
        "max_gap_min": awake["max_gap_min"],
        "missed_slots": count_slots_between(task, mem_mtime, now, trade_day)
        if mem_mtime else 0,
        "evidence": (f"槽位 {slot:%m-%d %H:%M} 后无 memory、无产物；"
                     f"{awake['detail']} ⇒ " +
                     ("整机休眠，任务未触发" if not awake["awake"]
                      else "机器醒着却零留痕，问题在任务自身")),
    })
    row["stale"] = True
    return row


def audit(now: Optional[datetime] = None,
          base: Optional[Path] = None,
          guardian_log: Optional[Path] = None,
          trade_day: Optional[Callable[[date], bool]] = None,
          tasks: Sequence[Task] = TASKS) -> list:
    now = now or datetime.now()
    beats = load_heartbeats(guardian_log)
    return [classify_task(t, now, base=base, beats=beats, trade_day=trade_day)
            for t in tasks]


def summarize(rows: Sequence[dict]) -> dict:
    counts = {STATE_NORMAL: 0, STATE_HALTED: 0, STATE_MISSED: 0}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    bad = [r for r in rows if r["state"] != STATE_NORMAL]
    return {
        "counts": counts,
        "total": len(rows),
        "abnormal": bad,
        "headline": (
            f"活性三态：① {counts[STATE_NORMAL]} / ② {counts[STATE_HALTED]} / "
            f"③ {counts[STATE_MISSED]}（共 {len(rows)}）"
            + ("" if not bad else "；异常：" + "、".join(
                f"{r['name']}→{r['state_label']}（{r['verdict_label']}）"
                for r in bad))),
    }


if __name__ == "__main__":                       # 手工排查用：只读打印
    rows = audit()
    s = summarize(rows)
    print(s["headline"])
    for r in rows:
        print(f"  {r['state_label']:<14} {r['name']:<22} 槽位 {r['slot'] or '—':<16} "
              f"最近 {r['last_run'] or '—':<16} {r['verdict_label']}")
        if r["evidence"]:
            print(f"      ↳ {r['evidence']}")

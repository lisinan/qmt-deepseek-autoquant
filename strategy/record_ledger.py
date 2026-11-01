# -*- coding: utf-8 -*-
"""自进化闭环的「度量（OBSERVE → 记账）」环节。

把每日盘后复盘产出的**真实**账户表现，追加写入自进化账本，作为后续
EVOLVE（进化）环节计算「与优秀收益目标的差距」的唯一事实来源。

产出（全部仅追加，绝不改写历史）：
  reports/EVOLUTION_LEDGER.md   人读表格（复盘/审计用）
  logs/evolution_ledger.jsonl   机器读单行 JSON（EVOLVE 自动化消费）

设计要点：
  * 采用**跨日口径**的真实收益（prev_close → 当日 close，含隔夜重估），
    而非当日日内涨跌——后者会漏掉隔夜跳空，导致账户真的在亏钱时复盘
    却显示「健康」（2026-09-16 事故：日内 -0.25%，跨日真实 -16.4%）。
  * 任一字段缺失降级为 None，绝不因单点异常中断记账。

用法：
    python strategy/record_ledger.py                 # 自动取最新 review
    python strategy/record_ledger.py --date 2026-09-16
    python strategy/record_ledger.py --recent 10     # 只看最近 N 日摘要
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date as date_cls
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Windows 控制台 UTF-8（本文件会打印中文 / ★ 等字符）。不加这段时，cp1252
# 控制台上 print 会抛 UnicodeEncodeError，使 --check-gap / --backfill 在打印阶段就崩。
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ❗ 必须先 import config.settings，再 import sqlite3（与 strategy/review_daily.py 一致）。
# 原因：config/settings.py 里有一段 Windows ``os.add_dll_directory`` 修复，用于把
# conda env 的 ``Library\bin``（含 sqlite3.dll）加入搜索路径。若先 import sqlite3，
# 在**未执行 conda activate** 的场景（如直接用解释器路径调用
# ``<env>/python.exe strategy/record_ledger.py``，或由定时任务/自动化拉起）会抛
# ``ImportError: DLL load failed while importing _sqlite3``，账本工具彻底跑不起来。
import config.settings  # noqa: E402,F401  (须先于 sqlite3)

import sqlite3  # noqa: E402

LOG_DIR = ROOT / "logs"
REPORT_DIR = ROOT / "reports"
LEDGER_MD = REPORT_DIR / "EVOLUTION_LEDGER.md"
LEDGER_JSONL = LOG_DIR / "evolution_ledger.jsonl"
# 【2026-09-30 PM-EVOLVE 第 15 轮】账本不再只依赖 OBSERVE 的 review JSON。
# 见下方 build_row_from_db：DB 是「账户真相」的原始出处，review JSON 只是它的
# 加工品；加工链断掉时不应让度量一起断。测试可 monkeypatch 本变量指向临时库。
DB_PATH = ROOT / "storage" / "qmt.db"
# 区间累计收益的锚点：2026-09-21 账户复位为 1,000,000（与 review_daily 口径一致）。
# 可用环境变量覆盖，便于测试。
RANGE_BASE = float(os.environ.get("QMT_RANGE_BASE", "1000000"))

# ---------------------------------------------------------------------------
# 【2026-11-01】★ 模式断点（mode boundary）—— 账本不得跨 paper/live 链接
#
# 缺陷（P0，量测层，会让整条自进化链读到假崩盘）：
#   2026-10-29 15:05 账户由 paper（初始 1,000,000，EOD 972,533.60）切换为
#   **live 真实资金**（券商总资产 66,542.31）。账本原先只按「最近一个有快照
#   的交易日」取 prev_asset，完全不认识模式切换 ⇒ live 首日会被记成
#       63,684 / 972,533.60 − 1 = **−93.5%**
#   而 EVOLVE 的全部「当前 vs 目标差距」都以账本为唯一事实来源 ⇒ 会得出
#   「账户已崩、需要降杠杆/停手」的结论，与真实情况（+1,273 元当日盈利）
#   完全相反。这属于本项目第 19 类缺陷「口径背离」的量测版本。
#
# 处置：显式登记模式断点；断点日及其之后
#   ① prev_asset 不再跨断点链接 —— 上一快照日早于断点则用断点基线；
#   ② range_pct 以断点基线为分母（不再用 paper 的 1,000,000）；
#   ③ 行内新增 mode / baseline 字段，供 KPI#4「live 日度账本」按模式过滤。
# 新增断点时**只追加**，不改写历史行。
MODE_BREAKS = {
    "2026-10-29": {"mode": "live", "baseline": 66542.31},
}


def _mode_break_for(day: str):
    """返回该日适用的 (断点日, mode, baseline)；无断点返回 None。"""
    if not day:
        return None
    cands = [d for d in MODE_BREAKS if d <= day]
    if not cands:
        return None
    d = max(cands)
    spec = MODE_BREAKS[d]
    try:
        base = float(spec["baseline"])
    except (TypeError, ValueError):
        return None
    return d, str(spec["mode"]), base

_MD_HEADER = """# EVOLUTION_LEDGER — 自进化账本（真实收益反馈）

> 本文件由 `strategy/record_ledger.py` **仅追加**写入，历史一律不改写。
> 它是自进化闭环的「度量」层：EVOLVE 环节据此计算与优秀收益目标的差距，
> 达标前持续迭代。机读版见 `logs/evolution_ledger.jsonl`。

## 考核目标（达标即停止激进迭代）
| 指标 | 目标值 |
|---|---|
| walk-forward 样本外均值 Sharpe | ≥ 1.6（原 2.0，2026-09-19 OWNER 修订） |
| walk-forward 正收益折数 | ≥ 6/7 |
| walk-forward 最大回撤 | ≥ -22% |
| live paper 近 4 周滚动收益 | > 0% |
| 相对等权买入持有全样本 alpha | ≥ -30pt（新增，2026-09-19） |

## 逐日实绩
| 交易日 | 跨日真实收益% | 当日盈亏(元) | 期末权益 | 累计区间% | 稳定 | 安全 | 准确 | 高效 | 综合 | 熔断 | 最大连亏 | 成交数 | 收盘持仓 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
"""


def _latest_review_date() -> str | None:
    """取最新的 review JSON 对应日期。"""
    files = sorted(LOG_DIR.glob("review_*.json"))
    if not files:
        return None
    stem = files[-1].stem                      # review_2026-09-16
    return stem.split("_", 1)[1] if "_" in stem else None


def load_review(target: str) -> dict:
    p = LOG_DIR / f"review_{target}.json"
    if not p.exists():
        raise FileNotFoundError(f"复盘文件不存在: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def build_row(rep: dict) -> dict:
    """从复盘 JSON 抽取「账户真实表现」行。缺字段降级 None。"""
    acct = rep.get("account_daily") or {}
    rng = rep.get("account_range") or {}
    opt = rep.get("optimization") or {}
    scores = opt.get("scores") or {}
    risk = rep.get("risk") or {}
    pnl = rep.get("pnl") or {}
    eod_pos = pnl.get("eod_positions") or {}

    def _num(v):
        try:
            return round(float(v), 2)
        except (TypeError, ValueError):
            return None

    return annotate_eod_freshness(apply_mode_boundary({
        "date": rep.get("date") or _latest_review_date(),
        # 跨日真实口径（含隔夜重估）——账户表现的唯一基准
        "prev_asset": _num(acct.get("prev_asset")),
        "eod_asset": _num(acct.get("eod_asset")),
        "daily_ret_pct": _num(acct.get("daily_ret_pct")),
        "daily_pnl": _num(acct.get("daily_pnl")),
        "range_pct": _num(rng.get("range_pct")),
        # 健康评分
        "stable": scores.get("稳定"),
        "safety": scores.get("安全"),
        "accuracy": scores.get("准确"),
        "efficiency": scores.get("高效"),
        "overall": opt.get("overall"),
        # 风险/执行
        "halt_count": risk.get("halt_count"),
        "max_consec_loss": risk.get("max_consecutive_losses"),
        "fills_n": pnl.get("fills_n"),
        "eod_positions_n": len(eod_pos) if isinstance(eod_pos, dict) else None,
    }), None)


# ---------------------------------------------------------------------------
# 【2026-09-30 PM-EVOLVE 第 15 轮】★ DB 直读回退（账本断供修复）
#
# 缺陷（P0，量测层）：账本的唯一输入是 ``logs/review_<date>.json``，而它由
# 15:35 OBSERVE 自动化产出 ⇒ 存在**三级单点故障**：
#     OBSERVE 未运行 / 中途异常 / 无成交日取错目标日  →  无 review JSON
#     →  record_ledger 直接 return 1  →  账本静默断供，无告警
# 实盘铁证：``logs/evolution_ledger.jsonl`` 只到 2026-09-25，而
#   ``equity_snapshots`` 明明有 09-28 / 09-29 / 09-30 三日完整快照 ⇒
#   KPI #4「paper 近 4 周滚动收益」连续 4 个交易日**无法计算**，
#   而没人收到任何报错（09-28 / 09-29 / 09-30 三轮自进化都只能在报告里
#   手写「账本断供」）。这是与 09-24 日线滞后 / 09-26 快照冻结同型的
#   「量测通道本身不可信」缺陷。
#
# 修复原则：DB 的 ``equity_snapshots`` 才是账户真相的**原始出处**，
#   review JSON 只是它的加工品。加工链断掉时，退回原始出处而不是放弃度量。
# 可逆性：新增函数均为纯读取；main() 只在「review 缺失」时才走回退路径，
#   有 review 时行为与改动前**逐位相同**。
# ---------------------------------------------------------------------------


def _conn(path=None):
    """打开一个只读用途的 sqlite 连接。

    ★【2026-10-16 工程缺陷修复】原实现返回裸 ``sqlite3.connect(...)``，调用方
    一律写成 ``with _conn(path) as con:`` —— 而 **sqlite3.Connection 的上下文
    管理器协议只管事务（commit/rollback），从不 ``close()`` 连接**。后果：

      · 每个 helper 调用泄漏一个 file handle + WAL shm 映射，进程退出才回收；
      · Windows 上文件句柄未关 ⇒ 临时数据库文件无法删除。实盘铁证：
        ``tests/run_all.py`` 中 16 个用例（test_ledger_stale_mark_guard 9 例 +
        test_ledger_db_fallback 7 例）全部因 ``TemporaryDirectory`` 清理时
        ``PermissionError: [WinError 32]`` 而**失败**，而账本逻辑本身是正确的
        ⇒ 基线 461/478，其中 16 例是「测试卫生」失败而非产品缺陷。
      · 生产侧同样受影响：OBSERVE 每次跑 ``--check-gap`` 都多留一个句柄，
        与 ``storage/qmt.db-wal`` 长期不归零、磁盘占用虚高直接相关。

    这里包一层 ``contextlib.closing`` 语义：**进入时建连，退出时必 close**。
    调用方 ``with _conn(path) as con:`` 的写法完全不用改（接口不变）。
    """
    con = sqlite3.connect(str(path or DB_PATH))
    con.row_factory = sqlite3.Row
    return _ClosingConn(con)


class _ClosingConn:
    """让 ``with _conn(...) as con`` 退出时真正关闭连接。

    为什么不用 ``contextlib.closing(sqlite3.connect(...))``：调用方在 with 体内
    执行 ``con.execute(...)``，而 ``closing`` 包装对象只转发 ``close``（无
    ``__getattr__``），会让每个调用点都改代码。本类把其余属性/方法原样转发，
    **调用方零改动**。
    """

    __slots__ = ("_con",)

    def __init__(self, con):
        self._con = con

    def __enter__(self):
        return self._con

    def __exit__(self, exc_type, exc, tb):
        try:
            self._con.close()
        except Exception:
            pass
        return False

    def __getattr__(self, name):
        return getattr(self._con, name)


def snapshot_days(path=None) -> list:
    """equity_snapshots 中出现过的全部日期（升序）。"""
    if not Path(path or DB_PATH).exists():
        return []
    with _conn(path) as con:
        rows = con.execute(
            "SELECT DISTINCT substr(ts,1,10) AS d FROM equity_snapshots "
            "WHERE ts IS NOT NULL ORDER BY d").fetchall()
    return [r["d"] for r in rows]


def eod_asset(day: str, path=None) -> float | None:
    """某日**最后一条**权益快照的总资产 = 当日 EOD 权益（跨日口径的锚）。

    【2026-10-07 PM-EVOLVE 第 21 轮】加入**陈旧盯市污染校正**：
    若当日被 :func:`stale_mark_suspect_day` 判为污染（零成交 + 权益零波动 +
    却相对前一日跳变 > 0.5%），则该日的「EOD」视为**不可信**，
    回溯到最近一个可信交易日的 EOD（最多回退 10 天）。
    语义：没有成交、盯市价也没动的日子，**不应产生任何盈亏** ⇒ 记为 0%。
    """
    if not Path(path or DB_PATH).exists():
        return None
    raw = eod_asset_raw(day, path)
    if raw is None:
        return None
    if not stale_mark_suspect_day(day, path):
        return raw
    d = day
    for _ in range(10):
        d = prev_snapshot_day(d, path)
        if not d:
            return raw
        if not stale_mark_suspect_day(d, path):
            v = eod_asset_raw(d, path)
            return v if v is not None else raw
    return raw


def day_stats(day: str, path=None) -> tuple:
    """当日快照的 (条数, 最小权益, 最大权益, 成交笔数)。

    用于「陈旧盯市污染」检测（见 :func:`stale_mark_suspect_day`）。
    """
    if not Path(path or DB_PATH).exists():
        return (0, None, None, 0)
    with _conn(path) as con:
        r = con.execute(
            "SELECT COUNT(*) c, MIN(total_asset) lo, MAX(total_asset) hi "
            "FROM equity_snapshots WHERE substr(ts,1,10)=? "
            "AND total_asset IS NOT NULL", (day,)
        ).fetchone()
        f = con.execute(
            "SELECT COUNT(*) c FROM fills WHERE substr(ts,1,10)=?", (day,)
        ).fetchone()
    if r is None or not r["c"]:
        return (0, None, None, int(f["c"]) if f else 0)
    return (int(r["c"]), float(r["lo"]), float(r["hi"]),
            int(f["c"]) if f else 0)


def stale_mark_suspect_day(day: str, path=None, flat_tol: float = 1e-9,
                           move_tol: float = 0.005) -> bool:
    """当日权益是否被「陈旧盯市」污染（True = 不可用作 EOD 锚）。

    【2026-10-07 PM-EVOLVE 第 21 轮】同伴缺陷：AM 时段才修完「陈旧报价
    抬高盯市」的根因（`engine/event_engine.py` 的
    ``MARK_STALE_FREEZE_VALUATION``），但**已被污染的历史快照仍然留在库里**：
    2026-10-07（国庆休市、休市日）全天快照恒为 1,020,446.6，而 09-30 真实
    EOD 是 975,273.6 ⇒ 虚增 +45,173（+4.63%）。若不拦截，10-08 的
    ``prev_asset`` 会取到虚高值，账本会记一笔 **−4.4% 的假亏损**
    （与 R14 合成行情假成交同型），KPI #4 符号再次失真。

    判据（全部来自 DB 自身，不依赖任何外部行情源 ⇒ 稳定可测）：
      ① 当日**零成交**（组合未变动）；
      ② 当日快照权益**零波动**（max−min ≤ tol）——真实交易日里 5 只持仓
         的市值不可能逐分钟完全相同 ⇒ 说明盯市价根本没在动（陈旧/休市）；
      ③ 当日 EOD 相对**前一个可信日**偏离 > move_tol(0.5%)
         ——零成交 + 零波动却「赚了钱」，只能是脏价格。
    三者同时成立 ⇒ 判定为陈旧盯市污染。

    判据的可证伪性：真实交易日即使全天涨停/跌停也会留下**成交**（①不成立），
    且单日组合波动超过 0.5% 的极端日子几乎必然伴随成交或前一个交易日的
    真实价格位移 ⇒ 不会误伤。极端情况下的代价是「当日记为 0% 并留痕」，
    而非静默采用可疑数字——这正是本守卫的目的。
    """
    cnt, lo, hi, fills = day_stats(day, path)
    if cnt < 2 or fills:
        return False
    if lo is None or hi is None or (hi - lo) > flat_tol:
        return False
    cur = eod_asset_raw(day, path)
    anchor = _credible_anchor(day, path, cur)
    if cur is None or anchor is None or anchor <= 0:
        return False
    return abs(cur / anchor - 1.0) > move_tol


def _credible_anchor(day: str, path, cur: float | None) -> float | None:
    """向前找到**取值不同**的最近一天的 EOD，作为对比锚。

    为什么不能直接比「前一天」：污染可能**连续多日**（整个长假），此时每天都
    与前一天逐位相同 ⇒ 互相掩护、谁都不显得异常（实测 10-06 / 10-07 双污染日
    第一天会被抓到、第二天 100% 漏网）。这里跳过所有「与当前值相同」的日子，
    一路回溯到真正发生位移的那天再比。
    """
    if cur is None:
        return None
    d = prev_snapshot_day(day, path)
    for _ in range(10):
        if not d:
            return None
        v = eod_asset_raw(d, path)
        if v is not None and abs(v / cur - 1.0) > 1e-9:
            return v
        d = prev_snapshot_day(d, path)
    return None


def eod_asset_raw(day: str, path=None) -> float | None:
    """某日最后一条快照的权益（**不**做污染校正，仅供内部/诊断使用）。"""
    if not Path(path or DB_PATH).exists():
        return None
    with _conn(path) as con:
        r = con.execute(
            "SELECT total_asset FROM equity_snapshots "
            "WHERE substr(ts,1,10)=? ORDER BY ts DESC, id DESC LIMIT 1", (day,)
        ).fetchone()
    return None if r is None or r["total_asset"] is None else float(r["total_asset"])


# 收盘快照判定线（HHMM）。A 股 15:00 收盘，快照落在 15:00 及之后才算收盘价。
EOD_CLOSE_HHMM = 1500


def eod_snapshot_ts(day: str, path=None) -> str | None:
    """该日最后一条权益快照的时间戳（ISO 字符串）。

    为什么需要：``eod_asset_raw`` 取的是「当日最后一条快照」，它**不保证是收盘价**。
    引擎若在收盘前退出/休眠（2026-10-30 11:29 后即无快照），这个值就是盘中价，
    直接当 EOD 会系统性低估当日跌幅（实测 −4.29% 被记成 −1.19%，差 3.1pt）。
    """
    if not Path(path or DB_PATH).exists():
        return None
    with _conn(path) as con:
        r = con.execute(
            "SELECT ts FROM equity_snapshots "
            "WHERE substr(ts,1,10)=? ORDER BY ts DESC, id DESC LIMIT 1", (day,)
        ).fetchone()
    return None if r is None or r["ts"] is None else str(r["ts"])


def annotate_eod_freshness(row: dict, path=None) -> dict:
    """给账本行补 ``eod_ts`` / ``eod_is_close``，让「EOD 是否真是收盘值」可被消费方看见。

    ★ 缺陷背景（2026-11-01 复核）
        账本的 ``eod_asset`` 取自当日**最后一条快照**。2026-10-30 引擎 11:29 后休眠
        （13:00–15:00 零快照，属整机休眠，见 ``docs/AUTOMATION_CONTRACT.md`` §4.3），
        于是把 11:29 的盘中资产 65,748.42 当成收盘值，当日收益记 −1.19%，
        真实 −4.29%。**数字看着合理、方向也对，所以没有任何环节报警。**
        这类「静默低估」比明显错误更危险：KPI#4 直接建立在它上面。

    ★ 本函数只增字段、不改任何既有数值语义（可逆：删掉调用即恢复）。
        - ``eod_ts``：最后一根快照时间；取不到 → None（不猜）。
        - ``eod_is_close``：True=快照在 15:00 后，可信作收盘价；
          False=盘中快照，**不可与其它交易日的 EOD 直接比较**；
          None=无快照可判。
        为 False 时在 ``note`` 追加提示，避免消费方（EVOLVE / KPI#4）误用。
    """
    day = row.get("date") or ""
    ts = eod_snapshot_ts(day, path) if day else None
    row["eod_ts"] = ts
    if not ts:
        row["eod_is_close"] = None
        return row
    try:
        hhmm = int(ts[11:13]) * 100 + int(ts[14:16])
    except (ValueError, IndexError):
        row["eod_is_close"] = None
        return row
    is_close = hhmm >= EOD_CLOSE_HHMM
    row["eod_is_close"] = is_close
    if not is_close:
        tag = f"[EOD 非收盘] 最后一根快照 {ts[11:16]} 早于 15:00，该 eod_asset 为盘中值"
        note = row.get("note") or ""
        if tag not in note:
            row["note"] = (note + " " + tag).strip()
    return row


def prev_snapshot_day(day: str, path=None) -> str | None:
    """该日之前最近一个有快照的交易日（跨日口径的 prev_asset 来源）。"""
    days = [d for d in snapshot_days(path) if d < day]
    return days[-1] if days else None


def apply_mode_boundary(row: dict, path=None) -> dict:
    """模式断点后处理（**单一实现**，两条构造路径共用，防口径分叉）。

    paper 时期行：原样返回，只补 ``mode="paper"``（历史行语义不变）。
    live 时期行：重算 prev_asset / daily_pnl / daily_ret_pct / range_pct，
    使断点日从基线起算、其后在 live 内部正常逐日链接。数据取不到时保持原值。
    """
    day = row.get("date") or ""
    brk = _mode_break_for(day)
    if brk is None:
        row.setdefault("mode", "paper")
        return row
    bday, mode, base = brk
    row["mode"] = mode
    row["baseline"] = round(base, 2)
    eod = row.get("eod_asset")
    if eod is None:
        return row
    try:
        eod = float(eod)
    except (TypeError, ValueError):
        return row
    pd = prev_snapshot_day(day, path)
    if pd and pd >= bday:
        prev = eod_asset(pd, path)
        try:
            prev = float(prev) if prev is not None else None
        except (TypeError, ValueError):
            prev = None
    else:
        prev = base          # 断点日：不跨模式链接，从基线起算
    if not prev or prev <= 0:
        return row
    row["prev_asset"] = round(prev, 2)
    row["daily_pnl"] = round(eod - prev, 2)
    row["daily_ret_pct"] = round((eod / prev - 1) * 100.0, 2)
    if base > 0:
        row["range_pct"] = round((eod / base - 1) * 100.0, 2)
    return row


def day_fills_n(day: str, path=None) -> int | None:
    if not Path(path or DB_PATH).exists():
        return None
    with _conn(path) as con:
        r = con.execute(
            "SELECT COUNT(*) c FROM fills WHERE substr(ts,1,10)=?", (day,)
        ).fetchone()
    return int(r["c"]) if r else None


def day_eod_positions_n(day: str, path=None) -> int | None:
    if not Path(path or DB_PATH).exists():
        return None
    with _conn(path) as con:
        r = con.execute(
            "SELECT positions_count FROM equity_snapshots "
            "WHERE substr(ts,1,10)=? ORDER BY ts DESC, id DESC LIMIT 1", (day,)
        ).fetchone()
    return None if r is None or r["positions_count"] is None else int(r["positions_count"])


def build_row_from_db(target: str, path=None, note: str = "") -> dict | None:
    """不依赖 review JSON，直接从 DB 构造账本行。

    只填 DB 能确证的核心字段（跨日真实收益 / 盈亏 / EOD 权益 / 区间），
    需要复盘流水线的健康评分与熔断明细降级为 None（遵循「缺字段降级」设计）。
    """
    eod = eod_asset(target, path)
    if eod is None:
        return None
    # 陈旧盯市污染留痕：原文,dict 里必须能看出「这一天的原始快照被否决了」，
    # 否则审计时无法区分「真的没波动」与「脏数据被中和」。
    raw_eod = eod_asset_raw(target, path)
    contaminated = (raw_eod is not None and abs(raw_eod - eod) > 1e-6)
    prev_day = prev_snapshot_day(target, path)
    prev = eod_asset(prev_day, path) if prev_day else None

    def _n(v):
        try:
            return round(float(v), 2)
        except (TypeError, ValueError):
            return None

    ret = pnl = None
    if prev:
        pnl = eod - prev
        ret = (eod / prev - 1) * 100.0 if prev else None
    rng = (eod / RANGE_BASE - 1) * 100.0 if RANGE_BASE else None
    row = {
        "date": target,
        "prev_asset": _n(prev),
        "eod_asset": _n(eod),
        "daily_ret_pct": _n(ret),
        "daily_pnl": _n(pnl),
        "range_pct": _n(rng),
        # 复盘流水线专属字段：DB 无法确证，一律降级 None（绝不猜测）
        "stable": None, "safety": None, "accuracy": None,
        "efficiency": None, "overall": None,
        "halt_count": None, "max_consec_loss": None,
        "fills_n": day_fills_n(target, path),
        "eod_positions_n": day_eod_positions_n(target, path),
        # ★ 溯源标记：EVOLVE 消费时能一眼看出该行是 DB 直读而非复盘加工品，
        #   避免把「降级行」当成「完整行」使用。
        "source": "db_fallback",
        "note": (note or ("" if not contaminated else ""))
                + (f"陈旧盯市污染已中和：原始快照 {raw_eod:,.2f} 被判为脏值，"
                   f"改用最近可信交易日 EOD {eod:,.2f}"
                   if contaminated else ""),
    }
    # 【2026-11-01】live 时期行必须在断点处重置基线，见 MODE_BREAKS 注释。
    # 同时补 EOD 时间戳/收盘判定，防止「盘中快照冒充收盘」再次静默发生。
    return annotate_eod_freshness(apply_mode_boundary(row, path), path)


def missing_days(path=None, ledger_rows=None, limit: int = 40,
                 since: str | None = None) -> list:
    """有快照但账本里没有的交易日 = 断供日（治理对象）。

    ``since``：只返回 **该日之后** 的断供日。补记时务必要用——账本首行之前
    的历史可能跨越「账户复位」边界（本项目 2026-09-21 复位为 100 万），
    跨边界算出来的「跨日收益」是复位金额差，不是策略盈亏。
    """
    rows = ledger_rows if ledger_rows is not None else read_ledger()
    have = {r.get("date") for r in rows}
    days = snapshot_days(path)
    # 休市日/陈旧盯市日不产出账本行（2026-10-07 起）：它们没有真实盈亏，
    # 补记只会制造「零成交却大涨/大跌」的假记录。
    days = [d for d in days if not stale_mark_suspect_day(d, path)]
    out = [d for d in days[-limit:] if d not in have]
    if since:
        out = [d for d in out if d > since]
    return out


def last_ledger_date(ledger_rows=None) -> str | None:
    rows = ledger_rows if ledger_rows is not None else read_ledger()
    ds = [r.get("date") for r in rows if r.get("date")]
    return max(ds) if ds else None


def _fmt(v, suffix: str = "") -> str:
    return "—" if v is None else f"{v}{suffix}"


def append_markdown(row: dict) -> bool:
    """人读表格追加；首次写入补表头。返回是否新增了行。"""
    LEDGER_MD.parent.mkdir(parents=True, exist_ok=True)
    exists = LEDGER_MD.exists()
    prev = LEDGER_MD.read_text(encoding="utf-8") if exists else ""
    if row["date"] and f"| {row['date']} " in prev:
        return False                                   # 幂等：同日不重复写
    if not exists or "## 逐日实绩" not in prev:
        LEDGER_MD.write_text(_MD_HEADER, encoding="utf-8")
    line = (
        f"| {row['date']} "
        f"| {_fmt(row['daily_ret_pct'], '%')} "
        f"| {_fmt(row['daily_pnl'])} "
        f"| {_fmt(row['eod_asset'])} "
        f"| {_fmt(row['range_pct'], '%')} "
        f"| {_fmt(row['stable'])} | {_fmt(row['safety'])} "
        f"| {_fmt(row['accuracy'])} | {_fmt(row['efficiency'])} "
        f"| {_fmt(row['overall'])} "
        f"| {_fmt(row['halt_count'])} | {_fmt(row['max_consec_loss'])} "
        f"| {_fmt(row['fills_n'])} | {_fmt(row['eod_positions_n'])} |\n"
    )
    with LEDGER_MD.open("a", encoding="utf-8") as f:
        f.write(line)
    return True


def append_jsonl(row: dict) -> bool:
    """机读 JSONL 追加；同日幂等（去重后重写该行）。"""
    LEDGER_JSONL.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    if LEDGER_JSONL.exists():
        for ln in LEDGER_JSONL.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                rows.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    replaced = False
    for i, r in enumerate(rows):
        if r.get("date") == row["date"]:
            rows[i] = row
            replaced = True
            break
    if not replaced:
        rows.append(row)
    rows.sort(key=lambda r: r.get("date") or "")
    with LEDGER_JSONL.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return not replaced


def read_ledger() -> list:
    if not LEDGER_JSONL.exists():
        return []
    out = []
    for ln in LEDGER_JSONL.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="记录当日真实收益到自进化账本")
    ap.add_argument("--date", default=None, help="交易日 YYYY-MM-DD（默认最新）")
    ap.add_argument("--recent", type=int, default=0, help="打印最近 N 日摘要")
    # ---- 第 15 轮新增：断供治理入口 ----
    ap.add_argument("--check-gap", action="store_true",
                    help="只检查断供：列出有权益快照但账本缺失的交易日，不写账本")
    ap.add_argument("--backfill", action="store_true",
                    help="把断供日全部用 DB 直读补记（默认只补到最近，见 --limit）")
    ap.add_argument("--exclude", default="",
                    help="逗号分隔、明确**不**补记的日期（数据被污染/待回滚时用）")
    ap.add_argument("--limit", type=int, default=40, help="回溯的交易日上限")
    ap.add_argument("--all", action="store_true",
                    help="补记时也覆盖账本首行之前的日期（可能跨越账户复位，慎用）")
    args = ap.parse_args()

    if args.recent:
        rows = read_ledger()[-args.recent:]
        for r in rows:
            print(
                f"{r['date']}  真实收益 {_fmt(r['daily_ret_pct'], '%')}"
                f"  盈亏 {_fmt(r['daily_pnl'])}"
                f"  累计 {_fmt(r['range_pct'], '%')}"
                f"  综合 {_fmt(r['overall'])}"
            )
        return 0

    # ---- 断供检查（只读，不写）----
    if args.check_gap:
        gap = missing_days(limit=args.limit)
        if not gap:
            print("[record_ledger] 无断供：所有有快照的交易日都已在账本中")
            return 0
        print(f"[record_ledger] ★ 账本断供 {len(gap)} 日：")
        for d in gap:
            print(f"    {d}  EOD {_fmt(eod_asset(d))}  "
                  f"（可 --backfill 补记）")
        return 2

    # ---- 断供补记 ----
    if args.backfill:
        excl = {x.strip() for x in args.exclude.split(",") if x.strip()}
        # ★ 默认只补「账本最后一日之后」的缺口：更早的日期可能跨越账户复位
        #   （本项目 2026-09-21 复位为 100 万），跨边界算出的「跨日收益」是
        #   复位金额差而非策略盈亏。--all 关闭该保护。
        since = None if args.all else last_ledger_date()
        targets = [d for d in missing_days(limit=args.limit, since=since)
                   if d not in excl]
        if since and args.all is False:
            skipped = [d for d in missing_days(limit=args.limit)
                       if d <= since and d not in excl]
            if skipped:
                print(f"[record_ledger] 跳过账本首行之前/复位边界前的 {len(skipped)} 日"
                      f"（{skipped[0]}…{skipped[-1]}），"
                      f"跨边界收益无意义；需补记请显式 --all")
        if not targets:
            print("[record_ledger] 无断供日可补记")
            return 0
        n = 0
        for d in targets:
            row = build_row_from_db(d)
            if not row:
                print(f"    {d}  跳过（无 EOD 快照）")
                continue
            append_markdown(row)
            append_jsonl(row)
            n += 1
            print(f"    {d}  补记  收益 {_fmt(row['daily_ret_pct'], '%')}  "
                  f"EOD {_fmt(row['eod_asset'])}  [source=db_fallback]")
        print(f"[record_ledger] DB 直读补记 {n} 日"
              + (f"；排除 {sorted(excl)}" if excl else ""))
        return 0

    target = args.date or _latest_review_date()
    if not target:
        # 【第 15 轮】旧行为：无 review JSON 直接放弃 → 账本静默断供。
        # 新行为：退回 DB 直读（equity_snapshots 才是账户真相的原始出处）。
        latest_days = snapshot_days()
        if not latest_days:
            print("[record_ledger] 未找到任何 review JSON，且 DB 无权益快照，跳过")
            return 1
        target = latest_days[-1]
        row = build_row_from_db(target, note="review JSON 缺失，DB 直读回退")
        if not row:
            print("[record_ledger] 未找到任何 review JSON，且 DB 回退失败，跳过")
            return 1
        append_markdown(row)
        append_jsonl(row)
        print(
            f"[record_ledger] {target}（DB 直读回退）真实收益 "
            f"{_fmt(row['daily_ret_pct'], '%')}  累计 {_fmt(row['range_pct'], '%')}"
        )
        return 0
    try:
        rep = load_review(target)
    except FileNotFoundError:
        row = build_row_from_db(target, note=f"review_{target}.json 缺失，DB 直读回退")
        if not row:
            print(f"[record_ledger] {target} 无 review JSON 且 DB 无快照，跳过")
            return 1
        append_markdown(row)
        append_jsonl(row)
        print(
            f"[record_ledger] {target}（DB 直读回退）真实收益 "
            f"{_fmt(row['daily_ret_pct'], '%')}  累计 {_fmt(row['range_pct'], '%')}"
        )
        return 0
    row = build_row(rep)
    row.setdefault("source", "review")
    added_md = append_markdown(row)
    added_json = append_jsonl(row)
    print(
        f"[record_ledger] {target} 真实收益 {_fmt(row['daily_ret_pct'], '%')}"
        f"  累计 {_fmt(row['range_pct'], '%')}"
        f"  综合 {_fmt(row['overall'])}"
        f"  (md={'新增' if added_md else '已存在'}, jsonl={'新增' if added_json else '更新'})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

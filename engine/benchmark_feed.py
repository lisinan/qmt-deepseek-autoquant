# -*- coding: utf-8 -*-
"""
基准管道（B4，WP-I）——从 EventEngine 切出的独立模块（C1 第一步·保守切片）。

职责：维护基准快照缓存，最多每 1800 秒在后台 daemon 线程里重算一次
（等权 BH / 宽基区间涨幅），并把当日基准 upsert 到 ``LOG_DIR/benchmark_daily.jsonl``。

设计约束（务必遵守，否则 tests/test_benchmark_and_rerank_b4b5.py 会红）：

1. **LOG_DIR 必须调用时解析**：``_TempLogDir`` 动态改写 ``engine.event_engine.LOG_DIR``，
   因此本模块绝不缓存 LOG_DIR；``append_daily`` 的 ``log_dir`` 由调用方（EventEngine 的
   薄委托）在每次调用时显式传入。

2. **依赖经 owner 动态解析**：既有测试会 monkeypatch ``eng._benchmark_since`` /
   ``eng._compute_benchmark`` / ``eng._total_asset`` / ``eng._benchmark_ts`` /
   ``eng._benchmark_thread``。本类通过 ``self._owner``（引擎）回调这些方法/属性，
   保证薄委托与 monkeypatch 语义逐位不变。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import Optional

from config.settings import INITIAL_CASH, MARKET_INDEX_CODE, STOCK_CODES

logger = logging.getLogger(__name__)


class BenchmarkFeed:
    """基准管道：缓存 + 后台重算 + 日度落盘（见模块 docstring）。"""

    # 后台刷新间隔（秒）。
    REFRESH_INTERVAL_SEC = 1800

    def __init__(self, owner):
        # 引擎引用：依赖（storage / _total_asset / _benchmark_since /
        # _compute_benchmark / _benchmark_daily_append / _benchmark_ts /
        # _benchmark_thread）都经 owner 在调用时解析，保证薄委托与 monkeypatch 兼容。
        self._owner = owner
        # 上次成功算成（或尝试）的墙钟
        self._ts: float = 0.0
        # 刷新线程句柄
        self._thread: Optional[threading.Thread] = None
        # 保护缓存 / 线程句柄的锁
        self._lock: threading.Lock = threading.Lock()
        # 已算成的缓存
        self._snap: dict = {"basis": "unavailable"}
        # 已落盘 jsonl 的日期
        self._daily_written: Optional[str] = None

    def snapshot(self) -> dict:
        """只读缓存，最多每 1800 秒在后台重算一次。

        绝不在本同步路径调用 ``strategy.backtest_daily.load_daily``——它内部
        可能触发 ``download_history``（~0.1s/只）。本方法仅返回已算成的缓存，
        并视需要在后台起一个 daemon 线程刷新。
        """
        try:
            with self._lock:
                snap = dict(self._snap)
            now = time.time()
            if (now - self._ts) >= self.REFRESH_INTERVAL_SEC:
                # 刷新中不重复起线程：线程存活即跳过，下次 snapshot 再补。
                thr = self._thread
                if thr is None or not thr.is_alive():
                    self._ts = now
                    # 经 owner 解析线程目标，保证 eng._compute_benchmark 的 monkeypatch 生效。
                    t = threading.Thread(
                        target=self._owner._compute_benchmark, daemon=True)
                    self._thread = t
                    t.start()
            return snap
        except Exception:
            return {"basis": "unavailable"}

    def since(self) -> str:
        """equity_snapshots 的最早快照日（MIN(substr(ts,1,10))），只读查询。

        失败或无数据回退 ``date.today() - 60 天``（保证基准不至于因查询故障
        而永久 unavailable）。
        """
        try:
            conn = self._owner.storage._conn_get()
            row = conn.execute(
                "SELECT MIN(substr(ts, 1, 10)) FROM equity_snapshots"
            ).fetchone()
        except Exception:
            row = None
        if row and row[0]:
            return str(row[0])
        return (date.today() - timedelta(days=60)).isoformat()

    def compute(self) -> None:
        """后台线程：拉日线算等权 BH / 宽基，成功后更新缓存并每日落盘一次。"""
        try:
            import strategy.benchmark as bench_mod
            codes = sorted(STOCK_CODES)
            since = self._owner._benchmark_since()
            result = bench_mod.compute(since=since, codes=codes,
                                       index_code=MARKET_INDEX_CODE)
            account_pct = (self._owner._total_asset() / INITIAL_CASH) - 1.0
            universe_bh = result.get("universe_bh_pct")
            index_pct = result.get("index_pct")
            if account_pct is None or universe_bh is None:
                excess = None
            else:
                excess = account_pct - universe_bh
            snap = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "since": since,
                "as_of": result.get("as_of"),
                "account_pct": account_pct,
                "universe_bh_pct": universe_bh,
                "index_pct": index_pct,
                "cash_pct": 0.0,
                "excess_pct": excess,
                "basis": "initial_cash",
                "n_codes": result.get("n_codes") or 0,
            }
            with self._lock:
                self._snap = snap
            # 经 owner 的薄委托落盘（LOG_DIR 在委托内调用时解析）。
            self._owner._benchmark_daily_append(snap)
        except Exception as ex:
            logger.warning("基准计算失败: %s", ex)
            try:
                with self._lock:
                    # 保留上一次成功的快照：宁可用 30 分钟前的数字，也不要把已算成的
                    # 基准抹成 unavailable（否则前端会在一次日线拉取失败后闪回「—」）。
                    if self._snap.get("basis") != "initial_cash":
                        self._snap = {"basis": "unavailable"}
            except Exception:
                pass
        finally:
            self._ts = time.time()

    def append_daily(self, snap: dict, log_dir: str) -> None:
        """把当日基准写入 ``log_dir/benchmark_daily.jsonl``（按日期 upsert）。

        【2026-10-21 修正】原实现是「进程内首次算成才 append」，有两个缺陷：
          1) 引擎重启后内存标记归零 ⇒ 同一交易日会重复写一行（跨进程去重失效）；
          2) 只记「当日首次算成」的值 ⇒ 早盘启动的引擎会把当日行永久停在 09:xx
             的资产口径，日度序列系统性失真。
        改为「每次成功刷新都重写当日行」（读全量 → 过滤同日 → 原子替换）：
        同一日期恒只有一行，最后写入即当日最新值；引擎在 15:05 前后停止轮询，
        因而最终收敛到接近收盘的口径。文件极小（一行/日），无性能顾虑。
        """
        try:
            today = date.today().isoformat()
            path = Path(log_dir) / "benchmark_daily.jsonl"
            row = {
                "date": today,
                "ts": snap.get("ts"),
                "account_pct": snap.get("account_pct"),
                "universe_bh_pct": snap.get("universe_bh_pct"),
                "index_pct": snap.get("index_pct"),
                "excess_pct": snap.get("excess_pct"),
                "n_codes": snap.get("n_codes"),
            }
            kept = []
            if path.exists():
                for ln in path.read_text(encoding="utf-8").splitlines():
                    if not ln.strip():
                        continue
                    try:
                        if json.loads(ln).get("date") == today:
                            continue        # 同日旧行丢弃，用本次重写
                    except Exception:
                        pass                    # 脏行原样保留，不吞掉别人的数据
                    kept.append(ln)
            kept.append(json.dumps(row, ensure_ascii=False))
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
            tmp.replace(path)
            self._daily_written = today
        except Exception as ex:
            logger.debug("基准日度落盘失败: %s", ex)

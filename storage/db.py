# -*- coding: utf-8 -*-
"""
SQLite 持久化（WAL 模式）

表：
  signals        策略信号
  orders         下单请求
  fills          成交回报
  ai_analyses    AI 分析
  risk_snapshots 风控快照
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from config.settings import BASE_DIR
from core.data_models import AIAnalysis, Fill, Order, Signal

logger = logging.getLogger(__name__)


_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        code TEXT NOT NULL,
        name TEXT,
        side TEXT NOT NULL,
        score REAL,
        price REAL,
        reason TEXT,
        factors_json TEXT,
        ai_comment TEXT,
        ai_confidence REAL
    )""",
    """CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        code TEXT NOT NULL,
        side TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        price REAL,
        order_type TEXT,
        account TEXT,
        order_id TEXT,
        mode TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS fills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        code TEXT NOT NULL,
        side TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        price REAL NOT NULL,
        amount REAL NOT NULL,
        account TEXT,
        order_id TEXT,
        mode TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS ai_analyses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        code TEXT NOT NULL,
        model TEXT,
        summary TEXT,
        stance TEXT,
        confidence REAL,
        risks TEXT,
        raw TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS risk_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        payload_json TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS equity_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        total_asset REAL,
        cash REAL,
        market_value REAL,
        positions_count INTEGER,
        drawdown_pct REAL,
        mode TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS sector_recommendations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        code TEXT NOT NULL,
        name TEXT,
        sector TEXT,
        sector_label TEXT,
        composite REAL,
        heat_contribution REAL,
        tech_score REAL,
        fundamental_score REAL,
        pe REAL,
        roe REAL,
        change_pct REAL,
        reason TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_signals_code_ts ON signals(code, ts)",
    "CREATE INDEX IF NOT EXISTS idx_fills_code_ts ON fills(code, ts)",
    "CREATE INDEX IF NOT EXISTS idx_ai_code_ts ON ai_analyses(code, ts)",
    "CREATE INDEX IF NOT EXISTS idx_sector_code_ts ON sector_recommendations(code, ts)",
    """CREATE TABLE IF NOT EXISTS engine_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        ts TEXT,
        cash REAL,
        positions TEXT,
        daily_trade_count INTEGER,
        daily_pnl REAL,
        consec_loss INTEGER,
        peak_asset REAL,
        day_open_asset REAL,
        tick_count INTEGER,
        peak_equity REAL,
        trade_date TEXT
    )""",
]


class Storage:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else BASE_DIR / "storage" / "qmt.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._init()

    def _conn_get(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self.db_path), timeout=10, isolation_level=None,
                check_same_thread=False,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _init(self) -> None:
        with self._lock:
            try:
                c = self._conn_get()
                for stmt in _SCHEMA:
                    c.execute(stmt)
                self._migrate(c)
            except Exception as e:
                logger.warning("Storage init 失败: %s", e)

    def _migrate(self, c) -> None:
        """幂等迁移：为已存在的旧表补列（不破坏既有数据）。

        老库（orders / fills / equity_snapshots）无 mode 列，这里在启动期
        补上，使 paper→live 切换后的记录可带执行模式标记，供复盘区分核实。
        列已存在时 ALTER 抛错，静默忽略即可。
        """
        for stmt in (
            "ALTER TABLE orders ADD COLUMN mode TEXT",
            "ALTER TABLE fills ADD COLUMN mode TEXT",
            "ALTER TABLE equity_snapshots ADD COLUMN mode TEXT",
        ):
            try:
                c.execute(stmt)
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    # ---------- 写入 ----------

    def save_signal(self, s: Signal) -> int:
        with self._lock:
            try:
                c = self._conn_get()
                cur = c.execute(
                    "INSERT INTO signals(ts,code,name,side,score,price,reason,"
                    "factors_json,ai_comment,ai_confidence) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (s.ts.isoformat(), s.code, s.name, s.side, s.score, s.price,
                     s.reason, json.dumps(s.factors, ensure_ascii=False),
                     s.ai_comment, s.ai_confidence),
                )
                return int(cur.lastrowid or 0)
            except Exception as e:
                logger.warning("save_signal 失败: %s", e)
                return 0

    def save_order(self, o: Order, order_id: Optional[str] = None,
                   mode: str = "paper") -> int:
        with self._lock:
            try:
                c = self._conn_get()
                cur = c.execute(
                    "INSERT INTO orders(ts,code,side,quantity,price,order_type,"
                    "account,order_id,mode) VALUES (?,?,?,?,?,?,?,?,?)",
                    (o.ts.isoformat(), o.code, o.side, o.quantity, o.price,
                     o.order_type, o.account, order_id or "",
                     (mode or "paper").lower()),
                )
                return int(cur.lastrowid or 0)
            except Exception as e:
                logger.warning("save_order 失败: %s", e)
                return 0

    def save_fill(self, f: Fill, order_id: Optional[str] = None,
                  mode: str = "paper") -> int:
        with self._lock:
            try:
                c = self._conn_get()
                cur = c.execute(
                    "INSERT INTO fills(ts,code,side,quantity,price,amount,"
                    "account,order_id,mode) VALUES (?,?,?,?,?,?,?,?,?)",
                    (f.ts.isoformat(), f.code, f.side, f.quantity, f.price,
                     f.amount, f.account, order_id or "",
                     (mode or "paper").lower()),
                )
                return int(cur.lastrowid or 0)
            except Exception as e:
                logger.warning("save_fill 失败: %s", e)
                return 0

    def save_ai(self, a: AIAnalysis) -> int:
        with self._lock:
            try:
                c = self._conn_get()
                cur = c.execute(
                    "INSERT INTO ai_analyses(ts,code,model,summary,stance,"
                    "confidence,risks,raw) VALUES (?,?,?,?,?,?,?,?)",
                    (a.ts.isoformat(), a.code, a.model, a.summary, a.stance,
                     a.confidence, a.risks, a.raw),
                )
                return int(cur.lastrowid or 0)
            except Exception as e:
                logger.warning("save_ai 失败: %s", e)
                return 0

    def save_risk_snapshot(self, payload: dict) -> int:
        with self._lock:
            try:
                c = self._conn_get()
                cur = c.execute(
                    "INSERT INTO risk_snapshots(ts,payload_json) VALUES (?,?)",
                    (datetime.now().isoformat(),
                     json.dumps(payload, ensure_ascii=False)),
                )
                return int(cur.lastrowid or 0)
            except Exception as e:
                logger.warning("save_risk_snapshot 失败: %s", e)
                return 0

    def save_equity_snapshot(self, total_asset: float, cash: float,
                             market_value: float, positions_count: int,
                             drawdown_pct: float, mode: str = "paper") -> int:
        """权益快照：供盘后复盘的当日权益曲线 / 收益率 / 最大回撤。

        mode 标记执行模式（paper/live），使实盘切换后的权益曲线可在复盘时
        与模拟盘区分核实。
        """
        with self._lock:
            try:
                c = self._conn_get()
                cur = c.execute(
                    "INSERT INTO equity_snapshots(ts,total_asset,cash,"
                    "market_value,positions_count,drawdown_pct,mode) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (datetime.now().isoformat(), total_asset, cash,
                     market_value, positions_count, drawdown_pct,
                     (mode or "paper").lower()),
                )
                return int(cur.lastrowid or 0)
            except Exception as e:
                logger.warning("save_equity_snapshot 失败: %s", e)
                return 0

    def first_equity_today(self) -> Optional[float]:
        """今日首个权益快照的总资产（用作日内盈亏基线）。无记录返回 None。"""
        with self._lock:
            try:
                c = self._conn_get()
                today = datetime.now().strftime("%Y-%m-%d")
                row = c.execute(
                    "SELECT total_asset FROM equity_snapshots "
                    "WHERE ts >= ? ORDER BY id ASC LIMIT 1",
                    (today + " 00:00:00",),
                ).fetchone()
                return float(row["total_asset"]) if row else None
            except Exception as e:
                logger.debug("first_equity_today 失败: %s", e)
                return None

    def save_engine_state(self, state: dict) -> int:
        """持久化引擎可重启延续的本地账本（paper 持仓/现金/日内计数/峰值）。

        单例行 id=1（UPSERT），供引擎重启后恢复，保证连续多日 Paper 测试的
        持仓与盈亏不被清空。live 模式以 broker 为权威源，无需依赖此表。
        """
        with self._lock:
            try:
                c = self._conn_get()
                c.execute(
                    "INSERT OR REPLACE INTO engine_state("
                    "id, ts, cash, positions, daily_trade_count, daily_pnl,"
                    " consec_loss, peak_asset, day_open_asset, tick_count,"
                    " peak_equity, trade_date) VALUES (1,?,?,?,?,?,?,?,?,?,?,?)",
                    (datetime.now().isoformat(),
                     float(state.get("cash") or 0.0),
                     state.get("positions") or "[]",
                     int(state.get("daily_trade_count") or 0),
                     float(state.get("daily_pnl") or 0.0),
                     int(state.get("consec_loss") or 0),
                     float(state.get("peak_asset") or 0.0),
                     state.get("day_open_asset"),
                     int(state.get("tick_count") or 0),
                     float(state.get("peak_equity") or 0.0),
                     state.get("trade_date")),
                )
                return 1
            except Exception as e:
                logger.warning("save_engine_state 失败: %s", e)
                return 0

    def load_engine_state(self) -> Optional[dict]:
        """读取最近一次持久化的引擎状态（id=1）。无记录返回 None。"""
        with self._lock:
            try:
                c = self._conn_get()
                row = c.execute(
                    "SELECT * FROM engine_state WHERE id=1"
                ).fetchone()
                return dict(row) if row else None
            except Exception as e:
                logger.debug("load_engine_state 失败: %s", e)
                return None

    def save_sector_recommendation(self, rec) -> int:
        """保存单条 sector 推荐（rec 是 sector_scorer.StockRecommendation）。"""
        with self._lock:
            try:
                c = self._conn_get()
                cur = c.execute(
                    "INSERT INTO sector_recommendations("
                    "ts, code, name, sector, sector_label, composite,"
                    " heat_contribution, tech_score, fundamental_score,"
                    " pe, roe, change_pct, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rec.ts.isoformat(), rec.code, rec.name,
                     rec.sector, rec.sector_label, rec.composite,
                     rec.heat_contribution, rec.tech_score,
                     rec.fundamental_score, rec.pe, rec.roe,
                     rec.change_pct, rec.reason),
                )
                return int(cur.lastrowid or 0)
            except Exception as e:
                logger.warning("save_sector_recommendation 失败: %s", e)
                return 0

    def get_sector_recommendations(self, limit: int = 50,
                                   sector: str = None,
                                   code: str = None) -> List[dict]:
        with self._lock:
            try:
                c = self._conn_get()
                if sector:
                    rows = c.execute(
                        "SELECT * FROM sector_recommendations WHERE sector=? "
                        "ORDER BY id DESC LIMIT ?",
                        (sector, limit),
                    ).fetchall()
                elif code:
                    rows = c.execute(
                        "SELECT * FROM sector_recommendations WHERE code=? "
                        "ORDER BY id DESC LIMIT ?",
                        (code, limit),
                    ).fetchall()
                else:
                    rows = c.execute(
                        "SELECT * FROM sector_recommendations "
                        "ORDER BY id DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                return [dict(r) for r in rows]
            except Exception as e:
                logger.warning("查询 sector_recommendations 失败: %s", e)
                return []

    # ---------- 查询 ----------

    def get_signals(self, limit: int = 100, code: Optional[str] = None) -> List[dict]:
        return self._query("signals", limit, code)

    def get_orders(self, limit: int = 100, code: Optional[str] = None) -> List[dict]:
        return self._query("orders", limit, code)

    def get_fills(self, limit: int = 100, code: Optional[str] = None) -> List[dict]:
        return self._query("fills", limit, code)

    def get_equity_snapshots(self, limit: int = 200,
                             code: Optional[str] = None) -> List[dict]:
        """权益快照读取（复盘/审计用）。code 参数保留以兼容 _query 签名。"""
        return self._query("equity_snapshots", limit, code)

    def get_ai_analyses(self, limit: int = 50, code: Optional[str] = None) -> List[dict]:
        return self._query("ai_analyses", limit, code)

    def _query(self, table: str, limit: int, code: Optional[str]) -> List[dict]:
        with self._lock:
            try:
                c = self._conn_get()
                if code:
                    rows = c.execute(
                        f"SELECT * FROM {table} WHERE code=? ORDER BY id DESC LIMIT ?",
                        (code, limit),
                    ).fetchall()
                else:
                    rows = c.execute(
                        f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                return [dict(r) for r in rows]
            except Exception as e:
                logger.warning("查询 %s 失败: %s", table, e)
                return []
# -*- coding: utf-8 -*-
"""双功能区统一看板 —— 一个界面同时看 live 与 paper。

## 为什么是独立进程，而不是改主 Web

主 Web（`web/app.py`）是「单引擎绑定」结构：`create_app(engine)` 把一个
`EventEngine` 塞进 `app.config`，所有路由都从它取数。要在一个页面里看两个区，
必须改 `web/routes.py` 加跨区取数逻辑 —— 而 live 正在跑，**任何 web 层改动都要
重启 live 才生效**，这是当前明确禁止的动作。

所以本看板做成独立进程：
  · **不 import 任何项目模块**（不碰 `config.settings`、不碰 `core.broker`），
    因此对两个引擎零影响，也不会因为改动引擎代码而失配；
  · 数据获取只有两条只读路径：① HTTP 调两边的 `/api/snapshot`；
    ② `sqlite3` 以 `mode=ro` 只读打开两个库取权益快照；
  · 两个区都挂了也能启动，页面会显示「区域离线」而不是白屏。

## 为什么必须做归一化

paper 起始资金 100 万、live 真实 5.8 万，相差约 17 倍。绝对金额放在同一张图上
paper 会把 live 压成一条贴地线。所以权益曲线统一**以窗口首点 = 100 归一**，
只比「形状」（涨跌幅），不比金额。页面上有一条常驻提示说明这一点。

## 用法

    python scripts/zone_dashboard.py            # 默认 127.0.0.1:5002
    python scripts/zone_dashboard.py --port 5010
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]

# 两个区的定义。端口与库路径必须与 core/zone.py 的 zone_config() 一致；
# tests/test_zone_dashboard.py 会把这个一致性钉住（避免两处漂移）。
ZONES = (
    {
        "key": "live",
        "name": "实盘区",
        "sub": "真实资金 · 唯一交易区",
        "port": 5000,
        "db": BASE_DIR / "storage" / "qmt.db",
        "color": "#4f8cff",
    },
    {
        "key": "paper",
        "name": "观察区",
        "sub": "AI 产业链热点观察台 · 禁止真实下单",
        "port": 5001,
        "db": BASE_DIR / "storage_paper" / "qmt.db",
        "color": "#b06bff",
    },
)

SNAP_TIMEOUT = 3.0       # 单区 HTTP 超时
SERIES_LIMIT = 240       # 曲线最多取多少个点（够看形状，又不至于卡浏览器）
SERIES_DAYS = 6          # 曲线窗口：最近 N 个自然日（跨周末时仍能看到上一交易日）

# ---- 热点面板（板块热度 / 个股榜 / 信号流水）----
HOT_DAYS = 5             # 只回最近 N 个有数据的交易日（够看环比，又不至于把下拉撑爆）
HOT_SECTORS = 12         # 板块热度 Top N
HOT_STOCKS = 15          # 个股热度 Top N
HOT_SIGNALS = 20         # 信号流水最近 N 条
_HOT_CACHE_TTL = 60      # 热点聚合缓存 TTL（秒），理由见 build_hot
_HOT_CACHE: dict = {}    # {(zone, day): (写入时刻, payload)} —— 进程内、无持久化


# --------------------------------------------------------------------------- 取数
def _fetch_snapshot(port: int) -> dict:
    """HTTP 取该区的 /api/snapshot。失败返回 {"__error__": "..."}，绝不抛。"""
    url = f"http://127.0.0.1:{int(port)}/api/snapshot"
    try:
        with urllib.request.urlopen(url, timeout=SNAP_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.URLError as e:
        return {"__error__": f"无法连接（{type(e).__name__}: {e.reason}）"}
    except Exception as e:                      # noqa: BLE001 - 看板不得因单区故障崩
        return {"__error__": f"{type(e).__name__}: {e}"}


def _read_series(db_path: Path) -> dict:
    """只读打开该区账本，取最近 N 天的权益快照并按首点归一。

    返回 {"points": [[ts, idx, total_asset], ...], "rows": n, "error": None|str}
    `idx` = total_asset / 窗口首点 * 100，用于跨资金规模比较形状。
    """
    out = {"points": [], "rows": 0, "error": None}
    if not db_path.exists():
        out["error"] = "账本尚未创建"
        return out
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(
                "SELECT ts, total_asset FROM equity_snapshots "
                "WHERE ts >= date('now', ?) ORDER BY ts",
                (f"-{SERIES_DAYS} day",),
            ).fetchall()
            out["rows"] = len(rows)
            if not rows:
                # 窗口内没有数据（新库 / 长休市）：退回全表末 N 行，让用户至少看到历史形状
                rows = conn.execute(
                    "SELECT ts, total_asset FROM equity_snapshots "
                    "ORDER BY ts DESC LIMIT ?", (SERIES_LIMIT,),
                ).fetchall()
                rows = list(reversed(rows))
                out["rows"] = len(rows)
        finally:
            conn.close()
    except Exception as e:                      # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
        return out

    if not rows:
        return out
    # 等距抽样到 SERIES_LIMIT 点，避免 6000+ 点把浏览器压住
    step = max(1, len(rows) // SERIES_LIMIT)
    rows = rows[::step] or rows
    base = float(rows[0][1] or 0.0)
    if base <= 0:
        return out
    out["points"] = [[r[0], round(float(r[1] or 0.0) / base * 100.0, 4),
                      round(float(r[1] or 0.0), 2)] for r in rows]
    return out


def _db_stats(db_path: Path) -> dict:
    """今日成交/委托计数 —— 用来回答「这个区今天到底动没动手」。"""
    out = {"orders_today": None, "fills_today": None, "last_fill_ts": None}
    if not db_path.exists():
        return out
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            today = datetime.now().date().isoformat()
            out["orders_today"] = conn.execute(
                "SELECT COUNT(*) FROM orders WHERE ts LIKE ?", (today + "%",)
            ).fetchone()[0]
            out["fills_today"] = conn.execute(
                "SELECT COUNT(*) FROM fills WHERE ts LIKE ?", (today + "%",)
            ).fetchone()[0]
            row = conn.execute("SELECT MAX(ts) FROM fills").fetchone()
            out["last_fill_ts"] = row[0] if row else None
        finally:
            conn.close()
    except Exception:                           # noqa: BLE001
        pass
    return out


# --------------------------------------------------------------------- 热点取数
def _zone_by_key(key: str):
    """按 key 找区定义；找不到返回 None（端点据此回 400，而不是 500）。"""
    for z in ZONES:
        if z["key"] == key:
            return z
    return None


def _mean(vals) -> "float | None":
    """非 None 值的均值，保留 2 位；全空 ⇒ None。

    不能把 None 当 0 参与均值：那会把「没算出来」摊成「热度 0.0」，
    在降序榜里直接沉底，看起来跟「真的冷门」一模一样。
    """
    xs = [float(v) for v in vals if v is not None]
    if not xs:
        return None
    return round(sum(xs) / len(xs), 2)


def _day_rows(conn, day: str) -> list:
    """取某交易日各票的**最新一条**读数（每股恰好一条）。

    为什么「每股只取最后一条」，而不是对当天全表求均值：
    引擎盘中每 15~30 秒重扫一遍，同一只票一天能落几百行
    （2026-11-06：新易盛 804 行、中际旭创 597 行、中微公司 308 行）。
    对全表求均值等价于按「被扫描次数」加权 —— 扫得勤的票会主导板块热度，
    那是采样伪影，不是行情。取当日最后一条 = 该票最新的真实读数，
    且能逐行对上库里的原始记录（口径可核查）。

    已知局限（实测过，不影响可用性）：每票「最后一条」的时间点并不整齐
    （2026-11-06 从 09:57 到 15:04 都有），因为引擎只在某票仍是候选时才写它。
    曾担心这会让 heat_delta 的**方向**失真，故与「按票内均值、再跨票平均」
    的替代口径逐板块对过账：6 个板块的 Δ 方向完全一致
    （晶圆代工 -2.97 vs -2.14、AI应用 -3.21 vs -2.88、服务器 -2.26 vs -3.86、
    AI芯片 -2.71 vs -3.28、光模块 ±0.00、PCB 新进榜 +1.50），
    即方向可信、幅度有噪声，页面只把方向当箭头用，不展示精确幅度比较。
    """
    rows = conn.execute(
        "SELECT ts, code, name, sector_label, composite, heat_contribution, "
        "tech_score, fundamental_score, pe, roe, change_pct "
        "FROM sector_recommendations WHERE substr(ts,1,10) = ?",
        (day,),
    ).fetchall()
    latest = {}
    for r in rows:
        code = str(r[1] or "")
        if not code:
            continue
        ts = str(r[0] or "")
        cur = latest.get(code)
        if cur is None or ts >= cur["_ts"]:
            latest[code] = {
                "_ts": ts,
                "code": code,
                "name": r[2],
                "sector_label": r[3] or "未分类",
                "composite": r[4],
                "heat": r[5],
                "tech": r[6],
                "fund": r[7],
                "pe": r[8],
                "roe": r[9],
                "change_pct": r[10],
            }
    out = []
    for v in latest.values():
        v.pop("_ts", None)
        out.append(v)
    return out


def _agg_sectors(cur_rows: list, prev_rows: list) -> list:
    """按 sector_label 聚合，并算相对上一交易日的 heat_delta。

    heat_delta = 今日该板块 avg_heat − 上一交易日**同名板块** avg_heat；
    上一交易日没有这个板块 ⇒ None（不是 0：0 会被读成「热度没变」，
    而实际是「这板块是今天才进榜的」，含义完全不同）。
    """
    def agg(rows: list) -> dict:
        grouped = {}
        for r in rows:
            grouped.setdefault(r["sector_label"], []).append(r)
        out = {}
        for label, items in grouped.items():
            # 龙头 = 板块内综合分最高的一只；综合分全空时退回第一只，
            # 保证 top 永不为空（页面要显示「龙头是谁」）。
            best = max(items, key=lambda x: (x["composite"] is not None,
                                             x["composite"] or 0.0))
            name = best.get("name")
            out[label] = {
                "label": label,
                "n": len(items),
                "avg_heat": _mean([i["heat"] for i in items]),
                "avg_composite": _mean([i["composite"] for i in items]),
                "avg_change": _mean([i["change_pct"] for i in items]),
                "top": f"{best['code']} {name}" if name else str(best["code"]),
            }
        return out

    cur, prev = agg(cur_rows), agg(prev_rows)
    out = []
    for label, s in cur.items():
        p = prev.get(label)
        ph = p["avg_heat"] if p else None
        delta = None
        if s["avg_heat"] is not None and ph is not None:
            delta = round(s["avg_heat"] - ph, 2)
        out.append(dict(s, heat_delta=delta))
    # avg_heat 降序；None 沉底（用 -1e9 兜住，避免 None 与 float 比较报错）
    out.sort(key=lambda s: (-(s["avg_heat"] if s["avg_heat"] is not None else -1e9),
                            s["label"]))
    return out[:HOT_SECTORS]


def _recent_signals(conn, limit: int) -> list:
    """最近 N 条信号（不限当天，按 ts 降序）。blocked_by 归一成空串。"""
    rows = conn.execute(
        "SELECT ts, code, name, side, score, reason, blocked_by "
        "FROM signals ORDER BY ts DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    return [{
        "ts": r[0], "code": r[1], "name": r[2], "side": r[3],
        "score": r[4], "reason": r[5], "blocked_by": r[6] or "",
    } for r in rows]


def _query_hot(db_path: Path, day=None) -> dict:
    """热点面板的裸取数 + 聚合（不含缓存）。**绝不抛**：异常一律折成 error 串。"""
    out = {"day": None, "days": [], "prev_day": None,
           "sectors": [], "stocks": [], "signals": [], "error": None}
    if not db_path.exists():
        out["error"] = "账本尚未创建（该区首次启动后产生）"
        return out
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    except Exception as e:                          # noqa: BLE001
        out["error"] = f"无法打开账本：{type(e).__name__}: {e}"
        return out
    try:
        try:
            days = [r[0] for r in conn.execute(
                "SELECT substr(ts,1,10) AS d FROM sector_recommendations "
                "WHERE ts IS NOT NULL AND length(ts) >= 10 "
                "GROUP BY d ORDER BY d DESC LIMIT ?", (HOT_DAYS,),
            ).fetchall() if r[0]]
        except Exception as e:                      # noqa: BLE001 - 缺表也走降级
            days = []
            out["error"] = f"板块表不可读：{type(e).__name__}: {e}"
        out["days"] = days

        if not days:
            if out["error"] is None:
                out["error"] = "该区尚无板块数据（交易日盘中产生）"
        else:
            use = day if day in days else days[0]
            out["day"] = use
            idx = days.index(use)
            prev = days[idx + 1] if idx + 1 < len(days) else None
            out["prev_day"] = prev
            try:
                cur_rows = _day_rows(conn, use)
                prev_rows = _day_rows(conn, prev) if prev else []
                out["sectors"] = _agg_sectors(cur_rows, prev_rows)
                stocks = sorted(
                    cur_rows,
                    key=lambda s: -(s["composite"] if s["composite"] is not None else -1e9))
                out["stocks"] = stocks[:HOT_STOCKS]
            except Exception as e:                  # noqa: BLE001
                out["sectors"], out["stocks"] = [], []
                out["error"] = f"板块聚合失败：{type(e).__name__}: {e}"

        # 信号表独立降级：缺表 / 查询失败只影响流水，不拖掉已经算好的板块
        try:
            out["signals"] = _recent_signals(conn, HOT_SIGNALS)
        except Exception:                           # noqa: BLE001
            out["signals"] = []
    finally:
        try:
            conn.close()
        except Exception:                           # noqa: BLE001
            pass
    return out


def _hot_cache_clear() -> None:
    """清空热点缓存（自检 / 测试用）。"""
    _HOT_CACHE.clear()


def build_hot(zone_key: str, day=None) -> dict:
    """热点面板：板块热度 / 个股榜 / 信号流水（只读 + 进程内缓存）。

    ## 为什么必须缓存

    页面每 5 秒轮询 `/api/zones`。如果热点查询也挂进那条 5 秒链，live 库
    10 万行 `sector_recommendations` 会被反复全表聚合，与正在写库的 live 引擎
    抢锁 —— 这是整个看板唯一可能**影响到生产**的路径。所以热点单独缓存，
    key=(zone, day)，TTL 60 秒；页面侧另开 30 秒轮询，不跟 5 秒的 tick 走。

    `zone_key` 非法 ⇒ 抛 ValueError（端点映射成 400，不是 500）。
    """
    z = _zone_by_key(zone_key)
    if z is None:
        raise ValueError(
            f"未知区 {zone_key!r}，可选：{[x['key'] for x in ZONES]}")

    now = time.time()
    key = (zone_key, day)
    hit = _HOT_CACHE.get(key)
    if hit is not None and (now - hit[0]) < _HOT_CACHE_TTL:
        data = dict(hit[1])
    else:
        data = _query_hot(Path(z["db"]), day)
        # 只缓存落在「有效 key 空间」内的结果：day 是外部参数，若照收不拒，
        # (zone, 任意字符串) 会把缓存撑成无界增长。
        if day is None or day in data.get("days", []):
            _HOT_CACHE[key] = (now, data)
            if len(_HOT_CACHE) > 2 * HOT_DAYS + 4:   # 实测上限：2 区 × 5 日 + 兜底
                for k, v in list(_HOT_CACHE.items()):
                    if now - v[0] >= _HOT_CACHE_TTL:
                        _HOT_CACHE.pop(k, None)
        data = dict(data)

    return {
        "zone": zone_key,
        "day": data.get("day"),
        "days": data.get("days", []),
        "prev_day": data.get("prev_day"),
        "sectors": data.get("sectors", []),
        "stocks": data.get("stocks", []),
        "signals": data.get("signals", []),
        "error": data.get("error"),
    }


def _pick(snap: dict, path: str, default=None):
    """按 "risk.halted" 这种点路径取嵌套字段。"""
    cur = snap
    for part in path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(part)
    return default if cur is None else cur


def build_payload() -> dict:
    zones = {}
    for z in ZONES:
        snap = _fetch_snapshot(z["port"])
        err = snap.get("__error__") if isinstance(snap, dict) else "未知错误"
        online = not err
        zones[z["key"]] = {
            "key": z["key"],
            "name": z["name"],
            "sub": z["sub"],
            "port": z["port"],
            "color": z["color"],
            "online": online,
            "error": err if not online else None,
            # 身份
            "exec_mode": _pick(snap, "exec_mode") if online else None,
            "strategy_mode": _pick(snap, "strategy_mode") if online else None,
            # 通道
            "data_mode": _pick(snap, "data_mode") if online else None,
            "broker_mode": _pick(snap, "broker_mode") if online else None,
            "broker_connected": _pick(snap, "broker_connected") if online else None,
            # 资金
            "total_asset": _pick(snap, "total_asset") if online else None,
            "cash": _pick(snap, "cash") if online else None,
            "positions_n": len(_pick(snap, "positions", {}) or {}) if online else None,
            # 风控
            "halted": _pick(snap, "risk.halted") if online else None,
            "halt_reason": _pick(snap, "risk.halt_reason") if online else None,
            "peak_asset": _pick(snap, "risk.peak_asset") if online else None,
            "asset_trusted": _pick(snap, "risk.asset_trusted") if online else None,
            # 会话 / 漏斗
            "session_state": _pick(snap, "session.state") if online else None,
            "trading_now": _pick(snap, "session.trading_now") if online else None,
            "gate_funnel": _pick(snap, "gate_funnel") if online else None,
            "no_entry_reason": _pick(snap, "no_entry_reason") if online else None,
            "tick": _pick(snap, "tick") if online else None,
            "market_data_ts": _pick(snap, "market_data_ts") if online else None,
            "degraded": _pick(snap, "degraded", []) if online else [],
            # 账本
            "series": _read_series(z["db"]),
            "dbtoday": _db_stats(z["db"]),
        }
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "series_days": SERIES_DAYS,
        "zones": zones,
    }


# --------------------------------------------------------------------------- 页面
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>双功能区 · 统一看板</title>
<style>
  :root{
    --bg:#0e1117; --panel:#161b24; --panel2:#1c222d; --line:#262e3b;
    --fg:#e6ebf2; --fg2:#9aa7b8; --fg3:#6b7789;
    --live:#4f8cff; --paper:#b06bff; --ok:#3fbf7f; --warn:#e0a54a; --bad:#e0575a;
  }
  @media (prefers-color-scheme: light){
    :root{ --bg:#f4f6fa; --panel:#fff; --panel2:#eef1f7; --line:#dde3ec;
           --fg:#1a2130; --fg2:#546073; --fg3:#8b95a6; }
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.55 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
  .wrap{max-width:1280px;margin:0 auto;padding:22px 20px 60px}
  header{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin-bottom:6px}
  h1{font-size:19px;margin:0;font-weight:600;letter-spacing:.3px}
  .stamp{color:var(--fg3);font-size:12px}
  .tip{margin:10px 0 18px;padding:10px 13px;border-radius:8px;
       background:var(--panel2);border-left:3px solid var(--warn);
       color:var(--fg2);font-size:12.5px}
  .grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
  @media (max-width:880px){ .grid{grid-template-columns:1fr} }
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;
        padding:16px 16px 14px;position:relative;overflow:hidden}
  .card::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;
                background:var(--zc)}
  .hd{display:flex;align-items:center;gap:10px;margin-bottom:2px}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--ok);
       box-shadow:0 0 0 3px color-mix(in srgb,var(--ok) 22%,transparent)}
  .dot.off{background:var(--bad);box-shadow:0 0 0 3px color-mix(in srgb,var(--bad) 22%,transparent)}
  .zname{font-size:16px;font-weight:600}
  .sub{color:var(--fg3);font-size:12px;margin-bottom:14px}
  .kv{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;font-size:13px}
  .kv dt{color:var(--fg2);white-space:nowrap}
  .kv dd{margin:0;font-variant-numeric:tabular-nums;text-align:right}
  .big{font-size:26px;font-weight:600;font-variant-numeric:tabular-nums;
       letter-spacing:-.3px;margin:6px 0 2px}
  .muted{color:var(--fg3);font-size:12px}
  .pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11.5px;
        border:1px solid var(--line);color:var(--fg2);background:var(--panel2)}
  .pill.bad{color:var(--bad);border-color:color-mix(in srgb,var(--bad) 40%,transparent)}
  .pill.warn{color:var(--warn);border-color:color-mix(in srgb,var(--warn) 40%,transparent)}
  .pill.ok{color:var(--ok);border-color:color-mix(in srgb,var(--ok) 40%,transparent)}
  .chartbox{margin-top:16px;background:var(--panel);border:1px solid var(--line);
            border-radius:12px;padding:14px 16px 8px}
  .chartbox h2{font-size:14px;margin:0 0 2px;font-weight:600}
  .legend{display:flex;gap:16px;font-size:12px;color:var(--fg2);margin:8px 0 4px}
  .legend i{display:inline-block;width:10px;height:10px;border-radius:2px;
            margin-right:5px;vertical-align:-1px}
  canvas{width:100%;height:230px;display:block}
  table{width:100%;border-collapse:collapse;margin-top:16px;font-size:13px}
  caption{text-align:left;color:var(--fg3);font-size:12px;padding-bottom:8px}
  th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left}
  th{color:var(--fg2);font-weight:500;font-size:12px}
  td.v{font-variant-numeric:tabular-nums}
  tr.diff td{background:color-mix(in srgb,var(--warn) 9%,transparent)}
  tr.diff td:first-child::after{content:" ≠";color:var(--warn);font-size:11px}
  .foot{margin-top:20px;color:var(--fg3);font-size:12px;line-height:1.7}
  code{background:var(--panel2);padding:1px 5px;border-radius:4px;font-size:12px}
  a{color:var(--live)}

  /* ---- 热点面板：板块热度 / 个股榜 / 信号流水 ---- */
  .hotctl{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:10px 0 16px}
  .seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
  .seg button{appearance:none;border:0;background:transparent;color:var(--fg2);
              font:inherit;font-size:12.5px;padding:5px 13px;cursor:pointer}
  .seg button + button{border-left:1px solid var(--line)}
  .seg button.on{background:var(--zc,var(--live));color:#fff}
  select{background:var(--panel2);color:var(--fg);border:1px solid var(--line);
         border-radius:8px;padding:5px 8px;font:inherit;font-size:12.5px}
  .hotgrid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:22px}
  @media (max-width:880px){ .hotgrid{grid-template-columns:1fr} }
  .hotgrid h3,.hotsec h3{font-size:13px;margin:0 0 10px;font-weight:600}
  .bar{margin-bottom:10px}
  .bar .r1{display:flex;align-items:baseline;gap:8px;font-size:12.5px}
  .bar .nm{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .bar .hv{font-variant-numeric:tabular-nums}
  .bar .dl{font-size:11.5px;font-variant-numeric:tabular-nums;min-width:52px;text-align:right}
  /* 中国习惯：涨 = 红（--bad #e0575a），跌 = 绿（--ok #3fbf7f） */
  .up{color:var(--bad)} .down{color:var(--ok)} .flat{color:var(--fg3)}
  .track{height:6px;border-radius:3px;background:var(--panel2);margin-top:4px;overflow:hidden}
  .fill{height:100%;border-radius:3px;background:var(--zc,var(--live));opacity:.85}
  .bar .ld{font-size:11px;color:var(--fg3);margin-top:3px}
  .empty{color:var(--fg3);font-size:12.5px;padding:16px 0}
  table.mini{margin-top:0;font-size:12px}
  table.mini th,table.mini td{padding:5px 7px}
  tr.blocked td{background:color-mix(in srgb,var(--warn) 13%,transparent)}
  .bl{color:var(--warn)}
  .sigwrap{max-height:330px;overflow:auto}
  .hotsec{margin-top:22px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>双功能区 · 统一看板</h1>
    <span class="stamp" id="stamp">读取中…</span>
  </header>
  <div class="tip">
    两个区基础设施同构，但<b>资金规模不同</b>：实盘区是券商实际资产，观察区固定 100 万起始。
    权益曲线已按<b>窗口首点 = 100 归一</b>，<b>只比形状（涨跌幅），不比金额</b>；
    持仓数量与绝对收益同样<b>不可跨区横比</b>。
    <br>漏斗五段依次为 <b>宇宙 → 收盘>MA60 趋势 → 动量闸门 → 日偏置 → 评分通过</b>，
    用来回答「今天为什么没买」和「热点走到哪一关了」。
  </div>

  <div class="grid" id="cards"></div>

  <div class="chartbox">
    <h2>权益曲线（归一化，首点 = 100）</h2>
    <div class="legend" id="legend"></div>
    <canvas id="cv"></canvas>
  </div>

  <div class="chartbox" id="hotbox">
    <h2>板块热度 · 个股榜 · 信号流水</h2>
    <div class="muted" style="margin-top:2px">
      回答「AI 产业链哪个环节在升温、哪些票是热点」。同一交易日每只票会被引擎重扫上百次，
      这里<b>每票只取当日最后一条读数</b>（每票一票，不按扫描次数加权）；
      环比 = 与上一交易日同名板块的平均热度之差，<b>只看趋势方向</b>。
    </div>
    <div class="hotctl">
      <div class="seg" id="hotzones"></div>
      <select id="hotday"></select>
      <span class="muted" id="hotstamp">读取中…</span>
    </div>
    <div class="hotgrid">
      <div>
        <h3>板块热度 Top 12</h3>
        <div id="hotsec"></div>
      </div>
      <div>
        <h3>个股热度 Top 15</h3>
        <div id="hotstk"></div>
      </div>
    </div>
    <div class="hotsec">
      <h3>信号流水（最近 20 条 · 黄底为被风控拦下）</h3>
      <div id="hotsig"></div>
    </div>
  </div>

  <table id="cmp">
    <caption>逐项对照 —— 高亮行表示两个区当前<b>取值不同</b></caption>
    <thead><tr><th>项目</th><th>实盘区</th><th>观察区</th></tr></thead>
    <tbody></tbody>
  </table>

  <div class="foot">
    数据来源：两个区各自的 <code>/api/snapshot</code>（HTTP 只读）与账本
    <code>equity_snapshots</code>（<code>mode=ro</code> 只读打开）。本看板不写任何库、不改任何引擎状态。
    区域离线时只影响本卡片显示，不代表该区引擎异常。
    <br>热点面板另读账本里的 <code>sector_recommendations</code> 与 <code>signals</code>（同样
    <code>mode=ro</code>），服务端<b>缓存 60 秒</b>、页面<b>每 30 秒</b>刷新一次，
    避免反复全表聚合与正在写库的引擎抢锁。
  </div>
</div>

<script>
const fmtMoney = v => (v===null||v===undefined) ? "—"
  : v.toLocaleString("zh-CN",{minimumFractionDigits:2,maximumFractionDigits:2});
const esc = s => String(s===null||s===undefined?"—":s);

function card(z){
  const off = !z.online;
  const halted = z.halted ? `<span class="pill bad">熔断 · ${esc(z.halt_reason)}</span>`
                          : `<span class="pill ok">未熔断</span>`;
  const trust = z.asset_trusted === false
      ? `<span class="pill warn">总资产口径不可信</span>` : "";
  const deg = (z.degraded||[]).map(d=>`<span class="pill warn">${esc(d)}</span>`).join(" ");
  const f = z.gate_funnel || {};
  const day = z.dbtoday || {};
  return `<div class="card" style="--zc:${z.color}">
    <div class="hd"><span class="dot ${off?'off':''}"></span>
      <span class="zname">${esc(z.name)}</span>
      <span class="muted">:${z.port}</span></div>
    <div class="sub">${esc(z.sub)}</div>
    ${off ? `<div class="pill bad">区域离线</div>
             <div class="muted" style="margin-top:8px">${esc(z.error)}</div>
             <div class="muted" style="margin-top:6px">若该区刚重启，首个快照要网络预热约 15 秒，稍等即恢复。</div>`
          : `
    <div class="big">${fmtMoney(z.total_asset)}</div>
    <div class="muted">总资产 · 现金 ${fmtMoney(z.cash)} · 持仓 ${esc(z.positions_n)} 只</div>
    <dl class="kv" style="margin-top:14px">
      <dt>执行 / 策略</dt><dd>${esc(z.exec_mode)} / ${esc(z.strategy_mode)}</dd>
      <dt>行情源</dt><dd>${esc(z.data_mode)}</dd>
      <dt>交易通道</dt><dd>${esc(z.broker_mode)}${z.broker_connected?" · 已连":" · 未连"}</dd>
      <dt>会话</dt><dd>${esc(z.session_state)}${z.trading_now?" · 交易中":""}</dd>
      <dt>今日 委托/成交</dt><dd>${esc(day.orders_today)} / ${esc(day.fills_today)}</dd>
      <dt>最近成交</dt><dd>${esc(day.last_fill_ts)}</dd>
      <dt>漏斗 宇宙→评分</dt><dd>${esc(f.universe_n)} → ${esc(f.trend_up_n)} → ${esc(f.momentum_pass_n)} → ${esc(f.bias_ok_n)} → ${esc(f.score_ok_n)}</dd>
      <dt>峰值资产</dt><dd>${fmtMoney(z.peak_asset)}</dd>
    </dl>
    <div style="margin-top:12px;display:flex;gap:6px;flex-wrap:wrap">
      ${halted} ${trust} ${deg}
      ${z.no_entry_reason?`<span class="pill">${esc(z.no_entry_reason)}</span>`:""}
    </div>`}
  </div>`;
}

function rows(){
  const L = data.zones.live, P = data.zones.paper;
  const items = [
    ["执行模式", L.exec_mode, P.exec_mode],
    ["策略模式", L.strategy_mode, P.strategy_mode],
    ["行情源", L.data_mode, P.data_mode],
    ["交易通道", L.broker_mode, P.broker_mode],
    ["券商已连接", L.broker_connected, P.broker_connected],
    ["总资产", fmtMoney(L.total_asset), fmtMoney(P.total_asset)],
    ["现金", fmtMoney(L.cash), fmtMoney(P.cash)],
    ["持仓只数", L.positions_n, P.positions_n],
    ["熔断", L.halted, P.halted],
    ["总资产口径可信", L.asset_trusted, P.asset_trusted],
    ["会话状态", L.session_state, P.session_state],
    ["今日成交", L.dbtoday && L.dbtoday.fills_today, P.dbtoday && P.dbtoday.fills_today],
    ["账本快照行数", L.series && L.series.rows, P.series && P.series.rows],
  ];
  document.querySelector("#cmp tbody").innerHTML = items.map(([k,a,b])=>{
    const same = JSON.stringify(a) === JSON.stringify(b);
    return `<tr class="${same?"":"diff"}"><td>${k}</td>
      <td class="v">${a===null||a===undefined?"—":a}</td>
      <td class="v">${b===null||b===undefined?"—":b}</td></tr>`;
  }).join("");
}

function chart(){
  const cv = document.getElementById("cv");
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth, H = 230;
  cv.width = W*dpr; cv.height = H*dpr;
  const g = cv.getContext("2d"); g.scale(dpr,dpr); g.clearRect(0,0,W,H);

  const pad = {l:44,r:12,t:10,b:22};
  const series = [];
  for (const k of ["live","paper"]){
    const z = data.zones[k];
    const pts = (z.series && z.series.points) || [];
    if (pts.length) series.push({k, color:z.color, name:z.name, pts});
  }
  document.getElementById("legend").innerHTML = series.length
    ? series.map(s=>`<span><i style="background:${s.color}"></i>${s.name}（${s.pts.length} 点，${esc(s.pts[0][0].slice(0,10))} 起）</span>`).join("")
    : `<span>窗口内两个区都没有权益快照 —— 休市期间不产生快照属正常</span>`;
  if (!series.length) return;

  const all = series.flatMap(s=>s.pts.map(p=>p[1]));
  let lo = Math.min(...all), hi = Math.max(...all);
  if (hi - lo < 2){ lo -= 1; hi += 1; }
  const mg = (hi-lo)*0.12; lo -= mg; hi += mg;

  const X = i => pad.l + (W-pad.l-pad.r) * (i/(Math.max(2,Math.max(...series.map(s=>s.pts.length)))-1));
  const Y = v => pad.t + (H-pad.t-pad.b) * (1-(v-lo)/(hi-lo));

  // 网格 + 100 基准线
  g.font = "11px ui-monospace,monospace"; g.textBaseline = "middle";
  for (let t=0;t<=4;t++){
    const v = lo + (hi-lo)*t/4, y = Y(v);
    g.strokeStyle = "rgba(128,140,160,.18)"; g.beginPath();
    g.moveTo(pad.l,y); g.lineTo(W-pad.r,y); g.stroke();
    g.fillStyle = "rgba(150,162,178,.9)"; g.textAlign="right";
    g.fillText(v.toFixed(1), pad.l-6, y);
  }
  if (lo < 100 && hi > 100){
    g.strokeStyle="rgba(224,165,74,.5)"; g.setLineDash([4,4]); g.beginPath();
    g.moveTo(pad.l,Y(100)); g.lineTo(W-pad.r,Y(100)); g.stroke(); g.setLineDash([]);
  }
  for (const s of series){
    g.strokeStyle = s.color; g.lineWidth = 1.8; g.beginPath();
    s.pts.forEach((p,i)=>{ const x=X(i),y=Y(p[1]); i?g.lineTo(x,y):g.moveTo(x,y); });
    g.stroke();
  }
}

// ---- 热点面板：独立 30 秒轮询，不跟 5 秒的 tick 走（live 库 10 万行，别抢锁）----
const HOT_ZONES = [["live","实盘区"],["paper","观察区"]];
let hotZone = "live", hotDay = "";

const n2  = v => (v===null||v===undefined) ? "—" : Number(v).toFixed(2);
const pct = v => (v===null||v===undefined) ? "—"
                : ((v>0?"+":"") + Number(v).toFixed(2) + "%");

function hotUrl(){
  return "api/hot?zone=" + encodeURIComponent(hotZone)
       + (hotDay ? "&day=" + encodeURIComponent(hotDay) : "");
}

function renderHot(d){
  // 面板配色跟随所选区：复用已有 --live / --paper，不引入新色值
  document.getElementById("hotbox")
    .style.setProperty("--zc", hotZone === "paper" ? "var(--paper)" : "var(--live)");

  document.getElementById("hotzones").innerHTML = HOT_ZONES.map(([k,n])=>
    `<button data-z="${k}" class="${k===hotZone?"on":""}">${n}</button>`).join("");
  document.querySelectorAll("#hotzones button").forEach(b=>{
    b.onclick = ()=>{ if (hotZone === b.dataset.z) return;
                      hotZone = b.dataset.z; hotDay = ""; hotTick(); };
  });

  const days = d.days || [];
  const cur = d.day || "";
  const sel = document.getElementById("hotday");
  sel.innerHTML = days.length
    ? days.map(x=>`<option value="${x}"${x===cur?" selected":""}>${x}</option>`).join("")
    : `<option value="">无数据</option>`;
  sel.onchange = ()=>{ hotDay = sel.value; hotTick(); };
  hotDay = cur;                    // 服务端解析后的日期，后续轮询沿用

  document.getElementById("hotstamp").textContent = d.error
    ? "⚠ " + d.error
    : "热点 " + (cur || "—") + (d.prev_day ? " · 环比基准 " + d.prev_day : " · 无环比基准");

  // 板块热度：横向条形（CSS width 百分比，不用 canvas）
  const sec = d.sectors || [];
  const mx = Math.max(1e-9, ...sec.map(s=>Number(s.avg_heat)||0));
  document.getElementById("hotsec").innerHTML = sec.length ? sec.map((s,i)=>{
    const w = Math.max(2, (Number(s.avg_heat)||0)/mx*100);
    const dd = s.heat_delta;
    const cls = (dd===null||dd===undefined) ? "flat" : (dd>0?"up":(dd<0?"down":"flat"));
    const txt = (dd===null||dd===undefined) ? "—"
              : (dd > 0 ? "▲ +" + Number(dd).toFixed(2)
                        : (dd < 0 ? "▼ " + Number(dd).toFixed(2) : "0.00"));
    return `<div class="bar">
      <div class="r1"><span class="nm">${i+1}. ${esc(s.label)}</span>
        <span class="hv">${n2(s.avg_heat)}</span>
        <span class="dl ${cls}">${txt}</span></div>
      <div class="track"><div class="fill" style="width:${w.toFixed(1)}%"></div></div>
      <div class="ld">${esc(s.n)} 只 · 综合 ${n2(s.avg_composite)}
        · 均涨 ${pct(s.avg_change)} · 龙头 ${esc(s.top)}</div>
    </div>`;
  }).join("") : `<div class="empty">该区尚无板块数据（交易日盘中产生）</div>`;

  // 个股榜
  const stk = d.stocks || [];
  document.getElementById("hotstk").innerHTML = stk.length ? `<table class="mini">
    <thead><tr><th>代码</th><th>名称</th><th>环节</th><th>综合</th><th>热度</th>
      <th>技术</th><th>基本面</th><th>涨跌幅</th></tr></thead>
    <tbody>${stk.map(s=>`<tr>
      <td class="v">${esc(s.code)}</td>
      <td>${esc(s.name)}</td>
      <td>${esc(s.sector_label)}</td>
      <td class="v">${n2(s.composite)}</td>
      <td class="v">${n2(s.heat)}</td>
      <td class="v">${n2(s.tech)}</td>
      <td class="v">${n2(s.fund)}</td>
      <td class="v ${s.change_pct>0?"up":(s.change_pct<0?"down":"")}">${pct(s.change_pct)}</td>
    </tr>`).join("")}</tbody></table>`
    : `<div class="empty">该区尚无个股数据（交易日盘中产生）</div>`;

  // 信号流水：blocked_by 非空 ⇒ 整行标黄 + 显示拦截原因
  const sig = d.signals || [];
  document.getElementById("hotsig").innerHTML = sig.length ? `<div class="sigwrap"><table class="mini">
    <thead><tr><th>时间</th><th>代码</th><th>名称</th><th>方向</th>
      <th>评分</th><th>理由</th><th>风控</th></tr></thead>
    <tbody>${sig.map(s=>{
      const blocked = !!(s.blocked_by && String(s.blocked_by).trim());
      return `<tr class="${blocked?"blocked":""}">
        <td class="v">${esc(String(s.ts||"").replace("T"," ").slice(0,19))}</td>
        <td class="v">${esc(s.code)}</td>
        <td>${esc(s.name)}</td>
        <td class="${s.side==="BUY"?"up":"down"}">${esc(s.side)}</td>
        <td class="v">${n2(s.score)}</td>
        <td>${esc(s.reason)}</td>
        <td class="${blocked?"bl":""}">${blocked?esc(s.blocked_by):"—"}</td>
      </tr>`;}).join("")}</tbody></table></div>`
    : `<div class="empty">该区尚无信号流水（未产生买卖信号）</div>`;
}

async function hotTick(){
  try{
    const r = await fetch(hotUrl(), {cache:"no-store"});
    if (!r.ok){
      document.getElementById("hotstamp").textContent = "热点接口 HTTP " + r.status;
      return;
    }
    renderHot(await r.json());
  }catch(e){
    document.getElementById("hotstamp").textContent = "热点读取失败：" + e;
  }
}
hotTick(); setInterval(hotTick, 30000);

let data = null;
async function tick(){
  try{
    const r = await fetch("api/zones", {cache:"no-store"});
    data = await r.json();
    document.getElementById("stamp").textContent = "更新于 " + data.generated_at +
      " · 曲线窗口 " + data.series_days + " 天";
    document.getElementById("cards").innerHTML =
      card(data.zones.live) + card(data.zones.paper);
    rows(); chart();
  }catch(e){
    document.getElementById("stamp").textContent = "读取失败：" + e;
  }
}
tick(); setInterval(tick, 5000);
window.addEventListener("resize", ()=>{ if(data) chart(); });
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- 服务
class Handler(BaseHTTPRequestHandler):
    server_version = "ZoneDashboard/1.0"

    def log_message(self, fmt, *args):        # 静音访问日志，避免刷屏
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:                      # noqa: BLE001 - 浏览器断开不算错
            pass

    def do_GET(self):                          # noqa: N802 - BaseHTTPRequestHandler 约定
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/zones":
            try:
                payload = build_payload()
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as e:             # noqa: BLE001
                body = json.dumps({"error": f"{type(e).__name__}: {e}"},
                                  ensure_ascii=False).encode("utf-8")
                self._send(500, body, "application/json; charset=utf-8")
        elif path == "/api/hot":
            # 参数错误回 400；取数异常折成 error 字段回 200（页面永不白屏）
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            zone_key = (q.get("zone") or ["live"])[0]
            day = (q.get("day") or [""])[0] or None
            try:
                payload = build_hot(zone_key, day)
            except ValueError as e:
                body = json.dumps({"error": str(e)},
                                  ensure_ascii=False).encode("utf-8")
                self._send(400, body, "application/json; charset=utf-8")
                return
            except Exception as e:             # noqa: BLE001 - 兜底不得升级成 500
                payload = {"zone": zone_key, "day": day, "days": [],
                           "prev_day": None, "sectors": [], "stocks": [],
                           "signals": [], "error": f"{type(e).__name__}: {e}"}
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/healthz":
            self._send(200, b"ok", "text/plain; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="双功能区统一看板（只读）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5002)
    ap.add_argument("--once", action="store_true",
                    help="只打印一次聚合 JSON 后退出（自检用）")
    ap.add_argument("--hot-once", metavar="ZONE", default=None,
                    help="只打印一次该区的热点面板 JSON 后退出（自检用，ZONE=live|paper）")
    ap.add_argument("--day", default=None, metavar="YYYY-MM-DD",
                    help="配合 --hot-once 指定交易日")
    args = ap.parse_args()

    if args.hot_once:
        try:
            hot = build_hot(args.hot_once, args.day)
        except ValueError as e:
            print(f"[zone-dashboard] {e}", file=sys.stderr)
            return 2
        print(json.dumps(hot, ensure_ascii=False, indent=2))
        return 0

    if args.once:
        print(json.dumps(build_payload(), ensure_ascii=False, indent=2))
        return 0

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[zone-dashboard] http://{args.host}:{args.port}"
          f"  （live :{ZONES[0]['port']} / paper :{ZONES[1]['port']}，只读）", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

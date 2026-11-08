# -*- coding: utf-8 -*-
"""双功能区开盘自检 —— 一条命令看清 live / paper 是否真的能干活。

## 为什么需要它

2026-11-08 一天的教训全是「看起来在跑，其实没干活」：

- live 进程活着、面板有数，但 `data_mode=mock` + `broker=disconnected` + `halted=true`
  —— **三个字段同时错，任何一个都足以让成交量为零**，而单独看每一个都像「小问题」；
- paper 独立成区之后，候选池文件没了 ⇒ `动态候选池: 0 只活跃`，**信号层直接空转**，
  而面板上「运行中」一切正常；
- paper 曾经覆写 live 的 `storage/dynamic_universe.json`（候选池决定 live 买什么），
  这种越界**不会产生任何报错**，只能靠「文件 mtime vs 进程启动时刻」抓。

所以本脚本只做一件事：把「能不能干活」拆成一组可判定的断言，逐条 PASS/WARN/FAIL。

## 用法

    python scripts/zone_preflight.py            # 全量检查
    python scripts/zone_preflight.py --quiet    # 只打印问题行

退出码：0 = 无 FAIL；1 = 有 FAIL（可直接用于定时任务或开盘前脚本）。

## 边界

**纯只读**：只发 HTTP GET、只 `mode=ro` 读库、只 `stat` 文件。不写任何库、
不 kill 任何进程、不改任何配置。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from core.zone import zone_config  # noqa: E402

DASH_PORT = 5002
HTTP_TIMEOUT = 6.0

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_ICON = {PASS: "✓", WARN: "!", FAIL: "✗"}

_rows = []


def add(level: str, zone: str, item: str, detail: str) -> None:
    _rows.append((level, zone, item, detail))


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _snapshot(port: int) -> dict | None:
    try:
        return _get_json(f"http://127.0.0.1:{port}/api/snapshot")
    except Exception:                                  # noqa: BLE001
        return None


def _listening(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz",
                                    timeout=3) as r:
            return r.status == 200
    except Exception:                                  # noqa: BLE001
        try:
            _get_json(f"http://127.0.0.1:{port}/api/snapshot")
            return True
        except Exception:                              # noqa: BLE001
            return False


def _db_latest(db: Path) -> tuple:
    """该区账本最后的权益快照时间、行数、今日成交数。"""
    if not db.exists():
        return None, 0, None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            n = conn.execute("SELECT COUNT(*) FROM equity_snapshots").fetchone()[0]
            ts = conn.execute("SELECT MAX(ts) FROM equity_snapshots").fetchone()[0]
            today = datetime.now().date().isoformat()
            fills = conn.execute("SELECT COUNT(*) FROM fills WHERE ts LIKE ?",
                                 (today + "%",)).fetchone()[0]
            return ts, n, fills
        finally:
            conn.close()
    except Exception:                                  # noqa: BLE001
        return None, 0, None


def check_port(port: int, name: str) -> bool:
    ok = _listening(port)
    add(PASS if ok else FAIL, name, f"端口 {port}",
        "在监听" if ok else "无响应（引擎可能没起来）")
    return ok


def check_zone(key: str, snap: dict | None) -> None:
    """一个区的「能不能干活」判定。"""
    cfg = zone_config(key)
    label = {"live": "实盘区", "paper": "观察区"}[key]

    if not snap:
        add(FAIL, label, "快照", "取不到 /api/snapshot ⇒ 该区不可用")
        return

    add(PASS, label, "快照", f"tick={snap.get('tick')} 会话={_pick(snap, 'session.state')}")

    # ---- 身份：exec_mode 必须与 zone 一致（错配 = 看错区/算错账）
    exec_mode = snap.get("exec_mode")
    if exec_mode == key:
        add(PASS, label, "exec_mode", exec_mode)
    else:
        add(FAIL, label, "exec_mode", f"{exec_mode} != {key}（区域身份错配）")

    # ---- 行情源：mock 会让所有成交被 mock 闸门拦掉
    data_mode = snap.get("data_mode")
    if data_mode == "mock":
        add(FAIL, label, "data_mode",
            "mock ⇒ mock_trade_block_reason() 会阻断全部买卖（合成行情不得成交）")
    else:
        add(PASS, label, "data_mode", str(data_mode))

    # ---- 交易通道
    broker_mode = snap.get("broker_mode")
    connected = snap.get("broker_connected")
    if key == "paper":
        if broker_mode == "paper_no_trade":
            add(PASS, label, "broker_mode", "paper_no_trade（拒单桩，符合预期）")
        else:
            add(FAIL, label, "broker_mode",
                f"{broker_mode} ⇒ paper 不该出现真实通道")
    else:
        if broker_mode == "disconnected" or not connected:
            add(FAIL, label, "broker_mode",
                f"{broker_mode}/connected={connected} ⇒ 无法下单")
        else:
            add(PASS, label, "broker_mode", f"{broker_mode}（已连接）")

    # ---- 熔断
    if _pick(snap, "risk.halted"):
        add(FAIL, label, "熔断",
            f"halted=true 原因={_pick(snap, 'risk.halt_reason')}")
    else:
        add(PASS, label, "熔断", "未熔断")

    # ---- 总资产可信度（live 专用：回落 INITIAL_CASH 会伪装成 100 万）
    if key == "live":
        if _pick(snap, "risk.asset_trusted") is False:
            add(FAIL, label, "总资产口径",
                f"asset_trusted=false ⇒ 面板上的 {snap.get('total_asset')} 不是券商真值")
        else:
            add(PASS, label, "总资产口径", "可信")

    # ---- 候选池（0 只 = 信号层空转，最容易漏看）
    pool = (snap.get("dynamic_universe") or {}).get("active_pool") or []
    if len(pool) == 0:
        add(FAIL, label, "候选池", "0 只活跃 ⇒ 永远不会出信号")
    else:
        add(PASS, label, "候选池", f"{len(pool)} 只活跃")

    # ---- 漏斗（宇宙 0 说明本轮的评估根本没跑）
    funnel = snap.get("gate_funnel") or {}
    universe = funnel.get("universe_n")
    if universe == 0:
        add(WARN, label, "漏斗", "universe_n=0（本轮未评估，非交易时段正常）")
    else:
        add(PASS, label, "漏斗",
            f"{universe}→{funnel.get('trend_up_n')}→{funnel.get('momentum_pass_n')}"
            f"→{funnel.get('bias_ok_n')}→{funnel.get('score_ok_n')}")

    # ---- 账本
    ts, n, fills = _db_latest(cfg.db_path)
    if not cfg.db_path.exists():
        add(FAIL, label, "账本", f"缺少 {cfg.db_path}")
    elif n == 0:
        add(WARN, label, "账本", "还没有权益快照（新库/休市正常）")
    else:
        add(PASS, label, "账本", f"{n} 条快照，末条 {ts}，今日成交 {fills}")


def check_isolation() -> None:
    """运行时越界断言：paper 不得写过 live 的文件。

    判据：`storage/dynamic_universe.json` 的 mtime 必须**早于** paper 进程启动时刻
    （以 `logs_paper/engine.pid` 的 mtime 近似）。若晚于，说明当前这轮 paper 在跑
    之后又写过 live 的候选池 —— 那正是 2026-11-08 20:09 发生过的越界。
    """
    live_file = BASE_DIR / "storage" / "dynamic_universe.json"
    paper_pid = BASE_DIR / "logs_paper" / "engine.pid"
    if not live_file.exists() or not paper_pid.exists():
        add(WARN, "隔离", "越界检查", "缺少文件，跳过")
        return
    live_mtime = live_file.stat().st_mtime
    paper_start = paper_pid.stat().st_mtime
    if live_mtime > paper_start + 1.0:
        add(FAIL, "隔离", "越界检查",
            f"live 候选池在 paper 启动后又被写过 "
            f"({datetime.fromtimestamp(live_mtime):%H:%M:%S} > "
            f"{datetime.fromtimestamp(paper_start):%H:%M:%S}) ⇒ paper 在写 live 的文件")
    else:
        add(PASS, "隔离", "越界检查",
            f"live 候选池末次写入 {datetime.fromtimestamp(live_mtime):%m-%d %H:%M:%S}"
            f" 早于 paper 启动 {datetime.fromtimestamp(paper_start):%m-%d %H:%M:%S}")

    # paper 必须有自己的池子（否则就是又去蹭 live 的）
    paper_file = BASE_DIR / "storage_paper" / "dynamic_universe.json"
    if paper_file.exists():
        try:
            d = json.loads(paper_file.read_text(encoding="utf-8"))
            add(PASS, "隔离", "paper 候选池",
                f"独立文件 {len(d.get('active_pool') or [])} 只活跃")
        except Exception as e:                         # noqa: BLE001
            add(WARN, "隔离", "paper 候选池", f"读取失败 {type(e).__name__}")
    else:
        add(WARN, "隔离", "paper 候选池",
            "尚无独立池子（会在首次刷新后创建；失败时沿用空池 ⇒ 候选池检查会报 FAIL）")

    # 日志/库路径不得共用
    for key, other in (("live", "paper"),):
        a, b = zone_config(key), zone_config(other)
        pairs = [("DB", a.db_path, b.db_path), ("日志", a.log_dir, b.log_dir)]
        for name, pa, pb in pairs:
            if Path(pa) == Path(pb):
                add(FAIL, "隔离", f"{name}路径", f"两区共用 {pa}")
            else:
                add(PASS, "隔离", f"{name}路径", "分离")


def _pick(snap: dict, path: str, default=None):
    cur = snap
    for part in path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(part)
    return default if cur is None else cur


def main() -> int:
    ap = argparse.ArgumentParser(description="双功能区开盘自检（纯只读）")
    ap.add_argument("--quiet", action="store_true", help="只打印非 PASS 行")
    args = ap.parse_args()

    print(f"双功能区开盘自检  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 74)

    live_ok = check_port(zone_config("live").web_port, "实盘区")
    paper_ok = check_port(zone_config("paper").web_port, "观察区")
    check_port(DASH_PORT, "看板")
    check_zone("live", _snapshot(zone_config("live").web_port) if live_ok else None)
    check_zone("paper", _snapshot(zone_config("paper").web_port) if paper_ok else None)
    check_isolation()

    for level, zone, item, detail in _rows:
        if args.quiet and level == PASS:
            continue
        print(f"  {_ICON[level]} [{level}] {zone:<4} {item:<12} {detail}")

    n_fail = sum(1 for r in _rows if r[0] == FAIL)
    n_warn = sum(1 for r in _rows if r[0] == WARN)
    n_pass = sum(1 for r in _rows if r[0] == PASS)
    print("=" * 74)
    print(f"PASS {n_pass}  WARN {n_warn}  FAIL {n_fail}")
    if n_fail:
        print("\n有 FAIL 的区不可依赖，请先处理 —— 下列条目为「会导致零成交/零信号」的硬伤：")
        for level, zone, item, detail in _rows:
            if level == FAIL:
                print(f"  · {zone} {item}：{detail}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())

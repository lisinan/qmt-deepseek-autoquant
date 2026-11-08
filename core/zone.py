# -*- coding: utf-8 -*-
"""zone（live / paper 双实例）隔离配置。

## 为什么要有这个模块

`config/settings.py` 对自动化只读（ACL 保护，`open('wb')` / `os.replace` 一律
WinError 5），所以 live / paper 的差异**不能**靠改 settings 表达。这里把 zone 的
全部推导集中到一处，由 `main.py` 在 `from engine.event_engine import EventEngine`
**之前**调用。

顺序为什么关键：`engine/event_engine.py:38` 是模块级
`from config.settings import ... LOG_DIR`，而 `STOP_SENTINEL`（`event_engine.py:1419`）
是**类属性、导入时求值**。晚一步改写 `config.settings.LOG_DIR` 就再也传不进去了。

## 两个区的定位（2026-11-08 OWNER 定）

- **live**：真实资金，唯一交易区。`config/settings.py` 原值一律不动。
- **paper**：AI 产业链的**热点/行业观察台**，起始资金 100 万（`INITIAL_CASH`）。
  它不受 live 的「一手 100 股现金门槛」约束，所以能把链条上买不起的标的也跑起来
  —— 这正是它作为观察台的价值。**代价：它验证的是信号层/逻辑层，不是 live 的
  资金可执行性，绝对收益与持仓数量都不可与 live 横比。**

## live 分支的硬承诺

`zone_config("live")` 的每一项都必须与既有常量**逐项相等**，`apply_zone("live")`
除了 `QMT_ZONE` 之外**不写任何环境变量**。`tests/test_zone_isolation.py` 用断言钉住
这一点，防止日后有人「顺手」改了 live 的路径。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

VALID_ZONES = ("live", "paper")

# 与 config/settings.py 的对应值必须一致（见 zone_config 的 live 分支）
_LIVE_BRIDGE_ROOT = r"C:\pazq_qmt_bridge"
_LIVE_WEB_PORT = 5000


@dataclass(frozen=True)
class ZoneConfig:
    """一个 zone 的全部隔离路径与端口。"""

    zone: str
    db_path: Path
    log_dir: Path
    notices_log: Path
    web_port: int
    bridge_root: Path


def argv_zone(argv: list) -> str:
    """从原始 argv 抠 ``--zone``。

    为什么不用 argparse：`main()` 里 `_setup_logging()`（main.py:499）跑在
    `parse_args()`（:541）之前，而那时 LOG_DIR 必须已经定型。所以 zone 必须在
    argparse 之前从 argv 里手工取。
    """
    args = list(argv)
    for i, a in enumerate(args):
        if a == "--zone" and i + 1 < len(args):
            return str(args[i + 1]).strip().lower()
        if a.startswith("--zone="):
            return a.split("=", 1)[1].strip().lower()
    return ""


def resolve_zone(argv: list | None = None) -> str:
    """确定本进程的 zone：``--zone`` 优先，其次 ``QMT_ZONE``，缺省 live。"""
    if argv is None:
        import sys

        argv = sys.argv[1:]
    raw = (argv_zone(argv) or os.environ.get("QMT_ZONE") or "live")
    zone = str(raw).strip().lower()
    if zone not in VALID_ZONES:
        raise SystemExit(
            f"[zone] 非法 zone={zone!r}；只接受 {VALID_ZONES}。"
            f"（--zone live|paper，或环境变量 QMT_ZONE）"
        )
    return zone


def zone_config(zone: str, base_dir: Path | None = None) -> ZoneConfig:
    """zone → 全部隔离路径。

    目录刻意用**同级兄弟**（`logs_paper/`）而不是子目录（`logs/paper/`）：
    `engine/event_engine.py:630` 曾用 ``Path(LOG_DIR).parent / "storage"`` 反推
    storage，兄弟命名能让 ``LOG_DIR.parent`` 恒为 ``BASE_DIR``，少一处隐式耦合。
    （那行已改为从 DB 路径推导，见 `_apply_pending_reset`。）
    """
    base = Path(base_dir) if base_dir else Path(__file__).resolve().parents[1]
    zone = str(zone).strip().lower()

    if zone == "paper":
        storage = base / "storage_paper"
        logs = base / "logs_paper"
        return ZoneConfig(
            zone="paper",
            db_path=storage / "qmt.db",
            log_dir=logs,
            notices_log=logs / "notices.log",
            web_port=_LIVE_WEB_PORT + 1,
            # 纵深防御：paper 的 `_NoTradeBroker` 已从构造期拒单，这个目录只是
            # 万一桩被绕过时的「无人监听的投信桶」。
            # ⚠ 注意：引擎侧读的 bridge_root 来自 trading_config.json
            #   （core/broker_bridge.py:94 → core/broker.py:31），**不读这个环境变量**
            #   （全仓唯一读取点在 qmt_bridge/qmtide_bridge.py:77，QMT 进程内部）。
            bridge_root=storage / "bridge",
        )

    return ZoneConfig(
        zone="live",
        db_path=base / "storage" / "qmt.db",
        log_dir=base / "logs",
        notices_log=base / "logs" / "notices.log",
        web_port=_LIVE_WEB_PORT,
        bridge_root=Path(_LIVE_BRIDGE_ROOT),
    )


def zone_storage_dir(base_dir: Path | None = None,
                     zone: str | None = None) -> Path:
    """当前 zone 的**落盘根目录**：live → ``storage/``，paper → ``storage_paper/``。

    为什么需要它（2026-11-08 实证的越界）：``engine_state`` 早已按 DB 分库隔离，
    但**共享缓存文件**此前硬编码 ``BASE_DIR/"storage"``，与 zone 无关 —— paper
    会写 live 的文件。实测证据：``storage/dynamic_universe.json`` 的 mtime 是
    ``2026-11-08 20:09:43.394506``，与 paper 日志第 27 行「刷新完成 20:09:43,394」
    毫秒级吻合 ⇒ **paper 把 live 的候选池文件覆写了**。

    为什么这是资金安全级：候选池（``active_pool``）直接决定 live 评估/买入哪些票。
    paper 用 tushare、live 用 xtdata，两边池子本可以不同；只要一次 paper 刷新失败
    写入半截 JSON，live 重启读到的就是空池或残缺池。

    live 返回值与改造前**逐位一致**（``BASE_DIR/storage``），由
    ``tests/test_zone_shared_paths.py`` 钉住。
    """
    return zone_config(zone or current_zone(), base_dir).db_path.parent


def zone_data_cache_dir(base_dir: Path | None = None,
                        zone: str | None = None) -> Path:
    """tushare 落盘缓存目录：live 保持 ``data/cache``，paper 用 ``storage_paper/data_cache``。

    live 分支必须与改造前逐位一致（``BASE_DIR/data/cache``）—— 那里的
    ``northbound_hsgt.json`` 是**单文件**，两个进程同写会读到半截 JSON
    （``northbound_cache.py:31`` 的 ``F`` 就是从它派生的）。
    """
    base = Path(base_dir) if base_dir else Path(__file__).resolve().parents[1]
    if (zone or current_zone()) == "paper":
        return zone_storage_dir(base, "paper") / "data_cache"
    return base / "data" / "cache"


def apply_zone(zone: str, base_dir: Path | None = None) -> ZoneConfig:
    """写环境变量，把 zone 传递给下游惰求的读点。

    下游读点（都已在源码处验证为「调用时解析」，不是 import 时快照）：
      - ``QMT_DB``          → `storage/db.py:151 default_db_path()`
      - ``QMT_NOTICES_LOG`` → `core/notices.py:50 notices_log_path()`
      - ``QMT_BRIDGE_ROOT`` → `qmt_bridge/qmtide_bridge.py:77`（QMT 侧，非引擎侧）

    ★ live 分支只写 ``QMT_ZONE``，其余一律不碰 —— 这是「改造前后 live 逐位不变」
      的兑现方式，也是 `tests/test_zone_isolation.py` 里那条断言的依据。
    """
    cfg = zone_config(zone, base_dir)
    os.environ["QMT_ZONE"] = cfg.zone

    if cfg.zone == "paper":
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        # setdefault 而非直接赋值：单测/一次性脚本若已把 QMT_DB 指到 tmp，
        # 必须让它赢（`tests/run_all.py` 与 test_no_production_db_pollution 依赖这一点）。
        os.environ.setdefault("QMT_DB", str(cfg.db_path))
        os.environ.setdefault("QMT_NOTICES_LOG", str(cfg.notices_log))
        os.environ.setdefault("QMT_BRIDGE_ROOT", str(cfg.bridge_root))
    return cfg


def current_zone() -> str:
    """当前进程所属 zone（读环境变量，缺省 live）。"""
    return (os.environ.get("QMT_ZONE") or "live").strip().lower()

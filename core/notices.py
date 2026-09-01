# -*- coding: utf-8 -*-
"""
统一「系统提示」通道

把关键事件——引擎启动结论、市场分析结论、交易成交、风控熔断/恢复、运行异常——
从散落的 DEBUG/INFO 日志中隔离出来，统一用固定前缀 ``【系统提示】`` 输出到
日志文件，并保留一份内存环形缓冲，供 Web 仪表板的 SSE 实时推送展示。

为什么需要：
  原实现里，交易、风控、分析结论都混在 quant_system.log 的普通 INFO 行里，
  没有固定标识，人工巡检或前端展示时很难一眼区分「系统级关键提示」和「逐轮
  调试噪声」。本模块提供一个语义清晰的通道，让三类最关键的信息（分析结论 /
  交易信息 / 风控异常）在任何客户端都醒目可读。

设计约束：
  - 零项目内依赖（只依赖标准库），避免循环 import。
  - 环形缓冲有上限，不随运行时间无界增长。
  - 即便前端未启动，日志文件也完整保留所有系统提示。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

# 内存环形缓冲：最多保留最近 300 条系统提示（足够覆盖一个交易日的巡查）。
_MAX_NOTICES = 300
_NOTICES: "deque" = deque(maxlen=_MAX_NOTICES)

# 独立的 logger 名，便于在日志配置里单独控制级别 / 写到专用文件。
_LOGGER = logging.getLogger("SYSTEM")

# 耐久落盘：每条系统提示额外以 JSONL 追加到 logs/notices.log，
# 重启不丢、可按日检索（供盘后复盘工具读取）。内存环形缓冲仅作 Web 实时展示用。
#
# 【修正 2026-09-02】路径改为**惰求解析 + 环境变量可覆盖**。
# 为什么：原实现在 import 时把路径硬绑到生产 logs/notices.log，于是**单测也会
# 往生产日志里写入测试桩**（实测生产 notices.log 里掘出大量
# 「提交买入委托 300308.SZ ×2000 @100.000 理由=test」与 18:19 的「触发熔断」）。
# 这些脏数据又反过来逆向驱动了 review_daily._in_trading_window() 这个补丁
# —— 在给症状打补丁，而不是治病。现在测试只需设 QMT_NOTICES_LOG 到 tmp 即可隔离。
_DEFAULT_NOTICES_LOG = Path(__file__).resolve().parents[1] / "logs" / "notices.log"
_NOTICES_LOG_LOCK = threading.Lock()


def notices_log_path() -> Path:
    """当前系统提示耐久日志路径。``QMT_NOTICES_LOG`` 环境变量优先。

    每次调用重新读环境变量（而不是 import 时快照），使测试/工具可以在
    运行中重定向到临时目录，不会污染生产复盘数据。频率极低，开销可忽。
    """
    override = (os.environ.get("QMT_NOTICES_LOG") or "").strip()
    return Path(override) if override else _DEFAULT_NOTICES_LOG

# level 名 -> logger 方法（SUCCESS/WARNING 等语义级映射到标准 level）
_LEVEL_FN = {
    "INFO": _LOGGER.info,
    "SUCCESS": _LOGGER.info,
    "SYSTEM": _LOGGER.info,
    "WARNING": _LOGGER.warning,
    "ERROR": _LOGGER.error,
}


def system_notice(level: str, tag: str, msg: str) -> None:
    """发出一条系统提示。

    :param level: INFO / SUCCESS / SYSTEM / WARNING / ERROR
    :param tag:   事件分类，如 系统 / 分析结论 / 交易 / 风控 / ENGINE
    :param msg:   人类可读的具体说明
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rec = {"ts": ts, "level": level, "tag": tag, "msg": msg}
    _NOTICES.append(rec)
    fn = _LEVEL_FN.get(level, _LOGGER.info)
    fn("[系统提示][%s] %s", tag, msg)
    # 耐久落盘（JSONL，按日可查），失败不影响主流程
    try:
        path = notices_log_path()
        with _NOTICES_LOG_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def notices_on_date(target_date: str) -> list:
    """从耐久日志读取某日的系统提示（target_date 形如 '2026-08-25'）。

    优先读 logs/notices.log；若该文件不存在则回退解析 quant_system.log 中
    带 ``【系统提示】`` 前缀的行。返回按时间升序的 dict 列表。
    """
    out = []
    notices_log = notices_log_path()
    # 主源：logs/notices.log（耐久 JSONL）
    if notices_log.exists():
        try:
            for line in notices_log.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("ts", "").startswith(target_date):
                    out.append(rec)
        except Exception:
            pass
    # 回退：当 notices.log 当日无记录时，解析 quant_system.log 中带【系统提示】的行
    if not out:
        log_path = notices_log.parent / "quant_system.log"
        if log_path.exists():
            try:
                for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if "【系统提示】" not in line:
                        continue
                    # 形如: 2026-08-25 09:15:01,123 - SYSTEM - [系统提示][交易] ...
                    if line.startswith(target_date):
                        out.append({"ts": target_date, "level": "INFO",
                                    "tag": "系统", "msg": line})
            except Exception:
                pass
    return out


def purge_notices_matching(substr: str, dry_run: bool = False) -> int:
    """删除耐久日志中包含 ``substr`` 的行，返回命中行数。

    用途：清理单测桩污染（如 ``理由=test`` 的假成交、非交易时段的假熔断）。
    这些脏数据会直接抬高盘后复盘的熔断计数与交易流水。
    写入前先备份为 ``<name>.bak``（只在非 dry_run 且有命中时）。
    """
    path = notices_log_path()
    if not path.exists():
        return 0
    with _NOTICES_LOG_LOCK:
        try:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        except Exception:
            return 0
        keep = [ln for ln in lines if substr not in ln]
        hit = len(lines) - len(keep)
        if hit and not dry_run:
            try:
                path.with_suffix(path.suffix + ".bak").write_text(
                    "".join(lines), encoding="utf-8")
                path.write_text("".join(keep), encoding="utf-8")
            except Exception:
                return 0
        return hit


def latest_notices(limit: int = 50) -> list:
    """返回最近的系统提示（按时间升序），供 Web 端点调用。"""
    items = list(_NOTICES)
    if limit and 0 < limit < len(items):
        items = items[-limit:]
    return items

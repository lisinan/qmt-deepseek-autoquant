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
_NOTICES_LOG = Path(__file__).resolve().parents[1] / "logs" / "notices.log"
_NOTICES_LOG_LOCK = threading.Lock()

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
        with _NOTICES_LOG_LOCK:
            _NOTICES_LOG.parent.mkdir(parents=True, exist_ok=True)
            with _NOTICES_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def notices_on_date(target_date: str) -> list:
    """从耐久日志读取某日的系统提示（target_date 形如 '2026-08-25'）。

    优先读 logs/notices.log；若该文件不存在则回退解析 quant_system.log 中
    带 ``【系统提示】`` 前缀的行。返回按时间升序的 dict 列表。
    """
    out = []
    # 主源：logs/notices.log（耐久 JSONL）
    if _NOTICES_LOG.exists():
        try:
            for line in _NOTICES_LOG.read_text(encoding="utf-8").splitlines():
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
        log_path = _NOTICES_LOG.parent / "quant_system.log"
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


def latest_notices(limit: int = 50) -> list:
    """返回最近的系统提示（按时间升序），供 Web 端点调用。"""
    items = list(_NOTICES)
    if limit and 0 < limit < len(items):
        items = items[-limit:]
    return items

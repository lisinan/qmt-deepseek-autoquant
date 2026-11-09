# -*- coding: utf-8 -*-
"""桥心跳体检（大 QMT + bridge 方案专用，2026-11-09 建立）。

为什么需要它：
自启脚本原先用 `XtMiniQmt.exe` 判定「QMT 是否就绪」。2026-11-08 起改用
**大 QMT（XtItClient.exe）+ 文件桥**，`XtMiniQmt.exe` 这个进程名在本机
**永远不会出现** ⇒ 每次开机都误报一条「25s 内未就绪，可能停在登录界面」
的 WARN，而那正是最容易被误读成人不在、机器坏了的那类假告警。

真正回答「QMT 能不能下单」的问题是：**桥的心跳还新不新鲜**。
桥（qmtide_bridge.py）跑在 QMT 进程内，每 500ms 刷一次 heartbeat.json
（含 ts / pid / rev）。这个文件比任何进程名都更贴近事实：
  - QMT 没启动       → 文件不存在或 ts 很旧
  - QMT 起了但策略没跑 → ts 很旧（策略没运行 → init 没跑 → 没人刷心跳）
  - 一切正常         → age < 2s

用法：
    python scripts/bridge_health.py            # 默认阈值 60s
    python scripts/bridge_health.py --max-age 120
    python scripts/bridge_health.py --json

退出码：0=新鲜 1=过期/不可读 2=参数错误
只读，不写任何文件，不 import 项目模块。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

DEFAULT_HB = r"C:\pazq_qmt_bridge\heartbeat.json"


def hb_path() -> str:
    return os.environ.get("QMT_BRIDGE_HB") or DEFAULT_HB


def check(max_age: float = 60.0) -> dict:
    p = hb_path()
    out = {"path": p, "ok": False, "reason": "", "age": None,
           "rev": None, "pid": None}
    if not os.path.isfile(p):
        out["reason"] = "heartbeat.json 不存在（QMT 未启动，或桥从未运行）"
        return out
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception as exc:
        out["reason"] = "无法解析: %s" % exc
        return out
    if not isinstance(d, dict):
        out["reason"] = "内容不是对象"
        return out
    try:
        ts = float(d.get("ts") or 0)
    except Exception:
        ts = 0.0
    if ts <= 0:
        out["reason"] = "ts 缺失或非法"
        return out
    age = time.time() - ts
    out["age"] = age
    out["rev"] = d.get("rev")
    out["pid"] = d.get("pid")
    if age > max_age:
        out["reason"] = "心跳已 %.0fs 未更新（阈值 %.0fs）⇒ QMT 侧策略未运行或已退出" % (
            age, max_age)
        return out
    out["ok"] = True
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="桥心跳体检")
    ap.add_argument("--max-age", type=float, default=60.0,
                    help="视为新鲜的秒数上限（默认 60，开机阶段用 60；盘中可收紧到 15）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    r = check(args.max_age)
    if args.json:
        print(json.dumps(r, ensure_ascii=False))
    elif r["ok"]:
        print("[bridge] OK rev=%s pid=%s age=%.1fs" % (r["rev"], r["pid"], r["age"]))
    else:
        print("[bridge] NOT READY: %s" % r["reason"])
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

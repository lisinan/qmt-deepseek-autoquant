# -*- coding: utf-8 -*-
"""落地验收：2026-11-10 两批补丁重启后是否真的生效。

## 为什么需要独立验收脚本

本仓已多次踩「改了文件但没重启 ⇒ 跑的还是旧代码」（`deploy ≠ 生效`）与
「面板显示已恢复、决策仍用假数据」。所以落地必须由**机器**给出证据，
不能由人看日志说「应该是生效了」。

七项验收：
  A1 运行中引擎的进程启动时刻 vs 源码 mtime（逐个文件）
  A2 engine_state.peak_asset 已修正为真实历史峰值
  A3 熔断状态自洽（halted / consec_loss / daily_trade_count 可读）
  A4 桥心跳新鲜 + CODE_REV
  A5 宽度闸门日志出现「口径=universe」
  A6 源码里两批补丁的关键锚点仍在位（防被回滚却没人知道）
  A7 全量测试计数不低于落地前基线

用法：
    python scripts/verify_landing_20261110.py
    python scripts/verify_landing_20261110.py --baseline tests_final.txt
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "storage" / "qmt.db"
BRIDGE_DIR = Path("C:/pazq_qmt_bridge")
EXPECTED_PEAK = 67453.62

WATCHED = [
    "engine/event_engine.py",
    "risk/manager.py",
    "core/owner_order.py",
    "config/settings.py",
    "strategy/_param_audit.py",
]

ANCHORS = [
    # (文件, 片段, 说明)
    ("engine/event_engine.py", "WHERE mode=? GROUP BY substr(ts, 1, 10)",
     "V1 peak 兜底按 mode 过滤"),
    ("risk/manager.py", "_dd_rearm_floor", "V2 峰值单调 + 重新武装阈值"),
    ("risk/manager.py", "_was_dd_halt", "V2c 熔断恢复不重基峰值"),
    ("engine/event_engine.py", "def _register_unknown_order", "V3 未知态占位"),
    ("engine/event_engine.py", "def _expire_unknown_orders", "V3 墙钟自过期扫描"),
    ("engine/event_engine.py", "self._expire_unknown_orders()", "V5 先于 get_trades 扫描"),
    ("engine/event_engine.py", "pool_all = set(candidate_codes)", "B1 全量池快照"),
    ("engine/event_engine.py", "_breadth_n < _min_breadth", "B3 判据换口径"),
    ("config/settings.py", '"breadth_gate_basis": "universe"', "B4 回滚开关"),
    ("strategy/_param_audit.py", '"breadth_gate_basis": "live-only', "B5 契约登记"),
    ("tests/run_all.py", "tests.test_20261110_landing_guards", "B6 守卫入册"),
    # OWNER 2026-11-10 自己落的那条（不在本批补丁内，但必须一起去验）
    ("core/owner_order.py", "lots=qty", "OWNER 自己的 SELL lots 修复在位"),
]


def _ps_json(cmd: str):
    # ★ 两个坑（2026-11-10 评审发现，实测复现）：
    #   ① 不加 encoding 时 subprocess 用 locale（cp1252）解码 PowerShell 的
    #      中文输出 ⇒ UnicodeDecodeError / 解析出空列表 ⇒ A1 恒 FAIL；
    #   ② ConvertTo-Json 会把 DateTime 序列化成 `\/Date(1794...)\/`（斜杠被转义），
    #      用 startswith("/Date(") 判断必然失败。
    #   这里显式设 UTF-8，并在 PowerShell 侧把时间投影成 ISO 字符串（不再出现 /Date)。
    full = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + cmd)
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", full],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace")
    except Exception as e:                           # noqa: BLE001
        return None, str(e)
    if out.returncode != 0:
        return None, (out.stderr or "").strip()[:300]
    raw = (out.stdout or "").strip().lstrip("\ufeff")
    if not raw:
        return None, "empty"
    try:
        return json.loads(raw), ""
    except Exception as e:                           # noqa: BLE001
        return None, "json: %s / %s" % (e, raw[:200])


def _engine_proc():
    """找 live 引擎进程（main.py --web 且不带 --zone paper），返回 (启动时刻, 说明)。"""
    data, err = _ps_json(
        "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
        "Select-Object ProcessId,"
        "@{n='StartTime';e={$_.CreationDate.ToString('yyyy-MM-ddTHH:mm:ss')}},"
        "CommandLine | ConvertTo-Json -Compress")
    if data is None:
        return None, "取进程列表失败: %s" % err
    if isinstance(data, dict):
        data = [data]
    for p in data:
        cl = str(p.get("CommandLine") or "")
        if "main.py" in cl and "--web" in cl and "--zone paper" not in cl:
            st = str(p.get("StartTime") or "").replace("\\/", "/")
            try:
                return datetime.strptime(st[:19], "%Y-%m-%dT%H:%M:%S"), \
                    "pid=%s" % p.get("ProcessId")
            except Exception:                        # noqa: BLE001
                return None, "StartTime 解析失败: %r" % (st,)
    return None, "未找到 live 引擎进程（main.py --web）"


def _db_rows():
    con = sqlite3.connect("file:%s?mode=ro" % str(DB).replace("\\", "/"), uri=True)
    try:
        cur = con.cursor()
        cur.execute("SELECT ts, peak_asset, peak_equity, daily_trade_count, "
                    "consec_loss, risk_state FROM engine_state ORDER BY id DESC LIMIT 1")
        return cur.fetchone()
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", default="", help="落地前全量测试输出文件，用于 A7 对比")
    args = ap.parse_args()

    results = []          # (编号, 名称, 状态, 明细)

    # ---------------- A1 源码 mtime vs 进程启动 ----------------
    started, note = _engine_proc()
    if started is None:
        results.append(("A1", "源码 mtime < 进程启动", "FAIL", note))
    else:
        stale = []
        for rel in WATCHED:
            p = ROOT / rel
            if not p.exists():
                stale.append("%s 缺失" % rel)
                continue
            m = datetime.fromtimestamp(p.stat().st_mtime)
            if m > started:
                stale.append("%s mtime=%s > 启动=%s"
                             % (rel, m.strftime("%m-%d %H:%M:%S"),
                                started.strftime("%m-%d %H:%M:%S")))
        detail = "启动=%s（%s）；%d 个文件全部早于启动" % (
            started.strftime("%m-%d %H:%M:%S"), note, len(WATCHED))
        if stale:
            results.append(("A1", "源码 mtime < 进程启动", "FAIL",
                            "以下文件比进程新 ⇒ 跑的还是旧代码: " + "; ".join(stale)))
        else:
            results.append(("A1", "源码 mtime < 进程启动", "PASS", detail))

    # ---------------- A2/A3 账本 ----------------
    row = _db_rows()
    if not row:
        results.append(("A2", "peak_asset 已修正", "FAIL", "engine_state 无数据"))
        results.append(("A3", "熔断状态可读", "FAIL", "engine_state 无数据"))
    else:
        ts, peak, peak_eq, dtc, cl, rstate = row
        d = abs(float(peak or 0.0) - EXPECTED_PEAK)
        results.append(("A2", "peak_asset 已修正", "PASS" if d < 0.02 else "FAIL",
                        "peak_asset=%.2f（期望 %.2f，差 %.2f）；ts=%s"
                        % (float(peak or 0), EXPECTED_PEAK, d, ts)))
        results.append(("A3", "熔断状态可读", "PASS",
                        "halted/dtc/consec_loss/peak_equity → %s / %s / %s / %.2f"
                        % (str(rstate)[:160], dtc, cl, float(peak_eq or 0))))

    # ---------------- A4 桥心跳 ----------------
    hb = BRIDGE_DIR / "heartbeat.json"
    if not hb.exists():
        results.append(("A4", "桥心跳新鲜", "FAIL", "找不到 %s" % hb))
    else:
        age = time.time() - hb.stat().st_mtime
        try:
            rev = json.loads(hb.read_text(encoding="utf-8", errors="replace")).get("rev")
        except Exception:                            # noqa: BLE001
            rev = "?"
        results.append(("A4", "桥心跳新鲜", "PASS" if age < 30 else "WARN",
                        "age=%.1fs rev=%s" % (age, rev)))

    # ---------------- A5 宽度口径日志 ----------------
    pat = "口径=universe"
    hit_after_start = 0
    scanned = 0
    for logp in [ROOT / "logs" / "quant_system.log"]:
        if not logp.exists():
            continue
        with open(logp, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                scanned += 1
                if pat not in line:
                    continue
                if started is not None:
                    try:
                        lt = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
                    except Exception:                # noqa: BLE001
                        continue
                    if lt < started:
                        continue
                hit_after_start += 1
    if hit_after_start:
        results.append(("A5", "宽度日志出现口径=universe", "PASS",
                        "重启后命中 %d 行（扫描 %d 行）" % (hit_after_start, scanned)))
    else:
        results.append(("A5", "宽度日志出现口径=universe", "WARN",
                        "重启后尚未命中（可能今天没有触发宽度闸门；"
                        "触发时会打印「口径=universe」或写入 blocked_by）"))

    # ---------------- A6 补丁锚点在位 ----------------
    missing = []
    for rel, frag, desc in ANCHORS:
        p = ROOT / rel
        if not p.exists():
            missing.append("%s 缺失" % rel)
            continue
        txt = p.read_text(encoding="utf-8")
        if frag not in txt:
            missing.append("%s ← %s" % (desc, rel))
    results.append(("A6", "补丁锚点在位（%d 条）" % len(ANCHORS),
                    "PASS" if not missing else "FAIL",
                    "全部在位" if not missing else "缺失: " + "; ".join(missing)))

    # ---------------- A7 全量测试计数 ----------------
    if args.baseline and os.path.exists(args.baseline):
        import re

        def _parse(p):
            for line in open(p, "r", encoding="utf-8", errors="replace"):
                if not line.startswith("PASSED:"):
                    continue
                m = re.search(r"PASSED:\s*(\d+)\s+FAILED:\s*(\d+)\s+TOTAL:\s*(\d+)", line)
                if m:
                    return {"PASSED:": int(m.group(1)),
                            "FAILED:": int(m.group(2)),
                            "TOTAL:": int(m.group(3))}
            return {}

        base = _parse(args.baseline)
        cur_f = sorted(glob.glob(str(ROOT / "logs" / "test_landing*.txt")))
        detail = "基线 %s" % base
        status = "WARN"
        if base and cur_f:
            cur = _parse(cur_f[-1])
            ok = (cur.get("FAILED:", 99) <= base.get("FAILED:", 0)
                  and cur.get("TOTAL:", 0) >= base.get("TOTAL:", 0))
            status = "PASS" if ok else "FAIL"
            detail += " → 本次 %s（多 %d 例）" % (
                cur, cur.get("TOTAL:", 0) - base.get("TOTAL:", 0))
        results.append(("A7", "全量测试计数不低于基线", status, detail))
    else:
        results.append(("A7", "全量测试计数不低于基线", "WARN", "未提供 --baseline"))

    # ---------------- 汇总 ----------------
    print("=" * 78)
    print("落地验收  %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 78)
    bad = 0
    for code, name, status, detail in results:
        if status == "FAIL":
            bad += 1
        print("[%s] %-3s %-30s %s" % (status, code, name, detail))
    print("-" * 78)
    print("结论: %s（FAIL %d 项）" % ("PASS" if bad == 0 else "未通过", bad))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

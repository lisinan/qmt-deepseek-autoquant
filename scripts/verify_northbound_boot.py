#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""北向资金轴重启生效验证（一次性检查）。

判定引擎在重启后是否真正加载北向序列并启用协同闸门。
四层证据：
  1) config 配置是否启用（northbound_mode=gate, nb_lookback=20）
  2) engine 代码是否仍含北向预热 + _market_gate 协同闸门（防回退）
  3) 北向数据能否加载、覆盖率与末端日期
  4) 离线复刻闸门逻辑，确认在历史数据上确实能识别净流出段
  5) 引擎运行日志是否检出"北向序列已加载"（进程已实际预热）

输出 JSON -> logs/verify_northbound_boot.json；标准输出人类可读结论。
verdict: PASS(全链路生效) | NEED-RESTART(配置/代码/数据就绪但进程未见加载) | FAIL(配置或代码未就绪)
"""
import sys, os, re, json, datetime as dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
results = {"checks": [], "verdict": None,
           "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def _mark(ok):
    return "OK " if ok is True else ("?? " if ok is None else "XX ")


def add(name, ok, detail):
    results["checks"].append({"name": name, "ok": (ok is True), "detail": str(detail)})
    print(f"[{_mark(ok)}] {name}: {detail}")


print("===== 北向轴重启生效验证 =====")

# ---- 1) 配置检查 ----
import config.settings as S
nb_mode = S.STRATEGY_PARAMS.get("northbound_mode", "off")
nb_lb = int(S.STRATEGY_PARAMS.get("nb_lookback", 20))
if nb_mode == "gate" and nb_lb == 20:
    add("config.northbound_enabled", True, f"northbound_mode={nb_mode}, nb_lookback={nb_lb}")
else:
    add("config.northbound_enabled", False,
        f"northbound_mode={nb_mode}(应为gate), nb_lookback={nb_lb}(应为20)")
    results["verdict"] = "FAIL: 配置未启用，北向轴未激活"

# ---- 2) 代码静态检查（落盘未被回退） ----
ee_path = os.path.join(ROOT, "engine", "event_engine.py")
src = open(ee_path, encoding="utf-8").read() if os.path.exists(ee_path) else ""
has_preload = "preload_northbound" in src
has_gate = ("北向协同闸门" in src) and ("滚动 nb_lookback 日累计净买入<0" in src)
if has_preload and has_gate:
    add("code.northbound_integrated", True, "event_engine 含北向预热 + _market_gate 协同闸门")
else:
    add("code.northbound_integrated", False,
        f"preload={has_preload}, gate={has_gate}（落盘可能被回退）")

# ---- 3) 数据可加载 + 覆盖 ----
try:
    from data.northbound_cache import get_northbound
    nb = get_northbound("20221201", "20260825")
    vals = list(nb.values())
    cov = sum(1 for v in vals if v is not None)
    end = max(nb.keys()) if nb else "NA"
    neg = sum(1 for v in vals if v is not None and v < 0)
    add("data.northbound_loadable", True,
        f"覆盖 {cov}/{len(vals)} 交易日, 末端 {end}, 净流出日占比 {100.0*neg/max(cov,1):.1f}%")
    results["northbound_end"] = end
    results["northbound_cov"] = f"{cov}/{len(vals)}"
except Exception as e:
    add("data.northbound_loadable", False, f"加载失败: {e!r}")

# ---- 4) 离线闸门逻辑验证（复刻判定：滚动 nb_lookback 累计净买入<0 -> 拦截） ----
if "northbound_cov" in results:
    try:
        from data.northbound_cache import get_northbound as _g
        nb2 = _g("20221201", "20260825")
        dates = sorted(nb2.keys())
        vv = [nb2[d] for d in dates]
        lb = nb_lb
        block = sum(1 for i in range(lb, len(dates)) if sum(vv[i - lb:i]) < 0)
        add("logic.gate_triggers", True,
            f"按 lb={lb} 滚动累计净买入<0 会拦截 {block} 个交易日"
            f"（占总 {100.0*block/len(dates):.1f}%）")
        results["gate_block_days"] = block
    except Exception as e:
        add("logic.gate_triggers", None, f"逻辑验证跳过: {e!r}")

# ---- 5) 进程实际加载证据（读最新引擎日志） ----
log_path = os.path.join(ROOT, "logs", "quant_system.log")
if os.path.exists(log_path):
    try:
        with open(log_path, encoding="utf-8", errors="ignore") as f:
            tail = f.read()[-200000:]
        m = re.findall(r"北向序列已加载:\s*(\d+)\s*日", tail)
        if m:
            results["engine_loaded_days"] = int(m[-1])
            add("runtime.engine_loaded", True,
                f"日志检出'北向序列已加载: {m[-1]} 日'（进程已实际预热北向）")
        else:
            add("runtime.engine_loaded", None,
                "日志未见'北向序列已加载'（可能尚未重启 / 日志已轮转）")
    except Exception as e:
        add("runtime.engine_loaded", None, f"读日志失败: {e!r}")
else:
    add("runtime.engine_loaded", None, "无 quant_system.log（引擎从未运行）")

# ---- 总判定 ----
if results["verdict"] is None:
    hard = ("config.northbound_enabled", "code.northbound_integrated", "data.northbound_loadable")
    if all(c["ok"] for c in results["checks"] if c["name"] in hard):
        loaded = next((c for c in results["checks"] if c["name"] == "runtime.engine_loaded"), None)
        if loaded and loaded["ok"]:
            results["verdict"] = "PASS: 北向轴已配置+代码+数据+进程全链路生效"
        else:
            results["verdict"] = "NEED-RESTART: 配置/代码/数据就绪，但引擎日志未见北向加载——请重启引擎"
    else:
        results["verdict"] = "FAIL: 配置或代码未就绪（见上）"

out = os.path.join(ROOT, "logs", "verify_northbound_boot.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

print(f"\n判定: {results['verdict']}")
print(f"JSON -> {out}")

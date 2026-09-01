# -*- coding: utf-8 -*-
"""诊断探针：验证 live 路径两个疑似缺陷
1) tick.volume 是否为「当日累计量」（若是，则 _aggregate_bar 的 += 会把
   bar.volume 变成累计量的重复叠加 → volume_surge 因子失真）。
2) 非交易时段 bar 聚合会不会用「平价 tick」污染 120 根 bar 缓冲区
   （→ 开盘瞬间 8 因子里的分钟级指标全部退化）。
用法：conda env qmt 下执行。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import STOCK_CODES, STRATEGY_PARAMS  # noqa: E402
from core.qmt_client import qmt_client  # noqa: E402


def probe_volume_semantics(codes, rounds=3, gap=3.0):
    """连续采样，观察 volume 字段是否单调累计。"""
    series = {c: [] for c in codes}
    for _ in range(rounds):
        raw = qmt_client.get_ticks(codes)
        for c in codes:
            r = raw.get(c) or {}
            series[c].append({
                "price": r.get("lastPrice"),
                "volume": r.get("volume"),
                "amount": r.get("amount"),
            })
        time.sleep(gap)
    return series


def simulate_bar_aggregation(cum_volumes):
    """复刻 engine._aggregate_bar 的 volume 处理，量化失真倍数。"""
    # 当前实现：同一分钟内每轮 b.volume += tick.volume(累计量)
    current_impl = 0
    for v in cum_volumes:
        current_impl += v
    # 正确实现：只加增量
    fixed_impl = 0
    last = None
    for v in cum_volumes:
        if last is None:
            last = v
            continue
        fixed_impl += max(0, v - last)
        last = v
    return {"current_impl_bar_volume": current_impl,
            "correct_delta_bar_volume": fixed_impl,
            "inflation_x": (round(current_impl / fixed_impl, 1)
                            if fixed_impl > 0 else None)}


def main():
    codes = list(STOCK_CODES)[:3]
    out = {
        "data_mode": qmt_client.mode,
        "bars_maxlen": STRATEGY_PARAMS["ma_long"] * 6,
        "probe_codes": codes,
    }
    series = probe_volume_semantics(codes, rounds=3, gap=2.0)
    out["tick_series"] = series
    # 判定 volume 语义
    verdict = {}
    for c, rows in series.items():
        vols = [r["volume"] or 0 for r in rows]
        verdict[c] = {
            "volumes": vols,
            "magnitude": max(vols),
            "monotonic_nondecreasing": all(
                vols[i] <= vols[i + 1] for i in range(len(vols) - 1)),
            "looks_cumulative_daily": max(vols) > 100_000,
        }
    out["volume_verdict"] = verdict

    # 用真实量级模拟一分钟 20 轮轮询（REFRESH_INTERVAL=3s）
    sample = [v for v in (verdict[codes[0]]["volumes"]) if v]
    base = sample[0] if sample else 20_000_000
    # 交易时段内一分钟里累计量缓慢增长（假设每轮 +0.05%）
    cum = [int(base * (1 + 0.0005 * i)) for i in range(20)]
    out["bar_volume_distortion_1min_20polls"] = simulate_bar_aggregation(cum)

    # 非交易时段：平价 tick 会生成多少根 flat bar
    out["offhours_flat_bar_analysis"] = {
        "close_to_open_minutes": 18 * 60 + 30,
        "bars_buffer_capacity": STRATEGY_PARAMS["ma_long"] * 6,
        "buffer_overwritten_times": round(
            (18 * 60 + 30) / (STRATEGY_PARAMS["ma_long"] * 6), 1),
        "conclusion": ("隔夜平价 tick 会把 bar 缓冲区完整覆写多次 → "
                       "次日开盘时 120 根 bar 全是同一价格"),
    }

    p = ROOT / "logs" / "probe_livedata.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                 encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

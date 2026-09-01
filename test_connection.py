# -*- coding: utf-8 -*-
"""
miniQMT 连接自检脚本

用法（在 conda env qmt 中）：
  python test_connection.py

输出：
  - QMTClient 模式（xtdata / mock）
  - 探活几只关键标的的 tick
  - 历史 K 线能否拉取
  - QMTBroker 连接状态（如果交易配置存在）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Windows 控制台 UTF-8
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import MARKET_INDEX_CODE, STOCK_CODES, UNIVERSE
from core.qmt_client import qmt_client
from core.broker import qmt_broker


def main() -> int:
    print("=" * 60)
    print("qmtIDE-deepseek miniQMT 连接自检")
    print("=" * 60)

    # 1) 行情
    print(f"\n[行情] QMTClient.mode = {qmt_client.mode}")
    test_codes = [MARKET_INDEX_CODE]
    for c in list(STOCK_CODES)[:3]:
        test_codes.append(c)
    qmt_client.subscribe(test_codes)
    ticks = qmt_client.get_ticks(test_codes)
    print(f"[行情] 拉取 {len(ticks)} / {len(test_codes)} 只标的:")
    for code, t in ticks.items():
        name = UNIVERSE.get(code, code)
        print(f"  {code} {name:<8} 价={t.get('lastPrice')}  "
              f"开={t.get('open')} 高={t.get('high')} 低={t.get('low')}  "
              f"量={t.get('volume')}")

    # 2) 历史 K 线
    print("\n[K线] 拉取 1d 历史（最近 5 根）:")
    sample = list(STOCK_CODES)[0] if STOCK_CODES else MARKET_INDEX_CODE
    bars = qmt_client.get_history(sample, period="1d", count=5)
    if bars:
        for b in bars[-5:]:
            print(f"  {sample} ts={b['ts'].isoformat()}  "
                  f"close={b['close']} vol={b['volume']}")
    else:
        print(f"  {sample}: 返回空（mock 模式可能也生成）")

    # 3) 交易
    print(f"\n[交易] QMTBroker.mode = {qmt_broker.mode}")
    # 强制重连以拿到最新状态（避免 session_id 复用导致 false negative）
    if qmt_broker.mode == "disconnected":
        ok = qmt_broker.connect(force=True)
        print(f"  强制重连: {ok}, 新 mode: {qmt_broker.mode}")
    if qmt_broker.mode == "disconnected":
        print("  ⚠ miniQMT 交易通道未连接（minibroker.exe 未运行）")
        print("  ℹ 参考 qmtIDE-kimik3/account_setup_guide.md 开启独立交易模式")
    else:
        asset = qmt_broker.get_asset("cash")
        print(f"  普通账户资产: {asset}")
        positions = qmt_broker.get_positions("cash")
        print(f"  普通账户持仓 {len(positions)} 只")
        for p in positions[:5]:
            print(f"    {p['code']} x {p['quantity']}  成本={p['avg_cost']}  市值={p['market_value']}")
        if (qmt_broker._cfg.get("accounts") or {}).get("credit"):
            asset_c = qmt_broker.get_asset("credit")
            print(f"  信用账户资产: {asset_c}")
            positions_c = qmt_broker.get_positions("credit")
            print(f"  信用账户持仓 {len(positions_c)} 只")

    # 4) 总结
    print("\n" + "=" * 60)
    if qmt_client.mode == "xtdata" and qmt_broker.mode == "xttrader":
        print("✅ 全连接成功")
        return 0
    if qmt_client.mode == "xtdata":
        print("⚠ 行情已连，交易未连（minibroker 未启）")
        return 0
    print("ℹ miniQMT 未运行，已自动降级 mock（可开发/测试）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
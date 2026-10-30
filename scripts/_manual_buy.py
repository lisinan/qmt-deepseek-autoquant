# -*- coding: utf-8 -*-
"""一次性手动买入脚本（OWNER 指令：买回 300308.SZ，价格低于 2026-10-30 卖出价 569.24）。

设计要点
--------
1. **独立会话**：引擎当前占着 session=326057。本脚本用另一个 session，
   避免「抢同一 session → connect rc=-1 → 引擎被踢下线」。
2. **先只读**：`--check` 只 connect + 查资产/持仓，不下单。
   确认连得上、且不干扰引擎后，才用 `--exec` 真正下单。
3. **限价单**：价格类型必须 `FIX_PRICE=11`（2026-10-30 刚修的坑，
   传 0 会被 miniQMT 判 `invalid price type`）。
4. **硬校验**：价格必须 < 卖出价 569.24，数量必须 100 的整数倍，
   金额不得超过可用资金 —— 任一不满足直接退出，不下单。

用法
----
  python scripts/_manual_buy.py --check
  python scripts/_manual_buy.py --exec --price 567.50 --qty 100

回滚/影响：本文件是独立工具，不被引擎 import，删掉即无副作用。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xtquant.xttrader import XtQuantTrader          # noqa: E402
from xtquant.xttype import StockAccount             # noqa: E402
from xtquant.xtconstant import STOCK_BUY, FIX_PRICE  # noqa: E402

USERDATA = r"C:\pazq_qmt\userdata_mini"
CODE = "300308.SZ"
SELL_PRICE = 569.24        # 2026-10-30 10:08:11 的卖出成交价（价格上限）


def _account_id() -> str:
    """从 trading_config.json 读资金账号（该文件已 .gitignore，**不入库**）。

    为什么不在本文件里写死：券商资金账号属于个人敏感信息，一旦硬编码进仓库
    就会随 push 永久留在 git 历史里（删掉也清不干净）。此处改为运行时读取。
    """
    import json
    cfg = Path(ROOT) / "trading_config.json"
    try:
        d = json.loads(cfg.read_text(encoding="utf-8"))
        acc = (d.get("accounts") or {}).get("cash") or ""
    except Exception as e:
        raise SystemExit(f"读取 {cfg} 失败：{e}")
    if not acc:
        raise SystemExit(f"{cfg} 里 accounts.cash 为空，无法确定资金账号")
    return str(acc)


def _round_to_tick(code: str, price: float) -> float:
    """股票 0.01 / ETF 0.001 —— 与 core/broker.py::_round_to_tick 同规则。"""
    head = code.split(".")[0]
    is_fund = head.startswith(("51", "50", "58", "15", "16", "18"))
    return round(float(price), 3 if is_fund else 2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只读：连接 + 查资产/持仓")
    ap.add_argument("--exec", action="store_true", help="真正下单")
    ap.add_argument("--price", type=float, default=0.0)
    ap.add_argument("--qty", type=int, default=100)
    ap.add_argument("--session", type=int, default=900001,
                    help="独立会话号，必须避开引擎的 326057")
    a = ap.parse_args()

    if not (a.check or a.exec):
        print("必须指定 --check 或 --exec")
        return 2

    t = XtQuantTrader(USERDATA, a.session)
    t.start()
    rc = t.connect()
    print(f"connect rc={rc} (session={a.session})")
    if rc != 0:
        print("连接失败：可能会话冲突或 miniQMT 未就绪")
        return 3

    acc = StockAccount(_account_id(), "STOCK")
    sub = t.subscribe(acc)
    print(f"subscribe rc={sub}")
    time.sleep(0.6)

    asset = t.query_stock_asset(acc)
    if asset is None:
        print("查资产失败")
        return 4
    print(f"资产：总 {asset.total_asset:,.2f} | 可用 {asset.cash:,.2f} | "
          f"市值 {asset.market_value:,.2f}")

    poss = t.query_stock_positions(acc) or []
    print(f"持仓 {len(poss)} 只：")
    for p in poss:
        print(f"  {p.stock_code} 数量={p.volume} 可用={p.can_use_volume} "
              f"成本={p.open_price} 市值={p.market_value}")

    if a.check:
        print("\n[check] 只读完成，未下单。")
        return 0

    # ---- 硬校验（任一不满足即拒，绝不下单）----
    if a.price <= 0:
        print("拒绝：--price 必填且 > 0")
        return 5
    px = _round_to_tick(CODE, a.price)
    if px >= SELL_PRICE:
        print(f"拒绝：限价 {px} 不低于卖出价 {SELL_PRICE}（OWNER 指令要求低于卖出价）")
        return 5
    if a.qty <= 0 or a.qty % 100 != 0:
        print(f"拒绝：数量 {a.qty} 必须是 100 的整数倍")
        return 5
    need = px * a.qty
    if need > asset.cash:
        print(f"拒绝：需要 {need:,.2f} 元 > 可用 {asset.cash:,.2f} 元")
        return 5
    print(f"\n准备买入 {CODE} ×{a.qty} 限价 {px}（卖出价 {SELL_PRICE} 之下 "
          f"{(SELL_PRICE - px) / SELL_PRICE * 100:.2f}%），预计 {need:,.2f} 元")

    oid = t.order_stock(acc, CODE, STOCK_BUY, a.qty, FIX_PRICE, px)
    print(f"order_stock → {oid}")
    if not isinstance(oid, int) or oid < 0:
        print("下单失败（负数即拒单；原因见 miniQMT 日志）")
        return 6

    # ---- 轮询成交 ----
    for i in range(20):
        time.sleep(1.0)
        orders = t.query_stock_orders(acc) or []
        hit = [o for o in orders if str(getattr(o, "order_id", "")) == str(oid)]
        if hit:
            o = hit[0]
            print(f"[{i + 1}s] 状态={o.order_status} 已成交={o.traded_volume}/"
                  f"{o.order_volume} 均价={o.traded_price}")
            if o.traded_volume >= o.order_volume:
                print(f"\n✅ 全部成交：{o.traded_volume} 股 @ {o.traded_price}")
                return 0
        else:
            print(f"[{i + 1}s] 未查到该委托（可能未刷新）")
    print("\n⚠️ 20 秒内未确认全部成交，请到 QMT 客户端或下次查询核实")
    return 7


if __name__ == "__main__":
    raise SystemExit(main())

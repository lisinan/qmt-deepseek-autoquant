# -*- coding: utf-8 -*-
"""大 QMT 文件桥 只读联调探针（S3 用，零下单风险）

用途：QMT 侧 `qmt_bridge/qmt_executor.py` 策略启动后，跑这个确认桥通了，
并把 QMT 真实字段名 dump 出来（各版本 get_trade_detail_data 字段有差异，
DUMP 是唯一可靠的校准手段）。

它**只发 PING / DUMP / QUERY_ASSET / QUERY_POSITION**，
不发任何 BUY / SELL / CANCEL。

用法：
    python scripts/bridge_probe.py
    python scripts/bridge_probe.py --root D:\\other_bridge
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.broker_bridge import BridgeBroker, DEFAULT_BRIDGE_ROOT  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=DEFAULT_BRIDGE_ROOT)
    ap.add_argument("--account", default="cash")
    ap.add_argument("--timeout", type=float, default=10.0)
    args = ap.parse_args()

    print("=" * 68)
    print("大 QMT 文件桥 只读联调（不会下单）")
    print("root = %s" % args.root)
    print("=" * 68)

    hb = os.path.join(args.root, "heartbeat.json")
    if not os.path.exists(hb):
        print("\n[FAIL] 心跳文件不存在：%s" % hb)
        print("  ⇒ QMT 侧策略还没启动。请到 QMT「功能 → 模型交易」里加载")
        print("     C:\\pazq_qmt\\python\\qmtide_bridge.py")
        print("     （源码在 qmt_bridge/qmt_executor.py，两份内容一致）")
        print("     资金账号选你自己的普通股票账户、运行模式选「实盘」，然后点运行。")
        print("     日志出现 [BRIDGE] init done 后重试本探针。")
        print("     ⚠ QMT 重启后策略不会自动恢复，必须在模型交易里手动「运行」。")
        return 2

    b = BridgeBroker(root=args.root, cfg={"accounts": {"cash": args.account}},
                     timeout=args.timeout)

    print("\n[1] connect()")
    ok = b.connect()
    print("    -> %s  (mode=%s)" % (ok, b.mode))
    if not ok:
        with open(hb, "r", encoding="utf-8") as f:
            print("    心跳内容: %s" % f.read()[:200])
        return 2

    print("\n[2] PING")
    print("    -> %r" % (b._request("PING"),))

    print("\n[3] DUMP（字段自校准，重点看 schema）")
    d = b.dump()
    if d is None:
        # 注意：dump() 返回 None 有两条完全不同的路径 ——
        #   ① 真的超时（stats.timeout +1）
        #   ② 写 signal 失败（stats.error +1，QMT 侧持有句柄导致 os.replace 冲突）
        # 原来这里一律写「DUMP 超时」，2026-11-08 因此把一次「写 signal 失败」
        # 误判成「QMT 不支持 get_trade_detail_data」，白查一轮。故按 stats 分流。
        st = b.stats()
        if st.get("timeout"):
            print("    -> None（超时：QMT 侧没在 %.0fs 内回 result）" % args.timeout)
        else:
            print("    -> None（写 signal 失败，不是超时；见上方 WARNING）")
            print("       常见原因：QMT 侧读 signal.json 时未申请 FILE_SHARE_DELETE，")
            print("       导致本机 os.replace 冲突。检查 qmtide_bridge._read_bytes_ctypes。")
    else:
        print("    account_id   = %s" % d.get("account_id"))
        print("    account_type = %s" % d.get("account_type"))
        print("    python       = %s" % d.get("python"))
        print("    暴露的下单/查询函数 = %s" % d.get("interesting"))
        for typ, entry in (d.get("schema") or {}).items():
            print("    --- %s count=%s ---" % (typ, entry.get("count")))
            if entry.get("error"):
                print("        error: %s" % entry["error"])
            for k, v in (entry.get("fields") or {}).items():
                print("        %-32s = %s" % (k, v))

    print("\n[4] QUERY_ASSET")
    a = b.get_asset(args.account)
    print("    -> %r" % (a,))
    if a is None:
        print("    !! None = 查询失败（不是「没钱」）")

    print("\n[5] QUERY_POSITION")
    p = b.get_positions(args.account)
    print("    -> %r" % (p,))
    if p is None:
        print("    !! None = 查询失败（不是「空仓」）")
    elif p == []:
        print("    （确认空仓）")

    print("\n[6] stats")
    print("    -> %r" % (b.stats(),))

    print("\n" + "=" * 68)
    if a is not None and p is not None:
        print("RESULT: 只读桥已打通。下一步才是灰度卖出（S4）。")
        return 0
    print("RESULT: 桥未打通，先解决 QMT 侧策略/字段问题，别急着开下单。")
    return 1


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""把 QMT 模型清单里某个策略的 simpleRun 从 1 改成 0（或反之）。

背景
----
QMT 跑 python 策略有两条完全不同的路径，而 ``simpleRun`` 决定走哪条：

  simpleRun="1"  独立进程：``pythonw.exe -u <脚本> <userdata> <ms>``
                 ⇒ ``__name__=='__main__'``、**不调用 init()**、
                   **不注入 passorder**、``sys.stdout is None``
  simpleRun="0"  内嵌引擎：调 ``init(ContextInfo)`` + ``run_time``、
                 注入 ``passorder`` / ``get_trade_detail_data``

2026-11-08 实测：QMTIDE_BRIDGE 被设成 "1"，于是点运行后脚本跑完就退，
QMT 记 ``doRun return code:1``，``C:\\pazq_qmt_bridge\\`` 里连 heartbeat 都没有。
同目录下用户自建的 4 个实盘策略全是 "0"。

**必须在 QMT 完全关闭时运行本脚本**，否则 QMT 退出时会用内存副本覆盖配置文件。

用法：
    python scripts/qmt_set_simple_run.py --check  QMTIDE_BRIDGE
    python scripts/qmt_set_simple_run.py --apply  QMTIDE_BRIDGE
"""
import argparse
import os
import shutil
import sys
import time

CFG = r"C:\pazq_qmt\config\indexUserConfig.xml"


def _read_raw(path):
    with open(path, "rb") as f:
        return f.read()


def find_patterns(name):
    """返回 (1 的写法, 0 的写法)。catalog 项的属性顺序固定为 name type simpleRun。"""
    return (('name="%s" type="2" simpleRun="1"' % name),
            ('name="%s" type="2" simpleRun="0"' % name))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name", help="模型名，例如 QMTIDE_BRIDGE")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--apply", action="store_true")
    ap.add_argument("--target", choices=["0", "1"], default="0",
                    help="目标值，默认 0（内嵌引擎）")
    args = ap.parse_args()

    if not os.path.isfile(CFG):
        print("配置文件不存在: %s" % CFG)
        return 2

    raw = _read_raw(CFG)
    s1, s0 = find_patterns(args.name)
    want = s0 if args.target == "0" else s1
    other = s1 if args.target == "0" else s0

    n_want = raw.count(want.encode("utf-8"))
    n_other = raw.count(other.encode("utf-8"))

    print("配置: %s (%d 字节)" % (CFG, len(raw)))
    print("目标: %s -> simpleRun=%s" % (args.name, args.target))
    print("  已是目标值: %d 处" % n_want)
    print("  需要修改  : %d 处" % n_other)

    if n_other == 0:
        print("无需修改。")
        return 0
    if n_other > 1:
        print("[ABORT] 匹配到 %d 处，预期只有 1 处；不做批量替换。" % n_other)
        return 3

    if args.check:
        print("--check：未写入")
        return 0

    bak = "%s.bak-%s" % (CFG, time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(CFG, bak)
    print("已备份: %s" % bak)

    new = raw.replace(other.encode("utf-8"), want.encode("utf-8"))
    if len(new) != len(raw):
        print("[ABORT] 替换后长度变化，属性顺序可能不是我预期的，未写入。")
        return 4

    with open(CFG, "wb") as f:
        f.write(new)

    back = _read_raw(CFG)
    if back != new:
        print("[FAIL] 回读不一致，正在从备份恢复")
        shutil.copy2(bak, CFG)
        return 5

    # XML 必须仍然合法，否则 QMT 打开会崩或丢配置
    import xml.etree.ElementTree as ET
    try:
        ET.fromstring(new.decode("utf-8"))
    except Exception as e:
        print("[FAIL] XML 解析失败（%s），正在从备份恢复" % e)
        shutil.copy2(bak, CFG)
        return 6

    print("  [OK] 已写入、回读一致、XML 仍合法")
    print("")
    print("下一步：打开 QMT → 模型交易 → 点运行 → 立刻看")
    print(r"  C:\pazq_qmt_bridge\boot.log")
    print('  出现 "[BRIDGE] MODE=embedded init() called" 才算真的起来了')
    return 0


if __name__ == "__main__":
    sys.exit(main())

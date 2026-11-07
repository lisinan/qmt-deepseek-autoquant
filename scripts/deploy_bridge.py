# -*- coding: utf-8 -*-
"""把 qmt_bridge/qmtide_bridge.py 部署到 QMT 的策略目录。

为什么需要这个脚本，而不是直接复制文件
--------------------------------------
QMT 的编辑器只认 GBK。用 UTF-8 存过去的文件，QMT 打开/保存时会按 GBK 解码
失败，把**所有非 ASCII 字符替换成 ``?``**，还会给你塞一个
``# -*- coding: utf-8 -*-`` 头 + 把文件名转大写。第一次部署就踩了这个坑：
583 个问号、中文注释全丢。

所以：源文件保持 UTF-8（可读、可 review），部署时转成 GBK + CRLF 写出去，
QMT 读进来就是合法 GBK，不会再改写。

用法：
    python scripts/deploy_bridge.py            # 部署 + 校验
    python scripts/deploy_bridge.py --check    # 只校验，不写
"""
import argparse
import io
import os
import shutil
import sys
import time

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "qmt_bridge", "qmtide_bridge.py")
DST_DIR = r"C:\pazq_qmt\python"
DST = os.path.join(DST_DIR, "QMTIDE_BRIDGE.py")   # QMT 会把文件名转大写
BRIDGE_ROOT = r"C:\pazq_qmt_bridge"


def _read_src():
    with io.open(SRC, "r", encoding="utf-8") as f:
        return f.read()


def _to_gbk_crlf(text):
    # 统一换行再转 CRLF，QMT 期望 CRLF
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # 源文件是 UTF-8（可读、可 review），部署时首行换成 QMT 认的 gbk 声明
    lines = text.split("\n")
    if lines and lines[0].startswith("#"):
        lines[0] = "#encoding:gbk"
    else:
        lines.insert(0, "#encoding:gbk")
    crlf = "\r\n".join(lines)
    # 先确认能无损编成 GBK，编不了就直接报错，别写坏文件
    return crlf.encode("gbk")


def _verify(raw):
    """回读校验：GBK 能解开、语法能过、没有问号污染。"""
    text = raw.decode("gbk")
    problems = []
    if not text.startswith("#encoding:gbk"):
        problems.append("首行不是 #encoding:gbk")
    if "\r\n" not in text:
        problems.append("没有 CRLF 换行")
    head2 = text.split("\r\n", 2)[:2]
    if any("# -*- coding:" in ln for ln in head2):
        problems.append("残留 utf-8 coding 头（QMT 会把它当 GBK 解）")
    # 问号污染检测：只看注释/字符串之外的连续问号不好做，这里只看密度
    if text.count("?") > 20:
        problems.append("含 %d 个问号，疑似被 QMT 改写过的旧文件" % text.count("?"))
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验不写")
    args = ap.parse_args()

    if not os.path.isfile(SRC):
        print("源文件不存在: %s" % SRC)
        return 2

    text = _read_src()
    try:
        raw = _to_gbk_crlf(text)
    except UnicodeEncodeError as e:
        print("源文件含 GBK 无法表示的字符，无法部署：%s" % e)
        print("提示：QMT 侧文件请保持纯 ASCII 或 GBK 可表示字符。")
        return 3

    problems = _verify(raw)
    print("源: %s (%d 字符)" % (SRC, len(text)))
    print("目标: %s (%d 字节, GBK+CRLF)" % (DST, len(raw)))
    if problems:
        for p in problems:
            print("  [FAIL] %s" % p)
        return 4
    print("  [OK] GBK 编码 / CRLF / 无 coding 头 / 无问号污染")

    if args.check:
        print("--check：未写入")
        return 0

    if not os.path.isdir(DST_DIR):
        print("QMT python 目录不存在: %s" % DST_DIR)
        return 5

    if os.path.isfile(DST):
        bak = "%s.bak-%s" % (DST, time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(DST, bak)
        print("旧文件已备份: %s" % bak)

    # 直接覆盖。若 QMT 正开着，QMT 内存里可能还留着旧副本，
    # 所以部署后要在 QMT 里重新打开策略（或重启客户端）才会读到新内容。
    with open(DST, "wb") as f:
        f.write(raw)

    # 回读确认落地内容就是我们要的
    with open(DST, "rb") as f:
        back = f.read()
    if back != raw:
        print("  [FAIL] 回读内容与写入不一致")
        return 6
    print("  [OK] 已写入并回读一致")

    import py_compile
    import tempfile
    # 编译到临时路径，别在 QMT 的 python 目录里留 .pyc
    # （QMT 内嵌解释器是 3.6，本机 conda 是 3.9，多出的 cpython-39.pyc 只是垃圾）
    fd, tmp_pyc = tempfile.mkstemp(suffix=".pyc")
    os.close(fd)
    try:
        py_compile.compile(DST, cfile=tmp_pyc, doraise=True)
        print("  [OK] 语法检查通过")
    except Exception as e:
        print("  [FAIL] 语法检查失败: %s" % e)
        return 7
    finally:
        try:
            os.remove(tmp_pyc)
        except Exception:
            pass

    # 清掉 QMT 可能留下的 pyc，避免读到旧字节码
    pyc = os.path.join(DST_DIR, "__pycache__")
    if os.path.isdir(pyc):
        for n in os.listdir(pyc):
            if n.lower().startswith("qmtide_bridge"):
                try:
                    os.remove(os.path.join(pyc, n))
                    print("  已清理旧字节码: %s" % n)
                except Exception:
                    pass

    if not os.path.isdir(BRIDGE_ROOT):
        os.makedirs(BRIDGE_ROOT)
    print("共享目录: %s" % BRIDGE_ROOT)
    print("")
    print("下一步（必须人工）：")
    print("  1. QMT 里重新打开该策略，确认编辑器里代码是新内容（不是问号）")
    print("  2. 点运行，然后立刻看 %s" % os.path.join(BRIDGE_ROOT, "boot.log"))
    print("  3. boot.log 里出现 'MODE=embedded init() called' 才算真的起来了")
    return 0


if __name__ == "__main__":
    sys.exit(main())

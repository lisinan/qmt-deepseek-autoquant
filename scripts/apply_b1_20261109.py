# -*- coding: utf-8 -*-
"""B1 落地驱动：一条命令完成「写盘 + 四项自检 + 失败自动还原」。

## 为什么还要一层驱动脚本

11:25 只有一个 5 分钟窗口，四项自检如果靠临场敲命令，最容易出的错就是
「漏了一项」或「比错了对象」。这里把它们固定成代码，任何一项不过就**自动
还原部署版**并返回非零 —— 宁可不打，也不能让 QMT 下次 init 加载坏文件。

四项自检（与 team-lead 2026-11-09 指令一一对应）：

1. 备份存在，且与写盘**前**的目标**逐字节相同**（sha256 比，不看大小）。
2. 部署版 size / mtime 已变（打印确切值，供人工核对）。
3. 对**落盘后的真实文件**再 `compile()` 一次（不是编译内存字符串）。
4. 打印测试命令供执行者立刻跑（本脚本不代跑全量：70s 会挤掉时间窗）。

## 用法

    python scripts/apply_b1_20261109.py --precheck   # 只读：打印写前状态 + 四道断言预演
    python scripts/apply_b1_20261109.py --apply      # 真正写盘（需 team-lead 口令）

## 回滚

本脚本在任一自检失败时会自动把**部署版**用备份覆盖回来并保持仓库版不动；
手工回滚命令也会打印出来。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PATCHER = REPO_ROOT / "scripts" / "patch_bridge_sharing_20261109.py"
REPO_TARGET = REPO_ROOT / "qmt_bridge" / "qmtide_bridge.py"
DEPLOY_TARGET = Path(r"C:\pazq_qmt\python\QMTIDE_BRIDGE.py")
BACKUP_DIR = REPO_ROOT / "_backup" / "patch_bridge_sharing_20261109"
NL = "\n"


def say(msg=""):
    try:
        sys.stdout.write(msg + NL)
        sys.stdout.flush()
    except Exception:
        pass


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def stat_of(p: Path) -> dict:
    st = p.stat()
    return {"size": st.st_size,
            "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
            "sha": sha(p)}


def compile_file(p: Path) -> str:
    """编译**落盘的真实文件**。返回 "" = 通过。"""
    try:
        raw = p.read_bytes()
    except Exception as e:
        return "read failed: %s" % e
    codec = "utf-8"
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        codec = "gbk"
    try:
        compile(raw.decode(codec), str(p), "exec")
        return ""
    except Exception as e:
        return "%s: %s" % (type(e).__name__, e)


def find_backup(target: Path):
    """按 patcher 的命名规则找刚生成的备份（3 分钟内）。"""
    cands = []
    if target == REPO_TARGET:
        if BACKUP_DIR.is_dir():
            cands = sorted(BACKUP_DIR.glob(REPO_TARGET.name + ".bak-*"))
    else:
        cands = sorted(target.parent.glob(target.name + ".bak-*"))
    fresh = [c for c in cands if (time.time() - c.stat().st_mtime) < 180]
    return fresh[-1] if fresh else None


def run_patcher(apply: bool) -> int:
    cmd = [sys.executable, str(PATCHER), "--only", "b1"]
    if apply:
        cmd.append("--apply")
    say("[RUN] %s" % " ".join(cmd))
    pr = subprocess.run(cmd, cwd=str(REPO_ROOT))
    return pr.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description="B1 apply with 4 self-checks")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--precheck", action="store_true", help="read-only: print state, no write")
    g.add_argument("--apply", action="store_true", help="write (needs team-lead go)")
    args = ap.parse_args()

    missing = [p for p in (REPO_TARGET, DEPLOY_TARGET) if not p.exists()]
    if missing:
        for m in missing:
            say("[ABORT] target missing: %s" % m)
        return 2

    before = {p: stat_of(p) for p in (REPO_TARGET, DEPLOY_TARGET)}
    say("[BEFORE]")
    for p, s in before.items():
        say("  %-52s size=%-7d mtime=%s sha=%s" % (p, s["size"], s["mtime"], s["sha"][:16]))

    if args.precheck:
        rc = run_patcher(apply=False)
        say("[PRECHECK] patcher dry-run rc=%d（未写盘）" % rc)
        say("[PRECHECK] 落盘后自检项将检查：备份逐字节 / size+mtime 变化 / compile(真实文件) / 测试")
        return 0 if rc == 0 else 2

    # ---------------- apply ----------------
    rc = run_patcher(apply=True)
    if rc != 0:
        say("[ABORT] patcher returned rc=%d（它自己已拒绝写盘或已回滚）" % rc)
        return 2

    fails = []

    # 自检 1：备份与原字节逐字节相同
    say("[CHECK 1] 备份比对（sha256）")
    for p in (REPO_TARGET, DEPLOY_TARGET):
        bak = find_backup(p)
        if bak is None:
            fails.append("备份未找到: %s" % p)
            say("  [FAIL] %s -> 备份未找到" % p)
            continue
        bsha = sha(bak)
        ok = (bsha == before[p]["sha"])
        say("  [%s] %s" % ("OK" if ok else "FAIL", bak))
        say("         backup sha=%s  before sha=%s" % (bsha[:16], before[p]["sha"][:16]))
        if not ok:
            fails.append("备份与写前字节不同: %s" % bak)

    # 自检 2：部署版 size/mtime 已变
    say("[CHECK 2] 部署版 size / mtime")
    after_dep = stat_of(DEPLOY_TARGET)
    say("  before size=%d mtime=%s" % (before[DEPLOY_TARGET]["size"],
                                       before[DEPLOY_TARGET]["mtime"]))
    say("  after  size=%d mtime=%s" % (after_dep["size"], after_dep["mtime"]))
    if after_dep["sha"] == before[DEPLOY_TARGET]["sha"]:
        fails.append("部署版内容未变（补丁没写进去？）")
        say("  [FAIL] 内容未变")

    # 自检 3：编译落盘后的真实文件
    say("[CHECK 3] compile() 落盘后的真实文件")
    for p in (REPO_TARGET, DEPLOY_TARGET):
        err = compile_file(p)
        say("  [%s] %s%s" % ("OK" if not err else "FAIL", p, "" if not err else "  " + err))
        if err:
            fails.append("compile 失败: %s (%s)" % (p, err))

    if fails:
        say("[ROLLBACK] 自检失败 %d 项，用备份还原部署版（仓库版保持不动）" % len(fails))
        bak = find_backup(DEPLOY_TARGET)
        if bak:
            shutil.copy2(str(bak), str(DEPLOY_TARGET))
            say("  restored %s <- %s" % (DEPLOY_TARGET, bak))
            say("  post-restore sha=%s" % sha(DEPLOY_TARGET)[:16])
        else:
            say("  [CRITICAL] 找不到部署版备份，请手工执行：")
            say('  copy /Y "<backup>" "%s"' % DEPLOY_TARGET)
        for f in fails:
            say("  - %s" % f)
        return 2

    say("[CHECK 4] 立刻执行（本脚本不代跑，避免挤掉时间窗）：")
    say("  python tests/run_all.py")
    say('  python -c "import tests.test_bridge_io_sharing as t, inspect; '
        '[f() for n,f in inspect.getmembers(t, inspect.isfunction) '
        'if n.startswith(\'test_\')]"')
    say("[DONE] 文件已就位 —— 但**未生效**：需 OWNER 在 QMT 里 停止策略 -> 运行策略，")
    say("       然后读 C:\\pazq_qmt_bridge\\heartbeat.json 的 rev 确认。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

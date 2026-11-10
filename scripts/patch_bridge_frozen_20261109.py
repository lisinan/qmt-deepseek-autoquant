# -*- coding: utf-8 -*-
"""QMT 侧 bridge 补丁：total_asset 计入冻结资金（P0）+ heartbeat 账号回退 + CODE_REV。

## 为什么需要这个脚本，而不是手工 Edit

1. **部署版与仓库版编码不同**：仓库 ``qmt_bridge/qmtide_bridge.py`` 是 UTF-8/LF，
   部署版 ``C:\\pazq_qmt\\python\\QMTIDE_BRIDGE.py`` 是 **GBK/CRLF**（实测差异只有
   第 1 行编码头 + 行尾 + 编码本体）。仓库源码里有 ``★ → − ≤`` 等 **GBK 装不下**
   的符号，一旦「读成 str → 改 → 整文件写回」，这些字符会被替换成 ``?``，
   注释与日志串全毁。所以本脚本一律 **rb 读 / ASCII 子串替换 / wb 写**，
   全程不解码、不重排、不规范化行尾。
2. **必须证明「只改了该改的」**：写盘前对三处目标串各断言**恰好命中 1 次**，
   任一 ≠1 立即中止且**不写盘**（避免把补丁打到错误的上下文里）。
3. **写盘后回读自校验**：用 difflib 逐行对比，要求「除首行编码头外，只有 3 处
   预期改动，其余逐行等价」。这等价于手工 diff，但不依赖人眼。

## 三处改动（详见 reports/PATCH_bridge_frozen_20261109.md）

| # | 位置 | 作用 | 级别 |
|---|---|---|---|
| A | ``_total_asset_of()`` 内 | ``derived = cash + frozen + mv``（原漏 frozen） | **P0** |
| B | ``_write_heartbeat()`` 内 | ``account`` 复用 ``_account_of()`` 的 globals 回退 | P2 |
| C | 模块级 ``CODE_REV`` | 递增版本戳，供 heartveat/PING 证明新代码已加载 | 必做 |

## 用法（默认 dry-run，必须显式 --apply 才写盘）

    # 1) 先预演（不写盘），看三处命中与预期改动
    python scripts/patch_bridge_frozen_20261109.py

    # 2) 正式写盘（自动备份 + 回读校验）
    python scripts/patch_bridge_frozen_20261109.py --apply

    # 3) 仓库版与部署版一起打（两者必须同源，否则下次排障会看到两个 bridge）
    python scripts/patch_bridge_frozen_20261109.py --apply --repo

## 回滚

备份文件路径会在写盘时打印。回滚 = 把备份写回原路径：

    copy /Y "<backup>" "<target>"

并**同样需要在 QMT 里重新运行策略**才会生效（与打补丁同一条规矩）。

## 生效条件（改文件 ≠ 生效）

QMT 里跑的是**已加载**的旧代码。写完文件后必须由 OWNER 在 QMT GUI 里
停止策略 → 重新运行，然后回读 ``heartbeat.json`` 的 ``rev``：

    rev == "2026-11-09T0915-frozen-in-total"   => 新代码已加载
    仍是 "2026-11-08T21:31-mv-from-positions"  => 没生效，等于没修
"""
from __future__ import annotations

import argparse
import difflib
import os
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REPO_TARGET = REPO_ROOT / "qmt_bridge" / "qmtide_bridge.py"
DEPLOY_TARGET = Path(r"C:\pazq_qmt\python\QMTIDE_BRIDGE.py")

OLD_REV = b'CODE_REV = "2026-11-08T21:31-mv-from-positions"'
NEW_REV = b'CODE_REV = "2026-11-09T0915-frozen-in-total"'

# (标签, 旧字节串, 新行列表)  —— 新行不带行尾，写盘时按目标文件的行尾风格拼
PATCHES = [
    (
        "A/total_asset",
        b"    derived = cash + mv",
        [
            b'    frozen = _f(_attr(a, ["m_dFrozenCash", "m_dFrozen"], 0))',
            b"    derived = cash + frozen + mv",
        ],
    ),
    (
        # 一律用**整行**做 old/new（含行首缩进）。若只替换行内子串，
        # 新内容会叠在原缩进之后（12+12=24 空格），回读自校验就会报「意外改动」。
        "B/heartbeat_account",
        b'            "account": getattr(ContextInfo, "accountID", DEFAULT_ACCOUNT_ID),',
        [b'            "account": _account_of(ContextInfo, None)[0],'],
    ),
    (
        "C/code_rev",
        OLD_REV,
        [NEW_REV],
    ),
]


def _say(msg: str) -> None:
    """只输出 ASCII：部署/运维常在 cp1252 或 gbk 控制台上跑，非 ASCII 会直接抛。"""
    try:
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def _detect_codec(data: bytes) -> str:
    try:
        data.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "gbk"


def _newline_of(data: bytes) -> bytes:
    return b"\r\n" if b"\r\n" in data else b"\n"


def _backup_path(target: Path, stamp: str) -> Path:
    """仓库内的目标把备份放进 _backup/（已被 .gitignore 覆盖），避免污染工作区。"""
    try:
        target.resolve().relative_to(REPO_ROOT.resolve())
        inside_repo = True
    except Exception:
        inside_repo = False
    if inside_repo:
        d = REPO_ROOT / "_backup" / "patch_bridge_20261109"
        d.mkdir(parents=True, exist_ok=True)
        return d / (target.name + ".bak-" + stamp)
    return target.with_name(target.name + ".bak-" + stamp)


def _verify(old_text: str, new_text: str) -> list:
    """回读自校验：除预期 3 处外逐行等价。返回错误列表（空 = 通过）。

    注意：**不能按 PATCHES 的列表顺序逐 hunk 对齐**。PATCHES 是按「重要性/编号」
    排的（A/B/C），而它们在文件里的物理位置是 C(:116) → B(:465) → A(:614)。
    因此这里按「替换后的行内容」匹配 hunk，而不是按下标。
    """
    errs = []
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    if not old_lines or not new_lines:
        return ["empty file after patch"]
    if old_lines[0] != new_lines[0]:
        errs.append("line 1 changed: %r -> %r" % (old_lines[0][:40], new_lines[0][:40]))
    sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    changed = [op for op in sm.get_opcodes() if op[0] != "equal"]
    if len(changed) != len(PATCHES):
        errs.append("expected %d changed hunks, got %d" % (len(PATCHES), len(changed)))
        return errs
    want_map = {}
    for tag, _old, new_lines_raw in PATCHES:
        want_map[tuple(ln.decode("ascii") for ln in new_lines_raw)] = tag
    seen = set()
    for op in changed:
        _t, _i1, _i2, j1, j2 = op
        got = tuple(new_lines[j1:j2])
        tag = want_map.get(got)
        if tag is None:
            errs.append("unexpected hunk at new line %d: %r" % (j1 + 1, list(got)[:2]))
            continue
        if tag in seen:
            errs.append("hunk %s appears twice" % tag)
        seen.add(tag)
    missing = set(p[0] for p in PATCHES) - seen
    if missing:
        errs.append("missing hunks: %s" % ",".join(sorted(missing)))
    return errs


def patch_one(target: Path, apply: bool) -> int:
    """返回 0=成功/可写，2=中止。"""
    if not target.exists():
        _say("[ABORT] target not found: %s" % target)
        return 2
    raw = target.read_bytes()
    codec = _detect_codec(raw)
    nl = _newline_of(raw)
    _say("[INFO] %s" % target)
    _say("[INFO] size=%d codec=%s newline=%s bytes=%d"
         % (len(raw), codec, "CRLF" if nl == b"\r\n" else "LF", raw.count(nl)))

    # ---- 1) 三处目标串各断言恰好命中 1 次；任一不符立即中止，绝不写盘 ----
    # 先判「是否已打过」：否则重复执行会以「matched 0 times」报错，容易误读成
    # 「文件不对」，掩盖真正的结论「已经打过了」。
    if raw.count(b"derived = cash + frozen + mv") > 0:
        _say("[ABORT] already patched (frozen form present); nothing written")
        return 2
    for tag, old, _new in PATCHES:
        n = raw.count(old)
        _say("[CHECK] %-20s hits=%d" % (tag, n))
        if n != 1:
            _say("[ABORT] pattern %s matched %d times (must be exactly 1); nothing written"
                 % (tag, n))
            return 2

    # ---- 2) 内存中做 ASCII 字节替换 ----
    new_raw = raw
    for _tag, old, new_lines in PATCHES:
        new = nl.join(new_lines)
        new_raw = new_raw.replace(old, new, 1)

    old_text = raw.decode(codec)
    try:
        new_text = new_raw.decode(codec)
    except UnicodeDecodeError as e:
        _say("[ABORT] patched bytes no longer decode as %s: %s; nothing written" % (codec, e))
        return 2

    # ---- 3) 写完前先在内存里做等价性自校验 ----
    errs = _verify(old_text, new_text)
    if errs:
        for e in errs:
            _say("[ABORT] %s" % e)
        _say("[ABORT] self-check failed; nothing written")
        return 2

    new_lines = new_text.splitlines()
    old_lines = old_text.splitlines()
    _say("[CHECK] lines %d -> %d (expect +%d)"
         % (len(old_lines), len(new_lines), sum(len(p[2]) for p in PATCHES) - len(PATCHES)))
    _say("[CHECK] self-check OK: only 3 expected hunks changed, all other lines identical")

    if not apply:
        _say("[DRY-RUN] no write. re-run with --apply to patch this file.")
        return 0

    # ---- 4) 备份 → 写盘 → 回读复核 ----
    stamp = time.strftime("%Y%m%d-%H%M")
    bak = _backup_path(target, stamp)
    try:
        shutil.copy2(str(target), str(bak))
    except Exception as e:
        _say("[ABORT] backup failed (%s); nothing written" % e)
        return 2
    _say("[BACKUP] %s" % bak)

    try:
        target.write_bytes(new_raw)
    except Exception as e:
        _say("[ABORT] write failed: %s" % e)
        _say("[ROLLBACK] copy /Y \"%s\" \"%s\"" % (bak, target))
        return 2

    back = target.read_bytes()
    if back != new_raw:
        _say("[ABORT] read-back mismatch after write; restoring backup")
        shutil.copy2(str(bak), str(target))
        return 2
    _say("[OK] written (%d bytes)" % len(back))
    _say("[NEXT] reload the QMT strategy (stop -> run), then check rev in heartbeat.json")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="patch bridge total_asset/heartbeat/CODE_REV (dry-run by default)")
    ap.add_argument("--target", action="append", default=None,
                    help="file to patch (repeatable). default: deployed QMTIDE_BRIDGE.py")
    ap.add_argument("--repo", action="store_true",
                    help="also patch the in-repo qmt_bridge/qmtide_bridge.py")
    ap.add_argument("--apply", action="store_true",
                    help="actually write (default is dry-run)")
    args = ap.parse_args()

    targets = [Path(t) for t in (args.target or [str(DEPLOY_TARGET)])]
    if args.repo and REPO_TARGET not in targets:
        targets.append(REPO_TARGET)

    _say("[MODE] %s" % ("APPLY" if args.apply else "DRY-RUN"))
    rc = 0
    for t in targets:
        rc = patch_one(t, args.apply)
        if rc != 0:
            break
    if rc == 0 and args.apply:
        _say("[DONE] remember: file change is NOT effective until QMT re-runs the strategy")
    return rc


if __name__ == "__main__":
    sys.exit(main())

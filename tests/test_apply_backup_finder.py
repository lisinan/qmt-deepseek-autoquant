# -*- coding: utf-8 -*-
"""守卫：`scripts/apply_b1_20261109.py::find_backup` 必须找得回刚生成的备份。

## 为什么需要这条守卫

2026-11-09 11:25 落盘 B1 后，自检 1 报「备份未找到」，驱动脚本因此判定失败并进入回滚
分支 —— 而**回滚分支调的是同一个 finder**，于是它「想回滚但找不到备份」，两个自检项
一起哑火。真因不是备份不存在，而是 finder 用 mtime 判新旧：

    fresh = [c for c in cands if (time.time() - c.stat().st_mtime) < 180]

备份是 `shutil.copy2` 做的，**copy2 会把源文件 mtime 一并复制** ⇒ 刚生成的备份带着
源文件（昨天）的 mtime ⇒ 永远被过滤掉。

## 这条守卫怎么才有鉴别力

用 `shutil.copy2` 造一个「mtime 是旧日期、文件名是今天」的备份 —— 这正是真实场景。
然后断言：
  1. 能找到它（按文件名时间戳）；
  2. 它的 `st_mtime` 确实是很久以前（证明这条用例真的在考 finder，不是在考文件系统）；
  3. 文件名时间戳超出窗口的备份**不能**被选中；
  4. 多个候选时选**最新**的那个。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts.apply_b1_20261109 import _bak_ts, _pick_fresh  # noqa: E402


def _mk_backup(dirpath: Path, name: str, src: Path, old_mtime: bool = True) -> Path:
    """造一个备份：content 复制、mtime 保持源文件的旧时间（模拟 copy2 的真实行为）。"""
    bak = dirpath / name
    shutil.copy2(str(src), str(bak))
    if not old_mtime:
        os.utime(str(bak), None)
    return bak


def test_find_backup_uses_filename_not_mtime():
    d = Path(tempfile.mkdtemp())
    src = d / "target.py"
    src.write_text("# source\n", encoding="utf-8")
    old = time.time() - 86400 * 30          # 源文件 mtime 设为 30 天前
    os.utime(str(src), (old, old))

    ts = time.strftime("%Y%m%d-%H%M%S")
    bak = _mk_backup(d, "target.py.bak-" + ts, src)

    # 前置断言：这条用例真的在考 finder —— 备份的 mtime 必须是旧的
    assert time.time() - bak.stat().st_mtime > 86400, \
        "拷贝出来的备份 mtime 竟然不是旧的 ⇒ 本用例失去鉴别力（copy2 行为变了？）"

    picked = _pick_fresh(sorted(d.glob("target.py.bak-*")))
    assert picked == bak, "刚生成的备份没被找到（mtime bug 复发）"
    assert _bak_ts(bak) is not None, "文件名时间戳解析失败"


def test_stale_backup_outside_window_is_ignored():
    d = Path(tempfile.mkdtemp())
    src = d / "target.py"
    src.write_text("# source\n", encoding="utf-8")
    old_ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() - 3600))
    _mk_backup(d, "target.py.bak-" + old_ts, src)
    assert _pick_fresh(sorted(d.glob("target.py.bak-*"))) is None, \
        "1 小时前的备份被当成了「刚生成」"


def test_newest_backup_wins():
    d = Path(tempfile.mkdtemp())
    src = d / "target.py"
    src.write_text("# source\n", encoding="utf-8")
    older = time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() - 60))
    newer = time.strftime("%Y%m%d-%H%M%S")
    _mk_backup(d, "target.py.bak-" + older, src)
    bak_new = _mk_backup(d, "target.py.bak-" + newer, src)
    assert _pick_fresh(sorted(d.glob("target.py.bak-*"))) == bak_new, \
        "多候选时没有选最新的那个"


def test_names_without_timestamp_are_ignored():
    d = Path(tempfile.mkdtemp())
    src = d / "target.py"
    src.write_text("# source\n", encoding="utf-8")
    _mk_backup(d, "target.py.bak", src)
    assert _pick_fresh(sorted(d.glob("target.py.bak*"))) is None, \
        "没有时间戳后缀的文件被当成了备份"


def test_both_timestamp_precisions_are_recognised():
    """★ 必须认生产代码真实产生的**两种**精度。

    `patch_bridge_sharing_20261109.py` 用 `%Y%m%d-%H%M`（4 位，真实备份名形如
    `qmtide_bridge.py.bak-20261109-1124`），`patch_b2_20261109.py` 用 6 位。
    只认 6 位会让真实备份全部解析失败 —— 而只造 6 位样本的守卫测试**测不出来**。
    """
    d = Path(tempfile.mkdtemp())
    src = d / "target.py"
    src.write_text("# source\n", encoding="utf-8")
    now = time.time()
    p4 = _mk_backup(d, "target.py.bak-" + time.strftime("%Y%m%d-%H%M",
                                                       time.localtime(now - 30)), src)
    p6 = _mk_backup(d, "target.py.bak-" + time.strftime("%Y%m%d-%H%M%S",
                                                       time.localtime(now)), src)
    assert _bak_ts(p4) is not None, "4 位精度（HHMM）解析失败 —— 真实备份会全部找不到"
    assert _bak_ts(p6) is not None, "6 位精度（HHMMSS）解析失败"
    assert _pick_fresh(sorted(d.glob("target.py.bak-*"))) == p6, \
        "4/6 位混存时没有选最新的那个"

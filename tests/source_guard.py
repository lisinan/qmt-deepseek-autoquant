# -*- coding: utf-8 -*-
"""结构性源码守卫的**共享安全读取** helper。

## 为什么需要它（2026-10-19 实盘铁证）

本项目有 6 处「结构性源码守卫」用 ``inspect.getsource(某方法)`` 检查源码里
是否含某段调用/赋值。该 API 依赖 ``linecache``，而 linecache 的首个条目来自
**`.pyc` 里记录的源文件大小/mtime**。当 ``__pycache__`` 里留着旧版 `.pyc` 时
（本仓库被多个会话并行改动，`.pyc` 滞后是常态）：

    co_firstlineno 指向**旧文件**的行号
    ⇒ getsource 在**新文件**上按旧行号切块
    ⇒ 返回**另一个函数**的源码

实盘铁证：2026-10-19 ``test_daily_pnl_crossday_reset`` 报
「恢复路径不再调用 _reset_daily_if_needed」，而源码里明明有这一行；
清掉 ``__pycache__`` 后立刻转绿。表现为**幽灵失败**——单独跑必过、
插探针必过、只有完整跑才红，极难定位（本轮花了 6 轮假设才锁定）。

## 本 helper 的做法

**直接读磁盘源码**（``Path.read_text``）+ 按 ``def <名>(`` 切块
⇒ 与 `.pyc` / 行号 / linecache 彻底解耦，只认「当前文件内容」。
这也顺带覆盖「源码被改但 pyc 未更新」的情形。

## 用法

    from tests.source_guard import method_source
    src = method_source("engine/event_engine.py", "_restore_engine_state")
    assert "_reset_daily_if_needed()" in src

或对模块级函数（有 ``__module__`` 的）：

    from tests.source_guard import fn_source
    src = fn_source(run_minute_backtest)
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent


def method_source(rel_path: str, name: str,
                  indent: str = "    ") -> Optional[str]:
    """从磁盘读取某文件里名为 ``name`` 的方法/函数体（到下一个同级 def 为止）。

    :param rel_path: 相对仓库根目录的路径，如 "engine/event_engine.py"
    :param name:     方法名（不含 self/类前缀），如 "_restore_engine_state"
    :param indent:   该定义的缩进（类内方法默认 4 空格；模块级函数传 ""）
    :return:         源码块；找不到返回 None（调用方应 assert 失败）
    """
    p = ROOT / rel_path
    if not p.exists():
        return None
    src = p.read_text(encoding="utf-8")
    # 匹配 `def name(` 且缩进恰为 indent；块体到下一个**同级或更外层** def 为止
    pat = re.compile(
        r"^" + re.escape(indent) + r"def " + re.escape(name) + r"\b.*?\n"
        r"(?=" + re.escape(indent) + r"def |\nclass |\Z)",
        re.DOTALL | re.MULTILINE)
    m = pat.search(src)
    return m.group(0) if m else None


def fn_source(fn) -> Optional[str]:
    """从磁盘读取某模块级函数的源码（按 ``__module__`` 定位文件）。

    比 ``inspect.getsource(fn)`` 安全：同样走磁盘，不受 `.pyc` 行号影响。
    找不到文件/符号时返回 None。

    实现：只用 ``inspect.getsourcefile`` 拿文件路径（它不读源码内容，
    只按 ``__code__.co_filename`` 解析），再用 ``__name__`` 做磁盘切块
    ⇒ 完全不经过 linecache 的源码缓存。
    """
    name = getattr(fn, "__name__", None)
    if not name:
        return None
    try:
        file = inspect.getsourcefile(fn)
    except Exception:
        return None
    if not file:
        return None
    try:
        rel = str(Path(file).resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return None      # 不在本仓库内（如 stdlib / site-packages）
    return method_source(rel, name, indent="")


def module_source(rel_path: str) -> Optional[str]:
    """整文件源码（磁盘直读）。"""
    p = ROOT / rel_path
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")

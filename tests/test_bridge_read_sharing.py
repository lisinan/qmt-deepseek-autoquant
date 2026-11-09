# -*- coding: utf-8 -*-
"""B2 守卫：读侧共享位 / 心跳窗 / 两条 P1 日志文案。

设计原则（与 test_bridge_io_sharing 一致）：**补丁未部署 / 已部署两种状态都必须绿**。
做法是拿 `scripts/patch_b2_20261109.py` 的 `transform()` 打在仓库当前源码上，
再 `ast` 抠出需要的函数 exec —— 既不 import 生产 `core/`（避免连券商/连行情），
也不要求补丁已经落盘。

★ 本文件里有一条**反断言**（`test_share_handle_does_not_enable_os_replace`）：
  共享位**并不能**让 `os.replace` 在目标被持句柄时成功（2026-11-09 实测，本进程与
  跨进程一律 WinError 5）。所以 B2 不是「修好 183」，它只是让读者的句柄不再阻断
  `os.remove`（次生链的入口）。这条断言的作用是防止未来有人把 B2 写成「183 已解决」。
"""
from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts.patch_b2_20261109 import TARGETS, _detect, transform  # noqa: E402

_KEEP_ASSIGN = ("_SHARE_ALL", "_GENERIC_READ", "_OPEN_EXISTING", "_FILE_ATTRIBUTE_NORMAL")


def _patched_text(rel: str) -> str:
    """仓库当前源码 + 补丁（无论是否已部署，结果一致）。"""
    spec = [t for t in TARGETS if t["rel"] == rel][0]
    raw = (REPO / rel).read_bytes()
    codec, nl = _detect(raw)
    text = raw.decode(codec)
    out, _states = transform(text, nl, spec["blocks"], spec["tag"])
    compile(out, rel, "exec")          # 顺手验补丁产物可编译
    return out


def _ns_for(rel: str, funcs=()) -> dict:
    text = _patched_text(rel)
    tree = ast.parse(text)
    ns = {"os": os, "json": json, "logger": _FakeLogger()}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in funcs:
            exec(compile(ast.Module([node], []), rel, "exec"), ns)
        elif isinstance(node, ast.Assign):
            name = getattr(node.targets[0], "id", "")
            if name in _KEEP_ASSIGN or name == "HEARTBEAT_STALE_SEC":
                exec(compile(ast.Module([node], []), rel, "exec"), ns)
    return ns


class _FakeLogger:
    def __init__(self):
        self.records = []

    def warning(self, msg, *a):
        try:
            self.records.append(msg % a if a else msg)
        except Exception:
            self.records.append(str(msg))


def _stub(name="result.json", body='{"old":1}'):
    d = tempfile.mkdtemp()
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(body)
    return p


# ---------------------------------------------------------------- (a) 共享位
def test_share_handle_does_not_block_os_remove():
    """★ 本补丁真正要证明的事：持共享句柄时 `os.remove` 不被拒。

    这是次生链的入口 —— 旧代码「os.remove 失败 → 回退 os.rename 撞上仍存在的
    目标 ⇒ WinError 183」。读者给了 DELETE 位，`os.remove` 就能过。
    """
    ns = _ns_for("core/broker_bridge.py", ("_open_share_delete",))
    p = _stub()
    f = ns["_open_share_delete"](p)
    try:
        assert f.read() == '{"old":1}', "共享句柄读不到内容"
        os.remove(p)                       # 句柄未关就必须成功
    finally:
        f.close()
    assert not os.path.exists(p)


def test_plain_open_handle_does_block_os_remove():
    """对照组：普通 `open()` 句柄必须**挡住** `os.remove`。

    没有这条，上一条测试可能因为「这机器上 os.remove 根本不在乎句柄」而假绿。
    """
    p = _stub()
    f = open(p, "r", encoding="utf-8")
    try:
        try:
            os.remove(p)
            raise AssertionError("普通 open 句柄竟然让 os.remove 通过了 —— 对照组失效，"
                                 "共享位那条测试失去鉴别力")
        except PermissionError:
            pass
    finally:
        f.close()


def test_share_handle_does_not_enable_os_replace():
    """★ 反断言：共享位**不能**让 `os.replace` 在目标被持句柄时成功。

    2026-11-09 实测（本进程 + 跨进程）一律 WinError 5。留着这条，是为了防止
    未来有人把 B2 写成「183 已解决」—— 183 的主修是 B1 的 8 次退避重试。
    """
    ns = _ns_for("core/broker_bridge.py", ("_open_share_delete",))
    p = _stub()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write('{"new":2}')
    f = ns["_open_share_delete"](p)
    try:
        try:
            os.replace(tmp, p)
            raise AssertionError("共享位竟然让 os.replace 成功了 —— 与 2026-11-09 实测"
                                 "不符，说明环境变了，§4 的结论必须重测")
        except PermissionError:
            pass
    finally:
        f.close()


def test_falls_back_to_plain_open_and_still_reads():
    """共享打开失败必须回落普通 open，且**仍读到内容**（读不到比 183 严重得多）。"""
    ns = _ns_for("core/broker_bridge.py", ("_open_share_delete",))
    p = _stub()

    class _Broken:
        def __getattr__(self, name):
            raise RuntimeError("ctypes unavailable")

    saved = sys.modules.get("ctypes")
    sys.modules["ctypes"] = _Broken()
    try:
        f = ns["_open_share_delete"](p)
        try:
            assert f.read() == '{"old":1}', "回落普通 open 后读不到内容"
        finally:
            f.close()
    finally:
        if saved is not None:
            sys.modules["ctypes"] = saved
        else:
            sys.modules.pop("ctypes", None)


def test_read_json_uses_share_helper_and_degrades_to_none():
    text = _patched_text("core/broker_bridge.py")
    assert "_open_share_delete(path)" in text, "_read_json 没有走共享打开的 helper"
    ns = _ns_for("core/broker_bridge.py", ("_open_share_delete", "_read_json"))
    p = _stub()
    assert ns["_read_json"](p) == {"old": 1}
    assert ns["_read_json"](p + ".missing") is None


# ------------------------------------------------------------ (b) 心跳窗 12s
def test_heartbeat_stale_sec_is_twelve():
    ns = _ns_for("core/broker_bridge.py", ())
    assert ns["HEARTBEAT_STALE_SEC"] == 12.0, \
        "HEARTBEAT_STALE_SEC 应为 12.0（6.0 会把一次写失败+一次读延迟判成断线）"
    text = _patched_text("core/broker_bridge.py")
    assert "HEARTBEAT_STALE_SEC = 6.0" not in text, "旧值 6.0 残留"


# --------------------------------------------------------- (c) 两条 P1 日志
def test_engine_state_missing_row_is_warned():
    text = _patched_text("engine/event_engine.py")
    flat = " ".join(text.split())
    assert "engine_state 无行 ⇒ 从初始状态启动" in flat, "缺 engine_state 行时仍未留痕"
    for field in ("peak_asset", "consec_loss", "halted", "daily_trade_count"):
        assert field in flat, "WARNING 文案缺字段 %s" % field


def test_restore_failure_wording_is_not_consolatory():
    text = _patched_text("engine/event_engine.py")
    flat = " ".join(text.split())
    assert "恢复引擎状态失败（从初始状态启动）" not in flat, \
        "旧措辞仍在：把异常写成了正常启动"
    assert "恢复引擎状态**失败**，已从初始状态启动" in flat
    assert "%s" in flat, "改了措辞却丢了 %s 占位符（异常本身会消失）"

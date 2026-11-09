# -*- coding: utf-8 -*-
"""守卫：桥接读写的 Windows 共享冲突（B1 写侧重试 / B2 读侧共享位，2026-11-09）。

## 为什么这样测（而不是直接 import 生产模块）

补丁按团队规矩**先不部署**（OWNER 未批）。但「测试红着等上线」比不提交更糟：
它会让下一轮把「本来就坏」当成「改坏的」。所以本模块把
``scripts/patch_bridge_sharing_20261109.py`` 的纯函数 ``transform()`` 作用在
仓库**当前**源码上，写进临时目录再 import：

  - 补丁未上线 -> 测的是「打过补丁后」的代码，即补丁必须先证明自己写对了；
  - 补丁已上线 -> ``transform`` 幂等返回同一份代码，测的还是它。

两种状态都必须全绿。附带效果：补丁脚本本身写错（模式命不中、编码不对、
行尾破坏）也会在这里立刻变红。

## 钉住的四件事

1. ``os.replace`` 前两次抛 ``PermissionError``、第三次成功 ⇒ 必须成功且内容正确，
   退避必须是 0.02/0.04（不是死循环也不是一次就放弃）。
2. 8 次全失败 ⇒ **必须 raise**，且**目标文件仍存在、内容逐字节未变**。
   这条专门防旧实现那个 ``os.remove + os.rename`` 非原子回退复活：它会把
   目标文件删掉再重命名，失败即**丢一份 result**（实盘 = 静默漏单）。
   同时钉死「不许静默」：函数体内不得有裸 ``except Exception``，重试耗尽必须
   先 ``_boot(...)`` 落盘留痕再 raise —— 11-09 那次就是两个真实错误被
   ``except``/``pass`` 依次吞掉，日志里只剩回退第二步那句误导人的 WinError 183。
3. ``_read_json``：文件不存在 → ``None``；半截 JSON → ``None`` 且不抛；正常 → dict。
4. 读句柄必须带 ``FILE_SHARE_DELETE``：读者持句柄期间写方 ``os.remove`` 必须
   能过（对照：普通 ``open()`` 句柄会被挡住）。这是 ``WinError 183`` 那条链的
   起点。**注意**：``os.replace`` 即使在给足 SHARE_DELETE 时也会因目标被他人
   持句柄而返回 WinError 5（2026-11-09 实测，本进程与跨进程都一样），所以
   replace 只由 B1 的退避重试兜，本文件**不**对 replace 的成功做断言。
5. 「不许静默」不只是「写了日志」：``_boot`` 自己抛异常时，``_atomic_write_json``
   仍须抛**原始**错误 —— 否则就是用次生错误盖住第一因，和 183 那次同型。
6. ``HEARTBEAT_STALE_SEC`` 该是 12.0（安全侧常量，需授权）。
"""
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "scripts", "patch_bridge_sharing_20261109.py")
B1_SRC = os.path.join(ROOT, "qmt_bridge", "qmtide_bridge.py")
B2_SRC = os.path.join(ROOT, "core", "broker_bridge.py")


def _load_patch_script():
    """按路径加载补丁脚本（scripts/ 不是包，不能按模块名 import）。"""
    spec = importlib.util.spec_from_file_location(
        "_patch_bridge_sharing_20261109", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_patch = _load_patch_script()


def _patched_bytes(which, src_path):
    with open(src_path, "rb") as f:
        raw = f.read()
    new_raw, _status, _codec, _nl = _patch.transform(raw, which)
    return new_raw


def _load_patched_module(which, src_path, modname):
    """把补丁打到源码副本上并 import。返回 (module, tmpdir)。"""
    new_raw = _patched_bytes(which, src_path)
    d = tempfile.mkdtemp(prefix="qmt_share_")
    p = os.path.join(d, modname + ".py")
    with open(p, "wb") as f:
        f.write(new_raw)
    old_env = os.environ.get("QMT_BRIDGE_ROOT")
    old_hook = sys.excepthook
    # QMT 侧 bridge 在 import 时就把 BRIDGE_ROOT 定死并写 boot.log，
    # 必须指向临时目录，否则测试会往 C:\pazq_qmt_bridge 里写诊断日志。
    os.environ["QMT_BRIDGE_ROOT"] = os.path.join(d, "bridge_root")
    try:
        spec = importlib.util.spec_from_file_location(modname, p)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[modname] = mod
        spec.loader.exec_module(mod)
    finally:
        # 生产模块 import 时会用 _install_excepthook() 把 sys.excepthook 指向
        # boot.log；留着它会让整个测试 runner 的未捕获异常消失（无法排障）。
        sys.excepthook = old_hook
        if old_env is None:
            os.environ.pop("QMT_BRIDGE_ROOT", None)
        else:
            os.environ["QMT_BRIDGE_ROOT"] = old_env
    return mod, d


def _shim_replace(mod, seq):
    """把 mod 内的 os.replace 换成一个按 seq 行为的假实现。

    seq 为「前 N 次失败」的 N；None = 每次都失败。返回 (state, sleeps, restore)。
    只重绑模块全局名 ``mod.os`` / ``mod.time``，不动真正的 os / time 模块 ——
    后者会影响并发跑的其它测试。
    """
    state = {"n": 0}
    sleeps = []
    real_replace = os.replace

    def _replace(src, dst):
        state["n"] += 1
        if seq is None or state["n"] <= seq:
            raise PermissionError(32, "ERROR_SHARING_VIOLATION")
        return real_replace(src, dst)

    class _OsShim(object):
        replace = staticmethod(_replace)

        def __getattr__(self, name):
            # 其余 os.* 原样转发：_boot() 要用 os.path / os.makedirs / os.getpid
            # 去写 boot.log。首版把整个 os 换成「只有 replace」的桩，结果 _boot
            # 静默失效 —— 那个「静默」正是本补丁要消灭的东西，桩不能自己再造一个。
            return getattr(os, name)

    class _TimeShim(object):
        sleep = staticmethod(lambda s: sleeps.append(s))

        def __getattr__(self, name):
            return getattr(time, name)

    orig_os, orig_time = mod.os, mod.time
    mod.os, mod.time = _OsShim(), _TimeShim()

    def restore():
        mod.os, mod.time = orig_os, orig_time
    return state, sleeps, restore


# ---------------------------------------------------------------- B1 写侧重试
def test_b1_retry_two_failures_then_succeeds():
    """前两次 os.replace 抛共享冲突，第三次必须成功，且退避是 0.02 / 0.04。"""
    mod, d = _load_patched_module("b1", B1_SRC, "patched_qmtide_bridge_a")
    try:
        p = os.path.join(d, "result.json")
        state, sleeps, restore = _shim_replace(mod, seq=2)
        try:
            mod._atomic_write_json(p, {"signal_id": "sid-1", "status": "ok"})
        finally:
            restore()
        assert state["n"] == 3, "必须试到第 3 次（前两次失败不能直接放弃）"
        assert [round(s, 3) for s in sleeps] == [0.02, 0.04], \
            "退避必须是 0.02*(i+1)，实际 %r" % (sleeps,)
        with open(p, encoding="utf-8") as f:
            assert json.load(f)["signal_id"] == "sid-1"
        assert not os.path.exists(p + ".tmp"), "成功后不得留下 .tmp"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_b1_retries_exhausted_raise_and_leave_target_intact():
    """8 次全失败 ⇒ 必须抛；目标文件原样保留（删掉的那个非原子回退不能复活）。

    旧实现在这里会 `os.remove(path)` 成功 + `os.rename(tmp, path)` 成功，
    于是**不抛异常、目标文件被换掉**，一份 result 就这么丢了。本用例同时
    断言「抛了」与「文件没动」，任一不成立即红。
    """
    mod, d = _load_patched_module("b1", B1_SRC, "patched_qmtide_bridge_b")
    try:
        p = os.path.join(d, "heartbeat.json")
        original = b'{"rev": "keep-me"}'
        with open(p, "wb") as f:
            f.write(original)
        state, sleeps, restore = _shim_replace(mod, seq=None)
        raised = None
        try:
            mod._atomic_write_json(p, {"rev": "must-not-land"})
        except OSError as e:
            raised = e
        finally:
            restore()
        assert raised is not None, "重试耗尽必须抛 OSError，不得静默吞掉"
        assert state["n"] == 8, "必须恰好重试 8 次，实际 %d" % state["n"]
        assert [round(s, 3) for s in sleeps] == \
            [0.02, 0.04, 0.06, 0.08, 0.10, 0.12, 0.14, 0.16], sleeps
        with open(p, "rb") as f:
            assert f.read() == original, "失败时目标文件内容必须逐字节未变"
        # 运行时（不是静态 grep）证明「先落 boot.log 再抛」：QMT 用 pythonw 跑，
        # 没有 stdout，不落盘就等于没发生 —— 11-09 就是因此误判过两次。
        boot = os.path.join(d, "bridge_root", "boot.log")
        assert os.path.exists(boot), "重试耗尽必须写 boot.log"
        with open(boot, encoding="utf-8", errors="replace") as f:
            hits = [ln for ln in f if "write failed after 8 retries" in ln]
        assert hits, "boot.log 里必须留一条写失败记录（%r）" % hits
        assert "PermissionError" in hits[0], "留痕必须带异常类型，否则仍要靠猜: %r" % hits[0]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _func_text(src_bytes, name):
    """取出一个模块级函数的源码文本（到下一个 def/class 为止）。"""
    text = src_bytes.decode("utf-8")
    i = text.index("def %s(" % name)
    ends = [p for p in (text.find("\ndef ", i + 1), text.find("\nclass ", i + 1)) if p != -1]
    return text[i:(min(ends) if ends else len(text))]


def _code_only(body):
    """去掉整行注释与**白名单**日志吞错行，剩下的才是「不许静默」的作用域。

    注释里会**引用**坏写法（如 `except Exception:`），必须剥掉，否则误判；
    而 `_boot(...)` 外面那一层 `except Exception:  # noqa: log-only` 是
    team-lead 明确允许的唯一例外（只吞日志、不许吞业务错误），用可 grep 的
    标记把它白名单化 —— 不做宽松匹配，白名单之外照旧一条都不许有。
    """
    keep = []
    for ln in body.splitlines():
        s = ln.strip()
        if s.startswith("#"):
            continue
        if s == "except Exception:  # noqa: log-only":
            continue
        keep.append(ln)
    return "\n".join(keep)


def test_b1_log_failure_never_masks_original_error():
    """``_boot`` 自己抛异常时，必须仍抛**原始**错误，不能被日志异常顶掉。

    这条比「函数里有 ``_boot(``」更有价值：后者只证明写了日志，前者证明
    「日志失败不会吃掉真错误」—— 而「用次生错误盖住第一因」正是今天在修的
    那个毛病（183 盖住 replace 的 WinError 5）。
    """
    mod, d = _load_patched_module("b1", B1_SRC, "patched_qmtide_bridge_c")
    try:
        p = os.path.join(d, "result.json")
        original = b'{"rev": "keep-me"}'
        with open(p, "wb") as f:
            f.write(original)
        state, _sleeps, restore = _shim_replace(mod, seq=None)
        orig_boot = mod._boot

        def _boom_boot(_msg):
            raise RuntimeError("boot.log 写不进去")

        mod._boot = _boom_boot
        raised = None
        try:
            mod._atomic_write_json(p, {"rev": "must-not-land"})
        except BaseException as e:          # noqa: BLE001 - 就是要抓全部
            raised = e
        finally:
            mod._boot = orig_boot
            restore()
        assert raised is not None, "重试耗尽必须抛，不得因日志异常而静默返回"
        assert isinstance(raised, PermissionError), (
            "必须抛原始的 PermissionError，实际 %r —— 日志异常不许顶掉真错误"
            % (raised,))
        assert state["n"] == 8, "重试次数不该受日志异常影响"
        with open(p, "rb") as f:
            assert f.read() == original, "失败时目标文件内容必须逐字节未变"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_inserted_blocks_have_no_py37_plus_syntax():
    """插入块必须是 **Python 3.6 兼容**的语法 —— 不是「3.9 能 compile」。

    QMT 内嵌解释器是 **3.6.8**（项目记录），而我们的 compile 闸跑在 3.9
    （conda qmt）/ 3.13（PATH）。**3.9 能编译 ≠ 3.6 能跑**：只要插入块里出现
    海象运算符、`match`、`from __future__ import annotations`、`dataclasses`、
    f-string 的 `=` 说明符、`dict |` 合并、`removeprefix`、位置-only 参数 `/`
    之类，3.9 全绿而 3.6 一重载就 `SyntaxError` —— 且发生在 ``init`` 阶段，
    桥直接起不来 = 盘中假死。

    ⚠️ 这是**扫描**，不是兼容性证明。唯一的真证明是 11:30 重载后
    ``boot.log`` 出现新的 ``init done`` 且 ``heartbeat.rev`` 变成新值；
    若 rev 变了但 boot.log 没有 ``init done``，就是「编译过了但初始化失败」。
    """
    hazards = [
        (r":=", "海象运算符(3.8)"),
        (r"^\s*match\s+.*:\s*$", "match 语句(3.10)"),
        (r"from\s+__future__\s+import\s+annotations", "__future__.annotations(3.7)"),
        (r"^\s*import\s+dataclasses|^\s*from\s+dataclasses", "dataclasses(3.7)"),
        (r"f[\"'][^\"']*\{[^}]*=\}", "f-string 的 = 说明符(3.8)"),
        (r"capture_output\s*=", "subprocess capture_output(3.7)"),
        (r"\.removeprefix\(|\.removesuffix\(", "str.removeprefix/suffix(3.9)"),
        (r"^\s*def\s+\w+\([^)]*,\s*/\s*[,)]", "位置-only 参数(3.8)"),
        (r"\bbreakpoint\s*\(", "breakpoint()(3.7)"),
        (r"asyncio\.run\s*\(", "asyncio.run(3.7)"),
        (r"\bmath\.prod\b", "math.prod(3.8)"),
    ]
    for which, src in (("b1", B1_SRC), ("b2", B2_SRC)):
        blocks = _patch.PATCH_SPECS[which]["blocks"]
        inserted = []
        for _old, new in blocks:
            inserted.extend(new)
        text = "\n".join(inserted)
        for pat, why in hazards:
            m = re.search(pat, text, re.M)
            assert m is None, \
                "%s 插入块含 3.6 不支持的语法 %s：%r" % (which, why, m.group(0) if m else "")
    # 正向留痕：插入块里允许（且确实存在）的 3.6 安全构造
    b2_text = "\n".join(ln for _o, new in _patch.PATCH_SPECS["b2"]["blocks"] for ln in new)
    assert "-> bytes:" in b2_text, "函数返回注解是 3.0+ 语法，3.6 安全（记录在案）"


def test_b1_code_rev_is_bumped():
    """打完补丁后 ``CODE_REV`` 必须**不等于**旧值，且形如 ``2026-11-09T<HHMM>-…``。

    为什么这条是硬需求：`heartbeat.json` 里**没有**独立的 code_rev 键 ——
    ``rev`` 就是 ``CODE_REV``（三处 ``"rev": CODE_REV``）。只改代码不 bump
    rev ⇒ 重载前后 ``rev`` 逐字符相同 ⇒ 「部署 ≠ 生效」无法验收，重载了和
    没重载读数一模一样（2026-11-09 差点就这么上线）。

    附带钉两件事：① ``CODE_REV`` 只能有**一处**模块级定义（多处会让 bump 只
    生效一半）；② 三处引用必须都指向这个常量，不许有人另写死字符串字面量。
    """
    src = _patched_bytes("b1", B1_SRC).decode("utf-8")
    revs = re.findall(r'^CODE_REV = "([^"]+)"', src, re.M)
    assert len(revs) == 1, "CODE_REV 必须恰好一处模块级定义，实际 %r" % (revs,)
    rev = revs[0]
    assert rev != "2026-11-08T21:31-mv-from-positions", \
        "必须 bump CODE_REV，否则重载后 heartbeat 的 rev 不变、验收失效"
    assert rev.startswith("2026-11-09T"), \
        "格式应为 2026-11-09T<HHMM>-<slug>，实际 %r" % (rev,)
    assert rev.isascii(), "CODE_REV 必须纯 ASCII（部署版是 GBK，非 ASCII 会被写成 ?）"
    assert src.count('"rev": CODE_REV') == 3, \
        "heartbeat / 心跳 / PONG 三处引用必须都指向 CODE_REV 常量"
    assert "2026-11-08T21:31-mv-from-positions" not in src, "旧 rev 不得残留"


def test_b1_non_atomic_fallback_is_gone():
    """静态契约：非原子回退不许复活，且写失败**不许再被静默吞掉**。"""
    new_raw = _patched_bytes("b1", B1_SRC)
    body = _func_text(new_raw, "_atomic_write_json")
    code = _code_only(body)
    assert "os.rename" not in code, \
        "非原子回退（remove+rename）不得复活：它必败且会丢一份 result"
    assert "for i in range(8):" in code
    assert code.count("os.replace(tmp, path)") == 1
    # ★ 2026-11-09 team-lead 追加要求：今天的代价是「日志骗了我们两次」——
    #   os.replace 的失败被 `except Exception:` 吞掉、os.remove 的失败被
    #   `except: pass` 吞掉，日志里只剩回退第二步那句误导人的 WinError 183。
    #   所以钉死：除「只吞日志」的白名单那一处外，函数体内不得出现裸
    #   `except Exception`；写失败必须落 boot.log 再抛。
    assert "except Exception" not in code, \
        "写失败不得再用裸 `except Exception` 吞掉（那次就是这样丢掉第一因的）"
    assert "except OSError as e" in code, "只该捕获 os.replace 的 OSError 并重试"
    assert "_boot(" in code, "重试耗尽必须先写 boot.log 留痕，再 raise"
    assert "raise last" in code, "重试耗尽必须 raise，不能静默返回"
    # 白名单只许出现一次，且必须紧跟 `pass`（只吞日志），不得夹带其它语句
    lines = [ln.strip() for ln in body.splitlines()]
    idx = [i for i, ln in enumerate(lines) if ln == "except Exception:  # noqa: log-only"]
    assert len(idx) == 1, "log-only 白名单必须恰好一处，实际 %d" % len(idx)
    assert lines[idx[0] + 1] == "pass", "白名单块内只允许 pass"
    assert "raise last" in "\n".join(lines[idx[0]:]), "白名单之后必须仍 raise 原始错误"


# ------------------------------------------------------------- B2 读侧共享位
def test_b2_missing_file_returns_none():
    mod, d = _load_patched_module("b2", B2_SRC, "patched_broker_bridge_a")
    try:
        assert mod._read_json(os.path.join(d, "nope.json")) is None
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_b2_half_written_json_returns_none_without_raising():
    """半截 JSON（写方正在写）必须 None，绝不能抛到调用方。"""
    mod, d = _load_patched_module("b2", B2_SRC, "patched_broker_bridge_b")
    try:
        p = os.path.join(d, "result.json")
        for payload in (b'{"signal_id": "abc"', b"", b"not json at all"):
            with open(p, "wb") as f:
                f.write(payload)
            assert mod._read_json(p) is None, "半截/非法 JSON 必须返回 None: %r" % payload
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_b2_normal_json_returns_dict():
    mod, d = _load_patched_module("b2", B2_SRC, "patched_broker_bridge_c")
    try:
        p = os.path.join(d, "heartbeat.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"rev": "x", "ts": 1.5}, f)
        assert mod._read_json(p) == {"rev": "x", "ts": 1.5}
        # QMT 那边某些工具写出的文件带 UTF-8 BOM；带 BOM 也必须读得出来。
        with open(p, "wb") as f:
            f.write(b'\xef\xbb\xbf{"rev": "bom"}')
        assert mod._read_json(p) == {"rev": "bom"}
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_b2_reader_grants_share_delete_so_writer_can_remove():
    """读者持句柄期间，写方 os.remove 必须成功 —— 缺 FILE_SHARE_DELETE 就红。

    确定性做法：把 kernel32 的 ReadFile 换成一个「先放行、再等闸门」的包装，
    读句柄因此被**确定地**按住；此时主线程删文件。

    为什么钉 remove 而不是 replace（2026-11-09 实测）：本机 Windows 上
    ``MoveFileEx(REPLACE_EXISTING)`` 对「目标仍被任何进程持句柄」一律返回
    WinError 5，**给足 SHARE_DELETE 也一样**（本进程、跨进程都试过）。所以
    replace 只能靠退避重试救，而 SHARE_DELETE 真正买到的是「remove 能过」——
    这正是旧实现 ``os.remove + os.rename`` 回退的第一步：它在这里失败过，
    才有后面的 WinError 183。

    对照组（plain open）必须在同一用例里失败，证明本断言确实有牙。
    """
    mod, d = _load_patched_module("b2", B2_SRC, "patched_broker_bridge_d")
    try:
        p = os.path.join(d, "result.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"rev": "old"}, f)
        # 先真读一次：让真实 ReadFile 的 argtypes 建好（否则包装里按 int 传
        # 64 位句柄会被截断）。
        assert mod._read_json(p) == {"rev": "old"}

        k32 = mod._k32()
        real_read = k32.ReadFile
        gate = threading.Event()
        release = threading.Event()

        def _slow(*a, **kw):
            gate.set()
            release.wait(2.0)
            return real_read(*a, **kw)

        err = []

        def _reader():
            try:
                mod._read_json(p)
            except Exception as e:          # pragma: no cover - 失败即测试红
                err.append(e)

        k32.ReadFile = _slow
        try:
            t = threading.Thread(target=_reader, daemon=True)
            t.start()
            assert gate.wait(2.0), "读者没有走到 ReadFile（用例自身失效）"
            os.remove(p)                    # ← 必须不被读句柄挡住
            release.set()
            t.join(5.0)
            assert not t.is_alive(), "读者线程未结束"
            assert not err, "读者抛异常: %r" % (err,)
        finally:
            release.set()
            k32.ReadFile = real_read

        # 对照组：同样在持句柄期间删，但句柄是普通 open()（无 SHARE_DELETE）。
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"rev": "old"}, f)
        fh = open(p, "rb")
        try:
            blocked = False
            try:
                os.remove(p)
            except PermissionError:
                blocked = True
            assert blocked, (
                "对照组没被挡住 ⇒ 本机对 open() 句柄不设限，本用例失去鉴别力，"
                "必须重新设计而不是留着一条永远绿的断言")
        finally:
            fh.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_b2_share_mask_includes_write_and_delete():
    """静态契约：共享掩码必须是 READ|WRITE|DELETE 三位，少一位就红。"""
    new_raw = _patched_bytes("b2", B2_SRC)
    assert b"SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004" in new_raw, \
        "读句柄必须同时给 FILE_SHARE_READ|WRITE|DELETE"
    assert new_raw.count(b"_read_bytes_shared(path)") == 1, \
        "_read_json 必须走共享读原语"
    # B2b：引擎侧那句「根因已在 qmtide_bridge 侧一并修掉」必须被更正 ——
    # 按实测，补共享位并不能让 os.replace 通过，主修是写侧重试。
    assert "根因已在 qmtide_bridge 侧一并修掉".encode("utf-8") not in new_raw, \
        "过时结论必须删掉，否则下一轮会按错的因果去查"
    assert "真正的免疫就是这里的重试".encode("utf-8") in new_raw


def test_b2c_heartbeat_threshold_is_twelve():
    """静态契约：`HEARTBEAT_STALE_SEC` 必须是 12.0，且旧的 6.0 不得残留。

    它是**安全侧常量**（决定 `connect()` 的断线判定）：判太早会把「QMT 侧一次
    慢查询」误判成已死并触发重连；判太晚会让引擎拿着死通道当活着用。改这条
    必须带授权（本次 6.0→12.0 由 OWNER + team-lead 授权）。
    """
    new_raw = _patched_bytes("b2", B2_SRC)
    assert b"HEARTBEAT_STALE_SEC = 12.0" in new_raw, "必须是 12.0"
    assert b"HEARTBEAT_STALE_SEC = 6.0" not in new_raw, "旧的 6.0 不得残留"


def test_b2_bridgebroker_round_trip_still_works():
    """打过 B2 之后，BridgeBroker 的「心跳 → 发信号 → 收 result」闭环仍要通。

    ``_read_json`` 同时被心跳判定（``_heartbeat_age``）与 result 轮询使用：
    改坏了会直接让 ``connect()`` 判「桥接未就绪」，live 全线停摆 ——
    「补丁自己把功能敲掉」这类风险必须当场测掉，不能等部署后靠实盘发现。
    """
    mod, d = _load_patched_module("b2", B2_SRC, "patched_broker_bridge_e")
    try:
        hb_p = os.path.join(d, "heartbeat.json")
        sig_p = os.path.join(d, "signal.json")
        res_p = os.path.join(d, "result.json")
        with open(hb_p, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "rev": "test"}, f)
        stop = threading.Event()

        def _fake_qmt():
            while not stop.is_set():
                try:
                    with open(sig_p, encoding="utf-8") as f:
                        sig = json.load(f)
                except Exception:
                    time.sleep(0.01)
                    continue
                payload = {"signal_id": sig["signal_id"], "status": "ok",
                           "data": {"cash": 1.0, "frozen": 0.0,
                                    "market_value": 2.0, "total_asset": 3.0}}
                tmp = res_p + ".qmt"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f)
                # 这里必须自己重试：本用例就实测到——引擎正读 result.json 的
                # 那几十微秒里，写方 os.replace 会拿到 WinError 5（与 11-09 实盘
                # 那两次写失败同因）。B1 补丁给 QMT 侧加的就是这个退避重试；本桩
                # 照着做，否则线程会带着 traceback 死掉，用例变成偶然绿。
                for _ in range(50):
                    try:
                        os.replace(tmp, res_p)
                        break
                    except PermissionError:
                        time.sleep(0.005)
                stop.wait(0.01)

        t = threading.Thread(target=_fake_qmt, daemon=True)
        t.start()
        try:
            broker = mod.BridgeBroker(cfg={"bridge_root": d}, root=d, timeout=2.0)
            assert broker.connect() is True, "心跳新鲜却判未连接 ⇒ _read_json 被改坏"
            asset = broker.get_asset()
            assert asset == {"cash": 1.0, "frozen": 0.0,
                             "market_value": 2.0, "total_asset": 3.0}, asset
        finally:
            stop.set()
            t.join(3.0)
    finally:
        shutil.rmtree(d, ignore_errors=True)

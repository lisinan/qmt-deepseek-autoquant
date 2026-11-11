# -*- coding: utf-8 -*-
"""bridge 读写两侧共享冲突补丁：B1 写侧重试（P0）+ B2 读侧共享位（P1）。

## 为什么需要这个脚本，而不是手工 Edit

1. **两个目标编码不同**：仓库 ``qmt_bridge/qmtide_bridge.py`` 是 UTF-8/LF，
   部署版 ``C:\\pazq_qmt\\python\\QMTIDE_BRIDGE.py`` 是 **GBK/CRLF**；而
   ``core/broker_bridge.py`` 只有仓库版（引擎直接跑仓库代码）。本脚本把
   「旧块 / 新块」都当**行列表**，按目标文件实测的编码与行尾拼成字节串再
   替换 —— 全程 rb/wb，不解码、不重排、不规范化行尾。
2. **必须证明「只改了该改的」**：写盘前对每个目标块断言**恰好命中 1 次**，
   任一 ≠1 立即中止且不写盘。
3. **幂等**：新块已存在时返回 ``already`` 并跳过，重复执行不会报
   「matched 0 times」把人误导成「文件不对」。
4. **四道断言，缺一不写盘**：
   ① 每块 ``old`` 恰好命中 1 次；② 逐块**逆向还原**后字节串等于原文；
   ③ 编码/行尾按目标文件实测值拼接（GBK 装不下就中止，绝不写出 ``?``）；
   ④ ``compile()`` —— 写盘前编译内存里的新源码，写盘后再编译**落盘的真实
   文件**。语法错的文件一旦落盘，QMT 下次 ``init`` 直接加载失败 = 盘中断桥。

## 四个改动（详见 reports/PATCH_bridge_sharing_20261109.md）

| # | 位置 | 作用 | 级别 |
|---|---|---|---|
| B1 | ``qmt_bridge/qmtide_bridge.py::_atomic_write_json`` | ``os.replace`` 加 8 次退避重试；删掉 ``os.remove + os.rename`` 非原子回退；**重试耗尽先落 boot.log 再 raise** | **P0** |
| B1b | ``qmt_bridge/qmtide_bridge.py:116 CODE_REV`` | 递增为 ``2026-11-09T1125-write-retry`` —— 否则 heartbeat 的 ``rev`` 重载前后一样，**生效无法验收** | 必做 |
| B2 | ``core/broker_bridge.py::_read_json`` | 读句柄改 ``FILE_SHARE_READ\\|WRITE\\|DELETE``（ctypes），异常回落 ``open()`` | P1 |
| B2b | ``core/broker_bridge.py:59-60`` 注释 | 更正「根因已一并修掉」这句过时结论（team-lead 2026-11-09 授权） | 文档 |
| B2c | ``core/broker_bridge.py:46`` | ``HEARTBEAT_STALE_SEC`` 6.0 → 12.0（决定 ``connect()`` 断线判定） | **安全侧常量** |

★ **「脚本里有块」≠「允许写盘」**：B2c 与 B1 的写盘各自需要 team-lead 当场
口令（B2c 还要求 OWNER 对「收盘后 B2 整批一次 commit」点头）。

★ 因果链要看对（team-lead 2026-11-09 亲自复核）：**日志里那个 WinError 183 不是
``os.replace`` 报的，是回退路径第二步 ``os.rename`` 报的。** 三步依次是
① ``os.replace`` 失败（实测 WinError 5）被 ``except Exception:`` 无声吞掉 →
② 回退里 ``os.remove`` 因目标被读句柄占着而 PermissionError，又被 ``except:
pass`` 吞掉 → ③ 目标还在，``os.rename`` 报 WinError 183。**整条链吞掉两个真实
错误，只留下最误导人的第三个**，所以修的时候连「不许静默」一起修（B1 会先写
boot.log 再 raise）。

★ 实测结论（2026-11-09，本机 Windows + conda qmt）：给足 SHARE_DELETE **也不能**
让 ``os.replace`` 在「目标被任何进程持句柄」时成功（一律 WinError 5，本进程 /
跨进程都一样）。所以**B1 的退避重试才是主修**；B2 只做两件事：堵住那条次生
错误链的入口（remove 可通过），以及让读侧不再独占。**别把 B2 当成 replace 的
替代方案。** 至于第一因为什么存在（Defender / 索引器 / 瞬时占用）**不深挖**——
重试的语义就是「等它松手」，0.72s 远小于 10s 超时。

## 用法（默认 dry-run，必须显式 --apply 才写盘）

    python scripts/patch_bridge_sharing_20261109.py              # 预演三个目标
    python scripts/patch_bridge_sharing_20261109.py --only b2    # 只预演读侧
    python scripts/patch_bridge_sharing_20261109.py --apply      # 正式写盘（自动备份+回读校验）

## 回滚

写盘时打印备份路径（仓库内目标放在 ``_backup/patch_bridge_sharing_20261109/``，
已被 .gitignore 覆盖）。回滚 = ``copy /Y "<backup>" "<target>"``。

## 生效条件（改文件 ≠ 生效）

- B1（QMT 侧）：必须由 OWNER 在 QMT GUI 停止策略 → 重新运行；靠
  ``heartbeat.json`` 的 ``rev`` 字段证明加载的是哪一版。
- B2（引擎侧）：必须重启引擎；随后核对 ``源码 mtime < 进程启动时刻``。
"""
from __future__ import annotations

import argparse
import difflib
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
B1_REPO = REPO_ROOT / "qmt_bridge" / "qmtide_bridge.py"
B1_DEPLOYED = Path(r"C:\pazq_qmt\python\QMTIDE_BRIDGE.py")
B2_REPO = REPO_ROOT / "core" / "broker_bridge.py"

# ---------------------------------------------------------------------------
# B1：写侧重试，删掉非原子回退
# ---------------------------------------------------------------------------
B1_OLD = [
    "    try:",
    "        os.replace(tmp, path)",
    "    except Exception:",
    "        # very old python without os.replace",
    "        try:",
    "            os.remove(path)",
    "        except Exception:",
    "            pass",
    "        os.rename(tmp, path)",
]
B1_NEW = [
    "    # 2026-11-09 P0: replace 失败改为 8 次退避重试, 并删除 os.remove + os.rename 回退。",
    "    # ★ 因果链要看对(team-lead 2026-11-09 亲自验过): 日志里那个 WinError 183",
    "    #   不是 os.replace 报的, 而是**回退路径第二步** os.rename 报的:",
    "    #     1) os.replace 失败(本机实测 WinError 5), 被 `except Exception:` 无声吞掉;",
    "    #     2) 回退里 os.remove(path) 因目标仍被读句柄占着而 PermissionError,",
    "    #        又被 `except: pass` 吞掉;",
    "    #     3) 目标还在 -> os.rename(tmp, path) 报 WinError 183(ERROR_ALREADY_EXISTS)。",
    "    #   整条链吞掉两个真实错误, 只留下最误导人的第三个。实盘代价: 08:48:52 与",
    "    #   盘中各一次写失败, 整份 result 丢失, 引擎只能等满 10s 超时。",
    "    # 现在: 退避重试(总预算 0.02*(1+..+8)=0.72s, 远小于 10s 超时), 耗尽才 raise;",
    "    #   且**先写 boot.log 再抛** —— QMT 用 pythonw 跑, 没有 stdout, 不落盘就等于",
    "    #   没发生过(2026-11-08 就是因为只 print 而误判过一次)。",
    "    last = None",
    "    for i in range(8):",
    "        try:",
    "            os.replace(tmp, path)",
    "            return",
    "        except OSError as e:",
    "            last = e",
    "            time.sleep(0.02 * (i + 1))",
    "    # ★ 日志失败绝不许盖住真错误: 若 _boot 自己抛, 原始 last 也必须照抛不误;",
    "    #   否则就变成「用次生错误盖住第一因」—— 正是今天在修的那个毛病。",
    "    try:",
    '        _boot("[BRIDGE] write failed after 8 retries: %s (%s) -> %s"',
    "              % (type(last).__name__, last, path))",
    "    except Exception:  # noqa: log-only",
    "        pass",
    "    raise last",
]

# ---------------------------------------------------------------------------
# B2：读句柄共享位 FILE_SHARE_READ|WRITE|DELETE
# ---------------------------------------------------------------------------
B2_OLD = [
    "def _read_json(path: str):",
    "    try:",
    '        with open(path, "r", encoding="utf-8") as f:',
    "            return json.load(f)",
    "    except Exception:",
    "        return None",
]
B2_NEW = [
    '_K32 = {}',
    "",
    "",
    "def _k32():",
    '    """惰性加载 kernel32（带 use_last_error，GetLastError 才可靠）。"""',
    '    k = _K32.get("h")',
    "    if k is None:",
    "        import ctypes",
    '        k = ctypes.WinDLL("kernel32", use_last_error=True)',
    '        _K32["h"] = k',
    "    return k",
    "",
    "",
    "def _read_bytes_shared(path: str) -> bytes:",
    '    """用 FILE_SHARE_READ|WRITE|DELETE 打开并整份读出。',
    "",
    "    为什么不能用内置 open()：Windows 下 Python 的 open() 只申请",
    "    FILE_SHARE_READ|FILE_SHARE_WRITE，**不含** FILE_SHARE_DELETE。于是只要",
    "    本机引擎正读着 result.json / heartbeat.json，QMT 侧写方的",
    "    os.remove(target) 就抛 PermissionError，跟着 os.rename 撞上仍然存在的",
    "    目标文件，抛 WinError 183（ERROR_ALREADY_EXISTS）—— 2026-11-09 08:48:52",
    "    与盘中那两次写失败都是这条链。给上 DELETE 共享位后 remove 直接通过",
    "    （实测：share=READ|DELETE 就能删掉一个正被别的进程读着的文件）。",
    "",
    "    ★ 但这一步**不是** os.replace 的替代：实测本机 Windows 上",
    "    MoveFileEx(REPLACE_EXISTING) 对「任何进程仍持句柄」的目标一律返回",
    "    ERROR_ACCESS_DENIED（WinError 5），给足 SHARE_DELETE 也一样。所以",
    "    replace 失败只能靠 QMT 侧的退避重试（B1）——读者句柄只活几十微秒，",
    "    下一次重试必然过。本改动消除的是 183 那条链的**起点**，不是 replace。",
    '    """',
    "    import ctypes",
    "    from ctypes import wintypes",
    "",
    "    k32 = _k32()",
    "    GENERIC_READ = 0x80000000",
    "    SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004",
    "    OPEN_EXISTING = 3",
    "    FILE_ATTRIBUTE_NORMAL = 0x80",
    "    INVALID_HANDLE = ctypes.c_void_p(-1).value",
    "",
    "    k32.CreateFileW.argtypes = [",
    "        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,",
    "        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]",
    "    k32.CreateFileW.restype = wintypes.HANDLE",
    "    k32.GetFileSize.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]",
    "    k32.GetFileSize.restype = wintypes.DWORD",
    "    k32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,",
    "                             wintypes.LPDWORD, wintypes.LPVOID]",
    "    k32.ReadFile.restype = wintypes.BOOL",
    "    k32.CloseHandle.argtypes = [wintypes.HANDLE]",
    "",
    "    h = k32.CreateFileW(path, GENERIC_READ, SHARE_ALL, None,",
    "                        OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)",
    "    if not h or h == INVALID_HANDLE:",
    '        raise OSError(ctypes.get_last_error(), "CreateFileW failed: %s" % path)',
    "    try:",
    "        size = int(k32.GetFileSize(h, None))",
    "        if size <= 0:",
    '            return b""',
    "        buf = ctypes.create_string_buffer(size)",
    "        got = wintypes.DWORD(0)",
    "        if not k32.ReadFile(h, buf, size, ctypes.byref(got), None):",
    '            raise OSError(ctypes.get_last_error(), "ReadFile failed: %s" % path)',
    "        return buf.raw[:got.value]",
    "    finally:",
    "        try:",
    "            k32.CloseHandle(h)",
    "        except Exception:",
    "            pass",
    "",
    "",
    "def _read_json(path: str):",
    '    """读一个 JSON 文件；任何失败返回 None（调用方一律按「查不到」处理）。',
    "",
    "    读侧走 _read_bytes_shared（FILE_SHARE_READ|WRITE|DELETE），这是",
    "    2026-11-09「引擎读 result.json 期间 QMT 侧 os.replace 抛 WinError 183」",
    "    的根因修复之一。ctypes 不可用（非 Windows / 沙箱）时回落普通 open()，",
    "    行为与修复前逐位一致；半截 JSON 一律 None，不抛。",
    '    """',
    "    try:",
    "        raw = _read_bytes_shared(path)",
    "    except Exception:",
    "        try:",
    '            with open(path, "r", encoding="utf-8") as f:',
    "                return json.load(f)",
    "        except Exception:",
    "            return None",
    "    try:",
    '        return json.loads(raw.decode("utf-8-sig"))',
    "    except Exception:",
    "        return None",
]

# ---------------------------------------------------------------------------
# B2b：引擎侧那句「根因已一并修掉」的注释更正（team-lead 2026-11-09 授权）
# ---------------------------------------------------------------------------
B2_COMMENT_OLD = [
    "    # 2026-11-08 实测 8 次请求里 1 次触发；根因已在 qmtide_bridge 侧一并修掉",
    "    # （ctypes 读改为 FILE_SHARE_READ|WRITE|DELETE），此处是纵深防御。",
]
B2_COMMENT_NEW = [
    "    # 2026-11-08 实测 8 次请求里 1 次触发。",
    "    # ★ 2026-11-09 更正：别再指望「把读侧共享位补全就能让 replace 通过」。实测",
    "    #   本机 Windows 上 MoveFileEx(REPLACE_EXISTING) 对任何仍被其他进程持句柄的",
    "    #   目标一律 WinError 5，给足 FILE_SHARE_DELETE 也一样（本进程 / 跨进程都",
    "    #   验过）。所以**真正的免疫就是这里的重试** —— 占用是瞬时的（读句柄只活",
    "    #   几十微秒），等它松手即可。qmtide_bridge 侧补的共享位只解决「被读着时",
    "    #   能不能删」，不是 replace 的替代品。",
]

# ---------------------------------------------------------------------------
# B2c：HEARTBEAT_STALE_SEC 6.0 -> 12.0（安全侧常量；OWNER + team-lead 授权）
#
# 「脚本里有这一块」≠「允许写盘」。写盘前提：OWNER 对「收盘后 B2 整批一次
# commit」点头，且 team-lead 当场给口令。原因：它决定 connect() 的断线判定，
# 判错会让引擎拿着死通道当活着用。
# ---------------------------------------------------------------------------
B2_HEARTBEAT_OLD = [
    "HEARTBEAT_STALE_SEC = 6.0       # 心跳超过此值视为 QMT 侧已死",
]
B2_HEARTBEAT_NEW = [
    "# 2026-11-09: 6.0 -> 12.0（安全侧常量：决定 connect() 的断线判定；",
    "#   OWNER + team-lead 授权，随 B2 整批一起上）。",
    "#   改大 = 更能容忍 QMT 侧一段心跳断档而不误判「已死」；代价是「真死」被",
    "#   识破得更晚（最多晚 6s）。单次请求仍有 DEFAULT_TIMEOUT=10s 兜底，",
    "#   所以不会无限等，也不会把死通道当成能下单的通道。",
    "HEARTBEAT_STALE_SEC = 12.0      # 心跳超过此值视为 QMT 侧已死",
]

# ---------------------------------------------------------------------------
# B1b：CODE_REV 递增（否则「重载是否生效」无法验收）
#
# heartbeat.json 里没有独立的 code_rev 键 —— `rev` 就是 CODE_REV
# （qmtide_bridge.py:470/857/887 三处 `"rev": CODE_REV`）。只改代码不 bump
# CODE_REV，重载前后 rev 逐字符相同，「部署 ≠ 生效」这件事就验不出来。
# 值必须是**静态常量**、纯 ASCII：用 now() 之类运行时值会让两次编译产物不同，
# 逐块逆向还原（sha/字节比对）立刻失效。
# ---------------------------------------------------------------------------
B1B_OLD = [
    'CODE_REV = "2026-11-08T21:31-mv-from-positions"',
]
B1B_NEW = [
    'CODE_REV = "2026-11-09T1125-write-retry"',
]

PATCH_SPECS = {
    # 每个目标可以有多个块，每块各自断言「恰好命中 1 次」。
    "b1": {"tag": "B1/qmt_write_retry",
           "blocks": [(B1_OLD, B1_NEW), (B1B_OLD, B1B_NEW)]},
    "b2": {"tag": "B2/engine_read_share",
           "blocks": [(B2_OLD, B2_NEW),
                      (B2_COMMENT_OLD, B2_COMMENT_NEW),
                      (B2_HEARTBEAT_OLD, B2_HEARTBEAT_NEW)]},
}


def _say(msg: str) -> None:
    """只输出 ASCII：运维常在 cp1252 或 gbk 控制台上跑，非 ASCII 会直接抛。"""
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
        d = REPO_ROOT / "_backup" / "patch_bridge_sharing_20261109"
        d.mkdir(parents=True, exist_ok=True)
        return d / (target.name + ".bak-" + stamp)
    return target.with_name(target.name + ".bak-" + stamp)


def build_blocks(which: str, codec: str, nl: bytes):
    """把每个块的「行列表」按目标编码/行尾拼成 [(old_bytes, new_bytes), ...]。

    编码失败必须立刻中止：GBK 装不下 ⇒ / − / ≤ 这类符号，硬写会被替换成 ``?``，
    整份部署版源码的注释就毁了。
    """
    out = []
    for i, (old_lines, new_lines) in enumerate(PATCH_SPECS[which]["blocks"], 1):
        try:
            old = nl.join(ln.encode(codec) for ln in old_lines)
            new = nl.join(ln.encode(codec) for ln in new_lines)
        except UnicodeEncodeError as e:
            raise SystemExit(
                "[ABORT] %s block %d not encodable as %s: %s" % (which, i, codec, e))
        out.append((old, new))
    return out


def _is_rev_block(which: str, idx: int) -> bool:
    """该块是不是「CODE_REV 版本戳」（1-based，与 build_blocks 的下标一致）。

    rev 块与其它块语义不同：其它块是**代码结构**，必须严格幂等；
    rev 是**一次性版本号**，只要文件里已是更新的 rev，就不该再被本补丁改写，
    更不该让「rev 已推进」变成一次 ABORT（2026-11-11 frozen 补丁就撞在这上面）。
    """
    if which != "b1":
        return False
    spec = PATCH_SPECS["b1"]
    return idx <= len(spec["blocks"]) and \
        spec["blocks"][idx - 1] == (B1B_OLD, B1B_NEW)


def transform(raw: bytes, which: str):
    """纯函数：把补丁作用在字节串上。返回 (new_raw, status, codec, nl)。

    status: ``patched`` / ``already``（全部块都已存在）。每个块独立判定：
    已存在就跳过 ⇒ 重复执行幂等；存在一半 ⇒ 只补剩下的，仍在写盘前逐块校验。
    任何「命中次数 ≠1」抛 SystemExit（调用方据此中止，绝不写盘）。测试直接
    调这个函数，因此「补丁脚本自己写错了」会和「补丁逻辑错了」一起被抓住。
    """
    codec = _detect_codec(raw)
    nl = _newline_of(raw)
    cur = raw
    n_patched = 0
    for i, (old, new) in enumerate(build_blocks(which, codec, nl), 1):
        if cur.count(new) == 1:
            continue                       # 该块已打过
        n = cur.count(old)
        if n != 1:
            # 【2026-11-11】CODE_REV 块是**一次性版本戳**：它只能被往前推，
            # 不该成为幂等的障碍。2026-11-11 打 frozen 补丁把 rev 推到
            # `2026-11-11T2005-frozen-in-total` 后，本脚本的 B1B_OLD 就再也
            # 命不中了 —— 那不是"补丁写错"，是版本本来就该继续往前。
            # 因此 rev 块允许「命中 0 次」= 已被后续补丁推进过，跳过即可。
            # （仍禁止命中 >1 次：那说明文件里有两个 CODE_REV，是真故障。）
            if _is_rev_block(which, i) and n == 0:
                continue
            raise SystemExit(
                "[ABORT] %s block %d: old block matched %d times (must be exactly 1)"
                % (which, i, n))
        cur = cur.replace(old, new, 1)
        n_patched += 1
    return cur, ("patched" if n_patched else "already"), codec, nl


def _verify(raw: bytes, new_raw: bytes, which: str) -> list:
    """回读自校验：用**逆向还原**证明「只换了这些块」，返回错误列表（空=通过）。

    为什么不用 difflib 数 hunk：新块的 ``try:`` / ``except Exception:`` 等行
    与旧块内部若干行文本相同，SequenceMatcher 会把它切成 2~3 个片段，于是
    「期望 1 个 hunk」的断言误报（本脚本首版就踩了）。改为：
    把新块逐个换回旧块后，字节串必须与原文**完全相等** —— 这比逐行等价更强，
    连行尾、编码、缩进都不可能被悄悄改动。
    """
    errs = []
    codec = _detect_codec(raw)
    nl = _newline_of(raw)
    blocks = build_blocks(which, codec, nl)
    cur = new_raw
    for i, (old, new) in enumerate(reversed(blocks), 1):
        if cur.count(new) != 1:
            # rev 块可能已被更新的版本戳取代（见 transform 里的说明），
            # 反向还原时同样跳过，不算校验失败。
            if _is_rev_block(which, len(blocks) - i + 1):
                continue
            errs.append("new block (from the end, #%d) must appear exactly 1 time" % i)
            continue
        cur = cur.replace(new, old, 1)
    if cur != raw:
        errs.append("inverse check failed: swapping the new blocks back does not "
                    "reproduce the original bytes")
    old_text = raw.decode(codec, "replace")
    new_text = new_raw.decode(codec, "replace")
    sm = difflib.SequenceMatcher(a=old_text.splitlines(), b=new_text.splitlines(),
                                 autojunk=False)
    changed = [op for op in sm.get_opcodes() if op[0] != "equal"]
    _say("[CHECK] blocks=%d changed line-hunks (report only): %d"
         % (len(blocks), len(changed)))
    return errs


def _compile_gate(raw: bytes, codec: str, target: Path) -> str:
    """第 4 道断言：补丁后的源码必须能 ``compile()``。返回 "" = 通过。

    为什么必须有这道：语法错的文件一旦落盘，**QMT 下次 init 直接加载失败**，
    等于盘中把桥打断。所以在写盘**之前**（内存里的字节串）与写盘**之后**
    （从磁盘读回的真实文件）各跑一次 —— 后者不是重复劳动，它防的是「写的
    和想写的不是一个东西」（编码/行尾/截断）。
    """
    try:
        compile(raw.decode(codec), str(target), "exec")
        return ""
    except Exception as e:
        return "%s: %s" % (type(e).__name__, e)


def patch_one(which: str, target: Path, apply: bool) -> int:
    """返回 0=成功/可写，2=中止。"""
    tag = PATCH_SPECS[which]["tag"]
    if not target.exists():
        _say("[SKIP] %s: target not found: %s" % (tag, target))
        return 0
    raw = target.read_bytes()
    _say("[INFO] %s" % target)
    try:
        new_raw, status, codec, nl = transform(raw, which)
    except SystemExit as e:
        _say(str(e))
        _say("[ABORT] %s: nothing written" % tag)
        return 2
    _say("[INFO] size=%d codec=%s newline=%s hits=1"
         % (len(raw), codec, "CRLF" if nl == b"\r\n" else "LF"))
    if status == "already":
        _say("[SKIP] %s: already patched; nothing written" % tag)
        return 0

    try:
        old_text = raw.decode(codec)
        new_text = new_raw.decode(codec)
    except UnicodeDecodeError as e:
        _say("[ABORT] %s: patched bytes no longer decode as %s: %s" % (tag, codec, e))
        return 2

    errs = _verify(raw, new_raw, which)
    if errs:
        for e in errs:
            _say("[ABORT] %s: %s" % (tag, e))
        _say("[ABORT] %s: self-check failed; nothing written" % tag)
        return 2
    _say("[CHECK] %s lines %d -> %d"
         % (tag, len(old_text.splitlines()), len(new_text.splitlines())))

    # ---- 第 4 道：写盘前先在内存里 compile() ----
    err = _compile_gate(new_raw, codec, target)
    if err:
        _say("[ABORT] %s: compile() failed on the patched source: %s" % (tag, err))
        _say("[ABORT] %s: nothing written" % tag)
        return 2
    _say("[CHECK] compile() OK (in-memory) bytes %d -> %d" % (len(raw), len(new_raw)))

    if not apply:
        _say("[DRY-RUN] %s: no write. re-run with --apply" % tag)
        return 0

    stamp = time.strftime("%Y%m%d-%H%M")
    bak = _backup_path(target, stamp)
    try:
        shutil.copy2(str(target), str(bak))
    except Exception as e:
        _say("[ABORT] %s: backup failed (%s); nothing written" % (tag, e))
        return 2
    _say("[BACKUP] %s" % bak)

    try:
        target.write_bytes(new_raw)
    except Exception as e:
        _say("[ABORT] %s: write failed: %s" % (tag, e))
        _say('[ROLLBACK] copy /Y "%s" "%s"' % (bak, target))
        return 2

    back = target.read_bytes()
    if back != new_raw:
        _say("[ABORT] %s: read-back mismatch; restoring backup" % tag)
        shutil.copy2(str(bak), str(target))
        return 2
    # ---- 第 4 道（写盘后）：编译**落盘的真实文件**，不是内存字符串 ----
    err = _compile_gate(back, codec, target)
    if err:
        _say("[ABORT] %s: compile() failed on the file written to disk: %s" % (tag, err))
        shutil.copy2(str(bak), str(target))
        _say("[ROLLBACK] backup restored: %s" % bak)
        return 2
    _say("[CHECK] compile() OK (from disk)")
    _say("[OK] %s written (%d bytes)" % (tag, len(back)))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="patch bridge write-retry(B1) / read-share(B2) (dry-run by default)")
    ap.add_argument("--only", choices=["b1", "b2", "all"], default="all")
    ap.add_argument("--apply", action="store_true",
                    help="actually write (default is dry-run)")
    ap.add_argument("--skip-deployed", action="store_true",
                    help="do not touch C:\\pazq_qmt\\python\\QMTIDE_BRIDGE.py")
    args = ap.parse_args()

    plan = []
    if args.only in ("b1", "all"):
        plan.append(("b1", B1_REPO))
        if not args.skip_deployed:
            plan.append(("b1", B1_DEPLOYED))
    if args.only in ("b2", "all"):
        plan.append(("b2", B2_REPO))

    _say("[MODE] %s" % ("APPLY" if args.apply else "DRY-RUN"))
    rc = 0
    for which, target in plan:
        rc = patch_one(which, target, args.apply)
        if rc != 0:
            break
    if rc == 0 and args.apply:
        _say("[DONE] B1 needs a QMT strategy reload; B2 needs an engine restart.")
    return rc


if __name__ == "__main__":
    sys.exit(main())

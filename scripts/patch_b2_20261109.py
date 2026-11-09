# -*- coding: utf-8 -*-
"""B2 字节级补丁器：读侧共享位 + 心跳窗 6→12 + 两条 P1 日志文案。

默认 **dry-run**，`--apply` 才写盘。`--root` 可用于在**副本**上预演（盘中唯一允许的方式）。

四道写盘断言（与 B1 同一套）：
  1. 每块 ASCII 子串定位 `hits == 1`（已打过则跳过，支持幂等）
  2. 逐块**逆向还原**后字节串必须等于原文
  3. 编码/行尾按实测（broker_bridge = utf-8/LF，event_engine = utf-8/CRLF）
  4. `compile()`：写盘前编内存串，写盘后编**落盘真实文件**

为什么要 `--root`：`engine/` 与 `core/` 是运行中引擎要 import 的。guardian 可能在盘中把
引擎拉起来（`health_check.py:288` 无参拉起路径），若我们正改到一半，**半成品代码直接上
实盘**。所以盘中只许在副本上 apply，生产文件等收盘后发令。

用法：
    python scripts/patch_b2_20261109.py                                   # 生产文件 dry-run
    python scripts/patch_b2_20261109.py --apply --root logs/_tmp/b2rehearsal
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- 块 1：共享位
B1_OLD = [
    'def _read_json(path: str):',
    '    try:',
    '        with open(path, "r", encoding="utf-8") as f:',
    '            return json.load(f)',
    '    except Exception:',
    '        return None',
]

B1_NEW = [
    '_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004   # READ | WRITE | DELETE',
    '_GENERIC_READ = 0x80000000',
    '_OPEN_EXISTING = 3',
    '_FILE_ATTRIBUTE_NORMAL = 0x80',
    '',
    '',
    'def _open_share_delete(path: str):',
    '    """以 FILE_SHARE_READ|WRITE|DELETE 打开文件（非 Windows 直接回落 open）。',
    '',
    '    Windows 上 Python 的 ``open()`` 只申请 READ|WRITE、**不含 DELETE**，于是桥侧',
    '    ``os.replace(tmp, path)`` 在「我们正持有句柄」的那几十微秒里必然失败',
    '    （WinError 5）—— 这是 2026-11-09 那 4 次 ``WinError 183`` 的另一半（次生链的',
    '    起点）。给它 DELETE 位，写侧就能把文件换掉。',
    '',
    '    **失败一律回落普通 ``open``**：读不到 result.json 比共享位失败严重得多',
    '    （那会把「明确拒单」误判成「超时无回执」）。回落**不打日志**：本函数每 3 秒',
    '    被调一次，刷屏会淹没真信号。',
    '    """',
    '    if os.name != "nt":',
    '        return open(path, "r", encoding="utf-8")',
    '    try:',
    '        import ctypes',
    '        import msvcrt',
    '        from ctypes import wintypes',
    '        k32 = ctypes.WinDLL("kernel32", use_last_error=True)',
    '        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,',
    '                                    wintypes.DWORD, wintypes.LPVOID,',
    '                                    wintypes.DWORD, wintypes.DWORD,',
    '                                    wintypes.HANDLE]',
    '        k32.CreateFileW.restype = ctypes.c_void_p',
    '        h = k32.CreateFileW(path, _GENERIC_READ, _SHARE_ALL, None,',
    '                            _OPEN_EXISTING, _FILE_ATTRIBUTE_NORMAL, None)',
    '        if not h or h == ctypes.c_void_p(-1).value:',
    '            raise OSError("CreateFileW failed")',
    '        fd = msvcrt.open_osfhandle(h, os.O_RDONLY)',
    '        try:',
    '            return os.fdopen(fd, "r", encoding="utf-8")',
    '        except Exception:',
    '            os.close(fd)',
    '            raise',
    '    except Exception:',
    '        # 共享打开失败 ⇒ 回落普通 open（仍然能读到内容，只是不带 DELETE 位）',
    '        return open(path, "r", encoding="utf-8")',
    '',
    '',
    'def _read_json(path: str):',
    '    f = None',
    '    try:',
    '        f = _open_share_delete(path)',
    '        return json.load(f)',
    '    except Exception:',
    '        return None',
    '    finally:',
    '        if f is not None:',
    '            try:',
    '                f.close()',
    '            except Exception:',
    '                pass',
]

# ---------------------------------------------------------- 块 2：过时结论更正
B2_OLD = [
    '    # 2026-11-08 实测 8 次请求里 1 次触发；根因已在 qmtide_bridge 侧一并修掉',
    '    # （ctypes 读改为 FILE_SHARE_READ|WRITE|DELETE），此处是纵深防御。',
]

B2_NEW = [
    '    # 2026-11-08 实测 8 次请求里 1 次触发。2026-11-09 复测**更正**：给足',
    '    # FILE_SHARE_DELETE 也**不能**让 os.replace 在目标被任何进程持句柄时成功，',
    '    # 真正的免疫就是这里的重试；读侧共享位（见 _open_share_delete）只用来堵住',
    '    # 「os.remove 被拒 → 回退 os.rename 撞上仍存在的目标」那条次生链。',
]

# ------------------------------------------------------------ 块 3：心跳窗 6→12
B3_OLD = [
    'HEARTBEAT_STALE_SEC = 6.0       # 心跳超过此值视为 QMT 侧已死',
]

B3_NEW = [
    '# 【2026-11-09】6.0 → 12.0：心跳是桥每 500ms 刷一次，但**写心跳本身也会撞 183**。',
    '#   6 秒窗等于「连续 12 次写失败」才判过期，而实测 183 是阵发的（单次间隔可达',
    '#   49 分钟）⇒ 6 秒窗会把「一次写失败 + 一次读延迟」直接判成断线，代价是 10 秒',
    '#   查询盲区 + 重新订阅 38 只。12 秒给两次读的机会，不削弱「真死」的判定能力。',
    'HEARTBEAT_STALE_SEC = 12.0      # 心跳超过此值视为 QMT 侧已死（原 6.0，理由见上）',
]

# ------------------------------------------------- 块 4：engine_state 无行的静默
E1_OLD = [
    '            row = self.storage.load_engine_state()',
    '            if not row:',
    '                return',
]

E1_NEW = [
    '            row = self.storage.load_engine_state()',
    '            if not row:',
    '                # 【2026-11-09 P1】本函数唯一的**真静默**路径：没有 engine_state 行就',
    '                #   原样返回，调用方看不出「恢复失败」与「本就该空白启动」的区别。',
    '                #   代价不是报错，而是**风控四字段全部失真**（静默回落初始值）。',
    '                logger.warning(',
    '                    "engine_state 无行 ⇒ 从初始状态启动：peak_asset / consec_loss / "',
    '                    "halted / daily_trade_count 四个风控字段全部失真")',
    '                return',
]

# ---------------------------------------------------- 块 5：恢复失败措辞（非正常）
E2_OLD = [
    '            logger.warning("恢复引擎状态失败（从初始状态启动）: %s", e)',
]

E2_NEW = [
    '            # 【2026-11-09 P1】原措辞「失败（从初始状态启动）」读起来像正常启动。',
    '            logger.warning(',
    '                "恢复引擎状态**失败**，已从初始状态启动（风控四字段失真："',
    '                "peak_asset / consec_loss / halted / daily_trade_count）: %s", e)',
]

TARGETS = [
    {"rel": "core/broker_bridge.py", "tag": "B2/read_share",
     "blocks": [(B1_OLD, B1_NEW), (B2_OLD, B2_NEW), (B3_OLD, B3_NEW)]},
    {"rel": "engine/event_engine.py", "tag": "B2/p1_logs",
     "blocks": [(E1_OLD, E1_NEW), (E2_OLD, E2_NEW)]},
]


def say(msg=""):
    sys.stdout.write(str(msg) + "\n")
    sys.stdout.flush()


def _detect(raw: bytes):
    codec = "utf-8"
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        codec = "gbk"
    nl = "\r\n" if b"\r\n" in raw else "\n"
    return codec, nl


def _compile_gate(raw: bytes, codec: str, target: Path) -> str:
    try:
        compile(raw.decode(codec), str(target), "exec")
        return ""
    except Exception as e:
        return "%s: %s" % (type(e).__name__, e)


def transform(text: str, nl: str, blocks, tag: str):
    """逐块替换。返回 (新文本, 每块状态)。已打过 → skipped。"""
    cur = text
    states = []
    for i, (old_lines, new_lines) in enumerate(blocks, 1):
        old_s = nl.join(old_lines)
        new_s = nl.join(new_lines)
        if new_s in cur:
            states.append("block%d already-patched" % i)
            continue
        cnt = cur.count(old_s)
        if cnt != 1:
            raise SystemExit("[ABORT] %s block%d 命中 %d 次（要求恰好 1）" % (tag, i, cnt))
        cur = cur.replace(old_s, new_s, 1)
        states.append("block%d replaced" % i)
    return cur, states


def inverse_verify(new_text: str, nl: str, blocks) -> str:
    """倒序把 new 换回 old；调用方随后必须拿结果与原文字节比对。"""
    cur = new_text
    for i, (old_lines, new_lines) in enumerate(reversed(list(blocks)), 1):
        new_s = nl.join(new_lines)
        old_s = nl.join(old_lines)
        if new_s not in cur:
            return "inverse: 第 %d 个待还原块找不到新块" % i
        cur = cur.replace(new_s, old_s, 1)
    return ""


def patch_one(root: Path, spec: dict, apply: bool) -> int:
    target = root / spec["rel"]
    tag = spec["tag"]
    if not target.exists():
        say("[ABORT] 缺目标文件: %s" % target)
        return 2
    raw = target.read_bytes()
    codec, nl = _detect(raw)
    text = raw.decode(codec)
    say("[INFO] %s" % target)
    say("[INFO] size=%d codec=%s newline=%s blocks=%d"
        % (len(raw), codec, "CRLF" if nl == "\r\n" else "LF", len(spec["blocks"])))

    try:
        new_text, states = transform(text, nl, spec["blocks"], tag)
    except SystemExit as e:
        say(str(e))
        return 2
    for s in states:
        say("[CHECK] %s" % s)

    if new_text == text:
        say("[SKIP] %s: 全部块已打过，无写盘" % tag)
        return 0

    # 断言 2：逆向还原 —— 倒序把 new 换回 old 后，必须逐字节等于原文
    replaced = [b for i, b in enumerate(spec["blocks"])
                if states[i] == "block%d replaced" % (i + 1)]
    err = inverse_verify(new_text, nl, replaced)
    if not err:
        back = inverse_verify(new_text, nl, replaced)
        restored = new_text
        for old_lines, new_lines in reversed(replaced):
            restored = restored.replace(nl.join(new_lines), nl.join(old_lines), 1)
        if restored != text:
            err = "逆序还原后与原文字节不同"
    if err:
        say("[ABORT] 逆向还原自校验失败: %s" % err)
        return 2
    say("[CHECK] 逆向还原自校验通过")

    # 断言 4a：写盘前编译内存串
    new_raw = new_text.encode(codec)
    err = _compile_gate(new_raw, codec, target)
    if err:
        say("[ABORT] compile() 失败（in-memory）: %s" % err)
        return 2
    say("[CHECK] compile() OK (in-memory) bytes %d -> %d" % (len(raw), len(new_raw)))

    if not apply:
        say("[DRY-RUN] %s: 未写盘（加 --apply 才写）" % tag)
        return 0

    # 备份（copy2 会保留 mtime，故 find_backup 一律按文件名时间戳判，不按 mtime）
    ts = time.strftime("%Y%m%d-%H%M%S")
    bak = target.with_name(target.name + ".bak-" + ts)
    shutil.copy2(str(target), str(bak))
    say("[BACKUP] %s" % bak)

    target.write_bytes(new_raw)

    # 断言 4b：编译**落盘真实文件**
    err = _compile_gate(target.read_bytes(), codec, target)
    if err:
        shutil.copy2(str(bak), str(target))
        say("[ROLLBACK] 落盘后 compile 失败，已用备份还原: %s" % err)
        return 2
    say("[CHECK] compile() OK (from disk)")
    say("[OK] %s written (%d bytes)" % (tag, len(new_raw)))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="B2 patcher (default dry-run)")
    ap.add_argument("--apply", action="store_true", help="真正写盘（默认 dry-run）")
    ap.add_argument("--root", default=str(REPO_ROOT),
                    help="目标根目录（预演时指向副本，默认仓库根）")
    args = ap.parse_args()
    root = Path(args.root).resolve()
    say("[MODE] %s   root=%s" % ("APPLY" if args.apply else "DRY-RUN", root))
    rc = 0
    for spec in TARGETS:
        rc = patch_one(root, spec, args.apply) or rc
    say("[DONE] B2 需重启引擎才生效（core/ 与 engine/ 都会被 import）")
    return rc


if __name__ == "__main__":
    sys.exit(main())

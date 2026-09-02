# -*- coding: utf-8 -*-
"""
自建测试 runner（不依赖 pytest，离线可用）

用法（在 conda env qmt 中）：
  python tests/run_all.py
"""
from __future__ import annotations

import importlib
import inspect
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Windows 控制台 UTF-8（与 main.py 一致）。不加这段时，cp1252 控制台上
# 输出 ✓ / 中文会抛 UnicodeEncodeError，使整个 runner 在打印阶段就崩。
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ============================================================ 测试隔离
# 必须在**任何项目模块 import 之前**把数据库 / 系统提示日志重定向到临时目录。
#
# 为什么：原来单测直接写生产 ``storage/qmt.db`` 与 ``logs/notices.log``，把测试桩
# 注入了真实交易记录（生产 notices.log 里实测存在大量「理由=test」的买卖委托、
# 以及 18:19 的假熔断）。这些脏数据又反过来逆向驱动出了
# ``review_daily._in_trading_window()`` 那个“只统计 9—15 点熔断”的补丁——
# 在给症状打补丁，而不是治病。现在从源头隔离。
_TMP = Path(tempfile.mkdtemp(prefix="qmt_tests_"))
os.environ.setdefault("QMT_DB", str(_TMP / "test_qmt.db"))
os.environ.setdefault("QMT_NOTICES_LOG", str(_TMP / "test_notices.log"))
os.environ.setdefault("QMT_IGNORE_INTERPRETER_CHECK", "1")


def _collect_tests():
    mods = ["tests.test_indicators", "tests.test_data_models",
            "tests.test_risk", "tests.test_strategy",
            "tests.test_storage", "tests.test_ai_client",
            "tests.test_event_engine_live", "tests.test_review_daily",
            "tests.test_stock_names", "tests.test_mode_tagging",
            "tests.test_batch_a_fixes", "tests.test_batch_c_fixes",
            "tests.test_batch_d_fixes",
            "tests.test_batch_b_t1",
            "tests.test_batch_b_minute",
            "tests.test_batch_e_daily_decision"]
    tests = []
    import_errors = []
    for m in mods:
        try:
            mod = importlib.import_module(m)
        except Exception as e:
            # 不能静默跳过：模块 import 失败意味着整文件的用例全部**隐形未执行**，
            # 而旧实现只打一行 [skip] 就继续、最后仍报 PASSED / 退出码 0。
            # 实测踩过：改动 storage.db 后两个测试模块静默消失，却显示全绿。
            import_errors.append((m, f"{type(e).__name__}: {e}"))
            print(f"[IMPORT FAIL] {m}: {type(e).__name__}: {e}")
            continue
        for name, fn in inspect.getmembers(mod, inspect.isfunction):
            if name.startswith("test_") and fn.__module__ == m:
                tests.append((m, name, fn))
    return tests, import_errors


def main() -> int:
    print(f"[隔离] QMT_DB          = {os.environ['QMT_DB']}")
    print(f"[隔离] QMT_NOTICES_LOG = {os.environ['QMT_NOTICES_LOG']}\n")
    tests, import_errors = _collect_tests()
    if not tests:
        print("no tests found")
        return 1
    passed = failed = 0
    errors = []
    t0 = time.time()
    for mod, name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  ✓ {mod}.{name}")
        except Exception:
            failed += 1
            err = traceback.format_exc()
            errors.append((mod, name, err))
            print(f"  ✗ {mod}.{name}")
    dt = time.time() - t0
    print(f"\n{'=' * 60}")
    print(f"PASSED: {passed}  FAILED: {failed}  TOTAL: {passed + failed}  "
          f"({dt:.2f}s)")
    if import_errors:
        print(f"\n⚠ IMPORT FAILURES: {len(import_errors)} 个测试模块未能加载（其用例全部未执行）：")
        for m, err in import_errors:
            print(f"    {m}: {err}")
    if errors:
        print("\n失败详情：")
        for mod, name, err in errors:
            print(f"\n--- {mod}.{name} ---")
            print(err)
    return 1 if (errors or import_errors) else 0


def _cleanup() -> None:
    """清理临时隔离目录（失败也不影响退出码）。"""
    try:
        shutil.rmtree(_TMP, ignore_errors=True)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        _rc = main()
    finally:
        _cleanup()
    sys.exit(_rc)
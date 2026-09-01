# -*- coding: utf-8 -*-
"""轻量测试运行器：绕过 pytest 的插件加载（qmt 环境 anyio/ssl 冲突），
直接导入 tests/test_*.py 并执行其中的 test_* 函数。"""
import importlib
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  (确保包可导入)

passed = failed = 0
failed_names = []

for p in sorted(Path("tests").glob("test_*.py")):
    mod_name = f"tests.{p.stem}"
    try:
        mod = importlib.import_module(mod_name)
    except Exception as e:
        print(f"IMPORT ERROR {mod_name}: {e}")
        failed += 1
        failed_names.append(mod_name)
        continue
    funcs = [getattr(mod, n) for n in dir(mod)
             if n.startswith("test_") and callable(getattr(mod, n))]
    for f in funcs:
        try:
            f()
            passed += 1
        except Exception as e:
            failed += 1
            failed_names.append(f"{mod_name}.{f.__name__}")
            print(f"  FAIL {f.__name__}: {type(e).__name__}: {e}")

print(f"\n==== {passed} passed, {failed} failed ====")
if failed_names:
    print("FAILED:")
    for n in failed_names:
        print("  -", n)

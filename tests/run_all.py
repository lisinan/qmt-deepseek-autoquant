# -*- coding: utf-8 -*-
"""
自建测试 runner（不依赖 pytest，离线可用）

用法（在 conda env qmt 中）：
  python tests/run_all.py
"""
from __future__ import annotations

import importlib
import inspect
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _collect_tests():
    mods = ["tests.test_indicators", "tests.test_data_models",
            "tests.test_risk", "tests.test_strategy",
            "tests.test_storage", "tests.test_ai_client",
            "tests.test_event_engine_live", "tests.test_review_daily",
            "tests.test_stock_names", "tests.test_mode_tagging"]
    tests = []
    for m in mods:
        try:
            mod = importlib.import_module(m)
        except Exception as e:
            print(f"[skip] {m}: import failed: {e}")
            continue
        for name, fn in inspect.getmembers(mod, inspect.isfunction):
            if name.startswith("test_") and fn.__module__ == m:
                tests.append((m, name, fn))
    return tests


def main() -> int:
    tests = _collect_tests()
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
    if errors:
        print("\n失败详情：")
        for mod, name, err in errors:
            print(f"\n--- {mod}.{name} ---")
            print(err)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
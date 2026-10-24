# -*- coding: utf-8 -*-
"""【WP-M 2026-10-22】测试隔离守卫：断言 run_all 环境绝不写生产 DB。

背景：tests/ 下的夹具曾直接用真实 Storage（未重定向 QMT_DB），把伪造成交
（price=100.0 的 300502/300394/300308/600519）写进了生产 storage/qmt.db。
run_all.py 现已在导入任何项目模块**之前**把 QMT_DB / QMT_NOTICES_LOG 指到
临时目录；本守卫锁死这条纪律，防回归：

  1) 模拟 run_all 的环境设置方式（进程内先设 env 再 import）；
  2) 断言 Storage 实际路径 != 生产 storage/qmt.db（且确在临时目录下）；
  3) 断言测试写入只落在临时库 —— 生产库 fills 行数前后不变；
  4) 断言 run_all.py 顶部确有「导入前设 QMT_DB」的隔离代码（源码级防回归）。
"""
from __future__ import annotations

import importlib
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROD_DB = ROOT / "storage" / "qmt.db"


def _with_qmt_db(tmp_db: Path):
    """设置 QMT_DB 并确保结束后**恢复原值**（run_all 进程内已有全局隔离值，
    直接 pop 会破坏后续测试的隔离，见 run_all.py 顶部纪律）。"""
    import contextlib

    @contextlib.contextmanager
    def _cm():
        old = os.environ.get("QMT_DB")
        os.environ["QMT_DB"] = str(tmp_db)
        try:
            yield
        finally:
            if old is None:
                os.environ.pop("QMT_DB", None)
            else:
                os.environ["QMT_DB"] = old
    return _cm()


def test_run_all_sets_qmt_db_before_import():
    """run_all.py 必须在 import 项目模块之前设置 QMT_DB（源码级检查）。"""
    src = (ROOT / "tests" / "run_all.py").read_text(encoding="utf-8")
    # 隔离代码必须在任何项目模块 import 之前：找第一个 importlib.import_module
    # 或 import tests. 的位置，QMT_DB 设置必须出现在它之前。
    set_pos = src.find('os.environ.setdefault("QMT_DB"')
    assert set_pos > 0, "run_all.py 缺少 QMT_DB 环境变量设置（测试隔离被移除？）"
    # 第一个项目模块导入点（_collect_tests 内的 importlib.import_module 调用）
    imp_pos = src.find("importlib.import_module(m)")
    assert imp_pos > set_pos, (
        "run_all.py 的 QMT_DB 设置必须位于任何项目模块 import 之前")
    # 纪律注释必须在文件里
    assert "测试绝不允许写生产" in src or "生产" in src, (
        "run_all.py 顶部应写明「测试绝不允许写生产 DB/日志」纪律")


def test_storage_respects_qmt_db_env():
    """设 QMT_DB 后 Storage 实际路径必须离开生产库、落在临时目录。"""
    with tempfile.TemporaryDirectory(prefix="qmt_iso_") as td:
        tmp_db = Path(td) / "test_qmt.db"
        with _with_qmt_db(tmp_db):
            # 动态 import：确保 Storage 在 env 设置之后才读路径
            storage_mod = importlib.import_module("storage.db")
            importlib.reload(storage_mod)
            from storage.db import Storage, default_db_path

            p = default_db_path()
            assert p != PROD_DB, (
                f"QMT_DB 已设为 {tmp_db}，default_db_path 却返回 {p} —— "
                "环境变量覆盖失效，测试会写生产库！")
            assert p == tmp_db, f"期望 {tmp_db}，实际 {p}"

            s = Storage()
            try:
                assert s.db_path != PROD_DB, (
                    f"Storage 实际路径是生产库 {s.db_path}！")
                assert Path(s.db_path) == tmp_db, (
                    f"Storage 应落在临时库 {tmp_db}，实际 {s.db_path}")
                # 写入必须只落在临时库
                from core.data_models import Fill
                s.save_fill(
                    Fill(ts=datetime(2026, 10, 22, 12, 0), code="000001.SZ",
                         side="BUY", quantity=100, price=11.11,
                         amount=1111.0, account="cash"),
                    order_id="wp-m-guard", mode="paper")
                n = sqlite3.connect(str(tmp_db)).execute(
                    "SELECT COUNT(*) FROM fills WHERE order_id='wp-m-guard'"
                ).fetchone()[0]
                assert n == 1, "测试写入未落在临时库"
            finally:
                s.close()


def test_production_db_untouched_by_test_writes():
    """测试写入期间生产库 fills 行数必须不变（即使 QMT_DB 忘了设也不会命中）。"""
    if not PROD_DB.exists():
        return  # 生产库不在（如全新 clone）时跳过
    before = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True).execute(
        "SELECT COUNT(*) FROM fills").fetchone()[0]
    with tempfile.TemporaryDirectory(prefix="qmt_iso2_") as td:
        with _with_qmt_db(Path(td) / "t.db"):
            storage_mod = importlib.import_module("storage.db")
            importlib.reload(storage_mod)
            from storage.db import Storage
            from core.data_models import Fill
            s = Storage()
            try:
                for i in range(3):
                    s.save_fill(
                        Fill(ts=datetime(2026, 10, 22, 12, i), code="000001.SZ",
                             side="BUY", quantity=1, price=1.0,
                             amount=1.0, account="cash"),
                        order_id="wp-m-guard", mode="paper")
            finally:
                s.close()
    after = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True).execute(
        "SELECT COUNT(*) FROM fills").fetchone()[0]
    assert before == after, (
        f"生产库 fills 行数变了：{before} -> {after}，测试隔离被破坏！")


if __name__ == "__main__":
    test_run_all_sets_qmt_db_before_import()
    print("✓ test_run_all_sets_qmt_db_before_import")
    test_storage_respects_qmt_db_env()
    print("✓ test_storage_respects_qmt_db_env")
    test_production_db_untouched_by_test_writes()
    print("✓ test_production_db_untouched_by_test_writes")
    print("ALL PASSED")

# -*- coding: utf-8 -*-
"""桥心跳体检的语义守卫（2026-11-09 大 QMT + bridge 方案）。

为什么要有守卫：`startup_all.bat` 用 `bridge_health.py --max-age 60` 的退出码
决定「QMT 是否就绪」。如果哪天有人把阈值放宽、或把「文件不存在」判成 OK，
自启就会在一次真的 QMT 未启动时静默放行 —— 那种失败在开盘前是不可见的。

★ 本仓测试**不能用任何 pytest fixture**（含 tmp_path / monkeypatch）：
  tests/run_all.py 是自研 runner，直接 `fn()` 无参调用，带形参一律 TypeError。
  这里改用 tempfile + 手工存取环境变量。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import bridge_health as bh  # noqa: E402

_ENV_KEY = "QMT_BRIDGE_HB"


class _TmpEnv(object):
    """临时目录 + QMT_BRIDGE_HB 覆盖，退出时完整还原。"""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="bh_")
        self.old = os.environ.get(_ENV_KEY)
        return self

    def write(self, payload, name="heartbeat.json"):
        p = os.path.join(self.dir, name)
        io.open(p, "w", encoding="utf-8").write(json.dumps(payload))
        os.environ[_ENV_KEY] = p
        return p

    def use(self, path):
        os.environ[_ENV_KEY] = path
        return path

    def __exit__(self, *a):
        if self.old is None:
            os.environ.pop(_ENV_KEY, None)
        else:
            os.environ[_ENV_KEY] = self.old
        shutil.rmtree(self.dir, ignore_errors=True)
        return False


def test_fresh_heartbeat_is_ok():
    with _TmpEnv() as t:
        t.write({"ts": time.time() - 1.0, "rev": "X", "pid": 1})
        r = bh.check(max_age=60)
    assert r["ok"] is True, r
    assert r["age"] < 5 and r["rev"] == "X"


def test_stale_heartbeat_is_not_ok():
    with _TmpEnv() as t:
        t.write({"ts": time.time() - 3600, "rev": "X", "pid": 1})
        r = bh.check(max_age=60)
    assert r["ok"] is False and "未更新" in r["reason"], r


def test_missing_file_is_not_ok():
    with _TmpEnv() as t:
        t.use(os.path.join(t.dir, "nope.json"))
        r = bh.check(max_age=60)
    assert r["ok"] is False and "不存在" in r["reason"], r


def test_bad_ts_is_not_ok():
    with _TmpEnv() as t:
        t.write({"rev": "X"})                    # 无 ts
        r = bh.check(max_age=60)
    assert r["ok"] is False, r


def test_cli_exit_codes():
    with _TmpEnv() as t:
        t.write({"ts": time.time(), "rev": "R", "pid": 7})
        ok = bh.main([])
        t.use(os.path.join(t.dir, "none.json"))
        bad = bh.main([])
    assert (ok, bad) == (0, 1), (ok, bad)

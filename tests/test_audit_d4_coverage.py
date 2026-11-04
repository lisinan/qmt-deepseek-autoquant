# -*- coding: utf-8 -*-
"""d4（xtdata 日线末端新鲜度体检）的覆盖率守卫。

背景（2026-11-04，data-ledger-auditor-2 发现、team-lead 复核定值 38）
--------------------------------------------------------------------
`scripts/_audit_20261103_d1_d4.py` 的 d4 段有**两个**独立缺陷，叠加后
「体检全绿但其实什么都没看」：

  1. `from xtdata import get_local_data` —— ``xtdata`` 顶层模块不存在
     （正确入口是 ``xtquant.xtdata``）⇒ d4 **在 import 那行直接抛异常中止**，
     从来没有任何输出。标题却写着「38 只订阅标的」。
  2. 代码集用 ``STOCK_CODES``(12) + ``VALIDATION_UNIVERSE``(1) + 一个不存在的
     ``DYNAMIC_UNIVERSE_CODES``（被 ``except: dyn=[]`` 吞掉）⇒ 即使 import 修好，
     也只覆盖 **13 只**，而引擎实订阅 **38 只**（UNIVERSE 16 + VALIDATION 1 +
     active_pool 30 + 持仓 2，去重）。

    ★ 38 是**订阅只数**；34 是日志里「补拉 34 只」（38−34=4 只本就新鲜）。
      把补拉数当订阅数读，正是本缺陷被误判成「只差几只」的原因。

本文件钉住三条：
  1. 订阅清单必须**委托引擎自己的** ``EventEngine._subscribe_codes``（单一事实源），
     不得在本脚本里另写一套；
  2. 覆盖集合必须包含 ``UNIVERSE`` 与动态池 ``active_pool``（旧实现两者都漏）；
  3. 断言锚点取自引擎日志的「共 N 只」，而不是在脚本里写死数字。
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config.settings  # noqa: E402,F401  （须先于 sqlite3：DLL 路径）


def _mod():
    p = ROOT / "scripts" / "_audit_20261103_d1_d4.py"
    spec = importlib.util.spec_from_file_location("_audit_d1d4", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_d4_delegates_to_engine_subscription_source():
    src = inspect.getsource(_mod().d4) + inspect.getsource(_mod()._subscription_codes)
    assert "_subscribe_codes" in src, (
        "d4 必须委托 EventEngine._subscribe_codes —— 另写一套拼接逻辑必然漂移")
    # 只查 import 语句，不查注释/docstring（缺陷历史需要写在文档里）
    assert "import STOCK_CODES" not in src, (
        "STOCK_CODES(12) 是候选池的静态子集，不是订阅清单（订阅用 UNIVERSE(16)）")
    assert "import DYNAMIC_UNIVERSE_CODES" not in src, (
        "该名字在 config.settings 里不存在，曾被 except 吞成 [] 而静默漏检 30 只")


def test_d4_uses_correct_xtdata_entrypoint():
    src = inspect.getsource(_mod())
    assert "from xtquant import xtdata" in src, \
        "xtdata 顶层模块不存在（ModuleNotFoundError），必须走 xtquant"
    assert "from xtdata import" not in src, "旧的错误 import 不得复活"


def test_subscription_codes_cover_universe_and_active_pool():
    m = _mod()
    codes, parts = m._subscription_codes()
    from config.settings import UNIVERSE
    s = set(codes)
    assert set(UNIVERSE) <= s, "订阅清单必须包含 UNIVERSE（16 只）"
    jf = ROOT / "storage" / "dynamic_universe.json"
    if jf.exists():
        pool = set(json.loads(jf.read_text(encoding="utf-8")).get("active_pool") or [])
        assert pool <= s, f"动态池 {len(pool)} 只必须全部纳入（旧实现整块漏检）"
    assert len(s) == len(codes), "订阅清单不得有重复代码"
    assert len(codes) >= 16, f"覆盖只数明显偏小：{len(codes)}"


def test_log_anchor_is_parsed_not_hardcoded():
    """锚点必须来自引擎日志，不能在脚本里写死 38（宇宙一变就会静默过/静默炸）。"""
    m = _mod()
    n = m._log_subscription_count()
    assert n is None or isinstance(n, int)
    if n is not None:
        assert n > 0
        assert n == len(m._subscription_codes()[0]), (
            f"脚本实算 {len(m._subscription_codes()[0])} 只 != 日志锚点 {n} 只"
            f" —— 覆盖率已不正确")

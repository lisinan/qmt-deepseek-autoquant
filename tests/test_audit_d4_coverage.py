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


# ---------------------------------------------------------------- 退出码契约

def test_d4_skip_exit_code_is_3_and_coverage_line_stays_visible(monkeypatch=None):
    """xtdata 不可达 ⇒ 必须 exit 3 + 明确打印 `0 / N（未执行）`，且绝不 exit 0。

    契约（2026-11-04）：0=跑了且干净；2=跑了但结论不可用；3=**没跑**；1=工具抛异常。
    为什么 `0 / 38（未执行）` 这行必须存在：一行的**缺席**比一个 `0` 更容易被当成
    健康 —— 人眼扫摘要时 `0 / 38` 很刺眼，没有这一行则什么都不会发现。
    """
    import contextlib
    import io

    m = _mod()
    m._fetch_daily = lambda *a, **k: (False, "mock: xtdata 不可达")
    buf = io.StringIO()
    code = None
    try:
        with contextlib.redirect_stdout(buf):
            m.d4()
    except SystemExit as e:
        code = e.code
    else:
        raise AssertionError("xtdata 不可达时必须 SystemExit，不得静默通过（exit 0）")
    assert code == 3, f"未执行必须是 3（实际 {code}）；0 会被读成健康"
    out = buf.getvalue()
    assert "[SKIP]" in out, out[-400:]
    assert "未执行" in out
    assert "覆盖只数: 0 /" in out, "未执行时仍必须打印 0 / N，不能靠『这行消失』表达"


def test_d4_coverage_mismatch_is_not_exit_0():
    """订阅只数与日志锚点不符 ⇒ exit 2（跑了但结论不可用），不是 0、也不是 3。"""
    import contextlib
    import io

    m = _mod()
    real = m._subscription_codes
    m._probe_xtdata = lambda *a, **k: True          # 不连网
    m._subscription_codes = lambda: (["X.SH"], {"UNIVERSE": 1, "VALIDATION": 0,
                                                "dynamic_active": 0, "positions": 0})
    m._log_subscription_count = lambda: 38
    buf = io.StringIO()
    code = None
    try:
        with contextlib.redirect_stdout(buf):
            m.d4()
    except SystemExit as e:
        code = e.code
    finally:
        m._subscription_codes = real
    assert code == 2, f"覆盖率不符应 exit 2（实际 {code}）"
    assert "[FAIL]" in buf.getvalue()


def test_probe_runs_before_engine_import():
    """行情不可达时，**不得** import engine.event_engine。

    `import engine.event_engine` 自身就会连接 xtdata（模块级 singleton 副作用，
    实测 0.89s）。若先 import 再探活，xtdata 没开就会在 import 行炸成 traceback，
    [SKIP] 分支永远执行不到，且「行情没开 / xtquant 没装 / 代码有错」不可区分。
    本用例钉死顺序：probe=False ⇒ 直接 exit 3，`_subscription_codes` 一次都不许调用。
    """
    import contextlib
    import io

    m = _mod()
    called = {"sub": 0}

    def _boom(*a, **k):
        called["sub"] += 1
        raise AssertionError("行情不可达时不应去 import engine 取订阅清单")

    m._probe_xtdata = lambda *a, **k: False
    m._subscription_codes = _boom
    buf = io.StringIO()
    code = None
    try:
        with contextlib.redirect_stdout(buf):
            m.d4()
    except SystemExit as e:
        code = e.code
    assert code == 3, f"应为 3（没跑），实际 {code}"
    assert called["sub"] == 0, "探活失败后仍调用了 _subscription_codes（顺序反了）"
    out = buf.getvalue()
    assert "[SKIP]" in out and "0 / " in out and "未执行" in out, out[-300:]


def test_fetch_daily_does_not_swallow_ctrl_c():
    """Ctrl-C / SystemExit 必须继续上抛，绝不能变成 `[SKIP] xtdata 不可达`。

    「人把取数掐了」也是「没跑」，但它不是「环境不可达」。混进 exit 3 就又造出
    一个「看起来正常的坏状态」（auditor-2 2026-11-04 指出）。

    实现细节：不能在工作线程内直接 raise —— 那只会杀死线程、box 为空，
    调用方会误判成功；必须在主线程按类型 re-raise。
    """
    import sys
    from types import SimpleNamespace

    m = _mod()

    class _X:
        @staticmethod
        def get_local_data(**kw):
            raise KeyboardInterrupt()

    old = sys.modules.get("xtquant")
    sys.modules["xtquant"] = SimpleNamespace(xtdata=_X)
    try:
        try:
            m._fetch_daily(["000001.SH"], "20260101", "20260102", timeout=5)
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("KeyboardInterrupt 被吞成 SKIP(3)，与『环境不可达』不可区分")
    finally:
        if old is None:
            sys.modules.pop("xtquant", None)
        else:
            sys.modules["xtquant"] = old


def test_skip_message_keeps_exception_type_for_triage():
    """SKIP 文案必须带异常类型 + 「连续 ≥3 次按工具缺陷排查」后手提示。"""
    import sys
    from types import SimpleNamespace

    m = _mod()

    class _X:
        @staticmethod
        def get_local_data(**kw):
            raise ConnectionError("mock refused")

    old = sys.modules.get("xtquant")
    sys.modules["xtquant"] = SimpleNamespace(xtdata=_X)
    try:
        ok, reason = m._fetch_daily(["000001.SH"], "20260101", "20260102", timeout=5)
    finally:
        if old is None:
            sys.modules.pop("xtquant", None)
        else:
            sys.modules["xtquant"] = old
    assert ok is False
    assert "ConnectionError" in reason, reason
    assert "≥3 次" in reason or ">=3 次" in reason, reason

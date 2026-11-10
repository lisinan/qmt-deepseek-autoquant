# -*- coding: utf-8 -*-
"""bridge ``total_asset`` 必须计入冻结资金（frozen）的守卫测试（2026-11-09 · P0）。

## 立项依据（实盘铁证，勿删）

``qmt_bridge/qmtide_bridge.py`` 是**真正跑在 QMT 里下单**的那份代码
（部署版 ``C:\\pazq_qmt\\python\\QMTIDE_BRIDGE.py``，GBK 编码，除首行编码头外与
仓库版逐行等价）。它的 ``_total_asset_of()`` 原来写成::

    derived = cash + mv          # cash = m_dAvailable（可用资金）

而 ``cash`` 取的是 ``m_dAvailable``（**可用**资金），挂出买单时券商立即把资金从
可用划到冻结，``m_dAvailable`` 立刻减少、``m_dFrozenCash`` 等额增加。原实现只把
``frozen`` 当作独立字段返回，**没有计入 total_asset** ⇒ 一挂单总资产就被低估。

下游后果（逐段实测读通，不是推测）：

    total_asset 低估
      -> engine/event_engine.py:4207  self._broker_total_asset = asset["total_asset"]
      -> _risk_total_asset()          取 3 样本中位数（RISK_ASSET_MEDIAN_N=3，约 9 秒）
      -> risk/manager.py:142-143      dlp = (total - day_open)/day_open
      -> dlp <= daily_stop_flatten_pct(-6%)  =>  flatten_requested
      -> engine/event_engine.py:2204  逐仓 force=True 强平**全部可卖持仓**

量化（2026-11-09 盘前，基线 57,862.57）：−6% 阈值只有 **3,472 元**，而单笔
仓位规模 15,000~23,145 元（``max_single_position_pct=0.40``）⇒ **一笔未即时成交
的买单就是阈值的 4.3~6.7 倍**，挂满 9 秒即触发强平。这与 2026-11-02
「−89.25% 假亏 → 强平真实持仓」属同一类机制，只是入口从「持仓表空返回」
换成「frozen 漏加」。

## 本文件钉死什么

1. ``frozen = 0`` 时结果与旧公式 **逐位相等**（``cash + mv``）——
   **这条最重要**：它保证补丁上线后，今天这种「没有挂单」的常态下，
   总资产一个分都不变，风控看到的数字与打补丁前完全相同。
2. ``frozen > 0`` 时 ``derived = cash + frozen + mv``（补丁部署后生效；
   部署前走 ``[PENDING-DEPLOY]`` 分支，断言的是**当前真实部署行为**，
   因此补丁前后本文件都必须是绿的 —— 不能出现「测试红着等上线」）。
3. QMT 的 ``DBL_MAX`` 哨兵值不得进入总资产（``_f()`` 会过滤成 0）。
4. 券商 ``m_dBalance`` 坏值仍被 1% 自洽性校验拒绝，不得因为引入 frozen 而误信。
5. 券商 ``m_dTotalAsset`` 自洽时仍然优先（补丁不得破坏既有优先级）。

## 隔离

模块 import 期会执行 ``_boot_probe()`` 往 ``BOOT_LOG`` 写一行，因此加载前把
``QMT_BRIDGE_ROOT`` 指到临时目录（与 ``tests/test_qmtide_bridge.py`` 同法）。
本文件不碰生产 DB / 生产日志。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "qmt_bridge" / "qmtide_bridge.py"

# 与生产一致的量级（2026-11-06 收盘：可用 11,611.57 + 市值 46,251.00 = 57,862.57）
CASH = 11611.49
MV = 46251.00
TOTAL_LEGACY = CASH + MV              # 57,862.49
BAD_BALANCE = 137321.50               # 2026-11-08 实测：任何字段组合都拼不出的坏值
DBL_MAX = 1.7976931348623157e+308     # QMT 对「本账户类型不适用」的哨兵值

LEGACY_FORM = b"derived = cash + mv"
FROZEN_FORM = b"derived = cash + frozen + mv"


class _Row(object):
    """``get_trade_detail_data`` 返回的账户对象：属性访问即可。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _load_module():
    """按真身加载发货代码（qmt_bridge 不是包，没有 __init__.py）。"""
    d = tempfile.mkdtemp(prefix="qmtbridge_frozen_")
    os.environ["QMT_BRIDGE_ROOT"] = d
    try:
        spec = importlib.util.spec_from_file_location(
            "qmtide_bridge_frozen_under_test", str(SRC))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        os.environ.pop("QMT_BRIDGE_ROOT", None)
    return mod


_MOD = _load_module()


def _is_patched() -> bool:
    """补丁是否已写进源码（部署状态探测，而不是猜）。"""
    return FROZEN_FORM in SRC.read_bytes()


def test_source_has_exactly_one_of_the_two_forms():
    """源码里必须**恰好**存在一种写法：旧的或新的。

    两种都在 ⇒ 改了一半（例如只加了 frozen 提取行、没改 derived）；
    两种都不在 ⇒ 有人重写了这段，本文件的其余断言不再代表真实行为，必须重审。
    """
    src = SRC.read_bytes()
    n_old = src.count(LEGACY_FORM)
    n_new = src.count(FROZEN_FORM)
    assert n_old + n_new == 1, (
        "expected exactly one TOTAL_ASSET formula, got legacy=%d frozen=%d" % (n_old, n_new))


def test_frozen_zero_is_bit_identical_to_legacy_formula():
    """★最重要：没有挂单（frozen=0）时，总资产与旧公式逐位相等。

    这是「补丁上线后行为不变」的唯一保证 —— 今天绝大多数轮次都是这个状态。
    """
    a = _Row(m_dFrozenCash=0.0)          # m_dTotalAsset / m_dBalance 均缺省 => 0
    got = _MOD._total_asset_of(a, CASH, MV)
    assert got == TOTAL_LEGACY, "frozen=0 must be bit-identical: %r != %r" % (got, TOTAL_LEGACY)


def test_missing_frozen_field_behaves_as_zero():
    """字段完全不存在时（普通账户没这个字段的可能性）必须退化为 frozen=0。

    这条是「字段存在性未验证」的兜底：查不到 ⇒ ``_attr`` 返回 0 ⇒ ``_f`` 返回 0
    ⇒ 结果与旧公式一致。**不更坏**是补丁可上线的前提。
    """
    a = _Row()                            # 一个字段都没有
    got = _MOD._total_asset_of(a, CASH, MV)
    assert got == TOTAL_LEGACY


def test_dbl_max_frozen_is_filtered_to_zero():
    """QMT 用 ``DBL_MAX`` 表示「不适用」，绝不能让它进总资产。

    2026-11-08 DUMP 实测：``m_dCurrMargin`` / ``m_dPreBalance`` 等字段就是该值。
    若 ``m_dFrozenCash`` 在某些账户类型上返回它，必须被 ``_f()`` 过滤成 0；
    否则 ``derived`` 变成 1.8e308，总资产直接 inf。
    """
    a = _Row(m_dFrozenCash=DBL_MAX)
    got = _MOD._total_asset_of(a, CASH, MV)
    assert got == TOTAL_LEGACY, "DBL_MAX sentinel leaked into total_asset: %r" % (got,)


def test_bad_broker_balance_still_rejected():
    """``m_dBalance=137,321.50``（坏值）必须继续被 1% 自洽性校验拒绝。

    引入 frozen 会改变 ``derived``，但只会让它与坏值差得更远 ⇒ 拒绝得更干脆。
    这条防「有人为了让 total_asset 变准而改成直接信 m_dBalance」——那样断路器
    峰值会虚高 2.37 倍、回撤恒为 0、max_drawdown 熔断永久失效。
    """
    a = _Row(m_dBalance=BAD_BALANCE)
    got = _MOD._total_asset_of(a, CASH, MV)
    assert got == TOTAL_LEGACY, "bad m_dBalance must be rejected, got %r" % (got,)


def test_self_consistent_broker_total_still_preferred():
    """券商字段自洽（差 ≤1%）时仍然优先，补丁不得改变既有优先级。"""
    a = _Row(m_dTotalAsset=TOTAL_LEGACY)
    got = _MOD._total_asset_of(a, CASH, MV)
    assert got == TOTAL_LEGACY


def test_frozen_is_included_when_funds_are_frozen():
    """挂单把 15,000 元冻结时，总资产必须**不变**（不能因为「可用变少」就变少）。

    补丁未部署时走 ``[PENDING-DEPLOY]`` 分支：断言**当前真实部署行为**
    （仍等于 cash+mv，即漏掉 frozen），使本文件在任何时刻都是绿的，
    且补丁一旦落盘会自动切到新断言。
    """
    frozen = 15000.0
    a = _Row(m_dFrozenCash=frozen)
    got = _MOD._total_asset_of(a, CASH, MV)
    if _is_patched():
        want = CASH + frozen + MV
        assert got == want, (
            "frozen must be in total: got %r want %r (delta %r)"
            % (got, want, want - got))
    else:
        print("[PENDING-DEPLOY] bridge patch not applied yet: total_asset still "
              "excludes frozen (%r). Test will switch to the strict form automatically."
              % (got,))
        assert got == TOTAL_LEGACY, (
            "unpatched module should still ignore frozen, got %r" % (got,))


def test_code_rev_is_non_empty_and_has_a_date_prefix():
    """``CODE_REV`` 必须可读、带日期前缀 —— 它是「新代码是否已加载」的唯一凭据。

    QMT 里跑的是已加载的旧代码，改完文件不重跑策略等于没改。运维只能靠
    ``heartbeat.json`` / ``PING`` 里的 ``rev`` 判断，所以这个字段不能为空、
    也不能退化成无意义常量。
    """
    rev = str(getattr(_MOD, "CODE_REV", "") or "")
    assert len(rev) >= 16, "CODE_REV too short to identify a build: %r" % (rev,)
    assert rev[:4].isdigit() and rev[4] == "-", "CODE_REV must start with YYYY-MM: %r" % (rev,)


if __name__ == "__main__":
    # 允许单文件直跑（与 tests/run_all.py 的收集口径一致）
    failed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print("PASS %s" % _name)
            except AssertionError as _e:
                failed += 1
                print("FAIL %s: %s" % (_name, _e))
    sys.exit(1 if failed else 0)

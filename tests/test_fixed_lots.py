# -*- coding: utf-8 -*-
"""固定金额 → 股数换算的守卫测试（2026-11-10 P1 实盘缺陷回归）。

★ 本仓测试**不得使用任何 pytest fixture**（含 tmp_path / monkeypatch / capsys）：
  tests/run_all.py 是自研 runner，直接 ``fn()`` 无参调用，带形参一律 TypeError。

## 为什么单独钉这一条

2026-11-10 13:23:15 实盘撞上：

    BUY 拒绝 510050.SH: fixed_amount=290元 不足一手(290元/手) ⇒ 无法按小额验证建仓

290.2 元当然够买一手 2.902 元的 ETF。真因是二进制浮点把
``amount / price / 100`` 算到整数**下方 1 个 ULP**：

    price=2.902, amount=290.2  →  0.99999999999999988898  →  int() = 0 手

而 ``fixed_amount`` 在 OWNER 指令单（``core/owner_order.py`` 的 ``qty * price``）
和观察篮（manual_entry）里**都是这么反算出来的**，所以这是随机复发型缺陷：
同一代码同一金额，2.897 能成、2.902 就废。

下面第二节的扫描测试是主防线 —— 单点用例只能钉住踩过的那个价格。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine.event_engine import fixed_lots  # noqa: E402


# ------------------------------------------------------------ 实盘踩过的点
def test_regression_price_2_902():
    """13:23:15 现场：290.2 元 / 2.902 元 = 一手，必须返回 100 而不是 0。"""
    assert fixed_lots(290.2, 2.902) == 100


def test_regression_price_2_897():
    """13:04:39 现场（这笔运气好、恰好能过）必须继续成立，不能修一个坏一个。"""
    assert fixed_lots(289.7, 2.897) == 100


# ------------------------------------------------------------ 主防线：全价扫
def test_sweep_every_cent_price_yields_one_lot():
    """价格 1.00~20.00 每分钱一档，"恰好一手"都必须算出一手。

    这是防"随机复发"的主防线：浮点误差挑价格，单点用例抓不住。
    """
    bad = []
    p = 100
    while p <= 2000:
        price = p / 100.0
        qty = fixed_lots(100 * price, price)
        if qty != 100:
            bad.append((price, qty))
        p += 1
    assert not bad, "以下价格上「恰好一手」被算成 %d 股：%s" % (0, bad[:5])


def test_sweep_multi_lot_amounts():
    """2 手 / 5 手 / 37 手 同样不能被截断成少一手。"""
    for lots in (2, 5, 37, 100):
        p = 100
        while p <= 2000:
            price = p / 100.0
            assert fixed_lots(lots * 100 * price, price) == lots * 100, (price, lots)
            p += 7


# ------------------------------------------------------------ 真实不足与非法输入
def test_genuinely_insufficient_returns_zero():
    """289.00 元买 2.902 元的一手（需 290.20）确实不够 —— 容差不得把它放行。"""
    assert fixed_lots(289.0, 2.902) == 0
    assert fixed_lots(290.19, 2.902) == 0


def test_zero_and_negative_inputs_return_zero():
    assert fixed_lots(0, 2.9) == 0
    assert fixed_lots(-100, 2.9) == 0
    assert fixed_lots(1000, 0) == 0
    assert fixed_lots(1000, -1) == 0


def test_none_and_bad_types_return_zero():
    """不得抛异常：调用点在 _handle_buy 里，抛了会把整条入场路径炸掉。"""
    assert fixed_lots(None, 2.9) == 0
    assert fixed_lots("abc", 2.9) == 0
    assert fixed_lots(1000, None) == 0


def test_rounds_down_to_board_lot():
    """不能四舍五入成多买：1000 元 / 2.902 = 344.6 股 ⇒ 只能 300 股。"""
    assert fixed_lots(1000.0, 2.902) == 300
    assert fixed_lots(1000.0, 2.9) == 300

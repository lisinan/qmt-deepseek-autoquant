# -*- coding: utf-8 -*-
"""冻结资金必须计入总资产 —— 2026-11-11 打新误熔断的守卫测试。

## 立项依据（实盘铁证，勿删）

2026-11-11 11:25:43，账户做北交所新股申购（全额预缴），券商把 58,800.03 元
从「可用」划到「冻结」：

    m_dAvailable   58,985.55  ->  185.55
    m_dFrozenCash       0.00  ->  58,800.03
    持仓市值                         4,206.70

桥侧 ``_total_asset_of()`` 用 ``cash(可用) + 市值`` 当作自洽口径，得到
**4,392.25**；与券商 ``m_dTotalAsset``(63,192) 差 93% ⇒ 判为坏值 ⇒ 回落到
4,392.25。引擎据此算出：

    max_drawdown  (4,392.25 - 67,453.62) / 67,453.62 = -93.49%   -> 熔断
    日内亏损      (4,392.25 - 61,523.15) / 61,523.15 = -92.86%   -> 硬止损
    => 真实卖出 510500.SH x 500 @6.687（成交 3,343.50，白亏 13.5 元）

## 本文件钉死什么

1. **冻结为 0 时逐位返回原值** —— 绝大多数轮次都走这条路径，必须零行为变化。
2. **总资产 < 可用 + 冻结** 时补回到该下限（这是唯一需要的判据，不依赖券商
   字段语义）。
3. **幂等**：桥侧补丁（``derived = cash + frozen + mv``）上线后
   ``total >= cash + frozen`` 恒成立 ⇒ 不得重复加冻结。
4. 脏输入（None / 字符串 / 负数冻结）不得抛异常，也不得凭空放大总资产。

不碰生产 DB / 生产日志：本文件只调纯函数。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.event_engine import frozen_adjusted_total  # noqa: E402

# 2026-11-11 11:25 盘中的真实数值
CASH_FROZEN = 185.55
FROZEN = 58800.03
MV = 4206.70
BROKEN_TOTAL = CASH_FROZEN + MV        # 4,392.25  桥侧回落值（不含冻结）
REAL_TOTAL = CASH_FROZEN + FROZEN + MV  # 63,192.28


def test_zero_frozen_is_bit_identical():
    """冻结为 0 ⇒ 原样返回。这是「补丁不改变常态行为」的唯一保证。"""
    assert frozen_adjusted_total(11611.49, 0.0, 57862.49) == 57862.49
    assert frozen_adjusted_total(58985.55, 0.0, 63192.25) == 63192.25


def test_ipo_freeze_is_restored_to_floor():
    """打新预缴 58,800.03 ⇒ 总资产必须回到 63,192.28，而不是 4,392.25。

    这就是今天那次误熔断的完整数值复现：不修则 -93.49%，修则 -6.32%（正常）。
    """
    got = frozen_adjusted_total(CASH_FROZEN, FROZEN, BROKEN_TOTAL)
    assert abs(got - REAL_TOTAL) < 1e-6, (
        "frozen must be restored: got %.2f want %.2f" % (got, REAL_TOTAL))
    peak, day_open = 67453.62, 61523.15
    assert abs((BROKEN_TOTAL - peak) / peak) > 0.9      # 旧行为：-93.5% 假熔断
    assert abs((got - peak) / peak) < 0.07              # 新行为：-6.3% 正常
    assert abs((got - day_open) / day_open) < 0.06      # 不再越过 -6% 硬止损


def test_idempotent_after_bridge_patch():
    """桥侧已把 frozen 计入 derived 时，不得再加一次（防重复计）。"""
    already = CASH_FROZEN + FROZEN + MV            # 63,192.28 已含冻结
    assert frozen_adjusted_total(CASH_FROZEN, FROZEN, already) == already
    # 券商口径更高（含未结算收益）时也不得压低
    assert frozen_adjusted_total(CASH_FROZEN, FROZEN, 64000.0) == 64000.0


def test_partial_freeze_below_market_value_untouched():
    """小额定金冻结（挂单 300 元）且总资产已不小于下限时，不得改写。"""
    assert frozen_adjusted_total(62000.0, 300.0, 64000.0) == 64000.0


def test_dirty_inputs_do_not_raise_or_inflate():
    """脏输入不得抛异常，也不得凭空放大总资产。"""
    assert frozen_adjusted_total(None, None, 1000.0) == 1000.0
    assert frozen_adjusted_total("x", "y", 1000.0) == 1000.0
    assert frozen_adjusted_total(100.0, -50.0, 1000.0) == 1000.0   # 负冻结视同 0
    assert frozen_adjusted_total(0.0, 0.0, 0.0) == 0.0


if __name__ == "__main__":
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

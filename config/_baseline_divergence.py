# -*- coding: utf-8 -*-
"""P0 基线「已声明分歧」登记表（**唯一事实源**）。

为什么需要这张表（2026-11-08 R50-RSI）：
  验证器的 ``base_cfg()`` 是生产配置的手抄镜像，本项目已在 docstring 里记录过
  13 次「基线漂移」。为了抓漂移，仓库里出现了**两份**判据：

    1. ``tests/test_evolve_baseline_sync.py::KNOWN_DIVERGENCE``
       —— 逐值钉住「刻意保留的差异」；任一侧改值即红，防差异无声扩大。
    2. ``scripts/_recursive_self_improve.py::audit_baseline_drift()``
       —— 逐字段比对，只要不相等就打 ``★漂移``。

  于是 ``manual_entry_codes``（回测 ``()`` / 生产 ``['510050.SH']``）在 #1 里是
  **已登记、已写理由、已钉值**的刻意分歧，在 #2 里却是**每周都报一次的 P0 漂移**，
  且这个 P0 永远修不了：修它只有两条路，要么把 510050 抄进回测基线（会改 P0，
  2026-11-05 OWNER 已裁决不放开），要么删掉这个字段（丢失真实资金的对账对象）。

  ⇒ 后果不是「多一条噪声」，而是**优先级第一名被永久占用**：R49-RSI 与 R50-RSI
    两周的 `priority_actions()` 首项都是这条假 P0，真正的漂移即便出现也挤不到首位。
    这正是本项目反复踩的「狼来了」型缺陷（GUARD 每小时假告警、KPI#2 划分假象同型）。

修法（不是加白名单，而是消掉第二份判据）：
  把表移到本模块，**测试与审计脚本都 import 它**；审计侧对所有「不相等」的字段
  再问一次本表：值**逐位等于**登记值 → ``已声明分歧``（不算漂移）；否则 → ``★漂移``。

  ★ 安全属性：豁免只对**登记的那一对具体值**生效。
    若某人把生产改成 ``['510050.SH','300308.SZ']``，或把回测基线改成 ``('510050.SH',)``，
    值不再等于登记值 ⇒ 立刻回到 ``★漂移`` 并占用 P0 首位。因此本表不会把真漂移吞掉。

回滚：删本文件，把 ``KNOWN_DIVERGENCE`` 字面量恢复回
      ``tests/test_evolve_baseline_sync.py``，并把审计脚本里的
      ``is_declared(...)`` 分支删除（恢复「不相等即漂移」）。
"""
from __future__ import annotations

from typing import Any, Dict, Tuple

# 已知、尚未裁决的刻意差异：(回测侧值, 生产侧值)。钉住数值 ⇒ 任一侧变动即红。
KNOWN_DIVERGENCE: Dict[str, Tuple[Any, Any]] = {
    # （空）2026-09-29 第 12 轮：down_day_exit_pct 裁决为 -5.0，移入 MUST_MATCH。
    # （空）2026-09-29 第 13 轮：atr_stop_mult 裁决为 2.9，移入 MUST_MATCH。
    # 【2026-09-30 第 15 轮】观察篮首次建模，两个字段**刻意保留不同**：
    #   manual_entry_codes      生产 = 5 只观察篮 / 回测基线 = () 空元组
    #   manual_entry_exit_exempt 生产 = True                  / 回测基线 = True
    # 为什么不同：**P0 基线的定义就是「已验证的纯动量路径」**，
    #   它代表「观察篮存在之前」的那条被走完 15 轮验证的策略；
    #   观察篮是**叠加在其上的一层实盘行为**，必须作为**显式候选**（N1/N2/N3/N4）
    #   与基线对比，而不是悄悄并进 P0——否则所有历史 dSh 都会因为基线里多塞了
    #   一块从未验证过的仓位来源而整体失真。
    # ⇒ 代价：这两个字段现在是「回测无、实盘有」的第 8 次登记差异，
    #   演进时会用 `--track defect --baseline N1_观察篮实盘现状_豁免` 反向对照，
    #   使 dSh 直接读出「关闭观察篮/豁免」的修复收益。
    # 【2026-09-30 第 15 轮·已裁决】OWNER 授权清空观察篮 ⇒ 两侧**均为空**，
    #   差异已消除。之所以仍留在 KNOWN_DIVERGENCE 而不是删掉或移入 MUST_MATCH：
    #   MUST_MATCH 的逐字段比较是为数值设计的，对 tuple 语义不稳；
    #   留在此处则保留「任一侧变动必须显式登记」的保护 ——
    #   日后若有人往生产塞回观察篮，本表立即变红并提示先回测代价。
    # 【2026-10-29 live 链路验证】生产篮子 = ('510050.SH',)，回测基线仍为 ()。
    #   为什么允许这处不对称：510050.SH 是**链路探针**（VALIDATION_UNIVERSE），
    #   不是研究样本 —— 它不进回测宇宙、不参与排名/绩效，只用于在 live 模式下
    #   用固定 1,500 元买一笔真实成交，证明「订阅→风控→下单→成交回报→持仓」通。
    #   ⇒ 回测基线**必须保持 ()**：P0 的定义是「已验证的纯动量路径」，
    #     任何把探针并进基线的动作都会让所有历史 dSh 失真（观察篮已上过一次课）。
    # 【2026-11-05 OWNER 裁决「510050 是观察舱，一直保留；300308 也保持不动」】
    #   当日曾备好「只放开 510050」的整套改动（本 pin 置 ((), ()) + settings 三处），
    #   在披露 v29 那条 `[FORBIDDEN]`（观察篮是「有持仓才能评估」的唯一度量对象）
    #   之后，OWNER 决定**不放开**，改动整体撤回 ⇒ 本 pin 保持原值。
    #   ★ 若日后真要放开，两处必须同改：`settings.py` 的 `manual_entry_codes` 与
    #     `exit_exempt_codes`。只清后者会被自反撤销 —— 510050 被 −6% 强平卖出后
    #     观察篮**次日**补买（`_manual_sold_today` 只管当日，`:2588`），补买成功时
    #     `:2604 _manual_positions.add(code)` 重新登记 ⇒ `_is_manual_exempt` 判据 ①
    #     复活 ⇒ 豁免自动恢复、本 pin 却仍钉着旧值，测试会「绿着说谎」。
    #   回滚：把 production 侧置回 [] 并把下方 pin 改回 ((), ())。
    "manual_entry_codes": ((), ("510050.SH",)),
    "manual_entry_exit_exempt": (True, True),
}


def _norm(v: Any) -> Any:
    """把 list/tuple 归一成 tuple；None 归一成空 tuple；bool/数值原样返回。

    `[]` 与 `()` 语义相同，不应因容器类型差异造出假漂移；但 **bool 不能走
    `v or ()`** —— 否则 False 会被抹成空元组，与登记的 False 对不上。
    """
    if v is None:
        return ()
    if isinstance(v, (list, tuple)):
        return tuple(v)
    return v


def declared_pair(field: str):
    """返回该字段的登记值 (回测值, 生产值)；未登记返回 None。"""
    return KNOWN_DIVERGENCE.get(field)


def is_declared(field: str, bt_value: Any, prod_value: Any) -> bool:
    """两侧差异是否**正是**登记在案的那一对值。

    只对登记的**具体值**生效 —— 任一侧再变（哪怕只是多一只观察篮）都返回 False，
    让调用方把它重新判成真漂移。这是本表唯一的安全边界：
    ★ 勿改成「按字段名豁免」（那样 510050 悄悄变 3 只也照样绿）。
    """
    pair = KNOWN_DIVERGENCE.get(field)
    if not pair:
        return False
    back_pin, prod_pin = pair
    return (_norm(bt_value) == _norm(back_pin)
            and _norm(prod_value) == _norm(prod_pin))

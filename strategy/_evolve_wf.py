# -*- coding: utf-8 -*-
"""自进化执行器专用 walk-forward 验证器（EVOLVE 环节）。

用途：把「当前生产配置」与候选改动放在**同一批互不重叠的滚动折**上对比，
输出 IS（全样本）+ OOS（多折）双栏证据，供晋升闸门裁决。

两种模式：
  --mode compare  对比候选配置组（candidates()）
  --mode grid     对单个参数做 OOS 网格扫描（找参数高原，拒绝尖峰）

用法：
    python strategy/_evolve_wf.py --mode grid --param reentry_cooldown --values 3,5,8,10,15,20
    python strategy/_evolve_wf.py --mode compare

【2026-09-18 易踩坑】``--values`` 传**负值**列表时必须用等号连写，不能用空格：
    python strategy/_evolve_wf.py --mode grid --param hard_stop_pct --values=-0.12,-0.15,-0.18
  否则 argparse 会把 "-0.12,-0.15,..." 当成另一个选项名，报
  "argument --values: expected one argument" 而让人误以为回测数据出错。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import (  # noqa: E402
    STOCK_CODES, SECTOR_CONFIG, INDEX_CODES, MARKET_INDEX_CODE,
    # 【2026-09-30 第 15 轮】观察篮清单**直接从生产配置读取**，不在验证器里
    # 复制一份常量——硬编码第二份清单正是本项目踩了 7 次口径漂移的根因。
    STRATEGY_PARAMS as _PROD_STRATEGY_PARAMS,
)
# 生产观察篮（may be empty tuple if not configured）
PROD_MANUAL_ENTRY_CODES = tuple(
    _PROD_STRATEGY_PARAMS.get("manual_entry_codes") or ())
PROD_MANUAL_ENTRY_EXEMPT = bool(
    _PROD_STRATEGY_PARAMS.get("manual_entry_exit_exempt", True))
from strategy.backtest_daily import BacktestConfig, run_backtest, load_daily  # noqa: E402

FIXED_WARMUP = 130


def wide_universe() -> list:
    s = set(STOCK_CODES)
    for _k, v in SECTOR_CONFIG["sectors"].items():
        for code, _name in v["stocks"]:
            s.add(code)
    return sorted(s)


_CACHE: dict = {}


def preload(codes: list, count: int) -> dict:
    out = {}
    for code in codes:
        key = f"{code}:{count}"
        if key not in _CACHE:
            d = load_daily(code, count)
            if d:
                _CACHE[key] = d
        if key in _CACHE:
            out[code] = _CACHE[key]
    return out


def slice_by_index(data: dict, lo: int, hi: int) -> dict:
    return {c: {k: list(v[lo:hi]) for k, v in d.items()} for c, d in data.items()}


def aligned_universe(data: dict, codes: list) -> tuple:
    """★★ 先把全宇宙对齐到统一交易日轴，再交给折划分切。

    【2026-09-28 第 11 轮 PM-EVOLVE 修复：折窗口跨标的日期错位 + 近 11 个月从未入折】

    原实现直接用**未对齐的原始数组**按索引切片造折
    （``n = min(len(d["close"]))`` → ``slice_by_index(data, lo, hi)``）。
    各标的历史长度不同（900/888/883/841/809/748/744/741），索引 i 在
    不同标的上指向**不同日期** —— 实测 F6（索引 580..670）：

        000977.SZ 等 17 只（900 根）： 20250609 → 20251020
        300476.SZ          （741 根）： 20260128 → 20260616   ← 错位约 8 个月
        603986.SH          （748 根）： 20260112 → 20260601
        688008.SH          （744 根）： 20260305 → 20260720

    切片后 ``run_backtest`` 内的 ``align_panel`` 取各标的日期**并集**，
    于是每折实际横跨 ~250 根、各票在不同时点"登场"，横截面动量排名在
    比较**不同日期**的标的 ⇒ 折内结果既不是 OOS 也不是 IS，是无意义混合。

    附带后果：``n`` 被最短的 300476.SZ（741 根）钉住，折只排到索引 670，
    而长历史标的索引 670 = 20251021 ⇒ **20251022 ~ 20260924 共 229 根
    （近 11 个月，恰恰是 live paper 亏损期）从未进入任何折**。

    修复：先 ``align_panel``（按日期对齐 + 停牌前向填充 + valid 标记），
    再按**面板索引**切片 ⇒ 所有标的逐折共享同一日期窗口。
    面板里补回 ``date`` 字段，使 ``run_backtest`` 内的二次 align 仍走
    「按日期对齐」分支（否则会退化成尾部截断、日期变 "0,1,2…" 导致
    北向序列 dates[_i] 查不到而闸门静默失效）。
    """
    from strategy.backtest_daily import align_panel
    ks = [k for k in data.keys() if k not in INDEX_CODES]
    dates, panel = align_panel({k: data[k] for k in ks})
    if not dates:
        return [], {}
    for code in panel:
        panel[code]["date"] = list(dates)
    return dates, panel


def base_cfg() -> BacktestConfig:
    """当前生产配置（与已验证基线同口径）。

    【2026-09-18 口径修正】原 base_cfg 停留在 momentum_top_n=6 / risk_per_trade=0.02，
    与 config/settings.py 的当前生产值（top_n=3 / risk_per_trade=0.012）不一致，
    会导致「增量 dSh」相对错误基线计算——上一轮 C1_topn3 已落盘，若不修基线，
    本轮 top_n=3 的收益会被重复计入增量。
    现按 P0 = 真实生产配置对齐，后续所有增量一律相对此基线。

    【2026-09-28 第 10 轮 AM-EVOLVE 基线同步（量测口径修正，生产零改动）】
    补齐 `northbound_mode="gate"` / `nb_lookback=20`。
    背景：生产 STRATEGY_PARAMS 自 2026-09-19 起就是 northbound_mode="gate"
      （engine/event_engine.py:253-262 启动时预取 nb 序列，:1840 `_regime_ok`
      在 `_nb_state()["blocked"]` 时拒绝放行），而 BacktestConfig 该字段默认
      "off"、base_cfg 又未显式设置 ⇒ **验证器 P0 一直是不含北向闸门的口径**。
      这与 2026-09-18 的 top_n/rpt 基线漂移同型：P0 低估生产 ⇒ 此前所有
      「dSh 增量」与 KPI（wf 均值 Sharpe）都建立在偏保守的错误基线上。
    实测（25 只 AI 宇宙 × 741 根日线 20230110→20260924，单边 0.15% 成本，
          4 套互不重叠折划分）：
      IS   ret +166.39%→+197.31%  Sh 1.31→1.47  MDD −16.63%→−15.86%
      OOS  60x9 1.197→1.382 (+0.185) / 75x7 0.952→1.271 (+0.319)
           90x6 1.348→1.574 (+0.226) / 120x4 1.565→1.664 (+0.099)
           均值 dSh **+0.207**，四窗口全正；均值 MDD −10.38%→−9.74%；
           正收折 22/26 → 24/26 (92.3%)
      ⇒ wf 均值 Sharpe **1.266 → 1.473**（与 2.0 目标差距 −0.734 → −0.527）
    本改动**只影响验证器**，不触碰 config/settings.py，生产行为零变化。
    可逆：删掉这两个关键字参数即回到旧口径。
    """
    return BacktestConfig(
        use_gate=True, cost_pct=0.0015, vol_sizing=True,
        exit_mode="trend", trend_exit_ma=60, hard_stop_pct=-0.18,
        trend_max_hold_days=120,
        momentum_rank=True, momentum_top_n=3, momentum_lookback=60,
        risk_per_trade=0.012, fixed_amount=300000.0,
        # 【2026-09-29 第 12 轮 AM-EVOLVE】-9.0 → **-5.0，与生产对齐**。
        #   此前两侧是登记在案的 KNOWN_DIVERGENCE（回测 -9.0 / 生产 -99.0），
        #   本轮裁决为「启用单日暴跌退出」，生产已改为 -5.0 ⇒ 基线同步改为 -5.0，
        #   并把该字段从 KNOWN_DIVERGENCE 移入 MUST_MATCH（见
        #   tests/test_evolve_baseline_sync.py）。这是该漂移第 4 次同型复现
        #   （09-18 top_n/rpt、09-28 northbound_mode、09-29 本项）。
        down_day_exit_pct=-5.0, max_positions=5,
        buy_score_threshold=4.0, min_signals=3,
        # 【2026-09-29 第 13 轮 PM-EVOLVE】2.0 → **2.9，与生产对齐**（原为本文件
        #   唯一残留的 KNOWN_DIVERGENCE：回测 2.0 / 生产 2.5，属第 5 次同型漂移）。
        #   裁决依据：相对**生产现状 2.5** 的 4 窗口 dSh = +0.171/+0.202/+0.119/+0.162，
        #   均值 +0.164、最差窗口 +0.119（2.8~3.2 连续正高原，2.9 为均值与最差双 argmax）。
        #   本字段已由 KNOWN_DIVERGENCE 移入 MUST_MATCH（见 test_evolve_baseline_sync.py）。
        atr_stop_mult=2.9, tp_atr_mult=4.0,
        northbound_mode="gate", nb_lookback=20,
        # 【2026-09-30 AM-EVOLVE 第 14 轮】0.30 → **0.19，与生产 RISK_PARAMS 对齐**。
        #   这是**第 6 次同型口径漂移**，且是本守卫首次漏检的：它属于 RISK_PARAMS
        #   而非 STRATEGY_PARAMS，旧守卫只比对后者 ⇒ 默默漂移无人知晓。
        #   剂量扫描（90×6）：0.14 +0.002 / 0.16~0.40 全 +0.000 ⇒ **当前仓位由
        #   risk_per_trade/(ATR%×2.9) 决定，上限根本不绑定**，故本次对齐零行为变化
        #   （IS/OOS 逐位相同）。但它是**潜伏陷阱**：一旦日后放宽 atr_stop_mult 或
        #   提高 risk_per_trade 使单仓膨胀，回测会自动允许 30% 而实盘被夹到 19%，
        #   两侧在无人察觉的情况下分叉。故现在就对齐并纳入守卫。
        #   同时把 test_evolve_baseline_sync.py 的漏登记检查扩展到 RISK_PARAMS。
        max_single_position_pct=0.19,
        # 【2026-09-30 AM-EVOLVE 第 14 轮】-99.0（关闭）→ **-0.06，与生产对齐**。
        #   这是同一守卫扩展后**第二次**抓到的漂移（第 7 次同型漂移）。生产自
        #   2026-09-16 起就有「当日账户亏损 ≤ -6% 强平全部可卖持仓」，而 P0 基线
        #   从未建模。剂量扫描（90×6）：-0.10 / -0.08 / -0.06 / -0.05 与关闭态
        #   **逐位完全相同**，仅 -0.04 有 -0.005 差异 ⇒ 日线上日内权益跌 6% 极罕见，
        #   该闸门在回测中几乎不触发 ⇒ 对齐零行为变化。
        #   意义同样在防潜伏陷阱：若日后有人收紧该阈值到 -0.03 量级，回测会继续
        #   「无此闸门」而实盘会真的强平，两侧静默分叉。
        daily_stop_flatten_pct=-0.06,
        min_warmup=FIXED_WARMUP,
    )


def candidates() -> dict:
    b = base_cfg()
    out = {"P0_当前生产基线": b}
    # ---- 第 2 轮：网格扫描筛出的正向维度 ----
    out["C1_topn3"] = replace(b, momentum_top_n=3)
    out["C2_topn2"] = replace(b, momentum_top_n=2)
    out["C3_topn4"] = replace(b, momentum_top_n=4)
    out["C4_topn3_maxpos3"] = replace(b, momentum_top_n=3, max_positions=3)
    out["C5_topn3_cd3"] = replace(b, momentum_top_n=3, reentry_cooldown=3)
    out["C6_topn3_cd5"] = replace(b, momentum_top_n=3, reentry_cooldown=5)
    out["C7_maxpos3"] = replace(b, max_positions=3)
    out["C8_topn3_hs12"] = replace(b, momentum_top_n=3, hard_stop_pct=-0.12)
    return out


def candidates_final() -> dict:
    """第 3 轮：在已确认的 momentum_top_n=3 主效应上做最小增量。"""
    b = base_cfg()
    out = {"P0_当前生产基线": b}
    out["D1_topn3"] = replace(b, momentum_top_n=3)
    out["D2_topn3_cd5"] = replace(b, momentum_top_n=3, reentry_cooldown=5)
    out["D3_topn3_cd5_hs12"] = replace(
        b, momentum_top_n=3, reentry_cooldown=5, hard_stop_pct=-0.12)
    out["D4_topn3_hs12"] = replace(b, momentum_top_n=3, hard_stop_pct=-0.12)
    out["D5_topn3_cd8"] = replace(b, momentum_top_n=3, reentry_cooldown=8)
    out["D6_topn3_cd5_hs15"] = replace(
        b, momentum_top_n=3, reentry_cooldown=5, hard_stop_pct=-0.15)
    # ---- 2026-09-21 AM：单日暴跌清仓阈值（生产当前 -99.0=关闭，基线 -9.0）----
    # 90x6 网格里 -5.0 达 +0.121 门槛，但邻居 -5.5/+0.005、-4.5/+0.063 均未达标
    # → 疑似尖峰。此处纳入 4 窗口共识做终局裁决（防窗口运气）。
    out["E1_dd5"] = replace(b, down_day_exit_pct=-5.0)
    out["E2_dd6"] = replace(b, down_day_exit_pct=-6.0)
    # ---- 2026-09-29 AM-EVOLVE（第 12 轮）重裁 ----
    # 为什么重跑：09-21 的共识是在**两项已证伪的量测缺陷**之上做的——
    #   ① 折窗口跨标的日期错位（09-28 PM 修复）；② 基线漏掉 northbound_mode
    #   （09-28 AM 修复）。两者合计改变 OOS 约 0.17~0.27 ⇒ 旧结论不可继承。
    # E0 = **生产现状**（-99.0 关闭）。注意 base_cfg 的 P0 基线取 -9.0，与生产不符
    #   （已登记为 KNOWN_DIVERGENCE），故必须单独跑 E0 才能量出「相对生产」的真增量。
    # E1b = -4.0 剂量对照（检验 -3~-5 是否构成高原而非尖峰）。
    out["E0_dd99_生产现状"] = replace(b, down_day_exit_pct=-99.0)
    out["E1b_dd4"] = replace(b, down_day_exit_pct=-4.0)
    # ---- 2026-09-29 PM-EVOLVE（第 13 轮）：ATR 止损倍数（重裁）----
    # 为什么重跑：09-21 判定「杠杆旋钮、Sharpe 几乎不变」，但那是**错基线**结论
    #   （折错位 + 缺 nb 闸门 + down_day 关闭三重缺陷之上做的）。新基线（down_day
    #   -5.0 已启用）下 90x6 网格出现 2.4~3.2 的连续高原：
    #     2.0(基线) +0.000 / 2.4 +0.123 / 2.6 +0.092 / 2.8 +0.170
    #     3.0 +0.182 / 3.2 +0.188 / 3.4 -0.114  ⇒ 非尖峰（5 个邻值连续为正）
    # 机理：仓位 ∝ risk_per_trade / (atr% * mult) ⇒ 放宽止损 = 同步缩小仓位、
    #   单笔风险不变，但换手与成本下降、被洗出去的次数减少 ⇒ 收益降（21.1%→14.5%）
    #   而波动降得更多 ⇒ Sharpe 升、MDD 显著改善（-6.86%→-4.66%）。
    #   ★ 这不是纯缩放：纯缩放下 Sharpe 不变；此处 Sharpe 升说明省下的摩擦是真实 alpha。
    # ★ 注意口径：验证器 base_cfg 是 2.0，而**生产是 2.5**（登记在案的
    #   KNOWN_DIVERGENCE）。故必须单独跑 J0（=生产现状 2.5）才能量出相对生产的真增量。
    # ★ 口径更新（2026-09-29 第 13 轮）：生产已由 2.5 改为 **2.9**，base_cfg 同步
    #   为 2.9 ⇒ 本候选不再是「生产现状」，仅作剂量对照保留（相对新 P0 的 dSh
    #   应约为 -0.16，即改回 2.5 会损失多少）。
    out["J0_atr25_旧生产值"] = replace(b, atr_stop_mult=2.5)
    # 剂量邻居：2.8 / 3.0 / 3.1 / 3.2（4 窗口最差分别为 +0.069/+0.097/+0.092/-0.001）
    out["J1_atr28"] = replace(b, atr_stop_mult=2.8)
    out["J2_atr30"] = replace(b, atr_stop_mult=3.0)
    out["J3_atr31"] = replace(b, atr_stop_mult=3.1)
    out["J4_atr32"] = replace(b, atr_stop_mult=3.2)
    # ---- 2026-09-22 PM-EVOLVE：日线偏置闸门 min_daily_bias ----
    # 实盘入场闸门是 ``trend_up or bias >= min_daily_bias``（trend_strategy.py:151，
    # 生产 0.2），而回测历史只有 trend_up 一路（等价于 2.0=关闭）。
    # F1 = 回测基线口径（关闭 bias 通道）；F2 = **当前生产口径**（0.2，放行 bias>=0.3）。
    # 若 F2 在四窗口一致为负，则生产应改为 2.0 与已验证口径对齐。
    out["F1_bias关闭_2.0"] = replace(b, min_daily_bias=2.0)
    out["F2_生产口径_0.2"] = replace(b, min_daily_bias=0.2)
    out["F3_bias_-0.3"] = replace(b, min_daily_bias=-0.3)
    # ---- 2026-09-23 AM-EVOLVE：轮动「日内突破绕过日线闸门」代理 ----
    # 实盘 _maybe_rotate 在 is_breakout=True 时无视 daily-gate 的 HOLD 直接买入
    # （event_engine.py:674-675 / 706-707），回测器无 rotation 逻辑 ⇒ 口径背离。
    # G1 = 只豁免日线闸门（保留评分门槛）；G2 = 连同评分门槛一起豁免
    # （完整复现实盘轮动语义）；G3 = 更严的突破阈值做邻居对照。
    # 若 G* 在四窗口一致为负，则「关闭轮动的闸门旁路」即为正向改动。
    out["G1_轮动绕闸门_1.5"] = replace(b, breakout_bypass_gate=1.5)
    out["G2_轮动绕闸门绕评分_1.5"] = replace(
        b, breakout_bypass_gate=1.5, breakout_bypass_score=True)
    out["G3_轮动绕闸门_3.0"] = replace(b, breakout_bypass_gate=3.0)
    # ---- 2026-09-23 PM-EVOLVE：★「实盘现状」复合代理 ----
    # 上午只建模了「绕闸门」单一缺陷。下午审计发现实盘**同时**存在第二个缺陷：
    # 轮动「弱换强」在换出被 T+1 拦截时仍执行买入 → 净持仓 +1 → 突破 max_positions。
    # 铁证：09-23 10:31:06 日志「[T+1 拦截] 603986 跳过」→ 同一时刻仍 BUY 688012，
    # 持仓 5→6；equity_snapshots 09-22 EOD 8 仓 / 09-23 EOD 6 仓，均 > max_positions=5。
    # 而回测器 max_positions 是**严格夹紧**的 ⇒ 实盘跑的是「从未被回测验证的变体」。
    # 网格已证超仓方向单调有害：6 −0.087 / 7 −0.097 / 8 −0.086（5 为高原峰值）。
    # H 系列 = 两项缺陷叠加，即**实盘真实状态**；基线 P0 = 两项均已修复。
    # 故「修复收益」= −(H − P0)，需取反阅读。
    out["H1_实盘现状_绕闸门+超仓6"] = replace(
        b, breakout_bypass_gate=1.5, max_positions=6)
    out["H2_实盘现状_绕闸门绕评分+超仓6"] = replace(
        b, breakout_bypass_gate=1.5, breakout_bypass_score=True, max_positions=6)
    out["H3_实盘现状_绕闸门+超仓7"] = replace(
        b, breakout_bypass_gate=1.5, max_positions=7)
    # ---- 2026-09-24 AM-EVOLVE（第 6 轮）：★ 连亏降仓 position_scale 代理 ----
    # 上一轮（09-23 PM）交接给我的头号方向：「下一处已知『实盘有、回测无』是
    #   position_scale 连亏降仓（consec_loss=3→0.8），建议建代理后用缺陷通道度量」。
    # 今日实盘铁证已出现：09-24 10:00:00 **同一时刻** 4 笔亏损 SELL → _consec_loss
    #   3→7 → 越过 halt=5 → scale=0.0、halted=True，账户自 10:00 起完全无法开仓。
    # 机理假设（本代理要检验的）：阶梯末端 0.0 = **完全冻结**，且连亏计数把
    #   「1 次板块级相关退出」当成「N 次独立判断失误」⇒ 在策略唯一能靠少数大赢家
    #   修复净值的时候反而缩仓/停摆，形成「跌→缩仓→更难修复」的负反馈。
    # I1 = 实盘现状（trigger=3 / halt=5 / 地板 0.0，即当前 RISK_PARAMS 语义）
    out["I1_实盘现状_连亏降仓"] = replace(b, consec_loss_scale=True)
    # I2 = 只改阶梯地板：降仓但**永不完全冻结**（0.4）。参数零改动可达（改代码常量）。
    out["I2_连亏降仓_地板0.4"] = replace(
        b, consec_loss_scale=True, consec_loss_floor=0.4)
    # I3 = 触发阈值提到 5（与 halt 同点）：阶梯理论上永不生效，只保留熔断。
    out["I3_连亏降仓_trigger5"] = replace(
        b, consec_loss_scale=True, consec_loss_trigger=5)
    # I4 = 关掉熔断、只留阶梯（分离「降仓」与「停摆」两个效应，定位真凶）
    out["I4_连亏降仓_无halt"] = replace(
        b, consec_loss_scale=True, consec_loss_halt=10**6)
    # I5 = 阶梯整体减半幅度（0.9/0.8/0.7/0.6/0.5）—— 剂量反应对照
    out["I5_连亏降仓_地板0.6"] = replace(
        b, consec_loss_scale=True, consec_loss_floor=0.6)
    # ---- ★ 本轮主候选：同批退出合并计数（缺陷修复）----
    # 实盘 on_fill 逐笔 +1；今日 4 笔 SELL 相隔 3ms（10:00:00.662591/.664592/
    # .664592/.665591）⇒ 一次板块级回调被计成 4 次连亏，consec 3→7 瞬间越 halt。
    # J1 = 修复后：按交易日聚合净盈亏判定（一次板块退出 = 1 次连亏）。
    # 参数零改动（只改计数语义），可逆，属缺陷修复通道。
    out["J1_修复_同批退出合并计数"] = replace(
        b, consec_loss_scale=True, consec_loss_batch=True)
    # J2 = 更激进的剂量对照：连**触发阈值**一起放宽到 5（阶梯实际永不生效）
    out["J2_修复_合并计数+trigger5"] = replace(
        b, consec_loss_scale=True, consec_loss_batch=True,
        consec_loss_trigger=5)
    # J3 = 合并计数 + 保留地板 0.4（双重保险，剂量更弱）
    out["J3_修复_合并计数+地板0.4"] = replace(
        b, consec_loss_scale=True, consec_loss_batch=True,
        consec_loss_floor=0.4)
    # ---- 2026-09-24 PM-EVOLVE（第 7 轮）：★ 日内已实现亏损熔断代理 ----
    # 上午交接给我的头号方向：「下一处『实盘有、回测无』是日内强平
    #   daily_stop_flatten_pct / daily_loss_limit_abs，建议建代理后按同一方法论度量」。
    # 今日实盘铁证（risk_snapshots 15:02）：
    #   ``{"halted": true, "daily_pnl": -5602.4, "consecutive_losses": 7,
    #      "position_scale": 0.0, "peak_asset": 1005996.0}``
    #   ⇒ daily_pnl=-5602.4 已越过 **daily_loss_limit_abs=-5000（=100万账户的0.5%）**，
    #     但 loss_pct=-0.565% 远未达 daily_loss_limit_pct=-3%。
    #     当日 44 条观察篮 BUY 信号（300308，每 5 分钟一条）**全部零成交**。
    # ★★ 缺陷假设：阈值 ② 是**绝对金额、不随账户规模缩放**的遗留值。paper 账户
    #   09-21 复位为 1,000,000 后它等价于 0.5%，比显式设定的 -3% 严格 **6 倍**，
    #   且账户规模越大越严 ⇒ 属「长度单位不缩放」型工程缺陷，不是策略选择。
    # K1 = **实盘现状**（连亏阶梯 I1 + 日内熔断 abs 0.5% + pct 3%）⇒ 本轮的反向对照基线
    out["K1_实盘现状_连亏+日内熔断0.5"] = replace(
        b, consec_loss_scale=True, daily_loss_halt=True,
        daily_loss_abs_pct=0.005, daily_loss_pct=-0.03)
    # K2 = **修复候选**：移除不缩放的绝对阈值，只保留显式的百分比口径 -3%
    out["K2_修复_日内熔断仅pct3"] = replace(
        b, consec_loss_scale=True, daily_loss_halt=True,
        daily_loss_abs_pct=0.0, daily_loss_pct=-0.03)
    # K3 = 完全关闭日内亏损熔断（剂量终点，用于检验单调性）
    out["K3_日内熔断关闭"] = replace(b, consec_loss_scale=True)
    # K4~K7 = 剂量反应网格：等效绝对阈值 0.3% / 1% / 2% / 3%（与 pct 同）
    out["K4_日内熔断_abs0.3"] = replace(
        b, consec_loss_scale=True, daily_loss_halt=True,
        daily_loss_abs_pct=0.003, daily_loss_pct=-0.03)
    out["K5_日内熔断_abs1"] = replace(
        b, consec_loss_scale=True, daily_loss_halt=True,
        daily_loss_abs_pct=0.01, daily_loss_pct=-0.03)
    out["K6_日内熔断_abs2"] = replace(
        b, consec_loss_scale=True, daily_loss_halt=True,
        daily_loss_abs_pct=0.02, daily_loss_pct=-0.03)
    out["K7_日内熔断_abs3"] = replace(
        b, consec_loss_scale=True, daily_loss_halt=True,
        daily_loss_abs_pct=0.03, daily_loss_pct=-0.03)
    # ---- 日内强平（daily_stop_flatten_pct = -6%）----
    # 实盘 risk/manager.py:98-111 用「开盘资产口径（含隔夜重估）」算当日亏损，
    #   达阈值即置 flatten_requested + 停牌，引擎次日开盘强平全部可卖持仓。
    # 回测器历史上**无该分支**（grep 零命中）⇒ 第三处「实盘有、回测无」。
    # 今日实盘 -0.571% 远未达 -6%，不是当日真凶，但历史上 09-16 单日 -16.4%
    #   这种日子会被强平 ⇒ 值得单独度量，完成 AM 交接的 #3 全项。
    out["K8_实盘现状_再叠加强平6"] = replace(
        b, consec_loss_scale=True, daily_loss_halt=True,
        daily_loss_abs_pct=0.005, daily_loss_pct=-0.03,
        daily_stop_flatten_pct=-0.06)
    # K9 = 只加强平、不叠加 abs（分离两个效应，检验强平自身的方向）
    out["K9_仅强平6_无abs"] = replace(
        b, consec_loss_scale=True, daily_loss_halt=True,
        daily_loss_abs_pct=0.0, daily_loss_pct=-0.03,
        daily_stop_flatten_pct=-0.06)
    # K10 = 更宽的强平阈值 -10%（剂量对照，检验是否阈值越宽越好）
    out["K10_仅强平10"] = replace(
        b, consec_loss_scale=True, daily_loss_halt=True,
        daily_loss_abs_pct=0.0, daily_loss_pct=-0.03,
        daily_stop_flatten_pct=-0.10)
    # ---- L 系列：以「AM 已落盘的 J1 修复」为新基线，测「再修 abs」的**净增量** ----
    # 为什么单列：K 系列基线是 I1（逐笔计数 = 今日实盘现状，因 J1 需重启未生效）。
    #   但 J1 已落盘，重启后的实盘状态 = J1 + 日内熔断。要回答「重启后还要不要
    #   再修 daily_loss_limit_abs」，对照必须是 J1 而非 I1，否则增量被 I1→J1
    #   的效应污染。
    # L1 = 重启后的**实盘现状**（J1 批量计数 + 日内熔断 abs 0.5% + pct 3%）⇒ 新基线
    out["L1_重启后现状_J1+abs0.5"] = replace(
        b, consec_loss_scale=True, consec_loss_batch=True,
        daily_loss_halt=True, daily_loss_abs_pct=0.005, daily_loss_pct=-0.03)
    # L2 = 在 J1 之上再修 abs（只留显式 pct 口径）⇒ 待裁决的净增量
    out["L2_重启后再修abs"] = replace(
        b, consec_loss_scale=True, consec_loss_batch=True,
        daily_loss_halt=True, daily_loss_abs_pct=0.0, daily_loss_pct=-0.03)
    # L3 = 剂量对照：abs 放宽到 1%
    out["L3_重启后abs1"] = replace(
        b, consec_loss_scale=True, consec_loss_batch=True,
        daily_loss_halt=True, daily_loss_abs_pct=0.01, daily_loss_pct=-0.03)
    # ---- 2026-09-30 PM-EVOLVE（第 15 轮）：★ 结构性候选「日线突破追涨入场」----
    # 来源：本轮首选方向是「信号质量」——主路径近 8 个交易日仅 1 笔成交，
    #   而 14 轮单参数网格已难再出 +0.10，故转结构性入场。
    # 语义（backtest_daily.py 分支 entry_mode=="trend"）：
    #   入场条件由「6 因子评分 ≥ buy_score_threshold(4.0) 且 ≥3 个正因子」
    #   换成「trend_up（日线主升）且 收盘 ≥ 近 20 日最高 × 0.98」。
    #   注意：动量前 N 名的候选池过滤（momentum_top_n=3）**照旧先于**本分支生效，
    #   所以不是放宽到全宇宙，只是把「评分质量门」换成「新高确认门」。
    # 90x6 单窗：dSh **+0.147**（Sh 1.795→1.942）、正收 6/6、均值 MDD −4.82%→−4.76%、
    #   最差折 +1.2%→+2.2%；代价是收益降（IS 110.0%→103.2%，累计 −11.2pt）
    #   ⇒ 典型的「降波动多于降收益」型风险调整改善，而非纯缩放。
    # ⚠ 口径提示（见 §7 纪律）：`entry_mode` 目前**只存在于回测侧**，实盘
    #   trend_strategy 无对应分支。若共识通过，必须先补实盘实现再落盘，
    #   否则就是「回测有效、实盘无效」的背离。
    out["M1_突破入场_098"] = replace(b, entry_mode="trend")
    # 剂量邻居（查高原、拒绝尖峰）：0.95 更宽 / 1.00 必须创 20 日新高
    out["M2_突破入场_095"] = replace(
        b, entry_mode="trend", trend_breakout_near_high=0.95)
    out["M3_突破入场_100"] = replace(
        b, entry_mode="trend", trend_breakout_near_high=1.00)
    # 新高窗口剂量（检验「越严格越好」是真实剂量反应还是 20 日窗口的巧合）
    out["M4_突破入场_100_窗40"] = replace(
        b, entry_mode="trend", trend_breakout_near_high=1.00,
        trend_breakout_window=40)
    out["M5_突破入场_100_窗60"] = replace(
        b, entry_mode="trend", trend_breakout_near_high=1.00,
        trend_breakout_window=60)
    # ---- 2026-09-30 PM-EVOLVE（第 15 轮）：★★ 观察篮（manual_entry）代理建模 ----
    # **为什么现在才做**：这是本项目最贵的一条「实盘有、回测无」分支。
    #   实盘 2026-09-29 收盘 5 只持仓里 3 只来自观察篮，而回测器历史上对
    #   `manual_entry_codes` / `manual_entry_exit_exempt` **完全零建模**（grep 零命中）
    #   ⇒ 最大的一块实盘仓位来源**从未被任何回测验证过**，而 2026-09-28 当日
    #   实盘亏损几乎全部来自观察篮（`down_day_exit_pct` 对它无效、
    #   趋势破位也无效，因为它 `manual_entry_exit_exempt=True` 只保留 −18% 硬止损）。
    # 建模内容（strategy/backtest_daily.py）：篮子票绕过动量/日线/评分闸门、
    #   **优先**占用 max_positions 槽位，剩余槽位才给动量候选；
    #   exempt=True 时豁免 trend_break / crash / timeout，只留 hard_stop。
    # 用法：`--mode consensus --track defect --baseline N1_观察篮实盘现状`，
    #   则 N2 的 dSh 直接就是「关闭退出豁免」的修复收益（正数=有效）。
    out["N1_观察篮实盘现状_豁免"] = replace(
        b, manual_entry_codes=PROD_MANUAL_ENTRY_CODES,
        manual_entry_exit_exempt=True)
    out["N2_观察篮_关闭退出豁免"] = replace(
        b, manual_entry_codes=PROD_MANUAL_ENTRY_CODES,
        manual_entry_exit_exempt=False)
    # N3 = 「清空观察篮」的理论上限：回测基线 P0 本身即等于该情形，
    #   但显式建一列才能在同一张表里直接读出「篮子的净成本」。
    out["N3_观察篮_仅2槽"] = replace(
        b, manual_entry_codes=PROD_MANUAL_ENTRY_CODES[:2],
        manual_entry_exit_exempt=True)
    out["N4_观察篮_关豁免_留20上限"] = replace(
        b, manual_entry_codes=PROD_MANUAL_ENTRY_CODES,
        manual_entry_exit_exempt=False, max_positions=5)
    return out


# ============================================================ 通用评估

def make_folds(data, codes, n, fold, nfolds):
    starts = list(range(FIXED_WARMUP, n - fold + 1, fold))[-nfolds:]
    subs = []
    for s in starts:
        lo, hi = s - FIXED_WARMUP, min(s + fold, n)
        subs.append((s, min(s + fold, n), slice_by_index(data, lo, hi)))
    return subs


def oos_stats(cfg, subs, bt):
    rs = [bt(cfg, d) for _s, _e, d in subs]
    ok = [r for r in rs if r and "error" not in r]
    if not ok:
        return None, rs
    shs = [r["sharpe"] for r in ok]
    return dict(
        mean_sharpe=sum(shs) / len(shs),
        mean_ret=sum(r["total_return"] for r in ok) / len(ok),
        mean_mdd=sum(r["max_drawdown"] for r in ok) / len(ok),
        pos=sum(1 for r in ok if r["total_return"] > 0),
        n=len(ok),
        worst=min(r["total_return"] for r in ok),
        rets=[r["total_return"] for r in ok],
    ), rs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=900)
    ap.add_argument("--fold", type=int, default=90)
    ap.add_argument("--folds", type=int, default=6)
    ap.add_argument("--mode", default="grid", choices=["grid", "compare", "consensus"])
    ap.add_argument("--param", default="reentry_cooldown")
    ap.add_argument("--values", default="3,5,8,10,15,20")
    ap.add_argument("--json", default="")
    # 【2026-09-23 OWNER 授权】晋升双通道：alpha（默认，防 churn）/ defect（缺陷修复）
    ap.add_argument("--track", default="alpha", choices=["alpha", "defect"],
                    help="alpha=闸门②要求最差窗口 dSh≥+0.10（防过拟合调参）；"
                         "defect=缺陷修复通道，闸门②放宽为『方向不劣化』"
                         "（均值 dSh≥0 且最差窗口≥-0.05），其余闸门照旧")
    # 反向对照：缺陷修复时「对照」应是实盘现状（如 G1/H1），「候选」是修复后的基线。
    # 默认以 P0 为对照会把修复收益算成负号、闸门①（IS 提升）也会判反。
    ap.add_argument("--baseline", default="P0_当前生产基线",
                    help="对照配置名（默认 P0_当前生产基线）。缺陷修复通道应设为"
                         "实盘现状代理（如 G1_轮动绕闸门_1.5 / H1_实盘现状_绕闸门+超仓6）")
    args = ap.parse_args()

    codes = wide_universe()
    t0 = time.time()
    data = preload(codes + [MARKET_INDEX_CODE, "000300.SH"], args.count)
    if not data:
        print("无数据，退出")
        return
    # 【2026-09-28 第 11 轮】折划分必须建立在**已对齐**的面板上（见 aligned_universe）
    d0, panel = aligned_universe(data, codes)
    if not d0:
        print("无数据（对齐失败），退出")
        return
    data = panel
    n = len(d0)
    print(f"[数据] {len(data)} 只 × {n} 根（已按日期对齐）  "
          f"{d0[0]} -> {d0[-1]}  载入 {time.time()-t0:.1f}s")

    def bt(cfg, dset):
        ks = [k for k in dset.keys() if k not in INDEX_CODES]
        return run_backtest(ks, cfg, count=args.count, preloaded=dset)

    subs = make_folds(data, codes, n, args.fold, args.folds)
    print(f"[折] {len(subs)} 折 × {args.fold} 根："
          + " ".join(f"F{i+1}:{d0[s][:6]}-{d0[e-1][:6]}" for i, (s, e, _d) in enumerate(subs)))

    base = base_cfg()
    base_is = bt(base, data)
    b_stats, base_rs = oos_stats(base, subs, bt)
    print(f"\n基线 P0: IS ret={base_is['total_return']*100:+.2f}% Sh={base_is['sharpe']:+.2f} "
          f"MDD={base_is['max_drawdown']*100:.2f}% | OOS 均值Sh={b_stats['mean_sharpe']:+.3f} "
          f"正收={b_stats['pos']}/{b_stats['n']} 均值MDD={b_stats['mean_mdd']*100:.2f}% "
          f"最差={b_stats['worst']*100:+.1f}%")

    # ---------------- grid ----------------
    if args.mode == "grid":
        raw = args.values.split(",")
        values = []
        for v in raw:
            v = v.strip()
            try:
                values.append(int(v) if v.lstrip("-").isdigit() else float(v))
            except ValueError:
                values.append(v)
        cur = getattr(base, args.param, "?")
        print(f"\n{'=' * 108}")
        print(f"OOS 网格扫描：{args.param}（当前={cur}）— 找参数高原，拒绝尖峰")
        print("=" * 108)
        print(f"{'值':<10}{'OOS均值Sh':>10}{'dSh':>8}{'均值ret':>10}{'均值MDD':>9}"
              f"{'正收':>7}{'最差折':>9}{'累计差pt':>10}  IS_ret    IS_Sh   IS_MDD")
        rows = []
        for v in values:
            cfg = replace(base, **{args.param: v})
            st, rs = oos_stats(cfg, subs, bt)
            if not st:
                print(f"{str(v):<10} ERROR")
                continue
            isr = bt(cfg, data)
            dsh = st["mean_sharpe"] - b_stats["mean_sharpe"]
            tot = sum((rs[k]["total_return"] - base_rs[k]["total_return"]) * 100
                      for k in range(len(rs))
                      if "error" not in rs[k] and "error" not in base_rs[k])
            mk = "  <=当前" if v == cur else ""
            print(f"{str(v):<10}{st['mean_sharpe']:>+10.3f}{dsh:>+8.3f}"
                  f"{st['mean_ret']*100:>+9.1f}%{st['mean_mdd']*100:>8.2f}%"
                  f"{st['pos']:>4}/{st['n']}{st['worst']*100:>+8.1f}%{tot:>+9.1f}  "
                  f"{isr['total_return']*100:>+7.1f}%{isr['sharpe']:>+8.2f}"
                  f"{isr['max_drawdown']*100:>8.2f}%{mk}")
            rows.append(dict(value=v, oos_sharpe=st["mean_sharpe"], d_sharpe=dsh,
                             oos_ret=st["mean_ret"], oos_mdd=st["mean_mdd"],
                             pos=st["pos"], n=st["n"], worst=st["worst"],
                             cum_diff_pt=tot, is_ret=isr["total_return"],
                             is_sharpe=isr["sharpe"], is_mdd=isr["max_drawdown"]))
        best = max(rows, key=lambda r: r["oos_sharpe"]) if rows else None
        if best:
            print(f"\n  -> OOS 最优值 = {best['value']}  dSharpe={best['d_sharpe']:+.3f} "
                  f"累计差={best['cum_diff_pt']:+.1f}pt  "
                  f"{'【达到 +0.10 闸门】' if best['d_sharpe'] >= 0.10 else '【未达 +0.10 闸门】'}")
        if args.json:
            Path(args.json).write_text(json.dumps(
                dict(param=args.param, base=dict(
                    oos_sharpe=b_stats["mean_sharpe"], oos_ret=b_stats["mean_ret"],
                    oos_mdd=b_stats["mean_mdd"], pos=b_stats["pos"], n=b_stats["n"],
                    worst=b_stats["worst"], is_ret=base_is["total_return"],
                    is_sharpe=base_is["sharpe"], is_mdd=base_is["max_drawdown"]),
                    rows=rows), ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[保存] {args.json}")
        return

    # ---------------- consensus（多窗口共识，防窗口运气）----------------
    if args.mode == "consensus":
        WINDOWS = [(60, 9), (75, 7), (90, 6), (120, 4)]
        track = args.track
        tests = candidates_final()
        print(f"\n{'=' * 116}")
        print("多窗口共识：同一候选在 4 套互不重叠的折划分下重跑，"
              "看最差窗口而非平均（防窗口运气）")
        if track == "defect":
            print(f"★ TRACK = DEFECT-REPAIR（缺陷修复通道，OWNER 2026-09-23 授权）："
                  f"闸门② 放宽为『方向不劣化』（均值dSh≥0 且最差窗口≥-0.05）；"
                  f"③④⑤⑥⑦⑧ 照旧")
        else:
            print("★ TRACK = ALPHA（默认）：闸门② 要求最差窗口 dSh ≥ +0.10")
        print("=" * 116)
        # 反向对照（--baseline）：缺陷修复通道下对照应是「实盘现状」代理，
        # 候选是修复后的 P0，此时 dSh 直接就是**修复收益**（正数=修复有效）。
        base_name = args.baseline
        if base_name not in tests:
            raise SystemExit(f"--baseline 无效：{base_name}；可选：{list(tests)}")
        base_cfg_used = tests[base_name]
        is_res = {name: bt(cfg, data) for name, cfg in tests.items()}
        bi = is_res[base_name]
        print(f"对照（baseline）= {base_name}"
              f"{'（反向对照：dSh 即修复收益）' if base_name != 'P0_当前生产基线' else ''}")
        print(f"基线 P0 IS: ret={bi['total_return']*100:+.2f}% Sh={bi['sharpe']:+.2f} "
              f"MDD={bi['max_drawdown']*100:.2f}%")
        res = {name: [] for name in tests}
        for fold, nf in WINDOWS:
            subs_w = make_folds(data, codes, n, fold, nf)
            b_st, b_rs = oos_stats(base_cfg_used, subs_w, bt)
            for name, cfg in tests.items():
                st, rs = oos_stats(cfg, subs_w, bt)
                tot = sum((rs[k]["total_return"] - b_rs[k]["total_return"]) * 100
                          for k in range(len(rs))
                          if "error" not in rs[k] and "error" not in b_rs[k])
                res[name].append(dict(win=f"{fold}x{nf}",
                                      d_sh=st["mean_sharpe"] - b_st["mean_sharpe"],
                                      oos_sh=st["mean_sharpe"],
                                      mdd=st["mean_mdd"], worst=st["worst"],
                                      pos=st["pos"], n=st["n"],
                                      cum=tot, base_sh=b_st["mean_sharpe"]))
        print(f"\n{'配置':<22}" + "".join(f"{w[0]}x{w[1]:<2}".rjust(11) for w in WINDOWS)
              + f"{'最差dSh':>10}{'均值dSh':>10}{'最差MDD':>10}{'最差折':>10}{'IS_ret':>10}{'IS_Sh':>8}  结论")
        final = []
        for name in tests:
            if name == base_name:      # 跳过对照自身（可能是 --baseline 指定的实盘现状）
                continue
            rs_ = res[name]
            line = f"{name:<22}"
            for r in rs_:
                line += f"{r['d_sh']:>+10.3f} "
            mn = min(r["d_sh"] for r in rs_)
            avg = sum(r["d_sh"] for r in rs_) / len(rs_)
            # ③ OOS 与 IS 的 Sharpe 差距：必须用**绝对值**比较，
            #    不是拿「相对基线的增量 dSh」去比 IS Sharpe。
            mean_oos_sh = sum(r["oos_sh"] for r in rs_) / len(rs_)
            wmdd = max(r["mdd"] for r in rs_)          # 负得最多
            wf = min(r["worst"] for r in rs_)
            isr = is_res[name]
            g1 = (isr["total_return"] > bi["total_return"]) or (isr["sharpe"] > bi["sharpe"])
            # ---- 【2026-09-23 OWNER 授权】晋升双通道 ----
            # ``--track alpha``（默认）：闸门② 要求最差窗口 dSh ≥ +0.10。
            #   该门槛是为**防伪 alpha churn** 设计的——阻止为了回测上的小幅增益
            #   反复调参而过拟合。
            # ``--track defect``：缺陷修复通道。+0.10 套在「恢复已验证基线行为」
            #   的缺陷修复上会**系统性阻断修复**（09-23 两项 P0 修复均因此被卡：
            #   +0.038~+0.087）。缺陷修复不改任何参数值、只消除「实盘行为与配置
            #   语义不符」，收益来自消除偏离而非寻找新 alpha，故闸门② 放宽为
            #   「方向不劣化」：均值 dSh ≥ 0 且最差窗口 dSh ≥ -0.05（不得有显著负窗口）。
            #   其余闸门（③④⑤⑥⑦⑧）**全部照旧保留**。
            #   人工判据（脚本无法自动判定，由 OWNER 在 EVOLUTION_DECISIONS.md 留痕）：
            #     DR-a 参数零改动（config/settings.py diff 为空）
            #     DR-b 实盘铁证（日志/账本级别证据）
            #     DR-c 守卫测试存在（tests 中有锁死新语义的用例）
            if track == "defect":
                g2 = (avg >= 0.0) and (mn >= -0.05)
            else:
                g2 = mn >= 0.10
            g4 = wmdd >= -0.22
            g5 = wf > -0.15
            gap = (abs(mean_oos_sh - isr["sharpe"]) / abs(isr["sharpe"])
                   if isr["sharpe"] else 9.99)
            g3 = gap <= 0.25
            ok = g1 and g2 and g3 and g4 and g5
            line += (f"{mn:>+10.3f}{avg:>+10.3f}{wmdd*100:>9.2f}%{wf*100:>+9.1f}%"
                     f"{isr['total_return']*100:>+9.1f}%{isr['sharpe']:>+8.2f}"
                     f"  {'>>> 通过' if ok else '否决'}")
            print(line)
            print(f"{'':<22} ③OOS/IS: 均值OOS_Sh={mean_oos_sh:+.3f} vs IS_Sh={isr['sharpe']:+.2f} "
                  f"→ 差 {gap*100:.1f}% {'PASS' if g3 else 'FAIL'}"
                  f" | TRACK={track} ①IS↑={'PASS' if g1 else 'fail'}"
                  f" ②={'均值≥0且最差≥-.05' if track == 'defect' else '最差≥+.10'}"
                  f"={'PASS' if g2 else 'fail'}"
                  f" ④最差MDD≥-22%={'PASS' if g4 else 'FAIL'} ⑤最差折>-15%={'PASS' if g5 else 'FAIL'}")
            final.append(dict(name=name, per_window=rs_, min_d_sh=mn, mean_d_sh=avg,
                              mean_oos_sharpe=mean_oos_sh,
                              worst_mdd=wmdd, worst_fold=wf, is_ret=isr["total_return"],
                              is_sharpe=isr["sharpe"], is_mdd=isr["max_drawdown"],
                              g1=g1, g2=g2, g3=g3, g4=g4, g5=g5, gap=gap, pass_gate=ok))
        if args.json:
            Path(args.json).write_text(json.dumps(
                dict(window=f"{d0[0]}-{d0[-1]}", n_bars=n,
                     base=dict(is_ret=bi["total_return"], is_sharpe=bi["sharpe"],
                               is_mdd=bi["max_drawdown"]), final=final),
                ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n[保存] {args.json}")
        return

    # ---------------- compare ----------------
    tests = candidates()
    print(f"\n{'=' * 124}\n【样本内 IS】全样本（含成本）\n{'=' * 124}")
    print(f"{'配置':<24}{'IS收益%':>10}{'IS_Sh':>8}{'IS_MDD%':>9}{'成交':>7}"
          f"{'胜率%':>8}{'持仓d':>7}{'暴露%':>8}{'alpha_pt':>10}")
    is_res = {}
    for name, cfg in tests.items():
        r = bt(cfg, data)
        is_res[name] = r
        if "error" in r:
            print(f"{name:<24} ERROR {r['error']}")
            continue
        print(f"{name:<24}{r['total_return']*100:>+9.2f}%{r['sharpe']:>+8.2f}"
              f"{r['max_drawdown']*100:>8.2f}%{r['n_trades']:>6}"
              f"{r['win_rate']*100:>7.1f} {r['avg_hold']:>6.1f}"
              f"{r['exposure']*100:>7.1f} {r['alpha']*100:>+9.2f}")

    print(f"\n{'=' * 124}\n【样本外 OOS】{len(subs)} 折逐折明细\n{'=' * 124}")
    print(f"{'配置':<24}" + "".join(f"{'F'+str(i+1):>10}" for i in range(len(subs)))
          + f"{'均值Sh':>9}{'正收':>7}{'均值MDD':>9}{'最差折':>9}{'均值ret':>9}")
    print("-" * 124)
    fold_res = {name: [bt(cfg, d) for _s, _e, d in subs] for name, cfg in tests.items()}
    base_rs = fold_res["P0_当前生产基线"]
    print(f"{'[基准]等权买入持有':<24}"
          + "".join(f"{r['bench_return']*100:>+9.1f}%" for r in base_rs))
    summary = []
    for name in tests:
        rs = fold_res[name]
        line = f"{name:<24}"
        shs, mdds, rets, pos, worst = [], [], [], 0, 999.0
        for r in rs:
            if "error" in r:
                line += f"{'ERR':>10}"
                continue
            line += f"{r['total_return']*100:>+9.1f}%"
            shs.append(r["sharpe"]); mdds.append(r["max_drawdown"])
            rets.append(r["total_return"])
            if r["total_return"] > 0:
                pos += 1
            worst = min(worst, r["total_return"])
        msh = sum(shs)/len(shs) if shs else 0.0
        mdd = sum(mdds)/len(mdds) if mdds else 0.0
        mret = sum(rets)/len(rets) if rets else 0.0
        line += f"{msh:>+9.3f}{pos:>4}/{len(rs)}{mdd*100:>8.2f}%{worst*100:>+8.1f}%{mret*100:>+8.1f}%"
        print(line)
        summary.append(dict(name=name, mean_sharpe=msh, pos=pos, n=len(rs),
                            mean_mdd=mdd, worst=worst, mean_ret=mret))

    print(f"\n{'=' * 124}\n逐折 vs 基线 P0（单位 pt）\n{'=' * 124}")
    print(f"{'配置':<24}" + "".join(f"{'F'+str(i+1):>10}" for i in range(len(subs)))
          + f"{'胜':>7}{'累计差':>10}")
    for s in summary:
        name = s["name"]
        if name == "P0_当前生产基线":
            continue
        rs = fold_res[name]
        line = f"{name:<24}"
        wins, tot = 0, 0.0
        for k, r in enumerate(rs):
            if "error" in r or "error" in base_rs[k]:
                line += f"{'-':>10}"
                continue
            d = (r["total_return"] - base_rs[k]["total_return"]) * 100
            tot += d
            if d > 0:
                wins += 1
            line += f"{d:>+9.1f}"
        line += f"{wins:>4}/{len(rs)}{tot:>+9.1f}pt"
        print(line)
        s["wins"] = wins
        s["cum_diff_pt"] = tot

    print(f"\n{'=' * 124}\n晋升闸门裁决\n{'=' * 124}")
    bs = next(s for s in summary if s["name"] == "P0_当前生产基线")
    bi = is_res["P0_当前生产基线"]
    print(f"基线 P0: IS ret={bi['total_return']*100:+.2f}% Sh={bi['sharpe']:+.2f} "
          f"MDD={bi['max_drawdown']*100:.2f}% | OOS 均值Sh={bs['mean_sharpe']:+.3f} "
          f"正收={bs['pos']}/{bs['n']} 均值MDD={bs['mean_mdd']*100:.2f}% "
          f"最差={bs['worst']*100:+.1f}%")
    print(f"{'配置':<24}{'dOOS_Sh':>9}{'②≥+.10':>9}{'③IS/OOS':>9}"
          f"{'④MDD≥-22':>10}{'⑤最差>-15':>10}{'①IS↑':>7}  结论")
    for s in summary:
        if s["name"] == "P0_当前生产基线":
            continue
        isr = is_res[s["name"]]
        d_sh = s["mean_sharpe"] - bs["mean_sharpe"]
        g2 = d_sh >= 0.10
        g4 = s["mean_mdd"] >= -0.22
        g5 = s["worst"] > -0.15
        g1 = (isr["total_return"] > bi["total_return"]) or (isr["sharpe"] > bi["sharpe"])
        is_sh, oos_sh = isr["sharpe"], s["mean_sharpe"]
        gap = abs(oos_sh - is_sh) / abs(is_sh) if is_sh else 9.99
        g3 = gap <= 0.25
        ok = g1 and g2 and g3 and g4 and g5
        print(f"{s['name']:<24}{d_sh:>+9.3f}{'PASS' if g2 else 'fail':>9}"
              f"{gap*100:>8.1f}%{'PASS' if g4 else 'FAIL':>10}"
              f"{'PASS' if g5 else 'FAIL':>10}{'PASS' if g1 else 'fail':>7}"
              f"  {'>>> 通过' if ok else '否决'}")
        s.update(dict(d_oos_sharpe=d_sh, g1=g1, g2=g2, g3=g3, g4=g4, g5=g5,
                      is_sharpe=is_sh, is_ret=isr["total_return"],
                      is_mdd=isr["max_drawdown"], gap=gap, pass_gate=ok))

    if args.json:
        Path(args.json).write_text(json.dumps(
            dict(window=f"{d0[0]}-{d0[-1]}", n_bars=n, fold=args.fold,
                 folds=len(subs), base=dict(is_ret=bi["total_return"],
                                            is_sharpe=bi["sharpe"],
                                            is_mdd=bi["max_drawdown"],
                                            oos_sharpe=bs["mean_sharpe"],
                                            oos_mdd=bs["mean_mdd"],
                                            oos_worst=bs["worst"],
                                            oos_pos=bs["pos"], oos_n=bs["n"]),
                 summary=summary), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[保存] {args.json}")


if __name__ == "__main__":
    main()

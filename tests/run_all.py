# -*- coding: utf-8 -*-
"""
自建测试 runner（不依赖 pytest，离线可用）

用法（在 conda env qmt 中）：
  python tests/run_all.py

【纪律（WP-M 2026-10-22）】测试绝不允许写生产 DB / 生产日志。
任何新测试必须先设 QMT_DB / QMT_NOTICES_LOG 指向临时目录，再 import 项目模块；
守卫见 tests/test_no_production_db_pollution.py。
（教训：2026-10-21 夜间夹具把 price=100.0 的伪造成交写进了生产 storage/qmt.db
的 fills / orders 表，已由 scripts/repair_test_pollution.py 清理。）
"""
from __future__ import annotations

import importlib
import inspect
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Windows 控制台 UTF-8（与 main.py 一致）。不加这段时，cp1252 控制台上
# 输出 ✓ / 中文会抛 UnicodeEncodeError，使整个 runner 在打印阶段就崩。
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ============================================================ 测试隔离
# 必须在**任何项目模块 import 之前**把数据库 / 系统提示日志重定向到临时目录。
#
# 为什么：原来单测直接写生产 ``storage/qmt.db`` 与 ``logs/notices.log``，把测试桩
# 注入了真实交易记录（生产 notices.log 里实测存在大量「理由=test」的买卖委托、
# 以及 18:19 的假熔断）。这些脏数据又反过来逆向驱动出了
# ``review_daily._in_trading_window()`` 那个“只统计 9—15 点熔断”的补丁——
# 在给症状打补丁，而不是治病。现在从源头隔离。
_TMP = Path(tempfile.mkdtemp(prefix="qmt_tests_"))
os.environ.setdefault("QMT_DB", str(_TMP / "test_qmt.db"))
os.environ.setdefault("QMT_NOTICES_LOG", str(_TMP / "test_notices.log"))
os.environ.setdefault("QMT_IGNORE_INTERPRETER_CHECK", "1")


def _collect_tests():
    mods = ["tests.test_indicators", "tests.test_data_models",
            "tests.test_risk", "tests.test_strategy",
            "tests.test_storage", "tests.test_ai_client",
            "tests.test_event_engine_live", "tests.test_review_daily",
            "tests.test_stock_names", "tests.test_mode_tagging",
            "tests.test_rotation",
            "tests.test_t1_sell_guard",
            "tests.test_batch_a_fixes", "tests.test_batch_c_fixes",
            "tests.test_batch_d_fixes",
            "tests.test_batch_b_t1",
            "tests.test_batch_b_minute",
            "tests.test_batch_e_daily_decision",
            "tests.test_daily_context_consistency",
            "tests.test_broker_reconnect",
            "tests.test_daily_fallback_exit",
            "tests.test_manual_entry",
            "tests.test_northbound",
            "tests.test_risk_zombie", "tests.test_web_freshness",
            "tests.test_sector_heat",
            "tests.test_risk_consec_batch",
            "tests.test_daily_stale_fallback",
            # 【2026-09-29 R13】日线陈旧度必须是**交易日**口径（自然日阈值隔个周末
            # 就放过 3 个交易日；实盘铁证：末根 09-24 / now 09-29 判为"新鲜"）。
            "tests.test_daily_stale_trading_days",
            "tests.test_universe_data_validity",
            # 【2026-09-26 R9】补注册：这两个文件此前**从未被执行**（不在 mods 里
            # ⇒ 用例隐形未执行，详见 run_all 顶部注释）。test_qmt_client_stale
            # 守的是行情新鲜度阈值，与本轮新增的陈旧守卫同源。
            "tests.test_qmt_client_stale",
            "tests.test_market_staleness_guard",
            # 【2026-10-07 AM-EVOLVE R20】陈旧报价盯市冻结守卫（P0：伪快照虚增权益）
            "tests.test_stale_valuation_guard",
            "tests.test_mark_price_fallback",
            "tests.test_live_order_price_type",
            "tests.test_total_asset_broker_authority",
            "tests.test_exit_exempt_declared",
            # 【2026-10-30 P1】持仓订阅 + 盯市价兜底：池外持仓（OWNER 手工建仓 /
            # 观察篮补买）原来收不到 tick，盯市价退回成本价 ⇒ 总资产低估 57.7%。
            "tests.test_position_subscription_mark",
            # 【2026-10-07 PM R21】账本「陈旧盯市污染」守卫：AM 已修「不再产生
            # 新污染」，但已被污染的历史快照仍在库里 ⇒ 10-08 的 prev_asset 会
            # 取到虚高的 1,020,446.6，账本将记一笔 −4.4% 的假亏损（同型 R14）。
            "tests.test_ledger_stale_mark_guard",
            # 【2026-10-07 PM R21】入场「宽度确认」闸门代理守卫：20 轮网格后第一个
            # 过关候选；锁死「默认 off 零变化 / 开启后真变 / 方向一致 / 两侧同值」。
            "tests.test_breadth_gate_proxy",
            # 【2026-10-14 PM-EVOLVE R31】弱市「相对动量门槛回退」研究开关守卫：
            # 本轮 REJECTED（OOS 最差窗 −0.399），但代理保留用于给「KPI#4 死锁」
            # 定价；锁死「默认 off 逐位不变 / 开启真变 / 剂量单调 / 陷阱形态 /
            # 生产未启用（无实盘读取点前禁落盘）」5 条。
            "tests.test_mom_rel_fallback",
            # 【2026-10-15 AM-EVOLVE R32】L2 杠杆基线下的「宽度 / 左尾」惰性图谱：
            # 两个候选均 REJECTED，但把两条轴的分辨率一次钉死 ——
            # ① max_positions ≥4 因 momentum_top_n=3 先截断而**逐位惰性**
            #   （⇒ 生产值 5 是死参数），只有 2/3 有分辨率；
            # ② hard_stop_pct −0.10…关闭 在 L2 下**仍全程惰性**，仅 −0.08 有反应
            #   且更差 ⇒ 证伪「L2 放大回撤后硬止损进入可达区间」的开工假设。
            "tests.test_l2_breadth_inertia",
            # 【2026-10-08 AM-EVOLVE R22】峰值自愈守卫：R20 只阻断「新增」盯市污染，
            # 但 10-07 休市伪快照已把 peak_equity 落库为 1,020,446.6（真实可信峰值
            # 1,005,860）⇒ 随状态恢复跨重启继承，回撤虚高 1.38pp、断路器提前触发。
            "tests.test_peak_equity_sanity",
            # 【2026-10-08 PM-EVOLVE R23】离场执行时点口径（exit_next_open）守卫：
            # 入场是「收盘信号→次日开盘成交」，但离场 trend_break 是「当日收盘判定
            # + 当日收盘成交」⇒ exit 侧少一根 bar。实测影响 ≈ 0（dSh +0.006），
            # 故 REJECTED 且保持 off；本守卫防止它被人悄悄改成 True 而改写基线。
            "tests.test_exit_next_open_proxy",
            # 【2026-10-09 AM-EVOLVE 第 24 轮】入场闸门 trend_up 放宽/收紧档守卫。
            # 半日度量铁证：连续两日开盘卖光后全天零 BUY；诊断显示近 90 日宽度闸门
            # 仅 54.4% 交易日满足、末 30 日 57% 全宇宙零通过。本轮实测：放宽档
            # 四窗 dSh −0.28~−0.66（灾难）、收紧档 ma60s −0.084~+0.050（噪声）
            # ⇒ 闸门严格度恰在最优，该轴闭合。守卫把「放宽必为负效应」钉成断言。
            "tests.test_trend_up_relax_proxy",
            # 【2026-10-09 PM-EVOLVE 第 25 轮】趋势模式 ATR 止损研究开关守卫。
            # 结构缺口：trend 分支从不检查入场时算好的 stop_price（回测/实盘两侧
            # 同为死代码）。实测：静态止损 k≥2.5 惰性、k=1.5 负 −0.089；吊灯止损
            # 严格单调负（4.0 −0.019 → 1.0 −1.551）⇒ 退出侧整类闭合。
            # 另钉死第 12 次同型口径漂移：验证器 fixed_amount 必须等于生产
            # max_order_amount（30万 vs 18万 ⇒ IS 收益虚高 34.5pt）。
            "tests.test_trend_stop_switch",
            # 【2026-10-12 AM-EVOLVE 第 26 轮】趋势破位「更慢离场」双开关守卫。
            # 25 轮里所有退出侧扫描只朝「更紧」一个方向走（静态 ATR 止损 k=1.5
            # −0.089 / 吊灯严格单调负 / hard_stop 惰性），其反面从未被测过。
            # 本守卫锁死：默认 1 / 0.0 零行为变化 + 开启后真变 + 方向为「更慢」。
            "tests.test_trend_break_slower_exit",
            # 【2026-10-10 SYNTHESIZE】入场侧半仓降级（entry_half_when_one）守卫。
            # 注：10-10 落盘时**漏注册**到 run_all.py ⇒ 4 例用例一直隐形未执行，
            # 本轮补登记（与「import 失败必须报错」同一类纪律）。
            "tests.test_entry_half_when_one",
            "tests.test_vwap_window_alignment",
            # 【2026-10-13 AM-EVOLVE 第 28 轮】动量闸门「正动量硬过滤」阈值守卫。
            # 起因是当日实盘铁证：7 次「宇宙无 60 日正动量标的」⇒ 该硬过滤才是
            # 连续 6 日零 BUY 的绑定约束。落盘 0.15（四窗最差 dSh +0.115）。
            # 锁死：默认 0.0 零行为变化 + 开启后真变 + 子集单调 + 生产/验证器同值。
            "tests.test_momentum_min_mom",
            "tests.test_pool_membership_no_lookahead",
            # 【2026-09-28 R10】验证器 base_cfg 与生产配置的口径漂移守卫。
            # 该坑已踩两次（09-18 top_n/rpt、09-28 northbound_mode）。
            "tests.test_evolve_baseline_sync",
            # 【2026-09-28 R11】① 北向闸门可信度守卫（数据是恒正成交额 ⇒ 死闸门）；
            # ② walk-forward 折窗口跨标的日期错位守卫（切片必须建立在已对齐面板上）。
            "tests.test_northbound_credibility",
            "tests.test_evolve_fold_alignment",
            "tests.test_down_day_exit_daily_scale",
            # 【2026-09-30 R14】合成行情（mock）禁止交易闸门守卫：
            # 夜间启动落 mock ⇒ 整个交易日按假价格成交（09-30 假亏 21 万元）。
            "tests.test_mock_data_trading_guard",
            # 【2026-10-08】日内熔断 abs/pct 取较宽松者守卫：¥5000(-0.51%) 使
            # -3% 成死代码且比单笔风险还小，约每 2~3 日冻结全天（09-24/09-28/10-08）。
            "tests.test_daily_loss_limit_union",
            # 【2026-10-14 AM-EVOLVE 第 30 轮】日内已实现盈亏「跨日复位」守卫（参数零改动）。
            # 实盘铁证：engine_state trade_date=2026-10-14 / daily_pnl=-7964.0 / 当日 0 笔
            # ⇒ _reset_daily_if_needed 复位了笔数·开盘基线·强平标志，却漏了 risk._daily_pnl，
            # 而引擎每天早晨重启 ⇒ 走恢复路径、进程内跨日清零分支永不执行 ⇒ 计数器永不复位。
            # 量化：-7,964 占生效阈值 max(5000,3%×972,533.6)=29,176 的 **27.3%**，且跨日累加。
            # 口径背离（第 14 例同型）：回测每 bar `_dl_day_pnl=0.0 日切归零`，实盘不清零。
            "tests.test_daily_pnl_crossday_reset",
            # 【2026-10-08 PM R23】杠杆/尺度型改动的判据守卫：Sharpe 与 Calmar 都是
            # 尺度不变量 ⇒ 「最差窗 dSh ≥ +0.10」会系统性否决一切仓位放大。
            # 20 轮网格无果的真正原因在此（ deficiency 在方法层不在策略层）。
            "tests.test_leverage_scale_invariance",
            # 【2026-10-13 PM-EVOLVE R29】路径 2 信念仓位（conviction）研究开关守卫。
            # 弱市/强市 AI 宽度不对称：仅在「宇宙内站上 MA 比例 ≥ 阈值」的强市态下
            # 对目标仓位乘 conviction_boost（1.5/2.0）。锁死：默认 off 零行为变化
            # + 开启后真变 + 杠杆型增量有界（Δret<2.0 且 ΔSh<0.5，Sharpe 不变量）。
            "tests.test_conviction_boost",
            # 【2026-10-16 PM-EVOLVE R35】信念仓位的 **live 引擎读取点**守卫。
            # 与上面 test_conviction_boost（回测侧建模）互补：钉死
            #   settings → daily_context.conviction_multiplier() → event_engine qty 乘子
            # 这条实盘链路真实存在且默认 off 恒 1.0；并拒绝「启用 mode 却留 boost<=1」
            # 的静默空操作陷阱（比回测侧多出的正是「实盘读点」这一层）。
            "tests.test_conviction_live_readpoint",
            # 【2026-09-30 R15】账本断供守卫：账本只依赖 OBSERVE 的 review JSON
            # ⇒ 三级单点故障让 KPI #4 连续 4 个交易日静默无法计算。
            "tests.test_ledger_db_fallback",
            # 【2026-09-30 R15】观察篮（manual_entry）回测代理守卫：这是本项目
            # 最贵的一条「实盘有、回测无」分支（IS Sh 1.71→1.02、MDD 翻倍）。
            # 门锁 2 专门防止它退化成「看起来建模了、其实没生效」的死代码。
            "tests.test_manual_entry_proxy",
            # 【2026-10-06 R17】板块轮动（rotation）回测代理守卫：最后一条
            # 「实盘有、回测无」的建仓分支（热板块候选显式绕过动量闸门）。
            "tests.test_rotation_proxy",
            # 【2026-10-06 R18】拼接口径（span）量测守卫：锁死「同一段行情的
            # 拼接 Sharpe 唯一」，即折均值口径的差异 100% 是划分假象；
            # 同时按代理守门锁死 entry_max_day_gain_pct 默认 off + 开启后真变。
            "tests.test_span_measure",
            # 【2026-10-08 S2】业绩预告上修提取（earnrev_cache）NaN 数据完整性守卫：
            # 旧实现 float(nan) or 0.0 返回 nan，json.dumps(nan) 写坏缓存。
            "tests.test_earnrev_cache",
            # 【2026-10-16 工程缺陷修复轮】可观测性 + 测试卫生守卫（5 个缺陷）：
            # ① sqlite 连接泄漏 —— record_ledger._conn 返回裸连接，而
            #    sqlite3.Connection 的 with 协议只管事务、从不 close() ⇒
            #    Windows 上临时库文件删不掉，16 个账本用例假失败
            #    （基线 461/478 里除 flask 外的全部失败都是它）；
            # ② review_daily.disconnect_n 把 paper 启动横幅
            #    「券商=disconnected」误判为断线（10-16 实盘 28 次 vs 真实 0 次，
            #    稳定分被扣满 25 分并产出误导性 P1 发现）；
            # ③ 行情陈旧守卫在时段边界（09:27 / 12:57 / 15:02）误报 ERROR；
            # ④ 无入场信号日志只报漏斗第 1 层（min_mom 文案谎报「无正动量」
            #    + 缺 trend_up 通过数 ⇒ 下次排查仍要从零复算）；
            # ⑤ min_daily_bias=2.0 超出 bias 值域 [-1,1] ⇒ bias 通道永不可达，
            #    却无任何启动播报（一次「看似无害」的改小就会从关闭跳成生效）。
            # 本模块锁死 15 条语义；**全部为零策略参数改动的工程修复**。
            "tests.test_observability_fixes_20261016",
            # 【AM-EVOLVE R37 2026-10-19】生产两条「风控降级通道」
            #   （连亏降仓阶梯 / 日内已实现亏损熔断）与回测代理的**语义对齐**守卫。
            #   背景：生产长期开启这两条通道，而验证器 P0 基线两者默认关闭
            #   ⇒ 第 15/16 例「实盘有、回测无」型口径背离（R37 实测价签：
            #     两条全开 vs P0：OOS 均值 dSh −0.017、最差 −0.058、
            #     IS 收益 +295.74%→+266.56%、IS MDD −13.05%→−15.61%）。
            #   本模块钉死「值对齐 / 基线关闭态 / 阶梯地板不可达 / union-max 语义」，
            #   **生产参数值零改动**。
            "tests.test_derisk_channel_parity",
            # 【2026-10-19 PM-EVOLVE 第 38 轮】生产「组合级回撤断路器」
            #   （risk/manager.py max_drawdown_pct=-0.20 / dd_recover_days=3
            #    冷却自愈 + 峰值重置）vs 回测 P0 默认关闭 = 第 **17** 例
            #   「实盘有、回测无」。本轮结论：**休眠的潜伏陷阱**——
            #   生产阈值下与 P0 逐位相同（IS MDD −13.05%，余量 ~7pt），
            #   但 −0.10 档 IS 收益 −15.6pt 且 **MDD 反而深 3.18pt**
            #   （与 R37 两条通道同向，第 3 条「降风险放大回撤」证据）。
            #   另钉死研究旋钮 equity_dd_stop 的**吸收态**缺陷
            #   （resume 轴完全惰性，两段式第二段按构造不可达）。
            "tests.test_rm_dd_pause_parity",
            # 【2026-10-20 AM-EVOLVE 第 39 轮】动量排名加权（momentum_weight）
            #   的**第 18 例「回测有、实盘无」**口径背离修复：wf 原本乘在
            #   max_single_position_pct 夹紧**之后**（回测可到 60% 单仓，
            #   实盘 PositionSizer 必夹回 40%）。现抽出纯函数把 wf 作用在
            #   风险预算上并置于 min(...) 之内 ⇒ 单仓上限恒夹紧。
            #   同时钉死 R39 裁决指纹（IS 收益下降 + 剂量单调劣化），
            #   并防止该未过闸旋钮被写进生产 STRATEGY_PARAMS。
            "tests.test_momentum_weight_cap_parity",
            # R40（2026-10-20 PM 收官）：钉死「KPI#2 正收折数是划分方式依赖的
            #   统计量」。实测同一条真实曲线（末 540 根）只改 fold 重切，
            #   正收比例在 50.0%~77.8% 摆动（极差 27.8pp），7 种划分无一达到
            #   85.7% 目标线；而拼接 Sharpe 恒为 2.1596（划分不变量）。
            #   ⇒ 后续轮次不得再把「正收差 2 折」当策略缺陷去调参。
            #   断言全部使用合成输入（第 19 条教训：钉契约不钉副产品）。
            "tests.test_kpi2_granularity_is_fold_dependent",
            # 【2026-10-21 优化实施】C1–C12 契约守卫：Position.available /
            # get_positions None 语义 / 对账不清仓 / paper 成本 / live 风控恢复 /
            # AI_VETO_MODE / 交易时段 09:30 / 外部缓存龄期。
            "tests.test_optimization_20261021",
            # 【2026-10-21 B1/B2】live 委托生命周期 + 卖出侧保护验收守卫
            # （对应 docs/OPTIMIZATION_SPEC_B1B2_2026-10-21.md §3 的 8 条）。
            # 注意：漏注册会让整文件用例隐形未执行（见本文件顶部「import 失败
            # 必须报错」纪律），故此处显式登记。
            "tests.test_order_lifecycle_b1b2",
            # 【2026-10-21 B4/B5】基准管道 + LLM 重排缓存/落库/触发条件验收守卫
            # （对应 docs/OPTIMIZATION_SPEC_B4B5_2026-10-21.md §3 的 6 条）。
            # 注意：漏注册会让整文件用例隐形未执行，故此处显式登记。
            "tests.test_benchmark_and_rerank_b4b5",
            # 【2026-10-21 B10】KPI 代码化守卫：config/kpi_targets.py 判定逻辑。
            "tests.test_kpi_gate",
            # 【2026-10-22】测试污染根治守卫（生产库/日志绝不被测试写入）。
            "tests.test_no_production_db_pollution",
            # 【2026-10-22 B9】回测真实性开关（涨跌停不可成交），默认关闭零行为变化。
            "tests.test_limit_up_block_flag",
            # 【2026-10-22 C5】显著性护栏（置换检验 / Deflated Sharpe）。
            "tests.test_significance",
            # 【2026-10-22 P0-1】regime 恢复快通道（默认关闭；关闭态逐位不变）。
            "tests.test_recovery_fastpath",
            # 【2026-10-22 P0-2】KPI gate 显著性严格模式（DSR 门槛）。
            "tests.test_kpi_gate_significance",
            # 【2026-10-23】入场闸门逐只明细（学习/排障面板后端）。
            "tests.test_gate_detail",
            # 【2026-10-28 R42 递归自进化第 1 轮】搜索空间契约守卫。
            #   背景：R41 把「回测无反应」当「参数无价值」，但其中一批参数在
            #   backtest_daily.py 里**根本没有读取点** ⇒ 那些「证伪」测的是空气。
            #   本模块钉住「每个生产参数被谁读」的显式清单：
            #   仅实盘 ⇒ 结论只能记「未验证」；死参数 ⇒ 任何网格纯浪费。
            #   含**正向对照**（占比 ≥40% + 关键旋钮点名），防止「检测器坏了、
            #   契约跟着坏」的合谋式假绿。零策略参数改动。
            "tests.test_search_space_contract",
            # 【2026-10-29】信号「未成交原因」blocked_by 守卫。
            #   起因：2026-10-29 早盘中芯国际 688981.SH 全天 24 轮 BUY（score 4.78）
            #   却零成交，真实原因是「宽度确认 <2 只」，但该信息只在 logger.info 里，
            #   signals 表与 Web 面板都显示裸 BUY ⇒ 用户只能反推且易误判成「策略没反应」。
            #   本模块钉死：字段默认空 / 旧库幂等迁移 / 补写不污染原始事实 /
            #   空入参不得抛错（该字段绝不允许影响交易路径）。
            "tests.test_signal_blocked_by",
            # 【2026-10-29】ETF 小额链路验证配置守卫。
            #   起因：OWNER 切 live 后要求「加一个小金额 ETF 验证系统功能」。
            #   三个静默失败点：① 验证标的混进候选池（会按 40% 单仓被买 ≈7,900 元）
            #   ② 订阅清单两份实现分叉（重启能收、重连收不到）③ 固定金额被手滑放大。
            #   本模块钉死：验证标的必在 STOCK_CODES 之外 / 订阅单一事实源 /
            #   fixed_amount 默认必须为 0（主路径零行为影响）。
            "tests.test_etf_validation_probe",
            # 【2026-10-29】miniQMT 交互健康面板守卫。
            #   用户顾虑「miniQMT 是否还在正常响应」，而最危险的失败模式是
            #   **静默降级为 mock**：不报错、不熔断，只悄悄禁掉全部成交
            #   （2026-09-30 曾因此按合成价成交，凭空造出 −213,354 元假亏损）。
            #   本模块钉死三条纪律：绝不抛异常 / 绝不发阻塞 IO / 判定顺序
            #   （mock 优先于行情陈旧），并端到端验证路由永不 500。
            "tests.test_qmt_health",
            # 【2026-10-29】「为什么没买」播报诚实性守卫。
            #   同日早盘 `_maybe_log_no_signal` 对中芯国际 688981.SH 输出
            #   「最高分仅 4.78（阈值 4.0）」——而 4.78 > 4.0，它**已过阈值**，
            #   真正拦它的是下游宽度确认（min_passing_candidates=2，本轮 1 只）。
            #   一个「仅」字把「被闸门挡住」说成「分数不够」，会把排查引向
            #   R40 已全档证伪的 buy_score_threshold。本模块钉死三条分流：
            #   已成交 ⇒ 静默（且不占节流）/ 有 BUY 零成交 ⇒ 只报拦截层 /
            #   无 BUY ⇒ 按策略真实 reason 判层，只有真低才允许说「不足」。
            "tests.test_no_entry_reason_honesty"]
    tests = []
    import_errors = []
    for m in mods:
        try:
            mod = importlib.import_module(m)
        except Exception as e:
            # 不能静默跳过：模块 import 失败意味着整文件的用例全部**隐形未执行**，
            # 而旧实现只打一行 [skip] 就继续、最后仍报 PASSED / 退出码 0。
            # 实测踩过：改动 storage.db 后两个测试模块静默消失，却显示全绿。
            import_errors.append((m, f"{type(e).__name__}: {e}"))
            print(f"[IMPORT FAIL] {m}: {type(e).__name__}: {e}")
            continue
        for name, fn in inspect.getmembers(mod, inspect.isfunction):
            if name.startswith("test_") and fn.__module__ == m:
                tests.append((m, name, fn))
    # 【2026-10-21 B4】把 EventEngine 的基准日度落盘目录重定向到临时目录。
    # 原因：``snapshot()`` 会起后台线程做基准计算，成功后向
    # ``LOG_DIR/benchmark_daily.jsonl`` 追加一行；LOG_DIR 是
    # ``BASE_DIR/logs``（硬编码、无环境变量开关），于是**任何调用 snapshot()
    # 的单测都会往生产 logs/ 写脏数据**（实测 test_stock_names 的
    # test_snapshot_exposes_intraday_pnl 即是元凶）。此处统一隔离。
    try:
        import engine.event_engine as _EE
        _tmp_logs = _TMP / "logs"
        _tmp_logs.mkdir(parents=True, exist_ok=True)
        _EE.LOG_DIR = str(_tmp_logs)
    except Exception as e:
        print(f"[隔离] EventEngine.LOG_DIR 重定向失败: {type(e).__name__}: {e}")
    return tests, import_errors


def main() -> int:
    print(f"[隔离] QMT_DB          = {os.environ['QMT_DB']}")
    print(f"[隔离] QMT_NOTICES_LOG = {os.environ['QMT_NOTICES_LOG']}\n")
    tests, import_errors = _collect_tests()
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
    if import_errors:
        print(f"\n⚠ IMPORT FAILURES: {len(import_errors)} 个测试模块未能加载（其用例全部未执行）：")
        for m, err in import_errors:
            print(f"    {m}: {err}")
    if errors:
        print("\n失败详情：")
        for mod, name, err in errors:
            print(f"\n--- {mod}.{name} ---")
            print(err)
    return 1 if (errors or import_errors) else 0


def _cleanup() -> None:
    """清理临时隔离目录（失败也不影响退出码）。"""
    try:
        shutil.rmtree(_TMP, ignore_errors=True)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        _rc = main()
    finally:
        _cleanup()
    sys.exit(_rc)
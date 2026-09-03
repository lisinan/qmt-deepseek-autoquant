# -*- coding: utf-8 -*-
"""
qmtIDE-deepseek 全局配置中心

所有可调参数集中在这里，模块通过 from config.settings import ... 读取。
约定：不要在各模块里硬编码路径 / 阈值 / 标的。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# ============================================================
# .env 加载（零依赖：conda qmt 未安装 python-dotenv）
# ============================================================
# 用法：在项目根目录放一个 .env 文件（已加入 .gitignore，勿提交真实密钥）：
#   OPENROUTER_API_KEY=sk-or-xxxxxxxx
#   # 可选：DEEPSEEK_API_KEY=sk-xxxxxxxx
# 规则：真实环境变量优先级最高；.env / .env.local 仅作本地兜底（不覆盖已存在的环境变量）。
# 这样 LLM 重排序 / AI 分析层无需改源码即可启用，且密钥永不进版本库。
def _load_dotenv() -> None:
    def _parse(path: Path) -> None:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if not k:
                continue
            # 去掉首尾引号
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            # 简单变量展开 ${VAR} / $VAR
            if "$" in v:
                v = re.sub(
                    r"\$\{([^}]+)\}|\$(\w+)",
                    lambda m: os.environ.get(m.group(1) or m.group(2), ""),
                    v,
                )
            # 已存在的真实环境变量优先，不覆盖
            os.environ.setdefault(k, v)

    # .env.local 优先于 .env（前者通常含本地覆盖）
    for _fname in (".env.local", ".env"):
        _parse(BASE_DIR / _fname)


_load_dotenv()

# ============================================================
# miniQMT (xtquant) 路径
# ============================================================
# xtquant / xtdata 所在目录。append 到末尾，避免 QMT 自带旧依赖
# (旧 werkzeug 等) 抢占 conda qmt env 的现代包。
# 可移植性：默认仍是本机路径，但允许通过环境变量 QMT_PYTHON_PATH 覆盖，
# 迁移到其它机器 / 其它 miniQMT 安装位置时无需改源码。
QMT_PYTHON_PATH = os.environ.get("QMT_PYTHON_PATH",
                                 r"C:\pazq_qmt\bin.x64\Lib\site-packages")
if Path(QMT_PYTHON_PATH).exists() and QMT_PYTHON_PATH not in sys.path:
    sys.path.append(QMT_PYTHON_PATH)

# miniQMT 用户数据目录（交易通道 XtQuantTrader 用）。同理可用环境变量覆盖。
QMT_USERDATA_PATH = os.environ.get("QMT_USERDATA_PATH",
                                    r"C:\pazq_qmt\userdata_mini")

# ============================================================
# Windows 下 sqlite3.dll 搜索路径修复
# ============================================================
if sys.platform == "win32":
    # 可移植性：conda env 位置允许用 QMT_CONDA_ENV 环境变量覆盖
    # （例如换机器后 conda 装在 D:\conda\envs\qmt）。
    _env_conda = os.environ.get("QMT_CONDA_ENV", "").strip()
    _dll_candidates = [
        Path(_env_conda) / "Library" / "bin" if _env_conda else None,
        BASE_DIR.parent / ".conda" / "envs" / "qmt" / "Library" / "bin",
        Path(os.environ.get("CONDA_PREFIX", "")) / "Library" / "bin",
        Path(sys.prefix) / "Library" / "bin",
    ]
    for _d in _dll_candidates:
        if not _d:
            continue
        if _d.is_dir() and (_d / "sqlite3.dll").exists():
            try:
                if hasattr(os, "add_dll_directory"):
                    os.add_dll_directory(str(_d))
            except Exception:
                pass
            try:
                _cur = os.environ.get("PATH", "")
                if str(_d) not in _cur:
                    os.environ["PATH"] = str(_d) + os.pathsep + _cur
            except Exception:
                pass
            break

# ============================================================
# 交易账户配置（真实账号写入 trading_config.json，不硬编码）
# ============================================================
TRADING_CONFIG_FILE = BASE_DIR / "trading_config.json"

# ============================================================
# OpenRouter (LLM / AI 分析层，免费模型)
# ============================================================
# 通过环境变量注入密钥，绝不硬编码：
#   PowerShell: $env:OPENROUTER_API_KEY='sk-or-...'
#   CMD:        set OPENROUTER_API_KEY=sk-or-...
#   或：项目根目录 .env 文件（已 gitignore）写入 OPENROUTER_API_KEY=sk-or-...
#   （.env 加载在文件顶部 _load_dotenv() 完成，环境变量优先于 .env）
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()

OPENROUTER_BASE_URL = os.environ.get(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
)

# 主模型：DeepSeek V3 免费版（OpenRouter 上 token 免费）。
# 可覆盖：OPENROUTER_MODEL=xxx
OPENROUTER_MODEL = os.environ.get(
    "OPENROUTER_MODEL", "deepseek/deepseek-chat-v3-0324:free"
)

# 主模型不可用时的免费候选（自动按序尝试）
OPENROUTER_FREE_FALLBACKS = [
    "deepseek/deepseek-chat-v3-0324:free",
    "deepseek/deepseek-r1:free",
    "meta-llama/llama-3.3-70b-instruct:free",
]

# ============================================================
# DeepSeek 直连 API（OpenAI 兼容协议）
# ============================================================
# 启用方式：设置环境变量 DEEPSEEK_API_KEY='sk-...'
#   端点固定 https://api.deepseek.com（DeepSeek 官方）
#   模型：deepseek-chat（V3）/ deepseek-reasoner（R1）
# 与 OpenRouter 共存：两者都有 key 时优先 DeepSeek（直连更稳）。
#   也可在根目录 .env 写入 DEEPSEEK_API_KEY=sk-...（环境变量优先）。
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_BASE_URL = os.environ.get(
    "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
)
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

# AI 请求超时 / 结果缓存
OPENROUTER_TIMEOUT = float(os.environ.get("OPENROUTER_TIMEOUT", "60"))
AI_CACHE_TTL_SEC = 300          # 同一输入 5 分钟内复用结果，省 token / 提速
AI_MAX_TOKENS = 600             # 分析输出上限

# ============================================================
# 标的池（Universe）
# ============================================================
UNIVERSE = {
    # 指数（市场环境参考，不直接交易）
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000300.SH": "沪深300",
    # 个股（策略候选池）
    "300308.SZ": "中际旭创",
    "300502.SZ": "新易盛",
    "300394.SZ": "天孚通信",
    "688256.SH": "寒武纪",
    "688981.SH": "中芯国际",
    "002371.SZ": "北方华创",
    "603019.SH": "中科曙光",
    "000977.SZ": "浪潮信息",
    "002230.SZ": "科大讯飞",
    "688111.SH": "金山办公",
    "002415.SZ": "海康威视",
    "300033.SZ": "同花顺",
}
# 指数代码集合
INDEX_CODES = {"000001.SH", "399001.SZ", "399006.SZ", "000300.SH"}
STOCK_CODES = {c for c in UNIVERSE if c not in INDEX_CODES}

# 市场环境过滤所用指数
MARKET_INDEX_CODE = "399006.SZ"

# ============================================================
# 策略参数（多因子趋势策略）
# ============================================================
STRATEGY_PARAMS = {
    # 均线
    "ma_short": 5,
    "ma_medium": 10,
    "ma_long": 20,
    # RSI
    "rsi_period": 14,
    "rsi_oversold": 30,
    "rsi_overbought": 70,
    # MACD
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    # KDJ
    "kdj_period": 9,
    # BOLL
    "boll_period": 20,
    "boll_std": 2.0,
    # ATR
    "atr_period": 14,
    # 信号阈值
    "buy_score_threshold": 4.0,     # 评分 >= 4 才考虑买入
    "volume_surge": 1.2,            # 量比阈值
    "min_signals": 3,               # 至少 3 个因子共振
    # 日线多周期闸门（MTF）
    "min_daily_bias": 0.2,          # 日线偏置 >= 该值才允许买入（trend_up 直接放行）
    # 【2026-09-02 P0 修正】日线数据缺失时的处置。
    #   原实现：features(code) 为 None 即 daily_ok=True「保守放行」。但 DailyContext
    #   只装载静态 STOCK_CODES，而候选池还包含 DYNAMIC_UNIVERSE 的 ~30 只活跃股 →
    #   动态池 100% 拿不到日线特征 → 日线闸门对它们**完全不生效**，等于只靠 1 分钟
    #   噪音下单。实盘证据（storage/qmt.db 2026-09-01）：601138.SH / 688347.SH /
    #   300604.SZ 等买入信号全部标记 [no-daily]。
    #   现在：① DailyContext 的 codes 动态合并动态池（见 EventEngine._daily_codes）；
    #        ② 本开关为 True 时，日线上下文「已就绪但查不到该标的」→ 拒绝入场，
    #           不再把「没数据」当成通行证。warmup（从未刷新成功）阶段仍放行，
    #           避免启动瞬间冻结引擎。设为 False 可回到旧的宽松行为（可逆）。
    "require_daily_data": True,
    # 动量排名作用域："all" = 静态+动态候选统一排名（修正后的正确语义）；
    #   "static" = 仅静态池排名、动态池无条件放行（旧行为，仅供回归对照）。
    "momentum_scope": "all",
    # 离场（ATR 自适应：实际止损/止盈距离在入场时按日线 ATR% 计算，
    #        下面 stop_loss / take_profit 仅作下限兜底）
    "stop_loss": -0.04,             # 固定止损下限（ATR 止损更远时以此保底）
    "take_profit": 0.12,            # 固定止盈下限（ATR 目标更远时以此保底）
    "atr_stop_mult": 2.5,           # 止损距离 = ATR% * 该倍数（波动越大止损越宽）
    "tp_atr_mult": 4.0,             # 止盈距离 = ATR% * 该倍数（让盈利按波动奔跑）
    "trailing_stop": -0.03,         # 移动止损：浮盈后从峰值回撤 -3% 离场
    "trailing_activation": 0.06,    # 移动止损激活阈值：浮盈 >= +6%
    "trailing_floor": -0.005,       # 移动止损下限（保本线，不低于成本-0.5%）
    "max_hold_days": 20,            # 最长持仓天数（scalp 模式；trend 模式见下）

    # ---- 退出范式（本次优化核心）----
    # "scalp" = 紧移动止损（原逻辑，剥头皮，吃小波动，易被趋势甩下）
    # "trend" = 趋势跟随（骑行至 MA20 下穿 exit_ma / 收盘破 exit_ma 才离场，
    #           仅用宽幅硬止损做灾难保护）。回测证明 trend 模式在趋势行情里
    #           收益捕获约为 scalp 的 3 倍，但回撤更大。
    "exit_mode": "trend",
    "trend_exit_ma": 60,            # 趋势破位判定均线（MA20 下穿它 / 收盘跌破它）
    "hard_stop_pct": -0.18,         # 趋势模式宽幅硬止损（仅灾难保护，默认 -18%）
    "trend_max_hold_days": 120,     # 趋势模式最长持仓（让大趋势充分奔跑）

    # 动量排名（规避死水股，只交易最强趋势）
    # 启用后，候选池只保留 60 日动量前 N 名（且动量必须为正）。
    "momentum_rank": True,
    "momentum_lookback": 60,        # 动量回看天数
    "momentum_top_n": 6,            # 只交易动量前 N 名
    "down_day_exit_pct": -99.0,     # 单日暴跌清仓阈值（-99 表示关闭）

    # ---- 市场环境过滤（regime filter）----
    # 【2026-08-29 重大修正，已落地】原以为闸门是「唯一稳健结构性改进」，但 2026-08-29C
    # 用 25 个连续滚动窗（2024-01→2026-06）+ 7 折 walk-forward 交叉验证（count=750）
    # 推翻该结论：在强趋势 AI 宇宙里闸门是**风险调整后收益的净拖累**。
    #   滚动窗：闸门 Sharpe 胜率 40%、alpha 胜率 40%；平均贡献 ret -3.29%、Sh -0.50、α -3.29pt、MDD +2.31%。
    #   7 折交叉验证：无闸门 P0 均值 Sharpe **+1.69**（正α 5/7）；有闸门 P1 **+0.85**（正α 4/7）；
    #                 闸门在 **7/7 折全部跑输**，累计落后 -67.6pt。
    # 机理：本宇宙强趋势，闸门「出得来、回不去」——弱市清仓后错过反弹，反复 whipsaw，
    #       保费（踏空）> 赔付（少跌）。早先「+0.38 Sharpe / OOS +24.6pt」系短窗(count=500)+
    #       旧基线(未优化 max_positions)低估「永远满仓」所致。
    # 决策：关闭 regime 闸门以追求收益。注意——仅移除「市场择时叠加层」，**个股级风控仍在**
    #       （trend MA60 破位离场 / 硬止损 -18% / ATR 自适应止损）+ RiskManager（日内亏 -3%或¥5000
    #       熔断、总回撤 -10% 全局暂停、连亏降仓）。闸门逻辑保留在引擎(_regime_ok)，需恢复只需
    #       把 regime_mode 改回 "index"。可逆、零代码改动。
    "regime_mode": "off",           # "off" 关闭 | "index" 指数MA门 | "breadth" 宽度门
    "regime_index": "399006.SZ",    # 判定所用指数（创业板指，关闭后仅作研究参考）
    "regime_ma": 60,                # 指数 MA 门（收盘 > MA60 视为市场健康）
    "regime_breadth_thresh": 0.5,   # 宽度门阈值（>=50% 个股站上 MA60 才放行）
    "regime_force_exit": False,     # 市场转弱（down）时强制清仓（regime_mode=off 时不会触发）
    # ---- 仓位（波动率目标）----
    # 【2026-09-02 说明】trend 退出范式下，sizing 用的止损距离与真实止损**不一致**：
    #   sizing:  stop_pct = max(|stop_loss|=4%, atr%×atr_stop_mult=2.5) ≈ 6.75%
    #   真实止损: wide   = max(|hard_stop_pct|=18%, atr%×6)             ≈ 18%
    # 于是名义 risk_per_trade=2% 的真实值约为 2% × 18/6.75 ≈ 5.3%。
    # 这**不是**可以随手改掉的 bug —— 已验证的回测基准（+162% / Sharpe 1.69）正是在
    # 该口径下跑出来的（backtest_daily.BacktestConfig.trend_vol_sizing 默认 False，
    # 即用紧止损算仓位、每笔顶到 30% 上限、靠现金夹紧实现「~满仓 5 只」）。
    # 因此这里**保持与回测一致**，把差异显式化为下面的开关，留给 walk-forward A/B：
    #   trend_vol_sizing=True → 改用真实趋势止损做风险平价（敞口会从 ~97% 降到 ~55%），
    #   必须先用 opt_harness 多折验证收益/Sharpe 再决定是否切换。
    # 无论开关取值，启动时都会如实播报「真实单笔风险」，避免 -25% 断路器被误校准。
    "trend_vol_sizing": False,
    "risk_per_trade": 0.02,         # 单笔风险占总资产比例（风险平价，原 1%→2%）
    "max_position_amount": 300000,  # 单标的最大仓位(元)（原 5万→30万，按信念放大）
    # 集中度上限（并发持仓数）：经 IS/OOS + 多折 walk-forward 双验证，
    # max_positions=5 显著优于 8（OOS +1.3%→+12.3%、Sharpe +0.24→+0.70、alpha 转正 +1.6pt；
    # 多折均值 Sharpe +0.84 为全部配置最高、最差折/平均 MDD 最优）。机制：只持动量前5名，
    # 剔除第6名（常为弱势误信号），提升持仓质量。2026-08-27 并入。
    "max_positions": 5,
}

# ============================================================
# 风控参数
# ============================================================
RISK_PARAMS = {
    "daily_loss_limit_pct": -0.03,      # 日内累计亏损 -3% 熔断
    "daily_loss_limit_abs": 5000,       # 日内累计亏损 ¥5000 熔断
    # 总资产回撤全局暂停（2026-08-30 修正）：
    #   原 -0.10 远低于本趋势策略自然回撤 ~-20%，会频繁触发且**永久停牌**
    #   （on_asset_update 熔断后无自动恢复），使回测 +162% 收益在实盘蜕变为 ~0%。
    #   放宽到 -0.25：仅真尾部崩盘（远超正常波动）才触发，平时不再干扰；
    #   配合 dd_recover_days 冷却自动恢复（重置基线），成为真「断路器」而非「杀死开关」。
    "max_drawdown_pct": -0.25,
    "dd_recover_days": 5,               # 回撤熔断后冷却 N 日自动恢复（重置风险基线）
    # 连续亏损 / 日亏熔断的冷却自动恢复天数（2026-08-30 补丁）：
    #   原实现中 consec_loss / daily_loss_abs 两类熔断在 _halt() 后**永久停牌**
    #   （on_asset_update 仅恢复 max_drawdown 类），属与 -0.10 回撤同一类「杀死开关」，
    #   会使实盘在连亏 5 笔后停止开仓、收益蜕变为 ~0%（2026-08-25 模拟盘已触发 consec_loss=5）。
    #   现统一为可恢复「断路器」：非回撤类冷却 halt_recover_days 日后自动解除并重置连亏/日内盈亏。
    "halt_recover_days": 1,             # 连续亏损/日亏熔断后冷却 N 日自动恢复（默认 1 日）
    "max_consecutive_losses": 3,        # 连续亏损 3 次降仓
    "max_consecutive_losses_halt": 5,   # 连续亏损 5 次暂停
    "max_single_position_pct": 0.19,    # 单标的占总资产上限（0.19×5=0.95 与现金夹紧上限自洽，消除每日风险预算告警）
    "max_daily_trades": 10,             # 日内最大交易次数
    "max_order_amount": 300000,         # 单笔金额上限（与 max_position_amount 对齐）
}

# ============================================================
# 运行参数
# ============================================================
REFRESH_INTERVAL = 3        # 行情轮询间隔（秒）
# ---- 交易时段守卫（2026-08-29 并入，修复 live 路径两个实测缺陷）----
# 缺陷1：非交易时段 xtdata 返回收盘快照（价格恒定），引擎照常按分钟聚合
#        「平价 bar」。收盘到次日开盘 1110 分钟 > bar 缓冲区 120 根 →
#        缓冲区被隔夜平价 bar 完整覆写 9.2 次，次日开盘时 120 根 bar 全同价，
#        MA5≡MA20 / ATR≈0，分钟级因子集体失效。
# 缺陷2：非交易时段占全天 83%，全速轮询+组合打分+行业评估纯属白烧 CPU。
SESSION_GUARD = True             # 非交易时段跳过行情/策略/下单
IDLE_REFRESH_INTERVAL = 60       # 非交易时段轮询间隔（秒），按 5s 分片可及时响应停止
SINGLETON_LOCK = True            # 单实例互斥（防重复启动引擎堆积成僵尸进程）
EXECUTION_MODE = "paper"    # "paper" 模拟 / "live" 真实下单
STRATEGY_MODE = "single"    # "single" 单标的 / "portfolio" 组合（Top-N）
INITIAL_CASH = 1_000_000.0  # paper 模式初始资金
DATA_SOURCE = "auto"        # "auto" 优先 xtdata，失败回退 mock；或强制 "mock"
AI_ENABLED = True           # 是否启用 AI 分析层（无 key/无网络时自动跳过）
AI_ASYNC = True             # AI 分析走后台线程，不阻塞行情主循环

# ============================================================
# 持久化粒度（数据库体积控制，2026-09-02 新增）
# ============================================================
# 背景：引擎原本把**每个候选股每 tick 的信号（包括 HOLD）**全部入库。
# 实测：signals 表 319 万行（单日最高 81.8 万行）、sector_recommendations 136 万行、
# risk_snapshots 28.8 万行，而真正的交易流水 fills 只有 301 行；qmt.db 涨到 1.38GB。
# 后果不是硬盘，而是盘后复盘的 ts 范围查询逐日变慢 + 人工无法直接看数据。
PERSIST_HOLD_SIGNALS = False   # HOLD 信号不入库（仍记 DEBUG 日志）；True = 旧行为
RISK_SNAPSHOT_MIN_INTERVAL = 300   # 风控快照最小写入间隔（秒）；状态变化时立即写

# ============================================================
# 分钟线预热（bar warmup，2026-09-02 新增）
# ============================================================
# 背景：``EventEngine._bars`` 是纯内存 deque，而 ``on_bars`` 要求 >=60 根。
# 于是每次进程重启 / 每天开盘都得先现场攒 60 根 1 分钟 bar 才能开始评分
# —— 等于每天前一小时是盘区（占交易时长 25%，而早盘恰是趋势股成交最活跃、
# 突破最多发的时段）。
BAR_WARMUP = True
# ❗ 新鲜度硬校验（必需，不可关）：本机 miniQMT 的本地 1m 缓存可能极其陈旧。
# 实测 2026-09-02 直读本地：300308.SZ 拿到的是 **07-22** 的 bar（早 6 周），
# 收盘价 1060.8 vs 真实 859.3（差 19%），其余 3 只完全无数据。
# 把这种数据灌进指标比不预热**更危险**，所以超龄一律拒用。
BAR_WARMUP_MAX_STALE_DAYS = 4      # 最后一根 bar 允许的最大日历龄（覆盖周末+假日）
# 本地无数据/数据陈旧时尝试 xtdata.download_history_data 补拉。
# 实测首次全量下载 ~9.75s/只，46 只约 7.5 分钟，因此：
#   ① 整个预热跑在**后台线程**，不阻塞引擎启动；
#   ② 按优先级（持仓 → 静态池 → 其余）处理，并受总时间预算约束。
BAR_WARMUP_DOWNLOAD = True
BAR_WARMUP_BUDGET_SEC = 240.0

# ============================================================
# Portfolio 组合策略参数
# ============================================================
PORTFOLIO_CONFIG = {
    "max_positions": 5,           # 同时持有最多标的数（与 STRATEGY_PARAMS.max_positions 对齐，2026-08-27 并入）
    "score_threshold": 4.0,       # 入场评分阈值
    "max_single_pct": 0.30,       # 单标的占总资产上限
    "cash_buffer_pct": 0.05,      # 保留现金比例（防满仓）
}

# ============================================================
# 自动重连参数
# ============================================================
RECONNECT_CONFIG = {
    "interval": 3.0,          # 初始重试间隔（秒）
    "max_interval": 60.0,     # 最大重试间隔（指数退避上限）
    "max_retries": 5,         # 连续失败重试次数（之后持续退避尝试）
}

# ============================================================
# Web 仪表板
# ============================================================
WEB_HOST = "127.0.0.1"
WEB_PORT = 5000
WEB_DEBUG = False

# ============================================================
# 日志
# ============================================================
LOG_DIR = BASE_DIR / "logs"
LOG_LEVEL = "INFO"

# ============================================================
# Tushare 基本面数据
# ============================================================
# token 读取优先级：环境变量 > trading_config.json > 空
# 推荐：写在 trading_config.json（已在 .gitignore），不硬编码到代码。
import json as _json
TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "").strip()
if not TUSHARE_TOKEN:
    try:
        if TRADING_CONFIG_FILE.exists():
            with TRADING_CONFIG_FILE.open("r", encoding="utf-8") as _f:
                _cfg = _json.load(_f)
            TUSHARE_TOKEN = (_cfg.get("tushare_token") or "").strip()
    except Exception:
        pass

TUSHARE_TIMEOUT = float(os.environ.get("TUSHARE_TIMEOUT", "15"))
TUSHARE_CACHE_TTL = 3600  # 基本面缓存 1 小时
TUSHARE_FUNDAMENTAL_FILTER = {
    "max_pe": 200.0,          # PE_TTM 上限
    "min_pe": 0.0,            # PE 下限（亏损股 PE 为负会被过滤）
    "max_pb": 30.0,           # PB 上限
    "min_roe": -50.0,         # ROE 下限（允许负值）
    "min_total_mv": 30.0,     # 最小总市值（亿元）
}

# ============================================================
# 动态候选池（data/dynamic_universe.py）
# ============================================================
# 从 Tushare 全市场按行业过滤，作为动态扩展候选池。
# 与静态 UNIVERSE 合并后传给 PortfolioStrategy。
DYNAMIC_UNIVERSE_CONFIG = {
    "enabled": True,
    # 关注的行业（与 SectorScorer 划分互补：前者是“宇宙”后者是“代表”）
    "industries": ["通信设备", "半导体", "IT设备"],
    # 过滤条件
    "min_list_days": 365,       # 上市至少 1 年
    "exclude_st": True,          # 排除 ST / *ST
    "exclude_bj": True,          # 排除北交所（订阅/资金门槛不同）
    # 活跃股票池：只订阅和评分这部分（免网络爆炸）
    "active_pool_size": 30,      # 按总市值取 Top-N
    "refresh_interval": 86400,   # 每天刷新（秒）
}

# ============================================================
# AI 产业链热度评分（sector_scorer.py）
# ============================================================
SECTOR_CONFIG = {
    "sectors": {
        "光模块": {
            "label": "光模块/光器件",
            "stocks": [
                ("300308.SZ", "中际旭创"),
                ("300502.SZ", "新易盛"),
                ("300394.SZ", "天孚通信"),
                ("300570.SZ", "太辰光"),
            ],
        },
        "AI芯片": {
            "label": "AI 芯片/GPU",
            "stocks": [
                ("688256.SH", "寒武纪"),
                ("688041.SH", "海光信息"),
                ("300474.SZ", "景嘉微"),
                ("603986.SH", "兆易创新"),
            ],
        },
        "晶圆代工": {
            "label": "晶圆代工/设备",
            "stocks": [
                ("688981.SH", "中芯国际"),
                ("002371.SZ", "北方华创"),
                ("688012.SH", "中微公司"),
            ],
        },
        "服务器算力": {
            "label": "服务器/算力/IDC",
            "stocks": [
                ("603019.SH", "中科曙光"),
                ("000977.SZ", "浪潮信息"),
                ("300383.SZ", "光环新网"),
                ("603881.SH", "数据港"),
            ],
        },
        "PCB互联": {
            "label": "PCB/高速互联",
            "stocks": [
                ("002916.SZ", "深南电路"),
                ("300476.SZ", "胜宏科技"),
                ("688008.SH", "澜起科技"),
            ],
        },
        "AI应用": {
            "label": "AI 应用/软件",
            "stocks": [
                ("002230.SZ", "科大讯飞"),
                ("688111.SH", "金山办公"),
                ("300033.SZ", "同花顺"),
                ("300496.SZ", "中科创达"),
                ("002415.SZ", "海康威视"),
            ],
        },
    },
    "refresh_interval": 30,
    "recommendation_pool_size": 5,
    "min_composite_for_pool": 4.5,
    # 推荐池空时自动降阈值（避免熊市/非交易时间永远 0 推荐）
    "adaptive_threshold": True,
    "min_threshold_floor": 1.5,
    "history_limit": 100,
}

# 个股 -> 所属 AI 产业链环节（供组合层"分行业分散持仓"使用）。
# 与 SECTOR_CONFIG 同源，避免重复维护；任何未列出的代码归入
# "other" 桶（不受 per-sector 上限约束，避免误杀）。
SECTOR_OF = {}
for _sec_key, _sec in SECTOR_CONFIG["sectors"].items():
    for _code, _name in _sec["stocks"]:
        SECTOR_OF[_code] = _sec_key

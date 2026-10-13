# -*- coding: utf-8 -*-
"""基线数值守卫的确定性数据源（tushare 面板）。

★ 背景（为什么需要它）
    回测器 ``strategy.backtest_daily.load_daily`` 优先用 **xtdata 本地日线**。
    而 xtdata 在自动化 shell / 非交易时段可达性不稳定：
      - 可达时，它的返回值与本项目权威回测源 **tushare** 相差约 1.5~2.2pt
        （R27 跨源对比：tushare +128.38% vs xtdata +129.87%；R28 配置下扩大到
        +2.17pt，已超出基线守卫的 ±0.02 容忍 ⇒ 误报失败）；
      - 不可达时，``qmt_client`` 回落 mock ⇒ ``load_daily`` 返回合成价且 date
        全为当天 ⇒ ``aligned_universe`` 对齐后只剩 1 根 ⇒ 回测直接崩溃。
    基线数值守卫（test_rotation_proxy / test_manual_entry_proxy /
    test_trend_stop_switch）的常量是 AM/PM 周期在 **tushare 权威源** 上校准的。
    若改用 xtdata 执行，会因数据源漂移而误报失败——这不是代码回归，是环境噪声。

★ 本模块的做法
    把守卫钉死到 **tushare 面板缓存**（storage/hist_cache/ts_panel_*.pkl，
    与 AM/PM 校准同源、且与 R27 的兜底基础设施共用同一份缓存），使基线守卫
    不随 miniQMT 状态波动。直接构造面板喂给 ``run_backtest(preloaded=)``，
    **不改全局 ``load_daily``**，故不会污染其它仍走 xtdata 的测试。

★ 复现口径（与守卫原 ``_is_result`` 完全一致，仅数据源不同）
    取末 900 天 → aligned_universe 对齐 → run_backtest(count=900, preloaded=)。
    实测（base_cfg + 20261013 tushare 面板）：1.319341 / 1.889634 / -0.070425 / 163
    == R28 校准常量 1.319320 / 1.889559 / -0.070443 / 163（逐位吻合）。

★ 维护提示
    若缓存被删/过期，请运行 ``python scripts/_pm_ts_panel_20261012.py`` 重新生成
    （需 TUSHARE_TOKEN 与网络）。若缓存末日大幅前移（>10 个交易日），守卫复现值会
    轻微漂移，届时由 AM/PM 周期按新面板重钉常量即可（与历史惯例一致）。
"""
from __future__ import annotations

import glob
import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_CACHE_DIR = ROOT / "storage" / "hist_cache"
_COUNT = 900
_MIN_DAYS = 880  # 每只标的要求的最小根数（面板应 ≥ 900）

_panel_cache: dict | None = None


def _pick_panel() -> dict:
    """选最新的、且覆盖全部 wide_universe 代码的 tushare 面板缓存。"""
    cands = sorted(_CACHE_DIR.glob("ts_panel_*.pkl"))
    if not cands:
        raise RuntimeError(
            "未找到 tushare 面板缓存（storage/hist_cache/ts_panel_*.pkl）。"
            "请先运行 python scripts/_pm_ts_panel_20261012.py 生成"
            "（需 TUSHARE_TOKEN 与网络），或确认缓存未被删除。")
    import strategy._evolve_wf as W  # 延迟导入，避免无谓的 xtdata 连接
    needed = set(W.wide_universe())
    problems = []
    for cf in reversed(cands):  # 从最新往旧找
        try:
            panel = pickle.loads(Path(cf).read_bytes())
        except Exception as e:  # noqa: BLE001
            problems.append(f"{Path(cf).name}: 读取失败 {e}")
            continue
        missing = [c for c in needed if c not in panel]
        short = [c for c in needed
                 if c in panel and len(panel[c]["close"]) < _MIN_DAYS]
        if not missing and not short:
            return panel
        problems.append(
            f"{Path(cf).name}: 缺 {missing} 过短 {short}")
    raise RuntimeError(
        "没有可用的 tushare 面板缓存覆盖全部所需代码：\n  " + "\n  ".join(problems))


def baseline_is_result(cfg) -> dict:
    """等价于守卫原 ``_is_result``，但数据来自确定性 tushare 面板。"""
    global _panel_cache
    if _panel_cache is None:
        _panel_cache = _pick_panel()

    import strategy._evolve_wf as W
    from strategy.backtest_daily import run_backtest

    codes = W.wide_universe()
    # 复刻 preload(code, COUNT) -> load_daily 取末 COUNT 天
    data = {}
    for c in codes:
        d = _panel_cache.get(c)
        if not d:
            raise RuntimeError(f"tushare 面板缺少代码 {c}，基线守卫无法构造数据")
        n = len(d["close"])
        k = min(n, max(_COUNT, 120))
        data[c] = {kk: list(vv[-k:]) for kk, vv in d.items()}

    _d0, panel = W.aligned_universe(data, codes)
    ks = [k for k in panel.keys() if k not in W.INDEX_CODES]
    return run_backtest(ks, cfg, count=_COUNT, preloaded=panel)

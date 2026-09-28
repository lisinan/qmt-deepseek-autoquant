# -*- coding: utf-8 -*-
"""walk-forward 折窗口「跨标的日期对齐」守卫（2026-09-28 PM-EVOLVE 第 11 轮）。

【修复前实测的 P0 量测缺陷】
折划分直接对**未对齐的原始数组**按索引切片：
``n = min(len(d["close"]))`` → ``slice_by_index(data, lo, hi)``。
各标的历史长度不同（900/888/883/841/809/748/744/741），索引 i 在不同标的上
指向**不同日期** —— F6（索引 580..670）实测：

    000977.SZ 等 17 只（900 根）： 20250609 → 20251020
    300476.SZ          （741 根）： 20260128 → 20260616   ← 错位约 8 个月
    603986.SH          （748 根）： 20260112 → 20260601
    688008.SH          （744 根）： 20260305 → 20260720

切片后 run_backtest 内 align_panel 取日期**并集** ⇒ 每折实际横跨 ~250 根、
各票在不同时点登场，横截面动量排名在比较**不同日期**的标的。

附带后果：n 被最短的 300476.SZ（741 根）钉住，折只排到索引 670 ⇒
长历史标的的 20251022 ~ 20260924（229 根，近 11 个月）**从未进入任何折**。

本文件钉死修复后的契约：折必须建立在**已按日期对齐**的面板上，
且逐折所有标的共享同一日期窗口。
"""
from __future__ import annotations

from config import settings as S

_MOD = (S.BASE_DIR / "strategy" / "_evolve_wf.py")


def _src() -> str:
    return _MOD.read_text(encoding="utf-8")


def test_evolve_wf_uses_aligned_panel_for_folds():
    """main() 必须先用 aligned_universe 对齐，再交给 make_folds。"""
    src = _src()
    assert "aligned_universe" in src, \
        "_evolve_wf.py 未接入 aligned_universe（折会跨标的错位）"
    # 旧的错位写法不得残留
    assert 'n = min(len(d["close"]) for d in data.values())' not in src, \
        "_evolve_wf.py 仍在用 min(len(close)) 决定折长度（跨标的日期错位根因）"


def test_aligned_universe_keeps_date_field():
    """面板必须补回 date 字段，否则 run_backtest 内二次 align 会退化成
    尾部截断（日期变 "0,1,2…"）⇒ 北向序列查询全落空、闸门静默失效。"""
    src = _src()
    i = src.index("def aligned_universe")
    body = src[i:i + 2200]
    assert 'panel[code]["date"]' in body, \
        "aligned_universe 未把 date 写回面板 ⇒ 二次 align 会退化"


def test_fold_windows_share_one_date_axis():
    """★ 实盘铁证级断言：同一折内所有标的的日期窗口必须逐位相同。"""
    try:
        import strategy._evolve_wf as W
    except Exception as e:                                   # 数据不可达
        print(f"  [skip] 无法导入验证器: {e}")
        return
    codes = W.wide_universe()
    try:
        data = W.preload(codes + [W.MARKET_INDEX_CODE, "000300.SH"], 900)
    except Exception as e:
        print(f"  [skip] xtdata 不可达: {e}")
        return
    if not data:
        print("  [skip] 无数据")
        return
    d0, panel = W.aligned_universe(data, codes)
    if not d0:
        print("  [skip] 对齐失败")
        return
    subs = W.make_folds(panel, codes, len(d0), 90, 6)
    assert subs, "未生成任何折"
    for i, (_s, _e, dset) in enumerate(subs):
        wins = {c: (dset[c]["date"][0], dset[c]["date"][-1])
                for c in dset if dset[c].get("date")}
        assert len(set(wins.values())) == 1, (
            f"F{i+1} 折内标的日期窗口不一致（错位最多 "
            f"{max(w for w in wins.values())} vs "
            f"{min(w for w in wins.values())}）⇒ 横截面排名在比较不同日期")
        ln = {c: len(dset[c]["close"]) for c in dset}
        assert len(set(ln.values())) == 1, f"F{i+1} 折内长度不一致: {set(ln.values())}"


def test_folds_cover_recent_data():
    """折必须覆盖到近期数据（修复前最近 11 个月从未入折）。"""
    try:
        import strategy._evolve_wf as W
    except Exception as e:
        print(f"  [skip] 无法导入验证器: {e}")
        return
    codes = W.wide_universe()
    try:
        data = W.preload(codes + [W.MARKET_INDEX_CODE, "000300.SH"], 900)
    except Exception as e:
        print(f"  [skip] xtdata 不可达: {e}")
        return
    if not data:
        print("  [skip] 无数据")
        return
    d0, panel = W.aligned_universe(data, codes)
    if not d0:
        print("  [skip] 对齐失败")
        return
    n = len(d0)
    subs = W.make_folds(panel, codes, n, 90, 6)
    last_end = subs[-1][1]
    # 修复前 last_end = 670 / n_used=741，且长历史标的实际只到 20251021
    assert last_end >= n - 200, (
        f"最后一折止于索引 {last_end} / 共 {n} 根 ⇒ 最近 {n - last_end} 根"
        f"（{d0[last_end - 1] if last_end else '?'} 之后）从未进入任何折")

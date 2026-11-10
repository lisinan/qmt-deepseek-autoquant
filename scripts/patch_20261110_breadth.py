# -*- coding: utf-8 -*-
"""2026-11-10 落盘补丁 · Batch 2（P1 收益：宽度闸门口径对齐回测）。

背景（第 20 例「回测有、实盘无」口径背离）：
    回测 ``strategy/backtest_daily.py:2104-2167`` 的宽度 = **全宇宙**通过入场门
    的票数；生产 ``engine/event_engine.py:2959`` 数的却是
    ``_apply_momentum_gate`` **top_n=3 截断之后**的 BUY 数 ⇒ 生产侧宽度数学上限
    恒为 3，``mom_pass_n<=1`` 时恒为 1 ⇒ ``min_passing_candidates=2`` 必然触发。
    实测今日 688981.SH 被挡 24 次（09:30:09 → 11:25:58）。

    价签（T2 实测，面板 911 根 20230119→20261028）：
      单窗 IS   生产口径 +268.19% / Sh 1.604 / MDD −13.12% / Calmar 20.4
                回测口径 +371.03% / Sh 1.707 / MDD −14.03% / Calmar 26.4
      四窗共识  **收益差均值 +11.56pt，四窗全正（+7.42/+10.57/+14.51/+13.76）**，
                window 级 t=+7.12；fold 级 +10.88pt，t=+3.53，CI[+5.29,+17.14]；
                jackknife 剔任意 1 折仍在 [+11.49,+17.19] ⇒ 非单点依赖。
      四道闸门  全 PASS；逐折指纹为**放大右尾**（正向增量全在大赚折 +27~+48pt），
                与已否决的 D8「削峰填谷」指纹相反。
      ★ 不可引用的数字：均值 Calmar 20.98→37.11 的**幅度**。逐折 Calmar 中位数只有
                +0.30，均值被「某折 MDD 极小」的小分母撑爆（60x9-F4 101.68→236.89）。
                Sharpe +0.101 亦不显著（t=+0.70，CI 跨 0）。Calmar/Sharpe 只作方向。
      ★ 已知残差：生产 `trend_up` 的 b 分量是 `close>MA60`、回测是 `MA20>MA60`
                ⇒ 回测是生产子集 ⇒ 修后生产宽度 **≥** 回测宽度。本批**不**夹带修它
                （`above_ma60` 与 `bias` 复用，改了会动 score，会把两项改善混成一个 dSh）。
      负面价签  75x7 单窗 ΔSharpe −0.129；全样本单窗 Calmar 4.98→4.24、
                MDD −9.12%→−13.05%（6 万本金约多担 2,380 元账面回撤）。

    回滚（无需改代码，改参数 + 重启）：``STRATEGY_PARAMS["breadth_gate_basis"]``
    置为 ``"truncated"`` 即逐位恢复 2026-11-10 之前的行为。

用法：
    python scripts/patch_20261110_breadth.py --dry-run
    python scripts/patch_20261110_breadth.py
    python scripts/patch_20261110_breadth.py --rollback
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "engine" / "event_engine.py"
SETTINGS = ROOT / "config" / "settings.py"
AUDIT = ROOT / "strategy" / "_param_audit.py"
RUNNER = ROOT / "tests" / "run_all.py"
LOG = ROOT / "logs" / "PATCH_20261110.log"
STAMP = "20261110-1330-b2"

PATCHES = [
    # ---------------------------------------------------------------- B-1 全量池快照
    (
        "B1-keep-pool-all-before-momentum-gate",
        ENGINE,
        '''        candidate_codes = set(STOCK_CODES)
        if self.dynamic_universe is not None:
            candidate_codes.update(self.dynamic_universe.active_codes)
        candidate_codes -= INDEX_CODES
        candidate_codes = self._apply_momentum_gate(candidate_codes)

        # 可观测性 + 轮换：满仓报踏空；4/5 起允许轮换补强空槽（不改变默认交易行为）''',
        '''        candidate_codes = set(STOCK_CODES)
        if self.dynamic_universe is not None:
            candidate_codes.update(self.dynamic_universe.active_codes)
        candidate_codes -= INDEX_CODES
        # 【2026-11-10 P0 第 20 例口径背离】动量截断**之前**的全量池快照。
        # 宽度闸门必须数它，而不是截断后的 top_n=3（详见下方 _min_breadth 段注释）。
        pool_all = set(candidate_codes)
        candidate_codes = self._apply_momentum_gate(candidate_codes)

        # 可观测性 + 轮换：满仓报踏空；4/5 起允许轮换补强空槽（不改变默认交易行为）''',
    ),
    # ---------------------------------------------------------------- B-2 宽度计算
    (
        "B2-compute-breadth-over-universe",
        ENGINE,
        '''        _min_breadth = int(STRATEGY_PARAMS.get("min_passing_candidates", 0) or 0)

        best = None   # 本轮最高分候选（可观测性用）''',
        '''        _min_breadth = int(STRATEGY_PARAMS.get("min_passing_candidates", 0) or 0)
        # 【2026-11-10 P0 第 20 例口径背离修复】宽度必须量「全宇宙通过入场门的
        # 票数」（回测 backtest_daily.py:2104-2167 的口径），而不是动量截断后的
        # BUY 数。原先 :2959 数 len(pending_buys) ⇒ 上限恒为 top_n=3，mom_pass_n<=1
        # 时恒为 1 ⇒ K=2 必然触发（今日 688981 被挡 24 次）。
        # 这里只做**计数**：不存信号、不进 pending_buys ⇒ 不改变任何落单行为。
        # 必须用 strategy.on_daily_features（纯函数、无副作用；它不查
        # momentum_min_mom，与回测 breadth_n 同义）。若改用
        # daily_context.top_momentum 会再叠一层 min_mom，等于把背离复现一遍。
        # 回滚：STRATEGY_PARAMS["breadth_gate_basis"] = "truncated"。
        _breadth_basis = str(
            STRATEGY_PARAMS.get("breadth_gate_basis", "universe") or "universe")
        _breadth_scoped_universe = bool(
            _min_breadth > 0 and _breadth_basis != "truncated")
        _universe_buy_sigs: Dict[str, object] = {}
        if _breadth_scoped_universe:
            for _c in pool_all:
                if _c in held_codes or not regime_ok:
                    continue
                try:
                    _f = self.daily.features(_c) if self.daily else None
                    _s = self.strategy.on_daily_features(_c, _c, _f)
                except Exception as _e:          # noqa: BLE001 — 计数失败不得阻断开仓
                    logger.debug("[宽度确认] 全量池评估失败 %s: %s", _c, _e)
                    continue
                if getattr(_s, "side", "") == "BUY":
                    _universe_buy_sigs[_c] = _s

        best = None   # 本轮最高分候选（可观测性用）''',
    ),
    # ---------------------------------------------------------------- B-3 判据换口径
    (
        "B3-gate-uses-breadth-n",
        ENGINE,
        '''        if _min_breadth > 0 and len(pending_buys) < _min_breadth:
            if pending_buys:
                logger.info("[宽度确认] 本轮 BUY 信号仅 %d 只 < %d ⇒ 当日不开新仓"
                            "（%s）", len(pending_buys), _min_breadth,
                            ",".join(s.code for s, _t, _i in pending_buys[:5]))
                # 把「被宽度确认拦下」写回信号行（此前只存在于日志里，
                # UI 与 DB 都是裸 BUY ⇒ 用户只能反推）。
                _reason = (f"breadth_lt_{_min_breadth}"
                           f"(本轮仅 {len(pending_buys)} 只 BUY < {_min_breadth})")''',
        '''        _breadth_n = (len(_universe_buy_sigs)
                      if _breadth_scoped_universe else len(pending_buys))
        if _min_breadth > 0 and _breadth_n < _min_breadth:
            if pending_buys:
                logger.info("[宽度确认] 本轮 BUY 信号仅 %d 只 < %d ⇒ 当日不开新仓"
                            "（口径=%s；%s）", _breadth_n, _min_breadth,
                            _breadth_basis,
                            ",".join(s.code for s, _t, _i in pending_buys[:5]))
                # 把「被宽度确认拦下」写回信号行（此前只存在于日志里，
                # UI 与 DB 都是裸 BUY ⇒ 用户只能反推）。
                # 前缀约定 breadth_lt_<N>( 保持不变（tests 按 startswith 搜）。
                _reason = (f"breadth_lt_{_min_breadth}"
                           f"(本轮仅 {_breadth_n} 只 BUY < {_min_breadth}"
                           f"；口径={_breadth_basis})")''',
    ),
    # ---------------------------------------------------------------- B-4 参数登记
    (
        "B4-settings-breadth-basis",
        SETTINGS,
        '''    "min_passing_candidates": 2,    # 入场宽度确认（0=关闭）''',
        '''    "min_passing_candidates": 2,    # 入场宽度确认（0=关闭）
    # 【2026-11-10 P0】宽度闸门的**度量口径**（第 20 例「回测有、实盘无」背离）：
    #   "universe"  = 动量截断前全量池里通过入场门的票数（= 回测口径，默认）
    #   "truncated" = 动量 top_n 截断后的 BUY 数（2026-11-10 之前的旧行为）
    # 旧口径下宽度上限恒为 top_n=3，mom_pass_n<=1 时恒为 1 ⇒ K=2 闸门必然触发
    # （2026-11-10 实测 688981.SH 被挡 24 次）。
    # 价签：四窗收益差均值 +11.56pt、四窗全胜、window 级 t=+7.12（四窗全正）。
    #   ★ Calmar/Sharpe 只作方向：逐折 Calmar 的小分母会把均值撑爆
    #     （某折 MDD 3.8% ⇒ Calmar 236.89），Sharpe +0.101 亦不显著。
    # 已知残差：生产 trend_up 用 close>MA60、回测用 MA20>MA60 ⇒ 修后生产宽度 ≥ 回测。
    #   详见本文件模块 docstring；**本批刻意不夹带修它**。
    # 回滚：改回 "truncated" + 重启即逐位恢复旧行为。
    "breadth_gate_basis": "universe",''',
    ),
    # ---------------------------------------------------------------- B-5 契约登记
    (
        "B5-register-live-only-contract",
        AUDIT,
        '''    "owner_order": "live-only OWNER 指令单（回测无外部指令队列概念）",
}''',
        '''    "owner_order": "live-only OWNER 指令单（回测无外部指令队列概念）",
    # 【2026-11-10】宽度闸门的**度量口径**开关。它本身只在实盘引擎里读
    #   （回测器没有这个开关，因为它本来就按 "universe" 口径算 breadth_n）。
    #   ★ 但注意：本键与 owner_* 不同 —— 被它调用的**语义**（宽度 = 全宇宙通过
    #   入场门的票数）在回测里是**已实现且可验证**的（backtest_daily.py:2104-2167），
    #   所以「生产改为与回测同口径」这件事有四窗共识背书：收益差均值 +11.56pt、
    #   四窗全正、window t=+7.12（Calmar 幅度不可引用）。本键只是**回滚开关**，
    #   默认 "universe"。
    #   回滚："truncated" ⇒ 逐位恢复 2026-11-10 之前的旧口径行为。
    "breadth_gate_basis": "live-only 宽度口径回滚开关（默认 universe；"
                          "语义由回测 side 的四窗共识验证）",
}''',
    ),
    # ---------------------------------------------------------------- B-6 守卫测试入册
    (
        "B6-register-guard-tests",
        RUNNER,
        '''            "tests.test_fixed_lots"]''',
        '''            "tests.test_fixed_lots",
            # 【2026-11-10 主理人】本批落盘的反向断言：峰值基线不被熔断恢复下移 /
            #   回撤熔断解除后不立即再熔断 / peak 兜底按 mode 过滤 / 未知态占位
            #   按墙钟自解且先于 get_trades / 宽度闸门用全量池口径。
            "tests.test_20261110_landing_guards"]''',
    ),
]


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def phase1_validate() -> bool:
    ok = True
    cache = {}
    for pid, path, old, _new in PATCHES:
        text = cache.get(path)
        if text is None:
            text = path.read_text(encoding="utf-8")
            cache[path] = text
        n = text.count(old)
        if n != 1:
            ok = False
            print("[FAIL] %-40s 锚点 %d 次      %s" % (pid, n, _rel(path)))
        else:
            print("[ OK ] %-40s 锚点唯一       %s" % (pid, _rel(path)))
    return ok


def phase2_apply() -> bool:
    files = sorted({p for _i, p, _o, _n in PATCHES}, key=lambda x: str(x))
    backups = []
    for f in files:
        bak = f.with_suffix(f.suffix + ".bak-" + STAMP)
        shutil.copy2(str(f), str(bak))
        backups.append((f, bak))
        print("[BAK ] %s -> %s" % (_rel(f), bak.name))

    by_file = {}
    for pid, path, old, new in PATCHES:
        by_file.setdefault(path, []).append((pid, old, new))
    for path, items in by_file.items():
        text = path.read_text(encoding="utf-8")
        for pid, old, new in items:
            assert text.count(old) == 1, "%s 锚点异常" % pid
            text = text.replace(old, new)
        path.write_text(text, encoding="utf-8")
        print("[WROT] %s（%d 处）" % (_rel(path), len(items)))

    bad = []
    for pid, path, old, new in PATCHES:
        text = path.read_text(encoding="utf-8")
        if new not in text:
            bad.append("%s 新文本未落盘" % pid)
        if old not in new and text.count(old):
            bad.append("%s 旧文本仍有残留" % pid)
    if bad:
        print("[FAIL] 复核失败：" + "; ".join(bad))
        return False
    print("[ OK ] 复核通过：%d 条补丁全部在位" % len(PATCHES))

    import py_compile
    for f in files:
        try:
            py_compile.compile(str(f), doraise=True)
        except Exception as e:                       # noqa: BLE001
            print("[FAIL] 语法检查失败 %s: %s" % (_rel(f), e))
            return False
    print("[ OK ] 语法检查通过（%d 个文件）" % len(files))

    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write("\n=== %s Batch2 落盘 ===\n" % datetime.now().isoformat())
        for pid, path, _o, _n in PATCHES:
            fh.write("  %-40s %s\n" % (pid, _rel(path)))
        for f, bak in backups:
            fh.write("  备份: %s -> %s\n" % (_rel(f), bak.name))
    print("[ OK ] 日志 -> %s" % _rel(LOG))
    return True


def rollback() -> bool:
    files = sorted({p for _i, p, _o, _n in PATCHES}, key=lambda x: str(x))
    done = 0
    for f in files:
        bak = f.with_suffix(f.suffix + ".bak-" + STAMP)
        if bak.exists():
            shutil.copy2(str(bak), str(f))
            print("[REV ] %s <- %s" % (_rel(f), bak.name))
            done += 1
        else:
            print("[SKIP] %s 无备份" % _rel(f))
    print("回滚 %d 个文件" % done)
    return done > 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    args = ap.parse_args()
    if args.rollback:
        return 0 if rollback() else 2
    print("== Phase 1 锚点校验（只读）==")
    if not phase1_validate():
        print("\n[ABORT] 有锚点不唯一 ⇒ 一个字节都没写。")
        return 2
    if args.dry_run:
        print("\n[DRY-RUN] 全部锚点唯一，可落盘。未写入任何文件。")
        return 0
    print("\n== Phase 2 落盘 ==")
    return 0 if phase2_apply() else 3


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""2026-11-10 落盘补丁 · V7：日内硬止损连续确认去抖。

## 为什么

2026-11-10 14:17 实盘事故：`14:17:21` 买入 510500 500 股（3,347 元），
`14:17:36` 的资产更新里「资金已扣、持仓未刷新」⇒ 总资产少算一个持仓
⇒ 读出 `daily_stop −7.04%`（真实约 −1.7%）⇒ 置 `_flatten_requested` 并熔断，
随后每 ~4.5s 空转一次强平直到收盘（实测未再下真单，但这是运气）。

同日 13:52 的对照：OWNER 指令单 SELL 100 实为 500（丢 lots）已被 OWNER 修掉，
本补丁与之无关，勿混淆。

## 做法

引入 `RiskManager.DAILY_STOP_CONFIRM_ROUNDS = 3`（类常量，不新增可调参数，
避免触发参数契约登记，也避免被当成旋钮重扫）：

- 每轮 `on_asset_update` 维护 `_daily_stop_streak`：越线 +1、回到线内清零；
- 只有连续 N 轮都越线才置 `_flatten_requested` 并熔断。
- 主循环约 4.5s/轮 ⇒ 3 轮 ≈ 13.5s，足以跨过「买入在途」窗口；
  真实日内崩跌持续越线，13.5s 后仍会强平，保护不失效。

## 既有测试的同步（不是放松断言，是改成「持久越线仍然触发」）

4 条测试原来喂 1~2 轮就断言触发，语义已变，必须同步为喂满 N 轮：

  tests/test_risk.py::test_daily_hard_stop_flatten_triggers
  tests/test_risk.py::test_daily_hard_stop_flatten_reset_on_resume
  tests/test_mark_price_fallback.py::test_cost_fallback_would_have_halted
  tests/test_risk_asset_trust.py::test_positive_control_old_path_still_halts  ← 正向对照

★ 正向对照的意图必须保住：喂「拼装坏口径」时必须仍能熔断（只是要连续 N 轮）。
   若有人把阈值改松，它仍会红。

用法：
    python scripts/patch_20261110_v7.py --dry-run
    python scripts/patch_20261110_v7.py
    python scripts/patch_20261110_v7.py --rollback
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RISK = ROOT / "risk" / "manager.py"
LOG = ROOT / "logs" / "PATCH_20261110.log"
STAMP = "20261110-2015-v7"

PATCHES = [
    # ---------------------------------------------------------------- W1 类常量
    (
        "W1-confirm-rounds-constant",
        RISK,
        '''class RiskManager:
    def __init__(self, params: Optional[dict] = None):''',
        '''class RiskManager:
    # 【2026-11-10 P0】日内强平必须连续 N 轮确认才动作。
    # 动机：买入成交后的亚秒窗口里「资金已扣、持仓未刷新」会让总资产瞬间少算
    # 一个持仓 —— 2026-11-10 14:17:21 买 510500 500 股（3,347 元）后，14:17:36
    # 即读出 daily_stop −7.04%（真实约 −1.7%）⇒ 假熔断 + flatten_requested，
    # 随后每 ~4.5s 空转强平到收盘。主循环约 4.5s/轮 ⇒ 3 轮 ≈ 13.5s，
    # 足以跨过该窗口；真实崩跌持续越线，13.5s 后仍会强平。
    # 用类常量而非 RISK_PARAMS 键：不触发参数契约登记，也不当旋钮重扫。
    DAILY_STOP_CONFIRM_ROUNDS = 3

    def __init__(self, params: Optional[dict] = None):''',
    ),
    # ---------------------------------------------------------------- W2 计数初始化
    (
        "W2-streak-init",
        RISK,
        '''        self._flatten_requested: bool = False''',
        '''        self._flatten_requested: bool = False
        # 连续越线轮数（见 DAILY_STOP_CONFIRM_ROUNDS）：越线 +1、回到线内清零。
        self._daily_stop_streak: int = 0''',
    ),
    # ---------------------------------------------------------------- W3 去抖判据
    (
        "W3-debounce-decision",
        RISK,
        '''            if self._day_open_asset > 0:
                dlp = (total_asset - self._day_open_asset) / self._day_open_asset
                if dlp <= self.p.get("daily_stop_flatten_pct", -0.06):
                    if not self._flatten_requested:''',
        '''            if self._day_open_asset > 0:
                dlp = (total_asset - self._day_open_asset) / self._day_open_asset
                # 【2026-11-10 P0】连续确认去抖（见 DAILY_STOP_CONFIRM_ROUNDS）。
                # 单轮瞬态读数（买入在途少算一个持仓）**不得**强平真仓。
                if dlp <= self.p.get("daily_stop_flatten_pct", -0.06):
                    self._daily_stop_streak += 1
                else:
                    self._daily_stop_streak = 0
                if (dlp <= self.p.get("daily_stop_flatten_pct", -0.06)
                        and self._daily_stop_streak
                        >= self.DAILY_STOP_CONFIRM_ROUNDS):
                    if not self._flatten_requested:''',
    ),
    # ---------------------------------------------------------------- W4 日切清零
    (
        "W4-streak-reset-daily",
        RISK,
        '''            self._flatten_requested = False
            self._day_open_asset = 0.0''',
        '''            self._flatten_requested = False
            self._daily_stop_streak = 0
            self._day_open_asset = 0.0''',
    ),
]


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def phase1_validate() -> bool:
    ok = True
    text = RISK.read_text(encoding="utf-8")
    for pid, _path, old, _new in PATCHES:
        n = text.count(old)
        if n != 1:
            ok = False
            print("[FAIL] %-32s 锚点 %d 次" % (pid, n))
        else:
            print("[ OK ] %-32s 锚点唯一" % pid)
    return ok


def phase2_apply() -> bool:
    bak = RISK.with_suffix(RISK.suffix + ".bak-" + STAMP)
    shutil.copy2(str(RISK), str(bak))
    print("[BAK ] %s -> %s" % (_rel(RISK), bak.name))

    text = RISK.read_text(encoding="utf-8")
    for pid, _path, old, new in PATCHES:
        if text.count(old) != 1:
            print("[FAIL] %s 内存态锚点不唯一 ⇒ 中止（未写文件）" % pid)
            return False
        text = text.replace(old, new)
    RISK.write_text(text, encoding="utf-8")
    print("[WROT] %s（%d 处）" % (_rel(RISK), len(PATCHES)))

    bad = []
    for pid, _path, old, new in PATCHES:
        cur = RISK.read_text(encoding="utf-8")
        if new not in cur:
            bad.append("%s 新文本未落盘" % pid)
        if old not in new and cur.count(old):
            bad.append("%s 旧文本残留" % pid)
    if bad:
        print("[FAIL] 复核失败：" + "; ".join(bad))
        return False
    print("[ OK ] 复核通过：%d 条在位" % len(PATCHES))

    import py_compile
    try:
        py_compile.compile(str(RISK), doraise=True)
    except Exception as e:                           # noqa: BLE001
        print("[FAIL] 语法检查失败: %s" % e)
        return False
    print("[ OK ] 语法检查通过")

    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write("\n=== %s V7 落盘 ===\n" % datetime.now().isoformat())
        for pid, _p, _o, _n in PATCHES:
            fh.write("  %s\n" % pid)
        fh.write("  备份: %s\n" % bak.name)
    print("[ OK ] 日志 -> %s" % _rel(LOG))
    return True


def rollback() -> bool:
    bak = RISK.with_suffix(RISK.suffix + ".bak-" + STAMP)
    if not bak.exists():
        print("[SKIP] 无备份 %s" % bak.name)
        return False
    shutil.copy2(str(bak), str(RISK))
    print("[REV ] %s <- %s" % (_rel(RISK), bak.name))
    return True


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

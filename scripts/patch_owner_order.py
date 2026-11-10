# -*- coding: utf-8 -*-
"""给 engine/event_engine.py 挂载 OWNER 指令单通道（默认 dry-run）。

设计取向：**引擎侧只加一个挂载点**，逻辑全在 `core/owner_order.py`。
这样补丁面积最小（约 25 行），回滚也只是删掉这一段。

用法：
    python scripts/patch_owner_order.py                 # dry-run，只打印将要插入的位置
    python scripts/patch_owner_order.py --apply         # 真正写盘（先备份、后编译校验）
    python scripts/patch_owner_order.py --apply --root logs/_tmp/rehearsal   # 对副本演练

四道自检（沿用 B1/B2 的做法）：
  1. 锚点必须**恰好命中 1 次**（否则说明源码已漂移，直接拒绝）
  2. 插入后 `compile()` 必须通过
  3. 反向还原必须逐字节回到原文件
  4. 幂等：已挂载则直接退出 0，不重复插入
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

MARK = "# ==== OWNER 指令单挂载点（由 scripts/patch_owner_order.py 插入）===="

ANCHOR_REENTRY = ("        self._owner_reentry_step(ticks, "
                  "{c: t.price for c, t in ticks.items()})\n")

METHOD = '''    # ==== OWNER 指令单挂载点（由 scripts/patch_owner_order.py 插入）====
    def _owner_order_step(self, ticks, current_prices) -> None:
        """执行 OWNER 手工指令（storage/owner_orders.jsonl）。

        与既有两条通道的区别：manual_entry 跳过已持仓、owner_reentry 只补空仓，
        两条都**故意**拒绝加仓；本通道允许加仓，也允许卖出。

        红线（一条都不放开）：落单只走 ``_handle_buy`` / ``_handle_sell``，
        因此熔断、日内次数、现金夹紧、行情新鲜度、总资产可信度、T+1、
        建仓保护期、max_positions 全部照旧生效。逻辑本体在 core/owner_order.py。

        关闭时（STRATEGY_PARAMS["owner_order"]["enabled"] 为假）立即返回，
        不读文件、不打日志 —— 这是本改动的第一验收项。
        """
        cfg = STRATEGY_PARAMS.get("owner_order") or {}
        if not isinstance(cfg, dict) or not cfg.get("enabled"):
            return
        try:
            from core.owner_order import run_step as _owner_run_step
            _owner_run_step(
                self, ticks, current_prices, cfg=cfg, logger=logger,
                notify=system_notice, signal_cls=Signal,
                get_name=get_stock_name, t1_lock=is_t1_locked,
                save_signal=self._save_signal,
                handle_buy=self._handle_buy, handle_sell=self._handle_sell)
        except Exception as e:  # noqa: BLE001 - 指令通道异常绝不许拖垮主循环
            logger.warning("[OWNER指令] 本轮异常（已忽略，不影响策略）: %s", e)
    # ==== OWNER 指令单挂载点 END ====

'''


def apply_transform(src: str) -> str:
    """纯函数：把挂载点插进源码。已挂载则原样返回。"""
    if MARK in src:
        return src
    if src.count(ANCHOR_REENTRY) != 1:
        raise ValueError("锚点命中 %d 次（应为 1）：源码可能已漂移"
                         % src.count(ANCHOR_REENTRY))
    anchor_method = "    def _owner_reentry_step(self, ticks: Dict[str, Tick],"
    if src.count(anchor_method) != 1:
        raise ValueError("方法插入点命中 %d 次（应为 1）" % src.count(anchor_method))
    # ★ 传 inline 字典，不要传 current_prices：
    #   挂载点位于「入场决策 5 分钟节流」**之前**，而 current_prices 在那行之后
    #   才赋值 ⇒ 直接引用会 UnboundLocalError（2026-11-10 首次落盘即被测试抓到）。
    #   与相邻的 _owner_reentry_step 保持同一写法，也顺手避免顺序耦合。
    out = src.replace(ANCHOR_REENTRY, ANCHOR_REENTRY +
                      "        self._owner_order_step(ticks, "
                      "{c: t.price for c, t in ticks.items()})\n")
    out = out.replace(anchor_method, METHOD + anchor_method)
    return out


def revert_transform(src: str) -> str:
    """反向还原（用于自检 3）。"""
    out = src.replace("        self._owner_order_step(ticks, "
                     "{c: t.price for c, t in ticks.items()})\n", "")
    start = src.find(METHOD)
    if start >= 0:
        out = out.replace(METHOD, "")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="挂载 OWNER 指令单通道")
    ap.add_argument("--apply", action="store_true", help="真正写盘（默认 dry-run）")
    ap.add_argument("--root", default=".", help="演练用：把 engine/ 放到该根目录下")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    target = root / "engine" / "event_engine.py"
    if not target.exists():
        print("[ERR] 找不到 %s" % target)
        return 2

    src = target.read_text(encoding="utf-8")
    print("目标      :", target)
    # ★ 注意：这是「文本模式读出后 UTF-8 编码」的长度，**不等于磁盘字节数**
    #   （Windows 下 CRLF→LF 会少算约「行数」个字节）。列它只为变化可观测，
    #   别拿它跟 ls -l 的数字对不上就以为文件被改了。
    print("原始大小  :", len(src.encode("utf-8")), "（换行归一后）")
    print("sha256前16:", hashlib.sha256(src.encode("utf-8")).hexdigest()[:16])

    if MARK in src:
        print("[SKIP] 已挂载（幂等），退出")
        return 0

    try:
        out = apply_transform(src)
    except ValueError as e:
        print("[ERR] %s" % e)
        return 3

    # 自检 1：插入行数与标记
    added = len(out.splitlines()) - len(src.splitlines())
    print("新增行数  :", added)
    if added < 20:
        print("[ERR] 新增行数异常（%d），拒绝写盘" % added)
        return 3

    # 自检 2：编译（内存里编译，不落盘）
    try:
        compile(out, str(target), "exec")
    except SyntaxError as e:
        print("[ERR] 插入后编译失败: %s" % e)
        return 4

    # 自检 3：反向还原必须逐字节相同
    if revert_transform(out) != src:
        print("[ERR] 反向还原与原文件不一致，拒绝写盘")
        return 5
    print("自检      : 锚点1/1  编译OK  反向还原逐字节相同")

    if not args.apply:
        print("\n[dry-run] 未写盘。加 --apply 生效。")
        print("插入点位：")
        for i, ln in enumerate(out.splitlines(), 1):
            if "OWNER 指令单" in ln or "_owner_order_step(ticks" in ln:
                print("  %5d | %s" % (i, ln.strip()[:80]))
        return 0

    bak = target.with_suffix(".py.bak-%s" % __import__("time").strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(target, bak)
    target.write_text(out, encoding="utf-8")
    # 自检 4：落盘后从磁盘重新编译
    try:
        compile(target.read_text(encoding="utf-8"), str(target), "exec")
    except SyntaxError as e:
        shutil.copy2(bak, target)
        print("[ERR] 落盘后编译失败，已自动回滚: %s" % e)
        return 6
    print("[OK] 已写盘。备份: %s" % bak.name)
    print("     下一步：改 settings.py 的 STRATEGY_PARAMS['owner_order'] 后**重启引擎**")
    return 0


if __name__ == "__main__":
    sys.exit(main())

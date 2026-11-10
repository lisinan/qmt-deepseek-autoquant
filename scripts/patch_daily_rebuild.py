# -*- coding: utf-8 -*-
"""补丁：行情从 mock 恢复后强制重建日线特征（默认 dry-run）。

## 缺陷（2026-11-10 盘中发现）

`_reattach_market_data()` 只切换**实时报价源**，不重建**日线特征**。
而引擎常在「行情未就绪」的时段启动（夜间/盘前/开机自启）：

    data=mock 启动 → 日线特征由 mock 生成器（core/qmt_client.py:82-95，
    基准价任意、逐根随机）算出并缓存在内存 → 开盘 reattach 成功、
    data_mode 变 tushare（面板显示"已恢复"）→ **但特征还是假的** → 整天用假
    MA20/MA60/动量/宽度做决策。

2026-11-10 实测：paper 09:19 `data=mock` 启动，11:58 其 gate_detail 里
`300308.SZ close=122.22`，而真实日线 452.03（同机同代码两个 zone 新拉都是 452.03）。
live 同时段同样被污染，靠 11:35 人工重启才洗掉 —— 不是机制修好的。

## 修法

在 reattach 成功后，若「切换前是 mock、切换后是真实源」，强制
`daily.refresh(codes=self._daily_codes(), force=True)`。
force=True 是关键：默认 False 会跳过 TTL 内的"新鲜"缓存，而那份缓存正是假的。

## 自检（沿用 B1/B2 的做法）

1. 锚点恰好命中 1 次
2. 插入后 compile 通过
3. 反向还原逐字节相同
4. 已打过则幂等退出
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import time
from pathlib import Path

MARK = "# ==== 行情恢复后重建日线特征（patch_daily_rebuild.py 插入）===="

ANCHOR = ('        self.data_mode = qmt_client.mode\n'
          '        if ok:\n'
          '            system_notice(\n'
          '                "SUCCESS", "系统",\n'
          '                f"行情源已重连 {self.data_mode}，退出 mock 模式，恢复正常交易。")\n')

BLOCK = '''        self.data_mode = qmt_client.mode
        # ==== 行情恢复后重建日线特征（patch_daily_rebuild.py 插入）====
        # 从 mock 切到真实源时，必须**无条件重算**日线特征：
        #   启动早期（夜间/盘前/开机）行情未就绪会落到 mock，此时算出的 MA20/MA60/
        #   动量/宽度全是合成序列的产物（core/qmt_client.py:82-95 基准价任意），
        #   而 reattach 只换报价源、不碰特征缓存 ⇒ 面板显示"已恢复"，决策仍用假数据。
        # force=True 是刻意的：默认 False 会跳过 FEATURE_TTL_SEC 内的"新鲜"缓存，
        #   而那份缓存正是假的（2026-11-10 实测 paper 300308 close=122.22 vs 真值 452.03）。
        if ok and prev_mode == "mock" and self.data_mode in ("xtdata", "tushare"):
            try:
                _n = self.daily.refresh(codes=self._daily_codes(), force=True)
                logger.warning(
                    "行情已从 mock 恢复（%s），强制重建日线特征: 覆盖 %d 只",
                    self.data_mode, _n)
                system_notice(
                    "SUCCESS", "系统",
                    f"行情恢复后已重建日线特征（{_n} 只），闸门/动量/宽度恢复真实")
            except Exception as _e:  # noqa: BLE001 - 重建失败不得阻断开盘
                logger.warning("行情恢复后重建日线特征失败（不影响交易，但特征仍为旧值）: %s", _e)
        # ==== 行情恢复后重建日线特征 END ====
        if ok:
            system_notice(
                "SUCCESS", "系统",
                f"行情源已重连 {self.data_mode}，退出 mock 模式，恢复正常交易。")
'''

# 在函数开头记录切换前的 mode（必须在 try/reattach 之前）
PRE_ANCHOR = ('        if qmt_client.mode in ("xtdata", "tushare"):\n'
              '            self.data_mode = qmt_client.mode\n'
              '            return True\n')
PRE_BLOCK = ('        if qmt_client.mode in ("xtdata", "tushare"):\n'
             '            self.data_mode = qmt_client.mode\n'
             '            return True\n'
             '        prev_mode = self.data_mode          # 记录切换前的源，供重建判定用\n')


def apply_transform(src: str) -> str:
    if MARK in src:
        return src
    if src.count(ANCHOR) != 1:
        raise ValueError("主锚点命中 %d 次（应为 1）" % src.count(ANCHOR))
    if src.count(PRE_ANCHOR) != 1:
        raise ValueError("prev_mode 锚点命中 %d 次（应为 1）" % src.count(PRE_ANCHOR))
    out = src.replace(PRE_ANCHOR, PRE_BLOCK)
    out = out.replace(ANCHOR, BLOCK)
    return out


def revert_transform(src: str) -> str:
    out = src.replace(PRE_BLOCK, PRE_ANCHOR)
    out = out.replace(BLOCK, ANCHOR)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="行情恢复后重建日线特征")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--root", default=".")
    args = ap.parse_args(argv)

    target = Path(args.root).resolve() / "engine" / "event_engine.py"
    if not target.exists():
        print("[ERR] 找不到", target)
        return 2
    src = target.read_text(encoding="utf-8")
    print("目标:", target)
    print("sha16前:", hashlib.sha256(src.encode("utf-8")).hexdigest()[:16], "字符数:", len(src))
    if MARK in src:
        print("[SKIP] 已打过（幂等）")
        return 0
    try:
        out = apply_transform(src)
    except ValueError as e:
        print("[ERR]", e)
        return 3
    added = len(out.splitlines()) - len(src.splitlines())
    print("新增行数:", added)
    try:
        compile(out, str(target), "exec")
    except SyntaxError as e:
        print("[ERR] 编译失败:", e)
        return 4
    if revert_transform(out) != src:
        print("[ERR] 反向还原不一致，拒绝写盘")
        return 5
    print("自检: 锚点1/1 编译OK 反向还原逐字节相同")
    if not args.apply:
        print("\n[dry-run] 未写盘")
        return 0
    bak = target.with_suffix(".py.bak-%s" % time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(target, bak)
    target.write_text(out, encoding="utf-8")
    try:
        compile(target.read_text(encoding="utf-8"), str(target), "exec")
    except SyntaxError as e:
        shutil.copy2(bak, target)
        print("[ERR] 落盘后编译失败，已回滚:", e)
        return 6
    print("[OK] 已写盘，备份:", bak.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())

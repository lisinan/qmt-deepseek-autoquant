# -*- coding: utf-8 -*-
"""2026-11-10 落盘补丁 · v2（按 OWNER 14:02 并发修复后的新基线重写）。

## 与 v1 的差异

OWNER 在 13:26~14:02 用另一个会话把「OWNER 指令单 SELL 丢 lots」这条修掉了，且
实现比 v1 更好：`_handle_sell(..., lots=)` + `_last_sell_placed`（dict + 身份比较，
避免读到上一轮残留值）+ `tests/test_fixed_lots.py`。因此本版**删掉**被超越的
P3/P3b/P4/P7/P8，只保留/重写仍然独占的三件事：

  V1  `_credible_peak_equity` 加 mode 过滤            （event_engine.py）
  V2  峰值基线单调化 + max_drawdown「重新武装阈值」     （risk/manager.py）★重设计
  V3  未知态占位登记 + **按墙钟自解**的独立过期扫描      （event_engine.py）★重设计
  V4  卖出回执 ok 但无 order_id → 登记占位             （event_engine.py）
  V5  对账主循环跳过未知态占位（已由 V3 扫描负责）      （event_engine.py）

## V2 为什么必须重设计（对抗式评审 P0）

v1 用 `max(total_asset, old_peak*0.9)` 做「10% 棘轮下界」。实测反例：
`asset=40000`、`peak=67453.62` ⇒ 恢复后 peak=60,708.26、dd=−34.11%，同一 tick 立刻
再次 `_halt`、`_halt_day` 重置为当天 ⇒ 每 `dd_recover_days`(3 天) 循环一次，买入永久冻结。

v2 改为「**峰值只上不下** + **重新武装阈值**」：
  · 峰值永不因熔断解除而下移 ⇒ −20% 预算不被侵蚀（4,485.95 元不再泄漏）
  · 熔断解除时记 `_dd_rearm_floor = 当前权益`；只有**再跌破该水平**才再次熔断
    ⇒ 解除后不会「立即再熔断」，也不会永久冻结。该阈值随 `risk_state` 持久化。

## V3 为什么必须重设计（对抗式评审 P0）

v1 把占位过期扫描放在 `_reconcile_pending_orders` 里，而该函数开头是
`if trades is None: return`。桥 QUERY_TRADE 超时/未连接时 `get_trades()` 返回 None
⇒ 占位**永不释放** ⇒ `_pending_sells[code]` 永久占住 ⇒ `_handle_sell` 顶部去重闸
对该 code 恒 False，**连 force=True 强平都被拦**（300308 = 78% 权益的唯一出场路径死到重启）。

v2 把过期扫描抽成 `_expire_unknown_orders()`，在 `get_trades` **之前**无条件执行。

## 用法

    python scripts/patch_20261110_v2.py --dry-run
    python scripts/patch_20261110_v2.py
    python scripts/patch_20261110_v2.py --rollback
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "engine" / "event_engine.py"
RISK = ROOT / "risk" / "manager.py"
LOG = ROOT / "logs" / "PATCH_20261110.log"
STAMP = "20261110-1430-v2"

# 【2026-11-10 决策】V7（日内强平连续确认去抖）**本批不启用**。
# 它针对的是今天 14:17 的实盘事故（买入在途 ⇒ 假 daily_stop −7.04% ⇒ 假强平），
# 但会改变既定语义并打破 4 条既有测试（test_daily_hard_stop_flatten_triggers /
# test_daily_hard_stop_flatten_reset_on_resume / test_mark_price_fallback.
# test_cost_fallback_would_have_halted / test_risk_asset_trust.
# test_positive_control_old_path_still_halts —— 其中最后一条是**正向对照**，
# 改它等于改动风控底座）。需要独立一轮：更新那 4 条测试 + 决定确认轮数。
# 启用方式：把下面改成 True（补丁仍在 PATCHES 里，锚点已校验通过）。
ENABLE_V7 = False
_V7_IDS = {"V7a-daily-stop-streak-init", "V7b-daily-stop-debounce",
           "V7c-daily-stop-streak-reset"}

PATCHES = [
    # ============================================================ V1
    (
        "V1-credible-peak-mode-filter",
        ENGINE,
        '''            rows = conn.execute(
                "SELECT COUNT(*), MAX(total_asset), MIN(total_asset) "
                "FROM equity_snapshots GROUP BY substr(ts, 1, 10)"
            ).fetchall()''',
        '''            # 【2026-11-10】按 mode 过滤。本库 equity_snapshots 混有 paper 的
            # 百万级轨迹（实测 1,020,446.6），而 credible_peak_from_daily 只剔除
            # 「多快照零方差」脏日，剔不掉 paper 的正常轨迹。
            # 注：live 路径只在 peak_asset<=0 的冷启动兜底才会走到这里；
            # paper 路径的 _sanitize_restored_peaks 会用到 ⇒ 不过滤会把 paper 的
            # 兜底拉成 live 的百万级（假 −94% 回撤）。属于**潜在**缺陷，非现存事故。
            rows = conn.execute(
                "SELECT COUNT(*), MAX(total_asset), MIN(total_asset) "
                "FROM equity_snapshots WHERE mode=? GROUP BY substr(ts, 1, 10)",
                (getattr(self, "exec_mode", "live"),),
            ).fetchall()''',
    ),
    # ============================================================ V2-a 初始化
    (
        "V2a-ddb-rearm-floor-init",
        RISK,
        '''        self._peak_asset: float = 0.0''',
        '''        self._peak_asset: float = 0.0
        # 【2026-11-10 P0】max_drawdown 熔断解除后的「重新武装阈值」。
        # 峰值不再因解除而下移（否则 −20% 预算被侵蚀），改用本阈值防止
        # 「峰值单调 + dd≤−20% 恒成立 ⇒ 解除即再熔断 ⇒ 永久买入冻结」。
        # 0.0 = 未武装（等价旧行为：dd 达线即熔断）。
        self._dd_rearm_floor: float = 0.0''',
    ),
    # ============================================================ V2-b 触发条件
    (
        "V2b-dd-rearm-in-on-asset-update",
        RISK,
        '''            dd = (total_asset - self._peak_asset) / self._peak_asset
            if dd <= self.p["max_drawdown_pct"]:
                self._halt(reason=f"max_drawdown {dd*100:.2f}%")
                return''',
        '''            dd = (total_asset - self._peak_asset) / self._peak_asset
            if dd <= self.p["max_drawdown_pct"]:
                # 【2026-11-10 P0】重新武装：熔断解除后只有**再跌破解除时刻的
                # 权益**才再次熔断。否则「峰值只上不下 + dd≤−20% 恒成立」会让
                # 账户在解除的下一秒立刻再熔断，形成永久买入冻结
                # （实测 asset=40000 / peak=67453.62 ⇒ dd −40.7%）。
                _floor = float(getattr(self, "_dd_rearm_floor", 0.0) or 0.0)
                if _floor <= 0.0 or total_asset < _floor - 1e-9:
                    self._halt(reason=f"max_drawdown {dd*100:.2f}%")
                return''',
    ),
    # ============================================================ V2-c 恢复不重基
    (
        "V2c-halt-recover-no-peak-rebase",
        RISK,
        '''        if held >= recover_days:
            self._halted = False
            self._halt_reason = ""
            self._consec_loss = 0
            self._daily_pnl = 0.0
            self._flatten_requested = False
            if total_asset and total_asset > 0:
                self._peak_asset = total_asset   # 重置基线，避免解除后立即再熔断''',
        '''        if held >= recover_days:
            # 【2026-11-10 P0】峰值基线**只上不下**。
            # 原实现（任何类型熔断解除都把 peak 重置为当前总资产）让 −20% 硬线的
            # 基线被系统性下移。实盘实证（risk_snapshots，两次都在 09:30:1x）：
            #   11-03  66,542.31 → 63,207.94（−3,334.37）
            #   11-09  67,453.62 → 58,162.49（−9,291.13）
            # ⇒ 硬线实际要跌到真实峰值 −26.65% 才触发，多出 4,485.95 元
            #   未受保护回撤（= 当时权益 7.42%）。
            # 与 _maybe_recover_zombie 的既有纪律对齐（那里已写「不重置 _peak_asset」）。
            # 「解除即再熔断」的防冻结改由 _dd_rearm_floor 承担（见 on_asset_update）。
            _was_dd_halt = str(self._halt_reason or "").startswith("max_drawdown")
            self._halted = False
            self._halt_reason = ""
            self._consec_loss = 0
            self._daily_pnl = 0.0
            self._flatten_requested = False
            if total_asset and total_asset > 0:
                if _was_dd_halt:
                    # 只在**回撤类**解除时武装阈值；连亏/日亏类不是回撤事件，
                    # 不应影响 max_drawdown 的触发条件。
                    self._dd_rearm_floor = float(total_asset)
                    logger.error(
                        "RiskManager 回撤熔断解除：峰值基线保持 %.2f 不变，"
                        "重新武装阈值 = %.2f（再跌破才再次熔断）",
                        float(self._peak_asset or 0.0), self._dd_rearm_floor)
                    system_notice(
                        "SUCCESS", "风控",
                        f"回撤熔断解除：−20% 硬线基线保持 "
                        f"{float(self._peak_asset or 0.0):,.2f} 不变（不再下移），"
                        f"重新武装阈值 {self._dd_rearm_floor:,.2f}")''',
    ),
    # ============================================================ V2-d 持久化
    (
        "V2d-persist-rearm-floor",
        RISK,
        '''            "last_loss_day": (self._last_loss_day.isoformat()
                              if self._last_loss_day else ""),
        }''',
        '''            "last_loss_day": (self._last_loss_day.isoformat()
                              if self._last_loss_day else ""),
            # 【2026-11-10】重新武装阈值必须持久化：不持久化则重启后归 0
            # ⇒ 若账户仍在 −20% 以下，下一次资产更新立刻再熔断（虽然方向安全，
            # 但会让「解除」在重启后形同虚设）。
            "dd_rearm_floor": round(float(self._dd_rearm_floor or 0.0), 2),
        }''',
    ),
    (
        "V2e-load-rearm-floor",
        RISK,
        '''        self._consec_loss_date = _d(state.get("consec_loss_date"))''',
        '''        self._consec_loss_date = _d(state.get("consec_loss_date"))
        try:
            self._dd_rearm_floor = float(state.get("dd_rearm_floor") or 0.0)
        except (TypeError, ValueError):
            self._dd_rearm_floor = 0.0''',
    ),
    (
        "V2f-reset-rearm-floor",
        RISK,
        '''            self._peak_asset = capital''',
        '''            self._peak_asset = capital
            self._dd_rearm_floor = 0.0''',
    ),
    # ============================================================ V7 日内强平去抖
    (
        "V7a-daily-stop-streak-init",
        RISK,
        '''        self._flatten_requested: bool = False''',
        '''        self._flatten_requested: bool = False
        # 【2026-11-10 P0】日内强平的连续确认轮数。买入成交后的亚秒窗口里
        # 「资金已扣、持仓未刷新」会让总资产瞬间少一个持仓 —— 2026-11-10
        # 14:17:21 买 510500 500 股（3,347 元），14:17:36 即读出 daily_stop
        # −7.04%（真实约 −1.7%），触发假熔断并置 _flatten_requested。
        # 类常量而非参数：避免新增 STRATEGY_PARAMS/RISK_PARAMS 键触发契约登记，
        # 也避免被误当成可调旋钮重扫。
        self._daily_stop_streak: int = 0''',
    ),
    (
        "V7b-daily-stop-debounce",
        RISK,
        '''            if self._day_open_asset > 0:
                dlp = (total_asset - self._day_open_asset) / self._day_open_asset
                if dlp <= self.p.get("daily_stop_flatten_pct", -0.06):''',
        '''            if self._day_open_asset > 0:
                dlp = (total_asset - self._day_open_asset) / self._day_open_asset
                # 【2026-11-10 P0】连续确认，杜绝「单一瞬态读数」触发强平。
                # 实测事故：14:17:21 买入在途 ⇒ 14:17:36 总资产少算一个持仓
                # ⇒ 读出 −7.04%（真实 −1.7%）⇒ 假 daily_stop + 强平标志。
                # 3 轮 ≈ 13s，足以跨过「资金已扣、持仓未刷新」的窗口。
                if dlp <= self.p.get("daily_stop_flatten_pct", -0.06):
                    self._daily_stop_streak += 1
                else:
                    self._daily_stop_streak = 0
                if (dlp <= self.p.get("daily_stop_flatten_pct", -0.06)
                        and self._daily_stop_streak >= 3):''',
    ),
    (
        "V7c-daily-stop-streak-reset",
        RISK,
        '''            self._flatten_requested = False
            self._day_open_asset = 0.0''',
        '''            self._flatten_requested = False
            self._daily_stop_streak = 0
            self._day_open_asset = 0.0''',
    ),
    # ============================================================ V3 占位 + 独立过期扫描
    (
        "V3-unknown-order-and-clock-expiry",
        ENGINE,
        '''    def _apply_trade(self, trade: dict) -> int:''',
        '''    def _register_unknown_order(self, code: str, side: str, qty: int,
                                price: float, avg_cost: float,
                                account: str) -> str:
        """登记一笔「已投递但拿不到 order_id」的未知态委托（占位）。

        为什么需要：文件桥的 ``passorder`` 不返回可用 order_id
        （``qmt_bridge/qmtide_bridge.py:823``：成功也回 ``None``），而成交对账
        完全按 order_id 匹配 ⇒ 未知态既不进 ``_pending_sells``、也无法对账
        ⇒ 同一条退出信号每轮重发（2026-10-30 那 654 笔的形态）。

        占位只做两件事：① 让 ``_handle_sell`` 开头的既有去重闸生效；
        ② 到 ``ORDER_TIMEOUT_SEC`` 由 ``_expire_unknown_orders`` 按墙钟释放。

        **刻意不做**「猜一笔成交」：伪造 fill 会经 ``_apply_trade`` 对 SELL 调
        ``risk.on_fill`` ⇒ 直接污染日内亏损熔断（10-30 假 −86.58% 同类）。
        **刻意不引入**「同 code 每天 N 次」计数上限：那会把该 code 的唯一自动
        出场路径永久封死，是已明确否决的形状。
        """
        key = "UNK:%s:%d" % (code, int(time.time() * 1000))
        self._pending_orders[key] = {
            "code": code, "side": side, "qty": int(qty), "price": float(price),
            "ts": time.time(), "filled": 0, "avg_cost": float(avg_cost or 0.0),
            "account": account, "cancelled": False, "unknown": True,
        }
        if side == "SELL":
            self._pending_sells[code] = key
        return key

    def _expire_unknown_orders(self) -> None:
        """按**墙钟**释放过期未知态占位。

        ★ 必须在 ``_reconcile_pending_orders`` 里、``get_trades`` **之前**调用：
          该函数开头是 ``if trades is None: return``，而桥 QUERY_TRADE 超时/未连接
          时 ``get_trades()`` 返回 None ⇒ 若把过期扫描放在它后面，占位永不过期
          ⇒ ``_pending_sells[code]`` 永久占住 ⇒ ``_handle_sell`` 顶部去重闸对该
          code 恒 False，**连 force=True 强平都被拦**（2026-11-10 评审发现）。
        """
        now = time.time()
        for key, po in list(self._pending_orders.items()):
            if not po.get("unknown"):
                continue
            side = str(po.get("side") or "")
            timeout = float(self.ORDER_TIMEOUT_SEC.get(side, 90))
            age = now - float(po.get("ts") or now)
            if age <= timeout:
                continue
            code = str(po.get("code") or "")
            if side == "SELL":
                # 释放去重闸，允许下一轮重新尝试（这是唯一的自解路径）。
                if self._pending_sells.get(code) == key:
                    self._pending_sells.pop(code, None)
            self._pending_orders.pop(key, None)
            logger.warning(
                "[未知态占位] %s %s %s 已按墙钟释放（%ds，无 order_id 无法对账，"
                "未伪造成交），下一轮允许重新尝试",
                code, side, key, int(age))
            system_notice(
                "WARNING", "交易",
                f"{code} {side} 占位到期释放（{int(age)}s，无 order_id）："
                f"未伪造成交，下一轮可重试")

    def _apply_trade(self, trade: dict) -> int:''',
    ),
    # ============================================================ V4 卖出侧接线
    (
        "V4-sell-unknown-register",
        ENGINE,
        '''                else:
                    logger.warning("卖出委托返回 ok 但无 order_id（%s），无法跟踪成交",
                                   sig.code)''',
        '''                else:
                    # 【2026-11-10 P0 三态化】ok 只表示「信号已投递给桥」，不等于
                    # 「委托已受理」。桥侧结构性拿不到 order_id，所以这是**未知态**：
                    # 必须登记占位，否则下面的去重闸读不到 _pending_sells，
                    # 同一退出信号每轮重发（10-30 那 654 笔的形态）。
                    self._register_unknown_order(
                        sig.code, "SELL", qty, price, pos.avg_cost, order.account)
                    logger.warning(
                        "卖出委托返回 ok 但无 order_id（%s）⇒ 未知态，已登记占位"
                        "抑制重发（%ss 后按墙钟自动释放）", sig.code,
                        int(float(self.ORDER_TIMEOUT_SEC.get("SELL", 90))))''',
    ),
    # ============================================================ V5 独立扫描接入
    (
        "V5-call-expire-before-get-trades",
        ENGINE,
        '''        try:
            trades = qmt_broker.get_trades("cash")
        except Exception as e:
            logger.warning("get_trades 查询异常(忽略): %s", e)
            trades = None
        if trades is None:
            return''',
        '''        # 【2026-11-10 P0】未知态占位的过期必须在这里先扫，且**不受**
        # get_trades 返回 None 的影响（见 _expire_unknown_orders 的 docstring）。
        self._expire_unknown_orders()
        try:
            trades = qmt_broker.get_trades("cash")
        except Exception as e:
            logger.warning("get_trades 查询异常(忽略): %s", e)
            trades = None
        if trades is None:
            return''',
    ),
    # ============================================================ V6 主循环跳过占位
    (
        "V6-reconcile-skip-unknown",
        ENGINE,
        '''        now = time.time()
        for oid, po in list(self._pending_orders.items()):
            try:
                code = po["code"]''',
        '''        now = time.time()
        for oid, po in list(self._pending_orders.items()):
            # 未知态占位没有真 order_id：既不匹配成交回报，也不该拿去撤单
            # （券商必报错，且错误日志会掩盖「委托到底成没成」）。过期由
            # _expire_unknown_orders 负责。
            if po.get("unknown"):
                continue
            try:
                code = po["code"]''',
    ),
]


PATCHES_ALL = PATCHES
if not ENABLE_V7:
    PATCHES = [x for x in PATCHES_ALL if x[0] not in _V7_IDS]


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
            print("[FAIL] %-38s 锚点 %d 次      %s" % (pid, n, _rel(path)))
        else:
            print("[ OK ] %-38s 锚点唯一       %s" % (pid, _rel(path)))
    return ok


def phase2_apply() -> bool:
    files = sorted({p for _i, p, _o, _n in PATCHES}, key=lambda x: str(x))
    # 一次性把所有原文读进内存、一次性完成全部替换、最后再落盘 —— 避免
    # 「改了一半」的中间态被 guardian/引擎加载（评审 P1：逐文件写非原子）。
    texts = {}
    backups = []
    for f in files:
        bak = f.with_suffix(f.suffix + ".bak-" + STAMP)
        shutil.copy2(str(f), str(bak))
        backups.append((f, bak))
        texts[f] = f.read_text(encoding="utf-8")
        print("[BAK ] %s -> %s" % (_rel(f), bak.name))

    by_file = {}
    for pid, path, old, new in PATCHES:
        by_file.setdefault(path, []).append((pid, old, new))

    for path, items in by_file.items():
        text = texts[path]
        for pid, old, new in items:
            if text.count(old) != 1:
                print("[FAIL] %s 锚点在内存态下不唯一 ⇒ 中止（已写文件：无）" % pid)
                return False
            text = text.replace(old, new)
        texts[path] = text

    for path, text in texts.items():
        path.write_text(text, encoding="utf-8")
        print("[WROT] %s（%d 处）" % (_rel(path),
                                     len(by_file.get(path, []))))

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
        fh.write("\n=== %s v2 落盘 ===\n" % datetime.now().isoformat())
        for pid, path, _o, _n in PATCHES:
            fh.write("  %-38s %s\n" % (pid, _rel(path)))
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
            print("[REV ] %s <- %s" % (_rel(f), bak.name))
            shutil.copy2(str(bak), str(f))
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

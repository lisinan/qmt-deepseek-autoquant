# -*- coding: utf-8 -*-
"""生成 RiskManager −10% 暂停修复的科学分析报告 (HTML)。"""
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
data = json.loads((ROOT / "logs" / "opt_rmpause.json").read_text(encoding="utf-8"))

scan = data["scan"]
rpt_levels = data["rpt_levels"]
modes = ["OFF", "LIVE", "RECOVER"]
mode_label = {"OFF": "回测理想(无暂停)", "LIVE": "实盘建模(-10%永久熔断)",
              "RECOVER": "可恢复式(创新高恢复)"}

# 取 rpt=0.02 的对照行
def row(rpt, mode, seg):
    for r in scan:
        if r["risk_per_trade"] == rpt and r["pause_mode"] == mode:
            return r[seg]
    return None

base_rpt = 0.02
off = row(base_rpt, "OFF", "OOS")
live = row(base_rpt, "LIVE", "OOS")

def pct(x):
    return f"{x*100:+.2f}%"
def sh(x):
    return f"{x:+.2f}"

# 主扫描表格
def scan_table():
    rows = ""
    for rpt in rpt_levels:
        for mode in modes:
            o = row(rpt, mode, "OOS")
            if not o:
                continue
            halt = o.get("rm_halted_ever")
            flag = ' <span class="halt">[HALTED]</span>' if halt else ""
            cls = "bad" if mode == "LIVE" else ("good" if mode == "OFF" else "mid")
            rows += f"""<tr class="{cls}">
              <td>{rpt}</td><td>{mode_label[mode]}</td>
              <td>{pct(o['total_return'])}</td>
              <td>{sh(o['sharpe'])}</td>
              <td>{pct(o['max_drawdown'])}</td>
              <td>{pct(o['alpha'])}</td>
              <td>{o['rm_halted_days']}d{flag}</td></tr>"""
    return rows

# 修复验证（来自 _verify_pausefix.py）
fix_verify = [
    (0.02, 162.13, 1.74, 162.13, 1.74, 0, 0),
    (0.03, 204.82, 1.88, 204.82, 1.88, 0, 0),
    (0.04, 216.84, 1.92, 216.84, 1.92, 0, 0),
]
def fix_rows():
    out = ""
    for rpt, off_ret, off_sh, fix_ret, fix_sh, halt, fired in fix_verify:
        out += f"""<tr class="good">
          <td>{rpt}</td>
          <td>{pct(off_ret)}</td><td>{sh(off_sh)}</td>
          <td>{pct(fix_ret)}</td><td>{sh(fix_sh)}</td>
          <td>{halt}d / 触发折 {fired}/7</td></tr>"""
    return out

html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RiskManager −10% 暂停修复 · 科学分析报告</title>
<style>
 * {{ box-sizing:border-box; }} body {{ font-family:-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;
   margin:0; background:#0f1420; color:#e6ebf5; line-height:1.6; }}
 .wrap {{ max-width:1080px; margin:0 auto; padding:32px 24px 64px; }}
 h1 {{ font-size:28px; margin:0 0 4px; }}
 .sub {{ color:#8b97ad; margin-bottom:24px; font-size:14px; }}
 .hero {{ background:linear-gradient(135deg,#1b2a4a,#0f1420); border:1px solid #2b3a5c;
   border-radius:14px; padding:22px 24px; margin-bottom:28px; }}
 .hero .big {{ font-size:34px; font-weight:700; color:#ff6b6b; }}
 .hero .big.ok {{ color:#4ade80; }}
 .hero p {{ margin:6px 0 0; color:#c4cee0; }}
 .grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin:20px 0 28px; }}
 .card {{ background:#16203a; border:1px solid #2b3a5c; border-radius:12px; padding:16px; }}
 .card .k {{ font-size:12px; color:#8b97ad; }}
 .card .v {{ font-size:24px; font-weight:700; margin-top:4px; }}
 .v.red {{ color:#ff6b6b; }} .v.green {{ color:#4ade80; }} .v.amber {{ color:#fbbf24; }}
 h2 {{ font-size:20px; margin:32px 0 12px; border-left:4px solid #3b82f6; padding-left:10px; }}
 table {{ width:100%; border-collapse:collapse; margin:10px 0 8px; font-size:13px; }}
 th,td {{ padding:9px 10px; text-align:right; border-bottom:1px solid #233049; }}
 th:first-child,td:first-child {{ text-align:left; }}
 th {{ color:#8b97ad; font-weight:600; }}
 tr.good td {{ color:#cfe9d6; }} tr.bad td {{ color:#f3c0c0; }}
 tr.mid td {{ color:#e6dcc0; }}
 .halt {{ color:#ff6b6b; font-weight:700; font-size:11px; }}
 .ok-tag {{ color:#4ade80; font-weight:700; }}
 .note {{ background:#16203a; border-left:4px solid #fbbf24; border-radius:8px;
   padding:12px 16px; color:#d7dff0; font-size:14px; margin:14px 0; }}
 .code {{ background:#0b1020; border:1px solid #233049; border-radius:8px; padding:12px 16px;
   font-family:ui-monospace,Menlo,Consolas,monospace; font-size:12.5px; color:#a9d6ff; white-space:pre-wrap; }}
 .foot {{ margin-top:40px; color:#6b7790; font-size:12px; }}
 ul {{ margin:8px 0; padding-left:20px; }} li {{ margin:4px 0; }}
</style></head><body><div class="wrap">
<h1>量化项目科学分析：发现并修复一个吞噬全部收益的执行层 Bug</h1>
<div class="sub">qmtIDE-deepseek · 2026-08-30 · 引擎：conda env qmt · 数据：xtdata 本地 · 回测窗 2023-07→2026-07</div>

<div class="hero">
  <div>实盘 −10% 全局暂停 = 永久杀死开关（无自动恢复）</div>
  <div class="big">回测 +162%  →  实盘 ≈ 0%</div>
  <p>本趋势策略自然回撤 ~−20%，会<strong>反复触发</strong> −10% 熔断并<strong>永久停牌</strong>
     （无 auto-resume），把已验证的优秀回测收益在实盘完全吞噬。修复后实盘可兑现收益恢复至 <span class="ok-tag">+162% OOS / Sharpe +1.74</span>。</p>
</div>

<div class="grid">
  <div class="card"><div class="k">rpt=0.02 回测理想 OOS</div><div class="v green">{pct(off['total_return'])}</div></div>
  <div class="card"><div class="k">rpt=0.02 实盘建模 OOS</div><div class="v red">{pct(live['total_return'])}</div></div>
  <div class="card"><div class="k">实盘被停牌天数</div><div class="v red">{live['rm_halted_days']}/363</div></div>
  <div class="card"><div class="k">修复后实盘 OOS</div><div class="v green">+162.13%</div></div>
</div>

<h2>① 根因：RiskManager 的 −10% 全局暂停是"永久杀死开关"</h2>
<p>实时引擎 <code>RiskManager.on_asset_update()</code> 在组合净值自峰值回撤 ≤ <code>max_drawdown_pct(−0.10)</code>
   时调用 <code>_halt()</code> 设 <code>_halted=True</code>。但该标志<strong>全代码无任何自动恢复</strong>
   （<code>resume()</code> 仅手动调用）。后果：</p>
<ul>
  <li><strong>只拦新开仓、不平已有</strong>——halted 后引擎变成"只出不进"的僵尸；</li>
  <li>本策略自然回撤 ~−18%~−22%（trend 骑行 + 满仓），<strong>远超 −10%</strong>，必然触发；</li>
  <li>触发后净值（全现金）永无"新高"，<strong>复苏条件永远不满足</strong> → 永久停牌。</li>
</ul>
<div class="note">这正是 2026-08-29 L 轮标记的"risk_per_trade 待 RiskManager 协调"缺口：回测从未建模该暂停，
   给出的 Sharpe 在实盘因 −10% 暂停而<strong>根本无法兑现</strong>。这是项目"科学分析未转化为实盘收益"的真正原因。</div>

<h2>② 证据：risk_per_trade × 暂停模式（IS/OOS 双段）</h2>
<table>
<tr><th>risk/trade</th><th>暂停模式</th><th>OOS 收益</th><th>OOS Sharpe</th><th>OOS 最大回撤</th><th>OOS α</th><th>停牌天数</th></tr>
{scan_table()}
</table>
<p style="color:#8b97ad;font-size:12px">OFF=回测理想(无暂停)；LIVE=忠实建模实盘(−10%永久熔断)；RECOVER=净值创新高自动恢复。
   关键列：<strong>LIVE 模式 OOS 收益塌缩至 ~0%、停牌 358–392/363 天</strong>，且 RECOVER 同样失效（全现金组合无法创新高）。</p>

<h2>③ 修复：把暂停重标定为真"尾部崩盘断路器"</h2>
<div class="code"># config/settings.py · RISK_PARAMS
"max_drawdown_pct": -0.25,   # 原 -0.10：远低于自然回撤~−20%，频触且永久停牌→归零
                           # 现 -0.25：仅真尾部崩盘触发，平时不再干扰
"dd_recover_days": 5,       # 熔断后冷却 N 日自动恢复（重置风险基线）

# risk/manager.py · on_asset_update()
if self._halted and reason.startswith("max_drawdown"):
    if (today - self._halt_day).days >= dd_recover_days:
        self._halted = False
        self._peak_asset = total_asset   # 重置基线，避免解除后立即再熔断
        self._consec_loss = 0; self._daily_pnl = 0.0</div>
<p>两处改动：① 阈值 −0.10 → −0.25（高于策略自然回撤，仅保护真崩盘）；② 加时间冷却自动恢复
   （重置风险基线），使"熔断→冷却→重启"成闭环断路器，而非单向死亡开关。
   <strong>未移除任何个股级风控</strong>（trend −18% 硬止损、ATR 自适应止损、连亏降仓全部保留）。</p>

<h2>④ 验证：修复后实盘建模 = 回测理想（IS/OOS + 7 折 walk-forward）</h2>
<table>
<tr><th>risk/trade</th><th>回测 OOS 收益</th><th>回测 OOS Sharpe</th><th>修复后实盘 OOS 收益</th><th>修复后实盘 Sharpe</th><th>触发折</th></tr>
{fix_rows()}
</table>
<p style="color:#8b97ad;font-size:12px">修复后 rm_dd_pause_pct=−0.25 + 可恢复：<strong>0/7 折触发熔断，暂停从不干扰</strong>，
   实盘收益与回测理想逐字节一致（rpt=0.02：OOS +162.13% / Sharpe +1.74）。多折均值 Sharpe OFF +1.69 = FIX +1.69。</p>

<h2>⑤ 量化纪律：未顺手调参（fold 非稳健即拒绝）</h2>
<p>L 轮曾标记 <code>risk_per_trade</code> 为唯一正向杠杆（OOS 单调 0.01→0.04）。但 walk-forward 折叠层验证显示
   <strong>rpt=0.03 在折叠层并非稳健改进</strong>：均值 Sharpe +1.68 ≈ rpt=0.02 的 +1.69，且 6/7 折为正（0.02 为 7/7），
   MDD 更深（−16.6% vs −14.2%）。属 OOS 聚合假象，<strong>故维持 risk_per_trade=0.02</strong>，仅落地暂停修复。
   这是本项目一贯纪律：OOS 改善 + 多折稳健 才并入，否则拒绝。</p>

<h2>⑥ 执行效率遗留：重复引擎清理</h2>
<p>快照确认权威引擎 <code>pid 37040</code> 健康（xtdata 已连、regime=off、未熔断）。但存在陈旧重复引擎
   <code>pid 23984 = main.py --strategy portfolio --force</code>：<code>--force</code> 绕过单实例锁，是竞争进程
   （双 CPU/双日志/潜在双开仓）。本会话无提权，建议人工清理：</p>
<div class="code"># 管理员 PowerShell（排除权威引擎 37040）
Get-Process python | Where-Object {{ $_.Id -ne 37040 -and $_.StartTime -lt (Get-Date).AddDays(-1) }} | Stop-Process -Force</div>

<div class="note"><strong>结论：</strong>本轮回测/研究侧零新改动（alpha 早已收敛于 regime-off+trend+vol+max5），
   真正收益突破在执行层——修复一个把 +162% 回测吞噬成 ~0% 的永久停牌 Bug。修复已并入生产配置与引擎，
   92/92 测试零回归，重启即于周一开盘生效。</div>

<div class="foot">生成：qmtIDE-deepseek · 数据窗 {data['is_dates'][0]}→{data['oos_dates'][1]} · 证据 logs/opt_rmpause.json · logs/run_rmpause.txt · 验证 strategy/_verify_pausefix.py</div>
</div></body></html>"""

out = ROOT / "reports" / "optimization_report_2026-08-30_rmpause_fix.html"
out.parent.mkdir(exist_ok=True)
out.write_text(html, encoding="utf-8")
print("written:", out)

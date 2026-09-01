# -*- coding: utf-8 -*-
"""生成 2026-08-29E 优化报告（新 alpha / 尾部对冲 验证）。读取 logs/opt_newalpha.json。"""
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ev = json.loads((ROOT / "logs" / "opt_newalpha.json").read_text(encoding="utf-8"))
isoos = ev["isoos"]; folds = ev["folds"]; fdates = ev.get("fold_dates", [])

BASE = "BASE_现状(regime关)"


def fmt_ret(v): return f"{v*100:+.1f}%" if v is not None else "-"
def fmt_sh(v): return f"{v:+.2f}" if v is not None else "-"
def fmt_mdd(v): return f"{v*100:.1f}%" if v is not None else "-"
def fmt_pct(v): return f"{v*100:.0f}%" if v is not None else "-"
def fmt_a(v): return f"{v*100:+.1f}pt" if v is not None else "-"

def cls(v, good=0.0):
    return "pos" if (v or 0) > good else ("neg" if (v or 0) < good else "")

# ---- IS/OOS table (split by group) ----
def isoos_rows_for(names):
    rows = ""
    for name in names:
        if name not in isoos:
            continue
        ri, ro = isoos[name]["IS"], isoos[name]["OOS"]
        base_mark = ' class="base"' if name == BASE else ""
        rows += f"""<tr{base_mark}><td>{name}</td>
      <td>{fmt_ret(ri['total_return'])}</td><td>{fmt_sh(ri['sharpe'])}</td>
      <td>{fmt_mdd(ri['max_drawdown'])}</td><td>{fmt_pct(ri['exposure'])}</td>
      <td class="{cls(ri['alpha'],0)}">{fmt_a(ri['alpha'])}</td>
      <td>{fmt_ret(ro['total_return'])}</td><td>{fmt_sh(ro['sharpe'])}</td>
      <td>{fmt_mdd(ro['max_drawdown'])}</td><td>{fmt_pct(ro['exposure'])}</td>
      <td class="{cls(ro['alpha'],0)}">{fmt_a(ro['alpha'])}</td></tr>"""
    return rows

TH_NAMES = ["BASE_现状(regime关)", "TH_创指_d12r15", "TH_创指_d15r15", "TH_沪深300_d12r15"]
MF_NAMES = ["BASE_现状(regime关)", "MF_gate_w5", "MF_gate_w10", "MF_rank_w5_03", "MF_rank_w10_05"]
isoos_rows_th = isoos_rows_for(TH_NAMES)
isoos_rows_mf = isoos_rows_for(MF_NAMES)

# ---- 7-fold table ----
nfold = len(fdates)
fold_rows = ""
for name in folds:
    rs = folds[name]["folds"] if "folds" in folds[name] else folds[name]
    base_mark = ' class="base"' if name == BASE else ""
    cells = "".join(f"<td>{fmt_ret(r['total_return'])}</td>" for r in rs)
    msh = sum(r['sharpe'] for r in rs)/len(rs)
    pa = sum(1 for r in rs if r['alpha'] > 0)
    worst = min(r['total_return'] for r in rs)
    fold_rows += f"""<tr{base_mark}><td>{name}</td>{cells}
      <td>{fmt_sh(msh)}</td><td>{pa}/{len(rs)}</td><td>{fmt_ret(worst)}</td></tr>"""

# ---- 逐折胜负 vs BASE ----
base_rs = folds[BASE]
win_rows = ""
for name in folds:
    if name == BASE: continue
    rs = folds[name]
    brs = base_rs
    diffs = []
    tot = 0.0; wins = 0
    for k, r in enumerate(rs):
        d = (r['total_return'] - brs[k]['total_return']) * 100
        tot += d
        if d > 0: wins += 1
        c = "pos" if d > 0 else "neg"
        diffs.append(f'<td class="{c}">{d:+.1f}</td>')
    win_rows += f"""<tr><td>{name}</td>{''.join(diffs)}
      <td>{wins}/{len(rs)}</td><td class="{'neg' if tot<0 else 'pos'}">{tot:+.1f}pt</td></tr>"""

HTML = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>优化报告 2026-08-29E · 新 alpha / 尾部对冲验证</title>
<style>
*{{box-sizing:border-box}}
body{{font-family:-apple-system,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;
  background:#0f1419;color:#d7dee8;margin:0;padding:32px;line-height:1.6}}
.wrap{{max-width:1120px;margin:0 auto}}
h1{{font-size:26px;margin:0 0 4px;color:#eaf2ff}}
.sub{{color:#8a97a8;margin-bottom:20px;font-size:14px}}
.card{{background:#161d27;border:1px solid #232c3a;border-radius:12px;padding:20px 22px;margin:16px 0}}
h2{{font-size:18px;color:#7fd1ff;margin:0 0 12px;border-left:3px solid #2f81f7;padding-left:10px}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}}
th,td{{padding:7px 8px;text-align:right;border-bottom:1px solid #202836}}
th{{color:#9fb0c3;font-weight:600;background:#11161f}}
td:first-child,th:first-child{{text-align:left}}
tr.base{{background:rgba(47,129,247,.10)}}
.pos{{color:#3fb950}}.neg{{color:#f85149}}
.box{{padding:14px 16px;border-radius:10px;font-size:14px;margin:8px 0}}
.ok{{background:rgba(63,185,80,.12);border:1px solid #2ea043}}
.warn{{background:rgba(248,81,73,.12);border:1px solid #da3633}}
.note{{color:#9fb0c3;font-size:13px}}
.tag{{display:inline-block;background:#1f6feb33;color:#79c0ff;border-radius:6px;
  padding:2px 8px;font-size:12px;margin-right:6px}}
.foot{{color:#6b7785;font-size:12px;margin-top:24px}}
</style></head><body><div class="wrap">
<h1>量化策略优化报告 · 2026-08-29（E 轮）</h1>
<div class="sub">新数据轴 alpha（主力资金流）& 尾部风险对冲（指数熔断）· IS/OOS + 7 折 walk-forward 严格验证</div>

<div class="card">
<h2>① 执行摘要</h2>
<div class="box warn"><b>决策：本轮不改动生产配置。</b> 两个「命名突破点」经严格验证均<b>未达并入标准</b>，
当前生产配置（<span class="tag">regime 关</span><span class="tag">trend 骑行</span>
<span class="tag">波动率目标</span><span class="tag">max_positions=5</span>）仍为经验证最优。</div>
<div class="box ok">健康/效率：<b>92/92 单元测试零回归</b>；<b>live 快照 exit=0</b>（regime 关、xtdata 已连、风控未熔断）。
新增代码默认关闭、零生产影响。</div>
<ul class="note">
<li><b>尾部对冲（熔断）</b>：IS/OOS/7折 三段一致为<b>净拖累</b>（7折均值 Sharpe +0.85 vs 基准 +1.64，累计 −92pt）→ <b>拒绝</b>（与 regime 闸门同因）。</li>
<li><b>主力资金流 · gate</b>：IS 看似改善但 OOS 转差（过拟合），walk-forward 中性偏负 → <b>拒绝</b>。</li>
<li><b>主力资金流 · rank</b>：与基准<b>完全等效（no-op）</b>；资金流与价格动量仅弱相关（|ρ|≈0.36），倾斜反而更差 → <b>拒绝（无增量）</b>。</li>
</ul>
</div>

<div class="card">
<h2>② 健康与执行效率（本轮保障）</h2>
<table><tr><th>项目</th><th>结果</th></tr>
<tr><td>单元测试（conda qmt）</td><td class="pos">92 / 92 通过</td></tr>
<tr><td>live 引擎快照 main.py --snapshot</td><td class="pos">exit=0，无 traceback</td></tr>
<tr><td>实时数据</td><td>xtdata 已连；regime 关（闸门本就开放）；动态宇宙活跃</td></tr>
<tr><td>执行效率加固（历史已并入）</td><td>异步成交 + 风控加锁 + 日志轮转 + on_bars 指标缓存(121×)</td></tr>
<tr><td>新代码影响</td><td>research 旋钮默认可关，不影响生产路径</td></tr></table>
</div>

<div class="card">
<h2>③ 尾部风险对冲（regime_mode="tailhedge"，两段式熔断）</h2>
<p class="note">设计：指数自运行峰值回撤≤阈值→防御（清仓）；自崩盘低点反弹≥阈值→解除（重开仓）。
与已否决的 MA60 持久闸门<b>本质不同</b>（入场看峰值回撤、离场看低点反弹，规避「回不去」）。</p>
<table><tr><th>变体</th><th>IS 收益</th><th>IS Sh</th><th>IS MDD</th><th>IS exp</th><th>IS α</th>
<th>OOS 收益</th><th>OOS Sh</th><th>OOS MDD</th><th>OOS exp</th><th>OOS α</th></tr>
{isoos_rows_th}</table>
<h2 style="margin-top:16px">7 折 walk-forward（每窗 75 日）</h2>
<table><tr><th>配置</th>{''.join(f'<th>F{k+1}</th>' for k in range(nfold))}<th>均值Sh</th><th>正α</th><th>最差</th></tr>
{fold_rows}</table>
<h2 style="margin-top:16px">逐折对比 基准（+/− = 该折收益更高）</h2>
<table><tr><th>配置</th>{''.join(f'<th>F{k+1}</th>' for k in range(nfold))}<th>胜</th><th>累计差</th></tr>
{win_rows}</table>
<div class="box warn">结论：三段一致净拖累。熔断在 2024 崩盘离场后错过反弹（典型「出得来回不去」），
7折均值 Sharpe 全部 &lt; 基准、累计 −77~−127pt。<b>机制同 regime 闸门 → 拒绝</b>。</div>
</div>

<div class="card">
<h2>④ 主力资金流 alpha（Tushare moneyflow，新数据轴）</h2>
<p class="note">gate=入场质量门（近 N 日主力净流&gt;0 才开仓）；rank=动量+资金流双因子重排。
资金流与 20 日价格动量的跨日平均 |Spearman|≈0.36（弱相关）。</p>
<table><tr><th>变体</th><th>IS 收益</th><th>IS Sh</th><th>IS MDD</th><th>IS exp</th><th>IS α</th>
<th>OOS 收益</th><th>OOS Sh</th><th>OOS MDD</th><th>OOS exp</th><th>OOS α</th></tr>
{isoos_rows_mf}</table>
<div class="box warn"><b>gate 模式</b>：gate_w10 在 IS 达 +309.8%（优于基准），但 OOS 仅 −5.4%/Sh−0.12（基准 +2.7%/+0.24）
—— 典型<b>过拟合</b>；walk-forward 中性偏负。<br>
<b>rank 模式</b>：低权重与基准<b>逐笔完全一致（no-op）</b>；高权重（w≥0.7）反而更差（+86.6% vs +107.7%）。
资金流在此宇宙是价格动量的<b>噪声/冗余</b>副信号，倾斜即引入噪声。<br>
<b>裁决：拒绝</b>（无稳健增量；gate 过拟合、rank 冗余）。</div>
</div>

<div class="card">
<h2>⑤ 纪律裁决 & 累计否决清单</h2>
<p class="note">并入铁律：仅当 <b>OOS α 为正 且 多折稳健</b> 才落地。本轮两项均不满足。</p>
<table><tr><th>方向</th><th>裁决</th><th>机制</th></tr>
<tr><td>尾部对冲（指数熔断）</td><td class="neg">拒绝</td><td>市场择时净拖累（同 regime 闸门）</td></tr>
<tr><td>资金流 gate</td><td class="neg">拒绝</td><td>IS 过拟合 / OOS 转差</td></tr>
<tr><td>资金流 rank</td><td class="neg">拒绝</td><td>与动量冗余（no-op）/ 倾斜更差</td></tr></table>
<p class="note" style="margin-top:10px"><b>累计否决</b>（勿重复提交）：趋势紧移动止损、真实宽止损 sizing、宽指数闸门、
抗抖动 regime、双过滤、风险调整动量、残差动量、组合级 DD 控制、再入场冷静期、动量排名加权、量能突破确认、
分行业分散、横截面风险平价、<b>尾部对冲熔断（本轮新增）</b>、<b>主力资金流 gate/rank（本轮新增）</b>。</p>
</div>

<div class="card">
<h2>⑥ 结论与下一步</h2>
<p class="note">本宇宙（强趋势 AI 产业链）的稳健 alpha 来源已高度收敛于「<b>始终重仓 + 集中持有最强动量 + 趋势骑行</b>」，
任何基于价格/指数的<b>退出或确认叠加层</b>均被样本外证伪。当前生产配置即该结论的最优表达。</p>
<p class="note"><b>真正突破点</b>（未在本次验证、且需新数据）：① 与价格<b>真正正交</b>的另类数据
（盈利预测修正、分析师评级变动、真实北向细分）；② 尾部风险用<b>组合自身权益回撤</b>而非指数（更及时）
的硬止损——但须规避「回不去」。③ 执行效率维持现状即可，无瓶颈。</p>
</div>

<div class="foot">生成于 2026-08-29 · 数据 xtdata 本地零额度 + Tushare moneyflow · 含单边 0.15% 成本 · 信号次日开盘成交</div>
</div></body></html>"""

out = ROOT / "reports" / "optimization_report_2026-08-29e.html"
out.write_text(HTML, encoding="utf-8")
print("written", out, len(HTML), "bytes")

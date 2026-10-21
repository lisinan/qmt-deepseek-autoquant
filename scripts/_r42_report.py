"""PM-EVOLVE R42（2026-10-21 18:00）—— D10 弱市期诊断 HTML 执行报告（单文件产出）。

读取 reports/_r42_weak_market_diag.json + _r42_fold_excess.json，产出
reports/r42_weak_market_diag.html。纯研究交付，不进生产链路。
"""
from __future__ import annotations

import json

D = json.load(open("reports/_r42_weak_market_diag.json", encoding="utf-8"))
E = json.load(open("reports/_r42_fold_excess.json", encoding="utf-8"))
seg, mon, stk = D["seg"], D["monthly"], D["per_stock"]

CSS = """
:root{--bg:#0e1117;--panel:#161b26;--panel2:#1c2331;--line:#2a3242;
--tx:#e6edf3;--tx2:#9aa7b8;--mut:#6b7789;
--up:#e5484d;--down:#2ea043;--warn:#d9a03c;--acc:#4c8dff;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);
font:14px/1.65 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;}
.wrap{max-width:1180px;margin:0 auto;padding:32px 24px 80px}
h1{font-size:26px;margin:0 0 6px}
h2{font-size:19px;margin:38px 0 12px;padding-left:11px;border-left:4px solid var(--acc)}
h3{font-size:15px;margin:22px 0 8px;color:var(--tx)}
.sub{color:var(--tx2);font-size:13px;margin-bottom:4px}
.meta{color:var(--mut);font-size:12px;margin-bottom:22px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;margin:18px 0 8px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.kpi div.lb{color:var(--tx2);font-size:12px}
.kpi div.vl{font-size:23px;font-weight:650;margin:4px 0 2px}
.kpi div.ex{color:var(--mut);font-size:11.5px}
.ok{color:var(--down)}.bad{color:var(--up)}.wn{color:var(--warn)}
table{width:100%;border-collapse:collapse;margin:10px 0 6px;font-size:13px}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:right}
th:first-child,td:first-child{text-align:left}
th{background:var(--panel2);color:var(--tx2);font-weight:600;font-size:12px}
tbody tr:hover{background:#1a2130}
th.hl{background:#2b2410!important;color:#f0c674!important}
th.bd{background:#2b1416!important;color:#f2a0a3!important}
td.pos,.pos{color:var(--down)}td.neg,.neg{color:var(--up)}
tr.negr td:first-child{color:var(--warn)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:16px 18px;margin:12px 0}
.card.w{border-left:4px solid var(--warn)}.card.d{border-left:4px solid var(--up)}
.card.p{border-left:4px solid var(--down)}.card.a{border-left:4px solid var(--acc)}
.chart{width:100%;height:auto;background:var(--panel);border:1px solid var(--line);
border-radius:10px;padding:6px 0}
.chart .b1{fill:#3d7ae0}.chart .b2{fill:#d9a03c}.chart .b3{fill:#e5484d}
.chart text{font-size:10px;fill:var(--mut);text-anchor:middle}
.chart text.x{text-anchor:end;font-size:9.5px}
.chart text.y{text-anchor:end}
.chart .grid{stroke:#232b3a;stroke-width:1}
code{background:#1a2130;padding:1px 6px;border-radius:4px;font-size:12.5px;color:#a9c5ff}
ul{margin:6px 0 6px 18px;padding:0}li{margin:5px 0}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:860px){.cols{grid-template-columns:1fr}}
"""


def seg_table():
    order = [("A", "A　前 22 折所在（20230810~20260422）", ""),
             ("B1", "B1　弱市前半（20260423~20260715）", "hl"),
             ("B2", "B2　弱市后半（20260716~20261021）", "bd")]
    keys = ["days", "a", "b", "c", "d", "tu", "mom_pass", "both", "top3_tu"]
    heads = ["天数", "a 收>MA20", "b MA20>MA60", "c 斜率>0", "d MACD>=0",
             "trend_up", "动量>15%", "两者交集", "top3中过tu"]
    r = ["<tr><th>段</th>" + "".join("<th>%s</th>" % h for h in heads) + "</tr>"]
    for k, lb, cl in order:
        s = seg[k]
        r.append('<tr><th class="%s">%s</th>' % (cl, lb)
                 + "".join("<td>%s</td>" % s[x] for x in keys) + "</tr>")
    return "".join(r)


def rate_table():
    order = [("A", "A", ""), ("B1", "B1", "hl"), ("B2", "B2", "bd")]
    keys = ["tu_rate", "both_rate", "d_rate", "b_rate",
            "days_both_ge2", "days_both_ge1", "days_top3_tu_ge1"]
    heads = ["tu 率%", "交集率%", "MACD 率%", "MA60 率%",
             "交集>=2只 天%", "交集>=1只 天%", "top3有tu 天%"]
    r = ["<tr><th>段</th>" + "".join("<th>%s</th>" % h for h in heads) + "</tr>"]
    for k, lb, cl in order:
        s = seg[k]
        r.append('<tr><th class="%s">%s</th>' % (cl, lb)
                 + "".join("<td>%s</td>" % s[x] for x in keys) + "</tr>")
    return "".join(r)


def monthly_svg():
    ks = [k for k in mon if k >= "202510"]
    vals = [mon[k]["both"] for k in ks]
    n = max(1, len(ks))
    W, H = 1000, 220
    pl, pb, pt = 40, 46, 14
    bw = (W - pl - 20) / n
    mx = max(max(vals), 1.0)
    bars = []
    for j in range(5):
        y = H - pb - (j / 4.0) * (H - pb - pt)
        bars.append('<line class="grid" x1="%d" x2="%d" y1="%.1f" y2="%.1f"/>'
                    % (pl, W - 20, y, y))
        bars.append('<text class="y" x="%d" y="%.1f">%.1f</text>'
                    % (pl - 6, y + 4, mx * j / 4))
    for i, (k, v) in enumerate(zip(ks, vals)):
        h = (v / mx) * (H - pb - pt)
        x = pl + i * bw
        y = H - pb - h
        cl = "b3" if v < 1.0 else ("b2" if v < 3 else "b1")
        bars.append('<rect class="%s" x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="2"/>'
                    % (cl, x, y, bw * 0.66, h))
        bars.append('<text x="%.1f" y="%.1f">%s</text>' % (x + bw * 0.33, y - 4, v))
        bars.append('<text class="x" transform="translate(%.1f,%.1f) rotate(-52)">%s</text>'
                    % (x + bw * 0.33, H - pb + 14, k))
    return '<svg viewBox="0 0 %d %d" class="chart">%s</svg>' % (W, H, "".join(bars))


def excess_rows():
    out = []
    for scheme, rows in E.items():
        pa = sum(1 for r in rows if r["ret"] > 0)
        pe = sum(1 for r in rows if r["excess"] > 0)
        out.append("<h3>划分 %s　　绝对正收 %d/%d (%.1f%%)"
                   "　　超额正收 %d/%d (%.1f%%)</h3>"
                   % (scheme, pa, len(rows), 100.0 * pa / len(rows),
                      pe, len(rows), 100.0 * pe / len(rows)))
        out.append("<table><tr><th>折区间</th><th>策略%</th><th>宇宙等权B&amp;H%</th>"
                   "<th>超额 pt</th><th>Sharpe</th></tr>")
        for r in rows:
            ex = r["excess"] * 100
            out.append('<tr class="%s"><td>%s</td><td>%.2f</td><td>%.2f</td>'
                       '<td class="%s">%+.2f</td><td>%.2f</td></tr>'
                       % ("negr" if r["ret"] < 0 else "", r["date"], r["ret"] * 100,
                          r["bh"] * 100, "neg" if ex < 0 else "pos", ex, r["sharpe"]))
        out.append("</table>")
    return "".join(out)


def stock_rows():
    out = ["<table><tr><th>#</th><th>代码</th><th>区间收益%</th><th>区间最大回撤%</th></tr>"]
    for i, s in enumerate(stk, 1):
        out.append('<tr><td>%d</td><td>%s</td><td class="%s">%+.2f</td><td>%.1f</td></tr>'
                   % (i, s["code"], "pos" if s["ret"] > 0 else "neg", s["ret"], s["mdd"]))
    out.append("</table>")
    return "".join(out)


up = sum(1 for s in stk if s["ret"] > 0)
rets = sorted(s["ret"] for s in stk)
med = rets[len(rets) // 2]
mean = sum(rets) / len(rets)

BODY = """
<h1>PM-EVOLVE 第 42 轮 · 弱市期（2026-04 ~ 2026-10）独立诊断</h1>
<div class="sub">D10 交付物 ｜ 运行时刻 2026-10-21 18:00（收盘后）｜ 面板 __PANEL__</div>
<div class="meta">EXECUTION_MODE = paper，已确认 ｜ 生产参数<b>零改动</b> ｜
tests 533/533 通过 ｜ 引擎未 kill、未重启</div>

<div class="kpis">
<div class="kpi"><div class="lb">当日跨日真实收益</div><div class="vl">0.000%</div>
  <div class="ex">972,533.60 恒定・262 快照・0 成交</div></div>
<div class="kpi"><div class="lb">连续零成交交易日</div><div class="vl">8</div>
  <div class="ex">2026-10-12 起（末笔成交 10-09）・非熔断</div></div>
<div class="kpi"><div class="lb">近 4 周滚动收益（KPI#4）</div><div class="vl bad">-2.46%</div>
  <div class="ex">锚 09-23 EOD 997,003.60 ｜ 目标 &gt; 0%</div></div>
<div class="kpi"><div class="lb">wf 均值 Sharpe（KPI#1）</div><div class="vl wn">1.669</div>
  <div class="ex">拼接口径 2.143 达标 ｜ D4 待裁决</div></div>
<div class="kpi"><div class="lb">wf 最大回撤（KPI#3）</div><div class="vl ok">-9.97%</div>
  <div class="ex">最差折 ｜ 目标 &gt;= -22% ｜ <span class="ok">PASS</span></div></div>
<div class="kpi"><div class="lb">本轮落盘参数</div><div class="vl">0</div>
  <div class="ex">参数空间穷尽性证明见下文第 5 节</div></div>
</div>

<h2>1 · 核心结论（一句话）</h2>
<div class="card p"><b>2026-04~2026-10 的「亏损」不是策略失灵，而是宇宙级 beta 崩塌</b>：
该区间 23 只标的<b>仅 __UP__/__N__ 只上涨、中位 __MED__%、均值 __MEAN__%</b>。
策略在同一段只亏 <b>4.6%~10.0%</b>，相对宇宙等权买入持有<b>超额 +12.6 ~ +24.6 pt</b>。
所谓「4 个负折」里 <b>3 个是大幅跑赢</b>。</div>

<h2>2 · 诊断①：弱市期其实是两段性质相反的状态</h2>
<p class="sub">逐日入场漏斗（宇宙 23 只）。A = 前 22 折所在区间；B1/B2 = 4 个负折所在区间的前后半。</p>
<table>__SEG__</table>
<table>__RATE__</table>

<div class="cols">
<div class="card w"><b>B1（20260423~20260715）—— 信号不但没断层，反而比历史更宽</b>
<ul>
<li>trend_up 日均 <b>6.88 只</b> &gt; A 段 <b>4.47 只</b></li>
<li>trend_up ∩ 动量&gt;15% 日均 <b>5.55 只</b> &gt; A 段 <b>2.97 只</b></li>
<li><b>91.1%</b> 的交易日有 &gt;=2 只同时通过（A 段仅 52.3%）</li>
<li>但同期策略 -4.64%、宇宙 <b>+8.10%</b> ⇒ 超额 <b class="bad">-12.74 pt</b>（全样本最差之一）</li>
</ul>
<b>⇒ 这不是「没票可买」，是「有票可买时买了就跌」——信号质量问题，不是信号数量问题。</b></div>

<div class="card d"><b>B2（20260716~20261021）—— 真正的信号断层</b>
<ul>
<li>trend_up 日均 <b>0.89 只</b>（A 段的 20%）</li>
<li>trend_up ∩ 动量 日均 <b>0.16 只</b>；<b>0.0%</b> 的交易日有 &gt;=2 只</li>
<li>top3 中过 trend_up 仅 <b>6.2%</b> 的天数</li>
<li>同期宇宙 -22% ~ -30%，策略 -5.7% ~ -10.0% ⇒ 超额 <b class="ok">+12.6 ~ +24.6 pt</b></li>
</ul>
<b>⇒ 回测自 2026-07-15 零建仓与实盘连续零 BUY 完全同源（D1 第 16 条证据）。</b></div>
</div>

<div class="card a"><b>绑定子条件真相（修正历史表述）</b>：
B2 里降幅最大的是 <code>b = MA20 &gt; MA60</code>（<b>53.0% → 24.9%</b>，-28.1pt），
其次 a（-21.4pt）、c（-22.4pt），<b>d = MACD 柱 &gt;= 0 降幅最小（-6.2pt）</b>。
⇒ 弱市失效由<b>中期均线多头排列瓦解</b>驱动，不是 MACD 滞后。<br>
另：<b>超乘性收紧</b> —— B2 中 4 个子条件各自仍有 5~10 只通过，而<b>交集只有 0.89 只</b>，
说明弱市里 4 个指标指向的是<b>不同的股票</b>（市场没有统一多头趋势）。
这条解释了历史上「放宽任一分量」全部失败的原因。</div>

<h3>逐月信号宽度（trend_up ∩ 动量&gt;15%，单位：只）</h3>
__SVG__
<p class="meta">蓝 = 宽度健康（&gt;=3 只）　黄 = 偏窄（1~3）　红 = 近乎无票（&lt;1）　｜ 仅展示 2025-10 之后</p>

<h2>3 · 诊断②：个股层面 —— 这是一次宇宙级 beta 崩塌</h2>
<p class="sub">区间 20260423~20261021，按收益降序</p>
__STK__

<h2>4 · 诊断③：把 R40 建议的替代 KPI 真正算出来 —— 结果是被证伪</h2>
<p class="sub">R40 教训第 21 条：「提替代 KPI 前必须先把替代口径算出来」。本轮完成实算。</p>
<table><tr><th>口径</th><th>正收比例</th><th>目标 85.7%</th></tr>
<tr><td>绝对正收折（现行 KPI#2）</td><td>21/26 = 80.8%</td><td class="neg">FAIL</td></tr>
<tr><td>正收益日历月（R40 实测）</td><td>15/28 = 53.6%</td><td class="neg">FAIL</td></tr>
<tr><td><b>相对宇宙 B&amp;H 超额正收（本轮实算）</b></td><td><b>15/26 = 57.7%</b></td>
<td class="neg">FAIL</td></tr>
</table>
<div class="card w"><b>⇒ 三个互相独立的口径全部不可达 85.7%。</b>
「相对宇宙超额」这条替代口径<b>同样否决</b>，且比现行口径更差（80.8% → 57.7%）。
这是 D9（KPI#2 目标本身）的第三种独立证据。</div>

<h3>4 个「负折」的超额拆解</h3>
__EXC__

<h2>5 · 参数空间穷尽性证明（126 字段枚举）</h2>
<div class="card"><p>对 <code>BacktestConfig</code> 全部 <b>126 个字段</b>做
「实盘读取点 × 决策史提及次数」交叉枚举，读取点范围含 <code>engine/event_engine.py</code>、
<code>strategy/trend_strategy.py</code>、<code>strategy/daily_context.py</code>、
<code>risk/position_sizer.py</code>、<code>risk/risk_manager.py</code>。</p>
<ul>
<li><b>有实盘读取点且已扫过并闭合</b>：atr_stop_mult / tp_atr_mult / hard_stop_pct /
max_positions / max_single_position_pct / risk_per_trade / vol_sizing / trend_vol_sizing /
min_daily_bias / buy_score_threshold / min_signals / min_passing_candidates / momentum_top_n /
momentum_lookback / momentum_min_mom / exit_mode / down_day_exit_pct / daily_stop_flatten_pct /
nb_lookback / northbound_mode / nb_credibility_guard / manual_entry_* 等。</li>
<li><b>有实盘读取点但仍未扫的只剩 16 个，且全部属于已闭合家族</b>：trailing_stop（trailing 家族）、
rotation_* ×7（已停用）、regime_* ×5、conviction_breadth_thr、nb_stale/degenerate_days（死闸门）、
t1_restriction（铁律不可动）。</li>
<li><b>无实盘读取点的字段按纪律 7 一律禁止落盘</b>：mom_metric / momentum_weight / vol_confirm /
risk_parity / reentry_cooldown / trend_exit_ma / max_per_sector / dd_ctrl* / tail_* / earnrev_* /
moneyflow_* / equity_dd_stop_* / rm_dd_pause_* / trend_up_relax 等。</li>
</ul>
<b>⇒ 本轮不存在任何合法候选参数。</b>「参数空间已无可挖 alpha」由经验判断升级为可复核的穷举结论。</div>

<h2>6 · 不变性复核（四窗共识，4m31s）</h2>
<table><tr><th>项</th><th>R41（11:40 盘中 bar）</th><th>本轮（18:00 收盘 bar）</th><th>结论</th></tr>
<tr><td>IS 收益 / Sharpe</td><td>+301.05% / +1.93</td><td>+301.0% / +1.93</td>
<td class="pos">逐位相同</td></tr>
<tr><td>IS MDD</td><td>-12.97%</td><td>-12.97%</td><td class="pos">逐位相同</td></tr>
<tr><td>4 窗 OOS 均值 Sharpe</td><td>1.669</td><td>1.669</td><td class="pos">逐位相同</td></tr>
<tr><td>最差折</td><td>-10.0%</td><td>-10.0%</td><td class="pos">逐位相同</td></tr>
<tr><td>OOS/IS Sharpe 差距（闸门③）</td><td>—</td><td>13.3%</td><td class="pos">PASS（&lt;=25%）</td></tr>
</table>
<p class="meta">⇒ R41 「末端未收盘 bar」的量测卫生风险本轮实测为零。
同轮确认：动量阈值轴 X3（0.15）与相对回退轴 Y0（关闭）在两个候选族里仍是 argmax，
其余 12 个变体四窗最差 dSh 全负。</p>

<h2>7 · 本轮新增待办（交 OWNER）</h2>
<div class="card w"><b>新增 1 项「待重启」</b>：提交 <code>7f57bfb</code>（2026-10-21 16:29）改
<code>engine/event_engine.py</code> —— <code>_market_age_recovering()</code> 改为无条件先算一次，
修复「宽限期内从未被调用 ⇒ <code>_last_market_max_age</code> 恒 -1 ⇒ R37 补强 1 形同失效」
（10-21 12:58:39.785 距宽限边界仅差 32ms 暴露）。<br>
<b>引擎启动 2026-10-19 21:06:53 &lt; mtime 2026-10-21 16:29 ⇒ 尚未加载</b>。
属行情陈旧误报抑制，不影响仓位/风控，随下次重启生效。本轮按铁律未重启。</div>

<div class="card a"><b>下一轮唯一值得投的子问题：20260423 ~ 20260721（B1）</b><br>
全样本唯一「有票且真落后」的窗口（超额 -12.74 pt），机理＝<b>宽信号下的趋势尾段追入</b>。
建议方向<b>不是</b>放宽/收紧 trend_up（已闭合，且第 2 节给出机理层解释为何放宽无效），
而是<b>趋势成熟度 / 持续性</b>类新特征（需新增实现，属结构性候选）。</div>

<p class="meta" style="margin-top:34px">
完整逐折数据与决策依据见 <code>reports/EVOLUTION_DECISIONS.md</code>
（§2026-10-21 PM-EVOLVE 第 42 轮）、<code>reports/_r42_weak_market_diag.json</code>、
<code>reports/_r42_fold_excess.json</code>。
</p>
"""

BODY = (BODY.replace("__PANEL__", D["panel"])
            .replace("__UP__", str(up)).replace("__N__", str(len(stk)))
            .replace("__MED__", "%+.2f" % med).replace("__MEAN__", "%+.2f" % mean)
            .replace("__SEG__", seg_table()).replace("__RATE__", rate_table())
            .replace("__SVG__", monthly_svg()).replace("__STK__", stock_rows())
            .replace("__EXC__", excess_rows()))

HTML = ('<!DOCTYPE html>\n<html lang="zh-CN"><head><meta charset="utf-8">\n'
        '<title>PM-EVOLVE R42 · 弱市期（2026-04~10）独立诊断</title>\n'
        '<style>' + CSS + '</style></head><body><div class="wrap">'
        + BODY + '</div></body></html>\n')

p = "reports/r42_weak_market_diag.html"
open(p, "w", encoding="utf-8").write(HTML)
print("wrote", p, len(HTML), "chars")

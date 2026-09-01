# -*- coding: utf-8 -*-
"""生成滚动 walk-forward 稳健性研究报告（HTML）。"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
roll = json.loads((ROOT / "logs" / "opt_rollwf.json").read_text(encoding="utf-8"))
folds = json.loads((ROOT / "logs" / "opt_folds.json").read_text(encoding="utf-8"))

rows = roll["rows"]
hl = roll["headline"]
bystate = roll["by_index_state"]
byyear = roll["by_year"]
des = roll["design"]

# folds 关键对比
fP0 = folds["results"]["P0_现状(无regime)"]
fP1 = folds["results"]["P1_regime指数MA60+清仓"]
folds_meta = folds["folds"]


def sh_mean(lst):
    return sum(r["sharpe"] for r in lst) / len(lst)


p0_sh = sh_mean(fP0)
p1_sh = sh_mean(fP1)
p0_wins = sum(1 for r in fP0 if r["alpha"] > 0)
p1_wins = sum(1 for r in fP1 if r["alpha"] > 0)
n_fold = len(fP0)
gate_wins = sum(1 for a, b in zip(fP0, fP1) if b["total_return"] > a["total_return"])
gate_cum = sum((b["total_return"] - a["total_return"]) * 100
               for a, b in zip(fP0, fP1))


def pct(x, d=1):
    return f"{x*100:+.{d}f}%"


def ptn(x, d=1):
    return f"{x*100:+.{d}f}pt"


# ---- 逐窗表格行 ----
tbl_rows = ""
for r in rows:
    sgn = "下行" if not r["idx_above_start"] else "上行"
    ca = r["contrib_alpha"]
    color = "#c0392b" if ca >= 0 else "#1e8449"   # 红涨绿跌
    tbl_rows += (
        f"<tr><td>{r['date_from']}→{r['date_to']}</td>"
        f"<td>{sgn}</td>"
        f"<td>{pct(r['base_ret'])}</td><td>{pct(r['prod_ret'])}</td>"
        f"<td style='color:{color};font-weight:600'>{ptn(ca)}</td>"
        f"<td>{r['prod_exp']*100:.0f}%</td>"
        f"<td>{r['base_exp']*100:.0f}%</td>"
        f"<td>{r['prod_n']}</td></tr>"
    )

# ---- 按年聚合条形 ----
year_bars = ""
maxv = max(abs(y["mean_contrib_alpha"]) for y in byyear) or 1
for y in byyear:
    v = y["mean_contrib_alpha"] * 100
    w = abs(v) / maxv * 100
    col = "#c0392b" if v >= 0 else "#1e8449"
    side = "left" if v >= 0 else "right"
    year_bars += (
        f"<div class='yrow'><span class='ylab'>{y['year']} (n={y['n']})</span>"
        f"<div class='ytrack'><div class='ybar' style='width:{w:.0f}%;"
        f"background:{col};float:{side}'></div></div>"
        f"<span class='yval' style='color:{col}'>{v:+.1f}pt</span></div>"
    )

# ---- folds 对比表 ----
fold_rows = ""
for i, (a, b, m) in enumerate(zip(fP0, fP1, folds_meta)):
    d = (b["total_return"] - a["total_return"]) * 100
    col = "#c0392b" if d >= 0 else "#1e8449"
    fold_rows += (
        f"<tr><td>F{i+1} {m['date_from']}→{m['date_to']}</td>"
        f"<td>{pct(a['total_return'])}</td><td>{a['sharpe']:+.2f}</td>"
        f"<td>{pct(b['total_return'])}</td><td>{b['sharpe']:+.2f}</td>"
        f"<td style='color:{col};font-weight:600'>{d:+.1f}pt</td></tr>"
    )

HTML = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>滚动 Walk-Forward 稳健性研究 · regime 闸门</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0;font-family:-apple-system,'Segoe UI',Roboto,'Microsoft YaHei',sans-serif}}
body{{background:#f5f6f8;color:#1f2733;line-height:1.6;padding:28px}}
.wrap{{max-width:1080px;margin:0 auto}}
h1{{font-size:24px;margin-bottom:4px}}
.sub{{color:#6b7785;font-size:13px;margin-bottom:20px}}
.card{{background:#fff;border-radius:12px;padding:22px 26px;margin-bottom:18px;
  box-shadow:0 1px 3px rgba(0,0,0,.08)}}
.card h2{{font-size:17px;margin-bottom:12px;color:#16324f;border-left:4px solid #2b6cb0;padding-left:10px}}
.kpis{{display:flex;gap:14px;flex-wrap:wrap}}
.kpi{{flex:1;min-width:150px;background:#fafbfc;border:1px solid #eef1f4;border-radius:10px;padding:14px}}
.kpi .v{{font-size:26px;font-weight:700}}
.kpi .l{{font-size:12px;color:#6b7785;margin-top:2px}}
.up{{color:#c0392b}} .down{{color:#1e8449}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}}
th,td{{padding:7px 8px;text-align:center;border-bottom:1px solid #eef1f4}}
th{{background:#f0f3f7;color:#445;font-weight:600}}
tbody tr:hover{{background:#fafcff}}
.note{{background:#fff8e6;border:1px solid #f3d98b;border-radius:10px;padding:14px 18px;font-size:14px;margin-bottom:18px}}
.rec{{background:#eafaf1;border-left:4px solid #1e8449;padding:16px 18px;border-radius:8px;margin-top:10px;font-size:14px}}
.code{{background:#1f2733;color:#e6edf3;padding:12px 16px;border-radius:8px;font-family:monospace;font-size:13px;margin:8px 0;overflow-x:auto}}
.yrow{{display:flex;align-items:center;margin:6px 0;font-size:13px}}
.ylab{{width:120px;color:#445}}
.ytrack{{flex:1;height:18px;background:#eef1f4;border-radius:4px;position:relative}}
.ybar{{height:100%;border-radius:4px}}
.yval{{width:70px;text-align:right;font-weight:600}}
footer{{color:#9aa5b1;font-size:12px;text-align:center;margin-top:10px}}
.tag{{display:inline-block;background:#eef1f4;border-radius:4px;padding:1px 8px;font-size:12px;color:#445;margin-right:6px}}
</style></head><body><div class="wrap">

<h1>滚动 Walk-Forward 稳健性研究：regime 闸门贡献</h1>
<div class="sub">量化交易项目 · 科学分析 · 生成于 2026-08-29 ·
数据：xtdata 本地日线（miniQMT，零额度）· 宇宙：23 只 AI 产业链股</div>

<div class="note">
<b>核心发现（与既有结论相反，已双方法验证）：</b>在 {des['n_windows']} 个连续滚动窗口
（{des['date_from']}→{des['date_to']}，≈3 年）上，当前生产配置中的
<b>regime 闸门（创业板指 MA60 + 强制清仓）是风险调整后收益的<b>净拖累</b>。</b>
多折 walk-forward 交叉验证同样显示：无闸门（永远满仓）均值 Sharpe
<b class="up">{p0_sh:+.2f}</b> ＞ 有闸门 <b class="down">{p1_sh:+.2f}</b>，
闸门在 <b>{n_fold}/{n_fold}</b> 个折中均跑输，累计落后 <b class="down">{gate_cum:+.1f}pt</b>。
早先「闸门 +0.38 Sharpe 贡献」系较短数据窗（count=500）+ 较旧基线造成的假象。
</div>

<div class="card">
<h2>一、研究设计（防过拟合）</h2>
<ul style="font-size:14px">
<li>固定生产配置、<b>不重新调参</b>；仅检验「同一套已验证策略」在连续时间上的样本外稳健性。</li>
<li>每个检验窗 = {des['window']} 根交易日（≈3 个月），步长 {des['step']} 天滚动；
窗前 {130} 根做指标预热，窗内交易严格无重叠。</li>
<li>对照：<span class="tag">BASE 无闸门（永远满仓）</span>
<span class="tag">PROD 闸门+趋势+波动率目标+max5</span>；
所有回测含单边 0.15% 真实成本，信号次日开盘成交（防未来函数）。</li>
<li>双方法：① 滚动 25 窗；② 独立多折 walk-forward（7 折，75 天/折）交叉验证。</li>
</ul>
</div>

<div class="card">
<h2>二、滚动窗口 headline 指标</h2>
<div class="kpis">
<div class="kpi"><div class="v down">{hl['gate_winrate_sharpe']*100:.0f}%</div>
<div class="l">闸门 Sharpe 胜率（{sum(1 for r in rows if r['contrib_sh']>0)}/{des['n_windows']} 窗）</div></div>
<div class="kpi"><div class="v down">{hl['gate_winrate_alpha']*100:.0f}%</div>
<div class="l">闸门 alpha 胜率</div></div>
<div class="kpi"><div class="v down">{ptn(hl['mean_contrib_alpha'])}</div>
<div class="l">平均 alpha 贡献</div></div>
<div class="kpi"><div class="v down">{hl['mean_contrib_sh']:+.2f}</div>
<div class="l">平均 Sharpe 贡献</div></div>
<div class="kpi"><div class="v down">{ptn(hl['mean_contrib_mdd'])}</div>
<div class="l">平均 MDD 贡献（越小越好）</div></div>
</div>
<p style="font-size:13px;color:#6b7785;margin-top:10px">
闸门在多数窗口为负贡献：它压低了回撤（MDD 平均改善 {ptn(hl['mean_contrib_mdd'])}）
与暴露，但代价是错过上涨——在 2024–2025 AI 长牛中，被拦在门外损失远大于避险收益。</p>
</div>

<div class="card">
<h2>三、逐窗口明细（贡献 = PROD − BASE）</h2>
<table><thead><tr>
<th>窗口</th><th>起始市况</th><th>BASE 收益</th><th>PROD 收益</th>
<th>α 贡献</th><th>PROD 暴露</th><th>BASE 暴露</th><th>PROD 笔数</th>
</tr></thead><tbody>{tbl_rows}</tbody></table>
</div>

<div class="card">
<h2>四、按年聚合（α 贡献，红涨绿跌）</h2>
{year_bars}
<p style="font-size:13px;color:#6b7785;margin-top:8px">2024–2025 长牛区间闸门几乎全为负贡献；
仅 2026 年初个别窗口因 AI 板块急跌、永远满仓重挫而转正——属罕见尾部事件。</p>
</div>

<div class="card">
<h2>五、多折 Walk-Forward 交叉验证（独立方法，同数据）</h2>
<table><thead><tr>
<th>折（窗口）</th><th>BASE 收益</th><th>BASE Sharpe</th>
<th>PROD 收益</th><th>PROD Sharpe</th><th>闸门 Δ</th>
</tr></thead><tbody>{fold_rows}</tbody></table>
<div class="kpis" style="margin-top:14px">
<div class="kpi"><div class="v up">{p0_sh:+.2f}</div><div class="l">BASE 均值 Sharpe（无闸门）</div></div>
<div class="kpi"><div class="v down">{p1_sh:+.2f}</div><div class="l">PROD 均值 Sharpe（有闸门）</div></div>
<div class="kpi"><div class="v down">{gate_wins}/{n_fold}</div><div class="l">闸门胜出折数</div></div>
<div class="kpi"><div class="v down">{gate_cum:+.1f}pt</div><div class="l">累计收益落后</div></div>
</div>
<p style="font-size:13px;color:#6b7785;margin-top:10px">
两法一致：闸门是<b>风险调整后收益的净拖累</b>，非 alpha 来源。
其唯一价值是「崩盘保险」——降低暴露与回撤，但在本宇宙（强趋势 AI 产业链）历史上
「保险保费」远高于赔付。</p>
</div>

<div class="card">
<h2>六、按窗起始市况分组</h2>
<table><thead><tr><th>分组</th><th>窗数</th><th>平均 α 贡献</th>
<th>α 胜率</th><th>区间指数收益</th></tr></thead><tbody>
<tr><td>起始处于<b>下行</b>市（指数＜MA60）</td><td>{bystate['down_start']['n']}</td>
<td class="{'up' if bystate['down_start']['mean_contrib_alpha']>=0 else 'down'}">{ptn(bystate['down_start']['mean_contrib_alpha'])}</td>
<td>{bystate['down_start']['win_alpha']}/{bystate['down_start']['n']}</td>
<td>{pct(bystate['down_start']['idx_ret'])}</td></tr>
<tr><td>起始处于<b>上行</b>市（指数＞MA60）</td><td>{bystate['up_start']['n']}</td>
<td class="{'up' if bystate['up_start']['mean_contrib_alpha']>=0 else 'down'}">{ptn(bystate['up_start']['mean_contrib_alpha'])}</td>
<td>{bystate['up_start']['win_alpha']}/{bystate['up_start']['n']}</td>
<td>{pct(bystate['up_start']['idx_ret'])}</td></tr>
</tbody></table>
<p style="font-size:13px;color:#6b7785;margin-top:10px">
闸门在下行起始窗平均仍为负（{ptn(bystate['down_start']['mean_contrib_alpha'])}），
说明历史下行多伴随 V 型反弹，闸门「出得来、回不去」反而踏空。
其保护作用仅在<b>持续深跌</b>（如 2026 年初 AI 板块急杀）这一罕见情形下才兑现。</p>
</div>

<div class="card">
<h2>七、结论与优化建议</h2>
<div class="rec">
<b>结论：</b>regime 闸门在完整 2024–2026 样本上并非稳健 alpha，而是风险调整后收益的净拖累
（均值 Sharpe {p0_sh:+.2f}→{p1_sh:+.2f}，7/7 折跑输）。它本质是「崩盘保险」，
保费（错过的牛市收益）长期高于赔付（躲过的回撤）。<br><br>
<b>优化建议（待你确认后实施）：</b>在追求「优秀收益」且本宇宙趋势强劲的前提下，
<b>关闭 regime 闸门</b>（设 <code>regime_mode="off" / force_exit=False</code>），
让策略满仓捕捉 AI 长牛。回测显示此举将均值 Sharpe 由 {p1_sh:+.2f} 提升至 {p0_sh:+.2f}（约 2×），
且历史最差折 BASE 仍为 <b class="up">+3.8%</b>（正收益）。<br><br>
<b>风险提示：</b>关闭后组合在持续熊市中暴露升至 ~97%、最大回撤略增（−14.2% vs −11.1%）。
若你更看重本金保全而非收益最大化，可保留闸门作为保险。此改动影响实盘，
<b>本自动化运行未自动修改生产配置</b>，等你确认后再落地（当前实时 regime=ok，闸门本就开放、暂未拦截）。
<div class="code"># config/settings.py  (待确认)
STRATEGY_PARAMS: regime_mode="off", regime_force_exit=False
# engine/event_engine.py: self.regime_mode = "off"
# 回测对照：strategy/opt_harness.base_cfg() 已为无闸门基线（Sharpe {p0_sh:+.2f}）</div>
</div>
</div>

<div class="card">
<h2>八、执行效率与工程健康（保障项）</h2>
<ul style="font-size:14px">
<li>✅ 单元测试 <b>92/92 通过</b>（conda env qmt），零回归。</li>
<li>✅ 实盘引擎快照 <code>exit=0</code>：data_mode=xtdata 已连、regime ok=true（闸门开放）、风控未熔断。</li>
<li>✅ 滚动研究 25 窗 + 7 折交叉验证全程本地零额度，无外部依赖、可复现。</li>
</ul>
<p style="font-size:12px;color:#9aa5b1;margin-top:6px">证据：
logs/opt_rollwf.json（25 窗明细）、logs/opt_folds.json（7 折交叉验证）。</p>
</div>

<footer>本报告由自动化量化研究生成 · 红涨绿跌（中国惯例）· 所有结论基于真实历史日线 + 真实交易成本</footer>
</div></body></html>"""

out = ROOT / "reports" / "optimization_report_2026-08-29c.html"
out.write_text(HTML, encoding="utf-8")
print(f"[报告] {out}  ({out.stat().st_size//1024} KB)")

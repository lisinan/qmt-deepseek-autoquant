// dashboard.js —— qmtIDE-deepseek 实时仪表板前端

(function () {
  'use strict';
  const UNIVERSE = window.UNIVERSE || {};
  const STOCK_NAMES = window.STOCK_NAMES || {};
  function NAME(code) {
    // 优先完整名称映射（含动态候选池/全市场），回退静态 UNIVERSE，再回退代码
    return STOCK_NAMES[code] || UNIVERSE[code] || code;
  }

  const $ = (id) => document.getElementById(id);
  function fmt(n, digits) {
    if (digits === undefined) digits = 2;
    if (n === null || n === undefined || n === '') return '--';
    if (typeof n !== 'number') return n;
    return n.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }
  function colorClass(pct) {
    if (pct > 0) return 'up';
    if (pct < 0) return 'down';
    return 'flat';
  }
  function shortTs(ts) {
    // 【2026-09-21】原实现只渲染 HH:mm:ss，跨日的旧信号（例如 4 天前的 15:03）
    // 会显示成"15:03"，看起来就像当天产生的 → 把停摆的信号源误判为实时。
    // 现在：非今天的信号补上 MM-DD，日期归属一眼可辨。
    try {
      var d = new Date(ts);
      if (isNaN(d.getTime())) return ts;
      var t = d.toLocaleTimeString('zh-CN', { hour12: false });
      var now = new Date();
      var sameDay = d.getFullYear() === now.getFullYear()
        && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
      if (sameDay) return t;
      var md = (d.getMonth() + 1) + '-' + ('0' + d.getDate()).slice(-2);
      return md + ' ' + t;
    } catch (e) { return ts; }
  }
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  // 【2026-09-22 配色重做】原实现是 灰→亮绿 的单色渐变，有两个问题：
  //   ① 低热度区间（0~5）几乎全是暗色，冷/热分不出来，热力图失去了"图"的意义；
  //   ② 用绿色代表高热，与中国股市「绿=跌」的直觉相反，容易被误读成走弱。
  // 现在改成真正的冷→热色阶，并**刻意避开绿色**：
  //   蓝(冷) → 青(温) → 琥珀(暖) → 橙(热) → 红(极热)
  // 涨跌幅仍然单独按中国习惯用红涨/绿跌，两套语义不打架。
  const HEAT_STOPS = [
    [0.0, [61, 74, 92]],    // 冷：灰蓝
    [3.0, [63, 124, 184]],  // 蓝
    [5.0, [47, 166, 160]],  // 青
    [6.5, [217, 164, 65]],  // 琥珀
    [8.0, [232, 114, 45]],  // 橙
    [10.0, [224, 49, 49]],  // 极热：红
  ];

  function heatColor(score) {
    if (score == null) return '#3d4a5c';
    const s = Math.max(0, Math.min(10, Number(score) || 0));
    for (let i = 0; i < HEAT_STOPS.length - 1; i++) {
      const [a, ca] = HEAT_STOPS[i], [b, cb] = HEAT_STOPS[i + 1];
      if (s <= b) {
        const t = (s - a) / (b - a || 1);
        const c = ca.map((v, k) => Math.round(v + (cb[k] - v) * t));
        return 'rgb(' + c.join(',') + ')';
      }
    }
    return 'rgb(224,49,49)';
  }

  // 热度等级文字（配合色阶，数值之外再给一个可读的定性标签）
  function heatLevel(score) {
    const s = Number(score) || 0;
    if (s >= 8.0) return '极热';
    if (s >= 6.5) return '偏热';
    if (s >= 5.0) return '温和';
    if (s >= 3.0) return '偏冷';
    return '冷';
  }

  // 涨跌幅着色：严格中国习惯 涨=红 / 跌=绿
  function chgColor(pct) {
    const p = Number(pct) || 0;
    if (p > 0) return '#ef5350';
    if (p < 0) return '#26a67a';
    return '#8898a6';
  }
  function chgArrow(pct) {
    const p = Number(pct) || 0;
    return p > 0 ? '▲' : (p < 0 ? '▼' : '—');
  }
  function signed(pct, d) {
    const p = Number(pct) || 0;
    return (p > 0 ? '+' : '') + fmt(p, d === undefined ? 2 : d);
  }

  // ============================================================ 行模板

  function tickRow(code, t) {
    const cls = colorClass(t.change_pct);
    return '<tr>' +
      '<td>' + esc(code) + '</td>' +
      '<td>' + esc(NAME(code)) + '</td>' +
      '<td class="num">' + fmt(t.price, 3) + '</td>' +
      '<td class="num ' + cls + '">' +
        (t.change_pct >= 0 ? '+' : '') + fmt(t.change_pct, 2) + '%</td>' +
      '<td class="num">' + (t.volume || 0).toLocaleString() + '</td>' +
      '<td><span class="badge ' + (t.source || 'mock') + '">' +
        (t.source || 'mock') + '</span></td>' +
      '</tr>';
  }
  function signalRow(s) {
    const cls = s.side === 'BUY' ? 'up' : (s.side === 'SELL' ? 'down' : 'flat');
    return '<tr>' +
      '<td>' + esc(shortTs(s.ts)) + '</td>' +
      '<td>' + esc(s.code) + ' ' + esc(NAME(s.code)) + '</td>' +
      '<td class="' + cls + '">' + esc(s.side) + '</td>' +
      '<td class="num">' + fmt(s.score, 2) + '</td>' +
      '<td class="num">' + fmt(s.price, 3) + '</td>' +
      '<td title="' + esc(s.reason || '') + '">' +
        esc((s.reason || '').slice(0, 40)) + '</td>' +
      '</tr>';
  }
  function fillRow(f) {
    const cls = f.side === 'BUY' ? 'up' : 'down';
    return '<tr>' +
      '<td>' + esc(shortTs(f.ts)) + '</td>' +
      '<td>' + esc(f.code) + ' ' + esc(NAME(f.code)) + '</td>' +
      '<td class="' + cls + '">' + esc(f.side) + '</td>' +
      '<td class="num">' + f.quantity + '</td>' +
      '<td class="num">' + fmt(f.price, 3) + '</td>' +
      '<td class="num">' + fmt(f.amount, 0) + '</td>' +
      '</tr>';
  }
  function posRow(code, p) {
    const cls = colorClass(p.pnl_pct);
    // 优先用持仓自带名称（后端已解析为中文名），否则回退前端 NAME() 映射，
    // 再回退代码本身——保证「持仓窗口」始终显示公司名称而非裸代码。
    const nm = (p.name && p.name !== code) ? p.name : NAME(code);
    return '<tr>' +
      '<td class="code">' + esc(code) + '</td>' +
      '<td>' + esc(nm) + '</td>' +
      '<td class="num">' + p.quantity + '</td>' +
      '<td class="num">' + fmt(p.avg_cost, 3) + '</td>' +
      '<td class="num">' + fmt(p.last_price, 3) + '</td>' +
      '<td class="num ' + cls + '">' +
        (p.pnl_pct >= 0 ? '+' : '') + fmt(p.pnl_pct, 2) + '%</td>' +
      '</tr>';
  }

  // ============================================================ 渲染

  function renderSnapshot(snap) {
    if (!snap) return;
    $('data-mode').textContent = 'data: ' + snap.data_mode;
    $('data-mode').className = 'badge ' + snap.data_mode;
    $('broker-mode').textContent = 'broker: ' + snap.broker_mode;
    $('broker-mode').className = 'badge ' + (snap.broker_connected ? 'live' : 'mock');
    $('exec-mode').textContent = 'exec: ' + snap.exec_mode;
    $('strat-mode').textContent = 'strategy: ' + snap.strategy_mode;
    $('tick-count').textContent = snap.tick + ' ticks';
    $('kpi-asset').textContent = fmt(snap.total_asset, 0);
    $('kpi-cash').textContent = fmt(snap.cash, 0);
    $('kpi-pos').textContent = Object.keys(snap.positions || {}).length;

    var posBody = $('pos-table').querySelector('tbody');
    posBody.innerHTML = '';
    Object.keys(snap.positions || {}).forEach(function (code) {
      posBody.insertAdjacentHTML('beforeend', posRow(code, snap.positions[code]));
    });

    var r = snap.risk || {};
    $('risk-halted').textContent = r.halted ? ('熔断 (' + r.halt_reason + ')') : '正常';
    $('risk-halted').className = r.halted ? 'halted' : 'ok';
    // 风控卡片整体状态色：熔断红框 / 正常绿框
    $('risk-box').className = 'risk-box ' + (r.halted ? 'alert' : 'safe');
    // 日内盈亏：优先显示真实日内总盈亏（已实现 + 当日浮动），无基线时回退已实现 daily_pnl
    var ip = (r.intraday_pnl != null) ? r.intraday_pnl : (r.daily_pnl || 0);
    var ipc = (r.intraday_pnl_pct != null) ? r.intraday_pnl_pct : null;
    var ipStr = fmt(ip, 0) + (ipc != null ? ' (' + (ipc >= 0 ? '+' : '') + fmt(ipc, 2) + '%)' : '');
    $('risk-pnl').textContent = ipStr;
    $('risk-pnl').className = ip >= 0 ? 'ok' : 'halted';
    $('risk-consec').textContent = r.consecutive_losses;
    $('risk-scale').textContent = fmt(r.position_scale, 2);
    $('risk-trades').textContent = r.daily_trade_count;

    // 北向资金轴（正交闸门，2026-09-19 落盘）：展示 mode/滚动净买入/是否拦截
    var nb = snap.northbound || {};
    $('nb-mode').textContent = 'mode: ' + (nb.mode || 'off');
    $('nb-blocked').textContent = nb.blocked ? '拦截新开仓' : '放行';
    $('nb-blocked').className = nb.blocked ? 'halted' : 'ok';
    $('nb-box').className = 'risk-box ' + (nb.blocked ? 'alert' : 'safe');
    // 原值单位为万元，前端换算成「亿元」便于阅读
    $('nb-rolling').textContent = (nb.rolling_sum != null)
      ? fmt(nb.rolling_sum / 10000, 2) : '--';
    $('nb-latest').textContent = (nb.latest != null)
      ? fmt(nb.latest / 10000, 2) : '--';
    $('nb-meta').textContent = (nb.lookback || '-') + '日 / 覆盖 '
      + (nb.days || 0) + '日' + (nb.end_date ? (' 至 ' + nb.end_date) : '');
  }

  function renderTicks(ticks) {
    var body = $('tick-table').querySelector('tbody');
    body.innerHTML = '';
    Object.keys(ticks || {}).forEach(function (code) {
      body.insertAdjacentHTML('beforeend', tickRow(code, ticks[code]));
    });
  }

  function appendSignals(sigs) {
    if (!sigs || !sigs.length) return;
    var body = $('signal-table').querySelector('tbody');
    var newest = '';
    sigs.forEach(function (s) { if (s.ts > newest) newest = s.ts; });
    if (newest) updateSignalFreshness(newest);
    sigs.slice().reverse().forEach(function (s) {
      body.insertAdjacentHTML('afterbegin', signalRow(s));
    });
    while (body.children.length > 30) body.removeChild(body.lastChild);
  }
  // 【2026-09-21】信号源新鲜度徽标。
  // 背景：策略判定为 HOLD 的信号默认不入库（PERSIST_HOLD_SIGNALS=False），
  // 市场转弱时会出现「连日零信号」。表格里残留的旧信号又没有日期，
  // 很容易被误读成"策略在正常发信号但没成交"。这里显式给出停滞时长。
  function updateSignalFreshness(ts) {
    var el = $('signal-fresh');
    if (!el || !ts) return;
    var d = new Date(ts);
    if (isNaN(d.getTime())) return;
    var mins = Math.floor((Date.now() - d.getTime()) / 60000);
    var txt, cls = 'fresh-ok';
    if (mins < 30) {
      txt = '最新信号 ' + mins + ' 分钟前';
    } else if (mins < 60 * 24) {
      txt = '⚠ 已 ' + mins + ' 分钟无新信号';
      cls = 'fresh-warn';
    } else {
      txt = '⚠ 已 ' + Math.floor(mins / 1440) + ' 天无新信号（信号源停摆）';
      cls = 'fresh-stale';
    }
    el.textContent = txt;
    el.className = 'signal-fresh ' + cls;
  }

  // 【2026-09-22】行情新鲜度徽标。
  // 之前 tick 没有时间戳，页面只能显示 tick 计数，行情停更（订阅掉线 /
  // 主循环被慢调用拖住）时毫无提示，用户只能看到"价格好像没变"。现在后端
  // 回传行情真实时间（market_data_ts）与单轮耗时（round_ms / slow_rounds），
  // 这里把「行情滞后 N 秒」与「主循环卡顿」显式化。
  function updateMarketFreshness(snap) {
    var el = $('market-fresh');
    if (!el) return;
    var ts = snap && snap.market_data_ts;
    if (!ts) { el.textContent = '无行情时间'; el.className = 'signal-fresh fresh-stale'; return; }
    var d = new Date(ts);
    if (isNaN(d.getTime())) { el.textContent = '行情时间无效'; el.className = 'signal-fresh fresh-stale'; return; }
    var sec = Math.floor((Date.now() - d.getTime()) / 1000);
    var txt, cls = 'fresh-ok';
    if (sec < 30) {
      txt = '行情 ' + sec + ' 秒前';
    } else if (sec < 180) {
      txt = '⚠ 行情滞后 ' + sec + ' 秒';
      cls = 'fresh-warn';
    } else {
      txt = '⚠ 行情滞后 ' + Math.floor(sec / 60) + ' 分钟（疑似停更）';
      cls = 'fresh-stale';
    }
    var slow = (snap && snap.slow_rounds) || 0;
    if (slow > 0) {
      txt += ' · 主循环慢轮 ' + slow + ' 次';
      cls = 'fresh-warn';
    }
    el.textContent = txt;
    el.className = 'signal-fresh ' + cls;
  }

  function appendFills(fills) {
    if (!fills || !fills.length) return;
    var body = $('fill-table').querySelector('tbody');
    fills.slice().reverse().forEach(function (f) {
      body.insertAdjacentHTML('afterbegin', fillRow(f));
    });
    while (body.children.length > 30) body.removeChild(body.lastChild);
  }

  // ----- 产业链热力图 -----
  // 【2026-09-22 展示重做】原实现的问题：
  //   ① 按配置顺序平铺，不排序 → 看不出谁最热；
  //   ② 只给热度/涨幅/上涨数/领涨四项，量比（热度三大构成之一）从未展示；
  //   ③ 没有领跌与分化度，"单只暴涨 + 平均涨幅高"会被读成板块普涨
  //      （实测 PCB互联 avg +7.14% 但上涨仅 1/3，正是这种情况）。
  // 现在：按热度降序 + 排名、热度进度条、广度条、量比、领涨/领跌、分化提示。
  function renderSectorHeat(heat) {
    var el = $('sector-heatmap');
    if (!heat || !Object.keys(heat).length) {
      el.innerHTML = '<div class="muted">等待数据...</div>';
      return;
    }
    var rows = Object.keys(heat).map(function (k) {
      var s = heat[k] || {};
      s._key = k;
      return s;
    }).sort(function (a, b) {
      return (Number(b.heat_score) || 0) - (Number(a.heat_score) || 0);
    });

    var html = '';
    rows.forEach(function (s, idx) {
      var score = Number(s.heat_score) || 0;
      var col = heatColor(score);
      var nStocks = Number(s.n_stocks) || 0;
      var nUp = Number(s.n_up) || 0;
      var breadth = nStocks > 0 ? Math.round(nUp / nStocks * 100) : 0;
      var vr = (s.avg_volume_ratio === undefined || s.avg_volume_ratio === null)
        ? null : Number(s.avg_volume_ratio);
      var best = Number(s.best_change_pct) || 0;
      var worst = Number(s.worst_change_pct) || 0;
      var spread = best - worst;
      // 分化度：领涨与领跌差距超过 5 个百分点即视为内部严重分化
      var divergent = spread >= 5.0 && nUp < nStocks;

      html += '<div class="heat-cell' + (idx === 0 ? ' hot-leader' : '') + '"' +
        ' style="border-left-color:' + col + ';' +
        ' background:linear-gradient(90deg,' + col + '1f 0%, #151a23 60%);">' +

        '<div class="hc-head">' +
          '<span class="hc-rank">' + (idx + 1) + '</span>' +
          '<span class="label">' + esc(s.label || s._key) + '</span>' +
          '<span class="hc-lvl" style="color:' + col + ';">' + heatLevel(score) + '</span>' +
        '</div>' +

        '<div class="hc-score">' +
          '<span class="heat" style="color:' + col + ';">' + fmt(score, 1) + '</span>' +
          '<span class="hc-chg" style="color:' + chgColor(s.avg_change_pct) + ';">' +
            chgArrow(s.avg_change_pct) + ' ' + signed(s.avg_change_pct) + '%' +
          '</span>' +
        '</div>' +

        // 热度进度条：把 0~10 的抽象分值变成肉眼可比的宽度
        '<div class="hc-bar"><i style="width:' +
          Math.max(2, Math.min(100, score * 10)) + '%;background:' + col + ';"></i></div>' +

        // 广度（上涨占比）：红=上涨，其余灰。与"涨红跌绿"一致
        '<div class="hc-breadth" title="板块内上涨家数占比">' +
          '<div class="hc-bbar"><i style="width:' + breadth + '%;"></i></div>' +
          '<span class="hc-btxt">' + nUp + '/' + nStocks + ' 上涨</span>' +
        '</div>' +

        '<div class="hc-meta">' +
          '量比 ' + (vr === null ? '--' : fmt(vr, 2)) +
          ' · 强度 ' + fmt((Number(s.strength) || 0) * 100, 0) + '%' +
        '</div>' +

        '<div class="hc-best">领涨 ' + esc(s.best_name || s.best_code || '--') +
          ' <b style="color:' + chgColor(best) + ';">' + signed(best) + '%</b></div>' +
        '<div class="hc-worst">领跌 ' + esc(s.worst_name || s.worst_code || '--') +
          ' <b style="color:' + chgColor(worst) + ';">' + signed(worst) + '%</b></div>' +

        (divergent ? '<div class="hc-warn" title="领涨与领跌差距过大，' +
          '平均涨幅由少数个股拉动">⚠ 内部分化 价差 ' + fmt(spread, 2) + 'pt</div>' : '') +

        '</div>';
    });
    el.innerHTML = html;
  }

  // ----- 动态候选池 -----
  function renderUniverse(u) {
    var el = $('universe-info');
    if (!u || !u.enabled) {
      el.innerHTML = '<div class="muted">动态池未启用</div>';
      return;
    }
    var html = '<div class="item">行业: ' +
      (u.by_industry ? Object.keys(u.by_industry).join(' / ') : '--') + '</div>';
    html += '<div class="item">总候选: <b>' + (u.n_total || 0) + '</b> 只</div>';
    html += '<div class="item">活跃池: <b>' + (u.active_pool_size || 0) +
      '</b> 只</div>';
    if (u.by_industry) {
      Object.keys(u.by_industry).forEach(function (ind) {
        html += '<div class="item">' + esc(ind) + ': <b>' +
          u.by_industry[ind] + '</b></div>';
      });
    }
    html += '<div class="item">刷新: ' + esc(u.last_refresh || '--') + '</div>';
    el.innerHTML = html;
    $('universe-summary').textContent =
      u.n_total + ' 总 / ' + u.active_pool_size + ' 活跃';
  }

  // ----- LLM 重排序 -----
  var _lastLLMRerank = null;
  function renderLLMRerank(r) {
    _lastLLMRerank = r;
    if (!r) {
      $('llm-status').textContent = '未运行';
      $('llm-analysis').textContent = '（无）';
      $('llm-rank-list').innerHTML = '';
      $('llm-macro').innerHTML = '--';
      $('llm-model').textContent = '--';
      return;
    }
    $('llm-status').textContent = shortTs(r.ts) + (r.cached ? ' (缓存)' : '');
    // 【2026-09-22】失败可见化：以前 LLM 调用失败（典型是代理没开）时页面
    // 依然显示上一次的成功结果，用户完全看不出"它其实已经坏了"。
    renderLLMHealth(r.health);
    var macro = r.macro_view || 'neutral';
    var macroCls = macro === 'bullish' ? 'bullish' :
                  macro === 'bearish' ? 'bearish' : 'neutral';
    $('llm-macro').innerHTML =
      '<span class="badge ' + macroCls + '">宏观: ' + esc(macro) + '</span>';
    $('llm-model').textContent = '模型: ' + esc(r.model || '--');
    $('llm-analysis').textContent = r.analysis || '（无分析）';

    var html = '';
    if (r.ranked_codes && r.ranked_codes.length) {
      html += '<div style="color:#8898a6;margin:4px 0">LLM 重排序（Top ' +
        r.ranked_codes.length + '）：</div>';
      r.ranked_codes.forEach(function (code, i) {
        var adj = (r.adjustments || {})[code] || '';
        var cls = '';
        if (adj.indexOf('+') === 0) cls = 'up';
        else if (adj.indexOf('-') === 0) cls = 'down';
        html += '<div class="adj ' + cls + '">' +
          (i + 1) + '. ' + esc(code) + ' ' +
          esc(NAME(code)) + ' · ' + esc(adj) +
          '</div>';
      });
    }
    $('llm-rank-list').innerHTML = html;
  }

  // LLM 健康徽标（可独立调用：即使还没有任何重排结果也要能显示失败原因）
  function renderLLMHealth(h) {
    var herr = $('llm-health');
    if (!herr) return;
    h = h || {};
    if (!h.enabled) {
      herr.textContent = '未启用（无 API Key）';
      herr.className = 'signal-fresh fresh-warn';
    } else if (h.last_error) {
      herr.textContent = '⚠ 最近一次调用失败：' + h.last_error;
      herr.className = 'signal-fresh fresh-stale';
    } else if (!h.has_result) {
      herr.textContent = '等待首次重排';
      herr.className = 'signal-fresh fresh-idle';
    } else {
      herr.textContent = '调用正常' + (h.mode === 'direct' ? '（直连）' : '');
      herr.className = 'signal-fresh fresh-ok';
    }
  }

  function renderRecommendations(recs) {
    var body = $('rec-table').querySelector('tbody');
    body.innerHTML = '';
    if (!recs || !recs.length) {
      $('rec-pool-summary').textContent = '空';
      return;
    }
    $('rec-pool-summary').textContent = recs.length + ' 只';
    var adjMap = (_lastLLMRerank && _lastLLMRerank.adjustments) || {};
    var rankedOrder = (_lastLLMRerank && _lastLLMRerank.ranked_codes) || [];
    // 按 LLM 排序（如果有）展示
    var orderedCodes = rankedOrder.length ? rankedOrder : recs.map(function (r) { return r.code; });
    var recByCode = {};
    recs.forEach(function (r) { recByCode[r.code] = r; });

    var pos = 0;
    orderedCodes.forEach(function (code) {
      var r = recByCode[code];
      if (!r) return;
      pos++;
      var adj = adjMap[code] || '';
      var adjCls = '';
      if (adj.indexOf('+') === 0) adjCls = 'up';
      else if (adj.indexOf('-') === 0) adjCls = 'down';
      body.insertAdjacentHTML('beforeend',
        '<tr>' +
        '<td>' + pos + '</td>' +
        '<td>' + esc(code) + '</td>' +
        '<td>' + esc(r.name) + '</td>' +
        '<td>' + esc(r.sector_label || r.sector) + '</td>' +
        '<td class="num"><b>' + fmt(r.composite, 2) + '</b></td>' +
        '<td class="num">' + fmt(r.heat_contribution, 1) + '</td>' +
        '<td class="num">' + fmt(r.tech_score, 1) + '</td>' +
        '<td class="num">' + fmt(r.fundamental_score, 1) + '</td>' +
        '<td class="num">' + (r.pe == null ? '--' : fmt(r.pe, 1)) + '</td>' +
        '<td class="num">' + (r.roe == null ? '--' : fmt(r.roe, 1)) + '</td>' +
        '<td class="' + adjCls + '" title="' + esc(adj) + '">' +
          esc(adj.slice(0, 20)) + '</td>' +
        '</tr>');
    });
  }

  // ============================================================ SSE

  var sse = null;
  var sseReconnectTimer = null;

  function connectSSE() {
    if (sse) { try { sse.close(); } catch (e) {} sse = null; }
    if (sseReconnectTimer) { clearTimeout(sseReconnectTimer); sseReconnectTimer = null; }
    var cs = $('conn-status');
    if (cs) { cs.textContent = '● 连接中'; cs.className = 'conn-status'; }
    try {
      sse = new EventSource('/api/stream');
    } catch (e) {
      console.error('EventSource 创建失败', e);
      scheduleReconnect();
      return;
    }
    sse.onopen = function () {
      var cs2 = $('conn-status');
      if (cs2) { cs2.textContent = '● 在线'; cs2.className = 'conn-status online'; }
    };
    sse.onerror = function () {
      var cs3 = $('conn-status');
      if (cs3) { cs3.textContent = '● 离线'; cs3.className = 'conn-status offline'; }
      scheduleReconnect();
    };
    sse.onmessage = function (ev) {
      try {
        var data = JSON.parse(ev.data);
        if (data.error) return;
        if (data.snapshot) renderSnapshot(data.snapshot);
        if (data.snapshot) updateMarketFreshness(data.snapshot);
        if (data.ticks) renderTicks(data.ticks);
        if (data.new_signals) appendSignals(data.new_signals);
        if (data.last_signal_ts) updateSignalFreshness(data.last_signal_ts);
        if (data.new_fills) appendFills(data.new_fills);
        if (data.sector_heat) renderSectorHeat(data.sector_heat);
        if (data.dynamic_universe_summary) renderUniverse(data.dynamic_universe_summary);
        if (data.llm_rerank !== undefined) renderLLMRerank(data.llm_rerank);
        if (data.recommendations) renderRecommendations(data.recommendations);
      } catch (e) { console.debug('sse parse err', e); }
    };
  }

  function scheduleReconnect() {
    if (sseReconnectTimer) return;
    sseReconnectTimer = setTimeout(function () {
      sseReconnectTimer = null;
      connectSSE();
    }, 3000);
  }

  // ============================================================ 按钮

  async function postJson(url) {
    try {
      var r = await fetch(url, { method: 'POST' });
      return await r.json();
    } catch (e) { return { ok: false, reason: e.message }; }
  }
  function bind(id, fn) {
    var el = $(id);
    if (el) el.onclick = fn;
  }
  bind('btn-start', async function () {
    var r = await postJson('/api/engine/start');
    alert(r.ok ? 'engine 已启动' : ('启动失败: ' + (r.reason || 'unknown')));
  });
  bind('btn-stop', async function () {
    var r = await postJson('/api/engine/stop');
    alert(r.ok ? 'engine 已请求停止' : '停止失败');
  });
  bind('btn-refresh', async function () {
    try {
      var r = await fetch('/api/snapshot');
      var snap = await r.json();
      renderSnapshot(snap);
      var r2 = await fetch('/api/ticks');
      var ticks = await r2.json();
      renderTicks(ticks);
    } catch (e) { alert('刷新失败: ' + e.message); }
  });
  function rerankAction() {
    postJson('/api/llm/rerank').then(function (r) {
      if (!r.ok) {
        alert('LLM rerank 失败: ' + (r.reason || ''));
      } else {
        $('llm-status').textContent = '执行中...';
        setTimeout(refreshLLM, 5000);
      }
    });
  }
  bind('btn-rerank', rerankAction);
  bind('btn-rerank-small', rerankAction);
  function refreshLLM() {
    fetch('/api/llm/rerank/latest').then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.result) renderLLMRerank(d.result);
        if (d.health) renderLLMHealth(d.health);
      }).catch(function () {});
  }

  // ============================================================ 初始加载
  fetch('/api/snapshot').then(function (r) { return r.json(); })
    .then(renderSnapshot).catch(function () {});
  fetch('/api/ticks').then(function (r) { return r.json(); })
    .then(renderTicks).catch(function () {});
  fetch('/api/sector/score').then(function (r) { return r.json(); })
    .then(function (d) {
      if (d.sector_scores) renderSectorHeat(d.sector_scores);
    }).catch(function () {});
  fetch('/api/universe/dynamic').then(function (r) { return r.json(); })
    .then(renderUniverse).catch(function () {});
  fetch('/api/llm/rerank/latest').then(function (r) { return r.json(); })
    .then(function (d) {
      if (d.result) renderLLMRerank(d.result);
    }).catch(function () {});

  // ============================================================ 启动 SSE
  connectSSE();
})();
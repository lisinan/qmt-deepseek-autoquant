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
    try {
      const d = new Date(ts);
      return d.toLocaleTimeString('zh-CN', { hour12: false });
    } catch (e) { return ts; }
  }
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function heatColor(score) {
    // 0~10 → 灰/红/绿 渐变
    if (score == null) return '#2a3441';
    const s = Math.max(0, Math.min(10, score));
    // 热度越高越亮绿
    const r = Math.round(40 + (95 - 40) * (s / 10));
    const g = Math.round(60 + (217 - 60) * (s / 10));
    const b = Math.round(70 + (158 - 70) * (s / 10));
    return 'rgb(' + r + ',' + g + ',' + b + ')';
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
    return '<tr>' +
      '<td>' + esc(code) + ' ' + esc(p.name || NAME(code)) + '</td>' +
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
    // 日内盈亏：优先显示真实日内总盈亏（已实现 + 当日浮动），无基线时回退已实现 daily_pnl
    var ip = (r.intraday_pnl != null) ? r.intraday_pnl : (r.daily_pnl || 0);
    var ipc = (r.intraday_pnl_pct != null) ? r.intraday_pnl_pct : null;
    var ipStr = fmt(ip, 0) + (ipc != null ? ' (' + (ipc >= 0 ? '+' : '') + fmt(ipc, 2) + '%)' : '');
    $('risk-pnl').textContent = ipStr;
    $('risk-pnl').className = ip >= 0 ? 'ok' : 'halted';
    $('risk-consec').textContent = r.consecutive_losses;
    $('risk-scale').textContent = fmt(r.position_scale, 2);
    $('risk-trades').textContent = r.daily_trade_count;
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
    sigs.slice().reverse().forEach(function (s) {
      body.insertAdjacentHTML('afterbegin', signalRow(s));
    });
    while (body.children.length > 30) body.removeChild(body.lastChild);
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
  function renderSectorHeat(heat) {
    var el = $('sector-heatmap');
    if (!heat || !Object.keys(heat).length) {
      el.innerHTML = '<div class="muted">等待数据...</div>';
      return;
    }
    var html = '';
    Object.keys(heat).forEach(function (sector) {
      var s = heat[sector];
      var score = s.heat_score || 0;
      var strength = s.strength || 0;
      html += '<div class="heat-cell" style="border-left-color:' +
        heatColor(score) + ';">' +
        '<div class="label">' + esc(s.label || sector) + '</div>' +
        '<div class="heat" style="color:' + heatColor(score) + ';">' +
        fmt(score, 1) + '</div>' +
        '<div class="meta">' +
        '涨幅 ' + fmt(s.avg_change_pct, 2) + '% · ' +
        '上涨 ' + s.n_up + '/' + s.n_stocks +
        '</div>' +
        '<div class="best">领涨: ' + esc(s.best_name || s.best_code) +
        ' (' + fmt(s.best_change_pct, 2) + '%)</div>' +
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
        if (data.ticks) renderTicks(data.ticks);
        if (data.new_signals) appendSignals(data.new_signals);
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
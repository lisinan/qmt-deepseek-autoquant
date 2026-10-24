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

    // C-B5-5：相对基准超额（snapshot.benchmark.excess_pct，正绿负红，缺失显示 —）
    renderBenchmark(snap.benchmark);
  }

  function renderBenchmark(bm) {
    var el = $('dec-excess');
    var box = $('dec-excess-box');
    if (!el || !box) return;
    bm = bm || {};
    var lines = [
      'account: ' + fmtPctOrDash(bm.account_pct),
      'universe_bh: ' + fmtPctOrDash(bm.universe_bh_pct),
      'index: ' + fmtPctOrDash(bm.index_pct)
    ];
    box.title = lines.join('\n');
    var e = bm.excess_pct;
    if (e === undefined || e === null || isNaN(Number(e))) {
      el.textContent = '—';
      box.className = 'dec-item';
      return;
    }
    e = Number(e);
    el.textContent = (e > 0 ? '+' : '') + fmt(e * 100, 2) + '%';
    box.className = 'dec-item ' + (e > 0 ? 'dec-good' : (e < 0 ? 'dec-neg' : ''));
  }
  function fmtPctOrDash(v) {
    if (v === undefined || v === null || isNaN(Number(v))) return '—';
    return (Number(v) > 0 ? '+' : '') + fmt(Number(v) * 100, 2) + '%';
  }

  function renderTicks(ticks) {
    var body = $('tick-table').querySelector('tbody');
    body.innerHTML = '';
    Object.keys(ticks || {}).forEach(function (code) {
      body.insertAdjacentHTML('beforeend', tickRow(code, ticks[code]));
    });
  }

  // 已渲染的信号/成交 id 集合，用于初始 fetch 与 SSE 增量推送之间的去重
  var _seenSignalIds = {};
  var _seenFillIds = {};

  function appendSignals(sigs) {
    if (!sigs || !sigs.length) return;
    var body = $('signal-table').querySelector('tbody');
    // 注：徽标刷新由 onmessage 统一调用 updateSignalFreshness(data) 完成
    // （data.last_signal_ts 已含本次新增信号），此处不再单独调用，避免签名不一致。
    sigs.slice().reverse().forEach(function (s) {
      var sid = (s.id !== undefined && s.id !== null) ? String(s.id) : null;
      if (sid && _seenSignalIds[sid]) return;
      if (sid) _seenSignalIds[sid] = 1;
      body.insertAdjacentHTML('afterbegin', signalRow(s));
    });
    while (body.children.length > 30) body.removeChild(body.lastChild);
  }
  // 【2026-09-21 原始】信号源新鲜度徽标：原本仅在 last_signal_ts 陈旧时统一显示
  // 「信号源停摆」，但这是误报——引擎在线、市场弱势导致「连日零买入信号」也属正常，
  // 不应被贴上「停摆」标签。
  // 【2026-10-14 修正】把「引擎是否真的离线」与「是否有新信号」拆开判断：
  //   · SSE 超过 6s 无帧  → 引擎确实离线，才是真「信号源停摆」；
  //   · SSE 在线但 last_signal_ts 陈旧 → 引擎在线，只是市场暂无满足阈值的标的，
  //     标为「引擎在线 · 连续 N 天无满足阈值的买入信号（市场弱势，非故障）」，不再误报停摆。
  function updateSignalFreshness(data) {
    var el = $('signal-fresh');
    if (!el) return;
    // 尚未收到过任何 SSE 帧（页面刚加载）：保持等待态，避免误报离线
    if (_lastSseAt === 0) {
      el.textContent = '等待信号数据...';
      el.className = 'signal-fresh fresh-idle';
      return;
    }
    // 引擎在线判定：最近 6 秒内收到过 SSE 帧（后端每 ~1s 推送一次）
    var online = (Date.now() - _lastSseAt) < 6000;
    if (!online) {
      el.textContent = '⚠ 信号源停摆（引擎离线 / SSE 断开）';
      el.className = 'signal-fresh fresh-stale';
      return;
    }
    var ts = data && data.last_signal_ts;
    if (!ts) {
      el.textContent = '引擎在线 · 暂无历史信号';
      el.className = 'signal-fresh fresh-idle';
      return;
    }
    var d = new Date(ts);
    if (isNaN(d.getTime())) {
      el.textContent = '引擎在线 · 信号时间无效';
      el.className = 'signal-fresh fresh-warn';
      return;
    }
    var mins = Math.floor((Date.now() - d.getTime()) / 60000);
    var txt, cls = 'fresh-ok';
    if (mins < 30) {
      txt = '最新信号 ' + mins + ' 分钟前';
    } else if (mins < 60 * 24) {
      txt = '⚠ 已 ' + mins + ' 分钟无新信号（引擎在线，市场暂无满足阈值的标的）';
      cls = 'fresh-warn';
    } else {
      var days = Math.floor(mins / 1440);
      txt = '引擎在线 · 连续 ' + days + ' 天无满足阈值的买入信号（市场弱势，非故障）';
      cls = 'fresh-idle';
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
      var fid = (f.id !== undefined && f.id !== null) ? String(f.id) : null;
      if (fid && _seenFillIds[fid]) return;
      if (fid) _seenFillIds[fid] = 1;
      body.insertAdjacentHTML('afterbegin', fillRow(f));
    });
    while (body.children.length > 30) body.removeChild(body.lastChild);
  }

  // ----- 产业链热力图 -----
  // 热力图渲染节流与排序迟滞（见 renderSectorHeat 注释）
  var HEAT_REFRESH_MS = 3000;   // 与 3s 行情节奏对齐
  var HEAT_SORT_TOL = 0.15;     // 热度差小于此值视为并列，不重排
  var _lastHeatSig = null, _lastHeatHtml = null, _lastHeatTs = 0;
  var _lastHeatOrder = [], _lastHeatValues = null;

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

    // 【2026-09-22】渲染节奏控制。
    // SSE 每 1s 推一帧，而热力图数值只需跟住 3s 行情节奏：
    //   ① 节流到 3s，避免每秒整块重建 DOM（hover 会被打断、CSS 过渡失效）；
    //   ② 数值没变就不重建，纯省开销。
    var sig = Object.keys(heat).map(function (k) {
      var s = heat[k] || {};
      return k + ':' + (s.heat_score || 0) + ':' + (s.avg_change_pct || 0) +
        ':' + (s.n_up || 0) + ':' + (s.best_change_pct || 0) +
        ':' + (s.worst_change_pct || 0);
    }).sort().join('|');
    var now = Date.now();
    if (sig === _lastHeatSig && _lastHeatHtml) return;          // 无变化
    if (_lastHeatTs && now - _lastHeatTs < HEAT_REFRESH_MS) return;  // 节流
    _lastHeatTs = now;

    var prev = _lastHeatValues || {};
    var prevOrder = _lastHeatOrder || [];
    var rows = Object.keys(heat).map(function (k) {
      var s = heat[k] || {};
      s._key = k;
      return s;
    }).sort(function (a, b) {
      var d = (Number(b.heat_score) || 0) - (Number(a.heat_score) || 0);
      // 排序迟滞：热度差 <0.15 视为并列，沿用上一帧顺序。
      // 否则两个环节热度交替领先时卡片会来回跳，根本看不清数值。
      if (Math.abs(d) < HEAT_SORT_TOL) {
        var ia = prevOrder.indexOf(a._key), ib = prevOrder.indexOf(b._key);
        if (ia >= 0 && ib >= 0) return ia - ib;
      }
      return d;
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
      // 数值有变化就闪一下：3s 刷新后数字是静默变化的，不提示会以为没更新
      var pv = prev[s._key];
      var flash = (pv !== undefined && pv !== score) ? ' hc-flash' : '';

      html += '<div class="heat-cell' + (idx === 0 ? ' hot-leader' : '') +
        flash + '"' +
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

    // 记住本帧状态供下一帧做「变化检测 + 排序迟滞」
    _lastHeatSig = sig;
    _lastHeatHtml = html;
    _lastHeatOrder = rows.map(function (r) { return r._key; });
    _lastHeatValues = {};
    rows.forEach(function (r) { _lastHeatValues[r._key] = Number(r.heat_score) || 0; });
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
    // C-B5-5：状态行展示 calls / cached_hits / last_error（字段可能暂不存在，容错 —）
    var stats = $('llm-stats');
    if (stats) {
      stats.textContent = 'calls ' +
        (h.calls != null ? h.calls : '—') +
        ' / 缓存命中 ' + (h.cached_hits != null ? h.cached_hits : '—') +
        ' / 最近错误 ' + (h.last_error ? String(h.last_error) : '—');
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

  // ----- 今日决策条（C8：gate_funnel / no_entry_reason / ai_veto_n / degraded） -----
  function renderDecision(snap) {
    if (!snap) return;
    var deg = snap.degraded;
    var degEl = $('dec-degraded');
    var degBox = $('dec-degraded-box');
    if (deg && deg.length) {
      degEl.textContent = '⚠ ' + deg.join('；');
      degBox.className = 'dec-item dec-bad';
    } else if (deg) {
      degEl.textContent = '正常';
      degBox.className = 'dec-item dec-ok';
    } else {
      degEl.textContent = '—';
      degBox.className = 'dec-item';
    }
    var ne = snap.no_entry_reason;
    $('dec-no-entry').textContent = (ne === undefined || ne === null || ne === '') ? '—' : String(ne);
    var gf = snap.gate_funnel;
    if (gf && typeof gf === 'object') {
      $('dec-funnel').textContent =
        (gf.universe_n || 0) + ' / ' + (gf.momentum_pass_n || 0) + ' / ' +
        (gf.trend_up_n || 0) + ' / ' + (gf.bias_ok_n || 0) + ' / ' + (gf.score_ok_n || 0);
    } else {
      $('dec-funnel').textContent = '—';
    }
    var av = snap.ai_veto_n;
    $('dec-ai-veto').textContent = (av === undefined || av === null) ? '—' : String(av);
  }

  // ----- 告警条：最近 5 条 notices（ERROR 红 / WARNING 黄），未读计数 + 展开收起 -----
  var _noticeSeen = 0;      // 已读条数（展开时清零到最新）
  var _noticeLatest = [];   // 最近一次收到的 notices
  var _noticeExpanded = false;

  function renderNotices(notices) {
    if (!notices) return;
    _noticeLatest = notices;
    var list = notices.slice(0, 5);
    var latestEl = $('notice-latest');
    var dot = $('notice-dot');
    if (!list.length) {
      latestEl.textContent = '暂无告警';
      dot.className = 'notice-dot';
    } else {
      var n0 = list[0];
      latestEl.textContent = '[' + (n0.level || 'INFO') + '] ' +
        (n0.source || '') + '：' + (n0.msg || n0.message || '');
      dot.className = 'notice-dot ' + (n0.level === 'ERROR' ? 'dot-err' :
        (n0.level === 'WARNING' ? 'dot-warn' : ''));
    }
    // 未读计数：折叠时显示 新到达条数
    var unread = Math.max(0, notices.length - _noticeSeen);
    var badge = $('notice-badge');
    badge.textContent = unread;
    badge.style.display = unread > 0 ? 'inline-block' : 'none';
    // 展开列表
    var html = '';
    list.forEach(function (n) {
      var lv = n.level || 'INFO';
      var cls = lv === 'ERROR' ? 'n-err' : (lv === 'WARNING' ? 'n-warn' : 'n-info');
      html += '<div class="notice-item ' + cls + '">' +
        '<span class="n-ts">' + esc(shortTs(n.ts)) + '</span>' +
        '<span class="n-lv">' + esc(lv) + '</span>' +
        '<span class="n-src">' + esc(n.source || '') + '</span>' +
        '<span class="n-msg">' + esc(n.msg || n.message || '') + '</span>' +
        '</div>';
    });
    $('notice-list').innerHTML = html || '<div class="muted">暂无告警</div>';
  }

  function bindNoticeBar() {
    var head = $('notice-head');
    if (!head) return;
    head.onclick = function () {
      _noticeExpanded = !_noticeExpanded;
      var bar = $('notice-bar');
      bar.className = 'notice-bar' + (_noticeExpanded ? '' : ' collapsed');
      $('notice-toggle').textContent = _noticeExpanded ? '收起 ▼' : '展开 ▲';
      if (_noticeExpanded) {
        _noticeSeen = _noticeLatest.length;
        var badge = $('notice-badge');
        badge.textContent = '0';
        badge.style.display = 'none';
      }
    };
  }

  // ============================================================ SSE

  var sse = null;
  var sseReconnectTimer = null;
  var _lastSseAt = 0;        // 最近一次收到 SSE 帧的时间（后端每 ~1s 推送一帧）
  var _lastSignalData = null; // 缓存最近一帧信号相关数据，供离线自检重渲染

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
        _lastSseAt = Date.now();
        _lastSignalData = data;
        if (data.snapshot) renderSnapshot(data.snapshot);
        if (data.snapshot) renderDecision(data.snapshot);
        if (data.snapshot) updateMarketFreshness(data.snapshot);
        if (data.notices) renderNotices(data.notices);
        if (data.ticks) renderTicks(data.ticks);
        if (data.new_signals) appendSignals(data.new_signals);
        updateSignalFreshness(data);
        if (data.new_fills) appendFills(data.new_fills);
        if (data.sector_heat) renderSectorHeat(data.sector_heat);
        if (data.dynamic_universe_summary) renderUniverse(data.dynamic_universe_summary);
        if (data.llm_rerank !== undefined) renderLLMRerank(data.llm_rerank);
        if (data.recommendations) renderRecommendations(data.recommendations);
      } catch (e) { console.debug('sse parse err', e); }
    };
  }

  // 离线自检：SSE 超过 6s 无新帧时，用缓存的最后一帧重渲染徽标为「引擎离线/信号源停摆」。
  // 这样即使引擎进程崩溃、Flask 端口断开，页面也能在数秒内反映真实停摆，而非一直显示陈旧绿标。
  setInterval(function () {
    if (_lastSseAt > 0 && (Date.now() - _lastSseAt) >= 6000 && _lastSignalData) {
      updateSignalFreshness(_lastSignalData);
    }
  }, 4000);

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

  // ============================================================ 入场闸门逐只明细（学习面板）
  // 为什么有这块：决策条只告诉你「trend_up 0 只」，但学量化最需要的是
  // 「这只票具体差在哪一条」。表格按通过条件数从多到少排序，最接近买入的在最上面。
  function gateMark(ok) {
    return '<td class="g-' + (ok ? 'yes' : 'no') + '">' + (ok ? '✔' : '✘') + '</td>';
  }

  function renderGate(d) {
    var sum = (d && d.summary) || {};
    var rows = (d && d.rows) || [];
    var sumEl = $('gate-summary');
    if (sumEl) {
      if (!sum.ready) {
        sumEl.textContent = '（日线上下文未就绪，引擎预热中）';
      } else {
        var p = sum.patterns || sum.params || {};
        sumEl.textContent =
          '宇宙 ' + sum.universe_n + ' 只 ｜ 收盘>MA20 ' + sum.above_ma20_n +
          ' ｜ MA20>MA60 ' + sum.ma20_over_ma60_n + ' ｜ 斜率>0 ' + sum.slope_up_n +
          ' ｜ MACD≥0 ' + sum.macd_ok_n + ' ｜ 趋势成立 ' + sum.trend_up_n +
          ' ｜ 动量>' + (p.min_mom != null ? (p.min_mom * 100).toFixed(0) + '%' : '?') +
          ' ' + sum.mom_pass_n + ' ｜ 评分达标 ' + sum.score_ok_n +
          (sum.breadth60 != null ? ' ｜ 宽度(>MA60) ' + (sum.breadth60 * 100).toFixed(0) + '%' : '');
      }
    }
    var body = $('gate-table') && $('gate-table').querySelector('tbody');
    if (!body) return;
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="11" class="muted">暂无数据</td></tr>';
      return;
    }
    var html = '';
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      var cls = r.trend_up ? 'g-row-all' : (r.passed_n >= 5 ? 'g-row-near' : '');
      var mom = r.mom60 == null ? '—' :
        ((r.mom60 > 0 ? '+' : '') + (r.mom60 * 100).toFixed(1) + '%');
      var chg = (r.day_change_pct || 0) * 100;
      html += '<tr class="' + cls + '">' +
        '<td class="code">' + esc(r.code) + '</td>' +
        '<td>' + esc(r.name || NAME(r.code)) + '</td>' +
        '<td class="ta-r">' + fmt(r.close, 2) +
          '<span class="' + (chg > 0 ? 'up' : (chg < 0 ? 'down' : 'flat')) +
          '" style="font-size:10px"> ' + (chg > 0 ? '+' : '') + chg.toFixed(2) + '%</span></td>' +
        '<td class="ta-r">' + fmt(r.ma20, 2) + '</td>' +
        '<td class="ta-r">' + fmt(r.ma60, 2) + '</td>' +
        '<td class="ta-r">' + fmt(r.ma20_slope, 3) + '</td>' +
        '<td class="ta-r">' + fmt(r.macd_hist, 3) + '</td>' +
        '<td class="ta-r ' + (r.mom_ok ? 'up' : '') + '">' + mom + '</td>' +
        '<td class="ta-r">' + fmt(r.score, 2) + '</td>' +
        '<td class="ta-c g-flags">' +
          (r.above_ma20 ? '✔' : '✘') + (r.above_ma60 ? '✔' : '✘') +
          (r.ma20_over_ma60 ? '✔' : '✘') + (r.slope_up ? '✔' : '✘') +
          (r.macd_ok ? '✔' : '✘') + (r.mom_ok ? '✔' : '✘') +
          (r.score_ok ? '✔' : '✘') + '</td>' +
        '<td class="g-block">' + esc(r.blocked_by || (r.trend_up ? '—（已通过）' : '')) + '</td>' +
        '</tr>';
    }
    body.innerHTML = html;
  }

  function loadGate() {
    return fetch('/api/gate_detail').then(function (r) { return r.json(); })
      .then(renderGate).catch(function () {});
  }

  // ============================================================ 闸门卡点时间线
  // 条件 × 日期 热力矩阵（纯 div 网格）。颜色按当日通过率 pass/universe_n
  // 在 HSL(0,70%,32%) → HSL(48,80%,42%) → HSL(140,55%,38%) 之间线性插值。
  var TL_KEYS = ['above_ma20', 'above_ma60', 'ma20_over_ma60', 'slope_up',
    'macd_ok', 'mom_ok', 'score_ok'];
  var TL_LABELS = {
    above_ma20: '收盘>MA20', above_ma60: '收盘>MA60',
    ma20_over_ma60: 'MA20>MA60', slope_up: 'MA20斜率>0',
    macd_ok: 'MACD≥0', mom_ok: '60日动量>15%', score_ok: '评分达标'
  };
  var tlCurDays = 90;

  function tlColor(rate) {
    // 分两段线性插值：0→50% 红到黄，50→100% 黄到绿
    var h, s, l;
    if (rate <= 0.5) {
      var t = rate / 0.5;
      h = 0 + (48 - 0) * t; s = 70 + (80 - 70) * t; l = 32 + (42 - 32) * t;
    } else {
      var t2 = (rate - 0.5) / 0.5;
      h = 48 + (140 - 48) * t2; s = 80 + (55 - 80) * t2; l = 42 + (38 - 42) * t2;
    }
    return 'hsl(' + Math.round(h) + ',' + Math.round(s) + '%,' + Math.round(l) + '%)';
  }

  // 「最强候选卡点」标记行：blocked_key → 颜色 + 中文说明
  var TL_BEST_COLORS = {
    '': '#2e9e63',             // 可买入
    mom_ok: '#e8853a',         // 动量不足
    above_ma60: '#4a90d9',     // 收盘<MA60
    ma20_over_ma60: '#9b6bd6', // 均线未多头
    slope_up: '#d95f9e',       // 斜率≤0
    macd_ok: '#3fb6b0',        // MACD<0
    score_ok: '#d9b53a'        // 评分不足
  };
  var TL_BEST_LABELS = {
    '': '可买入',
    mom_ok: '动量不足',
    above_ma60: '收盘<MA60',
    ma20_over_ma60: '均线未多头',
    slope_up: '斜率≤0',
    macd_ok: 'MACD<0',
    score_ok: '评分不足'
  };
  var TL_BEST_ORDER = ['', 'mom_ok', 'above_ma60', 'ma20_over_ma60',
    'slope_up', 'macd_ok', 'score_ok'];

  function renderGateTimeline(data) {
    var box = $('tl-matrix');
    var scale = $('tl-scale');
    if (!box) return;
    var rows = (data && data.rows) || [];
    if (!rows.length) {
      box.innerHTML = '<div class="muted tl-empty">暂无时间线数据（等待每日回填/实时追加）</div>';
      if (scale) scale.innerHTML = '';
      return;
    }
    var html = '';
    // 行标签列 + 7 个条件行
    for (var c = 0; c < TL_KEYS.length; c++) {
      var key = TL_KEYS[c];
      html += '<div class="tl-row"><div class="tl-rowlabel">' + esc(TL_LABELS[key]) + '</div>';
      for (var i = 0; i < rows.length; i++) {
        var r = rows[i];
        var n = r.universe_n || 0;
        var p = (r.pass && r.pass[key]) || 0;
        var rate = n > 0 ? p / n : 0;
        var pct = n > 0 ? Math.round(rate * 100) : 0;
        html += '<div class="tl-cell" style="background:' + tlColor(rate) + '"' +
          ' title="' + esc(r.date) + ' ' + esc(TL_LABELS[key]) +
          ' 通过 ' + p + '/' + n + ' (' + pct + '%)"></div>';
      }
      html += '</div>';
    }
    // 「最强候选卡点」标记行：每天一个色块，颜色 = 当日最强候选被哪条闸门挡住
    html += '<div class="tl-row tl-best-row" id="tl-best-row">' +
      '<div class="tl-rowlabel">最强候选卡点</div>';
    for (var j = 0; j < rows.length; j++) {
      var rb = rows[j];
      var best = rb.best;
      var bkey = (best && best.blocked_key != null) ? String(best.blocked_key) : null;
      var allN = (rb.all_pass_n != null) ? rb.all_pass_n : null;
      // 「全通过」还要区分：组合闸门要求 ≥2 只同时够格（min_passing_candidates）。
      // 只有 1 只够格时照样不买 —— 用浅色 + 描边表示，避免把「绿」误读成「买了」。
      var single = (bkey === '' && allN !== null && allN < 2);
      var col;
      if (bkey === null) {
        col = '#3a4451';
      } else if (single) {
        col = '#2e7d5b';
      } else {
        col = (TL_BEST_COLORS[bkey] !== undefined) ? TL_BEST_COLORS[bkey] : '#3a4451';
      }
      var blabel = (bkey !== null && TL_BEST_LABELS[bkey] !== undefined)
        ? TL_BEST_LABELS[bkey] : '数据缺失';
      var title = esc(rb.date);
      if (best && best.code) {
        title += ' 最强候选 ' + esc(best.code) + ' 通过 ' + (best.passed_n || 0) +
          '/7，卡在：' + blabel;
        if (allN !== null) {
          title += '；当日够格（7 项全过）' + allN + ' 只' +
            (allN < 2 ? '，不足 2 只 ⇒ 组合闸门不放行' : '');
        }
      } else {
        title += ' 无最强候选数据';
      }
      html += '<div class="tl-cell tl-best-cell' + (single ? ' tl-best-single' : '') +
        '" style="background:' + col + '" title="' + title + '"></div>';
    }
    html += '</div>';
    box.innerHTML = html;
    // 底部日期刻度：每 10 列标一个 MM-DD
    if (scale) {
      var sHtml = '<span class="tl-rowlabel"></span>';
      for (var k = 0; k < rows.length; k++) {
        if (k % 10 === 0 && rows[k].date && rows[k].date.length >= 10) {
          sHtml += '<span class="tl-tick">' + esc(rows[k].date.slice(5, 10)) + '</span>';
        } else {
          sHtml += '<span class="tl-tick tl-tick-empty"></span>';
        }
      }
      scale.innerHTML = sHtml;
    }
    renderBestLegend();
  }

  function renderBestLegend() {
    var el = $('tl-best-legend');
    if (!el) return;
    var html = '';
    for (var i = 0; i < TL_BEST_ORDER.length; i++) {
      var k = TL_BEST_ORDER[i];
      html += '<span class="lg-item tl-legend-item">' +
        '<i class="tl-swatch" style="background:' + TL_BEST_COLORS[k] + '"></i>' +
        esc(TL_BEST_LABELS[k]) + '</span>';
    }
    // 灰色 = 旧行 / 数据缺失
    html += '<span class="lg-item tl-legend-item">' +
      '<i class="tl-swatch" style="background:#3a4451"></i>无数据</span>';
    el.innerHTML = html;
  }

  function loadGateTimeline(days) {
    tlCurDays = days || tlCurDays;
    return fetch('/api/gate_timeline?days=' + encodeURIComponent(tlCurDays))
      .then(function (r) { return r.json(); })
      .then(renderGateTimeline).catch(function () {
        renderGateTimeline({ rows: [] });
      });
  }

  function bindGateTimelineButtons() {
    var btns = document.querySelectorAll('#timeline-panel .tl-btn');
    for (var i = 0; i < btns.length; i++) {
      (function (btn) {
        btn.addEventListener('click', function () {
          for (var j = 0; j < btns.length; j++) {
            btns[j].classList.remove('tl-btn-active');
          }
          btn.classList.add('tl-btn-active');
          loadGateTimeline(parseInt(btn.getAttribute('data-days'), 10) || 90);
        });
      })(btns[i]);
    }
  }

  // ============================================================ 初始加载
  bindNoticeBar();
  bindGateTimelineButtons();
  loadGate();
  loadGateTimeline(90);
  if (document.getElementById('btn-gate-refresh')) {
    document.getElementById('btn-gate-refresh').addEventListener('click', loadGate);
  }
  fetch('/api/snapshot').then(function (r) { return r.json(); })
    .then(function (snap) { renderSnapshot(snap); renderDecision(snap); })
    .catch(function () {});
  // 刷新后信号/成交表格不空：先拉历史（appendSignals/appendFills 按 id 去重，
  // SSE 随后推送的增量不会重复渲染）
  fetch('/api/signals?limit=30').then(function (r) { return r.json(); })
    .then(function (sigs) { appendSignals(sigs); }).catch(function () {});
  fetch('/api/fills?limit=30').then(function (r) { return r.json(); })
    .then(function (fills) { appendFills(fills); }).catch(function () {});
  fetch('/api/notices?limit=5').then(function (r) { return r.json(); })
    .then(function (ns) {
      if (ns && ns.length) { _noticeSeen = ns.length; renderNotices(ns); }
    }).catch(function () {});
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
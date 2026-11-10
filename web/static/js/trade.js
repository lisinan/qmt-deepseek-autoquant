/* 实盘买卖面板（2026-11-10）
 *
 * 三条纪律：
 *  1. 下单必须**先取一次性令牌**，且提交前弹二次确认（含预估金额与当前价）——
 *     这个接口动的是真实资金，「点错方向」是真实风险。
 *  2. 前端校验只是提示，**权威校验在服务端**（并且服务端复用引擎的 decide()）。
 *  3. 任何失败都把服务端的 reason 原样显示，不要自己编原因——
 *     「面板说 A、引擎拒 B」是最难查的一类问题。
 */
(function () {
  "use strict";

  var ST = null;          // 最近一次 /api/trade/status
  var token = null;

  function $(id) { return document.getElementById(id); }

  function money(v) {
    if (v === null || v === undefined || v === "") return "—";
    return Number(v).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function postJSON(url, body, needToken) {
    var headers = { "Content-Type": "application/json" };
    if (needToken) headers["X-Trade-Token"] = token || "";
    return fetch(url, {
      method: "POST",
      headers: headers,
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().then(function (j) { return { status: r.status, body: j }; });
    });
  }

  function acquireToken() {
    return postJSON("/api/trade/token", {}, false).then(function (res) {
      if (res.body && res.body.ok) { token = res.body.token; return true; }
      token = null;
      return false;
    });
  }

  function setMsg(text, kind) {
    var el = $("form-msg");
    el.textContent = text || "";
    el.className = "form-msg" + (kind ? " " + kind : "");
  }

  function priceOf(code) {
    if (!ST) return 0;
    var arr = ST.positions || [];
    for (var i = 0; i < arr.length; i++) if (arr[i].code === code) return arr[i].last_price || 0;
    return 0;
  }

  function renderStatus() {
    if (!ST) return;
    $("zone-badge").textContent = "zone: " + ST.zone;
    $("zone-badge").className = "badge " + (ST.zone === "live" ? "live" : "neutral");

    var ch = ST.channel || {};
    $("channel-badge").textContent = "channel: " + (ch.enabled ? "启用" : "关闭");
    $("channel-badge").className = "badge " + (ch.enabled ? "passed" : "blocked");

    var rk = ST.risk || {};
    $("risk-badge").textContent = "risk: " + (rk.halted ? "熔断" : "正常");
    $("risk-badge").className = "badge " + (rk.halted ? "blocked" : "passed");

    var eng = ST.engine || {};
    // 【2026-11-10】只有券商权威资产到位才显示数字。live 引擎刚启动/断链期，
    // 内存里是构造初值（cash=1,000,000、无持仓）——显示它等于让人对着假数字下单。
    var trusted = !!eng.asset_trusted;
    var NA = "未同步券商";
    $("kv-total").textContent = trusted ? money(eng.total_asset) : NA;
    $("kv-cash").textContent = trusted ? money(eng.cash) : NA;
    $("kv-used").textContent = (ch.used_today || 0) + " / " + (ch.max_per_day || "—");
    $("kv-cap").textContent = money(ch.max_amount);

    var warn = $("risk-warn");
    var msgs = [];
    if (rk.halted) msgs.push("账户处于熔断：" + (rk.halt_reason || "—") + "（买入会被拒，卖出仍按各自规则判定）");
    if (eng.data_mode === "mock") msgs.push("行情源=mock（合成行情）：整条指令通道不动作，单子会一直 pending");
    if (!ch.enabled) msgs.push("OWNER 指令通道未启用：任何提交都会被拒");
    if (!trusted) msgs.push("账户资产尚未从券商同步（引擎刚启动或断链期）：现金与持仓显示为「未同步券商」，请勿据此下单");
    if (msgs.length) { warn.style.display = ""; warn.textContent = msgs.join(" ｜ "); }
    else { warn.style.display = "none"; }

    // 持仓表
    var tb = $("pos-tbl").querySelector("tbody");
    tb.innerHTML = "";
    (ST.positions || []).forEach(function (p) {
      var tr = document.createElement("tr");
      tr.innerHTML =
        "<td class='mono'>" + p.code + "</td>" +
        "<td class='mono'>" + p.quantity + "</td>" +
        "<td class='mono'>" + (p.available || 0) + "</td>" +
        "<td class='mono'>" + money(p.avg_cost) + "</td>" +
        "<td class='mono'>" + money(p.last_price) + "</td>" +
        "<td></td>";
      var btn = document.createElement("button");
      btn.textContent = "卖出";
      btn.className = "mini btn-sell";
      btn.onclick = function () {
        document.querySelector("#order-form input[name=side][value=SELL]").checked = true;
        $("f-code").value = p.code;
        $("f-lots").value = Math.min(100, p.quantity);
        updateEstimate();
      };
      tr.lastChild.appendChild(btn);
      tb.appendChild(tr);
    });

    // 队列表
    var qb = $("queue-tbl").querySelector("tbody");
    qb.innerHTML = "";
    (ST.queue || []).forEach(function (q) {
      var tr = document.createElement("tr");
      tr.innerHTML =
        "<td class='mono'>" + q.id + "</td>" +
        "<td class='mono'>" + q.code + "</td>" +
        "<td>" + q.side + "</td>" +
        "<td class='mono'>" + (q.lots === null || q.lots === undefined ? "—" : q.lots) + "</td>" +
        "<td>" + q.status + "</td>" +
        "<td class='hint'>" + (q.reason || "") + "</td>" +
        "<td></td>";
      if (q.status === "pending") {
        var cb = document.createElement("button");
        cb.textContent = "取消";
        cb.className = "danger mini";
        cb.onclick = function () { cancelOrder(q.id); };
        tr.lastChild.appendChild(cb);
      }
      qb.appendChild(tr);
    });
  }

  function updateEstimate() {
    var code = ($("f-code").value || "").trim().toUpperCase();
    var lots = parseInt($("f-lots").value, 10) || 0;
    var px = code ? priceOf(code) : 0;
    var est = $("est-amount");
    if (!px || !lots) { est.textContent = "—（无行情价时无法预估）"; return; }
    est.textContent = money(px * lots) + " 元  （现价 " + money(px) + " × " + lots + " 股）";
    var hint = $("hint-lots");
    if (ST && ST.channel && ST.channel.max_amount > 0) {
      hint.textContent = (px * lots > ST.channel.max_amount)
        ? "⚠ 超过单笔上限 " + money(ST.channel.max_amount) + " 元，引擎会拒绝"
        : "";
    }
  }

  function load() {
    return fetch("/api/trade/status").then(function (r) {
      if (r.status === 403) throw new Error("交易面板已关闭（QMT_TRADE_UI=0 或 paper 区未开启）");
      return r.json();
    }).then(function (j) {
      ST = j;
      $("trade-conn-status").textContent = "● 在线";
      $("trade-conn-status").className = "conn-status ok";
      renderStatus();
      updateEstimate();
    }).catch(function (e) {
      $("trade-conn-status").textContent = "● 离线";
      $("trade-conn-status").className = "conn-status bad";
      setMsg(String(e.message || e), "bad");
    });
  }

  function cancelOrder(id) {
    acquireToken().then(function (ok) {
      if (!ok) { setMsg("取令牌失败，请刷新后重试", "bad"); return; }
      return postJSON("/api/trade/cancel", { id: id }, true).then(function (res) {
        if (res.body && res.body.ok) { setMsg("已取消 " + id, "ok"); }
        else { setMsg("取消失败：" + ((res.body && res.body.reason) || res.status), "bad"); }
        return load();
      });
    });
  }

  function onSubmit(ev) {
    ev.preventDefault();
    var side = document.querySelector("#order-form input[name=side]:checked").value;
    var body = {
      side: side,
      code: ($("f-code").value || "").trim().toUpperCase(),
      lots: parseInt($("f-lots").value, 10),
      price_cap: $("f-cap").value === "" ? null : Number($("f-cap").value),
      valid_until: $("f-valid").value,
      note: ($("f-note").value || "").trim()
    };
    var px = priceOf(body.code);
    var estTxt = px ? ("预估 " + money(px * body.lots) + " 元") : "无行情价";
    if (!window.confirm(
      "确认提交真实资金委托？\n\n" +
      "方向：" + (side === "BUY" ? "买入" : "卖出") + "\n" +
      "代码：" + body.code + "\n" +
      "股数：" + body.lots + "\n" +
      "价格上限：" + (body.price_cap || "不限") + "\n" +
      "有效期：" + body.valid_until + "\n" + estTxt
    )) return;

    $("btn-submit").disabled = true;
    setMsg("提交中…");
    acquireToken().then(function (ok) {
      if (!ok) { throw new Error("取令牌失败，请刷新后重试"); }
      return postJSON("/api/trade/submit", body, true);
    }).then(function (res) {
      var b = res.body || {};
      if (b.ok) {
        var pre = b.precheck || {};
        setMsg("已入队 " + b.id + "（预检：" + (pre.status || "-") +
               (pre.reason ? " / " + pre.reason : "") + "）", "ok");
        $("order-form").reset();
        $("f-valid").value = todayISO();
      } else {
        setMsg("被拒：" + (b.reason || res.status), "bad");
      }
      return load();
    }).catch(function (e) {
      setMsg("提交失败：" + (e.message || e), "bad");
    }).then(function () {
      $("btn-submit").disabled = false;
    });
  }

  function todayISO() {
    var d = new Date();
    var m = String(d.getMonth() + 1).padStart(2, "0");
    var day = String(d.getDate()).padStart(2, "0");
    return d.getFullYear() + "-" + m + "-" + day;
  }

  function initCodeList() {
    var dl = $("code-list");
    if (!dl) return;
    Object.keys(window.UNIVERSE || {}).forEach(function (c) {
      var o = document.createElement("option");
      o.value = c;
      o.label = (window.STOCK_NAMES && window.STOCK_NAMES[c]) || c;
      dl.appendChild(o);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    $("f-valid").value = todayISO();
    initCodeList();
    $("order-form").addEventListener("submit", onSubmit);
    $("btn-trade-refresh").addEventListener("click", load);
    ["f-code", "f-lots"].forEach(function (id) {
      $(id).addEventListener("input", updateEstimate);
    });
    load();
    setInterval(load, 5000);
  });
})();

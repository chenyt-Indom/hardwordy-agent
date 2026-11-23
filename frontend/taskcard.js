/* 任务结束卡片（2026-11-23）
 *
 * 用户原话：「自动迭代重启之后没有弹出卡片并显示已验证然后结束，
 *           结束任务时卡片必须有弹出来」。
 *
 * 链路：自动迭代 → 改完自动重启 → 重启后 iterloop.run_post_verify 再量一次分
 *       → 结论落成一张待播报卡片（后端 iterloop.push_card，落盘不靠内存）
 *       → 本文件每 4 秒轮询 /api/selfiter/cards → 渲染成对话里的一条卡片
 *       → 渲染完 POST ack 出队（所以刷新页面不会重复弹）。
 *
 * 为什么轮询而不是 SSE：复核在重启后的新进程里跑，用户可能过一会儿才刷新；
 * 卡片是落盘的，轮询天然补上「错过推送」这种情况，SSE 断了就永远收不到。
 */
(function () {
  "use strict";

  var API_GET = "/api/selfiter/cards";
  var API_ACK = "/api/selfiter/cards/ack";
  var TICK = 4000;
  var shown = {};          // 本页面已渲染过的 id（防同一会话内重复）
  var timer = null;
  var busy = false;

  function $(id) { return document.getElementById(id); }

  function num(v) {
    if (v === null || v === undefined || v === "") { return null; }
    var n = Number(v);
    return isFinite(n) ? n : null;
  }

  function fmt(v, d) {
    var n = num(v);
    if (n === null) { return "—"; }
    return d ? n.toFixed(d) : String(n);
  }

  function signed(v, d) {
    var n = num(v);
    if (n === null) { return "—"; }
    return (n > 0 ? "+" : "") + n.toFixed(d || 0);
  }

  var CSS = "" +
    ".tc-card{border:1px solid rgba(255,255,255,.14);border-radius:12px;" +
    "padding:10px 12px;margin:2px 0;background:rgba(255,255,255,.03);" +
    "font-size:13px;line-height:1.6;}" +
    ".tc-card.tc-ok{border-left:4px solid #2f9e63;}" +
    ".tc-card.tc-fail{border-left:4px solid #d0554b;}" +
    ".tc-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;}" +
    ".tc-badge{font-size:11px;font-weight:700;padding:2px 8px;border-radius:8px;" +
    "background:#2f6f4f;color:#eafff2;white-space:nowrap;}" +
    ".tc-card.tc-fail .tc-badge{background:#7a2f2f;color:#ffeceb;}" +
    ".tc-title{font-weight:600;}" +
    ".tc-why{opacity:.86;margin-top:6px;}" +
    ".tc-scores{display:flex;align-items:flex-end;gap:14px;margin-top:8px;flex-wrap:wrap;}" +
    ".tc-kpi{display:flex;flex-direction:column;line-height:1.35;}" +
    ".tc-kpi span{font-size:11px;opacity:.6;}" +
    ".tc-kpi b{font-size:17px;font-weight:700;}" +
    ".tc-kpi b.up{color:#5fd08a;}.tc-kpi b.down{color:#ff8d84;}" +
    ".tc-arrow{opacity:.5;padding-bottom:4px;}" +
    ".tc-rsi{margin-top:7px;font-size:12px;opacity:.8;}" +
    ".tc-replan{margin-top:6px;font-size:12px;color:#ffc46b;}" +
    ".tc-foot{margin-top:6px;font-size:11px;opacity:.5;}" +
    ".tc-x{float:right;border:0;background:transparent;color:inherit;opacity:.5;" +
    "cursor:pointer;font-size:12px;}";

  function injectCss() {
    if ($("tcCss")) { return; }
    var s = document.createElement("style");
    s.id = "tcCss";
    s.textContent = CSS;
    (document.head || document.documentElement).appendChild(s);
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (text !== undefined && text !== null) { e.textContent = String(text); }
    return e;
  }

  function buildCard(c) {
    var ok = String(c.state || "") === "verified";
    var box = el("div", "tc-card " + (ok ? "tc-ok" : "tc-fail"));

    var head = el("div", "tc-head");
    head.appendChild(el("span", "tc-badge", c.badge || (ok ? "已验证" : "复核未通过")));
    head.appendChild(el("span", "tc-title", c.title || "任务结束"));
    var x = el("button", "tc-x", "收起");
    x.type = "button";
    x.onclick = function () {
      var body = box.querySelector(".tc-body");
      if (!body) { return; }
      body.hidden = !body.hidden;
      x.textContent = body.hidden ? "展开" : "收起";
    };
    head.appendChild(x);
    box.appendChild(head);

    var body = el("div", "tc-body");
    if (c.why) { body.appendChild(el("div", "tc-why", c.why)); }

    var prev = num(c.prev), total = num(c.total), delta = num(c.delta);
    if (prev !== null || total !== null) {
      var row = el("div", "tc-scores");
      function kpi(label, val, cls) {
        var k = el("div", "tc-kpi");
        k.appendChild(el("span", null, label));
        k.appendChild(el("b", cls || null, val));
        return k;
      }
      row.appendChild(kpi("重启前", fmt(prev, 2)));
      row.appendChild(el("div", "tc-arrow", "→"));
      row.appendChild(kpi("重启后", fmt(total, 2)));
      if (delta !== null) {
        row.appendChild(kpi("差值", signed(delta, 2),
          delta < 0 ? "down" : (delta > 0 ? "up" : null)));
      }
      body.appendChild(row);
    }

    var r = c.rsi || null;
    if (r && (r.score !== null && r.score !== undefined)) {
      var line = "RSI " + fmt(r.score, 1) + " 分";
      if (r.shape) {
        line += " · " + r.shape;
        if (r.slope !== null && r.slope !== undefined) {
          line += "（" + signed(r.slope, 2) + "/次";
          if (r.samples) { line += "，" + r.samples + " 次采样"; }
          line += "）";
        }
      }
      if (r.weakest && r.weakest.name) {
        line += " · 最该修：" + r.weakest.name;
        if (r.weakest.value !== null && r.weakest.value !== undefined) {
          line += "（" + Math.round(Number(r.weakest.value) * 100) + "%）";
        }
      }
      body.appendChild(el("div", "tc-rsi", line));
    }

    if (c.replan) {
      var t = c.replan.ok ? "已自动排下一轮修复计划"
        : (c.replan.skipped
          ? "未重排：" + (c.replan.why || "被预算闸门拦住")
          : "重排失败：" + (c.replan.why || "未知原因"));
      body.appendChild(el("div", "tc-replan", t));
    }

    var foot = [];
    if (c.at) { foot.push(c.at); }
    if (c.pid) { foot.push("计划 " + c.pid); }
    if (foot.length) { body.appendChild(el("div", "tc-foot", foot.join(" · "))); }

    box.appendChild(body);
    return box;
  }

  function appendToChat(card) {
    var host = $("messages");
    if (!host) { return false; }
    var wrap = el("div", "msg bot");
    var bubble = el("div", "bubble");
    bubble.appendChild(buildCard(card));
    wrap.appendChild(bubble);
    host.appendChild(wrap);
    try { host.scrollTop = host.scrollHeight; } catch (e) { /* 忽略 */ }
    return true;
  }

  function ack(ids) {
    try {
      fetch(API_ACK, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: ids })
      });
    } catch (e) { /* 忽略：下次轮询还会拿到，最多重复渲染一次（已按 id 去重） */ }
  }

  function poll() {
    if (busy) { return; }
    busy = true;
    fetch(API_GET).then(function (r) { return r.json(); }).then(function (d) {
      busy = false;
      if (!d || !d.ok || !d.items || !d.items.length) { return; }
      var done = [];
      for (var i = 0; i < d.items.length; i++) {
        var c = d.items[i];
        var id = String(c.id || "");
        if (!id || shown[id]) { done.push(id); continue; }
        shown[id] = 1;
        if (appendToChat(c)) {
          done.push(id);
        } else if (window.__toast) {
          window.__toast((c.badge || "任务结束") + "：" + (c.title || ""), "ok");
          done.push(id);
        }
      }
      if (done.length) { ack(done); }
    }).catch(function () { busy = false; /* 断网/重启中：下一轮再试 */ });
  }

  function start() {
    injectCss();
    poll();
    if (!timer) { timer = setInterval(poll, TICK); }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();

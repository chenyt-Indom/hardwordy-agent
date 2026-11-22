/* 自迭代日志 —— 左侧弹窗（2026-11-23）
 *
 * 用户原话：「左侧工具栏增加一个可以查看自迭代日志，并且可以查看这些任务的
 * 迭代详情以及状态，并且一键拉起未成功执行的完整的任务，我需要这道保险机制」
 *
 * 后端（已存在，不另造接口）：
 *   GET  /api/selfiter/journal?limit=40   在跑/排队/历史 + 进度 + 动作 + 分数 + 事件
 *   GET  /api/selfiter/candidates         可一键拉起
 *   POST /api/selfiter/revive             {kind,tid,plan_id}
 *
 * 为什么轮询而不是 SSE：任务几分钟就变一次，2 秒轮询成本可以忽略；
 * 而 SSE 一旦被休眠/代理打断，进度会永远停在半路（比不显示更糟）。
 */
(function () {
  "use strict";

  var API_J = "/api/selfiter/journal?limit=40";
  var API_R = "/api/selfiter/revive";
  var timer = null;
  var openFlag = false;
  var lastData = null;
  var expanded = {};          // tid -> true 展开详情

  function $(id) { return document.getElementById(id); }
  function mk(t, c, x) {
    var e = document.createElement(t);
    if (c) e.className = c;
    if (x !== undefined && x !== null) e.textContent = String(x);
    return e;
  }
  function toast(m, k) {
    if (window.__toast) { window.__toast(m, k); } else { console.log("[iterjournal]", m); }
  }
  function jget(u) {
    return fetch(u).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }
  function jpost(u, b) {
    return fetch(u, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(b || {})
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (!r.ok) throw new Error((d && d.error) || ("HTTP " + r.status));
        return d;
      });
    });
  }

  var CSS = "" +
    "#ijPanel{position:fixed;top:64px;left:14px;width:384px;max-height:78vh;z-index:1200;" +
    "background:#1b1e24;color:#e8eaed;border:1px solid rgba(255,255,255,.12);border-radius:14px;" +
    "box-shadow:0 12px 36px rgba(0,0,0,.42);display:flex;flex-direction:column;overflow:hidden;}" +
    "#ijPanel[hidden]{display:none;}" +
    ".ij-head{display:flex;align-items:center;gap:8px;padding:10px 12px;" +
    "border-bottom:1px solid rgba(255,255,255,.10);}" +
    ".ij-title{font-size:13px;font-weight:600;flex:1;}" +
    ".ij-x{border:0;background:transparent;color:inherit;opacity:.7;cursor:pointer;font-size:14px;}" +
    ".ij-body{padding:10px 12px;overflow:auto;}" +
    ".ij-stat{font-size:11.5px;opacity:.74;margin-bottom:8px;line-height:1.65;}" +
    ".ij-sec{font-size:11.5px;opacity:.6;margin:10px 0 4px;}" +
    ".ij-task{border:1px solid rgba(255,255,255,.10);border-radius:9px;padding:7px 9px;" +
    "margin-top:7px;font-size:12px;}" +
    ".ij-task .r1{display:flex;gap:6px;align-items:center;}" +
    ".ij-task .nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}" +
    ".ij-badge{font-size:10.5px;padding:1px 6px;border-radius:6px;" +
    "background:rgba(255,255,255,.12);white-space:nowrap;}" +
    ".ij-badge.run{background:#2f6f4f;}.ij-badge.fail{background:#7a2f2f;}" +
    ".ij-badge.ok{background:#2f5b7a;}.ij-badge.wait{background:#6b5a2f;}" +
    ".ij-meta{opacity:.68;font-size:11px;margin-top:3px;line-height:1.55;}" +
    ".ij-ev{margin-top:4px;font-size:11px;opacity:.74;max-height:132px;overflow:auto;" +
    "white-space:pre-wrap;line-height:1.55;}" +
    ".ij-go{border:0;border-radius:7px;padding:4px 9px;cursor:pointer;font-size:11.5px;" +
    "background:#3b7dd8;color:#fff;font-weight:600;}" +
    ".ij-go[disabled]{opacity:.5;cursor:default;}" +
    ".ij-exp{border:0;background:transparent;color:#8ab4f8;cursor:pointer;" +
    "font-size:11px;padding:0;margin-top:3px;}" +
    ".ij-empty{opacity:.6;font-size:11.5px;margin-top:6px;}";

  function injectCss() {
    if ($("ijCss")) { return; }
    var s = document.createElement("style");
    s.id = "ijCss";
    s.textContent = CSS;
    (document.head || document.documentElement).appendChild(s);
  }

  var LABEL = {
    running: "在跑", waiting: "排队", done: "完成", verified: "已验证",
    failed: "失败", cancelled: "已终止", error: "异常"
  };
  function badgeCls(st) {
    if (st === "running") { return "ij-badge run"; }
    if (st === "failed" || st === "error") { return "ij-badge fail"; }
    if (st === "done" || st === "verified") { return "ij-badge ok"; }
    if (st === "waiting") { return "ij-badge wait"; }
    return "ij-badge";
  }

  function taskNode(t) {
    var box = mk("div", "ij-task");
    var r1 = mk("div", "r1");
    var nm = mk("span", "nm", t.title || "(无标题)");
    nm.title = (t.tid || "") + "  " + (t.source || "");
    r1.appendChild(nm);
    var st = String(t.state || "");
    r1.appendChild(mk("span", badgeCls(st), LABEL[st] || st || "?"));
    box.appendChild(r1);

    var meta = [];
    var pg = t.progress || {};
    if (pg.total) {
      meta.push("进度 " + (pg.done || 0) + "/" + pg.total +
                (pg.current ? " · 正在：" + String(pg.current).slice(0, 28) : ""));
    }
    if (t.done !== null && t.done !== undefined) {
      meta.push("成功 " + (t.done || 0) + " · 失败 " + (t.failed || 0));
    }
    if (t.source) { meta.push("来源 " + t.source); }
    if (t.score) { meta.push(t.score); }
    if (t.at) { meta.push(t.at); }
    if (meta.length) { box.appendChild(mk("div", "ij-meta", meta.join("　·　"))); }

    if (t.error) {
      var er = mk("div", "ij-meta");
      er.style.color = "#e5534b";
      er.textContent = "原因：" + String(t.error).slice(0, 160);
      box.appendChild(er);
    }

    var open = !!expanded[t.tid];
    if (open) {
      var acts = t.actions || [];
      if (acts.length) {
        box.appendChild(mk("div", "ij-sec", "动作清单"));
        acts.forEach(function (a) {
          var s = a.status === "done" ? "✅" : (a.status === "failed" ? "❌"
                  : (a.status === "skipped" ? "⏭"
                  : (a.status === "rejected" ? "⛔" : "⬜")));
          box.appendChild(mk("div", "ij-meta", s + " " + (a.id || "") + " " +
            String(a.title || "").slice(0, 40)));
        });
      }
      var evs = t.events || [];
      if (evs.length) {
        box.appendChild(mk("div", "ij-sec", "最近事件"));
        var ev = mk("div", "ij-ev");
        ev.textContent = evs.slice(-14).map(function (e) {
          return "· " + String(e.text || e.kind || "");
        }).join("\n");
        box.appendChild(ev);
      }
    }

    var ex = mk("button", "ij-exp", open ? "收起详情" : "查看详情");
    ex.type = "button";
    ex.onclick = function () {
      expanded[t.tid] = !expanded[t.tid];
      render(lastData);
    };
    box.appendChild(ex);

    // 未完成 / 未验证的给一个「拉起」按钮
    var stx = String(t.state || "");
    if (stx === "failed" || stx === "error" || stx === "cancelled") {
      box.appendChild(reviveBtn({
        kind: (stx === "cancelled" ? "cancelled" : "failed"),
        tid: t.tid, plan_id: t.plan_id, title: t.title
      }));
    }
    return box;
  }

  function reviveBtn(c) {
    var b = mk("button", "ij-go", "▶ 拉起");
    b.type = "button";
    b.title = "把这条任务重新拉起来（同一个计划，跑完会自动重启并验证）";
    if (c.can_revive === false) {
      b.disabled = true;
      b.title = c.why || "无法拉起";
    }
    b.onclick = function () {
      b.disabled = true;
      b.textContent = "拉起中…";
      jpost(API_R, { kind: c.kind, tid: c.tid, plan_id: c.plan_id })
        .then(function (d) {
          if (d && d.ok === false) { throw new Error(d.error || "拉起失败"); }
          toast("已拉起：" + String(c.title || "").slice(0, 20), "ok");
          load();
        })
        .catch(function (e) {
          b.disabled = false;
          b.textContent = "▶ 拉起";
          toast("拉起失败：" + (e && e.message ? e.message : e), "warn");
        });
    };
    return b;
  }

  function render(d) {
    var body = $("ijBody");
    if (!body) { return; }
    lastData = d;
    body.innerHTML = "";
    if (!d || d.ok === false) {
      body.appendChild(mk("div", "ij-empty",
        "读不到自迭代日志：" + ((d && d.error) || "未知原因")));
      return;
    }
    var c = d.counts || {};
    var stat = mk("div", "ij-stat");
    stat.textContent = "状态：" + (d.state || "空闲") +
      "　在跑 " + (c.running || 0) + " · 排队 " + (c.queue || 0) +
      " · 历史 " + (c.history || 0) + "　本轮 " + (d.budget || 0) +
      "/" + (d.max_rounds || "?");
    body.appendChild(stat);
    if (d.verify) {
      var v = mk("div", "ij-stat");
      v.style.color = "#e8b23a";
      v.textContent = "待验证：" + String(d.verify.title || "").slice(0, 30) +
        "（已试 " + (d.verify.tries || 0) + " 次）";
      body.appendChild(v);
    }

    var cand = d.candidates || [];
    body.appendChild(mk("div", "ij-sec",
      cand.length ? "可一键拉起（" + cand.length + "）" : "可一键拉起（无）"));
    cand.slice(0, 12).forEach(function (x) {
      var box = mk("div", "ij-task");
      var r1 = mk("div", "r1");
      var nm = mk("span", "nm", x.title || "(无标题)");
      nm.title = x.tid || "";
      r1.appendChild(nm);
      r1.appendChild(mk("span", "ij-badge wait", x.kind_text || x.kind || ""));
      box.appendChild(r1);
      var meta = [];
      if (x.at) { meta.push(x.at); }
      if (x.done !== null && x.done !== undefined) {
        meta.push("成功 " + (x.done || 0) + " · 失败 " + (x.failed || 0));
      }
      if (x.why) { meta.push(x.why); }
      if (meta.length) { box.appendChild(mk("div", "ij-meta", meta.join("　·　"))); }
      box.appendChild(reviveBtn(x));
      body.appendChild(box);
    });

    var tasks = d.tasks || [];
    body.appendChild(mk("div", "ij-sec", "全部任务（" + tasks.length + "）"));
    if (!tasks.length) {
      body.appendChild(mk("div", "ij-empty", "还没有任何自迭代任务。"));
    }
    tasks.slice(0, 30).forEach(function (t) { body.appendChild(taskNode(t)); });
  }

  function load() {
    return jget(API_J).then(function (d) { render(d); return d; })
      .catch(function (e) {
        var body = $("ijBody");
        if (body) {
          body.innerHTML = "";
          body.appendChild(mk("div", "ij-empty", "读取失败：" +
            (e && e.message ? e.message : e)));
        }
        return null;
      });
  }

  function startPoll() {
    stopPoll();
    timer = setInterval(load, 2000);
  }
  function stopPoll() {
    if (timer) { clearInterval(timer); timer = null; }
  }

  function build() {
    if ($("ijPanel")) { return; }
    injectCss();
    var p = mk("div");
    p.id = "ijPanel";
    p.hidden = true;
    var h = mk("div", "ij-head");
    h.appendChild(mk("span", "ij-title", "📜 自迭代日志"));
    var x = mk("button", "ij-x", "✕");
    x.type = "button";
    x.title = "关闭";
    x.onclick = close;
    h.appendChild(x);
    p.appendChild(h);
    var b = mk("div", "ij-body");
    b.id = "ijBody";
    p.appendChild(b);
    document.body.appendChild(p);
  }

  function open() {
    build();
    var p = $("ijPanel");
    if (!p) { return; }
    p.hidden = false;
    openFlag = true;
    load();
    startPoll();
  }
  function close() {
    var p = $("ijPanel");
    if (p) { p.hidden = true; }
    openFlag = false;
    stopPoll();
  }
  function toggle() { if (openFlag) { close(); } else { open(); } }

  function mountButton() {
    if ($("btnIterJournal")) { return; }
    var host = $("topQuick");
    if (!host) { setTimeout(mountButton, 400); return; }
    var b = document.createElement("button");
    b.type = "button";
    b.className = "tq-btn";
    b.id = "btnIterJournal";
    b.title = "看自迭代任务的进度/状态/日志，未跑完的可一键拉起";
    var ico = document.createElement("span");
    ico.className = "tq-ico";
    ico.textContent = "📜";
    var txt = document.createElement("span");
    txt.className = "tq-txt";
    txt.textContent = "迭代日志";
    b.appendChild(ico);
    b.appendChild(txt);
    b.onclick = toggle;
    host.appendChild(b);
  }

  function bind() { mountButton(); }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }

  window.IterJournal = { open: open, close: close, toggle: toggle, reload: load };
})();

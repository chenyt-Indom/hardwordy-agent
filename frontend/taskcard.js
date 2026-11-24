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
      var line = "评分 " + fmt(r.score, 1) + " 分";
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

  /* ==================================================================
   * 说一句就开工：待办卡片流（2026-11-23）
   *
   * 用户原话：「少打字，说完就看到它在动」「过程过长 / 碎步」。
   * 原来的交互是「打字 → 等一大段回复」，中间那段黑箱最难受。这里改成：
   *   按住首页悬浮球说话（index.html 里那段 MediaRecorder）
   *   → 语音转出来的那句话进 sendMessage（app.js 的 doSend）
   *   → 本文件把它交给后端 /api/job/todos 拆成待办数组
   *   → **立刻**渲染成可勾选的卡片（不等回复），再按依赖分批触发执行
   *   → 每张卡都订阅状态更新，做完就自己打勾。
   *
   * 并行：同一句话切出来的待办 deps 都是 []（见 backend/jobplan.split_todos），
   * 所以第一批可以**同时**发出去 —— 参考 GAP 的并行工具调用思路：没有依赖就
   * 别排队，串行只会让用户白等。有依赖的（deps 指向前面的卡）自动排到下一批。
   * ================================================================== */
  var API_TODOS = "/api/job/todos";
  var API_PLAN = "/api/job/plan";
  var API_TODO_STATUS = "/api/plan/status/";     // 后面拼 job_id
  var RUN_POLL = 1200;                           // 进度轮询间隔（毫秒）
  var RUN_MAX_TICKS = 150;                       // 单个待办最多盯 3 分钟

  var todoList = [];          // 权威状态：[{id,title,done,status,deps,percent,error}]
  var todoSubs = [];          // 订阅者（谁想知道待办变了就注册）
  var todoRowEl = {};         // id -> {row, chk, st, bar}
  var todoBox = null;         // 卡片容器
  var todoHead = null;        // 标题节点
  var todoTimers = {};        // id -> setInterval 句柄

  function tget(url) {
    return fetch(url).then(function (r) {
      if (!r.ok) { throw new Error("HTTP " + r.status); }
      return r.json();
    });
  }

  function tpost(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (!r.ok) { throw new Error((d && d.detail) || ("HTTP " + r.status)); }
        return d;
      });
    });
  }

  // ---------- 状态层（唯一真实来源；卡片只是它的一个视图） ----------
  function normTodos(items) {
    var out = [];
    var src = items || [];
    for (var i = 0; i < src.length; i++) {
      var it = src[i];
      if (typeof it === "string") { it = { title: it }; }
      if (!it || typeof it !== "object") { continue; }
      var title = String(it.title || it.text || "").trim();
      if (!title) { continue; }
      var id = (it.id !== undefined && it.id !== null) ? String(it.id) : String(i + 1);
      var deps = [];
      var rawDeps = it.deps || it.depends_on || [];
      for (var k = 0; k < rawDeps.length; k++) { deps.push(String(rawDeps[k])); }
      out.push({
        id: id,
        title: title.slice(0, 60),
        done: !!it.done,
        // status: pending（等着开工）/ running（在做）/ done / error
        status: String(it.status || (it.done ? "done" : "pending")),
        deps: deps,
        percent: 0,
        error: ""
      });
    }
    return out;
  }

  function snapshot() {
    return todoList.map(function (t) {
      return { id: t.id, title: t.title, done: !!t.done, status: t.status,
               deps: t.deps.slice(), percent: t.percent, error: t.error };
    });
  }

  function notify() {
    var snap = snapshot();
    for (var i = 0; i < todoSubs.length; i++) {
      try { todoSubs[i](snap); } catch (e) { /* 订阅者自己出错别拖垮卡片 */ }
    }
    paint();
  }

  function subscribe(fn) {
    if (typeof fn !== "function") { return function () {}; }
    todoSubs.push(fn);
    try { fn(snapshot()); } catch (e) { /* 忽略 */ }
    return function () {                                 // 返回退订函数
      var i = todoSubs.indexOf(fn);
      if (i >= 0) { todoSubs.splice(i, 1); }
    };
  }

  function findTodo(id) {
    var key = String(id);
    for (var i = 0; i < todoList.length; i++) {
      if (todoList[i].id === key) { return todoList[i]; }
    }
    return null;
  }

  /** 勾一张卡。**只改这一张** —— 其余卡片的状态一个字都不动。 */
  function setDone(id, done) {
    var t = findTodo(id);
    if (!t) { return null; }
    t.done = !!done;
    if (t.done) {
      t.status = "done";
    } else if (t.status === "done") {
      t.status = t.percent > 0 ? "running" : "pending";
    }
    notify();
    return snapshot();
  }

  function setStatus(id, status, extra) {
    var t = findTodo(id);
    if (!t) { return; }
    if (status) { t.status = String(status); }
    if (extra && extra.percent !== undefined) { t.percent = Number(extra.percent) || 0; }
    if (extra && extra.error !== undefined) { t.error = String(extra.error || ""); }
    if (t.status === "done") { t.done = true; }
    notify();
  }

  // ---------- 依赖分批：无依赖的排第一批（可并行），有依赖的往后排 ----------
  function planWaves(items) {
    var src = items || todoList;
    var waves = [];
    var left = src.slice();
    var placed = {};
    var guard = 0;
    while (left.length && guard++ < 50) {
      var wave = [];
      var rest = [];
      for (var i = 0; i < left.length; i++) {
        var t = left[i];
        var ready = true;
        var deps = t.deps || [];
        for (var k = 0; k < deps.length; k++) {
          if (!placed[String(deps[k])]) { ready = false; break; }
        }
        if (ready) { wave.push(t); } else { rest.push(t); }
      }
      if (!wave.length) { wave = rest.splice(0); }     // 依赖成环：别死等，直接放出去
      for (var j = 0; j < wave.length; j++) { placed[String(wave[j].id)] = 1; }
      waves.push(wave.map(function (x) { return String(x.id); }));
      left = rest;
    }
    return waves;
  }

  // ---------- 视图层：可勾选的待办卡片 ----------
  function paint() {
    if (!todoBox) { return; }
    var doneN = 0;
    for (var i = 0; i < todoList.length; i++) {
      if (todoList[i].done) { doneN++; }
    }
    if (todoHead) {
      todoHead.textContent =
        "开工 · 共 " + todoList.length + " 件，已完成 " + doneN + " 件";
    }
    for (var k = 0; k < todoList.length; k++) {
      var t = todoList[k];
      var refs = todoRowEl[t.id];
      if (!refs) { continue; }
      refs.chk.checked = !!t.done;
      refs.row.className = "td-row" + (t.done ? " td-done" : "") +
        (t.status === "error" ? " td-error" : "") +
        (t.status === "running" ? " td-running" : "");
      var txt = "等待中";
      if (t.status === "running") {
        txt = "进行中" + (t.percent ? " " + t.percent + "%" : "");
      } else if (t.status === "done" || t.done) {
        txt = "已完成";
      } else if (t.status === "error") {
        txt = "失败";
      }
      refs.st.textContent = txt;
      if (refs.bar) {
        refs.bar.style.width = Math.max(0, Math.min(100, t.percent)) + "%";
      }
    }
  }

  /** 待办数组 → 一条可勾选的卡片（追加进 #messages），返回操作句柄。 */
  function renderTaskCards(items, opts) {
    var list = normTodos(items);
    if (!list.length) { return null; }
    todoList = list;
    todoRowEl = {};
    todoSubs = [];                            // 新一轮：旧订阅随旧卡片一起退休
    opts = opts || {};

    var host = $("messages");
    var wrap = el("div", "msg bot");
    var bubble = el("div", "bubble td-bubble");
    var card = el("div", "td-card");

    var head = el("div", "td-head");
    var title = el("span", "td-head-title",
                   "开工 · 共 " + list.length + " 件，已完成 0 件");
    head.appendChild(title);
    todoHead = title;
    var runBtn = el("button", "td-run", "▶ 开工");
    runBtn.type = "button";
    runBtn.onclick = function () {
      if (runBtn.disabled) { return; }
      runBtn.disabled = true;
      runBtn.textContent = "已开工";
      runTodos();
    };
    head.appendChild(runBtn);
    card.appendChild(head);

    var rows = el("div", "td-rows");
    for (var i = 0; i < list.length; i++) {
      var t = list[i];
      var row = el("div", "td-row");
      var chk = el("input", "td-chk");
      chk.type = "checkbox";
      chk.checked = !!t.done;
      chk.onchange = (function (id) {
        return function () { setDone(id, this.checked); };
      })(t.id);
      var num = el("span", "td-num", String(i + 1));
      var txt = el("span", "td-title", t.title);
      var st = el("span", "td-st", "等待中");
      var barWrap = el("div", "td-bar");
      var bar = el("i", "td-bar-fill");
      bar.style.width = "0%";
      barWrap.appendChild(bar);
      row.appendChild(chk);
      row.appendChild(num);
      row.appendChild(txt);
      row.appendChild(st);
      row.appendChild(barWrap);
      rows.appendChild(row);
      todoRowEl[t.id] = { row: row, chk: chk, st: st, bar: bar };
    }
    card.appendChild(rows);
    bubble.appendChild(card);
    wrap.appendChild(bubble);
    if (host) {
      host.appendChild(wrap);
      try { host.scrollTop = host.scrollHeight; } catch (e) { /* 忽略 */ }
    } else if (window.__toast) {
      window.__toast("待办：" + list.map(function (x) { return x.title; }).join(" / "), "ok");
    }
    todoBox = card;
    paint();
    if (opts.autorun) { runTodos(); }
    return {
      el: card,
      todos: function () { return snapshot(); },
      setDone: setDone,
      subscribe: subscribe,
      waves: function () { return planWaves(); },
      run: runTodos
    };
  }

  // ---------- 执行层：同一批里没有依赖的并排发出去 ----------
  function pollTodo(t, jobId) {
    var ticks = 0;
    if (todoTimers[t.id]) { clearInterval(todoTimers[t.id]); }
    todoTimers[t.id] = setInterval(function () {
      ticks++;
      if (ticks > RUN_MAX_TICKS) {
        clearInterval(todoTimers[t.id]);
        delete todoTimers[t.id];
        setStatus(t.id, "error", { error: "超时没等到结果" });
        return;
      }
      tget(API_TODO_STATUS + encodeURIComponent(jobId)).then(function (d) {
        if (!d || !d.ok || !d.job) { return; }
        var job = d.job;
        var percent = Number(job.percent) || 0;
        var st = String(job.status || "");
        if (st === "done" || st === "partial") {
          clearInterval(todoTimers[t.id]);
          delete todoTimers[t.id];
          // 跑完了：全成都勾上；有步骤没跑成如实标失败（别假装成功）
          setStatus(t.id, st === "done" ? "done" : "error",
                    { percent: 100, error: st === "done" ? "" : "有步骤没跑成" });
        } else {
          setStatus(t.id, "running", { percent: percent });
        }
      }).catch(function () { /* 断网 / 后端重启中：下一拍再试 */ });
    }, RUN_POLL);
  }

  function runOne(t) {
    setStatus(t.id, "running", { percent: 0 });
    return tpost(API_PLAN, { goal: t.title }).then(function (d) {
      var job = (d && d.job) || {};
      if (!d || d.ok === false || !job.id) {
        setStatus(t.id, "error", { error: (d && d.error) || "没能开工" });
        return null;
      }
      pollTodo(t, job.id);
      return job.id;
    }).catch(function (err) {
      setStatus(t.id, "error", { error: (err && err.message) || "没能开工" });
      return null;
    });
  }

  /** 按依赖分批跑；同一批（deps 都满足）**并行**触发，不排在前面等。 */
  function runTodos() {
    var waves = planWaves();
    var chain = Promise.resolve();
    waves.forEach(function (ids) {
      chain = chain.then(function () {
        var jobs = [];
        ids.forEach(function (id) {
          var t = findTodo(id);
          if (t && !t.done && t.status !== "running") { jobs.push(runOne(t)); }
        });
        return Promise.all(jobs);              // ← 并行：这一批一起发出去
      });
    });
    return chain;
  }

  /** 一句话 → 待办卡片：先拆（毫秒级），再按 opts.autorun 决定要不要立刻开工。 */
  function startFromSay(text, opts) {
    var say = String(text || "").trim();
    opts = opts || {};
    if (!say) { return Promise.resolve(null); }
    return tpost(API_TODOS, { text: say }).then(function (d) {
      var todos = (d && d.todos) || [];
      if (!d || d.ok === false || !todos.length) {
        throw new Error((d && d.error) || "没能拆成待办");
      }
      return renderTaskCards(todos, { autorun: opts.autorun !== false });
    });
  }

  /** 这句话该怎么对待：问句 / 寒暄走对话，不硬拆成活。 */
  function looksLikeTask(text) {
    var t = String(text || "").trim();
    if (t.length < 4) { return false; }
    if (/^(你好|您好|hi|hello|在吗|谢谢|thanks?)[!！。.\s]*$/i.test(t)) { return false; }
    if (/[?？]$/.test(t)) { return false; }
    return true;
  }

  /**
   * sendMessage 入口（app.js 的 doSend）递过来的那句话。
   * · 语音来的（opts.voice）→ 拆待办 + **立刻开工**（「说完就看到它在动」）；
   * · 打字来的 → 只出卡片（真开跑由用户点卡片上的「▶ 开工」决定），
   *   不抢对话链路的活，也不会顺手多建一个任务。
   */
  function onUserSay(text, opts) {
    var say = String(text || "").trim();
    opts = opts || {};
    if (!looksLikeTask(say)) { return null; }
    var p = startFromSay(say, { autorun: !!opts.voice });
    p.catch(function (err) {
      if (window.__toast) {
        window.__toast("待办没拆出来：" + ((err && err.message) || err), "err");
      }
    });
    return p;
  }

  window.TaskCard = {
    renderTaskCards: renderTaskCards,
    setDone: setDone,
    subscribe: subscribe,
    getTodos: snapshot,
    planWaves: planWaves,
    startFromSay: startFromSay,
    beginFromSay: onUserSay,      // index.html 的悬浮球用这个名字
    onUserSay: onUserSay,
    runTodos: runTodos,
    looksLikeTask: looksLikeTask,
    // 勾完还有剩下的，就存成「明日接着做」（别因为关窗口就丢）
    carryOver: function () {
      var left = todoList.filter(function (t) { return !t.done; });
      return left.map(function (t) { return { title: t.title, done: false }; });
    }
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();

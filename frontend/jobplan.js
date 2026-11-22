/* 一句话订好整件事 —— 侧栏进度面板（2026-11-21）
 *
 * 解决的问题
 * ----------
 * 用户此前是「查天气 → 查路线 → 写行程 → 做 PPT」一件一件交代：每件事都要
 * 重新开一次口，中间还得自己盯着哪步做完了。这是摩擦点「同一需求被重复发送」
 * 里最贵的一种 —— 不是重复一句话，而是把一件事拆成四次任务发。
 *
 * 这一层做什么
 * ------------
 *   · 一个输入框：「帮我订好汕头两日游，出文档和 PPT」→ 一句话交代整件事；
 *   · 侧栏进度条：每步一行（等待中 / 进行中 / 完成 / 失败），1 秒轮询一次；
 *   · 跑完给「⬇ 下载成品包」：一个 zip，里面是文档 / PPT + 任务说明。
 *
 * 后端对接（三条，别自己另造接口）
 *   POST /api/job/plan          建任务并开跑 → 回来一个 job（含 id / steps）
 *   GET  /api/plan/status/{id}  轮询进度（返回 backend/jobplan.py public_job 的口径）
 *   GET  /api/job/zip?job=<id>  下载成品包
 *
 * 为什么轮询而不是 SSE：任务最长几十秒，1 秒一次的轮询成本可以忽略；而 SSE
 * 一旦被代理 / 休眠打断，用户看到的进度就永远停在半路（比不显示更糟）。
 */
(function () {
  "use strict";

  var API_PLAN = "/api/job/plan";
  var API_STATUS = "/api/plan/status/";      // 后面拼 job_id
  var API_ZIP = "/api/job/zip";              // ?job=<job_id>
  var API_WATCH = "/api/watch";              // 盯梢：列表 + 待弹卡（GET）/ 一句话新建（POST）
  var API_WATCH_ACK = "/api/watch/ack";      // 看过的卡标记已读
  var API_WATCH_DEL = "/api/watch/remove";   // 停掉一条监视
  var API_WATCH_RUN = "/api/watch/run";      // ★ 立即跑一次（同步抓 2~8 秒）
  var API_WATCH_DETAIL = "/api/watch/detail"; // ★ 一屏看全：时间线 + 最近内容
  var API_WATCH_RUN = "/api/watch/run";      // ★ 立即运行一次（比对 + 返回当前值）
  var API_WATCH_DETAIL = "/api/watch/detail"; // ★ 详情：时间线 + 最近内容 + 下次运行
  var API_SCHED_ARCHIVED = "/api/schedule/archived";  // ★ 已删除但可恢复
  var API_SCHED_RESTORE = "/api/schedule/restore";    // ★ 恢复

  var root = null;          // 面板根节点
  var timer = null;         // 轮询句柄
  var curJob = null;        // 当前展示的任务（后端返回的 public_job）
  var polling = "";         // 正在轮询的 job_id（防重复起表）

  function $(id) { return document.getElementById(id); }

  function mk(tag, cls, txt) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt !== undefined && txt !== null) e.textContent = String(txt);
    return e;
  }

  function toast(msg, kind) {
    if (window.__toast) { window.__toast(msg, kind); }
    else { console.log("[jobplan]", msg); }
  }

  function jget(url) {
    return fetch(url).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  function jpost(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (!r.ok) throw new Error((d && d.detail) || ("HTTP " + r.status));
        return d;
      });
    });
  }

  // ---------- 样式（一次性注入，不往 style.css 里加全局规则） ----------
  var CSS = "" +
    "#jpPanel{position:fixed;top:64px;right:14px;width:352px;max-height:76vh;z-index:1200;" +
    "background:#1b1e24;color:#e8eaed;border:1px solid rgba(255,255,255,.12);" +
    "border-radius:14px;box-shadow:0 12px 36px rgba(0,0,0,.42);display:flex;" +
    "flex-direction:column;overflow:hidden;}" +
    "#jpPanel[hidden]{display:none;}" +
    ".jp-head{display:flex;align-items:center;gap:8px;padding:10px 12px;" +
    "border-bottom:1px solid rgba(255,255,255,.10);}" +
    ".jp-title{font-size:13px;font-weight:600;flex:1;}" +
    ".jp-x{border:0;background:transparent;color:inherit;opacity:.7;cursor:pointer;font-size:14px;}" +
    ".jp-body{padding:10px 12px;overflow:auto;}" +
    ".jp-in{width:100%;box-sizing:border-box;min-height:52px;resize:vertical;border-radius:9px;" +
    "border:1px solid rgba(255,255,255,.16);background:rgba(255,255,255,.05);color:inherit;" +
    "padding:8px;font:inherit;font-size:12.5px;}" +
    ".jp-row{display:flex;gap:8px;align-items:center;margin-top:8px;}" +
    ".jp-go{flex:1;border:0;border-radius:9px;padding:8px 10px;cursor:pointer;font-size:12.5px;" +
    "background:#3b7dd8;color:#fff;font-weight:600;}" +
    ".jp-go[disabled]{opacity:.55;cursor:default;}" +
    ".jp-hint{font-size:11.5px;opacity:.62;margin-top:6px;line-height:1.5;}" +
    ".jp-bar{height:6px;border-radius:6px;background:rgba(255,255,255,.12);overflow:hidden;margin:10px 0 4px;}" +
    ".jp-fill{height:100%;width:0;background:#3fa66a;transition:width .25s ease;}" +
    ".jp-meta{font-size:11.5px;opacity:.72;display:flex;justify-content:space-between;}" +
    ".jp-step{border:1px solid rgba(255,255,255,.10);border-radius:9px;padding:7px 9px;" +
    "margin-top:7px;font-size:12px;}" +
    ".jp-step .t{display:flex;gap:6px;align-items:center;}" +
    ".jp-step .n{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}" +
    ".jp-step .s{opacity:.72;font-size:11px;}" +
    ".jp-step .e{color:#e5534b;font-size:11px;margin-top:3px;word-break:break-all;}" +
    ".jp-step .b{opacity:.62;font-size:11px;margin-top:3px;white-space:pre-wrap;" +
    "max-height:52px;overflow:hidden;}" +
    ".jp-zip{display:block;text-align:center;margin-top:10px;padding:9px;border-radius:9px;" +
    "background:#3fa66a;color:#fff;font-weight:600;text-decoration:none;font-size:12.5px;}" +
    ".jp-warn{margin-top:8px;font-size:11.5px;color:#e8883a;white-space:pre-wrap;}" +
    // ---- 盯梢面板（与上面同一套外观，只是改成左侧、窄一点）----
    "#jpwPanel{position:fixed;top:64px;left:14px;width:340px;max-height:76vh;z-index:1200;" +
    "background:#1b1e24;color:#e8eaed;border:1px solid rgba(255,255,255,.12);" +
    "border-radius:14px;box-shadow:0 12px 36px rgba(0,0,0,.42);display:flex;" +
    "flex-direction:column;overflow:hidden;}" +
    "#jpwPanel[hidden]{display:none;}" +
    ".jpw-item{border:1px solid rgba(255,255,255,.10);border-radius:9px;padding:7px 9px;" +
    "margin-top:7px;font-size:12px;}" +
    ".jpw-item .t{display:flex;gap:6px;align-items:center;}" +
    ".jpw-item .n{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}" +
    ".jpw-item .s{opacity:.7;font-size:11px;white-space:nowrap;}" +
    ".jpw-item .m{opacity:.62;font-size:11px;margin-top:3px;word-break:break-all;}" +
    ".jpw-item .w{color:#e8883a;font-size:11px;margin-top:3px;}" +
    ".jpw-x{border:0;background:transparent;color:#e5534b;cursor:pointer;font-size:11px;" +
    "padding:0 2px;}" +
    ".jpw-cards{margin-top:8px;}" +
    ".jpw-cards .jp-hint{opacity:.72;}" +
    // ★ 2026-11-23 用户要求：盯梢卡片加「立即运行」「查看」两个按钮。
    ".jpw-acts{display:flex;gap:6px;margin-top:6px;}" +
    ".jpw-btn{border:1px solid rgba(255,255,255,.18);background:rgba(255,255,255,.06);" +
    "color:#cfd3d8;border-radius:6px;padding:2px 8px;font-size:11px;cursor:pointer;}" +
    ".jpw-btn:hover{background:rgba(255,255,255,.14);}" +
    ".jpw-btn:disabled{opacity:.45;cursor:default;}" +
    // 详情弹窗（一屏看全：时间线 + 最近内容 + 下次运行）
    ".jpwd-mask{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:1299;}" +
    ".jpwd-mask[hidden]{display:none;}" +
    "#jpwDetail{position:fixed;top:7vh;left:50%;transform:translateX(-50%);" +
    "width:min(760px,92vw);max-height:82vh;z-index:1300;background:#1b1e24;color:#e8eaed;" +
    "border:1px solid rgba(255,255,255,.12);border-radius:14px;" +
    "box-shadow:0 16px 48px rgba(0,0,0,.5);display:flex;flex-direction:column;overflow:hidden;}" +
    "#jpwDetail[hidden]{display:none;}" +
    ".jpwd-head{display:flex;align-items:center;gap:8px;padding:10px 12px;" +
    "border-bottom:1px solid rgba(255,255,255,.10);}" +
    ".jpwd-head .tt{flex:1;font-size:13px;font-weight:600;overflow:hidden;" +
    "text-overflow:ellipsis;white-space:nowrap;}" +
    ".jpwd-body{overflow:auto;padding:10px 12px;font-size:12px;line-height:1.6;}" +
    ".jpwd-sec{margin-bottom:12px;}" +
    ".jpwd-sec h4{margin:0 0 5px;font-size:12px;opacity:.75;font-weight:600;}" +
    ".jpwd-pre{white-space:pre-wrap;word-break:break-word;background:rgba(255,255,255,.05);" +
    "border-radius:8px;padding:8px;max-height:240px;overflow:auto;font-size:11px;opacity:.9;}" +
    ".jpwd-tl{list-style:none;margin:0;padding:0;}" +
    ".jpwd-tl li{padding:5px 0;border-bottom:1px dashed rgba(255,255,255,.10);font-size:11px;}" +
    ".jpwd-kv{display:flex;flex-wrap:wrap;gap:10px;opacity:.8;font-size:11px;}";

  function injectCss() {
    if ($("jpStyle")) return;
    var st = document.createElement("style");
    st.id = "jpStyle";
    st.textContent = CSS;
    document.head.appendChild(st);
  }

  // ---------- 面板 ----------
  function build() {
    if (root) return root;
    injectCss();
    root = mk("section");
    root.id = "jpPanel";
    root.hidden = true;

    var head = mk("div", "jp-head");
    head.appendChild(mk("span", "jp-title", "🧩 一句话订整件事"));
    var x = mk("button", "jp-x", "✕");
    x.title = "收起";
    x.onclick = close;
    head.appendChild(x);
    root.appendChild(head);

    var body = mk("div", "jp-body");

    var ta = mk("textarea", "jp-in");
    ta.id = "jpGoal";
    ta.placeholder = "一句话交代整件事，例如：帮我订好汕头两日游，出文档和 PPT";
    body.appendChild(ta);

    var row = mk("div", "jp-row");
    var go = mk("button", "jp-go", "▶ 开始");
    go.id = "jpGo";
    go.onclick = function () { start(ta.value || ""); };
    row.appendChild(go);
    body.appendChild(row);
    body.appendChild(mk("div", "jp-hint",
      "能跑的步骤：查天气 · 查路线 · 写 Word 文档 · 做 PPT。跑完自动打成一个 zip。"));

    var box = mk("div");
    box.id = "jpProg";
    box.appendChild(mk("div", "jp-hint", "还没有任务。"));
    body.appendChild(box);

    root.appendChild(body);
    document.body.appendChild(root);

    // Ctrl/⌘ + Enter 直接开始，省得再找按钮
    ta.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        start(ta.value || "");
      }
    });
    return root;
  }

  function open() {
    build();
    root.hidden = false;
    var ta = $("jpGoal");
    if (ta) { try { ta.focus(); } catch (e) { /* 焦点拿不到不影响用 */ } }
  }

  function close() {
    if (root) root.hidden = true;
    stopPoll();
  }

  function toggle() {
    if (root && !root.hidden) close();
    else open();
  }

  // ---------- 进度渲染 ----------
  var MARK = { pending: "○", running: "◐", done: "✔", error: "✕" };

  function stepLabel(st) {
    var status = String(st.status || "pending");
    if (status === "done") return st.ok === false ? "完成（有报错）" : "完成";
    if (status === "error") return "失败";
    if (status === "running") return "进行中…";
    return "等待中";
  }

  function stepLine(st) {
    var row = mk("div", "jp-step");
    var top = mk("div", "t");
    top.appendChild(mk("span", "", MARK[String(st.status || "pending")] || "○"));
    top.appendChild(mk("span", "n",
      (st.id ? st.id + ". " : "") + (st.title || st.tool || "")));
    var label = stepLabel(st);
    if (st.secs) label += " · " + st.secs + "s";
    top.appendChild(mk("span", "s", label));
    row.appendChild(top);
    if (st.error) row.appendChild(mk("div", "e", st.error));
    else if (st.brief) row.appendChild(mk("div", "b", String(st.brief).slice(0, 220)));
    return row;
  }

  /** 把后端给的 job 画成侧栏进度（总进度条 + 每步一行 + 完成后的下载按钮）。
   *
   *  job 的口径就是 backend/jobplan.py 的 public_job()：
   *    {id, goal, status, percent, finished, total, steps:[...], zip_ready, warnings}
   *  这里**只画**：状态机全在后端，前端不自己推演进度，免得两边对不上。
   */
  function renderJobProgress(job) {
    build();
    var box = $("jpProg");
    if (!box) return null;
    box.textContent = "";
    if (!job) {
      box.appendChild(mk("div", "jp-hint", "还没有任务。"));
      return box;
    }
    curJob = job;

    var pct = Number(job.percent || 0);
    if (!isFinite(pct)) pct = 0;
    pct = Math.max(0, Math.min(100, pct));
    var bar = mk("div", "jp-bar");
    var fill = mk("div", "jp-fill");
    fill.style.width = pct + "%";
    if (String(job.status) === "partial") fill.style.background = "#e8883a";
    bar.appendChild(fill);
    box.appendChild(bar);

    var meta = mk("div", "jp-meta");
    meta.appendChild(mk("span", "", "进度 " + pct + "%（" +
      Number(job.finished || 0) + "/" + Number(job.total || 0) + "）"));
    meta.appendChild(mk("span", "", {
      pending: "排队中", running: "执行中", done: "已完成",
      partial: "部分完成", error: "失败"
    }[String(job.status)] || String(job.status || "")));
    box.appendChild(meta);

    if (job.goal) box.appendChild(mk("div", "jp-hint", "目标：" + job.goal));

    (job.steps || []).forEach(function (s) { box.appendChild(stepLine(s || {})); });

    if (job.zip_ready) {
      var a = mk("a", "jp-zip", "⬇ 下载成品包（" +
        ((job.zip_files && job.zip_files.length) || 0) + " 个文件 + 任务说明）");
      a.href = API_ZIP + "?job=" + encodeURIComponent(job.id);
      a.target = "_blank";
      a.rel = "noopener";
      box.appendChild(a);
    }

    (job.warnings || []).forEach(function (w) {
      box.appendChild(mk("div", "jp-warn", "⚠ " + w));
    });

    var go = $("jpGo");
    if (go) {
      var busy = String(job.status) === "running" || String(job.status) === "pending";
      go.disabled = busy;
      go.textContent = busy ? "…正在跑" : "▶ 开始";
    }
    return box;
  }

  // ---------- 轮询 ----------
  function stopPoll() {
    if (timer) { clearInterval(timer); timer = null; }
    polling = "";
    var go = $("jpGo");
    if (go && curJob) {
      var s = String(curJob.status || "");
      go.disabled = (s === "running" || s === "pending");
    }
  }

  function pollJob(jobId) {
    if (!jobId) return;
    if (polling === jobId && timer) return;       // 已经在盯这个任务
    stopPoll();
    polling = jobId;
    timer = setInterval(function () {
      jget(API_STATUS + encodeURIComponent(jobId)).then(function (d) {
        if (!d || !d.ok) {
          stopPoll();
          toast((d && d.error) || "进度取不到", "warn");
          return;
        }
        var job = d.job || {};
        renderJobProgress(job);
        var s = String(job.status || "");
        if (s === "done" || s === "partial" || s === "error") {
          stopPoll();
          if (job.zip_ready) toast("做完了 —— 点面板里的「下载成品包」", "ok");
          else toast("做完了，但成品包没打出来（看面板里的提示）", "warn");
        }
      }).catch(function (e) {
        stopPoll();
        toast("进度轮询失败：" + (e && e.message ? e.message : e), "warn");
      });
    }, 1000);
  }

  // ==================================================================
  //  「盯梢」面板（2026-11-22）
  // ==================================================================
  // 为什么要有它
  // ------------
  // 用户连续 4 轮追问「盯到了吗」——这是他真正想要的能力，而原来的状态是：
  // 后台确实在按点比对，但**变化了没人告诉他**，只能靠他自己回来问。
  //
  // 这一层做三件事：
  //   ① 一句话入口：输入「帮我盯着某商品降价」→ POST /api/watch，
  //      意图解析在后端（handoff.parse_watch_intent），本地路径走快照比对、
  //      关键词 / 网址走联网抓取，用户不需要知道区别；
  //   ② 列表：当前所有监视 + 各自的**最近一次触发时间**（有变化才算触发）；
  //   ③ 主动弹卡：后端 push_livecard 把卡排进队列，这里轮询取走并交给
  //      livecard.js 渲染 —— 用户不用问，卡自己会冒出来。
  // 后端接口（别自己另造）：
  //   GET  /api/watch?session=&drain=1 → {ok, items, cards, group}
  //   POST /api/watch {text}           → 建一条
  //   POST /api/watch/ack {id}         → 卡看过了
  //   POST /api/watch/remove {id}      → 停掉
  // ==================================================================
  var wroot = null;          // 盯梢面板根节点
  var wtimer = null;         // 轮询句柄
  var witems = [];           // 最近一次拿到的监视列表
  var wcardsSeen = {};       // 已弹过的卡（按 ts+标题去重，避免同一张反复 toast）

  function wmkItem(it) {
    it = it || {};
    var row = mk("div", "jpw-item");
    var top = mk("div", "t");
    top.appendChild(mk("span", "", it.kind === "path" ? "📁" : "🌐"));
    top.appendChild(mk("span", "n", it.name || it.target || "监视"));
    top.appendChild(mk("span", "s", it.period || ""));
    var del = mk("button", "jpw-x", "✕");
    del.title = "停掉这条监视";
    del.onclick = function () { removeWatch(it.id); };
    top.appendChild(del);
    row.appendChild(top);

    row.appendChild(mk("div", "m", "目标：" + (it.target || "—")));

    var trig = it.last_change_at
      ? ("最近触发：" + it.last_change_at + (it.last_change_text ? "（" + it.last_change_text + "）" : ""))
      : "最近触发：还没触发过";
    row.appendChild(mk("div", "m", trig));

    // ★ 2026-11-23 用户要求：每条盯梢加「立即运行」「查看」——
    //   原来只能等下一个整点，用户想立刻知道「现在有没有变化」时毫无入口。
    var acts = mk("div", "jpw-acts");
    var runb = mk("button", "jpw-btn", "▶ 立即运行");
    runb.title = "现在就抓一次（联网 2~8 秒），立刻告诉你有没有变化";
    runb.onclick = function () { runWatchNow(it.id, runb); };
    var vw = mk("button", "jpw-btn", "查看");
    vw.title = "看它盯的是什么、最近抓到的内容、变化时间线";
    vw.onclick = function () { openWatchDetail(it.id); };
    acts.appendChild(runb);
    acts.appendChild(vw);
    row.appendChild(acts);

    var st = [];
    if (it.status) st.push(it.status);
    if (it.condition_text) st.push(it.condition_text);
    if (it.changes) st.push("共变了 " + it.changes + " 次");
    if (st.length) row.appendChild(mk("div", "m", st.join(" · ")));
    if (it.pending) row.appendChild(mk("div", "w", "● 有新变化，刚推到上面了"));

    // ★ 2026-11-23 用户要求：「增加一个立即运行以及查看的按钮」。
    //   立即运行 = 现在就跑一次（既比对又显示当前值，结果直接铺在卡片上）；
    //   查看     = 弹窗一屏看全（时间线 + 最近内容 + 下次运行）。
    var acts = mk("div", "jpw-acts");
    var run = mk("button", "jpw-btn", "▶ 立即运行");
    run.title = "现在就抓一次，和上次比一比";
    run.onclick = function () { runWatchNow(it, run, row); };
    var view = mk("button", "jpw-btn", "查看");
    view.title = "时间线 + 最近内容 + 下次运行";
    view.onclick = function () { openWatchDetail(it.id, it.name || it.target || "盯梢"); };
    acts.appendChild(run);
    acts.appendChild(view);
    row.appendChild(acts);
    return row;
  }

  function wrender() {
    var box = $("jpwList");
    if (!box) return;
    box.textContent = "";
    if (!witems.length) {
      box.appendChild(mk("div", "jp-hint",
        "现在没有在盯任何东西。说一句「帮我盯着某商品降价」就会开一条。"));
      return;
    }
    box.appendChild(mk("div", "jp-hint", "当前在盯 " + witems.length + " 项："));
    witems.forEach(function (it) { box.appendChild(wmkItem(it)); });
  }

  /** 后台推来的卡：交给 livecard.js 画（和回复气泡里是同一条渲染路径）。 */
  function wshowCards(cards) {
    var box = $("jpwCards");
    if (!box || !cards || !cards.length) return;
    var fresh = [];
    cards.forEach(function (c) {
      if (!c || typeof c !== "object") return;
      var k = String(c.ts || "") + "|" + String(c.title || "");
      if (wcardsSeen[k]) return;
      wcardsSeen[k] = 1;
      fresh.push(c);
    });
    if (!fresh.length) return;
    box.textContent = "";
    if (window.LiveCard && typeof window.LiveCard.renderLiveCard === "function") {
      try {
        var node = window.LiveCard.renderLiveCard(fresh, {
          onOpen: function (card, el) {
            if (el && el.classList) el.classList.toggle("open");
          }
        });
        if (node) box.appendChild(node);
      } catch (e) { /* 卡片渲染失败退化成文字，不影响列表 */ }
    }
    if (!box.childNodes.length) {
      fresh.forEach(function (c) {
        box.appendChild(mk("div", "jp-hint", "👁 " + (c.title || "盯梢提醒")));
      });
    }
    // 弹出来的卡上带监视 id → 顺手标记已读，免得下次再弹一遍
    fresh.forEach(function (c) {
      var p = (c && c.payload) || {};
      var wid = p["监视"] || p.watch_id || "";
      if (wid) ackWatch(wid);
    });
    toast(fresh[0].title ? ("盯梢有变化：" + fresh[0].title) : "盯梢有变化", "ok");
  }

  function ackWatch(id) {
    if (!id) return Promise.resolve(null);
    return jpost(API_WATCH_ACK, { id: id }).catch(function () { return null; });
  }

  function removeWatch(id) {
    if (!id) return Promise.resolve(null);
    return jpost(API_WATCH_DEL, { id: id }).then(function (d) {
      if (d && d.ok) { toast("已停掉这条监视"); refreshWatch(); loadArchived(); }
      else { toast((d && d.error) || "停不掉，稍后再试", "warn"); }
      return d;
    }).catch(function (e) {
      toast("停监视失败：" + (e && e.message ? e.message : e), "warn");
      return null;
    });
  }

  /** ★ 立即运行一次：既比对又显示当前值，结果直接铺在这张卡下面。 */
  function runWatchNow(it, btn, row) {
    if (!it || !it.id) return;
    var old = btn.textContent;
    btn.disabled = true;
    btn.textContent = "抓取中…";
    jpost(API_WATCH_RUN, { id: it.id }).then(function (d) {
      btn.disabled = false;
      btn.textContent = old;
      if (!d || !d.ok) { toast((d && d.error) || "运行失败", "warn"); return; }
      row.appendChild(mk("div", "m", "▶ 刚跑完：" + (d.answer || "") +
        (d.next_run_human && d.next_run_human !== "-" ? " · 下次 " + d.next_run_human : "")));
      if (d.preview) {
        var pre = mk("div", "jpwd-pre", d.preview);
        pre.style.maxHeight = "160px";
        row.appendChild(pre);
      }
      toast(d.changed ? "发现变化了" : (d.baseline ? "已记录基线" : "这次没变化"), "ok");
    }).catch(function (e) {
      btn.disabled = false;
      btn.textContent = old;
      toast("运行失败：" + (e && e.message ? e.message : e), "warn");
    });
  }

  var wdroot = null, wdmask = null;

  function wdetailBuild() {
    if (wdroot) return wdroot;
    injectCss();
    wdmask = mk("div", "jpwd-mask");
    wdmask.onclick = closeWatchDetail;
    document.body.appendChild(wdmask);
    wdroot = mk("section");
    wdroot.id = "jpwDetail";
    wdroot.hidden = true;
    var head = mk("div", "jpwd-head");
    head.appendChild(mk("span", "tt", "盯梢详情"));
    var x = mk("button", "jpw-x", "✕");
    x.onclick = closeWatchDetail;
    head.appendChild(x);
    wdroot.appendChild(head);
    var body = mk("div", "jpwd-body");
    body.id = "jpwdBody";
    wdroot.appendChild(body);
    document.body.appendChild(wdroot);
    return wdroot;
  }

  function closeWatchDetail() {
    if (wdroot) wdroot.hidden = true;
    if (wdmask) wdmask.hidden = true;
  }

  /** ★ 查看：时间线 + 最近内容 + 下次运行，一屏看全。 */
  function openWatchDetail(id, title) {
    if (!id) return;
    wdetailBuild();
    var body = $("jpwdBody");
    body.textContent = "";
    body.appendChild(mk("div", "jp-hint", "读取中…"));
    wdroot.hidden = false;
    wdmask.hidden = false;
    jget(API_WATCH_DETAIL + "?id=" + encodeURIComponent(id)).then(function (d) {
      body.textContent = "";
      if (!d || !d.ok) {
        body.appendChild(mk("div", "jp-hint", (d && d.error) || "取不到详情"));
        return;
      }
      var tt = wdroot.querySelector(".jpwd-head .tt");
      if (tt) tt.textContent = d.name || title || "盯梢详情";
      var kv = mk("div", "jpwd-kv");
      [["类型", d.kind_text || ""], ["周期", d.period || ""],
       ["下次运行", d.next_run_human || "-"],
       ["最近一次", d.last_run || "还没跑过"],
       ["状态", d.last_status || "—"],
       ["共变化", (d.timeline_count || 0) + " 次"]].forEach(function (p) {
        if (p[1]) kv.appendChild(mk("span", "", p[0] + "：" + p[1]));
      });
      body.appendChild(kv);
      if (d.query) {
        var s1 = mk("div", "jpwd-sec");
        s1.appendChild(mk("h4", "", "盯的目标"));
        s1.appendChild(mk("div", "jpwd-pre", d.query));
        body.appendChild(s1);
      }
      var s2 = mk("div", "jpwd-sec");
      s2.appendChild(mk("h4", "", "最近一次抓到的内容"));
      s2.appendChild(d.preview ? mk("div", "jpwd-pre", d.preview)
                               : mk("div", "jp-hint", "还没抓到内容（第一次运行后就有了）"));
      body.appendChild(s2);
      var s3 = mk("div", "jpwd-sec");
      s3.appendChild(mk("h4", "", "变化时间线（" + (d.timeline_count || 0) + " 条）"));
      var tl = d.timeline || [];
      if (!tl.length) {
        s3.appendChild(mk("div", "jp-hint", "还没有记录到变化。"));
      } else {
        var ul = mk("ul", "jpwd-tl");
        tl.slice().reverse().forEach(function (p) {
          var li = mk("li", "");
          var t = p.t || p.time || "";
          var add = (p.added != null ? p.added : "");
          var rem = (p.removed != null ? p.removed : "");
          li.appendChild(mk("div", "", t + (add !== "" ? "　+" + add + " / -" + rem : "")));
          var df = String(p.diff || p.preview || "").slice(0, 400);
          if (df) li.appendChild(mk("div", "jpwd-pre", df));
          ul.appendChild(li);
        });
        s3.appendChild(ul);
      }
      body.appendChild(s3);
    }).catch(function (e) {
      body.textContent = "";
      body.appendChild(mk("div", "jp-hint", "取详情失败：" + (e && e.message ? e.message : e)));
    });
  }

  /**
   * ★ 已删除但可恢复的任务（GET /api/schedule/archived）。
   *
   * 删除现在是**归档**而不是真删（时间线还留着），这里把它露出来，
   * 用户点错了当场能恢复，否则归档等于白做。
   */
  function loadArchived() {
    var box = $("jpwArch");
    if (!box) return Promise.resolve(null);
    return jget(API_SCHED_ARCHIVED).then(function (d) {
      var items = (d && Array.isArray(d.items)) ? d.items : [];
      box.textContent = "";
      if (!items.length) return d;
      box.appendChild(mk("div", "jp-hint",
        "已删除（" + items.length + " 条，点「恢复」就能找回来，时间线还在）："));
      items.slice(0, 20).forEach(function (a) {
        a = a || {};
        var row = mk("div", "jpw-item");
        var top = mk("div", "t");
        top.appendChild(mk("span", "", "🗑"));
        top.appendChild(mk("span", "n", a.name || a.id || "任务"));
        top.appendChild(mk("span", "s", a.period || ""));
        var b = mk("button", "jpw-btn", "恢复");
        b.onclick = function () { restoreArchived(a.id); };
        top.appendChild(b);
        row.appendChild(top);
        var meta = [];
        if (a.deleted_at) meta.push("删除于 " + a.deleted_at);
        if (a.timeline_count) meta.push("保留 " + a.timeline_count + " 条时间线");
        if (meta.length) row.appendChild(mk("div", "m", meta.join(" · ")));
        box.appendChild(row);
      });
      return d;
    }).catch(function () { return null; });
  }

  function restoreArchived(id) {
    if (!id) return Promise.resolve(null);
    return jpost(API_SCHED_RESTORE, { id: id }).then(function (d) {
      if (d && d.ok) { toast("已恢复"); refreshWatch(); loadArchived(); }
      else { toast((d && d.error) || "恢复失败", "warn"); }
      return d;
    }).catch(function (e) {
      toast("恢复失败：" + (e && e.message ? e.message : e), "warn");
      return null;
    });
  }

  // ★ 2026-11-23 用户要求：不用等到整点，点一下就抓一次。
  //   后端 watch_run_now 是同步的（2~8 秒），返回里直接给「有没有变」。
  function runWatchNow(id, btn) {
    if (!id) return Promise.resolve(null);
    var old = btn ? btn.textContent : "";
    if (btn) { btn.disabled = true; btn.textContent = "…抓取中"; }
    return jpost(API_WATCH_RUN, { id: id }).then(function (d) {
      if (btn) { btn.disabled = false; btn.textContent = old || "▶ 立即运行"; }
      if (!d || !d.ok) { toast((d && d.error) || "跑不动，稍后再试", "warn"); return null; }
      var msg = d.baseline ? "已记录基线（第一次抓取）" : String(d.answer || "跑完了");
      toast("「" + (d.name || "盯梢") + "」" + msg, d.changed ? "ok" : "");
      refreshWatch();
      return d;
    }).catch(function (e) {
      if (btn) { btn.disabled = false; btn.textContent = old || "▶ 立即运行"; }
      toast("立即运行失败：" + (e && e.message ? e.message : e), "warn");
      return null;
    });
  }

  // 详情弹窗：骨架只建一次，反复打开只换内容（省得每点一次塞一个 DOM）
  var droot = null, dmask = null;

  function closeWatchDetail() {
    if (droot) droot.hidden = true;
    if (dmask) dmask.hidden = true;
  }

  function buildDetail() {
    if (droot) return droot;
    injectCss();
    dmask = mk("div", "jpwd-mask");
    dmask.hidden = true;
    dmask.onclick = closeWatchDetail;
    document.body.appendChild(dmask);
    droot = mk("section");
    droot.id = "jpwDetail";
    droot.hidden = true;
    var head = mk("div", "jpwd-head");
    var tt = mk("span", "tt", "盯梢详情");
    tt.id = "jpwdTitle";
    head.appendChild(tt);
    var x = mk("button", "jp-x", "✕");
    x.title = "关闭";
    x.onclick = closeWatchDetail;
    head.appendChild(x);
    droot.appendChild(head);
    var body = mk("div", "jpwd-body");
    body.id = "jpwdBody";
    droot.appendChild(body);
    document.body.appendChild(droot);
    return droot;
  }

  /** 拉一次面板数据（列表 + 待弹卡）。 */
  function refreshWatch() {
    return jget(API_WATCH + "?drain=1").then(function (d) {
      if (!d || !d.ok) {
        if (d && d.error) toast("盯梢面板取不到：" + d.error, "warn");
        return null;
      }
      witems = Array.isArray(d.items) ? d.items : [];
      wrender();
      wshowCards(d.cards || []);
      return d;
    }).catch(function (e) {
      // 轮询失败不弹 toast（网络抖一下就弹会很吵），只在控制台留痕
      if (window.console) console.log("[jobplan] 盯梢轮询失败", e);
      return null;
    });
  }

  function wbuild() {
    if (wroot) return wroot;
    injectCss();
    wroot = mk("section");
    wroot.id = "jpwPanel";
    wroot.hidden = true;

    var head = mk("div", "jp-head");
    head.appendChild(mk("span", "jp-title", "👁 盯梢"));
    var x = mk("button", "jp-x", "✕");
    x.title = "收起";
    x.onclick = closeWatch;
    head.appendChild(x);
    wroot.appendChild(head);

    var body = mk("div", "jp-body");

    var ta = mk("input", "jp-in");
    ta.id = "jpwIn";
    ta.style.minHeight = "0";
    ta.placeholder = "一句话：帮我盯着某商品降价（有变化就告诉我）";
    body.appendChild(ta);

    var row = mk("div", "jp-row");
    var go = mk("button", "jp-go", "👁 开始盯");
    go.id = "jpwGo";
    go.onclick = function () { addWatch(ta.value || ""); };
    row.appendChild(go);
    body.appendChild(row);
    body.appendChild(mk("div", "jp-hint",
      "本地文件 / 目录：有变化就提示；关键词 / 网址：定时联网比对。有变化会自己弹卡。"));

    var cards = mk("div", "jpw-cards");
    cards.id = "jpwCards";
    body.appendChild(cards);

    var list = mk("div");
    list.id = "jpwList";
    list.appendChild(mk("div", "jp-hint", "正在读取监视列表…"));
    body.appendChild(list);

    wroot.appendChild(body);
    document.body.appendChild(wroot);

    ta.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); addWatch(ta.value || ""); }
    });
    return wroot;
  }

  function addWatch(text) {
    text = String(text || "").trim();
    if (!text) { toast("说一句要盯什么，例如：帮我盯着某商品降价"); return Promise.resolve(null); }
    var go = $("jpwGo");
    if (go) { go.disabled = true; go.textContent = "…登记中"; }
    return jpost(API_WATCH, { text: text }).then(function (d) {
      if (go) { go.disabled = false; go.textContent = "👁 开始盯"; }
      if (!d || !d.ok) {
        toast((d && d.error) || "没能建起来", "warn");
        return null;
      }
      var w = d.watch || {};
      toast((d.reused ? "已经在盯了：" : "开始盯：") +
        (w.name || w.target || text) + (w.period ? "（" + w.period + "）" : ""), "ok");
      var ta = $("jpwIn");
      if (ta) ta.value = "";
      refreshWatch();
      return d;
    }).catch(function (e) {
      if (go) { go.disabled = false; go.textContent = "👁 开始盯"; }
      toast("建监视失败：" + (e && e.message ? e.message : e), "warn");
      return null;
    });
  }

  function openWatch() {
    wbuild();
    wroot.hidden = false;
    refreshWatch();
    if (wtimer) clearInterval(wtimer);
    // 4 秒一轮：后台变化通常以分钟计，太快只是白耗电；但也不能太慢，
    // 用户在等「它自己回来告诉我」的那一下。
    wtimer = setInterval(refreshWatch, 4000);
    var ta = $("jpwIn");
    if (ta) { try { ta.focus(); } catch (e) { /* 焦点拿不到不影响用 */ } }
  }

  function closeWatch() {
    if (wroot) wroot.hidden = true;
    if (wtimer) { clearInterval(wtimer); wtimer = null; }
  }

  function toggleWatch() {
    if (wroot && !wroot.hidden) closeWatch();
    else openWatch();
  }

  // ---------- 开始 ----------
  function start(goal) {
    goal = String(goal || "").trim();
    if (!goal) {
      toast("先说一句要办的事");
      open();
      return Promise.resolve(null);
    }
    build();
    open();
    var go = $("jpGo");
    if (go) { go.disabled = true; go.textContent = "…正在拆步"; }
    return jpost(API_PLAN, { goal: goal }).then(function (d) {
      if (!d || !d.ok) {
        if (go) { go.disabled = false; go.textContent = "▶ 开始"; }
        toast((d && d.error) || "任务没建起来", "warn");
        return null;
      }
      var job = d.job || {};
      renderJobProgress(job);
      if (job.id) pollJob(job.id);
      return job;
    }).catch(function (e) {
      if (go) { go.disabled = false; go.textContent = "▶ 开始"; }
      toast("开工失败：" + (e && e.message ? e.message : e), "warn");
      return null;
    });
  }

  // ---------- 入口（侧栏顶栏那一排快捷按钮里加一个） ----------
  function mountButton() {
    if ($("btnJobPlan")) return;
    var host = $("topQuick");
    if (!host) {
      // 顶栏还没渲染出来（app.js 在建）—— 稍后再试，别把入口丢了
      setTimeout(mountButton, 400);
      return;
    }
    var b = document.createElement("button");
    b.type = "button";
    b.className = "tq-btn";
    b.id = "btnJobPlan";
    b.title = "一句话交代整件事：自动拆步跑完，成品打包下载";
    var ico = document.createElement("span");
    ico.className = "tq-ico";
    ico.textContent = "🧩";
    var txt = document.createElement("span");
    txt.className = "tq-txt";
    txt.textContent = "订整件事";
    b.appendChild(ico);
    b.appendChild(txt);
    b.onclick = toggle;
    host.appendChild(b);

    // 「盯梢」入口和上面那个并排：用户原话是「说一句盯着 XX」，
    // 所以按钮名就叫盯梢，不叫「监视任务」这种系统术语。
    if (!$("btnWatch")) {
      var wb = document.createElement("button");
      wb.type = "button";
      wb.className = "tq-btn";
      wb.id = "btnWatch";
      wb.title = "说一句「盯着 XX」就后台长期监视，有变化自己弹卡告诉你";
      var wico = document.createElement("span");
      wico.className = "tq-ico";
      wico.textContent = "👁";
      var wtxt = document.createElement("span");
      wtxt.className = "tq-txt";
      wtxt.textContent = "盯梢";
      wb.appendChild(wico);
      wb.appendChild(wtxt);
      wb.onclick = toggleWatch;
      host.appendChild(wb);
    }
  }

  function bind() {
    mountButton();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }

  window.JobPlan = {
    open: open, close: close, toggle: toggle, start: start,
    // 聊天区 / 别处也可以直接把一句话递进来跑
    run: start,
    renderJobProgress: renderJobProgress,
    poll: pollJob,
    stopPoll: stopPoll,
    // 盯梢：别的入口（聊天里的「盯着 XX」按钮等）也能直接把这句话递进来
    watchPanel: toggleWatch,
    openWatch: openWatch,
    closeWatch: closeWatch,
    addWatch: addWatch,
    refreshWatch: refreshWatch,
    get watches() { return witems; },
    get job() { return curJob; }
  };
})();


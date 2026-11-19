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
    ".jp-warn{margin-top:8px;font-size:11.5px;color:#e8883a;white-space:pre-wrap;}";

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
    get job() { return curJob; }
  };
})();


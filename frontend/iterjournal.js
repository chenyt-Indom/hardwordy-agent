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
  // 施工单 8.5：账本 / 尺子两条独立序列（后端只给原始点，没有「综合分」字段）
  var API_D = "/api/iter/dual?limit=120";
  var API_R = "/api/selfiter/revive";
  // 体检接口（iterpanel 的 health 块里带「历史记录不一致」计数）
  var API_P = "/api/iter/panel?days=7";
  var timer = null;
  var openFlag = false;
  var lastData = null;
  var expanded = {};          // tid -> true 展开详情
  var audit = null;           // {count, claims, scanned, items:[{path,t}]}
  var auditAt = 0;            // 上次取回检计数的时间（ms），30 秒内不重复请求
var bench = null;           // 施工单 8.5：尺子（项目外固定基准）最新读数
  var dual = null;            // 施工单 8.5：{ledger:[…], bench:[…]} 两条独立序列

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
    ".ij-empty{opacity:.6;font-size:11.5px;margin-top:6px;}" +
    ".ij-led{font-size:11.5px;opacity:.74;margin-bottom:8px;}" +
    ".ij-dual{margin:2px 0 6px;}" +
    ".ij-dual svg{width:100%;height:96px;display:block;}" +
    ".ij-dual .lg{display:flex;gap:12px;font-size:10.5px;opacity:.7;margin-top:2px;}" +
    ".ij-dual .lg i{display:inline-block;width:10px;height:2px;margin-right:4px;" +
    "vertical-align:middle;border-radius:1px;}" +
    // 🧾 改动证据卡（2026-11-24）：针对哪个真实失败 + 计数 + 回归前后对比
    ".ij-evc{margin-top:5px;border:1px solid rgba(255,255,255,.12);border-radius:8px;" +
    "padding:5px 8px;font-size:11px;line-height:1.6;background:rgba(255,255,255,.035);}" +
    ".ij-evc .row{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}" +
    ".ij-evc .row.t{opacity:.72;font-size:10.5px;}" +
    ".ij-evc .fk{color:#8ab4f8;font-weight:600;}" +
    ".ij-evc .cnt{color:#e8b23a;font-weight:600;}" +
    ".ij-evc .rg{color:#8fce9b;}.ij-evc .rg.bad{color:#e5534b;}" +
    ".ij-evc .rg.na{color:#8a929e;}" +
    ".ij-evc .ghost{color:#e5534b;font-weight:600;}";

  function injectCss() {
    if ($("ijCss")) { return; }
    var s = document.createElement("style");
    s.id = "ijCss";
    s.textContent = CSS;
    (document.head || document.documentElement).appendChild(s);
  }

  var LABEL = {
    running: "在跑", waiting: "排队", done: "完成", verified: "已验证",
    failed: "失败", cancelled: "已终止", error: "异常",
    // ★ 2026-11-26：blocked = 本轮被前置拦截（提案缺证据 / 动作还没批准），
    //   它**不是执行失败**，单独给个标签，别再显示成红卡「失败」。
    blocked: "已拦截"
  };
  function badgeCls(st) {
    if (st === "running") { return "ij-badge run"; }
    if (st === "failed" || st === "error") { return "ij-badge fail"; }
    if (st === "done" || st === "verified") { return "ij-badge ok"; }
    if (st === "waiting") { return "ij-badge wait"; }
    if (st === "blocked") { return "ij-badge wait"; }
    return "ij-badge";
  }

  // 🧾 改动证据卡（2026-11-24 用户要求）
  //
  // 对应痛点：幽灵修复 —— 模型凭印象编一个「失败」，然后加护栏去修它，
  // 结果是成功率 44.7%、回滚 27 次。用户要看到的不是「我改了什么」，
  // 而是「**凭什么改**」：这次改动是因为账本里 fail_kind=X 真实出现 N 次，
  // 以及改完之后回归通过数有没有掉。
  //
  // 数据全部来自后端，前端**不造数**：
  //   t.evidence.items[]       每个带证据的动作（fail_kind / count / phantom / text）
  //   t.evidence.regression    {before, after, delta, verdict, text}
  // 读不到就不画那一行 —— 补 0 会把「没跑」显示成「一条都没过」。
  function evidenceItems(t) {
    var ev = (t && t.evidence) || {};
    var items = (ev.items && ev.items.length) ? ev.items : [];
    if (!items.length) {
      // 后端没给（老版本 / 还没跑到提案闸门）→ 从动作清单里自己摘，
      // 保证「有 fail_kind 的改动」不会因为接口字段缺了就静默不显示证据。
      (t && t.actions ? t.actions : []).forEach(function (a) {
        var fk = String((a && a.fail_kind) || "");
        var ph = !!(a && a.phantom);
        if (!fk && !ph) { return; }
        items.push({
          fail_kind: fk,
          count: (a.count === undefined ? null : a.count),
          phantom: ph,
          title: String((a && a.title) || ""),
          text: String((a && a.evidence_text) || "")
        });
      });
    }
    return items;
  }

  function _cntText(c) {
    return (c === null || c === undefined) ? "—" : String(c);
  }

  function renderEvidenceCard(t) {
    var items = evidenceItems(t);
    var ev = (t && t.evidence) || {};
    var reg = (ev && ev.regression) ? ev.regression : null;
    if (!items.length && !reg) { return null; }

    var box = mk("div", "ij-evc");
    box.appendChild(mk("span", "row t", "改动证据（针对的真实失败 / 回归前后）"));
    items.slice(0, 3).forEach(function (x) {
      var row = mk("span", "row");
      if (x.phantom) {
        var g = mk("span", "ghost", "⛔ 幽灵修复 ");
        row.appendChild(g);
        row.appendChild(mk("span", "fk", x.fail_kind || "?"));
        row.appendChild(mk("span", "cnt", " 计数 " + _cntText(x.count)));
        row.appendChild(document.createTextNode("（提案已被拒，不执行）"));
      } else {
        row.appendChild(document.createTextNode("🎯 针对 "));
        row.appendChild(mk("span", "fk", x.fail_kind || "?"));
        row.appendChild(document.createTextNode(" · "));
        row.appendChild(mk("span", "cnt", _cntText(x.count) + " 次"));
        if (x.text) { row.title = x.text; }
      }
      box.appendChild(row);
    });
    if (items.length > 3) {
      box.appendChild(mk("span", "row t", "…还有 " + (items.length - 3) + " 条带证据的动作"));
    }
    if (reg) {
      var cls = "row rg";
      if (reg.verdict === "regressed") { cls = "row rg bad"; }
      if (reg.verdict === "unknown") { cls = "row rg na"; }
      var r = mk("span", cls, "🧪 " + (reg.text || "回归：没有证据"));
      r.title = "同一组 quick 回归用例，改动前后各跑一次，只比通过数；" +
                "两个数并排看，不相加、不加权、不算综合分。";
      box.appendChild(r);
    }
    return box;
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

    // 🧾 改动证据卡：不用展开详情就能看到「凭什么改 + 改完回归怎么样」
    var evc = renderEvidenceCard(t);
    if (evc) { box.appendChild(evc); }

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
    if (stx === "failed" || stx === "error" || stx === "cancelled"
        || stx === "blocked") {
      box.appendChild(reviveBtn({
        kind: (stx === "cancelled" ? "cancelled"
               : (stx === "blocked" ? "blocked" : "failed")),
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
          // ★ 2026-11-24：用后端返回的真实任务名，别再用卡片上的旧标题 ——
          //   否则「拉起的是 A，跑起来变成 B」，用户会以为拉错了任务。
          var nm = String((d && d.title) || c.title || "").slice(0, 20);
          var extra = (d && d.auto_approved) ? ("（已代批 " + d.auto_approved + " 条待批准）") : "";
          toast("已拉起：" + nm + extra, "ok");
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

  // 取「历史记录不一致」计数（2026-11-24）：体检接口 health.history_claims。
  // 只读接口，30 秒缓存一次，不跟着 2 秒轮询去反复打后端。
  function refreshAudit() {
    var now = Date.now();
    if (audit && (now - auditAt) < 30000) { return Promise.resolve(audit); }
    return jget(API_P).then(function (d) {
      var h = (d && d.health) || {};
      var a = h.history_claims || (d && d.history_claims) || null;
      if (a && typeof a === "object") { audit = a; auditAt = now; }
      // ⚠️ 施工单第 8 章：尺子只并排显示，**不与账本相加/加权/平均**。
      //    读不到就是 null → 界面显示「—」，绝不补 0（补 0 会把关机当成退化）。
      bench = (d && d.bench && typeof d.bench === "object") ? d.bench : null;
      return audit;
    }).catch(function () { return audit; });
  }

  // 施工单 8.5：尺子一行。首屏能看到「现在多能打」，而不是「攒了多少家当」。
  function benchNode() {
    if (!bench) { return null; }
    var row = mk("div", "ij-stat");
    var p = Number(bench.pass), tot = Number(bench.total);
    var txt = "尺子（项目外固定基准）：";
    txt += (tot > 0 ? (isFinite(p) ? p : "—") + " / " + tot : "—");
    if (bench.pct !== undefined && bench.pct !== null) {
      txt += "（" + bench.pct + "%）";
    }
    var age = bench._age_h;
    if (typeof age === "number" && age >= 0) {
      txt += age < 1 ? "　刚跑过" : ("　" + Math.round(age) + " 小时前");
    }
    row.textContent = txt;
    row.style.fontWeight = "600";
    row.title = "施工单第 8 章：账本是施工日志（累计只增），尺子是体检报告（会抖但不说谎）。\n" +
                "两个数并排看，永不相加。";
    return row;
  }

  // 施工单 8.5 第二行小字：账本（降级为辅助信息，但保留可解释性）
  function ledgerNode() {
    if (!dual) { return null; }
    var now = dual.ledger_now || null;
    var row = mk("div", "ij-led");
    if (!now) {
      row.textContent = "账本（累计只增）：—";
      row.title = "还没有综合分轮。读不到就显示「—」，不补 0、不估算。";
      return row;
    }
    var v = Number(now.v);
    var arr = dual.ledger || [];
    var prev = arr.length >= 2 ? Number(arr[arr.length - 2].v) : null;
    var d = (prev !== null && isFinite(prev) && isFinite(v)) ? (v - prev) : null;
    row.textContent = "账本（累计只增）：" + (isFinite(v) ? v.toFixed(2) : "—") +
      (d === null ? "" : "（本轮 " + (d >= 0 ? "+" : "") + d.toFixed(2) + "）");
    row.title = "施工单 8.5：账本 = 攒了多少家当，只增不减，" +
                "不能用来看「是否越改越强」。";
    return row;
  }

  // 施工单 8.5 曲线图：两条线 + **左右两个独立 Y 轴**。
  // 归一化就是变相加权 —— 所以两条线各按各自的 min/max 画，谁也不许被拉平。
  function dualChart() {
    if (!dual) { return null; }
    var L = dual.ledger || [], B = dual.bench || [];
    if (!L.length && !B.length) {
      var e = mk("div", "ij-dual");
      e.innerHTML = '<div class="ij-empty">账本与尺子都还没有数据（不画点，不补 0）。</div>';
      return e;
    }
    var W = 356, H = 96, x0 = 34, x1 = W - 36, y0 = 8, y1 = H - 14;
    function mm(a, k) {
      var f = [];
      for (var i = 0; i < a.length; i++) {
        var v = Number(a[i][k]);
        if (isFinite(v)) { f.push(v); }
      }
      if (!f.length) { return [0, 1]; }
      var lo = Math.min.apply(null, f), hi = Math.max.apply(null, f);
      if (hi - lo < 1e-9) { hi = lo + 1; }
      return [lo, hi];
    }
    function line(a, k, rng, color) {
      var n = a.length, d = "", on = false;
      for (var i = 0; i < n; i++) {
        var v = Number(a[i][k]);
        if (!isFinite(v)) { on = false; continue; }
        var x = n < 2 ? (x0 + x1) / 2 : x0 + (x1 - x0) * i / (n - 1);
        var t = (v - rng[0]) / (rng[1] - rng[0]);
        var y = y1 - (y1 - y0) * Math.max(0, Math.min(1, t));
        d += (on ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1) + " ";
        on = true;
      }
      return d ? '<path d="' + d + '" fill="none" stroke="' + color +
        '" stroke-width="2"/>' : "";
    }
    var lr = mm(L, "v");
    var tot = (dual.bench_now && Number(dual.bench_now.total)) || 15;
    var br = [0, tot > 0 ? tot : 15];
    var svg = '<svg viewBox="0 0 ' + W + " " + H + '">' +
      '<line x1="' + x0 + '" y1="' + y1 + '" x2="' + x1 + '" y2="' + y1 +
      '" stroke="#2a3140"/><line x1="' + x0 + '" y1="' + y0 + '" x2="' + x0 +
      '" y2="' + y1 + '" stroke="#2a3140"/><line x1="' + x1 + '" y1="' + y0 +
      '" x2="' + x1 + '" y2="' + y1 + '" stroke="#2a3140"/>' +
      '<text x="2" y="' + (y0 + 8) + '" fill="#7b8496" font-size="9">' + lr[1] + "</text>" +
      '<text x="2" y="' + (y1 + 2) + '" fill="#7b8496" font-size="9">' + lr[0] + "</text>" +
      '<text x="' + (x1 + 4) + '" y="' + (y0 + 8) + '" fill="#7b8496" font-size="9">' + br[1] + "</text>" +
      '<text x="' + (x1 + 4) + '" y="' + (y1 + 2) + '" fill="#7b8496" font-size="9">0</text>' +
      line(L, "v", lr, "#e8b23a") + line(B, "pass", br, "#6ea8fe") + "</svg>" +
      '<div class="lg"><span><i style="background:#e8b23a"></i>账本 ' + L.length +
      ' 点</span><span><i style="background:#6ea8fe"></i>尺子 ' + B.length + ' 点</span></div>';
    var box = mk("div", "ij-dual");
    box.innerHTML = svg;
    box.title = "左轴 = 账本（累计），右轴 = 尺子（0~" + br[1] +
      " 题）。两条线各自缩放，**不相加、不加权、不平均、不归一化**。";
    return box;
  }

  // 施工单 8.5：取两条序列（只读；取不到就是 null → 显示「—」，不画点）
  function refreshDual() {
    return jget(API_D).then(function (d) {
      dual = (d && d.ok !== false) ? d : null;
      return dual;
    }).catch(function () { return dual; });
  }

  function auditNode() {
    if (!audit) { return null; }
    var n = audit.count;
    if (n === undefined || n === null) { n = audit.mismatch; }
    n = Number(n) || 0;
    var row = mk("div", "ij-stat");
    row.textContent = "历史记录不一致：" + n + " 条";
    if (n > 0) { row.style.color = "#e8823a"; }
    var items = audit.items || [];
    if (items.length) {
      row.title = "声称新建但文件不存在：\n" + items.slice(0, 8).map(function (x) {
        return "· " + String(x.path || "") + (x.t ? "（" + x.t + "）" : "");
      }).join("\n");
    } else {
      row.title = "扫了最近 " + (audit.scanned || 0) + " 条历史记录，声称新建的文件都存在";
    }
    return row;
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
    // 施工单 8.5：首屏第一眼是尺子（「我多能打」），账本降级为第二行
    var _b0 = benchNode();
    if (_b0) { body.appendChild(_b0); }
    var c = d.counts || {};
    var stat = mk("div", "ij-stat");
    stat.textContent = "状态：" + (d.state || "空闲") +
      "　在跑 " + (c.running || 0) + " · 排队 " + (c.queue || 0) +
      " · 历史 " + (c.history || 0) + "　本轮 " + (d.budget || 0) +
      "/" + (d.max_rounds ? d.max_rounds : "不限");
    var ln = ledgerNode();
    if (ln) { body.appendChild(ln); }
    var ch = dualChart();
    if (ch) { body.appendChild(ch); }
    // 施工单 8.5 的优先级：尺子 > 账本 > 两条曲线 > 迭代状态
    body.appendChild(stat);
    var an = auditNode();
    if (an) { body.appendChild(an); }
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
    // 体检计数和日志并行取：计数失败不影响日志主体（各自 catch）
    return Promise.all([jget(API_J), refreshAudit(), refreshDual()])
      .then(function (x) { render(x[0]); return x[0]; })
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

  window.IterJournal = { open: open, close: close, toggle: toggle, reload: load,
    renderEvidenceCard: renderEvidenceCard };
})();

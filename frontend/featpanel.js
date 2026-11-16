/* =====================================================================
   【待批准】+【推荐新功能】（2026-11-16 用户要求）

   【待批准】：所有还没执行的自动迭代任务，按计划分组，每条可以
     · 写备注微调（备注会一路带到执行时，模型能看到并照做）
     · 单条「批准并执行」/「不做」
     · 顶部还能「全部批准」
   【推荐新功能】：模型想出来的、能提高体验的**全新功能**（不是优化）。
     · 「✨ 现在想一批」→ 它先想「你哪件事最费劲」，再构思功能
     · 每条分三层展示：他为什么需要 / 你会看到什么 / 大致怎么做
     · 能写备注微调；批准后立刻排进执行队列真去写代码

   两张卡片都是右侧抽屉（.fr-drawer），不改主布局。
   独立成文件是为了不往 15000 行的 app.js 里再塞 600 行 —— 作用域也隔离。
   ===================================================================== */
(function () {
  "use strict";

  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  async function api(url, body, method) {
    const m = method || (body === undefined ? "GET" : "POST");
    const noBody = (m === "GET" || m === "DELETE") || body === undefined;
    const opt = noBody ? { method: m } :
      { method: m, headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) };
    try {
      const r = await fetch(url, opt);
      return await r.json();
    } catch (e) {
      return { ok: false, error: "请求失败：" + (e && e.message ? e.message : e) };
    }
  }

  // ---- 轻提示：app.js 的 say() 不是全局的，这里自带一个
  let toastEl = null, toastTimer = 0;
  function toast(text, kind) {
    if (!toastEl) {
      toastEl = document.createElement("div");
      toastEl.className = "fr-toast";
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = String(text || "");
    toastEl.className = "fr-toast show" + (kind === "bad" ? " bad" : kind === "good" ? " good" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.className = "fr-toast"; }, 3200);
  }

  // ------------------------------------------------------------ 面板骨架
  function ensurePanel(id, title, sub) {
    let mask = document.getElementById(id + "Mask");
    if (!mask) {
      mask = document.createElement("div");
      mask.className = "fr-mask";
      mask.id = id + "Mask";
      mask.hidden = true;
      document.body.appendChild(mask);
    }
    let box = document.getElementById(id);
    if (!box) {
      box = document.createElement("div");
      box.className = "fr-drawer";
      box.id = id;
      box.hidden = true;
      box.innerHTML =
        '<div class="fr-head">' +
          '<div class="fr-h-txt"><b>' + esc(title) + '</b>' +
            '<span class="fr-sub">' + esc(sub || "") + '</span></div>' +
          '<button type="button" class="fr-x" title="关闭（Esc）">✕</button>' +
        '</div>' +
        '<div class="fr-tools"></div>' +
        '<div class="fr-body"><div class="fr-empty">加载中…</div></div>';
      document.body.appendChild(box);
      mask.addEventListener("click", function () { closePanel(id); });
      box.querySelector(".fr-x").addEventListener("click", function () { closePanel(id); });
    }
    return box;
  }

  function openPanel(id) {
    const box = document.getElementById(id);
    const mask = document.getElementById(id + "Mask");
    if (!box) return;
    if (mask) mask.hidden = false;
    box.hidden = false;
    requestAnimationFrame(function () { box.classList.add("in"); });
  }

  function closePanel(id) {
    const box = document.getElementById(id);
    const mask = document.getElementById(id + "Mask");
    if (box) {
      box.classList.remove("in");
      setTimeout(function () { box.hidden = true; }, 220);
    }
    if (mask) mask.hidden = true;
  }

  function setBody(id, html) {
    const box = document.getElementById(id);
    const b = box && box.querySelector(".fr-body");
    if (b) b.innerHTML = html;
  }

  function setTools(id, html) {
    const box = document.getElementById(id);
    const t = box && box.querySelector(".fr-tools");
    if (t) t.innerHTML = html;
  }

  function setSub(id, text) {
    const box = document.getElementById(id);
    const s = box && box.querySelector(".fr-sub");
    if (s) s.textContent = text || "";
  }

  function cardOf(panelId, attr, val) {
    return document.querySelector("#" + panelId + " .fr-card[" + attr + '="' +
      String(val).replace(/"/g, '\\"') + '"]');
  }

  function noteOf(card) {
    const ta = card && card.querySelector(".fr-note");
    return ta ? ta.value : "";
  }

  // ============================================================ 待批准
  const PEND = "frPending";
  let pendData = null;

  async function loadPending() {
    setBody(PEND, '<div class="fr-empty">加载中…</div>');
    const d = await api("/api/selfiter/pending");
    pendData = d || { groups: [], total: 0 };
    renderPending();
  }

  function pendActionHtml(pid, a) {
    const review = a.status === "need_review";
    const tag = review
      ? '<span class="fr-tag fr-tag-warn">⚠ 待人工复核</span>'
      : '<span class="fr-tag">待批准</span>';
    const risk = String(a.risk || "mid");
    const rtag = risk === "high" ? '<span class="fr-tag fr-tag-bad">高风险</span>'
      : risk === "low" ? '' : '<span class="fr-tag fr-tag-mid">中风险</span>';
    return '<div class="fr-card" data-pid="' + esc(pid) + '" data-aid="' + esc(a.id) + '">' +
      '<div class="fr-c-head"><b>' + esc(a.title || a.id) + '</b>' + tag + rtag + '</div>' +
      (a.detail ? '<div class="fr-c-detail">' + esc(a.detail) + '</div>' : '') +
      (a.error ? '<div class="fr-c-err">' + esc(a.error) + '</div>' : '') +
      (a.files && a.files.length
        ? '<div class="fr-c-files">📄 ' + a.files.map(esc).join(" · ") + '</div>' : '') +
      '<textarea class="fr-note" rows="2" placeholder="备注（可选）：想怎么微调这条？' +
        '写在这儿，执行时会按你的话做">' + esc(a.note || '') + '</textarea>' +
      '<div class="fr-c-btns">' +
        '<button type="button" class="fr-btn fr-ok" data-act="ok">✓ 批准并执行</button>' +
        '<button type="button" class="fr-btn fr-no" data-act="no">✕ 不做</button>' +
      '</div>' +
    '</div>';
  }

  function renderPending() {
    const d = pendData || {};
    const groups = d.groups || [];
    setSub(PEND, "共 " + (d.total || 0) + " 条待执行 · 按计划分组 · 备注会带进执行");
    setTools(PEND,
      '<button type="button" class="fr-tbtn" data-act="reload">🔄 刷新</button>' +
      '<button type="button" class="fr-tbtn fr-tbtn-main" data-act="all">✓ 全部批准</button>' +
      '<span class="fr-tip">「待人工复核」= 被护栏拦下的（改太频繁），批准后自动解除</span>');
    if (!groups.length) {
      setBody(PEND, '<div class="fr-empty">现在没有待批准的任务 🎉<br>' +
        '<span class="fr-dim">它自动改代码时会先攒到这里，等你点头。</span></div>');
      return;
    }
    setBody(PEND, groups.map(function (g) {
      return '<div class="fr-group">' +
        '<div class="fr-g-head">' +
          '<div class="fr-g-sum">' + esc(g.summary || "(无摘要)") + '</div>' +
          '<div class="fr-g-meta">' + esc(g.ts) + ' · ' + g.n + ' 条' +
            (g.n_review ? ' · <b class="fr-warn">' + g.n_review + ' 待复核</b>' : '') +
          '</div>' +
        '</div>' +
        g.actions.map(function (a) { return pendActionHtml(g.plan_id, a); }).join("") +
      '</div>';
    }).join(""));
  }

  async function pendApprove(pid, aid) {
    const note = noteOf(cardOf(PEND, "data-aid", aid));
    toast("批准中…");
    // 被熔断拦下的先放行（不是 need_review 时后端返回 error，忽略即可）
    await api("/api/selfiter/force", { id: pid, action_ids: [aid], execute: false });
    const r = await api("/api/selfiter/approve", { id: pid, action_ids: [aid], note: note });
    if (!r || !r.ok) { toast((r && r.error) || "批准失败", "bad"); return; }
    const e = await api("/api/selfiter/enqueue", { id: pid, source: "manual" });
    toast((e && e.ok) ? "已批准并提交执行，去聊天区看进度"
      : "已批准，但提交队列失败：" + ((e && e.error) || "未知"),
      (e && e.ok) ? "good" : "bad");
    await loadPending();
    refreshDots();
  }

  async function pendReject(pid, aid) {
    const note = noteOf(cardOf(PEND, "data-aid", aid));
    const r = await api("/api/selfiter/reject", { id: pid, action_ids: [aid], note: note });
    if (!r || !r.ok) { toast((r && r.error) || "拒绝失败", "bad"); return; }
    toast("已标记不做", "good");
    await loadPending();
    refreshDots();
  }

  async function pendApproveAll() {
    if (!pendData || !(pendData.groups || []).length) { toast("没有待批准的任务"); return; }
    if (!confirm("要把这 " + (pendData.total || 0) + " 条全部批准并执行吗？\n" +
                 "（会先备份、语法校验，改坏了自动回滚）")) return;
    toast("正在整批批准…");
    let ok = 0, bad = 0;
    for (const g of (pendData.groups || [])) {
      await api("/api/selfiter/force", { id: g.plan_id, execute: false });
      const r = await api("/api/selfiter/approve", { id: g.plan_id });
      if (r && r.ok) {
        ok += (r.approved || 0);
        await api("/api/selfiter/enqueue", { id: g.plan_id, source: "manual" });
      } else { bad++; }
    }
    toast("整批批准完成：成功 " + ok + " 条" + (bad ? "，" + bad + " 个计划失败" : ""),
      bad ? "bad" : "good");
    await loadPending();
    refreshDots();
  }

  // ============================================================ 推荐新功能
  const FEAT = "frFeat";
  let featData = null;

  async function loadFeat() {
    setBody(FEAT, '<div class="fr-empty">加载中…</div>');
    const d = await api("/api/featradar/items");
    featData = d || { items: [] };
    renderFeat();
  }

  function scoreChip(label, v, cls) {
    const n = Math.max(1, Math.min(10, parseInt(v, 10) || 5));
    return '<span class="fr-score ' + (cls || "") + '" title="' + esc(label) + ' ' + n + '/10">' +
      '<i>' + esc(label) + '</i><b>' + n + '</b></span>';
  }

  function featCardHtml(it) {
    const st = it.state || "pending";
    const tag = st === "pending" ? '<span class="fr-tag">待你决定</span>'
      : st === "done" ? '<span class="fr-tag fr-tag-ok">已提交执行</span>'
      : st === "rejected" ? '<span class="fr-tag fr-tag-off">已拒绝</span>'
      : '<span class="fr-tag fr-tag-ok">已批准</span>';
    const editable = (st === "pending" || st === "rejected");
    return '<div class="fr-card fr-feat" data-fid="' + esc(it.id) + '">' +
      '<div class="fr-c-head"><b>' + esc(it.title || "（未命名）") + '</b>' + tag + '</div>' +
      '<div class="fr-scores">' +
        scoreChip("价值", it.value) +
        scoreChip("新颖", it.novelty, "fr-s-n") +
        scoreChip("可行", it.feasibility, "fr-s-f") +
      '</div>' +
      (it.scene ? '<div class="fr-kv"><span class="fr-k">他为什么需要</span>' +
        '<span class="fr-v">' + esc(it.scene) + '</span></div>' : '') +
      (it.idea ? '<div class="fr-kv"><span class="fr-k">你会看到什么</span>' +
        '<span class="fr-v">' + esc(it.idea) + '</span></div>' : '') +
      (it.how ? '<div class="fr-kv"><span class="fr-k">大致怎么做</span>' +
        '<span class="fr-v">' + esc(it.how) + '</span></div>' : '') +
      (it.paper ? '<div class="fr-c-files">📄 参考：' + esc(it.paper) + '</div>' : '') +
      '<textarea class="fr-note" rows="2" placeholder="备注（可选）：想怎么改这个功能？' +
        '写在这儿，批准后会按你的话做"' + (editable ? '' : ' disabled') + '>' +
        esc(it.note || '') + '</textarea>' +
      (editable
        ? '<div class="fr-c-btns">' +
            '<button type="button" class="fr-btn fr-ok" data-act="ok">✓ 批准，去做</button>' +
            '<button type="button" class="fr-btn fr-no" data-act="no">✕ 不要</button>' +
            '<button type="button" class="fr-btn fr-save" data-act="save">💾 只存备注</button>' +
          '</div>'
        : '<div class="fr-c-btns">' +
            '<button type="button" class="fr-btn fr-no" data-act="del">🗑 删掉这条</button>' +
          '</div>') +
    '</div>';
  }

  function renderFeat() {
    const d = featData || {};
    const items = d.items || [];
    const pend = items.filter(function (x) { return x.state === "pending"; }).length;
    setSub(FEAT, pend ? "有 " + pend + " 条新功能等你决定" : "还没有待决定的新功能");
    setTools(FEAT,
      '<button type="button" class="fr-tbtn fr-tbtn-main" data-act="think">✨ 现在想一批</button>' +
      '<button type="button" class="fr-tbtn" data-act="reload">🔄 刷新</button>' +
      '<span class="fr-tip">先想「你哪件事最费劲」→ 再构思功能。约 30~60 秒</span>');
    if (!items.length) {
      setBody(FEAT, '<div class="fr-empty">还没有推荐的新功能。<br>' +
        '<span class="fr-dim">点上面的「✨ 现在想一批」让它现想 ——<br>' +
        '它会先想你现在哪件事最费劲，再想什么功能能省掉这个动作。</span></div>');
      return;
    }
    setBody(FEAT, items.map(featCardHtml).join(""));
  }

  async function featThink(btn) {
    const old = btn ? btn.textContent : "";
    if (btn) { btn.disabled = true; btn.textContent = "正在想…"; }
    setBody(FEAT, '<div class="fr-empty">它正在想：你现在哪件事最费劲 → ' +
      '什么功能能省掉这个动作…<br><span class="fr-dim">大约 30~60 秒</span></div>');
    const r = await api("/api/featradar/think", {});
    if (btn) { btn.disabled = false; btn.textContent = old || "✨ 现在想一批"; }
    if (!r || !r.ok) {
      setBody(FEAT, '<div class="fr-empty">没想出结果：' +
        esc((r && r.error) || "未知错误") + '</div>');
      return;
    }
    if (r.skipped) toast("没生成：" + r.skipped);
    else toast("想到 " + (r.added || 0) + " 个新功能", (r.added ? "good" : ""));
    await loadFeat();
    refreshDots();
  }

  async function featApprove(fid) {
    const note = noteOf(cardOf(FEAT, "data-fid", fid));
    toast("已批准，正在排进执行队列…");
    const r = await api("/api/featradar/approve", { id: fid, note: note, execute: true });
    if (!r || !r.ok) { toast("批准失败：" + ((r && r.error) || "未知"), "bad"); return; }
    toast(r.submitted ? "已提交执行，去聊天区看它改代码的进度" : "已批准（排队中）", "good");
    await loadFeat();
    refreshDots();
  }

  async function featReject(fid) {
    const note = noteOf(cardOf(FEAT, "data-fid", fid));
    const r = await api("/api/featradar/reject", { id: fid, note: note });
    if (!r || !r.ok) { toast("操作失败：" + ((r && r.error) || "未知"), "bad"); return; }
    toast("已标记不要", "good");
    await loadFeat();
    refreshDots();
  }

  async function featSaveNote(fid) {
    const note = noteOf(cardOf(FEAT, "data-fid", fid));
    const r = await api("/api/featradar/" + encodeURIComponent(fid), { note: note }, "PATCH");
    if (!r || !r.ok) { toast("保存失败：" + ((r && r.error) || "未知"), "bad"); return; }
    toast("备注已存", "good");
  }

  async function featDelete(fid) {
    if (!confirm("删掉这条推荐？")) return;
    const r = await api("/api/featradar/" + encodeURIComponent(fid), undefined, "DELETE");
    if (!r || !r.ok) { toast("删除失败：" + ((r && r.error) || "未知"), "bad"); return; }
    await loadFeat();
    refreshDots();
  }

  // ============================================================ 红点
  async function refreshDots() {
    try {
      const p = await api("/api/selfiter/pending");
      const pd = document.getElementById("pendingDot");
      if (pd) {
        const n = (p && p.total) || 0;
        pd.hidden = !n;
        pd.title = n ? n + " 条待批准" : "";
      }
    } catch (e) { /* 忽略 */ }
    try {
      const f = await api("/api/featradar/stats");
      const fd = document.getElementById("featDot");
      if (fd) {
        const n = (f && f.pending) || 0;
        fd.hidden = !n;
        fd.title = n ? n + " 条新功能等你决定" : "";
      }
    } catch (e) { /* 忽略 */ }
  }

  // ============================================================ 绑定
  const pendBtn = document.getElementById("pendingBtn");
  if (pendBtn) {
    pendBtn.addEventListener("click", function (e) {
      e.preventDefault();
      ensurePanel(PEND, "📋 待批准", "所有还没执行的自动迭代任务");
      openPanel(PEND);
      loadPending();
    });
  }

  const featBtn = document.getElementById("featBtn");
  if (featBtn) {
    featBtn.addEventListener("click", function (e) {
      e.preventDefault();
      ensurePanel(FEAT, "💡 推荐新功能", "模型想出来的、能提高体验的全新功能");
      openPanel(FEAT);
      loadFeat();
    });
  }

  document.addEventListener("click", function (e) {
    const t = e.target && e.target.closest ? e.target.closest("[data-act]") : null;
    if (!t) return;

    if (t.closest("#" + PEND)) {
      const act = t.getAttribute("data-act");
      if (act === "reload") { loadPending(); return; }
      if (act === "all") { pendApproveAll(); return; }
      const card = t.closest(".fr-card");
      if (!card) return;
      const pid = card.getAttribute("data-pid");
      const aid = card.getAttribute("data-aid");
      if (act === "ok") { pendApprove(pid, aid); return; }
      if (act === "no") { pendReject(pid, aid); return; }
      return;
    }

    if (t.closest("#" + FEAT)) {
      const act = t.getAttribute("data-act");
      if (act === "reload") { loadFeat(); return; }
      if (act === "think") { featThink(t); return; }
      const card = t.closest(".fr-card");
      if (!card) return;
      const fid = card.getAttribute("data-fid");
      if (act === "ok") { featApprove(fid); return; }
      if (act === "no") { featReject(fid); return; }
      if (act === "save") { featSaveNote(fid); return; }
      if (act === "del") { featDelete(fid); return; }
      return;
    }
  });

  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    [PEND, FEAT].forEach(function (id) {
      const box = document.getElementById(id);
      if (box && !box.hidden) closePanel(id);
    });
  });

  refreshDots();
  setInterval(function () { if (!document.hidden) refreshDots(); }, 60000);
})();

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
    // 健康度卡片固定贴在面板顶部：每次刷新待批准都顺手拉一次，数字不会过期
    loadHealth(PEND);
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
      // 用户要求「放到待批准并说明原因以及解决方案」：
      //   为什么 = a.why（模型给的原因），怎么改 = a.detail（解决方案）。
      (a.why ? '<div class="fr-kv"><span class="fr-k">为什么</span>' +
        '<span class="fr-v">' + esc(a.why) + '</span></div>' : '') +
      (a.detail ? '<div class="fr-kv"><span class="fr-k">怎么改</span>' +
        '<span class="fr-v">' + esc(a.detail) + '</span></div>' : '') +
      (a.error ? '<div class="fr-c-err">' + esc(a.error) + '</div>' : '') +
      (a.files && a.files.length
        ? '<div class="fr-c-files">📄 ' + a.files.map(esc).join(" · ") + '</div>' : '') +
      '<textarea class="fr-note" rows="2" maxlength="800" ' +
        'placeholder="备注（可选）：想怎么微调这条？' +
        '写在这儿，执行时会按你的话做（最多 800 字）">' + esc(a.note || '') + '</textarea>' +
      '<div class="fr-c-btns">' +
        '<button type="button" class="fr-btn fr-ok" data-act="ok">✓ 批准并执行</button>' +
        '<button type="button" class="fr-btn fr-no" data-act="no">✕ 不做</button>' +
      '</div>' +
    '</div>';
  }

  function renderPending() {
    const d = pendData || {};
    const groups = d.groups || [];
    setSub(PEND, "共 " + (d.total || 0) + " 条待执行 · 只含改进/修复 · 按计划分组");
    setTools(PEND,
      '<button type="button" class="fr-tbtn" data-act="reload">🔄 刷新</button>' +
      '<button type="button" class="fr-tbtn fr-tbtn-main" data-act="all">✓ 全部批准</button>' +
      '<span class="fr-tip">「待人工复核」= 被护栏拦下的（改太频繁），批准后自动解除。' +
      '新功能不在这里，去「💡 推荐新功能」里批准</span>');
    if (!groups.length) {
      setBody(PEND, '<div class="fr-empty">现在没有待批准的改进任务 🎉<br>' +
        '<span class="fr-dim">信任模式关闭时，它想改代码会先攒到这里等你点头；<br>' +
        '信任模式开着时，这里只会有被护栏拦下的高风险改动 —— 它不会把整批待批准<br>' +
        '一口气批准掉。想出来的「新功能」在「💡 推荐新功能」里单独列，永远要你点头。</span></div>');
      return;
    }
    setBody(PEND, groups.map(function (g) {
      return '<div class="fr-group">' +
        '<div class="fr-g-head">' +
          '<div class="fr-g-sum">' + esc(g.summary || "(无摘要)") + '</div>' +
          '<div class="fr-g-meta">' + esc(g.ts) + ' · ' + g.n + ' 条' +
            (g.trigger_zh ? ' · ' + esc(g.trigger_zh) : '') +
            (g.n_review ? ' · <b class="fr-warn">' + g.n_review + ' 待复核</b>' : '') +
          '</div>' +
        '</div>' +
        // 「为什么这条没自动执行」——信任模式关闭时用户最需要看到这句
        (g.hold_reason ? '<div class="fr-g-hold">' + esc(g.hold_reason) + '</div>' : '') +
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
      // 只批这一组里**列出来的**动作 id。不传 id 会把同一计划里的新功能动作
      // 一起批掉 —— 那正好违反「两个类别分开」。
      const ids = (g.actions || []).map(function (a) { return a.id; });
      await api("/api/selfiter/force", { id: g.plan_id, action_ids: ids, execute: false });
      const r = await api("/api/selfiter/approve", { id: g.plan_id, action_ids: ids });
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
  // 2026-11-16 用户要求：「待批准里不能有新功能的批准，两个类别得分开」。
  // 所以这里单独拉 /api/selfiter/pending_features —— 自动反思里想出来的新功能
  // （origin=="feature"）以及 featradar 造的计划，全部归到这张面板。
  let featPendData = null;

  async function loadFeatPending() {
    const d = await api("/api/selfiter/pending_features");
    featPendData = d || { groups: [], total: 0 };
  }

  function featPendCardHtml(pid, a) {
    const review = a.status === "need_review";
    return '<div class="fr-card" data-ppid="' + esc(pid) + '" data-faid="' + esc(a.id) + '">' +
      '<div class="fr-c-head"><b>' + esc(a.title || a.id) + '</b>' +
        '<span class="fr-tag">待你决定</span>' +
        (review ? '<span class="fr-tag fr-tag-warn">⚠ 待人工复核</span>' : '') +
      '</div>' +
      // ★ 2026-11-16 用户问「为什么有些推荐的没有价值/新颖/可行」：
      //   这张卡片原来压根没渲染这三项（数据也没从 feat_radar 带过来）。
      //   现在补齐，未评估的显示「—」而不是假分数。
      '<div class="fr-scores">' +
        scoreChip("价值", a.value) +
        scoreChip("新颖", a.novelty, "fr-s-n") +
        scoreChip("可行", a.feasibility, "fr-s-f") +
      '</div>' +
      (a.why ? '<div class="fr-kv"><span class="fr-k">为什么</span>' +
        '<span class="fr-v">' + esc(a.why) + '</span></div>' : '') +
      (a.detail ? '<div class="fr-kv"><span class="fr-k">怎么做</span>' +
        '<span class="fr-v">' + esc(a.detail) + '</span></div>' : '') +
      (a.files && a.files.length
        ? '<div class="fr-c-files">📄 ' + a.files.map(esc).join(" · ") + '</div>' : '') +
      '<textarea class="fr-note" rows="2" placeholder="备注（可选）：想怎么微调这个功能？' +
        '写在这儿，执行时会按你的话做">' + esc(a.note || '') + '</textarea>' +
      '<div class="fr-c-btns">' +
        '<button type="button" class="fr-btn fr-ok" data-act="pok">✓ 批准并执行</button>' +
        '<button type="button" class="fr-btn fr-no" data-act="pno">✕ 不要</button>' +
      '</div>' +
    '</div>';
  }

  function featPendHtml() {
    const d = featPendData || {};
    const groups = d.groups || [];
    if (!groups.length) return "";
    return '<div class="fr-sec">📋 待批准的新功能 · ' + (d.total || 0) + ' 条' +
        '<span class="fr-sec-tip">这些是自动反思里想出来的新功能，批准后才动手</span></div>' +
      groups.map(function (g) {
        return '<div class="fr-group">' +
          '<div class="fr-g-head">' +
            '<div class="fr-g-sum">' + esc(g.summary || "(无摘要)") + '</div>' +
            '<div class="fr-g-meta">' + esc(g.ts) + ' · ' + g.n + ' 条' +
              (g.trigger_zh ? ' · ' + esc(g.trigger_zh) : '') + '</div>' +
          '</div>' +
          (g.hold_reason ? '<div class="fr-g-hold">' + esc(g.hold_reason) + '</div>' : '') +
          (g.actions || []).map(function (a) { return featPendCardHtml(g.plan_id, a); }).join("") +
        '</div>';
      }).join("");
  }

  async function featPendApprove(pid, aid) {
    const card = cardOf(FEAT, "data-faid", aid);
    const note = noteOf(card);
    toast("批准中…");
    await api("/api/selfiter/force", { id: pid, action_ids: [aid], execute: false });
    const r = await api("/api/selfiter/approve", { id: pid, action_ids: [aid], note: note });
    if (!r || !r.ok) { toast((r && r.error) || "批准失败", "bad"); return; }
    const e = await api("/api/selfiter/enqueue", { id: pid, source: "manual" });
    toast((e && e.ok) ? "已批准并提交执行，去聊天区看进度"
      : "已批准，但提交队列失败：" + ((e && e.error) || "未知"),
      (e && e.ok) ? "good" : "bad");
    await loadFeat();
    refreshDots();
    renderBadge();
  }

  async function featPendReject(pid, aid) {
    const note = noteOf(cardOf(FEAT, "data-faid", aid));
    const r = await api("/api/selfiter/reject", { id: pid, action_ids: [aid], note: note });
    if (!r || !r.ok) { toast((r && r.error) || "拒绝失败", "bad"); return; }
    toast("已标记不要", "good");
    await loadFeat();
    refreshDots();
    renderBadge();
  }

  async function loadFeat() {
    setBody(FEAT, '<div class="fr-empty">加载中…</div>');
    const d = await api("/api/featradar/items");
    featData = d || { items: [] };
    await loadFeatPending();
    renderFeat();
  }

  // ---- 角标（2026-11-20 用户要求）
  // 顶栏「推荐新功能」按钮上的未读数。数据源是 /api/featradar/recommend
  // 返回的 unread 计数（后端 unread_count() 算的是 state=="pending" 的条数）。
  // 设计要点：
  //   · 数字为 0 时隐藏角标，不制造视觉噪音；
  //   · 接口没就绪 / 读盘失败时静默失败，角标保持隐藏 —— 不能因为一个
  //     可选接口 404 就在控制台刷红，更不能挡住页面；
  //   · 采纳 / 拒绝 / 删除之后立刻重算，用户能马上看到数字减一。
  // 2026-11-16：原来这里单独读 /api/featradar/recommend 的 unread，
  // 而顶栏角标走的是 refreshDots（featradar.pending + pending_features），
  // 两套算法不同 → 侧栏和顶栏会显示两个不一样的数字。
  // 现在统一到 refreshDots 一处取数，renderBadge 只做兼容别名。
  async function renderBadge() {
    return refreshDots();
  }
  window.__refreshSuggestBadge = renderBadge;

  // 2026-11-16：原来 parseInt(v)||5 —— 缺失/0 一律变成 5，于是「模型没打分」
  // 在界面上显示成「价值 5 新颖 5 可行 5」，是个假分数，用户以为评过了。
  // 现在 0/缺失/非法一律显示「未评估」（灰底），不编数字。
  function scoreChip(label, v, cls) {
    const raw = parseInt(v, 10);
    const has = Number.isFinite(raw) && raw >= 1;
    const n = has ? Math.max(1, Math.min(10, raw)) : 0;
    return '<span class="fr-score ' + (cls || "") + (has ? "" : " fr-score-na") +
      '" title="' + esc(label) + (has ? ' ' + n + '/10' : '：未评估') + '">' +
      '<i>' + esc(label) + '</i><b>' + (has ? n : "—") + '</b></span>';
  }

  function featCardHtml(it) {
    const st = it.state || "pending";
    // 2026-11-16：原来 done 一律显示「已提交执行」，而旧版本把「提交进队列但
    // 一条都没跑」也标成 done，用户看到的就是「已提交执行」却什么都没发生。
    // 现在 done 只表示执行器真跑过；submitted 才是「刚交上去、还在跑」。
    const tag = st === "pending" ? '<span class="fr-tag">待你决定</span>'
      : st === "done" ? '<span class="fr-tag fr-tag-ok">✅ 已改完</span>'
      : st === "submitted" ? '<span class="fr-tag fr-tag-run">⏳ 执行中…</span>'
      : st === "failed" ? '<span class="fr-tag fr-tag-bad">❌ 没改成</span>'
      : st === "rejected" ? '<span class="fr-tag fr-tag-off">已拒绝</span>'
      : '<span class="fr-tag fr-tag-ok">已批准</span>';
    const editable = (st === "pending" || st === "rejected" || st === "failed");
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
      '<textarea class="fr-note" rows="2" maxlength="800" ' +
        'placeholder="备注（可选）：想怎么改这个功能？' +
        '写在这儿，批准后会按你的话做（最多 800 字）"' +
        (editable ? '' : ' disabled') + '>' +
        esc(it.note || '') + '</textarea>' +
      (st === "failed" && it.error
        ? '<div class="fr-c-files fr-err">失败原因：' + esc(String(it.error).slice(0, 200)) + '</div>'
        : '') +
      (st === "done" && it.files && it.files.length
        ? '<div class="fr-c-files">改了 ' + it.files.length + ' 个文件：' +
          esc(it.files.join("、")) + '</div>'
        : '') +
      (st === "submitted"
        ? '<div class="fr-c-files">已交给执行器，正在改代码 —— 跑完这里会自动变成结果，' +
          '去聊天区也能看到它的每一步。</div>'
        : '') +
      (editable
        ? '<div class="fr-c-btns">' +
            '<button type="button" class="fr-btn fr-ok" data-act="ok">' +
              (st === "failed" ? '🔁 重新执行' : '✓ 批准，去做') + '</button>' +
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
    const pendPlan = (featPendData && featPendData.total) || 0;
    const n = pend + pendPlan;
    setSub(FEAT, n ? "有 " + n + " 条新功能等你决定（其中 " + pendPlan + " 条来自自动反思）"
      : "还没有待决定的新功能");
    setTools(FEAT,
      '<button type="button" class="fr-tbtn fr-tbtn-main" data-act="think">✨ 现在想一批</button>' +
      '<button type="button" class="fr-tbtn" data-act="reload">🔄 刷新</button>' +
      '<span class="fr-tip">先想「你哪件事最费劲」→ 再构思功能。约 30~60 秒。' +
      '改进/修复类不在这里，去「📋 待批准」</span>');
    const pendHtml = featPendHtml();
    if (!items.length && !pendHtml) {
      setBody(FEAT, '<div class="fr-empty">还没有推荐的新功能。<br>' +
        '<span class="fr-dim">点上面的「✨ 现在想一批」让它现想 ——<br>' +
        '它会先想你现在哪件事最费劲，再想什么功能能省掉这个动作。</span></div>');
      return;
    }
    setBody(FEAT, pendHtml + items.map(featCardHtml).join(""));
    // 有「执行中」的就盯到出结果，省得用户手动刷新
    if (items.some(function (x) { return x.state === "submitted"; })) watchRun();
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
    renderBadge();
  }

  async function featApprove(fid) {
    const note = noteOf(cardOf(FEAT, "data-fid", fid));
    toast("已批准，正在排进执行队列…");
    const r = await api("/api/featradar/approve", { id: fid, note: note, execute: true });
    if (!r || !r.ok) { toast("批准失败：" + ((r && r.error) || "未知"), "bad"); return; }
    toast(r.submitted ? "已交给执行器，正在改代码…" : "已批准（排队中）", "good");
    await loadFeat();
    refreshDots();
    renderBadge();
    watchRun();
  }

  // 2026-11-16：用户点批准后最想知道「到底跑了没」。执行是异步的，
  // 这里盯到所有 submitted 变成 done/failed 就停，最多盯 8 分钟。
  let _watchT = null;
  function watchRun() {
    if (_watchT) return;
    const t0 = Date.now();
    _watchT = setInterval(async function () {
      const items = (featData && featData.items) || [];
      const running = items.filter(function (x) { return x.state === "submitted"; });
      if (!running.length || Date.now() - t0 > 8 * 60 * 1000) {
        clearInterval(_watchT); _watchT = null;
        return;
      }
      await loadFeat();
      refreshDots();
    }, 5000);
  }

  async function featReject(fid) {
    const note = noteOf(cardOf(FEAT, "data-fid", fid));
    const r = await api("/api/featradar/reject", { id: fid, note: note });
    if (!r || !r.ok) { toast("操作失败：" + ((r && r.error) || "未知"), "bad"); return; }
    toast("已标记不要", "good");
    await loadFeat();
    refreshDots();
    renderBadge();
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
    renderBadge();
  }

  // ---- 对外暴露：让顶栏按钮 / app.js 能直接打开这张抽屉
  // 用户要求「功能有计划时就在上面那个按钮中提示有推荐新功能」——
  // 顶栏那颗按钮点开就该是这张完整的推荐面板，而不是另做一个占位浮层。
  window.FeatPanel = {
    open: function () {
      ensurePanel(FEAT, "💡 推荐新功能", "模型想出来的、能提高体验的全新功能");
      openPanel(FEAT);
      loadFeat();
    },
    close: function () { closePanel(FEAT); },
    refreshBadge: renderBadge,
  };

  // ============================================================ 对外 API
  // 2026-11-16 用户要求：把 renderPending / renderRecommend / refreshBadge
  // 三个渲染入口显式暴露出去，方便 app.js / 顶栏按钮 / 其它模块直接调用。
  //   · renderPending   —— 画「待批准」卡片（标题+理由+备注输入框+批准/拒绝）
  //   · renderRecommend —— 画「推荐新功能」卡片（标题+why+how+采纳按钮）
  //   · refreshBadge    —— 拉两个接口更新红点计数
  // 三者都做了「面板未创建时自动 ensurePanel」的兜底，避免调用方先手动
  // 建面板；同时保持幂等，重复调用不会重复建 DOM。
  function ensurePendingPanel() {
    ensurePanel(PEND, "📋 待批准", "所有还没执行的自动迭代任务");
  }
  function ensureFeatPanel() {
    ensurePanel(FEAT, "💡 推荐新功能", "模型想出来的、能提高体验的全新功能");
  }

  // renderPending：画待审卡片。数据源是 /api/selfiter/pending。
  // 备注输入框绑定 approve 的 note 字段，上限 800 字（见 pendActionHtml）。
  async function renderPendingPanel() {
    ensurePendingPanel();
    await loadPending();
  }

  // renderRecommend：画推荐卡片。数据源是 /api/featradar/items。
  // 每条卡片展示 标题 + why(scene) + how + 采纳按钮，备注同样上限 800 字。
  async function renderRecommendPanel() {
    ensureFeatPanel();
    await loadFeat();
  }

  // refreshBadge：拉 /api/selfiter/pending 与 /api/featradar/stats 两个接口，
  // 更新顶栏红点计数。与 refreshDots 同源，但这里额外把「推荐新功能」按钮
  // 上的 suggestBadge 也一起刷新，保证两个角标始终一致。
  async function refreshBadgeAll() {
    await refreshDots();
    await renderBadge();
  }

  window.FeatPanel.renderPending = renderPendingPanel;
  window.FeatPanel.renderRecommend = renderRecommendPanel;
  window.FeatPanel.refreshBadge = refreshBadgeAll;
  // 2026-11-21：健康度卡片（自迭代面板顶部固定展示）
  //   · renderHealthCard(dict?) —— 画卡片并返回卡片数据（纯消费后端 health_snapshot）
  //   · refreshHealth()         —— 重新拉 /api/selfiter/health 再画
  window.FeatPanel.renderHealthCard = renderHealthCard;
  window.FeatPanel.refreshHealth = function () { return loadHealth(PEND); };
  // 兼容旧调用：window.__refreshSuggestBadge 仍指向 renderBadge
  window.__refreshSuggestBadge = renderBadge;

  // ============================================================ 执行时间线
  // 2026-11-16：读 get_stages 结果，渲染成时间线卡片。
  // 复用 .if-flag 样式（不新增布局），只把每个 stage 的标题/状态/耗时/说明
  // 排成一条竖向时间线。数据源：/api/selfiter/get_stages（按 plan_id 取）。
  const STAGES = "frStages";
  let stagesData = null;
  let stagesPlanId = null;

  async function loadStages(planId) {
    stagesPlanId = planId || stagesPlanId;
    setBody(STAGES, '<div class="fr-empty">加载中…</div>');
    const url = "/api/selfiter/get_stages" +
      (stagesPlanId ? "?id=" + encodeURIComponent(stagesPlanId) : "");
    const d = await api(url);
    stagesData = d || { stages: [] };
    renderStages();
  }

  function stageStateTag(st) {
    const s = String((st && st.state) || "").toLowerCase();
    if (s === "done" || s === "ok" || s === "success")
      return '<span class="if-flag if-flag-ok">✓ 完成</span>';
    if (s === "running" || s === "doing")
      return '<span class="if-flag if-flag-run">⏳ 进行中</span>';
    if (s === "failed" || s === "error")
      return '<span class="if-flag if-flag-bad">✕ 失败</span>';
    if (s === "skipped")
      return '<span class="if-flag if-flag-off">— 跳过</span>';
    return '<span class="if-flag">' + esc(s || "待执行") + '</span>';
  }

  function fmtDur(ms) {
    const n = Number(ms);
    if (!isFinite(n) || n <= 0) return "";
    if (n < 1000) return n + "ms";
    if (n < 60000) return (n / 1000).toFixed(1) + "s";
    const m = Math.floor(n / 60000);
    const s = Math.round((n % 60000) / 1000);
    return m + "m" + (s ? s + "s" : "");
  }

  function stageCardHtml(st, i, total) {
    const title = st.title || st.name || st.stage || ("步骤 " + (i + 1));
    const dur = fmtDur(st.duration_ms != null ? st.duration_ms : st.ms);
    const detail = st.detail || st.summary || st.message || "";
    const err = st.error || "";
    const last = i === total - 1;
    return '<div class="fr-card fr-stage" data-sidx="' + i + '">' +
      '<div class="fr-c-head"><b>' + esc(title) + '</b>' + stageStateTag(st) +
        (dur ? '<span class="if-flag">' + esc(dur) + '</span>' : '') +
      '</div>' +
      (detail ? '<div class="fr-c-detail">' + esc(detail) + '</div>' : '') +
      (err ? '<div class="fr-c-err">' + esc(err) + '</div>' : '') +
      (last ? '' : '<div class="fr-stage-line"></div>') +
    '</div>';
  }

  function renderStages() {
    const d = stagesData || {};
    const stages = d.stages || d.items || [];
    const pid = d.plan_id || stagesPlanId || "";
    setSub(STAGES, pid ? ("计划 " + pid + " · " + stages.length + " 个阶段")
      : (stages.length + " 个阶段"));
    setTools(STAGES,
      '<button type="button" class="fr-tbtn" data-act="reload">🔄 刷新</button>' +
      '<span class="fr-tip">数据来自 get_stages，按执行顺序排成时间线</span>');
    if (!stages.length) {
      setBody(STAGES, '<div class="fr-empty">还没有执行阶段记录。<br>' +
        '<span class="fr-dim">任务开始执行后，这里会按时间线显示每一步。</span></div>');
      return;
    }
    setBody(STAGES, '<div class="fr-timeline">' +
      stages.map(function (st, i) { return stageCardHtml(st, i, stages.length); }).join("") +
      '</div>');
  }

  // ============================================================ 失败分类看板
  const FAILK = "frFailKinds";
  let failkData = null;

  async function loadFailKinds() {
    setBody(FAILK, '<div class="fr-empty">加载中…</div>');
    const d = await api("/api/actlog/top_fail_kinds?n=10");
    failkData = d || { items: [] };
    renderFailKinds();
  }

  function renderFailKinds() {
    const d = failkData || {};
    const items = d.items || [];
    setSub(FAILK, items.length ? "共 " + items.length + " 类失败 · 按次数降序" : "暂无失败记录");
    setTools(FAILK,
      '<button type="button" class="fr-tbtn" data-act="reload">🔄 刷新</button>' +
      '<span class="fr-tip">数据来自 actlog 里已有的 fail_kind 字段，纯聚合不调模型</span>');
    if (!items.length) {
      setBody(FAILK, '<div class="fr-empty">还没有失败记录 🎉<br>' +
        '<span class="fr-dim">自动迭代失败时会记下 fail_kind，这里按次数排给你看。</span></div>');
      return;
    }
    const max = items.reduce(function (m, x) { return Math.max(m, x.count || 0); }, 1);
    setBody(FAILK, items.map(function (x) {
      const pct = Math.max(4, Math.round((x.count || 0) / max * 100));
      return '<div class="fr-card fr-failk">' +
        '<div class="fr-c-head"><b>' + esc(x.fail_kind) + '</b>' +
          '<span class="fr-tag fr-tag-bad">' + (x.count || 0) + ' 次</span></div>' +
        '<div class="fr-bar"><i style="width:' + pct + '%"></i></div>' +
      '</div>';
    }).join(""));
  }

  // ============================================================ 健康度卡片
  // 2026-11-21 用户要求：他连续多轮追问「自迭代到底能不能正常运转」——
  // 说明成功率 / 回滚 / 熔断 / 最近一轮触发名 / 幽灵修复这几项硬指标
  // 一直没有固定视图，每次都得靠追问。这里做成一张**固定在自迭代面板顶部**
  // 的卡片（插在 .fr-tools 和 .fr-body 之间，切面板内容也不会被刷掉），
  // 数据源 /api/selfiter/health（后端 self_iterate.health_snapshot()）。
  let healthData = null;

  function healthNum(v) { const n = Number(v); return isFinite(n) ? n : 0; }

  // healthCardData：纯函数，dict → 卡片数据。不碰 DOM，
  // 所以「能不能消费 health_snapshot() 返回的 dict」是可直接断言的。
  function healthCardData(h) {
    const d = (h && typeof h === "object" && h.health && typeof h.health === "object")
      ? h.health : (h || {});
    // 后端给的是 0~1 的比例；万一是百分数（>1）也接受，统一归一化到 0~1
    let rate = healthNum(d.success_rate);
    const attempts = Math.max(0, Math.round(healthNum(
      d.attempts === undefined ? healthNum(d.ok_count) + healthNum(d.fail_count) : d.attempts)));
    // 老版本接口只给了 ok_count/fail_count（没有 success_rate）时现算一次，
    // 口径与后端一致：分母是「真执行过的条数」，不含批准/拒绝这类事件。
    if (d.success_rate === undefined && attempts > 0) {
      rate = healthNum(d.ok_count) / attempts;
    }
    if (rate > 1) rate = rate / 100;
    rate = Math.max(0, Math.min(1, rate));
    const total = healthNum(d.total);
    const hasData = d.has_data === undefined
      ? (attempts > 0 || total > 0) : !!d.has_data;
    const phantom = Math.max(0, Math.round(healthNum(d.phantom_fix_count)));
    const rollbacks = Math.max(0, Math.round(healthNum(d.rollbacks)));
    const breaks = Math.max(0, Math.round(healthNum(d.circuit_breaks)));
    const trigCode = String(d.last_round_trigger || "");
    const trigZh = String(d.last_round_trigger_zh || trigCode || "还没有跑过一轮");
    let level = String(d.level || "");
    if (["good", "warn", "bad", "unknown"].indexOf(level) < 0) {
      // 后端没给 level 时按同样的口径自己判一次（老版本接口也能用）
      level = !hasData ? "unknown"
        : (rate >= 0.8 && !phantom && !breaks) ? "good"
          : (rate < 0.6 || phantom >= 3) ? "bad" : "warn";
    }
    const tagMap = {
      good: { cls: "fr-tag-ok", text: "✓ 运转正常" },
      warn: { cls: "fr-tag-warn", text: "⚠ 需要盯一眼" },
      bad: { cls: "fr-tag-bad", text: "✕ 状态偏差" },
      unknown: { cls: "fr-tag-off", text: "— 暂无数据" },
    };
    return {
      level: level,
      hasData: hasData,
      attempts: attempts,
      total: total,
      title: "自迭代健康度",
      tag: tagMap[level] || tagMap.unknown,
      metrics: [
        { key: "success_rate",
          label: "成功率",
          value: hasData ? (rate * 100).toFixed(1) + "%" : "—",
          tone: !hasData ? "plain" : rate >= 0.8 ? "ok" : rate < 0.6 ? "bad" : "mid" },
        { key: "rollbacks", label: "回滚", value: String(rollbacks),
          tone: rollbacks ? "mid" : "ok" },
        { key: "circuit_breaks", label: "熔断", value: String(breaks),
          tone: breaks ? "mid" : "ok" },
        { key: "phantom_fix_count", label: "幽灵修复", value: String(phantom),
          tone: phantom ? "bad" : "ok" },
        { key: "last_round_trigger", label: "最近一轮触发", value: trigZh,
          tone: "plain" },
      ],
      trigger: trigCode,
      triggerZh: trigZh,
      hint: String(d.hint || ""),
      ts: String(d.last_round_ts || ""),
    };
  }

  function healthCardHtml(data) {
    const d = data || healthCardData(null);
    const metrics = (d.metrics || []).map(function (m) {
      return '<div class="fr-metric fr-tone-' + esc(m.tone || "plain") + '">' +
        '<i>' + esc(m.label) + '</i><b>' + esc(m.value) + '</b></div>';
    }).join("");
    return '<div class="fr-card fr-health-card">' +
      '<div class="fr-c-head"><b>' + esc(d.title) + '</b>' +
        '<span class="fr-tag ' + esc(d.tag.cls) + '">' + esc(d.tag.text) + '</span>' +
        // 成功率旁边永远带着样本量：3 次里中 2 次的 66% 和 300 次里的 66% 不是一回事
        (d.attempts ? '<span class="fr-tag fr-tag-off">样本 ' + d.attempts + ' 次</span>' : '') +
        (d.ts ? '<span class="fr-tag fr-tag-off">' + esc(d.ts) + '</span>' : '') +
      '</div>' +
      '<div class="fr-metrics">' + metrics + '</div>' +
      (d.hint ? '<div class="fr-hint">' + esc(d.hint) + '</div>' : '') +
    '</div>';
  }

  // 卡片槽位：固定在面板顶部（tools 之下、body 之上），
  // 这样 setBody() 每次重刷列表都不会把它刷掉。
  function healthSlot(panelId) {
    const box = document.getElementById(panelId || PEND);
    if (!box) return null;
    let el = box.querySelector(".fr-health");
    if (!el) {
      el = document.createElement("div");
      el.className = "fr-health";
      const body = box.querySelector(".fr-body");
      if (body && body.parentNode === box) box.insertBefore(el, body);
      else box.appendChild(el);
    }
    return el;
  }

  // renderHealthCard：画卡片并**返回卡片数据**（dict → 卡片，可被断言）。
  // 三种调用都支持：renderHealthCard() / renderHealthCard(dict) / renderHealthCard(panelId)
  function renderHealthCard(h, panelId) {
    let src = h, pid = panelId;
    if (typeof h === "string") { pid = h; src = null; }
    if (!src || typeof src !== "object") src = healthData;
    const data = healthCardData(src);
    const el = healthSlot(pid || PEND);
    if (el) el.innerHTML = healthCardHtml(data);
    return data;
  }

  async function loadHealth(panelId) {
    const pid = panelId || PEND;
    if (!healthData) {
      const el = healthSlot(pid);
      if (el) el.innerHTML = '<div class="fr-hint">健康度加载中…</div>';
    }
    const d = await api("/api/selfiter/health");
    healthData = (d && d.ok === false) ? null : (d || null);
    return renderHealthCard(healthData, pid);
  }

  // ============================================================ 红点
  // paintDot：把数字**真正写进**角标。
  // 2026-11-16 用户报「明明有 9 条，顶栏还是 0」：原来只设了 hidden / title，
  // 圆圈里的文字一直是 HTML 里写死的「0」，所以数字永远不动 ——
  // 有数据时只是把 hidden 去掉，露出那个写死的 0。
  function paintDot(id, n, label) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = n > 99 ? "99+" : String(n);
    el.hidden = n <= 0;
    el.title = n ? (n + " " + label) : "";
  }

  async function refreshDots() {
    try {
      const p = await api("/api/selfiter/pending");
      const n = (p && p.total) || 0;
      paintDot("pendingDot", n, "条待批准");
      paintDot("pendingBadge", n, "条待批准");
    } catch (e) { /* 忽略 */ }
    try {
      const f = await api("/api/featradar/stats");
      // 2026-11-16：新功能从【待批准】挪到【推荐新功能】后，红点必须把
      // 「自动反思里想出来、还没批的新功能」也算上，否则用户看不到提示。
      let n = (f && f.pending) || 0;
      try {
        const fp = await api("/api/selfiter/pending_features");
        n += (fp && fp.total) || 0;
      } catch (e) { /* 忽略 */ }
      paintDot("featDot", n, "条新功能等你决定");
      paintDot("suggestBadge", n, "条新功能等你决定");
    } catch (e) { /* 忽略 */ }
  }

  // ============================================================ 绑定
  // 2026-11-16：侧栏还有一套同名入口（btnPending / btnSuggest），原来**没有
  // 任何点击处理** —— 点了完全没反应。现在和顶栏共用同一套打开逻辑。
  function openPendingPanel() {
    ensurePanel(PEND, "📋 待批准", "所有还没执行的自动迭代任务");
    openPanel(PEND);
    loadPending();
    loadHealth(PEND);       // 打开就能看到成功率/回滚/熔断/幽灵修复，不用再问
  }
  function openFeatPanel() {
    ensurePanel(FEAT, "💡 推荐新功能", "模型想出来的、能提高体验的全新功能");
    openPanel(FEAT);
    loadFeat();
  }

  ["pendingBtn", "btnPending"].forEach(function (id) {
    const b = document.getElementById(id);
    if (b) {
      b.addEventListener("click", function (e) {
        e.preventDefault();
        openPendingPanel();
      });
    }
  });

  const failkBtn = document.getElementById("failkBtn");
  if (failkBtn) {
    failkBtn.addEventListener("click", function (e) {
      e.preventDefault();
      ensurePanel(FAILK, "📊 失败分类", "自动迭代失败类型 Top 10（按次数）");
      openPanel(FAILK);
      loadFailKinds();
    });
  }

  ["featBtn", "btnSuggest"].forEach(function (id) {
    const b = document.getElementById(id);
    if (b) {
      b.addEventListener("click", function (e) {
        e.preventDefault();
        openFeatPanel();
      });
    }
  });

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

    if (t.closest("#" + FAILK)) {
      const act = t.getAttribute("data-act");
      if (act === "reload") { loadFailKinds(); return; }
      return;
    }

    if (t.closest("#" + FEAT)) {
      const act = t.getAttribute("data-act");
      if (act === "reload") { loadFeat(); return; }
      if (act === "think") { featThink(t); return; }
      const card = t.closest(".fr-card");
      if (!card) return;
      // 待批准的新功能（来自自动反思的计划动作）走 selfiter 的批准/拒绝
      const ppid = card.getAttribute("data-ppid");
      if (ppid) {
        const faid = card.getAttribute("data-faid");
        if (act === "pok") { featPendApprove(ppid, faid); return; }
        if (act === "pno") { featPendReject(ppid, faid); return; }
        return;
      }
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
    [PEND, FEAT, FAILK].forEach(function (id) {
      const box = document.getElementById(id);
      if (box && !box.hidden) closePanel(id);
    });
  });

  refreshDots();
  setInterval(function () { if (!document.hidden) refreshDots(); }, 60000);
})();

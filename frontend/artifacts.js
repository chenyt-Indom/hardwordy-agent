/* 产物卡片（2026-11-08）
 * ------------------------------------------------------------
 * 用户反馈：
 *   ① 生成的文件只给一个「下载」按钮，想看内容还得自己去文件夹里翻；
 *   ② 希望聊天里能直接展开 / 收起，默认收起；动画别太闹。
 *   ③（2026-11-08 追加）**双击就要能直接打开**，不要再让人去文件夹里一层层翻。
 *
 * 做法：
 *   · app.js 渲染正文时，把站内文件链接交给 chip() → 得到一个行内小胶囊；
 *   · 正文渲染完调 flush(bubble) → 在气泡末尾追加可折叠的产物卡片；
 *   · 卡片默认收起，点标题才按需 fetch 内容（文本类 / 文档类抽取正文，图片直显）；
 *   · 「直接打开」按钮 / 双击卡片标题 → POST /api/doclib/open_file，
 *     后端用 os.startfile（macOS open / Linux xdg-open）拉起本机默认程序，
 *     等于在资源管理器里双击那个文件；
 *   · 事件用 document 级委托，历史回放出来的卡片同样可用。
 */
(() => {
  let ctx = null;              // 当前渲染上下文：{ items: [] }；null = 不在渲染中

  const KINDS = {
    docx: ["📝", "Word"], doc: ["📝", "Word"],
    pptx: ["📊", "PPT"], ppt: ["📊", "PPT"],
    xlsx: ["📈", "表格"], xls: ["📈", "表格"], csv: ["📈", "CSV"],
    pdf: ["📕", "PDF"], zip: ["🗜", "压缩包"],
    png: ["🖼", "图片"], jpg: ["🖼", "图片"], jpeg: ["🖼", "图片"],
    gif: ["🖼", "图片"], webp: ["🖼", "图片"], svg: ["🖼", "图片"], bmp: ["🖼", "图片"],
    md: ["📄", "Markdown"], markdown: ["📄", "Markdown"], txt: ["📄", "文本"],
    log: ["📄", "日志"], py: ["🐍", "Python"], js: ["📜", "JS"], ts: ["📜", "TS"],
    json: ["🧾", "JSON"], html: ["🌐", "HTML"], css: ["🎨", "CSS"],
    mp4: ["🎬", "视频"], mp3: ["🎵", "音频"], wav: ["🎵", "音频"],
  };
  const IMG_EXT = ["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp"];
  const NO_PREVIEW = ["zip", "mp4", "mp3", "wav", "exe", "dll", "bin"];

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function toast(msg, kind) {
    if (window.__toast) window.__toast(msg, kind || "");
  }
  function relOf(url) {
    try {
      const u = new URL(url, location.href);
      if (u.pathname === "/api/doclib/download") return u.searchParams.get("rel") || "";
    } catch (e) { /* 不是合法 URL 就当普通链接 */ }
    return "";
  }
  function extOf(name) {
    const s = String(name || "").split("/").pop() || "";
    const i = s.lastIndexOf(".");
    return i >= 0 ? s.slice(i + 1).toLowerCase() : "";
  }

  function cardHtml(item, idx) {
    const rel = item.rel || "";
    const name = rel ? (rel.split("/").pop() || rel) : (item.label || "文件");
    const ext = extOf(name);
    const k = KINDS[ext] || ["📎", ext ? ext.toUpperCase() : "文件"];
    const isImg = IMG_EXT.indexOf(ext) >= 0;
    // ⚠️ 拼成一行：气泡是 white-space:pre-wrap，源码换行会变成真的空行
    return '<div class="art-card" data-idx="' + idx + '" data-rel="' + esc(rel) + '" data-ext="' + esc(ext) + '">'
      + '<button type="button" class="art-head" aria-expanded="false" title="单击展开 / 收起，双击直接用本机程序打开">'
      + '<span class="art-ico">' + k[0] + '</span>'
      + '<span class="art-name">' + esc(name) + '</span>'
      + '<span class="art-kind">' + esc(k[1]) + '</span>'
      + '<span class="art-arrow">▾</span>'
      + '</button>'
      + '<div class="art-body"><div class="art-inner">'
      + '<div class="art-tip">💡 双击标题可直接用本机程序打开</div>'
      + '<div class="art-preview" data-state="idle">' + (isImg ? "展开后加载图片…" : "展开后加载内容…") + '</div>'
      + '<div class="art-acts">'
      + (rel ? '<button type="button" class="art-btn primary" data-art-openfile="' + esc(rel) + '">🗂 直接打开</button>' : "")
      + '<a class="art-btn' + (rel ? "" : " primary") + '" href="' + esc(mobileViewUrl(item.url)) + '">⬇ 下载</a>'
      + (rel ? '<button type="button" class="art-btn" data-art-open="' + esc(rel) + '">📂 在文件库中定位</button>' : "")
      + '</div></div></div></div>';
  }

  // app.js 渲染一条消息前调用
  function begin() { ctx = { items: [] }; }

  // app.js 的 dlGroup 调用；返回 null 表示让它走老样式
  function chip(label, url) {
    if (!ctx) return null;
    const rel = relOf(url);
    let idx = -1;
    if (rel) {
      idx = ctx.items.findIndex((x) => x.rel === rel);
    }
    if (idx < 0) {
      ctx.items.push({ url: url, rel: rel, label: label });
      idx = ctx.items.length - 1;
    }
    const disp = (label && label !== "点击下载") ? label : (rel ? rel.split("/").pop() : label);
    return '<button type="button" class="art-chip" data-art="' + idx
      + '" title="单击展开卡片，双击直接用本机程序打开">📎 ' + esc(disp) + '</button>';
  }

  // 正文渲染完调用：把卡片追加到气泡末尾（默认收起）
  function flush(bubble) {
    const c = ctx;
    ctx = null;
    if (!bubble || !c || !c.items.length) return;
    const wrap = document.createElement("div");
    wrap.className = "art-list";
    wrap.innerHTML = c.items.map((it, i) => cardHtml(it, i)).join("");
    bubble.appendChild(wrap);
  }

  function isOpen(card) { return card.classList.contains("open"); }

  function toggle(card) {
    if (!card) return;
    const open = !isOpen(card);
    card.classList.toggle("open", open);
    const head = card.querySelector(".art-head");
    if (head) head.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) fillPreview(card);
  }

  // ---------- 用本机默认程序直接打开（后端 os.startfile） ----------
  async function openWithSystem(rel) {
    const r = String(rel || "");
    if (!r) { toast("这个文件不在生成文库里，只能下载", "warn"); return false; }
    toast("正在用本机程序打开…", "");
    try {
      const resp = await fetch("/api/doclib/open_file", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rel: r }),
      });
      const d = await resp.json().catch(() => ({}));
      if (!resp.ok || (d && d.ok === false)) {
        throw new Error((d && (d.detail || d.error)) || ("HTTP " + resp.status));
      }
      if (d.opened) {
        toast("已用本机程序打开：" + r, "ok");
        return true;
      }
      toast((d.detail || "无法直接打开") + (d.host_path ? "：" + d.host_path : ""), "warn");
      return false;
    } catch (e) {
      toast("打开失败：" + (e && e.message ? e.message : e), "warn");
      return false;
    }
  }

  async function fillPreview(card) {
    const pv = card.querySelector(".art-preview");
    if (!pv || pv.dataset.state !== "idle") return;
    const rel = card.getAttribute("data-rel") || "";
    const ext = card.getAttribute("data-ext") || "";
    pv.dataset.state = "loading";

    if (IMG_EXT.indexOf(ext) >= 0) {
      if (!rel) { pv.innerHTML = '<div class="art-note">这个文件没有可预览的地址，请直接下载。</div>'; return; }
      pv.innerHTML = '<img alt="' + esc(rel) + '" src="/api/doclib/download?rel='
        + encodeURIComponent(rel) + '">';
      pv.dataset.state = "done";
      return;
    }
    if (!rel) {
      pv.innerHTML = '<div class="art-note">这个文件不在生成文库里，点下面的按钮下载查看。</div>';
      pv.dataset.state = "done";
      return;
    }
    if (NO_PREVIEW.indexOf(ext) >= 0) {
      pv.innerHTML = '<div class="art-note">' + esc(ext.toUpperCase())
        + ' 不能内联预览，点上面的「直接打开」用本机程序打开。</div>';
      pv.dataset.state = "done";
      return;
    }

    pv.textContent = "正在读取内容…";
    try {
      const r = await fetch("/api/doclib/file?rel=" + encodeURIComponent(rel));
      const d = await r.json().catch(() => ({}));
      if (!r.ok || !d || d.ok === false) throw new Error((d && (d.detail || d.error)) || "读取失败");
      let text = String(d.text || "");
      const LIMIT = 4000;
      let cut = false;
      if (text.length > LIMIT) { text = text.slice(0, LIMIT); cut = true; }
      pv.innerHTML = "";
      const pre = document.createElement("div");
      pre.className = "art-text";
      pre.textContent = text || "（这个文件里没有可抽取的文字）";
      pv.appendChild(pre);
      const tip = document.createElement("div");
      tip.className = "art-note";
      const isDoc = ["docx", "doc", "pptx", "ppt", "xlsx", "xls", "pdf"].indexOf(ext) >= 0;
      tip.textContent = (isDoc ? "以上是抽取出的文字，排版以原文件为准。" : "")
        + (cut ? "（内容较长，这里只显示前 4000 字，完整内容请点「直接打开」）" : "");
      if (tip.textContent) pv.appendChild(tip);
      pv.dataset.state = "done";
    } catch (e) {
      pv.innerHTML = '<div class="art-note">读取失败：' + esc(e && e.message ? e.message : e)
        + '。可以点上面的「直接打开」用本机程序打开。</div>';
      pv.dataset.state = "done";
    }
  }

  // 事件委托：胶囊 → 展开对应卡片；标题 → 折叠/展开；按钮 → 跳文件库 / 系统打开
  document.addEventListener("click", (e) => {
    const chipEl = e.target.closest ? e.target.closest(".art-chip") : null;
    if (chipEl) {
      const bubble = chipEl.closest(".bubble");
      const idx = chipEl.getAttribute("data-art");
      const card = bubble && bubble.querySelector('.art-card[data-idx="' + idx + '"]');
      if (card) {
        if (!isOpen(card)) toggle(card);
        card.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
      return;
    }
    const head = e.target.closest ? e.target.closest(".art-head") : null;
    if (head) { toggle(head.closest(".art-card")); return; }

    const openFile = e.target.closest ? e.target.closest("[data-art-openfile]") : null;
    if (openFile) { openWithSystem(openFile.getAttribute("data-art-openfile") || ""); return; }

    const openBtn = e.target.closest ? e.target.closest("[data-art-open]") : null;
    if (openBtn) {
      const rel = openBtn.getAttribute("data-art-open") || "";
      if (window.__openInLibrary) window.__openInLibrary(rel);
    }
  });

  // 双击 = 直接用本机程序打开（标题、胶囊都支持）
  document.addEventListener("dblclick", (e) => {
    const t = e.target;
    const head = t && t.closest ? t.closest(".art-head") : null;
    if (head) {
      const card = head.closest(".art-card");
      openWithSystem(card ? (card.getAttribute("data-rel") || "") : "");
      return;
    }
    const chipEl = t && t.closest ? t.closest(".art-chip") : null;
    if (chipEl) {
      const bubble = chipEl.closest(".bubble");
      const idx = chipEl.getAttribute("data-art");
      const card = bubble && bubble.querySelector('.art-card[data-idx="' + idx + '"]');
      openWithSystem(card ? (card.getAttribute("data-rel") || "") : "");
    }
  });

  window.__Artifacts = { begin: begin, chip: chip, flush: flush, openWithSystem: openWithSystem };
})();

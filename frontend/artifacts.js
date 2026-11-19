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

  // ============================================================
  // 画布圈选再改（2026-11-16）
  // ------------------------------------------------------------
  // 用户诉求：一轮回复里往往混着「说明文字 + 图片 + PPT + 网页链接」，
  //   想只改其中某一块，却只能整段重发 —— 模型经常把没让改的地方也动了。
  // 做法：
  //   · split_blocks(reply) 把一轮回复按类型拆成 block 对象
  //     { id, type, content }，type ∈ text / image / ppt / web；
  //   · block_context(blocks, id) 返回选中块的原文（给后端当上下文）；
  //   · 前端在气泡上做块级圈选（点击 / 拖选高亮）与箭头连线合并两块，
  //     选中后把 block_context 结果随消息一起发给后端。
  // 无选中时 block_context 返回空串，调用方按旧逻辑走（降级）。
  // ============================================================

  // 从一段文本里识别「块」：图片 markdown / 网页链接 / PPT 文件引用 / 普通文字。
  // 返回 [{type, content}]，content 是这一块的原文（不含分隔用的空行）。
  function split_blocks(reply) {
    const src = String(reply == null ? "" : reply);
    if (!src.trim()) return [];
    const blocks = [];
    let buf = [];
    const flushText = () => {
      const t = buf.join("\n").replace(/^\s*\n+|\n+\s*$/g, "");
      if (t.trim()) blocks.push({ type: "text", content: t });
      buf = [];
    };
    // 逐行扫：命中「独立成块的媒体/链接」就先把累积的文字块收掉，再单独成块。
    const lines = src.split(/\r?\n/);
    const IMG_RE = /^\s*!\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)\s*$/;
    const PPT_RE = /^\s*(?:\[?[^\]]*\]\(([^)\s]+\.(?:pptx?|key)(?:\?[^)\s]*)?)\)|([^\s]+\.(?:pptx?|key)))\s*$/i;
    const WEB_RE = /^\s*(?:[-*+]\s+)?(?:\[([^\]]+)\]\(([^)\s]+)\)|(https?:\/\/[^\s]+))\s*$/;
    for (let i = 0; i < lines.length; i++) {
      const ln = lines[i];
      let m;
      if ((m = IMG_RE.exec(ln))) {
        flushText();
        blocks.push({ type: "image", content: ln.trim() });
        continue;
      }
      if ((m = PPT_RE.exec(ln))) {
        flushText();
        blocks.push({ type: "ppt", content: ln.trim() });
        continue;
      }
      if ((m = WEB_RE.exec(ln))) {
        flushText();
        blocks.push({ type: "web", content: ln.trim() });
        continue;
      }
      buf.push(ln);
    }
    flushText();
    // 补 id：b0 / b1 / b2 …（稳定、可预测，方便前端和后端对齐）
    return blocks.map((b, i) => ({ id: "b" + i, type: b.type, content: b.content }));
  }

  // 取选中块的原文。id 可以是单个 id，也可以是 id 数组（连线合并两块时用）。
  // 找不到 / 没选中 → 返回空串（调用方据此降级为旧行为）。
  function block_context(blocks, id) {
    const list = Array.isArray(blocks) ? blocks : [];
    if (!list.length || id == null) return "";
    const ids = Array.isArray(id) ? id : [id];
    const picked = [];
    ids.forEach((one) => {
      const b = list.find((x) => x && x.id === one);
      if (b && typeof b.content === "string" && b.content.trim()) picked.push(b);
    });
    if (!picked.length) return "";
    // 单块：直接给原文；多块（合并）：按类型加个小标题，让模型知道这是几块拼起来的
    if (picked.length === 1) return picked[0].content;
    return picked.map((b) => "【" + b.type + "】\n" + b.content).join("\n\n");
  }

  // 把 split_blocks 的结果渲染成可圈选的块（挂在气泡里）。
  // 返回一个句柄，供 app.js 读取当前选中 / 连线状态。
  function mountBlocks(bubble, reply) {
    if (!bubble) return null;
    const blocks = split_blocks(reply);
    if (!blocks.length) return null;
    const wrap = document.createElement("div");
    wrap.className = "blk-wrap";
    wrap.setAttribute("data-blk-count", String(blocks.length));
    wrap.innerHTML = blocks.map((b) =>
      '<div class="blk" data-blk-id="' + esc(b.id) + '" data-blk-type="' + esc(b.type) + '">'
      + '<span class="blk-tag">' + esc(b.type) + '</span>'
      + '<div class="blk-body">' + esc(b.content) + '</div>'
      + '</div>'
    ).join("");
    bubble.appendChild(wrap);

    const state = { blocks: blocks, selected: [], links: [] };
    const blkEls = () => Array.from(wrap.querySelectorAll(".blk"));
    const idOf = (el) => (el ? el.getAttribute("data-blk-id") : "");
    const elOf = (id) => wrap.querySelector('.blk[data-blk-id="' + id + '"]');

    function paint() {
      blkEls().forEach((el) => {
        const id = idOf(el);
        el.classList.toggle("sel", state.selected.indexOf(id) >= 0);
        el.classList.toggle("linked", state.links.some((p) => p.indexOf(id) >= 0));
      });
    }

    // 点击：单选 / Ctrl(⌘) 多选切换
    wrap.addEventListener("click", (e) => {
      const el = e.target.closest ? e.target.closest(".blk") : null;
      if (!el) return;
      const id = idOf(el);
      const multi = e.ctrlKey || e.metaKey;
      if (multi) {
        const i = state.selected.indexOf(id);
        if (i >= 0) state.selected.splice(i, 1); else state.selected.push(id);
      } else {
        state.selected = state.selected.length === 1 && state.selected[0] === id ? [] : [id];
      }
      paint();
      if (window.__onBlockSelect) window.__onBlockSelect(state);
    });

    // 拖选：从一块拖到另一块 → 高亮这一段（连续区间）
    let dragFrom = null;
    wrap.addEventListener("mousedown", (e) => {
      const el = e.target.closest ? e.target.closest(".blk") : null;
      if (!el) return;
      dragFrom = idOf(el);
    });
    wrap.addEventListener("mouseup", (e) => {
      if (!dragFrom) return;
      const el = e.target.closest ? e.target.closest(".blk") : null;
      const to = el ? idOf(el) : "";
      if (to && to !== dragFrom) {
        const ids = blocks.map((b) => b.id);
        const a = ids.indexOf(dragFrom), b = ids.indexOf(to);
        if (a >= 0 && b >= 0) {
          const lo = Math.min(a, b), hi = Math.max(a, b);
          state.selected = ids.slice(lo, hi + 1);
          paint();
          if (window.__onBlockSelect) window.__onBlockSelect(state);
        }
      }
      dragFrom = null;
    });

    // 连线：按住 Shift 点两块 → 合并成一组（links 里存 [idA, idB]）
    let linkFrom = null;
    wrap.addEventListener("click", (e) => {
      if (!(e.shiftKey)) return;
      const el = e.target.closest ? e.target.closest(".blk") : null;
      if (!el) return;
      const id = idOf(el);
      if (!linkFrom) { linkFrom = id; return; }
      if (linkFrom !== id) {
        state.links.push([linkFrom, id]);
        state.selected = [linkFrom, id];
        paint();
        if (window.__onBlockSelect) window.__onBlockSelect(state);
      }
      linkFrom = null;
    });

    // 对外句柄：app.js 发消息时读它拿上下文
    const handle = {
      blocks: blocks,
      getSelected: () => state.selected.slice(),
      getLinks: () => state.links.map((p) => p.slice()),
      context: () => block_context(blocks, state.selected),
      clear: () => { state.selected = []; state.links = []; paint(); },
    };
    wrap.__blkHandle = handle;
    return handle;
  }

  // 从气泡里找块句柄（发消息时用）
  function handleOf(bubble) {
    if (!bubble) return null;
    const wrap = bubble.querySelector(".blk-wrap");
    return wrap ? (wrap.__blkHandle || null) : null;
  }

  // ============================================================
  // 画布卡片化回复（2026-11-19）
  // ------------------------------------------------------------
  // 摩擦点：「同一需求被重复发送」—— 用户反复描述，本质是上一轮结果混在
  //   文字流里不好指认，只能整段重述。
  // 做法：
  //   · split_blocks(reply) 的拆块口径不动，这里只负责**画**：
  //     renderCanvasCard(block) 画一张卡片（文字/图片/表格/文件/网页各自成块），
  //     renderCanvas(blocks) 把它们挂到 .canvas 容器里，卡片可拖；
  //   · 拖动位置按 canvas key 存 localStorage —— 刷新页面后位置还在；
  //   · 点一下卡片 = 选中，回调 onPick({cardId,type,content,…}) 由 app.js
  //     唤起底部「就这块改」输入框（再点一次已选中的卡片 = 取消选中）。
  // 纯文字回复不会被拆成卡片（拆不拆由 app.js 的 canvasCardsOf 决定）。
  // ============================================================
  const CARD_KIND = {
    text: "📝 文字", image: "🖼 图片", ppt: "📊 PPT", file: "📎 文件",
    table: "📈 表格", web: "🌐 网页",
  };
  const POS_KEY = "canvas_pos::";

  // markdown 图片语法 / 裸路径 → 真正能显示的地址（站内文件走后端下载口）
  function canvasImgSrc(raw) {
    const m = /!\[[^\]]*\]\(\s*([^)\s]+)/.exec(String(raw == null ? "" : raw));
    const src = m ? m[1] : String(raw == null ? "" : raw).trim();
    if (!src) return "";
    if (/^(https?:|data:|blob:|\/)/i.test(src)) return src;
    if (/^(data|image_library|models)[\\/]/i.test(src)) {
      return "/api/doclib/download?rel=" + encodeURIComponent(src);
    }
    return src;
  }

  // 稳定的画布 key：同一轮回复（内容一样）刷新后拿到同一个 key，位置才能恢复；
  // 内容变了就是新画布，不会把旧位置错套到别的卡片上。
  function canvasKeyFor(blocks) {
    const src = (Array.isArray(blocks) ? blocks : [])
      .map((b) => String((b && b.type) || "") + ":" + String((b && b.content) || "")).join("\u0001");
    let h = 5381;
    for (let i = 0; i < src.length; i++) h = ((h << 5) + h + src.charCodeAt(i)) | 0;
    return "cv" + (h >>> 0).toString(36) + "_" + src.length;
  }

  function loadCanvasPos(key) {
    try {
      const raw = localStorage.getItem(POS_KEY + key);
      const obj = raw ? JSON.parse(raw) : null;
      return (obj && typeof obj === "object") ? obj : {};
    } catch (e) { return {}; }
  }
  function saveCanvasPos(key, id, pos) {
    if (!key || !id || !pos) return;
    try {
      const all = loadCanvasPos(key);
      all[id] = { left: Math.round(pos.left || 0), top: Math.round(pos.top || 0) };
      localStorage.setItem(POS_KEY + key, JSON.stringify(all));
    } catch (e) { /* 存不下（隐私模式 / 配额满）就当没拖过，不影响使用 */ }
  }
  function resetCanvasPos(key) {
    try { localStorage.removeItem(POS_KEY + key); } catch (e) { /* 忽略 */ }
  }

  // 一张卡片。block = { id, type, content }
  // opts = { index, onPick(pick|null), onMove(id, {left, top}) }
  function renderCanvasCard(block, opts) {
    opts = opts || {};
    const b = block || {};
    const id = String(b.id || b.cardId || ("c" + (opts.index || 0)));
    const type = String(b.type || "text");
    const content = String(b.content == null ? "" : b.content);

    const el = document.createElement("div");
    el.className = "canvas-card canvas-card-" + type;
    el.setAttribute("data-card-id", id);
    el.setAttribute("data-card-type", type);
    el.__card = { cardId: id, type: type, content: content };

    // ---- 标题条 = 拖动把手（也是点选的落点）----
    const hd = document.createElement("div");
    hd.className = "canvas-card-hd";
    const kind = document.createElement("span");
    kind.className = "canvas-card-kind";
    kind.textContent = CARD_KIND[type] || "📝 内容";
    const idEl = document.createElement("span");
    idEl.className = "canvas-card-id";
    idEl.textContent = id;
    hd.appendChild(kind);
    hd.appendChild(idEl);
    // 强调色：语音画布「改成红色」会往卡片上写 color 字段。
    // 这里原来**只存不渲染**，改完色卡片一点变化都看不出来（2026-11-18 实测发现）。
    const accent = String(b.color || "").trim();
    if (/^#[0-9a-fA-F]{6}$/.test(accent)) {
      el.style.borderColor = accent;
      el.style.boxShadow = "inset 0 0 0 1px " + accent;
      const chip = document.createElement("span");
      chip.className = "canvas-card-color";
      chip.style.background = accent;
      chip.title = "卡片强调色 " + accent;
      hd.appendChild(chip);
    }
    el.appendChild(hd);

    // ---- 正文 ----
    const bd = document.createElement("div");
    bd.className = "canvas-card-bd";
    if (type === "image") {
      const fig = document.createElement("div");
      fig.className = "canvas-card-fig";
      const src = canvasImgSrc(content);
      if (src) {
        const img = document.createElement("img");
        img.src = src;
        img.alt = "卡片图片";
        fig.appendChild(img);
      } else {
        fig.textContent = content;
      }
      bd.appendChild(fig);
    } else if (type === "web") {
      const a = document.createElement("a");
      a.className = "canvas-card-web";
      const mm = /\]\(\s*([^)\s]+)/.exec(content);
      const url = mm ? mm[1] : (/(https?:\/\/\S+)/.exec(content) || [])[1];
      a.textContent = content.replace(/^\s*[-*+]\s+/, "");
      if (url) { a.href = url; a.target = "_blank"; a.rel = "noreferrer"; }
      bd.appendChild(a);
    } else if (type === "file" || type === "ppt") {
      const row = document.createElement("div");
      row.className = "canvas-card-file";
      const mm = /\(\s*([^)\s]+)[^)]*\)/.exec(content);
      const path = mm ? mm[1] : content.trim();
      const name = path.split(/[\\/]/).pop() || path;
      const ico = document.createElement("span");
      ico.className = "canvas-card-file-ico";
      ico.textContent = "📎";
      const nm = document.createElement("span");
      nm.className = "canvas-card-file-name";
      nm.title = path;
      nm.textContent = name;
      row.appendChild(ico);
      row.appendChild(nm);
      if (/^(data|image_library|models)[\\/]/.test(path)) {
        const dl = document.createElement("a");
        dl.className = "canvas-card-file-dl";
        dl.href = "/api/doclib/download?rel=" + encodeURIComponent(path);
        dl.textContent = "下载";
        dl.addEventListener("click", (e) => e.stopPropagation());
        row.appendChild(dl);
      }
      bd.appendChild(row);
    } else {
      // 文字 / 表格：原样呈现（表格保留等宽对齐），太长先折叠
      const pre = document.createElement("div");
      pre.className = "canvas-card-text" + (content.length > 420 ? " cut" : "")
        + (type === "table" ? " mono" : "");
      pre.textContent = content;
      bd.appendChild(pre);
      if (content.length > 420) {
        const more = document.createElement("button");
        more.type = "button";
        more.className = "canvas-card-more";
        more.textContent = "展开全文 ▾";
        more.addEventListener("click", (e) => {
          e.stopPropagation();
          const cut = pre.classList.toggle("cut");
          more.textContent = cut ? "展开全文 ▾" : "收起 ▴";
          if (typeof el.__frame === "function") el.__frame();
        });
        bd.appendChild(more);
      }
    }
    el.appendChild(bd);

    // ---- 拖动（只在标题条上，不跟正文的点击选中打架）----
    let drag = null;
    let moved = false;
    hd.addEventListener("pointerdown", (e) => {
      if (e.button != null && e.button !== 0) return;
      drag = { sx: e.clientX, sy: e.clientY, left: el.offsetLeft || 0, top: el.offsetTop || 0 };
      moved = false;
      el.classList.add("dragging");
      try { hd.setPointerCapture(e.pointerId); } catch (err) { /* 老浏览器不支持就算了 */ }
      e.preventDefault();
    });
    hd.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const dx = e.clientX - drag.sx;
      const dy = e.clientY - drag.sy;
      if (Math.abs(dx) + Math.abs(dy) > 3) moved = true;
      el.style.left = Math.max(0, drag.left + dx) + "px";
      el.style.top = Math.max(0, drag.top + dy) + "px";
      el.__placed = true;
      if (typeof el.__frame === "function") el.__frame();
    });
    const endDrag = () => {
      if (!drag) return;
      drag = null;
      el.classList.remove("dragging");
      if (moved) {
        el.__placed = true;
        if (typeof opts.onMove === "function") {
          try { opts.onMove(id, { left: el.offsetLeft || 0, top: el.offsetTop || 0 }); } catch (e) {}
        }
      }
      if (typeof el.__frame === "function") el.__frame();
    };
    hd.addEventListener("pointerup", endDrag);
    hd.addEventListener("pointercancel", endDrag);

    // ---- 点选（刚拖完的那一下不算点选）----
    el.addEventListener("click", (e) => {
      if (moved) { moved = false; return; }
      // 卡片里的链接 / 展开按钮自己处理，别把点击吞成选中
      if (e.target && e.target.closest && e.target.closest("a,button.canvas-card-more")) return;
      const on = !el.classList.contains("sel");
      el.classList.toggle("sel", on);
      if (typeof opts.onPick === "function") {
        try {
          if (on) {
            opts.onPick({
              cardId: id, type: type, content: content, how: "点选",
              rect: { x: 0, y: 0, w: el.offsetWidth || 0, h: el.offsetHeight || 0 },
            });
          } else {
            opts.onPick(null);
          }
        } catch (e2) { /* 回调炸了不能影响选中态本身 */ }
      }
    });

    return el;
  }

  // 把若干卡片挂成一块画布。blocks = [{id,type,content}]
  // opts = { key, onPick(pick|null), onMove(id, pos) }
  // 返回画布 DOM（.canvas）；没有卡片时返回 null（调用方按旧逻辑走）。
  function renderCanvas(blocks, opts) {
    opts = opts || {};
    const list = (Array.isArray(blocks) ? blocks : []).filter(
      (b) => b && String(b.content == null ? "" : b.content).trim());
    if (!list.length) return null;

    const key = String(opts.key || canvasKeyFor(list));
    const wrap = document.createElement("section");
    wrap.className = "canvas";
    wrap.setAttribute("data-canvas-key", key);

    const hd = document.createElement("div");
    hd.className = "canvas-hd";
    const hint = document.createElement("span");
    hint.className = "canvas-hint";
    hint.textContent = "🖼 画布 · 拖标题条挪位置；点一下卡片就出现「就这块改」";
    hd.appendChild(hint);
    const reset = document.createElement("button");
    reset.type = "button";
    reset.className = "canvas-reset";
    reset.textContent = "复位";
    reset.title = "把卡片摆回自动排版的位置";
    reset.addEventListener("click", (e) => {
      e.stopPropagation();
      resetCanvasPos(key);
      layout(true);
      hint.textContent = "🖼 画布 · 已复位（拖标题条挪位置，点卡片就出现「就这块改」）";
    });
    hd.appendChild(reset);
    wrap.appendChild(hd);

    const grid = document.createElement("div");
    grid.className = "canvas-grid";
    wrap.appendChild(grid);

    const els = [];
    const saved = loadCanvasPos(key);
    let selected = "";

    // 容器高度跟着卡片走（卡片绝对定位，撑不开父元素）
    function frame() {
      let bottom = 0;
      els.forEach((el) => { bottom = Math.max(bottom, (el.offsetTop || 0) + (el.offsetHeight || 0)); });
      if (!bottom) bottom = els.length * 130;
      grid.style.height = (bottom + 10) + "px";
    }

    // 自动排版：桌面上两列交错，手机上单列；已手动拖过的卡片不动
    function layout(force) {
      const narrow = typeof window !== "undefined" && window.innerWidth && window.innerWidth <= 768;
      const cols = (els.length > 1 && !narrow) ? 2 : 1;
      const tops = [];
      for (let i = 0; i < cols; i++) tops.push(4);
      let anyPlaced = false;
      els.forEach((el, i) => {
        if (!force && el.__placed) { anyPlaced = true; return; }
        const col = i % cols;
        el.style.left = cols === 1 ? "0%" : (col === 0 ? "0%" : "51%");
        el.style.top = tops[col] + "px";
        el.__placed = false;
        tops[col] += (el.offsetHeight || 120) + 10;
      });
      frame();
      return anyPlaced;
    }

    list.forEach((b, i) => {
      const el = renderCanvasCard(b, {
        index: i,
        onMove: (id, pos) => {
          saveCanvasPos(key, id, pos);          // 刷新页面后位置还在
          if (typeof opts.onMove === "function") {
            try { opts.onMove(id, pos); } catch (e) {}
          }
        },
        onPick: (pick) => {
          // 单选：一块被选中时，其它卡片自动取消选中
          els.forEach((one) => { if (!pick || one !== el) one.classList.remove("sel"); });
          if (pick) {
            hint.textContent = "🎯 已选中 " + pick.cardId + "（" + (CARD_KIND[pick.type] || pick.type)
              + "）—— 底部输入「就这块改」";
          } else {
            hint.textContent = "🖼 画布 · 拖标题条挪位置；点一下卡片就出现「就这块改」";
          }
          if (pick) selected = pick.cardId; else selected = "";
          if (typeof opts.onPick === "function") {
            try { opts.onPick(pick); } catch (e) {}
          }
        },
      });
      if (!el) return;
      el.__frame = frame;
      grid.appendChild(el);
      els.push(el);
      const p = saved[el.__card.cardId];
      if (p) {
        // 窄屏会把旧的横向位置顶出画布，这里夹一下再贴回去
        const maxLeft = Math.max(0, (grid.clientWidth || wrap.clientWidth || 600) - 120);
        el.style.left = Math.min(Math.max(0, p.left || 0), maxLeft) + "px";
        el.style.top = Math.max(0, p.top || 0) + "px";
        el.__placed = true;
      }
    });
    // 贴回保存位置后再夹一次：首帧容器可能还没有真实宽度，等布局出来再夹，
    // 免得窄屏上刷新后卡片横着飘到画布外面去。
    function clampPlaced() {
      const maxLeft = Math.max(0, (grid.clientWidth || 0) - 100);
      els.forEach((el) => {
        if (!el.__placed) return;
        const left = parseFloat(el.style.left) || 0;
        if (left > maxLeft) el.style.left = maxLeft + "px";
      });
      frame();
    }
    layout(false);
    if (typeof requestAnimationFrame === "function") {
      // 字体 / 图片加载完高度会变，等一帧再排一次（手动拖过的卡片不动，尊重用户手摆的位置）
      requestAnimationFrame(() => { layout(false); clampPlaced(); });
    }

    const handle = {
      key: key,
      cards: list,
      cardEls: els,
      selectedId: () => selected,
      positions: () => loadCanvasPos(key),
      getCard: (id) => {
        const el = els.filter((x) => x.__card && x.__card.cardId === id)[0];
        return el ? el.__card : null;
      },
      reset: () => { resetCanvasPos(key); layout(true); },
    };
    wrap.__canvasHandle = handle;
    return wrap;
  }

  // 把「就是这块」说成一句人和模型都读得懂的话（拼进下一条用户消息）
  function canvasPickContext(pick) {
    if (!pick || !pick.cardId) return "";
    const tail = String(pick.content == null ? "" : pick.content)
      .replace(/\s+/g, " ").trim().slice(0, 300);
    const r = pick.rect || {};
    return "〔画布卡片〕就这块改：cardId=" + pick.cardId
      + "（" + (pick.type || "text") + "）"
      + (pick.how ? "（" + pick.how + "）" : "")
      + (r.w || r.h ? " 选区 " + Math.round(r.w || 0) + "×" + Math.round(r.h || 0) : "")
      + (tail ? "；这块的原文：" + tail : "");
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

  // ============================================================
  // 常驻工作台：产出物堆在桌面上，回来接着干（2026-11-21）
  // ------------------------------------------------------------
  // 痛点：图片生成 / 微改、PPT / Excel、视频剪辑、数字人做出来的东西**只活在
  //   对话流里** —— 关掉页面就找不回，只能重新让模型做一遍。
  // 做法：
  //   · 每次工具产出（app.js 递进来的 obj.cards 里 image / doc / ppt / video）到手
  //     就 add() 一条 {id, kind, title, thumb, path, ts, status}；
  //   · **双写**：localStorage 是主副本（关页面 / 断网都还在，刷新立刻显示）；后端
  //     backend/canvas.py 再存一份（清单口径 + 批量打包都在那边）；后端没起来时
  //     前端照常能用，起来后 hydrate() 再把差异并回来；
  //   · 面板挂在右侧：HTML5 draggable 拖拽排序、点开预览、批量导出 zip、
  //     删除两边同步；顶部「⟳ 接着上次」把上次的产出物带进输入框继续做。
  // ⚠️ 缩略图**绝不存 data: base64**（几百 KB 一张，localStorage 5MB 会写满）。
  // ============================================================
  const WB_KEY = "artifacts_board_v1";
  const WB_MAX = 240;                                  // 与 backend/canvas.py 同口径
  const WB_LIST = "/api/canvas/workbench/list";
  const WB_ADD = "/api/canvas/workbench/add";
  const WB_RM = "/api/canvas/workbench/remove";
  const WB_ORDER = "/api/canvas/workbench/order";
  const WB_EXPORT = "/api/canvas/workbench/export";

  // 扩展名 → 种类（与 backend/canvas.py 的 _ART_EXT_KIND 保持一致）
  const WB_EXT_KIND = {
    png: "image", jpg: "image", jpeg: "image", webp: "image", gif: "image",
    bmp: "image", svg: "image",
    pptx: "ppt", ppt: "ppt", key: "ppt",
    docx: "doc", doc: "doc", pdf: "doc", md: "doc", txt: "doc",
    xlsx: "sheet", xls: "sheet", csv: "sheet",
    mp4: "video", mov: "video", webm: "video", mkv: "video",
    mp3: "audio", wav: "audio", m4a: "audio",
    html: "web", htm: "web", url: "web",
  };
  const WB_KIND_ICO = {
    image: "🖼", ppt: "📊", doc: "📄", sheet: "📈",
    video: "🎬", audio: "🎵", web: "🌐", other: "📦",
  };
  const WB_KINDS = ["image", "ppt", "doc", "sheet", "video", "audio", "web", "other"];

  function wbKindOf(path, hint) {
    const k = String(hint || "").trim().toLowerCase();
    if (WB_KINDS.indexOf(k) >= 0) return k;
    const ext = extOf(String(path || "").split("?")[0]);
    return WB_EXT_KIND[ext] || "other";
  }

  function wbThumbOf(path, kind, thumb) {
    let t = String(thumb || "").trim();
    if (/^data:/i.test(t)) t = "";                    // 绝不把 base64 存进 localStorage
    if (t) return t;
    const p = String(path || "").trim();
    if (!p || wbKindOf(p, kind) !== "image") return "";
    if (/^(https?:|\/api\/)/i.test(p)) return p;
    return "/api/doclib/download?rel=" + encodeURIComponent(p);
  }

  // 稳定短 id：同一个文件被重新生成 / 刷新页面后还是同一条（去重靠它）
  function wbId(path, title) {
    const s = String(path || "") + "\u0001" + String(title || "");
    let h = 5381;
    for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
    return "a" + (h >>> 0).toString(36);
  }

  function wbNormalize(raw) {
    const src = (raw && typeof raw === "object") ? raw : {};
    const path = String(src.path || src.rel || src.file || "").trim();
    let title = String(src.title || src.name || "").replace(/\s+/g, " ").trim().slice(0, 120);
    const kind = wbKindOf(path, src.kind || src.type);
    if (!title) title = String(path).split("?")[0].split(/[\\/]/).pop() || "产出物";
    let ts = Number(src.ts || src.time || 0);
    if (!isFinite(ts) || ts <= 0) ts = Date.now() / 1000;
    return {
      id: String(src.id || "").trim() || wbId(path, title),
      kind: kind,
      title: title,
      thumb: wbThumbOf(path, kind, src.thumb),
      path: String(path).slice(0, 400),
      ts: ts,
      status: String(src.status || "done").slice(0, 20) || "done",
      tool: String(src.tool || "").slice(0, 40),
      note: String(src.note || "").slice(0, 200),
    };
  }

  function wbLoad() {
    try {
      const raw = localStorage.getItem(WB_KEY);
      const arr = raw ? JSON.parse(raw) : [];
      if (!Array.isArray(arr)) return [];
      return arr.filter((x) => x && typeof x === "object").map(wbNormalize).slice(0, WB_MAX);
    } catch (e) { return []; }                        // 存坏了就当空桌面，不影响用
  }

  function wbSave(list) {
    try {
      localStorage.setItem(WB_KEY, JSON.stringify((list || []).slice(0, WB_MAX)));
      return true;
    } catch (e) {
      // 配额满 / 隐私模式：只提示一次。后端那份还在，功能不至于整块失效。
      if (!wbSave._warned) {
        wbSave._warned = true;
        toast("本地存不下更多产出物了（浏览器配额满），后端那份仍然保留", "warn");
      }
      return false;
    }
  }

  // 并进一条：已在列表里就搬到最前、保留原时间（时间代表"什么时候做出来的"）
  function wbMerge(list, raw) {
    const one = wbNormalize(raw);
    const out = (list || []).map(wbNormalize);
    const old = out.filter((x) => x.id === one.id)[0];
    if (old && old.ts) one.ts = old.ts;
    const rest = out.filter((x) => x.id !== one.id);
    rest.unshift(one);
    return { items: rest.slice(0, WB_MAX), item: one };
  }

  function wbPost(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then((r) => r.json().catch(() => ({})).then((d) => d || {}));
  }

  // 加一条产出物。后端那份异步写：写失败不影响本地（界面上照样看得到）。
  function add(raw) {
    const merged = wbMerge(wbLoad(), raw);
    wbSave(merged.items);
    try { wbRender(); } catch (e) { /* 面板还没建好也不能挡住产出物落地 */ }
    wbPost(WB_ADD, { item: merged.item }).catch(() => null);
    return merged.item;
  }

  // 工具产出到手（app.js 每轮把 obj.cards 递进来）。只收真产出物：
  // 地图 / 天气这类"查了一下"的卡片不进工作台 —— 桌面上只堆东西。
  function addCards(cards) {
    const list = Array.isArray(cards) ? cards : [];
    let n = 0;
    list.forEach((c) => {
      if (!c || typeof c !== "object") return;
      const kind = String(c.type || "");
      if (["image", "doc", "ppt", "video"].indexOf(kind) < 0) return;
      const p = (c.payload && typeof c.payload === "object") ? c.payload : {};
      const path = String(p.path || p.saved_path || p.rel || "");
      // 卡片标题里常带一个 emoji 前缀（「🎨 海边的猫」），存进工作台要去掉
      const title = String(c.title || "").replace(/^[^\s\u4e00-\u9fa5]+\s+/, "").trim();
      if (!path && !title) return;
      let k = kind;
      if (kind === "doc" && wbKindOf(path, "") === "sheet") k = "sheet";
      add({
        kind: k,
        title: title || path,
        path: path,
        thumb: String(c.thumb || "") || String(p.source || ""),
        tool: String(p.tool || ""),
      });
      n += 1;
    });
    return n;
  }

  function wbList() { return wbLoad(); }

  function wbRemove(id) {
    const target = String(id || "");
    if (!target) return false;
    const keep = wbLoad().filter((x) => x.id !== target);
    wbSave(keep);
    try { wbRender(); } catch (e) { /* 面板重建失败不影响"已经删掉了"这个事实 */ }
    wbPost(WB_RM, { ids: [target] }).catch(() => null);
    return true;
  }

  // 拖拽排序：按给定的 id 顺序重排。**只重排不增删** ——
  // 漏发的条目按原顺序接在后面（和 backend/canvas.py 同口径）。
  function wbReorder(orderedIds) {
    const ids = (orderedIds || []).map(String);
    if (!ids.length) return wbLoad();
    const all = wbLoad();
    const byId = {};
    all.forEach((x) => { if (!byId[x.id]) byId[x.id] = x; });
    const out = [];
    ids.forEach((i) => { if (byId[i]) { out.push(byId[i]); delete byId[i]; } });
    all.forEach((x) => { if (byId[x.id]) { out.push(byId[x.id]); delete byId[x.id]; } });
    wbSave(out);
    try { wbRender(); } catch (e) { /* 同上 */ }
    wbPost(WB_ORDER, { order: out.map((x) => x.id) }).catch(() => null);
    return out;
  }

  // 和后端那份对齐：后端有、本地没有的补进来（换浏览器 / 清了缓存也不丢东西）
  function wbHydrate() {
    return fetch(WB_LIST).then((r) => r.json()).then((d) => {
      const remote = (d && d.items) || [];
      if (!remote.length) return wbLoad();
      let local = wbLoad();
      const have = {};
      local.forEach((x) => { have[x.id] = true; });
      let n = 0;
      remote.forEach((it) => {
        const one = wbNormalize(it);
        if (have[one.id]) return;
        local = wbMerge(local, one).items;
        n += 1;
      });
      if (n) { wbSave(local); try { wbRender(); } catch (e) { /* 忽略 */ } }
      return local;
    }).catch(() => wbLoad());
  }

  function wbTimeText(ts) {
    const d = new Date(Number(ts) * 1000);
    if (!isFinite(d.getTime())) return "";
    const pad = (n) => (n < 10 ? "0" + n : "" + n);
    return (d.getMonth() + 1) + "-" + pad(d.getDate()) + " "
      + pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  // ---------- 工作台面板 ----------
  let wbPanel = null;
  let wbDragId = "";

  function wbEl(tag, cls, txt) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (txt !== undefined && txt !== null) el.textContent = String(txt);
    return el;
  }

  function wbBuild() {
    if (wbPanel) return wbPanel;
    const p = wbEl("section");
    p.id = "wbPanel";
    p.hidden = true;

    const head = wbEl("div", "wb-head");
    head.appendChild(wbEl("span", "wb-title", "🧳 工作台"));
    const cnt = wbEl("span", "wb-count", "0");
    cnt.id = "wbCount";
    head.appendChild(cnt);
    const x = wbEl("button", "wb-x", "✕");
    x.type = "button";
    x.title = "收起工作台";
    x.onclick = () => { p.hidden = true; };
    head.appendChild(x);
    p.appendChild(head);

    const bar = wbEl("div", "wb-bar");
    const goOn = wbEl("button", "wb-btn primary", "⟳ 接着上次");
    goOn.type = "button";
    goOn.id = "wbGoOn";
    goOn.title = "把上次的产出物带进输入框，接着说一句就继续做";
    goOn.onclick = wbContinue;
    bar.appendChild(goOn);
    const ex = wbEl("a", "wb-btn", "⬇ 批量导出");
    ex.href = WB_EXPORT;
    ex.title = "把工作台上的产出物打成一个 zip（带清单）";
    bar.appendChild(ex);
    const clr = wbEl("button", "wb-btn", "🧹 清空");
    clr.type = "button";
    clr.title = "清空工作台（本地和后端一起清）";
    clr.onclick = wbClear;
    bar.appendChild(clr);
    p.appendChild(bar);

    p.appendChild(wbEl("div", "wb-hint",
      "产出物关掉页面也在这儿。拖卡片左侧把手可排序，点卡片看内容。"));
    const body = wbEl("div", "wb-body");
    body.id = "wbBody";
    p.appendChild(body);
    document.body.appendChild(p);
    wbPanel = p;
    return p;
  }

  function wbOpen() {
    wbBuild();
    wbPanel.hidden = false;
    wbRender();
    wbHydrate();                    // 顺手和后端对一次（失败就当没有）
  }

  function wbToggle() {
    wbBuild();
    if (wbPanel.hidden) wbOpen();
    else wbPanel.hidden = true;
  }

  // 一张卡片的预览区：能内联预览的就内联，不能的给下载入口
  function wbPreviewInto(box, item) {
    box.textContent = "";
    const p = String(item.path || "");
    const kind = String(item.kind || "other");
    if (kind === "image") {
      const img = document.createElement("img");
      img.className = "wb-prev-img";
      img.alt = item.title || "产出物";
      img.src = item.thumb || ("/api/doclib/download?rel=" + encodeURIComponent(p));
      box.appendChild(img);
      return;
    }
    if (kind === "video" || kind === "audio") {
      const m = document.createElement(kind === "video" ? "video" : "audio");
      m.controls = true;
      m.className = "wb-prev-media";
      m.src = (kind === "video" && !p) ? (item.thumb || "") : ("/api/doclib/download?rel=" + encodeURIComponent(p));
      box.appendChild(m);
      return;
    }
    if (!p) {
      box.appendChild(wbEl("div", "wb-note", "这条只有记录，源文件不在了。"));
      return;
    }
    box.appendChild(wbEl("div", "wb-note", "正在读取内容…"));
    fetch("/api/doclib/file?rel=" + encodeURIComponent(p))
      .then((r) => r.json())
      .then((d) => {
        box.textContent = "";
        const pre = wbEl("div", "wb-prev-text");
        let text = String((d && d.text) || "");
        if (text.length > 4000) text = text.slice(0, 4000) + "\n…（只显示前 4000 字）";
        pre.textContent = text || "（这个文件里没有可抽取的文字，点下载看原文件）";
        box.appendChild(pre);
        const dl = wbEl("a", "wb-btn", "⬇ 下载原文件");
        dl.href = "/api/doclib/download?rel=" + encodeURIComponent(p);
        box.appendChild(dl);
      })
      .catch(() => {
        box.textContent = "";
        box.appendChild(wbEl("div", "wb-note", "读取失败，点下载看原文件。"));
      });
  }

  function wbCardEl(item) {
    const card = wbEl("div", "wb-card");
    card.setAttribute("data-id", item.id);
    card.draggable = true;

    const grip = wbEl("span", "wb-grip", "⠿");
    grip.title = "按住拖动排序";
    card.appendChild(grip);

    if (item.thumb && item.kind === "image") {
      const im = document.createElement("img");
      im.className = "wb-thumb";
      im.src = item.thumb;
      im.alt = item.title || "缩略图";
      card.appendChild(im);
    } else {
      card.appendChild(wbEl("span", "wb-ico", WB_KIND_ICO[item.kind] || "📦"));
    }

    const main = wbEl("div", "wb-main");
    const name = wbEl("div", "wb-name", item.title || item.path || "产出物");
    name.title = item.title || item.path || "";
    main.appendChild(name);
    const meta = wbEl("div", "wb-meta",
      (WB_KIND_ICO[item.kind] || "📦") + " " + item.kind
      + (item.ts ? " · " + wbTimeText(item.ts) : "")
      + (item.tool ? " · " + item.tool : ""));
    main.appendChild(meta);
    card.appendChild(main);

    const del = wbEl("button", "wb-del", "🗑");
    del.type = "button";
    del.title = "从工作台删掉";
    del.onclick = (e) => {
      e.stopPropagation();
      wbRemove(item.id);
      toast("已从工作台删掉：" + (item.title || ""), "ok");
    };
    card.appendChild(del);

    // 点卡片 → 展开 / 收起预览（拖拽刚结束时那一下不算点击）
    const prev = wbEl("div", "wb-prev");
    prev.hidden = true;
    card.appendChild(prev);
    card.addEventListener("click", (e) => {
      if (wbDragId) return;
      if (e.target && e.target.closest && e.target.closest("button,a")) return;
      if (prev.hidden) {
        prev.hidden = false;
        card.classList.add("open");
        wbPreviewInto(prev, item);
      } else {
        prev.hidden = true;
        card.classList.remove("open");
      }
    });
    return card;
  }

  function wbRender() {
    const list = wbLoad();
    if (!wbPanel) {
      const b = document.getElementById("wbBadge");
      if (b) { b.textContent = String(list.length); b.hidden = !list.length; }
      return list;
    }
    const body = document.getElementById("wbBody");
    const cnt = document.getElementById("wbCount");
    if (cnt) cnt.textContent = String(list.length);
    const badge = document.getElementById("wbBadge");
    if (badge) { badge.textContent = String(list.length); badge.hidden = !list.length; }
    if (!body) return list;
    body.textContent = "";
    if (!list.length) {
      body.appendChild(wbEl("div", "wb-empty",
        "还没有产出物。让助手画张图 / 做份 PPT / 剪段视频，做完就自动堆到这儿。"));
      return list;
    }
    list.forEach((it) => body.appendChild(wbCardEl(it)));

    // 拖拽排序：dragover 时先把 DOM 挪好（给用户实时反馈），
    // 松手（dragend）再一次性落盘 —— 免得拖一次往 localStorage 写几十遍。
    body.addEventListener("dragover", (e) => {
      if (!wbDragId) return;
      e.preventDefault();
      const over = e.target && e.target.closest ? e.target.closest(".wb-card") : null;
      if (!over || over.getAttribute("data-id") === wbDragId) return;
      const dragEl = body.querySelector('.wb-card[data-id="' + wbDragId + '"]');
      if (!dragEl) return;
      const rect = over.getBoundingClientRect();
      const after = (e.clientY - rect.top) > rect.height / 2;
      body.insertBefore(dragEl, after ? over.nextSibling : over);
    });
    return list;
  }

  // 接着上次没做完的：把上次那条带进输入框（不发出去 —— 用户还要补一句）
  function wbContinue() {
    const list = wbLoad();
    if (!list.length) { toast("工作台还是空的，先让助手做点东西", "warn"); return false; }
    const it = list[0];
    const txt = "接着上次工作台里的「" + (it.title || it.path || "产出物") + "」继续"
      + (it.path ? "（文件：" + it.path + "）" : "")
      + "，在这个基础上接着改 / 把它做完。";
    const inp = document.getElementById("input");
    if (inp) {
      inp.value = inp.value && inp.value.trim() ? (inp.value.replace(/\s+$/, "") + "\n" + txt) : txt;
      try { inp.focus(); } catch (e) { /* 拿不到焦点不影响内容已填入 */ }
      toast("已填进输入框 —— 补一句要改什么，按 ↵ 就接着干", "ok");
      return true;
    }
    toast(txt, "");
    return false;
  }

  function wbClear() {
    const list = wbLoad();
    if (!list.length) { toast("工作台已经是空的", ""); return false; }
    let ok = true;
    try { ok = window.confirm("清空工作台里的 " + list.length + " 条产出物？（只删记录，不动源文件）"); }
    catch (e) { ok = true; }
    if (!ok) return false;
    wbSave([]);
    wbRender();
    wbPost(WB_RM, { ids: list.map((x) => x.id) }).catch(() => null);
    toast("工作台已清空（源文件没删，还在生成文库里）", "ok");
    return true;
  }

  // 侧栏顶栏那一排快捷按钮里加一个「工作台」入口
  function wbMountButton() {
    const host = document.getElementById("topQuick");
    if (!host) { setTimeout(wbMountButton, 400); return; }   // 顶栏还没画出来，稍后再试
    if (document.getElementById("btnWorkbench")) return;
    const b = document.createElement("button");
    b.type = "button";
    b.className = "tq-btn";
    b.id = "btnWorkbench";
    b.title = "工作台：生成过的图 / 文档 / PPT / 视频都堆在这儿，关页面也还在";
    const ico = document.createElement("span");
    ico.className = "tq-ico";
    ico.textContent = "🧳";
    const txt = document.createElement("span");
    txt.className = "tq-txt";
    txt.textContent = "工作台";
    const badge = document.createElement("span");
    badge.className = "tq-badge";
    badge.id = "wbBadge";
    badge.hidden = true;
    b.appendChild(ico);
    b.appendChild(txt);
    b.appendChild(badge);
    b.onclick = wbToggle;
    host.appendChild(b);
    wbRender();                       // 把已有的条数先显示出来（刷新页面后立刻对得上）
  }

  // 入口：脚本在 app.js 之后加载（见 index.html），这里自己往侧栏挂按钮
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wbMountButton);
  } else {
    wbMountButton();
  }

  // [[WB-END]]

  window.__Artifacts = {
    begin: begin, chip: chip, flush: flush, openWithSystem: openWithSystem,
    split_blocks: split_blocks, block_context: block_context,
    mountBlocks: mountBlocks, handleOf: handleOf,
    // 画布卡片化回复：一块一块画，可拖、可点选「就这块改」
    CARD_KIND: CARD_KIND,
    canvasImgSrc: canvasImgSrc,
    canvasKeyFor: canvasKeyFor,
    canvasPickContext: canvasPickContext,
    loadCanvasPos: loadCanvasPos, saveCanvasPos: saveCanvasPos, resetCanvasPos: resetCanvasPos,
    renderCanvasCard: renderCanvasCard, renderCanvas: renderCanvas,
    // 常驻工作台（2026-11-21）：产出物堆在桌面上，关页面也还在
    WB_KEY: WB_KEY,
    add: add, addCards: addCards, list: wbList,
    remove: wbRemove, reorder: wbReorder, hydrate: wbHydrate,
    clear: wbClear, continueLast: wbContinue,
    open: wbOpen, toggle: wbToggle, renderBoard: wbRender,
  };
})();

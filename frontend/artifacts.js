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
  // 画布对话：回复变成可拖拽 / 可圈选的卡片（2026-11-16）
  // ------------------------------------------------------------
  // 痛点（用户实测计数）：
  //   · 「回复被截断」39 次 —— 长回复是一整条文字流，重点要滚半天找；
  //   · 「同一需求被重复发送」51 次 —— 只想改其中一块，却只能整段重新描述。
  // 做法：
  //   · renderCanvas(blocks) 按 block.type 分派：
  //       text  → renderTextBlock   （正文照旧走 markdown，长文可展开/收起，绝不截断）
  //       image → renderImageBlock  （缩略图 + 文件名 + 下载 / 直接打开）
  //       file  → renderFileBlock   （PPT / 文档 / 网页摘要都落在这一类）
  //     每个子函数返回一个带 data-block-id 的 .canvas-card 节点，
  //     由 renderCanvas 组装进 .canvas-grid（auto-fill 网格，窄屏自动一列）。
  //   · app.js 渲染助手正文前先 tryRenderCanvas(message)：成功就挂画布；
  //     失败 / 没块 → 返回 false，继续走原来的文字流渲染（宁可丑，不能丢内容）。
  //   · 圈选交互：点某张卡片 → 输入框预填「就这块：<blockId> 再改改」，
  //     省掉「把需求重新描述一遍」这一步。
  //   · 卡片可拖拽：拖到另一张卡上就换位置（只改 DOM 顺序，不动正文内容）。
  // 首版只做 text / image / file 三类；web 归到 file 块，未知类型按 text 兜底。
  // ============================================================
  const CANVAS_TAGS = { text: "文字", image: "图片", file: "文件", web: "网页" };

  // 归一化：后端给的 block 可能只有 {id,type,content}，缺 type/id 也要能画
  function normBlock(b, i) {
    const src = (b && typeof b === "object") ? b : { type: "text", content: String(b == null ? "" : b) };
    const t0 = String(src.type || "text").toLowerCase();
    let t = t0;
    if (["image", "img", "picture", "photo", "png", "jpg", "jpeg", "gif", "webp", "svg"].indexOf(t0) >= 0) t = "image";
    else if (["file", "ppt", "pptx", "key", "doc", "docx", "pdf", "xlsx", "xls", "csv", "zip",
              "md", "txt", "log", "py", "js", "json"].indexOf(t0) >= 0) t = "file";
    else if (["web", "link", "url", "site", "page"].indexOf(t0) >= 0) t = "web";
    else if (t0 !== "text") t = "text";
    return {
      id: String(src.id || ("b" + i)),
      type: t,
      raw_type: t0,
      content: String(src.content == null ? (src.text == null ? "" : src.text) : src.content),
      url: String(src.url || src.src || src.href || ""),
      name: String(src.name || src.title || ""),
    };
  }

  // 绝对地址 / 站内 api / 生成文库相对路径 → 可直接用的 src
  function srcOf(url, rel) {
    const u = String(url || "").trim();
    if (!u) return rel ? "/api/doclib/download?rel=" + encodeURIComponent(rel) : "";
    if (/^(https?:|data:|blob:)/i.test(u)) return u;
    if (u.charAt(0) === "/") return u;
    return "/api/doclib/download?rel=" + encodeURIComponent(u.replace(/^\.\//, ""));
  }

  // 从块内容里抠出「文件 / 图片 / 网页」需要的东西（名字、地址、后缀、图标）
  function fileInfoOf(b) {
    const raw = String(b.content || "").trim();
    let name = String(b.name || "").trim();
    let url = String(b.url || "").trim();
    const md = /^!?\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)$/.exec(raw);
    if (md) {
      if (!name) name = md[1];
      if (!url) url = md[2];
    }
    if (!url) {
      const bare = /(https?:\/\/[^\s)）】]+|\/api\/[^\s)）】]+)/.exec(raw);
      if (bare) url = bare[1];
    }
    if (!name) {
      const nm = /([^\s/\\]+\.(?:pptx?|key|docx?|pdf|xlsx?|csv|zip|md|txt|log|png|jpe?g|gif|webp|svg|py|js|json))$/i.exec(raw);
      if (nm) name = nm[1];
      else if (url) name = String(url).split("/").pop() || url;
    }
    if (!url && name && (/[\\/]/.test(name) || /\.[a-z0-9]{1,5}$/i.test(name))) {
      url = "/api/doclib/download?rel=" + encodeURIComponent(name.replace(/^\.?\//, ""));
    }
    const disp = name || "未命名";
    const ext = extOf(disp);
    const k = KINDS[ext] || ["📎", ext ? ext.toUpperCase() : "文件"];
    return { name: disp, url: url, rel: relOf(url), ext: ext, ico: k[0], kindName: k[1] };
  }

  // 卡片外壳：带 data-block-id / draggable，类型标签贴在右上角
  function canvasCard(b) {
    const node = document.createElement("div");
    node.className = "canvas-card canvas-card-" + b.type;
    node.setAttribute("data-block-id", b.id);
    node.setAttribute("data-block-type", b.type);
    node.setAttribute("draggable", "true");
    node.title = "点这块：输入框会预填「就这块：" + b.id + " 再改改」；也可以拖到别的位置";
    const tag = document.createElement("span");
    tag.className = "canvas-tag";
    tag.textContent = CANVAS_TAGS[b.type] || b.type;
    node.appendChild(tag);
    return node;
  }

  // 卡片里的小按钮 / 小链接（点它别触发整块的「圈选」）
  function canvasBtn(text, attrs, onClick) {
    const el = document.createElement(onClick ? "button" : "a");
    el.className = "canvas-btn";
    if (onClick) el.type = "button";
    Object.keys(attrs || {}).forEach((k) => { el.setAttribute(k, attrs[k]); });
    el.textContent = text;
    if (onClick) el.addEventListener("click", (e) => { e.stopPropagation(); onClick(e); });
    return el;
  }

  // ---------- 文字块 ----------
  function renderTextBlock(b) {
    const node = canvasCard(b);
    const src = String(b.content || "");
    const body = document.createElement("div");
    body.className = "canvas-text";
    body.innerHTML = window.__mdToHtml
      ? window.__mdToHtml(src)
      : esc(src).replace(/\r?\n/g, "<br>");
    node.appendChild(body);
    // 长文默认折叠（是「收起」，不是「截断」：点一下就看全，一个字都不丢）
    if (src.length > 900) {
      body.classList.add("cut");
      const more = document.createElement("button");
      more.type = "button";
      more.className = "canvas-btn canvas-more";
      more.textContent = "展开全文 ▾";
      more.addEventListener("click", (e) => {
        e.stopPropagation();                      // 别让「展开」被当成「圈选这块」
        const cut = body.classList.toggle("cut");
        more.textContent = cut ? "展开全文 ▾" : "收起 ▴";
      });
      node.appendChild(more);
    }
    return node;
  }

  // ---------- 图片块 ----------
  function renderImageBlock(b) {
    const node = canvasCard(b);
    const info = fileInfoOf(b);
    const fig = document.createElement("figure");
    fig.className = "canvas-fig";
    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = info.name || "图片";
    img.src = srcOf(info.url, info.rel || info.name);
    img.onerror = () => {
      const note = document.createElement("div");
      note.className = "canvas-note";
      note.textContent = "这张图没加载出来（可能还没生成好或已被删除）：" + (info.name || "");
      if (img.parentNode) img.parentNode.replaceChild(note, img);
    };
    fig.appendChild(img);
    if (info.name) {
      const cap = document.createElement("figcaption");
      cap.className = "canvas-cap";
      cap.textContent = info.name;
      fig.appendChild(cap);
    }
    node.appendChild(fig);
    const acts = document.createElement("div");
    acts.className = "canvas-acts";
    const view = window.__mobileViewUrl ? window.__mobileViewUrl(info.url || "") : srcOf(info.url, info.rel);
    if (info.url) acts.appendChild(canvasBtn("⬇ 下载", { href: view }, null));
    // 「直接打开」复用产物卡片已有的 document 级事件委托（见文件末尾）
    if (info.rel) acts.appendChild(canvasBtn("🗂 直接打开", { "data-art-openfile": info.rel }, null));
    if (acts.childNodes.length) node.appendChild(acts);
    return node;
  }

  // ---------- 文件块（PPT / 文档 / 网页摘要都走这里） ----------
  function renderFileBlock(b) {
    const node = canvasCard(b);
    const info = fileInfoOf(b);
    const isWeb = b.type === "web";
    const head = document.createElement("div");
    head.className = "canvas-file";
    const ico = document.createElement("span");
    ico.className = "canvas-ico";
    ico.textContent = isWeb ? "🌐" : info.ico;
    const main = document.createElement("span");
    main.className = "canvas-file-main";
    const nm = document.createElement("span");
    nm.className = "canvas-name";
    nm.textContent = info.name || (isWeb ? "网页链接" : "文件");
    const sub = document.createElement("span");
    sub.className = "canvas-sub";
    sub.textContent = isWeb ? (info.url || "网页") : info.kindName;
    main.appendChild(nm);
    main.appendChild(sub);
    head.appendChild(ico);
    head.appendChild(main);
    node.appendChild(head);

    const note = document.createElement("div");
    note.className = "canvas-note";
    const desc = String(b.content || "").replace(/\s+/g, " ").trim();
    note.textContent = isWeb
      ? (desc || info.url || "（这一块是个网页链接）")
      : (info.rel ? "生成文库：" + info.rel : (desc || "（这一块是个文件）"));
    node.appendChild(note);

    const acts = document.createElement("div");
    acts.className = "canvas-acts";
    if (isWeb) {
      if (info.url) {
        acts.appendChild(canvasBtn("🔗 打开网页", { href: info.url, target: "_blank", rel: "noopener" }, null));
      }
    } else {
      if (info.url) {
        const view = window.__mobileViewUrl ? window.__mobileViewUrl(info.url) : srcOf(info.url, info.rel);
        acts.appendChild(canvasBtn("⬇ 下载", { href: view }, null));
      }
      if (info.rel) acts.appendChild(canvasBtn("🗂 直接打开", { "data-art-openfile": info.rel }, null));
      if (info.rel && typeof window.__openInLibrary === "function") {
        acts.appendChild(canvasBtn("📂 在文件库中定位", {}, () => window.__openInLibrary(info.rel)));
      }
    }
    if (acts.childNodes.length) node.appendChild(acts);
    return node;
  }

  // 圈选再改：点卡片 → 输入框预填「就这块：<blockId> 再改改」
  function prefillRevise(blockId) {
    const id = String(blockId || "").trim();
    if (!id) return false;
    const inp = document.querySelector("#input")
      || document.querySelector(".composer textarea")
      || document.querySelector("textarea");
    if (!inp) return false;
    const text = "就这块：" + id + " 再改改";
    try {
      inp.value = text;
      inp.focus();
      if (typeof inp.setSelectionRange === "function") inp.setSelectionRange(text.length, text.length);
      // 触发一次 input，让 app.js 的 autoGrow 把输入框高度调回来
      inp.dispatchEvent(new Event("input", { bubbles: true }));
    } catch (e) { return false; }
    return true;
  }

  // 挂交互：圈选（click）+ 拖拽换位（dragstart / dragover / drop）
  function wireCanvas(grid, list) {
    let dragId = "";
    let moved = false;

    grid.addEventListener("click", (e) => {
      if (moved) { moved = false; return; }        // 刚拖过，不算「点选」
      const node = e.target.closest ? e.target.closest(".canvas-card") : null;
      if (!node || !grid.contains(node)) return;
      const id = node.getAttribute("data-block-id") || "";
      Array.prototype.forEach.call(grid.querySelectorAll(".canvas-card"), (n) => {
        if (n !== node) n.classList.remove("sel");
      });
      node.classList.add("sel");
      const ok = prefillRevise(id);
      if (window.__onBlockSelect) {
        try { window.__onBlockSelect({ blocks: list, selected: [id] }); }
        catch (err) { /* 外部回调出错不影响画布 */ }
      }
      if (!ok) toast("点到了「" + id + "」，但没找到输入框", "warn");
    });

    grid.addEventListener("dragstart", (e) => {
      const node = e.target.closest ? e.target.closest(".canvas-card") : null;
      if (!node) return;
      dragId = node.getAttribute("data-block-id") || "";
      node.classList.add("dragging");
      try {
        e.dataTransfer.setData("text/plain", dragId);
        e.dataTransfer.effectAllowed = "move";
      } catch (err) { /* 老浏览器没有 dataTransfer：拖拽降级，点选仍然可用 */ }
    });
    grid.addEventListener("dragover", (e) => {
      if (!dragId) return;
      const over = e.target.closest ? e.target.closest(".canvas-card") : null;
      if (!over || over.getAttribute("data-block-id") === dragId) return;
      e.preventDefault();                          // 必须 preventDefault，否则不会触发 drop
      try { e.dataTransfer.dropEffect = "move"; } catch (err) { /* 忽略 */ }
    });
    grid.addEventListener("drop", (e) => {
      if (!dragId) return;
      const over = e.target.closest ? e.target.closest(".canvas-card") : null;
      if (!over) return;
      e.preventDefault();
      const from = grid.querySelector('.canvas-card[data-block-id="' + dragId + '"]');
      if (from && from !== over) {
        // 落在卡片右半边就插到它后面、左半边插到前面 —— 拖一下就到想去的位置
        const r = over.getBoundingClientRect();
        const after = (e.clientX - r.left) > r.width / 2;
        grid.insertBefore(from, after ? over.nextSibling : over);
        moved = true;
      }
      dragId = "";
    });
    grid.addEventListener("dragend", () => {
      Array.prototype.forEach.call(grid.querySelectorAll(".canvas-card.dragging"), (n) => {
        n.classList.remove("dragging");
      });
      dragId = "";
      setTimeout(() => { moved = false; }, 0);
    });
  }

  // 主入口：blocks = [{id, type, content}]（后端 artifacts / split_blocks 的输出）
  // 返回组装好的 .canvas-grid 节点；没有可画的块就返回 null（调用方回退文字流）
  function renderCanvas(blocks) {
    const list = (Array.isArray(blocks) ? blocks : [])
      .map(normBlock)
      .filter((b) => String(b.content || "").trim() || b.url);
    if (!list.length) return null;
    const grid = document.createElement("div");
    grid.className = "canvas-grid";
    grid.setAttribute("data-block-count", String(list.length));
    list.forEach((b) => {
      let node = null;
      try {
        if (b.type === "image") node = renderImageBlock(b);
        else if (b.type === "file" || b.type === "web") node = renderFileBlock(b);
        else node = renderTextBlock(b);
      } catch (e) {
        // 单块画失败 → 按文字块兜底，绝不把这块内容吞掉
        try { console.warn("[canvas] 块渲染失败，按文字兜底：", b && b.id, e); } catch (e2) { /* 忽略 */ }
        node = renderTextBlock(Object.assign({}, b, { type: "text" }));
      }
      if (node) grid.appendChild(node);
    });
    if (!grid.childNodes.length) return null;
    wireCanvas(grid, list);
    return grid;
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

  window.__Artifacts = {
    begin: begin, chip: chip, flush: flush, openWithSystem: openWithSystem,
    split_blocks: split_blocks, block_context: block_context,
    mountBlocks: mountBlocks, handleOf: handleOf,
    // 画布卡片（2026-11-16）：回复按块渲染成可拖拽 / 可圈选的卡片
    renderCanvas: renderCanvas, renderTextBlock: renderTextBlock,
    renderImageBlock: renderImageBlock, renderFileBlock: renderFileBlock,
    prefillRevise: prefillRevise,
  };
})();

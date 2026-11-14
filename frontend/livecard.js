/* ------------------------------------------------------------------ *
 * LiveCard —— 单聊与群聊【共用】的实时卡片零件（2026-11-15）
 *
 * 为什么要有这个文件（用户原话）：
 *   「为什么前端单人和群聊模式不能沿用一套一样的代码？不就是换了个多人环境吗」
 *
 * 之前的做法是「让群聊照着单人抄一遍」：同一个 .sd-row 结构在 app.js 里
 * 存在两份（单人的 ensureSelfdevList / makeSelfdevCard，群聊的 hdEnsure /
 * hdAddCode），改一处另一处就走样 —— 表现就是「两边的思考卡、工具条、
 * 代码卡总有一边不对劲」，同一个 bug 修两遍。
 *
 * 现在把 5 个零件收成唯一一份实现，app.js 里的单聊和群聊都调它：
 *   1. buildThink       思考卡（标题 + 折叠体 + 状态文案 + 点击折叠）
 *   2. makeToolStrip    工具小条（横向 chip + 状态文案）
 *   3. makeCodeList     「🛠 本轮代码改动」折叠列表 + 单行 diff
 *   4. makeThinkPlayer  思考逐字播放（自适应步长，永不落后）
 *   5. makeWatchdog     卡死看门狗（长时间无事件提示 / 硬上限中断）
 *
 * 约定：
 *   · 本文件【不持有任何全局状态】。所有状态都在调用方拿到的对象里，
 *     所以单聊的多次对话、群聊的多人并发任务互不干扰。
 *   · 本文件【不碰业务】。不知道什么是 abort、不知道 sessionId、不弹 toast，
 *     需要外部动作的地方一律走 opts 回调传进来。
 *   · 生成的 DOM 结构 / class 名必须与旧实现逐字节一致 —— 单人模式的行为
 *     一点都不能变（用户明确要求），CSS 也不用动。
 * ------------------------------------------------------------------ */
window.LiveCard = (function () {
  "use strict";

  // ---------------------------------------------------------------- 基础

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function escAttr(s) {
    return esc(s).replace(/"/g, "&quot;");
  }

  // 工具名 → 中文展示。
  // ★ 这是【唯一一份】。app.js 里原来的 TOOL_LABELS 字面量已改成引用这里，
  //   所以单聊和群聊的工具小条永远不会再出现「一边中文一边英文」。
  const LABELS = {
    web_image_search: "🌐 联网搜图",
    save_image_to_library: "⭐ 保存图片",
    generate_image: "🎨 文生图",
    edit_image: "🖼 图片微改",
    compose_images: "🧩 拼接图片",
    cutout_image: "✂️ 抠图",
    composite_image: "🖼 合成图片",
    video_edit: "🎬 剪视频",
    tts_speak: "🔊 生成语音",
    avatar_speak: "🧑 数字人视频",
    list_directory: "📁 浏览目录",
    read_file: "📄 读取文件",
    search_files: "🔍 搜索文件",
    write_file: "✍️ 写入文件",
    append_file: "✎ 追加文件",
    remember: "🧠 更新记忆",
    search_memory: "💭 检索记忆",
    get_time: "🕐 获取时间",
    web_search: "🌐 联网搜索",
    web_read: "📖 读网页",
    library: "📚 生成文库",
    make_pptx: "📊 做 PPT",
    make_docx: "📝 写文档",
    make_xlsx: "📊 做表格",
    edit_office: "✏️ 改文档",
    map_plan: "🗺 规划路线",
    nearby_places: "📍 周边查找",
    get_weather: "🌤 查天气",
    // 自开发 / 工作区系列：原来没有中文名，工具小条里直接显示
    // `selfdev_grep` 这种英文 —— 群聊造软件时一排英文看着像日志。
    selfdev_tree: "📂 项目结构",
    selfdev_grep: "🔍 搜代码",
    selfdev_read: "📄 读源码",
    selfdev_write: "✍️ 写源码",
    selfdev_insert_after: "➕ 插入代码",
    selfdev_patch: "🩹 改源码",
    selfdev_apply: "🛠 代码模型重写",
    selfdev_run: "▶ 跑脚本",
    selfdev_restart: "🔄 重启应用",
    workspace_list: "📁 列项目文件",
    workspace_read: "📄 读项目文件",
    workspace_write: "✍️ 写项目文件",
    workspace_new_project: "➕ 新建项目",
    workspace_use_project: "🔀 切项目",
    workspace_run: "▶ 运行项目",
    workspace_pack: "📦 打包项目",
    workspace_mkdir: "📁 新建目录",
    workspace_move: "🔀 移动文件",
    workspace_delete: "🗑 删除文件",
    ask_user: "❓ 问用户",
  };

  function labelOf(name) {
    return LABELS[name] || ("🔧 " + name);
  }

  // 工具正在做什么 → 思考卡标题上的一句话（和旧 addToolChip 完全一致）
  function toolStatusText(name) {
    if (name === "generate_image") return "🎨 正在生成图片…";
    if (name === "edit_image") return "🖼 正在微改图片…";
    if (name === "remember" || name === "search_memory") return "🧠 正在读写记忆…";
    if (name === "web_search") return "🌐 正在联网检索…";
    // ⚠️ 这里用 LABELS[name] || name，不能用 labelOf()：
    //   labelOf 会给未知工具补一个 "🔧 " 前缀，拼出来就是「🔧 正在调用工具：🔧 xxx」。
    return "🔧 正在调用工具：" + (LABELS[name] || name);
  }

  // 折叠高度补间：卡片里有 display:none 的元素，纯 CSS 会「啪一下弹出来」。
  // 直接量「切换前 → 切换后」的真实高度，用 WAAPI 补间最稳。
  function animHeight(el, mutate, dur) {
    if (typeof dur !== "number") dur = 300;
    if (!el || typeof el.animate !== "function") { mutate(); return; }
    const h1 = el.getBoundingClientRect().height;
    el.classList.add("anim-h");     // 动画期间关掉子元素的 CSS 过渡，免得两边打架
    mutate();
    const h2 = el.getBoundingClientRect().height;
    if (Math.abs(h2 - h1) < 1.5) { el.classList.remove("anim-h"); return; }
    const a = el.animate(
      [{ height: h1 + "px" }, { height: h2 + "px" }],
      { duration: dur, easing: "cubic-bezier(.4,0,.2,1)" }
    );
    const done = () => el.classList.remove("anim-h");
    a.onfinish = done;
    a.oncancel = done;
  }

  // ---------------------------------------------------------- 1. 思考卡
  // DOM 与旧实现逐字节一致：
  //   div.msg.bot
  //     └ div.bubble.think-hdr → span(状态文案) + span.arrow
  //     └ div.think-body
  //
  // opts: { autoOpen: bool, initialBody: string }
  function buildThink(opts) {
    opts = opts || {};

    const wrap = document.createElement("div");
    wrap.className = "msg bot";

    const hdr = document.createElement("div");
    hdr.className = "bubble think-hdr";

    const status = document.createElement("span");
    status.textContent = "🧠 思考中…";

    const arrow = document.createElement("span");
    arrow.className = "arrow";
    arrow.textContent = opts.autoOpen ? "▾" : "▸";

    hdr.appendChild(status);
    hdr.appendChild(arrow);

    const body = document.createElement("div");
    body.className = "think-body";          // 默认 display:none（折叠）
    if (opts.autoOpen) body.classList.add("open");
    body.textContent = opts.initialBody != null ? opts.initialBody : "本地模型思考中…";

    wrap.appendChild(hdr);
    wrap.appendChild(body);

    const ctrl = {
      wrap: wrap, hdr: hdr, status: status, arrow: arrow, body: body,

      isOpen() { return body.classList.contains("open"); },

      setStatus(t) { status.textContent = t; },

      // 标题实时显示字数（单人「🧠 思考中…（N 字）」就是这个）
      setThinkingLen(n, suffix) {
        const tail = suffix || "";
        status.textContent = n
          ? "🧠 思考中…（" + n + " 字）" + tail
          : "🧠 思考中…" + tail;
      },

      // 正文逐字刷新时调它（带 ▌ 光标）
      paint(text) { body.textContent = text + "▌"; },

      // 收尾：去掉光标
      seal(text) { body.textContent = text; },

      toggle() {
        const open = body.classList.contains("open");
        body.classList.toggle("open", !open);
        arrow.textContent = open ? "▸" : "▾";
        return !open;       // true = 现在展开了
      },
    };

    // 点标题折叠/展开。展开时立刻把全文补上（旧行为：if (!open) body = text + "▌"）
    hdr.onclick = () => {
      const nowOpen = ctrl.toggle();
      if (nowOpen && typeof opts.getText === "function") {
        body.textContent = opts.getText() + "▌";
      }
    };

    return ctrl;
  }

  // --------------------------------------------------------- 2. 工具小条
  // div.tool-strip，每个工具一个 span.tool-chip
  function makeToolStrip() {
    const wrap = document.createElement("div");
    wrap.className = "tool-strip";
    let n = 0;

    return {
      wrap: wrap,

      // 加一个工具 chip，返回 chip 元素。onStatus 用来同步思考卡标题。
      add(name, onStatus) {
        const chip = document.createElement("span");
        chip.className = "tool-chip";
        chip.textContent = labelOf(name);
        wrap.appendChild(chip);
        if (typeof chip.scrollIntoView === "function") {
          try { chip.scrollIntoView({ block: "nearest" }); } catch (e) { /* ignore */ }
        }
        n += 1;
        if (typeof onStatus === "function") onStatus(toolStatusText(name));
        return chip;
      },

      // 放一个自定义按钮（群聊的「取消」就放这儿，和单人「■ 终止」同义）
      addButton(text, onclick, extraClass) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "tool-chip" + (extraClass ? " " + extraClass : "");
        b.textContent = text;
        b.onclick = onclick;
        wrap.appendChild(b);
        return b;
      },

      count() { return n; },
      clear() { wrap.textContent = ""; n = 0; },
    };
  }

  // ------------------------------------------------- 3. 代码改动列表（核心）
  // 单聊和群聊原来各写了一份，这是最典型的「复制粘贴走样」。
  // 现在只有这一份。

  const ACT_NAME = {
    apply: "代码模型重写",
    patch: "局部替换",
    project: "项目",
  };
  function actName(act) {
    return ACT_NAME[act] || "写入";
  }

  // 给 diff 上色：只做行首判断，不解析内容（和旧实现一致）
  function diffHtml(diffText) {
    return String(diffText).split("\n").map((ln) => {
      const e = esc(ln);
      if (ln.startsWith("+++") || ln.startsWith("---") || ln.startsWith("@@"))
        return '<span class="l-hdr">' + e + "</span>";
      if (ln.startsWith("+")) return '<span class="l-add">' + e + "</span>";
      if (ln.startsWith("-")) return '<span class="l-del">' + e + "</span>";
      return e;
    }).join("\n");
  }

  // 单行：div.sd-row（dataset.add / dataset.del + 头部摘要 + 可展开 diff）
  //
  // ui: { rel, act, added, removed, diff, who, backup, is_new }
  // hooks: { onRollback(rel, backup) }  —— 可选，只有单聊会传
  function buildCodeRow(ui, hooks) {
    ui = ui || {};
    hooks = hooks || {};

    const rel = String(ui.rel || "");
    const add = Number(ui.added || 0);
    const del = Number(ui.removed || 0);

    const row = document.createElement("div");
    row.className = "sd-row";
    row.dataset.add = add;
    row.dataset.del = del;

    const head = document.createElement("div");
    head.className = "sd-head";
    head.innerHTML =
      '<span class="sd-badge">🛠 ' + esc(actName(ui.act)) + "</span>" +
      '<span class="sd-rel" title="' + escAttr(rel) + '">' + esc(rel) + "</span>" +
      '<span class="sd-stat"><span class="sd-add">+' + add +
      '</span> <span class="sd-del">−' + del + "</span></span>" +
      '<button class="sd-toggle" type="button">详情 ▾</button>';
    row.appendChild(head);

    const body = document.createElement("div");
    body.className = "sd-body";
    body.hidden = true;

    const pre = document.createElement("pre");
    pre.className = "sd-pre";
    const diffText = String(ui.diff || "").trim() || "（没有差异内容）";
    pre.innerHTML = diffHtml(diffText);
    body.appendChild(pre);

    // 底部补充信息：只有单聊的 ui 会带 who / backup / is_new
    if (ui.who || ui.backup || ui.is_new) {
      const foot = document.createElement("div");
      foot.className = "sd-foot";
      foot.innerHTML =
        (ui.who ? "<span>模型：" + esc(ui.who) + "</span>" : "") +
        (ui.backup ? "<span>备份：" + esc(ui.backup) + "</span>" : "") +
        (ui.is_new ? "<span>（新文件，无改前版本）</span>" : "");
      if (ui.backup && typeof hooks.onRollback === "function") {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.textContent = "↩ 回撤这个文件";
        btn.onclick = () => hooks.onRollback(rel, ui.backup, btn);
        foot.appendChild(btn);
      }
      body.appendChild(foot);
    }

    row.appendChild(body);

    const tg = head.querySelector(".sd-toggle");
    // 抽成一个函数：小按钮和整行都调它。
    // ⚠️ 不要用 tg.click() —— 某些嵌入环境里元素没有 click()，会直接抛异常
    const toggleBody = () => {
      body.hidden = !body.hidden;
      tg.textContent = body.hidden ? "详情 ▾" : "收起 ▴";
    };
    tg.onclick = toggleBody;
    head.onclick = (ev) => { if (ev.target !== tg) toggleBody(); };

    return row;
  }

  // 整个列表：div.sd-list（头可整体收起，默认展开）
  function makeCodeList() {
    const box = document.createElement("div");
    box.className = "sd-list";

    const head = document.createElement("button");
    head.type = "button";
    head.className = "sd-list-head";
    head.innerHTML = '<span class="sd-list-arrow">▾</span>' +
      '<span class="sd-list-title">🛠 本轮代码改动</span>' +
      '<span class="sd-list-sum">0 处</span>';

    const body = document.createElement("div");
    body.className = "sd-list-body";

    box.appendChild(head);
    box.appendChild(body);
    box.__body = body;
    box.__head = head;

    head.onclick = () => animHeight(box, () => {
      const closed = box.classList.toggle("collapsed");
      body.hidden = closed;
      head.querySelector(".sd-list-arrow").textContent = closed ? "▸" : "▾";
    });

    const refreshSum = () => {
      const rows = body.querySelectorAll(".sd-row");
      let add = 0, del = 0;
      rows.forEach((r) => {
        add += Number(r.dataset.add || 0);
        del += Number(r.dataset.del || 0);
      });
      head.querySelector(".sd-list-sum").innerHTML =
        rows.length + ' 处 · <span class="sd-add">+' + add +
        '</span> <span class="sd-del">−' + del + "</span>";
    };

    return {
      box: box,
      head: head,
      body: body,
      refreshSum: refreshSum,

      // 加一行，返回 row 元素
      add(ui, hooks) {
        const row = buildCodeRow(ui, hooks);
        body.appendChild(row);
        refreshSum();
        // 整列被收起时来了新改动 → 自动展开，免得用户以为「没动静」
        if (box.classList.contains("collapsed")) {
          box.classList.remove("collapsed");
          body.hidden = false;
          head.querySelector(".sd-list-arrow").textContent = "▾";
        }
        return row;
      },

      count() { return body.querySelectorAll(".sd-row").length; },
    };
  }

  // ------------------------------------------------------- 4. 逐字播放
  // 问题：Ollama 对 qwen3-vl 等推理模型常把 thinking 一次性整体返回（非逐 token），
  //       不缓冲就会「和回答一起弹出来」。这里把到达的增量拆成小块逐步渲染。
  // ⚠️ 每帧字数【不能写死】：固定 5 字/26ms ≈ 190 字/秒，模型思考一旦比这快，
  //    待播队列就越积越长 —— 表现是「思考半天不动、结束前突然刷一大段」。
  //    改成跟着积压量自适应：积压越多每帧吐越多，既平滑又永远不落后。
  //
  // opts: { onTick(text) }  —— 每次吐出文字后回调
  function makeThinkPlayer(opts) {
    opts = opts || {};
    const STEP_MS = 26;
    let pending = "", text = "", timer = null;

    const emit = () => {
      if (typeof opts.onTick === "function") opts.onTick(text);
    };

    return {
      push(delta) {
        if (!delta) return;
        pending += delta;
        if (timer !== null) return;
        timer = setInterval(() => {
          if (!pending) return;
          const STEP = Math.max(5, Math.ceil(pending.length / 10));
          const take = Math.min(STEP, pending.length);
          text += pending.slice(0, take);
          pending = pending.slice(take);
          emit();
        }, STEP_MS);
      },

      // 停播：把剩下的立刻并进 text（和旧 stopThinking 一致，不额外 emit）
      stop() {
        if (timer !== null) { clearInterval(timer); timer = null; }
        if (pending) { text += pending; pending = ""; }
      },

      // 外部强制设定基准文本（后端 thinking_reset / done 兜底整段覆盖时用）。
      // 不清空 pending：那些是还没吐出来的增量，仍要接着播。
      set(t) { text = String(t == null ? "" : t); },

      text() { return text; },
      isRunning() { return timer !== null; },
      pendingLen() { return pending.length; },
    };
  }

  // --------------------------------------------------------- 5. 看门狗
  // 后端可能卡在工具里（比如等一个没人回答的确认框），这段时间界面看起来
  // "活着"其实什么都没发生。这里给两条线：长时间无事件提示 + 硬上限中断。
  //
  // opts: { isActive() -> bool, onIdle(), onHardAbort(),
  //         hardMs, idleMs, tickMs }
  function makeWatchdog(opts) {
    opts = opts || {};
    const hardMs = opts.hardMs != null ? opts.hardMs : 3600000;
    const idleMs = opts.idleMs != null ? opts.idleMs : 600000;
    const tickMs = opts.tickMs != null ? opts.tickMs : 5000;

    let t0 = 0, last = 0, timer = null, running = false;

    const stop = () => {
      if (timer !== null) { clearInterval(timer); timer = null; }
      running = false;
    };

    const start = () => {
      if (running) return;
      running = true;
      t0 = Date.now();
      last = Date.now();
      timer = setInterval(() => {
        if (typeof opts.isActive === "function" && !opts.isActive()) { stop(); return; }
        if (Date.now() - t0 > hardMs) {
          stop();
          if (typeof opts.onHardAbort === "function") opts.onHardAbort();
          return;
        }
        if (Date.now() - last < idleMs) return;
        stop();
        if (typeof opts.onIdle === "function") opts.onIdle();
      }, tickMs);
    };

    return {
      start: start,
      stop: stop,
      touch() { last = Date.now(); },      // 每收到一个事件调一次
      isRunning() { return running; },
    };
  }

  return {
    esc: esc,
    escAttr: escAttr,
    LABELS: LABELS,
    labelOf: labelOf,
    toolStatusText: toolStatusText,
    animHeight: animHeight,
    buildThink: buildThink,
    makeToolStrip: makeToolStrip,
    makeCodeList: makeCodeList,
    buildCodeRow: buildCodeRow,
    makeThinkPlayer: makeThinkPlayer,
    makeWatchdog: makeWatchdog,
  };
})();

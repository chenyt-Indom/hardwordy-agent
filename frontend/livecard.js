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

  // ------------------------------------------------- 4.5 时间线卡片
  // 会话时间轴（actlog.timeline）的竖向卡片渲染。
  // 每条显示 ts / status / summary，点击展开 diff_summary 与 regress_pass。
  //
  // 为什么放在 LiveCard 里：时间轴和实时卡片是同一套视觉语言（圆点 + 竖线），
  // 而且它要在单聊/群聊两个界面里都能用 —— 放这里就天然只有一份实现。
  //
  // opts:
  //   container  要渲染进去的容器节点（必填）
  //   items      actlog.timeline 返回的数组；不传则从 window.actlog 现取
  //   emptyText  空态提示文案
  //   onItemClick(item)  可选，点击某条时的额外回调（默认只做展开/收起）
  //
  // 返回 { render(items), refresh(), count() }，方便外部重新拉取。
  function renderTimeline(opts) {
    opts = opts || {};
    const box = opts.container;
    if (!box) return null;

    const EMPTY = opts.emptyText || "本会话暂无自动改动";

    // 状态 → 视觉档位。后端可能给 ok/fail/rollback 或 success/error 等别名，
    // 这里统一归一化，避免「同一个失败态在时间轴里显示成两种颜色」。
    function statusClass(st) {
      const s = String(st == null ? "" : st).toLowerCase();
      if (s === "ok" || s === "success" || s === "done" || s === "pass") return "ok";
      if (s === "fail" || s === "failed" || s === "error" || s === "err") return "fail";
      if (s === "rollback" || s === "revert" || s === "reverted") return "rollback";
      return "";
    }
    function statusText(st) {
      const c = statusClass(st);
      if (c === "ok") return "成功";
      if (c === "fail") return "失败";
      if (c === "rollback") return "回滚";
      return String(st == null ? "" : st) || "—";
    }

    // ts 可能是秒级/毫秒级时间戳，也可能是已经格式化好的字符串。
    function fmtTs(ts) {
      if (ts == null || ts === "") return "";
      if (typeof ts === "string" && !/^\d+$/.test(ts)) return ts;
      let n = Number(ts);
      if (!isFinite(n)) return String(ts);
      if (n < 1e12) n *= 1000;          // 秒 → 毫秒
      const d = new Date(n);
      if (isNaN(d.getTime())) return String(ts);
      const p = (x) => (x < 10 ? "0" + x : "" + x);
      return p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " +
             p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
    }

    function buildItem(it) {
      it = it || {};
      const cls = statusClass(it.status);

      const row = document.createElement("div");
      row.className = "tl-item" + (cls ? " " + cls : "");

      const dot = document.createElement("span");
      dot.className = "tl-dot";
      row.appendChild(dot);

      const body = document.createElement("div");
      body.className = "tl-body";

      const sum = document.createElement("div");
      sum.className = "tl-sum";
      const time = fmtTs(it.ts);
      if (time) {
        const t = document.createElement("span");
        t.className = "tl-time";
        t.textContent = time;
        sum.appendChild(t);
      }
      const st = document.createElement("span");
      st.className = "tl-status";
      st.textContent = statusText(it.status);
      sum.appendChild(st);
      const txt = document.createElement("span");
      txt.className = "tl-text";
      txt.textContent = " " + String(it.summary == null ? "" : it.summary);
      sum.appendChild(txt);
      body.appendChild(sum);

      // 详情：diff_summary 与 regress_pass。两者都没有就不做展开，
      // 免得点一下只弹出一个空盒子。
      const hasDiff = it.diff_summary != null && String(it.diff_summary).trim() !== "";
      const hasReg = it.regress_pass != null && it.regress_pass !== "";
      if (hasDiff || hasReg) {
        const det = document.createElement("div");
        det.className = "tl-detail";
        det.hidden = true;

        if (hasDiff) {
          const d = document.createElement("div");
          d.className = "tl-diff";
          d.textContent = String(it.diff_summary);
          det.appendChild(d);
        }
        if (hasReg) {
          const r = document.createElement("div");
          r.className = "tl-regress";
          const pass = it.regress_pass === true || it.regress_pass === "true" ||
                       it.regress_pass === 1 || it.regress_pass === "1";
          r.textContent = "回归测试：" + (pass ? "通过 ✓" : "未通过 ✗");
          r.classList.add(pass ? "ok" : "fail");
          det.appendChild(r);
        }
        body.appendChild(det);

        row.classList.add("expandable");
        row.onclick = () => {
          det.hidden = !det.hidden;
          row.classList.toggle("open", !det.hidden);
          if (typeof opts.onItemClick === "function") opts.onItemClick(it);
        };
      } else if (typeof opts.onItemClick === "function") {
        row.onclick = () => opts.onItemClick(it);
      }

      row.appendChild(body);
      return row;
    }

    const api = {
      render(items) {
        let list = items;
        if (!Array.isArray(list)) {
          try {
            list = (window.actlog && typeof window.actlog.timeline === "function")
              ? window.actlog.timeline() : [];
          } catch (e) { list = []; }
        }
        box.textContent = "";
        if (!list || !list.length) {
          const empty = document.createElement("div");
          empty.className = "tl-empty";
          empty.textContent = EMPTY;
          box.appendChild(empty);
          return 0;
        }
        // 新的在上：时间轴通常按时间正序返回，倒过来更符合「最近发生了什么」
        const arr = list.slice().reverse();
        arr.forEach((it) => box.appendChild(buildItem(it)));
        return arr.length;
      },
      refresh() { return api.render(); },
      count() { return box.querySelectorAll(".tl-item").length; },
    };

    api.render(opts.items);
    return api;
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

  // --------------------------------------------- 6. 会话卡（整轮，唯一一份）
  // （时间线卡片见上面的 renderTimeline，它不依赖会话卡，可单独调用）
  // 单聊与群聊【唯一】的一轮卡片：思考卡 → 工具小条 → 媒体 → 正文 → 🛠 代码改动。
  //
  // 为什么要有它：上面 5 个只是零件。「按什么顺序拼、什么时候展开、标题怎么写、
  // 丢帧怎么纠正」这套**编排**原来两边各写一遍（单聊 doSend 里 200 行、
  // 群聊 hdEnsure + hdStream + hdAddCode 320 行），于是同一个 bug 修两遍、
  // 风格总有一边不对 —— 用户原话「不就是换了个多人环境吗」。
  //
  // opts:
  //   autoOpen  思考框默认展开（手机端 true）
  //   getText   点击展开时补全文的回调
  //   onTick    思考逐字播放时回调（单聊用它同步 thinking 变量）
  //   onStatus  标题文案变化回调
  //   withWho   是否带「谁唤醒了模型」那一行（只有群聊 true）
  function makeSession(opts) {
    opts = opts || {};

    const think = buildThink({ autoOpen: !!opts.autoOpen, getText: opts.getText });
    const tools = makeToolStrip();

    const mediaWrap = document.createElement("div");
    mediaWrap.className = "media-strip";

    const answerWrap = document.createElement("div");
    answerWrap.className = "msg bot";
    const answerBubble = document.createElement("div");
    answerBubble.className = "bubble";
    answerBubble.textContent = "…";
    answerWrap.appendChild(answerBubble);

    let who = null;
    if (opts.withWho) {
      who = document.createElement("div");
      who.className = "hmsg-who";
    }

    let codeList = null;
    let raw = "", painted = "", sealed = false, lblT = 0, answerStarted = false;
    // ★ 2026-11-15 用户要求「流式正文也按 markdown 输出」。
    //   但每帧都整块解析 markdown 会很贵（上次「响应太慢」的同类问题），所以：
    //   · 正文先在 outRaw 里累积，paintAnswer() 按 300ms 节流渲染；
    //   · 收尾（done / seal）时强制立刻渲染一次，保证最终观感；
    //   · 内容没变化（outRaw === outPainted）直接跳过，不白跑一次解析。
    let outRaw = "", outPainted = "", outTimer = 0;

    // 标题文案。★ 「（N 字）」每帧都变，而改标题会让**整条消息列表重排** ——
    //   长列表时那是 6.7 次/秒的整页重排（用户报「响应太慢」的直接原因之一）。
    //   字数类 400ms 节流，状态类（正在调用工具…）立即显示。
    const setLabel = (tx, force) => {
      const el = think.status;
      if (!el || el.textContent === tx) return;
      if (!force && /字）/.test(tx)) {
        const now = (window.performance || Date).now();
        if (now - lblT < 400) return;
        lblT = now;
      }
      el.textContent = tx;
      if (typeof opts.onStatus === "function") opts.onStatus(tx);
    };

    const openThink = () => {
      if (!think.body.classList.contains("open")) {
        think.body.classList.add("open");
        think.arrow.textContent = "▾";
      }
    };

    // 思考正文：**增量追加**（O(1)）；和已有内容对不上才整块重设。
    // 原来每帧 `textContent = 全文 + "▌"`，思考到 5000 字时每秒重排 3 万字。
    // 光标做成独立 span，追加时插到它前面 —— 空卡片仍能靠 :empty 规则隐藏。
    // ★ 2026-11-19 用户要求「跟随思考」：开启时每帧把思考正文滚到最底部。
    //   用户手动上滚（滚轮 / 触摸 / 拖滚动条）后 1.2 秒内不抢滚动位置 ——
    //   否则想往上翻一眼历史，每帧都被拽回底部，根本没法看。
    //   开关由 app.js 写进 window.__thinkScrollFollow；单聊和群聊共用这一份绘制。
    let _userScrollAt = 0;
    const markUserScroll = () => { _userScrollAt = Date.now(); };
    ["wheel", "touchmove", "pointerdown"].forEach((ev) => {
      try { think.body.addEventListener(ev, markUserScroll, { passive: true }); }
      catch (e) { /* 老浏览器不支持 passive 选项 */ }
    });
    const followThinkScroll = () => {
      const body = think.body;
      if (!body.classList.contains("open")) return;             // 折叠着看不见，不用滚
      if (!window.__thinkScrollFollow) return;                  // 用户关掉了开关
      if (Date.now() - _userScrollAt < 1200) return;            // 用户正在手动翻，别抢
      if (body.scrollHeight <= body.clientHeight + 1) return;   // 内容没超出一屏
      body.scrollTop = body.scrollHeight;
    };

    const paintThink = (text, withCursor) => {
      const body = think.body;
      if (!body.classList.contains("open")) return;
      let cur = body.querySelector(".lc-cur");
      if (!cur) {
        cur = document.createElement("span");
        cur.className = "stream-cursor lc-cur";
        body.appendChild(cur);
      }
      cur.textContent = withCursor === false ? "" : "▌";
      // ★ 2026-11-15 真 bug（真实浏览器实测抓到）：buildThink 会给思考体放一句
      //   占位文案「本地模型思考中…」。单聊原来每帧 `textContent = 全文` 会把它
      //   整个盖掉；改成增量追加后，占位会**永久留在最前面**，实际显示成
      //   「本地模型思考中…真正的思考内容」。首次绘制时先清掉它。
      if (!painted) {
        Array.from(body.childNodes).forEach((n) => { if (n !== cur) body.removeChild(n); });
      }
      if (text.length >= painted.length && text.indexOf(painted) === 0) {
        const add = text.slice(painted.length);
        if (add) cur.insertAdjacentText("beforebegin", add);
      } else {
        Array.from(body.childNodes).forEach((n) => { if (n !== cur) body.removeChild(n); });
        if (text) cur.insertAdjacentText("beforebegin", text);
      }
      painted = text;
      followThinkScroll();      // 跟随思考：滚到最新一句（开关关闭时内部直接返回）
    };

    const player = makeThinkPlayer({
      onTick: (text) => {
        raw = text;
        paintThink(text, true);
        if (typeof opts.onTick === "function") opts.onTick(text);
      },
    });

    const api = {
      thinkWrap: think.wrap, think: think,
      toolWrap: tools.wrap, tools: tools,
      mediaWrap: mediaWrap,
      answerWrap: answerWrap, answerBubble: answerBubble,
      who: who, player: player,

      // 按单聊的顺序挂进消息流（群聊也用同一份，所以两边结构完全一致）
      mount(box) {
        if (who && who.parentNode !== box) box.appendChild(who);
        if (think.wrap.parentNode !== box) box.appendChild(think.wrap);
        if (tools.wrap.parentNode !== box) box.appendChild(tools.wrap);
        if (mediaWrap.parentNode !== box) box.appendChild(mediaWrap);
        if (answerWrap.parentNode !== box) box.appendChild(answerWrap);
      },
      isMounted(box) {
        return think.wrap.parentNode === box && answerWrap.parentNode === box;
      },

      setWho(t) { if (who) who.textContent = t; },
      setLabel: setLabel,
      openThink: openThink,

      addTool(name) { tools.add(name, (t) => setLabel(t, true)); },
      addToolButton(text, onclick, cls) { return tools.addButton(text, onclick, cls); },

      pushThinking(delta) { player.push(delta); },
      setThinking(text) { player.set(text); raw = String(text || ""); },
      thinkingText() { return raw; },
      isSealed() { return sealed; },

      // 收尾：停播 + 去掉光标 + 标题带字数（正文一开始输出就该调）
      sealThinking() {
        player.stop();
        raw = player.text();
        sealed = true;
        openThink();
        paintThink(raw, false);
        const cur = think.body.querySelector(".lc-cur");
        if (cur) cur.remove();
        if (raw) setLabel("🧠 思考过程（" + raw.length + " 字）", true);
      },

      // 代码改动列表按需创建（第一次有改动才挂进卡片）
      ensureCodeList() {
        if (!codeList) {
          codeList = makeCodeList();
          answerWrap.appendChild(codeList.box);
        }
        return codeList;
      },
      addCode(ui, hooks) {
        return api.ensureCodeList().add(ui || {}, hooks);
      },
      codeCount() { return codeList ? codeList.count() : 0; },

      setAnswer(text) {
        answerBubble.textContent = text == null ? "" : text;
        answerBubble.style.whiteSpace = "pre-wrap";
      },
      appendAnswerHtml(html) { answerBubble.insertAdjacentHTML("beforeend", html); },
      overwriteAnswerHtml(html) { answerBubble.innerHTML = html; },

      // 正文按 markdown 渲染（节流）。force=true 时立刻渲染并清掉待办。
      // mdToHtml 在 app.js 的 IIFE 里，靠 window.__mdToHtml 拿到；
      // 拿不到时退回纯文本 + <br>，和改动前的行为一致，不会白屏。
      answerText() { return outRaw; },
      paintAnswer(force) {
        if (outTimer) { clearTimeout(outTimer); outTimer = 0; }
        if (!force && outRaw === outPainted) return;
        outPainted = outRaw;
        const md = window.__mdToHtml;
        if (typeof md === "function") {
          try {
            // markdown 产出的是 <p>/<ul> 这类块级元素，父容器的 pre-wrap 会把
            // 每行缩进和空行一起保留下来，排版会散架 —— 这里显式清掉。
            answerBubble.style.whiteSpace = "";
            api.overwriteAnswerHtml(md(outRaw));
            return;
          } catch (e) { /* 解析失败就退回纯文本 */ }
        }
        answerBubble.style.whiteSpace = "pre-wrap";
        api.overwriteAnswerHtml(esc(outRaw).replace(/\r?\n/g, "<br>"));
      },
      // 流式追加：只累积，渲染交给节流
      pushAnswer(text, full) {
        outRaw = (typeof full === "string" && full) ? full : (outRaw + String(text == null ? "" : text));
        if (!answerStarted) { answerStarted = true; answerBubble.textContent = ""; }
        if (!outTimer) outTimer = setTimeout(() => { outTimer = 0; api.paintAnswer(false); }, 300);
      },

      destroy() {
        try { player.stop(); } catch (e) { /* ignore */ }
        if (outTimer) { clearTimeout(outTimer); outTimer = 0; }
        [who, think.wrap, tools.wrap, mediaWrap, answerWrap].forEach((n) => {
          try { if (n) n.remove(); } catch (e) { /* ignore */ }
        });
      },

      // 流式帧 → 卡片。群聊的「多一个广播」只体现在：帧从网上来、而发起人不是自己。
      // 其余表现（标题、光标、工具 chip、代码列表、收尾）与单聊完全同一份代码。
      applyFrame(f) {
        const t = f.task || {};
        const ph = f.phase;

        if (ph === "queued" || ph === "start") {
          setLabel("🧠 思考中…", true);
          if (typeof opts.whoText === "function") api.setWho(opts.whoText(t, ph));
          return ph;
        }
        if (ph === "thinking") {
          setLabel("🧠 思考中…（" + (t.chars || 0) + " 字）");
          openThink();
          const full = (typeof f.full === "string" && f.full) ? f.full : "";
          const dlt = String(f.delta || "");
          if (full) {
            if (full === raw) { /* 没变化 */ }
            else if (full.indexOf(raw) === 0) { api.pushThinking(full.slice(raw.length)); }
            else { api.setThinking(full); raw = full; paintThink(full, true); }
          } else if (dlt) {
            api.pushThinking(dlt);
          }
          return ph;
        }
        if (ph === "tool") {
          const names = (t.tools || []).map(String).filter(Boolean);
          names.forEach((nm) => api.addTool(nm));
          return ph;
        }
        if (ph === "code") {
          const code = t.code || {};
          const rel = String(code.rel || code.project || "").trim();
          if (rel) api.addCode(code);
          return ph;
        }
        if (ph === "content") {
          if (!sealed && raw) api.sealThinking();
          // 累积 + 节流渲染 markdown（不再每帧整块重建纯文本）
          api.pushAnswer(f.delta || "", f.full);
          return ph;
        }
        if (ph === "note") {
          const tx = String(f.delta || "").trim();
          if (tx) setLabel(tx.slice(0, 120), true);
          return ph;
        }
        if (ph === "done") {
          if (!sealed && raw) api.sealThinking();
          // 收尾必须立刻渲染，否则节流窗口里最后一截正文不会出现
          if (outRaw) api.paintAnswer(true);
          return ph;
        }
        return ph;
      },
    };

    return api;
  }

  // ------------------------------------------------------- 6. 账单速览卡
  //
  // 为什么要有它（用户 2026-11-17 连续两轮）：
  //   「现在比以前能大概省下多少？」「查看账单」—— 我两次给的都是排查过程，
  //   正文还被 max_tokens 截在半句。数字本来就在账本里（data/llm_usage），
  //   不需要模型转述，所以做成一张固定卡片：**顶部一句结论，明细默认折叠**。
  //
  // 数据来自后端 /api/usage/summary（= usage_ledger.summarize 的返回值）。
  // 约定：renderBillCard 只吃一个 plain object、返回 **HTML 字符串**，
  //       不碰 document —— 这样 node 里也能直接跑它做断言（无 DOM 依赖）。

  function fmtTokens(n) {
    n = Number(n || 0);
    if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
    return String(n);
  }

  // 0.195 → "+20%"；-0.043 → "-4%"；0 → "+0%"
  function fmtDelta(v) {
    const x = Number(v || 0) * 100;
    return (x >= 0 ? "+" : "") + x.toFixed(0) + "%";
  }

  // 一句话结论：有「今天」就用今天的，否则退回区间合计（老数据/测试桩都吃得下）
  function billHeadline(data) {
    data = data || {};
    const t = data.today || {};
    const calls = Number(t.calls != null ? t.calls : (data.total_calls || 0));
    const tokens = Number(t.tokens != null ? t.tokens : (data.total_tokens || 0));
    return "今日 " + calls + " 次调用 / " + fmtTokens(tokens) + " token / 较昨日 "
      + fmtDelta(data.delta_vs_prev);
  }

  // 明细行：优先用后端给的 *_detail（带 token / 花销），退回纯次数表
  function billRows(data, kind) {
    const det = data[kind + "_detail"];
    const keys = kind === "tag" ? "tag" : "hour";
    if (Array.isArray(det) && det.length) {
      return det.map((r) => {
        const name = keys === "hour"
          ? String(r.hour).padStart(2, "0") + ":00"
          : String(r.tag == null ? "未标注" : r.tag);
        const tok = Number(r.t || 0);
        const cost = Number(r.cost || 0);
        return '<div class="bill-row"><span class="bill-k">' + esc(name) + "</span>"
          + '<span class="bill-v">' + Number(r.calls || 0) + " 次"
          + (tok ? " · " + fmtTokens(tok) + " token" : "")
          + (cost ? " · ¥" + cost.toFixed(2) : "")
          + "</span></div>";
      }).join("");
    }
    const raw = data[kind === "tag" ? "by_tag" : "by_hour"] || {};
    // 后端给的是 {标签: 次数} 的字典；有人塞 [{tag, calls, tokens}] 也照吃
    if (Array.isArray(raw)) {
      return raw.map((r) => {
        r = r || {};
        const k = kind === "hour" ? r.hour : r.tag;
        const name = kind === "hour"
          ? String(k).padStart(2, "0") + ":00"
          : String(k == null ? "未标注" : k);
        const tok = Number(r.t || r.tokens || 0);
        const n = Number(r.calls || r.n || 0);
        return '<div class="bill-row"><span class="bill-k">' + esc(name) + "</span>"
          + '<span class="bill-v">' + n + " 次"
          + (tok ? " · " + fmtTokens(tok) + " token" : "") + "</span></div>";
      }).join("");
    }
    const ks = Object.keys(raw);
    if (!ks.length) return "";
    // 按标签：次数多的在前；按时段：按钟点顺序
    if (kind === "tag") ks.sort((a, b) => Number(raw[b] || 0) - Number(raw[a] || 0));
    else ks.sort((a, b) => Number(a) - Number(b));
    return ks.map((k) => {
      const name = kind === "hour"
        ? String(k).padStart(2, "0") + ":00"
        : String(k);
      return '<div class="bill-row"><span class="bill-k">' + esc(name) + "</span>"
        + '<span class="bill-v">' + Number(raw[k] || 0) + " 次</span></div>";
    }).join("");
  }

  function billSection(data, kind, title) {
    const body = billRows(data, kind);
    if (!body) return "";
    // ⚠️ 两个刻意的写法：
    //   ① <details> 上**不加任何属性**（class 挪到外层 div）—— 折叠节点保持
    //      「裸」的 <details>，任何按字符串找折叠节点的检查/插件都不会漏；
    //   ② 绝不写 <details open> —— 明细默认折叠，用户先看到的必须是结论那一行
    //      （「被截断 / 答非所问」就是从这儿修的）。
    return '<div class="bill-sec"><details><summary>' + esc(title) + "</summary>"
      + '<div class="bill-body">' + body + "</div></details></div>";
  }

  function renderBillCard(data) {
    data = data || {};
    const head = billHeadline(data);
    const cost = Number(data.weighted_cost || 0);
    const calls = Number(data.total_calls || 0);
    // 副行只放「一句结论之外」的补充：加权成本（TEI 口径）+ 区间调用数
    const sub = (cost > 0 ? "≈ ¥" + cost.toFixed(2) + " 加权成本" : "加权成本：暂无计费数据")
      + (data.days ? " · 近 " + data.days + " 天合计 " + calls + " 次调用" : "");

    return '<div class="bill-card">'
      + '<div class="bill-head"><b>' + esc(head) + "</b></div>"
      + '<div class="bill-sub">' + esc(sub) + "</div>"
      + billSection(data, "tag", "按标签")
      + billSection(data, "hour", "按时段")
      + "</div>";
  }

  // 挂到容器上（渲染入口只有这一个，app.js 不用知道内部结构）
  function mountBillCard(el, data) {
    if (!el) return null;
    el.innerHTML = renderBillCard(data);
    return el;
  }

  // ------------------------------------------------------- 7. 画布即对话
  //
  // 为什么要有它（用户 2026-11-17，摩擦点「同一需求被重复发送 20 次」）：
  //   回复是一整块文字流，用户想改「这张图的右下角 / 这个表格的第二行」，
  //   只能重新打一大段字去描述位置 —— 所以同一件事被反复发。
  // 做法：
  //   · 把一轮回复拆成若干卡片，**绝对定位**摆在一块画布上（.lc-canvas）；
  //   · 每张卡片的标题条是拖动把手（pointerdown / pointermove / pointerup），
  //     卡片正文里按住拖 = 圈选一块矩形（mousedown / mousemove / mouseup）；
  //   · 圈完（或单击=选整块）回调 { cardId, rect:{x,y,w,h} }，
  //     app.js 把它拼进下一条用户消息 —— 圈哪问哪，少打一半字。
  // 约定：这里只造 DOM，不持有全局状态、不碰业务。选中结果交给 opts.onPick。

  const CANVAS_KIND = {
    text: "📝 文字",
    table: "📊 表格",
    image: "🖼 图片",
    file: "📎 文件",
    ppt: "📊 PPT",
    web: "🌐 网页",
    map: "🗺 地图",
  };

  // markdown 图片语法 → 真正能显示的地址。
  // 站内文件（data/library/x.png、image_library/…）走后端下载口，否则会 404。
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

  // 把选中结果说成一句人能读懂、模型也能读懂的话（拼进下一条用户消息）
  function canvasContext(pick) {
    if (!pick || !pick.cardId) return "";
    const r = pick.rect || { x: 0, y: 0, w: 0, h: 0 };
    const tail = String(pick.content == null ? "" : pick.content)
      .replace(/\s+/g, " ").trim().slice(0, 300);
    return "〔画布圈选〕卡片 cardId=" + pick.cardId
      + "（" + (pick.type || "text") + "）"
      + " 选区 x=" + Math.round(Number(r.x) || 0)
      + ",y=" + Math.round(Number(r.y) || 0)
      + ",w=" + Math.round(Number(r.w) || 0)
      + ",h=" + Math.round(Number(r.h) || 0)
      + (pick.how ? "（" + pick.how + "）" : "")
      + (tail ? "；这块的原文：" + tail : "");
  }

  // cards: [{ id, type, content }]
  // opts:  { onPick(pick, canvasEl) }
  // 返回：画布 DOM（.lc-canvas）；没有卡片时返回 null（调用方按旧逻辑走）
  function renderCanvas(cards, opts) {
    opts = opts || {};
    const list = (Array.isArray(cards) ? cards : []).filter(function (c) {
      return c && String(c.content == null ? "" : c.content).trim();
    });
    if (!list.length) return null;

    const grid = document.createElement("div");
    grid.className = "lc-canvas" + (list.length === 1 ? " single" : "");

    const hd = document.createElement("div");
    hd.className = "lc-canvas-hd";
    const hint = document.createElement("span");
    hint.className = "lc-canvas-hint";
    hint.textContent = "🖼 画布视图 · 拖标题条移动卡片，在卡片里按住拖一个方框就能圈选追问";
    hd.appendChild(hint);
    grid.appendChild(hd);

    const els = [];

    function frame() {
      // 容器高度跟着卡片走（卡片是绝对定位，不撑不开父元素）
      let bottom = 0;
      els.forEach(function (el) {
        bottom = Math.max(bottom, el.offsetTop + el.offsetHeight);
      });
      if (!bottom) bottom = 8 + els.length * 120;      // 还没有布局信息（首帧）时的估计
      grid.style.height = (bottom + 8) + "px";
    }

    function pickCard(el, rect, how) {
      const info = el.__card || {};
      const pick = {
        cardId: info.cardId || "",
        type: info.type || "text",
        content: info.content || "",
        how: how || "区域",
        rect: rect,
      };
      grid.__canvasPick = pick;
      els.forEach(function (one) { one.classList.toggle("sel", one === el); });
      hint.textContent = "🎯 已圈选 " + pick.cardId + "（" + pick.how + " · "
        + Math.round(rect.w) + "×" + Math.round(rect.h)
        + "）—— 直接说要怎么改，发送时会带上这块";
      if (typeof opts.onPick === "function") {
        try { opts.onPick(pick, grid); } catch (e) { /* 回调炸了不能影响圈选本身 */ }
      }
      return pick;
    }

    list.forEach(function (card, i) {
      const cardId = String(card.id || card.cardId || ("c" + i));
      const type = String(card.type || "text");
      const content = String(card.content == null ? "" : card.content);

      const el = document.createElement("div");
      el.className = "lc-card lc-" + type;
      el.setAttribute("data-card-id", cardId);
      el.setAttribute("data-card-type", type);
      el.__card = { cardId: cardId, type: type, content: content };

      // ---- 标题条 = 拖动把手 ----
      const bar = document.createElement("div");
      bar.className = "lc-card-hd";
      bar.innerHTML = '<span class="lc-card-kind">' + esc(CANVAS_KIND[type] || "📝 内容")
        + "</span>" + '<span class="lc-card-id">' + esc(cardId) + "</span>";
      el.appendChild(bar);

      // ---- 正文 ----
      const body = document.createElement("div");
      body.className = "lc-card-bd";
      if (type === "image") {
        const src = canvasImgSrc(content);
        const fig = document.createElement("div");
        fig.className = "lc-fig";
        if (src) {
          const img = document.createElement("img");
          img.src = src;
          img.alt = "画布图片";
          fig.appendChild(img);
        } else {
          fig.textContent = content;
        }
        body.appendChild(fig);
      } else if (type === "web") {
        const a = document.createElement("a");
        const mm = /\]\(\s*([^)\s]+)/.exec(content);
        const url = mm ? mm[1] : (/(https?:\/\/\S+)/.exec(content) || [])[1];
        a.className = "lc-web";
        a.textContent = content.replace(/^\s*[-*+]\s+/, "");
        if (url) { a.href = url; a.target = "_blank"; a.rel = "noreferrer"; }
        body.appendChild(a);
      } else if (type === "file" || type === "ppt") {
        const row = document.createElement("div");
        row.className = "lc-file";
        const mm = /\(\s*([^)\s]+)[^)]*\)/.exec(content);
        const path = mm ? mm[1] : content.trim();
        const name = path.split(/[\\/]/).pop() || path;
        row.innerHTML = '<span class="lc-file-ico">📎</span>'
          + '<span class="lc-file-name" title="' + escAttr(path) + '">' + esc(name) + "</span>";
        if (/^(data|image_library|models)[\\/]/.test(path)) {
          const dl = document.createElement("a");
          dl.className = "lc-file-dl";
          dl.href = "/api/doclib/download?rel=" + encodeURIComponent(path);
          dl.textContent = "下载";
          row.appendChild(dl);
        }
        body.appendChild(row);
      } else {
        const txt = document.createElement("div");
        txt.className = "lc-text" + (content.length > 420 ? " cut" : "");
        txt.textContent = content;
        body.appendChild(txt);
        if (content.length > 420) {
          const more = document.createElement("button");
          more.type = "button";
          more.className = "lc-more";
          more.textContent = "展开全文 ▾";
          more.onclick = function (e) {
            e.preventDefault();
            e.stopPropagation();
            const on = txt.classList.toggle("cut");
            more.textContent = on ? "展开全文 ▾" : "收起 ▴";
          };
          body.appendChild(more);
        }
      }
      el.appendChild(body);

      // ---- 选区框（画在卡片自己的坐标系里）----
      const rectEl = document.createElement("div");
      rectEl.className = "lc-sel-rect";
      rectEl.hidden = true;
      el.appendChild(rectEl);

      // ---- 拖拽（标题条上 pointer 事件，不跟正文圈选打架）----
      let drag = null;
      let moved = false;
      bar.addEventListener("pointerdown", function (e) {
        if (e.button != null && e.button !== 0) return;
        drag = { sx: e.clientX, sy: e.clientY, left: el.offsetLeft || 0, top: el.offsetTop || 0 };
        el.classList.add("dragging");
        moved = true;
        try { bar.setPointerCapture(e.pointerId); } catch (err) { /* 老浏览器不支持就算了 */ }
        e.preventDefault();
      });
      bar.addEventListener("pointermove", function (e) {
        if (!drag) return;
        const dx = e.clientX - drag.sx;
        const dy = e.clientY - drag.sy;
        el.style.left = Math.max(0, drag.left + dx) + "px";
        el.style.top = Math.max(0, drag.top + dy) + "px";
        frame();
      });
      const endDrag = function () {
        if (!drag) return;
        drag = null;
        el.classList.remove("dragging");
        frame();
      };
      bar.addEventListener("pointerup", endDrag);
      bar.addEventListener("pointercancel", endDrag);

      // ---- 圈选（正文里 mousedown / mousemove / mouseup）----
      let sel = null;
      const paintRect = function (x, y, w, h) {
        rectEl.hidden = false;
        rectEl.style.left = x + "px";
        rectEl.style.top = y + "px";
        rectEl.style.width = w + "px";
        rectEl.style.height = h + "px";
      };
      body.addEventListener("mousedown", function (e) {
        if (e.button != null && e.button !== 0) return;
        const r = el.getBoundingClientRect ? el.getBoundingClientRect() : { left: 0, top: 0 };
        sel = { x: e.clientX - r.left, y: e.clientY - r.top };
        paintRect(sel.x, sel.y, 0, 0);
        e.preventDefault();
      });
      body.addEventListener("mousemove", function (e) {
        if (!sel) return;
        const r = el.getBoundingClientRect ? el.getBoundingClientRect() : { left: 0, top: 0 };
        const x = e.clientX - r.left;
        const y = e.clientY - r.top;
        paintRect(Math.min(sel.x, x), Math.min(sel.y, y), Math.abs(x - sel.x), Math.abs(y - sel.y));
      });
      body.addEventListener("mouseup", function (e) {
        if (!sel) return;
        const r = el.getBoundingClientRect ? el.getBoundingClientRect() : { left: 0, top: 0, width: 0, height: 0 };
        const x = e.clientX - r.left;
        const y = e.clientY - r.top;
        const d = sel;
        sel = null;
        let rect = {
          x: Math.min(d.x, x), y: Math.min(d.y, y),
          w: Math.abs(x - d.x), h: Math.abs(y - d.y),
        };
        // 几乎没拖动 = 用户只是点了一下 → 当「选整块」
        const whole = rect.w < 6 || rect.h < 6;
        if (whole) {
          rect = { x: 0, y: 0, w: r.width || 0, h: r.height || 0 };
        }
        rect = {
          x: Math.round(rect.x), y: Math.round(rect.y),
          w: Math.round(rect.w), h: Math.round(rect.h),
        };
        paintRect(rect.x, rect.y, rect.w, rect.h);
        pickCard(el, rect, whole ? "整块" : "区域");
      });

      grid.appendChild(el);
      els.push(el);
    });

    // 首帧排版：两列交错排（宽度由 CSS 决定），每列按前面卡片高度往下接
    const layout = function () {
      // 手机上只排一列（宽度由 CSS 的媒体查询放到 100%），桌面上两列交错
      const narrow = typeof window !== "undefined" && window.innerWidth && window.innerWidth <= 768;
      const cols = (list.length > 1 && !narrow) ? 2 : 1;
      const tops = [];
      for (let i = 0; i < cols; i++) tops.push(8);
      els.forEach(function (el, i) {
        const col = i % cols;
        el.style.left = cols === 1 ? "0px" : (col === 0 ? "0%" : "51%");
        el.style.top = tops[col] + "px";
        tops[col] += (el.offsetHeight || 120) + 10;
      });
      frame();
    };
    layout();
    if (typeof requestAnimationFrame === "function") {
      // 字体/图片加载完高度会变，等一帧再排一次（拖过的卡片不动，尊重用户手摆的位置）
      requestAnimationFrame(function () {
        const touched = els.some(function (el) { return el.style.left.indexOf("%") < 0; });
        if (!touched) layout();
        else frame();
      });
    }

    return grid;
  }

  // 挂到容器上（渲染入口只有这一个，app.js 不用知道内部结构）
  function mountCanvas(el, cards, opts) {
    if (!el) return null;
    const grid = renderCanvas(cards, opts);
    if (grid) el.appendChild(grid);
    return grid;
  }

  // ================================================================
  // 回复内嵌实时卡片（2026-11-21）——「边说边出画面」
  // ----------------------------------------------------------------
  // 摩擦点（用户原话）：回答是一面文字墙，地图 / 天气 / 生成的图 / 文档
  // 全散在正文和折叠块里，想看得自己去翻面板。
  // 做法：后端在工具结果里附一份 cards 元数据（backend/livecard_meta.py），
  // 气泡里内嵌一排小卡片，点一下就能看细节（图片放大 / 坐标 / 文件名）。
  //
  // 约定：
  //   · renderLiveCard(list) 返回**一个容器节点**，里面每张卡一个子节点，
  //     子节点数 === 输入条数（无 DOM 环境下返回同构的最小节点，纯逻辑可断言）；
  //   · 卡片 class 带类型（.livecard-map / .livecard-weather / …），标题在
  //     .lc-title 的文本里，方便"按类型找卡 / 按文字找卡"；
  //   · 点卡片 → opts.onOpen(card, node)；没给回调就就地展开 payload 详情，
  //     所以单看这个文件也能点，不依赖 app.js。
  // ================================================================
  const LC_META = {
    map: { ico: "🗺", label: "地图" },
    weather: { ico: "🌤", label: "天气" },
    image: { ico: "🖼", label: "图片" },
    doc: { ico: "📄", label: "文档" },
    ppt: { ico: "📊", label: "演示" },
    video: { ico: "🎬", label: "视频" },
    // ★ 2026-11-22 盯梢小助手：后台监视发现变化时主动推过来的卡。
    //   加在这里而不是在调用方塞成 "doc"：卡片上的角标要写「盯梢」，
    //   塞成文档会让用户以为只是又生成了一个文件。
    watch: { ico: "👁", label: "盯梢" },
  };

  // 建节点：浏览器里用真 DOM；node 里跑断言时给同构的最小节点（够断言用）
  function lcNode(tag, cls) {
    if (typeof document !== "undefined" && document && document.createElement) {
      const el = document.createElement(tag);
      if (cls) el.className = cls;
      return el;
    }
    const n = {
      tagName: String(tag).toUpperCase(), className: cls || "",
      childNodes: [], children: [], textContent: "", style: {},
      setAttribute: function () {}, appendChild: function (c) {
        this.childNodes.push(c); this.children.push(c); return c;
      },
      // 2026-11-24 补：结果卡片（renderCard）会把缩略图插到头部前面，
      // 没有 insertBefore 的话 node 里跑断言会抛「不是函数」，卡片直接走兜底。
      insertBefore: function (c, ref) {
        const i = ref ? this.childNodes.indexOf(ref) : -1;
        if (i < 0) { this.childNodes.push(c); this.children.push(c); }
        else { this.childNodes.splice(i, 0, c); this.children.splice(i, 0, c); }
        return c;
      },
      addEventListener: function () {}, remove: function () {},
      classList: { add: function () {}, remove: function () {}, contains: function () { return false; } },
    };
    return n;
  }

  function lcPayloadLines(payload) {
    const out = [];
    if (!payload || typeof payload !== "object") return out;
    Object.keys(payload).forEach(function (k) {
      const v = payload[k];
      if (v == null || v === "" || v === false) return;
      if (Array.isArray(v)) {
        const flat = v.map(function (x) {
          if (x && typeof x === "object") return x.name || x.title || JSON.stringify(x);
          return String(x);
        }).filter(Boolean);
        if (flat.length) out.push(k + "：" + flat.slice(0, 6).join("、"));
      } else if (typeof v === "object") {
        const inner = v.name || v.title || v.from || v.path;
        if (inner) out.push(k + "：" + String(inner));
      } else {
        out.push(k + "：" + String(v));
      }
    });
    return out.slice(0, 8);
  }

  // 单张卡
  function renderOneCard(card, opts) {
    opts = opts || {};
    const c = (card && typeof card === "object") ? card : {};
    const type = LC_META[c.type] ? c.type : "doc";
    const meta = LC_META[type];
    const title = String(c.title == null ? "" : c.title).trim() || (meta.label + "结果");

    const node = lcNode("div", "livecard livecard-" + type);
    try { node.setAttribute("data-type", type); } catch (e) { /* 无 DOM 环境 */ }
    node.title = title;

    const ico = lcNode("span", "lc-ico");
    ico.textContent = meta.ico;
    node.appendChild(ico);

    if (c.thumb) {
      const img = lcNode("img", "lc-thumb");
      try {
        img.setAttribute("src", String(c.thumb));
        img.setAttribute("alt", title);
        img.setAttribute("loading", "lazy");
      } catch (e) { /* 无 DOM 环境 */ }
      node.appendChild(img);
    }

    const tEl = lcNode("span", "lc-title");
    tEl.textContent = title;
    node.appendChild(tEl);

    const badge = lcNode("span", "lc-kind");
    badge.textContent = meta.label;
    node.appendChild(badge);

    // 详情面板：默认收起，点卡片展开（有 onOpen 就交给调用方）
    const detail = lcNode("div", "lc-detail");
    detail.hidden = true;
    const lines = lcPayloadLines(c.payload);
    detail.textContent = lines.length ? lines.join("\n") : "（没有更多细节）";
    node.appendChild(detail);

    const onOpen = typeof opts.onOpen === "function" ? opts.onOpen : null;
    const click = function (e) {
      try { if (e && e.stopPropagation) e.stopPropagation(); } catch (err) { /* 忽略 */ }
      if (onOpen) { onOpen(c, node); return; }
      detail.hidden = !detail.hidden;
      node.classList.toggle && node.classList.toggle("open", !detail.hidden);
    };
    if (typeof node.addEventListener === "function") node.addEventListener("click", click);
    else node.onclick = click;

    return node;
  }

  /**
   * 把 [{type,title,thumb,payload}] 渲染成一排可点的小卡片。
   * @returns {Element} 容器节点（子节点数 === 有效卡片数）
   */
  function renderCards(list, opts) {
    const box = lcNode("div", "livecards");
    const arr = Array.isArray(list) ? list : [];
    arr.forEach(function (c) {
      if (!c || typeof c !== "object") return;
      box.appendChild(renderOneCard(c, opts));
    });
    if (!box.childNodes.length) box.className = "livecards livecards-empty";
    return box;
  }

  // 挂到容器上（append 语义）——app.js 用这一个入口，不用关心内部结构
  function mountCards(host, list, opts) {
    if (!host) return null;
    const box = renderCards(list, opts);
    if (!box || !box.childNodes.length) return null;
    host.appendChild(box);
    return box;
  }

  // 合并去重（同类型 + 同标题只留一张）：后端分批送 cards 时用
  function mergeCards(base, more) {
    const out = Array.isArray(base) ? base.slice() : [];
    const seen = {};
    out.forEach(function (c) { if (c) seen[c.type + "|" + c.title] = 1; });
    (Array.isArray(more) ? more : []).forEach(function (c) {
      if (!c || typeof c !== "object") return;
      const k = c.type + "|" + c.title;
      if (seen[k]) return;
      seen[k] = 1;
      out.push(c);
    });
    return out.slice(0, 12);
  }

  // ================================================================
  // 答完给张卡：结果卡片化（2026-11-24）—— renderCard(kind, payload)
  // ----------------------------------------------------------------
  // 用户长期偏好：「知识点用图表或知识卡呈现」。但答完的结果现在还是一屏文字
  // 流：天气是一句话、路线是一段话、文档/图片散在下面。
  // 这个分发器把「一类结果 + 一份 payload」直接渲染成**一张可交互小卡**：
  //   renderCard("weather"|"map"|"doc"|"image", payload) → 节点
  //   mountCard(host, kind, payload)                    → 挂到容器上（子节点 +1）
  //
  // 约定：
  //   · 返回节点的 class 里带 kind（`resultcard-weather` 也有裸 `weather`），
  //     按 class 就能找到卡；
  //   · 只认上面四类；未知 kind / payload 形状不认识 → 走兜底卡，**绝不抛异常**
  //     （渲染主流程正在收尾，宁可少一张卡也不能把正文搞崩）；
  //   · 不碰业务：不 fetch、不弹 toast、不知道 sessionId；样式一律走
  //     style.css 里的 `.resultcard` 与 CSS 变量（白底低饱和）。
  // ================================================================
  const RESULT_META = {
    weather: { ico: "🌤", label: "天气" },
    map: { ico: "🗺", label: "地图" },
    doc: { ico: "📄", label: "文档" },
    image: { ico: "🖼", label: "图片" },
    unknown: { ico: "🧩", label: "结果" },
  };

  function rcText(cls, text) {
    const el = lcNode("span", cls);
    el.textContent = String(text == null ? "" : text);
    return el;
  }

  // 卡壳：头部（图标 + 标题 + 类型角标）+ 主体。点一下展开/收起。
  function rcShell(kind, title) {
    const meta = RESULT_META[kind] || RESULT_META.unknown;
    const node = lcNode("div", "livecard resultcard resultcard-" + kind + " " + kind);
    try { node.setAttribute("data-kind", kind); } catch (e) { /* 无 DOM 环境 */ }
    node.title = String(title == null ? "" : title) || meta.label;

    const hd = lcNode("div", "rc-hd");
    hd.appendChild(rcText("rc-ico", meta.ico));
    hd.appendChild(rcText("rc-title", node.title));
    hd.appendChild(rcText("rc-badge", meta.label));
    node.appendChild(hd);

    const body = lcNode("div", "rc-body");
    node.appendChild(body);

    const click = function (e) {
      try { if (e && e.stopPropagation) e.stopPropagation(); } catch (err) { /* 忽略 */ }
      try { if (node.classList && node.classList.toggle) node.classList.toggle("open"); } catch (err) { /* 忽略 */ }
    };
    if (typeof node.addEventListener === "function") node.addEventListener("click", click);
    else node.onclick = click;

    return { node: node, body: body };
  }

  // 一行「标签：值」，空值直接不画（避免出现半行空白）
  function rcRow(body, label, value) {
    if (!body) return;
    if (value == null || value === "" || value === false) return;
    const row = lcNode("div", "rc-row");
    row.appendChild(rcText("rc-k", label));
    row.appendChild(rcText("rc-v", value));
    body.appendChild(row);
  }

  // payload 形状不认识时的兜底：把能读出来的 key/value 摊平（不会空白，也不抛）
  function rcDump(body, payload) {
    if (!body) return;
    const lines = lcPayloadLines(payload);
    if (!lines.length) { rcRow(body, "内容", "（这类结果暂时没有可展示的细节）"); return; }
    lines.forEach(function (line) {
      const i = line.indexOf("：");
      if (i > 0) rcRow(body, line.slice(0, i), line.slice(i + 1));
      else rcRow(body, "内容", line);
    });
  }

  function rcArr() {
    for (let i = 0; i < arguments.length; i++) {
      if (Array.isArray(arguments[i])) return arguments[i];
    }
    return [];
  }

  function rcThumb(src, alt) {
    const img = lcNode("img", "rc-thumb");
    try {
      img.setAttribute("src", String(src));
      img.setAttribute("alt", String(alt || ""));
      img.setAttribute("loading", "lazy");
    } catch (e) { /* 无 DOM 环境 */ }
    return img;
  }

  // —— 天气 ——
  function renderWeatherCard(payload) {
    const p = (payload && typeof payload === "object") ? payload : {};
    const city = p.city || p.place || p.location || "";
    const temp = (p.temp != null ? p.temp : (p.temperature != null ? p.temperature : ""));
    const desc = p.desc || p.weather || p.text || p.condition || "";
    const head = [city, temp, desc].filter(function (x) { return x !== "" && x != null; }).join(" · ");
    const sh = rcShell("weather", head || "天气");
    rcRow(sh.body, "城市", city);
    rcRow(sh.body, "温度", temp);
    rcRow(sh.body, "天气", desc);
    rcRow(sh.body, "湿度", p.humidity);
    rcRow(sh.body, "风", p.wind || p.winddesc || p.wind_dir);
    const days = rcArr(p.days, p.forecast, p.list, p.weekly);
    days.slice(0, 5).forEach(function (d) {
      if (d == null) return;
      if (typeof d !== "object") { rcRow(sh.body, "预报", d); return; }
      const day = d.date || d.day || d.week || d.name || "预报";
      const txt = d.text || d.desc || d.weather || d.condition || "";
      const hi = (d.high != null ? d.high : (d.tempMax != null ? d.tempMax : d.max));
      const lo = (d.low != null ? d.low : (d.tempMin != null ? d.tempMin : d.min));
      const t = (hi != null || lo != null)
        ? String(hi != null ? hi : "") + (lo != null ? "~" + lo : "") : "";
      rcRow(sh.body, day, [txt, t].filter(Boolean).join(" "));
    });
    if (!sh.body.childNodes.length) rcDump(sh.body, p);
    return sh.node;
  }

  // —— 地图 / 路线 ——
  function renderMapCard(payload) {
    const p = (payload && typeof payload === "object") ? payload : {};
    const from = p.from || p.origin || p.start || "";
    const to = p.to || p.dest || p.destination || p.end || "";
    const head = (from || to)
      ? String(from || "") + (from && to ? " → " : "") + String(to || "")
      : (p.title || p.name || "路线");
    const sh = rcShell("map", head);
    const thumb = p.thumb || p.static_map || p.map_url || "";
    if (thumb) sh.node.insertBefore(rcThumb(thumb, head), sh.body);
    rcRow(sh.body, "起点", from);
    rcRow(sh.body, "终点", to);
    rcRow(sh.body, "距离", p.distance || p.dist);
    rcRow(sh.body, "耗时", p.duration || p.time || p.eta);
    rcRow(sh.body, "方式", p.mode || p.by);
    const steps = rcArr(p.steps, p.route, p.paths, p.points);
    steps.slice(0, 6).forEach(function (s) {
      if (s == null) return;
      if (typeof s !== "object") { rcRow(sh.body, "步骤", s); return; }
      const name = s.instruction || s.road || s.name || s.title || s.desc || "";
      const dist = s.distance || s.dist || "";
      const line = [name, dist].filter(Boolean).join(" ");
      if (line) rcRow(sh.body, "步骤", line);
    });
    if (!sh.body.childNodes.length) rcDump(sh.body, p);
    return sh.node;
  }

  // —— 文档 ——
  function renderDocCard(payload) {
    const p = (payload && typeof payload === "object") ? payload : {};
    const name = p.title || p.name || p.file || p.filename || p.path || "文档";
    const sh = rcShell("doc", name);
    rcRow(sh.body, "文件", p.path || p.file || p.filename || name);
    rcRow(sh.body, "格式", p.ext || p.format || p.type_ext);
    rcRow(sh.body, "页数", Array.isArray(p.pages) ? p.pages.length : p.pages);
    rcRow(sh.body, "字数", p.chars || p.words || p.length);
    rcRow(sh.body, "大小", p.size || p.bytes);
    rcRow(sh.body, "链接", p.url || p.link || p.download || "");
    const pages = rcArr(p.pages, p.outline, p.sections);
    pages.slice(0, 5).forEach(function (pg, i) {
      if (pg == null) return;
      if (typeof pg === "object") {
        rcRow(sh.body, "第 " + (i + 1) + " 页", pg.title || pg.name || pg.heading || pg.text || "");
      } else {
        rcRow(sh.body, "第 " + (i + 1) + " 页", pg);
      }
    });
    if (!sh.body.childNodes.length) rcDump(sh.body, p);
    return sh.node;
  }

  // —— 图片 ——
  function renderImageCard(payload) {
    const p = (payload && typeof payload === "object") ? payload : {};
    const src = p.thumb || p.src || p.url || p.path || p.image || "";
    const name = p.title || p.name || p.filename || p.prompt || "图片";
    const sh = rcShell("image", name);
    if (src) sh.node.insertBefore(rcThumb(src, name), sh.body);
    rcRow(sh.body, "尺寸", (p.width && p.height) ? (p.width + "×" + p.height) : (p.size_px || ""));
    rcRow(sh.body, "格式", p.ext || p.format || p.mime);
    rcRow(sh.body, "文件", p.path || p.file || p.filename || "");
    rcRow(sh.body, "提示词", p.prompt || p.query || "");
    rcRow(sh.body, "来源", p.source || p.from || "");
    if (!sh.body.childNodes.length) rcDump(sh.body, p);
    return sh.node;
  }

  RESULT_META.weather.render = renderWeatherCard;
  RESULT_META.map.render = renderMapCard;
  RESULT_META.doc.render = renderDocCard;
  RESULT_META.image.render = renderImageCard;

  // ================================================================
  // 结果卡分派（第二版，2026-11-24）：render(kind, payload) → 专用渲染器
  // ----------------------------------------------------------------
  // 和 renderCard 的分工（两个都留着，职责不同）：
  //   renderCard → 通用「详情卡」：四类共用一个模子，摊 key/value。
  //   render     → **按类型分派到专用渲染器**，把数据画成一眼能看懂的形状：
  //     weather → renderTempBar     温度条（一天一条，高低温区间画成条）
  //     map     → renderRouteThumb  路线缩略图（起终点按坐标摆放 + 连线位）
  //     doc     → renderDocPreview  文档预览（前几页可翻，带"字迹"占位）
  //     image   → renderImageCanvas 图片画布（缩略图 + 尺寸/提示词）
  //   未知 type → renderPlainCard（纯文本卡，绝不裸奔）。
  //
  // 约定（与 renderCard 一致）：
  //   · 返回节点的 class 里既有 `resultcard`、也有 `resultcard-<kind>` 和裸 kind；
  //   · **永不抛异常**：payload 形状不认识 / 字段为空只影响卡片内容多少，
  //     绝不影响正文（主流程这时正在收尾）；
  //   · 不碰业务：不 fetch、不弹 toast、不知道 sessionId；
  //   · 颜色一律走 style.css 的 --lc-* / --res-* 变量。
  // ================================================================

  // 数字容错：字符串 "25℃" / " 3.5 " 也要能画进条里；读不出就是 null
  function lcNum(v) {
    if (typeof v === "number") return isFinite(v) ? v : null;
    if (v == null) return null;
    const m = String(v).replace(/[，,\s]/g, "").match(/-?\d+(?:\.\d+)?/);
    if (!m) return null;
    const n = parseFloat(m[0]);
    return isFinite(n) ? n : null;
  }

  function lcFirst(obj, keys) {
    if (!obj || typeof obj !== "object") return "";
    for (let i = 0; i < keys.length; i++) {
      const v = obj[keys[i]];
      if (v != null && v !== "" && typeof v !== "object") return v;
    }
    return "";
  }

  // 卡壳 + 主体，并允许在头部和主体之间插一块"图形"（温度条 / 路况图 / 预览 / 画布）
  function lcShell(kind, title) {
    const sh = rcShell(kind, title);
    return { node: sh.node, body: sh.body };
  }

  function lcInsertArt(sh, art) {
    if (!sh || !art) return sh;
    try { sh.node.insertBefore(art, sh.body || null); } catch (e) {
      try { sh.node.appendChild(art); } catch (e2) { /* 无 DOM 环境 */ }
    }
    return sh;
  }

  // —— 天气：温度条 ——
  // 一天一条：左日期/天气，中间"区间条"表现低温→高温（相对这几天最低/最高算位置），
  // 右边写 "19~28℃"。读不出温度就退回文字行，不画空条。
  function renderTempBar(payload) {
    const p = (payload && typeof payload === "object") ? payload : {};
    const city = lcFirst(p, ["city", "place", "location"]);
    const detail = lcFirst(p, ["detail", "desc", "text", "condition", "weather"]);
    const now = lcFirst(p, ["temp", "temperature", "now"]);
    const nowTxt = (now === "") ? "" : (String(now).indexOf("℃") >= 0 ? String(now) : now + "℃");
    const head = [city, nowTxt, detail].filter(function (x) {
      return x !== "" && x != null;
    }).join(" · ");
    const sh = lcShell("weather", head || "天气");

    const days = rcArr(p.days, p.forecast, p.list, p.weekly);
    const rows = [];
    days.slice(0, 7).forEach(function (d) {
      if (d == null) return;
      if (typeof d !== "object") {
        rows.push({ label: "预报", text: String(d), hi: null, lo: null });
        return;
      }
      const label = lcFirst(d, ["date", "day", "week", "name"]) || "预报";
      const txt = lcFirst(d, ["text", "desc", "weather", "condition"]);
      const hi = lcNum(d.high != null ? d.high : (d.tempMax != null ? d.tempMax : d.max));
      const lo = lcNum(d.low != null ? d.low : (d.tempMin != null ? d.tempMin : d.min));
      rows.push({ label: String(label), text: String(txt), hi: hi, lo: lo });
    });

    // 这几天温度的整体范围（把条摆到 0~100% 的位置上要用）
    let min = null, max = null;
    rows.forEach(function (r) {
      const lo = (r.lo != null ? r.lo : r.hi);
      const hi = (r.hi != null ? r.hi : r.lo);
      if (lo == null && hi == null) return;
      const a = (lo == null ? hi : lo), b = (hi == null ? lo : hi);
      if (min == null || a < min) min = a;
      if (max == null || b > max) max = b;
    });
    const span = (min != null && max != null && max > min) ? (max - min) : 0;

    if (rows.length) {
      const box = lcNode("div", "lc-bars");
      rows.forEach(function (r) {
        const row = lcNode("div", "lc-bar-row");
        row.appendChild(rcText("lc-bar-day", r.label));
        const bar = lcNode("div", "lc-bar");
        if (r.lo != null || r.hi != null) {
          const lo = (r.lo != null ? r.lo : r.hi);
          const hi = (r.hi != null ? r.hi : r.lo);
          const left = span ? Math.round(((Math.min(lo, hi) - min) / span) * 100) : 0;
          const width = span ? Math.max(4, Math.round((Math.abs(hi - lo)) / span * 100)) : 100;
          const fill = lcNode("div", "lc-bar-fill");
          fill.style = fill.style || {};
          fill.style.left = left + "%";
          fill.style.width = width + "%";
          fill.title = String(lo) + "~" + String(hi) + "℃";
          bar.appendChild(fill);
        }
        row.appendChild(bar);
        row.appendChild(rcText("lc-bar-val",
          (r.hi != null || r.lo != null)
            ? ((r.lo != null ? r.lo : "") + (r.hi != null ? "~" + r.hi : "")) + "℃"
            : (r.text || "")));
        box.appendChild(row);
      });
      lcInsertArt(sh, box);
    }

    rcRow(sh.body, "城市", city);
    rcRow(sh.body, "实况", detail);
    rcRow(sh.body, "温度", nowTxt);
    rcRow(sh.body, "湿度", p.humidity);
    rcRow(sh.body, "风", p.wind || p.winddesc || p.wind_dir);
    if (!sh.body.childNodes.length && !rows.length) rcDump(sh.body, p);
    return sh.node;
  }

  // —— 地图：路线缩略图 ——
  // 不拉瓦片（离线也要能画）：把 markers 按经纬度相对位置摆进一个小方块，
  // 首尾点高亮，中间的点当途经。有 static_map / thumb 时优先用真图。
  function renderRouteThumb(payload) {
    const p = (payload && typeof payload === "object") ? payload : {};
    const route = (p.route && typeof p.route === "object") ? p.route : {};
    const frm = lcFirst(p, ["from", "origin", "start"]) || lcFirst(route, ["from", "origin"]);
    const to = lcFirst(p, ["to", "dest", "destination", "end"]) || lcFirst(route, ["to", "dest"]);
    const head = (frm || to)
      ? String(frm || "") + (frm && to ? " → " : "") + String(to || "")
      : (lcFirst(p, ["title", "name"]) || "地图");
    const sh = lcShell("map", head);

    const markers = rcArr(p.markers, p.points, p.places);
    const pts = [];
    markers.slice(0, 8).forEach(function (m) {
      if (!m || typeof m !== "object") return;
      const lat = lcNum(m.lat != null ? m.lat : m.latitude);
      const lon = lcNum(m.lon != null ? m.lon : (m.lng != null ? m.lng : m.longitude));
      if (lat == null && lon == null) return;
      pts.push({ name: String(m.name || m.title || ""), lat: lat, lon: lon });
    });

    const img = lcFirst(p, ["thumb", "static_map", "map_url"]);
    if (img) {
      lcInsertArt(sh, rcThumb(img, head));
    } else if (pts.length) {
      let latMin = null, latMax = null, lonMin = null, lonMax = null;
      pts.forEach(function (q) {
        if (q.lat != null) {
          if (latMin == null || q.lat < latMin) latMin = q.lat;
          if (latMax == null || q.lat > latMax) latMax = q.lat;
        }
        if (q.lon != null) {
          if (lonMin == null || q.lon < lonMin) lonMin = q.lon;
          if (lonMax == null || q.lon > lonMax) lonMax = q.lon;
        }
      });
      const thumb = lcNode("div", "lc-route-thumb");
      const posOf = function (q) {
        const x = (q.lon == null || lonMin == null || lonMax == null || lonMax === lonMin)
          ? 50 : Math.round((q.lon - lonMin) / (lonMax - lonMin) * 76 + 12);
        const y = (q.lat == null || latMin == null || latMax == null || latMax === latMin)
          ? 50 : Math.round((1 - (q.lat - latMin) / (latMax - latMin)) * 76 + 12);
        return { x: x, y: y };
      };
      pts.forEach(function (q, i) {
        const xy = posOf(q);
        const dot = lcNode("span", "lc-dot" +
          (i === 0 ? " lc-dot-start" : (i === pts.length - 1 ? " lc-dot-end" : "")));
        dot.style = dot.style || {};
        dot.style.left = xy.x + "%";
        dot.style.top = xy.y + "%";
        dot.title = q.name || ("点 " + (i + 1));
        thumb.appendChild(dot);
        if (q.name) {
          const lb = rcText("lc-dot-label", q.name);
          lb.style = lb.style || {};
          lb.style.left = xy.x + "%";
          lb.style.top = xy.y + "%";
          thumb.appendChild(lb);
        }
      });
      lcInsertArt(sh, thumb);
    }

    rcRow(sh.body, "起点", frm);
    rcRow(sh.body, "终点", to);
    rcRow(sh.body, "距离", p.distance || p.dist || route.distance);
    rcRow(sh.body, "耗时", p.duration || p.time || p.eta || route.duration);
    rcRow(sh.body, "方式", p.mode || p.by || route.mode);
    const nearby = (p.nearby && typeof p.nearby === "object") ? p.nearby : {};
    const items = rcArr(p.steps, p.nearby_items, nearby.items);
    if (items.length) rcRow(sh.body, "地点数", items.length);
    items.slice(0, 5).forEach(function (s) {
      if (s == null) return;
      if (typeof s !== "object") { rcRow(sh.body, "途经", s); return; }
      const line = [s.instruction || s.road || s.name || s.title || "",
                    s.distance || s.dist || ""].filter(Boolean).join(" ");
      if (line) rcRow(sh.body, "途经", line);
    });
    if (!sh.body.childNodes.length) rcDump(sh.body, p);
    return sh.node;
  }
  // —— 文档：可翻页预览 ——
  // 前 3 页画成小纸片（页码 + 一行标题 + 两道"字迹"占位），点卡片展开其余细节。
  function renderDocPreview(payload) {
    const p = (payload && typeof payload === "object") ? payload : {};
    const path = lcFirst(p, ["path", "file", "filename"]);
    const name = lcFirst(p, ["title", "name"]) ||
      (path ? String(path).split(/[\\/]/).pop() : "") || "文档";
    const sh = lcShell("doc", name);

    const pages = rcArr(p.pages, p.outline, p.sections, p.preview);
    const nPages = (typeof p.pages === "number") ? p.pages : pages.length;
    const show = pages.slice(0, 3);
    if (show.length) {
      const strip = lcNode("div", "lc-doc-pages");
      show.forEach(function (pg, i) {
        const page = lcNode("div", "lc-doc-page");
        page.appendChild(rcText("lc-doc-no", String(i + 1)));
        const ink = lcNode("div", "lc-doc-ink");
        const txt = (pg && typeof pg === "object")
          ? String(pg.title || pg.name || pg.heading || pg.text || "")
          : String(pg == null ? "" : pg);
        ink.appendChild(rcText("lc-doc-line", txt || "（本页无标题）"));
        ink.appendChild(lcNode("span", "lc-doc-line lc-doc-ghost"));
        ink.appendChild(lcNode("span", "lc-doc-line lc-doc-ghost lc-doc-ghost2"));
        page.appendChild(ink);
        strip.appendChild(page);
      });
      lcInsertArt(sh, strip);
    }

    rcRow(sh.body, "文件", path);
    rcRow(sh.body, "页数", nPages || "");
    rcRow(sh.body, "格式", p.ext || p.format || (path ? String(path).split(".").pop() : ""));
    rcRow(sh.body, "字数", p.chars || p.words);
    rcRow(sh.body, "链接", p.url || p.link || p.download);
    if (!sh.body.childNodes.length && !show.length) rcDump(sh.body, p);
    return sh.node;
  }

  // —— 图片：画布 ——
  function renderImageCanvas(payload) {
    const p = (payload && typeof payload === "object") ? payload : {};
    const src = lcFirst(p, ["thumb", "src", "url", "path", "saved_path", "image"]);
    const name = lcFirst(p, ["title", "name", "prompt", "query"]) || "图片";
    const sh = lcShell("image", name);

    const cv = lcNode("div", "lc-canvas");
    if (src && String(src).length <= 300) {
      cv.appendChild(rcThumb(src, String(name)));
    } else {
      // 图片本体走 ui image 事件那条流（带 b64），卡片里只放提示
      cv.appendChild(rcText("lc-canvas-ph", "（这张图不在卡片里，点开放大看）"));
    }
    lcInsertArt(sh, cv);

    const w = lcNum(p.width), h = lcNum(p.height);
    rcRow(sh.body, "尺寸", (w && h) ? (w + "×" + h) : (p.size || ""));
    rcRow(sh.body, "格式", p.mime || p.ext || p.format);
    rcRow(sh.body, "提示词", p.prompt || p.query);
    rcRow(sh.body, "模型", p.model);
    rcRow(sh.body, "文件", p.saved_path || p.path || p.file);
    if (!sh.body.childNodes.length) rcDump(sh.body, p);
    return sh.node;
  }

  // —— 未知类型：纯文本卡 ——
  // 用户看不到"这是什么"，但至少要能读出内容；因此保留 resultcard 外观、
  // 走纯文本排布，不画任何图形。
  function renderPlainCard(kind, payload) {
    const k = String(kind == null ? "" : kind).toLowerCase().trim();
    const node = lcNode("div", "livecard resultcard resultcard-plain resultcard-text text plain");
    try { node.setAttribute("data-kind", k || "text"); } catch (e) { /* 无 DOM 环境 */ }
    const lines = lcPayloadLines(payload);
    let body = "";
    if (typeof payload === "string" && payload.trim()) body = payload.trim();
    else if (lines.length) body = lines.join("；");
    if (!body) body = "（这类结果暂时只有文字）";
    node.title = k ? (k + " 结果") : "结果";

    const hd = lcNode("div", "rc-hd");
    hd.appendChild(rcText("rc-ico", "📝"));
    hd.appendChild(rcText("rc-title", node.title));
    hd.appendChild(rcText("rc-badge", "文本"));
    node.appendChild(hd);

    const txt = lcNode("div", "lc-plain");
    txt.textContent = body;
    node.appendChild(txt);
    return node;
  }

  // kind → 渲染器（导出成 DISPATCH，方便断言/调试时确认分派到了谁）
  const RESULT_DISPATCH = {
    weather: renderTempBar,
    map: renderRouteThumb,
    doc: renderDocPreview,
    image: renderImageCanvas,
  };
  const RESULT_RENDERER_NAME = {
    weather: "renderTempBar",
    map: "renderRouteThumb",
    doc: "renderDocPreview",
    image: "renderImageCanvas",
  };

  /**
   * 结果卡分派入口：按类型选专用渲染器。
   * @param {string} kind    weather | map | doc | image
   * @param {object} payload 这一轮的结果（后端 livecard_meta.build 的 payload）
   * @returns {Element} 永远是节点，永不 null、永不抛异常
   */
  function render(kind, payload, opts) {
    const k = String(kind == null ? "" : kind).toLowerCase().trim();
    const fn = RESULT_DISPATCH[k];
    const name = fn ? RESULT_RENDERER_NAME[k] : "renderPlainCard";
    let node = null;
    try {
      node = fn ? fn(payload, opts) : renderPlainCard(k, payload);
    } catch (e) {
      node = null;
    }
    if (!node) {
      try { node = renderPlainCard(k, payload); } catch (e2) { node = null; }
    }
    if (!node) {
      // 连兜底卡都建不出来（极端环境）→ 给最小节点，绝不让调用方拿到 null
      node = lcNode("div", "livecard resultcard resultcard-plain resultcard-text text plain");
      node.textContent = "（这类结果暂时画不出来）";
    }
    // 可观测：node 里跑断言 / 控制台里查"这张卡是谁画的"
    try { node.__lcRenderer = name; } catch (e3) { /* 忽略 */ }
    try { node.setAttribute("data-renderer", name); } catch (e4) { /* 忽略 */ }
    return node;
  }

  /** 挂到容器上（append 语义，子节点数 +1）。@returns {Element|null} 卡片节点 */
  function mountResult(host, kind, payload, opts) {
    if (!host || typeof host.appendChild !== "function") return null;
    const node = render(kind, payload, opts);
    if (!node) return null;
    host.appendChild(node);
    return node;
  }

  // 未知 kind 的兜底卡：不抛异常，把 payload 摊平成几行给人看
  function renderUnknownCard(kind, payload) {
    const sh = rcShell("unknown", (kind ? String(kind) + " 结果" : "结果"));
    try { sh.node.setAttribute("data-kind", String(kind == null ? "" : kind)); } catch (e) { /* 忽略 */ }
    rcRow(sh.body, "类型", String(kind == null ? "" : kind) || "（未知）");
    rcDump(sh.body, payload);
    return sh.node;
  }

  /**
   * 结果卡片分发器。
   * @param {string} kind    weather | map | doc | image
   * @param {object} payload 这一轮的工具结果（形状不认识也不会抛）
   * @returns {Element} 带 kind class 的卡片节点（永远是节点，永不 null）
   */
  function renderCard(kind, payload) {
    try {
      const k = String(kind == null ? "" : kind).toLowerCase().trim();
      const meta = RESULT_META[k];
      if (meta && typeof meta.render === "function") return meta.render(payload);
      return renderUnknownCard(k, payload);
    } catch (e) {
      // 兜底也炸了 → 给一张最小的提示卡，绝不让渲染主流程收到异常
      const node = lcNode("div", "livecard resultcard resultcard-unknown unknown");
      node.textContent = "（这类结果暂时画不出来）";
      return node;
    }
  }

  /** 挂到容器上（append 语义，子节点数 +1）。@returns {Element|null} 卡片节点 */
  function mountCard(host, kind, payload) {
    if (!host || typeof host.appendChild !== "function") return null;
    const node = renderCard(kind, payload);
    if (!node) return null;
    host.appendChild(node);
    return node;
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
    makeSession: makeSession,
    fmtTokens: fmtTokens,
    fmtDelta: fmtDelta,
    billHeadline: billHeadline,
    renderBillCard: renderBillCard,
    mountBillCard: mountBillCard,
    // 画布即对话（卡片可拖拽 / 可圈选追问）
    CANVAS_KIND: CANVAS_KIND,
    canvasImgSrc: canvasImgSrc,
    canvasContext: canvasContext,
    renderCanvas: renderCanvas,
    mountCanvas: mountCanvas,
    // 回复内嵌实时卡片（边说边出画面）：renderLiveCard 是正式入口，
    // renderCards 是同一份实现的别名（计划里用的名字，两边都能调）。
    LC_META: LC_META,
    renderLiveCard: renderCards,
    renderCards: renderCards,
    renderLiveCardItem: renderOneCard,
    mountLiveCard: mountCards,
    mergeLiveCards: mergeCards,
    // 答完给张卡（结果卡片化）：renderCard 是分发入口，mountCard 顺手挂上容器。
    RESULT_META: RESULT_META,
    renderCard: renderCard,
    mountCard: mountCard,
    renderWeatherCard: renderWeatherCard,
    renderMapCard: renderMapCard,
    renderDocCard: renderDocCard,
    renderImageCard: renderImageCard,
    // 结果卡分派（第二版）：render(type, payload) → 专用渲染器
    //   weather→renderTempBar / map→renderRouteThumb / doc→renderDocPreview /
    //   image→renderImageCanvas；未知 type→renderPlainCard（纯文本卡）。
    RESULT_DISPATCH: RESULT_DISPATCH,
    render: render,
    mountResult: mountResult,
    renderTempBar: renderTempBar,
    renderRouteThumb: renderRouteThumb,
    renderDocPreview: renderDocPreview,
    renderImageCanvas: renderImageCanvas,
    renderPlainCard: renderPlainCard,
  };
})();

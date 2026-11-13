/* 本地多模态助手 —— 前端逻辑 */
(() => {
  const $ = (s) => document.querySelector(s);
  let images = [];          // 待发送附件 base64（含 data: 前缀）
  // ★ 涂抹选区（精确微改）：在这张图上涂过的地方才会被重画，别处一个像素不动。
  //   实测：整图微改（img2img）**改不准** ——「换衣服颜色」「加眼镜」在
  //   0.45/0.60/0.85 都没生效，0.85 换背景还会把脸重画；涂选区 + 局部重绘才行。
  let selMask = null;       // 选区蒙版（base64 PNG，无前缀；白=要改）
  let selMaskOn = null;     // 涂在哪张图上（data URL），用于「重涂」
  let docs = [];            // 待发送文档：[{name, text, chars}]（拖进来时抽取正文）
  let videoB64 = null;      // 待发送视频帧 base64 列表
  let streaming = false;
  // 每轮只提示一次「AI 正在写代码」，别刷屏
  let typingHintShown = false;
  let abortCtl = null;      // 用于「■ 终止」：中断当前的流式请求
  let aborted = false;      // 标记本轮是用户主动终止的（不是出错）
  const history = [];       // 会话消息（用于多轮上下文）
  // 会话标识（后端据此把历史会话落盘，供记忆检索）
  let sessionId = localStorage.getItem("ai_session_id") || "";
  const setSessionId = (sid) => {
    sessionId = sid || "";
    if (sessionId) localStorage.setItem("ai_session_id", sessionId);
  };

  // 工具名 → 中文展示
  const TOOL_LABELS = {
    web_image_search: "🌐 联网搜图",
    save_image_to_library: "⭐ 保存图片",
    generate_image: "🎨 文生图",
    edit_image: "🖼 图片微改",
    list_directory: "📁 浏览目录",
    read_file: "📄 读取文件",
    search_files: "🔍 搜索文件",
    write_file: "✍️ 写入文件",
    append_file: "✎ 追加文件",
    remember: "🧠 更新记忆",
    search_memory: "💭 检索记忆",
    get_time: "🕐 获取时间",
    web_search: "🌐 联网搜索",
  };

  const messagesEl = $("#messages");
  const inputEl = $("#input");
  const mainEl = $("#main");

  // ---------- 基础工具 ----------
  function esc(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // ---------- 自由查看（2026-11-12 用户要求）----------
  // 问题：AI 每有一点动静就把滚动条拽到底，用户想停在中间看某一步根本做不到。
  // 做法：不逐个去改那十几处 `scrollTop = scrollHeight`（漏一处就又会被拉走），
  //      而是直接接管 messagesEl.scrollTop 的写入 —— freeView 开着时，
  //      凡是「想滚到底部」的写入一律忽略；用户自己滚动、点按钮恢复都不受影响。
  let freeView = false;
  let _keepTop = 0;       // 自由查看期间「用户想停留的位置」
  let _userActAt = 0;     // 用户最后一次主动滚动的时间戳
  const _userActing = () => Date.now() - _userActAt < 600;

  function syncFreeBtn() {
    const b = $("#freeViewBtn");
    if (!b) return;
    b.classList.toggle("fv-on", freeView);
    b.textContent = freeView ? "📌 自由查看中" : "⬇ 跟随最新";
    b.title = freeView
      ? "已暂停自动滚动 —— 可以停在任意位置慢慢看。点一下恢复跟随最新"
      : "AI 有新内容时自动滚到最新。点一下暂停，就能停在某一步慢慢看";
  }
  let _startHold = null;    // 由 bindFreeView 注入：进入自由查看时启动「逐帧守位」
  function setFreeView(v) {
    if (freeView === v) return;
    freeView = v;
    if (v) {
      _keepTop = messagesEl.scrollTop;   // 记住「就停在这儿」
      if (_startHold) _startHold();
    }
    syncFreeBtn();
  }
  function scrollBottom(force) {
    if (force) setFreeView(false);
    if (freeView) return;
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }
  const _nearBottom = () =>
    messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 60;
  (function bindFreeView() {
    const _d = Object.getOwnPropertyDescriptor(Element.prototype, "scrollTop");
    const _maxTop = () => Math.max(0, messagesEl.scrollHeight - messagesEl.clientHeight);
    let _lastScrollAt = 0;      // 上一次滚动事件时间（判断「正在连续滚动」，含触摸惯性）
    // 程序自己写位置时必须标记一下：否则这次写入触发的 scroll 事件会被当成
    // 「用户滚到这儿了」，反而把 _keepTop 改成底部 —— 那正是之前一直跳底的原因之一。
    let _reverting = false;
    const _setTop = (v) => {
      if (!_d || !_d.set) return;
      _reverting = true;
      try { _d.set.call(messagesEl, v); } finally { _reverting = false; }
    };
    if (_d && _d.set) {
      Object.defineProperty(messagesEl, "scrollTop", {
        configurable: true,
        get() { return _d.get.call(this); },
        set(v) {
          // ★ 2026-11-12 三次反馈「还是会被带下去」：
          //   原来只拦「写入值≈底部」这一种，只要有一条路径写的是「离底部差几像素」
          //   的值就漏过去了。现在自由查看期间干脆**不接受任何 JS 写入**，
          //   一律写回用户停留的位置 —— 视口只由用户的手势决定。
          if (freeView) { _setTop(Math.min(_keepTop, _maxTop())); return; }
          _d.set.call(this, v);
        },
      });
    }
    // scrollTo / scrollBy 是浏览器内部滚动，同样绕开上面的 setter —— 一并挡住
    const _sto = messagesEl.scrollTo, _sby = messagesEl.scrollBy;
    if (typeof _sto === "function") {
      messagesEl.scrollTo = function (a, b) { if (freeView) return; return _sto.call(this, a, b); };
    }
    if (typeof _sby === "function") {
      messagesEl.scrollBy = function (a, b) { if (freeView) return; return _sby.call(this, a, b); };
    }
    const b = $("#freeViewBtn");
    if (b) b.onclick = () => {
      setFreeView(!freeView);
      if (!freeView) scrollBottom();     // 恢复跟随时立刻跳到最新
    };
    // 用户自己往上滚 → 自动进入自由查看，否则刚滚上去就被拽回来
    const maybeFree = () => {
      if (freeView) return;
      if (!_nearBottom()) setFreeView(true);
    };
    // 任何「像用户在滚」的动作都记一笔，用于区分「用户自己滚」和「程序把他拽走」
    const markUser = () => { _userActAt = Date.now(); };
    ["wheel", "touchstart", "touchmove", "pointerdown", "mousedown", "keydown"].forEach((ev) =>
      messagesEl.addEventListener(ev, markUser, { passive: true })
    );
    messagesEl.addEventListener("wheel", () => requestAnimationFrame(maybeFree), { passive: true });
    messagesEl.addEventListener("touchmove", maybeFree, { passive: true });
    // ★ 2026-11-12 用户报「拖滚动条上去看，模型一有动作还是被拽到底部」：
    //   拖滚动条 / 按 PageUp / 点滚动槽**都不会触发 wheel**，上面那条检测就漏了。
    //   补一条 scroll 监听：只要不是「贴着底部」，就自动进入自由查看。
    //   （程序化滚到底时 _nearBottom() 为真，不会误判。）
    messagesEl.addEventListener("scroll", () => {
      const _now = Date.now();
      const _continuous = _now - _lastScrollAt < 140;   // 连续滚动中（含惯性）
      if (_reverting) return;          // 程序自己写的，不算用户意图
      _lastScrollAt = _now;
      if (!freeView) { requestAnimationFrame(maybeFree); return; }
      // 自由查看期间「位置归谁」统一交给 holdLoop 判断（见下），
      // 这里只做一件事：用户手势刚过 / 正在连续滚动，就把他停的位置记下来。
      if (_userActing() || _continuous) _keepTop = messagesEl.scrollTop;
    }, { passive: true });
    // ★ 2026-11-12 修「模型一有动作就跳到底部」的真正元凶：
    //   每来一个工具调用，addToolChip 会调 chip.scrollIntoView({block:"nearest"})，
    //   而 scrollIntoView 是**浏览器内部滚动**，根本不经过上面那个 scrollTop setter，
    //   所以 freeView 挡不住它 —— 实测每调一次工具就跳一次底。
    //   这里在自由查看期间把「消息区内部元素」的 scrollIntoView 直接跳过；
    //   其余元素（侧栏、面板等）照常。
    const _siiv = Element.prototype.scrollIntoView;
    if (typeof _siiv === "function") {
      Element.prototype.scrollIntoView = function (opt) {
        if (freeView && messagesEl.contains(this)) return;
        return _siiv.call(this, opt);
      };
    }
    // ★ 最终方案（2026-11-12 三次反馈「自由查看要完全由用户决定」）：
    //   前面几版都在「猜哪条路径会把视口拽走」（scrollTop 写入 / scrollIntoView /
    //   scrollTo / DOM 变化），总有漏的：图片加载完、布局变化、浏览器滚动锚定、
    //   focus()、拖滚动条…… 用户实测还是会被带下去。
    //   改成不猜：自由查看期间开一个逐帧看门狗，只要发现视口「一下跳到底部」、
    //   而用户并没有在做手势，就立刻写回用户停留的位置。
    //   判据：程序滚动几乎总是一帧内直接跳到 scrollHeight；人滚轮/拖动是渐进的。
    let _holdRAF = 0;
    let _lastSeen = messagesEl.scrollTop;
    function holdLoop() {
      if (!freeView) { _holdRAF = 0; return; }
      const cur = messagesEl.scrollTop;
      const maxTop = _maxTop();
      const now = Date.now();
      // 用户自己滚（有手势 / 正在连续滚动，含触摸惯性）→ 以他为准；
      // 否则只要位置被挪动就拉回 —— 不再要求「一帧跳到底」，
      // 渐进式滚动（动画、图片加载、布局重排）同样拦得住。
      const userDriving = _userActing() || (now - _lastScrollAt < 140);
      if (Math.abs(cur - _keepTop) >= 1) {
        if (userDriving) _keepTop = Math.min(cur, maxTop);
        else _setTop(Math.min(_keepTop, maxTop));
      }
      _lastSeen = messagesEl.scrollTop;
      _holdRAF = requestAnimationFrame(holdLoop);
    }
    _startHold = function () {
      if (_holdRAF) return;
      _lastSeen = messagesEl.scrollTop;
      _holdRAF = requestAnimationFrame(holdLoop);
    };
    syncFreeBtn();
  })();

  // ---------- 思考框：跟随视口 / 固定原位（2026-11-12 用户要求）----------
  // 「跟随」= 思考时固定悬浮在左下角（.think-dock, position:fixed），滚到哪都能看到它在想什么；
  // 「固定」= 老老实实待在消息流里（工具条之前），不跟着滚。
  // 状态记在 localStorage.think_follow，刷新后保持。
  // ★ 2026-11-12 用户反馈「点了跟随，有时能跟有时又回原位」：
  //   原来只认 `_thinkLive` 一个框，且正文一开始输出就无条件收回原位 —— 把用户
  //   选的「跟随」也一起盖掉了；思考结束后 _thinkLive 置空，再点按钮就完全没反应。
  //   现在用 _thinkBoxes 登记**所有**思考框，按 thinkFollow 统一定位，
  //   正文输出也不再强行收回（只有用户选「固定」才收）。
  let thinkFollow = localStorage.getItem("think_follow") !== "0";
  let _thinkLive = null;              // 当前这轮还在思考的 thinkWrap
  const _thinkBoxes = new Set();      // 还活在页面上的所有思考框

  function syncThinkBtn() {
    const b = $("#thinkFollowBtn");
    if (!b) return;
    b.classList.toggle("tf-on", thinkFollow);
    // 文案按用户原话写清楚：跟随 = 相对视口固定；固定 = 回消息流原位
    b.textContent = thinkFollow ? "🧠 跟随视角" : "📍 回到原位";
    b.setAttribute("aria-pressed", thinkFollow ? "true" : "false");
    b.title = thinkFollow
      ? "当前：跟随视角 —— 思考框固定在视口左下角，滚到哪都看得到。点一下改为「回到原位」"
      : "当前：回到原位 —— 思考框待在消息流原来的位置（工具条之前），不浮动。点一下改为「跟随视角」";
  }
  // 计算悬浮位置：钉在「视口左上角、顶栏下方」，左边对齐主区域
  // ★ 2026-11-12 用户反馈「左下角会挡到回答内容」→ 从输入区上方改到左上方。
  // 顶栏会收起/展开（max-height 过渡）、窗口会缩放，所以不能硬编码，每次 dock / resize 都重算
  function placeThinkDock(w) {
    if (!w || !w.classList.contains("think-dock")) return;
    const mRect = mainEl ? mainEl.getBoundingClientRect() : null;
    const topBar = document.querySelector("#topBar");
    const tRect = topBar ? topBar.getBoundingClientRect() : null;
    const vw = window.innerWidth, vh = window.innerHeight;
    const left = Math.max(8, (mRect ? mRect.left : 0) + 16);
    // 顶栏展开时贴在它下沿再往下 12px；收起后（max-height:0）用 12px 兜底
    let top = tRect && tRect.height > 4 ? Math.max(12, tRect.bottom + 12) : 12;
    // 窗口很矮时别把框顶出视口下沿
    const cap = Math.max(12, vh - 140);
    if (top > cap) top = cap;
    const avail = (mRect ? mRect.width : vw) - 32;
    w.style.left = Math.round(left) + "px";
    w.style.top = Math.round(top) + "px";
    // ⚠️ 必须清掉旧的 bottom：top 和 bottom 同时有值时框会被拉成整屏高
    w.style.bottom = "";
    w.style.width = Math.max(220, Math.min(430, Math.round(avail))) + "px";
  }
  // 所有浮动中的思考框都要跟着重排（窗口缩放、输入框变高、切按钮）
  function placeAllThinkDocks() {
    document.querySelectorAll("body > .think-dock").forEach((n) => placeThinkDock(n));
  }
  window.addEventListener("resize", placeAllThinkDocks);
  // 输入框高度会变（多行输入、附件条出现）→ 用 ResizeObserver 实时跟随，别等 resize
  (function watchComposer() {
    const comp = document.querySelector(".composer");
    if (!comp || typeof ResizeObserver !== "function") return;
    try {
      new ResizeObserver(() => placeAllThinkDocks()).observe(comp);
    } catch (e) { /* 老浏览器忽略 */ }
  })();
  // 把当前这轮的思考框按当前偏好摆好（思考途中切按钮也能立刻生效）
  // 切换/新建/清空对话时：悬浮的思考框挂在 body 上，不会随 messagesEl.innerHTML 一起消失，
  // 必须手动摘掉，否则会一直浮在左下角。
  function dockOne(w) {
    // ★ 2026-11-12 修：原来这里要求 `w._home` 必须存在，否则直接 return ——
    //   没有锚点的思考框点「跟随」就完全没反应（用户反馈「有时候点了不跟」就是这个）。
    //   现在不再依赖 _home：进消息流时就地插一个注释占位当锚点，绝对回得去。
    if (!w) return;
    if (!w.classList.contains("think-dock")) {
      if (!w._ph || !w._ph.parentNode) {
        w._ph = document.createComment("think-anchor");
        if (w.parentNode) w.parentNode.insertBefore(w._ph, w);
      }
      w.classList.add("think-dock");
      // 挂到 body 上：.messages 是 overflow:auto 的滚动容器，
      // 留在里面虽然 fixed 也能浮起来，但一旦以后给祖先加 transform/contain 就会失效。
      // 直接挂 body 最稳，也彻底不占消息流高度（不会把内容挤动）。
      document.body.appendChild(w);
    }
    placeThinkDock(w);
    ensureThinkHome(w, sendThinkHome);
  }
  // ★ 2026-11-12 用户三次反馈「点了跟随，过一会儿又自己回原位」：
  //   根因是**好几处**都会调 undockOne（正文开始输出、思考结束、新一轮开始…），
  //   只要有一处没判断 thinkFollow，用户选的「跟随」就被悄悄盖掉。
  //   现在把判断收进函数本身：跟随模式下**一律拒绝**收回，只有 force=true 才允许
  //   （新一轮清理上一轮框时用 force）。这样以后再加调用点也不会再破坏跟随。
  function undockOne(w, force) {
    if (!w || !w.classList.contains("think-dock")) return;
    if (thinkFollow && !force) return;
    w.classList.remove("think-dock");
    w.style.left = w.style.bottom = w.style.width = w.style.top = "";
    // ★ 优先按占位节点回原位（精确到「工具条之前」）。
    //   以前只认 _home，那一轮的容器被清掉时会 appendChild 到消息区末尾 ——
    //   用户看到的就是「点了固定，它跑到最下面去了，不是原位」。
    const ph = w._ph;
    if (ph && ph.parentNode) ph.parentNode.insertBefore(w, ph);
    else if (w._home && w._home.parentNode === messagesEl) messagesEl.insertBefore(w, w._home);
    else messagesEl.appendChild(w);
  }
  // ---------- 思考卡片右上角「归位」（2026-11-13 用户要求）----------
  // 跟随视角下卡片浮在视口里；用户想让它回原位时，不必跑去工具条切全局开关 ——
  // 卡片右上角直接给一颗【归位】，只让**这一张**回原位，其它卡片和全局偏好都不动。
  // ⚠️ 为什么要 _noFollow 标记：下面的 watchThinkEscape 会盯着「跟随模式下被挪回消息流」
  //    的框并立刻重新浮起；不打标记的话点完归位会马上又被弹回浮动状态（按钮等于没反应）。
  function ensureThinkHome(w, onHome) {
    if (!w) return null;
    let b = w._homeBtn;
    if (!b) {
      b = document.createElement("button");
      b.type = "button";
      b.className = "think-home";
      b.textContent = "归位";
      b.title = "让这张思考卡片回到消息流里的原位置（其它卡片和「跟随视角」设置不受影响）";
      b.setAttribute("aria-label", "思考卡片归位");
      b.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (typeof b._onHome === "function") b._onHome(w);
      });
      w._homeBtn = b;
    }
    b._onHome = onHome;
    if (b.parentNode !== w) w.appendChild(b);
    return b;
  }
  // ★ 2026-11-13 用户报 bug：点【归位】后「跟随视角」开关看着没变化，要连点两次才恢复跟随。
  //   原因：归位只把这张卡挪回原位，全局 thinkFollow 仍是 true、按钮仍显示「🧠 跟随视角」；
  //   用户点一次其实是把它关掉（卡片本来就在原位，界面毫无变化），必须点第二次才真的恢复。
  //   修法：归位后如果已经没有任何卡片在浮动，就把全局开关同步关掉 ——
  //   让开关状态永远等于「实际有没有卡片在跟随」，用户点一次就能恢复。
  function syncFollowIdle() {
    if (!thinkFollow) return;
    const anyFloating = Array.from(_thinkBoxes).some(
      (n) => n.isConnected && n.classList.contains("think-dock") && !n._noFollow
    );
    if (anyFloating) return;   // 还有卡片跟着 → 跟随仍成立，开关保持不动
    thinkFollow = false;
    try { localStorage.setItem("think_follow", "0"); } catch (e) { /* 隐私模式 */ }
    syncThinkBtn();
  }
  // 单人模式的「归位」：只把这一张放回原位；没有别的卡在跟随时，全局开关一并关掉
  function sendThinkHome(w) {
    if (!w || !w.isConnected) return;
    w._noFollow = true;
    undockOne(w, true);
    syncFollowIdle();
    if (typeof showToast === "function") showToast("思考卡片已归位", "");
  }
  function clearThinkDock() {
    _thinkLive = null;
    _thinkBoxes.clear();
    document.querySelectorAll("body > .think-dock").forEach((n) => {
      if (n._ph && n._ph.parentNode) n._ph.parentNode.removeChild(n._ph);
      n._ph = null;
      n.remove();
    });
  }
  // 按当前偏好把**所有**思考框摆好（思考途中、思考结束后切按钮都立刻生效）
  function applyThinkDock() {
    _thinkBoxes.forEach((w) => {
      if (!w.isConnected) { _thinkBoxes.delete(w); return; }
      // 点过「归位」的卡片保持原位，不再被全局「跟随视角」拽起来
      if (w._noFollow) { undockOne(w, true); return; }
      if (thinkFollow) dockOne(w); else undockOne(w);
    });
    if (thinkFollow) placeAllThinkDocks();
  }
  function setThinkFollow(v) {
    const was = thinkFollow;
    thinkFollow = !!v;
    try { localStorage.setItem("think_follow", thinkFollow ? "1" : "0"); } catch (e) { /* 隐私模式 */ }
    // 用户主动打开「跟随视角」→ 之前点过「归位」的卡片重新纳入跟随
    if (thinkFollow) _thinkBoxes.forEach((w) => { w._noFollow = false; });
    syncThinkBtn();
    applyThinkDock();
    if (was !== thinkFollow && typeof showToast === "function") {
      // 说明当前有没有真的动了框：没有存活思考框时，讲清楚「下一轮才生效」，
      // 免得用户点了按钮、界面没变化，以为按钮坏了。
      const n = Array.from(_thinkBoxes).filter((w) => w.isConnected).length;
      const mode = thinkFollow
        ? "跟随视角：思考框固定在视口左下角，滚到哪都看得到"
        : "回到原位：思考框已放回消息流原来的位置";
      showToast(n ? mode : mode + "（这一轮没有进行中的思考框，下一轮开始后生效）", "");
    }
  }
  (function bindThinkFollow() {
    const b = $("#thinkFollowBtn");
    if (b) b.onclick = () => setThinkFollow(!thinkFollow);
    syncThinkBtn();
  })();
  // ★ 第二道保险：跟随模式下，思考框只要被挪回消息流（父节点不是 body），立刻重新浮起。
  //   前面 undockOne 已经拒绝了所有非强制收回，这里再兜一层 —— 万一有别处直接
  //   操作 DOM（insertBefore / appendChild）把框搬回去，也能当场纠正。
  (function watchThinkEscape() {
    if (typeof MutationObserver !== "function") return;
    try {
      new MutationObserver(() => {
        if (!thinkFollow) return;
        _thinkBoxes.forEach((w) => {
          if (w._noFollow) return;   // 用户点过「归位」，别再把它弹回浮动
          if (w.isConnected && w.parentNode !== document.body) dockOne(w);
        });
      }).observe(messagesEl, { childList: true });
    } catch (e) { /* 忽略 */ }
  })();

  // ---------- 折叠高度补间（2026-11-12 用户要求「代码框展开/收起要有动画」）----------
  // 为什么不用纯 CSS：卡片里还有一批 display:none 的元素（输出区、按钮、编辑区），
  // 它们一出现高度就突变；CSS 只过渡了 code-body，看起来仍是「啪一下弹出来」。
  // 这里直接量「切换前 → 切换后」的真实高度，用 WAAPI 把整张卡片补间过去，最稳。
  function animHeight(el, mutate, dur = 300) {
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

  // 轻提示：顶部居中出现，2.6 秒后自动淡出
  function showToast(msg, kind = "") {    let wrap = document.getElementById("toastWrap");
    if (!wrap) {
      wrap = document.createElement("div");
      wrap.id = "toastWrap";
      wrap.className = "toast-wrap";
      document.body.appendChild(wrap);
    }
    const el = document.createElement("div");
    el.className = "toast" + (kind ? " " + kind : "");
    el.textContent = msg;
    wrap.appendChild(el);
    setTimeout(() => {
      el.style.transition = "opacity .25s";
      el.style.opacity = "0";
      setTimeout(() => el.remove(), 280);
    }, 2600);
  }
  // 给开发台（studio.js，独立 IIFE）复用同一个提示条 —— 否则两套提示风格不一致
  window.__toast = showToast;

  // 输入框弹层（自建，不用原生 prompt）。
  // ⚠️ 为什么不能用 window.prompt：桌面窗口是 WebView2，它**不支持 prompt**
  // （静默返回 null，表现成"点了按钮什么都没发生"）。alert / confirm 是支持的，
  // 所以本项目里 confirm 可以照常用，prompt 一律用这个替代。
  // ⚠️ 必须放在**最外层**作用域：文库面板是一段独立的 IIFE，
  // 把这个函数定义在别处它访问不到（实测报 `showInputDialog is not defined`）。
  function showInputDialog(opts) {
    const o = opts || {};
    const escHtml = (s) => String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    return new Promise((resolve) => {
      if (document.querySelector(".confirm-layer")) return resolve(null);
      const layer = document.createElement("div");
      layer.className = "confirm-layer";
      layer.innerHTML =
        '<div class="confirm-box">' +
        `<div class="confirm-title">${escHtml(o.title || "请输入")}</div>` +
        (o.tip ? `<div class="confirm-reason">${escHtml(o.tip)}</div>` : "") +
        `<input class="ask-free input-dialog" spellcheck="false" placeholder="${escHtml(o.placeholder || "")}" />` +
        '<div class="confirm-btns"><button class="btn ghost" data-act="cancel">取消</button>' +
        `<button class="btn primary" data-act="ok">${escHtml(o.okText || "确定")}</button></div></div>`;
      const input = layer.querySelector(".input-dialog");
      input.value = o.value || "";
      document.body.appendChild(layer);
      input.focus();
      input.select();
      const done = (val) => { layer.remove(); resolve(val); };
      layer.querySelector('[data-act="cancel"]').onclick = () => done(null);
      layer.querySelector('[data-act="ok"]').onclick = () => done((input.value || "").trim() || null);
      input.onkeydown = (e) => {
        if (e.key === "Enter") { e.preventDefault(); done((input.value || "").trim() || null); }
        else if (e.key === "Escape") { e.preventDefault(); done(null); }
      };
    });
  }

  // 兜底：api() 失败时抛出的错误，如果某个调用点忘了 try/catch，
  // 也要让用户看见原因，绝不静默 —— 静默失败比报错难查得多。
  window.addEventListener("unhandledrejection", (e) => {
    const r = e.reason;
    if (r && r.status) {
      e.preventDefault();
      showToast("操作失败：" + (r.message || r), "warn");
    }
  });
  // 复制文本到剪贴板。127.0.0.1 属于安全上下文，clipboard API 可直接用；
  // 另留一层 execCommand 兜底，兼容个别浏览器。
  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (e) {
      try {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        ta.remove();
        return true;
      } catch (e2) { return false; }
    }
  }

  // 统一请求「打开文件夹」：
  //   成功            → 提示已打开
  //   容器内无小助手  → 把宿主机真实路径复制到剪贴板
  // capEl 给了就写到它里面，否则弹 toast。
  async function openFolderRequest(url, options, capEl) {
    const say = (msg) => { if (capEl) capEl.textContent = msg; else showToast(msg, "ok"); };
    try {
      const r = await fetch(url, options);
      const d = await r.json().catch(() => ({ ok: false, detail: `服务端返回 ${r.status}（非 JSON）` }));
      if (!d.ok) { say("❌ " + (d.detail || "打开失败")); return; }
      if (d.opened === false) {
        const host = d.host_path || d.path;
        const copied = await copyText(host);
        say("📂 " + host + (copied ? "（已复制）" : ""));
        return;
      }
      say(d.via === "host-agent" ? "📂 正在打开所在文件夹…" : "📂 已打开：" + d.path);
    } catch (err) {
      say("❌ " + err);
    }
  }

  function showThinking(text) {
    const el = document.createElement("div");
    el.className = "msg bot";
    el.innerHTML = `<div class="bubble thinking">⏳ ${esc(text)}</div>`;
    messagesEl.appendChild(el);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return el;
  }
  // 回答里的**站内下载链接**渲染成可点按钮。
  // ⚠️ 为什么需要：气泡一直用 textContent（纯文本，防 XSS），而模型很爱写
  // Markdown 的 [点击下载](/api/doclib/download?rel=xxx)，用户看到的是一坨带
  // 方括号的原文，**点不动** —— 生成 PPT 这类功能等于废了一半。
  // 只认**站内路径**（以 /api/ 开头）：外链一律不自动变可点，免得模型随口
  // 编个网址就变成能点的（那种才是真危险）。
  // ⚠️ 必须放在顶层（不能塞进里面那个作用域）—— **实时回答**和**恢复历史
  // 记录**是两条不同的渲染路径，两边都要用得到。
  // 一个站内下载链接 = **两个动作**：下载到电脑 + 在「生成文库」里定位它。
  // 为什么要有第二个：文件其实**已经**在库里了（模型写完就落盘），可用户从聊天里
  // 只看到"下载"，感受上就是"没存进文件库"。给一个直接跳到它的入口才说得通。
  // 把模型写的站内链接归一化成**已经编码好**的 path+query。
  // ⚠️ 模型经常把文件名原样写成中文（`rel=我的照片展示.pptx`），
  //    直接拿去请求会失败（后端 http.client 发请求行时要按 ascii 编码，
  //    报 `'ascii' codec can't encode characters in position 29-34` —— 实测踩到）。
  //    交给浏览器解析一遍，它一定会给出编码正确的绝对地址。
  function fixApiUrl(url) {
    try {
      const u = new URL(url, location.href);
      if (u.origin !== location.origin) return url;   // 外链不动
      return u.pathname + u.search;
    } catch (e) {
      return url;
    }
  }

  // 手机上把"直接下载"换成"打开说明页"（2026-11-09）：
  //   Office 文件手机浏览器渲染不了，直接 download 在微信里常常点了没反应；
  //   /api/doclib/view 会给出文件名/大小/明确按钮，并提示"在浏览器中打开"。
  function mobileViewUrl(url) {
    try {
      const u = new URL(url, location.href);
      if (u.pathname !== "/api/doclib/download") return url;
      const coarse = window.matchMedia && window.matchMedia("(pointer: coarse)").matches;
      if (!coarse && window.innerWidth > 768) return url;
      return "/api/doclib/view" + u.search;
    } catch (e) { return url; }
  }

  window.__mobileViewUrl = mobileViewUrl;      // artifacts.js 复用

  function dlGroup(label, url) {
  // 产物卡片接管（见 artifacts.js）：正文显示成小胶囊，卡片由 flush() 追加
  if (window.__Artifacts) {
    const _chip = window.__Artifacts.chip(label, url);
    if (_chip) return _chip;
  }
    let rel = "";
    if (url.indexOf("/api/doclib/download") === 0) {
      const m = url.match(/[?&]rel=([^&]+)/);
      if (m) { try { rel = decodeURIComponent(m[1]); } catch (e) { rel = m[1]; } }
    }
    return '<span class="dl-group">' +
      `<a class="dl-link" href="${mobileViewUrl(url)}">${label}</a>` +
      (rel ? `<button type="button" class="dl-open" data-rel="${escapeHtml(rel)}"` +
             ` title="在左侧「生成文库」里定位并选中它">📂 在文件库中打开</button>` : "") +
      "</span>";
  }

  // ⚠️⚠️ 「工具调用泄漏进正文」的最后一道闸（2026-09-21）。
  // 模型偶尔不按 Ollama 的原生格式调用，而是自己套一层 XML 包装写进正文，例如：
  //     <function-call>
  //     { "name": "search_knowledge", "arguments": { … } }
  //     </function-call>
  // 原生通道只认它自己模板里的 <tool_call>，认不出这个 → 后端以前也只在代码模型
  // 那轮做清理 → 这坨 JSON 就会**原样渲染给用户看**，而工具根本没执行。
  // 后端现在会摘掉并真去执行（见 _split_text_tool_calls）；这里再兜一层，防的是：
  // 历史会话回放、以及后端没认出来的变体（大小写/空格不同、只写了开标签）。
  // ⚠️ 定义在**外层作用域**：renderAnswerLinks（历史回放也走它）在 doSend 之外。
  const TOOL_LEAK_XML_RE = /<\s*\|?\s*(?:function[ _-]?calls?|tool[ _-]?calls?)\s*\|?\s*>[\s\S]*?(?:<\s*\/\s*(?:function[ _-]?calls?|tool[ _-]?calls?)\s*>|$)/gi;
  const TOOL_LEAK_TAG_RE = /<\s*\|?\s*\/?\s*(?:function[ _-]?calls?|tool[ _-]?calls?)\s*\|?\s*\/?\s*>/gi;
  // ⚠️ 2026-11-10 修「前端输出只有一半」的根因。
  //   上面那条 TOOL_LEAK_XML_RE 结尾是 `|$`：模型只要写了一个**没闭合**的 <tool_call>
  //   （写到一半放弃 / 被长度上限截断），非贪婪的 [\s\S]*? 就会一路吃到全文末尾，
  //   把后面所有正文整段删掉 —— 实测 160 字正文洗完只剩 69 字，正是"一半"。
  //   下面改用两条新正则（旧常量保留但不再引用）：成对闭合的整块删，
  //   没闭合的只摘「标签 + 那坨调用」，后面正文一个字都不动。
  const TOOL_LEAK_XML_PAIR_RE = /<\s*\|?\s*(?:function[ _-]?calls?|tool[ _-]?calls?)\s*\|?\s*>[\s\S]*?<\s*\/\s*(?:function[ _-]?calls?|tool[ _-]?calls?)\s*>/gi;
  const TOOL_LEAK_XML_OPEN_RE = /<\s*\|?\s*(?:function[ _-]?calls?|tool[ _-]?calls?)\s*\|?\s*>/i;
  const TOOL_LEAK_FENCE_RE = /```(?:tool|tool_call)[ \t]*\r?\n[\s\S]*?```/g;
  // 「半个标签」：模型写到一半放弃，只剩 <function / <tool_call 就接正文了。
  // 实机复现过（2026-09-21）：它单独占一行，被流式推出去，正文开头冒出一截 `<function`。
  // ⚠️ 判据很窄 —— 只删"整行只有这么一截"和"出现在正文最开头"，免得吃掉讲标签用法的正常内容。
  // ⚠️ `call` 那截写成**可选**：碎片往往正是缺了它（模型刚打出 `<function` 就放弃了）。
  // 尾部只吃**标签名那种字符**，不能放 `[^>\n]` —— 那样会把同一行的正文整段吃掉。
  const TOOL_LEAK_STRAY_RE = /(^|\n)[ \t]*<[ \t]*\/?[ \t]*(?:function|tool)(?:[ _-]?calls?)?[ \t]*[A-Za-z0-9_.-]{0,24}>?[ \t]*(?=\n|$)/gi;
  const TOOL_LEAK_HEAD_RE = /^\s*<[ \t]*\/?[ \t]*(?:function|tool)(?:[ _-]?calls?)?(?=\n|[^\s>])/i;
  function stripToolLeak(text) {
    let s = String(text == null ? "" : text);
    // 内部提示不能出现在正文里（2026-11-09）：续写/补句被截断时后端会吐这句话
    s = s.replace(/\n*（输出达到 max_tokens 上限被截断）\n*/g, "\n");
    if (s.indexOf("<") < 0 && s.indexOf("```tool") < 0) return s;   // 快路径
    // ⚠️ 2026-11-10：**必须在旧 TOOL_LEAK_XML_RE 之前**先把未闭合的 tool 标签摘掉。
    //   旧正则结尾是 `|$`，遇到没闭合的 <tool_call> 会把后面全部正文吃光
    //   （实测 160 字剩 69 字，正是用户说的"只有一半"）。摘掉后旧正则就找不到开标签了。
    s = _stripUnclosedToolBlocks(s);
    s = s.replace(TOOL_LEAK_XML_RE, "").replace(TOOL_LEAK_TAG_RE, "");
    s = s.replace(TOOL_LEAK_FENCE_RE, "");
    s = s.replace(TOOL_LEAK_STRAY_RE, "$1").replace(TOOL_LEAK_HEAD_RE, "");
    return s.replace(/\n{3,}/g, "\n\n").trim();
  }

  // ---------------- 轻量 Markdown → HTML（2026-11-08）----------------
  // 没闭合的 <tool_call> / <function_call>：只摘掉标签和那坨调用，**正文一律保留**。
  // 2026-11-10：这是「输出只有一半」的修法 —— 旧正则遇到未闭合标签会一路吃到全文末尾。
  function _stripUnclosedToolBlocks(s) {
    let out = String(s || "");
    for (let guard = 0; guard < 8; guard++) {
      const m = TOOL_LEAK_XML_OPEN_RE.exec(out);
      if (!m) break;
      const start = m.index;
      const p = start + m[0].length;
      let q = p;
      while (q < out.length && /\s/.test(out[q])) q++;
      let end = p;                                  // 至少摘掉开标签本身
      if (out[q] === "{" || out[q] === "[") {
        const je = _jsonEndAt(out, q);
        if (je > 0) {
          let ok = false;
          try {
            const o = JSON.parse(out.slice(q, je));
            ok = !!(o && (o.name || o.tool || o.function || o.tool_calls));
          } catch (e) { ok = false; }
          if (ok) end = je;                         // 调用体完整 → 一起摘掉
          else {                                    // 调用体被截断 → 摘到空行/1500 字
            const rest = out.slice(q);
            const blank = rest.search(/\n[ \t]*\n/);
            end = q + (blank >= 0 ? blank : Math.min(rest.length, 1500));
          }
        }
      }
      const tail = /^\s*<\s*\/\s*(?:function[ _-]?calls?|tool[ _-]?calls?)\s*>/i.exec(out.slice(end));
      if (tail) end += tail[0].length;
      // ⚠️ 兜底：这一段里要是有成片中文，说明吃进了正文 —— 只摘标签，内容留下
      if ((out.slice(start, end).match(/[\u4e00-\u9fa5]/g) || []).length > 40) end = p;
      out = out.slice(0, start) + out.slice(end);
    }
    return out;
  }

  // 从 s[i]（应当是 { 或 [）开始做括号配对，返回配对结束后的下标；配不上返回 -1
  function _jsonEndAt(s, i) {
    let depth = 0, inStr = false, esc = false;
    for (let k = i; k < s.length; k++) {
      const ch = s[k];
      if (inStr) {
        if (esc) esc = false;
        else if (ch === "\\") esc = true;
        else if (ch === '"') inStr = false;
        continue;
      }
      if (ch === '"') { inStr = true; continue; }
      if (ch === "{" || ch === "[") depth++;
      else if (ch === "}" || ch === "]") { depth--; if (depth === 0) return k + 1; }
    }
    return -1;
  }
  // 用户的反馈：「解决完问题的总结不够结构化和美观」。
  // 之前回复是 textContent + <br>，所以 `### 标题`、`- 列表`、`**重点**` 全是原样字符。
  // 这里做一层**受控**的渲染：先 escapeHtml（防注入），再套少量块级/行内语法。
  // 刻意不引第三方库：只支持对话里真正会用到的几种，出错面小。
  function mdToHtml(text) {
    const raw = String(text == null ? "" : text);
    let esc = escapeHtml(raw);
    // ★ 2026-11-08 用户要求：「简单情况下就不用 markdown 了」。
    //   没有标题/列表/粗体/引用/表格这类标记的短文本，直接按纯文本渲染
    //   （保留原来的换行观感，也不会因为 <p> 的间距显得"空荡荡"）。
    const _hasMd = /(^|\n)\s*(#{1,4}\s|[-*•]\s|\d+[.、)]\s|>\s|\|)/.test(raw)
      || /\*\*[^*\n]+\*\*/.test(raw) || /`[^`\n]+`/.test(raw) || /https?:\/\//.test(raw) || /\[[^\]\n]{1,60}\]\([^)\s]+\)/.test(raw);
    if (!_hasMd && raw.length < 400) {
      return esc.replace(/\r?\n/g, "<br>");
    }
    // 行内：`code` / **粗** / *斜* / [文字](链接) / 裸网址 / 裸本地路径
    // ★ 2026-11-10 用户要求：回答里写的本地路径、网址要能点开
    const pathKind = (s) => {
      if (/^https?:\/\/\S+$/i.test(s)) return "url";
      if (s.indexOf("/api/") === 0) return "";
      if (/^[a-zA-Z]:[\\/]/.test(s)) return "path";
      if (/^\\\\[^\\]/.test(s)) return "path";
      if (/^~\//.test(s)) return "path";
      if (/^\/(Users|home|var|tmp|opt|mnt|Volumes|srv)\//.test(s)) return "path";
      // 相对路径（data/library/x.pptx 这种）：带斜杠且是已知文件后缀才算
      if (/[\\/]/.test(s) && /\.(py|js|ts|json|md|txt|zip|rar|7z|pptx|docx|xlsx|csv|pdf|png|jpe?g|gif|mp4|mp3|exe|log)$/i.test(s)) return "path";
      return "";
    };
    const anchorHtml = (kind, s) => (kind === "url"
      ? '<a class="ext-link" href="' + s + '" target="_blank" rel="noopener">' + s + "</a>"
      : '<a class="path-link" href="javascript:void(0)" data-path="' + s + '" title="点击打开">' + s + "</a>");
    const inline = (s) => {
      const held = [];
      const hold = (h) => { held.push(h); return "\u0000H" + (held.length - 1) + "\u0000"; };
      s = s.replace(/`([^`\n]{1,300})`/g, (m, code) => {
        const k = pathKind(code);
        return hold(k ? anchorHtml(k, code) : '<code class="md-code">' + code + "</code>");
      });
      s = s.replace(/\[([^\]\n]{1,60})\]\(([^)\s]+)\)/g, (m, label, url) => {
        if (url.indexOf("/api/") === 0) return hold(dlGroup(label, fixApiUrl(url)));
        return hold('<a href="' + url + '" target="_blank" rel="noopener">' + label + "</a>");
      });
      s = s.replace(/\*\*([^*\n]{1,80})\*\*/g, "<b>$1</b>")
        .replace(/(^|[^*])\*([^*\n]{1,60})\*(?!\*)/g, "$1<i>$2</i>");
      s = s.replace(/\bhttps?:\/\/[^\s<>"'）)】\]，。；：、]+/g, (u) => hold(anchorHtml("url", u)));
      s = s.replace(/(^|[\s（(【\[：:、，])([a-zA-Z]:\\[^\s<>"'（）()【】\[\]，。；：、]{2,})/g,
        (m, pre, p) => pre + hold(anchorHtml("path", p)));
      return s.replace(/\u0000H(\d+)\u0000/g, (m, i) => held[+i]);
    };

    const lines = esc.split(/\r?\n/);
    const out = [];
    let list = "";
    const closeList = () => { if (list) { out.push("</" + list + ">"); list = ""; } };
    for (const line of lines) {
      const t = line.trim();
      if (!t) { closeList(); out.push('<div class="md-gap"></div>'); continue; }
      let m = /^(#{1,4})\s+(.*)$/.exec(t);
      if (m) { closeList(); const lv = Math.min(5, m[1].length + 2); out.push('<h' + lv + ' class="md-h">' + inline(m[2]) + '</h' + lv + '>'); continue; }
      if (/^([-*_]{3,})$/.test(t)) { closeList(); out.push('<hr class="md-hr">'); continue; }
      m = /^[-*•]\s+(.*)$/.exec(t);
      if (m) { if (list !== "ul") { closeList(); out.push('<ul class="md-ul">'); list = "ul"; } out.push("<li>" + inline(m[1]) + "</li>"); continue; }
      m = /^(\d+)[.、)]\s+(.*)$/.exec(t);
      if (m) { if (list !== "ol") { closeList(); out.push('<ol class="md-ol">'); list = "ol"; } out.push("<li>" + inline(m[2]) + "</li>"); continue; }
      // ⚠️ 这里是**转义之后**的文本，行首的 > 已经变成 &gt;，要按转义后的形式匹配
      m = /^(?:>|&gt;)\s?(.*)$/.exec(t);
      if (m) { closeList(); out.push('<blockquote class="md-quote">' + inline(m[1]) + "</blockquote>"); continue; }
      closeList();
      out.push('<p class="md-p">' + inline(t) + "</p>");
    }
    closeList();
    return out.join("");
  }

  function renderAnswerLinks(bubble, text) {
  // 产物卡片：先开启收集，正文里的文件链接变成胶囊，最后 flush 出卡片
  if (window.__Artifacts) window.__Artifacts.begin();
    // 结构化渲染（标题/列表/粗体/行内代码/链接），下载链接仍然渲染成按钮
    let html = mdToHtml(stripToolLeak(text));
    // 裸路径也要变按钮（模型常写「下载链接：/api/...」，前面是全角冒号；
    // 尾部要挡住中文标点，否则会把句号逗号一起吞进 URL）
    html = html.replace(
      /(^|[\s（(【\[:：,，、>])(\/api\/(?:doclib\/download|ws\/zip)[^\s<)）】\]，。；：、"'']*)/g,
      (_, pre, url) => `${pre}${dlGroup("点击下载", fixApiUrl(url))}`);
    bubble.innerHTML = html;
    // 追加可折叠的产物卡片（默认收起）
    if (window.__Artifacts) window.__Artifacts.flush(bubble);
  }

  // 跳到「生成文库」并把这个文件选中、闪一下（列表长的时候也能一眼找到）
  async function openInLibrary(rel) {
    const panel = document.getElementById("doclibPanel");
    if (!panel) { showToast("界面上找不到「生成文库」面板", "warn"); return false; }
    if (window.__dlExpand) window.__dlExpand();
    if (window.innerWidth <= 768 && typeof window.__openSidebar === "function") {
      window.__openSidebar();                 // 手机：先拉开抽屉，否则在屏幕外看不见
    }
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
    await loadDoclib(rel || "");          // 刷新列表 + 选中（内部会打上 active）
    let hit = null;
    document.querySelectorAll(".dl-item").forEach((el) => {
      if (el.dataset.rel === rel) hit = el;
    });
    if (hit) {
      hit.classList.add("dl-flash");
      setTimeout(() => hit.classList.remove("dl-flash"), 2600);
      showToast("已在生成文库中定位：" + rel, "ok");
    } else {
      showToast("文件库里没找到「" + rel + "」（可能已被删除或改名）", "warn");
    }
    return true;
  }
  window.__openInLibrary = openInLibrary;

  // ---------- 地图卡片 ----------
  // 后端把地点 / 路线算好（真实坐标、真实距离用时），前端只负责画。
  // 瓦片走后端代理 `/api/map/tile/{z}/{x}/{y}.png`（后端换镜像，并统一转发）。
  // ⚠️ 地图**不缓存**：后端响应 no-store，离线时瓦片直接 404、卡片不显示底图。
  // Leaflet 本身放在本地 vendor 里，不依赖外网 CDN。
  let mapSeq = 0;
  function makeMapCard(ui) {
    const wrap = document.createElement("div");
    wrap.className = "msg bot";
    const box = document.createElement("div");
    box.className = "map-card";
    wrap.appendChild(box);

    const rt = ui.route || null;
    const online = ui.online !== false;      // 后端会把当前模式带下来
    const info = document.createElement("div");
    info.className = "map-info";
    let html = `<span class="map-badge ${online ? "on" : "off"}">` +
      (online ? "联网" : "离线") + `</span>` +
      // 底图是哪家，直接标出来 —— 不然用户看不出这张图是"换了个地图"
      // （高德底图是中文路网图，和 OSM 那种清淡的志愿者图一眼就能分辨）
      `<span class="map-basemap">${ui.tile_source === "amap" ? "高德底图" : "OSM 底图"}</span>`;
    if (rt && rt.straight) {
      // 离线兜底：没有路网数据，只能给直线距离 —— 必须说清楚，
      // 否则用户会把"直线 6 公里"当成"开车 6 公里"。
      html += ` <b>${escapeHtml(rt.from || "")} → ${escapeHtml(rt.to || "")}</b>` +
        `　直线 ${escapeHtml(rt.distance || "")}` +
        (rt.bearing ? `，在起点${escapeHtml(rt.bearing)}方向` : "") +
        `<span class="map-warn">非实际道路</span>`;
    } else if (rt) {
      html += ` <b>${escapeHtml(rt.from || "")} → ${escapeHtml(rt.to || "")}</b>` +
        `　${escapeHtml(rt.mode || "")} ${escapeHtml(rt.distance || "")}` +
        `，约 ${escapeHtml(rt.duration || "")}`;
      if ((rt.routes || []).length > 1) {
        html += `<span class="map-alt">共 ${rt.routes.length} 条可选</span>`;
      }
      // 步行/骑行的距离其实来自驾车路网、时间只是估算 —— 不能不说
      if (rt.estimated) {
        html += `<span class="map-warn">时间为估算</span>`;
      }
    } else if (ui.nearby && (ui.nearby.items || []).length) {
      // 周边搜索：信息栏要说清"找的是什么、几家"，别只说"N 个地点"
      html += ` <b>${escapeHtml(ui.nearby.center_name || "")}</b> 周边 ` +
        `${ui.nearby.radius} 米内的${escapeHtml(ui.nearby.category || "")}` +
        `（${ui.nearby.total} 家）`;
    } else if ((ui.markers || []).length) {
      html += ` <b>${escapeHtml(ui.markers[0].name || "")}</b>` +
        ((ui.markers || []).length > 1 ? ` 等 ${ui.markers.length} 个地点` : "");
    }
    // 天气（只有请求里带 weather:true 才有）
    const wt = (rt && rt.weather) || null;
    if (wt) {
      const fn = wt.from_now || {}, td = wt.to_day || {};
      const bits = [];
      if (fn.desc) {
        bits.push("出发地此刻 " + escapeHtml(fn.desc) +
          (fn.temp != null ? " " + Math.round(fn.temp) + "℃" : ""));
      }
      if (td.desc) {
        // ⚠️ 写「抵达那天」而不是「抵达时」：高德没有逐小时接口，
        //    给的是**当天的逐日预报**。写成"抵达时"就等于骗人。
        bits.push("抵达那天（" + escapeHtml(wt.to_day_label || "当天") + "）" +
          escapeHtml(td.desc) +
          (td.low != null && td.high != null
            ? " " + Math.round(td.low) + "~" + Math.round(td.high) + "℃" : ""));
      }
      if (bits.length) {
        const src = wt.src || "";
        html += `<div class="map-weather">${bits.join("　·　")}` +
          (src ? `<span class="map-src">（数据源 ${escapeHtml(src)}）</span>` : "") +
          `</div>`;
      }
    }
    if (rt && rt.note) {
      html += `<div class="map-note">${escapeHtml(rt.note)}</div>`;
    }
    // 出行方式对比（后端用同一套路网算出来的真实数据）
    if (rt && (rt.modes || []).length) {
      const chips = rt.modes.map(function (m) {
        return `<span class="mode-chip${m.suggest ? " on" : ""}">` +
          `${escapeHtml(m.mode)} ${escapeHtml(m.distance)} · ${escapeHtml(m.duration)}` +
          (m.estimated ? "（估算）" : "") + `</span>`;
      }).join("");
      html += `<div class="map-modes">${chips}` +
        (rt.suggest_reason
          ? `<div class="map-note">建议：${escapeHtml(rt.suggest_reason)}</div>` : "") +
        `</div>`;
    }
    // 周边场所清单
    const nb = ui.nearby || null;
    if (nb && (nb.items || []).length) {
      const rows = nb.items.map(function (it) {
        const extra = [];
        if (it.cost) extra.push("人均 " + escapeHtml(it.cost) + " 元");
        if (it.addr) extra.push(escapeHtml(it.addr));
        if (it.phone) extra.push("电话 " + escapeHtml(it.phone));
        if (it.hours) extra.push("营业 " + escapeHtml(it.hours));
        // 走高德时有**真实评分** —— 这是用户最想看的
        const rt = it.rating
          ? `<span class="nb-rating">★ ${escapeHtml(it.rating)}</span>` : "";
        return `<li><b>${escapeHtml(it.name)}</b>` +
          `<span class="nb-dist">${escapeHtml(it.dist)}</span>` + rt +
          (extra.length ? `<div class="nb-extra">${extra.join("　·　")}</div>` : "") +
          (it.note ? `<div class="nb-note">${escapeHtml(it.note)}</div>` : "") +
          `</li>`;
      }).join("");
      // 信息栏已经写了「汕头大学 周边 2000 米内的餐厅（3 家）」，
      // 这里别再放一遍标题（实测会连着出现两遍，很啰嗦）
      html += `<div class="map-nearby">` +
        (nb.source === "amap"
          ? `<div class="nb-src">数据来自高德地图</div>` : "") +
        `<ul>${rows}</ul></div>`;
    }
    info.innerHTML = html || "地图";

    const canvas = document.createElement("div");
    canvas.className = "map-canvas";
    canvas.id = "mmMap" + (++mapSeq);

    box.appendChild(info);
    box.appendChild(canvas);
    messagesEl.appendChild(wrap);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    const id = canvas.id;
    // 等这一帧排完再初始化，否则容器高度还是 0，地图画不出来
    requestAnimationFrame(() => initMapCard(id, ui));
    return wrap;
  }

  // 离线时地图上没有底图 → 给一句明白话，别让用户对着灰底猜"是不是坏了"
  function mapOfflineTip(el) {
    if (el.querySelector(".map-offline-tip")) return;
    const d = document.createElement("div");
    d.className = "map-offline-tip";
    d.innerHTML = "离线模式：不显示地图底图。" +
      "<br>地点、路线这些结论照样给；要看底图把顶栏的「联网」打开。";
    el.appendChild(d);
  }

  // ---------- 坐标系：WGS-84 → GCJ-02（火星坐标） ----------
  // 为什么必须有这个：高德底图是 **GCJ-02** 的，而我们的数据（GPS / OpenStreetMap）
  // 是 **WGS-84** 的，两者在国内差 **50~500 米**。直接把 WGS-84 的点画到高德底图上，
  // 视觉上就是"点和路整体错开一个街区" —— 看起来像数据查错了，其实只是没换算。
  // 这里跟后端 backend/amap.py 的算法保持一致（同一套公式，结果才是同一处）。
  const GCJ_A = 6378245.0;
  const GCJ_EE = 0.00669342162296594323;

  function gcjOutOfChina(lng, lat) {
    return !(lng > 73.66 && lng < 135.05 && lat > 3.86 && lat < 53.55);
  }
  function gcjTfLat(x, y) {
    let r = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y +
      0.2 * Math.sqrt(Math.abs(x));
    r += (20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0 / 3.0;
    r += (20.0 * Math.sin(y * Math.PI) + 40.0 * Math.sin(y / 3.0 * Math.PI)) * 2.0 / 3.0;
    r += (160.0 * Math.sin(y / 12.0 * Math.PI) + 320 * Math.sin(y * Math.PI / 30.0)) * 2.0 / 3.0;
    return r;
  }
  function gcjTfLng(x, y) {
    let r = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
    r += (20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0 / 3.0;
    r += (20.0 * Math.sin(x * Math.PI) + 40.0 * Math.sin(x / 3.0 * Math.PI)) * 2.0 / 3.0;
    r += (150.0 * Math.sin(x / 12.0 * Math.PI) + 300.0 * Math.sin(x / 30.0 * Math.PI)) * 2.0 / 3.0;
    return r;
  }
  // 入参/出参都是 [lat, lon]（跟着 Leaflet 的习惯走，免得来回调顺序出错）
  function wgs2gcj(lat, lon) {
    if (gcjOutOfChina(lon, lat)) return [lat, lon];
    let dLat = gcjTfLat(lon - 105.0, lat - 35.0);
    let dLon = gcjTfLng(lon - 105.0, lat - 35.0);
    const rad = lat / 180.0 * Math.PI;
    let magic = Math.sin(rad);
    magic = 1 - GCJ_EE * magic * magic;
    const sq = Math.sqrt(magic);
    dLat = (dLat * 180.0) / ((GCJ_A * (1 - GCJ_EE)) / (magic * sq) * Math.PI);
    dLon = (dLon * 180.0) / (GCJ_A / sq * Math.cos(rad) * Math.PI);
    return [lat + dLat, lon + dLon];
  }

  function initMapCard(elId, ui) {
    const el = document.getElementById(elId);
    if (!el) return;
    if (typeof L === "undefined") {
      el.innerHTML = '<div class="hint" style="padding:10px">地图组件没加载出来' +
        '（frontend/lib/leaflet 缺文件）</div>';
      return;
    }
    // ---- 底图：联网 + 配了高德 key → 高德；其余（离线 / 没配 key）→ OSM ----
    // 离线时其实两张底图都取不到（不缓存），但 tile_source 仍然要定下来 ——
    // 前端据此决定打点要不要做 WGS-84 → GCJ-02 换算，不能少。
    const useAmap = ui.tile_source === "amap";
    const toTile = useAmap
      ? function (la, lo) { return wgs2gcj(la, lo); }
      : function (la, lo) { return [la, lo]; };
    const ptsOf = function (arr) {
      return (arr || []).map(function (p) { return toTile(p[0], p[1]); });
    };
    const center = ui.center || [23.13, 113.26];
    const map = L.map(el, { zoomControl: true, attributionControl: true })
      .setView(toTile(center[0], center[1]), ui.zoom || 12);

    // 瓦片一律走后端代理（国内直连 OSM 官方瓦片经常超时，后端统一换了镜像）。
    // ⚠️ 后端**不做任何缓存**、响应带 no-store，所以离线时这里就是 404，
    //    要靠 tileerror 接住、给用户一句明白话。
    let tileErr = 0;
    const layer = L.tileLayer(
      useAmap ? "/api/map/amap/{z}/{x}/{y}.png" : "/api/map/tile/{z}/{x}/{y}.png", {
        minZoom: 3, maxZoom: 19,
        attribution: useAmap
          ? "地图数据 © 高德地图（GCJ-02）"
          : "地图数据 © OpenStreetMap 贡献者",
      });
    layer.on("tileerror", function () {
      tileErr++;
      if (ui.online === false && tileErr >= 5) mapOfflineTip(el);
    });
    layer.addTo(map);

    const span = [];
    (ui.markers || []).forEach(function (mk) {
      const p = toTile(mk.lat, mk.lon);
      const icon = L.divIcon({ className: "mm-pin", html: "<i></i>",
                               iconSize: [18, 18], iconAnchor: [9, 9] });
      const m1 = L.marker(p, { icon: icon, title: mk.name || "" }).addTo(map);
      const body = "<b>" + escapeHtml(mk.name || "") + "</b>" +
        (mk.addr ? "<br><span style='color:#666'>" + escapeHtml(mk.addr) + "</span>" : "");
      m1.bindPopup(body);
      span.push(p);
    });

    const rt = ui.route;
    if (rt && (rt.routes || []).length > 1) {
      // 多条候选：**推荐的那条蓝色加粗实线**，其余灰色虚线 —— 一眼看出主次。
      // 点线还能弹出各自的距离/时间/推荐理由。
      rt.routes.forEach(function (r) {
        if (!r.points || r.points.length < 2) return;
        const rec = !!r.recommended;
        const pts = ptsOf(r.points);       // 高德底图时这里会转成 GCJ-02
        const line = L.polyline(pts, {
          color: rec ? "#2e75b6" : "#9aa7b4",
          weight: rec ? 6 : 4,
          opacity: rec ? 0.9 : 0.65,
          dashArray: rec ? null : "7 7",
        }).addTo(map);
        line.bindPopup((rec ? "<b>推荐路线</b>" : "备选路线") + "<br>" +
          escapeHtml(r.distance || "") + "，约 " + escapeHtml(r.duration || "") +
          (r.reason ? "<br><span style='color:#666'>" + escapeHtml(r.reason) + "</span>" : ""));
        span.push.apply(span, pts);
      });
    } else if (rt && (rt.points || []).length > 1) {
      const pts = ptsOf(rt.points);
      L.polyline(pts, { color: "#2e75b6", weight: 5, opacity: 0.85 })
        .addTo(map);
      span.push.apply(span, pts);
    } else if (rt && rt.straight) {
      // 离线兜底：没有路网，用橙色虚线连两点，一眼就能和真路线区分开
      const ms = ui.markers || [];
      const pa = ms.filter(function (m) { return m.name === ui.route.from; })[0] || ms[0];
      const pb = ms.filter(function (m) { return m.name === ui.route.to; })[0] ||
                 ms[ms.length - 1];
      if (pa && pb) {
        L.polyline([toTile(pa.lat, pa.lon), toTile(pb.lat, pb.lon)],
                   { color: "#e08a2e", weight: 3, dashArray: "8 8", opacity: 0.9 })
          .addTo(map);
      }
    }
    try {
      if (span.length > 1) map.fitBounds(L.latLngBounds(span).pad(0.18));
      else if (span.length === 1) map.setView(span[0], ui.zoom || 14);
    } catch (e) { /* 只有一个点之类，保持默认视野即可 */ }
    // 卡片在聊天流里，容器尺寸可能要等一帧才稳定
    setTimeout(function () { try { map.invalidateSize(); } catch (e) {} }, 260);
    window.__mmMaps = (window.__mmMaps || []).concat([map]);
  }

  // 高德 key 面板：填了立刻生效，不用重启，也不用去改 config.json
  // ---------- 高德 key 的状态：按钮亮/暗 + 出问题主动提醒 ----------
  // 需求来自用户：配好了按钮要**亮起**，失效/被重置要**变暗**并催他重配，
  // 而且要**自动持续检测**、出异常就提醒。
  // ⚠️ 为什么要"持续检测"：key 会因为额度用满 / 控制台重置 / 服务端异常而
  //    不声不响地失效 —— 用户只会觉得"地图怎么突然变难用了"，根本不知道原因。
  let _amapState = null;          // 上一次的状态，用来判断"刚刚变坏了"
  let _amapSelfAction = false;    // 刚才是用户自己点的连接/断开 —— 那是主动行为，别报警
  let _amapSeq = 0;               // 请求序号：只认最后一次发出的那个请求的结果
  // force=true 时走 `?force=1`：**无视后端缓存、真调一次高德**。
  // 用在两个地方：① 用户刚把「联网」打开（那时必须重新确认 key 通不通）；
  //              ② 面板上的「重新验证」按钮。
  async function refreshAmapState(force) {
    const btn = document.getElementById("amapKeyBtn");
    if (!btn) return null;
    const seq = ++_amapSeq;
    let s = null;
    try {
      s = await api("/api/map/amap_status" + (force ? "?force=1" : ""));
    } catch (e) {
      return null;                // 读不到就别瞎标，保持现状
    }
    // ⚠️ 期间又发起了更新的请求（比如用户连着拨了两次「联网」开关）→
    //    这一份已经过期，丢掉。不加这个守卫的话，先发的慢请求**后到**，
    //    会把界面刷回旧状态，看着就是"按钮没跟上开关"。
    if (seq !== _amapSeq) return null;
    const prev = _amapState;
    _amapState = s;
    // ⚠️ 关掉「联网」开关时，高德**根本用不了**（一个请求都发不出去），
    //    所以按钮也要跟着变暗 —— 但**不能报红**：那不是 key 坏了，是用户自己关的。
    //    点它要给的是"先打开联网"的提示，不是"你的 key 有问题"。
    const online = s.online !== false;
    const armed = !!(s.configured && s.enabled) && online;   // 真正在用的状态
    // 五种样子：
    //   可用              → 亮起（绿 + ●）
    //   联网开着但不可用  → 变暗 + 红「!」
    //   用户主动断开      → 变暗（key 还留着，不报红）
    //   没配              → 变暗
    //   **联网关掉**      → 变暗（也不能报红）
    btn.classList.toggle("on", online && !!s.ok);
    btn.classList.toggle("bad", armed && !s.ok);
    // 文字保持固定，状态靠**颜色 + 右上角标记**表达（●=可用 / !=异常）——
    // 这样它和顶栏其它开关长得一样，不再是一个"跳出来"的按钮。
    btn.textContent = "高德 key";
    btn.title = !online
      ? "离线模式：高德用不了 —— 先打开顶栏的「联网」开关"
      : (s.ok
          ? ("已连接（" + (s.key_hint || "") + "）· 点一下查看、验证、断开或更换 key")
          : (armed
              ? ("⚠️ 当前不可用：" + (s.message || "原因未知") + " —— 点一下处理")
              : (s.configured
                  ? "已断开（key 还留着）· 点一下可以重新连接"
                  : "还没配高德 key。点一下配置：填了能搜到全国小店、有真实评分和实时路况")));

    // ⚠️ 只在"从可用变成不可用"的那一下提醒，不是每次都弹；
    //    而且**用户自己点断开 / 自己关联网时不要弹**（那是他要的，不是故障）。
    const self = _amapSelfAction;
    _amapSelfAction = false;
    if (!self && prev && prev.ok && !s.ok && s.enabled && online) {
      showToast("⚠️ 高德 key 现在用不了了：" + (s.message || "原因未知") +
                "　点顶栏「高德 key」处理一下", "warn");
    }
    return s;
  }
  window.__refreshAmapState = refreshAmapState;
  // 45 秒轮询一次（后端那边自己带节流：正常 15 分钟才真调一次高德）
  setInterval(refreshAmapState, 45000);
  setTimeout(refreshAmapState, 1200);

  // ⚠️ 地图**不做任何本地缓存**（2026-09-18 起），所以原来那个「地图缓存」按钮没了，
  //    换成这个 —— 它解决的才是真问题（新机器上没 key，地图只能退回 OpenStreetMap）。
  //
  // 面板的交互是按用户要求定的：
  //   · **连接 / 断开** 是个开关：断开**不删 key**（"重新输入太麻烦"），只是不用它；
  //   · 已经存下的 key **只读显示**，不能直接改；
  //   · 要换 key 得点「更改 API」：输入新的 → 验证成功才替换旧的 → 失败则原 key 一字不动；
  //   · 「重新验证」：无视缓存立刻真调一次高德，确认现在到底通不通。
  async function showAmapKey() {
    if (document.querySelector(".amap-layer")) return;
    await refreshAmapState();                 // 先刷新一下，弹出来的状态才是准的
    // 关着「联网」时点它：先把话说清楚，别让用户以为是 key 出问题了。
    // （面板照样打开 —— 状态、key、断开/更改 这些在离线时也要能看能管。）
    if (_amapState && _amapState.online === false) {
      showToast("现在是离线模式，高德用不了 —— 先打开顶栏的「联网」开关", "warn");
    }
    let full = "";
    try {
      const d = await api("/api/config");
      full = String(((d || {}).config || {}).amap_key || "");
    } catch (e) { /* 读不到就按没配处理，下面会说实话 */ }

    const layer = document.createElement("div");
    layer.className = "confirm-layer amap-layer";
    layer.innerHTML =
      '<div class="confirm-box">' +
      '<div class="confirm-title">🗺️ 高德地图</div>' +
      '<div class="amap-state"></div>' +
      '<div class="amap-keyrow">' +
      '<input class="amap-input" type="text" readonly />' +
      '<button class="btn ghost amap-change" type="button">更改 API</button>' +
      '</div>' +
      '<div class="amap-edit" style="display:none">' +
      '<div class="amap-edit-hint">粘贴**新的** key。验证通过才会替换掉现在这个；' +
      '验证不通过的话，原来的 key 不动。</div>' +
      '<input class="amap-newinput" type="text" placeholder="把新的 32 位 key 粘贴在这里" />' +
      '</div>' +
      '<div class="confirm-reason amap-tip">' +
      '配了 key 之后，地图能搜到全国的小店、有真实评分、有实时路况和公交换乘，' +
      '步行骑行也是真实路径。<br>断开或没配时退回 OpenStreetMap —— ' +
      '能查大城市/道路/机场车站，但小店、评分、路况、公交都没有。<br>' +
      '申请（1 分钟）：手机号注册 + 实名 → 建应用 → 加 Key → ' +
      '<b>服务平台必须选「Web 服务」</b>（选成「Web 端(JS API)」用不了）。' +
      '</div>' +
      // 申请入口：**始终显示**（切到"改 key"时上面的 tip 会隐藏，那时最需要这个链接）
      '<div class="amap-golink">👉 还没有 key？' +
      '<a href="https://console.amap.com/dev/key/app" target="_blank" rel="noopener">' +
      '去高德控制台申请 / 管理 Key</a></div>' +
      '<div class="amap-msg"></div>' +
      '<div class="confirm-btns amap-btns"></div>' +
      '</div>';
    document.body.appendChild(layer);

    const stateEl = layer.querySelector(".amap-state");
    const input = layer.querySelector(".amap-input");
    const newInput = layer.querySelector(".amap-newinput");
    const editBox = layer.querySelector(".amap-edit");
    const keyRow = layer.querySelector(".amap-keyrow");
    const changeBtn = layer.querySelector(".amap-change");
    const tipEl = layer.querySelector(".amap-tip");
    const msg = layer.querySelector(".amap-msg");
    const btnsEl = layer.querySelector(".amap-btns");
    const say = (t, kind) => { msg.textContent = t; msg.className = "amap-msg " + (kind || ""); };
    let editing = false;

    function render() {
      const s = _amapState || {};
      const online = s.online !== false;
      const armed = !!(s.configured && s.enabled) && online;
      let line, cls;
      if (!online) {
        // ⚠️ 离线时**先解释清楚这是什么原因** —— 不然用户会以为是 key 坏了
        line = "🌐 离线模式 —— 高德现在用不了（打开顶栏的「联网」开关就会自动恢复）。" +
               (s.configured ? "key 还在，不用重输。" : "");
        cls = "";
      } else if (s.ok) {
        line = "✅ 配置成功，正在使用（" + (s.key_hint || "") + "）"; cls = "ok";
      } else if (armed) {
        line = "⚠️ 已配置但**当前不可用**：" + (s.message || "原因未知"); cls = "bad";
      } else if (s.configured) {
        line = "⏸ 已断开 —— **key 还留着**，点「连接」就能恢复（不用重新输入）"; cls = "";
      } else if (s.configured === false) {
        line = "未配置 —— 地图在用 OpenStreetMap（数据弱一些）"; cls = "";
      } else {
        line = "状态读取失败（后端没响应？）—— 可以点「更改 API」或「自动验证」重试"; cls = "bad";
      }
      stateEl.textContent = line;
      stateEl.className = "amap-state " + cls;

      // key 只读展示：有就显示完整 key（本地应用，方便核对）
      input.value = s.configured ? full : "";
      input.placeholder = s.configured ? "" : "（还没有配 key）";
      changeBtn.style.display = editing ? "none" : "";
      keyRow.style.display = editing ? "none" : "";
      editBox.style.display = editing ? "" : "none";
      tipEl.style.display = editing ? "none" : "";

      let html = '<button class="btn ghost" data-act="close">关闭</button>';
      if (editing) {
        html += '<button class="btn ghost" data-act="cancel">取消</button>' +
                '<button class="btn primary" data-act="save">验证并保存</button>';
      } else {
        // 「自动验证」：无视缓存，立刻真调一次高德（离线时没有意义，不显示）
        if (online && s.configured) {
          html += '<button class="btn ghost" data-act="verify">自动验证</button>';
        }
        if (s.configured) {
          html += '<button class="btn ' + (s.enabled ? "ghost" : "primary") +
                  '" data-act="toggle">' + (s.enabled ? "断开" : "连接") + '</button>';
        } else {
          html += '<button class="btn primary" data-act="change">填一个 key</button>';
        }
      }
      btnsEl.innerHTML = html;

      btnsEl.querySelector('[data-act="close"]').onclick = () => layer.remove();
      const c = btnsEl.querySelector('[data-act="cancel"]');
      if (c) c.onclick = () => { editing = false; say(""); render(); };
      const vf = btnsEl.querySelector('[data-act="verify"]');
      if (vf) vf.onclick = () => doVerify(vf);
      const t = btnsEl.querySelector('[data-act="toggle"]');
      if (t) t.onclick = () => doToggle();
      const ch = btnsEl.querySelector('[data-act="change"]');
      if (ch) ch.onclick = () => { editing = true; say(""); render(); newInput.focus(); };
      const sv = btnsEl.querySelector('[data-act="save"]');
      if (sv) sv.onclick = () => doSave();
      changeBtn.onclick = () => { editing = true; say(""); render(); newInput.focus(); };
    }

    async function doToggle() {
      const s = _amapState || {};
      const turnOn = !s.enabled;
      btnsEl.querySelectorAll("button").forEach((b) => { b.disabled = true; });
      say(turnOn ? "正在连接…" : "正在断开…", "");
      try {
        const r = await api("/api/map/amap_toggle", {
          method: "POST", body: JSON.stringify({ enabled: turnOn }) });
        if (r && r.ok) {
          _amapSelfAction = true;          // 这是我点的，别弹"key 用不了了"
          await refreshAmapState();
          say(r.message || (turnOn ? "已连接" : "已断开"), turnOn ? "ok" : "");
          showToast(turnOn ? "高德已连接" : "已断开高德（key 还留着）",
                    turnOn ? "ok" : "warn");
        } else {
          say((r && r.message) || "操作失败", "bad");
        }
      } catch (e) {
        say("连不上后端：" + String(e.message || e), "bad");
      } finally {
        btnsEl.querySelectorAll("button").forEach((b) => { b.disabled = false; });
        render();
      }
    }

    // 「自动验证」：无视后端缓存，**立刻真调一次高德**，把结论直接摆在面板上。
    // 为什么要这个按钮：key 会因为额度、控制台重置等原因不声不响失效，
    // 用户想"我确认一下现在到底通不通"时，不该只能等 15 分钟一次的自动复查。
    async function doVerify(btnEl) {
      if (btnEl) btnEl.disabled = true;
      say("正在连高德验证…（这一步会真的调一次高德接口）", "");
      try {
        const r = await api("/api/map/amap_status?force=1");
        _amapState = r;
        if (r && r.online === false) {
          say("现在是离线模式，没法验证 —— 先打开顶栏的「联网」开关。", "bad");
        } else if (r && r.ok) {
          say("✅ 验证通过：" + (r.message || "高德可以正常使用") +
              "\n（刚才实时查到：" + (r.key_hint || "") + "）", "ok");
          showToast("高德验证通过，一切正常", "ok");
        } else if (r && r.configured === false) {
          say("还没配高德 key —— 点「填一个 key」配一下。", "bad");
        } else {
          say("❌ 验证没通过：" + ((r && r.message) || "原因未知") +
              "\n（可以点「更改 API」换一个 key，或者先「断开」用回 OpenStreetMap）", "bad");
          showToast("高德验证没通过，看面板里的原因", "warn");
        }
      } catch (e) {
        say("验证请求失败：" + String(e.message || e), "bad");
      } finally {
        render();
      }
    }

    async function doSave() {
      const v = (newInput.value || "").trim();
      if (!v) { say("还没有填新的 key 呢。", "bad"); newInput.focus(); return; }
      const old = input.value || "";
      btnsEl.querySelectorAll("button").forEach((b) => { b.disabled = true; });
      say("正在连高德验证…（验证通过才会替换）", "");
      try {
        const r = await api("/api/map/amap_key", {
          method: "POST", body: JSON.stringify({ key: v }) });
        if (r && r.ok) {
          _amapSelfAction = true;
          const d = await api("/api/config").catch(() => null);
          full = String((((d || {}).config || {}).amap_key) || v);
          editing = false;
          newInput.value = "";
          await refreshAmapState();
          say(r.message || "已改用新的 key", "ok");
          showToast("高德 key 已更新", "ok");
        } else {
          // ⚠️ 失败时旧 key 必须原样不动 —— 把这句话说出来，用户才敢放心重试
          say(((r && r.message) || "验证失败") +
              "\n（原来的 key 没有被改动，还是 " + (old ? old.slice(0, 6) + "…" : "原来那个") + "）",
              "bad");
        }
      } catch (e) {
        say("连不上后端：" + String(e.message || e), "bad");
      } finally {
        btnsEl.querySelectorAll("button").forEach((b) => { b.disabled = false; });
        render();
      }
    }

    newInput.onkeydown = (e) => {
      if (e.key === "Enter") { const b = btnsEl.querySelector('[data-act="save"]'); if (b) b.click(); }
    };
    render();
  }
  // ---------- 采样参数（温度 / top-p）：拖动即生效 ----------
  // 为什么做成"滑条 + 建议值"而不是输入框：
  //   这两个参数**没有唯一正确答案**，用户需要的是"手感"（拖一下马上看效果）
  //   和一个锚点（建议值）。所以每条滑条旁边都标着建议值，点一下就能跳回去。
  //
  // ⚠️ 生效时机：后端每次 /api/chat 都会重新读 config（`cfg = config.load_config()`），
  //    所以**拖完立刻生效、不用重启** —— 这句话必须在界面上说明白，
  //    否则用户会以为"改了没反应"（这类误会以前在 num_ctx 上踩过）。
  const SAMPLE_META = {
    temperature: {
      name: "温度 temperature", min: 0, max: 2, step: 0.05, best: 0.6,
      less: "更严谨", more: "更发散",
      hint: "越高越发散：话更活、也更容易跑偏和胡编；越低越稳、越可复现。" +
            "思考型模型（Qwen3 这类）官方推荐 0.6 —— 写代码、要结论稳定时用 0.2~0.4，" +
            "头脑风暴可以 1.0~1.3；超过 1.5 基本就开始胡说八道了。",
    },
    top_p: {
      name: "采样范围 top-p", min: 0.1, max: 1, step: 0.01, best: 0.95,
      less: "更死板", more: "更野",
      hint: "只在「概率加起来到 top-p 的那一批词」里挑下一个词。0.95 既排掉长尾的胡话、" +
            "又保留一点灵活度；调到 1.0 连很离谱的词也会被选上（更野）；" +
            "低于 0.5 会变得死板，而且容易开始重复（能挑的词太少，来回绕）。",
    },
  };

  async function showSampling() {
    if (document.querySelector(".sample-layer")) return;      // 防重复弹层
    let cfg = {};
    try { cfg = ((await api("/api/config")) || {}).config || {}; } catch (e) { /* 读不到就用默认 */ }
    const cur = {};
    Object.keys(SAMPLE_META).forEach((k) => {
      const t = SAMPLE_META[k];
      const v = Number(cfg[k]);
      cur[k] = Number.isFinite(v) ? Math.min(t.max, Math.max(t.min, v)) : t.best;
    });

    const rowHtml = Object.keys(SAMPLE_META).map((k) => {
      const t = SAMPLE_META[k];
      // 建议刻度**按真实比例定位**（不是简单居中）——
      // 温度建议 0.6 落在 30% 处、top-p 建议 0.95 落在 94% 处，
      // 摆到真实位置上，"我现在偏左还是偏右"才一眼可见。
      const pct = ((t.best - t.min) / (t.max - t.min) * 100).toFixed(1);
      return '<div class="sample-row" data-key="' + k + '">' +
        '<div class="sample-head">' +
        '<span class="sample-name">' + t.name + '</span>' +
        '<span class="sample-right">' +
        '<b class="sample-val" data-key="' + k + '">' + cur[k].toFixed(2) + '</b>' +
        '<button type="button" class="sample-best" data-key="' + k + '" ' +
        'title="点一下跳回建议值">建议 ' + t.best + '</button>' +
        '</span></div>' +
        '<input class="sample-range" type="range" data-key="' + k + '" ' +
        'min="' + t.min + '" max="' + t.max + '" step="' + t.step + '" value="' + cur[k] + '" />' +
        '<div class="sample-scale"><span>' + t.less + ' ' + t.min + '</span>' +
        '<span class="sample-tick" style="left:' + pct + '%">▲ 建议 ' + t.best + '</span>' +
        '<span>' + t.max + ' ' + t.more + '</span></div>' +
        '<div class="sample-hint">' + t.hint + '</div>' +
        '</div>';
    }).join("");

    const layer = document.createElement("div");
    layer.className = "confirm-layer sample-layer";
    layer.innerHTML =
      '<div class="confirm-box sample-box">' +
      '<div class="confirm-title">🎛 采样参数</div>' +
      '<div class="sample-sub">拖动滑条 → <b>立刻生效，不用重启</b>（下一次提问就用新值）。' +
      '旁边标着<b>建议值</b>，点它可以直接跳回去。</div>' +
      rowHtml +
      '<div class="sample-msg"></div>' +
      '<div class="confirm-btns">' +
      '<button class="btn ghost sample-reset" type="button">恢复建议值</button>' +
      '<button class="btn primary sample-close" type="button">完成</button>' +
      '</div></div>';
    document.body.appendChild(layer);

    const msgEl = layer.querySelector(".sample-msg");
    const valEl = (k) => layer.querySelector('.sample-val[data-key="' + k + '"]');
    const rangeEl = (k) => layer.querySelector('.sample-range[data-key="' + k + '"]');

    // 数值离建议值远的时候标黄 —— 让"我是不是调歪了"一眼可见
    function paint(k) {
      const t = SAMPLE_META[k];
      const v = Number(rangeEl(k).value);
      valEl(k).textContent = v.toFixed(2);
      const far = Math.abs(v - t.best) > (t.max - t.min) * 0.18;
      valEl(k).classList.toggle("off", far);
    }

    // 拖动期间不刷请求：等停手 350ms 再存一次（否则一拖就是几十个 POST）
    const pending = {};
    let timer = null;
    async function flush() {
      const body = {};
      Object.keys(pending).forEach((k) => { body[k] = pending[k]; delete pending[k]; });
      if (!Object.keys(body).length) return;
      try {
        await api("/api/config", { method: "POST", body: JSON.stringify(body) });
        msgEl.textContent = "✅ 已保存：" + Object.entries(body)
          .map(([k, v]) => (SAMPLE_META[k] ? SAMPLE_META[k].name.split(" ")[0] : k) +
               " " + Number(v).toFixed(2)).join(" · ") + " —— 立即生效";
        msgEl.className = "sample-msg ok";
      } catch (e) {
        msgEl.textContent = "⚠ 保存失败：" + String(e.message || e);
        msgEl.className = "sample-msg err";
      }
    }
    function queue(k, v) {
      pending[k] = v;
      if (timer) clearTimeout(timer);
      timer = setTimeout(flush, 350);
    }
    function setVal(k, v, save) {
      rangeEl(k).value = v;
      paint(k);
      if (save) queue(k, v);
    }

    Object.keys(SAMPLE_META).forEach((k) => {
      paint(k);
      rangeEl(k).addEventListener("input", () => {
        paint(k);
        queue(k, Number(rangeEl(k).value));
      });
      layer.querySelector('.sample-best[data-key="' + k + '"]')
        .addEventListener("click", () => setVal(k, SAMPLE_META[k].best, true));
    });
    layer.querySelector(".sample-reset").addEventListener("click", () => {
      Object.keys(SAMPLE_META).forEach((k) => setVal(k, SAMPLE_META[k].best, false));
      queue("temperature", SAMPLE_META.temperature.best);
      pending.top_p = SAMPLE_META.top_p.best;
      if (timer) clearTimeout(timer);
      flush();
    });

    function close() { layer.remove(); }
    layer.querySelector(".sample-close").addEventListener("click", async () => {
      if (timer) clearTimeout(timer);
      await flush();                     // 关之前把没存完的补上，别丢改动
      close();
    });
    layer.addEventListener("click", (e) => { if (e.target === layer) close(); });
  }
  window.__showSampling = showSampling;

  window.__showAmapKey = showAmapKey;
  window.__makeMapCard = makeMapCard;      // 导出一下，方便排查"地图没画出来"

  // ---------- 站内下载：桌面壳里改走原生「另存为」 ----------
  // ⚠️ 这是用户实际报过的坑：桌面窗口是 WebView2，而 pywebview 的
  //    settings['ALLOW_DOWNLOADS'] **默认是 False** —— 后端在 DownloadStarting
  //    里直接 `args.Cancel = True`，把页面发起的所有下载**静默取消**。
  //    表现就是：点「点击下载」毫无反应，不报错、不提示，用户完全不知道成没成。
  //    （和「WebView2 不支持 window.prompt、静默返回 null」是同一类坑。）
  // 现在的做法：有 pywebview 桥就用它 —— 弹系统「另存为」、存到用户选的位置、
  // 再把结果如实说出来。浏览器里没有这个桥，照旧走浏览器的下载。
  function bridgeApi() {
    return (window.pywebview && window.pywebview.api) || null;
  }
  let savingNow = false;
  async function saveViaDesktop(url) {
    const api = bridgeApi();
    if (!api || typeof api.save_file !== "function") return false;
    // ⚠️ 防重入：手快点两下会**并发**发出多个保存请求，第 2 个抢不到系统对话框
    // → 立刻返回空 → 界面连着弹「已取消保存」，看起来就像"点了没用"（实测踩到：
    // 日志里 1 秒内三条「另存为：用户取消了」）。
    if (savingNow) {
      showToast("上一次保存还没结束，稍等一下再点", "warn");
      return true;
    }
    savingNow = true;
    try {
      const r = await api.save_file(url, "");
      if (r && r.ok) showToast("已保存：" + r.path, "ok");
      else if (r && r.cancelled) showToast("已取消保存（在「另存为」里选了取消）", "warn");
      else showToast("保存失败：" + ((r && r.error) || "未知原因"), "warn");
    } catch (e) {
      showToast("保存失败：" + String(e.message || e), "warn");
    } finally {
      savingNow = false;
    }
    return true;                       // true = 已经接管，别再走默认行为
  }
  // 点开一个路径 / 网址：交给后端用系统程序打开（浏览器里也能用）
  async function openTarget(target) {
    const s = String(target || "").trim();
    if (!s) return;
    try {
      const r = await fetch(fixApiUrl("/api/open_path"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: s }),
      });
      const j = await r.json().catch(() => ({}));
      if (j && j.ok) {
        if (j.opened === false && j.detail) showToast(j.detail, "warn");
        return;
      }
      showToast((j && j.detail) || ("打不开：" + s), "warn");
    } catch (err) {
      if (/^https?:\/\//i.test(s)) window.open(s, "_blank");
      else showToast("打开失败：" + String(err.message || err), "warn");
    }
  }
  window.__openTarget = openTarget;


  // 事件委托：回答里的下载/定位按钮是 innerHTML 塞进去的，逐个绑定不现实
  document.addEventListener("click", (e) => {
    const t = e.target;
    if (!t || !t.closest) return;
    // ①「在文件库中打开」：跳到生成文库并选中该文件
    const op = t.closest(".dl-open");
    if (op) {
      e.preventDefault();
      openInLibrary(op.getAttribute("data-rel") || "");
      return;
    }
    // ③ 路径 / 网址：点一下真的打开（后端调系统文件管理器 / 默认浏览器）
    const pl = t.closest(".path-link, a.ext-link");
    if (pl) {
      e.preventDefault();
      openTarget(pl.getAttribute("data-path") || pl.getAttribute("href") || "");
      return;
    }

    // ② 下载按钮：桌面壳里走原生「另存为」（否则会被 WebView2 静默吞掉）
    const a = t.closest("a.dl-link");
    if (!a) return;
    const url = fixApiUrl(a.getAttribute("href") || a.href || "");
    if (url.indexOf("/api/") !== 0) return;
    const api = bridgeApi();
    if (!api || typeof api.save_file !== "function") return;   // 浏览器：默认下载
    e.preventDefault();
    saveViaDesktop(url);
  });
  window.__saveViaDesktop = saveViaDesktop;       // 文库面板等处复用

  // 统一渲染入口（2026-11-09）：实时回答与历史回放都走这里。
  // 纯文本模式下只用 textContent（不可能少字）；格式化模式下渲染完做尾部核对，
  // 屏幕上找不到原文结尾就退回纯文本。
  function isPlainMode() {
    // 默认开着 markdown（收尾渲染一次），?plain=1 可切纯文本。
    // 安全网：渲染后逐字核对结尾，屏幕上找不到原文结尾就自动退回纯文本。
    try {
      if (window.__PLAIN_FORCE) return true;
      // 换新键：旧的 plain_answer 曾被"默认纯文本"写成 1，会挡住 markdown 恢复
      const v = localStorage.getItem("render_style");
      if (v) return v === "plain";
      // 旧的 plain_answer 曾被"默认纯文本"写成 1，不能再用它判断 —— 直接清掉
      try { localStorage.removeItem("plain_answer"); } catch (e) {}
      return false;                       // 默认：markdown
    } catch (e) { return false; }
  }

  function renderFinal(bubble, text, userText, silent) {
    const raw = String(text == null ? "" : text);
    if (isPlainMode()) {
      bubble.textContent = raw;
      bubble.style.whiteSpace = "pre-wrap";
      attachSpeak(bubble, raw);
      return;
    }
    try {
      if (raw.indexOf("```") >= 0) renderAnswerWithCode(bubble, raw, userText || "");
      else renderAnswerLinks(bubble, raw);
    } catch (e) {
      bubble.textContent = raw;
      attachSpeak(bubble, raw);
      return;
    }
    try {
      const wantTail = raw.replace(/\*\*|`|^\s*[-*]\s+/gm, "").trim().slice(-24)
                          .replace(/\s+/g, "");
      const shown = (bubble.innerText || "").replace(/\s+/g, "");
      if (wantTail && shown && !shown.includes(wantTail)) {
        console.warn("[stream] 渲染丢了结尾，退回纯文本：", JSON.stringify(wantTail));
        bubble.textContent = raw;
        bubble.style.whiteSpace = "pre-wrap";
      }
    } catch (e) { /* 忽略 */ }
  }

  // ---------- 朗读（TTS）----------
  // 每条助手回答末尾一个 🔊：点了合成并播放。
  //   · 音色 / 语速存 localStorage；右键 🔊 弹小菜单换（含离线兜底那个）
  //   · 后端 /api/tts/synth 对长文要几十秒 → 按钮先 ⏳ 再 ⏹，有反馈不装死
  //   · 按钮必须挂在 bubble.innerHTML 渲染**之后**，否则会被冲掉（见 renderFinal）
  let _ttsAudio = null;
  const TTS_VOICES = [
    ["zh-CN-XiaoxiaoNeural", "晓晓 · 女 · 通用"],
    ["zh-CN-YunxiNeural", "云希 · 男 · 年轻"],
    ["zh-CN-YunjianNeural", "云健 · 男 · 解说"],
    ["zh-CN-YunyangNeural", "云扬 · 男 · 播音"],
    ["sapi:Microsoft Huihui Desktop", "慧慧 · 离线兜底"],
  ];
  function ttsVoice() {
    try { return localStorage.getItem("tts_voice") || TTS_VOICES[0][0]; }
    catch (e) { return TTS_VOICES[0][0]; }
  }
  function ttsRate() {
    try { return parseInt(localStorage.getItem("tts_rate") || "0", 10) || 0; }
    catch (e) { return 0; }
  }
  // 情感预设：后端用「语速+音高+音量」组合近似，不是真正的情感合成
  const TTS_STYLES = [
    ["neutral", "自然"], ["cheerful", "愉悦"], ["gentle", "温柔"],
    ["serious", "严肃"], ["sad", "低沉"], ["news", "播报"], ["excited", "激动"],
  ];
  function ttsStyle() {
    try { return localStorage.getItem("tts_style") || "neutral"; }
    catch (e) { return "neutral"; }
  }
  function ttsPitch() {
    try { return parseInt(localStorage.getItem("tts_pitch") || "0", 10) || 0; }
    catch (e) { return 0; }
  }
  function attachSpeak(bubble, text) {
    const raw = String(text == null ? "" : text).trim();
    if (!bubble || raw.length < 2) return;
    const old = bubble.querySelector(".tts-btn");
    if (old) old.remove();
    const b = document.createElement("button");
    b.type = "button";
    b.className = "tts-btn";
    b.textContent = "🔊";
    b.title = "朗读这段（右键换音色 / 语速）";
    b.onclick = (e) => { e.stopPropagation(); ttsPlay(raw, b); };
    b.oncontextmenu = (e) => { e.preventDefault(); e.stopPropagation(); pickVoice(b); };
    bubble.appendChild(b);
  }
  async function ttsPlay(text, btn) {
    if (_ttsAudio) { try { _ttsAudio.pause(); } catch (e) { /* ignore */ } _ttsAudio = null; }
    const orig = btn.onclick;
    btn.textContent = "⏳";
    btn.disabled = true;
    try {
      const r = await api("/api/tts/synth", {
        method: "POST",
        body: JSON.stringify({ text, voice: ttsVoice(), rate: ttsRate(),
                               pitch: ttsPitch(), style: ttsStyle() }),
      });
      if (!r || !r.ok) throw new Error((r && r.error) || "合成失败");
      const a = new Audio(r.url);
      _ttsAudio = a;
      btn.disabled = false;
      btn.textContent = "⏹";
      btn.onclick = (e) => {
        e.stopPropagation();
        try { a.pause(); } catch (err) { /* ignore */ }
        btn.textContent = "🔊";
        btn.onclick = orig;
      };
      a.onended = () => { btn.textContent = "🔊"; btn.onclick = orig; };
      a.onerror = () => {
        btn.textContent = "🔊"; btn.onclick = orig;
        showToast("播放失败", "warn");
      };
      await a.play();
      if (r.warnings && r.warnings.length) showToast(r.warnings.join("；"), "warn");
    } catch (e) {
      btn.textContent = "🔊";
      btn.disabled = false;
      showToast("朗读失败：" + String((e && e.message) || e), "warn");
    }
  }
  function pickVoice(anchor) {
    document.querySelectorAll(".tts-menu").forEach((n) => n.remove());
    const menu = document.createElement("div");
    menu.className = "tts-menu";
    const cur = ttsVoice();
    TTS_VOICES.forEach((pair) => {
      const id = pair[0], label = pair[1];
      const it = document.createElement("button");
      it.type = "button";
      it.className = "tts-menu-item" + (id === cur ? " on" : "");
      it.textContent = label;
      it.onclick = () => {
        try { localStorage.setItem("tts_voice", id); } catch (e) { /* ignore */ }
        menu.remove();
        showToast("朗读音色：" + label, "ok");
      };
      menu.appendChild(it);
    });
    const rateRow = document.createElement("div");
    rateRow.className = "tts-menu-rate";
    const tag = document.createElement("span");
    tag.textContent = "语速";
    rateRow.appendChild(tag);
    [-15, 0, 15].forEach((v) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "tts-menu-item" + (v === ttsRate() ? " on" : "");
      b.textContent = v > 0 ? "+" + v + "%" : (v === 0 ? "原速" : v + "%");
      b.onclick = () => {
        try { localStorage.setItem("tts_rate", String(v)); } catch (e) { /* ignore */ }
        rateRow.querySelectorAll(".tts-menu-item").forEach((n) => n.classList.remove("on"));
        b.classList.add("on");
      };
      rateRow.appendChild(b);
    });
    menu.appendChild(rateRow);

    // 情感（用 rate/pitch/volume 组合近似，不是真情感合成）
    const stRow = document.createElement("div");
    stRow.className = "tts-menu-rate";
    const stTag = document.createElement("span");
    stTag.textContent = "情感";
    stRow.appendChild(stTag);
    TTS_STYLES.forEach((pair) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "tts-menu-item" + (pair[0] === ttsStyle() ? " on" : "");
      b.textContent = pair[1];
      b.onclick = () => {
        try { localStorage.setItem("tts_style", pair[0]); } catch (e) { /* ignore */ }
        stRow.querySelectorAll(".tts-menu-item").forEach((n) => n.classList.remove("on"));
        b.classList.add("on");
      };
      stRow.appendChild(b);
    });
    menu.appendChild(stRow);

    // 音高
    const piRow = document.createElement("div");
    piRow.className = "tts-menu-rate";
    const piTag = document.createElement("span");
    piTag.textContent = "音高";
    piRow.appendChild(piTag);
    [-12, 0, 12].forEach((v) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "tts-menu-item" + (v === ttsPitch() ? " on" : "");
      b.textContent = v > 0 ? "+" + v + "Hz" : (v === 0 ? "原调" : v + "Hz");
      b.onclick = () => {
        try { localStorage.setItem("tts_pitch", String(v)); } catch (e) { /* ignore */ }
        piRow.querySelectorAll(".tts-menu-item").forEach((n) => n.classList.remove("on"));
        b.classList.add("on");
      };
      piRow.appendChild(b);
    });
    menu.appendChild(piRow);

    document.body.appendChild(menu);
    const r = anchor.getBoundingClientRect();
    menu.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 220)) + "px";
    menu.style.top = Math.min(r.bottom + 6, window.innerHeight - 330) + "px";
    setTimeout(() => {
      const off = (ev) => {
        if (!menu.contains(ev.target)) {
          menu.remove();
          document.removeEventListener("mousedown", off);
        }
      };
      document.addEventListener("mousedown", off);
    }, 0);
  }

  function addMsg(role, text) {
    const el = document.createElement("div");
    el.className = "msg " + role;
    el.innerHTML = `<div class="bubble"></div>`;
    // 助手的历史回答也走一遍链接渲染 —— 否则程序重启后恢复出来的对话里，
    // 之前给过的下载链接又变回不能点的纯文本了（实测踩到）。
    if (role === "assistant") renderFinal(el.querySelector(".bubble"), text, "", true);
    else el.querySelector(".bubble").textContent = text;
    messagesEl.appendChild(el);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return el;
  }
  async function api(path, opts = {}) {
    const r = await fetch(path, Object.assign({
      headers: { "Content-Type": "application/json" }
    }, opts));
    const t = await r.text();
    let data;
    try { data = JSON.parse(t); } catch { data = { raw: t }; }
    // ⚠️ 必须检查状态码。不检查的话，后端返回 400 / 500 时会把
    // `{detail: "已存在同名文件：xxx"}` 当成正常结果交给调用方，
    // 界面就会出现「已复制为：undefined」这种**把失败当成功**的提示 ——
    // 用户以为成了，其实什么都没做（实测踩到，这是最坑的一类 bug）。
    if (!r.ok) {
      const m = (data && (data.detail || data.error || data.message)) || ("HTTP " + r.status);
      const err = new Error(typeof m === "string" ? m : JSON.stringify(m));
      err.status = r.status;
      throw err;
    }
    return data;
  }

  // ---------- 状态检测 ----------
  // 「模型版本 + 参数大小」**一律从后端现读**（后端再去问 Ollama 的 /api/tags），
  // 前端一个模型名都不写死 —— 用户 2026-09-26 明确要求：
  // 界面显示的模型版本与参数大小要跟着最新的走（换模型后自动变，不用改前端）。
  function fmtModel(info) {
    if (!info) return "";
    // 名 · 参数量 · 量化档 · 体积，缺哪个就跳哪个（别显示成 "· · ·"）
    return [info.name, info.parameter_size, info.quantization_level, info.size_text]
      .filter((x) => x && String(x).trim()).join(" · ");
  }
  // 家族名 → 好看的大写写法（deepseek → DeepSeek，而不是 Deepseek）
  const MODEL_BRANDS = {
    qwen: "Qwen", deepseek: "DeepSeek", llama: "Llama", gemma: "Gemma",
    glm: "GLM", mistral: "Mistral", phi: "Phi",
  };

  async function refreshHealth() {
    try {
      // ★ 2026-11-08：状态卡片**跟随前端当前选用的模型**，不再固定显示 config 的默认值。
      //   用户要求：「选用什么模型就显示什么模型的状态，检验也是一样」。
      const selEl = document.querySelector("#modelSelect");
      const prov = selEl ? selEl.value : "auto";
      const isDS = prov.indexOf("deepseek") === 0;
      const dot = $("#statusDot"), t = $("#statusTitle"), det = $("#statusDetail");
      const mi = $("#modelInfo");
      const bs = $("#brandSub");

      if (isDS) {
        // ===== DeepSeek 云端模式：不查本地 Ollama（没意义）=====
        const saved = localStorage.getItem("ds_api_key") || "";
        let label = "DeepSeek";
        try { label = selEl.options[selEl.selectedIndex].text.replace(/^\S+\s*/, ""); } catch {}
        dot.className = "dot" + (saved ? " on" : " off");
        t.textContent = saved ? "DeepSeek 云端模式" : "DeepSeek 未配置 Key";
        det.textContent = saved ? "不占显存 · 走云端 API" : "点右上角 🔑 填入 API Key";
        if (mi) {
          mi.textContent = "当前 " + label + (saved
            ? "　|　工具调度：function calling（41 个工具）"
            : "（缺 Key，无法调用）");
          mi.title = mi.textContent;
        }
        if (bs) bs.textContent = "DeepSeek 智能体 · 云端推理";
        return;
      }

      // ===== 本地模式（原有逻辑）=====
      const d = await api("/api/health");

      // 本地模式可能指定了具体模型（modelSelect 的值就是模型名）
      const wantModel = prov === "auto" ? "" : prov;
      const inst = Array.isArray(d.installed) ? d.installed : [];
      const has = (n) => !!n && (inst.indexOf(n) >= 0 || inst.indexOf(n + ":latest") >= 0);
      // ⚠️ 不能用 d.model === wantModel 判断：d.model 是 config 的默认模型，
      //    下拉选了 8B 时两者不等，会被误判成"模型未下载"。要查**已安装清单**。
      const ready = wantModel ? (d.online && has(wantModel)) : !!d.model_ready;
      const shown = wantModel || d.model || "默认模型";

      if (d.online) {
        dot.className = "dot" + (ready ? " on" : " off");
        t.textContent = ready ? "模型就绪（离线）" : "Ollama 在线，模型未下载";
        det.textContent = ready ? shown + " · 智能体已就绪" : "缺少 " + shown + "，请下载";
      } else {
        dot.className = "dot off";
        t.textContent = "Ollama 未运行";
        det.textContent = "请先启动 ollama serve";
      }

      if (mi) {
        // ⚠️ 区分两种"没有信息"：**连不上** vs **真的没下载**。
        const online = d.online !== false;
        const bits = [];
        if (wantModel) {
          // 用户在下拉里明确选了模型 —— 状态只讲这一个
          bits.push("已选 " + wantModel + (ready ? "" : (online ? "（未下载）" : "（Ollama 未运行）")));
        } else {
          const main = fmtModel(d.model_info);
          if (main) bits.push("对话 " + main);
          else if (d.model) bits.push("对话 " + d.model + (online ? "（未下载）" : ""));
        }
        if (d.code_model) {
          if (d.code_model_ready) bits.push("代码 " + fmtModel(d.code_model_info));
          else bits.push("代码 " + d.code_model + (online ? "（未下载，暂用默认模型）" : ""));
        }
        mi.textContent = bits.join("　|　");
        mi.title = bits.join("\n");
      }

      if (bs) {
        const nm = String(wantModel || d.model || "").toLowerCase();
        const fam = String((wantModel ? "" : (d.model_info && d.model_info.family)) || "").toLowerCase();
        let brand = "";
        for (const k of Object.keys(MODEL_BRANDS)) {
          if (nm.includes(k) || fam.includes(k)) { brand = MODEL_BRANDS[k]; break; }
        }
        bs.textContent = (brand ? brand + " " : "本地") + "智能体 · 数据不出机";
      }
    } catch { /* 后端未就绪时忽略 */ }
  }

  // ---------- 顶部正上方的能力开关（极简，仅开关）----------
  async function loadToggles() {
    try {
      const d = await api("/api/config");
      const c = d.config;
      document.querySelectorAll(".pill[data-cfg]").forEach((p) => {
        const key = p.dataset.cfg;
        const on = key === "memory_enabled" ? c.memory_enabled !== false : !!c[key];
        p.classList.toggle("on", on);
      });
      // 询问模式：二选一，不是开关（见 index.html 里为什么不加 data-cfg）
      setAskMode(c.ask_mode === "deep" ? "deep" : "quick");
      // 若联网模式已开启，顺带校验当前网络（断网时给出提示）
      if (c.web_enabled) {
        const net = await checkNetwork();
        if (!net.online) showToast("电脑未连接网络，联网搜索将不可用", "warn");
      }
    } catch {}
  }
  // ---------- 询问方式：快速了解 / 深度询问 ----------
  // 用户 2026-09-22：
  //   · 两种模式的区别只在"问到什么程度就动手"，**两种模式都不限制问题个数**
  //     （后端 ask_mode 注入系统提示，见 main.py 的 _ask_mode_line）；
  //   · 前端原来并排两个按钮，要求**合并成一个「询问方式」按钮**，
  //     并把当前选择直接显示在按钮上。
  const ASK_MODES = {
    quick: {
      name: "快速",
      full: "快速了解",
      desc: "只问最关键的 1~3 个问题，拿到基本信息就开工；"
          + "剩下的按最合理的默认做，并在结果里说明替你假设了什么。",
    },
    deep: {
      name: "深度",
      full: "深度询问",
      desc: "问题个数不限、可以分多轮，把关键信息问透再动手。"
          + "想做的东西越复杂，越建议用这个。",
    },
  };
  let askMode = "quick";                       // 当前选择（唯一事实来源）

  function askModeLabel() {
    return (askMode === "deep" ? "深度问" : "快速问");
  }
  function setAskMode(mode) {
    askMode = ASK_MODES[mode] ? mode : "quick";
    const b = document.getElementById("askModeBtn");
    if (b) {
      b.textContent = askModeLabel();
      // 深度模式给个视觉标记（开着 accent 色），一眼能看出不是默认档
      b.classList.toggle("on", askMode === "deep");
    }
  }
  async function saveAskMode(mode) {
    setAskMode(mode);
    try {
      await api("/api/config", { method: "POST", body: JSON.stringify({ ask_mode: askMode }) });
      showToast(askMode === "deep"
        ? "已切到「深度询问」：模型会把关键信息问透再动手"
        : "已切到「快速了解」：模型只问最关键的几条", "ok");
    } catch (e) {
      showToast("切换失败：" + String(e.message || e), "warn");
    }
  }
  // 点按钮 → 弹一个小面板二选一（不新开页面、不占顶栏宽度）
  function showAskModePicker() {
    if (document.querySelector(".askmode-layer")) return;      // 防重复弹层
    const layer = document.createElement("div");
    layer.className = "confirm-layer askmode-layer";
    const opts = Object.keys(ASK_MODES).map((k) => {
      const m = ASK_MODES[k];
      const on = k === askMode;
      return '<button type="button" class="askmode-opt' + (on ? " on" : "") +
        '" data-mode="' + k + '">' +
        '<span class="askmode-mark">' + (on ? "●" : "○") + "</span>" +
        '<span class="askmode-text"><b>' + m.full + "</b><em>" + m.desc + "</em></span>" +
        "</button>";
    }).join("");
    layer.innerHTML =
      '<div class="confirm-box askmode-box">' +
      '<div class="confirm-title">💬 询问方式</div>' +
      '<div class="sample-sub">决定模型在<b>动手之前</b>会问你多少问题。' +
      '随时可以换，<b>下一次提问就生效</b>。</div>' +
      opts +
      '<div class="confirm-btns">' +
      '<button class="btn primary askmode-close" type="button">完成</button>' +
      "</div></div>";
    document.body.appendChild(layer);

    const close = () => layer.remove();
    layer.querySelector(".askmode-close").onclick = close;
    layer.addEventListener("click", (e) => {
      if (e.target === layer) close();          // 点遮罩关闭
    });
    layer.querySelectorAll(".askmode-opt").forEach((btn) => {
      btn.onclick = async () => {
        await saveAskMode(btn.dataset.mode);
        close();
      };
    });
  }
  document.getElementById("askModeBtn") &&
    (document.getElementById("askModeBtn").onclick = showAskModePicker);
  window.__showAskMode = showAskModePicker;
  async function saveToggles() {
    const body = {
      memory_enabled: $('.pill[data-cfg="memory_enabled"]').classList.contains("on"),
      rag_enabled: $('.pill[data-cfg="rag_enabled"]').classList.contains("on"),
      web_enabled: $('.pill[data-cfg="web_enabled"]').classList.contains("on"),
      auto_memorize: $('.pill[data-cfg="auto_memorize"]').classList.contains("on"),
      code_auto_route: $('.pill[data-cfg="code_auto_route"]').classList.contains("on"),
      code_exec_enabled: $('.pill[data-cfg="code_exec_enabled"]').classList.contains("on"),
      // ⚠️ 带上询问方式：否则用户改完模式、再点任意开关时，
      //    保存的 body 里没有 ask_mode —— 后端只 merge 传了的键，本身不会丢，
      //    但两处状态容易不同步（这里显式带上，前端为准）。
      //    合并成一个按钮后，模式的唯一事实来源是 `askMode` 变量（不再从 DOM 反推）。
      ask_mode: askMode,
    };
    await api("/api/config", { method: "POST", body: JSON.stringify(body) });
  }
  // 检测本机是否已连接互联网（开启「联网」前的前置校验）
  async function checkNetwork() {
    try {
      const r = await fetch("/api/net/check");
      return await r.json();
    } catch (e) {
      return { online: false, error: String(e) };
    }
  }

  // ⚠️ 只给**真正的开关**（带 data-cfg 的）绑开关逻辑。
  // 顶栏的「高德 key」按钮借用了 .pill 的外观，如果这里选 ".pill" 会把它一起抓进来，
  // `p.onclick = ...` 会**覆盖掉它自己的点击处理**，表现就是"按钮点了完全没反应"。
  // 加 [data-cfg] 限定就好。顶栏以后再加按钮，记得同样别让它被这条规则吃掉。
  document.querySelectorAll(".pill[data-cfg]").forEach((p) => {
    p.onclick = async () => {
      const key = p.dataset.cfg;
      const turningOn = !p.classList.contains("on");

      // 开启「联网」前先校验电脑是否联网：未联网则提示，且开关保持原状不变亮
      if (key === "web_enabled" && turningOn) {
        p.classList.add("checking");
        const net = await checkNetwork();
        p.classList.remove("checking");
        if (!net.online) {
          showToast("电脑未连接网络", "warn");
          return;
        }
      }

      p.classList.toggle("on");
      await saveToggles();
      if (key === "web_enabled") {
        const on = p.classList.contains("on");
        showToast(on ? "已开启联网模式" : "已关闭联网模式", on ? "ok" : "");
        // ⚠️ 联网开关一变，高德**还能不能用**这件事就立刻变了 ——
        //    必须**马上**同步地图按钮，等 45 秒轮询是不行的：
        //    实测用户关了联网后按钮还亮着，非要点它一下才变暗，
        //    看着就像"这个按钮坏了"。
        //    关掉时不必重新验证（离线必然用不了）；打开时要 force 一次，
        //    真确认 key 还好使，没问题才重新亮起。
        //    （app.js 整个包在一个 IIFE 里，直接调即可。）
        await refreshAmapState(on);
      }
      // 开启"本地算代码"是**有风险的操作**，必须明确说清而不是默默打开
      if (key === "code_exec_enabled" && p.classList.contains("on")) {
        showToast("已开启：模型写的 Python 会在你电脑上真实执行（临时目录 + 时限，会拦截删除/联网类操作）", "warn");
      }
      // 开了代码模型但本机没装时，别让用户以为坏了
      if (key === "code_auto_route" && p.classList.contains("on")) {
        try {
          const r = await fetch("/api/models");
          const d = await r.json();
          const names = (d.models || []).map((m) => m.name || "");
          const cfg = (await (await fetch("/api/config")).json()).config || {};
          const want = cfg.code_model || "";
          if (want && !names.some((n) => n === want || n.split(":")[0] === want.split(":")[0])) {
            showToast(`本机还没下载 ${want}，现在仍用默认模型；下载后会自动生效`, "warn");
          }
        } catch (e) {}
      }
    };
  });

  // 模型**全自动选择** —— 一律走后端的按轮路由，前端不做手动指定。
  // （曾经加过手动下拉，用户反馈"手动太麻烦了"，已移除。
  //   后端的 ChatRequest.model 覆盖能力保留着，随时能再挂上来。）

  // ---------- 主题切换（2026-11-08） ----------
  // 用户要求：「深色太难看，改成深色 + 白色两种模式可以来回自主切换」。
  // 颜色全在 style.css 的 :root / html[data-theme="light"] 里，这里只负责切属性 + 记住选择。
  // 首屏那份内联脚本（index.html <head>）用的是同一个 key，所以刷新不会闪。
  const THEME_KEY = "ui_theme";
  function applyTheme(theme) {
    const t = theme === "light" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", t);
    const btn = $("#themeBtn");
    if (btn) {
      // 图标表示「点一下会变成什么」：深色时显示 ☀️，浅色时显示 🌙
      btn.textContent = t === "light" ? "🌙" : "☀️";
      btn.title = t === "light" ? "当前：浅色 —— 点击切到深色" : "当前：深色 —— 点击切到浅色";
    }
    try { localStorage.setItem(THEME_KEY, t); } catch (e) {}
  }
  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
  }
  // 初始化：沿用 <head> 里已经定好的值（它已经处理过 localStorage 与系统偏好）
  applyTheme(currentTheme());
  const themeBtnEl = $("#themeBtn");
  if (themeBtnEl) {
    themeBtnEl.onclick = () => {
      applyTheme(currentTheme() === "light" ? "dark" : "light");
    };
  }
  // 用户没手动选过时，跟随系统主题变化
  try {
    const mq = window.matchMedia("(prefers-color-scheme: light)");
    const onSysChange = (e) => {
      let saved = null;
      try { saved = localStorage.getItem(THEME_KEY); } catch (err) {}
      if (!saved) applyTheme(e.matches ? "light" : "dark");
    };
    if (mq.addEventListener) mq.addEventListener("change", onSysChange);
    else if (mq.addListener) mq.addListener(onSysChange);
  } catch (e) {}

  // ---------- 事件 ----------
  // 手机端侧栏（抽屉）开合：抽屉会盖住顶栏的 ☰，所以必须另有入口能关掉（2026-11-09）
  const _sbEl = () => document.querySelector("#sidebar");
  const _maskEl = () => document.querySelector("#sbMask");
  const openSidebar = () => {
    const el = _sbEl(); if (!el) return;
    el.classList.remove("hidden");
    const m = _maskEl(); if (m) m.hidden = false;
  };
  const closeSidebar = () => {
    const el = _sbEl(); if (!el) return;
    el.classList.add("hidden");
    const m = _maskEl(); if (m) m.hidden = true;
  };
  window.__openSidebar = openSidebar;
  window.__closeSidebar = closeSidebar;

  // 2026-11-13 用户要求：去掉「智能体对话」旁的 ☰ 单钮，改由侧栏 logo 本身
  // 折叠整条侧栏。桌面端收起是「78px 窄条」（logo 还在，随时点回来），
  // 手机端是「滑出屏幕」——两种行为都还是靠 .hidden 表示，差异全在 CSS 里。
  const SB_KEY = "sidebar_collapsed";
  // ⚠️ 桌面端收起时**直接写内联样式**，不完全依赖 style.css 的 .sidebar.hidden：
  //    实测某些内核对那条规则（连老的 width:0 也是）不应用，收起后宽度仍是 300px。
  //    内联样式优先级最高，且 .sidebar 本身有 transition，动画照常。
  const applySbStyle = (collapsed) => {
    const el = _sbEl(); if (!el) return;
    if (window.innerWidth <= 768) {
      // 手机端是抽屉（transform 控制），内联宽度会捣乱，全部清掉
      el.style.width = ""; el.style.minWidth = "";
      el.style.paddingLeft = ""; el.style.paddingRight = "";
      return;
    }
    el.style.width = collapsed ? "78px" : "";
    el.style.minWidth = collapsed ? "78px" : "";
    el.style.paddingLeft = collapsed ? "10px" : "";
    el.style.paddingRight = collapsed ? "10px" : "";
  };
  const setSbCollapsed = (collapsed, persist) => {
    const el = _sbEl(); if (!el) return;
    if (collapsed) closeSidebar(); else openSidebar();
    applySbStyle(collapsed);
    const b = document.querySelector("#logoBtn");
    if (b) b.setAttribute("aria-expanded", collapsed ? "false" : "true");
    if (persist) { try { localStorage.setItem(SB_KEY, collapsed ? "1" : "0"); } catch (e) {} }
  };
  // 窗口跨过 768px 时把内联宽度清掉，避免桌面收起态带到手机抽屉上
  let _sbRzT = null;
  window.addEventListener("resize", () => {
    clearTimeout(_sbRzT);
    _sbRzT = setTimeout(() => {
      const el = _sbEl(); if (!el) return;
      applySbStyle(el.classList.contains("hidden"));
    }, 150);
  });
  const _logoBtn = document.querySelector("#logoBtn");
  if (_logoBtn) {
    _logoBtn.onclick = () => {
      const el = _sbEl(); if (!el) return;
      setSbCollapsed(!el.classList.contains("hidden"), true);
    };
  }
  // 手机端侧栏滑走后 logo 也跟着走了，用主区那个同款 logo 小按钮叫回来
  const _sbFloat = document.querySelector("#sbFloat");
  if (_sbFloat) _sbFloat.onclick = () => setSbCollapsed(false, true);
  // 恢复上次的折叠状态（只在桌面端恢复：手机上默认就该是收起的）
  try {
    if (localStorage.getItem(SB_KEY) === "1" && window.innerWidth > 768) setSbCollapsed(true, false);
  } catch (e) {}

  // ---------- 侧栏面板折叠（2026-11-13）----------
  // 用户要求：知识库 / 音频 / 图片库 / 协作群 这些面板也要能各自收起展开、带动画。
  // 做法：把 .sec-head 之后的兄弟节点整体包进 .pnl-fold > .pnl-fold-in，
  // 高度动画交给 CSS 的 grid-template-rows 1fr ↔ 0fr（不用预先量高度）。
  // ⚠️ 生成文库 / 对话 / 记忆库本来就有自己的折叠逻辑，这里**跳过**，否则两套打架。
  // ---------- 🎭 数字人（2026-11-13） ----------
  // 一张图 + 一段话 → 会说话的视频。口型是**参数化**的（声学特征驱动），
  // 不是神经网络重绘，界面上必须如实写清这一点。
  (function initAvatar2() {
    const pnl = document.getElementById("av2Panel");
    if (!pnl) return;
    const $ = (s) => pnl.querySelector(s);
    const img = $("#av2Img"), empty = $("#av2Empty");
    const fileIn = $("#av2File"), textIn = $("#av2Text");
    const voiceSel = $("#av2Voice"), hint = $("#av2Hint");
    const video = $("#av2Video"), list = $("#av2List");
    const cal = $("#av2Cal"), my = $("#av2MY"), mw = $("#av2MW");
    const pinBtn = $("#av2Pin");
    const PIN_KEY = "avatar2_pin";
    let busy = false, voicesLoaded = false;

    // 右下角常驻：动态建节点，不动已有布局骨架
    const pin = document.createElement("div");
    pin.className = "av2-pin";
    pin.innerHTML = '<div class="av2-pin-head"><span>🎭 数字人</span>' +
      '<button type="button" title="关闭常驻">✕</button></div>' +
      '<img alt="数字人形象" /><video playsinline></video>';
    document.body.appendChild(pin);
    const pinImg = pin.querySelector("img"), pinVid = pin.querySelector("video");

    function say(msg, kind) {
      hint.textContent = msg || "";
      hint.style.color = kind === "err" ? "#e06c75" : "";
    }

    function setPin(on) {
      // 没形象就别开常驻：右下角会挂一张破图
      if (on && img.hidden) {
        say("先上传一张形象图（或点「用最新生成图」），再常驻右下角", "err");
        return;
      }
      pin.classList.toggle("on", !!on);
      pinBtn.classList.toggle("active", !!on);
      pinBtn.title = on ? "取消常驻" : "常驻右下角";
      try { localStorage.setItem(PIN_KEY, on ? "1" : ""); } catch (e) {}
      if (!on) { pin.classList.remove("speaking"); }
    }
    pin.querySelector("button").onclick = () => setPin(false);
    pinBtn.onclick = () => setPin(!pin.classList.contains("on"));

    function paintInfo(info) {
      const has = !!(info && info.has_image);
      img.hidden = !has;
      empty.hidden = has;
      if (has && info.url) {
        // 加时间戳：换了形象要立刻看到新的，别吃缓存
        const u = info.url + (info.url.indexOf("?") >= 0 ? "&" : "?") + "t=" + Date.now();
        img.src = u;
        pinImg.src = u;
      }
      cal.hidden = !has;
      if (info && info.mouth) {
        my.value = info.mouth.y;
        mw.value = info.mouth.w;
      }
    }

    async function loadInfo() {
      try { paintInfo(await api("/api/avatar2/info")); }
      catch (e) { say("读取形象失败：" + e.message, "err"); }
    }

    async function loadStatus() {
      try {
        const st = await api("/api/avatar2/status");
        const bits = [st.ffmpeg ? "ffmpeg ✓" : "ffmpeg ✗",
                      st.asr ? "字级对齐 ✓" : "仅声学驱动"];
        say(bits.join(" · ") + "。口型为声学形变。");
      } catch (e) { /* 状态读不到不影响用 */ }
    }

    async function loadVoices() {
      if (voicesLoaded) return;
      try {
        const r = await api("/api/tts/voices");
        const vs = (r.voices || []).filter((v) => v.available !== false);
        voiceSel.innerHTML = vs.map((v) =>
          '<option value="' + v.id + '">' + (v.name || v.id) + "</option>").join("");
        if (r.default) voiceSel.value = r.default;
        voicesLoaded = true;
      } catch (e) {}
    }

    function esc(s) {
      return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
        { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    }

    // nameOrItem：字符串 = 数字人视频名；对象 = 列表项（带 url，文库视频走下载地址）
    function playVideo(nameOrItem) {
      const it = typeof nameOrItem === "string"
        ? { name: nameOrItem, url: "/api/avatar2/video/" + encodeURIComponent(nameOrItem) }
        : nameOrItem;
      const src = it.url;
      video.hidden = false;
      video.src = src;
      video.play().catch(() => {});
      // 常驻小窗同步播：说话时把小图换成视频
      pinVid.src = src;
      pin.classList.add("speaking");
      pinVid.onended = () => pin.classList.remove("speaking");
      pinVid.onerror = () => pin.classList.remove("speaking");
    }

    // 长文本按句子切段：每段 ≤ max 字。
    // 单次请求渲染越久越容易在前端超时，所以先切成小段分别生成，最后合并。
    // 优先在句号/问号/感叹号/分号/换行处断开，尽量不把一句话劈两半；
    // 单句本身就超长时才硬切。
    function splitSpeech(t, max) {
      const src = String(t || "");
      const sents = src.match(/[^。！？!?；;\n]+[。！？!?；;\n]*/g) || [src];
      const out = [];
      let cur = "";
      for (const s of sents) {
        if (s.length > max) {
          if (cur) { out.push(cur); cur = ""; }
          for (let i = 0; i < s.length; i += max) out.push(s.slice(i, i + max));
        } else if ((cur + s).length <= max) {
          cur += s;
        } else {
          if (cur) out.push(cur);
          cur = s;
        }
      }
      if (cur) out.push(cur);
      return out.filter((x) => String(x).trim());
    }

    const scopeSel = $("#av2Scope");
    const SCOPE_KEY = "avatar2_scope";
    if (scopeSel) {
      const saved = localStorage.getItem(SCOPE_KEY);
      if (saved) scopeSel.value = saved;
      scopeSel.onchange = () => {
        try { localStorage.setItem(SCOPE_KEY, scopeSel.value); } catch (e) {}
        loadVideos();
      };
    }

    async function loadVideos() {
      try {
        const sc = scopeSel ? scopeSel.value : "avatar";
        const r = await api("/api/avatar2/videos?scope=" + encodeURIComponent(sc));
        const items = r.items || [];
        if (!items.length) {
          list.innerHTML = '<div class="hint av2-empty-list">这个目录里还没有视频</div>';
          return;
        }
        list.innerHTML = items.map((it, i) =>
          '<div class="av2-item" data-i="' + i + '" title="' + esc(it.name) + '">' +
          '<span class="av2-name">' +
          esc(String(it.name).replace(/^avatar_\d{8}_\d{6}_/, "")) +
          (it.scope === "library" ? ' <i class="av2-tag">文库</i>' : "") + "</span>" +
          '<span class="av2-meta">' + esc(String(it.mtime).slice(5, 16)) + "</span>" +
          '<button type="button" class="av2-del" title="删除这个视频">✕</button>' +
          "</div>").join("");
        list.querySelectorAll(".av2-item").forEach((el) => {
          const it = items[parseInt(el.dataset.i, 10)];
          el.querySelector(".av2-name").onclick = () => playVideo(it);
          el.querySelector(".av2-meta").onclick = () => playVideo(it);
          const del = el.querySelector(".av2-del");
          del.onclick = async (ev) => {
            ev.stopPropagation();
            const where = it.scope === "library" ? "生成文库" : "数字人视频";
            if (!confirm("删除「" + it.name + "」？\n（在" + where + "里）")) return;
            try {
              const d = await api("/api/avatar2/video/delete", {
                method: "POST",
                body: JSON.stringify({ name: it.rel || it.name, scope: it.scope }),
              });
              if (!d.ok) throw new Error(d.error || "删除失败");
              if (video.getAttribute("src") &&
                  decodeURIComponent(video.getAttribute("src")).indexOf(it.name) >= 0) {
                video.pause(); video.hidden = true; video.removeAttribute("src");
              }
              say("已删除：" + it.name);
              loadVideos();
            } catch (e) { say("删除失败：" + e.message, "err"); }
          };
        });
      } catch (e) {}
    }

    // —— 上传 ——
    $("#av2UploadBtn").onclick = () => fileIn.click();
    fileIn.onchange = async () => {
      const f = fileIn.files && fileIn.files[0];
      if (!f) return;
      fileIn.value = "";
      const fd = new FormData();
      fd.append("file", f);
      say("上传中…");
      try {
        // 上传不能用 api()：它默认塞 application/json，multipart 的 boundary 会丢
        const r = await fetch("/api/avatar2/upload", { method: "POST", body: fd });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || "上传失败");
        paintInfo(d);
        const am = d.auto_mouth || {};
        say(am.ok
          ? "已上传 · 自动定位嘴部（置信度 " + am.score + "）"
          : "已上传，但没检测到人脸：" + (am.error || "未知") + "。请拖滑块校准嘴位。",
          am.ok ? "" : "err");
      } catch (e) { say("上传失败：" + e.message, "err"); }
    };

    // —— 用最近生成的那张图 ——
    $("#av2LatestBtn").onclick = async () => {
      say("正在取最近生成的图…");
      try {
        const r = await api("/api/library/images");
        const gen = (r.images || []).filter((x) => x.origin === "gen");
        gen.sort((a, b) => String(b.created).localeCompare(String(a.created)));
        if (!gen.length) { say("图库里还没有 AI 生成的图。先在对话里让我画一张。", "err"); return; }
        const d = await api("/api/avatar2/from_library", {
          method: "POST", body: JSON.stringify({ id: gen[0].id }) });
        paintInfo(d);
        const am = d.auto_mouth || {};
        say("已设为形象" + (am.ok ? " · 嘴部已自动定位" : " · 请手动校准嘴位"),
            am.ok ? "" : "err");
      } catch (e) { say("设置失败：" + e.message, "err"); }
    };

    // —— 自动定位嘴部 ——
    $("#av2Detect").onclick = async () => {
      say("正在检测人脸…");
      try {
        const r = await api("/api/avatar2/detect_mouth", { method: "POST", body: "{}" });
        if (!r.ok) { say("没检测到人脸：" + (r.error || "未知"), "err"); return; }
        my.value = r.mouth.y;
        mw.value = r.mouth.w;
        say("已重新定位嘴部（置信度 " + r.score + "）");
      } catch (e) { say("检测失败：" + e.message, "err"); }
    };

    // —— 手动校准（拖动时防抖，220ms 才落盘） ——
    let calTimer = null;
    function pushMouth() {
      clearTimeout(calTimer);
      calTimer = setTimeout(async () => {
        try {
          await api("/api/avatar2/mouth", {
            method: "POST",
            body: JSON.stringify({ y: parseFloat(my.value), w: parseFloat(mw.value) }),
          });
        } catch (e) {}
      }, 220);
    }
    my.oninput = pushMouth;
    mw.oninput = pushMouth;

    // —— 心情 & 语速 ——
    const moodSel = $("#av2Mood"), rateSel = $("#av2Rate");
    const MOOD_KEY = "avatar2_mood", RATE_KEY = "avatar2_rate";
    let moodsCache = [], moodsLoaded = false;
    async function loadMoods() {
      if (moodsLoaded || !moodSel) return;
      try {
        const r = await api("/api/avatar2/moods");
        moodsCache = r.items || [];
        const opts = ['<option value="auto">🤖 心情：自动判断</option>'];
        moodsCache.forEach((m) => {
          opts.push('<option value="' + esc(m.key) + '">' + esc(m.emoji + " " + m.label) + "</option>");
        });
        moodSel.innerHTML = opts.join("");
        moodsLoaded = true;
        const saved = localStorage.getItem(MOOD_KEY);
        if (saved && moodSel.querySelector('option[value="' + saved + '"]')) moodSel.value = saved;
        moodSel.onchange = () => {
          try { localStorage.setItem(MOOD_KEY, moodSel.value); } catch (e) {}
          const m = moodsCache.find((x) => x.key === moodSel.value);
          if (m) say("心情：" + m.emoji + " " + m.label + " · " + m.desc);
        };
      } catch (e) {}
    }
    if (rateSel) {
      const saved = localStorage.getItem(RATE_KEY);
      if (saved) rateSel.value = saved;
      rateSel.onchange = () => {
        try { localStorage.setItem(RATE_KEY, rateSel.value); } catch (e) {}
      };
    }

    // —— 口型幅度 ——
    // 增益作用在张口曲线上（不是硬拉伸）：同一段语音实测 1.0→1.6 时
    // 最大开口 31px→56px、明显张开的帧从 0% 升到 40%。
    // 默认 1.2：比原始观感明显一点，又不至于出嘴角张力。
    const ampIn = $("#av2Amp"), ampVal = $("#av2AmpVal");
    const AMP_KEY = "avatar2_amp";
    function ampText(v) { return (Math.round(v * 100) / 100).toFixed(2).replace(/0$/, "") + "×"; }
    function syncAmp() {
      if (!ampIn) return 1.2;
      let v = parseFloat(ampIn.value);
      if (!isFinite(v)) v = 1.2;
      v = Math.min(2, Math.max(0.6, v));
      if (ampVal) ampVal.textContent = ampText(v);
      return v;
    }
    if (ampIn) {
      try {
        const sv = parseFloat(localStorage.getItem(AMP_KEY));
        if (isFinite(sv) && sv >= 0.6 && sv <= 2) ampIn.value = String(sv);
      } catch (e) {}
      syncAmp();
      ampIn.oninput = () => {
        const v = syncAmp();
        try { localStorage.setItem(AMP_KEY, String(v)); } catch (e) {}
      };
      // 松手才提示，拖动时别刷屏
      ampIn.onchange = () => {
        const v = syncAmp();
        say("口型幅度 " + ampText(v) +
            (v <= 0.9 ? "（嘴会比较含蓄）" : v >= 1.6 ? "（更夸张，可能有嘴角张力）" : ""));
      };
    }

    // —— 导入文档朗读：先预览，用户自己删减后再填进朗读框 ——
    const docBtn = $("#av2DocBtn"), docIn = $("#av2Doc");
    const docBox = $("#av2DocBox"), docName = $("#av2DocName");
    const docMeta = $("#av2DocMeta"), docText = $("#av2DocText");
    if (docBtn && docIn) {
      docBtn.onclick = () => docIn.click();
      docIn.onchange = async () => {
        const f = docIn.files && docIn.files[0];
        if (!f) return;
        docIn.value = "";
        say("正在读取「" + f.name + "」…");
        const fd = new FormData();
        fd.append("file", f);
        try {
          // multipart 必须走原生 fetch，api() 会强制 JSON
          const r = await fetch("/api/avatar2/import_doc", { method: "POST", body: fd });
          const d = await r.json();
          if (!r.ok) throw new Error(d.detail || "导入失败");
          if (!d.ok) throw new Error(d.error || "读不出文字");
          docBox.hidden = false;
          docName.textContent = f.name;
          const bits = [d.chars + " 字", String(d.kind || "").toUpperCase()];
          if (d.encoding) bits.push(d.encoding);
          if (d.truncated) bits.push("已截断");
          docMeta.textContent = bits.filter(Boolean).join(" · ") +
            (d.warning ? " ⚠ " + d.warning : "");
          docText.value = d.text;
          say("已导入，可在下面预览、删减后再点「用这段」");
        } catch (e) { say("导入失败：" + e.message, "err"); }
      };
      const useBtn = $("#av2DocUse");
      if (useBtn) useBtn.onclick = () => {
        const t = (docText.value || "").trim();
        if (!t) { say("预览里没有文字", "err"); return; }
        // 不再截断：长文本由生成时自动分段处理（splitSpeech）
        textIn.value = t;
        const nSeg = splitSpeech(t, 120).length;
        say("已填入朗读框（" + t.length + " 字" +
            (nSeg > 1 ? "，将自动分 " + nSeg + " 段生成后合并" : "") + "）");
        docBox.hidden = true;
      };
      const closeBtn = $("#av2DocClose");
      if (closeBtn) closeBtn.onclick = () => { docBox.hidden = true; };
    }

    // —— 生成 ——
    $("#av2Go").onclick = async () => {
      if (busy) return;
      const text = (textIn.value || "").trim();
      if (!text) { say("先写点要让数字人说的话", "err"); return; }
      const btn = $("#av2Go");
      const old = btn.textContent;
      busy = true;
      btn.disabled = true;
      btn.textContent = "生成中…";
      // rate 传 null 表示「用心情的建议语速」，不能传 0（那会把语速钉死）
      const rateVal = rateSel && rateSel.value !== "auto"
        ? parseInt(rateSel.value, 10) : null;
      const base = {
        voice: voiceSel.value,
        mood: moodSel ? moodSel.value : "auto",
        rate: rateVal,
        amp: syncAmp(),
      };
      try {
        // 长文本切成多段分别生成，每段都在请求超时窗口内，最后合并成一个视频。
        // 之前一次性发整篇，字数一多就渲染超时（用户反馈的那个问题）。
        // 120 字约 25 秒语音、渲染约 25 秒；实测 260 字要渲染 55 秒，
        // 太长仍可能被中间层掐断，所以取 120 更稳（段多但每段都快）。
        const parts = splitSpeech(text, 120);
        let r;
        if (parts.length <= 1) {
          say("正在配音 → 口型对齐 → 合成，约 10 秒…");
          r = await api("/api/avatar2/speak", {
            method: "POST",
            body: JSON.stringify(Object.assign({}, base, { text: parts[0] || text })),
          });
          if (!r.ok) throw new Error(r.error || "生成失败");
        } else {
          const names = [];
          let lastOk = null;
          for (let i = 0; i < parts.length; i++) {
            btn.textContent = "生成中 " + (i + 1) + "/" + parts.length;
            say("长文本已切成 " + parts.length + " 段，正在生成第 " + (i + 1) +
                "/" + parts.length + " 段（" + parts[i].length + " 字）…");
            const seg = await api("/api/avatar2/speak", {
              method: "POST",
              body: JSON.stringify(Object.assign({}, base, { text: parts[i] })),
            });
            if (!seg.ok) throw new Error("第 " + (i + 1) + " 段失败：" + (seg.error || "未知错误"));
            names.push(seg.name);
            lastOk = seg;
          }
          btn.textContent = "合并中…";
          say("已生成 " + names.length + " 段，正在合并…");
          const c = await api("/api/avatar2/concat", {
            method: "POST",
            body: JSON.stringify({ names: names }),
          });
          if (!c.ok) throw new Error(c.error || "合并失败");
          // 合并后碎片没用了，清掉免得视频列表里堆一堆小段
          for (const n of names) {
            try {
              await api("/api/avatar2/video/delete", {
                method: "POST",
                body: JSON.stringify({ name: n, scope: "avatar" }),
              });
            } catch (e) {}
          }
          r = Object.assign({}, lastOk, {
            name: c.name, duration: c.duration, parts: c.parts,
            align: "已合并 " + c.parts + " 段",
          });
        }
        let tail = "";
        if (r.fallback_from) {
          tail = "（真口型失败已回退：" + (r.fallback_reason || "") + "）";
        } else if (r.lipsync_label) {
          tail = " · " + r.lipsync_label;
        }
        if (r.mood_label) {
          tail += " · 心情" + r.mood_label + (r.mood_auto ? "(自动)" : "") +
                  " 语速" + (r.rate >= 0 ? "+" : "") + r.rate;
        }
        say("已生成 " + r.duration + " 秒 · " + (r.size || []).join("×") +
            " · " + (r.align || "") + tail, r.fallback_from ? "err" : "");
        // 自动判心情时把依据也说清楚，别让用户觉得是黑盒
        if (r.mood_auto && (r.mood_why || []).length) {
          say(hint.textContent + "\n判定依据：" + r.mood_why.slice(0, 3).join("；"));
        }
        playVideo(r.name);
        loadVideos();
      } catch (e) {
        say("生成失败：" + e.message, "err");
      } finally {
        busy = false;
        btn.disabled = false;
        btn.textContent = old;
      }
    };

    // —— 启动 ——
    loadInfo().then(() => { loadStatus(); loadVoices(); loadMoods(); loadVideos(); });
    setPin(localStorage.getItem(PIN_KEY) === "1");
  })();

  (function initFoldablePanels() {
    const FOLD_KEY = "folded_panels";
    let folded = {};
    try { folded = JSON.parse(localStorage.getItem(FOLD_KEY) || "{}") || {}; } catch (e) { folded = {}; }
    const saveFolded = () => { try { localStorage.setItem(FOLD_KEY, JSON.stringify(folded)); } catch (e) {} };

    // 2026-11-13：autoPanel（⏰ 自动化）加入这套折叠机制 ——
    // 它和其他面板共用同一套高度动画与 localStorage 记忆，观感一致。
    ["autoPanel", "av2Panel", "kbPanel", "audioPanel", "libraryPanel",
     "hivePanel"].forEach((id) => {
      const pnl = document.getElementById(id);
      if (!pnl) return;
      const head = pnl.querySelector(".sec-head");
      if (!head) return;
      // 已经包过就不再包（热重载 / 重复初始化时防重）
      if (pnl.querySelector(":scope > .pnl-fold")) return;

      const fold = document.createElement("div");
      fold.className = "pnl-fold";
      const inner = document.createElement("div");
      inner.className = "pnl-fold-in";
      fold.appendChild(inner);

      // 先把所有待移动的兄弟节点收集起来再移动 —— 边遍历边 append 会漏掉节点
      const moving = [];
      let n = head.nextSibling;
      while (n) { moving.push(n); n = n.nextSibling; }
      if (!moving.length) return;
      pnl.appendChild(fold);
      moving.forEach((node) => inner.appendChild(node));

      const chev = document.createElement("span");
      chev.className = "fold-chev";
      chev.textContent = "▾";
      chev.setAttribute("aria-hidden", "true");
      // 插在标题文字**后面**，而不是 head 末尾 —— head-acts 有 margin-left:auto，
      // 放在末尾会被推到面板最右边，紧贴边缘、几乎看不见（实测就是这个效果）。
      const h2 = head.querySelector("h2");
      if (h2 && h2.parentNode === head) h2.after(chev);
      else head.insertBefore(chev, head.firstChild);
      head.classList.add("fold-head");
      head.setAttribute("role", "button");
      head.setAttribute("tabindex", "0");

      // 高度动画由 JS 直接写 height：从当前高度 → 0，或从 0 → 内容高度。
      // 展开结束后把 height 设回 auto，内容变多（比如新传了文档）也不会被卡住。
      const foldIn = inner;
      // 同样兜底：overflow/transition 直接写内联，不指望 style.css 那条规则生效
      foldIn.style.overflow = "hidden";
      foldIn.style.transition = "height .3s cubic-bezier(.4, 0, .2, 1)";
      const setHeight = (collapsed, animate) => {
        if (!animate) {
          foldIn.style.transition = "none";
          foldIn.style.height = collapsed ? "0px" : "auto";
          void foldIn.offsetHeight; // 强制重排，让 transition 从新值开始
          // ⚠️ 不能清成 ""：那会回落到 style.css 的规则，而那条在这里不生效
          foldIn.style.transition = "height .3s cubic-bezier(.4, 0, .2, 1)";
          return;
        }
        // ⚠️ 这里不用 requestAnimationFrame：页面在后台标签页时 rAF 会被挂起，
        // 高度就永远停在起始值（实测折叠后仍是 55.5px）。改成「写起始值 →
        // 强制重排 → 写目标值」，transition 照样从起始值动到目标值。
        if (collapsed) {
          const cur = foldIn.getBoundingClientRect().height;
          foldIn.style.height = cur + "px";
          void foldIn.offsetHeight;
          foldIn.style.height = "0px";
        } else {
          const h = foldIn.scrollHeight;
          foldIn.style.height = "0px";
          void foldIn.offsetHeight;
          foldIn.style.height = h + "px";
          setTimeout(() => {
            if (!pnl.classList.contains("collapsed")) foldIn.style.height = "auto";
          }, 330);
        }
      };
      const apply = (collapsed, animate) => {
        pnl.classList.toggle("collapsed", collapsed);
        head.setAttribute("aria-expanded", collapsed ? "false" : "true");
        setHeight(collapsed, animate !== false);
      };
      // 2026-11-13 用户要求：「默认收起，和其他面板一致」——
      // 但其他面板历史上是"没记过就展开"。这里只给 autoPanel 定默认收起，
      // 用户手动展开过之后按记忆走（folded[id] 一旦有值就以它为准）。
      // 数字人面板同理：形象框 + 校准 + 文本框 + 按钮 + 历史，默认展开会把侧栏撑得很长
      const defCollapsed = (id === "autoPanel" || id === "av2Panel");
      apply(folded[id] === undefined ? defCollapsed : !!folded[id], false);

      const toggle = () => {
        const next = !pnl.classList.contains("collapsed");
        apply(next, true);
        folded[id] = next;
        saveFolded();
      };
      head.addEventListener("click", (ev) => {
        // 标题行里的按钮（📂 / ⋯ / ＋建群 / 🔑密码入群）是干活的，不算折叠
        if (ev.target.closest("button")) return;
        toggle();
      });
      head.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggle(); }
      });
    });
  })();

  // ---------- 模型状态卡折叠（2026-11-13）----------
  (function initStatusFold() {
    const card = document.getElementById("statusCard");
    const head = document.getElementById("scToggle");
    if (!card || !head) return;
    const KEY = "statuscard_collapsed";
    const foldIn = document.querySelector("#scFold > .sc-fold-in");
    if (foldIn) {
      foldIn.style.overflow = "hidden";
      foldIn.style.transition = "height .3s cubic-bezier(.4, 0, .2, 1)";
    }
    const setHeight = (collapsed, animate) => {
      if (!foldIn) return;
      if (!animate) {
        foldIn.style.transition = "none";
        foldIn.style.height = collapsed ? "0px" : "auto";
        void foldIn.offsetHeight;
        foldIn.style.transition = "height .3s cubic-bezier(.4, 0, .2, 1)";
        return;
      }
      if (collapsed) {
        const cur = foldIn.getBoundingClientRect().height;
        foldIn.style.height = cur + "px";
        void foldIn.offsetHeight;
        foldIn.style.height = "0px";
      } else {
        const h = foldIn.scrollHeight;
        foldIn.style.height = "0px";
        void foldIn.offsetHeight;
        foldIn.style.height = h + "px";
        setTimeout(() => {
          if (!card.classList.contains("collapsed")) foldIn.style.height = "auto";
        }, 330);
      }
    };
    const apply = (collapsed, animate) => {
      card.classList.toggle("collapsed", collapsed);
      head.setAttribute("aria-expanded", collapsed ? "false" : "true");
      setHeight(collapsed, animate !== false);
    };
    try { apply(localStorage.getItem(KEY) === "1", false); } catch (e) {}
    const toggle = () => {
      const next = !card.classList.contains("collapsed");
      apply(next, true);
      try { localStorage.setItem(KEY, next ? "1" : "0"); } catch (e) {}
    };
    head.addEventListener("click", (ev) => {
      if (ev.target.closest("button")) return;
      toggle();
    });
    head.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggle(); }
    });
  })();
  const _sbCloseBtn = document.querySelector("#sbClose");
  if (_sbCloseBtn) _sbCloseBtn.onclick = closeSidebar;
  const _sbMaskEl = _maskEl();
  if (_sbMaskEl) _sbMaskEl.onclick = closeSidebar;
  // 点侧栏里的菜单项后自动收起（手机上更顺手）
  _sbEl() && _sbEl().addEventListener("click", (ev) => {
    if (window.innerWidth > 768) return;
    if (ev.target.closest(".list-item") || ev.target.closest(".panel .btn")) {
      setTimeout(closeSidebar, 150);
    }
  });
  // 手机端（窄屏）默认把侧栏收起来，否则一打开就盖住半个屏幕（2026-11-09）
  if (window.innerWidth <= 768) {
    $("#sidebar").classList.add("hidden");
    // 手机屏：顶栏默认收起，别让三行控件吃掉半屏（点「▸ 展开」可拉开）
    const _tb = document.querySelector("#topbarToggle");
    if (_tb && !document.body.classList.contains("topbar-collapsed")) _tb.click();
  }

  // ---------- 拖拽文件到聊天框 ----------
  // 拖进来的**任何非图片/视频文件**都按"文档"处理：抽取正文当作本轮资料。
  // 这里刻意**不做扩展名白名单** —— 白名单会把没列到的类型静默丢掉，
  // 用户看到的就是"拖了完全没反应"。能不能读交给后端判断，读不出会明确说明原因。

  // 字数显示：不足 1000 就照实写，别四舍五入成"1k"（一份 50 字的文件显示"1k 字"很误导）
  function fmtChars(n) {
    n = Number(n || 0);
    return n >= 1000 ? (n / 1000).toFixed(1) + "k 字" : n + " 字";
  }

  async function addDocAttachment(f) {
    const chip = { name: f.name, text: "", chars: 0, loading: true };
    docs.push(chip);
    renderAttachments();
    try {
      const fd = new FormData();
      fd.append("file", f, f.name);
      const r = await fetch("/api/doc/extract", { method: "POST", body: fd });
      const d = await r.json();
      if (d.ok) {
        chip.text = d.text || "";
        chip.chars = d.chars || 0;
        chip.loading = false;
        chip.note = d.note || "";
      } else {
        // 抽不出文字（例如旧版 .doc/.wps）：如实告诉用户，别静默失败
        const i = docs.indexOf(chip);
        if (i >= 0) docs.splice(i, 1);
        showToast(`《${f.name}》读不出文字：${d.error || "格式不支持"}`, "warn");
      }
    } catch (e) {
      const i = docs.indexOf(chip);
      if (i >= 0) docs.splice(i, 1);
      showToast(`《${f.name}》上传失败：${e.message || e}`, "warn");
    }
    renderAttachments();
  }

  // 从剪贴板里找出"本机图片路径"（2026-11-09）
  // 不用正则：路径里反斜杠太多，跨层转义容易出错（已经被坑过一次）。
  const IMG_EXT = [".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"];
  function looksLikeImage(p) {
    const q = String(p || "").toLowerCase().replace(/\s+$/, "");
    return IMG_EXT.some((e) => q.endsWith(e));
  }
  function extractLocalPath(text, cb) {
    let t = String(text || "").trim().replace(/^"+|"+$/g, "");
    if (!t) {
      try {
        const html = (cb && cb.getData && cb.getData("text/html")) || "";
        const m = String(html).match(/src=["']([^"']+)["']/i);
        t = m ? m[1] : "";
      } catch (err) { t = ""; }
    }
    if (!t) return "";
    const low = t.toLowerCase();
    if (low.startsWith("file:///")) {
      let p = decodeURI(t.slice(8));
      if (looksLikeImage(p)) return p;
      return "";
    }
    if (/^[a-zA-Z]:/.test(t) && t.length > 3 && looksLikeImage(t)) return t;
    if (t.startsWith("\\\\") && looksLikeImage(t)) return t;
    return "";
  }

  // 把"本机路径"读成附件（后端 /api/local-file）
  async function addImageFromPath(path) {
    try {
      const rr = await fetch("/api/local-file", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: path }),
      });
      const r = await rr.json();
      if (!r.ok) { showToast(r.error || "读取失败", "warn"); return; }
      if (images.indexOf(r.dataurl) >= 0) { showToast("这张图已经在附件里了", "warn"); return; }
      images.push(r.dataurl);
      renderAttachments();
      showToast("已添加：" + r.name, "ok");
    } catch (err) {
      showToast("读取失败：" + err.message, "err");
    }
  }

  function onDropFiles(files) {
    let list = files ? [...files] : [];
    if (!list.length) {
      // 别静默失败：拖进来却什么都没发生，用户只会以为功能坏了
      showToast("没有读到文件。请从资源管理器把文件直接拖到窗口里再松开。", "warn");
      return;
    }
    // ⚠️ **同一批里先去重**（2026-09-22 用户报"拖一张图结果贴上两张"）：
    //    剪贴板/拖拽源有时会同时给出同一个文件的多个表示（PNG + JPG、带/不带元数据），
    //    WebView2 下尤其常见 —— 按 名字+大小+修改时间 判重就够了。
    const seen = new Set();
    list = list.filter((f) => {
      if (!f) return false;
      const key = [f.name || "", f.size || 0, f.lastModified || 0].join("|");
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
    list.forEach((f) => {
      if (!f) return;
      if (f.type && f.type.startsWith("image/")) {
        const reader = new FileReader();
        reader.onload = () => {
          const src = String(reader.result || "");
          // ⚠️ 再按**内容**兜一道：同一张图（字节完全相同）已经挂在那儿了就别重复挂。
          //    （跨批次也拦，因为"同一个图挂两遍"几乎总是误操作，用户不会这么干。）
          if (src && images.indexOf(src) >= 0) {
            showToast("这张图已经在附件里了，没有重复添加", "warn");
            return;
          }
          images.push(src);
          renderAttachments();
        };
        reader.readAsDataURL(f);
      } else if (/\.(mp4|avi|mkv|mov|webm|flv|wmv|m4v|ts)$/i.test(f.name || "")) {        // 视频：交给底部按钮处理逻辑保持一致（复用 videoInput）
        const dt = new DataTransfer(); dt.items.add(f);
        $("#videoInput").files = dt.files;
        $("#videoInput").dispatchEvent(new Event("change"));
      } else {
        // 其余一律当"文档"处理，**不再用扩展名白名单**
        // （以前白名单里没有的类型会被静默忽略，表现为"拖了没反应"）。
        // 能不能读由后端判定，读不出会明确告诉你原因。
        addDocAttachment(f);
      }
    });
  }
  // ---------- 拖拽导入：**整个窗口**都是拖放区 ----------
  // 为什么不绑在 #main 上（踩过）：只绑 #main 的话，拖到左侧边栏、顶部空白、
  // 底部提示这些**不在 #main 里的区域**就完全没人接事件，用户看到的
  // 就是"拖了半天一点反应都没有"。现在改成绑 document、并且用**捕获阶段** ——
  // 事件还没轮到别的元素处理就先被我们接住。
  const dropOverlay = $("#dropOverlay");
  let dragDepth = 0;            // dragenter/dragleave 会在子元素间反复触发，用计数收敛

  function dragHasFiles(e) {
    const dt = e.dataTransfer;
    if (!dt) return false;
    if (dt.types && [...dt.types].includes("Files")) return true;
    return !!(dt.items && [...dt.items].some((it) => it.kind === "file"));
  }
  function setDragging(on) {
    document.body.classList.toggle("dragging", !!on);
    if (dropOverlay) dropOverlay.classList.toggle("show", !!on);
  }
  document.addEventListener("dragenter", (e) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    dragDepth++;
    setDragging(true);
  }, true);
  document.addEventListener("dragover", (e) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();                    // 不 preventDefault 就永远等不到 drop
    e.dataTransfer.dropEffect = "copy";
  }, true);
  document.addEventListener("dragleave", (e) => {
    if (!dragHasFiles(e)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) setDragging(false);
  }, true);
  document.addEventListener("drop", (e) => {
    dragDepth = 0;
    setDragging(false);
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    const dt = e.dataTransfer;
    let files = (dt && dt.files) ? [...dt.files] : [];
    if (!files.length && dt && dt.items) {
      // 少数环境下 files 是空的，得从 items 里捞（必须在任何 await 之前同步取）
      for (const it of dt.items) {
        if (it.kind === "file") { const f = it.getAsFile(); if (f) files.push(f); }
      }
    }
    // 拖到「开发台」里 → 当成"导入到项目"，不要塞进聊天附件
    // （在开发台里拖文件，用户的意图显然是"把这个文件加进项目"）
    const tgt = e.target;
    if (tgt && tgt.closest && tgt.closest(".studio") &&
        window.Studio && window.Studio.onDropFiles) {
      window.Studio.onDropFiles(files);
      return;
    }
    // 群聊模式：拖进来的文件属于这个群，别塞进单人模式的附件区
    if (window.__hiveMode && typeof window.__hiveOnDrop === "function") {
      window.__hiveOnDrop(files);
      return;
    }
    onDropFiles(files);
  }, true);

  // 再兜一层：拖到窗口任意位置都不要让浏览器"直接打开"这个文件 ——
  // 否则整个界面会被替换成文件内容，看起来像把应用弄坏了。
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => e.preventDefault());

  // ---------- 粘贴导入（万一拖放被宿主窗口吞掉，还有这条路）----------
  // 支持：复制文件后 Ctrl+V、截图后 Ctrl+V、复制图片后 Ctrl+V。
  document.addEventListener("paste", (e) => {
    const items = (e.clipboardData && e.clipboardData.items) || [];
    const files = [];
    for (const it of items) {
      if (it.kind === "file") { const f = it.getAsFile(); if (f) files.push(f); }
    }
    const text = (e.clipboardData && e.clipboardData.getData("text")) || "";
    if (files.length) {
      e.preventDefault();
      onDropFiles(files);
    } else {
      // 从资源管理器复制文件后粘贴，剪贴板里是**路径文本**，网页读不了。
      // 但本应用和文件在同一台机器上 —— 让后端按路径读（2026-11-09 用户报"无法传输文件"）。
      const lp = extractLocalPath(text, e.clipboardData);
      if (lp) {
        e.preventDefault();
        addImageFromPath(lp);
      } else if (text && text.length < 40 && /[\\/]/.test(text)) {
        // 像路径但读不了：说清楚怎么绕
        showToast("没认出这个路径的图片。可以用 📎 选文件，或把图片直接拖进来。", "warn");
      }
    }
  });

  // ---------- 图片库 ----------
  // 搜到的图 / 生成的图保存后集中在这里，可随时查看、放大、删除。
  const libGrid = $("#libGrid");
  const libHint = $("#libHint");

  async function loadLibrary() {
    if (!libGrid) return;
    let data;
    try {
      data = await api("/api/library/images");
    } catch (e) {
      return;
    }
    const items = (data && data.images) || [];
    if (libHint) {
      const st = (data && data.stats) || {};
      const mb = ((st.total_bytes || 0) / 1048576).toFixed(1);
      libHint.textContent = items.length
        ? `共 ${items.length} 张 · 占用 ${mb} MB · 点图可放大`
        : "还没有保存的图片。搜图或生成图片后，点「⭐ 保存到图库」即可留存。";
    }
    libGrid.innerHTML = "";
    items.forEach((m) => {
      const cell = document.createElement("div");
      cell.className = "lib-item";
      const tag = m.origin === "web" ? "🌐" : ((m.origin === "gen" || m.origin === "edit") ? "🎨" : "🖼️");
      cell.innerHTML = `
        <img src="/api/library/images/${m.id}/raw" alt="${m.name}" loading="lazy">
        <div class="lib-name" title="${m.name}">${tag} ${m.name}</div>
        <button class="lib-del" title="删除这张图">×</button>`;
      cell.querySelector("img").onclick = async () => {
        try {
          const d = await api("/api/library/images/" + m.id);
          if (d && d.b64) showLightbox("image/png", d.b64, tag + " " + m.name);
        } catch (e) { /* 忽略 */ }
      };
      cell.querySelector(".lib-del").onclick = async (e) => {
        e.stopPropagation();
        if (!confirm(`删除图片「${m.name}」？`)) return;
        await api("/api/library/images/" + m.id, { method: "DELETE" });
        loadLibrary();
        showToast("已删除", "ok");
      };
      libGrid.appendChild(cell);
    });
  }

  const libRefreshBtn = $("#libRefresh");
  if (libRefreshBtn) libRefreshBtn.onclick = loadLibrary;

  // 打开图片库所在的文件夹（就是图片实际保存的位置）
  const libOpenFolderBtn = $("#libOpenFolder");
  if (libOpenFolderBtn) {
    libOpenFolderBtn.onclick = () =>
      openFolderRequest("/api/library/open_folder", { method: "POST" });
  }

  // ---------- 知识库 ----------
  // 把 .txt/.md 丢进这个文件夹就会被检索到，**不需要导入操作** ——
  // 所以界面上最重要的一件事就是"让人知道该往哪放"，
  // 因此把目录路径直接显示出来，并提供一键打开。
  async function loadKb() {
    const list = $("#kbList"), pathEl = $("#kbPath"), hint = $("#kbHint");
    if (!list) return;
    try {
      const d = await api("/api/kb");
      if (pathEl) pathEl.textContent = d.dir || "";
      const docs = d.documents || [];
      if (hint) {
        hint.innerHTML = docs.length
          ? `已收录 <b>${docs.length}</b> 篇文档。把新的 .txt / .md 放进文件夹即自动生效。`
          : "还没有资料。把 .txt / .md 文件放进下面的文件夹即可，<b>不需要导入</b>。";
      }
      if (!docs.length) {
        list.innerHTML = '<p class="hint">（空）</p>';
        return;
      }
      list.innerHTML = "";
      // 知识库支持子文件夹（按课程 / 项目分类放）。
      // 有子文件夹就按文件夹分组，像资源管理器；只有根目录文件时照旧平铺 ——
      // 否则明明没分层却多出一行「📁 根目录」，反而更乱。
      const kbRow = (doc) => {
        const row = document.createElement("div");
        row.className = "kb-item" + (doc.indexed ? "" : " bad");
        const ext = (doc.ext || "").toLowerCase();
        const icon = ext === "pdf" ? "📕"
          : ["doc", "docx", "wps"].includes(ext) ? "📘"
          : ["xls", "xlsx", "et", "csv"].includes(ext) ? "📗"
          : ["ppt", "pptx", "dps"].includes(ext) ? "📙"
          : ["md", "markdown"].includes(ext) ? "📝" : "📄";
        row.innerHTML = `<span class="kb-ico">${icon}</span>
          <span class="kb-dot" aria-hidden="true"></span>
          <span class="kb-body">
            <span class="kb-name"></span>
            <span class="kb-sub"></span>
          </span>
          <span class="kb-size"></span>
          <button class="btn sm ghost danger kb-del" title="从知识库移除">×</button>`;
        const nameEl = row.querySelector(".kb-name");
        const subEl = row.querySelector(".kb-sub");
        nameEl.textContent = doc.filename || doc.id;
        // 卡片里文件名是省略号，悬停看完整相对路径（含子文件夹）+ 备注
        nameEl.title = (doc.rel || doc.filename || doc.id) +
          (doc.note ? "\n" + doc.note : "");
        // 副标题：字数 + 块数。块数 = 这篇在检索时有多少个独立可比对的位置，
        // 比字数更能说明检索粒度（1 篇切成 6 块，就能定位到具体段落）。
        if (doc.indexed) {
          subEl.textContent = fmtChars(doc.chars) + " 字 · " +
            (doc.chunks ? doc.chunks + " 块" : "未建索引");
        } else {
          subEl.textContent = doc.note || "读不出文字";  // 读不了要说清原因，不能让人以为没放进去
        }
        subEl.title = subEl.textContent;
        row.querySelector(".kb-size").textContent = fmtBytes(doc.size || 0);
        const rel = doc.rel || doc.filename || doc.id;
        row.querySelector(".kb-del").onclick = async () => {
          if (!confirm(`从知识库移除《${rel}》？\n（磁盘上的文件也会一起删掉）`)) return;
          // ⚠️ 用 rel（含文件夹）而不是 filename：子目录里可能有同名文件，
          // 只给文件名后端无法确定删哪一个（会拒绝执行）
          await api("/api/kb/" + encodeURIComponent(rel), { method: "DELETE" });
          loadKb();
        };
        return row;
      };
      if (!docs.some((d) => (d.folder || "").trim())) {
        docs.forEach((doc) => list.appendChild(kbRow(doc)));
      } else {
        // 分组顺序**照后端给的来**（后端会把「第一章」排在「第二章」前面）。
        // 前端自己 sort 的话，中文会按拼音排成 二、九、六、七、三… 看不懂。
        const groups = {};
        const order = [];
        docs.forEach((d) => {
          const k = d.folder || "";
          if (!groups[k]) { groups[k] = []; order.push(k); }
          groups[k].push(d);
        });
        order.forEach((folder) => {
          const head = document.createElement("div");
          head.className = "folder-head";
          head.textContent = "📁 " + (folder || "根目录");
          list.appendChild(head);
          groups[folder].forEach((doc) => list.appendChild(kbRow(doc)));
        });
      }
    } catch (e) { /* 静默 */ }
  }

  // 索引状态：后端 / 模型 / 维度 / 块数。放在知识库面板里，
  // 让用户一眼看出「向量检索到底有没有生效」，而不是猜。
  async function loadKbIndexStatus() {
    const el = $("#kbIndexStatus");
    if (!el) return;
    try {
      const d = await api("/api/kb/index_status");
      const s = (d && d.status) || {};
      if (!s.chunks) {
        // 用 textContent 而不是 innerHTML：s.error / s.model 是后端返回的字符串，
        // 拼进 HTML 有注入风险（哪怕来源可信，也没必要留这个口子）
        el.textContent = "索引未建立" +
          (s.error ? "：" + s.error : " —— 点上面「重建索引」");
        return;
      }
      el.textContent = s.chunks + " 块 / " + s.docs + " 篇 · " +
        ((!s.backend || s.embedded === false) ? "仅关键词"
          : (s.backend + " · " + s.model + " · " + s.dim + " 维"));
    } catch (e) {
      el.textContent = "";
    }
  }

  // 面板标题右侧的「⋯」菜单，知识库和生成文库共用。
  // 抽成一个函数是因为两边都要「点开 → 点外面或 Esc 收起」，
  // 各写一遍迟早行为不一致（一边能 Esc 一边不能）。
  function bindMoreMenu(btnId, menuId) {
    const btn = $("#" + btnId), menu = $("#" + menuId);
    if (!btn || !menu) return;
    const set = (show) => {
      menu.hidden = !show;
      btn.setAttribute("aria-expanded", show ? "true" : "false");
    };
    btn.onclick = (e) => { e.stopPropagation(); set(menu.hidden); };
    // 点菜单外面收起 —— 否则开着菜单去点别处，菜单一直挡着下面的列表
    document.addEventListener("click", (e) => {
      if (menu.hidden) return;
      if (menu.contains(e.target) || btn.contains(e.target)) return;
      set(false);
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !menu.hidden) set(false);
    });
  }

  (function bindKbPanel() {
    const open = $("#kbOpenFolder");
    if (open) open.onclick = () =>
      openFolderRequest("/api/kb/open_folder", { method: "POST" });

    bindMoreMenu("kbMore", "kbMenu");
    // 点路径直接复制，省得手抄一长串 D:\...\data\kb
    const pathEl = $("#kbPath");
    if (pathEl) pathEl.onclick = async () => {
      const t = (pathEl.textContent || "").trim();
      if (!t) return;
      try {
        await navigator.clipboard.writeText(t);
        showToast("路径已复制", "ok");
      } catch (e) {
        showToast("复制失败，请手动选中路径", "warn");
      }
    };

    const rf = $("#kbRefresh");
    if (rf) rf.onclick = () => { loadKb(); loadKbIndexStatus(); };
    const rx = $("#kbReindex");
    if (rx) rx.onclick = async () => {
      const old = rx.textContent;
      rx.disabled = true;
      rx.textContent = "⏳ 构建中…";
      try {
        const d = await api("/api/kb/reindex", { method: "POST" });
        const s = (d && d.stats) || {};
        if (s.ok) {
          showToast("索引已重建：" + s.chunks + " 块 / " + s.docs + " 篇 / " + s.ms + "ms",
                    "ok");
        } else {
          showToast("重建失败：" + (s.error || "未知原因"), "warn");
        }
      } catch (e) {
        showToast("重建失败：" + String(e.message || e), "warn");
      } finally {
        rx.disabled = false;
        rx.textContent = old;
        loadKbIndexStatus();
        // 重建后每篇的块数会变，列表必须一起刷，否则卡片上还是旧块数
        loadKb();
      }
    };
    loadKbIndexStatus();
  })();


  // ---------- 音频库（TTS 产物）----------
  // 数据来自 /api/audio/list（data/tts 目录）；播放走 /api/tts/audio/{name}。
  let __auAudio = null;      // 当前正在播的 Audio 对象
  let __auPlaying = "";      // 正在播的文件名（用来切 ▶ / ⏸）
  let __auDir = "";          // data/tts 的绝对路径（后端 /api/audio/list 返回的 dir）

  async function loadAudioLib() {
    const box = $("#audioList");
    if (!box) return;
    let d;
    try {
      d = await api("/api/audio/list");
    } catch (e) {
      box.innerHTML = '<div class="hint">读取失败：' + escapeHtml(String(e.message || e)) + "</div>";
      return;
    }
    const files = (d && d.files) || [];
    __auDir = (d && d.dir) || __auDir;
    const hintEl = $("#audioHint");
    if (hintEl && __auDir) {
      hintEl.textContent = "语音合成和音乐生成的音频都放这里，点 ▶ 试听。保存位置：" + __auDir;
    }
    if (!files.length) {
      box.innerHTML = '<div class="hint">还没有音频。让我「读一下这段」就会生成到这里。</div>';
      return;
    }
    box.innerHTML = files.map((f) => {
      const kb = (f.bytes / 1024).toFixed(1);
      const playing = __auPlaying === f.name;
      return '<div class="list-item au-item" data-name="' + escapeHtml(f.name) + '">' +
        '<button type="button" class="au-play" title="试听">' + (playing ? "⏸" : "▶") + "</button>" +
        '<span class="au-name" title="' + escapeHtml(f.name) + '">' + escapeHtml(f.name) + "</span>" +
        '<span class="dl-meta">' + escapeHtml(f.modified) + " · " + kb + "KB</span>" +
        '<a class="au-dl" href="' + escapeHtml(f.url) + '" download title="下载">⬇</a>' +
        '<button type="button" class="au-loc" title="在文件夹中显示这个文件">📁</button>' +
        '<button type="button" class="au-ren" title="重命名">✏️</button>' +
        '<button type="button" class="dl-del" title="删除">🗑</button>' +
        "</div>";
    }).join("");
  }

  function auPlay(url, name) {
    if (__auAudio) { __auAudio.pause(); __auAudio = null; }
    if (__auPlaying === name) { __auPlaying = ""; loadAudioLib(); return; }   // 再点一次 = 停
    __auPlaying = name;
    __auAudio = new Audio(url);
    __auAudio.onended = () => { __auPlaying = ""; __auAudio = null; loadAudioLib(); };
    __auAudio.play().catch(() => {
      __auPlaying = ""; __auAudio = null; loadAudioLib(); showToast("播放失败", "warn");
    });
    loadAudioLib();
  }

  (() => {
    const box = $("#audioList"), rf = $("#audioRefresh");
    if (rf) rf.onclick = () => loadAudioLib();
    const ofb = $("#audioOpenFolder");
    if (ofb) ofb.onclick = () => openFolderRequest("/api/audio/open_folder", { method: "POST" });
    if (!box) return;
    box.addEventListener("click", async (ev) => {
      const item = ev.target.closest(".au-item");
      if (!item) return;
      const name = item.getAttribute("data-name") || "";
      if (ev.target.closest(".au-play")) {
        try {
          const d = await api("/api/audio/list");
          const hit = ((d && d.files) || []).find((x) => x.name === name);
          if (hit) auPlay(hit.url, name);
        } catch (e) { showToast("读取失败", "warn"); }
        return;
      }
      if (ev.target.closest(".au-loc")) {
        // 打开文件所在文件夹并**选中这个文件**（后端 /api/open_path 走 explorer /select）
        const sep = (__auDir || "").indexOf("\\") >= 0 ? "\\" : "/";
        const full = __auDir ? __auDir + sep + name : name;
        await openFolderRequest("/api/open_path",
          { method: "POST", body: JSON.stringify({ path: full }) });
        return;
      }
      if (ev.target.closest(".au-ren")) {
        const nn = prompt("新的文件名（扩展名会保留）", name.replace(/\.[^.]+$/, ""));
        if (!nn) return;
        try {
          const r = await api("/api/audio/rename",
            { method: "POST", body: JSON.stringify({ name: name, new_name: nn }) });
          showToast(r && r.ok ? "已重命名" : ((r && r.error) || "重命名失败"), r && r.ok ? "ok" : "warn");
          loadAudioLib();
        } catch (e) { showToast(String(e.message || e), "warn"); }
        return;
      }
      if (ev.target.closest(".dl-del")) {
        if (!confirm("删除这个音频？删掉找不回来。")) return;
        try {
          const r = await api("/api/audio/delete",
            { method: "POST", body: JSON.stringify({ name: name }) });
          if (r && r.ok) {
            if (__auPlaying === name) {
              if (__auAudio) __auAudio.pause();
              __auPlaying = ""; __auAudio = null;
            }
            showToast("已删除", "ok"); loadAudioLib();
          } else showToast((r && r.error) || "删除失败", "warn");
        } catch (e) { showToast(String(e.message || e), "warn"); }
        return;
      }
    });
  })();

  // ---------- 生成文库（模型产出的文件）----------
  // 刻意和知识库分开：知识库是用户的资料、只读；这里是模型的产出，可改可删可导出。
  let dlCurrent = "";

  // 生成文库整块可折叠：点标题收起 / 展开，动画由 CSS（grid-template-rows 0fr↔1fr）负责
  (() => {
    const panel = $("#doclibPanel"), btn = $("#dlToggle");
    if (!panel || !btn) return;
    // 把标题之后的内容整体包进 .dl-body > .dl-body-in —— 有这一层才能做连续的高度过渡
    const head = panel.querySelector(".sec-head");
    if (head && !panel.querySelector(".dl-body")) {
      const body = document.createElement("div");
      body.className = "dl-body"; body.id = "dlBody";
      const inner = document.createElement("div");
      inner.className = "dl-body-in";
      Array.prototype.slice.call(panel.children).forEach((n) => {
        if (n !== head) inner.appendChild(n);
      });
      body.appendChild(inner); panel.appendChild(body);
    }
    // 2026-11-12 用户报「前端没有音频栏」：生成文库默认展开时，7 个分类（40+ 文件）
    // 会把下面的「🎵 音频」挤出 900px 视口，滚到「软件/压缩包」看着就像到底了。
    // 改用 v2 这个 key + 默认收起，音频栏一进页面就在视野内；用户点标题展开后照旧记住。
    const KEY = "doclib.collapsed.v2";
    const apply = (closed, remember) => {
      panel.classList.toggle("collapsed", closed);
      btn.setAttribute("aria-expanded", closed ? "false" : "true");
      if (remember) { try { localStorage.setItem(KEY, closed ? "1" : "0"); } catch (e) {} }
    };
    let saved = "1";
    try { saved = localStorage.getItem(KEY) || "1"; } catch (e) {}
    if (saved === "1") apply(true, false);
    btn.onclick = () => apply(!panel.classList.contains("collapsed"), true);
    btn.onkeydown = (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); btn.onclick(); }
    };
    // 外部（如「在文件库中打开」）要定位文件时，先自动展开
    window.__dlExpand = () => { if (panel.classList.contains("collapsed")) apply(false, true); };
  })();

  async function loadDoclib(highlight) {
  // 2026-11-11 重构：改走「全量视图」—— 生成文库 + 工作区软件 + 生成图片，
  // 按类别分组、可展开收起、双击用本机程序打开。旧实现保留在 loadDoclibLegacy。
  return __dlLoadAll(highlight);
    const list = $("#dlList");
    if (!list) return;
    if (highlight) dlCurrent = highlight;
    let d;
    try {
      d = await api("/api/doclib/files");
    } catch (e) {
      list.innerHTML = '<div class="hint">读取失败：' + escapeHtml(String(e.message || e)) + "</div>";
      return;
    }
    const files = d.files || [];
    if (!files.length) {
      list.innerHTML = '<div class="hint">还没有文件。让模型「把这篇作文写成文档」' +
        '或者「给我一个 Python 模块」，它就会存到这里。</div>';
      return;
    }
    const dlRow = (f) => {
      // 注意反斜杠只有一个：`/\\.(docx)$/` 是"反斜杠+任意字符+docx"，
      // 永远匹配不上 .docx（早先过度转义留下的坑，这里修掉）
      const icon = /\.(docx)$/i.test(f.name) ? "📄"
        : /\.(md|markdown)$/i.test(f.name) ? "📝"
        : /\.(py|js|ts|java|c|cpp|go|rs|sh|bat|ps1)$/i.test(f.name) ? "💻"
        : /\.(json|csv|yml|yaml|ini)$/i.test(f.name) ? "🗂" : "📃";
      return `<div class="list-item dl-item${f.rel === dlCurrent ? " active" : ""}" data-rel="${escapeHtml(f.rel)}">` +
        `<span class="dl-icon">${icon}</span>` +
        `<span class="dl-name" title="${escapeHtml(f.rel)}">${escapeHtml(f.name)}</span>` +
        `<span class="dl-meta">${escapeHtml(f.modified)} · ${(f.size / 1024).toFixed(1)}KB</span>` +
        `<button type="button" class="dl-del" data-rel="${escapeHtml(f.rel)}" title="删除">🗑</button></div>`;
    };
    // 同知识库：有子文件夹才分组显示
    if (!files.some((f) => (f.folder || "").trim())) {
      list.innerHTML = files.map(dlRow).join("");
    } else {
      // 同知识库：顺序按后端给的来，别在前端重排
      const groups = {};
      const order = [];
      files.forEach((f) => {
        const k = f.folder || "";
        if (!groups[k]) { groups[k] = []; order.push(k); }
        groups[k].push(f);
      });
      // 文件夹可点开/收起（手机上没有资源管理器，"查看文件夹"就是展开这一组）2026-11-09
      list.innerHTML = order
        .map((folder) =>
          `<button type="button" class="folder-head" data-folder="${escapeHtml(folder)}">` +
          `<span class="fh-arrow">▾</span> 📁 ${escapeHtml(folder || "根目录")}` +
          `<span class="fh-count">${groups[folder].length}</span></button>` +
          `<div class="folder-body" data-folder="${escapeHtml(folder)}">` +
          groups[folder].map(dlRow).join("") + "</div>")
          // __probe__
        .join("");
    }
    // 文件夹展开/收起
    // 目录默认收起：列表里只看到目录名，点一下展开、再点收起 2026-11-09
    list.querySelectorAll(".folder-body").forEach((b) => b.classList.add("collapsed"));
    list.querySelectorAll(".folder-head .fh-arrow").forEach((a) => { a.textContent = "▸"; });
    // 要定位某个文件时，自动展开它所在的那一组（默认收起状态看不到高亮项）
    if (highlight) {
      const hit = list.querySelector('.folder-body .dl-item[data-rel="' + String(highlight).replace(/"/g, '\\"') + '"]');
      const box = hit && hit.closest(".folder-body");
      if (box) {
        box.classList.remove("collapsed");
        const head = box.previousElementSibling;
        if (head && head.classList.contains("folder-head")) {
          const ar = head.querySelector(".fh-arrow");
          if (ar) ar.textContent = "▾";
        }
      }
    }
    list.querySelectorAll(".folder-head").forEach((h) => {
      h.onclick = () => {
        const body = list.querySelector('.folder-body[data-folder="' +
          (h.dataset.folder || "").replace(/"/g, '\\"') + '"]');
        if (!body) return;
        body.style.maxHeight = body.scrollHeight + "px";
        void body.offsetHeight;
        const closed = body.classList.toggle("collapsed");
        h.querySelector(".fh-arrow").textContent = closed ? "▸" : "▾";
        // 高度过渡：收起时过渡到 0；展开时过渡到实测高度，动画结束后放开为 none（否则内容变多会被截断）
        if (closed) setTimeout(() => { body.style.maxHeight = "0px"; }, 0);
        else {
          body.style.maxHeight = body.scrollHeight + "px";
          setTimeout(() => {
            if (!body.classList.contains("collapsed")) body.style.maxHeight = "none";
          }, 340);
        }
      };
    });
    list.querySelectorAll(".dl-del").forEach((btn) => {
      btn.onclick = async (ev) => {
        ev.stopPropagation();
        const rel = btn.dataset.rel;
        if (!confirm("删除《" + rel + "》？\n（会移进回收站，需要时能捞回来）")) return;
        try {
          await api("/api/doclib/delete", { method: "POST",
            body: JSON.stringify({ rel: rel }) });
          showToast("已删除（在回收站里）", "ok");
          if (dlCurrent === rel) { const ed = $("#dlEditor"); if (ed) ed.hidden = true; dlCurrent = ""; }
          loadDoclib();
        } catch (e) {
          showToast("删除失败：" + (e.message || e), "err");
        }
      };
    });
    list.querySelectorAll(".dl-item").forEach((el) => {
      el.onclick = () => openDoclibFile(el.dataset.rel);
      // 双击：用本机默认程序直接打开（Word/PPT/Excel 双击即开，不用去文件夹里翻）
      el.ondblclick = () => {
        if (window.__Artifacts && window.__Artifacts.openWithSystem) {
          window.__Artifacts.openWithSystem(el.dataset.rel);
        }
      };
    });
  }

  // ---------- 生成文库 · 全量视图（2026-11-11 重构）----------
  // 用户要求：① 文库要有文本 / PPT / Excel / Word / 图片 / 软件等**所有**产出；
  //          ② 双击直接用本机程序打开；③ 按类别展开与收起（默认全部收起）。
  // 数据来自 /api/doclib/all（后端聚合 文库 + 工作区项目 + 生成图片，只读、不动磁盘）。
  const DL_CATS = [
    ["recent", "🕒 最近生成"],
    ["doc", "📄 文档"],
    ["ppt", "📊 演示文稿"],
    ["sheet", "📈 表格"],
    ["text", "📝 文本"],
    ["code", "💻 代码"],
    ["image", "🖼 图片 / 视频"],
    ["software", "📦 软件 / 压缩包"],
    ["other", "📎 其它"],
  ];
  const DL_OPEN_KEY = "doclib.openCats";
  let dlAll = [], dlById = {}, dlCurrentId = "";

  function dlOpenSet() {
    try {
      const raw = localStorage.getItem(DL_OPEN_KEY);
      if (raw) return new Set(JSON.parse(raw) || []);
    } catch (e) { /* 读不到就按默认：全部收起 */ }
    return new Set();
  }
  function dlSaveOpen(set) {
    try { localStorage.setItem(DL_OPEN_KEY, JSON.stringify(Array.from(set))); } catch (e) {}
  }
  function dlSizeText(b) {
    const n = Number(b) || 0;
    if (n >= 1048576) return (n / 1048576).toFixed(1) + "MB";
    if (n >= 1024) return (n / 1024).toFixed(0) + "KB";
    return n + "B";
  }
  function dlIconOf(f) {
    const m = { doc: "📄", ppt: "📊", sheet: "📈", text: "📝",
                code: "💻", image: "🖼", software: "📦", other: "📎" };
    return m[f.cat] || "📎";
  }
  function dlCatLabel(key) {
    for (let i = 0; i < DL_CATS.length; i++) if (DL_CATS[i][0] === key) return DL_CATS[i][1];
    return key;
  }
  function dlRowHtml(f) {
    const sub = f.src === "ws" ? (f.project || "") : (f.folder || "");
    const tag = (f.src === "ws" ? '<span class="dl-tag">软件</span>'
      : (f.src === "img" ? '<span class="dl-tag">生成图</span>' : "")) +
      (f.runnable ? '<span class="dl-run">▶ 双击运行</span>' : "");
    return '<div class="list-item dl-item' + (f.id === dlCurrentId ? " active" : "") +
      '" data-id="' + escapeHtml(f.id) + '" data-rel="' +
      escapeHtml(f.src === "lib" ? f.rel : "") +
      '" title="' + (f.runnable ? "双击直接运行：" : "双击用本机程序打开：") +
      escapeHtml(f.name) + '">' +
      '<span class="dl-icon">' + dlIconOf(f) + '</span>' +
      '<span class="dl-main"><span class="dl-name">' + escapeHtml(f.name) + '</span>' +
      '<span class="dl-sub">' + (sub ? escapeHtml(sub) + " · " : "") +
      escapeHtml(f.modified) + " · " + dlSizeText(f.size) +
      (tag ? " " + tag : "") + '</span></span>' +
      (f.editable ? '<button type="button" class="dl-del" data-rel="' +
        escapeHtml(f.rel) + '" title="删除">🗑</button>' : "") +
      '</div>';
  }
  function dlCatBlock(key, items, opened) {
    return '<div class="dl-cat' + (opened ? " open" : "") + '" data-cat="' + key + '">' +
      '<button type="button" class="dl-cat-head" data-cat="' + key + '" aria-expanded="' +
      (opened ? "true" : "false") + '">' +
      '<span class="dl-cat-arrow">' + (opened ? "▾" : "▸") + '</span>' +
      '<span class="dl-cat-name">' + dlCatLabel(key) + '</span>' +
      '<span class="dl-cat-count">' + items.length + '</span></button>' +
      '<div class="dl-cat-body">' + items.map(dlRowHtml).join("") + '</div></div>';
  }

  async function dlOpenWithSystem(id) {
    const f = dlById[id];
    if (!f) return;
    showToast((f.runnable ? "正在启动程序：" : "正在用本机程序打开：") + f.name, "");
    try {
      const r = await api("/api/doclib/open_any", { method: "POST",
        body: JSON.stringify({ id: id }) });
      if (r && r.opened) showToast("已打开：" + f.name, "ok");
      else showToast((r && r.detail) || "无法直接打开这个文件", "warn");
    } catch (e) {
      showToast("打开失败：" + (e.message || e), "warn");
    }
  }

  async function dlSelect(id) {
    const f = dlById[id];
    if (!f) return;
    dlCurrentId = id;
    document.querySelectorAll(".dl-item").forEach((el) =>
      el.classList.toggle("active", el.dataset.id === id));
    // 文本 / 代码：面板内打开可编辑；Office、软件、图片：提示双击用本机程序打开
    if (f.editable && (f.cat === "text" || f.cat === "code" || f.cat === "other")) {
      await openDoclibFile(f.rel);
    } else {
      showToast("双击「" + f.name + "」用本机程序打开", "");
    }
  }

  async function dlDelete(rel) {
    if (!rel) return;
    if (!confirm("删除《" + rel + "》？\n（会移进回收站，需要时能捞回来）")) return;
    try {
      await api("/api/doclib/delete", { method: "POST",
        body: JSON.stringify({ rel: rel }) });
      showToast("已删除（在回收站里）", "ok");
      if (dlCurrent === rel) {
        const ed = $("#dlEditor"); if (ed) ed.hidden = true; dlCurrent = "";
      }
      loadDoclib();
    } catch (e) {
      showToast("删除失败：" + (e.message || e), "err");
    }
  }

  async function __dlLoadAll(highlight) {
    const list = $("#dlList");
    if (!list) return;
    let d;
    try {
      d = await api("/api/doclib/all");
    } catch (e) {
      list.innerHTML = '<div class="hint">读取失败：' +
        escapeHtml(String(e.message || e)) + "</div>";
      return;
    }
    dlAll = d.files || [];
    dlById = {};
    dlAll.forEach((f) => { dlById[f.id] = f; });
    if (!dlAll.length) {
      list.innerHTML = '<div class="hint">还没有任何产出。让模型写文档、做 PPT / Excel、' +
        '写代码或做软件，产物都会出现在这里（工作区项目和生成的图片也算）。</div>';
      return;
    }
    // 旧调用传的是文库内 rel（「在文件库中定位」），换算成新 id
    if (highlight) {
      const hit = dlAll.find((f) => f.src === "lib" && f.rel === highlight);
      if (hit) dlCurrentId = hit.id;
    }
    const open = dlOpenSet();
    const byCat = {};
    dlAll.forEach((f) => { (byCat[f.cat] = byCat[f.cat] || []).push(f); });
    const recent = (d.recent || []).map((id) => dlById[id]).filter(Boolean);
    // 定位时自动展开它所在的类别，否则收起状态下看不见高亮项
    if (highlight && dlCurrentId) {
      const hit = dlById[dlCurrentId];
      if (hit) {
        open.add(hit.cat);
        if (recent.some((x) => x.id === hit.id)) open.add("recent");
        dlSaveOpen(open);
      }
    }
    let html = "";
    if (recent.length) html += dlCatBlock("recent", recent, open.has("recent"));
    DL_CATS.forEach((pair) => {
      const key = pair[0];
      if (key === "recent") return;
      const arr = byCat[key] || [];
      if (arr.length) html += dlCatBlock(key, arr, open.has(key));
    });
    list.innerHTML = html;

    list.querySelectorAll(".dl-cat-head").forEach((h) => {
      h.onclick = () => {
        const box = h.parentElement;
        const key = h.dataset.cat;
        const isOpen = box.classList.toggle("open");
        h.setAttribute("aria-expanded", isOpen ? "true" : "false");
        const ar = h.querySelector(".dl-cat-arrow");
        if (ar) ar.textContent = isOpen ? "▾" : "▸";
        const set = dlOpenSet();
        if (isOpen) set.add(key); else set.delete(key);
        dlSaveOpen(set);
      };
    });
    list.querySelectorAll(".dl-item").forEach((el) => {
      el.onclick = () => dlSelect(el.dataset.id);
      // 双击：一律用本机默认程序打开（Word/PPT/Excel/zip/exe 都是双击即开）
      el.ondblclick = () => dlOpenWithSystem(el.dataset.id);
    });
    list.querySelectorAll(".dl-del").forEach((btn) => {
      btn.onclick = (ev) => { ev.stopPropagation(); dlDelete(btn.dataset.rel); };
    });
    if (highlight) {
      const esc = String(highlight).replace(/"/g, '\\"');
      const hit = list.querySelector('.dl-item[data-rel="' + esc + '"]');
      if (hit) {
        hit.classList.add("dl-flash");
        setTimeout(() => hit.classList.remove("dl-flash"), 2600);
      }
    }
  }

  async function openDoclibFile(rel) {
    dlCurrent = rel;
    let d;
    try {
      d = await api("/api/doclib/file?rel=" + encodeURIComponent(rel));
    } catch (e) {
      // docx 这类二进制文件读不出文本是正常的，给一条能走的出路
      showToast("这个文件读不出正文（可能是 Word/表格这类）。可以直接下载或导出。", "warn");
      return;
    }
    $("#dlEditor").hidden = false;
    $("#dlEditorName").textContent = d.rel + "（" + (d.chars || 0) + " 字）";
    $("#dlText").value = d.text || "";
    document.querySelectorAll(".dl-item").forEach((el) =>
      el.classList.toggle("active", el.dataset.rel === rel));
  }

  (function bindDoclibPanel() {
    const wrap = $("#doclibPanel");
    if (!wrap) return;
    bindMoreMenu("dlMore", "dlMenu");
    const open = $("#dlOpenFolder");
    if (open) open.onclick = () =>
      openFolderRequest("/api/doclib/open_folder", { method: "POST" });
    const rf = $("#dlRefresh");
    if (rf) rf.onclick = () => loadDoclib();
    const bk = $("#dlBackup");
    if (bk) bk.onclick = async () => {
      try {
        const r = await api("/api/doclib/backup", { method: "POST" });
        showToast(`已备份 ${r.count} 个文件到 ${r.path}`, "ok");
      } catch (e) { showToast("备份失败：" + String(e.message || e), "warn"); }
    };
    const close = $("#dlCloseBtn");
    if (close) close.onclick = () => { $("#dlEditor").hidden = true; dlCurrent = ""; };
    const save = $("#dlSaveBtn");
    if (save) save.onclick = async () => {
      if (!dlCurrent) return;
      try {
        await api("/api/doclib/file", { method: "POST",
          body: JSON.stringify({ rel: dlCurrent, text: $("#dlText").value }) });
        showToast("已保存（原内容自动备份）", "ok");
        loadDoclib();
      } catch (e) { showToast("保存失败：" + String(e.message || e), "warn"); }
    };
    const docx = $("#dlDocxBtn");
    if (docx) docx.onclick = async () => {
      if (!dlCurrent) return;
      docx.disabled = true;
      try {
        // 先把当前编辑框的内容存下来，再导出 —— 否则导出的还是旧版本
        await api("/api/doclib/file", { method: "POST",
          body: JSON.stringify({ rel: dlCurrent, text: $("#dlText").value }) });
        const r = await api("/api/doclib/export_docx", { method: "POST",
          body: JSON.stringify({ rel: dlCurrent }) });
        showToast("已导出：" + r.rel + "（WPS / Word 都能打开）", "ok");
        // ⚠️ 这里**不能**传 r.rel：loadDoclib 会把 dlCurrent 指到那个新 .docx 上，
        // 下次点「保存」就会把文本正文写进 .docx 文件、把刚导出的 Word 冲掉。
        // 保持 dlCurrent 仍指向原文本文件，新文件靠列表按时间倒序自然置顶。
        loadDoclib();
      } catch (e) { showToast("导出失败：" + String(e.message || e), "warn"); }
      finally { docx.disabled = false; }
    };
    const dl = $("#dlDownload");
    if (dl) dl.onclick = async () => {
      if (!dlCurrent) return;
      const url = "/api/doclib/download?rel=" + encodeURIComponent(dlCurrent);
      // 桌面壳里 window.open 会被丢给外部浏览器（OpenExternalLinksInBrowser），
      // 先试原生另存为；没有桥再退回原来的行为。
      if (await window.__saveViaDesktop(url)) return;
      window.open(url, "_blank");
    };
    const cp = $("#dlCopyBtn");
    if (cp) cp.onclick = async () => {
      if (!dlCurrent) return;
      // 预填「原名-副本」：想改名就改，直接回车也能用
      const dot = dlCurrent.lastIndexOf(".");
      const guess = dot > 0
        ? dlCurrent.slice(0, dot) + "-副本" + dlCurrent.slice(dot)
        : dlCurrent + "-副本";
      const name = await showInputDialog({
        title: "复制为",
        tip: "会在原位置生成一份新文件，原文件不动。名字可以改，也可带子目录（如 草稿/第二版.md）。",
        value: guess,
        okText: "复制",
        placeholder: "新文件名",
      });
      if (!name || name === dlCurrent) return;
      cp.disabled = true;
      try {
        const r = await api("/api/doclib/copy", { method: "POST",
          body: JSON.stringify({ rel: dlCurrent, new_rel: name }) });
        showToast("已复制为：" + r.to, "ok");
        await loadDoclib();
        // 文本类直接打开新文件（复制完通常就是要接着改它）；
        // Word/表格这类读不出正文，只刷新列表，免得弹一句"读不出正文"扫兴。
        if (/\.(md|markdown|txt|json|csv|log|ini|cfg|ya?ml|py|js|ts|html?|css|sql|sh|bat)$/i.test(r.to)) {
          await openDoclibFile(r.to);
        }
      } catch (e) { showToast("复制失败：" + String(e.message || e), "warn"); }
      finally { cp.disabled = false; }
    };
    const del = $("#dlDelBtn");
    if (del) del.onclick = async () => {
      if (!dlCurrent) return;
      if (!confirm("删除《" + dlCurrent + "》？\n（会移进回收站，需要时能捞回来）")) return;
      try {
        await api("/api/doclib/delete", { method: "POST",
          body: JSON.stringify({ rel: dlCurrent }) });
        showToast("已删除（在回收站里）", "ok");
        $("#dlEditor").hidden = true;
        dlCurrent = "";
        loadDoclib();
      } catch (e) { showToast("删除失败：" + String(e.message || e), "warn"); }
    };
  })();


  // ---------- 多会话管理 ----------
  // 每个会话互相独立（各自的消息与上下文），全部持久化在后端，
  // 程序重启后自动恢复上次使用的会话。
  const sessionsListEl = $("#sessionsList");

  // 界面渲染 + 送给后端的上下文条数。
  // ⚠️ 原来是 20（=10 轮），实测太短：聊到十几轮以后模型就开始"忘了前面说的话"。
  // 真正兜底的是后端的 token 预算（_trim_history_to_budget），
  // 它装不下会自动裁并压成摘要 —— 所以这里可以放心放宽。
  const RECENT_SHOW = 40;

  /** 把历史消息渲染到界面，并同步为上下文。返回实际渲染条数。 */
  function renderHistory(msgs) {
    clearThinkDock();
    messagesEl.innerHTML = "";
    history.length = 0;
    const shown = (msgs || []).slice(-RECENT_SHOW);
    const hidden = (msgs || []).length - shown.length;
    if (hidden > 0) {
      const tip = document.createElement("div");
      tip.className = "msg bot";
      tip.innerHTML = `<div class="bubble fold-tip">📁 更早的 ${hidden} 条记录已折叠（要点已存入长期记忆）</div>`;
      messagesEl.appendChild(tip);
    }
    shown.forEach((m) => {
      if ((m.role === "user" || m.role === "assistant") && m.content) {
        addMsg(m.role, m.content);
        history.push({ role: m.role, content: m.content });
      }
    });
    scrollBottom(true);        // 切会话是主动行为 → 强制回到底部
    return shown.length;
  }

  async function loadSessions() {
    let data;
    try {
      data = await api("/api/sessions");
    } catch (e) {
      return [];
    }
    const list = (data && data.sessions) || [];
    // ⚠️⚠️ 这里会**悄悄换掉当前会话**：localStorage 里没有 id、或那个会话已经不在
    // 列表里时，自动切到最新的一条。它和文件末尾的 loadMemory() 是**并发**跑的：
    //   ① loadMemory 先用「旧的（可能是空）sessionId」把请求发了出去
    //   ② loadSessions 回来，把 sessionId 改成另一条
    //   ③ 那个响应回来时 `d.session_id !== sessionId` → 被判成"用户已切走"**整条丢弃**：
    //      不填任何字段、不报错、也不会重试
    // → 界面就**一直停在灰色占位符**上，看着像"记忆一个字都没记住"。
    // 实测复现过：DOM 里 #memLongMeta / #memShortMeta 全空、#memSessName 还是初始的「—」。
    // 所以：只要真的换了会话，这里必须**补刷一次 loadMemory()**。
    if (!sessionId || !list.some((s) => s.id === sessionId)) {
      const next = list.length ? list[0].id : "";
      const changed = next !== sessionId;
      setSessionId(next);
      if (changed) loadMemory();
    }
    renderSessions(list);
    return list;
  }

  // 批量选择状态（只在本次页面里，不落盘）
  let sessBatchMode = false;
  const sessSelected = new Set();
  let sessCache = [];        // 最近一次渲染的列表，切批量模式时直接重绘、不再请求

  function updateSessBatchBar() {
    const bar = $("#sessBatchBar"), info = $("#sessBatchInfo"), allBtn = $("#sessSelectAll");
    if (!bar) return;
    bar.hidden = !sessBatchMode;
    if (!sessBatchMode) return;
    const n = sessSelected.size;
    if (info) info.innerHTML = n ? `已选 <b>${n}</b> 个` : "已选 0 个";
    if (allBtn) allBtn.textContent = (n && n === sessCache.length) ? "全不选" : "全选";
  }

  function renderSessions(list) {
    if (!sessionsListEl) return;
    sessCache = list || [];
    // 列表刷新后，把已经不存在的选中项丢掉
    const ids = new Set(sessCache.map((s) => s.id));
    [...sessSelected].forEach((id) => { if (!ids.has(id)) sessSelected.delete(id); });
    sessionsListEl.innerHTML = "";
    if (!sessCache.length) {
      sessionsListEl.innerHTML = '<p class="hint">暂无对话，点「＋ 新建」开始。</p>';
      updateSessBatchBar();
      return;
    }
    sessCache.forEach((s) => {
      const row = document.createElement("div");
      row.className = "session-item" + (s.id === sessionId ? " active" : "");
      const title = document.createElement("span");
      title.className = "session-title";
      title.textContent = s.title || "新对话";
      title.title = `${s.title || "新对话"}（${s.count || 0} 条）`;
      if (sessBatchMode) {
        // 批量模式：行首勾选框，点整行＝勾选，不会切会话
        const box = document.createElement("input");
        box.type = "checkbox";
        box.className = "sess-check";
        box.checked = sessSelected.has(s.id);
        box.onclick = (ev) => ev.stopPropagation();
        box.onchange = () => {
          if (box.checked) sessSelected.add(s.id); else sessSelected.delete(s.id);
          row.classList.toggle("sel", box.checked);
          updateSessBatchBar();
        };
        row.classList.toggle("sel", box.checked);
        row.onclick = () => { box.checked = !box.checked; box.onchange(); };
        row.appendChild(box);
        row.appendChild(title);
      } else {
        const del = document.createElement("button");
        del.className = "session-del";
        del.textContent = "×";
        del.title = "删除这个对话";
        del.onclick = async (ev) => {
          ev.stopPropagation();
          if (!confirm(`删除对话「${s.title || "新对话"}」？`)) return;
          await api("/api/sessions/" + s.id, { method: "DELETE" });
          const rest = await loadSessions();
          if (s.id === sessionId && rest.length) await switchSession(rest[0].id);
        };
        row.appendChild(title);
        row.appendChild(del);
        row.onclick = () => switchSession(s.id);
      }
      sessionsListEl.appendChild(row);
    });
    updateSessBatchBar();
  }

  function setSessBatchMode(on) {
    sessBatchMode = !!on;
    if (!sessBatchMode) sessSelected.clear();
    const btn = $("#sessBatchBtn");
    if (btn) btn.textContent = sessBatchMode ? "完成" : "批量";
    renderSessions(sessCache);
  }

  async function deleteSelectedSessions() {
    const ids = [...sessSelected];
    if (!ids.length) { showToast("先勾选要删除的对话", "warn"); return; }
    if (streaming) { showToast("正在回答中，请稍候再删", "warn"); return; }
    if (!confirm(`删除选中的 ${ids.length} 个对话？删掉就找不回来了。`)) return;
    const cur = sessionId;
    let ok = 0;
    for (const id of ids) {
      try { await api("/api/sessions/" + id, { method: "DELETE" }); ok++; }
      catch (e) { /* 单条失败不影响其它 */ }
    }
    sessSelected.clear();
    const rest = await loadSessions();
    // 当前对话也被删掉 → 切到剩下最新的一条，否则界面会停在一个已不存在的会话上
    if (cur && !rest.some((s) => s.id === cur)) {
      if (rest.length) { setSessionId(""); await switchSession(rest[0].id); }
      else {
        history.length = 0;
        clearThinkDock();
        messagesEl.innerHTML = "";
        addMsg("bot", "对话都删掉了，点「＋ 新建」开一段新的。");
      }
    }
    setSessBatchMode(false);
    showToast(`已删除 ${ok} 个对话`, "ok");
  }

  // 对话列表：整块折叠（点标题行，状态记 localStorage）+ 批量按钮绑定
  (function bindSessFold() {
    const head = $("#sessToggle"), body = $("#sessFoldBody"), caret = $("#sessCaret");
    if (!head || !body) return;
    const KEY = "ui.sessFolded";
    const isFolded = () => head.classList.contains("folded");
    const apply = (folded, animate) => {
      head.classList.toggle("folded", folded);
      head.setAttribute("aria-expanded", folded ? "false" : "true");
      if (caret) caret.textContent = folded ? "▸" : "▾";
      if (!animate) {
        body.style.transition = "none";
        body.style.maxHeight = folded ? "0px" : "none";
        body.style.opacity = folded ? "0" : "1";
        void body.offsetHeight;
        body.style.transition = "";
        return;
      }
      if (folded) {
        body.style.maxHeight = body.scrollHeight + "px";
        requestAnimationFrame(() => {
          body.style.maxHeight = "0px";
          body.style.opacity = "0";
        });
      } else {
        body.style.maxHeight = body.scrollHeight + "px";
        body.style.opacity = "1";
        // 动画结束后放开高度限制，之后列表变长也不会被裁
        setTimeout(() => { if (!isFolded()) body.style.maxHeight = "none"; }, 360);
      }
    };
    let folded = false;
    try { folded = localStorage.getItem(KEY) === "1"; } catch (e) { /* 隐私模式忽略 */ }
    apply(folded, false);
    const toggle = () => {
      const next = !isFolded();
      if (next && sessBatchMode) setSessBatchMode(false);   // 收起时退出批量
      apply(next, true);
      try { localStorage.setItem(KEY, next ? "1" : "0"); } catch (e) { /* 忽略 */ }
    };
    head.onclick = (e) => {
      if (e.target.closest(".head-acts")) return;   // 点「批量 / 新建」不折叠
      toggle();
    };
    head.onkeydown = (e) => {
      if (e.target !== head) return;
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    };
    const batchBtn = $("#sessBatchBtn");
    if (batchBtn) batchBtn.onclick = () => setSessBatchMode(!sessBatchMode);
    const allBtn = $("#sessSelectAll");
    if (allBtn) allBtn.onclick = () => {
      if (sessSelected.size && sessSelected.size === sessCache.length) sessSelected.clear();
      else sessCache.forEach((s) => sessSelected.add(s.id));
      renderSessions(sessCache);
    };
    const delBtn = $("#sessDelSel");
    if (delBtn) delBtn.onclick = deleteSelectedSessions;
    const cancelBtn = $("#sessCancel");
    if (cancelBtn) cancelBtn.onclick = () => setSessBatchMode(false);
  })();

  async function switchSession(sid) {
    if (!sid || sid === sessionId) return;
    if (streaming) { showToast("正在回答中，请稍候再切换", "warn"); return; }
    setSessionId(sid);
    let msgs = [];
    try {
      const d = await api("/api/sessions/" + sid);
      msgs = d.messages || [];
    } catch (e) { /* 读取失败则当作空会话 */ }
    if (renderHistory(msgs) === 0) {
      addMsg("bot", "这是一段新对话，直接说需求即可。");
    }
    await loadSessions();
    // 记忆按对话隔离：换了对话，记忆面板也必须跟着换
    await loadMemory();
    showToast(`已切换到「${await currentTitle(sid)}」`);
  }

  async function currentTitle(sid) {
    try {
      const d = await api("/api/sessions");
      const hit = (d.sessions || []).find((s) => s.id === sid);
      return (hit && hit.title) || "新对话";
    } catch (e) { return "新对话"; }
  }

  async function newSession() {
    if (streaming) { showToast("正在回答中，请稍候再新建", "warn"); return; }
    try {
      const d = await api("/api/sessions", { method: "POST", body: JSON.stringify({}) });
      const sid = d.session && d.session.id;
      if (!sid) return;
      setSessionId(sid);
      history.length = 0;
      clearThinkDock();
      messagesEl.innerHTML = "";
      addMsg("bot", "新对话已开始，这段对话与之前互不影响。");
      await loadSessions();
      // 新对话有自己独立的记忆（从空白开始），面板要跟着切
      await loadMemory();
      showToast("已新建对话", "ok");
    } catch (e) {
      showToast("新建失败：" + e.message, "warn");
    }
  }

  async function persistSession() {
    if (!sessionId) return;
    try {
      const d = await api("/api/sessions/" + sessionId, {
        method: "PUT",
        body: JSON.stringify({ messages: history }),
      });
      if (d && d.session) renderSessions(await loadSessions_noSwitch());
    } catch (e) { /* 保存失败不打断对话 */ }
  }

  async function loadSessions_noSwitch() {
    try {
      const d = await api("/api/sessions");
      return (d && d.sessions) || [];
    } catch (e) { return []; }
  }

  const newSessionBtn = $("#newSessionBtn");
  if (newSessionBtn) newSessionBtn.onclick = newSession;

  // ---------- 记忆库 · 文段式 ----------
  // ---------- 记忆库：长期（跨对话共享）+ 短期（按对话独立）----------
  // 切对话时只有"短期记忆"跟着变；长期记忆是同一份，永远不变。
  // 长期记忆读取失败了几次（用于"自动重试 + 显示原因"）
  let _memLoadFails = 0;

  async function loadMemory() {
    const lng = $("#memLongText"), sht = $("#memShortText");
    if (!lng || !sht) return;
    try {
      const d = await api("/api/memory?session_id=" + encodeURIComponent(sessionId || ""));
      if ((d.session_id || "") !== (sessionId || "")) return;   // 用户已切走，别覆盖
      lng.value = d.long || "";
      sht.value = d.short || "";
      _memLoadFails = 0;
      const lm = $("#memLongMeta");
      if (lm) lm.textContent = `${(d.long || "").length} / ${d.long_cap || 2000} 字`;
      const sm = $("#memShortMeta");
      if (sm) sm.textContent = (d.short || "").length
        ? `${(d.short || "").length} / ${d.short_cap || 1000} 字`
        : "这个对话还没有短期记忆";
      const nm = $("#memSessName");
      if (nm) nm.textContent = await currentTitle(sessionId);
    } catch (e) {
      // ⚠️ **这里绝不能静默**。原来是一句 `catch (e) { /* 静默 */ }`，后果很坑：
      // 后端还没起来 / 正在重启时加载失败，输入框就只剩**灰色占位符**，
      // 用户看到的是"记忆是空的、字体发暗"，以为模型压根没记住 ——
      // 实测被这么误判过（我重启动应用时抓到的，截图里那个就是）。
      // 现在：① 在字数行上写明失败原因；② 自动退避重试几次。
      _memLoadFails += 1;
      const lm = $("#memLongMeta");
      if (lm) lm.textContent = `⚠️ 记忆读取失败（第 ${_memLoadFails} 次）：${e.message || e}`;
      if (_memLoadFails <= 4) setTimeout(loadMemory, 1500 * _memLoadFails);
    }
  }

  // ⚠️ 别在对话一结束就立刻 loadMemory()。
  // 后端提炼记忆是**异步 + 防抖**的：停手 6 秒才启动，
  // 而提炼本身要跑一次完整推理（思考型模型很慢）——
  // **实测在 12GB 显卡上从"回答结束"到"记忆落盘"要 ~155 秒**（2026-09-15 实测）。
  // 答完马上刷新只会读到"还没提炼"的旧内容，用户看到面板没变，
  // 会误判成"模型没记住"（这正是用户反馈的问题）。
  // 所以按实测节奏补刷几次，一直覆盖到 3 分钟。
  let _memTimers = [];
  function scheduleMemoryRefresh() {
    _memTimers.forEach(clearTimeout);
    _memTimers = [10000, 30000, 60000, 120000, 180000].map((ms) => setTimeout(() => {
      loadMemory();
      loadMemoryUsage();
    }, ms));
  }

  function bindMemoryPanel() {
    const ls = $("#memLongSave");
    if (ls) ls.onclick = async () => {
      const r = await api("/api/memory", {
        method: "POST",
        body: JSON.stringify({ scope: "long", content: $("#memLongText").value }),
      });
      showToast(r.ok ? "已保存长期记忆（所有对话通用）" : "保存失败", r.ok ? "ok" : "warn");
      loadMemory();
    };
    const ss = $("#memShortSave");
    if (ss) ss.onclick = async () => {
      if (!sessionId) { showToast("还没有对话", "warn"); return; }
      const r = await api("/api/memory", {
        method: "POST",
        body: JSON.stringify({ session_id: sessionId, content: $("#memShortText").value }),
      });
      showToast(r.ok ? "已保存本次对话的短期记忆" : "保存失败", r.ok ? "ok" : "warn");
      loadMemory();
    };
    const sc = $("#memShortClear");
    if (sc) sc.onclick = async () => {
      if (!sessionId) return;
      if (!confirm("清空**这个对话**的短期记忆？\n\n其他对话的短期记忆、以及长期记忆都不受影响。")) return;
      await api("/api/memory?scope=session&session_id=" + encodeURIComponent(sessionId),
                { method: "DELETE" });
      showToast("已清空本对话短期记忆");
      loadMemory();
    };

    // ---- 向量检索：试一句看召回 + 重建索引 ----
    const vgo = $("#memVecGo");
    if (vgo) vgo.onclick = () => runMemVecSearch();
    const vq = $("#memVecQ");
    if (vq) vq.onkeydown = (e) => {
      if (e.key === "Enter") { e.preventDefault(); runMemVecSearch(); }
    };
    const vrb = $("#memVecRebuild");
    if (vrb) vrb.onclick = async () => {
      const old = vrb.textContent;
      vrb.disabled = true;
      vrb.textContent = "重建中…";
      try {
        const r = await api("/api/memory/reindex", { method: "POST" });
        showToast(r.indexed
          ? `索引已重建：${r.items} 条 · ${r.ms}ms`
          : `已重建，但没有可用向量模型（${r.backend || "无"}），检索走关键词兜底`,
          r.indexed ? "ok" : "warn");
      } catch (e) {
        showToast("重建失败：" + (e.message || e), "warn");
      }
      vrb.textContent = old;
      vrb.disabled = false;
      loadMemVecStat();
    };
  }
  bindMemoryPanel();
  // ---------- 记忆库 · 向量检索（状态 + 检索测试）----------
  // 为什么要有这个测试框：召回率好不好，用户不该只能"感觉"——
  // 输入一句话就能看到召回了哪几条、各自语义分/字面分多少，
  // 阈值调得对不对、索引有没有生效，一眼可验。
  async function loadMemVecStat() {
    const el = $("#memVecStat");
    if (!el) return;
    try {
      const d = await api("/api/memory/index");
      if (!d.ok) { el.textContent = "向量索引：不可用"; return; }
      const lv = d.levels || {};
      const parts = Object.keys(lv).map((k) => `${k} ${lv[k]}`).join(" · ");
      const model = d.indexed
        ? `${d.model || "?"} · ${d.dim || 0}维`
        : "无向量模型（关键词兜底）";
      el.textContent = `🧠 ${d.items || 0} 条（${parts || "空"}）· ${model}`
        + (d.transcript_files ? ` · 历史会话 ${d.transcript_files} 个` : "");
      el.title = el.textContent;
    } catch (e) {
      el.textContent = "向量索引：读取失败";
    }
  }

  async function runMemVecSearch() {
    const box = $("#memVecHits");
    if (!box) return;
    const q = (($("#memVecQ") || {}).value || "").trim();
    if (!q) { box.hidden = true; return; }
    box.hidden = false;
    box.innerHTML = `<div class="mem-vec-empty">检索中…</div>`;
    try {
      const d = await api("/api/memory/search?q=" + encodeURIComponent(q)
        + "&session_id=" + encodeURIComponent(sessionId || "") + "&top_k=6");
      const hits = d.hits || [];
      if (!hits.length) {
        box.innerHTML = `<div class="mem-vec-empty">没有相关记忆（阈值生效：宁缺毋滥）</div>`;
        return;
      }
      box.innerHTML = hits.map((h) => {
        const sc = Number(h.score || 0).toFixed(3);
        const extra = (h.vec === undefined) ? ""
          : ` · 语义 ${Number(h.vec).toFixed(3)} · 字面 ${Number(h.cov || 0).toFixed(2)}`;
        return `<div class="mem-vec-hit">
          <div class="mem-vec-hit-h">${esc(h.level || "")} · 分 ${sc}${extra}</div>
          <div class="mem-vec-hit-b">${esc(h.content || "")}</div>
        </div>`;
      }).join("");
    } catch (e) {
      box.innerHTML = `<div class="mem-vec-empty">检索失败：${esc(e.message || String(e))}</div>`;
    }
  }

  loadMemVecStat();

  // ---------- 内存占用：看清占在哪 + 一键释放 ----------
  // 设计原则：**释放只动对话记录与归档，长期记忆一律保留**。
  // 用户最怕的是"清理的时候把有用的记忆也清了"，所以界面上要反复讲清楚这一点。
  function fmtBytes(n) {
    n = Number(n || 0);
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
    return (n / 1073741824).toFixed(2) + " GB";
  }
  function fmtTokens(n) {
    n = Number(n || 0);
    return n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n);
  }

  async function loadMemoryUsage() {
    const list = $("#muList"), totals = $("#muTotals"), total = $("#muTotal");
    if (!list) return;
    try {
      const d = await api("/api/memory/usage");
      if (!d.ok) return;
      const t = d.totals || {};
      total.textContent = fmtBytes(t.disk_bytes);
      totals.innerHTML =
        `<div class="mu-row"><span>对话记录</span><b>${fmtBytes(t.sessions_bytes)}</b>`
        + `<em>${t.session_count} 个对话 · ${t.message_count} 条消息</em></div>`
        + `<div class="mu-row"><span>归档底稿</span><b>${fmtBytes(t.transcript_bytes)}</b>`
        + `<em>${t.transcript_files} 个文件（释放前会先沉淀要点）</em></div>`
        + `<div class="mu-row keep"><span>🧩 长期记忆</span><b>${t.long_chars || 0} 字</b>`
        + `<em>所有对话共享 · 始终保留</em></div>`
        + `<div class="mu-row"><span>💬 短期记忆</span><b>${t.short_chars || 0} 字</b>`
        + `<em>${t.short_used || 0} 个对话有短期记忆 · 可释放</em></div>`
        + (t.rss_bytes
            ? `<div class="mu-row"><span>运行内存</span><b>${fmtBytes(t.rss_bytes)}</b><em>应用进程常驻内存</em></div>`
            : "");

      const items = d.sessions || [];
      if (!items.length) {
        list.innerHTML = '<p class="hint">暂无对话记录。</p>';
      } else {
        list.innerHTML = "";
        items.slice(0, 12).forEach((s) => {
          const row = document.createElement("div");
          row.className = "mu-sess";
          const full = s.messages >= (t.context_messages || 80);
          row.innerHTML = `<div class="mu-sess-head">
              <span class="mu-sess-title"></span>
              <span class="mu-sess-size">${fmtBytes(s.bytes)}</span>
            </div>
            <div class="mu-sess-meta">${s.messages} 条 · 约 ${fmtTokens(s.tokens)} token`
            + `${s.memory_chars ? ` · 记忆 ${s.memory_chars} 字` : ' · 无记忆'}`
            + `${full ? ' · <span class="mu-warn">已达上下文上限，早期内容靠摘要保留</span>' : ''}</div>
            <button class="btn sm ghost mu-free">释放此对话历史</button>`;
          row.querySelector(".mu-sess-title").textContent = s.title || s.id;
          row.querySelector(".mu-free").onclick = async (ev) => {
            ev.stopPropagation();
            if (!confirm(`释放「${s.title || s.id}」的对话记录？\n\n`
                       + "· 释放前会先把这段对话里值得记住的要点提炼进记忆\n"
                       + "· 记忆本身不受影响")) return;
            const r = await api("/api/memory/release", {
              method: "POST",
              body: JSON.stringify({ scope: "session", session_id: s.id, sweep: true }),
            });
            const extra = r.swept ? `，先沉淀了 ${r.swept} 条要点` : "";
            showToast(r.ok ? `已释放 ${fmtBytes(r.freed_bytes)}${extra}` : "释放失败",
                      r.ok ? "ok" : "warn");
            loadMemoryUsage();
            loadMemory();
          };
          list.appendChild(row);
        });
      }
    } catch (e) { /* 忽略：面板不可用不影响其它功能 */ }
  }

  // 记忆库：整块可折叠（点标题行收起 / 展开，状态记在 localStorage）
  (function bindMemFold() {
    const head = $("#memToggle"), body = $("#memFoldBody"), caret = $("#memCaret");
    if (!head || !body) return;
    const KEY = "ui.memFolded";
    const isFolded = () => head.classList.contains("folded");
    const apply = (folded, animate) => {
      head.classList.toggle("folded", folded);
      head.setAttribute("aria-expanded", folded ? "false" : "true");
      if (caret) caret.textContent = folded ? "▸" : "▾";
      if (!animate) {
        body.style.transition = "none";
        body.style.maxHeight = folded ? "0px" : "none";
        body.style.opacity = folded ? "0" : "1";
        void body.offsetHeight;
        body.style.transition = "";
        return;
      }
      if (folded) {
        body.style.maxHeight = body.scrollHeight + "px";
        requestAnimationFrame(() => {
          body.style.maxHeight = "0px";
          body.style.opacity = "0";
        });
      } else {
        body.style.maxHeight = body.scrollHeight + "px";
        body.style.opacity = "1";
        // 动画结束后放开高度限制，内容再变化也不会被裁
        setTimeout(() => { if (!isFolded()) body.style.maxHeight = "none"; }, 360);
      }
    };
    let folded = false;
    try { folded = localStorage.getItem(KEY) === "1"; } catch (e) { /* 隐私模式忽略 */ }
    apply(folded, false);
    const toggle = () => {
      const next = !isFolded();
      apply(next, true);
      try { localStorage.setItem(KEY, next ? "1" : "0"); } catch (e) { /* 忽略 */ }
    };
    head.onclick = toggle;
    head.onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    };
  })();

  (function bindMemUsage() {
    const head = $("#muToggle"), body = $("#muBody");
    if (!head || !body) return;
    head.onclick = () => {
      body.hidden = !body.hidden;
      head.querySelector(".mu-caret").textContent = body.hidden ? "▸" : "▾";
      head.classList.toggle("open", !body.hidden);
      if (!body.hidden) loadMemoryUsage();
    };
    const rf = $("#muRefresh");
    if (rf) rf.onclick = (e) => { e.stopPropagation(); loadMemoryUsage(); };
    // 释放短期记忆：它是"缓存"性质的，可以先清；
    // 清之前后端会先把归档里的要点沉淀一遍，避免把结论一起清掉。
    const fs = $("#muFreeShort");
    if (fs) fs.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm("释放所有对话的**短期记忆**？\n\n"
                 + "· 会先做：把归档里还没记下的要点提炼进记忆\n"
                 + "· 会清掉：各对话的短期记忆（正在做的事、本次结论）\n"
                 + "· 不会动：长期记忆（身份/偏好/约定）")) return;
      fs.disabled = true;
      const old = fs.textContent;
      fs.textContent = "沉淀并释放中…";
      try {
        const r = await api("/api/memory/release",
                            { method: "POST",
                              body: JSON.stringify({ scope: "short", sweep: true }) });
        if (r.ok) {
          const extra = r.swept ? `，先沉淀了 ${r.swept} 条要点` : "";
          showToast(`已释放短期记忆 ${r.short_freed_chars || 0} 字${extra}；长期记忆已保留`, "ok");
        } else {
          showToast(r.reason || "释放失败", "warn");
        }
        loadMemoryUsage();
        loadMemory();
      } finally {
        fs.disabled = false;
        fs.textContent = old;
      }
    };
    const rl = $("#muRelease");
    if (rl) rl.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm("释放缓存占用？\n\n"
                 + "· 会先做：把归档里还没记下的重要要点提炼进记忆\n"
                 + "· 再清理：归档底稿、几乎没用过的空会话、过长对话的早期记录\n"
                 + "· 不会动：各对话的记忆、全局偏好")) return;
      rl.disabled = true;
      const old = rl.textContent;
      rl.textContent = "沉淀并释放中…";
      try {
        const r = await api("/api/memory/release",
                            { method: "POST",
                              body: JSON.stringify({ scope: "auto", sweep: true }) });
        if (r.ok) {
          const extra = r.swept ? `，先沉淀了 ${r.swept} 条要点` : "";
          showToast(`已释放 ${fmtBytes(r.freed_bytes)}${extra}；记忆已保留`, "ok");
        } else {
          showToast(r.reason || "释放失败", "warn");
        }
        loadMemoryUsage();
        loadMemory();
      } finally {
        rl.disabled = false;
        rl.textContent = old;
      }
    };
  })();

  // ---------- 附件：图片 / 文档 / 视频 ----------
  $("#attachBtn").onclick = () => $("#fileInput").click();
  $("#fileInput").onchange = (e) => {
    onDropFiles(e.target.files);
    e.target.value = "";
  };

  // 文档走独立入口：**不设 accept 白名单**（设了就只能在对话框里选到那几种）。
  // 这条路的必要性：拖放依赖宿主窗口把事件交给网页，万一被吞掉，
  // 用户就彻底没办法导入文档了 —— 有这个按钮至少永远有条走得通的路。
  $("#docBtn").onclick = () => $("#docInput").click();
  $("#docInput").onchange = (e) => {
    onDropFiles(e.target.files);
    e.target.value = "";
  };

  $("#videoBtn").onclick = () => $("#videoInput").click();
  $("#videoInput").onchange = (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const tip = showThinking("正在读取视频并抽取关键帧…");
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        const videoEl = document.createElement("video");
        videoEl.src = URL.createObjectURL(f);
        videoEl.crossOrigin = "anonymous";
        videoEl.muted = true;
        await new Promise((res) => { videoEl.onloadeddata = res; videoEl.load(); });
        const frames = [];
        for (let i = 0; i < 6; i++) {
          await new Promise((r) => setTimeout(r, 150));
          videoEl.currentTime = (videoEl.duration * i) / 6;
          await new Promise((res) => { videoEl.onseeked = res; });
          const c = document.createElement("canvas");
          c.width = videoEl.videoWidth; c.height = videoEl.videoHeight;
          const scale = Math.min(768 / Math.max(c.width, c.height), 1);
          c.width = Math.round(c.width * scale); c.height = Math.round(c.height * scale);
          c.getContext("2d").drawImage(videoEl, 0, 0, c.width, c.height);
          frames.push(c.toDataURL("image/jpeg", 0.8));
        }
        videoB64 = frames.map((s) => s.split(",")[1]);
        tip.querySelector(".bubble").textContent = `✔ 已抽取 ${frames.length} 帧视频画面，可发送给 AI 分析。`;
      } catch (err) {
        tip.querySelector(".bubble").textContent = "❌ 视频抽帧失败: " + err.message;
      }
      e.target.value = "";
    };
    reader.readAsArrayBuffer(f);
  };

  function renderAttachments() {
    const box = $("#attachments");
    box.innerHTML = "";
    images.forEach((src, i) => {
      const d = document.createElement("div");
      d.className = "attachment";
      d.innerHTML = `<img src="${src}"><span class="x">✕</span>`;
      d.querySelector(".x").onclick = () => { images.splice(i, 1); renderAttachments(); };
      box.appendChild(d);
    });
    // 文档附件：显示成一张小卡片（文件名 + 字数）
    docs.forEach((doc, i) => {
      const d = document.createElement("div");
      d.className = "attachment doc" + (doc.loading ? " loading" : "");
      const icon = /\.pdf$/i.test(doc.name) ? "📕"
                 : /\.(docx?|wps)$/i.test(doc.name) ? "📘"
                 : /\.(xlsx?|et)$/i.test(doc.name) ? "📗"
                 : /\.(pptx?|dps)$/i.test(doc.name) ? "📙" : "📄";
      d.innerHTML = `<span class="doc-ico">${icon}</span>
        <span class="doc-meta"><b></b><em></em></span><span class="x">✕</span>`;
      d.querySelector("b").textContent = doc.name;
      d.querySelector("em").textContent = doc.loading
        ? "解析中…" : fmtChars(doc.chars);
      d.querySelector(".x").onclick = () => { docs.splice(i, 1); renderAttachments(); };
      box.appendChild(d);
    });
    if (videoB64) {
      const d = document.createElement("div"); d.className = "attachment vid";
      d.innerHTML = `<span style="font-size:22px">🎬</span><span class="x">✕</span>`;
      d.querySelector(".x").onclick = () => { videoB64 = null; renderAttachments(); };
      box.appendChild(d);
    }
  }

  // ---------- 发送 / 流式接收 ----------
  // 检索来源列表：回答下方的可伸缩面板（默认折叠）
  // 为什么放在正文之外：链接列表塞进回答气泡会把答案本身淹掉，
  // 而且模型每次重写都可能漏抄或改动链接；由后端直接给出真实来源更可靠。
  function renderSources(afterEl, data) {
    const items = data.items || [];
    const box = document.createElement("div");
    box.className = "src-box";

    const hdr = document.createElement("button");
    hdr.className = "src-hdr";
    hdr.innerHTML = `<span class="src-caret">▸</span>
      <span class="src-title">📚 检索来源（${items.length}）</span>
      <span class="src-hint">${data.query ? escapeHtml(data.query).slice(0, 40) : ""}</span>`;

    const list = document.createElement("div");
    list.className = "src-list";
    list.hidden = true;

    items.forEach((it) => {
      const row = document.createElement("div");
      row.className = "src-item";
      const site = it.site ? `<span class="src-site">${escapeHtml(it.site)}</span>` : "";
      const read = it.read ? `<span class="src-read" title="已抓取该页正文用于分析">已精读</span>` : "";
      row.innerHTML = `<span class="src-idx">[${it.i}]</span>
        <a class="src-link" href="${escapeHtml(it.url)}" target="_blank" rel="noopener"></a>
        ${site}${read}`;
      const a = row.querySelector(".src-link");
      a.textContent = it.title || it.url;
      a.title = it.url;
      list.appendChild(row);
    });

    const toggle = () => {
      list.hidden = !list.hidden;
      hdr.classList.toggle("open", !list.hidden);
      hdr.querySelector(".src-caret").textContent = list.hidden ? "▸" : "▾";
    };
    hdr.onclick = toggle;

    box.appendChild(hdr);
    box.appendChild(list);
    // 插到回答气泡之后
    if (afterEl && afterEl.parentNode) afterEl.parentNode.insertBefore(box, afterEl.nextSibling);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function showLightbox(mime, b64, prompt) {
    const ov = document.createElement("div");
    ov.className = "lightbox";
    ov.innerHTML = `<div class="lb-box">
      <img src="data:${mime || "image/png"};base64,${b64}">
      <div class="lb-cap"></div>
      <div class="lb-tools">
        <button class="lb-save">💾 保存到本地</button>
        <button class="lb-open">📂 打开所在文件夹</button>
      </div>
      <button class="lb-close" title="关闭">✕</button>
    </div>`;
    ov.querySelector(".lb-cap").textContent = prompt || "";
    ov.onclick = (e) => { if (e.target === ov) ov.remove(); };
    ov.querySelector(".lb-close").onclick = () => ov.remove();

    const cap = ov.querySelector(".lb-cap");
    let lastPath = null;
    ov.querySelector(".lb-save").onclick = async (e) => {
      e.stopPropagation();
      const fn = "generated_" + Date.now() + (mime === "image/jpeg" ? ".jpg" : ".png");
      const body = { b64: b64, filename: fn, mime: mime || "image/png" };
      try {
        const r = await fetch("/api/save_image", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const d = await r.json().catch(() => ({ ok: false, detail: `服务端返回 ${r.status}（非 JSON）` }));
        if (d.ok) { lastPath = d.path; cap.textContent = "✅ 已保存：" + d.path; }
        else cap.textContent = "❌ 保存失败：" + (d.detail || "");
      } catch (err) {
        cap.textContent = "❌ 保存失败：" + err;
      }
    };
    ov.querySelector(".lb-open").onclick = (e) => {
      e.stopPropagation();
      openFolderRequest("/api/open_folder", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: lastPath }),
      }, cap);
    };
    document.body.appendChild(ov);
  }

  async function send() {
    setFreeView(false);        // 自己发消息 → 恢复跟随，别停在半路
    const text = inputEl.value.trim();
    const imgB64 = images.map((s) => s.split(",")[1]);
    if (!text && imgB64.length === 0 && !videoB64) return;
    if (streaming) {
      // 2026-11-10：桌面端"发不出去"最常见的原因是 streaming 卡在 true（上一轮异常没复位），
      //   老代码直接 return，用户点什么都没反应。这里先判断是否真的还在跑：
      const idle = Date.now() - (window.__lastStreamAt || 0);
      const stuck = !abortCtl || idle > 60000;
      if (stuck) {
        console.warn("[send] streaming 卡住了（闲置 " + Math.round(idle / 1000) + " 秒），自动复位");
        streaming = false;
        try { abortCtl && abortCtl.abort(); } catch (e) { /* 忽略 */ }
        abortCtl = null;
        try { setStopVisible(false); $("#sendBtn").disabled = false; } catch (e) {}
        showToast("上一轮似乎卡住了，已自动复位，正在重发", "warn");
      } else {
        showToast("上一条还在生成，等它结束或点「■ 终止」", "warn");
        return;
      }
    }
    window.__lastStreamAt = Date.now();
    // ★ 2026-11-12：用户主动发消息 → 自动退出自由查看。
    //   否则开着「自由查看」时发出去的消息会被留在屏幕外，用户以为没发出去。
    setFreeView(false);
    const promptText = text || (videoB64 ? "分析这段视频的内容" : "请详细描述这张图片的内容");
    addMsg("user", promptText);
    if (videoB64) {
      const el = document.createElement("div"); el.className = "msg user";
      el.innerHTML = `<div class="bubble">🎬 已附加视频关键帧 (${videoB64.length} 帧)</div>`;
      messagesEl.appendChild(el);
    } else {
      images.forEach((src) => {
        const el = document.createElement("div"); el.className = "msg user";
        el.innerHTML = `<div class="bubble" style="padding:6px"><img src="${src}" style="max-width:220px;border-radius:8px"></div>`;
        messagesEl.appendChild(el);
      });
    }
    const media = videoB64 ? [...videoB64] : imgB64;
    // 文档附件：把抽取出的正文带上（解析中的/失败的不要发）
    const docPayload = docs.filter((d) => d.text && d.text.trim())
                           .map((d) => ({ name: d.name, text: d.text }));
    if (docPayload.length) {
      const el = document.createElement("div");
      el.className = "msg user";
      el.innerHTML = `<div class="bubble doc-sent"></div>`;
      el.querySelector(".doc-sent").textContent =
        "📎 " + docPayload.map((d) => d.name).join("、");
      messagesEl.appendChild(el);
    }
    images = []; videoB64 = null; docs = []; renderAttachments();
    inputEl.value = ""; autoGrow();
    await doSend(promptText, media.length ? media : null, docPayload);
    scheduleMemoryRefresh();   // 记忆提炼是后端异步防抖的，延迟补刷面板
  }

  // ---------- 涂抹选区编辑器 ----------
  const maskChip = $("#maskChip");
  const refreshMaskChip = () => { if (maskChip) maskChip.hidden = !selMask; };
  const clearSelMask = () => { selMask = null; selMaskOn = null; refreshMaskChip(); };

  const openMaskEditor = (imgDataUrl) => {
    const layer = document.createElement("div");
    layer.className = "mask-editor";
    layer.innerHTML =
      '<div class="me-box">' +
        '<div class="me-head">✂️ 涂抹要改的地方' +
          '<span class="me-tip">被涂到的区域才会重画，其余部分原样保留</span></div>' +
        '<div class="me-stage"><div class="me-wrap">' +
          '<img src="' + imgDataUrl + '" alt="">' +
          '<canvas></canvas>' +
        '</div></div>' +
        '<div class="me-bar">' +
          '<div class="seg">' +
            '<button type="button" data-mode="brush" class="on">🖌 涂抹</button>' +
            '<button type="button" data-mode="rect">▭ 矩形</button>' +
            '<button type="button" data-mode="erase">🧽 擦除</button>' +
          '</div>' +
          '<label>粗细 <input type="range" min="8" max="140" value="40"></label>' +
          '<span class="me-count">已涂 0%</span>' +
          '<span class="sp"></span>' +
          '<button type="button" class="btn ghost" data-act="clear">清空</button>' +
          '<button type="button" class="btn ghost" data-act="cancel">取消</button>' +
          '<button type="button" class="btn primary" data-act="ok">就用这块区域</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(layer);

    const img = layer.querySelector("img");
    const cv = layer.querySelector("canvas");
    const ctx = cv.getContext("2d");
    const countEl = layer.querySelector(".me-count");
    let mode = "brush", size = 40, ops = [], drawing = false, cur = null;

    // 记录"画了哪些笔"（而不是只存像素）：这样擦除、清空、导出蒙版都好做
    const init = () => {
      const w = img.naturalWidth || 1024, h = img.naturalHeight || 1024;
      cv.width = w; cv.height = h;
      paint();
    };
    const toCv = (ev) => {
      const r = cv.getBoundingClientRect();
      return [ (ev.clientX - r.left) * (cv.width / (r.width || 1)),
               (ev.clientY - r.top) * (cv.height / (r.height || 1)) ];
    };
    const paint = () => {
      const w = cv.width, h = cv.height;
      ctx.clearRect(0, 0, w, h);
      const base = Math.max(2, size * (w / (cv.getBoundingClientRect().width || w)));
      for (const op of ops) {
        const isErase = op.kind === "erase";
        ctx.globalCompositeOperation = "source-over";
        ctx.strokeStyle = ctx.fillStyle = isErase ? "rgba(0,0,0,.75)" : "rgba(255,64,64,.55)";
        ctx.lineWidth = Math.max(2, op.size || base);
        ctx.lineCap = "round"; ctx.lineJoin = "round";
        if (op.kind === "rect") {
          ctx.beginPath();
          ctx.rect(Math.min(op.x0, op.x1), Math.min(op.y0, op.y1),
                   Math.abs(op.x1 - op.x0), Math.abs(op.y1 - op.y0));
          ctx.fill();
        } else {
          ctx.beginPath();
          op.pts.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
          if (op.pts.length === 1) ctx.lineTo(op.pts[0][0] + .1, op.pts[0][1] + .1);
          ctx.stroke();
        }
      }
    };
    // 导出真正的蒙版：黑底 + 白笔（擦除画黑）
    const buildMask = () => {
      const w = cv.width, h = cv.height;
      const out = document.createElement("canvas");
      out.width = w; out.height = h;
      const o = out.getContext("2d");
      o.fillStyle = "#000"; o.fillRect(0, 0, w, h);
      const base = Math.max(2, size * (w / (cv.getBoundingClientRect().width || w)));
      for (const op of ops) {
        o.strokeStyle = o.fillStyle = op.kind === "erase" ? "#000" : "#fff";
        o.lineWidth = Math.max(2, op.size || base);
        o.lineCap = "round"; o.lineJoin = "round";
        if (op.kind === "rect") {
          o.beginPath();
          o.rect(Math.min(op.x0, op.x1), Math.min(op.y0, op.y1),
                 Math.abs(op.x1 - op.x0), Math.abs(op.y1 - op.y0));
          o.fill();
        } else {
          o.beginPath();
          op.pts.forEach((p, i) => (i ? o.lineTo(p[0], p[1]) : o.moveTo(p[0], p[1])));
          if (op.pts.length === 1) o.lineTo(op.pts[0][0] + .1, op.pts[0][1] + .1);
          o.stroke();
        }
      }
      // 顺带算一下覆盖率，让用户知道"涂够没涂够"
      let on = 0;
      const px = o.getImageData(0, 0, w, h).data;
      for (let i = 0; i < px.length; i += 4 * 7) { if (px[i] > 127) on++; }
      return { url: out.toDataURL("image/png"),
               ratio: on / Math.max(1, (px.length / (4 * 7))) };
    };

    const updateCount = () => {
      const m = buildMask();
      countEl.textContent = "已涂 " + Math.round(m.ratio * 100) + "%";
      if (m.ratio < 0.002 && ops.length) countEl.textContent += "（太少了，再涂一点）";
    };
    const stopDrag = () => {
      if (!drawing) return;
      drawing = false;
      if (cur && cur.kind === "brush" && cur.pts.length === 1) { ops.push(cur); }
      cur = null; paint(); updateCount();
    };
    cv.addEventListener("pointerdown", (ev) => {
      ev.preventDefault();
      cv.setPointerCapture && cv.setPointerCapture(ev.pointerId);
      const [x, y] = toCv(ev);
      drawing = true;
      if (mode === "rect") cur = { kind: ops.length && ev.shiftKey ? "erase" : "rect", x0: x, y0: y, x1: x, y1: y, size: 4 };
      else cur = { kind: mode === "erase" ? "erase" : "brush", size: size * (cv.width / (cv.getBoundingClientRect().width || cv.width)), pts: [[x, y]] };
      paint();
    });
    cv.addEventListener("pointermove", (ev) => {
      if (!drawing || !cur) return;
      const [x, y] = toCv(ev);
      if (cur.kind === "rect") { cur.x1 = x; cur.y1 = y; }
      else cur.pts.push([x, y]);
      paint();
    });
    cv.addEventListener("pointerup", (ev) => {
      if (cur && cur.kind === "brush") ops.push(cur);
      else if (cur && cur.kind === "erase" && cur.pts) ops.push(cur);
      if (cur && cur.kind === "rect") ops.push(cur);
      cur = null; drawing = false; paint(); updateCount();
    });
    cv.addEventListener("pointerleave", stopDrag);

    layer.querySelectorAll(".seg button").forEach((b) => {
      b.onclick = () => {
        mode = b.dataset.mode;
        layer.querySelectorAll(".seg button").forEach((x) => x.classList.toggle("on", x === b));
      };
    });
    layer.querySelector('input[type=range]').oninput = (e) => { size = +e.target.value; };
    const close = () => { layer.remove(); };
    layer.querySelector('[data-act="cancel"]').onclick = close;
    layer.querySelector('[data-act="clear"]').onclick = () => { ops = []; paint(); updateCount(); };
    layer.querySelector('[data-act="ok"]').onclick = () => {
      const m = buildMask();
      if (m.ratio < 0.002) { showToast("还没涂到东西 —— 在图上涂一块再确认", "warn"); return; }
      selMask = m.url.split(",")[1];
      selMaskOn = imgDataUrl;
      refreshMaskChip();
      close();
      showToast("已选好区域，接着说要改成什么（例如「换成深蓝色」）", "ok");
    };
    layer.onclick = (e) => { if (e.target === layer) close(); };
    if (img.complete) init(); else img.onload = init;
  };

  if (maskChip) {
    const editBtn = $("#maskEditBtn"), clearBtn = $("#maskClearBtn");
    if (editBtn) editBtn.onclick = () => {
      if (selMaskOn) openMaskEditor(selMaskOn);
      else showToast("先在图片上点「✂️ 涂抹选区」再来重涂", "warn");
    };
    if (clearBtn) clearBtn.onclick = () => { clearSelMask(); showToast("已清除选区", ""); };
  }
  refreshMaskChip();

  // 给"不支持 fetch 流"的浏览器用的 XHR 增量读取器（2026-11-09）
  // 微信内置浏览器（X5）会把 fetch 的 ReadableStream 整段缓冲，
  // 但 XHR 的 onprogress 是能拿到增量 responseText 的 —— 用它模拟 reader。
  function makeXhrReader(url, headers, bodyStr, onAbortReg) {
    const queue = [];
    let done = false, fail = null, waiter = null, seen = 0;
    const wake = () => { const w = waiter; waiter = null; if (w) w(); };
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url, true);
    Object.keys(headers || {}).forEach((k) => {
      try { xhr.setRequestHeader(k, headers[k]); } catch (e) { /* 忽略 */ }
    });
    xhr.onprogress = () => {
      const all = xhr.responseText || "";
      const chunk = all.slice(seen);
      seen = all.length;
      if (chunk) { queue.push(new TextEncoder().encode(chunk)); wake(); }
    };
    xhr.onload = () => {
      // ⚠️ 2026-11-09：XHR 的 onprogress 不一定把最后一段吐出来（尤其是收尾那一两百字节），
      //   这里在 load 时把 responseText 剩下的部分补一次 —— 否则手机上"最后几个字"会被吞。
      const all = xhr.responseText || "";
      if (all.length > seen) {
        const chunk = all.slice(seen);
        seen = all.length;
        if (chunk) queue.push(new TextEncoder().encode(chunk));
      }
      done = true; wake();
    };
    xhr.onerror = () => { fail = new Error("网络中断"); done = true; wake(); };
    xhr.onabort = () => { done = true; wake(); };
    xhr.send(bodyStr);
    if (typeof onAbortReg === "function") onAbortReg(() => { try { xhr.abort(); } catch (e) {} });
    return {
      read: async () => {
        while (!queue.length && !done) {
          await new Promise((r) => { waiter = r; });
        }
        if (queue.length) return { done: false, value: queue.shift() };
        if (fail) throw fail;
        return { done: true, value: undefined };
      },
    };
  }

  async function doSend(promptText, mediaB64, docPayload) {
    // 用户自己发的（不是系统续写）→ 重置自动续写次数，避免"断两次以后再也不敢续"
    try {
      if (!/上一条回复被截断/.test(String(promptText || ""))) window.__autoCont = 0;
    } catch (e) { /* 忽略 */ }
    history.push({ role: "user", content: promptText });

    // ⚠️⚠️ 2026-11-11 修（致命 bug）：`_prov` 必须声明在 doSend 的**函数作用域**里。
    //
    //   原来的写法是在下面那个 try 块里 `let _prov = ...`（大约第 4344 行）。
    //   `let` 是**块级作用域**，而这个函数里有两处引用在 try 块**之外**：
    //     · finishThinking() 里的第 4281 / 4288 行
    //     · 下面 1.8 秒那个 setTimeout 的第 3363 行
    //   于是它们每次执行都抛 `ReferenceError: _prov is not defined`。
    //
    //   为什么这个错误特别致命（实测复现）：
    //     catch 分支的**第一句**就是 `finishThinking()` —— 一崩就把后面那句
    //     `answerBubble.textContent = "❌ " + err.message` 整个跳过。
    //     后端明明已经返回了清清楚楚的错误原因，界面上却只剩一个 "…"，
    //     用户看到的就是「发出去没有任何反应 / 什么都没输出」。
    //     实测：把 API Key 改成无效值 → 气泡文本恒为 "…"，一个字都不显示。
    //
    //   修法：在这里声明一次，try 里那行改成**只赋值**（那行在锁定区外面，
    //   不触碰任何 🔒 区块）。同时这也让 finishThinking 能显示真实的模型名。
    let _prov = "auto";

    // ---------- 思考过程：默认折叠，但实时刷新状态；展开可见实时全文 ----------
    const thinkWrap = document.createElement("div");
    thinkWrap.className = "msg bot";
    const thinkHdr = document.createElement("div");
    thinkHdr.className = "bubble think-hdr";
    const thinkStatus = document.createElement("span");
    thinkStatus.textContent = "🧠 思考中…";
    // 本地 qwen3-vl 不是推理模型，永远不会有思考 → 别让「思考中…」一直挂着误导人
    setTimeout(() => {
      if (thinking.length === 0 && String(_prov || "").indexOf("deepseek") !== 0) {
        thinkStatus.textContent = "🧠 本地模型不输出思考链";
      }
    }, 1800);
    const thinkArrow = document.createElement("span");
    thinkArrow.className = "arrow";
    thinkArrow.textContent = "▸";
    thinkHdr.appendChild(thinkStatus);
    thinkHdr.appendChild(thinkArrow);
    const thinkBody = document.createElement("div");
    thinkBody.className = "think-body";     // 默认 display:none（折叠）
    // 手机上屏幕窄、可点的地方少 —— 思考过程**默认展开**，让用户真能看到它在想什么
    // 2026-11-12 用户要求「展开/收起要有动画」：不再用 style.display 硬切，
    //   改用 .open 类，高度/透明度由 CSS 过渡（见 style.css 的 .think-body）。
    const _autoThink = window.innerWidth <= 768;
    if (_autoThink) { thinkBody.classList.add("open"); thinkArrow.textContent = "▾"; }
    thinkBody.textContent = "本地模型思考中…";
    thinkWrap.appendChild(thinkHdr);
    thinkWrap.appendChild(thinkBody);
    messagesEl.appendChild(thinkWrap);
    const updateThinkStatus = () => {
      thinkStatus.textContent = thinking
        ? "🧠 思考中…（" + thinking.length + " 字）"
        : "🧠 思考中…";
    };
    thinkHdr.onclick = () => {
      const open = thinkBody.classList.contains("open");
      thinkBody.classList.toggle("open", !open);
      thinkArrow.textContent = open ? "▸" : "▾";
      if (!open) thinkBody.textContent = thinking + "▌";
    };

    // ---------- 工具调用过程（横向小条）----------
    const toolWrap = document.createElement("div");
    toolWrap.className = "tool-strip";
    messagesEl.appendChild(toolWrap);

    // ---------- 生成/读取的媒体 ----------
    const mediaWrap = document.createElement("div");
    mediaWrap.className = "media-strip";
    messagesEl.appendChild(mediaWrap);

    // ---------- 最终回答气泡 ----------
    const answerWrap = document.createElement("div");
    answerWrap.className = "msg bot";
    const answerBubble = document.createElement("div");
    answerBubble.className = "bubble";
    answerBubble.textContent = "…";
    answerWrap.appendChild(answerBubble);
    messagesEl.appendChild(answerWrap);
    // ★ 2026-11-11 用户要求：「思考时，模型的思考框永远要居于最下方，以便我监控思考过程」。
    //   做法：思考期间把思考框挪到消息流末尾，并用 .think-dock 粘住视口底部；
    //   正文一开始输出就挪回原位（工具条之前），免得挡住答案。
    // 2026-11-12：贴不贴底由「🧠 思考框跟随 / 📍 思考框固定」按钮决定（thinkFollow）。
    thinkWrap._home = toolWrap;      // 取消跟随时回到这里（工具条之前）
    // 新一轮开始：上一轮还浮着的思考框一律收回原位，免得在左下角越堆越多
    Array.from(_thinkBoxes).forEach((w) => {
      if (w !== thinkWrap) { undockOne(w, true); _thinkBoxes.delete(w); }
    });
    _thinkBoxes.add(thinkWrap);
    _thinkLive = thinkWrap;          // 登记成「当前这轮的思考框」，按钮切换时能立刻生效
    if (thinkFollow) dockOne(thinkWrap);   // fixed 悬浮，不占消息流高度
    const undockThink = () => {
      if (_thinkLive === thinkWrap) _thinkLive = null;
      // ★ 2026-11-12 用户反馈「点了跟随，思考一会儿又回到原位」：
      //   原来是正文一输出就无条件收回原位 —— 把用户选的「跟随」也一起盖掉了。
      //   现在只有用户选「固定」时才收回；选「跟随」就让它一直浮着，直到下一轮开始。
      if (!thinkFollow) undockOne(thinkWrap);
    };
    scrollBottom();     // 走统一入口：自由查看期间不抢滚动位置
    // 后端可能发来 note（例如"输出被长度上限截断，正在重试"），
    // 收起来备用：万一最终没有正文，就把它显示出来，而不是留一个空气泡
    const notes = [];
    // 联网检索的来源清单：由后端在 ui 事件里单独发来，
    // 在回答下方渲染成可伸缩列表（不再让模型把链接写进正文）
    let sourcesData = null;

    let thinking = "", answer = "";
    // ★ 2026-11-08：流式时也**边到边渲染 Markdown**（节流 ~120ms）。
    //   以前是 textContent 原样贴，用户看到的是「**PPT**」「### 」这种原始符号，
    //   观感像"没有格式化 / 不是流式"。现在每 120ms 重渲染一次，收尾再定稿。
    let _paintAt = 0, _paintTimer = null;
    // ⚠️ 2026-11-08：用户报「小蓝条一直一闪一闪的」= 流没结束但也没内容。
    //   后端可能卡在工具里（比如等一个没人回答的确认框，最长 15 分钟），
    //   这段时间界面看起来"活着"其实什么都没发生。这里加个看门狗：
    //   90 秒没有任何事件 → 移掉光标 + 明确告诉用户，别让人干等。
    let _lastEventAt = Date.now();
    const _t0 = Date.now();
    const _watchdog = setInterval(() => {
      if (!streaming) { clearInterval(_watchdog); return; }
      // 硬上限：5 分钟还没结束 → 直接中止并保留已生成内容
      if (Date.now() - _t0 > 3600000) {   // 硬上限 20 分钟 → 60 分钟
        clearInterval(_watchdog);
        try { abortCtl.abort(); } catch (e) { /* ignore */ }
        answerBubble.querySelectorAll(".stream-cursor").forEach((n) => n.remove());
        showToast("这轮超过 20 分钟还没结束，已自动中断并保留已生成的部分", "warn");
        return;
      }
      if (Date.now() - _lastEventAt < 600000) return;   // 90 秒 → 10 分钟才提示
      clearInterval(_watchdog);
      answerBubble.querySelectorAll(".stream-cursor").forEach((n) => n.remove());
      showToast("已经 90 秒没有任何新内容 —— 模型可能卡在某个工具里了，"
                + "可以点「■ 终止」重来", "warn");
      if (thinkStatus && thinkStatus.textContent.indexOf("思考中") === 0) {
        thinkStatus.textContent = "🧠 卡住了（90 秒无响应）";
      }
    }, 5000);
    const paintAnswer = (force) => {
      const now = Date.now();
      if (!force && now - _paintAt < 120) {
        if (!_paintTimer) {
          _paintTimer = setTimeout(() => {
            _paintTimer = null;
            if (!streaming) return;      // 已经收尾就别再动
            paintAnswer(true);
          }, 130);
        }
        return;
      }
      _paintAt = now;
      // 2026-11-10：流式阶段**不再做 markdown**。
      //   实测：收尾渲染那一步偶尔不执行（异常/分支没走到），屏幕就只剩流式的最后状态；
      //   而流式用的是 mdToHtml，尾部会被吃掉。这里直接 textContent，屏幕 === answer。
      if (answer) undockThink();   // 正文开始输出 → 思考框回归原位
      answerBubble.textContent = answer;
      answerBubble.style.whiteSpace = "pre-wrap";
      const _cur = document.createElement("span");
      _cur.className = "stream-cursor";
      answerBubble.appendChild(_cur);
    };
    // ---------- 思考过程平滑逐字播放 ----------
    // Ollama 对 qwen3-vl 等推理模型常把 thinking 一次性整体返回（非逐 token），
    // 为避免"和回答一起弹出来"，这里把到达的增量拆成小块逐步渲染，
    // 使思考内容在答案输出之前就能持续、实时地逐字增长。
    let thinkPending = "";      // 待逐字播放的增量缓冲
    let thinkTimer = null;
    const pushThinking = (delta) => {
      thinkPending += delta;
      if (thinkTimer === null) {
        thinkTimer = setInterval(() => {
          // ⚠️ 每帧字数**不能写死**。原来固定 5 字/26ms ≈ 190 字/秒，
          // 模型思考一旦比这快，待播队列就越积越长 —— 表现是"思考半天不动、
          // 结束前突然刷一大段"，正是用户说的"不是逐字"。
          // 改成跟着积压量自适应：积压越多每帧吐越多，既保持平滑又**永远不落后**。
          const STEP = Math.max(5, Math.ceil(thinkPending.length / 10));
          if (thinkPending) {
            const take = Math.min(STEP, thinkPending.length);
            thinking += thinkPending.slice(0, take);
            thinkPending = thinkPending.slice(take);
            updateThinkStatus();
            if (thinkBody.classList.contains("open")) thinkBody.textContent = thinking + "▌";
          }
        }, 26);                                  // 帧间隔 ≈ 每帧约 26ms
      }
    };
    const stopThinking = () => {
      if (thinkTimer !== null) { clearInterval(thinkTimer); thinkTimer = null; }
      if (thinkPending) { thinking += thinkPending; thinkPending = ""; }
    };
    const addToolChip = (name) => {
      const chip = document.createElement("span");
      chip.className = "tool-chip";
      chip.textContent = TOOL_LABELS[name] || ("🔧 " + name);
      toolWrap.appendChild(chip);
      chip.scrollIntoView({ block: "nearest" });
      if (name === "generate_image") thinkStatus.textContent = "🎨 正在生成图片…";
      else if (name === "edit_image") thinkStatus.textContent = "🖼 正在微改图片…";
      else if (name === "remember" || name === "search_memory") thinkStatus.textContent = "🧠 正在读写记忆…";
      else if (name === "web_search") thinkStatus.textContent = "🌐 正在联网检索…";
      else thinkStatus.textContent = "🔧 正在调用工具：" + (TOOL_LABELS[name] || name);
    };

    // ---------- 代码卡片：模型跑了什么代码、结果怎样，都能直接看、直接改、直接重跑 ----------
    // 模型在回答里贴的代码块也会用它渲染（见 renderAnswerWithCode）。
    // ---- 自开发改动卡片（2026-11-08）----
    // 用户要求：改了哪个文件、改了什么要能点开看详情，但**不许把页面撑变形**。
    // 做法：默认只显示一行摘要；展开后 diff 在 max-height 320px 的容器里滚动。
    // ---------- 代码改动汇总列表（2026-11-10）----------
    // ⚠️ 用户原话：「每次修改代码不要一份一份往下堆，这样我看不到你的思考过程，
    //    我想要你把更改的代码像列表一样收起与展开，就跟生成文库那样差不多」。
    // 以前：每改一个文件就往聊天里追加一张 sd-card，一轮改 6 个文件就是 6 张卡，
    //      把正文和思考都挤到屏幕外。现在：本轮所有改动收进**一个**折叠列表 ——
    //      列表头「🛠 本轮代码改动 · N 处 · +X −Y」可整体收起；
    //      列表里每个文件只占一行（类型 + 路径 + 增删数），点这一行才展开 diff。
    let selfdevList = null;
    const ensureSelfdevList = () => {
      if (selfdevList && selfdevList.parentNode) return selfdevList;
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
      answerWrap.appendChild(box);
      selfdevList = box;
      return box;
    };
    const refreshSelfdevSum = (box) => {
      const rows = box.__body.querySelectorAll(".sd-row");
      let add = 0, del = 0;
      rows.forEach((r) => {
        add += Number(r.dataset.add || 0);
        del += Number(r.dataset.del || 0);
      });
      const sum = box.__head.querySelector(".sd-list-sum");
      sum.innerHTML = rows.length + ' 处 · <span class="sd-add">+' + add +
        '</span> <span class="sd-del">−' + del + "</span>";
    };

    // 单个文件一行：点这一行才展开它的 diff（原 sd-card 的详情逻辑原样保留）
    const makeSelfdevCard = (ui) => {
      const box = ensureSelfdevList();
      const wrap = document.createElement("div");
      wrap.className = "sd-row";
      wrap.dataset.add = ui.added || 0;
      wrap.dataset.del = ui.removed || 0;
      const actName = ui.act === "apply" ? "代码模型重写"
        : (ui.act === "patch" ? "局部替换" : "写入");
      const rel = String(ui.rel || "");
      const add = ui.added || 0;
      const del = ui.removed || 0;

      const head = document.createElement("div");
      head.className = "sd-head";
      head.innerHTML =
        '<span class="sd-badge">🛠 ' + actName + '</span>' +
        '<span class="sd-rel" title="' + rel.replace(/"/g, "&quot;") + '">' + rel + '</span>' +
        '<span class="sd-stat"><span class="sd-add">+' + add +
        '</span> <span class="sd-del">−' + del + '</span></span>' +
        '<button class="sd-toggle" type="button">详情 ▾</button>';
      wrap.appendChild(head);

      const body = document.createElement("div");
      body.className = "sd-body";
      body.hidden = true;
      const pre = document.createElement("pre");
      pre.className = "sd-pre";
      // 给 diff 上色（只做行首判断，不解析内容）
      const diffText = String(ui.diff || "（没有差异内容）");
      pre.innerHTML = diffText.split("\n").map((ln) => {
        const esc = ln.replace(/&/g, "&amp;").replace(/</g, "&lt;");
        if (ln.startsWith("+++") || ln.startsWith("---") || ln.startsWith("@@"))
          return '<span class="l-hdr">' + esc + "</span>";
        if (ln.startsWith("+")) return '<span class="l-add">' + esc + "</span>";
        if (ln.startsWith("-")) return '<span class="l-del">' + esc + "</span>";
        return esc;
      }).join("\n");
      body.appendChild(pre);

      const foot = document.createElement("div");
      foot.className = "sd-foot";
      foot.innerHTML =
        (ui.who ? '<span>模型：' + ui.who + '</span>' : "") +
        (ui.backup ? '<span>备份：' + ui.backup + '</span>' : "") +
        (ui.is_new ? '<span>（新文件，无改前版本）</span>' : "");
      if (ui.backup) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.textContent = "↩ 回撤这个文件";
        btn.onclick = async () => {
          btn.disabled = true;
          try {
            const r = await (await fetch("/api/selfdev/rollback", {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ rel: rel, backup: ui.backup }) })).json();
            if (r.ok) {
              btn.textContent = "✅ 已回撤";
              showToast("已回撤 " + rel + "（刷新后生效）", "ok");
            } else {
              btn.textContent = "↩ 回撤这个文件";
              btn.disabled = false;
              showToast("回撤失败：" + (r.error || r.msg || ""), "err");
            }
          } catch (e) {
            btn.disabled = false;
            showToast("回撤失败：" + e.message, "err");
          }
        };
        foot.appendChild(btn);
      }
      body.appendChild(foot);
      wrap.appendChild(body);

      const tg = head.querySelector(".sd-toggle");
      // 抽成一个函数：小按钮和整行都调它。
      // ⚠️ 不要用 tg.click() —— 某些嵌入环境里元素没有 click()，会直接抛异常
      const toggleBody = () => {
        body.hidden = !body.hidden;
        tg.textContent = body.hidden ? "详情 ▾" : "收起 ▴";
      };
      tg.onclick = toggleBody;
      head.onclick = (ev) => { if (ev.target !== tg) toggleBody(); };

      box.__body.appendChild(wrap);
      refreshSelfdevSum(box);
      // 整列被收起时来了新改动 → 自动展开，免得用户以为"没动静"
      if (box.classList.contains("collapsed")) {
        box.classList.remove("collapsed");
        box.__body.hidden = false;
        box.__head.querySelector(".sd-list-arrow").textContent = "▾";
      }
      return wrap;
    };

    // ---- 代码卡片分组：一次跑很多段代码时，不让卡片一张张往下堆 ----
    // 2026-11-11 用户反馈：模型一轮里连着跑好几段代码，每段一张大卡片，
    // 聊天区被这些提示塞满、翻不到正文。现在**连续的**代码卡片自动收进同一个
    // 分组，分组头只占一行「🐍 本地执行的代码 · N 段」，点一下整组收起/展开。
    const codeGroupOf = (parent) => {
      const last = parent.lastElementChild;
      if (last && last.classList && last.classList.contains("code-group")) return last;
      const g = document.createElement("div");
      g.className = "code-group";
      const head = document.createElement("div");
      head.className = "code-group-head";
      // 用 createElement 逐个建，而不是 innerHTML 拼串：可读、可控、便于自测
      const ttl = document.createElement("span");
      ttl.className = "cg-title";
      ttl.textContent = "🐍 本地执行的代码";
      const cntEl = document.createElement("span");
      cntEl.className = "cg-count";
      const btn = document.createElement("button");
      btn.className = "btn sm ghost cg-toggle";
      btn.type = "button";
      btn.textContent = "▴ 全部收起";
      head.appendChild(ttl);
      head.appendChild(cntEl);
      head.appendChild(btn);
      const body = document.createElement("div");
      body.className = "code-group-body";
      g.appendChild(head);
      g.appendChild(body);
      g.__body = body;
      g.__cnt = cntEl;
      // ⚠️ 和 sd-card 一样：不要用 btn.click() —— 某些嵌入环境里元素没有 click()
      const sync = () => {
        const collapsed = g.classList.contains("collapsed");
        body.hidden = collapsed;
        btn.textContent = collapsed ? "▾ 展开全部" : "▴ 全部收起";
      };
      const toggle = () => { g.classList.toggle("collapsed"); sync(); };
      btn.onclick = (ev) => { ev.stopPropagation(); toggle(); };
      head.onclick = toggle;
      sync();
      parent.appendChild(g);
      return g;
    };
    const appendCodeCard = (parent, card) => {
      const g = codeGroupOf(parent);
      g.__body.appendChild(card);
      if (g.__cnt) g.__cnt.textContent = "· " + g.__body.querySelectorAll(".code-card").length + " 段";
      return card;
    };

    const makeCodeCard = (ui, title) => {
      const card = document.createElement("div");
      card.className = "code-card";
      const risk = (ui.risky || []).length
        ? `<span class="code-risk" title="这些操作已经过你批准">⚠ ${escapeHtml((ui.risky || []).join("、"))}</span>`
        : "";
      const meta = [];
      if (ui.seconds) meta.push("⏱ " + ui.seconds + "s");
      if (ui.rc === 0) meta.push("退出码 0");
      else if (ui.rc !== null && ui.rc !== undefined) meta.push("退出码 " + ui.rc);
      card.innerHTML =
        `<div class="code-head"><span class="code-title">${escapeHtml(title || "🐍 本地执行的代码")}</span>${risk}` +
        `<span class="code-meta">${escapeHtml(meta.join(" · "))}</span>` +
        `<span class="code-btns"><button class="btn sm ghost code-ws" title="直接写进开发台当前项目，并立刻在编辑器里打开 —— 不用手动复制粘贴">📝 写进开发台</button>` +
        `<button class="btn sm ghost code-edit">✏ 编辑</button>` +
        `<button class="btn sm ghost code-run">▶ 运行</button>` +
        `<button class="btn sm ghost code-save">💾 存到文库</button>` +
        `<button class="btn sm ghost code-copy">📋 复制</button></span>` +
        // ★ 2026-11-11 用户要求：卡片默认缩成「一条」，折叠时右侧只显示这个提示
        `<span class="code-open-hint">▸ 展开</span></div>` +
        `<pre class="code-body"><code></code></pre><div class="code-out"></div>` +
        // 运行中才显示：给程序喂标准输入（程序里有 input() 时全靠它）
        `<div class="code-stdin" hidden><input class="code-stdin-in" spellcheck="false" ` +
        `placeholder="程序在等输入的话，在这里打字后回车（一行一次）">` +
        `<button class="btn sm ghost code-stdin-send">发送</button></div>`;
      const codeEl = card.querySelector(".code-body code");
      const wsBtn = card.querySelector(".code-ws");
      const editBtn = card.querySelector(".code-edit");
      const runBtn = card.querySelector(".code-run");
      const saveBtn = card.querySelector(".code-save");
      const copyBtn = card.querySelector(".code-copy");
      const outEl = card.querySelector(".code-out");
      const stdinRow = card.querySelector(".code-stdin");
      const stdinIn = card.querySelector(".code-stdin-in");
      const stdinSend = card.querySelector(".code-stdin-send");
      let current = ui.code || "";
      codeEl.textContent = current;

      // ★ 长代码**默认折叠**（用户要求：不要把整段代码摊在聊天里）
      //   短代码（≤12 行且 ≤700 字）照旧直接显示，免得为三五行还要点一下。
      const _lines = current.split("\n").length;
      // ★ 2026-11-08 用户要求：「默认收起，我自己决定展开还是收缩」。
      //   原来是 >12 行才折，结果模型一次产出好几段短代码，每段都摊开，
      //   屏幕瞬间被占满。现在**一律默认收起**，点一下才展开。
      let _collapsed = true;
      const _tg = document.createElement("button");
      _tg.className = "btn sm ghost code-toggle";
      _tg.type = "button";
      // ★ 2026-11-11 用户要求：「代码折叠框除了上方有折叠按钮，下方也需要有」。
      //   原因：长代码展开后要滚回卡片顶部才够得着收起按钮，很麻烦。
      //   底部这个与顶部共用同一个切换函数，文案跟着状态同步。
      let _tg2 = null;
      const _syncTg = () => {
        card.classList.toggle("collapsed", _collapsed);
        _tg.textContent = _collapsed
          ? "▸ 展开代码（" + _lines + " 行 / " + current.length + " 字）"
          : "▾ 收起代码";
        if (_tg2) _tg2.textContent = _collapsed ? "▸ 展开代码" : "▾ 收起代码";
      };
      // ★ 2026-11-12：原来靠 CSS 的 max-height 0 ↔ 460px 过渡，短代码从 0 到实际
      //   高度只用了 460px 里的前一小段，视觉上就是「啪」一下弹开。改成量真实高度补间。
      const _toggle = () => animHeight(card, () => { _collapsed = !_collapsed; _syncTg(); });
      _tg.onclick = _toggle;
      const _btns = card.querySelector(".code-btns");
      if (_btns) _btns.prepend(_tg);
      // ★ 2026-11-11 简化：删掉底部重复的「展开/收起代码」按钮。
      //   原来一张卡最多挂 7 个按钮，很乱；现在底部只留一个「收起整张卡片」。

      // ★ 2026-11-11 用户要求：除了在下方折叠「代码」，还要能折叠「整张卡片」这个框。
      //   场景：长代码展开后卡片很高，想把它整个收成一行，聊天区立刻清爽。
      //   与上面的代码折叠是两套独立状态：整框收起时保留代码的折叠/展开记忆。
      // ★ 2026-11-11 用户要求：「收缩到只剩一条」—— 所以默认就是整框收起状态，
      //   聊天里每段代码只占一行，要看内容再点开。
      let _cardCollapsed = true;
      const _tg3 = document.createElement("button");
      _tg3.className = "btn sm ghost code-card-toggle code-card-toggle-b";
      _tg3.type = "button";
      _tg3.title = "收起 / 展开整张代码卡片（只保留标题行）";
      const _syncCard = () => {
        card.classList.toggle("card-collapsed", _cardCollapsed);
        _tg3.textContent = _cardCollapsed ? "▾ 展开整张卡片" : "▴ 收起整张卡片";
      };
      const _flipCard = () => animHeight(card, () => { _cardCollapsed = !_cardCollapsed; _syncCard(); });
      _tg3.onclick = _flipCard;
      card.appendChild(_tg3);
      // 收起状态下整张卡只剩标题行 —— 点这一行也能展开，不用去找按钮
      const _chead = card.querySelector(".code-head");
      if (_chead) {
        _chead.onclick = (ev) => {
          if (!_cardCollapsed) return;                    // 展开时标题行里的按钮照常可用
          if (ev.target && ev.target.closest && ev.target.closest(".code-btns")) return;
          _flipCard();
        };
      }
      _syncCard();
      _syncTg();

      const paintOut = (r) => {
        const bits = [];
        if (r.out) bits.push(`<div class="code-part"><b>输出</b><pre>${escapeHtml(r.out)}</pre></div>`);
        if (r.err) bits.push(`<div class="code-part err"><b>${r.rc ? "报错" : "提示"}</b><pre>${escapeHtml(r.err)}</pre></div>`);
        if (!r.out && !r.err) bits.push('<div class="code-part empty">（没有输出 —— 代码里要用 print() 打印结果）</div>');
        if (r.seconds !== undefined && r.seconds !== null) bits.push(`<div class="code-time">耗时 ${r.seconds}s</div>`);
        outEl.innerHTML = bits.join("");
      };
      if (ui.out || ui.err) paintOut(ui);

      // 编辑：把 <pre> 换成 textarea（所见即所得，改完直接跑）
      let editing = false;
      let area = null;
      editBtn.onclick = () => {
        // ⚠️ 替换的父节点要用 card，**不能用 pre 自己**：
        // `<pre>` 本身就是 .code-body，在它自己身上 querySelector(".code-body")
        // 只会搜子元素、返回 null，replaceChild(x, null) 会抛异常，
        // 结果就是"点了编辑按钮毫无反应"（踩过）。
        const pre = card.querySelector(".code-body");
        if (!editing) {
          area = document.createElement("textarea");
          area.className = "code-edit-area";
          area.spellcheck = false;
          area.value = current;
          card.replaceChild(area, pre);
          editing = true;
          editBtn.textContent = "✔ 完成";
          area.focus();
        } else {
          current = area.value;
          const np = document.createElement("pre");
          np.className = "code-body";
          const c = document.createElement("code");
          c.textContent = current;
          np.appendChild(c);
          card.replaceChild(np, area);
          editing = false;
          editBtn.textContent = "✏ 编辑";
        }
      };
      const grab = () => (editing && area ? area.value : current);

      // ---- ▶ 运行：**流式**跑，不设时限 ----
      // 为什么不用 /api/code/run：那个是同步的（跑完才出字）而且有硬性时限 ——
      // 计时器、服务器、番茄钟这类**本来就要一直跑**的程序，点运行只会等来
      // 一句"执行超过 25 秒，已被强制中止"，看起来像程序坏了（实测用户就是这么被卡住的）。
      // 现在：边跑边出字、不设时限、随时能停。
      //
      // 进度条/倒计时用的是 `print(x, end='\r')` —— 一个换行都没有。
      // 所以显示前要把同一行里的 \r 折叠掉，否则一屏都是 "25:00 24:59 24:58…"。
      const foldCR = (s) => s.split("\n").map((ln) => {
        if (ln.indexOf("\r") === -1) return ln;
        const parts = ln.split("\r").filter((x) => x !== "");
        return parts.length ? parts[parts.length - 1] : "";
      }).join("\n");
      let runningId = null;
      // 往正在跑的程序里送一行标准输入（程序里有 input() 时靠它）
      const sendStdin = async () => {
        if (!runningId) { showToast("现在没有正在跑的程序", "warn"); return; }
        const v = stdinIn.value;
        stdinIn.value = "";
        const rid = runningId;
        try {
          const r = await api("/api/ws/run_input",
            { method: "POST", body: JSON.stringify({ id: rid, data: v }) });
          if (r && r.ok === false) showToast(r.error || "输入送不进去", "warn");
        } catch (e) {
          showToast("输入送不进去：" + String(e.message || e), "warn");
        }
      };
      stdinSend.onclick = sendStdin;
      stdinIn.onkeydown = (e) => {
        // 回车就发；程序里一次 input() 读一行，所以这里不要自动补内容
        if (e.key === "Enter") { e.preventDefault(); sendStdin(); }
      };
      runBtn.onclick = async () => {
        if (runningId) {                 // 再点一次 = 停止
          const rid = runningId;
          runningId = null;
          runBtn.textContent = "停止中…";
          runBtn.disabled = true;
          try { await api("/api/ws/run_stop", { method: "POST", body: JSON.stringify({ id: rid }) }); }
          catch (e) {}
          runBtn.disabled = false;
          runBtn.textContent = "▶ 运行";
          runBtn.title = "";
          stdinRow.hidden = true;
          return;
        }
        const code = grab();
        if (!code.trim()) { showToast("代码是空的", "warn"); return; }
        // 兜底：万一漏进来的是「内部工具调用」那坨 JSON（不是代码），别拿去跑 ——
        // 那只会得到 `SyntaxError: '{' was never closed`，把人吓一跳。
        if (looksLikeToolCall(ui.lang || "", code)) {
          showToast("这段不是代码，是一次内部工具调用（已折叠），不用运行它", "warn");
          return;
        }
        current = code;
        const rid = "card-" + Date.now();
        runningId = rid;
        runBtn.textContent = "■ 停止";
        runBtn.title = "点它就能随时停掉正在跑的程序";
        stdinRow.hidden = false;         // 运行中显示输入框：带 input() 的程序靠它
        let raw = "";
        const paintLive = () => {
          outEl.innerHTML = `<div class="code-part"><b>运行中…</b><pre>${escapeHtml(foldCR(raw))}</pre></div>`;
          outEl.scrollTop = outEl.scrollHeight;
        };
        outEl.innerHTML = '<div class="code-part">正在启动…</div>';
        try {
          const resp = await fetch("/api/code/run_stream", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ code, id: rid }) });
          if (!resp.ok) throw new Error(resp.statusText || ("HTTP " + resp.status));
          const reader = resp.body.getReader();
          const dec = new TextDecoder();
          let buf = "";
          while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buf += dec.decode(value, { stream: true });
            const lines = buf.split("\n"); buf = lines.pop();
            for (const line of lines) {
              if (!line.trim()) continue;
              let o; try { o = JSON.parse(line); } catch { continue; }
              if (o.t === "out") { raw += o.data; paintLive(); }
              else if (o.t === "end") {
                if (!o.ok) throw new Error(o.error || "启动失败");
                paintOut({ out: foldCR(raw), rc: o.rc, seconds: o.seconds });
              }
            }
          }
          if (!raw) outEl.innerHTML = '<div class="code-part empty">（没有输出 —— 代码里要用 print() 打印结果）</div>';
        } catch (e) {
          outEl.innerHTML =
            `<div class="code-part err">运行失败：${escapeHtml(String(e.message || e))}</div>`;
        } finally {
          if (runningId === rid) runningId = null;
          runBtn.textContent = "▶ 运行";
          runBtn.title = "";
          stdinRow.hidden = true;
        }
      };
      // 存到生成文库：代码模型那一轮没有工具，保存只能靠这个按钮。
      // 后端会按语言挑后缀、优先用用户点名的文件名（"就叫 stats.py"）。
      saveBtn.onclick = async () => {
        const code = grab();
        if (!code.trim()) { showToast("代码是空的", "warn"); return; }
        current = code;
        saveBtn.disabled = true;
        try {
          const r = await api("/api/code/save", {
            method: "POST",
            body: JSON.stringify({ code, language: ui.lang || "", user_text: ui.userText || "" }),
          });
          showToast("已存入生成文库：" + (r.rel || ""), "ok");
          // 面板可能正开着：刷新一下列表（不传参，别改当前选中项）
          try { if (typeof loadDoclib === "function") loadDoclib(); } catch (e) {}
        } catch (e) {
          showToast("保存失败：" + String(e.message || e), "warn");
        } finally {
          saveBtn.disabled = false;
        }
      };
      copyBtn.onclick = async () => {
        try {
          await navigator.clipboard.writeText(grab());
          showToast("代码已复制", "ok");
        } catch (e) {
          showToast("复制失败，请手动选中", "warn");
        }
      };
      // 写进开发台：一步落盘到**当前工作区项目**并立刻在编辑器里打开。
      // 为什么要有它：开发台的 workspace_write 只有模型自己能调，
      // 模型在聊天里贴的代码块是纯文本 —— 原来只能「📋 复制」再手动粘进编辑器，
      // 等于把 AI 的输出又搬一遍。文件名优先用用户点名的那个（"就叫 stats.py"）。
      wsBtn.onclick = async () => {
        const code = grab();
        if (!code.trim()) { showToast("代码是空的", "warn"); return; }
        current = code;
        if (!window.Studio || !window.Studio.reveal) {
          showToast("开发台还没就绪，稍后再试", "warn"); return;
        }
        wsBtn.disabled = true;
        const oldText = wsBtn.textContent;
        wsBtn.textContent = "写入中…";
        try {
          const r = await api("/api/ws/code", {
            method: "POST",
            body: JSON.stringify({ code, language: ui.lang || "",
                                   user_text: ui.userText || "" }),
          });
          showToast((r.overwrote ? "已覆盖并写进开发台：" : "已写进开发台：") + r.rel, "ok");
          await window.Studio.reveal(r.rel);
        } catch (e) {
          showToast("写入失败：" + String(e.message || e), "warn");
        } finally {
          wsBtn.disabled = false;
          wsBtn.textContent = oldText;
        }
      };
      return card;
    };

    /* AI 真的把文件写到磁盘上、而开发台没开着时给的提示卡片。
       为什么不直接弹开发台：用户可能只是在聊天里问问题，AI 顺手存了个文件，
       整页盖上去会很打扰。但也不能什么都不说 —— 那样用户根本不知道文件在哪，
       只能去聊天里复制粘贴。所以给一张卡片，点一下就到。 */
    const makeWsCard = (ui) => {
      const card = document.createElement("div");
      card.className = "ws-card";
      card.innerHTML =
        `<span class="ws-ico">📄</span>` +
        `<span class="ws-txt">AI 写入了 <b>${escapeHtml(ui.rel)}</b>` +
        (ui.chars ? `（${ui.chars} 字）` : "") + `</span>` +
        `<button class="btn sm ghost ws-open">在开发台打开</button>`;
      const go = async () => {
        if (window.Studio && window.Studio.reveal) {
          await window.Studio.reveal(ui.rel);
          card.classList.add("done");
        } else showToast("开发台还没就绪，稍后再试", "warn");
      };
      card.querySelector(".ws-open").onclick = (e) => { e.stopPropagation(); go(); };
      card.onclick = go;
      return card;
    };

    // 回答里的**站内下载链接**渲染成可点按钮（实现在文件上方 addMsg 附近，
    // 那里是顶层函数声明，历史恢复和实时回答两条路径都能用）。
    // ⚠️ 回答里的"代码块"未必都是代码。
    // 代码模型那一轮用的是**文本协议**：它会把工具调用写成一个 ```tool 块塞在正文里
    // （`{"name": "workspace_write", "arguments": {...}}`），后端会把它们摘掉；
    // 万一漏了一个（实测：模型把 JSON 的最后一个 `}` 漏了 → 后端解析失败 → 块留在正文里），
    // 这里就是最后一道闸 —— 否则那段 JSON 会变成一张"可编辑可运行"的代码卡片，
    // 用户点「▶ 运行」得到 `SyntaxError: '{' was never closed`，
    // 而且会看到好几张一模一样的卡片，完全不知道发生了什么。
    // ⚠️ 用**函数声明**而不是 const 箭头函数：后者在初始化之前被调用会抛
    // TDZ ReferenceError（而历史会话回放可能发生在初始化之前）。函数声明会提升。
    function looksLikeToolCall(lang, body) {
      if (/^(tool|tool_call|tool-call)$/i.test(String(lang || "").trim())) return true;
      const s = String(body || "").trim();
      return /^\{\s*"(name|tool|function)"\s*:/.test(s)
        && /"(arguments|parameters|args|input)"\s*:/.test(s);
    }

    const renderAnswerWithCode = (bubble, text, userText) => {
    // 这条路径也走 mdToHtml，产物链接同样收集起来
    if (window.__Artifacts) window.__Artifacts.begin();
      text = stripToolLeak(text);          // 先摘掉泄漏的工具调用（见 stripToolLeak）
      const parts = [];
      const re = /```([a-zA-Z0-9_+#.-]*)[ \t]*\n([\s\S]*?)```/g;
      let last = 0, m;
      while ((m = re.exec(text)) !== null) {
        if (m.index > last) parts.push({ t: "text", v: text.slice(last, m.index) });
        if (looksLikeToolCall(m[1], m[2])) {
          parts.push({ t: "raw" });          // 内部工具调用：不渲染成代码卡片
        } else {
          parts.push({ t: "code", lang: m[1] || "", v: m[2].replace(/\n$/, "") });
        }
        last = m.index + m[0].length;
      }
      if (last < text.length) parts.push({ t: "text", v: text.slice(last) });
      if (!parts.some((p) => p.t === "code")) { bubble.textContent = text; return; }
      bubble.textContent = "";
      parts.forEach((p) => {
        if (p.t === "text") {
          if (!p.v.trim()) return;
          const d = document.createElement("div");
          d.className = "md-text";
          d.innerHTML = mdToHtml(p.v);      // ← 结构化，而不是原样贴
          bubble.appendChild(d);
        } else if (p.t === "raw") {
          const d = document.createElement("div");
          d.className = "md-text muted";
          d.textContent = "（这里原本是一次内部工具调用，已折叠）";
          bubble.appendChild(d);
        } else {
          appendCodeCard(bubble, makeCodeCard(
            { code: p.v, lang: p.lang, userText: userText || "" },
            "📄 代码（可编辑后直接运行）"));
        }
      });
    };

    // 危险操作确认：模型要删文件/起进程/联网时，**先问一句**而不是直接拒绝
    const showConfirm = (ui) => {
      if (document.querySelector(".confirm-layer")) return;   // 同一时刻只弹一个
      const layer = document.createElement("div");
      layer.className = "confirm-layer";
      layer.innerHTML =
        '<div class="confirm-box">' +
        '<div class="confirm-title">⚠️ 这次操作需要你确认</div>' +
        `<div class="confirm-reason">${escapeHtml(ui.reason || "模型请求执行一段有风险的代码")}</div>` +
        '<pre class="confirm-code"><code></code></pre>' +
        '<div class="confirm-tip">批准后会在本机真实执行（临时目录、有运行时限）。不确定就别点允许。</div>' +
        // 「本次任务不再询问」：只对这一轮对话生效（后端每轮清零），不是永久授权
        '<label class="confirm-remember"><input type="checkbox" id="cfRemember">' +
        '<span>本次任务不再询问（本轮剩余操作自动确认）</span></label>' +
        '<div class="confirm-btns"><button class="btn ghost" data-act="deny">拒绝</button>' +
        '<button class="btn primary" data-act="allow">允许执行</button></div></div>';
      layer.querySelector("code").textContent = ui.code || "";
      document.body.appendChild(layer);
      const answer = async (allow) => {
        const remember = !!(layer.querySelector("#cfRemember") || {}).checked;
        layer.querySelectorAll("button").forEach((b) => { b.disabled = true; });
        try {
          const r = await api("/api/tool/confirm", {
            method: "POST",
            body: JSON.stringify({ id: ui.id, allow, remember }) });
          showToast(
            allow
              ? (remember ? "已允许，本轮后续操作不再询问" : "已允许执行")
              : "已拒绝",
            allow ? "ok" : "");
        } catch (e) {
          showToast("回复失败：" + String(e.message || e), "warn");
        } finally {
          layer.remove();
        }
      };
      layer.querySelector('[data-act="deny"]').onclick = () => answer(false);
      layer.querySelector('[data-act="allow"]').onclick = () => answer(true);
    };

    // 输入框弹层在文件顶部（最外层作用域）—— 文库面板要用它，
    // 而那段代码在独立的 IIFE 里，放到这里它访问不到（踩过）。

    // 问答框：材料不足时模型可以在这里问细节。用户填完它接着做，
    // 不用把需求重新描述一遍 —— 这是"写得像你要的"和"瞎猜一篇"的区别。
    const showAskDialog = (ui) => {
      if (document.querySelector(".ask-layer")) return;
      const qs = ui.questions || [];
      if (!qs.length) return;
      // title / hint 可以由后端指定 —— 比如"请填高德 key"那种场景，
      // 用默认那句"想先跟你确认几个细节"就对不上了。
      const title = ui.title || "💬 想先跟你确认几个细节";
      const hint = ui.hint || "补充下面的信息，写出来才贴你的要求。不想答的直接留空跳过。";
      const layer = document.createElement("div");
      layer.className = "confirm-layer ask-layer" + (ui.freeInput ? " ask-free-mode" : "");
      layer.innerHTML =
        '<div class="confirm-box">' +
        `<div class="confirm-title">${escapeHtml(title)}</div>` +
        `<div class="confirm-reason">${escapeHtml(hint).replace(/\n/g, "<br>")}</div>` +
        '<div class="ask-list"></div>' +
        '<div class="confirm-btns">' +
        `<button class="btn ghost" data-act="skip">${ui.freeInput ? "不填，跳过" : "跳过，按你的理解写"}</button>` +
        `<button class="btn primary" data-act="send">${ui.freeInput ? "保存并连接" : "提交，继续"}</button></div></div>`;
      const list = layer.querySelector(".ask-list");
      const items = [];
      qs.forEach((q, i) => {
        const box = document.createElement("div");
        box.className = "ask-item";
        const type = q.multi ? "checkbox" : "radio";
        const opts = (q.options || []).map((o) =>
          `<label class="ask-opt"><input type="${type}" name="ask${i}" value="${escapeHtml(o)}">` +
          `<span>${escapeHtml(o)}</span></label>`).join("");
        box.innerHTML = `<div class="ask-q">${ui.freeInput ? "" : (i + 1) + ". "}${escapeHtml(q.question)}</div>` +
          (opts ? `<div class="ask-opts">${opts}</div>` : "") +
          `<input class="ask-free" type="text" placeholder="${opts ? "也可以自己写…" : "在这里回答"}" />`;
        list.appendChild(box);
        items.push(box);
      });
      document.body.appendChild(layer);

      const finish = async (skip) => {
        layer.querySelectorAll("button").forEach((b) => { b.disabled = true; });
        const answers = [];
        items.forEach((box, i) => {
          const checked = [...box.querySelectorAll("input[type=radio]:checked, input[type=checkbox]:checked")]
            .map((el) => el.value);
          const free = (box.querySelector(".ask-free").value || "").trim();
          const merged = [checked.join("、"), free].filter(Boolean).join("；");
          answers.push({ question: qs[i].question, answer: skip ? "" : merged });
        });
        try {
          await api("/api/tool/answer", {
            method: "POST", body: JSON.stringify({ id: ui.id, answers }) });
          showToast(skip ? "已跳过，让它自己决定" : "已提交，它继续写", skip ? "" : "ok");
        } catch (e) {
          showToast("提交失败：" + String(e.message || e), "warn");
        } finally {
          layer.remove();
        }
      };
      layer.querySelector('[data-act="skip"]').onclick = () => finish(true);
      layer.querySelector('[data-act="send"]').onclick = () => finish(false);
      layer.querySelector(".ask-free") && layer.querySelector(".ask-free").focus();
    };
    const addMedia = (ui) => {
      const isWeb = ui.origin === "web";
      const card = document.createElement("div");
      card.className = "media-card";
      const mime = ui.mime || "image/png";
      const b64 = ui.b64 || "";
      // 明确区分「网上搜到的」「AI 生成的」「AI 微改的」，避免混淆
      const badge = isWeb ? "🌐 网上搜到的"
        : (ui.origin === "gen" ? "🎨 AI 生成"
        : (ui.origin === "edit" ? "🎨 AI 微改" : "🖼️ 图片"));
      const caption = (ui.prompt || "").slice(0, 60);
      card.innerHTML = `
        <div class="media-cap"><span class="badge ${isWeb ? "web" : "gen"}">${badge}</span>${
          caption ? " " + caption : ""}</div>
        <img src="data:${mime};base64,${b64}">
        <div class="media-meta">${
          ui.source ? `<a href="${ui.source}" target="_blank" rel="noopener">来源页</a> · ` : ""}${
          ui.device ? "🖥 " + ui.device + " " : ""}${ui.cost_s ? "⏱ " + ui.cost_s + "s" : ""}${
          ui.size ? " · " + ui.size : ""}</div>
        <div class="media-actions">${
          (ui.saved && ui.saved.id)
            ? `<span class="badge gen" title="${(ui.saved.path || "").replace(/"/g, "&quot;")}">✅ 已存图库</span>`
            : `<button class="btn sm ghost lib-save">⭐ 保存到图库</button>`}
          <button class="btn sm ghost mask-pick" title="在这张图上涂一下，之后只改涂过的地方（比整图微改准得多）">✂️ 涂抹选区</button></div>`;
      card.querySelector("img").onclick = (e) => {
        e.stopPropagation();
        showLightbox(mime, b64, badge + (caption ? "：" + caption : ""));
      };
      if (ui.saved && ui.saved.id && typeof loadLibrary === "function") {
        setTimeout(loadLibrary, 30);
      }
      const pick = card.querySelector(".mask-pick");
      if (pick) pick.onclick = (e) => {
        e.stopPropagation();
        openMaskEditor("data:" + mime + ";base64," + b64);
      };
      const btn = card.querySelector(".lib-save");
      if (btn) btn.onclick = async (e) => {
        e.stopPropagation();
        btn.disabled = true;
        btn.textContent = "保存中…";
        try {
          const r = await api("/api/library/images", {
            method: "POST",
            body: JSON.stringify({
              b64, name: caption.slice(0, 30),
              source: ui.source || ui.url || "",
              origin: ui.origin || "web",
            }),
          });
          if (r && r.ok) {
            btn.textContent = "✅ 已保存";
            loadLibrary();
            showToast("已保存到图片库", "ok");
          } else {
            btn.textContent = "⭐ 保存到图库";
            btn.disabled = false;
            showToast("保存失败：" + ((r && r.error) || "未知错误"), "warn");
          }
        } catch (err) {
          btn.textContent = "⭐ 保存到图库";
          btn.disabled = false;
          showToast("保存失败：" + err.message, "warn");
        }
      };
      mediaWrap.appendChild(card);
      messagesEl.scrollTop = messagesEl.scrollHeight;
    };
    const finishThinking = () => {
      stopThinking();                            // 结束播放，一次性补齐剩余思考
      undockThink();                             // 思考结束 → 思考框回归原位
      // 手机端：思考是从头展开显示的；收尾时如果太长就折起来，只留字数
      if (_autoThink && thinking.length > 500 && thinkBody.classList.contains("open")) {
        thinkBody.classList.remove("open");
        thinkArrow.textContent = "▸";
      }
      // ⚠️ 2026-11-08：原来无论什么模型都显示「未输出思考」，
      //   用户看到会以为坏了。其实**本地 qwen3-vl 不是推理模型，压根没有思考链**，
      //   只有 DeepSeek V4.1（已默认开思考）/ 推理模型才会有。
      const _isDSNow = String(_prov || "").indexOf("deepseek") === 0;
      thinkStatus.textContent = thinking
        ? "🧠 思考过程（" + thinking.length + " 字）"
        : (_isDSNow ? "🧠 本次没有思考内容" : "🧠 本地模型不输出思考链");
      if (thinking) thinkBody.textContent = thinking;
      else thinkBody.textContent = _isDSNow
        ? "（这次模型没有输出思考内容 —— 可能问题太简单，或该模型未开思考）"
        : "（本地 " + (_prov || "模型") + " 不是推理模型，没有思考过程。"
          + "想看思考过程请在顶栏把聊天模型切到 DeepSeek V4.1）";
      thinkBody.classList.remove("open");   // 完成后保持折叠（带动画）
      thinkArrow.textContent = "▸";
    };

    streaming = true;
    typingHintShown = false;
    aborted = false;
    // 长时间收不到任何数据（不是用户点终止）时自动断开的标记
    let _hardStall = false;
    $("#sendBtn").disabled = true;
    setStopVisible(true);
    abortCtl = new AbortController();
    // ⏳ 卡顿看门狗：**没有任何数据进来时，界面不能一声不吭**。
    //
    // 为什么需要（2026-09-22 用户拿着截图问"思考才憋出两个字然后卡着不动，正常吗"）：
    //   第 0.5 秒到第一个 token 之间是**纯等待**（Ollama 要先预填充整份提示词，
    //   启动后第一条约 2 万 token，实测 10~16 秒；并发时还可能被前一条占住几十秒）。
    //   这段时间界面只有一个不动的「🧠 思考中…」，用户完全无法分辨
    //   "在算" 还是 "死了"。这里每收到一块就重置计时，超过 4 秒就报出已等待时长。
    // ⚠️ 有数据进来后自然恢复成「思考中…（N 字）」——因为 updateThinkStatus 会覆盖它。
    let _stallT0 = 0;
    let _stallTimer = null;
    // 后端在"模型还没加载"时会先发一条 status（30B 冷启动实测约 20 秒）。
    // 存下来，让下面的等待提示带上**具体原因**，而不是只说"在准备"。
    let _backendStatus = "";
    const _clearStall = () => {
      if (_stallTimer) { clearInterval(_stallTimer); _stallTimer = null; }
    };
    const _armStall = () => {
      _clearStall();
      _stallT0 = Date.now();
      _stallTimer = setInterval(() => {
        const sec = Math.round((Date.now() - _stallT0) / 1000);
        // 3 分钟一个字节都没进来 → 连接已经卡死，别再让用户对着转圈等
        if (sec >= 1800 && !_hardStall) {   // 原来 240 秒就断言卡死；用户要求去掉限制
          _hardStall = true;
          _clearStall();
          try { abortCtl && abortCtl.abort(); } catch (e) { /* 忽略 */ }
          return;
        }
        if (sec >= 4) {
          thinkStatus.textContent = _backendStatus
            ? (_backendStatus + "　已等待 " + sec + " 秒")
            : ("⏳ 模型正在准备…已等待 " + sec
               + " 秒（大提示词要先读一遍；若前面还有任务在跑，会等它让出来）");
        }
      }, 1000);
    };
    _armStall();
    try {
      // ── 模型路由（2026-11-08 新增）─────────────────────────────
      // 本地模型走 /api/chat（带工具/记忆/RAG），DeepSeek 走 /api/chat/deepseek
      // （同样带工具：41 个全量 schema，见 _ds_tools）。Key 放请求头，不进 body、不进 URL。
      const _msEl = document.querySelector("#modelSelect");
      _prov = _msEl ? _msEl.value : "auto";   // ← 只赋值；声明已提到 doSend 顶部（见上）
      // 「本地·自动」= 跟随配置；如果配置的默认模型是云端模型，就别往本地通道发
      /* 🔒LOCK:default-model-ready —— 自迭代禁止修改本区块（与上面同属一处逻辑） */
      // 先等"配置里的默认模型"就绪（最多 1.5 秒），否则「本地·自动」会被误判成
      // 纯本地通道 → 后端拒绝 → 前端把错误吞掉 → 看起来像"一直没反应"。
      if (window.__defaultModel === undefined && window.__defaultModelPromise) {
        try {
          await Promise.race([
            window.__defaultModelPromise,
            new Promise((r) => setTimeout(r, 1500)),
          ]);
        } catch (e) { /* 拿不到就按"未知"处理 */ }
      }
      if (_prov === "auto" && /^deepseek/i.test(String(window.__defaultModel || ""))) {
        _prov = String(window.__defaultModel);
      }
      /* 🔒END:default-model-ready */
      const _isDS = _prov.indexOf("deepseek") === 0;
      const _dsKey = _isDS ? (localStorage.getItem("ds_api_key") || "") : "";
      if (_isDS && !_dsKey && !window.__dsEnv) {
        showToast("请先点右上角 🔑 DeepSeek 填入 API Key", "warn");
        streaming = false;
        return;
      }
      const _chatUrl = _isDS ? "/api/chat/deepseek" : "/api/chat";
      const _hdrs = { "Content-Type": "application/json" };
      // 两个 Key 都带上（都在请求头，不进 body/URL/日志）：
      //   · DeepSeek Key —— 云端对话要用；**本地模式也要**，因为代码生成走 V4.1
      //   · 图片 Key   —— 文生图/微改要用（两条通道都可能调 generate_image）
      if (_dsKey) _hdrs["X-DeepSeek-Key"] = _dsKey;
      const _imgKey = localStorage.getItem("img_api_key") || "";
      if (_imgKey) _hdrs["X-Image-Key"] = _imgKey;
      const _imgModel = localStorage.getItem("img_api_model") || "";
      if (_imgModel) _hdrs["X-Image-Model"] = _imgModel;

      const _payload = {
        messages: history, images_b64: mediaB64,
        // ★ 涂抹选区（若有）：后端据此走"局部重绘"，只改涂过的地方
        mask_b64: selMask ? [selMask] : null,
        docs: (docPayload && docPayload.length) ? docPayload : null,
        // 在开发台里说话 = 就是在写代码 → 后端**直接接入编程模型**，
        // 不再靠关键词猜意图（用户："自动接入编程的模型"）
        studio: !!(window.Studio && window.Studio.opened),
        stream: true, session_id: sessionId,
      };
      if (_isDS) {
        // ⚠️ 2026-11-09 修：这里原来把 images_b64 置空了，理由是"DeepSeek 不吃图片"——
        //    **这是错的**：V4.1 Flash 是多模态（input_modalities 含 image），
        //    实测能读截图。置空导致"粘贴了图，模型却说看不到"。
        //    现在图片照常发（后端会按 image_url 拼进最后一条 user 消息）；
        //    选区蒙版和文档仍然只在本地通道用，云端不支持就保持 null。
        _payload.model = _prov;
        _payload.mask_b64 = null;
        _payload.docs = null;
        _payload.studio = false;
      } else if (_prov && _prov !== "auto") {
        // 本地模式指定了具体模型（下拉里选了 30B 或 8B）
        _payload.model = _prov;
      }

      // 微信内置浏览器（X5）会整段缓冲 fetch 流 → 直接走 XHR 增量读取
      const _isWeChat = /MicroMessenger/i.test(navigator.userAgent || "");
      let _bodyStr = JSON.stringify(_payload);
      let reader;
      if (_isWeChat) {
        let _xhrAbort = null;
        const _origAbort = abortCtl.abort.bind(abortCtl);
        abortCtl.abort = () => { try { _xhrAbort && _xhrAbort(); } catch (e) {} _origAbort(); };
        reader = makeXhrReader(_chatUrl, _hdrs, _bodyStr, (fn) => { _xhrAbort = fn; });
      } else {
      const resp = await fetch(_chatUrl, { method: "POST", headers: _hdrs,
        signal: abortCtl.signal,
        body: _bodyStr });
      if (!resp.ok) {
        const e = await resp.json();
        throw new Error(e.detail || resp.statusText);
      }
      // ⚠️ 2026-11-09：手机（尤其微信内置浏览器 / 老 Safari）没有
      //   ReadableStream，`resp.body` 可能是 null —— 直接 .getReader() 会抛错，
      //   表现就是"没有流式、甚至不出字"。这里做一个等价 shim：
      //   拿不到 reader 时，把整段 NDJSON 一次性读出来当"一个分片"喂给同一个循环，
      //   虽然不流式，但内容一定会出来。
      if (resp.body && typeof resp.body.getReader === "function") {
        reader = resp.body.getReader();
      } else {
        let sent = false;
        showToast("当前浏览器不支持流式输出，已改为整段显示（换 Chrome/Safari 可恢复流式）", "warn");
        reader = {
          read: async () => {
            if (sent) return { done: true, value: undefined };
            sent = true;
            const txt = await resp.text();
            return { done: false, value: new TextEncoder().encode(txt) };
          },
          cancel: async () => {},
        };
      }
      }   // ← 非微信分支结束
      const decoder = new TextDecoder();
      // 2026-11-09：**最后几个字被吞掉**的根因。
      //   NDJSON 最后一行可能没有结尾换行（或被切在半个字上），
      //   原代码 `if (done) break;` 直接退出循环，buf 里剩的那一截永远没人解析，
      //   表现就是"回答停在一半、光标还在闪"。
      //   修法：reader 结束时先 flush 解码器，再补一个换行，让残留走同一套解析。
      const _rawRead = reader.read.bind(reader);
      let _tailSent = false;
      reader.read = async () => {
        const r = await _rawRead();
        if (r.done && !_tailSent) {
          _tailSent = true;
          const rest = decoder.decode();
          return { done: false, value: new TextEncoder().encode((rest || "") + "\n") };
        }
        return r;
      };
      let buf = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        _armStall();                 // 收到数据 → 重新计时（不再是"卡住"状态）
        window.__lastStreamAt = Date.now();
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split("\n"); buf = lines.pop();
        for (const line of lines) {
          if (!line.trim()) continue;
          let obj;
          try { obj = JSON.parse(line); } catch { continue; }
          // ★ 服务端在结束事件里带完整正文（2026-11-09）
          //   接收环节只要漏了一片（切在半个 JSON、XHR 缓冲吞尾…），界面上就会少字。
          //   这里直接以服务端原文为准做一次回填，彻底不依赖"猜哪里丢的"。
          if (obj.ping) { _armStall(); continue; }     // 服务端心跳：只是在跑长工具，别当成卡死
          /* 🔒LOCK:stream-error-order —— 自迭代禁止修改本区块
             【为什么锁】后端有 3 处返回**同一条事件里同时带 error 和 __end**
             （本地通道收到云端模型名的提示、Ollama 报错、超窗兜底）。
             只要 `__end` 排在 `error` 前面且带 `continue`，这些错误就会被整条吞掉，
             界面上一个字都不显示 —— 用户只能看到空白，以为是"卡了/慢了"。
             ⚠️ 正确顺序必须是：ping → error → __end。改动请先问用户。 */
          if (obj.error) {
            // `explained` = 后端已把原因翻成人话、也给了下一步怎么办。
            // 这类错误**不要**再补"（可能内存/模型未就绪，请查看状态）"——
            // 实测用户看到那句会去查显存，而真因（工具调用 JSON 写坏）跟硬件无关。
            // 少数错误事件可能还带完整正文（部分成功后失败），先回填再抛，别丢内容。
            if (typeof obj.answer === "string" && obj.answer) {
              answer = obj.answer;
              try { paintAnswer(true); } catch (e) { /* 重绘失败不影响报错 */ }
            }
            const _e = new Error(obj.error);
            if (obj.explained) _e.explained = true;
            throw _e;
          }
          if (obj.__end) {
            try { console.warn("[stream] __end local=", String(answer||"").length,
                               "server=", (obj.answer||"").length,
                               "尾=", JSON.stringify(String(obj.answer||"").slice(-26))); } catch (e) {}
            // 无条件以服务端为准：本地累计可能少（丢分片）或多（重复），都没意义
            if (typeof obj.answer === "string" && obj.answer) {
              try {
                if (obj.answer !== answer) {
                  console.warn("[stream] 以服务端正文为准：", answer.length, "→", obj.answer.length);
                }
              } catch (e) {}
              answer = obj.answer;
              try { paintAnswer(true); } catch (e) { /* 重绘失败不影响 */ }
            }
            continue;
          }
          /* 🔒END:stream-error-order */
          if (obj.message) {
            if (obj.message.thinking_reset) {
              // 后端因"思考吃满了输出空间"而加长上限重试：上一轮的思考已经作废。
              // 必须把面板和缓冲都清空 —— 否则新一轮的思考会直接接在旧的后面，
              // 界面上看起来就是"同一个思路说了两遍"（2026-09-19 用户反馈）。
              thinking = ""; thinkPending = "";
              thinkBody.textContent = "";
              updateThinkStatus();
            }
            if (obj.message.thinking) {
              pushThinking(obj.message.thinking);  // 进入平滑播放器，逐字实时渲染
            }
            if (obj.message.content) {
              answer += obj.message.content;
              // 逐字流式时也过一遍闸：后端会先把"可能是工具调用壳子"的尾巴扣住，
              // 万一有变体漏过来，这里显示时也不会闪出一坨裸 JSON（`answer` 存的仍是原文）。
              paintAnswer(false);
            }
          }
          if (obj.tool_start) addToolChip(obj.tool_start.name);
          // 一轮里同时发起多个工具时给个提示 —— 让用户知道这是并行执行、在省时间
          if (obj.tool_parallel) {
            addToolChip(`⚡ 并行执行 ${obj.tool_parallel} 个工具`);
          }
          if (obj.ui) {
            if (obj.ui.type === "sources") sourcesData = obj.ui;   // 稍后在回答下方渲染
            else if (obj.ui.type === "confirm") showConfirm(obj.ui);
            else if (obj.ui.type === "ask") showAskDialog(obj.ui);
            else if (obj.ui.type === "restart_ready") { if (window.__onRestartReady) window.__onRestartReady(obj.ui); }
            else if (obj.ui.type === "library") {
              // 模型动了生成文库 → 面板跟着刷新，让用户马上看到结果
              if (typeof loadDoclib === "function") loadDoclib(obj.ui.rel);
            }
            else if (obj.ui.type === "map") {
              // 地图卡片：地点/路线已经算好，这里把它画出来
              makeMapCard(obj.ui);
            }
            else if (obj.ui.type === "selfdev") {
              // 模型改了应用源码：给一张**可折叠**的改动卡片（默认收起，不撑页面）
              makeSelfdevCard(obj.ui);   // 收进本轮改动列表（不再一张张往下堆）
              messagesEl.scrollTop = messagesEl.scrollHeight;
            }
            else if (obj.ui.type === "code") {
              appendCodeCard(answerWrap, makeCodeCard(obj.ui));
              messagesEl.scrollTop = messagesEl.scrollHeight;
            }
            else if (obj.ui.type === "typing") {
              // AI 正在把代码**逐字**写进项目文件（后端是边生成边落盘的）。
              // 编辑器（内置 VS Code）自己盯着磁盘，会看到内容一点点长出来；
              // 这里只负责把状态说清楚，免得用户以为"它又在憋大招"。
              if (window.Studio && window.Studio.notify) window.Studio.notify(obj.ui);
              if (!window.Studio || !window.Studio.opened) {
                if (!typingHintShown) {
                  typingHintShown = true;
                  showToast("AI 正在写代码到项目文件（可在开发台看到它逐字敲）", "ok");
                }
              }
            }
            else if (obj.ui.type === "workspace") {
              // 模型改了工作区文件：
              // · 开发台开着 → notify 直接把那个文件切到编辑器里，用户不用复制
              // · 开发台没开 → **不强行弹出来打断阅读**，给一张卡片，点一下才打开
              if (window.Studio && window.Studio.notify) window.Studio.notify(obj.ui);
              if (obj.ui.act === "write" && window.Studio && !window.Studio.opened) {
                answerWrap.appendChild(makeWsCard(obj.ui));
                messagesEl.scrollTop = messagesEl.scrollHeight;
              }
            } else addMedia(obj.ui);
          }
          if (obj.note) { notes.push(obj.note); showToast(obj.note, "warn"); }
          // ⏳ 后端在模型**还没加载**时先发一条状态（30B 冷启动实测约 20 秒）。
          //    这段时间 Ollama 一个字都不吐，不提示的话界面上就是"完全没反应"。
          //    只改状态栏文案，不动回答气泡 —— 真正的思考/正文到了会覆盖它。
          if (obj.status) { _backendStatus = obj.status; thinkStatus.textContent = obj.status; }
          // ★ 2026-11-09：用户反复报「回答差最后那几个字」。
          //   后端在结束块里带了**整段完整正文**（done.text），但以前这里只 break、
          //   完全没用它 —— 增量流只要少发一个分片（扣住的尾巴没补发、最后一片被截断、
          //   网络收尾丢包），界面上就永远缺那一小段，没有任何补救机会。
          //   这里以 done.text 为准做一次兜底：比增量累积的长，就用它重绘。
          if (obj.done) {
            const _full = (typeof obj.text === "string") ? obj.text : "";
            if (_full && _full.length > answer.length) {
              answer = _full;
              paintAnswer(true);
            }
            const _fullThink = (typeof obj.thinking === "string") ? obj.thinking : "";
            if (_fullThink && _fullThink.length > thinking.length) {
              thinking = _fullThink;
              updateThinkStatus();
              if (thinkBody.classList.contains("open")) thinkBody.textContent = thinking;
            }
          }
          if (obj.done || obj.__end) break;
          _lastEventAt = Date.now();
        }
        messagesEl.scrollTop = messagesEl.scrollHeight;
      }
      finishThinking();
      // 回答里带代码块的话渲染成可编辑卡片；否则维持原来的纯文本（不改变原有观感）
      // ⚠️ 收尾：最后一片 NDJSON 可能没有换行结尾（服务端异常断开时常见），
      //    它会被留在 buf 里、**静默丢掉** —— 用户看到的就是"最后一个字没了"。
      //    这里补处理一次（只取正文增量，够用且不会出错）。
      try {
        const _rest = (buf || "").trim();
        if (_rest) {
          const _o = JSON.parse(_rest);
          if (_o && _o.message && _o.message.content) answer += _o.message.content;
          // 残片如果正好是结束块（服务端把最后一行写了一半），同样拿它带的完整正文兜底
          if (_o && _o.done && typeof _o.text === "string" && _o.text.length > answer.length) {
            answer = _o.text;
          }
        }
        buf = "";
      } catch (e) { /* 残片解析不了就算了，别影响收尾 */ }
      // ⚠️ 2026-11-09 修「回答差最后几个字」：上面整段 JSON.parse 一旦失败就全丢，
      //   而流被切断时残留的往往正是最后那个 done 块的一半 —— 它的 "text"
      //   （整段完整正文）在切断点之前、通常是完整的，只是后面的 "thinking"
      //   没传完 → 整体 parse 失败 → 界面上就少了尾巴。这里做字段级抢救。
      try {
        const _rest2 = (buf || "").trim();
        if (_rest2) {
          for (const _seg of _rest2.split("\n").map((x) => x.trim()).filter(Boolean)) {
            let _o2 = null;
            try { _o2 = JSON.parse(_seg); } catch (e1) { _o2 = null; }
            if (_o2) {
              if (_o2.message && _o2.message.content) answer += _o2.message.content;
              if (_o2.done && typeof _o2.text === "string" && _o2.text.length > answer.length) {
                answer = _o2.text;
              }
              continue;
            }
            // —— 半截行：按字段抢救 ——
            const _mt = _seg.match(/"text"\s*:\s*"((?:[^"\\]|\\.)*)"/);
            if (_mt) {
              try {
                const _f = JSON.parse('"' + _mt[1] + '"');
                if (_f.length > answer.length) answer = _f;
              } catch (e2) { /* 转义序列被切断，救不回 */ }
            }
            const _mc = _seg.match(/"content"\s*:\s*"((?:[^"\\]|\\.)*)"/);
            if (_mc) {
              try { answer += JSON.parse('"' + _mc[1] + '"'); } catch (e3) { answer += _mc[1]; }
            }
            // 2026-11-09 再兜一层：**字符串被切断**（JSON 停在半个字中间、没有收尾引号）。
            // 上面两个正则都要求有结尾引号，这种残片会整体匹配失败 → 最后 1~2 个字就没了。
            // 这里退化成"能读多少读多少"，半个转义序列（末尾是 \）先丢掉再解码。
            if (!_mt && !_mc) {
              const _mt2 = _seg.match(/"text"\s*:\s*"([\s\S]*)$/);
              const _mc2 = _seg.match(/"content"\s*:\s*"([\s\S]*)$/);
              const _m2 = _mt2 || _mc2;
              if (_m2) {
                let _raw = _m2[1];
                if (/\\$/.test(_raw)) _raw = _raw.slice(0, -1);
                let _dec = "";
                try { _dec = JSON.parse('"' + _raw + '"'); }
                catch (e4) {
                  _dec = _raw.replace(/\\n/g, "\n").replace(/\\"/g, '"').replace(/\\\\/g, "\\");
                }
                if (_dec) {
                  if (_mt2) { if (_dec.length > answer.length) answer = _dec; }
                  else { answer += _dec; }
                  try { console.warn("[stream] 尾部残片已抢救", _dec.length, "字"); } catch (e5) {}
                }
              }
            }
          }
        }
        buf = "";
      } catch (e) { /* 收尾抢救失败也不影响主流程 */ }
      // ★ 2026-11-09 最终防线：先渲染，再**逐字核对尾部**。
      //   长度比对不可靠（markdown 符号会少几十个字），但"结尾有没有出现在屏幕上"骗不了人。
      // 纯文本模式（A/B 用）：地址栏带 ?plain=1 即生效，之后一直记住。
      //   若开了它就不再断字，说明问题在 markdown 渲染；若还断，问题在数据/传输。
      try {
        if (/[?&]plain=1/.test(location.search)) localStorage.setItem("render_style", "plain");
        if (/[?&]plain=0/.test(location.search)) localStorage.setItem("render_style", "md");
      } catch (e) { /* 忽略 */ }
      // 默认开启纯文本渲染（先保证"一个字都不少"），格式化模式可用 ?plain=0 关掉。
      // 统一走 renderFinal：纯文本 / 格式化 + 尾部核对（见函数定义处）
      renderFinal(answerBubble, answer, promptText);
      try { localStorage.setItem("__last_answer", String(answer || "")); } catch (e) {}
      if (!answer) {
        answerBubble.textContent = notes.length
          ? "⚠️ " + notes[notes.length - 1]
          : "⚠️ 模型这次没有输出内容，请再试一次或换个问法。";
        answerBubble.classList.add("empty-answer");
      }
      // 自动续写：结尾没有终止标点就补一条"从断开处继续"（最多 2 次，按用户消息重置）
      try {
        const _t = String(answer || "").trimEnd();
        const _cut = _t.length > 60 && !/[。！？!?….…”』」）)】\n`~；;：:]$/.test(_t);
        const _n = (window.__autoCont = (window.__autoCont || 0));
        if (_cut && !aborted && _n < 2 && !/上一条回复被截断/.test(promptText || "")) {
          window.__autoCont = _n + 1;
          setTimeout(() => {
            try {
              const inp = document.querySelector("#input");
              inp.value = "（系统）上一条回复被截断了（结尾停在「" + _t.slice(-6) + "」）。请从断开处继续写下去，不要重复前面的内容。";
              inp.dispatchEvent(new Event("input"));
              send();
            } catch (e) { /* 忽略 */ }
          }, 400);
        } else if (!_cut) { window.__autoCont = 0; }
      } catch (e) { /* 忽略 */ }
      if (answer) history.push({ role: "assistant", content: answer });
      // 检索来源：渲染成回答下方的可伸缩列表（默认折叠，点标题展开）
      if (sourcesData && sourcesData.items && sourcesData.items.length) {
        renderSources(answerWrap, sourcesData);
      }
      persistSession();   // 落盘，保证程序重启后能恢复这段对话
      // ★ 最后一道保险（2026-11-10）：从服务端回读这轮的完整正文，
      //   如果比屏幕上显示的长，就用服务端的重绘 —— 服务端是唯一真相。
      (async () => {
        try {
          if (!sessionId) return;
          const d = await api("/api/sessions/" + sessionId);
          const msgs = d.messages || [];
          const last = [...msgs].reverse().find((m) => m && m.role === "assistant");
          const srv = String((last && last.content) || "");
          if (srv.length > String(answer).length) {
            console.warn("[stream] 服务端原文更长，按它重绘：", answer.length, "→", srv.length);
            answer = srv;
            renderFinal(answerBubble, answer, promptText);
            for (let i = history.length - 1; i >= 0; i--) {
              if (history[i] && history[i].role === "assistant") { history[i].content = srv; break; }
            }
            try { persistSession(); } catch (e) { /* 忽略 */ }
          }
        } catch (e) { /* 回读失败不影响已显示内容 */ }
      })();
    } catch (err) {
      finishThinking();
      if (_hardStall) {
        // 不是用户点的终止，而是 3 分钟没有任何新内容（连接卡死）——
        // 必须说清「内容已保留」，否则用户会以为是自己误触了终止。
        const _keep = (answer || "").trim();
        answerBubble.textContent = (_keep ? _keep + "\n\n" : "")
          + "（⚠️ 已经 4 分钟没有任何新内容，连接像是卡住了，我已自动断开。"
          + "上面已生成的部分都保留着 —— 回一句「继续」我就接着往下写。）";
        answerBubble.classList.remove("empty-answer");
        if (answer) history.push({ role: "assistant", content: answerBubble.textContent });
        showToast("连接卡住了，已自动断开（内容已保留）");
        try { persistSession(); } catch (e) { /* 落盘失败不影响使用 */ }
      } else if (aborted || (err && err.name === "AbortError")) {
        // 用户点了「■ 终止」：把已经生成出来的部分留着，别报成错误
        answerBubble.classList.remove("empty-answer");
        // ⚠️⚠️ **终止也必须往历史里写一条助手消息**（哪怕一个字都没生成）。
        //
        // 实测（2026-09-22 用户反馈）：点了终止之后，历史里只留下一条"没人回答的用户消息"，
        // 下一轮模型会把它当成**未完成的需求接着做** —— 用户问「红腹锦鸡长什么样」，
        // 图库里却混进一张上一条要的狐狸图。⚠️ 已复现：把这种历史直接发给后端，
        // 模型当场又调了一次 generate_image，而且**完全没回答这一轮真正的问题**。
        //
        // 文案要**对模型说话**（它会被写进提示词），用户看了也不突兀：
        // 明确"这条需求作废、别在新的一轮补做"。显示与落盘用**同一段文本**，
        // 免得刷新后两处不一致。
        const stopMark = "〔已终止〕用户主动中断了这次生成 —— 上面那条需求就此作废，"
                       + "不要在新的一轮里接着做，除非用户再次明确要求。";
        answerBubble.textContent = answer ? answer + "\n\n" + stopMark : stopMark;
        history.push({ role: "assistant",
                       content: (answer ? answer + "\n\n" : "") + stopMark });
        if (answer) {
          // 卡住被强制断开：把已有的正文留着，明确告诉用户"不是答完了"
          answerBubble.textContent = answer.trimEnd() +
            "\n\n（⚠️ 连接中断，以上是已生成的部分；可以点「发送」旁的终止后再问一次，或直接说「继续」）";
          history.push({ role: "assistant", content: String(answer) });
          try { persistSession(); } catch (e) { /* 忽略 */ }
        }
        showToast("连接中断，已保留已生成的内容");
        try { persistSession(); } catch (e) { /* 落盘失败不影响使用 */ }
      } else {
        // 后端已经解释过原因的（explained），正文里就是完整的人话，别再补尾巴。
        answerBubble.textContent = "❌ " + err.message
          + (err.explained ? "" : "（可能内存/模型未就绪，请查看状态）");
        answerBubble.classList.remove("empty-answer");
      }
    } finally {
      streaming = false;
      _hardStall = false;
      abortCtl = null;
      // 2026-11-09：不管正常结束、被终止、还是流卡住，**必须把闪烁光标清掉**。
      //   原来 aborted 分支不碰气泡，结果"内容停一半 + 光标一直闪"。
      try {
        answerBubble.querySelectorAll(".stream-cursor").forEach((n) => n.remove());
      } catch (e) { /* 忽略 */ }
      _clearStall();                 // 别忘了停掉卡顿看门狗
      // ⚠️ 已经生成了一半才出错（网络断、后端异常）时，**绝不能把已生成的内容抹掉** ——
      //    catch 分支原本会直接把气泡替换成一行报错，用户看到的就是"回答突然只剩报错"。
      if (answer && !aborted && answerBubble.textContent.indexOf("❌") === 0) {
        answerBubble.textContent = answer + "\n\n" + answerBubble.textContent;
        history.push({ role: "assistant", content: answerBubble.textContent });
        try { persistSession(); } catch (e) { /* 落盘失败不影响使用 */ }
      }
      // 2026-11-10：收尾渲染那一步在某些路径下不执行（见 __last_answer=0 的探针），
      //   这里做最后兜底：还有 answer 但气泡里没有任何结构化标签 → 补渲染一次（带尾部核对）。
      try {
        if (answer && !isPlainMode() &&
            !answerBubble.querySelector("strong,b,ul,ol,pre,code,h1,h2,h3,table")) {
          renderFinal(answerBubble, answer, promptText);
        }
      } catch (e) { /* 忽略 */ }
      $("#sendBtn").disabled = false;
      setStopVisible(false);
    }
  }

  // ---------- 终止当前任务 ----------
  function setStopVisible(on) {
    const b = $("#stopBtn");
    if (!b) return;
    b.hidden = !on;
    b.disabled = !on;
  }

  function stopStreaming() {
    if (!streaming) return;
    aborted = true;
    // ① 断掉前端的流（连接一断，服务端就会掐掉到 Ollama 的连接）
    try { abortCtl && abortCtl.abort(); } catch (e) { /* 忽略 */ }
    // ② 再补一刀：显式通知后端停止，防止连接没断干净导致模型继续跑
    try {
      fetch("/api/chat/stop", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId }),
      });
    } catch (e) { /* 忽略 */ }
    // ③ 立刻恢复界面，不用等 finally
    streaming = false;
    $("#sendBtn").disabled = false;
    setStopVisible(false);
  }

  const stopBtnEl = $("#stopBtn");
  if (stopBtnEl) stopBtnEl.onclick = stopStreaming;
  setStopVisible(false);

  $("#sendBtn").onclick = send;
  inputEl.onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } };
  function autoGrow() { inputEl.style.height = "auto"; inputEl.style.height = inputEl.scrollHeight + "px"; }
  inputEl.addEventListener("input", autoGrow);

  // 记住配置里的默认模型：「本地·自动」需要知道它是不是云端模型（2026-11-10）
  /* 🔒LOCK:default-model-ready —— 自迭代禁止修改本区块
     【为什么锁】这段决定「本地·自动」到底走本地通道还是云端通道。
     `__defaultModel` 是异步取 /api/config 得来的；如果发消息时它还没到，
     就会被判成纯本地通道，而 default_model 是云端模型时后端会直接拒绝
     （返回 {error, explained, __end}）→ 界面上只剩一个"…"，看着像卡死。
     所以必须：① 保存成可等待的 Promise；② 发消息前 await 它。
     改动请先问用户。 */
  window.__defaultModelPromise = (async () => {
    try {
      const d = await api("/api/config");
      window.__defaultModel = (d && d.config && d.config.default_model) || "";
    } catch (e) {
      // 拉不到就给个确定值（空串=不知道），否则下面"等它"的逻辑会一直等
      if (window.__defaultModel === undefined) window.__defaultModel = "";
    }
  })();
  /* 🔒END:default-model-ready */

  // 界面自更新重载后，把用户没发出去的草稿放回输入框（2026-11-09）
  try {
    const _draft = sessionStorage.getItem("draft_input");
    if (_draft) {
      sessionStorage.removeItem("draft_input");
      const _box = document.getElementById("input");
      if (_box && !_box.value.trim()) {
        _box.value = _draft;
        _box.dispatchEvent(new Event("input"));
      }
    }
  } catch (e) { /* 忽略 */ }

  // ---------- 初始化 ----------
  // 先恢复会话（含上次的历史消息），再跑其它轮询
  (async () => {
    const list = await loadSessions();
    if (sessionId) {
      try {
        const d = await api("/api/sessions/" + sessionId);
        const msgs = d.messages || [];
        if (msgs.length && renderHistory(msgs) > 0) {
          showToast("已恢复上次的对话记录", "ok");
        }
      } catch (e) { /* 恢复失败则用空白会话 */ }
    }
    renderSessions(list);
  })();

  // ---------- 界面自更新：改了前端不用用户手动刷新 ----------
  // 这个应用是 WebView2 窗口，**没有地址栏、也没有刷新按钮** ——
  // 原来每次改完界面都要让用户"自己刷新一下才看得到"，等于把开发成本转嫁给用户。
  // 现在后台比对前端的版本号（mtime+size），变了就自己重载。
  //
  // ⚠️ 三条**必须满足**才敢重载，否则宁可不更新：
  //   ① 没有正在生成 —— 中途重载会把这一轮回答打断，用户会以为"又崩了"；
  //   ② 输入框是空的 —— 否则用户打了一半的字会被冲掉；
  //   ③ 开发台没有未保存的改动 —— 否则改了一半的文件会丢。
  // 不满足就把 pendingReload 挂起，等下一轮轮询条件够了再重载（回答结束后会自动恢复聊天记录）。
  let feVersion = "";
  let pendingReload = false;
  let reloading = false;

  const _touchActivity = () => { window.__lastActivity = Date.now(); };
  try { _touchActivity(); setInterval(_touchActivity, 5000); } catch (e) {}

  const canReload = () => {
    if (streaming) return false;
    // 2026-11-09：原来"输入框有字就不重载"，结果用户输入框长期留着草稿 →
    //   前端更新永远不生效（改了几版都看不到效果）。现在改成**保存草稿再重载**。
    if (window.Studio && window.Studio.hasUnsaved && window.Studio.hasUnsaved()) return false;
    return true;
  };

  async function checkFrontendVersion() {
    if (reloading) return;
    let v = "";
    try {
      const d = await api("/api/frontend/version");
      v = d.version || "";
    } catch (e) { return; }          // 探测失败不影响使用
    if (!feVersion) {
      feVersion = v;
      // 把版本号显示在标题旁边：一眼能看出窗口跑的是哪份前端（太小看不到也没关系）
      try {
        const el = document.getElementById("uiVer");
        if (el) el.textContent = "v" + String(v).split(":").pop().slice(-6);
      } catch (e) {}
      return;
    }
    if (v === feVersion) return;
    feVersion = v;
    if (!canReload()) {
      // 2026-11-09：如果 streaming 卡死（异常后没复位），更新会永远等下去 ——
      // 加一条自愈：闲置超过 30 秒就强制重载（草稿会保存）。
      const _idle = Date.now() - (window.__lastActivity || 0);
      if (_idle > 30000) {
        try {
          const _box = document.getElementById("input");
          if (_box && _box.value.trim()) sessionStorage.setItem("draft_input", _box.value);
        } catch (e) { /* 忽略 */ }
        console.warn("[ui] 闲置 " + Math.round(_idle / 1000) + " 秒，强制加载新界面");
        location.reload();
        return;
      }
      if (!pendingReload) {
        pendingReload = true;
        showToast("界面有新版本，等这一轮结束会自动更新", "ok");
      }
      return;
    }
    reloading = true;
    showToast("界面已更新，正在重新加载…", "ok");
    // 给 toast 一点时间被看见，再重载（重载后会恢复聊天记录）
    try {
      const _box = document.getElementById("input");
      if (_box && _box.value.trim()) sessionStorage.setItem("draft_input", _box.value);
    } catch (e) { /* 忽略 */ }
    setTimeout(() => location.reload(), 900);
  }

  setInterval(checkFrontendVersion, 3000);
  // 挂起中时，每轮生成结束后立刻再试一次（不用干等下一个 3 秒）
  setInterval(() => { if (pendingReload) checkFrontendVersion(); }, 1200);

  // ---------- 绘图计算设备：徽标 + 切换菜单 ----------
  // 文生图与图片微改共用同一个 torch 环境，设备一致。
  // 切换是"有后果"的操作：后端会在新设备上跑一次真实运算、并把绘图引擎
  // 实际加载上去，两步都过了才算成功。所以这里必须把结果讲清楚，
  // 失败时尤其要说明**当前是什么模式**和**为什么失败**。
  let deviceSwitching = false;

  function renderDeviceBadge(kind, gpu) {
    const text = $("#deviceBadgeText");
    if (!text) return;
    text.textContent = kind === "gpu"
      ? ("⚡ GPU 加速" + (gpu ? "（" + gpu + "）" : ""))
      : "🖥 CPU 模式";
    const badge = $("#deviceBadge");
    badge.classList.toggle("gpu", kind === "gpu");
  }

  // 标记哪一项是"当前选择"：forced 是用户的选择（auto/cpu/gpu），
  // kind 是实际生效的设备（cpu/gpu）。两者要分开显示，
  // 否则"自动 → 实际 GPU"会被误标成"手动选了 GPU"。
  const MODE_LABEL = { auto: "自动（推荐）", gpu: "GPU 加速", cpu: "CPU 模式" };

  function markDeviceOptions(forced, kind) {
    ["auto", "gpu", "cpu"].forEach((m) => {
      const el = $("#devState" + m.charAt(0).toUpperCase() + m.slice(1));
      if (el) {
        // 「自动」选中时补一句实际用的是什么，避免用户猜
        el.textContent = (m === forced)
          ? (m === "auto" ? "当前·" + (kind === "gpu" ? "GPU" : "CPU") : "当前")
          : "";
      }
      const btn = document.querySelector(`.dev-opt[data-mode="${m}"]`);
      if (btn) btn.classList.toggle("active", m === forced);
    });
  }

  function setDeviceStatus(html, cls) {
    const el = $("#deviceStatus");
    if (!el) return;
    el.innerHTML = html || "";
    el.className = "dev-status" + (cls ? " " + cls : "");
    el.hidden = !html;
  }

  async function loadDevice() {
    const badge = $("#deviceBadge");
    if (!badge) return;
    try {
      const d = await api("/api/t2i/capability");
      renderDeviceBadge(d.kind, d.gpu);
      markDeviceOptions(d.forced || "auto", d.kind);
      badge.title = (d.kind === "gpu"
        ? `绘图使用显卡加速：${d.gpu || "未知型号"}`
        : `绘图使用 CPU，单张约 20 秒`)
        + `\ntorch ${d.torch || "?"}`
        + `\n选择：${MODE_LABEL[d.forced] || d.forced}`
        + (d.reason ? `\n${d.reason}` : "")
        + "\n点击可切换设备";
      badge.classList.remove("loading");
    } catch (e) {
      const text = $("#deviceBadgeText");
      if (text) text.textContent = "设备未知";
    }
  }

  async function switchDevice(mode) {
    if (deviceSwitching) return;
    deviceSwitching = true;
    const badge = $("#deviceBadge");
    badge.classList.add("loading");
    const label = MODE_LABEL[mode] || mode;
    setDeviceStatus(`⏳ 正在切换到「${label}」，并校验设备可用性…`, "busy");
    try {
      const r = await api("/api/t2i/device", {
        method: "POST",
        body: JSON.stringify({ mode }),
      });
      if (r.ok) {
        renderDeviceBadge(r.mode, r.gpu);
        markDeviceOptions(r.forced || mode, r.mode);
        const extra = r.note ? `<div class="dev-sub">${r.note}</div>` : "";
        setDeviceStatus(
          `<div class="dev-ok">✅ 已切换到「${label}」</div>`
          + `<div class="dev-sub">绘图实际使用：<b>`
          + `${r.mode === "gpu" ? "GPU 加速" : "CPU 模式"}`
          + `${r.gpu ? "（" + r.gpu + "）" : ""}</b>　已通过运行校验</div>` + extra,
          "ok");
        showToast(`设备已切换：${label}`, "ok");
      } else {
        const cur = r.mode === "gpu" ? "GPU 加速" : "CPU 模式";
        setDeviceStatus(
          `<div class="dev-fail">❌ 切换到「${label}」失败</div>`
          + `<div class="dev-sub">当前仍是：<b>${cur}</b></div>`
          + `<div class="dev-sub">失败原因：${r.reason || "未知"}</div>`, "fail");
        markDeviceOptions(r.forced || r.mode, r.mode);
        renderDeviceBadge(r.mode, r.gpu);
        showToast(`切换到「${label}」失败，当前为 ${cur}`, "warn");
      }
    } catch (e) {
      setDeviceStatus(`<div class="dev-fail">❌ 切换失败：${e.message || e}</div>`, "fail");
    } finally {
      deviceSwitching = false;
      badge.classList.remove("loading");
    }
  }

  (function bindDeviceMenu() {
    const badge = $("#deviceBadge");
    const menu = $("#deviceMenu");
    if (!badge || !menu) return;
    badge.addEventListener("click", (ev) => {
      ev.stopPropagation();
      menu.hidden = !menu.hidden;
      if (!menu.hidden) {
        // 打开时刷新一下当前状态，并清掉上一次的结果（避免误读成这次的）
        setDeviceStatus("");
        loadDevice();
      }
    });
    menu.querySelectorAll(".dev-opt").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        switchDevice(btn.dataset.mode);
      });
    });
    // 点别处收起；切换进行中不收起（要看结果）
    document.addEventListener("click", () => {
      if (!deviceSwitching) menu.hidden = true;
    });
    menu.addEventListener("click", (ev) => ev.stopPropagation());
  })();

  // ================== DeepSeek 接入（2026-11-08）==================
  // 模型选择 + API Key 管理。
  // ⚠️ Key 存 localStorage（**不写磁盘文件、不进日志、不进 URL**）。
  //   2026-11-08 从 sessionStorage 改为 localStorage：用户要求「每次退出重启
  //   需要保存 apikey 以及上次模型的使用状态」，关掉浏览器不能再丢。
  (() => {
    const sel = document.querySelector("#modelSelect");
    const btn = document.querySelector("#dsKeyBtn");
    if (!sel || !btn) return;

    const saved = localStorage.getItem("chat_provider");
    if (saved) sel.value = saved;

    // ★ 2026-11-10：把「对话最后选用的模型」镜像一份到本机 ——
    //   定时任务半夜跑时浏览器不在，后端只能靠这份记录决定用哪个模型。
    function syncChatModel() {
      try {
        fetch("/api/pref/chat_model", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ model: sel.value }) });
      } catch (e) { /* 同步失败不影响对话 */ }
    }
    if (saved) syncChatModel();

    sel.addEventListener("change", () => {
      localStorage.setItem("chat_provider", sel.value);
      syncChatModel();
      const isDS = sel.value.indexOf("deepseek") === 0;
      // ★ 2026-11-11：切到云端就把本地模型请出显存。
      //   为什么必须做：model_keep_alive 配的是 **4 小时**，切到 DeepSeek 之后
      //   本地模型毫无用处却一直占着显存（8B 实测 9GB / 12GB 卡），
      //   结果就是别的程序卡、再切回本地还要等重载。以前这里什么都不做。
      //   后端会先查 /api/ps，只卸真的加载着的，并等它真退出再返回。
      if (isDS) {
        fetch("/api/model/unload", { method: "POST" })
          .then((r) => r.json())
          .then((d) => {
            if (!d) return;
            if (d.ok && (d.freed || []).length) {
              showToast("已释放显存：" + d.freed.join("、")
                + (d.waited ? "（" + d.waited + "s）" : ""), "ok");
            } else if (!d.ok && d.error) {
              // Ollama 收下请求却没放显存 = 模型进程卡死，得人手动重启
              showToast(d.error, "warn");
            }
          })
          .catch(() => { /* 释放失败不影响对话 */ });
      }
      if (isDS && !localStorage.getItem("ds_api_key")) {
        showToast("已切到 DeepSeek，请点 🔑 填入 API Key", "warn");
      } else {
        showToast("本轮使用：" + sel.options[sel.selectedIndex].text, "ok");
      }
      // 状态卡片 / 自检面板都必须**立刻**跟着切过来（用户要求：
      // 「选用什么模型就显示什么模型的状态，检验也是一样」）
      if (typeof refreshHealth === "function") refreshHealth();
      window.dispatchEvent(new CustomEvent("model-changed", { detail: sel.value }));
    });

    btn.addEventListener("click", () => {
      const old = localStorage.getItem("ds_api_key") || "";
      const mask = document.createElement("div");
      mask.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:99999;display:flex;align-items:center;justify-content:center";
      mask.innerHTML = [
        '<div style="background:#1e1e22;color:#e8e8ea;border-radius:12px;padding:22px;width:min(480px,92vw);box-shadow:0 20px 60px rgba(0,0,0,.5)">',
        '  <h3 style="margin:0 0 6px;font-size:16px">🔑 DeepSeek API Key</h3>',
        '  <p style="margin:0 0 14px;font-size:12.5px;line-height:1.6;color:#9a9aa2">',
        '    存在<b>本机浏览器</b>（localStorage），关掉也不会丢；<br>同时存一份到本机数据目录，让<b>定时任务</b>半夜也能用云端模型。',
        '  </p>',
        '  <input id="dsKeyIn" type="password" placeholder="sk-..." style="width:100%;box-sizing:border-box;padding:9px 11px;border-radius:8px;border:1px solid #3a3a42;background:#141416;color:#e8e8ea;font-size:13px;font-family:monospace">',
        '  <div style="display:flex;gap:14px;margin:12px 0 4px;font-size:12.5px;flex-wrap:wrap">',
        '    <a href="https://platform.deepseek.com/api_keys" target="_blank" rel="noopener" style="color:#5aa9ff">创建 Key</a>',
        '    <a href="https://platform.deepseek.com/top_up" target="_blank" rel="noopener" style="color:#5aa9ff">充值</a>',
        '    <a href="https://platform.deepseek.com/usage" target="_blank" rel="noopener" style="color:#5aa9ff">查余额/用量</a>',
        '    <a href="#" id="dsKeyShow" style="color:#9a9aa2">显示</a>',
        '  </div>',
        '  <div id="dsKeyMsg" style="font-size:12.5px;min-height:18px;color:#f0b34a"></div>',
        '  <div style="display:flex;gap:10px;justify-content:flex-end;margin-top:10px">',
        '    <button id="dsKeyCancel" style="padding:7px 14px;border-radius:8px;border:1px solid #3a3a42;background:transparent;color:#c8c8d0;cursor:pointer">取消</button>',
        '    <button id="dsKeyClear" style="padding:7px 14px;border-radius:8px;border:1px solid #3a3a42;background:transparent;color:#e06c6c;cursor:pointer">清除</button>',
        '    <button id="dsKeySave" style="padding:7px 16px;border-radius:8px;border:none;background:#3b82f6;color:#fff;cursor:pointer;font-weight:600">保存并校验</button>',
        '  </div>',
        '</div>'
      ].join("\n");
      document.body.appendChild(mask);

      const inp = mask.querySelector("#dsKeyIn");
      const msg = mask.querySelector("#dsKeyMsg");
      const close = () => mask.remove();
      inp.value = old;
      setTimeout(() => inp.focus(), 50);

      mask.querySelector("#dsKeyShow").onclick = (e) => {
        e.preventDefault();
        inp.type = inp.type === "password" ? "text" : "password";
        e.target.textContent = inp.type === "password" ? "显示" : "隐藏";
      };
      mask.querySelector("#dsKeyCancel").onclick = close;
      mask.querySelector("#dsKeyClear").onclick = () => {
        localStorage.removeItem("ds_api_key");
        fetch("/api/secret/clear", { method: "POST" }).catch(() => {});
        inp.value = "";
        msg.style.color = "#7bd88f";
        msg.textContent = "已清除本页保存的 Key";
      };
      mask.querySelector("#dsKeySave").onclick = async () => {
        const v = inp.value.trim();
        if (!v) { msg.style.color = "#e06c6c"; msg.textContent = "Key 不能为空"; return; }
        msg.style.color = "#f0b34a";
        msg.textContent = "正在校验…";
        try {
          const r = await fetch("/api/deepseek/check", {
            method: "POST", headers: { "X-DeepSeek-Key": v } });
          const d = await r.json();
          if (d.ok) {
            localStorage.setItem("ds_api_key", v);
            // ★ 2026-11-09：同时存一份到本机 —— 定时任务半夜跑时浏览器不在，
            //   只能靠这一份 Key 才能用云端模型（用户报「定时任务连不上 ollama」）。
            let stored = false;
            try {
              const sr = await (await fetch("/api/secret/save", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ key: v }) })).json();
              stored = !!(sr && sr.ok);
            } catch (e) { /* 存不上也不影响浏览器里用 */ }
            msg.style.color = "#7bd88f";
            msg.textContent = "✅ 可用" + (d.balance ? "，余额 " + d.balance + " " + d.currency : "")
              + (stored ? "（已存到本机，定时任务也能用）"
                        : "（本机保管失败，定时任务仍用本地模型）");
            setTimeout(close, 900);
          } else {
            msg.style.color = "#e06c6c";
            msg.textContent = "❌ " + (d.msg || "校验失败");
          }
        } catch (e) {
          msg.style.color = "#e06c6c";
          msg.textContent = "❌ 校验请求失败：" + e.message;
        }
      };
      mask.addEventListener("click", (e) => { if (e.target === mask) close(); });
    });
  })();

  // ================== 代码模型下拉（2026-11-08）==================
  // 用户要求：「代码生成与自我迭代用有用户自主切换的模型，一般用 DeepSeek V4.1」。
  // 和聊天模型（#modelSelect）分开：聊天用哪个 ≠ 写代码用哪个。
  (() => {
    const sel = document.querySelector("#codeModelSelect");
    if (!sel) return;

    const shortName = (c) => String(c.name || c.id)
      .replace("（推荐）", "")
      .replace("本地 ", "")
      .replace("DeepSeek ", "DS ")
      .replace("自动（有 DeepSeek Key 就用 V4.1）", "自动")
      .replace("DeepSeek V4.1 Flash", "DS V4.1")
      .replace("DeepSeek V4.1", "DS V4.1")
      .replace("DeepSeek V4 Pro", "DS V4 Pro")
      .replace("本机", "本地");

    async function load() {
      try {
        const d = await (await fetch("/api/code/models")).json();
        const haveKey = !!localStorage.getItem("ds_api_key");
        const inst = Array.isArray(d.installed) ? d.installed : [];
        sel.innerHTML = "";
        for (const c of d.choices) {
          const o = document.createElement("option");
          o.value = c.id;
          const isLocal = c.id.indexOf("local:") === 0;
          const lm = isLocal ? c.id.slice(6) : "";
          const okLocal = !isLocal || inst.indexOf(lm) >= 0;
          // 顶栏空间有限：auto 直接叫「自动」，其余用 shortName 压一压
          o.textContent = (isLocal ? "💻 " : "☁ ")
            + (c.id === "auto" ? "自动" : shortName(c))
            + (isLocal && !okLocal ? "（未下载）"
               : (!isLocal && c.id !== "auto" && !haveKey ? "（缺Key）" : ""));
          if (isLocal && !okLocal) o.disabled = true;
          sel.appendChild(o);
        }
        sel.value = d.current || "auto";
        sel.title = "写代码/自我迭代用：" + (sel.options[sel.selectedIndex] || {}).text
          + "\n（聊天用哪个模型是右边那个下拉，两者独立）";
      } catch (e) { /* ignore */ }
    }

    sel.onchange = async () => {
      try {
        const r = await (await fetch("/api/code/model", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ model: sel.value }) })).json();
        if (r.ok) {
          const isDS = String(sel.value).indexOf("deepseek") === 0;
          const hasKey = !!localStorage.getItem("ds_api_key");
          showToast("代码模型 → " + (sel.options[sel.selectedIndex] || {}).text
                    + (isDS && !hasKey ? "（还没填 Key，会退回本地）" : ""),
                    isDS && !hasKey ? "warn" : "ok");
        } else showToast(r.msg || "切换失败", "err");
      } catch (e) { showToast("切换失败：" + e.message, "err"); }
      load();
    };

    load();
  })();

  // ================== 定时任务面板（2026-11-08）==================
  // 用户要求：「智能体能够实现长期自动化或定时化任务」。
  // 后端调度线程每 20 秒检查一次到期任务，结果写回会话；这里只做管理界面。
  (() => {
    const list = document.querySelector("#schedList");
    const badge = document.querySelector("#schedBadge");
    const fold = document.querySelector("#schedFold");
    const body = document.querySelector("#schedBody");
    const float = document.querySelector("#schedFloat");
    if (!list) return;

    // ★ 2026-11-11：定时任务改成**独立浮层**（用户要求：不占对话/侧栏位置，
    //   「单开一个框，只可以读不能聊天」）。开合入口是顶栏那颗 ⏰（与「⚙ 上下文」
    //   同一套 pill 样式，插在 #selfDevBtn 前面，不改 index.html 的顶栏结构）。
    const openFloat = (on) => {
      if (!float) return;
      float.hidden = !on;
      if (on) load();
    };
    // 2026-11-13 用户要求：「定时任务移动到左侧工具栏」——
    // 原来这里往顶栏插一颗 ⏰ pill（后来被抽屉脚本收进「开发与工具」组，
    // 要翻两层才点得到）。现在入口是侧栏的 #autoPanel 分组：
    // 侧栏看概览和检索、点 ⤢ 开这个弹窗看全文，两个入口都能用。
    // 所以这里**不再创建任何顶栏按钮**。
    const closeBtn = document.querySelector("#schedClose");
    if (closeBtn) closeBtn.onclick = () => openFloat(false);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && float && !float.hidden) openFloat(false);
    });

    fold.onclick = () => {
      body.hidden = !body.hidden;
      fold.textContent = body.hidden ? "▸" : "▾";
    };

    // ★ 2026-11-11：结果查看窗。用户反馈「定时任务看不到结果在哪」——
    //   原来只有 last_preview 那 200 字，正文要翻对话才找得到。
    //   这里点「看结果」直接把最近一次的完整正文拉出来，只读、可滚动。
    const win = document.querySelector("#schedWin");
    const winBody = document.querySelector("#schedWinBody");
    const winTitle = document.querySelector("#schedWinTitle");
    const closeWin = () => { if (win) win.hidden = true; };
    const winCloseBtn = document.querySelector("#schedWinClose");
    if (winCloseBtn) winCloseBtn.onclick = closeWin;
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && win && !win.hidden) closeWin();
    });
    async function openResult(id, name) {
      if (!win) return;
      win.hidden = false;
      winTitle.textContent = "📄 " + (name || "运行结果");
      winBody.textContent = "读取中…";
      try {
        const d = await (await fetch("/api/schedule/result?id=" + encodeURIComponent(id))).json();
        if (!d || !d.ok) {
          winBody.textContent = "读取失败：" + ((d && d.error) || "未知原因");
          return;
        }
        const txt = d.text ||
          "这个任务还没有跑过，或者上次没留下正文。点「立即运行」跑一次就有结果了。";
        // 2026-11-13 用户要求：「结果窗口里把 markdown 渲染成排版（标题/表格/代码块）」。
        // 直接复用对话里那套 renderFinal —— 它自带"渲染丢了结尾就退回纯文本"的兜底，
        // 比在这里另写一套 markdown 解析稳。万一它不在当前作用域，就退纯文本，不会白屏。
        winBody.textContent = "";
        // renderFinal 产出的 markdown 样式全挂在 .bubble 下（.bubble .md-h / .md-ul …），
        // 所以套一层 .bubble 才能真的复用那套排版；气泡本身的底色/边框/内边距
        // 由 CSS 里的 .sched-md 盖掉，看起来还是这个结果窗，不会多出一个气泡。
        const host = document.createElement("div");
        host.className = "bubble sched-md";
        winBody.appendChild(host);
        try {
          if (typeof renderFinal === "function") {
            renderFinal(host, txt, "");
          } else {
            host.textContent = txt;
            host.style.whiteSpace = "pre-wrap";
          }
        } catch (e) {
          host.textContent = txt;
          host.style.whiteSpace = "pre-wrap";
        }
      } catch (e) {
        winBody.textContent = "读取失败：" + e.message;
      }
    }

    // 2026-11-13 用户要求：「时间精确到分钟，不要输入用选择分档的形式，
    // 也就是 24 档加 60 档那样」。原来是一个手打 HH:MM 的输入框，
    // 打错格式（9:0 / 09：00 / 9点）后端会直接拒。改成两个下拉，永远不会输错。
    const typeSel = document.querySelector("#schedType");
    const hourSel = document.querySelector("#schedHour");
    const minSel = document.querySelector("#schedMin");
    const timeIn = document.querySelector(".sched-time");   // 容器，整体显隐
    const everyIn = document.querySelector("#schedEvery");
    const pad2 = (i) => String(i).padStart(2, "0");
    if (hourSel && !hourSel.options.length) {
      hourSel.innerHTML = Array.from({ length: 24 }, (_, i) =>
        '<option value="' + pad2(i) + '"' + (i === 9 ? " selected" : "") + '>' +
        pad2(i) + " 时</option>").join("");
    }
    if (minSel && !minSel.options.length) {
      minSel.innerHTML = Array.from({ length: 60 }, (_, i) =>
        '<option value="' + pad2(i) + '"' + (i === 0 ? " selected" : "") + '>' +
        pad2(i) + " 分</option>").join("");
    }
    if (typeSel) {
      typeSel.onchange = () => {
        const daily = typeSel.value === "daily";
        if (timeIn) timeIn.hidden = !daily;
        if (everyIn) everyIn.hidden = daily;
      };
    }

    // ---------- 侧栏（⏰ 自动化）与弹窗共用同一套渲染 ----------
    // 2026-11-13 用户要求：「定时任务移动到左侧工具栏」「以卡片的形式展示，
    // 卡片不要太小也不要太大」。所以同一份数据渲染两处：
    //   侧栏 = 紧凑卡片（看结果 / 运行），弹窗 = 完整卡片（含暂停 / 删除）。
    const sideList = document.querySelector("#autoList");
    const sideNote = document.querySelector("#autoBackend");
    const searchIn = document.querySelector("#autoSearch");
    const searchClear = document.querySelector("#autoSearchClear");
    let lastJobs = [];
    let searching = false;

    function itemHtml(j, compact, runningId) {
      const st = (runningId && j.id === runningId) ? "⏳" : (j.enabled ? "🟢" : "⏸️");
      const last = j.last_run
        ? ("上次 " + String(j.last_run).slice(5, 16) + " " + (j.last_status || ""))
        : "还没跑过";
      const prev = j.last_preview
        ? '<div class="sched-prev">' +
          String(j.last_preview).replace(/</g, "&lt;").slice(0, compact ? 56 : 80) + '…</div>'
        : "";
      // 相关度只在检索结果里显示；普通列表里显示会满屏百分比，反而干扰
      const sc = (searching && j.score != null && j.score < 1)
        ? '<span class="sched-score" title="检索相关度">' + Math.round(j.score * 100) + '%</span>'
        : "";
      const acts = compact
        ? '<button data-act="result">看结果</button>' +
          '<button data-act="run">运行</button>'
        : '<button data-act="result">看结果</button>' +
          '<button data-act="run">立即运行</button>' +
          '<button data-act="toggle">' + (j.enabled ? "暂停" : "启用") + '</button>' +
          '<button data-act="del">删除</button>';
      return '<div class="sched-item' + (compact ? " compact" : "") +
        '" data-id="' + j.id + '">' +
        '<div class="sched-hd">' + st + ' <b>' + (j.name || "") + '</b>' + sc + '</div>' +
        '<div class="sched-meta">' + (j.period || "") + ' · 下次 ' + (j.next_human || "-") +
        ' · ' + last + '</div>' +
        (j.channel_hint ? '<div class="sched-meta">上次用：' + j.channel_hint + '</div>' : '') +
        prev +
        '<div class="sched-acts">' + acts + '</div></div>';
    }

    const EMPTY_HTML = '<div class="hint" style="padding:6px 10px">还没有定时任务。' +
      '点下面的「＋ 新建任务」，或者直接在对话里说「每天早上九点帮我总结XX」。</div>';

    function paint(jobs, runningId) {
      if (badge) badge.textContent = String(jobs.length);
      const h = jobs.length ? null : EMPTY_HTML;
      if (list) list.innerHTML = h || jobs.map((j) => itemHtml(j, false, runningId)).join("");
      if (sideList) sideList.innerHTML = h || jobs.map((j) => itemHtml(j, true, runningId)).join("");
    }

    async function load() {
      try {
        const d = await (await fetch("/api/schedule")).json();
        const jobs = d.jobs || [];
        lastJobs = jobs;
        const runningId = (d.status || {}).running || "";
        // 检索状态下不要覆盖结果 —— 否则每 15 秒一次的轮询会把检索结果冲掉
        if (!searching) paint(jobs, runningId);
        else if (badge) badge.textContent = String(jobs.length);
      } catch (e) { /* ignore */ }
    }

    // ---------- 检索（用户要求：「定时任务内容也要支持检索，最好向量化提高召回率」）----------
    // 后端 /api/schedule/search 用 embed.py 的语义向量 + 字符 bigram 覆盖率混合打分，
    // 返回的 backend 字段如实说明这次走的是"向量+关键词"还是"仅关键词"，
    // 界面照实标注，不假装一直在用向量。
    let searchTimer = 0;
    function setNote(t) {
      if (!sideNote) return;
      sideNote.hidden = !t;
      sideNote.textContent = t || "";
    }
    async function doSearch() {
      if (!searchIn) return;
      const q = (searchIn.value || "").trim();
      if (searchClear) searchClear.hidden = !q;
      if (!q) {
        searching = false;
        setNote("");
        paint(lastJobs);
        return;
      }
      searching = true;
      setNote("检索中…");
      try {
        const d = await (await fetch("/api/schedule/search?q=" +
          encodeURIComponent(q))).json();
        if (!d.ok) { setNote("检索失败：" + (d.error || "")); return; }
        const items = d.items || [];
        const bk = d.backend === "vector+keyword" ? "语义向量 + 关键词"
                 : d.backend === "keyword" ? "仅关键词（向量后端不可用）"
                 : "全部";
        setNote("命中 " + items.length + " 条 · " + bk);
        paint(items);
      } catch (e) {
        setNote("检索失败：" + e.message);
      }
    }
    if (searchIn) {
      searchIn.addEventListener("input", () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(doSearch, 320);   // 打字防抖，别每敲一个字打一次后端
      });
      searchIn.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { clearTimeout(searchTimer); doSearch(); }
      });
    }
    if (searchClear) {
      searchClear.onclick = () => { searchIn.value = ""; doSearch(); };
    }

    const onAct = async (ev) => {
      const b = ev.target.closest("button[data-act]");
      if (!b) return;
      const box = b.closest(".sched-item");
      const id = box.dataset.id;
      const act = b.dataset.act;
      b.disabled = true;
      try {
        if (act === "result") {
          const nm = (box.querySelector(".sched-hd b") || {}).textContent || "";
          b.disabled = false;
          openResult(id, nm);
          return;
        }
        if (act === "run") {
          // 2026-11-09：改成"后台跑 + 轮询"。一次要 60~120 秒，
          // 原来同步 await，浏览器早就超时了，用户看到的就是"点了没反应"。
          b.textContent = "运行中…";
          const before = await fetch("/api/schedule").then((x) => x.json())
            .then((d) => ((d.jobs || []).find((x) => x.id === id) || {}).last_run || "")
            .catch(() => "");
          const r = await (await fetch("/api/schedule/run", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ id }) })).json();
          if (!r.ok) {
            showToast("没能启动：" + (r.error || ""), "err");
            b.disabled = false; b.textContent = "立即运行"; return;
          }
          if (r.busy) { showToast(r.msg || "正在跑，稍等", "warn"); b.disabled = false; return; }
          showToast("已开始运行（通常 1~2 分钟），跑完会提示", "ok");
          let n = 0;
          const timer = setInterval(async () => {
            n++;
            try {
              const d = await (await fetch("/api/schedule")).json();
              const job = (d.jobs || []).find((x) => x.id === id) || {};
              if (job.last_run && job.last_run !== before) {
                clearInterval(timer);
                const bad = String(job.last_status || "").startsWith("error");
                showToast(bad ? ("运行失败：" + job.last_status)
                              : ("跑完了，结果已写回会话：「" + (job.name || "") + "」"),
                          bad ? "err" : "ok");
                load();
                if (typeof loadSessions === "function") loadSessions();
              } else if (n >= 100) {
                clearInterval(timer);
                showToast("还在跑（可能任务较重），稍后到会话里看结果", "warn");
                load();
              }
            } catch (e) { /* 忽略单次失败 */ }
          }, 3000);
          return;                       // 别急着把按钮恢复
        } else if (act === "toggle") {
          const cur = b.textContent === "暂停";
          await fetch("/api/schedule/toggle", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ id, enabled: !cur }) });
        } else if (act === "del") {
          if (!confirm("删除这个定时任务？")) { b.disabled = false; return; }
          await fetch("/api/schedule/delete", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ id }) });
        }
      } catch (e) { showToast("操作失败：" + e.message, "err"); }
      b.disabled = false;
      load();
    };

    // 侧栏和弹窗是两个独立容器，但按钮语义完全一样 —— 绑同一个处理器
    if (list) list.onclick = onAct;
    if (sideList) sideList.onclick = onAct;

    // ---------- 侧栏入口：⤢ 打开大窗口 / ＋ 新建任务 ----------
    const autoWinBtn = document.querySelector("#autoWinBtn");
    if (autoWinBtn) autoWinBtn.onclick = () => openFloat(true);
    const autoNewBtn = document.querySelector("#autoNewBtn");
    if (autoNewBtn) {
      autoNewBtn.onclick = () => {
        openFloat(true);
        const det = document.querySelector(".sched-new");
        if (det) det.open = true;
        setTimeout(() => {
          const p = document.querySelector("#schedPrompt");
          if (p) p.focus();
        }, 140);
      };
    }

    const addBtn = document.querySelector("#schedAdd");
    if (addBtn) {
      addBtn.onclick = async () => {
        const name = (document.querySelector("#schedName").value || "").trim();
        const prompt = (document.querySelector("#schedPrompt").value || "").trim();
        if (!prompt) { showToast("先写清楚到点要做什么", "warn"); return; }
        const daily = (typeSel.value === "daily");
        const body2 = {
          name: name || prompt.slice(0, 16),
          prompt,
          session: (typeof sessionId !== "undefined" ? sessionId : ""),
          model: (document.querySelector("#schedModel") || {}).value || "",
        };
        if (daily) {
          body2.at_time = ((hourSel && hourSel.value) || "09") + ":" +
                          ((minSel && minSel.value) || "00");
        } else {
          body2.every_minutes = parseInt(everyIn.value || "60", 10) || 60;
        }
        addBtn.disabled = true;
        try {
          const r = await (await fetch("/api/schedule", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body2) })).json();
          if (r.ok) {
            showToast("已添加定时任务", "ok");
            document.querySelector("#schedName").value = "";
            document.querySelector("#schedPrompt").value = "";
            load();
          } else showToast(r.error || "添加失败", "err");
        } catch (e) { showToast("添加失败：" + e.message, "err"); }
        addBtn.disabled = false;
      };
    }

    load();
    setInterval(load, 15000);
  })();

  // ================== 服务器是否已配好 Key（2026-11-09）==================
  // 服务器版把 Key 放在 /etc/multimodal.env 里 —— 浏览器就不用再填一次。
  (() => {
    fetch("/api/ds/env").then((r) => r.json()).then((d) => {
      window.__dsEnv = !!(d && d.has_key);
      if (window.__dsEnv) {
        const b = document.querySelector("#dsKeyBtn");
        if (b) { b.textContent = "🔑 已配置"; b.title = "服务器环境变量里已有 Key，本机不用再填"; }
      }
    }).catch(() => {});
  })();

  // ================== Key 状态探针（2026-11-08）==================
  // 只上报"有没有 Key"这个布尔值，**不上报 Key 本身**。
  // 用途：用户说"我明明填了"，服务端就能确认浏览器里到底有没有。
  (() => {
    const report = () => fetch("/api/ds/seen", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ has_key: !!localStorage.getItem("ds_api_key") }),
    }).catch(() => {});
    report();
    window.addEventListener("focus", report);
  })();

  // ================== 顶栏：性能档位 / 上下文（2026-11-08）==================
  // 用户反馈：这俩是日常聊天就要调的东西，不该藏在「🛠 自开发」面板里 —— 挪到顶栏。
  // 控件由本脚本插到 #selfDevBtn 前面（index.html 不动），面板里那份已删除，不留重复控件。
  (() => {
    const bar = document.querySelector(".tools-bar");
    const anchor = document.querySelector("#selfDevBtn");
    if (!bar || !anchor) return;
    const sel = document.createElement("select");
    sel.id = "perfSelect";
    sel.className = "pill pill-select";
    sel.title = "性能档位：决定默认的输出上限与上下文长度";
    const ctxBtn = document.createElement("button");
    ctxBtn.type = "button";
    ctxBtn.id = "perfCtxBtn";
    ctxBtn.className = "pill pill-btn";
    ctxBtn.title = "手动指定上下文长度（越大越能装长内容，但更吃显存、更慢）";
    ctxBtn.textContent = "⚙ 上下文 —";
    bar.insertBefore(sel, anchor);
    bar.insertBefore(ctxBtn, anchor);

    let last = null, panel = null, cloudCap = null;   // 云模型的真实能力（1M/393k）
    const show = (v, override) => v + (override > 0 ? "（自定义）" : "（跟随档位）");
    const paint = (d) => {
      last = d;
      sel.innerHTML = (d.profiles || []).map((x) => '<option value="' + x.id + '">' + x.name + "</option>").join("");
      if (d.profile) sel.value = d.profile;
      ctxBtn.textContent = "⚙ 上下文 " + Math.round(Number(d.num_ctx || 0) / 1024) + "k";
      // 自动读取到的模型上限，直接写在按钮 title 里（用户要求：先探测再给选项）
      try {
        const det = d.detected || {};
        if (det.model) {
          ctxBtn.title = "当前模型 " + det.model + "：" + Math.round((det.max_ctx || 0) / 1024)
            + "k 上下文上限（已自动读取）· 点这里手动调";
          sel.title = "性能档位（当前 " + d.name + "，输出上限 " + d.max_tokens
            + "；模型 " + det.model + " 支持 " + Math.round((det.max_ctx || 0) / 1024) + "k）";
        }
      } catch (e) {}
      if (!panel) return;
      const r = panel.querySelector("#perfCtxRange"), now = panel.querySelector("#perfCtxNow");
      if (r) { r.min = d.min_ctx; r.max = d.max_ctx; r.value = d.num_ctx; }
      if (now) now.textContent = show(d.num_ctx, d.override);
    };
    const post = async (body, okMsg) => {
      try {
        const r = await (await fetch("/api/perf", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
        if (r.ok) {
          paint(r);
          showToast(okMsg, "ok");
          // ★ 上下文长度变了 → 占用比的分母就变了，通知圆环重算
          window.dispatchEvent(new CustomEvent("perf-changed", { detail: r }));
        } else showToast(r.error || "设置失败", "warn");
      } catch (e) { showToast("设置失败：" + e.message, "warn"); }
    };
    sel.onchange = () => post({ profile: sel.value }, "性能档位 → " + sel.options[sel.selectedIndex].text);
    ctxBtn.onclick = () => {
      if (panel) { panel.remove(); panel = null; return; }
      const d = last || { num_ctx: 40960, min_ctx: 4096, max_ctx: 131072, override: 0 };
      panel = document.createElement("div");
      panel.id = "perfPanel";
      panel.style.cssText = "position:fixed;right:16px;bottom:90px;width:300px;background:#1e1e22;border:1px solid #33333c;border-radius:12px;padding:14px;z-index:99999;color:#e8e8ea;font-size:12.5px;box-shadow:0 16px 40px rgba(0,0,0,.5)";
      panel.innerHTML =
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px"><b>上下文长度</b>' +
          '<span id="perfCtxNow" style="color:#5aa9ff">' + show(d.num_ctx, d.override) + "</span></div>" +
        '<input id="perfCtxRange" type="range" step="1024" min="' + d.min_ctx + '" max="' + d.max_ctx + '" value="' + d.num_ctx + '" style="width:100%;accent-color:#4f8bf9">' +
        '<div style="font-size:11px;color:#8a8a94;margin:6px 0 10px;line-height:1.6">拖动后松手才生效；「跟随档位」= 不单独指定。</div>' +
        '<div style="display:flex;gap:8px;justify-content:flex-end">' +
          '<button id="perfCtxFollow" type="button" style="padding:5px 10px;border-radius:8px;border:1px solid #3a4353;background:transparent;color:#9ad0ff;cursor:pointer">跟随档位</button>' +
          '<button id="perfCtxClose" type="button" style="padding:5px 10px;border-radius:8px;border:1px solid #3a3a42;background:transparent;color:#c8c8d0;cursor:pointer">关闭</button>' +
        "</div>";
      document.body.appendChild(panel);
      const r = panel.querySelector("#perfCtxRange"), now = panel.querySelector("#perfCtxNow");
      r.oninput = () => { now.textContent = r.value + "（自定义）"; };   // 拖动只改文字
      r.onchange = () => post({ num_ctx: parseInt(r.value, 10) }, "上下文 → " + r.value);   // 松手才 POST
      panel.querySelector("#perfCtxFollow").onclick = () => post({ num_ctx: 0 }, "上下文 → 跟随档位");
      panel.querySelector("#perfCtxClose").onclick = () => { panel.remove(); panel = null; };
    };
    (async () => {
      try { paint(await (await fetch("/api/perf")).json()); } catch (e) { /* 占位 */ }
      // ★ 再读一次**云模型**的真实参数（用户：V4.1 是 1M，别写死 128k）
      try {
        const key = localStorage.getItem("ds_api_key") || "";
        if (!key) return;
        const c = await (await fetch("/api/perf/cloud", {
          method: "POST", headers: { "X-DeepSeek-Key": key } })).json();
        if (!c.ok || !(c.models || []).length) return;
        const m = c.models.find((x) => x.id === "deepseek-flash") || c.models[0];
        cloudCap = m;
        if (last) {
          last.max_ctx = Math.max(Number(last.max_ctx || 0), Number(m.context_window || 0));
          last.max_out = Number(m.max_output_tokens || 0);
          last.cloud = m;
          ctxBtn.title = "云模型 " + (m.name || m.id) + "：上下文 "
            + Math.round((m.context_window || 0) / 1024) + "k、输出上限 "
            + Math.round((m.max_output_tokens || 0) / 1024) + "k（已自动读取）"
            + (m.modalities && m.modalities.indexOf("image") >= 0 ? " · 支持图片输入" : "");
          paint(last);
        }
      } catch (e) { /* 读不到就按本地能力 */ }
    })();
  })();

  // ================== 顶栏：完全访问开关（2026-11-08）==================
  // 用户要求：这不是"只在自开发里用"的开关 —— 日常让模型改后端/写文件也要它，
  // 所以放在顶栏，和 🛠 面板里那个读同一个后端状态（两边都能开关）。
  (() => {
    const bar = document.querySelector(".tools-bar");
    const anchor = document.querySelector("#selfDevBtn");
    if (!bar || !anchor) return;
    const b = document.createElement("button");
    b.type = "button";
    b.id = "fullAccessBtn";
    b.className = "pill pill-btn";
    b.title = "完全访问：允许模型改写 backend/ 等核心代码（关着只能改 frontend/ 与新建文件）";
    bar.insertBefore(b, anchor);
    let on = false;
    const paint = () => {
      b.textContent = on ? "🔓 完全访问·开" : "🔓 完全访问·关";
      b.style.color = on ? "#f0b34a" : "";
      b.style.borderColor = on ? "#7a5a20" : "";
    };
    const load = async () => {
      try { on = !!(await (await fetch("/api/selfdev/fullaccess")).json()).on; } catch (e) {}
      paint();
    };
    load();
    b.onclick = async () => {
      const tip = on
        ? "关闭后模型只能改 frontend/，改 backend/ 会被拒绝。确定关闭？"
        : "打开后模型可以改写后端核心代码（backend/），改坏了应用可能起不来。确定打开？";
      if (!confirm(tip)) return;
      b.disabled = true;
      try {
        const r = await (await fetch("/api/selfdev/fullaccess", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ on: !on }) })).json();
        if (r.ok) { on = !!r.on; paint(); showToast(r.msg || "已切换", "ok"); }
        else showToast("切换失败", "err");
      } catch (e) { showToast("切换失败：" + e.message, "err"); }
      b.disabled = false;
    };
    window.addEventListener("focus", load);
  })();

  // 服务器版锁定自开发 → 把 🛠 / 🔓 两个入口都藏起来（2026-11-09）
  (async () => {
    try {
      const d = await (await fetch("/api/selfdev/status")).json();
      if (d && d.locked) {
        ["#selfDevBtn", "#fullAccessBtn"].forEach((sel) => {
          const el = document.querySelector(sel);
          if (el) el.style.display = "none";
        });
        window.__selfdevLocked = true;
      }
    } catch (e) { /* 忽略 */ }
  })();

  // ================== 自开发面板（2026-11-08）==================
  // 用户要求：① 回撤要收进「🛠 自开发」里，点开才出现；
  //          ② 每个版本要标明**更新内容 + 日期**；
  //          ③ 回撤能力本身必须活到最后（不能因为某次自我迭代出错就退不回去）。
  // 第 ③ 点靠后端：备份镜像到项目外 + 独立 restore.py（应用起不来也能跑）。
  (() => {
    const btn = document.querySelector("#selfDevBtn");
    if (!btn) return;
    let on = false;

    const paint = () => {
      btn.classList.toggle("on", on);
      btn.textContent = on ? "🛠 自开发·开" : "🛠 自开发";
    };
    (async () => {
      try {
        const d = await (await fetch("/api/selfdev/status")).json();
        on = !!d.enabled;
        paint();
      } catch (e) { /* ignore */ }
    })();

    const fmtRel = (rel) => String(rel || "").split("/").pop();

    btn.addEventListener("click", async () => {
      let st = { enabled: on, mirror: "" };
      let hist = { batches: [], restore: "" };
      let vst = { ok: false };
      let vlist = { ok: false, items: [] };
      try {
        st = await (await fetch("/api/selfdev/status")).json();
        hist = await (await fetch("/api/selfdev/backups")).json();
        on = !!st.enabled;
        paint();
      } catch (e) { /* ignore */ }
      try { vst = await (await fetch("/api/version/status")).json(); } catch (e) { /* ignore */ }
      try { vlist = await (await fetch("/api/version/list?limit=30")).json(); } catch (e) { /* ignore */ }

      const mask = document.createElement("div");
      mask.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:99999;display:flex;align-items:center;justify-content:center;padding:16px";
      mask.innerHTML = [
        '<div style="background:#1e1e22;color:#e8e8ea;border-radius:14px;padding:20px 22px;width:min(640px,96vw);max-height:86vh;overflow:auto;box-shadow:0 20px 60px rgba(0,0,0,.55)">',
        '  <h3 style="margin:0 0 10px;font-size:16px">🛠 自开发</h3>',
        '  <div style="display:flex;align-items:center;gap:12px;padding:10px 12px;border:1px solid #2f3a4a;border-radius:10px;background:rgba(30,34,42,.6)">',
        '    <div style="flex:1;min-width:0">',
        '      <div style="font-size:13px">状态：<b id="sdState"></b></div>',
        '      <div style="font-size:11.5px;color:#8f8f99;margin-top:3px;line-height:1.6">',
        '        打开后模型能读写本应用源码并重启自己。改坏了用下面的历史回撤。</div>',
        '    </div>',
        '    <button id="sdToggle" style="flex:0 0 auto;padding:7px 14px;border-radius:8px;border:1px solid #3a3a42;background:transparent;color:#c8c8d0;cursor:pointer"></button>',
        '    <button id="sdDemo" type="button" title="用顶栏选定的代码模型真跑一次写文件，验证自我迭代能力（只写 dev_note.md，不碰源码）" style="flex:0 0 auto;padding:7px 12px;border-radius:8px;border:1px solid #2a4a6a;background:transparent;color:#5aa9ff;cursor:pointer;white-space:nowrap">🧪 试一次迭代</button>',
        '    <button id="sdFull" type="button" title="完全访问：允许模型改写 backend/ 核心代码（风险自负）" style="flex:0 0 auto;padding:7px 12px;border-radius:8px;border:1px solid #3a3a42;background:transparent;color:#9a9aa2;cursor:pointer;white-space:nowrap">🔓 完全访问·关</button>',
        '  </div>',
        '  <div id="sdVerBar" style="display:flex;align-items:center;gap:8px;margin-top:10px;padding:8px 12px;border:1px solid #2f3a4a;border-radius:10px;background:rgba(26,30,38,.65)">',
        '    <span id="sdVerTxt" style="flex:1;min-width:0;font-size:12px;color:#cfd6e2;line-height:1.6">当前版本：读取中…</span>',
        '    <button id="sdUndo" type="button" style="display:none;flex:0 0 auto;padding:6px 10px;border-radius:7px;border:1px solid #4a5a3a;background:transparent;color:#9fd88f;cursor:pointer;font-size:12px;white-space:nowrap">↩ 撤销上次回撤</button>',
        '  </div>',
        '  <div style="font-size:12.5px;color:#9a9aa2;margin:16px 0 6px">版本历史（git 快照，脚本直改也记；点「回滚」整批还原）</div>',
        '  <div id="sdHist"></div>',
        '  <div id="sdMsg" style="font-size:12.5px;min-height:18px;color:#f0b34a;margin-top:8px"></div>',
        '  <div id="sdFoot" style="font-size:11px;color:#6f7787;line-height:1.7;margin-top:10px;border-top:1px solid #2a323e;padding-top:8px;word-break:break-all"></div>',
        '  <div style="display:flex;justify-content:flex-end;margin-top:10px">',
        '    <button id="sdClose" style="padding:7px 16px;border-radius:8px;border:1px solid #3a3a42;background:transparent;color:#c8c8d0;cursor:pointer">关闭</button>',
        '  </div>',
        '</div>',
      ].join("\n");
      document.body.appendChild(mask);

      const close = () => mask.remove();
      const stateEl = mask.querySelector("#sdState");
      const tgEl = mask.querySelector("#sdToggle");
      const list = mask.querySelector("#sdHist");
      const msg = mask.querySelector("#sdMsg");
      const verTxt = mask.querySelector("#sdVerTxt");
      const undoBtn = mask.querySelector("#sdUndo");
      const say = (t, c) => { msg.style.color = c || "#f0b34a"; msg.textContent = t; };

      const applyState = () => {
        stateEl.textContent = on ? "已开启" : "已关闭";
        stateEl.style.color = on ? "#7bd88f" : "#9a9aa2";
        tgEl.textContent = on ? "关闭自开发" : "打开自开发";
      };
      applyState();


      // ---- 用当前代码模型真跑一次迭代（只写 dev_note.md，零风险）----
      const demoBtn = mask.querySelector("#sdDemo");
      // ---- 完全访问开关（后端接口已就绪）----
      const faBtn = mask.querySelector("#sdFull");
      let faOn = false;
      const faPaint = () => {
        faBtn.textContent = faOn ? "🔓 完全访问·开" : "🔓 完全访问·关";
        faBtn.style.color = faOn ? "#f0b34a" : "#9a9aa2";
        faBtn.style.borderColor = faOn ? "#7a5a20" : "#3a3a42";
      };
      const toast = (m, k) => {
        try {
          if (typeof showToast === "function") showToast(m, k || "ok");
          else if (window.showToast) window.showToast(m, k || "ok");
        } catch (e) {}
      };
      (async () => {
        try { const d = await (await fetch("/api/selfdev/fullaccess")).json(); faOn = !!d.on; }
        catch (e) { /* ignore */ }
        faPaint();
      })();
      faBtn.onclick = async () => {
        const tip = faOn
          ? "关闭「完全访问」后，模型只能改 frontend/，改 backend/ 会被拒绝。\n\n确定关闭吗？"
          : "打开「完全访问」＝允许模型改写后端核心代码（backend/），改坏了应用可能起不来。\n\n风险自负，确定打开吗？";
        if (!confirm(tip)) return;
        faBtn.disabled = true;
        try {
          const r = await (await fetch("/api/selfdev/fullaccess", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ on: !faOn }) })).json();
          if (r.ok) {
            faOn = !!r.on; faPaint();
            toast(r.msg || (faOn ? "完全访问已开启" : "完全访问已关闭"));
          } else toast("切换失败", "err");
        } catch (e) { toast("切换失败：" + e.message, "err"); }
        faBtn.disabled = false;
      };
      if (demoBtn) demoBtn.onclick = async () => {
        demoBtn.disabled = true;
        demoBtn.textContent = "迭代中…";
        try {
          const r = await (await fetch("/api/selfdev/demo", {
            method: "POST",
            headers: { "X-DeepSeek-Key": localStorage.getItem("ds_api_key") || "" },
          })).json();
          if (r.ok) {
            say("✅ 迭代成功｜模型：" + r.who + "｜写入 " + r.rel
                + "（" + r.chars + " 字）\n" + (r.preview || "").slice(0, 120),
                "#7bd88f");
            demoBtn.textContent = "🧪 再试一次";
          } else {
            say("❌ 迭代失败｜模型：" + (r.who || "?") + "｜" + (r.error || ""), "#e06c6c");
            demoBtn.textContent = "🧪 试一次迭代";
          }
        } catch (e) {
          say("❌ " + e.message, "#e06c6c");
          demoBtn.textContent = "🧪 试一次迭代";
        }
        demoBtn.disabled = false;
      };

      tgEl.onclick = async () => {
        if (!on) {
          if (!confirm(
            "打开「自开发」后，模型将能够：\n" +
            "· 读/改本应用的源码（backend/、frontend/）\n" +
            "· 重启应用让改动生效\n\n" +
            "⚠️ 每次改动前都会自动备份（本机 + 项目外各一份），改坏了可以回撤。\n\n确定打开吗？"
          )) return;
        }
        try {
          const r = await (await fetch("/api/selfdev/toggle", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ on: !on }) })).json();
          if (r.ok) {
            on = !!r.enabled;
            applyState();
            paint();
            say(r.msg || "", on ? "#7bd88f" : "#9a9aa2");
          } else say("切换失败", "#e06c6c");
        } catch (e) { say("切换失败：" + e.message, "#e06c6c"); }
      };

      // ---- 版本历史：读 git 快照（2026-11-10 改）----
      // 旧版读的是 selfdev 改动日志，只记「用 selfdev 工具改的」；
      // 用脚本直接写文件（run_python）的改动完全记不到 —— 用户发现「最近几次没更新」。
      // git 快照不挑工具，任何改动在重启前都会被提交，所以改成读 git。
      const btnStyle = "flex:0 0 auto;padding:5px 10px;border-radius:7px;background:transparent;cursor:pointer;font-size:12px";
      const verPaint = () => {
        const run = (vst && vst.running) || {};
        const cur = (vst && vst.current) || {};
        let txt;
        if (!run.ok) {
          txt = "当前版本：读不到（" + ((vst && vst.error) || "git 不可用") + "）";
          verTxt.style.color = "#e0a0a0";
        } else {
          txt = "当前正在用：v" + (vst.total || "?") + " · " + (run.hash || "-")
            + (run.boot_at ? "（" + String(run.boot_at).slice(5, 16) + " 启动）" : "");
          txt += cur.dirty ? " · 有 " + cur.dirty + " 处未提交改动（重启前自动存快照）" : " · 工作区干净";
          verTxt.style.color = "#cfd6e2";
        }
        verTxt.textContent = txt;
        const u = vst && vst.undo;
        undoBtn.style.display = (u && u.ok) ? "" : "none";
        if (u && u.ok) undoBtn.title = "回到回滚前（" + String(u.undo.restore_hash || "").slice(0, 7) + "）";
      };
      const render = (data) => {
        const bs = (data && data.items) || [];
        if (!bs.length) {
          list.innerHTML = '<div style="font-size:12.5px;color:#8f8f99;padding:6px 0">还没有版本。模型改过一次源码、重启后，这里就会出现。</div>';
          return;
        }
        list.innerHTML = bs.map((b) => {
          const h = b.hash;
          return '<div style="display:flex;gap:10px;align-items:flex-start;padding:9px 0;border-top:1px solid #2a2a31">' +
            '<div style="flex:1;min-width:0">' +
            '<div style="font-size:12.5px;color:#cfd6e2">' + (b.subject || "（没有说明）") + '</div>' +
            '<div style="font-size:11.5px;color:#828b99;margin-top:3px;line-height:1.6">' +
            '<span style="font-family:ui-monospace,monospace">' + h + '</span> · ' + (b.time || '') +
            ' · ' + (b.files || 0) + ' 个文件</div>' +
            '</div>' +
            '<button data-hash="' + h + '" style="' + btnStyle + ';border:1px solid #4a3a3a;color:#e0a0a0">回滚</button>' +
            '<button data-detail="' + h + '" style="' + btnStyle + ';border:1px solid #3a4a5a;color:#8ab6e0">看改动</button>' +
            '</div>' +
            '<div class="sd-batch-diff" data-for="' + h + '" hidden></div>';
        }).join("");
      };
      verPaint();
      render(vlist);

      // 「撤销上次回撤」：回到回滚前那一刻（后端回滚时会再存一个还原点，可反复）
      undoBtn.onclick = async () => {
        if (!confirm("确定撤销上一次回滚、把代码恢复到回滚前吗？\n\n点「确定」后：立刻恢复 → 自动重启应用（会短暂断开一下）")) return;
        undoBtn.disabled = true;
        undoBtn.textContent = "撤销中…";
        try {
          const r = await (await fetch("/api/version/undo", { method: "POST" })).json();
          say(r.ok ? "✅ " + (r.msg || "已撤销回滚") : "❌ " + (r.error || "撤销失败"),
              r.ok ? "#7bd88f" : "#e06c6c");
          if (r.ok) {
            try { vst = await (await fetch("/api/version/status")).json(); } catch (e) {}
            try { vlist = await (await fetch("/api/version/list?limit=30")).json(); } catch (e) {}
            verPaint(); render(vlist);
            if (window.__restartAppNow) setTimeout(() => window.__restartAppNow(), 500);
          }
        } catch (e) { say("❌ " + e.message, "#e06c6c"); }
        undoBtn.disabled = false;
        undoBtn.textContent = "↩ 撤销上次回撤";
      };

      list.onclick = async (ev) => {
        // ---- git 版本：回滚 / 看改动 ----
        const rb = ev.target.closest("button[data-hash]");
        if (rb) {
          const h = rb.dataset.hash;
          if (!confirm("确定回滚到版本 " + h + " 吗？\n\n· 回滚前会自动存一个还原点，后悔可点「↩ 撤销上次回撤」\n· 点「确定」后：立刻回滚 → 自动重启应用（会短暂断开一下）")) return;
          rb.disabled = true; rb.textContent = "回滚中…";
          try {
            const r = await (await fetch("/api/version/rollback", {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ hash: h }) })).json();
            if (r.ok) {
              rb.textContent = "✅ 已回滚";
              say("✅ " + (r.msg || "已回滚") + "，正在自动重启…", "#7bd88f");
              try { vst = await (await fetch("/api/version/status")).json(); } catch (e) {}
              try { vlist = await (await fetch("/api/version/list?limit=30")).json(); } catch (e) {}
              verPaint(); render(vlist);
              if (window.__restartAppNow) { setTimeout(() => window.__restartAppNow(), 500); return; }
            } else {
              rb.disabled = false; rb.textContent = "回滚";
              say("❌ " + (r.error || "回滚失败"), "#e06c6c");
            }
          } catch (e) { rb.disabled = false; rb.textContent = "回滚"; say("❌ " + e.message, "#e06c6c"); }
          return;
        }
        const dd = ev.target.closest("button[data-detail]");
        if (dd) {
          const box = list.querySelector('[data-for="' + dd.dataset.detail + '"]');
          if (!box) return;
          if (!box.hidden) { box.hidden = true; dd.textContent = "看改动"; return; }
          dd.disabled = true; dd.textContent = "读取…";
          let html = "";
          try {
            const d = await (await fetch("/api/version/detail?hash=" + encodeURIComponent(dd.dataset.detail))).json();
            if (!d.ok) html = '<div class="sd-diff-hd">读不到：' + (d.error || "") + '</div>';
            else html = (d.files || []).map((f) =>
              '<div class="sd-diff-hd">' + f.rel + ' <span style="color:#5fd08a">+' + f.added +
              '</span> <span style="color:#e07a7a">−' + f.deleted + '</span></div>').join("")
              || '<div class="sd-diff-hd">这个版本没有文件变更</div>';
          } catch (e) { html = '<div class="sd-diff-hd">读取失败：' + e.message + '</div>'; }
          box.innerHTML = html;
          box.hidden = false;
          dd.disabled = false; dd.textContent = "收起改动";
          return;
        }

        const db = ev.target.closest("button[data-diff]");
        if (db) {
          const box = list.querySelector('[data-for="' + db.dataset.diff + '"]');
          if (!box) return;
          if (!box.hidden) { box.hidden = true; db.textContent = "看改动"; return; }
          db.disabled = true; db.textContent = "读取…";
          const rels = String(db.dataset.files || "").split(",").filter(Boolean);
          let html = "";
          for (const rel of rels) {
            try {
              const d = await (await fetch("/api/selfdev/diff?batch=" + encodeURIComponent(db.dataset.diff)
                + "&rel=" + encodeURIComponent(rel))).json();
              if (!d.ok) { html += '<div class="sd-diff-hd">' + rel + "（" + (d.error || "读不到") + "）</div>"; continue; }
              html += '<div class="sd-diff-hd">' + rel
                + ' <span style="color:#5fd08a">+' + d.added + '</span> <span style="color:#e07a7a">−' + d.removed + "</span></div>"
                + '<pre class="sd-pre" style="max-height:220px">' + String(d.diff || "")
                    .replace(/&/g, "&amp;").replace(/</g, "&lt;") + "</pre>";
            } catch (e) { html += '<div class="sd-diff-hd">' + rel + "（失败）</div>"; }
          }
          box.innerHTML = html || '<div class="sd-diff-hd">没有可显示的内容</div>';
          box.hidden = false;
          db.disabled = false; db.textContent = "收起改动";
          return;
        }
        const t = ev.target.closest("button[data-batch]");
        if (!t) return;
        const batch = t.dataset.batch;
        if (!confirm("确定回撤到「" + batch + "」这次改动之前吗？\n\n· 这一批改过的文件都会被还原\n· 点「确定」后：立刻回撤 → 自动重启应用（会短暂断开一下）")) return;
        t.disabled = true; t.textContent = "回撤中…";
        try {
          const r = await (await fetch("/api/selfdev/rollback_batch", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ batch: batch }) })).json();
          if (r.ok) {
            t.textContent = "✅ 已回撤";
            say("✅ " + (r.msg || "已回撤") + "，正在自动重启…", "#7bd88f");
            if (window.__restartAppNow) { setTimeout(() => window.__restartAppNow(), 500); return; }
          } else {
            t.disabled = false; t.textContent = "回撤";
            say("❌ " + (r.error || r.msg || "回撤失败"), "#e06c6c");
          }
        } catch (e) {
          t.disabled = false; t.textContent = "回撤";
          say("❌ " + e.message, "#e06c6c");
        }
      };

      // ---- 兜底说明：应用起不来时怎么退 ----
      const foot = mask.querySelector("#sdFoot");
      const mirror = hist.mirror || st.mirror || "";
      foot.innerHTML =
        "🛟 <b>最后一道保险</b>：备份同时存在项目外（" + (mirror || "本机") + "），"
        + "并附带独立脚本 restore.py。<br>"
        + "万一某次自我迭代把应用改到起不来，直接双击桌面上的 "
        + "<b>「回滚到上一版.bat」</b> 就能退回（不依赖应用本身）。";

      mask.querySelector("#sdClose").onclick = close;
      mask.addEventListener("click", (e) => { if (e.target === mask) close(); });
    });
  })();

  // ================== 绘图模型：云端 / 本地，可切换、可下载卸载（2026-11-08）==================
  // 用户要求：「可以切换生成和微改图片的模型，就像 LLM 可以切换本地和云端那样，
  //           然后图片模型也一样支持下载和卸载。」
  (() => {
    const btn = document.querySelector("#imgKeyBtn");
    const sel = document.querySelector("#imgModelSelect");
    if (!btn || !sel) return;

    let lastData = null;

    const paintBtn = () => {
      const has = !!localStorage.getItem("img_api_key");
      const backend = localStorage.getItem("img_backend") || "cloud";
      btn.classList.toggle("on", has || backend === "local");
      btn.title = has || backend === "local"
        ? "图片模型 / Key 已配置（点此修改）"
        : "还没配图片 API Key —— 点这里填，否则画不了图";
    };
    paintBtn();

    // ---------- 顶栏下拉：云端 + 本地（已装/未装都列出来）----------
    function renderSelect(d) {
      lastData = d;
      const backend = d.backend || "cloud";
      localStorage.setItem("img_backend", backend);
      const cur = d.cloud.model || "";
      // 以**后端配置为准**同步一次：配置里换了模型（比如在设置里改），
      // 前端 localStorage 别再拿旧名字去覆盖（否则后端改了也不生效）。
      if (cur) localStorage.setItem("img_api_model", cur);
      const _ed = d.cloud.model_edit || "";
      if (_ed) localStorage.setItem("img_api_model_edit", _ed);
      sel.innerHTML = "";
      const oc = document.createElement("option");
      oc.value = "cloud";
      oc.textContent = "☁️ 云端";
      oc.title = "云端模型：" + cur;
      sel.appendChild(oc);
      (d.local || []).forEach((m) => {
        const o = document.createElement("option");
        o.value = "local:" + m.id;
        o.textContent = (m.installed ? "💾 本地 · " : "⬇ 本地 · ") + m.name +
          (m.installed ? "" : "（未下载）");
        o.disabled = !m.installed;
        sel.appendChild(o);
      });
      // 回显当前选择
      const cfgLocal = (d.local || []).find((m) => m.current && m.role === "gen");
      if (backend === "local" && cfgLocal) sel.value = "local:" + cfgLocal.id;
      else sel.value = "cloud";
      paintBtn();
    }

    async function loadModels() {
      try {
        const d = await (await fetch("/api/image/models")).json();
        renderSelect(d);
        autoHeal(d);
        return d;
      } catch (e) { return null; }
    }

    // 自动修「503 没有可用通道」：用你浏览器里的 Key 问服务商有哪些图片模型，
    // 如果当前选的**不在清单里**，就自动切到第一个能用的（并把结果告诉你）。
    // 这是"充了钱还是 503"的唯一解法 —— 那是分组没这个模型的通道，不是余额问题。
    let _healBusy = false;
    async function autoHeal(d) {
      const key = localStorage.getItem("img_api_key") || "";
      if (!key || _healBusy) return;
      if ((d && d.backend) === "local") return;
      const cur = (d && d.cloud && d.cloud.model) || "";
      if (!cur) return;
      _healBusy = true;
      try {
        const r = await (await fetch("/api/image/discover", {
          method: "POST", headers: { "X-Image-Key": key } })).json();
        const cands = r.candidates || [];
        if (!cands.length) {
          // 清单拿到了但一个图片模型都没有 → 这是**分组权限问题**，必须让用户知道，
          // 否则他只会反复看到 503、以为充钱没生效。（每次打开只提示一次）
          if (r.ok && (r.models || []).length && !sessionStorage.getItem("img_nopic_warned")) {
            sessionStorage.setItem("img_nopic_warned", "1");
            showToast("你的图片 Key 只能用这几个模型："
                      + (r.models || []).slice(0, 3).join("、")
                      + " —— 里面没有能画图的，需要换分组/换服务商的 Key", "warn");
          }
          return;
        }
        if (cands.indexOf(cur) >= 0) return;       // 现在这个能用，不动
        const pick = cands[0];
        const sel2 = await (await fetch("/api/image/select", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ backend: "cloud", model: pick, role: "gen" }) })).json();
        if (sel2.ok) {
          localStorage.setItem("img_api_model", pick);
          showToast("原来的图片模型没有可用通道，已自动切换到 " + pick, "ok");
          const d2 = await (await fetch("/api/image/models")).json();
          renderSelect(d2);
        }
      } catch (e) { /* 静默 */ }
      finally { _healBusy = false; }
    }

    sel.onchange = async () => {
      const v = sel.value;
      try {
        if (v === "cloud") {
          const r = await (await fetch("/api/image/select", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ backend: "cloud" }) })).json();
          showToast(r.ok ? "已切到云端绘图" : (r.msg || "切换失败"), r.ok ? "ok" : "err");
        } else {
          const id = v.replace("local:", "");
          const r = await (await fetch("/api/image/select", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ backend: "local", model: id }) })).json();
          showToast(r.ok ? (r.msg || "已切到本地绘制") : (r.msg || "切换失败"),
                    r.ok ? "ok" : "err");
        }
      } catch (e) { showToast("切换失败：" + e.message, "err"); }
      loadModels();
      if (typeof refreshHealth === "function") refreshHealth();
    };

    // ---------- 🎨 面板：Key + 云端模型名 + 本地模型管理 ----------
    btn.addEventListener("click", async () => {
      const oldKey = localStorage.getItem("img_api_key") || "";
      const d = lastData || (await loadModels()) ||
        { cloud: { model: "gpt-image-2.5-flare", base: "https://xingpan.site/v1" }, local: [] };

      const mask = document.createElement("div");
      mask.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:99999;display:flex;align-items:center;justify-content:center;padding:16px";
      mask.innerHTML = [
        '<div style="background:#1e1e22;color:#e8e8ea;border-radius:14px;padding:20px 22px;width:min(560px,96vw);max-height:90vh;overflow:auto;box-shadow:0 20px 60px rgba(0,0,0,.55)">',
        '  <h3 style="margin:0 0 4px;font-size:16px">🎨 绘图模型</h3>',
        '  <p style="margin:0 0 14px;font-size:12px;line-height:1.7;color:#8f8f99">',
        '    云端 = 按张计费、不吃显卡；本地 = 要下载权重、画图时占满显存。<br>',
        '    服务：<b style="color:#c8c8d0">' + d.cloud.base + '</b>',
        '  </p>',

        '  <div style="font-size:12.5px;color:#9a9aa2;margin-bottom:6px">① 云端 API Key</div>',
        '  <input id="imgKeyIn" type="password" placeholder="sk-...（xingpan.site 的 Key）" style="width:100%;box-sizing:border-box;padding:9px 11px;border-radius:8px;border:1px solid #3a3a42;background:#141416;color:#e8e8ea;font-size:13px;font-family:monospace">',

        '  <div style="font-size:12.5px;color:#9a9aa2;margin:12px 0 6px">② 云端模型名（生成 / 微改，不同分组能用的不一样）</div>',
        '  <div style="display:flex;gap:8px">',
        '    <input id="imgModelIn" list="imgModelList" placeholder="gpt-image-2.5-flare" style="flex:1;padding:8px 10px;border-radius:8px;border:1px solid #3a3a42;background:#141416;color:#e8e8ea;font-size:12.5px;font-family:monospace">',
        '    <datalist id="imgModelList"></datalist>',
        '    <button id="imgModelListBtn" type="button" style="padding:7px 11px;border-radius:8px;border:1px solid #3a3a42;background:transparent;color:#c8c8d0;cursor:pointer;white-space:nowrap">列出可用</button>',
        '    <button id="imgAutoBtn" type="button" style="padding:7px 11px;border-radius:8px;border:1px solid #2a4a6a;background:transparent;color:#5aa9ff;cursor:pointer;white-space:nowrap">自动选</button>',
        '  </div>',
        '  <div style="display:flex;gap:8px;margin-top:8px">',
        '    <input id="imgModelEditIn" list="imgModelList" placeholder="微改模型（留空＝同上）" style="flex:1;padding:8px 10px;border-radius:8px;border:1px solid #3a3a42;background:#141416;color:#e8e8ea;font-size:12.5px;font-family:monospace">',
        '  </div>',

        '  <div style="font-size:12.5px;color:#9a9aa2;margin:16px 0 6px">③ 本地模型（下载后可在顶栏切换）</div>',
        '  <div id="imgLocalList"></div>',

        '  <div id="imgTryBox" style="margin-top:10px"></div>',
        '  <div id="imgKeyMsg" style="font-size:12.5px;min-height:18px;color:#f0b34a;margin-top:10px"></div>',
        '  <div style="display:flex;gap:10px;justify-content:flex-end;margin-top:8px">',
        '    <a href="https://xingpan.site" target="_blank" rel="noopener" style="color:#5aa9ff;font-size:12.5px;margin-right:auto;align-self:center">打开服务商</a>',
        '    <button id="imgTryBtn" style="padding:7px 14px;border-radius:8px;border:1px solid #2a4a6a;background:transparent;color:#5aa9ff;cursor:pointer">🧪 试画一张</button>',
        '    <button id="imgKeyCancel" style="padding:7px 14px;border-radius:8px;border:1px solid #3a3a42;background:transparent;color:#c8c8d0;cursor:pointer">关闭</button>',
        '    <button id="imgKeySave" style="padding:7px 16px;border-radius:8px;border:none;background:#3b82f6;color:#fff;cursor:pointer;font-weight:600">保存</button>',
        '  </div>',
        '</div>'
      ].join("\n");
      document.body.appendChild(mask);

      const inp = mask.querySelector("#imgKeyIn");
      const modelIn = mask.querySelector("#imgModelIn");
      const modelEditIn = mask.querySelector("#imgModelEditIn");
      const modelList = mask.querySelector("#imgModelList");
      const localBox = mask.querySelector("#imgLocalList");
      const msg = mask.querySelector("#imgKeyMsg");
      const close = () => mask.remove();
      inp.value = oldKey;
      modelIn.value = d.cloud.model || "";
      modelEditIn.value = d.cloud.model_edit || "";

      const say = (t, c) => { msg.style.color = c || "#f0b34a"; msg.textContent = t; };

      // ---- 本地模型列表（下载 / 卸载 / 进度）----
      function renderLocal(data) {
        const list = (data && data.local) || [];
        const st = (data && data.status) || {};
        localBox.innerHTML = list.map((m) => {
          const pulling = st.status === "pulling" && st.model === m.id;
          const bar = pulling
            ? '<div style="margin-top:6px"><div style="height:6px;background:#2a2a31;border-radius:3px;overflow:hidden">' +
              '<div style="height:100%;width:' + (st.pct || 0) + '%;background:#3b82f6"></div></div>' +
              '<div style="font-size:11px;color:#8f8f99;margin-top:3px">' + (st.pct || 0) + '% · ' +
              (st.speed ? (st.speed / 1048576).toFixed(1) + ' MB/s' : '…') +
              (st.eta ? ' · 剩 ' + Math.round(st.eta / 60) + ' 分' : '') + '</div></div>'
            : '';
          const act = m.installed
            ? '<button data-act="rm" data-id="' + m.id + '" style="padding:5px 10px;border-radius:7px;border:1px solid #5a2a2a;background:transparent;color:#e06c6c;cursor:pointer;font-size:12px">🗑 卸载</button>'
            : (pulling
              ? '<button data-act="stop" style="padding:5px 10px;border-radius:7px;border:1px solid #3a3a42;background:transparent;color:#c8c8d0;cursor:pointer;font-size:12px">停止</button>'
              : '<button data-act="dl" data-id="' + m.id + '" style="padding:5px 10px;border-radius:7px;border:1px solid #2a4a6a;background:transparent;color:#5aa9ff;cursor:pointer;font-size:12px">⬇ 下载</button>');
          return '<div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-top:1px solid #2a2a31">' +
            '<div style="flex:1;min-width:0">' +
            '<div style="font-size:13px">' + (m.installed ? '💾 ' : '') + m.name +
            (m.current ? ' <span style="color:#7bd88f;font-size:11px">当前</span>' : '') + '</div>' +
            '<div style="font-size:11.5px;color:#8f8f99">' + m.note + ' · ' +
            (m.installed ? m.size_gb + ' GB' : '约 ' + m.expect_gb + ' GB') + '</div>' +
            bar + '</div>' + act + '</div>';
        }).join("") || '<div style="font-size:12px;color:#8f8f99">（没有本地模型）</div>';
      }
      renderLocal(d);

      localBox.onclick = async (ev) => {
        const b = ev.target.closest("button");
        if (!b) return;
        const act = b.dataset.act;
        b.disabled = true;
        try {
          if (act === "dl") {
            const r = await (await fetch("/api/image/pull", {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ id: b.dataset.id }) })).json();
            say(r.msg || (r.ok ? "开始下载" : "下载失败"), r.ok ? "#f0b34a" : "#e06c6c");
            watch();
          } else if (act === "stop") {
            await fetch("/api/image/cancel", { method: "POST" });
            say("已请求停止（已下部分保留）", "#f0b34a");
            setTimeout(refresh, 1200);
          } else if (act === "rm") {
            if (!confirm("确定卸载这个本地模型吗？\n重装要重新下载好几 GB。")) { b.disabled = false; return; }
            const r = await (await fetch("/api/image/remove", {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ id: b.dataset.id }) })).json();
            say(r.msg || (r.ok ? "已卸载" : "卸载失败"), r.ok ? "#7bd88f" : "#e06c6c");
            refresh();
          }
        } catch (e) { say("操作失败：" + e.message, "#e06c6c"); }
        b.disabled = false;
      };

      let timer = null;
      async function refresh() {
        const d2 = await (await fetch("/api/image/models")).json();
        renderLocal(d2);
        renderSelect(d2);
      }
      function watch() {
        if (timer) clearInterval(timer);
        timer = setInterval(async () => {
          const st = await (await fetch("/api/image/status")).json();
          if (st.status !== "pulling") {
            clearInterval(timer); timer = null;
            await refresh();
            if (st.status === "done") say("✅ 下载完成", "#7bd88f");
            else if (st.status === "error") say("❌ " + (st.msg || "下载失败"), "#e06c6c");
            return;
          }
          await refresh();
        }, 1000);
      }
      // 打开时如果正在下载，接着显示进度
      fetch("/api/image/status").then((r) => r.json()).then((st) => {
        if (st.status === "pulling") watch();
      });
      mask.addEventListener("click", (e) => {
        if (e.target === mask) { if (timer) clearInterval(timer); close(); }
      });

      // ---- 自动选一个**真的能用**的图片模型 ----
      mask.querySelector("#imgAutoBtn").onclick = async () => {
        const k = inp.value.trim();
        if (!k) { say("先填 Key", "#e06c6c"); return; }
        say("正在问服务商有哪些模型可用…");
        try {
          const r = await (await fetch("/api/image/discover", {
            method: "POST", headers: { "X-Image-Key": k } })).json();
          if (!r.ok) { say("❌ " + (r.msg || "拿不到模型清单"), "#e06c6c"); return; }
          const c = r.candidates || [];
          if (!c.length) {
            say("❌ 你这个分组里**没有任何图片模型** —— 这是分组权限问题，"
                + "换分组或找服务商开通，充钱解决不了", "#e06c6c");
            return;
          }
          modelIn.value = c[0];
          localStorage.setItem("img_api_model", c[0]);
          await fetch("/api/image/select", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ backend: "cloud", model: c[0], role: "gen" }) });
          say("✅ 已切到 " + c[0] + "（共 " + c.length + " 个图片模型可用：" 
              + c.slice(0, 4).join("、") + "）", "#7bd88f");
          await loadModels();
        } catch (e) { say("❌ " + e.message, "#e06c6c"); }
      };

      // ---- 试画一张：一键自证 Key + 模型 + 保存路径 ----
      const tryBox = mask.querySelector("#imgTryBox");
      mask.querySelector("#imgTryBtn").onclick = async (e) => {
        const k = inp.value.trim();
        const mm = modelIn.value.trim();
        if (!k) { say("先填 Key 再试画", "#e06c6c"); return; }
        e.target.disabled = true;
        tryBox.innerHTML = "";
        say("正在试画（约 10~40 秒，按张计费）…");
        const t0 = Date.now();
        try {
          const r = await fetch("/api/t2i/generate", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Image-Key": k,
              "X-Image-Model": mm,
            },
            body: JSON.stringify({
              prompt: "a red fox sitting in an autumn forest, golden hour backlight, "
                    + "photorealistic, 35mm photograph, detailed fur",
              width: 1024, height: 1024,
            }),
          });
          const d = await r.json();
          const secs = ((Date.now() - t0) / 1000).toFixed(1);
          if (d.ok) {
            const sv = d.saved || {};
            tryBox.innerHTML =
              '<img src="data:image/png;base64,' + d.b64 + '" '
              + 'style="width:100%;border-radius:10px;display:block">'
              + '<div style="font-size:11.5px;color:#8f8f99;margin-top:6px;line-height:1.6">'
              + '模型 ' + (d.model || "") + ' · ' + (d.size || "") + ' · ' + secs + ' 秒<br>'
              + (sv.path
                  ? '✅ 已存图库：<span style="color:#7bd88f">' + sv.path + '</span>'
                  : '⚠️ 图出来了但没存进图库（看后端日志）')
              + '</div>';
            say("✅ 云端出图成功，这条链路是通的", "#7bd88f");
          } else {
            tryBox.innerHTML = "";
            say("❌ " + (d.error || "生成失败") + (d.hint ? "｜" + d.hint : ""), "#e06c6c");
          }
        } catch (err) {
          say("❌ " + err.message, "#e06c6c");
        } finally {
          e.target.disabled = false;
        }
      };

      // ---- 列出可用云端模型 ----
      mask.querySelector("#imgModelListBtn").onclick = async () => {
        const k = inp.value.trim();
        if (!k) { say("先填 Key 再列模型", "#e06c6c"); return; }
        say("正在拉取模型清单…");
        try {
          const r = await (await fetch("/api/image/check", {
            method: "POST", headers: { "X-Image-Key": k } })).json();
          if (!r.ok) { say("❌ " + (r.msg || "拉取失败"), "#e06c6c"); return; }
          const all = r.models || [];
          const guess = all.filter((n) => /image|dall|flux|sd|mj|midjourney|seedream|kolors/i.test(n));
          const use = guess.length ? guess : all;
          modelList.innerHTML = use.map((n) => '<option value="' + n + '"></option>').join("");
          say("✅ 共 " + all.length + " 个模型" + (guess.length ? "（" + guess.length + " 个图片相关）" : "") +
              "，点输入框右侧箭头选", "#7bd88f");
        } catch (e) { say("❌ " + e.message, "#e06c6c"); }
      };

      // ---- 保存 ----
      mask.querySelector("#imgKeyCancel").onclick = () => {
        if (timer) clearInterval(timer);
        close();
      };
      mask.querySelector("#imgKeySave").onclick = async () => {
        const v = inp.value.trim();
        const mMain = modelIn.value.trim();
        const mEdit = modelEditIn.value.trim();
        if (v) {
          say("正在校验 Key…");
          try {
            const r = await (await fetch("/api/image/check", {
              method: "POST", headers: { "X-Image-Key": v } })).json();
            if (!r.ok) { say("❌ " + (r.msg || "Key 无效"), "#e06c6c"); return; }
            localStorage.setItem("img_api_key", v);
          } catch (e) { say("❌ 校验失败：" + e.message, "#e06c6c"); return; }
        }
        if (mEdit) localStorage.setItem("img_api_model_edit", mEdit);
        else localStorage.removeItem("img_api_model_edit");
        // 云端模型名写进后端配置（不同 Key 用不同模型）
        try {
          if (mMain) {
            await fetch("/api/image/select", {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ backend: "cloud", model: mMain, role: "gen" }) });
          }
          if (mEdit) {
            await fetch("/api/image/select", {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ backend: "cloud", model: mEdit, role: "inpaint" }) });
          }
        } catch (e) { /* ignore */ }
        localStorage.setItem("img_backend", "cloud");
        say("✅ 已保存", "#7bd88f");
        paintBtn();
        await loadModels();
        setTimeout(close, 700);
      };
    });

    loadModels();
    // 有下载在跑时，顶栏也定期刷新一下状态
    setInterval(() => {
      fetch("/api/image/status").then((r) => r.json()).then((st) => {
        if (st.status === "pulling") loadModels();
      }).catch(() => {});
    }, 3000);
  })();

  // ================== 模型下载管理（2026-11-08）==================
  // 状态机：未下载 → [⬇ 下载] → 下载中（进度条 + 取消）→ 已完成（[🗑 删除]）
  // 下载在**后端线程**里跑，前端每 500ms 轮询 /api/model/status。
  // 为什么不用 SSE：下载可能几十分钟，长连接容易被代理掐断；轮询更稳。
  (() => {
    const box = document.querySelector("#modelDl");
    const btnPull = document.querySelector("#btnModelPull");
    const btnRemove = document.querySelector("#btnModelRemove");
    const prog = document.querySelector("#dlProgress");
    const fill = document.querySelector("#dlFill");
    const pctEl = document.querySelector("#dlPct");
    const spdEl = document.querySelector("#dlSpeed");
    const etaEl = document.querySelector("#dlEta");
    const btnCancel = document.querySelector("#btnDlCancel");
    if (!box || !btnPull) return;

    let pollTimer = null;
    let installedSet = new Set();
    let targetModel = "";

    const fmtSize = (b) => {
      if (!b) return "0 B";
      const u = ["B", "KB", "MB", "GB"];
      let i = 0, n = b;
      while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
      return n.toFixed(i >= 2 ? 1 : 0) + " " + u[i];
    };
    const fmtEta = (s) => {
      s = Number(s || 0);
      if (s <= 0) return "—";
      if (s < 60) return s + "s";
      if (s < 3600) return Math.floor(s / 60) + "m" + (s % 60) + "s";
      return Math.floor(s / 3600) + "h" + Math.floor((s % 3600) / 60) + "m";
    };

    // ⚠️⚠️ 2026-11-11 修：「下载模型」原来直接拿 config.default_model 当目标。
    //   而默认模型现在可以被设成**云端**模型（deepseek-flash）——云端模型名
    //   不是 Ollama 能 pull 的东西，于是点下载就去 `ollama pull deepseek-flash`，
    //   必然失败，用户看到的就是「本地模型下载不了」。
    //   这里只认本地模型名：默认模型是本地就用它，是云端就退回文档里的本地默认 8B。
    const LOCAL_FALLBACK = "qwen3-vl:8b";
    const CLOUD_RE = /^(deepseek|gpt-|chatgpt|o1|o3|o4|claude|gemini|glm|qwen-(max|plus|turbo|flash)|moonshot|kimi|doubao|ernie|hunyuan)/i;
    const isCloudName = (m) => CLOUD_RE.test(String(m || "").trim());

    function pickLocalModel(cfg) {
      const def = String((cfg && cfg.default_model) || "").trim();
      if (def && !isCloudName(def)) return def;
      return LOCAL_FALLBACK;
    }

    // 下载框要跟**顶栏当前选中的本地模型**走：
    //   选「🖥 本地 30B」就该能下载 30B，而不是永远盯着配置里的默认模型。
    //   选「本地·自动」或任意云端模型时，退回配置里的本地默认，再不行用 8B。
    let cfgCache = null;
    function resolveTarget() {
      const el = document.querySelector("#modelSelect");
      const v = el ? String(el.value || "") : "";
      if (v && v !== "auto" && !isCloudName(v)) return v;
      return pickLocalModel(cfgCache);
    }

    async function loadTarget() {
      try {
        const r = await fetch("/api/config");
        const d = await r.json();
        cfgCache = (d && d.config) || d || {};
      } catch (e) {
        cfgCache = null;
      }
      targetModel = resolveTarget();
    }

    async function refreshInstalled() {
      try {
        const r = await fetch("/api/model/installed");
        const d = await r.json();
        installedSet = new Set((d.models || []).map((m) => m.name));
      } catch (e) {
        installedSet = new Set();
      }
      renderDl();
    }

    let dlState = "idle";   // idle | pulling | verifying | done | error | cancelled

    // 三态互斥渲染：下载中 / 已安装 / 未安装
    // 用户要求（2026-11-08）：一切就绪时进度条必须收起，只留「卸载模型」。
    // 之前只用 hidden 控制，但 .dl-btn 有 display:flex，会把 [hidden] 盖掉
    // → 四个控件同时显示（截图里的 bug）。CSS 已加兜底，这里再严格互斥一次。
    function renderDl() {
      box.hidden = false;
      box.title = "当前配置的默认模型：" + targetModel;
      const has = installedSet.has(targetModel);
      const busy = (dlState === "pulling" || dlState === "verifying");

      if (busy) {
        prog.hidden = false;
        btnPull.hidden = true;
        btnRemove.hidden = true;
        return;
      }

      // 非下载态：进度条收起、取消按钮复位
      // （修「正在断开…」永久卡住：以前 cancelled/error 分支不复位文字）
      prog.hidden = true;
      btnCancel.textContent = "取消";
      btnCancel.disabled = false;

      btnPull.hidden = has;        // 已装 → 不显示「下载」
      btnRemove.hidden = !has;     // 未装 → 不显示「卸载/删除」
      // ⚠️⚠️ 2026-11-11 修：按钮的 disabled **必须在这里复位**。
      //   点「下载」时会先 btnPull.disabled = true 防重复点击，而下载失败/取消/
      //   完成之后，唯一会重新渲染的就是这个函数 —— 之前没清 disabled，
      //   结果报错一次按钮就永久变灰、点不动，只能刷新页面。
      btnPull.disabled = false;
      btnRemove.disabled = false;
      if (targetModel) btnPull.textContent = "⬇ 下载 " + targetModel;
    }

    function startPoll() {
      if (pollTimer) return;
      pollTimer = setInterval(async () => {
        try {
          const r = await fetch("/api/model/status");
          const d = await r.json();
          applyStatus(d);
        } catch (e) { /* 网络抖动，下一轮再试 */ }
      }, 500);
    }
    function stopPoll() {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    }

    function applyStatus(d) {
      const st = d.status || "idle";
      // ⚠️ 先同步状态再渲染：render() 靠 dlState 判断该显示进度条还是按钮。
      // 之前忘了这一步，导致 cancelled/error 后 render 仍按旧状态画，
      // 界面上「正在断开…」「下载模型」「删除模型」同时挂着。
      dlState = st;

      if (st === "pulling" || st === "verifying") {
        renderDl();                     // 显示进度条、收起两个按钮
        const p = Number(d.percent || 0);
        fill.style.width = p + "%";
        pctEl.textContent = p.toFixed(1) + "%"
          + (d.total ? "　" + fmtSize(d.completed) + " / " + fmtSize(d.total) : "");
        spdEl.textContent = d.speed ? fmtSize(d.speed) + "/s" : "—";
        etaEl.textContent = "剩余 " + fmtEta(d.eta);
        btnCancel.textContent = st === "verifying" ? "校验中…" : "取消";
        btnCancel.disabled = (st === "verifying");
        return;
      }

      if (st === "done") {
        stopPoll();
        fill.style.width = "100%";
        showToast("✅ 模型下载完成：" + (d.model || targetModel), "ok");
        refreshInstalled();           // 内部 render() → 只剩「卸载模型」
        if (typeof refreshHealth === "function") refreshHealth();
        return;
      }

      if (st === "cancelled") {
        stopPoll();
        showToast("已取消（已下载部分保留，下次自动续传）", "warn");
        refreshInstalled();           // 内部 render() → 取消按钮文字复位
        return;
      }

      if (st === "error") {
        stopPoll();
        showToast("下载失败：" + (d.error || "未知错误"), "err");
        refreshInstalled();
        return;
      }

      // idle 或其他未知状态：收起进度条，按是否已安装决定显示哪个按钮
      stopPoll();
      renderDl();
    }

    btnPull.onclick = async () => {
      btnPull.disabled = true;
      try {
        const r = await fetch("/api/model/pull", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ model: targetModel }),
        });
        const d = await r.json();
        if (!d.ok) {
          showToast(d.msg || "启动下载失败", "err");
          btnPull.disabled = false;
          return;
        }
        // 立刻切到「下载中」态，避免点完之后界面还停在旧状态（有一瞬空窗）
        dlState = "pulling";
        renderDl();
        fill.style.width = "0%";
        pctEl.textContent = "0%";
        spdEl.textContent = "…";
        etaEl.textContent = "计算中";
        btnCancel.disabled = false;
        btnCancel.textContent = "取消";
        startPoll();
      } catch (e) {
        showToast("请求失败：" + e.message, "err");
        btnPull.disabled = false;
      }
    };

    btnCancel.onclick = async () => {
      btnCancel.disabled = true;
      btnCancel.textContent = "正在断开…";
      try {
        await fetch("/api/model/cancel", { method: "POST" });
      } catch (e) { /* ignore */ }
    };

    btnRemove.onclick = async () => {
      if (!confirm("确定删除模型 " + targetModel + " 吗？\n删除后需要重新下载（可能十几 GB）。")) return;
      btnRemove.disabled = true;
      try {
        const r = await fetch("/api/model/remove", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ model: targetModel }),
        });
        const d = await r.json();
        showToast(d.ok ? "已删除 " + targetModel : (d.msg || "删除失败"),
                  d.ok ? "ok" : "err");
        await refreshInstalled();
        if (typeof refreshHealth === "function") refreshHealth();
      } catch (e) {
        showToast("删除请求失败：" + e.message, "err");
      } finally {
        btnRemove.disabled = false;
      }
    };

    // 初始化：读默认模型 → 查已安装 → 若正在下载则接管进度
    (async () => {
      await loadTarget();
      await refreshInstalled();
      // 顶栏换模型 → 下载框跟着换目标（选 30B 就显示「下载 30B」）
      const sel = document.querySelector("#modelSelect");
      if (sel) {
        sel.addEventListener("change", async () => {
          await loadTarget();
          await refreshInstalled();
        });
      }
      try {
        const r = await fetch("/api/model/status");
        const d = await r.json();
        if (d.status === "pulling" || d.status === "verifying") {
          prog.hidden = false;
          btnPull.hidden = true;
          btnRemove.hidden = true;
          startPoll();
        }
      } catch (e) { /* ignore */ }
    })();
  })();


  // ================== 启动自检（2026-11-08）==================
  // 用户要求「每次启动全部验证并报告状态」。后端启动时已跑过一次，
  // 这里读缓存（毫秒级）；右键可现场重跑（约 5-10 秒）。
  // ★ 2026-11-08 追加：自检**跟随当前选用的模型** —— 用 30B 就查 30B 下没下载，
  //   用 DeepSeek 就只查云端通道（本地 Ollama/本地模型那两项标 ⚪ 不计分）。
  (() => {
    const btn = document.querySelector("#btnSelftest");
    const sum = document.querySelector("#stSummary");
    const det = document.querySelector("#stDetail");
    if (!btn || !sum || !det) return;

    let busy = false;

    function curModel() {
      const el = document.querySelector("#modelSelect");
      return el ? el.value : "auto";
    }

    function modelLabel(m) {
      const el = document.querySelector("#modelSelect");
      if (!el || !m || m === "auto") return "跟随配置";
      for (const o of el.options) if (o.value === m) return o.text.replace(/^\S+\s*/, "");
      return m;
    }

    async function load(rerun) {
      if (busy) return;
      busy = true;
      const m = curModel();
      sum.textContent = "🔄 自检中…（" + modelLabel(m) + "）";
      try {
        const q = "?model=" + encodeURIComponent(m) + (rerun ? "&run=1" : "");
        const r = await fetch("/api/selftest" + q);
        const d = await r.json();
        // 期间用户又切了模型 → 结果作废，重来
        if (curModel() !== m) { busy = false; return load(rerun); }
        const ok = d.ok_count || 0, total = d.total || 0, sk = d.skipped || 0;
        const good = total > 0 && ok === total;
        sum.textContent = (good ? "✅" : "⚠️") + " 自检 " + ok + "/" + total +
                          " · " + modelLabel(d.model || m) + (sk ? "（⚪" + sk + " 项不参与）" : "");
        det.innerHTML = (d.items || []).map((it) =>
          "<div class=\"st-row\"><span>" + (it.skip ? "⚪" : (it.ok ? "✅" : "❌")) + "</span>" +
          "<b>" + it.label + "</b>" +
          "<span class=\"st-det\">" + String(it.detail || "").replace(/</g, "&lt;") + "</span>" +
          (!it.ok && !it.skip && it.hint ? "<div class=\"st-hint\">→ " + String(it.hint).replace(/</g, "&lt;") + "</div>" : "") +
          "</div>").join("");
      } catch (e) {
        sum.textContent = "⚠️ 自检失败";
        det.textContent = String(e);
      } finally {
        busy = false;
      }
    }

    btn.onclick = () => { det.hidden = !det.hidden; };
    btn.oncontextmenu = (e) => { e.preventDefault(); det.hidden = false; load(true); };
    btn.title = "左键展开/收起详情；右键现场重跑（约 5-10 秒）";

    // 切模型 → 立即按新模型重查（缓存按模型分开，切回旧模型是毫秒级）
    window.addEventListener("model-changed", () => {
      det.hidden = false;
      load(false);
    });

    load(false);
    setInterval(() => load(false), 60000);
  })();

  refreshHealth();
  loadToggles();
  loadMemory();
  loadKb();
  loadDoclib();
  loadAudioLib();
  loadLibrary();
  loadDevice();
  setInterval(refreshHealth, 5000);
  // ---------- 「新版本已就绪」：模型改完源码不自动重启，等用户点确认 ----------
  // 用户要求：不许悄悄重启把对话打断；弹一条提示，按下才重启，不按继续跑旧代码。
  (() => {
    let banner = null;
    let busy = false;

    function box() {
      if (banner) return banner;
      banner = document.createElement("div");
      banner.className = "restart-banner";
      banner.innerHTML =
        '<span class="rb-ico">🔄</span>' +
        '<span class="rb-text">新版本已就绪</span>' +
        '<button class="rb-go" type="button">立即重启</button>' +
        '<button class="rb-later" type="button" title="先不重启，继续用旧版本">稍后</button>';
      document.body.appendChild(banner);
      banner.querySelector(".rb-go").onclick = apply;
      banner.querySelector(".rb-later").onclick = later;
      return banner;
    }

    function show(d) {
      const b = box();
      const fs = (d && d.files) || [];
      const names = fs.slice(0, 2).map((f) => String(f).split("/").pop());
      // 用户要求（2026-11-11）：前端文件改动也要提示重启（重启后按 Ctrl+F5 才会用上新样式）
      const onlyFe = fs.length > 0 && fs.every((f) => String(f).startsWith("frontend/"));
      const tail = onlyFe ? "（前端改动 · 重启后 Ctrl+F5 生效）" : "";
      b.querySelector(".rb-text").textContent = names.length
        ? "新版本已就绪：" + names.join("、") + (fs.length > 2 ? " 等" : "") + tail
        : "新版本已就绪";
      b.classList.add("on");
    }

    function hide() { if (banner) banner.classList.remove("on"); }

    async function poll() {
      if (busy) return;
      try {
        const d = await (await fetch("/api/selfdev/pending", { cache: "no-store" })).json();
        if (d && d.pending) show(d); else hide();
      } catch (e) { /* 后端正在重启时连不上，忽略 */ }
    }

    async function apply() {
      if (busy) return;
      busy = true;
      const b = box();
      const go = b.querySelector(".rb-go");
      go.disabled = true;
      go.textContent = "重启中…";
      try {
        const r = await (await fetch("/api/selfdev/apply_restart", { method: "POST" })).json();
        if (!r || !r.ok) {
          showToast("重启失败：" + ((r && r.error) || "原因未知"), "warn");
          go.disabled = false;
          go.textContent = "立即重启";
          busy = false;
          return;
        }
      } catch (e) { /* 服务已经断开去重启了，属正常 */ }
      let n = 0;
      const tick = setInterval(async () => {
        n++;
        try {
          const r = await fetch("/api/selfdev/pending", { cache: "no-store" });
          if (r.ok) { clearInterval(tick); location.reload(); return; }
        } catch (e) { /* 还没起来 */ }
        if (n >= 90) { clearInterval(tick); location.reload(); }
      }, 1000);
    }

    async function later() {
      hide();
      try { await fetch("/api/selfdev/dismiss_pending", { method: "POST" }); } catch (e) {}
    }

    // 后端推了「新版本就绪」事件 → 立刻显示，不用等轮询
    window.__onRestartReady = (d) => show(d);
    // 回撤版本后自动重启（用户要求：确认框点「确定」→ 回滚 → 立刻重启生效）
    window.__restartAppNow = apply;
    poll();
    setInterval(poll, 4000);
  })();

  // 上下文占用环写在**平级的另一个模块**里（技能模块后面），拿不到这里的
  // history / sessionId / showToast —— 用 window 钩子把引用暴露出去，
  // 和 __showSampling / __restartAppNow 同一套做法（箭头函数读的是最新值）。
  window.__ctxState = () => ({ messages: history, session: sessionId });
  window.__ctxToast = showToast;
})();

/* =====================================================================
   技能（skill）面板 —— 2026-11-10 用户要求：
     · 顶栏「⚡ 技能」点开弹层，列出全部技能，点条目看详情；
     · 支持新建 / 编辑 / 删除 / 启用开关；
     · 三种生效方式：手动（点「用它」注入下一轮）/ 常驻 / 自动（模型自己判断）。
   数据全在后端 data/skills.json，这里只做展示与增删改。
   ===================================================================== */
(function () {
  const btn = document.getElementById("skillBtn");
  if (!btn) return;

  const MODE_LABEL = { manual: "手动", always: "常驻", auto: "自动" };
  const MODE_HINT = {
    manual: "点「用它」注入到下一轮对话，只生效一次",
    always: "始终注入系统提示，每一轮都起作用",
    auto: "只把名称和触发词给模型，它自己判断该不该用"
  };
  let items = [];
  let openId = null;     // 展开详情的技能 id
  let editing = null;    // 正在编辑的数据（新建时是空对象）

  const sid = () => localStorage.getItem("ai_session_id") || "";
  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  async function call(url, body) {
    const opt = body === undefined ? {} :
      { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) };
    const r = await fetch(url, opt);
    return await r.json();
  }

  let el = null;
  function layer() {
    if (el) return el;
    el = document.createElement("div");
    el.className = "confirm-layer sk-layer";
    el.innerHTML =
      '<div class="confirm-box sk-box">' +
        '<div class="sk-head">' +
          '<span class="sk-title">⚡ 技能</span>' +
          '<span class="sk-count"></span>' +
          '<span class="sk-head-acts">' +
            '<button type="button" class="btn sm sk-new">＋ 新建</button>' +
            '<button type="button" class="btn sm ghost sk-import">导入</button>' +
            '<button type="button" class="btn sm ghost sk-close">关闭</button>' +
          '</span>' +
        '</div>' +
        '<div class="sk-msg"></div>' +
        '<div class="sk-list"></div>' +
      '</div>';
    document.body.appendChild(el);
    el.addEventListener("click", (e) => { if (e.target === el) hide(); });
    el.querySelector(".sk-close").onclick = hide;
    el.querySelector(".sk-import").onclick = () => { openImport(); };
    el.querySelector(".sk-new").onclick = () => {
      openId = null; editing = { mode: "manual", enabled: true }; render();
    };
    el.querySelector(".sk-list").addEventListener("click", onList);
    return el;
  }

  function say(t, kind) {
    const m = layer().querySelector(".sk-msg");
    m.textContent = t || "";
    m.className = "sk-msg " + (kind || "");
    if (t) setTimeout(() => { if (m.textContent === t) m.textContent = ""; }, 3500);
  }

  function hide() {
    if (el) { el.remove(); el = null; }
    openId = null; editing = null;
  }

  function render() {
    const box = layer();
    box.querySelector(".sk-count").textContent =
      "共 " + items.length + " 个 · 已启用 " + items.filter(i => i.enabled).length + " 个";
    const list = box.querySelector(".sk-list");
    let html = "";

    if (editing) {
      const e = editing;
      html += '<div class="sk-edit">' +
        '<div class="sk-field"><label>名称</label>' +
          '<input class="sk-in-name" maxlength="60" value="' + esc(e.name) + '" placeholder="如：写周报"></div>' +
        '<div class="sk-field"><label>一句话描述</label>' +
          '<input class="sk-in-desc" maxlength="200" value="' + esc(e.desc) + '" placeholder="它解决什么问题（模型判断用不用时会看这句）"></div>' +
        '<div class="sk-field"><label>触发词</label>' +
          '<input class="sk-in-trig" value="' + esc((e.triggers || []).join("，")) + '" placeholder="用逗号分隔，如：周报，工作总结"></div>' +
        '<div class="sk-field"><label>生效方式</label>' +
          '<select class="sk-in-mode">' +
            ["manual", "always", "auto"].map(k =>
              '<option value="' + k + '"' + ((e.mode || "manual") === k ? " selected" : "") + '>' +
              MODE_LABEL[k] + " — " + MODE_HINT[k] + '</option>').join("") +
          '</select></div>' +
        '<div class="sk-field"><label>内容（做法 / 步骤 / 规范）</label>' +
          '<textarea class="sk-in-content" rows="10" placeholder="写清要模型怎么做：分几步、每步产出什么、有哪些禁忌">' +
          esc(e.content) + '</textarea></div>' +
        '<div class="sk-acts">' +
          '<button type="button" class="btn sm" data-act="save">保存</button>' +
          '<button type="button" class="btn sm ghost" data-act="cancel">取消</button>' +
        '</div></div>';
      list.innerHTML = html;
      return;
    }

    if (!items.length) {
      list.innerHTML = '<div class="sk-empty">还没有技能。点右上角「＋ 新建」加一个，' +
        '或者直接跟我说「加一个XX技能」。</div>';
      return;
    }

    html = items.map(function (it) {
      const open = openId === it.id;
      let row = '<div class="sk-item' + (it.enabled ? "" : " off") + '" data-id="' + esc(it.id) + '">' +
        '<div class="sk-row" data-act="open">' +
          '<span class="sk-name">' + esc(it.name) + '</span>' +
          '<span class="sk-mode m-' + esc(it.mode) + '">' + MODE_LABEL[it.mode] + '</span>' +
          '<span class="sk-desc">' + esc(it.desc || "（没有描述）") + '</span>' +
          '<span class="sk-sw" data-act="toggle" title="启用 / 关闭">' +
            '<i class="' + (it.enabled ? "on" : "") + '"></i></span>' +
          '<span class="sk-caret">' + (open ? "▾" : "▸") + '</span>' +
        '</div>';
      if (open) {
        row += '<div class="sk-detail">' +
          (it.triggers && it.triggers.length
            ? '<div class="sk-trig">触发词：' + it.triggers.map(esc).join("、") + '</div>' : "") +
          '<pre class="sk-content">' + esc(it.content || "（空）") + '</pre>' +
          '<div class="sk-acts">' +
            (it.mode === "manual"
              ? '<button type="button" class="btn sm" data-act="use">用它</button>' : "") +
            '<button type="button" class="btn sm ghost" data-act="share">分享</button>' +
            '<button type="button" class="btn sm ghost" data-act="edit">编辑</button>' +
            '<button type="button" class="btn sm ghost danger" data-act="del">删除</button>' +
          '</div></div>';
      }
      return row + '</div>';
    }).join("");
    list.innerHTML = html;
  }

  async function onList(e) {
    const t = e.target.closest("[data-act]");
    if (!t) return;
    const act = t.getAttribute("data-act");
    const item = t.closest(".sk-item");
    const id = item ? item.getAttribute("data-id") : "";

    if (act === "save") { await save(); return; }
    if (act === "cancel") { editing = null; render(); return; }
    if (!id) return;

    if (act === "open") {
      openId = openId === id ? null : id;
      render();
      return;
    }
    if (act === "toggle") {
      e.stopPropagation();
      const cur = items.find(i => i.id === id);
      if (!cur) return;
      const r = await call("/api/skills/toggle", { id: id, enabled: !cur.enabled });
      if (!r.ok) { say(r.error || "操作失败", "bad"); return; }
      await reload();
      say((r.item.enabled ? "已启用：" : "已关闭：") + r.item.name, "ok");
      return;
    }
    if (act === "use") {
      const r = await call("/api/skills/use", { id: id, session: sid() });
      if (!r.ok) { say(r.error || "用不了这个技能", "bad"); return; }
      say("已选「" + r.name + "」—— 下一条消息会按它来做（只生效一次）", "ok");
      return;
    }
    if (act === "share") {
      const cur = items.find(i => i.id === id);
      if (cur) await openShare(cur);
      return;
    }
    if (act === "edit") {
      const cur = items.find(i => i.id === id);
      if (cur) { editing = JSON.parse(JSON.stringify(cur)); openId = null; render(); }
      return;
    }
    if (act === "del") {
      const cur = items.find(i => i.id === id);
      if (!cur) return;
      if (!confirm("删除技能「" + cur.name + "」？删了就找不回来了。")) return;
      const r = await call("/api/skills/delete", { id: id });
      if (!r.ok) { say("删除失败（可能已经不存在）", "bad"); return; }
      await reload();
      say("已删除：" + cur.name, "ok");
      return;
    }
  }

  async function save() {
    const box = layer();
    const v = (sel) => { const n = box.querySelector(sel); return n ? n.value : ""; };
    const data = {
      name: v(".sk-in-name").trim(),
      desc: v(".sk-in-desc").trim(),
      content: v(".sk-in-content"),
      triggers: v(".sk-in-trig"),
      mode: v(".sk-in-mode") || "manual"
    };
    if (!data.name) { say("名称不能为空", "bad"); return; }
    const isNew = !editing || !editing.id;
    const r = isNew
      ? await call("/api/skills/create", data)
      : await call("/api/skills/update", Object.assign({ id: editing.id }, data));
    if (!r.ok) { say(r.error || "保存失败", "bad"); return; }
    editing = null;
    await reload();
    openId = r.item.id;
    render();
    say("已保存：" + r.item.name, "ok");
  }

  // ---------------- 分享 / 导入（2026-11-10 用户要求） ----------------
  //  分享：一段可直接粘给任何 AI 的话 + 机器可读分享码，可复制 / 下载 / 二维码 / 系统分享
  //  导入：把别人发来的整段内容粘回来，一键建成本地技能（重名自动加后缀）
  function modal(html) {
    const d = document.createElement("div");
    d.className = "confirm-layer sk-layer";
    d.innerHTML = '<div class="confirm-box sk-box sk-share">' + html + '</div>';
    document.body.appendChild(d);
    d.addEventListener("click", (e) => { if (e.target === d) d.remove(); });
    return d;
  }

  function fallbackCopy(t) {
    const ta = document.createElement("textarea");
    ta.value = t;
    ta.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e) {}
    ta.remove();
  }

  function copyText(t) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(t).catch(() => fallbackCopy(t));
    }
    fallbackCopy(t);
    return Promise.resolve();
  }

  async function openShare(it) {
    const r = await call("/api/skills/share", { id: it.id });
    if (!r.ok) { say(r.error || "导出失败", "bad"); return; }
    const d = modal(
      '<div class="sk-head"><span class="sk-title">分享：' + esc(it.name) + '</span>' +
        '<span class="sk-head-acts"><button type="button" class="btn sm ghost sk-x">关闭</button></span></div>' +
      '<div class="sk-share-tip">下面这段可以直接发给别人，内容可改，改完再复制。' +
        '生效方式已固定建议为「自动」，对方可自行调整。</div>' +
      '<textarea class="sk-share-ta" rows="12" spellcheck="false"></textarea>' +
      '<div class="sk-acts sk-share-acts">' +
        '<button type="button" class="btn sm" data-s="copy">复制文本</button>' +
        '<button type="button" class="btn sm ghost" data-s="md">保存 .md</button>' +
        '<button type="button" class="btn sm ghost" data-s="qr">二维码</button>' +
        '<button type="button" class="btn sm ghost" data-s="sys">系统分享</button>' +
      '</div>' +
      '<div class="sk-share-msg"></div>' +
      '<div class="sk-qr"></div>'
    );
    const ta = d.querySelector(".sk-share-ta");
    ta.value = r.text;
    d.querySelector(".sk-x").onclick = () => d.remove();
    d.querySelector(".sk-share-acts").addEventListener("click", async (e) => {
      const b = e.target.closest("[data-s]");
      if (!b) return;
      const act = b.getAttribute("data-s");
      const txt = ta.value;
      if (act === "copy") {
        await copyText(txt);
        const old = b.textContent;
        b.textContent = "已复制 ✓";
        setTimeout(() => { b.textContent = old; }, 1600);
      } else if (act === "md") {
        // 2026-11-10 用户反馈：点「下载 .md」没反应、也不知道存哪了。
        // 根因：原来直接 a.click() 触发浏览器下载，内嵌 WebView 会静默拦掉；
        //       而且 a 没挂到 DOM 上，某些浏览器同样不触发。改成后端真实写盘，
        //       把绝对路径显示出来（点一下就能打开所在文件夹）。
        const mb = d.querySelector(".sk-share-msg");
        const oldTxt = b.textContent;
        b.disabled = true;
        b.textContent = "保存中…";
        let sr = { ok: false, error: "请求失败" };
        try { sr = await call("/api/skills/save_md", { text: txt, name: it.name }); }
        catch (err) { sr = { ok: false, error: String((err && err.message) || err) }; }
        b.disabled = false;
        b.textContent = oldTxt;
        if (sr.ok) {
          mb.className = "sk-share-msg ok";
          mb.innerHTML = "已保存到：" +
            '<a class="path-link" href="javascript:void(0)" data-path="' + esc(sr.path) +
            '" title="点击打开所在文件夹">' + esc(sr.path) + "</a>" +
            "（" + sr.bytes + " 字节，点路径可直接打开）";
        } else {
          // 后端写不了盘时退回浏览器下载，这次挂到 DOM 上并明确告知去哪儿找
          const blob = new Blob([txt], { type: "text/markdown;charset=utf-8" });
          const a = document.createElement("a");
          a.href = URL.createObjectURL(blob);
          a.download = "技能-" + it.name + ".md";
          a.style.display = "none";
          document.body.appendChild(a);
          a.click();
          setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 5000);
          mb.className = "sk-share-msg bad";
          mb.textContent = "后端保存失败（" + (sr.error || "未知原因") +
            "），已改为浏览器下载：请到浏览器的「下载」目录里找「技能-" + it.name + ".md」";
        }
      } else if (act === "qr") {
        const box = d.querySelector(".sk-qr");
        const m = txt.match(/SKILL1:[A-Za-z0-9_\-=]+/);
        box.innerHTML = '<div class="sk-qr-tip">生成中…</div>';
        const q = await call("/api/skills/qr", m ? { text: m[0] } : { id: it.id });
        if (!q.ok) {
          box.innerHTML = '<div class="sk-qr-tip bad">' + esc(q.error || "生成失败") + '</div>';
          return;
        }
        box.innerHTML = '<img class="sk-qr-img" onclick="window.open(this.src)" alt="分享二维码" title="点击看原图" src="' + q.data_url + '">' +
          '<div class="sk-qr-tip">手机扫码 → 复制 → 粘到「导入」里即可原样重建</div>';
      } else if (act === "sys") {
        if (navigator.share) {
          try { await navigator.share({ title: "技能分享：" + it.name, text: txt }); }
          catch (err) {}
        } else {
          await copyText(txt);
          const old = b.textContent;
          b.textContent = "已复制（本机无系统分享）";
          setTimeout(() => { b.textContent = old; }, 2200);
        }
      }
    });
  }

  function openImport() {
    const d = modal(
      '<div class="sk-head"><span class="sk-title">导入技能</span>' +
        '<span class="sk-head-acts"><button type="button" class="btn sm ghost sk-x">关闭</button></span></div>' +
      '<div class="sk-share-tip">把别人发来的整段分享内容粘进来（带 SKILL1: 的分享码也认）。' +
        '重名会自动加「（导入1）」，不会覆盖你已有的技能。</div>' +
      '<textarea class="sk-share-ta sk-import-ta" rows="10" placeholder="粘贴分享内容…"></textarea>' +
      '<div class="sk-acts"><button type="button" class="btn sm sk-do">导入</button></div>' +
      '<div class="sk-msg sk-import-msg"></div>'
    );
    const ta = d.querySelector(".sk-import-ta");
    const msg = d.querySelector(".sk-import-msg");
    d.querySelector(".sk-x").onclick = () => d.remove();
    d.querySelector(".sk-do").onclick = async () => {
      if (!ta.value.trim()) {
        msg.textContent = "先粘贴内容"; msg.className = "sk-msg bad"; return;
      }
      const r = await call("/api/skills/import", { text: ta.value });
      if (!r.ok) {
        msg.textContent = r.error || "导入失败"; msg.className = "sk-msg bad"; return;
      }
      await reload();
      d.remove();
      openId = r.item.id;
      render();
      say("已导入：" + r.item.name, "ok");
    };
  }

  async function reload() {
    try {
      const d = await call("/api/skills");
      items = (d && d.items) || [];
    } catch (err) { items = []; }
    if (openId && !items.some(i => i.id === openId)) openId = null;
  }

  btn.addEventListener("click", async function () {
    layer();
    await reload();
    render();
  });

  // ================== 上下文占用环（2026-11-11）==================
  // 底栏那个小圆环：绿 → 橙 → 红，越接近上限越红。
  // · 容量（limit）由用户自己定（顶栏「⚙ 上下文」/ 性能档位），
  //   所以每次都重新问后端要 —— 换模型、改长度后比例会跟着变；
  // · 到 90% 就**自动压缩**：较早的对话压成要点摘要、最近的原文保留，
  //   这样既不顶爆窗口，又不会失忆（摘要里带着「必须记住」的关键信息）。
  (() => {
    const btn = document.querySelector("#ctxRing");
    const fg = document.querySelector("#ctxRingFg");
    const txt = document.querySelector("#ctxRingTxt");
    if (!btn || !fg || !txt) return;
    const C = 2 * Math.PI * 15.5;        // 圆周长（r=15.5），与 HTML 的 dasharray 一致
    let lastLen = -1, busy = false, compactAt = 0;

    // 0 → 绿；0.7 → 橙；1 → 红，中间线性过渡
    const colorOf = (r) => {
      const g = [34, 197, 94], o = [245, 158, 11], rd = [239, 68, 68];
      const mix = (a, b, t) => a.map((v, i) => Math.round(v + (b[i] - v) * t));
      const c = r <= 0.7 ? mix(g, o, Math.max(0, r) / 0.7)
                         : mix(o, rd, Math.min(1, (r - 0.7) / 0.3));
      return "rgb(" + c.join(",") + ")";
    };
    const kk = (n) => (Number(n) >= 10000 ? (Number(n) / 10000).toFixed(1) + "万" : String(Number(n) || 0));

    const paint = (d) => {
      const r = Math.max(0, Math.min(1, Number(d.ratio) || 0));
      fg.style.strokeDashoffset = String(C * (1 - r));
      fg.style.stroke = colorOf(r);
      txt.textContent = r >= 0.995 ? "满" : Math.round(r * 100) + "%";
      txt.style.color = r >= 0.85 ? colorOf(r) : "#9a9aa2";
      btn.title = "上下文占用 " + (r * 100).toFixed(1) + "%\n"
        + "已用 " + kk(d.used) + " / " + kk(d.limit) + " token\n"
        + "· 系统提示 " + kk(d.prompt) + "\n"
        + "· 工具定义 " + kk(d.tools) + "\n"
        + "· 对话历史 " + kk(d.history)
        + (r >= 0.9 ? "\n\n已接近上限，会自动压缩较早的对话（保留要点）" : "");
    };
    const post = async (url, body) => {
      const res = await fetch(url, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      });
      return res.json();
    };

    // 会话消息与 sessionId 在**主模块**作用域里，本模块拿不到 —— 走主模块暴露的钩子。
    const st = () => (window.__ctxState ? window.__ctxState() : { messages: [], session: "" });

    async function refresh(force) {
      const s = st();
      const msgs = s.messages || [];
      if (busy) return;
      if (!force && msgs.length === lastLen) return;   // 没变化就不打扰后端
      busy = true;
      try {
        const d = await post("/api/context/usage", { messages: msgs, session: s.session || "" });
        if (!d || !d.ok) return;
        lastLen = msgs.length;
        paint(d);
        // 到 90% 就压：留余量，别等真顶爆（那时这一轮已经塞不下了）
        if (d.ratio >= 0.9 && msgs.length > 4 && (Date.now() - compactAt) > 20000) {
          compactAt = Date.now();
          const r = await post("/api/context/compact",
            { messages: msgs, session: s.session || "", target: 0.6 });
          if (r && r.ok && r.changed && (r.messages || []).length) {
            msgs.length = 0;                      // msgs 就是主模块那个 history 数组本身
            r.messages.forEach((m) => msgs.push(m));
            lastLen = msgs.length;
            if (window.__ctxToast) {
              window.__ctxToast("上下文已自动压缩：保留最近 " + (r.kept || 0)
                + " 条原文 + 较早对话的要点摘要（不会失忆）", "ok");
            }
            const d2 = await post("/api/context/usage", { messages: msgs, session: s.session || "" });
            if (d2 && d2.ok) paint(d2);
          }
        }
      } catch (e) { /* 读不到就保持原样，不打扰用户 */ }
      finally { busy = false; }
    }

    btn.addEventListener("click", () => refresh(true));
    window.addEventListener("model-changed", () => setTimeout(() => refresh(true), 300));
    window.addEventListener("perf-changed", () => setTimeout(() => refresh(true), 300));
    window.addEventListener("focus", () => refresh(false));
    setInterval(() => refresh(false), 1500);      // 只比长度，变了才真的请求
    setTimeout(() => refresh(true), 800);
  })();

  /* ============================================================
     多人协作群（Hive）· 2026-11-13
     ------------------------------------------------------------
     和单机模式共用一个页面，靠「模式标签」切换：
       单机 → 侧栏 6 个原有面板 + 主区聊天
       群聊 → 侧栏只有群列表 + 主区群聊界面
     ⚠️ 切换只改 hidden，不动任何布局骨架（避免又出现"页面被撑变形"）。
     ============================================================ */
  (function () {
    const tabs = document.getElementById("modeTabs");
    if (!tabs) return;

    const SINGLE_PANELS = ["kbPanel", "doclibPanel", "audioPanel",
                           "libraryPanel", "sessionsPanel", "memoryPanel"];
    const el = (id) => document.getElementById(id);

    let mode = "single";
    let curGid = "";
    let groups = [];
    let members = [];
    let myId = "";
    let myDev = "";
    let myName = "";
    let myProto = 0;      // 本机协议版本，用来标出「对方版本比我老」
    let netCls = "idle";  // 当前连接状态类别（online/connecting/offline/error/idle）
    let lastSeq = 0;
    let ws = null;
    let wsRetry = 0;
    let hiveTimer = null;

    function hEsc(s) {
      return String(s == null ? "" : s)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;")
        .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }

    // 统一请求：body 有就是 POST，没有就是 GET
    async function api(path, body) {
      const opt = { method: body ? "POST" : "GET", headers: {} };
      if (body) {
        opt.headers["Content-Type"] = "application/json";
        opt.body = JSON.stringify(body);
      }
      const r = await fetch(path, opt);
      if (!r.ok) throw new Error("HTTP " + r.status);
      return await r.json();
    }

    // ---------------------------------------------------------- 浮层
    const modal = el("hiveModal");
    let modalOnOk = null;

    function openModal(opt) {
      el("hiveModalTitle").textContent = opt.title || "";
      el("hiveModalHint").textContent = opt.hint || "";
      const ta = el("hiveModalInput");
      if (opt.input != null) {
        ta.value = opt.input;
        ta.placeholder = opt.placeholder || "";
        ta.hidden = false;
        // 只读：能滚动看全文、能选中复制，但改不了（展示群记忆用）
        ta.readOnly = !!opt.readonly;
        ta.style.height = opt.readonly ? "260px" : "";
        ta.style.overflow = opt.readonly ? "auto" : "";
      } else {
        ta.hidden = true;
        ta.readOnly = false;
        ta.style.height = "";
        ta.style.overflow = "";
      }
      // 第二个输入框（可选）：跨网络入群时填对方地址
      const ex = el("hiveModalExtra");
      if (opt.extra != null) {
        ex.value = opt.extra;
        ex.placeholder = opt.extraPlaceholder || "";
        ex.hidden = false;
      } else { ex.hidden = true; }
      const code = el("hiveModalCode");
      if (opt.code) { code.value = opt.code; code.hidden = false; }
      else { code.hidden = true; }
      // 复制按钮的两种来源：单行 code 框（密码/连接码），或上面的多行文本框
      const cp = el("hiveModalCopy");
      cp.hidden = !(opt.code || opt.copyInput);
      cp.dataset.from = opt.copyInput ? "input" : "code";
      // 精确指定「复制出去的那串字」：不填就复制整个框（旧行为）。
      // 「我的地址」用它，否则会把整段说明文字一起复制走。
      cp.dataset.value = opt.copyValue || "";
      cp.textContent = opt.copyLabel || (opt.copyInput ? "📋 复制" : "📋 复制密码");
      const ok = el("hiveModalOk");
      ok.hidden = !!opt.noOk;
      ok.textContent = opt.okText || "确定";
      modalOnOk = opt.onOk || null;
      modal.hidden = false;
      if (opt.input != null) setTimeout(() => ta.focus(), 50);
    }

    function closeModal() {
      modal.hidden = true;
      modalOnOk = null;
    }

    el("hiveModalClose").addEventListener("click", closeModal);
    modal.addEventListener("click", (e) => { if (e.target === modal) closeModal(); });
    el("hiveModalCopy").addEventListener("click", async () => {
      const b0 = el("hiveModalCopy");
      const src = b0.dataset.from === "input" ? el("hiveModalInput") : el("hiveModalCode");
      // copyValue 优先：只复制那串字，不复制整框
      const v = b0.dataset.value || src.value;
      try { await navigator.clipboard.writeText(v); }
      catch (e) {
        // ⚠️ 用 http://192.168.x.x 这类非安全地址访问时 navigator.clipboard 是
        // undefined，一定走这里。**绝不能** select() 可见的那个大文本框 ——
        // 那会把「【同一 WiFi】…」整段说明一起复制走（用户实测报过）。
        // 用隐藏 textarea 只装要复制的那串字。
        const ta = document.createElement("textarea");
        ta.value = v;
        ta.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0";
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); } catch (_) { /* 忽略 */ }
        ta.remove();
      }
      const b = el("hiveModalCopy");
      const old = b.textContent;
      b.textContent = "已复制 ✓";
      setTimeout(() => { b.textContent = old; }, 1400);
    });
    el("hiveModalOk").addEventListener("click", async () => {
      const fn = modalOnOk;
      const v = el("hiveModalInput").hidden ? "" : el("hiveModalInput").value;
      const ex = el("hiveModalExtra").hidden ? "" : el("hiveModalExtra").value;
      if (fn) {
        const keep = await fn(v, ex);
        if (keep === false) return;
      }
      closeModal();
    });

    // ---------------------------------------------------------- 模式切换
    function setMode(m) {
      mode = m;
      const isHive = m === "hive";
      // 全局拖拽处理在 document 捕获阶段，它要知道现在是不是群聊模式，
      // 才能把文件交给 addFiles 而不是单人模式的附件区。
      window.__hiveMode = isHive;
      Array.prototype.forEach.call(tabs.querySelectorAll(".mode-tab"), (b) => {
        b.classList.toggle("on", b.getAttribute("data-mode") === m);
      });
      // ★ 2026-11-13 用户要求：「群聊模式左侧的工具栏，生成文库、图片库等等都没了」。
      //   原来这里在群聊模式下把 6 个面板全藏了（p.hidden = isHive），
      //   侧栏只剩「协作群」一块，看起来就像工具栏消失了。
      //   现在两种模式左侧**完全一致**，群聊只是多一块「协作群」在最上面。
      SINGLE_PANELS.forEach((id) => {
        const p = el(id);
        if (p) p.hidden = false;
      });
      el("hivePanel").hidden = !isHive;
      el("hiveMain").hidden = !isHive;
      const msgs = document.querySelector(".messages");
      const comp = document.querySelector(".composer");
      if (msgs) msgs.hidden = isHive;
      if (comp) comp.hidden = isHive;
      try { localStorage.setItem("ui_mode", m); } catch (e) { /* 隐私模式 */ }

      if (isHive) {
        loadGroups();
        loadStatus();
        connectWs();
        if (!hiveTimer) hiveTimer = setInterval(loadStatus, 4000);
      } else if (hiveTimer) {
        clearInterval(hiveTimer);
        hiveTimer = null;
      }
    }

    Array.prototype.forEach.call(tabs.querySelectorAll(".mode-tab"), (b) => {
      b.addEventListener("click", () => setMode(b.getAttribute("data-mode")));
    });

    // ---------------------------------------------------------- 状态
    async function loadStatus() {
      try {
        const d = await api("/api/hive/status");
        paintStatus(d);
      } catch (e) { /* 后端没起来就保持原样 */ }
    }

    // 连接状态 → 灯的颜色 + 一句人话。
    // 四态：online（绿）/ connecting（黄闪）/ offline（灰）/ error（红）。
    // 「离线」和「异常」必须分开：前者是正常断线会自愈，后者是连不上、要人管。
    function netInfo(c) {
      // 2026-11-13：没有服务器了，这里说的是「直连了几个人」。
      // peers = 当前真实建连的对端数（不是成员总数 —— 成员可能没上线）。
      const st = c.state || (c.connected ? "online" : "offline");
      const lat = c.latency_ms ? " · " + Math.round(c.latency_ms) + "ms" : "";
      const n = Number(c.peers || 0);
      if (st === "error") {
        return { cls: "error", text: "连不上" + (c.error ? "：" + c.error : "") };
      }
      if (st === "online" && n > 0) {
        let s = "已直连 · " + n + " 人在线" + lat;
        if (c.compat === "older") s += " · 对方版本较老";
        else if (c.compat === "newer") s += " · 对方版本较新";
        return { cls: "online", text: s };
      }
      if (c.queued) {
        return { cls: "offline",
          text: "还没有人在线 · " + c.queued + " 条待发" };
      }
      return { cls: "idle",
        text: "还没有人连上（同一 WiFi 会自动发现，跨网络用连接码）" };
    }

    // 身份冲突（2026-11-13）：另一台机器和本机共用同一个 user_id。
    // 典型原因：把整个程序目录拷给了别人，data/hive/profile.json 一起带过去了。
    function paintIdConflict(cf) {
      const box = el("hiveIdWarn");
      if (!box) return;
      if (!cf) { box.hidden = true; return; }
      box.hidden = false;
      const t = el("hiveIdWarnText");
      if (t) {
        t.textContent = "身份冲突：另一台机器（" + (cf.addr || "?") + "）的 user_id 和本机一样，"
          + "多半是把整个程序目录拷过去了。这会让双方消息互相显示成「自己发的」。";
      }
    }

    function paintStatus(d) {
      if (!d || !d.ok) return;
      myId = d.hive.user_id;
      myName = d.hive.name;
      const c = d.client || {};
      myProto = c.proto || myProto;
      myDev = c.dev || "";
      // 身份冲突：对方和本机 user_id 一样但不是这台机器（拷贝了整个目录）
      paintIdConflict(c.uid_conflict);

      // 头部状态灯
      const ni = netInfo(c);
      if (ni.cls !== netCls) {
        netCls = ni.cls;
        renderGroups();          // 侧栏群列表的点也跟着变色
      }
      const net = el("hiveNet");
      if (net) {
        net.className = "hive-net " + ni.cls;
        const nt = el("hiveNetText");
        if (nt) nt.textContent = ni.text;
        net.title = [
          "方式：P2P 直连（不经过任何服务器）",
          "监听端口 " + (c.port || "?") + " · 局域网 " + (c.lan_ip || "?"),
          "协议 v" + (c.proto || "?") + " · 在线 " + (c.peers || 0) + " 人",
          c.queued ? c.queued + " 条消息等对方上线" : "",
          c.error ? "最近错误：" + c.error : "",
        ].filter(Boolean).join("\n");
      }

      // 侧栏菜单里那行文字也同步（详细版）
      const line = el("hiveStatusLine");
      if (line) {
        const bits = [ni.text];
        if (c.url) bits.push(c.url);
        if (c.queued) bits.push(c.queued + " 条待发");
        if (c.error && ni.cls !== "online") bits.push("上次错误：" + c.error);
        line.textContent = bits.join(" · ");
      }

      // 成员在线状态：status 带回来的 peers 只对「当前连着的那个群」有效
      if (Array.isArray(d.peers) && d.peers.length && curGid && c.gid === curGid) {
        members = d.peers;
        const side = el("hiveSide");
        if (side && !side.hidden) renderMembers();
      }

      if (curGid) {
        const meta = el("hiveGroupMeta");
        const g = (d.hive.group_list || []).find((x) => x.gid === curGid);
        if (meta && g) {
          meta.textContent = (g.kfp ? "指纹 " + g.kfp + " · " : "")
            + (g.owner === myId ? "我是群主" : "群主 " + (g.owner_name || "?"))
            + " · " + (g.members || 0) + " 人";
          // 身份/角色可能变（换身份、被转成管理员），按钮可见性跟着刷
          const db = el("hiveDelBtn");
          if (db) db.hidden = !(g.owner === myId);
        }
      }
    }

    // ---------------------------------------------------------- 群列表
    async function loadGroups() {
      try {
        const d = await api("/api/hive/groups");
        groups = d.groups || [];
        renderGroups();
        if (!curGid && groups.length) openGroup(groups[0].gid);
      } catch (e) { /* 忽略 */ }
    }

    function renderGroups() {
      const box = el("hiveGroupList");
      if (!box) return;
      if (!groups.length) {
        box.innerHTML = '<p class="hint">还没有群。点「＋ 建群」开一个，'
          + '或者用群主给的 12 位密码「密码入群」。</p>';
        return;
      }
      box.innerHTML = groups.map((g) => {
        const isCur = g.gid === curGid;
        const on = isCur ? " on" : "";
        const owner = g.owner === myId ? "群主" : (g.owner_name || "");
        // 只有当前群在 P2P 网络里，其它群不知道状态 → 保持中性灰
        let dot = "";
        if (isCur) dot = netCls === "idle" ? " offline" : " " + netCls;
        const stTxt = isCur
          ? (netCls === "online" ? "在线" : netCls === "connecting" ? "连接中"
            : netCls === "error" ? "连不上" : "离线")
          : "";
        return '<div class="hive-item' + on + '" data-gid="' + hEsc(g.gid) + '"'
          + (isCur ? ' title="' + hEsc(g.name + " · " + stTxt) + '"' : "")
          + ">"
          + '<span class="hi-dot' + dot + '"></span>'
          + '<span class="hi-name">' + hEsc(g.name) + "</span>"
          + '<span class="hi-meta">' + hEsc(owner) + " · " + (g.members || 0) + "人</span>"
          + "</div>";
      }).join("");
      Array.prototype.forEach.call(box.querySelectorAll(".hive-item"), (it) => {
        it.addEventListener("click", () => openGroup(it.getAttribute("data-gid")));
      });
    }

    // ---------------------------------------------------------- 打开群
    async function openGroup(gid) {
      curGid = gid;
      lastSeq = 0;
      renderGroups();
      const g = groups.find((x) => x.gid === gid);
      el("hiveGroupName").textContent = g ? g.name : gid;
      // 删除群只有群主能点：不是群主就干脆不显示（比点了报错友好）
      const delBtn = el("hiveDelBtn");
      if (delBtn) delBtn.hidden = !(g && g.owner === myId);
      el("hiveMsgs").innerHTML = '<p class="hint">正在读取消息…</p>';
      try {
        const d = await api("/api/hive/" + encodeURIComponent(gid)
          + "/messages?since=0&limit=200");
        members = d.members || [];
        const list = d.messages || [];
        lastSeq = list.reduce((a, m) => Math.max(a, m.seq || 0), 0);
        renderMsgs(list);
        renderMembers();
      } catch (e) {
        el("hiveMsgs").innerHTML = '<p class="hint">读消息失败：' + hEsc(e.message) + "</p>";
      }
      // 启动 P2P 网络：不传地址 = 局域网模式（UDP 广播自动发现同群的人）。
      // 完全不需要服务器，也不会有「没配地址就收不到」这种事。
      try {
        const cn = await api("/api/hive/connect", { gid: gid });
        // 老群（二维码时代）迁移到密码体系时会顺手生成密码，这里提醒一次，
        // 否则群主根本不知道密码是多少，也没法告诉别人。
        const mg = (cn && cn.migrate) || {};
        if (mg.migrated && mg.password) {
          setTimeout(() => {
            alert("这个群已启用「12 位密码进群」✅\n\n"
              + "入群密码：" + mg.password + "\n\n"
              + "把它告诉对方，他点「🔑 密码入群」输进去就能进。\n"
              + "（随时可以点右上角「🔑 密码」再看一次或重置）");
          }, 400);
        }
      } catch (e) { /* 忽略 */ }
      loadStatus();
      connectWs();
    }

    // ---------------------------------------------------------- 消息
    //
    // ★ 正文**复用单人模式的 renderFinal**（代码高亮 + 每块复制按钮 + 可点链接
    //   + 图片 + 表格 + Markdown 排版），不再自己写一套简易版 —— 用户要求
    //   两个模式「完全统一甚至更好」。群聊额外多的只有：谁说的、附件、引用、
    //   以及消息操作条（复制 / 引用 / 朗读）。
    //
    // ⚠️ 系统消息和「看不懂的新类型」**必须走纯文本**：它们是保真优先，
    //    走 Markdown 渲染反而可能把对方原样写的内容改形。

    // 当前这批消息的索引：引用块要按 mid 找回原文
    let msgIndex = {};
    let quoteMsg = null;

    function fmtSize(n) {
      n = Number(n) || 0;
      if (n < 1024) return n + " B";
      if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
      return (n / 1048576).toFixed(1) + " MB";
    }
    function fileIco(f) {
      const n = String((f && f.name) || "").toLowerCase();
      if (/\.pdf$/.test(n)) return "📕";
      if (/\.(docx?|rtf)$/.test(n)) return "📘";
      if (/\.(xlsx?|csv)$/.test(n)) return "📗";
      if (/\.(pptx?)$/.test(n)) return "📙";
      if (/\.(zip|rar|7z|tar|gz)$/.test(n)) return "🗜";
      if (/\.(py|js|ts|java|c|cpp|go|rs|json|html|css|md|txt|log|yml|yaml)$/.test(n)) return "📄";
      if ((f && f.kind) === "audio") return "🎵";
      return "📎";
    }

    function fileCards(m) {
      const fs = (m && m.files) || [];
      if (!fs.length) return "";
      const base = "/api/hive/" + encodeURIComponent(curGid) + "/files/";
      const out = [];
      fs.forEach((f) => {
        if (!f || !f.fid) return;
        const url = base + encodeURIComponent(f.fid);
        const nm = hEsc(f.name || "文件");
        // 📡 点对点直传：大文件走这条，**不经服务器**
        const p2p = '<button type="button" class="hf-p2p" data-p2p="' + hEsc(f.fid)
          + '" data-name="' + nm + '" title="点对点直传（G 级大文件，不经服务器）">📡</button>';
        if (f.kind === "image") {
          out.push('<span class="hf-wrap"><a href="' + url + '" target="_blank" rel="noopener">'
            + '<img class="hive-file-img" src="' + url + '" alt="' + nm + '" loading="lazy"></a>'
            + p2p + "</span>");
        } else if (f.kind === "video") {
          out.push('<span class="hf-wrap"><video class="hive-file-video" src="' + url
            + '" controls preload="metadata"></video>' + p2p + "</span>");
        } else if (f.kind === "audio") {
          out.push('<span class="hf-wrap"><audio src="' + url
            + '" controls preload="metadata" style="max-width:320px"></audio>' + p2p + "</span>");
        } else {
          out.push('<a class="hive-file-doc" href="' + url + '" download>'
            + '<span class="hf-ico">' + fileIco(f) + "</span>"
            + '<span class="hf-meta"><span class="hf-name">' + nm + "</span>"
            + '<span class="hf-size">' + fmtSize(f.size) + "</span></span>"
            + '<span class="hf-dl">⬇</span></a>' + p2p);
        }
      });
      return out.length ? '<div class="hive-files">' + out.join("") + "</div>" : "";
    }

    function refBlock(m) {
      if (!m || !m.ref) return "";
      const r = msgIndex[m.ref] || {};
      const who = hEsc(r.name || "成员");
      const t = String(r.text || "").replace(/\s+/g, " ").slice(0, 90) || "（原消息）";
      return '<div class="hive-ref"><b>' + who + "</b>：" + hEsc(t) + "</div>";
    }

    function mkAct(label, fn) {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = label;
      b.onclick = (e) => { e.stopPropagation(); fn(); };
      return b;
    }

    async function copyText(t, box) {
      try { await navigator.clipboard.writeText(t); }
      catch (e) {
        const ta = document.createElement("textarea");
        ta.value = t;
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); } catch (_) { /* 忽略 */ }
        ta.remove();
      }
      // 反馈只改第一个按钮的文字，**不重建 innerHTML** ——
      // 重建会把按钮的 onclick 一起冲掉，之后就点不动了。
      const b = box && box.querySelector("button");
      if (b) {
        const o = b.textContent;
        b.textContent = "已复制 ✓";
        setTimeout(() => { try { b.textContent = o; } catch (_) { /* 忽略 */ } }, 1200);
      }
    }

    function setQuote(m) {
      quoteMsg = m;
      const bar = el("hiveQuoteBar");
      const tx = el("hiveQuoteTxt");
      if (!bar || !tx) return;
      const t = String(m.text || "").replace(/\s+/g, " ").slice(0, 60);
      tx.textContent = (m.name || "成员") + "：" + (t || "（附件消息）");
      bar.hidden = false;
      try { el("hiveText").focus(); } catch (e) { /* 忽略 */ }
    }

    function clearQuote() {
      quoteMsg = null;
      const bar = el("hiveQuoteBar");
      if (bar) bar.hidden = true;
    }

    function msgNode(m) {
      const wrap = document.createElement("div");
      const type = m.type || "text";
      // ★ 2026-11-13 用户要求：「单人和群聊的版式必须完全一样」。
      //   所以这里直接复用单人模式的 class（.msg.bot / .msg.user / .bubble），
      //   不再另起一套 .hive-msg / .hmsg-bubble —— 两套样式迟早走样。
      //   只有系统消息保留 .hive-msg.sys：单人模式没有系统消息，它必须居中。
      // ★ 2026-11-13 用户反馈：「ai 的回答为什么会在右边？右边只能是用户的回答」。
      //   根因：AI 消息的 m.from 记的是**唤醒者**（谁 @ 的 hd），所以
      //   `m.from === myId` 成立 → 被判成「自己发的」→ 靠右绿气泡。
      //   正确规则：AI 回答**永远靠左**；只有「我自己说的话」靠右。
      //   剩下的人（其他成员）靠左，但用白底黑字气泡与 AI 区分开。
      const isAI = (type === "ai");
      const isMine = !isAI && m.from === myId && (!m.dev || !myDev || m.dev === myDev);
      let cls = (type === "system") ? "hive-msg sys"
        : isAI ? "msg bot"
        : isMine ? "msg user"
        : "msg member";
      if (type === "unknown") cls += " unknown";
      wrap.className = cls;
      wrap.setAttribute("data-mid", m.id || m.mid || "");
      // 跨版本：对方用更新版本发来的类型，本机不认识 —— **照样显示原文**，
      // 只标一句「你的版本看不懂」，而不是让消息凭空消失。
      // AI 消息的 m.name 记的是**唤醒者**的名字，直接显示就成了「我（由我唤醒）」，
      // 看着像自己发的。这里统一显示「@hd（由 我 唤醒）」。
      const whoName = isAI ? "🤖 @hd" : (m.name || m.from || "?");
      const who = type === "system" ? ""
        : '<span class="hmsg-who">' + hEsc(whoName)
          + (!isAI && m.from === myId && m.dev && myDev && m.dev !== myDev
            ? '<span class="hmsg-dup">（同身份的另一台设备）</span>' : "")
          + (isAI && m.by ? "（由 " + hEsc(m.by) + " 唤醒）" : "")
          + (type === "unknown"
            ? "（" + hEsc(m.orig_type || "新类型") + " · 你的版本看不懂，已按原文显示）"
            : "")
          + (m.pending ? " · 待发送" : "") + "</span>";
      // 思考过程：**默认收起**。它可能几千字，展开会把整个聊天记录冲走；
      // 但必须存下来且能点开 —— 后来进群的人也要能回看当时模型在想什么。
      let think = "";
      if (type === "ai" && m.thinking) {
        const tc = String(m.thinking).length;
        const dur = m.dur_ms ? " · 耗时 " + (m.dur_ms / 1000).toFixed(1) + "s" : "";
        // 和单人模式**同一个结构**：.bubble.think-hdr + .think-body，
        // 点击折叠、带过渡动画、位置也在正文气泡正上方（原来是 details.hmsg-think，
        // 原生折叠条，样式和位置都和单人模式不同 —— 用户截图指出来的就是这里）。
        think = '<div class="bubble think-hdr"><span>🧠 思考过程（'
          + tc + " 字" + dur + '）</span><span class="arrow">▸</span></div>'
          + '<div class="think-body">'
          + hEsc(m.thinking).replace(/\n/g, "<br>") + "</div>";
      }
      if (type === "ai" && m.cancelled) {
        think = '<div class="hmsg-cancel">⏹ 这次任务被取消了</div>' + think;
      }
      const ts = m.ts ? new Date(m.ts * 1000).toLocaleTimeString("zh-CN",
        { hour: "2-digit", minute: "2-digit" }) : "";

      wrap.innerHTML = who + think + refBlock(m)
        + '<div class="bubble hmsg-body"></div>'
        + '<div class="hive-acts"></div>'
        + (ts ? '<span class="hmsg-ts">' + ts + "</span>" : "");

      // 思考卡片：点一下展开/收起（交互和单人模式一致，带 CSS 过渡）
      const th = wrap.querySelector(".think-hdr");
      if (th) {
        const tb = wrap.querySelector(".think-body");
        th.addEventListener("click", () => {
          const open = tb.classList.contains("open");
          tb.classList.toggle("open", !open);
          th.querySelector(".arrow").textContent = open ? "▸" : "▾";
        });
      }

      const bubble = wrap.querySelector(".hmsg-body");
      const raw = String(m.text == null ? "" : m.text);
      if (type === "system" || type === "unknown" || m.pending) {
        bubble.textContent = raw;
        bubble.style.whiteSpace = "pre-wrap";
      } else {
        try { renderFinal(bubble, raw, "", true); }
        catch (e) {
          bubble.textContent = raw;
          bubble.style.whiteSpace = "pre-wrap";
        }
      }
      const fh = fileCards(m);
      if (fh) bubble.insertAdjacentHTML("beforeend", fh);

      const acts = wrap.querySelector(".hive-acts");
      if (type !== "system" && (raw || (m.files || []).length)) {
        if (raw) {
          acts.appendChild(mkAct("复制", () => copyText(raw, acts)));
          acts.appendChild(mkAct("引用", () => setQuote(m)));
        }
        if (type === "ai" && raw) {
          const b = mkAct("🔊", () => {});
          b.title = "朗读这段";
          b.onclick = (e) => { e.stopPropagation(); ttsPlay(raw, b); };
          acts.appendChild(b);
        }
        if (!acts.children.length) acts.remove();
      } else {
        acts.remove();
      }
      return wrap;
    }

    // ---------------------------------------------------------- @hd 实时进度
    // 每个正在跑的任务一张卡片：思考区和正文区边跑边刷新。
    // 用 mid 做 key —— 多人同时 @ 时各看各的（执行端是串行的，但卡片要分开）。
    const hdCards = {};

    function hdEnsure(f) {
      const mid = f.mid || "?";
      let c = hdCards[mid];
      if (c && document.body.contains(c.node)) return c;
      const box = el("hiveMsgs");
      const empty = box.querySelector(".hive-empty");
      if (empty) empty.remove();
      const node = document.createElement("div");
      // 实时进度卡片同样用单人模式那套 class（.msg.bot / .bubble / .think-hdr）
      node.className = "msg bot hd-live";
      node.innerHTML = '<span class="hmsg-who"></span>'
        + '<div class="hd-live-head"><span class="hd-dot"></span>'
        + '<span class="hd-phase">排队中…</span>'
        + '<button class="hd-cancel" type="button">取消</button></div>'
        + '<div class="bubble think-hdr hd-think"><span>🧠 思考过程</span>'
        + '<span class="arrow">▸</span></div>'
        + '<div class="think-body"></div>'
        + '<div class="bubble hmsg-body hd-out"></div>';
      box.appendChild(node);
      box.scrollTop = box.scrollHeight;
      c = {
        node: node, think: node.querySelector(".think-body"),
        out: node.querySelector(".hd-out"),
        phase: node.querySelector(".hd-phase"),
        who: node.querySelector(".hmsg-who"),
        details: node.querySelector(".hd-think"),
      };
      // 实时卡片的思考框：点标题也能折叠（和正式消息一致）
      c.details.addEventListener("click", () => {
        const open = c.think.classList.contains("open");
        c.think.classList.toggle("open", !open);
        c.details.querySelector(".arrow").textContent = open ? "▸" : "▾";
      });
      node.querySelector(".hd-cancel").addEventListener("click", () => hdCancel(mid));
      hdCards[mid] = c;
      hvDock(node);          // 「跟随视角」开着就浮到左上角（和单人模式一致）
      return c;
    }

    async function hdCancel(mid) {
      if (!curGid) return;
      try {
        const d = await api("/api/hive/" + encodeURIComponent(curGid) + "/hd/cancel",
          { task_id: mid });
        if (!d.ok) alert(d.error || "取消失败");
      } catch (e) { alert("取消失败：" + e.message); }
    }

    function hdStream(f) {
      const c = hdEnsure(f);
      const t = f.task || {};
      const ph = f.phase;
      const box = el("hiveMsgs");
      if (ph === "queued") {
        c.phase.textContent = (t.ahead > 0)
          ? "排队中（前面还有 " + t.ahead + " 个任务）" : "已受理，准备开始…";
        c.who.textContent = (t.asker || "成员") + " 唤醒了模型";
      } else if (ph === "start") {
        c.who.textContent = (t.asker || "成员") + " 唤醒了模型";
        c.phase.textContent = "模型 " + (t.model || "") + " 正在思考…（参考了 "
          + (t.hist || 0) + " 条上下文）";
      } else if (ph === "thinking") {
        c.phase.textContent = "思考中… " + (t.chars || 0) + " 字";
        // 有思考内容了才展开，避免一开始就是一个空的折叠框
        if (!c.think.classList.contains("open")) {
          c.think.classList.add("open");
          c.details.querySelector(".arrow").textContent = "▾";
        }
        c.think.innerHTML += hEsc(f.delta || "").replace(/\n/g, "<br>");
      } else if (ph === "tool") {
        // ★ 2026-11-13：群聊 @hd 自开发时，模型在**真改代码** ——
        //   必须把「正在动哪个工具」显示出来，否则界面上只有思考在滚，
        //   看不出它到底有没有在干活（用户会以为又只是嘴上说说）。
        c.phase.textContent = "正在执行工具（第 " + (t.round || 1) + " 轮）："
          + ((t.tools || []).join("、") || "");
      } else if (ph === "content") {
        c.phase.textContent = "正在输出… " + (t.chars || 0) + " 字";
        // 流式阶段用**轻量渲染**（转义 + 换行），不走 renderFinal：
        // 每次增量都全量重渲染会让代码块反复重建、滚动位置乱跳。
        // 跑完后正式消息会由 msg 事件补上，那条走完整的 renderFinal。
        c.out.innerHTML += hEsc(f.delta || "").replace(/\n/g, "<br>");
      } else if (ph === "done") {
        c.phase.textContent = t.cancelled ? "已取消"
          : (t.error ? "执行失败" : "完成");
        setStopBtn(false);
        // 正式消息马上会由 msg 事件补上（它带完整思考和耗时），
        // 这里延时移除卡片，别抢在正式消息前面闪一下。
        setTimeout(() => {
          try { c.node.remove(); } catch (e) { /* 忽略 */ }
          delete hdCards[f.mid || "?"];
        }, 1200);
        return;
      }
      setStopBtn(true, f.mid);
      box.scrollTop = box.scrollHeight;
    }

    // 输入区的「■ 终止」：有任务在跑才显示，点了取消当前那个。
    // 为什么还要这个按钮 —— 实时卡片上已经有「取消」了，但卡片可能被滚出视野，
    // 而用户盯着输入框的时候最想按的就是这里（和单人模式的位置一致）。
    let stopMid = "";
    function setStopBtn(on, mid) {
      const b = el("hiveStopBtn");
      if (!b) return;
      if (on && mid) stopMid = mid;
      b.hidden = !on;
      if (!on) stopMid = "";
    }

    // ---------------------------------------------------------- 底部工具条
    // ★ 2026-11-13 用户反馈「跟随视角这些按钮好像也没了」。
    //   单人模式输入框下面那一排（上下文环 / 采样 / 跟随最新 / 跟随视角）群聊里
    //   一个都没有。按钮已经补进 HTML，这里让它们**真的生效**，不是摆设：
    //   ① 跟随最新 → 接管 hiveMsgs 的滚动（做法和单人模式完全一致）
    //   ② 跟随视角 → 把正在跑的实时卡片浮到视口左上角
    //   ③ 上下文环 → 按群聊消息算占用
    //   采样按钮走 onclick="__showSampling()"，和单人模式共用一个面板，无需额外代码。
    let hvLastMsgs = [];        // 最近一次渲染的群消息（上下文环要用）

    // ① 跟随最新
    let hvFreeView = false, hvKeepTop = 0;
    function hvSyncFree() {
      const b = el("hvFreeViewBtn");
      if (!b) return;
      b.classList.toggle("fv-on", hvFreeView);
      b.textContent = hvFreeView ? "📌 自由查看中" : "⬇ 跟随最新";
      b.title = hvFreeView
        ? "已暂停自动滚动 —— 可以停在任意位置慢慢看。点一下恢复跟随最新"
        : "AI 有新内容时自动滚到最新。点一下暂停，就能停在某一步慢慢看";
    }
    (function bindHvFree() {
      const box = el("hiveMsgs");
      const b = el("hvFreeViewBtn");
      if (!box) return;
      // 和单人模式同一招：直接接管 scrollTop 的写入。
      // 群聊里有 4 处 `box.scrollTop = box.scrollHeight`，逐个改容易漏，
      // 拦截写入能一次全兜住（自由查看期间一律写回用户停留的位置）。
      const _d = Object.getOwnPropertyDescriptor(Element.prototype, "scrollTop");
      if (_d && _d.set) {
        Object.defineProperty(box, "scrollTop", {
          configurable: true,
          get() { return _d.get.call(this); },
          set(v) {
            if (hvFreeView) {
              const max = Math.max(0, box.scrollHeight - box.clientHeight);
              _d.set.call(this, Math.min(hvKeepTop, max));
              return;
            }
            _d.set.call(this, v);
          },
        });
      }
      if (b) {
        b.onclick = () => {
          hvFreeView = !hvFreeView;
          if (hvFreeView) hvKeepTop = box.scrollTop;
          hvSyncFree();
          if (!hvFreeView) box.scrollTop = box.scrollHeight;
        };
      }
      // 用户自己往上滚 → 自动进入自由查看，否则刚滚上去就被拽回底部
      box.addEventListener("scroll", () => {
        const gap = box.scrollHeight - box.scrollTop - box.clientHeight;
        if (!hvFreeView && gap > 60) { hvFreeView = true; hvSyncFree(); }
        if (hvFreeView) hvKeepTop = box.scrollTop;
      }, { passive: true });
      hvSyncFree();
    })();

    // ② 跟随视角：正在跑的实时卡片浮到视口左上角
    //    和单人模式共用同一个偏好（localStorage.think_follow），
    //    这样两种模式里选一次，切过去还是同一个行为。
    let hvThinkFollow = localStorage.getItem("think_follow") !== "0";
    function hvSyncThink() {
      const b = el("hvThinkFollowBtn");
      if (!b) return;
      b.classList.toggle("tf-on", hvThinkFollow);
      b.textContent = hvThinkFollow ? "🧠 跟随视角" : "📍 回到原位";
      b.setAttribute("aria-pressed", hvThinkFollow ? "true" : "false");
      b.title = hvThinkFollow
        ? "当前：跟随视角 —— 思考框固定在视口左上角，滚到哪都看得到。点一下改为「回到原位」"
        : "当前：回到原位 —— 思考框待在消息流原来的位置。点一下改为「跟随视角」";
    }
    // 把某张实时卡片按当前偏好摆好（.think-dock 的 fixed 样式与单人模式共用）
    // 群聊的「归位」：只把这张实时卡片放回群消息流，全局「跟随视角」不变
    function hvSendHome(node) {
      if (!node || !node.isConnected) return;
      node._noFollow = true;
      node.classList.remove("think-dock");
      node.style.left = node.style.top = node.style.width = node.style.bottom = "";
      const box = el("hiveMsgs");
      if (box) box.appendChild(node);
      // ★ 2026-11-13 同上：没有别的实时卡片在跟随时，把群聊的「跟随视角」开关一并关掉，
      //   否则开关仍显示「跟随视角」，用户点一次只是关掉它，得连点两次才恢复。
      if (hvThinkFollow) {
        const anyFloat = Object.keys(hdCards).some((k) => {
          const n = hdCards[k] && hdCards[k].node;
          return n && n.isConnected && n.classList.contains("think-dock") && !n._noFollow;
        });
        if (!anyFloat) {
          hvThinkFollow = false;
          try { localStorage.setItem("think_follow", "0"); } catch (e) { /* 隐私模式 */ }
          hvSyncThink();
        }
      }
      if (typeof showToast === "function") showToast("思考卡片已归位", "");
    }
    function hvDock(node) {
      if (!node || !node.isConnected) return;
      if (node._noFollow) return;   // 点过「归位」的卡片不再被跟随拽起来
      if (!hvThinkFollow) {
        if (node.classList.contains("think-dock")) {
          node.classList.remove("think-dock");
          node.style.left = node.style.top = node.style.width = "";
          const box = el("hiveMsgs");
          if (box) box.appendChild(node);
        }
        return;
      }
      if (!node.classList.contains("think-dock")) {
        node.classList.add("think-dock");
        document.body.appendChild(node);
      }
      try { placeThinkDock(node); } catch (e) { /* 顶层函数不可用时忽略 */ }
      // 单人模式那个函数在本文件顶层，正常同作用域；用 typeof 兜底，避免万一取不到就抛错
      if (typeof ensureThinkHome === "function") ensureThinkHome(node, hvSendHome);
    }
    (function bindHvThink() {
      const b = el("hvThinkFollowBtn");
      if (b) {
        b.onclick = () => {
          hvThinkFollow = !hvThinkFollow;
          try { localStorage.setItem("think_follow", hvThinkFollow ? "1" : "0"); } catch (e) { /* 隐私模式 */ }
          // 主动打开跟随 → 之前点过「归位」的卡片重新纳入跟随
          if (hvThinkFollow) {
            Object.keys(hdCards).forEach((k) => {
              const n = hdCards[k] && hdCards[k].node;
              if (n) n._noFollow = false;
            });
          }
          hvSyncThink();
          // 已存在的实时卡片立刻跟着动
          Object.keys(hdCards).forEach((k) => hvDock(hdCards[k] && hdCards[k].node));
          if (typeof showToast === "function") {
            showToast(hvThinkFollow ? "跟随视角：思考框固定在视口左上角" : "回到原位：思考框已放回消息流", "");
          }
        };
      }
      hvSyncThink();
    })();
    window.addEventListener("resize", () => {
      document.querySelectorAll("body > .hd-live.think-dock").forEach((n) => {
        try { placeThinkDock(n); } catch (e) { /* 忽略 */ }
      });
    });

    // ③ 上下文环：按群聊消息估算占用（群聊的「上下文」= 最近这些消息）
    (function hvCtx() {
      const btn = el("hvCtxRing"), fg = el("hvCtxRingFg"), txt = el("hvCtxRingTxt");
      if (!btn || !fg || !txt) return;
      const C = 2 * Math.PI * 15.5;
      let busy = false, lastN = -1;
      const colorOf = (r) => {
        const g = [34, 197, 94], o = [245, 158, 11], rd = [239, 68, 68];
        const mix = (a, b, t) => a.map((v, i) => Math.round(v + (b[i] - v) * t));
        const c = r <= 0.7 ? mix(g, o, Math.max(0, r) / 0.7)
                           : mix(o, rd, Math.min(1, (r - 0.7) / 0.3));
        return "rgb(" + c.join(",") + ")";
      };
      const kk = (n) => (Number(n) >= 10000 ? (Number(n) / 10000).toFixed(1) + "万" : String(Number(n) || 0));
      async function refresh(force) {
        const msgs = hvLastMsgs.map((m) => ({
          role: (m.from === myId) ? "user" : "assistant",
          content: String(m.text || ""),
        }));
        if (busy) return;
        if (!force && msgs.length === lastN) return;
        busy = true;
        try {
          const res = await fetch("/api/context/usage", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ messages: msgs, session: "" }),
          });
          const d = await res.json();
          if (!d || !d.ok) return;
          lastN = msgs.length;
          const r = Math.max(0, Math.min(1, Number(d.ratio) || 0));
          fg.style.strokeDashoffset = String(C * (1 - r));
          fg.style.stroke = colorOf(r);
          txt.textContent = r >= 0.995 ? "满" : Math.round(r * 100) + "%";
          txt.style.color = r >= 0.85 ? colorOf(r) : "#9a9aa2";
          btn.title = "群聊上下文占用 " + (r * 100).toFixed(1) + "%\n"
            + "已用 " + kk(d.used) + " / " + kk(d.limit) + " token\n"
            + "· 按群里的 " + msgs.length + " 条消息估算";
        } catch (e) { /* 后端忙时忽略，下次轮询再来 */ }
        finally { busy = false; }
      }
      setInterval(() => refresh(false), 3000);
      setTimeout(() => refresh(true), 1200);
    })();

    const stopEl = el("hiveStopBtn");
    if (stopEl) {
      stopEl.addEventListener("click", () => {
        if (stopMid) hdCancel(stopMid);
        else {
          // 没拿到 mid（比如刚进群时任务已经在跑）：让后端按群取消
          hdCancel("");
        }
      });
    }

    function renderMsgs(list) {
      const box = el("hiveMsgs");
      box.innerHTML = "";
      hvLastMsgs = Array.isArray(list) ? list : [];   // 上下文环要用
      // ⚠️ 索引必须**先**建好再渲染：引用块要靠它找回被引用的原文，
      //    边渲染边建的话，前面那条引用后面才渲染的消息就查不到了。
      msgIndex = {};
      list.forEach((m) => { if (m && m.id) msgIndex[m.id] = m; });
      if (!list.length) {
        box.innerHTML = '<p class="hint hive-empty">还没有消息，说点什么吧。</p>';
        return;
      }
      list.forEach((m) => box.appendChild(msgNode(m)));
      box.scrollTop = box.scrollHeight;
    }

    function addMsg(m) {
      const box = el("hiveMsgs");
      const empty = box.querySelector(".hive-empty");
      if (empty) empty.remove();
      if (m && m.id) msgIndex[m.id] = m;
      box.appendChild(msgNode(m));
      box.scrollTop = box.scrollHeight;
    }

    async function sendMsg() {
      const ta = el("hiveText");
      const text = (ta.value || "").trim();
      const files = pendingFiles.map((f) => f.meta).filter(Boolean);
      if (!text && !files.length) return;
      if (!curGid) { alert("先在左边选一个群"); return; }
      if (uploading) { alert("附件还在上传，等一下再发"); return; }
      ta.value = "";
      const refId = quoteMsg ? (quoteMsg.id || "") : "";
      try { atBtnSync(); } catch (e) { /* 函数在后面定义，首次调用可能未就绪 */ }
      try {
        const body = { text: text };
        if (files.length) body.extra = { files: files };
        if (refId) body.ref = refId;
        const d = await api("/api/hive/" + encodeURIComponent(curGid) + "/send", body);
        if (!d.ok) { alert(d.error || "发送失败"); return; }
        // 发成功才清空附件与引用 —— 失败还留着，用户可以重发
        pendingFiles = [];
        renderAttachBar();
        clearQuote();
        // 没连中继时后端会本地落盘并推送；连了中继则等 WS 回执，避免重复显示
        if (d.sent && d.sent.msg && d.sent.local) { /* 已由广播推送 */ }
      } catch (e) {
        alert("发送失败：" + e.message);
      }
    }

    // ---------------------------------------------------------- 附件上传
    //
    // 流程：选文件 → 读成 dataURL → POST /upload 落盘 → 拿到 meta
    //      → 发消息时把 meta 列表放进 extra.files。
    // 为什么**先上传再发消息**：文件要先落进群目录才能被广播出去；
    // 而且上传失败时消息不该发出去（否则群里会出现一条指向空文件的消息）。
    let pendingFiles = [];        // [{name,size,kind,preview,meta,up}]
    let uploading = false;
    const HIVE_MAX_FILE = 20 * 1024 * 1024;

    function renderAttachBar() {
      const bar = el("hiveAttachBar");
      if (!bar) return;
      if (!pendingFiles.length) {
        bar.hidden = true;
        bar.innerHTML = "";
        return;
      }
      bar.hidden = false;
      bar.innerHTML = pendingFiles.map((f, i) => {
        const img = f.preview
          ? '<img src="' + f.preview + '" alt="">'
          : '<span class="hc-ico">' + fileIco({ name: f.name, kind: f.kind }) + "</span>";
        return '<div class="hive-chip' + (f.up ? " up" : "") + '">'
          + img
          + '<span class="hc-name" title="' + hEsc(f.name) + '">' + hEsc(f.name) + "</span>"
          + '<span class="hc-size">' + (f.up ? "上传中…" : fmtSize(f.size)) + "</span>"
          + '<button type="button" class="hc-x" data-del="' + i + '" title="移除">✕</button>'
          + "</div>";
      }).join("");
      Array.prototype.forEach.call(bar.querySelectorAll("[data-del]"), (b) => {
        b.addEventListener("click", () => {
          const i = parseInt(b.getAttribute("data-del"), 10);
          if (!isNaN(i)) pendingFiles.splice(i, 1);
          renderAttachBar();
        });
      });
    }

    function readAsDataURL(file) {
      return new Promise((res, rej) => {
        const r = new FileReader();
        r.onload = () => res(String(r.result || ""));
        r.onerror = () => rej(new Error("这个文件读不出来"));
        r.readAsDataURL(file);
      });
    }

    async function addFiles(list) {
      if (!curGid) { alert("先在左边选一个群，再上传附件"); return; }
      const arr = Array.prototype.slice.call(list || []);
      if (!arr.length) return;
      uploading = true;
      for (const file of arr) {
        if (file.size > HIVE_MAX_FILE) {
          alert("「" + file.name + "」太大（" + fmtSize(file.size) + "），单个上限 "
            + fmtSize(HIVE_MAX_FILE));
          continue;
        }
        const isImg = /^image\//.test(file.type || "");
        const item = {
          name: file.name, size: file.size,
          kind: isImg ? "image" : "doc",
          preview: "", meta: null, up: true,
        };
        pendingFiles.push(item);
        renderAttachBar();
        try {
          const dataUrl = await readAsDataURL(file);
          if (isImg) item.preview = dataUrl;      // 缩略图先用本地 dataURL，秒出
          const r = await api("/api/hive/" + encodeURIComponent(curGid) + "/upload",
            { name: file.name, mime: file.type || "", data: dataUrl });
          if (!r.ok) throw new Error(r.error || "上传失败");
          item.meta = r.file;
          item.kind = r.file.kind || item.kind;
          item.up = false;
        } catch (e) {
          item.up = false;
          pendingFiles = pendingFiles.filter((x) => x !== item);
          alert("「" + file.name + "」上传失败：" + e.message);
        }
        renderAttachBar();
      }
      uploading = false;
    }

    // 三个上传入口（图片 / 文档 / 视频）走同一个 addFiles，
    // accept 只影响选择框过滤，最终按真实 mime 归类。
    el("hiveAttachBtn").addEventListener("click", () => el("hiveFileInput").click());
    el("hiveDocBtn").addEventListener("click", () => el("hiveDocInput").click());
    el("hiveVideoBtn").addEventListener("click", () => el("hiveVideoInput").click());
    ["hiveFileInput", "hiveDocInput", "hiveVideoInput"].forEach((id) => {
      const inp = el(id);
      if (inp) {
        inp.addEventListener("change", () => {
          addFiles(inp.files);
          inp.value = "";          // 清空，否则同一个文件选第二次不触发 change
        });
      }
    });

    // 拖拽：全局那套在 document 捕获阶段就接走了，所以这里把入口暴露给外层，
    // 由外层判断"当前是群聊模式"再转过来（见 app.js 的 drop 处理）。
    window.__hiveOnDrop = (files) => addFiles(files);
    window.__hiveMode = false;

    // 引用条关闭
    const qx = el("hiveQuoteX");
    if (qx) qx.addEventListener("click", clearQuote);

    el("hiveSendBtn").addEventListener("click", sendMsg);
    el("hiveText").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMsg(); }
    });

    // ---------------------------------------------------------- @hd 快捷开关
    // 点一下在开头插入「@hd 」，再点一下撤掉。手输 @hd 时按钮也会自动高亮，
    // 免得用户以为没生效又点一次（那就把刚写的指令撤掉了）。
    const HD_TAG = "@hd";
    // 判断开头是不是真的 @hd —— 后面紧跟字母数字的不算（@hdx / @hd2），
    // 否则点一下会把 @hd 削掉、剩下「x 干活」这种残句。和后端 parse_hd 一致。
    function hdAtStart(body) {
      if (body.slice(0, HD_TAG.length).toLowerCase() !== HD_TAG) return false;
      const nxt = body.slice(HD_TAG.length, HD_TAG.length + 1);
      return !(/[a-z0-9_]/i.test(nxt));
    }
    function atBtnSync() {
      const b = el("hiveAtBtn");
      const ta = el("hiveText");
      if (!b || !ta) return;
      const on = hdAtStart((ta.value || "").replace(/^\s+/, ""));
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    }

    function atBtnToggle() {
      const ta = el("hiveText");
      if (!ta) return;
      const body = (ta.value || "").replace(/^\s+/, "");
      if (hdAtStart(body)) {
        // 撤掉 @hd，连同紧跟的空白 / 标点，避免留下「 帮我写…」开头的空格
        ta.value = body.slice(HD_TAG.length).replace(/^[\s，,：:、]+/, "");
      } else {
        ta.value = HD_TAG + " " + body;
      }
      atBtnSync();
      ta.focus();
      try { ta.setSelectionRange(ta.value.length, ta.value.length); } catch (e) { /* 忽略 */ }
    }

    el("hiveAtBtn").addEventListener("click", atBtnToggle);
    el("hiveText").addEventListener("input", atBtnSync);
    atBtnSync();

    // ---------------------------------------------------------- 成员
    // 「3 分钟前」这种相对时间 —— 离线的人要能看出离开了多久
    function fmtAgo(ts) {
      if (!ts) return "";
      const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
      if (s < 60) return "刚刚";
      if (s < 3600) return Math.floor(s / 60) + " 分钟前";
      if (s < 86400) return Math.floor(s / 3600) + " 小时前";
      return Math.floor(s / 86400) + " 天前";
    }

    // 退出按钮显隐：群主退出会让群没有主人，所以不给按钮，只显示一句提示。
    // amOwner 由 renderMembers 算好传进来；没传就自己按成员表判断。
    function syncLeaveBtn(amOwner) {
      const btn = el("hiveLeaveBtn");
      const tip = el("hiveLeaveTip");
      if (!btn || !tip) return;
      if (amOwner === undefined) {
        amOwner = members.some((m) => (m.uid || m.user_id) === myId && m.role === "owner");
      }
      btn.hidden = !!amOwner || !curGid;
      tip.hidden = !amOwner || !curGid;
    }

    el("hiveLeaveBtn").addEventListener("click", async () => {
      if (!curGid) return;
      const g = groups.find((x) => x.gid === curGid);
      const nm = g ? g.name : curGid;
      if (!confirm("确定退出「" + nm + "」？\\n\\n"
        + "· 群里其他人会看到一条「你退出了群」的提示\\n"
        + "· 这个群会从你的列表里消失\\n"
        + "· 历史消息仍留在本机，重新用密码进群可以再回来")) return;
      const gid = curGid;
      try {
        const d = await api("/api/hive/" + encodeURIComponent(gid) + "/leave", {});
        if (!d.ok) { alert(d.error || "退出失败"); return; }
        // 退出成功：清掉当前会话，回到「未选择群」状态再刷新列表
        curGid = "";
        lastSeq = 0;
        members = [];
        el("hiveGroupName").textContent = "未选择群";
        el("hiveGroupMeta").textContent = "";
        el("hiveSide").hidden = true;
        el("hiveMsgs").innerHTML = '<p class="hint hive-empty">已退出「'
          + hEsc(nm) + "」。左边选一个群，或者点「＋ 建群」开一个新的。</p>";
        await loadGroups();
      } catch (e) { alert("退出失败：" + e.message); }
    });

    // ============================================================
    // 📡 点对点直传（G 级文件，不经任何服务器）
    // ------------------------------------------------------------
    // 连接信息靠「连接码」人工传递（二维码 / 复制粘贴），文件内容 TCP 直连。
    // 面板用 fixed 浮层，不参与消息区 flex 计算（避免又压扁输入框）。
    // ============================================================
    let p2pTimer = null;
    let p2pCurShare = "";

    function p2pFmtEta(s) {
      if (s === null || s === undefined) return "—";
      s = Math.max(0, Math.round(s));
      if (s < 60) return s + " 秒";
      if (s < 3600) return Math.floor(s / 60) + " 分 " + (s % 60) + " 秒";
      return Math.floor(s / 3600) + " 时 " + Math.floor((s % 3600) / 60) + " 分";
    }

    function p2pStateText(t) {
      const map = {
        waiting: "排队中", listening: "等待对方连接", handshake: "握手中",
        connecting: "连接中", transferring: "传输中", paused: "已暂停",
        done: "已完成", error: "失败", canceled: "已取消",
      };
      return map[t.state] || t.state || "";
    }

    function p2pOpen() {
      const m = el("p2pMask");
      if (!m) return;
      m.hidden = false;
      p2pRefresh();
      if (!p2pTimer) p2pTimer = setInterval(p2pRefresh, 1000);
    }

    function p2pClose() {
      const m = el("p2pMask");
      if (m) m.hidden = true;
      if (p2pTimer) { clearInterval(p2pTimer); p2pTimer = null; }
    }

    // 点附件上的 📡：把本机这个文件设为可共享，显示二维码 + 连接码
    async function p2pShare(fid, name) {
      if (!curGid) { alert("先选一个群"); return; }
      p2pOpen();
      const box = el("p2pShare");
      const code = el("p2pCode");
      const fn = el("p2pFname");
      const addr = el("p2pAddr");
      if (box) box.hidden = false;
      if (fn) fn.textContent = name || "准备中…";
      if (code) code.value = "正在开启监听…";
      if (addr) addr.textContent = "";
      try {
        const d = await api("/api/hive/" + encodeURIComponent(curGid) + "/p2p/share",
          { fid: fid, upnp: true });
        if (!d.ok) {
          if (code) code.value = "";
          if (fn) fn.textContent = "开启失败";
          if (addr) addr.textContent = d.error || "";
          return;
        }
        p2pCurShare = d.code || "";
        if (code) code.value = d.code || "";
        if (fn) fn.textContent = name || "文件";
        if (addr) {
          const up = d.upnp || {};
          let tip = "本机地址 " + (d.lan_ip || "") + ":" + (d.port || "");
          if (up.ok) tip = "已用路由器映射到公网 " + (up.ip || "") + ":" + (d.port || "");
          else if (up.why) tip = "本机地址 " + (d.lan_ip || "") + ":" + (d.port || "")
            + "（" + up.why + "，公网需手动做端口映射或已组网）";
          addr.textContent = tip;
        }
        p2pRefresh();
      } catch (e) {
        if (code) code.value = "";
        if (addr) addr.textContent = "开启失败：" + e.message;
      }
    }

    // 共享本机任意文件：G 级文件的真正入口（不走上传，不复制，不占额外磁盘）
    async function p2pOpenFile() {
      const inp = el("p2pPath");
      const path = ((inp && inp.value) || "").trim().replace(/^["']|["']$/g, "");
      if (!path) { alert("先把文件的完整路径填进来"); return; }
      if (!curGid) { alert("先选一个群"); return; }
      const box = el("p2pShare");
      const code = el("p2pCode");
      const fn = el("p2pFname");
      const addr = el("p2pAddr");
      if (box) box.hidden = false;
      if (fn) fn.textContent = "正在开启…";
      if (code) code.value = "";
      if (addr) addr.textContent = "";
      try {
        const d = await api("/api/hive/" + encodeURIComponent(curGid) + "/p2p/open",
          { path: path });
        if (!d.ok) {
          if (fn) fn.textContent = "开启失败";
          if (addr) addr.textContent = d.error || "";
          return;
        }
        p2pCurShare = d.code || "";
        if (code) code.value = d.code || "";
        if (fn) fn.textContent = d.name || "文件";
        if (addr) {
          const up = d.upnp || {};
          let tip = "本机地址 " + (d.lan_ip || "") + ":" + (d.port || "");
          if (up.ok) tip = "已用路由器映射到公网 " + (up.ip || "") + ":" + (d.port || "");
          else if (up.why) tip = "本机地址 " + (d.lan_ip || "") + ":" + (d.port || "")
            + "（" + up.why + "，公网需手动端口映射或已组网）";
          addr.textContent = tip + " · " + fmtSize(d.size || 0);
        }
        p2pRefresh();
      } catch (e) {
        if (fn) fn.textContent = "开启失败";
        if (addr) addr.textContent = e.message;
      }
    }

    async function p2pReceive() {
      const inp = el("p2pInput");
      const code = (inp && inp.value || "").trim();
      if (!code) { alert("先把连接码粘进来"); return; }
      if (!curGid) { alert("先选一个群"); return; }
      try {
        const d = await api("/api/hive/" + encodeURIComponent(curGid) + "/p2p/receive",
          { code: code });
        if (!d.ok) { alert(d.error || "接收失败"); return; }
        if (inp) inp.value = "";
        p2pRefresh();
      } catch (e) { alert("接收失败：" + e.message); }
    }

    async function p2pAct(tid, act) {
      try {
        await api("/api/hive/" + encodeURIComponent(curGid) + "/p2p/"
          + encodeURIComponent(tid) + "/" + act, {});
        p2pRefresh();
      } catch (e) { /* 静默：下一秒轮询会刷新真实状态 */ }
    }

    async function p2pRefresh() {
      if (!curGid) return;
      const box = el("p2pList");
      if (!box || el("p2pMask").hidden) return;
      try {
        const d = await api("/api/hive/" + encodeURIComponent(curGid) + "/p2p/transfers");
        if (!d.ok) return;
        const list = d.transfers || [];
        const bs = (d.beacons || []).filter((b) => b && b.code);
        // 局域网发现：同网段有人共享时会自动冒出来，点一下就收（公网仍需人工传码）
        const beaconHtml = bs.length
          ? '<div class="p2p-beacon"><div class="p2p-bt">📶 局域网里有人在共享（点一下就能收）</div>'
            + bs.map((b) => '<div class="p2p-brow">'
              + '<span class="p2p-name" title="' + hEsc(b.name || "") + '">'
              + hEsc(b.name || "文件") + "</span>"
              + '<span class="p2p-st">' + fmtSize(b.size || 0) + "</span>"
              + '<button class="p2p-mini" data-p2pcode="' + hEsc(b.code) + '">接收</button>'
              + "</div>").join("")
            + "</div>"
          : "";
        if (!list.length) {
          box.innerHTML = beaconHtml
            || '<p class="p2p-empty">还没有传输任务。点附件上的 📡 开始共享，或粘贴连接码接收。</p>';
          return;
        }
        box.innerHTML = beaconHtml + list.map((t) => {
          const total = t.total || 0;
          const done = t.done || 0;
          const pct = total ? Math.min(100, done * 100 / total) : 0;
          const up = t.dir === "send";
          const cls = t.state === "done" ? "done" : (t.state === "error" ? "err"
            : (t.state === "paused" ? "paused" : ""));
          let line = fmtSize(done) + " / " + fmtSize(total);
          if (t.state === "transferring" && t.speed) {
            line += " · " + fmtSize(t.speed) + "/s · 剩 " + p2pFmtEta(t.eta);
          }
          if (t.state === "listening") line = fmtSize(total) + " · 等对方粘贴连接码";
          if (t.state === "done") line = fmtSize(total) + " · 已完成";
          if (t.error) line = t.error;
          let acts = "";
          if (t.state === "transferring" || t.state === "connecting"
            || t.state === "handshake" || t.state === "listening") {
            acts += '<button class="p2p-mini" data-p2pact="pause" data-tid="' + hEsc(t.tid) + '">暂停</button>';
          } else if (t.state === "paused") {
            acts += '<button class="p2p-mini" data-p2pact="resume" data-tid="' + hEsc(t.tid) + '">继续</button>';
          }
          if (t.state !== "done" && t.state !== "canceled") {
            acts += '<button class="p2p-mini danger" data-p2pact="cancel" data-tid="' + hEsc(t.tid) + '">取消</button>';
          }
          return '<div class="p2p-item ' + cls + '">'
            + '<div class="p2p-item-top">'
            + '<span class="p2p-dir ' + (up ? "up" : "") + '">' + (up ? "发送" : "接收") + "</span>"
            + '<span class="p2p-name" title="' + hEsc(t.name || "") + '">' + hEsc(t.name || "文件") + "</span>"
            + '<span class="p2p-st ' + cls + '">' + hEsc(p2pStateText(t))
            + (t.peers ? " · " + t.peers + " 人" : "") + "</span></div>"
            + '<div class="p2p-bar"><i style="width:' + pct.toFixed(1) + '%"></i></div>'
            + '<div class="p2p-item-bottom"><span class="sp">' + hEsc(line) + "</span>"
            + acts + "</div></div>";
        }).join("");
      } catch (e) { /* 网络抖动时静默，下次轮询再说 */ }
    }



    // 打开 / 关闭
    if (el("p2pOpenBtn")) el("p2pOpenBtn").addEventListener("click", p2pOpen);
    if (el("p2pClose")) el("p2pClose").addEventListener("click", p2pClose);
    if (el("p2pMask")) {
      el("p2pMask").addEventListener("click", (ev) => {
        if (ev.target === el("p2pMask")) p2pClose();   // 点遮罩关闭
      });
    }
    if (el("p2pOpenFile")) el("p2pOpenFile").addEventListener("click", p2pOpenFile);
    if (el("p2pPath")) {
      el("p2pPath").addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") { ev.preventDefault(); p2pOpenFile(); }
      });
    }
    if (el("p2pGo")) el("p2pGo").addEventListener("click", p2pReceive);
    if (el("p2pInput")) {
      el("p2pInput").addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") { ev.preventDefault(); p2pReceive(); }
      });
    }
    if (el("p2pCopy")) {
      el("p2pCopy").addEventListener("click", async () => {
        const t = (el("p2pCode") && el("p2pCode").value) || p2pCurShare;
        if (!t) return;
        try {
          await navigator.clipboard.writeText(t);
          el("p2pCopy").textContent = "已复制 ✓";
          setTimeout(() => { if (el("p2pCopy")) el("p2pCopy").textContent = "复制连接码"; }, 1500);
        } catch (e) {
          // 剪贴板被拒（非 https 或没授权）：退化成选中让用户自己 Ctrl+C
          const ta = el("p2pCode");
          if (ta) { ta.focus(); ta.select(); }
          alert("浏览器不让自动复制，已帮你选中，按 Ctrl+C 复制");
        }
      });
    }
    // 附件上的 📡 + 传输卡片上的按钮（事件委托）
    if (el("hiveMsgs")) {
      el("hiveMsgs").addEventListener("click", (ev) => {
        const b = ev.target && ev.target.closest && ev.target.closest(".hf-p2p");
        if (!b) return;
        ev.preventDefault();
        ev.stopPropagation();
        p2pShare(b.getAttribute("data-p2p"), b.getAttribute("data-name") || "");
      });
    }
    if (el("p2pList")) {
      el("p2pList").addEventListener("click", (ev) => {
        // 局域网发现的一键接收
        const g = ev.target && ev.target.closest && ev.target.closest("[data-p2pcode]");
        if (g) {
          const inp = el("p2pInput");
          if (inp) inp.value = g.getAttribute("data-p2pcode") || "";
          p2pReceive();
          return;
        }
        const b = ev.target && ev.target.closest && ev.target.closest("[data-p2pact]");
        if (!b) return;
        p2pAct(b.getAttribute("data-tid"), b.getAttribute("data-p2pact"));
      });
    }

    function renderMembers() {
      const box = el("hiveMemList");
      if (!box) return;
      const onN = members.filter((m) => m.online).length;
      el("hiveMemCount").textContent = "(" + onN + "/" + members.length + ")";
      if (!members.length) {
        box.innerHTML = '<p class="hint">暂无成员</p>';
        syncLeaveBtn();
        return;
      }
      const amOwner = members.some((m) => m.user_id === myId && m.role === "owner");
      box.innerHTML = members.map((m) => {
        const uid = m.uid || m.user_id;
        const isMe = uid === myId;
        const st = m.state || (m.online ? "online" : "offline");
        const role = m.role === "owner" ? "群主" : (m.role === "admin" ? "管理" : "");
        // 状态文字：在线 / 离线 3 分钟前 / 异常
        let stTxt, tip;
        if (st === "online") {
          stTxt = "在线";
          tip = m.name + " · 在线";
        } else if (st === "error") {
          stTxt = "异常";
          tip = m.name + " · 异常（连过但报错了，可能是版本或网络问题）";
        } else {
          stTxt = m.last_seen ? fmtAgo(m.last_seen) : "离线";
          tip = m.name + " · 离线" + (m.last_seen ? "（最后在线 " + fmtAgo(m.last_seen) + "）" : "");
        }
        // 对方协议版本比本机老 → 标 vN，省得奇怪"他怎么没有新功能"
        const vTag = (m.v && myProto && m.v < myProto)
          ? '<span class="hm-v" title="对方客户端协议 v' + m.v
            + "，比你老，可能没有最新功能\">v" + m.v + "</span>"
          : "";
        const kick = (amOwner && !isMe)
          ? '<button class="hive-kick" data-uid="' + hEsc(uid) + '"'
            + ' data-name="' + hEsc(m.name) + '" title="移出群">踢</button>'
          : "";
        return '<div class="hive-mem' + (m.online ? "" : " off") + '" title="' + hEsc(tip) + '">'
          + '<i class="hm-dot ' + st + '"></i>'
          + '<span class="hm-name">'
          + (isMe ? "★ " : "") + hEsc(m.name) + "</span>"
          + (role ? '<span class="hm-role">' + role + "</span>" : "")
          + vTag
          + '<span class="hm-st ' + st + '">' + hEsc(stTxt) + "</span>"
          + kick + "</div>";
      }).join("");
      syncLeaveBtn(amOwner);
      Array.prototype.forEach.call(box.querySelectorAll(".hive-kick"), (b) => {
        b.addEventListener("click", async () => {
          const uid = b.getAttribute("data-uid");
          const nm = b.getAttribute("data-name");
          if (!confirm("确定把「" + nm + "」移出群？")) return;
          try {
            const d = await api("/api/hive/" + encodeURIComponent(curGid) + "/kick",
              { user_id: uid });
            if (!d.ok) { alert(d.error || "踢人失败"); return; }
            if (curGid) openGroup(curGid);
          } catch (e) { alert("踢人失败：" + e.message); }
        });
      });
    }

    el("hiveMembersBtn").addEventListener("click", () => {
      const side = el("hiveSide");
      side.hidden = !side.hidden;
      if (!side.hidden) renderMembers();
    });

    // ---------------------------------------------------------- 入群密码（取代二维码）
    // 电脑没有摄像头，扫码这条路从来走不通；真正能用的一直是那串文本。
    // 现在换成 12 位密码：群主看得到、成员输得进，不用再传长串口令。
    async function showPassword() {
      if (!curGid) { alert("先选一个群"); return; }
      const gid = curGid;
      let d;
      try {
        d = await api("/api/hive/" + encodeURIComponent(gid) + "/invite");
      } catch (e) { alert("读取密码失败：" + e.message); return; }
      if (!d.ok) { alert(d.error || "读取密码失败"); return; }
      // 老群还没设过密码：群主点进来时直接生成一个
      if (!d.password && d.is_owner) {
        try {
          const r = await api("/api/hive/" + encodeURIComponent(gid) + "/password",
            { password: "" });
          if (r.ok) d = Object.assign({}, d, { password: r.password });
        } catch (e) { /* 下面照常提示 */ }
      }
      const pw = d.password || "";
      openModal({
        title: "「" + (el("hiveGroupName").textContent || "群") + "」的入群密码",
        hint: pw
          ? "把这 12 位密码告诉对方（微信、口头都行）。他点「🔑 密码入群」输进去，"
            + "同一个 WiFi 下就能直接进群 —— 不用传长串口令，也没有二维码。"
            + (d.is_owner
              ? "\n\n注意：重置密码会换群密钥，老成员必须用新密码重新进群。"
              : "\n\n只有群主能重置密码。")
          : "只有群主能看到入群密码。请让群主点「🔑 密码」查看后告诉你。",
        code: pw,
        noOk: !d.is_owner || !pw,
        okText: "重置密码",
        onOk: async () => {
          if (!confirm("重置后群密钥会变，所有老成员都必须用新密码重新进群。确定重置？")) {
            return false;
          }
          try {
            const r = await api("/api/hive/" + encodeURIComponent(gid) + "/password",
              { password: "" });
            if (!r.ok) { alert(r.error || "重置失败"); return false; }
            el("hiveModalCode").value = r.password || "";
            el("hiveModalCopy").hidden = false;
            el("hiveModalHint").textContent =
              "密码已更新。老成员需要用这个新密码重新进群。";
          } catch (e) { alert("重置失败：" + e.message); }
          return false;                       // 留在弹窗里，方便再复制一次
        },
      });
    }
    el("hiveQrBtn").addEventListener("click", showPassword);

    // ---------------------------------------------------------- 建群 / 入群
    // ---------------------------------------------------------- 建群向导
    function hsLog(level, text, ms) {
      const box = el("hsLog");
      if (!box) return;
      box.hidden = false;
      const d = document.createElement("div");
      d.className = "l " + (level || "info");
      d.innerHTML = '<span class="t">' + (ms != null ? ms + "ms" : "") + "</span>"
        + '<span class="m">' + hEsc(text) + "</span>";
      box.appendChild(d);
      box.scrollTop = box.scrollHeight;
    }

    function hsClearLog() {
      const box = el("hsLog");
      if (!box) return;
      box.innerHTML = "";
      box.hidden = true;
    }

    async function openSetup() {
      el("hsName").value = "";
      hsClearLog();
      el("hiveSetup").hidden = false;
      setTimeout(() => el("hsName").focus(), 50);
    }

    el("hiveCreateBtn").addEventListener("click", openSetup);
    el("hsClose").addEventListener("click", () => { el("hiveSetup").hidden = true; });
    el("hiveSetup").addEventListener("click", (e) => {
      if (e.target === el("hiveSetup")) el("hiveSetup").hidden = true;
    });

    el("hsCreate").addEventListener("click", async () => {
      const name = (el("hsName").value || "").trim();
      if (!name) { alert("群名不能为空"); el("hsName").focus(); return; }
      el("hsCreate").disabled = true;
      try {
        // 不再传任何服务器地址 —— 建群只做两件事：生成群密钥、启动 P2P 监听
        const d = await api("/api/hive/create", { name: name });
        if (!d.ok) { alert(d.error || "建群失败"); return; }
        el("hiveSetup").hidden = true;
        await loadGroups();
        openGroup(d.group.gid);
        const r = d.relay || {};
        // 建完给一句人话：接下来怎么让别人进来
        const pw = (d.group && d.group.password) || "";
        setTimeout(() => {
          alert("群建好了 ✅\n\n"
            + (pw ? "· 入群密码：" + pw + "\n"
                 + "  把这个 12 位密码发给对方，他点「🔑 密码入群」输进去就能进\n"
                 : "")
            + "· 同一个 WiFi：对方输密码即可自动找到你\n"
            + "· 不同网络：点群名旁边的「📡 我的地址」，复制那段发给他\n\n"
            + "本机监听端口 " + (r.port || "?")
            + "，局域网地址 " + (r.lan_ip || "?")
            + "\n（消息只在成员机器之间直连，不经过任何服务器）");
        }, 300);
      } catch (e) { alert("建群失败：" + e.message); }
      finally { el("hsCreate").disabled = false; }
    });

    el("hiveJoinBtn").addEventListener("click", () => {
      openModal({
        title: "密码入群",
        hint: "输入群主给你的 12 位密码（字母数字，不含 0、1、I、L、O）。\n"
          + "· 同一个 WiFi：只填密码就够了，会自动找到群\n"
          + "· 不在同一个网络：再填群主的地址 —— 让他点群名旁边的「📡 我的地址」，"
          + "复制那段发给你",
        input: "",
        placeholder: "XXXX-XXXX-XXXX",
        extra: "",
        extraPlaceholder: "对方地址（跨网络才填），如 192.168.1.5:8899",
        okText: "进群",
        onOk: async (v, ex) => {
          const pw = (v || "").trim();
          if (!pw) { alert("密码不能为空"); return false; }
          // 地址兼容三种写法：hive3://IP:端口 / IP:端口 / 只写 IP（用默认端口）
          const raw = (ex || "").trim().replace(/^hive3?:\/\//i, "");
          let host = "", port = 0;
          if (raw) {
            const m = raw.match(/^\[?([^\]]+?)\]?:(\d+)$/);
            if (m) { host = m[1]; port = parseInt(m[2], 10); }
            else { host = raw; }
          }
          let d;
          try {
            d = await api("/api/hive/join",
              { password: pw, host: host, port: port });
          } catch (e) { alert("入群失败：" + e.message); return false; }
          if (!d.ok) {
            const stage = d.stage ? "（" + d.stage + "）" : "";
            alert((d.error || "入群失败") + stage);
            return false;
          }
          await loadGroups();
          openGroup(d.group.gid);
          return true;
        },
      });
    });

    // ---------------------------------------------------------- 更多菜单
    const moreBtn = el("hiveMore");
    const menu = el("hiveMenu");
    moreBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      menu.hidden = !menu.hidden;
      moreBtn.setAttribute("aria-expanded", menu.hidden ? "false" : "true");
    });
    document.addEventListener("click", (e) => {
      if (menu.hidden) return;
      if (menu.contains(e.target) || moreBtn.contains(e.target)) return;
      menu.hidden = true;
      moreBtn.setAttribute("aria-expanded", "false");
    });

    el("hiveNameBtn").addEventListener("click", () => {
      openModal({
        title: "我的群昵称",
        hint: "群里其他人看到的名字。改完对之后的消息生效。",
        input: myName || "",
        placeholder: "昵称",
        onOk: async (v) => {
          const name = (v || "").trim();
          if (!name) { alert("昵称不能为空"); return false; }
          const d = await api("/api/hive/profile", { name: name });
          if (!d.ok) { alert("修改失败"); return false; }
          myName = d.profile.name;
          loadGroups();
          return true;
        },
      });
    });

    // 🧠 群记忆：按当前群聊内容重建，并把生成结果给用户看一眼
    el("hiveMemBtn").addEventListener("click", async () => {
      if (!curGid) { alert("先在左边选一个群"); return; }
      const d = await api("/api/hive/" + encodeURIComponent(curGid) + "/memory/rebuild",
        { method: "POST", body: {} });
      if (!d.ok) { alert("重建失败：" + (d.error || "未知错误")); return; }
      let content = "";
      try {
        const g = await api("/api/hive/" + encodeURIComponent(curGid) + "/memory");
        content = g.content || "";
      } catch (e) { /* 忽略 */ }
      openModal({
        title: "🧠 群记忆已重建",
        hint: "依据最近 " + (d.msgs || 0) + " 条消息，共 " + (d.chars || 0)
          + " 字。@hd 干活时会自动带上这份记忆当背景。下面是生成的内容（只读，"
          + "再点一次「群记忆」会按最新聊天重新生成）：",
        input: content,
        readonly: true,
        okText: "知道了",
      });

    });

    // 🗑 删除群（仅群主）：二次确认，删的是本地这份，其他人会收到通知一起下线
    el("hiveDelBtn").addEventListener("click", async () => {
      if (!curGid) return;
      const g = groups.find((x) => x.gid === curGid) || {};
      const nm = g.name || curGid;
      if (!confirm("删除群「" + nm + "」？\n\n"
        + "· 群里的聊天记录、附件都会一起下线（移到 data/hive/_deleted，能捞回来）\n"
        + "· 其他成员会收到通知，本地这个群也一起消失\n"
        + "· 这个操作只有群主能做")) return;
      if (!confirm("再确认一次：真的删除「" + nm + "」？")) return;
      const d = await api("/api/hive/" + encodeURIComponent(curGid) + "/delete",
        { method: "POST", body: {} });
      if (!d.ok) { alert("删除失败：" + (d.error || "未知错误")); return; }
      curGid = "";
      toast("已删除群「" + nm + "」");
      loadGroups();
    });

    // 换一个新身份（只在身份冲突时出现）
    el("hiveIdRenew").addEventListener("click", async () => {
      const ok = confirm(
        "换一个新身份？\n\n"
        + "· 你会在群里变成一个新成员（旧身份显示为离线）\n"
        + "· 历史消息不受影响\n"
        + "· 只有在「两台机器共用一个身份」时才需要这么做"
      );
      if (!ok) return;
      const d = await api("/api/hive/profile/renew", {});
      if (!d.ok) { alert("换身份失败：" + (d.error || "未知错误")); return; }
      myId = (d.profile || {}).user_id || myId;
      el("hiveIdWarn").hidden = true;
      loadGroups();
      if (curGid) openGroup(curGid);
    });

    // 2026-11-14：公网中继配置。跨网络的群聊要靠一台公网服务器转发，
    // 但转发的是 mesh 层的密文 —— 服务器既看不到内容，也没有群密钥。
    // 密钥只在本机文件里，接口只回显掩码；只有「刚生成」那一刻会显示全文。
    el("hiveSrvBtn").addEventListener("click", async () => {
      let d = {};
      try { d = (await api("/api/hive/relay")) || {}; }
      catch (e) { d = { ok: false, error: e.message }; }
      const cfg = d.config || {};
      const live = d.live || {};
      let now = "还没配置（现在走局域网/直连）";
      if (cfg.url) {
        now = live.online
          ? "🟢 已连接 " + (live.rtt_ms || 0) + "ms"
          : "⚪ 未连接" + (live.error ? "：" + live.error : "（进群时会自动连）");
      }
      openModal({
        title: "🌐 中继服务器",
        hint: "跨网络的群聊需要一台公网服务器转发消息。转发的是密文，"
          + "服务器看不到任何聊天内容。\n当前：" + now + "\n\n"
          + "地址形如 hive4://你的IP:8765。密钥第一次保存会自动生成，"
          + "再把它填到服务器上（用 deploy 脚本则不用管）。",
        input: cfg.url || "",
        placeholder: "hive4://1.2.3.4:8765",
        extra: "",
        extraPlaceholder: cfg.token_set
          ? "密钥已设置（" + cfg.token_hint + "），留空不改"
          : "中继密钥（留空 = 自动生成）",
        okText: "保存",
        onOk: async (url, tok) => {
          const u = (url || "").trim();
          if (!u) { alert("先填服务器地址"); return false; }
          const body = { url: u, token: (tok || "").trim(), enabled: true };
          if (!body.token && !cfg.token_set) body.gen_token = true;
          let r = {};
          try { r = await api("/api/hive/relay", body) || {}; }
          catch (e) { alert("保存失败：" + e.message); return false; }
          if (!r.ok) { alert("保存失败：" + (r.error || "未知原因")); return false; }
          const newTok = (r.config || {}).token;
          if (newTok) {
            setTimeout(() => {
              openModal({
                title: "🔑 中继密钥已生成",
                hint: "把这串填到服务器（用 deploy 脚本会自动读本机这个文件，"
                  + "手动部署时作为 install 脚本的参数）。只显示这一次。",
                input: newTok, readonly: true,
                copyInput: true, copyValue: newTok, copyLabel: "📋 复制密钥",
                noOk: true,
              });
            }, 60);
            return true;
          }
          let t = {};
          try { t = await api("/api/hive/relay/test", { url: u }) || {}; }
          catch (e) { t = { ok: false, error: e.message }; }
          alert(t.ok
            ? "已保存，服务器可达（" + t.ms + "ms）"
            : "已保存，但连不上：" + (t.error || "未知原因"));
          return true;
        },
      });
    });

    // 2026-11-13：这个按钮原来填「云端中继地址」，后来叫「连接码」，
    // 现在改成「我的地址」—— 群主最常做的事就是把自己的地址发给别人。
    // 同一个 WiFi 下压根用不上地址（UDP 广播自动发现），只有跨网络才需要。
    el("hiveRelayBtn").addEventListener("click", async () => {
      if (!curGid) { alert("先在左边选一个群"); return; }
      let d = {};
      try {
        d = (await api("/api/hive/mesh/code?gid=" + encodeURIComponent(curGid))) || {};
      } catch (e) { d = { ok: false, error: e.message }; }
      if (!d.ok) {
        openModal({
          title: "📡 我的地址",
          hint: "拿不到地址：" + (d.error || "未知原因")
            + "\n\n点一下左边那个群的名字进入群聊，网络会自动启动，再回来看这里。",
          noOk: true,
        });
        return;
      }
      const lan = d.lan_addr || (d.lan_ip ? d.lan_ip + ":" + d.port : "");
      const wan = d.wan_addr || (d.public_ip ? d.public_ip + ":" + d.port : "");
      const ts = d.ts_addr || (d.tailscale_ip ? d.tailscale_ip + ":" + d.port : "");
      // 跨网打洞地址（本项目自带，不需要装任何外部软件）
      const punch = d.punch_code || (d.punch_addr ? "punch://" + d.punch_addr : "");
      // 要发出去的那一行：打洞优先（能穿透家用宽带 NAT），其次 Tailscale，再公网，最后本机
      const copyAddr = punch || ts || wan || lan;
      const copyWhat = punch ? "跨网" : (ts ? "Tailscale" : (wan ? "公网" : "本机"));
      const L = [];
      L.push("【同一 WiFi】对方只输密码就行，不用填地址");
      if (lan) L.push("    本机地址   " + lan);
      L.push("");
      if (punch) {
        L.push("【不同网络 · 推荐】把下面这行发给他，填进「对方地址」");
        L.push("    " + punch);
        L.push("    这是打洞地址，能穿透家用宽带的 NAT，不用公网 IP、不用装任何软件");
        L.push("    双方都在家庭宽带后面时，让他也把他的这行发给你，两边各连一次最稳");
      } else {
        L.push("【不同网络】现在没拿到打洞地址（可能断网，或公共 STUN 被挡了）");
        L.push("    稍等几秒再点开这个窗口重试；仍然没有的话，同一 WiFi 用上面的本机地址");
      }
      if (ts) {
        L.push("");
        L.push("（备用）Tailscale 地址 " + ts);
      }
      if (wan) {
        L.push("（备用）公网地址 " + wan + "，需要在路由器上做 " + d.port + " 端口映射");
      }
      if (copyAddr) {
        L.push("");
        L.push("（点「复制" + copyWhat + "地址」只会复制那一行地址本身，"
          + "不带上面这些说明）");
      }
      openModal({
        title: "📡 我的地址",
        hint: "这是「你」这台机器的地址，点下面的「复制地址」就能发给别人。\n"
          + "消息只在成员机器之间直连，不经过任何服务器。",
        input: L.join("\n"),
        readonly: true,
        copyInput: !!copyAddr,
        copyValue: copyAddr || "",
        copyLabel: "📋 复制" + copyWhat + "地址",
        extra: "",
        extraPlaceholder: "（可选）要主动连别人？把他的地址粘到这里，如 1.2.3.4:8899",
        okText: "连接",
        onOk: async (v, ex) => {
          const c = (ex || "").trim();
          // 没填对方地址 = 只是看看自己的地址，直接关掉
          if (!c) return true;
          try {
            const r = await api("/api/hive/mesh/connect", { gid: curGid, code: c });
            if (!r.ok) {
              alert("连不上：" + ((r.result || {}).error || r.error || "未知原因"));
              return false;
            }
          } catch (e) { alert("连接失败：" + e.message); return false; }
          loadStatus();
          return true;
        },
      });
    });

    // ---------------------------------------------------------- WebSocket
    function connectWs() {
      if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      try {
        ws = new WebSocket(proto + "//" + location.host + "/ws/hive");
      } catch (e) { return; }
      ws.onopen = () => { wsRetry = 0; };
      ws.onmessage = (ev) => {
        let d = null;
        try { d = JSON.parse(ev.data); } catch (e) { return; }
        if (d.type === "hello") { paintStatus(d); return; }
        // 群被群主删除：本机那份也下线了，列表要立刻跟着变
        if (d.type === "group_gone") {
          if (d.gid === curGid) { curGid = ""; }
          toast("群「" + (d.name || d.gid) + "」已被群主删除");
          loadGroups();
          return;
        }
        // 成员表变了（有人进群/退群/被踢）→ 立刻刷成员列表。
        // 原来只能靠 4 秒一次的轮询，别人进群后最多要等 4 秒才看到。
        if (d.type === "members" && d.gid === curGid) { loadStatus(); return; }
        // 有人上线/下线 → 立刻刷在线状态（顺带把积压附件补发的结果反映出来）
        if (d.type === "peers") { loadStatus(); return; }
        if (d.type === "hd_stream" && d.gid === curGid && d.frame) {
          hdStream(d.frame);
          return;
        }
        if (d.type === "msg" && d.gid === curGid && d.msg) {
          const s = d.msg.seq || 0;
          // 去重：seq 比当前小的说明已经显示过；-1 是本地待发，允许显示
          if (s > lastSeq || s === -1) {
            if (s > lastSeq) lastSeq = s;
            addMsg(d.msg);
          }
          return;
        }
        // 别人的附件收齐了：那条消息原本可能显示「传输中」，现在能点开了。
        // 直接重渲染那条消息节点（比局部替换附件区简单，也不会错位）。
        if (d.type === "file" && d.gid === curGid && d.file) {
          const fid = d.file.fid || "";
          const mid = d.file.mid || "";
          let hit = null;
          Object.keys(msgIndex).forEach((k) => {
            const m = msgIndex[k];
            if (hit) return;
            if (mid && k === mid) hit = m;
            else if (!mid && (m.files || []).some((f) => f && f.fid === fid)) hit = m;
          });
          if (hit) {
            (hit.files || []).forEach((f) => {
              if (f && f.fid === fid) { f.local = true; f.size = d.file.size || f.size; }
            });
            const node = el("hiveMsgs").querySelector('[data-mid="' + (hit.id || "") + '"]');
            if (node && node.parentNode) node.parentNode.replaceChild(msgNode(hit), node);
          }
        }
      };
      ws.onclose = () => {
        ws = null;
        wsRetry += 1;
        if (mode === "hive") setTimeout(connectWs, Math.min(1000 * wsRetry, 10000));
      };
      ws.onerror = () => { try { ws.close(); } catch (e) { /* 忽略 */ } };
    }

    // ---------------------------------------------------------- 启动
    let saved = "single";
    try { saved = localStorage.getItem("ui_mode") || "single"; } catch (e) { /* 忽略 */ }
    setMode(saved === "hive" ? "hive" : "single");
  })();

})();

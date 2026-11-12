/* ============================================================
   数字人悬浮窗（2026-11-12 新增）
   ------------------------------------------------------------
   做法：一张静态立绘 + 前端实时变形，不依赖任何模型/后端推理。
     · 口型：Web Audio 取 TTS 音频的音量包络 → 驱动上下唇分离
     · 眨眼：随机间隔 2~6 秒，眼睛纵向压缩
     · 呼吸/微摆：CSS transform，不重绘 canvas
     · 表情：眼睛开合 + 眼睛位移 + 眉毛位移 + 色调
   面部区域比例是用网格图实测校准的（眼睛 36.2~40.8%，嘴中心 57.5%），
   换立绘的话这几个常量要重新校准。
   ============================================================ */
(function () {
  "use strict";
  if (window.Avatar) return;

  const IMG_SRC = "/static/assets/avatar.png";
  const CW = 330, CH = 440;               // canvas 内部分辨率（3:4）

  // ---- 面部区域（画面比例）----
  const EYE_TOP = 0.362, EYE_BOT = 0.408;
  const EYE_RECT = [[0.330, 0.494], [0.506, 0.670]];   // 左右眼的 x 区间
  const BROW_TOP = 0.330, BROW_BOT = 0.364;
  const MOUTH_CY = 0.575, MOUTH_SY = 0.020, MOUTH_SX = 0.075;

  // ---- 表情 ----
  const EMOTIONS = {
    neutral:  { name: "自然", icon: "🙂", squint: 0.00, eyeDY: 0, brow: 0,   tint: null },
    happy:    { name: "开心", icon: "😊", squint: 0.30, eyeDY: -1, brow: -1, tint: "rgba(255,186,120,0.10)" },
    thinking: { name: "思考", icon: "🤔", squint: 0.12, eyeDY: -2, brow: 1,  tint: "rgba(150,180,255,0.07)" },
    serious:  { name: "严肃", icon: "😐", squint: -0.08, eyeDY: 0, brow: 2,  tint: "rgba(90,110,150,0.10)" },
  };

  let wrap, cvs, ctx, srcCanvas, srcData, dstData, img = null, ready = false;
  let open = 0, openTarget = 0;
  let blink = 0, nextBlink = 0, blinkT = 0;
  let emotion = "neutral", emoCur = null;
  let audioEl = null, actx = null, analyser = null, tbuf = null;
  let rafId = 0, collapsed = false;

  const LS = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, String(v)); } catch (e) { /* ignore */ } },
  };

  // ---------------- 音频 ----------------
  function ensureGraph(el) {
    if (!el) return null;
    try {
      if (!actx) {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return null;
        actx = new AC();
      }
      if (actx.state === "suspended") actx.resume();
      if (el.__avatarSrc) return el.__avatarSrc;
      const src = actx.createMediaElementSource(el);
      const an = actx.createAnalyser();
      an.fftSize = 512;
      an.smoothingTimeConstant = 0.55;
      src.connect(an);
      an.connect(actx.destination);
      el.__avatarSrc = an;
      return an;
    } catch (e) {
      return null;      // 已经绑过 / 浏览器不支持，都不影响播放
    }
  }

  function bindAudio(el) {
    if (!el) return;
    audioEl = el;
    analyser = ensureGraph(el);
    tbuf = analyser ? new Uint8Array(analyser.fftSize) : null;
    try {
      el.addEventListener("ended", () => { audioEl = null; analyser = null; });
    } catch (e) { /* ignore */ }
  }

  function readLevel() {
    if (!analyser || !tbuf) return 0;
    analyser.getByteTimeDomainData(tbuf);
    let sum = 0;
    for (let i = 0; i < tbuf.length; i++) {
      const v = (tbuf[i] - 128) / 128;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / tbuf.length);
    return Math.min(1, rms * 5.2);      // 说话时典型 rms 0.05~0.18
  }

  // ---------------- 渲染 ----------------
  function buildSrc() {
    srcCanvas = document.createElement("canvas");
    srcCanvas.width = CW; srcCanvas.height = CH;
    const sctx = srcCanvas.getContext("2d", { willReadFrequently: true });
    sctx.drawImage(img, 0, 0, CW, CH);
    srcData = sctx.getImageData(0, 0, CW, CH).data;
    dstData = ctx.createImageData(CW, CH);
    ready = true;
  }

  function drawFrame(now) {
    if (!ready) return;
    const emo = EMOTIONS[emotion] || EMOTIONS.neutral;

    // ---- 眨眼状态机 ----
    if (now > nextBlink) {
      blinkT = now;
      nextBlink = now + 2200 + Math.random() * 4000;
    }
    const bt = now - blinkT;
    if (bt >= 0 && bt < 150) blink = bt / 150;
    else if (bt >= 150 && bt < 260) blink = 1 - (bt - 150) / 110;
    else blink = 0;

    // ---- 口型：attack 快 / release 慢，看起来才像说话 ----
    openTarget = audioEl && !audioEl.paused ? readLevel() : 0;
    open += (openTarget - open) * (openTarget > open ? 0.55 : 0.18);

    const mouthOpen = Math.max(0, Math.min(1, open * 1.25));
    const blinkAmt = Math.max(0, Math.min(1, blink));
    const cyM = MOUTH_CY * CH, syM = MOUTH_SY * CH;
    const et = EYE_TOP * CH, eb = EYE_BOT * CH;
    const eyeH = eb - et;
    const kEye = 1 - blinkAmt * 0.86 - emo.squint * 0.45;   // <1 压缩，>1 睁大
    const eyeDY = emo.eyeDY;
    const browShift = emo.brow;

    const src = srcData, dst = dstData;

    // ① 整行拷贝（嘴部上下唇分离 + 眉毛位移）
    for (let y = 0; y < CH; y++) {
      let s = y;
      const gy = (y - cyM) / syM;
      if (gy > -3.2 && gy < 3.2) {
        s = y - mouthOpen * 0.040 * CH * (gy * Math.exp(-gy * gy));
      }
      if (browShift && y >= BROW_TOP * CH && y <= BROW_BOT * CH) s -= browShift;
      s = Math.round(s);
      if (s < 0) s = 0; else if (s >= CH) s = CH - 1;
      dst.set(src.subarray(s * CW * 4, (s + 1) * CW * 4), y * CW * 4);
    }

    // ② 眼睛区域（逐像素重映射：纵向压缩/睁大 + 轻微上移）
    if (Math.abs(kEye - 1) > 0.004 || eyeDY !== 0) {
      const newH = Math.max(2, Math.round(eyeH * kEye));
      for (let r = 0; r < EYE_RECT.length; r++) {
        const x0 = Math.round(EYE_RECT[r][0] * CW);
        const x1 = Math.round(EYE_RECT[r][1] * CW);
        for (let y = 0; y < newH; y++) {
          const dy = et + eyeDY + y;
          if (dy < 0 || dy >= CH) continue;
          const s = Math.round(et + y / kEye);
          if (s < 0 || s >= CH) continue;
          const so = s * CW * 4, dof = dy * CW * 4;
          for (let x = x0; x < x1; x++) {
            const si = so + x * 4, di = dof + x * 4;
            dst[di] = src[si]; dst[di + 1] = src[si + 1]; dst[di + 2] = src[si + 2]; dst[di + 3] = 255;
          }
        }
        // 压缩后空出来的地方，用下眼睑以下的皮肤补
        for (let y = et + eyeDY + newH; y < eb; y++) {
          const dy = Math.round(y);
          if (dy < 0 || dy >= CH) continue;
          const s = Math.min(CH - 1, Math.round(eb + (y - (et + newH)) * 0.5));
          const so = s * CW * 4, dof = dy * CW * 4;
          for (let x = x0; x < x1; x++) {
            const si = so + x * 4, di = dof + x * 4;
            dst[di] = src[si]; dst[di + 1] = src[si + 1]; dst[di + 2] = src[si + 2]; dst[di + 3] = 255;
          }
        }
      }
    }

    ctx.putImageData(dstData, 0, 0);

    // ③ 口腔：张开到一定程度才画，柔边
    if (mouthOpen > 0.18) {
      const hh = mouthOpen * 0.013 * CH;
      const ww = 0.046 * CW;
      ctx.save();
      try { ctx.filter = "blur(" + (hh * 0.42).toFixed(1) + "px)"; } catch (e) { /* ignore */ }
      ctx.fillStyle = "rgba(56,28,34,0.80)";
      ctx.beginPath();
      ctx.ellipse(CW * 0.5, cyM + CH * 0.003 + hh * 0.35, ww, hh, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    }

    // ④ 表情色调
    if (emo.tint) {
      ctx.save();
      ctx.globalCompositeOperation = "soft-light";
      ctx.fillStyle = emo.tint;
      ctx.fillRect(0, 0, CW, CH);
      ctx.restore();
    }

    rafId = requestAnimationFrame(drawFrame);
  }

  // ---------------- UI ----------------
  function buildUI() {
    wrap = document.createElement("div");
    wrap.id = "avatarFloat";
    wrap.innerHTML =
      '<div class="av-head">' +
        '<span class="av-dot"></span><span class="av-title">数字人</span>' +
        '<span class="av-sp"></span>' +
        '<button type="button" class="av-icon" data-act="emotion" title="切换表情">🙂</button>' +
        '<button type="button" class="av-icon" data-act="autoread" title="回答后自动朗读">🔈</button>' +
        '<button type="button" class="av-icon" data-act="collapse" title="收起 / 展开">—</button>' +
      '</div>' +
      '<div class="av-stage"><canvas id="avCanvas" width="' + CW + '" height="' + CH + '"></canvas>' +
        '<div class="av-tip" id="avTip">待机中</div></div>' +
      '<div class="av-foot">' +
        '<input id="avSay" type="text" placeholder="跟它说句话…" autocomplete="off">' +
        '<button type="button" id="avSend" title="发送">➤</button>' +
      '</div>';
    document.body.appendChild(wrap);

    cvs = wrap.querySelector("#avCanvas");
    ctx = cvs.getContext("2d", { willReadFrequently: true });

    // 拖动（只有按住标题栏才拖）
    const head = wrap.querySelector(".av-head");
    let drag = null;
    head.addEventListener("mousedown", (e) => {
      if (e.target.closest("button")) return;
      const r = wrap.getBoundingClientRect();
      drag = { dx: e.clientX - r.left, dy: e.clientY - r.top };
      e.preventDefault();
    });
    window.addEventListener("mousemove", (e) => {
      if (!drag) return;
      let x = e.clientX - drag.dx, y = e.clientY - drag.dy;
      x = Math.max(4, Math.min(window.innerWidth - wrap.offsetWidth - 4, x));
      y = Math.max(4, Math.min(window.innerHeight - 40, y));
      wrap.style.left = x + "px"; wrap.style.top = y + "px";
      wrap.style.right = "auto"; wrap.style.bottom = "auto";
    });
    window.addEventListener("mouseup", () => { drag = null; });

    // 按钮
    wrap.querySelector('[data-act="emotion"]').addEventListener("click", () => cycleEmotion());
    wrap.querySelector('[data-act="autoread"]').addEventListener("click", () => toggleAutoRead());
    wrap.querySelector('[data-act="collapse"]').addEventListener("click", () => setCollapsed(!collapsed));

    // 对话输入
    const inp = wrap.querySelector("#avSay");
    const send = () => {
      const t = (inp.value || "").trim();
      if (!t) return;
      inp.value = "";
      const main = document.querySelector("#input");
      if (!main) { setTip("找不到主输入框"); return; }
      main.value = t;
      main.dispatchEvent(new Event("input", { bubbles: true }));
      const form = main.closest("form");
      if (form && typeof form.requestSubmit === "function") form.requestSubmit();
      else main.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    };
    wrap.querySelector("#avSend").addEventListener("click", send);
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); send(); } });

    paintAutoRead();
  }

  function setTip(t) {
    const el = wrap && wrap.querySelector("#avTip");
    if (el) el.textContent = t;
  }

  function setCollapsed(v) {
    collapsed = !!v;
    wrap.classList.toggle("av-collapsed", collapsed);
    LS.set("avatar_collapsed", collapsed ? "1" : "0");
    const btn = wrap.querySelector('[data-act="collapse"]');
    if (btn) btn.textContent = collapsed ? "+" : "—";
  }

  function applyEmotion(name) {
    emotion = EMOTIONS[name] ? name : "neutral";
    LS.set("avatar_emotion", emotion);
    const btn = wrap.querySelector('[data-act="emotion"]');
    if (btn) btn.textContent = EMOTIONS[emotion].icon;
  }

  function cycleEmotion() {
    const keys = Object.keys(EMOTIONS);
    const i = keys.indexOf(emotion);
    applyEmotion(keys[(i + 1) % keys.length]);
  }

  function autoReadOn() { return LS.get("avatar_autoread", "0") === "1"; }

  function paintAutoRead() {
    const btn = wrap && wrap.querySelector('[data-act="autoread"]');
    if (!btn) return;
    btn.classList.toggle("on", autoReadOn());
    btn.title = autoReadOn() ? "回答后自动朗读：开" : "回答后自动朗读：关";
  }

  function toggleAutoRead() {
    LS.set("avatar_autoread", autoReadOn() ? "0" : "1");
    paintAutoRead();
  }

  // ---------------- 对外接口 ----------------
  async function speak(text) {
    const raw = String(text == null ? "" : text).trim();
    if (raw.length < 2) return;
    try {
      const r = await fetch("/api/tts/synth", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text: raw,
          voice: LS.get("tts_voice", "zh-CN-XiaoxiaoNeural"),
          rate: parseInt(LS.get("tts_rate", "0"), 10) || 0,
          pitch: parseInt(LS.get("tts_pitch", "0"), 10) || 0,
          style: LS.get("tts_style", "neutral"),
        }),
      });
      const d = await r.json();
      if (!r.ok || !d.url) throw new Error(d.detail || d.error || "合成失败");
      const a = new Audio(d.url);
      bindAudio(a);
      setTip("说话中…");
      a.addEventListener("ended", () => setTip("待机中"));
      await a.play();
    } catch (e) {
      setTip("朗读失败");
      console.warn("[avatar] speak failed:", e);
    }
  }

  function onAnswer(text) {
    const t = String(text || "");
    // 跟内容切表情（很轻的判断，手动点过就手动优先）
    if (LS.get("avatar_auto_emotion", "1") === "1") {
      if (/(抱歉|失败|错误|无法|不能|出错)/.test(t)) applyEmotion("serious");
      else if (/(分析|思考|让我看|查一下|考虑)/.test(t)) applyEmotion("thinking");
      else if (/(完成|成功|好的|已经|搞定了)/.test(t)) applyEmotion("happy");
      else applyEmotion("neutral");
    }
    if (autoReadOn()) speak(t);
  }

  // ---------------- 启动 ----------------
  function init() {
    buildUI();
    applyEmotion(LS.get("avatar_emotion", "neutral"));
    setCollapsed(LS.get("avatar_collapsed", "0") === "1");

    img = new Image();
    img.onload = () => { buildSrc(); rafId = requestAnimationFrame(drawFrame); };
    img.onerror = () => setTip("形象图加载失败");
    img.src = IMG_SRC;
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();

  window.Avatar = {
    speak,
    onAnswer,
    bindAudio,
    setEmotion: applyEmotion,
    cycleEmotion,
    toggleAutoRead,
    isAutoRead: autoReadOn,
    show(v) { if (wrap) wrap.style.display = v === false ? "none" : ""; },
  };
})();

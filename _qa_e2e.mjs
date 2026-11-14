// 真实端到端：建临时群 → 真发 @hd（会真的走一次群 AI 任务）→ 观察实时卡结构 → 删群
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const BASE = "http://127.0.0.1:8000";
const PORT = 9335;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const post = async (p, body) => {
  const r = await fetch(BASE + p, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return r.json();
};

// 1) 建临时群
const gname = "QA浮层验证_" + Date.now();
const made = await post("/api/hive/create", { name: gname });
console.log("建群:", JSON.stringify(made).slice(0, 200));
if (!made.ok) { console.log("FAIL: 建群失败"); process.exit(1); }
const gid = made.group ? made.group.gid : (made.gid || "");
if (!gid) { console.log("FAIL: 拿不到 gid", JSON.stringify(made).slice(0, 400)); process.exit(1); }
console.log("gid:", gid, "name:", gname);

const udd = fs.mkdtempSync(path.join(os.tmpdir(), "edge-qa3-"));
const proc = spawn(EDGE, [
  "--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${udd}`,
  "--window-size=1440,1000", "--no-first-run", "--no-default-browser-check",
  "--disable-gpu", "--hide-scrollbars", BASE + "/",
], { stdio: "ignore" });

let list = null;
for (let i = 0; i < 60; i++) {
  await sleep(500);
  try { list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); } catch (e) { continue; }
  if (list.some((t) => t.type === "page" && t.webSocketDebuggerUrl)) break;
}
const page = (list || []).find((t) => t.type === "page");
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let id = 0; const pend = new Map();
ws.onmessage = (e) => { const m = JSON.parse(e.data); if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); } };
const send = (method, params = {}) => new Promise((res) => { const mid = ++id; pend.set(mid, res); ws.send(JSON.stringify({ id: mid, method, params })); });
const ev = async (expr) => {
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.result && r.result.exceptionDetails) return { __err: r.result.exceptionDetails.text };
  return r.result && r.result.result ? r.result.result.value : null;
};
await send("Page.enable"); await send("Runtime.enable");
await sleep(4000);

// 2) 切群聊 → 点进新群
await ev(`(() => { const t = Array.from(document.querySelectorAll('button')).find(n=>(n.textContent||'').trim()==='👥 群聊'); if (t) t.click(); })()`);
await sleep(1500);
const clicked = await ev(`(() => {
  const it = document.querySelector('.hive-item[data-gid="${gid}"]');
  if (!it) return 'not-found';
  it.click(); return 'ok';
})()`);
console.log("点进新群:", clicked);
await sleep(2500);

// 3) 真发 @hd
const sent = await post(`/api/hive/${encodeURIComponent(gid)}/send`, { text: "@hd 用一句话说明你在做什么" });
console.log("发 @hd:", JSON.stringify(sent).slice(0, 200));

// 4) 轮询观察
let last = "";
for (let i = 0; i < 40; i++) {
  await sleep(600);
  const snap = await ev(`(() => {
    const dock = document.querySelector('body > .think-dock');
    const box = document.getElementById('hiveMsgs');
    const live = Array.from(box.querySelectorAll('.hd-live'));
    const thinkInStream = Array.from(box.querySelectorAll('.hd-think-card'));
    return JSON.stringify({
      dock: dock ? {
        cls: dock.className,
        chips: dock.querySelectorAll('.tool-chip').length,
        hasOut: !!dock.querySelector('.hd-out'),
        hasSd: !!dock.querySelector('.hd-sd'),
        lbl: (dock.querySelector('.hd-think-lbl')||{}).textContent || '',
        len: (dock.innerText||'').length,
      } : null,
      liveCount: live.length,
      liveDetail: live.map(n => ({
        chips: n.querySelectorAll('.tool-chip').length,
        outLen: (n.querySelector('.hd-out')||{innerText:''}).innerText.length,
        sdVisible: !!(n.querySelector('.hd-sd') && !n.querySelector('.hd-sd').hidden),
      })),
      thinkInStream: thinkInStream.length,
      boxChildren: box.children.length,
    });
  })()`);
  if (snap !== last) { console.log("[" + i + "]", snap); last = snap; }
  if (snap && snap.indexOf('"liveCount":1') >= 0) break;
}

// 5) 截图
const shot = await send("Page.captureScreenshot", { format: "png" });
if (shot.result && shot.result.data) {
  fs.writeFileSync(path.join(process.cwd(), "_qa_dock3.png"), Buffer.from(shot.result.data, "base64"));
  console.log("截图: _qa_dock3.png");
}

ws.close(); await sleep(200); proc.kill(); await sleep(400);
try { fs.rmSync(udd, { recursive: true, force: true }); } catch (e) { /* 忽略 */ }

// 6) 删临时群
const del = await post(`/api/hive/${encodeURIComponent(gid)}/delete`, {});
console.log("删群:", JSON.stringify(del).slice(0, 200));
console.log("DONE");

// 诊断：打开应用 → 切群聊 → 进群 → dump 完整 DOM 结构
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const APP = "http://127.0.0.1:8000/";
const PORT = 9334;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const udd = fs.mkdtempSync(path.join(os.tmpdir(), "edge-qa2-"));

const proc = spawn(EDGE, [
  "--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${udd}`,
  "--window-size=1440,1000", "--no-first-run", "--no-default-browser-check",
  "--disable-gpu", "--hide-scrollbars", APP,
], { stdio: "ignore" });

let list = null;
for (let i = 0; i < 60; i++) {
  await sleep(500);
  try {
    const r = await fetch(`http://127.0.0.1:${PORT}/json/list`);
    list = await r.json();
    if (list.some((t) => t.type === "page" && t.webSocketDebuggerUrl)) break;
  } catch (e) { /* 等 */ }
}
const page = (list || []).find((t) => t.type === "page");
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let id = 0; const pend = new Map();
ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); } };
const send = (method, params = {}) => new Promise((res) => { const mid = ++id; pend.set(mid, res); ws.send(JSON.stringify({ id: mid, method, params })); });
const ev = async (expr) => {
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.result && r.result.exceptionDetails) return { __err: r.result.exceptionDetails.text };
  return r.result && r.result.result ? r.result.result.value : null;
};

await send("Page.enable"); await send("Runtime.enable");
await sleep(4000);

await ev(`(() => { const t = Array.from(document.querySelectorAll('button')).find(n=>(n.textContent||'').trim()==='👥 群聊'); if (t) t.click(); })()`);
await sleep(1500);
await ev(`document.querySelector('.hive-item').click()`);
await sleep(4000);

const dump = await ev(`(() => {
  const b = document.getElementById('hiveMsgs');
  const kids = Array.from(b.children);
  return JSON.stringify({
    hiveMsgsChildren: kids.length,
    last5: kids.slice(-5).map(n => ({ cls: n.className, len: (n.innerText||'').length, hasThink: !!n.querySelector('.think-hdr'), hasOut: !!n.querySelector('.hd-out') })),
    bodyDirect: Array.from(document.body.children).map(n => n.className).filter(Boolean),
    dock: !!document.querySelector('body > .think-dock'),
    anyHdLive: document.querySelectorAll('.hd-live').length,
    anyThinkCard: document.querySelectorAll('.hd-think-card').length,
  });
})()`);
console.log("结构:", dump);

const shot = await send("Page.captureScreenshot", { format: "png" });
if (shot.result && shot.result.data) {
  fs.writeFileSync(path.join(process.cwd(), "_qa_dock2.png"), Buffer.from(shot.result.data, "base64"));
  console.log("截图: _qa_dock2.png");
}
ws.close(); await sleep(200); proc.kill(); await sleep(400);
try { fs.rmSync(udd, { recursive: true, force: true }); } catch (e) { /* 忽略 */ }
console.log("DONE");

// 真实浏览器验证：跟随视角浮层里是否只剩思考框
// 做法：起 headless Edge → 打开应用 → 点进第一个群 → 走真实 /ws/hive 发 hd_stream 帧
//      → 检查 body > .think-dock 里到底装了哪些东西 → 截图
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const APP = "http://127.0.0.1:8000/";
const PORT = 9333;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const udd = fs.mkdtempSync(path.join(os.tmpdir(), "edge-qa-"));
const proc = spawn(EDGE, [
  "--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${udd}`,
  "--window-size=1440,1000", "--no-first-run", "--no-default-browser-check",
  "--disable-gpu", "--hide-scrollbars", APP,
], { stdio: "ignore", detached: false });

let list = null;
for (let i = 0; i < 60; i++) {
  await sleep(500);
  try {
    const r = await fetch(`http://127.0.0.1:${PORT}/json/list`);
    list = await r.json();
    if (list.some((t) => t.type === "page" && t.webSocketDebuggerUrl)) break;
  } catch (e) { /* 还没起来 */ }
}
const page = (list || []).find((t) => t.type === "page");
if (!page) { console.log("FAIL: 浏览器没起来"); proc.kill(); process.exit(1); }

const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });

let id = 0;
const pend = new Map();
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); }
};
const send = (method, params = {}) => new Promise((res) => {
  const mid = ++id;
  pend.set(mid, res);
  ws.send(JSON.stringify({ id: mid, method, params }));
});
const evaluate = async (expr) => {
  const r = await send("Runtime.evaluate", {
    expression: expr, returnByValue: true, awaitPromise: true,
  });
  if (r.result && r.result.exceptionDetails) {
    return { __err: r.result.exceptionDetails.text || "exception" };
  }
  return r.result && r.result.result ? r.result.result.value : null;
};

await send("Page.enable");
await send("Runtime.enable");
await sleep(3500);

// 1) 找到群（轮询等前端把列表渲染出来）
let groups = null, gids = [];
for (let i = 0; i < 40; i++) {
  groups = await evaluate(`JSON.stringify(Array.from(document.querySelectorAll('.hive-item')).map(n=>n.getAttribute('data-gid')))`);
  try { gids = JSON.parse(groups || "[]"); } catch (e) { gids = []; }
  if (gids.length) break;
  await sleep(500);
}
console.log("群列表:", groups, "| 等了几轮:", 40);
if (!gids.length) {
  const diag = await evaluate(`JSON.stringify({
    title: document.title,
    ready: document.readyState,
    bodyLen: (document.body.innerText||'').length,
    head: (document.body.innerText||'').slice(0,200),
    boxes: Array.from(document.querySelectorAll('[id]')).map(n=>n.id).filter(x=>/hive|group/i.test(x)),
  })`);
  console.log("诊断:", diag);
  console.log("FAIL: 没有群，无法验证");
  ws.close(); proc.kill(); process.exit(2);
}

// 2) 点进第一个群
await evaluate(`document.querySelector('.hive-item').click()`);
await sleep(2000);
const cur = await evaluate(`(document.querySelector('.hive-item.on')||{}).getAttribute ? document.querySelector('.hive-item.on').getAttribute('data-gid') : ''`);
console.log("当前群:", cur);
const gid = cur || gids[0];

// 3) 走真实 WS 发帧
const appWs = new WebSocket("ws://127.0.0.1:8000/ws/hive");
await new Promise((res, rej) => { appWs.onopen = res; appWs.onerror = rej; });
const mid = "qa-" + Date.now();
const frame = (o) => JSON.stringify({ type: "hd_stream", gid, frame: Object.assign({ mid }, o) });

const think = "我先看一下工作区里现有的项目，避免重复建设。工作区已切到本项目（当前为空），我从零搭一个签到系统。";
const ans = "我先把工作区情况看一下，再动手做。工作区里已经有一个空的「考勤签到」项目，我切进去从零搭。";
const seq = [
  { phase: "queued", task: { asker: "QA", ahead: 0 } },
  { phase: "start", task: { asker: "QA", ahead: 0 } },
  { phase: "thinking", task: { chars: think.length }, delta: think, full: think },
  { phase: "tool", tool: "workspace_projects" },
  { phase: "tool", tool: "workspace_use_project" },
  { phase: "tool", tool: "workspace_write" },
  { phase: "code", code: { rel: "backend/main.js", act: "patch", added: 12, removed: 3,
      diff: "@@ -10,3 +10,4 @@\n- old line\n+ new line\n+ another" } },
  { phase: "content", delta: ans, full: ans },
];
for (const s of seq) { appWs.send(frame(s)); await sleep(180); }
await sleep(1200);

// 4) 检查浮层内容
const report = await evaluate(`(() => {
  const dock = document.querySelector('body > .think-dock');
  const box = document.getElementById('hiveMsgs');
  const q = (n, s) => { const e = n && n.querySelector(s); return e ? (e.className || e.tagName) : null; };
  const inDock = dock ? {
    cls: dock.className,
    hasThinkHdr: !!dock.querySelector('.think-hdr'),
    hasThinkBody: !!dock.querySelector('.think-body'),
    toolChips: dock.querySelectorAll('.tool-chip').length,
    hasOut: !!dock.querySelector('.hd-out'),
    hasSd: !!dock.querySelector('.hd-sd'),
    hasWho: !!dock.querySelector('.hmsg-who'),
    textLen: (dock.innerText || '').length,
    text: (dock.innerText || '').slice(0, 120),
  } : null;
  return JSON.stringify({
    dockInBody: !!dock,
    inDock,
    // 消息流里剩下什么
    streamCards: Array.from(document.querySelectorAll('#hiveMsgs .hd-live')).map(n => ({
      toolChips: n.querySelectorAll('.tool-chip').length,
      hasOut: !!n.querySelector('.hd-out'),
      hasSd: !!n.querySelector('.hd-sd'),
      outLen: (n.querySelector('.hd-out')||{innerText:''}).innerText.length,
    })),
    thinkCardsInStream: document.querySelectorAll('#hiveMsgs .hd-think-card').length,
    // 全局工具条数量（防止思考节点被误清空）
    allToolChips: document.querySelectorAll('#hiveMsgs .tool-chip').length,
  });
})()`);
console.log("DOM 报告:", report);

// 5) 截图
const shot = await send("Page.captureScreenshot", { format: "png" });
if (shot.result && shot.result.data) {
  fs.writeFileSync(path.join(process.cwd(), "_qa_dock.png"), Buffer.from(shot.result.data, "base64"));
  console.log("截图已存 _qa_dock.png");
}

appWs.close();
ws.close();
await sleep(300);
proc.kill();
await sleep(500);
try { fs.rmSync(udd, { recursive: true, force: true }); } catch (e) { /* 忽略 */ }
console.log("DONE");

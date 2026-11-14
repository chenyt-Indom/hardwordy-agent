import asyncio, json, os, subprocess, sys, time, tempfile, shutil
import requests
import websockets

ROOT = r"D:\local-multimodal-src"
PORT = 8011
CDP = 9333
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

proc = None
ep = None
ud = None

async def call(ws, mid, method, params=None):
    await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        m = json.loads(await ws.recv())
        if m.get("id") == mid:
            return m

async def ev(ws, mid, expr):
    r = await call(ws, mid, "Runtime.evaluate",
                   {"expression": expr, "returnByValue": True, "awaitPromise": True})
    res = r.get("result", {}).get("result", {})
    return res.get("value", res)

async def main():
    global proc, ep, ud
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.main:app",
                             "--host", "127.0.0.1", "--port", str(PORT)],
                            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok = False
    for _ in range(40):
        try:
            r = requests.get("http://127.0.0.1:%d/api/avatar2/info" % PORT, timeout=1)
            if r.status_code == 200:
                ok = True
                break
        except Exception:
            pass
        time.sleep(0.5)
    print("[svc] ready:", ok)
    if not ok:
        return

    ud = tempfile.mkdtemp(prefix="edgeqa_")
    ep = subprocess.Popen([EDGE, "--headless=new", "--remote-debugging-port=%d" % CDP,
                           "--user-data-dir=" + ud, "--disable-gpu", "--no-first-run",
                           "--no-default-browser-check", "--window-size=1400,1000",
                           "http://127.0.0.1:%d/" % PORT],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tgt = None
    for _ in range(40):
        try:
            js = requests.get("http://127.0.0.1:%d/json" % CDP, timeout=1).json()
            for t in js:
                if t.get("type") == "page" and ("127.0.0.1:%d" % PORT) in (t.get("url") or ""):
                    tgt = t
                    break
            if tgt:
                break
        except Exception:
            pass
        time.sleep(0.5)
    print("[cdp] target:", bool(tgt), (tgt or {}).get("url"))
    if not tgt:
        return

    async with websockets.connect(tgt["webSocketDebuggerUrl"], max_size=8 << 20) as ws:
        await call(ws, 1, "Runtime.enable")
        await asyncio.sleep(3.5)

        r1 = await ev(ws, 2, """(() => {
          const pnl = document.getElementById('av2Panel');
          if (pnl) { pnl.classList.remove('collapsed'); pnl.style.maxHeight='none'; pnl.style.height='auto'; }
          const stage = document.getElementById('av2Stage');
          const img = document.getElementById('av2Img');
          const mbox = document.getElementById('av2MouthBox');
          const res = document.getElementById('av2Res');
          return JSON.stringify({
            stageW: stage ? stage.clientWidth : -1,
            stageH: stage ? stage.clientHeight : -1,
            imgNat: img ? [img.naturalWidth, img.naturalHeight] : null,
            imgHidden: img ? img.hidden : null,
            mboxHidden: mbox ? mbox.hidden : null,
            mbox: mbox ? {l: mbox.style.left, t: mbox.style.top, w: mbox.style.width, h: mbox.style.height} : null,
            resOpts: res ? Array.from(res.options).map(o => o.value) : null,
            resVal: res ? res.value : null,
            calHidden: document.getElementById('av2Cal') ? document.getElementById('av2Cal').hidden : null
          });
        })()""")
        print("[R1 初始状态]", r1)

        r2 = await ev(ws, 3, """(() => {
          const my = document.getElementById('av2MY');
          const mbox = document.getElementById('av2MouthBox');
          const t0 = mbox.style.top, l0 = mbox.style.left;
          my.value = '0.85';
          my.dispatchEvent(new Event('input', {bubbles: true}));
          const t1 = mbox.style.top, l1 = mbox.style.left;
          return JSON.stringify({before: t0, after: t1, moved: t0 !== t1, leftSame: l0 === l1});
        })()""")
        print("[R2 拖嘴位]", r2)

        r3 = await ev(ws, 4, """(() => {
          const mw = document.getElementById('av2MW');
          const mbox = document.getElementById('av2MouthBox');
          const w0 = mbox.style.width;
          mw.value = '0.42';
          mw.dispatchEvent(new Event('input', {bubbles: true}));
          return JSON.stringify({w0: w0, w1: mbox.style.width, wider: parseFloat(mbox.style.width) > parseFloat(w0)});
        })()""")
        print("[R3 拖嘴宽]", r3)

        r4 = await ev(ws, 5, """(() => {
          const s = document.getElementById('av2Res');
          s.value = '1536';
          s.dispatchEvent(new Event('change', {bubbles: true}));
          return JSON.stringify({value: s.value, saved: localStorage.getItem('avatar2_res')});
        })()""")
        print("[R4 分辨率持久化]", r4)

        # 复位到 1024，别把测试值留给用户
        await ev(ws, 6, """(() => {
          const s = document.getElementById('av2Res');
          s.value = '1024'; s.dispatchEvent(new Event('change', {bubbles: true}));
          return 'ok';
        })()""")

        r5 = await ev(ws, 7, """(() => {
          const stage = document.getElementById('av2Stage');
          const img = document.getElementById('av2Img');
          const mbox = document.getElementById('av2MouthBox');
          if (!stage || !img.naturalWidth) return JSON.stringify({skip: true});
          const k = Math.min(stage.clientWidth / img.naturalWidth, stage.clientHeight / img.naturalHeight);
          const dw = img.naturalWidth * k, dh = img.naturalHeight * k;
          const ox = (stage.clientWidth - dw) / 2, oy = (stage.clientHeight - dh) / 2;
          const l = parseFloat(mbox.style.left), t = parseFloat(mbox.style.top);
          const w = parseFloat(mbox.style.width), h = parseFloat(mbox.style.height);
          return JSON.stringify({
            inside: (l >= ox - 1) && (t >= oy - 1) && (l + w <= ox + dw + 1) && (t + h <= oy + dh + 1),
            box: [Math.round(l), Math.round(t), Math.round(w), Math.round(h)],
            imgArea: [Math.round(ox), Math.round(oy), Math.round(dw), Math.round(dh)]
          });
        })()""")
        print("[R5 框在图片范围内]", r5)

try:
    asyncio.run(main())
finally:
    for p in (ep, proc):
        try:
            if p:
                p.terminate()
        except Exception:
            pass
    time.sleep(1.5)
    for p in (ep, proc):
        try:
            if p:
                p.kill()
        except Exception:
            pass
    if ud and os.path.isdir(ud):
        try:
            shutil.rmtree(ud, ignore_errors=True)
        except Exception:
            pass
    print("[cleanup] done")

import asyncio, json, os, subprocess, sys, time, tempfile, shutil
import requests
import websockets

ROOT = r"D:\local-multimodal-src"
PORT = 8012
CDP = 9334
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
            if requests.get("http://127.0.0.1:%d/api/avatar2/info" % PORT, timeout=1).status_code == 200:
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
            for t in requests.get("http://127.0.0.1:%d/json" % CDP, timeout=1).json():
                if t.get("type") == "page" and ("127.0.0.1:%d" % PORT) in (t.get("url") or ""):
                    tgt = t
                    break
            if tgt:
                break
        except Exception:
            pass
        time.sleep(0.5)
    print("[cdp] target:", bool(tgt))
    if not tgt:
        return

    async with websockets.connect(tgt["webSocketDebuggerUrl"], max_size=8 << 20) as ws:
        await call(ws, 1, "Runtime.enable")
        await asyncio.sleep(3.5)

        r1 = await ev(ws, 2, """(() => {
          const pnl = document.getElementById('av2Panel');
          if (pnl) { pnl.classList.remove('collapsed'); pnl.style.maxHeight='none'; pnl.style.height='auto'; }
          const mbox = document.getElementById('av2MouthBox');
          const tip = document.getElementById('av2CalTip');
          const mode = document.getElementById('av2Mode');
          return JSON.stringify({
            mode: mode.value,
            mboxHidden: mbox.hidden,
            tip: (tip.textContent || '').slice(0, 40),
            imgNat: [document.getElementById('av2Img').naturalWidth,
                     document.getElementById('av2Img').naturalHeight]
          });
        })()""")
        print("[A w2l 默认]", r1)

        r2 = await ev(ws, 3, """(() => {
          const mode = document.getElementById('av2Mode');
          mode.value = 'p25d';
          mode.dispatchEvent(new Event('change', {bubbles: true}));
          const mbox = document.getElementById('av2MouthBox');
          const tip = document.getElementById('av2CalTip');
          return JSON.stringify({
            mode: mode.value, mboxHidden: mbox.hidden,
            box: [mbox.style.left, mbox.style.top, mbox.style.width, mbox.style.height],
            tip: (tip.textContent || '').slice(0, 30)
          });
        })()""")
        print("[B 切伪3D]", r2)

        r3 = await ev(ws, 4, """(() => {
          const my = document.getElementById('av2MY');
          const mbox = document.getElementById('av2MouthBox');
          const t0 = mbox.style.top;
          my.value = '0.30';
          my.dispatchEvent(new Event('input', {bubbles: true}));
          return JSON.stringify({before: t0, after: mbox.style.top, moved: t0 !== mbox.style.top});
        })()""")
        print("[C 拖嘴位上移]", r3)

        r4 = await ev(ws, 5, """(() => {
          const mw = document.getElementById('av2MW');
          const mbox = document.getElementById('av2MouthBox');
          const w0 = mbox.style.width;
          mw.value = '0.12';
          mw.dispatchEvent(new Event('input', {bubbles: true}));
          return JSON.stringify({w0: w0, w1: mbox.style.width,
            narrower: parseFloat(mbox.style.width) < parseFloat(w0)});
        })()""")
        print("[D 拖嘴宽变窄]", r4)

        r5 = await ev(ws, 6, """(() => {
          const mode = document.getElementById('av2Mode');
          mode.value = 'w2l';
          mode.dispatchEvent(new Event('change', {bubbles: true}));
          return JSON.stringify({mode: mode.value,
            mboxHidden: document.getElementById('av2MouthBox').hidden,
            tip: (document.getElementById('av2CalTip').textContent || '').slice(0, 20)});
        })()""")
        print("[E 切回 w2l 应隐藏]", r5)

        # 恢复用户原来的设置（嘴位/嘴宽/模式都别留测试值）
        r6 = await ev(ws, 7, """(async () => {
          const info = await fetch('/api/avatar2/info').then(r => r.json());
          const my = document.getElementById('av2MY'), mw = document.getElementById('av2MW');
          my.value = info.mouth.y; mw.value = info.mouth.w;
          await fetch('/api/avatar2/mouth', {method:'POST',
            headers:{'Content-Type':'application/json'},
            body: JSON.stringify({y: info.mouth.y, w: info.mouth.w, x: info.mouth.x, h: info.mouth.h})});
          const mode = document.getElementById('av2Mode');
          mode.value = 'w2l'; mode.dispatchEvent(new Event('change', {bubbles: true}));
          return JSON.stringify({restored: [info.mouth.y, info.mouth.w]});
        })()""")
        print("[F 复位]", r6)

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
        shutil.rmtree(ud, ignore_errors=True)
    print("[cleanup] done")

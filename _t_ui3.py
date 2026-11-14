"""展开数字人面板，截图确认动作下拉的视觉对齐"""
import os, sys, json, time, subprocess, urllib.request, base64
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asyncio
import websockets

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if not os.path.exists(EDGE):
    EDGE = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
PORT = 9335
BASE = "http://127.0.0.1:8000/"
TMP = os.path.join(os.environ["TEMP"], "edge_av2panel2")
proc = subprocess.Popen([EDGE, "--headless=new", "--disable-gpu",
                         "--remote-debugging-port=%d" % PORT,
                         "--window-size=1500,1100", "--no-first-run",
                         "--user-data-dir=" + TMP, BASE],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def wait_devtools(timeout=25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/json" % PORT, timeout=1) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            time.sleep(0.4)
    return None

def safe(s):
    if not isinstance(s, str): return s
    return s.encode("utf-8", "replace").decode("utf-8", "replace")

tabs = wait_devtools()
page = [t for t in (tabs or []) if t.get("type") == "page" and "8000" in (t.get("url") or "")]
page = page[0] if page else [t for t in tabs if t.get("type") == "page"][0]
print("页:", page.get("url"))

async def main():
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=30*1024*1024) as ws:
        mid = [0]
        async def call(method, params=None):
            mid[0] += 1
            await ws.send(json.dumps({"id": mid[0], "method": method, "params": params or {}}))
            while True:
                m = json.loads(await ws.recv())
                if m.get("id") == mid[0]:
                    return m.get("result") or {}
        async def js(expr):
            r = await call("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                                "awaitPromise": True})
            if r.get("exceptionDetails"):
                return "EXC: " + safe(str(r["exceptionDetails"]))[:200]
            return r.get("result", {}).get("value")

        await call("Page.enable"); await call("Runtime.enable")
        for _ in range(40):
            if await js("!!document.querySelector('#av2Panel')"): break
            await asyncio.sleep(0.5)

        # 把动作档位恢复成「自然」，避免测试残留影响用户
        await js("""(()=>{try{localStorage.setItem('avatar2_motion','1');}catch(e){}
                   const s=document.querySelector('#av2Motion'); if(s) s.value='1'; return 'ok';})()""")

        print("展开前 collapsed:", await js(
            "document.querySelector('#av2Panel').classList.contains('collapsed')"))
        # 点折叠头展开
        await js("""(()=>{const p=document.querySelector('#av2Panel');
                   const h=p.querySelector('.fold-head')||p.querySelector('h2');
                   if(h) h.click(); return 'clicked';})()""")
        await asyncio.sleep(1.2)
        print("展开后 collapsed:", await js(
            "document.querySelector('#av2Panel').classList.contains('collapsed')"))

        # 滚到动作下拉
        await js("document.querySelector('#av2Motion').scrollIntoView({block:'center'})")
        await asyncio.sleep(0.8)
        print("位置:", await js("""(()=>{const s=document.querySelector('#av2Motion');
            const r=s.getBoundingClientRect();
            return JSON.stringify({x:Math.round(r.x),y:Math.round(r.y),
              w:Math.round(r.width),h:Math.round(r.height),
              inView:r.top>=0&&r.bottom<=innerHeight});})()"""))

        shot = await call("Page.captureScreenshot", {"format": "png"})
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_t_out", "ui_panel2.png")
        open(out, "wb").write(base64.b64decode(shot["data"]))
        print("截图:", out)

        # 相邻控件顺序（去掉 emoji，避免编码问题）
        txt = await js("""(()=>{const s=document.querySelector('#av2Motion');
            const row=s.closest('.av2-row')||s.parentElement;
            const p=row.parentElement; const arr=[];
            if(p) for(const c of p.children){
              arr.push((c===row?'>':' ')+String(c.textContent||'').replace(/[^\\x20-\\x7e\\u4e00-\\u9fa5]/g,'').trim().slice(0,18));
            } return arr.join(' | ');})()""")
        print("控件顺序:", safe(txt))

try:
    asyncio.run(main())
finally:
    proc.terminate()
    try: proc.wait(timeout=8)
    except Exception: proc.kill()
    subprocess.run(["powershell","-NoProfile","-Command",
        "Get-CimInstance Win32_Process | Where-Object {$_.CommandLine -like '*remote-debugging-port=%d*'} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" % PORT],
        capture_output=True)
    print("已清理")

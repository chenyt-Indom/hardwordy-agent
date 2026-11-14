"""展开数字人面板并截图，确认动作下拉的视觉对齐"""
import os, sys, json, time, subprocess, socket, urllib.request, base64
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asyncio
import websockets

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if not os.path.exists(EDGE):
    EDGE = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
PORT = 9334
BASE = "http://127.0.0.1:8000/"
TMP = os.path.join(os.environ["TEMP"], "edge_av2panel")
proc = subprocess.Popen([EDGE, "--headless=new", "--disable-gpu",
                         "--remote-debugging-port=%d" % PORT,
                         "--window-size=1500,1000", "--no-first-run",
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
                return "EXC: " + str(r["exceptionDetails"])[:200]
            return r.get("result", {}).get("value")

        await call("Page.enable"); await call("Runtime.enable")
        for _ in range(40):
            if await js("!!document.querySelector('#av2Motion')"): break
            await asyncio.sleep(0.5)

        print("可见性(offsetParent):", await js(
            "(()=>{const e=document.querySelector('#av2Motion');return e?String(e.offsetParent!==null):'no el';})()"))
        print("祖先链:", await js("""(()=>{
            let e=document.querySelector('#av2Motion'), out=[];
            while(e && out.length<8){ out.push(e.tagName+'#'+(e.id||'')+'.'+(e.className||'').toString().slice(0,40)
              +'['+(e.hidden?'hidden':'')+']'); e=e.parentElement; }
            return out.join(' < ');
        })()"""))

        # 找折叠头并点开（数字人相关）
        print("展开尝试:", await js("""(()=>{
            const s=document.querySelector('#av2Motion');
            if(!s) return 'no sel';
            // 若祖先有 display:none，往上找并尝试恢复
            let e=s, fixed=[];
            while(e){
              const st=getComputedStyle(e);
              if(st.display==='none'){ e.style.display='block'; fixed.push(e.tagName+'#'+(e.id||'')); }
              e=e.parentElement;
            }
            // 再找 hidden 属性
            let p=s;
            while(p){ if(p.hidden){ p.hidden=false; fixed.push('hidden:'+(p.id||p.tagName)); } p=p.parentElement; }
            const r=s.getBoundingClientRect();
            return JSON.stringify({fixed:fixed, rect:[Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)]});
        })()"""))

        await js("document.querySelector('#av2Motion').scrollIntoView({block:'center'})")
        await asyncio.sleep(0.6)
        shot = await call("Page.captureScreenshot", {"format": "png"})
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_t_out", "ui_panel.png")
        open(out, "wb").write(base64.b64decode(shot["data"]))
        print("截图:", out)

        # 读一下相邻控件，确认顺序
        print("相邻控件:", await js("""(()=>{
            const s=document.querySelector('#av2Motion');
            const row=s.closest('.av2-row')||s.parentElement;
            const sib=[];
            let p=row.parentElement;
            if(p) for(const c of p.children){
              const t=(c.textContent||'').trim().slice(0,22);
              sib.push((c===row?'>>':'  ')+t);
            }
            return sib.join(' | ');
        })()"""))

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

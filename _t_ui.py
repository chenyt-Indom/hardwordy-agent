"""真实浏览器验证：动作强度下拉（存在性/选项/持久化/布局）"""
import os, sys, json, time, subprocess, socket, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asyncio
import websockets

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if not os.path.exists(EDGE):
    EDGE = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
PORT = 9333
BASE = "http://127.0.0.1:8000/"

def free(port):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port)); return True
    except OSError:
        return False
    finally:
        s.close()

proc = None
if not free(PORT):
    print("端口 %d 被占用，先清理" % PORT)
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process | Where-Object {$_.CommandLine -like '*remote-debugging-port=%d*'} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" % PORT],
                   capture_output=True)
    time.sleep(1)

proc = subprocess.Popen([EDGE, "--headless=new", "--disable-gpu",
                         "--remote-debugging-port=%d" % PORT,
                         "--window-size=1400,900", "--no-first-run",
                         "--user-data-dir=" + os.path.join(os.environ["TEMP"], "edge_av2motion"),
                         BASE],
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
if not tabs:
    print("FAIL: DevTools 未就绪"); sys.exit(1)
page = None
for t in tabs:
    if t.get("type") == "page" and "127.0.0.1:8000" in (t.get("url") or ""):
        page = t; break
if page is None:
    page = [t for t in tabs if t.get("type") == "page"][0]
print("目标页:", page.get("url"))
ws_url = page["webSocketDebuggerUrl"]

async def main():
    async with websockets.connect(ws_url, max_size=20 * 1024 * 1024) as ws:
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
            return r.get("result", {}).get("value")

        await call("Page.enable")
        await call("Runtime.enable")
        for _ in range(40):
            ok = await js("!!document.querySelector('#av2Motion')")
            if ok: break
            await asyncio.sleep(0.5)

        print("1) 控件存在:", await js("!!document.querySelector('#av2Motion')"))
        print("2) 选项:", await js(
            "Array.from(document.querySelectorAll('#av2Motion option')).map(o=>o.value+'|'+o.textContent).join(' / ')"))
        print("3) 默认值:", await js("document.querySelector('#av2Motion').value"))
        print("4) 说明文字:", await js(
            "(document.querySelector('#av2Motion')||{}).title || ''"))

        # 切到 1.6 并触发 change
        await js("""(()=>{const s=document.querySelector('#av2Motion');
                   s.value='1.6'; s.dispatchEvent(new Event('change')); return s.value;})()""")
        print("5) 切到 1.6 后 value:", await js("document.querySelector('#av2Motion').value"))
        print("6) localStorage:", await js("localStorage.getItem('avatar2_motion')"))

        # 切回 0 验证
        await js("""(()=>{const s=document.querySelector('#av2Motion');
                   s.value='0'; s.dispatchEvent(new Event('change')); return s.value;})()""")
        print("7) 切到 0 后 localStorage:", await js("localStorage.getItem('avatar2_motion')"))

        # 布局：是否溢出容器
        print("8) 布局:", await js("""(()=>{
            const s=document.querySelector('#av2Motion');
            const r=s.getBoundingClientRect();
            const box=s.closest('.av2-row')||s.parentElement;
            const br=box.getBoundingClientRect();
            return JSON.stringify({sel:[Math.round(r.width),Math.round(r.height)],
              box:[Math.round(br.width),Math.round(br.height)],
              overflowX: r.right>br.right+1, overflowY: r.bottom>br.bottom+1,
              visible: r.width>50 && r.height>10});
        })()"""))

        # 面板整体是否横向溢出
        print("9) 面板宽度:", await js("""(()=>{
            const p=document.querySelector('#av2Motion');
            let el=p; while(el && el.clientWidth<200) el=el.parentElement;
            const box=el? el.getBoundingClientRect():null;
            return JSON.stringify({panel: box?[Math.round(box.width),Math.round(box.height)]:null,
              scrollW: el?el.scrollWidth:null, clientW: el?el.clientWidth:null});
        })()"""))

        # 截图
        shot = await call("Page.captureScreenshot", {"format": "png"})
        import base64
        data = base64.b64decode(shot["data"])
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_t_out", "ui_motion.png")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        open(out, "wb").write(data)
        print("10) 截图:", out, len(data), "bytes")

try:
    asyncio.run(main())
finally:
    if proc:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process | Where-Object {$_.CommandLine -like '*remote-debugging-port=%d*'} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" % PORT],
                   capture_output=True)
    print("已清理无头浏览器")

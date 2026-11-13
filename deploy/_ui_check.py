# 临时：用无头 Edge 走一遍「进群」交互，确认改动真的生效（验完即删）
import subprocess, time, json, os, tempfile, urllib.request, asyncio, base64
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import websockets

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
SHOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_ui_hive.png")


def boot():
    prof = os.path.join(tempfile.gettempdir(), "cdp_hive_ui")
    p = subprocess.Popen(
        [EDGE, "--headless=new", "--disable-gpu", "--no-sandbox",
         "--remote-debugging-port=9335", "--user-data-dir=" + prof,
         "--no-first-run", "--window-size=1440,900",
         "http://127.0.0.1:8000/"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            j = json.loads(urllib.request.urlopen(
                "http://127.0.0.1:9335/json/list", timeout=2).read())
            pg = [t for t in j if t.get("type") == "page" and "8000" in (t.get("url") or "")]
            if pg:
                return p, pg[0]["webSocketDebuggerUrl"]
            pg = [t for t in j if t.get("type") == "page"]
            if pg:
                return p, pg[0]["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.25)
    return p, None


async def main(ws_url):
    async with websockets.connect(ws_url, max_size=30 * 1024 * 1024) as ws:
        mid = 0

        async def cmd(method, params=None):
            nonlocal mid
            mid += 1
            await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            while True:
                m = json.loads(await ws.recv())
                if m.get("id") == mid:
                    return m

        await cmd("Page.enable")
        await cmd("Runtime.enable")

        async def ev(expr):
            r = await cmd("Runtime.evaluate",
                          {"expression": expr, "returnByValue": True, "awaitPromise": True})
            return r.get("result", {}).get("result", {}).get("value")

        # 等应用起来（app.js 会把群列表渲染出来）
        ready = False
        for _ in range(40):
            if await ev("!!document.getElementById('hiveGroupList')"):
                ready = True
                break
            await asyncio.sleep(0.3)
        print("0 应用就绪    :", ready)
        if not ready:
            return

        print("1 切群聊模式  :", await ev(
            "(()=>{const b=document.querySelector('[data-mode=\"hive\"]');"
            "if(!b)return 'no tab'; b.click(); return 'ok';})()"))
        await asyncio.sleep(0.8)
        print("2 空状态文字  :", await ev(
            "(()=>{const b=document.getElementById('hiveGroupList');"
            "return b?b.textContent.replace(/\\s+/g,' ').slice(0,90):'no box';})()"))
        print("3 点密码入群  :", await ev(
            "(()=>{document.getElementById('hiveJoinBtn').click();"
            "return document.getElementById('hiveModal').hidden?'隐藏':'已打开';})()"))
        await asyncio.sleep(0.4)
        print("4 地址框隐藏  :", await ev("document.getElementById('hiveModalExtra').hidden"))
        print("5 输入模式    :", await ev("document.getElementById('hiveModalInput').dataset.mode"))
        print("6 提示文字    :", await ev(
            "document.getElementById('hiveModalHint').textContent.replace(/\\s+/g,' ')"))
        print("7 乱输整形    :", await ev(
            "(()=>{const t=document.getElementById('hiveModalInput');"
            "t.value='ab0c d1efgh ijkl'; t.dispatchEvent(new Event('input',{bubbles:true}));"
            "return t.value;})()"))
        print("8 小写整形    :", await ev(
            "(()=>{const t=document.getElementById('hiveModalInput');"
            "t.value='abcdefghjkmn'; t.dispatchEvent(new Event('input',{bubbles:true}));"
            "return t.value;})()"))
        print("9 横杠幂等    :", await ev(
            "(()=>{const t=document.getElementById('hiveModalInput');"
            "t.value='ABCD-EFGH-JKMN'; t.dispatchEvent(new Event('input',{bubbles:true}));"
            "return t.value;})()"))
        print("10 建群向导   :", await ev(
            "(()=>{document.getElementById('hiveModalClose').click();"
            "document.getElementById('hiveCreateBtn').click();"
            "return document.getElementById('hiveSetup').hidden?'隐藏':'已打开';})()"))
        await asyncio.sleep(0.4)
        print("11 向导文字   :", await ev(
            "document.getElementById('hiveSetup').textContent.replace(/\\s+/g,' ').slice(0,150)"))
        # 关掉向导，截一张群聊面板
        await ev("document.getElementById('hsClose').click()")
        await asyncio.sleep(0.3)
        shot = await cmd("Page.captureScreenshot", {"format": "png"})
        open(SHOT, "wb").write(base64.b64decode(shot["result"]["data"]))
        print("12 截图       :", SHOT)


p, ws_url = boot()
print("ws:", bool(ws_url))
if ws_url:
    asyncio.run(main(ws_url))
p.terminate()
print("done")

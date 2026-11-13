# -*- coding: utf-8 -*-
"""补测：拖拽文件 + 保存密钥（临时脚本，验完删）。"""
import base64, json, os, shutil, subprocess, sys, tempfile, time, urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

URL = "http://127.0.0.1:8010"
PORT = 9341
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EDGES = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
         r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"]
exe = next((p for p in EDGES if os.path.exists(p)), None)
prof = tempfile.mkdtemp(prefix="edge_dp2_")
proc = subprocess.Popen([exe, "--headless=new", "--disable-gpu", "--no-sandbox",
                         "--disable-dev-shm-usage", "--no-first-run",
                         "--no-default-browser-check", "--remote-debugging-port=%d" % PORT,
                         "--user-data-dir=%s" % prof, "--window-size=1440,900", URL],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def wait_ws():
    for _ in range(40):
        try:
            d = json.loads(urllib.request.urlopen("http://127.0.0.1:%d/json" % PORT, timeout=2).read())
            for t in d:
                if t.get("type") == "page" and "127.0.0.1:8010" in (t.get("url") or ""):
                    return t["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.5)
    return None

ws_url = wait_ws()
if not ws_url:
    print("CDP 没起来"); proc.kill(); sys.exit(1)

from websockets.sync.client import connect
mid = [0]
def call(ws, method, params=None):
    mid[0] += 1
    ws.send(json.dumps({"id": mid[0], "method": method, "params": params or {}}))
    while True:
        m = json.loads(ws.recv(timeout=30))
        if m.get("id") == mid[0]:
            return m

def ev(ws, expr):
    r = call(ws, "Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
    res = r.get("result", {})
    if res.get("exceptionDetails"):
        return {"__error": str(res["exceptionDetails"])[:300]}
    return res.get("result", {}).get("value")

# 拦掉 alert/confirm，避免弹窗卡住
OVERRIDE = "window.alert = (m)=>{window.__alerts=(window.__alerts||[]);window.__alerts.push(String(m));};" \
           "window.confirm = ()=>true; window.toast = window.toast || function(){};"

with connect(ws_url, max_size=60 * 1024 * 1024) as ws:
    call(ws, "Page.enable"); call(ws, "Runtime.enable")
    for _ in range(40):
        if ev(ws, "document.readyState") == "complete":
            break
        time.sleep(0.4)
    time.sleep(1.5)
    ev(ws, OVERRIDE)
    ev(ws, "document.getElementById('deployBtn').click(); 'ok'")
    time.sleep(1.8)

    print("=== 1. 拖拽私钥文件 ===")
    r = ev(ws, """(async () => {
      const pem = '-----BEGIN OPENSSH PRIVATE KEY-----\\nAAAAB3NzaC1yc2E\\n-----END OPENSSH PRIVATE KEY-----';
      const f = new File([pem], 'id_rsa', {type: 'application/octet-stream'});
      const dt = new DataTransfer(); dt.items.add(f);
      const drop = document.getElementById('dpSshDrop');
      drop.dispatchEvent(new DragEvent('dragover', {dataTransfer: dt, bubbles: true, cancelable: true}));
      const clsOver = drop.classList.contains('over');
      drop.dispatchEvent(new DragEvent('drop', {dataTransfer: dt, bubbles: true, cancelable: true}));
      // file.text() 是 Promise —— 必须等它读完，否则读到的是空（上一版就踩了这个坑）
      await new Promise(r => setTimeout(r, 900));
      return {overClassApplied: clsOver, clsAfter: drop.className,
              dropText: drop.textContent,
              taLen: document.getElementById('dpSshText').value.length,
              taHead: document.getElementById('dpSshText').value.slice(0, 32)};
    })()""")
    print("  ", r)
    time.sleep(0.4)

    print("\n=== 2. 拖拽 100KB 文件（应被拦）===")
    r = ev(ws, """(() => {
      const big = 'x'.repeat(100*1024);
      const f = new File([big], 'big.bin', {type:'application/octet-stream'});
      const dt = new DataTransfer(); dt.items.add(f);
      window.__alerts = [];
      document.getElementById('dpSshDrop').dispatchEvent(new DragEvent('drop', {dataTransfer: dt, bubbles: true, cancelable: true}));
      return {alerts: window.__alerts};
    })()""")
    print("  ", r)
    time.sleep(0.5)

    print("\n=== 3. 保存假私钥（应被后端拒绝）===")
    r = ev(ws, """(async () => {
      const ta = document.getElementById('dpSshText');
      ta.value = '-----BEGIN OPENSSH PRIVATE KEY-----\\nBAD\\n-----END OPENSSH PRIVATE KEY-----';
      window.__alerts = [];
      document.getElementById('dpSshSave').click();
      await new Promise(r => setTimeout(r, 1500));
      return {alerts: window.__alerts, taLen: ta.value.length};
    })()""")
    print("  ", r)

    print("\n=== 4. 保存中继密钥（假内容，长度够）===")
    r = ev(ws, """(async () => {
      const ta = document.getElementById('dpTokText');
      const before = (document.getElementById('dpTokTag')||{}).textContent;
      ta.value = 'a'.repeat(40);
      document.getElementById('dpTokSave').click();
      await new Promise(r => setTimeout(r, 1500));
      return {taCleared: ta.value === '', tag: document.getElementById('dpTokTag').textContent};
    })()""")
    print("  ", r)

    print("\n=== 5. 按钮绑定存在性 ===")
    r = ev(ws, """(() => {
      const ids = ['dpClose','dpSrvSave','dpSrvClear','dpSshEye','dpSshSave','dpSshClear',
                   'dpTokEye','dpTokSave','dpTokClear','dpRun','dpRestart','dpStop'];
      const miss = ids.filter(i => !document.getElementById(i));
      return {missing: miss, count: ids.length - miss.length};
    })()""")
    print("  ", r)

subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
time.sleep(0.8)
shutil.rmtree(prof, ignore_errors=True)
print("\n完成")

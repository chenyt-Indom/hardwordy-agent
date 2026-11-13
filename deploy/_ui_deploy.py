# -*- coding: utf-8 -*-
"""无头浏览器验证「🔐 密钥与部署」弹窗（临时脚本，验完删）。"""
import base64, json, os, shutil, subprocess, sys, tempfile, time, urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

URL = "http://127.0.0.1:8010"
PORT = 9333
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "deploy", "_ui_deploy.png")

EDGES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
exe = next((p for p in EDGES if os.path.exists(p)), None)
if not exe:
    print("找不到 Edge"); sys.exit(1)

prof = tempfile.mkdtemp(prefix="edge_dp_")
proc = subprocess.Popen([
    exe, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
    "--remote-debugging-port=%d" % PORT, "--user-data-dir=%s" % prof,
    "--window-size=1440,900", URL,
], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def wait_ws():
    """只挑真正打开 8010 的那个 page target。

    ⚠️ 新 profile 下 Edge 会先开一个「同步数据 / 欢迎」页，直接取第一个
    page 会连错地方（实测踩过：标题变成「正在同步浏览数据」）。
    """
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
    r = call(ws, "Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                      "awaitPromise": True})
    return r.get("result", {}).get("result", {}).get("value")

with connect(ws_url, max_size=60 * 1024 * 1024) as ws:
    call(ws, "Page.enable"); call(ws, "Runtime.enable")
    # 等加载完
    for _ in range(40):
        st = ev(ws, "document.readyState")
        if st == "complete":
            break
        time.sleep(0.4)
    time.sleep(1.5)

    print("页面标题:", ev(ws, "document.title"))
    print("deployBtn 存在:", ev(ws, "!!document.getElementById('deployBtn')"))

    # 打开弹窗
    print("点击按钮:", ev(ws, "document.getElementById('deployBtn').click(); 'ok'"))
    time.sleep(2.0)

    info = ev(ws, """(() => {
      const m = document.getElementById('dpMask');
      if (!m) return {built: false};
      const box = m.querySelector('.dp-box');
      const st = document.getElementById('dpStatus');
      const r = box.getBoundingClientRect();
      return {
        built: true, hidden: m.hidden,
        boxW: Math.round(r.width), boxH: Math.round(r.height),
        overflowX: box.scrollWidth > box.clientWidth + 1,
        status: st ? st.textContent : '',
        tags: ['dpSrvTag','dpSshTag','dpTokTag'].map(id => {
          const n = document.getElementById(id);
          return n ? n.textContent + '(' + n.className + ')' : null;
        }),
        host: (document.getElementById('dpHost')||{}).value,
        user: (document.getElementById('dpUser')||{}).value,
        port: (document.getElementById('dpPort')||{}).value,
        relayPort: (document.getElementById('dpRelayPort')||{}).value,
        sshHint: (document.getElementById('dpSshHint')||{}).textContent,
        sshTextEmpty: (document.getElementById('dpSshText')||{}).value === '',
        tokTextEmpty: (document.getElementById('dpTokText')||{}).value === '',
        dropText: (document.getElementById('dpSshDrop')||{}).textContent,
        runBtn: (document.getElementById('dpRun')||{}).textContent,
      };
    })()""")
    print("\n弹窗状态:")
    for k, v in (info or {}).items():
        print("   %-14s %s" % (k, v))

    # 点眼睛：应取到明文
    ev(ws, "document.getElementById('dpSshEye').click(); 'ok'")
    time.sleep(1.6)
    eye = ev(ws, """(() => {
      const ta = document.getElementById('dpSshText');
      const b = document.getElementById('dpSshEye');
      return {len: (ta.value||'').length, head: (ta.value||'').slice(0,32),
              btn: b.textContent, on: b.dataset.on};
    })()""")
    print("\n点眼睛后:", eye)

    # 再点一次应收起
    ev(ws, "document.getElementById('dpSshEye').click(); 'ok'")
    time.sleep(0.8)
    print("再点一次:", ev(ws, "({len: document.getElementById('dpSshText').value.length, btn: document.getElementById('dpSshEye').textContent})"))

    # 截图
    shot = call(ws, "Page.captureScreenshot", {"format": "png"})
    data = shot.get("result", {}).get("data")
    if data:
        with open(OUT, "wb") as f:
            f.write(base64.b64decode(data))
        print("\n截图:", OUT, os.path.getsize(OUT), "字节")

proc.kill()
time.sleep(0.5)
shutil.rmtree(prof, ignore_errors=True)
print("完成")

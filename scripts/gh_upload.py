# -*- coding: utf-8 -*-
"""GitHub Release 上传器 v2（修 Content-Length）

用法：
  py -3 gh_upload.py whoami
  py -3 gh_upload.py probe  <file>          # 传前 5MB 测速
  py -3 gh_upload.py upload <file> [asset]  # 传整个文件
"""
import json
import os
import subprocess
import sys
import threading
import time

import requests

OWNER = "chenyt-Indom"
REPO = "local-multimodal"
TAG = "v2026.11.10"
NAME = "本地多模态助手 2026.11.10c（全量版）"
BODY = """**全量版 · 2026.11.10c**（不含任何 API Key，请自行在程序里填）

GitHub 单文件上限 2 GB，所以切两卷，下载后合并：

```cmd
copy /b LocalMultimodalAssistant-Full-2026.11.10c.zip.part1 + LocalMultimodalAssistant-Full-2026.11.10c.zip.part2 LocalMultimodalAssistant-Full-2026.11.10c.zip
```

校验（合并后应等于）：
```
49ec6e997c1370e85fd0950167e5b5fccefe5231d2bae7f99517b189f689d0df
```

解压 → 双击 `一键启动.bat`；顶栏 🔑 填自己的 DeepSeek Key。
"""


def get_token():
    out = subprocess.run(["git", "credential", "fill"],
                         input="protocol=https\nhost=github.com\n\n",
                         capture_output=True, text=True, cwd=os.getcwd())
    for line in out.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("没拿到 token")


def api(tok, method, url, **kw):
    h = {"Authorization": "Bearer " + tok,
         "Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28"}
    h.update(kw.pop("headers", {}))
    return requests.request(method, url, headers=h, timeout=120, **kw)


def ensure_release(tok):
    r = api(tok, "GET", f"https://api.github.com/repos/{OWNER}/{REPO}/releases/tags/{TAG}")
    if r.status_code == 200:
        rid = r.json()["id"]
        print("复用 release:", rid)
        return rid
    r = api(tok, "POST", f"https://api.github.com/repos/{OWNER}/{REPO}/releases",
            json={"tag_name": TAG, "name": NAME, "body": BODY})
    if r.status_code not in (200, 201):
        raise SystemExit("建 release 失败: %s %s" % (r.status_code, r.text[:200]))
    print("已创建 release:", r.json()["id"], r.json()["html_url"])
    return r.json()["id"]


def existing_asset(tok, rid, name):
    r = api(tok, "GET", f"https://api.github.com/repos/{OWNER}/{REPO}/releases/{rid}/assets")
    if r.status_code == 200:
        for a in r.json():
            if a["name"] == name:
                return a
    return None


def upload(tok, rid, path, asset_name=None):
    name = asset_name or os.path.basename(path)
    size = os.path.getsize(path)
    old = existing_asset(tok, rid, name)
    if old:
        print("已存在同名 asset（%d 字节），先删掉：" % old["size"])
        api(tok, "DELETE", f"https://api.github.com/repos/{OWNER}/{REPO}/releases/assets/{old['id']}")

    url = ("https://uploads.github.com/repos/%s/%s/releases/%d/assets?name=%s"
           % (OWNER, REPO, rid, requests.utils.quote(name)))
    f = open(path, "rb")
    t0 = time.time()
    stop = threading.Event()

    def beat():
        while not stop.wait(10):
            sent = f.tell()
            print("   %.1f%%  %.0f/%.0f MB  %.0f KB/s"
                  % (sent * 100.0 / size, sent / 1048576, size / 1048576,
                     sent / max(time.time() - t0, 1) / 1024), flush=True)

    th = threading.Thread(target=beat, daemon=True)
    th.start()
    try:
        r = requests.post(url, headers={
            "Authorization": "Bearer " + tok,
            "Content-Type": "application/octet-stream",
            "Accept": "application/vnd.github+json",
        }, data=f, timeout=(30, 7200))
    finally:
        stop.set()
        f.close()
    dt = max(time.time() - t0, 0.001)
    if r.status_code in (200, 201):
        a = r.json()
        print("完成 %s  %.2f MB  用时 %.1f 分钟  平均 %.0f KB/s"
              % (a["name"], a["size"] / 1048576, dt / 60, a["size"] / dt / 1024))
        return True
    print("失败: HTTP %s  %s" % (r.status_code, r.text[:300]))
    return False


if __name__ == "__main__":
    tok = get_token()
    mode = sys.argv[1]
    if mode == "whoami":
        r = api(tok, "GET", "https://api.github.com/user")
        print("token:", r.json().get("login"), "| scopes:", r.headers.get("x-oauth-scopes"))
    elif mode == "probe":
        rid = ensure_release(tok)
        tmp = os.path.join(os.environ.get("TEMP", "."), "_probe5m.bin")
        with open(sys.argv[2], "rb") as src, open(tmp, "wb") as dst:
            dst.write(src.read(5 * 1024 * 1024))
        upload(tok, rid, tmp, asset_name="probe-5mb.bin")
        os.remove(tmp)
    elif mode == "upload":
        rid = ensure_release(tok)
        ok = upload(tok, rid, sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
        sys.exit(0 if ok else 1)

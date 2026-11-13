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

# ⚠️ 2026-11-13（11.13f）：本机有 HTTP(S)_PROXY 指向本地代理。
#   实测结论（别凭感觉选）：
#     · api.github.com      —— 直连 0.61s、代理 0.88s，都能用；
#     · uploads.github.com  —— **直连被本机策略掐断**（WinError 10053），
#                              必须走代理；代理约 1.2~1.3 MB/s，长连接偶尔
#                              ConnectionReset(10054) → 所以要带重试。
#   设 GH_DIRECT=1 可强制直连（换网络环境时用来对比）。
if os.environ.get("GH_DIRECT") == "1":
    for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
               "ALL_PROXY", "all_proxy"):
        os.environ.pop(_k, None)

OWNER = "chenyt-Indom"
REPO = "hardwordy-agent"
TAG = "v2026.11.13f"
NAME = "Hardwordy Agent 2026.11.13f（全量版）"
BODY = """**全量版 · 2026.11.13f**（不含任何 API Key，请自行在程序里填）

## 这一版新增：⏰ 自动化（定时任务）

- **定时 / 长期任务**：支持「每 N 分钟」和「每天 HH:MM」两种周期。
  时间用 24 档小时 + 60 档分钟两个下拉拼出来，不用手敲格式
  （敲错就永远不触发，这种问题最难查）。
- **结果落回会话**：第二天打开应用，在对应会话里就能看到
  「【定时任务】昨晚跑了什么」；日志另存一份便于排查。
- **固定走本机 Ollama**：半夜跑任务时没人给你送云端 Key
  （Key 只存在浏览器本地，服务端从不存），所以这一路只认本地模型；
  面板里可以指定用哪个本地模型。
- **无人值守不做交互**：需要提问 / 确认的地方直接跳过或按通过处理 ——
  弹一个没人点的框等于永久卡死，比「没问」更糟。
- 侧栏与弹窗两个入口共用同一套卡片与动作处理；面板首次默认收起；
  结果窗口里 markdown 正常渲染（标题 / 列表 / 代码块不再是一坨纯文本）。

## 上一版（11.13e）还包含

- 启动器与重启更稳：端口检测 bind + connect_ex 双路、校验 8000 应答的是自己、
  bind 前确认端口空闲（避开 10048）、重启前等旧窗口关闭；
  监听地址改 0.0.0.0，同一 WiFi 下手机可以直接打开界面。
- 群聊 @hd 接入自开发工具循环；连接码多源探测公网 IP + 缓存 + lan_addr；
  未知帧安全忽略不再崩连接。
- 界面大改：侧栏去掉灰框改极淡底、输入区改带边框卡片、小屏收紧留白；
  撤掉语音输入入口。

## 更早的修修

11.13d 群聊附件整套（单个坏附件不再阻断后续、纯附件消息可发、
图片按真实 mime、历史附件进提示词、断线附件入补发队列）；
11.13c 群聊补发重做 + P2P 双向连接去重；
11.13b 知识库语义检索 + 记忆向量索引；
11.13a TTS 与重启修复。

## 下载

GitHub 单文件上限 2 GB，所以切两卷，下载后合并：

```cmd
copy /b HardwordyAgent-Full-2026.11.13f.zip.part1 + HardwordyAgent-Full-2026.11.13f.zip.part2 HardwordyAgent-Full-2026.11.13f.zip
```

校验（合并后应等于）：
```
fcc57b363628c6bb23c3feedc1ee17f6836491c44785e646e3a9db6ecba641e9
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


def upload(tok, rid, path, asset_name=None, tries=6):
    """分块上传 release asset，失败自动重试。

    ⚠️ 2026-11-13（11.13f）：代理传 1.9GB 时会在任意进度被 ConnectionReset
    (10054) 掐断。GitHub 的 asset 上传**不支持 Range 续传**，重试只能从头来，
    所以这里每次失败先清掉残留的半截 asset（否则下一次会 422 已存在），
    再退避重试。实测 1.2~1.3 MB/s，1946MB 约 25 分钟。
    """
    name = asset_name or os.path.basename(path)
    size = os.path.getsize(path)

    def drop_stale():
        old = existing_asset(tok, rid, name)
        if old:
            api(tok, "DELETE",
                f"https://api.github.com/repos/{OWNER}/{REPO}/releases/assets/{old['id']}")
            return old["id"]
        return None

    if drop_stale():
        print("已存在同名 asset，已删除（准备重传）")

    url = ("https://uploads.github.com/repos/%s/%s/releases/%d/assets?name=%s"
           % (OWNER, REPO, rid, requests.utils.quote(name)))

    for attempt in range(1, tries + 1):
        f = open(path, "rb")
        t0 = time.time()
        stop = threading.Event()

        def beat():
            while not stop.wait(15):
                sent = f.tell()
                print("   第%d次 %.1f%%  %.0f/%.0f MB  %.0f KB/s"
                      % (attempt, sent * 100.0 / size, sent / 1048576,
                         size / 1048576, sent / max(time.time() - t0, 1) / 1024),
                      flush=True)

        th = threading.Thread(target=beat, daemon=True)
        th.start()
        r = None
        try:
            r = requests.post(url, headers={
                "Authorization": "Bearer " + tok,
                "Content-Type": "application/octet-stream",
                "Accept": "application/vnd.github+json",
            }, data=f, timeout=(30, 7200))
        except Exception as e:
            print("   第%d次中断：%s %s" % (attempt, type(e).__name__, str(e)[:150]))
        finally:
            stop.set()
            try:
                f.close()
            except Exception:
                pass

        if r is not None and r.status_code in (200, 201):
            a = r.json()
            dt = max(time.time() - t0, 0.001)
            print("完成 %s  %.2f MB  用时 %.1f 分钟  平均 %.0f KB/s"
                  % (a["name"], a["size"] / 1048576, dt / 60, a["size"] / dt / 1024))
            return True
        if r is not None:
            print("   第%d次 HTTP %s  %s" % (attempt, r.status_code, r.text[:200]))

        drop_stale()
        if attempt < tries:
            wait = min(20 * attempt, 90)
            print("   %d 秒后重试（%d/%d）" % (wait, attempt + 1, tries), flush=True)
            time.sleep(wait)
    print("已重试 %d 次仍失败" % tries)
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

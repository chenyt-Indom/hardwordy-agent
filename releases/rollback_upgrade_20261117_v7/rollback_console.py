# -*- coding: utf-8 -*-
"""全栈快照 · 回滚控制台（独立版）

不修改被升级项目的任何现有文件：
  - 只读取同目录下的 rescue.py 并调用它的函数
  - 不 import backend，主程序崩了也能用
  - 纯标准库，无需安装任何依赖
"""
import importlib.util
import json
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
RESCUE_PATH = os.path.join(HERE, "rescue.py")
PORT = 8123

if not os.path.exists(RESCUE_PATH):
    print("找不到 rescue.py，请把它和本文件放在同一个目录（通常是项目根目录）")
    sys.exit(1)


def _load_rescue():
    spec = importlib.util.spec_from_file_location("rescue_core", RESCUE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rescue_core"] = mod
    spec.loader.exec_module(mod)
    return mod


RESCUE = _load_rescue()


def _norm(s):
    try:
        size = s.get("size") or 0
    except Exception:
        size = 0
    return {
        "name": s.get("name") or os.path.basename(str(s.get("path") or "")),
        "path": s.get("path") or "",
        "size_mb": round(size / 1048576.0, 1) if size else 0,
        "mtime": str(s.get("mtime_str") or s.get("mtime") or s.get("created") or ""),
        "healthy": bool(s.get("healthy")),
        "bad": bool(s.get("bad")),
        "man": bool(s.get("man")),
        "tag": s.get("tag") or "",
    }


def snapshots():
    items = RESCUE.list_snapshots() or []
    out = [_norm(s) for s in items]
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>回滚控制台</title>
<style>
body{background:#16161a;color:#e6e6ea;font:14px/1.6 -apple-system,"Microsoft YaHei",sans-serif;margin:0;padding:24px}
h2{font-size:17px;margin:0 0 4px}
.sub{color:#8b8b95;font-size:12px;margin-bottom:18px}
.row{display:flex;align-items:center;gap:12px;padding:10px 12px;border-bottom:1px solid #2a2a32}
.row:hover{background:#1d1d24}
.nm{flex:1;font-size:13px;word-break:break-all}
.meta{color:#8b8b95;font-size:12px;white-space:nowrap}
.tag{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;margin-left:6px}
.t-ok{background:#173a22;color:#7bd88f}
.t-bad{background:#2c2c33;color:#9a9aa2}
.t-man{background:#1b2a45;color:#79b8ff}
button{background:#2a2a33;color:#e6e6ea;border:1px solid #3a3a45;border-radius:6px;padding:5px 12px;cursor:pointer;font-size:13px}
button:hover{background:#34343f}
button.pri{background:#2f5fd0;border-color:#2f5fd0}
.bar{margin-top:18px;display:flex;gap:10px}
.msg{margin-top:14px;padding:10px 12px;border-radius:6px;font-size:13px;display:none}
.msg.ok{display:block;background:#173a22;color:#7bd88f}
.msg.err{display:block;background:#3a1b1b;color:#ff8f8f}
</style></head><body>
<h2>全栈快照 · 回滚控制台</h2>
<div class="sub">项目目录：__ROOT__ ｜ 端口 __PORT__</div>
<div id="list"><div class="meta">加载中…</div></div>
<div class="bar">
  <button class="pri" onclick="mk()">＋ 立即快照</button>
  <button onclick="autoRoll()">⟲ 回滚到最近健康版</button>
  <button onclick="load()">刷新</button>
</div>
<div id="msg" class="msg"></div>
<script>
function esc(s){return String(s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function show(t,ok){var m=document.getElementById('msg');m.className='msg '+(ok?'ok':'err');m.textContent=t}
function load(){
  fetch('/api/snapshots').then(function(r){return r.json()}).then(function(d){
    if(!d.ok){show('读取失败：'+d.error,false);return}
    var h='';
    if(!d.items.length){h='<div class="meta">还没有快照，点下面「立即快照」建一个</div>'}
    d.items.forEach(function(s){
      var t='';
      if(s.man) t+='<span class="tag t-man">📌 手动</span>';
      if(s.bad) t+='<span class="tag t-bad">回滚前备份</span>';
      else if(s.healthy) t+='<span class="tag t-ok">健康</span>';
      h+='<div class="row"><div class="nm">'+esc(s.name)+'</div>'
        +'<div class="meta">'+esc(s.mtime)+' · '+s.size_mb+' MB'+t+'</div>'
        +'<button onclick="rb(this)" data-n="'+esc(s.name)+'">回滚</button></div>';
    });
    document.getElementById('list').innerHTML=h;
  }).catch(function(e){show('连接失败：'+e,false)})
}
function rb(btn){
  var n=btn.getAttribute('data-n');
  if(!confirm('确定回滚到：\n'+n+'\n\n回滚前会自动把当前状态存一份备份，可再退回。\n注意：data/ 下的状态文件也会一起退回。')) return;
  btn.disabled=true;btn.textContent='回滚中…';
  fetch('/api/rollback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n})})
    .then(function(r){return r.json()}).then(function(d){
      if(d.ok){show('回滚成功：'+d.detail+'。请重启主程序使其生效。',true);load()}
      else{show('回滚失败：'+d.error,false);btn.disabled=false;btn.textContent='回滚'}
    }).catch(function(e){show('请求失败：'+e,false);btn.disabled=false;btn.textContent='回滚'})
}
function autoRoll(){
  if(!confirm('回滚到最近一个健康快照？\n会逐个尝试，坏快照自动跳过。')) return;
  fetch('/api/auto',{method:'POST'}).then(function(r){return r.json()}).then(function(d){
    if(d.ok){show('自动回滚成功：'+d.detail,true);load()}else{show('自动回滚失败：'+d.error,false)}
  })
}
function mk(){
  var tag=prompt('给这个快照起个名字（方便以后认出来）：','手动快照');
  if(tag===null) return;
  fetch('/api/snapshot',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tag:tag||'手动快照'})})
    .then(function(r){return r.json()}).then(function(d){
      if(d.ok){show('已创建：'+d.detail,true);load()}else{show('创建失败：'+d.error,false)}
    })
}
load();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        if self.path.startswith("/api/snapshots"):
            try:
                self._json({"ok": True, "items": snapshots()})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
            return
        page = HTML.replace("__ROOT__", RESCUE.ROOT).replace("__PORT__", str(PORT))
        self._send(200, page, "text/html; charset=utf-8")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        try:
            if self.path.startswith("/api/snapshot"):
                tag = (body.get("tag") or "手动快照").strip() or "手动快照"
                r = RESCUE.create_snapshot(tag=tag, prefix="man") or {}
                if not isinstance(r, dict):
                    r = {"ok": True, "name": str(r)}
                if r.get("ok"):
                    self._json({"ok": True, "detail": str(r.get("name") or r.get("path") or "")})
                else:
                    self._json({"ok": False, "error": str(r.get("error") or "创建失败")})
            elif self.path.startswith("/api/rollback"):
                name = body.get("name") or ""
                if not name:
                    self._json({"ok": False, "error": "缺少快照名"})
                    return
                path = os.path.join(RESCUE.SNAP_DIR, name)
                if not os.path.exists(path):
                    self._json({"ok": False, "error": "找不到快照文件：%s" % name})
                    return
                r = RESCUE.rollback(path, force=True) or {}
                if not isinstance(r, dict):
                    r = {"ok": bool(r), "detail": str(r)}
                ok = bool(r.get("ok"))
                detail = str(r.get("detail") or r.get("msg") or r.get("name") or "")
                if ok:
                    self._json({"ok": True, "detail": detail or "已恢复文件"})
                else:
                    self._json({"ok": False, "error": str(r.get("error") or "回滚失败")})
            elif self.path.startswith("/api/auto"):
                r = RESCUE.auto_rollback() or {}
                if not isinstance(r, dict):
                    r = {"ok": bool(r), "detail": str(r)}
                ok = bool(r.get("ok"))
                detail = str(r.get("detail") or r.get("msg") or r.get("name") or "")
                if ok:
                    self._json({"ok": True, "detail": detail or "已回滚"})
                else:
                    self._json({"ok": False, "error": str(r.get("error") or "自动回滚失败")})
            else:
                self._json({"ok": False, "error": "未知接口"}, 404)
        except Exception as e:
            self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})


def main():
    print("=" * 56)
    print(" 全栈快照 · 回滚控制台")
    print(" 项目目录：%s" % RESCUE.ROOT)
    print(" 快照目录：%s" % RESCUE.SNAP_DIR)
    print(" 浏览器打开：http://127.0.0.1:%d" % PORT)
    print(" 关闭窗口即退出")
    print("=" * 56)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        print("端口 %d 被占用（%s），请关掉占用它的程序后重试" % (PORT, e))
        return 1
    threading.Timer(0.8, lambda: webbrowser.open("http://127.0.0.1:%d" % PORT)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())

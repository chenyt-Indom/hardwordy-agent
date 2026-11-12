"""临时探针：验证 Ollama 是否支持**历史消息**上的 images（不是只认最后一条）。

做法：构造两条 user 消息，第一条带一张纯红色图，第二条问「我上一条消息里那张图是什么颜色」。
  · 模型答"红色" → 历史图被正确接收，跨轮方案成立
  · 模型答"没有图片/看不到" → 只认最后一条，方案要改
用完即删。
"""
import base64
import io
import json
import sys
import urllib.request

from PIL import Image

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = "http://127.0.0.1:11434"


def get(path, timeout=10):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def post(path, body, timeout=180):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


try:
    tags = get("/api/tags")
except Exception as e:
    print("Ollama 不可达：%s" % e)
    sys.exit(0)

models = [m.get("name", "") for m in tags.get("models", [])]
print("本机模型:", models)

# 挑一个视觉模型
vis = [m for m in models if any(k in m.lower() for k in
                                ("vl", "llava", "vision", "minicpm", "gemma3", "qwen2.5v"))]
if not vis:
    print("没有视觉模型，无法验证历史图片")
    sys.exit(0)
model = vis[0]
print("用模型:", model)

# 造一张纯红色图
buf = io.BytesIO()
Image.new("RGB", (320, 240), (220, 20, 20)).save(buf, format="PNG")
b64 = base64.b64encode(buf.getvalue()).decode("ascii")

body = {
    "model": model,
    "stream": False,
    "options": {"num_ctx": 8192, "num_predict": 120},
    "messages": [
        {"role": "user", "content": "记住这张图片。", "images": [b64]},
        {"role": "assistant", "content": "好的，我记住了。"},
        {"role": "user", "content": "我刚才发给你的那张图片是什么颜色的？只回答颜色。"},
    ],
}
try:
    r = post("/api/chat", body)
    msg = (r.get("message") or {})
    print("历史图回答:", repr(str(msg.get("content") or "").strip()[:200]))
    print("thinking:", repr(str(msg.get("thinking") or "").strip()[:100]))
except Exception as e:
    print("请求失败:", type(e).__name__, str(e)[:300])

"""临时探针 2：端到端验证「历史图 + 本轮图」同时存在时模型能否区分。

链路：chatimg.record → chatimg.attach → ollama_client.chat(images_base64=本轮图)
预期：模型能说出第一张是红色、第二张是蓝色。
用完即删。
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from PIL import Image

from backend import chatimg, ollama_client

TMP = os.path.abspath("data/_tmp_imgtest")
os.makedirs(TMP, exist_ok=True)


def make(name, color):
    p = os.path.join(TMP, name)
    Image.new("RGB", (480, 360), color).save(p, format="PNG")
    return p


red = make("e2e_red.png", (220, 20, 20))
blue = make("e2e_blue.png", (20, 20, 220))

sid = "_e2e_sid"
d = chatimg._load()
d["sessions"].pop(sid, None)
chatimg._save(d)

turn1 = "记住这张图片：这是我家的猫。"
chatimg.record(sid, turn1, [red])

messages = [
    {"role": "user", "content": turn1},
    {"role": "assistant", "content": "好的，我记住了。"},
    {"role": "user", "content": "现在再看这一张。"},
]

hist = chatimg.attach(messages, sid)
print("历史回挂张数:", len(hist))
print("msg0 带图:", len(messages[0].get("images") or []),
      "| msg2 带图:", len(messages[2].get("images") or []))

# 本轮图（蓝色）按原路挂到最后一条
cur = chatimg._load_b64(blue)

client = ollama_client.OllamaClient() if hasattr(ollama_client, "OllamaClient") else None
if client is None:
    print("找不到 OllamaClient 类")
    print("模块属性:", [a for a in dir(ollama_client) if "Client" in a or "client" in a][:10])
    sys.exit(0)

resp = client.chat(
    messages,
    model="qwen3-vl:8b",
    stream=False,
    images_base64=[cur],
    params={"max_tokens": 200, "temperature": 0.1},
)
data = resp.json() if hasattr(resp, "json") else resp
msg = data.get("message") or {}
print("回答:", repr(str(msg.get("content") or "").strip()[:300]))

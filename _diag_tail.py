"""诊断：后端 /api/chat 的 done.text 与增量拼接是否一致（少最后几个字）。"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
OUT = r"D:\local-multimodal-src\_diag_tail_out.txt"

payload = {
    "messages": [{"role": "user", "content": "只回答这一句，不要多余的话：一二三四五六七八九十。"}],
    "stream": True,
    "session_id": "diag-tail-check",
}
req = urllib.request.Request(
    BASE + "/api/chat",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
)

lines_out = []
t0 = time.time()
raw_lines, content_parts = [], []
done_obj = None
err = ""

try:
    resp = urllib.request.urlopen(req, timeout=280)
    buf = b""
    while True:
        chunk = resp.read(4096)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            ln, buf = buf.split(b"\n", 1)
            s = ln.decode("utf-8", "replace").strip()
            if not s:
                continue
            raw_lines.append(s)
            try:
                o = json.loads(s)
            except Exception:
                continue
            m = o.get("message") or {}
            if m.get("content"):
                content_parts.append(m["content"])
            if o.get("done"):
                done_obj = o
    s = buf.decode("utf-8", "replace").strip()
    if s:
        raw_lines.append(s)
        try:
            o = json.loads(s)
            m = o.get("message") or {}
            if m.get("content"):
                content_parts.append(m["content"])
            if o.get("done"):
                done_obj = o
        except Exception:
            pass
except Exception as e:
    err = "%s: %s" % (type(e).__name__, e)

joined = "".join(content_parts)
lines_out.append("耗时 %.1fs  收到 %d 行" % (time.time() - t0, len(raw_lines)))
if err:
    lines_out.append("请求异常: " + err)
lines_out.append("增量拼接长度: %d" % len(joined))
lines_out.append("增量末尾: %r" % joined[-40:])
if done_obj:
    ft = done_obj.get("text") or ""
    lines_out.append("done.text 长度: %d" % len(ft))
    lines_out.append("done 末尾: %r" % ft[-40:])
    lines_out.append("两者相等: %s" % (ft == joined))
    if ft != joined:
        lines_out.append("差异段: %r" % (ft[len(joined):] if len(ft) > len(joined) else joined[len(ft):]))
else:
    lines_out.append("!! 没有收到 done 块")
lines_out.append("最后 2 行原文:")
for x in raw_lines[-2:]:
    lines_out.append("  " + x[:400])

with open(OUT, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines_out))
print("\n".join(lines_out))

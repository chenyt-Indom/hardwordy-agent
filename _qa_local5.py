"""诊断：回填后模型到底返回了什么（完整字段）。"""
import os, sys, json
ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT); sys.path.insert(0, ROOT)
from backend import main as M

MODEL = "qwen3-vl:8b"
TOOL = [{"type": "function", "function": {
    "name": "get_time", "description": "查询当前本地时间",
    "parameters": {"type": "object", "properties": {}}}}]
BASE = [{"role": "system", "content": "需要时间时必须调用 get_time 工具，不要猜。"},
        {"role": "user", "content": "现在几点？调工具查。"}]
RESULT = "2026-11-15 01:45:33 (Sunday)"

r = M.client.chat(BASE, model=MODEL, stream=False,
                  params={"temperature": 0.1, "max_tokens": 120}, tools=TOOL)
d = r.json() or {}
calls = (d.get("message") or {}).get("tool_calls") or []
print("calls =", json.dumps(calls, ensure_ascii=False))

msgs = BASE + [{"role": "assistant", "content": "", "tool_calls": calls},
               {"role": "tool", "content": RESULT, "tool_name": "get_time"}]
r2 = M.client.chat(msgs, model=MODEL, stream=False,
                   params={"temperature": 0.1, "max_tokens": 300})
d2 = r2.json() or {}
print("\n完整响应：")
print(json.dumps(d2, ensure_ascii=False, indent=2)[:1200])

print("\n==== 换一种：assistant 带上 content 再回填 ====")
msgs3 = BASE + [{"role": "assistant", "content": "我来查一下时间。",
                 "tool_calls": calls},
                {"role": "tool", "content": RESULT, "tool_name": "get_time"}]
r3 = M.client.chat(msgs3, model=MODEL, stream=False,
                   params={"temperature": 0.1, "max_tokens": 300})
d3 = r3.json() or {}
print("content =", repr(((d3.get("message") or {}).get("content") or "")[:300]))
print("done_reason =", d3.get("done_reason"))

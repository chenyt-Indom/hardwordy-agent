"""对照实验：Ollama 工具回填，三种写法哪种能被模型读到。

A) 原样回传 tool_calls + role=tool/tool_name
B) 清理 tool_calls（只留 function.name/arguments）+ role=tool/tool_name
C) 工具结果包成 user 消息（文本协议兜底）
"""
import os, sys, json, time
ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT); sys.path.insert(0, ROOT)
from backend import main as M

MODEL = os.environ.get("QA_MODEL") or "qwen2.5vl:3b"
TOOL = [{"type": "function", "function": {
    "name": "get_time", "description": "查询当前本地时间",
    "parameters": {"type": "object", "properties": {}}}}]
BASE = [{"role": "system", "content": "需要时间时必须调用 get_time 工具，不要猜。"},
        {"role": "user", "content": "现在几点？调工具查。"}]
RESULT = "2026-11-15 01:45:33 (Sunday)"


def once(msgs, label):
    t0 = time.time()
    try:
        r = M.client.chat(msgs, model=MODEL, stream=False,
                          params={"temperature": 0.1, "max_tokens": 200})
        d = r.json() or {}
        out = ((d.get("message") or {}).get("content") or "").strip()
        print("   [%s] %.1fs → %s" % (label, time.time() - t0, (out[:150] or "（空）")))
        return out
    except Exception as e:
        print("   [%s] 失败 %s: %s" % (label, type(e).__name__, str(e)[:160]))
        return ""


print("模型：", MODEL)
print("0) 先拿到一次真实 tool_calls")
calls = []
try:
    r = M.client.chat(BASE, model=MODEL, stream=False,
                      params={"temperature": 0.1, "max_tokens": 200}, tools=TOOL)
    d = r.json() or {}
    calls = (d.get("message") or {}).get("tool_calls") or []
except Exception as e:
    print("   失败：", e)
print("   tool_calls =", json.dumps(calls, ensure_ascii=False)[:300] or "（无）")
if not calls:
    print("该模型没返回 tool_calls，无法继续对照。")
    raise SystemExit(0)

print("A) 原样回传")
a = once(BASE + [{"role": "assistant", "content": "", "tool_calls": calls},
                 {"role": "tool", "content": RESULT, "tool_name": "get_time"}], "A")
print("   含时间:", ("01:45" in a) or ("2026" in a))

print("B) 清理 tool_calls 后回传")
clean = [{"function": {"name": (c.get("function") or {}).get("name"),
                       "arguments": (c.get("function") or {}).get("arguments") or {}}}
         for c in calls]
b = once(BASE + [{"role": "assistant", "content": "", "tool_calls": clean},
                 {"role": "tool", "content": RESULT, "tool_name": "get_time"}], "B")
print("   含时间:", ("01:45" in b) or ("2026" in b))

print("C) 结果包成 user 消息（文本协议兜底）")
c = once(BASE + [{"role": "assistant", "content": "（调用 get_time）"},
                 {"role": "user", "content": "工具结果：\n" + RESULT
                  + "\n\n请基于这个结果直接回答。"}], "C")
print("   含时间:", ("01:45" in c) or ("2026" in c))

print("=" * 60)
print("A=%s B=%s C=%s" % (bool(a), bool(b), bool(c)))

"""只测 B：清理 tool_calls 后回填，看模型能否读到工具结果。"""
import os, sys, json, time
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


def call(msgs, tools=None, mt=120):
    t0 = time.time()
    r = M.client.chat(msgs, model=MODEL, stream=False,
                      params={"temperature": 0.1, "max_tokens": mt}, tools=tools)
    d = r.json() or {}
    m = d.get("message") or {}
    return m, time.time() - t0


print("0) 拿 tool_calls（模型应已预热）")
m0, dt = call(BASE, TOOL)
calls = m0.get("tool_calls") or []
print("   %.1fs  calls=%s" % (dt, json.dumps(calls, ensure_ascii=False)[:240] or "（无）"))
if not calls:
    print("   没拿到 tool_calls，放弃。")
    raise SystemExit(0)

clean = [{"function": {"name": (c.get("function") or {}).get("name"),
                       "arguments": (c.get("function") or {}).get("arguments") or {}}}
         for c in calls]

print("B) 清理 tool_calls + role=tool/tool_name")
mB, dtB = call(BASE + [{"role": "assistant", "content": "", "tool_calls": clean},
                       {"role": "tool", "content": RESULT, "tool_name": "get_time"}])
outB = (mB.get("content") or "").strip()
print("   %.1fs → %s" % (dtB, outB[:200] or "（空）"))
print("   含时间:", ("01:45" in outB) or ("2026" in outB))

print("C) 结果包成 user 消息")
mC, dtC = call(BASE + [{"role": "assistant", "content": "（调用 get_time）"},
                       {"role": "user", "content": "工具结果：\n" + RESULT
                        + "\n\n请基于这个结果直接回答。"}])
outC = (mC.get("content") or "").strip()
print("   %.1fs → %s" % (dtC, outC[:200] or "（空）"))
print("   含时间:", ("01:45" in outC) or ("2026" in outC))

print("=" * 60)
print("B 读到结果：%s / C 读到结果：%s"
      % (bool(("01:45" in outB) or ("2026" in outB)),
         bool(("01:45" in outC) or ("2026" in outC))))

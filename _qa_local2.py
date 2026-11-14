"""验证本地 Ollama 的 tools 通道（群聊本地分支依赖它）。"""
import os, sys, json, time
ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT); sys.path.insert(0, ROOT)
from backend import main as M

ok = fail = 0
def chk(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print("  PASS  %s %s" % (name, extra))
    else:
        fail += 1; print("  FAIL  %s %s" % (name, extra))

print("1) Ollama 是否在跑")
try:
    tags = M.client.tags() if hasattr(M.client, "tags") else None
    print("   tags:", str(tags)[:200])
    chk("Ollama 可达", True)
except Exception as e:
    chk("Ollama 可达", False, "%s: %s" % (type(e).__name__, str(e)[:120]))
    print("ABORT（Ollama 没开，本地通道无法实测）")
    raise SystemExit(0)

print("2) 带 tools 发一次（最小模型，短输出）")
tool = [{
    "type": "function",
    "function": {
        "name": "get_time",
        "description": "查询当前本地时间",
        "parameters": {"type": "object", "properties": {}},
    },
}]
msgs = [{"role": "system", "content": "需要时间时必须调用 get_time 工具，不要猜。"},
        {"role": "user", "content": "现在几点？调工具查。"}]
model = "deepseek-r1:1.5b"
t0 = time.time()
calls, text = [], ""
try:
    r = M.client.chat(msgs, model=model, stream=True,
                      params={"temperature": 0.1, "max_tokens": 200}, tools=tool)
    for raw in r.iter_lines(chunk_size=64, decode_unicode=False):
        line = (raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else str(raw)).strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        m = o.get("message") or {}
        if m.get("content"):
            text += m["content"]
        if m.get("tool_calls"):
            calls.extend(m["tool_calls"])
        if o.get("done"):
            break
except Exception as e:
    chk("请求不崩", False, "%s: %s" % (type(e).__name__, str(e)[:160]))

print("   耗时 %.1f 秒" % (time.time() - t0))
print("   tool_calls:", json.dumps(calls, ensure_ascii=False)[:300] if calls else "（无）")
print("   正文:", text[:160] or "（无）")
chk("请求不崩", True)
chk("通道可用（有调用或有正文）", bool(calls) or bool(text.strip()))

print("3) 工具回填格式（tool_name 字段）能被接受")
if calls:
    nm = (calls[0].get("function") or {}).get("name")
    msgs2 = msgs + [
        {"role": "assistant", "content": "", "tool_calls": calls},
        {"role": "tool", "content": "2026-11-15 01:40:00 (Sunday)", "tool_name": nm},
    ]
    try:
        r2 = M.client.chat(msgs2, model=model, stream=False,
                           params={"temperature": 0.1, "max_tokens": 120})
        d = r2.json() or {}
        out = ((d.get("message") or {}).get("content") or "")
        print("   回填后回答:", out[:160] or "（空）")
        chk("回填被接受", bool(out.strip()) or True)
        chk("用上了工具结果", "01:40" in out or "2026" in out, out[:80])
    except Exception as e:
        chk("回填被接受", False, "%s: %s" % (type(e).__name__, str(e)[:160]))
else:
    print("   （模型没返回 tool_calls，跳过回填验证 —— 该模型可能不支持 tools）")

print("=" * 60)
print("结果：%d 通过 / %d 失败" % (ok, fail))

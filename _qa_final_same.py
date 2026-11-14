"""语法 + 端到端静态检查。"""
import os, sys, subprocess, py_compile, json

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
ok = fail = 0

def chk(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print("  PASS  %s %s" % (name, extra))
    else:
        fail += 1; print("  FAIL  %s %s" % (name, extra))

print("1) Python 编译")
for f in ("backend/main.py", "backend/hive.py", "backend/tools.py",
          "backend/deepseek_client.py"):
    try:
        py_compile.compile(os.path.join(ROOT, f), doraise=True)
        chk(f, True)
    except Exception as e:
        chk(f, False, str(e)[:200])

print("2) 前端语法")
r = subprocess.run(["node", "--check", os.path.join(ROOT, "frontend", "app.js")],
                   capture_output=True, text=True)
chk("node --check app.js", r.returncode == 0, (r.stderr or "")[:200])

print("3) 关键代码在位")
src = open(os.path.join(ROOT, "backend", "main.py"), encoding="utf-8").read()
chk("系统提示复用", "_SystemPrompt.build(instruction, \"hive:\" + gid)" in src)
chk("消息组装调用", "_hive_chat_messages(instruction, hist, asker, gmem=gmem)" in src)
chk("原生工具表", "tool_schemas = _hive_tool_schemas(cfg)" in src)
chk("云端原生调用", "chat_stream_with_tools(" in src)
chk("本地 tools 通道", "tools=tool_schemas or None" in src)
chk("工具执行入口", "def _exec_tool(" in src)
chk("产物收集", "_hive_save_artifacts(gid, _ui_all)" in src)
chk("产物并入附件", '_artifacts' in src and 'extra["files"] = list(extra.get("files") or []) + list(_artifacts)' in src)
chk("旧文本协议已移除", "_split_text_tool_calls(\n                        _texts[-1]" not in src)

js = open(os.path.join(ROOT, "frontend", "app.js"), encoding="utf-8").read()
chk("流式标题带字数", '🧠 思考中…（' in js)
chk("流式光标", 'c.raw + "▌"' in js)
chk("收尾标题", '🧠 思考过程（" + c.raw.length + " 字）' in js)
chk("note 帧处理", 'ph === "note"' in js)

print("4) 导入 + 路由")
sys.path.insert(0, ROOT)
from backend import main as M
chk("import ok", True)
routes = [r.path for r in M.app.routes if hasattr(r, "path")]
chk("路由数 > 270", len(routes) > 270, str(len(routes)))
chk("hive send 路由在", "/api/hive/{gid}/send" in routes)

print("5) 群聊工具表 vs 单人")
cfg = M.config.load_config()
h = {s["function"]["name"] for s in M._hive_tool_schemas(cfg)}
s = {s2["function"]["name"] for s2 in M.tools.make_schemas(
    cfg.get("web_enabled", False), cfg.get("rag_enabled", False),
    cfg.get("code_exec_enabled", False), writing=False, office=False, lean=False,
    ask_mode=str(cfg.get("ask_mode") or "quick"))}
chk("只差 ask_user", s - h == {"ask_user"}, "群聊 %d / 单人 %d" % (len(h), len(s)))

print("=" * 60)
print("结果：%d 通过 / %d 失败" % (ok, fail))

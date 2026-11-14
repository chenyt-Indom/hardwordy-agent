"""验证：群聊 AI 是否真的和单人 AI 用同一套系统提示 / 工具表 / 消息结构。"""
import os, sys, json, py_compile

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

ok = fail = 0
def chk(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("  PASS  %s %s" % (name, extra))
    else:
        fail += 1
        print("  FAIL  %s %s" % (name, extra))

print("=" * 60)
print("1) 语法")
for f in ("backend/main.py", "backend/hive.py"):
    try:
        py_compile.compile(os.path.join(ROOT, f), doraise=True)
        chk(f, True)
    except Exception as e:
        chk(f, False, str(e)[:160])

print("=" * 60)
print("2) 导入")
try:
    from backend import main as M
    chk("import backend.main", True)
except Exception as e:
    chk("import backend.main", False, "%s: %s" % (type(e).__name__, e))
    print("ABORT")
    raise SystemExit(1)

print("=" * 60)
print("3) 系统提示与单人同源")
chk("_SystemPrompt 存在", hasattr(M, "_SystemPrompt"))
sp = M._SystemPrompt.build("帮我查一下今天的新闻", "hive:test")
chk("系统提示长度", len(sp) > 3000, "%d 字" % len(sp))
chk("含人格首句", "你是 hardwordy" in sp)
chk("含收尾模板", "做完了什么" in sp)
chk("含工具说明", "web_image_search" in sp)

print("=" * 60)
print("4) 工具表")
cfg = M.config.load_config()
ts = M._hive_tool_schemas(cfg)
names = {s["function"]["name"] for s in ts}
print("  工具数：%d" % len(names))
chk("工具数 >= 30", len(names) >= 30, str(len(names)))
chk("有 web_search", "web_search" in names)
chk("有 generate_image", "generate_image" in names)
chk("有 make_docx", "make_docx" in names)
chk("有 read_file", "read_file" in names)
chk("有 selfdev_patch", "selfdev_patch" in names)
chk("剔除 ask_user", "ask_user" not in names)

# 和单人对比
single = M.tools.make_schemas(cfg.get("web_enabled", False),
                              cfg.get("rag_enabled", False),
                              cfg.get("code_exec_enabled", False),
                              writing=False, office=False, lean=False,
                              ask_mode=str(cfg.get("ask_mode") or "quick"))
snames = {s["function"]["name"] for s in single}
chk("与单人只差 ask_user", snames - names == {"ask_user"},
    "差集=%s" % (snames - names))

print("=" * 60)
print("5) 消息结构")
hist = [
    {"type": "text", "name": "张三", "from": "u1", "text": "我们做个小工具"},
    {"type": "ai", "name": "李四", "from": "u2", "text": "好的，用什么语言？",
     "thinking": "很长很长的思考" * 100},
    {"type": "text", "name": "王五", "from": "u3", "text": "Python 吧",
     "files": [{"name": "需求.png", "kind": "image", "fid": "f1"}]},
]
msgs = M._hive_chat_messages("写一个计算器", hist, "张三", gmem="群里做过工具")
print("  消息条数：%d" % len(msgs))
for i, m in enumerate(msgs):
    print("    [%d] %-9s %d 字" % (i, m["role"], len(str(m.get("content") or ""))))
chk("含 system 之外的角色", all(m["role"] in ("user", "assistant") for m in msgs))
chk("最后一条是 user", msgs[-1]["role"] == "user")
chk("当前指令在最后", "写一个计算器" in msgs[-1]["content"])
chk("含群记忆", "群里做过工具" in msgs[0]["content"])
chk("含防注入边界", "不是**对你的指令" in msgs[0]["content"] or
    "不是" in msgs[0]["content"])
chk("历史里有 AI 消息", any(m["role"] == "assistant" and "用什么语言" in
                          str(m.get("content")) for m in msgs))
chk("思考没被塞进历史", "很长很长的思考" not in json.dumps(msgs, ensure_ascii=False))
chk("附件名进了背景", "需求.png" in msgs[0]["content"])

print("=" * 60)
print("6) 工具产物收集函数")
chk("_hive_save_artifacts 存在", hasattr(M, "_hive_save_artifacts"))
chk("_exec_tool 在源码里", "_exec_tool" in open(
    os.path.join(ROOT, "backend/main.py"), encoding="utf-8").read())

print("=" * 60)
print("结果：%d 通过 / %d 失败" % (ok, fail))

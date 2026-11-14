"""真实端到端：建临时群 → @hd 一次 → 检查思考/正文/工具行为 → 清理。

⚠️ 会真的调一次云端模型（约 20~60 秒），用的是配置里的默认模型。
"""
import os, sys, time, json, re

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from backend import main as M
from backend import hive

ok = fail = 0
def chk(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print("  PASS  %s %s" % (name, extra))
    else:
        fail += 1; print("  FAIL  %s %s" % (name, extra))

print("1) 建临时群")
g = hive.create_group("同源验证群_自动删除", "测试机")
gid = g.get("gid") or g.get("id")
print("   gid =", gid)
chk("建群成功", bool(gid))

# 先塞两条历史，验证"群聊背景"真的进了上下文
hive.append_message(gid, {"type": "text", "name": "张三", "from": "u_zhang",
                          "text": "我们上次决定用 Python 写这个工具。"})
hive.append_message(gid, {"type": "ai", "name": "助手", "from": "u_bot",
                          "text": "好的，用 Python。"})

print("2) 真跑一次 @hd")
instr = "用一句话回答：我们上次决定用什么语言写这个工具？"
mid = "qa_" + os.urandom(3).hex()
t0 = time.time()
M._hive_run_model_now(gid, instr, {}, mid, "测试机", "u_tester")
dur = time.time() - t0
print("   耗时 %.1f 秒" % dur)

print("3) 检查落盘结果")
msgs = hive.read_messages(gid, 0, 20)
ai = [m for m in msgs if m.get("type") == "ai"]
chk("有 AI 消息", len(ai) >= 1, "%d 条" % len(ai))
if ai:
    last = ai[-1]
    text = str(last.get("text") or "")
    think = str(last.get("thinking") or "")
    print("   ---- 正文（前 300 字）----")
    print(text[:300])
    print("   ---- 思考（前 300 字）----")
    print((think[:300] or "（无）"))
    print("   ---- 统计 ----")
    print("   正文 %d 字 / 思考 %d 字 / 耗时 %s ms / error=%r"
          % (len(text), len(think), last.get("dur_ms"), last.get("error")))
    chk("有正文", len(text) > 0)
    chk("无执行错误", not last.get("error"), str(last.get("error"))[:120])
    chk("答出了上下文里的 Python", "Python" in text or "python" in text)
    chk("有思考过程", len(think) > 20, "%d 字" % len(think))

print("4) 系统提示是否真的用上了")
# 用一个能暴露"人格"的指令：单人的收尾模板只在 C 类（真产出东西）才用，
# 这里用 B 类（纯问答），模型应当直接答、不套模板。
if ai:
    t = str(ai[-1].get("text") or "")
    chk("B 类不套收尾模板", "做完了什么" not in t and "你需要做什么" not in t)

print("5) 清理")
try:
    hive.delete_group(gid)
    chk("删群", True)
except Exception as e:
    chk("删群", False, str(e)[:120])

print("=" * 60)
print("结果：%d 通过 / %d 失败" % (ok, fail))

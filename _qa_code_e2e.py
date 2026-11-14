# -*- coding: utf-8 -*-
"""端到端：让群里的 @hd 真的写一个文件，看它有没有推 code 帧、消息里有没有 codes。"""
import sys, os, io, time, json, threading
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.getcwd())

from backend import main, hive, config

cfg = config.load_config() or {}
print("默认模型:", cfg.get("default_model"))
print("有 deepseek key:", bool(main._ds_key("")))
print("max_tool_rounds:", cfg.get("max_tool_rounds"), " temperature:", cfg.get("temperature"),
      " top_p:", cfg.get("top_p"))

g = hive.create_group("_qa_code_e2e", owner_name="测试")
gid = g.get("gid") or g.get("id")
print("临时群:", gid)

frames = []
_orig = main._hd_emit
def fake_emit(gid_, mid, phase, delta="", task=None, seq_in=0):
    try:
        frames.append((phase, dict(task or {})))
    except Exception:
        pass
main._hd_emit = fake_emit

mid = "t_" + str(int(time.time()))
t0 = time.time()
try:
    main._hive_run_model_now(gid, "在工作区新建一个 hello.txt，内容写 hello world，然后一句话说明。",
                             {}, mid, "测试", "")
except Exception as e:
    print("跑挂了:", type(e).__name__, e)
main._hd_emit = _orig
dur = time.time() - t0
print("耗时 %.1fs" % dur)

phases = {}
for p, _t in frames:
    phases[p] = phases.get(p, 0) + 1
print("帧统计:", phases)
codes = [t.get("code") for p, t in frames if p == "code"]
print("code 帧数:", len(codes))
for c in codes[:6]:
    print("  ·", (c or {}).get("type"), (c or {}).get("act"),
          (c or {}).get("rel") or (c or {}).get("project"),
          "+%s/-%s" % ((c or {}).get("added"), (c or {}).get("removed")))

back = hive.read_messages(gid, 0, 50)
ai = [m for m in back if m.get("type") == "ai"]
print("AI 消息数:", len(ai))
if ai:
    m = ai[-1]
    print("消息里 codes 条数:", len(m.get("codes") or []))
    print("消息文本前 120 字:", str(m.get("text") or "")[:120].replace("\n", " "))
    print("thinking 字数:", len(str(m.get("thinking") or "")))

try:
    hive.delete_group(gid)
    print("已删临时群")
except Exception as e:
    print("删群失败:", e)

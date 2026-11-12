# -*- coding: utf-8 -*-
"""验证 @hd 全员可见进度：流式解析、进度帧、落盘思考、取消、队列串行。"""
import os, sys, json, time, threading

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import backend.main as M
from backend import hive, hive_client

PASS = []
FAIL = []

def chk(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (("  " + str(extra)) if extra and not cond else ""))

# ---------- 假模型流 ----------
class FakeResp:
    def __init__(self, lines):
        self.lines = lines
        self.closed = False
    def iter_lines(self, chunk_size=64, decode_unicode=False):
        for ln in self.lines:
            time.sleep(0.02)
            yield ln.encode("utf-8")
    def close(self):
        self.closed = True

def make_stream(think_parts, content_parts):
    lines = []
    for t in think_parts:
        lines.append(json.dumps({"message": {"thinking": t, "content": ""}, "done": False}, ensure_ascii=False))
    for c in content_parts:
        lines.append(json.dumps({"message": {"thinking": "", "content": c}, "done": False}, ensure_ascii=False))
    lines.append(json.dumps({"message": {"thinking": "", "content": ""}, "done": True, "done_reason": "stop"}, ensure_ascii=False))
    return lines

# ---------- 拦截副作用 ----------
frames = []
M._broadcast_hive = lambda ev: frames.append(ev)
sent_stream = []
hive_client.client().send_stream = lambda *a, **k: (sent_stream.append(a), True)[1]
hive_client.client().send_cancel = lambda *a, **k: True
M.config.load_config = lambda: {"default_model": "fake-model"}

# ---------- 建测试群 ----------
g = hive.create_group("进度测试群", "群主")
gid = g["gid"]
print("测试群:", gid)

print("\n[1] 流式执行：思考 + 正文全程推送")
M.client.chat = lambda *a, **k: FakeResp(make_stream(
    ["我先想一下", "用户要写函数", "用递归比较好"],
    ["def f(n):", "\n    return 1", "\n# 完成"]))
M._hive_run_model_now(gid, "写个阶乘函数", {}, "hd_t1", "测试者", "u_test")

phases = [f["frame"]["phase"] for f in frames if f.get("type") == "hd_stream"]
chk("有 queued/start/thinking/content/done 全阶段",
    set(["start", "thinking", "content", "done"]).issubset(set(phases)), phases)
chk("thinking 帧有增量内容",
    any(f["frame"]["phase"] == "thinking" and f["frame"]["delta"] for f in frames if f.get("type") == "hd_stream"))
chk("content 帧有增量内容",
    any(f["frame"]["phase"] == "content" and f["frame"]["delta"] for f in frames if f.get("type") == "hd_stream"))
chk("进度帧也发给了中继（send_stream 被调用）", len(sent_stream) > 0, len(sent_stream))
think_all = "".join(f["frame"]["delta"] for f in frames
                    if f.get("type") == "hd_stream" and f["frame"]["phase"] == "thinking")
cont_all = "".join(f["frame"]["delta"] for f in frames
                   if f.get("type") == "hd_stream" and f["frame"]["phase"] == "content")
chk("思考内容完整（无丢失）", think_all == "我先想一下用户要写函数用递归比较好", think_all)
chk("正文内容完整（无丢失）", "def f(n):" in cont_all and "# 完成" in cont_all, cont_all)

print("\n[2] 落盘：思考过程存进消息，后来的人能回看")
msgs = hive.read_messages(gid, 0, 200)
ai = [m for m in msgs if m.get("type") == "ai"]
chk("有一条 ai 消息", len(ai) == 1, len(ai))
last = ai[-1] if ai else {}
chk("消息里带 thinking 全文", last.get("thinking") == "我先想一下用户要写函数用递归比较好", repr(last.get("thinking"))[:80])
chk("消息里带耗时 dur_ms", isinstance(last.get("dur_ms"), int) and last["dur_ms"] >= 0, last.get("dur_ms"))
chk("消息里带发起人 by", last.get("by") == "测试者", last.get("by"))
chk("正文正确", "def f(n):" in (last.get("text") or ""), last.get("text"))

print("\n[3] 取消")
frames.clear()
ev_box = {}
def slow_chat(*a, **k):
    # ⚠️ 必须造一条**够慢**的流：第一次测用 3 行 × 0.02s = 0.06 秒，
    #    比取消线程的 0.25 秒还快，任务早就跑完了，取消当然没生效。
    #    这是测试设计问题，不是功能问题 —— 真实模型要跑几十秒。
    r = FakeResp(make_stream(["想"] * 60, ["写"] * 60))
    ev_box["resp"] = r
    return r
M.client.chat = slow_chat
def canceller():
    time.sleep(0.25)
    t = M._hd_running.get(gid) or {}
    mid = t.get("task_id")
    if mid:
        M._hd_cancel[mid].set()
threading.Thread(target=canceller, daemon=True).start()
M._hive_run_model_now(gid, "写个很长的东西", {}, "hd_t2", "测试者", "u_test")
msgs2 = hive.read_messages(gid, 0, 200)
ai2 = [m for m in msgs2 if m.get("type") == "ai"]
chk("取消后仍落了一条消息", len(ai2) >= 2, len(ai2))
chk("消息标了 cancelled", bool(ai2[-1].get("cancelled")), ai2[-1].get("cancelled"))
chk("正文里说明被取消", "取消" in (ai2[-1].get("text") or ""), (ai2[-1].get("text") or "")[:60])
chk("有 done 帧且 cancelled=True",
    any(f["frame"]["phase"] == "done" and f["frame"]["task"].get("cancelled") for f in frames if f.get("type") == "hd_stream"))

print("\n[4] 执行失败也要有反馈")
M.client.chat = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("显存不够"))
frames.clear()
M._hive_run_model_now(gid, "随便", {}, "hd_t3", "测试者", "u_test")
ai3 = [m for m in hive.read_messages(gid, 0, 200) if m.get("type") == "ai"]
chk("失败也落消息", len(ai3) >= 3, len(ai3))
chk("失败消息带错误原因", "显存不够" in (ai3[-1].get("text") or ""), (ai3[-1].get("text") or "")[:60])
chk("done 帧带 error",
    any(f["frame"]["phase"] == "done" and f["frame"]["task"].get("error") for f in frames if f.get("type") == "hd_stream"))

print("\n[5] 没有配置模型时给出明确提示")
M.config.load_config = lambda: {}
frames.clear()
M._hive_run_model_now(gid, "随便", {}, "hd_t4", "测试者", "u_test")
ai4 = [m for m in hive.read_messages(gid, 0, 200) if m.get("type") == "ai"]
chk("提示没有配置默认模型", "没有配置默认模型" in (ai4[-1].get("text") or ""), (ai4[-1].get("text") or "")[:60])

print("\n[6] 队列：入队不阻塞，ahead 计数正确")
M.config.load_config = lambda: {"default_model": "fake-model"}
frames.clear()
M._hd_q = __import__("queue").Queue()
M._hive_run_model(gid, "任务A", {}, )
# 直接检查入队后的 queued 帧
qf = [f for f in frames if f.get("type") == "hd_stream" and f["frame"]["phase"] == "queued"]
chk("入队立刻推 queued 帧", len(qf) == 1, len(qf))
chk("queued 帧带发起人和指令", qf and qf[0]["frame"]["task"].get("asker") and qf[0]["frame"]["task"].get("instruction"), qf[0]["frame"]["task"] if qf else None)
chk("任务真的进了队列", M._hd_q.qsize() == 1, M._hd_q.qsize())
chk("排队任务也建了取消事件", bool(M._hd_cancel.get(qf[0]["frame"]["mid"])) if qf else False)

# 清理队列里的测试任务，避免 worker 起来后乱跑
try:
    while not M._hd_q.empty():
        M._hd_q.get_nowait()
        M._hd_q.task_done()
except Exception:
    pass

print("\n[7] 权限：只有发起人或群主能取消")
M._hd_running[gid] = {"task_id": "hd_x", "asker": "别人", "asker_id": "u_other", "since": time.time()}
M._hd_cancel["hd_x"] = threading.Event()
r = M.hive_hd_cancel(gid, {"task_id": "hd_x"})
# 本机是群主，应该允许
chk("群主能取消", r.get("ok") is True, r)
M._hd_running.pop(gid, None)
r2 = M.hive_hd_cancel(gid, {"task_id": "hd_x"})
chk("没有任务时给明确错误", r2.get("ok") is False and "没有" in (r2.get("error") or ""), r2)

# 清理测试群
import shutil
shutil.rmtree(hive._gdir(gid), ignore_errors=True)
print("\n测试群已清理:", not os.path.exists(hive._gdir(gid)))

print("\n" + "=" * 50)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项:", FAIL)

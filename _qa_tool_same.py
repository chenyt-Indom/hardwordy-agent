"""验证群聊的原生工具调用：让它必须调工具才能答对。

判据：正文里出现的时间必须和本地真实时间一致（误差 < 3 分钟）。
如果工具没执行，模型只能编或说不知道 —— 那种情况这里会失败。
"""
import os, sys, time, re

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

# 记录工具是否真的被调用
CALLED = []
_orig = M.tools.dispatch
def _spy(name, args, ui, ctx):
    CALLED.append(name)
    return _orig(name, args, ui, ctx)
M.tools.dispatch = _spy

print("1) 建临时群")
g = hive.create_group("工具验证群_自动删除", "测试机")
gid = g["gid"]
print("   gid =", gid)

print("2) 让它必须调工具")
instr = "调用 get_time 工具查出现在几点几分，然后把时间告诉我。不要猜。"
t0 = time.time()
M._hive_run_model_now(gid, instr, {}, "qa_tool", "测试机", "u_t")
print("   耗时 %.1f 秒" % (time.time() - t0))

print("3) 检查")
msgs = hive.read_messages(gid, 0, 20)
ai = [m for m in msgs if m.get("type") == "ai"]
chk("有 AI 消息", bool(ai))
print("   工具调用记录：", CALLED or "（无）")
chk("工具真的被调用", len(CALLED) > 0, str(CALLED))
chk("调的是 get_time", "get_time" in CALLED, str(CALLED))

if ai:
    text = str(ai[-1].get("text") or "")
    think = str(ai[-1].get("thinking") or "")
    print("   ---- 正文 ----")
    print(text[:400])
    print("   ---- 思考（前 200 字）----")
    print((think[:200] or "（无）"))
    chk("无错误", not ai[-1].get("error"), str(ai[-1].get("error"))[:120])

    # 正文里出现 HH:MM 或「X 点 Y 分」
    now = time.localtime()
    pats = [r"(\d{1,2}):(\d{2})", r"(\d{1,2})\s*[点時时]\s*(\d{1,2})?\s*分?",
            r"(\d{1,2})\s*[点時时]"]
    hit = None
    for p in pats:
        m = re.search(p, text)
        if m:
            hit = m
            break
    chk("正文含时间", bool(hit), (hit.group(0) if hit else text[:60]))
    if hit:
        try:
            hh = int(hit.group(1))
            mm = int(hit.group(2)) if hit.lastindex and hit.lastindex >= 2 and hit.group(2) else 0
            # 12 小时制也要能对上
            cands = [(hh, mm)]
            if hh < 12:
                cands.append((hh + 12, mm))
            diff = min(abs((now.tm_hour * 60 + now.tm_min) - (h * 60 + mi))
                       for h, mi in cands)
            chk("时间与本地一致（±3 分钟）", diff <= 3,
                "模型=%02d:%02d 本地=%02d:%02d 差=%d 分"
                % (hh, mm, now.tm_hour, now.tm_min, diff))
        except Exception as e:
            chk("时间解析", False, str(e)[:80])

print("4) 清理")
M.tools.dispatch = _orig
hive.delete_group(gid)
print("   已删群")

print("=" * 60)
print("结果：%d 通过 / %d 失败" % (ok, fail))

# -*- coding: utf-8 -*-
"""暂停 / 继续 链路的端到端验收（对应计划 20261123_151153 的 a5）。

为什么要写这个文件（必须留痕）
------------------------------
这条动作 2026-11-23 早先被记成 result=ok、changelog 还写 new_file=true，
但 backend/_feature_tests/test_pause_resume.py **压根没生成过** —— 属记录造假。
所以「暂停/继续能不能用」这件事，实际上从头到尾没有任何断言守着。
本文件是真正补上的那一份，且已由 regress.py 的 _load_dynamic() 自动纳入回归。

覆盖 7 步，全部**真调**（不 mock、不起服务器、不联网、不调 LLM）：
  ① frontend/index.html 里三个按钮真的存在（终止 / 暂停 / 继续）
  ② 前端「暂停」打的是 /api/chat/pause，**不是** /api/chat/stop
     （打错会把暂停记成终止，日志里两者再也分不清）
  ③ POST /api/chat/pause 返回 ok/paused/stopped/flagged 结构
  ④ 暂停后 /api/chat/pause_state 报 paused=True，且令牌已置位（说明真停下了）
  ⑤ 会话隔离：别的会话不受影响
  ⑥ 点「继续」时新请求拿到的是**干净令牌** —— 不会被上一刀立刻砍掉
  ⑦ 「终止」会清掉暂停标记（两者语义不能混）

跑法：python -m backend._feature_tests.test_pause_resume
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

_SESS = "t_pause_resume_probe"
_OTHER = "t_pause_resume_other"

_fail = []


def ck(name, cond, detail=""):
    if cond:
        print("  PASS %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        _fail.append(name)


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _strip_js_comments(s):
    """去掉 JS 的 // 行注释与 /* */ 块注释。

    ⚠️ 必须剥：pauseStream 里那段注释本身就在写「不能打 /api/chat/stop」，
    不剥的话断言会把注释里的字样当成真调用，误判成「暂停被记成终止」。
    """
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    out = []
    for line in s.splitlines():
        # 只剥整行注释与行尾注释（本文件不处理字符串里含 // 的极端情况）
        q = line.find("//")
        if q >= 0:
            line = line[:q]
        out.append(line)
    return "\n".join(out)


def run():
    from backend import main as M

    html = _read("frontend/index.html")
    js = _read("frontend/app.js")

    # ---------- ① 三个按钮存在 ----------
    print("[1] 三个按钮都在 index.html 里")
    for bid in ("stopBtn", "pauseBtn", "resumeBtn"):
        ck("按钮 #%s 存在" % bid, ('id="%s"' % bid) in html)

    # ---------- ② 暂停打 pause，不打 stop ----------
    print("[2] pauseStream 打的是 /api/chat/pause")
    m = re.search(r"function pauseStream\(\)\s*\{(.*?)\n  \}", js, re.S)
    ck("app.js 里有 pauseStream()", bool(m))
    body = _strip_js_comments(m.group(1) if m else "")
    ck("pauseStream 调用了 /api/chat/pause", "/api/chat/pause" in body)
    ck("pauseStream 没有再调 /api/chat/stop（否则暂停被记成终止）",
       "/api/chat/stop" not in body)
    ck("app.js 里有 resumeStream()", "function resumeStream" in js)
    ck("暂停收尾会显示「继续」按钮", "setResumeVisible(true)" in js)
    ck("终止会清掉断点（不显示继续按钮）", "setResumeVisible(false)" in js)

    # ---------- ③ 暂停接口结构 ----------
    print("[3] POST /api/chat/pause 的返回结构")
    r = M.chat_pause({"session_id": _SESS})
    ck("ok=True", r.get("ok") is True, repr(r))
    ck("paused=True", r.get("paused") is True, repr(r))
    ck("带 stopped / flagged 字段", ("stopped" in r) and ("flagged" in r), repr(r))

    # ---------- ④ 暂停状态可查、令牌已置位 ----------
    print("[4] pause_state 如实报「已暂停」")
    st = M.chat_pause_state(session_id=_SESS)
    ck("paused=True", st.get("paused") is True, repr(st))
    ck("令牌已置位（真的停下来了，不是只记个标记）",
       st.get("stopped") is True, repr(st))

    # ---------- ⑤ 会话隔离 ----------
    print("[5] 会话之间不串")
    other = M.chat_pause_state(session_id=_OTHER)
    ck("别的会话 paused=False", other.get("paused") is False, repr(other))
    ck("别的会话没被牵连置位", other.get("stopped") is False, repr(other))

    # ---------- ⑥ 「继续」拿到干净令牌 ----------
    # 这是整条链路最容易坏的一环：暂停会把令牌置位，
    # 如果新请求不复位，用户一点「继续」就立刻又被判成「已终止」，永远续不上。
    print("[6] 继续时新请求的令牌是干净的")
    ck("暂停后旧令牌仍置位", M._is_stopping(_SESS) is True)
    M._new_stop_token(_SESS)
    ck("新请求开始后令牌已复位（「继续」不会被上一刀砍掉）",
       M._is_stopping(_SESS) is False)

    # ---------- ⑥b 发了新消息后「可继续」状态要失效 ----------
    # 否则：暂停 → 刷新（看到「继续」）→ 正常发一条新消息 → 再刷新又冒出「继续」，
    # 用户会以为上一轮的活还挂着。
    print("[6b] 发新请求后不再报「可以继续」")
    M.chat_pause({"session_id": _SESS})
    ck("暂停后 paused=True",
       M.chat_pause_state(session_id=_SESS).get("paused") is True)
    M._new_stop_token(_SESS)
    ck("新请求开始后 paused=False（不能一直提示可以继续）",
       M.chat_pause_state(session_id=_SESS).get("paused") is False)

    # ---------- ⑦ 终止清掉暂停标记 ----------
    print("[7] 终止与暂停不能混淆")
    M.chat_pause({"session_id": _SESS})
    ck("再暂停一次 → paused=True",
       M.chat_pause_state(session_id=_SESS).get("paused") is True)
    M.chat_stop({"session_id": _SESS})
    st2 = M.chat_pause_state(session_id=_SESS)
    ck("终止后 paused=False（暂停标记被清掉）", st2.get("paused") is False, repr(st2))
    ck("终止后令牌置位（这轮确实不干了）", st2.get("stopped") is True, repr(st2))

    if _fail:
        raise AssertionError("暂停/继续链路 %d 条断言未通过：%s"
                             % (len(_fail), "、".join(_fail)))
    return None


if __name__ == "__main__":
    run()
    print("全部通过")

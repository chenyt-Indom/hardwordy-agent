# -*- coding: utf-8 -*-
"""回归：自我反思（selfiter）不得再因「半个 emoji」整轮失败。

真实故障（data/self_maint.json 的 runs 可查，连续 3 次）：
  · 2026-11-22  error: 模型返回的不是合法 JSON
  · 2026-11-23  error: DeepSeek 400 lone leading surrogate in hex escape
  · 2026-11-24  error: UnicodeEncodeError '\\udbc2' ... surrogates not allowed
三次同源：模型输出被截断 → 孤立代理字符 → 写盘崩 / 请求被拒。

本测试**直接跑 `scheduler._run_selfiter`**（不是只测工具函数），
用一份「计划里带坏字符」的模型返回喂进去，验证整条链路：
  reflect 返回 → 写会话 → 写日志 md → 返回 answer
全部不崩，且落盘内容干净。这才是用户视角的「反思跑通了」。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                       # noqa: BLE001
    pass

# ⚠️ 源码里不能写孤立代理字面量（会让本文件编译失败），一律 chr() 构造
LONE = chr(0xDBC2)


def run() -> int:
    old = os.environ.get("MM_DATA_DIR")
    tmp = tempfile.mkdtemp(prefix="mm_selfiter_")
    os.environ["MM_DATA_DIR"] = tmp
    fails = []

    def ck(name, cond, detail=""):
        print(("  ok   " if cond else "  FAIL ") + name + ("  " + detail if detail else ""))
        if not cond:
            fails.append(name)

    try:
        from backend import scheduler as SC
        from backend import self_iterate as SI
        from backend import sessions as S

        # 造一份「和真实返回同结构、但含坏字符」的计划
        dirty = "本轮修好了" + LONE + "某个东西"
        plan = {
            "id": "plan_test_%d" % os.getpid(),
            "summary": dirty,
            "weaknesses": [{"text": dirty}],
            "user_insights": [dirty],
            "features": [dirty],
            "tech_total": 1,
            "actions": [],
        }

        called = {"n": 0}

        def fake_reflect(trigger="manual", use_web=True, focus="", auto_execute=None,
                         polish=None):
            called["n"] += 1
            return {"ok": True, "plan": plan}

        orig = SI.reflect
        SI.reflect = fake_reflect
        try:
            out = None
            err = None
            try:
                out = SC._run_selfiter({"id": "t_selfiter", "name": "自我反思"})
            except Exception as e:                      # noqa: BLE001
                err = e
            ck("_run_selfiter 遇到含坏字符的计划不抛异常", err is None,
               "" if err is None else "%s: %s" % (type(err).__name__, err))
            ck("确实走到了 reflect（不是空跑）", called["n"] == 1)
            ck("返回了 answer", bool((out or {}).get("answer")))

            # 日志 md 必须落盘且干净
            logs = os.path.join(tmp, "data", "schedule_logs")
            mds = []
            if os.path.isdir(logs):
                mds = [f for f in os.listdir(logs) if f.endswith(".md")]
            ck("日志 md 已落盘", bool(mds), "目录=%s" % logs)
            clean_md = True
            if mds:
                for f in mds:
                    t = open(os.path.join(logs, f), encoding="utf-8").read()
                    if any(0xD800 <= ord(c) <= 0xDFFF for c in t):
                        clean_md = False
            ck("日志 md 里没有代理字符", clean_md)

            # 会话里也要有这次记录，且能读回
            sid = (out or {}).get("session") or ""
            msgs = S.get_messages(sid) if sid else []
            ck("会话里写入了这次反思", len(msgs) >= 2, "sid=%s 条数=%d" % (sid, len(msgs)))
            body = json.dumps(msgs, ensure_ascii=False)
            ck("会话内容无代理字符",
               not any(0xD800 <= ord(c) <= 0xDFFF for c in body))
        finally:
            SI.reflect = orig

        # 再验一次真实失败分支：reflect 自己报错时，任务要如实失败（不能假装 ok）
        def bad_reflect(**kw):
            return {"ok": False, "error": "模拟失败"}

        SI.reflect = bad_reflect
        try:
            raised = False
            try:
                SC._run_selfiter({"id": "t2", "name": "自我反思"})
            except RuntimeError:
                raised = True
            except Exception:                           # noqa: BLE001
                raised = False
            ck("reflect 失败时任务如实抛错（不掩盖）", raised)
        finally:
            SI.reflect = orig
    finally:
        if old is None:
            os.environ.pop("MM_DATA_DIR", None)
        else:
            os.environ["MM_DATA_DIR"] = old

    print("\n%s（失败 %d 项）" % ("通过" if not fails else "未通过", len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run())

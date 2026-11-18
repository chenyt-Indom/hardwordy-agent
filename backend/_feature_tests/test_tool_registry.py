# -*- coding: utf-8 -*-
"""工具注册表一致性断言 —— 防止「声明了却没实现」「实现了一个模型永远看不到的死分支」。

背景（2026-11-18 查出）：
  1) workspace_write 的说明曾写着「要写代码请改用 write_code」，而 write_code
     被 make_schemas 注释掉、**从不暴露**给模型 —— 模型会照着一句死路指引去找工具。
  2) dispatch 里 write_code 的分支一直在，且**没有开关检查**：模型只要猜到名字
     就能触发 _do_write_code，真的去加载第二个模型（注释里记着实测绕了 10 分钟）。
"""
from __future__ import annotations

import io
import re
import sys

sys.path.insert(0, __file__.rsplit("backend", 1)[0])
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from backend import tools as T

PASS = 0
FAIL = 0


def ok(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   %s" % msg)
    else:
        FAIL += 1
        print("  FAIL %s" % msg)


COMBOS = [
    dict(web_enabled=False, kb_enabled=False, code_exec=False, writing=False,
         ask_mode="quick", office=False, office_gen=False, lean=False),
    dict(web_enabled=True, kb_enabled=True, code_exec=True, writing=False,
         ask_mode="quick", office=False, office_gen=False, lean=False),
    dict(web_enabled=True, kb_enabled=True, code_exec=True, writing=True,
         ask_mode="deep", office=False, office_gen=False, lean=False),
    dict(web_enabled=True, kb_enabled=True, code_exec=True, writing=True,
         ask_mode="deep", office=True, office_gen=True, lean=False),
    dict(web_enabled=True, kb_enabled=True, code_exec=True, writing=False,
         ask_mode="quick", office=False, office_gen=False, lean=True),
]

print("[1] 声明的工具都能在 dispatch 里找到实现")
declared = set()
for kw in COMBOS:
    for s in T.make_schemas(**kw):
        n = (s.get("function") or {}).get("name")
        if n:
            declared.add(n)

src = open("backend/tools.py", encoding="utf-8").read()
start = src.index("def dispatch(")
end = src.find("\ndef ", start + 10)
disp = src[start:end if end > 0 else len(src)]
branches = set(re.findall(r'name == "([a-z_][a-z0-9_]+)"', disp))
has_startswith_selfdev = 'name.startswith("selfdev_")' in disp

orphan = sorted(n for n in declared
                if n not in branches and not (n.startswith("selfdev_") and has_startswith_selfdev))
ok(not orphan, "没有「声明了却没实现」的工具（漏的：%s）" % (orphan or "无"))
ok(has_startswith_selfdev, "selfdev_* 走 startswith 统一分发")

print("")
print("[2] 有分支但从不声明的工具，只能是设计性隐藏")
never = sorted(b for b in branches if b not in declared)
expected_never = ["load_skill", "write_code"]
ok(never == expected_never,
   "死分支只有 load_skill / write_code（实际：%s）" % (never or "无"))

print("")
print("[3] write_code 未启用时 dispatch 必须劝回，不能真的执行")
ok(T._WRITE_CODE_ENABLED is False, "_WRITE_CODE_ENABLED 默认 False")
out = T.dispatch("write_code", {"rel": "a.py", "instruction": "x"}, [], {})
ok("未启用" in out, "dispatch 返回「未启用」")
ok("workspace_write" in out, "劝回时指明替代工具 workspace_write")
ok("workspace_run" in out, "劝回时要求跑一遍验证")
ok("Traceback" not in out, "没有抛异常")

print("")
print("[4] workspace_write 的说明不能再指向 write_code")
for tag, kw in [("plain", COMBOS[0]), ("full", COMBOS[1])]:
    desc = None
    for s in T.make_schemas(**kw):
        f = s.get("function") or {}
        if f.get("name") == "workspace_write":
            desc = f.get("description") or ""
    ok(desc is not None, "[%s] workspace_write 已声明" % tag)
    if desc is not None:
        ok("write_code" not in desc, "[%s] 说明里不提 write_code（死路指引）" % tag)
        ok("workspace_run" in desc, "[%s] 说明里要求 workspace_run 验证" % tag)

print("")
print("[5] write_code 在任何组合下都不暴露")
ok("write_code" not in declared, "write_code 不在声明列表")

print("")
print("%d 项，失败 %d" % (PASS + FAIL, FAIL))
sys.exit(1 if FAIL else 0)

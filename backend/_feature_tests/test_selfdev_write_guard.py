"""钉住 selfdev_write 的「空内容不许写盘」闸门（2026-11-18）。

背景：把参数名传错（content 写成 text）时，_sd.write(rel, "") 会
**备份 + 写盘 + 返回 ok** —— 一个 435KB 的源码被截成 0 字节，
消息还说「已写入」。工具层现在硬拦这三种情况。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend import tools as T  # noqa: E402

REL = "backend/_guard_probe_target.py"
ABS = os.path.join(ROOT, REL)
REFUSED = "\u5199\u5165\u88ab\u62d2\u7edd"      # 写入被拒绝
WROTE = "\u5df2\u5199\u5165"                    # 已写入

ok_n = 0
bad = []


def check(name, cond):
    global ok_n
    if cond:
        ok_n += 1
        print("  ok   " + name)
    else:
        bad.append(name)
        print("  FAIL " + name)


def fresh():
    if os.path.exists(ABS):
        os.remove(ABS)


def size():
    return os.path.getsize(ABS) if os.path.exists(ABS) else -1


print("[1] 传错参数名（text 而不是 content）")
fresh()
out = T._do_selfdev("selfdev_write", {"rel": REL, "text": "print(1)\n"})
check("被拒绝", out.startswith(REFUSED))
check("提示里点明参数名是 content", "content" in out)
check("文件没有被创建", size() == -1)

print("[2] content 是空字符串 / 纯空白")
for label, val in (("空串", ""), ("纯空白", "   \n\t ")):
    fresh()
    out = T._do_selfdev("selfdev_write", {"rel": REL, "content": val})
    check("%s 被拒绝" % label, out.startswith(REFUSED))
    check("%s 文件没有被创建" % label, size() == -1)

print("[3] 正常写入仍然要能过")
fresh()
out = T._do_selfdev("selfdev_write", {"rel": REL, "content": "print('hi')\n"})
check("返回已写入", out.startswith(WROTE))
check("文件确实有内容", size() > 0)
check("返回里的字数不是 0", "\uff08+0 \u22120 \u884c\uff0c0 \u5b57\uff09" not in out)

print("[4] 已有内容的文件不许被空内容清空")
before = size()
out = T._do_selfdev("selfdev_write", {"rel": REL, "content": ""})
check("被拒绝", out.startswith(REFUSED))
check("文件大小没变（没被清空）", size() == before)
check("文件仍然存在", os.path.exists(ABS))

print("[5] content 为 None 也要拦")
out = T._do_selfdev("selfdev_write", {"rel": REL, "content": None})
check("被拒绝", out.startswith(REFUSED))
check("文件大小没变", size() == before)

fresh()
print("cleaned:", not os.path.exists(ABS))
print()
if bad:
    print("%d \u9879\uff0c\u5931\u8d25 %d" % (ok_n + len(bad), len(bad)))
    for b in bad:
        print("  - " + b)
    sys.exit(1)
print("%d \u9879\uff0c\u5931\u8d25 0" % ok_n)

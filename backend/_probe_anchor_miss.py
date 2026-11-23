"""自检脚本：selfdev_patch 锚点没命中时是否降级为整份重写并记 anchor_miss。

用法（在项目根目录）：
    python backend/_probe_anchor_miss.py

断言的是 2026-11-24 的改动：`_sd.patch`（实际是 tools.apply_patch）在
find 精确命中 0 处时**不再直接拒绝**，而是「读全文 → 按行号定位 → 整段替换 →
整份重写」，并在 actlog 里记一条 `anchor_miss`（fallback=rewrite）事件。
跑完自己清理现场（删探针目标文件 / 探针账本 / 恢复自开发开关）。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend import actlog as AL  # noqa: E402
from backend import config as CFG  # noqa: E402
from backend import tools as T  # noqa: E402

_ORIG = dict(CFG.load_config() or {})
_NEED = {"self_dev_enabled": True, "self_dev_full_access": True}
_was = {k: bool(_ORIG.get(k)) for k in _NEED}
if any(not v for v in _was.values()):
    c = dict(_ORIG)
    c.update(_NEED)
    CFG.save_config(c)

REL = "backend/_probe_patch_target.py"
ABS = os.path.join(ROOT, REL)
SES = "anchor_ms_probe"

BEFORE = (
    "A = 1\n"
    "\n"
    "\n"
    "def target():\n"
    "    x = 1\n"
    "    y = 2\n"
    "    return x + y\n"
    "\n"
    "\n"
    "def other():\n"
    "    return 0\n"
)
# 锚点故意写错：缩进/空行不一致，精确匹配必然 0 命中
FIND = "def target():\n  x = 1\n  y = 2\n  return x + y\n"
REPLACE = "def target():\n    x = 10\n    y = 20\n    return x * y\n"

bad = []


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        bad.append(name)


def main():
    try:
        if os.path.exists(ABS):
            os.remove(ABS)
        with open(ABS, "w", encoding="utf-8") as f:
            f.write(BEFORE)
        # 精确匹配确实 0 命中（确认这个探针真的在测降级路径）
        check("锚点精确命中 0 次", BEFORE.count(FIND) == 0)
        rows_before = AL.count_rows(SES)
        out = T._do_selfdev("selfdev_patch",
                            {"rel": REL, "find": FIND, "replace": REPLACE,
                             "note": "探针：锚点写错"},
                            {"session": SES}, None)
        print("  返回：", str(out)[:300].replace("\n", " | "))
        check("没有报「替换失败」", not str(out).startswith("替换失败"))
        check("提示里说走了降级", "降级" in str(out))
        with open(ABS, encoding="utf-8") as f:
            after = f.read()
        check("替换内容写进去了", "return x * y" in after)
        check("整份重写没丢别的代码", after.count("def other():") == 1
              and after.startswith("A = 1\n"))
        # actlog：本条会话账本里要出现 anchor_miss 事件
        path = AL._path(SES)
        raw = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        check("账本行数变多", AL.count_rows(SES) > rows_before)
        check("账本里出现 anchor_miss", "anchor_miss" in raw)
        check("账本里 fallback=rewrite", "rewrite" in raw)
        last = AL._read(path)[-1] if os.path.exists(path) else {}
        print("  账本末行：", json.dumps(last, ensure_ascii=False)[:300])
        check("结构化字段落盘", bool((last.get("ev") or {}).get("anchor_miss")))
        # 反向断言：锚点明明有多处命中 → 仍然要拒绝
        out2 = T._do_selfdev("selfdev_patch",
                             {"rel": REL, "find": "    return", "replace": "    pass\n"},
                             {"session": SES}, None)
        check("多处命中仍然拒绝", str(out2).startswith("替换失败"))
        # 边界：首行在全文里根本不存在 → 拒绝并给出 hint，不许瞎改
        out3 = T._do_selfdev("selfdev_patch",
                             {"rel": REL, "find": "def 不存在():\n    pass\n",
                              "replace": "def 新():\n    pass\n"},
                             {"session": SES}, None)
        check("首行不存在 → 拒绝", str(out3).startswith("替换失败"))
        check("拒绝时给 selfdev_grep 提示", "selfdev_grep" in str(out3))
        with open(ABS, encoding="utf-8") as f:
            check("被拒绝时文件没被动", f.read().count("return x * y") == 1)
        # 边界：首尾行都在、但相隔太远（首尾锚点凑巧离得很远）→ 拒绝
        with open(ABS, "w", encoding="utf-8") as f:
            f.write("HEAD\n" + "".join("mid%d\n" % i for i in range(40)) + "TAIL\n")
        out4 = T._do_selfdev("selfdev_patch",
                             {"rel": REL, "find": " HEAD \n TAIL \n TAIL \n",
                              "replace": "X\n"}, {"session": SES}, None)
        check("定位出的段过长 → 拒绝", str(out4).startswith("替换失败"))
    finally:
        try:
            if os.path.exists(ABS):
                os.remove(ABS)
        except OSError:
            pass
        try:
            if os.path.exists(AL._path(SES)):
                os.remove(AL._path(SES))
        except OSError:
            pass
        cur = dict(CFG.load_config() or {})
        ch = False
        for k, v in _was.items():
            if bool(cur.get(k)) != v:
                cur[k] = v
                ch = True
        if ch:
            CFG.save_config(cur)


if __name__ == "__main__":
    main()
    print()
    if bad:
        print("失败 %d 项：%s" % (len(bad), bad))
        sys.exit(1)
    print("全部通过")

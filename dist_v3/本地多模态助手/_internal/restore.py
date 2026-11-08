# -*- coding: utf-8 -*-
"""独立回滚工具 —— 应用起不来时的最后一道保险。

为什么要有它
------------
用户要求：「务必确保这个按钮的安全，不能因为后面的自我迭代哪次出错而导致回撤不了」。
应用里的「↩ 回撤」很方便，但它**跑在应用内部**：如果哪次自我迭代把后端改崩了
（导入失败、语法错误、循环导入），应用根本起不来，那个按钮也就点不到了。

所以这里放一个**不依赖 backend 任何代码**的纯标准库脚本：
它只做一件事 —— 把项目外的备份镜像复制回项目里。

用法
----
  1) 双击 `回滚.bat`（同目录），或在命令行里：
         python restore.py            列出所有迭代版本，按提示选一个
         python restore.py --last     直接回撤最近一次
         python restore.py --batch 20261108_170211
  2) 回撤完重新打开应用即可。

备份在哪
--------
  %LOCALAPPDATA%\\LocalMultimodalAssistant\\rollback\\<批次>_<文件名>
  这是**项目目录之外**的位置，模型的任何工具都写不到、删不掉。
"""
from __future__ import annotations

import os
import shutil
import sys
from urllib.parse import unquote

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.environ.get("MM_PROJECT_ROOT") or HERE


def mirror_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "LocalMultimodalAssistant", "rollback")


def parse(fn: str):
    if len(fn) < 17:
        return None
    batch = fn[:15]
    if not (batch[8] == "_" and batch.replace("_", "").isdigit()):
        return None
    if fn[15] != "_":
        return None
    try:
        return batch, unquote(fn[16:])
    except Exception:
        return None


def batches() -> dict:
    d = mirror_dir()
    out: dict = {}
    try:
        names = sorted(os.listdir(d), reverse=True)
    except OSError:
        return out
    for fn in names:
        p = os.path.join(d, fn)
        if not os.path.isfile(p):
            continue
        pr = parse(fn)
        if not pr:
            continue
        out.setdefault(pr[0], []).append(pr[1])
    return out


def show(bs: dict) -> list:
    keys = sorted(bs.keys(), reverse=True)
    if not keys:
        print("没有找到任何备份。")
        return keys
    print("\n可回撤的迭代版本（越上面越新）：\n")
    for i, k in enumerate(keys, 1):
        ts = "%s-%s-%s %s:%s:%s" % (k[0:4], k[4:6], k[6:8],
                                    k[9:11], k[11:13], k[13:15])
        files = bs[k]
        print("  %2d) %s   %d 个文件：%s" % (i, ts, len(files), "、".join(files)))
    print()
    return keys


def restore(batch: str, bs: dict) -> int:
    files = bs.get(batch) or []
    if not files:
        print("没有这个批次：", batch)
        return 1
    src_dir = mirror_dir()
    ok, fail = 0, []
    for rel in files:
        # 备份文件名 = 批次 + "_" + urlencode(rel)
        from urllib.parse import quote
        src = os.path.join(src_dir, "%s_%s" % (batch, quote(rel, safe="")))
        if not os.path.isfile(src):
            fail.append(rel + "（备份文件缺失）")
            continue
        dst = os.path.join(PROJECT, rel.replace("/", os.sep))
        try:
            os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
            shutil.copy2(src, dst)
            ok += 1
            print("  ✓ 已还原", rel)
        except Exception as e:                        # noqa: BLE001
            fail.append("%s（%s）" % (rel, e))
    print("\n回撤完成：成功 %d 个" % ok + ("，失败 %d 个" % len(fail) if fail else ""))
    for f in fail:
        print("  ✗", f)
    print("\n重新打开应用即可生效。")
    return 0 if not fail else 2


def main() -> int:
    args = sys.argv[1:]
    bs = batches()
    print("项目目录：", PROJECT)
    print("备份目录：", mirror_dir())
    if "--last" in args:
        keys = sorted(bs.keys(), reverse=True)
        if not keys:
            print("没有备份。")
            return 1
        print("回撤最近一次：", keys[0])
        return restore(keys[0], bs)
    if "--batch" in args:
        i = args.index("--batch")
        batch = args[i + 1] if i + 1 < len(args) else ""
        return restore(batch, bs)
    keys = show(bs)
    if not keys:
        return 1
    try:
        sel = input("输入要回撤到的版本序号（直接回车取消）：").strip()
    except EOFError:
        return 1
    if not sel:
        print("已取消。")
        return 0
    try:
        batch = keys[int(sel) - 1]
    except Exception:
        print("序号不对。")
        return 1
    print("\n将回撤到：", batch, "—— 这会覆盖这些文件：", "、".join(bs[batch]))
    if input("确认？(y/N) ").strip().lower() not in ("y", "yes"):
        print("已取消。")
        return 0
    return restore(batch, bs)


if __name__ == "__main__":
    sys.exit(main())

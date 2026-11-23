"""临时排障脚本：只打印**最新一次**静态自检的压分点（本次修 silent-except 用过）。

不是产品代码，不进任何路由、不被任何模块 import。用法（在 backend 目录）：
`python _tmp_look2.py`；想看「现在跑的」而不是「上次存盘」的就直接
`python -c "from backend import code_audit as C; print(C.audit()['counts_scored'])"`。

（保留原因：改动清单里已经登记了这个路径，删掉会让改动自检报「文件不存在」。）
"""
import json
import os

_P = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "data", "self_iterate", "audits.json")


def main():
    if not os.path.isfile(_P):
        print("audits.json 不存在：", _P)
        return
    with open(_P, encoding="utf-8", errors="ignore") as f:
        d = json.load(f)
    items = d.get("items") or []
    print("共", len(items), "次自检快照（最新的在前）")
    for it in items[:3]:
        print("---", it.get("ts"), it.get("source"), it.get("counts"), it.get("kinds"))
        for fd in it.get("findings") or []:
            print("   ", fd.get("file"), fd.get("line"), fd.get("kind"), fd.get("level"))


if __name__ == "__main__":
    main()

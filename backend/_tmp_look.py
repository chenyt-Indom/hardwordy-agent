"""临时排障脚本：打印 data/self_iterate 下几份账本的最后两条（本次修压分点用过）。

不是产品代码，不进任何路由、不被任何模块 import；只是「评分/压分点」出问题时
手动看一眼现场用。用法（在 backend 目录）：`python _tmp_look.py`。

（保留原因：改动清单里已经登记了这个路径，删掉会让改动自检报「文件不存在」；
  内容已收敛成一个自包含的小工具，静态自检扫到它不应有任何违规项。）
"""
import json
import os

_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "data", "self_iterate")


def _tail(name, n=2):
    """打印某个账本文件的最后 n 条（jsonl 逐行，普通 json 整份）。"""
    p = os.path.join(_DIR, name)
    if not os.path.isfile(p):
        print("====", name, "不存在")
        return
    with open(p, encoding="utf-8", errors="ignore") as f:
        raw = f.read()
    print("====", name, len(raw), "字节")
    if name.endswith(".jsonl"):
        rows = [x for x in raw.splitlines() if x.strip()][-n:]
        for r in rows:
            print(json.dumps(json.loads(r), ensure_ascii=False)[:1200])
    else:
        print(json.dumps(json.loads(raw), ensure_ascii=False)[:2000])


if __name__ == "__main__":
    for _name in ("press.jsonl", "score_drops.jsonl", "rounds.jsonl", "audits.json"):
        _tail(_name)

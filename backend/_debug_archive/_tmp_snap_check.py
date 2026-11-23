# -*- coding: utf-8 -*-
"""临时自检：iterloop 的 silent-except 修法是否真的留痕（复核用，不参与运行）。

背景：`_load_ability_snap` 原来两处 `except ...: pass`，上一轮唯一的扣分点。
    现在改成「首次运行只留 debug / 真故障 warning + actlog 事件 + 1 小时节流」，
    本脚本用 MM_DATA_DIR 隔离，把三个口径钉住：

      1. 没有快照文件（首次运行）→ 返回空快照，不刷 warning；
      2. 快照写坏 → 按空快照处理，但账本里有 rsi_snapshot 事件；
      3. 同一问题 1 小时内只进账本 1 条（节流），写失败也留痕。

用法：py backend/_tmp_snap_check.py
"""
import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="mm_iterloop_snap_")
os.environ["MM_DATA_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import actlog as AL  # noqa: E402
from backend import iterloop as IL  # noqa: E402

_ok = 0
_fail = 0


def check(name, cond, detail=""):
    global _ok, _fail
    if cond:
        _ok += 1
        print("  OK   %s%s" % (name, ("  " + detail) if detail else ""))
    else:
        _fail += 1
        print("  FAIL %s%s" % (name, ("  " + detail) if detail else ""))


def _events():
    return [r for r in AL.list_events("rsi_snapshot", limit=0)]


def main():
    empty = {"failed": [], "fixed": [], "lessons": []}
    p = IL._ability_snap_path()
    if os.path.exists(p):
        os.remove(p)

    print("\n[1] 首次运行（没有快照文件）不发 warning，返回空快照")
    check("返回空快照", IL._load_ability_snap() == empty)

    print("\n[2] 快照写坏 → 按空快照处理，但账本里留痕")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("{坏的 json ,,,")
    IL._SNAP_WARN_SEEN["ts"] = 0.0
    check("坏文件按空快照处理", IL._load_ability_snap() == empty)
    evs = _events()
    kinds = [str((r.get("w") or r.get("ev") or {}).get("kind") or "") for r in evs]
    check("账本里有 rsi_snapshot 事件", "rsi_snapshot" in kinds, str(kinds[:5]))
    txt = json.dumps(evs, ensure_ascii=False)
    check("事件里有可读说明", "ability_snapshot" in txt, txt[:120])

    print("\n[3] 节流：同一类问题 1 小时内只进账本 1 条")
    before = len(_events())
    IL._load_ability_snap()
    check("第二次不再写账本", len(_events()) == before,
          "%d -> %d" % (before, len(_events())))

    print("\n[4] 写失败同样留痕，且返回 False 不抛")
    IL._SNAP_WARN_SEEN["ts"] = 0.0
    real = IL._ability_snap_path
    IL._ability_snap_path = lambda: os.path.join(_TMP, "no_such_dir", "x", "snap.json")
    try:
        check("写不进去返回 False",
              IL._save_ability_snap(dict(empty)) is False)
        check("写失败也进了账本", len(_events()) > before, str(len(_events())))
    finally:
        IL._ability_snap_path = real

    print("\n[5] 正常读写仍然工作")
    IL._SNAP_WARN_SEEN["ts"] = 0.0
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"failed": ["a"], "fixed": ["b"], "lessons": ["c"]}, fh)
    check("读回内容正确",
          IL._load_ability_snap() == {"failed": ["a"], "fixed": ["b"], "lessons": ["c"]},
          str(IL._load_ability_snap()))

    print("\n结论：通过 %d / 失败 %d" % (_ok, _fail))
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

"""「新版本已就绪」标记。

用户明确要求（2026-11-08）：模型改完自己的源码后**不许自己重启** ——
那会在用户毫不知情的时候断一次服务、打断正在进行的对话。
正确做法是：改动落盘后只把「新版本已就绪」记下来，由前端弹一条提示，
**等用户按下「立即重启」才真的重启**；用户不按，就一直跑旧代码。

这个模块只有三个用途：
  · mark()   —— 模型改完代码后打个标记（tools.py 里调用）；
  · status() —— 前端轮询，问「有没有待生效的新版本」；
  · clear()  —— 用户点了「稍后」/ 重启完成后的收尾。

状态放内存即可：进程一重启，代码已经生效，标记自然该消失。
"""

import time

_STATE = {"pending": False, "files": [], "at": 0.0, "note": ""}


def mark(rel="", note=""):
    """标记「有新改动等待生效」。rel 是刚改的文件（可空）。"""
    _STATE["pending"] = True
    _STATE["at"] = time.time()
    if note:
        _STATE["note"] = str(note)[:200]
    rel = str(rel or "")
    if rel and rel not in _STATE["files"]:
        _STATE["files"].append(rel)
        del _STATE["files"][20:]          # 只留最近 20 个，别无限长
    return status()


def status():
    return {"pending": bool(_STATE["pending"]),
            "files": list(_STATE["files"]),
            "at": _STATE["at"],
            "note": _STATE["note"]}


def clear():
    _STATE.update({"pending": False, "files": [], "at": 0.0, "note": ""})
    return status()

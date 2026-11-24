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

import json
import logging
import os
import time
logger = logging.getLogger(__name__)

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


# ---------------------------------------------------------------- 明日待办承接
# 「说一句就开工」的收尾：用户按住悬浮球说的那句话拆成几张待办卡片，勾没勾完
# 不该因为关掉窗口就消失。所以这条**必须落盘**（上面那三个函数是内存标记，
# 进程重启就该没；而"明天接着做"的待办恰恰要能熬过重启，两者不能混在一起）。
_TODO_FILE = "todo_carry.json"


def _todo_path() -> str:
    from . import config
    return config.data("data", _TODO_FILE)


def save_tomorrow(items) -> list:
    """把还没勾完的待办存下来（下次开机接着勾）。返回规范化后的列表。

    宽容入参：`['修复登录']` / `[{'title':'修复登录','done':False}]` 都收，
    取不到标题的条目直接丢掉 —— 宁可少一条，也别在卡片里渲染一行空白。
    **永不抛异常**（收尾动作不该让前端报红）。
    """
    out = []
    for it in (items or []):
        if isinstance(it, str):
            it = {"title": it, "done": False}
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or it.get("text") or "").strip()
        if not title:
            continue
        out.append({"title": title[:80], "done": bool(it.get("done"))})
    try:
        from . import safeio
        safeio.atomic_write_json(_todo_path(),
                                 {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                  "items": out}, indent=1)
    except Exception:                                    # noqa: BLE001
        logger.warning("待办承接写盘失败（不影响本轮任务）", exc_info=True)
    return out


def load_tomorrow() -> list:
    """读回上次存的待办；没有/读坏了都返回 []（不抛异常）。"""
    try:
        with open(_todo_path(), encoding="utf-8") as f:
            data = json.load(f)
    except Exception:                                    # noqa: BLE001
        return []
    items = (data or {}).get("items") if isinstance(data, dict) else data
    out = []
    for it in (items or []):
        if isinstance(it, str):
            it = {"title": it, "done": False}
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or it.get("text") or "").strip()
        if not title:
            continue
        out.append({"title": title[:80], "done": bool(it.get("done"))})
    return out

# -*- coding: utf-8 -*-
"""记忆条目层：把「一整段记忆文本」变成**可看、可管、分区的条目**。

为什么需要它
------------
memory.py 里长期记忆是**一段用「；」连起来的文本**（现在 31 条 / 1112 字）。
对模型够用，但对人是个黑盒：界面上只能看到一个 textarea，用户没法知道
「到底记住了几条、哪条过期了、哪条是别的模型写的」。

本模块只做**视图 + 旁挂状态**，绝不另存一份记忆正文：
  · 正文唯一真相仍然是 memory.get_long() / memory.get_short(sid)；
  · 这里只维护「分区 / 钉住 / 来源模型」这类装饰性状态，
    存在 data/memory_desk.json；这个文件丢了最坏结果是分区回到自动推断，
    **记忆一个字都不会少**。
  · 条目 id = 正文哈希，同一条每次进来 id 都一样，钉住不会失效。

分区（用户要的「体系化」）
--------------------------
profile 个人档案 / pref 偏好约定 / work 项目与工具 / recent 近期动态
分区默认按关键词自动推断，用户手动改过的记在旁挂文件里（manual=True），
之后自动推断不再覆盖它。
"""
import os
import re
import json
import time
import hashlib
import logging
import threading

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()

SECTIONS = [
    ("profile", "👤 个人档案"),
    ("pref", "🎯 偏好约定"),
    ("work", "🛠 项目与工具"),
    ("recent", "🕒 近期动态"),
]
_SECTION_NAME = dict(SECTIONS)

# 关键词权重：3 = 强特征（这条几乎只可能是这个分区），1 = 弱特征（只是相关）。
# ⚠️ 原来用「先匹配先赢」，实测误判很离谱：
#   「用户姓名：陈宇桐…要求直接称呼全名」→ 命中"要求"被算成 pref；
#   「编程始于初一，主要语言 Python」→ 命中 Python 被算成 work。
# 改成打分后这两条都归位到 profile。
_WORDS = {
    "profile": {3: ("姓名", "学校", "家庭", "母亲", "父亲", "居住", "四级", "昵称",
                    "性格", "编程始于", "入团", "大一", "显卡", "爱好"),
                1: ("广东", "汕头", "人工智能", "专业")},
    "pref": {3: ("回答", "偏好", "要求", "不要", "别", "希望", "简短", "口语化",
                 "模板", "排版", "风格", "在意", "优先", "必须", "禁忌", "创作"),
             1: ("图表", "知识卡", "折叠")},
    "work": {3: ("项目", "FastAPI", "数字人", "Wav2Lip", "定时任务", "群聊",
                 "Ollama", "端口", "数据目录"),
             1: ("自迭代", "Python", "VS Code", "前端", "后端", "迁移", "技能")},
    "recent": {3: ("已停止", "进展", "正在", "了解", "本轮", "关注", "探索", "一轮"),
               1: ("近期", "最近一轮", "目标进展", "权衡")},
}


def _memory():
    """延迟导入（memory.py 顶层会 import 别的模块，避免成环）。"""
    from . import memory
    return memory


def _data_dir():
    try:
        return _memory().DATA_DIR
    except Exception:                                    # noqa: BLE001
        logger.debug("silent-except", exc_info=True)
        return (os.environ.get("MM_DATA_DIR")
                or os.path.join(os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__))), "data"))


def desk_path():
    return os.path.join(_data_dir(), "memory_desk.json")


def _read_desk():
    try:
        with open(desk_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        d = {}
    except Exception:                                    # noqa: BLE001
        # 旁挂文件坏了就当空：宁可丢「钉住」，不能让记忆读不出来。
        logger.warning("记忆桌面状态文件损坏，按空处理", exc_info=True)
        d = {}
    if not isinstance(d, dict):
        d = {}
    if not isinstance(d.get("items"), dict):
        d["items"] = {}
    if not isinstance(d.get("order"), list):
        d["order"] = []
    d["ver"] = 2
    return d


def _write_desk(d):
    p = desk_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def _norm(text):
    return re.sub(r"\s+", "", (text or "")).strip("；;。 ")


def item_id(text):
    return "m" + hashlib.md5(_norm(text).encode("utf-8")).hexdigest()[:12]


def _split(text):
    return [p.strip() for p in re.split(r"[；;\n]", text or "") if p.strip()]


def guess_section(text):
    """按关键词打分推断分区；平分时取「命中位置更靠前」的，再平按分区顺序。"""
    t = text or ""
    best, best_key = "recent", None
    for order, (sec, _name) in enumerate(SECTIONS):
        score, first = 0, 10 ** 9
        for weight, kws in _WORDS.get(sec, {}).items():
            for kw in kws:
                pos = t.find(kw)
                if pos >= 0:
                    score += weight
                    first = min(first, pos)
        if not score:
            continue
        key = (-score, first, order)      # 分高者胜 → 位置靠前者胜 → 分区顺序
        if best_key is None or key < best_key:
            best, best_key = sec, key
    return best


def _state_for(d, text):
    iid = item_id(text)
    st = d["items"].get(iid) or {}
    sec = st.get("section") if st.get("manual") else guess_section(text)
    if sec not in _SECTION_NAME:
        sec = guess_section(text)
    return iid, {
        "section": sec,
        "pin": bool(st.get("pin")),
        "source": st.get("source") or "auto",
        "model": st.get("model") or "",
        "created": st.get("created") or 0,
        "hits": int(st.get("hits") or 0),
    }


def items(sid=""):
    """返回条目列表（长期 + 本对话短期），带分区与旁挂状态。"""
    mem = _memory()
    with _LOCK:
        d = _read_desk()
        out = []
        for level, body in (("long", mem.get_long()),
                            ("short", mem.get_short(mem._safe_sid(sid) if sid else
                                                    mem.NO_SESSION_SID))):
            for text in _split(body):
                iid, st = _state_for(d, text)
                out.append({"id": iid, "text": text, "level": level,
                            "chars": len(text),
                            "section": st["section"],
                            "section_name": _SECTION_NAME.get(st["section"], st["section"]),
                            "pin": st["pin"], "source": st["source"],
                            "model": st["model"], "hits": st["hits"]})
        # 钉住的排前面，其余保持原文顺序（原文顺序＝写入顺序，越靠前越稳定）
        out.sort(key=lambda x: (0 if x["pin"] else 1))
        return out


def stats(sid=""):
    its = items(sid)
    by = {k: 0 for k, _ in SECTIONS}
    for it in its:
        by[it["section"]] = by.get(it["section"], 0) + 1
    return {"total": len(its),
            "long": sum(1 for x in its if x["level"] == "long"),
            "short": sum(1 for x in its if x["level"] == "short"),
            "pinned": sum(1 for x in its if x["pin"]),
            "by_section": by,
            "sections": [{"key": k, "name": n, "count": by.get(k, 0)} for k, n in SECTIONS]}


def set_section(iid, section):
    if section not in _SECTION_NAME:
        return {"ok": False, "error": "未知分区：%s" % section}
    with _LOCK:
        d = _read_desk()
        st = d["items"].setdefault(iid, {})
        st["section"] = section
        st["manual"] = True
        _write_desk(d)
    return {"ok": True, "id": iid, "section": section}


def toggle_pin(iid):
    with _LOCK:
        d = _read_desk()
        st = d["items"].setdefault(iid, {})
        st["pin"] = not bool(st.get("pin"))
        if st["pin"] and iid not in d["order"]:
            d["order"].insert(0, iid)
        _write_desk(d)
        return {"ok": True, "id": iid, "pin": st["pin"]}


def delete_item(iid, sid=""):
    """删掉一条记忆：**只删正文里与这个 id 完全对应的那一条**。

    安全措施（用户被误删坑过）：
      1. 先在锁内把当前长期/短期正文各留一份备份（同名 + 时间戳）；
      2. 只从正文里剔除**哈希匹配**的那一条，其余条目原样保留；
      3. 剔除后条数必须正好少 1，否则判定失败、不落盘。
    """
    mem = _memory()
    with _LOCK:
        d = _read_desk()
        target = None
        for text in _split(mem.get_long()):
            if item_id(text) == iid:
                target = text
                break
        level = "long"
        short_sid = mem._safe_sid(sid) if sid else mem.NO_SESSION_SID
        if target is None:
            for text in _split(mem.get_short(short_sid)):
                if item_id(text) == iid:
                    target = text
                    level = "short"
                    break
        if target is None:
            return {"ok": False, "error": "没有这条记忆（可能已被删除或改动）"}

        stamp = time.strftime("%Y%m%d-%H%M%S")
        body = mem.get_long() if level == "long" else mem.get_short(short_sid)
        try:
            bk = (mem.LONG_FILE if level == "long" else mem.SHORT_FILE) + ".bak-del-" + stamp
            with open(bk, "w", encoding="utf-8") as f:
                json.dump({"content": body, "updated_at": time.time()}, f, ensure_ascii=False)
        except Exception:                                # noqa: BLE001
            logger.warning("删除前备份失败（继续，但记一笔）", exc_info=True)

        old_items = _split(body)
        new_items = [x for x in old_items if item_id(x) != iid]
        if len(new_items) != len(old_items) - 1:
            return {"ok": False, "error": "匹配到 %d 条，已中止（不落盘）"
                    % (len(old_items) - len(new_items))}
        new_text = "；".join(new_items)
        if level == "long":
            mem.set_long(new_text)
        else:
            mem.set_short(short_sid, new_text)
        d["items"].pop(iid, None)
        if iid in d["order"]:
            d["order"].remove(iid)
        _write_desk(d)
        logger.info("删除记忆条目 %s（%s 层）：%s", iid, level, target[:40])
        return {"ok": True, "id": iid, "deleted": target, "left": len(new_items)}

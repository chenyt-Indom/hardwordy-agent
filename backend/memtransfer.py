# -*- coding: utf-8 -*-
"""跨模型记忆传承层（Model-agnostic memory inheritance）。

要解决的问题
------------
`memory.py` 里的长期/短期记忆**已经是**模型无关的纯文本，但它只解决
「同一个模型、不同对话」；一旦换模型（本地 ollama ↔ 云端 deepseek ↔ 别家 API），
没有东西记录「这份档案是被哪些模型共同写出来的」，也没有一份**独立于模型**的
继承包 —— 换模型后新模型只能从长期记忆那段纯文本重新理解，看不出
「这条是谁写的、什么时候写的、换过几个模型」。

这一层做的事：
1. `sync(model)` —— 把 memory 里的条目抽成**结构化、模型无关**的继承包
   （data/memory/inherit/pack.json）。每条记 sources（哪些模型写过）、
   sessions（哪些对话写过）、first_ts / last_ts。
2. 去重 / 合并用 `memcore.relation`：同一个事实由两个模型分别写过时
   **只留一条**，但把两个模型都记进 sources —— 这就是「跨模型传承」。
3. `brief(model)` —— 压成一小段注入对话上下文（几百字），告诉当前模型：
   「这份记忆是 N 个模型共同积累的，你可以直接用」。
4. `export_text()` —— 导出成可读文本，换机器 / 换模型时能整份带走。

⚠️ 存储格式故意用**纯 JSON + 纯文本**：任何模型、任何外部工具都能读，
不绑定任何厂商的向量库或私有格式。
"""
import os
import re
import json
import time
import threading

from . import config, memcore, memory

_LOCK = threading.RLock()
_MAX_ITEMS = 400              # 继承包条目上限（超出丢最久没更新的）
_BRIEF_CAP = 760              # 注入上下文的硬上限（记忆段要小，别挤掉对话）

_CACHE = {"mt_long": None, "mt_short": None, "at": 0.0}
_CACHE_TTL = 20.0             # 20 秒内不重复扫盘（每轮都会调 brief）


def _dir() -> str:
    return os.path.join(config.data("data"), "memory", "inherit")


def _pack_path() -> str:
    return os.path.join(_dir(), "pack.json")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _read_pack() -> dict:
    p = _pack_path()
    if not os.path.isfile(p):
        return {"version": 1, "updated_at": "", "items": [], "models": [],
                "sessions": []}
    try:
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        if not isinstance(d, dict):
            raise ValueError("pack 不是 dict")
        d.setdefault("items", [])
        d.setdefault("models", [])
        d.setdefault("sessions", [])
        return d
    except Exception:                                     # noqa: BLE001
        # 损坏时**不要静默清空**：留一份 .bad 备份，再从空包重建
        try:
            os.replace(p, p + ".bad")
        except Exception:                                 # noqa: BLE001
            pass
        return {"version": 1, "updated_at": "", "items": [], "models": [],
                "sessions": []}


def _write_pack(d: dict) -> None:
    os.makedirs(_dir(), exist_ok=True)
    p = _pack_path()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def _split(text: str) -> list:
    """和 memory._split_items 同口径切条目（；或换行）。"""
    return [p.strip() for p in re.split(r"[；;\n]", text or "") if p.strip()]


def _norm_model(model: str) -> str:
    m = str(model or "").strip()
    return m[:60] or "unknown"


def _merge_item(items: list, text: str, level: str, session: str,
                model: str) -> int:
    """把一条记忆并进继承包（原地修改 items），返回新增条数（0 或 1）。"""
    text = (text or "").strip()[:400]
    if len(text) < 2:
        return 0
    b = memcore.parse(text)
    if not b.body:
        return 0
    for it in items:
        a = memcore.parse(str(it.get("text") or ""))
        if not a.body:
            continue
        rel = memcore.relation(a, b)
        if rel in (memcore.SAME, memcore.COVERED):
            it["last_ts"] = time.time()
            _add_uniq(it.setdefault("models", []), model)
            if session:
                _add_uniq(it.setdefault("sessions", []), session)
            return 0
        if rel == memcore.SUPERSET:
            # 新条目更具体 → 用新的替换文本，但保留来源历史
            it["text"] = text
            it["last_ts"] = time.time()
            _add_uniq(it.setdefault("models", []), model)
            if session:
                _add_uniq(it.setdefault("sessions", []), session)
            return 0
        if rel == memcore.FLIP:
            # 正反说法：说明事实被纠正了 → 以新的为准
            it["text"] = text
            it["last_ts"] = time.time()
            _add_uniq(it.setdefault("models", []), model)
            if session:
                _add_uniq(it.setdefault("sessions", []), session)
            return 0
    items.append({
        "text": text,
        "level": level,
        "models": [model],
        "sessions": [session] if session else [],
        "first_ts": time.time(),
        "last_ts": time.time(),
    })
    return 1


def _add_uniq(arr: list, val: str) -> None:
    if val and val not in arr:
        arr.append(val)


def sync(model: str = "", session: str = "", force: bool = False) -> dict:
    """把 memory 里的条目同步进继承包（模型无关）。

    幂等：同一份记忆重复同步不会重复记条目。20 秒内重复调用走缓存，
    除非 force=True。
    """
    m = _norm_model(model)
    now = time.time()
    if (not force and _CACHE["at"] and (now - _CACHE["at"]) < _CACHE_TTL
            and _CACHE["mt_long"] == m):
        d = _read_pack()
        return {"ok": True, "cached": True, "items": len(d.get("items") or []),
                "added": 0, "models": d.get("models") or []}
    try:
        with _LOCK:
            d = _read_pack()
            items = d.get("items") or []
            added = 0
            long_txt = memory.get_long()
            for t in _split(long_txt):
                added += _merge_item(items, t, "长期记忆", "", m)
            # 短期记忆：只同步**当前对话**的，别的对话的内容不该被带进这一轮
            if session:
                try:
                    short_txt = memory.get_short(session)
                except Exception:                         # noqa: BLE001
                    short_txt = ""
                for t in _split(short_txt):
                    added += _merge_item(items, t, "短期记忆", session, m)
            # 超出上限：丢最久没更新的（头部稳定档案通常最早，故只丢超过 40% 之后的部分）
            if len(items) > _MAX_ITEMS:
                items.sort(key=lambda x: float(x.get("last_ts") or 0))
                items = items[len(items) - _MAX_ITEMS:]
            d["items"] = items
            d["version"] = 1
            d["updated_at"] = _now()
            _add_uniq(d.setdefault("models", []), m)
            if session:
                _add_uniq(d.setdefault("sessions", []), session)
            _write_pack(d)
            _CACHE.update({"mt_long": m, "mt_short": session, "at": now})
            return {"ok": True, "cached": False, "items": len(items),
                    "added": added, "models": d.get("models") or []}
    except Exception as e:                                # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def stats() -> dict:
    d = _read_pack()
    items = d.get("items") or []
    by_level = {}
    for it in items:
        lv = str(it.get("level") or "未标注")
        by_level[lv] = by_level.get(lv, 0) + 1
    multi = [it for it in items if len(it.get("models") or []) > 1]
    return {
        "items": len(items),
        "by_level": by_level,
        "models": d.get("models") or [],
        "sessions": len(d.get("sessions") or []),
        "multi_model_items": len(multi),
        "updated_at": d.get("updated_at") or "",
        "path": _pack_path(),
    }


def brief(model: str = "", session: str = "", max_chars: int = _BRIEF_CAP) -> str:
    """压成一小段注入对话上下文；没有记忆时返回空串。"""
    try:
        r = sync(model=model, session=session)
        if not r.get("ok"):
            return ""
        st = stats()
        if not st["items"]:
            return ""
        models = st.get("models") or []
        head = ("【跨模型记忆传承】记忆存在本机纯文本里，**与模型无关**："
                "换模型、换对话都不会丢。当前档案由 %d 个模型共同写入（%s），"
                "共 %d 条（%s）。"
                % (len(models), "、".join(models[:4]) or "本机",
                   st["items"],
                   "、".join("%s %d" % (k, v) for k, v in st["by_level"].items())))
        if st["multi_model_items"]:
            head += "其中 %d 条被多个模型分别确认过。" % st["multi_model_items"]
        d = _read_pack()
        items = sorted(d.get("items") or [],
                       key=lambda x: -float(x.get("last_ts") or 0))[:3]
        lines = [head, "最近更新的 3 条（完整档案见【记忆】段）："]
        for it in items:
            lines.append("- %s（%s）" % (str(it.get("text") or "")[:90],
                                        "、".join((it.get("models") or [])[:3])))
        lines.append("直接用这些信息，别重新问一遍；新事实用 remember 追加，"
                     "写进去后会自动并进传承包。")
        out = "\n".join(lines)
        if len(out) > max_chars:
            out = out[:max_chars] + "…"
        return out
    except Exception:                                     # noqa: BLE001
        return ""


def export_text(path: str = "") -> dict:
    """导出成可读文本（换机器 / 换模型时整份带走）。"""
    try:
        d = _read_pack()
        items = sorted(d.get("items") or [],
                       key=lambda x: float(x.get("first_ts") or 0))
        lines = ["# 跨模型记忆传承包",
                 "更新时间：%s" % (d.get("updated_at") or ""),
                 "写入模型：%s" % ("、".join(d.get("models") or []) or "未知"),
                 "条目数：%d" % len(items), ""]
        for i, it in enumerate(items, 1):
            lines.append("%d. [%s] %s" % (i, it.get("level") or "-",
                                          it.get("text") or ""))
            lines.append("   来源模型：%s；对话数：%d；首次：%s" % (
                "、".join(it.get("models") or []) or "-",
                len(it.get("sessions") or []),
                time.strftime("%Y-%m-%d %H:%M",
                              time.localtime(float(it.get("first_ts") or 0)))
                if it.get("first_ts") else "-"))
        txt = "\n".join(lines)
        if not path:
            path = os.path.join(_dir(), "export_%s.txt" % time.strftime("%Y%m%d_%H%M%S"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(txt)
        return {"ok": True, "path": path, "chars": len(txt), "items": len(items)}
    except Exception as e:                                # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def forget(text: str) -> dict:
    """从传承包里删掉一条（用户在 memory 里 forget 之后，这里也要跟着清）。"""
    try:
        with _LOCK:
            d = _read_pack()
            items = d.get("items") or []
            b = memcore.parse(text or "")
            keep = []
            hit = 0
            for it in items:
                a = memcore.parse(str(it.get("text") or ""))
                if a.body and b.body and memcore.relation(a, b) in (
                        memcore.SAME, memcore.COVERED):
                    hit += 1
                    continue
                keep.append(it)
            d["items"] = keep
            d["updated_at"] = _now()
            _write_pack(d)
            _CACHE["at"] = 0.0
            return {"ok": True, "removed": hit, "items": len(keep)}
    except Exception as e:                                # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}

"""用户自定义技能（skill）—— 增删改查 + 三种生效方式。

用户 2026-11-10 的要求：
  · 可以按用户要求给自己添加 skill；
  · 前端能点开看详情、能改、能删、能新建；
  · 每个 skill 自己选生效方式（用户答"三种都支持"）：
      manual —— 手动：前端点一下，注入到下一轮对话，只生效一次；
      always —— 常驻：始终注入系统提示，每轮都起作用；
      auto   —— 自动：只把「名称+描述+触发词」目录给模型，模型自己判断该不该用，
                要用再调 load_skill 工具取全文（用户答"每轮让模型自己判断"）。
  · 带触发词（用于自动匹配提示）；
  · 内置几个常用示例，一打开就有东西可点。

存储：data/skills.json（纯本地文件，不出机器）。

⚠️ 为什么 always 的正文能放系统提示（那里是 Ollama 的前缀缓存区）：
   系统提示里凡是"每轮都变"的东西都会让缓存全部作废（见 main.py 里
   _attach_live_ctx 的长注释）。但技能集合**只在用户增删改技能时才变** ——
   同一会话里连续几十轮都是同一份文本，缓存照样命中。所以 always/auto 的
   目录放系统提示是安全的；只有「手动注入的那一个」每轮会变，那个挂到
   最后一条用户消息上（由 consume_manual 取出来交给 _attach_live_ctx）。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any

from . import config

_LOCK = threading.RLock()

# 手动注入的待用技能：{session: skill_id}
# 只在"点了技能 → 下一条消息"这一轮有效，用掉即清（见 consume_manual）。
_MANUAL: dict[str, str] = {}

MODES = ("manual", "always", "auto")
MODE_LABEL = {"manual": "手动", "always": "常驻", "auto": "自动"}


def _path() -> str:
    return config.data("data", "skills.json")


# ---------------------------------------------------------------- 内置示例
def _builtin() -> list[dict]:
    """首次运行时写入的示例技能。用户可以随便改、随便删。"""
    now = time.strftime("%Y-%m-%d %H:%M")
    raw = [
        {
            "name": "写周报",
            "desc": "把零散的工作记录整理成一份结构清晰的周报",
            "triggers": ["周报", "工作总结", "这周做了什么"],
            "content": (
                "按下面的结构写周报，不要写成一整段散文：\n"
                "1. 本周完成（3~6 条，每条「做了什么 + 结果/数字」，动词开头）\n"
                "2. 进行中（写清当前进度百分比和卡在哪）\n"
                "3. 下周计划（2~4 条，可验收，别写「继续推进」这种空话）\n"
                "4. 需要协调 / 风险（没有就写「无」）\n"
                "写作要求：不夸大、不写形容词堆砌；用户没给的数字不要编，"
                "缺信息就标「待补充」；总长度控制在 400 字以内。"
            ),
        },
        {
            "name": "文案润色",
            "desc": "把口语化、啰嗦的文字改成干净利落的表达",
            "triggers": ["润色", "改文案", "优化表达", "通顺"],
            "content": (
                "改写时守这几条：\n"
                "· 删掉「进行 / 方面 / 相关 / 一定程度」这类空词；\n"
                "· 一句只说一件事，超过 40 字就拆开；\n"
                "· 能用动词就别用名词化表达（「对方案进行优化」→「优化方案」）；\n"
                "· 保留用户原意和原有信息量，**不要自行添加事实或数据**；\n"
                "· 输出时先给改好的版本，再用一两行说明主要改了什么（不要逐句对照）。"
            ),
        },
        {
            "name": "代码审查",
            "desc": "挑出代码里真会出问题的地方，而不是罗列风格偏好",
            "triggers": ["审代码", "代码审查", "review", "检查代码", "看看这段代码"],
            "content": (
                "审查顺序（先致命后次要）：\n"
                "1. 会出错：空指针 / 越界 / 未处理的异常 / 资源没释放 / 竞态；\n"
                "2. 会出错但隐蔽：边界条件、类型转换、编码问题、时区、浮点比较；\n"
                "3. 会拖累人：重复逻辑、过长函数、命名误导、魔法数字；\n"
                "4. 安全问题：注入、路径穿越、密钥硬编码、越权。\n"
                "输出格式：每条写「文件:行 → 问题 → 为什么是问题 → 怎么改（给具体代码）」。"
                "**不要**列纯风格偏好（缩进、引号）；没有真问题就直说「没发现实质问题」，"
                "不要为了凑数硬挑。"
            ),
        },
        {
            "name": "PPT 大纲",
            "desc": "先把演示结构和大纲定下来，再动手做页面",
            "triggers": ["PPT", "演示稿", "汇报材料", "大纲"],
            "content": (
                "先定结构再动手，顺序不能反：\n"
                "1. 问清（或从上下文推断）：讲给谁听、多长时间、要对方做什么决定；\n"
                "2. 给出一页一句话的核心结论（整份 PPT 只服务这一句）；\n"
                "3. 列 8~15 页大纲，每页写「标题 + 3~5 条要点」，要点要带数字或结论，"
                "不要只写「背景」「现状」这种空标签；\n"
                "4. 标出哪几页上表格 / 图表，以及数据从哪来；\n"
                "5. 大纲确认后再调 make_pptx 出成品，并给每页写演讲备注。"
            ),
        },
        {
            "name": "会议纪要",
            "desc": "把杂乱的会议记录整理成能直接发出去的纪要",
            "triggers": ["会议纪要", "开会记录", "会议记录"],
            "content": (
                "整理成四块：\n"
                "1. 结论（本次会议定了什么，逐条，动词开头）\n"
                "2. 待办（事项 / 负责人 / 截止时间，三列齐全；缺的标「待确认」）\n"
                "3. 分歧（还没谈拢的点，写清各方立场）\n"
                "4. 下次讨论（时间与议题，没有就不写）\n"
                "要求：只写会议里真实说过的内容，不要补全、不要推测动机；"
                "口语和重复的表述直接归并；全文控制在 500 字内。"
            ),
        },
    ]
    out = []
    for i, r in enumerate(raw):
        out.append({
            "id": "sk_builtin_%d" % (i + 1),
            "name": r["name"],
            "desc": r["desc"],
            "content": r["content"],
            "triggers": list(r["triggers"]),
            "mode": "manual",
            "enabled": True,
            "builtin": True,
            "created": now,
            "updated": now,
        })
    return out


# ---------------------------------------------------------------- 读写
def _read_raw() -> list[dict] | None:
    p = _path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        # 文件坏了不能让整个应用起不来：备份一份再用空表继续
        try:
            import shutil
            shutil.copyfile(p, p + ".broken-" + time.strftime("%Y%m%d_%H%M%S"))
        except Exception:
            pass
        return []
    if isinstance(d, dict):
        d = d.get("items")
    if not isinstance(d, list):
        return []
    out = []
    for it in d:
        if isinstance(it, dict) and it.get("name"):
            out.append(_normalize(it))
    return out


def _normalize(it: dict) -> dict:
    mode = str(it.get("mode") or "manual").strip().lower()
    if mode not in MODES:
        mode = "manual"
    trig = it.get("triggers")
    if isinstance(trig, str):
        trig = [t.strip() for t in trig.replace("，", ",").split(",") if t.strip()]
    elif not isinstance(trig, list):
        trig = []
    return {
        "id": str(it.get("id") or ("sk_" + uuid.uuid4().hex[:10])),
        "name": str(it.get("name") or "").strip()[:60],
        "desc": str(it.get("desc") or "").strip()[:200],
        "content": str(it.get("content") or ""),
        "triggers": [str(t).strip()[:30] for t in trig if str(t).strip()][:20],
        "mode": mode,
        "enabled": bool(it.get("enabled", True)),
        "builtin": bool(it.get("builtin", False)),
        "created": str(it.get("created") or time.strftime("%Y-%m-%d %H:%M")),
        "updated": str(it.get("updated") or time.strftime("%Y-%m-%d %H:%M")),
    }


def _write_raw(items: list[dict]) -> None:
    p = _path()
    import os
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"items": items}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def load() -> list[dict]:
    """读出全部技能；文件不存在时先写入内置示例。"""
    with _LOCK:
        items = _read_raw()
        if items is None:
            items = _builtin()
            try:
                _write_raw(items)
            except Exception:
                pass
        return items


def save(items: list[dict]) -> None:
    with _LOCK:
        _write_raw([_normalize(i) for i in items])


# ---------------------------------------------------------------- 增删改查
def list_all() -> list[dict]:
    return load()


def get(sid: str) -> dict | None:
    sid = str(sid or "").strip()
    for it in load():
        if it["id"] == sid:
            return it
    return None


def get_by_name(name: str) -> dict | None:
    n = str(name or "").strip()
    for it in load():
        if it["name"] == n:
            return it
    return None


def create(data: dict) -> dict:
    """新建。name 必填，同名则拒绝（返回 None 由调用方报错）。"""
    name = str((data or {}).get("name") or "").strip()
    if not name:
        raise ValueError("技能名不能为空")
    with _LOCK:
        items = load()
        if any(i["name"] == name for i in items):
            raise ValueError("已经有叫「%s」的技能了，换个名字或直接改那一个" % name)
        now = time.strftime("%Y-%m-%d %H:%M")
        item = _normalize({
            "id": "sk_" + uuid.uuid4().hex[:10],
            "name": name,
            "desc": data.get("desc"),
            "content": data.get("content"),
            "triggers": data.get("triggers"),
            "mode": data.get("mode"),
            "enabled": data.get("enabled", True),
            "builtin": False,
            "created": now,
            "updated": now,
        })
        items.append(item)
        _write_raw(items)
        return item


def update(sid: str, data: dict) -> dict | None:
    with _LOCK:
        items = load()
        for i, it in enumerate(items):
            if it["id"] != sid:
                continue
            new = dict(it)
            for k in ("name", "desc", "content", "triggers", "mode", "enabled"):
                if k in (data or {}):
                    new[k] = data[k]
            # 改名要查重（排除自己）
            nm = str(new.get("name") or "").strip()
            if not nm:
                raise ValueError("技能名不能为空")
            if any(o["name"] == nm and o["id"] != sid for o in items):
                raise ValueError("已经有叫「%s」的技能了" % nm)
            new["updated"] = time.strftime("%Y-%m-%d %H:%M")
            new = _normalize(new)
            items[i] = new
            _write_raw(items)
            return new
        return None


def delete(sid: str) -> bool:
    with _LOCK:
        items = load()
        left = [i for i in items if i["id"] != sid]
        if len(left) == len(items):
            return False
        _write_raw(left)
        # 顺手清掉指向它的手动选择
        for k, v in list(_MANUAL.items()):
            if v == sid:
                _MANUAL.pop(k, None)
        return True


def toggle(sid: str, enabled: bool | None = None) -> dict | None:
    it = get(sid)
    if not it:
        return None
    return update(sid, {"enabled": (not it["enabled"]) if enabled is None else bool(enabled)})


# ---------------------------------------------------------------- 注入用文本
def render_skill(sk: dict, with_header: bool = True) -> str:
    head = ""
    if with_header:
        head = "### 技能：%s（%s）" % (sk["name"], MODE_LABEL.get(sk["mode"], sk["mode"]))
        if sk.get("desc"):
            head += " —— " + sk["desc"]
        head += "\n"
    return head + str(sk.get("content") or "").strip()


def build_always_block() -> str:
    """常驻技能正文 —— 拼进系统提示（稳定段，见模块开头说明）。"""
    items = [i for i in load() if i["enabled"] and i["mode"] == "always"]
    if not items:
        return ""
    body = "\n\n".join(render_skill(i) for i in items)
    return (
        "\n【常驻技能 —— 用户设定，每一轮回答都必须遵守】\n"
        "下面这些是用户给自己配的固定做法，优先级高于你的默认习惯，"
        "但**低于用户本轮的明确要求**（本轮说法和它冲突时以本轮为准，并提一句）。\n\n"
        + body + "\n"
    )


def build_auto_catalog() -> str:
    """自动技能的目录（只给名字/描述/触发词，正文让模型自己按需取）。"""
    items = [i for i in load() if i["enabled"] and i["mode"] == "auto"]
    if not items:
        return ""
    lines = []
    for i in items:
        t = (" ｜ 触发词：" + "、".join(i["triggers"])) if i["triggers"] else ""
        lines.append("- %s（id=%s）：%s%s" % (i["name"], i["id"], i.get("desc") or "（无描述）", t))
    return (
        "\n【可用技能目录 —— 自己判断该不该用】\n"
        "用户配了下面这些技能。**每轮先判断**：本轮请求是否落在某个技能的适用范围内"
        "（看描述和触发词，别只看字面关键词 —— 用户换个说法也算）。\n"
        "· 判断适用 → **先调用 load_skill 取它的全文**，再严格照它执行；\n"
        "· 判断都不适用 → 正常回答，**不要硬套**，也不要提「有这些技能」来打扰用户；\n"
        "· 同时命中多个 → 取最贴近的那个；确实需要配合才多取。\n"
        + "\n".join(lines) + "\n"
    )


def manual_catalog() -> list[dict]:
    """手动技能的极简清单（给前端用，也用于「用户说『用XX技能』」时按名字找）。"""
    return [{"id": i["id"], "name": i["name"], "desc": i["desc"]}
            for i in load() if i["enabled"] and i["mode"] == "manual"]


# ---------------------------------------------------------------- 手动注入
def set_manual(session: str, sid: str) -> bool:
    """前端点「用这个技能」→ 记下来，下一轮注入一次。"""
    sk = get(sid)
    if not sk:
        return False
    if not sk["enabled"]:
        return False
    with _LOCK:
        _MANUAL[str(session or "")] = sid
    return True


def clear_manual(session: str) -> None:
    with _LOCK:
        _MANUAL.pop(str(session or ""), None)


def peek_manual(session: str) -> dict | None:
    with _LOCK:
        sid = _MANUAL.get(str(session or ""))
    return get(sid) if sid else None


def do_load_skill(arguments: dict) -> str:
    """工具 load_skill 的实现：按 id 或名字取技能全文，回给模型。

    放在本模块而不是 tools.py：它只依赖这里的数据，tools.py 那边只留一行转发。
    """
    a = arguments or {}
    sid = str(a.get("id") or "").strip()
    nm = str(a.get("name") or "").strip()
    sk = get(sid) if sid else None
    if not sk and nm:
        sk = get_by_name(nm)
        if not sk:
            # 宽松匹配一次：用户说「周报」而技能叫「写周报」这种情况很常见
            cands = [i for i in load() if nm in i["name"]]
            if len(cands) == 1:
                sk = cands[0]
    if not sk:
        avail = "、".join(i["name"] for i in load()) or "（一个都没有）"
        return "没找到这个技能。现有技能：%s" % avail
    if not sk["enabled"]:
        return "技能「%s」当前是关闭状态，不要使用它。" % sk["name"]
    head = ("【技能：%s】—— 请严格按下面的做法完成本轮任务"
            "（这是用户自己配的规范，优先于你的默认习惯）：\n" % sk["name"])
    return head + render_skill(sk, with_header=False)



def do_save_skill(arguments: dict) -> str:
    """工具 save_skill：新增，或按名字覆盖一个已有技能。

    用户说「加一个XX技能 / 以后都按这个格式做」时由模型调用 ——
    这样"按用户要求给自己加技能"才真的能做到，而不是只能靠前端手填。
    """
    a = arguments or {}
    name = str(a.get("name") or "").strip()
    content = str(a.get("content") or "").strip()
    if not name:
        return "技能名不能为空。"
    if not content:
        return "技能内容不能为空 —— 要写清具体怎么做（分几步、每步产出什么），别只写一句口号。"
    payload = {}
    for k in ("desc", "triggers", "mode", "enabled"):
        if a.get(k) is not None:
            payload[k] = a[k]
    payload["content"] = content
    try:
        cur = get_by_name(name)
        if cur:
            it = update(cur["id"], payload)
            return ("已更新技能「%s」（%s）。用户可在顶栏「⚡ 技能」里查看、修改、删除。"
                    % (it["name"], MODE_LABEL.get(it["mode"], it["mode"])))
        it = create(dict(payload, name=name))
        return ("已新建技能「%s」（%s）。用户可在顶栏「⚡ 技能」里查看、修改、删除。"
                % (it["name"], MODE_LABEL.get(it["mode"], it["mode"])))
    except Exception as e:
        return "保存失败：%s" % e


def do_delete_skill(arguments: dict) -> str:
    """工具 delete_skill：按名字或 id 删掉一个技能。"""
    a = arguments or {}
    key = str(a.get("name_or_id") or "").strip()
    if not key:
        return "要删哪个技能？给个名字或 id。"
    sk = get(key) or get_by_name(key)
    if not sk:
        cands = [i for i in load() if key in i["name"]]
        if len(cands) == 1:
            sk = cands[0]
    if not sk:
        avail = "、".join(i["name"] for i in load()) or "（一个都没有）"
        return "没找到叫「%s」的技能。现有：%s" % (key, avail)
    if delete(sk["id"]):
        return "已删除技能「%s」。" % sk["name"]
    return "删除失败（可能已经被删了）。"


def consume_manual(session: str) -> str:
    """取出并清空本轮的手动技能，返回要挂到用户消息上的文本。"""
    with _LOCK:
        sid = _MANUAL.pop(str(session or ""), None)
    if not sid:
        return ""
    sk = get(sid)
    if not sk or not sk["enabled"]:
        return ""
    return (
        "【用户本轮指定了技能：%s】—— 请严格按下面这套做法完成本轮任务"
        "（它是用户自己配的规范，优先于你的默认习惯）：\n%s"
        % (sk["name"], render_skill(sk, with_header=False))
    )



# =====================================================================
#  技能分享 / 导入（2026-11-10 用户要求）
#    · 一键把技能导出成「一段可直接粘给任何 AI 的话 + 一段机器可读分享码」；
#    · 分享时生效方式固定建议为 auto（用户选的），对方可自行改；
#    · 也支持反向导入：把别人发来的分享内容粘进来，一键建成本地技能。
#  分享码 = "SKILL1:" + zlib 压缩后 JSON 的 urlsafe-base64，
#  一段几百字的技能压完通常只有一两百字符，二维码也塞得下。
# =====================================================================
SHARE_PREFIX = "SKILL1:"
SHARE_MODE = "auto"


def _b64e(raw: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(txt: str) -> bytes:
    import base64
    pad = "=" * (-len(txt) % 4)
    return base64.urlsafe_b64decode(txt + pad)


def share_payload(sk: dict) -> dict:
    """导出结构：去掉 id / 时间 / builtin 这些只属于本机的痕迹。"""
    return {
        "v": 1,
        "name": str(sk.get("name") or "").strip(),
        "desc": str(sk.get("desc") or "").strip(),
        "triggers": [str(t) for t in (sk.get("triggers") or [])],
        "mode": SHARE_MODE,
        "content": str(sk.get("content") or "").strip(),
    }


def share_code(sk: dict) -> str:
    """机器可读分享码（一段无空白字符串，方便扫码 / 复制）。"""
    import json as _json
    import zlib
    raw = _json.dumps(share_payload(sk), ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    return SHARE_PREFIX + _b64e(zlib.compress(raw, 9))


def share_text(sk: dict) -> str:
    """给人看 + 给 AI 执行的完整分享文本（用户要求两者都写）。"""
    p = share_payload(sk)
    trig = "、".join(p["triggers"]) or "（无）"
    parts = [
        "📦 技能分享：" + p["name"],
        "这是我的一个 AI 技能，你复制下面整段发给你的 AI 助手，它就能原样建好。",
        "",
        "──────── 以下是给 AI 的指令 ────────",
        "请把下面这个技能加入你的技能库：调用 save_skill 保存，"
        "名称、描述、触发词、正文照抄，不要改写或精简；"
        "生效方式设为 auto（自动，你以后自己判断该不该用）。",
        "",
        "名称：" + p["name"],
        "描述：" + (p["desc"] or "（无）"),
        "触发词：" + trig,
        "生效方式：auto（自动）",
        "正文：",
        p["content"],
        "──────── 指令结束 ────────",
        "",
        "【分享码 · 机器可读，别手改】",
        share_code(sk),
        "",
    ]
    return "\n".join(parts)


def parse_share(text: str) -> dict | None:
    """从用户粘贴的内容里认出技能。

    依次尝试：分享码 → 裸 JSON → 可读文本（名称/描述/触发词/正文）。
    认不出来返回 None。
    """
    import json as _json
    import re
    import zlib

    s = str(text or "").strip()
    if not s:
        return None

    # ① 分享码（可能夹在整段话里，也可能单独一行）
    m = re.search(r"SKILL1:([A-Za-z0-9_\-=]+)", s)
    if m:
        try:
            data = _json.loads(zlib.decompress(_b64d(m.group(1))).decode("utf-8"))
            if isinstance(data, dict) and data.get("name"):
                return data
        except Exception:
            pass

    # ② 裸 JSON（整段就是 JSON，或话里夹了一段 {...}）
    if s.startswith("{"):
        try:
            data = _json.loads(s)
            if isinstance(data, dict) and data.get("name"):
                return data
        except Exception:
            pass
    m = re.search(r"\{[\s\S]*\"name\"[\s\S]*\}", s)
    if m:
        try:
            data = _json.loads(m.group(0))
            if isinstance(data, dict) and data.get("name"):
                return data
        except Exception:
            pass

    # ③ 可读文本（对方只把上半段发回来了）
    def field(key: str, stops: str) -> str:
        mm = re.search(key + r"[：:]\s*([\s\S]*?)(?=\n(?:%s)|\Z)" % stops, s)
        return mm.group(1).strip() if mm else ""

    name = field("名称", "描述|触发词|生效方式|正文")
    body = field("正文", "────────|【分享码")
    if name and body:
        trig = field("触发词", "生效方式|正文")
        desc = field("描述", "触发词|生效方式|正文")
        return {
            "v": 1,
            "name": name.splitlines()[0].strip()[:60],
            "desc": desc.splitlines()[0].strip()[:200],
            "triggers": [t.strip() for t in re.split(r"[、,，]", trig) if t.strip()][:20],
            "mode": SHARE_MODE,
            "content": body,
        }
    return None


def import_skill(text: str) -> dict:
    """把分享内容建成本地技能；重名自动加后缀，绝不覆盖用户已有的。"""
    data = parse_share(text)
    if not data:
        raise ValueError("没认出技能内容 —— 请粘贴完整的分享文本，或以 SKILL1: 开头的分享码")
    name = str(data.get("name") or "").strip()[:60]
    content = str(data.get("content") or "").strip()
    if not name:
        raise ValueError("分享内容里没有技能名")
    if not content:
        raise ValueError("分享内容里没有技能正文")
    with _LOCK:
        items = load()
        taken = {i["name"] for i in items}
        if name in taken:
            base = name[:56]
            n = 1
            while ("%s（导入%d）" % (base, n))[:60] in taken:
                n += 1
            name = ("%s（导入%d）" % (base, n))[:60]
    return create({
        "name": name,
        "desc": data.get("desc"),
        "content": content,
        "triggers": data.get("triggers"),
        "mode": data.get("mode") or SHARE_MODE,
        "enabled": True,
    })

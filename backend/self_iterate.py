"""自我迭代（self-iterate）—— AI 主动反思、看新技术、形成计划、等用户批准后动手。

2026-11-15 用户要求（原话要点）：
  · 前端一个「AI 迭代想法」按钮，点开能看到：AI 对自己的反思、改进方法、
    对用户工作方式的想法、可以增加的功能；
  · 能实时从网上各大平台获取最新技术论文与新闻，主动思考怎么用到本项目；
  · AI 可以自主决定要改什么，但**动手前必须给提醒**，用户在抽屉里点批准/拒绝；
  · 批准后弹备注框，用户可以补充建议或微调；
  · 逐条批准、整批批准都要支持；
  · 归档历史 + 显示「建议采纳率」（提了多少条、实际做了多少条）；
  · 每次改动前自动备份，可一键回滚。

设计上的三条硬约束（自己定的，避免这个功能变成"乱改代码"的入口）：
  1. 只允许改 backend/ 与 frontend/ 下的 .py/.js/.css/.html；
     data/ models/ sessions/ image_library/ secrets 一律拒绝。
  2. 每个动作在应用前把原文件备份到 data/self_iterate/backups/<tag>/，
     应用后做语法校验，失败自动回滚。
  3. **没有用户批准的动作绝不执行**（approve 之外没有第二条执行入口）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_DATA = _ROOT / "data" / "self_iterate"
_BACKUPS = _DATA / "backups"
_PLANS = _DATA / "plans.json"
_SIGNALS = _DATA / "signals.json"
_JOURNAL = _ROOT / "data" / "selfdev_journal.jsonl"

# 允许 AI 自主修改的范围（白名单），其余一律拒绝
_ALLOWED_PREFIX = ("backend/", "frontend/")
_ALLOWED_EXT = (".py", ".js", ".css", ".html")
_FORBIDDEN_PARTS = ("data/", "models/", "sessions/", "image_library/",
                    "secrets", ".env", ".git/", "__pycache__")

_MAX_FILES_PER_ACTION = 3
_SIGNAL_KEEP = 60


# ---------------------------------------------------------------- 存储层

def _ensure():
    try:
        _BACKUPS.mkdir(parents=True, exist_ok=True)
    except Exception:                                   # noqa: BLE001
        pass


def _read_json(path, default):
    try:
        if path.is_file():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:                                   # noqa: BLE001
        pass
    return default


def _write_json(path, obj):
    _ensure()
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _pid():
    return time.strftime("%Y%m%d_%H%M%S")


def load_plans():
    d = _read_json(_PLANS, {"items": []})
    if isinstance(d, list):
        d = {"items": d}
    if not isinstance(d.get("items"), list):
        d["items"] = []
    return d


def save_plans(d):
    _write_json(_PLANS, d)


def load_signals():
    d = _read_json(_SIGNALS, {"items": []})
    if isinstance(d, list):
        d = {"items": d}
    if not isinstance(d.get("items"), list):
        d["items"] = []
    return d


def save_signals(d):
    _write_json(_SIGNALS, d)


# ---------------------------------------------------------------- 信号（对话摘要）

def add_signal(user_text, assistant_text, meta=None):
    """前端每轮对话结束后上报一段摘要，供反思时当作"最近发生了什么"。

    只留最近 _SIGNAL_KEEP 条，避免文件无限增长。
    """
    d = load_signals()
    d["items"].append({
        "ts": _now(),
        "user": str(user_text or "")[:800],
        "assistant": str(assistant_text or "")[:800],
        "meta": meta or {},
    })
    if len(d["items"]) > _SIGNAL_KEEP:
        d["items"] = d["items"][-_SIGNAL_KEEP:]
    save_signals(d)
    return {"ok": True, "count": len(d["items"])}


# ---------------------------------------------------------------- 技术情报

_UA = {"User-Agent": "Mozilla/5.0 (compatible; self-iterate/1.0)"}


def _http(url, timeout=9):
    try:
        import requests
        r = requests.get(url, headers=_UA, timeout=timeout)
        if r.status_code == 200:
            return r.text
    except Exception:                                   # noqa: BLE001
        pass
    return ""


def _http_json(url, timeout=10):
    try:
        import requests
        r = requests.get(url, headers=_UA, timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception:                                   # noqa: BLE001
        pass
    return None


def fetch_arxiv(max_n=8):
    """arXiv 最新 AI/多模态论文（官方 Atom API）。"""
    url = ("http://export.arxiv.org/api/query?"
           "search_query=cat:cs.AI+OR+cat:cs.CV+OR+cat:cs.CL"
           "&sortBy=submittedDate&sortOrder=descending&max_results=%d" % max_n)
    txt = _http(url)
    if not txt:
        return []
    out = []
    for blk in re.findall(r"<entry>(.*?)</entry>", txt, re.S)[:max_n]:
        t = re.search(r"<title>(.*?)</title>", blk, re.S)
        s = re.search(r"<summary>(.*?)</summary>", blk, re.S)
        u = re.search(r"<id>(.*?)</id>", blk, re.S)
        title = re.sub(r"\s+", " ", (t.group(1) if t else "")).strip()
        summ = re.sub(r"\s+", " ", (s.group(1) if s else "")).strip()[:280]
        if title:
            out.append({"source": "arXiv", "title": title,
                        "url": (u.group(1).strip() if u else ""),
                        "summary": summ})
    return out


def fetch_github_trending(max_n=8):
    """GitHub 热门项目。

    ⚠️ 2026-11-15 实测：`https://github.com/trending` 网页在本机网络下
    **直连超时**（ReadTimeout 10s），但 `api.github.com` 正常返回 200。
    所以改用官方 Search API 取「近 30 天新建、星标较高」的项目 ——
    这比 Trending 页面更稳，而且能拿到 star 数和描述。
    """
    import datetime
    d30 = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
    url = ("https://api.github.com/search/repositories"
           "?q=created:>%s+stars:>30&sort=stars&order=desc&per_page=%d"
           % (d30, max(1, min(max_n, 20))))
    j = _http_json(url)
    if not isinstance(j, dict):
        return []
    out = []
    for it in (j.get("items") or [])[:max_n]:
        name = it.get("full_name") or ""
        if not name:
            continue
        out.append({
            "source": "GitHub",
            "title": "%s ★%s" % (name, it.get("stargazers_count", "")),
            "url": it.get("html_url") or ("https://github.com/" + name),
            "summary": (it.get("description") or "")[:200],
        })
    return out


def fetch_hn(max_n=8):
    """Hacker News 热门。"""
    ids_txt = _http("https://hacker-news.firebaseio.com/v0/beststories.json")
    if not ids_txt:
        return []
    try:
        ids = json.loads(ids_txt)[:max_n]
    except Exception:                                   # noqa: BLE001
        return []
    out = []
    for i in ids:
        t = _http("https://hacker-news.firebaseio.com/v0/item/%s.json" % i, timeout=6)
        if not t:
            continue
        try:
            j = json.loads(t)
        except Exception:                               # noqa: BLE001
            continue
        if j.get("title"):
            out.append({"source": "HackerNews", "title": j["title"][:160],
                        "url": j.get("url") or ("https://news.ycombinator.com/item?id=%s" % i),
                        "summary": ""})
    return out


def fetch_cn_media(max_n=6):
    """中文技术媒体 RSS（机器之心 / 量子位）。"""
    feeds = ["https://www.jiqizhixin.com/rss", "https://www.qbitai.com/feed"]
    out = []
    for fu in feeds:
        txt = _http(fu, timeout=8)
        if not txt:
            continue
        for blk in re.findall(r"<item>(.*?)</item>", txt, re.S)[:max_n]:
            t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", blk, re.S)
            l = re.search(r"<link>(.*?)</link>", blk, re.S)
            if not t:
                continue
            title = re.sub(r"\s+", " ", t.group(1)).strip()
            if title:
                out.append({"source": "中文媒体", "title": title[:160],
                            "url": (l.group(1).strip() if l else ""), "summary": ""})
    return out[:max_n * 2]


def fetch_tech_all():
    """四个来源都试一遍，任何一个失败都不影响其它。"""
    got = {}
    for name, fn in (("arxiv", fetch_arxiv), ("github", fetch_github_trending),
                     ("hn", fetch_hn), ("cn", fetch_cn_media)):
        try:
            got[name] = fn()
        except Exception:                               # noqa: BLE001
            got[name] = []
    total = sum(len(v) for v in got.values())
    return got, total


# ---------------------------------------------------------------- LLM

def _llm_text(prompt, max_tokens=4200, temperature=0.55):
    try:
        from . import keyvault
        from .deepseek_client import chat_stream, DeepSeekError
    except Exception as e:                              # noqa: BLE001
        return None, "导入 LLM 客户端失败：%s" % e
    key = ""
    try:
        key = keyvault.get_key()
    except Exception:                                   # noqa: BLE001
        key = ""
    if not key:
        return None, "没有可用的 DeepSeek Key（先在顶栏 🔑 填一个）"
    try:
        buf = []
        for kind, txt in chat_stream(
                [{"role": "user", "content": prompt}],
                "deepseek-flash", key, temperature=temperature,
                max_tokens=max_tokens, thinking=False):
            if kind == "content" and txt:
                buf.append(txt)
        return "".join(buf).strip(), None
    except DeepSeekError as e:
        return None, "DeepSeek 失败：%s" % e
    except Exception as e:                              # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, e)


def _parse_json(txt):
    """LLM 常把 JSON 包在 ```json 里，或前后带解释文字。这里尽量抠出来。"""
    if not txt:
        return None
    s = txt.strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", s, re.S)
    if m:
        s = m.group(1).strip()
    try:
        return json.loads(s)
    except Exception:                                   # noqa: BLE001
        pass
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(s[i:j + 1])
        except Exception:                               # noqa: BLE001
            return None
    return None


# ---------------------------------------------------------------- 反思

def _journal_tail(n=24):
    """读自开发日志最后 n 条，作为"我最近改过什么"的素材。"""
    try:
        if not _JOURNAL.is_file():
            return []
        with open(_JOURNAL, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-n:]
        out = []
        for ln in lines:
            try:
                j = json.loads(ln)
            except Exception:                           # noqa: BLE001
                continue
            out.append({
                "ts": j.get("ts") or j.get("time") or "",
                "rel": j.get("rel") or j.get("file") or "",
                "note": (j.get("note") or "")[:160],
            })
        return out
    except Exception:                                   # noqa: BLE001
        return []


_PROJECT_BRIEF = """本项目是一个"本地多模态助手"（Python FastAPI 后端 + 原生 JS 前端）：
· 对话/工具调用（联网搜索、读网页、地图、天气、图片生成/微改、文档 PPT Excel 生成、视频剪辑、语音合成、数字人）
· 知识库检索、长期/短期记忆、技能（skill）系统、定时任务、自开发（能改自己的源码）
· 数字人模块：warp 形变口型（纯 CPU、6ms/帧）、闭嘴基准合成、SDXL 局部重绘
· 已有的自我改进通道：selfdev_patch / selfdev_write / selfdev_restart
前端文件：frontend/index.html、app.js(约1.2万行)、style.css
后端文件：backend/*.py（main.py 约1.4万行、tools.py、avatar_*.py、cloud_image.py、keyvault.py 等）"""


def _build_reflect_prompt(signals, tech, journal, focus=""):
    sig_lines = []
    for it in signals[-30:]:
        u = (it.get("user") or "").replace("\n", " ")[:220]
        a = (it.get("assistant") or "").replace("\n", " ")[:220]
        if u:
            sig_lines.append("- 用户：%s\n  我：%s" % (u, a))
    tech_lines = []
    for k, arr in (tech or {}).items():
        for it in (arr or [])[:6]:
            tech_lines.append("- [%s] %s %s" % (it.get("source"), it.get("title"),
                                                it.get("url") or ""))
    jr = ["- %s %s %s" % (x.get("ts"), x.get("rel"), x.get("note")) for x in journal[-40:]]

    return """你是这个本地多模态助手项目的"自我迭代引擎"。请做一次**诚实、具体、不客套**的自我审视。

%s

## 最近的对话（用户说了什么、你怎么回的）
%s

## 我最近已经做过的改动（⚠️ 这些**已经修过了**，绝对不要再把它们提成 action）
%s

## 最近的技术动态（真实抓取，可能与本项目无关，请自己判断相关性）
%s

## 你的任务
1. **自我反思**：最近哪些地方没做好？要具体到"哪类任务/哪个环节/为什么会这样"，
   不要写"我会更努力"这种空话。优先找**反复出现**的问题。
2. **对用户的观察**：从对话里看出用户的工作习惯、反复提出的诉求、被浪费的时间。
   只写有证据的，没证据就不要编。
3. **可增加的功能**：结合上面的技术动态和项目现状，提出真正值得做的功能。
   每条要说明"为什么值得做"和"大概怎么实现"。
4. **可执行的动作**：这是最重要的一项。给出**具体到文件**的改动计划，
   每条动作要能被单独批准/拒绝。风险等级自己判断（low=文案/参数/样式，
   mid=新增函数/接口，high=改核心逻辑）。
   ⚠️ 只允许改 backend/ 和 frontend/ 下的 .py/.js/.css/.html。
   ⚠️ 不要提议"重写整个模块"这种无法安全回滚的动作。
   ⚠️ 如果确实没有值得改的，actions 就给空数组 —— 不要为了凑数硬提。

## 输出格式（严格 JSON，不要任何额外文字）
{
  "summary": "一句话总结这次反思（30字内）",
  "weaknesses": [{"title":"短标题","detail":"具体问题+证据","severity":"high|mid|low"}],
  "user_insights": [{"title":"短标题","detail":"观察+证据","suggestion":"可以怎么改进"}],
  "features": [{"title":"功能名","why":"为什么值得做","how":"大概怎么做","effort":"S|M|L"}],
  "tech_takeaways": [{"title":"技术名","source":"来源","url":"链接","takeaway":"一句话要点","applicable":"对本项目有没有用，怎么用"}],
  "actions": [{"title":"短标题","detail":"具体改什么、为什么","risk":"low|mid|high","files":["backend/xxx.py"]}]
}

%s""" % (_PROJECT_BRIEF,
        "\n".join(sig_lines) or "（暂无）",
        "\n".join(jr) or "（暂无）",
        "\n".join(tech_lines) or "（本次没抓到，可能没联网）",
        ("## 用户这次的额外关注点\n" + focus) if focus else "")


def reflect(trigger="manual", use_web=True, focus=""):
    """跑一次完整反思，生成一个 plan（状态 pending，等用户批准）。"""
    _ensure()
    tech, tech_total = ({}, 0)
    if use_web:
        tech, tech_total = fetch_tech_all()
    sig = load_signals().get("items", [])
    journal = _journal_tail()

    prompt = _build_reflect_prompt(sig, tech, journal, focus=focus)
    txt, err = _llm_text(prompt, max_tokens=4200, temperature=0.55)
    if err:
        return {"ok": False, "error": err}

    data = _parse_json(txt)
    if not isinstance(data, dict):
        return {"ok": False, "error": "模型返回的不是合法 JSON",
                "raw": (txt or "")[:600]}

    actions = []
    for i, a in enumerate(data.get("actions") or []):
        if not isinstance(a, dict) or not (a.get("title") or "").strip():
            continue
        files = [f for f in (a.get("files") or []) if isinstance(f, str)]
        files = [_norm_rel(f) for f in files if _norm_rel(f)]
        if not files:
            files = []
        actions.append({
            "id": "a%d" % (i + 1),
            "title": str(a.get("title"))[:120],
            "detail": str(a.get("detail") or "")[:900],
            "risk": (a.get("risk") or "mid").lower(),
            "files": files[:_MAX_FILES_PER_ACTION],
            "status": "pending",          # pending/approved/rejected/done/failed
            "note": "",
            "error": "",
            "backup": "",
        })

    plan = {
        "id": _pid(),
        "ts": _now(),
        "trigger": trigger,
        "summary": str(data.get("summary") or "")[:200],
        "weaknesses": data.get("weaknesses") or [],
        "user_insights": data.get("user_insights") or [],
        "features": data.get("features") or [],
        "tech_takeaways": data.get("tech_takeaways") or [],
        "actions": actions,
        "tech_raw": {k: (v or [])[:6] for k, v in (tech or {}).items()},
        "tech_total": tech_total,
        "signals_used": len(sig),
    }

    d = load_plans()
    d["items"].insert(0, plan)
    d["items"] = d["items"][:60]          # 最多留 60 次
    save_plans(d)
    return {"ok": True, "plan": plan}


# ---------------------------------------------------------------- 审批

def _find_plan(pid):
    d = load_plans()
    for p in d["items"]:
        if p.get("id") == pid:
            return d, p
    return d, None


def approve(pid, action_ids=None, note=""):
    d, p = _find_plan(pid)
    if not p:
        return {"ok": False, "error": "找不到这次反思"}
    hit = 0
    for a in p.get("actions") or []:
        if action_ids and a["id"] not in action_ids:
            continue
        if a["status"] in ("pending", "rejected"):
            a["status"] = "approved"
            a["note"] = str(note or "")[:800]
            hit += 1
    save_plans(d)
    return {"ok": True, "approved": hit, "plan": p}


def reject(pid, action_ids=None, note=""):
    d, p = _find_plan(pid)
    if not p:
        return {"ok": False, "error": "找不到这次反思"}
    hit = 0
    for a in p.get("actions") or []:
        if action_ids and a["id"] not in action_ids:
            continue
        if a["status"] in ("pending", "approved"):
            a["status"] = "rejected"
            a["note"] = str(note or "")[:800]
            hit += 1
    save_plans(d)
    return {"ok": True, "rejected": hit, "plan": p}


# ---------------------------------------------------------------- 执行（带备份/校验/回滚）

def _norm_rel(rel):
    """规范化并校验路径；不合法返回空串。"""
    if not isinstance(rel, str):
        return ""
    s = rel.replace("\\", "/").strip().lstrip("./")
    low = s.lower()
    for bad in _FORBIDDEN_PARTS:
        if low.startswith(bad) or ("/" + bad) in ("/" + low):
            return ""
    if not any(low.startswith(p) for p in _ALLOWED_PREFIX):
        return ""
    if not low.endswith(_ALLOWED_EXT):
        return ""
    p = (_ROOT / s)
    try:
        p.resolve().relative_to(_ROOT.resolve())
    except Exception:                                   # noqa: BLE001
        return ""
    return s


def _read_text(rel):
    try:
        with open(_ROOT / rel, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:                                   # noqa: BLE001
        return ""


def _backup_files(files, tag):
    """备份要改的文件。

    返回 (tag, saved, created)：
      saved   = 已存在、已备份的文件
      created = 原本不存在（属于新建）的文件 —— 回滚时要把它们删掉
    """
    _ensure()
    dst = _BACKUPS / tag
    dst.mkdir(parents=True, exist_ok=True)
    saved, created = [], []
    for rel in files:
        src = _ROOT / rel
        if not src.is_file():
            created.append(rel)
            continue
        out = dst / rel.replace("/", "__")
        try:
            shutil.copy2(src, out)
            saved.append(rel)
        except Exception:                               # noqa: BLE001
            pass
    _write_json(dst / "_manifest.json",
                {"files": saved, "created": created, "ts": _now(), "tag": tag})
    return tag, saved, created


def _verify(rel):
    """改完做语法校验：.py 用 py_compile，.js 用 node --check（没有 node 就跳过）。"""
    p = _ROOT / rel
    if not p.is_file():
        return False, "文件不存在"
    if rel.endswith(".py"):
        try:
            r = subprocess.run([sys.executable, "-m", "py_compile", str(p)],
                               capture_output=True, timeout=60)
            if r.returncode != 0:
                return False, (r.stderr or b"").decode("utf-8", "replace")[-600:]
            return True, ""
        except Exception as e:                          # noqa: BLE001
            return False, "py_compile 失败：%s" % e
    if rel.endswith(".js"):
        try:
            r = subprocess.run(["node", "--check", str(p)],
                               capture_output=True, timeout=40)
            if r.returncode != 0:
                return False, (r.stderr or b"").decode("utf-8", "replace")[-600:]
            return True, ""
        except FileNotFoundError:
            return True, ""                             # 没装 node 就跳过
        except Exception:                               # noqa: BLE001
            return True, ""
    return True, ""


def _apply_find_replace(rel, find, replace):
    """精确替换（必须恰好命中 1 处），与 selfdev_patch 同一套语义。

    特例：find 为空且目标文件不存在 → 视为**新建文件**，直接写入 replace。
    （"建一个回归脚本"这类动作必须支持，否则永远执行不了。）
    """
    path = _ROOT / rel
    if not find and not path.is_file():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(replace or "")
            return True, ""
        except Exception as e:                          # noqa: BLE001
            return False, "新建文件失败：%s" % e
    txt = _read_text(rel)
    if not txt:
        return False, "读不到文件"
    if not find:
        return False, "find 为空"
    n = txt.count(find)
    if n == 0:
        return False, "find 在文件里找不到（可能已被改过）"
    if n > 1:
        return False, "find 命中 %d 处，不唯一" % n
    new = txt.replace(find, replace, 1)
    try:
        with open(_ROOT / rel, "w", encoding="utf-8") as f:
            f.write(new)
        return True, ""
    except Exception as e:                              # noqa: BLE001
        return False, "写入失败：%s" % e


def _plan_patch_for_action(plan, action):
    """让 LLM 针对这一条动作产出具体 patch（此时才读相关文件内容）。"""
    want = [f for f in (action.get("files") or []) if isinstance(f, str)]
    files = [f for f in want if (_ROOT / f).is_file()]
    new_files = [f for f in want if not (_ROOT / f).is_file()]
    if not files and not new_files:
        return None, "没有可读的目标文件"
    ctx = []
    for rel in files[:_MAX_FILES_PER_ACTION]:
        body = _read_text(rel)
        if len(body) > 14000:
            body = body[:14000] + "\n…（后略，文件过长，请只针对你确定存在的片段给 find）"
        ctx.append("### 文件：%s\n```\n%s\n```" % (rel, body))
    for rel in new_files[:_MAX_FILES_PER_ACTION]:
        ctx.append("### 待新建文件：%s\n（这个文件还不存在。要新建它，"
                   "patch 里把 find 写成空字符串 \"\"，replace 写完整文件内容。）" % rel)

    prompt = """你要在这个项目里实施一个已经获得用户批准的小改动。请输出**精确的查找替换**。

## 改动需求
标题：%s
说明：%s
用户备注：%s

## 涉及文件（当前内容）
%s

## 硬性要求
1. 只输出 JSON，不要任何解释文字。
2. find 必须与文件内容**一字不差**（含缩进和换行），并且在整个文件里**只出现一次**。
3. 改动要**最小化**，不要顺手重构无关代码，不要删掉现有功能。
4. 如果发现无法安全实施（比如需求与代码不符），就输出 {"skip":"原因"}。

## 输出格式
{"patches":[{"file":"backend/xxx.py","find":"原文片段","replace":"替换后片段"}]}
""" % (action.get("title"), action.get("detail"),
       action.get("note") or "（无）", "\n\n".join(ctx))

    txt, err = _llm_text(prompt, max_tokens=3600, temperature=0.2)
    if err:
        return None, err
    data = _parse_json(txt)
    if not isinstance(data, dict):
        return None, "模型没给出合法 JSON"
    if data.get("skip"):
        return None, "模型判断无法安全实施：%s" % str(data.get("skip"))[:200]
    patches = data.get("patches") or []
    if not isinstance(patches, list) or not patches:
        return None, "模型没有给出任何 patch"
    return patches, ""


def execute_approved(pid):
    """执行这次反思里所有 status=approved 的动作。带备份 + 校验 + 失败回滚。"""
    d, plan = _find_plan(pid)
    if not plan:
        return {"ok": False, "error": "找不到这次反思"}
    todo = [a for a in (plan.get("actions") or []) if a.get("status") == "approved"]
    if not todo:
        return {"ok": False, "error": "这次反思没有已批准的动作"}

    results = []
    for a in todo:
        tag = "%s_%s_%s" % (pid, a["id"], _pid())
        patches, err = _plan_patch_for_action(plan, a)
        if err:
            a["status"] = "failed"
            a["error"] = err
            results.append({"id": a["id"], "ok": False, "error": err})
            continue

        # 校验所有目标文件都在白名单内
        rels = []
        bad = ""
        for pt in patches:
            rel = _norm_rel(pt.get("file"))
            if not rel:
                bad = "目标文件不在允许范围：%s" % pt.get("file")
                break
            rels.append(rel)
        if bad:
            a["status"] = "failed"
            a["error"] = bad
            results.append({"id": a["id"], "ok": False, "error": bad})
            continue
        if len(set(rels)) > _MAX_FILES_PER_ACTION:
            a["status"] = "failed"
            a["error"] = "一次动作涉及文件过多（上限 %d）" % _MAX_FILES_PER_ACTION
            results.append({"id": a["id"], "ok": False, "error": a["error"]})
            continue

        # 备份（created 里是本次要新建的文件，回滚时会被删除）
        tag2, saved, created = _backup_files(list(dict.fromkeys(rels)), tag)
        a["backup"] = tag2

        # 应用
        done_rels, fail_msg = [], ""
        for pt in patches:
            rel = _norm_rel(pt.get("file"))
            ok, m = _apply_find_replace(rel, pt.get("find") or "", pt.get("replace") or "")
            if not ok:
                fail_msg = "%s：%s" % (rel, m)
                break
            done_rels.append(rel)

        if fail_msg:
            _restore(tag2)
            a["status"] = "failed"
            a["error"] = fail_msg + "（已自动回滚）"
            results.append({"id": a["id"], "ok": False, "error": a["error"]})
            continue

        # 语法校验
        vbad = ""
        for rel in dict.fromkeys(done_rels):
            ok, m = _verify(rel)
            if not ok:
                vbad = "%s 语法校验失败：%s" % (rel, m)
                break
        if vbad:
            _restore(tag2)
            a["status"] = "failed"
            a["error"] = vbad + "（已自动回滚）"
            results.append({"id": a["id"], "ok": False, "error": a["error"]})
            continue

        a["status"] = "done"
        a["error"] = ""
        results.append({"id": a["id"], "ok": True, "files": list(dict.fromkeys(done_rels)),
                        "backup": tag2})

    save_plans(d)
    ok_n = sum(1 for r in results if r["ok"])
    return {"ok": ok_n > 0, "done": ok_n, "total": len(results), "results": results,
            "plan": plan,
            "need_restart": ok_n > 0}


def _restore(tag):
    """把备份覆盖回去；本次新建的文件则删除。"""
    dst = _BACKUPS / tag
    man = _read_json(dst / "_manifest.json", {"files": []})
    for rel in man.get("files") or []:
        src = dst / rel.replace("/", "__")
        if src.is_file():
            try:
                shutil.copy2(src, _ROOT / rel)
            except Exception:                       # noqa: BLE001
                pass
    for rel in man.get("created") or []:
        try:
            p = _ROOT / rel
            if p.is_file():
                p.unlink()
        except Exception:                           # noqa: BLE001
            pass


def rollback(pid):
    """把某次反思里所有 done 的动作回滚到改动前。"""
    d, plan = _find_plan(pid)
    if not plan:
        return {"ok": False, "error": "找不到这次反思"}
    hit = 0
    for a in plan.get("actions") or []:
        if a.get("status") != "done" or not a.get("backup"):
            continue
        _restore(a["backup"])
        a["status"] = "pending"
        a["error"] = ""
        a["note"] = (a.get("note") or "") + " [已回滚 %s]" % _now()
        hit += 1
    save_plans(d)
    return {"ok": hit > 0, "rolled_back": hit, "plan": plan}


# ---------------------------------------------------------------- 汇总

def stats():
    d = load_plans()
    proposed = approved = done = rejected = failed = 0
    for p in d["items"]:
        for a in p.get("actions") or []:
            proposed += 1
            st = a.get("status")
            if st == "approved":
                approved += 1
            elif st == "done":
                done += 1
                approved += 1
            elif st == "rejected":
                rejected += 1
            elif st == "failed":
                failed += 1
    rate = round(done / proposed * 100.0, 1) if proposed else 0.0
    return {"proposed": proposed, "approved": approved, "done": done,
            "rejected": rejected, "failed": failed, "rate": rate,
            "plans": len(d["items"])}


def overview():
    """前端抽屉一次拿全：最新一次反思、待批准数、采纳率、信号数。"""
    d = load_plans()
    items = d.get("items") or []
    latest = items[0] if items else None
    pending = 0
    approved = 0
    for p in items:
        for a in p.get("actions") or []:
            if a.get("status") == "pending":
                pending += 1
            elif a.get("status") == "approved":
                approved += 1
    sig = load_signals().get("items", [])
    return {
        "ok": True,
        "latest": latest,
        "pending": pending,
        "approved_waiting": approved,
        "signals": len(sig),
        "stats": stats(),
        "history": [{"id": p.get("id"), "ts": p.get("ts"), "summary": p.get("summary"),
                     "trigger": p.get("trigger"),
                     "actions": len(p.get("actions") or [])} for p in items[:12]],
    }


def get_plan(pid):
    d, p = _find_plan(pid)
    if not p:
        return {"ok": False, "error": "找不到这次反思"}
    return {"ok": True, "plan": p}

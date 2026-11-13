# -*- coding: utf-8 -*-
"""Hardwordy Agent —— FastAPI 后端服务
用法:
    py -3 -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
或直接:
    py -3 run.py
"""
from fastapi import (FastAPI, HTTPException, UploadFile, WebSocket, WebSocketDisconnect,
                     Response, File, Header)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import (StreamingResponse, FileResponse, PlainTextResponse,
                                HTMLResponse)
from pydantic import BaseModel

import os
import queue
import re
import sys
import base64
import codecs
import io
import json
# ⚠️ 必须显式导入 urllib.parse：只写 `import urllib` 是拿不到 .parse 的，
#    而 `_fake_doclib_links()` 要靠它把链接里的 `%E4%BC%AA...` 还原成中文名。
#    漏了这行的话，unquote 会抛 NameError 被 except 吞掉 →
    #    每个**真的**中文名文件都会被误判成"并不存在"（假警报满天飞）。
import urllib.parse
import urllib.request
import math
import time
import asyncio
import datetime
import logging
import threading
from . import (config, ollama_client, deepseek_client, cloud_image, image_models,
               selfdev, scheduler, perf, model_manager, selftest, memory, kb,
               file_tools, video, web_tools,
               t2i, tools, sessions, image_library, doclib, docx_write, tts,
               workspace, actlog, chatimg, hive, hive_client, hive_p2p,
               hive_mesh, avatar2)

app = FastAPI(title="hardwordy", version="1.0.0")

# ⚠️ 本机地址一律不走系统代理（2026-11-11）。
#   开发机上经常挂着 http_proxy（抓包 / 科学上网 / 各种客户端自带），
#   requests 和 urllib 会老实把 http://127.0.0.1:11434 也发给代理，
#   拿回来的不是 Ollama 而是一个 502 Bad Gateway —— 表现就是
#   「Ollama 明明在跑，界面却说连不上 / 模型下载不了」，而且极难查。
#   这里把本机地址加进 NO_PROXY（追加，不覆盖用户已有的设置）。
#   · requests 走 get_environ_proxies()，会读 NO_PROXY
#   · urllib 走 proxy_bypass_environment()，同样读 NO_PROXY
#   注意：外网请求（DeepSeek 等）照旧走代理，这里只豁免本机回环地址。
for _k in ("NO_PROXY", "no_proxy"):
    _cur = os.environ.get(_k, "")
    _add = [h for h in ("127.0.0.1", "localhost", "::1", "0.0.0.0")
            if h not in [x.strip() for x in _cur.split(",") if x.strip()]]
    if _add:
        os.environ[_k] = (",".join(filter(None, [_cur] + _add)))


# ⚠️ 必须在任何子进程启动之前装上：之后所有 subprocess.Popen（含只读模块
#    backend/selfdev.py 里的 node --check、git）都不会再弹控制台黑窗。
#    用户反馈：「调用工具时总有一些窗口闪来闪去，很影响观感」。
_NOWIN_STATUS = {}
try:
    from . import procwin as _procwin
    _procwin.install_nowin_patch()
    _NOWIN_STATUS = _procwin.status()
except Exception as _e:            # noqa: BLE001
    # 不能静默吞掉：补丁没装上 = 用户会看到黑窗一闪一闪，而代码里"看起来是好的"。
    _NOWIN_STATUS = {"error": "%s: %s" % (type(_e).__name__, _e)}
client = ollama_client.OllamaClient()

# ⚠️ 这个 **必须**有：本文件多处用 `logger.xxx` 打日志，
# 但一直只 `import logging` 却没建 logger —— 于是每次走到都是
# `NameError: name 'logger' is not defined`。
# 最要命的是它发生在**后台线程**里（模型预热、code-server 残留回收）：
# 线程挂了不报错、不留痕，功能就这么静默地"从来没生效过" ——
# 用户看到的现象是"打开开发台还是要干等十几秒"，
# 而代码里明明写着预热。是启动器把后端 stdout 落到 %TEMP%\mm_backend.log 后才抓到。
logger = logging.getLogger("uvicorn.error")
logger.info("[procwin] 黑窗防护状态：%s", _NOWIN_STATUS)

# 启动时就把知识库目录建好 —— 用户可以直接往里拷 .txt / .md，
# 不用先"导入"一次才知道往哪放。
try:
    kb.ensure_dir()
except Exception:
    pass

# 最近一次出现过的图片（base64），供后续「把这张图改成…」直接微改，免去重新导入。
# 单用户桌面应用，保存最近一张即可；换新图时自动覆盖。
#
# ⚠️⚠️ 2026-10-17：**必须落盘**。以前只存内存，应用一重启（改代码 / 崩溃 / 正常退出）
#   "上一张图"就没了 → 用户说「把刚才那张改成…」时底图为空 →
#   工具回「无法确定要修改的图片，请拖入对话」→ 用户被迫重新上传。
#   用户原话：「文生图之后我想让它微改图片，却显示没有看到图片要我重新上传，
#   我不想重新上传」。
_LAST_IMAGE: dict = {"b64": None, "ts": 0.0, "ttl": 3600.0}
_LAST_IMAGE_NAME = "last_image.json"


def _last_image_path() -> str:
    return config.data(_LAST_IMAGE_NAME)


def _load_last_image() -> None:
    """启动时把上次的「最近一张图」读回来（仍在有效期内才算）。"""
    try:
        p = _last_image_path()
        if not os.path.isfile(p):
            return
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        b64 = d.get("b64")
        ts = float(d.get("ts") or 0)
        if b64 and (time.time() - ts) < _LAST_IMAGE["ttl"]:
            _LAST_IMAGE["b64"] = b64
            _LAST_IMAGE["ts"] = ts
    except Exception:
        logger.warning("读取上次的图片失败（不影响使用）", exc_info=True)


def _env_ds_key() -> str:
    """服务器自用：Key 放环境变量（/etc/multimodal.env，权限 600）。

    为什么不写进 config.json：那个文件会随打包/同步到处跑，容易泄露；
    systemd EnvironmentFile 只在服务进程环境里，`systemctl show` 也看不到值。
    """
    # ★ 2026-11-09：环境变量之外，再读**本机保管**的 Key（data/secrets.json）。
    #   原因：定时任务半夜跑时浏览器不在，拿不到 localStorage 里的 Key，
    #   原来只能退回本地 Ollama → 用户报「定时任务无法连接 ollama」。
    k = str(os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if k:
        return k
    try:
        from . import keyvault
        return keyvault.get_key()
    except Exception:
        return ""


def _ds_key(header_key: str = "") -> str:
    """请求头里的 Key 优先（用户在浏览器填的），没有再退回服务器环境变量。"""
    return (header_key or "").strip() or _env_ds_key()


def _remember_image(images: list) -> None:
    """记住本轮图片，供下一轮引用（**同时落盘**，重启后还能接着微改）。"""
    if images:
        _LAST_IMAGE["b64"] = images[-1]
        _LAST_IMAGE["ts"] = time.time()
        try:
            p = _last_image_path()
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"b64": _LAST_IMAGE["b64"], "ts": _LAST_IMAGE["ts"]}, f)
            os.replace(tmp, p)
        except Exception:
            logger.warning("保存上次的图片失败（本次仍可用）", exc_info=True)


try:
    _load_last_image()
except Exception:
    pass


def _recent_image() -> list:
    """取回最近一张仍在有效期内的图片（列表形式，未过期才返回）。"""
    b64 = _LAST_IMAGE.get("b64")
    if b64 and (time.time() - _LAST_IMAGE.get("ts", 0)) < _LAST_IMAGE.get("ttl", 3600):
        return [b64]
    return []


def _img_available_note() -> str:
    """本轮要附在最后一条用户消息里的「上一张图仍可用」提示（没图就返回空）。

    ⚠️ 2026-10-17 实测：用户报「文生图之后想微改，却显示没看到图片要我重新上传」。
    除了"状态只在内存"之外还有一条更直接的：**模型根本不知道有那张图** ——
    实测在新会话里说「把刚才那张图改成黑白风格」，模型会**原地复读**这一句
    （后端日志 `[think-loop] 思考打转：同一段重复出现`），十分钟不动作、也不调工具。
    所以只要后端还留着图，就明确告诉它"能改、别让用户重传"。
    """
    if not _recent_image():
        return ""
    return ("（系统提示：**上一张图片仍然可用** —— 后端还留着用户之前生成/上传的那张图。"
            "⚠️ 你**不需要先「看到」这张图**才能改：`edit_image` 会自己从后端把它取出来当底图。"
            "所以本轮只要用户是在说它（「把刚才那张改成…」「再改一下」「微改这张」等），"
            "**就直接调用 edit_image**，不要追问「哪张图」、不要弹框问用户、"
            "更不要要求重新上传或说「没看到图片」；"
            "只有用户明确要一张全新的图时，才需要他重新提供。）")


# 「这一轮是不是还在说上一张图」的判据。
#
# ⚠️⚠️ 为什么必须有：`_recent_image()` 的**有效期是 1 小时**，而 `ctx["images"]`
# 原来写的是 `images or _recent_image()` —— 等于**用户拖过一次图之后的整整一小时里，
# 每一轮都会被系统当成"这一轮的图"**。三个真实后果：
#   ① 「把这张图改成蓝色」之外的**全新话题**，也会拿着那张旧图去当底图（微改/参考图）；
#   ② `writing_mode` 被旧图挡掉（`and not _recent_image()`）→ 写长文退化成普通问答；
#   ③ `simple_q` 也不收紧 → 白等更久。
# 用户 2026-09-22 报的「上一轮画水母，这一轮要旅行计划，结果又出了水母的图」
# 就是这一类**跨轮串扰**。⇒ 只有在用户**明确指向**上一张图时才复用。
#
# ⚠️ 判据要**双条件**：「指代词」或「改图动作」**单独出现都不算** ——
# "帮我写份文档，**换成** Word" 里的"换成"跟图片毫无关系，
# 只看动词会把旧图接到文档任务上（那就是另一种串扰）。
_IMG_WORDS = ("图", "照片", "图片", "海报", "壁纸", "插画", "头像", "图标",
              "logo", "抠图", "画")
_IMG_DEIXIS = ("这张", "那张", "这幅", "那幅", "此图", "该图", "原图", "参考图",
               "上图", "刚才", "刚刚", "上一张", "上张", "前一张",
               "上面那", "上面这")
_IMG_EDIT = ("改成", "改为", "换成", "再改", "继续改", "修一下", "调一下",
             "加个", "去掉", "裁剪", "放大", "微改")
# ⚠️ **只可能用在图片上**的动作词：出现它们就等于在说图片，不需要再配"图"字或指代词。
#    （用户实测原话：「我直接发文字让它给刚才生成的图片进行微改」→ 说了"微改"。）
#    2026-09-26 补「去背景 / 换背景」：抠图与合成功能上线后用户的常见说法，
#    同样只可能是在说图片（"去掉背景音乐"另有措辞，这里加"去背景"三连字已足够窄）。
_IMG_ONLY_VERBS = ("微改", "改图", "修图", "p图", "P图", "抠图", "加滤镜", "调色",
                   "去背景", "换背景")
# 外观类词：配合"改图动作"判断（「改成红色」这种短句常常不带"图"字）
_IMG_LOOK = ("红", "蓝", "绿", "黄", "黑", "白", "灰", "紫", "橙", "颜色", "色系",
             "亮", "暗", "背景", "尺寸", "大小", "比例", "风格", "清晰")


def _refers_to_prev_image(text: str) -> bool:
    """这句话是不是在指「上一轮那张图」。

    宁漏勿误：判不出来就**不复用**旧图（大不了让用户重拖一次，或让工具明确报
    "无法确定要修改的图片"），因为"把上一轮的图悄悄接到新话题上"是更糟的错误。
    """
    t = str(text or "")
    if not t:
        return False
    has_img = any(w in t for w in _IMG_WORDS)
    # ① 明确指向"某张图"：图相关词 + 指代词
    if has_img and any(d in t for d in _IMG_DEIXIS):
        return True
    # ② 在改这张图：图相关词 + 改图动作
    if has_img and any(e in t for e in _IMG_EDIT):
        return True
    # ③ 短句只说"改成红色 / 调亮一点"这类（没带"图"字，但说的是外观）
    if any(e in t for e in _IMG_EDIT) and any(w in t for w in _IMG_LOOK):
        return True
    # ④ ⚠️ **图片专属的动作词**：这些词只会用在图片上（"微改文档"不是人话），
    #    所以单独出现就够 —— 用户实测常说「帮我微改一下」「改图」「修图」，
    #    不带"图"字也不带指代词，前三条全都判不出来 → 底图取不到 →
    #    edit_image 只能回「无法确定要修改的图片」（2026-09-22 报的"找不到刚才的图"）。
    if any(w in t for w in _IMG_ONLY_VERBS):
        return True
    return False


def _prev_image_for(text: str) -> list:
    """本轮可复用的"上一张图"：只有用户明确指向时才给。"""
    return _recent_image() if _refers_to_prev_image(text) else []


# 启动时做一次轻量清理：移除「太久未用 + 几乎没内容」的僵尸会话。
# 有实际内容的会话一律保留；真正重要的信息由长期记忆承载，不靠聊天记录堆积。
try:
    _cleaned_sessions = sessions.cleanup_old()
except Exception:
    _cleaned_sessions = 0


def _now_str() -> str:
    """返回本地当前时间的中文描述，供注入对话，让模型具备时间感知。"""
    now = datetime.datetime.now()
    wd = "一二三四五六日"[now.weekday()]
    return f"{now.year}年{now.month}月{now.day}日（星期{wd}），{now.strftime('%H:%M:%S')}"


def _memory_ctx(session: str, last_user_text: str, cfg: dict) -> str:
    """组装要注入的**记忆**内容（长期+短期 + 目标/计划的用法说明）。

    ⚠️⚠️ 这段**不能放进系统提示**（2026-09-22 实测）：

    系统提示渲染在**工具定义之前**，所以系统提示里任何一处变化，都会让它后面的
    整段（38 个工具定义 ≈ 13000 token）一起作废，Ollama 只能整份重新预填充
    （~10 秒）。而记忆恰好是"每轮都会变"的：

      · 后台的记忆提炼**每一轮都可能改写短期记忆**；
      · 换一个对话（session）就是另一份短期记忆。

    实测（同一会话连问 5 轮，每轮之间等 14 秒让提炼跑）：
    首字 15.2s / 17.4s / 15.4s / **0.78s** / 17.3s —— 只要记忆被改写就打回原形。

    所以改由 `_attach_live_ctx` 挂到**最后一条用户消息**上（排在工具定义之后）。
    返回空串表示没有可注入的记忆。
    """
    if not (cfg or {}).get("memory_enabled", True):
        return ""
    try:
        ctx = memory.build_context(session, query=last_user_text)
    except Exception:
        logger.warning("读取记忆失败（本轮不注入）", exc_info=True)
        return ""
    if not ctx:
        return ""
    # 目标/计划类记忆的用法：当背景用，别当催命符；有进展就更新。
    return ctx + "\n\n" + (
        "【关于记忆里的目标与计划】\n"
        "- 记忆里可能有用户的目标、计划、答应过要做的事。把它们当**已知背景**，\n"
        "  不要每轮都追问进展，也不要每次回答都提一遍。\n"
        "- 但话题相关时可以自然地关心一句，或提醒关键时间点（临近报名/考试/截止）。\n"
        "- 用户说「做完了 / 没做成 / 改主意了 / 不打算做了」时，**立刻更新记忆**：\n"
        "  用 remember 工具，action=\"update\"，old 填记忆里的原句，content 填最新状态；\n"
        "  彻底放弃的用 action=\"forget\" 删掉。**别让档案里留着过期目标。**\n"
        "- 用户透露**新目标**时，除了记进记忆，还要给**可执行的规划建议**：\n"
        "  拆成几步、每步做什么、大致什么时间做，并指出最容易卡住的地方。\n"
        "  不要只说「加油」「坚持就是胜利」这种空话。")


def _sched_ctx() -> str:
    """定时任务的**现状摘要**（全局，跨会话共享）。

    2026-11-11 用户要求：「外面无论单开多少对话，所有对话都可以访问定时任务的
    完成情况以及内容，**是否查看由模型自主决断**」。

    所以这里只给一行摘要（有几个、最近一次跑的是什么、200 字预览），
    并明确告诉模型「要不要细看由你判断、要看就调 list_schedules」。
    ⚠️ 正文**不塞进来**：这块挂在用户消息上，每轮都发，塞全文会白烧上下文。
    """
    try:
        from . import scheduler as _sch
        jobs = _sch.list_jobs()
    except Exception:
        return ""
    if not jobs:
        return ""
    n_on = sum(1 for j in jobs if j.get("enabled"))
    last = None
    for j in jobs:
        lr = str(j.get("last_run") or "")
        if lr and (last is None or lr > str(last.get("last_run") or "")):
            last = j
    bits = ["共 %d 个（启用 %d）" % (len(jobs), n_on)]
    if last:
        bits.append("最近一次「%s」%s %s" % (
            last.get("name"), str(last.get("last_run") or "")[5:16],
            str(last.get("last_status") or "")[:20]))
        prev = str(last.get("last_preview") or "").strip().replace("\n", " ")
        if prev:
            bits.append("摘要：" + prev[:80] + ("…" if len(prev) > 80 else ""))
    return ("【定时任务的现状】（全局共享，任何对话看到的都是同一批；"
            "**要不要细看、要不要跟用户提，由你自己判断** —— 跟本轮无关就别念）\n· "
            + "；".join(bits)
            + "\n· 想看全部任务或某次运行的完整结果 → 调 list_schedules"
              "（要正文加 with_result=true）。")


def _attach_live_ctx(working: list, *, digest: str = "", mem_ctx: str = "",
                     rag_ctx: str = "", img_ctx: str = "", skill_ctx: str = "",
                     sched_ctx: str = "") -> list:
    """把**每轮会变的**内容（历史摘要/记忆/知识库检索/当前时间）挂到**最后一条用户消息**上。

    返回一份新列表；`working` 本身不动。

    ⚠️⚠️ 为什么这些内容不能放进系统提示 —— 2026-09-22 实测出来的
    「发出去半天没反应」的根因：

    Ollama 的前缀缓存按 **token 前缀**复用 KV 缓存。而系统提示渲染在
    **工具定义之前**（qwen 模板把 tools 拼在第一条 system 里），所以系统提示里
    任何一处变化，**它后面的整段（含 38 个工具定义 ≈ 13000 token）全部作废**。
    而系统提示里恰好塞了四样「每轮都会变」的东西：

      ① 第一行是「当前时间：…HH:MM:SS」（带秒）→ 每个 token 都变；
      ② 知识库材料（RAG）是**按本轮问题检索**的 → 换一个问题就不同；
      ③ 记忆会被后台提炼**每轮改写** → 同一会话里也会不停变；
      ④ 历史摘要会随上下文裁剪而变。

    后果（Ollama 日志原文，实跑取证）：
      · `f_sim_best = 0.18` —— 只认出 18% 的公共前缀，几乎等于没命中；
      · `prompt eval time = 10607 ms / 19837 tokens` —— **10.6 秒全花在重算前缀上**。

    把这些内容挪到**最后一条用户消息**里，它就排在「系统提示 + 工具定义」**之后**，
    那一大段稳定前缀就能一直被复用。实测（同一台机器，修复前后）：
      · 换问题（知识库开着）：首字 11.1s → **0.2s 量级**
      · 同一句话连发：11.6s → **0.12s**
      · 同一会话连问 5 轮（记忆被改写）：15~17s → **0.2~0.8s**

    ⚠️ 必须**复制**一条消息再改：`working` 与落盘用的历史共享同一批 dict，
    原地改会把记忆/检索材料/时间戳写进聊天记录（越积越多、还会污染后续轮次）。
    """
    out = list(working)
    # ⚠️⚠️ **上一条用户消息没有得到回复**（用户点了「■ 终止」，或历史是旧版本存的）：
    #    模型会把它当成"未完成的需求"，在新的一轮里**接着做** —— 实测复现：
    #    历史里放「生成一张图片，关于狐狸（无回复）」+「红腹锦鸡长什么样？」，
    #    模型当场又调了一次 generate_image（出狐狸图），而且**完全没回答锦鸡那一问**。
    #    这正是用户 2026-09-22 报的"图库里掺杂了一张上个任务关于狐狸的图片"。
    #
    # ⚠️ 不能另外插一条 system 消息提醒（Ollama 的 qwen 模板只用**第一条** system，
    #    后面的一律被静默丢弃 —— 这个坑踩过），所以并进本轮用户消息的开头。
    _dangling = any(out[k].get("role") == "user" and out[k + 1].get("role") == "user"
                    for k in range(len(out) - 1))
    for i in range(len(out) - 1, -1, -1):
        if out[i].get("role") != "user":
            continue
        m = dict(out[i])
        q = str(m.get("content") or "")
        blocks = []
        if _dangling:
            blocks.append(
                "（提醒：**上一条**用户消息没有得到你的回复 —— 用户当时主动中断了那次生成。"
                "那条需求**已经作废**，不要和这一条合并、也不要顺手把它做掉；"
                "只回答本轮这一条。用户要是还想做，会再明确说一次。）")
        if digest:
            blocks.append("【较早对话的摘要】（更早的内容已被折叠，"
                          "需要细节就问用户）\n" + digest)
        if mem_ctx:
            blocks.append(mem_ctx)
        if rag_ctx:
            blocks.append(rag_ctx)
            # ⚠️ 必须点明"这是文档、不是用户本人"：实测（2026-09-22）知识库里那份
            #    《入团申请书》里有个学生名字，模型随后写旅行攻略时把它当成了**用户的名字**
            #    （标题写成「【某某某专属版】」）。资料来自用户导入的文档，
            #    里面的人物不等于用户本人。
            blocks.append("（以上是系统按你的问题自动检索到的**文档资料**，"
                          "来自用户导入的文件 —— **不等于用户本人的情况**，"
                          "别把它里面的人名/身份当成用户的；"
                          "请优先依据它作答并注明出自哪一篇；"
                          "与问题无关就忽略，不要硬扯。）")
        if skill_ctx:
            blocks.append(skill_ctx)
        if sched_ctx:
            blocks.append(sched_ctx)
        if img_ctx:
            blocks.append(img_ctx)
        if blocks:
            blocks.append("【用户这一轮说的话】")
        blocks.append(q)
        blocks.append("\n\n（当前时间：" + _now_str() + "）")
        m["content"] = "\n\n".join(blocks)
        out[i] = m
        break
    return out


# 简单问题识别：命中则收紧生成长度，避免模型对"你好"这类问题长篇思考。
# qwen3-vl 的 thinking 无法通过 API 关闭（think=false /no_think 均实测无效，
# 提示词引导反而让它想更多），**限制 num_predict 是唯一有效手段**：
# 实测「你好」从 24.3s 降到 3.9s。
_COMPLEX_HINTS = (
    "写", "画", "生成", "做一份", "方案", "报告", "代码", "脚本", "文件", "搜索",
    "查一下", "查查", "总结", "翻译", "分析", "对比", "设计", "规划", "计划",
    "记住", "帮我", "为什么", "怎么", "如何", "详细", "解释", "列出", "整理",
    # 图片相关一律走完整模式：微改需要输出工具调用，收紧长度会被思考吃光
    "图", "照片", "图片", "这张", "那张", "刚才", "上面", "改成", "换成", "修改",
)


def _is_simple_question(text: str) -> bool:
    """判断是否属于可快速作答的简单问题。

    注意：只做**文本**层面的判断；调用方还需确认本轮没有图片上下文
    （见 chat()），否则模型可能来不及输出工具调用。
    """
    t = (text or "").strip()
    # 阈值从 30 收到 14：问句短 ≠ 答案短。
    # 「Python 的列表和元组有什么区别」19 字，却要写几百字才讲得清。
    if not t or len(t) > 14:
        return False
    # 带疑问/求解释意味的一律不当简单问题
    if any(k in t for k in ("什么", "为什么", "怎么", "如何", "区别", "对比",
                            "介绍", "解释", "分析", "讲讲", "说说", "写", "帮")):
        return False
    return not any(k in t for k in _COMPLEX_HINTS)


# 简单问题的**起手配额**（思考+回答总量）——⚠️ 2026-09-26 起它不再是"篇幅上限"：
# 用户明确要求「取消所有篇幅限制」，各档位一律提到与天花板同级，
# 真正能用多少只由「num_ctx − 提示词占用」决定（重试逻辑会按实测剩余窗口夹住）。
# 打这么高不会拖慢短回答：模型自己 EOS 就停了，这个值只是"允许它写多长"。
# ⚠️ 原来是 512，实测**明显不够**：「Python 的列表和元组有什么区别」这种
# 19 字的问题会被 `_is_simple_question` 判成"简单"，512 token 写一半就断，
# 用户看到的是"配额不足/回答被截断"。这类问题只是**问句短**，不代表答案短。
# 1536 仍会偶尔截断，再提到 3072 —— 用户明确要求"尽量不要限制"。
# 问候语这类真正的一问一答根本用不到 3072，自然停下，不会变慢。
SIMPLE_MAX_TOKENS = 8192

# 联网场景的生成长度下限：要把搜索结果喂给模型 + 让它逐条列出来源链接，
# token 消耗远高于普通问答。给少了就会出现"搜索完了但没输出回答"。
WEB_MAX_TOKENS = 16384
# 长文创作（作文/方案/报告）的输出上限。
# 这类任务要真写出几百上千字，还要留足"思考"的额度 —— 4096 实测经常写不完，
# 而且这一轮已经把用不到的工具 schema 砍掉了，腾出的空间正好给它。
WRITING_MAX_TOKENS = 16384

# 「写代码 / 做项目」这一轮的输出上限。
# 实测踩过：用默认额度（2048）让模型写一个完整文件时，输出**中途被截断** ——
# 生成出来的 todo.py 停在 `print(f'{index}. {task[`，一跑就 SyntaxError。
# 写整份代码文件 + 工具调用 JSON 的开销，比普通问答大得多，必须单独给足。
CODE_MAX_TOKENS = 16384

# 输出长度天花板：空回答重试时加倍，但不能无限涨
# （上下文窗口还要留给提示词与历史，超出只会让 Ollama 截断提示词）
MAX_TOKENS_CEILING = 24576

# 送入模型的历史消息上限（约 60 轮）。
# 之前是 40 条（20 轮），实测**玩"成语接龙"这种多轮小游戏时不够**：
# 超过 20 轮以后，第 1 轮的内容会被裁掉，用户回头问"第一轮接的是什么"，
# 模型只能说"忘记了"。轮次密集的短对话并不占多少 token，放宽到 120 条。
# （真正把关的是下面的 token 预算，它会按 num_ctx 自动裁剪，所以这里放大是安全的。）
MAX_CONTEXT_MESSAGES = 120


# ============ 「回答是不是停在半句上」的判断（2026-11-09）============
# 为什么需要它：
#   用户反复报「回答只有一半」。查下来本地通道只在 done_reason == "length"
#   时才续写；可 Ollama **经常给出 done_reason="stop"**（看起来正常结束），
#   正文却停在半句话上 —— 模型自己吐了结束符、或上游把最后一段吞了。
#   这种「假正常结束」以前既不续写、也不提示，用户看到的就是"说到一半没了"。
_UNFINISHED_TAIL = set("，,、；;：:（(【[《<「『“‘")
_FINISHED_TAIL = set("。！？!?….\"'”』」）)】]》>`~*|")
# 这些词出现在结尾 → 句子明显没说完（"把主题告诉我，我" 就断在「我」上）
# ★★ 2026-11-09：**双字连词/介词结尾**同样是半句话。
#   用户截图实测：「…接下来想聊什么，或者」——上面表里只有单字「或」，
#   而 endswith("或") 对「或者」不成立 → 判不出来 → 既不续写也不提示，
#   用户又看到一条断在「或者」上的回复。这类词后面必然还有话。
_CONNECTIVE_TAILS = ("或者", "还有", "还是", "但是", "不过", "可是", "然而",
                     "因此", "于是", "接着", "另外", "此外", "同时", "至于",
                     "关于", "对于", "除了", "无论", "不管", "即使", "虽然",
                     "尽管", "要么", "而是", "并非", "以便", "从而", "进而",
                     "一旦", "要是", "那么", "的话")
_DANGLING_WORDS = ("我", "你", "他", "她", "它", "的", "地", "得", "和", "与",
                   "或", "但", "而", "就", "也", "都", "所以", "因为", "如果",
                   "然后", "并且", "而且", "以及", "包括", "例如", "比如")


def _looks_unfinished(text: str) -> bool:
    """正文是否**明显停在半句上**（用于「假正常结束」的自动续写判断）。

    宁可不续写，也不要让模型把已经答完的内容重写一遍 —— 所以只认强信号：
      · 结尾是逗号 / 顿号 / 分号 / 冒号 / 左括号 这类"后面必然还有话"的符号；
      · 结尾是「我 / 的 / 但 / 所以…」这类明显的半句词；
      · 或：正文够长（≥200 字）且**最后 40 字里一个句末标点都没有**。
    """
    s = (text or "").rstrip()
    if not s:
        return False
    tail = s[-1]
    # 双字连词结尾（「…或者」「…但是」）句子必然没说完；endswith("或") 认不出「或者」。
    for w in _CONNECTIVE_TAILS:
        if s.endswith(w):
            return True
    if tail in _FINISHED_TAIL:
        return False
    if tail in _UNFINISHED_TAIL:
        return True
    for w in _DANGLING_WORDS:
        if s.endswith(w):
            return True
    # 弱信号：长正文 + 最后 40 字没有任何句末标点 → 大概率被切了
    if len(s) >= 200 and not any(ch in _FINISHED_TAIL for ch in s[-40:]):
        return True
    return False


def _est_tokens(text) -> int:
    """粗略估算 token 数：中文约 1.2 字/token，其余约 3.5 字符/token。

    只需要"够准到能做预算"，不追求精确——故意偏保守（宁可少带历史）。
    """
    if not text:
        return 0
    s = str(text)
    cn = sum(1 for c in s if "\u4e00" <= c <= "\u9fff")
    return int(cn / 1.2) + int((len(s) - cn) / 3.5) + 1


def _history_digest(dropped: list, per_msg: int = 90, cap: int = 1500) -> str:
    """把被裁掉的早期对话压成一段摘要，避免模型"完全不记得开头"。

    为什么不用 LLM 做摘要：那要额外一轮推理（几秒起步），还会把首字延迟拉长；
    而这里的目标只是"别忘光"。逐条抽取 + 截断就够用了 ——
    对"成语接龙"这类短消息场景几乎是无损的，长消息也留住了开头信息。

    ⚠️ 标签必须写「用户 / 助手」，不能写「我 / 你」。
    实测：写成"我/你"时模型会搞混指代，把摘要里的内容当成了别的东西，
    用户问"第一轮接的是什么"照样答错。系统提示里没有"我/你"的明确指向，
    换成角色名才不会有歧义。

    注意：这是**兜底**，不是主力。真正的历史仍然尽量完整保留（见 MAX_CONTEXT_MESSAGES）。
    """
    if not dropped:
        return ""
    lines, total, rnd = [], 0, 0
    pending_user = None
    for m in dropped:
        role = m.get("role")
        text = " ".join(str(m.get("content") or "").split())
        if not text:
            continue
        if len(text) > per_msg:
            text = text[:per_msg] + "…"
        if role == "user":
            pending_user = text
            rnd += 1
            continue
        if role == "assistant":
            if pending_user is not None:
                lines.append(f"【第{rnd}轮】用户：{pending_user}　｜　助手：{text}")
                pending_user = None
            else:
                lines.append(f"（早期）助手：{text}")
        if total + len(text) > cap:
            break
        total += len(text)
    if pending_user is not None:
        lines.append(f"【第{rnd}轮】用户：{pending_user}")
    if not lines:
        return ""
    return ("【较早对话摘要｜必须记住】以下内容是本次对话**开头**的部分，"
            "因为长度限制已移出上下文，但你**仍然要记得**。\n"
            "用户若问起「最开始」「第一轮」「开头」聊了什么，就依据本段回答：\n"
            + "\n".join(lines))


# 历史至少要留这么多 token。⚠️⚠️ 见 _trim_history_to_budget 里的说明 ——
# 这一条是"模型老是失忆"的正面修复：以前输出预留（max_tokens）不设上限地挤占历史，
# 结果 `24576 - 8192(输出) - 16025(系统提示+工具) - 3800(联网预留) - 512 = -3953`
# → **负数** → 历史每轮被砍到只剩最后 2 条，其余压成 743 字摘要。
MIN_HISTORY_TOKENS = 2500


def _recent_turns_index(messages: list, rounds: int = 8,
                        user_chars: int = 60, bot_chars: int = 110) -> str:
    """把最近几轮压成一份「速览」，专治多轮对话里的指代失忆。

    为什么需要它（2026-11-10 用户报「上下文记忆不行，没两轮就失忆」）：
      实测下来**历史并没有被裁掉**（1M 窗口下历史预算 63 万 token，MAX_CONTEXT_MESSAGES=120），
      但用户问「我是说**胡萝卜**那个笑话」时，模型会拿**最近一条**回答顶替，
      然后一本正经地解释错的那个。本质是"在几千字历史里做关键词对齐"失败了，
      不是上下文丢了。
      所以这里把最近 8 轮的「用户说了什么 + 我的回答开头」单独列一遍，
      给模型一个可以直接比对的锚点 —— 用户提到「胡萝卜」，它一眼能扫到第几轮。

    ⚠️ 标签必须写「用户 / 你的回答」，理由同 _history_digest：写「我/你」会让模型搞混指代。
    ⚠️ 只取开头若干字：目的只是"能被认出来"，不是复述全文（全文本来就在历史里）。
    """
    pairs = []
    pending = None
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        text = " ".join(str(m.get("content") or "").split())
        if not text:
            continue
        if role == "user":
            pending = text[:user_chars] + ("…" if len(text) > user_chars else "")
        elif role == "assistant" and pending is not None:
            pairs.append((pending,
                          text[:bot_chars] + ("…" if len(text) > bot_chars else "")))
            pending = None
    if pending is not None:
        pairs.append((pending, "（未回复）"))
    pairs = pairs[-max(1, rounds):]
    if not pairs:
        return ""
    total = len(pairs)
    lines = ["· 第%d轮（倒数第%d轮）｜用户：%s｜你的回答开头：%s"
             % (i, total - i + 1, u, a)
             for i, (u, a) in enumerate(pairs, 1)]
    return ("【最近 %d 轮速览｜用来对齐「刚才那个 / 上一个 / 第几个」这类指代】\n"
            % total + "\n".join(lines))
# 「较早对话摘要」的预留：摘要是在裁剪**之后**才生成的，之前完全没进预算，
# 等于白送最多 1500 字（~1200 token）→ 也是撑爆窗口的一份子。这里先扣掉。
DIGEST_RESERVE = 700


def _image_tokens(images_b64: list) -> int:
    """估算这一轮附件图片要吃掉多少 token。

    ⚠️⚠️ 以前**完全没算**：图片挂在最后一条 user 消息上发给 Ollama
    （见 ollama_client.chat 的 `images_base64`），但预算只按文字长度估 ——
    用户拖一张大图进来就会撑爆窗口，Ollama 直接回 400：
      `request (26352 tokens) exceeds the available context size (24576 tokens)`
    （2026-09-22 用户截图里的报错就是这个）。

    ⚠️⚠️ 2026-09-26 **按实测校准**（原来低估 3 倍，是"带图提问就 400"的根因之一）：
      公式 `ceil(w/28)*ceil(h/28)/4` 给 1024×1024 算出 **342**，
      而 Ollama **实报 1028** —— 差别在于 Ollama 会把图规整到自己的目标分辨率，
      所以**同一张图 512×512 与 1024×1024 都是 1028 token**（实测两次一致）。
      ⇒ 改成"按张数估、每张 1030"，并且**超过 1024 时才按面积放大**
        （上游 `_shrink_for_model` 已经会把图缩到长边 1024，所以正常情况下就是 1030/张）。
    ⚠️ 宁可**高估**：多留一点空间只是少带两条历史，估少了就是直接报错。
    """
    total = 0
    for b64 in (images_b64 or []):
        # 每张图固定约 1030 token（见上面实测）；只有超过 1024 才按面积放大。
        per = 1030
        try:
            from PIL import Image as _PILImage      # 懒导入：没装 PIL 也能跑
            raw = base64.b64decode(str(b64).split(",")[-1], validate=False)
            with _PILImage.open(io.BytesIO(raw)) as im:
                side = max(im.size)
            if side > 1024:                          # 超大的才线性放大
                per = int(1030 * (side / 1024.0) ** 2)
        except Exception:
            pass
        total += per
    return total


def _no_limit() -> bool:
    """用户要求"限制都去掉"：返回 True 时不做任何输入裁剪。"""
    if str(os.environ.get("MM_NO_OUTPUT_LIMIT", "")).lower() in ("1", "true", "yes"):
        return True
    try:
        return bool((config.load_config() or {}).get("no_output_limit"))
    except Exception:
        return False


def _clamp_message_text(text: str, max_tokens: int) -> tuple:
    """把过长的**单条**消息截到 max_tokens 以内（**保头保尾**）。

    返回 (新文本, 原字数, 保留字数)。

    ⚠️⚠️ 2026-09-26 补，实测的缺口：裁剪逻辑原来只丢**历史**，而"用户这一条"
    永远保留 —— 于是用户**粘贴一篇几万字的文档**时，提示词会直接顶爆窗口
    （实测：4 万字输入 ≈ 3.1 万 token，加系统提示与工具定义共 4.8 万 > num_ctx 24576），
    Ollama 回 400，用户只拿到一句"内容超出长度"。**这是我们的缺口，不是模型装不下。**

    保头保尾而不是只保头：结尾常常才是真正的诉求
    （「……以上是全部材料，请帮我写一份总结」），只留开头会把它丢掉。
    用二分找"截到多少字刚好装得下"，比按比例硬猜准（中英文 token 密度差很多）。
    """
    s = str(text or "")
    if _no_limit() or max_tokens <= 0 or not s:      # 用户要求"去掉限制"：不裁剪任何消息
        return text, s, False
        return s, len(s), len(s)
    if _est_tokens(s) <= max_tokens:
        return s, len(s), len(s)
    head_ratio = 0.55
    best = ""
    lo, hi = 0, len(s)
    while lo <= hi:
        mid = (lo + hi) // 2
        h = int(mid * head_ratio)
        t = mid - h
        mark = "\n\n…（中间省略 %d 字）…\n\n" % max(0, len(s) - mid)
        cand = s[:h] + mark + (s[len(s) - t:] if t else "")
        if _est_tokens(cand) <= max_tokens:
            best = cand
            lo = mid + 1
        else:
            hi = mid - 1
    if not best:                      # 极端情况：连一小段都装不下
        best = s[: max(1, int(len(s) * 0.1))]
    return best, len(s), len(best)


def _trim_history_to_budget(messages: list, sys_prompt: str, tool_schemas: list,
                            cfg: dict, images_b64: list | None = None) -> tuple:
    """按上下文预算裁剪历史消息。

    token 账：num_ctx = 系统提示 + 工具定义 + 附件图片 + 检索材料 + 历史 + 本轮输出。
    除了历史，其它都是"写死的开销"，所以这里从最旧的开始丢，直到装得下。

    ⚠️⚠️ **输出预留（max_tokens）不能无上限地挤历史**（2026-09-22 修）：
    用户把 max_tokens 调到 8192 之后，账变成
      `24576 − 8192(输出) − 16025(系统提示+工具) − 3800(联网预留) − 512 = −3953`
    是**负数** → 走下面那条兜底分支，历史每轮只剩最后 2 条、其余压成 743 字摘要。
    用户体感就是"**模型老是失忆**"。
    修法：输出预留只当**上限**，并且历史至少要能拿到 MIN_HISTORY_TOKENS。

    **返回 (保留的历史, 被丢掉的历史, 截断信息)**。
    丢掉的那部分不是直接扔 —— 调用方会把它压成「较早对话摘要」注入，
    否则用户回头问"开头聊了什么"，模型会一脸茫然（实测踩过）。
    截断信息是 dict（{"orig","kept","cut"）或 None：只有"单条消息本身太长、
    被截断"时才有值，用来**如实告诉用户**他粘贴的内容被裁了（见 _clamp_message_text）。
    """
    try:
        ctx_limit = int(cfg.get("num_ctx") or 8192)
        reserve_out_cfg = int(cfg.get("max_tokens") or 2048)
    except Exception:
        ctx_limit, reserve_out_cfg = 8192, 2048

    import json as _json
    overhead = (_est_tokens(sys_prompt)
                + _est_tokens(_json.dumps(tool_schemas, ensure_ascii=False)))
    # 联网时，工具结果（8 条检索结果 + 若干篇网页正文）会在工具循环里
    # 追加进上下文，此时还不知道具体多大 —— 按实测约 3600 token 预留，
    # 否则"历史 + 检索材料"一起会撑爆窗口，Ollama 直接截断提示词。
    search_reserve = 3800 if cfg.get("web_enabled") else 0
    img_tokens = _image_tokens(images_b64)
    room = ctx_limit - overhead - search_reserve - img_tokens - DIGEST_RESERVE - 512

    def _clamp_all(kept_list: list, budget_tokens: int):
        """把 kept 里**装不下的单条消息**截断（保头保尾）。返回截断信息或 None。"""
        info = None
        per_msg = max(512, int(budget_tokens))
        for i, m in enumerate(kept_list):
            t = m.get("content") or ""
            if not isinstance(t, str) or _est_tokens(t) <= per_msg:
                continue
            new_t, orig, keptc = _clamp_message_text(t, per_msg)
            if keptc < orig:
                m2 = dict(m)
                m2["content"] = new_t
                kept_list[i] = m2
                info = {"orig": orig, "kept": keptc, "cut": orig - keptc}
        return info

    if room <= 512:
        # 连固定开销都快占满了：只带最近 2 条，别把提示词撑爆
        tail = messages[-2:]
        return tail, messages[:-2], _clamp_all(tail, max(256, room))

    # 输出愿留多少就留多少，但**不许把历史挤没**（见函数说明）
    reserve_out = min(reserve_out_cfg, max(512, room - MIN_HISTORY_TOKENS))
    budget = room - reserve_out
    if budget <= 0:
        tail = messages[-2:]
        return tail, messages[:-2], _clamp_all(tail, max(256, room))

    kept, used = [], 0
    for i, m in enumerate(reversed(messages)):
        if len(kept) >= MAX_CONTEXT_MESSAGES:
            break
        t = _est_tokens(m.get("content") or "")
        if kept and used + t > budget:
            break
        kept.append(m)
        used += t
    kept.reverse()
    dropped = messages[: len(messages) - len(kept)]
    # ⚠️ 最后一步：**单条消息自己也得装得下**（用户粘贴几万字文档的场景，见上面的说明）
    return kept, dropped, _clamp_all(kept, budget)


# 工具轮里单条消息的上限。工具结果动辄上万字（网页正文 9000 字 ≈ 6700 token），
# 一条就能吃掉四分之一窗口。超过就**截断**（保头保尾）而不是整条丢掉 ——
# 丢掉会让模型以为"没查到"，截断至少保住主要信息。
ROUND_MSG_MAX_TOKENS = 4000


def _shrink_round_prompt(working: list, cfg: dict, tool_schemas: list) -> int:
    """工具轮之间再核一次预算：装不下就先压大的、再丢最旧的。返回估算 token 数。

    ⚠️⚠️ 2026-09-26 补。整轮开始前只裁过一次历史和用户输入，但**工具轮是越跑越长的**：
    每一轮都会把工具结果追加进 working（web_read 一篇网页 9000 字 ≈ 6700 token），
    多跑两轮必然顶爆窗口。顶爆后的兜底是"砍到只剩最后两条消息"——
    那等于**把刚查到的材料全丢了**，用户看到的就是"查了却没回答"。
    所以在每轮真正发请求前压一次：先截断超长的单条，仍然超再从最旧的丢
    （system 永不丢，它是唯一的规则来源；至少留 3 条，别把本轮也丢掉）。
    """
    import json as _json
    try:
        ctx = int(cfg.get("num_ctx") or 8192)
    except Exception:
        ctx = 8192
    overhead = _est_tokens(_json.dumps(tool_schemas or [], ensure_ascii=False))
    room = ctx - overhead - DIGEST_RESERVE - 512

    def _total():
        return sum(_est_tokens(m.get("content") or "")
                   for m in working if isinstance(m, dict))

    for i, m in enumerate(working):
        if not isinstance(m, dict) or m.get("role") == "system":
            continue
        t = m.get("content") or ""
        if isinstance(t, str) and _est_tokens(t) > ROUND_MSG_MAX_TOKENS:
            new_t, _o, _k = _clamp_message_text(t, ROUND_MSG_MAX_TOKENS)
            m2 = dict(m)
            m2["content"] = new_t
            working[i] = m2

    # ② 还装不下 → 从最旧的**非 system** 消息开始丢。
    #    ⚠️ 三样东西**永不丢**：system（唯一的规则来源）、最后一条（当前进展）、
    #    **最近一条 user 消息**（那是用户的诉求本身 —— 丢了它就等于答非所问，
    #    而它前面那些"更旧的"才是该先牺牲的）。
    def _protected():
        keep = set()
        for i, m in enumerate(working):
            if isinstance(m, dict) and m.get("role") == "system":
                keep.add(i)
        if working:
            keep.add(len(working) - 1)
        for i in range(len(working) - 1, -1, -1):
            if isinstance(working[i], dict) and working[i].get("role") == "user":
                keep.add(i)
                break
        return keep

    while _total() > room:
        prot = _protected()
        idx = next((i for i, m in enumerate(working)
                    if isinstance(m, dict) and i not in prot), None)
        if idx is None:
            break                     # 只剩受保护的那些，不能再丢了
        working.pop(idx)

    # ③ 丢到只剩受保护的**还装不下**（提示词 + 工具定义本身就快占满窗口）→
    #    把最大的那条再压一压（通常就是工具结果/长材料）。
    #    宁可这一轮材料少一点，也不能让请求直接 400 —— 那连回答都没有。
    _guard = 0
    while _total() > room and _guard < 6:
        _guard += 1
        cand = [(len(m.get("content") or ""), i) for i, m in enumerate(working)
                if isinstance(m, dict) and m.get("role") != "system"
                and isinstance(m.get("content"), str) and m.get("content")]
        if not cand:
            break
        _ln, _i = max(cand)
        _room_it = max(256, _est_tokens(working[_i]["content"]) - (_total() - room))
        _new_t, _o, _k = _clamp_message_text(working[_i]["content"], _room_it)
        if _k >= _ln:
            break
        _m2 = dict(working[_i])
        _m2["content"] = _new_t
        working[_i] = _m2
    return _total()


# 触发自动记忆的信号词：出现这些词说明用户可能透露了值得长期记住的信息。
# 只在命中时才做后台提炼，避免每轮都多跑一次模型。
#
# 之前这张表太窄（只有"记住/我是/我的…"），结果用户说
# 「我平时用 Python」「以后别用英文回答我」「我们公司用的是飞书」
# 这类明显该记的信息，因为没命中关键词而**根本没进长期记忆**。
# 这里按"身份 / 偏好 / 约定 / 项目"四类补全。
_MEMORY_SIGNALS = (
    # 显式要求
    "记住", "别忘", "记一下", "记录一下", "提醒我", "以后", "下次", "今後",
    # 身份 / 背景
    "我叫", "我是", "我的名字", "叫我", "我在", "我们公司", "我们单位", "我们学校",
    "我读", "我学", "专业", "职业", "岗位", "生日", "年龄", "岁",
    # 偏好 / 习惯
    "我喜欢", "我不喜欢", "我习惯", "我平时", "我一般", "我通常", "偏好",
    "讨厌", "不喜欢", "受不了", "最好", "不要用", "别用", "要用",
    # 约定 / 约定俗成
    "约定", "规定", "统一", "默认", "一律", "每次都要", "以后都", "风格",
    "语气", "格式", "称呼",
    # 项目 / 工作
    "我的项目", "我在做", "我们在做", "正在开发", "技术栈", "用的是什么",
    "环境是", "部署在", "服务器", "版本是",
    # 经历 / 成果 —— 这类信息**天然不带任何标记词**，是漏记最多的一类：
    # 用户讲自己的经历时不会说"记住"，只会平铺直叙"我做过…"。
    "做过", "搞过", "写过", "参加过", "拿过", "获过", "得过", "考过", "学过",
    "我用过", "我熟悉", "我负责", "我参与", "我曾经", "我以前", "以前在",
    "比赛", "获奖", "拿奖", "证书", "实习", "兼职", "项目经验", "实验室",
    # 技能 / 工具
    "我会用", "我会写", "熟练", "习惯用", "常用",
)


def _looks_memorable(text: str) -> bool:
    return any(k in (text or "") for k in _MEMORY_SIGNALS)


# ---------------------------------------------------------------
#  后台记忆提炼的调度（防抖）
# ---------------------------------------------------------------
# 以前是"命中信号词才提炼 + 每 4 轮兜底一次"，两个毛病：
#   · 只分析**最后一条用户消息**，前面几轮讲的重要信息（尤其经历类）永远轮不到；
#   · 兜底要等满 4 轮，聊天一停就再也不会提炼了。
#
# 现在改成：**每轮结束都安排一次提炼**，但做防抖 ——
# 提炼要占用同一个 Ollama 模型（实测约 25 秒），立刻跑会把紧接着的下一条消息堵在队列里。
# 所以：用户停手 6 秒后再提炼；若一直不停嘴，最迟 45 秒也强制提炼一次。
# 每次带上**最近几轮**对话，前几轮讲的事不会漏。
_MEM_DEBOUNCE_SEC = 6.0        # 停手这么久才动
_MEM_FORCE_AFTER_SEC = 45.0    # 连续聊天时，最迟这么久必须提炼一次
_MEM_WINDOW_TURNS = 8          # 每次最多带上最近 8 条消息（约 4 轮）

_mem_pending: dict = {}
_mem_lock = threading.Lock()

# ⚠️ 后台提炼会**独占 Ollama**（它一次只跑一个请求），而提炼实测要 85~250 秒，
# 万一再触发一次翻倍重试就是 ~500 秒。用户在这个窗口里发消息，
# 只能干等到提炼跑完 —— 表现就是"问了半天没反应、像卡死了"（2026-09-15 实测踩到）。
# 两道防线：
#   ① 有聊天在跑时**绝不启动**提炼，推后 15 秒再试（见 _run_memory_extract）；
#   ② 万一聊天来的时候提炼已经在跑，给它发一条 note 说明原因，
#      至少用户知道"是在整理记忆"，而不是以为程序死了。
_chat_busy = 0          # 正在进行的聊天请求数
_extracting = False     # 后台记忆提炼是否正在跑
_extract_abort = False  # 用户开始说话了 → 让提炼主动断开，把显卡还回去
_busy_lock = threading.Lock()


def _chat_started() -> bool:
    """有聊天请求进来：登记 + 通知正在跑的提炼"让路"。

    返回 **这条之外是否还有聊天在跑** —— Ollama 一次只服务一个生成，
    所以"有别人在跑"就等于"这条要等"，得提前告诉用户（见 _track_chat）。
    """
    global _chat_busy, _extract_abort
    with _busy_lock:
        _chat_busy += 1
        _extract_abort = True
        return _chat_busy > 1


def _chat_finished() -> None:
    global _chat_busy
    with _busy_lock:
        _chat_busy -= 1


async def _track_chat(agen):
    """包住聊天流：登记"有聊天在跑"，并顺带告知用户**这条为什么可能慢**。

    ⚠️ Ollama 一次只跑一个生成（单 slot）。所以"模型被别的东西占着"是真实存在的
    等待来源，而用户看到的现象只是"半天没反应"。实测（2026-09-22）：
    两条聊天并发时，先发的那条会在吐 1 个思考字之后**静默 26 秒**，
    界面上完全看不出在等什么。⇒ 两种占用都提前说出来。
    """
    _other_chat = _chat_started()      # 登记 + 让提炼让路；返回"是否已有别的聊天在跑"
    try:
        if _extracting:
            yield json.dumps({"note": (
                "正在后台整理上一轮的记忆，模型被占用，这次回复会稍慢一些…")}) + "\n"
        elif _other_chat:
            yield json.dumps({"note": (
                "还有一条回复正在生成，模型一次只跑一个，你这条要等它让出来…")}) + "\n"
        async for chunk in agen:
            yield chunk
    finally:
        _chat_finished()


def _schedule_memory_extract(session: str, turns: list, model: str, cfg: dict,
                             urgent: bool = False) -> None:
    """安排一次后台记忆提炼（防抖 + 串行，详见上面的说明）。"""
    now = time.time()
    with _mem_lock:
        st = _mem_pending.get(session) or {}
        old_timer = st.get("timer")
        if old_timer:
            old_timer.cancel()
        first_ts = float(st.get("first_ts") or now)
        buf = (list(st.get("turns") or []) + list(turns or []))[-_MEM_WINDOW_TURNS:]
        st["turns"] = buf
        st["first_ts"] = first_ts
        st["timer"] = None
        if st.get("running"):
            # 上一轮提炼还没跑完：这次的内容已经并入缓冲区，等它跑完再来
            _mem_pending[session] = st
            return
        delay = 0.0 if (urgent or (now - first_ts) >= _MEM_FORCE_AFTER_SEC) else _MEM_DEBOUNCE_SEC
        t = threading.Timer(delay, _run_memory_extract, args=(session, model, cfg))
        t.daemon = True
        st["timer"] = t
        _mem_pending[session] = st
        t.start()


def _run_memory_extract(session: str, model: str, cfg: dict) -> None:
    global _extracting, _extract_abort
    # 有聊天在跑就先让路（详见上面 _chat_busy 的说明）。
    # 注意：这里**不能动 st["turns"]** —— 我们只是推迟，不是消费。
    with _busy_lock:
        busy = _chat_busy > 0
    if busy:
        t = threading.Timer(15.0, _run_memory_extract, args=(session, model, cfg))
        t.daemon = True
        with _mem_lock:
            st = _mem_pending.get(session) or {}
            st["timer"] = t
            _mem_pending[session] = st
        t.start()
        return
    with _mem_lock:
        st = _mem_pending.get(session) or {}
        turns = list(st.get("turns") or [])
        # ⚠️ **先别清空 turns**：万一这次被用户打断（或失败），
        # 这批对话还得留着下次再来一遍，否则这几轮的内容就永远记不上了。
        st["first_ts"] = time.time()
        st["running"] = True
        _mem_pending[session] = st
    with _busy_lock:
        _extracting = True
        _extract_abort = False        # 新的提炼开始，重新接受打断信号
    ok = False
    try:
        ok = bool(_auto_extract_memory(session, turns, model, cfg,
                                       abort=lambda: _extract_abort))
    except Exception:
        # ⚠️ 这里**不能**静默 pass。
        # 自动记忆整个链路跑在后台线程里，出错了界面上完全看不出来 ——
        # 用户只会觉得"模型又不记事"，而日志里一个字都没有，无从排查（踩过）。
        logging.getLogger("uvicorn.error").warning(
            "自动记忆提炼失败（session=%s）", session, exc_info=True)
    finally:
        with _busy_lock:
            _extracting = False
        with _mem_lock:
            st = _mem_pending.get(session) or {}
            st["running"] = False
            if ok:
                st["turns"] = []      # 成功了才清；被打断/失败留着下次重试
            _mem_pending[session] = st


# 抽取调用的输出预算。
# ⚠️ **这里是"该记的没记住"的真正原因，务必看清**：
# qwen3 系列是思考型模型，做这类抽取时会**先思考 2400~2600 字**才动笔。
# 实测（qwen3-vl:8b，同一段输入）：
#     num_predict = 300  → done_reason=length，content **一个字都没有**
#     num_predict = 500  → 同上（_sweep_one 原来是这个值）
#     num_predict = 1500 → 同上
#     num_predict = 4000 → 有时能出（长这样），有时思考超长仍然**空手而归**
#     num_predict = 8000 → **连跑 3 次全部成功**（2026-09-15 实测）
# 老代码给 300，于是**每一次自动提炼都在"空结果"上静默 return** ——
# 只有用户明确说"记住"时走的 remember 工具（聊天路径预算大）才写得进去。
# 试过 /no_think、系统提示写"不要思考"、把指令写得极简：**全都压不住它**，
# 唯一的解法就是把思考的额度给够。
# 代价：这一步耗时**波动很大（实测 84s ~ 246s）**，跑在后台线程里。
# 所以下面的 _run_memory_extract 出任何问题都必须打日志 —— 否则用户只看到
# "怎么又不记事"，而我们手上一点线索都没有。
_EXTRACT_TOKENS = 8000
_EXTRACT_TOKENS_RETRY = 12000


def _llm_extract(prompt: str, model: str, cfg: dict,
                 budget: int = _EXTRACT_TOKENS, abort=None) -> str:
    """跑一次"要点抽取"调用，返回模型正文；拿不到就返回空串。

    两个必须守住的点：
    1. **num_ctx 必须与聊天一致**（所以直接复用 cfg）—— Ollama 一旦发现
       num_ctx 与已加载的不同，就会卸载并重载模型（约 5 秒），
       而**重载会中断正在进行的生成**。老代码写死 4096 就是这么把回答打断的。
    2. 预算要够思考用（见 _EXTRACT_TOKENS）；万一还是被截断又没出正文，
       自动把预算翻倍重试一次 —— **绝不能静默失败**。

    abort：可选的 `()->bool`。传了就改用**流式**请求，每收到一块问一次；
    返回 True 说明"用户开始聊天了，让出模型"，立刻断开并返回 ""。
    背景：Ollama 一次只跑一个请求，提炼动辄 85~250 秒 ——
    不打断的话，用户这时发消息会**一直卡到提炼跑完**。
    """
    # ★ 2026-11-08：模型没下载时别硬撞 404（后台会刷一屏
    #   `model 'qwen3-vl:30b' not found`，记忆提炼静默失败）。
    try:
        _have = _installed_models()
        if _have and model not in _have:
            model = (next((n for n in _have if "qwen3-vl" in n), "")
                     or _have[0])
    except Exception:
        pass
    params = dict(cfg)
    params["temperature"] = 0.2
    params["max_tokens"] = budget
    for _ in range(2):
        try:
            if abort is None:
                resp = client.chat([{"role": "user", "content": prompt}], model=model,
                                   stream=False, params=params)
                data = resp.json() if hasattr(resp, "json") else resp
                msg = data.get("message") or {}
                text = (msg.get("content") or "").strip()
                reason = data.get("done_reason")
            else:
                text, reason = _llm_extract_stream(model, prompt, params, abort)
                if text is None:            # 被中断：直接把机会还给用户，不重试
                    return ""
            if text:
                return text
            # 正文为空：多半是思考把额度吃光了 → 加预算再来一次
            if reason != "length":
                return ""
        except Exception:
            logging.getLogger("uvicorn.error").warning(
                "记忆提炼调用失败（model=%s, budget=%s）", model, params.get("max_tokens"),
                exc_info=True)
            return ""
        # ⚠️ 重试的预算必须**不小于**上一次 —— 原来是直接赋 _EXTRACT_TOKENS_RETRY，
        # 一旦调用方传了更大的 budget（比如 8000），重试反而用回 6000，
        # 等于"越试越少"，必然还是空。取两者较大的那个。
        params["max_tokens"] = max(_EXTRACT_TOKENS_RETRY,
                                   int(params.get("max_tokens") or 0) * 2)
    return ""


def _llm_extract_stream(model: str, prompt: str, params: dict, abort):
    """流式跑提炼，中途可被 abort() 打断。

    返回 (正文, done_reason)；被打断时返回 (None, "")。
    用流式的唯一目的就是**能中途放手** —— 一旦发现用户开始说话，
    立刻退出 with 块（连接关闭 → Ollama 停掉这次生成），把显卡让回去。
    """
    text = ""
    reason = ""
    resp = None
    try:
        resp = client.chat([{"role": "user", "content": prompt}], model=model,
                           stream=True, params=params)
        # iter_lines 是阻塞读 —— 这里本来就是后台线程，直接读没问题。
        # chunk_size 给小一点，abort() 的响应才及时（否则要等一大块读完）。
        for raw in resp.iter_lines(chunk_size=64, decode_unicode=False):
            if abort and abort():
                return None, ""
            line = raw.decode("utf-8", "ignore").strip() if isinstance(raw, bytes) else raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            c = (obj.get("message") or {}).get("content")
            if c:
                text += c
            if obj.get("done"):
                reason = obj.get("done_reason") or ""
    except Exception:
        logging.getLogger("uvicorn.error").warning("流式提炼中断/失败", exc_info=True)
        return (text.strip() or None), reason
    finally:
        # 一定要显式关掉：关连接 = 告诉 Ollama 别再算了，把显卡让出来
        try:
            if resp is not None:
                resp.close()
        except Exception:
            pass
    return text.strip(), reason


# 判定"该不该记"的标准。要点是**把判断权交给模型**，而不是靠关键词 ——
# 关键词必然漏（"我做过一个考勤系统"里没有任何"记住/我是"）。
_EXTRACT_PROMPT = """你是"长期记忆整理员"。阅读下面的对话，挑出**值得长期留存**的内容。

【一条总原则，先想清楚再动手】
这份长期记忆是**"关于这个人的档案"** —— 写进去的每一条，都要能回答
「**这条对了解他、或对他以后要做的事有用吗？**」
凡是**和用户无关**的东西（客观资料、一次性查询的结果、本应用自己的操作说明），
不管看起来多"有用"，都**不许写进去**。

⚠️ 但要留的**远不只是"填表式档案"**。凡是**和他这个人有关**的都要记 ——
· 可以是**他自己**（身份、经历、技能、偏好、目标、顾虑、情绪）
· 可以是**他身边的人或组织**（家人、同学、老师、同事、导师、学校、公司、社团）
· 可以是**他身边的事物**（他用的电脑/手机/车/相机/宠物、他住的地方、他的项目）
· 可以是**他未来可能的意图与行为**（哪怕只是苗头、只是问了一嘴）
🔑 判断标准只有一条：**"这是关于他的吗？"** 是 → 记；不是 → 不记。

【必须提取】（只要出现就写下来）
· 身份背景：姓名、年龄、职业、学校/单位、专业、居住地、家庭情况
· **身边的人与组织**（最容易漏）：他提到的家人/朋友/同学/老师/同事/导师是谁、
  跟他什么关系、什么情况；以及学校、公司、社团、报名过的机构。
  例：`妹妹在读高三，明年高考`、`导师姓陈，要求每周交一次进度`
· **身边的事物**：他用的设备（电脑/显卡/手机/相机）、他的车、宠物、住处、
  正在做的项目/作品。例：`显卡是 RTX 5070 Ti（12GB）`、`养了只叫豆豆的猫`
· 经历成果：做过什么项目、参加过什么比赛或活动、干过什么工作、拿过什么奖
· 技能工具：会哪些编程语言、用过什么软件/框架/硬件、熟练程度
· 偏好习惯：喜欢/讨厌什么、希望怎么回答、惯用工作方式
· 约定规则：要求以后都遵守的规则、称呼、格式、语气
· **称呼与命名**（最容易漏，务必留意）：用户给助手起的名字，以及给某人/某物/
  某个项目/某个功能的**独特叫法**。听到「以后叫你 X」「我叫它 Y」这类话，
  就记成"以后怎么叫"的形式，**必须进长期记忆**。
  例：用户说「以后我就叫你小千」→ `长期|以后用「小千」称呼助手`
· **他自己项目里的约定**（不是本应用的操作说明！）：
  只记"**他**这个项目怎么定的、踩过什么坑、用了什么参数"，因为以后还要接着做。
  例：`长期|他的 smoke-detection 项目最终选定 YOLOv12s`
  ❌ 不记：`修改文件要用 edit_office 传文件名` —— 那是**本应用怎么用**，
     和"他这个人"无关，属于工具说明，不该占长期记忆。
· 目标计划：想学什么、打算做什么、正在准备什么、答应过要做的事
· 目标进展：之前提过的目标/计划有了新进展（做完了、没做成、改主意了、放弃了）

【必须推断的意图】（用户**没有明说**、但从提问方式能看出来的 —— 这一类最容易被漏掉）
只要他是在**说自己的事**（哪怕用"如果/假如/要是"这种假设说法），就是在透露意向：
· 「我能不能 / 可不可以 / 值不值得 / 适不适合 / 要不要 / 该不该 …」→ 正在**权衡**
· 「怎么才能 … / 需要什么条件 / 要准备什么 / 难不难 / 来得及吗」说的是自己 → **行动意向**
· 「**如果 / 假如 / 要是 / 万一** 我要做 X…」→ 正在**认真评估 X 的可行性**。
  ⚠️ **假设句同样要记**：他会用假设句，往往正说明还在犹豫 —— 这恰恰最该记下来。
· 「我以后想 / 将来打算 / 有机会的话我想 …」→ **远期意向**
· 「先做 A 还是先做 B / 什么时候做合适」→ 正在**排计划**
· 「我一直在纠结 / 考虑很久了 / 反复想过」→ **长期关注点**
· 同一话题反复追问、或问完又问细节 → **长期关注点**
· ⚠️ **不限于上面这几种句式** —— 只要话里透出"**他以后可能会做什么、需要什么、
  会在意什么**"，就写下来，哪怕只是随口一提、只是"以后再说"。包括：
  · 他抱怨的东西（`嫌笔记本风扇吵` → 以后可能想换机器）
  · 他羡慕/打听别人有的东西（`同学都在用 XX` → 可能也想搞一个）
  · 他提前做的准备（`先把资料存下来`、`先占个坑` → 在为以后铺路）
  · 他对某个领域的兴趣苗头（`最近老是在看 XX 的东西`）
  · 他提到的时间点（`等放假了`、`明年`、`等我毕业` → 那之后可能有事要发生）
· 写进记忆时**必须以「（推断）」开头**，让人一眼看出这是推出来的、不是他说的。
  例：用户问「自考本科怎么样？我未来可不可以去考？」
  → `长期|（推断）用户在考虑通过自考提升学历，正在权衡要不要报考`
· ⚠️ **推断要"到位"，不能只甩一句"在权衡"** —— 把**能看出来的阶段**写清楚：
  他是在了解条件、在算时间成本、在纠结值不值，还是已经在准备了？
  他问的是哪个环节，就把哪个环节写出来（只写"在权衡"等于没说，他自己知道在权衡）。
  · 问「如果我要考教师资格证，需要准备些什么？」
    → 差：`（推断）在权衡是否考教师资格证`（没说出任何新信息）
    → 好：`（推断）有意考教师资格证，已在了解报考条件与备考内容`
  · 问「假如我明年考研，现在开始来得及吗？」
    → 好：`（推断）在评估明年考研的可行性，关心从当下开始备考的时间是否充足`
· **不要**推断的：纯客观知识提问（「什么是…」「…的历史」「…分几类」）、
  一次性查资料；也**不要替他把话说满** —— 他说"在考虑"就记"在考虑"，
  别记成"决定要考"（推断过头会让后面的对话全跑偏）
· ⚠️ **写「（推断）」之前先查【已知信息】里有没有同一件事** ——
  推断只是补充"他没明说但能看出来的那部分"，**不是换个措辞把已知的事再记一遍**。
  例：已知信息里已有「计划考自考本科，当前在权衡」，就**不要**再写
  「（推断）正在权衡是否报考自考本科」—— 这是同一条，重复会把档案撑成同义句堆。
  判断标准：**去掉「（推断）」后，和已知条目说的是不是同一件事？** 是 → 不写。
· ⚠️ **同一主题只写一条**（这条最容易犯）：一个话题往往"既有事实又有推断"，
  这时**只写信息更全的那条，事实优先于推断**。例：本轮已经写了
  「长期|计划近期考教师资格证，目前正在准备」，就**不要再**补一条
  「长期|（推断）正在权衡是否报考教师资格证」—— 后者没有新增任何信息，
  纯属把同一条说了两遍，会白占长期记忆的字数上限。

【绝对不要提取】（这一节是硬约束，违反一次就污染档案一次）
· 寒暄闲聊（你好、谢谢、哈哈）和临时指令（"再短一点""换个说法"）
· 一次性的具体提问（"帮我查天气""这段代码哪错了"）
· 🔴 **一次性查询的结果本身** —— 这是最容易犯、也最没用的：
  查到的餐厅评分、天气、路线、车次、价格、股票、比分、网页摘要……
  这些是**当时那一下**的答案，跟"他这个人"没关系，明天就过期。
  ❌ 反例（真实踩过）：
     `长期|汕头大学附近餐厅评分：桑浦树屋 4.7分（人均20.00元）……`
     —— 他只是问了一次"附近有什么好吃的"，凭什么把菜单记一辈子？
  ✅ 该记的是**从这次提问能看出的偏好**：
     `长期|挑餐厅时在意评分`
  🔑 口诀：**记"他想干什么"，不记"查到了什么"。**
· 🔴 **本应用自己的操作说明 / 工具用法** —— 同样与他无关：
  ❌ `修改文件要用 edit_office 传文件名`、`文件存在「生成文库」里`、
     `地图缓存要去设置里清`……
  ✅ 但**他要求你遵守的规则**要记（那是对他的偏好）：`回复先给结论再给理由`
· 助手自己的客套话与过程叙述（"好的""明白""我来帮你看看"）。
  ⚠️ 但**对话里得出的、关于他的结论/约定/命名要记**（见上面那几类）——
  别因为"这是助手说的"就把它们一起丢掉。
· 客观知识（"什么是…""…的历史""…分几类"的答案）、知识库资料**本身**
· 【已知信息】里已经有的内容 —— **绝对不要重复记**

【记成长期还是短期】问自己一句话：换个话题还成立吗？
· 成立 → 长期（跨所有对话通用）
· 只在当前这件事里成立 → 短期（只服务这个对话）

【输出格式】每行一条，**只写有变化的内容**：
· 新信息 → `长期|具体事实` 或 `短期|具体事实`
· 已有信息要改动 → `更新|旧要点|新要点`
  ↳「旧要点」**必须照抄**【已知信息】里的原句（至少前 10 个字），照抄才能改到它
· 已有信息作废 → `删除|旧要点`（同样是照抄原句）

【目标与计划 · 特别重要】
· 用户说「想/打算/准备/计划/以后要…」→ 记成目标，写清 **做什么 + 大致时间 + 当前状态**：
  例 `长期|计划2027年6月考网络工程师，目前还没开始`
· 用户后来提到同一件事的进展 → **必须用 `更新|` 改写原来那条，绝对不要新增一条**：
  例 `更新|计划2027年6月考网络工程师|已报名，2027年3月开班，正在看教材`
· 只有改写，档案才不会自相矛盾 —— 「打算考」和「已经考完」并存会让模型答错话
· 用户没说不做了，就别因为"还没做"而删掉目标；那正是需要被记住的事

【一条要点怎么写：归纳，但数据一个都不能丢】
· **用自己的话归纳成"档案条目"**，不要照抄用户原句 ——
  照抄会把"嗯、就是说、那个、啦、吧"这些口语一起带进来，档案又长又难查。
· ⚠️ **归纳 ≠ 丢信息。** 下面这些必须**原样写进去**，一个都不能省：
  数字（分数、年龄、人数）、时间（年份、月份、期限）、金额与费用、
  比例与阈值、名称（学校/单位/证书/专业/城市/人名）、版本与技术栈，
  以及**他给出的理由 / 依据 / 顾虑**（"因为…""主要是…""怕…"）。
  宁可这条多几个字，也不要把数字和依据丢掉 —— 丢了这条记录就废了。
· 正反例（用户原话）：
  「我上次四级考了 424 分，就差 1 分过线，这次一定要把它过了」
  → 差：`用户四级成绩不理想`（424、1 分全丢了）
  → 差：`我上次四级考了 424 分，就差 1 分过线，这次一定要把它过了`（照抄原句）
  → 好：`四级曾考 424 分（差 1 分过线），决定重考`
  「我每天只能挤出 2 个小时复习，想在 3 个月内把 CET-4 考到 425 以上」
  → 好：`每天可投入 2 小时复习，目标 3 个月内 CET-4 达 425 分以上`
  「我打算考网络工程师，因为学校有个合作项目要求这个证，报名费 800 块」
  → 好：`计划考网络工程师（学校合作项目要求），报名费 800 元`
· 口语化的啰嗦话要**压成一句**，但压掉的是"说法"，不是"信息"：
  「嗯……就是说我其实挺想学一下那个数据分析的啦，感觉对我以后找工作应该会有帮助吧，
   但是又怕自己坚持不下来」
  → 好：`（推断）有意学数据分析，认为对求职有帮助，但担心坚持不下来`
   （"想学 / 为了求职 / 怕坚持不下来"三件事一件没少，只是把口语去掉了）

【怎么算值得记】问自己：这句话**三个月后**还用得上吗？
· 用得上 → 记；只是一次性问答、闲聊、临时要求 → 不记
· 一条尽量控制在 **60 字以内**，第三人称陈述句；**但数字与依据不受字数限制** ——
  宁可这条长一点，也要把数据写全
· 确实没有任何变化，就只输出两个字：无

【已知信息】（不要重复这些）
{long}

{short}

【本轮对话】
{convo}"""


# 提炼结果里的"动词"：模型除了新增，还可以改已有条目、删掉作废条目。
# 这三个词决定了**目标类信息能不能跟着进展走**（见 memory.py 里 update_long 的说明）。
_VERB_REVISE = ("更新", "修改", "修正", "改为", "调整", "推进")
_VERB_FORGET = ("删除", "作废", "取消", "遗忘", "移除", "无效")
# ⚠️ 这里用的是**精确匹配**。模型写「长期记忆|…」「档案|…」这种近义标签时，
# 原来会因为"不在名单里"被判成 kind=""，然后**一律落到短期记忆**去
# （_apply_extract 末尾：kind 不识别就走 merge_short）——
# 用户的表现就是"我明明让它记长期，怎么没进长期记忆"。所以把常见近义写法都收进来。
_KIND_LONG = ("长期", "长期记忆", "全局", "全局记忆", "档案", "用户档案")
_KIND_SHORT = ("短期", "短期记忆", "当前对话", "本对话", "本会话", "本次")


def _clean_extract_line(raw: str) -> str:
    line = raw.strip().lstrip("-*• ").strip("`").strip()
    while line[:1].isdigit():
        line = line[1:].lstrip("、.)． ").strip()
    if line.startswith("【") and "】" in line:
        inner = line.split("】", 1)
        if inner[1].strip():
            line = inner[1].strip()
    return line


def _split_extract_line(line: str) -> list:
    # 竖线优先（标准格式），其次容忍「长期：xxx」这种中文冒号写法
    for sep in ("|", "｜", "：", ":"):
        if sep in line:
            return [p.strip() for p in line.split(sep)]
    return [line]


def _forget_point(old: str, kind: str, session: str) -> bool:
    if kind in _KIND_LONG:
        return memory.drop_long(old)
    if kind in _KIND_SHORT:
        return memory.drop_short(session, old)
    return memory.drop_long(old) or memory.drop_short(session, old)


def _revise_point(old: str, new: str, kind: str, session: str) -> bool:
    new = (new or "").strip()
    if not new:
        return False
    if kind in _KIND_LONG:
        return memory.update_long(old, new) != "skipped"
    if kind in _KIND_SHORT:
        return memory.update_short(session, old, new) != "skipped"
    # 没写类别：先在长期里找、再在短期里找；两处都没有就按新信息追加（进短期，更安全）
    if memory.update_long(old, new, append_missing=False) == "replaced":
        return True
    if memory.update_short(session, old, new, append_missing=False) == "replaced":
        return True
    return memory.merge_short(session, new)


def _apply_extract(text: str, session: str, limit: int = 4) -> int:
    """把提炼结果按行并入记忆，返回真正生效的条数。

    支持三种指令（模型写法可能不规范，这里尽量宽容）：
      长期|要点 / 短期|要点      → 新增
      更新|旧要点|新要点         → 改写已有条目（**目标的进展就靠这个**）
      删除|旧要点                → 作废已有条目
    没写类别的新信息一律进**短期** —— 宁可少进一点长期记忆，
    也不要让乱七八糟的东西污染跨所有对话生效的全局档案。
    """
    if not text:
        return 0
    kept = 0
    for raw in text.splitlines():
        line = _clean_extract_line(raw)
        if not line or line.startswith("#"):
            continue
        if line.startswith("无") and len(line) <= 8:
            continue
        parts = _split_extract_line(line)
        verb = parts[0].strip().strip("【】[]（）() ")
        # ⚠️ 值里**不要**剥掉圆括号 —— 「（推断）用户…」这种标记是内容的一部分，
        # 剥了用户就看不出这条是推出来的（踩过：存进去变成「推断）用户…」）
        rest = [p.strip().strip("【】[]|｜ ") for p in parts[1:]]
        kind = ""
        if rest and (rest[0] in _KIND_LONG or rest[0] in _KIND_SHORT):
            kind = rest.pop(0)          # 容忍 "更新|长期|旧|新" 这种多写一层的写法
        if verb in _KIND_LONG or verb in _KIND_SHORT:
            verb, kind = "", verb       # "长期|要点"：第一段写的其实是类别
        try:
            if any(v in verb for v in _VERB_FORGET) and rest:
                if _forget_point(rest[0], kind, session):
                    kept += 1
            elif any(v in verb for v in _VERB_REVISE) and len(rest) >= 2:
                if _revise_point(rest[0], " ".join(rest[1:]), kind, session):
                    kept += 1
            else:
                val = (" ".join(rest) if rest else verb).strip().strip("，。；;、 ")
                if len(val) < 3:
                    continue
                val = val[:120]
                ok = memory.merge_long(val) if kind in _KIND_LONG else memory.merge_short(session, val)
                if ok:
                    kept += 1
        except Exception:
            continue
        if kept >= limit:
            break
    return kept


def _auto_extract_memory(session: str, turns: list, model: str, cfg: dict,
                         abort=None) -> bool:
    """把**最近几轮**对话里的要点提炼进记忆（后台线程，不阻塞回复）。

    返回是否**真的写进了记忆**（调用方据此决定要不要清空待处理队列）。

    长期记忆是"久远对话可被清理"的前提 —— 要点沉淀下来后，
    老的聊天记录才可以安全裁剪或清除。**所以释放归档前必须先跑一遍这个沉淀**，
    否则还没提炼的内容会跟着归档一起消失。

    为什么带"最近几轮"而不是只看最后一条消息：
    用户常常在闲聊中顺口讲出自己的经历（"我之前做过一个考勤系统…"），
    紧接着又聊别的。只看最后一条，这类信息必然漏掉。

    为什么要把【已知信息】喂给模型：不然它会把同一件事反复写成不同措辞，
    长期记忆里堆出一串"同义句"（实测见过同一个人被记了三遍），白白吃掉 15000 字上限。
    """
    if not turns:
        return False
    convo = "\n".join(
        "%s：%s" % ("用户" if m.get("role") == "user" else "助手",
                    str(m.get("content") or "")[:300])
        for m in turns if (m.get("content") or "").strip())
    if not convo:
        return False
    known_long = memory.get_long().strip() or "（暂无）"
    known_short = memory.get_short(session).strip() or "（暂无）"
    prompt = _EXTRACT_PROMPT.format(long=known_long[-2500:], short=known_short[-800:],
                                    convo=convo)
    text = _llm_extract(prompt, model, cfg, abort=abort)
    if not text:
        # 模型没吐出任何要点（多为思考吃光额度），或者被用户打断。
        # 前者原来什么都不说，表现就是"聊了半天，记忆库还是空的"，排查时毫无线索。
        if not (abort and abort()):
            logging.getLogger("uvicorn.error").warning(
                "记忆提炼返回空（session=%s, model=%s）", session, model)
        return False
    # ⚠️ 这里的上限原来写 4 —— 而 `_apply_extract` 里是 `if kept >= limit: break`：
    # 模型一次给出 6~8 条时，**超出的部分被静默丢掉**（不报错、界面上也看不出来）。
    # 实测：一段 6 条的对话，模型能正确给出「事迹」「称呼约定」「技术结论」共 3 条；
    # 但窗口是最近 8 条消息（约 4 轮），信息密集时给出 6~8 条很常见，
    # 于是每次多半只落进前半部分，用户就觉得"我明明说了，它没记住"。
    # 放宽到 8：留一个上限是为了防模型灌一堆噪声，但别卡在"刚好够一半"的位置。
    return _apply_extract(text, session, limit=8) > 0



# 允许本地界面跨域访问（浏览器 debug 时用）
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

# 前端静态目录（打包后为只读打包资源）
FRONTEND_DIR = config.res("frontend")


# ---------- 数据模型 ----------
class ToolConfirmRequest(BaseModel):
    id: str
    allow: bool = False
    # 勾了「本次任务不再询问」→ 本轮剩余的危险操作全部自动批准
    remember: bool = False


class ToolAnswerRequest(BaseModel):
    id: str
    answers: list = []


class CodeRunRequest(BaseModel):
    code: str


class ChatRequest(BaseModel):
    messages: list[dict]
    model: str | None = None
    images_b64: list[str] | None = None   # 附加到本轮 user 消息的图片
    # 用户在界面上**涂抹的选区蒙版**（白色=要改的地方）：给「精确微改」用。
    # 它**不发给对话模型**（纯黑白的图对模型没意义、还白占一千多 token），
    # 只放进工具上下文，由 edit_image 直接取用。
    mask_b64: list[str] | None = None
    docs: list[dict] | None = None        # 拖进来的文档：[{name, text}]
    stream: bool = True
    session_id: str | None = None         # 会话标识，用于历史会话透视归档
    studio: bool = False                  # 这轮是从「开发台」发起的（＝在写代码）


# ---------- 图片保存 ----------
class SaveImageRequest(BaseModel):
    b64: str | None = None                # 图片 base64 正文（无 data: 前缀）
    filename: str | None = None           # 期望文件名（自动去重）
    mime: str | None = None               # 如 image/png
    subdir: str | None = None             # 可选子目录，默认 saved_images


@app.post("/api/save_image")
def save_image(req: SaveImageRequest):
    """把图片（base64）保存到应用可写的本地目录，返回最终绝对路径。

    解决桌面端 pywebview 里 <a download + data URI> 无法保存的问题：
    图片由后端写盘，前端用返回路径提示用户，并可通过系统的
    os.startfile 打开对应文件夹。
    """
    import re, time, uuid
    if not req.b64:
        raise HTTPException(400, "缺少图片数据")
    try:
        raw = base64.b64decode(req.b64)
    except Exception as e:
        raise HTTPException(400, f"base64 解码失败: {e}")
    if not raw:
        raise HTTPException(400, "图片数据为空")

    subdir = req.subdir or "saved_images"
    root = config.data(subdir)
    os.makedirs(root, exist_ok=True)

    base_name = (req.filename or "image").strip() or "image"
    ext = (req.mime or "image/png").split("/")[-1].split(";")[0].lower()
    if ext not in ("png", "jpg", "jpeg", "webp"):
        ext = "png"
    if not base_name.lower().endswith(f".{ext}"):
        base_name += f".{ext}"

    safe = re.sub(r"[^\w.\-]", "_", base_name, flags=re.UNICODE) or "image.png"
    path = os.path.join(root, safe)
    stem, e = os.path.splitext(safe)
    i = 1
    while os.path.exists(path):
        path = os.path.join(root, f"{stem}_{i}{e}")
        i += 1
    with open(path, "wb") as f:
        f.write(raw)
    return {"ok": True, "path": path.replace("/", "\\"),
            "filename": os.path.basename(path)}


class OpenFolderRequest(BaseModel):
    path: str | None = None


def _is_container() -> bool:
    """是否运行在容器里（Docker 会创建 /.dockerenv）。"""
    if os.environ.get("MM_IN_CONTAINER"):
        return True
    return os.path.exists("/.dockerenv")


def _host_path(container_path: str) -> str:
    """把容器内路径映射回宿主机的实际路径。

    容器里没法打开宿主机的文件管理器，但可以把「宿主机上对应哪个目录」
    告诉用户（部署脚本会把该目录写进 MM_HOST_DATA_DIR）。
    """
    base = (os.environ.get("MM_HOST_DATA_DIR") or "").rstrip("/\\")
    if not base:
        return container_path
    root = config.data_root().rstrip("/\\")
    if not container_path.startswith(root):
        return container_path
    rel = container_path[len(root):].replace("/", os.sep).lstrip("/\\")
    return os.path.join(base, rel) if rel else base


# 宿主机上的「打开文件夹」小助手（open-folder-agent.ps1）会使用这两个文件：
#   .open_folder_agent    心跳，用于判断它是否在运行
#   .open_folder_request  请求，容器把路径写进去，由它真正弹出文件夹
_AGENT_BEAT = ".open_folder_agent"
_AGENT_REQ = ".open_folder_request"


def _host_agent_alive(max_age: float = 20.0) -> bool:
    """宿主机上的小助手是否在运行（靠心跳文件的更新时间判断）。"""
    try:
        beat = os.path.join(config.data_root(), _AGENT_BEAT)
        return (time.time() - os.path.getmtime(beat)) < max_age
    except Exception:
        return False


def _request_host_open(target: str) -> bool:
    """把「打开这个目录」的请求写进挂载目录，交给宿主机的小助手执行。"""
    try:
        req = os.path.join(config.data_root(), _AGENT_REQ)
        with open(req, "w", encoding="utf-8") as f:
            f.write(target)
        return True
    except Exception:
        return False


@app.post("/api/open_folder")
def open_folder(req: OpenFolderRequest = None):
    """在系统文件管理器中打开指定路径（默认打开 saved_images 目录）。"""
    return _open_folder_impl((req.path if req and req.path else None)
                             or config.data("saved_images"))


@app.post("/api/kb/open_folder")
def kb_open_folder():
    """打开「知识库」文件夹 —— 用户把 .txt/.md 丢进去就能被检索。"""
    kb.ensure_dir()
    return _open_folder_impl(kb.KB_DIR)


@app.post("/api/library/open_folder")
def library_open_folder():
    """打开「图片库」所在的文件夹。

    打开的目录就是图片实际保存的目录（image_library.DIR），
    保证「看到的」和「存进去的」是同一个地方。
    """
    return _open_folder_impl(image_library.DIR)


def _open_folder_impl(target: str) -> dict:
    """在系统文件管理器中打开一个目录（容器内交由宿主机小助手代劳）。

    - 源码/桌面方式运行：直接调用系统文件管理器
      （Windows explorer / macOS open / Linux xdg-open）
    - 容器方式运行：容器内调不起宿主机的文件管理器，改为把请求写进挂载的数据目录，
      由宿主机上的小助手代为弹出文件夹；若小助手没在运行，
      则退化成「把宿主机真实路径给用户复制」。
    """
    # 注意顺序：必须先判断是不是文件再 makedirs。
    # 若先对"文件路径"调 os.makedirs(exist_ok=True)，路径存在但不是目录时
    # 仍会抛 FileExistsError → 接口 500（前端表现为 r.json() 解析失败）。
    if os.path.isfile(target):
        target = os.path.dirname(target)
    os.makedirs(target, exist_ok=True)

    if _is_container():
        if _host_agent_alive() and _request_host_open(target):
            return {"ok": True, "opened": True, "path": target,
                    "host_path": _host_path(target), "via": "host-agent"}
        return {"ok": True, "opened": False, "path": target,
                "host_path": _host_path(target),
                "detail": "运行在容器里，无法直接打开宿主机的文件夹"}

    try:
        import subprocess
        if os.name == "nt":
            # ⚠️ 必须走 ShellExecute（os.startfile），不能用 subprocess.Popen(["explorer", ...])。
            # procwin.install_nowin_patch() 给所有 Popen 补了 STARTF_USESHOWWINDOW + SW_HIDE
            # （本意是治 ffmpeg/python 这类控制台程序弹黑窗），但 explorer.exe 是 GUI 程序：
            # 被 SW_HIDE 启动的结果是「窗口开出来了、但始终是隐藏的」——用户点「打开文件夹」
            # 完全没反应。2026-11-11 实测：残留 10 个 CabinetWClass 窗口，IsWindowVisible 全为 False。
            # os.startfile 走 ShellExecute，不经过 Popen，补丁管不到，实测窗口可见。
            os.startfile(os.path.normpath(target))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
        return {"ok": True, "opened": True, "path": target}
    except FileNotFoundError:
        return {"ok": True, "opened": False, "path": target,
                "detail": "当前系统没找到文件管理器，请手动打开上面的路径"}
    except Exception as e:
        raise HTTPException(500, f"打开文件夹失败: {e}")


class OpenPathRequest(BaseModel):
    path: str = ""


@app.post("/api/open_path")
def open_path(req: OpenPathRequest = None):
    """点一下就打开：本地路径 → 文件管理器；http(s) 网址 → 默认浏览器。

    用户 2026-11-10 提的：回答里写的本地路径、以及网址，都应该能直接点开，
    而不是让用户自己复制到资源管理器里找。
    """
    target = (req.path if req and req.path else "").strip().strip('"').strip("'")
    if not target:
        return {"ok": False, "detail": "没有给出路径"}
    low = target.lower()
    # ① 网址：只放行 http/https
    if low.startswith("http://") or low.startswith("https://"):
        try:
            import webbrowser
            webbrowser.open(target)
            return {"ok": True, "opened": True, "kind": "url", "path": target}
        except Exception as e:
            return {"ok": False, "detail": f"打不开这个网址：{e}"}
    if low.startswith("file://") or low.startswith("javascript:") or low.startswith("data:"):
        return {"ok": False, "detail": "这个协议不允许直接打开"}

    # ② 本地路径
    p = os.path.expanduser(target)
    if not os.path.isabs(p):
        for base in (os.getcwd(), config.data_root(),
                     os.path.dirname(config.data_root())):
            cand = os.path.join(base, p)
            if os.path.exists(cand):
                p = cand
                break
    p = os.path.abspath(p)

    if os.path.isdir(p):
        r = _open_folder_impl(p)
        r["kind"] = "dir"
        return r
    if os.path.isfile(p):
        if _is_container():
            return _open_folder_impl(os.path.dirname(p))
        try:
            import subprocess
            if os.name == "nt":
                # /select 会打开父文件夹并选中该文件，比只开目录更省事。
                # 同样不能用 subprocess.Popen —— procwin 的补丁会给它加 SW_HIDE，
                # explorer 窗口会「开出来但隐藏」，用户看着就是没反应。
                # 改走 ShellExecuteW：不经过 Popen，补丁管不到。
                import ctypes
                _rc = ctypes.windll.shell32.ShellExecuteW(
                    None, "open", "explorer.exe",
                    '/select,"%s"' % os.path.normpath(p), None, 1)
                if _rc <= 32:   # ShellExecuteW 返回值 ≤32 表示失败
                    os.startfile(os.path.dirname(os.path.normpath(p)))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", p])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(p)])
            return {"ok": True, "opened": True, "kind": "file", "path": p}
        except Exception as e:
            return {"ok": False, "detail": f"打不开这个文件：{e}"}
    return {"ok": False, "detail": f"路径不存在：{p}"}


# ---------- 健康 / 配置 ----------
@app.get("/api/selftest")
def api_selftest(run: int = 0, model: str = ""):
    """启动自检：所有能力的状态（含修复提示）。

    · `GET /api/selftest`                → 读缓存（启动时后台已跑过一次，毫秒级返回）
    · `GET /api/selftest?run=1`          → 现场重跑（约 5-10 秒，会真的探一次网络）
    · `GET /api/selftest?model=xxx`      → **按当前选用的模型**检测（缓存按模型分开存）

    `model` 传界面下拉里的值：本地模型查它是否已下载；`deepseek-*` 则跳过
    本地 Ollama/本地模型项、只查云端通道 —— 用户的原话：「选用什么模型就
    显示什么模型的状态，检验也是一样」。

    检测项：Ollama 服务 / 当前模型 / GPU 显存 / 文生图 / 图片微改 /
           Office 文档 / 文件读写 / 图像处理 / 联网 / DeepSeek API / 语音输入
    每项都带 `hint`：失败时直接告诉用户怎么修。`skip=True` 表示本项与当前
    选用的模型无关（⚪），不计入 ok/total。
    """
    mdl = (model or "").strip()
    if run:
        return {"ok": True, **selftest.run_all(mdl)}
    r = selftest.get(mdl)
    if not r.get("total"):
        return {"ok": True, **selftest.run_all(mdl)}
    return {"ok": True, **r}


@app.get("/api/health")
def health():
    d = client.health()
    # ★ 2026-11-12：额外报告「本进程有没有自己的窗口」。
    #   run.py 双击启动时靠这两个字段判断对面是不是一个无窗口后端：
    #   是 → 接管（杀掉它、自己建窗口），否则用户看到的就是「双击没反应」。
    #   背景见 backend/selfdev.py 的 _window_owner_alive()。
    if isinstance(d, dict):
        d = dict(d)
        d["pid"] = os.getpid()
        d["window"] = os.environ.get("MM_SELF_WINDOW") == "1"
    return d


@app.get("/api/diag/nowin")
def diag_nowin():
    """自检「黑窗防护」到底有没有生效。

    为什么需要这个接口：用户报过三次「黑窗反复闪」，每次都不是没改代码，
    而是"改了但没真生效"。补丁状态必须在**运行中的主进程**里看才准 ——
    在子进程里测是测不出来的（子进程根本不装这个补丁）。
    """
    try:
        from . import procwin
        return {"ok": True, "status": procwin.status(), "probe": procwin.probe()}
    except Exception as exc:                   # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


@app.get("/api/config")
def get_config():
    return {"config": config.load_config(), "defaults": config.DEFAULT_CONFIG}


@app.post("/api/config")
def update_config(body: dict):
    merged = config.load_config()
    for k, v in body.items():
        if k in config.DEFAULT_CONFIG:
            merged[k] = v
    config.save_config(merged)
    # ⚠️ 「联网」开关刚可能被改过 → 立刻丢掉地图模块里那个 2 秒的在线状态小缓存。
    #    不然用户点完开关、前端紧接着来问"现在联网吗"，还可能拿到改之前的答案，
    #    表现就是地图按钮 / 底图要愣一下才跟着变。
    try:
        from . import map_tools as _mt
        _mt.invalidate_online()
    except Exception:
        pass
    return {"ok": True, "config": merged}


# =====================================================================
#  技能（skill）：用户自定义的做法 / 规范 —— 增删改查 + 手动启用
#  ⚠️ 存在 data/skills.json（纯本地文件），不进知识库、不上传任何地方。
# =====================================================================
@app.get("/api/skills")
def skills_list():
    from . import skills as _sk
    return {"ok": True, "items": _sk.list_all(),
            "modes": [{"key": k, "label": _sk.MODE_LABEL[k]} for k in _sk.MODES]}


@app.post("/api/skills/create")
def skills_create(body: dict):
    from . import skills as _sk
    try:
        return {"ok": True, "item": _sk.create(body or {})}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/skills/update")
def skills_update(body: dict):
    from . import skills as _sk
    b = body or {}
    try:
        it = _sk.update(str(b.get("id") or ""), b)
        if not it:
            return {"ok": False, "error": "没找到这个技能（可能已被删除）"}
        return {"ok": True, "item": it}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/skills/delete")
def skills_delete(body: dict):
    from . import skills as _sk
    return {"ok": _sk.delete(str((body or {}).get("id") or ""))}


@app.post("/api/skills/toggle")
def skills_toggle(body: dict):
    from . import skills as _sk
    b = body or {}
    it = _sk.toggle(str(b.get("id") or ""), b.get("enabled"))
    if not it:
        return {"ok": False, "error": "没找到这个技能"}
    return {"ok": True, "item": it}


@app.post("/api/skills/use")
def skills_use(body: dict):
    """手动技能：点一下 → 注入到下一轮对话，只用一次。"""
    from . import skills as _sk
    b = body or {}
    sid = str(b.get("id") or "")
    if not _sk.set_manual(str(b.get("session") or ""), sid):
        return {"ok": False, "error": "这个技能不存在或已关闭"}
    return {"ok": True, "name": (_sk.get(sid) or {}).get("name", "")}


@app.post("/api/skills/unuse")
def skills_unuse(body: dict):
    from . import skills as _sk
    _sk.clear_manual(str((body or {}).get("session") or ""))
    return {"ok": True}


@app.get("/api/skills/pending")
def skills_pending(session: str = ""):
    """前端用：查这个会话有没有待用的手动技能（显示「已选：xx」）。"""
    from . import skills as _sk
    it = _sk.peek_manual(session)
    return {"ok": True, "item": ({"id": it["id"], "name": it["name"]} if it else None)}

# ---------------------------------------------------------------- 分享 / 导入
@app.post("/api/skills/share")
def skills_share(body: dict):
    """把一个技能导出成「可粘给任何 AI 的话 + 机器可读分享码」。"""
    from . import skills as _sk
    b = body or {}
    key = str(b.get("id") or b.get("name") or "").strip()
    sk = _sk.get(key) or _sk.get_by_name(key)
    if not sk:
        return {"ok": False, "error": "没找到这个技能"}
    return {"ok": True, "name": sk["name"], "text": _sk.share_text(sk),
            "code": _sk.share_code(sk),
            "json": _sk.share_payload(sk)}


@app.post("/api/skills/import")
def skills_import(body: dict):
    """把别人发来的分享内容建成本地技能（重名自动加后缀，不覆盖已有的）。"""
    from . import skills as _sk
    try:
        return {"ok": True, "item": _sk.import_skill(str((body or {}).get("text") or ""))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/skills/qr")
def skills_qr(body: dict):
    """把分享码渲染成二维码（PNG data URL），手机扫一下就能带走。"""
    import base64
    import io
    b = body or {}
    txt = str(b.get("text") or "").strip()
    key = str(b.get("id") or "").strip()
    if not txt and key:
        from . import skills as _sk
        sk = _sk.get(key) or _sk.get_by_name(key)
        if sk:
            txt = _sk.share_code(sk)
    if not txt:
        return {"ok": False, "error": "没有可编码的内容"}
    if len(txt) > 1500:
        return {"ok": False, "error": "分享码太长（%d 字符），二维码会密到扫不出来，"
                                      "请改用「复制」发文本" % len(txt)}
    try:
        import qrcode
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                           box_size=10, border=4)
        qr.add_data(txt)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return {"ok": True, "chars": len(txt),
                "data_url": "data:image/png;base64,"
                            + base64.b64encode(buf.getvalue()).decode("ascii")}
    except Exception as e:
        return {"ok": False, "error": "生成二维码失败：%s" % e}


@app.post("/api/skills/save_md")
def skills_save_md(body: dict):
    """把分享内容真正写成本机 .md 文件，并把绝对路径返回给前端。

    用户 2026-11-10 反馈：点「下载 .md」没任何反应，也不知道存到哪了。
    根因是前端用 <a download> 触发浏览器下载 —— 内嵌 WebView / 部分浏览器
    会直接拦掉，而且不报错，用户只能看到"按了没反应"。
    改成后端写盘：路径确定（data/skill_exports/）、能返回、前端直接显示在哪儿。
    """
    import re as _re
    b = body or {}
    txt = str(b.get("text") or "")
    if not txt.strip():
        return {"ok": False, "error": "没有内容可保存"}
    name = str(b.get("name") or "技能").strip() or "技能"
    name = _re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip(" .")[:60] or "技能"
    out_dir = config.data("data", "skill_exports")
    try:
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, "技能-%s.md" % name)
        i = 2
        while os.path.exists(path):
            path = os.path.join(out_dir, "技能-%s(%d).md" % (name, i))
            i += 1
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(txt)
    except Exception as e:
        return {"ok": False, "error": "写文件失败：%s" % e}
    return {"ok": True, "path": os.path.abspath(path),
            "dir": os.path.abspath(out_dir),
            "bytes": len(txt.encode("utf-8"))}


@app.get("/api/net/check")
def net_check():
    """检测本机是否已连接互联网。

    供前端「联网」开关做前置校验：未联网时不允许点亮开关。
    用 TCP 连通性判断（比 HTTP 更快、不受代理/重定向干扰），
    多目标**并行**探测，任意一个通即视为已联网——断网时也能快速返回。
    """
    import socket
    from concurrent.futures import ThreadPoolExecutor

    targets = [
        ("223.5.5.5", 53),        # 阿里公共 DNS
        ("www.bing.com", 443),
        ("www.baidu.com", 443),
    ]

    def _probe(host_port):
        host, port = host_port
        try:
            conn = socket.create_connection((host, port), timeout=2.0)
            conn.close()
            return host_port
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        for hit in pool.map(_probe, targets):
            if hit:
                return {"online": True, "via": f"{hit[0]}:{hit[1]}"}
    return {"online": False, "error": "无法连接互联网"}


# ---------- 模型管理 ----------
@app.get("/api/models")
def list_models():
    try:
        return {"ok": True, "models": client.list_models()}
    except ollama_client.OllamaError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/pull")
def pull_model(body: dict):
    name = body.get("model") or config.load_config()["default_model"]
    try:
        client.pull_model(name)
        return {"ok": True, "message": f"模型 {name} 下载完成"}
    except ollama_client.OllamaError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---------- 模型下载管理（后台线程 + 进度 + 取消 + 删除）----------
# 和上面的 /api/pull 区别：/api/pull 是**阻塞式**（下载完才返回，前端 60 秒就超时），
# 这里改成后台线程 + 轮询 /api/model/status：有进度、能取消、能删。
@app.post("/api/model/pull")
def model_pull(body: dict):
    """启动后台下载。立即返回，进度查 /api/model/status。"""
    name = (body or {}).get("model") or config.load_config().get("default_model") or ""
    if not name:
        return {"ok": False, "msg": "缺少 model 参数"}
    return model_manager.pull(name)


@app.get("/api/model/status")
def model_status():
    """当前下载进度（前端每 500ms 轮询）。"""
    return {"ok": True, **model_manager.get_state()}


@app.post("/api/model/cancel")
def model_cancel():
    """取消下载。已下载的 layer 会保留，下次 pull 断点续传。"""
    ok = model_manager.cancel()
    return {"ok": ok,
            "msg": "已请求取消，正在断开…" if ok else "当前没有进行中的下载"}


@app.post("/api/model/remove")
def model_remove(body: dict):
    """删除已下载的模型（释放磁盘）。"""
    name = (body or {}).get("model") or ""
    if not name:
        return {"ok": False, "msg": "缺少 model 参数"}
    return model_manager.remove(name)


@app.get("/api/model/installed")
def model_installed():
    """已安装模型清单（含体积）。前端据此判断「未下载 / 已安装」。"""
    return {"ok": True, "models": model_manager.installed()}


@app.post("/api/model/unload")
def model_unload():
    """把本地模型请出显存。切到云端模型时前端自动调用。

    为什么需要：`model_keep_alive` 配的是 4 小时，切到 DeepSeek 之后本地模型
    毫无用处却一直占着显存（8B 实测 9GB）。12GB 卡上这就是"显卡被锁死"，
    用户体感是别的程序卡、再切回本地还要等重载。
    """
    return model_manager.unload_all()


# ---------- 对话 ----------
# 单次对话内最多连续调用工具轮次（默认值；真正生效的取 config.max_tool_rounds）。
# ⚠️ 2026-11-08 用户反馈「8 次不够」：写代码/做项目这类活天生是
#   「一轮写一个文件 + 一轮跑一次」，10 轮根本不够用，任务做到一半就被掐断。
#   现在默认放到 60 轮，同时加一道**时间预算**（max_tool_seconds，默认 20 分钟）——
#   光靠轮次防死循环不可靠：一轮可能很快，也可能很慢。
MAX_TOOL_ROUNDS = 120

# Ollama 解析"模型写出来的工具调用"失败时，会把 Go 的 JSON 错误塞进流里。
# ⚠️ 用户看到的是一句英文，比如
#     invalid character '\'' looking for beginning of object key string
#   前端还会跟一句"（可能内存/模型未就绪，请查看状态）" —— 完全指错了方向。
# 2026-09-22 用户就是拿这句话来问"这是什么原因"的。
# 实测 Ollama 日志里的真身（`%LOCALAPPDATA%\Ollama\server.log`）：
#     level=WARN source=qwen3vl.go:90 msg="qwen tool call parsing failed"
#       error="invalid character '\'' looking for beginning of object key string"
#   ——是**模型自己**把参数写成了单引号（或写残了），Ollama 解析不出来。
# 这是采样噪声、不是环境问题，**重新发一次通常就好**，所以值得自动重试一次。
_TOOLPARSE_MARKS = (
    "looking for beginning of object key string",
    "looking for beginning of value",
    "unexpected end of JSON input",
    "tool call parsing failed",
    # ⚠️ 2026-09-25 补：Go 的 JSON 解析器报"某个字符串里有非法字符"是**同一族**。
    # 用户当天报的那次，Ollama 日志里的真身是：
    #   source=qwen3vl.go:90 msg="qwen tool call parsing failed"
    #     error="invalid character '\n' in string literal"
    # ——模型把工具调用参数里的换行写成了**裸换行**（没转义）。
    # 这一族的**前缀**都是 `invalid character`，后面跟具体字符和位置，形形色色：
    #     invalid character '\n' in string literal
    #     invalid character 'ä' after object key:value pair
    #     invalid character 'æ' looking for beginning of value
    # ⇒ 按前缀一次覆盖全。**别再按后缀枚举** —— 旧名单就漏了其中两个，
    #   用户拿到的是一句看不懂的英文（前端还会跟一句"可能内存不足"，指错方向）。
    "invalid character",
)


def _ask_mode_line(cfg: dict) -> str:
    """当前询问模式的说明（注入系统提示）。

    用户 2026-09-22 的要求：前端分「快速了解 / 深度询问」两个模式，
    **两种模式都不限制问题个数**，区别只在"问到什么程度就动手"。

    ⚠️⚠️ 2026-09-23 修（用户报「开启深度询问时模型没有多轮提问、单轮问题数也不够」）：
    原来只写「个数不限、可以分多轮」—— 这种**没有数字的许可**对模型几乎不起作用，
    它照样只问 2~3 条、问完就动手。改成给**具体数量**（一轮 5~7 个）和**具体轮次**
    （通常 2~3 轮），并列清每轮该覆盖哪些维度，模型才真的会问透。
    抽成独立函数是为了能单测（原来内联在 chat 接口里，测不到）。
    """
    if str((cfg or {}).get("ask_mode") or "quick").lower() == "deep":
        return ("**深度询问**（当前模式）：用户**主动打开了这个模式**，就是要你把信息问透再动手。\n"
                "    · ⚠️ **只要这一轮要产出东西**（文章/文档、PPT/表格、代码/程序、图片、"
                "方案…），**必须先调用 ask_user 问一轮**（5~7 个）——"
                "**哪怕你觉得已经比较清楚了**：深度模式存在的意义就是「别猜」。\n"
                "      （只有用户明说「直接做 / 别问 / 你看着办」时才跳过。）\n"
                "    · 一轮尽量覆盖：用途/场景、目标受众、范围与篇幅、风格或口径、"
                "硬性约束与雷区、交付形式（格式/尺寸/页数/语言）、有没有参考样例。\n"
                "    · 拿到答复后**先自查**：还有哪个会影响做法的点没问清？"
                "**有就再问一轮**（通常 2~3 轮），直到没有疑问为止。\n"
                "    · **每一轮都要带来新的关键信息**：绝不复问已经答过的；"
                "用户答了「随便/你定」的方向就不再追。\n"
                "    · 问够就动手 —— 不要为了显得认真而无限追问。")
    return ("**快速了解**（当前模式）：只问**最关键的 1~3 个**问题 —— "
            "不问就会做错方向的那几个；拿到基本信息就开工，剩下的按最合理的默认做，"
            "并在结果里说明你替他假设了什么。")


def _ask_rules(cfg: dict) -> str:
    """「信息不足就先问」这一整段规则（注入系统提示）。

    抽成独立函数是为了**能单测** —— 原来内联在 chat 接口里，改坏了没人发现
    （`_ask_mode_line` 当初也是为同样的理由抽出来的）。
    ⚠️ 2026-09-22 在这里加了「画图是个例外，宁问勿猜」：用户说
    「生成一张图片，关于狐狸」，模型自己判断"没给特定要求 → 给个通用的"就画了。
    """
    _ask_mode_txt = _ask_mode_line(cfg)
    return (
        "\n\n【信息不足就先问 —— 这是你的自主判断，不是硬性要求】\n"
        f"- 当前询问模式：{_ask_mode_txt}\n"
        "- 只要这一轮要**产出东西**（文章/报告、Word/PPT/Excel、代码或整个程序、"
        "图片/绘图、界面设计……），而用户**没说清关键前提** → "
        "**先调用 ask_user 问清楚**，拿到答复再动手。"
        "（要弹问答框，**不要在回答里用文字问**。）\n"
        "- ⚠️⚠️ **只要你打算问用户任何问题，就必须走 ask_user 工具**（会弹框）。"
        "正文里写「请问…」「你能告诉我…吗」这类问句**不算提问** —— "
        "用户看到的只是一个问句，没有弹框可以点，只会以为你卡住了，"
        "而且这一轮就这么结束了。如果你发现自己正在正文里写问号 → **停下，改成调用 ask_user**。\n"
        "- ⚠️⚠️ **提问不会结束这一轮对话**：用户填完，答复会作为「工具结果」发回给你，"
        "你**接着把活干完**（该写的写、该做的做），"
        "**不要停下来等用户再发一条消息**，也不要只回一句「好的，我这就开始」就收工。"
        "只有用户明确说「先别做 / 等等」时才停。\n"
        "- 各类任务该问什么：\n"
        "    · 文章/文档 → 用途、给谁看、篇幅、文体、要突出的重点\n"
        "    · 表格 → 给谁看、要哪些列、数据从哪来、统计口径\n"
        "    · PPT → 讲给谁、多少页、侧重点、风格\n"
        "    · 代码/程序 → 用什么语言、跑在什么环境、输入输出是什么、要不要界面\n"
        "    · 图片/绘图 → 风格、画面主体与细节、尺寸比例、用途\n"
        # ⚠️ 2026-09-22 实测：用户说「生成一张图片，关于狐狸」，模型**没问就直接画**了。
        #    查它的思考过程，它自己写的是「没有给出特定要求 → 提供通用但高质量的狐狸形象」——
        #    也就是被上面那条「能合理默认的别问」放过去了。可画图跟写文章不一样：
        #    风格选错**要重画**，一次几十秒，问一句远比返工便宜。
        #    所以这里单独把"模糊的画图需求"钉死成**必须先问**。
        "    ⚠️ **画图/生成图片是个例外，宁问勿猜**：用户只给了一个主体"
        "（「画一只狐狸」「来张风景图」）而没说**风格/用途/氛围/尺寸**时，"
        "**先 ask_user 问一轮**（1~3 个就够：写实还是插画？用在哪、要什么氛围？"
        "要不要高清竖版？），拿到答复**马上开画**。\n"
        "      · 理由：画一次要几十秒，风格选错就得重画 —— 问一句比返工便宜得多；\n"
        "      · 用户**已经**说了风格/用途（「画一只写实狐狸，做儿童绘本封面，竖版」）"
        "→ 一个字都别问，直接画；\n"
        "      · 问到/说到画幅就**传给 generate_image 的 aspect**"
        "（「竖版/海报/壁纸」→ 9:16 或 2:3，「横版/宽屏/Banner/PPT 配图」→ 16:9 或 3:2，"
        "方形 → 1:1）；要写实、要细节、要印出来就把 size 提到 768，必要时 hd=true。\n"
        "      · **微改已有图片**（「把背景改成夜晚」「给她戴上眼镜」「把刚才那张换个颜色」）"
        "→ 用 **edit_image**，不要重画；用户本轮拖进来的图或刚生成/刚改过的那张"
        "**不用填 source**（系统自动拿最近那张）。改动越大 strength 越高，"
        "换背景/换日夜必须 0.8~0.9，只改颜色/加小物件用 0.35~0.45。\n"
        "      · **多张图要合到一起**（「把这几张拼起来 / 拼成一张 / 做个对比图 / 拼个长图」）"
        "→ 用 **compose_images**：它做**像素级拼接**（横排/竖排/网格），接缝无缝、内容不走样；"
        "用户还要求「看起来像一张图 / 色调统一」时再给 harmonize 0.25~0.4（会轻微重绘）。\n"
        "      · **要「只改某处、别的一律不动」**（「**把她的衣服换成深蓝**」「给她**戴上眼镜**」"
        "「只把背景换掉、人别动」）→ 这是**精确微改**，**必须给区域**：\n"
        "        ① 用户在界面上**涂抹过选区**时，系统会自动把蒙版传给 edit_image，"
        "你只管写 prompt（**不用**自己填 mask）；\n"
        "        ② 没有涂抹时，**自己给 `regions`**（矩形 [x0,y0,x1,y1]，0~1 归一化）"
        "或 `area`（face 脸 / torso 上半身衣服 / upper-third 上三分之一 / center 等）；\n"
        "        ③ 走这条路时 strength 要 **0.75~1.0**（inpainting 的档跟整图重画不同）。\n"
        "        ⚠️ 实测：**不给区域、只写 prompt 是改不出来的** ——「换衣服颜色」「加眼镜」"
        "在整图 img2img 的 0.45/0.60/0.85 三档全都没生效，而 0.85 换背景还会把脸重画。"
        "所以别空着手让用户等，先圈区域再改。\n"
        "      · **抠图 / 去背景**（「把背景去掉」「只留人」「抠出这个产品」「换个背景」）"
        "→ 用 **cutout_image**（BiRefNet，跑在 CPU 上、不占显存）；"
        "人像/头发多用 model=birefnet-portrait，极限质量用 birefnet-massive。\n"
        "      · **把 A 图里的人/物放进 B 图**（「把这个人放到那个场景里」"
        "「把产品放进这张背景」「帮我把这两张合成一张」）→ 用 **composite_image**："
        "它做的是**像素级真实合成**，主体**一个像素都不重画**"
        "（长相/衣服/细节 100% 保留），主体没抠过会自动先抠；"
        "位置用 position（center/top-left/top-right…），大小用 scale，"
        "贴人物或产品时开 shadow 会更「坐得住」。\n"
        "        ⚠️ 别用高 strength 的 edit_image 去硬糊这类需求 —— 那会把人物重画变形。\n"
        "      · **只问一轮**：答复到手就必须动手画，不许问完又停在原地等用户再说话。\n"
        "- ⚠️ **问题个数不限，也可以分多轮问**（用户明确要求）——守住这三条：\n"
        "    · **绝不重复**：已经问过、用户已经答过的，一个字都别再问；\n"
        "    · **每个都要关键**：问了能改变你怎么做的才值得问。凑数的、无关紧要的、"
        "你自己查得到（search_knowledge / web_search）或能合理默认的，统统别问；\n"
        "    · **别没完没了**：该知道的都知道了就动手做。分多轮是允许的，"
        "但每一轮都必须带来**新的**关键信息；如果拿到答复后发现还有影响做法的疑问，"
        "可以再问一轮 —— **别硬猜着做**。\n"
        "- ⚠️ **不要为了「少打扰」就跳过关键问题** —— 方向问错了，比多问一句代价大得多"
        "（这是用户明确指出的：不要因为想减少询问次数而漏掉关键信息、理解错意图）。\n"
        "- **什么时候不用问，直接做**：用户已经把要求说清楚了；只是问个问题、查资料、"
        "解释概念、算个数；打招呼、闲聊、表达情绪；"
        "或者关键信息你自己查得到。**别为了显得周到而问，也别什么任务都问。**\n"
        "- 用户已经回答过、或说过「你看着办 / 直接做」→ 不要再问同一个方向，直接做完。\n"    )


def _is_context_err(msg) -> bool:
    """是不是"提示词超出模型窗口"这一类报错。

    Ollama 原文长这样（2026-09-22 用户截图里那条）：
      `{"error":{"code":400,"message":"request (26352 tokens) exceeds the available
        context size (24576 tokens), try increasing it",
        "type":"exceeded_context_size_error","param":26352,"n_ctx":24576}}`

    ⚠️ 这不是"环境坏了"，是**这一轮带的东西太多**（常见元凶：附件图片 + 长历史）。
    处置见 gen() 里的兜底：砍到最近两条 + 丢掉检索材料再试一次，别把英文丢给用户。
    """
    m = str(msg or "").lower()
    return ("exceeded_context_size" in m
            or "exceeds the available context size" in m
            or ("n_ctx" in m and "exceed" in m))


def _shrink_for_model(images: list, max_side: int = 1024) -> list:
    """把过大的附件图缩到长边 ≤ max_side，**只影响发给模型的那份**。

    ⚠️ 图片是按像素折算成 token 的（Qwen3-VL：28×28 patch、2×2 合并），
    一张 2048×2048 要 1300+ token，四张就是半个对话框 —— 用户拖几张图进来
    就可能把窗口撑爆。缩到 1024 长边能省掉约 3/4。
    ⚠️ 原件**不动**：落盘、后续「把这张图改进 PPT」都要用清晰的原始字节。
    取不到尺寸 / 不是图片 → 原样返回那一项（宁可多占点，也别把图弄丢）。
    """
    out = []
    for b64 in (images or []):
        s = str(b64)
        try:
            from PIL import Image as _PILImage
            raw = base64.b64decode(s.split(",")[-1], validate=False)
            with _PILImage.open(io.BytesIO(raw)) as im:
                if max(im.size) <= max_side:
                    out.append(s)
                    continue
                im2 = im.convert("RGB")
                ratio = max_side / float(max(im2.size))
                im2 = im2.resize((max(1, int(im2.width * ratio)),
                                  max(1, int(im2.height * ratio))))
                buf = io.BytesIO()
                im2.save(buf, format="JPEG", quality=88)
            out.append(base64.b64encode(buf.getvalue()).decode("ascii"))
        except Exception:
            out.append(s)
    return out


def _is_toolparse_err(msg) -> bool:
    """判断这是不是"Ollama 解析模型输出的工具调用失败"。"""
    m = str(msg or "")
    return any(k in m for k in _TOOLPARSE_MARKS)


def _is_alloc_err(msg) -> bool:
    """Ollama 因为**内存/显存申请不下来**而启动失败的标志。

    实测原文（2026-10-16）：
      `llama-server startup failed after projector CPU offload retry: ...
       ggml_backend_cpu_buffer_type_alloc_buffer: failed to allocate buffer of size 17742723072
       ... failed to allocate CUDA_Host buffer of size ...`
    """
    m = str(msg or "").lower()
    # ⚠️ 别只匹配 `failed to allocate buffer` —— 原文还有
    #    `failed to allocate CUDA_Host buffer of size …` 这种中间插了词的说法，
    #    漏了它就等于这个功能不存在（实测一开始就漏了）。
    return (("failed to allocate" in m and "buffer" in m)
            or ("alloc_buffer" in m and "failed" in m)
            or ("startup failed after projector" in m))


def _list_llama_servers() -> list:
    """列出 llama-server 进程的 (pid, ppid, exe路径)。

    用 Win32 的 Toolhelp 快照 + QueryFullProcessImageName，
    **不引入 psutil**（这个项目的依赖表里没有它）。
    ⚠️ 一定要拿到 exe 全路径：机器上可能装着**另一个** Ollama（比如系统托盘版），
    不能把别人的 runner 一起杀了。
    """
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    k = ctypes.windll.kernel32
    out = []
    snap = k.CreateToolhelp32Snapshot(0x2, 0)          # TH32CS_SNAPPROCESS
    if not snap or snap == -1:
        return out
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = k.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if str(e.szExeFile).lower().startswith("llama-server"):
                path = ""
                h = k.OpenProcess(0x1000, False, e.th32ProcessID)   # QUERY_LIMITED_INFORMATION
                if h:
                    buf = ctypes.create_unicode_buffer(1024)
                    size = wintypes.DWORD(1024)
                    if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                        path = buf.value
                    k.CloseHandle(h)
                out.append((int(e.th32ProcessID),
                            int(e.th32ParentProcessID), path))
            ok = k.Process32NextW(snap, ctypes.byref(e))
    finally:
        k.CloseHandle(snap)
    return out


def _reap_orphan_llama_servers(reason: str = "") -> int:
    """清掉**我们自己那份 Ollama** 留下的 llama-server 孤儿进程。

    ⚠️⚠️ 这是 2026-10-16 实测出来的**粘性故障**（用户报"内存/模型未就绪"的真正原因）：
    模型加载失败（内存/显存不够）时，Ollama 会留下 `llama-server.exe` 进
    程**没有回收** —— 每个占着约 **9GB 显存** + 约 **25GB 提交内存**，
    而 `/api/ps` 里**看不到它**（显示 0 个模型）。
    后果：用户"关掉别的程序再重试"依然失败，看起来像"这台机器内存永远不够"，
    其实是孤儿把提交内存顶满了。实测：两个孤儿让提交内存到 **87.8GB / 上限 96.7GB**，
    清掉后立刻降到 **37GB**、显存从 10.3GB 降到 1.2GB，对话马上恢复。

    只杀**路径在我们自己 Ollama 目录下**的 llama-server：
    - 优先按 `MM_BUNDLE_ROOT/ollama` 判断（便携包布局）；
    - 回退到"进程 exe 路径里带 `ollama`"。
    拿不到路径的一律**不杀**（宁可留着，也不要误杀系统里另一个 Ollama 的 runner）。

    ⚠️ 判"孤儿"不能看父进程：llama-server 的父进程是 `ollama.exe`（不是另一个
    llama-server），父进程一直活着。**可靠的判据是 `/api/ps`**：
    Ollama 正常服务时它一定列得出模型；列不出（0 个）却还挂着 llama-server，
    那这些 runner 就是加载失败后留下的僵尸。
    """
    import os as _os
    servers = _list_llama_servers()
    if not servers:
        return 0
    # 先问 Ollama：现在有没有在正常服务的模型？有 ⇒ 一个都不动。
    try:
        from . import config as _cfg
        base = str((_cfg.load_config() or {}).get("ollama_url")
                   or "http://127.0.0.1:11434").rstrip("/")
        with urllib.request.urlopen(base + "/api/ps", timeout=5) as r:
            if (json.loads(r.read().decode("utf-8")).get("models") or []):
                return 0
    except Exception:
        return 0          # 问不到就先不动手（宁可漏清，也不要误杀）
    roots = []
    bundle = _os.environ.get("MM_BUNDLE_ROOT")
    if bundle:
        roots.append(_os.path.join(bundle, "ollama").lower())
    killed = 0
    import ctypes
    k = ctypes.windll.kernel32
    for pid, _ppid, path in servers:
        low = (path or "").lower()
        mine = bool(roots) and any(low.startswith(r) for r in roots)
        if not mine and path:
            mine = "\\ollama\\" in low or "/ollama/" in low
        if not mine:
            continue
        try:
            h = k.OpenProcess(0x0001, False, pid)        # PROCESS_TERMINATE
            if h:
                k.TerminateProcess(h, 1)
                k.CloseHandle(h)
                killed += 1
        except Exception:
            continue
    if killed:
        logger.warning("[ollama] 已清理 %d 个残留的 llama-server 进程（%s）",
                       killed, reason or "启动自检")
    return killed


def _friendly_ollama_error(msg) -> str:
    """把 Ollama 的内部英文报错翻成用户能懂、也知道下一步怎么办的话。

    ⚠️ 原样转发等于没说 —— 用户会来问"这是什么原因"，而且前端那句
    "可能内存/模型未就绪"会把人带去查硬件（查了也没用）。
    """
    m = str(msg or "").strip()
    if _is_toolparse_err(m):
        # ⚠️ 措辞里**必须保留"工具调用格式不对"这几个字** ——
        #    test_ollama_error.py 正是断言这一句（它守的是"不许把英文原文丢给用户"）。
        #    2026-09-25 我为了说得更准把它改成"写坏了"，当场把这个测试弄挂了。
        #    教训：改**用户可见文案**之前先 grep 一遍 test_*.py —— 文案也是有契约的。
        return ("模型这一次生成的**工具调用格式不对**（JSON 里的引号或换行没写对），"
                "所以这一轮没执行成功。\n"
                "**这不是你的操作问题，也不是内存/显存不够** —— "
                "直接**把刚才那句话再发一次**通常就好了。\n"
                "如果反复出现，把下面这行发给开发者：\n`%s`" % m)
    if "not found" in m.lower() and "model" in m.lower():
        return ("找不到这个模型（可能还没下载，或名字写错了）。"
                "可以在设置里换一个已下载的模型。\n`%s`" % m)
    if "connection" in m.lower() or "refused" in m.lower():
        return ("连不上本机的 Ollama 服务（它可能没在运行）。"
                "启动 Ollama 之后直接再发一次即可。\n`%s`" % m)
    # ⚠️ 「超出上下文窗口」：自动精简重试过了还失败，才走到这里。
    #    必须说清**怎么办** —— 那条英文原文（26352 > 24576）用户看了只会一头雾水，
    #    而前端默认的"可能内存/模型未就绪"会把人带去查硬件（查了也没用）。2026-09-22。
    if _is_context_err(m):
        return ("这一轮的内容**超出了模型能装下的长度**（`num_ctx` 不够用），"
                "自动精简后仍然装不下。\n"
                "**下一步（任选其一即可）**：\n"
                "· 少附几张图 / 少拖几个文件再问一次；\n"
                "· 或者**新开一个对话**（旧对话的历史太长）；\n"
                "· 长文建议**存成文件**再拖进来让我读（这样不占对话窗口）；\n"
                "· 或者到设置里把「上下文长度 num_ctx」调大 —— "
                "⚠️ **前提是显存装得下**：超了会有一部分算到 CPU 上，明显变慢\n"
                "  （实测 12GB 卡：24576 是 100% 在显卡上的上限；调到 32768 会有约 1GB 落到内存）。\n"
                "**这不是内存或显存坏了**，纯粹是这一轮带的内容太长。\n`%s`" % m)
    # ⚠️ 「申请不到内存」：2026-10-16 用户报的就是这个（原话附了整段英文原文）。
    #    真凶往往是**上次加载失败留下的 llama-server 孤儿**把提交内存顶满了，
    #    所以这里顺手清一次，并明确告诉用户"直接再发一次"。
    if _is_alloc_err(m):
        n = _reap_orphan_llama_servers("报错后自动清理")
        extra = ("\n✅ **已自动清理 %d 个上次失败留下的残留进程**，"
                 "**直接再发一次通常就好了**。" % n) if n else ""
        return ("模型这次**没能装进内存**（显存 + 内存不够），所以没启动起来。\n"
                "**不是模型坏了，也不是没下载**。\n"
                "**怎么办**（任选）：\n"
                "· 关掉占内存的程序（浏览器 / 游戏 / 微信…）再发一次；\n"
                "· 把设置里的「上下文长度 num_ctx」调小一点"
                "（40k 的 KV 缓存本身就要好几个 GB 内存）；\n"
                "· 机器上如果还装着**另一个 Ollama**（系统托盘版），建议退出它。"
                + extra + "\n`%s`" % m)
    return m



# =====================================================================
#  代码能力：专用代码模型的路由
# =====================================================================
# 为什么要换模型（实测数据）：默认的 qwen3-vl:8b 是**视觉**模型，
# 写代码时"思考"会失控 —— 同一道 LRU 缓存的题，有时 9 秒正常出代码，
# 有时陷入原地重复的思考死循环（"但是，题目没有说明，所以我们可以不处理"反复刷屏），
# 121 秒后 done_reason=length、正文一个字都没有；3 道题只过 1 道。
# qwen2.5-coder 是**非思考型**的代码专用模型，不存在这个问题。
#
# 路由是**按轮**的：这一轮要写代码或写长文就用专用模型，下一轮闲聊自动回到视觉模型。
# （2026-09-15 扩展：原来只管代码。实测「写一篇小作文」这类长文生成会让默认模型
#   思考吃光额度、正文为空、请求挂 10 分钟以上，所以长文创作也走这里。）
# 代价是切换模型要重新加载（12GB 显存放不下两个模型），所以只在真需要时才切。
# ⚠️ 分「强信号 / 弱信号」两档（2026-09-15 修）。
# 原来把语言名和"代码/脚本/函数"混在一张表里，只要句子里出现 "python" 就切模型 ——
# 实测「Python 的列表和元组有什么区别？」这种**纯概念题**被切到代码模型，
# 而 qwen2.5-coder 不会走原生工具通道，直接把工具调用当 JSON 文本吐出来，
# 用户看到的是满屏 `{"name": "search_memory", "arguments": …}`，等于答非所问。
# 规则：**强信号**出现即切；**弱信号**（只是提到了语言/格式名）必须
# 同时出现动作词才算写代码任务。
_CODE_STRONG = (
    "写代码", "调试", "重构", "单元测试", "爬虫",
    "帮我实现", "实现一个", "实现个",
    "帮我改这段", "改这段", "注释一下",
)
# ⚠️ 下面这一档是"**既是术语/现象、也能是日常问法**"的词 —— 必须配上动作词才算要写代码。
#
# 2026-09-16 从 _CODE_STRONG **降级**下来的一批。用户反馈："明明只是日常对话，
# 却给我显示模型不思考（切到了代码模型）" —— 元凶就是它们单独命中就切：
#     「写个总结」        ← 命中 "写个"
#     「这段代码是什么意思」← 命中 "代码" / "这段代码"
#     「为什么会报错」     ← 命中 "报错"
#     「跑一下看看」       ← 命中 "跑一下"
# 现在它们必须配上"写/改/实现/帮我"这类动作词才算。
# 另外「什么是递归？」是知识问答，不能因此切到代码模型；
# 「用递归实现斐波那契」才真的在要代码。
_CODE_WEAK = (
    "python", "javascript", "typescript", "java", "c++", "c#", "golang",
    "rust", "html", "css", "shell", "bash", "bat", "powershell", "json",
    "api", "接口", "函数", "排序", "数据库", "递归", "算法", "数据结构",
    # —— 从 strong 降级下来的（单独出现多为问答）——
    "代码", "脚本", "报错", "错误", "bug", "正则", "sql",
    "这段代码",
)
# ⚠️ 「跑一下 / 运行一下」**不放 weak、也不放 strong** ——
# 它们身上根本没有技术特征，纯是动作：「跑一下看看什么情况」不该切代码模型。
# 真要跑代码时旁边一定有别的信号（「帮我跑一下这个脚本」→ weak 命中"脚本"）。
# 同理「报错 / 错误」不能放动作词（见下），否则「为什么会报错」会被判成代码任务。
# ⚠️ "报错 / 错误" **不能放在动作词里** —— 它们是"现象描述"不是"动作"，
# 否则「为什么会报错」会同时命中 weak 与 actions，又被判成代码任务。
# 「帮我修一下这个报错」里有 "帮我" 兜着，不会漏。
# ⚠️ 「帮我 / 给我 / 生成 / 完成」**已经从动作词里拿掉**（2026-09-22 实测）。
#
# 它们不是"代码动作"，而是**通用请求标记** —— 任何一个客气的请求都带一个，
# 配上 `_CODE_WEAK` 里的常用术语就必然误判。实测用户原话：
#   · 「帮我用一句话**解释**什么是向量**数据库**」→ "帮我"+"数据库" → 判成写代码 ❌
#   · 「再帮我说说它和普通**数据库**的区别」    → 同上 ❌
# 后果不只是"模型选错"，而是**换模型要卸载/重载数 GB 的模型**：
# 实测这几轮的 Ollama 侧耗时 15~17 秒，而同一模型连着的下一轮只要 0.7 秒。
_CODE_ACTIONS = (
    "写", "改", "实现", "调试", "运行", "跑", "修复", "优化", "补全",
)

# "接着上一轮继续改"的意图词 —— 用来判断"上一轮写过代码，这轮还在改它"。
# 只放明确的**接续/修改**词：日常寒暄、问概念都不会命中，也就不会被误切到代码模型。
_CONTINUE_HINTS = (
    "再", "继续", "接着", "然后", "还有", "另外",
    "改", "加", "换成", "替换", "调整", "优化",
    "去掉", "删", "补", "新增",
)

# ⚠️ **大白话的"造东西"需求**（2026-09-16 补，vibecoding 场景）
# 普通人描述需求不会说"写代码/脚本/python"，只会说「帮我做个小网页」
# 「我想要个记账的小程序」。光靠上面那两张技术词表，这类请求**一个都匹配不上**，
# 会被送到**思考型默认模型**上 —— 实测「帮我做一个小网页，番茄钟」跑了 7 分钟
# 还没出结果（GPU 一直 97% 在"思考"），因为思考型模型既慢又不擅长写代码。
# ⚠️ 规则：**"求你做"的措辞（或明确的"我想要"） + 软件类产物名词** → 当成代码任务。
# 只留明确的软件产物，别放「清单/表格/方案」这类也能是文档的词（会误切）。
#
# ⚠️⚠️ 这里原来是一串**光杆动词**（"做/写/搞/弄/生成…"），2026-09-22 发现它会误判：
#   用户原话「你好，我叫陈工，**在做**本地 AI **应用**」
#   —— 只是在**介绍自己的背景**，却因为句子里有"做"和"应用"被判成写代码，
#      切到 14B 代码模型 → 卸载/重载 15 秒。
#   所以改成**只认"请求式"搭配**：光杆动词不算，要么有"帮我/给我/我想…"这类求助口气，
#   要么是"做个 / 写个 / 搞个 / 搭个"这种**动词紧跟量词**的造物口吻。
#   「在做本地 AI 应用」里没有"做个"，也不带求助口气 → 不再命中。
_BUILD_REQ = (
    # 求助 / 意愿口气
    "帮我", "给我", "帮忙", "麻烦", "能不能", "可不可以", "我要", "我想要", "想要",
    # 动词 + 量词（造物口吻）。⚠️ 别写成光杆动词，那样"在做 xx 应用"也会中
    "做个", "做一个", "做个一", "写个", "写一个", "搞个", "搞一个", "弄个", "弄一个",
    "搭个", "搭一个", "建个", "建一个", "编个", "编一个", "生成个", "生成一个",
    "开发个", "开发一个", "实现个", "实现一个", "来个", "来一个", "要个", "要一个",
)
_PRODUCT_NOUNS = (
    "网页", "页面", "网站", "小程序", "应用", "软件", "工具", "脚本",
    "程序", "插件", "组件", "界面", "面板", "游戏", "app", "exe",
)


def _is_code_task(text: str) -> bool:
    t = (text or "").lower()
    if not t:
        return False
    if "```" in t:
        return True
    if any(k in t for k in _CODE_STRONG):
        return True
    # 只是"提到了"Python/JSON 这类词 —— 还要有动作词才算真要写代码
    if any(k in t for k in _CODE_WEAK):
        return any(a in t for a in _CODE_ACTIONS)
    # 大白话的造物需求：「帮我做个小网页」「我想要个记账的小程序」
    # （没有技术词，但要有**求助口气或"做个/写个"这类造物口吻** + 软件类产物）
    # ⚠️ 判据见 _BUILD_REQ 的说明：光杆动词会误伤"我在做 xx 应用"这种自我介绍。
    if any(k in t for k in _PRODUCT_NOUNS) and any(k in t for k in _BUILD_REQ):
        return True
    return False

# 长文创作类请求：这些任务**必须一次写出几百上千字**，
# 默认的思考型模型会把输出额度全烧在思考上（实测两次重试都是空正文、请求挂 10 分钟以上），
# 交给非思考型模型才稳。短问答不要走这里（没必要，还慢）。
_WRITING_HINTS = (
    "写一篇", "写篇", "写一段", "写个作文", "作文", "文章", "短文", "长文",
    "演讲稿", "发言稿", "致辞", "倡议书", "读后感", "观后感", "心得体会",
    "工作总结", "实践报告", "调研报告", "开题报告", "毕业论文", "论文",
    "策划书", "方案", "企划", "文案", "宣传稿", "新闻稿", "通讯稿",
    "简历", "自荐信", "求职信", "自我介绍稿", "检讨书", "申请书",
    "帮我写", "替我写", "拟一篇", "拟一份", "起草",
)

# 「要能打开的办公产物」线索：PPT / Word / Excel。
# ⚠️⚠️ 2026-09-25 加（用户报"复杂内容的 PPT 做不出来、做工也不对"）：
#   用户的请求经常**同时**命中"写作"和"办公产物"，例如
#     「帮我做一份建设**方案**PPT」—— "方案" 在 _WRITING_HINTS 里，
#   于是被判成"纯写作"→ 工具集被砍到只剩 3 个 → **make_pptx 直接没了** →
#   模型只能把内容写成一大段文字，用户拿不到 PPT。
#   所以命中办公产物时走 **office 模式**：保住生成工具 + 给足输出额度。
_OFFICE_HINTS = (
    "ppt", "PPT", "幻灯片", "演示文稿", "演示稿", "演示", "课件",
    "word", "Word", "word文档", "文档", "文稿",
    "excel", "Excel", "表格", "台账", "报表", "统计表", "数据表", "汇总表",
    "xlsx", "docx", "pptx",
)


def _is_office_task(text: str) -> bool:
    """用户要的是"能点开的办公文件"（PPT / Word / Excel）吗。

    这类请求必须**保住生成工具**，见 _OFFICE_HINTS 上面的说明。
    """
    t = (text or "").strip()
    if not t:
        return False
    return any(k in t for k in _OFFICE_HINTS)


def _is_doc_task(text: str) -> bool:
    """这句话要的是**文档类产物**（写文章 / 做 PPT / 做表格）而不是写代码吗。

    ⚠️⚠️ 2026-09-27 加。用户报："有时候让它写作文、做 PPT 或 Excel，
    **结果给我用了代码模型**。" 根因就在 `prev_code` 那条兜底路上：

      判"这轮是不是还在改代码"时，原来只看**上一轮回答里有没有 ```**。
      而上一轮做 Excel / PPT 时，助手的回答里**经常贴一段 python 代码**
      （实测："已生成，代码如下：" + ```python from openpyxl import Workbook …```）：
        上一轮「帮我做一个 Excel 表格」→ 回答里带代码块
        这一轮「再帮我加一列合计」    → 只有接续词"再 / 加"，没有任何写作或办公关键词
        ⇒ prev_code = True ⇒ **切到 qwen3-coder** ❌

    伤害不只是"模型换错"：**代码模型那一轮的工具被整轮清空**
    （见 `code_model_on`，只留文本协议工具），make_xlsx / make_pptx /
    make_docx / edit_office **全没了** —— 用户不是"感觉不对"，是**根本改不动那份文件**。

    离线判定实测（`test_code_routing.py` ⑤）：12 条文档迭代句
    （Excel 5 条 / PPT 4 条 / 作文 3 条）**12 条全被误切**。

    ⚠️ 判据顺序要紧：**真的是代码任务就不算文档任务** ——
    上一轮"帮我写个程序把数据导出成 xlsx"虽含 Excel 字样，但那是在写代码，
    后面几轮的"再加一列"就该继续按代码迭代走。
    """
    if _is_code_task(text):
        return False
    return _is_writing_task(text) or _is_office_task(text)


def _prev_code_flag(messages: list, last_user: str) -> bool:
    """上一轮刚写过代码、而这轮**还在改它**吗（决定要不要继续用代码模型）。

    这正是用户报的"写作文 / 做 PPT / 做 Excel 却被用了代码模型"的现场 ——
    抽成函数只为一件事：**能被测试直接覆盖**（见 `test_code_routing.py` ⑤），
    以前它内联在 `chat()` 里，测试只能靠复刻，改坏了也发现不了。

    ⚠️ 判据只能看"上一轮回答里有没有 ```"是**不够**的，两个坑：
      ① 太宽：助手解释一下、贴个示例都会带代码块，用户接着聊别的也会被切；
      ② **会自锁**：切到代码模型后它的回答**必然**带代码块 →
         下一轮 prev_code 又是真 → **永远切不回来**。
         （用户实测反馈："明明只是日常对话，却一直显示模型不思考"。）
    → 所以要求**三个条件**：上轮有代码块 + 本轮带"接着改"的意图 + **上下文确实是代码**。

    ⚠️⚠️ 第三个条件里"上一轮的意图"最要紧（2026-09-27 补）：
    上一轮做 Excel / PPT 时，助手的回答里**经常贴一段 python 代码**
    （实测："已生成，代码如下：" + ```python from openpyxl …```）——
    那不能说明后面几轮还在写代码。漏掉它就会：
      上一轮「帮我做一个 Excel 表格」→ 回答带代码块
      这一轮「再帮我加一列合计」    → 只有接续词"再/加"
      ⇒ 切 qwen3-coder ❌（而它那轮**工具被整轮清空**，用户根本改不动那份文件）
    离线实测：12 条文档迭代句（Excel 5 / PPT 4 / 作文 3）**12 条全中**。
    """
    last_user = str(last_user or "")
    _last_u = next((i for i in range(len(messages) - 1, -1, -1)
                    if messages[i].get("role") == "user"), len(messages))
    prev_code_raw = False
    for _m in reversed(messages[:_last_u]):
        if _m.get("role") == "assistant":
            prev_code_raw = "```" in str(_m.get("content") or "")
            break
    prev_user = ""
    for _m in reversed(messages[:_last_u]):
        if _m.get("role") == "user":
            prev_user = str(_m.get("content") or "")
            break
    # 本轮是不是"要一个能点开的办公文件"（造物口吻 + 办公名词）。
    # ⚠️ 只看本轮的办公词是危险的：上轮写"读 csv 的脚本"、本轮"再改成支持 xlsx"
    #    —— 句中也有 xlsx，但这是**代码迭代**，必须让它切代码模型。
    #    所以额外要求"造物口吻"：只有用户**要文件**时才拦住（代码模型那轮工具被清空、
    #    make_xlsx 根本不在，拦下来才拿得到文件）。
    office_build = (_is_office_task(last_user)
                    and any(w in last_user for w in _BUILD_REQ))
    return (prev_code_raw
            and any(w in last_user for w in _CONTINUE_HINTS)
            # ⚠️ 写作 / 办公请求不受上一轮代码影响 ——
            # 「帮我写一篇作文，再改一下开头」里有"再/改"，上一轮又刚写过代码的话，
            # 光看前两个条件会被切到**代码模型**去写作文（它会跑偏，实测过）。
            # ⚠️ 这两条只拦**兜底路**：如果本轮用户明说要写代码，
            #   `_needs_task_model` 里的 `_is_code_task` 照样会切，不会漏。
            and not _is_writing_task(last_user)
            and not office_build
            # ⚠️ 上一轮的意图决定这是不是代码迭代：上轮写代码 → 继续按代码迭代；
            #   上轮写文档 → 按文档迭代（见上面 _is_doc_task 的说明）。
            and not _is_doc_task(prev_user))


_model_tags_cache = {"t": 0.0, "names": set(), "fail_t": 0.0}

def _installed_models() -> set:
    """本机已下载的 Ollama 模型名（缓存 60 秒，别每轮都去问一次）。

    ⚠️ 2026-11-11 修：原来只在**成功**时更新时间戳，异常分支直接 return 旧值 ——
       于是 Ollama 一关，`names` 永远是空集合，`if _model_tags_cache["names"]`
       永远为假，**每次都重新等超时**，60 秒缓存形同虚设。
       而本机连 11434 即使被拒绝也要等满 2 秒（实测 2004 ms，原始 socket 也一样），
       这段又是在 async def chat 里同步调用 → 每条消息白冻 2 秒。
       现在失败也记时间戳（fail_t），60 秒内不再重试。
    """
    now = time.time()
    # 成功和失败共用 60 秒节流：哪怕真的一个模型都没装，也不用每轮都去问
    if now - max(_model_tags_cache["t"], _model_tags_cache["fail_t"]) < 60:
        return _model_tags_cache["names"]
    names = set()
    try:
        import urllib.request
        url = str(config.load_config().get("ollama_url")
                  or "http://127.0.0.1:11434").rstrip("/")
        with urllib.request.urlopen(url + "/api/tags", timeout=5) as r:
            for m in (json.loads(r.read().decode()).get("models") or []):
                n = str(m.get("name") or "").strip()
                if n:
                    names.add(n)
                    if ":" not in n:
                        names.add(n + ":latest")
    except Exception:
        _model_tags_cache["fail_t"] = now
        return _model_tags_cache["names"]
    _model_tags_cache.update(t=now, names=names, fail_t=0.0)
    return names


def _ollama_port_alive(timeout: float = 0.6) -> bool:
    """11434 到底有没有人接。用裸 socket，比 urlopen 快（本机被拒要等 2 秒）。"""
    import socket
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", 11434))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _try_start_ollama() -> None:
    """端口没起来时，尽力拉一次 `ollama serve`（尽力而为，失败不吵）。

    为什么要有这个（2026-11-11 用户报「前端下载不了本地模型」）：
      · 打包版的一键启动.bat 里本来就会 `start "" /min ollama serve`，
      · 但**源码启动**（pythonw run.py）和桌面快捷方式完全没有这一步，
      · Ollama 一关，前端就是彻底的死链：列表空、下载报错、删除报错，
        而界面上只有一句「请先启动 ollama serve」，用户不知道该怎么办。

    只为「服务在不在」而拉起：ollama serve 不加载任何模型，
    显存占用是 0，真正吃显存要等有人发请求。
    """
    if _ollama_port_alive():
        return
    exe = ""
    try:
        import shutil
        exe = shutil.which("ollama") or ""
    except Exception:
        pass
    if not exe:
        for p in (os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe"),
                  r"C:\Program Files\Ollama\ollama.exe"):
            if os.path.isfile(p):
                exe = p
                break
    if not exe:
        logger.info("[启动] 没找到 ollama 可执行文件，本地模型功能需自行启动 Ollama")
        return
    try:
        import subprocess
        _base_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # 用「受管子进程」而不是脱离 job：
        #   · CREATE_NO_WINDOW 保证不弹黑窗；
        #   · **不**加 DETACHED/BREAKAWAY —— 实测脱离后在某些环境里会被
        #     后台回收得莫名其妙（进程在、端口不通），受管的反而稳定。
        #   · 代价是应用退出时 Ollama 一起停；下次启动再拉起来。
        #     Ollama 本来就有 4h keep_alive，重开的成本可以接受。
        subprocess.Popen([exe, "serve"],
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         creationflags=_base_flags)
        logger.info("[启动] Ollama 没在运行，已尝试拉起 ollama serve：%s", exe)
    except Exception:
        logger.warning("[启动] 拉起 ollama serve 失败（不影响云端对话）", exc_info=True)


def _is_writing_task(text: str) -> bool:
    """是不是"要写一整篇东西"（而不是问一句答一句）。"""
    t = (text or "")
    if not t or len(t) < 6:
        return False
    return any(k in t for k in _WRITING_HINTS)


# 纯创作 / 纯个人叙事：这类**没有"外部事实"可查**，不该因为它去联网。
# ⚠️ 用途见 `_needs_web_ctx`：写长文默认要提示"先核实事实"，
#    但散文/诗/家书这类硬拉去搜反而会把无关材料塞进文章里，必须排除。
_PURE_CREATIVE_HINTS = (
    "散文", "随笔", "诗歌", "诗", "家书", "日记", "感想", "心得", "读后感",
    "观后感", "祝福语", "祝福", "情书", "故事", "小说", "童话", "寓言",
    "歌词", "打油诗", "顺口溜", "笑话", "段子", "座右铭", "自我介绍",
    "我的寒假", "我的暑假", "我的童年", "游记", "抒情", "感悟",
    # ⚠️ 2026-09-28：作文类文体 —— 它们靠的是**写作能力**不是外部事实。
    #    "议论文"里含子串"论文"，会被 _WEB_FACT_NOUNS 命中而误判"要联网核实"（实测踩到）。
    "议论文", "说明文", "记叙文", "作文", "读书笔记", "演讲稿", "检讨书", "感谢信",
)


def _is_pure_creative(text: str) -> bool:
    """是不是"纯创作 / 纯个人叙事"（不需要外部事实，联网只会帮倒忙）。"""
    t = str(text or "")
    return bool(t) and any(k in t for k in _PURE_CREATIVE_HINTS)


# 「通用知识 / 方法技巧」类主题：**不需要联网核实**。
# ⚠️ 2026-09-28 实测踩到：`_needs_web_ctx` 原来是"长文任务只要不是纯创作就提示联网"，
#    于是「写一份《城市夜景摄影技巧》Word 文档」被判成要核实 → 模型真去 web_search，
#    搜完上下文被结果占满，**最后根本没生成文档**（用户反馈"文档不按要求"的真实来源之一）。
#    这类内容属于通用知识，模型自己就会；搜了反而挤掉写正文的额度。
_GENERIC_TOPIC_HINTS = (
    "技巧", "方法", "教程", "入门", "攻略", "心得", "经验", "诀窍", "窍门",
    "步骤", "基础", "常识", "注意事项", "要点", "误区", "经验分享",
    "礼仪", "习惯", "范文", "写法", "做法", "学习法", "效率",
    "科普", "通识", "讲解", "释义", "原理", "概念", "定义", "机制",
)

# 「具体实体」线索：型号 / 版本 —— 这些**会变、最容易编错**，出现它们才真需要联网
# （「大疆 Mini 5 Pro 使用技巧」要搜，而「摄影技巧」不要）。
_SPECIFIC_ENTITY_RE = re.compile(
    r"[A-Za-z]{2,}[\s\-]?\d{1,4}"            # Mini 5 / RTX5090 / iPhone 16
    r"|\d{1,4}[\s\-]?[A-Za-z]{2,}"            # 5 Pro / 16 Pro
    r"|v?\d+\.\d+"                            # 版本号 2.3 / v1.5
)
# 书名号 / 引号里包着的内容（常是主题名，未必是专有名词）
_QUOTED_RE = re.compile(r"[《【“\"「]([^》】”\"」]{2,20})[》】”\"」]")


def _is_generic_knowledge(text: str) -> bool:
    """主题像"通用知识 / 方法技巧"吗（这类不必联网核实）。"""
    t = str(text or "")
    return bool(t) and any(k in t for k in _GENERIC_TOPIC_HINTS)


def _has_specific_entity(text: str) -> bool:
    """话里有没有**具体型号 / 版本 / 专有名词**（有就该联网核实）。

    ⚠️ 引号里的内容要**排除通用知识词** —— 否则「城市夜景摄影技巧」
    这种被书名号一包，就会被误判成"专有名词 → 要联网"（实测踩到）。
    """
    t = str(text or "")
    if _SPECIFIC_ENTITY_RE.search(t):
        return True
    for m in _QUOTED_RE.finditer(t):
        inner = m.group(1)
        if not _is_generic_knowledge(inner) and not _is_pure_creative(inner):
            return True
    return False


# 用户话里出现的"本机路径"线索：盘符 / UNC / 家目录 / 常见 Unix 绝对路径。
_LOCAL_PATH_RE = re.compile(
    r"[A-Za-z]:[\\/]"                       # C:\  D:/  （Windows 盘符）
    r"|\\\\[A-Za-z0-9_.\-]+[\\/]"           # \\server\share
    r"|(?:^|[\s（(\"'「])~[\\/]"             # ~/  ~\
    r"|(?:^|[\s（(\"'「])/(?:home|Users|mnt|tmp|opt|var|data|root)/"
)


def _mentions_local_path(text: str) -> bool:
    """用户这句话里有没有**本机路径**。

    ⚠️ 有路径就**不能**换到专用代码模型。原因：
    qwen2.5-coder 不支持 Ollama 的原生工具调用，切过去就只能**整轮不给工具**
    （否则它把调用当 JSON 文本吐出来）—— 那样它既读不了文件也写不了文件。
    实测问它「读取 D:\\...\\demo.py 把代码展开给我看」，它会答
    **「抱歉，我无法读取或访问本地文件」**，还建议你"开启联网开关"（完全跑偏）。
    这种轮次留在默认模型上（它有 read_file / write_file / library）才做得成。

    "存到生成文库"**不算**：那一步由前端按钮/兜底逻辑落盘，不依赖模型调工具。
    """
    return bool(_LOCAL_PATH_RE.search(text or ""))


def _needs_task_model(text: str, prev_code: bool = False,
                      has_images: bool = False) -> bool:
    """要不要换用"专用模型"（非思考型）。

    ⚠️ **长文创作不算在内**（试过，更糟）：换成 qwen2.5-coder 之后确实不出空答案了，
    但它是代码模型，多轮里会跑偏 —— 把工具调用当 JSON 文本吐出来、
    话题漂到无关内容（让它写作文，它导出了一份"Python 网络请求示例"）。
    长文创作改走"保住输出额度 + 砍掉无关工具"的路子（见下面系统提示里
    "写作文 / 拟方案 / 写报告这类创作任务"那一段），仍用默认模型。

    ⚠️ **带本机路径的请求也不算** —— 见 `_mentions_local_path` 的说明。

    ⚠️⚠️ **本轮带了图片的更不算**（2026-09-27 补）——
    代码模型 `qwen3-coder:30b` 的能力段里**只有 completion / tools，没有 vision**
    （`ollama show` 实测），切过去图片**必然丢失**。而路由只看文本、不看有没有图，
    所以这些说法都会踩到：
      · 「**这张截图**里的报错怎么修，帮我改一下」→ 命中"报错+改" → 切代码模型 ❌
      · 「分析一下**这张图**的数据，写个脚本处理」→ 命中"脚本+写" → 切 ❌
      · 上一轮写过代码、这一轮贴图说「再帮我改两处」→ `prev_code` 命中 → 切 ❌
    用户主动贴图就是最强的"我要你看图"信号，**看不到图等于白传**。
    留在视觉模型上还能两全：qwen3-vl 既看图、也照样能写代码。

    prev_code：上一轮回答里有没有代码块。**迭代轮次全靠这个兜住** ——
    用户第二轮往往只说「再帮我改两处：加个深色模式」，这句话里一个代码关键词都没有，
    只按本句判定就会掉回默认（思考型）模型；而那个模型的系统提示里写着 library 工具，
    它会直接编「已保存到 xxx.html（3580 字节）」，**一个字代码都不给**（实测）。
    """
    if has_images:
        return False          # 图必须被"看见" → 只能留在有视觉能力的模型上
    if _mentions_local_path(text):
        return False          # 要读写本机文件 → 必须留在有工具的默认模型上
    if prev_code:
        return True           # 上一轮刚写过代码，这轮显然还在改它
    return _is_code_task(text)


# ---------------------------------------------------------------------------
# 文本协议工具循环 —— 给"不支持原生 tool_calls"的代码模型补上执行回路
# ---------------------------------------------------------------------------
# 背景：qwen2.5-coder 走不了 Ollama 的原生工具通道（带 tools 时**不返回 tool_calls**，
# 而是把调用当 JSON 文本写进正文）。于是代码模型这一轮的工具被清空，只能一次吐一坨代码。
# 后果就是：**写 → 跑 → 看报错 → 改** 这条回路断了 —— 而它恰恰是 vibecoding
# 与"只会写代码"的分水岭。最坑的是模型会顺着话头说"我已经验证过了"，其实根本没跑。
#
# 实测探针（3 组提示 × 2 个必须真跑的任务）：模型用**文本协议**时
# **6/6 都能吐出可解析的工具调用**，把结果喂回去还会接着改。所以回路可以自己搭。
#
# 做法：把文本里解析出来的调用**伪装成原生 tool_calls**，
# 直接复用下面那套「执行 / 前端事件 / 危险操作先问用户」的逻辑，零重复实现。
_TEXT_TOOL_RE = re.compile(r"```(?:tool|tool_call|json)?[ \t]*\r?\n(.*?)```", re.S)
# ⚠️ **只认这两个是"工具调用专用围栏"**，可以无条件从正文里删掉。
#    `json` 不能算进来：模型写 .json 文件、贴接口返回时也用它，
#    误删就是把用户要的内容吃掉了（见 _split_text_tool_calls 的说明）。
_TOOL_FENCE_RE = re.compile(r"```(?:tool|tool_call)[ \t]*\r?\n(.*?)```", re.S)
# ⚠️ 配套的"**没闭合**"版：只匹配那行标记本身（不含内容），用来清理残留。
#    见 _split_text_tool_calls 里 ① 的兜底说明 —— 流被截断时收尾的 ``` 永远等不到，
#    只靠上面那条正则会留下一行孤零零的 ```tool 在正文里。
_TOOL_FENCE_OPEN_RE = re.compile(r"```(?:tool|tool_call)[ \t]*\r?\n?")

# ---------- 「调用壳子」：模型自己带上的 XML 包装 ----------
# ⚠️ 这不是我们教的格式（我们教的是 ```tool 围栏），是模型从别处学来的习惯。
#    实测（2026-09-21 用户反馈）：**默认模型 qwen3-vl 偶尔**把整个调用包成
#    <function-call>{...}</function-call> 写进**正文**。而 Ollama 的原生工具通道
#    只认它自己模板里的 <tool_call>，认不出这个 → tool_calls 为空 →
#    后端以为"这轮就是普通回答"，于是那坨 JSON **被当成正文渲染成一张卡片**，
#    用户看到的就是一坨裸露的 {"name": "search_knowledge", ...}，而工具压根没执行。
#
#    为什么以前没被接住：文本协议的解析（_split_text_tool_calls）**只在代码模型那轮**跑
#    （见 `if code_model_on`），默认模型走原生通道、根本没经过它。
_TOOL_XML_NAME = r"function[ _-]?calls?|tool[ _-]?calls?"
# 成对的包装：<function-call>…</function-call>（前后缀必须同名，避免把普通 XML 吃进来）
_TOOL_XML_BLOCK_RE = re.compile(
    r"<\s*\|?\s*(%s)\s*\|?\s*>\s*(.*?)\s*<\s*/\s*\1\s*>" % _TOOL_XML_NAME,
    re.S | re.I)
# 只剩开标签（被截断 / 模型忘了闭合）——从开标签一直吃到结尾才安全
_TOOL_XML_OPEN_RE = re.compile(r"<\s*\|?\s*(%s)\s*\|?\s*>" % _TOOL_XML_NAME, re.I)
# 散落的空壳标签（里面的 JSON 已被别的分支摘走时剩下的）
_TOOL_XML_TAG_RE = re.compile(r"<\s*\|?\s*/?\s*(?:%s)\s*\|?\s*/?\s*>" % _TOOL_XML_NAME, re.I)
# 「半个标签」—— 模型**写到一半就放弃了**（或输出被截断），只剩 <function / <tool_call
# 这种开头，后面没跟 `>` 就直接接正文。实测（2026-09-21，实机复现）它单独占一行出现，
# 被流式原样推给了用户，正文开头冒出一截 `<function`，看着像程序坏了。
# ⚠️ 判据必须**很窄**，否则会吃掉正常内容（比如回答里讲 `<function>` 标签怎么写）：
#    只删"整行只有这么一截"的情况，以及"出现在正文最开头"的情况。
# ⚠️ `call` 那截**写成可选**：碎片往往正是缺了它（模型刚打出 `<function` 就放弃了）。
# ⚠️ 尾部的 `[A-Za-z0-9_.-]{0,24}` 只吃**标签名那种字符**，绝不能放 `[^>\n]` ——
#    那样会把同一行的正文整段吃掉（实测："<function以下是…" 整行没了）。
_TOOL_XML_STRAY_RE = re.compile(
    r"(?m)^[ \t]*<\s*/?\s*(?:function|tool)(?:[ _-]?calls?)?[ \t]*"
    r"[A-Za-z0-9_.\-]{0,24}>?[ \t]*$", re.I)
# 开头那个碎片：后面跟"换行"或"非空白非 >"的字符才算（后面直接跟 `>` 的更像在讲标签用法，放过）
_TOOL_XML_HEAD_RE = re.compile(
    r"^\s*<\s*/?\s*(?:function|tool)(?:[ _-]?calls?)?(?=\n|[^\s>])", re.I)

# ---------- 思考打转（复读）的检测 ----------
# ⚠️ 为什么要它：思考型模型偶尔会陷进**段落级复读** —— 同一句话换个连接词
#    反复写（「可能的题目：… 或者：… 可能需要换一个例子：… 例如：…」），
#    同一句出现三四遍。它不报错、不超时，只是把输出预算全烧在绕圈上，
#    用户看到的就是"思考过程里全是重复内容"（2026-09-19 反馈）。
#    采样参数（`repeat_last_n` / `repeat_penalty`）能大幅减少它，但压不干净 ——
#    真发生时至少要让用户看到实话、并在日志里留下证据。
_LOOP_PIECE_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]{6,}")
# ⚠️ 片段长度门槛（判据本意是抓**段落级**复读，实测重复的原句有 30~60 字）：
#   · 中文侧：**≥10 字**才算。9 字以下的多半是"用户导入的领域文档"这种常用短语，
#     在互不相同的句子里出现三次是**正常表达**，不是打转（实机误报抓到的就是这个）。
#   · 英文侧：**≥16**，因为正则会把 `search_knowledge` 拆成 `search` / `knowledge`，
#     模型正常讨论几次工具名就"重复 3 次"了（日志里 8 条警告全是同一个词 `search`）。
_LOOP_MIN_CJK = 10
_LOOP_MIN_ASCII = 16


def _looping_piece_ok(piece: str) -> bool:
    """这个片段值得拿去判"复读"吗（见 _LOOP_MIN_CJK / _LOOP_MIN_ASCII 上面的说明）。"""
    if piece.isascii():
        return len(piece) >= _LOOP_MIN_ASCII
    return len(piece) >= _LOOP_MIN_CJK


def find_looping_piece(text: str, threshold: int = 3) -> str:
    """在 text 里找"重复了 threshold 次以上的片段"并返回它；没有就返回 ""。

    只看长度 ≥6 的连续中英文片段：短词（"的"、"然后"、"所以"）重复是**正常语言**，
    拿它们做判据会疯狂误报。段落级复读的重复片段通常有 10~60 个字，跑不掉。
    ⚠️ 纯英文片段再叠一层过滤（见 _LOOP_MIN_ASCII）：英文侧的全是标识符碎片，
    正常讨论工具名就会"重复"，实测假警报就是这么来的。
    """
    if not text:
        return ""
    count = {}
    for piece in _LOOP_PIECE_RE.findall(text):
        if not _looping_piece_ok(piece):
            continue
        n = count.get(piece, 0) + 1
        count[piece] = n
        if n >= threshold:
            return piece
    return ""

# 代码轮可用工具的**文本协议说明**（键名同时充当白名单）
_TEXT_TOOL_DOCS = {
    # ⚠️ 这个必须排在第一位：它是"动手之前"该做的事。
    # 2026-09-21 之前它**不在这张表里**，所以代码轮的模型连"能问用户"都不知道
    # （白名单是 `set(code_text_tools)`，不在这张表里就调不动），
    # 表现就是"不管需求多含糊，都闷头写一份出来"。
    "ask_user": '信息不足时**在界面上弹问答框**问用户（**别在回答里用文字问**）。'
                '要产出东西却缺关键前提时才用，例如：做程序缺语言/输入输出/运行环境，'
                '写文档缺用途篇幅给谁看，画图缺风格和画面要点。'
                '参数 {"questions": [{"question": "一句话问题", "header": "4-8字短标签", '
                '"options": ["候选答案1", "候选答案2"]}]}。'
                '⚠️ 问题个数不限，也可以**分多轮**问；但不许问重复的、不许凑数，'
                '每个问题都要能改变你怎么做。'
                '⚠️ 用户已经说清楚了就别问，直接做。',
    "run_python": '真正运行一段 Python 代码，拿到真实输出与报错。'
                  '参数 {"code": "完整可运行代码（要 print 出结果）"}',
    "save_file": '把产物存进「生成文库」（覆盖同名文件，用户能在界面上看到）。'
                 '参数 {"name": "文件名（如 stats.py）", "content": "文件全文"}',
    "read_saved": '读取「生成文库」里已有的文件。'
                  '**改一个已有文件之前必须先读它**，不要凭记忆重写。'
                  '参数 {"name": "文件名，如 stats.py"}',
    "list_saved": '列出「生成文库」里现在有哪些文件（含子目录）。参数 {}',
    "read_file": '读取本机的一个文本文件（用户给的绝对路径）。'
                 '参数 {"path": "文件的绝对路径"}',
    # 开发工作区（人机协同开发的正路）：路径一律相对，后端拼绝对路径
    "workspace_list": '列出「开发工作区」里现有的文件。参数 {}',
    "workspace_read": '读取工作区里的一个文件。**改之前必须先读**，'
                      '不要凭记忆重写（用户可能刚在前端手改过）。'
                      '参数 {"rel": "相对路径，如 app.py"}',
    "workspace_write": '把内容写进工作区文件（存在则覆盖，旧版自动进回收站）。'
                       '**要给整份内容**。参数 {"rel": "相对路径", "text": "完整内容"}',
    "workspace_run": '运行工作区里的一个 .py，拿到真实输出与报错（工作目录=文件所在目录）。'
                     '参数 {"rel": "相对路径，如 app.py", '
                     '"args": "可选，命令行参数，如 add 张三 138", '
                     '"stdin": "可选，预先喂给程序的标准输入，一行对应一次 input()"}',
    # ---- 「完全自动开发项目」必需：从零建项目、铺结构、改名、清理 ----
    # 只有 write/read/run 的话，模型只能改**已有**文件，没法把项目搭起来。
    "workspace_new_project": '新建一个开发项目并立刻切进去，之后都用相对路径。'
                             '**从零开始做东西时第一步就调它。**'
                             '参数 {"name": "项目名，如 todo-app"}',
    "workspace_projects": '列出所有项目（· 是当前项目）。不确定现在在哪个项目里就先调它。参数 {}',
    "workspace_use_project": '切换到另一个已有项目。参数 {"name": "项目名"}',
    "workspace_mkdir": '新建一个空目录（注意：写文件时父目录会自动建，'
                       '只有**要空目录**时才需要它）。参数 {"rel": "相对路径，如 assets"}',
    "workspace_delete": '删除项目里的文件或目录（进 _回收站，可捞回）。'
                        '参数 {"rel": "相对路径"}',
    "workspace_move": '重命名或移动文件/目录。参数 {"rel": "原路径", "to": "新路径"}',
    # 编程时同样用得上：查新用法 / 查资料 / 生图当素材 / 拿当前时间
    "web_search": '联网搜索最新资料（库的新用法、报错原因、版本差异…）。'
                  '参数 {"query": "搜索词"}',
    "web_read": '联网读某个网页的**正文**（搜索只给摘要，要细节就用它点进去）。'
                '**用户直接贴了一个网址时，就用它把内容读下来再回答**，'
                '不要凭网址猜内容、也不要说"我打不开链接"。'
                '参数 {"urls": ["https://…"]}',
    "github_push": '把当前项目上传到代码托管平台（GitHub / Gitee / 自建 Git）。'
                   '参数 {"repo": "git@github.com:user/repo.git", "message": "提交说明"}',
    "search_knowledge": '在用户的知识库里检索资料。参数 {"query": "检索词"}',
    "generate_image": '生成一张图片并展示（可以直接当网页 / 应用的素材）。'
                      '参数 {"prompt": "画面描述", "size": 512, '
                      '"aspect": "1:1 / 4:3 / 3:4 / 16:9 / 9:16 / 3:2 / 2:3", "hd": false}'
                      ' —— 用户说"竖版/海报/手机壁纸"用 9:16 或 2:3，'
                      '说"横版/宽屏/PPT 配图/Banner"用 16:9 或 3:2；'
                      '要写实/要印出来把 size 提到 768、必要时 hd=true。',
    "compose_images": '把**多张图片拼接/缝合**成一张（横排 / 竖排 / 网格），可选统一风格。'
                      '用户说「把这几张拼起来 / 拼成一张 / 做个对比图 / 拼个长图」时用它。'
                      '参数 {"sources": ["路径…(可省，默认用本轮拖进来的全部图片)"], '
                      '"layout": "horizontal|vertical|grid", "size": 1024, "gap": 0, '
                      '"bg": "FFFFFF", "cols": 2, "harmonize": 0}'
                      ' —— 拼接是**像素级**的（不重绘、接缝无缝）；'
                      'harmonize>0 才会再跑一次图生图统一色调（0.25~0.4，别更大）。'
                      '⚠️ 它只做**排版式拼接**，不会把 A 图里的人搬到 B 图里。',
    "make_pptx": '生成一份真正的 PPT（.pptx），存进生成文库并给出可点下载链接。'
                 '**你只管想内容，排版由工具做，不用写代码。**'
                 '参数 {"title": "封面主标题", "subtitle": "副标题（可选）", '
                 '"author": "落款（可选）", "theme": "blue/green/warm/purple/mono/red", '
                 '"colors": {"accent": "#B8860B", "cover_bg": "#8B0000"}, '
                 '"cover_image": "封面整页背景图（本地路径；用户给了图并说放封面就用它）", '
                 '"slides": [{"title": "页标题", '
                 '"bullets": ["要点1", "- 二级要点", {"text": "重点", "hl": true}], '
                 '"image": "本页配图（本地路径，可用用户附的图）", '
                 '"image_query": "配图搜索词（可选，会自动搜图插入）", '
                 '"badge": "右上角小标签（可选，如 重点/必考/KPI）", '
                 '"caption": "页脚题注（可选，如 数据来源：…）", '
                 '"section": false}]} —— section=true 是章节过渡页。'
                 '要点写成 {"text":…, "hl":true} 就是**荧光笔高亮**（标重点用它）；'
                 '页面还能用 layout 换版式：two_col 两栏 / image_right 图文并排 / '
                 'table 表格 / chart 图表 / cards 卡片 / stats 大数字 / steps 步骤 / '
                 'timeline 时间线 / quote 引言 / toc 目录。'
                 '生成后把返回的下载链接**原样**告诉用户。',
    "map_plan": '【地图】查地点 / 规划路线，并把结果画成地图卡片给用户看。'
                '用户说「怎么走 / 规划路线 / 从A到B多远多久 / 某地方在哪」时用它。'
                '参数 {"places": ["广州塔", "汕头大学"], "city": "广州", '
                '"route": {"from": "广州塔", "to": "广州白云机场", "mode": "driving"}, '
                '"offline": true} —— places 和 route 至少给一个；'
                '**用户说了在哪个城市就把 city 填上**（"广州市内有什么商场" → city:"广州"）；'
                '不填的话，标"天河城"这种同名地点会被解析到"江西省南昌市进贤县天河城"，很离谱。'
                'mode 可选 driving(默认)/foot/bike/transit(公交)。'
                'transit 走高德公交换乘，返回**具体线路、票价、换乘站**。'
                '配了高德 key 时：驾车带**实时路况**、步行骑行是**真实路径**；'
                '没配 key 回退 OSRM 时，免费路网只给驾车路径、步行骑行时间是估算，'
                '返回里会带 note，**必须把这话转告用户**，不能让他以为是真步行路线。'
                '中长途会自动给**多条备选路线**并挑一条推荐，返回里有 routes 和 reason；'
                '把「为什么推荐这条」照实说出来，只有一条时也别硬凑。'
                '用户问天气或要把出行讲清楚时给 weather:true。'
                '⚠️ 它跟着「联网」开关走，而且**地图不做任何本地缓存**：'
                '开着联网就实时上网查；关着联网＝离线模式，此时只剩一份内置常用地名表可用，'
                '路线只能给"直线距离"。'
                '离线时查不到就**照实说查不到**，绝不许编坐标、编距离；'
                '路线只会给"直线距离"，必须讲明那是直线、不是实际道路。'
                '返回里是**真实**距离与用时，照实说，别自己算。'
                '⚠️ **只有用 mode=transit 拿到的才能讲公共交通**。没走 transit 就不许编'
                '「坐 X 路公交、票价 Y 元、每 Z 分钟一班」这种具体线路（实测模型真的会编）；'
                '最多说一句「这段距离也可以考虑公共交通」。'
                '⚠️ 配了高德 key 时驾车时间**已含实时路况**，可以照实说；'
                '但别编「XX 路段现在堵」这种具体路况 —— 接口不给这个。',
    "nearby_places": '【地图】查某个地点**周围**有哪些场所（按半径+类别），并在图上标出来。'
                     '用户说「附近有什么吃的 / 这周围有没有便利店 / 附近哪能买药 / '
                     '找一下附近的银行」这类周边搜索时用它。'
                     '参数 {"place": "汕头大学", "category": "餐厅", "radius": 1500}；'
                     'place 也接受 "23.35,116.68"，也可以是城市名；'
                     'category 用中文日常说法即可（餐厅/咖啡馆/便利店/超市/购物中心/药店/医院/'
                     '银行/加油站/停车场/酒店/学校/公交站/公园/厕所）；radius 单位米，默认 1500，最大 50000。'
                     '**问「哪里有什么 XX / 有哪些 YY」一律用这个工具查真实数据**，'
                     '别凭自己知道的地标凑几个丢给 map_plan（那是编的，还经常给错城市）：'
                     '「广州市内有什么商场」→ place:"广州市", category:"购物中心", radius:30000。'
                     '· 数据源：配了高德 key 时走**高德**（**有真实评分和人均消费**），'
                     '没配或离线时回退 OpenStreetMap（没有评分）。'
                     '⚠️ **返回里带 rating 就照实念** —— 那是真实评分，可以直接用来推荐和排序；'
                     '**没有 rating 时绝不许自己编评分、星级或评价**。'
                     '⚠️ **只准转述返回里确实有的字段**（名字/距离/评分/人均/地址/电话/营业时间），'
                     '**不要补数据里没有的东西** —— 实测模型会顺口加「校内主干道旁」'
                     '「校门对面」这类位置描述和「学生常去」这类评价，那全是编的。',
    "make_xlsx": '生成一份真正的 Excel 表格（.xlsx），存进生成文库并给出可点下载链接。'
                 '用户说「做个表格 / Excel / 统计表 / 对照表 / 报表 / 台账 / 预算表」'
                 '或给了一堆数据要整理时用它。**你只填数据，不用写代码。**'
                 '参数 {"filename": "文件名（可省）", "theme": "blue/green/warm/purple/mono/red", '
                 '"colors": {"accent": "#B8860B"}, '
                 '"sheets": [{"name": "工作表名", "title": "表内大标题（可省）", '
                 '"header": ["列1","列2"], "rows": [["A", 120.5], ["B", 300]], '
                 '"formats": ["text","money"], "widths": [14,12], "total_row": true, '
                 '"total_cols": [1], "note": "数据来源：…", '
                 '"chart": {"kind":"column","title":"…","categories_col":0,"value_cols":[1]}, '
                 '"conditional": {"col":1,"type":"data_bar"}}]}'
                 ' —— 可放多张表（先明细后汇总）。formats 支持 text/int/number/money/'
                 'percent/date。数字直接写数字，别加千分位或￥。',
    "make_docx": '生成一份真正的 Word 文档（.docx），存进生成文库并给出可点下载链接。'
                 '**用户要「文档 / 报告 / 方案 / 说明书 / 写成 Word」时用它，'
                 '不要用 library 写 .md 再让用户自己转。**'
                 '参数 {"title": "标题", "cover": true, "toc": true, '
                 '"theme": "blue/green/warm/purple/mono/red", '
                 '"colors": {"accent": "#B8860B"}, '
                 '"blocks": [{"type": "heading", "level": 1, "text": "一、背景"}, '
                 '{"type": "para", "text": "正文，可用 **加粗**、==高亮== 标重点"}, '
                 '{"type": "bullet", "items": ["要点", "- 二级要点"]}, '
                 '{"type": "table", "header": ["列1","列2"], "rows": [["a","b"]]}]}'
                 ' —— 块类型还有 quote/callout/code/image/divider/pagebreak/number/end。',
    "edit_office": '查看或修改生成文库里的 .docx / .pptx（改文字、换配色、加删页、调样式）。'
                   '参数 {"rel": "文件名.docx", "action": "inspect"} 先看结构；'
                   '再 {"rel": "...", "action": "edit", "ops": [{"op":"replace_text",'
                   '"find":"旧","replace":"新"}, {"op":"set_text","slide":3,"shape":1,'
                   '"text":"新标题"}, {"op":"add_text","slide":3,"text":"...","x":1,"y":6,'
                   '"w":6,"h":0.5,"size":16}, {"op":"set_theme","theme":"green"}]}。'
                   '**编号要用 inspect 给的那些，别猜。**',
    "get_time": '获取当前日期与时间。参数 {}',
    "video_edit": '【剪视频】裁剪 / 拼接 / 配音 / 加文字水印 / 烧字幕 / 抽音轨 / '
                  '图片成片 / 压缩转码，底层是本机 ffmpeg，**能保留原声**。'
                  '参数 {"action": "trim|concat|audio|text|subtitle|extract_audio|'
                  'images_to_video|compress|info", ...}，其余参数：'
                  'source（输入视频）/ sources（多段列表）/ audio（音乐）/ subtitle（.srt）'
                  '/ start、end、duration（秒）/ mode（replace|mix）/ volume / '
                  'text、position、fontsize（压字）/ crf、width（压缩）。'
                  '⚠️ 做不了多轨时间线编辑、转场特效、AI 抠视频主体 —— 用户要就说做不到。',
    "connect_amap": '【连接高德地图】请用户把他的高德 key 填进来，填完当场验证、立刻生效。'
                    '**用户要用地图、而本机还没配高德 key 时，先调它** ——'
                    '别直接说做不到，也别默默用着弱底图不吭声。'
                    '填了能搜到全国小店、有真实评分/实时路况/公交换乘；'
                    '不填就退回 OpenStreetMap（大城市和道路能查，小店评分都没有）。'
                    '参数 {"reason": "为什么现在需要（一句话，会给用户看）"}，可省略。'
                    '⚠️ 用户说「不用」就按没 key 继续，别反复问。',
}
_TEXT_TOOL_NAMES = set(_TEXT_TOOL_DOCS)
# 正文里出现这些，就说明模型开始写"文本协议工具调用"了 —— 用来做流式时的边界判断
_TEXT_TOOL_MARKS = ("```tool", "```tool_call", "```json", '{"name"')

# ⚠️ **聊天轮（默认模型）用的保守标记集**：只认"无歧义"的壳子。
#    代码轮那套（上面）连 ```json / `{"name"` 都算 —— 那轮模型本来就在用文本协议调工具，
#    扣住是应该的；但聊天轮的正文里出现 JSON 是**正常内容**（模型在讲接口、贴数据），
#    照抄那套会让正文从 JSON 那句起全被扣住、**逐字流式直接没了**，等整轮说完才砸下来。
#    所以这里只留 XML 包装与 ```tool 围栏 —— 正常回答不会长这样。
_LEAK_MARKS = ("```tool", "```tool_call",
               "<function-call", "<function_call", "<functioncall", "<function call",
               "<tool_call", "<tool-call", "<tool call",
               "<function-calls", "<tool_calls")
# 裸调用（模型什么壳子都不套）**只能靠"开头就是调用形状"来认** ——
# 它没有可匹配的标记，所以单独给一条判据：正文**以 { 开头、第一个键就是 name/arguments**
# 时，先把整段扣住，等轮末用 _strict_bare_calls 判：
#   真的是调用 → 一个字都不发（摘掉去执行）；不是（比如用户就要一段 JSON）→ 原样补发。
# ⚠️ 只认"开头"，不认"中间" —— 正文中间出现 {"name": …} 多半是在讲接口，扣住会毁掉流式。
_CALL_HEAD_RE = re.compile(
    r'^\s*\{\s*"(?:name|tool|function|arguments|parameters|args|input)"\s*:', re.I)
# 同一个"调用形状"的开头，但**不要求有冒号、也不要求引号闭合** ——
# 用来认"写到一半放弃"的残片（实测模型会只写 `{"name` 就改口说人话，
# 连 `name` 后面的引号都没写）。见 _strip_call_head_debris。
_CALL_HEAD_ANY_RE = re.compile(
    r'^\s*\{\s*"(?:name|tool|function|arguments|parameters|args|input)"?', re.I)

# 文本工具名 → 真实工具名（save_file 其实就是 library 的 write 动作）
_TEXT_TOOL_ALIAS = {"save_file": "library"}


def _code_text_tools(cfg: dict) -> dict:
    """代码模型这一轮能用哪些文本工具（按设置里的开关裁剪）。

    开关语义与聊天轮**完全一致**：关着联网就不给 web_search，
    关着知识库就不给 search_knowledge，关着本地算代码就不给 run_python。
    """
    out = dict(_TEXT_TOOL_DOCS)
    if not cfg.get("code_exec_enabled", False):
        out.pop("run_python", None)          # 沙箱没开就不给执行能力
    if not cfg.get("web_enabled", False):
        out.pop("web_search", None)
        out.pop("web_read", None)
    if not cfg.get("rag_enabled", False):
        out.pop("search_knowledge", None)
    # ⚠️ 问几条也跟着询问模式走（2026-09-23）：代码轮的白名单就是这张表的键，
    # 描述写死了"问 1~2 个"，深度模式下也只会问一两个。
    if "ask_user" in out and str(cfg.get("ask_mode") or "quick").lower() == "deep":
        out["ask_user"] = out["ask_user"] + (
            "⚠️ 当前是**深度询问**模式：一次给出 **5~7 个**问题，"
            "拿到答复后若还有影响实现的关键空白，**再问一轮**（通常 2~3 轮），"
            "直到需求清楚再动手。")
    return out


def _map_text_tool_args(name: str, args: dict):
    """把文本工具的参数翻译成真实工具 schema 认识的形状。"""
    if name == "save_file":
        rel = str(args.get("name") or args.get("path") or "").strip()
        return "library", {"action": "write", "name": rel,
                           "content": args.get("content") or args.get("text") or ""}
    if name == "read_saved":
        return "library", {"action": "read",
                           "name": str(args.get("name") or args.get("path") or "").strip()}
    if name == "list_saved":
        return "library", {"action": "list"}
    return name, args


def _as_tool_call_obj(obj, allowed=None):
    """校验并规整一个候选调用；不是合法调用就返回 None。

    allowed = 允许执行的名字集合。**默认是代码轮的文本工具名**；
    聊天轮要传"本轮真正给过模型的工具名"，见 _split_text_tool_calls 的说明。
    """
    if not isinstance(obj, dict):
        return None
    name = obj.get("name") or obj.get("tool") or obj.get("function")
    if isinstance(name, dict):                      # OpenAI 风格 {"function": {...}}
        name = name.get("name")
    args = obj.get("arguments")
    if args is None:
        args = obj.get("parameters") or obj.get("args") or obj.get("input")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            return None
    if not isinstance(name, str) or not isinstance(args, dict):
        return None
    name = name.strip()
    # ⚠️ 必须走白名单：代码轮里模型会写大段 HTML/JSON，里面本来就带
    # {"name": ...} 这种字段，不做白名单会把业务数据误当成工具调用去执行。
    if name not in (allowed if allowed is not None else _TEXT_TOOL_NAMES):
        return None
    # 模型偶尔把代码里的换行写成**字面量** \n（探针里 V3 提示词下就出现了），
    # 那样丢进沙箱必然语法错误 —— 顺手兜一下。
    code = args.get("code")
    if isinstance(code, str) and "\n" not in code and "\\n" in code:
        args = dict(args, code=code.replace("\\n", "\n").replace("\\t", "\t"))
    return {"name": name, "arguments": args}


def _loads_lenient(s: str):
    """宽松解析一段"可能是 JSON"的文本 —— 失败时**补上缺的右括号**再试一次。

    ⚠️ 为什么需要：模型经常把工具调用的 JSON **写残缺**。实测抓到的原文是
    `{"name": "workspace_write", "arguments": {"rel": "timer.py", "text": "…"}` ——
    代码、引号、内层 `}` 全对，**就是漏了最外层的那个 `}`**。
    这种残缺以前会让整个工具调用**静默失效**（解析失败 → 不执行 → 那块 JSON
    还留在正文里显示成一张"代码卡片"，用户点运行得到 `SyntaxError`）。

    补括号是**只加不减**的操作，不会改坏原有内容；
    但**字符串没闭合就放弃**（那是被截断了，硬补会把半截代码当成完整代码写盘）。
    """
    s = (s or "").strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        pass
    stack, in_str, esc = [], False, False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
    if in_str or not stack:
        return None                      # 截断在半截字符串里 / 本来就没缺括号
    try:
        return json.loads(s + "".join("}" if c == "{" else "]" for c in reversed(stack)))
    except Exception:
        return None


# 一个"调用对象"允许出现的键 —— 多出别的键（age/城市…）就说明那是业务数据，不是调用
_CALL_OBJ_KEYS = {"name", "tool", "function", "arguments", "parameters", "args", "input"}
# 模型**自己编出来的**"工具返回"块（实测跟着裸调用一起出现）：
#     <result>…</result> / <output>…</output> / <tool_result>…
_TOOL_ECHO_BLOCK_RE = re.compile(
    r"<\s*/?\s*(?:result|output|tool_result|工具结果|返回结果)\s*>[\s\S]*?"
    r"(?:<\s*/\s*(?:result|output|tool_result|工具结果|返回结果)\s*>|\Z)", re.I)


def _line_isolated(text: str, start: int, end: int) -> bool:
    """这段内容是不是"自成一整块"：所在行前面只有空白、后面的这一行也只剩空白。

    用来区分"模型在调用"和"模型在句子中间举例"（后者前后都有正文）。
    """
    ls = text.rfind("\n", 0, start) + 1
    if text[ls:start].strip():
        return False
    le = text.find("\n", end)
    tail = text[end:] if le < 0 else text[end:le]
    return not tail.strip()


def _strict_bare_calls(text: str, allowed=None):
    """聊天轮里那些**裸着**的调用（什么壳子都不套），尽量挑准 —— 宁漏勿误。

    实测原样（2026-09-21 实机复现，`run_python` **一次都没执行**）：
        {"name": "run_python", "arguments": {"code": "print('…')"}}
        <result>
        # 键值对示例
        …
        </result>
    它既没有 ```tool 围栏、也没有 XML 壳子 —— Ollama 认不出、我们以前也不认，
    于是工具没执行，那坨 JSON 连着模型**编出来的**结果一起显示给了用户。

    ⚠️ 为什么不能"见 JSON 就认"：用户问「用 JSON 举个例子」时，模型给的
       ```json 块是**要给他看的内容**；正文里讲接口也会贴 JSON。
       ⇒ 四条一起卡：
         ① 不在代码围栏里（围栏里的 JSON 是给用户的内容）
         ② 自成一整块（整行只有它，不是夹在句子中间的举例）
         ③ 键名只能是调用那几种（带 age/城市 这种业务字段的一律不算数）
         ④ 名字必须在**本轮真给过它的工具**里（顺带管住"联网/知识库"开关）
    """
    out = []
    if not text:
        return out, text
    fenced = [(m.start(), m.end()) for m in _FENCE_RE.finditer(text)]

    def in_fence(i):
        return any(a <= i < b for a, b in fenced)

    hits = []
    pos = 0
    while True:
        start = text.find("{", pos)
        if start < 0:
            break
        pos = start + 1
        if in_fence(start):
            continue
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    seg = text[start:i + 1]
                    obj = _loads_lenient(seg)
                    if (isinstance(obj, dict) and set(obj) <= _CALL_OBJ_KEYS
                            and _line_isolated(text, start, i + 1)):
                        call = _as_tool_call_obj(obj, allowed)
                        if call:
                            hits.append((start, i + 1, call))
                    break
    # 从头往后删会打乱后面的下标 → 从后往前删
    for start, end, call in reversed(hits):
        text = text[:start] + text[end:]
        out.append(call)
    out.reverse()
    return out, text


def _calls_from_body(body: str, allowed=None):
    """从一段"可能是工具调用"的文本里抠出调用 —— 一整坨、或里面塞了好几个都行。

    为什么不是简单 json.loads：实测模型常常把**两个**调用塞进同一个块
    （先 write 再 run），而且第一个后面还有逗号/换行。所以先整体试，
    失败了再按平衡括号逐个扫 —— 与正文扫描用的是同一套判据。
    """
    out = []
    body = (body or "").strip()
    if not body:
        return out
    obj = _loads_lenient(body)
    if obj is not None:
        call = _as_tool_call_obj(obj, allowed)
        if call:
            return [call]
    for start in (i for i, c in enumerate(body) if c == "{"):
        depth = 0
        for i in range(start, len(body)):
            if body[i] == "{":
                depth += 1
            elif body[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        call = _as_tool_call_obj(json.loads(body[start:i + 1]), allowed)
                    except Exception:
                        call = None
                    if call:
                        out.append(call)
                    break
    return out


def _strip_call_head_debris(text: str) -> str:
    """删掉正文**开头**那截"写到一半放弃"的裸调用残片。

    实测（2026-09-22）：模型想调工具，起了个头 `{"name` 之后就改成正常说话了 ——
    既解析不出调用（不是合法 JSON），扣住的尾巴又会被原样补发，
    于是用户看到的回答变成「{"name您好！我是……」这种，前面糊了一截乱码。

    ⚠️ 判据必须**两条同时满足**才敢删，否则会把用户要的 JSON 吃掉：
      ① 以 `{"name"` 这类调用键开头；
      ② **整段不是合法 JSON**，且残片后面紧跟**中文**（JSON 里不可能出现中文标点之外的裸中文键外内容）。
    用户要一段 JSON 示例时，整段是合法 JSON → 条件 ② 不成立 → 原样保留。
    """
    t = text or ""
    m = _CALL_HEAD_ANY_RE.match(t)
    if not m:
        return t
    if _loads_lenient(t) is not None:      # 整段是合法 JSON → 是用户要的内容
        return t
    rest = t[m.end():]
    if rest[:1] and ("\u4e00" <= rest[0] <= "\u9fff"):
        return rest.lstrip()
    return t


def _split_text_tool_calls(text: str, allowed=None, bare: bool = True):
    """从正文里拆出**所有**「泄漏成文本的工具调用」。

    返回 (calls, 清理后的正文)。清理后的正文里，**所有**调用壳子都被删掉了 ——
    ⚠️ 只删第一个是不够的：实测模型一轮里会连着输出两个块（比如先 write 再 run），
    第二个会被原样留在正文里显示成一坨 JSON 给用户看，而且还会被静默丢掉不执行。

    ⚠️ 它同时服务两种模型，**两轮的"敢删到哪一步"不一样**：
      · 代码轮（qwen2.5-coder，只能走文本协议）：
        `allowed=代码轮可用的文本工具`、`bare=True` —— 裸 JSON 也敢认（宽松扫描），
        因为那轮模型本来就是把调用当 JSON 吐在正文里。
      · 聊天轮（qwen3-vl，有原生工具通道，但**偶尔也乱写**）：
        `allowed=本轮真正给过它的工具名`、`bare="strict"` ——
        带壳子的（```tool / `<function-call>`）照认；裸 JSON 只认**很挑**的那种
        （不在围栏里、自成一整块、键名只有调用那几种，见 _strict_bare_calls），
        因为正文里的 JSON 大多是**给用户看的内容**。
    """
    raw = text or ""
    if not raw:
        return [], raw
    # ⚠️ 先摘掉**开头那截裸调用残片**（见 _strip_call_head_debris）：
    #    它不含任何标记（连 `"name"` 的闭合引号都没有），下面的快路径判据
    #    全都认不出来，会直接原样返回 —— 所以必须在快路径**之前**处理。
    raw = _strip_call_head_debris(raw)
    # ⚠️ 快路径要同时看**三个**标记，不能只看 `"name"`：
    #    模型经常只吐一对**空的** ```tool（或空的 <function-call></function-call>）
    #    当分隔符（整段正文里一个 "name" 都没有），那种也必须删掉 ——
    #    否则前端会给它渲染出一张空的「代码卡片」，
    #    用户看到的就是"莫名其妙好几张一样的卡片"。
    has_fence = ("```tool" in raw) or ("```tool_call" in raw)
    has_xml = bool(_TOOL_XML_OPEN_RE.search(raw))
    has_stray = bool(_TOOL_XML_STRAY_RE.search(raw)) or bool(_TOOL_XML_HEAD_RE.match(raw))
    # 裸调用需要**同时**有 name 和参数键才值得往下扫（避免拿普通正文白跑一遍）
    has_bare = ('"name"' in raw) and any(
        k in raw for k in ('"arguments"', '"parameters"', '"args"', '"input"'))
    if not has_fence and not has_xml and not has_stray and not has_bare:
        return [], raw

    calls, cleaned = [], raw
    # ① 代码围栏里的（模型最常这么写）
    #
    # ⚠️⚠️ 显式工具围栏（```tool / ```tool_call）**一律从正文里删掉**，
    #    不管里面的 JSON 能不能解析、甚至是不是空的 —— 这条不能省：
    #    模型把 JSON 写残缺是常事（最常见就是漏掉最外层 `}`），
    #    而以前是"解析失败就 continue"，于是那个块**原样留在正文里**，
    #    前端把每个围栏块都渲染成一张「代码卡片」→ 用户看到好几张一模一样的卡片，
    #    点「▶ 运行」还会把那段 JSON 当成 Python 去跑，得到
    #    `SyntaxError: '{' was never closed`（实测用户就是这么被卡住的）。
    #    模型还爱吐一对**空的** ```tool 当分隔符，也一样删干净。
    if has_fence:
        for m in list(_TOOL_FENCE_RE.finditer(raw)):
            cleaned = cleaned.replace(m.group(0), "")
            body = m.group(1).strip()
            if not body:
                continue                  # 空的分隔块：删掉就行，没什么可执行的
            _got = _calls_from_body(body, allowed)
            if _got:
                calls.extend(_got)
            else:
                # 解析不出来 = 这个调用**没有执行**。别静默 —— 记下来好排查
                # （否则现象是"我明明调了工具，怎么没反应"，日志里一个字都没有）。
                logger.warning("[text-tool] 工具块解析失败、已跳过：%s", body[:160])
        # ⚠️⚠️ **没闭合的围栏**（流被截断、或模型写完 JSON 就直接接着写正文）：
        #    上面的 `_TOOL_FENCE_RE` 要求有收尾的 ```，匹配不到 → 那个 "```tool"
        #    标记会**原样留在正文里**。实测（2026-09-22 回放真实会话
        #    `sessions/mode-quick.json`）：正文里冒出一行孤零零的 ```tool。
        #    这里兜底把残留的标记行删掉。
        #    ⚠️ 只删**标记本身**、绝不吞后面的正文 —— 实测模型常常在 JSON 之后
        #       继续写正常内容（"…接下来我们运行这个应用"），整段截掉会误删用户要的东西。
        if _TOOL_FENCE_OPEN_RE.search(cleaned):
            cleaned = _TOOL_FENCE_OPEN_RE.sub("", cleaned)

    # ①.5 XML 包装的（<function-call>…</function-call> / <tool_call>…</tool_call>）
    #     ⚠️ 这是默认模型偶尔会用的写法（2026-09-21 用户反馈），
    #        Ollama 的原生通道不认它，所以必须在这里接住。
    #     与围栏同理：**不管解析成不成功，壳子一律从正文里删掉** ——
    #        解析失败还留着的话，用户看到的就是一坨裸露的 JSON。
    if has_xml:
        for m in list(_TOOL_XML_BLOCK_RE.finditer(cleaned)):
            cleaned = cleaned.replace(m.group(0), "")
            body = (m.group(2) or "").strip()
            if not body:
                continue
            _got = _calls_from_body(body, allowed)
            if _got:
                calls.extend(_got)
            else:
                logger.warning("[text-tool] XML 工具块解析失败、已跳过：%s", body[:160])
        # 只剩开标签的（被截断 / 忘了闭合）—— 从开标签起全删，宁缺勿滥：
        # 那种情况后面跟的必然是调用体，留着就是把半截 JSON 糊在正文里。
        m = _TOOL_XML_OPEN_RE.search(cleaned)
        if m:
            body = cleaned[m.end():].strip()
            cleaned = cleaned[:m.start()].rstrip()
            if body:
                _got = _calls_from_body(body, allowed)
                if _got:
                    calls.extend(_got)
                else:
                    logger.warning("[text-tool] 半截 XML 工具块解析失败、已跳过：%s",
                                   body[:160])
        # 散落的空壳标签（JSON 已被别的分支摘走时剩下的）
        cleaned = _TOOL_XML_TAG_RE.sub("", cleaned)

    # ①.8 「半个标签」——整行只有一截 <function / <tool_call，或正文就以它开头。
    #      它不是调用（没有 JSON），纯粹是模型写到一半放弃留下的碎片，
    #      留着就会在正文开头冒出一截乱码一样的文字。
    if has_stray:
        cleaned = _TOOL_XML_STRAY_RE.sub("", cleaned)
        cleaned = _TOOL_XML_HEAD_RE.sub("", cleaned)
        cleaned = cleaned.lstrip("\n")

    # ② 聊天轮：**裸着**的调用（模型什么壳子都不套，直接把 {"name": …, "arguments": …}
    #    写在正文里）。实测（2026-09-21 实机复现）：这种写法 Ollama 解析不出来、
    #    我们也认不出 → 工具**根本没执行**，用户看到的就是那一坨 JSON，
    #    后面还常常跟着模型**自己编的** `<result>…</result>`。
    #    ⚠️ 判据必须非常挑（见 _strict_bare_calls），否则会把用户要的 JSON 示例吃掉。
    if bare == "strict":
        _got, cleaned = _strict_bare_calls(cleaned, allowed)
        calls.extend(_got)
        if calls:
            # 顺带把模型编出来的"工具返回"一起清掉 —— 它是幻觉，
            # 真结果会由我们执行完喂回去（只在我们确实摘到调用时才敢清）。
            cleaned = _TOOL_ECHO_BLOCK_RE.sub("", cleaned)

    if calls:
        return calls, _strip_call_head_debris(cleaned).strip()

    # ③ 代码轮：宽松的裸 JSON 扫描（那轮模型本来就是把调用当 JSON 吐在正文里）
    #    ⚠️ 这条路在聊天轮**不能开**：正文里贴接口返回、写 .json 文件都会长这样，
    #       误删就是把用户要的内容吃了。
    if bare is True and ('"arguments"' in cleaned or '"parameters"' in cleaned):
        for start in (i for i, c in enumerate(cleaned) if c == "{"):
            depth = 0
            for i in range(start, len(cleaned)):
                if cleaned[i] == "{":
                    depth += 1
                elif cleaned[i] == "}":
                    depth -= 1
                    if depth == 0:
                        seg = cleaned[start:i + 1]
                        call = None
                        try:
                            call = _as_tool_call_obj(json.loads(seg), allowed)
                        except Exception:
                            call = None
                        if call:
                            calls.append(call)
                            cleaned = cleaned.replace(seg, "")
                        break
    return calls, _strip_call_head_debris(cleaned).strip()


def _safe_emit_len(text: str, marks=None) -> int:
    """正文里从哪个位置开始**可能**是"工具调用壳子"——之前的部分可以安全推给用户。

    这是"既要逐字流式、又不能把 ```tool / <function-call> 那坨 JSON 闪到界面上"的解法：
    用**前缀匹配**把尾巴先扣住。只要结尾这几个字符可能是某个标记的开头，
    就先不发，等后续内容来了再判断。

    marks 决定"多敢扣"：代码轮用 _TEXT_TOOL_MARKS（激进），
    聊天轮用 _LEAK_MARKS（保守，只扣 XML 包装和 ```tool）—— 见它们的注释。
    """
    t = text or ""
    if not t:
        return 0
    ms = marks or _TEXT_TOOL_MARKS
    low = t.lower()          # 标记统一小写，而模型可能吐 <Function-Call> —— 不区分大小写才稳
    # ① 已经能看出是壳子的起点 → 从那里开始全扣住
    best = len(t)
    for mark in ms:
        i = low.find(mark)
        if 0 <= i < best:
            best = i
    # ② 结尾可能正打到一半（"`"、"``"、"<"、"<f" …）→ 也扣住
    #    用**前缀匹配**而不是穷举，这样 ```tool_call / <function_call 都能覆盖
    for k in range(1, min(len(t), max(len(m) for m in ms))):
        suffix = low[-k:]
        if any(m.startswith(suffix) for m in ms):
            best = min(best, len(t) - k)
    return max(0, best)


_WS_WRITE_NAME_RE = re.compile(r'"name"\s*:\s*"workspace_write"')


def _partial_ws_write(buf: str):
    """从**还没生成完**的正文里，尽量抠出 workspace_write 的 (rel, text)。

    为什么要这么干：代码模型是把工具调用当**文本**吐出来的，
    也就是说 `{"rel": "app.py", "text": "…代码…"}` 里的代码是**逐字流出来的**。
    把这半截内容先落盘，编辑器那边的文件监视器就能看到代码一点点长出来 ——
    用户要的"AI 自动逐字输入到编辑器"就是这么实现的（不需要他复制任何东西）。

    `text` 可能只有一半（甚至停在转义符中间），所以这里全部走容错解析：
    能解析多少给多少，解析不了就返回 None（这一帧不写盘，等下一帧）。

    返回 (rel, text, project)：
      · rel 可能为 None（文件名还没打完）
      · text 为 None 表示还没有正文
      · project 是"这一轮里 workspace_new_project 要建的新项目名"（没有则 None）
        —— **必须带上它**：模型常常在同一轮里先建项目再写文件，
        而工具要等模型把整轮说完才执行，此时 `active_project()` 还是**旧项目**，
        预览就会写进上一个项目里，留下不认识这个文件的孤儿目录（实测踩到）。
    """
    if not buf or "workspace_write" not in buf:
        return None, None, None
    m = _WS_WRITE_NAME_RE.search(buf)
    if not m:
        return None, None, None
    seg = buf[m.end():]
    # 同一轮里如果还要新建项目，预览就该直接写进那个新项目
    proj = None
    mn = re.search(r'"name"\s*:\s*"workspace_new_project"', buf[:m.start()])
    if mn:
        mp = re.search(r'"name"\s*:\s*"([^"\\]{1,64})"', buf[mn.end():])
        if mp:
            proj = mp.group(1)
    rel = None
    mr = re.search(r'"rel"\s*:\s*"((?:[^"\\]|\\.)*)"', seg)
    if mr:
        try:
            rel = json.loads('"%s"' % mr.group(1))
        except Exception:
            rel = None
    mt = re.search(r'"text"\s*:\s*"', seg)
    if not mt:
        return rel, None, proj    # 正文还没开始
    rest = seg[mt.end():]
    out = []
    i = 0
    while i < len(rest):
        c = rest[i]
        if c == "\\":
            if i + 1 >= len(rest):
                break             # 转义序列还没打完 → 这一帧先不放出来
            out.append(rest[i:i + 2])
            i += 2
            continue
        if c == '"':
            break                 # 正文结束
        out.append(c)
        i += 1
    try:
        return rel, json.loads('"%s"' % "".join(out)), proj
    except Exception:
        return rel, None, proj


def _show_writing_file(rel: str, proj: str = "") -> None:
    """AI 开始写某个文件时：让编辑器**打开这个文件**，必要时把窗口拉到前台。

    这是"AI 边写、用户边看"的关键一步，缺了它用户会以为"AI 说写了，编辑器里啥也没有"：
    编辑器只对**已经打开着的文件**做外部改动重载。
    优先用内置 VS Code（它嵌在应用界面里，用户不用切窗口）；
    退回本机 PyCharm 时才需要额外把窗口置前。
    """
    try:
        r = workspace.open_file_in_editor(rel, proj)
        if r.get("editor") != "vscode":
            workspace.focus_ide_window(proj)
    except Exception:
        logger.warning("让编辑器打开 %s 失败（不影响写入）", rel, exc_info=True)


def _reconcile_streamed(state: dict, ui_events: list, final: bool = False) -> list:
    """给"流式预览"和"正式写入"对账，返回要讲给用户听的话。

    背景：AI 写文件是**边生成边落盘**的（这样编辑器里能看到代码一个个字长出来），
    但那个落盘只是**预览**，真正算数的写入是模型调 `workspace_write` 那一次。
    两者对不上时就会出问题，这里负责收拾：

      ① **孤儿预览**：预览写进了项目 A（那一刻还没切项目），
         正式写入却落在项目 B —— 于是 A 里多出一份只属于 B 的文件。
         条件（**两个都必须满足**才删，缺一不可）：
           · 这份文件是**我们这次新建的**（`before is None`），不是本来就有的；
           · 正式写入确实落在了**另一个**项目。
         这样既不会碰用户原有的文件，也不会碰正式写入自己写的那份。

      ② **只有预览、没有正式写入**（模型输出被截断，或它压根没走写文件工具）：
         磁盘上会剩下**半截文件**，而界面上看起来"AI 正在写"，用户以为成功了。
         这是最坑的一种，必须在整次生成结束时撤掉（新建的删掉、老文件还原）。

    `final=True` 表示整次生成已经结束 —— 只有这时才做②，
    因为中途每一轮都可能"这一轮没有写入"而下一轮才写（跨轮对账，
    所以 `state` 必须整次生成共用一份，不能按轮清空）。

    ⚠️ 之前这里踩过两次坑，都写进注释免得再犯：
      · 清理太激进（按"每个文件有没有被正式写"逐个判）→ **误删**，
        把上一轮刚写好的项目目录清空了；
      · 记录按轮清空 → 跨轮的孤儿文件**永远清不掉**。
    """
    streamed = state.setdefault("streamed", {})     # rel -> (before 内容或 None, 预览写进的项目)
    written = state.setdefault("written", {})       # rel -> 正式写入落在的项目
    notices = []
    try:
        for u in ui_events:
            if (isinstance(u, dict) and u.get("act") == "write" and u.get("rel")):
                written[u["rel"]] = str(u.get("project") or "")
        # ① 孤儿预览
        for rel, (before, used) in list(streamed.items()):
            rp = written.get(rel)
            if before is None and rp and used and rp != used:
                try:
                    p = workspace.abs_path(rel, used)
                    if os.path.exists(p):
                        os.remove(p)
                        logger.info("清掉孤儿预览文件：%s/%s（正式写入在 %s）", used, rel, rp)
                except Exception:
                    pass
        # ② 只有预览、没有正式写入 —— 只在整次生成结束时收拾
        if final:
            left = [r for r in streamed if r not in written]
            for rel in left:
                before, used = streamed[rel]
                try:
                    p = workspace.abs_path(rel, used)
                    if before is None:
                        if os.path.exists(p):
                            os.remove(p)
                    else:
                        with open(p, "w", encoding="utf-8", newline="") as fh:
                            fh.write(before)
                except Exception:
                    pass
            if left:
                notices.append(
                    "模型这次没能把文件写完（输出被截断，或没走写文件工具），"
                    "已撤掉不完整的文件 %s —— 让它重试一次即可。" % "、".join(left))
            streamed.clear()
    except Exception:
        logger.warning("对流式预览与正式写入对账失败", exc_info=True)
    return notices


def _route_code_model(cfg: dict, text: str, fallback: str, prev_code: bool = False,
                      force: bool = False, has_images: bool = False):
    """**聊天这一轮永远用默认模型**（保留函数只为兼容调用点）。

    ⚠️⚠️ 这里以前是"代码类请求就切到 qwen2.5-coder"，**2026-09-16 改掉了**，
    原因是实测复现的硬伤：

      · qwen2.5-coder **不支持 Ollama 的原生工具调用通道**，只能走"文本协议"
        （要求它自己吐 ```tool 块）；
      · 一旦历史里没出现过工具块，它就照着历史"只回答、不调工具"，
        连着几轮之后会**理直气壮地说「我无法读取本地文件系统」**——
        实测：用户问"读一下 hello.py"→"看有没有错误"→"你看不到吗？？"，
        越答越离谱。真正的问题不是它读不到，是**它以为自己没有这个能力**。
      · 而默认模型（qwen3-vl）**原生支持工具调用**，读文件/跑代码/做决策都稳。

    所以现在的分工（用户提的架构，已验证）：
      **大脑 = 默认模型**（决策、读文件、跑代码、串联流程）
      **打字员 = 代码模型**（只在需要"写出代码"时，由 `write_code` 工具单独调用它）

    ⚠️ **2026-09-16 这里一度被改成"永远返回默认模型 + write_code 工具代办"**
    （大脑用 qwen3-vl 决策、代码模型只负责写）。原因是这台机器 12GB 显存
    **装不下两个模型**，每调一次代码模型就得把大脑重新加载，实测一个任务绕了 10 分钟。
    用户试过之后要求**换回本架构（按轮切换）**，所以恢复了。
    **两种架构的取舍，留着以后别再走回头路：**
      · 本架构（按轮切）：简单、单轮快；代价是切到代码模型后只能走"文本协议"，
        多轮里它可能不吐工具块 —— 下面那段「你有读文件的能力」的硬规则就是为它加的。
      · write_code 架构：决策与写码分离、更稳；代价是模型来回切、总时长可能长几倍。
    要重新启用 write_code：把 tools.make_schemas 里的 _WS_CODE_SCHEMA 加回去即可
    （_do_write_code 一直保留着，没删）。
    """
    if not cfg.get("code_auto_route", True):
        return fallback, ""
    want = str(cfg.get("code_model") or "").strip()
    if not want or want == fallback:
        return fallback, ""
    # ⚠️⚠️ 本轮带了图片 → **一律留在视觉模型上**（连开发台的 force 也不例外）。
    #   代码模型 `qwen3-coder:30b` 的能力段里**没有 vision**（`ollama show` 实测：
    #   只有 completion / tools），切过去图片必然丢，用户贴图就白贴了。
    #   留在默认模型还能两全：qwen3-vl 既看得见图，也照样能写代码。
    #   （开发台那轮给一句提示，因为用户明确期望"进开发台=用代码模型"，
    #    不然他会以为是切换坏了。）
    if has_images:
        return fallback, ("本轮带了图片，已留在视觉模型上 —— 代码模型看不了图"
                          if force else "")
    if not force and not _needs_task_model(text, prev_code=prev_code,
                                           has_images=has_images):
        return fallback, ""
    if want not in _installed_models():
        # 还没下载 → 静默用回默认模型。配置名留着，用户下载后自动生效，不用改设置。
        return fallback, ""
    return want, "已切到专用模型 %s（写代码：它不思考，不会把输出额度烧在思考上）" % want


# =====================================================================
#  工具执行前的"问用户"通道
# =====================================================================
# 用户明确要求：模型碰到危险操作时**别直接拒绝，先问一句**，批准了就执行。
# 难点在于：工具是跑在线程池里的（阻塞），而弹窗必须**立刻**出现在网页上。
# 做法：把事件塞进一个异步队列（流式生成器边跑边吐出去），
# 然后**阻塞那个工作线程**，等 /api/tool/confirm 把用户的选择写回来。
def _dict_ui_channel(pair) -> dict:
    confirm, ask = pair
    return {"confirm": confirm, "ask": ask}

_ui_pending: dict = {}
_ui_lock = threading.Lock()

# 「本次任务不再询问」= 本轮对话内所有危险操作自动批准（2026-11-08 用户要求）
# ⚠️ 作用域刻意做成**一轮对话**：用户勾选时想的是"这一个任务别烦我"，
#    不是"以后永远别问"。所以每轮 chat 开始时都会重置。
_AUTO_OK = {"on": False, "hits": 0}


# 用户明确要"一个文件"的说法。
# 命中它却没看到 library 工具被调用 → 大概率是模型"虚假完成"，需要兜底。
_WANT_FILE_HINTS = (
    "保存", "存起来", "存到", "存进", "存成", "写成文档", "写成文件", "导出",
    "生成文件", "放进文库", "另存", "文件形式", "给我一个文件", "存一下", "存档",
)


def _wants_file(text: str) -> bool:
    t = text or ""
    return any(k in t for k in _WANT_FILE_HINTS)


# "能产出办公文件"的工具 —— 用来判断"用户要的成品到底做出来没有"。
# ⚠️ edit_office 也算：给已有 PPT/Word/Excel 加内容同样是产出文件
#    （实测「再加一页」这类迭代轮只调 edit_office 就够了）。
_OFFICE_MAKER_TOOLS = {"make_pptx", "make_docx", "make_xlsx", "edit_office"}


# ---------- 把代码存成"能直接跑的源码文件" ----------
# 代码块的语言标记 → 文件后缀
_LANG_EXT = {
    "python": ".py", "py": ".py", "python3": ".py",
    "javascript": ".js", "js": ".js", "node": ".js",
    "typescript": ".ts", "ts": ".ts", "tsx": ".tsx", "jsx": ".jsx",
    "java": ".java", "c": ".c", "cpp": ".cpp", "c++": ".cpp", "cxx": ".cpp",
    "csharp": ".cs", "cs": ".cs", "go": ".go", "golang": ".go",
    "rust": ".rs", "rs": ".rs", "kotlin": ".kt", "swift": ".swift",
    "bash": ".sh", "sh": ".sh", "shell": ".sh", "zsh": ".sh",
    "powershell": ".ps1", "ps1": ".ps1", "bat": ".bat", "cmd": ".bat",
    "sql": ".sql", "html": ".html", "css": ".css", "scss": ".scss",
    "json": ".json", "yaml": ".yml", "yml": ".yml", "xml": ".xml",
    "markdown": ".md", "md": ".md", "text": ".txt", "txt": ".txt",
}
# ```lang 代码块（允许语言标记为空）
_FENCE_RE = re.compile(r"```([A-Za-z0-9_+#.\-]*)[ \t]*\r?\n(.*?)```", re.S)
# 用户点名要的文件名，如"文件名就叫 stats.py"
_NAMED_FILE_RE = re.compile(
    r"[\w\u4e00-\u9fa5\-]{1,40}\.(?:py|js|ts|tsx|jsx|java|go|rs|c|cpp|cs|kt|swift"
    r"|sh|ps1|bat|sql|html|css|scss|json|ya?ml|xml|md|txt)\b", re.I)


def _slug_name(s: str, fallback: str = "code") -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "", s or "").strip()
    return s[:48] or fallback


def _guess_code_filename(code: str, language: str = "", user_text: str = "") -> str:
    """给一段代码取文件名。

    优先级：① 用户点名要的名字（"文件名就叫 stats.py"）
            ② 代码里第一个 def/class/function 名 + 语言后缀
            ③ 时间戳 + 语言后缀
    """
    m = _NAMED_FILE_RE.search(user_text or "")
    if m:
        return _slug_name(m.group(0))
    ext = _LANG_EXT.get((language or "").strip().lower(), ".txt")
    # ⚠️ 名字长度从 0 个额外字符起算 —— 原来写 `\w{1,32}`（要求至少 2 个字符），
    # 于是 `function f(x)` 这种单字母函数名匹配不上，白白掉到时间戳兜底。
    m = re.search(r"^\s*(?:def|class|func|function)\s+([A-Za-z_]\w{0,32})",
                  code or "", re.M)
    if m:
        return "%s%s" % (m.group(1), ext)
    return "%s%s" % (time.strftime("%m%d-%H%M"), ext)


def _save_code_to_doclib(code: str, language: str = "", filename: str = "",
                         user_text: str = "") -> str:
    """把一段代码存成生成文库里的**源码文件**，返回相对路径（失败返回空串）。

    ⚠️ 为什么得单独做这件事：代码模型这一轮**没有工具**（它不支持原生工具调用，
    给了 schema 只会把调用当 JSON 文本吐出来）。所以"存文件"不能指望模型自己
    去调 library —— 由前端代码卡片的「💾 存到文库」按钮，或这里的兜底逻辑来做。
    """
    code = (code or "").strip("\n")
    if not code.strip():
        return ""
    name = _slug_name(filename) if filename else _guess_code_filename(code, language, user_text)
    if "." not in os.path.basename(name):
        name += _LANG_EXT.get((language or "").strip().lower(), ".txt")
    try:
        r = doclib.write_file(name, code)
        if not r.get("ok"):
            logging.getLogger("uvicorn.error").warning(
                "存代码到文库失败：%s", r.get("error"))
            return ""
        return r.get("rel") or ""
    except Exception:
        logging.getLogger("uvicorn.error").warning("存代码到文库异常", exc_info=True)
        return ""


def _looks_like_tool_block(lang: str, body: str) -> bool:
    """这个"代码块"其实是一次**内部工具调用**吗（不是给用户看的代码）？

    真实踩坑：代码模型把工具调用写成一个 ```tool 块塞在正文里，而它写的 JSON
    **漏了最外层的 `}`**，于是清理逻辑没能识别、块留在了正文里。后果有两层：
      · 前端把每个围栏块都渲染成一张「可运行」的代码卡片（用户看到好几张一样的）；
      · `_autosave_answer` 还可能把它当成"最大的那个代码块"存成 .py 源码文件。
    ⇒ 凡是"工具围栏"或"长得就是工具调用 JSON"的块，一律当内部调用处理。
    """
    if str(lang or "").strip().lower() in ("tool", "tool_call", "tool-call"):
        return True
    obj = _loads_lenient(body)
    return obj is not None and _as_tool_call_obj(obj) is not None


def _fake_doclib_links(text: str) -> list:
    """正文里引用的「生成文库文件」链接，哪些其实**并不存在**。

    ⚠️ 2026-09-25 加（用户报「模型生成的 PPT 打不开」）：
    模型会**凭空捏造产物和下载链接**。实测原话：
        用户：「生成一份关于我的入团申请书的PPT」
        模型：「✅ 已为您完成！…共 12 页」+
              [立即下载完整版](/api/doclib/download?rel=陈宇桐同志入团申请报告.pptx)
    可它**一个工具都没调**，文库目录里什么都没有。用户点开当然打不开；
    回来问它"为什么打不开"，它又编出"组织部门审核保密""浏览器不兼容"
    "图书馆系统维护"等一串假原因 —— 一路幻觉到底。
    这里把"声称存在但实际没有"的链接挑出来，如实告诉用户。
    """
    out = []
    for m in re.finditer(r"/api/doclib/download\?rel=([^)\s\"'>]+)", text or ""):
        raw = m.group(1)
        try:
            rel = urllib.parse.unquote(raw)
        except Exception:
            rel = raw
        rel = (rel or "").strip()
        if not rel:
            continue
        try:
            exists = os.path.isfile(doclib.file_path(rel))
        except Exception:
            exists = False
        if not exists and rel not in out:
            out.append(rel)
    return out


def _autosave_answer(text: str, user_text: str = "") -> str:
    """把模型写好的正文自动存进生成文库，返回相对路径（失败返回空串）。

    分两种情况：
    - 正文里有**像样的代码块** → 存成**可直接运行的源码文件**（.py/.js/…），
      文件名优先用用户点名的那个。原来是统一存 .md，结果"写个 stats.py"最后
      得到一个名字里带标题、后缀还是 .md 的文件 —— 又丑又跑不了。
    - 其他（作文 / 报告 / 方案）→ 仍然存成 .md，用首行当标题。
    """
    try:
        # 存进文库的必须是"给用户看的内容"：先把内部工具调用（```tool 那坨 JSON）摘干净，
        # 否则 .md/.py 里会躺着一串 `{"name": "workspace_write", …}`（实测踩过）。
        text = _split_text_tool_calls(text or "")[1]
        # ⚠️ 再滤掉"其实是工具调用"的块：代码模型把调用写成 ```tool 块，
        #    万一漏进正文，这里会把它当成"最大的那个代码块"存成 .py ——
        #    生成文库里的源码就成了那坨 JSON。
        blocks = [b for b in _FENCE_RE.findall(text)
                  if not _looks_like_tool_block(b[0], b[1])]
        plain = _FENCE_RE.sub("", text).strip()
        # ⚠️ 不要要求"有且只有一个代码块" —— 实测模型的回答常是
        # 「一段说明 + 主代码块 + 一小段用法/输出示例」，那样就变成 2 个块，
        # 会被判成"不是代码回答"而存成 .md（踩过）。取**最大的那块**即可。
        #
        # 阈值也**不能定高**：一个"扫描目录按大小排序"的脚本只有 4 行、约 110 字，
        # 按"≥5 行或 ≥200 字"就会被误判成长文（踩过）。这里放宽到 3 行 / 100 字。
        if blocks and len(plain) < 800:
            lang, code = max(blocks, key=lambda b: len(b[1]))
            if len(code.strip().splitlines()) >= 3 or len(code) >= 100:
                rel = _save_code_to_doclib(code, lang, "", user_text)
                if rel:
                    return rel
        first = ""
        for ln in (text or "").splitlines():
            seg = ln.strip().lstrip("#＃* ").strip()
            if seg:
                first = seg
                break
        name = (re.sub(r'[\\/:*?"<>|]', "", first)[:24].strip()
                .rstrip("。，、！？.!? ")) or "未命名"
        rel = "%s-%s.md" % (time.strftime("%Y%m%d-%H%M"), name)
        r = doclib.write_file(rel, text)
        return r.get("rel") if r.get("ok") else ""
    except Exception:
        return ""


def _make_ui_channel(loop, live_ui, timeout: float = 300.0):
    """生成"问用户"的两个通道：confirm（危险操作确认）与 ask（追问细节）。

    ⚠️ 为什么非得这么绕：工具跑在线程池里（阻塞），而弹窗必须**立刻**出现在网页上。
    做法是把事件 `call_soon_threadsafe` 塞进一个异步队列（流式生成器边跑边吐），
    然后**阻塞那个工作线程**，等 HTTP 端点把用户的选择/回答写回来。
    """

    def _request(kind: str, payload: dict):
        cid = ("cf-" if kind == "confirm" else "ak-") + os.urandom(5).hex()
        item = {"event": threading.Event(), "allow": False, "answers": None}
        with _ui_lock:
            _ui_pending[cid] = item
        try:
            msg = {"type": kind, "id": cid}
            msg.update(dict(payload or {}))
            loop.call_soon_threadsafe(live_ui.put_nowait, msg)
            if not item["event"].wait(timeout):
                return None            # 用户一直没回应
            return item
        finally:
            with _ui_lock:
                _ui_pending.pop(cid, None)

    def confirm(payload: dict) -> bool:
        if _AUTO_OK["on"]:
            # 勾了"本次任务不再询问" → 直接批准，不弹框、不阻塞
            _AUTO_OK["hits"] += 1
            return True
        item = _request("confirm", payload)
        return bool(item and item.get("allow"))

    def ask(payload: dict) -> list:
        item = _request("ask", payload)
        return list(((item or {}).get("answers")) or [])

    return confirm, ask


def _amap_status_line() -> str:
    """把高德 key 的当前状态写成一行提示，让模型能主动跟用户说。

    为什么要进系统提示：key 失效是"不声不响"的 —— 用户只会觉得地图莫名其妙变难用。
    模型知道了就能顺口提醒一句"你的高德 key 现在用不了（原因），点顶栏重新配一下"。
    结论来自 amap.check_health()（带缓存），所以这里不会每次都去调网络。
    """
    try:
        from . import amap as _am
        from . import map_tools as _mt
        h = _am.check_health(online=_mt.online())
    except Exception:
        return ""
    if not h.get("configured"):
        return (""
                "  · 本机**还没配高德 key**：用户一用到地图就先调 connect_amap 请他填。"
                "别直接说做不到，也别默默用着弱数据不吭声。\n")
    if not h.get("enabled", True):
        # 用户**主动断开**的：这是他自己的选择，别去劝、也别报警。
        return ""
    if h.get("ok"):
        return ""
    return (""
            "  · ⚠️ **高德 key 现在不可用**（%s）。这会让地图悄悄退回弱数据源，"
            "用户多半还不知道。**在回答里主动提醒他一句**（把上面这个原因说清楚），"
            "并让他点界面顶栏的「高德 key」按钮重新配置 —— 别把这事憋着不说。\n"
            % (h.get("message") or "原因未知"))


class _SystemPrompt:
    """WorkBuddy 风格系统提示：精简注入分层记忆，给出工具使用引导。"""

    @staticmethod
    def build(last_user_text: str, session: str = "", docs: list = None,
              no_tools: bool = False):
        cfg = config.load_config()
        parts = [
            # ⚠️⚠️ **系统提示的第一行必须是「稳定不变」的内容。**
            # 这里原来放的是「当前时间：…HH:MM:SS」（带秒）—— 结果每发一条消息，
            # 提示词的**第一个 token 就变了**，Ollama 的前缀缓存（KV 复用）**永远命不中**，
            # 于是每一轮都要把整份提示词重新预填充一遍。
            # 实测（2026-09-22，Qwen3-VL:8B / RTX 5070 Ti）：
            #   提示词 19863 token（其中工具定义 38 个 ≈ 13029 token），
            #   Ollama 侧 `prompt eval time = 10607 ms / 19837 tokens` ——
            #   **10.6 秒全在重算前缀**，用户看到的就是"发出去半天没反应"。
            # 现在把时间挪到**最后一条用户消息的末尾**（见 _attach_live_ctx），
            # 它排在「系统提示 + 工具定义」之后，大段稳定前缀就能被缓存复用。
            "你是 hardwordy，像一位能干的项目助理。你的所有处理都在用户本机完成，注意保护隐私。",
            # ★ 2026-11-08 用户要求：「所有调动都必须有 LLM 打头阵，由 LLM 决策调度、
            #   有规划地完成任务」。这段是**稳定的固定文本**（不随时间变），
            #   放在最前面不会破坏 Ollama 的前缀缓存。
            "\n【工作方式：先理解 → 再规划 → 后执行 → 最后如实汇报】\n"
            "1) **先理解**：先弄清用户这句话真正要的是什么（成品？信息？改一版？）。"
            "关键前提缺失或有歧义 → **先调用 ask_user 问清**，不要自己猜着做。\n"
            "2) **再规划**：动手前先在脑子里列出步骤（1…2…3…），想清楚每步用哪个工具。"
            "需要多个产物就分步做，不要一口气乱调。\n"
            "3) **后执行**：每一步都**真的调用对应工具**，拿到真实结果再往下走。"
            "工具报错就换参数/换工具重试一次；**绝不**把没发生的事写成已经发生。\n"
            "4) **最后汇报**：做了什么、产物在哪（文件路径 / 图片已在界面上）、"
            "哪些没做成以及**真实原因**。\n"
            "⚠️ 你的每一个动作都必须是「想清楚 → 调工具 → 看结果」这个循环，"
            "禁止跳过工具直接编结果；也禁止只描述「我打算怎么做」就收工。\n"
            # ★ 2026-11-08 用户要求：「解决问题之后的总结要美观、结构化」。
            #   模板写死在这里（稳定文本，不破坏缓存）；前端已支持 Markdown 渲染。
            "\n【收尾必须结构化 —— 用户明确要求】\n"
                        """
            【先判形态，再动笔 —— 这里最容易出错】
            动笔前先给这一轮归个类，只有 C 类才允许用后面的收尾模板：
            · A 类｜寒暄、闲聊、道谢、确认收到、表达情绪、只发一句「你好 / 啊？ / 可以啊」→ 像正常人一样接话，1~2 句、不超过 30 字；不许出现任何标题、清单、「做完了什么 / 关键点 / 产物 / 下一步」这类段落，也不许为了凑内容去调工具。
            · B 类｜回答问题、解释概念、查信息、给建议、把某件事说清 → 直接答，第一句就是答案；可以用要点，但不套收尾模板。
            · C 类｜真的调了工具并产出了文件 / 图片 / 文档 / 代码 / 代码改动 → 最后一段才按下面的模板写。
            ⚠️ 判成 C 类是最常见的误判：用户只打了个招呼，你却回一份带「产物 / 下一步」的报告，比不写总结更糟。拿不准时一律按 A / B 类处理，宁短不长。
            ⚠️ 用户没在对话里亲口说过的身份信息（姓名、城市、年龄、单位、职业），一律不许写进回复，更不许从记忆或猜测里搬来填模板 —— 那是编造。
            """,
            "活干完后，**最后一段回复**按这个模板写（用 Markdown 标题和列表，不要大段散文）：\n"
            "⚠️ 上面说的「活干完」严格指：**这一轮真的调用了工具，并且产出了文件 / 图片 / 文档 / 代码 / 代码改动**。"
            "**寒暄（你好 / 谢谢 / 在吗）、闲聊、单纯回答问题、解释概念、确认收到、用户只是表达情绪或疑问 —— 一律不用这个模板**，"
            "正常对话 1~3 句说清就完事。绝不为了凑格式去编造用户身份、记忆、文件名或改动。\n"
            "⚠️ 模板里每一格都必须有**本轮真实依据**；某格填不出来就**整节删掉**，绝不硬编（尤其不许凭空写用户的姓名、城市、记忆内容）。\n"
            "\n【先正面回答，别答非所问】\n"
            "开口前先在心里问一句：「用户这句话到底在问什么、要什么？」**第一句必须直接回应那个问题**，之后再补充别的。\n"
            "· 用户问「刚刚改了什么」→ 就说改了什么；没改就直说没改，别扯别的；\n"
            "· 用户只发一句「你好 / 啊？ / 可以啊」→ 正常接话即可，不要输出报告、清单、总结；\n"
            "· 用户指出你上一轮说错了 → 先承认、说清错在哪，再谈怎么改。\n"
            "⚠️ 不要自动附加「关键点 / 产物 / 下一步」这类自我介绍式段落，用户没要就别加。\n"
            "\n【多轮对话：先对齐「那个」到底指什么，再开口】\n"
            "用户说「刚才那个 / 上一个 / 第几个 / 我是说 X 那个」时，**不要拿最近一条顶替**。\n"
            "· 先看本轮用户消息里附的【最近 N 轮速览】，按里面的关键词逐条比对，"
            "确认是哪一轮，再回到历史里取那一轮的原文来回答；\n"
            "· 对不上就直接问「你是指第几轮那个？」—— 猜错比问一句代价大得多；\n"
            "· 用户纠正你「我是说 X 那个」时，**先承认答错了**，再按 X 重新回答，不要辩解。\n"
                        """（注：上面这条「不要自动附加」只约束 A / B 类；本轮属于 C 类、真的产出了东西时，按下面模板写是允许且要求的。）
            """,
            "### ✅ 做完了什么\n"
            "（一两句话，说结果，不说过程）\n"
            "### 📋 关键点\n"
            "- 每条一行，最多 5 条；能带数字/路径就带上\n"
            "### 📁 产物 / 改动\n"
            "- 文件路径或图片在哪（没有就写「无」）\n"
            "### 👉 你需要做什么\n"
            "- 需要用户操作的步骤；没有就写「不需要，可以直接用」\n"
            "⚠️ 正文里**不要贴大段代码/日志**（超过 10 行就该调工具落盘或让界面折叠显示）；\n"
            "⚠️ 不要重复工具已经展示过的内容；⚠️ 不确定的事不要写进总结。\n",
            # 告诉模型时间去哪儿找（这句本身是稳定的，不会破坏缓存）
            "需要知道「现在几点/今天几号」时，看**用户消息末尾附的「当前时间」**，"
            "或调用 get_time 工具；不要凭训练数据猜日期。",
            "你可以调用以下工具来完成具体任务，而不仅是空谈：\n"
            "- 视觉识别（直接看，不要调用工具）：当图片/视频已经附在当前对话中（用户拖入/上传），直接用你自身的多模态视觉能力识别、描述或分析其内容即可，绝对不要为「看图」调用任何工具。只有以下四种情况才需要调用工具：\n"
            "  ① 用户想要**真实存在**的图片，如「找张 xx 的图」「搜一下 xx 图片」「xx 长什么样」「来点 xx 壁纸」→ 调用 web_image_search（到网上搜索现成的真实图片）；\n"
            "  ② 用户要求「画图/生成图片/AI 绘画/画一只 xx」这类**从零创作**→ 调用 generate_image（prompt 用英文描述），绝不能只口头描述，只有调用工具才算完成；\n"
            "  ③ 用户要求对某张图做局部修改（如「把这张图的背景改成夜晚」「给猫戴帽子」）→ 调用 edit_image；用户拖入本轮的图片优先，或填 source 为本地图片路径；\n"
            "  ④ 用户给的是一个本地文件路径、要你读取该文件 → 调用 read_file。\n"
            "- ★ 务必分清「搜图」与「生成图」：用户说「找/搜/看看」→ web_image_search（搜真实图片，不绘制）；"
            "用户说「画/生成/制作」→ generate_image（AI 创作）。两者结果来源完全不同，绝不能混淆。\n"
            "- ⚠️ **工具没给结果时不要编**：工具返回「没找到／失败／0 条」时，就**如实**告诉用户没拿到，"
            "并说明可能原因（关键词不合适、站点防盗链等）以及可以怎么再试。"
            "**绝不能**换个说法把没发生的事说得像发生了，也不要凭印象去描述图片内容或补充未经核实的事实。\n"
            "- ⚠️ **不要把无关的记忆／知识库内容硬扯进回答**：只有确实相关时才引用；"
            "没有相关资料就直说没有，不要为了显得「有依据」而牵强关联。\n"
            "- 图片库：用户说「保存这张/存起来/收进图库」时调用 save_image_to_library（index 填本轮第几张）。\n"
            "- 文件系统：浏览用户目录、读取任意本地文件、按关键词搜索、写入或修改文件。\n"
            "- 记忆：记忆按【分区文段】整体维护（工作背景/个人背景/当前关注/近期动态…）。遇到值得长期记住的**关于用户**的信息时（身份/偏好/约定/目标/他身边的人事物/他未来可能的意图），主动调用 remember 把对应分区的**整段文段**重写成合并新旧信息后的最新版（**自主判断，只记关于他的，不要把所有问答都写入**）。⚠️ **别把一次性查询的结果记进去**（查到的评分/天气/路线/价格…），也别记本应用自己的用法说明 —— 要记的是「他是谁、他在意什么、他打算做什么」；"
            "当用户问「你还记得吗/我们之前说过」或需要历史信息时调用 search_memory。\n"
            "- 时间：需要当前日期时间时调用 get_time。\n"
            "- 地图：**跟地点、路线、周边相关的一律用地图工具，不要用 web_search 去搜**。\n"
            "  · 「怎么走 / 规划路线 / 从A到B多远多久」→ map_plan"
            "（mode 可选 driving / foot / bike / **transit 公交**，它会画地图卡片）。\n"
            "  · 「附近有什么吃的 / 这周围有什么 / 附近哪家评分高」→ **nearby_places**。"
            "配了高德 key 时它**直接返回真实评分**，所以问「评分最高的餐厅」也用它 —— "
            "把 radius 放大到 3000 拿更多候选，再按 rating 排序就行，"
            "**绝对不要为了找评分跑去 web_search**（那样搜不到结构化评分，还慢）。\n"
            "  · 返回里都是真实数据，照实念，不要自己算、不要编。\n"
            "  · **用户说了在哪个城市/地区，就把 map_plan 的 city 填上**"
            "（「广州市内有什么商场」→ city:\"广州\"）。不填会把同名地点解析到外省 ——"
            "实测标「天河城」会跑到「江西省南昌市进贤县天河城」。\n"
            "  · 想找「某个城市里有什么 XX」这种大范围的，**一定要用 nearby_places**"
            "（place 填城市名，radius 给 20000~40000），"
            "**不要凭印象挑几个地标丢给 map_plan** —— 那是编的，实测会把「商场」"
            "标成地铁站、甚至标到外省去；\n"
            "  · 在地图上标出几个**用户点名要到**的具体地点，才用 map_plan 的 places。\n"
            "  · ⚠️ **本机没配高德 key 时**（地图会退回 OpenStreetMap：中国的店铺几乎查不到，"
            "也没有评分/路况/公交）：用户一用到地图，就**先调 connect_amap 请他填一个** ——"
            "别直接说做不到，也别默默用着弱数据不吭声。用户说「不用」就按没有 key 继续，"
            "并在回答里如实说明用的是什么数据、缺了什么，别再反复追问。\n"
            "  · ⚠️ **地图不做任何本地缓存**：联网查的是实时数据，断网就查不到 ——"
            "离线时只能用内置常用地名表定位、路线只给直线距离，**地图卡片不显示底图**。"
            "所以别承诺离线也能看，也别提已缓存/已下载这类话。\n"
            + _amap_status_line()
            + (
                (
                    "- 联网搜索：用户已开启「联网」开关，你有 web_search 工具可主动联网检索。\n"
                    "  凡是涉及**最新/实时/近期**信息的问题（新闻时事、股价行情、软件新版本、"
                    "赛事比分，或你不确定、知识可能已过时的内容），**绝不要回答「我无法联网」"
                    "或凭记忆猜测**，而应主动调用 web_search 获取真实网页结果，再据此用中文总结回答"
                    "并注明来源。\n"
                    "- ⚠️⚠️ **写\"现实题材\"时必须先查证再动笔**（2026-09-27 用户报："
                    "\"让模型写产品介绍文，它没联网就写\"）：\n"
                    "  要写/要介绍的是**现实中真实存在的东西**时 —— 产品、品牌、公司、型号、"
                    "规格参数、价格、行业现状、竞品、政策法规、标准条款、机构单位、人物、事件、"
                    "学术或专业概念、生僻术语 —— **先调用 web_search 拿真实资料，再据此写**。\n"
                    "  · 凭记忆写这类内容**极易编造**：参数、数字、日期、型号、名称、引用"
                    "十有八九是错的，而用户拿去用会出真问题。\n"
                    "  · 搜到官网/权威来源时，用 web_read 点进去看**正文**更准（搜索只给摘要）。\n"
                    "  · 查不到就**如实说\"没搜到权威资料\"**，可以写但要说清哪些是通用描述；"
                    "**绝不许编具体数字、型号、日期或引用**。\n"
                    "  · **只有**纯虚构/纯抒情/纯个人经验（散文、家书、故事、诗、感想）或"
                    "纯逻辑推理、用户已给全材料的改写，才不搜就直接写。\n"
                    "- 日常闲聊、翻译、代码等不需要联网的任务不要调用。\n"
                    "- ★ **读指定网页正文**：用户**直接贴出一个网址**，或说「看看这个链接」"
                    "「这个网页里写了什么」「帮我读一下这篇」「这个页面讲了啥」时，"
                    "**必须调用 web_read 把正文抓下来，再基于正文回答**。\n"
                    "  · **绝不能**凭网址猜内容，也**绝不能**说「我打不开链接 / 我看不到网页」"
                    "—— 你有 web_read，能真的读。\n"
                    "  · 一次可传多个网址（1~5 个）；若正文里出现更关键的下级链接，可以再读一层。\n"
                    "  · 搜索结果里的链接同样用 web_read 点进去看细节（搜索只给摘要）。\n"
                    "  · 只有真抓不到时才如实说明原因（需要登录、被反爬拦了、内容靠 JS 动态渲染），"
                    "并建议改用 web_search 找别的来源。**不许编造页面内容。**\n"
                    "- 天气：**一律用 get_weather 工具**，不要用 web_search。"
                    "搜索引擎对天气只会返回「XX天气预报_15天」这类网站导航页，给不出真实数值；"
                    "get_weather 直接返回气温、天气现象等结构化数据。"
                    "⚠️ 数据源**只有中国气象局**（经高德地图，实况是气象站观测、预报是气象台产品）。"
                    "本机没配高德 key 时**查不了天气** —— 这时如实告诉用户"
                    "「需要先配高德 key，点顶栏的「高德 key」填一个」，"
                    "或者调用 connect_amap 请他填；**绝不许改用搜索或凭印象编天气**。"
                    "工具结果里写了「数据源」和「观测时间」，**照实转述**。"
                    "⚠️⚠️ **只能用工具结果里真正出现的字段**：它开头会写「本次没有这些数据」，"
                    "那就**一个数字都不许编**（体感温度、降水概率这类高德本来就没有），"
                    "连 0、未知 这种占位数字也别写 —— 用户问到就回一句「这个数据源不提供」。"
                    "另外高德只有 **4 天**预报，用户要更长的就直接说明「这个数据源只给 4 天」。\n"
                    "⚠️ 用户问「明天 / 后天 / 某天」的天气时：**不要用 days 参数去缩小范围**"
                    "（填 1 会只剩今天，反而把用户要的那天截掉）；不填即可。\n"
                    "工具结果里**每条预报都带了日期和「今天/明天/后天·周几」的标注**，"
                    "表头还写了今天是几号 —— 直接照着**对应那天**讲，"
                    "**不许说「本次没有提供明天的预报」**（数据里明明有）。\n"
                    "⚠️ 结果里那行「此刻实况」的**湿度、风力只属于现在这一刻**，"
                    "逐日预报里没有这些字段 —— **不许**把它当成某一天的预报值"
                    "（实测踩过：把实况湿度 72% 写进了「明天的预报」）。\n"
                    "- 查机构/单位的对外公开信息（性质、地址、招生章程、招聘公告、年报、办事指南等）："
                    "web_search 会自动追加官方站定向检索（site:gov.cn / site:edu.cn / site:org.cn），"
                    "优先返回官网结果。关键词里带上机构**全称**效果最好。\n"
                )
                if cfg.get("web_enabled")
                else (
                    "- 本机当前处于**离线模式**（用户未开启「联网」开关），无法访问互联网。"
                    "若用户需要最新信息，请提示其打开界面顶部的「联网」开关，"
                    "不要编造实时数据。\n"
                    "  · **地图同样受限**：现在地图**不做任何本地缓存**，离线时只有一份"
                    "内置的常用地名表可用（城市/机场/车站/高校/景点这类常见地名认得）。"
                    "查得到就照实报坐标；查不到就直说「离线模式下查不到，打开联网就能查」，"
                    "**不许编坐标**。「附近有什么」这类查询在离线时**完全查不了**，如实说。\n"
                    "  · 离线时算不出真实道路，工具只会返回**直线距离**。"
                    "这种情况必须跟用户讲明「这是直线距离、不是实际道路」，"
                    "**绝不能说成「驾车 X 公里」**；可以建议他打开「联网」开关再查。\n"
                )
            )
            + "调用工具后，根据工具返回结果继续作答。能直接完成的就动手，不要只建议。\n"
            "- **一轮里可以同时发起多个工具调用**：如果几件事彼此不依赖"
            "（例如同时要「查知识库」+「联网搜索」+「看时间」），"
            "就**在同一条回复里一起发出**，不要一个一个轮着来 —— "
            "它们会被并行执行，总耗时只取决于最慢的那个，比串行快得多、也更准。\n"
            "  只有当下一个工具的**参数依赖**上一个工具的结果时，才分轮调用。\n"
            "- **知识库优先**：用户自己的资料（知识库）比网上搜到的更贴合其领域，"
            "两者都有相关内容时以知识库为准，联网只用来补时效性信息。\n"
            "- **回复长度按「档位」执行，不许凭感觉伸缩**（硬规定 —— 用户反馈过「一会长一会短」）：\n"
            "  · **L0 寒暄**（打招呼、道谢、确认收到、纯情绪回应）：1~2 句，**不超过 30 字**，不列点、不加标题。\n"
            "  · **L1 单一事实 / 单步操作**（问时间、某文件在哪、改个名字、查个数、一句话能答完的）：**直接答，1 段，30~120 字**，不分点、不加小标题。\n"
            "  · **L2 解释 / 怎么做 / 排查**（概念解释、操作步骤、报错原因、用法说明）：**150~300 字**，2~4 个要点或 2~3 段，**不加一级标题**。\n"
            "  · **L3 分析 / 比较 / 方案 / 综述**（为什么、选哪个、怎么看、对比、基于材料做综合判断）：**300~600 字**，用「小标题 + 分点」组织，3~5 个分点。\n"
            "  · **交付物类**（写文章 / 文档 / PPT / 代码）：**正文不受上面限制**，按任务本身需要写满（文档正文一般 800 字以上）；但正文之外的说明、总结**不超过 100 字**。\n"
            "- **同一类问题必须落在同一档，这是防抖动的关键**：\n"
            "  · 用户没追加「详细说说 / 展开讲」时，**不要自行升档**写成长篇；\n"
            "  · 用户没明确说「简单说 / 一句话」时，**不要压到只有半句**；\n"
            "  · 用户说「详细点」升一档、说「简单说」降一档 —— 这是唯一允许的调档方式。\n"
            "- 若检索到的材料足以支撑推断，可以给出**你自己的分析和见解**，\n"
            "  但必须让用户分得清「材料里写的」和「你的推断」——后者要明说是推断。",

            # ---- 「全自动开发平台」的作业方式 ----
            # ⚠️⚠️ 这一段以前**只写在代码模型的文本协议提示里**，默认模型（原生工具调用）
            # 完全看不到 —— 结果平时聊天用的那个模型压根不知道有"建项目/改名/删除"这些工具，
            # 也不会"自己跑一遍、看报错、再改"。用户反馈"模型没有全自动操作平台的能力"、
            # "不能自动完成复杂任务"，根因就是提示词和工具表**只给了一半的模型**。
            "\n【开发平台 · 做东西时按这个来，不必反复问我】\n"
            "- 你有一套**完整的项目操作工具**：workspace_projects（列项目）、"
            "workspace_new_project（新建项目并切进去）、workspace_use_project（切项目）、"
            "workspace_list / workspace_read / workspace_write、workspace_mkdir、"
            "workspace_move（改名、移动）、workspace_delete（删除，进回收站可恢复）、"
            "workspace_run（**真跑**并拿到真实输出与报错）。\n"
            "- 从零做一个东西：**第一步就调 workspace_new_project** 建好并切进去"
            "（同名已存在时它会直接切过去，不会失败）。\n"
            "- 写文件用 workspace_write，**给整份内容**（父目录会自动创建）；"
            "写完**一定要用 workspace_run 真跑一遍**，**不要凭空猜运行结果**。\n"
            "- **报错就自己修**：照真实 traceback 改，改完再跑，最多来回 4 次。"
            "缺依赖、缺数据也**不要甩给用户**——优先改代码绕过去，或用标准库造一份样例数据。\n"
            "- 路径拿不准就先 workspace_list 看一眼，别凭记忆猜。\n"
            "- 用户说「改名/换个文件名」→ workspace_move；「删掉那个」→ workspace_delete；"
            "「看下那个文件」→ workspace_read 读出来再回答（**你有读文件的能力，"
            "绝不要说「我无法读取本地文件」**）。\n"
            "- 整个过程用户只说一次需求，**不要反复确认细节**；拿不准就按最合理的做法做下去，"
            "最后用一两句话总结：建了哪些文件、怎么运行、结果如何。\n",
        ]
        # 长期记忆：**只注入当前对话的那一块** + 极简全局偏好。
        # 对话之间互不相通 —— 切到别的对话就换一份记忆。
        #
        # ⚠️⚠️ 这块**不在这里拼**了 —— 见 `_memory_ctx()` 的说明：
        # 记忆每轮都可能被后台提炼改写，放在系统提示里会让整段前缀（含工具定义）
        # 每轮作废、重算 10 秒。现在由 `_attach_live_ctx` 挂到最后一条用户消息上。
        # 用户拖进来的文档：正文直接给模型，让它能针对内容回答
        if docs:
            blocks, budget = [], 60000      # 总字数上限，避免把上下文撑爆
            for d in docs[:5]:
                text = str((d or {}).get("text") or "").strip()
                name = str((d or {}).get("name") or "文档").strip()
                if not text:
                    continue
                take = text[:max(0, budget)]
                budget -= len(take)
                blocks.append(f"《{name}》\n{take}")
                if budget <= 0:
                    break
            if blocks:
                parts.append(
                    "【用户本轮拖入的文档】—— 请**直接依据这些内容**回答；"
                    "用户问文档里的事时不要说自己看不到文件，也不要凭空编造文中没有的内容：\n\n"
                    + "\n\n---\n\n".join(blocks))
        # ⚠️⚠️ RAG 知识库材料**不能放在这里**（系统提示 = 缓存前缀里）。
        #
        # `kb.build_rag_context(last_user_text, ...)` 是**按本轮问题检索**的 ——
        # 用户每换一个问题，检索到的资料就不同，于是系统提示在第 6000 字左右分叉，
        # 它后面的**全部内容（含 38 个工具定义 ≈ 13000 token）一起作废**，
        # Ollama 只能整份重新预填充。
        # 实测（2026-09-22）：直连 Ollama 固定 system+tools 只换问题时，预填充 0.03s（缓存命中）；
        # 而走应用时 Ollama 日志是 `f_sim_best = 0.18`（只认出 18% 的公共前缀）、
        # `prompt eval time = 10607 ms / 19837 tokens` —— 每次都从零算，10.6 秒。
        #
        # 现在改由 `_attach_live_ctx` 挂到**最后一条用户消息**上（排在工具定义之后），
        # 系统提示 + 工具定义这一大段就恒定不变了，缓存能一直命中。
        # ---------- 技能（skill）：固定说明 ----------
        # 这段**不含具体技能内容**，永远不变，所以放系统提示不影响前缀缓存；
        # 它的作用是让模型知道"有这套东西、怎么增删改"，否则用户说「加一个XX技能」
        # 模型手里只有 save_skill 工具却不清楚该在什么时候用。
        parts.append(
            "\n【技能（skill）—— 用户自定义的做法/规范】\n"
            "用户有一套自己的技能库（写周报、审代码这类固定做法），在界面顶栏「⚡ 技能」里"
            "可以查看、新建、修改、删除；每个技能可选生效方式：手动（用户点一下才用）/"
            "常驻（每轮都生效）/ 自动（你自己判断该不该用）。\n"
            "· 用户说「加一个XX技能」「以后都按这个格式做」「记住这个流程」→ 调 save_skill 写进去；"
            "content 要写成**能直接照做的步骤**（分几步、每步产出什么），别写空话；mode 默认 manual。\n"
            "· 用户说「改一下那个技能」→ 用同名 save_skill 覆盖；「删掉XX技能」→ delete_skill。\n"
            "· 用户只是提了一嘴偏好、没说要长期记住 → **不要**擅自建技能。\n")
        # ---------- 用户自定义技能（skill）----------
        # always 类：正文常驻（用户自己配的规范，每轮都要遵守）；
        # auto 类：只给「名称+描述+触发词」目录，模型判断适用再调 load_skill 取全文。
        # ⚠️ 这两块**只在用户增删改技能时才变**，同一会话里连续几十轮都是同一份文本，
        #    所以放系统提示（缓存前缀区）不会破坏 Ollama 的前缀缓存 —— 与记忆/RAG
        #    那种"每轮都变"的内容不同，别混为一谈。
        try:
            from . import skills as _skills_sp
            for _sk_blk in (_skills_sp.build_always_block(),
                            _skills_sp.build_auto_catalog()):
                if _sk_blk:
                    parts.append(_sk_blk)
        except Exception:
            logger.warning("技能注入系统提示失败（不影响对话）", exc_info=True)
        parts.append("回答请使用中文；直接、可执行。"
                     "长度严格按上面【回复长度分档】执行，不许凭感觉伸缩。")
        if no_tools:
            # ⚠️ **必须把工具说明整段摘掉，光追加一句"没有工具"不够。**
            # 那段说明又长又具体（"用 library 工具写进生成文库""写文件才进文库"…），
            # 模型会照着它编：实测让代码模型改个番茄钟，它回
            # 「已成功保存到 `网页/番茄钟.html`（4730 字节）」——
            # 文件压根不存在，而且**一个字代码都没给**，用户手上什么都没有。
            # 工具说明是 parts 里的**一整个元素**（以这句开头），所以能整体摘掉。
            parts = [p for p in parts if not p.startswith("你可以调用以下工具")]
            parts.append(
                "【本轮没有任何工具】工具清单已被移除。不要调用工具、不要描述工具，"
                "更**不要声称**自己调用了工具、保存了文件或运行了代码 —— 那些你做不到。"
                "需要用户动手的，直接告诉他点哪里（如「点卡片上的 ▶ 运行」）。")
        # ---------- 自开发模式（用户打开后才会走到这里）----------
        try:
            if (config.load_config() or {}).get("self_dev_enabled"):
                parts.append(
                    "\n【自开发模式已打开 —— 你可以改**这个应用自己的源码**】\n"
                    "可用的工具：selfdev_tree（看目录）、selfdev_read（读文件）、"
                    "selfdev_patch（精确替换，**首选**）、"
                    "**selfdev_apply（把\"改成什么样\"交给用户选定的代码模型去写，"
                    "逻辑大改/整段重写时用它）**、selfdev_write（整份覆盖）、"
                    "selfdev_run（跑 .py 验证）、selfdev_restart（重启生效）。\n"
                    "改自己代码的**标准流程**，一步都不能跳：\n"
                    "  0) **先 selfdev_grep 搜关键字定位**（文件动辄几千行，别整份读）；\n"
                    "  1) 再 selfdev_read 只读那一段（start/end 各 ±40 行，带行号）；\n"
                    "  2) selfdev_patch 改最少的几行（find 要和原文一字不差）；\n"
                    "  3) 写完如果需要验证，用 selfdev_run 或让用户看效果；\n"
                    "  4) **最后必须调 selfdev_restart**，否则改动不生效 —— "
                    "它会先试 import 一遍 backend.main，导入失败会**拒绝重启**并把错误告诉你，"
                    "那就回去修。\n"
                    "⚠️ 不许碰 data/、models/、sessions/、image_library/ 这些运行期目录"
                    "（工具会直接拒绝，那是用户的数据不是代码）。\n"
                    "⚠️ 改前端（frontend/*.js|css|html）不需要重启后端，但要让用户 Ctrl+F5 刷新。\n"
                    "⚠️ 每次改动都要在回答里说清：改了哪个文件、改了什么、为什么。\n"
                    "\n【你有眼睛 —— 改完要看一眼】\n"
                    "· 改完前端/样式/排版后，**必须**调 `look_at_screen` 自查："
                    "有没有控件重叠、被裁切、横向挤压、文字看不清；"
                    "有问题就继续改，别凭想象交付。\n"
                    "· 生成图片、PPT、排版稿之后，用 `look_at_image` 看成品质量"
                    "（构图/留白/文字是否糊/是否跑偏），不合格就重做。\n"
                    "· 这两个工具会返回「它看到了什么」的文字描述，把它当**事实**用，"
                    "不要嘴上说「应该没问题」。\n"
                    "· **改前端后系统会自动附一次自查结果**（不用你主动调）；"
                    "看到问题就继续改，改完再收尾 —— 这属于你的判断，不要偷懒。\n"
                    "· 非视觉改动（纯后端逻辑、纯文本），不必反复截图，省时间。\n"
                    "\n【前端改动纪律 —— 用户明确要求「保持页面简洁，不要改太多把页面撑变形」】\n"
                    "· **只许往现有结构里加小控件**，禁止动布局骨架：\n"
                    "  ✗ 不许改顶栏/输入区/消息区的宽高、flex 策略、position；\n"
                    "  ✗ 不许新增大块面板、整行工具条、多个并排按钮；\n"
                    "  ✗ 不许写大段内联 style（超过 3 个属性就要落到 style.css 的 class 里）；\n"
                    "  ✗ 不许给 body/主容器加固定高度或 overflow:hidden。\n"
                    "· **一次只加一个可见控件**（一个按钮 / 一个下拉 / 一个入口），"
                    "加完立刻停手，不要顺手再加两三个。\n"
                    "· 单次前端改动控制在 **80 行以内**；超了说明你在重写而不是在加功能，"
                    "拆成几次做，每次做完让用户看一眼。\n"
                    "· 要展示长内容（diff、日志、代码）**必须**用可折叠容器："
                    "默认收起 + `max-height` + `overflow:auto`，参考现有的 `.sd-card`。\n"
                    "· 顶栏控件总数**不超过 8 个**（现在已接近上限）—— 再加就塞进既有面板里，"
                    "不要往顶栏硬塞。\n"
                    "· 改完让用户 Ctrl+F5，并自查三条：顶栏是否还在一行、输入框是否还在底部、"
                    "消息区是否还能正常滚动。")
        except Exception:
            pass
        return "\n\n".join(p for p in parts if p)


# 上游「一句话都不说」多久判定为卡死（秒）。
# 2026-11-08 用户报「光标还在闪、按钮还显示终止，但再也不出字」：
# 上游连接没断、也不吐数据，旧代码会永远等下去，前端永远停在"生成中"。
_STREAM_IDLE_TIMEOUT = 150.0


async def _stream_lines(resp, session: str = ""):
    """在子线程里读阻塞的 `requests.iter_lines`，通过队列交回事件循环。

    ⚠️ **为什么必须这样绕一层**（实测踩过）：
    `StreamingResponse` 要的是 async 生成器，但 `requests.iter_lines()` 是**阻塞**的。
    直接在 async 生成器里 for 循环读它，会把事件循环整个占住 ——
    uvicorn 拿不到执行机会去 flush socket 缓冲，
    于是所有分块**憋到最后一次性发出去**。
    表现就是用户看到的：发完消息后长时间没反应，然后思考和回答"一股脑"全冒出来。
    （实测 547 个分块全部在同一时刻到达；而直连 Ollama 首块只要 0.4s。）

    改成"子线程阻塞读 + 队列传递"后，事件循环始终空闲，每来一块就能立刻推给前端。

    「终止任务」是怎么实现的：前端 abort 掉 fetch → 连接断开 → Starlette 关掉本生成器
    → 走到下面的 `finally` → `resp.close()` 掐断到 Ollama 的连接 → 模型随即停止生成。
    所以**不需要额外的停止接口**，关键是这个 finally 必须真的执行到。
    """
    q: asyncio.Queue = asyncio.Queue(maxsize=256)
    q = asyncio.Queue()      # ⚠️ 不设上限：满了丢块 = 正文中间被挖掉一段（2026-11-08）
    loop = asyncio.get_running_loop()

    def _worker():
        try:
            for ln in resp.iter_lines(decode_unicode=False):
                loop.call_soon_threadsafe(_q_put, q, ln)
        except Exception:
            pass
        finally:
            loop.call_soon_threadsafe(_q_put, q, None)   # 结束哨兵

    threading.Thread(target=_worker, daemon=True, name="ollama-stream").start()
    _register_stream(session, resp)
    try:
        while True:
            try:
                item = await asyncio.wait_for(q.get(), timeout=_STREAM_IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                # 上游 150 秒一个字节都没吐（连接没断、但也不出字）——
                # 旧代码在这里会永远等下去，前端就一直停在"生成中"。
                # 现在主动掐断，并给主循环一个哨兵：当成"中途断掉"走续写逻辑。
                logger.warning("[stream-idle] 上游 %.0f 秒无任何数据，判定卡死并掐断",
                               _STREAM_IDLE_TIMEOUT)
                yield b'{"__idle": true}'
                break
            if item is None:
                break
            yield item
    finally:
        # 用户点了「终止」，或者连接断了：立刻掐掉到 Ollama 的连接，
        # 否则子线程会一直读下去、模型也会一直生成（白烧显卡）。
        _unregister_stream(session, resp)
        try:
            resp.close()
        except Exception:
            pass


def _q_put(q, item) -> None:
    """往队列里塞数据；满了就丢最旧的，绝不阻塞读取线程。"""
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:
        try:
            q.get_nowait()
        except Exception:
            pass
        try:
            q.put_nowait(item)
        except Exception:
            pass


# ---------- 正在进行的生成（供「终止任务」使用）----------
# 正常情况下前端 abort 掉连接就够了；这里额外留一份引用，
# 是为了「连接没断干净」时也能主动掐掉上游，不至于让模型白跑。
_active_streams: dict = {}
_active_lock = threading.Lock()


def _register_stream(session: str, resp) -> None:
    with _active_lock:
        _active_streams.setdefault(session or "", []).append(resp)


def _unregister_stream(session: str, resp) -> None:
    with _active_lock:
        lst = _active_streams.get(session or "")
        if not lst:
            return
        try:
            lst.remove(resp)
        except ValueError:
            pass
        if not lst:
            _active_streams.pop(session or "", None)


def _stop_streams(session: str = "") -> int:
    """掐断指定会话（空字符串=全部）正在进行的上游连接，返回停掉的数量。"""
    with _active_lock:
        targets = (list(_active_streams.items()) if not session
                   else [(session, _active_streams.get(session, []))])
        killed = 0
        for _sid, lst in targets:
            for resp in list(lst):
                try:
                    resp.close()
                    killed += 1
                except Exception:
                    pass
        return killed


def _ollama_loaded_models() -> set:
    """查询 Ollama 当前**已加载在显存/内存里**的模型名集合。

    用途：判断这一轮要不要现加载模型 —— 30B 冷启动实测约 20 秒，
    不知道的话用户只会看到"发出去半天不吐字"（2026-09-27 用户反馈）。
    拿不到（Ollama 没开等）就返回空集合，调用方按"未加载"处理即可。
    """
    try:
        cfg = config.load_config() or {}
        base = str(cfg.get("ollama_url") or "http://127.0.0.1:11434").rstrip("/")
        req = urllib.request.Request(base + "/api/ps")
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.load(r)
        return {str(m.get("name") or "") for m in (data.get("models") or [])}
    except Exception:
        return set()


@app.post("/api/chat")
async def chat(req: ChatRequest,
               x_image_key: str = Header(default=""),
               x_image_model: str = Header(default=""),
               x_deepseek_key: str = Header(default="")):
    # ⚠️⚠️ 2026-10-17：**开跑前先把绘图引擎放掉**（如果它还占着）。
    #   用户报的现象：画完图切回大模型，「加载很久没反应」。
    #   实测根因有两条：① unload() 之前会 `pipe.to("cpu")`，把 6.9GB 权重搬进
    #   系统内存后还不回去（已修，见 t2i.unload 注释）；② 生图/微改的**失败分支
    #   会提前 return、跳过 unload**，SDXL 就一直占着约 16GB 提交内存。
    #   这里兜最后一道：有残留就释放（正常情况是空操作，耗时 <1ms）。
    #   ⚠️ 2026-11-08：本地 SD 已下线，这段只剩"清理历史残留"，保留无害。
    try:
        if t2i.any_loaded():
            logger.info("[chat] 检测到绘图引擎仍占着内存，先释放再对话")
            t2i.unload()
    except Exception:
        logger.warning("对话前释放绘图引擎失败（不影响对话）", exc_info=True)
    # 新一轮对话 → 清掉上一轮的"自动确认"，避免跨任务生效
    _AUTO_OK["on"] = False
    _AUTO_OK["hits"] = 0
    cfg = config.load_config()
    # ★ 性能档位/上下文在这里落到本次请求（用户要求：参数量不低于此，且可无极调）
    try:
        # ⚠️ with_detect=False：对话路径只要 num_ctx / max_tokens，这两个完全由配置算出来，
        #    跟"探测模型真实上限"无关。传 True 的话 Ollama 一关，每条消息先白等 4 秒
        #    （2026-11-11 实测 4,128 ms，而且是同步调用，会把事件循环一起冻住）。
        _pf = perf.get(with_detect=False)
        cfg["max_tokens"] = max(int(cfg.get("max_tokens") or 0), _pf["max_tokens"])
        cfg["num_ctx"] = _pf["num_ctx"]
    except Exception:
        logger.warning("性能档位读取失败（按原配置继续）", exc_info=True)
    model = req.model or cfg["default_model"]
    if str(model).strip().lower() in ("", "auto"):
        model = cfg["default_model"]          # 前端传 "auto" 时按配置走
    # ★ 2026-11-10：**云端模型名不能走本地通道**。
    #   默认模型改成 deepseek-flash 后，下拉停在「本地·自动」会把 deepseek-flash
    #   发到 Ollama，结果就是界面一直停在"正在加载模型…"（用户报"桌面端发不出去"）。
    if str(model).strip().lower().startswith("deepseek"):
        # ⚠️ 这里**不能 yield**：这个函数本身是 `async def chat()`（返回 StreamingResponse），
        #   一旦出现 yield 就变成 async generator，末尾 `return StreamingResponse(...)` 直接语法错误
        #   （2026-11-10 实测把服务器打挂了一次）。改成构造一个小生成器返回。
        _msg = ("当前「本地·自动」跟随的配置是云端模型 %s，但这一路是本地 Ollama 通道。"
                "请在顶栏把聊天模型切到「⚡ DS V4.1」，或切到本地模型再发。" % model)

        def _ds_mismatch():
            yield json.dumps({"error": _msg, "explained": True, "__end": True},
                             ensure_ascii=False) + "\n"

        return StreamingResponse(_ds_mismatch(), media_type="application/x-ndjson")
    model_note = ""
    # ★ 2026-11-08：**模型没下载就别硬撞 404**。
    #   本机 default_model 是 qwen3-vl:30b（18GB，还没下），选「跟随配置」时
    #   每条消息都会返回 `404 model not found` —— 用户看到的就是"发出去没反应/报错"。
    #   这里自动退到**已装**的同门模型，并明确告诉用户退到了哪个（不偷偷换）。
    try:
        _have = _installed_models()
        if _have and model not in _have:
            _alt = (next((n for n in _have if "qwen3-vl" in n), "")
                    or _have[0])
            if _alt and _alt != model:
                model_note = ("%s 还没下载（或已删除），这次先用 %s 跑；"
                              "下载完在下拉里选它即可。" % (model, _alt))
                model = _alt
    except Exception:
        pass
    images = list(req.images_b64 or [])
    _remember_image(images)          # 记住本轮图片，供后续「把这张图改成…」直接引用
    # ⚠️ 发给模型的那份要**压到长边 ≤1024**：图片是按像素折算 token 的，
    #    2048 的图一张就要 1300+ token，拖几张就把窗口撑爆（Ollama 回 400）。
    #    原件**不动** —— 落盘、"放进 PPT" 都还要清晰的原图。
    model_images = _shrink_for_model(images)
    # ⚠️⚠️ 2026-09-26：**带图的轮次要把工具集精简掉**（见下面的实测数据）。
    #   34~39 个工具 ≈ 15595 token，加系统提示 + 1 张图 = 23244，num_ctx 24576 里
    #   只剩 1332 给"思考+回答" → 带 3 张图直接 400（实测 5 秒返回报错）。
    #   精简掉「地图 / 工作区 / 天气 / 上传代码托管」≈ 省 3400 token。
    #   ⚠️ 但用户这轮**明说了**要地图/工作区时不能裁，否则他会发现"工具不见了"。
    _lean = False
    if model_images:
        _turn_text = str((req.messages[-1].get("content") if req.messages else "") or "")
        _keep_rich = ("地图", "路线", "导航", "附近", "怎么走", "地图上", "标记",
                      "工作区", "项目目录", "workspace", "上传到", "推到", "github",
                      "git", "天气", "气温", "下雨")
        _lean = not any(k in _turn_text.lower() for k in
                        [x.lower() for x in _keep_rich])
    # ⚠️ 本轮附件还必须**落盘**：模型能"看到"图，却拿不到图的字节 ——
    # 用户说「把这张图放进 PPT / 插到文档里」时，只有磁盘上的文件才能被
    # python-pptx / python-docx 使用。落盘后把路径写进提示词，模型照抄即可。
    # （按内容哈希命名，同一张图反复附也不会堆垃圾。）
    attach_paths = []
    if images:
        try:
            from . import img_fetch
            attach_paths = img_fetch.save_chat_images(images)
        except Exception:
            logging.getLogger("uvicorn.error").warning("附件图片落盘失败", exc_info=True)
    messages = list(req.messages)
    # ⚠️ 这里**不要**再按条数截断历史！
    # 曾经这里有一句 `messages = messages[-MAX_CONTEXT_MESSAGES:]`，
    # 后来裁剪逻辑统一挪到了 _trim_history_to_budget（它还会把丢掉的部分
    # 压成「较早对话摘要」注入）。两处并存时，这里先把历史砍掉，
    # 下面的裁剪就无内容可丢 → 摘要恒为空 → 用户回头问"第一轮"照样答不上。
    # 条数上限由 _trim_history_to_budget 内部统一处理。

    last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")

    # 本轮可复用的"上一张图" —— **只有用户明确指向它时才给**（见 _refers_to_prev_image）。
    # 单用户桌面应用，`_LAST_IMAGE` 只存最近一张；以前是"无脑复用"，会把上一轮的图
    # 悄悄接到新话题上，导致"问旅行计划却拿旧图去改/去当参考图"这类串扰。
    prev_img = _prev_image_for(last_user)

    # ⚠️ 迭代轮次：上一轮刚写过代码时，这轮用户可能只说「再改两处」——
    # 里面一个代码关键词都没有，只按本句判定就会掉回默认模型。
    # 判据与三个坑都写在 `_prev_code_flag` 里（抽成函数是为了能被测试直接覆盖）。
    prev_code = _prev_code_flag(messages, last_user)

    # 本轮像写代码 → 换专用代码模型（只影响这一轮，下一轮自动回默认模型）
    if not req.model:
        # 开发台里提问 = 明确在写代码 → **直接接入编程模型**，不用再猜意图
        model, model_note = _route_code_model(
            cfg, last_user, model, prev_code=prev_code,
            force=bool(getattr(req, "studio", False)),
            # ⚠️ 本轮带图 → 不能切到代码模型（它没有视觉能力，图会白传）。
            #    判据见 _needs_task_model / _route_code_model。
            has_images=bool(model_images))
    # 本轮实际用的模型是不是"专用代码模型"？
    # 它不支持原生工具调用，工具定义必须整轮砍掉（见下面 tool_schemas 的处理）。
    _code_model_name = str(cfg.get("code_model") or "").strip()
    code_model_on = bool(_code_model_name) and model == _code_model_name

    # 简单问题收紧生成长度：把"先思考很久"压到几秒（qwen3-vl 无法真正关闭思考）。
    # 但以下情况**绝不能**收紧，否则模型来不及输出工具调用或总结（表现为"思考中断、没有回答"）：
    #   - 有图片上下文（可能要微改）
    #   - 联网模式已开启（要搜索 + 深度阅读 + 引用来源，最耗 token）
    web_on = bool(cfg.get("web_enabled"))
    simple_q = (_is_simple_question(last_user)
                and not images and not prev_img and not web_on)
    if simple_q:
        cfg["max_tokens"] = min(int(cfg.get("max_tokens") or 2048), SIMPLE_MAX_TOKENS)
    elif web_on:
        # 联网场景给足空间：材料多、要求写得详细
        cfg["max_tokens"] = max(int(cfg.get("max_tokens") or 2048), WEB_MAX_TOKENS)

    # 系统提示与工具定义属于**固定开销**，和聊天历史抢同一个上下文窗口，
    # 所以要先算出来，才能知道还剩多少空间给历史。
    # session 必须提前取到：记忆是按对话隔离的，注入时必须知道是哪个对话。
    session = req.session_id or ""
    # ⚠️ 新一轮用户消息开始 → 清空"询问守卫"计数（深度模式的"一次至少问几条/最多问几轮"
    #    靠它兜底，不重置的话上一轮的计数会一直累加，导致这一轮被误判成"已经问够了"）。
    try:
        tools.ask_guard_reset(session)
    except Exception:
        pass
    sys_prompt = _SystemPrompt.build(last_user, session, req.docs, no_tools=code_model_on)
    globals()["_LAST_SESSION"] = session   # 交接卡：重启前要读这个会话的历史（见 backend/handoff.py）

    # ★ 2026-11-13：**图片跨轮可见**（用户报「图片只能当轮使用，下一轮模型就查不到」）。
    #   两件事，缺一不可：
    #     ① 把本轮附件落盘后的路径记进台账（按"用户消息内容哈希"索引，不怕消息序号错位）；
    #     ② 把**之前轮次**的图片按哈希挂回对应的那条历史消息。
    #   本轮图片仍走 images_b64 挂到最后一条 user 消息（attach 会跳过最后一条，不会重复）。
    #   更早的图只留一行「文件路径」提示，不占图片 token（一张图约 1030 token）。
    _hist_imgs: list = []
    try:
        if attach_paths:
            chatimg.record(session, last_user, attach_paths)
        _hist_imgs = chatimg.attach(messages, session)
        if _hist_imgs:
            logger.info("历史图片回挂：%d 张（session=%s）", len(_hist_imgs), session or "-")
    except Exception:
        logger.warning("历史图片回挂失败（本轮按无图继续）", exc_info=True)
    if attach_paths:
        # 把落盘路径交给模型 —— 这是"按用户提供的图片做文档/PPT"能成立的前提。
        # ⚠️ **代码模型那一轮也要注入**：用户说「做个 PPT」会命中造物规则、
        #    被路由给 qwen2.5-coder，如果这时不告诉它图片路径，它就会
        #    **凭空做一份没有图的 PPT，还照样说"封面含您的照片背景"**（实测踩到）。
        sys_prompt += (
            "\n\n【本轮用户附了图片，已存到本机】\n"
            + "\n".join("- %s" % p for p in attach_paths)
            # ⚠️ 这些路径是**给工具参数用的内部路径**：实测模型会把它们当成图片地址
            #    直接贴进回复（`![图](D:\...\chat_images\xxx.png)`）—— 用户看到一串本机
            #    路径，而前端本来就渲染不出它。所以这里明说"别贴出来"。
            + "\n⚠️ 上面是**内部路径**，只用来填工具参数；**不要写进回复正文**"
              "（不要说「已保存到某个磁盘路径」，也不要写成 markdown 图片）。"
              "回复里直接说改了什么 / 做了什么即可，图片会自动展示给用户。\n"
            + "\n按用户的说法把上面的路径填到对应参数里（**不要只描述图片内容、"
              "也不要说「我无法插入图片」** —— 路径已经给你了）：\n"
              "· 说要「放封面 / 当背景 / 做封面图」→ make_pptx 的 **cover_image**\n"
              "· 说「插到第 N 页 / 配在某页上」→ 那一页的 **image**\n"
              "· Word 里插图 → make_docx 的 image 块的 **src**\n"
              "· 给已有文件加图 → edit_office 的 add_image 的 **src**\n"
              "路径原样填进去就行（绝对路径，带盘符）。\n"
        )
    # ⚠️ 这两段都在描述工具（run_python / ask_user / library）。代码模型那一轮
    # 工具已被清空，留着它们只会让模型"照着描述编"（实测：声称已保存文件、却没给代码）。
    if cfg.get("code_exec_enabled") and not code_model_on:
        sys_prompt += (
            "\n\n【本地执行代码】你有一个 run_python 工具，可以在用户电脑上**真跑** Python。\n"
            "- 凡是要精确计算、处理数据、验证算法、换算日期/单位、测试正则的，"
            "**都要先跑一遍再回答**，不要靠心算（心算很容易错，尤其是数字和日期）。\n"
            "- 拿到的输出是真实结果，请依据它作答；如果代码报错，先说明错在哪、"
            "给出修正后的代码并**再跑一次**。\n"
            "- ⚠️ run_python 只能跑**算完就退出**的代码。"
            "**计时器 / 服务器 / 游戏 / 图形界面**这类要一直跑的程序，工具只会跑前几秒做冒烟测试；"
            "那**不是报错、也不说明代码有问题** —— 千万别为了「绕过超时」去改结构"
            "（加线程、加 signal 都没用），直接把代码交给用户、让用户点卡片上的「▶ 运行」。\n"
            "- 回答里保留代码（用户要的是代码），但结论必须来自真实运行结果。")
    # 创作类任务（作文/方案/报告）与生成文库的用法（同上：代码模型那轮不注入）
    if not code_model_on:
        # ⚠️ 问几条要**跟着询问模式走**（2026-09-23）：这里原来写死"2~4 个"，
        # 用户在深度模式下仍然只被问 2~4 条 —— 这正是他报的"单轮问题数不够"。
        _ask_n = ("**5~7 个**（深度询问模式：把用途/受众/篇幅/文体/重点/约束/参考样例都问到；"
                  "拿到答复后如果还有影响写法的空白，**再问一轮**，别问一轮就动笔）"
                  if str(cfg.get("ask_mode") or "quick").lower() == "deep"
                  else "**2~4 个**（快速模式：只问不问就会写错方向的）")
        sys_prompt += (
        "\n\n【写作文 / 拟方案 / 写报告这类创作任务】\n"
        "- ⚠️⚠️ **写现实题材：先查证，再动笔**（2026-09-27 用户报："
        "「让模型写产品介绍文，它没联网就写」）：\n"
        "    要写的东西**在现实中真实存在**时（产品 / 品牌 / 公司 / 型号 / 规格参数 / 价格 / "
        "行业现状 / 竞品 / 政策法规 / 标准条款 / 机构单位 / 人物 / 事件 / 学术或专业概念 / "
        "生僻术语），**必须先调用 web_search 查真实资料，必要时用 web_read 读官网正文，"
        "再依据查到的内容写**。\n"
        "    · 反面例子（用户实际踩到）：让它写某个产品的介绍文，它**没搜索就动笔**，"
        "写出来的参数和亮点**是编的** —— 这类错误最严重，用户拿去用会出真问题。\n"
        "    · 判断标准：**只要你对细节没有把握**（记不清具体数字/名称/日期，或属于专业、"
        "冷门、时效性内容），就先搜。\"我觉得大概是这样\"不算把握。\n"
        "    · 搜不到就**如实说明**「没搜到权威资料」，可以写通用部分但要说清；"
        "**绝不许编造具体数字、型号、日期、引用、来源**。\n"
        "    · **只有**纯虚构/纯抒情/纯个人经验（散文、家书、故事、诗、感想、祝福语）或"
        "**纯粹基于用户已给材料**的改写，才不搜直接写。\n"
        "- **信息不够就先问**：如果用户没说清【用途、给谁看、字数、文体、"
        "要突出的重点、时间或背景】，**必须调用 ask_user 工具**问 " + _ask_n + " 个关键问题再动笔"
        "（要弹问答框，**不要在回答里用文字问**）—— 这比硬猜一篇强得多。"
        "**已经说清楚了就别问**，直接写。\n"
        "- 拿到补充信息后**直接开始写正文**，不要再说「好的我这就写」之类的话，也不要重复问。\n"
        "- 写法：标题 + 小标题 + 段落，**篇幅要够**（没指定字数时一般 800 字以上），"
        "多用具体细节和例子，别写空话套话。\n"
        "- ⚠️⚠️ **交付物必须真的产出，只在正文里写 ≠ 完成**（2026-09-27 实测踩到）：\n"
        "    用户**点名要的成品**，本轮就**必须真的调用对应工具把它做出来**。实测两例：\n"
        "      · 「查一下天气，据此写份出行建议，**做成 Word 文档**」→ 模型查完天气、\n"
        "        把建议写在正文里就收工了，**Word 根本没生成**；\n"
        "      · 「总结一下这些代码，**存进生成文库**」→ 总结写得很详细（4800 字），\n"
        "        却没调写文件的工具，**文库是空的**。\n"
        "    **自检标准：用户能不能点开一个文件？不能就是没做完。**\n"
        "    需要先查数据（天气 / 搜索 / 读文件）时，**查完要接着把成品做出来**，别查完就收尾。\n"
        "- ⚠️ **要什么文件就用哪个工具，别搞混**：\n"
        "    · 要 **Word 文档**（「写成文档」「来个报告/方案/说明书」「导出成 Word」）"
        "→ 用 **make_docx**（封面/目录/表格/提示框一次成型），**不要**用 library 写 .md 再让用户自己转；\n"
        "    · 要 **PPT / 演示稿 / 汇报材料** → 用 **make_pptx**"
        "（⚠️⚠️ **单次调用不许超过 4 页**：先给封面 + 2~3 页，剩下的用 **edit_office** 的 "
        "`{\"op\":\"add_slide\",\"slide_spec\":{...}}` **每次补 1~2 页**。"
        "页数越多越要拆细 —— 实测把 6~8 页塞进一个 slides 数组时 JSON 会被写坏，"
        "连续重试全失败、**整轮白跑，一个文件都拿不到**）；\n"
        "    · 要 **Excel / 表格 / 统计表 / 报表 / 台账 / 预算表**"
        "（尤其用户直接给了一堆数据）→ 用 **make_xlsx**；\n"
        "    · 要**改已有的 Word / PPT / 表格**（「把第 3 页标题换掉」「加一页」「换个配色」）"
        "→ 用 **edit_office**（先 action=inspect 看结构，再 action=edit 改）；\n"
        "    · 要**纯文本 / 代码 / 数据文件**（.md .txt .py .json .csv）→ 才用 library。\n"
        "   生成完只把**下载链接**给用户，**别描述界面按钮或操作步骤**（界面上没有那些）。\n"
        "- ⚠️ **正文里别写 HTML 标签**（`<b>` `<br>` `<small>` 这类）：文档工具不认它们，"
        "会被**原样排进文件**，用户在正式文档里看到一堆字面量标签（2026-09-27 实测踩到）。"
        "要加粗就用 markdown 的 `**文字**`，要换行就自然分句或另起一条。\n"
        "- **配色与风格要按用户说的来**（2026-09-26 用户明确要求支持自定义）："
        "用户提到具体颜色（「红金色系」「企业蓝」「莫兰迪色」「我们 VI 是 #0B5FA5」）时，"
        "**必须**给 make_pptx / make_docx / make_xlsx 传 **colors**"
        '（如 {"accent": "#B8860B", "cover_bg": "#8B0000"} 就是红金），'
        "**光挑个近似的预设不算做到**；只给一部分键也行，其余走 theme。"
        "可用键：accent（主色）/ cover_bg、cover_fg（封面底与字）/ bg / body / muted / card。\n"
        "- ⚠️⚠️ **内容要充实，这是最容易做砸的地方**（2026-09-26 用户明确要求，"
        "实测 8B 模型很容易只给三四条短句就交差）：\n"
        "    · **PPT**：常规主题 **8~15 页**；**每页 3~6 条要点、每条 15~40 字的完整句子**"
        "（写清做什么、为什么、做到什么程度，带上数字 / 时间 / 责任人），"
        "**绝不出现只有标题、下面一两个词的空壳页**；该上表格/图表/指标卡的地方就上；\n"
        "    · **Word**：**8~20 个内容块**，每个段落 **3~6 句、150~400 字**，别只写两三段；\n"
        "    · **Excel**：明细表通常 **8~30 行**（用户给多少写多少，没给就按主题把该有的行补全）。\n"
        "    素材来自知识库/前文/附件时**照着写实**，别自己编数字。\n"
        "- ⚠️⚠️ **工具返回里若出现「自检：…请再补一次」，要立刻再调一次 make_pptx 补足**"
        "（硬规则，但要**适可而止**）：\n"
        "    · 照着自检指的方向补 —— 页数偏少就加页，文字页字数偏少就把每条要点写完整；\n"
        "    · **最多补一次**：补完看返回，若仍提示偏薄也**就此交差**，"
        "把你的正文回答写给用户、如实说一句「内容还可再充实，需要的话我再补」；\n"
        "    · 表格页 / 图表页 / 配图页**不算薄**（它们的内容不在字数上），"
        "别为了它们反复重做；\n"
        "    · **绝不**因为反复重做而把正文回答丢下（实测：连改 7 版后额度耗尽，"
        "用户最后连一句正式回答都没拿到 —— 那比稿子薄更糟）。\n"
        "- **配图**（用户说「配点图」「图文并茂」「找张图放上去」「加个 logo」时）：\n"
        "    · 给那一页/那一块写 **image_query**（中文搜索词，如「校园 图书交换 活动」）\n"
        "      → 系统会**自动联网搜一张合适的图插进去**，你不用自己找链接、也不要说做不到；\n"
        "    · 已经有图（本机路径 / 图片库 id / 网址）就直接填进 image / src；\n"
        "    · 想整页铺背景图用 bg_image，想每页加 logo 用顶层 logo 参数；\n"
        "    · **别编造图片来源**。搜不到时如实说没搜到，不要假装插了图。\n"
        "- 只是让你「写一篇作文 / 拟个方案」→ **直接写在回答里**，不要自作主张建文件。\n"
        "- ⚠️ 用户说「**把这篇 / 刚才那篇**存起来、导出」时，content 必须是**你上一轮写的正文原文**，"
        "**完整复制过去** —— 不许只写摘要、不许留占位符（如「（在此粘贴正文）」）、不许自己另编一版。"
        "存完如实告诉用户存了哪个文件、多少字。\n"
        "- ⚠️ 描述文件内容时**只能说你真正写进去的东西**，别编造章节、页数或里边根本没有的内容。\n"
        "- ⚠️ **别编造工具名或界面功能**。你手上只有系统给的那几个工具；"
        "想做的事没有对应工具时，直接说做不到，不要虚构工具名（如 list_directory、search_files），"
        "也不要编造界面里并不存在的操作（如「右键选择导出为 Word」）——"
        "用户会照着去找，然后发现根本没有。\n"
        "- **知识库和生成文库都支持子文件夹**：列目录时你会看到按文件夹分组的结构；"
        "文件名可以带路径（如 `课程A/第一章/讲义.md`）。"
        "写文件时如果用户指定了文件夹就按他说的放；文件多了也可以主动归类，"
        "但别为了分层硬造文件夹。\n"
        "- ⚠️ **只有真的调用了 library 工具、并拿到成功回执，才能说「已保存」。"
        "没调用工具就声称已保存，是最严重的错误** —— 用户去文库一看是空的，白信你一场。\n"
        "- ⚠️ **知识库是用户的资料，只能读、绝不能改**；凡是要写文件一律进生成文库。"
        "两者用途完全不同，不要混为一谈。\n"
        "- 用户要「WPS 格式 / Word 文档」→ 先写进文库，再用 library 的 export_docx 转成 .docx"
        "（WPS 能直接打开）。旧的 .wps 是私有二进制格式，写不了，要跟他说明这一点。")
    # ⚠️ 通用规则：**两个模型都要看到**（所以不放在上面的 `if not code_model_on` 里）。
    # 用户的要求（2026-09-21）：不只是写文章，**做文档/表格/PPT/代码/图片**这些
    # "要产出东西"的活儿，都该由模型自己判断要不要先问一句。
    # 以前这条只写在"创作类任务"那一段里（还被 `if not code_model_on` 挡着），
    # 于是做代码、画图时从来不问。
    # 询问模式（前端「快速询问 / 深度询问」切换，见 config.ask_mode）。
    # 用户 2026-09-22 的要求：**问题个数不限制、允许多轮询问**，两种模式的区别只在
    # "问到什么程度就动手"；同时必须"无重复、每个都关键、别问个没完"。
    sys_prompt += _ask_rules(cfg)

    # ⚠️ 跨轮串扰：用户 2026-09-22 报「上一轮画水母、这一轮要旅行计划，结果又出了水母的图」。
    # 这类问题的**根子是"默认沿用上文"**：模型会把上一轮的对象/产物/图片无脑带下来。
    # 代码侧已把"旧图自动复用"改成"只在明确指向时才复用"（见 _refers_to_prev_image），
    # 这里再给模型一条明确的判断规则 —— 相关不相关**由它自己判**，但要判对。
    sys_prompt += (
        "\n\n【每一轮先判一句：这是在接着说上面那件事，还是开了个新话题】\n"
        "- **接着说的信号**：有指代词或承接词（它 / 这个 / 那张 / 刚才 / 上面说的 / "
        "再改改 / 继续 / 还有吗 / 换成…），或明确提到了上文的对象。"
        "→ 那就沿用上文的对象、结论和产物。\n"
        "- **新话题的信号**：换了领域、且**没提上文任何东西**（例如上一轮在画图，"
        "这一轮在问旅行安排）。→ **当全新的需求处理**。\n"
        "- ⚠️⚠️ 新话题时**绝对不要**把上一轮的东西再端出来：\n"
        "    · **上一轮生成/搜过图片 ≠ 这一轮还要图片** —— 这一轮没提图，就不要出图、"
        "不要把那张旧图拿出来说事；\n"
        "    · 上一轮写过文件/做过表 ≠ 这一轮还要写文件；\n"
        "    · 不要因为「刚才在聊 A」就把 B 硬往 A 上靠。\n"
        "- 判断**拿不准**时：按新话题做（宁可不带旧上下文），"
        "或者用 ask_user 问一句「你是指刚才那张图，还是新的一件事？」。\n"
        "- 长期记忆与知识库检索到的东西同理：**只在与本轮问题确实相关时才用**，"
        "不相关就别往回答里塞（塞了只会显得答非所问）。\n"
        # ★ 2026-11-08 由 DeepSeek V4.1 自我迭代插入（AI 改的，人只调整了位置）
        "\n【做完的事，不要再做一遍】\n"
        "- 动手前先看上文：这件事是不是已经做完并有结果了？\n"
        "- 用户只是打招呼/追问/确认时，不要再执行一遍，直接引用上次结果；\n"
        "- 只有明确要求重做（重做/再画一张/重新写/换版本）才重新执行。\n"
        "- 如果你觉得用户这句话是在指**更早**聊过的东西（不在你能看到的最近几轮里）："
        "先去**记忆**（在用户消息里）或**历史摘要**里找，再决定；"
        "实在找不到就别硬猜，用 ask_user 问一句「你是指之前聊的 XX 吗？」。\n")
    # 长文创作（作文/方案/报告…）：这一轮只需要"问细节"和"存文件"两个工具，
    # 其余 schema 全砍掉，把省下的额度让给正文；同时把输出上限提上去。
    # 为什么要这么绕：默认模型是思考型的，18 个工具的 schema（约 5800 token）
    # 加上思考，会把 num_ctx 挤到写不下几百字的正文，表现就是"想完什么都没写"。
    # 「要的是能点开的产物」（PPT / Word / Excel）**优先判定**，命中走 office 模式：
    # 见 _OFFICE_HINTS 的说明 —— 否则「做一份方案PPT」会被"方案"判成纯写作、
    # 生成工具被砍，用户最后只拿到一大段文字。
    office_mode = _is_office_task(last_user) and not _is_code_task(last_user)
    writing_mode = (bool(_is_writing_task(last_user))
                    # ⚠️ 「帮我写一段代码」同时命中写代码与长文创作，必须排除 ——
                    # 否则会被当成"作文"砍掉工具、又切到代码模型，两头不讨好。
                    and not _is_code_task(last_user)
                    # ⚠️ 命中办公产物时不走"纯写作"（那会把 make_pptx 等砍掉）
                    and not office_mode
                    and not images and not prev_img)
    # ⚠️ "该联网核实"的提醒**并进第一条 system**，不要另外 append 一条 system。
    #    多条 system 在不同 Ollama 版本上的行为并不稳定（本项目既见过"第二条被忽略"、
    #    也见过"第二条生效"），并进首条才是必然生效的写法。
    # ⚠️ 位置必须在这里 —— `writing_mode` / `office_mode` 是判断依据之一，
    #    得等它们算完（原来放在上面，只有关键词命中才提醒，型号类产品会漏）。
    if _needs_web_ctx(last_user, bool(writing_mode or office_mode)):
        sys_prompt += (
            "\n\n【动笔前的准备：先把事实核实清楚】\n"
            "⚠️ 这**只是一个前置步骤，不改变本轮任务** —— 该写文章就写文章，"
            "该生成 **PPT / Word / Excel 就照样必须把文件生成出来**"
            "（只在正文里写内容、不调用生成工具 = **没做完**，这是硬要求）。\n"
            "这一轮可能涉及**现实中的具体事物**（产品/品牌/公司/型号参数、价格、政策法规、"
            "行业现状、机构人物、时效信息、专业或生僻概念）。\n"
            "- 需要核实时：**先调用 web_search**（必要时再用 web_read 读官网正文），"
            "**然后依据检索到的内容**继续完成本来的任务；\n"
            "- 不要凭记忆写这类内容的参数、数字、名称、日期 —— 编造会真的误导用户，"
            "这是最严重的一类错误；\n"
            "- ⚠️ **不必联网**的情形：纯虚构/纯抒情/纯个人叙事（散文、诗、家书、故事、感想）；"
            "或材料完全来自用户已给的内容、用户自己的私有产品（网上根本搜不到）"
            "—— 这时直接写；信息不够就**用 ask_user 问用户要**。\n"
            "- 搜了确实没有可用资料：如实说明「没搜到权威资料」，可以写通用部分，"
            "但**不得编造具体细节**。")
    if writing_mode or office_mode:
        # 长产物的**工具调用 JSON 本身就很长**（一份 15 页 PPT 的内容都在
        # tool_calls 的参数里），额度给小了会被截断 → 生成失败或只剩半份。
        # 用户 2026-09-25 明确要求「取消所有篇幅限制」，这里统一给足。
        cfg["max_tokens"] = max(int(cfg.get("max_tokens") or 2048), WRITING_MAX_TOKENS)
    tool_schemas = tools.make_schemas(cfg.get("web_enabled", False),
                                      cfg.get("rag_enabled", False),
                                      cfg.get("code_exec_enabled", False),
                                      writing=writing_mode,
                                      office=office_mode,
                                      lean=_lean,     # 带图的轮次走精简集（见 _lean 的说明）
                                      # ⚠️ 询问模式要传进去：ask_user 的**工具描述**会据此
                                      # 写清"这一轮该问几条、要不要再多问一轮"。
                                      # 只写在系统提示里不够 —— 模型挑工具时先看 schema。
                                      ask_mode=str(cfg.get("ask_mode") or "quick"))
    # 本轮**真正提供给模型**的工具名（原生通道）—— 用来校验"泄漏进正文的调用"。
    # ⚠️ 必须在下面 `tool_schemas = []`（代码轮砍工具）**之前**取，否则代码轮永远是空集。
    # ⚠️ 有了它，模型把调用写成文本时我们才敢执行：只有这一轮确实给了这个工具
    #    （开关也开着）才放行，否则只把那段文本清掉、不执行 —— 免得模型凭空调出一个
    #    用户已经关掉的工具（比如「联不上网却还是联网搜索了」）。
    _native_tool_names = {s.get("function", {}).get("name")
                          for s in (tool_schemas or []) if isinstance(s, dict)}
    if code_model_on:
        # 代码轮给足输出额度：要写整份文件 + 工具调用 JSON，
        # 用普通问答的 2048 会让文件**写一半被截断**（见 CODE_MAX_TOKENS 的说明）。
        cfg["max_tokens"] = max(int(cfg.get("max_tokens") or 2048), CODE_MAX_TOKENS)
    if code_model_on:
        # ⚠️ 专用代码模型（qwen2.5-coder）**不支持 Ollama 的原生工具调用通道**。
        # 实测（2026-09-15，同一句「写个合并有序列表的函数并运行验证」）：
        #   · 带 tools：31.5 秒，**没有 tool_calls**，把调用当 JSON 文本写进正文 ——
        #     用户看到一堵 `{"name": "run_python", "arguments": {...}}`，代码根本没跑；
        #   · 不带 tools：12.2 秒，直接给 ```python 代码块，内容正确。
        # 所以这一轮把工具全砍掉，让它专心写代码；前端会把代码块渲染成
        # 带「▶ 运行 / ✏ 编辑」的卡片，用户照样能一键跑 —— 而且更有掌控感。
        tool_schemas = []

    # ---------- 上下文预算：按剩余空间裁剪历史 ----------
    # 工具定义本身就有 3000~4500 token，系统提示约 1000；
    # 联网时还要塞进"8 条结果 + 若干篇网页正文"，动辄上万 token。
    # 不按预算裁剪的话很容易超过 num_ctx，Ollama 会**直接截断提示词**
    # （模型可能看不到系统提示或本轮问题）→ 表现为回答莫名其妙或干脆没有。
    #
    # ⚠️ 注意保存用的是原始 messages —— 必须是**完整**历史。
    # 曾经的写法是裁剪后直接覆盖 messages，结果每次落盘都只存下裁剪后的部分，
    # 早期对话被永久删除（重启后恢复出来就是残的）。裁剪只影响"这一轮发给模型什么"。
    all_messages = list(messages)
    # ⚠️ 记忆 / 知识库材料现在挂在**用户消息**上（见 _attach_live_ctx），
    #    但它们同样要占上下文额度 —— 预算里必须算进去，否则会撑爆窗口。
    mem_ctx = _memory_ctx(session, last_user, cfg)
    # ★ 2026-11-08 用户要求：「自我迭代重启之后，还记得刚才聊了什么、改了什么」。
    #   交接卡由 backend/handoff.py 维护（改动清单 + 上次重启前的几轮对话），
    #   **挂在最后一条用户消息上**（和记忆同一位置）—— 放进系统提示会破坏前缀缓存。
    try:
        from . import handoff as _handoff
        _handoff_ctx = _handoff.render(session)
        if _handoff_ctx:
            mem_ctx = (_handoff_ctx + "\n\n" + mem_ctx) if mem_ctx else _handoff_ctx
    except Exception:
        logger.warning("交接卡注入失败（本轮忽略）", exc_info=True)
    # ★ 2026-11-09：把「本对话真实执行过的工具动作」也挂上去 —— 用户问
    #   「你刚刚改了什么」时，模型手里必须有账本，不能只说「没改任何东西」。
    try:
        _act_ctx = actlog.render(session)
        if _act_ctx:
            mem_ctx = (_act_ctx + "\n\n" + mem_ctx) if mem_ctx else _act_ctx
    except Exception:
        logger.warning("工具动作账本注入失败（本轮忽略）", exc_info=True)
    rag_ctx = ""
    if cfg.get("rag_enabled"):
        try:
            rag_ctx = kb.build_rag_context(last_user,
                                           top_k=cfg.get("rag_top_k", 4)) or ""
        except Exception:
            logger.warning("知识库检索失败（本轮忽略）", exc_info=True)
            rag_ctx = ""
    budget_sys = sys_prompt + "\n\n" + mem_ctx + "\n\n" + rag_ctx
    # ⚠️ 附件图片也要占上下文（挂在最后一条 user 消息上发给 Ollama）——
    #    以前没算，拖一张大图进来就会撑爆窗口、Ollama 直接回 400。
    messages, dropped, _clamped = _trim_history_to_budget(
        messages, budget_sys, tool_schemas, cfg,
        # ⚠️ 2026-11-13：**回挂的历史图片也要算进预算**（每张约 1030 token）。
        #    只算本轮 model_images 的话，历史里多挂两张就把窗口撑爆 → Ollama 回 400。
        images_b64=(list(model_images or []) + list(_hist_imgs or [])))
    digest = _history_digest(dropped)
    # ★ 2026-11-10：历史没被裁掉，但模型在几千字里做「关键词对齐」会抓错
    #   （用户问「胡萝卜那个笑话」，它拿最近一条顶替）。这里把最近 8 轮压成速览
    #   一起注入，给它一个能直接比对的锚点。
    try:
        _recent_idx = _recent_turns_index(req.messages)
        if _recent_idx:
            digest = (digest + "\n\n" + _recent_idx) if digest else _recent_idx
    except Exception:
        logger.warning("最近轮次速览生成失败（本轮忽略）", exc_info=True)

    # 联网搜索：以前这里会**预先**跑一次搜索并把结果塞进上下文，
    # 但模型自己也会调用 web_search 工具 —— 两条路径重复执行，结果两批材料叠加，
    # 极易撑爆上下文窗口（表现为"搜完了却没有回答"）。
    # 而且预检索那批材料既没有相关性过滤，也没有深度阅读，质量更差。
    # 现在改成只给一句**提醒**，真正的检索统一走 web_search 工具。
    # ⚠️ 2026-09-27：这句提醒**已经上移到 sys_prompt 里**（见上面 _wants_search 那段）。
    #    原来是在这里 `messages.append({"role": "system", ...})` 追加**第二条 system**，
    #    而"多条 system 谁生效"在不同 Ollama 版本上并不稳定 —— 并进首条才必然生效。

    async def gen():
        # ⚠️ 这两个是 chat() 作用域里的变量，但要在"思考吃光配额→重试"那条路上
        #    **换掉**（重试时把工具砍到精简集腾窗口）。
        #    不声明 nonlocal 的话，下面的赋值会让它们变成 gen() 的局部变量，
        #    于是前面第一次 `client.chat(..., tools=tool_schemas)` 直接
        #    UnboundLocalError: cannot access local variable（实测踩过）。
        nonlocal tool_schemas, _native_tool_names
        # 每轮对话的本地工作消息序列 = system + 用户历史
        #
        # ⚠️ 较早对话摘要**必须并进第一条 system 消息**，不能再加一条 system。
        # 实测：Ollama 的 qwen 系对话模板只取第一条 system，第二条会被**静默丢弃** ——
        # 摘要写进去了、接口也没报错，但模型压根看不到，症状就是"还是说忘记了"。
        # 工具执行期间要能**实时**把弹窗推给前端，所以单独开一个队列
        loop = asyncio.get_running_loop()
        live_ui: asyncio.Queue = asyncio.Queue()
        # ⚠️ 用户**自己这一轮**发的内容太长时（粘贴了一篇几万字的文档），
        #    _trim_history_to_budget 会把它截断到装得下 —— 截了就必须**如实说**，
        #    不能偷偷丢掉他粘贴的内容（他以为模型读过全文，实际没有，会得出错误结论）。
        if _clamped:
            yield json.dumps({"note": (
                "你这一轮发的内容偏长（约 %d 字），**超出了模型单次能装下的长度** —— "
                "已保留**开头和结尾**共约 %d 字（中间省略约 %d 字）。\n"
                "· 想让我**完整**读完，建议把它**存成文件**拖进来让我读（文件不占对话窗口）；\n"
                "· 或者到设置里把「上下文长度 num_ctx」调大 —— "
                "⚠️ 前提是显存装得下，超了会有一部分算到 CPU 上、明显变慢。"
                % (_clamped["orig"], _clamped["kept"], _clamped["cut"]))}) + "\n"
        # ⚠️ 历史摘要**不能并进系统提示** —— 它会随上下文裁剪而变，
        #    同样会把它后面的工具定义一起废掉（见 _attach_live_ctx 的说明）。
        full_sys = sys_prompt
        # 代码模型走不了原生工具通道 → 改用它能用的"文本协议"（见 _split_text_tool_call）
        code_text_tools = _code_text_tools(cfg) if code_model_on else {}
        if code_model_on:
            # 代码模型不支持工具调用，得明确告诉它"直接写代码"，
            # 否则它会模仿工具调用的格式吐一堵 JSON（实测）。
            #
            # ⚠️ 光说"没有工具"还不够 —— 上面系统提示的正文里明明写着
            # 「你可以调用以下工具」「用 library 工具写进生成文库」。
            # 工具 schema 被清掉、提示词却还在描述工具，模型就会**以为自己存了**：
            # 实测第二轮改需求时它只回 337 字，写着
            # 「已保存到文件夹 代码/番茄钟/番茄钟.html（4208 字节）」——
            # 文件压根不存在，而且**一个字代码都没给**，用户啥也拿不到。
            # 所以这里要写成**覆盖性**的硬规则，并明确禁止"虚假完成"。
            _tt_docs = "\n".join("· %s —— %s" % (k, v)
                                 for k, v in code_text_tools.items())
            full_sys += "\n\n【本轮最高优先级 · 覆盖上面所有关于工具的说明】\n"
            if code_text_tools:
                # 有文本协议工具：**说清怎么调**，并允许它说"跑过了"——
                # 因为这回是真跑（以前不许它说，是因为它确实做不到）。
                full_sys += (
                    "本轮的模型不支持普通的函数调用，所以工具**改用文本协议**给你。\n"
                    "想用工具时，输出一个以 ```tool 开头的代码块，里面放一段 JSON，例如：\n"
                    "```tool\n"
                    '{"name": "run_python", "arguments": {"code": "print(1+1)"}}\n'
                    "```\n"
                    "我会**真的执行**它，然后把结果作为下一条消息发给你"
                    "（以「工具结果：」开头），你接着继续。\n\n"
                    "**当前开发项目：%s**（工作区工具都作用在它身上；"
                    "用户可以在界面上切换项目，切过来就是另一套完全独立的文件）。\n\n"
                    "本轮可用工具：\n" % workspace.active_project()
                    + _tt_docs + "\n\n"
                    # ⚠️⚠️ 这段是为一个**实测复现过**的坑加的，别删：
                    # 用户连着问"读一下 hello.py"→"看有没有错误"→"项目1里的 hello.py"→
                    # "你看不到吗？？"，模型越答越自信地说「我无法读取本地文件系统」。
                    # 真相是**它有能力，但忘了**：
                    #   · 第 1 轮用了默认模型（原生工具通道），workspace_read 正常；
                    #   · 第 1 轮回答里带了 ``` 代码块 → 命中"上一轮有代码就继续用代码模型"，
                    #     第 2 轮起换到 qwen2.5-coder，只能走文本协议；
                    #   · 而历史里的助手消息**从来没出现过 ```tool 块**，
                    #     模型就照着历史"只回答、不调工具"，越往后越笃定自己没有文件能力。
                    # 光在开头说一句"你改用文本协议"不够 —— 要有**针对症状**的硬规则。
                    "⚠️ **最重要**：你**有**读文件的能力（workspace_read）。"
                    "用户只要提到文件名或路径（如 hello.py、src/app.js），"
                    "**就必须先用 workspace_read 把它读出来**再回答，"
                    "**绝不允许**说「我无法读取本地文件」「请把内容粘贴给我」"
                    "「也不要凭记忆猜文件里写了什么」。\n"
                    "例：用户说「看一下 hello.py 有没有错误」→ 你这一步只输出：\n"
                    "```tool\n"
                    '{"name": "workspace_read", "arguments": {"rel": "hello.py"}}\n'
                    "```\n\n"
                    # 「完全自动开发项目」的作业流程。不写这一段的话，模型就算
                    # 手里有 workspace_new_project 也想不到要先建项目、
                    # 更不会自己"跑→看报错→改→再跑"地把项目做完整。
                    # ⚠️ 代码轮**也要能问用户**（2026-09-21 用户要求：做代码、做图
                    # 这些"产出内容"的活儿，也该由模型自己决定要不要先问）。
                    # 原来这里写的是「不要中途问用户」，等于把询问彻底禁掉了。
                    # 现在改成"缺关键前提先问一次、问完就别再问"。
                    "【需求没说清就先问，说清了就直接做】\n"
                    "用户只给了一句话、关键前提缺失（做来干什么、给谁用、"
                    "要什么语言、输入输出长什么样、跑在什么数据上）→ **动手前先问一次**：\n"
                    "```tool\n"
                    '{"name": "ask_user", "arguments": {"questions": ['
                    '{"question": "这个程序主要给谁用？", "header": "使用场景", '
                    '"options": ["自己用", "交作业", "给别人用"]}]}}\n'
                    "```\n"
                    "我会把问答框弹给用户，他填完以「工具结果：」发回给你，你再开始写。\n"
                    "⚠️⚠️ **提问不会结束这一轮**：拿到答复就**接着把代码写完**，"
                    "不要只回一句「好的，请稍等」「明白了」就收工，"
                    "也不要停下等用户再发一条消息 —— 那等于把活丢回给他。\n"
                    "⚠️ 问题**只写在 ```tool 块里** —— 正文里**不要**再写「请问……？」"
                    "「好的，您想要……」这类问句。实测模型会两处都写一遍，"
                    "用户在正文里看到问句会以为你在等他回话，而不是去点弹框。\n"
                    "⚠️ **问题个数不限、可以分多轮问**（用户明确要求）：拿到答复后如果"
                    "又冒出影响写法的关键疑问，**再发一次 ask_user** 就行；"
                    "但不许问重复的、不许凑数。\n"
                    "⚠️ 用户已经说清楚了（或只是让你改一行、跑一下、解释一段代码）→ "
                    "**别问，直接做**；也不要为了少打扰而跳过关键问题。\n\n"
                    "【做一整个项目时，按这个顺序自动推进】\n"
                    "0. 不确定现在在哪个项目里 → 先调 workspace_projects 看一眼。\n"
                    "1. 从零做一个新东西 → **第一步调 workspace_new_project** 建项目并切进去。\n"
                    "2. 铺结构：直接用 workspace_write 写文件即可，**父目录会自动创建**"
                    "（要建**空目录**才用 workspace_mkdir）。一次一个文件，写整份内容。\n"
                    "3. **写完就跑**：.py 用 workspace_run 真跑一遍看真实输出，不要靠猜。\n"
                    "4. **报错就自己修**：照真实 traceback 改，改完**再跑**，"
                    "直到跑通为止（最多来回 4 次）。缺依赖/缺数据也不要甩给用户 —— "
                    "优先只改代码绕过去，或用标准库把样例数据造出来。\n"
                    "5. 全部跑通后，再用一两句话总结：建了哪些文件、怎么运行、结果是什么。\n"
                    "6. ⚠️ **读完文件必须接着写回去**：workspace_read / read_saved 只是"
                    "「先看清楚现状」的一步，**读完立刻用 workspace_write 把改好的整份内容写回**，"
                    "不要停下来问用户、也不要只是把改法讲一遍。"
                    "实测踩过：模型读完之后只回一段「你可以这样改…」就收工了，"
                    "用户拿到的文件一个字都没变 —— 那等于白做。\n"
                    # ⚠️ 这里原来写的是"不要反复确认、不要问他细节"。
                    # 那是在**上一条硬规则**（模型老是问、问完还不做）的背景下加的，
                    # 但副作用是"需求再含糊也闷头做"。现在改成：开工前可以问一次（见上面），
                    # **问了之后就必须一次做完**，不许再确认第二遍。
                    "用户说过「你看着办 / 直接做」、或你已经问过一次拿到答复了 → "
                    "**就必须一次做完，不要再确认** —— "
                    "拿不准的地方按最合理的做法做下去，并在总结里说明你的选择。\n\n"
                    "【硬性规则】\n"
                    "1. 想验证代码对不对，就**真的调用 run_python / workspace_run 跑一遍**"
                    "看真实输出，**绝对不要**凭空猜输出。\n"
                    "   ⚠️ 但**计时器 / 服务器 / 游戏 / 图形界面**这类**要一直跑**的程序，"
                    "在工具里本来就跑不完 —— 工具只会跑前几秒做冒烟测试。"
                    "那是**正常的、不是报错**，**更不要为了「绕过它」去改代码**"
                    "（加线程、加 signal 都没用）。这种情况直接收工，"
                    "告诉用户「点代码卡片上的 ▶ 运行 就能完整跑」。\n"
                    "2. **一次只调用一个工具**；调用时除了那个 ```tool 块"
                    "**不要输出任何其他文字**。\n"
                    "3. **一路调到真的跑通为止**：报错就照真实报错改、再跑，最多来回 4 次。"
                    "**缺文件、缺数据时不要叫用户去准备** —— 你自己在代码里造一份样例数据"
                    "（临时文件或直接用内置字符串），把流程跑顺；"
                    "用户要的是「能跑的东西」，不是「还得他自己补条件的代码」。\n"
                    "4. 要改「生成文库」里已有的文件，**先用 read_saved 读一遍**，"
                    "在真实内容上改；不要凭记忆重写。\n"
                    "5. 除了通过工具，**绝对不要说**「已保存到…」「已经写入…」"
                    "「文件已生成」「我运行过了」—— 没调用工具就等于没做。\n"
                    "6. **要交付出文件**（脚本 / 网页 / 配置…）：用 workspace_write 写进"
                    "**开发工作区**（传相对路径，如 hello.py / static/app.js），"
                    "再按需用 workspace_run 跑它验证。用户会在界面的「开发台」里看到、"
                    "并且可以自己改。**只在 run_python 里跑一遍、不在工作区留下文件，"
                    "等于没有交付。**\n"
                    "7. **多文件项目**：需要 css/js/图片就分别 workspace_write 到子目录"
                    "（static/、src/ 等），HTML 里用**相对路径**引用它们；"
                    "写完后可以用 workspace_list 核对文件齐不齐。\n"
                    "8. **拿不准的新用法**（某个库的新版本、报错含义）就先 web_search 查，"
                    "别凭印象编 API。\n"
                    "9. 全部做完后，把**完整代码**（放在 ``` 代码块里、标注语言）"
                    "和**真实运行结果**一起给我。\n"
                    "10. ⚠️ **工具调用只能写在 ```tool 块里、一次写完整的一条**，"
                    "而且**别在正文里重复贴同一份代码**（用户会看到一堆一模一样的卡片）。\n"
                    "    调用里的 JSON 必须**括号配对**：写 workspace_write 时"
                    "记得把最外层那个 `}` 也写上。\n"
                    "11. ⚠️ 程序里有 `input()` / `sys.stdin` 时：工具运行时**没人在旁边打字**，"
                    "直接跑只会拿到 EOF 报错（**那不代表代码写错了**）。"
                    "要验证就把输入按行放进 `stdin` 参数；"
                    "不然写清楚「这个程序需要你在运行后输入」，让用户点 ▶ 运行 自己输。\n")
            else:
                full_sys += (
                    "本轮**一个工具都没有**（工具清单已被移除），所以：\n"
                    "· 上面提到的 library / 生成文库 / 保存文件 / 运行代码 **一律不适用**。\n"
                    "  **绝对不要说**「已保存到…」「已经写入…」「文件已生成」"
                    "「我已经运行并验证」——\n"
                    "  你做不到这些，说了就是骗人。\n")
            full_sys += (
                "· **必须把完整代码重新写一遍**放在 ``` 代码块里（标注语言）。\n"
                "  哪怕是改一个小地方，也要给出改完之后的**整份代码**，\n"
                "  不能只说「我改了 A、加了 B」——那样用户手上没有可用的东西。\n")
            if not code_text_tools:
                full_sys += (
                    "· 需要用户自己做的动作，就直说：「请点卡片上的 ▶ 运行」"
                    "「请点 💾 存到文库」。\n"
                    "· **不要**输出形如 {\"name\": \"...\", \"arguments\": {...}} 的 JSON —— "
                    "那是工具调用的内部格式，写出来用户看到的是一堆乱码。\n")
            full_sys += (
                "【代码要能直接跑】用户会在界面上点「▶ 运行」执行你的代码："
                "\n· **不要用 input() 等交互输入** —— 运行环境没有键盘，会直接报 EOFError。"
                "需要参数就写成模块顶部的变量，或从 sys.argv 取并给默认值。"
                "\n· 结尾要有 print() 把结果打出来，否则界面上只会显示「没有输出」。"
                "\n· 尽量只用标准库（沙箱里的第三方库不保证装全）。"
                "\n· 网页类产物：写成**单个自包含的 .html**（样式和脚本都内联），"
                "用户双击就能用，别引用外部 CDN。")
        # ⚠️ 记忆、知识库检索、当前时间、历史摘要都挂到**最后一条用户消息**上
        #    （见 _attach_live_ctx）：它们都是"每轮都会变"的内容，放进系统提示
        #    会把后面的工具定义一起废掉，导致 Ollama 每轮重算 ~10 秒。
        #    放在稳定前缀之后，缓存才能一直命中。
        # 用户在前端点选的「手动技能」：只对这一轮生效，取完即清（见 skills.consume_manual）。
        # ⚠️ 它属于"每轮会变"的内容，所以挂到最后一条用户消息上，不进系统提示。
        try:
            from . import skills as _skills_chat
            _skill_ctx = _skills_chat.consume_manual(session)
        except Exception:
            _skill_ctx = ""
        working = _attach_live_ctx([{"role": "system", "content": full_sys}] + messages,
                                   digest=digest, mem_ctx=mem_ctx, rag_ctx=rag_ctx,
                                   img_ctx=_img_available_note(), skill_ctx=_skill_ctx,
                                   sched_ctx=_sched_ctx())
        final_text = ""
        final_thinking = ""
        used_tools = set()          # 本轮真正调用过的工具（用来判断"是不是光嘴上说说"）
        # 换了模型就提前吱一声，免得用户以为"怎么这次回复的口吻变了"
        if model_note:
            yield json.dumps({"note": model_note}) + "\n"
        # ★ 模型还没加载 → 先告诉前端"在加载"，别让用户对着空白等 20 秒。
        #   30B 冷启动实测约 20~45 秒（磁盘被占时更久），这段时间里 Ollama
        #   一个字都不吐 —— 没有这条提示，界面上就是"发出去完全没反应"，
        #   用户会以为卡死了（2026-09-27 反馈："每次模型启动都要等那么久"）。
        #   ⚠️ 必须放在**第一次 client.chat 之前**，否则起不到作用。
        #
        #   ⚠️⚠️ 2026-09-27 补：**不能只看 `_ollama_loaded_models()`** ——
        #   实测 `api/ps` 会报旧数：画图时对话模型已被请出显存，ps 依旧报
        #   "已加载、占 9.69GB 显存"，于是这条提示**根本不触发**，
        #   用户对着空白等了 **397 秒**（见 t2i.rewarm_async 的实测记录）。
        #   所以再加一个判据：后台正在做"请出去 + 请回来"时，也算"在加载"。
        try:
            _need_load = model not in _ollama_loaded_models()
            try:
                _need_load = _need_load or t2i.rewarm_in_flight()
            except Exception:
                pass
            if _need_load:
                yield json.dumps({"status": "⏳ 正在加载模型 %s（首次加载较慢，之后就是秒回）…"
                                            % model}) + "\n"
        except Exception:
            pass
        # session 已在上面定义（记忆按对话隔离，需要提前拿到）

        # 空回答重试：qwen3-vl 的思考会吃掉大量 token，偶尔会「想完了但没来得及写正文」，
        # 表现为思考戛然而止、界面什么都没有。这种情况按上面配置重跑并加倍配额。
        # 允许**最多 2 次**加倍（原来只给 1 次）：简单问题档只有 3072，
        # 一次加倍到 6144 仍可能被思考吃光 —— 留第二次才够顶到 12288。
        # 注意：用独立的 gen_params 而不是改 cfg —— 在 gen() 里给 cfg 赋值会让它
        # 变成局部变量，导致前面读取 cfg 时报 UnboundLocalError。
        retries_done = 0
        _MAX_EMPTY_RETRIES = 2
        # "Ollama 解析工具调用失败"的自动重试次数（见 _is_toolparse_err）。
        # 给 2 次：一次是采样噪声，连着两次都写坏的概率很低。
        _toolparse_retries = 0
        # ⚠️ 2026-09-28：2 → 4。实测 30B 写坏工具调用 JSON 的频率不低
        # （日志里反复见 `invalid character '+' after object key:value pair` 之类），
        # 只重试 2 次经常还不够，用户看到的就是"工具调用失败/格式不对"。
        # 多试两次代价很小（这类失败返回很快，不会卡住）。
        _MAX_TOOLPARSE_RETRIES = 4
        # 「办公产物没生成」只兜一次（见下面 break 前那段）。
        _office_nudge_done = False
        # 「要图却没画」也只兜一次（同样的道理：模型常把画面写成文字就收工）
        _img_nudge_done = False
        _ask_nudge_done = False      # 深度询问兜底只放一次
        # 「回答被截断在半句」的自动续写次数（最多 2 次，见下面的 [continue] 段）
        _cont_used = 0
        # 轮次/时间预算（用户要求把调度次数放宽到最多）
        try:
            _max_rounds = int(cfg.get("max_tool_rounds") or MAX_TOOL_ROUNDS)
        except Exception:
            _max_rounds = MAX_TOOL_ROUNDS
        _max_rounds = max(1, min(_max_rounds, 500))
        try:
            _budget_s = float(cfg.get("max_tool_seconds") or 1200)
        except Exception:
            _budget_s = 1200.0
        _t_start = time.time()
        gen_params = dict(cfg)

        # 【完整性兜底】整次生成共用一份"预览过哪些文件、当时写进了哪个项目"的记录。
        # 必须放在轮次循环**外面**：模型常常第 1 轮建项目+预览、第 2 轮才正式写入，
        # 每轮清空就看不出"预览写到 A 项目、正式写入落在 B 项目"，孤儿文件清不掉。
        _wsstate = {"streamed": {}, "written": {}}
        # 「提示词超出窗口」只自动重试一次（见下面 _is_context_err 分支）——
        # 再砍就什么都没了，第二次还失败就如实告诉用户。
        _ctx_trimmed = False
        for _round in range(_max_rounds):
            # ★ 时间预算：轮次管不住"一轮很慢"的情况（读大文件、跑长脚本），
            #   所以再加一道墙钟上限。超了就收尾，但**不做静默退出**。
            if _round > 0 and (time.time() - _t_start) > _budget_s:
                logger.warning("[rounds] 超过时间预算 %.0f 秒（第 %d 轮），转入收尾",
                               _budget_s, _round)
                yield json.dumps({"note": (
                    "这一轮已经跑了 %.0f 分钟（时间上限），我让它先把进度交代清楚。"
                    "接着做就回一句「继续」。" % (_budget_s / 60.0))}) + "\n"
                break
            # 工具调用中间轮不再重复附图片
            attach_images = model_images if _round == 0 else None
            # ⚠️ 每轮发请求前**再核一次预算**：整轮开始前只裁过一次历史，
            #    但工具轮是"越跑越长"的 —— 每轮都会把工具结果追加进 working
            #    （网页正文一条就 9000 字 ≈ 6700 token），多跑两轮必然顶爆窗口。
            #    顶爆后的兜底是"砍到只剩最后两条"，等于**把工具查到的材料全丢了**，
            #    任务半途而废（用户看到的是"查了却没回答"）。这里先把大的压下去。
            if _round > 0:
                try:
                    _pt = _shrink_round_prompt(working, cfg, tool_schemas)
                    logger.debug("[round] 第 %d 轮提示词约 %d token", _round, _pt)
                except Exception:
                    logger.warning("工具轮预算压缩失败（按原样继续）", exc_info=True)
            try:
                resp = client.chat(working, model=model, stream=True,
                                   images_base64=attach_images, params=gen_params,
                                   tools=tool_schemas)
            except ollama_client.OllamaError as e:
                yield json.dumps({"error": str(e)} | {"__end": True}) + "\n"
                return

            round_msg = {"content": "", "thinking": None, "model": model}
            code_emitted = 0        # 代码轮已经推给前端的正文长度（见 _safe_emit_len）
            # 思考打转（复读）检测的游标：上次检测到多少字 / 这轮提示过没有
            loop_checked, loop_warned = 0, False
            # 边生成边落盘的进度（见 _partial_ws_write）：文件 / 已落盘字数 / 上次时刻
            _ws_rel, _ws_len, _ws_t = "", 0, 0.0
            # 【完整性兜底】流式落盘只是"给编辑器看的预览"，**不是正式写入**。
            # 记下每个被预览过的文件"原本长什么样 + 预览时写进了哪个项目"。
            # ⚠️ `_streamed` **跨轮累计，不能每轮清空**：实测模型经常
            # "第 1 轮建项目 + 预览文件、第 2 轮才正式写入" ——
            # 每轮清空的话，第 2 轮看不到第 1 轮留下的预览记录，
            # 孤儿文件就永远清不掉（实测正是这么漏的）。
            tool_calls = None
            done_reason = ""
            # 这一轮有没有收到 Ollama 的「结束块」(done)。
            # 收不到 = 上游是**中途断掉**的（连接被掐 / Ollama 崩了 / 超时），
            # 此时正文只有半截。以前这种情况既不提示也不续写，
            # 用户看到的就是"说到一半突然没了"（2026-11-08 用户报）。
            done_seen = False
            # Ollama 报的"这个提示词实际占了多少 token"（见下面 done 的处理）。
            # 判断"还剩多少额度能写正文"时，它是唯一权威的数字 ——
            # 按字符估算会差得很远（工具 schema 那种 JSON 尤其不准）。
            prompt_tokens = 0
            # 本轮是否需要"原样再来一次"（Ollama 解析工具调用失败的自动重试）
            _retry_round = False
            # 注意：必须用 _stream_lines（子线程读 + 队列），
            # 不能直接 for line in resp.iter_lines() —— 见它的注释
            async for line in _stream_lines(resp, session):
                if not line:
                    continue
                if isinstance(line, bytes):
                    line = line.decode("utf-8")
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("__idle"):
                    logger.warning("[stream-idle] 本轮被空闲超时中断，转入续写/收尾")
                    yield json.dumps({"note": (
                        "模型这边超过 %d 秒没有任何输出（上游卡住了），已断开重来…"
                        % int(_STREAM_IDLE_TIMEOUT))}) + "\n"
                    break
                if obj.get("error"):
                    _emsg = str(obj["error"])
                    # ⚠️ 这种情况**不是环境坏了**，是模型这一次把工具调用的 JSON
                    # 写坏了（单引号 / 写残）。同一句话重来一次基本就好，
                    # 所以先重试，别急着把英文报错丢给用户。
                    if (_is_toolparse_err(_emsg)
                            and _toolparse_retries < _MAX_TOOLPARSE_RETRIES):
                        _toolparse_retries += 1
                        _retry_round = True
                        logger.warning("[tool-parse] 模型工具调用 JSON 写坏，自动重试 %d/%d：%s",
                                       _toolparse_retries, _MAX_TOOLPARSE_RETRIES, _emsg[:120])
                        yield json.dumps({"note": (
                            "模型这次把调用格式写错了，正在自动重试（第 %d 次）…"
                            % _toolparse_retries)}) + "\n"
                        break
                    # ⚠️ 提示词真的超出窗口（Ollama 明说 prompt_tokens > n_ctx）：
                    #    不是环境坏了，是这一轮带的东西太多（附件图片 + 长历史最常见）。
                    #    硬砍到最近两条、连检索材料一起丢掉，再试一次 —— 用户不该为这个
                    #    看到一屏英文报错（2026-09-22 用户截图里那条就是这个）。
                    if _is_context_err(_emsg) and not _ctx_trimmed:
                        _ctx_trimmed = True
                        _retry_round = True
                        _hard = all_messages[-2:] if len(all_messages) > 2 else all_messages
                        working[:] = _attach_live_ctx(
                            [{"role": "system", "content": full_sys}] + list(_hard),
                            digest=digest, mem_ctx=mem_ctx, rag_ctx="",
                            sched_ctx=_sched_ctx())
                        logger.warning("[ctx-overflow] 提示词超出窗口，已精简到最近 %d 条重试：%s",
                                       len(_hard), _emsg[:120])
                        yield json.dumps({"note": (
                            "这一轮带的内容超出了模型窗口（图片或前文太长），"
                            "已自动精简后重试…")}) + "\n"
                        break
                    # 重试用完（或不是这类错误）→ 给一句人话，别再丢英文原文
                    logger.warning("[ollama-error] %s", _emsg[:200])
                    yield json.dumps({"error": _friendly_ollama_error(_emsg),
                                      "explained": True, "__end": True}) + "\n"
                    return
                m = obj.get("message") or {}
                if m.get("thinking"):
                    round_msg["thinking"] = (round_msg["thinking"] or "") + m["thinking"]
                    final_thinking += m["thinking"]
                    # 实时透出思考增量（即使在折叠状态下也要持续刷新进度）
                    yield json.dumps({"message": {"thinking": m["thinking"]}}, ensure_ascii=False) + "\n"
                    # —— 思考打转（复读）检测 ——
                    # 每多出 240 字查一次"有没有哪段话重复了 3 遍以上"。
                    # 采样参数能大幅减少打转，但压不干净；真发生时至少要让
                    # 用户看到实话、让我们在日志里留痕（否则只是"AI 看起来傻了"）。
                    if not loop_warned and len(final_thinking) - loop_checked >= 240:
                        loop_checked = len(final_thinking)
                        _piece = find_looping_piece(final_thinking)
                        if _piece:
                            loop_warned = True
                            logger.warning("[think-loop] 思考打转：同一段重复出现 —— %r",
                                           _piece[:48])
                            yield json.dumps({"note": (
                                "模型在思考里绕圈了（同一段话重复了好几遍），"
                                "这次回答可能不太靠谱 —— 直接回一句「别重复，换个思路」"
                                "再问一次通常就好。")}) + "\n"
                if m.get("content"):
                    round_msg["content"] += m["content"]
                    # **两种模型都要逐字流式**，区别只在"多敢扣住尾巴"：
                    #   · 代码轮（文本协议）标记集激进：```json / {"name" 都算；
                    #   · 聊天轮标记集保守：只认 XML 包装和 ```tool 围栏 ——
                    #     正文里出现 JSON 是正常内容，激进会毁掉流式体感（见 _LEAK_MARKS）。
                    # 见 _safe_emit_len。
                    _safe = _safe_emit_len(
                        round_msg["content"],
                        _TEXT_TOOL_MARKS if code_model_on else _LEAK_MARKS)
                    if not code_model_on and _safe > 0 and _CALL_HEAD_RE.match(round_msg["content"]):
                        # 裸调用（见 _CALL_HEAD_RE）：一个可匹配的标记都没有，
                        # 只能"整段先扣住"，等轮末的严格判据决定是执行还是原样补发。
                        _safe = 0
                    if _safe > code_emitted:
                        _piece = round_msg["content"][code_emitted:_safe]
                        code_emitted = _safe
                        final_text += _piece
                        yield json.dumps({"message": {"content": _piece}}, ensure_ascii=False) + "\n"
                    if code_model_on:
                        # ★ "AI 自动逐字输入到编辑器"就靠这一段：
                        #   模型把 workspace_write 的正文**逐字**吐出来，
                        #   我们把已经生成的部分先落盘 → 内置 VS Code 的文件监视器
                        #   看到文件在变，编辑器里就是代码一点点长出来。
                        #   不落盘的话，用户只能等生成完"啪"地出现一整份文件。
                        _rel, _partial, _newproj = _partial_ws_write(round_msg["content"])
                        if _rel and _partial is not None:
                            _now = time.time()
                            _fresh = _rel != _ws_rel
                            # **这一轮要写进哪个项目**：模型如果同时发了
                            # workspace_new_project，就写进那个新项目 —— 因为工具要等
                            # 模型把整轮说完才执行，此刻 active_project() 还是旧项目，
                            # 直接写会落进上一个项目里（实测踩到，留下孤儿文件）。
                            _target = _newproj or workspace.active_project()
                            if _fresh and cfg.get("ide_focus_on_write", True):
                                # 刚开始写一个新文件 → 让编辑器**把它打开**、
                                # 并把窗口拉到前台（见 _show_writing_file 的说明）。
                                # 放线程里做：起进程 / Win32 调用别卡住生成流。
                                threading.Thread(
                                    target=_show_writing_file,
                                    kwargs={"rel": _rel, "proj": _target},
                                    daemon=True).start()
                            # 节流：换文件立刻写；同文件每 60 字或每 0.3 秒写一次。
                            # 太密会把磁盘和 VS Code 的文件监视器打爆；太疏就没有"打字"感。
                            if (_fresh or len(_partial) - _ws_len >= 60
                                    or _now - _ws_t >= 0.30):
                                if _fresh and _rel not in _wsstate["streamed"]:
                                    # 第一次预览这个文件 → 先记下"它原本长什么样、
                                    # 在哪个项目里"，万一这轮没能正式写完，好还原回去
                                    try:
                                        _p = workspace.abs_path(_rel, _target)
                                        if os.path.exists(_p):
                                            with open(_p, "r", encoding="utf-8",
                                                      errors="replace") as _fh:
                                                _wsstate["streamed"][_rel] = (_fh.read(), _target)
                                        else:
                                            _wsstate["streamed"][_rel] = (None, _target)
                                    except Exception:
                                        _wsstate["streamed"][_rel] = (None, _target)
                                try:
                                    workspace.stream_write(_rel, _partial, _target)
                                except Exception:
                                    pass      # 落盘失败不影响模型继续生成，结束时还会正式写一次
                                yield json.dumps({"ui": {
                                    "type": "typing", "rel": _rel,
                                    "chars": len(_partial), "reset": _fresh,
                                    "project": _target,
                                }}) + "\n"
                                _ws_rel, _ws_len, _ws_t = _rel, len(_partial), _now
                if "message" in obj:
                    tc = m.get("tool_calls")
                    if tc:
                        tool_calls = tc
                # Ollama 在最终块里给 done_reason：
                #   "stop"   = 正常结束
                #   "length" = 撞到 num_predict 上限被截断（评测数正好等于配额）
                # 这是判断"回答是否被思考吃光"的**可靠信号**，比猜正文长不长准得多。
                if obj.get("done"):
                    done_reason = obj.get("done_reason") or ""
                    done_seen = True
                    # 记下**提示词实测 token 数**：算"本轮还能写多少正文"要用它。
                    # 实测（2026-09-25）：全开 38 个工具时这里会报 ~14700，
                    # 而 num_ctx 只有 24576 —— 光提示词就吃掉 60% 的窗口。
                    prompt_tokens = obj.get("prompt_eval_count") or 0

            if _retry_round:
                # 这一轮整轮作废、原样重来。
                # ⚠️ 放在**任何后续处理之前**：报错是在读到错误事件那一刻发生的，
                # 此时 `working` 还没被追加过任何东西（工具结果/助手消息都在后面），
                # 所以直接 continue 重新发一次请求即可，不需要清理状态。
                continue

            # ---- 摘掉"泄漏成正文的工具调用"，并补发之前被扣住的尾巴 ----
            # ⚠️ **两种模型都要跑**（2026-09-21）：默认模型走原生通道，
            #    但它偶尔会把调用包成 <function-call>…</function-call> 写进正文，
            #    而 Ollama 只认自己模板里的 <tool_call> → tool_calls 为空 →
            #    以前这里被 `if code_model_on` 挡掉，那坨 JSON 就直接漏给用户了。
            # 放在 `if not tool_calls` 之前 —— 解析出来的调用要能接进下面同一套执行逻辑。
            text_protocol = False
            if round_msg["content"]:
                if code_model_on:
                    # 代码轮：只认代码轮真正给过的文本工具（开关关掉的给了也不能跑）
                    _allowed, _bare = set(code_text_tools), True
                else:
                    # 聊天轮：只认"本轮真正给过它的工具"；带壳子的照认，
                    # 裸 JSON 走**严格**判据（见 _strict_bare_calls —— 宁漏勿误）
                    _allowed, _bare = _native_tool_names, "strict"
                _calls, _clean = _split_text_tool_calls(
                    round_msg["content"], allowed=_allowed, bare=_bare)
                round_msg["content"] = _clean
                # 逐字流式时把"可能是工具调用壳子"的尾巴扣住了，这里确认过再补发。
                # 用 `> code_emitted` 判断：如果扣住的那段真是 ```tool 块，
                # 清理后的正文会比已发的短，那就什么都不补。
                if len(_clean) > code_emitted:
                    _tail = _clean[code_emitted:]
                    code_emitted = len(_clean)
                    final_text += _tail
                    yield json.dumps({"message": {"content": _tail}}, ensure_ascii=False) + "\n"
                if _calls:
                    # ⚠️ 本轮**原生通道已经调过工具**时，正文里这坨多半是模型在"复述"
                    #    （实测它连 `<result>` 都自己编出来了）—— 只清文本、**不再执行**，
                    #    否则同一个工具会跑两遍（写文件、跑代码这种是有副作用的）。
                    if tool_calls:
                        logger.info("[text-tool] 本轮已有原生调用，正文里的调用按复述处理（只清理）：%s",
                                    [c["name"] for c in _calls])
                    else:
                        # 这轮模型是**把调用当文本写的**（原生通道没接住），
                        # 后面组装对话时也必须按文本协议走，否则模板对不上。
                        text_protocol = True
                        tool_calls = [{"function": {"name": c["name"],
                                                    "arguments": c["arguments"]}}
                                      for c in _calls]

            if not tool_calls:
                # 被思考吃光配额：Ollama 明确告诉我们 done_reason=length，
                # 说明撞到了 num_predict 上限。典型表现是"思考到一半就断、没有回答"
                # （思考把配额用尽，正文一个字都没来得及写）。
                # 这不是模型坏了，纯粹是配额给少了——加倍重试一次。
                truncated = (done_reason == "length")
                # 上游中途断掉（整轮没收到 done 块）：正文很可能只有半截。
                # 这跟"撞长度上限"是两种不同的断法，但对用户的观感一样 ——
                # 话说到一半就没了，所以续写逻辑要一起覆盖（2026-11-08）。
                cut_off = (not done_seen)
                # ★★ 2026-11-09：**「假正常结束」也必须续写**（用户反复报「回答只有一半」）。
                #   实测 Ollama 经常给 done_reason="stop"（看起来正常收尾），正文却停在半句上
                #   （截图里「把主题告诉我，我」就是这么断的）。以前这种情况既不续写也不提示，
                #   用户永远只能看到半句话。用 _looks_unfinished() 判定：明显没写完就按截断处理。
                if not truncated and done_seen and _looks_unfinished(round_msg["content"]):
                    truncated = True
                    logger.warning("[continue] 正常结束但正文停在半句上，按截断处理：…%r",
                                   round_msg["content"][-40:])
                if cut_off:
                    truncated = True      # 复用下面同一套续写逻辑：对用户来说都是「话说一半没了」
                # 只有「本轮一个字正文都没写出来」才值得重试。
                # 已经有正文还重试的话：模型是从头重写，而前端已经渲染过一份，
                # 两轮内容会**拼在一起**（正文叠正文，最难看出是重复的那种）。
                has_body = bool(round_msg["content"].strip())
                if truncated and not has_body and retries_done < _MAX_EMPTY_RETRIES:
                    # ---------- 重试之前，先把"窗口"腾出来 ----------
                    # ⚠️ 2026-09-25 实测（这就是"重试了两轮还是没正文"的真因）：
                    #   全开工具时 **光提示词就占 14706 token**（38 个工具，
                    #   32280 字符），num_ctx 只有 24576 —— 固定开销吃掉 60%，
                    #   留给"思考 + 正文"的不到 1 万。
                    #   而原来的重试只把 max_tokens 从 8192 加到 16384，
                    #   **早就超过剩余窗口了** → 必然再次被截断 → 白转两轮。
                    # ⇒ 重试这一轮把工具砍到精简集：这一轮模型一个字正文都没写出来
                    #   （全烧在思考上），说明它根本没在调工具，砍掉不损失能力，
                    #   却能腾出**上万 token** 的窗口 —— 重试这才有可能成功。
                    freed = 0
                    if retries_done == 0:        # 只在第一次重试时砍，第二次沿用
                        try:
                            _before = _est_tokens(
                                json.dumps(tool_schemas, ensure_ascii=False))
                            # ⚠️⚠️ 办公任务（要做 PPT/Word/Excel）**不能**切成
                            #   "写作精简集"——那个集合里没有 make_pptx/dcox/xlsx，
                            #   于是首次尝试把额度烧在思考上之后，重试这一轮
                            #   **根本没法生成文件**，用户最后只拿到一段文字
                            #   （2026-09-26 端到端实测：工具=[]、零个产物）。
                            #   office_mode 时改用 office_gen：只留三个生成工具，
                            #   照样省下两千多 token，但守住"能做出文件"。
                            tool_schemas = tools.make_schemas(
                                cfg.get("web_enabled", False),
                                cfg.get("rag_enabled", False),
                                code_exec=False,
                                writing=not office_mode,
                                office=office_mode,
                                office_gen=office_mode,
                                # 带图的轮次重试时**继续用精简集**，
                                # 否则一刀切回全集，又从"省了窗口"变回"超窗"
                                lean=_lean,
                                ask_mode=str(cfg.get("ask_mode") or "quick"))
                            freed = max(0, _before - _est_tokens(
                                json.dumps(tool_schemas, ensure_ascii=False)))
                            # 工具集变小了，白名单必须跟着换 ——
                            # 否则模型把工具名写进正文时会被误判成"真的调过"
                            _native_tool_names = {
                                s.get("function", {}).get("name")
                                for s in (tool_schemas or []) if isinstance(s, dict)}
                            logger.warning("[retry] 为腾窗口精简工具：省下约 %d token",
                                           freed)
                        except Exception:
                            logger.warning("[retry] 精简工具失败，按原样重试",
                                           exc_info=True)
                    retries_done += 1
                    # 这一轮**真实**还能写多少 = num_ctx − 提示词（扣掉刚腾出来的）
                    room = MAX_TOKENS_CEILING
                    if prompt_tokens:
                        room = min(MAX_TOKENS_CEILING,
                                   max(1024,
                                       int(cfg.get("num_ctx") or 8192)
                                       - max(0, int(prompt_tokens) - freed) - 512))
                    boosted = max(int(gen_params.get("max_tokens") or 2048) * 2, 4096)
                    nxt = min(boosted, room)
                    _cur = int(gen_params.get("max_tokens") or 0)
                    # ⚠️⚠️ 「额度涨不上去」**不等于**「重试没意义」（2026-09-26 修）。
                    #   办公任务（做 PPT/Word/Excel）一上来就把 max_tokens 设成 16384
                    #   （见上面的 WRITING_MAX_TOKENS），可它**本来就超窗**：
                    #   提示词 1.4 万 + 16384 > 24576。于是重试走到这里时 nxt(≈12008)
                    #   反而**小于**当前额度 → 直接判"放弃重试" → 连第二次机会都没有
                    #   → 模型首轮把额度全烧在思考上之后，用户只拿到一段思考。
                    #   而精简工具让提示词**小了两千多 token**，同样额度下实际能用的
                    #   空间比上一轮宽 —— 这一轮当然值得再试。
                    #   所以：额度能涨、**或者**这轮腾出了窗口（freed>0），都要重试。
                    if nxt > _cur or freed > 0:
                        # 但额度必须夹进本轮**真实窗口**：比窗口还大的额度等于没给，
                        # 模型照样在窗口边界被截断（这正是办公任务首轮失败的原因）。
                        gen_params["max_tokens"] = min(max(_cur, nxt), room)
                        # ★ 上一轮的思考必须作废，并通知前端把面板清空。
                        #   重试是从头重新生成，思考会**重新来一遍**；不重置的话
                        #   新一轮的思考就直接接在旧思考后面 —— 界面上看起来就是
                        #   "同一个思路说了两遍"（2026-09-19 用户反馈的"思考重复"）。
                        #   final_thinking 是整轮结束后发给前端的完整思考，
                        #   这里的失败尝试一个字都不该留在里面。
                        final_thinking = ""
                        yield json.dumps({"message": {"thinking_reset": True}}) + "\n"
                        # 措辞别用"配额不足" —— 用户看到会以为是自己额度用完了，
                        # 其实是模型把输出空间花在"思考"上了（2026-09-15 用户反馈）
                        yield json.dumps({"note": (
                            "模型思考占满了本次输出空间，正在腾出空间、加长上限重试…")}) + "\n"
                        continue
                    # 连"再试一次"的空间都没有（提示词已经把窗口占满）→
                    # 别让用户白等一轮，直接走下面的兜底
                    logger.warning("[retry] 放弃重试：max_tokens=%s 已顶到可用上限 %d",
                                   gen_params.get("max_tokens"), room)
                if truncated and not round_msg["content"].strip():
                    # 重试也没救回来。**别再给用户一个空气泡 + 一句道歉** ——
                    # 把它的思考原样交付（明确标注这不是正式回答）。
                    # 「什么都没输出」和「输出的是半成品」相比，后者至少有信息量。
                    if final_thinking.strip():
                        _fb = ("（⚠️ 下面这段是模型的**思考过程**，不是正式回答 —— "
                               "这一次它把输出空间全花在思考上了，没能写出答案。"
                               "想让它答得更短，直接回一句「别想太多，直接给答案」"
                               "再问一次。）\n\n" + final_thinking.strip())
                        final_text += _fb
                        yield json.dumps({"message": {"content": _fb}}) + "\n"
                        yield json.dumps({"note": (
                            "这次模型没写出正式回答，已把它的思考内容直接展示给你。")}) + "\n"
                    else:
                        yield json.dumps({"note": (
                            "模型把输出空间都花在思考上了，没能写出正文。"
                            "换个更具体的问法，或在设置里调大「最大生成长度」再试。")}) + "\n"
                # ⚠️⚠️ 【办公产物的最后一道兜底】用户要的是"**能点开的文件**"
                #   （PPT / Word / Excel），但模型有时把内容直接写在回复里就收工。
                #   实测（2026-09-27）：「帮我做个 PPT，介绍大疆 Mini 4 Pro 无人机」
                #   只回了 1525 字大纲、**一个文件都没有**；同类还发生在
                #   「介绍我们公司的新产品」那条上。提示词里明明写了铁律
                #   （自检标准：用户能不能点开一个文件？），它偶尔还是漏。
                #   ⇒ 这里**代码层兜一次**：没产出文件就退回一轮，明确要求它去生成。
                #   ⚠️ 只兜一次（`_office_nudge_done`），否则会和模型死磕、白烧时间。
                #   ⚠️ 只在这种情况放行：模型确实写了正文（说明它自认为答完了）。
                # ★★ 2026-11-08 用户报「你有时候说话总说一半」——根因就在这：
                #   done_reason=length（撞到 num_predict 上限）时，上面的逻辑只处理
                #   「一个字正文都没写」那种情况；**已经写了一半**的反而什么都不做，
                #   于是回答硬生生停在半句上（截图里「什么时候」就是这么断的）。
                #   ⇒ 补上「续写」：把已写正文放回历史，让它从断开处接着写。
                #   云端通道（deepseek_client）早就有这段，本地通道一直缺。
                if (truncated and has_body and _cont_used < 3
                        and _round + 1 < _max_rounds):
                    _cont_used += 1
                    logger.warning("[continue] 正文被截断（第 %d 次续写），接着往下写",
                                   _cont_used)
                    yield json.dumps({"note": (
                        "上一条回答被长度上限截断了，正在接着往下写完…")}) + "\n"
                    working.append({"role": "assistant",
                                    "content": round_msg["content"] or ""})
                    working.append({"role": "user", "content": (
                        "（系统提示）你上一条回答**被长度上限截断了**，停在了半句话上。"
                        "现在**从断开的地方接着往下写**：不要重复已经写过的内容，"
                        "不要重新开头，不要解释、不要道歉，直接续上正文；"
                        "如果内容其实已经写完，就只把最后那句补完整。")})
                    # 续写这一轮把额度抬到「窗口允许的最大」——
                    # 否则同样的额度又会在同一个地方断掉，续写等于白跑。
                    _room2 = MAX_TOKENS_CEILING
                    if prompt_tokens:
                        _room2 = min(MAX_TOKENS_CEILING,
                                     max(1024,
                                         int(cfg.get("num_ctx") or 8192)
                                         - int(prompt_tokens) - 512))
                    _cur2 = int(gen_params.get("max_tokens") or 2048)
                    gen_params["max_tokens"] = min(max(_cur2 * 2, 4096), _room2)
                    continue

                # ★ 2026-11-10：续写次数用完（或轮次用尽）时**必须告诉用户**。
                #   以前这里什么都不做、直接往下走 —— 用户看到的就是「说到一半没了」，
                #   既不知道是撞了长度上限，也不知道回一句「继续」就能接着写。
                if truncated and has_body:
                    _tip = ("\n\n（⚠️ 这条回答撞到了输出长度上限，已自动续写仍没写完。"
                            "回一句「继续」我就从断开的地方接着往下写。）")
                    final_text += _tip
                    yield json.dumps({"message": {"content": _tip}},
                                     ensure_ascii=False) + "\n"
                if (office_mode and not _office_nudge_done
                        and round_msg["content"].strip()
                        and not (used_tools & _OFFICE_MAKER_TOOLS)
                        and _round + 1 < _max_rounds):
                    _office_nudge_done = True
                    logger.warning("[office] 本轮未产出文件（已用工具=%s），退回一轮要求生成",
                                   sorted(used_tools))
                    yield json.dumps({"note": (
                        "还没看到文件产出，我让它把成品真正做出来"
                        "（PPT / Word / Excel 才是交付物）。")}) + "\n"
                    working.append({"role": "user", "content": (
                        "（系统提示）你刚才只把内容写在回复里了，**但用户要的是一个能点开的文件**。"
                        "现在**立即调用生成工具把它做出来**："
                        "演示稿 → make_pptx；文档 → make_docx；表格 → make_xlsx。"
                        "⚠️ **单个工具调用的内容不许超过 4 页/4 项**（超长 JSON 会被写坏、"
                        "整轮全废）—— 先做封面 + 2~3 项，剩下的用 **edit_office** "
                        "**每次补 1~2 项**，分几次调用。"
                        "不要复述内容、不要再问用户、也不要解释，直接开始生成。")})
                    continue
                # ⚠️⚠️ 【深度询问 的代码层兜底】（2026-11-08）
                #   用户开了「深度问」，但模型**一个 ask_user 都没调**就开工，
                #   最后在正文里写「你选一个」——用户看到的就不是弹框而是普通文字。
                #   提示词里写死了"必须弹框"，8B 仍然会漏，所以这里兜一轮。
                if (str(cfg.get("ask_mode") or "").lower() == "deep"
                        and not _ask_nudge_done
                        and "ask_user" not in used_tools
                        and _looks_like_task(last_user)
                        and round_msg["content"].strip()
                        and _round + 1 < _max_rounds):
                    _ask_nudge_done = True
                    logger.warning("[ask] 深度模式但本轮没调用 ask_user，退回一轮")
                    yield json.dumps({"note": (
                        "现在是深度询问模式，我让它先把关键问题问清楚。")}) + "\n"
                    working.append({"role": "user", "content": (
                        "（系统提示）当前是**深度询问**模式，用户明确要求"
                        "「动手前把信息问透」。你刚才**没有调用 ask_user**，"
                        "而是直接开始做/在正文里问 —— 这不符合他的设置。\n"
                        "现在**立刻调用 ask_user**：一次给 **5~7 个**关键问题"
                        "（用途/受众、范围与篇幅、风格或口径、硬性约束、交付形式、参考样例…），"
                        "每个都要能改变你怎么做。**不要**在正文里写问句，也不要把问题写成一串文字。\n"
                        "拿到答复后接着把活做完，不要再停。")})
                    continue

                # ⚠️⚠️ 【要图却没画 的最后一道兜底】（2026-11-08）
                #   用户说「画一只狐狸」「来张壁纸」，模型有时只写一段画面描述就收工。
                #   提示词里写了「只有调用工具才算完成」，但 8B 会漏。
                #   这里代码层兜一次：本轮没调用任何出图工具 → 退回一轮逼它真的画。
                if (not _img_nudge_done and round_msg["content"].strip()
                        and any(_k in (last_user or "") for _k in _IMG_HINTS)
                        and not (used_tools & _IMG_MAKER_TOOLS)
                        and _round + 1 < _max_rounds):
                    _img_nudge_done = True
                    logger.warning("[image] 本轮未产出图片（已用工具=%s），退回一轮",
                                   sorted(used_tools))
                    yield json.dumps({"note": "还没看到图片，我让它真正画出来。"}) + "\n"
                    working.append({"role": "user", "content": (
                        "（系统提示）用户要的是**看得见的图片**，你刚才只写了文字描述。"
                        "现在**立即调用工具**：AI 创作 → generate_image（prompt 写清主体/风格/构图，"
                        "aspect 按用途给 9:16 / 16:9 / 1:1）；要现成真实照片 → web_image_search。"
                        "不要复述、不要再问、也不要解释，直接调用。")})
                    continue
                break  # 本轮无工具调用，得到最终答复

            # ---------- 执行工具（Agent loop）----------
            # 1) 把 assistant 的 tool_calls 加入工作序列
            #    文本协议下**不能**带 tool_calls 字段 —— 它本来就没按原生格式调用，
            #    塞进去会让对话模板对不上（那是原生通道的结构）。
            if text_protocol:
                working.append({"role": "assistant", "content": round_msg["content"] or ""})
            else:
                working.append({"role": "assistant", "content": round_msg["content"] or "",
                                "tool_calls": tool_calls})
            # 2) 逐个执行
            ui_events = []
            # images：本轮拖入的图（否则复用最近一张）
            # shown_images：本轮已展示给用户的图，供「保存到图库」工具按序号引用
            # session：记忆按对话隔离，写记忆的工具必须知道当前是哪个对话
            ctx = {"images": images or prev_img, "shown_images": [],
                   # ★ 「上一张图」**不经过指代判断**也透一份给工具（2026-10-17）：
                   #   模型既然已经**决定调用 edit_image**，那就是明确要改上一张图，
                   #   此时用它是安全的（不存在"把旧图接到别的话题"的串扰）。
                   #   以前只给 ctx["images"]（受 _refers_to_prev_image 约束），
                   #   用户说「再改一下 / 改改看」这类不含指代词的短句就取不到底图。
                   "last_image": (_recent_image() or [None])[0],
                   # ★ 用户涂抹的选区蒙版（白=要改的地方）→ 透给 edit_image 做局部重绘
                   "mask": (list(req.mask_b64 or []) or [None])[0],
                   # 询问模式（快速/深度）—— 弹框顶部会据此显示不同提示
                   "ask_mode": str(cfg.get("ask_mode") or "quick"),
                   "session": session,
                   # ★ 用户这一轮的**原话**：画图闸门要靠它判断"风格/画幅说了没有"
                   #   （不能拿模型写的英文 prompt 反推 —— 它会自己补上 photorealistic）
                   "user_text": last_user,
                   # ★ 云端图片 Key / DeepSeek Key：由前端放请求头带上来，
                   #   只在本轮工具调用期间用，不落盘、不进日志（见 cloud_image 的约定）
                   "image_key": x_image_key,
                   # 前端 🎨 里选的模型名（不同 Key/分组能用的模型不一样，必须可换）
                   "image_model": x_image_model,
                   "ds_key": _ds_key(x_deepseek_key),
                   # 危险操作（删文件、起进程、联网…）先问用户，批准了再执行
                   # ask：材料不足时弹出问答框向用户追问细节
                   **_dict_ui_channel(_make_ui_channel(loop, live_ui))}

            # 先把本轮所有工具调用解析出来，并逐个通知前端"开始执行"
            calls = []
            for tc in tool_calls:
                fn = (tc.get("function") or {})
                name = fn.get("name", "")
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                if not isinstance(args, dict):
                    args = {}
                # 文本工具名/参数 → 真实工具（save_file 其实就是 library 的 write 动作）
                name, args = _map_text_tool_args(name, args)
                calls.append((name, args))
                yield json.dumps({"tool_start": {"name": name, "args": args}}) + "\n"

            # ---------- 并行执行本轮工具 ----------
            # 模型经常一轮里同时要好几样东西（查知识库 + 联网搜索 + 看时间…）。
            # 串行执行时总耗时是它们**相加**；并行后只取决于最慢的那个，
            # 对"多工具协同"的任务体感差别很明显。
            # 每个工具用**独立的 ui_events 列表**，避免并发写入互相污染，
            # 最后再按原顺序合并，保证前端看到的次序稳定。
            async def _run_one(idx, name, args):
                ev = []
                # 诊断：把"每个工具执行那一刻的当前项目"打出来。
                # 排查"AI 建完项目、文件却落进上一个项目"时必须看这个 ——
                # 光看工具调用顺序是看不出来的，关键是**执行时**的 active_project。
                if name in _SERIAL_TOOLS:
                    logger.warning("[ws] 执行 %s 前 active_project=%s args=%s",
                                   name, workspace.active_project(),
                                   json.dumps(args, ensure_ascii=False)[:160])
                try:
                    # dispatch 里都是阻塞逻辑（网络/文件/推理），必须丢线程池，
                    # 否则会占住事件循环、把流式输出又憋成"一次性返回"。
                    res = await asyncio.to_thread(tools.dispatch, name, args, ev,
                                                  dict(ctx))
                except Exception as e:
                    res = f"[工具执行失败] {name}：{e}"
                # ★ 画完图后**后台把对话模型请回显存**（不阻塞这里）。
                #   画图前 t2i 会主动卸载对话模型（12GB 卡装不下 SDXL + 30B），
                #   而 30B 冷启动要 ~20 秒 —— 不请回来的话，用户画完图接着聊
                #   就得干等 20 秒（2026-09-27 用户反馈"多轮对话每次都要等模型启动"）。
                #   预热跑在后台线程里，用户看图这几秒模型就加载好了。
                #   没画过图时它是空操作（见 t2i.rewarm_async）。
                try:
                    t2i.rewarm_async()
                except Exception:
                    pass
                if name in _SERIAL_TOOLS:
                    logger.warning("[ws] 执行 %s 后 active_project=%s 结果=%s",
                                   name, workspace.active_project(),
                                   str(res)[:90])
                return idx, name, res, ev

            # ⚠️⚠️ **有"状态相关"工具时，整轮必须按顺序串行执行。**
            #
            # 踩过的坑（用户报的"AI 建完项目，里面却是空的"）：
            # 模型在同一轮里既发 `workspace_new_project`（建项目并切进去）
            # 又发 `workspace_write`（往**当前**项目写文件），而工具是
            # `asyncio.gather` **同时开跑**的 —— 写文件可能在切项目**之前**
            # 就读到了旧的 `active_project`，文件于是落进了**上一个项目**。
            # 注意：`gather` 只保证"**结果**按原顺序合并给前端"，
            # **副作用早就以任意顺序发生了** —— 这是最容易看走眼的地方。
            #
            # 所以：本轮只要出现会改共享状态、或依赖"当前项目"的工具，
            # 就整轮串行（顺序＝模型给出的顺序，前后依赖才成立）；
            # 纯读类的（查知识库 / 联网 / 看时间…）仍然并行，不损失速度。
            _SERIAL_TOOLS = {
                # ⚠️ ask_user 也在这里：它是**阻塞等用户答复**的（弹框）。
                # 如果和别的工具并行跑，"写文件/跑代码"可能在用户回答**之前**就执行了，
                # 那问来的答案就白问了。串行保证"先问、后做"的顺序。
                "ask_user",
                "workspace_new_project", "workspace_use_project",
                "workspace_write", "workspace_mkdir", "workspace_delete",
                "workspace_move", "workspace_run", "run_python",
                "save_file", "library", "github_push",
            }
            _serial = any(n in _SERIAL_TOOLS for n, _a in calls)
            if _serial and len(calls) > 1:
                yield json.dumps({"note": "本轮有写文件/切项目这类操作，按顺序执行"}) + "\n"

            async def _run_serial():
                out = []
                for _i, (_n, _a) in enumerate(calls):
                    out.append(await _run_one(_i, _n, _a))
                return out

            if len(calls) > 1 and not _serial:
                yield json.dumps({"tool_parallel": len(calls)}) + "\n"
            # ⚠️ 不能用裸的 `await asyncio.gather(...)`：
            # 那样要等**所有工具跑完**才有机会往外吐东西，
            # 而"询问用户是否继续"的弹窗必须立刻出现在界面上（工具那会儿正阻塞等着答复）。
            # 所以改成边等工具、边把实时事件推给前端。
            _runner = (_run_serial() if _serial else asyncio.gather(
                *[_run_one(i, n, a) for i, (n, a) in enumerate(calls)]))
            _tool_task = asyncio.ensure_future(_runner)
            while not _tool_task.done():
                try:
                    _evt = await asyncio.wait_for(live_ui.get(), timeout=0.2)
                except asyncio.TimeoutError:
                    continue
                yield json.dumps({"ui": _evt}) + "\n"
            gathered = await _tool_task
            while not live_ui.empty():      # 收尾：把队列里剩下的也吐出去
                yield json.dumps({"ui": live_ui.get_nowait()}) + "\n"
            text_results = []
            for _idx, name, result, ev in sorted(gathered, key=lambda x: x[0]):
                used_tools.add(name)
                # 记进「工具动作账本」：这是"刚才改了什么"唯一的可靠来源
                try:
                    _args = calls[_idx][1] if 0 <= _idx < len(calls) else {}
                    actlog.record(session, name, _args,
                                  ok=not str(result).startswith("[工具执行失败]"),
                                  result=result)
                except Exception:
                    pass
                # 把本次新产生的图片登记下来，后续工具（如保存到图库）可按序号引用
                for e in ev:
                    if e.get("type") == "image":
                        ctx["shown_images"].append(e)
                        # ⚠️⚠️ 生成/微改出来的图**也要记成"最近一张图"**：
                        #    以前只有"用户自己拖进来的图"才会被 `_remember_image` 记住，
                        #    所以用户接着说「微改**刚才那张**」时 `_recent_image()` 是空的，
                        #    edit_image 只能回「无法确定要修改的图片」——
                        #    这就是 2026-09-22 用户报的"模型找不到刚才生成的图"。
                        #    记进来之后，下一轮只要用户明确指向它（_refers_to_prev_image）
                        #    就能直接拿来当底图，不用重新拖一次。
                        if e.get("b64") and e.get("origin") in ("gen", "edit"):
                            _remember_image([e["b64"]])
                ui_events.extend(ev)
                if text_protocol:
                    # 文本协议没有 tool_call_id 可关联。实测用 user 消息 +
                    # 「工具结果：」前缀，模型能正确接着改（探针里全部收敛）。
                    # 一轮可能有多个调用 → 先攒起来，最后合成**一条**消息发回去
                    # （连着发多条 user 消息会让对话模板看着很怪）。
                    text_results.append("【%s】\n%s" % (name, result))
                else:
                    # 注意：Ollama 的 tool 消息用 tool_name 关联调用，
                    # 不是 tool_calls/tool_call_id，否则模型读不到工具返回内容
                    # （会误答"没查到/无法联网"）。
                    working.append({"role": "tool", "content": result, "tool_name": name})
            if text_protocol and text_results:
                working.append({"role": "user",
                                "content": "工具结果：\n" + "\n\n".join(text_results)})
            # 2.5) 【完整性兜底】把"只被预览过、没被正式写入"的残file撤掉。
            #
            # 为什么必须有这一步：流式落盘是**边生成边写**的预览，模型输出一旦
            # 被截断（或它压根没走 workspace_write 这个工具），磁盘上就只剩**半截文件**。
            # 而界面上看起来"AI 正在写"，用户会以为成功了 ——
            # 实测就踩到：生成出来的 todo.py 停在 `print(f'{index}. {task[`，
            # 一跑就 SyntaxError。**半成品冒充成品，比"什么都没写"更糟。**
            # 所以：新文件 → 删掉；老文件 → 还原成本轮之前的内容。
            # 每轮都对一次账：更新"正式写入落在哪个项目"，并清掉跨项目的孤儿预览。
            # 撤销残缺文件留到整次生成结束时做（见下面的 final=True）。
            for _n in _reconcile_streamed(_wsstate, ui_events):
                yield json.dumps({"note": _n}) + "\n"

            # 3) 把前端副作用事件透出
            for ui in ui_events:
                yield json.dumps({"ui": ui}) + "\n"

        else:
            # ★★ for 没被 break 打断 = **工具轮次用满**，任务没收尾就退场了。
            #   2026-09-27 端到端实测定位：这是"任务跑到一半就不动了、
            #   最后只看到「⚠️ 模型这次没有输出内容」"的主要来源之一。
            #   写代码 / 做项目这类活天生是"一轮写一个文件 + 一轮跑一次"，
            #   很容易吃满 MAX_TOOL_ROUNDS 轮；而循环一结束就**静默**退场 ——
            #   模型那边一个字都没交代，前端只能兜一句通用的"没有输出内容"。
            #   ⇒ 这里补一个**收尾轮**：不带工具，要它用文字把进度交代清楚。
            #     产物都已落盘、会话历史也在，用户回一句「继续」就能接着做。
            logger.warning("[rounds] 工具轮次用满 %d 轮，转入收尾总结", _max_rounds)
            yield json.dumps({"note": (
                "已经连续用了 %d 轮工具（单轮上限，可在 config 里改 max_tool_rounds），"
                "我让它先把已完成的部分交代清楚。想接着做，回一句「继续」就行。"
                % _max_rounds)}) + "\n"
            try:
                _wrap_params = dict(gen_params)
                _wrap_params["max_tokens"] = min(
                    4096, int(_wrap_params.get("max_tokens") or 4096))
                working.append({"role": "user", "content": (
                    "（系统提示）本轮的工具调用轮次已达上限，现在不能再调用任何工具。"
                    "请**只用文字**向用户交代：① 已经完成了什么；② 产出在哪个文件或项目里；"
                    "③ 还差什么没做完；④ 建议的下一步。"
                    "不要写「我将要…」，直接把现状总结出来。")})
                _wrap = client.chat(working, model=model, stream=True,
                                    params=_wrap_params, tools=None)
                async for _raw in _stream_lines(_wrap, session):
                    if not _raw:
                        continue
                    if isinstance(_raw, bytes):
                        _raw = _raw.decode("utf-8", "replace")
                    _line = _raw.strip()
                    if not _line:
                        continue
                    try:
                        _o = json.loads(_line)
                    except Exception:
                        continue
                    _mm = _o.get("message") or {}
                    if _mm.get("thinking"):
                        final_thinking += _mm["thinking"]
                        yield json.dumps({"message": {"thinking": _mm["thinking"]}}) + "\n"
                    if _mm.get("content"):
                        final_text += _mm["content"]
                        yield json.dumps({"message": {"content": _mm["content"]}}) + "\n"
                    if _o.get("done"):
                        break
            except Exception:
                logger.warning("[rounds] 收尾总结失败（已完成的产物不受影响）",
                               exc_info=True)
            if not final_text.strip():
                # 连收尾都没写出来 → 至少要给一句**能操作**的话，别让用户对着空白
                yield json.dumps({"note": (
                    "工具轮次已用满，这一次也没能写出总结。"
                    "**已完成的文件都还在**：直接回一句「继续」，它会接着往下做。")}) + "\n"

        # 整次生成结束 —— **这时候才**收拾"只被预览过、从没正式写入"的残缺文件。
        # 为什么不能每轮收：模型经常"这一轮只预览、下一轮才正式写"，每轮收会误伤
        # （上一版就是这么把一个刚建好的项目目录清空的）。
        for _n in _reconcile_streamed(_wsstate, [], final=True):
            yield json.dumps({"note": _n}) + "\n"

        # 对话结束：归档历史会话（供记忆检索）+ 保存会话文件（供重启后恢复）
        # **必须用 all_messages（完整历史）**，不能用裁剪后的 messages，
        # 否则每轮都会把早期对话从磁盘上抹掉（见上面的注释）。
        full = all_messages + [{"role": "assistant", "content": final_text}]
        try:
            memory.save_transcript(session, full)
        except Exception:
            pass
        try:
            sessions.save_messages(session, full)
            sessions.prune(session)       # 过长则自动裁剪，避免记录无限膨胀
            # ★ 2026-11-09 用户反馈「重启后总是忘记刚刚做过的事」：
            #   以前交接卡只在「点重启」那一刻快照，崩溃/直接关窗口就一条不留。
            #   这里每轮结束就把最近几轮原话实时落盘（内部有 3 秒节流，开销可忽略）。
            try:
                from . import handoff as _handoff_live
                _handoff_live.touch(session, "对话进行中（实时落盘）")
            except Exception:
                pass
        except Exception:
            pass

        # ---- 兜底：用户要了文件，模型却只是"嘴上说存好了" ----
        # 实测会这样：回答里写「已将本文写入生成文库，保存为 xxx.md」，
        # 但文库目录是空的 —— 它压根没调工具。用户以为存好了，这种"虚假完成"最坑。
        if (_wants_file(last_user) and "library" not in used_tools
                and len(final_text.strip()) > 200):
            # 带上用户原话：正文是代码块时，要用他点名的文件名（"就叫 stats.py"）
            _saved = _autosave_answer(final_text, last_user)
            if _saved:
                yield json.dumps({"ui": {"type": "library", "act": "write",
                                         "rel": _saved, "auto": True}}) + "\n"
                yield json.dumps({"note": (
                    "模型没有真的执行保存，我已把正文自动存进生成文库：%s" % _saved)}) + "\n"

        # ---- 幻觉产物：正文里声称的下载链接，文件其实不存在 ----
        # 比"没保存"更坑：它给了个**看起来很真的链接和页数**，用户点开才发现是空的。
        # 如实说清，并给出下一步（让它用真正的生成工具重做）。
        _fake = _fake_doclib_links(final_text)
        if _fake:
            logger.warning("[fake-artifact] 正文声称的产物并不存在：%s", _fake[:3])
            yield json.dumps({"note": (
                "⚠️ 这些文件**其实没有生成**（只是写在正文里）：%s\n"
                "想真正拿到文件，直接说一句「用工具生成」，"
                "例如「用 make_pptx 做一份 XX 的 PPT」。"
                % "、".join(_fake[:3]))}) + "\n"

        # 自动记忆：把本轮要点提炼进记忆（后台，且**等用户停手再做**，不跟聊天抢显卡）。
        # ---- 收尾兜底：这次回答是「说一半就断」的，必须明确告诉用户 ----
        # 续写两次仍被截断、或已经没有轮次可续时，前端只会看到一个半截气泡 ——
        # 用户根本分不清是「答完了」还是「断了」。这里补一句人话（2026-11-08）。
        try:
            _last_cut = bool(locals().get("truncated")) and not locals().get("tool_calls")
        except Exception:
            _last_cut = False
        if _last_cut:
            _hint = ("\n\n（⚠️ 这条回答在长度上限处被截断了，**不是答完了** —— "
                     "回一句「继续」我就接着往下写。）")
            final_text += _hint
            yield json.dumps({"message": {"content": _hint}}) + "\n"
        # 每轮都安排，覆盖最近几轮内容；命中信号词（尤其"我做过/参加过"这类经历）则立即做。
        # 有了这层沉淀，久远的聊天记录才能安全清理。
        if cfg.get("auto_memorize", True):
            try:
                # ⚠️⚠️ 提炼**固定用默认模型**，不要跟着本轮路由走。
                # 这里原来传的是 `model`（本轮路由后的模型），后果实测：
                # 只要这一轮被判成代码任务（`_route_code_model` 切到 qwen2.5-coder:14b），
                # 提炼就也用 14B 代码模型 —— 而它**根本不做这类抽取**：
                #     同一条 prompt，qwen3-vl:8b → 56.8 秒，正确给出 3 条要点；
                #     qwen2.5-coder:14b → 10.1 秒，只回一个「无」字，**0 条**。
                # 表现就是"聊了代码之后，那几轮的内容一条都没记进去"，
                # 而且**没有任何报错**（返回了非空文本，解析后 kept=0）。
                # 另外 14B 模型 12GB 显存装不下，`ollama ps` 实测 26% 跑在 CPU 上，就算它能干也慢。
                _mem_model = cfg.get("default_model") or model
                _schedule_memory_extract(session, full[-_MEM_WINDOW_TURNS:], _mem_model, cfg,
                                         urgent=_looks_memorable(last_user))
            except Exception:
                pass
        yield json.dumps({"done": True, "text": final_text, "thinking": final_thinking}, ensure_ascii=False) + "\n"

    # 用 _track_chat 包一层：登记"有聊天在跑"，后台记忆提炼会主动让路（见 _chat_busy）
    return StreamingResponse(_track_chat(gen()), media_type="application/x-ndjson")


# ================== DeepSeek 纯对话（流式）==================
# 为什么单独开一路，不塞进 /api/chat：
#   /api/chat 里全是工具循环 / 记忆 / RAG / 图片压缩，DeepSeek 是纯文本对话 API，
#   没有函数调用能力接本地工具，硬走进去要加一堆 if 分支，容易碰坏原有逻辑。
#
# ⚠️ Key 安全（改这里之前先读）：
#   · 从前端 X-DeepSeek-Key 请求头取，**不进 URL、不进 body**
#   · 不写 config.json、不写日志文件、不留全局缓存
#   · 所有错误信息过 deepseek_client.scrub()，把 sk-xxx 打码成 sk-***
@app.get("/api/deepseek/models")
def deepseek_models():
    """前端下拉用：可选模型 + 官方链接（充值 / 建 Key / 查用量）。"""
    return {
        "ok": True,
        "models": deepseek_client.list_models(),
        "links": {
            "topup": "https://platform.deepseek.com/top_up",
            "keys": "https://platform.deepseek.com/api_keys",
            "usage": "https://platform.deepseek.com/usage",
        },
    }


@app.post("/api/deepseek/check")
def deepseek_check(x_deepseek_key: str = Header(default="")):
    """校验 Key 并顺便查余额（GET /user/balance）。Key 只在本次请求用。"""
    return deepseek_client.check_key(_ds_key(x_deepseek_key))


@app.post("/api/image/check")
def image_check(x_image_key: str = Header(default="")):
    """校验云端图片 Key（GET /models，不花钱）。Key 只在本次请求用。"""
    return cloud_image.check_key(x_image_key, config.load_config() or {})


@app.get("/api/image/models")
def image_models_list():
    """图片模型总清单：云端（当前配置的）+ 本地（含是否已装、占用多少）。"""
    cfg = config.load_config() or {}
    return {"ok": True,
            "backend": str(cfg.get("image_backend") or "cloud"),
            "cloud": {
                "model": str(cfg.get("image_api_model") or cloud_image.DEFAULT_MODEL),
                "model_edit": str(cfg.get("image_api_model_edit") or ""),
                "base": str(cfg.get("image_api_base") or cloud_image.DEFAULT_BASE),
                "default": cloud_image.DEFAULT_MODEL,
            },
            "local": image_models.list_local(),
            "status": image_models.status(),
            "models_dir": image_models.base_dir()}


@app.post("/api/image/select")
def image_select(body: dict):
    """切换绘图模型。

    body = {"backend": "cloud"|"local", "model": "xxx", "role": "gen"|"inpaint"}
      · cloud：model 就是服务商的模型 id（不同 Key/分组能用的不一样）
      · local：model 是注册表里的本地 id（必须已下载）
    """
    b = body or {}
    backend = str(b.get("backend") or "cloud").lower()
    model = str(b.get("model") or "").strip()
    role = str(b.get("role") or "gen").lower()
    cfg = config.load_config() or {}
    if backend == "local":
        r = image_models.apply_local(model)
        if not r.get("ok"):
            return r
        cfg = config.load_config() or {}
        cfg["image_backend"] = "local"
        config.save_config(cfg)
        return {"ok": True, "backend": "local", "model": model, "msg": r.get("msg")}
    cfg["image_backend"] = "cloud"
    if not model:
        # 只切后端、不换模型名（前端顶栏下拉就是这么用的）
        config.save_config(cfg)
        return {"ok": True, "backend": "cloud",
                "model": str(cfg.get("image_api_model") or ""),
                "msg": "已切到云端绘图"}
    if role == "inpaint":
        cfg["image_api_model_edit"] = model
    else:
        cfg["image_api_model"] = model
    config.save_config(cfg)
    return {"ok": True, "backend": "cloud", "model": model, "role": role,
            "msg": "已切到云端 " + model}


@app.post("/api/image/pull")
def image_pull(body: dict):
    return image_models.pull(str((body or {}).get("id") or ""))


@app.post("/api/image/remove")
def image_remove(body: dict):
    return image_models.remove(str((body or {}).get("id") or ""))


@app.post("/api/image/cancel")
def image_cancel():
    return image_models.cancel()


@app.get("/api/image/status")
def image_status():
    return image_models.status()


# ---------------- DeepSeek Key 状态探针 ----------------
# 用户问「我明明填了 Key」时，服务端没法验证（Key 从不落盘）。
# 这个接口只收**布尔值**（有没有 Key），不接收 Key 本身 ——
# 用来给用户/我确认"浏览器的 localStorage 里到底有没有"。
_DS_SEEN = {"has_key": None, "at": ""}


# ---------------- 本机保管 Key（2026-11-09）----------------
# 目的只有一个：**无人值守的定时任务**也能用云端模型。
# 用户在前端点「保存并校验」时顺手存一份到本机（data/secrets.json，权限 600）。
# 对外接口一律只回布尔，绝不回显 Key 本身。
@app.get("/api/secret/status")
def secret_status():
    from . import keyvault
    return keyvault.status()


@app.post("/api/secret/save")
def secret_save(body: dict):
    from . import keyvault
    return keyvault.set_key(str((body or {}).get("key") or ""))


@app.post("/api/secret/clear")
def secret_clear():
    from . import keyvault
    return keyvault.clear()


# ---------------- 本机保管 Key（2026-11-09）----------------
# 目的只有一个：**无人值守的定时任务**也能用云端模型。
# 用户在前端点「保存并校验」时顺手存一份到本机（data/secrets.json，权限 600）。
# 对外接口一律只回布尔，绝不回显 Key 本身。
@app.get("/api/secret/status")
def secret_status():
    from . import keyvault
    return keyvault.status()


@app.post("/api/secret/save")
def secret_save(body: dict):
    from . import keyvault
    return keyvault.set_key(str((body or {}).get("key") or ""))


@app.post("/api/secret/clear")
def secret_clear():
    from . import keyvault
    return keyvault.clear()


# ---------------- 本机保管 Key（2026-11-09）----------------
# 目的只有一个：**无人值守的定时任务**也能用云端模型。
# 用户在前端点「保存并校验」时顺手存一份到本机（data/secrets.json，权限 600）。
# 对外接口一律只回布尔，绝不回显 Key 本身。
@app.get("/api/secret/status")
def secret_status():
    from . import keyvault
    return keyvault.status()


@app.post("/api/secret/save")
def secret_save(body: dict):
    from . import keyvault
    return keyvault.set_key(str((body or {}).get("key") or ""))


@app.post("/api/secret/clear")
def secret_clear():
    from . import keyvault
    return keyvault.clear()


@app.get("/api/ds/env")
def ds_env():
    """服务器有没有配好 Key（只返回布尔，不回传 Key 本身）。"""
    return {"ok": True, "has_key": bool(_env_ds_key())}


@app.post("/api/ds/seen")
def ds_seen(body: dict):
    _DS_SEEN["has_key"] = bool((body or {}).get("has_key"))
    _DS_SEEN["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return {"ok": True, **_DS_SEEN}


@app.get("/api/ds/seen")
def ds_seen_get():
    return {"ok": True, **_DS_SEEN}


# ---------------- 代码模型选择 ----------------
@app.get("/api/code/models")
def code_models():
    """可选的代码模型清单 + 当前选择 + 本机已装（本地项要标灰用）。"""
    cfg = config.load_config() or {}
    try:
        have = _installed_models()
    except Exception:
        have = []
    return {"ok": True,
            "current": str(cfg.get("code_model_choice") or "auto"),
            "choices": tools.CODE_MODEL_CHOICES,
            "installed": have,
            "has_ds_key": None}     # Key 在前端，前端自己判断


@app.post("/api/code/model")
def code_model_set(body: dict):
    """切换代码模型（代码生成 + 自我迭代都跟着变）。"""
    m = str((body or {}).get("model") or "").strip()
    if not m:
        return {"ok": False, "msg": "模型不能为空"}
    valid = {c["id"] for c in tools.CODE_MODEL_CHOICES}
    if m not in valid:
        return {"ok": False, "msg": "未知的代码模型：" + m}
    cfg = config.load_config() or {}
    cfg["code_model_choice"] = m
    config.save_config(cfg)
    return {"ok": True, "model": m, "msg": "代码模型已切换"}


# ---------------- 完全访问 / 性能档位 / 版本 diff ----------------
@app.get("/api/selfdev/fullaccess")
def fullaccess_get():
    cfg = config.load_config() or {}
    return {"ok": True, "on": bool(cfg.get("self_dev_full_access"))}


@app.post("/api/selfdev/fullaccess")
def fullaccess_set(body: dict):
    """开关「完全访问」：关着只能改 frontend/，开着才能改 backend/*.py。"""
    on = bool((body or {}).get("on"))
    cfg = config.load_config() or {}
    cfg["self_dev_full_access"] = on
    config.save_config(cfg)
    selfdev.journal("fullaccess", "-", on=on)
    return {"ok": True, "on": on,
            "msg": ("已开启完全访问：模型可以改 backend/*.py 等核心代码"
                    if on else "已关闭完全访问：只能改 frontend/ 与新建文件")}


@app.post("/api/perf/cloud")
def perf_cloud(x_deepseek_key: str = Header(default="")):
    """用浏览器里的 Key 读云模型真实参数（型号/上下文/输出/模态/推理档）。"""
    return perf.detect_cloud(_ds_key(x_deepseek_key))


@app.get("/api/perf")
def perf_get():
    return {"ok": True, **perf.get()}


@app.post("/api/perf")
def perf_set(body: dict):
    b = body or {}
    try:
        ctx = int(b.get("num_ctx")) if b.get("num_ctx") is not None else -1
    except Exception:
        ctx = -1
    try:
        mt = int(b.get("max_tokens") or 0)
    except Exception:
        mt = 0
    return perf.set(str(b.get("profile") or ""), num_ctx=ctx, max_tokens=mt)


# ---------------- 上下文占用环（2026-11-11）----------------
# 前端底栏那个小圆环要显示「这次对话用掉了多少上下文」。
# ⚠️ 不能只按历史字数估：**固定开销（系统提示 + 工具定义）就有 3 万 token 量级**，
#    比多数对话的历史还大。只算历史的话圆环永远是绿的，等它变红已经顶爆了。
# 固定开销缓存 60 秒 —— 重建一次系统提示要读记忆/技能文件，没必要每轮都读。
_CTX_OVH = {"t": 0.0, "tokens": 0, "parts": {}}


def _ctx_overhead(session: str = "") -> dict:
    """系统提示 + 工具定义占多少 token（缓存 60 秒）。"""
    now = time.time()
    if _CTX_OVH["tokens"] and (now - _CTX_OVH["t"]) < 60:
        return _CTX_OVH
    cfg = config.load_config() or {}
    try:
        sp = _SystemPrompt.build("", session)
    except Exception:
        logger.warning("上下文环：系统提示构建失败，按 0 计", exc_info=True)
        sp = ""
    try:
        sp += _ask_rules(cfg)
    except Exception:
        pass
    try:
        import json as _json
        sch = tools.make_schemas(web_enabled=bool(cfg.get("web_enabled")),
                                 kb_enabled=bool(cfg.get("rag_enabled")))
        tools_t = _est_tokens(_json.dumps(sch, ensure_ascii=False))
    except Exception:
        logger.warning("上下文环：工具定义统计失败，按 0 计", exc_info=True)
        tools_t = 0
    prompt_t = _est_tokens(sp)
    _CTX_OVH.update({"t": now, "tokens": prompt_t + tools_t,
                     "parts": {"prompt": prompt_t, "tools": tools_t}})
    return _CTX_OVH


def _ctx_limit(num_ctx=0) -> int:
    try:
        v = int(num_ctx or 0)
    except Exception:
        v = 0
    if v > 0:
        return v
    # 分母必须跟**实际生效**的窗口一致：perf 会把 num_ctx_override 和档位一起
    # 算进 num_ctx，而 config["num_ctx"] 只是档位默认值、不含手动覆盖。
    # 直接读后者会让圆环按 40960 算，实际窗口却是 154624 —— 显示成 126% 假爆表。
    try:
        # 前端圆环每次消息变化都会打这个接口 —— 绝对不能再带模型探测（见 perf.get 注释）
        n = int((perf.get(with_detect=False) or {}).get("num_ctx") or 0)
        if n > 0:
            return n
    except Exception:                                # noqa: BLE001
        pass
    cfg = config.load_config() or {}
    return int(cfg.get("num_ctx") or 8192)


def _ctx_used(messages, session: str = "") -> dict:
    ov = _ctx_overhead(session)
    hist = 0
    for m in (messages or []):
        if isinstance(m, dict):
            hist += _est_tokens(m.get("content") or "")
    return {"overhead": int(ov["tokens"]),
            "prompt": int(ov["parts"].get("prompt", 0)),
            "tools": int(ov["parts"].get("tools", 0)),
            "history": hist, "used": int(ov["tokens"]) + hist}


@app.post("/api/context/usage")
def context_usage(body: dict):
    """返回当前上下文的占用情况（前端圆环用）。

    前端每次换模型 / 改上下文长度都会重新调它 —— 所以 limit 一定按**当前配置**算，
    不缓存 limit（只缓存固定开销）。
    """
    b = body or {}
    limit = _ctx_limit(b.get("num_ctx"))
    u = _ctx_used(b.get("messages"), str(b.get("session") or ""))
    ratio = (u["used"] / limit) if limit > 0 else 0.0
    return {"ok": True, "limit": limit, "ratio": round(ratio, 4),
            "pct": round(ratio * 100, 1),
            "free": max(0, limit - u["used"]), **u}


@app.post("/api/context/compact")
def context_compact(body: dict):
    """把上下文压到目标比例以内：**保留最近的消息，较早的压成要点摘要**。

    为什么不是直接丢：用户回头问「开头聊了什么」时模型必须还答得上来 ——
    摘要就是「压缩但不失忆」的保证（和主通道 _trim_history_to_budget 同一套做法）。
    返回压缩后的消息数组，前端直接替换自己的 history。
    """
    b = body or {}
    msgs = [m for m in (b.get("messages") or []) if isinstance(m, dict)]
    limit = _ctx_limit(b.get("num_ctx"))
    try:
        target = float(b.get("target") or 0.6)
    except Exception:
        target = 0.6
    target = min(0.9, max(0.2, target))
    u = _ctx_used(msgs, str(b.get("session") or ""))
    budget = int(limit * target) - u["overhead"]
    # ⚠️ 固定开销（系统提示 + 工具定义）本身就可能超过目标预算 ——
    #    实测 4 万窗口下开销 2.86 万，60% 目标只有 2.46 万，差值为负。
    #    这时**仍然要压**（至少把历史压到最近几条），否则圆环永远降不下来。
    if budget <= 512:
        budget = 512
    if len(msgs) <= 2:
        return {"ok": True, "changed": False, "messages": msgs, "dropped": 0,
                "limit": limit, "reason": "历史太短，无需压缩"}
    kept, used = [], 0
    for m in reversed(msgs):
        t = _est_tokens(m.get("content") or "")
        if kept and (used + t) > budget:
            break
        kept.append(m)
        used += t
    kept.reverse()
    dropped = msgs[: len(msgs) - len(kept)]
    digest = _history_digest(dropped)
    out = list(kept)
    if digest:
        # 摘要并进**第一条**（若是 user 就合并，避免出现两条连续 user）
        if out and out[0].get("role") == "user":
            out[0] = {"role": "user",
                      "content": digest + "\n\n" + str(out[0].get("content") or "")}
        else:
            out.insert(0, {"role": "user", "content": digest})
    return {"ok": True, "changed": bool(digest) or len(out) != len(msgs),
            "messages": out, "dropped": len(dropped), "kept": len(kept),
            "digest_chars": len(digest), "limit": limit}


@app.get("/api/selfdev/diff")
def selfdev_diff(batch: str = "", rel: str = ""):
    """给版本历史用：某批次里某个文件「备份 vs 现在」的差异。"""
    if not batch or not rel:
        return {"ok": False, "error": "需要 batch 和 rel"}
    items = [x for x in selfdev.list_backups(600)
             if x["batch"] == batch and x["rel"] == rel]
    if not items:
        return {"ok": False, "error": "找不到这个备份"}
    bak = items[0]["path"]
    try:
        before = open(bak, "r", encoding="utf-8", errors="replace").read()
    except OSError as e:
        return {"ok": False, "error": "读备份失败：%s" % e}
    cur_path = os.path.join(selfdev.project_root(), rel)
    try:
        after = open(cur_path, "r", encoding="utf-8", errors="replace").read()
    except OSError:
        after = ""
    d = selfdev.make_diff(before, after, rel)
    return {"ok": True, **d, "rel": rel, "batch": batch}


# ---------------- 自开发（改自己的源码）----------------
# ---------- 「新版本已就绪」：改动落盘后不自动重启，等用户点确认 ----------
# 用户明确要求：模型改完源码**不许自己重启**（会打断对话、用户不知情），
# 只能在前端弹提示，等用户按下「立即重启」才真正重启；不按就继续跑旧代码。
@app.get("/api/selfdev/pending")
def selfdev_pending():
    """前端轮询：有没有改完还没生效的新版本。"""
    from . import pending as _pend
    return {"ok": True, **_pend.status()}


@app.post("/api/selfdev/apply_restart")
def selfdev_apply_restart():
    """用户点了「立即重启」才走到这里 —— 唯一真正重启的入口。"""
    try:
        from . import handoff as _hdy
        _hdy.snapshot(str(globals().get("_LAST_SESSION") or ""), "用户点了「立即重启」")
    except Exception:
        logger.warning("重启前写交接卡失败（不影响重启）", exc_info=True)
    # 重启前自动打一次版本快照（用户要求：改完能一键回到上一版）
    _version_autosnapshot("用户点了「立即重启」")
    from . import pending as _pend
    v = selfdev.verify_import()
    if not v.get("ok"):
        return {"ok": False, "error": v.get("error") or v.get("msg")
                or ("导入自检没通过：%s" % str(v)[:200])}
    _pend.clear()
    return selfdev.restart()


@app.post("/api/selfdev/dismiss_pending")
def selfdev_dismiss_pending():
    """用户点了「稍后」：先不重启，把提示收起来（改动仍然没生效）。"""
    from . import pending as _pend
    return {"ok": True, **_pend.clear()}

@app.get("/api/selfdev/status")
def selfdev_status():
    cfg = config.load_config() or {}
    return {"locked": selfdev.locked(),
            "ok": True,
            "enabled": bool(cfg.get("self_dev_enabled")),
            "root": selfdev.project_root(),
            "mirror": selfdev.mirror_dir(),
            "journal": selfdev.recent_journal(20)}




@app.post("/api/selfdev/toggle")
def selfdev_toggle(body: dict):
    """打开/关闭自开发。开关状态写进 config，重启后仍生效。"""
    on = bool((body or {}).get("on"))
    cfg = config.load_config() or {}
    cfg["self_dev_enabled"] = on
    config.save_config(cfg)
    selfdev.journal("toggle", "-", on=on)
    return {"ok": True, "enabled": on,
            "msg": ("自开发已打开：模型现在能读写本项目源码并重启应用"
                    if on else "自开发已关闭")}


@app.post("/api/selfdev/verify")
def selfdev_verify():
    """试导入一次 backend.main（改完代码后可以先点这个自检）。"""
    return selfdev.verify_import()


@app.post("/api/selfdev/restart")
def selfdev_restart():
    """重启应用（内部会先试导入，导入失败就拒绝）。"""
    # ⚠️ 用户明确要求：模型改完源码**不许自己重启**（会打断对话、用户不知情）。
    #    这个接口现在只做「自检 + 打标记」，真正重启只走 /api/selfdev/apply_restart。
    from . import pending as _pend
    _v = selfdev.verify_import()
    try:
        from . import handoff as _hdz
        _hdz.snapshot(str(globals().get("_LAST_SESSION") or ""), "模型请求重启")
    except Exception:
        logger.warning("重启前写交接卡失败（不影响重启）", exc_info=True)
    _version_autosnapshot("模型请求重启")
    if not _v.get("ok"):
        return {"ok": False, "error": _v.get("error") or _v.get("msg")
                or ("导入自检没通过：%s" % str(_v)[:200])}
    _st = _pend.mark("", "模型请求重启，等待用户确认")
    return {"ok": True, "pending": True, "restart": False,
            "files": _st.get("files") or [],
            "msg": "已标记新版本就绪，等待用户在前端点「立即重启」"}
    return selfdev.restart()


@app.post("/api/selfdev/demo")
def selfdev_demo(x_deepseek_key: str = Header(default="")):
    """用**当前选定的代码模型**做一次无害的自我迭代演示。

    为什么加它：用户想验证「DeepSeek 能不能自己改这个应用」，但 DeepSeek 的 Key
    只存在浏览器里（服务端从来不存）。所以由前端带上 Key 调到这个接口 ——
    既不需要把 Key 交给我，又能真跑一遍"模型写代码 → 护栏校验 → 落盘"。

    演示只写一个 `dev_note.md`（说明文件），**不碰任何源码**，零风险。
    """
    from . import tools as _T
    cfg = config.load_config() or {}
    ctx = {"ds_key": _ds_key(x_deepseek_key), "images": [], "last_image": None,
           "mask": None, "shown_images": [], "ask_mode": "quick",
           "session": "", "user_text": "", "image_key": ""}
    kind, pick = _T.code_model_pick(cfg, ctx)
    who = ("DeepSeek " + pick) if kind == "cloud" else ("本地 " + (pick or "默认"))
    # ⚠️ 用户**明确选了云端**但没 Key 时，不要静默退回本地 ——
    #   那样他会以为"DeepSeek 迭代成功了"，其实跑的是本地小模型。
    _choice = str(cfg.get("code_model_choice") or "")
    if _choice.startswith("deepseek") and not x_deepseek_key:
        return {"ok": False, "who": "DeepSeek " + _choice,
                "error": "你选的是 " + _choice + "，但没填 DeepSeek Key。"
                         "点顶栏 🔑 DeepSeek 填一次（只存浏览器）再点这个按钮。"}
    instr = ("写一份简短的 Markdown 说明：这个应用刚完成一次自我迭代演示。"
             "包含：① 这次用了哪个模型 ② 迭代能力包含哪些工具 ③ 一句话结论。"
             "要求 15 行以内，用 Markdown 标题和列表。")
    try:
        code = _T._code_gen_text(instr, cfg, ctx, max_tokens=1200)
    except Exception as e:                            # noqa: BLE001
        return {"ok": False, "who": who, "error": "%s: %s" % (type(e).__name__, e)}
    text = _T._strip_code_fence(code or "").strip()
    if not text:
        return {"ok": False, "who": who, "error": "模型没有输出内容"}
    r = selfdev.write("dev_note.md", text, note="自我迭代演示")
    if not r.get("ok"):
        return {"ok": False, "who": who, "error": r.get("error")}
    return {"ok": True, "who": who, "rel": "dev_note.md",
            "chars": r.get("chars"), "preview": text[:300],
            "path": r.get("path")}


@app.get("/api/selfdev/backups")
def selfdev_backups():
    """历史改动快照：按**迭代批次**聚合，带时间与"这次改了什么"。

    前端 🛠 面板里显示「版本更新内容 + 日期」，就靠这里的 ts/notes/added/removed。
    """
    return {"ok": True,
            "batches": selfdev.list_batches(20),
            "files": selfdev.list_backups(80),
            "mirror": selfdev.mirror_dir(),
            "restore": os.path.join(selfdev.mirror_dir(), "restore.py")}


@app.post("/api/selfdev/rollback_batch")
def selfdev_rollback_batch(body: dict):
    """整批回撤（回到某一次迭代之前）。"""
    batch = str((body or {}).get("batch") or "").strip()
    if not batch:
        return {"ok": False, "error": "缺少 batch"}
    r = selfdev.rollback_batch(batch)
    if isinstance(r, dict) and r.get("ok"):
        try:
            from . import pending as _pend
            _pend.mark("", "回撤到「%s」之前" % batch)
        except Exception:
            logger.warning("批次回撤后标记待生效失败", exc_info=True)
    return r


@app.post("/api/selfdev/rollback")
def selfdev_rollback(body: dict):
    try:
        return selfdev.rollback(str((body or {}).get("rel") or ""),
                                str((body or {}).get("backup") or ""))
    except Exception as e:                            # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


# ---------------- 对话最后选用的模型（定时任务继承用） ----------------
@app.get("/api/pref/chat_model")
def pref_chat_model_get():
    return {"ok": True, "model": scheduler.get_chat_model()}


@app.post("/api/pref/chat_model")
def pref_chat_model_set(body: dict):
    return scheduler.set_chat_model(str((body or {}).get("model") or ""))


# ---------------- 定时 / 长期自动化任务 ----------------
@app.get("/api/schedule")
def schedule_list():
    return {"ok": True, "jobs": scheduler.list_jobs(),
            "status": scheduler.status()}


@app.get("/api/schedule/result")
def schedule_result(id: str = "", limit: int = 0):
    """某个定时任务**最近一次运行**的完整结果正文（只读）。

    2026-11-11：用户反馈「定时任务看不到结果在哪」—— 面板和 list_schedules
    只给 last_preview（前 200 字），正文在日志里。这里把它整段取出来，
    供前端那个「结果查看窗」显示。

    2026-11-13 用户要求「不要少字」：默认 limit=0 = **整篇返回，不截断**。
    原来默认 20000 字符、上限 60000，一篇长报告（尤其带表格的）会被切在半句上。
    """
    jid = str(id or "").strip()
    if not jid:
        return {"ok": False, "error": "缺少任务 id"}
    try:
        n = int(limit or 0)
    except Exception:
        n = 0
    # 0 / 负数 = 全文；显式要截断时才封顶，避免误传一个天文数字
    n = 0 if n <= 0 else max(500, min(n, 2000000))
    return {"ok": True, "id": jid, "text": scheduler.latest_result(jid, limit=n)}


@app.get("/api/schedule/search")
def schedule_search(q: str = "", top_k: int = 50):
    """在定时任务里检索（名称 / 任务内容 / 上次结果）。

    2026-11-13 用户要求：「定时任务内容也要支持检索，最好向量化提高召回率」。
    检索本身在 scheduler.search_jobs 里（语义向量 + 字符 bigram 覆盖率），
    这里只做参数校验和转发。backend 字段如实告诉前端用的是哪种方式。
    """
    try:
        k = max(1, min(int(top_k or 50), 200))
    except Exception:
        k = 50
    try:
        return scheduler.search_jobs(str(q or ""), top_k=k)
    except Exception as e:                            # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e),
                "items": [], "backend": "error"}


@app.post("/api/schedule")
def schedule_add(body: dict):
    b = body or {}
    return scheduler.add(
        str(b.get("name") or ""),
        str(b.get("prompt") or ""),
        every_minutes=int(b.get("every_minutes") or 0),
        at_time=str(b.get("at_time") or ""),
        session=str(b.get("session") or ""),
        model=str(b.get("model") or ""))


@app.post("/api/schedule/toggle")
def schedule_toggle(body: dict):
    b = body or {}
    return scheduler.update(str(b.get("id") or ""),
                            enabled=bool(b.get("enabled")))


@app.post("/api/schedule/run")
def schedule_run(body: dict):
    """立即跑一次（会真的调模型 + 工具，可能要一两分钟）。"""
    return scheduler.run_now(str((body or {}).get("id") or ""))


@app.post("/api/schedule/delete")
def schedule_delete(body: dict):
    return scheduler.remove(str((body or {}).get("id") or ""))


@app.post("/api/image/discover")
def image_discover(x_image_key: str = Header(default="")):
    """用这个 Key 问服务商**哪些模型能用**，并挑出可能能画图的候选。

    🎨 面板的「自动选择可用模型」就是靠它 —— 用户充了钱还是 503 时，
    说明分组里没有那个模型的通道，这时候唯一出路是换模型。
    """
    cfg = config.load_config() or {}
    names = cloud_image.list_models(x_image_key, cfg)
    cands = cloud_image.image_candidates(x_image_key, cfg)
    # 诊断用：**不记 Key**，只记"能拿到几个模型、当前选的那个在不在里面"。
    # 用户报「充了钱还是 503」时，这一行就能区分是"分组没权限"还是"模型名写错"。
    try:
        cur = str(cfg.get("image_api_model") or "")
        logger.info("[image] discover: 共 %d 个模型 %s；图片候选 %s；当前「%s」%s",
                    len(names), names[:12], cands[:6] or "无", cur,
                    "在清单里" if cur in cands else "不在清单里")
    except Exception:
        pass
    msg = "拿不到模型清单（Key 无效 / 网络不通）"
    if names:
        if cands:
            msg = "共 %d 个模型，其中 %d 个能画图" % (len(names), len(cands))
        else:
            msg = ("你这个 Key 只能用 %d 个模型（%s），**里面没有任何图片模型** —— "
                   "这是分组权限问题，充钱解决不了" %
                   (len(names), "、".join(names[:5])))
    return {"ok": bool(names),
            "count": len(names),
            "current": str(cfg.get("image_api_model") or ""),
            "candidates": cands,
            "models": names[:20],
            "msg": msg}


@app.get("/api/image/meta")
def image_meta():
    """给前端显示当前用的是哪个图片服务/模型（不含 Key）。"""
    cfg = config.load_config() or {}
    return {"ok": True,
            "base": cfg.get("image_api_base") or cloud_image.DEFAULT_BASE,
            "model": cfg.get("image_api_model") or cloud_image.DEFAULT_MODEL}


# DeepSeek 专用的精简工具集（2026-11-08 实测定位）
# ---------------------------------------------------------------
# 问题：这台机器访问 api.deepseek.com 时被解析到腾讯 EdgeOne CDN，
#       它对 **大 POST 请求有概率性拦截**（返回 550 / Server: TencentEdgeOne）。
#       实测成功率（同一请求体连发 8 次）：
#           276 字节 → 100%    17KB → 87.5%
#           29KB     → 75%     65KB → 0%
#       所以把工具集压到 ~18KB（15 个核心工具 + 描述截断到 60 字），
#       再配合 client 侧的 550 重试（3 次），成功率可达 99%+。
# 代价：DeepSeek 通道不能调用全部 39 个工具，只给最常用的一批。
#       本地 30B/8B 通道不受影响（走 Ollama，没有 CDN）。
_DS_CORE_TOOLS = {
    "web_search", "web_read", "get_time", "remember", "search_memory",
    "read_file", "write_file", "list_directory",
    "generate_image", "edit_image", "web_image_search", "save_image_to_library",
    "make_pptx", "make_docx", "ask_user",
}

# 能"真产出图片"的工具
_IMG_MAKER_TOOLS = {"generate_image", "edit_image", "compose_images",
                    "cutout_image", "composite_image"}
# 「这句话是在派活，不是在闲聊/提问」——用来决定要不要强制走深度询问
_TASK_VERBS = (
    "帮我", "给我", "替我", "做个", "做一个", "做成", "做一份", "弄个", "出个",
    "生成", "写一个", "写个", "写份", "来一", "来张", "来段",
    "画一", "画个", "画张", "出图", "绘图", "配图", "配个",
    "改", "修改", "调整", "补充", "换成", "换个", "去掉", "删掉",
    "设计", "整理", "制作", "翻译", "总结", "分析", "优化", "重构", "美化",
    "排版", "简化", "加个", "加一个", "加上", "做个表", "做表", "做成表",
    "ppt", "PPT", "文档", "表格",
)


def _looks_like_task(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 3:
        return False
    return any(v in t for v in _TASK_VERBS)


_IMG_HINTS = ("画一张", "画个", "画幅", "画张", "生成一张", "生成图片", "生成一张图",
              "帮我画", "出图", "来张图", "来一张图", "做一张图", "配图", "画出来")


def _ds_guard_factory(last_user: str):
    """DeepSeek 通道的「光嘴上说说」兜底（2026-11-08 新增）。

    为什么需要：主通道（/api/chat）早就有代码层兜底 —— 模型把内容写在正文里
    就算完事时，退回一轮明确要求它去生成（见 `_OFFICE_MAKER_TOOLS` 那段）。
    DeepSeek 通道走的是另一套循环（deepseek_client.chat_stream_with_tools），
    **原来完全没有这道保险**，所以用户实测「让 DeepSeek 做个 Excel，
    它把表格写在正文里，一个文件都没有」。

    返回 None 表示不用兜；返回字符串则作为下一轮的用户消息推回去（只兜一次）。
    """
    office = _is_office_task(last_user) and not _is_code_task(last_user)
    want_file = _wants_file(last_user)
    want_img = any(k in (last_user or "") for k in _IMG_HINTS)

    def guard(used: set, text: str):
        if office and not (used & _OFFICE_MAKER_TOOLS):
            return (
                "（系统提示）你刚才只把内容写在回复里了，**但用户要的是一个能点开的文件**。"
                "现在**立即调用生成工具把它做出来**：演示稿 → make_pptx；"
                "文档 → make_docx；表格 → make_xlsx。"
                "⚠️ 单个工具调用的内容不要超过 4 页/4 项（超长 JSON 会被写坏、整轮全废）—— "
                "先做封面 + 2~3 项，剩下的用 edit_office 每次补 1~2 项，分几次调用。"
                "不要复述内容、不要再问用户、也不要解释，直接开始生成。")
        if want_img and not (used & _IMG_MAKER_TOOLS):
            return (
                "（系统提示）用户要的是**真实产出的图片**，不是你用文字描述画面。"
                "现在**立即调用工具**：要 AI 创作 → generate_image（prompt 用英文描述）；"
                "要现成的真实照片 → web_image_search。做完再用一句话说明。")
        if want_file and not used:
            return (
                "（系统提示）用户要的是**落到文件上的成品**。现在立即调用对应工具"
                "（write_file / make_docx / make_xlsx / make_pptx）把文件真正写出来，"
                "再告诉用户文件路径。不要只在回复里贴内容。")
        return None

    return guard


def _ds_tools(cfg: dict) -> list:
    """DeepSeek 用的工具集（2026-11-08 恢复全量）。

    中间过程：一开始因为 EdgeOne CDN 拦大包，只给 15 个核心工具（18KB）。
    后来逐个 IP 实测发现 —— **只有极个别 CDN 节点是坏的**（124.225.27.128），
    其余 7 个节点对 65KB 请求体都正常。在 socket 层过滤坏 IP 后
    （见 deepseek_client._DS_BAD_IPS），39 个全量工具连发 10 次成功率 90%；
    配合 client 侧 3 次 550 重试 → 99.9%。
    所以这里恢复全量，和本地通道给一样的工具。
    """
    return tools.make_schemas(
        web_enabled=bool(cfg.get("web_enabled")),
        kb_enabled=bool(cfg.get("rag_enabled")),
        code_exec=bool(cfg.get("code_exec_enabled")),
        office_gen=True,
        ask_mode=str(cfg.get("ask_mode") or "quick"),
    )


def _content_to_text(c) -> str:
    """把消息的 content 压成**纯文本**：多模态数组只留文字，丢掉 base64 图片。

    ⚠️⚠️ 2026-11-11 修（这是「第一句话要等 8 秒」的真正根因）：
      云端通道落盘时原来写的是 `str(m.get("content") or "")`。
      而带图时 content 是一个列表 `[{type:text}, {type:image_url, data:base64…}]`，
      `str()` 会把它变成 **Python repr 字符串**（单引号那种），于是整张图的
      base64 以纯文本形式**永久写进会话历史**，之后每一轮请求都当普通文字
      重新发给模型。

      实测数据：一个 46 条消息的会话里只有 3 条被污染，却占了 55.4 万字
      （≈18 万 token），把首字延迟从 1.5 秒拖到 8 秒，而且**越用越慢**。

      历史里的旧图片对后续对话没有任何价值，只需要保留用户当时打的字。
      清掉之后：554,004 字 → 15,056 字，省 97.3%。
    """
    if isinstance(c, str):
        # 自愈：历史里可能已经躺着被旧代码 str() 成 repr 的多模态内容
        # （形如 "[{'type': 'text', …}, {'type': 'image_url', …}]"）。
        # 认出来就还原成纯文本，避免它被原样再存一遍、继续每轮重发。
        s = c.lstrip()
        if s.startswith('[{') and "'type'" in s[:200]:
            try:
                import ast as _ast
                parsed = _ast.literal_eval(s)
                if isinstance(parsed, list):
                    return _content_to_text(parsed)
            except Exception:
                pass
        return c
    if isinstance(c, list):
        out, imgs = [], 0
        for p in c:
            if not isinstance(p, dict):
                continue
            t = p.get("type")
            if t == "text":
                s = str(p.get("text") or "").strip()
                if s:
                    out.append(s)
            elif t in ("image_url", "image"):
                imgs += 1
        txt = "\n".join(out)
        if imgs:
            txt = ((txt + "\n\n") if txt else "") + (
                "（本轮附了 %d 张图片；图片已从历史中移除以节省上下文）" % imgs)
        return txt
    return str(c or "")


@app.post("/api/chat/deepseek")
async def chat_deepseek(req: ChatRequest,
                        x_deepseek_key: str = Header(default=""),
                        x_image_key: str = Header(default=""),
                        x_image_model: str = Header(default="")):
    """DeepSeek 流式对话（**带工具调用**）。输出格式与 /api/chat 一致。

    2026-11-08 补：原来是纯对话，用户反馈「接入 API 的模型无法调度其他工具」。
    DeepSeek 官方 API 支持 function calling（OpenAI 兼容协议），这里补上完整循环：

        发请求(带 tools) → 模型返回 tool_calls → 本地执行 → 结果回填 → 继续
        直到模型不再调工具，最多 8 轮。

    为什么用「独立线程 + queue」而不是直接 async：
        tools.dispatch 内部全是阻塞逻辑（网络/文件/推理），
        在 async 生成器里直接调会占住事件循环，把流式输出又憋成"一次性返回"。
        所以整条同步生成器跑在 worker 线程里，增量通过 queue 传回。
        —— 和主通道 /api/chat 的做法一致（那边用 asyncio.to_thread 逐个工具）。

    支持模型：deepseek-flash（V4.1 Flash，默认不思考）
              / deepseek-flash-think（同模型开 thinking，effort=medium）
              / deepseek-v4-pro（V4 Pro，thinking + effort=high）
    """
    _AUTO_OK["on"] = False          # 同上：云通道也一轮一清
    _AUTO_OK["hits"] = 0
    x_deepseek_key = _ds_key(x_deepseek_key)      # 没带 Key 就用服务器环境变量
    model = req.model or "deepseek-flash"
    messages = req.messages or []
    session = req.session_id or ""

    # 只保留 role/content（Ollama 的额外字段 DeepSeek 不认）。
    # ⚠️ 但 **图片是认的** —— 2026-11-08 之前这里把 images 一起丢了，
    #    所以用户拖图问云端模型，模型回答"我看不到图片"。
    #    实测 DeepSeek 接受 image_url（content 用数组形式），见 vision_chat()。
    _imgs = [x for x in (req.images_b64 or []) if isinstance(x, str) and len(x) > 100]
    _img_ok = model in ("deepseek-flash", "deepseek-flash-think")   # v4-pro 只支持文本

    # ★ 2026-11-13：**图片跨轮可见**（用户报「图片只能当轮使用，下一轮就查不到」）。
    #   云端通道原来两处都没存：前端 history 只有文字、后端只挂本轮。
    #   这里补上：本轮图落盘 + 记台账，再把之前轮次的图按哈希挂回对应消息。
    _attach_paths: list = []
    _hist_imgs: list = []
    try:
        if _imgs:
            from . import img_fetch as _imgf
            _attach_paths = _imgf.save_chat_images(_imgs)
        _last_u = next((str(m.get("content") or "") for m in reversed(messages)
                        if str(m.get("role") or "") == "user"), "")
        if _attach_paths:
            chatimg.record(session, _last_u, _attach_paths)
        _hist_imgs = chatimg.attach(messages, session)
    except Exception:
        logger.warning("云端通道图片台账失败（本轮按无图继续）", exc_info=True)

    # 只保留 role/content（Ollama 的额外字段 DeepSeek 不认）；
    # 但 **图片是认的** —— 历史消息带 images 时转成 OpenAI 多模态数组。
    clean: list[dict] = []
    for m in messages:
        content = m.get("content")
        if content is None:
            continue
        _role = str(m.get("role") or "user")
        _mi = [x for x in (m.get("images") or []) if isinstance(x, str) and len(x) > 100]
        if _mi and _img_ok:
            _hp = [{"type": "text", "text": str(content)}]
            for b64 in _mi[:4]:                       # 单条最多 4 张
                _hp.append({"type": "image_url",
                            "image_url": {"url": "data:image/png;base64," + b64}})
            clean.append({"role": _role, "content": _hp})
        else:
            clean.append({"role": _role, "content": str(content)})

    # 把本轮图片挂到最后一条 user 消息上（数组 content，OpenAI 多模态格式）
    if _imgs and _img_ok and clean:
        for i in range(len(clean) - 1, -1, -1):
            if clean[i].get("role") == "user":
                _c = clean[i].get("content")
                if isinstance(_c, list):
                    break                             # 已经是数组（理论上不会，attach 跳过本轮）
                _parts = [{"type": "text", "text": str(_c or "")}]
                for b64 in _imgs[:4]:                 # 一次最多 4 张，别把额度烧光
                    _parts.append({"type": "image_url",
                                   "image_url": {"url": "data:image/png;base64," + b64}})
                clean[i]["content"] = _parts
                logger.info("云端通道附带 %d 张图片（model=%s）", len(_parts) - 1, model)
                break
    elif _imgs and not _img_ok:
        logger.warning("当前云模型 %s 不支持图片输入，已忽略 %d 张图", model, len(_imgs))

    last_user = next((str(m.get("content") or "") for m in reversed(clean)
                      if m.get("role") == "user"), "")

    cfg = config.load_config()
    temp = float(cfg.get("temperature", 0.7) or 0.7)

    # DeepSeek 用精简工具集（压到 ~18KB，绕开 EdgeOne 对 30KB+ POST 的拦截）
    tool_schemas = _ds_tools(cfg)
    # 复用主通道的系统提示（工具规则 / 记忆 / 时间指引都在里面）
    sys_prompt = _SystemPrompt.build(last_user, session)
    # ⚠️⚠️ 2026-11-08 补：漏了这句 —— 「信息不足就先问」这一整段规则原来只在
    # 主通道（/api/chat）里 `sys_prompt += _ask_rules(cfg)`，DeepSeek 通道没加。
    # 症状：用户让 DeepSeek「做个 PPT / 画张图」，它不调 ask_user 直接瞎猜着做。
    # 工具在（41 个里有 ask_user），但模型不知道**什么时候该用**。
    sys_prompt += _ask_rules(cfg)

    # ★ 2026-11-09：云端通道原来**完全没接**交接卡和工具账本 —— 用户用 DeepSeek 时
    #   问「刚刚改了什么」，模型手里一点证据都没有（只会答"没改任何东西"）。
    #   和主通道一样挂在**最后一条用户消息**上（不动 system，保住前缀缓存）。
    try:
        from . import handoff as _handoff2
        _ctx2 = []
        _h2 = _handoff2.render(session)
        if _h2:
            _ctx2.append(_h2)
        _a2 = actlog.render(session)
        if _a2:
            _ctx2.append(_a2)
        if _ctx2 and clean:
            for _i in range(len(clean) - 1, -1, -1):
                if clean[_i].get("role") == "user":
                    _c = clean[_i].get("content")
                    _blk = "\n\n" + "\n\n".join(_ctx2)
                    if isinstance(_c, str):
                        clean[_i]["content"] = _c + _blk
                    elif isinstance(_c, list):      # 带图那轮 content 是数组
                        clean[_i]["content"] = _c + [{"type": "text", "text": _blk}]
                    break
    except Exception:
        logger.warning("云端通道注入交接卡/账本失败（本轮忽略）", exc_info=True)

        # ★ 2026-11-10：云端通道同样注入「最近 8 轮速览」。
        #   云端虽然不裁历史，但模型在长历史里做关键词对齐同样会抓错，
        #   和主通道用同一套锚点，用户换模型时体验才一致。
        try:
            _r2 = _recent_turns_index(req.messages)
            if _r2 and clean:
                for _i in range(len(clean) - 1, -1, -1):
                    if clean[_i].get("role") == "user":
                        _c = clean[_i].get("content")
                        if isinstance(_c, str):
                            clean[_i]["content"] = _c + "\n\n" + _r2
                        elif isinstance(_c, list):      # 带图那轮 content 是数组
                            clean[_i]["content"] = _c + [{"type": "text", "text": "\n\n" + _r2}]
                        break
        except Exception:
            logger.warning("云端通道注入最近轮次速览失败（本轮忽略）", exc_info=True)

    # ★ 2026-11-10：云端通道也接「手动技能」—— 用户在前端点选的技能，
    #   两条通道（本地 / DeepSeek）行为必须一致，否则换个模型就"技能失灵"。
    try:
        from . import skills as _skills_ds
        _sk_ds = _skills_ds.consume_manual(session)
        if _sk_ds and clean:
            for _i in range(len(clean) - 1, -1, -1):
                if clean[_i].get("role") == "user":
                    _c = clean[_i].get("content")
                    if isinstance(_c, str):
                        clean[_i]["content"] = _c + "\n\n" + _sk_ds
                    elif isinstance(_c, list):
                        clean[_i]["content"] = _c + [{"type": "text", "text": "\n\n" + _sk_ds}]
                    break
    except Exception:
        logger.warning("云端通道注入手动技能失败（本轮忽略）", exc_info=True)

    loop = asyncio.get_running_loop()
    _ACC: list = []          # 本轮完整正文（收尾回填用，见 __end.answer）

    async def gen():
        if not x_deepseek_key:
            yield json.dumps({"error": "未填写 DeepSeek API Key（点右上角 🔑 填入）"}) + "\n"
            yield json.dumps({"__end": True}) + "\n"
            return

        q: queue.Queue = queue.Queue()

        class _UIAdapter:
            """把 UI 事件直接塞进主队列 —— _make_ui_channel 只要求 put_nowait。

            ⚠️ 为什么不用独立的 asyncio.Queue：
                原来 gen 循环用 `wait_for(run_in_executor(None, q.get), timeout=0.1)`
                轮询，每次超时都会**泄漏一个仍阻塞在 q.get 上的线程任务**。
                多个任务竞争同一个队列 → 消息被某个已超时丢弃的任务吃掉。
                实测症状：假 Key 调用只返回 "正在调用…"，401 报错完全看不到。
                改成「单队列 + 阻塞读」后彻底消失。
            """
            __slots__ = ()

            def put_nowait(self, msg):
                q.put(("ui", msg))

        def worker() -> None:
            ui_events: list = []                 # tools.dispatch 往里追加 UI 事件
            try:
                ctx = {
                    # DeepSeek 是纯文本协议：不给图片类字段
                    "images": [], "last_image": None, "mask": None,
                    "shown_images": [],
                    "ask_mode": str(cfg.get("ask_mode") or "quick"),
                    "session": session,
                    # 画图闸门用（见 tools._img_ask_gate）：判断用户原话里说没说风格/画幅
                    "user_text": last_user,
                    # 云端图片 Key + DeepSeek Key（本轮生效，不落盘）
                    "image_key": x_image_key,
                    "image_model": x_image_model,
                    "ds_key": x_deepseek_key,
                    # 问用户 / 危险操作确认 两条通道（复用主通道的实现）
                    **_dict_ui_channel(_make_ui_channel(loop, _UIAdapter())),
                }

                def executor(name: str, args: dict) -> str:
                    ui_events.clear()
                    try:
                        _r = tools.dispatch(name, args, ui_events, ctx)
                    except Exception as _e:
                        actlog.record(session, name, args, ok=False, result=str(_e))
                        raise
                    actlog.record(session, name, args, ok=True, result=_r)
                    return _r

                for kind, val in deepseek_client.chat_stream_with_tools(
                        clean, model, x_deepseek_key, tool_schemas, executor,
                        temperature=min(1.3, max(0.0, temp)),
                        # 采样参数跟本地通道保持一致（之前 top_p 完全没传）
                        top_p=min(1.0, max(0.01, float(cfg.get("top_p") or 0.95))),
                        # ★ 2026-11-08 修：**原来没传 max_tokens**，用的是函数默认 4096。
                        #   开了 thinking 之后推理 token 也吃这个额度 → 正文被截在半句
                        #   （用户报「思考过程有了，但输出被限制了」）。
                        #   现在跟随全局配置（16384），并配 client 侧的截断续写。
                        max_tokens=max(int(cfg.get("max_tokens") or 8192),
                                       (perf.get(with_detect=False) or {}).get("max_tokens", 0)),
                        system_prompt=sys_prompt,
                        # 轮次上限跟主通道用同一份配置（用户要求放到最多）
                        max_rounds=max(1, min(int(cfg.get("max_tool_rounds") or MAX_TOOL_ROUNDS), 500)),
                        # ★ 「光嘴上说说」兜底：要文件/图片却只写正文时退回一轮
                        guard=_ds_guard_factory(last_user)):
                    # 先把上一轮工具产生的 UI 事件排出去
                    while ui_events:
                        q.put(("ui", ui_events.pop(0)))
                    q.put((kind, val))
                while ui_events:
                    q.put(("ui", ui_events.pop(0)))
            except Exception as e:               # noqa: BLE001
                q.put(("error", f"{type(e).__name__}: {e}"))
            finally:
                q.put(("__done__", None))

        threading.Thread(target=worker, daemon=True).start()

        yield json.dumps({"note": f"正在调用 {model}…"}) + "\n"

        # 单队列顺序消费：内容 / 思考 / 工具事件 / UI 事件 / 错误 都走这里。
        # 不用超时轮询（会泄漏 run_in_executor 任务、导致消息丢失），
        # 直接阻塞读 —— worker 结束时一定会 put ("__done__", None) 把这里放出来。
        while True:
            # 2026-11-09：长工具（跑代码/爬网页）会让这里**静默几分钟**，
            #   nginx 60s 就掐连接、前端也以为卡死。每 15 秒发一个心跳包。
            def _qget():
                try:
                    return q.get(timeout=15)
                except queue.Empty:
                    return ("__ping__", None)
            kind, val = await loop.run_in_executor(None, _qget)
            if kind == "__ping__":
                yield json.dumps({"ping": 1}) + "\n"
                continue
            if kind == "__done__":
                break
            if kind == "content":
                _ACC.append(val)                     # 全量正文，收尾时回填给前端
                yield json.dumps({"message": {"content": val}}) + "\n"
            elif kind == "thinking":
                yield json.dumps({"message": {"thinking": val}}) + "\n"
            elif kind == "tool_start":
                # 前端据此弹工具卡片
                yield json.dumps({"tool_start": {"name": val}}) + "\n"
            elif kind == "tool_result":
                pass          # 结果交给模型总结，前端不需要原文
            elif kind == "note":
                yield json.dumps({"note": val}) + "\n"
            elif kind == "ui":
                yield json.dumps({"ui": val}) + "\n"
            elif kind == "error":
                yield json.dumps({"error": deepseek_client.scrub(val)}) + "\n"

        # ★ 2026-11-09：把**完整正文**随结束事件一起发下去。
        #   之前只发 {"__end": true}，一旦前端在接收环节漏了最后几个分片
        #   （切在半个 JSON 上、XHR 缓冲吞尾等），界面上就永远少那几个字。
        #   现在前端以此为准做一次回填 —— 丢字问题不依赖"猜哪里丢的"。
        _full = "".join(_ACC)
        # ★ 服务端兜底补句（2026-11-09）：前端那些兜底都依赖"先收到"，但如果模型自己
        #   在 max_tokens 边界停住、或最后一轮带工具调用导致不续写，正文就是半句。
        #   这里直接再问一次"只补缺失的结尾"，把结果接上去 —— 不依赖客户端做任何事。
        try:
            _cut = (len(_full.strip()) > 60 and
                    not re.search(r"[。！？!?….…”』」）)】`~\n]$", _full.rstrip()))
        except Exception:
            _cut = False
        # 宁多勿少：最多补 3 轮，直到结尾落在终止标点上（用户明确要求）
        for _try in range(3):
            try:
                _bad_tail = (len(_full.strip()) > 60 and
                             not re.search(r"[。！？!?….…”』」）)】`~\n]$", _full.rstrip()))
            except Exception:
                _bad_tail = False
            if not _bad_tail:
                break
            try:
                _msgs = [
                    {"role": "system", "content": "你只负责补完句子。只输出缺失的那几个字，不要重复已有内容，不要解释。"},
                    {"role": "user", "content": "下面这段回答被截断了，请只输出**接着往下写的那一小段**（补完这句话即可，15~60 字）：\n\n" + _full[-400:]},
                ]
                _buf = []
                for _k, _v in deepseek_client.chat_stream(
                        _msgs, model=model, api_key=x_deepseek_key,
                        temperature=0.3, max_tokens=1024):
                    if _k == "content" and _v:
                        _buf.append(_v)
                _add = "".join(_buf).strip()
                # ⚠️ 补句请求自己也可能被截断，那时 chat_stream 会吐一句
                #   "（输出达到 max_tokens 上限被截断）" —— 绝不能把它当正文拼进去
                if "上限被截断" in _add:
                    logger.warning("补句结果本身就是截断提示，丢弃：%r", _add[:60])
                    break
                if not _add or len(_add) > 400:
                    break
                yield json.dumps({"message": {"content": _add}}, ensure_ascii=False) + "\n"
                _ACC.append(_add)
                _full = "".join(_ACC)
            except Exception:
                logger.warning("补句失败（第 %d 次，忽略）", _try + 1, exc_info=True)
                break
        # ★ 服务端落盘（2026-11-10）：把**服务端自己生成的完整正文**写进会话，
        #   不等前端 PUT。这样即使前端丢片、渲染出错，磁盘上也有一份完整原文，
        #   前端可以回读它来纠正显示（见 app.js 的 __reconcileFromServer）。
        try:
            if session:
                # ⚠️⚠️ 2026-11-11 修：必须用 **messages（原始请求）** 落盘，
                #   不能用 clean —— clean 里已经被追加了「工具动作账本 / 最近几轮速览 /
                #   长期记忆」等注入块。用 clean 落盘会把它们**永久写进会话历史**，
                #   下次打开时前端把整块当成用户说的话渲染成一个巨大的蓝气泡
                #   （用户反馈「手机端有些变形」就是这么来的），而且每轮还会重复发给模型。
                #   本地通道一直是对的（all_messages 在注入之前就取好了），只有云端漏了。
                _saved = [{"role": str(m.get("role") or "user"),
                           "content": _content_to_text(m.get("content"))} for m in messages]
                _saved.append({"role": "assistant", "content": _full})
                sessions.save_messages(session, _saved)
                try:
                    memory.save_transcript(session, _saved)
                except Exception:
                    pass
        except Exception:
            logger.warning("云端通道落盘失败（不影响显示）", exc_info=True)
        yield json.dumps({"__end": True, "answer": _full}, ensure_ascii=False) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.post("/api/tool/confirm")
def tool_confirm(req: ToolConfirmRequest):
    """用户在弹窗里点了「允许」/「拒绝」。

    工具线程正阻塞等待这个结果，所以这里必须**立即**返回，不能有任何耗时操作。
    """
    with _ui_lock:
        item = _ui_pending.get(req.id)
    if not item:
        raise HTTPException(status_code=404,
                            detail="这个确认请求已经失效或超时了，请让模型重新发起")
    item["allow"] = bool(req.allow)
    item["event"].set()
    if req.remember and req.allow:
        _AUTO_OK["on"] = True
        logger.info("[confirm] 用户勾选「本次任务不再询问」，本轮后续自动批准")
    return {"ok": True, "allow": bool(req.allow),
            "auto": bool(_AUTO_OK["on"])}


@app.post("/api/tool/answer")
def tool_answer(req: ToolAnswerRequest):
    """问答框的回答（模型问细节时用）。answers 可以直接是字符串列表。"""
    with _ui_lock:
        item = _ui_pending.get(req.id)
    if not item:
        raise HTTPException(status_code=404,
                            detail="这个提问已经失效或超时了，请让模型重新发起")
    item["answers"] = [dict(a) if isinstance(a, dict) else {"answer": str(a)}
                       for a in (req.answers or [])]
    item["event"].set()
    return {"ok": True, "count": len(item["answers"])}


# =====================================================================
#  生成文库（模型产出物；与只读的知识库分开）
# =====================================================================
# =====================================================================
#  开发工作区（人机协同开发 / vibecoding 的载体）
# =====================================================================
# 和「生成文库」的分工：文库放**成品**（一份一份归档），
# 工作区放**开发中的项目**（多文件、反复读改写、要能跑）。
# 前端把它渲染成「文件树 + 真编辑器(Monaco) + 运行/保存」的开发台。
@app.get("/api/ws/tree")
def ws_tree():
    return workspace.tree()


@app.get("/api/ws/file")
def ws_read(rel: str = ""):
    try:
        return workspace.read_text(rel)
    except ValueError as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/ws/file")
def ws_write(body: dict):
    b = body or {}
    try:
        return workspace.write_text(str(b.get("rel") or ""), str(b.get("text") or ""))
    except ValueError as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/ws/new")
def ws_new(body: dict):
    b = body or {}
    rel, kind = str(b.get("rel") or "").strip(), str(b.get("kind") or "file")
    try:
        if kind == "dir":
            return workspace.mkdir(rel)
        return workspace.write_text(rel, str(b.get("text") or ""))
    except ValueError as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/ws/rename")
def ws_rename(body: dict):
    b = body or {}
    try:
        return workspace.rename(str(b.get("rel") or ""), str(b.get("to") or ""))
    except ValueError as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/ws/delete")
def ws_delete(body: dict):
    try:
        return workspace.remove(str((body or {}).get("rel") or ""))
    except ValueError as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/ws/run")
def ws_run(body: dict):
    """在**工作区里**运行一个 .py —— cwd 设成它所在目录，脚本里的相对路径才找得到文件。"""
    rel = str((body or {}).get("rel") or "").strip()
    if not rel.lower().endswith(".py"):
        return {"ok": False, "error": "当前只能直接运行 .py 文件"}
    try:
        p = workspace.abs_path(rel)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    r = tools.run_file(p, allow_risky=False)
    if r.get("needs_confirm"):
        return {"ok": False, "needs_confirm": True, "risky": r.get("risky") or [],
                "error": "检测到需要确认的操作：" + "、".join(r.get("risky") or [])}
    return {"ok": True, "rel": rel, "rc": r.get("rc"), "out": r.get("out") or "",
            "err": r.get("err") or "", "seconds": r.get("seconds")}


@app.post("/api/ws/confirm_run")
def ws_confirm_run(body: dict):
    """用户批准了风险操作 → 放行重跑。"""
    rel = str((body or {}).get("rel") or "").strip()
    try:
        p = workspace.abs_path(rel)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    r = tools.run_file(p, allow_risky=True)
    return {"ok": True, "rel": rel, "rc": r.get("rc"), "out": r.get("out") or "",
            "err": r.get("err") or "", "seconds": r.get("seconds")}


@app.get("/api/ws/raw")
def ws_raw(rel: str = ""):
    """原样返回工作区文件（HTML 预览用）。"""
    try:
        r = workspace.read_text(rel)
    except ValueError as e:
        return PlainTextResponse(str(e), status_code=400)
    if not r.get("ok"):
        return PlainTextResponse(r.get("error") or "读取失败", status_code=404)
    return PlainTextResponse(r["text"])


# ---------- 多项目：各项目完全独立，可自由切换 ----------
@app.get("/api/ws/projects")
def ws_projects():
    return {"ok": True, "active": workspace.active_project(),
            "projects": workspace.projects()}


@app.post("/api/ws/projects/new")
def ws_project_new(body: dict):
    r = workspace.create_project(str((body or {}).get("name") or ""))
    if r.get("ok"):
        r["active"] = workspace.set_active_project(r["name"])
    return r


@app.post("/api/ws/projects/use")
def ws_project_use(body: dict):
    try:
        return {"ok": True,
                "active": workspace.set_active_project(str((body or {}).get("name") or ""))}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/ws/projects/rename")
def ws_project_rename(body: dict):
    b = body or {}
    return workspace.rename_project(str(b.get("old") or ""), str(b.get("new") or ""))


@app.post("/api/ws/projects/delete")
def ws_project_delete(body: dict):
    return workspace.delete_project(str((body or {}).get("name") or ""))


# ---------- 导入代码 / 素材（上传、拖拽都走这里） ----------
@app.post("/api/ws/upload")
async def ws_upload(files: list[UploadFile] = File(...), dir: str = ""):
    """把本地文件传进当前项目。

    **按原始字节存**（图片、压缩包都是二进制，不能解码）。
    支持多选、支持拖进来一批；目录部分由前端用相对路径带过来（切掉盘符）。
    """
    try:
        sub = str(dir or "").strip().strip("/")
        if sub:
            workspace.safe_rel(sub)          # 借它做一次校验
    except ValueError:
        sub = ""
    saved, failed = [], []
    for f in files or []:
        # 浏览器在拖**整个文件夹**时会给出 "myproj/static/app.js" 这样的相对路径，
        # 要保留目录结构（"上传代码"十有八九是拖一个项目文件夹）。
        # 但**只剥掉盘符**那种第一段（C:/…），其余原样保留。
        raw = str(getattr(f, "filename", "") or "").replace("\\", "/")
        parts = [p for p in raw.split("/") if p not in ("", ".")]
        if len(parts) > 1 and re.match(r"^[A-Za-z]:$", parts[0]):
            parts = parts[1:]
        rel = "/".join(parts) if parts else workspace.norm_upload_name(raw)
        if sub:
            rel = "%s/%s" % (sub, rel)
        try:
            data = await f.read()
            r = workspace.import_bytes(rel, data)
            (saved if r.get("ok") else failed).append(
                r.get("rel") or rel if r.get("ok") else
                {"rel": rel, "error": r.get("error")})
        except Exception as e:
            failed.append({"rel": rel, "error": str(e)})
    return {"ok": True, "saved": saved, "failed": failed,
            "project": workspace.active_project()}


@app.get("/api/ws/zip")
def ws_zip(proj: str = ""):
    """把项目打包成 zip 下载（"拿走整个项目"用）。"""
    data, name = workspace.export_zip(proj)
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition":
                             'attachment; filename="%s"' % name})


# ---------- 本地部署 / 预览：为项目起一个静态服务 ----------
# 多文件前端（有 css/js/图片相对引用）用 file:// 或 iframe 单文件预览都不对，
# 起个真正的静态服务最省事，且**完全在本机**（127.0.0.1），不联网。
_serve = {"proc": None, "port": 0, "project": ""}


def _stop_serve() -> None:
    p = _serve.get("proc")
    if p is not None:
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    _serve.update({"proc": None, "port": 0, "project": ""})


def _port_free(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


@app.post("/api/ws/serve")
def ws_serve(body: dict):
    """给当前项目起静态服务，返回可访问地址。"""
    import subprocess
    b = body or {}
    proj = workspace.active_project()
    want = int(b.get("port") or 8808)
    _stop_serve()
    port = 0
    for cand in [want] + [want + i for i in range(1, 20)]:
        if _port_free(cand):
            port = cand
            break
    if not port:
        return {"ok": False, "error": "8808~8827 都被占用了，换个端口再试"}
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
            creationflags=(0x08000000 if os.name == "nt" else 0),  # CREATE_NO_WINDOW
            cwd=workspace.root(proj),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        return {"ok": False, "error": "启动失败：%s" % e}
    # 给它一点时间起来，别返回一个还没监听的地址
    import socket as _s
    for _ in range(30):
        time.sleep(0.15)
        with _s.socket(_s.AF_INET, _s.SOCK_STREAM) as sk:
            sk.settimeout(0.4)
            if sk.connect_ex(("127.0.0.1", port)) == 0:
                break
        if proc.poll() is not None:
            return {"ok": False, "error": "服务进程异常退出"}
    _serve.update({"proc": proc, "port": port, "project": proj})
    return {"ok": True, "url": "http://127.0.0.1:%d/" % port, "port": port,
            "project": proj}


@app.post("/api/ws/serve/stop")
def ws_serve_stop():
    was = _serve.get("port") or 0
    _stop_serve()
    return {"ok": True, "stopped": was}


@app.get("/api/ws/serve/status")
def ws_serve_status():
    p = _serve.get("proc")
    alive = bool(p is not None and p.poll() is None)
    return {"ok": True, "running": alive, "port": _serve.get("port") or 0,
            "project": _serve.get("project") or "",
            "url": ("http://127.0.0.1:%d/" % _serve["port"]) if alive else ""}


# ---------- 改动审阅：AI 改了什么、一键撤销 ----------
@app.get("/api/ws/changes")
def ws_changes(limit: int = 50):
    return {"ok": True, "active": workspace.active_project(),
            "changes": workspace.changes(limit)}


@app.get("/api/ws/change")
def ws_change_detail(id: str = ""):
    return workspace.change_detail(id)


@app.post("/api/ws/changes/revert")
def ws_change_revert(body: dict):
    return workspace.revert(str((body or {}).get("id") or ""))


@app.post("/api/ws/changes/clear")
def ws_changes_clear(body: dict):
    n = workspace.clear_changes(str((body or {}).get("project") or ""))
    return {"ok": True, "dropped": n}


@app.get("/api/ws/check")
def ws_check(rel: str = ""):
    """语法检查（编辑器里当场标红，不用等运行才看到报错）。"""
    return workspace.check_py(rel)


@app.post("/api/ws/git/push")
def ws_git_push(body: dict):
    """把项目上传到代码托管平台（GitHub / Gitee / 自建 Git）。

    认证走**系统里已有的 git 凭据**（SSH key / credential helper）——
    应用本身不存 token，也不经手密码。
    """
    b = body or {}
    return workspace.git_push(
        proj=str(b.get("project") or ""),
        repo=str(b.get("repo") or ""),
        message=str(b.get("message") or ""),
        branch=str(b.get("branch") or "main"))


@app.post("/api/ws/run_stream")
async def ws_run_stream(body: dict):
    """**流式**运行工作区里的 .py —— 像终端一样边跑边出字。

    为什么不用原来那个 `/api/ws/run`：它是 `subprocess.run(capture_output=True)`，
    **跑完才拿得到输出**。计时器/服务器这类长任务在界面上就是"一直正在执行"，
    而且**一旦超时被强杀，这期间打印的内容全被丢掉**（实测番茄钟跑了 25 秒，
    界面显示"（没有输出）"）。这里逐行推 NDJSON，超时也保留已产出的内容。
    不设超时 —— 要不要停由用户按「■ 停止」决定。
    """
    b = body or {}
    rid = str(b.get("id") or "")
    workspace.kill_all_runs()             # 一次只跑一个，免得进程越堆越多
    # `args`：命令行参数（界面上有个「参数」框可以填）。
    # argparse 这类工具**不给参数就什么都不做**，界面只会显示"跑完了但没有输出"，
    # 用户会以为程序坏了 —— 实测就是这么被问到的。
    # `stdin`：要预先喂给程序的标准输入（一行对应一次 input()）。
    # 不喂也没关系：运行中用户还能继续在界面上输入（见 /api/ws/run_input）。
    r = workspace.start_run(str(b.get("rel") or ""), run_id=rid,
                            args=str(b.get("args") or ""),
                            stdin_text=str(b.get("stdin") or ""))
    if not r.get("ok"):
        async def _bad():
            yield json.dumps({"t": "end", "ok": False,
                              "error": r.get("error")}, ensure_ascii=False) + "\n"
        return StreamingResponse(_bad(), media_type="application/x-ndjson")

    proc = r["proc"]

    return StreamingResponse(
        _stream_proc_ndjson(proc, rid, r.get("rel") or "", r.get("risky")),
        media_type="application/x-ndjson")


def _stream_proc_ndjson(proc, rid: str = "", rel: str = "", risky=None):
    """把一个**正在跑的子进程**的 stdout 变成 NDJSON 流（逐行边读边推）。

    ⚠️ 子进程的 stdout 是**阻塞**读的，必须丢到线程里 —— 直接在事件循环里
    读会把整个后端卡死（是"整个界面都卡住"那个级别，不是慢一点）。
    """
    async def _gen():
        q: queue.Queue = queue.Queue()

        def _pump():
            """把子进程的输出搬进队列 —— **读到多少推多少**。

            ⚠️ 不能用 readline()：它只认换行，而进度条/倒计时（`print(x, end='\\r')`）
            **一个换行都不打**，readline 会一直憋到进程结束，于是"实时输出"变成
            "跑完一次性显示"，长驻程序更是永远看不到东西（番茄钟实测就是这个症状）。
            所以绕过文本层缓冲，在字节层做「有多少读多少」+ 增量解码。
            """
            dec = codecs.getincrementaldecoder("utf-8")("replace")
            stream = getattr(proc.stdout, "buffer", proc.stdout)   # 二进制层
            try:
                # read1 = "最多读一次底层"，管道里有数据就立刻返回，不会等满
                read = getattr(stream, "read1", None) or (lambda n: stream.read(1))
                while True:
                    chunk = read(65536)
                    if not chunk:
                        break
                    # 兜底：万一拿到的是已解码的文本（不是字节），直接推
                    text = chunk if isinstance(chunk, str) else dec.decode(chunk)
                    if text:
                        q.put(text)
            except Exception:
                pass
            finally:
                try:
                    tail = dec.decode(b"", True)      # 冲掉结尾残留的半截字符
                    if tail:
                        q.put(tail)
                except Exception:
                    pass
                try:
                    proc.stdout.close()
                except Exception:
                    pass
                q.put(None)

        threading.Thread(target=_pump, daemon=True).start()
        yield json.dumps({"t": "start", "rel": rel, "id": rid,
                          "risky": risky or []}, ensure_ascii=False) + "\n"
        t0 = time.time()
        while True:
            try:
                item = q.get_nowait()
            except queue.Empty:
                # 进程结束且队列空了 → 收工
                if proc.poll() is not None and q.empty():
                    break
                await asyncio.sleep(0.03)     # 让出事件循环，保证真的是"边跑边推"
                continue
            if item is None:
                break
            yield json.dumps({"t": "out", "data": item}, ensure_ascii=False) + "\n"
        rc = proc.poll()
        if rc is None:
            try:
                rc = proc.wait(timeout=3)
            except Exception:
                rc = None
        workspace.reap_runs()
        yield json.dumps({"t": "end", "ok": True, "rc": rc,
                          "seconds": round(time.time() - t0, 2)},
                         ensure_ascii=False) + "\n"

    return _gen()


@app.post("/api/ws/run_stop")
def ws_run_stop(body: dict):
    """停掉正在跑的用户脚本。"""
    return workspace.stop_run(str((body or {}).get("id") or ""))


@app.post("/api/ws/run_input")
def ws_run_input(body: dict):
    """往正在运行的脚本送一行标准输入（程序里用 input() 时）。

    界面上：运行结果面板底部有个输入框，回车就调这里。
    """
    b = body or {}
    return workspace.send_run_input(str(b.get("id") or ""), str(b.get("data") or ""))


@app.get("/api/ws/run_status")
def ws_run_status():
    return {"ok": True, "running": workspace.running_count()}


# ---------- 用外部专业 IDE 打开项目 ----------
# ---------- 内置 VS Code（code-server）----------
@app.get("/api/ide/status")
def ide_status():
    return workspace.code_server_status()


@app.on_event("startup")
def _start_amap_watch():
    """后台**持续**盯着高德 key 还能不能用，坏了就记下来让前端报警。

    为什么要有这个：key 会因为"额度用满 / 被控制台重置 / 服务端异常"而不声不响地失效，
    而用户看到的只是"地图怎么变难用了"，根本不知道发生了什么。
    这里每 60 秒看一次（amap.check_health 自己带节流：正常 15 分钟才真调一次，
    异常时 1 分钟一次以便尽快发现恢复），前端轮询 /api/map/amap_status 拿结论。

    ⚠️ 离线模式不检测 —— 那会把"没网"误判成"key 坏了"。
    """
    def _loop():
        import time as _t
        from . import amap as _am
        while True:
            try:
                _t.sleep(60)
                if not _mt_online():
                    continue
                h = _am.check_health(online=True)
                if h.get("configured") and not h.get("ok"):
                    logger.warning("[amap] key 不可用：%s", h.get("message"))
            except Exception:
                pass                      # 看门狗自己绝不能把应用带走
    try:
        import threading as _th
        _th.Thread(target=_loop, name="amap-watch", daemon=True).start()
        logger.info("已启动高德 key 状态看门狗")
    except Exception as e:
        logger.warning("启动高德看门狗失败：%s", e)


def _mt_online() -> bool:
    try:
        from . import map_tools as _mt
        return bool(_mt.online())
    except Exception:
        return False


@app.on_event("startup")
def _reap_ide_orphans():
    """启动时先回收上次残留的 code-server（见 workspace.reap_orphan_code_server）。"""
    try:
        r = workspace.reap_orphan_code_server()
        if r.get("killed"):
            logger.info("已回收上次残留的 code-server 进程")
    except Exception as e:
        logger.warning("回收 code-server 残留进程失败：%s", e)
    # ⚠️ 2026-10-16 补：模型加载失败会留下 llama-server 孤儿，占着显存+提交内存
    #    却不在 /api/ps 里 —— 不清理的话，用户"关掉别的程序再重试"照样失败。
    #    放在启动时清一次，保证"重启应用 = 回到干净状态"。
    try:
        n = _reap_orphan_llama_servers("应用启动自检")
        if n:
            logger.info("已清理 %d 个残留的模型进程（llama-server）", n)
    except Exception as e:
        logger.warning("清理 llama-server 残留进程失败：%s", e)


@app.post("/api/ide/start")
def ide_start(body: dict):
    """给当前项目起一个**内置的真 VS Code**（code-server），返回访问地址。

    只绑 127.0.0.1 + 免密：**只有本机能连**，外网访问不到。
    """
    return workspace.start_code_server(str((body or {}).get("project") or ""))


@app.post("/api/ide/stop")
def ide_stop():
    return workspace.stop_code_server()


@app.post("/api/ide/run_in_terminal")
async def ide_run_in_terminal(body: dict):
    """在**内置 VS Code 的集成终端**里运行一个 .py —— 用户点「▶ 运行」走这条。

    为什么改成走终端：那是**真正的终端**，`input()` 天然可用、能交互、能跑任意命令；
    我们自己在后端跑、再靠界面"喂标准输入"只是权宜之计。
    （AI 自动跑代码那条路**不走这里** —— 它要拿真实输出做自我修正，见 workspace_run。）
    """
    b = body or {}
    rel = str(b.get("rel") or "").strip()
    if not rel:
        return {"ok": False, "error": "没指定要运行的文件"}
    proj = str(b.get("project") or "").strip() or workspace.active_project()
    # ① 终端在 VS Code 里面，先确保它开着
    try:
        st = workspace.code_server_status()
    except Exception:
        st = {}
    if not st.get("running"):
        r0 = workspace.start_code_server(proj)
        if not r0.get("ok"):
            return {"ok": False,
                    "error": "得先启动内置 VS Code 才能用它的终端：%s" % r0.get("error")}
    # ② 算绝对路径（终端的 cwd 就是项目根，但用绝对路径最稳）
    try:
        p = workspace.abs_path(rel, proj)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not os.path.isfile(p):
        return {"ok": False, "error": "文件不存在：%s" % rel}
    # ③ 拼命令送进终端
    q = '"'
    cmd = "python %s%s%s" % (q, p, q)
    _args = str(b.get("args") or "").strip()
    if _args:
        cmd += " " + _args
    try:
        _cwd = workspace.root(proj)
    except Exception:
        _cwd = ""
    r = workspace.send_to_vscode_terminal(cmd, _cwd)
    if r.get("ok"):
        r["cmd"] = cmd
        r["note"] = "已送进 VS Code 终端（没自动弹出来的话按 Ctrl+`）。"
    return r


def _warm_models(models: list) -> None:
    """后台把模型拉进显存（空 prompt + 保活），失败只记日志、不影响使用。"""
    import urllib.request as _u
    try:
        cfg = config.load_config() or {}
    except Exception:
        cfg = {}
    base = str(cfg.get("ollama_url") or "http://127.0.0.1:11434").rstrip("/")
    keep = str(cfg.get("model_keep_alive") or "4h")
    # ⚠️⚠️ options 必须和真实聊天请求**同一套**（尤其是 num_ctx）——
    #    否则 Ollama 会认为"配置变了"，把刚预热的模型卸掉重装：
    #    实测日志里每 20 秒一次 `loading model via llama-server`，
    #    用户体感"每句话都要等 25 秒"。详见 ollama_client.warm_options 的说明。
    warm_opts = ollama_client.warm_options()
    for m in models:
        try:
            raw = json.dumps({"model": m, "prompt": "", "stream": False,
                              "keep_alive": keep,
                              "options": warm_opts}).encode()
            req = _u.Request(base + "/api/generate", data=raw,
                             headers={"Content-Type": "application/json"})
            _u.urlopen(req, timeout=600).read()
            logger.info("已预热模型 %s", m)
        except Exception as e:
            # 预热失败无所谓（模型没下载/显存不够），真正请求时还会再试一次。
            logger.warning("预热模型 %s 失败：%s", m, e)


@app.on_event("startup")
def _warmup_on_start():
    """启动后**主动预热默认模型**，让用户的第一条消息不用等模型加载。

    ⚠️ 为什么以前没有：之前这件事挂在"开发台"（studio.js）里，而开发台已整块移除，
    于是 `/api/ws/warm` 成了一个**没人调用的死接口** —— 实测确认预热从未执行过
    （`grep -rn "ws/warm" frontend/` 只剩 studio.js 一处，而它已不加载）。

    只预热**默认模型**：显卡显存装不下默认模型 + 代码模型两个，
    同时加载会溢出到内存，反而更慢。代码模型仍按需加载。
    """
    # ★ 2026-11-11：后台把 Ollama 拉起来 + 预热模型清单缓存。
    #   ① 端口没起来就尽力 `ollama serve` —— 否则前端「下载本地模型」是死链
    #      （列表空、下载必失败，界面上只有一句「请先启动 ollama serve」）；
    #   ② 预热缓存：本机连 11434 **即使被拒绝也要等满 2 秒**（实测 2004 ms，
    #      原始 socket 同样），不预热的话重启后第一条消息要先白冻 2 秒。
    #   放最前面，不等自检跑完；失败一律静默（云端对话不受影响）。
    def _boot_ollama() -> None:
        _try_start_ollama()
        for _ in range(16):                  # 最多等 8 秒，让它把端口起来
            if _ollama_port_alive():
                break
            time.sleep(0.5)
        _model_tags_cache["fail_t"] = 0.0     # 刚起过，允许立刻重探一次
        _installed_models()

    threading.Thread(target=_boot_ollama, daemon=True).start()

    # ★ 启动自检（2026-11-08 新增）
    #   用户要求：「每次启动都得全部验证所有东西并报告状态，确保无论隔多长时间都没问题」。
    #   后台跑一遍（5-10 秒），结果缓存供 /api/selftest 查询；不阻塞服务起来，
    #   也不受 warmup_on_start 开关影响。
    #   按 config 里的默认模型预热一份缓存（前端若切到别的模型，会各自再跑一份）。
    def _selftest_boot() -> None:
        try:
            _m = str((config.load_config() or {}).get("default_model") or "")
        except Exception:
            _m = ""
        try:
            selftest.run_all(_m)
        except Exception:                            # noqa: BLE001
            pass

    threading.Thread(target=_selftest_boot, daemon=True).start()

    # ★ 定时任务调度线程（2026-11-08）：每 20 秒看一次有没有到期的任务
    try:
        scheduler.start()
    except Exception:
        logger.warning("定时任务调度没起来（不影响其它功能）", exc_info=True)

    try:
        cfg = config.load_config() or {}
    except Exception:
        cfg = {}
    if not cfg.get("warmup_on_start", True):
        return
    m = str(cfg.get("default_model") or "").strip()
    if m:
        threading.Thread(target=_warm_models, args=([m],), daemon=True).start()


@app.post("/api/ws/warm")
def ws_warm(body: dict):
    """**预热模型**（按需调用）：免得第一次写代码干等十几秒。

    实测冷启动那一下：代码模型要现加载，首字节能到 14 秒（界面上就是"没反应"）。
    这里在后台用空 prompt 把它拉进显存并按配置保活，后面就是秒回。
    `body.model` 指定单个模型；不传则预热"代码模型 + 默认模型"。
    """
    try:
        cfg = config.load_config() or {}
    except Exception:
        cfg = {}
    want = str((body or {}).get("model") or "").strip()
    models = [want] if want else [
        m for m in [cfg.get("code_model"), cfg.get("default_model")] if m]
    if not models:
        return {"ok": False, "error": "没有可预热的模型"}
    threading.Thread(target=_warm_models, args=(models,), daemon=True).start()
    return {"ok": True, "models": models}


@app.get("/api/ws/ide/status")
def ws_ide_status():
    """本机装了哪个 IDE（PyCharm / VS Code）+ 当前项目目录。**只探测，不启动。**"""
    return workspace.ide_status()


@app.post("/api/ws/open_ide")
def ws_open_ide(body: dict):
    """用本机已装的 PyCharm / VS Code 打开当前项目。

    为什么不自己造一个"更专业的编辑器"：断点调试、变量监视、重构、
    代码导航这些东西，成熟 IDE 花了十几年 —— 与其重造一个半成品，
    不如**把专业 IDE 直接接进来**（项目目录就是同一个，改完这边刷新即可）。
    """
    return workspace.open_in_ide(str((body or {}).get("project") or ""))


@app.post("/api/ws/open_folder")
def ws_open_folder(body: dict):
    """在资源管理器里打开项目文件夹。"""
    return workspace.open_folder(str((body or {}).get("project") or ""))


@app.get("/api/doclib/files")
def library_files():
    return {"ok": True, "files": doclib.list_files(), "stats": doclib.stats()}


@app.get("/api/doclib/file")
def library_read(rel: str):
    r = doclib.read_file(rel)
    if not r.get("ok"):
        raise HTTPException(status_code=404, detail=r.get("error") or "读取失败")
    return r


@app.post("/api/doclib/file")
def library_write(body: dict):
    """前端编辑后保存（用户在界面上改的，不弹确认）。"""
    rel = str((body or {}).get("rel") or "").strip()
    r = doclib.write_file(rel, str((body or {}).get("text") or ""))
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r.get("error") or "保存失败")
    return r


@app.post("/api/doclib/delete")
def library_delete(body: dict):
    r = doclib.delete_file(str((body or {}).get("rel") or "").strip())
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r.get("error") or "删除失败")
    return r


@app.post("/api/doclib/export_docx")
def library_export_docx(body: dict):
    """把文库里的文本转成 Word（.docx）——WPS / Word 都能直接打开。"""
    rel = str((body or {}).get("rel") or "").strip()
    rd = doclib.read_file(rel)
    if not rd.get("ok"):
        raise HTTPException(status_code=404, detail=rd.get("error") or "读取失败")
    base = rd["rel"]
    out = re.sub(r"\.(md|markdown|txt|text)$", "", base, flags=re.I) + ".docx"
    if out == base:
        out = base + ".docx"
    data = docx_write.text_to_docx(rd.get("text") or "",
                                   title=os.path.splitext(os.path.basename(base))[0])
    w = doclib.save_bytes(out, data)
    if not w.get("ok"):
        raise HTTPException(status_code=400, detail=w.get("error") or "导出失败")
    return {"ok": True, "rel": out, "bytes": w.get("bytes", 0)}


@app.post("/api/doclib/copy")
def library_copy(body: dict):
    """复制库内文件（用户在界面上「另存一份」）。

    没给新名字就自动加「-副本」后缀 —— 让按钮点一下就能用，
    不用强迫用户先想一个名字。
    """
    b = body or {}
    rel = str(b.get("rel") or "").strip()
    new_rel = str(b.get("new_rel") or "").strip()
    if not new_rel:
        stem, ext = os.path.splitext(rel)
        new_rel = "%s-副本%s" % (stem, ext)
    r = doclib.copy_file(rel, new_rel)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r.get("error") or "复制失败")
    return r


@app.post("/api/doclib/backup")
def library_backup():
    r = doclib.backup_all()
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r.get("error") or "备份失败")
    return r


# ---------------------------------------------------------------- 地图
@app.get("/api/map/tile/{z}/{x}/{y}.png")
def map_tile(z: int, x: int, y: int):
    """OpenStreetMap 瓦片（WGS-84 坐标系）。

    ⚠️ 由后端代理：国内直连 OSM 官方瓦片经常超时（实测），后端统一换了镜像。
    ⚠️ **不做任何缓存**（2026-09-18 起地图不再本地持久化）——响应带 no-store，
       浏览器那边也不留。离线时这里直接 404，前端据此提示"离线不显示底图"。
    """
    from . import map_tools as _mt
    data, _cached = _mt.get_tile(z, x, y, src="osm")
    if not data:
        raise HTTPException(status_code=404,
                            detail="这张瓦片取不到（离线或源站不可用）",
                            headers={"Cache-Control": "no-store"})
    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/map/amap/{z}/{x}/{y}.png")
def map_tile_amap(z: int, x: int, y: int):
    """高德底图瓦片（GCJ-02 坐标系）。

    ⚠️ **不能和 OSM 共用路径**：同一个 z/x/y 在两套底图下不是同一块地
    （坐标系数值差 50~500 米）。前端按卡片上的 tile_source 选端点，
       打点也会同步做 WGS-84 → GCJ-02 的换算。
    ⚠️ 同样不缓存：no-store。
    """
    from . import map_tools as _mt
    data, _cached = _mt.get_tile(z, x, y, src="amap")
    if not data:
        raise HTTPException(status_code=404,
                            detail="这张高德瓦片取不到（离线或源站不可用）",
                            headers={"Cache-Control": "no-store"})
    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/map/search")
def map_search(q: str, limit: int = 5):
    """按名字找地点（给前端/模型用）。"""
    from . import map_tools as _mt
    return {"ok": True, "places": _mt.search_place(q, limit)}


@app.get("/api/map/stats")
def map_stats():
    """地图当前状态（模式 / 底图 / 有没有配高德 key）。

    ⚠️ 这里**不再报告缓存** —— 地图不做本地持久化了，没有缓存可报。
    """
    from . import amap as _am
    from . import map_tools as _mt
    h = _am.check_health(online=_mt.online())
    return {"ok": True,
            "online": _mt.online(),
            "tile_source": _mt.tile_source(),
            "tile_source_name": _mt.tile_source_text(),
            "amap_key": _am.has_key(),        # 只报"有没有"，不回显 key 本身
            "amap": h,                        # key 的可用性状态（前端按钮据此亮/暗）
            "builtin": _mt.builtin_count()}   # 内置常用地名表（不是缓存）


@app.get("/api/map/amap_status")
def map_amap_status(force: int = 0):
    """高德当前可用不可用 —— 前端拿它决定按钮「亮起 / 变暗」。

    带缓存（正常 15 分钟、异常 1 分钟复查一次），所以前端可以放心轮询。
    `?force=1` 则**无视缓存、立刻真调一次高德**（面板上那个「重新验证」按钮用）。
    """
    from . import amap as _am
    from . import map_tools as _mt
    return {"ok": True, **_am.check_health(force=bool(force), online=_mt.online())}


@app.post("/api/map/amap_key")
def set_amap_key(body: dict):
    """**更改**高德 key：先真调一次高德验证，**通过了才覆盖旧的**。

    用户明确要求：apikey 不能直接改，得点「更改 API」走这个入口，
    而且要"验证成功才能改、同时删掉原来的"。
    所以这里：验证失败 → **原 key 一字不动**（不能改坏）；验证成功 → 覆盖（旧的即被替换）。

    ⚠️ **不接受空 key**。以前传空会直接清空配置 —— 实测被误点一次就把 key 弄丢了，
    用户还以为自己没配过。要停用请走 `/api/map/amap_toggle`（断开），key 会留着。

    保存后**立刻生效**（配置每次现读，不用重启），并自动置为「已连接」。
    """
    from . import amap as _am
    from . import config as _cfg
    key = str((body or {}).get("key") or "").strip()
    if not key:
        return {"ok": False,
                "message": ("这里只能填一个**新的 key**。要临时不用高德，请用「断开」——"
                            "断开不会删掉 key，随时点「连接」就能恢复。")}
    ok, msg = _am.verify_key(key)
    if not ok:
        return {"ok": False, "message": msg}          # 失败 → 旧 key 保持原样
    cfg = _cfg.load_config()
    cfg["amap_key"] = key                             # 覆盖 = 旧的就此删掉
    cfg["amap_enabled"] = True                        # 换好就直接连上
    _cfg.save_config(cfg)
    _am.invalidate_health()      # 换了 key → 立刻重新检测，别等 15 分钟
    return {"ok": True, "message": "已改用新的 key：" + msg}


@app.post("/api/map/amap_toggle")
def toggle_amap(body: dict):
    """**连接 / 断开**高德（不动 key）。

    用户要求：断开时 key 要留着（"重新输入太麻烦"），断开后按钮变暗、
    连接后按钮变亮。所以断开改的是 `amap_enabled`，不是 `amap_key`。
    """
    from . import amap as _am
    from . import config as _cfg
    on = bool((body or {}).get("enabled"))
    cfg = _cfg.load_config()
    if on and not _am.stored_key():
        return {"ok": False,
                "message": "还没配过高德 key，先点「更改 API」填一个。"}
    cfg["amap_enabled"] = on
    _cfg.save_config(cfg)
    _am.invalidate_health()
    return {"ok": True,
            "message": ("已连接高德。" if on else
                        "已断开高德，地图改回 OpenStreetMap（**key 还留着**，"
                        "随时点「连接」就能恢复）。")}


_MIME_BY_EXT = {
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif",
    ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8", ".json": "application/json",
    ".html": "text/html; charset=utf-8", ".py": "text/plain; charset=utf-8",
    ".js": "text/plain; charset=utf-8", ".css": "text/plain; charset=utf-8",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
# 浏览器能直接显示的，就走 inline（手机上点了能看，而不是"下载到一个找不到的地方"）
_INLINE_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif",
               ".txt", ".md", ".csv", ".json", ".py", ".js", ".css", ".log"}


@app.get("/api/doclib/download")
def library_download(rel: str, inline: int = 0):
    """下载 / 预览生成文库里的文件。

    2026-11-09 用户报「手机端生成的文档打不开」：
      原来一律 `media_type=application/octet-stream` + `filename=`（= attachment），
      手机浏览器（尤其微信）只会把它当"下载"，多数情况直接失败或下到找不到的目录。
    现在：
      · pdf / 图片 / 文本类 → 默认 inline，浏览器原地打开
      · Office 等无法预览的 → 仍然 attachment（手机端请用 /api/doclib/view 那个说明页）
    """
    path = doclib.file_path(rel)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="文件不存在")
    ext = os.path.splitext(path)[1].lower()
    mime = _MIME_BY_EXT.get(ext, "application/octet-stream")
    want_inline = bool(inline) or ext in _INLINE_EXT
    if want_inline and ext not in (".docx", ".xlsx", ".pptx"):
        # inline 时不要把文件名塞进 disposition，否则部分手机仍会强制下载
        return FileResponse(path, media_type=mime,
                            headers={"Content-Disposition": "inline"})
    return FileResponse(path, filename=os.path.basename(path), media_type=mime)


def _office_to_html(path: str, ext: str) -> str:
    """把 Office 文件转成手机能直接读的 HTML（失败返回空串）。

    2026-11-09 用户报「手机端生成的文档打不开」：手机浏览器渲染不了 docx/pptx/xlsx，
    只给"下载"按钮，在微信里还常常点不动。这里把内容抽出来直接展示，
    排版不保证一致，但**能看**；下面仍然保留"下载原文件"。
    """
    import html as _h
    try:
        if ext == ".docx":
            import docx
            d = docx.Document(path)
            parts = []
            for p_ in d.paragraphs:
                t = (p_.text or "").strip()
                if not t:
                    continue
                style = (getattr(getattr(p_, "style", None), "name", "") or "").lower()
                if style.startswith("heading"):
                    lvl = "".join(ch for ch in style if ch.isdigit()) or "2"
                    lvl = lvl if lvl in "123456" else "2"
                    parts.append("<h%s>%s</h%s>" % (lvl, _h.escape(t), lvl))
                else:
                    parts.append("<p>%s</p>" % _h.escape(t))
            for tb in getattr(d, "tables", []) or []:
                rows = []
                for r in tb.rows:
                    cells = "".join("<td>%s</td>" % _h.escape((c.text or "").strip())
                                    for c in r.cells)
                    rows.append("<tr>%s</tr>" % cells)
                if rows:
                    parts.append("<table>%s</table>" % "".join(rows))
            return "".join(parts)

        if ext == ".pptx":
            from pptx import Presentation
            pr = Presentation(path)
            out = []
            for i, slide in enumerate(pr.slides, 1):
                texts = []
                for sh in slide.shapes:
                    if getattr(sh, "has_text_frame", False):
                        t = (sh.text_frame.text or "").strip()
                        if t:
                            texts.append(t)
                if texts:
                    out.append("<h3>第 %d 页</h3><p>%s</p>"
                               % (i, _h.escape("\n".join(texts)).replace("\n", "<br>")))
            return "".join(out)

        if ext == ".xlsx":
            import openpyxl
            wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
            ws = wb[wb.sheetnames[0]]
            rows = []
            for row in ws.iter_rows(values_only=True):
                cells = "".join("<td>%s</td>" % _h.escape("" if v is None else str(v))
                                for v in row)
                rows.append("<tr>%s</tr>" % cells)
                if len(rows) >= 200:
                    break
            try:
                wb.close()
            except Exception:
                pass
            return "<table>%s</table>" % "".join(rows)
    except Exception:
        return ""
    return ""


@app.get("/api/doclib/view")
def library_view(rel: str):
    """手机友好的"打开文件"页：能预览的预览，不能预览的给一个明确按钮。

    为什么需要：手机上点 Office 文件，浏览器没法渲染，微信里还可能直接吞掉。
    这个页面把"文件名 / 大小 / 该点哪里"讲清楚，并把下载按钮放到手指好点的位置。
    """
    import html as _html
    try:
        path = doclib.file_path(rel)
    except Exception as e:
        raise HTTPException(400, f"路径不合法：{e}")
    if not os.path.isfile(path):
        raise HTTPException(404, "文件不存在（可能已被删除或改名）")
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    size = os.path.getsize(path)
    human = ("%.1f MB" % (size / 1048576)) if size >= 1048576 else ("%.0f KB" % (size / 1024))
    q = urllib.parse.quote(rel)
    dl = "/api/doclib/download?rel=" + q + "&inline=1"

    # 文本类：直接把内容摊开，手机上不用任何 App 就能看
    text_html = ""
    if ext in (".txt", ".md", ".csv", ".json", ".py", ".js", ".css", ".log") and size <= 400 * 1024:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text_html = "<pre>" + _html.escape(f.read()) + "</pre>"
        except Exception:
            text_html = ""

    if text_html:
        body = text_html
    elif ext in (".docx", ".xlsx", ".pptx"):
        conv = _office_to_html(path, ext)
        if conv:
            body = ('<p class="hint">以下是文件内容的网页预览（排版与原文件略有差异）。</p>'
                    + conv +
                    '<a class="btn" href="%s" download>⬇ 下载原文件</a>' % dl)
        else:
            body = ('<p class="hint">这个文件暂时没法在线预览，请下载后用 WPS / Office 打开。</p>'
                    '<a class="btn" href="%s" download>⬇ 下载到手机</a>' % dl)
    elif ext in _INLINE_EXT:
        body = ('<p class="hint">这个文件浏览器可以直接打开。</p>'
                '<a class="btn" href="%s">打开 / 预览</a>' % dl)
    else:
        body = ('<p class="hint">%s 格式浏览器无法直接预览，请下载后用 WPS / Office 打开。</p>'
                '<a class="btn" href="%s" download>⬇ 下载到手机</a>'
                '<p class="hint warn">如果在微信里点不动：点右上角「⋯」→「在浏览器中打开」，再回到本页。</p>'
                % (_html.escape(ext.lstrip(".") or "该"), dl))

    # 用拼接而不是 %-格式化：CSS 里有 `width:100%`，会和 %s 冲突（踩过一次）
    page = ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>" + _html.escape(name) + "</title>"
        "<style>"
        "body{margin:0;padding:16px;background:#0f1218;color:#e6e9ef;"
        "font:15px/1.6 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif}"
        "h1{font-size:16px;margin:0 0 4px;word-break:break-all}"
        ".meta{color:#8b93a7;font-size:12.5px;margin-bottom:14px}"
        ".btn{display:block;text-align:center;background:#4f8bf9;color:#fff;"
        "text-decoration:none;padding:13px;border-radius:10px;font-weight:600;margin:14px 0}"
        ".hint{color:#a8b0c2;font-size:13px;margin:10px 0}"
        ".hint.warn{color:#e0b25a}"
        "pre{background:#141821;border:1px solid #232936;border-radius:10px;padding:12px;"
        "overflow:auto;white-space:pre-wrap;word-break:break-word;font-size:13.5px;margin:14px 0}"
        "p{margin:9px 0} h2,h3{font-size:15px;margin:18px 0 8px;color:#cfe0ff}"
        "table{border-collapse:collapse;width:100%;margin:12px 0;font-size:13px}"
        "td,th{border:1px solid #2a3140;padding:6px 8px;vertical-align:top}"
        "</style></head><body>"
        "<h1>" + _html.escape(name) + "</h1>"
        "<div class=\"meta\">" + human + "</div>"
        + body + "</body></html>")
    return HTMLResponse(page)


@app.post("/api/doclib/open_folder")
def library_open_folder():
    """在资源管理器里打开生成文库目录（容器里由宿主机小助手代劳）。"""
    return _open_folder_impl(doclib.ensure_dir())


@app.post("/api/doclib/open_file")
def library_open_file(body: dict):
    """用本机默认程序**直接打开**生成文库里的一个文件。

    用户反馈（2026-11-08）：产物卡片上只有一个「下载」按钮，想看 Word / PPT
    还得自己去文件夹里一层层翻 —— 希望"双击就能打开"。

    所以这里直接交给系统：Windows `os.startfile`、macOS `open`、
    Linux `xdg-open`（等同于在资源管理器里双击那个文件）。
    容器里调不起宿主机的程序，改由宿主机小助手在资源管理器里选中该文件。
    """
    rel = str((body or {}).get("rel") or "").strip()
    if not rel:
        raise HTTPException(400, "缺少文件路径 rel")
    try:
        path = doclib.file_path(rel)          # 内部会校验越界
    except Exception as e:
        raise HTTPException(400, f"路径不合法：{e}")
    if not os.path.isfile(path):
        raise HTTPException(404, "文件不存在（可能已被删除或改名）")

    if _is_container():
        if _host_agent_alive() and _request_host_open(path):
            return {"ok": True, "opened": False, "via": "host-agent",
                    "host_path": _host_path(path),
                    "detail": "运行在容器里，已请宿主机在资源管理器中选中这个文件"}
        return {"ok": True, "opened": False, "host_path": _host_path(path),
                "detail": "运行在容器里，无法直接调用宿主机的程序"}

    try:
        import subprocess
        if os.name == "nt":
            os.startfile(path)                # 等同双击
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return {"ok": True, "opened": True, "path": path}
    except FileNotFoundError:
        return {"ok": True, "opened": False, "path": path,
                "detail": "系统里没找到能打开这个文件的程序"}
    except Exception as e:
        raise HTTPException(500, f"打开文件失败：{e}")


@app.get("/api/doclib/all")
def library_all():
    """生成文库「全量视图」：文库本体 + 工作区软件 + 生成的图片/视频。

    2026-11-11 用户要求：「不仅要有文本，PPT/Excel/Word 等所有内容以及
    软件都要有，并且支持双击打开，按类别展开与收起」。
    这里只做**聚合读取**，不移动、不修改任何磁盘文件。
    """
    try:
        return doclib.list_all()
    except Exception as e:
        raise HTTPException(500, "读取失败：%s" % e)


@app.post("/api/doclib/open_any")
def library_open_any(body: dict):
    """按 id 用本机默认程序打开（文库 / 工作区软件 / 图片都支持）。

    前端双击列表项就走这里 —— 一律 os.startfile，等同在资源管理器里双击。
    """
    ident = str((body or {}).get("id") or "").strip()
    if not ident:
        raise HTTPException(400, "缺少文件 id")
    try:
        info = doclib.resolve_id(ident)
    except Exception as e:
        raise HTTPException(400, str(e))
    path = info["path"]
    if _is_container():
        if _host_agent_alive() and _request_host_open(path):
            return {"ok": True, "opened": False, "via": "host-agent",
                    "host_path": _host_path(path),
                    "detail": "运行在容器里，已请宿主机打开这个文件"}
        return {"ok": True, "opened": False, "host_path": _host_path(path),
                "detail": "运行在容器里，无法直接调用宿主机的程序"}
    try:
        import subprocess
        if os.name == "nt":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return {"ok": True, "opened": True, "path": path,
                "name": info["rel"].split("/")[-1]}
    except FileNotFoundError:
        return {"ok": True, "opened": False, "path": path,
                "detail": "系统里没找到能打开这个文件的程序"}
    except Exception as e:
        raise HTTPException(500, "打开失败：%s" % e)


@app.post("/api/code/run")
def code_run(req: CodeRunRequest):
    """前端代码卡片里的「▶ 运行」——跑用户自己写的/改过的代码。

    用户点"运行"这个动作本身就是批准，所以这里不再二次弹窗。
    走的是和模型同一个沙箱（临时目录、超时、拿不到应用数据目录）。
    """
    r = tools.run_code(req.code, allow_risky=True)
    r.pop("needs_confirm", None)
    return {"ok": True, **r}


@app.post("/api/code/run_stream")
async def code_run_stream(body: dict):
    """**流式**运行聊天里代码卡片上的那段代码 —— 像终端一样边跑边出字。

    为什么不用上面那个 `/api/code/run`：它是同步的，**跑完才拿得到输出**，
    而且有硬性时限。实测用户就是在这里被卡住的 —— 模型写了个番茄钟，
    点「▶ 运行」只等来一句"执行超过 25 秒，已被强制中止"，看起来像程序坏了，
    其实代码一点问题都没有（番茄钟本来就要跑 2 小时）。

    所以这个入口**不设时限**：要不要停，由用户按「■ 停止」决定
    （停止走 /api/ws/run_stop，两边共用同一份进程登记表）。
    """
    b = body or {}
    rid = str(b.get("id") or "")
    workspace.kill_all_runs()             # 一次只跑一个，免得进程越堆越多
    r = workspace.start_run_code(str(b.get("code") or ""), run_id=rid,
                                stdin_text=str(b.get("stdin") or ""))
    if not r.get("ok"):
        async def _bad():
            yield json.dumps({"t": "end", "ok": False,
                              "error": r.get("error")}, ensure_ascii=False) + "\n"
        return StreamingResponse(_bad(), media_type="application/x-ndjson")

    return StreamingResponse(
        _stream_proc_ndjson(r["proc"], rid, r.get("rel") or "", r.get("risky")),
        media_type="application/x-ndjson")


@app.post("/api/code/save")
def code_save(body: dict):
    """前端代码卡片里的「💾 存到文库」——把这段代码存成生成文库里的源码文件。

    为什么要这个端点：代码模型那一轮**没有工具**（它不支持原生工具调用），
    没法自己调 library 保存。所以保存动作由前端按钮触发，落盘逻辑复用
    `_save_code_to_doclib`（和自动兜底同一套命名规则）。
    """
    b = body or {}
    code = str(b.get("code") or "")
    lang = str(b.get("language") or "")
    name = str(b.get("filename") or "")
    rel = _save_code_to_doclib(code, lang, name, str(b.get("user_text") or ""))
    if not rel:
        raise HTTPException(status_code=400, detail="保存失败：代码为空或文件名不合法")
    return {"ok": True, "rel": rel}


@app.post("/api/ws/code")
def ws_code(body: dict):
    """代码卡片里的「📝 写进开发台」：把这段代码直接写进**当前工作区项目**。

    为什么需要它：开发台的 `workspace_write` 只有**模型自己能调**，
    而模型在聊天里贴出来的代码块是"文本" —— 用户原来只能点「📋 复制」再手动粘进
    编辑器，等于把 AI 的输出又搬一遍。这个端点让按钮一步落盘；顺带返回 change_id，
    于是开发台那套"AI 改动 / 一键撤销"也照常生效。
    """
    b = body or {}
    code = str(b.get("code") or "")
    if not code.strip():
        raise HTTPException(status_code=400, detail="代码是空的")
    lang = str(b.get("language") or "").strip()
    rel = str(b.get("rel") or "").strip()
    if not rel:
        # 复用"存到文库"那套命名：优先用户点名的文件名，否则按语言猜
        rel = _guess_code_filename(code, lang, str(b.get("user_text") or ""))
        if "." not in os.path.basename(rel):
            rel += _LANG_EXT.get(lang.lower(), ".txt")
    try:
        r = workspace.write_text(rel, code, by="ai")
    except ValueError as e:
        raise HTTPException(status_code=400, detail="写入失败：%s" % e)
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r.get("error") or "写入失败")
    return {"ok": True, "rel": r["rel"], "chars": r.get("chars", 0),
            "change_id": r.get("change_id") or "",
            # 有 backup 说明覆盖了已有文件（旧版已进回收站）——前端据此换个说法
            "overwrote": bool(r.get("backup")),
            "project": workspace.active_project()}


@app.post("/api/chat/stop")
def chat_stop(body: dict | None = None):
    """终止当前正在进行的生成（界面上的「■ 终止」按钮）。

    正常情况下前端 abort 掉 fetch、连接一断，服务端就会顺势掐掉上游；
    这里再兜一道：即使连接没断干净，也能立刻让模型停下来。
    """
    session = str(((body or {}).get("session_id")) or "").strip()
    killed = _stop_streams(session)
    return {"ok": True, "stopped": killed}


# 「现实世界里要查证才有把握」的名词线索（配合 _WEB_FACT_VERBS 一起用）。
# ⚠️ 只放**真的需要外部事实**的词。像"计划/简历/感想"这类**靠用户自己信息就够**的
#    不放进来，否则每写一份工作计划都要多搜一次、白等几秒还搜不到东西。
_WEB_FACT_NOUNS = (
    # —— 产品与商业（用户报的场景主要在这里）——
    "产品", "品牌", "公司", "企业", "厂商", "集团", "型号", "规格", "参数",
    "配置", "性能", "价格", "报价", "费用", "性价比", "竞品", "对标", "行业",
    "市场", "份额", "财报", "营收", "融资", "上市", "发布会", "新品", "招商",
    # —— 专业 / 学术 / 生僻 ——
    # ⚠️ 2026-09-28：把「指南 / 原理 / 机制 / 定义 / 概念 / 科普」**移除**了 ——
    #    它们是**通用知识词**，模型自己就会写；留着会把「写一份摄影入门指南」
    #    判成"写现实题材 → 要联网核实"，结果搜完上下文被占满、反而没额度写正文
    #    （实测：写《城市夜景摄影技巧》文档时模型只搜不写，用户看到"文档不按要求"）。
    "论文", "文献", "期刊", "学术", "研究", "专利", "标准", "法规", "政策",
    "条例", "规范", "协议", "合同", "条款", "临床", "药物", "疾病",
    "症状", "成分", "配料", "工艺", "材料", "术语", "算法", "架构",
    "深度学习", "机器学习", "人工智能", "神经网络", "大模型",
    # —— 机构 / 人物 / 地方 / 事件 ——
    "机构", "单位", "大学", "学院", "医院", "博物馆", "景区", "景点",
    "人物", "生平", "事件", "赛事", "比赛", "球队",
    # —— 时效 ——
    "最新", "最近", "近期", "目前", "当前", "今年", "去年", "新版", "新版本",
    "版本", "更新", "动态", "趋势", "现状",
    # —— 技术 / 工具 ——
    "框架", "插件", "软件", "接口", "api",
)

# 「要写/要介绍」这类动作。必须与 _WEB_FACT_NOUNS 里的词**同时出现**才判定"写现实题材"。
_WEB_FACT_VERBS = (
    "写", "撰写", "介绍", "简介", "概述", "说明", "整理", "总结", "生成", "做",
    "拟", "起草", "科普", "讲解", "分析", "对比", "评测", "推荐", "盘点",
)


def _wants_search(text: str) -> bool:
    """这句话是不是"该先联网核实一下"？

    两类触发（2026-09-27 扩写 —— 用户报"写产品介绍时不联网就编"）：
      ① **明说要查**："搜一下 / 查一下 / 最新消息"…
      ② **写现实题材**：同时含"撰写类动作"与"现实事物/专业名词"。
         例：「写一份 XX 产品的介绍文」「整理下这行业最近的政策」。

    ⚠️ ② 必须**两个条件同时成立**才触发。只看名词会误伤
       （"介绍一下你自己"、"写一篇春天的散文"），
       只看动作更会误伤（"写"字几乎无处不在）。
    ⚠️ 这里只是**加一句提醒**（不是硬性拦截），而且只在联网开关打开时才用；
       宁可偶尔多搜一次，也别让模型"凭记忆编产品参数"。
    """
    import re
    t = str(text or "")
    if not t:
        return False
    low = t.lower()
    if re.search(r"搜索|搜一下|搜一搜|查一下|查查|查询|帮我查|网上查|去网上|"
                 r"联网查|最新消息|实时新闻|最新进展|最新情况|现在怎么样", t, re.I):
        return True
    # ⚠️ 2026-09-28：纯创作 / 作文类文体直接放行，不走"现实题材"分支 ——
    #    「议论文」含子串「论文」、会被 _WEB_FACT_NOUNS 命中而误判"要联网核实"（实测踩到）。
    #    这类内容靠写作能力，联网只会挤占额度。
    if _is_pure_creative(t):
        return False
    has_noun = any((k in t) or (k in low) for k in _WEB_FACT_NOUNS)
    # ⚠️ "型号"是最可靠的产品信号，但没法穷举（"石头 G30""Mini 4 Pro""K9""Mate 70"…）
    #    —— 用"字母数字混排"来兜：真实型号几乎都长这样。
    #    代价是会把 "python 3""Excel 2016" 也算进来，所以**排除代码任务**（那本来就不搜）。
    if not has_noun and not _is_code_task(t):
        has_noun = bool(re.search(r"[A-Za-z]{1,}\s?\d|\d\s?[A-Za-z]{2,}", t))
    has_verb = any(k in t for k in _WEB_FACT_VERBS)
    return has_noun and has_verb


def _needs_web_ctx(text: str, long_form: bool) -> bool:
    """这一轮要不要提示"先联网核实"？（`long_form` = 长文创作或办公产物任务）

    ⚠️ 与 `_wants_search` 的区别：这里多了"**长文/办公任务默认要核实**"这条。
       原因（2026-09-27 实测）：写产品介绍、做产品 PPT 这类任务里，
       "型号 / 品牌"常常**一个通用名词都不命中** ——
         `帮我做个 PPT，介绍「大疆 Mini 4 Pro 无人机」，6 页左右`
       在 `_wants_search` 下判 False，于是模型没搜就编了一堆参数。
       而这类任务恰恰**事实错误风险最高**，所以只要不是纯创作，一律提醒一句。

    ⚠️ 这只是"多给模型一句提醒"，**不是硬性拦截**：模型判断确实不需要可以不搜
       （提醒文案里已写明例外：纯创作、用户私有产品、资料都在用户给的材料里）。
    """
    t = str(text or "")
    if not t:
        return False
    if _wants_search(t):
        return True
    if not long_form:
        return False
    if _is_pure_creative(t):
        return False
    # ⚠️ 2026-09-28：通用知识 / 方法技巧类（摄影技巧、写作方法、入门教程…）**不提示联网**。
    #    实测提示了它真去搜，搜索结果把上下文占满，反而**没生成文档**
    #    （用户报"文档不按要求 / 工具像坏了"的来源之一）。
    #    但只要出现**具体型号/版本/专有名词**（「大疆 Mini 5 Pro 使用技巧」），仍要核实。
    if _is_generic_knowledge(t) and not _has_specific_entity(t):
        return False
    return True


# ---------- 记忆 API ----------
# 结构：**一份长期记忆（所有对话共享） + 每个对话一份短期记忆（互相独立）**。
# 长期记忆放"换任何对话都成立"的信息（身份/偏好/约定），所以新开对话不会不认识你；
# 短期记忆放"这件事"的上下文，切对话就换一份，并且可以单独释放。
@app.get("/api/memory")
def memory_get(session_id: str = ""):
    """一次取回两块：长期记忆（全局）+ 该对话的短期记忆。"""
    sid = (session_id or "").strip()
    lng = memory.get_long_doc()
    sht = memory.get_short_doc(sid) if sid else {"content": "", "updated_at": 0}
    return {"ok": True, "session_id": sid,
            "long": lng["content"], "long_updated_at": lng["updated_at"],
            "short": sht["content"], "short_updated_at": sht["updated_at"],
            "long_cap": memory.LONG_CAP, "short_cap": memory.SHORT_CAP}


@app.post("/api/memory")
def memory_set(body: dict):
    """保存记忆。scope=long 存长期记忆，否则存该对话的短期记忆。"""
    scope = str((body or {}).get("scope") or "short").lower()
    content = str((body or {}).get("content") or "")
    if scope in ("long", "global"):
        d = memory.set_long(content)
        return {"ok": True, "scope": "long", "content": d["content"]}
    sid = str((body or {}).get("session_id") or "").strip()
    if not sid:
        raise HTTPException(400, "缺少 session_id")
    d = memory.set_short(sid, content)
    return {"ok": True, "scope": "short", "session_id": sid, "content": d["content"]}


@app.delete("/api/memory")
def memory_clear(session_id: str = "", scope: str = "short"):
    """清空记忆。scope=long 清长期记忆；session 清某个对话的短期记忆；否则清全部短期。"""
    scope = (scope or "short").lower()
    if scope == "long":
        memory.set_long("")
        return {"ok": True, "cleared": "long"}
    if scope == "session":
        sid = (session_id or "").strip()
        if not sid:
            raise HTTPException(400, "缺少 session_id")
        memory.set_short(sid, "")
        return {"ok": True, "cleared": "session", "session_id": sid}
    r = memory.release("short")
    return {"ok": True, "cleared": "all_short", **r}


@app.get("/api/memory/search")
def memory_search(q: str = "", session_id: str = "", top_k: int = 8):
    """记忆检索（条目级）。给前端「检索测试」和排查召回问题用。"""
    n = max(1, min(int(top_k or 8), 20))
    hits = memory.search_all(q, sid=(session_id or "").strip(), top_k=n)
    return {"ok": True, "query": q, "count": len(hits), "hits": hits}


@app.get("/api/memory/index")
def memory_index_status():
    """记忆向量索引状态：条目数、分层统计、向量后端 / 模型 / 维度。"""
    try:
        from . import memvec
        st = memvec.status()
        return {"ok": True, **st}
    except Exception as e:
        return {"ok": False, "error": str(e), "items": 0, "indexed": False}


@app.post("/api/memory/reindex")
def memory_reindex():
    """强制全量重建记忆索引（换模型或怀疑索引异常时用）。"""
    from . import memvec
    return {"ok": True, **memvec.rebuild()}


@app.get("/api/memory/usage")
def memory_usage():
    """各部分占用情况：每个会话的历史、记忆、归档、进程内存。

    用途有二：让用户看清"到底是哪块在占地方"；给一键释放提供依据。
    """
    import os as _os
    from . import sessions as sm

    def _tsize(path):
        try:
            return _os.path.getsize(path)
        except Exception:
            return 0

    mu = memory.usage()
    shorts = memory._load_shorts()
    items, sess_bytes = [], 0
    for s in sm.list_sessions():
        sid = s.get("id") or ""
        msgs = sm.get_messages(sid)
        chars = sum(len(str(m.get("content") or "")) for m in msgs)
        b = _tsize(sm._path(sid))
        sess_bytes += b
        items.append({
            "id": sid, "title": s.get("title") or "", "updated": s.get("updated") or "",
            "messages": len(msgs), "chars": chars,
            "tokens": _est_tokens("".join(str(m.get("content") or "") for m in msgs)),
            "bytes": b, "trimmed": int(s.get("trimmed") or 0),
            # 该对话的短期记忆（跟着对话走，一眼看出哪个对话记了什么）
            "memory_chars": len(str((shorts.get(sid) or {}).get("content") or "")),
        })
    items.sort(key=lambda x: x["bytes"], reverse=True)

    mem_bytes = _tsize(memory.LONG_FILE) + _tsize(memory.SHORT_FILE)

    # 归档 transcript（记忆的"底稿"，释放前要先把它沉淀掉，见 memory_release）
    tr_bytes, tr_files = 0, 0
    if _os.path.isdir(memory.TRANSCRIPT_DIR):
        for fn in _os.listdir(memory.TRANSCRIPT_DIR):
            if fn.endswith(".jsonl"):
                tr_files += 1
                tr_bytes += _tsize(_os.path.join(memory.TRANSCRIPT_DIR, fn))

    return {"ok": True,
            "sessions": items,
            "totals": {
                "sessions_bytes": sess_bytes, "transcript_bytes": tr_bytes,
                "memory_bytes": mem_bytes, "disk_bytes": sess_bytes + tr_bytes + mem_bytes,
                "session_count": len(items),
                "message_count": sum(i["messages"] for i in items),
                "transcript_files": tr_files,
                # 记忆：长期一份 + 短期按对话
                "long_chars": mu["long_chars"], "long_cap": mu["long_cap"],
                "short_chars": mu["short_chars"], "short_used": mu["short_used"],
                "short_cap": mu["short_cap"],
                "memory_chars": mu["long_chars"] + mu["short_chars"],
                "rss_bytes": _process_rss(),
                # 界面/接口用的关键阈值，避免前端写死数字
                "max_keep": sm.MAX_KEEP, "keep_recent": sm.KEEP_RECENT,
                "context_messages": MAX_CONTEXT_MESSAGES,
            }}


def _process_rss() -> int:
    """当前进程常驻内存（容器里就是应用实际吃掉的 RAM）。

    Linux 读 /proc；拿不到就返回 0（界面显示"—"即可，不影响其它功能）。
    """
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        pass
    return 0


@app.post("/api/memory/release")
def memory_release(body: dict):
    """一键释放占用。**默认只清缓存，绝不动长期记忆。**

    分级（从最不重要的开始，够用就停）：
      0. **先把归档里还没沉淀的重要内容提炼进记忆**（关键！）
      1. 归档 transcript —— 沉淀完才能删
      2. 空会话 / 很久没聊且内容极少的僵尸会话
      3. 把过长的会话历史裁到最近若干条（对话记录，不是记忆）
    记忆本身（每个对话那一块 + 全局偏好）**永远不动**。
    """
    import os as _os
    from . import sessions as sm

    scope = str((body or {}).get("scope") or "auto").lower()
    sid = str((body or {}).get("session_id") or "").strip()
    confirm_memory = bool((body or {}).get("confirm_memory"))
    do_sweep = bool((body or {}).get("sweep", True))
    freed = 0
    detail = []

    def _tsize(p):
        try:
            return _os.path.getsize(p)
        except Exception:
            return 0

    # ---- 清空单个会话的历史上下文（记忆不受影响）----
    if scope == "session":
        if not sid:
            return {"ok": False, "reason": "未指定 session_id"}
        # 删历史前也先沉淀一遍，否则这段对话里没被记下的要点就没了
        swept = 0
        if do_sweep:
            try:
                swept = _sweep_one(sid)
            except Exception:
                swept = 0
        before = _tsize(sm._path(sid))
        sm.save_messages(sid, [])
        after = _tsize(sm._path(sid))
        msg = f"已清空该对话的 {_fmt_bytes(before)} 历史"
        if swept:
            msg += f"（先沉淀了 {swept} 条要点进记忆，不会丢）"
        return {"ok": True, "freed_bytes": max(0, before - after),
                "swept": swept, "detail": [msg], "memory_intact": True}

    # ---- 0) 沉淀：把归档里还没进记忆的重要内容提炼出来 ----
    # 这一步是"释放不会丢重要信息"的关键。归档是提炼记忆的原始素材，
    # 而自动提炼只在命中关键词或每 4 轮触发，所以直接删归档 =
    # **把还没被记下的重要内容永久丢掉**。
    swept_total = 0
    if do_sweep and scope in ("auto", "caches"):
        try:
            known = {s.get("id") for s in sm.list_sessions()}
            swept_total = _sweep_all(known_sids=known)
        except Exception as e:
            detail.append(f"沉淀归档时出错（已跳过，不会丢数据）：{e}")
        if swept_total:
            detail.append(f"先从归档里沉淀了 {swept_total} 条要点进记忆")

    # ---- 1) 归档 transcript（沉淀之后才删）----
    if scope in ("auto", "caches"):
        if _os.path.isdir(memory.TRANSCRIPT_DIR):
            n_del = 0
            for fn in _os.listdir(memory.TRANSCRIPT_DIR):
                if not fn.endswith(".jsonl"):
                    continue
                p = _os.path.join(memory.TRANSCRIPT_DIR, fn)
                freed += _tsize(p)
                try:
                    _os.remove(p)
                    n_del += 1
                except Exception:
                    pass
            if n_del:
                detail.append(f"清理归档记录 {n_del} 个（已先沉淀要点）")

    # ---- 2) 僵尸会话（很久没聊且几乎没内容）----
    if scope in ("auto", "caches"):
        n = sm.cleanup_old(days=30, max_count=2)
        if n:
            detail.append(f"清理 {n} 个几乎没用过的空会话")

    # ---- 3) 裁剪过长的会话历史 ----
    if scope in ("auto", "sessions", "caches"):
        for s in sm.list_sessions():
            sid2 = s.get("id")
            msgs = sm.get_messages(sid2)
            if len(msgs) <= sm.MAX_KEEP:
                continue
            before = _tsize(sm._path(sid2))
            sm.prune(sid2)
            freed += max(0, before - _tsize(sm._path(sid2)))
            detail.append(f"「{(s.get('title') or '')[:12]}」历史裁到最近 {sm.KEEP_RECENT} 条")

    # ---- 4) 记忆本身：不参与释放 ----
    # 记忆（每个对话那一块 + 全局偏好）是**全库里最该保住的东西**，
    # 所以任何释放都明确不碰它。只有用户在界面上主动清空才会没。
    detail.append("记忆（各对话记忆块 + 全局偏好）已完整保留")

    # ---- 5) 短期记忆：可选释放（它是"缓存"性质的，长期记忆仍然保留）----
    short_freed = 0
    if scope == "short":
        # 释放前先把归档沉淀一轮，否则短期记忆一清、对话里的结论就真没了
        if do_sweep:
            try:
                swept_total += _sweep_all(
                    known_sids={s.get("id") for s in sm.list_sessions()})
            except Exception:
                pass
        r = memory.release("short")
        short_freed = int(r.get("freed_chars") or 0)
        detail.append(f"已释放全部短期记忆（{short_freed} 字）")
        detail.append("长期记忆（身份/偏好/约定）已保留")

    return {"ok": True, "freed_bytes": freed,
            "swept": swept_total, "short_freed_chars": short_freed,
            "detail": detail or ["没有需要释放的内容"],
            "memory_intact": True,
            "hint": "短期记忆与缓存已释放；长期记忆（身份/偏好/约定）始终保留。"}


# =====================================================================
#  归档沉淀：把 transcript 里还没进记忆的重要内容提炼出来
#  —— "释放不会丢重要信息"的关键保障
# =====================================================================
_SWEEP_MAX_SESSIONS = 8      # 单次释放最多沉淀几个对话，避免等太久
_SWEEP_MIN_MSGS = 4          # 少于这么多条的归档不值得单独跑一次模型


def _sweep_one(session: str, cfg: dict = None, model: str = None) -> int:
    """把某个对话的归档沉淀进它的记忆，返回新增要点条数。

    用一次轻量模型调用把归档里的稳定信息抽成要点，合并进该对话的记忆。
    失败就返回 0（**宁可少记，也不能因为沉淀失败就把数据删了**）。
    """
    if not session:
        return 0
    msgs = memory.read_transcript(session, limit=120)
    if len(msgs) < _SWEEP_MIN_MSGS:
        return 0
    cfg = cfg or config.load_config()
    model = model or cfg.get("default_model")
    convo = "\n".join(
        f"{'用户' if m['role'] == 'user' else '助手'}：{str(m['content'])[:220]}"
        for m in msgs[-60:])
    prompt = (
        "下面是一段历史对话。请提取其中值得记住的内容，"
        "**这段对话记录马上就要被删除**，只保留你提取的要点。\n"
        "· 要提取：用户透露的身份/偏好/约定、正在做的事、得出的结论、关键数据\n"
        "· 不要提取：寒暄、一次性的提问、助手自己的解释性内容\n"
        "· 输出格式（每行一条）：类别|要点\n"
        "  类别只能二选一：\n"
        "    长期 —— 换任何对话都成立（身份、姓名、职业、长期偏好、约定）\n"
        "    短期 —— 只跟这件事有关（当前项目、本次结论、临时设定）\n"
        "· 要点**用自己的话归纳**（不要照抄原句、去掉口语），每条 60 字以内 ——\n"
        "  但**数字 / 时间 / 金额 / 比例 / 名称 / 技术栈 / 理由依据必须原样保留**，\n"
        "  宁可这条长一点也不能丢（丢了这条记录就废了）\n"
        "· 最多 6 条；确实没有值得留存的内容就只输出：无\n"
        "· 下面【已知信息】里已经有的，不要再重复提取\n\n"
        "【已知信息】\n长期记忆：" + (memory.get_long().strip()[-2500:] or "（暂无）")
        + "\n短期记忆：" + (memory.get_short(session).strip()[-800:] or "（暂无）")
        + "\n\n【对话】\n" + convo
    )
    # 输出预算不足会让提炼静默失败（见 _EXTRACT_TOKENS），
    # 所以这里统一走 _llm_extract（它会自动在截断时加预算重试）。
    text = _llm_extract(prompt, model, cfg)
    return _apply_extract(text, session, limit=6)


def _sweep_all(known_sids=None) -> int:
    """对所有归档做一次沉淀。返回新增要点总数。

    只处理"对话还在"的归档 —— 对话已删的归档没有记忆可挂，
    提取出来也无处安放（那部分属于用户主动删除，本就该消失）。
    """
    import glob as _glob
    total, done = 0, 0
    try:
        cfg = config.load_config()
    except Exception:
        return 0
    for path in _glob.glob(os.path.join(memory.TRANSCRIPT_DIR, "*.jsonl")):
        if done >= _SWEEP_MAX_SESSIONS:
            break
        sid = os.path.basename(path)[:-6]
        if known_sids is not None and sid not in known_sids:
            continue          # 对话已删，没必要再沉淀
        try:
            got = _sweep_one(sid, cfg)
        except Exception:
            got = 0
        total += got
        done += 1
    return total


def _fmt_bytes(n: int) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


@app.delete("/api/memory/{sid}")
def memory_delete_by_sid(sid: str):
    """按对话删除记忆（对话被删时也会自动调到这里）。"""
    memory.delete_short(sid)
    return {"ok": True, "session_id": sid}


# ---------- 知识库 AI API ----------
class UploadBody(BaseModel):
    name: str
    content: str


@app.get("/api/kb")
def kb_list():
    """列出知识库文档，并带上目录路径（前端要显示"该把文件放哪"）。"""
    docs = kb.list_documents()
    # 带上「这篇被切成了几块」——前端显示「2.4k 字 · 6 块」。
    # 索引没建立时 chunk_counts 会自己从磁盘加载；真拿不到就填 0，
    # 不能因为这个字段让整个列表 500。
    try:
        counts = kb.chunk_counts()
    except Exception:
        counts = {}
    for d in docs:
        d["chunks"] = int(counts.get(d.get("rel") or "", 0))
    return {"ok": True, "documents": docs, "dir": kb.KB_DIR,
            "enabled": bool(config.load_config().get("rag_enabled"))}


@app.post("/api/kb")
def kb_save(body: UploadBody):
    """保存一段文本到知识库。name 可带子文件夹，如 `课程A/第一章/笔记.md`。"""
    try:
        doc = kb.save_document(body.name, body.content)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "document": doc}


@app.post("/api/local-file")
def local_file(body: dict):
    """把**本机路径**读成图片附件（2026-11-09）。

    为什么需要：用户在资源管理器里 Ctrl+C 一张图，再到应用里 Ctrl+V，
    浏览器拿到的是 `file:///C:/...jpg` 这样的**路径文本**，不是文件内容 ——
    网页没权限读本地文件，于是只能提示"无法传输"。
    本应用本来就在同一台机器上跑，由后端按路径读出来再回给前端即可。
    """
    raw = str((body or {}).get("path") or "").strip().strip('"')
    if not raw:
        return {"ok": False, "error": "路径为空"}
    # file:///C:/x.png  →  C:/x.png
    if raw.lower().startswith("file:///"):
        raw = raw[8:]
        try:
            from urllib.parse import unquote
            raw = unquote(raw)
        except Exception:
            pass
    raw = raw.replace("/", os.sep)
    if not os.path.isfile(raw):
        return {"ok": False, "error": "文件不存在：%s" % raw}
    ext = os.path.splitext(raw)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
        return {"ok": False,
                "error": "只支持图片（png/jpg/webp/gif/bmp）：%s" % os.path.basename(raw)}
    try:
        size = os.path.getsize(raw)
        if size > 25 * 1024 * 1024:
            return {"ok": False, "error": "图片太大（%.1fMB），请压到 25MB 以内" % (size / 1048576)}
        data = open(raw, "rb").read()
    except OSError as e:
        return {"ok": False, "error": "读取失败：%s" % e}
    import base64 as _b64
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}[ext]
    return {"ok": True, "name": os.path.basename(raw),
            "dataurl": "data:%s;base64,%s" % (mime, _b64.b64encode(data).decode())}


@app.post("/api/kb/upload")
async def kb_upload(file: UploadFile):
    """上传文档到知识库。

    **按原始字节存盘**，不做任何解码 —— PDF / Word 都是二进制，
    以前 decode("utf-8", errors="replace") 会把文件解坏，存进去等于垃圾。
    解析交给读取时按扩展名走 doc_extract。
    """
    data = await file.read()
    try:
        doc = kb.save_bytes(file.filename or "未命名", data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "document": doc}


@app.post("/api/doc/extract")
async def doc_extract_upload(file: UploadFile):
    """从上传的文档里抽取文本（拖拽文档到聊天框时调用）。

    只抽文本返回，**不落盘** —— 用户只是"想让模型看这份文档"，
    并不一定要收进知识库。想留下的可以再用 /api/kb/upload。
    """
    import tempfile
    from . import doc_extract
    name = file.filename or "未命名"
    data = await file.read()
    if not data:
        return {"ok": False, "name": name, "error": "文件是空的"}
    tmp = os.path.join(tempfile.gettempdir(),
                       f"mm_doc_{int(time.time() * 1000)}_"
                       + os.path.basename(name).replace("..", ""))
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        text, note = doc_extract.extract_text(tmp)
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass
    return {"ok": bool(text.strip()), "name": name,
            "chars": len(text), "text": text, "note": note,
            "supported": doc_extract.is_supported(name),
            "error": "" if text.strip() else (note or "没能从这份文档里读出文字")}


# ⚠️ 路径参数要用 `{name:path}`：知识库现在是支持子文件夹的，
# `课程A/第一章/讲义.md` 这种名字里带 `/`，普通的 `{name}` 匹配不上。
@app.delete("/api/kb/{name:path}")
def kb_delete(name: str):
    # ⚠️ `delete_document` 是**幂等**的：文件已经不在了也返回 True。
    # 实测踩过 —— 用户先在资源管理器里把文件删掉、界面上还留着旧列表，
    # 再点「删除」时原来是 404「文档不存在」，用户一脸问号
    # （"我就是要删它，它没了不正是我要的结果吗"）。
    # 所以这里的 409 只剩一种情况：**同名文件有多个，无法确定删哪一个**。
    if not kb.delete_document(name):
        raise HTTPException(409, "子文件夹里有多个同名文件，无法确定删哪一个。"
                                 "请带上文件夹路径再删。")
    return {"ok": True}


@app.post("/api/kb/search")
def kb_search(body: dict):
    """检索知识库（块级混合检索：语义向量 + BM25）。

    返回的是**片段**而不是整篇文档，所以同一篇长文档里不同段落会被分别命中，
    这正是召回率提升的关键。每条带 vec_score / kw_score / chunk 便于排查。
    """
    q = body.get("query") or ""
    results = kb.search(q, top_k=body.get("top_k", 5),
                        min_score=body.get("min_score"))
    return {"ok": True, "results": results}


@app.post("/api/kb/reindex")
def kb_reindex():
    """重建知识库索引（向量化）。

    做成**显式触发**：向量化要跑几秒到几十秒（看文档量），
    不该拖慢每一次普通检索，所以只在用户点按钮或检测到模型变更时才重建。
    """
    st = kb.reindex(force=True)
    return {"ok": bool(st.get("ok")), "stats": st, "status": kb.index_status()}


@app.get("/api/kb/index_status")
def kb_index_status():
    """索引状态：后端 / 模型 / 维度 / 块数 / 是否可用。"""
    return {"ok": True, "status": kb.index_status()}


# ---------- 本地文件读取 API ----------
class FileReadBody(BaseModel):
    path: str
    mode: str = "auto"   # auto / text / image


@app.post("/api/file/read")
def file_read(body: FileReadBody):
    result = file_tools.read_file(body.path)
    return result


@app.post("/api/file/scan")
def file_scan(body: dict):
    """按关键词扫描目录/文件集合，返回匹配片段。"""
    paths = body.get("paths") or [body.get("path", "")]
    keywords = body.get("keywords", "")
    content = file_tools.read_by_keywords(paths, keywords)
    return {"ok": True, "content": content[:8000]}


# ---------- 视频读取 API ----------
class VideoBody(BaseModel):
    path: str


@app.post("/api/video/analyze")
def video_analyze(body: VideoBody):
    """抽取视频关键帧并让模型理解。若请求带 frames 则只做抽帧返回。
    默认返回抽帧结果；前端拿到后走 /api/chat 让 Qwen-VL 分析。"""
    result = video.extract_frames(body.path)
    return result


# ---------- 联网搜索 + 外部 API ----------
class WebSearchBody(BaseModel):
    query: str
    top_k: int = 5


@app.post("/api/web/search")
def web_search(body: WebSearchBody):
    if not config.load_config().get("web_enabled"):
        return {"ok": False, "error": "联网搜索开关未开启，请在界面打开"}
    results = web_tools.web_search(body.query, body.top_k)
    return {"ok": True, "results": results}


@app.post("/api/web/call")
def web_call(body: dict):
    """调用已配置的外部 API 工具。"""
    cfg = config.load_config()
    result = web_tools.call_external_api(cfg, body.get("tool", ""), body.get("params", {}))
    return result


@app.get("/api/web/tools")
def web_tools_list():
    return {"ok": True, "tools": web_tools.BUILTIN_TOOLS}


# ---------- 文生图 API ----------
class T2IBody(BaseModel):
    prompt: str
    negative_prompt: str = ""
    steps: int = 4
    width: int = 512
    height: int = 512
    hd: bool = False          # 是否做 4 倍超分放大（512→2048）


@app.post("/api/t2i/generate")
def t2i_generate(body: T2IBody, x_image_key: str = Header(default=""),
                 x_image_model: str = Header(default="")):
    """独立的文生图接口（对话之外直接调）。**2026-11-08 起走云端。**

    本地 SDXL 已整体下线：不再吃 12GB 显存、不用等 50 秒加载流水线。
    """
    cfg = config.load_config() or {}
    aspect = ""
    if body.width and body.height:
        aspect = "16:9" if body.width > body.height else (
            "9:16" if body.height > body.width else "1:1")
    try:
        imgs = cloud_image.generate(body.prompt, x_image_key,
                                    aspect=aspect, cfg=cfg,
                                    model=x_image_model)
    except cloud_image.CloudImageError as e:
        return {"ok": False, "error": str(e),
                "hint": "点右上角 🎨 填图片 API Key，或检查余额"}
    # 顺手存进图库（和对话里生成的行为一致，方便"试画"后直接在图库看到）
    saved = {}
    try:
        saved = tools._autosave_image(imgs[0], body.prompt or "试画", "gen",
                                      "AI 生成（云端·试画）")
    except Exception:
        saved = {}
    return {"ok": True, "b64": imgs[0], "images": imgs,
            "model": x_image_model or cfg.get("image_api_model")
                     or cloud_image.DEFAULT_MODEL,
            "device": "cloud", "provider": "cloud",
            "size": cloud_image.size_for(aspect), "saved": saved}


@app.get("/api/t2i/capability")
def t2i_capability():
    """绘图能力：现在**全部走云端**，不再探测本机显卡。

    保留这个接口是为了前端「能力徽章」不用改结构 —— 它照旧读 kind/gpu/note。
    """
    cfg = config.load_config() or {}
    return {"ok": True, "hd_available": True,
            "device": "cloud", "kind": "cloud", "gpu": "",
            "torch": "", "forced": "cloud", "can_gpu": False,
            "provider": "cloud",
            "base": cfg.get("image_api_base") or cloud_image.DEFAULT_BASE,
            "model": cfg.get("image_api_model") or cloud_image.DEFAULT_MODEL,
            "note": "云端出图：不占本机显存，按张计费",
            "reason": "本地 SDXL 已下线（2026-11-08）"}


@app.post("/api/t2i/device")
def t2i_set_device(body: dict):
    """设备切换 —— 云端模式下没有意义，保留接口只为不报错。"""
    return {"ok": True, "mode": "cloud", "device": "cloud", "gpu": "",
            "torch": "", "verified": True, "forced": "cloud",
            "requested": "cloud",
            "note": "绘图已改走云端 API，不需要选择 CPU/GPU"}


@app.post("/api/t2i/unload")
def t2i_unload():
    """云端模式没有本地绘图引擎要卸载；保留接口兼容旧前端。"""
    try:
        if t2i.any_loaded():
            t2i.unload()
    except Exception:
        pass
    return {"ok": True, "device": "cloud"}


# ---------- 多会话管理 ----------
_legacy_migrated = False


def _migrate_memory_once(active_sid: str) -> None:
    """把老的"多分区"记忆搬进当前对话（只做一次）。

    记忆从"全局多分区"改成了"每个对话一块"，老数据必须搬过去，不能丢。
    放在这里而不是模块导入时执行，是因为迁移需要知道"搬给哪个对话"，
    而默认对话是第一次调 /api/sessions 才建出来的。
    """
    global _legacy_migrated
    if _legacy_migrated:
        return
    _legacy_migrated = True
    try:
        r = memory.migrate_legacy(active_sid)
        if r.get("migrated"):
            print(f"[memory] 老记忆已迁移：长期 {r.get('long', 0)} 条 / "
                  f"短期 {r.get('short', 0)} 条")
    except Exception as e:
        print(f"[memory] 旧记忆迁移失败（不影响使用）：{e}")


@app.get("/api/sessions")
def session_list():
    """列出所有会话（按最近更新倒序）。首次调用会自动创建一个默认会话。"""
    sessions.ensure_default()
    items = sessions.list_sessions()
    if items:
        _migrate_memory_once(items[0].get("id") or "")
    return {"ok": True, "sessions": items}


@app.post("/api/sessions")
def session_create(body: dict | None = None):
    """新建会话。"""
    item = sessions.create((body or {}).get("title"))
    return {"ok": True, "session": item}


@app.get("/api/sessions/{sid}")
def session_get(sid: str):
    """读取某个会话的完整消息（前端切换会话时用于恢复）。"""
    return {"ok": True, "id": sid, "messages": sessions.get_messages(sid)}


@app.put("/api/sessions/{sid}")
def session_save(sid: str, body: dict):
    """保存会话消息（前端每次对话后调用，保证重启后能恢复）。

    2026-11-10：**只许增长**。前端如果丢了一片，它 PUT 上来的 assistant 会比
    服务端刚存的短；这里取长者，防止"前端覆盖掉完整原文"。
    """
    msgs = body.get("messages") or []
    try:
        old = sessions.get_messages(sid) or []
        if msgs and old:
            o_last = next((m for m in reversed(old) if m.get("role") == "assistant"), None)
            n_last = next((m for m in reversed(msgs) if m.get("role") == "assistant"), None)
            if (o_last and n_last
                    and len(str(o_last.get("content") or "")) > len(str(n_last.get("content") or ""))):
                for m in reversed(msgs):
                    if m.get("role") == "assistant":
                        m["content"] = o_last.get("content")
                        break
    except Exception:
        pass
    info = sessions.save_messages(sid, msgs)
    # 前端每次对话后都会 PUT 一次 —— 这里也刷一遍交接卡，
    # 兜住「流式生成中途被中断、没走到收尾分支」的情况。
    try:
        from . import handoff as _handoff_put
        _handoff_put.touch(sid, "前端保存会话（实时落盘）")
    except Exception:
        pass
    return {"ok": True, "session": info}


@app.post("/api/sessions/{sid}/rename")
def session_rename(sid: str, body: dict):
    return {"ok": sessions.rename(sid, (body or {}).get("title") or "")}


@app.delete("/api/sessions/{sid}")
def session_delete(sid: str):
    # 记忆按对话隔离，对话没了它的记忆也没必要留着（否则会攒一堆孤儿记忆）
    sessions.delete(sid)
    memory.delete_short(sid)
    return {"ok": True}


# ---------- 图片库（搜到的图 / 生成的图统一留存）----------
@app.get("/api/library/images")
def library_list():
    return {"ok": True, "images": image_library.list_images(),
            "stats": image_library.stats()}


@app.post("/api/library/images")
def library_save(body: dict):
    """保存图片到图库（前端「保存到图库」按钮 / 模型 save_image_to_library 工具）。"""
    data = body.get("b64") or body.get("url") or ""
    if not data:
        return {"ok": False, "error": "缺少图片数据"}
    # 只给了远程 URL 时由后端下载，避免前端跨域
    if data.startswith("http"):
        raw = web_tools.download_image(data)
        if not raw:
            return {"ok": False, "error": "图片下载失败（可能被防盗链拦截）"}
        data = raw
    meta = image_library.save_image(data, name=body.get("name") or "",
                                    source=body.get("source") or "",
                                    origin=body.get("origin") or "web")
    if not meta.get("ok", True):
        return meta
    return {"ok": True, "image": meta}


@app.get("/api/library/images/{iid}/raw")
def library_raw(iid: str):
    """直接返回图片字节流（供前端 <img> 标签加载，避免 base64 膨胀）。"""
    p = image_library.get_path(iid)
    if not p:
        return Response(status_code=404)
    ext = os.path.splitext(p)[1].lower()
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif"}.get(ext, "image/png")
    with open(p, "rb") as f:
        return Response(content=f.read(), media_type=mime)


@app.get("/api/library/images/{iid}")
def library_get(iid: str):
    b64 = image_library.get_b64(iid)
    if not b64:
        return {"ok": False, "error": "图片不存在"}
    return {"ok": True, "id": iid, "b64": b64}


@app.post("/api/library/images/{iid}/rename")
def library_rename(iid: str, body: dict):
    return {"ok": image_library.rename(iid, (body or {}).get("name") or "")}


@app.delete("/api/library/images/{iid}")
def library_delete(iid: str):
    image_library.delete(iid)
    return {"ok": True}


# ---------- 文字转语音（TTS） ----------
# ⚠️ 这三个路由必须在**模块顶层**。上一版用 insert_after 插在函数体里被自动缩进，
#    结果装饰器在函数内执行、路由一个都没注册（导入不报错，但 /api/tts/* 全 404）。
@app.get("/api/tts/voices")
def tts_voices():
    """可选音色清单（含离线兜底那个，标了 available）。"""
    return {"ok": True, "default": tts.DEFAULT_VOICE, "voices": tts.voice_list()}


@app.post("/api/tts/synth")
def tts_synth(body: dict):
    """把文本合成音频。body: {text, voice, rate, engine, filename}

    · 同步路由 → FastAPI 会丢到线程池跑，不阻塞事件循环；
    · 长文（几千字）要几十秒，前端记得给"生成中"提示；
    · engine=auto 时在线失败会自动回退离线，返回里 warnings 说明原因。
    """
    body = body or {}
    text = str(body.get("text") or "")
    if not text.strip():
        raise HTTPException(status_code=400, detail="text 不能为空")
    try:
        rate = int(body.get("rate") or 0)
    except Exception:
        rate = 0
    try:
        pitch = int(body.get("pitch") or 0)
    except Exception:
        pitch = 0
    return tts.synth(text, voice=str(body.get("voice") or ""), rate=rate,
                     engine=str(body.get("engine") or "auto"),
                     filename=str(body.get("filename") or ""),
                     pitch=pitch, style=str(body.get("style") or ""))


@app.get("/api/tts/audio/{name}")
def tts_audio(name: str):
    """播放/下载合成好的音频（inline，浏览器和 <audio> 都能直接用）。"""
    p = tts.audio_path(name)
    if not p:
        raise HTTPException(status_code=404, detail="音频不存在")
    mime = "audio/mpeg" if p.lower().endswith(".mp3") else "audio/wav"
    return FileResponse(p, media_type=mime,
                        headers={"Content-Disposition": "inline"})


@app.get("/api/tts/styles")
def tts_styles():
    """情感预设清单（用 rate/pitch/volume 组合近似，不是真情感合成）。"""
    return {"ok": True, "styles": tts.style_list()}


@app.get("/api/audio/list")
def audio_list():
    """音频库：data/tts 下所有音频，新的在前。"""
    return {"ok": True, "files": tts.list_audio(), "dir": tts.OUT_DIR}


@app.post("/api/audio/delete")
def audio_delete(body: dict):
    body = body or {}
    return tts.delete_audio(str(body.get("name") or ""))


@app.post("/api/audio/rename")
def audio_rename(body: dict):
    body = body or {}
    return tts.rename_audio(str(body.get("name") or ""),
                            str(body.get("new_name") or ""))


@app.post("/api/audio/open_folder")
def audio_open_folder():
    """打开「音频库」所在的文件夹（data/tts，音频实际保存的位置）。

    和图片库的 /api/library/open_folder 同一套实现，保证
    「界面上看到的」和「磁盘上存的」是同一个目录。
    """
    os.makedirs(tts.OUT_DIR, exist_ok=True)
    return _open_folder_impl(tts.OUT_DIR)




# ---------- 数字人 2.0（一张图 + 一段话 → 会说话的视频） ----------
# ⚠️ 和其它路由一样必须放模块顶层：插进函数体里会被自动缩进，
#    装饰器在函数内执行、路由一个都注册不上（导入不报错但全 404）。
@app.get("/api/avatar2/info")
def avatar2_info():
    """当前形象 + 嘴部矩形 + 来源信息。"""
    return avatar2.info()


@app.get("/api/avatar2/status")
def avatar2_status():
    """能力自检：ffmpeg / ASR / 人脸检测 / torch 各自可用与否。

    前端拿它如实告诉用户"现在能做什么、缺什么"，不假装。
    """
    return avatar2.status()


@app.get("/api/avatar2/raw")
def avatar2_raw():
    p = avatar2._img_path()
    if not os.path.isfile(p):
        raise HTTPException(status_code=404, detail="还没有形象图")
    return FileResponse(p, media_type="image/png",
                        headers={"Content-Disposition": "inline",
                                 "Cache-Control": "no-store"})


@app.post("/api/avatar2/upload")
async def avatar2_upload(file: UploadFile = File(...)):
    """上传一张形象图。存完会自动跑人脸检测定位嘴部。"""
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="空文件")
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="图片太大（上限 20MB）")
    r = avatar2.save_bytes(raw, source=file.filename or "upload")
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r.get("error") or "保存失败")
    return r


@app.post("/api/avatar2/from_library")
def avatar2_from_library(body: dict):
    """把图片库里的图设为形象。body: {id} 或 {path}

    ⚠️ 前端只能拿到图库 id（列表里没有磁盘路径），所以必须支持 id；
       直接用 path 是给"本地绝对路径"这种调用留的口子。
    """
    body = body or {}
    p = str(body.get("path") or "")
    iid = str(body.get("id") or "")
    if not p and iid:
        try:
            p = image_library.get_path(iid) or ""
        except Exception:
            p = ""
    if not p:
        raise HTTPException(status_code=400, detail="缺少 path 或 id（或图库里找不到这张图）")
    r = avatar2.save_from_path(p, source=os.path.basename(p))
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r.get("error") or "保存失败")
    return r


@app.post("/api/avatar2/mouth")
def avatar2_mouth(body: dict):
    """手动校准嘴部矩形（前端拖动）。body: {x,y,w,h}，归一化 0~1。"""
    return {"ok": True, "mouth": avatar2.set_mouth(body or {})}


@app.post("/api/avatar2/detect_mouth")
def avatar2_detect_mouth():
    """重新自动定位嘴部（手动拖偏了点一下复位）。"""
    r = avatar2.detect_mouth(avatar2._img_path())
    if r.get("ok"):
        avatar2.set_mouth(r["mouth"])
        return {"ok": True, "mouth": avatar2.mouth(), "score": r.get("score")}
    return r


@app.post("/api/avatar2/reset")
def avatar2_reset():
    return avatar2.reset()


@app.post("/api/avatar2/speak")
def avatar2_speak(body: dict):
    """文本 → 配音 → 口型对齐 → mp4。body: {text, voice, rate, style, engine}

    ⚠️ 同步路由：FastAPI 会丢到线程池跑，不阻塞事件循环。
       10 秒左右的片子约 8~12 秒渲染完，前端要给"生成中"提示。
    """
    body = body or {}
    text = str(body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text 不能为空")
    if len(text) > 1200:
        raise HTTPException(status_code=400,
                            detail="文本太长（上限 1200 字，约 90 秒语音）")
    try:
        rate = int(body.get("rate") or 0)
    except Exception:
        rate = 0
    return avatar2.speak(text,
                         voice=str(body.get("voice") or ""),
                         rate=rate,
                         style=str(body.get("style") or ""),
                         engine=str(body.get("engine") or "auto"))


@app.get("/api/avatar2/videos")
def avatar2_videos():
    """已生成的数字人视频列表，新的在前。"""
    return {"ok": True, "items": avatar2.list_videos()}


@app.get("/api/avatar2/video/{name}")
def avatar2_video(name: str):
    p = avatar2.video_path(name)
    if not p:
        raise HTTPException(status_code=404, detail="视频不存在")
    return FileResponse(p, media_type="video/mp4",
                        headers={"Content-Disposition": "inline",
                                 "Accept-Ranges": "bytes"})


@app.post("/api/avatar2/video/delete")
def avatar2_video_delete(body: dict):
    body = body or {}
    return avatar2.delete_video(str(body.get("name") or ""))


# ---------- 前端 ----------
_NO_CACHE = "no-store, no-cache, must-revalidate, max-age=0"


@app.middleware("http")
async def _no_cache_pages(request, call_next):
    """让前端文件**彻底不缓存**。

    ⚠️ 这个坑踩过两次：
      ① 改了 app.js（给拖拽加"文档"分支），界面行为一点没变 —— 浏览器吃了缓存的旧文件；
         当时加的 `no-cache, must-revalidate`（靠 ETag 回来校验）解决了一部分。
      ② 但这不够：桌面窗口是 WebView2，**它不一定老老实实回来校验** ——
         实测"运行结果面板改成了实时输出"，用户截图里还是上一版的格式，
         白白让用户以为没修好。
    本地应用的静态资源重新拉一次的开销可以忽略，**正确性远比那几毫秒重要**，
    所以直接 `no-store`：不存、不问、每次重新拿。
    """
    resp = await call_next(request)
    path = request.url.path
    if (path == "/" or path.startswith("/static/")
            or path.endswith((".js", ".css", ".html"))):
        resp.headers["Cache-Control"] = _NO_CACHE
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


@app.get("/")
def index():
    resp = FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
    resp.headers["Cache-Control"] = _NO_CACHE
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# ---------- 界面自更新：改了前端不用用户手动刷新 ----------
# 这个应用跑在 WebView2 窗口里 —— **没有地址栏、也没有刷新按钮**。
# 每次改完前端都要求用户"手动刷新才看到新界面"，等于把开发成本转嫁给用户
# （实测就是这么被投诉的）。所以前端每隔几秒来问一次"界面文件变了没"。
# 只看 mtime+size，不读文件内容，开销可以忽略。
_FE_FILES = ("index.html", "app.js", "studio.js", "style.css")


@app.get("/api/frontend/version")
def frontend_version():
    parts = []
    for name in _FE_FILES:
        try:
            st = os.stat(os.path.join(FRONTEND_DIR, name))
            parts.append("%s:%d:%d" % (name, int(st.st_mtime), st.st_size))
        except OSError:
            parts.append("%s:-" % name)
    return {"version": "|".join(parts)}



# ---------------------------------------------------------------- 版本快照 / 回滚
# 用户 2026-11-10 提出：自开发改了上百次代码，出问题只能靠散落的 .bak 手工拼。
# 这里把 backend/version.py（git 封装）接出来：重启前自动打快照、前端看版本列表、
# 支持整轮 / 单文件两种回滚粒度，回滚前自动建还原点。

# 启动时把「正在跑的代码版本」记下来：之后工作区再变，也能分清
# 「已提交的最新版本」和「进程实际加载的版本」。
try:
    from . import version as _ver_boot
    _BOOT_VER = _ver_boot.current()
    _BOOT_VER["boot_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
except Exception as _e:  # noqa: BLE001
    _BOOT_VER = {"ok": False, "error": str(_e)}

class VersionSnapReq(BaseModel):
    message: str = ""


class VersionRollbackReq(BaseModel):
    hash: str = ""
    files: list = []
    note: str = ""


def _version_autosnapshot(reason: str) -> dict:
    """重启前自动打一次本地快照（只 commit，不 push）。失败绝不影响重启。"""
    try:
        from . import version as _ver
        r = _ver.snapshot("自迭代快照 · " + str(reason or "重启前")[:60])
        if r.get("ok") and not r.get("skipped"):
            logger.info("重启前已打版本快照 %s（%s 个文件）",
                        r.get("hash"), r.get("files"))
        return r or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("重启前打版本快照失败（不影响重启）：%s", e)
        return {"ok": False, "error": str(e)}


@app.get("/api/version/status")
def version_status():
    """版本功能是否可用 + 当前 HEAD + 有多少未提交改动。"""
    from . import version as _ver
    av = _ver.available()
    if not av.get("ok"):
        return {"ok": False, "error": av.get("error")}
    return {"ok": True, "current": _ver.current(), "root": av.get("root"),
            "running": _BOOT_VER, "total": _ver.count(),
            "undo": _ver.get_undo()}


@app.get("/api/version/list")
def version_list(limit: int = 30):
    from . import version as _ver
    return _ver.list_versions(limit)


@app.get("/api/version/detail")
def version_detail(hash: str = ""):
    from . import version as _ver
    return _ver.detail(hash)


@app.get("/api/version/diff")
def version_diff(hash: str = "", rel: str = ""):
    from . import version as _ver
    return _ver.file_diff(hash, rel)


@app.post("/api/version/snapshot")
def version_snapshot(req: VersionSnapReq):
    """手动打一个快照。"""
    from . import version as _ver
    return _ver.snapshot(req.message or "手动快照")


@app.post("/api/version/rollback")
def version_rollback(req: VersionRollbackReq):
    """回滚到某个版本：files 空=整轮，给了列表=只回滚这几个文件。"""
    from . import version as _ver
    r = _ver.rollback(req.hash, req.files or None, req.note or "")
    # 用户要求（2026-11-11）：代码一有改动就要有重启提示，回滚也算改动。
    if isinstance(r, dict) and r.get("ok"):
        try:
            from . import pending as _pend
            _pend.mark("", "回滚到版本 %s" % str(req.hash)[:7])
        except Exception:
            logger.warning("回滚后标记待生效失败", exc_info=True)
    return r


@app.get("/api/version/undo_info")
def version_undo_info():
    """上一次回滚的还原点（有就能「撤销这次回滚」）。"""
    from . import version as _ver
    return _ver.get_undo()


@app.post("/api/version/undo")
def version_undo():
    """撤销上一次回滚：回到回滚前的那一刻（本身也会再存一个还原点）。"""
    from . import version as _ver
    u = _ver.get_undo()
    if not u.get("ok"):
        return {"ok": False, "error": "没有可撤销的回滚记录"}
    h = u["undo"]["restore_hash"]
    r = _ver.rollback(h, None, "撤销回滚")
    if r.get("ok"):
        r["msg"] = "已撤销回滚，代码回到 %s 那一刻。重启应用后生效。" % str(h)[:7]
        try:
            from . import pending as _pend
            _pend.mark("", "撤销回滚到 %s" % str(h)[:7])
        except Exception:
            logger.warning("撤销回滚后标记待生效失败", exc_info=True)
    return r


# ---------- 多人协作群（Hive）----------
# ⚠️ 和语音一样，这些**必须是模块顶层路由**。插进函数体里会被自动缩进，
#    装饰器在函数内执行 → 路由一个都不注册（导入不报错，全 404）。

_hive_clients: set = set()
_hive_loop = None


def _broadcast_hive(event: dict) -> None:
    """把群聊事件推给所有已连接的前端（从后台线程安全投递）。"""
    loop = _hive_loop
    if loop is None or loop.is_closed():
        return
    data = json.dumps(event, ensure_ascii=False)
    for ws in list(_hive_clients):
        try:
            asyncio.run_coroutine_threadsafe(ws.send_text(data), loop)
        except Exception:
            pass


def _hive_on_message(gid: str, msg: dict) -> None:
    _broadcast_hive({"type": "msg", "gid": gid, "msg": msg})


# 中继推来的消息 → 转给前端（回调在客户端线程里触发，投递是线程安全的）
hive_client.client().on_message = _hive_on_message


def _hive_on_stream(gid: str, frame: dict) -> None:
    """别人的 @hd 进度帧 → 转给本机前端（不落盘，只刷新界面）。"""
    _broadcast_hive({"type": "hd_stream", "gid": gid, "frame": frame})


hive_client.client().on_stream = _hive_on_stream


def _hive_on_cancel(gid: str, body: dict) -> None:
    """别的成员请求取消任务 —— 只有**发起人本人或群主**才真的停。

    为什么要在这里判权限：中继只转发、不做权限判断（它看不到明文，也不该解），
    所以「谁有资格取消」必须在真正跑任务的这台机器上决定。
    """
    try:
        mid = (body or {}).get("mid") or ""
        by = (body or {}).get("by") or ""
        info = _hd_running.get(gid) or {}
        if not info or info.get("task_id") != mid:
            return
        g = hive.load_group(gid) or {}
        if not by or (by != info.get("asker_id") and by != g.get("owner")):
            return
        ev = _hd_cancel.get(mid)
        if ev:
            ev.set()
    except Exception:
        pass


hive_client.client().on_cancel = _hive_on_cancel


def _hive_on_file(gid: str, meta: dict) -> None:
    """别人的附件收齐了 → 通知本机前端「有新文件」。

    前端收到后刷新对应消息里的附件卡片（原来显示"传输中"的，现在能点开了）。
    """
    _broadcast_hive({"type": "file", "gid": gid, "file": meta})


hive_client.client().on_file = _hive_on_file


def _hive_on_gone(gid: str, name: str) -> None:
    """群主删了群：推一帧，前端立刻把这个群从列表里去掉。"""
    _broadcast_hive({"type": "group_gone", "gid": gid, "name": name or ""})


hive_client.client().on_gone = _hive_on_gone

# 局域网 P2P 自动发现：监听 UDP 广播，知道同网段谁在共享什么。
# 端口被占（比如开了两个实例）时内部会静默放弃，不影响手动传。
try:
    hive_p2p.start_discovery()
except Exception as _e:
    print("[hive-p2p] 自动发现启动失败（不影响手动传输）:", _e)


# 2026-11-13：内置中继（/hive、/hive/health）已按需求删除。
# 群聊现在走 backend/hive_mesh.py 的纯 P2P 网络 —— 消息只在对等机器之间
# 直连传输，不经过任何服务器。这个位置保留一行注释说明「为什么没有了」，
# 免得以后有人以为是漏删。


@app.websocket("/ws/hive")
async def ws_hive(ws: WebSocket):
    """群聊实时通道：只做下行推送（新消息 / 在线成员 / 连接状态）。

    上行统一走 REST（/api/hive/{gid}/send），因为发消息要落盘 + 可能唤醒模型，
    放在 HTTP 里能做超时和错误提示，比 WebSocket 里静默失败好排查。
    """
    global _hive_loop
    await ws.accept()
    _hive_loop = asyncio.get_running_loop()
    _hive_clients.add(ws)
    try:
        await ws.send_text(json.dumps({
            "type": "hello",
            "status": hive.status(),
            "client": hive_client.client().status(),
        }, ensure_ascii=False))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _hive_clients.discard(ws)


@app.get("/api/hive/status")
def hive_status():
    """连接状态 + 成员在线情况。

    `peers` 是本地角色表和中继在线状态合并后的结果 ——
    前端只认这一份，不用自己拼接两个来源。
    """
    cs = hive_client.client().status()
    gid = cs.get("gid") or ""
    peers: list = []
    if gid:
        try:
            peers = hive.merged_members(gid, cs.get("members"))
        except Exception:
            peers = []
    return {"ok": True, "hive": hive.status(), "client": cs, "peers": peers}


@app.post("/api/hive/profile")
def hive_profile(body: dict):
    """改本机在群里的昵称。"""
    return {"ok": True, "profile": hive.set_name((body or {}).get("name") or "")}


@app.post("/api/hive/profile/renew")
def hive_profile_renew():
    """换一个本机身份（新的 user_id，昵称保留）。

    场景：把整个程序目录拷给另一台机器，两台机器的 user_id 一模一样，
    群里分不清谁是谁、消息还会显示成自己发的。换一个就能正常区分。
    """
    try:
        p = hive.renew_identity()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    try:
        hive_client.client().refresh_identity()
    except Exception:
        pass
    return {"ok": True, "profile": {"user_id": p.get("user_id"), "name": p.get("name")}}


@app.post("/api/hive/create")
def hive_create(body: dict):
    """建群 —— 建群的人就是群主。

    `relay_url` 可选：填了就记下来并立即尝试连上（连不上也不影响建群，
    群仍然建在本机，之后随时可以补填/换服务器）。
    """
    b = body or {}
    try:
        g = hive.create_group(b.get("name") or "协作群", b.get("owner_name") or "")
    except Exception as e:
        return {"ok": False, "error": str(e)}

    # 2026-11-13：建群不再需要任何服务器。
    # `relay_url` 字段保留只为兼容旧前端，含义变成了「连接码」：
    #   留空          → 局域网模式，靠 UDP 广播自动发现同群的人（零配置）
    #   hive3://IP:端口 → 直接连某个人（跨网络，人工传一次码）
    code = (b.get("relay_url") or b.get("code") or "").strip()
    relay_info: dict = {"configured": False, "mode": "mesh"}
    try:
        st = hive_client.client().start(code, g["gid"])
        relay_info = {
            "configured": bool(code),
            "url": code or "局域网",
            "connected": bool(st.get("connected")),
            "mode": "mesh",
            "port": st.get("port") or 0,
            "lan_ip": st.get("lan_ip") or "",
            "error": st.get("error") or "",
        }
    except Exception as e:
        relay_info["error"] = f"{type(e).__name__}: {e}"
    return {"ok": True, "group": g, "relay": relay_info,
            "password": g.get("password") or ""}


@app.get("/api/hive/groups")
def hive_groups():
    return {"ok": True, "groups": hive.list_groups()}


@app.post("/api/hive/join")
def hive_join(body: dict):
    """密码入群。

    body: {password, name?, host?, port?}
      密码 12 位（大小写/连字符都会自动规范化）
      host 只有跨网络才要填 —— 同一 WiFi 靠 UDP 广播自动找到
    老口令 `hive://join?...` 仍然兼容（别人手里可能还存着）。
    """
    b = body or {}
    pw = str(b.get("password") or "").strip()
    legacy = str(b.get("payload") or "").strip()
    if not pw and not legacy:
        return {"ok": False, "stage": "format", "error": "请先输入 12 位入群密码"}
    via, matched = "", 0
    try:
        if pw:
            r = hive_client.client().join_by_password(
                pw, b.get("name") or "",
                str(b.get("host") or "").strip(), int(b.get("port") or 0))
            if not r.get("ok"):
                return r                      # 带 stage，前端能分清错在哪一类
            g = r["group"]
            # 透传「从哪找到的群」：local=本机记录 / lan=局域网 / addr=指定地址
            via, matched = r.get("via") or "", int(r.get("matched") or 0)
            if via == "local":
                # 本机本来就有这个群：补一次网络启动
                hive_client.client().start("", g["gid"])
        else:
            g = hive.join_group(legacy, b.get("name") or "")
    except Exception as e:
        return {"ok": False, "stage": "join", "error": str(e)}
    # 加入后启动 P2P 网络，并把「XX 加入了群」告诉大家 ——
    # 否则只有自己看得到提示，别人只知道名单多了一个人。
    relay = {"configured": False, "connected": False, "error": "", "mode": "mesh"}
    try:
        full = hive.load_group(g.get("gid") or "") or {}
        code = (full.get("host") or "").strip()
        st = hive_client.client().start(code, g["gid"])
        relay = {
            "configured": bool(code), "url": code or "局域网",
            "connected": bool(st.get("connected")),
            "mode": "mesh", "error": st.get("error") or "",
            "port": st.get("port") or 0, "lan_ip": st.get("lan_ip") or "",
        }
        if g.get("joined_new"):
            me = hive.profile()
            hive_client.client().send(g["gid"], f"{me['name']} 加入了群",
                                      mtype="system")
            # 成员表也同步出去 —— 否则别人只知道「有人加入」，
            # 名单里却没有这个人（这是之前「N 人永远显示 1」的根因）
            hive_client.client().broadcast_members(g["gid"])
    except Exception as e:
        relay["error"] = f"{type(e).__name__}: {e}"
    return {"ok": True, "group": g, "relay": relay,
            "via": via, "matched": matched}


@app.get("/api/hive/{gid}/messages")
def hive_messages(gid: str, since: int = 0, limit: int = 200):
    """增量拉消息。since = 上次见到的最大 seq，100 人群也不会卡。"""
    cs = hive_client.client().status()
    peers = cs.get("members") if cs.get("gid") == gid else []
    return {"ok": True,
            "messages": hive.read_messages(gid, since, limit),
            "members": hive.merged_members(gid, peers),
            "group": hive.load_group(gid) or {}}


@app.get("/api/hive/{gid}/invite")
def hive_invite(gid: str):
    """邀请信息：现在是 12 位密码（二维码已删除 —— 电脑扫不了）。"""
    try:
        r = hive.group_password(gid)
        g = hive.load_group(gid) or {}
        return {"ok": True, "gid": gid, "name": g.get("name") or "",
                "password": r.get("password") or "",
                "need_set": bool(r.get("need_set")),
                "owner_only": bool(r.get("owner_only")),
                "kfp": g.get("kfp") or "",
                "is_owner": bool(g.get("is_owner_here"))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/hive/{gid}/password")
def hive_password_get(gid: str):
    """查看入群密码（只有群主本机有明文）。"""
    try:
        return hive.group_password(gid)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/hive/{gid}/password")
def hive_password_set(gid: str, body: dict = None):
    """群主设置 / 重置入群密码。

    ⚠️ 换密码 = 换群密钥，老成员必须用新密码重新进群。前端会先提示。
    """
    b = body or {}
    try:
        r = hive.set_password(gid, str(b.get("password") or ""))
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    except ValueError as e:
        return {"ok": False, "stage": "format", "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    # 密钥变了：网络层要换新标签，并把「换密码」这件事广播出去
    try:
        full = hive.load_group(gid) or {}
        hive_client.client().start("", gid)
        hive_client.client().send(gid, "群主更新了入群密码（老成员需要用新密码重新进群）",
                                  mtype="system")
        hive_client.client().broadcast_members(gid)
        r["vt"] = full.get("vt") or ""
    except Exception as e:
        r["warn"] = str(e)
    return r


@app.post("/api/hive/{gid}/kick")
def hive_kick(gid: str, body: dict):
    """踢人（只有群主能踢）。"""
    try:
        r = hive.kick_member(gid, (body or {}).get("user_id") or "")
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    # 名单变了要广播：不然对方那边还以为自己在群里
    try:
        hive_client.client().broadcast_members(gid)
        hive_client.client().send(gid, f"{r.get('name') or '有人'} 被移出了群",
                                  mtype="system")
    except Exception:
        pass
    return {"ok": True, **r}


@app.get("/api/hive/{gid}/memory")
def hive_memory_get(gid: str):
    """读群记忆（没有就现建一份）。"""
    try:
        return {"ok": True, **hive.read_memory(gid)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/hive/{gid}/memory/rebuild")
def hive_memory_rebuild(gid: str, body: dict = None):
    """按当前群聊内容重建群记忆（覆盖写）。

    为什么要手动重建：自动重建只在 @hd 跑任务时按需触发，
    用户想立刻看到「现在这个群被理解成什么样」时得有个按钮。
    """
    try:
        r = hive.rebuild_memory(gid)
        return {"ok": True, **r}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/hive/{gid}/delete")
def hive_delete(gid: str, body: dict = None):
    """删除群（仅群主）。目录移到 data/hive/_deleted/，误删能捞回来。"""
    try:
        r = hive.delete_group(gid)
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    # 通知其他成员：他们本地那份也要下线（否则列表里还挂着一个打不开的群）
    try:
        hive_client.client().broadcast_gone(gid, r.get("name") or "")
    except Exception:
        pass
    # 广播完再从 P2P 网络里摘掉：群都没了，别再去发现/广播它
    try:
        from . import hive_mesh as _MESH
        _MESH.node().remove_group(gid)
    except Exception:
        pass
    return {"ok": True, **r}


@app.post("/api/hive/{gid}/leave")
def hive_leave(gid: str, body: dict = None):
    """成员主动退出群。

    顺序很重要：**先广播、再退出**。因为广播要靠中继连接，
    一旦先 stop 了客户端，消息就发不出去了，其他人看不到「XX 退出了群」。
    """
    try:
        me = hive.profile()
        notice = f"{me['name']} 退出了群"
        cli = hive_client.client()
        # ① 先广播提示：send 要求自己还在成员表里，所以必须赶在 leave 之前。
        #    ⚠️ 判断条件原来用 cs["connected"]，那是「连没连中继」的旧字段，
        #    P2P 下恒为 False —— 退出提示永远发不出去。改成看有没有在线 peer。
        sent_remote = False
        try:
            cs = cli.status()
            if (cs.get("peers") or 0) > 0 or cs.get("connected"):
                r0 = cli.send(gid, notice, mtype="system")
                sent_remote = bool(r0.get("sent"))
        except Exception:
            pass
        # ② 本地退出：移出成员表 + 记系统消息 + 打 left 标记
        r = hive.leave_group(gid)
        # ③ 名单变了，趁连接还在把新成员表发出去（别人那边才会少一个人）
        try:
            cli.broadcast_members(gid)
        except Exception:
            pass
        # ④ 断开，别让退出的人继续收消息
        try:
            cs = cli.status()
            if cs.get("gid") == gid:
                cli.stop()
        except Exception:
            pass
        return {"ok": True, "sent_remote": sent_remote, **r}
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/hive/{gid}/send")
def hive_send(gid: str, body: dict):
    """发消息。文本里带 @hd 时，异步唤醒模型干活。"""
    b = body or {}
    text = b.get("text") or ""
    mtype = b.get("type") or "text"
    extra = b.get("extra")
    # ⚠️ ref 必须单独取出来传给存储层 —— 之前漏了这一步，
    #    前端传了 ref、后端也收到了，但没写进消息，引用块永远不显示。
    ref = (b.get("ref") or "").strip()
    cli = hive_client.client()
    try:
        # 统一走 client.send —— 它会「本地落盘 + 泛洪给在线的人」。
        # 没人在线时消息进 outbox，等对方上线自动补发。
        # （原来这里是 if connected: send else: 只落盘，导致
        #   「对方不在线时发的消息」永远补发不出去。）
        r = cli.send(gid, text, mtype, extra, ref)
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error") or "发送失败"}
        if r.get("msg"):
            _broadcast_hive({"type": "msg", "gid": gid, "msg": r["msg"]})
    except Exception as e:
        return {"ok": False, "error": str(e)}

    # @hd：只有出现这个标记才花算力，普通聊天完全不动模型
    instr = hive.parse_hd(text)
    if instr is not None:
        threading.Thread(target=_hive_run_model, args=(gid, instr, b),
                         daemon=True).start()

    # 附件：本体分片广播给每个成员，各自存一份。
    # 放后台线程 —— 20MB 文件要发几十片，不能卡住"发送"这个请求。
    files = (extra or {}).get("files") or []
    # ⚠️ 这里**不能**再判断 connected：
    #    没人连上时直接跳过，`_hive_send_files` 就不会被调用，
    #    也就不会进 `_pending_files` 补发队列 —— 对方之后上线永远拿不到文件。
    #    是否真的发得出去交给 _hive_send_files 内部判断（失败即入队）。
    if files:
        threading.Thread(target=_hive_send_files, args=(gid, files),
                         daemon=True).start()

    return {"ok": True, "sent": r, "woke_model": instr is not None}


# 待补发的附件：发的时候群里一个人都不在线，就先记下来，等有人连上来再补发。
# 否则对方上线后消息里有附件卡片，但文件本体永远拿不到（实测确认过）。
_pending_files: list = []          # [{gid, meta, ts}]
_pending_files_lock = threading.Lock()
_PENDING_MAX = 30                  # 最多记 30 个，防止内存无限涨


def _queue_pending_file(gid: str, meta: dict) -> None:
    with _pending_files_lock:
        if len(_pending_files) >= _PENDING_MAX:
            _pending_files.pop(0)
        _pending_files.append({"gid": gid, "meta": meta, "ts": time.time()})


def _flush_pending_files() -> None:
    """有新成员连上来 → 把之前没发成功的附件补发一遍（接收端按 fid+片号去重）。"""
    with _pending_files_lock:
        # 先取走再发：on_peer 会在握手/断线时被多个线程触发，
        # 只 list 不 clear 的话同一批附件会被并发补发好几遍（实测发了 3 遍）。
        todo = list(_pending_files)
        _pending_files.clear()
    if not todo:
        return
    cli = hive_client.client()
    left: list = []
    for it in todo:
        try:
            gid, meta = it["gid"], it["meta"]
            fid = (meta or {}).get("fid") or ""
            if not fid:
                continue
            data = hive.read_file_bytes(gid, fid)
            r = cli.send_file(gid, meta, data) or {}
            if not r.get("ok") or int(r.get("sent") or 0) <= 0:
                left.append(it)
        except Exception:
            left.append(it)
    with _pending_files_lock:
        _pending_files[:] = left


_flush_lock = threading.Lock()
_flushing = False


def _hive_on_peer() -> None:
    """有人连上/断开：刷新在线人数，并把积压的附件补发出去。"""
    global _flushing
    try:
        _broadcast_hive({"type": "peers", "client": hive_client.client().status()})
    except Exception:
        pass
    if not _pending_files:
        return
    # 补发放到后台线程、并延迟 1.2 秒：
    #   · 不能在这个回调里直接发 —— on_peer 是在 mesh 的收包线程里同步调用的，
    #     发几十片附件会把那条连接的读循环卡住；
    #   · 也不能立刻发 —— 握手刚完成时对方偶尔还没准备好收大帧（实测偶发收不到）。
    with _flush_lock:
        if _flushing:
            return
        _flushing = True

    def _later() -> None:
        global _flushing
        try:
            time.sleep(1.2)
            _flush_pending_files()
        except Exception:
            logger.warning("补发附件失败", exc_info=True)
        finally:
            with _flush_lock:
                _flushing = False

    threading.Thread(target=_later, daemon=True).start()


def _hive_on_members(gid: str) -> None:
    """成员表变了（有人进群/退群/被踢）→ 立刻推前端，不用等 4 秒轮询。"""
    try:
        _broadcast_hive({"type": "members", "gid": gid})
    except Exception:
        pass


def _hive_send_files(gid: str, files: list) -> None:
    """把本机刚发的附件逐个广播给群里其他人。

    发不出去（没人在线 / 网络未启动）就进补发队列，等有人上线由
    `_hive_on_peer` 重发 —— 附件没有 outbox，不这么做对方上线后拿不到文件。
    """
    cli = hive_client.client()
    for f in files:
        try:
            fid = (f or {}).get("fid")
            if not fid:
                continue
            data = hive.read_file_bytes(gid, fid)
            r = cli.send_file(gid, f, data) or {}
            if not r.get("ok") or int(r.get("sent") or 0) <= 0:
                _queue_pending_file(gid, dict(f))
        except Exception:
            continue


# 把回调挂到 **mesh 节点**上（不是 HiveClient —— 这两个回调是 MeshNode 的属性）。
# 挂上之后：
#   · 有人连上来 → 补发积压附件 + 前端立刻刷新在线人数；
#   · 成员表变化 → 前端立刻刷新成员列表（原来只能靠 4 秒轮询）。
# ⚠️ 必须挂在 **HiveClient 实例**上，不能挂 MESH.node()：
#    客户端 start() 时 _hook() 会把 mesh 的回调整个重设一遍，
#    直接挂 mesh 的话会被静默覆盖掉（实测踩过：补发队列一直不触发）。
try:
    hive_client.client().on_peer = _hive_on_peer
    hive_client.client().on_members = _hive_on_members
except Exception:
    logger.warning("群聊回调注册失败", exc_info=True)


# ---------------------------------------------------------------- 群附件
#
# 前端把文件读成 base64 传上来（单文件 ≤ 20MB），后端落盘到群目录。
# 随后：
#   · 本机消息带 files 元信息；
#   · 若连着中继，**附件本体也会广播给每个成员**（分片），各存一份 ——
#     这样「全员可见」不依赖发送者在线（他关机了别人照样能看）。

@app.post("/api/hive/{gid}/upload")
def hive_upload(gid: str, body: dict):
    """上传一个附件到群目录。body: {name, mime, data(base64)}"""
    b = body or {}
    try:
        raw = b.get("data") or ""
        if isinstance(raw, str) and raw.startswith("data:"):
            # 允许直接传 dataURL（前端 FileReader 的默认产物）
            raw = raw.split(",", 1)[-1]
        data = base64.b64decode(raw)
        meta = hive.save_file(gid, b.get("name") or "file", data, b.get("mime") or "")
        return {"ok": True, "file": meta, "max": hive.MAX_FILE}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/hive/{gid}/files/{fid}")
def hive_file_get(gid: str, fid: str, download: int = 0):
    """取回附件本体。图片/视频直接内联显示，其它默认当附件下载。"""
    try:
        meta = hive.get_file_meta(gid, fid)
        data = hive.read_file_bytes(gid, fid)
    except Exception as e:
        return PlainTextResponse(str(e), status_code=404)
    mime = meta.get("mime") or "application/octet-stream"
    headers = {}
    # 中文文件名要走 RFC 5987 编码，否则浏览器拿到乱码文件名
    fn = meta.get("name") or "file"
    quoted = urllib.parse.quote(fn)
    if download or meta.get("kind") not in ("image", "video", "audio"):
        headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + quoted
    else:
        headers["Content-Disposition"] = "inline; filename*=UTF-8''" + quoted
    return Response(content=data, media_type=mime, headers=headers)


@app.get("/api/hive/{gid}/files")
def hive_file_list(gid: str):
    try:
        idx = hive._read_json(hive._files_index_path(gid), {}) or {}
        return {"ok": True, "files": list(idx.values()), "max": hive.MAX_FILE}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------- P2P 直传
#
# 用户要求：G 级文件必须**直连互联网**，不经服务器。
# 做法：连接信息做成「连接码」人工传递（二维码/复制粘贴），
#       文件内容走 TCP 直连，**一个字节都不经过中继**。

@app.post("/api/hive/{gid}/p2p/share")
def hive_p2p_share(gid: str, body: dict = None):
    """把本机一个附件设为可共享，返回连接码 + 二维码。"""
    b = body or {}
    try:
        fid = str(b.get("fid") or "")
        if not fid:
            return {"ok": False, "error": "缺少 fid"}
        r = hive_p2p.share(gid, fid, port=int(b.get("port") or 0),
                           want_upnp=bool(b.get("upnp", True)))
        return {"ok": True, **r}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/hive/{gid}/p2p/open")
def hive_p2p_open(gid: str, body: dict = None):
    """直接共享本机任意文件（G 级）。

    为什么不走上传接口：上传要先 base64 进内存，几个 G 的文件直接炸。
    这里只登记路径，发送时按偏移量流式读原文件，不复制、不占额外磁盘。
    """
    b = body or {}
    try:
        path = str(b.get("path") or "").strip().strip('"').strip("'")
        if not path:
            return {"ok": False, "error": "请填一个本机文件路径"}
        if not os.path.isfile(path):
            return {"ok": False, "error": "找不到这个文件：" + path}
        fid = "f_" + os.urandom(8).hex()
        r = hive_p2p.share(gid, fid, port=0, want_upnp=True,
                           src_path=path, name=os.path.basename(path))
        r["name"] = os.path.basename(path)
        return {"ok": True, **r}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/hive/{gid}/p2p/receive")
def hive_p2p_receive(gid: str, body: dict = None):
    """用连接码接收文件（断点续传自动生效）。"""
    b = body or {}
    try:
        code = str(b.get("code") or "").strip()
        if not code:
            return {"ok": False, "error": "缺少连接码"}
        return {"ok": True, **hive_p2p.receive(gid, code)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/hive/{gid}/p2p/transfers")
def hive_p2p_list(gid: str):
    try:
        return {"ok": True, "transfers": hive_p2p.list_all(gid),
                "local_ip": hive_p2p.local_ip(),
                "beacons": hive_p2p.beacons(gid)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/hive/{gid}/p2p/{tid}/{act}")
def hive_p2p_ctl(gid: str, tid: str, act: str):
    """act = pause / resume / cancel"""
    fn = {"pause": hive_p2p.pause, "resume": hive_p2p.resume,
          "cancel": hive_p2p.cancel}.get(act)
    if not fn:
        return {"ok": False, "error": "未知操作"}
    return {"ok": bool(fn(tid))}


@app.get("/api/hive/{gid}/hd/status")
def hive_hd_status(gid: str):
    """当前 @hd 执行状态：谁在跑、排队几个、我能不能取消。"""
    try:
        info = dict(_hd_running.get(gid) or {})
        me = hive.profile()
        g = hive.load_group(gid) or {}
        can = bool(info) and (me["user_id"] == info.get("asker_id")
                              or me["user_id"] == g.get("owner"))
        if info:
            info["elapsed_ms"] = int((time.time() - (info.get("since") or 0)) * 1000)
        waiting = [w for w in _hd_waiting.values() if w.get("gid") == gid]
        return {"ok": True, "running": bool(info), "task": info,
                "queued": _hd_q.qsize(), "waiting": waiting,
                "can_cancel": can}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/hive/{gid}/hd/cancel")
def hive_hd_cancel(gid: str, body: dict = None):
    """取消正在跑（或排队中）的 @hd 任务。

    权限：**发起人本人或群主**。其他人点了会被拒绝 —— 否则群里任何人
    都能把别人正在跑的长任务掐掉。
    """
    try:
        b = body or {}
        me = hive.profile()
        # 先看正在跑的，再看排队中的 —— 排队中也是「用户能看到的、还没结束的任务」
        info = _hd_running.get(gid) or _hd_waiting.get(b.get("task_id") or "") or {}
        if not info:
            return {"ok": False, "error": "当前没有正在执行或排队中的任务"}
        mid = b.get("task_id") or info.get("task_id")
        if mid != info.get("task_id"):
            return {"ok": False, "error": "任务号对不上（可能已经跑完了）"}
        g = hive.load_group(gid) or {}
        if me["user_id"] != info.get("asker_id") and me["user_id"] != g.get("owner"):
            return {"ok": False, "error": "只有发起人或群主能取消"}
        ev = _hd_cancel.get(mid)
        if not ev:
            return {"ok": False, "error": "任务状态已失效（可能刚好跑完）"}
        ev.set()
        _hd_waiting.pop(mid, None)
        # 告诉其他成员「这次任务被取消了」，否则他们那边进度卡片会一直转
        try:
            hive_client.client().send_cancel(gid, mid, me["user_id"])
        except Exception:
            pass
        return {"ok": True, "task_id": mid}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _hive_post(gid: str, text: str, mtype: str, extra: dict | None = None) -> None:
    """把一条消息发出去（本地落盘 + 泛洪给在线成员；没人在线时进补发队列）。

    ⚠️ 2026-11-13 修：原来是「connected 就 send，否则只本地落盘」。
    P2P 下 connected 表示「当前有 peer 在线」，于是**没人在线时**@hd 的回答
    只写进了本机磁盘 —— 既没进 outbox 补发队列，也不会被 T_SYNC 补发，
    其他成员上线后**永远看不到这条 AI 回复**。实测确认。
    统一走 client.send：本地落盘 + 泛洪 + 无人在线时进 outbox，一个都不漏。
    """
    try:
        r = hive_client.client().send(gid, text, mtype, extra)
        if not r.get("ok"):
            logger.warning("群消息发送失败: %s", r.get("error"))
            return
        if r.get("msg"):
            _broadcast_hive({"type": "msg", "gid": gid, "msg": r["msg"]})
    except Exception:
        logger.warning("群消息落盘失败", exc_info=True)


# ---------- @hd 执行队列与进度推送 ----------
# 为什么要排队：一次 @hd 会占满显存跑几十秒。多人同时 @ 时并行跑会互相抢显存
# （实测 Ollama 直接 cudaMalloc 失败），群里也会同时冒出好几个进度条，谁也看不清。
# 串行 + 告诉每个人「你排第几个」反而更清楚。
_hd_q: "queue.Queue" = queue.Queue()
_hd_worker_lock = threading.Lock()
_hd_worker_up = False
_hd_cancel: dict = {}       # task_id -> threading.Event
_hd_running: dict = {}      # gid -> {"task_id","asker","since"}
# 排队中的任务：**必须单独登记**，否则「排在别人后面等着」的那个任务
# 取消接口查不到它（_hd_running 只有正在跑的那个），用户点了取消会被告知
# 「当前没有正在执行的任务」—— 但他明明看到自己在排队。
_hd_waiting: dict = {}      # task_id -> {"task_id","asker","asker_id","gid","since"}
_HD_PUSH_GAP = 0.15         # 进度节流：最快 0.15 秒推一帧（约 6~7 帧/秒）
_HD_PUSH_CHARS = 400        # 或者攒够 400 字就推，长段落不会拖太久


def _hd_emit(gid: str, mid: str, phase: str, delta: str = "",
             task: dict | None = None, seq_in: int = 0) -> None:
    """推一帧 @hd 执行进度。

    两条路都要走：
      · `_broadcast_hive` —— 本机前端立刻看到（不依赖中继，本机模式下也能看）；
      · `send_stream` —— 通过中继广播给其他成员（没连中继时静默失败）。
    """
    frame = {"mid": mid, "phase": phase, "delta": delta,
             "task": task or {}, "seq_in": seq_in, "ts": time.time()}
    try:
        _broadcast_hive({"type": "hd_stream", "gid": gid, "frame": frame})
    except Exception:
        pass
    try:
        hive_client.client().send_stream(gid, mid, phase, delta, task or {}, seq_in)
    except Exception:
        pass


def _hd_start_worker() -> None:
    """懒启动唯一的 @hd 执行线程（进程内只起一次）。"""
    global _hd_worker_up
    with _hd_worker_lock:
        if _hd_worker_up:
            return
        _hd_worker_up = True

    def loop():
        while True:
            job = _hd_q.get()
            try:
                _hive_run_model_now(*job)
            except Exception:
                logger.warning("@hd 任务异常", exc_info=True)
            finally:
                _hd_q.task_done()

    threading.Thread(target=loop, daemon=True, name="hive-hd").start()


def _hive_run_model(gid: str, instruction: str, body: dict) -> None:
    """@hd 唤起的模型任务 —— **入队**，由单个 worker 串行执行。

    指令来源就是 @ 模型的那个人（**不一定是群主**），
    所以这里不检查身份 —— 群里任何人都能唤醒，谁唤醒就以谁的指令为准。
    但「指令优先、上下文只作参考」这条由 build_hd_prompt 在提示词里钉死。
    """
    _hd_start_worker()
    me = hive.profile()
    asker = me["name"]
    mid = "hd_" + os.urandom(4).hex()
    ahead = _hd_q.qsize() + (1 if _hd_running else 0)
    # 入队就推一帧：让群里的人**立刻**知道「有人 @ 了模型、正在排队」，
    # 而不是等轮到才有动静 —— 否则前面的人跑 40 秒，后面的人以为没反应。
    _hd_emit(gid, mid, "queued",
             task={"asker": asker, "ahead": ahead,
                   "instruction": (instruction or "")[:120]})
    # ⚠️ 入队时就建取消事件 + 登记等待表：否则「排队中」的任务没法取消，
    #    前面那个跑 40 秒，后面的人只能干等。
    _hd_cancel[mid] = threading.Event()
    _hd_waiting[mid] = {"task_id": mid, "asker": asker,
                        "asker_id": me["user_id"], "gid": gid,
                        "since": time.time()}
    _hd_q.put((gid, instruction, body or {}, mid, asker, me["user_id"]))


def _hive_collect_attachments(gid: str, body: dict, hist: list):
    """收集本次要喂给模型的附件：本次指令的 + 历史消息里的。

    返回 (images, image_mimes, note)：
      · images      —— base64 列表，交给视觉模型；
      · image_mimes —— 与 images 一一对应的真实 mime；
      · note        —— 拼进提示词的说明（图片名、文档正文）。

    用户要求「LLM 要记得上下文」：先发一张图、再单独 @hd 问「你收到图片了吗」，
    模型也必须看得到。原来只读本次 body，历史附件全丢，实测模型答「没有收到图片」。
    历史附件限流（图片≤4、文档≤3），避免聊久了把上下文和带宽打爆。
    """
    images: list = []
    image_mimes: list = []
    note = ""
    try:
        cur = list(((body or {}).get("extra") or {}).get("files") or [])
        old: list = []
        for m in reversed(hist or []):
            for f in (m.get("files") or []):
                if isinstance(f, dict) and f.get("fid"):
                    old.append(f)
            if len(old) >= 8:
                break
        seen = set()
        n_img = 0
        n_doc = 0
        for f in cur + old:
            fid = (f or {}).get("fid")
            if not fid or fid in seen:
                continue
            seen.add(fid)
            # ⚠️ 必须**逐个**附件容错：hive.file_path() 在索引里查不到 fid 时
            #    会抛 ValueError("文件不存在")，原来这个异常被最外层 try 吞掉，
            #    直接跳出整个 for —— 一个坏附件就把后面所有**存在的**附件一起丢掉，
            #    模型只能看到残缺的一部分。实测确认（坏 fid 放中间，后面的文档全丢）。
            try:
                info = file_tools.read_file(hive.file_path(gid, fid))
            except Exception:
                note += "\n[附件·取不到] " + str(f.get("name") or fid)
                continue
            if not info.get("ok"):
                note += "\n[附件·取不到] " + str(f.get("name") or fid)
                continue
            if info.get("type") == "image":
                if n_img >= 4:
                    note += "\n[附件·图片·本次未载入] " + str(f.get("name") or "")
                    continue
                n_img += 1
                images.append(info["b64"])
                # ⚠️ 必须写 image/jpeg，不能用原始 mime：
                #    file_tools.read_image_b64 一律把图片转成 JPEG 再编码
                #    （quality=85、长边≤1024），声明 png 而内容 jpeg 会让部分
                #    接口直接拒图。实测确认（base64 头是 /9j/ 即 JPEG 魔数）。
                image_mimes.append("image/jpeg")
                note += "\n[附件·图片] " + str(f.get("name") or "")
            else:
                if n_doc >= 3:
                    note += "\n[附件·文件·本次未载入] " + str(f.get("name") or "")
                    continue
                n_doc += 1
                note += ("\n\n【附件：" + str(f.get("name") or "")
                         + "】\n" + (info.get("content") or "")[:8000])
    except Exception:
        pass
    return images, image_mimes, note


def _hive_run_model_now(gid: str, instruction: str, body: dict,
                        mid: str, asker: str, asker_id: str = "") -> None:
    """真正跑一次模型，全程流式推送思考和正文。

    ⚠️ 用流式不是为了「好看」，是因为**思考过程要实时给全员看**：
       非流式要等模型全部算完才拿到结果，中间几十秒群里一片死寂。
    """
    t0 = time.time()
    _hd_waiting.pop(mid, None)      # 轮到它了，从等待表移出
    # 复用入队时建的那个 Event：如果排队期间已经有人取消，这里能直接看到
    ev = _hd_cancel.setdefault(mid, threading.Event())
    if ev.is_set():
        _hd_cancel.pop(mid, None)
        _hd_emit(gid, mid, "done",
                 task={"dur_ms": 0, "cancelled": True, "error": "",
                       "think_chars": 0, "out_chars": 0})
        return
    _hd_running[gid] = {"task_id": mid, "asker": asker,
                        "asker_id": asker_id, "since": t0}
    thinking = ""
    text = ""
    model = ""
    err = ""
    cancelled = False
    resp = None
    try:
        hist = hive.read_messages(gid, 0, 60)
        # 群记忆：按群聊内容整理过的长期背景（用户要求「记忆库上下文根据群聊内容重新构建」）。
        # 没有就现建一份；建失败也不影响这次任务，只是少一层背景。
        try:
            gmem = hive.memory_context(gid, 2500)
        except Exception:
            gmem = ""
        prompt = hive.build_hd_prompt(instruction, hist, asker, memory=gmem)

        # 附件：本次指令的 + 历史消息里的，一起交给模型
        images, image_mimes, attach_note = _hive_collect_attachments(
            gid, body, hist)
        if attach_note:
            prompt += "\n\n【群里的附件（含本次指令与之前聊天里发过的）】" + attach_note

        cfg = config.load_config() or {}
        model = cfg.get("default_model") or ""
        if not model:
            raise RuntimeError("没有配置默认模型（先在设置里选一个）")
        # 「工作全过程」的起点：把这次任务的输入条件也播出去
        # （注入了多少条历史、提示词多长），让人看得出模型是基于什么在干。
        _hd_emit(gid, mid, "start",
                 task={"asker": asker, "model": model, "hist": len(hist),
                       "prompt_chars": len(prompt),
                       "images": len(images),
                       "files": len(((body or {}).get("extra") or {}).get("files") or []),
                       "instruction": (instruction or "")[:120]})
        # 两条通道：本地 Ollama / 云端 DeepSeek。
        # ⚠️ 这个分支以前**不存在** —— 默认模型是 deepseek-flash 时，
        #    @hd 会拿云端模型名去问 Ollama，直接报「模型不存在」，
        #    整个功能在默认配置下根本跑不起来。
        is_cloud = str(model).strip().lower().startswith("deepseek")
        # ★ 2026-11-13 用户要求：「群聊模式也要能自开发」。
        #   @hd 原来只是一次纯文本生成，模型手上**没有任何工具** ——
        #   它在群里说「我已经改好了」，实际一行代码都没动。
        #   自开发开关打开时，这里把自开发工具交给它，并在下面补一个执行循环。
        #   ⚠️ 只给 selfdev_* / workspace_*：群聊要的是改代码，不是画图做 PPT；
        #      工具表越长，模型越容易挑错工具，也越吃上下文。
        tool_names, tool_schemas = set(), []
        try:
            if bool(cfg.get("self_dev_enabled")):
                _all_tools = tools.make_schemas(
                    web_enabled=False, kb_enabled=False, code_exec=True,
                    writing=False, ask_mode=str(cfg.get("ask_mode") or "quick"))
                tool_schemas = [
                    s for s in _all_tools
                    if str((s.get("function") or {}).get("name") or "").startswith(
                        ("selfdev_", "workspace_"))]
                tool_names = {s["function"]["name"] for s in tool_schemas}
        except Exception:
            logger.warning("@hd 取自开发工具表失败（这一轮按普通回答处理）",
                           exc_info=True)
            tool_names, tool_schemas = set(), []
        if tool_names:
            prompt += (
                "\n\n【自开发已打开：你可以改这个应用自己的源码】\n"
                "可用工具：" + "、".join(sorted(tool_names)) + "\n"
                "群聊通道走**文本协议** —— 需要调用工具时，正文里单独写一个块：\n"
                '```tool\n{"name": "selfdev_grep", "arguments": '
                '{"rel": "backend/main.py", "pattern": "关键字"}}\n```\n'
                "一次可以写多个块，工具会**真的执行**，结果会回给你。\n"
                "⚠️ 拿到工具结果之前，**不许**声称已经改完；每一步都要基于真实输出。\n"
                "标准流程：selfdev_grep 定位 → selfdev_read 读那一段 → "
                "selfdev_patch 最小改动 → selfdev_run 验证。\n"
                "⚠️ 不许碰 data/、models/、sessions/、image_library/（那是用户数据）。\n"
                "⚠️ 改前端不用重启后端，但要提醒用户 Ctrl+F5；"
                "改后端要让用户点「立即重启」。\n")
        last_push = 0.0
        seq_in = 0
        pend_t = ""
        pend_c = ""

        def _flush(force: bool = False) -> None:
            """把攒着的增量推出去。节流是为了不把中继和界面刷爆。"""
            nonlocal last_push, seq_in, pend_t, pend_c
            now = time.time()
            if not force and (now - last_push < _HD_PUSH_GAP
                              and len(pend_t) + len(pend_c) < _HD_PUSH_CHARS):
                return
            base = {"asker": asker, "model": model}
            if pend_t:
                seq_in += 1
                _hd_emit(gid, mid, "thinking", pend_t,
                         {**base, "chars": len(thinking)}, seq_in)
                pend_t = ""
            if pend_c:
                seq_in += 1
                _hd_emit(gid, mid, "content", pend_c,
                         {**base, "chars": len(text)}, seq_in)
                pend_c = ""
            last_push = now

        if is_cloud:
            dkey = _ds_key("")
            if not dkey:
                raise RuntimeError(
                    "默认模型是云端「%s」，但没配置 DeepSeek Key —— "
                    "在设置里填一个 Key，或把默认模型换成本地模型" % model)
            # thinking=True：这次任务的意义就在于「思考过程全员可见」，
            # 所以哪怕模型默认不思考，这里也显式打开。
            # 有图时走 OpenAI 的多模态 content 数组（DeepSeek-VL 接口格式）
            if images:
                content = [{"type": "text", "text": prompt}]
                for _i, _b64 in enumerate(images):
                    # mime 用真实类型：原来一律写死 image/jpeg，
                    # 传 PNG/WebP 时声明和内容不符，部分接口会直接拒图。
                    _mt = (image_mimes[_i] if _i < len(image_mimes)
                           else "image/png")
                    content.append({"type": "image_url", "image_url": {
                        "url": "data:" + _mt + ";base64," + _b64}})
                ds_msgs = [{"role": "user", "content": content}]
            else:
                ds_msgs = [{"role": "user", "content": prompt}]
            for kind, val in deepseek_client.chat_stream(
                    ds_msgs, model=model,
                    api_key=dkey, temperature=0.3, max_tokens=2400,
                    thinking=True):
                if ev.is_set():
                    cancelled = True
                    break
                if not val:
                    continue
                if kind == "thinking":
                    thinking += val
                    pend_t += val
                else:
                    text += val
                    pend_c += val
                _flush()
        else:
            _msg_in = {"role": "user", "content": prompt}
            if images:
                _msg_in["images"] = images      # Ollama 的多模态字段
            resp = client.chat([_msg_in],
                               model=model, stream=True,
                               params={"temperature": 0.3, "max_tokens": 2400})
            for raw in resp.iter_lines(chunk_size=64, decode_unicode=False):
                if ev.is_set():
                    cancelled = True
                    break
                line = (raw.decode("utf-8", "ignore") if isinstance(raw, bytes)
                        else str(raw)).strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                msg = obj.get("message") or {}
                th = msg.get("thinking") or ""
                ct = msg.get("content") or ""
                if th:
                    thinking += th
                    pend_t += th
                if ct:
                    text += ct
                    pend_c += ct
                _flush()
                if obj.get("done"):
                    break
        # 收尾：把最后没推完的一小段推掉，别让界面卡在倒数第二句
        if not cancelled:
            _flush(force=True)

        # ★ 2026-11-13：自开发工具执行循环（让群聊 @hd 也能真改代码）。
        #   流程：模型吐调用 → 本地真执行 → 结果回填 → 再问一轮，
        #   直到它不再调工具（或到轮数上限）。
        #   第一轮是流式的（思考过程全员可见）；后续工具轮用非流式，稳定优先。
        if tool_names and not cancelled and not err:
            try:
                _round_cap = max(1, min(8, int(cfg.get("max_tool_rounds") or 6)))
                _ctx = {"images": images or [], "last_image": None, "mask": None,
                        "shown_images": [],
                        "ask_mode": str(cfg.get("ask_mode") or "quick"),
                        "session": "hive:" + gid, "user_text": instruction,
                        "image_key": "", "image_model": "", "ds_key": ""}
                _msgs = [{"role": "user", "content": prompt}]
                _texts = [text]
                _rnd = 0
                while _rnd < _round_cap:
                    _calls, _clean = _split_text_tool_calls(
                        _texts[-1], allowed=tool_names, bare=True)
                    if not _calls:
                        break
                    _rnd += 1
                    _texts[-1] = _clean
                    # 让群里的人看见「它在动哪个文件」，而不是干等
                    _hd_emit(gid, mid, "tool",
                             task={"round": _rnd,
                                   "tools": [str(c.get("name")) for c in _calls]})
                    _res = []
                    for _c in _calls:
                        _nm = str(_c.get("name") or "")
                        _ag = _c.get("arguments") or {}
                        if _nm not in tool_names or not isinstance(_ag, dict):
                            continue
                        try:
                            _out = tools.dispatch(_nm, _ag, [], _ctx)
                        except Exception as _e:      # noqa: BLE001
                            _out = "[工具执行失败] %s：%s" % (_nm, _e)
                        logger.warning("[hive-hd] 自开发工具 %s → %s",
                                       _nm, str(_out)[:160])
                        _res.append("【%s 的结果】\n%s" % (_nm, str(_out)[:6000]))
                    _msgs.append({"role": "assistant",
                                  "content": _clean or "(调用工具)"})
                    _msgs.append({"role": "user", "content":
                                  "工具已执行完，结果如下：\n\n" + "\n\n".join(_res)
                                  + "\n\n没完成就继续调工具；"
                                    "完成了就用一两句话说明改了什么。"})
                    try:
                        if is_cloud:
                            _dkey = _ds_key("")
                            _buf = ""
                            for _k, _v in deepseek_client.chat_stream(
                                    _msgs, model=model, api_key=_dkey,
                                    temperature=0.3, max_tokens=2400,
                                    thinking=False):
                                if ev.is_set():
                                    cancelled = True
                                    break
                                if _k != "thinking" and _v:
                                    _buf += _v
                            _nxt = _buf
                        else:
                            _r2 = client.chat(_msgs, model=model, stream=False,
                                              params={"temperature": 0.3,
                                                      "max_tokens": 2400})
                            _nxt = (((_r2.json() or {}).get("message") or {})
                                    .get("content") or "")
                    except Exception as _e:          # noqa: BLE001
                        err = "%s: %s" % (type(_e).__name__, _e)
                        break
                    if cancelled or not str(_nxt or "").strip():
                        break
                    _texts.append(_nxt)
                text = "\n\n".join([t for t in _texts if str(t).strip()])
            except Exception:
                logger.warning("@hd 自开发工具循环失败（已回答的部分照常落盘）",
                               exc_info=True)
    except Exception as e:
        err = f"{type(e).__name__}: {e}" if not str(e) else str(e)
    finally:
        try:
            if resp is not None:
                resp.close()      # 关连接 = 告诉 Ollama 别再算了
        except Exception:
            pass

    dur = int((time.time() - t0) * 1000)
    # 落盘一条完整消息：**思考过程也要存**，这样后来进群 / 重开应用的人
    # 回看历史时仍能看到当时的完整思考，而不是只剩一个结论。
    if cancelled:
        final = ((text.strip() + "\n\n（任务已被取消）").strip()
                 if text.strip() else "（任务已被取消）")
    elif err:
        final = f"⚠️ 模型执行失败：{err}"
    else:
        # ★ 2026-11-13：模型偶尔会在**没开自开发**时也硬吐一个 ```tool 块
        #   （它见过这种格式）。那种块不会被执行，但会原样留在正文里 ——
        #   群里所有人看到的是一坨 JSON。这里只**清显示**、不执行。
        _shown = _TOOL_FENCE_RE.sub("", text or "").strip()
        final = _shown or "（模型没有返回正文）"
    extra = {"by": asker, "model": model, "thinking": thinking,
             "dur_ms": dur, "task_id": mid, "cancelled": cancelled,
             "think_chars": len(thinking), "out_chars": len(text),
             "error": err}
    try:
        _hive_post(gid, final, "ai", extra)
    except Exception:
        logger.warning("@hd 结果落盘失败", exc_info=True)
    # 结束帧：前端据此把「进度卡片」替换成正式消息
    _hd_emit(gid, mid, "done",
             task={"dur_ms": dur, "cancelled": cancelled, "error": err,
                   "think_chars": len(thinking), "out_chars": len(text)})
    _hd_cancel.pop(mid, None)
    _hd_running.pop(gid, None)


# ---------------------------------------------------------------- P2P 网络
# 2026-11-13：中继相关的路由（relay 配置 / 五层探测 / 部署包 / 升级包 /
# 让模型写部署步骤）已按需求**全部删除**。群聊改走 backend/hive_mesh.py 的
# 纯 P2P：消息只在成员机器之间直连，不经过任何服务器。
# 下面这组是替代它的接口。


@app.get("/api/hive/mesh")
def hive_mesh_status():
    """P2P 网络状态：监听端口、局域网地址、在线 peer 数、待发队列。"""
    return {"ok": True, "mesh": hive_mesh.node().status(),
            "client": hive_client.client().status()}


@app.get("/api/hive/mesh/code")
def hive_mesh_code(gid: str = ""):
    """生成连接码（**只在跨网络时才需要**）。

    同一个 WiFi 下根本用不上这个 —— UDP 广播会自动发现同群的人。
    这个码是给「不在同一个网络」准备的：发给对方，他粘一下就连上，
    数据一字节都不经过任何服务器。
    """
    return hive_client.client().connect_code(gid)


@app.post("/api/hive/mesh/connect")
def hive_mesh_connect(body: dict):
    """用连接码直连一个人。连上之后 peer exchange 会自动铺开其他成员。"""
    b = body or {}
    gid = b.get("gid") or ""
    code = (b.get("code") or "").strip()
    if not gid:
        return {"ok": False, "error": "缺群号"}
    if not code:
        return {"ok": False, "error": "连接码不能为空"}
    r = hive_client.client().connect_to(gid, code)
    return {"ok": bool(r.get("ok")), "result": r,
            "client": hive_client.client().status()}


@app.get("/api/hive/{gid}/peers")
def hive_peers(gid: str):
    """这个群现在连上了谁（局域网发现的 + 手动连的）。"""
    return {"ok": True, "peers": hive_client.client().lan_beacons(gid),
            "mesh": hive_mesh.node().status()}


# 中继部署包 / 升级包 / 模型写部署步骤 / 中继地址配置 —— 全部删除。
# 没有服务器了，这些功能没有存在意义。


@app.post("/api/hive/connect")
def hive_connect(body: dict):
    """进入群聊时调用：启动 P2P 网络。

    `url` 留空 = 局域网模式（UDP 广播自动找同群的人，零配置零服务器）；
    填 hive3://IP:端口 = 直连那个人（跨网络，人工传一次码）。
    """
    b = body or {}
    gid = b.get("gid") or ""
    if not gid:
        return {"ok": False, "error": "缺群号"}
    # 老群（二维码时代建的）没有验证标签，别人拿到密码也算不出密钥 ——
    # 进群时顺手迁移一下：单人群直接生成密码，多人群只提示不擅自动。
    mig = {}
    try:
        mig = hive.ensure_password(gid)
    except Exception as e:
        mig = {"ok": False, "error": str(e)}
    code = (b.get("url") or b.get("code") or "").strip()
    r = hive_client.client().start(code, gid)
    return {"ok": bool(r.get("ok")), "client": hive_client.client().status(),
            "error": r.get("error") or "", "migrate": mig}


@app.post("/api/hive/disconnect")
def hive_disconnect():
    hive_client.client().stop()
    return {"ok": True, "client": hive_client.client().status()}


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

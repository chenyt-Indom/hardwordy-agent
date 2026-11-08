# -*- coding: utf-8 -*-
"""云端图片客户端 —— 文生图 / 图片微改（OpenAI 兼容协议）。

背景（2026-11-08 用户决定）
--------------------------
本地 SDXL 那套（diffusers + 6.5GB 权重 + 吃满 12GB 显存）**整体下线**，
改成走云端 API：
  · 生成：POST {base}/images/generations
  · 微改：POST {base}/images/edits（带蒙版就是"只改涂过的地方"）
好处：不再抢显存（对话模型可以一直躺在显卡里）、不用等 50 秒加载流水线、
      出图质量也更高。代价：要联网、要 Key、按张计费。

安全约定（和 deepseek_client 同一套，改之前先读）
------------------------------------------------
1. Key **只在本函数调用期间存在**：不写 config.json、不写日志、不进全局变量。
2. 所有抛出去的异常信息都过 `scrub()`：`sk-xxx` 一律打码。
   踩过坑 —— requests 的异常消息会带 URL/header 片段，直接抛给前端等于把 Key 打在界面上。
3. `trust_env=False`：本机系统代理会把请求劫持到 127.0.0.1 再回 550/502，
   必须彻底绕开（DeepSeek 那边踩过两轮才定位）。
"""
from __future__ import annotations

import base64
import io
import json
import re
import threading
import time
from typing import Any

import requests

# 默认服务商（用户指定）：xingpan.site 的 OpenAI 兼容代理
DEFAULT_BASE = "https://xingpan.site/v1"
DEFAULT_MODEL = "gpt-image-2.5-flare"

TIMEOUT = (15, 300)          # 连接, 读取（出图慢，给足 5 分钟）

_sessions = threading.local()


def _session() -> "requests.Session":
    s = getattr(_sessions, "s", None)
    if s is None:
        s = requests.Session()
        s.trust_env = False                  # 不读 env / 注册表 / .netrc
        s.proxies = {"http": None, "https": None}
        _sessions.s = s
    return s


_KEY_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{6,})")


def scrub(text: Any) -> str:
    """把 Key 打码后再给用户/日志看。"""
    s = str(text or "")
    s = _KEY_RE.sub("sk-***", s)
    # 有的服务商 Key 不是 sk- 开头，按长度兜一层
    if len(s) > 400:
        s = s[:400] + "…"
    return s


class CloudImageError(RuntimeError):
    """云端图片接口错误（消息已脱敏）。"""


# ----------------------------------------------------------------- 尺寸/比例

# gpt-image 系列只接受这三档
_SIZES = {"square": "1024x1024", "portrait": "1024x1536", "landscape": "1536x1024"}


def size_for(aspect: str = "", explicit: str = "") -> str:
    """把「竖版/横版/9:16…」换算成接口认的尺寸。"""
    ex = (explicit or "").strip()
    if re.match(r"^\d{2,4}x\d{2,4}$", ex):
        return ex
    a = (aspect or "").strip().lower()
    if not a:
        return _SIZES["square"]
    if a in ("9:16", "2:3", "3:4", "portrait", "vertical", "竖版", "竖屏"):
        return _SIZES["portrait"]
    if a in ("16:9", "3:2", "4:3", "21:9", "landscape", "wide", "横版", "横屏", "宽屏"):
        return _SIZES["landscape"]
    return _SIZES["square"]


# ----------------------------------------------------------------- 基础请求

def _base(cfg: dict | None) -> str:
    b = str((cfg or {}).get("image_api_base") or DEFAULT_BASE).strip().rstrip("/")
    if not b.endswith("/v1"):
        b += "/v1"
    return b


def _model(cfg: dict | None) -> str:
    return str((cfg or {}).get("image_api_model") or DEFAULT_MODEL).strip()


def _parse(resp: "requests.Response", model: str = "") -> list[str]:
    """把响应解析成 base64 图片列表（也接受 url，自动下载）。"""
    if resp.status_code != 200:
        msg = ""
        try:
            j = resp.json()
            err = j.get("error")
            msg = (err.get("message") if isinstance(err, dict) else str(err or "")) or \
                  j.get("message") or ""
        except Exception:
            msg = ""
        if not msg:
            try:
                msg = resp.text[:300]
            except Exception:
                msg = ""
        hint = ""
        if resp.status_code in (401, 403):
            hint = "（Key 无效或没有图片权限）"
        elif resp.status_code == 402:
            hint = "（余额不足）"
        elif resp.status_code == 429:
            hint = "（请求太频繁 / 触发限流）"
        elif resp.status_code == 404:
            hint = "（接口路径或模型名不对，检查 image_api_base / image_api_model）"
        elif resp.status_code in (503, 500) and "no available channel" in msg.lower():
            # ⚠️ 2026-11-08 实测遇到（one-api / new-api 这类中转站的标准报错）：
            #   "No available channel for model gpt-image-2.5-flare under group Adobe 生图"
            #   意思是**这个 Key 所属的分组里没有该模型的通道** —— 不是网络问题，
            #   也不是描述问题，换模型名或让服务商开权限才行。
            hint = ("（这个 Key 所属分组没有模型「%s」的可用通道 —— "
                    "去 🎨 里换一个模型名，或找服务商给你开这个模型的权限）"
                    % (model or "?"))
        raise CloudImageError(
            f"图片接口返回 {resp.status_code}{hint}：{scrub(msg)}")

    try:
        data = resp.json()
    except Exception:
        raise CloudImageError("图片接口返回的不是 JSON：" + scrub(resp.text[:200])) from None

    items = data.get("data") or data.get("images") or []
    out: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        b64 = it.get("b64_json") or it.get("b64") or ""
        if b64:
            out.append(b64)
            continue
        url = it.get("url") or ""
        if url:
            try:
                r = _session().get(url, timeout=(10, 120), proxies={"http": None, "https": None})
                if r.status_code == 200:
                    out.append(base64.b64encode(r.content).decode("ascii"))
            except requests.RequestException:
                continue
    if not out:
        raise CloudImageError("接口没有返回图片（data 为空）：" + scrub(json.dumps(data)[:200]))
    return out


# ----------------------------------------------------------------- 对外接口

# 名字里带这些词，基本就是画图模型（one-api 类中转站的模型清单里混着聊天/向量模型）
_IMG_HINTS = ("image", "dall", "flux", "sd", "mj", "midjourney", "seedream",
              "kolors", "qwen-image", "imagen", "firefly", "adobe", "recraft",
              "ideogram", "playground", "stable")


def list_models(key: str, cfg: dict | None = None) -> list:
    """用这个 Key 问服务商：**你能用哪些模型**（不同分组能用的不一样）。"""
    if not key:
        return []
    try:
        r = _session().get(_base(cfg) + "/models",
                           headers={"Authorization": f"Bearer {key}"},
                           timeout=(8, 25), proxies={"http": None, "https": None})
        if r.status_code != 200:
            return []
        d = r.json()
    except Exception:
        return []
    return [str(m.get("id") or "") for m in (d.get("data") or []) if isinstance(m, dict)]


def image_candidates(key: str, cfg: dict | None = None) -> list:
    """从可用模型里挑出"可能能画图"的，按像不像图片模型排序。"""
    names = list_models(key, cfg)
    out = []
    for n in names:
        low = n.lower()
        if any(k in low for k in _IMG_HINTS):
            out.append(n)
    # 名字里带 image/dall/flux 的排前面
    out.sort(key=lambda n: (0 if any(k in n.lower() for k in ("image", "dall", "flux")) else 1, n))
    return out


def _is_no_channel(status: int, text: str) -> bool:
    """是不是"这个模型没有可用通道"这类**换模型就能解决**的错。"""
    low = (text or "").lower()
    if status == 404:
        return True
    if status in (500, 503) and ("no available channel" in low
                                 or "无可用渠道" in (text or "")
                                 or "model_not_found" in low
                                 or "not supported" in low):
        return True
    return False


# ---------------------------------------------------------------- 对话式出图
# 为什么需要它（2026-11-08）：用户的 Key 所属分组**只有 3 个对话模型**
# （gpt-5.6-sol / gpt-6-astra / gpt-6.1-sol），`/images/generations` 必然 503。
# 但不少中转站把"出图"实现在 **/chat/completions** 里：让模型把图片以
# markdown `![](url)` 或 `data:image/png;base64,...` 的形式返回。
# 所以这里补一条兜底通道：图片接口走不通时，用对话接口再试一次。
_IMG_URL_RE = re.compile(
    r'!\[[^\]]*\]\((?P<md>[^)\s]+)\)|<img[^>]+src=["\'](?P<img>[^"\']+)["\']')
_B64_RE = re.compile(r'data:image/(?:png|jpe?g|webp|gif);base64,(?P<b64>[A-Za-z0-9+/=\s]{200,})')


def _extract_images_from_chat(d: dict, key: str) -> list:
    """从对话响应里把图片抠出来（支持 b64 / markdown / <img> / 裸 URL）。"""
    out: list = []
    choices = d.get("choices") or []
    if not choices:
        return out
    msg = (choices[0] or {}).get("message") or {}

    # ① 有的中转站直接把图片挂在 message.images / message.image 上
    for k in ("images", "image"):
        v = msg.get(k) or (choices[0] or {}).get(k)
        if isinstance(v, list):
            for it in v:
                if isinstance(it, dict):
                    if it.get("b64_json"):
                        out.append(it["b64_json"])
                    elif it.get("url"):
                        out.append(("URL", it["url"]))
                elif isinstance(it, str):
                    out.append(it)
        elif isinstance(v, str) and v:
            out.append(v)

    content = msg.get("content")
    if isinstance(content, list):          # 有的返回数组形式
        content = "".join(
            str(c.get("text") or "") if isinstance(c, dict) else str(c)
            for c in content)
    content = str(content or "")

    # ② data:image/...;base64,
    for m in _B64_RE.finditer(content):
        out.append(re.sub(r"\s+", "", m.group("b64")))
    # ③ markdown / <img>
    for m in _IMG_URL_RE.finditer(content):
        u = m.group("md") or m.group("img") or ""
        if u.startswith("http"):
            out.append(("URL", u))

    # 归一：URL 项去重、下载成 b64
    finals: list = []
    seen = set()
    for it in out:
        if isinstance(it, tuple) and it[0] == "URL":
            u = it[1]
            if u in seen:
                continue
            seen.add(u)
            try:
                r = _session().get(u, timeout=(10, 120),
                                   proxies={"http": None, "https": None})
                if r.status_code == 200 and r.content:
                    finals.append(base64.b64encode(r.content).decode("ascii"))
            except Exception:
                continue
        elif isinstance(it, str) and it:
            b = it.split(",", 1)[1] if it.startswith("data:") else it
            b = re.sub(r"\s+", "", b)
            if len(b) > 200 and b not in seen:
                seen.add(b)
                finals.append(b)
    return finals


def generate_via_chat(prompt: str, key: str, model: str,
                      cfg: dict | None = None) -> list:
    """用**对话接口**出图（兜底通道）。失败抛 CloudImageError。"""
    if not key:
        raise CloudImageError("没有配置图片 API Key")
    body = {
        "model": model,
        "stream": False,
        "messages": [{
            "role": "user",
            "content": ("请直接生成一张图片并返回给我。画面要求：%s\n"
                        "直接把图片给我（markdown 图片或图片链接都行），"
                        "不要只给文字描述。" % (prompt or "").strip()),
        }],
    }
    try:
        resp = _session().post(
            _base(cfg) + "/chat/completions",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            timeout=TIMEOUT, proxies={"http": None, "https": None})
    except requests.RequestException as e:
        raise CloudImageError("对话式出图连接失败：%s" % scrub(e)) from None
    if resp.status_code != 200:
        raise CloudImageError("对话式出图返回 %s：%s"
                              % (resp.status_code, scrub((resp.text or "")[:200])))
    try:
        d = resp.json()
    except Exception:
        raise CloudImageError("对话式出图返回的不是 JSON") from None
    imgs = _extract_images_from_chat(d, key)
    if not imgs:
        raise CloudImageError("对话式出图也没有拿到图片（模型只回了文字）")
    return imgs


def _remember_model(model: str, *, edit: bool = False) -> None:
    """把"试通了"的模型写回 config，下次直接用（不再每次试错）。"""
    try:
        from . import config as _c
        cfg = _c.load_config() or {}
        keyn = "image_api_model_edit" if edit else "image_api_model"
        if model and str(cfg.get(keyn) or "") != model:
            cfg[keyn] = model
            _c.save_config(cfg)
    except Exception:
        pass


def generate(prompt: str, key: str, *, size: str = "", aspect: str = "",
             n: int = 1, cfg: dict | None = None, model: str = "",
             auto_fallback: bool = True) -> list[str]:
    """文生图。返回 base64 图片列表。"""
    prompt = (prompt or "").strip()
    if not prompt:
        raise CloudImageError("提示词为空，没法画")
    if not key:
        raise CloudImageError("没有配置图片 API Key（点右上角 🎨 填入）")

    body = {
        "model": model or _model(cfg),
        "prompt": prompt,
        "n": max(1, min(int(n or 1), 4)),
        "size": size_for(aspect, size),
        "response_format": "b64_json",
    }
    url = _base(cfg) + "/images/generations"

    # ★★ 自动换模型（2026-11-08 用户报"充了钱还是 503"）：
    #   503「No available channel for model X under group Y」是**该分组没有这个模型的通道**，
    #   充钱不会让它出现。能做的只有：换一个这个 Key **真的能用**的图片模型。
    #   所以这里先用同一个 Key 去 /v1/models 问清单，把像图片模型的排前面，
    #   逐个试；试通的那个**写回 config**，下次直接用，不用再试。
    models = [body["model"]]
    if auto_fallback:
        for c in image_candidates(key, cfg):
            if c not in models:
                models.append(c)
        models = models[:5]

    last = ""
    for m in models:
        body["model"] = m
        for attempt in range(2):
            try:
                resp = _session().post(
                    url,
                    headers={"Authorization": f"Bearer {key}",
                             "Content-Type": "application/json"},
                    # data= 显式 UTF-8：json= 会把中文转义、请求体膨胀
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    timeout=TIMEOUT, proxies={"http": None, "https": None})
            except requests.RequestException as e:
                last = "连接失败：%s" % scrub(e)
                if attempt < 1:
                    time.sleep(0.6)
                    continue
                break                      # 网络问题，换模型也没用
            if _is_no_channel(resp.status_code, resp.text):
                last = "%s（模型 %s 没有可用通道）" % (resp.status_code, m)
                break                      # 换下一个候选
            if resp.status_code in (500, 502, 503, 504, 550) and attempt < 1:
                last = "服务端 %s" % resp.status_code
                time.sleep(0.8)
                continue
            imgs = _parse(resp, m)
            _remember_model(m, edit=False)
            return imgs
    hint = ""
    if len(models) > 1:
        hint = "；已自动试过 %d 个模型：%s" % (len(models), ", ".join(models))
    raise CloudImageError((last or "图片生成失败") + hint)


def edit(prompt: str, key: str, image_b64: str, *, mask_b64: str = "",
         size: str = "", aspect: str = "", n: int = 1,
         cfg: dict | None = None, model: str = "",
         auto_fallback: bool = True) -> list[str]:
    """图片微改（img2img / inpaint）。

    有 `mask_b64` 时是**局部重绘**：只有蒙版白色区域会被改，其余像素基本不动。
    没有蒙版时是整图重绘（风格迁移、换天空这类）。
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise CloudImageError("提示词为空，不知道要改成什么样")
    if not key:
        raise CloudImageError("没有配置图片 API Key（点右上角 🎨 填入）")
    if not image_b64:
        raise CloudImageError("没有可修改的底图")

    def _blob(b64: str) -> bytes:
        s = b64.split(",", 1)[1] if b64.startswith("data:") else b64
        return base64.b64decode(s)

    files = [("image", ("image.png", io.BytesIO(_blob(image_b64)), "image/png"))]
    if mask_b64:
        files.append(("mask", ("mask.png", io.BytesIO(_blob(mask_b64)), "image/png")))
    form = {
        "model": model or _model(cfg),
        "prompt": prompt,
        "n": str(max(1, min(int(n or 1), 4))),
        "size": size_for(aspect, size),
        "response_format": "b64_json",
    }
    url = _base(cfg) + "/images/edits"

    # 和 generate 同样的自动换模型逻辑（编辑接口的通道往往比生成更少）
    models = [form["model"]]
    if auto_fallback:
        for c in image_candidates(key, cfg):
            if c not in models:
                models.append(c)
        models = models[:5]

    last = ""
    for m in models:
        form["model"] = m
        for attempt in range(2):
            try:
                resp = _session().post(
                    url,
                    headers={"Authorization": f"Bearer {key}"},
                    data=form, files=files, timeout=TIMEOUT,
                    proxies={"http": None, "https": None})
            except requests.RequestException as e:
                last = "连接失败：%s" % scrub(e)
                if attempt < 1:
                    time.sleep(0.6)
                    continue
                break
            if _is_no_channel(resp.status_code, resp.text):
                last = "%s（模型 %s 没有可用通道）" % (resp.status_code, m)
                break
            if resp.status_code in (500, 502, 503, 504, 550) and attempt < 1:
                last = "服务端 %s" % resp.status_code
                time.sleep(0.8)
                continue
            imgs = _parse(resp, m)
            _remember_model(m, edit=True)
            return imgs
    hint = ""
    if len(models) > 1:
        hint = "；已自动试过 %d 个模型：%s" % (len(models), ", ".join(models))
    raise CloudImageError((last or "图片微改失败") + hint)


def check_key(key: str, cfg: dict | None = None) -> dict:
    """校验 Key 能不能用（GET /models，不花钱）。"""
    if not key:
        return {"ok": False, "msg": "没有填 Key"}
    try:
        r = _session().get(_base(cfg) + "/models",
                           headers={"Authorization": f"Bearer {key}"},
                           timeout=(8, 20), proxies={"http": None, "https": None})
    except requests.RequestException as e:
        return {"ok": False, "msg": "连接失败：" + scrub(e)}
    if r.status_code == 401:
        return {"ok": False, "msg": "Key 无效"}
    if r.status_code != 200:
        return {"ok": False, "msg": f"HTTP {r.status_code}：{scrub(r.text[:160])}"}
    try:
        d = r.json()
    except Exception:
        return {"ok": True, "msg": "可达（返回非 JSON）"}
    names = [str(m.get("id") or "") for m in (d.get("data") or []) if isinstance(m, dict)]
    has_img = [n for n in names if any(k in n.lower() for k in ("image", "dall", "flux", "sd"))]
    return {"ok": True,
            "msg": f"可用 · 共 {len(names)} 个模型" + (f"（可画图：{has_img[0]}）" if has_img else ""),
            "models": names[:40]}

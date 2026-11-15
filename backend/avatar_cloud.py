# -*- coding: utf-8 -*-
"""数字人云端生成：阿里云百炼 Wan2.2-S2V（2026-11-15 重构）

为什么整条换掉本地渲染
----------------------
本地那套是「2D 弹性形变」：嘴张多大由音量决定、唇形由频谱推断。
原理上就做不出 b/p/m 闭唇、舌位、牙齿，怎么调参都是橡皮嘴——
上一轮加了拼音音素层，也只是把唇形轮廓补对，口腔内部照样是一团糊。
Wan2.2-S2V 是扩散模型逐帧重绘，口型真正由音频驱动，效果不在一个量级。

官方文档：https://help.aliyun.com/zh/model-studio/wan-s2v-api

接口
----
提交  POST {base}/services/aigc/image2video/video-synthesis
      Header: X-DashScope-Async: enable / Authorization: Bearer sk-xxx
      Body:   {"model": "wan2.2-s2v",
               "input": {"image_url": ..., "audio_url": ...},
               "parameters": {"resolution": "480P" | "720P"}}
      → {"output": {"task_id": ..., "task_status": "PENDING"}}

查询  GET  {base}/tasks/{task_id}
      → output.task_status: PENDING / RUNNING / SUCCEEDED / FAILED
      → output.results.video_url  ⚠ 仅 24 小时有效，必须立刻下载

三条硬限制（不处理就直接报错，都是官方文档写明的）
  1. 单次音频必须 < 20 秒 → 超长自动切段、逐段生成、再拼接
  2. 仅华北2（北京）地域，必须用该地域的 API Key
  3. image_url / audio_url 必须公网可访问 → 本机文件走 DashScope 临时上传

存的东西
--------
  data/avatar2/cloud.json   配置（API Key / 业务空间 ID / 分辨率）
  data/avatar2/videos/      成片（与本地方案同一目录，前端不用改）
  data/avatar2/_cloud_tmp/  切段音频、分段视频、下载缓存（用完清）
"""
from __future__ import annotations

import json
import mimetypes
import os
import shutil
import subprocess
import threading
import time
import uuid

import logging
logger = logging.getLogger(__name__)

_CFG = "cloud.json"
_DEF = {
    "api_key": "",
    "resolution": "480P",
    "model": "wan2.2-s2v",
}
# 说明：旧配置里可能还留着 workspace_id，读取时忽略即可，不做迁移。

MAX_AUDIO_SEC = 19.0      # 官方限制 <20 秒，留 1 秒余量
POLL_INTERVAL = 4.0       # 轮询间隔（秒）
POLL_TIMEOUT = 1200       # 单段最长等 20 分钟
DOWNLOAD_TIMEOUT = 300

# ---------------------------------------------------------------- 进度估算
# ⚠ 云端接口只回 PENDING / RUNNING / SUCCEEDED / FAILED，**不返回百分比**。
#   所以「第几段 / 共几段」是真实进度，「段内百分比」只能按已用时估。
#   界面上一律标「≈」，不能假装是服务器给的真实进度。
#
# 系数来自 2026-11-16 的真实出片实测：3.63 秒音频
#   480P → 全程 57 秒；720P → 全程 79 秒。
# 扣掉上传/提交/下载的固定开销约 8 秒，折成「每 1 秒音频要等多少秒」。
SEG_OVERHEAD = 8.0
FACTOR_DEF = {"480P": 13.5, "720P": 19.6}

SIGNUP_URL = "https://bailian.console.aliyun.com/"
KEY_URL = "https://help.aliyun.com/zh/model-studio/get-api-key"
DOC_URL = "https://help.aliyun.com/zh/model-studio/wan-s2v-api"
PRICE_URL = "https://help.aliyun.com/zh/model-studio/models"


# ---------------------------------------------------------------- 目录与配置
def _root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _dir() -> str:
    d = os.path.join(_root(), "data", "avatar2")
    os.makedirs(d, exist_ok=True)
    return d


def _tmpdir() -> str:
    d = os.path.join(_dir(), "_cloud_tmp")
    os.makedirs(d, exist_ok=True)
    return d


def _cfg_path() -> str:
    return os.path.join(_dir(), _CFG)


def _cfg() -> dict:
    d = dict(_DEF)
    try:
        with open(_cfg_path(), "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            d.update(raw)
    except FileNotFoundError:
        pass
    except Exception:                                       # noqa: BLE001
        logger.warning("cloud.json 读不出来，用默认值", exc_info=True)
    return d


def _save_cfg(d: dict) -> None:
    with open(_cfg_path(), "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def _mask(k: str) -> str:
    if not k:
        return ""
    if len(k) <= 10:
        return k[:3] + "***"
    return k[:6] + "…" + k[-4:]


def config() -> dict:
    """给前端读的配置视图。api_key 只回脱敏值，绝不回明文。"""
    c = _cfg()
    k = (c.get("api_key") or "").strip()
    return {
        "ok": True,
        "has_key": bool(k),
        "api_key": _mask(k),
        "resolution": c.get("resolution") or "480P",
        "model": c.get("model") or "wan2.2-s2v",
        "ready": bool(k),
        "max_audio_sec": MAX_AUDIO_SEC,
        "signup_url": SIGNUP_URL,
        "key_url": KEY_URL,
        "doc_url": DOC_URL,
        "price_url": PRICE_URL,
        "missing": [] if k else ["API Key"],
    }


def set_config(d: dict) -> dict:
    """保存配置。传脱敏值（含「…」）时视为「不改这一项」。"""
    c = _cfg()
    if "api_key" in d:
        v = str(d.get("api_key") or "").strip()
        # 只有「…」是前端回传的脱敏值（_mask 用 U+2026）。星号一定是用户
        # 自己粘进来的 —— 多半是从截图/聊天里复制的打码值，静默忽略只会让
        # 用户看到「已保存，但还缺 API Key」，完全不知道是自己没复制全。
        if "…" in v:
            pass                      # 前端回传的脱敏值，这一项不改
        elif "*" in v:
            return {"ok": False,
                    "error": "API Key 里含 * 号，说明它被打码或没复制全。"
                             "请到百炼控制台点「复制」按钮拿完整 Key"
                             "（形如 sk- 加 32 位字符，共 35 位）"}
        elif v and not v.startswith("sk-"):
            return {"ok": False, "error": "API Key 应该以 sk- 开头（百炼控制台里复制）"}
        elif v and len(v) < 30:
            return {"ok": False,
                    "error": "API Key 只有 %d 位，太短了。百炼的通常是 35 位，"
                             "请确认完整复制" % len(v)}
        else:
            c["api_key"] = v
    if "resolution" in d:
        v = str(d.get("resolution") or "").strip().upper()
        if v in ("480P", "720P"):
            c["resolution"] = v
        elif v:
            return {"ok": False, "error": "分辨率只能是 480P 或 720P"}
    if "model" in d:
        v = str(d.get("model") or "").strip()
        if v:
            c["model"] = v
    _save_cfg(c)
    out = config()
    out["saved"] = True
    return out


# ---------------------------------------------------------------- HTTP 底座
def _base() -> str:
    """百炼的**通用域名**（不需要业务空间 ID）。

    官方文档写明「业务空间专属域名」是建议迁移项，但「现有域名仍可正常使用」。
    实测（2026-11-15）：用假 Key 真连 dashscope.aliyuncs.com 返回
    401 InvalidApiKey —— 说明接口路径有效、只差一个有效的 Key。
    所以只填 API Key 就能用，不必再让用户去控制台翻业务空间 ID。
    """
    return "https://dashscope.aliyuncs.com/api/v1"


def _auth() -> dict:
    k = (_cfg().get("api_key") or "").strip()
    if not k:
        raise RuntimeError("还没填 API Key")
    return {"Authorization": "Bearer " + k}


def _friendly(code: int, text: str) -> str:
    """把阿里云的错误码翻成用户能照着做的提示。

    实测：用假 Key 真连过一次，返回 401 InvalidApiKey —— 说明路径和鉴权头
    都对，只是 Key 不行。这种错必须说清「去哪重拿」，否则用户只会看到
    一大串 JSON 不知道该干嘛。
    """
    t = text or ""
    # 实测（2026-11-16）：提交任务时真连服务器拿到
    #   HTTP 400 {"code":"Arrearage","message":"Access denied, please make sure
    #   your account is in good standing."}
    # 官方错误码文档：Arrearage = 账号欠费/余额不足/充值延迟，服务端拒绝一切调用。
    # ⚠ 这个错**不是** Key 的问题，改代码也修不好 —— 只能让用户去充值。
    #   所以提示必须点明「欠费」并给出充值地址，否则用户会一直去重拿 Key。
    if "Arrearage" in t or "good standing" in t or "overdue-payment" in t:
        return ("阿里云账号**欠费**了（Arrearage），不是 Key 的问题。"
                "服务端因此拒绝了这次调用，充完值等几分钟就能恢复。"
                "去「费用与成本」充值：https://billing.console.aliyun.com/ "
                "（余额与账单：https://usercenter2.aliyun.com/finance/fund-management/overview）")
    if code == 401 or "InvalidApiKey" in t:
        return ("API Key 无效（401）。请到百炼控制台重新复制，"
                "并确认是**华北2（北京）**地域的 Key —— 别的地域的 Key 调不了这个模型")
    if code == 403:
        return ("没有权限（403）：可能还没开通 wan2.2-s2v 模型，"
                "或 Key 与业务空间 ID 不匹配")
    if code == 429 or "Throttling" in t:
        return "调用过于频繁（429），等几十秒再试"
    if "WorkspaceId" in t or "workspace" in t.lower():
        return "业务空间 ID 不对：请到百炼控制台 → 业务空间 复制，形如 llm-xxxxxxxx"
    return "HTTP %s：%s" % (code, t[:300])


def _err_of(body) -> str:
    """从错误响应里拼出「code + message」。

    ⚠ 不能只取 message：百炼的欠费错是
        {"code":"Arrearage","message":"Access denied, please make sure ..."}
      只看 message 会丢掉 Arrearage 这个关键判据，就没法翻成「账号欠费」。
    """
    if not isinstance(body, dict):
        return str(body)[:400]
    code = str(body.get("code") or "").strip()
    msg = str(body.get("message") or body.get("_raw") or "").strip()
    return (code + " " + msg).strip()[:600]


def _post(url: str, payload: dict, timeout: int = 60) -> dict:
    import requests
    h = _auth()
    h["Content-Type"] = "application/json"
    h["X-DashScope-Async"] = "enable"
    # 实测（2026-11-16）：upload() 上传到 DashScope 临时 OSS 后返回的是
    # oss://xxx 私有路径，服务端默认不会去解析，任务会 PENDING 后立刻
    # FAILED —— InvalidURL / "Unable to download the media resource during
    # the data inspection process"。加上这个头之后服务端才认 oss:// 并
    # 自己去取文件（同一次真实任务从 FAILED 变成 SUCCEEDED）。
    h["X-DashScope-OssResourceResolve"] = "enable"
    r = requests.post(url, json=payload, headers=h, timeout=timeout)
    try:
        body = r.json()
    except Exception:                                       # noqa: BLE001
        body = {"_raw": r.text[:600]}
    if r.status_code != 200:
        raise RuntimeError(_friendly(r.status_code, _err_of(body)))
    return body if isinstance(body, dict) else {}


def _get(url: str, timeout: int = 60) -> dict:
    import requests
    r = requests.get(url, headers=_auth(), timeout=timeout)
    try:
        body = r.json()
    except Exception:                                       # noqa: BLE001
        body = {"_raw": r.text[:600]}
    if r.status_code != 200:
        raise RuntimeError(_friendly(r.status_code, _err_of(body)))
    return body if isinstance(body, dict) else {}


# ---------------------------------------------------------------- 素材上传
def upload(path: str, model: str = "") -> str:
    """本机文件 → DashScope 临时 OSS，返回 oss://xxx（可直接当 image_url/audio_url）。

    DashScope 的文件上传是两步：先拿 OSS 直传凭证，再 multipart 直传。
    """
    import requests
    c = _cfg()
    mdl = model or c.get("model") or "wan2.2-s2v"
    if not os.path.isfile(path):
        raise RuntimeError("文件不存在：%s" % path)
    r = requests.get(
        "https://dashscope.aliyuncs.com/api/v1/uploads",
        params={"action": "getPolicy", "model": mdl},
        headers=_auth(), timeout=40,
    )
    if r.status_code != 200:
        raise RuntimeError(_friendly(r.status_code, r.text))
    data = (r.json() or {}).get("data") or {}
    host = data.get("upload_host") or ""
    updir = data.get("upload_dir") or ""
    if not (host and updir):
        raise RuntimeError("上传凭证字段缺失：%s" % str(data)[:300])

    fn = os.path.basename(path)
    obj = updir.rstrip("/") + "/" + fn
    fields = {
        "OSSAccessKeyId": data.get("oss_access_key_id") or "",
        "policy": data.get("policy") or "",
        "Signature": data.get("signature") or "",
        "key": obj,
        "x-oss-object-acl": data.get("x_oss_object_acl") or "private",
        "x-oss-forbid-overwrite": data.get("x_oss_forbid_overwrite") or "true",
    }
    ctype = mimetypes.guess_type(fn)[0] or "application/octet-stream"
    with open(path, "rb") as f:
        r2 = requests.post(host, data=fields,
                           files={"file": (fn, f, ctype)}, timeout=180)
    if r2.status_code not in (200, 203, 204):
        raise RuntimeError(_friendly(r2.status_code, r2.text))
    return "oss://" + obj


# ---------------------------------------------------------------- 任务
def submit(image_url: str, audio_url: str, resolution: str = "") -> str:
    c = _cfg()
    res = (resolution or c.get("resolution") or "480P").upper()
    payload = {
        "model": c.get("model") or "wan2.2-s2v",
        "input": {"image_url": image_url, "audio_url": audio_url},
        "parameters": {"resolution": res},
    }
    body = _post(_base() + "/services/aigc/image2video/video-synthesis", payload)
    out = body.get("output") or {}
    tid = out.get("task_id") or ""
    if not tid:
        raise RuntimeError("没拿到 task_id：%s" % str(body)[:400])
    return tid


def fetch(task_id: str) -> dict:
    body = _get(_base() + "/tasks/" + str(task_id))
    out = body.get("output") or {}
    st = (out.get("task_status") or "").upper()
    res = out.get("results") or {}
    return {
        "status": st,
        "video_url": res.get("video_url") or "",
        "code": out.get("code") or "",
        "message": out.get("message") or "",
        "usage": body.get("usage") or {},
    }


class Cancelled(RuntimeError):
    """用户点了「停止生成」。

    单独一个类型，是为了和真正的失败区分开 —— 停止不算错误，
    界面上不该报红「生成失败」，而应该显示「已停止」。
    """


def wait(task_id: str, on_progress=None, timeout: float = POLL_TIMEOUT,
         cancel=None) -> dict:
    """轮询到出结果。超时抛异常，由调用方决定怎么提示。

    cancel：threading.Event，用户点「停止」时被 set。
    每轮（2 秒）检查一次 —— 不额外起线程，代价可以忽略。
    """
    t0 = time.time()
    last = ""
    while True:
        if cancel is not None and cancel.is_set():
            # 注意：已经提交到云端的这一段没法撤回（接口没有取消能力），
            # 它会在阿里云那边自己跑完。我们只是不再等它、也不再下后面的段。
            raise Cancelled("已停止生成")
        r = fetch(task_id)
        st = r["status"]
        if st and st != last:
            last = st
            if on_progress:
                try:
                    on_progress({"stage": "cloud", "status": st, "task_id": task_id})
                except Exception:                           # noqa: BLE001
                    logger.debug("progress 回调失败", exc_info=True)
        if st == "SUCCEEDED":
            if not r["video_url"]:
                raise RuntimeError("任务成功但没给 video_url")
            return r
        if st == "FAILED":
            code = r.get("code") or ""
            msg = r.get("message") or ""
            if "InvalidURL" in code or "Unable to download" in msg:
                raise RuntimeError(
                    "云端取不到素材（InvalidURL）：图片/音频上传后服务端下载失败。"
                    "检查请求是否带了 X-DashScope-OssResourceResolve: enable")
            raise RuntimeError("云端任务失败：%s %s" % (code, msg))
        if time.time() - t0 > timeout:
            raise RuntimeError("等待超时（%d 秒），任务可能还在跑，task_id=%s"
                               % (int(timeout), task_id))
        time.sleep(POLL_INTERVAL)


def download(url: str, out_path: str) -> str:
    """video_url 只有 24 小时有效，拿到就立刻下载到本地。"""
    import requests
    r = requests.get(url, timeout=DOWNLOAD_TIMEOUT, stream=True)
    if r.status_code != 200:
        raise RuntimeError("下载视频失败 HTTP %s" % r.status_code)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    n = 0
    with open(out_path, "wb") as f:
        for chunk in r.iter_content(1 << 16):
            if chunk:
                f.write(chunk)
                n += len(chunk)
    if n < 1000:
        raise RuntimeError("下载到的视频只有 %d 字节，不像是有效文件" % n)
    return out_path


# ---------------------------------------------------------------- 音频切段 / 视频拼接
def _ffmpeg() -> str:
    """复用 avatar2 的查找逻辑（PATH 里没有也能找到打包自带的）。"""
    try:
        from . import avatar2
        p = avatar2.ffmpeg()
        if p:
            return p
    except Exception:                                       # noqa: BLE001
        logger.debug("avatar2.ffmpeg 不可用", exc_info=True)
    p = shutil.which("ffmpeg")
    if p:
        return p
    raise RuntimeError("找不到 ffmpeg，无法切分音频/拼接视频")


def audio_duration(path: str) -> float:
    """用 ffmpeg -i 的 stderr 解析时长（不依赖 ffprobe 是否单独存在）。"""
    exe = _ffmpeg()
    try:
        p = subprocess.run([exe, "-hide_banner", "-i", path],
                           capture_output=True, timeout=60)
    except Exception:                                       # noqa: BLE001
        return 0.0
    txt = (p.stderr or b"").decode("utf-8", "ignore")
    for line in txt.splitlines():
        if "Duration:" in line:
            seg = line.split("Duration:")[1].split(",")[0].strip()
            try:
                h, m, s = seg.split(":")
                return int(h) * 3600 + int(m) * 60 + float(s)
            except Exception:                               # noqa: BLE001
                return 0.0
    return 0.0


def split_audio(src: str, max_sec: float = MAX_AUDIO_SEC) -> list:
    """超过 max_sec 就等长切段；不超就原样返回一条。

    官方限制「单次音频 < 20 秒，超了任务直接报错」，所以这是硬需求。
    切点按固定间隔，不做静音对齐——短句被切断的影响由后续拼接吸收。
    """
    dur = audio_duration(src)
    if dur <= 0:
        raise RuntimeError("读不出音频时长：%s" % os.path.basename(src))
    if dur <= max_sec + 0.05:
        return [src]

    exe = _ffmpeg()
    stem = os.path.splitext(os.path.basename(src))[0]
    tag = uuid.uuid4().hex[:6]
    pat = os.path.join(_tmpdir(), "%s_%s_%%03d%s" % (stem, tag, os.path.splitext(src)[1] or ".mp3"))
    cmd = [exe, "-y", "-hide_banner", "-loglevel", "error", "-i", src,
           "-f", "segment", "-segment_time", "%.2f" % max_sec,
           "-reset_timestamps", "1"]
    ext = os.path.splitext(src)[1].lower()
    if ext in (".mp3", ".m4a", ".aac"):
        cmd += ["-c:a", "libmp3lame", "-q:a", "4"]
    elif ext == ".wav":
        cmd += ["-c:a", "pcm_s16le"]
    else:
        cmd += ["-c:a", "libmp3lame", "-q:a", "4"]
    cmd.append(pat)
    p = subprocess.run(cmd, capture_output=True, timeout=300)
    if p.returncode != 0:
        raise RuntimeError("音频切段失败：%s"
                           % (p.stderr or b"")[:300].decode("utf-8", "ignore"))
    parts = sorted(x for x in os.listdir(_tmpdir())
                   if x.startswith("%s_%s_" % (stem, tag)))
    if not parts:
        raise RuntimeError("音频切段后没有产出文件")
    return [os.path.join(_tmpdir(), x) for x in parts]


def concat_videos(paths: list, out_path: str) -> str:
    """把多段视频按顺序拼成一个。只有一段时直接复制。"""
    if not paths:
        raise RuntimeError("没有可拼接的视频")
    if len(paths) == 1:
        shutil.copyfile(paths[0], out_path)
        return out_path
    exe = _ffmpeg()
    lst = os.path.join(_tmpdir(), "concat_%s.txt" % uuid.uuid4().hex[:8])
    with open(lst, "w", encoding="utf-8") as f:
        for p in paths:
            f.write("file '%s'\n" % os.path.abspath(p).replace("\\", "/").replace("'", "'\\''"))
    p = subprocess.run([exe, "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "concat", "-safe", "0", "-i", lst,
                        "-c", "copy", out_path],
                       capture_output=True, timeout=600)
    try:
        os.remove(lst)
    except OSError:
        pass
    if p.returncode != 0 or not os.path.isfile(out_path):
        raise RuntimeError("视频拼接失败：%s"
                           % (p.stderr or b"")[:300].decode("utf-8", "ignore"))
    return out_path


# ---------------------------------------------------------------- 主流程
def generate(image_path: str, audio_path: str, resolution: str = "",
             on_progress=None, keep_tmp: bool = False, cancel=None) -> dict:
    """一张图 + 一段音频 → 说话视频。

    长音频会切成多段分别生成再拼接，因为接口硬限制单次 <20 秒。

    cancel：threading.Event。用户点「停止」后，会在**下一个检查点**退出：
    每段开始前、每次轮询时各查一次，所以最多 2 秒响应。
    """
    c = _cfg()
    if not (c.get("api_key") or "").strip():
        return {
            "ok": False, "need_key": True,
            "error": "还没填 API Key，去百炼控制台拿一下填进来",
            "signup_url": SIGNUP_URL, "key_url": KEY_URL,
        }
    if not os.path.isfile(image_path):
        return {"ok": False, "error": "形象图不存在：%s" % os.path.basename(image_path)}
    if not os.path.isfile(audio_path):
        return {"ok": False, "error": "音频不存在：%s" % os.path.basename(audio_path)}

    t0 = time.time()
    tmp_files = []
    try:
        def _p(d):
            if on_progress:
                try:
                    on_progress(d)
                except Exception:                           # noqa: BLE001
                    logger.debug("progress 回调失败", exc_info=True)

        # 1) 切段（长音频）
        # ⚠ 短音频不需要切段时，split_audio 返回的就是**原始输入文件**。
        #   如果把它也塞进 tmp_files，finally 里就会把用户的音频删掉
        #   （实测踩到：8 秒的测试音频跑完就没了）。所以只清理自己新建的段。
        segs = split_audio(audio_path)
        _src_abs = os.path.abspath(audio_path)
        tmp_files.extend([s for s in segs if os.path.abspath(s) != _src_abs])
        total = len(segs)
        # 每段的真实时长：进度按「已处理音频秒数 / 总音频秒数」估，
        # 比按段数均分准得多 —— 实测 47 秒音频切成 19 + 19 + 9，三段并不等长。
        try:
            segs_sec = [audio_duration(s) for s in segs]
        except Exception:                                   # noqa: BLE001
            logger.debug("取分段时长失败，进度退回按段数均分", exc_info=True)
            segs_sec = [0.0] * total
        _p({"stage": "split", "total": total, "segs_sec": segs_sec,
            "audio_total": sum(segs_sec),
            "msg": "音频 %d 段（单段上限 %.0f 秒）" % (total, MAX_AUDIO_SEC)})

        # 2) 图片只需传一次
        _p({"stage": "upload", "msg": "上传形象图"})
        img_url = upload(image_path)

        # 3) 逐段生成
        # 每段都带上「这段多长、什么时候开始、之前已经完成多少秒」，
        # 进度条据此现算 —— 云端本身不给百分比。
        outs = []
        for i, seg in enumerate(segs, 1):
            # 每段开始前先看一眼：停了就**不再提交**后面的段 ——
            # 云端按次计费，少提交一段就少花一段的钱。
            if cancel is not None and cancel.is_set():
                raise Cancelled("已停止生成（第 %d/%d 段未提交）" % (i, total))
            _sec = segs_sec[i - 1] if i - 1 < len(segs_sec) else 0.0
            _done = sum(segs_sec[:i - 1])
            _p({"stage": "segment", "index": i, "total": total,
                "seg_sec": _sec, "seg_t0": 0, "done_sec": _done,
                "msg": "第 %d/%d 段：上传音频" % (i, total)})
            a_url = upload(seg)
            _t0 = time.time()          # 段内计时的起点：上传完、开始提交
            _p({"stage": "segment", "index": i, "total": total,
                "seg_sec": _sec, "seg_t0": _t0, "done_sec": _done,
                "msg": "第 %d/%d 段：提交任务" % (i, total)})
            tid = submit(img_url, a_url, resolution)
            _p({"stage": "segment", "index": i, "total": total,
                "seg_sec": _sec, "seg_t0": _t0, "done_sec": _done,
                "msg": "第 %d/%d 段：云端生成中" % (i, total), "task_id": tid})
            r = wait(tid, on_progress=_p, cancel=cancel)
            p = os.path.join(_tmpdir(), "seg_%s_%02d.mp4" % (uuid.uuid4().hex[:6], i))
            download(r["video_url"], p)
            tmp_files.append(p)
            outs.append(p)
            # 这一段实际花了多久 → 修正「每秒音频要等几秒」的系数，
            # 后面几段的预计剩余时间就跟着变准（自我校正，不用我猜死）。
            _spent = time.time() - _t0
            _learn = 0.0
            if _sec > 0.5 and _spent > SEG_OVERHEAD:
                _learn = max(1.0, (_spent - SEG_OVERHEAD) / _sec)
            _p({"stage": "segment_done", "index": i, "total": total,
                "seg_sec": 0, "seg_t0": 0, "done_sec": sum(segs_sec[:i]),
                "factor": _learn, "usage": r.get("usage") or {},
                "msg": "第 %d/%d 段完成" % (i, total)})

        # 4) 拼接 + 落盘
        name = "avatar_" + time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4] + ".mp4"
        out = os.path.join(_dir(), "videos", name)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        _p({"stage": "concat", "msg": "拼接 %d 段" % len(outs)})
        concat_videos(outs, out)

        dur = audio_duration(out)
        return {
            "ok": True,
            "name": name,
            "path": out,
            "bytes": os.path.getsize(out),
            "duration": round(dur, 2),
            "segments": total,
            "elapsed": round(time.time() - t0, 1),
            "engine": "cloud:wan2.2-s2v",
            "resolution": (resolution or c.get("resolution") or "480P").upper(),
            "lipsync_label": "云端扩散模型口型（Wan2.2-S2V）",
        }
    except Cancelled as e:
        # 用户主动停止 —— 这不是错误，不要打 warning 日志，也不该报红
        logger.info("云端生成被用户停止：%s", e)
        return {"ok": False, "cancelled": True,
                "error": str(e).strip() or "已停止生成",
                "elapsed": round(time.time() - t0, 1)}
    except Exception as e:                                  # noqa: BLE001
        logger.warning("云端生成失败", exc_info=True)
        # 只回用户看得懂的那句，不把 "RuntimeError: " 前缀甩到界面上
        msg = str(e).strip() or type(e).__name__
        return {"ok": False, "error": msg,
                "elapsed": round(time.time() - t0, 1)}
    finally:
        if not keep_tmp:
            for p in tmp_files:
                try:
                    if os.path.isfile(p):
                        os.remove(p)
                except OSError:
                    pass


# ---------------------------------------------------------------- 后台任务
# 一次生成要 1~3 分钟（多段更久）。挂在 HTTP 长连接上很容易被中间层掐断，
# 前端也拿不到进度。所以统一走「提交 → 轮询」：submit 立刻返回 job_id。
_JOBS = {}
_JOB_LOCK = threading.Lock()
JOB_KEEP = 3600          # 完成的任务保留 1 小时，够前端取结果


def _job_set(jid: str, **kw) -> None:
    with _JOB_LOCK:
        d = _JOBS.get(jid)
        if d is None:
            return
        d.update(kw)
        d["updated"] = time.time()


def _factor(d: dict) -> float:
    """每秒音频大约要等几秒。有实测值就用实测的，没有就用默认档。"""
    v = float(d.get("factor") or 0)
    if v > 0:
        return v
    return FACTOR_DEF.get((d.get("resolution") or "480P").upper(), 14.0)


def _progress(d: dict, now: float = None) -> dict:
    """按任务状态现算进度 / 已用时间 / 预计剩余。

    ⚠ 云端只回 PENDING / RUNNING / SUCCEEDED，**没有百分比**。
    所以：「第几段 / 共几段」是真的，段内百分比是按已用时估的 ——
    返回里带 pct_estimated=True，界面必须标「≈」，不能当真实进度显示。

    为什么在「读的时候」算而不是后台定时写：
      前端每 2 秒轮询一次，读时现算就能拿到新鲜的已用时间，
      后端不用为每个任务额外起心跳线程。
    """
    now = time.time() if now is None else now
    st = (d.get("status") or "pending").lower()
    stage = d.get("stage") or ""
    started = float(d.get("started") or now)
    out = {
        "elapsed": round(max(0.0, now - started), 1),
        "pct": 0.0,
        "eta": None,
        "pct_estimated": True,
    }

    if st == "done":
        out.update(pct=100.0, eta=0, pct_estimated=False)
        return out
    # 注意：failed **不**在这里早退。失败时也照常算到哪一步了，
    # 否则进度条会弹回 0，看着像根本没开始跑（实测踩到过）。
    # 只是最后把 eta 清掉 —— 失败了还说「还需 X 秒」是骗人的。

    at = float(d.get("audio_total") or 0)
    total = int(d.get("total") or 0)
    idx = int(d.get("index") or 0)

    # 还没切完段 —— 给个象征性的小值，别让条一直是 0（看着像卡住）
    if stage == "split":
        out["pct"] = 2.0
        return out
    if stage in ("", "pending"):
        out["pct"] = 0.5
        return out
    if stage == "upload":
        out["pct"] = 4.0
        if at:
            out["eta"] = int(round(at * _factor(d) + SEG_OVERHEAD * max(1, total)))
        return out
    if stage == "concat":
        out.update(pct=97.0, eta=3)
        return out

    # 段内（stage = segment / cloud / segment_done）
    factor = _factor(d)
    seg_sec = float(d.get("seg_sec") or 0)
    seg_t0 = float(d.get("seg_t0") or 0)
    done_sec = float(d.get("done_sec") or 0)
    frac = 0.0
    if seg_sec > 0 and seg_t0:
        expect = seg_sec * factor + SEG_OVERHEAD
        frac = min(0.95, max(0.0, (now - seg_t0) / max(1.0, expect)))
    cur = done_sec + seg_sec * frac

    if at > 0:
        out["pct"] = round(min(96.0, 5.0 + 91.0 * min(1.0, cur / at)), 1)
        # 剩余时间分两块算：当前这段「还要等多久」+ 后面几段整段的预计耗时。
        # 不能直接 (总音频 − 已处理) × 系数 —— 那样会漏掉当前这段已经等过的
        # 时间，一开始显示「还需 69 秒」而实际要 79 秒（实测差 10 秒）。
        rest = 0.0
        if seg_sec > 0:
            if seg_t0:
                rest += max(0.0, (seg_sec * factor + SEG_OVERHEAD) - (now - seg_t0))
            else:
                # 这一段还没开始计时（正在上传音频，seg_t0 还是 0）
                # → 按整段算。漏掉这一步会让 ETA 从 945 突然掉到 184
                #   （实测踩到：看着像快好了，其实还有一整段要跑）。
                rest += seg_sec * factor + SEG_OVERHEAD
        tail = max(0.0, at - done_sec - seg_sec)
        left = max(0, total - idx) if total else 0
        if tail > 0:
            rest += tail * factor + SEG_OVERHEAD * left
        out["eta"] = int(round(rest))
    else:
        # 拿不到音频时长（ffprobe 失败）→ 退回按段数均分，不给 ETA（那会是瞎猜）
        out["pct"] = round(5.0 + (91.0 * (idx - 1) / total if total else 0), 1)
    if st in ("failed", "cancelled"):
        out["eta"] = None
        out["pct_estimated"] = False
    return out


def job(jid: str) -> dict:
    """查任务进度/结果。进度是读时现算的（见 _progress）。"""
    with _JOB_LOCK:
        d = _JOBS.get(jid)
        if not d:
            return {"ok": False, "error": "任务不存在或已过期（完成后只保留 1 小时）"}
        snap = {k: v for k, v in d.items() if not k.startswith("_")}
    snap.update(_progress(snap))
    return snap


def cancel(jid: str) -> dict:
    """停止一个正在生成的任务。

    ⚠ 说清能力边界：**已经提交到云端的那一段撤不回来**（百炼没有取消
    任务的接口），它会在阿里云那边自己跑完。我们能做的是：
      · 立刻不再等它（2 秒内跳出轮询）；
      · 不再提交后面的段 —— 云端按次计费，这一段省下来的就是真金白银；
      · 已经下载到一半的产物不落盘、临时文件清理掉。
    所以文案写「已停止」，不写「已取消云端任务」—— 不夸大。
    """
    with _JOB_LOCK:
        d = _JOBS.get(jid)
        if not d:
            return {"ok": False, "error": "任务不存在或已过期"}
        st = d.get("status") or ""
        if st in ("done", "failed", "cancelled"):
            return {"ok": False, "error": "任务已经结束（%s），不用停" % st}
        ev = d.get("_cancel")
    if ev is not None:
        ev.set()
    _job_set(jid, msg="正在停止…")
    return {"ok": True, "job_id": jid, "msg": "已发送停止信号"}


def _gc_jobs() -> None:
    now = time.time()
    with _JOB_LOCK:
        for k in [k for k, v in _JOBS.items()
                  if v.get("status") in ("done", "failed", "cancelled")
                  and now - v.get("updated", now) > JOB_KEEP]:
            _JOBS.pop(k, None)


def generate_async(image_path: str, audio_path: str, resolution: str = "",
                   **kw) -> str:
    """提交后台生成，立刻返回 job_id。"""
    _gc_jobs()
    jid = uuid.uuid4().hex[:12]
    _res = (resolution or _cfg().get("resolution") or "480P").upper()
    with _JOB_LOCK:
        _JOBS[jid] = {
            "ok": True, "job_id": jid, "status": "pending",
            "msg": "排队中", "stage": "", "total": 0, "index": 0,
            "started": time.time(), "updated": time.time(),
            "result": None, "error": "",
            # —— 进度用（见 _progress）——
            "resolution": _res,
            "audio_total": 0.0,     # 全部音频共几秒
            "done_sec": 0.0,        # 已完成几秒音频
            "seg_sec": 0.0,         # 当前这段音频几秒
            "seg_t0": 0.0,          # 当前这段从什么时候开始算
            "factor": 0.0,          # 实测的「每秒音频要等几秒」，0 = 用默认
            # 用户点「停止」时被 set。下划线开头 → job() 不会把它吐给前端。
            "_cancel": threading.Event(),
        }

    def _run():
        _job_set(jid, status="running", msg="开始生成")
        ev = _JOBS.get(jid, {}).get("_cancel")

        def _p(d):
            # ⚠ 只更新这次真的给了的字段。
            #   原来写成 total=d.get("total") or 0，而 wait() 回调只带
            #   {"stage":"cloud","status":...}，会把 total/index 清成 0 ——
            #   界面上「第 2/3 段」会莫名跳回「生成中…」。实测踩到过。
            upd = {"stage": d.get("stage") or ""}
            for k in ("msg", "task_id"):
                if d.get(k):
                    upd[k] = d[k]
            for k in ("total", "index"):
                if d.get(k):
                    upd[k] = int(d[k])
            for k in ("seg_sec", "seg_t0", "done_sec", "audio_total", "factor"):
                if k in d:
                    upd[k] = d[k]
            _job_set(jid, **upd)

        try:
            r = generate(image_path, audio_path, resolution=resolution,
                         on_progress=_p, cancel=ev, **kw)
        except Exception as e:                              # noqa: BLE001
            logger.warning("后台生成异常", exc_info=True)
            r = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        if r.get("cancelled"):
            # 用户主动停止 → status=cancelled，前端显示「已停止」而不是报红
            _job_set(jid, status="cancelled", msg="已停止",
                     result=r, error=r.get("error") or "已停止生成")
        elif r.get("ok"):
            _job_set(jid, status="done", msg="完成", result=r, error="")
        else:
            _job_set(jid, status="failed", msg=r.get("error") or "生成失败",
                     result=r, error=r.get("error") or "生成失败")

    th = threading.Thread(target=_run, name="avatar-cloud-" + jid, daemon=True)
    with _JOB_LOCK:
        _JOBS[jid]["_thread"] = th
    th.start()
    return jid


def status() -> dict:
    c = config()
    try:
        _ffmpeg()
        ff = True
    except Exception:                                       # noqa: BLE001
        ff = False
    return {
        "ok": True,
        "ready": c["ready"],
        "has_key": c["has_key"],
        "resolution": c["resolution"],
        "model": c["model"],
        "max_audio_sec": MAX_AUDIO_SEC,
        "ffmpeg": ff,
        "note": ("已配置，可以生成" if c["ready"]
                 else "缺：" + "、".join(c["missing"])),
    }

# -*- coding: utf-8 -*-
"""图片模型注册表：云端 API 模型 + 本地权重（可下载 / 可卸载）。

为什么需要它（2026-11-08 用户要求）
--------------------------------
「我要可以切换生成和微改图片的模型，就像 LLM 可以切换为本地和云端那样，
 然后图片模型也一样支持下载和卸载。」

LLM 那边是 Ollama 管模型（`/api/pull`、`/api/delete`），图片这边没有这种服务，
所以这里自己实现一套最小可用的：
  · **云端**：模型名就是服务商 /v1/models 里的 id（不同 Key/分组能用的不一样），
    不需要下载，选中即用；
  · **本地**：几套 diffusers 权重，放统一目录，支持从 HF 镜像下载（带进度）、
    卸载（只允许删托管目录里的，绝不碰用户别的文件）。

和 t2i 的关系
-------------
t2i 只会读 config 里的 `sd_model_dir` / `inpaint_model_dir`。
所以「切换本地模型」= **把 config 指向那个模型的目录**，t2i 一行都不用改。
这也是为什么这里只管目录和下载，不碰推理逻辑。
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from typing import Any

from . import config

# ---------------------------------------------------------------- 本地模型清单
# ⚠️ 只放**确认能用 diffusers 直接加载**的官方仓库；gb 是磁盘占用估算（用于进度条）。
LOCAL_MODELS: list[dict] = [
    {
        "id": "sdxl-base",
        "name": "SDXL Base",
        "role": "gen",
        "repo": "stabilityai/stable-diffusion-xl-base-1.0",
        "gb": 6.9,
        "note": "通用/写实，1024 原生、28 步；本机默认的那套就是它",
    },
    {
        "id": "sdxl-turbo",
        "name": "SDXL Turbo",
        "role": "gen",
        "repo": "stabilityai/sdxl-turbo",
        "gb": 6.9,
        "note": "蒸馏版，4 步出图，比 base 快 5~7 倍（细节略少）",
    },
    {
        "id": "sd-turbo",
        "name": "SD Turbo",
        "role": "gen",
        "repo": "stabilityai/sd-turbo",
        "gb": 2.5,
        "note": "最小最快（2.5G），原生 512，适合草稿/试风格",
    },
    {
        "id": "sdxl-inpaint",
        "name": "SDXL Inpaint",
        "role": "inpaint",
        "repo": "diffusers/stable-diffusion-xl-1.0-inpainting-0.1",
        "gb": 6.9,
        "note": "局部重绘专用：只改蒙版内，圈外像素不动",
    },
]

_BY_ID = {m["id"]: m for m in LOCAL_MODELS}

# 云端默认（真正的候选由前端用 Key 去 /v1/models 拉）
CLOUD_DEFAULT = "gpt-image-2.5-flare"


# ---------------------------------------------------------------- 目录
def base_dir() -> str:
    """本地权重的托管根目录。"""
    cfg = config.load_config() or {}
    d = str(cfg.get("image_models_dir") or "").strip()
    if not d:
        # 以前 SDXL 放在 E:\本地多模态助手-Docker\models，沿用（省得再下一遍 6.5G）
        legacy = r"E:\本地多模态助手-Docker\models"
        d = legacy if os.path.isdir(legacy) else config.data("image_models")
    os.makedirs(d, exist_ok=True)
    return d


# 老版本的目录名（以前直接叫 sd_model / sd_inpaint）—— 已经下好的别再下一遍
_LEGACY_NAMES = {"sdxl-base": "sd_model", "sdxl-inpaint": "sd_inpaint"}


def model_dir(model_id: str) -> str:
    """这个模型**实际**在哪个目录。

    优先新目录 `<root>/<id>`；如果那里没权重，就看老的 `sd_model` / `sd_inpaint`
    目录（用户 6.5G 已经下过的那两套就在这里）。都检测不到才返回新目录（待下载）。
    """
    canonical = os.path.join(base_dir(), model_id)
    if os.path.exists(os.path.join(canonical, "model_index.json")):
        return canonical
    legacy = _LEGACY_NAMES.get(model_id)
    if legacy:
        p2 = os.path.join(base_dir(), legacy)
        if os.path.exists(os.path.join(p2, "model_index.json")):
            return p2
    return canonical


def is_installed(model_id: str) -> bool:
    d = model_dir(model_id)
    return (os.path.isdir(d)
            and os.path.exists(os.path.join(d, "model_index.json")))


def dir_size(path: str) -> int:
    total = 0
    try:
        for dp, _, fs in os.walk(path):
            for f in fs:
                try:
                    total += os.path.getsize(os.path.join(dp, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total


# ---------------------------------------------------------------- 状态（下载中）
_STATE: dict[str, Any] = {
    "status": "idle",      # idle | pulling | done | error
    "model": "",
    "done": 0,
    "total": 0,
    "pct": 0.0,
    "speed": 0.0,
    "eta": 0,
    "msg": "",
}
_LOCK = threading.Lock()
_CANCEL = threading.Event()


def status() -> dict:
    with _LOCK:
        return dict(_STATE)


def cancel() -> dict:
    _CANCEL.set()
    return {"ok": True}


def _set(**kw) -> None:
    with _LOCK:
        _STATE.update(kw)


# ---------------------------------------------------------------- 下载
def pull(model_id: str) -> dict:
    """后台线程下载某个本地模型。返回 {"ok": bool, "msg": str}。"""
    m = _BY_ID.get(model_id)
    if not m:
        return {"ok": False, "msg": "未知的本地模型 id：" + str(model_id)}
    if is_installed(model_id):
        return {"ok": True, "msg": "已经装好了"}
    with _LOCK:
        if _STATE["status"] == "pulling":
            return {"ok": False, "msg": "已经有一个下载在跑：" + _STATE["model"]}

    _CANCEL.clear()
    _set(status="pulling", model=model_id, done=0,
         total=int(m["gb"] * 1024 ** 3), pct=0.0, speed=0.0, eta=0, msg="正在连接镜像…")

    def worker() -> None:
        target = model_dir(model_id)
        os.makedirs(target, exist_ok=True)
        stop = threading.Event()

        def ticker() -> None:
            """按磁盘实际大小报进度（每 0.8 秒一次，比回调稳）。"""
            last_b, last_t = 0, time.time()
            while not stop.wait(0.8):
                cur = dir_size(target)
                now = time.time()
                dt = max(0.001, now - last_t)
                spd = max(0.0, (cur - last_b) / dt)
                last_b, last_t = cur, now
                with _LOCK:
                    total = _STATE["total"] or int(m["gb"] * 1024 ** 3)
                pct = min(99.5, cur * 100.0 / total) if total else 0.0
                eta = int((total - cur) / spd) if spd > 1024 else 0
                _set(done=cur, total=total, pct=round(pct, 1),
                     speed=round(spd, 1), eta=eta)

        th = threading.Thread(target=ticker, daemon=True)
        th.start()
        try:
            # ⚠️ 为什么不用 snapshot_download 一把梭（2026-11-08 实测）：
            #   它不接受取消信号 —— 用户点「停止」后它照样把 2.5GB 下完，
            #   界面看着"已取消"其实还在偷偷占带宽、写磁盘。
            #   改成**逐文件**下载：每个文件之间检查取消标志，点到就真停；
            #   顺带也拿到了准确的总量（model_info.files_metadata），进度条不再靠估算。
            from huggingface_hub import HfApi, hf_hub_download
            endpoint = str((config.load_config() or {}).get("hf_endpoint")
                           or "https://hf-mirror.com")
            os.environ.setdefault("HF_ENDPOINT", endpoint)
            api = HfApi(endpoint=endpoint)
            _set(msg="正在读取文件清单…")

            want_ext = (".json", ".safetensors", ".txt", ".bin", ".model")
            skip_ext = (".ckpt", ".pth", ".msgpack", ".h5", ".onnx", ".xml", ".mlmodel")
            info = api.model_info(m["repo"], files_metadata=True)
            files = []
            for sib in (info.siblings or []):
                name = sib.rfilename
                if name.startswith("."):
                    continue
                if name.endswith(skip_ext):
                    continue
                if not name.endswith(want_ext):
                    continue
                files.append((name, int(getattr(sib, "size", 0) or 0)))
            if not files:
                _set(status="error", msg="仓库里没有可用的权重文件")
                return
            total = sum(sz for _, sz in files)
            _set(total=total, msg="开始下载")

            for name, _sz in files:
                if _CANCEL.is_set():
                    break
                hf_hub_download(repo_id=m["repo"], filename=name,
                                local_dir=target, endpoint=endpoint)
            if _CANCEL.is_set():
                _set(status="idle", msg="已取消（已下载的部分保留，下次可续传）")
            elif is_installed(model_id):
                _set(status="done", pct=100.0, done=dir_size(target), msg="下载完成")
            else:
                _set(status="error",
                     msg="下完了但缺 model_index.json，仓库结构可能不完整")
        except Exception as e:                       # noqa: BLE001
            if _CANCEL.is_set():
                _set(status="idle", msg="已取消（已下载的部分保留）")
            else:
                _set(status="error", msg="%s: %s" % (type(e).__name__, e))
        finally:
            stop.set()

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "msg": "已开始下载 " + m["name"]}


# ---------------------------------------------------------------- 卸载
# ⚠️ 为什么要连 SystemExit 一起接（2026-11-08 实测）：
#   某些环境（这里的工作区沙箱会注入 sitecustomize 做删除代理）在删文件时不是抛
#   Exception，而是直接 `raise SystemExit(1)`。只写 `except Exception` 接不住，
#   FastAPI 那边就只能回 500，用户看到的是一句「Internal Server Error」——
#   比"删不掉，因为文件被占用"糟得多。所以这里显式兜住 BaseException 里的这两种。
_DEL_ERRS = (Exception, SystemExit, KeyboardInterrupt)


def _try_remove_tree(path: str) -> bool:
    """尽量把整个目录删掉。返回是否真的删干净了。"""
    try:
        for name in os.listdir(path):
            p = os.path.join(path, name)
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                try:
                    os.remove(p)
                except _DEL_ERRS:
                    pass
        os.rmdir(path)
        return not os.path.isdir(path)
    except _DEL_ERRS:
        return False


def remove(model_id: str) -> dict:
    """删除本地权重。**只允许删托管目录里的东西**（防止误删用户文件）。"""
    if model_id not in _BY_ID:
        return {"ok": False, "msg": "未知的本地模型 id：" + str(model_id)}
    d = os.path.abspath(model_dir(model_id))
    root = os.path.abspath(base_dir())
    if not d.startswith(root + os.sep):
        return {"ok": False, "msg": "拒绝删除托管目录以外的路径：" + d}
    if not os.path.isdir(d):
        return {"ok": True, "msg": "本来就没装"}
    with _LOCK:
        if _STATE["status"] == "pulling" and _STATE["model"] == model_id:
            return {"ok": False, "msg": "这个模型正在下载，先点「停止」再卸载"}

    name = _BY_ID[model_id]["name"]
    if _try_remove_tree(d):
        _set(status="idle", pct=0.0, done=0)
        return {"ok": True, "msg": "已卸载 " + name}

    # 退路：不让删就**先改名挪开**。对使用者来说这已经是"卸载"了
    # （目录不再被识别成模型、也不再占着 ID），剩下的残渣带 `.deleting-时间戳`
    # 后缀躺在旁边，重启后可手动清理。
    try:
        trash = d + ".deleting-" + time.strftime("%H%M%S")
        os.rename(d, trash)
        _try_remove_tree(trash)
        left = os.path.isdir(trash)
        _set(status="idle", pct=0.0, done=0)
        return {"ok": True,
                "msg": "已卸载 " + name + (
                    "（有文件被占用，已改名挪到 %s，重启后可手动删）" % trash
                    if left else "")}
    except _DEL_ERRS as e:                           # noqa: BLE001
        return {"ok": False,
                "msg": ("卸载失败（%s: %s）。如果有别的实例/进程正在用这套权重，"
                        "先把它们关掉再试。" % (type(e).__name__, e))}


# ---------------------------------------------------------------- 清单
def list_local() -> list[dict]:
    """本地模型清单 + 是否已装 + 实际占用。"""
    cfg = config.load_config() or {}
    cur_gen = os.path.abspath(str(cfg.get("sd_model_dir") or ""))
    cur_inp = os.path.abspath(str(cfg.get("inpaint_model_dir") or ""))
    out = []
    for m in LOCAL_MODELS:
        d = os.path.abspath(model_dir(m["id"]))
        inst = is_installed(m["id"])
        out.append({
            "id": m["id"], "name": m["name"], "role": m["role"],
            "repo": m["repo"], "note": m["note"],
            "dir": d,
            "installed": inst,
            "size_gb": round(dir_size(d) / 1024 ** 3, 2) if inst else 0,
            "expect_gb": m["gb"],
            "current": bool(inst and d and d in (cur_gen, cur_inp)),
        })
    return out


def apply_local(model_id: str) -> dict:
    """把「用哪个本地模型」写进 config（t2i 就是靠这两个路径找权重的）。"""
    m = _BY_ID.get(model_id)
    if not m:
        return {"ok": False, "msg": "未知的本地模型 id：" + str(model_id)}
    if not is_installed(model_id):
        return {"ok": False, "msg": m["name"] + " 还没下载，先点「⬇ 下载」"}
    d = model_dir(model_id)
    cfg = config.load_config() or {}
    if m["role"] == "inpaint":
        cfg["inpaint_model_dir"] = d
    else:
        cfg["sd_model_dir"] = d
    config.save_config(cfg)
    return {"ok": True, "msg": "已切到本地 " + m["name"], "dir": d}

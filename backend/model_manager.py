# -*- coding: utf-8 -*-
"""Ollama 模型下载管理：后台线程 + 实时进度 + 取消 + 删除

为什么不用现有的 /api/pull（阻塞式）
------------------------------------
一个 30B 模型十几 GB，下载要几十分钟。HTTP 请求一直挂着会：
  1. 浏览器 / 反向代理超时断开（实测 60 秒就断）
  2. 用户看不到任何进度，以为卡死
  3. 没法取消，只能杀进程
所以改成：**后台线程下载 + 前端轮询 /api/model/status**。

进度怎么算（关键）
------------------
Ollama 按 layer 分别上报，多个 layer 是**并行**下载的：
  {"status":"downloading","digest":"sha256:aaa","total":123,"completed":45}
  {"status":"downloading","digest":"sha256:bbb","total":999,"completed":100}
如果直接拿最后一条算，进度会在 3% 和 70% 之间乱跳。
必须**按 digest 把每个 layer 的最新值存下来，然后累加**。

取消怎么实现
------------
Ollama 的 pull 不支持"暂停"，只支持断开连接（已下载的 layer 会保留在
blobs 里，下次 pull 自动断点续传）。所以：
  · 取消 = 设标志 → 下载线程在下一个 chunk 发现 → resp.close()
  · 界面上"暂停"和"取消"是同一个动作，文案上区分即可。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

import requests

OLLAMA = "http://127.0.0.1:11434"
# (连接超时, 读取超时)：大文件下载读取给足 1 小时
TIMEOUT = (10, 3600)

_LOCK = threading.Lock()

# 全局状态（单实例：同一时刻只允许一个下载任务）
_STATE: dict[str, Any] = {
    "model": "",
    "status": "idle",      # idle | pulling | verifying | done | error | cancelled
    "percent": 0.0,
    "completed": 0,
    "total": 0,
    "speed": 0.0,          # bytes/s
    "eta": 0,              # 剩余秒数
    "error": "",
    "cancel": False,
    "started_at": 0.0,
    "finished_at": 0.0,
}

_thread: threading.Thread | None = None


def _reset(model: str) -> None:
    with _LOCK:
        _STATE.update({
            "model": model, "status": "pulling", "percent": 0.0,
            "completed": 0, "total": 0, "speed": 0.0, "eta": 0,
            "error": "", "cancel": False,
            "started_at": time.time(), "finished_at": 0.0,
        })


def get_state() -> dict[str, Any]:
    with _LOCK:
        return dict(_STATE)


def is_running() -> bool:
    with _LOCK:
        return _STATE["status"] in ("pulling", "verifying")


def cancel() -> bool:
    """请求取消。下载线程会在下一个 chunk 主动断开（已下载部分保留）。"""
    with _LOCK:
        if _STATE["status"] not in ("pulling", "verifying"):
            return False
        _STATE["cancel"] = True
        return True


def pull(model: str) -> dict[str, Any]:
    """启动后台下载。已有任务在跑就直接返回当前状态。"""
    global _thread
    if is_running():
        return {"ok": False, "msg": "已有下载任务在进行", "state": get_state()}
    _reset(model)
    _thread = threading.Thread(target=_worker, args=(model,), daemon=True)
    _thread.start()
    return {"ok": True, "msg": f"已开始下载 {model}", "state": get_state()}


def _worker(model: str) -> None:
    layers: dict[str, tuple[int, int]] = {}
    last_t = time.time()
    last_c = 0
    try:
        resp = requests.post(
            f"{OLLAMA}/api/pull",
            json={"name": model, "stream": True},
            stream=True, timeout=TIMEOUT)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Ollama 返回 {resp.status_code}: {resp.text[:200]}")

        for raw in resp.iter_lines(decode_unicode=True):
            if not raw:
                continue
            # 每个 chunk 都检查一次取消
            with _LOCK:
                if _STATE["cancel"]:
                    try:
                        resp.close()
                    except Exception:
                        pass
                    _STATE["status"] = "cancelled"
                    _STATE["finished_at"] = time.time()
                    return
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue

            st = str(obj.get("status") or "")

            if obj.get("error"):
                raise RuntimeError(str(obj["error"]))

            # ⚠️⚠️ 关键修正（2026-11-08 实测）：
            # Ollama 的下载状态是 `"pulling <digest前缀>"`，**不是** `"downloading"`：
            #   {"status":"pulling c5396e06af29","digest":"sha256:...",
            #    "total":397807936,"completed":82393231}
            # 之前按 st == "downloading" 判断，一条都命中不了，进度永远 0%。
            # 改成「有 total + 有 digest」即视为下载中 —— 这个判据更稳，
            # 因为 Ollama 换文案（pulling / downloading / fetching）都不影响。
            if obj.get("total") and obj.get("digest"):
                d = obj.get("digest") or "?"
                layers[d] = (int(obj.get("total") or 0),
                             int(obj.get("completed") or 0))
                total = sum(t for t, _ in layers.values())
                done = sum(c for _, c in layers.values())
                now = time.time()
                dt = max(0.001, now - last_t)
                speed = max(0.0, (done - last_c) / dt)
                last_t, last_c = now, done
                eta = int((total - done) / speed) if speed > 0 and total > done else 0
                with _LOCK:
                    _STATE.update({
                        "percent": round(done / total * 100, 1) if total else 0.0,
                        "completed": done,
                        "total": total,
                        "speed": speed,
                        "eta": eta,
                        "status": "pulling",
                    })
            elif st == "pulling manifest":
                # 拉清单阶段：还没开始传数据，保持 pulling 但进度不动
                with _LOCK:
                    _STATE["status"] = "pulling"
            elif st in ("verifying sha256 digest", "writing manifest",
                        "removing any unused layers"):
                with _LOCK:
                    _STATE["status"] = "verifying"
                    _STATE["percent"] = 99.0
            elif st == "success":
                with _LOCK:
                    _STATE.update({"status": "done", "percent": 100.0,
                                   "finished_at": time.time()})
                return

        # 流结束但没收到 success：按完成处理（Ollama 有时不发 success）
        with _LOCK:
            _STATE.update({"status": "done", "percent": 100.0,
                           "finished_at": time.time()})
    except Exception as e:                        # noqa: BLE001
        with _LOCK:
            _STATE.update({"status": "error",
                           "error": str(e)[:300],
                           "finished_at": time.time()})


def remove(model: str) -> dict[str, Any]:
    """删除已下载的模型（释放磁盘）。"""
    if is_running() and get_state().get("model") == model:
        return {"ok": False, "msg": "该模型正在下载，先取消再删除"}
    try:
        r = requests.delete(f"{OLLAMA}/api/delete",
                            json={"name": model}, timeout=(8, 120))
        if r.status_code != 200:
            return {"ok": False, "msg": f"HTTP {r.status_code}: {r.text[:200]}"}
        return {"ok": True, "msg": f"已删除 {model}"}
    except requests.RequestException as e:
        return {"ok": False, "msg": f"连接 Ollama 失败：{e}"}


def installed() -> list[dict]:
    """已安装模型清单（含体积）。"""
    try:
        r = requests.get(f"{OLLAMA}/api/tags", timeout=(6, 20))
        d = r.json()
        out = []
        for m in (d.get("models") or []):
            out.append({
                "name": m.get("name", ""),
                "size": int(m.get("size") or 0),
                "modified_at": m.get("modified_at", ""),
            })
        return out
    except Exception:
        return []

# -*- coding: utf-8 -*-
"""数字人云端（阿里云百炼 Wan2.2-S2V）配置助手。

用法一（推荐，Key 不经过聊天、不留在命令历史）：
    python scripts/set_cloud_key.py

用法二（一条命令配好）：
    python scripts/set_cloud_key.py --key sk-xxxx --ws llm-xxxx

它会做四件事：
  1. 校验 Key / 业务空间 ID 的形态
  2. 写入 data/avatar2/cloud.json
  3. 真连一次阿里云验证 Key 是否有效
  4. 打印下一步该干什么

⚠️ 不会把 Key 明文打印出来（只显示前 6 位 + 后 4 位）。
"""
import argparse
import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(ROOT, "data", "avatar2", "cloud.json")

KEY_URL = "https://help.aliyun.com/zh/model-studio/get-api-key"
SIGNUP_URL = "https://bailian.console.aliyun.com/"


def ask_secret(prompt: str) -> str:
    """读一行敏感输入。

    ⚠️ 不能无条件用 getpass：在非交互终端（stdin 被重定向、IDE 输出窗口、
    管道）里，Windows 上的 getpass 会一直等控制台输入而挂死 —— 实测超时
    120 秒。所以先判断是不是真终端，不是就退回普通 input()。
    """
    try:
        if sys.stdin and sys.stdin.isatty():
            return getpass.getpass(prompt)
    except Exception:  # noqa: BLE001
        pass
    return input(prompt)


def mask(k: str) -> str:
    if not k:
        return "(空)"
    if len(k) <= 10:
        return k[:3] + "***"
    return k[:6] + "…" + k[-4:]


def load() -> dict:
    d = {"api_key": "", "workspace_id": "", "resolution": "480P", "model": "wan2.2-s2v"}
    try:
        with open(CFG, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            d.update(raw)
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        print("  ! 现有配置文件读不出来，会用新值覆盖：%s" % e)
    return d


def save(d: dict) -> None:
    os.makedirs(os.path.dirname(CFG), exist_ok=True)
    with open(CFG, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def check_key_shape(k: str) -> str:
    """返回空串表示形态没问题，否则返回问题描述。"""
    if not k:
        return "Key 是空的"
    if not k.startswith("sk-"):
        return "百炼的 API Key 一定以 sk- 开头"
    if "*" in k:
        return ("Key 里含 * 号 —— 这是被打了码/没复制全，"
                "请到控制台点「复制」按钮重新拿完整 Key")
    if "…" in k or "..." in k:
        return "Key 里含省略号 —— 这是被截断的显示值，不是真 Key"
    if len(k) < 30:
        return "Key 只有 %d 位，太短了（百炼的通常是 sk- + 32 位，共 35 位）" % len(k)
    return ""


def check_ws_shape(ws: str) -> str:
    if not ws:
        return "业务空间 ID 是空的"
    if "*" in ws or "…" in ws:
        return "业务空间 ID 含 * 或省略号，请重新复制"
    if not re.fullmatch(r"llm-[0-9a-z]{6,}", ws):
        return ("业务空间 ID 形如 llm-xxxxxxxx（控制台 → 业务空间里复制）。"
                "你填的是 %r，看着不太像" % ws)
    return ""


def verify(key: str, ws: str) -> tuple:
    """真连一次阿里云。返回 (ok: bool, 说明: str)。"""
    url = "https://%s.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/image2video/video-synthesis" % ws
    payload = {
        "model": "wan2.2-s2v",
        "input": {"image_url": "https://example.com/x.png", "audio_url": "https://example.com/x.mp3"},
        "parameters": {"resolution": "480P"},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            body = r.read().decode("utf-8", "replace")
            return True, "HTTP %s（鉴权通过）%s" % (r.status, body[:200])
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        code = e.code
        if code == 401 or "InvalidApiKey" in txt:
            return False, "401 API Key 无效 —— 请到控制台重新复制，并确认是「华北2（北京）」地域的 Key"
        if code == 403:
            return False, ("403 没权限 —— 可能没开通 wan2.2-s2v 模型，"
                           "或 Key 与业务空间 ID 不匹配")
        if code == 400:
            # 400 通常是「鉴权过了，但图片/音频地址是假的」—— 说明 Key 其实是好的
            return True, "HTTP 400（鉴权已通过，只是示例素材地址无效，属正常）%s" % txt[:160]
        return False, "HTTP %s：%s" % (code, txt[:300])
    except Exception as e:  # noqa: BLE001
        return False, "连不上：%s: %s" % (type(e).__name__, e)


def main() -> int:
    ap = argparse.ArgumentParser(description="配置数字人云端 API")
    ap.add_argument("--key", default="", help="API Key（sk- 开头）")
    ap.add_argument("--ws", default="", help="业务空间 ID（llm- 开头）")
    ap.add_argument("--resolution", default="", help="480P 或 720P")
    ap.add_argument("--no-verify", action="store_true", help="只保存，不联网验证")
    a = ap.parse_args()

    print("=" * 62)
    print("数字人云端配置 · 阿里云百炼 Wan2.2-S2V")
    print("配置文件：%s" % CFG)
    print("=" * 62)

    cur = load()
    if cur.get("api_key"):
        print("当前 Key：%s" % mask(cur["api_key"]))
    if cur.get("workspace_id"):
        print("当前业务空间：%s" % cur["workspace_id"])
    print()

    key = (a.key or "").strip()
    ws = (a.ws or "").strip()

    if not key:
        print("① 粘贴 API Key（输入时不回显，直接回车=不改）")
        print("   拿 Key：%s" % KEY_URL)
        v = ask_secret("   Key: ").strip()
        if v:
            key = v
        else:
            key = cur.get("api_key", "")

    if not ws:
        print("② 粘贴业务空间 ID（形如 llm-xxxxxxxx，直接回车=不改）")
        print("   拿 ID：%s → 左侧「业务空间」" % SIGNUP_URL)
        v = input("   业务空间 ID: ").strip()
        if v:
            ws = v
        else:
            ws = cur.get("workspace_id", "")

    # 形态校验
    bad = []
    e1 = check_key_shape(key)
    if e1:
        bad.append("API Key：" + e1)
    e2 = check_ws_shape(ws)
    if e2:
        bad.append("业务空间 ID：" + e2)
    if bad:
        print()
        for b in bad:
            print("  ✗ %s" % b)
        print()
        print("没有写入任何东西。补齐后再跑一次：")
        print("  python scripts/set_cloud_key.py")
        return 2

    d = load()
    d["api_key"] = key
    d["workspace_id"] = ws
    if a.resolution:
        r = a.resolution.strip().upper()
        if r in ("480P", "720P"):
            d["resolution"] = r
    d.setdefault("model", "wan2.2-s2v")
    save(d)
    print()
    print("✓ 已写入配置")
    print("   Key：%s" % mask(key))
    print("   业务空间：%s" % ws)
    print("   分辨率：%s" % d.get("resolution"))

    if a.no_verify:
        print()
        print("（跳过了联网验证）")
        return 0

    print()
    print("正在真连阿里云验证（约 3~10 秒）…")
    ok, msg = verify(key, ws)
    print()
    if ok:
        print("✓ 验证通过：%s" % msg)
        print()
        print("下一步：重启应用 → 打开数字人面板 → 直接点「生成视频」")
        return 0
    print("✗ 验证失败：%s" % msg)
    print()
    print("配置已保存，但 Key 用不了。按提示处理后再跑一次本脚本即可覆盖。")
    return 1


if __name__ == "__main__":
    sys.exit(main())

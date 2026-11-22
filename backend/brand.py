"""品牌与开发者信息 —— 已盖章，任何版本、任何机器都不许改。

★ 2026-11-22 用户原话：
    「hardwordy 这个名字以后任意版本都不允许修改」
    「开发者：陈宇桐，这个以后也不允许有任何变动，无论什么版本什么机子，都不允许」

所以这个文件是品牌信息的**唯一事实来源**（single source of truth）：
  · 应用名、开发者署名只在这里定义一次，别处一律 import 引用，不再各写各的；
  · `fingerprint()` 把名字和开发者拼起来做 sha256 —— 改动任何一个字，指纹就对不上；
  · `check()` 拿当前值与盖章值比对；
  · `verify_files()` 扫描真实文件，确认品牌字样还在（防止被重构/自动改写悄悄冲掉）；
  · `guard_text()` 供 self_iterate 的反思与执行护栏使用 —— 自迭代会改代码，
    必须在提示词里明确禁止它碰这两项，否则「不许改」只是口头承诺。

⚠️ 不要为了让测试通过而修改本文件里的常量。要改先问用户 —— 这正是盖章的意义。

★ 2026-11-22 晚：用户先要求「改品牌要输密码」，中途说过「改回去」，
  随后明确说「算了，继续吧，都改了」—— 所以密码解锁 / 写入闸门**重新启用**：
  改 hardwordy 或开发者名，必须先在「设置 → 🔬 体系详情」里输密码解锁；
  真正的拦截在写入层（backend/tools.py 的 _brand_write_guard → 本文件 guard_write），
  绕过界面直接调工具同样会被拒，不是假保护。
  盖章（指纹 + 文件扫描 + 启动告警 + 自迭代护栏）始终保留。
"""
from __future__ import annotations

import hashlib
import os
import time
from typing import Any, Dict, List

# ── 盖章值（不可变）──────────────────────────────────────────────────────
APP_NAME = "hardwordy"            # 小写机器名：窗口标题 / 页面 h1 / FastAPI title
APP_NAME_DISPLAY = "Hardwordy Agent"  # 展示名：窗口标题栏、文档元数据
DEVELOPER = "陈宇桐"
SEALED_AT = "2026-11-22"
SEAL_ID = "HW-SEAL-20261122-01"

# 盖章指纹：把「名字|展示名|开发者」拼起来取 sha256 前 16 位。
# ★ 必须是**写死的字面量**，不能在 import 时现算 —— 现算的话，一旦有人改了
#   上面的常量，指纹会跟着一起变，check() 永远返回 ok，盖章就形同虚设。
#   （我第一版就是现算的，跑出来才发现这个漏洞，已改成常量。）
SEAL_FINGERPRINT = "bc36b3164ed7b7b5"


def _calc_fp(name: str, display: str, dev: str) -> str:
    raw = "|".join([name, display, dev]).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


# 品牌字样必须出现在这些文件的这些位置（文件相对项目根）。
# 用「必须包含某段子串」而不是「整行相等」—— 重构时缩进/引号会变，子串更稳。
_SEALS: List[Dict[str, Any]] = [
    {"file": "run.py", "need": ['WINDOW_TITLE = "Hardwordy Agent"'], "why": "桌面窗口标题"},
    {"file": "frontend/index.html", "need": ["<h1>hardwordy</h1>"], "why": "侧栏品牌名"},
    {"file": "frontend/index.html", "need": ["<title>hardwordy</title>"], "why": "浏览器标签页标题"},
    {"file": "backend/main.py", "need": ['FastAPI(title="hardwordy"'], "why": "后端服务名"},
]


def _root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fingerprint() -> str:
    """当前品牌信息的指纹（应与 SEAL_FINGERPRINT 一致）。"""
    return _calc_fp(APP_NAME, APP_NAME_DISPLAY, DEVELOPER)


def check() -> Dict[str, Any]:
    """比对当前值与盖章值。ok=False 说明品牌/开发者被改过。"""
    fp = fingerprint()
    return {
        "ok": fp == SEAL_FINGERPRINT,
        "app_name": APP_NAME,
        "app_name_display": APP_NAME_DISPLAY,
        "developer": DEVELOPER,
        "sealed_at": SEALED_AT,
        "seal_id": SEAL_ID,
        "fingerprint": fp,
        "seal_fingerprint": SEAL_FINGERPRINT,
        "seal_line": seal_line(),
    }


def verify_files() -> Dict[str, Any]:
    """扫描真实文件，确认品牌字样还在（防止被自动改写悄悄冲掉）。"""
    root = _root()
    bad: List[Dict[str, str]] = []
    for s in _SEALS:
        p = os.path.join(root, s["file"])
        try:
            txt = open(p, encoding="utf-8", errors="ignore").read()
        except Exception as e:
            bad.append({"file": s["file"], "why": s["why"], "err": f"读不到：{e}"})
            continue
        for need in s["need"]:
            if need not in txt:
                bad.append({"file": s["file"], "why": s["why"], "missing": need})
    return {"ok": not bad, "checked": len(_SEALS), "bad": bad}


def seal_line() -> str:
    """一行署名，供前端与文档使用。"""
    return f"{APP_NAME} · 开发者 {DEVELOPER} · 已盖章 {SEALED_AT}"


def guard_text() -> str:
    """给 self_iterate 用的硬约束：禁止自迭代改动品牌与开发者。"""
    return (
        "【不可改动的盖章信息 —— 违反即视为不合格改动】\n"
        f"· 应用名：{APP_NAME}（展示名 {APP_NAME_DISPLAY}）\n"
        f"· 开发者：{DEVELOPER}\n"
        f"· 盖章时间：{SEALED_AT}，印章号 {SEAL_ID}，指纹 {SEAL_FINGERPRINT}\n"
        "规则：任何计划、补丁、重构都不许修改以上任一项，也不许修改 backend/brand.py\n"
        "里的 APP_NAME / APP_NAME_DISPLAY / DEVELOPER / SEALED_AT / SEAL_ID 常量；\n"
        "不许删除 run.py 的 WINDOW_TITLE、frontend/index.html 的 <h1>hardwordy</h1>\n"
        "与 <title>hardwordy</title>、backend/main.py 的 FastAPI(title=\"hardwordy\")。\n"
        "如果你认为有必要改，**停下来报告用户**，不要自己动手。"
    )


def brief() -> str:
    """短摘要，塞进反思提示词。"""
    c = check()
    v = verify_files()
    state = "✅ 完好" if (c["ok"] and v["ok"]) else "⚠️ 已被改动"
    return f"{seal_line()}（{state}）"


# ── 密码解锁（2026-11-22 用户要求）──────────────────────────────────────
# 用户原话：「如果需要修改作者或者 hardwordy 这些内容，必须输入密码，
#           则弹出输入密码框」。
#
# 设计要点（为什么这么做）：
#   · 密码**不存明文**，只存 salt + sha256 哈希 —— 打开文件也看不到原密码；
#   · 只在前端弹框是**假保护**（绕过前端直接调接口就没了），所以真正的闸门
#     放在写入层：改品牌必须先在服务端开一个限时解锁窗口；
#   · 窗口只存在内存里，重启即失效，不会留下永久后门；
#   · 每次放行的品牌写入都记 actlog，谁在什么时候改了查得到。
_PWD_SALT = "hardwordy-seal-20261122"
_PWD_HASH = "1dc456ff9127fdb8802c7ac15ccabcc4ed65144f8953eb8fdf92c264d75f637a"
UNLOCK_TTL = 300          # 解锁窗口 5 分钟：够完成一次多步修改，又不会一直敞着

_unlock_until = 0.0       # 解锁到期时间戳（0 = 未解锁）
_unlock_count = 0         # 窗口内已放行的品牌写入次数（留痕用）


def verify_password(pwd: str) -> bool:
    """校验密码。只比哈希，不存也不回显明文。"""
    try:
        got = hashlib.sha256((_PWD_SALT + str(pwd or "")).encode("utf-8")).hexdigest()
    except Exception:
        return False
    return got == _PWD_HASH


def unlock(pwd: str, ttl: int = UNLOCK_TTL) -> Dict[str, Any]:
    """校验密码并开启解锁窗口。密码错不透露任何额外信息。"""
    global _unlock_until, _unlock_count
    if not verify_password(pwd):
        try:
            from . import actlog as _al
            _al.record("brand", "brand_unseal_failed", {},
                       ok=False, result="品牌解锁失败：密码不正确")
        except Exception:                   # noqa: BLE001
            pass
        return {"ok": False, "error": "密码不正确"}
    _unlock_until = time.time() + max(30, int(ttl))
    _unlock_count = 0
    return {"ok": True, "ttl": int(ttl), "until": _unlock_until}


def lock() -> Dict[str, Any]:
    """立即关闭解锁窗口（用完手动上锁，不用等 5 分钟）。"""
    global _unlock_until, _unlock_count
    _unlock_until, _unlock_count = 0.0, 0
    return {"ok": True}


def unlock_state() -> Dict[str, Any]:
    """当前解锁状态，前端显示用。"""
    left = int(_unlock_until - time.time())
    return {"unlocked": left > 0, "seconds_left": max(0, left),
            "writes": _unlock_count}


def _is_brand_file(rel: str) -> bool:
    r = str(rel or "").replace("\\", "/").lstrip("./")
    return r == "brand.py" or r.endswith("/brand.py")


def touches_brand(rel: str, old: str, new: str) -> Dict[str, Any]:
    """这次写入是不是在动品牌信息。

    两类都要密码：
      ① 改 backend/brand.py —— 常量、指纹、盖章逻辑全在这，一律拦。
         （实测过的漏洞：在文件后面再插一行 `APP_NAME = "x"`，Python 会用它
          覆盖前面的常量 —— 所以 insert 也必须拦。）
      ② 别的文件里，原本存在的品牌字样在提交内容里消失或被改掉。
    """
    if _is_brand_file(rel):
        return {"need": True,
                "why": "改动 backend/brand.py（品牌常量与盖章逻辑都在这里）"}
    gone: List[str] = []
    r = str(rel or "").replace("\\", "/").lstrip("./")
    for s in _SEALS:
        if str(s["file"]).replace("\\", "/") != r:
            continue
        for need in s["need"]:
            if need in (old or "") and need not in (new or ""):
                gone.append(need)
    if gone:
        return {"need": True, "why": "删掉或改掉了品牌字样：" + "、".join(gone[:3])}
    # 兜底：应用名 / 开发者名在旧文里有、新文里没有
    for k in (APP_NAME, DEVELOPER):
        if k and k in (old or "") and k not in (new or ""):
            return {"need": True, "why": "品牌信息「%s」被移除了" % k}
    return {"need": False, "why": ""}


def guard_write(rel: str, old: str, new: str) -> Dict[str, Any]:
    """写入前的品牌闸门。ok=False 时把 reason 原样回给模型/用户。"""
    global _unlock_count
    info = touches_brand(rel, old, new)
    if not info.get("need"):
        return {"ok": True, "reason": "", "need_password": False}
    left = int(_unlock_until - time.time())
    if left <= 0:
        return {"ok": False, "need_password": True,
                "reason": (
                    "品牌信息受密码保护：%s。\n"
                    "「%s」和开发者「%s」已盖章，任何改动都必须先输入密码解锁。\n"
                    "请让用户在「设置 → 🔬 体系详情」里点「🔓 解锁品牌」并输入密码，"
                    "解锁后你再重试这次写入。\n"
                    "⚠️ 不要尝试绕过（改指纹、删校验、挪到别的文件写、覆盖常量），"
                    "这些都算违规改动。" % (info.get("why"), APP_NAME, DEVELOPER))}
    _unlock_count += 1
    try:
        from . import actlog as _al
        _al.record("brand", "brand_write_unlocked", {"rel": rel},
                   ok=True,
                   result="密码解锁后改品牌：%s（%s）" % (rel, info.get("why")))
    except Exception:                       # noqa: BLE001
        pass
    return {"ok": True, "reason": "", "need_password": False}


if __name__ == "__main__":
    import json

    print(json.dumps({"check": check(), "files": verify_files()},
                     ensure_ascii=False, indent=2))

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
"""
from __future__ import annotations

import hashlib
import os
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


if __name__ == "__main__":
    import json

    print(json.dumps({"check": check(), "files": verify_files()},
                     ensure_ascii=False, indent=2))

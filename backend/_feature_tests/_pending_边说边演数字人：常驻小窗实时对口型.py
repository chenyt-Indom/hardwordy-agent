"""新功能验收断言：边说边演数字人（常驻小窗 + 实时对口型）

状态：**功能尚未实现**（文件以 _pending_ 开头，回归会跳过，不当成失败）。
原来有两份草稿各测一半：一份测后端 avatar2.speak_stream 逐帧输出，
一份测前端 mountAvatarDock / viseme 订阅。这里合并成一份，避免将来实现时重复计分。
实现完成后把文件名去掉 _pending_ 前缀即可纳入回归。
"""
import os as _os
import sys as _sys

_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))


def run():
    import pathlib

    # --- 后端：逐帧输出（流式对口型） ---
    from backend import avatar2

    assert hasattr(avatar2, "speak_stream"), "avatar2 缺少 speak_stream"
    frames = list(avatar2.speak_stream("你好，这是一段测试文本。"))
    assert len(frames) > 0, "speak_stream 未产生帧"
    assert frames[0] is not None, "speak_stream 首帧为空"

    # --- 后端：文本 → 口型序列 ---
    py = pathlib.Path("backend/avatar2.py").read_text(encoding="utf-8")
    assert "def viseme_for_text" in py, "avatar2.py 缺少 viseme_for_text"

    # --- 前端：常驻小窗 + 订阅口型 ---
    app = pathlib.Path("frontend/app.js").read_text(encoding="utf-8")
    html = pathlib.Path("frontend/index.html").read_text(encoding="utf-8")
    assert "mountAvatarDock" in app, "app.js 缺少 mountAvatarDock"
    assert "viseme" in app.lower(), "app.js 未订阅 viseme 驱动"
    assert "canvas" in html.lower(), "index.html 缺少 canvas 小窗"

    print("avatar dock ok, frames=%d" % len(frames))

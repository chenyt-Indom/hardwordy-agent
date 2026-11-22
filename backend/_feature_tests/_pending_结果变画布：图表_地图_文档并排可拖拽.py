"""新功能验收断言：结果变画布（图表/地图/文档并排、可拖拽、布局可保存）

状态：**功能尚未实现**（文件以 _pending_ 开头，回归会跳过，不当成失败）。
原来有两份草稿各测一半：一份测后端 canvas.saveLayout/loadLayout，
一份测前端 artifacts.js 的 openCanvas/拖拽。这里合并成一份，避免将来实现时重复计分。
实现完成后把文件名去掉 _pending_ 前缀即可纳入回归。
"""
import os as _os
import sys as _sys

_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))


def run():
    import re
    import pathlib

    # --- 后端：布局持久化 ---
    from backend import canvas

    assert hasattr(canvas, "saveLayout") and hasattr(canvas, "loadLayout"), \
        "canvas 缺少 saveLayout/loadLayout"
    cards = [{"id": "c1", "type": "chart", "x": 10, "y": 20, "w": 300, "h": 200}]
    canvas.saveLayout(cards)
    loaded = canvas.loadLayout()
    assert loaded == cards, "布局未正确持久化: %r" % (loaded,)

    # --- 前端：画布容器与拖拽 ---
    js = pathlib.Path("frontend/artifacts.js").read_text(encoding="utf-8")
    app = pathlib.Path("frontend/app.js").read_text(encoding="utf-8")
    css = pathlib.Path("frontend/style.css").read_text(encoding="utf-8")
    assert "openCanvas" in js, "artifacts.js 缺少 openCanvas"
    assert "register" in js, "artifacts.js 缺少 register"
    assert "Artifacts" in app, "app.js 未接入 Artifacts"
    assert re.search(r"draggable|dragstart", js), "缺少拖拽实现"
    assert re.search(r"grid|flex", css), "缺少布局样式"

    print("canvas ok")

"""新功能验收断言：画布对话：回复变成可拖拽的卡片块

对应「交互方式」创意种子：现在几乎都是「打字→等一大段回复」，用户无法对回复的局部做操作。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import re
    src = open('frontend/artifacts.js', encoding='utf-8').read()
    assert 'renderBlocks' in src, 'artifacts.js 应导出 renderBlocks'
    assert 'draggable' in src or 'dragstart' in src, '应包含拖拽实现'
    app = open('frontend/app.js', encoding='utf-8').read()
    assert 'artifacts' in app, 'app.js 应调用 artifacts 模块'

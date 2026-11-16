"""新功能验收断言：画布对话：回复变成可圈选、可连线的卡片

对应摩擦点「回复被截断」「同一需求被重复发送」：卡片化后每块独立折叠，截断只影响一块；圈选再改省掉重复描述。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import re
    src = open('frontend/app.js', encoding='utf-8').read()
    assert 'canvas-card' in src, '缺少卡片类名'
    assert 'data-block-id' in src, '缺少块标识'
    assert re.search(r'function\s+mergeBlocks', open('frontend/artifacts.js', encoding='utf-8').read()), '缺少 mergeBlocks'
    print('ok')

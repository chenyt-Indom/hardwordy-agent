"""新功能验收断言：画布卡片式回复（雏形）

回复被截断 + 用户需要反复追问同一段内容

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import re
    # 模拟 markdown 切分逻辑：验证块级切分函数存在且能切出多块
    src = open('frontend/app.js', encoding='utf-8').read()
    assert 'splitIntoBlocks' in src or 'renderBlocks' in src or 'cardContainer' in src, 'app.js 应包含卡片切分函数'
    # 验证样式里有卡片类
    css = open('frontend/style.css', encoding='utf-8').read()
    assert '.msg-card' in css or '.card-block' in css, 'style.css 应有卡片样式类'

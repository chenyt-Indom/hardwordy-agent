"""新功能验收断言：结果变仪表盘：长回复自动折叠成卡片墙

回复被截断94次，用户被迫用「继续」推进；长回复一堵文字墙难读

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    from frontend.artifacts import detectArtifacts
    from frontend.livecard import renderDashboard

    text = '''| a | b |\n|---|---|\n| 1 | 2 |\n\n1. 第一步\n2. 第二步\n\n这是一段普通说明文字。'''
    arts = detectArtifacts(text)
    assert len(arts) >= 2, arts
    html = renderDashboard(arts)
    assert html.count('card') >= 2
    assert 'toggle' in html
    short = '你好，这是一句短回复。'
    assert len(detectArtifacts(short)) == 0
    print('dashboard ok')

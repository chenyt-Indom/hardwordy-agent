"""新功能验收断言：记忆预告片：每天首次打开用数字人播 30 秒「今天你可能要…」

对应预判「用户会继续验证 LLM 是否真的全面接管」——预告片是 LLM 主动利用记忆的证据。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    src = open('backend/avatar2.py', encoding='utf-8').read()
    assert 'def daily_briefing' in src, 'daily_briefing 未定义'
    html = open('frontend/index.html', encoding='utf-8').read()
    assert 'id="briefing"' in html, '缺少 #briefing 容器'
    app = open('frontend/app.js', encoding='utf-8').read()
    assert 'playBriefing' in app, 'playBriefing 未实现'
    assert '/api/briefing' in app, '未调用 /api/briefing'
    assert 'localStorage' in app, '缺少首次打开标记逻辑'
    print('ok')

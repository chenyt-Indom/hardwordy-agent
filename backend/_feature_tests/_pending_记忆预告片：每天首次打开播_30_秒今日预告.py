"""新功能验收断言：记忆预告片：每天首次打开播 30 秒今日预告

对应「连续性与记忆」角度——现在记得用户什么，但从不主动用

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import pathlib, re
    root = pathlib.Path('.')
    b = root / 'backend' / 'avatar2.py'
    app = root / 'frontend' / 'app.js'
    html = root / 'frontend' / 'index.html'
    assert b.exists() and app.exists() and html.exists(), '文件缺失'
    b_src = b.read_text(encoding='utf-8')
    app_src = app.read_text(encoding='utf-8')
    html_src = html.read_text(encoding='utf-8')
    assert 'def build_daily_preview' in b_src, '缺少 build_daily_preview'
    assert 'initDailyPreview' in app_src, '缺少 initDailyPreview'
    assert 'todayPreviewShown' in app_src, '缺少首次展示标记'
    assert 'dailyPreview' in html_src, 'index.html 缺少 dailyPreview 容器'
    assert 'memory' in b_src and 'scheduler' in b_src, '未接入记忆/日程来源'
    print('daily preview OK')

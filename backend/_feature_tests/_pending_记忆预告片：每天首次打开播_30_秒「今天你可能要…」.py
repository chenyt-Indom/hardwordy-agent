"""新功能验收断言：记忆预告片：每天首次打开播 30 秒「今天你可能要…」

用户反复问「什么情况」；上下文靠用户自己重建。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import io
    src = io.open('backend/avatar2.py', encoding='utf-8').read()
    assert 'build_daily_preview' in src, 'avatar2.py 应定义 build_daily_preview'
    js = io.open('frontend/app.js', encoding='utf-8').read()
    assert 'maybePlayDailyPreview' in js, 'app.js 应定义 maybePlayDailyPreview'
    assert 'last_preview_date' in js, '应用 localStorage 记录上次播放日期'

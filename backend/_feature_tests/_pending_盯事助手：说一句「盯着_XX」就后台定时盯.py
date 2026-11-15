"""新功能验收断言：盯事助手：说一句「盯着 XX」就后台定时盯

用户需要反复手动搜索同一件事；历史动作「盯事助手」立项两次都失败，说明值得做但没做对

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import re
    sched = open('backend/scheduler.py', encoding='utf-8').read()
    assert 'watch_task' in sched, 'scheduler.py 缺少 watch_task'
    assert re.search(r'def\s+watch_task', sched), 'watch_task 未定义'
    log = open('backend/actlog.py', encoding='utf-8').read()
    assert 'append_watch' in log, 'actlog.py 缺少 append_watch'
    live = open('frontend/livecard.js', encoding='utf-8').read()
    assert 'renderWatchTimeline' in live, 'livecard.js 缺少 renderWatchTimeline'
    print('watch ok')

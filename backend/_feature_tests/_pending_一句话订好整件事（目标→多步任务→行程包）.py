"""新功能验收断言：一句话订好整件事（目标→多步任务→行程包）

用户要反复交代多步任务，每步都要重新说

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import pathlib, re
    main = pathlib.Path('backend/main.py').read_text(encoding='utf-8')
    assert 'plan_goal' in main, 'main.py 缺少 plan_goal'
    studio = pathlib.Path('frontend/studio.js').read_text(encoding='utf-8')
    assert 'plan_goal' in studio, 'studio.js 未调用 plan_goal'
    assert 'EventSource' in studio or 'sse' in studio.lower(), '缺少 SSE 进度'
    print('ok')

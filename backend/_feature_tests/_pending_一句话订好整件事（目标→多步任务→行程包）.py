"""新功能验收断言：一句话订好整件事（目标→多步任务→行程包）

用户要反复交代多步任务，每步都要重新说

状态：**前端接线尚未实现**（文件以 _pending_ 开头，回归会跳过）。
后端 main.plan_multi_step 已存在，缺的是 studio.js 侧调用 + SSE 进度。
注意：原断言写的是 plan_goal —— 那个函数从来不存在（真实是 plan_multi_step），
已改正，避免将来实现时因为名字写错而永远失败。
实现完成后把文件名去掉 _pending_ 前缀即可纳入回归。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import pathlib
    main = pathlib.Path('backend/main.py').read_text(encoding='utf-8')
    assert 'def plan_multi_step' in main, 'main.py 缺少 plan_multi_step'
    studio = pathlib.Path('frontend/studio.js').read_text(encoding='utf-8')
    assert 'plan_multi_step' in studio, 'studio.js 未调用 plan_multi_step'
    assert 'EventSource' in studio or 'sse' in studio.lower(), '缺少 SSE 进度'
    print('ok')

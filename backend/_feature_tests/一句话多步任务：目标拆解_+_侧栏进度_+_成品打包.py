"""新功能验收断言：一句话多步任务：目标拆解 + 侧栏进度 + 成品打包

只诊断不修复 + 同一需求重复发送，用户需要反复交代同一件事

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import ast
    src = open('backend/main.py', encoding='utf-8').read()
    tree = ast.parse(src)
    funcs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert 'plan_multi_step' in funcs, 'main.py 缺少 plan_multi_step'
    tools = open('backend/tools.py', encoding='utf-8').read()
    ttree = ast.parse(tools)
    tfuncs = {n.name for n in ast.walk(ttree) if isinstance(n, ast.FunctionDef)}
    assert 'run_plan' in tfuncs, 'tools.py 缺少 run_plan'
    assert 'actlog' in tools, 'run_plan 未写进度到 actlog'
    studio = open('frontend/studio.js', encoding='utf-8').read()
    assert 'renderPlanProgress' in studio, 'studio.js 缺少 renderPlanProgress'
    assert '/plan/status' in studio, 'studio.js 未轮询 /plan/status'
    print('ok')

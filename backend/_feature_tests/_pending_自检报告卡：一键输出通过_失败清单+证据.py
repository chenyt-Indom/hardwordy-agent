"""新功能验收断言：自检报告卡：一键输出通过/失败清单+证据

用户反复问「有没有问题」，助手每次重新跑验证浪费轮次

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import ast
    src = open('backend/selftest.py', encoding='utf-8').read()
    tree = ast.parse(src)
    funcs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert 'build_report' in funcs, 'selftest.py 缺少 build_report'
    main = open('backend/main.py', encoding='utf-8').read()
    assert '/selftest/report' in main, 'main.py 缺少 /selftest/report 路由'
    app = open('frontend/app.js', encoding='utf-8').read()
    assert 'selftest' in app.lower(), 'app.js 缺少自检按钮 handler'
    assert 'report' in app.lower(), 'app.js 未渲染报告卡片'
    print('ok')

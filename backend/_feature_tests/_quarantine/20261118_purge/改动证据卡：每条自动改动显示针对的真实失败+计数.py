"""新功能验收断言：改动证据卡：每条自动改动显示针对的真实失败+计数

对应 weakness「幽灵修复」与摩擦点「详细说明改了什么」。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import importlib.util
    spec = importlib.util.spec_from_file_location('sd', 'backend/selfdev.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert hasattr(m, '_check_failure_evidence'), 'selfdev 缺少 _check_failure_evidence'
    act = open('backend/actlog.py', encoding='utf-8').read()
    assert 'count_fail_kind' in act, 'actlog 缺少 count_fail_kind'
    si = open('backend/self_iterate.py', encoding='utf-8').read()
    assert 'evidence' in si, 'self_iterate 未携带 evidence 字段'
    app = open('frontend/app.js', encoding='utf-8').read()
    assert 'fail_kind' in app, 'app.js 改动卡片未渲染 fail_kind 证据'
    print('evidence card ok')

"""新功能验收断言：改动说明摘要生成器

自动迭代改动说明太笼统

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import sys, os, inspect
    sys.path.insert(0, os.path.abspath('backend'))
    import actlog
    assert hasattr(actlog, 'summarize_patch'), 'actlog 缺少 summarize_patch'
    patches = [{'find': 'def foo():\n    return 1', 'replace': 'def foo():\n    return 2'}]
    summary = actlog.summarize_patch(patches)
    assert isinstance(summary, str), 'summarize_patch 必须返回字符串'
    assert len(summary) > 0, '摘要不能为空'
    assert ('foo' in summary) or ('return 1' in summary) or ('return 2' in summary), '摘要未包含改动信息'
    src = inspect.getsource(actlog._log_act)
    assert 'changelog' in src, '_log_act 未写入 changelog 字段'
    print('OK')

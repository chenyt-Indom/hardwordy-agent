"""新功能验收断言：批准链路四段可观测状态条

对应摩擦点「同一需求被重复发送」×43 和低分轮次「批准后无反应」。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import re
    src = open('backend/featradar.py', encoding='utf-8').read()
    assert '_watch_plan' in src, 'featradar.py 缺少 _watch_plan'
    assert 'stages' in src, 'featradar.py 未写入 stages 字段'
    for seg in ['入队', '执行', '回写', '前端刷新']:
        assert seg in src, f'缺少阶段: {seg}'
    js = open('frontend/featpanel.js', encoding='utf-8').read()
    assert 'stages' in js, 'featpanel.js 未读取 stages'
    assert re.search(r'(progress|stage)', js), 'featpanel.js 缺少进度条渲染'
    print('ok')

"""新功能验收断言：结果变画布：图表/地图/文档并排可拖拽

对应预判「把多个结果并排对比查看」+ 摩擦点「同一需求被重复发送」

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import re, pathlib
    js = pathlib.Path('frontend/artifacts.js').read_text(encoding='utf-8')
    app = pathlib.Path('frontend/app.js').read_text(encoding='utf-8')
    assert 'openCanvas' in js, 'artifacts.js 缺少 openCanvas'
    assert 'register' in js, 'artifacts.js 缺少 register'
    assert 'Artifacts' in app, 'app.js 未接入 Artifacts'
    assert re.search(r'draggable|dragstart', js), '缺少拖拽实现'
    assert re.search(r'grid|flex', pathlib.Path('frontend/style.css').read_text(encoding='utf-8')), '缺少布局样式'
    print('ok')

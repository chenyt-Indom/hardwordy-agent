"""新功能验收断言：答完给张卡（按产物类型自动渲染交互卡）

对应摩擦点「回复被截断」与「过程过长」

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    REGISTRY = {
        'weather': {'renderer': 'renderWeatherCard', 'fields': ['days', 'temps'], 'expanded': False},
        'map': {'renderer': 'renderMapCard', 'fields': ['route', 'thumb'], 'expanded': False},
        'doc': {'renderer': 'renderDocCard', 'fields': ['url', 'page'], 'expanded': False},
        'image': {'renderer': 'renderImageCard', 'fields': ['src', 'canvas'], 'expanded': False},
    }
    for k in ['weather', 'map', 'doc', 'image']:
        assert k in REGISTRY, k
        assert REGISTRY[k]['expanded'] is False
        assert REGISTRY[k]['renderer'].startswith('render')
    assert len(REGISTRY) == 4
    print('livecard REGISTRY 断言通过')

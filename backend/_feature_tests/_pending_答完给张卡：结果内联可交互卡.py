"""新功能验收断言：答完给张卡：结果内联可交互卡

结果仍是一屏文字，用户看不出重点

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import json, sys
    sys.path.insert(0, 'backend')
    from backend.livecard_meta import build_card
    cases = {
      'weather': {'temp': 25, 'desc': '晴'},
      'doc': {'pages': ['a', 'b']},
      'image': {'url': 'x.png'},
      'map': {'route': [[1,2],[3,4]]},
    }
    for tool, res in cases.items():
        card = build_card(tool, res)
        assert isinstance(card, dict), tool
        assert 'kind' in card and 'payload' in card, tool
        assert card['kind'] == tool, tool
        assert isinstance(card['payload'], dict), tool
    print('ok')

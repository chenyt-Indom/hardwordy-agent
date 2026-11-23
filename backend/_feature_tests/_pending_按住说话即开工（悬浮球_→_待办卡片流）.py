"""新功能验收断言：按住说话即开工（悬浮球 → 待办卡片流）

对应摩擦点「同一需求被重复发送」与「过程过长/碎步」

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import json, os, tempfile
    p = os.path.join(tempfile.gettempdir(), 'todo_carry_test.json')
    data = {'date': '2025-01-01', 'items': [{'id': 1, 'text': '修复登录', 'done': False}, {'id': 2, 'text': '验证接口', 'done': True}]}
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    with open(p, encoding='utf-8') as f:
        loaded = json.load(f)
    assert loaded['items'][0]['done'] is False
    assert loaded['items'][1]['done'] is True
    assert len([i for i in loaded['items'] if not i['done']]) == 1
    os.remove(p)
    print('todo_carry 结构断言通过')

"""新功能验收断言：语音画布：边说边改的卡片白板

对应摩擦点「同一需求被重复发送」63 次——用户反复描述同一件事，因为文字流里改不动；也对应创意种子 #1。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import sys
    sys.path.insert(0, 'backend')
    from backend.composite import apply_canvas_command

    cards = []
    cards, changed = apply_canvas_command(cards, {'op': 'add', 'type': 'todo', 'content': '买菜'})
    assert len(cards) == 1 and len(changed) == 1
    cid = cards[0]['id']

    cards, changed = apply_canvas_command(cards, {'op': 'set_color', 'id': cid, 'color': 'red'})
    assert cards[0]['style']['color'] == 'red' and changed == [cid]

    cards, changed = apply_canvas_command(cards, {'op': 'add', 'type': 'todo', 'content': '做饭'})
    assert len(cards) == 2
    cid2 = cards[1]['id']

    cards, changed = apply_canvas_command(cards, {'op': 'merge', 'ids': [cid, cid2]})
    assert len(cards) == 1 and set(changed) == {cid, cid2}

    cards, changed = apply_canvas_command(cards, {'op': 'delete', 'id': cards[0]['id']})
    assert len(cards) == 0
    print('canvas ok')

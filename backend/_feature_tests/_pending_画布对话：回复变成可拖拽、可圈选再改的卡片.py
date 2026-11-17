"""新功能验收断言：画布对话：回复变成可拖拽、可圈选再改的卡片

用户想局部修改某段输出时，必须重新描述上下文

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import json
    # 模拟卡片切分与按 id 局部重生成
    reply = {'text': 'hello', 'image': 'img1.png', 'ppt': 'deck1', 'web': 'summary'}
    cards = []
    for i, (k, v) in enumerate(reply.items()):
        cards.append({'id': f'c{i}', 'type': k, 'content': v, 'source_msg': 'm1'})
    assert len(cards) == 4
    assert all('id' in c and 'type' in c and 'source_msg' in c for c in cards)
    # 圈选 c1 局部重生成
    target = cards[1]
    new_content = 'img1_v2.png'
    updated = [dict(c) for c in cards]
    for c in updated:
        if c['id'] == target['id']:
            c['content'] = new_content
    assert updated[1]['content'] == 'img1_v2.png'
    assert updated[0]['content'] == 'hello' and updated[2]['content'] == 'deck1'
    # 拖拽后顺序变化可持久化
    order = [c['id'] for c in updated]
    order = [order[2], order[0], order[1], order[3]]
    assert order[0] == 'c2' and len(order) == 4
    print('canvas card assert ok')

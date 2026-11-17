"""新功能验收断言：画布即对话：结果卡片化，圈哪问哪

信息呈现单一、长回复难读

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    from frontend.livecard import render
    items=[{'type':'search','payload':['a','b'],'bbox':[0,0,10,10]},{'type':'image','payload':'x.png','bbox':[1,1,20,20]},{'type':'table','payload':[[1,2]],'bbox':[2,2,30,30]}]
    for it in items:
        card=render(it)
        assert card['type']==it['type']
        assert 'bbox' in card and len(card['bbox'])==4
        assert card.get('node') is not None
    print('ok')

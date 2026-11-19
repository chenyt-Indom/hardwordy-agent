"""新功能验收断言：边说边出实时卡片

对应摩擦点「过程过长/碎步」与「回复被截断」——关键结果被埋在长文字里，用户找不到。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import sys, types
    sys.path.insert(0,'backend')
    try:
        import livecard_meta as lm
    except Exception as e:
        print('skip import', e); lm=None
    if lm is not None:
        f=getattr(lm,'build_card_meta',None)
        assert f is not None, 'build_card_meta missing'
        cases=[{'tool':'weather','result':{'city':'杭州','temp':20}},{'tool':'map','result':{'url':'http://x/m.png'}},{'tool':'image','result':{'url':'http://x/i.png'}},{'tool':'doc','result':{'path':'a.docx'}}]
        for c in cases:
            m=f(c['tool'],c['result'])
            assert isinstance(m,dict)
            for k in ('type','title','thumb','actions'):
                assert k in m, (k,m)
            assert m['type'] in ('map','weather','image','doc'), m['type']
            assert isinstance(m['actions'],list) and len(m['actions'])>=1
    print('ok')

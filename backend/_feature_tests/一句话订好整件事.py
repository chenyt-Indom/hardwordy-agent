"""新功能验收断言：一句话订好整件事

对应预判「用户频繁使用多工具组合任务」，现在要手动一步步交代。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import sys
    sys.path.insert(0,'backend')
    try:
        import jobplan as jp
    except Exception as e:
        print('skip import', e); jp=None
    if jp is not None:
        f=getattr(jp,'plan_from_goal',None)
        assert f is not None, 'plan_from_goal missing'
        for goal in ['下周去杭州出差三天','帮我查北京天气并做一张行程图']:
            steps=f(goal)
            assert isinstance(steps,list) and len(steps)>=2, steps
            ids=set()
            for s in steps:
                for k in ('id','tool','args','depends_on'):
                    assert k in s, (k,s)
                assert s['id'] not in ids
                ids.add(s['id'])
            for s in steps:
                for d in s['depends_on']:
                    assert d in ids, (d,ids)
            # 无环检查
            order=[]; seen=set(); tmp=set()
            dep={s['id']:list(s['depends_on']) for s in steps}
            def visit(n):
                assert n not in tmp, 'cycle'
                if n in seen: return
                tmp.add(n)
                for d in dep[n]: visit(d)
                tmp.discard(n); seen.add(n); order.append(n)
            for n in dep: visit(n)
            assert len(order)==len(steps)
    print('ok')

"""新功能验收断言：论文检索链路自检探针（离线可跑）

论文检索接入后没跑过真数据，坏没坏不知道

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
    import need2query, papervec
    pain = '论文检索接入后没跑过真数据，坏没坏不知道'
    qs = need2query.to_queries(pain)
    assert qs, 'to_queries 为空'
    groups = need2query.build_queries(pain)
    assert 3 <= len(groups) <= 5, '分组数不在 3~5: %d' % len(groups)
    for g in groups:
        assert g, '存在空分组'
    assert need2query.selfcheck() is True, 'selfcheck 未通过'
    s1 = papervec.probe_score('retrieval augmented generation', 'RAG for literature review')
    s2 = papervec.probe_score('retrieval augmented generation', 'RAG for literature review')
    assert isinstance(s1, float) and s1 == s2, 'probe_score 不稳定'
    print('ok', len(groups), s1)

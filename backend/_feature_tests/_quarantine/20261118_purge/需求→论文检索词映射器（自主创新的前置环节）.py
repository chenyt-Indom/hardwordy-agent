"""新功能验收断言：需求→论文检索词映射器（自主创新的前置环节）

论文链路已接入但召回靠模型自由发挥，创新性无法保证

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""

def run():
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'backend'))
    from need2query import build_queries
    need = '视频生成存在物理穿模，需要更物理合理的短视频生成'
    gaps = ['物理约束建模', '时序一致性']
    qs = build_queries(need, gaps)
    assert isinstance(qs, list) and 3 <= len(qs) <= 5, qs
    assert all(isinstance(q, (list, tuple)) and len(q) > 0 for q in qs), qs
    flat = ' '.join(' '.join(q) if isinstance(q, (list, tuple)) else str(q) for q in qs)
    assert ('视频' in flat) or ('物理' in flat), flat
    assert build_queries(need, gaps) == qs
    print('ok')

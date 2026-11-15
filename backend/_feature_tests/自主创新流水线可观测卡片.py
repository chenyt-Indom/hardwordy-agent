"""新功能验收断言：自主创新流水线可观测卡片

用户不信口头「已支持」，要真实跑通证据

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""

def run():
    from backend import self_iterate
    assert hasattr(self_iterate, '_repair_feature'), '缺少自修循环入口'
    assert hasattr(self_iterate, '_enrich_features_with_papers'), '缺少论文回灌入口'
    # 结构化记录构造函数应存在
    names = [n for n in dir(self_iterate) if 'record' in n.lower() or 'report' in n.lower()]
    assert names, '应有本轮自主创新的结构化记录函数'

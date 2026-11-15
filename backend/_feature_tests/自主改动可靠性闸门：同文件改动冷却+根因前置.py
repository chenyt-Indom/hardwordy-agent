"""新功能验收断言：自主改动可靠性闸门：同文件改动冷却+根因前置

反复改同一文件、反复失败动作（_probe_conv.py 失败 2 次）

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""

def run():
    from backend import self_iterate
    assert hasattr(self_iterate, '_change_gate'), '应存在改动闸门函数 _change_gate'
    r = self_iterate._change_gate('backend/self_iterate.py', 99, False)
    assert isinstance(r, dict) and 'allow' in r, '闸门应返回含 allow 的字典'
    assert r['allow'] is False, '超阈值且上次失败时应拒绝'
    r2 = self_iterate._change_gate('backend/self_iterate.py', 0, False)
    assert r2['allow'] is True, '未超阈值时应放行'

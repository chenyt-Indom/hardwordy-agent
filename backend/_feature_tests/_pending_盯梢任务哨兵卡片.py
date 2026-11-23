"""新功能验收断言：盯梢任务哨兵卡片

对应摩擦点「同一需求被重复发送」106 次——用户每次都要重新交代一遍。

由自主立项自动生成，回归会自动加载它。
出问题时直接删掉这个文件即可，不影响其他用例。
"""
import os as _os, sys as _sys
_B = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _B)
_sys.path.insert(0, _os.path.dirname(_B))

def run():
    import os, json, tempfile, sys
    sys.path.insert(0, 'backend')
    from backend.livecard_meta import create_sentinel
    from backend.scheduler import _poll_sentinels

    tmp = tempfile.mkdtemp()
    target = os.path.join(tmp, 'target.txt')
    with open(target, 'w') as f:
        f.write('v1')
    meta = create_sentinel(target=target, fields=['content'], interval=1)
    state1 = _poll_sentinels([meta])
    with open(target, 'w') as f:
        f.write('v2')
    state2 = _poll_sentinels([meta])
    assert state1[0]['changed'] is False
    assert state2[0]['changed'] is True
    assert 'content' in json.dumps(state2[0]['diff'])
    print('sentinel ok')
